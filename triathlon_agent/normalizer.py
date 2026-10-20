from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
import re
from statistics import median
from typing import Any

from .memory import build_athlete_memory
from .progression import assess_progression
from .race_selection import distance_label, race_bucket
from .threshold_testing import assess_threshold_testing
from .training_load import (
    DECOUPLING_FLAG_PCT,
    HEAT_FEELS_LIKE_C,
    LONG_SESSION_MIN,
    SIGNAL_STALE_DAYS,
    VOLUME_TYPES,
    annotate_estimated_loads,
    assess_training_load,
    effective_training_load,
    subjective_rolling,
    subjective_scale,
    zone_seconds,
)
from .utils import (
    calendar_weeks,
    chunk_weekly,
    data_as_of_date,
    has_niggle,
    niggle_body_parts,
    parse_date,
    slugify,
    start_of_planned_week,
    sum_minutes,
)


DAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
DAY_PATTERN = "|".join(day.lower() for day in DAY_NAMES)
DISCIPLINE_ALIASES = {
    "Ride": "Ride",
    "VirtualRide": "Ride",
    "GravelRide": "Ride",
    "MountainBikeRide": "Ride",
    "TrackRide": "Ride",
    "EBikeRide": "Ride",
    "EMountainBikeRide": "Ride",
    "Cyclocross": "Ride",
    "Velomobile": "Ride",
    "Handcycle": "Ride",
    "Run": "Run",
    "TrailRun": "Run",
    "VirtualRun": "Run",
    "Swim": "Swim",
    "OpenWaterSwim": "Swim",
    "WeightTraining": "WeightTraining",
}
PRIOR_PLAN_MOVE_TOLERANCE_DAYS = 2
TIME_CAP_RE = re.compile(
    rf"\b(?P<day>{DAY_PATTERN})\b[^0-9]*(?:max(?:imum)?|capped at|only)?[^0-9]*(?P<minutes>\d{{2,3}})\s*(?:minutes|min)\b",
    re.IGNORECASE,
)
FLEXIBLE_CAP_RE = re.compile(
    r"\b(guideline|not a hard cap|flexible|align on|align later|optimi[sz]e roi|roi)\b",
    re.IGNORECASE,
)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _average_numeric_field(
    items: list[dict[str, Any]],
    field: str,
    *,
    transform=lambda value: float(value),
) -> float | None:
    values: list[float] = []
    for item in items:
        raw = item.get(field)
        if raw in (None, ""):
            continue
        try:
            values.append(float(transform(raw)))
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    return sum(values) / len(values)


