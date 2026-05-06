from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..session_library import build_session, reduction_priority
from ..utils import day_name, daterange, parse_date


DAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

RIDE_KINDS = {"threshold_ride", "endurance_ride", "support_ride", "long_ride"}
RUN_KINDS = {"easy_run", "quality_run", "long_run"}


def _club_swim_preferences(snapshot: dict[str, Any]) -> tuple[int, int | None]:
    swim_profile = snapshot.get("athlete_profile", {}).get("swim_profile", {})
    duration = swim_profile.get("club_session_duration_min", 40)
    distance = swim_profile.get("club_session_distance_m")
    try:
        duration = max(40, int(duration))
    except (TypeError, ValueError):
        duration = 40
    try:
        distance = int(distance) if distance not in (None, "") else None
    except (TypeError, ValueError):
        distance = None
    return duration, distance


def _friday_support_ride_requested(snapshot: dict[str, Any]) -> bool:
    constraints = snapshot.get("planning_constraints", {})
    weekly = snapshot.get("weekly_checkin", {})
    texts = [
        *constraints.get("raw_time_constraints", []),
        str(weekly.get("athlete_notes", "")),
    ]
    for text in texts:
        lowered = text.lower()
        if "friday" not in lowered:
            continue
        if "support ride" in lowered:
            return True
        if "not be fully off" in lowered or "rather than being fully off" in lowered:
            return True
    return False


def _ordered_days(*groups: list[str]) -> list[str]:
    seen: list[str] = []
    for group in groups:
        for day in group:
            if day in DAY_NAMES and day not in seen:
                seen.append(day)
    return seen


def _sort_days_by_memory(
    candidate_days: list[str], athlete_memory: dict[str, Any]
) -> list[str]:
    reliable_days = set(athlete_memory.get("reliable_days", []))
    fragile_days = set(athlete_memory.get("fragile_days", []))
    return sorted(
        candidate_days,
        key=lambda day: (
            2 if day in fragile_days else 0,
            0 if day in reliable_days else 1,
            DAY_NAMES.index(day),
        ),
    )


def _day_map(snapshot: dict[str, Any]) -> dict[str, Any]:
    week_days = daterange(parse_date(snapshot["week_start"]), 7)
    return {day_name(item): item for item in week_days}


def _can_double(constraints: dict[str, Any], day: str) -> bool:
    explicit = constraints.get("double_session_capacity_by_day", {}).get(day)
    if explicit is not None:
        return bool(explicit)
    return day in constraints.get("preferred_double_days", [])


def _cap_for_day(constraints: dict[str, Any], day: str) -> int | None:
    cap = constraints.get("time_caps_by_day", {}).get(day)
    if cap in (None, ""):
        return None
    return int(cap)


def _round_to_5(value: float) -> int:
    return int(round(value / 5.0) * 5)


def _recommended_weekly_budget(
    snapshot: dict[str, Any],
    performance: dict[str, Any],
    macro: dict[str, Any],
    race: dict[str, Any],
) -> dict[str, Any]:
    constraints = snapshot.get("planning_constraints", {})
    policy = constraints.get("weekly_time_budget_policy", {})
    reference_min = int(policy.get("reference_min") or 480)
    days_to_race = int((race.get("priority_race") or {}).get("days_to_race") or 999)
    phase = str(macro.get("macro_phase", "build"))
    classification = str(macro.get("week_classification", "build"))
    readiness = str(performance.get("readiness_status", "green"))

    multiplier = 0.78 if days_to_race > 84 else 0.9
    if phase == "race_specific":
        multiplier = 1.0
    elif phase == "taper":
        multiplier = 0.72
    if classification == "absorb":
        multiplier *= 0.9
    if classification == "recovery":
        multiplier *= 0.8
    if readiness == "yellow":
        multiplier *= 0.93
    elif readiness == "red":
        multiplier *= 0.82

    target_min = _round_to_5(max(240, reference_min * multiplier))
    minimum_effective_min = _round_to_5(max(210, target_min - 45))
    maximum_effective_min = _round_to_5(
        max(target_min, min(reference_min, target_min + 45))
    )
    if policy.get("mode") == "hard" and policy.get("requested_min"):
        hard_cap = int(policy["requested_min"])
        target_min = min(target_min, hard_cap)
        minimum_effective_min = min(minimum_effective_min, target_min)
        maximum_effective_min = hard_cap
    else:
        hard_cap = None

    if days_to_race <= 21 and classification not in {"recovery", "taper"}:
        maximum_effective_min = max(maximum_effective_min, target_min + 30)

    rationale = (
        "Far from the priority race, the plan stays ROI-focused and trims non-essential volume."
        if days_to_race > 84
        else (
            "Closer to the priority race, the plan is allowed to use more time to sharpen race-specific durability."
            if days_to_race <= 49
            else "The week keeps enough volume to progress without spending time where the return is low."
        )
    )
    return {
        "mode": policy.get("mode", "flexible"),
        "reference_min": reference_min,
        "target_min": target_min,
        "minimum_effective_min": minimum_effective_min,
        "maximum_effective_min": maximum_effective_min,
        "hard_cap_min": hard_cap,
        "days_to_race": days_to_race,
        "rationale": rationale,
    }


