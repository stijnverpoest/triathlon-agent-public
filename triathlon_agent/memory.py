from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from statistics import median
from typing import Any

from .utils import detect_red_flags, has_niggle, niggle_body_parts, parse_date


REPEATED_NIGGLE_MIN_WEEKS = 2
# Intended RPE when a session carries no rpe_target: easy ~3/10, quality ~7/10.
INTENDED_RPE = {"easy": 3.0, "quality": 7.0}
RPE_HARDER_MARGIN = 2.0
# Intervals Feel: 1 = strong (best) .. 5 = weak (worst).
FEEL_WEAK = 4
FEEL_STRONG = 2
ILLNESS_REASON_RE = re.compile(
    r"\b(sick|ill|illness|cold|flu|covid|corona|fever|ziek|verkoud\w*|griep|koorts|infection|infectie|"
    r"injur\w*|blessure|geblesseerd)\b"
)
RPE_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)(?:\s*-\s*(\d+(?:\.\d+)?))?")


def _intended_rpe(review: dict[str, Any]) -> float | None:
    target = review.get("rpe_target")
    if target:
        match = RPE_RANGE_RE.search(str(target))
        if match:
            high = match.group(2) or match.group(1)
            return float(high)
    return INTENDED_RPE.get(str(review.get("planned_intensity") or ""))


def _review_rpe(review: dict[str, Any]) -> float | None:
    for key in ("icu_rpe", "rpe", "session_rpe"):
        value = review.get(key)
        if value in (None, ""):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        # session_rpe in Intervals is RPE x minutes; only a 1-10 value is an RPE.
        if 0 < number <= 10:
            return number
    return None


def rpe_compliance(review: dict[str, Any]) -> str | None:
    """'harder_than_intended' when RPE sits 2+ points above the intended effort."""
    actual = _review_rpe(review)
    intended = _intended_rpe(review)
    if actual is None or intended is None:
        return None
    if actual >= intended + RPE_HARDER_MARGIN:
        return "harder_than_intended"
    if actual <= intended - 3:
        return "easier_than_intended"
    return "as_intended"


def _safe_day(value: Any) -> date | None:
    try:
        return parse_date(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _checkin_flags_illness(checkin: dict[str, Any] | None) -> bool:
    if not checkin:
        return False
    symptoms = checkin.get("illness_symptoms")
    if isinstance(symptoms, list) and any(str(item).strip() for item in symptoms):
        return True
    flags = checkin.get("red_flags")
    if isinstance(flags, list) and any(str(item).strip() for item in flags):
        return True
    reasons = checkin.get("missed_session_reasons")
    values = reasons.values() if isinstance(reasons, dict) else reasons if isinstance(reasons, list) else []
    if any(ILLNESS_REASON_RE.search(str(reason).lower()) for reason in values):
        return True
    return any(
        detect_red_flags(checkin.get(field))
        for field in ("pain_or_niggles", "unexpected_fatigue", "athlete_notes")
    )


def health_affected_weeks(
    history: list[dict[str, Any]],
    *,
    health_events: list[dict[str, Any]] | None = None,
    checkins_by_run: dict[str, dict[str, Any]] | None = None,
    current_checkin: dict[str, Any] | None = None,
    weekly_actuals: list[dict[str, Any]] | None = None,
) -> set[str]:
    """Planned weeks (by planned_week_start) disturbed by illness or injury.

    A week counts as health-affected when a SICK/INJURED Intervals note
    overlaps it, when the check-in that planned it reported illness, when the
    check-in made after it (the next run's, or the current one for the latest
    week) reports illness or cites illness/injury for missed sessions, or when
    long-term memory flagged the week as an illness week.
    """
    affected: set[str] = set()
    checkins = checkins_by_run or {}
    dated = sorted(
        (item for item in history if item.get("planned_week_start")),
        key=lambda item: str(item.get("planned_week_start")),
    )
    illness_weeks = {
        str(item.get("week_start"))
        for item in weekly_actuals or []
        if item.get("illness")
    }
    for position, item in enumerate(dated):
        start = _safe_day(item.get("planned_week_start"))
        if start is None:
            continue
        monday = start - timedelta(days=start.weekday())
        end = monday + timedelta(days=6)
        key = str(item.get("planned_week_start"))
        for event in health_events or []:
            event_start = _safe_day(event.get("start_date"))
            event_end = _safe_day(event.get("end_date")) or event_start
            if event_start and event_end and event_start <= end and event_end >= monday:
                affected.add(key)
        if _checkin_flags_illness(checkins.get(str(item.get("source_run")))):
            affected.add(key)
        later = (
            checkins.get(str(dated[position + 1].get("source_run")))
            if position + 1 < len(dated)
            else current_checkin
        )
        if _checkin_flags_illness(later):
            affected.add(key)
        if monday.isoformat() in illness_weeks:
            affected.add(key)
    return affected


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 2)


