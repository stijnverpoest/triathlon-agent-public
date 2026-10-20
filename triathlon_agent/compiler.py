from __future__ import annotations

import re
from typing import Any

from .agents import syntax_guard
from .utils import slugify

# Matches coaching-label annotations that should not appear in Intervals.icu targets.
# e.g. "T-pace", "I-pace", "E-pace", "R-pace" are Jack Daniels labels for the coach,
# not a metric Intervals.icu can parse.
_COACHING_LABEL_RE = re.compile(r"\b[TIER]-pace\b", re.IGNORECASE)

# Zone label -> % Pace for Pace-metric runs (upper bound of any range is used).
_ZONE_TO_PACE: dict[str, str] = {
    "1": "70-75% Pace",
    "2": "75% Pace",
    "3": "85% Pace",
    "4": "100% Pace",
    "5": "105% Pace",
}


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


def _normalize_run_target(target: str, primary_metric: str) -> str:
    """Normalise run step targets to the exact format Intervals.icu expects.

    HR runs  (primary_metric == "HR"):
        "@Z1"     -> "Z1 HR"
        "@Z1-Z2"  -> "Z1-Z2 HR"
        "Z2"      -> "Z2 HR"

    Pace runs (primary_metric == "Pace"):
        "@Z1"           -> "70-75% Pace"   (zone -> % Pace via lookup)
        "@Z1-Z2"        -> "75% Pace"      (upper bound zone used)
        "@4:00-4:10/km" -> "4:00-4:10/km"  (absolute pace: strip @ only)
        "100% Pace"     -> "100% Pace"     (already correct: unchanged)
    """
    # Strip leading @ in all cases
    target = target.lstrip("@").strip()

    if primary_metric == "HR":
        # Add " HR" suffix to bare zone labels (Z1, Z2, Z1-Z2, etc.)
        if re.match(r"Z\d", target, re.IGNORECASE) and "HR" not in target:
            return f"{target} HR"

    elif primary_metric == "Pace":
        # Already correct format
        if "% Pace" in target or "%Pace" in target:
            return target
        # Zone label -> % Pace
        m = re.fullmatch(r"Z(\d+)(?:-Z(\d+))?", target, re.IGNORECASE)
        if m:
            upper = m.group(2) or m.group(1)
            return _ZONE_TO_PACE.get(upper, f"{target} Pace")
        # Absolute pace value (4:30/km etc.) — strip @ already done, leave as-is

    return target


def _fix_ftp_target(target: str) -> str:
    """Normalise bike power targets to the format Intervals.icu expects.

    "@82-88%FTP" -> "82-88% FTP"
    "88-92%FTP"  -> "88-92% FTP"
    "88% FTP"    -> "88% FTP"  (unchanged)
    """
    return re.sub(r"@?([\d][\d\-]*)\s*%\s*FTP", r"\1% FTP", target)


def _render_section(
    section: dict[str, Any], discipline: str, primary_metric: str
) -> list[str]:
    repeat = section.get("repeat")
    # Only annotate with "Nx" when the block actually repeats (>1).
    # repeat==1 is implicit and "1x" on a Warmup header confuses Intervals.icu.
    if repeat and repeat > 1:
        header = f"{repeat}x"
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
            target_text = _normalize_run_target(target_text, primary_metric)
        elif discipline == "Ride":
            target_text = _fix_ftp_target(target_text)

        line = f"- {duration} {target_text}".strip()
        if cadence:
            line = f"{line} {cadence}".strip()
        lines.append(line)
    return lines


_UNIT_WORDS = {
    "h": "hours",
    "s": "seconds",
    "sec": "seconds",
    "secs": "seconds",
    "min": "minutes",
    "mins": "minutes",
    "km": "kilometres",
    "mtr": "metres",
}
_TEXT_DURATION_RE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s?(h|m|s|min|mins|sec|secs|km|mtr)(?![\w])", re.IGNORECASE
)
PREAMBLE_MAX_CHARS = 220