def _adjust_spec_duration(
    item: dict[str, Any],
    delta: int,
    *,
    constraints: dict[str, Any],
    increase: bool,
) -> bool:
    day_cap = _cap_for_day(constraints, item["day"])
    if not increase:
        if item["duration"] <= item["min_duration"]:
            return False
        item["duration"] -= min(delta, item["duration"] - item["min_duration"])
        return True
    if day_cap is not None:
        return False
    item["duration"] += delta
    return True


def _align_weekly_budget(
    specs: list[dict[str, Any]],
    constraints: dict[str, Any],
    weekly_budget: dict[str, Any],
) -> None:
    total_minutes = lambda: sum(int(item["duration"]) for item in specs)

    ceiling = weekly_budget.get("hard_cap_min") or weekly_budget.get(
        "maximum_effective_min"
    )
    while ceiling is not None and total_minutes() > int(ceiling):
        reduced = False
        for item in sorted(
            specs,
            key=lambda value: (reduction_priority(value["kind"]), -value["duration"]),
        ):
            if _adjust_spec_duration(item, 5, constraints=constraints, increase=False):
                reduced = True
                break
        if not reduced:
            break

    target = weekly_budget.get("target_min")
    while target is not None and total_minutes() < int(target):
        expanded = False
        for item in sorted(
            specs,
            key=lambda value: (
                reduction_priority(value["kind"]),
                0
                if value["kind"]
                in {"long_ride", "long_run", "threshold_ride", "quality_run"}
                else 1,
                value["duration"],
            ),
        ):
            if _adjust_spec_duration(item, 5, constraints=constraints, increase=True):
                expanded = True
                break
        if not expanded:
            break


def _pick_day(
    candidate_days: list[str],
    *,
    constraints: dict[str, Any],
    blocked_days: set[str],
    minimum_minutes: int,
    disallow_bike: bool = False,
    fixed_swim_combo: bool = False,
) -> str | None:
    fixed_swim_days = set(constraints.get("fixed_swim_days", []))
    no_bike_days = set(constraints.get("no_bike_days", []))
    days_off = set(constraints.get("days_off", []))
    strength_only_days = set(constraints.get("strength_only_days", []))
    for day in candidate_days:
        if day in blocked_days or day in days_off:
            continue
        if disallow_bike and day in no_bike_days:
            continue
        if day in strength_only_days:
            continue
        cap = _cap_for_day(constraints, day)
        if cap is not None and cap < minimum_minutes:
            continue
        if (
            day in fixed_swim_days
            and fixed_swim_combo
            and not _can_double(constraints, day)
        ):
            continue
        return day
    return None


def _add_spec(
    specs: list[dict[str, Any]],
    *,
    kind: str,
    day: str,
    duration: int,
    min_duration: int,
) -> None:
    specs.append(
        {
            "kind": kind,
            "day": day,
            "duration": duration,
            "min_duration": min_duration,
        }
    )


