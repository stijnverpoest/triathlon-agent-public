from __future__ import annotations

import re
from typing import Any

_ZONE_TO_PACE: dict[str, str] = {
    "1": "70-75% Pace",
    "2": "75% Pace",
    "3": "85% Pace",
    "4": "100% Pace",
    "5": "105% Pace",
}


ALLOWED_SECTION_PREFIXES = (
    "Warmup",
    "Main Set",
    "Cooldown",
    "Finish",
    "Race Rhythm",
    "Reset",
    # Additional section names used by the planner
    "Strides",
    "Steady Finish",
    "Build Block",
    "Fueling Block",
    "Openers",
    "Easy Run",
    # Run section names
    "Easy Aerobic Run",
    "Long Aerobic Run",
    "Threshold Repeats",
    "Club Session",
    "Brick Run",
    "Race Effort",
    # Bike section names
    "Sweet Spot Intervals",
    "Low-Cadence Strength Blocks",
    "Aerobic Base",
    "Race-Rhythm Blocks",
    "Tempo Flush",
    "Easy Spin",
    # Swim section names
    "Easy Swim",
    # Threshold test section names
    "Blowout",
    "Recovery Spin",
    "Test Effort",
)


# Plain-text coaching lines the compiler puts above the structured steps.
# They never start with "-" and carry no duration/target token, so the
# Intervals parser treats them as description text, not as steps.
COACH_TEXT_PREFIXES = (
    "Why:",
    "Feel:",
    "RPE target:",
    "Bail out:",
    "Fuel:",
    "Terrain:",
)
# A duration token (10m, 90s, 1h, 1h30m, 400mtr) or a target token
# (Z2, 85%, 4:30/km, 250w, 140bpm, 90rpm) in a plain-text line could be
# parsed by Intervals; coaching text must contain neither.
DURATION_TOKEN_RE = re.compile(
    r"(?<![\w.])\d+(?:\.\d+)?\s?(?:h|m|s|min|mins|sec|secs|km|mtr)(?![\w])", re.IGNORECASE
)
TARGET_TOKEN_RE = re.compile(
    r"\bZ\d\b|\d\s?%|\d+:\d{2}\s?/\s?(?:km|mi|100m)|\b\d+\s?(?:w|bpm|rpm)\b", re.IGNORECASE
)


def looks_like_step(line: str) -> bool:
    """True when a text line could be read as a workout step by Intervals."""
    text = line.strip()
    if text.startswith("-"):
        return True
    return bool(DURATION_TOKEN_RE.search(text) or TARGET_TOKEN_RE.search(text))


def is_coach_text_line(line: str) -> bool:
    text = line.strip()
    return text.startswith(COACH_TEXT_PREFIXES) and not looks_like_step(text)


def _normalize_step_line(line: str) -> str:
    line = line.strip()
    if not line:
        return ""
    if is_coach_text_line(line):
        return line
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
            # Fix bare zone labels: @Z1, Z2, Z1-Z2 -> Z1 HR, Z2 HR, Z1-Z2 HR
            def _add_hr(m: re.Match) -> str:
                return f"{m.group(1).lstrip('@')} HR"
            fallback = re.sub(r"@?(Z\d+(?:-Z\d+)?)(?!\s*HR)", _add_hr, fallback)
        elif discipline == "Run" and primary_metric == "Pace":
            # Fix bare zone labels: @Z1, Z2 -> % Pace equivalent
            def _zone_to_pace(m: re.Match) -> str:
                upper = m.group(2) or m.group(1)
                return _ZONE_TO_PACE.get(upper, f"Z{upper} Pace")
            fallback = re.sub(r"@?Z(\d+)(?:-Z(\d+))?(?!\s*(?:Pace|HR|\d))", _zone_to_pace, fallback)
            # If still no "Pace" anywhere, append it to each step target line
            if "Pace" not in fallback:
                lines = []
                for line in fallback.splitlines():
                    if line.startswith("-") and "Pace" not in line and "HR" not in line:
                        lines.append(f"{line} Pace")
                    else:
                        lines.append(line)
                fallback = "\n".join(lines)
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
