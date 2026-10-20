"""Deterministic training-load signals for the weekly planning snapshot.

Everything here is computed from Intervals activities and wellness so the
coaching agents can reason about *under*-loading and *over*-loading as
explicitly as they reason about fatigue:

- fitness trend (CTL / ATL / TSB and the weekly CTL ramp)
- ramp / spike safety (CTL ramp, acute:chronic workload ratio, week-on-week load)
- three-zone intensity distribution over the last 28 days
- how many genuine quality sessions landed per week
- run-specific load (km, time, D+/D-, longest run) for trail/ITB risk
- non-endurance load (tennis, hikes, strength) with estimated load
- an HRV intensity gate on lnRMSSD (Plews / Javaloyes style)
- an underload assessment that combines those markers, with explicit
  explanations (taper, post-race transition, holiday, illness), a ramp veto
  and a physiological veto (suppressed HRV, raised RHR, high fatigue)
- return-from-break detection (illness / holiday / 7+ days off) with a
  graded re-entry ramp (50/60/75/90% of the pre-break week)
- daily early-warning signals (RHR / HRV over the last 1-3 days)
- functional vs non-functional overreaching and an efficiency trend

Windows are date-based and anchored on the planning date. Acute load windows
end on the last *complete* day (yesterday, or today when today already holds
training) and the chronic window (days 8-35) is uncoupled from it.

See `prompts/training_intensity_doctrine.md` for how the agents use them.
"""

from __future__ import annotations

from datetime import date, timedelta
from math import exp, log
import re
from statistics import mean, median, pstdev
from typing import Any, Callable

from .race_selection import (
    distance_bucket,
    last_completed_a_race,
    race_bucket,
    select_priority_race,
    taper_days,
    transition_days,
)
from .utils import calendar_weeks, parse_date


RUN_TYPES = {"Run", "TrailRun", "VirtualRun"}
RIDE_TYPES = {
    "Ride",
    "VirtualRide",
    "GravelRide",
    "MountainBikeRide",
    "TrackRide",
    "Cyclocross",
}
SWIM_TYPES = {"Swim", "OpenWaterSwim"}
RIDE_TYPES |= {"EBikeRide", "EMountainBikeRide", "Velomobile", "Handcycle"}
ENDURANCE_TYPES = RUN_TYPES | RIDE_TYPES
# Volume counts genuine endurance training only: no tennis, hikes or gym.
VOLUME_TYPES = ENDURANCE_TYPES | SWIM_TYPES
RACKET_TYPES = {
    "Tennis",
    "Padel",
    "Squash",
    "Badminton",
    "TableTennis",
    "Pickleball",
    "Racquetball",
    "RacquetBall",
}
TEAM_SPORT_TYPES = {"Soccer", "Basketball", "Hockey", "Volleyball", "Rugby", "Handball"}
WALK_TYPES = {"Hike", "Walk", "Snowshoe"}
STRENGTH_TYPES = {"WeightTraining", "Workout", "Crossfit", "Yoga", "Pilates"}
# Fallback load per hour when Intervals has no load and no HR for a session.
ESTIMATED_LOAD_PER_HOUR = {
    "racket": 45.0,
    "team": 50.0,
    "walk": 30.0,
    "strength": 20.0,
    "other": 30.0,
}
BIKE_RELEVANT_BUCKETS = {"sprint", "olympic", "70.3", "ironman"}

QUALITY_NAME_HINTS = (
    "threshold",
    "interval",
    "tempo",
    "vo2",
    "sweet spot",
    "race pace",
    "race-pace",
    "cruise",
    "over-under",
    "hill repeat",
    "fartlek",
    "test",
)
# Whole-word matching: "test" must not match "contest" / "fastest", "tempo"
# must not match "tempoh"; plurals ("intervals", "hill repeats") still match.
QUALITY_NAME_RE = re.compile(
    r"\b(?:threshold|intervals?|tempo|vo2(?:max)?|sweet[- ]?spot|race[- ]pace|"
    r"cruise|over[- ]unders?|hill repeats?|fartlek|tests?)\b"
)
# Intervals HR / pace zones are 7-zone (Friel). Three-zone model:
# low = Z1-Z2 (below LT1), moderate = Z3-Z4 (LT1-LT2), high = Z5+ (above LT2).
LOW_ZONE_COUNT = 2
MODERATE_ZONE_COUNT = 2

QUALITY_MIN_SECONDS_ABOVE_LT1 = 15 * 60
QUALITY_MIN_SECONDS_HIGH = 6 * 60
# TSB "positive" is relative to fitness: +5 means a lot more at CTL 30 than at
# CTL 85. Threshold = max(3, 10% of CTL): CTL 30 -> +3, CTL 50 -> +5, CTL 85 -> +8.5.
TSB_POSITIVE_THRESHOLD = 5.0
TSB_POSITIVE_FLOOR = 3.0
TSB_POSITIVE_CTL_PCT = 10.0
TSB_POSITIVE_DAYS_FLAG = 10
CTL_FLAT_RAMP_PER_WEEK = 1.0
# A CTL ramp of 3+/week over three weeks is real progression: never underload.
# Relative: max(2, 5% of CTL) -> CTL 30: 2.0, CTL 60: 3.0, CTL 85: 4.25.
UNDERLOAD_VETO_CTL_RAMP = 3.0
UNDERLOAD_VETO_FLOOR = 2.0
UNDERLOAD_VETO_CTL_PCT = 5.0
LOW_QUALITY_PER_WEEK = 2.0
# Base phase and trail/ultra builds live on long aerobic runs: one genuine
# quality session per week is already a normal dose there.
LOW_QUALITY_PER_WEEK_BASE_OR_ULTRA = 1.0
LONG_AEROBIC_BUCKETS = {"trail", "ultra_trail", "marathon", "ironman"}
VOLUME_SHORTFALL_RATIO = 0.7
VOLUME_REFERENCE_DAYS = 84
VOLUME_REFERENCE_MIN_COVERAGE_DAYS = 56
# The comparison window (last 28 days) is excluded from the reference norm, and
# so are break weeks (below this share of the median week, or overlapping an
# illness / holiday span).
VOLUME_BREAK_WEEK_RATIO = 0.3
VOLUME_REFERENCE_MIN_WEEKS = 3
# CR10 / Intervals RPE: threshold work sits around 6-7, so a "quality"
# session rated 4 or less felt genuinely easy.
EASY_QUALITY_RPE = 4.0
EFTP_FLAT_PCT = 2.0
HRV_BAND_SD_FACTOR = 0.5
# CV of lnRMSSD (SD / mean of the ln values, Plews 2012). Overnight wearable
# HRV (Garmin/Intervals) is smooth: 7-day lnRMSSD CVs of ~1-4% are typical
# (morning spot checks run higher). Above 6% the gate treats HRV as unstable
# (swinging) even when the mean sits inside its band.
HRV_MAX_LN_CV_PCT = 6.0
UNDERLOAD_MIN_MARKERS = 3
LOAD_MARKERS = {
    "tsb_persistently_positive",
    "ctl_flat_or_falling",
    "low_quality_frequency",
    "volume_below_profile",
}

# Ramp / spike safety thresholds. The absolute values are the fallback when
# CTL is unknown; otherwise the thresholds scale with CTL (percent of CTL per
# week, with a floor): elevated = max(4, 8% CTL), high = max(6, 12% CTL).
# CTL 30 -> 4 / 6 per week, CTL 60 -> 4.8 / 7.2, CTL 85 -> 6.8 / 10.2.
RAMP_ELEVATED_CTL_PER_WEEK = 5.0
RAMP_HIGH_CTL_PER_WEEK = 8.0
RAMP_ELEVATED_FLOOR = 4.0
RAMP_HIGH_FLOOR = 6.0
RAMP_ELEVATED_CTL_PCT = 8.0
RAMP_HIGH_CTL_PCT = 12.0
ACWR_ELEVATED = 1.3
ACWR_HIGH = 1.5
WEEK_LOAD_JUMP_PCT = 30.0
ACWR_MIN_CHRONIC_WEEKLY_LOAD = 50.0
# Chronic window = days 8-35 before the acute window end (uncoupled from acute).
ACWR_CHRONIC_FIRST_DAY = 7
ACWR_CHRONIC_LAST_DAY = 34
ACWR_MIN_CHRONIC_COVERAGE_DAYS = 14
# The prior 7 days count as a recovery dip below this share of the chronic week.
RECOVERY_DIP_RATIO = 0.8
# Coming back to the pre-dip weekly level (within this factor) after a planned
# lighter week is a normal return, not a spike (unless it follows a break).
RECOVERY_RETURN_TOLERANCE = 1.15

# Return from break (illness, holiday, 7+ days off).
BREAK_MIN_DAYS = 7
BREAK_LOOKBACK_DAYS = 35
# A day with fewer endurance minutes than this counts as "off".
OFF_DAY_MAX_MINUTES = 15.0
ILLNESS_BREAK_MIN_DAYS = 5
CTL_DROP_RETURN_PCT = 15.0
# Re-entry ramp: share of the pre-break weekly volume per week back.
REENTRY_VOLUME_PCTS = (50, 60, 75, 90)
RETURN_TO_RUN_LONG_RUN_MIN = 45.0

# Daily early warning (1-3 days).
DAILY_RHR_DELTA_BPM = 5.0
DAILY_HRV_Z = -1.5
DAILY_BASELINE_DAYS = 28
DAILY_WINDOW_DAYS = 3
# Wellness signals older than this (days before the planning date) are stale.
SIGNAL_STALE_DAYS = 2

# Intervals subjective wellness fields are 1-4 (fatigue / soreness / stress:
# 1 = low .. 4 = extreme; mood / motivation inverted, 1 = best; injury
# 1 = none .. 4 = injured). Values above 4 can only come from a 1-10 input.
INTERVALS_SUBJECTIVE_MAX = 4
# (intervals 1-4 high, 1-10 high) for the 3-day rolling mean.
SUBJECTIVE_HIGH = {"fatigue": (3.0, 7.0), "soreness": (3.0, 7.0)}
RHR_ELEVATED_7D_BPM = 3.0

# eFTP vs set FTP: above this the set FTP is stale and Intervals under-counts
# ride load (load ~ IF^2, IF = NP / set FTP).
FTP_STALE_PCT = 5.0
# Rides above this share of the 28-day load make the under-count material.
FTP_STALE_RIDE_LOAD_SHARE = 0.2

# Efficiency trend.
EFFICIENCY_CHANGE_PCT = 3.0
EFFICIENCY_MIN_MINUTES = 20.0
DECOUPLING_TREND_PCT = 1.5
FEEL_TREND_DELTA = 0.5
RPE_TREND_PCT = 10.0

# Run-specific (trail / ITB) thresholds.
RUN_KM_JUMP_PCT = 25.0
DESCENT_JUMP_PCT = 30.0
DESCENT_JUMP_MIN_M = 300.0
LONGEST_RUN_JUMP_RATIO = 1.25
# A longest-run jump only matters as a fresh stimulus (inside the last 10 days).
LONGEST_RUN_JUMP_RECENT_DAYS = 10
# A run-focused build counts as progression when run km rise this much.
RUN_BUILD_RISING_PCT = 10.0
# Planned-week run caps: ~+10% km / minutes, ~+15% descent, long run
# max(+15 min, +15%) over the longest non-race run of the last 28 days.
RUN_CAP_PCT = 10.0
DESCENT_CAP_PCT = 15.0
LONG_RUN_CAP_MIN = 15.0
LONG_RUN_CAP_PCT = 15.0
RUN_CAP_FLOOR_KM = 10.0
RUN_CAP_FLOOR_MIN = 60.0
DESCENT_CAP_FLOOR_M = 200.0
LONG_RUN_FALLBACK_56D_RATIO = 0.7
RUN_ACWR_MIN_CHRONIC_KM = 5.0
RUN_ACWR_MIN_CHRONIC_MIN = 30.0
RUN_ACWR_MIN_CHRONIC_DESCENT = 100.0
# Race-day activities (detected by Intervals race flags, race events on that
# date, or a race-like name) are excluded from longest-run references.
RACE_NAME_RE = re.compile(
    r"\b(?:race|wedstrijd|parkrun)\b(?![- ]?(?:pace|blocks?|sim\w*|specific|effort|"
    r"rehearsal|prep\w*|recon|week|day prep|intervals?))",
    re.IGNORECASE,
)
RACE_NAME_EXCLUDE_RE = re.compile(r"\b(?:pre|post|after|na|voor)[- ]?race\b", re.IGNORECASE)
HEAT_FEELS_LIKE_C = 25.0
DECOUPLING_FLAG_PCT = 5.0
LONG_SESSION_MIN = 60.0