def _fit_caps(specs: list[dict[str, Any]], constraints: dict[str, Any]) -> None:
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for spec in specs:
        by_day[spec["day"]].append(spec)

    for day, items in by_day.items():
        cap = _cap_for_day(constraints, day)
        if cap is None:
            continue
        while sum(item["duration"] for item in items) > cap:
            excess = sum(item["duration"] for item in items) - cap
            reduced = False
            for item in sorted(
                items,
                key=lambda value: (
                    reduction_priority(value["kind"]),
                    -value["duration"],
                ),
            ):
                if item["duration"] <= item["min_duration"]:
                    continue
                cut = min(5, excess, item["duration"] - item["min_duration"])
                item["duration"] -= cut
                reduced = True
                break
            if not reduced:
                break

    weekly_cap = constraints.get("available_hours_override_min")
    if weekly_cap in (None, ""):
        return
    weekly_cap = int(weekly_cap)
    while sum(item["duration"] for item in specs) > weekly_cap:
        excess = sum(item["duration"] for item in specs) - weekly_cap
        reduced = False
        for item in sorted(
            specs,
            key=lambda value: (reduction_priority(value["kind"]), -value["duration"]),
        ):
            if item["duration"] <= item["min_duration"]:
                continue
            cut = min(5, excess, item["duration"] - item["min_duration"])
            item["duration"] -= cut
            reduced = True
            break
        if not reduced:
            break


def _planned_count(specs: list[dict[str, Any]], kinds: set[str]) -> int:
    return sum(1 for spec in specs if spec["kind"] in kinds)


def _target_frequency(
    snapshot: dict[str, Any], performance: dict[str, Any], macro: dict[str, Any]
) -> dict[str, int]:
    profile_frequency = snapshot.get("athlete_profile", {}).get(
        "weekly_sport_frequency", {}
    )
    recovery_week = macro.get("week_classification") == "recovery"
    if recovery_week:
        return {"rides": 2, "runs": 2}

    readiness_green = performance.get("readiness_status") == "green"
    ride_cap = 3 if readiness_green else 2
    run_cap = 3 if readiness_green else 3

    try:
        ride_target = int(profile_frequency.get("bike_sessions", 2) or 2)
    except (TypeError, ValueError):
        ride_target = 2
    try:
        run_target = int(profile_frequency.get("run_sessions", 3) or 3)
    except (TypeError, ValueError):
        run_target = 3

    return {
        "rides": max(2, min(ride_target, ride_cap)),
        "runs": max(2, min(run_target, run_cap)),
    }


def _pick_extra_day(
    candidate_days: list[str],
    *,
    specs: list[dict[str, Any]],
    constraints: dict[str, Any],
    minimum_minutes: int,
    ride_session: bool,
    allowed_pair_kinds: set[str],
) -> str | None:
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for spec in specs:
        by_day[spec["day"]].append(spec)

    no_bike_days = set(constraints.get("no_bike_days", []))
    days_off = set(constraints.get("days_off", []))
    strength_only_days = set(constraints.get("strength_only_days", []))
    explicit_double_capacity = constraints.get("double_session_capacity_by_day", {})

    for day in candidate_days:
        if day in days_off or day in strength_only_days:
            continue
        if ride_session and day in no_bike_days:
            continue
        day_specs = by_day.get(day, [])
        cap = _cap_for_day(constraints, day)
        existing_minutes = sum(int(item["duration"]) for item in day_specs)
        if cap is not None and existing_minutes + minimum_minutes > cap:
            continue
        if not day_specs:
            return day
        if len(day_specs) >= 2:
            continue
        if explicit_double_capacity.get(day) is False:
            continue
        if {item["kind"] for item in day_specs} <= allowed_pair_kinds:
            return day
    return None


