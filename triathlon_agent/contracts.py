from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .utils import read_json


# The schemas in schemas/*.json are the single source of truth for required
# keys: the validator walks them recursively (objects, arrays, $ref), so a
# nested key the report renders can never be missing after finalize.
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
AGENT_SCHEMA_FILES: dict[str, str] = {
    "performance_doctor": "performance_doctor.json",
    "macro_coach": "macro_coach.json",
    "race_director": "race_director.json",
    "phase_alignment_auditor": "phase_alignment_auditor.json",
    "training_philosophy_auditor": "training_philosophy_auditor.json",
    "head_coach_planner": "head_coach_planner.json",
    "validator_coach": "validator_coach.json",
    "nutritionist": "nutritionist.json",
    "orchestrator": "final_plan.json",
    "intervals_syntax_guard": "intervals_syntax_guard.json",
}
# Sessions and threshold testing get their own, more specific checks below
# (the report renders threshold_testing tolerantly).
_SKIP_GENERIC_PROPERTIES = {"sessions", "threshold_testing"}

READINESS_STATUSES = ("green", "yellow", "red")
LOAD_RECOMMENDATIONS = ("increase", "hold", "reduce")
MACRO_PHASES = ("base", "build", "race_specific", "taper", "transition")
SESSION_TERRAINS = (
    "flat",
    "rolling",
    "hilly",
    "trail",
    "treadmill_incline",
    "stairs",
    "indoor",
)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict[str, Any]:
    payload = read_json(SCHEMA_DIR / name)
    if not isinstance(payload, dict):
        raise ValueError(f"Schema {name} must be a JSON object.")
    return payload


def required_keys(agent_name: str) -> list[str]:
    schema_file = AGENT_SCHEMA_FILES.get(agent_name)
    if not schema_file:
        return []
    return list(load_schema(schema_file).get("required") or [])


# Backwards-compatible view of the top-level requirements, read from schemas.
AGENT_REQUIREMENTS: dict[str, list[str]] = {
    name: required_keys(name) for name in AGENT_SCHEMA_FILES
}
SESSION_REQUIRED_FIELDS: list[str] = list(load_schema("session.json").get("required") or [])


