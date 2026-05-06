from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
import re
from statistics import median
from typing import Any

from .memory import build_athlete_memory
from .utils import chunk_weekly, parse_date, start_of_planned_week, sum_minutes


DAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
DAY_PATTERN = "|".join(day.lower() for day in DAY_NAMES)
DISCIPLINE_ALIASES = {
    "Ride": "Ride",
    "VirtualRide": "Ride",
    "Run": "Run",
    "TrailRun": "Run",
    "VirtualRun": "Run",
    "Swim": "Swim",
    "WeightTraining": "WeightTraining",
}
PRIOR_PLAN_MOVE_TOLERANCE_DAYS = 2
TIME_CAP_RE = re.compile(
    rf"\b(?P<day>{DAY_PATTERN})\b[^0-9]*(?:max(?:imum)?|capped at|only)?[^0-9]*(?P<minutes>\d{{2,3}})\s*(?:minutes|min)\b",
    re.IGNORECASE,
)
FLEXIBLE_CAP_RE = re.compile(
    r"\b(guideline|not a hard cap|flexible|align on|align later|optimi[sz]e roi|roi)\b",
    re.IGNORECASE,
)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _average_numeric_field(
    items: list[dict[str, Any]],
    field: str,
    *,
    transform=lambda value: float(value),
) -> float | None:
    values: list[float] = []
    for item in items:
        raw = item.get(field)
        if raw in (None, ""):
            continue
        try:
            values.append(float(transform(raw)))
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    return sum(values) / len(values)