def _build_specs(
    snapshot: dict[str, Any],
    performance: dict[str, Any],
    macro: dict[str, Any],
    race: dict[str, Any],
) -> list[dict[str, Any]]:
    constraints = snapshot.get("planning_constraints", {})
    athlete_memory = snapshot.get("athlete_memory", {})
    club_swim_duration, club_swim_distance = _club_swim_preferences(snapshot)
    friday_support_ride_requested = _friday_support_ride_requested(snapshot)
    target_frequency = _target_frequency(snapshot, performance, macro)
    weekly_budget = _recommended_weekly_budget(snapshot, performance, macro, race)
    recovery_week = macro.get("week_classification") == "recovery"
    lighter_week = macro.get("week_classification") in {"recovery", "absorb"}
    if constraints.get("requested_week_type") == "recovery":
        recovery_week = True
        lighter_week = True

    adherence = snapshot.get("prior_plan_adherence", {})
    if (
        adherence.get("status") == "available"
        and adherence.get("adherence_score", 1.0) < 0.65
    ):
        lighter_week = True

    day_lookup = _day_map(snapshot)
    rest_day = (
        constraints.get("days_off", ["Monday"])[0]
        if constraints.get("days_off")
        else "Monday"
    )
    if rest_day not in day_lookup:
        rest_day = "Monday"
    long_ride_day = constraints.get("preferred_long_ride_day") or "Saturday"
    if long_ride_day not in day_lookup:
        long_ride_day = "Saturday"
    long_run_day = constraints.get("preferred_long_run_day") or "Sunday"
    if long_run_day not in day_lookup:
        long_run_day = "Sunday"
    strength_day = (
        constraints.get("strength_days", ["Friday"])[0]
        if constraints.get("strength_days")
        else "Friday"
    )
    if strength_day not in day_lookup:
        strength_day = "Friday"

    fixed_swim_days = [
        day for day in constraints.get("fixed_swim_days", []) if day in day_lookup
    ]
    blocked = {rest_day, long_ride_day, long_run_day}
    ride_candidates = _sort_days_by_memory(
        _ordered_days(
            constraints.get("preferred_ride_days", []),
            ["Wednesday", "Tuesday", "Friday", "Thursday"],
        ),
        athlete_memory,
    )
    key_ride_day = _pick_day(
        ride_candidates,
        constraints=constraints,
        blocked_days=blocked,
        minimum_minutes=50 if recovery_week else 60,
        disallow_bike=True,
        fixed_swim_combo=True,
    )
    if key_ride_day is None:
        key_ride_day = long_ride_day

    run_candidates = _sort_days_by_memory(
        _ordered_days(
            constraints.get("preferred_run_days", []),
            ["Thursday", "Tuesday", "Wednesday", "Friday"],
        ),
        athlete_memory,
    )
    quality_run_day = None
    if not recovery_week:
        quality_run_day = _pick_day(
            run_candidates,
            constraints=constraints,
            blocked_days={*blocked, key_ride_day},
            minimum_minutes=50,
            fixed_swim_combo=True,
        )

    easy_run_candidates = _sort_days_by_memory(
        _ordered_days(["Tuesday", "Wednesday", "Friday", "Thursday"]),
        athlete_memory,
    )
    easy_run_day = _pick_day(
        easy_run_candidates,
        constraints=constraints,
        blocked_days={
            *blocked,
            key_ride_day,
            *([quality_run_day] if quality_run_day else []),
        },
        minimum_minutes=35,
        fixed_swim_combo=True,
    )
    secondary_easy_run_day = None
    if recovery_week:
        secondary_easy_run_day = _pick_day(
            ["Thursday", "Tuesday", "Friday", "Wednesday"],
            constraints=constraints,
            blocked_days={
                *blocked,
                key_ride_day,
                *([easy_run_day] if easy_run_day else []),
            },
            minimum_minutes=35,
            fixed_swim_combo=True,
        )

    specs: list[dict[str, Any]] = []
    _add_spec(specs, kind="recovery", day=rest_day, duration=20, min_duration=15)
    if key_ride_day == long_ride_day:
        _add_spec(
            specs,
            kind="long_ride",
            day=long_ride_day,
            duration=150 if recovery_week else (135 if lighter_week else 165),
            min_duration=120,
        )
    else:
        _add_spec(
            specs,
            kind="endurance_ride" if recovery_week else "threshold_ride",
            day=key_ride_day,
            duration=75,
            min_duration=55 if recovery_week else 60,
        )
        _add_spec(
            specs,
            kind="long_ride",
            day=long_ride_day,
            duration=150 if recovery_week else (135 if lighter_week else 165),
            min_duration=120,
        )

    if easy_run_day:
        _add_spec(
            specs,
            kind="easy_run",
            day=easy_run_day,
            duration=40 if lighter_week else 45,
            min_duration=30,
        )
    if secondary_easy_run_day:
        _add_spec(
            specs,
            kind="easy_run",
            day=secondary_easy_run_day,
            duration=40,
            min_duration=30,
        )
    if quality_run_day:
        _add_spec(
            specs,
            kind="quality_run",
            day=quality_run_day,
            duration=55 if lighter_week else 65,
            min_duration=45,
        )
    elif not recovery_week:
        fallback_run_day = _pick_day(
            ["Thursday", "Friday", "Wednesday"],
            constraints=constraints,
            blocked_days={*blocked, key_ride_day},
            minimum_minutes=45,
            fixed_swim_combo=True,
        )
        if fallback_run_day:
            _add_spec(
                specs,
                kind="easy_run",
                day=fallback_run_day,
                duration=40,
                min_duration=30,
            )

    support_ride_day = None
    rides_planned = _planned_count(specs, RIDE_KINDS)
    support_ride_needed = not recovery_week and (
        friday_support_ride_requested or rides_planned < target_frequency["rides"]
    )
    if support_ride_needed:
        support_ride_day = _pick_extra_day(
            _ordered_days(
                ["Friday", "Wednesday", "Tuesday"],
                constraints.get("preferred_ride_days", []),
            ),
            specs=specs,
            constraints=constraints,
            minimum_minutes=55 if lighter_week else 60,
            ride_session=True,
            allowed_pair_kinds={"easy_run"},
        )
        if support_ride_day:
            _add_spec(
                specs,
                kind="support_ride",
                day=support_ride_day,
                duration=55 if lighter_week else 60,
                min_duration=45,
            )

    _add_spec(
        specs,
        kind="long_run",
        day=long_run_day,
        duration=70 if recovery_week else (70 if lighter_week else 80),
        min_duration=60,
    )

    runs_planned = _planned_count(specs, RUN_KINDS)
    if not recovery_week and runs_planned < target_frequency["runs"]:
        extra_run_day = _pick_extra_day(
            _ordered_days(
                ["Friday", "Thursday", "Tuesday"],
                constraints.get("preferred_run_days", []),
            ),
            specs=specs,
            constraints=constraints,
            minimum_minutes=35,
            ride_session=False,
            allowed_pair_kinds={"support_ride"},
        )
        if extra_run_day:
            _add_spec(
                specs,
                kind="easy_run",
                day=extra_run_day,
                duration=35 if lighter_week else 40,
                min_duration=30,
            )

    if strength_day in day_lookup and strength_day not in {
        rest_day,
        long_ride_day,
        long_run_day,
    }:
        _add_spec(
            specs,
            kind="strength",
            day=strength_day,
            duration=30 if lighter_week else 35,
            min_duration=20,
        )

    for swim_day in fixed_swim_days:
        swim_kind = "swim_club" if swim_day == "Tuesday" else "swim_pool"
        if swim_day == key_ride_day and not _can_double(constraints, swim_day):
            continue
        if swim_day == quality_run_day and not _can_double(constraints, swim_day):
            continue
        if swim_day == easy_run_day and not _can_double(constraints, swim_day):
            continue
        _add_spec(
            specs,
            kind=swim_kind,
            day=swim_day,
            duration=club_swim_duration if swim_kind == "swim_club" else 30,
            min_duration=club_swim_duration if swim_kind == "swim_club" else 25,
        )

    _fit_caps(specs, constraints)
    _align_weekly_budget(specs, constraints, weekly_budget)
    for spec in specs:
        if spec["kind"] == "swim_club" and club_swim_distance is not None:
            spec["club_session_distance_m"] = club_swim_distance
    return specs


