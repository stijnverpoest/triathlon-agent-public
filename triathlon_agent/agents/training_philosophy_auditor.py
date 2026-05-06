from __future__ import annotations

from typing import Any


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
    elif len(daniels_quality) > 2:
        daniels_alignment = "adjust"
        required_edits.append(
            "Reduce the number of Daniels quality sessions so the week does not over-concentrate hard running."
        )
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
    else:
        findings.append(
            "Coggan bike structure is coherent: all ride sessions declare a Coggan level plus IF/TSS expectations."
        )

    friel_alignment = "pass"
    if macro.get("friel_block_week") is None or macro.get("friel_cycle_type") is None:
        friel_alignment = "fail"
        required_edits.append(
            "Macro coach must declare Friel block week and cycle type."
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
