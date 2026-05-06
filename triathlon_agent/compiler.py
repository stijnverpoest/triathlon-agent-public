from __future__ import annotations

import re
from typing import Any

from .utils import slugify

# Matches coaching-label annotations that should not appear in Intervals.icu targets.
# e.g. "T-pace", "I-pace", "E-pace", "R-pace" are Jack Daniels labels for the coach,
# not a metric Intervals.icu can parse.
_COACHING_LABEL_RE = re.compile(r"\b[TIER]-pace\b", re.IGNORECASE)


def _normalize_duration(duration: str) -> str:
    """Convert agent duration strings to Intervals.icu format.

    Examples: "15min" -> "15m", "90s" stays "90s", "8x200m" stays "8x200m".
    """
    # "15min" / "15 min" -> "15m"  (only bare minute suffixes, not "/min")
    result = re.sub(r"(?<!\d)(\d+)\s*min\b", r"\1m", duration)
    # "30sec" / "30 sec" -> "30s"
    result = re.sub(r"(\d+)\s*sec\b", r"\1s", result)
    return result.strip()


def _clean_run_target(target: str) -> str:
    """Strip coaching labels from run targets so Intervals.icu can parse the pace value.

    "4:05/km T-pace" -> "4:05/km"
    """
    cleaned = _COACHING_LABEL_RE.sub("", target)
    return " ".join(cleaned.split())


def _render_section(
    section: dict[str, Any], discipline: str, primary_metric: str
) -> list[str]:
    repeat = section.get("repeat")
    # Only annotate with "Nx" when the block actually repeats (>1).
    # repeat==1 is implicit and "1x" on a Warmup header confuses Intervals.icu.
    if repeat and repeat > 1:
        header = f"{section['section']} {repeat}x"
    else:
        header = section["section"]
    lines = [header]

    for step in section.get("steps", []):
        duration = _normalize_duration(step["duration"])
        target = step["target"]
        cadence = step.get("cadence")

        if discipline == "WeightTraining":
            lines.append(f"- {duration} {target}".strip())
            continue

        target_text = target
        if discipline == "Run":
            target_text = _clean_run_target(target_text)

        line = f"- {duration} {target_text}".strip()
        if (
            discipline == "Run"
            and primary_metric in {"HR", "Pace"}
            and primary_metric not in line
        ):
            line = f"{line} {primary_metric}".strip()
        if cadence:
            line = f"{line} {cadence}".strip()
        lines.append(line)
    return lines


def compile_session_to_event(session: dict[str, Any], run_id: str) -> dict[str, Any]:
    if session["discipline"] == "Recovery":
        raise ValueError("Recovery sessions are not pushed to Intervals.")
    custom_description = session.get("intervals_description")
    if custom_description is not None:
        description = custom_description
    elif session.get("intervals_placeholder"):
        description = ""
    else:
        lines = []
        for section in session.get("blocks", []):
            if lines:
                lines.append("")
            lines.extend(
                _render_section(
                    section,
                    session["discipline"],
                    session.get("primary_metric", "notes"),
                )
            )
        description = "\n".join(lines).strip()

    event_type = session["discipline"]
    if event_type == "Ride":
        event_type = "Ride"
    elif event_type == "Run":
        event_type = "Run"
    elif event_type == "WeightTraining":
        event_type = "WeightTraining"
    elif event_type == "Swim":
        event_type = "Swim"

    return {
        "category": "WORKOUT",
        "start_date_local": f"{session['date']}T00:00:00",
        "type": event_type,
        "name": session["session_name"],
        "description": description,
        "moving_time": int(session["duration_min"]) * 60,
        "external_id": session.get(
            "external_id",
            f"coach-{session['date']}-{slugify(session['discipline'])}-{slugify(session['session_name'])}",
        ),
        "primary_metric": session.get("primary_metric", "notes"),
        "session_objective": session["objective"],
    }


def compile_plan(final_plan: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    workouts = []
    for session in final_plan.get("sessions", []):
        if session["discipline"] == "Recovery":
            continue
        if session.get("push_to_intervals") is False:
            continue
        workouts.append(compile_session_to_event(session, run_id))
    return workouts


def compile_nutrition_notes(
    nutrition_summary: dict[str, Any], run_id: str
) -> list[dict[str, Any]]:
    return compile_note_events(nutrition_summary.get("daily_notes", []), run_id=run_id)


def compile_note_events(
    notes_payload: list[dict[str, Any]], run_id: str
) -> list[dict[str, Any]]:
    del run_id
    notes = []
    for note in notes_payload:
        if not note.get("push_to_intervals"):
            continue
        body = note.get("body", "").strip()
        if not body:
            continue
        notes.append(
            {
                "category": "NOTE",
                "show_as_note": True,
                "start_date_local": f"{note['date']}T00:00:00",
                "name": note.get("title", "Nutrition Targets"),
                "description": body,
                "external_id": note.get(
                    "external_id",
                    f"coach-{note['date']}-note-{slugify(note.get('title', 'nutrition'))}",
                ),
            }
        )
    return notes
