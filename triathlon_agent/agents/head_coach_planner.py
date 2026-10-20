"""Deterministic stub of the head coach planner.

The production planner is the LLM head coach (see `prompts/head_coach_planner.md`
and `prompts/training_intensity_doctrine.md`); it designs every session itself.
This stub exists for mock runs and tests. It follows the same principles instead
of a session library:

- the number of quality sessions comes from readiness (`intensity_allowance`),
  the HRV gate, underload and the week type, not from a fixed one-per-sport mould
- quality doses progress along time-in-zone ladders (`progression.py`)
- the distribution is pyramidal by default; VO2max/I work joins from build onward
- one thing progresses at a time (progression budget, `coaching_limits.py`)
- weekly volume is a coaching decision, but explicit day caps and the check-in's
  available-hours caps bind; run load respects `run_load.week_caps` and the
  return-from-break ramp
- races come only from the Intervals calendar; B races get a mini-taper
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import timedelta
from typing import Any

from ..coaching_limits import (
    DANIELS_CAPS,
    DANIELS_FAMILY_TYPE,
    LONG_FALLBACK_56D_FACTOR,
    LONG_MAX_JUMP_PCT,
    LONG_PROGRESSION_MIN,
    LONG_RIDE_FALLBACK_MIN,
    LONG_RUN_FALLBACK_MIN,
    LONG_SESSION_MIN,
    LONG_STEP_MAX,
    LONG_STEP_MIN,
    MAX_WEEKLY_ELEVATION_GROWTH_PCT,
    MAX_WEEKLY_RUN_GROWTH_PCT,
    MAX_WEEKLY_TOTAL_GROWTH_PCT,
    PAIN_SCORE_SWAP_MIN,
    PROGRESSION_BUDGET_MAX,
    PROGRESSION_BUDGET_MAX_IMPACT,
    RUN_IMPACT_BODY_PARTS,
    TAPER_VOLUME_BY_WEEKS_OUT,
    baseline_week_minutes,
    is_health_reason,
    is_long_session,
    post_race_easy_days,
    pre_race_light_days,
)
from ..progression import (
    FOCUS_BY_DISTANCE,
    I_BLOCK_WEEKS,
    LADDERS,
    current_rung,
    session_dose,
    focus_families,
    ladder_dose,
    missed_session_rule,
    next_rung,
    phase_load_weeks,
)
from ..race_selection import calendar_races, race_bucket, race_distance_km
from ..threshold_testing import PROTOCOLS
from ..utils import day_name, daterange, has_niggle, niggle_body_parts, parse_date
from .nutritionist import carb_range_g_per_h


DAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

RIDE_KINDS = {"quality_ride", "endurance_ride", "long_ride", "bike_threshold_test"}
RUN_KINDS = {
    "easy_run",
    "quality_run",
    "long_run",
    "medium_long_run",
    "run_threshold_test",
}
QUALITY_KINDS = {"quality_ride", "quality_run"}
VO2_FAMILIES = {"bike_vo2", "run_interval"}
TRIATHLON_BUCKETS = {"sprint", "olympic", "70.3", "ironman"}
TRAIL_BUCKETS = {"trail", "ultra_trail"}
LOAD_PHASES = {"build", "race_specific"}

# Strength in recovery / absorb / holiday / non-green weeks never exceeds this;
# the validator applies the same number (one rule, two agents).
LIGHT_STRENGTH_MAX_MIN = 25
ACTIVATION_STRENGTH_MIN = 20

# (fallback base, step per block week, cap) in minutes, per race bucket.
LONG_RUN_RULES: dict[str, tuple[int, int, int]] = {
    "sprint": (60, 5, 75),
    "olympic": (65, 5, 90),
    "70.3": (75, 10, 120),
    "ironman": (90, 10, 150),
    "5k": (60, 5, 90),
    "10k": (65, 5, 100),
    "half_marathon": (80, 10, 120),
    "marathon": (100, 15, 180),
    "trail": (90, 10, 180),
    "ultra_trail": (120, 20, 240),
}
DEFAULT_LONG_RUN_RULE = (70, 10, 110)
LONG_RIDE_RULES: dict[str, tuple[int, int, int]] = {
    "sprint": (90, 10, 150),
    "olympic": (120, 15, 210),
    "70.3": (150, 20, 300),
    "ironman": (180, 20, 360),
    "5k": (75, 0, 90),
    "10k": (75, 0, 90),
    "half_marathon": (90, 0, 120),
    "marathon": (90, 0, 120),
    # Trail weeks: the bike is aerobic support, not a 2-3.5 h long ride.
    "trail": (75, 5, 100),
    "ultra_trail": (80, 5, 100),
}
DEFAULT_LONG_RIDE_RULE = (135, 15, 240)
# Long-session progression limits (LONG_STEP_*, LONG_MAX_JUMP_PCT) and the taper
# ladder (TAPER_VOLUME_BY_WEEKS_OUT) live in `coaching_limits.py`.
RECOVERY_LONG_FACTOR = 0.72
HOLIDAY_LONG_FACTOR = 0.85
TAPER_LONG_FACTOR = 0.6
STALE_FTP_SCALE_LIMIT = 0.97
# eFTP this far above the set FTP: scale %FTP targets up, but never more than
# the cap until a test or the approved update confirms it.
LOW_FTP_SCALE_LIMIT = 1.05
LOW_FTP_SCALE_CAP = 1.08
# Pace for turning run minutes into km when the history has no pace (trail).
DEFAULT_RUN_MIN_PER_KM = 6.5
# Back-to-back trail weekend: the Saturday run starts short and grows per load week.
MEDIUM_LONG_START_MIN = 30
MEDIUM_LONG_STEP_MIN = 10
MEDIUM_LONG_SHARE = 0.55
# Run families swap to these bike families when a run-impact niggle is reported.
BIKE_SWAP_FAMILY = {
    "run_threshold": "bike_threshold",
    "run_uphill_threshold": "bike_threshold",
    "run_interval": "bike_vo2",
    "run_race_pace": "bike_sweet_spot",
}
HOLIDAY_CAMP_WORDS = ("trail camp", "hilly", "mountain", "alps", "ardennes", "vosges", "pyrenees", "dolomites")
TENNIS_SEASON_MONTHS = range(5, 10)
SKIP_WORDS = ("skip", "no tennis", "cancel", "not this week", "without tennis")
PERCENT_FTP_RE = re.compile(r"(\d+)(?:\s*-\s*(\d+))?\s*%")

# Lower = trimmed first when an explicit day cap forces a cut.
REDUCTION_PRIORITY = {
    "swim_club": 0,
    "swim_pool": 0,
    "brick_run": 0,
    "opener": 1,
    "easy_run": 1,
    "medium_long_run": 4,
    "cross_training": 1,
    "strength": 2,
    "endurance_ride": 3,
    "quality_run": 5,
    "quality_ride": 5,
    "long_run": 6,
    "long_ride": 7,
    "recovery": 8,
    "bike_threshold_test": 9,
    "run_threshold_test": 9,
    "race": 10,
    "tennis": 10,
}


def reduction_priority(kind: str) -> int:
    return REDUCTION_PRIORITY.get(kind, 9)


def _club_swim_preferences(snapshot: dict[str, Any]) -> tuple[int, int | None]:
    swim_profile = snapshot.get("athlete_profile", {}).get("swim_profile", {})
    duration = swim_profile.get("club_session_duration_min", 40)
    distance = swim_profile.get("club_session_distance_m")
    try:
        duration = max(40, int(duration))
    except (TypeError, ValueError):
        duration = 40
    try:
        distance = int(distance) if distance not in (None, "") else None
    except (TypeError, ValueError):
        distance = None
    return duration, distance


def _friday_support_ride_requested(snapshot: dict[str, Any]) -> bool:
    constraints = snapshot.get("planning_constraints", {})
    weekly = snapshot.get("weekly_checkin", {})
    texts = [
        *constraints.get("raw_time_constraints", []),
        str(weekly.get("athlete_notes", "")),
    ]
    for text in texts:
        lowered = text.lower()
        if "friday" not in lowered:
            continue
        if "support ride" in lowered:
            return True
        if "not be fully off" in lowered or "rather than being fully off" in lowered:
            return True
    return False


def _days_in(text: str) -> list[str]:
    lowered = str(text or "").lower()
    return [day for day in DAY_NAMES if day.lower() in lowered]


def _clauses(text: str) -> list[str]:
    return [part for part in re.split(r"[.;\n]| -- ", str(text or "")) if part.strip()]


def _week_mid_month(snapshot: dict[str, Any]) -> int | None:
    try:
        return daterange(parse_date(snapshot["week_start"]), 7)[3].month
    except (KeyError, TypeError, ValueError):
        return None


def tennis_days(snapshot: dict[str, Any]) -> list[str]:
    """Weekdays with a fixed tennis session this week (moderate-hard load).

    Profile routines are seasonal (May-Sep) unless they say year-round; a
    check-in mention applies this week regardless, and "skip" removes it.
    """
    profile = snapshot.get("athlete_profile", {}) or {}
    weekly = snapshot.get("weekly_checkin", {}) or {}
    constraints = snapshot.get("planning_constraints", {}) or {}
    month = _week_mid_month(snapshot)
    in_season = month is None or month in TENNIS_SEASON_MONTHS
    days: list[str] = []
    profile_texts: list[str] = [
        *[str(item) for item in profile.get("fixed_weekly_preferences", []) or []],
        *[str(item) for item in profile.get("known_constraints", []) or []],
    ]
    for day, text in (profile.get("regular_training_schedule") or {}).items():
        profile_texts.append(f"{day}: {text}")
    for text in profile_texts:
        for clause in _clauses(text):
            lowered = clause.lower()
            if "tennis" not in lowered:
                continue
            year_round = "year-round" in lowered or "all year" in lowered
            if in_season or year_round:
                days.extend(day for day in _days_in(text) if day not in days)
    checkin_texts = [
        *[str(item) for item in weekly.get("time_constraints", []) or []],
        *[str(item) for item in constraints.get("raw_time_constraints", []) or []],
        str(weekly.get("athlete_notes", "") or ""),
    ]
    for text in checkin_texts:
        lowered = text.lower()
        if "tennis" not in lowered:
            continue
        mentioned = _days_in(text) or days
        if any(word in lowered for word in SKIP_WORDS):
            days = [day for day in days if day not in mentioned]
        else:
            days.extend(day for day in _days_in(text) if day not in days)
    return [day for day in DAY_NAMES if day in days]


def travel_days(snapshot: dict[str, Any]) -> tuple[list[str], bool]:
    """(travel weekdays, travel flagged) from the check-in."""
    weekly = snapshot.get("weekly_checkin", {}) or {}
    constraints = snapshot.get("planning_constraints", {}) or {}
    flagged = bool(constraints.get("travel_this_week") or weekly.get("travel_this_week"))
    explicit = weekly.get("travel_days") or constraints.get("travel_days") or []
    days = [day for day in explicit if day in DAY_NAMES]
    texts = [
        *[str(item) for item in weekly.get("time_constraints", []) or []],
        str(weekly.get("athlete_notes", "") or ""),
    ]
    for text in texts:
        for clause in _clauses(text):
            lowered = clause.lower()
            if any(token in lowered for token in ("travel", "trip", "flight", "away for work")):
                days.extend(day for day in _days_in(clause) if day not in days)
    return [day for day in DAY_NAMES if day in days], flagged or bool(days)


def club_swim_days(snapshot: dict[str, Any]) -> list[str]:
    """Days whose swim is a club session, read from the profile, never assumed."""
    profile = snapshot.get("athlete_profile", {}) or {}
    hard = profile.get("hard_constraints", {}) or {}
    texts = [
        *[str(item) for item in hard.get("always", []) or []],
        *[str(item) for item in hard.get("when_open_water_mode_false", []) or []],
        *[str(item) for item in profile.get("fixed_weekly_preferences", []) or []],
        *[
            str(item)
            for item in (profile.get("soft_preferences", {}) or {}).get("weekday_rhythm", [])
            or []
        ],
    ]
    texts.extend(
        f"{day}: {text}"
        for day, text in (profile.get("regular_training_schedule") or {}).items()
    )
    days: list[str] = []
    for text in texts:
        for clause in _clauses(text):
            if "club" in clause.lower() and "swim" in clause.lower():
                days.extend(day for day in _days_in(clause) if day not in days)
    return [day for day in DAY_NAMES if day in days]


def races_in_week(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Races (any priority) inside the planned week, keyed by weekday."""
    result: dict[str, dict[str, Any]] = {}
    for race in calendar_races(snapshot):
        days = race.get("days_to_race")
        if days is None or not 0 <= int(days) <= 6:
            continue
        try:
            weekday = day_name(parse_date(str(race.get("date"))))
        except (TypeError, ValueError):
            weekday = DAY_NAMES[int(days)]
        current = result.get(weekday)
        rank = {"A": 0, "B": 1, "C": 2}
        if current is None or rank.get(race.get("priority"), 3) < rank.get(
            current.get("priority"), 3
        ):
            result[weekday] = race
    return result


def race_duration_estimate(race: dict[str, Any]) -> int:
    bucket = race_bucket(race)
    km = race_distance_km(race.get("distance"), race.get("name"), race.get("notes"))
    fixed = {
        "sprint": 80,
        "olympic": 150,
        "70.3": 330,
        "ironman": 720,
        "5k": 25,
        "10k": 50,
        "half_marathon": 110,
        "marathon": 240,
    }
    if bucket in TRAIL_BUCKETS:
        return _round_to_5((km or (50 if bucket == "ultra_trail" else 25)) * 8.5)
    return fixed.get(bucket, 90)


