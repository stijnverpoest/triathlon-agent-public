from __future__ import annotations

from typing import Any

from .macro_coach import phase_windows


QUALITY_COGGAN_LEVELS = {"L4 Threshold", "L5 VO2max", "L6 Anaerobic"}
DAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def _is_intensity(session: dict[str, Any]) -> bool:
    if session.get("threshold_test") or session.get("race_day"):
        return False
    return bool(
        session.get("progression")
        or session.get("coggan_level") in QUALITY_COGGAN_LEVELS
        or session.get("daniels_type") in {"T", "I", "R"}
    )


def run(
    snapshot: dict[str, Any],
    macro: dict[str, Any],
    race: dict[str, Any],
    draft_plan: dict[str, Any],
) -> dict[str, Any]:
    priority_race = race.get("priority_race", {}) or {}
    days_to_race = priority_race.get("days_to_race", 9999)
    phase = macro.get("macro_phase", "build")
    classification = macro.get("week_classification", "unknown")
    # Same windows as the macro coach, so the two never disagree on the taper start.
    taper_window, race_specific_window, _ = phase_windows(
        priority_race if priority_race.get("days_to_race") is not None else None
    )
    alignment_status = "aligned"
    required_adjustments: list[str] = []
    forbidden_elements: list[str] = []
    supporting_signals = [
        f"Macro coach classifies the week as {classification} inside the {phase} phase.",
        f"The primary race lens is {priority_race.get('name', 'no priority race')} in {days_to_race} days.",
        f"Friel block week is {macro.get('friel_block_week', 'unknown')} with cycle type {macro.get('friel_cycle_type', 'unknown')}.",
    ]
    sessions = draft_plan.get("sessions", [])
    key_sessions = [session for session in sessions if session.get("priority") == "key"]
    quality_ride_count = sum(
        1
        for session in sessions
        if session.get("discipline") == "Ride"
        and (
            session.get("coggan_level") in QUALITY_COGGAN_LEVELS
            or float(session.get("expected_if", 0) or 0) >= 0.78
            or session.get("progression")
        )
    )
    quality_run_count = sum(
        1
        for session in sessions
        if session.get("discipline") == "Run"
        and (session.get("progression") or session.get("daniels_type") in {"T", "I", "R"})
    )
    long_session_count = sum(
        1 for session in sessions if int(session.get("duration_min", 0) or 0) >= 70
    )
    intensity_sessions = [session for session in sessions if _is_intensity(session)]

    if phase == "transition":
        supporting_signals.append(
            f"Post-race transition after {macro.get('transition_after') or 'the race'}: easy, optional, unstructured."
        )
        if intensity_sessions:
            alignment_status = "adjust"
            required_adjustments.append(
                "The post-race transition carries no quality; make every session easy and optional."
            )
            forbidden_elements.append("Quality sessions or tests in the transition week.")
    elif days_to_race <= taper_window and phase != "taper":
        alignment_status = "misaligned"
        required_adjustments.append(
            f"The race is {days_to_race} days out, inside the {taper_window}-day taper: move into taper logic instead of build density."
        )
        forbidden_elements.append("Large volume growth or multiple big hero sessions.")
    elif days_to_race <= race_specific_window and phase not in {"race_specific", "taper"}:
        alignment_status = "adjust"
        required_adjustments.append(
            "Increase race-specific content and simplify non-specific intensity."
        )
    elif phase == "build":
        if classification in {"recovery", "holiday"}:
            supporting_signals.append(
                "The week closes the block with recovery-oriented structure: frequency and weekend durability stay in place while threshold density stays low."
            )
        else:
            supporting_signals.append(
                f"The week lands {quality_ride_count} bike quality session(s), {quality_run_count} run quality session(s), and {long_session_count} longer durability session(s), which fits the current build window."
            )
    if classification == "holiday":
        supporting_signals.append(
            "Holiday week: light, low-structure maintenance that doubles as the unload week of the block."
        )

    key_intensity = [session for session in key_sessions if _is_intensity(session)]
    if macro.get("friel_cycle_type") == "recovery" and key_intensity and phase != "transition":
        alignment_status = "adjust"
        required_adjustments.append(
            "A recovery week carries no quality sessions; keep only easy volume and the scheduled threshold test."
        )
        forbidden_elements.append("Quality sessions inside a declared recovery week.")

    supporting_signals.append(
        "Weekly volume is a head-coach decision; the athlete's typical hours are a reference, "
        "but the check-in's available-hours caps bind."
    )
    for checkpoint in macro.get("season_checkpoints") or []:
        if checkpoint.get("priority") == "B" and 0 <= int(checkpoint.get("days_to_race", 99)) <= 6:
            b_quality = [
                session
                for session in intensity_sessions
                if session.get("day") in DAY_NAMES
                and 0 < checkpoint["days_to_race"] - DAY_NAMES.index(session["day"]) <= 2
            ]
            if b_quality:
                alignment_status = "adjust" if alignment_status == "aligned" else alignment_status
                required_adjustments.append(
                    f"B-race {checkpoint.get('name')} needs a mini-taper: no quality in the 72h before it."
                )
    if not priority_race or priority_race.get("days_to_race", 9999) >= 9999:
        supporting_signals.append("No race on your Intervals calendar: a general base week is the right shape.")

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
