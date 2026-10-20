"""Shared coaching thresholds.

One number, one place: the stub head coach, the validator and the deterministic
plan audit (`plan_audit.py`, the safety net behind the LLM head coach) all read
these limits, so they never disagree about what "too much" or "too little" is.
The prose version lives in `prompts/training_intensity_doctrine.md`.
"""

from __future__ import annotations

import math
import re
from typing import Any


# --- weekly volume -------------------------------------------------------------
# Week-to-week growth of total planned time vs the recent loaded weekly average.
MAX_WEEKLY_TOTAL_GROWTH_PCT = 15.0
# Running is the injury-prone part: run minutes / km grow by ~10% a week at most.
MAX_WEEKLY_RUN_GROWTH_PCT = 10.0
# Weekly climbing (D+) and descent grow by ~15% a week at most.
MAX_WEEKLY_ELEVATION_GROWTH_PCT = 15.0
# "Added volume" counts as a progressing element above this growth.
VOLUME_PROGRESSION_PCT = 10.0
# Typical hours from the profile are a soft reference: more than this above warns.
OVER_REFERENCE_WARN_PCT = 15.0

# --- long sessions -------------------------------------------------------------
# A session at or above these minutes is a "long" (durability) session.
LONG_SESSION_MIN: dict[str, int] = {"Ride": 105, "Run": 75}
# Long-session progression from the longest recent one: +10-15 min, never > +15%.
LONG_STEP_MIN = 10
LONG_STEP_MAX = 15
LONG_MAX_JUMP_PCT = 0.15
# Audit tolerance: a long session more than max(15 min, 15%) above recent warns.
LONG_JUMP_MIN = 15.0
LONG_JUMP_PCT = 0.15
# Without a recent long run (break, missing data): 70% of the 56-day longest,
# else this conservative default. Never a template value.
LONG_FALLBACK_56D_FACTOR = 0.7
LONG_RUN_FALLBACK_MIN = 45
LONG_RIDE_FALLBACK_MIN = 75
# A long-session change below this is "holding", not progressing.
LONG_PROGRESSION_MIN = 5.0

# --- intensity distribution ----------------------------------------------------
MIN_GREEN_QUALITY = 2
# Pyramidal floor and ceiling of planned bike/run time above easy (moderate + high).
MIN_PLANNED_INTENSITY_PCT = 8.0
MAX_PLANNED_INTENSITY_PCT = 30.0
# Ladder families that sit in the moderate (tempo / sweet spot) zone of the
# 3-zone model; every other ladder family is high intensity.
MODERATE_FAMILIES = frozenset({"bike_sweet_spot", "run_race_pace"})
# Daniels per-session caps as a share of weekly run minutes, with a floor (min).
DANIELS_CAPS: dict[str, tuple[float, float]] = {
    "T": (0.10, 20.0),
    "I": (0.08, 12.0),
    "R": (0.05, 8.0),
}
DANIELS_FAMILY_TYPE: dict[str, str] = {
    "run_threshold": "T",
    "run_uphill_threshold": "T",
    "run_interval": "I",
}

# --- taper ---------------------------------------------------------------------
# Taper volume vs the recent loaded week, by full weeks to the priority race.
TAPER_VOLUME_BY_WEEKS_OUT: dict[int, float] = {0: 0.45, 1: 0.6, 2: 0.75}
# The audit accepts the planner's ladder within these tolerances.
TAPER_VOLUME_TOLERANCE_HIGH = 0.15
TAPER_VOLUME_TOLERANCE_LOW = 0.25

# --- progression budget ----------------------------------------------------------
# A load week progresses at most this many elements (rung steps, long-session
# duration, added volume), of which at most one is run impact (long run
# duration, run volume, descent). Exception: underload detected, readiness
# green and no ITB/niggle flags.
PROGRESSION_BUDGET_MAX = 2
PROGRESSION_BUDGET_MAX_IMPACT = 1

