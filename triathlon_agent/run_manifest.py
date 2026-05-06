from __future__ import annotations

from pathlib import Path


AGENT_OUTPUT_FILES = {
    "performance_doctor": "performance_doctor.json",
    "macro_coach": "macro_coach.json",
    "race_director": "race_director.json",
    "phase_alignment_auditor": "phase_alignment_auditor.json",
    "training_philosophy_auditor": "training_philosophy_auditor.json",
    "head_coach_planner": "head_coach_planner.json",
    "validator_coach": "validator_coach.json",
    "nutritionist": "nutritionist.json",
    "orchestrator": "orchestrator.json",
}


def agent_outputs_dir(run_dir: str | Path) -> Path:
    return Path(run_dir) / "agent_outputs"


def agent_output_path(run_dir: str | Path, agent_name: str) -> Path:
    return agent_outputs_dir(run_dir) / AGENT_OUTPUT_FILES[agent_name]
