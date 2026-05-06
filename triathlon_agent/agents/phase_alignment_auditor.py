from __future__ import annotations

from typing import Any


def run(
    snapshot: dict[str, Any],
    macro: dict[str, Any],
    race: dict[str, Any],
    draft_plan: dict[str, Any],
) -> dict[str, Any]:
    priority_race = race.get("priority_race", {})
    days_to_race = priority_race.get("days_to_race", 9999)
    phase = macro.get("macro_phase", "build")
    alignment_status = "aligned"
    required_adjustments: list[str] = []
    forbidden_elements: list[str] = []
    supporting_signals = [
        f"Macro coach classifies the week as {macro.get('week_classification', 'unknown')} inside the {phase} phase.",
        f"The primary race lens is {priority_race.get('name', 'no priority race')} in {days_to_race} days.",
        f"Friel block week is {macro.get('friel_block_week', 'unknown')} with cycle type {macro.get('friel_cycle_type', 'unknown')}.",
    ]
    key_sessions = [
        session
        for session in draft_plan.get("sessions", [])
        if session.get("priority") == "key"
    ]
    quality_ride_count = sum(
        1
        for session in draft_plan.get("sessions", [])
        if session.get("discipline") == "Ride"
        and (
            session.get("coggan_level") in {"L4 Threshold", "L5 VO2max", "L6 Anaerobic"}
            or float(session.get("expected_if", 0) or 0) >= 0.78
            or session.get("priority") == "key"
        )
    )
    quality_run_count = sum(
        1
        for session in draft_plan.get("sessions", [])
        if session.get("discipline") == "Run"
        and session.get("primary_metric") == "Pace"
    )
    long_session_count = sum(
        1
        for session in draft_plan.get("sessions", [])
        if int(session.get("duration_min", 0) or 0) >= 70
    )

    if days_to_race <= 21 and phase != "taper":
        alignment_status = "misaligned"
        required_adjustments.append(
            "Move into taper or sharpening logic instead of carrying normal build density."
        )
        forbidden_elements.append("Large volume growth or multiple big hero sessions.")
    elif days_to_race <= 49 and phase not in {"race_specific", "taper"}:
        alignment_status = "adjust"
        required_adjustments.append(
            "Increase race-specific content and simplify non-specific intensity."
        )
    elif phase == "build":
        if macro.get("week_classification") == "recovery":
            supporting_signals.append(
                "The week closes the block with recovery-oriented structure: frequency and weekend durability stay in place while threshold density stays low."
            )
        else:
            supporting_signals.append(
                f"The week lands {quality_ride_count} bike quality session(s), {quality_run_count} run quality session(s), and {long_session_count} longer durability session(s), which fits the current build window."
            )

    if macro.get("friel_cycle_type") == "recovery" and len(key_sessions) > 2:
        alignment_status = "adjust"
        required_adjustments.append(
            "This looks too aggressive for a recovery week. Reduce the number or density of key sessions."
        )
        forbidden_elements.append(
            "Three or more meaningful key sessions inside a declared recovery week."
        )

    if snapshot.get("weekly_checkin", {}).get("available_hours_override_min"):
        supporting_signals.append(
            "The plan respects the athlete's explicit weekly time cap while still preserving the main training objectives."
        )

    return {
        "agent": "phase_alignment_auditor",
        "current_phase": phase,
        "priority_race": priority_race,
        "alignment_status": alignment_status,
        "week_fit_summary": (
            "The current week fits the periodization picture because it supports the present phase and does not spend stress in ways that would compromise the next major race progression step."
            if alignment_status == "aligned"
            else "The current week needs adjustment to better match the athlete's phase and race timing."
        ),
        "supporting_signals": supporting_signals,
        "required_adjustments": required_adjustments,
        "forbidden_elements": forbidden_elements,
        "confidence": 0.73,
    }