# --- races -----------------------------------------------------------------------
# Last quality session before a race, in hours (the audit floor is 48h for B).
B_RACE_LAST_QUALITY_H = 72
B_RACE_AUDIT_QUALITY_H = 48
B_RACE_LIGHT_DAYS = 3
C_RACE_LIGHT_DAYS = 1

# --- return / detraining -------------------------------------------------------
# (days without a dose, rungs to step back), longest gap first; None = restart
# at the phase entry rung.
DOSE_GAP_STEP_BACK: tuple[tuple[int, int | None], ...] = (
    (56, None),
    (42, 3),
    (28, 2),
    (14, 1),
)

# --- block rhythm --------------------------------------------------------------
DEFAULT_BLOCK_LENGTH = 4  # 3 load weeks + 1 recovery week
SHORT_BLOCK_LENGTH = 3  # 2:1 after illness / a break / overreaching markers

# Body parts whose pain makes running (impact) the wrong tool this week.
RUN_IMPACT_BODY_PARTS = frozenset(
    {
        "itb",
        "achilles",
        "plantar fascia",
        "hamstring",
        "hip flexor",
        "calf",
        "knee",
        "shin",
        "ankle",
        "foot",
        "hip",
        "glute",
        "quad",
        "groin",
    }
)
PAIN_SCORE_SWAP_MIN = 3

# Missed-session reasons that are not athlete unreliability.
HEALTH_REASON_RE = re.compile(
    r"\b(ill|illness|sick|flu|cold|covid|fever|injur\w*|pain\w*|niggle\w*|ziek\w*|griep|blessure)\b",
    re.IGNORECASE,
)


def is_long_session(session: dict[str, Any]) -> bool:
    """A durability session: long by duration, never a race or test."""
    if session.get("race_day") or session.get("zone_focus") == "race":
        return False
    if session.get("threshold_test"):
        return False
    minimum = LONG_SESSION_MIN.get(str(session.get("discipline")))
    if minimum is None:
        return False
    return float(session.get("duration_min") or 0) >= minimum


def post_race_easy_days(priority: str | None, duration_min: float | None) -> int:
    """Easy days after a race, scaled by race duration (the race day not included)."""
    minutes = float(duration_min or 0)
    if priority == "C":
        return 1 if minutes < 90 else 2
    if minutes < 90:
        return 2
    if minutes < 180:
        return 3
    # Long trail races: roughly one more easy day per extra 2 hours, capped.
    return min(6, 4 + max(0, math.floor((minutes - 240) / 120)))


def pre_race_light_days(priority: str | None) -> int:
    """Lighter days before a race (A races are handled by the taper)."""
    if priority == "B":
        return B_RACE_LIGHT_DAYS
    if priority == "C":
        return C_RACE_LIGHT_DAYS
    return 2


def is_health_reason(reason: Any) -> bool:
    return bool(HEALTH_REASON_RE.search(str(reason or "")))


def baseline_week_minutes(snapshot: dict[str, Any]) -> float | None:
    """Recent loaded weekly average (min), excluding recovery/illness dips and the running week."""
    classified = {
        item.get("week_start"): item.get("week_type")
        for item in (snapshot.get("periodization_state") or {}).get("classified_weeks", []) or []
    }
    complete = [
        window
        for window in snapshot.get("weekly_windows") or []
        if not window.get("partial")
        and classified.get(window.get("week_start")) != "in_progress"
    ]
    windows = [window for window in complete[-4:] if float(window.get("minutes") or 0) > 0]
    if not windows:
        return None
    peak = max(float(window["minutes"]) for window in windows)
    loaded = [
        float(window["minutes"])
        for window in windows
        if classified.get(window.get("week_start")) != "recovery"
        and float(window["minutes"]) >= 0.6 * peak
    ]
    values = loaded or [float(window["minutes"]) for window in windows]
    return sum(values) / len(values)


def safe_float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