def _spell_duration(match: re.Match[str]) -> str:
    number, unit = match.group(1), match.group(2).lower()
    if unit == "m":
        word = "metres" if float(number) >= 100 else "minutes"
    else:
        word = _UNIT_WORDS.get(unit, unit)
    return f"{number} {word}"


def coach_text_line(label: str, text: Any) -> str | None:
    """One plain-text line Intervals will not parse as a workout step.

    Leading dashes/bullets are dropped and duration / target tokens are
    spelled out ("10m" -> "10 minutes", "Z2" -> "zone 2", "85%" -> "85 percent"),
    because Intervals reads tokens like "1073m" in free text as a step.
    """
    clean = " ".join(str(text or "").split()).lstrip("-*• ").strip()
    if not clean:
        return None
    clean = re.sub(r"\bZ(\d)\b", r"zone \1", clean, flags=re.IGNORECASE)
    clean = re.sub(r"(\d)\s?%", r"\1 percent", clean)
    clean = re.sub(r"(\d+:\d{2})\s?/\s?(km|mi|100m)", r"\1 per \2", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\b(\d+)\s?w\b", r"\1 watts", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\b(\d+)\s?bpm\b", r"\1 beats per minute", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\b(\d+)\s?rpm\b", r"\1 revs per minute", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\bper (km|mi|100m)\b", lambda m: "per " + {"km": "kilometre", "mi": "mile", "100m": "100 metres"}[m.group(1).lower()], clean, flags=re.IGNORECASE)
    clean = _TEXT_DURATION_RE.sub(_spell_duration, clean)
    if len(clean) > PREAMBLE_MAX_CHARS:
        clean = clean[: PREAMBLE_MAX_CHARS - 3].rstrip() + "..."
    line = f"{label} {clean}"
    if syntax_guard.looks_like_step(line):
        return None
    return line


def session_preamble(session: dict[str, Any]) -> list[str]:
    """Purpose, feel, RPE, bail-out rule and fuelling as text above the steps."""
    lines = [
        coach_text_line("Why:", session.get("objective")),
        coach_text_line("Feel:", session.get("feel_cue")),
        coach_text_line("RPE target:", session.get("rpe_target")),
        coach_text_line("Bail out:", session.get("bail_out_rule")),
    ]
    fuel = session.get("fuelling")
    if not fuel:
        guidance = (session.get("nutrition") or {}).get("guidance") or {}
        during = str(guidance.get("during") or "")
        if during and "carbohydrate/hour" in during:
            fuel = during.split(";")[0].split(". ")[0]
    lines.append(coach_text_line("Fuel:", fuel))
    if session.get("terrain") or session.get("elevation_gain_m"):
        bits = []
        if session.get("terrain"):
            bits.append(str(session["terrain"]).replace("_", " "))
        if session.get("elevation_gain_m"):
            bits.append(f"about {int(round(float(session['elevation_gain_m'])))} metres of climbing")
        if session.get("elevation_loss_m"):
            bits.append(f"{int(round(float(session['elevation_loss_m'])))} metres of descent")
        lines.append(coach_text_line("Terrain:", ", ".join(bits)))
    return [line for line in lines if line]


def compile_session_to_event(session: dict[str, Any], run_id: str) -> dict[str, Any]:
    if session["discipline"] == "Recovery":
        raise ValueError("Recovery sessions are not pushed to Intervals.")
    custom_description = session.get("intervals_description")
    if custom_description is not None:
        description = custom_description
        preamble = session_preamble(session) if custom_description.strip() else []
        if preamble and "Why:" not in custom_description:
            description = "\n".join(preamble) + "\n\n" + custom_description.strip()
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
        steps_text = "\n".join(lines).strip()
        preamble = session_preamble(session)
        description = (
            "\n".join(preamble) + ("\n\n" + steps_text if steps_text else "")
            if preamble
            else steps_text
        )

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
