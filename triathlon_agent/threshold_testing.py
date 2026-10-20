from __future__ import annotations

import re
from datetime import date
from typing import Any

from .race_selection import race_bucket, taper_days
from .utils import NO_NIGGLE_VALUES, has_niggle, parse_date


# Bike and run tests are planned automatically. Swim (CSS) is tracked for
# staleness only: no swim test is ever pushed automatically, because the
# 400/200 m CSS protocol needs pool time the plan cannot guarantee.
DISCIPLINES = ("bike", "run")
TRACKED_DISCIPLINES = ("bike", "run", "swim")
SPORT_BY_DISCIPLINE = {"bike": "Ride", "run": "Run", "swim": "Swim"}
ACTIVITY_DISCIPLINE = {
    "Ride": "bike",
    "VirtualRide": "bike",
    "GravelRide": "bike",
    "Run": "run",
    "VirtualRun": "run",
    "TrailRun": "run",
    "Swim": "swim",
    "OpenWaterSwim": "swim",
}
BIKE_ACTIVITY_TYPES = {"Ride", "VirtualRide", "GravelRide", "MountainBikeRide"}
# Set FTP and Intervals' rolling eFTP more than 5% apart: FTP is stale.
FTP_STALE_PCT = 5.0
READINESS_ALIASES = {"amber": "yellow", "orange": "yellow"}

# Friel "rest & test": re-test each discipline roughly every 8 weeks (alternating
# recovery weeks on a 3:1 rhythm), never sooner than 6 weeks unless an early
# trigger fires, and treat anything older than 12 weeks as overdue.
MIN_INTERVAL_DAYS = 42
EARLY_TRIGGER_MIN_DAYS = 28
# A maximal test is never "routine": a discipline is only tested once it is due
# (>= MIN_INTERVAL_DAYS, or an early trigger after EARLY_TRIGGER_MIN_DAYS).
# Kept for the `rules` block of the assessment.
ROUTINE_RETEST_MIN_DAYS = MIN_INTERVAL_DAYS
POST_A_RACE_WINDOW_DAYS = 28
MAX_INTERVAL_DAYS = 84
A_RACE_BLACKOUT_DAYS = 21
B_RACE_BLACKOUT_DAYS = 7
RACE_SUBSTITUTE_LOOKBACK_DAYS = 14
HEALTH_RETURN_BLOCK_DAYS = 10
# One drift rule for the early trigger and the stale flag (strictly above).
DRIFT_TRIGGER_PCT = FTP_STALE_PCT
# Set FTP this far ABOVE a confident eFTP: the interim update is proposed with
# apply=true (still gated by the athlete's approval).
FTP_TOO_HIGH_APPLY_PCT = 10.0
# The rolling eFTP is confident enough when it is this recent.
EFTP_CONFIDENT_MAX_AGE_DAYS = 28
# Race buckets whose A-race is decided on foot: the bike only tests when its
# FTP is stale or overdue and bike targets are actually prescribed.
RUN_FOCUSED_BUCKETS = {"trail", "ultra_trail", "5k", "10k", "half_marathon", "marathon"}
MAX_AUTO_APPLY_DELTA_PCT = 25.0
MIN_MEANINGFUL_DELTA_PCT = 1.0

DEFAULT_PREFERENCES: dict[str, str] = {
    "bike_protocol": "coggan_20",
    "bike_protocol_alternative": "friel_30",
    "run_protocol": "friel_30",
}

PROTOCOLS: dict[str, dict[str, Any]] = {
    "coggan_20": {
        "discipline": "bike",
        "label": "Coggan/Allen 20-minute FTP test",
        "test_minutes": 20,
        "ftp_factor": 0.95,
        "formula": "FTP = average power of the 20-minute effort x 0.95. LTHR is only a low-confidence estimate from this protocol.",
    },
    "friel_30": {
        "discipline": "bike",
        "label": "Friel 30-minute bike time trial",
        "test_minutes": 30,
        "ftp_factor": 1.0,
        "formula": "FTP = average power of the full 30 minutes. LTHR = average HR of the final 20 minutes.",
    },
    "friel_30_run": {
        "discipline": "run",
        "label": "Friel 30-minute run time trial",
        "test_minutes": 30,
        "formula": "Threshold pace = average pace of the full 30 minutes. LTHR = average HR of the final 20 minutes.",
    },
    "css_400_200": {
        "discipline": "swim",
        "label": "CSS test (400 m + 200 m)",
        "test_minutes": 10,
        "formula": "CSS pace per 100 m = (T400 - T200) / 2. Tracked only; review manually.",
    },
}