def _top_labels(counter: Counter[str], *, minimum: int = 1) -> list[str]:
    return [label for label, count in counter.most_common() if count >= minimum]


def _routine_status(rate: float) -> str:
    if rate >= 0.8:
        return "confirmed"
    if rate >= 0.55:
        return "mixed"
    return "fragile"


def build_response_profile(
    history: list[dict[str, Any]],
    weekly_checkin: dict[str, Any] | None = None,
    *,
    excluded_weeks: set[str] | None = None,
) -> dict[str, Any]:
    """How the athlete actually responds to the plan, from prior plan reviews.

    - key sessions: completed as planned vs easier than planned vs missed
    - RPE vs intended effort (RPE 2+ above intended = harder than intended)
    - Intervals Feel (1 = strong/best, 5 = weak/worst) on key sessions
    - typical weekly load absorbed in weeks that were planned green
    - body parts mentioned as niggles in two or more check-ins
    Weeks in `excluded_weeks` (illness / injury) do not count toward the
    execution statistics; their niggles still count.
    """
    excluded = excluded_weeks or set()
    healthy_history = [
        item for item in history if str(item.get("planned_week_start")) not in excluded
    ]
    all_reviews = [
        review
        for item in healthy_history
        if item.get("status") == "available"
        for review in item.get("session_reviews", [])
        if review.get("status") != "missed"
    ]
    rpe_reads = [(review, rpe_compliance(review)) for review in all_reviews]
    rpe_rated = [(review, verdict) for review, verdict in rpe_reads if verdict]
    harder_than_intended = [
        review.get("session_name") for review, verdict in rpe_rated if verdict == "harder_than_intended"
    ]
    feel_values = []
    for review in all_reviews:
        try:
            feel_values.append((review, int(round(float(review.get("feel"))))))
        except (TypeError, ValueError):
            continue
    weak_feel = [review.get("session_name") for review, feel in feel_values if feel >= FEEL_WEAK]
    strong_feel = [review.get("session_name") for review, feel in feel_values if feel <= FEEL_STRONG]
    key_reviews = [
        review
        for item in healthy_history
        if item.get("status") == "available"
        for review in item.get("session_reviews", [])
        if review.get("priority") == "key"
    ]
    as_planned = sum(
        1
        for review in key_reviews
        if review.get("status") != "missed"
        and review.get("intensity_compliance") == "as_planned"
    )
    easier = sum(
        1 for review in key_reviews if review.get("intensity_compliance") == "easier_than_planned"
    )
    harder = sum(
        1
        for review in key_reviews
        if review.get("intensity_compliance") == "harder_than_planned"
        or rpe_compliance(review) == "harder_than_intended"
    )
    missed = sum(1 for review in key_reviews if review.get("status") == "missed")

    green_loads = [
        float(item["week_load_actual"])
        for item in healthy_history
        if item.get("status") == "available"
        and str(item.get("readiness_status") or "").lower() == "green"
        and item.get("week_load_actual")
    ]

    niggle_texts = [
        str(item.get("pain_or_niggles") or "")
        for item in history
        if has_niggle(item.get("pain_or_niggles"))
    ]
    if weekly_checkin and has_niggle(weekly_checkin.get("pain_or_niggles")):
        niggle_texts.append(str(weekly_checkin.get("pain_or_niggles")))
    part_counter: Counter[str] = Counter()
    for text in niggle_texts:
        part_counter.update(niggle_body_parts(text))
    repeated = [
        part for part, count in part_counter.most_common() if count >= REPEATED_NIGGLE_MIN_WEEKS
    ]

    flags: list[str] = []
    if key_reviews and _rate(easier, len(key_reviews)) >= 0.4:
        flags.append(
            "Key sessions often land easier than planned: check targets are realistic before raising them."
        )
    if key_reviews and _rate(harder, len(key_reviews)) >= 0.4:
        flags.append(
            "Key sessions often run harder than planned: reinforce pacing caps."
        )
    if repeated:
        flags.append(f"Recurring niggle area(s): {', '.join(repeated)}.")
    if rpe_rated and _rate(len(harder_than_intended), len(rpe_rated)) >= 0.3:
        flags.append(
            "Sessions often feel harder than intended (RPE 2+ points above the planned effort): "
            "targets may be too ambitious or recovery is lagging."
        )
    if len(feel_values) >= 3 and _rate(len(weak_feel), len(feel_values)) >= 0.4:
        flags.append(
            "Many recent sessions felt weak (Intervals Feel 4-5, where 1 = strong): hold load before progressing."
        )
    return {
        "rpe_sessions_rated": len(rpe_rated),
        "rpe_harder_than_intended": harder_than_intended,
        "feel_sessions_rated": len(feel_values),
        "feel_strong_sessions": strong_feel,
        "feel_weak_sessions": weak_feel,
        "excluded_health_weeks": sorted(excluded),
        "key_sessions_reviewed": len(key_reviews),
        "key_completed_as_planned": as_planned,
        "key_completed_easier": easier,
        "key_completed_harder": harder,
        "key_missed": missed,
        "key_as_planned_rate": _rate(as_planned, len(key_reviews)) if key_reviews else None,
        "green_weeks_observed": len(green_loads),
        "typical_green_week_load": round(median(green_loads), 1) if green_loads else None,
        "max_green_week_load": round(max(green_loads), 1) if green_loads else None,
        "niggle_mentions": dict(part_counter),
        "repeated_niggles": repeated,
        "flags": flags,
    }


