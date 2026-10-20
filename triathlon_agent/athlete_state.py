from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .race_selection import select_priority_race
from .utils import (
    detect_red_flags,
    ensure_dir,
    has_niggle,
    niggle_body_parts,
    parse_date,
    read_json,
    slugify,
    write_json,
)


STATE_DIR_ENV = "TRIATHLON_AGENT_STATE_DIR"
# Older than this (relative to the planning date) the stored block counter and
# phase are re-derived from the fresh snapshot instead of trusted.
STATE_STALE_DAYS = 7
HISTORY_MAX_WEEKS = 104
WEEKLY_ACTUALS_MAX = 104
RACE_RESULTS_MAX = 50
INJURY_LOG_MAX = 100
OPEN_DECISIONS_MAX = 20
# Pain score (0-10) from which a body part counts as a niggle / injury entry.
PAIN_NIGGLE_SCORE = 3

DEFAULT_ATHLETE_STATE: dict[str, Any] = {
    "version": 1,
    "last_updated_at": None,
    "active_macro_cycle": {
        "block_start_date": None,
        "current_block_week": None,
        "cycle_type": None,
        "phase": None,
        # The Intervals race the stored phase was planned against (name|date).
        # Only used to detect that the calendar changed; never a race source.
        "phase_race_key": None,
        "last_updated_from_run": None,
        "override_rationale": None,
    },
    # Informational only: the priority race the last approved plan used, with
    # the run. Races always come from the Intervals calendar
    # (snapshot["races"]); this field never feeds race selection.
    "last_planned_priority_race": None,
    "history": [],
    "weekly_actuals": [],
    "race_results": [],
    "injury_log": [],
    "open_decisions": [],
    "threshold_testing": {"bike": None, "run": None, "history": []},
}


class AthleteStateError(ValueError):
    pass


def athlete_state_path(base_dir: str | Path | None = None) -> Path:
    resolved = base_dir if base_dir is not None else os.environ.get(STATE_DIR_ENV, "data")
    return Path(resolved) / "athlete_state.json"


def backup_path(path: str | Path) -> Path:
    resolved = Path(path)
    return resolved.with_name(resolved.name + ".bak")


def load_athlete_state(path: str | Path | None = None) -> dict[str, Any]:
    """Load the persisted state; a corrupt file raises instead of silently resetting.

    A silent reset would throw away the block counter, history and race results
    and let the next approve overwrite them. The `.bak` copy written before
    every save is the recovery path.
    """
    resolved = Path(path) if path is not None else athlete_state_path()
    if not resolved.exists():
        return deepcopy(DEFAULT_ATHLETE_STATE)
    try:
        payload = read_json(resolved)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AthleteStateError(
            f"{resolved} is not valid JSON ({exc}). Restore it from "
            f"{backup_path(resolved)} or fix it by hand; it was not reset."
        ) from exc
    if not isinstance(payload, dict):
        raise AthleteStateError(
            f"{resolved} must contain a JSON object, got {type(payload).__name__}. "
            f"Restore it from {backup_path(resolved)}; it was not reset."
        )
    state = deepcopy(DEFAULT_ATHLETE_STATE)
    state.update(payload)
    if not isinstance(state.get("active_macro_cycle"), dict):
        state["active_macro_cycle"] = {}
    for key in ("history", "weekly_actuals", "race_results", "injury_log", "open_decisions"):
        if not isinstance(state.get(key), list):
            state[key] = []
    state["active_macro_cycle"] = {
        **DEFAULT_ATHLETE_STATE["active_macro_cycle"],
        **state["active_macro_cycle"],
    }
    return _clean_state(state)


def _monday(value: Any) -> str | None:
    try:
        day = parse_date(str(value)[:10])
    except (TypeError, ValueError):
        return None
    return (day - timedelta(days=day.weekday())).isoformat()