def _resolve(schema: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if not ref:
        return schema
    return {**load_schema(str(ref)), **{k: v for k, v in schema.items() if k != "$ref"}}


def schema_errors(
    payload: Any,
    schema: dict[str, Any],
    path: str,
    *,
    skip: set[str] | None = None,
) -> list[str]:
    """Required-key and object/array shape errors, recursively."""
    schema = _resolve(schema)
    errors: list[str] = []
    expected = schema.get("type")
    if expected == "object" and not isinstance(payload, dict):
        return [f"{path} must be an object."]
    if expected == "array" and not isinstance(payload, list):
        return [f"{path} must be a list."]
    if isinstance(payload, dict):
        missing = [key for key in schema.get("required") or [] if key not in payload]
        if missing:
            errors.append(f"{path} is missing: {', '.join(missing)}")
        for key, child in (schema.get("properties") or {}).items():
            if skip and key in skip:
                continue
            if key in payload and payload[key] is not None and isinstance(child, dict):
                errors.extend(schema_errors(payload[key], child, f"{path}.{key}"))
    elif isinstance(payload, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(payload, start=1):
            errors.extend(schema_errors(item, schema["items"], f"{path}[{index}]"))
    return errors


def _enum_error(value: Any, allowed: tuple[str, ...], label: str) -> list[str]:
    if value in allowed:
        return []
    hint = " Use 'yellow' for amber." if value == "amber" else ""
    return [f"{label} must be one of: {', '.join(allowed)} (got {value!r}).{hint}"]


def _missing_keys(payload: dict[str, Any], keys: list[str]) -> list[str]:
    return [key for key in keys if key not in payload]


def _missing_keys_error(
    payload: dict[str, Any], keys: list[str], prefix: str
) -> list[str]:
    missing = _missing_keys(payload, keys)
    if missing:
        return [f"{prefix} is missing: {', '.join(missing)}"]
    return []


THRESHOLD_UPDATE_REQUIRED_FIELDS = [
    "discipline",
    "sport",
    "sport_settings_id",
    "field",
    "current",
    "proposed",
    "apply",
]
THRESHOLD_UPDATE_FIELDS = {"ftp", "lthr", "threshold_pace"}


def _validate_threshold_tests(decision: Any, prefix: str) -> list[str]:
    if not isinstance(decision, dict):
        return [f"{prefix} must be an object."]
    errors = _missing_keys_error(decision, ["scheduled", "deferred", "rationale"], prefix)
    for index, entry in enumerate(decision.get("scheduled", []) or [], start=1):
        if entry.get("discipline") not in {"bike", "run"} or not entry.get("protocol"):
            errors.append(
                f"{prefix}.scheduled[{index}] needs discipline 'bike' or 'run' and a protocol."
            )
    return errors


def _validate_threshold_updates(updates: Any, prefix: str) -> list[str]:
    if not isinstance(updates, list):
        return [f"{prefix} must be a list."]
    errors = []
    for index, update in enumerate(updates, start=1):
        label = f"{prefix}[{index}]"
        errors.extend(
            _missing_keys_error(update, THRESHOLD_UPDATE_REQUIRED_FIELDS, label)
        )
        if update.get("field") not in THRESHOLD_UPDATE_FIELDS:
            errors.append(
                f"{label}.field must be one of: {', '.join(sorted(THRESHOLD_UPDATE_FIELDS))}."
            )
        if update.get("apply") and update.get("sport_settings_id") is None:
            errors.append(f"{label} cannot apply without a sport_settings_id.")
    return errors


def _validate_session_extras(session: dict[str, Any], label: str) -> list[str]:
    """Optional coaching fields: rpe_target / feel_cue / bail_out_rule / terrain."""
    errors: list[str] = []
    for key in ("rpe_target", "feel_cue", "bail_out_rule"):
        if key in session and session[key] is not None and not isinstance(session[key], str):
            errors.append(f"{label}.{key} must be a string.")
    for key in ("elevation_gain_m", "elevation_loss_m"):
        value = session.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
        ):
            errors.append(f"{label}.{key} must be a non-negative number.")
    terrain = session.get("terrain")
    if terrain is not None and terrain not in SESSION_TERRAINS:
        errors.append(
            f"{label}.terrain must be one of: {', '.join(SESSION_TERRAINS)} (got {terrain!r})."
        )
    return errors


def _validate_open_decisions(decisions: Any) -> list[str]:
    if decisions is None:
        return []
    if not isinstance(decisions, list):
        return ["open_decisions must be a list."]
    errors: list[str] = []
    for index, decision in enumerate(decisions, start=1):
        label = f"open_decisions[{index}]"
        if not isinstance(decision, dict):
            errors.append(f"{label} must be an object.")
            continue
        if not str(decision.get("question") or "").strip():
            errors.append(f"{label}.question must be a non-empty string.")
        due = decision.get("due_date")
        if due is not None and not DATE_RE.match(str(due)):
            errors.append(f"{label}.due_date must be YYYY-MM-DD or null.")
        if not isinstance(decision.get("options", []), list):
            errors.append(f"{label}.options must be a list.")
    return errors


def _validate_sessions(agent_name: str, payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    sessions = payload.get("sessions", [])
    if not isinstance(sessions, list) or not sessions:
        return [f"{agent_name} must include a non-empty sessions list."]
    for index, session in enumerate(sessions, start=1):
        if not isinstance(session, dict):
            errors.append(f"Session {index} in {agent_name} must be an object.")
            continue
        missing_session = _missing_keys(session, SESSION_REQUIRED_FIELDS)
        if missing_session:
            errors.append(
                f"Session {index} in {agent_name} is missing: {', '.join(missing_session)}"
            )
        for block_index, block in enumerate(session.get("blocks") or [], start=1):
            if not isinstance(block, dict) or "section" not in block:
                errors.append(
                    f"Session {index} in {agent_name} block {block_index} needs a 'section'."
                )
                continue
            for step_index, step in enumerate(block.get("steps") or [], start=1):
                if not isinstance(step, dict) or "duration" not in step or "target" not in step:
                    errors.append(
                        f"Session {index} in {agent_name} block {block_index} step {step_index} "
                        "needs 'duration' and 'target'."
                    )
        errors.extend(_validate_session_extras(session, f"Session {index} in {agent_name}"))
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
            if session.get("expected_if") is None or session.get("estimated_tss") is None:
                errors.append(
                    f"Session {index} in {agent_name} is missing expected_if or estimated_tss for bike methodology enforcement."
                )
    return errors


def validate_agent_output(agent_name: str, payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return [f"{agent_name} output must be a JSON object."]
    errors: list[str] = []
    required = required_keys(agent_name)
    missing = _missing_keys(payload, required)
    if missing:
        errors.append(f"Missing required keys for {agent_name}: {', '.join(missing)}")
    schema_file = AGENT_SCHEMA_FILES.get(agent_name)
    if schema_file:
        schema = load_schema(schema_file)
        nested = schema_errors(payload, schema, agent_name, skip=_SKIP_GENERIC_PROPERTIES)
        # The top-level missing list is already reported above.
        errors.extend(
            error for error in nested if not error.startswith(f"{agent_name} is missing:")
        )
    if agent_name in {"head_coach_planner", "orchestrator"}:
        errors.extend(_validate_sessions(agent_name, payload))
    if agent_name == "performance_doctor" and not missing:
        errors.extend(
            _enum_error(payload["readiness_status"], READINESS_STATUSES, "readiness_status")
        )
        errors.extend(
            _enum_error(
                payload["load_recommendation"], LOAD_RECOMMENDATIONS, "load_recommendation"
            )
        )
    if agent_name == "macro_coach" and "macro_phase" in payload:
        errors.extend(_enum_error(payload["macro_phase"], MACRO_PHASES, "macro_phase"))
    if agent_name == "macro_coach" and "threshold_tests" in payload:
        errors.extend(
            _validate_threshold_tests(payload["threshold_tests"], "threshold_tests")
        )
    if agent_name == "orchestrator" and "threshold_testing" in payload:
        errors.extend(
            _validate_threshold_updates(
                (payload["threshold_testing"] or {}).get("threshold_updates", []),
                "threshold_testing.threshold_updates",
            )
        )
    if agent_name == "orchestrator":
        macro_cycle = payload.get("macro_cycle_decision")
        if isinstance(macro_cycle, dict) and "phase" in macro_cycle:
            errors.extend(
                _enum_error(macro_cycle["phase"], MACRO_PHASES, "macro_cycle_decision.phase")
            )
        if "readiness_status" in payload:
            errors.extend(
                _enum_error(payload["readiness_status"], READINESS_STATUSES, "readiness_status")
            )
        errors.extend(_validate_open_decisions(payload.get("open_decisions")))
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
