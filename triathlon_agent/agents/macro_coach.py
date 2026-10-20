from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..coaching_limits import (
    DEFAULT_BLOCK_LENGTH,
    SHORT_BLOCK_LENGTH,
    is_health_reason,
)
from ..progression import FOCUS_BY_DISTANCE, focus_families
from ..race_selection import (
    calendar_races,
    last_completed_a_race,
    race_bucket,
    select_priority_race,
    taper_days,
    transition_days,
)
from ..threshold_testing import plan_threshold_tests
from ..utils import parse_date


LOAD_FOCUS_PHASES = {"base", "build", "race_specific", "taper"}


# (race_specific window, build window) in days before the priority race.
# Long events need a longer specific block; short events sharpen faster.
PHASE_WINDOWS_BY_BUCKET: dict[str, tuple[int, int]] = {
    "sprint": (28, 70),
    "5k": (28, 70),
    "10k": (35, 84),
    "olympic": (42, 98),
    "half_marathon": (42, 112),
    "trail": (49, 112),
    "70.3": (56, 140),
    "marathon": (56, 140),
    "ultra_trail": (63, 140),
    "ironman": (63, 168),
    "unknown": (49, 112),
}
# Holiday weeks: overlap (days) with the planned week that turns it into a holiday week.
HOLIDAY_MIN_OVERLAP_DAYS = 4


def phase_windows(race: dict[str, Any] | None) -> tuple[int, int, int]:
    """(taper, race_specific, build) day windows for a race."""
    race_specific, build = PHASE_WINDOWS_BY_BUCKET.get(
        race_bucket(race), PHASE_WINDOWS_BY_BUCKET["unknown"]
    )
    return taper_days(race), race_specific, build


def phase_for_race(
    race: dict[str, Any] | None, fallback_phase: str = "base"
) -> str:
    """Phase implied by the days to the priority race; no race in range -> base."""
    if not race:
        return fallback_phase if fallback_phase in {"base", "build"} else "base"
    days = int(race.get("days_to_race", 9999))
    taper, race_specific, build = phase_windows(race)
    if days <= taper:
        return "taper"
    if days <= race_specific:
        return "race_specific"
    if days <= build:
        return "build"
    # A persisted race_specific/taper phase never outlives its race window.
    return fallback_phase if fallback_phase in {"base", "build"} else "base"


