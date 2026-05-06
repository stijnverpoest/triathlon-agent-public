from __future__ import annotations

from typing import Any


def build_post_fetch_checkin(planning_date: str) -> dict[str, Any]:
    questions = [
        {
            "key": "pain_or_niggles",
            "prompt": "Any pain, niggles, soreness, or unusual tightness right now?",
            "maps_to": ["pain_or_niggles"],
        },
        {
            "key": "key_session_feel",
            "prompt": "How did the key sessions feel during and after? Mention anything that felt notably better or worse than the data suggests.",
            "maps_to": ["last_week_summary", "athlete_notes"],
        },
        {
            "key": "work_life_stress",
            "prompt": "What does work stress and life stress look like this week?",
            "maps_to": ["unexpected_fatigue", "athlete_notes"],
        },
        {
            "key": "motivation_readiness",
            "prompt": "What are your motivation and subjective readiness out of 10?",
            "maps_to": ["current_motivation", "subjective_readiness"],
        },
        {
            "key": "constraints",
            "prompt": "Any coming-week constraints: travel, time caps, pool access changes, fixed sessions, or days that cannot be doubled?",
            "maps_to": [
                "travel_this_week",
                "time_constraints",
                "double_session_capacity_by_day",
                "time_caps_by_day",
                "available_hours_override_min",
                "athlete_notes",
            ],
        },
    ]
    return {
        "required_before_draft": True,
        "planning_date": planning_date,
        "intro": "Do not draft the week yet. First summarize the fetched training and wellness data, then ask this compact check-in block.",
        "questions": questions,
    }
