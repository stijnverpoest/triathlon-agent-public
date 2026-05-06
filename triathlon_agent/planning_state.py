from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .utils import ensure_dir, read_json, write_json


DEFAULT_REQUIRED_STEPS = [
    "prepare_run",
    "freshness_check",
    "last_week_summary",
    "post_fetch_checkin",
    "performance_doctor",
    "macro_coach",
    "race_director",
    "head_coach_planner",
    "phase_alignment_auditor",
    "training_philosophy_auditor",
    "validator_coach",
    "revision_loop",
    "nutritionist",
    "final_plan",
    "finalize_run",
    "approval",
    "syntax_guard",
    "push",
]

FINALIZE_REQUIRED_STEPS = [
    "prepare_run",
    "freshness_check",
    "last_week_summary",
    "post_fetch_checkin",
    "performance_doctor",
    "macro_coach",
    "race_director",
    "head_coach_planner",
    "phase_alignment_auditor",
    "training_philosophy_auditor",
    "validator_coach",
    "revision_loop",
    "nutritionist",
    "final_plan",
]


def planning_state_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "planning_state.json"


def load_planning_state(run_dir: str | Path) -> dict[str, Any]:
    path = planning_state_path(run_dir)
    if not path.exists():
        return {"required_steps": list(DEFAULT_REQUIRED_STEPS), "completed_steps": []}
    payload = read_json(path)
    if not isinstance(payload, dict):
        return {"required_steps": list(DEFAULT_REQUIRED_STEPS), "completed_steps": []}
    payload.setdefault("required_steps", list(DEFAULT_REQUIRED_STEPS))
    payload.setdefault("completed_steps", [])
    payload.setdefault("step_artifacts", {})
    return payload


def initialize_planning_state(
    run_dir: str | Path,
    *,
    workflow_mode: str,
    freshness: dict[str, Any],
    planning_date: str,
    post_fetch_checkin: dict[str, Any],
) -> dict[str, Any]:
    state = {
        "workflow_mode": workflow_mode,
        "planning_date": planning_date,
        "freshness": freshness,
        "post_fetch_checkin": post_fetch_checkin,
        "required_steps": list(DEFAULT_REQUIRED_STEPS),
        "completed_steps": ["prepare_run"],
        "step_artifacts": {},
        "revision_count": 0,
        "override_flags": [],
        "approval_status": "pending",
        "push_status": "not_started",
    }
    write_json(planning_state_path(run_dir), state)
    return state


def mark_step_complete(
    run_dir: str | Path,
    step_name: str,
    *,
    artifact_path: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = load_planning_state(run_dir)
    completed = list(dict.fromkeys([*state.get("completed_steps", []), step_name]))
    state["completed_steps"] = completed
    if artifact_path:
        step_artifacts = dict(state.get("step_artifacts", {}))
        step_artifacts[step_name] = artifact_path
        state["step_artifacts"] = step_artifacts
    if extra:
        state.update(extra)
    write_json(planning_state_path(run_dir), state)
    return state


def finalize_planning_state(
    run_dir: str | Path,
    *,
    approval_status: str | None = None,
    push_status: str | None = None,
) -> dict[str, Any]:
    state = load_planning_state(run_dir)
    if approval_status:
        state["approval_status"] = approval_status
    if push_status:
        state["push_status"] = push_status
    write_json(planning_state_path(run_dir), state)
    return state


def validate_planning_state_for_finalize(run_dir: str | Path) -> list[str]:
    state = load_planning_state(run_dir)
    if state.get("workflow_mode") != "live":
        return []
    missing = [
        step
        for step in FINALIZE_REQUIRED_STEPS
        if step not in state.get("completed_steps", [])
    ]
    errors = []
    if missing:
        errors.append(f"Missing required workflow steps: {', '.join(missing)}")
    freshness = state.get("freshness", {})
    if freshness.get("status") == "stale" and not freshness.get("override_approved"):
        errors.append("Freshness is stale and no override was approved.")
    return errors