def _spec_to_session(
    spec: dict[str, Any],
    day_lookup: dict[str, Any],
    lighter_week: bool,
    recovery_week: bool,
    session_context: dict[str, Any],
) -> dict[str, Any]:
    day = day_lookup[spec["day"]]
    return build_session(
        spec["kind"],
        day,
        int(spec["duration"]),
        lighter_week=lighter_week,
        recovery_week=recovery_week,
        club_session_distance_m=spec.get("club_session_distance_m"),
        session_context=session_context,
    )


def _build_session_context(
    snapshot: dict[str, Any],
    performance: dict[str, Any],
    macro: dict[str, Any],
    race: dict[str, Any],
) -> dict[str, Any]:
    athlete_profile = snapshot.get("athlete_profile", {})
    weekly_checkin = snapshot.get("weekly_checkin", {})
    return {
        "planning_date": snapshot.get("planning_date"),
        "macro_phase": macro.get("macro_phase"),
        "week_classification": macro.get("week_classification"),
        "friel_block_week": macro.get("friel_block_week"),
        "phase_focus": macro.get("phase_focus"),
        "readiness_status": performance.get("readiness_status"),
        "readiness_score": performance.get("readiness_score"),
        "main_risk": performance.get("main_risk"),
        "avg_sleep_hours": snapshot.get("avg_sleep_hours"),
        "priority_race": race.get("priority_race", {}),
        "race_specific_needs": race.get("race_specific_needs", []),
        "primary_performance_goal": athlete_profile.get("primary_performance_goal"),
        "secondary_goal": athlete_profile.get("secondary_goal"),
        "target_race_distance": athlete_profile.get("target_race_distance"),
        "pain_or_niggles": weekly_checkin.get("pain_or_niggles"),
        "recent_session_signatures_by_kind": snapshot.get(
            "recent_session_signatures_by_kind", {}
        ),
    }