def _clean_state(state: dict[str, Any]) -> dict[str, Any]:
    """Repair legacy state: stored races and duplicate history weeks.

    Legacy `active_macro_cycle.priority_race` is migrated to the informational
    `last_planned_priority_race` and removed from the cycle, so a stale stored
    race can never act as the race source again.
    """
    cycle = state["active_macro_cycle"]
    race = cycle.pop("priority_race", None)
    if isinstance(race, dict) and race:
        if not state.get("last_planned_priority_race"):
            state["last_planned_priority_race"] = {
                **_persisted_race(race),
                "planned_in_run": cycle.get("last_updated_from_run"),
                "migrated_from_legacy_state": True,
            }
        if not cycle.get("phase_race_key"):
            cycle["phase_race_key"] = race_key(race)
    deduped: dict[str, dict[str, Any]] = {}
    undated: list[dict[str, Any]] = []
    for item in state.get("history", []):
        week = _monday(item.get("week_start"))
        if week is None:
            undated.append(item)
            continue
        # Later entries for the same week win (re-approved or revised plans).
        deduped[week] = {**item, "week_start": week}
    state["history"] = undated + [deduped[key] for key in sorted(deduped)]
    return state


def write_athlete_state(path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Atomic write; the previous version is kept as `athlete_state.json.bak`."""
    resolved = Path(path)
    ensure_dir(resolved.parent)
    if resolved.exists():
        backup = backup_path(resolved)
        backup.write_bytes(resolved.read_bytes())
    write_json(resolved, payload)
    return payload


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


def _safe_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return parse_date(str(value))
    except (TypeError, ValueError):
        return None


def race_key(race: dict[str, Any] | None) -> str | None:
    if not isinstance(race, dict) or not race.get("name"):
        return None
    return f"{str(race.get('name')).strip().lower()}|{str(race.get('date') or '')[:10]}"


def state_staleness(state: dict[str, Any], planning_date: date) -> str | None:
    """Why the stored block counter can no longer be trusted, or None when fresh.

    Fresh means the state was updated (approved + pushed) within the last
    week, measured from the later of `last_updated_at` and the end of the last
    planned week in `history`.
    """
    references: list[date] = []
    updated = _safe_date(state.get("last_updated_at"))
    if updated:
        references.append(updated)
    for item in state.get("history") or []:
        week = _safe_date(item.get("week_start"))
        if week:
            references.append(week + timedelta(days=6))
    if not references:
        return "the state was never updated by an approved push"
    age = (planning_date - max(references)).days
    if age > STATE_STALE_DAYS:
        return f"the state was last updated {age} days before the planning date"
    return None


def _phase_mismatch(
    cycle: dict[str, Any], current_race: dict[str, Any] | None, planning_day: date | None
) -> str | None:
    """Why the stored phase does not belong to the current Intervals priority race."""
    if not cycle.get("phase"):
        return None
    current_key = race_key(current_race)
    if current_key is None:
        if cycle.get("phase") in {"race_specific", "taper"}:
            return "no priority race is on the Intervals calendar any more"
        return None
    if cycle.get("phase_race_key") != current_key:
        return (
            f"the Intervals priority race is {current_race.get('name')} "
            f"({current_race.get('date')}), not the race the stored phase was planned for"
        )
    race_day = _safe_date(current_race.get("date"))
    if planning_day and race_day and race_day < planning_day:
        return "the race the stored phase was planned for is in the past"
    return None


def bootstrap_athlete_state(
    snapshot: dict[str, Any], state: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Derive the active cycle for this run from the fresh snapshot.

    Races always come from the Intervals calendar (`snapshot["races"]` via
    `select_priority_race`), never from stored state. The stored phase
    survives only when the state is fresh and was planned against the same
    Intervals race; the block counter survives only while the state is fresh.
    """
    current = deepcopy(state if state is not None else load_athlete_state())
    cycle = current.setdefault("active_macro_cycle", {})
    cycle.pop("priority_race", None)
    planning_day = _safe_date(snapshot.get("planning_date"))
    priority_race = select_priority_race(snapshot)
    stale_reason = (
        state_staleness(current, planning_day)
        if planning_day and (cycle.get("phase") or cycle.get("current_block_week"))
        else None
    )
    phase_reason = stale_reason or _phase_mismatch(cycle, priority_race, planning_day)
    current["current_priority_race"] = (
        {
            key: priority_race.get(key)
            for key in ("name", "date", "priority", "distance", "source")
        }
        if priority_race
        else None
    )
    if (
        cycle.get("phase")
        and cycle.get("cycle_type")
        and cycle.get("current_block_week")
        and not phase_reason
    ):
        return current

    periodization_state = snapshot.get("periodization_state", {})
    active = {
        "block_start_date": snapshot.get("week_start"),
        "current_block_week": periodization_state.get("next_block_week"),
        "cycle_type": periodization_state.get("recommended_next_week_type"),
        # Re-derived by the macro coach from the Intervals races; a stale or
        # race-mismatched phase (race_specific included) is never kept.
        "phase": None,
        "phase_race_key": race_key(priority_race),
        "last_updated_from_run": None,
        "override_rationale": None,
    }
    if not stale_reason and cycle.get("current_block_week") and cycle.get("cycle_type"):
        # Fresh state, only the race changed: keep the load/recovery rhythm.
        active["block_start_date"] = cycle.get("block_start_date") or active["block_start_date"]
        active["current_block_week"] = cycle.get("current_block_week")
        active["cycle_type"] = cycle.get("cycle_type")
    if phase_reason:
        current["stale_reset"] = {
            "reason": phase_reason,
            "previous_phase": cycle.get("phase"),
            "previous_block_week": cycle.get("current_block_week"),
            "previous_cycle_type": cycle.get("cycle_type"),
        }
    current["active_macro_cycle"] = {**cycle, **active}
    return current


# Fields copied from the race calendar into the persisted priority race.
# `days_to_race` is deliberately excluded: it is relative to one planning date
# and goes stale the moment it is written.
PERSISTED_RACE_FIELDS = ("name", "date", "priority", "distance", "source", "notes", "tags")


def _plan_week_start(final_plan: dict[str, Any]) -> str | None:
    """Monday of the planned week, so history entries line up week by week."""
    dates = sorted(
        str(session.get("date"))
        for session in final_plan.get("sessions", [])
        if session.get("date")
    )
    if not dates:
        return None
    first = parse_date(dates[0])
    return (first - timedelta(days=first.weekday())).isoformat()


def plan_week_start(final_plan: dict[str, Any]) -> str | None:
    return _plan_week_start(final_plan)


def _persisted_race(race: dict[str, Any]) -> dict[str, Any]:
    return {key: race.get(key) for key in PERSISTED_RACE_FIELDS if key in race}


def update_athlete_state_from_plan(
    state: dict[str, Any],
    final_plan: dict[str, Any],
    run_dir: str | Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    updated = deepcopy(state)
    cycle = updated.setdefault("active_macro_cycle", {})
    macro = final_plan.get("macro_cycle_decision", {})
    race = final_plan.get("race_notes", {}).get("priority_race") or {}
    week_start = _plan_week_start(final_plan)
    new_phase = macro.get("phase", cycle.get("phase"))
    new_block_week = macro.get("friel_block_week", cycle.get("current_block_week"))
    # A block starts when the block counter resets to week 1, the phase changes,
    # or no block was recorded yet. Otherwise the start date is kept, so
    # `block_start_date` really is the first week of the current block.
    starts_new_block = (
        not cycle.get("block_start_date")
        or str(new_block_week) == "1"
        or new_phase != cycle.get("phase")
    )
    cycle.update(
        {
            "block_start_date": week_start
            if starts_new_block and week_start
            else cycle.get("block_start_date"),
            "current_block_week": new_block_week,
            "cycle_type": macro.get("friel_cycle_type", cycle.get("cycle_type")),
            "phase": new_phase,
            "phase_race_key": race_key(race),
            "last_updated_from_run": str(run_dir),
            "override_rationale": macro.get("override_rationale"),
        }
    )
    cycle.pop("priority_race", None)
    updated["last_updated_at"] = _now_iso(now)
    # Informational record of the race this plan used; never a race source.
    updated["last_planned_priority_race"] = (
        {
            **_persisted_race(race),
            "planned_in_run": str(run_dir),
            "recorded_at": updated["last_updated_at"],
        }
        if race
        else None
    )
    updated.pop("stale_reset", None)
    updated.pop("current_priority_race", None)
    if final_plan.get("adjustment"):
        # A mid-week adjustment revises the remaining days; the week's history
        # entry keeps describing the whole week.
        existing = next(
            (item for item in updated.get("history", []) if item.get("week_start") == week_start),
            None,
        )
        if existing:
            existing["adjusted_from"] = (final_plan.get("adjustment") or {}).get("from_date")
            existing["source_run"] = str(run_dir)
            return updated
    entry = {
        "source_run": str(run_dir),
        "week_start": week_start,
        "classification": macro.get("classification"),
        "phase": macro.get("phase"),
        "friel_block_week": new_block_week,
        "readiness_status": final_plan.get("readiness_status"),
        "planned_minutes": sum(
            int(session.get("duration_min") or 0)
            for session in final_plan.get("sessions", [])
        ),
        "planned_load": round(
            sum(
                float(session.get("estimated_load") or 0)
                for session in final_plan.get("sessions", [])
            ),
            1,
        ),
    }
    # Re-approving or revising a week replaces its entry instead of appending a
    # duplicate, so the history stays one row per planned week.
    history = [
        item
        for item in updated.get("history", [])
        if not week_start or item.get("week_start") != week_start
    ]
    history.append(entry)
    history.sort(key=lambda item: str(item.get("week_start") or ""))
    updated["history"] = history[-HISTORY_MAX_WEEKS:]
    return updated


# --- Long-term memory -------------------------------------------------------


ILLNESS_TEXT_RE = re.compile(
    r"\b(sick|ill|illness|cold|flu|covid|corona|fever|ziek|verkoud\w*|griep|koorts|infection|infectie)\b"
)


def _pain_scores(checkin: dict[str, Any]) -> dict[str, float]:
    scores: dict[str, float] = {}
    raw = checkin.get("pain_scores") or {}
    if not isinstance(raw, dict):
        return scores
    for part, value in raw.items():
        try:
            scores[str(part)] = float(value)
        except (TypeError, ValueError):
            continue
    return scores


def checkin_niggle_parts(checkin: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    text = checkin.get("pain_or_niggles")
    if has_niggle(text):
        parts.extend(niggle_body_parts(text) or ["unspecified"])
    for part, score in _pain_scores(checkin).items():
        if score >= PAIN_NIGGLE_SCORE and part not in parts:
            parts.append(part)
    return parts


def checkin_reports_illness(checkin: dict[str, Any]) -> bool:
    """Illness or red flags reported in a check-in (symptoms, missed-session reasons)."""
    symptoms = checkin.get("illness_symptoms")
    if isinstance(symptoms, list) and any(str(item).strip() for item in symptoms):
        return True
    if isinstance(symptoms, str) and symptoms.strip() and has_niggle(symptoms):
        return True
    flags = checkin.get("red_flags")
    if isinstance(flags, list) and any(str(item).strip() for item in flags):
        return True
    reasons = checkin.get("missed_session_reasons") or {}
    reason_texts = reasons.values() if isinstance(reasons, dict) else reasons
    for reason in reason_texts if isinstance(reason_texts, (list, type({}.values()))) else []:
        if ILLNESS_TEXT_RE.search(str(reason).lower()):
            return True
    for field in ("pain_or_niggles", "unexpected_fatigue", "athlete_notes"):
        if detect_red_flags(checkin.get(field)):
            return True
    return False


def _cap(items: list[dict[str, Any]], limit: int, key: str = "date") -> list[dict[str, Any]]:
    items.sort(key=lambda item: str(item.get(key) or ""))
    return items[-limit:]


def record_week_actuals(
    state: dict[str, Any],
    adherence: dict[str, Any],
    checkin: dict[str, Any],
    *,
    health_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Append planned-vs-completed for the week the adherence review covers."""
    updated = deepcopy(state)
    if not adherence or adherence.get("status") != "available":
        return updated
    week_start = _monday(adherence.get("planned_week_start"))
    if not week_start:
        return updated
    reviews = adherence.get("session_reviews") or []
    planned_min = sum(float(item.get("planned_minutes") or 0) for item in reviews)
    completed_min = sum(float(item.get("actual_minutes") or 0) for item in reviews)
    key_done = [
        item.get("session_name")
        for item in reviews
        if item.get("priority") == "key" and str(item.get("status", "")).startswith("completed")
    ]
    week_end = parse_date(week_start) + timedelta(days=6)
    illness = checkin_reports_illness(checkin)
    for event in health_events or []:
        if event.get("kind") != "sick":
            continue
        start = _safe_date(event.get("start_date"))
        end = _safe_date(event.get("end_date")) or start
        if start and end and start <= week_end and end >= parse_date(week_start):
            illness = True
    entry = {
        "week_start": week_start,
        "source_run": adherence.get("source_run"),
        "planned_min": int(round(planned_min)),
        "completed_min": int(round(completed_min)),
        "key_sessions_planned": int(adherence.get("key_sessions_planned") or 0),
        "key_sessions_done": key_done,
        "adherence_pct": round(float(adherence.get("adherence_score") or 0.0) * 100, 1),
        "missed_sessions": list(adherence.get("missed_sessions") or []),
        "niggles": checkin_niggle_parts(checkin),
        "illness": illness,
    }
    actuals = [
        item for item in updated.get("weekly_actuals", []) if item.get("week_start") != week_start
    ]
    actuals.append(entry)
    updated["weekly_actuals"] = _cap(actuals, WEEKLY_ACTUALS_MAX, key="week_start")
    return updated


def _format_duration(seconds: Any) -> str | None:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return None
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def capture_race_results(
    state: dict[str, Any],
    snapshot: dict[str, Any],
    checkin: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Store finished races: calendar races in the past matched to an activity.

    A `race_debrief` from the check-in is attached to the most recent race
    (or stored on its own when no race activity is known).
    """
    updated = deepcopy(state)
    results = list(updated.get("race_results") or [])
    known = {(item.get("date"), item.get("name")) for item in results}
    planning_text = snapshot.get("planning_date")
    planning_day = _safe_date(planning_text)
    activities = ((snapshot.get("intervals") or {}).get("activities")) or []
    for race in snapshot.get("races") or []:
        race_day = _safe_date(race.get("date"))
        if race_day is None or planning_day is None or race_day > planning_day:
            continue
        if (planning_day - race_day).days > 28:
            continue
        same_day = [
            activity
            for activity in activities
            if str(activity.get("start_date_local") or "")[:10] == race_day.isoformat()
        ]
        if not same_day:
            continue
        main = max(same_day, key=lambda item: float(item.get("moving_time") or 0))
        key = (race_day.isoformat(), race.get("name"))
        if key in known:
            continue
        known.add(key)
        distance_m = main.get("distance")
        results.append(
            {
                "date": race_day.isoformat(),
                "name": race.get("name"),
                "distance": race.get("distance")
                or (f"{float(distance_m) / 1000:.1f} km" if distance_m else None),
                "time": _format_duration(main.get("elapsed_time") or main.get("moving_time")),
                "priority": race.get("priority"),
                "notes": race.get("notes") or "",
                "source": "intervals_activity",
            }
        )
    debrief = str((checkin or {}).get("race_debrief") or "").strip()
    if debrief:
        if results:
            latest = max(results, key=lambda item: str(item.get("date") or ""))
            notes = str(latest.get("notes") or "")
            if debrief not in notes:
                latest["debrief"] = debrief
        else:
            results.append(
                {
                    "date": planning_text,
                    "name": "Race debrief",
                    "distance": None,
                    "time": None,
                    "priority": None,
                    "notes": "",
                    "debrief": debrief,
                    "source": "checkin",
                }
            )
    updated["race_results"] = _cap(results, RACE_RESULTS_MAX)
    return updated


def log_injuries(
    state: dict[str, Any],
    snapshot: dict[str, Any],
    checkin: dict[str, Any],
) -> dict[str, Any]:
    """Append body-part complaints from check-in, pain scores and activity notes."""
    updated = deepcopy(state)
    log = list(updated.get("injury_log") or [])
    seen = {(item.get("date"), item.get("body_part"), item.get("source")) for item in log}
    # The day the complaint was reported, not a planning date ahead of it.
    planning_text = str(
        snapshot.get("data_as_of")
        or snapshot.get("planning_date")
        or checkin.get("planning_date")
        or ""
    )

    def add(day: str, part: str, severity: Any, source: str) -> None:
        key = (day, part, source)
        if not day or key in seen:
            return
        seen.add(key)
        log.append({"date": day, "body_part": part, "severity": severity, "source": source})

    text = checkin.get("pain_or_niggles")
    if has_niggle(text):
        for part in niggle_body_parts(text) or ["unspecified"]:
            add(planning_text, part, "reported", "checkin")
    for part, score in _pain_scores(checkin).items():
        if score >= PAIN_NIGGLE_SCORE:
            add(planning_text, part, score, "checkin_pain_score")
    for flag in snapshot.get("activity_note_flags") or []:
        if flag.get("kind") == "pain":
            add(
                str(flag.get("date") or "")[:10],
                flag.get("body_part") or "unspecified",
                "reported",
                "activity_note",
            )
    for event in snapshot.get("recent_health_events") or []:
        if event.get("kind") == "injured":
            add(
                str(event.get("start_date") or "")[:10],
                event.get("description") or event.get("name") or "unspecified",
                event.get("severity") or "logged",
                "intervals_injury_note",
            )
    updated["injury_log"] = _cap(log, INJURY_LOG_MAX)
    return updated


# --- Open decisions -----------------------------------------------------------


def decision_id(question: str) -> str:
    return slugify(question)[:60]


def store_open_decisions(
    state: dict[str, Any],
    final_plan: dict[str, Any],
    run_dir: str | Path,
) -> dict[str, Any]:
    """Carry the plan's open decisions forward until they are answered."""
    updated = deepcopy(state)
    stored = list(updated.get("open_decisions") or [])
    by_id = {item.get("id"): item for item in stored}
    for decision in final_plan.get("open_decisions") or []:
        question = str(decision.get("question") or "").strip()
        if not question:
            continue
        identifier = decision_id(question)
        existing = by_id.get(identifier)
        if existing and existing.get("status") != "open":
            continue
        record = {
            "id": identifier,
            "question": question,
            "due_date": decision.get("due_date"),
            "options": list(decision.get("options") or []),
            "default_if_no_answer": decision.get("default_if_no_answer"),
            "status": "open",
            "raised_in_run": existing.get("raised_in_run") if existing else str(run_dir),
            "answer": None,
        }
        by_id[identifier] = record
    updated["open_decisions"] = list(by_id.values())[-OPEN_DECISIONS_MAX:]
    return updated


def resolve_open_decisions(
    state: dict[str, Any],
    checkin: dict[str, Any],
    today: str | None = None,
) -> dict[str, Any]:
    """Apply check-in `decision_answers` and default overdue decisions."""
    updated = deepcopy(state)
    answers = checkin.get("decision_answers") or {}
    if not isinstance(answers, dict):
        answers = {}
    normalized = {decision_id(str(key)): value for key, value in answers.items()}
    normalized.update({str(key): value for key, value in answers.items()})
    today_day = _safe_date(today) if today else None
    for decision in updated.get("open_decisions") or []:
        if decision.get("status") != "open":
            continue
        answer = normalized.get(decision.get("id")) or normalized.get(
            decision_id(str(decision.get("question") or ""))
        )
        if answer not in (None, ""):
            decision["status"] = "answered"
            decision["answer"] = answer
            continue
        due = _safe_date(decision.get("due_date"))
        if today_day and due and due < today_day:
            decision["status"] = "defaulted"
            decision["answer"] = decision.get("default_if_no_answer")
    return updated


def pending_open_decisions(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item for item in state.get("open_decisions") or [] if item.get("status") == "open"
    ]