TEST_NAME_RE = re.compile(
    r"(\[test\]|\b(ftp|threshold|lthr|drempel|css)\b.*\btest\b|\btest\b.*\b(ftp|threshold|lthr|css)\b)",
    re.IGNORECASE,
)
# Threshold (~60-minute race) pace relative to the race pace in sec/km, keyed
# by race_selection distance bucket. A recent race is a de-facto run test.
# Only flat road races of 10k or longer substitute for the run test (a 5k is
# too far above threshold, a trail race is not a pace test).
RUN_RACE_SUBSTITUTES = {
    "10k": 1.03,
    "half_marathon": 0.975,
    "marathon": 0.955,
}
# NO_NIGGLE_VALUES is re-exported from utils for backwards compatibility;
# niggle detection itself lives in utils.has_niggle.
__all__ = ["NO_NIGGLE_VALUES", "has_niggle"]


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def resolve_protocol(discipline: str, preferences: dict[str, str]) -> str:
    if discipline == "run":
        return "friel_30_run"
    if discipline == "swim":
        return "css_400_200"
    protocol = preferences.get("bike_protocol") or DEFAULT_PREFERENCES["bike_protocol"]
    if protocol not in PROTOCOLS or PROTOCOLS[protocol]["discipline"] != "bike":
        raise ValueError(
            f"Unsupported bike threshold test protocol: {protocol}. Use 'coggan_20' or 'friel_30'."
        )
    return protocol


def testing_preferences(
    athlete_profile: dict[str, Any], weekly_checkin: dict[str, Any]
) -> dict[str, str]:
    preferences = dict(DEFAULT_PREFERENCES)
    profile_preferences = athlete_profile.get("threshold_testing") or {}
    for key in DEFAULT_PREFERENCES:
        if profile_preferences.get(key):
            preferences[key] = str(profile_preferences[key])
    weekly_override = weekly_checkin.get("bike_test_protocol")
    if weekly_override:
        preferences["bike_protocol"] = str(weekly_override)
    resolve_protocol("bike", preferences)
    return preferences


def is_threshold_test_activity(activity: dict[str, Any]) -> bool:
    return bool(TEST_NAME_RE.search(str(activity.get("name") or "")))


def _protocol_from_name(name: str, discipline: str, preferences: dict[str, str]) -> str:
    if discipline in {"run", "swim"}:
        return resolve_protocol(discipline, preferences)
    lowered = name.lower()
    if re.search(r"\b30\s*(min|m|')", lowered) or "friel" in lowered:
        return "friel_30"
    if re.search(r"\b20\s*(min|m|')", lowered) or "coggan" in lowered:
        return "coggan_20"
    return resolve_protocol("bike", preferences)


