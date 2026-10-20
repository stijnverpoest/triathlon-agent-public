from __future__ import annotations

from typing import Any

from ..progression import session_dose
from ..coaching_limits import DANIELS_CAPS, DANIELS_FAMILY_TYPE


# Plausible IF ceilings/floors per Coggan level: outside them the label is wrong.
COGGAN_IF_MAX = {"L1 Active Recovery": 0.65, "L2 Endurance": 0.80, "L3 Tempo": 0.90}
COGGAN_IF_MIN = {"L4 Threshold": 0.70, "L5 VO2max": 0.70, "L6 Anaerobic": 0.70}
# A Friel recovery week plans clearly less than the recent load weeks.
RECOVERY_MAX_RATIO = 0.85


def daniels_cap_violations(run_sessions: list[dict[str, Any]]) -> list[str]:
    weekly = sum(
        float(session.get("duration_min") or 0)
        for session in run_sessions
        if not session.get("race_day")
    )
    problems: list[str] = []
    if not weekly:
        return problems
    for session in run_sessions:
        by_type: dict[str, float] = {}
        for family, minutes in session_dose(session).items():
            daniels = DANIELS_FAMILY_TYPE.get(family)
            if daniels:
                by_type[daniels] = by_type.get(daniels, 0.0) + minutes
        for daniels, minutes in by_type.items():
            share, floor = DANIELS_CAPS[daniels]
            cap = max(floor, share * weekly)
            if minutes > cap:
                problems.append(
                    f"{session['session_name']}: {minutes:g}' of {daniels} exceeds the Daniels cap of ~{cap:.0f}' "
                    f"({int(share * 100)}% of {weekly:g}' weekly running)."
                )
    return problems


def coggan_if_mismatches(ride_sessions: list[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    for session in ride_sessions:
        level = session.get("coggan_level")
        value = session.get("expected_if")
        if level is None or value is None:
            continue
        value = float(value)
        if level in COGGAN_IF_MAX and value > COGGAN_IF_MAX[level]:
            problems.append(
                f"{session['session_name']} is labelled {level} but expects IF {value:.2f} (> {COGGAN_IF_MAX[level]:.2f})."
            )
        if level in COGGAN_IF_MIN and value < COGGAN_IF_MIN[level] and not session.get("threshold_test"):
            problems.append(
                f"{session['session_name']} is labelled {level} but expects IF {value:.2f} (< {COGGAN_IF_MIN[level]:.2f})."
            )
    return problems


def recovery_week_load_ratio(
    snapshot: dict[str, Any], sessions: list[dict[str, Any]]
) -> float | None:
    """Planned minutes / average of the recent load weeks, or None without history."""
    weeks = (snapshot.get("periodization_state") or {}).get("classified_weeks") or []
    load_minutes = [
        float(week.get("minutes") or 0)
        for week in weeks[-4:]
        if week.get("week_type") == "load" and float(week.get("minutes") or 0) > 0
    ]
    if not load_minutes:
        return None
    planned = sum(
        float(session.get("duration_min") or 0)
        for session in sessions
        if session.get("discipline") != "Recovery"
    )
    return planned / (sum(load_minutes) / len(load_minutes))


def run(
    snapshot: dict[str, Any],
    macro: dict[str, Any],
    race: dict[str, Any],
    phase_alignment: dict[str, Any],
    draft_plan: dict[str, Any],
) -> dict[str, Any]:
    run_sessions = [
        session
        for session in draft_plan.get("sessions", [])
        if session.get("discipline") == "Run"
    ]
    ride_sessions = [
        session
        for session in draft_plan.get("sessions", [])
        if session.get("discipline") == "Ride"
    ]

    findings: list[str] = []
    required_edits: list[str] = []

    missing_daniels = [
        session["session_name"]
        for session in run_sessions
        if not session.get("daniels_type")
    ]
    missing_coggan = [
        session["session_name"]
        for session in ride_sessions
        if not session.get("coggan_level")
    ]
    missing_bike_metrics = [
        session["session_name"]
        for session in ride_sessions
        if session.get("expected_if") is None or session.get("estimated_tss") is None
    ]

    daniels_quality = [
        session
        for session in run_sessions
        if session.get("daniels_type") in {"T", "I", "R"}
    ]
    daniels_alignment = "pass"
    if missing_daniels:
        daniels_alignment = "fail"
        required_edits.append(
            f"Tag all run sessions with Daniels types. Missing: {', '.join(missing_daniels)}."
        )
    elif len(daniels_quality) > 3:
        daniels_alignment = "adjust"
        required_edits.append(
            "More than three quality runs in one week breaks the pyramidal distribution; keep most running E."
        )
    elif daniels_cap_violations(run_sessions):
        daniels_alignment = "adjust"
        required_edits.extend(daniels_cap_violations(run_sessions))
    else:
        findings.append(
            f"Daniels run structure is coherent: {len(daniels_quality)} quality run session(s) and the rest anchored in E running."
        )

    coggan_alignment = "pass"
    if missing_coggan:
        coggan_alignment = "fail"
        required_edits.append(
            f"Tag all bike sessions with Coggan levels. Missing: {', '.join(missing_coggan)}."
        )
    elif missing_bike_metrics:
        coggan_alignment = "adjust"
        required_edits.append(
            f"Add expected IF and estimated TSS to bike sessions. Missing: {', '.join(missing_bike_metrics)}."
        )
    elif coggan_if_mismatches(ride_sessions):
        coggan_alignment = "adjust"
        required_edits.extend(coggan_if_mismatches(ride_sessions))
    else:
        findings.append(
            "Coggan bike structure is coherent: all ride sessions declare a Coggan level plus IF/TSS expectations."
        )

    friel_alignment = "pass"
    recovery_ratio = (
        recovery_week_load_ratio(snapshot, draft_plan.get("sessions", []))
        if macro.get("friel_cycle_type") == "recovery"
        or macro.get("week_classification") == "recovery"
        else None
    )
    if macro.get("friel_block_week") is None or macro.get("friel_cycle_type") is None:
        friel_alignment = "fail"
        required_edits.append(
            "Macro coach must declare Friel block week and cycle type."
        )
    elif recovery_ratio is not None and recovery_ratio >= RECOVERY_MAX_RATIO:
        friel_alignment = "adjust"
        required_edits.append(
            f"The recovery week plans {int(recovery_ratio * 100)}% of the recent load-week volume; "
            f"a Friel recovery week stays below ~{int(RECOVERY_MAX_RATIO * 100)}% (typically 50-70%)."
        )
    elif phase_alignment.get("alignment_status") != "aligned":
        friel_alignment = "adjust"
        required_edits.append(
            "Resolve the phase-alignment issues before treating the week as final."
        )
    else:
        findings.append(
            f"Friel periodization is coherent: the week is a {macro.get('week_classification')} week in block week {macro.get('friel_block_week')} of the current cycle."
        )

    statuses = {daniels_alignment, coggan_alignment, friel_alignment}
    overall_status = "pass"
    if "fail" in statuses:
        overall_status = "fail"
    elif "adjust" in statuses:
        overall_status = "pass_with_edits"

    return {
        "agent": "training_philosophy_auditor",
        "overall_status": overall_status,
        "daniels_alignment": daniels_alignment,
        "coggan_alignment": coggan_alignment,
        "friel_alignment": friel_alignment,
        "findings": findings,
        "required_edits": required_edits,
        "confidence": 0.79,
    }
