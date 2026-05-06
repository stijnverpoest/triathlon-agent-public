from __future__ import annotations

from pathlib import Path
from typing import Any

from .utils import read_json


AGENT_REQUIREMENTS: dict[str, list[str]] = {
    "performance_doctor": [
        "agent",
        "readiness_status",
        "readiness_score",
        "objective_findings",
        "subjective_findings",
        "main_risk",
        "constraints",
        "load_recommendation",
        "baseline_sources",
        "missing_data",
    ],
    "macro_coach": [
        "agent",
        "macro_phase",
        "week_classification",
        "load_direction",
        "progression_type",
        "friel_block_week",
        "friel_cycle_type",
        "phase_focus",
        "preserve",
        "avoid",
        "rationale",
    ],
    "race_director": [
        "agent",
        "priority_race_lens",
        "race_specific_needs",
        "mistake_to_avoid",
        "specificity_locations",
        "race_recommendations",
    ],
    "phase_alignment_auditor": [
        "agent",
        "current_phase",
        "priority_race",
        "alignment_status",
        "week_fit_summary",
        "supporting_signals",
        "required_adjustments",
        "forbidden_elements",
    ],
    "training_philosophy_auditor": [
        "agent",
        "overall_status",
        "daniels_alignment",
        "coggan_alignment",
        "friel_alignment",
        "findings",
        "required_edits",
    ],
    "head_coach_planner": [
        "agent",
        "strategic_theme",
        "key_sessions",
        "adaptations_targeted",
        "sessions",
    ],
    "validator_coach": [
        "agent",
        "strengths",
        "weaknesses",
        "proposed_edits",
        "simpler_alternative",
        "verdict",
    ],
    "nutritionist": [
        "agent",
        "week_strategy",
        "daily_priorities",
        "session_fueling",
        "daily_targets",
        "hydration_targets",
        "energy_availability_risk",
        "daily_notes",
        "hydration_guidance",
        "recovery_priorities",
        "major_concerns",
    ],
    "orchestrator": [
        "executive_summary",
        "athlete_brief",
        "coach_note",
        "what_last_week_tells_us",
        "strategic_overview",
        "macro_cycle_decision",
        "phase_alignment_check",
        "training_philosophy_check",
        "readiness_status",
        "readiness_score",
        "risk_status",
        "sessions",
        "weekly_time_budget",
        "weekly_focus_notes",
        "changes_vs_prior_week",
        "validator_adjustments_incorporated",
        "nutrition_summary",
        "write_back_preview",
        "approval_checkpoint",
    ],
    "intervals_syntax_guard": [
        "status",
        "intent_fidelity",
        "final_description",
        "rewrite_reason",
        "push_ready",
    ],
}

SESSION_REQUIRED_FIELDS = [
    "day",
    "date",
    "discipline",
    "session_name",
    "objective",
    "duration_min",
    "intensity_guidance",
    "primary_metric",
    "blocks",
    "execution_notes",
    "success_criteria",
    "cut_first",
]


def _missing_keys(payload: dict[str, Any], keys: list[str]) -> list[str]:
    return [key for key in keys if key not in payload]


def _validate_note(note: dict[str, Any], prefix: str) -> list[str]:
    required = ["date", "day", "title", "body", "push_to_intervals"]
    missing = _missing_keys(note, required)
    if missing:
        return [f"{prefix} is missing: {', '.join(missing)}"]
    return []


def validate_agent_output(agent_name: str, payload: dict[str, Any]) -> list[str]:
    errors = []
    required = AGENT_REQUIREMENTS.get(agent_name, [])
    missing = _missing_keys(payload, required)
    if missing:
        errors.append(f"Missing required keys for {agent_name}: {', '.join(missing)}")
    if agent_name in {"head_coach_planner", "orchestrator"}:
        sessions = payload.get("sessions", [])
        if not isinstance(sessions, list) or not sessions:
            errors.append(f"{agent_name} must include a non-empty sessions list.")
        else:
            for index, session in enumerate(sessions, start=1):
                missing_session = _missing_keys(session, SESSION_REQUIRED_FIELDS)
                if missing_session:
                    errors.append(
                        f"Session {index} in {agent_name} is missing: {', '.join(missing_session)}"
                    )
                discipline = session.get("discipline")
                if discipline == "Run" and not session.get("daniels_type"):
                    errors.append(
                        f"Session {index} in {agent_name} is missing daniels_type for run methodology enforcement."
                    )
                if discipline == "Ride":
                    if not session.get("coggan_level"):
                        errors.append(
                            f"Session {index} in {agent_name} is missing coggan_level for bike methodology enforcement."
                        )
                    if (
                        session.get("expected_if") is None
                        or session.get("estimated_tss") is None
                    ):
                        errors.append(
                            f"Session {index} in {agent_name} is missing expected_if or estimated_tss for bike methodology enforcement."
                        )
    if agent_name == "orchestrator":
        weekly_time_budget = payload.get("weekly_time_budget", {})
        missing_budget = _missing_keys(
            weekly_time_budget,
            [
                "target_min",
                "minimum_effective_min",
                "maximum_effective_min",
                "mode",
                "rationale",
            ],
        )
        if missing_budget:
            errors.append(f"weekly_time_budget is missing: {', '.join(missing_budget)}")
        focus_notes = payload.get("weekly_focus_notes", [])
        if not isinstance(focus_notes, list):
            errors.append("weekly_focus_notes must be a list.")
        else:
            for index, note in enumerate(focus_notes, start=1):
                if isinstance(note, dict):
                    errors.extend(_validate_note(note, f"weekly_focus_notes[{index}]"))
                else:
                    errors.append(f"weekly_focus_notes[{index}] must be an object.")
        macro_cycle = payload.get("macro_cycle_decision", {})
        missing_macro = _missing_keys(
            macro_cycle,
            [
                "phase",
                "classification",
                "rationale",
                "friel_block_week",
                "friel_cycle_type",
                "phase_focus",
            ],
        )
        if missing_macro:
            errors.append(
                f"macro_cycle_decision is missing: {', '.join(missing_macro)}"
            )
    return errors


def validate_final_plan(payload: dict[str, Any]) -> list[str]:
    return validate_agent_output("orchestrator", payload)


def load_and_validate_json(
    path: str | Path, validator_name: str
) -> tuple[dict[str, Any], list[str]]:
    payload = read_json(path)
    if validator_name == "final_plan":
        return payload, validate_final_plan(payload)
    return payload, validate_agent_output(validator_name, payload)
