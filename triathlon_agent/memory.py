from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


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


def build_athlete_memory(
    history: list[dict[str, Any]],
    planning_constraints: dict[str, Any],
    athlete_profile: dict[str, Any],
    weekly_checkin: dict[str, Any],
) -> dict[str, Any]:
    del weekly_checkin

    available_history = [item for item in history if item.get("status") == "available"]
    if not available_history:
        return {
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

    return {
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