def _append_unique(target: list[str], values: list[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _day_index(day_name: str) -> int:
    try:
        return DAY_NAMES.index(day_name)
    except ValueError:
        return len(DAY_NAMES)


def _sorted_days(days: list[str]) -> list[str]:
    return sorted(dict.fromkeys(days), key=_day_index)


def _day_mentions(text: str) -> list[str]:
    lowered = (text or "").lower()
    return [day for day in DAY_NAMES if day.lower() in lowered]


def _schedule_days(
    athlete_profile: dict[str, Any], keywords: tuple[str, ...]
) -> list[str]:
    matches: list[str] = []
    schedule = athlete_profile.get("regular_training_schedule", {})
    for day in DAY_NAMES:
        lowered = str(schedule.get(day, "")).lower()
        if any(keyword in lowered for keyword in keywords):
            matches.append(day)
    return matches


def _parse_time_caps(*text_groups: list[str]) -> dict[str, int]:
    caps: dict[str, int] = {}
    for group in text_groups:
        for text in group:
            for match in TIME_CAP_RE.finditer(text or ""):
                caps[match.group("day").capitalize()] = int(match.group("minutes"))
    return caps


def _parse_days_for_keywords(texts: list[str], keywords: tuple[str, ...]) -> list[str]:
    matches: list[str] = []
    for text in texts:
        lowered = (text or "").lower()
        if any(keyword in lowered for keyword in keywords):
            _append_unique(matches, _day_mentions(text))
    return _sorted_days(matches)


def _parse_days_for_keywords_in_clauses(
    texts: list[str], keywords: tuple[str, ...]
) -> list[str]:
    matches: list[str] = []
    for text in texts:
        clauses = re.split(r"[.;,]", text or "")
        for clause in clauses:
            lowered = clause.lower()
            if any(keyword in lowered for keyword in keywords):
                _append_unique(matches, _day_mentions(clause))
    return matches


def _requested_week_type(weekly_checkin: dict[str, Any]) -> str | None:
    text = " ".join(
        str(weekly_checkin.get(field, ""))
        for field in ("athlete_notes", "last_week_summary", "unexpected_fatigue")
    ).lower()
    if any(
        token in text
        for token in ("recovery week", "easy week", "down week", "unload week")
    ):
        return "recovery"
    if "build week" in text:
        return "build"
    return None


def _coerce_optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _constraint_entry(
    kind: str,
    value: Any,
    *,
    source: str,
    constraint_type: str,
    confidence: str,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "value": value,
        "source": source,
        "type": constraint_type,
        "confidence": confidence,
    }


def _round_to_5(value: float) -> int:
    return int(round(value / 5.0) * 5)


def _time_budget_policy(
    athlete_profile: dict[str, Any], weekly_checkin: dict[str, Any]
) -> dict[str, Any]:
    requested_minutes = _coerce_optional_int(
        weekly_checkin.get("available_hours_override_min")
    )
    athlete_text = " ".join(
        str(value)
        for value in athlete_profile.get("decision_policy", {}).values()
        if value not in (None, "")
    )
    week_text = " ".join(
        [
            *(weekly_checkin.get("time_constraints") or []),
            str(weekly_checkin.get("athlete_notes", "") or ""),
        ]
    )
    flexible_language = bool(
        FLEXIBLE_CAP_RE.search(athlete_text) or FLEXIBLE_CAP_RE.search(week_text)
    )
    lowered_week_text = week_text.lower()
    hard_language = (
        "hard cap" in lowered_week_text and "not a hard cap" not in lowered_week_text
    )
    reference_minutes = requested_minutes
    if reference_minutes is None:
        try:
            reference_minutes = int(
                round(
                    float(
                        athlete_profile.get("typical_training_hours_per_week", 0) or 0
                    )
                    * 60
                )
            )
        except (TypeError, ValueError):
            reference_minutes = None
    if reference_minutes is None:
        reference_minutes = 480
    mode = "hard" if requested_minutes is not None and hard_language else "flexible"
    if requested_minutes is not None and not flexible_language and not hard_language:
        mode = "hard"
    return {
        "mode": mode,
        "reference_min": _round_to_5(reference_minutes),
        "requested_min": requested_minutes,
        "source": (
            "weekly_checkin.available_hours_override_min"
            if requested_minutes is not None
            else "athlete_profile.typical_training_hours_per_week"
        ),
    }


def _step_minutes(step: dict[str, Any]) -> int:
    duration = str(step.get("duration", "")).lower()
    match = re.match(r"(?P<minutes>\d+)\s*(?:m|min)\b", duration)
    if not match:
        return 0
    return int(match.group("minutes"))


def _section_minutes(section: dict[str, Any]) -> int:
    repeat = max(int(section.get("repeat", 1) or 1), 1)
    return repeat * sum(_step_minutes(step) for step in section.get("steps", []))


def _cooldown_minutes(session: dict[str, Any]) -> int:
    total = 0
    for section in session.get("blocks", []):
        label = str(section.get("section", "")).lower()
        if "cool" in label or "finish" in label:
            total += _section_minutes(section)
    return total


def _session_completion_targets(session: dict[str, Any]) -> tuple[int, int]:
    planned_minutes = max(int(session.get("duration_min", 0) or 0), 1)
    cooldown_minutes = min(
        _cooldown_minutes(session),
        max(planned_minutes - 15, 0),
    )
    cooldown_tolerance = min(
        cooldown_minutes,
        max(5, int(round(planned_minutes * 0.12))),
    )
    essential_minutes = max(
        planned_minutes - cooldown_tolerance, int(planned_minutes * 0.75)
    )
    return essential_minutes, cooldown_tolerance


def _session_review_note(
    session: dict[str, Any],
    match: dict[str, Any] | None,
    status: str,
    actual_minutes: float,
    duration_ratio: float,
    pragmatic_ratio: float,
) -> str:
    if status == "missed":
        return "No matching session was found, so this one should be treated as missed rather than analyzed."
    avg_hr = match.get("average_heartrate") if match else None
    load = match.get("icu_training_load") if match else None
    metric_bits: list[str] = []
    if avg_hr not in (None, ""):
        metric_bits.append(f"avg HR {int(round(float(avg_hr)))} bpm")
    if load not in (None, ""):
        metric_bits.append(f"load {int(round(float(load)))}")
    metric_text = f" ({', '.join(metric_bits)})" if metric_bits else ""
    if status == "completed_pragmatic":
        return (
            "The key work appears to have landed cleanly and only non-essential volume was left out"
            f"{metric_text}."
        )
    if status == "shortened":
        if duration_ratio >= 0.75 or pragmatic_ratio >= 0.8:
            return (
                "Most of the useful work was still there, but enough volume was trimmed that it should count as shortened"
                f"{metric_text}."
            )
        return f"The session lost too much core work to count as complete{metric_text}."
    if status == "completed_moved":
        return f"The session was completed on a different day, with the main workload preserved{metric_text}."
    return f"The session was completed as planned and the metrics look usable for interpretation{metric_text}."


def _normalize_planning_constraints(
    athlete_profile: dict[str, Any], weekly_checkin: dict[str, Any]
) -> dict[str, Any]:
    open_water_mode = bool(weekly_checkin.get("open_water_mode"))
    hard_constraints = list(
        athlete_profile.get("hard_constraints", {}).get("always", [])
    )
    if not open_water_mode:
        hard_constraints.extend(
            athlete_profile.get("hard_constraints", {}).get(
                "when_open_water_mode_false", []
            )
        )
    fixed_preferences = list(athlete_profile.get("fixed_weekly_preferences", []))
    known_constraints = list(athlete_profile.get("known_constraints", []))
    time_constraints = list(weekly_checkin.get("time_constraints", []))
    weekly_request_texts = [
        *time_constraints,
        str(weekly_checkin.get("athlete_notes", "") or ""),
    ]
    weekday_preferences = list(
        athlete_profile.get("soft_preferences", {}).get("weekday_rhythm", [])
    )
    weekend_preferences = list(
        athlete_profile.get("soft_preferences", {}).get("weekend_structure", [])
    )

    no_bike_days = _parse_days_for_keywords(
        [*hard_constraints, *known_constraints, *time_constraints],
        (
            "no bike",
            "no bike access",
            "no access to a bike",
            "cannot be a bike day",
            "bike is impractical",
            "free of bike",
        ),
    )
    requested_ride_days = []
    for text in weekly_request_texts:
        clauses = re.split(r"[.;,]", text or "")
        for clause in clauses:
            lowered = clause.lower()
            if any(
                phrase in lowered
                for phrase in (
                    "no bike",
                    "no bike access",
                    "no access to a bike",
                    "cannot bike",
                    "cannot be a bike day",
                    "bike is impractical",
                    "free of bike",
                )
            ):
                continue
            if any(keyword in lowered for keyword in ("ride", "bike", "cycling")):
                _append_unique(requested_ride_days, _day_mentions(clause))
    requested_ride_days = _sorted_days(requested_ride_days)
    requested_run_days = _parse_days_for_keywords_in_clauses(
        weekly_request_texts, ("run",)
    )
    no_bike_days = [day for day in no_bike_days if day not in requested_ride_days]
    fixed_swim_days = _parse_days_for_keywords(
        [*hard_constraints, *fixed_preferences, *time_constraints],
        ("swim", "pool", "club swim"),
    )
    preferred_double_days = _parse_days_for_keywords(
        [
            *fixed_preferences,
            *weekday_preferences,
            *list(athlete_profile.get("regular_training_schedule", {}).values()),
        ],
        ("run plus", "run and", "run +", "club swim", "pool swim"),
    )
    strength_only_days = _parse_days_for_keywords(
        [*known_constraints, *time_constraints], ("gym only", "strength only")
    )
    parsed_time_caps = _parse_time_caps(
        hard_constraints, known_constraints, time_constraints
    )
    explicit_time_caps = {
        day: int(minutes)
        for day, minutes in weekly_checkin.get("time_caps_by_day", {}).items()
        if _coerce_optional_int(minutes) is not None
    }
    parsed_time_caps.update(explicit_time_caps)
    time_budget_policy = _time_budget_policy(athlete_profile, weekly_checkin)
    double_capacity = {
        day: bool(value)
        for day, value in weekly_checkin.get(
            "double_session_capacity_by_day", {}
        ).items()
    }

    for day in fixed_swim_days:
        double_capacity.setdefault(day, day in preferred_double_days)

    hard_rules = [
        _constraint_entry(
            "day_off",
            day,
            source="athlete_profile.non_negotiable_days_off",
            constraint_type="hard",
            confidence="high",
        )
        for day in _sorted_days([*athlete_profile.get("non_negotiable_days_off", [])])
    ]
    hard_rules.extend(
        _constraint_entry(
            "no_bike_day",
            day,
            source=(
                "weekly_checkin.time_constraints"
                if time_constraints
                else "athlete_profile.known_constraints"
            ),
            constraint_type="hard",
            confidence="high",
        )
        for day in no_bike_days
    )
    hard_rules.extend(
        _constraint_entry(
            "fixed_swim_day",
            day,
            source="athlete_profile.hard_constraints",
            constraint_type="hard",
            confidence="medium",
        )
        for day in fixed_swim_days
    )
    hard_rules.extend(
        _constraint_entry(
            "day_time_cap",
            {"day": day, "minutes": minutes},
            source="weekly_checkin.time_caps_by_day",
            constraint_type="hard",
            confidence="high",
        )
        for day, minutes in sorted(
            parsed_time_caps.items(), key=lambda item: _day_index(item[0])
        )
    )
    if time_budget_policy["mode"] == "hard" and weekly_checkin.get(
        "available_hours_override_min"
    ) not in (None, ""):
        hard_rules.append(
            _constraint_entry(
                "weekly_time_cap",
                int(weekly_checkin["available_hours_override_min"]),
                source="weekly_checkin.available_hours_override_min",
                constraint_type="hard",
                confidence="high",
            )
        )
    for day, allowed in sorted(
        double_capacity.items(), key=lambda item: _day_index(item[0])
    ):
        if allowed is False:
            hard_rules.append(
                _constraint_entry(
                    "no_double_day",
                    day,
                    source="weekly_checkin.double_session_capacity_by_day",
                    constraint_type="hard",
                    confidence="high",
                )
            )

    soft_preferences = []
    if athlete_profile.get("preferred_long_ride_day"):
        soft_preferences.append(
            _constraint_entry(
                "preferred_long_ride_day",
                athlete_profile["preferred_long_ride_day"],
                source="athlete_profile.preferred_long_ride_day",
                constraint_type="soft",
                confidence="medium",
            )
        )
    if athlete_profile.get("preferred_long_run_day"):
        soft_preferences.append(
            _constraint_entry(
                "preferred_long_run_day",
                athlete_profile["preferred_long_run_day"],
                source="athlete_profile.preferred_long_run_day",
                constraint_type="soft",
                confidence="medium",
            )
        )
    soft_preferences.extend(
        _constraint_entry(
            "strength_day",
            day,
            source="athlete_profile.strength_days",
            constraint_type="soft",
            confidence="medium",
        )
        for day in athlete_profile.get("strength_days", [])
    )
    soft_preferences.extend(
        _constraint_entry(
            "preferred_double_day",
            day,
            source="athlete_profile.soft_preferences.weekday_rhythm",
            constraint_type="soft",
            confidence="medium",
        )
        for day in preferred_double_days
    )
    soft_preferences.extend(
        _constraint_entry(
            "requested_ride_day",
            day,
            source="weekly_checkin",
            constraint_type="soft",
            confidence="high",
        )
        for day in requested_ride_days
    )
    soft_preferences.extend(
        _constraint_entry(
            "requested_run_day",
            day,
            source="weekly_checkin",
            constraint_type="soft",
            confidence="high",
        )
        for day in requested_run_days
    )

    inferred_preferences = []
    inferred_preferences.extend(
        _constraint_entry(
            "preferred_ride_day",
            day,
            source="athlete_profile.regular_training_schedule",
            constraint_type="inferred",
            confidence="low",
        )
        for day in _schedule_days(athlete_profile, ("bike", "ride", "cycling"))
    )
    inferred_preferences.extend(
        _constraint_entry(
            "preferred_run_day",
            day,
            source="athlete_profile.regular_training_schedule",
            constraint_type="inferred",
            confidence="low",
        )
        for day in _schedule_days(athlete_profile, ("run",))
    )
    inferred_preferences.extend(
        _constraint_entry(
            "preferred_swim_day",
            day,
            source="athlete_profile.regular_training_schedule",
            constraint_type="inferred",
            confidence="low",
        )
        for day in _schedule_days(athlete_profile, ("swim", "pool", "club"))
    )

    return {
        "open_water_mode": open_water_mode,
        "days_off": _sorted_days(
            [
                *athlete_profile.get("non_negotiable_days_off", []),
            ]
        ),
        "no_bike_days": no_bike_days,
        "fixed_swim_days": fixed_swim_days,
        "preferred_double_days": preferred_double_days,
        "double_session_capacity_by_day": double_capacity,
        "time_caps_by_day": parsed_time_caps,
        "available_hours_override_min": (
            weekly_checkin.get("available_hours_override_min")
            if time_budget_policy["mode"] == "hard"
            else None
        ),
        "requested_available_hours_override_min": weekly_checkin.get(
            "available_hours_override_min"
        ),
        "weekly_time_budget_policy": time_budget_policy,
        "travel_this_week": bool(weekly_checkin.get("travel_this_week")),
        "preferred_long_ride_day": athlete_profile.get("preferred_long_ride_day"),
        "preferred_long_run_day": athlete_profile.get("preferred_long_run_day"),
        "strength_days": athlete_profile.get("strength_days", []),
        "preferred_ride_days": list(
            dict.fromkeys(
                [
                    *requested_ride_days,
                    *_schedule_days(athlete_profile, ("bike", "ride", "cycling")),
                ]
            )
        ),
        "preferred_run_days": list(
            dict.fromkeys(
                [*requested_run_days, *_schedule_days(athlete_profile, ("run",))]
            )
        ),
        "preferred_swim_days": _schedule_days(
            athlete_profile, ("swim", "pool", "club")
        ),
        "strength_only_days": strength_only_days,
        "requested_week_type": _requested_week_type(weekly_checkin),
        "current_block_week": _coerce_optional_int(
            weekly_checkin.get("current_block_week")
        ),
        "raw_time_constraints": time_constraints,
        "known_constraints": known_constraints,
        "decision_policy": athlete_profile.get("decision_policy", {}),
        "weekday_preferences": weekday_preferences,
        "weekend_preferences": weekend_preferences,
        "hard_rules": hard_rules,
        "soft_preferences": soft_preferences,
        "inferred_preferences": inferred_preferences,
        "override_explanations_required": [
            item["kind"] for item in [*soft_preferences, *inferred_preferences]
        ],
        "constraint_confidence": {
            "hard": "high",
            "soft": "medium",
            "inferred": "low",
        },
    }


def _normalize_discipline(value: Any) -> str:
    normalized = DISCIPLINE_ALIASES.get(str(value), str(value or "Other"))
    if normalized.endswith("Run"):
        return "Run"
    if normalized.endswith("Ride"):
        return "Ride"
    return normalized


def _match_score(
    session: dict[str, Any], activity: dict[str, Any]
) -> tuple[int, float]:
    day_diff = abs(
        (
            parse_date(activity.get("start_date_local", "1970-01-01"))
            - parse_date(session["date"])
        ).days
    )
    planned_minutes = int(session.get("duration_min", 0) or 0)
    actual_minutes = _safe_float(activity.get("moving_time")) / 60.0
    return (day_diff, abs(actual_minutes - planned_minutes))


def _session_type_label(session: dict[str, Any]) -> str:
    discipline = session.get("discipline")
    name = str(session.get("session_name", "")).lower()
    if discipline == "Ride" and "long" in name:
        return "long_ride"
    if discipline == "Run" and "long" in name:
        return "long_run"
    if discipline == "Ride" and session.get("priority") == "key":
        return "key_ride"
    if discipline == "Run" and session.get("primary_metric") == "Pace":
        return "quality_run"
    if discipline == "Swim":
        return "swim_routine"
    if discipline == "WeightTraining":
        return "strength"
    if discipline == "Recovery":
        return "recovery"
    return f"support_{str(discipline or 'other').lower()}"


def _single_prior_plan_adherence(
    prior_plan_context: dict[str, Any] | None, activities: list[dict[str, Any]]
) -> dict[str, Any]:
    if not prior_plan_context:
        return {
            "status": "none",
            "summary": "No prior approved plan was available for adherence review.",
        }

    final_plan = prior_plan_context.get("final_plan", {})
    planned_sessions = [
        session
        for session in final_plan.get("sessions", [])
        if session.get("discipline") != "Recovery"
    ]
    if not planned_sessions:
        return {
            "status": "none",
            "summary": "No prior approved workouts were available for adherence review.",
        }

    plan_start = min(parse_date(session["date"]) for session in planned_sessions)
    plan_end = max(parse_date(session["date"]) for session in planned_sessions)
    candidate_activities = [
        activity
        for activity in activities
        if plan_start - timedelta(days=PRIOR_PLAN_MOVE_TOLERANCE_DAYS)
        <= parse_date(activity.get("start_date_local", "1970-01-01"))
        <= plan_end + timedelta(days=PRIOR_PLAN_MOVE_TOLERANCE_DAYS)
    ]
    unmatched = list(candidate_activities)
    completed_on_day = 0
    completed_elsewhere = 0
    shortened = 0
    missed = 0
    key_sessions_planned = 0
    key_sessions_completed = 0
    moved_sessions: list[str] = []
    shortened_sessions: list[str] = []
    missed_sessions: list[str] = []
    total_score = 0.0
    pragmatic_completions = 0
    day_breakdown: dict[str, dict[str, int]] = defaultdict(
        lambda: {"planned": 0, "completed_any": 0, "completed_on_day": 0}
    )
    session_type_breakdown: dict[str, dict[str, int]] = defaultdict(
        lambda: {"planned": 0, "completed_any": 0, "completed_on_day": 0}
    )
    double_day_planned = 0
    double_day_fully_completed = 0
    planned_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    completion_by_day: dict[str, int] = defaultdict(int)
    session_reviews: list[dict[str, Any]] = []

    for session in planned_sessions:
        planned_by_day[session["day"]].append(session)

    for session in planned_sessions:
        if session.get("priority") == "key":
            key_sessions_planned += 1
        day_breakdown[session["day"]]["planned"] += 1
        session_type = _session_type_label(session)
        session_type_breakdown[session_type]["planned"] += 1
        matches = [
            activity
            for activity in unmatched
            if _normalize_discipline(activity.get("type")) == session.get("discipline")
            and abs(
                (
                    parse_date(activity.get("start_date_local", "1970-01-01"))
                    - parse_date(session["date"])
                ).days
            )
            <= PRIOR_PLAN_MOVE_TOLERANCE_DAYS
        ]
        if not matches:
            missed += 1
            missed_sessions.append(session["session_name"])
            session_reviews.append(
                {
                    "session_name": session["session_name"],
                    "day": session["day"],
                    "date": session["date"],
                    "discipline": session.get("discipline"),
                    "planned_minutes": int(session.get("duration_min", 0) or 0),
                    "actual_minutes": 0.0,
                    "essential_minutes": _session_completion_targets(session)[0],
                    "cooldown_tolerance_min": _session_completion_targets(session)[1],
                    "completion_ratio": 0.0,
                    "pragmatic_ratio": 0.0,
                    "status": "missed",
                    "average_heartrate": None,
                    "training_load": None,
                    "interpretation": _session_review_note(
                        session,
                        None,
                        "missed",
                        0.0,
                        0.0,
                        0.0,
                    ),
                }
            )
            continue

        match = min(matches, key=lambda item: _match_score(session, item))
        unmatched.remove(match)
        day_delta = (
            parse_date(match.get("start_date_local", "1970-01-01"))
            - parse_date(session["date"])
        ).days
        planned_minutes = max(int(session.get("duration_min", 0) or 0), 1)
        actual_minutes = _safe_float(match.get("moving_time")) / 60.0
        essential_minutes, cooldown_tolerance = _session_completion_targets(session)
        completion_ratio = actual_minutes / planned_minutes
        pragmatic_ratio = actual_minutes / max(essential_minutes, 1)
        review_status = "completed_on_day"
        if pragmatic_ratio < 0.55:
            missed += 1
            missed_sessions.append(session["session_name"])
            review_status = "missed"
            session_reviews.append(
                {
                    "session_name": session["session_name"],
                    "day": session["day"],
                    "date": session["date"],
                    "discipline": session.get("discipline"),
                    "planned_minutes": planned_minutes,
                    "actual_minutes": round(actual_minutes, 1),
                    "essential_minutes": essential_minutes,
                    "cooldown_tolerance_min": cooldown_tolerance,
                    "completion_ratio": round(completion_ratio, 2),
                    "pragmatic_ratio": round(pragmatic_ratio, 2),
                    "status": review_status,
                    "average_heartrate": match.get("average_heartrate"),
                    "training_load": match.get("icu_training_load"),
                    "interpretation": _session_review_note(
                        session,
                        match,
                        review_status,
                        actual_minutes,
                        completion_ratio,
                        pragmatic_ratio,
                    ),
                }
            )
            continue

        if completion_ratio < 0.7 and pragmatic_ratio < 0.9:
            shortened += 1
            shortened_sessions.append(session["session_name"])
            total_score += 0.6 if day_delta == 0 else 0.5
            day_breakdown[session["day"]]["completed_any"] += 1
            session_type_breakdown[session_type]["completed_any"] += 1
            completion_by_day[session["day"]] += 1
            review_status = "shortened"
        elif completion_ratio < 1.0 and pragmatic_ratio >= 0.9:
            pragmatic_completions += 1
            total_score += 1.0 if day_delta == 0 else 0.9
            day_breakdown[session["day"]]["completed_any"] += 1
            session_type_breakdown[session_type]["completed_any"] += 1
            if day_delta == 0:
                completed_on_day += 1
                day_breakdown[session["day"]]["completed_on_day"] += 1
                session_type_breakdown[session_type]["completed_on_day"] += 1
                review_status = "completed_pragmatic"
            else:
                completed_elsewhere += 1
                moved_sessions.append(session["session_name"])
                review_status = "completed_moved"
            completion_by_day[session["day"]] += 1
        elif day_delta == 0:
            completed_on_day += 1
            total_score += 1.0
            day_breakdown[session["day"]]["completed_any"] += 1
            day_breakdown[session["day"]]["completed_on_day"] += 1
            session_type_breakdown[session_type]["completed_any"] += 1
            session_type_breakdown[session_type]["completed_on_day"] += 1
            completion_by_day[session["day"]] += 1
        else:
            completed_elsewhere += 1
            moved_sessions.append(session["session_name"])
            total_score += 0.9
            day_breakdown[session["day"]]["completed_any"] += 1
            session_type_breakdown[session_type]["completed_any"] += 1
            completion_by_day[session["day"]] += 1
            review_status = "completed_moved"

        session_reviews.append(
            {
                "session_name": session["session_name"],
                "day": session["day"],
                "date": session["date"],
                "discipline": session.get("discipline"),
                "planned_minutes": planned_minutes,
                "actual_minutes": round(actual_minutes, 1),
                "essential_minutes": essential_minutes,
                "cooldown_tolerance_min": cooldown_tolerance,
                "completion_ratio": round(completion_ratio, 2),
                "pragmatic_ratio": round(pragmatic_ratio, 2),
                "status": review_status,
                "average_heartrate": match.get("average_heartrate"),
                "training_load": match.get("icu_training_load"),
                "interpretation": _session_review_note(
                    session,
                    match,
                    review_status,
                    actual_minutes,
                    completion_ratio,
                    pragmatic_ratio,
                ),
            }
        )

        if session.get("priority") == "key" and pragmatic_ratio >= 0.9:
            key_sessions_completed += 1

    for day, day_sessions in planned_by_day.items():
        if len(day_sessions) < 2:
            continue
        double_day_planned += 1
        if completion_by_day.get(day, 0) == len(day_sessions):
            double_day_fully_completed += 1

    total_sessions = len(planned_sessions)
    adherence_score = round(total_score / total_sessions, 2) if total_sessions else 0.0
    summary_parts = [
        f"Completed {completed_on_day + completed_elsewhere}/{total_sessions} planned sessions"
    ]
    if moved_sessions:
        summary_parts.append(f"moved {len(moved_sessions)}")
    if shortened_sessions:
        summary_parts.append(f"shortened {len(shortened_sessions)}")
    if pragmatic_completions:
        summary_parts.append(f"pragmatically completed {pragmatic_completions}")
    if missed_sessions:
        summary_parts.append(f"missed {len(missed_sessions)}")

    return {
        "status": "available",
        "source_run": prior_plan_context.get("source_run"),
        "planned_week_start": plan_start.isoformat(),
        "planned_week_end": plan_end.isoformat(),
        "planned_sessions": total_sessions,
        "completed_on_planned_day": completed_on_day,
        "completed_on_other_day": completed_elsewhere,
        "shortened_sessions_count": shortened,
        "missed_sessions_count": missed,
        "key_sessions_planned": key_sessions_planned,
        "key_sessions_completed": key_sessions_completed,
        "moved_sessions": moved_sessions,
        "shortened_sessions": shortened_sessions,
        "missed_sessions": missed_sessions,
        "pragmatic_completions_count": pragmatic_completions,
        "day_breakdown": day_breakdown,
        "session_type_breakdown": session_type_breakdown,
        "double_days": {
            "planned": double_day_planned,
            "fully_completed": double_day_fully_completed,
        },
        "session_reviews": session_reviews,
        "completion_policy_note": (
            "Short cooldown trims are treated pragmatically when the core work is still present."
        ),
        "adherence_score": adherence_score,
        "summary": ", ".join(summary_parts) + ".",
    }


def _prior_plan_review(
    prior_plan_contexts: list[dict[str, Any]], activities: list[dict[str, Any]]
) -> dict[str, Any]:
    if not prior_plan_contexts:
        latest = _single_prior_plan_adherence(None, activities)
        return {
            "latest": latest,
            "history": [],
            "status": latest["status"],
            "weeks_reviewed": 0,
            "average_adherence_score": None,
            "summary": latest["summary"],
        }

    history = [
        _single_prior_plan_adherence(context, activities)
        for context in prior_plan_contexts
        if context is not None
    ]
    available_history = [item for item in history if item.get("status") == "available"]
    latest = (
        available_history[0]
        if available_history
        else _single_prior_plan_adherence(None, activities)
    )
    average_adherence_score = None
    if available_history:
        average_adherence_score = round(
            sum(
                float(item.get("adherence_score", 0.0) or 0.0)
                for item in available_history
            )
            / len(available_history),
            2,
        )
    return {
        "latest": latest,
        "history": history,
        "status": "available" if available_history else latest["status"],
        "weeks_reviewed": len(available_history),
        "average_adherence_score": average_adherence_score,
        "summary": latest["summary"],
    }


def _session_archetype(session: dict[str, Any]) -> str | None:
    discipline = session.get("discipline")
    name = str(session.get("session_name", "")).lower()
    if discipline == "Recovery":
        return "recovery"
    if discipline == "WeightTraining":
        return "strength"
    if discipline == "Swim":
        return "swim_club" if session.get("day") == "Tuesday" else "swim_pool"
    if discipline == "Run" and "long" in name:
        return "long_run"
    if discipline == "Ride" and "long" in name:
        return "long_ride"
    if discipline == "Run" and (
        session.get("primary_metric") == "Pace"
        or session.get("daniels_type") in {"T", "I", "R"}
    ):
        return "quality_run"
    if discipline == "Ride" and (
        session.get("priority") == "key"
        or session.get("coggan_level") in {"L4 Threshold", "L5 VO2max", "L6 Anaerobic"}
        or float(session.get("expected_if", 0) or 0) >= 0.78
    ):
        return "threshold_ride"
    if discipline == "Ride" and (
        "cadence" in name or session.get("coggan_level") == "L3 Tempo"
    ):
        return "support_ride"
    if discipline == "Run":
        return "easy_run"
    if discipline == "Ride":
        return "endurance_ride"
    return None


def _recent_session_signatures(
    prior_plan_contexts: list[dict[str, Any]], limit_weeks: int = 4
) -> dict[str, list[str]]:
    signatures: dict[str, list[str]] = defaultdict(list)
    for context in prior_plan_contexts[:limit_weeks]:
        final_plan = context.get("final_plan", {})
        for session in final_plan.get("sessions", []):
            archetype = _session_archetype(session)
            if not archetype:
                continue
            for label in (session.get("variant_id"), session.get("session_name")):
                if not label:
                    continue
                normalized = str(label)
                if normalized not in signatures[archetype]:
                    signatures[archetype].append(normalized)
    return dict(signatures)


def _sport_settings_map(settings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for item in settings:
        for sport in item.get("types", []):
            mapping[sport] = item
    return mapping


def _confidence_label(
    baseline_count: int, acute_count: int, minimum_entries: int
) -> str:
    if baseline_count >= minimum_entries and acute_count >= 3:
        return "high"
    if baseline_count >= max(7, minimum_entries // 2) and acute_count >= 2:
        return "medium"
    if baseline_count >= 1 and acute_count >= 1:
        return "low"
    return "missing"


def _derive_wellness_baseline(
    wellness: list[dict[str, Any]],
    field: str,
    latest_day,
    *,
    baseline_days: int,
    acute_days: int,
    minimum_entries: int,
    transform=lambda value: float(value),
) -> dict[str, Any]:
    series: list[tuple[Any, float]] = []
    for item in wellness:
        raw = item.get(field)
        if raw in (None, ""):
            continue
        try:
            series.append((parse_date(item["id"]), float(transform(raw))))
        except (KeyError, TypeError, ValueError):
            continue

    acute_start = latest_day - timedelta(days=acute_days - 1)
    baseline_start = acute_start - timedelta(days=baseline_days)
    baseline_values = [
        value for day, value in series if baseline_start <= day < acute_start
    ]
    if not baseline_values:
        baseline_values = [value for day, value in series if day < acute_start]
    acute_values = [value for day, value in series if acute_start <= day <= latest_day]

    if not baseline_values:
        return {
            "baseline": None,
            "acute": None,
            "delta": None,
            "delta_pct": None,
            "baseline_entries": 0,
            "acute_entries": len(acute_values),
            "confidence": "missing",
            "source": "intervals",
        }

    baseline = round(float(median(baseline_values)), 2)
    acute = round(
        float(sum(acute_values) / len(acute_values)) if acute_values else baseline, 2
    )
    delta = round(acute - baseline, 2)
    delta_pct = round(((acute - baseline) / baseline) * 100.0, 2) if baseline else None
    return {
        "baseline": baseline,
        "acute": acute,
        "delta": delta,
        "delta_pct": delta_pct,
        "baseline_entries": len(baseline_values),
        "acute_entries": len(acute_values),
        "confidence": _confidence_label(
            len(baseline_values), len(acute_values), minimum_entries
        ),
        "source": "intervals",
    }


def _classify_periodization_state(
    weekly_windows: list[dict[str, Any]],
) -> dict[str, Any]:
    classified: list[dict[str, Any]] = []
    for index, window in enumerate(weekly_windows):
        load = float(window.get("load", 0))
        minutes = float(window.get("minutes", 0))
        week_type = "load"
        if index >= 2:
            prior_loads = [
                float(weekly_windows[index - 1]["load"]),
                float(weekly_windows[index - 2]["load"]),
            ]
            prior_minutes = [
                float(weekly_windows[index - 1]["minutes"]),
                float(weekly_windows[index - 2]["minutes"]),
            ]
            avg_load = sum(prior_loads) / len(prior_loads)
            avg_minutes = sum(prior_minutes) / len(prior_minutes)
            if (avg_load and load <= avg_load * 0.82) or (
                avg_minutes and minutes <= avg_minutes * 0.82
            ):
                week_type = "recovery"
        classified.append({**window, "week_type": week_type})

    consecutive_load_weeks = 0
    for window in reversed(classified):
        if window.get("load", 0) == 0 and window.get("minutes", 0) == 0:
            continue
        if window.get("week_type") == "recovery":
            break
        consecutive_load_weeks += 1

    last_week_type = classified[-1]["week_type"] if classified else "unknown"
    recommended_next_week_type = "recovery" if consecutive_load_weeks >= 3 else "load"
    next_block_week = (
        4
        if recommended_next_week_type == "recovery"
        else (1 if last_week_type == "recovery" else min(consecutive_load_weeks + 1, 3))
    )
    return {
        "classified_weeks": classified,
        "consecutive_load_weeks": consecutive_load_weeks,
        "last_week_type": last_week_type,
        "recommended_next_week_type": recommended_next_week_type,
        "next_block_week": next_block_week,
    }


def _infer_distance_from_text(*values: str | None) -> str | None:
    text = " ".join(value for value in values if value).lower()
    checks = [
        ("70.3", "Ironman 70.3"),
        ("half", "Ironman 70.3"),
        ("middle", "Ironman 70.3"),
        ("olympic", "Olympic"),
        ("sprint", "Sprint"),
        ("10k", "10k"),
        ("half marathon", "Half Marathon"),
        ("marathon", "Marathon"),
    ]
    for needle, label in checks:
        if needle in text:
            return label
    return None


def _interval_event_priority(category: str | None) -> str | None:
    mapping = {
        "RACE_A": "A",
        "RACE_B": "B",
        "RACE_C": "C",
        "TARGET": "A",
    }
    return mapping.get(category or "")


def _extract_interval_races(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    races = []
    for event in events:
        priority = _interval_event_priority(event.get("category"))
        if not priority:
            continue
        races.append(
            {
                "name": event.get("name", "Untitled race"),
                "date": event.get("start_date_local", "")[:10],
                "priority": priority,
                "distance": _infer_distance_from_text(
                    event.get("type"), event.get("name"), event.get("description")
                )
                or event.get("type")
                or "Unknown",
                "notes": event.get("description", ""),
                "source": "intervals",
                "tags": event.get("tags", []),
            }
        )
    return races


def _merge_race_calendars(
    interval_races: list[dict[str, Any]], manual_races: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for race in interval_races:
        key = (race.get("date", ""), race.get("name", ""))
        merged[key] = dict(race)
    for race in manual_races:
        key = (race.get("date", ""), race.get("name", ""))
        existing = merged.get(key, {})
        combined = dict(existing)
        combined.update({k: v for k, v in race.items() if v not in (None, "", [])})
        combined.setdefault("source", "manual")
        if existing.get("source") == "intervals":
            combined["source"] = "intervals+manual"
        merged[key] = combined
    return list(merged.values())


def normalize_snapshot(
    local_inputs: dict[str, Any], intervals_data: dict[str, Any]
) -> dict[str, Any]:
    athlete_profile = local_inputs["athlete_profile"]
    weekly_checkin = local_inputs["weekly_checkin"]
    athlete_state = local_inputs.get("athlete_state", {})
    race_calendar = local_inputs["race_calendar"]
    prior_plan_contexts = local_inputs.get("prior_plan_contexts", [])
    activities = sorted(
        intervals_data["activities"], key=lambda item: item["start_date_local"]
    )
    wellness = sorted(intervals_data["wellness"], key=lambda item: item["id"])
    planning_date = parse_date(weekly_checkin["planning_date"])
    week_start = start_of_planned_week(weekly_checkin["planning_date"])
    week_windows = chunk_weekly(planning_date, activities, weeks=8)

    latest_wellness = wellness[-1] if wellness else {}
    recent_wellness = wellness[-7:] if len(wellness) >= 7 else wellness
    latest_wellness_day = (
        parse_date(latest_wellness["id"]) if latest_wellness else planning_date
    )
    latest_activity_day = (
        parse_date(activities[-1]["start_date_local"]) if activities else planning_date
    )
    sport_settings_map = _sport_settings_map(intervals_data.get("sport_settings", []))
    by_sport: dict[str, dict[str, float]] = defaultdict(
        lambda: {"sessions": 0, "load": 0.0, "minutes": 0.0}
    )
    for activity in activities:
        sport = activity.get("type", "Other")
        bucket = by_sport[sport]
        bucket["sessions"] += 1
        bucket["load"] += _safe_float(activity.get("icu_training_load"))
        bucket["minutes"] += _safe_float(activity.get("moving_time")) / 60.0

    recent_week = week_windows[-1]["activities"] if week_windows else []
    prior_week = week_windows[-2]["activities"] if len(week_windows) >= 2 else []
    recent_week_load = sum(
        _safe_float(item.get("icu_training_load")) for item in recent_week
    )
    prior_week_load = sum(
        _safe_float(item.get("icu_training_load")) for item in prior_week
    )
    recent_week_minutes = int(sum_minutes(recent_week) / 60)
    prior_week_minutes = int(sum_minutes(prior_week) / 60)
    load_change_pct = 0.0
    if prior_week_load:
        load_change_pct = (
            (recent_week_load - prior_week_load) / prior_week_load
        ) * 100.0

    interval_races = _extract_interval_races(intervals_data.get("events", []))
    merged_race_calendar = {
        "races": _merge_race_calendars(interval_races, race_calendar.get("races", []))
    }

    races = []
    for race in merged_race_calendar.get("races", []):
        race_day = parse_date(race["date"])
        enriched = dict(race)
        enriched["days_to_race"] = (race_day - week_start).days
        races.append(enriched)
    races.sort(key=lambda item: (item["days_to_race"], item["priority"]))

    assumptions = []
    if not sport_settings_map.get("Ride", {}).get("ftp"):
        assumptions.append(
            "Bike FTP missing; live compiler would fall back from %FTP to HR."
        )
    run_settings = sport_settings_map.get("Run", {})
    if not run_settings.get("threshold_pace"):
        assumptions.append(
            "Run threshold pace missing; quality run compiler would fall back from Pace to HR."
        )
    if not recent_wellness:
        assumptions.append("Wellness data missing; readiness confidence reduced.")
    if not races:
        assumptions.append(
            "No race events were found in Intervals or the optional local race calendar. Planning will stay general."
        )

    summarized_windows = [
        {
            "week_start": window["week_start"],
            "week_end": window["week_end"],
            "load": round(
                sum(
                    _safe_float(item.get("icu_training_load"))
                    for item in window["activities"]
                ),
                1,
            ),
            "minutes": int(sum_minutes(window["activities"]) / 60),
            "sessions": len(window["activities"]),
        }
        for window in week_windows
    ]
    derived_baselines = {
        "hrv": _derive_wellness_baseline(
            wellness,
            "hrv",
            latest_wellness_day,
            baseline_days=42,
            acute_days=7,
            minimum_entries=21,
        ),
        "resting_hr": _derive_wellness_baseline(
            wellness,
            "restingHR",
            latest_wellness_day,
            baseline_days=42,
            acute_days=7,
            minimum_entries=21,
        ),
        "sleep_hours": _derive_wellness_baseline(
            wellness,
            "sleepSecs",
            latest_wellness_day,
            baseline_days=28,
            acute_days=7,
            minimum_entries=14,
            transform=lambda value: float(value) / 3600,
        ),
    }
    periodization_state = _classify_periodization_state(summarized_windows)
    planning_constraints = _normalize_planning_constraints(
        athlete_profile, weekly_checkin
    )
    prior_plan_review = _prior_plan_review(prior_plan_contexts, activities)
    prior_plan_adherence = prior_plan_review["latest"]
    athlete_memory = build_athlete_memory(
        prior_plan_review["history"],
        planning_constraints,
        athlete_profile,
        weekly_checkin,
    )
    avg_sleep_hours = _average_numeric_field(
        recent_wellness,
        "sleepSecs",
        transform=lambda value: float(value) / 3600,
    )
    avg_hrv = _average_numeric_field(recent_wellness, "hrv")
    avg_resting_hr = _average_numeric_field(recent_wellness, "restingHR")
    freshness_reasons: list[str] = []
    latest_signal_day = max(latest_activity_day, latest_wellness_day)
    freshness_gap = (planning_date - latest_signal_day).days
    if freshness_gap > 1:
        freshness_reasons.append(
            f"Latest live data is {freshness_gap} day(s) older than the planning date."
        )
    if not activities:
        freshness_reasons.append("No activities were returned from Intervals.")
    if not recent_wellness:
        freshness_reasons.append(
            "No recent wellness entries were returned from Intervals."
        )
    freshness_status = "fresh" if not freshness_reasons else "stale"

    return {
        "athlete_profile": athlete_profile,
        "weekly_checkin": weekly_checkin,
        "athlete_state": athlete_state,
        "planning_constraints": planning_constraints,
        "prior_plan_review": prior_plan_review,
        "prior_plan_adherence": prior_plan_adherence,
        "prior_plan_history": prior_plan_review["history"],
        "recent_session_signatures_by_kind": _recent_session_signatures(
            prior_plan_contexts
        ),
        "athlete_memory": athlete_memory,
        "race_calendar": merged_race_calendar,
        "intervals": intervals_data,
        "week_start": week_start.isoformat(),
        "planning_date": planning_date.isoformat(),
        "latest_activity_day": latest_activity_day.isoformat(),
        "latest_wellness_day": latest_wellness_day.isoformat(),
        "freshness": {
            "status": freshness_status,
            "reasons": freshness_reasons,
            "gap_days": freshness_gap,
            "fetch_timestamp": intervals_data.get("fetch_timestamp"),
        },
        "latest_wellness": latest_wellness,
        "recent_wellness": recent_wellness,
        "sport_settings": sport_settings_map,
        "recent_week_load": round(recent_week_load, 1),
        "prior_week_load": round(prior_week_load, 1),
        "recent_week_minutes": recent_week_minutes,
        "prior_week_minutes": prior_week_minutes,
        "load_change_pct": round(load_change_pct, 1),
        "weekly_windows": summarized_windows,
        "avg_sleep_hours": (
            round(avg_sleep_hours, 2) if avg_sleep_hours is not None else None
        ),
        "avg_hrv": round(avg_hrv, 1) if avg_hrv is not None else None,
        "avg_resting_hr": (
            round(avg_resting_hr, 1) if avg_resting_hr is not None else None
        ),
        "derived_baselines": derived_baselines,
        "periodization_state": periodization_state,
        "sports_summary": by_sport,
        "races": races,
        "assumptions": assumptions,
    }
