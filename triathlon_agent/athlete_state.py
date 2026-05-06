from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .race_selection import select_priority_race
from .utils import ensure_dir, read_json, write_json


DEFAULT_ATHLETE_STATE: dict[str, Any] = {
    "version": 1,
    "active_macro_cycle": {
        "block_start_date": None,
        "current_block_week": None,
        "cycle_type": None,
        "phase": None,
        "priority_race": None,
        "last_updated_from_run": None,
        "override_rationale": None,
    },
    "history": [],
}


def athlete_state_path(base_dir: str | Path = "data") -> Path:
    return Path(base_dir) / "athlete_state.json"


def load_athlete_state(path: str | Path = athlete_state_path()) -> dict[str, Any]:
    resolved = Path(path)
    if not resolved.exists():
        return deepcopy(DEFAULT_ATHLETE_STATE)
    payload = read_json(resolved)
    if not isinstance(payload, dict):
        return deepcopy(DEFAULT_ATHLETE_STATE)
    state = deepcopy(DEFAULT_ATHLETE_STATE)
    state.update(payload)
    state.setdefault("active_macro_cycle", {})
    state.setdefault("history", [])
    state["active_macro_cycle"] = {
        **DEFAULT_ATHLETE_STATE["active_macro_cycle"],
        **state["active_macro_cycle"],
    }
    return state


def write_athlete_state(path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    resolved = Path(path)
    ensure_dir(resolved.parent)
    write_json(resolved, payload)
    return payload


def bootstrap_athlete_state(
    snapshot: dict[str, Any], state: dict[str, Any] | None = None
) -> dict[str, Any]:
    current = deepcopy(state or load_athlete_state())
    cycle = current.setdefault("active_macro_cycle", {})
    if (
        cycle.get("phase")
        and cycle.get("cycle_type")
        and cycle.get("current_block_week")
    ):
        return current

    priority_race = select_priority_race(snapshot)
    periodization_state = snapshot.get("periodization_state", {})
    active = {
        "block_start_date": snapshot.get("week_start"),
        "current_block_week": periodization_state.get("next_block_week"),
        "cycle_type": periodization_state.get("recommended_next_week_type"),
        "phase": None,
        "priority_race": priority_race,
        "last_updated_from_run": None,
        "override_rationale": None,
    }
    if priority_race:
        active["priority_race"] = {
            "name": priority_race.get("name"),
            "date": priority_race.get("date"),
            "priority": priority_race.get("priority"),
            "distance": priority_race.get("distance"),
            "source": priority_race.get("source"),
        }
    current["active_macro_cycle"] = {**cycle, **active}
    return current


def update_athlete_state_from_plan(
    state: dict[str, Any],
    final_plan: dict[str, Any],
    run_dir: str | Path,
) -> dict[str, Any]:
    updated = deepcopy(state)
    cycle = updated.setdefault("active_macro_cycle", {})
    macro = final_plan.get("macro_cycle_decision", {})
    race = final_plan.get("race_notes", {}).get("priority_race") or {}
    cycle.update(
        {
            "block_start_date": final_plan.get("sessions", [{}])[0].get("date")
            if final_plan.get("sessions")
            else cycle.get("block_start_date"),
            "current_block_week": macro.get(
                "friel_block_week", cycle.get("current_block_week")
            ),
            "cycle_type": macro.get("friel_cycle_type", cycle.get("cycle_type")),
            "phase": macro.get("phase", cycle.get("phase")),
            "priority_race": race or cycle.get("priority_race"),
            "last_updated_from_run": str(run_dir),
            "override_rationale": final_plan.get("macro_cycle_decision", {}).get(
                "override_rationale"
            ),
        }
    )
    history = list(updated.get("history", []))
    history.append(
        {
            "source_run": str(run_dir),
            "week_start": final_plan.get("sessions", [{}])[0].get("date")
            if final_plan.get("sessions")
            else None,
            "classification": macro.get("classification"),
            "phase": macro.get("phase"),
        }
    )
    updated["history"] = history[-12:]
    return updated
