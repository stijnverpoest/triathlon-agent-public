from __future__ import annotations

from copy import deepcopy
from typing import Any


# Every optional check-in field. null means "unknown / not answered" and is
# never read as "fine".
CHECKIN_DEFAULTS: dict[str, Any] = {
    "last_week_summary": None,
    "missed_sessions": [],
    "missed_session_reasons": None,
    "unexpected_fatigue": None,
    "open_water_mode": False,
    "time_constraints": [],
    "travel_this_week": False,
    "current_motivation": None,
    "subjective_readiness": None,
    "pain_or_niggles": None,
    "pain_scores": None,
    "illness_symptoms": None,
    "red_flags": None,
    "sleep_quality": None,
    "work_stress": None,
    "athlete_notes": None,
    "upcoming_events": None,
    "race_debrief": None,
    "available_hours_override_min": None,
    "available_hours_hard_cap_min": None,
    "double_session_capacity_by_day": {},
    "time_caps_by_day": {},
    "decision_answers": None,
    "bike_test_protocol": None,
}

# field: (minimum, maximum) for numeric answers.
NUMERIC_RANGES: dict[str, tuple[float, float]] = {
    "current_motivation": (1, 10),
    "subjective_readiness": (1, 10),
    "sleep_quality": (1, 5),
    "work_stress": (1, 10),
    "available_hours_override_min": (0, 3000),
    "available_hours_hard_cap_min": (0, 3000),
}
LIST_FIELDS = ("illness_symptoms", "red_flags", "upcoming_events", "missed_sessions", "time_constraints")
DICT_FIELDS = (
    "pain_scores",
    "missed_session_reasons",
    "double_session_capacity_by_day",
    "time_caps_by_day",
    "decision_answers",
)


def build_post_fetch_checkin(
    planning_date: str,
    *,
    pending_decisions: list[dict[str, Any]] | None = None,
    recent_races: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    questions: list[dict[str, Any]] = [
        {
            "key": "red_flag_screen",
            "prompt": (
                "Safety first: any chest pain or tightness, palpitations or an irregular heartbeat, "
                "breathlessness out of proportion to the effort, dizziness or fainting, or fever in the last week? "
                "(Any yes means no training until a doctor has cleared you.)"
            ),
            "maps_to": ["red_flags"],
        },
        {
            "key": "illness_symptoms",
            "prompt": (
                "Any illness symptoms right now or in the last week? Say where: above the neck "
                "(runny nose, sore throat) or below the neck (chest congestion, body aches, stomach)."
            ),
            "maps_to": ["illness_symptoms"],
        },
        {
            "key": "pain_or_niggles",
            "prompt": (
                "Any pain, niggles or unusual tightness? Give each body part a score from 0 (none) to 10 "
                "(e.g. ITB 2/10, calf 4/10)."
            ),
            "maps_to": ["pain_or_niggles", "pain_scores"],
        },
        {
            "key": "key_session_feel",
            "prompt": "How did the key sessions feel during and after? Mention anything that felt notably better or worse than the data suggests.",
            "maps_to": ["last_week_summary", "athlete_notes"],
        },
        {
            "key": "missed_session_reasons",
            "prompt": "Which sessions did you miss or cut short, and why (time, fatigue, illness, pain, motivation)?",
            "maps_to": ["missed_sessions", "missed_session_reasons"],
        },
        {
            "key": "sleep_quality",
            "prompt": "How did you sleep this week, from 1 (very poor) to 5 (great)?",
            "maps_to": ["sleep_quality"],
        },
        {
            "key": "work_life_stress",
            "prompt": "Work load and life stress this week, from 1 (calm) to 10 (overwhelming)?",
            "maps_to": ["work_stress", "unexpected_fatigue", "athlete_notes"],
        },
        {
            "key": "motivation_readiness",
            "prompt": "What are your motivation and subjective readiness out of 10?",
            "maps_to": ["current_motivation", "subjective_readiness"],
        },
        {
            "key": "available_hours",
            "prompt": "How many hours can you really train this week? This is a hard cap the plan will not exceed.",
            "maps_to": ["available_hours_hard_cap_min"],
        },
        {
            "key": "constraints",
            "prompt": "Any coming-week constraints: travel, time caps per day, pool access changes, fixed sessions, or days that cannot be doubled?",
            "maps_to": [
                "travel_this_week",
                "time_constraints",
                "double_session_capacity_by_day",
                "time_caps_by_day",
                "available_hours_override_min",
                "athlete_notes",
            ],
        },
        {
            "key": "upcoming_events",
            "prompt": "Anything in the next 2-4 weeks beyond this week: travel, busy work periods, holidays, family events, races?",
            "maps_to": ["upcoming_events"],
        },
    ]
    for race in recent_races or []:
        questions.append(
            {
                "key": "race_debrief",
                "prompt": (
                    f"Race debrief for {race.get('name')} ({race.get('date')}): how did it go "
                    "(pacing, fuelling, what broke first, how do you feel now)?"
                ),
                "maps_to": ["race_debrief"],
            }
        )
        break
    for decision in pending_decisions or []:
        due = f" (decide by {decision['due_date']})" if decision.get("due_date") else ""
        questions.append(
            {
                "key": f"decision:{decision.get('id')}",
                "prompt": f"Open decision{due}: {decision.get('question')}",
                "options": decision.get("options") or [],
                "maps_to": ["decision_answers"],
            }
        )
    return {
        "required_before_draft": True,
        "planning_date": planning_date,
        "intro": (
            "Do not draft the week yet. First summarize the fetched training and wellness data, then ask "
            "this compact check-in block. Record the answers with "
            "`python -m triathlon_agent.cli record-checkin <run_dir> <answers.json>` before drafting."
        ),
        "questions": questions,
    }


def _validate_answer(field: str, value: Any) -> str | None:
    if value is None:
        return None
    if field in NUMERIC_RANGES:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{field} must be a number or null (got {value!r})."
        low, high = NUMERIC_RANGES[field]
        if not low <= float(value) <= high:
            return f"{field} must be between {low:g} and {high:g} (got {value!r})."
    if field in LIST_FIELDS and not isinstance(value, list):
        return f"{field} must be a list or null."
    if field in DICT_FIELDS and not isinstance(value, dict):
        return f"{field} must be an object or null."
    if field == "pain_scores" and isinstance(value, dict):
        for part, score in value.items():
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 10:
                return f"pain_scores.{part} must be a number from 0 to 10 (got {score!r})."
    return None


def merge_checkin_answers(
    checkin: dict[str, Any], answers: dict[str, Any]
) -> dict[str, Any]:
    """Merge the athlete's post-fetch answers into the prepared check-in.

    Answers override the prepared values field by field. The planning date
    cannot change (the snapshot was fetched for it). Invalid values raise a
    ValueError listing every problem.
    """
    if not isinstance(answers, dict):
        raise ValueError("Check-in answers must be a JSON object of check-in fields.")
    payload = answers.get("answers") if isinstance(answers.get("answers"), dict) else answers
    errors = [
        error
        for field, value in payload.items()
        if (error := _validate_answer(field, value))
    ]
    planning_date = payload.get("planning_date")
    if planning_date and planning_date != checkin.get("planning_date"):
        errors.append(
            f"planning_date cannot change after prepare-run ({checkin.get('planning_date')} -> {planning_date}); "
            "prepare a new run instead."
        )
    if errors:
        raise ValueError("Invalid check-in answers: " + " ".join(errors))
    merged = deepcopy(checkin)
    for field, value in payload.items():
        merged[field] = deepcopy(value)
    return merged