def _stress_distribution(sessions: list[dict[str, Any]]) -> list[str]:
    by_day: dict[str, int] = defaultdict(int)
    for session in sessions:
        score = 0
        if session["discipline"] == "Recovery":
            score = 0
        elif session["discipline"] == "Swim":
            score = 1
        elif session["discipline"] == "WeightTraining":
            score = 2
        elif session.get("priority") == "key":
            score = 3
        else:
            score = 2
        by_day[session["day"]] += score
    labels = []
    for day in DAY_NAMES:
        total = by_day.get(day, 0)
        if total <= 1:
            labels.append("low")
        elif total <= 3:
            labels.append("medium")
        else:
            labels.append("high")
    return labels


def run(
    snapshot: dict[str, Any],
    performance: dict[str, Any],
    macro: dict[str, Any],
    race: dict[str, Any],
) -> dict[str, Any]:
    day_lookup = _day_map(snapshot)
    constraints = snapshot.get("planning_constraints", {})
    recovery_week = macro.get("week_classification") == "recovery"
    lighter_week = macro.get("week_classification") in {"recovery", "absorb"}
    if constraints.get("requested_week_type") == "recovery":
        recovery_week = True
        lighter_week = True

    weekly_budget = _recommended_weekly_budget(snapshot, performance, macro, race)
    specs = _build_specs(snapshot, performance, macro, race)
    session_context = _build_session_context(snapshot, performance, macro, race)
    sessions = [
        _spec_to_session(
            spec,
            day_lookup,
            lighter_week,
            recovery_week,
            session_context,
        )
        for spec in sorted(
            specs,
            key=lambda item: (
                DAY_NAMES.index(item["day"]),
                reduction_priority(item["kind"]),
            ),
        )
    ]

    key_sessions = [
        session["session_name"]
        for session in sessions
        if session.get("priority") == "key"
    ]
    adaptations = [
        "aerobic durability",
        "routine preservation",
        "session execution quality",
    ]
    if not recovery_week:
        adaptations.insert(0, "sustainable bike power")
        adaptations.insert(1, "run threshold discipline")

    fixed_swim_days = constraints.get("fixed_swim_days", [])
    strategic_theme = (
        "Use a true recovery week: keep swim rhythm and weekend structure, but strip out threshold density so freshness returns."
        if recovery_week
        else (
            "Absorb the recent work while preserving the key bike, one quality run, and the fixed swim routine."
            if lighter_week
            else "Build the week around one key bike, one quality run, fixed swim routine, and a durable weekend."
        )
    )
    alternatives = [
        "If fatigue spikes, drop Friday strength before touching the key sessions.",
        "If the week gets squeezed, protect the main bike, the main run, and the long ride before the extra support work.",
    ]
    if fixed_swim_days:
        alternatives.append(
            f"If life gets messy, keep the fixed swim days ({', '.join(fixed_swim_days)}) easy rather than skipping them aggressively."
        )

    return {
        "agent": "head_coach_planner",
        "strategic_theme": strategic_theme,
        "key_sessions": key_sessions,
        "weekly_time_budget": weekly_budget,
        "stress_distribution": _stress_distribution(sessions),
        "adaptations_targeted": adaptations,
        "alternatives_if_readiness_drops": alternatives,
        "sessions": sessions,
    }