def race_windows(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Day offsets (0 = Monday of the planned week) around every nearby race.

    `days_to_race` is relative to week_start. B races get a mini-taper
    (3 lighter days, no quality in the last 72h) and C races 1 light day;
    every race is followed by easy days scaled by its estimated duration,
    including races of last week that spill into this one.
    """
    light: dict[int, list[str]] = defaultdict(list)
    easy_after: dict[int, list[str]] = defaultdict(list)
    for race in calendar_races(snapshot):
        try:
            offset = int(race.get("days_to_race"))
        except (TypeError, ValueError):
            continue
        if not -10 <= offset <= 13:
            continue
        name = str(race.get("name") or "race")
        priority = str(race.get("priority") or "C")
        if offset >= 0:
            for day in range(offset - pre_race_light_days(priority), offset):
                if 0 <= day <= 6:
                    light[day].append(f"{priority}-race {name}")
        for day in range(offset + 1, offset + 1 + post_race_easy_days(priority, race_duration_estimate(race))):
            if 0 <= day <= 6:
                easy_after[day].append(f"{priority}-race {name}")
    return {"light": dict(light), "easy_after": dict(easy_after)}


def has_itb_history(snapshot: dict[str, Any]) -> bool:
    history = (snapshot.get("athlete_profile", {}) or {}).get("injury_history", []) or []
    text = " ".join(str(item) for item in history).lower()
    return "itb" in text or "iliotibial" in text or "it band" in text


def impact_niggles(snapshot: dict[str, Any]) -> list[str]:
    """Run-impact body parts hurting this week: pain score >= 3 or an activity pain note."""
    weekly = snapshot.get("weekly_checkin", {}) or {}
    parts: list[str] = []
    scores = weekly.get("pain_scores") or {}
    if isinstance(scores, dict):
        for part, score in scores.items():
            try:
                value = float(score)
            except (TypeError, ValueError):
                continue
            label = str(part).strip().lower()
            if value >= PAIN_SCORE_SWAP_MIN and _is_run_impact_part(label):
                parts.append(label)
    for flag in snapshot.get("activity_note_flags") or []:
        if flag.get("kind") != "pain":
            continue
        label = str(flag.get("body_part") or "").strip().lower()
        if label and _is_run_impact_part(label):
            parts.append(label)
    return list(dict.fromkeys(parts))


def _is_run_impact_part(label: str) -> bool:
    lowered = label.lower()
    if lowered in RUN_IMPACT_BODY_PARTS:
        return True
    detected = [part.lower() for part in niggle_body_parts(f"{lowered} pain")]
    return any(part in RUN_IMPACT_BODY_PARTS for part in detected)


def niggle_flagged(snapshot: dict[str, Any]) -> bool:
    """Any reported niggle (free text, pain score or activity note) or ITB history."""
    weekly = snapshot.get("weekly_checkin", {}) or {}
    scores = weekly.get("pain_scores") or {}
    scored = isinstance(scores, dict) and any(
        _safe_number(value) and _safe_number(value) > 0 for value in scores.values()
    )
    return (
        has_itb_history(snapshot)
        or has_niggle(weekly.get("pain_or_niggles"))
        or scored
        or bool(impact_niggles(snapshot))
    )


def _safe_number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def ftp_target_scale(snapshot: dict[str, Any]) -> float | None:
    """Multiplier for %FTP targets while the set FTP is wrong and not being corrected.

    Set FTP too high (eFTP/FTP < 0.97): scale down to the estimated threshold.
    Set FTP too low (eFTP > 5% above FTP): scale up, capped at x1.08 until a
    test or the approved update confirms it. No scaling once an FTP update is
    marked to apply on approval (the targets then already fit the new FTP).
    """
    testing = snapshot.get("threshold_testing", {}) or {}
    bike = testing.get("bike") or {}
    if bike.get("stale") is False:
        return None
    scale = _safe_number(bike.get("ftp_target_scale"))
    if scale is None or scale <= 0.5:
        return None
    for update in testing.get("proposed_updates", []) or []:
        if update.get("field") == "ftp" and update.get("apply"):
            return None
    if scale < STALE_FTP_SCALE_LIMIT:
        return scale
    if scale > LOW_FTP_SCALE_LIMIT:
        return min(scale, LOW_FTP_SCALE_CAP)
    return None


def pending_ftp_update(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    for update in (snapshot.get("threshold_testing", {}) or {}).get("proposed_updates", []) or []:
        if update.get("field") == "ftp" and update.get("apply"):
            return update
    return None


def _week_hard_cap(snapshot: dict[str, Any], return_from_break: dict[str, Any]) -> int | None:
    """Binding weekly minutes: the check-in caps and, on re-entry, the volume cap."""
    caps = [hard_cap_minutes(snapshot)]
    pre = _safe_number(return_from_break.get("pre_break_weekly_min"))
    pct = _safe_number(return_from_break.get("volume_cap_pct"))
    if pre and pct:
        caps.append(int(pre * pct / 100))
    caps = [cap for cap in caps if cap]
    return min(caps) if caps else None


def reduced_dose_week(snapshot: dict[str, Any], plan: dict[str, Any] | None = None) -> bool:
    """Weeks where fewer than the default quality sessions is correct.

    Return from a break, recovering from a race (>= 3 post-race easy days this
    week), or the binding hours cap already cut the secondary quality.
    """
    load = snapshot.get("training_load", {}) or {}
    if (load.get("return_from_break") or {}).get("status") == "returning":
        return True
    if len(race_windows(snapshot).get("easy_after", {})) >= 3:
        return True
    cuts = ((plan or {}).get("weekly_time_budget") or {}).get("cuts") or []
    return any("quality" in str(cut) for cut in cuts)


def hard_cap_minutes(snapshot: dict[str, Any]) -> int | None:
    """Binding weekly cap from the check-in (hard cap and available-hours override)."""
    weekly = snapshot.get("weekly_checkin", {}) or {}
    constraints = snapshot.get("planning_constraints", {}) or {}
    values = []
    for source in (weekly, constraints):
        for key in ("available_hours_hard_cap_min", "available_hours_override_min"):
            value = _safe_number(source.get(key))
            if value and value > 0:
                values.append(int(value))
    return min(values) if values else None


def _ordered_days(*groups: list[str]) -> list[str]:
    seen: list[str] = []
    for group in groups:
        for day in group:
            if day in DAY_NAMES and day not in seen:
                seen.append(day)
    return seen


def _sort_days_by_memory(
    candidate_days: list[str], athlete_memory: dict[str, Any]
) -> list[str]:
    reliable_days = set(athlete_memory.get("reliable_days", []))
    fragile_days = set(athlete_memory.get("fragile_days", []))
    return sorted(
        candidate_days,
        key=lambda day: (
            2 if day in fragile_days else 0,
            0 if day in reliable_days else 1,
            candidate_days.index(day),
        ),
    )


def _day_map(snapshot: dict[str, Any]) -> dict[str, Any]:
    week_days = daterange(parse_date(snapshot["week_start"]), 7)
    return {day_name(item): item for item in week_days}


def _can_double(constraints: dict[str, Any], day: str) -> bool:
    explicit = constraints.get("double_session_capacity_by_day", {}).get(day)
    if explicit is not None:
        return bool(explicit)
    return day in constraints.get("preferred_double_days", [])


def _cap_for_day(constraints: dict[str, Any], day: str) -> int | None:
    cap = constraints.get("time_caps_by_day", {}).get(day)
    if cap in (None, ""):
        return None
    return int(cap)


def _round_to_5(value: float) -> int:
    return int(round(value / 5.0) * 5)


def _day_available(
    day: str,
    *,
    specs: list[dict[str, Any]],
    constraints: dict[str, Any],
    minimum_minutes: int,
    ride_session: bool,
    allowed_pair_kinds: set[str],
) -> bool:
    if day in constraints.get("days_off", []) or day in constraints.get(
        "strength_only_days", []
    ):
        return False
    if ride_session and day in constraints.get("no_bike_days", []):
        return False
    day_specs = [spec for spec in specs if spec["day"] == day]
    cap = _cap_for_day(constraints, day)
    existing = sum(int(spec["duration"]) for spec in day_specs)
    if cap is not None and existing + minimum_minutes > cap:
        return False
    if not day_specs:
        return True
    if len(day_specs) >= 2 or constraints.get(
        "double_session_capacity_by_day", {}
    ).get(day) is False:
        return False
    kinds = {spec["kind"] for spec in day_specs}
    if kinds <= {"swim_club", "swim_pool"}:
        return _can_double(constraints, day)
    return kinds <= allowed_pair_kinds


def _pick_day(
    candidates: list[str],
    *,
    specs: list[dict[str, Any]],
    constraints: dict[str, Any],
    blocked: set[str],
    minimum_minutes: int,
    ride_session: bool,
    allowed_pair_kinds: set[str] | None = None,
    avoid_adjacent_to: set[str] | None = None,
    notes: list[str] | None = None,
    label: str = "session",
) -> str | None:
    """First usable candidate day, preferring days not adjacent to `avoid`.

    When only an adjacent (conflicting) day is left, the choice is recorded in
    `notes` so the compromise is visible instead of silent.
    """
    allowed_pair_kinds = allowed_pair_kinds or set()
    avoid = avoid_adjacent_to or set()

    def adjacent(day: str) -> bool:
        index = DAY_NAMES.index(day)
        return any(
            abs(index - DAY_NAMES.index(other)) == 1 for other in avoid if other in DAY_NAMES
        )

    usable = [
        day
        for day in candidates
        if day not in blocked
        and _day_available(
            day,
            specs=specs,
            constraints=constraints,
            minimum_minutes=minimum_minutes,
            ride_session=ride_session,
            allowed_pair_kinds=allowed_pair_kinds,
        )
    ]
    for day in usable:
        if not adjacent(day):
            return day
    if usable and notes is not None:
        neighbours = sorted(
            (other for other in avoid if other in DAY_NAMES
             and abs(DAY_NAMES.index(other) - DAY_NAMES.index(usable[0])) == 1),
            key=DAY_NAMES.index,
        )
        notes.append(
            f"{label} placed on {usable[0]}, next to {', '.join(neighbours)}: no non-adjacent day was available."
        )
    return usable[0] if usable else None


def _add_spec(specs: list[dict[str, Any]], **spec: Any) -> dict[str, Any]:
    spec.setdefault("min_duration", spec["duration"])
    specs.append(spec)
    return spec


def _fit_day_caps(specs: list[dict[str, Any]], constraints: dict[str, Any]) -> None:
    """Only explicit day caps bind. Weekly totals are a coaching decision."""
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for spec in specs:
        by_day[spec["day"]].append(spec)
    for day, items in by_day.items():
        cap = _cap_for_day(constraints, day)
        if cap is None:
            continue
        while sum(item["duration"] for item in items) > cap:
            excess = sum(item["duration"] for item in items) - cap
            trimmable = [
                item
                for item in sorted(
                    items,
                    key=lambda value: (reduction_priority(value["kind"]), -value["duration"]),
                )
                if not item.get("fixed_duration")
                and item["duration"] > item["min_duration"]
            ]
            if not trimmable:
                break
            item = trimmable[0]
            item["duration"] -= min(5, excess, item["duration"] - item["min_duration"])


# --- threshold tests --------------------------------------------------------


def threshold_test_duration(protocol: str) -> int:
    return {"friel_30_run": 59, "friel_30": 61}.get(protocol, 71)


def _apply_threshold_tests(
    specs: list[dict[str, Any]],
    macro: dict[str, Any],
    *,
    midweek_ride_day: str | None,
    long_ride_day: str,
    long_run_day: str,
) -> list[str]:
    """Place scheduled tests late in the recovery week (Friel rest & test).

    A test replaces the weekend long session of its discipline. With two tests,
    the bike test moves to the midweek ride slot so the tests stay 48h apart.
    """
    scheduled = {
        entry["discipline"]: entry
        for entry in (macro.get("threshold_tests") or {}).get("scheduled", [])
    }
    if not scheduled:
        return []
    run_index = DAY_NAMES.index(long_run_day)
    bike_day = long_ride_day
    if "run" in scheduled and "bike" in scheduled:
        if midweek_ride_day and DAY_NAMES.index(midweek_ride_day) <= run_index - 2:
            bike_day = midweek_ride_day
        elif DAY_NAMES.index(long_ride_day) > run_index - 2:
            scheduled.pop("run")
    placed: list[str] = []
    for discipline, day, kind, replace in (
        ("bike", bike_day, "bike_threshold_test", RIDE_KINDS),
        ("run", long_run_day, "run_threshold_test", RUN_KINDS),
    ):
        if discipline not in scheduled:
            continue
        protocol = scheduled[discipline]["protocol"]
        duration = threshold_test_duration(protocol)
        test_spec = {
            "kind": kind,
            "day": day,
            "duration": duration,
            "min_duration": duration,
            "fixed_duration": True,
            "protocol": protocol,
        }
        for index, spec in enumerate(specs):
            if spec["day"] == day and spec["kind"] in replace:
                specs[index] = test_spec
                break
        else:
            specs.append(test_spec)
        placed.append(day)
    eve_days = {
        DAY_NAMES[DAY_NAMES.index(day) - 1] for day in placed if DAY_NAMES.index(day) > 0
    }
    specs[:] = [
        spec
        for spec in specs
        if not (spec["kind"] == "strength" and spec["day"] in eve_days)
    ]
    return placed


# --- week shape -------------------------------------------------------------


def _week_context(
    snapshot: dict[str, Any],
    performance: dict[str, Any],
    macro: dict[str, Any],
    race: dict[str, Any],
) -> dict[str, Any]:
    constraints = snapshot.get("planning_constraints", {})
    classification = str(macro.get("week_classification", "build"))
    holiday = classification == "holiday"
    recovery_week = (
        classification in {"recovery", "unload"}
        or macro.get("friel_cycle_type") == "recovery"
        or constraints.get("requested_week_type") == "recovery"
    )
    phase = str(macro.get("macro_phase") or "build")
    readiness = str(performance.get("readiness_status", "green"))
    allowance = performance.get("intensity_allowance") or {}
    default_quality = {"green": 2, "yellow": 1, "red": 0}.get(readiness, 1)
    max_quality = int(allowance.get("max_quality_sessions", default_quality))
    training_load = snapshot.get("training_load", {}) or {}
    underload_status = str(
        (performance.get("underload") or {}).get("status")
        or (training_load.get("underload") or {}).get("status")
        or "not_detected"
    )
    notes: list[str] = []
    # Races come only from the Intervals calendar (via the race director), never
    # from the profile goal text or target distance.
    priority_race = dict(race.get("priority_race") or {})
    if int(priority_race.get("days_to_race", 0) or 0) >= 9999 or (
        priority_race.get("distance") in (None, "", "unknown") and not priority_race.get("name")
    ):
        priority_race = {}
    if not priority_race:
        notes.append("No race on your Intervals calendar: general base week.")
    bucket = race_bucket(priority_race) if priority_race else "unknown"
    distance = bucket if bucket in FOCUS_BY_DISTANCE else "generic"
    days_to_race = int(priority_race.get("days_to_race", 9999) or 9999)
    week_races = races_in_week(snapshot)
    priority_in_week = bool(priority_race) and (
        any(item.get("name") == priority_race.get("name") for item in week_races.values())
        or (0 <= days_to_race <= 6)
    )
    transition = phase == "transition"
    taper = phase == "taper"
    race_week = taper and priority_in_week
    quality_count = 0 if recovery_week or transition or holiday else max_quality
    if taper:
        quality_count = min(quality_count, 1 if race_week else 2)
    return_from_break = training_load.get("return_from_break") or {}
    returning = return_from_break.get("status") == "returning"
    reentry_week = return_from_break.get("reentry_week")
    if returning and quality_count > 1 and (reentry_week in (None, 1, 2)):
        quality_count = 1
        notes.append(
            "Return from a break: one quality session this week; easy volume rebuilds first."
        )
    windows = race_windows(snapshot)
    # Three or more post-race easy days this week (a long race just done):
    # one quality session at most and the long run holds.
    post_race_hold = len(windows.get("easy_after", {})) >= 3
    if post_race_hold and quality_count > 1:
        quality_count = 1
        notes.append("Recovering from last weekend's race: one quality session, the long run holds.")
    volume_factor = 1.0
    if taper:
        weeks_out = max(0, days_to_race // 7) if days_to_race < 9999 else 2
        volume_factor = TAPER_VOLUME_BY_WEEKS_OUT.get(weeks_out, 0.75)
    travel, travel_flagged = travel_days(snapshot)
    block_week = int(macro.get("friel_block_week") or 1)
    load_weeks = phase_load_weeks(snapshot, phase, block_week)
    niggles = impact_niggles(snapshot)
    flagged = niggle_flagged(snapshot)
    budget_exempt = underload_status == "detected" and readiness == "green" and not flagged
    weekly = snapshot.get("weekly_checkin", {}) or {}
    camp_text = " ".join(
        str(item)
        for item in [
            weekly.get("athlete_notes"),
            *(weekly.get("upcoming_events") or []),
            *(weekly.get("time_constraints") or []),
        ]
    ).lower()
    return {
        "constraints": constraints,
        "classification": classification,
        "recovery_week": recovery_week or transition,
        "absorb_week": classification == "absorb",
        "phase": phase,
        "readiness": readiness,
        "underload_status": underload_status,
        "distance": distance,
        "bucket": bucket,
        "has_race": bool(priority_race),
        "trail": bucket in TRAIL_BUCKETS,
        "triathlon": bucket in TRIATHLON_BUCKETS,
        "quality_count": quality_count,
        "block_week": block_week,
        # Completed load weeks in this phase: support doses (descent, brick,
        # race blocks, steady finish, swim reps) build on it across blocks.
        "load_weeks": load_weeks,
        "i_block": phase == "build" and load_weeks < I_BLOCK_WEEKS,
        "transition": transition,
        "holiday": holiday,
        "trail_camp": holiday and any(word in camp_text for word in HOLIDAY_CAMP_WORDS),
        "taper": taper,
        "race_week": race_week,
        "volume_factor": volume_factor,
        "days_to_race": days_to_race,
        "week_races": week_races,
        "race_windows": windows,
        "post_race_hold": post_race_hold,
        "tennis_days": tennis_days(snapshot),
        "travel_days": travel,
        "travel_flagged": travel_flagged,
        "itb": has_itb_history(snapshot),
        "impact_niggles": niggles,
        "niggle_flagged": flagged,
        "budget_exempt": budget_exempt,
        "returning": returning,
        "return_from_break": return_from_break,
        "week_caps": (training_load.get("run_load") or {}).get("week_caps") or {},
        "hard_cap_min": _week_hard_cap(snapshot, return_from_break if returning else {}),
        "ftp_scale": ftp_target_scale(snapshot),
        "pending_ftp_update": pending_ftp_update(snapshot),
        "open_water": bool(constraints.get("open_water_mode")),
        "planning_date": snapshot.get("planning_date"),
        "training_load": training_load,
        "progressions": [],
        "held": [],
        "notes": notes,
        "warnings": [],
    }


def _quality_families(context: dict[str, Any], run_slots: int | None = None) -> list[str]:
    """Quality families for the week, most important first.

    Focus follows phase and race bucket (`progression.focus_families`). Run
    quality is limited by `run_slots` (the back-to-back weekend allows one run
    quality at most and there is always a genuinely easy run); the other
    quality goes to the bike. A run-impact niggle moves all quality to the bike.
    """
    focus = focus_families(context["distance"], context["phase"], i_block=context["i_block"])
    if context["impact_niggles"]:
        swapped = [BIKE_SWAP_FAMILY.get(family, family) for family in focus]
        focus = list(dict.fromkeys(swapped))
    bike_fill = ["bike_sweet_spot", "bike_threshold"]
    chosen: list[str] = []
    runs_used = 0
    for family in focus + [family for family in bike_fill if family not in focus]:
        if len(chosen) >= context["quality_count"]:
            break
        if family.startswith("run"):
            if run_slots is not None and runs_used >= run_slots:
                continue
            runs_used += 1
        chosen.append(family)
    return chosen


def _recent_minutes(load: dict[str, Any], key: str) -> float | None:
    value = load.get(key)
    try:
        value = float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return value if value and value > 0 else None


def progressed_long_minutes(
    recent: float | None, rule: tuple[int, int, int], context: dict[str, Any]
) -> int:
    """Next long session from the longest recent one (+10-15 min, never > +15%).

    `recent` must come from history (see `_long_reference`); the per-bucket rule
    only caps the duration. Recovery weeks drop to ~72%, holiday weeks ~85%,
    absorb/non-green weeks hold, tapers cut ~40%.
    """
    _, _, cap = rule
    if recent is None:
        recent = float(LONG_RUN_FALLBACK_MIN)
    jump = min(LONG_STEP_MAX, max(LONG_STEP_MIN, recent * 0.10))
    progressed = min(recent + jump, recent * (1 + LONG_MAX_JUMP_PCT), cap)
    reference = min(recent, cap)
    if context["taper"]:
        factor = 0.5 if context["race_week"] else TAPER_LONG_FACTOR
        minutes = reference * factor
    elif context["recovery_week"]:
        minutes = reference * RECOVERY_LONG_FACTOR
    elif context.get("holiday"):
        minutes = reference * (1.0 if context.get("trail_camp") else HOLIDAY_LONG_FACTOR)
    elif context["absorb_week"] or context["readiness"] != "green":
        minutes = min(reference, progressed) * 0.9
    else:
        minutes = progressed
    return _round_to_5(minutes)


def _long_reference(
    load: dict[str, Any],
    key_28d: str,
    key_56d: str,
    fallback_min: int,
    caps: list[float | None],
) -> tuple[float, bool, str]:
    """(reference minutes, from recent history?, basis) for the long session.

    The longest of the last 28 days, never a race (SIGNALS excludes races;
    a value equal to `longest_race_min_28d` is ignored defensively). Without
    it - after a break or with missing data - the cap from `run_load.week_caps`
    or `return_from_break`, else 70% of the 56-day longest, else a
    conservative default. Never a template value.
    """
    recent = _recent_minutes(load, key_28d)
    race = _recent_minutes(load, "longest_race_min_28d") if key_28d.startswith("longest_run") else None
    if recent is not None and race is not None and abs(recent - race) <= 1:
        recent = None
    if recent is not None:
        return recent, True, f"longest of the last 28 days ({recent:g}')"
    valid_caps = [float(value) for value in caps if _safe_number(value)]
    if valid_caps:
        return min(valid_caps), False, "re-entry cap (no recent long session)"
    longest_56 = _recent_minutes(load, key_56d)
    if longest_56 is not None and not (race is not None and abs(longest_56 - race) <= 1):
        return longest_56 * LONG_FALLBACK_56D_FACTOR, False, "70% of the 56-day longest (no recent long session)"
    return float(fallback_min), False, f"conservative {fallback_min}' start (no long-session history)"


def _long_ride_minutes(context: dict[str, Any]) -> int:
    rule = LONG_RIDE_RULES.get(context["bucket"], DEFAULT_LONG_RIDE_RULE)
    load = context["training_load"].get("ride_load") or {}
    reference, recent, basis = _long_reference(
        load, "longest_ride_min_28d", "longest_ride_min_56d", LONG_RIDE_FALLBACK_MIN, []
    )
    context["long_ride_reference"] = reference if recent else None
    if not recent:
        context["notes"].append(f"Long ride starts from a {basis}.")
        return _round_to_5(min(reference, rule[2]) * _non_progress_factor(context))
    return progressed_long_minutes(reference, rule, context)


def _non_progress_factor(context: dict[str, Any]) -> float:
    if context["taper"]:
        return 0.5 if context["race_week"] else TAPER_LONG_FACTOR
    if context["recovery_week"]:
        return RECOVERY_LONG_FACTOR
    return 1.0


def _long_run_minutes(context: dict[str, Any]) -> int:
    rule = LONG_RUN_RULES.get(context["bucket"], DEFAULT_LONG_RUN_RULE)
    load = context["training_load"].get("run_load") or {}
    caps = [
        context["week_caps"].get("max_long_run_min"),
        context["return_from_break"].get("long_run_cap_min")
        if context["returning"]
        else None,
    ]
    reference, recent, basis = _long_reference(
        load, "longest_run_min_28d", "longest_run_min_56d", LONG_RUN_FALLBACK_MIN, caps
    )
    context["long_run_reference"] = reference if recent else None
    if not recent:
        context["notes"].append(f"Long run starts from the {basis}: no template value after a gap.")
        minutes = _round_to_5(min(reference, rule[2]) * _non_progress_factor(context))
    else:
        minutes = progressed_long_minutes(reference, rule, context)
    hard_caps = [float(value) for value in caps if _safe_number(value)]
    if hard_caps and minutes > min(hard_caps):
        minutes = int(min(hard_caps) // 5 * 5)
        context["notes"].append(
            f"Long run capped at {minutes}' by the run-load / re-entry caps."
        )
    return max(20, minutes)


def _weekly_elevation_target(context: dict[str, Any]) -> int | None:
    """Weekly D+ for trail buckets, progressing <= ~15% a week from the recent average."""
    if not context["trail"] or context["transition"]:
        return None
    run_load = context["training_load"].get("run_load") or {}
    recent = _recent_minutes(run_load, "avg_weekly_elevation_gain_m_4w")
    default = 600 if context["bucket"] == "ultra_trail" else 400
    growth = 1 + min(0.12, MAX_WEEKLY_ELEVATION_GROWTH_PCT / 100)
    if context["returning"]:
        # Re-entry: climbing follows the volume cap of the return ramp.
        pct = _safe_number(context["return_from_break"].get("volume_cap_pct")) or 50
        target = (recent or default * 0.7) * pct / 100
    elif recent is None:
        target = default * (0.7 if context["phase"] == "base" else 1.0)
    elif context["taper"] or context["recovery_week"]:
        target = recent * (0.6 if context["taper"] else 0.7)
    elif context["absorb_week"] or context["readiness"] != "green" or context["holiday"]:
        target = recent
    else:
        target = recent * growth
    if context["trail_camp"]:
        target = max(target, (recent or default) * growth)
    return int(round(target / 50.0) * 50)


def _weekly_descent_target(context: dict[str, Any], progress: bool) -> int | None:
    """Weekly descent (D-) budget: recent descent, +~10% only when it may progress,
    never above `week_caps.max_descent_m`, zero with a run-impact niggle."""
    if not context["trail"] or context["transition"]:
        return None
    if context["impact_niggles"]:
        return 0
    run_load = context["training_load"].get("run_load") or {}
    recent = _recent_minutes(run_load, "avg_weekly_elevation_loss_m_4w")
    gain = context.get("elevation_target_m") or 0
    if recent is None:
        target = gain * 0.5
    elif context["taper"] or context["recovery_week"]:
        target = recent * 0.6
    elif progress:
        target = recent * 1.10
    else:
        target = recent
    cap = _safe_number(context["week_caps"].get("max_descent_m"))
    if cap is not None:
        target = min(target, cap)
    return int(min(target, gain or target) // 50 * 50)


def _target_frequency(
    snapshot: dict[str, Any], context: dict[str, Any]
) -> dict[str, int]:
    profile_frequency = snapshot.get("athlete_profile", {}).get(
        "weekly_sport_frequency", {}
    )
    if context["transition"]:
        return {"rides": 1, "runs": 2, "strength": 1}
    if context["holiday"]:
        # Maintenance: 2-3 short easy sessions plus one optional long session.
        return {"rides": 1, "runs": 2, "strength": 1}

    def _int(key: str, default: int) -> int:
        try:
            return int(profile_frequency.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    strength = max(1, _int("strength_sessions", 1))
    if context["recovery_week"]:
        return {"rides": 2, "runs": 2, "strength": 1}
    return {
        "rides": max(2, _int("bike_sessions", 3)),
        "runs": max(2, _int("run_sessions", 3)),
        "strength": strength,
    }


def strength_prescription(context: dict[str, Any]) -> dict[str, Any]:
    """Strength by phase: heavy in base, maintenance + eccentric in build, activation in taper."""
    light = (
        context["recovery_week"]
        or context["readiness"] != "green"
        or context.get("holiday")
        or context.get("returning")
        or context["readiness"] != "green"
    )
    if context["taper"] or context["race_week"]:
        return {
            "sessions": 1,
            "duration": ACTIVATION_STRENGTH_MIN,
            "name": "Activation Strength",
            "intensity": "Activation only: 1-2 easy sets, finish fresher than you started",
            "target": "Glute/calf activation, light single-leg balance, core",
        }
    if light:
        return {
            "sessions": 1,
            "duration": LIGHT_STRENGTH_MAX_MIN,
            "name": "Light Strength & Mobility",
            "intensity": "1-2 sets, well short of fatigue",
            "target": "Mobility, single-leg control, core, light hip/calf work",
        }
    eccentric = (
        " + eccentric/downhill prep: slow step-downs, eccentric squats, single-leg RDL"
        if context["trail"]
        else ""
    )
    if context["phase"] == "base":
        return {
            "sessions": min(2, context.get("strength_frequency", 1)),
            "duration": 40,
            "name": "Strength (Heavy)",
            "intensity": "3-4 sets of 4-6 reps, 2 reps in reserve",
            "target": "Squat, hinge, single-leg, calf, hip abduction" + eccentric,
        }
    if context["phase"] == "race_specific":
        return {
            "sessions": 1,
            "duration": 25,
            "name": "Strength Maintenance",
            "intensity": "2 sets of 4-6 reps, crisp, no soreness",
            "target": "Squat or trap-bar, single-leg, calf" + eccentric,
        }
    return {
        "sessions": 1 if not context["trail"] else min(2, context.get("strength_frequency", 1)),
        "duration": 30,
        "name": "Strength Maintenance" + (" + Eccentric" if context["trail"] else ""),
        "intensity": "2-3 sets of 5-8 reps, 2 reps in reserve",
        "target": "Squat, hinge, single-leg, calf" + eccentric,
    }


def _budget_take(context: dict[str, Any], element: str, impact: bool) -> bool:
    """Progression budget: at most 2 progressing elements, at most 1 run-impact one.

    Exempt when underload is detected, readiness is green and nothing (ITB
    history, niggle, pain score) is flagged. Held elements are recorded.
    """
    used = context["progressions"]
    if not context["budget_exempt"]:
        impact_used = sum(1 for item in used if item["impact"])
        if len(used) >= PROGRESSION_BUDGET_MAX or (
            impact and impact_used >= PROGRESSION_BUDGET_MAX_IMPACT
        ):
            context["held"].append(element)
            return False
    used.append({"element": element, "impact": impact})
    return True


def _medium_long_plan(context: dict[str, Any], long_run: int) -> tuple[int, int]:
    """(this week's medium-long run, last week's) on the gradual back-to-back ramp."""
    weeks = context["load_weeks"]
    ceiling = max(MEDIUM_LONG_START_MIN, _round_to_5(long_run * MEDIUM_LONG_SHARE))
    now = min(ceiling, MEDIUM_LONG_START_MIN + MEDIUM_LONG_STEP_MIN * weeks)
    before = (
        0 if weeks == 0 else min(ceiling, MEDIUM_LONG_START_MIN + MEDIUM_LONG_STEP_MIN * (weeks - 1))
    )
    return now, before


def _plan_progressions(
    context: dict[str, Any],
    progression: dict[str, Any],
    frequency: dict[str, int],
    *,
    has_long_run: bool,
    long_ride_possible: bool,
    load_week: bool,
) -> dict[str, Any]:
    """Decide what progresses this week: one thing at a time.

    Every candidate (quality rung steps, long-session duration, the Saturday
    medium-long run, descent) asks the progression budget; what does not fit
    holds at last week's dose. Trail weeks alternate the run-impact slot
    between the long run and the medium-long run.
    """
    long_run = _long_run_minutes(context) if has_long_run else 0
    run_reference = context.get("long_run_reference")
    back_to_back = (
        context["trail"]
        and context["phase"] in LOAD_PHASES
        and load_week
        and long_ride_possible
        and not context["impact_niggles"]
    )
    medium = 0
    would_grow = (
        run_reference is not None and has_long_run and long_run > run_reference + LONG_PROGRESSION_MIN
    )
    long_run_grows = would_grow and not context["post_race_hold"] and not context["impact_niggles"]
    if would_grow and not long_run_grows:
        # Recovering from a race or a niggle: the long run holds.
        long_run = _round_to_5(run_reference)
    if back_to_back:
        medium, medium_before = _medium_long_plan(context, long_run)
        medium_grows = medium > medium_before
        impact_order = (
            [("medium_long_run", medium_grows), ("long_run", long_run_grows)]
            if context["load_weeks"] % 2 == 0
            else [("long_run", long_run_grows), ("medium_long_run", medium_grows)]
        )
    else:
        medium_before = 0
        impact_order = [("long_run", long_run_grows)]
    run_fixed = int(has_long_run) + int(back_to_back)
    run_slots = max(0, frequency["runs"] - run_fixed - 1)
    if back_to_back:
        run_slots = min(run_slots, 1)
    families = _quality_families(context, run_slots)
    rungs: dict[str, tuple[int, bool, int | None]] = {}
    for family in families:
        last = (progression.get("last_dose_by_family") or {}).get(family)
        rung = next_rung(
            family,
            last,
            phase=context["phase"],
            block_week=context["block_week"],
            underload_status=context["underload_status"],
            readiness_status=context["readiness"],
            planning_date=context["planning_date"],
        )
        current = current_rung(family, last)
        rungs[family] = (rung, current is not None and rung > current, current)

    if context["triathlon"]:
        order = [("quality:0", False), ("long_ride", False), ("quality:1", False), *impact_order]
    else:
        order = [*impact_order[:1], ("quality:0", False), *impact_order[1:], ("quality:1", False), ("long_ride", False)]
    long_ride = None
    for element, grows in order:
        if element.startswith("quality:"):
            index = int(element.split(":")[1])
            if index >= len(families):
                continue
            family = families[index]
            rung, steps_up, current = rungs[family]
            if steps_up and not _budget_take(context, f"{family} rung", False):
                rungs[family] = (current if current is not None else rung, False, current)
        elif element == "long_run":
            if grows and not _budget_take(context, "long run duration", True):
                long_run = _round_to_5(run_reference)
        elif element == "medium_long_run":
            if back_to_back and grows and not _budget_take(context, "medium-long run", True):
                medium = medium_before
        elif element == "long_ride" and long_ride_possible and not back_to_back:
            long_ride = _long_ride_minutes(context)
            reference = context.get("long_ride_reference")
            if (
                reference is not None
                and long_ride > reference + LONG_PROGRESSION_MIN
                and not _budget_take(context, "long ride duration", False)
            ):
                long_ride = _round_to_5(reference)
    if back_to_back and medium < MEDIUM_LONG_START_MIN:
        # The Saturday run was not introduced this week: keep the long ride.
        back_to_back = False
        context["notes"].append(
            "Back-to-back weekend postponed one week: the long run took this week's run-impact progression."
        )
        long_ride = _long_ride_minutes(context)
    if long_ride is None and long_ride_possible and not back_to_back:
        long_ride = _long_ride_minutes(context)
    run_load = context["training_load"].get("run_load") or {}
    descent_grows = (
        context["trail"]
        and load_week
        and _recent_minutes(run_load, "avg_weekly_elevation_loss_m_4w") is not None
        and not context["impact_niggles"]
        and not context["returning"]
    )
    descent_progress = bool(descent_grows) and _budget_take(context, "descent", True)
    context["descent_progress"] = descent_progress
    context["descent_target_m"] = _weekly_descent_target(context, descent_progress)
    if context["held"]:
        context["notes"].append(
            "One thing at a time: held at last week's dose - " + ", ".join(context["held"]) + "."
        )
    return {
        "long_run": long_run,
        "long_ride": long_ride,
        "medium_long": medium if back_to_back else 0,
        "back_to_back": back_to_back,
        "families": families,
        "rungs": {family: value[0] for family, value in rungs.items()},
    }


def _build_specs(
    snapshot: dict[str, Any],
    context: dict[str, Any],
    progression: dict[str, Any],
) -> list[dict[str, Any]]:
    constraints = context["constraints"]
    notes: list[str] = context["notes"]
    warnings: list[str] = context["warnings"]
    athlete_memory = snapshot.get("athlete_memory", {})
    day_lookup = _day_map(snapshot)
    club_swim_duration, club_swim_distance = _club_swim_preferences(snapshot)
    club_days = set(club_swim_days(snapshot))

    rest_day = (constraints.get("days_off") or ["Monday"])[0]
    long_ride_day = constraints.get("preferred_long_ride_day") or "Saturday"
    long_run_day = constraints.get("preferred_long_run_day") or "Sunday"
    strength_days = list(constraints.get("strength_days") or ["Friday"])
    for name in (rest_day, long_ride_day, long_run_day, *strength_days):
        if name not in day_lookup:
            raise ValueError(f"Unknown planning day: {name}")

    week_races = context["week_races"]
    race_days = set(week_races)
    eve_days = {
        DAY_NAMES[DAY_NAMES.index(day) - 1] for day in race_days if DAY_NAMES.index(day) > 0
    } - race_days
    after_days = {
        DAY_NAMES[DAY_NAMES.index(day) + 1] for day in race_days if DAY_NAMES.index(day) < 6
    } - race_days
    windows = context["race_windows"]
    light_days = {DAY_NAMES[index] for index in windows.get("light", {})} - race_days
    easy_after_days = {DAY_NAMES[index] for index in windows.get("easy_after", {})} - race_days
    tennis = set(context["tennis_days"]) - race_days
    travel = set(context["travel_days"])
    after_tennis = {
        DAY_NAMES[DAY_NAMES.index(day) + 1] for day in tennis if DAY_NAMES.index(day) < 6
    }
    load_week = not context["recovery_week"] and not context["taper"] and not context["holiday"]
    frequency = _target_frequency(snapshot, context)
    context["strength_frequency"] = frequency["strength"]

    specs: list[dict[str, Any]] = []
    if rest_day not in race_days:
        _add_spec(specs, kind="recovery", day=rest_day, duration=20, min_duration=15)
    for day, race_item in week_races.items():
        duration = race_duration_estimate(race_item)
        _add_spec(
            specs, kind="race", day=day, duration=duration, fixed_duration=True, race=race_item
        )
        priority = race_item.get("priority", "C")
        if priority == "B":
            rule = "mini-taper: lighter days before, last quality 72h+ before, an opener the day before; easy days after."
        elif priority == "C":
            rule = "train through: nothing hard the day before, an easy day after."
        else:
            rule = "short opener the day before, nothing hard around it, easy or off the day after."
        notes.append(f"{race_item.get('name', 'Race')} ({priority}-race) on {day}: {rule}")
    for day in sorted(tennis, key=DAY_NAMES.index):
        if day in eve_days:
            notes.append(f"Tennis on {day} is the eve of a race: skip it or keep it very light.")
            continue
        _add_spec(specs, kind="tennis", day=day, duration=60, fixed_duration=True)

    race_window = race_days | eve_days | after_days | light_days | easy_after_days
    if light_days - eve_days:
        notes.append(
            "Race mini-taper: no quality and no long session on "
            + ", ".join(sorted(light_days, key=DAY_NAMES.index))
            + "."
        )
    if easy_after_days - after_days:
        notes.append(
            "Post-race recovery: easy only on "
            + ", ".join(sorted(easy_after_days, key=DAY_NAMES.index))
            + " (scaled to the race duration)."
        )
    has_long_run = not context["transition"] and long_run_day not in race_window
    long_ride_possible = (
        not context["transition"] and not context["holiday"] and long_ride_day not in race_window
    )
    plan = _plan_progressions(
        context,
        progression,
        frequency,
        has_long_run=has_long_run,
        long_ride_possible=long_ride_possible,
        load_week=load_week,
    )
    trail_back_to_back = plan["back_to_back"]
    context["trail_back_to_back"] = trail_back_to_back
    long_run_duration = plan["long_run"]
    if context["transition"]:
        notes.append("Transition week: no long sessions, no quality, everything optional.")
    else:
        if long_ride_day in race_window:
            notes.append(f"No long session on {long_ride_day}: it sits next to a race.")
        elif trail_back_to_back:
            medium = plan["medium_long"]
            _add_spec(
                specs,
                kind="medium_long_run",
                day=long_ride_day,
                duration=medium,
                min_duration=min(30, medium),
            )
            notes.append(
                f"Back-to-back weekend: {long_ride_day} medium-long run ({medium}', built up gradually), "
                f"{long_run_day} long run; the bike becomes midweek aerobic support."
            )
        elif plan["long_ride"]:
            ride_duration = plan["long_ride"]
            _add_spec(
                specs,
                kind="long_ride",
                day=long_ride_day,
                duration=ride_duration,
                min_duration=min(90, ride_duration),
            )
        if long_run_day in race_window:
            notes.append(f"No long run on {long_run_day}: it sits next to a race.")
        elif has_long_run and long_run_duration:
            _add_spec(
                specs,
                kind="long_run",
                day=long_run_day,
                duration=long_run_duration,
                min_duration=min(45, long_run_duration),
            )
    for day in sorted(eve_days, key=DAY_NAMES.index):
        if day != rest_day and day not in tennis:
            _add_spec(specs, kind="opener", day=day, duration=25, min_duration=20)
    for day in sorted(after_days, key=DAY_NAMES.index):
        if day == rest_day:
            continue
        race_before = week_races[DAY_NAMES[DAY_NAMES.index(day) - 1]]
        if race_duration_estimate(race_before) > 90:
            _add_spec(specs, kind="recovery", day=day, duration=20, min_duration=15)
        else:
            _add_spec(specs, kind="easy_run", day=day, duration=30, min_duration=20)
    for swim_day in constraints.get("fixed_swim_days", []):
        if swim_day not in day_lookup or swim_day in race_days or swim_day in eve_days:
            continue
        club = swim_day in club_days
        _add_spec(
            specs,
            kind="swim_club" if club else "swim_pool",
            day=swim_day,
            duration=club_swim_duration if club else 45,
            min_duration=club_swim_duration if club else 30,
            club_session_distance_m=club_swim_distance if club else None,
        )

    blocked = {rest_day, long_ride_day, long_run_day} | race_window

    def _prefer(days: list[str]) -> list[str]:
        # Tennis and travel days are last resorts for any added session.
        return [d for d in days if d not in tennis | travel] + [
            d for d in days if d in tennis | travel
        ]

    ride_days = _prefer(
        _sort_days_by_memory(
            _ordered_days(
                constraints.get("preferred_ride_days", []),
                ["Wednesday", "Tuesday", "Friday", "Thursday"],
            ),
            athlete_memory,
        )
    )
    run_days = _prefer(
        _sort_days_by_memory(
            _ordered_days(
                constraints.get("preferred_run_days", []),
                ["Thursday", "Tuesday", "Friday", "Wednesday"],
            ),
            athlete_memory,
        )
    )

    quality_blocked = blocked | tennis | travel
    if context["race_week"]:
        # The opener-style quality touch lands at least 3 days before the race.
        for race_day in race_days:
            index = DAY_NAMES.index(race_day)
            quality_blocked |= {DAY_NAMES[i] for i in range(max(0, index - 2), index)}
    # No quality the day after the previous week's long/hard Sunday (week boundary).
    if _previous_week_ended_hard(snapshot):
        quality_blocked.add("Monday")
    quality_days: set[str] = set()
    for position, family in enumerate(plan["families"]):
        preferred = ride_days if family.startswith("bike") else run_days
        # With 2+ quality sessions the first goes early in the week, so the next
        # one still finds a day that is not next to it.
        candidates = (
            sorted(preferred, key=DAY_NAMES.index)
            if position == 0 and len(plan["families"]) > 1
            else preferred
        )
        ride = family.startswith("bike")
        label = LADDERS[family]["label"]
        rung = plan["rungs"][family]
        if context["taper"]:
            # Short intensity touches: same intensity, less time-in-zone.
            rung = 0 if context["race_week"] else max(0, rung - 2)
        dose = ladder_dose(family, rung)
        if context["race_week"]:
            # Race week: half the repeats of the entry rung, race-sharp but short.
            repeats = max(2, dose["repeats"] // 2)
            dose = {**dose, "repeats": repeats, "time_in_zone_min": repeats * dose["rep_min"]}
        duration = 25 + dose["repeats"] * (dose["rep_min"] + dose["recovery_min"])
        day = _pick_day(
            candidates,
            specs=specs,
            constraints=constraints,
            blocked=quality_blocked | quality_days | (set() if ride else after_tennis),
            minimum_minutes=duration,
            ride_session=ride,
            allowed_pair_kinds={"swim_club", "swim_pool"},
            avoid_adjacent_to=quality_days | {long_ride_day},
            notes=notes,
            label=label,
        )
        # The second quality session never sits next to the first; a third one
        # (allowance 3, underload) may, with the compromise noted.
        next_to_quality = day is not None and len(quality_days) < 2 and any(
            abs(DAY_NAMES.index(day) - DAY_NAMES.index(other)) == 1 for other in quality_days
        )
        if day is None or next_to_quality:
            warnings.append(
                f"No suitable day for the {label} session this week; it was dropped rather than stacked."
            )
            continue
        quality_days.add(day)
        _add_spec(
            specs,
            kind="quality_ride" if ride else "quality_run",
            day=day,
            duration=duration,
            fixed_duration=True,
            dose=dose,
        )

    if _friday_support_ride_requested(snapshot) and not context["recovery_week"]:
        if _day_available(
            "Friday",
            specs=specs,
            constraints=constraints,
            minimum_minutes=55,
            ride_session=True,
            allowed_pair_kinds={"easy_run", "strength"},
        ) and "Friday" not in blocked | travel:
            # Athlete-requested support ride: kept as asked.
            _add_spec(
                specs, kind="endurance_ride", day="Friday", duration=55, min_duration=45,
                fixed_duration=True,
            )

    ride_duration = 60
    if context["transition"] or context["holiday"]:
        ride_duration = 45
    elif context["recovery_week"]:
        ride_duration = 50
    elif trail_back_to_back:
        ride_duration = 75
    while sum(1 for spec in specs if spec["kind"] in RIDE_KINDS) < frequency["rides"]:
        day = _pick_day(
            _prefer(_ordered_days(["Friday", "Wednesday", "Tuesday", "Thursday"], ride_days)),
            specs=specs,
            constraints=constraints,
            blocked=(blocked - easy_after_days - light_days) | race_days | travel,
            minimum_minutes=45,
            ride_session=True,
            # A short spin may join a quality-run day (hard days hard, easy days easy).
            allowed_pair_kinds={"easy_run", "strength", "quality_run"},
        )
        if day is None:
            break
        _add_spec(specs, kind="endurance_ride", day=day, duration=ride_duration, min_duration=40)

    run_duration = 45
    if context["transition"] or context["holiday"]:
        run_duration = 30 if context["transition"] else 40
    elif context["recovery_week"]:
        run_duration = 35
    while sum(1 for spec in specs if spec["kind"] in RUN_KINDS) < frequency["runs"]:
        day = _pick_day(
            _prefer(_ordered_days(["Tuesday", "Thursday", "Wednesday", "Friday"], run_days)),
            specs=specs,
            constraints=constraints,
            blocked=(blocked - easy_after_days - light_days) | race_days | eve_days,
            minimum_minutes=30,
            ride_session=False,
            allowed_pair_kinds={"endurance_ride", "strength", "quality_ride"},
        )
        if day is None:
            break
        # Travel days get a short run that fits a hotel morning; light days stay short.
        duration = 30 if day in travel | light_days | easy_after_days else run_duration
        _add_spec(specs, kind="easy_run", day=day, duration=duration, min_duration=min(30, duration))

    for kind, key, label in (
        (RIDE_KINDS, "rides", "rides"),
        (RUN_KINDS, "runs", "runs"),
    ):
        placed = sum(1 for spec in specs if spec["kind"] in kind)
        if placed < frequency[key] and not context["week_races"]:
            warnings.append(
                f"Only {placed} of the {frequency[key]} requested {label} fit this week's day constraints."
            )

    _place_strength(specs, context, constraints, strength_days, blocked, notes)
    _add_brick(specs, context, constraints, long_ride_day, warnings)
    _apply_taper_volume(specs, context)

    midweek_ride = next(
        (spec["day"] for spec in specs if spec["kind"] in {"quality_ride", "endurance_ride"}),
        None,
    )
    _apply_threshold_tests(
        specs,
        context.get("macro", {}),
        midweek_ride_day=midweek_ride,
        long_ride_day=long_ride_day,
        long_run_day=long_run_day,
    )
    _swap_impact_sessions(specs, context, constraints)
    _apply_run_caps(specs, context)
    _apply_growth_guard(specs, context, snapshot)
    _fit_day_caps(specs, constraints)
    _apply_hard_cap(specs, context)
    # Daniels caps last: they depend on the final weekly run minutes.
    _apply_daniels_caps(specs, context, notes)
    return specs


def _previous_week_ended_hard(snapshot: dict[str, Any]) -> bool:
    """The day before week_start held a long or quality session (prior plan info)."""
    try:
        eve = (parse_date(str(snapshot.get("week_start"))) - timedelta(days=1)).isoformat()
    except (TypeError, ValueError):
        return False
    for session in previous_week_sessions(snapshot):
        if str(session.get("date") or "")[:10] != eve:
            continue
        if session.get("status") == "missed":
            continue
        if is_hard_session(session):
            return True
    return False


def previous_week_sessions(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Sessions of earlier plans found in the snapshot (final plans or reviews)."""
    sessions: list[dict[str, Any]] = []
    for item in snapshot.get("previous_week_sessions") or []:
        if isinstance(item, dict):
            sessions.append(item)
    for week in snapshot.get("prior_plan_history") or []:
        if not isinstance(week, dict):
            continue
        sessions.extend((week.get("final_plan") or {}).get("sessions", []) or [])
        sessions.extend(week.get("session_reviews", []) or [])
    return sessions


def is_hard_session(session: dict[str, Any]) -> bool:

    if session.get("progression") or session.get("threshold_test") or session.get("race_day"):
        return True
    if session.get("daniels_type") in {"T", "I", "R"}:
        return True
    if str(session.get("priority") or "") == "key" and session.get("discipline") in {"Run", "Ride"}:
        return True
    minutes = session.get("duration_min") or session.get("planned_minutes")
    probe = {**session, "duration_min": minutes}
    return is_long_session(probe) or bool(session_dose(session))


def _strength_is_heavy(prescription: dict[str, Any]) -> bool:
    return "heavy" in str(prescription.get("name", "")).lower()


def _place_strength(
    specs: list[dict[str, Any]],
    context: dict[str, Any],
    constraints: dict[str, Any],
    strength_days: list[str],
    blocked: set[str],
    notes: list[str],
) -> None:
    """Strength by phase, never heavy legs the day before quality or a long session.

    Heavy work first tries the listed strength days, then a quality day (hard
    days hard, same-day double); when only an eve-of-key day is left it is
    downgraded to maintenance and the note says so.
    """
    prescription = strength_prescription(context)
    key_days = {
        spec["day"]
        for spec in specs
        if spec["kind"] in QUALITY_KINDS | {"long_run", "medium_long_run", "race"}
        or (spec["kind"] == "long_ride" and not context["trail"])
    }
    eves = {DAY_NAMES[DAY_NAMES.index(day) - 1] for day in key_days if DAY_NAMES.index(day) > 0}
    heavy = _strength_is_heavy(prescription)
    quality_days = [spec["day"] for spec in specs if spec["kind"] in QUALITY_KINDS]
    candidates = list(strength_days)
    if heavy:
        candidates += [day for day in quality_days if day not in candidates]
    placed = 0
    downgraded = False
    for strength_day in candidates:
        if placed >= prescription["sessions"]:
            break
        taken = any(spec["day"] == strength_day for spec in specs)
        no_double = (
            constraints.get("double_session_capacity_by_day", {}).get(strength_day) is False
        )
        if strength_day in blocked or (taken and no_double):
            continue
        day_count = sum(1 for spec in specs if spec["day"] == strength_day)
        if any(spec["day"] == strength_day and spec["kind"] == "strength" for spec in specs) or (
            strength_day not in strength_days and day_count >= 2
        ):
            continue
        plan = prescription
        if heavy and strength_day in eves:
            if any(day not in eves for day in candidates[candidates.index(strength_day) + 1:]):
                continue
            plan = {
                **prescription,
                "name": "Strength Maintenance",
                "duration": 30,
                "intensity": "2 sets of 5-6 reps, crisp, no soreness (the next day is a key session)",
            }
            downgraded = True
        _add_spec(
            specs,
            kind="strength",
            day=strength_day,
            duration=plan["duration"],
            min_duration=min(20, plan["duration"]),
            strength=plan,
        )
        placed += 1
    if downgraded:
        notes.append(
            "Heavy strength would sit the day before a key session, so it is maintenance-only this week."
        )
    if placed < prescription["sessions"]:
        notes.append(
            f"{prescription['name']}: {placed} of {prescription['sessions']} sessions fit the listed strength days."
        )


def _swap_impact_sessions(
    specs: list[dict[str, Any]], context: dict[str, Any], constraints: dict[str, Any]
) -> None:
    """Run-impact niggle: running becomes low-impact cross-training, no descent."""
    parts = context["impact_niggles"]
    if not parts:
        return
    swapped = []
    no_bike = set(constraints.get("no_bike_days", []))
    for spec in list(specs):
        if spec["kind"] in {"brick_run", "run_threshold_test"}:
            specs.remove(spec)
            swapped.append(spec["kind"].replace("_", " "))
            continue
        if spec["kind"] not in {"easy_run", "long_run", "medium_long_run", "opener"}:
            continue
        swapped.append(spec["kind"].replace("_", " "))
        original = spec["kind"]
        spec["kind"] = "cross_training"
        spec["swapped_from"] = original
        spec["aqua_jog"] = spec["day"] in no_bike
        if original == "long_run":
            spec["duration"] = max(60, spec["duration"])
    if swapped:
        context["notes"].append(
            f"Pain reported ({', '.join(parts)}): {', '.join(dict.fromkeys(swapped))} swapped to "
            "low-impact cross-training (easy bike or aqua jogging); no descending this week."
        )


def _run_minutes(specs: list[dict[str, Any]]) -> int:
    return sum(
        spec["duration"]
        for spec in specs
        if spec["kind"] in RUN_KINDS | {"brick_run", "opener"}
    )


def run_pace_min_per_km(training_load: dict[str, Any]) -> float:
    run_load = training_load.get("run_load") or {}
    minutes = _safe_number(run_load.get("avg_weekly_run_min_4w"))
    km = _safe_number(run_load.get("avg_weekly_run_km_4w"))
    if minutes and km and km > 0:
        return minutes / km
    return DEFAULT_RUN_MIN_PER_KM


def _run_minute_cap(context: dict[str, Any]) -> tuple[float | None, str]:
    """Binding weekly run minutes: week caps, re-entry caps, or recent +10%."""
    caps: list[tuple[float, str]] = []
    week_caps = context["week_caps"]
    pace = run_pace_min_per_km(context["training_load"])
    if _safe_number(week_caps.get("max_run_min")):
        caps.append((float(week_caps["max_run_min"]), "run-load week cap"))
    if _safe_number(week_caps.get("max_run_km")):
        caps.append((float(week_caps["max_run_km"]) * pace, "run-km week cap"))
    rfb = context["return_from_break"]
    if context["returning"] and _safe_number(rfb.get("run_km_cap")):
        caps.append((float(rfb["run_km_cap"]) * pace, "re-entry run-km cap"))
    if not caps:
        recent = _recent_minutes(context["training_load"].get("run_load") or {}, "avg_weekly_run_min_4w")
        if recent and not context["budget_exempt"]:
            caps.append((recent * (1 + MAX_WEEKLY_RUN_GROWTH_PCT / 100), "recent run minutes +10%"))
    if not caps:
        return None, ""
    return min(caps, key=lambda item: item[0])


def _apply_run_caps(specs: list[dict[str, Any]], context: dict[str, Any]) -> None:
    """Run volume stays inside the run-load caps.

    Easy runs shorten to 30' first, then extra easy runs go (one genuinely easy
    run always stays), then the medium-long run shortens, and the long run is
    trimmed last - never below the recent longest run unless a re-entry or
    week cap says so.
    """
    cap, basis = _run_minute_cap(context)
    if cap is None or _run_minutes(specs) <= cap:
        return
    before = _run_minutes(specs)

    def trim(kinds: set[str], floor_for: Any) -> None:
        for spec in sorted(
            (spec for spec in specs if spec["kind"] in kinds), key=lambda item: -item["duration"]
        ):
            excess = _run_minutes(specs) - cap
            if excess <= 0:
                return
            floor = floor_for(spec)
            cut = min(excess, spec["duration"] - floor)
            if cut > 0:
                spec["duration"] = max(floor, _round_to_5(spec["duration"] - cut))
                spec["min_duration"] = min(spec["min_duration"], spec["duration"])

    trim({"easy_run"}, lambda spec: 30)
    easy_runs = [spec for spec in specs if spec["kind"] == "easy_run"]
    while _run_minutes(specs) > cap and len(easy_runs) > 1:
        specs.remove(easy_runs.pop())
    trim({"medium_long_run"}, lambda spec: 30)
    reference = context.get("long_run_reference")
    capped_long = any(
        _safe_number(value)
        for value in (
            context["week_caps"].get("max_long_run_min"),
            context["return_from_break"].get("long_run_cap_min") if context["returning"] else None,
        )
    )
    trim(
        {"long_run"},
        lambda spec: spec["min_duration"]
        if capped_long or not reference
        else min(spec["duration"], _round_to_5(reference)),
    )
    message = f"Run volume trimmed from {before}' to {_run_minutes(specs)}' to respect the {basis} (~{cap:.0f}')"
    if _run_minutes(specs) > cap:
        message += "; the long run holds at its recent length, so the week sits slightly above it"
    context["notes"].append(message + ".")


def _planned_training_minutes(specs: list[dict[str, Any]]) -> int:
    """Same total the audit compares: no races, no rest/mobility."""
    return sum(spec["duration"] for spec in specs if spec["kind"] not in {"race", "recovery"})


def _apply_growth_guard(
    specs: list[dict[str, Any]], context: dict[str, Any], snapshot: dict[str, Any]
) -> None:
    """Weekly total grows <= ~15% over the recent loaded average (same limit as
    the validator and the audit). Easy volume is shortened first; if the key and
    long sessions alone exceed it, the plan says so instead of hiding it."""
    baseline = baseline_week_minutes(snapshot)
    if not baseline or context["budget_exempt"] or context["taper"] or context["recovery_week"]:
        return
    limit = baseline * (1 + MAX_WEEKLY_TOTAL_GROWTH_PCT / 100)
    before = _planned_training_minutes(specs)
    if before <= limit:
        return
    floors = {"easy_run": 30, "endurance_ride": 45, "swim_pool": 30, "cross_training": 30}
    for spec in sorted(
        (spec for spec in specs if spec["kind"] in floors and not spec.get("fixed_duration")),
        key=lambda item: -item["duration"],
    ):
        excess = _planned_training_minutes(specs) - limit
        if excess <= 0:
            break
        cut = min(excess, spec["duration"] - floors[spec["kind"]])
        if cut > 0:
            spec["duration"] = max(floors[spec["kind"]], _round_to_5(spec["duration"] - cut))
            spec["min_duration"] = min(spec["min_duration"], spec["duration"])
    after = _planned_training_minutes(specs)
    message = (
        f"Weekly growth limit: recent loaded weeks average {baseline:.0f}', so ~{limit:.0f}' is the "
        f"+{MAX_WEEKLY_TOTAL_GROWTH_PCT:g}% ceiling; easy volume trimmed from {before}' to {after}'."
    )
    if after > limit:
        message += " The key and long sessions alone still exceed it: check life load and sleep before adding more."
    context["notes"].append(message)


def _apply_hard_cap(specs: list[dict[str, Any]], context: dict[str, Any]) -> None:
    """The check-in's available hours bind: cut easy volume first, then the
    secondary quality, and trim the long sessions last. Say what was cut.
    Fixed swim-routine days are shortened, never removed."""
    cap = context["hard_cap_min"]
    if not cap:
        return
    fixed_swims = set(context["constraints"].get("fixed_swim_days", []) or [])

    def total() -> int:
        return sum(spec["duration"] for spec in specs)

    if total() <= cap:
        return
    before = total()
    cuts: list[str] = []
    easy = ("easy_run", "endurance_ride", "swim_pool", "cross_training", "recovery", "strength", "opener")
    for spec in sorted(
        (spec for spec in specs if spec["kind"] in easy and not spec.get("fixed_duration")),
        key=lambda item: (reduction_priority(item["kind"]), -item["duration"]),
    ):
        excess = total() - cap
        if excess <= 0:
            break
        cut = min(excess, spec["duration"] - spec["min_duration"])
        if cut > 0:
            spec["duration"] -= cut
            cuts.append(f"{spec['kind'].replace('_', ' ')} on {spec['day']} -{cut}'")
    removable = [
        spec
        for spec in specs
        if spec["kind"] in {"easy_run", "endurance_ride", "swim_pool", "cross_training", "brick_run"}
        and not (spec["kind"] == "swim_pool" and spec["day"] in fixed_swims)
    ]
    easy_runs = [spec for spec in removable if spec["kind"] == "easy_run"]
    for spec in sorted(removable, key=lambda item: reduction_priority(item["kind"])):
        if total() <= cap:
            break
        if spec["kind"] == "easy_run" and len(easy_runs) <= 1:
            continue
        specs.remove(spec)
        if spec in easy_runs:
            easy_runs.remove(spec)
        cuts.append(f"{spec['kind'].replace('_', ' ')} on {spec['day']} removed")
    quality = [spec for spec in specs if spec["kind"] in QUALITY_KINDS]
    if total() > cap and len(quality) > 1:
        secondary = quality[-1]
        specs.remove(secondary)
        cuts.append(f"secondary quality ({secondary['dose']['label']}) on {secondary['day']} removed")
    for kind in ("medium_long_run", "long_ride", "long_run"):
        for spec in [spec for spec in specs if spec["kind"] == kind]:
            excess = total() - cap
            if excess <= 0:
                break
            cut = min(excess, spec["duration"] - spec["min_duration"])
            if cut > 0:
                spec["duration"] -= cut
                cuts.append(f"{kind.replace('_', ' ')} trimmed -{cut}'")
    context["hard_cap_cuts"] = cuts
    message = (
        f"Weekly cap {cap}' (check-in available hours / re-entry ramp): the ideal week ({before}') was cut to {total()}' - "
        + "; ".join(cuts)
        + "."
    )
    if total() > cap:
        context["warnings"].append(message + " Still above the cap: drop an optional session.")
    else:
        context["notes"].append(message)


def _add_brick(
    specs: list[dict[str, Any]],
    context: dict[str, Any],
    constraints: dict[str, Any],
    long_ride_day: str,
    warnings: list[str],
) -> None:
    """Triathlon build/race-specific weeks: a short run straight off the long ride."""
    if not (
        context["triathlon"]
        and context["phase"] in LOAD_PHASES
        and not context["recovery_week"]
        and not context["taper"]
    ):
        return
    ride = next((spec for spec in specs if spec["kind"] == "long_ride"), None)
    if ride is None:
        return
    if constraints.get("double_session_capacity_by_day", {}).get(ride["day"]) is False:
        warnings.append(f"No brick run: {ride['day']} is marked as a no-double day.")
        return
    minutes = 10 + 5 * min(4, context["load_weeks"])
    if context["phase"] == "race_specific":
        minutes += 10
    if context["readiness"] != "green" or context["absorb_week"]:
        minutes = 10
    _add_spec(
        specs,
        kind="brick_run",
        day=ride["day"],
        duration=min(30, minutes),
        min_duration=10,
    )


def _apply_daniels_caps(
    specs: list[dict[str, Any]], context: dict[str, Any], notes: list[str]
) -> None:
    """Daniels per-session caps: T <= ~10%, I <= ~8% of weekly run minutes.

    Shortening a quality run also shrinks the weekly total, so the caps are
    re-evaluated until nothing changes. Below the first rung, repeats drop.
    """
    capped: dict[int, tuple[str, int, int]] = {}
    for _ in range(12):
        weekly_run = sum(
            spec["duration"]
            for spec in specs
            if spec["kind"] in RUN_KINDS or spec["kind"] in {"brick_run", "opener"}
        )
        changed = False
        for position, spec in enumerate(specs):
            if spec["kind"] != "quality_run":
                continue
            dose = spec["dose"]
            daniels = DANIELS_FAMILY_TYPE.get(dose["family"])
            if daniels is None:
                continue
            share, floor = DANIELS_CAPS[daniels]
            cap = max(floor, share * weekly_run)
            if dose["time_in_zone_min"] <= cap:
                continue
            if dose["rung"] > 0:
                dose = ladder_dose(dose["family"], dose["rung"] - 1)
            elif dose["repeats"] > 2:
                repeats = dose["repeats"] - 1
                dose = {**dose, "repeats": repeats, "time_in_zone_min": repeats * dose["rep_min"]}
            else:
                continue
            spec["dose"] = dose
            spec["duration"] = spec["min_duration"] = 25 + dose["repeats"] * (
                dose["rep_min"] + dose["recovery_min"]
            )
            capped[position] = (daniels, int(cap), int(weekly_run))
            changed = True
        if not changed:
            break
    for position, (daniels, cap, weekly_run) in capped.items():
        dose = specs[position]["dose"]
        notes.append(
            f"{dose['label']} held at {dose['time_in_zone_min']}' (Daniels {daniels} cap ~{cap}' for {weekly_run}' of weekly running)."
        )


def _apply_taper_volume(specs: list[dict[str, Any]], context: dict[str, Any]) -> None:
    """Taper: shrink the support volume progressively; keep the short sharp touches."""
    factor = context["volume_factor"]
    if factor >= 1.0:
        return
    for spec in specs:
        if spec["kind"] not in {"easy_run", "endurance_ride", "swim_pool", "medium_long_run"}:
            continue
        reduced = max(20, _round_to_5(spec["duration"] * factor))
        spec["duration"] = reduced
        spec["min_duration"] = min(spec["min_duration"], reduced)


# --- generic session construction (no library: shapes follow the principles) --


def _session(
    day: Any,
    discipline: str,
    name: str,
    *,
    objective: str,
    duration: int,
    intensity: str,
    priority: str,
    primary_metric: str,
    zone_focus: str,
    blocks: list[dict[str, Any]],
    notes: list[str],
    success: str,
    cut_first: str,
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "day": day_name(day),
        "date": day.isoformat(),
        "discipline": discipline,
        "session_name": name,
        "objective": objective,
        "duration_min": duration,
        "intensity_guidance": intensity,
        "priority": priority,
        "primary_metric": primary_metric,
        "zone_focus": zone_focus,
        "blocks": blocks,
        "execution_notes": notes,
        "success_criteria": success,
        "cut_first": cut_first,
    }
    payload.update({key: value for key, value in extra.items() if value is not None})
    return payload


def _ride_load(duration: int, intensity_factor: float) -> int:
    return int(round(duration / 60 * intensity_factor * intensity_factor * 100))


def _quality_session(day: Any, spec: dict[str, Any]) -> dict[str, Any]:
    dose = spec["dose"]
    family = dose["family"]
    ladder = LADDERS[family]
    ride = family.startswith("bike")
    work = f"{dose['rep_min']}m"
    rest = f"{dose['recovery_min']}m"
    duration = spec["duration"]
    name = f"{ladder['label']} {dose['repeats']}x{dose['rep_min']}'"
    objective = (
        f"Progression rung {dose['rung'] + 1}: {dose['time_in_zone_min']} min at {dose['target']}, "
        "one step beyond the last completed dose of this kind."
    )
    if ride:
        intensity_factor = {106: 0.82, 95: 0.85, 88: 0.8}.get(
            int(dose["target"].split("-")[0]), 0.8
        )
        return _session(
            day,
            "Ride",
            name,
            objective=objective,
            duration=duration,
            intensity=f"{dose['repeats']} x {dose['rep_min']}' at {dose['target']}",
            priority="key",
            primary_metric="%FTP",
            zone_focus=family,
            blocks=[
                {"section": "Warmup", "steps": [{"duration": "15m", "target": "55-70% FTP"}]},
                {
                    "section": "Main Set",
                    "repeat": dose["repeats"],
                    "steps": [
                        {"duration": work, "target": dose["target"]},
                        {"duration": rest, "target": "55-60% FTP"},
                    ],
                },
                {"section": "Cooldown", "steps": [{"duration": "10m", "target": "50-55% FTP"}]},
            ],
            notes=[
                f"Every repeat inside {dose['target']}; the last one should be the hardest to hold, not a fade.",
                "If you finish with clearly more in the tank (RPE under target), note it: next week moves up a rung.",
            ],
            success=f"All {dose['repeats']} repeats land in range with stable cadence.",
            cut_first="Shorten the cooldown; protect the full set of repeats.",
            training_method="Coggan",
            coggan_level=ladder["coggan_level"],
            expected_if=intensity_factor,
            estimated_tss=_ride_load(duration, intensity_factor),
            progression=dose,
        )
    hr_based = "HR" in dose["target"]
    return _session(
        day,
        "Run",
        name,
        objective=objective,
        duration=duration,
        intensity=f"{dose['repeats']} x {dose['rep_min']}' at {dose['target']}",
        priority="key",
        primary_metric="HR" if hr_based else "Pace",
        zone_focus=family,
        blocks=[
            {
                "section": "Warmup",
                "steps": [{"duration": "15m", "target": "Z1 HR" if hr_based else "70-75% Pace"}],
            },
            {
                "section": f"{dose['repeats']}x",
                "repeat": dose["repeats"],
                "steps": [
                    {"duration": work, "target": dose["target"]},
                    {"duration": rest, "target": "Z1 HR" if hr_based else "65% Pace"},
                ],
            },
            {
                "section": "Cooldown",
                "steps": [{"duration": "10m", "target": "Z1 HR" if hr_based else "70% Pace"}],
            },
        ],
        notes=(
            [
                "Run the repeats uphill on a sustained climb (6-12% grade); power-hike any section too steep to run at the target HR; jog or walk down as recovery.",
                "Flat terrain: use a treadmill at 8-12% incline, stairs / a stair-climber, or repeated bridge or ramp climbs instead.",
            ]
            if family == "run_uphill_threshold"
            else ["Flat route or track; hold the pace band evenly from first to last repeat."]
        ),
        success=f"All {dose['repeats']} repeats inside the band, the last as strong as the first.",
        cut_first="Shorten the cooldown; protect the repeats.",
        training_method="Daniels",
        daniels_type=ladder["daniels_type"],
        progression=dose,
    )


def _long_ride_session(day: Any, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    duration = spec["duration"]
    race_block = (
        context["distance"] in {"70.3", "ironman"}
        and context["phase"] in {"build", "race_specific"}
        and not context["recovery_week"]
        and context["quality_count"] >= 2
    )
    support = context["trail"] or context["bucket"] in {
        "5k",
        "10k",
        "half_marathon",
        "marathon",
    }
    if race_block:
        block_min = min(60, 20 + 10 * min(4, context["load_weeks"]))
        endurance = max(duration - 15 - block_min - 10, 30)
        blocks = [
            {"section": "Warmup", "steps": [{"duration": "15m", "target": "55-65% FTP"}]},
            {"section": "Aerobic Base", "steps": [{"duration": f"{endurance}m", "target": "65-72% FTP"}]},
            {"section": "Race Effort", "steps": [{"duration": f"{block_min}m", "target": "78-84% FTP"}]},
            {"section": "Cooldown", "steps": [{"duration": "10m", "target": "55% FTP"}]},
        ]
        name = f"Long Ride with {block_min}' Race Power"
        intensity_factor = 0.72
    else:
        blocks = [
            {"section": "Warmup", "steps": [{"duration": "15m", "target": "55-65% FTP"}]},
            {"section": "Aerobic Base", "steps": [{"duration": f"{duration - 25}m", "target": "65-75% FTP"}]},
            {"section": "Cooldown", "steps": [{"duration": "10m", "target": "55% FTP"}]},
        ]
        name = "Aerobic Support Ride" if support else "Long Endurance Ride"
        intensity_factor = 0.68
    low, high = carb_range_g_per_h(duration)
    fuel = (
        f"Fuel {low}-{high} g carbohydrate per hour from the first 30 minutes."
        if high
        else "Water is enough; eat normally afterwards."
    )
    return _session(
        day,
        "Ride",
        name,
        objective=(
            "Aerobic support for the running: low-impact volume that does not steal from the key runs."
            if support
            else "Build aerobic durability; race-power work lands late, on tired legs, where the race is decided."
        ),
        duration=duration,
        intensity="Mostly L2; race-power block at 78-84% FTP when present",
        priority="support" if support else "key",
        primary_metric="%FTP",
        zone_focus="aerobic" if support else "durability",
        blocks=blocks,
        notes=[fuel],
        success="Power stays steady to the end and the race block is as strong as its first minutes.",
        cut_first="Trim the aerobic base, never the race-power block.",
        training_method="Coggan",
        coggan_level="L2 Endurance",
        expected_if=intensity_factor,
        estimated_tss=_ride_load(duration, intensity_factor),
    )


def _long_run_session(day: Any, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    duration = spec["duration"]
    finish = (
        context["phase"] in {"build", "race_specific"}
        and context["distance"] in {"half_marathon", "marathon", "70.3"}
        and not context["recovery_week"]
        and context["quality_count"] >= 2
    )
    blocks = [{"section": "Warmup", "steps": [{"duration": "10m", "target": "Z1 HR"}]}]
    if finish:
        finish_min = min(30, 10 + 5 * (min(4, context["load_weeks"]) + 1))
        blocks.append({"section": "Easy Run", "steps": [{"duration": f"{duration - 10 - finish_min}m", "target": "Z2 HR"}]})
        blocks.append({"section": "Steady Finish", "steps": [{"duration": f"{finish_min}m", "target": "Z3 HR"}]})
        name = f"Long Run with {finish_min}' Steady Finish"
    elif _race_effort_long_run(context):
        # Ultra race-specific: race effort on the climbs, then progressive descending.
        effort = min(45, 15 + 10 * min(3, context["load_weeks"]))
        easy = max(20, duration - 10 - effort - 10)
        blocks.append({"section": "Easy Run", "steps": [{"duration": f"{easy}m", "target": "Z2 HR"}]})
        blocks.append({"section": "Race-Effort Climbs", "steps": [{"duration": f"{effort}m", "target": "Z3 HR"}]})
        blocks.append({"section": "Easy Finish", "steps": [{"duration": "10m", "target": "Z2 HR"}]})
        name = f"Long Trail Run with {effort}' Race-Effort Climbing"
    else:
        blocks.append({"section": "Easy Run", "steps": [{"duration": f"{duration - 10}m", "target": "Z2 HR"}]})
        name = "Long Aerobic Run"
    low, high = carb_range_g_per_h(duration)
    notes = [
        f"Fuel {low}-{high} g carbohydrate per hour from the first 30-40 minutes."
        if high
        else "Water as needed; no structured fueling needed at this length."
    ]
    if context["trail"]:
        notes.extend(trail_run_notes(context, share=0.55, long_session=True))
        if not name.startswith("Long Trail Run"):
            name = "Long Trail Run"
        if context.get("trail_camp"):
            name = "Hilly Trail Run (trail-camp opportunity)"
            notes.append("Holiday in the hills: use it as a mini trail camp, but keep it optional and easy.")
    return _session(
        day,
        "Run",
        name,
        objective=(
            "Extend time on feet with climbing and controlled descending; rehearse race fueling and kit."
            if context["trail"]
            else "Extend aerobic durability; a steady finish teaches race pace on tired legs."
        ),
        duration=duration,
        intensity="Z2 HR, steady finish in Z3 when present; power-hike steep climbs",
        priority="key",
        primary_metric="HR",
        zone_focus="durability",
        blocks=blocks,
        notes=notes,
        success="Form holds to the end and the finish is the strongest part.",
        cut_first="Trim the easy middle, keep the finish.",
        training_method="Daniels",
        daniels_type="E",
    )


def trail_run_notes(
    context: dict[str, Any], *, share: float, long_session: bool
) -> list[str]:
    """Trail specificity: vertical share, power-hiking, descending, terrain, fuel."""
    notes: list[str] = []
    weekly = context.get("elevation_target_m")
    if weekly:
        notes.append(
            f"Aim for ~{int(round(weekly * share / 50.0) * 50)} m D+ in this run (weekly target ~{weekly} m)."
        )
    notes.append(
        "Power-hike grades too steep to run in Z2 (roughly >12-15%), hands on thighs, as on race day."
    )
    steps = min(4, context["load_weeks"])
    if not context.get("descent_progress"):
        # Descent did not get this week's progression slot: hold last week's dose.
        steps = max(0, steps - 1)
    descent = min(25, 5 + 5 * steps)
    if context["recovery_week"] or context["taper"]:
        descent = 5
    descent_m = context.get("descent_target_m")
    descent_note = (
        f"Progressive downhill exposure: about {descent} min of controlled descending, short quick steps, no braking"
        + (f" (weekly descent budget ~{descent_m} m)." if descent_m else ".")
    )
    if context["impact_niggles"]:
        descent_note = "No descending this week (pain reported): walk every downhill."
    if context["itb"]:
        descent_note += (
            " ITB caution: build descending gradually; stop descending and walk downhill if lateral knee pain appears."
        )
    notes.append(descent_note)
    if long_session:
        notes.append(
            "Technical terrain where possible (roots, rocks, mud); practise race kit and race-day fueling products."
        )
    notes.append(
        "Flat terrain: get the climbing from treadmill incline (8-12%), stairs, bridges or the nearest hills; "
        "a hilly weekend trip beats flat kilometres for this session."
    )
    return notes


def _race_effort_long_run(context: dict[str, Any]) -> bool:
    return (
        context["trail"]
        and context["phase"] == "race_specific"
        and not context["recovery_week"]
        and not context["taper"]
        and not context["holiday"]
        and context["readiness"] == "green"
        and not context["impact_niggles"]
    )


def _medium_long_run_session(
    day: Any, spec: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    duration = spec["duration"]
    notes = ["First half of the back-to-back weekend: easy, on tired legs tomorrow is the point."]
    notes.extend(trail_run_notes(context, share=0.25, long_session=False))
    return _session(
        day,
        "Run",
        "Medium-Long Trail Run",
        objective="Back-to-back durability: run tomorrow's long run on pre-fatigued legs, like the second half of the race.",
        duration=duration,
        intensity="Z2 HR, power-hike the steep climbs",
        # A short first back-to-back run is support work, not a second key day.
        priority="key" if duration >= LONG_SESSION_MIN["Run"] else "support",
        primary_metric="HR",
        zone_focus="durability",
        blocks=[
            {"section": "Warmup", "steps": [{"duration": "10m", "target": "Z1 HR"}]},
            {"section": "Easy Run", "steps": [{"duration": f"{duration - 10}m", "target": "Z2 HR"}]},
        ],
        notes=notes,
        success="Relaxed and controlled; you finish wanting more.",
        cut_first="Trim 15 minutes.",
        training_method="Daniels",
        daniels_type="E",
    )


def _race_session(day: Any, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    race_item = spec["race"]
    bucket = race_bucket(race_item)
    discipline = "Run" if bucket not in TRIATHLON_BUCKETS else "Race"
    low, high = carb_range_g_per_h(spec["duration"])
    return _session(
        day,
        discipline,
        f"RACE: {race_item.get('name', 'Race')}",
        objective=f"{race_item.get('priority', 'C')}-race. Execute the race plan; pacing and fueling as rehearsed.",
        duration=spec["duration"],
        intensity="Race effort per the race plan",
        priority="key",
        primary_metric="notes",
        zone_focus="race",
        blocks=[{"section": "Race", "steps": [{"duration": f"{spec['duration']}m", "target": "Race plan"}]}],
        notes=[
            "Race-day routine as rehearsed: breakfast 3 h before, nothing new on race day.",
            f"Fuel {low}-{high} g carbohydrate per hour" + (" from early on." if high else " is not needed at this length."),
        ],
        success="Even pacing, fueling executed, no late collapse.",
        cut_first="Nothing: the race is the session.",
        training_method="Friel",
        daniels_type="race",
        race_day=True,
        push_to_intervals=False,
    )


def _opener_session(day: Any, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    duration = spec["duration"]
    return _session(
        day,
        "Run",
        "Pre-Race Opener",
        objective="Wake the legs up without fatigue: the day before a race is never hard.",
        duration=duration,
        intensity="Easy with 4-5 short race-pace pickups",
        priority="support",
        primary_metric="HR",
        zone_focus="opener",
        blocks=[
            {"section": "Easy Run", "steps": [{"duration": f"{duration - 5}m", "target": "Z1-Z2 HR"}]},
            {"section": "Pickups", "repeat": 4, "steps": [{"duration": "20s", "target": "Z3 HR"}, {"duration": "55s", "target": "Z1 HR"}]},
        ],
        notes=["Short and relaxed; lay out race kit and fuel tonight."],
        success="Legs feel springy, not tired.",
        cut_first="Skip the pickups.",
        training_method="Daniels",
        daniels_type="E",
    )


def _brick_session(day: Any, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    duration = spec["duration"]
    return _session(
        day,
        "Run",
        "Brick Run off the Bike",
        objective="Bike-to-run durability: run straight off the long ride, the 70.3 limiter.",
        duration=duration,
        intensity="First minutes easy, then settle to steady (Z2-low Z3)",
        priority="support",
        primary_metric="HR",
        zone_focus="brick",
        blocks=[{"section": "Brick", "steps": [{"duration": f"{duration}m", "target": "Z2 HR"}]}],
        notes=[
            "Start within 5 minutes of getting off the bike; quick cadence, short steps.",
            "Note how the legs and HR settle: this is the race's second half in miniature.",
        ],
        success="Smooth form within 5 minutes, HR controlled.",
        cut_first="Shorten to 10 minutes.",
        training_method="Friel",
        daniels_type="E",
    )


def _tennis_session(day: Any, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return _session(
        day,
        "Tennis",
        "Tennis (fixed)",
        objective="Fixed tennis session: counted as a moderate-hard load day.",
        duration=spec["duration"],
        intensity="As played; counts as moderate-hard",
        priority="support",
        primary_metric="notes",
        zone_focus="other_sport",
        blocks=[{"section": "Tennis", "steps": [{"duration": f"{spec['duration']}m", "target": "As played"}]}],
        notes=["No key session on tennis day and no hard run the day after."],
        success="Fun, no niggles.",
        cut_first="Skip it in a recovery or race week if legs are heavy.",
        training_method="Friel",
        push_to_intervals=False,
    )


def _easy_session(day: Any, spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    kind = spec["kind"]
    duration = spec["duration"]
    if kind == "easy_run":
        return _session(
            day,
            "Run",
            "Easy Aerobic Run",
            objective="Run frequency and aerobic volume, genuinely easy so the key sessions can be hard.",
            duration=duration,
            intensity="Z1-Z2 HR, conversational",
            priority="support",
            primary_metric="HR",
            zone_focus="aerobic",
            blocks=[
                {"section": "Warmup", "steps": [{"duration": "10m", "target": "Z1 HR"}]},
                {"section": "Easy Run", "steps": [{"duration": f"{duration - 15}m", "target": "Z2 HR"}]},
                {"section": "Cooldown", "steps": [{"duration": "5m", "target": "Z1 HR"}]},
            ],
            notes=["Easy means easy: if HR drifts into Z3, slow down."],
            success="Relaxed throughout.",
            cut_first="Trim 10 minutes.",
            training_method="Daniels",
            daniels_type="E",
        )
    if kind == "endurance_ride":
        return _session(
            day,
            "Ride",
            "Endurance Ride",
            objective="Aerobic bike volume without stealing from the key sessions.",
            duration=duration,
            intensity="L2 endurance",
            priority="support",
            primary_metric="%FTP",
            zone_focus="aerobic",
            blocks=[
                {"section": "Warmup", "steps": [{"duration": "10m", "target": "55-65% FTP"}]},
                {"section": "Aerobic Base", "steps": [{"duration": f"{duration - 15}m", "target": "65-75% FTP"}]},
                {"section": "Cooldown", "steps": [{"duration": "5m", "target": "55% FTP"}]},
            ],
            notes=["Steady, smooth, no surges."],
            success="Steady power, low HR drift.",
            cut_first="Trim 15 minutes.",
            training_method="Coggan",
            coggan_level="L2 Endurance",
            expected_if=0.68,
            estimated_tss=_ride_load(duration, 0.68),
        )
    if kind in {"swim_club", "swim_pool"}:
        club = kind == "swim_club"
        ladder = swim_main_set(context)
        notes = (
            ["Club session: follow the club set; keep technique clean when tired."]
            if club
            else [f"Main set: {ladder}."]
        )
        if spec.get("club_session_distance_m"):
            notes.append(f"Expect roughly {spec['club_session_distance_m'] / 1000:g}k total.")
        if context["open_water"]:
            notes.append(
                "Open-water season: when swimming outside, wetsuit on, sight every 6-8 strokes, "
                "practise mass-start and buoy turns, and swim the main set at race feel."
            )
        return _session(
            day,
            "Swim",
            "Club Swim" if club else "Pool Swim",
            objective="Keep swim frequency and feel for the water.",
            duration=duration,
            intensity="Aerobic with controlled CSS work",
            priority="support",
            primary_metric="notes",
            zone_focus="swim",
            blocks=[
                {"section": "Warmup", "steps": [{"duration": "10m", "target": "Easy swim + drills"}]},
                {"section": "Main Set", "steps": [{"duration": f"{duration - 15}m", "target": "Controlled aerobic / CSS"}]},
                {"section": "Cooldown", "steps": [{"duration": "5m", "target": "Easy swim"}]},
            ],
            notes=notes,
            success="Smooth, consistent stroke.",
            cut_first="Shorten the main set.",
            training_method="Friel",
        )
    if kind == "strength":
        plan = spec.get("strength") or strength_prescription(context)
        strength_notes = ["Keep it away from the day before a key run when possible."]
        if context["itb"]:
            strength_notes.append(
                "ITB: include hip abductor / glute med work (side-lying raises, banded walks) every session."
            )
        return _session(
            day,
            "WeightTraining",
            plan["name"],
            objective="Strength periodized by phase for running economy, descending resilience and injury protection.",
            duration=duration,
            intensity=plan["intensity"],
            priority="support",
            primary_metric="notes",
            zone_focus="strength",
            blocks=[
                {"section": "Main Set", "steps": [{"duration": f"{duration}m", "target": plan["target"]}]},
            ],
            notes=strength_notes,
            success="Clean technique, no soreness that hurts the next key session.",
            cut_first="Drop the last set of each lift.",
            training_method="Friel",
        )
    return _session(
        day,
        "Recovery",
        "Rest + Mobility",
        objective="Absorb the work so the next key session can be hard.",
        duration=duration,
        intensity="Very easy mobility only",
        priority="support",
        primary_metric="notes",
        zone_focus="recovery",
        blocks=[{"section": "Reset", "steps": [{"duration": f"{duration}m", "target": "Mobility and breathing reset"}]}],
        notes=["Keep the day light."],
        success="You feel better tonight than this morning.",
        cut_first="Skip the mobility.",
        training_method="Friel",
    )


def swim_main_set(context: dict[str, Any]) -> str:
    """Simple swim technique -> CSS -> race-pace ladder by phase and block week."""
    if context["recovery_week"] or context["taper"] or context["transition"]:
        return "technique and easy aerobic: 6x50 drill/swim (catch, rotation), 4x100 easy"
    reps = min(3, context["load_weeks"])
    if context["phase"] == "base":
        return f"technique 6x50 drill/swim, then {8 + 2 * reps}x100 at CSS+5s/100m, 15s rest"
    if context["phase"] == "race_specific":
        return f"{3 + min(1, reps)}x400 at race pace with sighting every 6 strokes, 30s rest"
    return f"{4 + reps}x200 at CSS, 20s rest"


def _test_session(day: Any, spec: dict[str, Any]) -> dict[str, Any]:
    protocol = spec["protocol"]
    meta = PROTOCOLS[protocol]
    if spec["kind"] == "run_threshold_test":
        blocks = [
            {"section": "Warmup", "steps": [{"duration": "15m", "target": "70-75% Pace"}]},
            {"section": "Strides", "repeat": 4, "steps": [{"duration": "20s", "target": "105% Pace"}, {"duration": "40s", "target": "70% Pace"}]},
            {"section": "Test Effort", "steps": [{"duration": "10m", "target": ""}, {"duration": "20m", "target": ""}]},
            {"section": "Cooldown", "steps": [{"duration": "10m", "target": "70% Pace"}]},
        ]
        session = _session(
            day,
            "Run",
            "Threshold Run Test (30 min)",
            objective="Measure threshold pace and run LTHR with Friel's 30-minute solo time trial.",
            duration=spec["duration"],
            intensity="Maximal but evenly paced 30' solo effort on flat ground",
            priority="key",
            primary_metric="Pace",
            zone_focus="threshold_test",
            blocks=blocks,
            notes=[
                "Flat route, no traffic stops; run it as a 30' race, evenly.",
                "The 10' + 20' laps let the final-20' HR become your run LTHR.",
            ],
            success="Evenly paced 30' on flat ground.",
            cut_first="Do not cut the test: skip it and run easy if a niggle shows up.",
            training_method="Friel",
            daniels_type="T",
        )
        formula = "Threshold pace = 30' average pace; LTHR = final-20' average HR."
        discipline = "run"
    else:
        if protocol == "friel_30":
            blocks = [
                {"section": "Warmup", "steps": [{"duration": "15m", "target": "55-70%", "cadence": "90rpm"}]},
                {"section": "Main Set", "repeat": 3, "steps": [{"duration": "1m", "target": "90-95%"}, {"duration": "1m", "target": "50%"}]},
                {"section": "Test Effort", "steps": [{"duration": "10m", "target": "freeride"}, {"duration": "20m", "target": "freeride"}]},
                {"section": "Cooldown", "steps": [{"duration": "10m", "target": "50-55%"}]},
            ]
            name = "FTP + LTHR Test (30 min)"
            formula = "FTP = 30' average power; LTHR = final-20' average HR."
        else:
            blocks = [
                {"section": "Warmup", "steps": [{"duration": "20m", "target": "55-70%", "cadence": "90rpm"}]},
                {"section": "Main Set", "repeat": 3, "steps": [{"duration": "1m", "target": "75%", "cadence": "110rpm"}, {"duration": "1m", "target": "50%"}]},
                {"section": "Blowout", "steps": [{"duration": "5m", "target": "freeride"}]},
                {"section": "Recovery Spin", "steps": [{"duration": "10m", "target": "50-60%"}]},
                {"section": "Test Effort", "steps": [{"duration": "20m", "target": "freeride"}]},
                {"section": "Cooldown", "steps": [{"duration": "10m", "target": "50-55%"}]},
            ]
            name = "FTP Test (20 min)"
            formula = "FTP = 20' average power x 0.95."
        session = _session(
            day,
            "Ride",
            name,
            objective="Measure current FTP so the next block trains on correct power zones.",
            duration=spec["duration"],
            intensity="Maximal but evenly paced test effort after an easy week",
            priority="key",
            primary_metric="%FTP",
            zone_focus="threshold_test",
            blocks=blocks,
            notes=[
                "Indoors on the trainer with ERG off (freeride).",
                "Keep the test effort as its own lap so the result can be read from Intervals.",
            ],
            success="Evenly paced and finished truly spent.",
            cut_first="Do not cut the test: if you feel flat or sick, move or skip it.",
            training_method="Coggan" if protocol == "coggan_20" else "Friel",
            coggan_level="L4 Threshold",
            expected_if=0.9,
            estimated_tss=_ride_load(spec["duration"], 0.9),
        )
        discipline = "bike"
    session["threshold_test"] = {
        "discipline": discipline,
        "protocol": protocol,
        "protocol_label": meta["label"],
        "result_formula": meta["formula"],
        "formula_note": formula,
    }
    return session


def _spec_to_session(
    spec: dict[str, Any], day_lookup: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    day = day_lookup[spec["day"]]
    kind = spec["kind"]
    builders = {
        "long_ride": _long_ride_session,
        "long_run": _long_run_session,
        "medium_long_run": _medium_long_run_session,
        "race": _race_session,
        "opener": _opener_session,
        "brick_run": _brick_session,
        "tennis": _tennis_session,
        "cross_training": _cross_training_session,
    }
    if kind in QUALITY_KINDS:
        session = _quality_session(day, spec)
    elif kind in {"bike_threshold_test", "run_threshold_test"}:
        session = _test_session(day, spec)
    elif kind in builders:
        session = builders[kind](day, spec, context)
    else:
        session = _easy_session(day, spec, context)
    if (context["transition"] or context["holiday"]) and kind not in {"race", "tennis", "recovery"}:
        session["priority"] = "optional"
        session["execution_notes"] = [
            "Optional: skip it without guilt if you would rather rest, travel or play.",
            *session["execution_notes"],
        ]
    if session["discipline"] == "Ride" and context["ftp_scale"]:
        _scale_ftp_targets(session, context["ftp_scale"])
    for key, value in session_cues(session, context).items():
        session.setdefault(key, value)
    return session


def _cross_training_session(
    day: Any, spec: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Low-impact replacement for a run while a run-impact niggle is reported."""
    duration = spec["duration"]
    replaced = str(spec.get("swapped_from") or "run").replace("_", " ")
    if spec.get("aqua_jog"):
        return _session(
            day,
            "Swim",
            "Aqua Jogging (replaces the run)",
            objective=f"Keep the running stimulus of the {replaced} without impact while the pain settles.",
            duration=duration,
            intensity="Deep-water running, easy-steady, cadence high",
            priority="support",
            primary_metric="notes",
            zone_focus="cross_training",
            blocks=[{"section": "Aqua Jog", "steps": [{"duration": f"{duration}m", "target": "Easy deep-water running"}]}],
            notes=["Flotation belt, upright posture, running arm swing."],
            success="Pain-free throughout and the next day.",
            cut_first="Shorten it; never push through pain.",
            training_method="Friel",
            swapped_from=spec.get("swapped_from"),
        )
    return _session(
        day,
        "Ride",
        "Low-Impact Cross-Training Ride (replaces the run)",
        objective=f"Keep the aerobic stimulus of the {replaced} without running impact while the pain settles.",
        duration=duration,
        intensity="L2 endurance, smooth, no surges",
        priority="support",
        primary_metric="%FTP",
        zone_focus="cross_training",
        blocks=[
            {"section": "Warmup", "steps": [{"duration": "10m", "target": "55-65% FTP"}]},
            {"section": "Aerobic Base", "steps": [{"duration": f"{max(10, duration - 15)}m", "target": "65-75% FTP"}]},
            {"section": "Cooldown", "steps": [{"duration": "5m", "target": "55% FTP"}]},
        ],
        notes=["Or 30-45' of aqua jogging if the bike aggravates it."],
        success="Pain-free during and the next morning.",
        cut_first="Trim 15 minutes.",
        training_method="Coggan",
        coggan_level="L2 Endurance",
        expected_if=0.68,
        estimated_tss=_ride_load(duration, 0.68),
        swapped_from=spec.get("swapped_from"),
    )


ITB_RULE = "ITB: stop descending and walk downhill if lateral knee pain appears; finish on the flat."


def session_cues(session: dict[str, Any], context: dict[str, Any]) -> dict[str, str]:
    """How a session should feel: RPE target, talk-test cue and a bail-out rule."""
    focus = str(session.get("zone_focus") or "")
    discipline = str(session.get("discipline") or "")
    family = (session.get("progression") or {}).get("family", "")
    itb = context.get("itb") and discipline == "Run"
    if session.get("threshold_test"):
        return {
            "rpe_target": "9-10/10 for the test effort",
            "feel_cue": "Evenly paced: the first 5 minutes should feel controlled, the last 5 all-out.",
            "bail_out_rule": "If you feel flat, ill or a niggle shows up in the warm-up, skip the test and ride/run easy; retest next week.",
        }
    if focus == "race" or session.get("race_day"):
        return {
            "rpe_target": "Race plan",
            "feel_cue": "Start conservatively; the second half decides the result.",
            "bail_out_rule": "Pain that changes your stride or dizziness: stop; a finish is never worth an injury.",
        }
    if family in {"bike_vo2", "run_interval"}:
        return {
            "rpe_target": "8-9/10",
            "feel_cue": "Hard; only 1-2 words between breaths by the end of each rep.",
            "bail_out_rule": "If the target needs RPE 10 or HR stays high in the recoveries by rep 2, stop the set and finish easy.",
        }
    if family in {"bike_threshold", "run_threshold", "run_uphill_threshold"}:
        return {
            "rpe_target": "7-8/10",
            "feel_cue": "Comfortably hard; you can speak 3-4 words at a time.",
            "bail_out_rule": (
                "If pace/power needs more than RPE 9 or HR will not settle in the recoveries by rep 2, "
                "finish as easy Z2."
            ),
        }
    if family in {"bike_sweet_spot", "run_race_pace"}:
        return {
            "rpe_target": "6-7/10",
            "feel_cue": "Steady and controlled; short sentences are possible.",
            "bail_out_rule": "If holding the band needs more than RPE 8 or HR drifts above LTHR, drop to the bottom of the range or finish easy.",
        }
    if focus in {"durability"} and discipline == "Run":
        rule = (
            "Fuel from the first 30-40 minutes; if you cannot eat or feel faint, shorten to an easy loop home."
        )
        if itb:
            rule += " " + ITB_RULE
        return {
            "rpe_target": "3-4/10 (race-effort parts 5-6/10)",
            "feel_cue": "Conversational on the flat; power-hike when the talk test fails on a climb.",
            "bail_out_rule": rule,
        }
    if focus in {"durability", "aerobic"} and discipline == "Ride":
        return {
            "rpe_target": "3-4/10",
            "feel_cue": "Full conversation; you could ride like this all day.",
            "bail_out_rule": "If legs are heavy and HR stays high at easy power, cut it short; fueling first.",
        }
    if focus == "strength":
        return {
            "rpe_target": "6-7/10 (2 reps in reserve)",
            "feel_cue": "Strong, clean reps; never grinding.",
            "bail_out_rule": "Any joint pain or a key session tomorrow feels at risk: drop the last set of each lift.",
        }
    if focus in {"recovery", "swim", "other_sport", "opener", "brick", "cross_training"} or discipline in {"Swim", "Recovery", "Tennis", "WeightTraining"}:
        return {
            "rpe_target": "2-4/10",
            "feel_cue": "Relaxed; you finish fresher than you started.",
            "bail_out_rule": "If anything hurts or you are wiped out, make it shorter or skip it.",
        }
    rule = "If HR drifts into Z3 at easy pace or legs feel heavy, slow down or shorten; easy days protect the key days."
    if itb:
        rule += " " + ITB_RULE
    return {
        "rpe_target": "2-3/10",
        "feel_cue": "Full conversation; nose-breathing pace.",
        "bail_out_rule": rule,
    }


def _assign_terrain(sessions: list[dict[str, Any]], context: dict[str, Any]) -> None:
    """Trail weeks: D+ / D- and terrain on every run, summing to the weekly targets.

    Climbing is shared over the long run, the medium-long run and uphill
    quality (treadmill incline on flat terrain: no descent); descent only goes
    on outdoor trail runs, within the descent budget (`week_caps.max_descent_m`).
    """
    if not context["trail"]:
        return
    gain_target = int(context.get("elevation_target_m") or 0)
    descent_target = int(context.get("descent_target_m") or 0)
    weights: list[tuple[dict[str, Any], float, float]] = []
    for session in sessions:
        if session.get("discipline") != "Run" or session.get("race_day"):
            continue
        family = (session.get("progression") or {}).get("family")
        name = str(session.get("session_name") or "")
        if name.startswith(("Long Trail Run", "Hilly Trail Run")):
            weights.append((session, 0.55, 0.65))
        elif name.startswith("Medium-Long"):
            weights.append((session, 0.25, 0.35))
        elif family == "run_uphill_threshold":
            weights.append((session, 0.20, 0.0))
        else:
            session.setdefault("terrain", "flat")
            session.setdefault("elevation_gain_m", 0)
            session.setdefault("elevation_loss_m", 0)
    gain_total = sum(weight for _, weight, _ in weights) or 1.0
    loss_total = sum(weight for _, _, weight in weights) or 1.0
    for session, gain_w, loss_w in weights:
        gain = int(gain_target * gain_w / gain_total // 25 * 25)
        loss = int(descent_target * loss_w / loss_total // 25 * 25) if loss_w else 0
        session["elevation_gain_m"] = gain
        session["elevation_loss_m"] = min(loss, gain) if loss_w else 0
        if loss_w:
            session["terrain"] = "hilly" if context.get("trail_camp") else "trail"
        else:
            session["terrain"] = "treadmill_incline"


def _scale_target(target: str, scale: float) -> str:
    def replace(match: re.Match[str]) -> str:
        low = round(int(match.group(1)) * scale)
        if match.group(2):
            return f"{low}-{round(int(match.group(2)) * scale)}%"
        return f"{low}%"

    return PERCENT_FTP_RE.sub(replace, target)


def _scale_ftp_targets(session: dict[str, Any], scale: float) -> None:
    """Stale FTP: scale %FTP targets to the estimated real threshold (eFTP)."""
    for block in session.get("blocks", []):
        for step in block.get("steps", []):
            if isinstance(step.get("target"), str):
                step["target"] = _scale_target(step["target"], scale)
    session["intensity_guidance"] = _scale_target(session["intensity_guidance"], scale)
    session["execution_notes"] = [
        *session["execution_notes"],
        f"%FTP targets are scaled x{scale:.2f} (eFTP/FTP): the set FTP is stale and higher than "
        "current threshold. Cap sweet spot/threshold work by RPE 6-7 and HR below LTHR.",
    ]


def _missed_session_policy(
    snapshot: dict[str, Any], sessions: list[dict[str, Any]]
) -> dict[str, Any]:
    """Adaptation rules: what to do when a key session is missed (never cram),
    and how last week's misses are read (illness/injury is not unreliability)."""
    quality_days = sorted(
        DAY_NAMES.index(session["day"])
        for session in sessions
        if session.get("progression") and session.get("day") in DAY_NAMES
    )
    if_missed = []
    for session in sessions:
        if not session.get("progression") or session.get("day") not in DAY_NAMES:
            continue
        index = DAY_NAMES.index(session["day"])
        following = next((day for day in quality_days if day > index), None)
        rule = missed_session_rule(index, following)
        if_missed.append({"session_name": session["session_name"], "day": session["day"], **rule})
    weekly = snapshot.get("weekly_checkin", {}) or {}
    reasons = weekly.get("missed_session_reasons") or {}
    health = []
    other = []
    if isinstance(reasons, dict):
        for name, reason in reasons.items():
            (health if is_health_reason(reason) else other).append(f"{name}: {reason}")
    return {
        "rule": (
            "Never cram a missed key session. Missed early in the week with 48h+ before the next "
            "quality session: move it. Otherwise drop it and repeat the same rung next week."
        ),
        "if_missed": if_missed,
        "last_week_health_misses": health,
        "last_week_other_misses": other,
        "note": (
            "Last week's misses were illness/injury: they repeat the rung but do not count as unreliability."
            if health and not other
            else None
        ),
    }


def _stress_distribution(sessions: list[dict[str, Any]]) -> list[str]:
    by_day: dict[str, int] = defaultdict(int)
    for session in sessions:
        if session["discipline"] == "Recovery":
            score = 0
        elif session["discipline"] == "Swim":
            score = 1
        elif session.get("priority") == "key":
            score = 3
        else:
            score = 2
        by_day[session["day"]] += score
    labels = []
    for day in DAY_NAMES:
        total = by_day.get(day, 0)
        labels.append("low" if total <= 1 else ("medium" if total <= 3 else "high"))
    return labels


def run(
    snapshot: dict[str, Any],
    performance: dict[str, Any],
    macro: dict[str, Any],
    race: dict[str, Any],
) -> dict[str, Any]:
    context = _week_context(snapshot, performance, macro, race)
    context["macro"] = macro
    context["elevation_target_m"] = _weekly_elevation_target(context)
    progression = snapshot.get("progression", {})
    day_lookup = _day_map(snapshot)
    specs = _build_specs(snapshot, context, progression)
    sessions = [
        _spec_to_session(spec, day_lookup, context)
        for spec in sorted(
            specs,
            key=lambda item: (DAY_NAMES.index(item["day"]), reduction_priority(item["kind"])),
        )
    ]
    _assign_terrain(sessions, context)
    quality = [session for session in sessions if session.get("progression")]
    tests = [session for session in sessions if session.get("threshold_test")]
    planned_min = sum(int(session["duration_min"]) for session in sessions)
    policy = context["constraints"].get("weekly_time_budget_policy", {})

    races = [session for session in sessions if session.get("race_day")]
    if context["transition"]:
        theme = "Post-race transition: short, easy, optional sessions only; no quality, no tests."
    elif context["holiday"]:
        theme = (
            "Holiday week (maintenance, not a recovery week): 2-3 short easy sessions plus one optional "
            + ("hilly trail session - a trail-camp opportunity" if context["trail_camp"] else "longer session")
            + "; quality is optional and every session can be skipped."
        )
    elif context["race_week"]:
        theme = "Race week: volume ~40-50%, one short sharp touch early in the week, an opener the day before."
    elif context["taper"]:
        theme = (
            f"Taper: volume ~{int(context['volume_factor'] * 100)}% of build, long sessions shortened, "
            "short intensity touches keep the legs sharp."
        )
    elif context["recovery_week"]:
        theme = "Recovery week: absorb the block, no quality, and close with a threshold test when one is scheduled."
    else:
        theme = (
            f"{len(quality)} quality session(s) this week, each one rung up its progression ladder, "
            "on a pyramidal base of genuinely easy volume and long durability sessions."
        )
    if tests:
        theme += " Test: " + " and ".join(session["session_name"] for session in tests) + "."
    if races:
        theme += " Race: " + " and ".join(session["session_name"][6:] for session in races) + "."
    warnings = list(dict.fromkeys(context["warnings"]))
    if context["travel_flagged"] and not context["travel_days"]:
        warnings.append(
            "Travel is flagged this week but no travel days were named; keep the key sessions movable."
        )
    planner_notes = list(dict.fromkeys(context["notes"]))
    if context["travel_days"]:
        planner_notes.append(
            f"Travel on {', '.join(context['travel_days'])}: no key sessions or rides there, only short runs."
        )
    if context["tennis_days"]:
        planner_notes.append(
            f"Tennis on {', '.join(context['tennis_days'])} counts as moderate-hard load: no key session that day, no hard run the day after."
        )
    if context["ftp_scale"]:
        direction = "down" if context["ftp_scale"] < 1 else "up"
        planner_notes.append(
            f"Stale FTP: bike %FTP targets scaled {direction} x{context['ftp_scale']:.2f} to the estimated threshold."
        )
    pending = context["pending_ftp_update"]
    if pending:
        planner_notes.append(
            f"Bike targets assume the proposed FTP update ({pending.get('current_display')} -> "
            f"{pending.get('proposed_display')} W) is approved; if you reject it, ride the "
            "quality sessions by RPE 6-7 and HR below LTHR."
        )
    missed_policy = _missed_session_policy(snapshot, sessions)
    hard_cap = context["hard_cap_min"]

    return {
        "agent": "head_coach_planner",
        "strategic_theme": theme,
        "key_sessions": [
            session["session_name"] for session in sessions if session.get("priority") == "key"
        ],
        "quality_session_count": len(quality),
        "weekly_time_budget": {
            "planned_min": planned_min,
            "reference_min": policy.get("reference_min"),
            "hard_cap_min": hard_cap,
            "mode": "hard_cap" if hard_cap else "coach_decided",
            "cuts": context.get("hard_cap_cuts", []),
            "rationale": (
                f"The check-in's available hours ({hard_cap}') bind: easy volume is cut first, then the "
                "secondary quality, and the long session is trimmed last."
                if hard_cap
                else "Volume follows what the key sessions and long sessions need for performance; "
                "the athlete's typical hours are a reference, not a cap. Day caps bind."
            ),
        },
        "progression_budget": {
            "progressing": [item["element"] for item in context["progressions"]],
            "held": context["held"],
            "exempt": context["budget_exempt"],
            "rule": (
                f"At most {PROGRESSION_BUDGET_MAX} progressing elements per load week, at most "
                f"{PROGRESSION_BUDGET_MAX_IMPACT} of them run impact (long run, run volume, descent), "
                "unless underload is detected, readiness is green and nothing is niggling."
            ),
        },
        "missed_session_policy": missed_policy,
        "stress_distribution": _stress_distribution(sessions),
        "adaptations_targeted": [
            "progressive threshold and race-specific dose",
            "aerobic durability",
            *(["accurate zones via threshold testing"] if tests else []),
        ],
        "alternatives_if_readiness_drops": [
            "Drop the last quality session of the week first; keep the long sessions easy.",
            "Repeat this week's rung next week instead of progressing.",
        ],
        "planner_notes": planner_notes,
        "planner_warnings": warnings,
        "week_context": {
            "phase": context["phase"],
            "classification": context["classification"],
            "race_bucket": context["bucket"],
            "race_week": context["race_week"],
            "volume_factor": context["volume_factor"],
            "races_in_week": [
                {"day": day, "name": item.get("name"), "priority": item.get("priority")}
                for day, item in context["week_races"].items()
            ],
            "tennis_days": context["tennis_days"],
            "travel_days": context["travel_days"],
            "weekly_elevation_target_m": context.get("elevation_target_m"),
            "weekly_descent_target_m": context.get("descent_target_m"),
            "load_weeks_in_phase": context["load_weeks"],
            "back_to_back_weekend": context.get("trail_back_to_back", False),
            "impact_niggles": context["impact_niggles"],
            "return_from_break": context["returning"],
            "ftp_target_scale": context["ftp_scale"],
        },
        "sessions": sessions,
    }