def build_athlete_memory(
    history: list[dict[str, Any]],
    planning_constraints: dict[str, Any],
    athlete_profile: dict[str, Any],
    weekly_checkin: dict[str, Any],
    *,
    health_events: list[dict[str, Any]] | None = None,
    checkins_by_run: dict[str, dict[str, Any]] | None = None,
    weekly_actuals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execution reliability from prior weeks, excluding illness / injury weeks.

    A week disrupted by a SICK/INJURED note, an illness check-in or missed
    sessions blamed on illness says nothing about routine reliability, so it
    is left out of the day / session-type / adherence statistics.
    """
    excluded = health_affected_weeks(
        history,
        health_events=health_events,
        checkins_by_run=checkins_by_run,
        current_checkin=weekly_checkin,
        weekly_actuals=weekly_actuals,
    )
    response_profile = build_response_profile(
        history, weekly_checkin, excluded_weeks=excluded
    )

    available_history = [
        item
        for item in history
        if item.get("status") == "available"
        and str(item.get("planned_week_start")) not in excluded
    ]
    if not available_history:
        return {
            "response_profile": response_profile,
            "excluded_health_weeks": sorted(excluded),
            "status": "limited",
            "weeks_reviewed": 0,
            "average_adherence_score": None,
            "reliable_days": [],
            "fragile_days": [],
            "reliable_session_types": [],
            "fragile_session_types": [],
            "double_day_profile": {
                "status": "unknown",
                "planned_double_days": 0,
                "completion_rate": None,
            },
            "routine_confidence": {},
            "recurring_issues": [],
            "coach_flags": [],
            "summary": "No multi-week execution history is available yet, so the planner should lean more on the current check-in and explicit constraints.",
        }

    day_stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"planned": 0, "completed_any": 0, "completed_on_day": 0}
    )
    session_type_stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"planned": 0, "completed_any": 0}
    )
    missed_counter: Counter[str] = Counter()
    shortened_counter: Counter[str] = Counter()
    moved_counter: Counter[str] = Counter()
    adherence_scores: list[float] = []
    total_double_days = 0
    completed_double_days = 0

    for item in available_history:
        adherence_scores.append(float(item.get("adherence_score", 0.0) or 0.0))
        for day, stats in item.get("day_breakdown", {}).items():
            day_stats[day]["planned"] += int(stats.get("planned", 0) or 0)
            day_stats[day]["completed_any"] += int(stats.get("completed_any", 0) or 0)
            day_stats[day]["completed_on_day"] += int(
                stats.get("completed_on_day", 0) or 0
            )
        for label, stats in item.get("session_type_breakdown", {}).items():
            session_type_stats[label]["planned"] += int(stats.get("planned", 0) or 0)
            session_type_stats[label]["completed_any"] += int(
                stats.get("completed_any", 0) or 0
            )
        missed_counter.update(item.get("missed_sessions", []))
        shortened_counter.update(item.get("shortened_sessions", []))
        moved_counter.update(item.get("moved_sessions", []))
        total_double_days += int(item.get("double_days", {}).get("planned", 0) or 0)
        completed_double_days += int(
            item.get("double_days", {}).get("fully_completed", 0) or 0
        )

    reliable_days: list[str] = []
    fragile_days: list[str] = []
    for day, stats in day_stats.items():
        if stats["planned"] < 2:
            continue
        completion_rate = _rate(stats["completed_any"], stats["planned"])
        if completion_rate >= 0.8:
            reliable_days.append(day)
        elif completion_rate <= 0.55:
            fragile_days.append(day)

    reliable_session_types: list[str] = []
    fragile_session_types: list[str] = []
    for label, stats in session_type_stats.items():
        if stats["planned"] < 2:
            continue
        completion_rate = _rate(stats["completed_any"], stats["planned"])
        if completion_rate >= 0.8:
            reliable_session_types.append(label)
        elif completion_rate <= 0.55:
            fragile_session_types.append(label)

    preferred_long_ride_day = planning_constraints.get("preferred_long_ride_day")
    preferred_long_run_day = planning_constraints.get("preferred_long_run_day")
    fixed_swim_days = planning_constraints.get("fixed_swim_days", [])

    long_ride_stats = session_type_stats.get(
        "long_ride", {"planned": 0, "completed_any": 0}
    )
    long_run_stats = session_type_stats.get(
        "long_run", {"planned": 0, "completed_any": 0}
    )
    swim_stats = session_type_stats.get(
        "swim_routine", {"planned": 0, "completed_any": 0}
    )

    routine_confidence = {
        "long_ride_day": {
            "preferred": preferred_long_ride_day,
            "status": _routine_status(
                _rate(long_ride_stats["completed_any"], long_ride_stats["planned"])
            )
            if long_ride_stats["planned"]
            else "unknown",
        },
        "long_run_day": {
            "preferred": preferred_long_run_day,
            "status": _routine_status(
                _rate(long_run_stats["completed_any"], long_run_stats["planned"])
            )
            if long_run_stats["planned"]
            else "unknown",
        },
        "fixed_swim_days": {
            "days": fixed_swim_days,
            "status": _routine_status(
                _rate(swim_stats["completed_any"], swim_stats["planned"])
            )
            if swim_stats["planned"]
            else "unknown",
        },
    }

    double_day_rate = _rate(completed_double_days, total_double_days)
    double_day_profile = {
        "status": (
            "comfortable"
            if total_double_days and double_day_rate >= 0.75
            else "fragile"
            if total_double_days and double_day_rate <= 0.5
            else "mixed"
            if total_double_days
            else "unknown"
        ),
        "planned_double_days": total_double_days,
        "completion_rate": double_day_rate if total_double_days else None,
    }

    recurring_issues: list[str] = []
    if fragile_days:
        recurring_issues.append(
            f"{', '.join(sorted(fragile_days))} has been less reliable in recent history."
        )
    if fragile_session_types:
        recurring_issues.append(
            f"The least reliable session types are {', '.join(sorted(fragile_session_types))}."
        )
    if missed_counter:
        recurring_issues.append(
            f"Most commonly missed sessions: {', '.join(_top_labels(missed_counter, minimum=1)[:3])}."
        )

    coach_flags: list[str] = []
    if reliable_days:
        coach_flags.append(
            f"Reliable execution days are {', '.join(sorted(reliable_days))}."
        )
    if reliable_session_types:
        coach_flags.append(
            f"Most repeatable session types are {', '.join(sorted(reliable_session_types))}."
        )
    if double_day_profile["status"] == "comfortable":
        coach_flags.append(
            "Recent history suggests double days are usually manageable."
        )
    elif double_day_profile["status"] == "fragile":
        coach_flags.append(
            "Recent history suggests double days are where the plan starts to unravel."
        )
    if athlete_profile.get("coaching_tone_preference"):
        coach_flags.append(
            f"Preferred coaching tone: {athlete_profile['coaching_tone_preference']}."
        )

    average_adherence_score = round(sum(adherence_scores) / len(adherence_scores), 2)
    summary_parts = [
        f"Reviewed {len(available_history)} prior week(s) with average adherence {average_adherence_score}."
    ]
    if reliable_days:
        summary_parts.append(f"Reliable days: {', '.join(sorted(reliable_days))}.")
    if fragile_days:
        summary_parts.append(f"Fragile days: {', '.join(sorted(fragile_days))}.")
    if double_day_profile["status"] != "unknown":
        summary_parts.append(
            f"Double-day handling looks {double_day_profile['status']} ({double_day_profile['completion_rate']})."
        )

    coach_flags.extend(response_profile["flags"])
    if response_profile["typical_green_week_load"]:
        summary_parts.append(
            f"Typical load absorbed in green weeks: {response_profile['typical_green_week_load']:g}."
        )

    if excluded:
        summary_parts.append(
            f"Excluded {len(excluded)} illness/injury week(s) from the reliability stats."
        )
    return {
        "response_profile": response_profile,
        "excluded_health_weeks": sorted(excluded),
        "status": "available",
        "weeks_reviewed": len(available_history),
        "average_adherence_score": average_adherence_score,
        "reliable_days": sorted(reliable_days),
        "fragile_days": sorted(fragile_days),
        "reliable_session_types": sorted(reliable_session_types),
        "fragile_session_types": sorted(fragile_session_types),
        "double_day_profile": double_day_profile,
        "routine_confidence": routine_confidence,
        "common_missed_sessions": _top_labels(missed_counter, minimum=1)[:3],
        "common_shortened_sessions": _top_labels(shortened_counter, minimum=1)[:3],
        "common_moved_sessions": _top_labels(moved_counter, minimum=1)[:3],
        "recurring_issues": recurring_issues,
        "coach_flags": coach_flags,
        "summary": " ".join(summary_parts),
    }