def transition_race(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """The A race whose post-race transition covers the planned week, if any.

    B races get easy days scaled by duration from the planner instead of a
    transition phase.
    """
    race = next(
        (
            item
            for item in sorted(
                calendar_races(snapshot), key=lambda item: -int(item.get("days_to_race") or 0)
            )
            if item.get("priority") == "A"
            and item.get("days_to_race") is not None
            and -28 <= int(item["days_to_race"]) < 0
        ),
        None,
    ) or last_completed_a_race(snapshot)
    if not race:
        return None
    days_since = -int(race.get("days_to_race", 0))
    window = transition_days(race)
    if race.get("priority") != "A":
        # A B race is a checkpoint inside the block, not a season end: the
        # planner gives it a mini-taper and 2-4 easy days scaled by its duration.
        return None
    return race if days_since <= window else None


def _holiday_overlap(
    holiday: dict[str, Any], start: Any, end: Any
) -> int:
    try:
        holiday_start = parse_date(str(holiday.get("start_date")))
        holiday_end = parse_date(str(holiday.get("end_date") or holiday.get("start_date")))
    except (TypeError, ValueError):
        return 0
    first = max(holiday_start, start)
    last = min(holiday_end, end)
    return max(0, (last - first).days + 1)


def holiday_weeks(snapshot: dict[str, Any]) -> dict[str, dict[str, Any] | None]:
    """Holiday overlapping the planned week and the week after (>= 4 days)."""
    holidays = snapshot.get("holidays") or []
    result: dict[str, dict[str, Any] | None] = {"this_week": None, "next_week": None}
    for holiday in holidays:
        overlap = holiday.get("days_overlap_planning_week")
        if overlap is None and snapshot.get("week_start"):
            start = parse_date(snapshot["week_start"])
            overlap = _holiday_overlap(holiday, start, start + timedelta(days=6))
        if (overlap or 0) >= HOLIDAY_MIN_OVERLAP_DAYS:
            result["this_week"] = holiday
        if snapshot.get("week_start"):
            next_start = parse_date(snapshot["week_start"]) + timedelta(days=7)
            if (
                _holiday_overlap(holiday, next_start, next_start + timedelta(days=6))
                >= HOLIDAY_MIN_OVERLAP_DAYS
            ):
                result["next_week"] = holiday
    return result


def _fresh_block_state(
    active_cycle: dict[str, Any], periodization_state: dict[str, Any]
) -> tuple[int, str]:
    """Block week and cycle type, preferring what the completed weeks show.

    The persisted macro-cycle counter only fills in when the fresh weekly
    windows say nothing, or when a coach override was recorded; otherwise it
    would freeze the 3:1 rhythm on whatever was written last.
    """
    override = bool(active_cycle.get("override_rationale"))
    fresh_week = periodization_state.get("next_block_week")
    fresh_type = periodization_state.get("recommended_next_week_type")
    if fresh_week and not override:
        block_week = int(fresh_week)
    else:
        block_week = int(active_cycle.get("current_block_week") or fresh_week or 1)
    if fresh_type and not override:
        cycle_type = "recovery" if fresh_type == "recovery" else "load"
    else:
        cycle_type = str(active_cycle.get("cycle_type") or fresh_type or "load")
        if cycle_type not in {"load", "recovery"}:
            cycle_type = "load"
    return block_week, cycle_type


def _short_block_reasons(
    snapshot: dict[str, Any], performance: dict[str, Any]
) -> list[str]:
    """Markers that turn the 3:1 rhythm into 2:1 (two load weeks, then recovery)."""
    load = snapshot.get("training_load") or {}
    reasons: list[str] = []
    if (load.get("return_from_break") or {}).get("status") == "returning":
        reasons.append("returning from a break")
    overreaching = (load.get("overreaching") or {}).get("status")
    if overreaching in {"functional", "non_functional_risk"}:
        reasons.append(f"overreaching markers ({overreaching.replace('_', ' ')})")
    if (load.get("daily_signals") or {}).get("status") == "alert":
        reasons.append("daily HRV/resting-HR alert")
    for event in snapshot.get("recent_health_events") or []:
        try:
            days_since = int(event.get("days_since_end", 99))
        except (TypeError, ValueError):
            days_since = 99
        if event.get("kind") in {"sick", "illness", "ill"} and (event.get("ongoing") or days_since <= 28):
            reasons.append(f"recent illness ({event.get('name') or 'sick note'})")
            break
    if (performance.get("overreaching") or {}).get("status") in {"functional", "non_functional_risk"}:
        reasons.append("performance doctor flags overreaching")
    return list(dict.fromkeys(reasons))


def season_checkpoints(
    snapshot: dict[str, Any], priority_race: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Upcoming B/C races before the priority race: checkpoints of the season."""
    if not priority_race:
        return []
    limit = int(priority_race.get("days_to_race", 9999))
    checkpoints = []
    for race in calendar_races(snapshot):
        days = race.get("days_to_race")
        if days is None or not 0 <= int(days) < limit or race is priority_race:
            continue
        if race.get("priority") not in {"B", "C"}:
            continue
        checkpoints.append(
            {
                "name": race.get("name"),
                "date": race.get("date"),
                "priority": race.get("priority"),
                "days_to_race": int(days),
                "bucket": race_bucket(race),
                "role": (
                    "B-race checkpoint: mini-taper (3-4 lighter days, last quality 72h+ before), "
                    "then 2-4 easy days scaled by race duration"
                    if race.get("priority") == "B"
                    else "C-race: train through, nothing hard the day before, 1-2 easy days after"
                ),
            }
        )
    return sorted(checkpoints, key=lambda item: item["days_to_race"])


def _season_phase(
    phase: str, checkpoints: list[dict[str, Any]]
) -> tuple[str, str | None]:
    """A B race inside its own build window lifts a base phase to build."""
    if phase != "base":
        return phase, None
    for checkpoint in checkpoints:
        if checkpoint["priority"] != "B":
            continue
        b_phase = phase_for_race(
            {"days_to_race": checkpoint["days_to_race"], "distance": checkpoint["bucket"]}
        )
        if b_phase in {"build", "race_specific", "taper"}:
            return "build", (
                f"B-race {checkpoint['name']} in {checkpoint['days_to_race']} days is a season "
                "checkpoint inside its build window, so this week builds instead of base."
            )
    return phase, None


def _health_explained_misses(snapshot: dict[str, Any]) -> bool:
    """Missed sessions explained by illness/injury are not athlete unreliability."""
    reasons = (snapshot.get("weekly_checkin") or {}).get("missed_session_reasons") or {}
    if not isinstance(reasons, dict) or not reasons:
        return False
    return all(is_health_reason(reason) for reason in reasons.values())


def run(snapshot: dict[str, Any], performance: dict[str, Any]) -> dict[str, Any]:
    # Races, goals, phase and taper/transition timing come only from the
    # Intervals calendar (`snapshot["races"]`); the persisted macro cycle may
    # only contribute block-week history.
    priority_race = select_priority_race(snapshot)
    athlete_state = snapshot.get("athlete_state", {}) or {}
    active_cycle = athlete_state.get("active_macro_cycle", {}) or {}
    days_to_a = priority_race.get("days_to_race", 9999) if priority_race else 9999
    notes: list[str] = []
    phase = phase_for_race(priority_race, "base")
    if not priority_race:
        notes.append("No race on your Intervals calendar: general base week.")
    checkpoints = season_checkpoints(snapshot, priority_race)
    phase, season_note = _season_phase(phase, checkpoints)
    if season_note:
        notes.append(season_note)
    stored_phase = active_cycle.get("phase")
    stored_race = (active_cycle.get("priority_race") or {}).get("name")
    if stored_phase and stored_phase != phase and stored_phase != "transition":
        notes.append(
            f"Stored phase '{stored_phase}' disagrees with the Intervals calendar; the calendar wins ({phase})."
        )
    if stored_race and stored_race != (priority_race or {}).get("name"):
        notes.append(
            f"Stored priority race '{stored_race}' is not the calendar's priority race; the calendar wins."
        )
    completed_race = transition_race(snapshot)
    if completed_race and phase != "taper":
        phase = "transition"

    weekly_windows = snapshot.get("weekly_windows", [])
    periodization_state = snapshot.get("periodization_state", {}) or {}
    planning_constraints = snapshot.get("planning_constraints", {}) or {}
    prior_plan_adherence = snapshot.get("prior_plan_adherence", {}) or {}
    readiness = str(performance.get("readiness_status", "green"))
    return_from_break = (
        (snapshot.get("training_load") or {}).get("return_from_break") or {}
    )
    returning = return_from_break.get("status") == "returning"
    # Only completed calendar weeks count; the in-progress week is partial.
    complete_windows = [
        window
        for window in weekly_windows
        if not window.get("partial") and window.get("week_type") != "in_progress"
    ]
    recent_loads = [window["load"] for window in complete_windows[-3:]]
    trend_up = len(recent_loads) >= 2 and recent_loads[-1] >= recent_loads[0]
    week_classification = "build"
    load_direction = "rise"
    progression_type = "specificity"
    friel_block_week, friel_cycle_type = _fresh_block_state(
        active_cycle, periodization_state
    )
    phase_focus = "general aerobic and durability development"

    if phase == "build":
        phase_focus = "raise sustainable threshold and race-specific durability"
    elif phase == "race_specific":
        phase_focus = "increase race-specific execution and pacing precision"
    elif phase == "taper":
        phase_focus = "shed fatigue while preserving race sharpness"
    elif phase == "transition":
        phase_focus = "recover from the race: unstructured, easy, optional movement"

    # 3:1 by default; 2:1 after illness/a break or when overreaching markers say so.
    short_reasons = _short_block_reasons(snapshot, performance)
    block_length = SHORT_BLOCK_LENGTH if short_reasons else DEFAULT_BLOCK_LENGTH
    if (
        short_reasons
        and friel_cycle_type == "load"
        and int(friel_block_week or 1) >= SHORT_BLOCK_LENGTH
        and not active_cycle.get("override_rationale")
    ):
        friel_cycle_type = "recovery"
        notes.append(
            "2:1 rhythm this block (" + ", ".join(short_reasons) + "): recovery after two load weeks."
        )

    if friel_cycle_type == "recovery":
        week_classification = "recovery"
        load_direction = "fall"
        progression_type = "restraint"

    requested_week_type = planning_constraints.get("requested_week_type")
    if requested_week_type == "recovery":
        week_classification = "recovery"
        load_direction = "fall"
        progression_type = "restraint"
        friel_cycle_type = "recovery"
        if planning_constraints.get("current_block_week"):
            friel_block_week = int(planning_constraints["current_block_week"])
        else:
            friel_block_week = max(int(friel_block_week or 1), block_length)
    elif planning_constraints.get("current_block_week"):
        friel_block_week = int(planning_constraints["current_block_week"])

    # Long holiday next week: it becomes the unload week, so this week may load.
    holidays = holiday_weeks(snapshot)
    if (
        holidays["next_week"]
        and not holidays["this_week"]
        and friel_cycle_type == "recovery"
        and requested_week_type != "recovery"
        and readiness == "green"
    ):
        friel_cycle_type = "load"
        week_classification = "build"
        load_direction = "rise"
        progression_type = "specificity"
        friel_block_week = min(int(friel_block_week or 3), 3)
        notes.append(
            f"The holiday ({holidays['next_week'].get('name', 'holiday')}) next week takes the unload role, so this week stays a load week."
        )

    underload = (performance.get("underload") or {}).get("status")
    if (
        readiness == "green"
        and underload == "detected"
        and friel_cycle_type != "recovery"
        and not returning
    ):
        week_classification = "build"
        load_direction = "rise"
        progression_type = "density"
        friel_cycle_type = "load"
    elif readiness != "green" and friel_cycle_type != "recovery":
        week_classification = "absorb"
        load_direction = "fall"
        progression_type = "restraint"
        friel_cycle_type = "load"
    elif trend_up and friel_cycle_type != "recovery":
        week_classification = "build"
        load_direction = "rise"
        progression_type = "specificity"
        friel_cycle_type = "load"

    adherence_score = float(prior_plan_adherence.get("adherence_score", 1.0) or 1.0)
    if (
        prior_plan_adherence.get("status") == "available"
        and adherence_score < 0.65
        and friel_cycle_type != "recovery"
    ):
        if _health_explained_misses(snapshot):
            notes.append(
                "Last week's misses were illness/injury, not unreliability: no adherence penalty."
            )
        else:
            week_classification = "absorb"
            load_direction = "fall"
            progression_type = "restraint"

    if returning and friel_cycle_type != "recovery" and phase not in {"taper", "transition"}:
        progression_type = "reentry"
        load_direction = "rise"
        notes.append(
            "Return from a break: "
            + str(return_from_break.get("rationale") or "re-entry ramp")
            + " Volume, long run and run km follow the re-entry caps; no tests."
        )

    no_tests_reason = None
    if holidays["this_week"]:
        # Holiday = maintenance: 2-3 short easy sessions + one optional long or
        # hilly session; it does not count as a recovery week that resets blocks.
        week_classification = "holiday"
        load_direction = "hold"
        progression_type = "maintenance"
        friel_cycle_type = "maintenance"
        no_tests_reason = "Holiday week: no threshold testing away from the usual setup."
        notes.append(
            f"Holiday week ({holidays['this_week'].get('name', 'holiday')}): maintenance, not a recovery week; "
            "2-3 short easy sessions plus one optional long or hilly session, quality optional."
        )

    if phase == "taper":
        week_classification = "sharpen"
        load_direction = "fall"
        progression_type = "specificity"
        friel_cycle_type = "taper"
        no_tests_reason = "Taper: no threshold test before the race."
    elif phase == "transition":
        week_classification = "recovery"
        load_direction = "fall"
        progression_type = "restraint"
        friel_cycle_type = "recovery"
        no_tests_reason = "Post-race transition: no tests, no quality, easy unstructured training."
        notes.append(
            f"Transition after {completed_race.get('name', 'the race')}: easy, optional, unstructured sessions only."
        )

    next_recovery_due = (
        max(0, block_length - friel_block_week) if friel_cycle_type == "load" else 0
    )
    bucket = race_bucket(priority_race) if priority_race else "unknown"
    focus_key = bucket if bucket in FOCUS_BY_DISTANCE else "generic"
    bike_targets = any(
        family.startswith("bike")
        for family in focus_families(focus_key, phase if phase in LOAD_FOCUS_PHASES else "base")[:3]
    )
    if no_tests_reason:
        threshold_tests = {
            "scheduled": [],
            "deferred": [
                {"discipline": discipline, "reason": no_tests_reason}
                for discipline in ("bike", "run")
            ],
            "rationale": no_tests_reason,
        }
    else:
        threshold_tests = plan_threshold_tests(
            snapshot.get("threshold_testing", {}),
            # Same definition the planner uses to build a recovery week.
            recovery_week=week_classification in {"recovery", "unload"}
            or friel_cycle_type == "recovery",
            readiness_status=readiness,
            # Block week 1 straight after a recovery week may also hold a test.
            allow_block_week_1=friel_cycle_type == "load" and not returning,
            priority_bucket=bucket,
            bike_targets_prescribed=bike_targets,
            return_from_break=returning,
        )

    taper, race_specific, build = phase_windows(priority_race)
    rationale = (
        f"Friel-style block logic ({block_length - 1}:1): the block week and load/recovery rhythm "
        "follow the completed weeks (persisted state only fills gaps), readiness and adherence "
        "adjust the stress, and the phase follows the Intervals calendar for the priority race "
        f"(taper {taper} d, race-specific {race_specific} d, build {build} d before it), with "
        "B races as season checkpoints."
    )
    if notes:
        rationale += " " + " ".join(notes)

    return {
        "agent": "macro_coach",
        "macro_phase": phase,
        "week_classification": week_classification,
        "load_direction": load_direction,
        "progression_type": progression_type,
        "friel_block_week": friel_block_week,
        "friel_cycle_type": friel_cycle_type,
        "block_rhythm": f"{block_length - 1}:1",
        "block_rhythm_reasons": short_reasons,
        "next_recovery_due": next_recovery_due,
        "threshold_tests": threshold_tests,
        "phase_focus": phase_focus,
        "days_to_priority_race": days_to_a,
        "taper_days": taper,
        "transition_after": completed_race.get("name") if completed_race else None,
        "season_checkpoints": checkpoints,
        "return_from_break": returning,
        "preserve": [
            "the quality sessions the readiness allowance supports, each one rung up its ladder",
            "weekend long ride and long run rhythm",
        ],
        "avoid": [
            "back-to-back hard run and heavy strength days",
            "repeating last week's dose when readiness is green (no progression = no adaptation)",
        ],
        "rationale": rationale,
    }