def detect_test_activities(
    activities: list[dict[str, Any]],
    preferences: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    resolved = preferences or DEFAULT_PREFERENCES
    detected = []
    for activity in activities:
        discipline = ACTIVITY_DISCIPLINE.get(str(activity.get("type") or ""))
        if not discipline or not is_threshold_test_activity(activity):
            continue
        name = str(activity.get("name") or "")
        detected.append(
            {
                "discipline": discipline,
                "date": str(activity.get("start_date_local") or "")[:10],
                "activity_id": activity.get("id"),
                "name": name,
                "protocol": _protocol_from_name(name, discipline, resolved),
                "source": "test",
            }
        )
    detected.sort(key=lambda item: item["date"])
    return detected


def _minutes(interval: dict[str, Any]) -> float:
    seconds = _safe_float(interval.get("moving_time")) or _safe_float(
        interval.get("elapsed_time")
    )
    return (seconds or 0.0) / 60.0


def _is_near(minutes: float, target: float, tolerance: float = 2.5) -> bool:
    return abs(minutes - target) <= tolerance


def _work_intervals(intervals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        intervals, key=lambda item: _safe_float(item.get("start_index")) or 0.0
    )
    return [
        item
        for item in ordered
        if str(item.get("type") or "WORK").upper() != "RECOVERY"
    ]


def _weighted(items: list[dict[str, Any]], field: str) -> float | None:
    total_seconds = 0.0
    total = 0.0
    for item in items:
        value = _safe_float(item.get(field))
        seconds = _minutes(item) * 60.0
        if value is None or not seconds:
            return None
        total += value * seconds
        total_seconds += seconds
    if not total_seconds:
        return None
    return total / total_seconds


def _pace_sec_per_km(items: list[dict[str, Any]]) -> float | None:
    distance = sum(_safe_float(item.get("distance")) or 0.0 for item in items)
    seconds = sum(_minutes(item) * 60.0 for item in items)
    if distance <= 0 or seconds <= 0:
        return None
    return seconds / (distance / 1000.0)


def _find_30_minute_split(
    intervals: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Return the full 30' test segments plus the final-20' segment when laps allow it."""
    work = _work_intervals(intervals)
    for first, second in zip(work, work[1:]):
        if _is_near(_minutes(first), 10, 1.5) and _is_near(_minutes(second), 20, 2.0):
            return [first, second], second
    for item in work:
        if _is_near(_minutes(item), 30):
            return [item], None
    return [], None


def _best_20_minute_interval(intervals: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [
        item
        for item in _work_intervals(intervals)
        if _is_near(_minutes(item), 20) and _safe_float(item.get("average_watts"))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: _safe_float(item.get("average_watts")) or 0.0)


def pace_setting_to_sec_per_km(value: Any) -> float | None:
    """Intervals stores threshold_pace in m/s; tolerate sec/km values in fixtures."""
    number = _safe_float(value)
    if not number or number <= 0:
        return None
    if number < 20:
        return 1000.0 / number
    return number


def sec_per_km_to_pace_setting(sec_per_km: float, current_value: Any) -> float:
    current = _safe_float(current_value)
    if current is not None and current >= 20:
        return round(sec_per_km, 1)
    return round(1000.0 / sec_per_km, 4)


def format_pace(sec_per_km: float | None) -> str | None:
    if not sec_per_km:
        return None
    total = int(round(sec_per_km))
    return f"{total // 60}:{total % 60:02d}/km"


def compute_test_result(
    test: dict[str, Any], intervals: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """Derive threshold values from the laps/intervals of a completed test."""
    protocol = test["protocol"]
    result: dict[str, Any] = {
        **test,
        "protocol_label": PROTOCOLS[protocol]["label"],
        "formula": PROTOCOLS[protocol]["formula"],
        "status": "needs_review",
        "values": {},
        "notes": [],
    }
    if protocol == "css_400_200":
        result["notes"].append(
            "Swim CSS tests are tracked for staleness only; update CSS in Intervals manually."
        )
        return result
    if not intervals:
        result["notes"].append(
            "No interval/lap data was available for this test, so thresholds could not be derived automatically."
        )
        return result

    if protocol == "coggan_20":
        interval = _best_20_minute_interval(intervals)
        if interval is None:
            result["notes"].append(
                "No ~20-minute work interval with power was found. Keep the 20' test as its own lap/step."
            )
            return result
        watts = _safe_float(interval.get("average_watts")) or 0.0
        result["values"]["ftp"] = {
            "value": int(round(watts * PROTOCOLS[protocol]["ftp_factor"])),
            "confidence": "high",
            "detail": f"20' average {int(round(watts))} W x 0.95",
        }
        heart_rate = _safe_float(interval.get("average_heartrate"))
        if heart_rate:
            result["values"]["lthr"] = {
                "value": int(round(heart_rate)),
                "confidence": "low",
                "detail": "20' average HR; the 20' protocol is not designed for LTHR, so this is an upper-bound estimate only.",
            }
        result["status"] = "computed"
        return result

    segments, final_twenty = _find_30_minute_split(intervals)
    if not segments:
        result["notes"].append(
            "No ~30-minute test effort (or 10' + 20' laps) was found in the interval data."
        )
        return result

    heart_rate_source = final_twenty or segments[0]
    heart_rate = _safe_float(heart_rate_source.get("average_heartrate"))
    lthr_confidence = "high" if final_twenty is not None else "low"
    lthr_detail = (
        "average HR of the final 20' lap"
        if final_twenty is not None
        else "average HR of the full 30'; press lap at 10' next time for a proper final-20' LTHR"
    )
    if protocol == "friel_30":
        watts = _weighted(segments, "average_watts")
        if watts:
            result["values"]["ftp"] = {
                "value": int(round(watts)),
                "confidence": "high",
                "detail": f"30' average {int(round(watts))} W",
            }
    else:
        pace = _pace_sec_per_km(segments)
        if pace:
            result["values"]["threshold_pace"] = {
                "value": round(pace, 1),
                "confidence": "high",
                "detail": f"30' average pace {format_pace(pace)}",
            }
    if heart_rate:
        result["values"]["lthr"] = {
            "value": int(round(heart_rate)),
            "confidence": lthr_confidence,
            "detail": lthr_detail,
        }
    result["status"] = "computed" if result["values"] else "needs_review"
    return result


def _race_substitutes(
    races: list[dict[str, Any]], activities: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    substitutes = []
    for race in races:
        days_to_race = race.get("days_to_race")
        factor = RUN_RACE_SUBSTITUTES.get(race_bucket(race))
        if factor is None or days_to_race is None:
            continue
        if not (-RACE_SUBSTITUTE_LOOKBACK_DAYS <= int(days_to_race) < 0):
            continue
        match = next(
            (
                activity
                for activity in activities
                if ACTIVITY_DISCIPLINE.get(str(activity.get("type") or "")) == "run"
                and str(activity.get("start_date_local") or "")[:10] == race.get("date")
                and (_safe_float(activity.get("distance")) or 0.0) >= 4800
            ),
            None,
        )
        if match is None:
            continue
        distance_km = (_safe_float(match.get("distance")) or 0.0) / 1000.0
        seconds = _safe_float(match.get("moving_time")) or 0.0
        if distance_km <= 0 or seconds <= 0:
            continue
        race_pace = seconds / distance_km
        threshold = race_pace * factor
        substitutes.append(
            {
                "discipline": "run",
                "date": race["date"],
                "activity_id": match.get("id"),
                "name": race.get("name") or match.get("name"),
                "protocol": "race_substitute",
                "protocol_label": f"{race.get('distance')} race used as run threshold test",
                "formula": f"Threshold pace = race pace x {factor}.",
                "source": "race",
                "status": "computed",
                "values": {
                    "threshold_pace": {
                        "value": round(threshold, 1),
                        "confidence": "medium",
                        "detail": f"race pace {format_pace(race_pace)} x {factor}",
                    }
                },
                "notes": [],
            }
        )
    return substitutes


def _latest_record_date(record: dict[str, Any] | None) -> date | None:
    if not record or not record.get("last_test_date"):
        return None
    return parse_date(str(record["last_test_date"]))


def _latest_eftp_detail(
    activities: list[dict[str, Any]],
) -> tuple[float | None, str | None, str | None]:
    """Intervals rolling eFTP from the most recent ride carrying it.

    `icu_rolling_ftp` is the athlete-level eFTP model. `icu_eftp` is only a
    fallback, and the single-ride `icu_pm_ftp` is never used: an easy spin
    produces a meaningless low value.
    """
    for field in ("icu_rolling_ftp", "icu_eftp"):
        for activity in reversed(activities):
            if str(activity.get("type") or "") not in BIKE_ACTIVITY_TYPES:
                continue
            value = _safe_float(activity.get(field))
            if value and value > 0:
                return value, field, str(activity.get("start_date_local") or "")[:10]
    return None, None, None


def _latest_eftp(activities: list[dict[str, Any]]) -> float | None:
    return _latest_eftp_detail(activities)[0]


def _has_niggle(weekly_checkin: dict[str, Any]) -> bool:
    return has_niggle(weekly_checkin.get("pain_or_niggles"))


def _blockers(
    discipline: str,
    races: list[dict[str, Any]],
    health_events: list[dict[str, Any]],
    weekly_checkin: dict[str, Any],
) -> list[str]:
    reasons = []
    for race in races:
        days_to_race = race.get("days_to_race")
        if days_to_race is None or int(days_to_race) < 0:
            continue
        priority = race.get("priority")
        if priority == "A" and int(days_to_race) <= A_RACE_BLACKOUT_DAYS:
            reasons.append(
                f"A-race {race.get('name')} is {days_to_race} days away; no threshold testing inside the final {A_RACE_BLACKOUT_DAYS} days."
            )
        elif priority == "B" and int(days_to_race) <= B_RACE_BLACKOUT_DAYS:
            reasons.append(
                f"B-race {race.get('name')} is {days_to_race} days away; keep the legs fresh instead of testing."
            )
    for event in health_events:
        days_since_end = int(event.get("days_since_end", 99))
        if event.get("ongoing") or days_since_end < HEALTH_RETURN_BLOCK_DAYS:
            reasons.append(
                f"{str(event.get('kind', 'health')).capitalize()} note '{event.get('name')}' is too recent; test only after {HEALTH_RETURN_BLOCK_DAYS}+ days of normal training."
            )
        if discipline == "run" and event.get("kind") == "injured":
            reasons.append(
                "An injury note is on the calendar; a maximal run test is not worth the impact risk."
            )
    if discipline == "run" and _has_niggle(weekly_checkin):
        reasons.append(
            f"Current niggle reported ({weekly_checkin.get('pain_or_niggles')}); no all-out run test until it is gone."
        )
    return list(dict.fromkeys(reasons))


def _early_trigger(
    discipline: str,
    sport_settings: dict[str, Any],
    activities: list[dict[str, Any]],
    health_events: list[dict[str, Any]],
) -> str | None:
    for event in health_events:
        if event.get("ongoing"):
            continue
        try:
            start = parse_date(str(event.get("start_date") or event.get("end_date")))
            end = parse_date(str(event.get("end_date") or event.get("start_date")))
        except (TypeError, ValueError):
            # A note without usable dates cannot say how long the break was.
            continue
        if (end - start).days + 1 >= 7 and int(event.get("days_since_end", 0)) >= HEALTH_RETURN_BLOCK_DAYS:
            return f"Returning from a {(end - start).days + 1}-day {event.get('kind')} break; re-check thresholds."
    if discipline == "bike":
        ftp = _safe_float(sport_settings.get("Ride", {}).get("ftp"))
        eftp = _latest_eftp(activities)
        drift = _ftp_drift_pct(ftp, eftp)
        if drift is not None and abs(drift) > DRIFT_TRIGGER_PCT:
            return f"Set FTP {int(ftp)} W differs {round(abs(drift), 1)}% from eFTP {int(eftp)} W."
    return None


def _ftp_drift_pct(ftp: float | None, eftp: float | None) -> float | None:
    """(eFTP - FTP) / FTP in %, the one drift definition used everywhere."""
    if not ftp or not eftp:
        return None
    return round((eftp - ftp) / ftp * 100.0, 1)


def _proposed_updates(
    results: list[dict[str, Any]], sport_settings: dict[str, Any]
) -> list[dict[str, Any]]:
    labels = {
        "ftp": ("FTP", "W"),
        "lthr": ("LTHR", "bpm"),
        "threshold_pace": ("Threshold pace", "sec/km"),
    }
    latest: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
    for result in results:
        if result.get("status") != "computed":
            continue
        for field, value in result.get("values", {}).items():
            latest[(result["discipline"], field)] = (result, value)

    updates = []
    for (discipline, field), (result, value) in sorted(latest.items()):
        sport = SPORT_BY_DISCIPLINE[discipline]
        settings = sport_settings.get(sport, {})
        current_raw = settings.get(field)
        proposed_setting: Any = value["value"]
        if field == "threshold_pace":
            current = pace_setting_to_sec_per_km(current_raw)
            proposed_setting = sec_per_km_to_pace_setting(value["value"], current_raw)
            proposed_display = value["value"]
        else:
            current = _safe_float(current_raw)
            proposed_display = value["value"]
        delta_pct = (
            round((proposed_display - current) / current * 100.0, 1) if current else None
        )
        apply = True
        apply_reason = "Confident result from the latest test."
        if value["confidence"] == "low":
            apply = False
            apply_reason = "Low-confidence estimate; shown for context only."
        elif settings.get("id") is None:
            apply = False
            apply_reason = f"No Intervals sport-settings id found for {sport}."
        elif delta_pct is not None and abs(delta_pct) > MAX_AUTO_APPLY_DELTA_PCT:
            apply = False
            apply_reason = f"Change of {delta_pct}% is implausibly large; review the test before applying."
        elif delta_pct is not None and abs(delta_pct) < MIN_MEANINGFUL_DELTA_PCT:
            apply = False
            apply_reason = "Change is within day-to-day noise; keep the current value."
        label, unit = labels[field]
        updates.append(
            {
                "discipline": discipline,
                "sport": sport,
                "sport_settings_id": settings.get("id"),
                "field": field,
                "label": label,
                "unit": unit,
                "current": current_raw,
                "current_display": format_pace(current) if field == "threshold_pace" else current,
                "proposed": proposed_setting,
                "proposed_display": format_pace(proposed_display) if field == "threshold_pace" else proposed_display,
                "delta_pct": delta_pct,
                "confidence": value["confidence"],
                "detail": value["detail"],
                "apply": apply,
                "apply_reason": apply_reason,
                "source": {
                    "date": result["date"],
                    "activity_id": result.get("activity_id"),
                    "name": result.get("name"),
                    "protocol": result["protocol"],
                },
            }
        )
    return updates


def assess_threshold_testing(
    *,
    athlete_profile: dict[str, Any],
    weekly_checkin: dict[str, Any],
    athlete_state: dict[str, Any],
    activities: list[dict[str, Any]],
    races: list[dict[str, Any]],
    health_events: list[dict[str, Any]],
    sport_settings: dict[str, Any],
    test_intervals: dict[str, list[dict[str, Any]]],
    week_start: date,
    block_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    preferences = testing_preferences(athlete_profile, weekly_checkin)
    state = athlete_state.get("threshold_testing") or {}
    detected = detect_test_activities(activities, preferences)

    new_results = []
    for test in detected:
        record_date = _latest_record_date(state.get(test["discipline"]))
        if record_date and parse_date(test["date"]) <= record_date:
            continue
        new_results.append(
            compute_test_result(test, test_intervals.get(str(test["activity_id"])))
        )
    for substitute in _race_substitutes(races, activities):
        record_date = _latest_record_date(state.get("run"))
        if record_date and parse_date(substitute["date"]) <= record_date:
            continue
        new_results.append(substitute)
    new_results.sort(key=lambda item: item["date"])

    disciplines: dict[str, Any] = {}
    for discipline in TRACKED_DISCIPLINES:
        candidates = []
        record = state.get(discipline) or {}
        if record.get("last_test_date"):
            candidates.append((str(record["last_test_date"]), record.get("source", "state")))
        candidates.extend(
            (item["date"], item["source"])
            for item in [*detected, *new_results]
            if item["discipline"] == discipline
        )
        last_date, last_source = max(candidates) if candidates else (None, None)
        days_since = (week_start - parse_date(last_date)).days if last_date else None
        trigger = _early_trigger(discipline, sport_settings, activities, health_events)

        if days_since is None:
            urgency = "overdue"
            reason = "No threshold test on record."
        elif days_since >= MAX_INTERVAL_DAYS:
            urgency = "overdue"
            reason = f"Last test was {days_since} days ago (max {MAX_INTERVAL_DAYS})."
        elif days_since >= MIN_INTERVAL_DAYS:
            urgency = "due"
            reason = f"Last test was {days_since} days ago; inside the normal {MIN_INTERVAL_DAYS}-{MAX_INTERVAL_DAYS} day retest window."
        elif trigger and days_since >= EARLY_TRIGGER_MIN_DAYS:
            urgency = "due"
            reason = f"Early retest allowed: {trigger}"
        else:
            urgency = "not_due"
            reason = f"Last test was {days_since} days ago; next retest from day {MIN_INTERVAL_DAYS}."
        if trigger and urgency == "overdue":
            reason = f"{reason} {trigger}"

        blockers = (
            _blockers(discipline, races, health_events, weekly_checkin)
            if discipline in DISCIPLINES
            else []
        )
        disciplines[discipline] = {
            "status": "blocked" if blockers and urgency != "not_due" else urgency,
            "urgency": urgency,
            "reason": reason,
            "blockers": blockers,
            "early_trigger": trigger,
            "last_test_date": last_date,
            "last_test_source": last_source,
            "days_since_last_test": days_since,
            "protocol": resolve_protocol(discipline, preferences),
        }

    disciplines["bike"]["alternative_protocol"] = preferences["bike_protocol_alternative"]
    disciplines["bike"].update(_bike_threshold_state(sport_settings, activities))
    swim_settings = sport_settings.get("Swim", {})
    disciplines["swim"].update(
        {
            "auto_schedule": False,
            "css_setting": swim_settings.get("threshold_pace"),
            "css_pace_units": swim_settings.get("pace_units"),
            "note": "Swim CSS is tracked for staleness only; no swim test is scheduled automatically.",
        }
    )
    proposed_updates = _proposed_updates(new_results, sport_settings)
    interim = _eftp_interim_update(
        disciplines["bike"], sport_settings, proposed_updates, week_start
    )
    if interim:
        proposed_updates.append(interim)
    in_taper = any(
        race.get("priority") in {"A", "B"}
        and race.get("days_to_race") is not None
        and 0 <= int(race["days_to_race"]) <= taper_days(race)
        for race in races
    )
    recent_a_race = next(
        (
            {"name": race.get("name"), "date": race.get("date"), "days_since": -int(race["days_to_race"])}
            for race in sorted(races, key=lambda item: -int(item.get("days_to_race") or 0))
            if race.get("priority") == "A"
            and race.get("days_to_race") is not None
            and -POST_A_RACE_WINDOW_DAYS <= int(race["days_to_race"]) < 0
        ),
        None,
    )
    return {
        "recent_a_race": recent_a_race,
        "preferences": preferences,
        "rules": {
            "min_interval_days": MIN_INTERVAL_DAYS,
            "early_trigger_min_days": EARLY_TRIGGER_MIN_DAYS,
            "max_interval_days": MAX_INTERVAL_DAYS,
            "a_race_blackout_days": A_RACE_BLACKOUT_DAYS,
            "b_race_blackout_days": B_RACE_BLACKOUT_DAYS,
            "routine_retest_min_days": ROUTINE_RETEST_MIN_DAYS,
            "placement": "every recovery week ends with a test, alternating bike and run; both after an A-race; optionally block week 1 straight after a recovery week",
            "ftp_stale_pct": FTP_STALE_PCT,
        },
        "block_context": {
            "last_week_type": (block_context or {}).get("last_week_type"),
            "next_block_week": (block_context or {}).get("next_block_week"),
            "race_in_taper_window": in_taper,
        },
        **disciplines,
        "completed_tests": new_results,
        "proposed_updates": proposed_updates,
    }


def _bike_threshold_state(
    sport_settings: dict[str, Any], activities: list[dict[str, Any]]
) -> dict[str, Any]:
    ftp = _safe_float(sport_settings.get("Ride", {}).get("ftp"))
    eftp, field, eftp_date = _latest_eftp_detail(activities)
    drift_pct = _ftp_drift_pct(ftp, eftp)
    stale = drift_pct is not None and abs(drift_pct) > FTP_STALE_PCT
    return {
        "ftp_setting": ftp,
        "eftp_rolling": round(eftp) if eftp else None,
        "eftp_source_field": field,
        "eftp_date": eftp_date,
        "ftp_eftp_drift_pct": drift_pct,
        "threshold_status": "stale" if stale else ("current" if drift_pct is not None else "unknown"),
        "stale": stale,
        # Planner: multiply %FTP targets by this while FTP is stale.
        "ftp_target_scale": round(eftp / ftp, 3) if ftp and eftp else None,
    }


def _eftp_confident(bike: dict[str, Any], week_start: date | None) -> tuple[bool, str]:
    """Rolling athlete-level eFTP from a recent ride is adequate for an interim FTP."""
    if bike.get("eftp_source_field") != "icu_rolling_ftp":
        return False, f"source {bike.get('eftp_source_field') or 'unknown'}, not the rolling eFTP model"
    if week_start is None or not bike.get("eftp_date"):
        return True, "Intervals rolling eFTP model"
    try:
        age = (week_start - parse_date(str(bike["eftp_date"]))).days
    except (TypeError, ValueError):
        return False, "eFTP date unreadable"
    if age > EFTP_CONFIDENT_MAX_AGE_DAYS:
        return False, f"latest eFTP is {age} days old"
    return True, f"Intervals rolling eFTP model, {age} days old"


def _eftp_interim_update(
    bike: dict[str, Any],
    sport_settings: dict[str, Any],
    proposed_updates: list[dict[str, Any]],
    week_start: date | None = None,
) -> dict[str, Any] | None:
    """Interim FTP = rolling eFTP while the set FTP is stale (athlete approves).

    Set FTP > 10% above a confident eFTP: apply=true by default (the athlete
    still approves the week). eFTP above FTP: proposed, apply=false, while the
    planner scales targets up a little.
    """
    if not bike.get("stale"):
        return None
    if any(
        item.get("discipline") == "bike" and item.get("field") == "ftp"
        for item in proposed_updates
    ):
        return None
    settings = sport_settings.get("Ride", {})
    ftp = bike["ftp_setting"]
    eftp = int(bike["eftp_rolling"])
    drift = float(bike["ftp_eftp_drift_pct"])
    confident, confidence_note = _eftp_confident(bike, week_start)
    if drift < 0 and abs(drift) > FTP_TOO_HIGH_APPLY_PCT and confident and settings.get("id") is not None:
        apply = True
        apply_reason = (
            f"Set FTP {int(ftp)} W is {abs(drift)}% ABOVE the rolling eFTP {eftp} W ({confidence_note}): "
            "every %FTP target is too hard and quality sessions turn into threshold+ efforts. "
            "Proposed as the interim FTP until the next test; it is only written to Intervals "
            "when you approve the week (set apply=false to keep the old value)."
        )
    elif drift > 0:
        apply = False
        apply_reason = (
            f"Rolling eFTP {eftp} W is {drift}% ABOVE the set FTP {int(ftp)} W: targets are too easy. "
            "Meanwhile the plan scales bike targets up a little; approve (apply=true) to raise FTP, "
            "or confirm it with the next FTP test."
        )
    else:
        apply = False
        apply_reason = (
            f"Set FTP is {abs(drift)}% away from the rolling eFTP, so every %FTP target is off. "
            + (
                "Interim value until the next FTP test; set apply=true to approve it."
                if confident
                else f"eFTP confidence is limited ({confidence_note}); set apply=true to approve it anyway."
            )
        )
    return {
        "discipline": "bike",
        "sport": "Ride",
        "sport_settings_id": settings.get("id"),
        "field": "ftp",
        "label": "FTP",
        "unit": "W",
        "current": settings.get("ftp"),
        "current_display": ftp,
        "proposed": eftp,
        "proposed_display": eftp,
        "delta_pct": bike["ftp_eftp_drift_pct"],
        "confidence": "medium" if confident else "low",
        "detail": f"Intervals rolling eFTP {eftp} W (ride on {bike.get('eftp_date')}) vs set FTP {int(ftp)} W.",
        "apply": apply,
        "apply_reason": apply_reason,
        "requires_athlete_approval": True,
        "source": "eftp_interim",
        "source_detail": {
            "date": bike.get("eftp_date"),
            "field": bike.get("eftp_source_field"),
            "protocol": "eftp_interim",
        },
    }


def plan_threshold_tests(
    testing: dict[str, Any],
    *,
    recovery_week: bool,
    readiness_status: str,
    allow_block_week_1: bool = False,
    priority_bucket: str | None = None,
    bike_targets_prescribed: bool = True,
    return_from_break: bool = False,
) -> dict[str, Any]:
    """Macro-coach decision: which threshold test(s) belong in this week.

    Tests go at the end of a recovery week (Friel rest & test), but only for a
    discipline that is due (>= 42 days, or an early trigger after 28) and
    relevant to the A-race: with a run-decided A-race (trail, road) the bike is
    tested only when its FTP is stale or overdue AND bike targets are actually
    prescribed. A recent flat road race (>= 10k) already counts as the run
    test (its date resets the clock). Never in a return-from-break week.
    With `allow_block_week_1=True` a due test may also open block week 1
    straight after a recovery week (green, no A/B race in its taper window).
    """
    scheduled: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    if not testing:
        return {
            "scheduled": [],
            "deferred": [],
            "rationale": "No threshold testing assessment was available in the snapshot.",
        }
    readiness_status = READINESS_ALIASES.get(
        str(readiness_status or "").lower(), str(readiness_status or "").lower()
    )
    block_context = testing.get("block_context") or {}
    block_week_1 = (
        allow_block_week_1
        and not recovery_week
        and block_context.get("last_week_type") == "recovery"
        and readiness_status == "green"
        and not block_context.get("race_in_taper_window")
    )
    test_window = recovery_week or block_week_1
    placement = "recovery_week" if recovery_week else "block_week_1"
    run_focused = priority_bucket in RUN_FOCUSED_BUCKETS

    candidates = []
    for discipline in DISCIPLINES:
        item = testing.get(discipline) or {}
        status = item.get("status")
        if not status or status == "not_due":
            continue
        if return_from_break:
            deferred.append(
                {
                    "discipline": discipline,
                    "reason": "Return from a break: rebuild first; a maximal test now measures detraining, not fitness.",
                }
            )
            continue
        if status == "blocked":
            deferred.append({"discipline": discipline, "reason": "; ".join(item.get("blockers", []))})
            continue
        if discipline == "bike" and run_focused:
            stale_or_overdue = bool(item.get("stale")) or item.get("urgency") == "overdue"
            if not (stale_or_overdue and bike_targets_prescribed):
                deferred.append(
                    {
                        "discipline": discipline,
                        "reason": (
                            f"The A-race is a {priority_bucket} race: a maximal bike test is only worth it "
                            "when FTP is stale or overdue and the plan prescribes %FTP bike targets."
                        ),
                    }
                )
                continue
        if not test_window:
            deferred.append(
                {
                    "discipline": discipline,
                    "reason": f"{discipline.capitalize()} test is {status}, but tests only go in recovery weeks (Friel rest & test).",
                }
            )
            continue
        if readiness_status == "red":
            deferred.append({"discipline": discipline, "reason": "Readiness is red; test next recovery week."})
            continue
        if readiness_status == "yellow" and discipline == "run":
            deferred.append(
                {"discipline": discipline, "reason": "Readiness is yellow; only the lower-impact bike test is allowed."}
            )
            continue
        candidates.append((discipline, item))

    both_overdue = len(candidates) == 2 and all(
        item.get("urgency") == "overdue" for _, item in candidates
    )
    post_a_race = bool(testing.get("recent_a_race"))
    if len(candidates) == 2 and not both_overdue and not post_a_race:
        # Alternate: test the discipline whose last test is oldest; bike wins ties.
        def oldest_first(entry: tuple[str, dict[str, Any]]) -> tuple[int, str, int]:
            discipline, item = entry
            urgency_rank = {"overdue": 0, "due": 1}.get(item.get("urgency"), 2)
            return (urgency_rank, item.get("last_test_date") or "", DISCIPLINES.index(discipline))

        candidates.sort(key=oldest_first)
        later_discipline = candidates[1][0]
        deferred.append(
            {
                "discipline": later_discipline,
                "reason": "Alternating tests: this discipline gets the next recovery week.",
            }
        )
        candidates = candidates[:1]

    for discipline, item in candidates:
        scheduled.append(
            {
                "discipline": discipline,
                "protocol": item["protocol"],
                "reason": item["reason"],
                "placement": placement,
            }
        )

    if scheduled and placement == "block_week_1":
        rationale = (
            "Block week 1 opens with "
            + " and ".join(f"a {entry['discipline']} threshold test" for entry in scheduled)
            + " while the legs are fresh from the recovery week, so the block trains on correct zones."
        )
    elif scheduled:
        rationale = (
            "Friel rest & test: the recovery week ends with "
            + " and ".join(f"a {entry['discipline']} threshold test" for entry in scheduled)
            + " because it is due, so the next block trains on correct zones."
        )
        if post_a_race and len(scheduled) == 2:
            rationale += " Both disciplines are tested because this is the first recovery week after the A-race."
    elif deferred:
        rationale = "Threshold testing is deferred this week: " + " ".join(
            entry["reason"] for entry in deferred
        )
    elif recovery_week:
        rationale = (
            "Recovery week without a test: no discipline is due (each was tested less than "
            f"{MIN_INTERVAL_DAYS} days ago and no early trigger applies). A maximal test is not routine."
        )
    else:
        rationale = "Thresholds are recent enough; no test needed this week."
    return {"scheduled": scheduled, "deferred": deferred, "rationale": rationale}


def record_threshold_tests(
    state: dict[str, Any],
    completed_tests: list[dict[str, Any]],
    applied_updates: list[dict[str, Any]],
) -> dict[str, Any]:
    testing = dict(state.get("threshold_testing") or {})
    history = list(testing.get("history", []))
    applied_fields = {
        (item["discipline"], item["field"]): item for item in applied_updates
    }
    for test in completed_tests:
        discipline = test["discipline"]
        previous = testing.get(discipline) or {}
        if previous.get("last_test_date") and previous["last_test_date"] >= test["date"]:
            continue
        values = {
            field: value.get("value") for field, value in test.get("values", {}).items()
        }
        entry = {
            "last_test_date": test["date"],
            "protocol": test["protocol"],
            "source": test.get("source", "test"),
            "activity_id": test.get("activity_id"),
            "values": values,
            "applied_fields": sorted(
                field for (item_discipline, field) in applied_fields if item_discipline == discipline
            ),
        }
        testing[discipline] = entry
        history.append({"discipline": discipline, **entry})
    testing["history"] = history[-12:]
    return {**state, "threshold_testing": testing}