def _append_unique(target: list[str], values: list[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _day_index(day_name: str) -> int:
    try:
        return DAY_NAMES.index(day_name)
    except ValueError:
        return len(DAY_NAMES)


def _sorted_days(days: list[str]) -> list[str]:
    return sorted(dict.fromkeys(days), key=_day_index)


def _day_mentions(text: str) -> list[str]:
    lowered = (text or "").lower()
    return [day for day in DAY_NAMES if day.lower() in lowered]


def _schedule_days(
    athlete_profile: dict[str, Any], keywords: tuple[str, ...]
) -> list[str]:
    matches: list[str] = []
    schedule = athlete_profile.get("regular_training_schedule", {})
    for day in DAY_NAMES:
        lowered = str(schedule.get(day, "")).lower()
        if any(keyword in lowered for keyword in keywords):
            matches.append(day)
    return matches


def _parse_time_caps(*text_groups: list[str]) -> dict[str, int]:
    caps: dict[str, int] = {}
    for group in text_groups:
        for text in group:
            for match in TIME_CAP_RE.finditer(text or ""):
                caps[match.group("day").capitalize()] = int(match.group("minutes"))
    return caps


def _parse_days_for_keywords(texts: list[str], keywords: tuple[str, ...]) -> list[str]:
    matches: list[str] = []
    for text in texts:
        lowered = (text or "").lower()
        if any(keyword in lowered for keyword in keywords):
            _append_unique(matches, _day_mentions(text))
    return _sorted_days(matches)


def _parse_days_for_keywords_in_clauses(
    texts: list[str], keywords: tuple[str, ...]
) -> list[str]:
    matches: list[str] = []
    for text in texts:
        clauses = re.split(r"[.;,]", text or "")
        for clause in clauses:
            lowered = clause.lower()
            if any(keyword in lowered for keyword in keywords):
                _append_unique(matches, _day_mentions(clause))
    return matches


def _requested_week_type(weekly_checkin: dict[str, Any]) -> str | None:
    text = " ".join(
        str(weekly_checkin.get(field, ""))
        for field in ("athlete_notes", "last_week_summary", "unexpected_fatigue")
    ).lower()
    if any(
        token in text
        for token in ("recovery week", "easy week", "down week", "unload week")
    ):
        return "recovery"
    if "build week" in text:
        return "build"
    return None


def _coerce_optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _constraint_entry(
    kind: str,
    value: Any,
    *,
    source: str,
    constraint_type: str,
    confidence: str,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "value": value,
        "source": source,
        "type": constraint_type,
        "confidence": confidence,
    }


def _round_to_5(value: float) -> int:
    return int(round(value / 5.0) * 5)


def _time_budget_policy(
    athlete_profile: dict[str, Any], weekly_checkin: dict[str, Any]
) -> dict[str, Any]:
    requested_minutes = _coerce_optional_int(
        weekly_checkin.get("available_hours_override_min")
    )
    athlete_text = " ".join(
        str(value)
        for value in athlete_profile.get("decision_policy", {}).values()
        if value not in (None, "")
    )
    week_text = " ".join(
        [
            *(weekly_checkin.get("time_constraints") or []),
            str(weekly_checkin.get("athlete_notes", "") or ""),
        ]
    )
    flexible_language = bool(
        FLEXIBLE_CAP_RE.search(athlete_text) or FLEXIBLE_CAP_RE.search(week_text)
    )
    lowered_week_text = week_text.lower()
    hard_language = (
        "hard cap" in lowered_week_text and "not a hard cap" not in lowered_week_text
    )
    reference_minutes = requested_minutes
    if reference_minutes is None:
        try:
            reference_minutes = int(
                round(
                    float(
                        athlete_profile.get("typical_training_hours_per_week", 0) or 0
                    )
                    * 60
                )
            )
        except (TypeError, ValueError):
            reference_minutes = None
    if reference_minutes is None:
        reference_minutes = 480
    mode = "hard" if requested_minutes is not None and hard_language else "flexible"
    if requested_minutes is not None and not flexible_language and not hard_language:
        mode = "hard"
    return {
        # Reference only: the head coach decides weekly volume on performance
        # grounds and is never trimmed to a weekly total. Day caps stay binding.
        "binding": False,
        "mode": mode,
        "reference_min": _round_to_5(reference_minutes),
        "requested_min": requested_minutes,
        "source": (
            "weekly_checkin.available_hours_override_min"
            if requested_minutes is not None
            else "athlete_profile.typical_training_hours_per_week"
        ),
    }


def _step_minutes(step: dict[str, Any]) -> int:
    duration = str(step.get("duration", "")).lower()
    match = re.match(r"(?P<minutes>\d+)\s*(?:m|min)\b", duration)
    if not match:
        return 0
    return int(match.group("minutes"))


def _section_minutes(section: dict[str, Any]) -> int:
    repeat = max(int(section.get("repeat", 1) or 1), 1)
    return repeat * sum(_step_minutes(step) for step in section.get("steps", []))


def _cooldown_minutes(session: dict[str, Any]) -> int:
    total = 0
    for section in session.get("blocks", []):
        label = str(section.get("section", "")).lower()
        if "cool" in label or "finish" in label:
            total += _section_minutes(section)
    return total


def _session_completion_targets(session: dict[str, Any]) -> tuple[int, int]:
    planned_minutes = max(int(session.get("duration_min", 0) or 0), 1)
    cooldown_minutes = min(
        _cooldown_minutes(session),
        max(planned_minutes - 15, 0),
    )
    cooldown_tolerance = min(
        cooldown_minutes,
        max(5, int(round(planned_minutes * 0.12))),
    )
    essential_minutes = max(
        planned_minutes - cooldown_tolerance, int(planned_minutes * 0.75)
    )
    return essential_minutes, cooldown_tolerance


# Intervals.icu "Feel" is a 1-5 scale where 1 is the BEST (athlete felt strong)
# and 5 is the WORST (athlete felt weak). Lower numbers are better, not worse.
FEEL_LABELS: dict[int, str] = {
    1: "strong",
    2: "good",
    3: "normal",
    4: "tired",
    5: "weak",
}


def _feel_label(value: Any) -> str | None:
    """Map an Intervals.icu numeric feel rating to its label (1=strong .. 5=weak)."""
    if value in (None, ""):
        return None
    try:
        rating = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return FEEL_LABELS.get(rating)


# Intervals wellness "sleepQuality" is 1-4 and inverted like feel:
# 1 = great, 2 = good, 3 = average, 4 = poor.
SLEEP_QUALITY_LABELS: dict[int, str] = {1: "great", 2: "good", 3: "average", 4: "poor"}


def _sleep_quality_label(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return SLEEP_QUALITY_LABELS.get(int(round(float(value))))
    except (TypeError, ValueError):
        return None


def _latest_numeric(items: list[dict[str, Any]], field: str) -> float | None:
    for item in reversed(items):
        value = item.get(field)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _dated_rows(
    wellness: list[dict[str, Any]], start: date, end: date
) -> list[dict[str, Any]]:
    """Wellness rows whose date (`id`) lies in [start, end]: windows by date, not by count."""
    rows = []
    for item in wellness:
        try:
            day = parse_date(str(item.get("id") or "")[:10])
        except ValueError:
            continue
        if start <= day <= end:
            rows.append(item)
    return rows


# Extra Intervals wellness fields summarised for the coaches, with their scales.
WELLNESS_FIELD_SCALES: dict[str, tuple[str, str]] = {
    # summary key: (Intervals field, scale note)
    "stress": ("stress", "Intervals 1-4: 1 = low .. 4 = extreme (higher is worse)"),
    "mood": ("mood", "Intervals 1-4, inverted: 1 = great .. 4 = grumpy (lower is better)"),
    "motivation": ("motivation", "Intervals 1-4, inverted: 1 = extreme .. 4 = low (lower is better)"),
    "injury": ("injury", "Intervals 1-4: 1 = none, 2 = niggle, 3 = poor, 4 = injured"),
    "spo2": ("spO2", "SpO2 % (lower is worse)"),
    "respiration": ("respiration", "breaths per minute (a rise can precede illness)"),
}
SUBJECTIVE_1_4_FIELDS = {"stress", "mood", "motivation", "injury"}


def _field_summary(
    wellness: list[dict[str, Any]], key: str, planning_date: date
) -> dict[str, Any]:
    field, scale = WELLNESS_FIELD_SCALES[key]
    recent = _dated_rows(wellness, planning_date - timedelta(days=6), planning_date)
    baseline_rows = _dated_rows(
        wellness, planning_date - timedelta(days=34), planning_date - timedelta(days=7)
    )
    mean_7d = _average_numeric_field(recent, field)
    baseline = _average_numeric_field(baseline_rows, field)
    if key in SUBJECTIVE_1_4_FIELDS and subjective_scale(wellness, field) == "1-10":
        scale = "1-10 input (not the Intervals 1-4 scale)"
    return {
        "mean_7d": round(mean_7d, 2) if mean_7d is not None else None,
        "baseline_28d": round(baseline, 2) if baseline is not None else None,
        "latest": _latest_numeric(recent, field),
        "entries_7d": sum(1 for item in recent if item.get(field) not in (None, "")),
        "scale": scale,
    }


def _weight_trend_kg_28d(
    wellness: list[dict[str, Any]], planning_date: date
) -> float | None:
    """Mean of the last 3 weights minus the mean of the first 3 in the last 28 days."""
    series = []
    for item in _dated_rows(wellness, planning_date - timedelta(days=27), planning_date):
        value = item.get("weight")
        if value in (None, ""):
            continue
        try:
            series.append((parse_date(str(item["id"])[:10]), float(value)))
        except (KeyError, TypeError, ValueError):
            continue
    series.sort()
    if len(series) < 4 or (series[-1][0] - series[0][0]).days < 14:
        return None
    head = [value for _, value in series[:3]]
    tail = [value for _, value in series[-3:]]
    return round(sum(tail) / len(tail) - sum(head) / len(head), 1)


def _recent_wellness_summary(
    wellness: list[dict[str, Any]], planning_date: date
) -> dict[str, Any]:
    """Sleep, ramp rate and the other Intervals wellness fields over the last 7 days.

    Subjective fatigue / soreness use a 3-day rolling mean (not one entry).
    """
    recent_wellness = _dated_rows(wellness, planning_date - timedelta(days=6), planning_date)
    quality_avg = _average_numeric_field(recent_wellness, "sleepQuality")
    quality_latest = _latest_numeric(recent_wellness, "sleepQuality")
    score_avg = _average_numeric_field(recent_wellness, "sleepScore")
    summary = {
        "window": "last 7 calendar days up to the planning date",
        "entries_7d": len(recent_wellness),
        "sleep_quality_scale": "Intervals 1-4, inverted: 1 = great, 4 = poor",
        "sleep_quality_avg": round(quality_avg, 2) if quality_avg is not None else None,
        "sleep_quality_label": _sleep_quality_label(quality_avg),
        "sleep_quality_latest": quality_latest,
        "sleep_quality_latest_label": _sleep_quality_label(quality_latest),
        "sleep_score_avg": round(score_avg, 1) if score_avg is not None else None,
        "sleep_score_latest": _latest_numeric(recent_wellness, "sleepScore"),
        "ramp_rate_latest": _latest_numeric(recent_wellness, "rampRate"),
        "weight_trend_kg_28d": _weight_trend_kg_28d(wellness, planning_date),
    }
    for key in WELLNESS_FIELD_SCALES:
        summary[key] = _field_summary(wellness, key, planning_date)
    for field in ("fatigue", "soreness"):
        rolling = subjective_rolling(wellness, field, planning_date)
        summary[field] = {
            "mean_3d": rolling["mean"],
            "entries_3d": rolling["entries"],
            "high": rolling["high"],
            "scale": (
                "1-10 input (not the Intervals 1-4 scale)"
                if rolling["scale"] == "1-10"
                else "Intervals 1-4: 1 = low .. 4 = extreme (higher is worse)"
            ),
        }
    return summary


# Elevation-gain (D+) density thresholds, in metres of climbing per km, above
# which terrain meaningfully changes how a session's pace/HR should be read.
_TERRAIN_DENSITY_NOTABLE = {"Run": 8.0, "Ride": 12.0}
_TERRAIN_GAIN_NOTABLE = {"Run": 150.0, "Ride": 500.0}


def _activity_terrain(activity: dict[str, Any] | None) -> dict[str, Any]:
    """Extract elevation gain (D+) and distance from an Intervals.icu activity.

    Intervals.icu exposes cumulative ascent as ``total_elevation_gain`` (metres)
    and ``distance`` (metres). ``elevation_per_km`` is the climbing density, the
    figure that tells us whether a given pace/HR reflects flat or hilly work.
    """
    if not activity:
        return {"elevation_gain_m": None, "distance_km": None, "elevation_per_km": None}
    gain_raw = activity.get("total_elevation_gain")
    if gain_raw in (None, ""):
        gain_raw = activity.get("icu_elevation_gain")
    elevation_gain_m: float | None = None
    if gain_raw not in (None, ""):
        try:
            elevation_gain_m = round(float(gain_raw))
        except (TypeError, ValueError):
            elevation_gain_m = None
    distance_km: float | None = None
    distance_raw = activity.get("distance")
    if distance_raw not in (None, ""):
        try:
            distance_km = round(float(distance_raw) / 1000.0, 2)
        except (TypeError, ValueError):
            distance_km = None
    elevation_per_km: float | None = None
    if elevation_gain_m is not None and distance_km and distance_km > 0:
        elevation_per_km = round(elevation_gain_m / distance_km, 1)
    return {
        "elevation_gain_m": elevation_gain_m,
        "distance_km": distance_km,
        "elevation_per_km": elevation_per_km,
    }


QUALITY_SESSION_HINTS = (
    "threshold",
    "interval",
    "tempo",
    "vo2",
    "sweet spot",
    "race pace",
    "race-pace",
    "mp finish",
    "marathon pace",
    "progression",
    "cruise",
    "hill repeat",
    "fartlek",
    "test",
)
# Whole words only: "test" must not match "contest" / "fastest".
QUALITY_SESSION_RE = re.compile(
    r"\b(?:threshold|intervals?|tempo|vo2(?:max)?|sweet[- ]?spot|race[- ]pace|mp finish|"
    r"marathon pace|progression|cruise|hill repeats?|fartlek|tests?)\b"
)
# Easy session drifted hard when this much time sat above LT1 (or above LT2).
EASY_ABOVE_LT1_MAX_MIN = 12.0
EASY_ABOVE_LT1_MAX_SHARE = 0.25
EASY_HIGH_MAX_MIN = 5.0
# A planned quality session landed when at least this much was above LT1.
QUALITY_ABOVE_LT1_MIN = 12.0
QUALITY_HIGH_MIN = 5.0
RIDE_QUALITY_IF = 0.80
RIDE_EASY_MAX_IF = 0.85
UNPLANNED_MIN_MINUTES = 30.0


def _planned_intensity(session: dict[str, Any]) -> str:
    """Whether a planned session was meant as quality or easy work."""
    name = str(session.get("session_name", "")).lower()
    if session.get("threshold_test") or session.get("progression"):
        return "quality"
    if session.get("daniels_type") in {"T", "I", "R", "M"}:
        return "quality"
    if session.get("coggan_level") in {"L4 Threshold", "L5 VO2max", "L6 Anaerobic"}:
        return "quality"
    if _safe_float(session.get("expected_if")) >= 0.8:
        return "quality"
    if QUALITY_SESSION_RE.search(name):
        return "quality"
    if session.get("discipline") in {"Run", "Ride"}:
        return "easy"
    return "unknown"


def _intensity_compliance(
    session: dict[str, Any], activity: dict[str, Any] | None
) -> str:
    """Compare planned quality vs actual time above LT1 / IF / avg HR."""
    if not activity:
        return "unknown"
    planned = _planned_intensity(session)
    if planned == "unknown":
        return "unknown"
    seconds = zone_seconds(activity)
    minutes = _safe_float(activity.get("moving_time")) / 60.0
    intensity_factor = _safe_float(activity.get("icu_intensity")) / 100.0
    has_power_if = (
        activity.get("type") in {"Ride", "VirtualRide", "GravelRide", "MountainBikeRide"}
        and _safe_float(activity.get("icu_average_watts")) > 0
        and intensity_factor > 0
    )
    if seconds["source"] != "none":
        above_lt1 = seconds["above_lt1_s"] / 60.0
        high = seconds["high_s"] / 60.0
    else:
        avg_hr = _safe_float(activity.get("average_heartrate"))
        lthr = _safe_float(activity.get("lthr"))
        if not (avg_hr and lthr) and not has_power_if:
            return "unknown"
        ratio = avg_hr / lthr if avg_hr and lthr else 0.0
        above_lt1 = minutes if ratio >= 0.9 else 0.0
        high = minutes if ratio >= 1.0 else 0.0
    if planned == "quality":
        landed = (
            above_lt1 >= QUALITY_ABOVE_LT1_MIN
            or high >= QUALITY_HIGH_MIN
            or (has_power_if and intensity_factor >= RIDE_QUALITY_IF)
        )
        return "as_planned" if landed else "easier_than_planned"
    drifted = (
        above_lt1 > max(EASY_ABOVE_LT1_MAX_MIN, EASY_ABOVE_LT1_MAX_SHARE * minutes)
        or high >= EASY_HIGH_MAX_MIN
        or (has_power_if and intensity_factor > RIDE_EASY_MAX_IF)
    )
    return "harder_than_planned" if drifted else "as_planned"


def _activity_signals(activity: dict[str, Any] | None) -> dict[str, Any]:
    """Secondary execution signals: decoupling, RPE, heat, descent, estimated load."""
    if not activity:
        return {
            "decoupling_pct": None,
            "decoupling_flag": False,
            "icu_rpe": None,
            "session_rpe": None,
            "average_feels_like": None,
            "heat_flag": False,
            "elevation_loss_m": None,
            "elevation_loss_estimated": False,
            "estimated_training_load": None,
        }
    decoupling = activity.get("decoupling")
    decoupling_value = None if decoupling in (None, "") else round(_safe_float(decoupling), 1)
    minutes = _safe_float(activity.get("moving_time")) / 60.0
    feels_like = activity.get("average_feels_like")
    feels_like_value = None if feels_like in (None, "") else round(_safe_float(feels_like), 1)
    loss = activity.get("total_elevation_loss")
    loss_estimated = False
    if loss in (None, ""):
        # Missing D- is not 0 m: use D+ as an estimated proxy.
        loss = activity.get("total_elevation_gain")
        loss_estimated = loss not in (None, "")
    return {
        "decoupling_pct": decoupling_value,
        "decoupling_flag": bool(
            decoupling_value is not None
            and decoupling_value > DECOUPLING_FLAG_PCT
            and minutes >= LONG_SESSION_MIN
        ),
        "icu_rpe": activity.get("icu_rpe"),
        "session_rpe": activity.get("session_rpe"),
        "average_feels_like": feels_like_value,
        "heat_flag": bool(feels_like_value is not None and feels_like_value > HEAT_FEELS_LIKE_C),
        "elevation_loss_m": None if loss in (None, "") else round(_safe_float(loss)),
        "elevation_loss_estimated": loss_estimated,
        "estimated_training_load": activity.get("estimated_training_load"),
    }


def _review_extras(
    session: dict[str, Any], activity: dict[str, Any] | None
) -> dict[str, Any]:
    return {
        "priority": session.get("priority"),
        "planned_intensity": _planned_intensity(session),
        "intensity_compliance": _intensity_compliance(session, activity),
        **_activity_signals(activity),
    }


def _signals_clause(signals: dict[str, Any]) -> str:
    bits = []
    if signals.get("decoupling_flag"):
        bits.append(
            f"aerobic decoupling was {signals['decoupling_pct']:g}% (above 5%: durability or fuelling limited the back half)"
        )
    if signals.get("heat_flag"):
        bits.append(
            f"it felt like {signals['average_feels_like']:g} C, so HR ran higher than the effort"
        )
    if not bits:
        return ""
    return " Note: " + "; ".join(bits) + "."


def _terrain_clause(discipline: str | None, terrain: dict[str, Any]) -> str:
    """A short sentence flagging climbing that makes pace/HR read tougher than flat."""
    gain = terrain.get("elevation_gain_m")
    if gain in (None, "") or float(gain) <= 0:
        return ""
    density = terrain.get("elevation_per_km")
    density_notable = (
        density is not None
        and float(density) >= _TERRAIN_DENSITY_NOTABLE.get(discipline or "", 10.0)
    )
    gain_notable = float(gain) >= _TERRAIN_GAIN_NOTABLE.get(discipline or "", 300.0)
    if not (density_notable or gain_notable):
        return ""
    if density is not None:
        detail = f"{int(round(float(gain)))} m of climbing (~{density:g} m/km)"
    else:
        detail = f"{int(round(float(gain)))} m of climbing"
    return (
        f" It carried {detail}, so the pace and heart rate read tougher than the "
        "same effort on flat ground."
    )


def _session_review_note(
    session: dict[str, Any],
    match: dict[str, Any] | None,
    status: str,
    actual_minutes: float,
    duration_ratio: float,
    pragmatic_ratio: float,
) -> str:
    if status == "missed":
        return "No matching session was found, so this one should be treated as missed rather than analyzed."
    avg_hr = match.get("average_heartrate") if match else None
    load = match.get("icu_training_load") if match else None
    if load in (None, "") and match:
        load = match.get("estimated_training_load")
    feel_label = _feel_label(match.get("feel")) if match else None
    metric_bits: list[str] = []
    if avg_hr not in (None, ""):
        metric_bits.append(f"avg HR {int(round(float(avg_hr)))} bpm")
    if load not in (None, ""):
        metric_bits.append(f"load {int(round(float(load)))}")
    if feel_label:
        metric_bits.append(f"felt {feel_label}")
    metric_text = f" ({', '.join(metric_bits)})" if metric_bits else ""
    terrain = _terrain_clause(
        session.get("discipline"), _activity_terrain(match)
    ) + _signals_clause(_activity_signals(match))
    if status == "completed_pragmatic":
        return (
            "The key work appears to have landed cleanly and only non-essential volume was left out"
            f"{metric_text}.{terrain}"
        )
    if status == "shortened":
        if duration_ratio >= 0.75 or pragmatic_ratio >= 0.8:
            return (
                "Most of the useful work was still there, but enough volume was trimmed that it should count as shortened"
                f"{metric_text}.{terrain}"
            )
        return f"The session lost too much core work to count as complete{metric_text}.{terrain}"
    if status == "completed_moved":
        return f"The session was completed on a different day, with the main workload preserved{metric_text}.{terrain}"
    return f"The session was completed as planned and the metrics look usable for interpretation{metric_text}.{terrain}"


def _normalize_planning_constraints(
    athlete_profile: dict[str, Any], weekly_checkin: dict[str, Any]
) -> dict[str, Any]:
    open_water_mode = bool(weekly_checkin.get("open_water_mode"))
    hard_constraints = list(
        athlete_profile.get("hard_constraints", {}).get("always", [])
    )
    if not open_water_mode:
        hard_constraints.extend(
            athlete_profile.get("hard_constraints", {}).get(
                "when_open_water_mode_false", []
            )
        )
    fixed_preferences = list(athlete_profile.get("fixed_weekly_preferences", []))
    known_constraints = list(athlete_profile.get("known_constraints", []))
    time_constraints = list(weekly_checkin.get("time_constraints", []))
    weekly_request_texts = [
        *time_constraints,
        str(weekly_checkin.get("athlete_notes", "") or ""),
    ]
    weekday_preferences = list(
        athlete_profile.get("soft_preferences", {}).get("weekday_rhythm", [])
    )
    weekend_preferences = list(
        athlete_profile.get("soft_preferences", {}).get("weekend_structure", [])
    )

    no_bike_days = _parse_days_for_keywords(
        [*hard_constraints, *known_constraints, *time_constraints],
        (
            "no bike",
            "no bike access",
            "no access to a bike",
            "cannot be a bike day",
            "bike is impractical",
            "free of bike",
        ),
    )
    requested_ride_days = []
    for text in weekly_request_texts:
        clauses = re.split(r"[.;,]", text or "")
        for clause in clauses:
            lowered = clause.lower()
            if any(
                phrase in lowered
                for phrase in (
                    "no bike",
                    "no bike access",
                    "no access to a bike",
                    "cannot bike",
                    "cannot be a bike day",
                    "bike is impractical",
                    "free of bike",
                )
            ):
                continue
            if any(keyword in lowered for keyword in ("ride", "bike", "cycling")):
                _append_unique(requested_ride_days, _day_mentions(clause))
    requested_ride_days = _sorted_days(requested_ride_days)
    requested_run_days = _parse_days_for_keywords_in_clauses(
        weekly_request_texts, ("run",)
    )
    no_bike_days = [day for day in no_bike_days if day not in requested_ride_days]
    fixed_swim_days = _parse_days_for_keywords(
        [*hard_constraints, *fixed_preferences, *time_constraints],
        ("swim", "pool", "club swim"),
    )
    preferred_double_days = _parse_days_for_keywords(
        [
            *fixed_preferences,
            *weekday_preferences,
            *list(athlete_profile.get("regular_training_schedule", {}).values()),
        ],
        ("run plus", "run and", "run +", "club swim", "pool swim"),
    )
    strength_only_days = _parse_days_for_keywords(
        [*known_constraints, *time_constraints], ("gym only", "strength only")
    )
    parsed_time_caps = _parse_time_caps(
        hard_constraints, known_constraints, time_constraints
    )
    explicit_time_caps = {
        day: int(minutes)
        for day, minutes in weekly_checkin.get("time_caps_by_day", {}).items()
        if _coerce_optional_int(minutes) is not None
    }
    parsed_time_caps.update(explicit_time_caps)
    time_budget_policy = _time_budget_policy(athlete_profile, weekly_checkin)
    double_capacity = {
        day: bool(value)
        for day, value in weekly_checkin.get(
            "double_session_capacity_by_day", {}
        ).items()
    }

    for day in fixed_swim_days:
        double_capacity.setdefault(day, day in preferred_double_days)

    hard_rules = [
        _constraint_entry(
            "day_off",
            day,
            source="athlete_profile.non_negotiable_days_off",
            constraint_type="hard",
            confidence="high",
        )
        for day in _sorted_days([*athlete_profile.get("non_negotiable_days_off", [])])
    ]
    hard_rules.extend(
        _constraint_entry(
            "no_bike_day",
            day,
            source=(
                "weekly_checkin.time_constraints"
                if time_constraints
                else "athlete_profile.known_constraints"
            ),
            constraint_type="hard",
            confidence="high",
        )
        for day in no_bike_days
    )
    hard_rules.extend(
        _constraint_entry(
            "fixed_swim_day",
            day,
            source="athlete_profile.hard_constraints",
            constraint_type="hard",
            confidence="medium",
        )
        for day in fixed_swim_days
    )
    hard_rules.extend(
        _constraint_entry(
            "day_time_cap",
            {"day": day, "minutes": minutes},
            source="weekly_checkin.time_caps_by_day",
            constraint_type="hard",
            confidence="high",
        )
        for day, minutes in sorted(
            parsed_time_caps.items(), key=lambda item: _day_index(item[0])
        )
    )
    if time_budget_policy["mode"] == "hard" and weekly_checkin.get(
        "available_hours_override_min"
    ) not in (None, ""):
        hard_rules.append(
            _constraint_entry(
                "weekly_time_cap",
                int(weekly_checkin["available_hours_override_min"]),
                source="weekly_checkin.available_hours_override_min",
                constraint_type="hard",
                confidence="high",
            )
        )
    for day, allowed in sorted(
        double_capacity.items(), key=lambda item: _day_index(item[0])
    ):
        if allowed is False:
            hard_rules.append(
                _constraint_entry(
                    "no_double_day",
                    day,
                    source="weekly_checkin.double_session_capacity_by_day",
                    constraint_type="hard",
                    confidence="high",
                )
            )

    soft_preferences = []
    if athlete_profile.get("preferred_long_ride_day"):
        soft_preferences.append(
            _constraint_entry(
                "preferred_long_ride_day",
                athlete_profile["preferred_long_ride_day"],
                source="athlete_profile.preferred_long_ride_day",
                constraint_type="soft",
                confidence="medium",
            )
        )
    if athlete_profile.get("preferred_long_run_day"):
        soft_preferences.append(
            _constraint_entry(
                "preferred_long_run_day",
                athlete_profile["preferred_long_run_day"],
                source="athlete_profile.preferred_long_run_day",
                constraint_type="soft",
                confidence="medium",
            )
        )
    soft_preferences.extend(
        _constraint_entry(
            "strength_day",
            day,
            source="athlete_profile.strength_days",
            constraint_type="soft",
            confidence="medium",
        )
        for day in athlete_profile.get("strength_days", [])
    )
    soft_preferences.extend(
        _constraint_entry(
            "preferred_double_day",
            day,
            source="athlete_profile.soft_preferences.weekday_rhythm",
            constraint_type="soft",
            confidence="medium",
        )
        for day in preferred_double_days
    )
    soft_preferences.extend(
        _constraint_entry(
            "requested_ride_day",
            day,
            source="weekly_checkin",
            constraint_type="soft",
            confidence="high",
        )
        for day in requested_ride_days
    )
    soft_preferences.extend(
        _constraint_entry(
            "requested_run_day",
            day,
            source="weekly_checkin",
            constraint_type="soft",
            confidence="high",
        )
        for day in requested_run_days
    )

    inferred_preferences = []
    inferred_preferences.extend(
        _constraint_entry(
            "preferred_ride_day",
            day,
            source="athlete_profile.regular_training_schedule",
            constraint_type="inferred",
            confidence="low",
        )
        for day in _schedule_days(athlete_profile, ("bike", "ride", "cycling"))
    )
    inferred_preferences.extend(
        _constraint_entry(
            "preferred_run_day",
            day,
            source="athlete_profile.regular_training_schedule",
            constraint_type="inferred",
            confidence="low",
        )
        for day in _schedule_days(athlete_profile, ("run",))
    )
    inferred_preferences.extend(
        _constraint_entry(
            "preferred_swim_day",
            day,
            source="athlete_profile.regular_training_schedule",
            constraint_type="inferred",
            confidence="low",
        )
        for day in _schedule_days(athlete_profile, ("swim", "pool", "club"))
    )

    return {
        "open_water_mode": open_water_mode,
        "days_off": _sorted_days(
            [
                *athlete_profile.get("non_negotiable_days_off", []),
            ]
        ),
        "no_bike_days": no_bike_days,
        "fixed_swim_days": fixed_swim_days,
        "preferred_double_days": preferred_double_days,
        "double_session_capacity_by_day": double_capacity,
        "time_caps_by_day": parsed_time_caps,
        "available_hours_override_min": (
            weekly_checkin.get("available_hours_override_min")
            if time_budget_policy["mode"] == "hard"
            else None
        ),
        "requested_available_hours_override_min": weekly_checkin.get(
            "available_hours_override_min"
        ),
        "weekly_time_budget_policy": time_budget_policy,
        "travel_this_week": bool(weekly_checkin.get("travel_this_week")),
        "preferred_long_ride_day": athlete_profile.get("preferred_long_ride_day"),
        "preferred_long_run_day": athlete_profile.get("preferred_long_run_day"),
        "strength_days": athlete_profile.get("strength_days", []),
        "preferred_ride_days": list(
            dict.fromkeys(
                [
                    *requested_ride_days,
                    *_schedule_days(athlete_profile, ("bike", "ride", "cycling")),
                ]
            )
        ),
        "preferred_run_days": list(
            dict.fromkeys(
                [*requested_run_days, *_schedule_days(athlete_profile, ("run",))]
            )
        ),
        "preferred_swim_days": _schedule_days(
            athlete_profile, ("swim", "pool", "club")
        ),
        "strength_only_days": strength_only_days,
        "requested_week_type": _requested_week_type(weekly_checkin),
        "current_block_week": _coerce_optional_int(
            weekly_checkin.get("current_block_week")
        ),
        "raw_time_constraints": time_constraints,
        "known_constraints": known_constraints,
        "decision_policy": athlete_profile.get("decision_policy", {}),
        "weekday_preferences": weekday_preferences,
        "weekend_preferences": weekend_preferences,
        "hard_rules": hard_rules,
        "soft_preferences": soft_preferences,
        "inferred_preferences": inferred_preferences,
        "override_explanations_required": [
            item["kind"] for item in [*soft_preferences, *inferred_preferences]
        ],
        "constraint_confidence": {
            "hard": "high",
            "soft": "medium",
            "inferred": "low",
        },
    }


def _normalize_discipline(value: Any) -> str:
    normalized = DISCIPLINE_ALIASES.get(str(value), str(value or "Other"))
    if normalized.endswith("Run"):
        return "Run"
    if normalized.endswith("Ride"):
        return "Ride"
    if normalized.endswith("Swim"):
        return "Swim"
    return normalized


def _match_score(
    session: dict[str, Any], activity: dict[str, Any]
) -> tuple[int, float]:
    day_diff = abs(
        (
            parse_date(activity.get("start_date_local", "1970-01-01"))
            - parse_date(session["date"])
        ).days
    )
    planned_minutes = int(session.get("duration_min", 0) or 0)
    actual_minutes = _safe_float(activity.get("moving_time")) / 60.0
    return (day_diff, abs(actual_minutes - planned_minutes))


def _session_type_label(session: dict[str, Any]) -> str:
    discipline = session.get("discipline")
    name = str(session.get("session_name", "")).lower()
    if discipline == "Ride" and "long" in name:
        return "long_ride"
    if discipline == "Run" and "long" in name:
        return "long_run"
    if discipline == "Ride" and session.get("priority") == "key":
        return "key_ride"
    if discipline == "Run" and session.get("primary_metric") == "Pace":
        return "quality_run"
    if discipline == "Swim":
        return "swim_routine"
    if discipline == "WeightTraining":
        return "strength"
    if discipline == "Recovery":
        return "recovery"
    return f"support_{str(discipline or 'other').lower()}"


def _session_event_id(
    session: dict[str, Any], events: list[dict[str, Any]]
) -> Any:
    """Intervals event id of a planned session (explicit, or via its coach external_id)."""
    for key in ("intervals_event_id", "event_id"):
        if session.get(key) not in (None, ""):
            return session[key]
    if not session.get("date") or not events:
        return None
    external_id = session.get("external_id") or (
        f"coach-{session['date']}-{slugify(str(session.get('discipline') or ''))}"
        f"-{slugify(str(session.get('session_name') or ''))}"
    )
    for event in events:
        if str(event.get("external_id") or "") == external_id and event.get("id") is not None:
            return event["id"]
    return None


def _single_prior_plan_adherence(
    prior_plan_context: dict[str, Any] | None,
    activities: list[dict[str, Any]],
    events: list[dict[str, Any]] | None = None,
    as_of: date | None = None,
) -> dict[str, Any]:
    if not prior_plan_context:
        return {
            "status": "none",
            "summary": "No prior approved plan was available for adherence review.",
        }

    final_plan = prior_plan_context.get("final_plan", {})
    planned_sessions = [
        session
        for session in final_plan.get("sessions", [])
        if session.get("discipline") != "Recovery"
    ]
    # Planning mid-week: sessions of the running week dated after the data
    # date are still to come, not missed.
    upcoming_sessions = [
        session
        for session in planned_sessions
        if as_of is not None and parse_date(session["date"]) > as_of
    ]
    planned_sessions = [
        session for session in planned_sessions if session not in upcoming_sessions
    ]
    if not planned_sessions:
        return {
            "status": "none",
            "summary": (
                "The prior approved plan has no sessions due yet."
                if upcoming_sessions
                else "No prior approved workouts were available for adherence review."
            ),
        }

    plan_start = min(parse_date(session["date"]) for session in planned_sessions)
    plan_end = max(parse_date(session["date"]) for session in planned_sessions)
    candidate_activities = [
        activity
        for activity in activities
        if plan_start - timedelta(days=PRIOR_PLAN_MOVE_TOLERANCE_DAYS)
        <= parse_date(activity.get("start_date_local", "1970-01-01"))
        <= plan_end + timedelta(days=PRIOR_PLAN_MOVE_TOLERANCE_DAYS)
    ]
    unmatched = list(candidate_activities)
    completed_on_day = 0
    completed_elsewhere = 0
    shortened = 0
    missed = 0
    key_sessions_planned = 0
    key_sessions_completed = 0
    moved_sessions: list[str] = []
    shortened_sessions: list[str] = []
    missed_sessions: list[str] = []
    total_score = 0.0
    pragmatic_completions = 0
    day_breakdown: dict[str, dict[str, int]] = defaultdict(
        lambda: {"planned": 0, "completed_any": 0, "completed_on_day": 0}
    )
    session_type_breakdown: dict[str, dict[str, int]] = defaultdict(
        lambda: {"planned": 0, "completed_any": 0, "completed_on_day": 0}
    )
    double_day_planned = 0
    double_day_fully_completed = 0
    planned_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    completion_by_day: dict[str, int] = defaultdict(int)
    session_reviews: list[dict[str, Any]] = []

    for session in planned_sessions:
        planned_by_day[session["day"]].append(session)
    # Intervals pairs a completed activity with its planned event
    # (`paired_event_id`); use that link before date/discipline heuristics.
    session_event_ids = {
        id(session): event_id
        for session in planned_sessions
        if (event_id := _session_event_id(session, events or [])) is not None
    }
    plan_event_ids = {str(value) for value in session_event_ids.values()}

    for session in planned_sessions:
        if session.get("priority") == "key":
            key_sessions_planned += 1
        day_breakdown[session["day"]]["planned"] += 1
        session_type = _session_type_label(session)
        session_type_breakdown[session_type]["planned"] += 1
        event_id = session_event_ids.get(id(session))
        paired = [
            activity
            for activity in unmatched
            if event_id is not None
            and str(activity.get("paired_event_id") or "") == str(event_id)
        ]
        matches = paired or [
            activity
            for activity in unmatched
            if _normalize_discipline(activity.get("type")) == session.get("discipline")
            and abs(
                (
                    parse_date(activity.get("start_date_local", "1970-01-01"))
                    - parse_date(session["date"])
                ).days
            )
            <= PRIOR_PLAN_MOVE_TOLERANCE_DAYS
            # An activity Intervals paired to another planned session is not a candidate.
            and str(activity.get("paired_event_id") or "") not in plan_event_ids
        ]
        if not matches:
            missed += 1
            missed_sessions.append(session["session_name"])
            session_reviews.append(
                {
                    "session_name": session["session_name"],
                    "day": session["day"],
                    "date": session["date"],
                    "discipline": session.get("discipline"),
                    "planned_minutes": int(session.get("duration_min", 0) or 0),
                    "actual_minutes": 0.0,
                    "essential_minutes": _session_completion_targets(session)[0],
                    "cooldown_tolerance_min": _session_completion_targets(session)[1],
                    "completion_ratio": 0.0,
                    "pragmatic_ratio": 0.0,
                    "status": "missed",
                    "average_heartrate": None,
                    "training_load": None,
                    **_activity_terrain(None),
                    "feel": None,
                    "feel_label": None,
                    **_review_extras(session, None),
                    "interpretation": _session_review_note(
                        session,
                        None,
                        "missed",
                        0.0,
                        0.0,
                        0.0,
                    ),
                }
            )
            continue

        match = min(matches, key=lambda item: _match_score(session, item))
        unmatched.remove(match)
        day_delta = (
            parse_date(match.get("start_date_local", "1970-01-01"))
            - parse_date(session["date"])
        ).days
        planned_minutes = max(int(session.get("duration_min", 0) or 0), 1)
        actual_minutes = _safe_float(match.get("moving_time")) / 60.0
        essential_minutes, cooldown_tolerance = _session_completion_targets(session)
        completion_ratio = actual_minutes / planned_minutes
        pragmatic_ratio = actual_minutes / max(essential_minutes, 1)
        review_status = "completed_on_day"
        if pragmatic_ratio < 0.55:
            missed += 1
            missed_sessions.append(session["session_name"])
            review_status = "missed"
            session_reviews.append(
                {
                    "session_name": session["session_name"],
                    "day": session["day"],
                    "date": session["date"],
                    "discipline": session.get("discipline"),
                    "planned_minutes": planned_minutes,
                    "actual_minutes": round(actual_minutes, 1),
                    "essential_minutes": essential_minutes,
                    "cooldown_tolerance_min": cooldown_tolerance,
                    "completion_ratio": round(completion_ratio, 2),
                    "pragmatic_ratio": round(pragmatic_ratio, 2),
                    "status": review_status,
                    "average_heartrate": match.get("average_heartrate"),
                    "training_load": match.get("icu_training_load"),
                    **_activity_terrain(match),
                    "feel": match.get("feel"),
                    "feel_label": _feel_label(match.get("feel")),
                    **_review_extras(session, match),
                    "interpretation": _session_review_note(
                        session,
                        match,
                        review_status,
                        actual_minutes,
                        completion_ratio,
                        pragmatic_ratio,
                    ),
                }
            )
            continue

        if completion_ratio < 0.7 and pragmatic_ratio < 0.9:
            shortened += 1
            shortened_sessions.append(session["session_name"])
            total_score += 0.6 if day_delta == 0 else 0.5
            day_breakdown[session["day"]]["completed_any"] += 1
            session_type_breakdown[session_type]["completed_any"] += 1
            completion_by_day[session["day"]] += 1
            review_status = "shortened"
        elif completion_ratio < 1.0 and pragmatic_ratio >= 0.9:
            pragmatic_completions += 1
            total_score += 1.0 if day_delta == 0 else 0.9
            day_breakdown[session["day"]]["completed_any"] += 1
            session_type_breakdown[session_type]["completed_any"] += 1
            if day_delta == 0:
                completed_on_day += 1
                day_breakdown[session["day"]]["completed_on_day"] += 1
                session_type_breakdown[session_type]["completed_on_day"] += 1
                review_status = "completed_pragmatic"
            else:
                completed_elsewhere += 1
                moved_sessions.append(session["session_name"])
                review_status = "completed_moved"
            completion_by_day[session["day"]] += 1
        elif day_delta == 0:
            completed_on_day += 1
            total_score += 1.0
            day_breakdown[session["day"]]["completed_any"] += 1
            day_breakdown[session["day"]]["completed_on_day"] += 1
            session_type_breakdown[session_type]["completed_any"] += 1
            session_type_breakdown[session_type]["completed_on_day"] += 1
            completion_by_day[session["day"]] += 1
        else:
            completed_elsewhere += 1
            moved_sessions.append(session["session_name"])
            total_score += 0.9
            day_breakdown[session["day"]]["completed_any"] += 1
            session_type_breakdown[session_type]["completed_any"] += 1
            completion_by_day[session["day"]] += 1
            review_status = "completed_moved"

        session_reviews.append(
            {
                "session_name": session["session_name"],
                "day": session["day"],
                "date": session["date"],
                "discipline": session.get("discipline"),
                "planned_minutes": planned_minutes,
                "actual_minutes": round(actual_minutes, 1),
                "essential_minutes": essential_minutes,
                "cooldown_tolerance_min": cooldown_tolerance,
                "completion_ratio": round(completion_ratio, 2),
                "pragmatic_ratio": round(pragmatic_ratio, 2),
                "status": review_status,
                "average_heartrate": match.get("average_heartrate"),
                "training_load": match.get("icu_training_load"),
                **_activity_terrain(match),
                "feel": match.get("feel"),
                "feel_label": _feel_label(match.get("feel")),
                **_review_extras(session, match),
                "interpretation": _session_review_note(
                    session,
                    match,
                    review_status,
                    actual_minutes,
                    completion_ratio,
                    pragmatic_ratio,
                ),
            }
        )

        if session.get("priority") == "key" and pragmatic_ratio >= 0.9:
            key_sessions_completed += 1

    for day, day_sessions in planned_by_day.items():
        if len(day_sessions) < 2:
            continue
        double_day_planned += 1
        if completion_by_day.get(day, 0) == len(day_sessions):
            double_day_fully_completed += 1

    total_sessions = len(planned_sessions)
    adherence_score = round(total_score / total_sessions, 2) if total_sessions else 0.0
    summary_parts = [
        f"Completed {completed_on_day + completed_elsewhere}/{total_sessions} planned sessions"
    ]
    if moved_sessions:
        summary_parts.append(f"moved {len(moved_sessions)}")
    if shortened_sessions:
        summary_parts.append(f"shortened {len(shortened_sessions)}")
    if pragmatic_completions:
        summary_parts.append(f"pragmatically completed {pragmatic_completions}")
    if missed_sessions:
        summary_parts.append(f"missed {len(missed_sessions)}")
    if upcoming_sessions:
        summary_parts.append(f"{len(upcoming_sessions)} still to come")

    unplanned_sessions = [
        {
            "date": str(activity.get("start_date_local"))[:10],
            "type": activity.get("type"),
            "name": activity.get("name"),
            "minutes": round(_safe_float(activity.get("moving_time")) / 60.0),
            "load": round(effective_training_load(activity), 1),
        }
        for activity in unmatched
        if activity.get("type") in VOLUME_TYPES
        and plan_start
        <= parse_date(activity.get("start_date_local", "1970-01-01"))
        <= plan_end
        and _safe_float(activity.get("moving_time")) / 60.0 >= UNPLANNED_MIN_MINUTES
    ]
    unplanned_load = round(sum(item["load"] for item in unplanned_sessions), 1)
    if unplanned_sessions:
        summary_parts.append(
            f"added {len(unplanned_sessions)} unplanned endurance session(s) (load {unplanned_load:g})"
        )
    compliance_counts: dict[str, int] = defaultdict(int)
    for review in session_reviews:
        compliance_counts[review.get("intensity_compliance", "unknown")] += 1
    week_load_actual = round(
        sum(
            effective_training_load(activity)
            for activity in activities
            if plan_start
            <= parse_date(activity.get("start_date_local", "1970-01-01"))
            <= plan_end
        ),
        1,
    )
    context_checkin = prior_plan_context.get("weekly_checkin") or {}

    return {
        "unplanned_sessions": unplanned_sessions,
        "unplanned_load": unplanned_load,
        "intensity_compliance_counts": dict(compliance_counts),
        "week_load_actual": week_load_actual,
        "readiness_status": final_plan.get("readiness_status"),
        "pain_or_niggles": context_checkin.get("pain_or_niggles"),
        "status": "available",
        "source_run": prior_plan_context.get("source_run"),
        "planned_week_start": plan_start.isoformat(),
        "planned_week_end": plan_end.isoformat(),
        "planned_sessions": total_sessions,
        "completed_on_planned_day": completed_on_day,
        "completed_on_other_day": completed_elsewhere,
        "shortened_sessions_count": shortened,
        "missed_sessions_count": missed,
        "key_sessions_planned": key_sessions_planned,
        "key_sessions_completed": key_sessions_completed,
        "moved_sessions": moved_sessions,
        "shortened_sessions": shortened_sessions,
        "missed_sessions": missed_sessions,
        "upcoming_sessions": [
            {
                "date": session["date"],
                "session_name": session["session_name"],
                "discipline": session.get("discipline"),
                "duration_min": int(session.get("duration_min", 0) or 0),
            }
            for session in upcoming_sessions
        ],
        "pragmatic_completions_count": pragmatic_completions,
        "day_breakdown": day_breakdown,
        "session_type_breakdown": session_type_breakdown,
        "double_days": {
            "planned": double_day_planned,
            "fully_completed": double_day_fully_completed,
        },
        "session_reviews": session_reviews,
        "completion_policy_note": (
            "Short cooldown trims are treated pragmatically when the core work is still present."
        ),
        "adherence_score": adherence_score,
        "summary": ", ".join(summary_parts) + ".",
    }


def _prior_plan_review(
    prior_plan_contexts: list[dict[str, Any]],
    activities: list[dict[str, Any]],
    events: list[dict[str, Any]] | None = None,
    as_of: date | None = None,
) -> dict[str, Any]:
    if not prior_plan_contexts:
        latest = _single_prior_plan_adherence(None, activities)
        return {
            "latest": latest,
            "history": [],
            "status": latest["status"],
            "weeks_reviewed": 0,
            "average_adherence_score": None,
            "summary": latest["summary"],
        }

    history = [
        _single_prior_plan_adherence(context, activities, events, as_of)
        for context in prior_plan_contexts
        if context is not None
    ]
    available_history = [item for item in history if item.get("status") == "available"]
    latest = (
        available_history[0]
        if available_history
        else _single_prior_plan_adherence(None, activities)
    )
    average_adherence_score = None
    if available_history:
        average_adherence_score = round(
            sum(
                float(item.get("adherence_score", 0.0) or 0.0)
                for item in available_history
            )
            / len(available_history),
            2,
        )
    return {
        "latest": latest,
        "history": history,
        "status": "available" if available_history else latest["status"],
        "weeks_reviewed": len(available_history),
        "average_adherence_score": average_adherence_score,
        "summary": latest["summary"],
    }


def _session_archetype(session: dict[str, Any]) -> str | None:
    discipline = session.get("discipline")
    name = str(session.get("session_name", "")).lower()
    if discipline == "Recovery":
        return "recovery"
    if discipline == "WeightTraining":
        return "strength"
    if discipline == "Swim":
        return "swim_club" if session.get("day") == "Tuesday" else "swim_pool"
    if discipline == "Run" and "long" in name:
        return "long_run"
    if discipline == "Ride" and "long" in name:
        return "long_ride"
    if discipline == "Run" and (
        session.get("primary_metric") == "Pace"
        or session.get("daniels_type") in {"T", "I", "R"}
    ):
        return "quality_run"
    if discipline == "Ride" and (
        session.get("priority") == "key"
        or session.get("coggan_level") in {"L4 Threshold", "L5 VO2max", "L6 Anaerobic"}
        or float(session.get("expected_if", 0) or 0) >= 0.78
    ):
        return "threshold_ride"
    if discipline == "Ride" and (
        "cadence" in name or session.get("coggan_level") == "L3 Tempo"
    ):
        return "support_ride"
    if discipline == "Run":
        return "easy_run"
    if discipline == "Ride":
        return "endurance_ride"
    return None


def _recent_session_signatures(
    prior_plan_contexts: list[dict[str, Any]], limit_weeks: int = 4
) -> dict[str, list[str]]:
    signatures: dict[str, list[str]] = defaultdict(list)
    for context in prior_plan_contexts[:limit_weeks]:
        final_plan = context.get("final_plan", {})
        for session in final_plan.get("sessions", []):
            archetype = _session_archetype(session)
            if not archetype:
                continue
            for label in (session.get("variant_id"), session.get("session_name")):
                if not label:
                    continue
                normalized = str(label)
                if normalized not in signatures[archetype]:
                    signatures[archetype].append(normalized)
    return dict(signatures)


def _sport_settings_map(settings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for item in settings:
        for sport in item.get("types", []):
            mapping[sport] = item
    return mapping


def _confidence_label(
    baseline_count: int, acute_count: int, minimum_entries: int
) -> str:
    if baseline_count >= minimum_entries and acute_count >= 3:
        return "high"
    if baseline_count >= max(7, minimum_entries // 2) and acute_count >= 2:
        return "medium"
    if baseline_count >= 1 and acute_count >= 1:
        return "low"
    return "missing"


def _derive_wellness_baseline(
    wellness: list[dict[str, Any]],
    field: str,
    latest_day,
    *,
    baseline_days: int,
    acute_days: int,
    minimum_entries: int,
    transform=lambda value: float(value),
) -> dict[str, Any]:
    series: list[tuple[Any, float]] = []
    for item in wellness:
        raw = item.get(field)
        if raw in (None, ""):
            continue
        try:
            series.append((parse_date(item["id"]), float(transform(raw))))
        except (KeyError, TypeError, ValueError):
            continue

    acute_start = latest_day - timedelta(days=acute_days - 1)
    baseline_start = acute_start - timedelta(days=baseline_days)
    baseline_values = [
        value for day, value in series if baseline_start <= day < acute_start
    ]
    if not baseline_values:
        baseline_values = [value for day, value in series if day < acute_start]
    acute_values = [value for day, value in series if acute_start <= day <= latest_day]

    if not baseline_values:
        return {
            "status": "insufficient_data",
            "baseline": None,
            "acute": None,
            "delta": None,
            "delta_pct": None,
            "baseline_entries": 0,
            "acute_entries": len(acute_values),
            "confidence": "missing",
            "source": "intervals",
        }

    baseline = round(float(median(baseline_values)), 2)
    if not acute_values:
        # An empty acute window is missing data, never "normal".
        return {
            "status": "insufficient_data",
            "baseline": baseline,
            "acute": None,
            "delta": None,
            "delta_pct": None,
            "baseline_entries": len(baseline_values),
            "acute_entries": 0,
            "confidence": "missing",
            "source": "intervals",
        }
    acute = round(float(sum(acute_values) / len(acute_values)), 2)
    delta = round(acute - baseline, 2)
    delta_pct = round(((acute - baseline) / baseline) * 100.0, 2) if baseline else None
    return {
        "status": "available",
        "baseline": baseline,
        "acute": acute,
        "delta": delta,
        "delta_pct": delta_pct,
        "baseline_entries": len(baseline_values),
        "acute_entries": len(acute_values),
        "confidence": _confidence_label(
            len(baseline_values), len(acute_values), minimum_entries
        ),
        "source": "intervals",
    }


# A completed week below this share of the two weeks before it is a break week.
BREAK_WEEK_LOAD_RATIO = 0.25


def _classify_periodization_state(
    weekly_windows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Classify Mon-Sun weeks as load/recovery.

    A partial (still running) calendar week is never called a recovery week:
    it is `load` once it already reached the load threshold, otherwise
    `in_progress` and ignored for the block count.
    """
    classified: list[dict[str, Any]] = []
    for index, window in enumerate(weekly_windows):
        load = float(window.get("load", 0))
        minutes = float(window.get("minutes", 0))
        week_type = "load"
        if index >= 2:
            prior_loads = [
                float(weekly_windows[index - 1]["load"]),
                float(weekly_windows[index - 2]["load"]),
            ]
            prior_minutes = [
                float(weekly_windows[index - 1]["minutes"]),
                float(weekly_windows[index - 2]["minutes"]),
            ]
            avg_load = sum(prior_loads) / len(prior_loads)
            avg_minutes = sum(prior_minutes) / len(prior_minutes)
            if (avg_load and load <= avg_load * 0.82) or (
                avg_minutes and minutes <= avg_minutes * 0.82
            ):
                week_type = "in_progress" if window.get("partial") else "recovery"
        elif window.get("partial") and load == 0 and minutes == 0:
            week_type = "in_progress"
        # A (near-)empty completed week is a break (illness, holiday), not a
        # planned recovery week: the week after it is a re-entry, not a build.
        prior_settled = [
            float(item.get("load", 0)) for item in weekly_windows[max(index - 2, 0) : index]
        ]
        reference_load = sum(prior_settled) / len(prior_settled) if prior_settled else 0.0
        break_week = not window.get("partial") and (
            (load == 0 and minutes == 0)
            or (reference_load > 0 and load <= BREAK_WEEK_LOAD_RATIO * reference_load)
        )
        if break_week and week_type == "load" and index >= 1:
            week_type = "recovery"
        classified.append({**window, "week_type": week_type, "break_week": bool(break_week)})

    consecutive_load_weeks = 0
    for window in reversed(classified):
        if window.get("week_type") == "in_progress":
            continue
        if window.get("load", 0) == 0 and window.get("minutes", 0) == 0:
            continue
        if window.get("week_type") == "recovery":
            break
        consecutive_load_weeks += 1

    settled = [item for item in classified if item["week_type"] != "in_progress"]
    last_week_type = settled[-1]["week_type"] if settled else "unknown"
    last_week_break = bool(settled[-1].get("break_week")) if settled else False
    recommended_next_week_type = "recovery" if consecutive_load_weeks >= 3 else "load"
    next_block_week = (
        4
        if recommended_next_week_type == "recovery"
        else (1 if last_week_type == "recovery" else min(consecutive_load_weeks + 1, 3))
    )
    return {
        "classified_weeks": classified,
        "consecutive_load_weeks": consecutive_load_weeks,
        "last_week_type": last_week_type,
        "last_week_was_break": last_week_break,
        "recommended_next_week_type": recommended_next_week_type,
        "next_block_week": next_block_week,
    }


def _infer_distance_from_text(*values: str | None) -> str | None:
    """Human-readable distance label from the shared race_selection buckets.

    Handles trail/ultra races ("Hill Trail" + "50 km" -> "Ultra Trail")
    and never reads "110k" as a 10k. Returns None when nothing is recognised.
    """
    return distance_label(*[value for value in values if value])


def _interval_event_priority(category: str | None) -> str | None:
    mapping = {
        "RACE_A": "A",
        "RACE_B": "B",
        "RACE_C": "C",
        "TARGET": "A",
    }
    return mapping.get(category or "")


def _extract_interval_races(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    races = []
    for event in events:
        priority = _interval_event_priority(event.get("category"))
        if not priority:
            continue
        inferred = _infer_distance_from_text(
            event.get("type"), event.get("name"), event.get("description")
        )
        races.append(
            {
                "name": event.get("name", "Untitled race"),
                "date": event.get("start_date_local", "")[:10],
                "priority": priority,
                "distance": inferred or event.get("type") or "Unknown",
                # Only a distance read from the Intervals text is authoritative;
                # a bare sport type may be filled from the local calendar.
                "distance_source": "intervals_text" if inferred else "intervals_type",
                "notes": event.get("description", ""),
                "source": "intervals",
                "tags": event.get("tags", []),
            }
        )
    return races


HEALTH_EVENT_CATEGORIES = {"SICK": "sick", "INJURED": "injured"}
# Long illnesses stay visible for four weeks so the doctor can run an extended
# return-to-training protocol; short colds still only matter for ~14 days.
HEALTH_EVENT_LOOKBACK_DAYS = 28
EXTENDED_ILLNESS_MIN_DAYS = 7
EXTENDED_ILLNESS_KEYWORDS = (
    "covid",
    "corona",
    "flu",
    "griep",
    "influenza",
    "fever",
    "koorts",
    "pneumonia",
    "longontsteking",
    "mono",
    "pfeiffer",
)
HOLIDAY_LOOKBACK_DAYS = 28
HOLIDAY_LOOKAHEAD_DAYS = 28


def _health_event_severity(kind: str, name: str, description: str, duration_days: int) -> str:
    text = f"{name} {description}".lower()
    if kind == "sick" and (
        duration_days >= EXTENDED_ILLNESS_MIN_DAYS
        or any(re.search(rf"\b{keyword}", text) for keyword in EXTENDED_ILLNESS_KEYWORDS)
    ):
        return "extended"
    if kind == "injured" and duration_days >= EXTENDED_ILLNESS_MIN_DAYS:
        return "extended"
    return "standard"


def _extract_holidays(
    events: list[dict[str, Any]], planning_date: date, week_start: date
) -> list[dict[str, Any]]:
    """Intervals HOLIDAY events around the planning date, with planned-week overlap."""
    week_end = week_start + timedelta(days=6)
    holidays = []
    for event in events:
        if str(event.get("category") or "").upper() != "HOLIDAY":
            continue
        raw_start = str(event.get("start_date_local") or "")[:10]
        if not raw_start:
            continue
        start_day = parse_date(raw_start)
        end_day = _health_event_end_day(event, start_day)
        if end_day < planning_date - timedelta(days=HOLIDAY_LOOKBACK_DAYS):
            continue
        if start_day > week_end + timedelta(days=HOLIDAY_LOOKAHEAD_DAYS):
            continue
        overlap_start = max(start_day, week_start)
        overlap_end = min(end_day, week_end)
        overlap = max((overlap_end - overlap_start).days + 1, 0)
        recent_start = planning_date - timedelta(days=13)
        recent_overlap = max(
            (min(end_day, planning_date) - max(start_day, recent_start)).days + 1, 0
        )
        holidays.append(
            {
                "name": event.get("name") or "Holiday",
                "start_date": start_day.isoformat(),
                "end_date": end_day.isoformat(),
                "in_planning_week": overlap > 0,
                "days_overlap_planning_week": overlap,
                "days_overlap_last_14": recent_overlap,
            }
        )
    holidays.sort(key=lambda item: item["start_date"])
    return holidays


def _health_event_end_day(event: dict[str, Any], start_day: date) -> date:
    raw_end = str(event.get("end_date_local") or "")
    if not raw_end[:10]:
        return start_day
    end_day = parse_date(raw_end[:10])
    # Intervals stores multi-day events with an exclusive midnight end date.
    if end_day > start_day and raw_end[11:19] in ("", "00:00:00"):
        end_day -= timedelta(days=1)
    return max(end_day, start_day)


def _extract_health_events(
    events: list[dict[str, Any]], planning_date: date
) -> list[dict[str, Any]]:
    health_events = []
    for event in events:
        kind = HEALTH_EVENT_CATEGORIES.get(str(event.get("category") or "").upper())
        raw_start = str(event.get("start_date_local") or "")[:10]
        if not kind or not raw_start:
            continue
        start_day = parse_date(raw_start)
        if start_day > planning_date:
            continue
        end_day = _health_event_end_day(event, start_day)
        days_since_end = (planning_date - end_day).days
        if days_since_end > HEALTH_EVENT_LOOKBACK_DAYS:
            continue
        name = event.get("name") or kind.capitalize()
        description = event.get("description") or ""
        duration_days = (end_day - start_day).days + 1
        health_events.append(
            {
                "kind": kind,
                "name": name,
                "description": description,
                "start_date": start_day.isoformat(),
                "end_date": end_day.isoformat(),
                "days_since_end": max(days_since_end, 0),
                "ongoing": days_since_end <= 0,
                "duration_days": duration_days,
                "severity": _health_event_severity(
                    kind, str(name), str(description), duration_days
                ),
            }
        )
    health_events.sort(key=lambda item: (item["days_since_end"], item["start_date"]))
    return health_events


RACE_IDENTITY_FIELDS = {"name", "date", "priority", "distance", "source", "days_to_race", "distance_bucket"}


def _race_name_tokens(name: Any) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(name or "").lower())
        if token not in {"the", "de", "la", "le", "van", "race", "of"}
    }


def _names_match(left: Any, right: Any) -> bool:
    a, b = _race_name_tokens(left), _race_name_tokens(right)
    if not a or not b:
        return False
    return a <= b or b <= a or len(a & b) / min(len(a), len(b)) >= 0.5


def _value_missing(value: Any) -> bool:
    return value in (None, "", [], {}) or str(value).strip().lower() == "unknown"


def _merge_race_calendars(
    interval_races: list[dict[str, Any]], manual_races: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Intervals RACE events are the only source of races and goals.

    A local race calendar can never add a race or change an Intervals race's
    date, name, priority or distance. It may only fill fields the matching
    Intervals race lacks (notes, demands, a distance Intervals does not state).
    Match = date within 1 day plus a similar name, or the date alone when only
    one Intervals race sits within that day. Unmatched local races are ignored
    and reported in `race_calendar_warnings`.
    """
    merged = [dict(race) for race in interval_races]
    warnings: list[str] = []
    for manual in manual_races:
        try:
            manual_day = parse_date(str(manual.get("date") or ""))
        except ValueError:
            warnings.append(
                f"{manual.get('name') or 'Unnamed race'} has no valid date in the local race calendar; ignored."
            )
            continue
        near = []
        for race in merged:
            try:
                race_day = parse_date(str(race.get("date") or ""))
            except ValueError:
                continue
            if abs((race_day - manual_day).days) <= 1:
                near.append(race)
        named = [race for race in near if _names_match(race.get("name"), manual.get("name"))]
        target = named[0] if len(named) == 1 else (near[0] if len(near) == 1 and not named else None)
        if target is None:
            warnings.append(
                f"{manual.get('name') or 'Unnamed race'} {manual.get('date')} is in the local race calendar "
                "but not on the Intervals calendar; ignored."
            )
            continue
        filled = False
        for key, value in manual.items():
            if key in RACE_IDENTITY_FIELDS - {"distance"} or _value_missing(value):
                continue
            if key == "distance":
                if target.get("distance_source") == "intervals_text":
                    continue
                target["distance"] = value
                target["distance_source"] = "local_calendar_fill"
                filled = True
            elif _value_missing(target.get(key)):
                target[key] = value
                filled = True
        if filled:
            target["source"] = "intervals+manual"
    return merged, warnings


def _sanitize_athlete_state(athlete_state: dict[str, Any]) -> dict[str, Any]:
    """Copy of the athlete state without a persisted priority race.

    Races and goals come only from the Intervals calendar; the last planned
    priority race is kept for information under a name nothing race-related reads.
    """
    cycle = athlete_state.get("active_macro_cycle")
    if not isinstance(cycle, dict) or "priority_race" not in cycle:
        return athlete_state
    cleaned_cycle = {key: value for key, value in cycle.items() if key != "priority_race"}
    if cycle.get("priority_race"):
        cleaned_cycle["last_planned_priority_race"] = cycle["priority_race"]
    return {**athlete_state, "active_macro_cycle": cleaned_cycle}


# Activity notes (descriptions / comments) scanned for health signals.
NOTE_LOOKBACK_DAYS = 14
NOTE_RED_FLAG_RE = re.compile(
    r"chest pain|chest tight\w*|tight chest|pain in (?:my |the )?chest|borstpijn|"
    r"pijn (?:in|op) (?:de|mijn) borst|druk op (?:de|mijn) borst|beklemm\w*|"
    r"palpitations?|hartkloppingen|heart racing|racing heart|irregular heart\w*|"
    r"hartritme\w*|dizz(?:y|iness)|duizel\w*|faint\w*|flauw\w*|"
    r"short(?:ness)? of breath|kortademig\w*|benauwd\w*|black(?:ed)? ?out"
)
NOTE_ILLNESS_RE = re.compile(
    r"\b(?:sick|ill|illness|ziek\w*|fever|koorts\w*|flu|griep\w*|covid|corona|"
    r"verkoud\w*|(?:caught|have|had|got) a cold|head cold|sore throat|keelpijn|cough\w*|hoest\w*|infect\w*|"
    r"stomach bug|buikgriep|misselijk\w*|nause\w*)\b"
)
NOTE_PAIN_RE = re.compile(
    r"\b(?:pain\w*|pijn\w*|hurt\w*|injur\w*|blessure\w*|ontsteking\w*|inflam\w*|"
    r"strain\w*|verrek\w*|tendinitis|tendinopathy|shin splints?)\b"
)
# Discomfort words only count as pain when a body part is named (no DOMS noise).
NOTE_DISCOMFORT_RE = re.compile(
    r"\b(?:sore\w*|ach(?:e|es|ing|y)|tight\w*|stiff\w*|stijf\w*|gevoelig\w*|"
    r"niggle\w*|tweak\w*|cramp\w*|kramp\w*|swollen|gezwollen|opgezet)\b"
)
NOTE_NEGATION_RE = re.compile(
    r"\b(?:no|geen|without|zonder|not|niet|never|nooit|didn'?t|didnt|zero|free of|"
    r"no more|niet meer)\b[\w\s'-]{0,20}$"
)
NOTE_FREE_SUFFIX_RE = re.compile(r"^[- ]?(?:free|vrij)\b")
NOTE_CLAUSE_RE = re.compile(r"[,;.\n!?]|\bbut\b|\bmaar\b|\balthough\b|\bhowever\b")


def _unnegated(pattern: re.Pattern[str], clause: str) -> bool:
    for match in pattern.finditer(clause):
        before = clause[: match.start()]
        after = clause[match.end() :]
        if NOTE_NEGATION_RE.search(before) or NOTE_FREE_SUFFIX_RE.match(after):
            continue
        return True
    return False


def classify_note_clause(clause: str) -> tuple[str, str | None] | None:
    """(kind, body_part) for a note clause, or None. Negations are ignored."""
    text = clause.strip().lower()
    if not text:
        return None
    if _unnegated(NOTE_RED_FLAG_RE, text):
        return "red_flag", None
    if _unnegated(NOTE_ILLNESS_RE, text):
        return "illness", None
    if not has_niggle(text):
        # "ITB fine", "no pain", "geen pijn": a clear clause.
        return None
    parts = niggle_body_parts(text)
    if _unnegated(NOTE_PAIN_RE, text) or (parts and _unnegated(NOTE_DISCOMFORT_RE, text)):
        return "pain", (parts[0] if parts else None)
    return None


def _note_texts(
    activity: dict[str, Any], messages: dict[str, Any]
) -> list[str]:
    texts = []
    for key in ("description", "notes", "private_note"):
        if activity.get(key):
            texts.append(str(activity[key]))
    for message in messages.get(str(activity.get("id")), []) or []:
        if isinstance(message, dict):
            content = message.get("content") or message.get("message") or message.get("text")
            if content:
                texts.append(str(content))
        elif message:
            texts.append(str(message))
    return texts


def extract_activity_note_flags(
    activities: list[dict[str, Any]],
    planning_date: date,
    messages: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Pain / illness / red-flag mentions in activity descriptions and comments (last 14 days)."""
    start = planning_date - timedelta(days=NOTE_LOOKBACK_DAYS - 1)
    flags: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for activity in activities:
        raw_day = str(activity.get("start_date_local") or "")[:10]
        try:
            day = parse_date(raw_day)
        except ValueError:
            continue
        if not (start <= day <= planning_date):
            continue
        for text in _note_texts(activity, messages or {}):
            for clause in NOTE_CLAUSE_RE.split(text):
                result = classify_note_clause(clause)
                if result is None:
                    continue
                kind, body_part = result
                excerpt = " ".join(clause.split())[:140]
                key = (raw_day, kind, excerpt.lower())
                if key in seen:
                    continue
                seen.add(key)
                flags.append(
                    {
                        "date": raw_day,
                        "activity_name": activity.get("name"),
                        "kind": kind,
                        "excerpt": excerpt,
                        "body_part": body_part,
                    }
                )
    return flags


# A wellness row is fresh wellness only when it carries a measured or entered
# signal; Intervals creates rows with only computed CTL / ATL / rampRate.
WELLNESS_SIGNAL_FIELDS = (
    "hrv",
    "hrvSDNN",
    "restingHR",
    "sleepSecs",
    "sleepScore",
    "sleepQuality",
    "avgSleepingHR",
    "weight",
    "soreness",
    "fatigue",
    "stress",
    "mood",
    "motivation",
    "injury",
    "readiness",
    "spO2",
    "respiration",
)


def _latest_field_date(
    wellness: list[dict[str, Any]], fields: tuple[str, ...], planning_date: date
) -> date | None:
    latest = None
    for item in wellness:
        if not any(item.get(field) not in (None, "") for field in fields):
            continue
        try:
            day = parse_date(str(item.get("id") or "")[:10])
        except ValueError:
            continue
        if day <= planning_date and (latest is None or day > latest):
            latest = day
    return latest


def _signal_freshness(
    wellness: list[dict[str, Any]], planning_date: date
) -> dict[str, Any]:
    dates = {
        "latest_hrv_date": _latest_field_date(wellness, ("hrv",), planning_date),
        "latest_resting_hr_date": _latest_field_date(wellness, ("restingHR",), planning_date),
        "latest_sleep_date": _latest_field_date(
            wellness, ("sleepSecs", "sleepScore", "sleepQuality"), planning_date
        ),
        "latest_wellness_signal_date": _latest_field_date(
            wellness, WELLNESS_SIGNAL_FIELDS, planning_date
        ),
    }
    labels = {
        "latest_hrv_date": "HRV",
        "latest_resting_hr_date": "Resting HR",
        "latest_sleep_date": "Sleep",
    }
    warnings = []
    for key, label in labels.items():
        day = dates[key]
        if day is not None and (planning_date - day).days > SIGNAL_STALE_DAYS:
            warnings.append(
                f"{label} was last recorded {day.isoformat()} ({(planning_date - day).days} days before planning)."
            )
    return {
        **{key: (value.isoformat() if value else None) for key, value in dates.items()},
        "signal_warnings": warnings,
        "_latest_signal_day": dates["latest_wellness_signal_date"],
    }


def normalize_snapshot(
    local_inputs: dict[str, Any], intervals_data: dict[str, Any]
) -> dict[str, Any]:
    athlete_profile = local_inputs["athlete_profile"]
    weekly_checkin = local_inputs["weekly_checkin"]
    # Races and goals come only from the Intervals calendar: a persisted
    # priority race in the athlete state is information, never a race source.
    athlete_state = _sanitize_athlete_state(local_inputs.get("athlete_state", {}))
    race_calendar = local_inputs.get("race_calendar") or {}
    prior_plan_contexts = local_inputs.get("prior_plan_contexts", [])
    # Sessions without an Intervals load (tennis, untracked HR) get an
    # `estimated_training_load` so weekly load sums do not silently drop them.
    activities = annotate_estimated_loads(
        sorted(intervals_data["activities"], key=lambda item: item["start_date_local"])
    )
    wellness = sorted(intervals_data["wellness"], key=lambda item: item["id"])
    requested_planning_date = parse_date(weekly_checkin["planning_date"])
    week_start = start_of_planned_week(weekly_checkin["planning_date"])
    # Evidence (load windows, wellness, freshness) runs up to the data date:
    # the fetch day when planning ahead ("plan the week of <next Monday>" on a
    # Tuesday), so future days never read as missed training or stale data.
    planning_date = data_as_of_date(
        requested_planning_date, intervals_data.get("fetch_timestamp")
    )
    # Rolling 7-day windows for "last 7 days vs the 7 before" load totals.
    rolling_windows = chunk_weekly(planning_date, activities, weeks=2)
    # Calendar Mon-Sun weeks before the planned week for periodization. The
    # approved plan of the running week lets a Sunday check-in close the week
    # once Sunday's planned sessions are done.
    running_week_sessions = [
        session
        for context in prior_plan_contexts
        for session in context.get("final_plan", {}).get("sessions", [])
        if str(session.get("date") or "")[:10] == planning_date.isoformat()
    ]
    week_windows = calendar_weeks(
        week_start,
        activities,
        weeks=8,
        planning_date=planning_date,
        planned_sessions=running_week_sessions or None,
    )

    latest_wellness = wellness[-1] if wellness else {}
    # The last 7 calendar days, not the last 7 entries.
    recent_wellness = _dated_rows(
        wellness, planning_date - timedelta(days=6), planning_date
    )
    latest_wellness_day = (
        parse_date(latest_wellness["id"]) if latest_wellness else planning_date
    )
    latest_activity_day = (
        parse_date(activities[-1]["start_date_local"]) if activities else planning_date
    )
    sport_settings_map = _sport_settings_map(intervals_data.get("sport_settings", []))
    by_sport: dict[str, dict[str, float]] = defaultdict(
        lambda: {"sessions": 0, "load": 0.0, "minutes": 0.0}
    )
    for activity in activities:
        sport = activity.get("type", "Other")
        bucket = by_sport[sport]
        bucket["sessions"] += 1
        bucket["load"] += effective_training_load(activity)
        bucket["minutes"] += _safe_float(activity.get("moving_time")) / 60.0

    recent_week = rolling_windows[-1]["activities"] if rolling_windows else []
    prior_week = rolling_windows[-2]["activities"] if len(rolling_windows) >= 2 else []
    recent_week_load = sum(effective_training_load(item) for item in recent_week)
    prior_week_load = sum(effective_training_load(item) for item in prior_week)
    recent_week_minutes = int(sum_minutes(recent_week) / 60)
    prior_week_minutes = int(sum_minutes(prior_week) / 60)
    load_change_pct = 0.0
    if prior_week_load:
        load_change_pct = (
            (recent_week_load - prior_week_load) / prior_week_load
        ) * 100.0

    interval_races = _extract_interval_races(intervals_data.get("events", []))
    recent_health_events = _extract_health_events(
        intervals_data.get("events", []), planning_date
    )
    holidays = _extract_holidays(
        intervals_data.get("events", []), planning_date, week_start
    )
    merged_races, race_calendar_warnings = _merge_race_calendars(
        interval_races, race_calendar.get("races", [])
    )
    merged_race_calendar = {"races": merged_races, "source": "intervals"}

    races = []
    for race in merged_race_calendar.get("races", []):
        race_day = parse_date(race["date"])
        enriched = dict(race)
        enriched["days_to_race"] = (race_day - week_start).days
        enriched["distance_bucket"] = race_bucket(enriched)
        races.append(enriched)
    races.sort(key=lambda item: (item["days_to_race"], item["priority"]))

    assumptions = []
    if not sport_settings_map.get("Ride", {}).get("ftp"):
        assumptions.append(
            "Bike FTP missing; live compiler would fall back from %FTP to HR."
        )
    run_settings = sport_settings_map.get("Run", {})
    if not run_settings.get("threshold_pace"):
        assumptions.append(
            "Run threshold pace missing; quality run compiler would fall back from Pace to HR."
        )
    if not recent_wellness:
        assumptions.append("Wellness data missing; readiness confidence reduced.")
    if not races:
        assumptions.append(
            "No race events were found on the Intervals calendar (the only race source). Planning will stay general."
        )

    summarized_windows = [
        {
            "week_start": window["week_start"],
            "week_end": window["week_end"],
            "load": round(
                sum(effective_training_load(item) for item in window["activities"]),
                1,
            ),
            "minutes": int(sum_minutes(window["activities"]) / 60),
            "sessions": len(window["activities"]),
            "partial": window["partial"],
        }
        for window in week_windows
    ]
    derived_baselines = {
        "hrv": _derive_wellness_baseline(
            wellness,
            "hrv",
            planning_date,
            baseline_days=42,
            acute_days=7,
            minimum_entries=21,
        ),
        "resting_hr": _derive_wellness_baseline(
            wellness,
            "restingHR",
            planning_date,
            baseline_days=42,
            acute_days=7,
            minimum_entries=21,
        ),
        "sleep_hours": _derive_wellness_baseline(
            wellness,
            "sleepSecs",
            planning_date,
            baseline_days=28,
            acute_days=7,
            minimum_entries=14,
            transform=lambda value: float(value) / 3600,
        ),
        "sleep_score": _derive_wellness_baseline(
            wellness,
            "sleepScore",
            planning_date,
            baseline_days=28,
            acute_days=7,
            minimum_entries=14,
        ),
    }
    periodization_state = _classify_periodization_state(summarized_windows)
    planning_constraints = _normalize_planning_constraints(
        athlete_profile, weekly_checkin
    )
    training_load = assess_training_load(
        activities=activities,
        wellness=wellness,
        athlete_profile=athlete_profile,
        athlete_state=athlete_state,
        health_events=recent_health_events,
        planning_date=planning_date,
        requested_week_type=planning_constraints.get("requested_week_type"),
        races=races,
        holidays=holidays,
        week_start=week_start,
        sport_settings=sport_settings_map,
    )
    threshold_testing = assess_threshold_testing(
        athlete_profile=athlete_profile,
        weekly_checkin=weekly_checkin,
        athlete_state=athlete_state,
        activities=activities,
        races=races,
        health_events=recent_health_events,
        sport_settings=sport_settings_map,
        test_intervals=intervals_data.get("threshold_test_intervals", {}),
        week_start=week_start,
        block_context={
            "last_week_type": periodization_state.get("last_week_type"),
            "next_block_week": periodization_state.get("next_block_week"),
        },
    )
    prior_plan_review = _prior_plan_review(
        prior_plan_contexts,
        activities,
        intervals_data.get("events", []),
        as_of=planning_date,
    )
    prior_plan_adherence = prior_plan_review["latest"]
    athlete_memory = build_athlete_memory(
        prior_plan_review["history"],
        planning_constraints,
        athlete_profile,
        weekly_checkin,
    )
    avg_sleep_hours = _average_numeric_field(
        recent_wellness,
        "sleepSecs",
        transform=lambda value: float(value) / 3600,
    )
    avg_hrv = _average_numeric_field(recent_wellness, "hrv")
    avg_resting_hr = _average_numeric_field(recent_wellness, "restingHR")
    freshness_reasons: list[str] = []
    signal_freshness = _signal_freshness(wellness, planning_date)
    # A wellness row holding only computed CTL / ATL is not fresh wellness.
    latest_physio_day = signal_freshness.pop("_latest_signal_day")
    latest_signal_day = max(
        [day for day in (latest_activity_day, latest_physio_day) if day is not None]
    )
    freshness_gap = (planning_date - latest_signal_day).days
    if freshness_gap > 1:
        freshness_reasons.append(
            f"Latest live data is {freshness_gap} day(s) older than the data date ({planning_date.isoformat()})."
        )
    if not activities:
        freshness_reasons.append("No activities were returned from Intervals.")
    if not recent_wellness:
        freshness_reasons.append(
            "No recent wellness entries were returned from Intervals."
        )
    freshness_status = "fresh" if not freshness_reasons else "stale"

    return {
        "athlete_profile": athlete_profile,
        "weekly_checkin": weekly_checkin,
        "athlete_state": athlete_state,
        "planning_constraints": planning_constraints,
        "prior_plan_review": prior_plan_review,
        "prior_plan_adherence": prior_plan_adherence,
        "prior_plan_history": prior_plan_review["history"],
        "recent_session_signatures_by_kind": _recent_session_signatures(
            prior_plan_contexts
        ),
        "athlete_memory": athlete_memory,
        "race_calendar": merged_race_calendar,
        "intervals": intervals_data,
        "week_start": week_start.isoformat(),
        "planning_date": requested_planning_date.isoformat(),
        "data_as_of": planning_date.isoformat(),
        "lead_days": (week_start - planning_date).days,
        "latest_activity_day": latest_activity_day.isoformat(),
        "latest_wellness_day": latest_wellness_day.isoformat(),
        "freshness": {
            "status": freshness_status,
            "reasons": freshness_reasons,
            "gap_days": freshness_gap,
            "data_as_of": planning_date.isoformat(),
            "fetch_timestamp": intervals_data.get("fetch_timestamp"),
            **signal_freshness,
        },
        "latest_wellness": latest_wellness,
        "recent_wellness": recent_wellness,
        "sport_settings": sport_settings_map,
        "recent_week_load": round(recent_week_load, 1),
        "prior_week_load": round(prior_week_load, 1),
        "recent_week_minutes": recent_week_minutes,
        "prior_week_minutes": prior_week_minutes,
        "load_change_pct": round(load_change_pct, 1),
        "weekly_windows": summarized_windows,
        "avg_sleep_hours": (
            round(avg_sleep_hours, 2) if avg_sleep_hours is not None else None
        ),
        "avg_hrv": round(avg_hrv, 1) if avg_hrv is not None else None,
        "avg_resting_hr": (
            round(avg_resting_hr, 1) if avg_resting_hr is not None else None
        ),
        "derived_baselines": derived_baselines,
        "periodization_state": periodization_state,
        "sports_summary": by_sport,
        "recent_wellness_summary": _recent_wellness_summary(wellness, planning_date),
        "activity_note_flags": extract_activity_note_flags(
            activities, planning_date, intervals_data.get("activity_messages") or {}
        ),
        "activity_notes_errors": list(intervals_data.get("activity_messages_errors") or []),
        "races": races,
        "race_calendar_warnings": race_calendar_warnings,
        "recent_health_events": recent_health_events,
        "holidays": holidays,
        "threshold_testing": threshold_testing,
        "training_load": training_load,
        "progression": assess_progression(
            prior_plan_contexts,
            prior_plan_review["history"],
            planning_date=planning_date,
        ),
        "assumptions": assumptions,
    }
