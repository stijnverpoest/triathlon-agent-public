from __future__ import annotations

from typing import Any

from ..race_selection import select_priority_race


def run(snapshot: dict[str, Any], performance: dict[str, Any]) -> dict[str, Any]:
    priority_race = select_priority_race(snapshot)
    days_to_a = priority_race.get("days_to_race", 9999) if priority_race else 9999
    athlete_state = snapshot.get("athlete_state", {})
    active_cycle = athlete_state.get("active_macro_cycle", {})
    phase = str(active_cycle.get("phase") or "base")
    if days_to_a <= 14:
        phase = "taper"
    elif days_to_a <= 49:
        phase = "race_specific"
    elif days_to_a <= 112:
        phase = "build"

    weekly_windows = snapshot.get("weekly_windows", [])
    periodization_state = snapshot.get("periodization_state", {})
    planning_constraints = snapshot.get("planning_constraints", {})
    prior_plan_adherence = snapshot.get("prior_plan_adherence", {})
    recent_loads = (
        [window["load"] for window in weekly_windows[-3:]] if weekly_windows else []
    )
    trend_up = len(recent_loads) >= 2 and recent_loads[-1] >= recent_loads[0]
    week_classification = "build"
    load_direction = "rise"
    progression_type = "specificity"
    friel_cycle_type = str(active_cycle.get("cycle_type") or "load")
    friel_block_week = int(
        active_cycle.get("current_block_week")
        or periodization_state.get("next_block_week", 1)
        or 1
    )
    phase_focus = "general aerobic and durability development"

    if phase == "build":
        phase_focus = "raise sustainable threshold and race-specific durability"
    elif phase == "race_specific":
        phase_focus = "increase race-specific execution and pacing precision"
    elif phase == "taper":
        phase_focus = "shed fatigue while preserving race sharpness"

    if periodization_state.get("recommended_next_week_type") == "recovery":
        week_classification = "recovery"
        load_direction = "fall"
        progression_type = "restraint"
        friel_cycle_type = "recovery"

    requested_week_type = planning_constraints.get("requested_week_type")
    if requested_week_type == "recovery":
        week_classification = "recovery"
        load_direction = "fall"
        progression_type = "restraint"
        friel_cycle_type = "recovery"
        if planning_constraints.get("current_block_week"):
            friel_block_week = int(planning_constraints["current_block_week"])
        else:
            friel_block_week = max(int(friel_block_week or 1), 4)

    if active_cycle.get("cycle_type") and requested_week_type != "recovery":
        week_classification = active_cycle.get("cycle_type") or week_classification
    if active_cycle.get("priority_race") and priority_race is None:
        priority_race = active_cycle.get("priority_race")

    if performance["readiness_status"] != "green" and friel_cycle_type != "recovery":
        week_classification = "absorb"
        load_direction = "fall"
        progression_type = "restraint"
        friel_cycle_type = "load"
    elif trend_up:
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
        week_classification = "absorb"
        load_direction = "fall"
        progression_type = "restraint"

    if phase == "taper":
        week_classification = "sharpen"
        load_direction = "fall"
        progression_type = "specificity"
        friel_cycle_type = "taper"

    next_recovery_due = (
        max(0, 4 - friel_block_week) if friel_cycle_type == "load" else 0
    )

    return {
        "agent": "macro_coach",
        "macro_phase": phase,
        "week_classification": week_classification,
        "load_direction": load_direction,
        "progression_type": progression_type,
        "friel_block_week": friel_block_week,
        "friel_cycle_type": friel_cycle_type,
        "next_recovery_due": next_recovery_due,
        "phase_focus": phase_focus,
        "preserve": [
            "one high-quality bike stimulus",
            "one high-quality run stimulus",
            "weekend long ride and long run rhythm",
        ],
        "avoid": [
            "back-to-back hard run and heavy strength days",
            "chasing extra load because one session felt good",
        ],
        "rationale": (
            "Follow a Friel-style block logic: prefer persisted macro-cycle state when available, then confirm whether the current week is a load or unload week from recent completed weeks and adjust the stress up or down based on readiness and race timing."
        ),
    }
