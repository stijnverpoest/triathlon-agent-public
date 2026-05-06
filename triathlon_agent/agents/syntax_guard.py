from __future__ import annotations

from typing import Any


ALLOWED_SECTION_PREFIXES = (
    "Warmup",
    "Main Set",
    "Cooldown",
    "Finish",
    "Race Rhythm",
    "Reset",
    # Additional section names used in session_library
    "Strides",
    "Steady Finish",
    "Build Block",
    "Fueling Block",
    "Openers",
    "Easy Run",
)


def _normalize_step_line(line: str) -> str:
    line = line.strip()
    if not line:
        return ""
    if line.startswith("-"):
        return f"- {line[1:].strip()}"
    if line.endswith("x") and line[:-1].strip().isdigit():
        return line
    if any(line.startswith(prefix) for prefix in ALLOWED_SECTION_PREFIXES):
        return line
    return f"- {line}"


def _validate_metric(
    discipline: str, primary_metric: str, description: str
) -> tuple[bool, str | None]:
    # Only require % when the session is explicitly power-controlled.
    # HR-primary rides (e.g. outdoor rides without a power meter) are valid and
    # should not have 65% dummy targets injected.
    if discipline == "Ride" and primary_metric == "%FTP" and "%" not in description:
        return False, "Ride workout missing %FTP-style targets."
    if discipline == "Run" and primary_metric == "HR" and "HR" not in description:
        return False, "Easy run missing HR targets."
    if discipline == "Run" and primary_metric == "Pace" and "Pace" not in description:
        return False, "Quality run missing Pace targets."
    return True, None


def review(workout: dict[str, Any]) -> dict[str, Any]:
    discipline = workout["type"]
    primary_metric = workout.get("primary_metric", "notes")
    description = workout.get("description", "")
    normalized_lines = [_normalize_step_line(line) for line in description.splitlines()]
    normalized_description = "\n".join(
        line for line in normalized_lines if line is not None
    ).strip()
    valid, reason = _validate_metric(discipline, primary_metric, normalized_description)

    status = "valid"
    intent = "exact"
    rewrite_reason = ""
    if normalized_description != description.strip():
        status = "rewritten"
        intent = "close"
        rewrite_reason = (
            "Normalized section and step formatting for Intervals parser safety."
        )
    if not valid:
        fallback = normalized_description
        if discipline == "Run" and primary_metric == "HR":
            fallback = normalized_description.replace("Pace", "HR")
        elif discipline == "Run" and primary_metric == "Pace":
            fallback = normalized_description.replace("HR", "Pace")
        elif discipline == "Ride" and "%" not in normalized_description:
            lines = []
            for line in normalized_description.splitlines():
                if line.startswith("-") and "%" not in line:
                    lines.append(f"{line} 65%")
                else:
                    lines.append(line)
            fallback = "\n".join(lines)
        normalized_description = fallback
        status = "rewritten"
        intent = "close"
        rewrite_reason = (
            reason or "Adjusted metric syntax to keep the workout parseable."
        )

    return {
        "status": status,
        "intent_fidelity": intent,
        "final_description": normalized_description,
        "rewrite_reason": rewrite_reason,
        "push_ready": True,
    }