def _num(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _activity_day(activity: dict[str, Any]) -> date | None:
    raw = activity.get("start_date_local")
    if not raw:
        return None
    try:
        return parse_date(str(raw)[:10])
    except ValueError:
        return None


def _minutes(activity: dict[str, Any]) -> float:
    return (_num(activity.get("moving_time")) or 0.0) / 60.0


def _in_window(activity: dict[str, Any], start: date, end: date) -> bool:
    day = _activity_day(activity)
    return day is not None and start <= day <= end


def _data_start(activities: list[dict[str, Any]]) -> date | None:
    """Earliest activity day: before it the history is unknown, not "off"."""
    days = [day for day in (_activity_day(item) for item in activities) if day]
    return min(days) if days else None


def acute_window_end(activities: list[dict[str, Any]], planning_date: date) -> date:
    """Last complete training day: yesterday, or today when today already holds training.

    The planning day usually has no training yet, so a 7-day window ending on
    it is really a 6-day window. A Sunday check-in after the long run keeps it.
    """
    today = planning_date.isoformat()
    if any(str(item.get("start_date_local") or "")[:10] == today for item in activities):
        return planning_date
    return planning_date - timedelta(days=1)


def _relative_threshold(ctl: float | None, pct: float, floor: float, fallback: float) -> float:
    if ctl is None or ctl <= 0:
        return fallback
    return round(max(floor, ctl * pct / 100.0), 2)


def ramp_thresholds(ctl: float | None) -> tuple[float, float]:
    """(elevated, high) CTL ramp per week, relative to CTL with absolute floors."""
    return (
        _relative_threshold(ctl, RAMP_ELEVATED_CTL_PCT, RAMP_ELEVATED_FLOOR, RAMP_ELEVATED_CTL_PER_WEEK),
        _relative_threshold(ctl, RAMP_HIGH_CTL_PCT, RAMP_HIGH_FLOOR, RAMP_HIGH_CTL_PER_WEEK),
    )


def tsb_positive_threshold(ctl: float | None) -> float:
    return _relative_threshold(ctl, TSB_POSITIVE_CTL_PCT, TSB_POSITIVE_FLOOR, TSB_POSITIVE_THRESHOLD)


def underload_veto_ramp(ctl: float | None) -> float:
    return _relative_threshold(ctl, UNDERLOAD_VETO_CTL_PCT, UNDERLOAD_VETO_FLOOR, UNDERLOAD_VETO_CTL_RAMP)


def is_race_activity(activity: dict[str, Any], race_dates: set[str] | None = None) -> bool:
    """Race-day activity: Intervals race flag, a race event on that date, or a race-like name.

    "Race pace", "race blocks", "race sim" and "pre-race" are training, not races.
    """
    if activity.get("race") is True or str(activity.get("sub_type") or "").upper() == "RACE":
        return True
    # Strava-style workout_type: 1 = run race, 11 = ride race.
    if activity.get("workout_type") in (1, 11, "1", "11", "race", "Race"):
        return True
    day = str(activity.get("start_date_local") or "")[:10]
    if race_dates and day in race_dates and activity.get("type") in VOLUME_TYPES:
        return True
    name = str(activity.get("name") or "")
    if RACE_NAME_EXCLUDE_RE.search(name):
        return False
    return bool(RACE_NAME_RE.search(name))


def elevation_loss(activity: dict[str, Any]) -> tuple[float, bool]:
    """(D- in metres, estimated). Missing D- falls back to D+ as a proxy, never 0."""
    loss = _num(activity.get("total_elevation_loss"))
    if loss is not None:
        return loss, False
    gain = _num(activity.get("total_elevation_gain"))
    if gain is None:
        gain = _num(activity.get("icu_elevation_gain"))
    if gain is None:
        return 0.0, False
    return gain, True


def subjective_scale(wellness: list[dict[str, Any]], field: str) -> str:
    """"intervals_1_4" unless any value of the series exceeds 4 (a 1-10 input)."""
    for item in wellness:
        value = _num(item.get(field))
        if value is not None and value > INTERVALS_SUBJECTIVE_MAX:
            return "1-10"
    return "intervals_1_4"


def subjective_rolling(
    wellness: list[dict[str, Any]], field: str, planning_date: date, days: int = DAILY_WINDOW_DAYS
) -> dict[str, Any]:
    """Rolling mean of a subjective wellness field over the last `days` calendar days."""
    start = planning_date - timedelta(days=days - 1)
    values = [
        value for day, value in _wellness_series(wellness, field) if start <= day <= planning_date
    ]
    scale = subjective_scale(wellness, field)
    result: dict[str, Any] = {
        "field": field,
        "window_days": days,
        "mean": round(mean(values), 2) if values else None,
        "entries": len(values),
        "scale": scale,
        "high": False,
    }
    thresholds = SUBJECTIVE_HIGH.get(field)
    if thresholds and values:
        limit = thresholds[1] if scale == "1-10" else thresholds[0]
        result["high"] = mean(values) >= limit
        result["high_threshold"] = limit
    return result


def load_category(activity_type: Any) -> str:
    kind = str(activity_type or "")
    if kind in RACKET_TYPES:
        return "racket"
    if kind in TEAM_SPORT_TYPES:
        return "team"
    if kind in WALK_TYPES:
        return "walk"
    if kind in STRENGTH_TYPES:
        return "strength"
    return "other"


def estimate_training_load(activity: dict[str, Any]) -> float | None:
    """Estimated load for sessions Intervals left without icu_training_load.

    HR-based (hrTSS-style: hours x (avg HR / LTHR)^2 x 100) when average HR and
    the activity LTHR are known, otherwise a per-hour rate by sport family
    (racket ~45/h, team sport ~50/h, hike/walk ~30/h, strength ~20/h).
    Returns None when Intervals already provides a load.
    """
    if _num(activity.get("icu_training_load")) is not None:
        return None
    minutes = _minutes(activity)
    if minutes <= 0:
        return None
    hours = minutes / 60.0
    avg_hr = _num(activity.get("average_heartrate"))
    lthr = _num(activity.get("lthr"))
    if avg_hr and lthr and lthr > 0:
        intensity = min(avg_hr / lthr, 1.1)
        return round(hours * intensity * intensity * 100.0, 1)
    rate = ESTIMATED_LOAD_PER_HOUR[load_category(activity.get("type"))]
    return round(hours * rate, 1)


def effective_training_load(activity: dict[str, Any]) -> float:
    """Intervals load when present, otherwise the estimated load (never None)."""
    actual = _num(activity.get("icu_training_load"))
    if actual is not None:
        return actual
    estimated = _num(activity.get("estimated_training_load"))
    if estimated is not None:
        return estimated
    return estimate_training_load(activity) or 0.0


def annotate_estimated_loads(activities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copies of the activities with `estimated_training_load` where Intervals has none."""
    annotated = []
    for activity in activities:
        estimate = estimate_training_load(activity)
        if estimate is None:
            annotated.append(activity)
        else:
            annotated.append({**activity, "estimated_training_load": estimate})
    return annotated


def _wellness_series(
    wellness: list[dict[str, Any]], field: str
) -> list[tuple[date, float]]:
    series = []
    for item in wellness:
        value = _num(item.get(field))
        if value is None or not item.get("id"):
            continue
        try:
            series.append((parse_date(str(item["id"])[:10]), value))
        except ValueError:
            continue
    return sorted(series)


def _value_on_or_before(series: list[tuple[date, float]], day: date) -> float | None:
    candidates = [value for entry_day, value in series if entry_day <= day]
    return candidates[-1] if candidates else None


def assess_fitness(wellness: list[dict[str, Any]], planning_date: date) -> dict[str, Any]:
    """CTL / ATL / TSB as of the planning date (entries after it are ignored)."""
    ctl_series = [item for item in _wellness_series(wellness, "ctl") if item[0] <= planning_date]
    atl_series = [item for item in _wellness_series(wellness, "atl") if item[0] <= planning_date]
    if not ctl_series:
        return {"status": "insufficient_data"}
    latest_day = ctl_series[-1][0]
    ctl = ctl_series[-1][1]
    atl = _value_on_or_before(atl_series, latest_day)
    ctl_7 = _value_on_or_before(ctl_series, latest_day - timedelta(days=7))
    ctl_21 = _value_on_or_before(ctl_series, latest_day - timedelta(days=21))
    ctl_42 = _value_on_or_before(ctl_series, latest_day - timedelta(days=42))
    ramp_per_week = (
        round((ctl - ctl_21) / 3.0, 2) if ctl_21 is not None else None
    )
    ramp_rate_series = [
        item for item in _wellness_series(wellness, "rampRate") if item[0] <= planning_date
    ]
    tsb_threshold = tsb_positive_threshold(ctl)

    atl_by_day = dict(atl_series)
    positive_days = 0
    above_5_days = 0
    observed_days = 0
    for day, ctl_value in ctl_series:
        if day < latest_day - timedelta(days=13) or day not in atl_by_day:
            continue
        observed_days += 1
        if ctl_value - atl_by_day[day] > tsb_threshold:
            positive_days += 1
        if ctl_value - atl_by_day[day] > TSB_POSITIVE_THRESHOLD:
            above_5_days += 1
    age_days = (planning_date - latest_day).days
    return {
        "status": "available",
        "as_of": latest_day.isoformat(),
        "age_days": age_days,
        "stale": age_days > SIGNAL_STALE_DAYS,
        "tsb_positive_threshold": tsb_threshold,
        "days_tsb_positive_last_14": positive_days,
        "ctl_drop_pct_42d": (
            round(100.0 * (ctl_42 - ctl) / ctl_42, 1) if ctl_42 else None
        ),
        "ctl": round(ctl, 1),
        "atl": round(atl, 1) if atl is not None else None,
        "tsb": round(ctl - atl, 1) if atl is not None else None,
        "ctl_7d_ago": round(ctl_7, 1) if ctl_7 is not None else None,
        "ctl_21d_ago": round(ctl_21, 1) if ctl_21 is not None else None,
        "ctl_42d_ago": round(ctl_42, 1) if ctl_42 is not None else None,
        "ctl_ramp_per_week": ramp_per_week,
        "ctl_change_last_7d": round(ctl - ctl_7, 1) if ctl_7 is not None else None,
        "intervals_ramp_rate": (
            round(ramp_rate_series[-1][1], 2) if ramp_rate_series else None
        ),
        # Legacy literal count (TSB > +5); the relative one drives underload.
        "days_tsb_above_5_last_14": above_5_days,
        "tsb_days_observed": observed_days,
    }


def _window_load(activities: list[dict[str, Any]], start: date, end: date) -> float:
    return sum(
        effective_training_load(activity)
        for activity in activities
        if _in_window(activity, start, end)
    )


def _window_sum(
    activities: list[dict[str, Any]],
    start: date,
    end: date,
    value: Callable[[dict[str, Any]], float],
) -> float:
    return sum(value(activity) for activity in activities if _in_window(activity, start, end))


def _chronic_weekly(
    activities: list[dict[str, Any]],
    acute_end: date,
    value: Callable[[dict[str, Any]], float],
) -> tuple[float | None, int]:
    """Weekly mean of days 8-35 before the acute end (uncoupled), coverage-aware.

    Days before the earliest activity are unknown history, not zero training,
    so the mean only covers the days the data actually reaches.
    """
    start = acute_end - timedelta(days=ACWR_CHRONIC_LAST_DAY)
    end = acute_end - timedelta(days=ACWR_CHRONIC_FIRST_DAY)
    data_start = _data_start(activities)
    if data_start is None:
        return None, 0
    covered_start = max(start, data_start)
    covered_days = (end - covered_start).days + 1
    if covered_days < ACWR_MIN_CHRONIC_COVERAGE_DAYS:
        return None, max(covered_days, 0)
    total = _window_sum(activities, covered_start, end, value)
    return total / covered_days * 7.0, covered_days


def _pre_dip_level(activities: list[dict[str, Any]], acute_end: date) -> float | None:
    """Mean weekly load of the two weeks before the prior week (days 15-28), covered only."""
    data_start = _data_start(activities)
    weeks = []
    for offset in (2, 3):
        end = acute_end - timedelta(days=7 * offset)
        start = end - timedelta(days=6)
        if data_start is None or start < data_start:
            continue
        weeks.append(_window_load(activities, start, end))
    return mean(weeks) if weeks else None


def assess_ramp(
    activities: list[dict[str, Any]],
    fitness: dict[str, Any],
    planning_date: date,
    *,
    return_from_break: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Overload / spike safety: CTL ramp, ACWR and week-on-week load change.

    Uncoupled ACWR = acute 7-day load (ending on the last complete day) /
    weekly mean of days 8-35 before it. CTL ramp thresholds scale with CTL.
    Intervals' wellness `rampRate` is a cross-check. The first weeks back after
    a break are flagged against the re-entry ramp, never called a normal return.
    """
    acute_end = acute_window_end(activities, planning_date)
    acute = _window_load(activities, acute_end - timedelta(days=6), acute_end)
    prior = _window_load(
        activities,
        acute_end - timedelta(days=13),
        acute_end - timedelta(days=7),
    )
    chronic_weekly, chronic_days = _chronic_weekly(
        activities, acute_end, effective_training_load
    )
    acwr = (
        round(acute / chronic_weekly, 2)
        if chronic_weekly is not None and chronic_weekly >= ACWR_MIN_CHRONIC_WEEKLY_LOAD
        else None
    )
    week_change = round(100.0 * (acute - prior) / prior, 1) if prior > 0 else None
    ctl_ramp = _num(fitness.get("ctl_ramp_per_week"))
    intervals_ramp = _num(fitness.get("intervals_ramp_rate"))
    ctl_value = _num(fitness.get("ctl"))
    elevated_ramp, high_ramp = ramp_thresholds(ctl_value)
    # Steady state: weekly load ~ 7 x CTL. A week-on-week jump that stays
    # below that is a rebound toward what the athlete is adapted to.
    adapted_weekly = 7.0 * ctl_value if ctl_value else None
    rfb = return_from_break or {}
    returning = rfb.get("status") == "returning"
    pre_dip = _pre_dip_level(activities, acute_end)
    recovery_return = bool(
        not returning
        and pre_dip is not None
        and pre_dip > 0
        and prior < RECOVERY_DIP_RATIO * pre_dip
        and acute <= RECOVERY_RETURN_TOLERANCE * pre_dip
    )

    level = 0
    findings: list[str] = []
    if ctl_ramp is not None and ctl_ramp > high_ramp:
        level = 2
        findings.append(
            f"CTL is ramping {ctl_ramp:+.1f}/week over 3 weeks (above +{high_ramp:g} for this CTL): too steep to sustain."
        )
    elif ctl_ramp is not None and ctl_ramp > elevated_ramp:
        level = max(level, 1)
        findings.append(
            f"CTL is ramping {ctl_ramp:+.1f}/week over 3 weeks (above +{elevated_ramp:g} for this CTL)."
        )
    if acwr is not None and acwr > ACWR_ELEVATED and recovery_return:
        findings.append(
            f"Acute:chronic load ratio is {acwr}, but the last 7 days ({round(acute)}) only return to the level before last week's lighter week ({round(pre_dip)}/week): a normal return to load."
        )
    elif acwr is not None and acwr > ACWR_HIGH:
        level = 2
        findings.append(
            f"Acute:chronic load ratio is {acwr} (7-day {round(acute)} vs {round(chronic_weekly)}/week chronic): a load spike."
        )
    elif acwr is not None and acwr > ACWR_ELEVATED:
        level = max(level, 1)
        findings.append(
            f"Acute:chronic load ratio is {acwr}: the last 7 days sit clearly above the 4-week norm."
        )
    if week_change is not None and week_change > WEEK_LOAD_JUMP_PCT:
        if recovery_return:
            findings.append(
                f"Load is up {week_change:.0f}% on the prior 7 days, but those were a planned lighter week and the load is back at the earlier level: a normal return to load."
            )
        elif not returning and adapted_weekly is not None and acute < adapted_weekly:
            findings.append(
                f"Load is up {week_change:.0f}% on the prior 7 days ({round(prior)} -> {round(acute)}), "
                f"but still below the ~{round(adapted_weekly)}/week that CTL {ctl_value:g} is adapted to: noted, not a spike."
            )
        elif not returning:
            level = max(level, 1)
            findings.append(
                f"Load is up {week_change:.0f}% on the prior 7 days ({round(prior)} -> {round(acute)})."
            )
    if returning:
        pre_load = _num(rfb.get("pre_break_weekly_load"))
        week = int(rfb.get("reentry_week") or 1)
        # The 7 days just done belong to the re-entry week before the planned one.
        allowed_pct = REENTRY_VOLUME_PCTS[max(week - 2, 0)]
        acute_pct = round(100.0 * acute / pre_load) if pre_load else None
        detail = (
            f"{acute_pct}% of the pre-break week (the re-entry ramp allows ~{allowed_pct}%)"
            if acute_pct is not None
            else "unknown against the pre-break week"
        )
        findings.append(
            f"Returning from a break ({rfb.get('days_off')} days off): the last 7 days are {detail}. "
            "Detrained tissue makes this a spike risk even at the old weekly level: follow the re-entry ramp."
        )
        if acute_pct is not None and acute_pct >= 100:
            level = 2
        elif acute_pct is not None and acute_pct > allowed_pct + 10:
            level = max(level, 1)
        elif week_change is None and prior <= 0 and acute > 0 and pre_load is None:
            level = max(level, 1)
    elif week_change is None and prior <= 0 and acute > 0 and chronic_weekly is None:
        findings.append(
            "Load restarted after an empty week with no usable chronic baseline: treat this week as a re-entry."
        )
        level = max(level, 1)
    if intervals_ramp is not None and intervals_ramp > high_ramp and level < 1:
        level = 1
        findings.append(
            f"Intervals reports a weekly ramp rate of {intervals_ramp:+.1f} (cross-check)."
        )
    status = ("ok", "elevated", "high")[level]
    if not findings:
        findings.append("Load progression is inside safe ramp limits.")
    return {
        "status": status,
        "ctl_ramp_per_week": ctl_ramp,
        "intervals_ramp_rate": intervals_ramp,
        "week_load_change_pct": week_change,
        "acwr": acwr,
        "acwr_status": "available" if acwr is not None else "insufficient_data",
        "acwr_method": "uncoupled: acute 7 days ending on the last complete day / weekly mean of days 8-35",
        "acute_window_end": acute_end.isoformat(),
        "chronic_coverage_days": chronic_days,
        "acute_7d_load": round(acute, 1),
        "prior_7d_load": round(prior, 1),
        "chronic_weekly_avg_load": (
            round(chronic_weekly, 1) if chronic_weekly is not None else None
        ),
        "return_from_break": returning,
        "recovery_return": recovery_return,
        "ramp_thresholds": {"elevated": elevated_ramp, "high": high_ramp},
        "findings": findings,
        "rules": {
            "elevated": f"CTL ramp > max({RAMP_ELEVATED_FLOOR:g}, {RAMP_ELEVATED_CTL_PCT:g}% CTL)/week, ACWR > {ACWR_ELEVATED} or 7-day load +{WEEK_LOAD_JUMP_PCT:.0f}%",
            "high": f"CTL ramp > max({RAMP_HIGH_FLOOR:g}, {RAMP_HIGH_CTL_PCT:g}% CTL)/week or ACWR > {ACWR_HIGH}",
            "return_from_break": "last 7 days above the re-entry ramp share of the pre-break week",
        },
    }


def _zone_values(raw: Any) -> list[float]:
    """Zone seconds from Intervals: a list of numbers, or [{"id": "Z1", "secs": n}].

    Power zone lists also carry a sweet-spot ("SS") bucket that overlaps Z3/Z4;
    it is dropped so the Z1..Zn times add up to the ride.
    """
    values: list[float] = []
    for item in raw or []:
        if isinstance(item, dict):
            zone_id = str(item.get("id") or "").upper()
            if zone_id and not re.fullmatch(r"Z\d+", zone_id):
                continue
            values.append(float(_num(item.get("secs")) or 0.0))
        else:
            values.append(float(_num(item) or 0.0))
    return values


def _zone_times(activity: dict[str, Any]) -> tuple[list[float], str]:
    """Zone times used to judge intensity (quality detection and distribution).

    Runs prefer pace-based zones (GAP when Intervals uses it, else pace) so a
    hilly easy run whose HR drifts on the climbs is not read as quality. Rides
    prefer power zones (Intervals `icu_zone_times`). HR zones are the fallback.
    """
    kind = activity.get("type")
    if kind in RUN_TYPES:
        gap = _zone_values(activity.get("gap_zone_times"))
        pace = _zone_values(activity.get("pace_zone_times"))
        if any(gap) and activity.get("use_gap_zone_times"):
            return gap, "gap"
        if any(pace):
            return pace, "pace"
        if any(gap):
            return gap, "gap"
    if kind in RIDE_TYPES:
        power = _zone_values(activity.get("icu_zone_times") or activity.get("power_zone_times"))
        if any(power):
            return power, "power"
    hr = _zone_values(activity.get("icu_hr_zone_times"))
    if any(hr):
        return hr, "hr"
    return [], "none"


def zone_seconds(activity: dict[str, Any]) -> dict[str, Any]:
    zones, source = _zone_times(activity)
    return {
        "source": source,
        "above_lt1_s": sum(zones[LOW_ZONE_COUNT:]) if zones else None,
        "high_s": sum(zones[LOW_ZONE_COUNT + MODERATE_ZONE_COUNT :]) if zones else None,
    }


def is_quality_by_zones(activity: dict[str, Any]) -> bool | None:
    seconds = zone_seconds(activity)
    if seconds["source"] == "none":
        return None
    return (
        seconds["above_lt1_s"] >= QUALITY_MIN_SECONDS_ABOVE_LT1
        or seconds["high_s"] >= QUALITY_MIN_SECONDS_HIGH
    )


def quality_name_hint(name: Any) -> bool:
    """Whole-word quality hint in an activity / session name."""
    return bool(QUALITY_NAME_RE.search(str(name or "").lower()))


def _is_quality(activity: dict[str, Any]) -> bool:
    if is_quality_by_zones(activity):
        return True
    return quality_name_hint(activity.get("name"))


def assess_intensity_distribution(
    activities: list[dict[str, Any]],
    planning_date: date,
    days: int = 28,
    *,
    ftp_stale: bool = False,
) -> dict[str, Any]:
    """Three-zone distribution from the same zone source as quality detection.

    Runs: GAP / pace zones, rides: power zones, HR zones as the fallback.
    """
    start = planning_date - timedelta(days=days - 1)
    low = moderate = high = 0.0
    sources: dict[str, int] = {}
    for activity in activities:
        if not _in_window(activity, start, planning_date):
            continue
        if activity.get("type") not in ENDURANCE_TYPES:
            continue
        values, source = _zone_times(activity)
        if not values:
            continue
        sources[source] = sources.get(source, 0) + 1
        low += sum(values[:LOW_ZONE_COUNT])
        moderate += sum(values[LOW_ZONE_COUNT : LOW_ZONE_COUNT + MODERATE_ZONE_COUNT])
        high += sum(values[LOW_ZONE_COUNT + MODERATE_ZONE_COUNT :])
    total = low + moderate + high
    if total <= 0:
        return {"status": "insufficient_data", "window_days": days}
    low_pct = round(100 * low / total, 1)
    moderate_pct = round(100 * moderate / total, 1)
    high_pct = round(100 * high / total, 1)
    if moderate_pct + high_pct < 8:
        model = "almost_all_easy"
    elif moderate_pct > high_pct and low_pct > moderate_pct:
        model = "pyramidal"
    elif high_pct >= moderate_pct and low_pct > high_pct:
        model = "polarized"
    else:
        model = "threshold_heavy"
    result = {
        "status": "available",
        "window_days": days,
        "zone_model": (
            "3-zone from Intervals zones (runs: GAP/pace, rides: power, fallback HR): "
            "low=Z1-Z2, moderate=Z3-Z4, high=Z5+"
        ),
        "zone_sources": sources,
        "low_pct": low_pct,
        "moderate_pct": moderate_pct,
        "high_pct": high_pct,
        "low_min": int(low / 60),
        "moderate_min": int(moderate / 60),
        "high_min": int(high / 60),
        "model": model,
        "pyramidal_target": "about 75-80% low, 12-20% moderate, 3-8% high",
    }
    if ftp_stale and sources.get("power"):
        result["caveat"] = (
            "The set FTP is above the current eFTP, so power zones under-read ride intensity: "
            "real ride time above LT1 is higher than shown."
        )
    return result


def _planned_week_start(planning_date: date) -> date:
    return planning_date + timedelta(days=(7 - planning_date.weekday()) % 7)


def _completed_weeks(
    activities: list[dict[str, Any]],
    planning_date: date,
    week_start: date,
    weeks: int,
) -> list[dict[str, Any]]:
    windows = calendar_weeks(
        week_start, activities, weeks=weeks + 1, planning_date=planning_date
    )
    completed = [window for window in windows if not window["partial"]]
    return completed[-weeks:]


def assess_quality_frequency(
    activities: list[dict[str, Any]],
    planning_date: date,
    weeks: int = 3,
    *,
    week_start: date | None = None,
) -> dict[str, Any]:
    """Quality sessions per completed Mon-Sun week before the planned week."""
    planned_start = week_start or _planned_week_start(planning_date)
    counts = []
    sessions = []
    for window in _completed_weeks(activities, planning_date, planned_start, weeks):
        count = 0
        for activity in window["activities"]:
            if activity.get("type") not in ENDURANCE_TYPES or not _is_quality(activity):
                continue
            count += 1
            sessions.append(
                {
                    "date": str(activity.get("start_date_local"))[:10],
                    "type": activity.get("type"),
                    "name": activity.get("name"),
                    "rpe": _num(activity.get("icu_rpe")),
                    "session_rpe": _num(activity.get("session_rpe")),
                    "feel": _num(activity.get("feel")),
                    "decoupling": _num(activity.get("decoupling")),
                    "zone_source": zone_seconds(activity)["source"],
                }
            )
        counts.append({"week_start": window["week_start"], "quality_sessions": count})
    average = (
        round(sum(item["quality_sessions"] for item in counts) / len(counts), 2)
        if counts
        else 0.0
    )
    rated = [item["rpe"] for item in sessions if item["rpe"] is not None]
    return {
        "weeks": counts,
        "average_per_week": average,
        "sessions": sessions,
        "average_quality_rpe": round(mean(rated), 1) if rated else None,
    }


def _span_overlaps(start: date, end: date, spans: list[tuple[date, date]]) -> bool:
    return any(span_start <= end and span_end >= start for span_start, span_end in spans)


def assess_volume(
    activities: list[dict[str, Any]],
    planning_date: date,
    athlete_profile: dict[str, Any],
    days: int = 28,
    *,
    excluded_spans: list[tuple[date, date]] | None = None,
) -> dict[str, Any]:
    """Endurance hours per week vs the athlete's own norm (profile as fallback).

    The norm is the mean of the 7-day weeks in days 29-84 before the planning
    date: the 28-day comparison window itself is excluded (not self-referential)
    and so are break weeks (below 30% of the median week, or overlapping an
    illness / holiday span), so a long summer holiday does not drag the norm down.
    """

    def hours(start: date, end: date) -> float:
        seconds = sum(
            _num(activity.get("moving_time")) or 0.0
            for activity in activities
            if activity.get("type") in VOLUME_TYPES and _in_window(activity, start, end)
        )
        return seconds / 3600

    recent = round(hours(planning_date - timedelta(days=days - 1), planning_date) / (days / 7), 2)
    data_start = _data_start(activities)
    spans = excluded_spans or []
    weeks: list[dict[str, Any]] = []
    for index in range((VOLUME_REFERENCE_DAYS - days) // 7):
        end = planning_date - timedelta(days=days + 7 * index)
        start = end - timedelta(days=6)
        if data_start is None or start < data_start:
            continue
        weeks.append({"week_start": start.isoformat(), "hours": round(hours(start, end), 2), "start": start, "end": end})
    typical_week = median([week["hours"] for week in weeks]) if weeks else 0.0
    used = []
    excluded = []
    for week in weeks:
        if _span_overlaps(week["start"], week["end"], spans):
            excluded.append({"week_start": week["week_start"], "hours": week["hours"], "reason": "illness_or_holiday"})
        elif typical_week > 0 and week["hours"] < VOLUME_BREAK_WEEK_RATIO * typical_week:
            excluded.append({"week_start": week["week_start"], "hours": week["hours"], "reason": "break_week"})
        else:
            used.append(week["hours"])
    reference = (
        round(mean(used), 2)
        if len(used) >= VOLUME_REFERENCE_MIN_WEEKS and mean(used) > 0
        else None
    )
    typical = _num(athlete_profile.get("typical_training_hours_per_week"))
    if reference:
        reference_hours, reference_source = reference, "recent_12_week_average"
    elif typical:
        reference_hours, reference_source = typical, "athlete_profile"
    else:
        reference_hours, reference_source = None, "missing"
    return {
        "window_days": days,
        "hours_per_week": recent,
        "types_counted": "endurance only (ride, run, swim)",
        "recent_12_week_hours_per_week": reference,
        "reference_window": "weeks 5-12 before the planning date, break / illness / holiday weeks excluded",
        "reference_weeks_used": len(used),
        "reference_weeks_excluded": excluded,
        "profile_typical_hours": typical,
        "ratio_to_profile": round(recent / typical, 2) if typical else None,
        "reference_hours": reference_hours,
        "reference_source": reference_source,
        "ratio_to_reference": (
            round(recent / reference_hours, 2) if reference_hours else None
        ),
    }


def _hrv_series(wellness: list[dict[str, Any]]) -> list[tuple[date, float]]:
    return [(day, value) for day, value in _wellness_series(wellness, "hrv") if value > 0]


def assess_hrv_gate(
    wellness: list[dict[str, Any]],
    *,
    baseline_days: int = 42,
    acute_days: int = 7,
    planning_date: date | None = None,
) -> dict[str, Any]:
    """Weekly HRV gate for adding intensity, computed on lnRMSSD.

    Normal band = baseline mean +/- 0.5 SD of lnRMSSD (smallest worthwhile
    change, Plews 2012). Intensity may rise while the 7-day ln mean is inside or
    above the band and the 7-day CV of lnRMSSD stays moderate (Javaloyes 2019).
    Raw-ms means are reported alongside for readability. With a planning date,
    HRV older than 2 days before it is `stale`: the gate is unknown, not open.
    """
    series = _hrv_series(wellness)
    if planning_date is not None:
        series = [item for item in series if item[0] <= planning_date]
    if not series:
        return {"status": "insufficient_data", "extra_intensity_allowed": None}
    latest_day = series[-1][0]
    if planning_date is not None and (planning_date - latest_day).days > SIGNAL_STALE_DAYS:
        return {
            "status": "stale",
            "extra_intensity_allowed": None,
            "latest_hrv_date": latest_day.isoformat(),
            "age_days": (planning_date - latest_day).days,
            "rationale": f"The newest HRV is from {latest_day.isoformat()}, {(planning_date - latest_day).days} days before planning: the gate cannot open on stale data.",
        }
    acute_start = latest_day - timedelta(days=acute_days - 1)
    baseline_start = acute_start - timedelta(days=baseline_days)
    acute = [value for day, value in series if day >= acute_start]
    baseline = [value for day, value in series if baseline_start <= day < acute_start]
    if len(baseline) < 14 or len(acute) < 3:
        return {
            "status": "insufficient_data",
            "extra_intensity_allowed": None,
            "baseline_entries": len(baseline),
            "acute_entries": len(acute),
            "latest_hrv_date": latest_day.isoformat(),
        }
    ln_baseline = [log(value) for value in baseline]
    ln_acute = [log(value) for value in acute]
    ln_baseline_mean = mean(ln_baseline)
    ln_band = HRV_BAND_SD_FACTOR * pstdev(ln_baseline)
    ln_acute_mean = mean(ln_acute)
    cv_pct = round(100 * pstdev(ln_acute) / ln_acute_mean, 1) if ln_acute_mean else None
    if ln_acute_mean < ln_baseline_mean - ln_band:
        status = "suppressed"
    elif ln_acute_mean > ln_baseline_mean + ln_band:
        status = "above_normal"
    else:
        status = "normal"
    unstable = cv_pct is not None and cv_pct > HRV_MAX_LN_CV_PCT
    allowed = status in {"normal", "above_normal"} and not unstable
    if status == "suppressed":
        rationale = "7-day lnRMSSD sits below the normal band: hold intensity, do not add quality."
    elif unstable:
        rationale = "HRV is inside the band but swinging day to day (high lnRMSSD CV): keep the planned quality, do not add more."
    elif status == "above_normal":
        rationale = "7-day lnRMSSD sits above the normal band: read together with load (coping well, or a parasympathetic overreaching pattern under heavy load)."
    else:
        rationale = "7-day lnRMSSD is inside the normal band: planned and additional quality are supported."
    return {
        "status": status,
        "method": "lnRMSSD",
        "extra_intensity_allowed": allowed,
        "unstable": unstable,
        "latest_hrv_date": latest_day.isoformat(),
        "baseline_mean": round(mean(baseline), 1),
        "normal_band": [
            round(exp(ln_baseline_mean - ln_band), 1),
            round(exp(ln_baseline_mean + ln_band), 1),
        ],
        "acute_mean": round(mean(acute), 1),
        "ln_baseline_mean": round(ln_baseline_mean, 3),
        "ln_normal_band": [
            round(ln_baseline_mean - ln_band, 3),
            round(ln_baseline_mean + ln_band, 3),
        ],
        "ln_acute_mean": round(ln_acute_mean, 3),
        "acute_cv_pct": cv_pct,
        "max_cv_pct": HRV_MAX_LN_CV_PCT,
        "rationale": rationale,
    }


def assess_eftp_trend(
    activities: list[dict[str, Any]], planning_date: date, days: int = 42
) -> dict[str, Any]:
    start = planning_date - timedelta(days=days)
    points = []
    for activity in activities:
        day = _activity_day(activity)
        value = _num(activity.get("icu_rolling_ftp"))
        if day is None or value is None or value <= 0 or day < start or day > planning_date:
            continue
        if activity.get("type") not in RIDE_TYPES:
            continue
        points.append((day, value))
    if len(points) < 2:
        return {"status": "insufficient_data"}
    points.sort()
    first, last = points[0][1], points[-1][1]
    delta_pct = round(100 * (last - first) / first, 1)
    return {
        "status": "available",
        "window_days": days,
        "first": first,
        "last": last,
        "delta_pct": delta_pct,
        "trend": "flat" if abs(delta_pct) < EFTP_FLAT_PCT else ("up" if delta_pct > 0 else "down"),
    }


def assess_ftp_staleness(
    activities: list[dict[str, Any]],
    planning_date: date,
    sport_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Set bike FTP vs the latest Intervals eFTP and what it does to ride load.

    Intervals load ~ hours x IF^2 x 100 with IF = NP / set FTP, so a set FTP
    above the real one under-counts ride load by about (eFTP / FTP)^2. The
    correction factor (FTP / eFTP)^2 is reported for information; the underload
    assessment does not rescale loads, it stops counting CTL/TSB markers instead.
    """
    rides = sorted(
        (
            (day, activity)
            for activity in activities
            if activity.get("type") in RIDE_TYPES
            and (day := _activity_day(activity)) is not None
            and day <= planning_date
        ),
        key=lambda item: item[0],
    )
    eftp = None
    for _, activity in reversed(rides):
        eftp = _num(activity.get("icu_rolling_ftp"))
        if eftp:
            break
    ride_settings = (sport_settings or {}).get("Ride") or {}
    ftp = _num(ride_settings.get("ftp"))
    if not ftp:
        for _, activity in reversed(rides):
            ftp = _num(activity.get("icu_ftp"))
            if ftp:
                break
    start = planning_date - timedelta(days=27)
    total = _window_load(activities, start, planning_date)
    ride_load = sum(
        effective_training_load(activity)
        for _, activity in rides
        if _in_window(activity, start, planning_date)
    )
    share = round(ride_load / total, 2) if total > 0 else 0.0
    if not ftp or not eftp:
        return {
            "status": "insufficient_data",
            "set_ftp": ftp,
            "eftp": eftp,
            "stale": False,
            "ride_load_share_28d": share,
            "load_undercounted": False,
        }
    gap_pct = round(100.0 * (ftp - eftp) / eftp, 1)
    stale = gap_pct > FTP_STALE_PCT
    return {
        "status": "available",
        "set_ftp": ftp,
        "eftp": eftp,
        "ftp_above_eftp_pct": gap_pct,
        "stale": stale,
        "ride_load_share_28d": share,
        "ride_load_correction_factor": round((ftp / eftp) ** 2, 2) if stale else 1.0,
        "load_undercounted": stale and share >= FTP_STALE_RIDE_LOAD_SHARE,
    }


def _pct_change(latest: float, reference: float) -> float | None:
    if reference <= 0:
        return None
    return round(100.0 * (latest - reference) / reference, 1)


def _longest(
    activities: list[dict[str, Any]],
    types: set[str],
    start: date,
    end: date,
    *,
    race_dates: set[str] | None = None,
    races: bool | None = False,
) -> float:
    """Longest session in the window. races=False excludes races, True keeps only races, None keeps all."""
    durations = []
    for activity in activities:
        if activity.get("type") not in types or not _in_window(activity, start, end):
            continue
        if races is not None and is_race_activity(activity, race_dates) != races:
            continue
        durations.append(_minutes(activity))
    return round(max(durations), 1) if durations else 0.0


def _run_km(activity: dict[str, Any]) -> float:
    if activity.get("type") not in RUN_TYPES:
        return 0.0
    return (_num(activity.get("distance")) or 0.0) / 1000.0


def _run_minutes(activity: dict[str, Any]) -> float:
    return _minutes(activity) if activity.get("type") in RUN_TYPES else 0.0


def _run_descent(activity: dict[str, Any]) -> float:
    return elevation_loss(activity)[0] if activity.get("type") in RUN_TYPES else 0.0


def assess_run_acwr(activities: list[dict[str, Any]], planning_date: date) -> dict[str, Any]:
    """Run-specific uncoupled ACWR on km, minutes and descent (D-)."""
    acute_end = acute_window_end(activities, planning_date)
    acute_start = acute_end - timedelta(days=6)
    result: dict[str, Any] = {"acute_window_end": acute_end.isoformat()}
    minimums = {
        "km": RUN_ACWR_MIN_CHRONIC_KM,
        "minutes": RUN_ACWR_MIN_CHRONIC_MIN,
        "descent": RUN_ACWR_MIN_CHRONIC_DESCENT,
    }
    worst = 0
    any_ratio = False
    for key, value in (("km", _run_km), ("minutes", _run_minutes), ("descent", _run_descent)):
        acute = _window_sum(activities, acute_start, acute_end, value)
        chronic, _ = _chronic_weekly(activities, acute_end, value)
        result[f"acute_{key}"] = round(acute, 1)
        result[f"chronic_weekly_{key}"] = round(chronic, 1) if chronic is not None else None
        if chronic is None or chronic < minimums[key]:
            result[key] = None
            continue
        ratio = round(acute / chronic, 2)
        result[key] = ratio
        any_ratio = True
        if ratio > ACWR_HIGH:
            worst = max(worst, 2)
        elif ratio > ACWR_ELEVATED:
            worst = max(worst, 1)
    result["status"] = ("ok", "elevated", "high")[worst] if any_ratio else "insufficient_data"
    result["method"] = "uncoupled: acute 7 days ending on the last complete day / weekly mean of days 8-35"
    return result


def _run_week_caps(
    *,
    last: dict[str, Any] | None,
    recent4: list[dict[str, Any]],
    longest_28: float,
    longest_56: float,
    return_from_break: dict[str, Any] | None,
) -> dict[str, Any]:
    """Suggested run caps for the planned week (never a template number)."""

    def avg(key: str) -> float:
        return mean(row[key] for row in recent4) if recent4 else 0.0

    base_km = max(last["run_km"] if last else 0.0, avg("run_km"))
    base_min = max(last["run_minutes"] if last else 0.0, avg("run_minutes"))
    base_descent = max(last["elevation_loss_m"] if last else 0.0, avg("elevation_loss_m"))
    basis: list[str] = []
    if base_km > 0 or base_min > 0:
        max_km: float | None = round(max(base_km * (1 + RUN_CAP_PCT / 100), RUN_CAP_FLOOR_KM), 1)
        max_min: float | None = round(max(base_min * (1 + RUN_CAP_PCT / 100), RUN_CAP_FLOOR_MIN))
        max_descent: float | None = round(
            max(base_descent * (1 + DESCENT_CAP_PCT / 100), DESCENT_CAP_FLOOR_M)
        )
        basis.append(
            f"+{RUN_CAP_PCT:g}% km/min and +{DESCENT_CAP_PCT:g}% D- over the larger of last week and the 4-week average"
        )
    else:
        max_km = max_min = max_descent = None
        basis.append("no recent run history: weekly run caps unknown")
    if longest_28 > 0:
        max_long = round(max(longest_28 + LONG_RUN_CAP_MIN, longest_28 * (1 + LONG_RUN_CAP_PCT / 100)))
        basis.append(
            f"long run max(+{LONG_RUN_CAP_MIN:g} min, +{LONG_RUN_CAP_PCT:g}%) over the longest non-race run of 28 days ({longest_28:g} min)"
        )
    elif longest_56 > 0:
        max_long = round(longest_56 * LONG_RUN_FALLBACK_56D_RATIO)
        basis.append(
            f"no long run in 28 days: {LONG_RUN_FALLBACK_56D_RATIO:g} x the longest of 56 days ({longest_56:g} min)"
        )
    else:
        max_long = round(RETURN_TO_RUN_LONG_RUN_MIN)
        basis.append(f"no run in 56 days: conservative return-to-run long run {RETURN_TO_RUN_LONG_RUN_MIN:g} min")
    rfb = return_from_break or {}
    if rfb.get("status") == "returning":
        pct = (rfb.get("volume_cap_pct") or REENTRY_VOLUME_PCTS[0]) / 100.0
        if rfb.get("run_km_cap") is not None:
            max_km = rfb["run_km_cap"]
        if rfb.get("pre_break_run_min"):
            max_min = round(rfb["pre_break_run_min"] * pct)
        if rfb.get("pre_break_descent_m") is not None:
            max_descent = round(rfb["pre_break_descent_m"] * pct)
        if rfb.get("long_run_cap_min"):
            max_long = min(max_long, round(rfb["long_run_cap_min"]))
        basis.append(
            f"tightened by the return-from-break ramp: week {rfb.get('reentry_week')} at {rfb.get('volume_cap_pct')}% of the pre-break week"
        )
    return {
        "max_run_km": max_km,
        "max_run_min": max_min,
        "max_descent_m": max_descent,
        "max_long_run_min": max_long,
        "basis": "; ".join(basis),
    }


def assess_run_load(
    activities: list[dict[str, Any]],
    planning_date: date,
    *,
    week_start: date | None = None,
    weeks: int = 8,
    race_dates: set[str] | None = None,
    return_from_break: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run km / time / D+ / D- per calendar week, longest-run trends, ACWR and caps.

    Longest-run references exclude race-day activities (a race is not a
    training long run to progress from). Missing D- falls back to D+.
    """
    planned_start = week_start or _planned_week_start(planning_date)
    week_rows = []
    for window in calendar_weeks(
        planned_start, activities, weeks=weeks, planning_date=planning_date
    ):
        runs = [item for item in window["activities"] if item.get("type") in RUN_TYPES]
        training_runs = [item for item in runs if not is_race_activity(item, race_dates)]
        longest = max(training_runs, key=_minutes) if training_runs else None
        losses = [elevation_loss(item) for item in runs]
        week_rows.append(
            {
                "week_start": window["week_start"],
                "partial": window["partial"],
                "runs": len(runs),
                "run_km": round(sum((_num(item.get("distance")) or 0.0) for item in runs) / 1000.0, 1),
                "run_minutes": round(sum(_minutes(item) for item in runs)),
                "elevation_gain_m": round(sum((_num(item.get("total_elevation_gain")) or 0.0) for item in runs)),
                "elevation_loss_m": round(sum(loss for loss, _ in losses)),
                "elevation_loss_estimated": any(estimated for _, estimated in losses),
                "races": len(runs) - len(training_runs),
                "longest_run_min": round(_minutes(longest), 1) if longest else 0.0,
                "longest_run_km": (
                    round((_num(longest.get("distance")) or 0.0) / 1000.0, 1) if longest else 0.0
                ),
            }
        )
    completed = [row for row in week_rows if not row["partial"]]
    last = completed[-1] if completed else None
    prior = completed[-5:-1]
    recent4 = completed[-4:]

    def avg(rows: list[dict[str, Any]], key: str) -> float:
        return round(mean(row[key] for row in rows), 1) if rows else 0.0

    run_km_change = (
        _pct_change(last["run_km"], avg(prior, "run_km")) if last and prior else None
    )
    loss_change = (
        _pct_change(last["elevation_loss_m"], avg(prior, "elevation_loss_m"))
        if last and prior
        else None
    )

    def longest(start_offset: int, end_offset: int, races: bool | None = False) -> float:
        return _longest(
            activities,
            RUN_TYPES,
            planning_date - timedelta(days=start_offset),
            planning_date - timedelta(days=end_offset),
            race_dates=race_dates,
            races=races,
        )

    longest_28 = longest(27, 0)
    longest_56 = longest(55, 0)
    longest_prior_28 = longest(55, 28)
    findings: list[str] = []
    flags: list[str] = []
    if run_km_change is not None and run_km_change > RUN_KM_JUMP_PCT and last["run_km"] >= 10:
        flags.append("run_km_jump")
        findings.append(
            f"Run volume jumped {run_km_change:.0f}% last week ({last['run_km']} km vs {avg(prior, 'run_km')} km/week before)."
        )
    if (
        last
        and prior
        and last["elevation_loss_m"] >= DESCENT_JUMP_MIN_M
        and (loss_change is None or loss_change > DESCENT_JUMP_PCT)
    ):
        flags.append("descent_jump")
        findings.append(
            f"Downhill running jumped to {last['elevation_loss_m']} m D- last week (vs {avg(prior, 'elevation_loss_m')} m/week): eccentric quad and ITB load."
        )
    longest_recent = longest(LONGEST_RUN_JUMP_RECENT_DAYS - 1, 0)
    if (
        longest_prior_28 > 0
        and longest_28 > LONGEST_RUN_JUMP_RATIO * longest_prior_28
        and longest_recent >= longest_28
    ):
        flags.append("long_run_jump")
        findings.append(
            f"Longest run in 28 days ({longest_28:.0f} min) is well above the prior month's longest ({longest_prior_28:.0f} min)."
        )
    acwr = assess_run_acwr(activities, planning_date)
    if acwr["status"] in {"elevated", "high"}:
        flags.append(f"run_acwr_{acwr['status']}")
        findings.append(
            f"Run acute:chronic is {acwr['status']} (km {acwr.get('km')}, minutes {acwr.get('minutes')}, descent {acwr.get('descent')})."
        )
    if any(row["elevation_loss_estimated"] for row in week_rows):
        findings.append("Some runs have no recorded D-; their D+ is used as an estimated proxy.")
    return {
        "weeks": week_rows,
        "longest_run_min_28d": longest_28,
        "longest_run_min_56d": longest_56,
        "longest_race_min_28d": longest(27, 0, races=True),
        "longest_run_excludes": "race-day activities",
        "avg_weekly_run_km_4w": avg(recent4, "run_km"),
        "avg_weekly_run_min_4w": avg(recent4, "run_minutes"),
        "avg_weekly_elevation_gain_m_4w": avg(recent4, "elevation_gain_m"),
        "avg_weekly_elevation_loss_m_4w": avg(recent4, "elevation_loss_m"),
        "elevation_loss_estimated": any(row["elevation_loss_estimated"] for row in week_rows),
        "run_km_change_pct": run_km_change,
        "elevation_loss_change_pct": loss_change,
        "comparison": "last completed week vs the average of the 4 completed weeks before it",
        "acwr": acwr,
        "week_caps": _run_week_caps(
            last=last,
            recent4=recent4,
            longest_28=longest_28,
            longest_56=longest_56,
            return_from_break=return_from_break,
        ),
        "flags": flags,
        "findings": findings,
    }


def assess_ride_load(activities: list[dict[str, Any]], planning_date: date) -> dict[str, Any]:
    return {
        "longest_ride_min_28d": _longest(
            activities, RIDE_TYPES, planning_date - timedelta(days=27), planning_date
        ),
        "longest_ride_min_56d": _longest(
            activities, RIDE_TYPES, planning_date - timedelta(days=55), planning_date
        ),
    }


def assess_other_load(activities: list[dict[str, Any]], planning_date: date) -> dict[str, Any]:
    """Tennis, hikes, strength and other non-endurance sessions of the last 7 days."""
    start = planning_date - timedelta(days=6)
    sessions = []
    for activity in activities:
        if activity.get("type") in VOLUME_TYPES or not _in_window(activity, start, planning_date):
            continue
        actual = _num(activity.get("icu_training_load"))
        sessions.append(
            {
                "type": activity.get("type"),
                "date": str(activity.get("start_date_local"))[:10],
                "name": activity.get("name"),
                "minutes": round(_minutes(activity)),
                "estimated_load": round(effective_training_load(activity), 1),
                "load_source": "intervals" if actual is not None else "estimated",
            }
        )
    return {
        "sessions_7d": sessions,
        "racket_sessions_7d": sum(1 for item in sessions if item["type"] in RACKET_TYPES),
        "estimated_load_7d": round(sum(item["estimated_load"] for item in sessions), 1),
    }


def _holiday_overlaps(
    holidays: list[dict[str, Any]], planning_date: date, week_start: date
) -> list[str]:
    recent_start = planning_date - timedelta(days=13)
    week_end = week_start + timedelta(days=6)
    names = []
    for holiday in holidays:
        try:
            start = parse_date(str(holiday.get("start_date")))
            end = parse_date(str(holiday.get("end_date") or holiday.get("start_date")))
        except (TypeError, ValueError):
            continue
        recent = start <= planning_date and end >= recent_start
        upcoming = bool(holiday.get("in_planning_week")) or (
            start <= week_end and end >= week_start
        )
        if recent or upcoming:
            names.append(f"{holiday.get('name') or 'Holiday'} ({start.isoformat()} to {end.isoformat()})")
    return names


def is_bike_relevant(
    priority_race: dict[str, Any] | None, athlete_profile: dict[str, Any] | None = None
) -> bool:
    """Bike relevance comes only from the race on the Intervals calendar.

    The profile's `target_race_distance` is deliberately ignored: local goals go
    stale, the Intervals calendar is the single source of races and goals.
    """
    if priority_race:
        return race_bucket(priority_race) in BIKE_RELEVANT_BUCKETS
    return False


def _parse_day(value: Any) -> date | None:
    try:
        return parse_date(str(value)[:10]) if value else None
    except ValueError:
        return None


def _endurance_minutes_by_day(activities: list[dict[str, Any]]) -> dict[date, float]:
    minutes: dict[date, float] = {}
    for activity in activities:
        day = _activity_day(activity)
        if day is None or activity.get("type") not in VOLUME_TYPES:
            continue
        minutes[day] = minutes.get(day, 0.0) + _minutes(activity)
    return minutes


def _off_day_spans(
    activities: list[dict[str, Any]], start: date, end: date
) -> list[tuple[date, date]]:
    """Runs of consecutive days with ~zero endurance training inside the known history."""
    data_start = _data_start(activities)
    if data_start is None:
        return []
    by_day = _endurance_minutes_by_day(activities)
    spans: list[tuple[date, date]] = []
    run_start: date | None = None
    day = max(start, data_start)
    while day <= end:
        if by_day.get(day, 0.0) < OFF_DAY_MAX_MINUTES:
            run_start = run_start or day
        elif run_start is not None:
            spans.append((run_start, day - timedelta(days=1)))
            run_start = None
        day += timedelta(days=1)
    if run_start is not None:
        spans.append((run_start, end))
    return spans


def _window_profile(
    activities: list[dict[str, Any]],
    start: date,
    end: date,
    race_dates: set[str] | None = None,
) -> dict[str, Any] | None:
    """Weekly endurance minutes / load and run km / minutes / D- over a covered window."""
    data_start = _data_start(activities)
    if data_start is None:
        return None
    covered_start = max(start, data_start)
    covered_days = (end - covered_start).days + 1
    if covered_days < 7:
        return None
    per_week = 7.0 / covered_days
    endurance = [
        item
        for item in activities
        if item.get("type") in VOLUME_TYPES and _in_window(item, covered_start, end)
    ]
    return {
        "weekly_min": round(sum(_minutes(item) for item in endurance) * per_week, 1),
        "weekly_load": round(_window_load(activities, covered_start, end) * per_week, 1),
        "run_km": round(_window_sum(activities, covered_start, end, _run_km) * per_week, 1),
        "run_min": round(_window_sum(activities, covered_start, end, _run_minutes) * per_week, 1),
        "descent_m": round(_window_sum(activities, covered_start, end, _run_descent) * per_week),
        "long_run_min": _longest(activities, RUN_TYPES, covered_start, end, race_dates=race_dates),
        "covered_days": covered_days,
    }


def _merge_spans(spans: list[tuple[date, date, str]]) -> list[tuple[date, date, list[str]]]:
    merged: list[tuple[date, date, list[str]]] = []
    for start, end, source in sorted(spans):
        if merged and start <= merged[-1][1] + timedelta(days=2):
            last_start, last_end, sources = merged[-1]
            merged[-1] = (last_start, max(last_end, end), sorted(set(sources + [source])))
        else:
            merged.append((start, end, [source]))
    return merged


def break_spans(
    activities: list[dict[str, Any]],
    planning_date: date,
    *,
    health_events: list[dict[str, Any]] | None = None,
    holidays: list[dict[str, Any]] | None = None,
    lookback_days: int = 84,
) -> list[tuple[date, date, list[str]]]:
    """Break periods: 7+ days without endurance training, illness, or a low-load holiday."""
    start = planning_date - timedelta(days=lookback_days)
    end = acute_window_end(activities, planning_date)
    spans: list[tuple[date, date, str]] = [
        (span_start, span_end, "activity_gap")
        for span_start, span_end in _off_day_spans(activities, start, end)
        if (span_end - span_start).days + 1 >= BREAK_MIN_DAYS
    ]
    for event in health_events or []:
        event_start = _parse_day(event.get("start_date"))
        event_end = _parse_day(event.get("end_date")) or event_start
        if event_start is None or event_end < start:
            continue
        duration = int(event.get("duration_days") or (event_end - event_start).days + 1)
        kind = event.get("kind")
        long_enough = (
            (kind == "sick" and (duration >= ILLNESS_BREAK_MIN_DAYS or event.get("severity") == "extended"))
            or (kind == "injured" and duration >= BREAK_MIN_DAYS)
        )
        if long_enough:
            spans.append((event_start, min(event_end, planning_date), "illness" if kind == "sick" else "injury"))
    by_day = _endurance_minutes_by_day(activities)
    for holiday in holidays or []:
        holiday_start = _parse_day(holiday.get("start_date"))
        holiday_end = _parse_day(holiday.get("end_date")) or holiday_start
        if holiday_start is None or holiday_start > planning_date or holiday_end < start:
            continue
        holiday_end = min(holiday_end, planning_date)
        days = (holiday_end - holiday_start).days + 1
        if days < BREAK_MIN_DAYS:
            continue
        before = _window_profile(
            activities, holiday_start - timedelta(days=28), holiday_start - timedelta(days=1)
        )
        holiday_minutes = sum(
            minutes for day, minutes in by_day.items() if holiday_start <= day <= holiday_end
        )
        usual = (before or {}).get("weekly_min") or 0.0
        if usual <= 0 or holiday_minutes / days * 7 < 0.5 * usual:
            spans.append((holiday_start, holiday_end, "holiday"))
    return _merge_spans(spans)


def assess_return_from_break(
    activities: list[dict[str, Any]],
    planning_date: date,
    *,
    fitness: dict[str, Any] | None = None,
    health_events: list[dict[str, Any]] | None = None,
    holidays: list[dict[str, Any]] | None = None,
    week_start: date | None = None,
    race_dates: set[str] | None = None,
) -> dict[str, Any]:
    """Return from a break (illness, holiday, 7+ days off) with a graded re-entry ramp.

    "returning" when a break ended within the last ~35 days (or is ongoing) and
    the planned week is still inside the 4-week re-entry ramp, or when CTL fell
    15%+ against 42 days ago. Caps are 50/60/75/90% of the pre-break week
    (endurance minutes, run km, longest run), never above what was achieved
    plus one ramp step.
    """
    planned_start = week_start or _planned_week_start(planning_date)
    acute_end = acute_window_end(activities, planning_date)
    ctl_drop = _num((fitness or {}).get("ctl_drop_pct_42d"))
    base: dict[str, Any] = {
        "status": "none",
        "days_off": 0,
        "ctl_drop_pct": ctl_drop,
        "pre_break_weekly_min": None,
        "pre_break_run_km": None,
        "reentry_week": None,
        "volume_cap_pct": None,
        "run_km_cap": None,
        "long_run_cap_min": None,
        "ramp": list(REENTRY_VOLUME_PCTS),
    }
    lookback_start = planning_date - timedelta(days=BREAK_LOOKBACK_DAYS)
    candidates = [
        span
        for span in break_spans(
            activities, planning_date, health_events=health_events, holidays=holidays
        )
        if span[1] >= lookback_start
    ]
    source: list[str]
    if candidates:
        break_start, break_end, source = candidates[-1]
        days_off = (break_end - break_start).days + 1
        ongoing = break_end >= acute_end
        pre = _window_profile(
            activities,
            break_start - timedelta(days=28),
            break_start - timedelta(days=1),
            race_dates,
        )
        calendar_week = 1 if ongoing else ((planned_start - break_end).days - 1) // 7 + 1
        if calendar_week > len(REENTRY_VOLUME_PCTS):
            return {
                **base,
                "days_off": days_off,
                "break_start": break_start.isoformat(),
                "break_end": break_end.isoformat(),
                "source": source,
                "rationale": f"The {days_off}-day break ending {break_end.isoformat()} is behind the 4-week re-entry ramp.",
            }
    elif ctl_drop is not None and ctl_drop >= CTL_DROP_RETURN_PCT:
        break_start = break_end = None
        days_off = 0
        ongoing = False
        source = ["ctl_drop"]
        pre = _window_profile(
            activities,
            planning_date - timedelta(days=69),
            planning_date - timedelta(days=42),
            race_dates,
        )
        calendar_week = 1 if ctl_drop >= 30 else 2 if ctl_drop >= 22 else 3
    else:
        return {**base, "rationale": "No break of 7+ days, illness or CTL drop of 15%+ in the last 5 weeks."}

    pre_min = (pre or {}).get("weekly_min") or 0.0
    recent_min = sum(
        minutes
        for day, minutes in _endurance_minutes_by_day(activities).items()
        if acute_end - timedelta(days=6) <= day <= acute_end
    )
    achieved_pct = round(100.0 * recent_min / pre_min) if pre_min > 0 else None
    achieved_step = (
        sum(1 for pct in REENTRY_VOLUME_PCTS if achieved_pct >= pct - 5)
        if achieved_pct is not None
        else 0
    )
    reentry = max(1, min(calendar_week, achieved_step + 1, len(REENTRY_VOLUME_PCTS)))
    pct = REENTRY_VOLUME_PCTS[reentry - 1]
    share = pct / 100.0
    long_run = (pre or {}).get("long_run_min") or 0.0
    run_km = (pre or {}).get("run_km")
    result = {
        **base,
        "status": "returning",
        "days_off": days_off,
        "break_start": break_start.isoformat() if break_start else None,
        "break_end": break_end.isoformat() if break_end else None,
        "ongoing": ongoing,
        "source": source,
        "pre_break_weekly_min": pre_min or None,
        "pre_break_weekly_load": (pre or {}).get("weekly_load") or None,
        "pre_break_run_km": run_km,
        "pre_break_run_min": (pre or {}).get("run_min"),
        "pre_break_descent_m": (pre or {}).get("descent_m"),
        "pre_break_long_run_min": long_run or None,
        "achieved_pct_last_7d": achieved_pct,
        "reentry_week": reentry,
        "volume_cap_pct": pct,
        "weekly_min_cap": round(pre_min * share) if pre_min else None,
        "run_km_cap": round(run_km * share, 1) if run_km else None,
        "long_run_cap_min": round(max(30.0, long_run * share)) if long_run else None,
    }
    what = {
        "activity_gap": "days without endurance training",
        "illness": "illness",
        "injury": "injury",
        "holiday": "holiday",
        "ctl_drop": "a CTL drop",
    }
    reason = " + ".join(what.get(item, item) for item in source)
    if source == ["ctl_drop"]:
        lead = f"CTL is {ctl_drop:.0f}% below 42 days ago"
    elif ongoing:
        lead = f"The break ({reason}, {days_off} days so far) is still ongoing"
    else:
        lead = f"Back from a {days_off}-day break ({reason}, {break_start.isoformat()} to {break_end.isoformat()})"
    result["rationale"] = (
        f"{lead}: re-entry week {reentry} of {len(REENTRY_VOLUME_PCTS)} at {pct}% of the pre-break week"
        + (f" ({round(pre_min)} min/week before; last 7 days reached {achieved_pct}%)" if pre_min else "")
        + ". Detrained tendons and the ITB adapt slower than the engine: progress volume before intensity."
    )
    return result


def _rhr_series(wellness: list[dict[str, Any]], planning_date: date) -> list[tuple[date, float]]:
    return [
        item
        for item in _wellness_series(wellness, "restingHR")
        if item[1] > 0 and item[0] <= planning_date
    ]


def resting_hr_delta_7d(wellness: list[dict[str, Any]], planning_date: date) -> float | None:
    """7-day mean resting HR minus its 42-day baseline before that week."""
    series = _rhr_series(wellness, planning_date)
    acute_start = planning_date - timedelta(days=6)
    acute = [value for day, value in series if day >= acute_start]
    baseline = [
        value
        for day, value in series
        if acute_start - timedelta(days=42) <= day < acute_start
    ]
    if len(acute) < 3 or len(baseline) < 14:
        return None
    return round(mean(acute) - mean(baseline), 1)


def assess_daily_signals(wellness: list[dict[str, Any]], planning_date: date) -> dict[str, Any]:
    """1-3 day early warning from resting HR and HRV.

    Warning: RHR >= +5 bpm over its 28-day baseline, or lnRMSSD z <= -1.5, on 2
    of the last 3 days (or one extreme reading on the latest day). Alert: both
    RHR and HRV flagged on 2 of the last 3 days.
    """
    rhr = _rhr_series(wellness, planning_date)
    hrv = [item for item in _hrv_series(wellness) if item[0] <= planning_date]
    latest_days = [series[-1][0] for series in (rhr, hrv) if series]
    if not latest_days:
        return {
            "status": "insufficient_data",
            "resting_hr_delta_bpm": None,
            "hrv_z": None,
            "days_flagged": 0,
            "latest_signal_date": None,
            "findings": ["No resting HR or HRV in the wellness data."],
        }
    latest = max(latest_days)
    if (planning_date - latest).days > SIGNAL_STALE_DAYS:
        return {
            "status": "stale",
            "resting_hr_delta_bpm": None,
            "hrv_z": None,
            "days_flagged": 0,
            "latest_signal_date": latest.isoformat(),
            "findings": [
                f"Newest resting HR / HRV is from {latest.isoformat()}: no early-warning read for this week."
            ],
        }
    window_start = planning_date - timedelta(days=DAILY_WINDOW_DAYS - 1)
    baseline_start = window_start - timedelta(days=DAILY_BASELINE_DAYS)
    rhr_base_values = [value for day, value in rhr if baseline_start <= day < window_start]
    hrv_base_values = [log(value) for day, value in hrv if baseline_start <= day < window_start]
    rhr_base = mean(rhr_base_values) if len(rhr_base_values) >= 7 else None
    hrv_mean = mean(hrv_base_values) if len(hrv_base_values) >= 10 else None
    hrv_sd = pstdev(hrv_base_values) if hrv_mean is not None else None
    if rhr_base is None and (hrv_mean is None or not hrv_sd):
        return {
            "status": "insufficient_data",
            "resting_hr_delta_bpm": None,
            "hrv_z": None,
            "days_flagged": 0,
            "latest_signal_date": latest.isoformat(),
            "findings": ["Not enough resting HR / HRV history for a 28-day baseline."],
        }
    rhr_by_day = {day: value for day, value in rhr if day >= window_start}
    hrv_by_day = {day: value for day, value in hrv if day >= window_start}
    rhr_days = hrv_days = flagged = 0
    latest_rhr_delta = latest_z = None
    for offset in range(DAILY_WINDOW_DAYS):
        day = window_start + timedelta(days=offset)
        day_flag = False
        if rhr_base is not None and day in rhr_by_day:
            delta = round(rhr_by_day[day] - rhr_base, 1)
            latest_rhr_delta = delta
            if delta >= DAILY_RHR_DELTA_BPM:
                rhr_days += 1
                day_flag = True
        if hrv_mean is not None and hrv_sd and day in hrv_by_day:
            z = round((log(hrv_by_day[day]) - hrv_mean) / hrv_sd, 2)
            latest_z = z
            if z <= DAILY_HRV_Z:
                hrv_days += 1
                day_flag = True
        flagged += int(day_flag)
    findings: list[str] = []
    extreme = (latest_rhr_delta is not None and latest_rhr_delta >= 2 * DAILY_RHR_DELTA_BPM) or (
        latest_z is not None and latest_z <= -2.5
    )
    if rhr_days >= 2 and hrv_days >= 2:
        status = "alert"
        findings.append(
            f"Resting HR raised and HRV low on {min(rhr_days, hrv_days)}+ of the last {DAILY_WINDOW_DAYS} days: likely illness or acute overload; easy or off until it clears."
        )
    elif rhr_days >= 2 or hrv_days >= 2 or extreme:
        status = "warning"
        if rhr_days >= 2:
            findings.append(f"Resting HR is {DAILY_RHR_DELTA_BPM:g}+ bpm above baseline on {rhr_days} of the last {DAILY_WINDOW_DAYS} days.")
        if hrv_days >= 2:
            findings.append(f"HRV is well below baseline (z <= {DAILY_HRV_Z}) on {hrv_days} of the last {DAILY_WINDOW_DAYS} days.")
        if extreme and not findings:
            findings.append("The latest morning reading is an extreme outlier (resting HR or HRV): check for illness before any quality.")
    else:
        status = "ok"
        findings.append("No 1-3 day resting HR / HRV warning.")
    return {
        "status": status,
        "resting_hr_delta_bpm": latest_rhr_delta,
        "hrv_z": latest_z,
        "days_flagged": flagged,
        "rhr_days_flagged": rhr_days,
        "hrv_days_flagged": hrv_days,
        "latest_signal_date": latest.isoformat(),
        "resting_hr_baseline": round(rhr_base, 1) if rhr_base is not None else None,
        "findings": findings,
    }


def _efficiency_point(activity: dict[str, Any]) -> tuple[str, float] | None:
    """(sport/source key, efficiency factor) for a steady endurance session."""
    if _minutes(activity) < EFFICIENCY_MIN_MINUTES:
        return None
    hr = _num(activity.get("average_heartrate"))
    kind = activity.get("type")
    if hr and hr > 0:
        if kind in RIDE_TYPES:
            watts = (
                _num(activity.get("icu_weighted_avg_watts"))
                or _num(activity.get("icu_average_watts"))
                or _num(activity.get("average_watts"))
            )
            if watts:
                return "ride_power_per_hr", watts / hr
        if kind in RUN_TYPES:
            distance = _num(activity.get("distance"))
            seconds = _num(activity.get("moving_time"))
            if distance and seconds:
                return "run_speed_per_hr", (distance / seconds * 60.0) / hr
    ef = _num(activity.get("icu_efficiency_factor"))
    if ef and kind in ENDURANCE_TYPES:
        return f"{'ride' if kind in RIDE_TYPES else 'run'}_intervals_ef", ef
    return None


def _trend_direction(recent: float | None, prior: float | None, threshold: float, *, higher_is: str) -> str | None:
    if recent is None or prior is None:
        return None
    delta = recent - prior
    if abs(delta) < threshold:
        return "stable"
    worse = delta > 0 if higher_is == "worse" else delta < 0
    return "worsening" if worse else "improving"


def assess_efficiency_trend(
    activities: list[dict[str, Any]], planning_date: date, *, days: int = 42, recent_days: int = 14
) -> dict[str, Any]:
    """EF (power or speed per heartbeat), decoupling, RPE vs intensity and Feel over 6 weeks.

    Recent = last 14 days, prior = days 15-42. Feel uses the Intervals scale:
    1 = strong (best) .. 5 = weak (worst), so a rising Feel number is worse.
    """
    start = planning_date - timedelta(days=days - 1)
    recent_start = planning_date - timedelta(days=recent_days - 1)
    window = [
        item
        for item in activities
        if item.get("type") in ENDURANCE_TYPES and _in_window(item, start, planning_date)
    ]

    def split(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        recent = [item for item in items if _activity_day(item) >= recent_start]
        prior = [item for item in items if _activity_day(item) < recent_start]
        return recent, prior

    groups: dict[str, dict[str, list[float]]] = {}
    steady = [item for item in window if not _is_quality(item)]
    for pool in (steady, window):
        groups = {}
        for item in pool:
            point = _efficiency_point(item)
            if point is None:
                continue
            bucket = groups.setdefault(point[0], {"recent": [], "prior": []})
            bucket["recent" if _activity_day(item) >= recent_start else "prior"].append(point[1])
        if any(len(g["recent"]) >= 2 and len(g["prior"]) >= 2 for g in groups.values()):
            break
    changes = {
        key: round(100.0 * (mean(values["recent"]) - mean(values["prior"])) / mean(values["prior"]), 1)
        for key, values in groups.items()
        if len(values["recent"]) >= 2 and len(values["prior"]) >= 2 and mean(values["prior"]) > 0
    }
    ef_change = round(mean(changes.values()), 1) if changes else None

    long_recent_start = planning_date - timedelta(days=20)
    decoupling_recent = [
        _num(item.get("decoupling"))
        for item in window
        if _minutes(item) >= LONG_SESSION_MIN and _activity_day(item) >= long_recent_start
        and _num(item.get("decoupling")) is not None
    ]
    decoupling_prior = [
        _num(item.get("decoupling"))
        for item in window
        if _minutes(item) >= LONG_SESSION_MIN and _activity_day(item) < long_recent_start
        and _num(item.get("decoupling")) is not None
    ]
    dec_recent = round(mean(decoupling_recent), 1) if decoupling_recent else None
    dec_prior = round(mean(decoupling_prior), 1) if decoupling_prior else None

    recent, prior = split(window)

    def rpe_per_intensity(items: list[dict[str, Any]]) -> float | None:
        values = []
        for item in items:
            rpe = _num(item.get("icu_rpe"))
            intensity = _num(item.get("icu_intensity"))
            if rpe and intensity and intensity > 0:
                values.append(rpe / (intensity / 10.0))
        return round(mean(values), 2) if len(values) >= 2 else None

    rpe_recent, rpe_prior = rpe_per_intensity(recent), rpe_per_intensity(prior)
    rpe_direction = None
    if rpe_recent is not None and rpe_prior:
        change = 100.0 * (rpe_recent - rpe_prior) / rpe_prior
        rpe_direction = "rising" if change >= RPE_TREND_PCT else "falling" if change <= -RPE_TREND_PCT else "stable"

    def feel(items: list[dict[str, Any]]) -> float | None:
        values = [_num(item.get("feel")) for item in items if _num(item.get("feel"))]
        return round(mean(values), 2) if len(values) >= 2 else None

    feel_recent, feel_prior = feel(recent), feel(prior)
    feel_direction = _trend_direction(feel_recent, feel_prior, FEEL_TREND_DELTA, higher_is="worse")
    decoupling_direction = _trend_direction(dec_recent, dec_prior, DECOUPLING_TREND_PCT, higher_is="worse")

    if ef_change is None:
        status = "insufficient_data"
    elif ef_change >= EFFICIENCY_CHANGE_PCT:
        status = "improving"
    elif ef_change <= -EFFICIENCY_CHANGE_PCT or (
        ef_change <= -EFFICIENCY_CHANGE_PCT / 2
        and "worsening" in (feel_direction, decoupling_direction)
    ):
        status = "declining"
    else:
        status = "stable"
    return {
        "status": status,
        "window_days": days,
        "comparison": f"last {recent_days} days vs days {recent_days + 1}-{days}",
        "ef_change_pct": ef_change,
        "ef_change_by_source": changes,
        "decoupling_trend": {
            "recent_21d_pct": dec_recent,
            "prior_pct": dec_prior,
            "direction": decoupling_direction,
        },
        "rpe_vs_load": {
            "recent_rpe_per_if": rpe_recent,
            "prior_rpe_per_if": rpe_prior,
            "direction": rpe_direction,
            "meaning": "RPE per unit of intensity factor; rising = the same intensity feels harder",
        },
        "feel_trend": {
            "recent": feel_recent,
            "prior": feel_prior,
            "direction": feel_direction,
            "scale": "Intervals Feel: 1 = strong (best) .. 5 = weak (worst); a rising number is worse",
        },
    }


def _weekly_physio_offsets(wellness: list[dict[str, Any]], planning_date: date) -> dict[str, int]:
    """How many of the last two 7-day blocks have HRV below its band / RHR +3 bpm."""
    hrv = [item for item in _hrv_series(wellness) if item[0] <= planning_date]
    rhr = _rhr_series(wellness, planning_date)
    baseline_end = planning_date - timedelta(days=14)
    baseline_start = baseline_end - timedelta(days=42)
    ln_base = [log(value) for day, value in hrv if baseline_start <= day < baseline_end]
    rhr_base = [value for day, value in rhr if baseline_start <= day < baseline_end]
    hrv_weeks = rhr_weeks = 0
    for block in range(2):
        end = planning_date - timedelta(days=7 * block)
        start = end - timedelta(days=6)
        ln_week = [log(value) for day, value in hrv if start <= day <= end]
        rhr_week = [value for day, value in rhr if start <= day <= end]
        if len(ln_base) >= 14 and len(ln_week) >= 3:
            band_low = mean(ln_base) - HRV_BAND_SD_FACTOR * pstdev(ln_base)
            if mean(ln_week) < band_low:
                hrv_weeks += 1
        if len(rhr_base) >= 14 and len(rhr_week) >= 3:
            if mean(rhr_week) - mean(rhr_base) >= RHR_ELEVATED_7D_BPM:
                rhr_weeks += 1
    return {"hrv_suppressed_weeks": hrv_weeks, "rhr_elevated_weeks": rhr_weeks}


def assess_overreaching(
    *,
    wellness: list[dict[str, Any]],
    planning_date: date,
    fitness: dict[str, Any],
    ramp: dict[str, Any],
    eftp: dict[str, Any],
    efficiency: dict[str, Any],
    hrv_gate: dict[str, Any],
    daily_signals: dict[str, Any],
    rhr_delta_7d: float | None,
) -> dict[str, Any]:
    """Functional vs non-functional overreaching.

    Performance markers down (eFTP falling, efficiency declining, Feel worse,
    RPE rising for the same intensity) plus HRV / RHR off. Short-term (< ~2
    weeks) under heavy load = functional; persisting for 2+ weeks with
    performance down = non-functional overreaching risk.
    """
    performance: list[str] = []
    if eftp.get("status") == "available" and eftp.get("trend") == "down":
        performance.append(f"eftp_down ({eftp.get('delta_pct'):+.1f}% over {eftp.get('window_days')} days)")
    if efficiency.get("status") == "declining":
        performance.append(f"efficiency_declining (EF {efficiency.get('ef_change_pct'):+.1f}%)")
    if (efficiency.get("feel_trend") or {}).get("direction") == "worsening":
        performance.append("feel_worsening")
    if (efficiency.get("rpe_vs_load") or {}).get("direction") == "rising":
        performance.append("rpe_rising_for_same_intensity")
    physio: list[str] = []
    if hrv_gate.get("status") == "suppressed":
        physio.append("hrv_suppressed_7d")
    if daily_signals.get("status") in {"warning", "alert"}:
        physio.append(f"daily_signals_{daily_signals['status']}")
    if rhr_delta_7d is not None and rhr_delta_7d >= RHR_ELEVATED_7D_BPM:
        physio.append(f"resting_hr_up_{rhr_delta_7d:g}bpm_7d")
    persistence = _weekly_physio_offsets(wellness, planning_date)
    persistent = (
        persistence["hrv_suppressed_weeks"] >= 2 or persistence["rhr_elevated_weeks"] >= 2
    )
    if persistent:
        physio.append("hrv_or_rhr_off_2_weeks")
    ctl = _num(fitness.get("ctl"))
    tsb = _num(fitness.get("tsb"))
    heavy_load = ramp.get("status") in {"elevated", "high"} or (
        tsb is not None and tsb < -max(10.0, 0.2 * (ctl or 0.0))
    )
    markers = performance + physio + (["heavy_recent_load"] if heavy_load else [])
    if performance and persistent:
        status = "non_functional_risk"
        rationale = "Performance markers are down and HRV / resting HR have been off for 2+ weeks: non-functional overreaching risk. Unload for 1-2 weeks and rule out illness before rebuilding."
    elif (performance and physio) or (physio and heavy_load) or (performance and heavy_load):
        status = "functional"
        rationale = "Short-term overreaching under recent load: absorb with an easier block of days; it should clear within ~1-2 weeks."
    else:
        status = "none"
        rationale = "No combined performance + physiology overreaching pattern."
    return {
        "status": status,
        "markers": markers,
        "performance_markers": performance,
        "physiology_markers": physio,
        "persistence": persistence,
        "heavy_load": heavy_load,
        "rationale": rationale,
    }


def assess_underload(
    *,
    fitness: dict[str, Any],
    quality: dict[str, Any],
    volume: dict[str, Any],
    hrv_gate: dict[str, Any],
    eftp: dict[str, Any],
    health_events: list[dict[str, Any]],
    active_phase: str | None,
    requested_week_type: str | None,
    ramp: dict[str, Any] | None = None,
    bike_relevant: bool = False,
    priority_race: dict[str, Any] | None = None,
    last_race: dict[str, Any] | None = None,
    holiday_names: list[str] | None = None,
    run_load: dict[str, Any] | None = None,
    daily_signals: dict[str, Any] | None = None,
    subjective: dict[str, dict[str, Any]] | None = None,
    resting_hr_delta: float | None = None,
    ftp_staleness: dict[str, Any] | None = None,
    return_from_break: dict[str, Any] | None = None,
) -> dict[str, Any]:
    markers: list[dict[str, str]] = []
    context: list[dict[str, str]] = []
    ftp_undercounted = bool((ftp_staleness or {}).get("load_undercounted"))
    # CTL / TSB are sums of Intervals load; with a stale (too high) FTP the
    # ride part is under-counted, so these markers are not trusted.
    load_derived = {"tsb_persistently_positive", "ctl_flat_or_falling"}

    def add(marker: str, evidence: str) -> None:
        if ftp_undercounted and marker in load_derived:
            context.append(
                {
                    "marker": marker,
                    "evidence": evidence
                    + " Not counted: the set FTP is stale (above eFTP), so Intervals under-counts ride load.",
                }
            )
            return
        markers.append({"marker": marker, "evidence": evidence})

    ctl = _num(fitness.get("ctl"))
    positive_days = fitness.get("days_tsb_positive_last_14", fitness.get("days_tsb_above_5_last_14", 0))
    if fitness.get("status") == "available" and (positive_days or 0) >= TSB_POSITIVE_DAYS_FLAG:
        threshold = fitness.get("tsb_positive_threshold", TSB_POSITIVE_THRESHOLD)
        add(
            "tsb_persistently_positive",
            f"TSB was above +{threshold:g} on {positive_days} of the last 14 days.",
        )
    ctl_ramp = fitness.get("ctl_ramp_per_week")
    if ctl_ramp is not None and ctl_ramp < CTL_FLAT_RAMP_PER_WEEK:
        add(
            "ctl_flat_or_falling",
            f"CTL ramp is {ctl_ramp:+.1f}/week over the last 3 weeks (CTL {fitness.get('ctl_21d_ago')} -> {fitness.get('ctl')}).",
        )
    phase = str(active_phase or "").lower()
    long_aerobic = race_bucket(priority_race) in LONG_AEROBIC_BUCKETS if priority_race else False
    quality_floor = (
        LOW_QUALITY_PER_WEEK_BASE_OR_ULTRA
        if phase == "base" or long_aerobic
        else LOW_QUALITY_PER_WEEK
    )
    if quality.get("average_per_week", 0) < quality_floor:
        add(
            "low_quality_frequency",
            f"Only {quality.get('average_per_week')} quality sessions per week over the last 3 completed weeks (floor {quality_floor:g}/week for this phase and race).",
        )
    ratio = volume.get("ratio_to_reference", volume.get("ratio_to_profile"))
    if ratio is not None and ratio < VOLUME_SHORTFALL_RATIO:
        source = (
            "own 12-week average"
            if volume.get("reference_source") == "recent_12_week_average"
            else "profile's typical"
        )
        reference = volume.get("reference_hours", volume.get("profile_typical_hours"))
        add(
            "volume_below_profile",
            f"{volume.get('hours_per_week')}h/week of endurance training over 28 days is {int(ratio * 100)}% of the {source} {reference}h.",
        )
    if hrv_gate.get("status") in {"normal", "above_normal"}:
        # Informational only: normal HRV is not evidence of underload.
        context.append(
            {
                "marker": "hrv_unchallenged",
                "evidence": f"HRV is {hrv_gate['status'].replace('_', ' ')} (7-day {hrv_gate.get('acute_mean')} vs band {hrv_gate.get('normal_band')}); not counted.",
            }
        )
    quality_rpe = quality.get("average_quality_rpe")
    if quality_rpe is not None and quality_rpe <= EASY_QUALITY_RPE:
        add(
            "quality_sessions_feel_easy",
            f"Average RPE of recent quality sessions is only {quality_rpe}/10.",
        )
    if eftp.get("status") == "available" and eftp.get("trend") == "flat":
        evidence = f"Bike eFTP moved {eftp['delta_pct']:+.1f}% over {eftp['window_days']} days."
        if bike_relevant:
            add("eftp_not_rising", evidence)
        else:
            reason = (
                "the priority race is not bike-relevant"
                if priority_race
                else "no race on the Intervals calendar"
            )
            context.append(
                {
                    "marker": "eftp_not_rising",
                    "evidence": evidence + f" Not counted: {reason}.",
                }
            )
    elif eftp.get("status") == "available" and eftp.get("trend") == "down":
        context.append(
            {
                "marker": "eftp_falling",
                "evidence": f"Bike eFTP fell {eftp['delta_pct']:+.1f}% over {eftp['window_days']} days: a fatigue / illness / overreaching signal, not underload.",
            }
        )
    if not priority_race:
        context.append(
            {
                "marker": "no_priority_race",
                "evidence": "No race on the Intervals calendar: bike relevance and race-specific floors are not applied.",
            }
        )

    explanations = []
    if health_events:
        explanations.append("A recent SICK/INJURED note explains reduced load.")
    rfb = return_from_break or {}
    if rfb.get("status") == "returning":
        explanations.append(
            f"Returning from a break: re-entry week {rfb.get('reentry_week')} at {rfb.get('volume_cap_pct')}% of the pre-break week is intended."
        )
    if phase == "taper":
        explanations.append("The athlete is in a planned taper; a positive TSB is intended.")
    if requested_week_type == "recovery":
        explanations.append("The athlete requested a recovery week.")
    if priority_race is not None:
        days_to_race = int(priority_race.get("days_to_race", 9999))
        window = taper_days(priority_race)
        if 0 <= days_to_race <= max(window, 6):
            explanations.append(
                f"{priority_race.get('name')} is {days_to_race} days away (taper window {window} days): reduced load is intended."
            )
    if last_race is not None:
        days_since = -int(last_race.get("days_to_race", 0))
        window = transition_days(last_race)
        if 0 < days_since <= window:
            explanations.append(
                f"{last_race.get('name')} was raced {days_since} days before this week (post-race transition {window} days)."
            )
    for name in holiday_names or []:
        explanations.append(f"Holiday {name} overlaps the last 14 days or the planned week.")

    physiological: list[str] = []
    if hrv_gate.get("status") == "suppressed":
        physiological.append("HRV (7-day lnRMSSD) is suppressed below its normal band.")
    if resting_hr_delta is not None and resting_hr_delta >= DAILY_RHR_DELTA_BPM:
        physiological.append(f"Resting HR is {resting_hr_delta:+.1f} bpm above its baseline.")
    signals = daily_signals or {}
    if signals.get("status") in {"warning", "alert"}:
        physiological.append(f"Daily early-warning signals are at {signals['status']}.")
    for field, reading in (subjective or {}).items():
        if reading.get("high"):
            physiological.append(
                f"Subjective {field} is high (3-day mean {reading.get('mean')} on the {reading.get('scale')} scale)."
            )

    vetoes = []
    veto_ramp = underload_veto_ramp(ctl)
    if ctl_ramp is not None and ctl_ramp >= veto_ramp:
        vetoes.append(
            f"CTL is rising {ctl_ramp:+.1f}/week over 3 weeks (>= {veto_ramp:g} for this CTL): real progression, not underload."
        )
    if ramp and ramp.get("status") in {"elevated", "high"}:
        vetoes.append(f"Ramp status is {ramp['status']}: load is rising, not stalling.")
    run_change = (run_load or {}).get("run_km_change_pct")
    if (
        not bike_relevant
        and run_change is not None
        and run_change >= RUN_BUILD_RISING_PCT
    ):
        vetoes.append(
            f"Run km rose {run_change:+.0f}% toward a run-focused goal: run-specific load is progressing even if total CTL is not."
        )

    load_hits = [item for item in markers if item["marker"] in LOAD_MARKERS]
    if len(markers) >= UNDERLOAD_MIN_MARKERS and load_hits:
        status = "detected"
    elif len(markers) >= 2 and load_hits:
        status = "watch"
    else:
        status = "not_detected"
    if status == "detected" and vetoes:
        status = "watch"
    if status in {"detected", "watch"} and physiological:
        status = "explained_physiological"
    elif status in {"detected", "watch"} and explanations:
        status = "explained"
    recommendation = {
        "detected": "Progress load this week: add a quality session or extend key-session time-in-zone, and target a CTL ramp of about +3 to +6/week.",
        "explained": "Underload markers are present but explained; resume progression as soon as the explanation no longer applies.",
        "explained_physiological": "Hold load: the body is not absorbing more right now (HRV / resting HR / fatigue); low load is not the limiter this week. Re-check next week.",
        "watch": "Some underload markers: progress the key sessions one rung rather than repeating last week's dose.",
        "not_detected": "No underload pattern; progress normally.",
    }[status]
    load_action = {
        "detected": "progress",
        "explained": "hold",
        "explained_physiological": "hold",
        "watch": "progress_one_rung",
        "not_detected": "normal",
    }[status]
    return {
        "status": status,
        "markers": markers,
        "marker_count": len(markers),
        "context": context,
        "explanations": explanations,
        "physiological_flags": physiological,
        "vetoes": vetoes,
        "recommendation": recommendation,
        "load_action": load_action,
        "ftp_stale_load_undercounted": ftp_undercounted,
        "quality_floor_per_week": quality_floor,
        "rules": {
            "min_markers": UNDERLOAD_MIN_MARKERS,
            "requires_one_of": sorted(LOAD_MARKERS),
            "veto": f"CTL ramp >= max({UNDERLOAD_VETO_FLOOR:g}, {UNDERLOAD_VETO_CTL_PCT:g}% CTL)/week or ramp status elevated/high caps the status at watch",
            "physiological_veto": "suppressed HRV, resting HR +5 bpm, daily warning/alert or high 3-day fatigue/soreness -> explained_physiological (hold)",
            "ftp_stale": "set FTP > 5% above eFTP with rides >= 20% of load: TSB/CTL markers are context only (ride load under-counted; loads are not rescaled)",
        },
    }


def _race_dates(races: list[dict[str, Any]], planning_date: date) -> set[str]:
    dates = set()
    for race in races:
        day = _parse_day(race.get("date"))
        if day is not None and day <= planning_date:
            dates.add(day.isoformat())
    return dates


def _health_and_holiday_spans(
    health_events: list[dict[str, Any]], holidays: list[dict[str, Any]]
) -> list[tuple[date, date]]:
    spans = []
    for item in [*health_events, *holidays]:
        start = _parse_day(item.get("start_date"))
        end = _parse_day(item.get("end_date")) or start
        if start is not None and end is not None:
            spans.append((start, end))
    return spans


def assess_training_load(
    *,
    activities: list[dict[str, Any]],
    wellness: list[dict[str, Any]],
    athlete_profile: dict[str, Any],
    athlete_state: dict[str, Any],
    health_events: list[dict[str, Any]],
    planning_date: date,
    requested_week_type: str | None = None,
    races: list[dict[str, Any]] | None = None,
    holidays: list[dict[str, Any]] | None = None,
    week_start: date | None = None,
    sport_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """All training-load signals. `races` must be the Intervals-derived snapshot races."""
    planned_start = week_start or _planned_week_start(planning_date)
    races = races or []
    holidays = holidays or []
    race_dates = _race_dates(races, planning_date)
    fitness = assess_fitness(wellness, planning_date)
    return_from_break = assess_return_from_break(
        activities,
        planning_date,
        fitness=fitness,
        health_events=health_events,
        holidays=holidays,
        week_start=planned_start,
        race_dates=race_dates,
    )
    ramp = assess_ramp(activities, fitness, planning_date, return_from_break=return_from_break)
    ftp_staleness = assess_ftp_staleness(activities, planning_date, sport_settings)
    distribution = assess_intensity_distribution(
        activities, planning_date, ftp_stale=bool(ftp_staleness.get("stale"))
    )
    quality = assess_quality_frequency(activities, planning_date, week_start=planned_start)
    excluded_spans = _health_and_holiday_spans(health_events, holidays) + [
        (start, end)
        for start, end, _ in break_spans(activities, planning_date)
    ]
    volume = assess_volume(
        activities, planning_date, athlete_profile, excluded_spans=excluded_spans
    )
    hrv_gate = assess_hrv_gate(wellness, planning_date=planning_date)
    eftp = assess_eftp_trend(activities, planning_date)
    daily_signals = assess_daily_signals(wellness, planning_date)
    subjective = {
        field: subjective_rolling(wellness, field, planning_date) for field in SUBJECTIVE_HIGH
    }
    rhr_delta = resting_hr_delta_7d(wellness, planning_date)
    active_phase = (athlete_state.get("active_macro_cycle") or {}).get("phase")
    # Races come only from the Intervals calendar; the profile never adds one.
    race_context = {"races": races, "athlete_profile": {}}
    priority_race = select_priority_race(race_context)
    run_load = assess_run_load(
        activities,
        planning_date,
        week_start=planned_start,
        race_dates=race_dates,
        return_from_break=return_from_break,
    )
    efficiency = assess_efficiency_trend(activities, planning_date)
    overreaching = assess_overreaching(
        wellness=wellness,
        planning_date=planning_date,
        fitness=fitness,
        ramp=ramp,
        eftp=eftp,
        efficiency=efficiency,
        hrv_gate=hrv_gate,
        daily_signals=daily_signals,
        rhr_delta_7d=rhr_delta,
    )
    underload = assess_underload(
        fitness=fitness,
        quality=quality,
        volume=volume,
        hrv_gate=hrv_gate,
        eftp=eftp,
        health_events=health_events,
        active_phase=active_phase,
        requested_week_type=requested_week_type,
        ramp=ramp,
        bike_relevant=is_bike_relevant(priority_race),
        priority_race=priority_race,
        last_race=last_completed_a_race(race_context),
        holiday_names=_holiday_overlaps(holidays, planning_date, planned_start),
        run_load=run_load,
        daily_signals=daily_signals,
        subjective=subjective,
        resting_hr_delta=rhr_delta,
        ftp_staleness=ftp_staleness,
        return_from_break=return_from_break,
    )
    return {
        "fitness": fitness,
        "ramp": ramp,
        "return_from_break": return_from_break,
        "intensity_distribution_28d": distribution,
        "quality_frequency": quality,
        "volume": volume,
        "run_load": run_load,
        "ride_load": assess_ride_load(activities, planning_date),
        "other_load": assess_other_load(activities, planning_date),
        "hrv_gate": hrv_gate,
        "eftp_trend": eftp,
        "ftp_staleness": ftp_staleness,
        "daily_signals": daily_signals,
        "subjective_3d": subjective,
        "resting_hr_delta_7d": rhr_delta,
        "overreaching": overreaching,
        "efficiency_trend": efficiency,
        "underload": underload,
    }
