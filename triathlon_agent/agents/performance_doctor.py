from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from ..training_load import effective_training_load
from ..utils import (
    NO_NIGGLE_VALUES,
    RED_FLAG_LABELS,
    detect_red_flags,
    has_niggle,
    mentions_above_neck_symptoms,
    niggle_body_parts,
    parse_date,
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
LATE_WEEK_DAYS = {"Friday", "Saturday", "Sunday"}
LATE_WEEK_TOKENS = (
    "friday",
    "saturday",
    "sunday",
    "weekend",
    "vrijdag",
    "zaterdag",
    "zondag",
)
FATIGUE_WORDS = (
    "tired",
    "fatigue",
    "fatigued",
    "exhausted",
    "drained",
    "heavy legs",
    "dead legs",
    "wiped",
    "knackered",
    "sluggish",
    "flat",
    "stale",
    "empty",
    "moe",
    "vermoeid",
    "uitgeput",
    "zware benen",
)
FATIGUE_NEGATION_RE = re.compile(
    r"\b(not|no|never|niet|geen)\s+(really\s+|at all\s+)?(tired|fatigued?|exhausted|moe|vermoeid)"
)
# Every missing core readiness signal (HRV, resting HR, sleep) costs points:
# a coach without data is less sure, not more.
MISSING_SIGNAL_PENALTY = 3
# Intervals wellness subjective fields (fatigue, soreness, stress) are 1-4:
# 1 = low, 2 = average, 3 = high, 4 = extreme; higher is worse. (mood,
# motivation and sleepQuality are inverted: 1 = best.) Values above 4 can only
# come from a 1-10 scale (older inputs, fixtures), detected per series.
INTERVALS_SUBJECTIVE_MAX = 4
SUBJECTIVE_THRESHOLDS = {
    # field: (intervals elevated, intervals high, 1-10 elevated, 1-10 high)
    "fatigue": (3, 4, 6, 8),
    "soreness": (3, 4, 4, 7),
}
SUBJECTIVE_PENALTIES = {"fatigue": (6, 9), "soreness": (3, 5)}
HIGH_LOAD_TSB = -20.0
ILLNESS_SYMPTOM_RE = re.compile(
    r"\b(cough|coughing|hoest|runny nose|snot|sore throat|keelpijn|cold|verkoud\w*|"
    r"fever|koorts|flu|griep|covid|corona|congest\w*|sinus\w*)\b"
)
STANDARD_HEALTH_LOOKBACK_DAYS = 14
EXTENDED_HEALTH_LOOKBACK_DAYS = 28
READINESS_STATUSES = {"green", "yellow", "red"}
STATUS_ALIASES = {"amber": "yellow", "orange": "yellow"}
LOAD_RECOMMENDATIONS = {"increase", "hold", "reduce"}
LOAD_ALIASES = {
    "rise": "increase",
    "progress": "increase",
    "maintain": "hold",
    "stable": "hold",
    "fall": "reduce",
    "decrease": "reduce",
}
STATUS_RANK = {"green": 0, "yellow": 1, "red": 2}


def _as_number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _optional_number(value: Any) -> float | None:
    """None for a missing / unanswered value: unknown is never read as 'fine'."""
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Check-in answers that were not given cost a little confidence, never points
# from an assumed "7/10".
UNANSWERED_CHECKIN_PENALTY = 2
# Intervals wellness stress / mood / motivation / injury are 1-4 with higher
# worse (stress 4 = extreme, mood 4 = grumpy, motivation 4 = low, injury 4 =
# injured). 3+ on the 7-day mean is flagged.
WELLNESS_FLAG_LEVEL = 3.0
WELLNESS_FIELDS = {
    "stress": "Life stress",
    "mood": "Mood",
    "motivation": "Motivation (Intervals)",
    "injury": "Injury rating",
}
WEIGHT_DROP_CONCERN_KG = -1.5
PAIN_NIGGLE_SCORE = 3
PAIN_NO_IMPACT_SCORE = 5
STOP_TRAINING_MESSAGE = (
    "Stop training and see a doctor before resuming: {labels}. "
    "Rest or easy walking only until a doctor has cleared you."
)


def _summary_value(entry: Any) -> float | None:
    """A 7-day mean from a recent_wellness_summary entry (number or dict)."""
    if isinstance(entry, dict):
        for key in ("mean_7d", "acute", "mean", "avg", "value", "change_kg", "delta_kg"):
            value = _optional_number(entry.get(key))
            if value is not None:
                return value
        return None
    return _optional_number(entry)


def _is_none_answer(text: str) -> bool:
    lowered = text.strip().lower().strip("!?.:- ")
    return lowered in NO_NIGGLE_VALUES or lowered.startswith(("no ", "geen ", "none", "nee"))


def _tighter_load_cap(current: str | None, new: str) -> str:
    order = {None: 0, "hold": 1, "reduce": 2}
    return new if order.get(new, 0) > order.get(current, 0) else (current or new)


def _red_flag_assessment(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Medical red flags plus the neck check.

    Red flags (chest pain/tightness, palpitations, disproportionate
    breathlessness, dizziness/fainting, fever, below-the-neck illness) mean
    no training and a doctor. Above-the-neck symptoms only allow easy work.
    """
    weekly = snapshot.get("weekly_checkin") or {}
    flags: list[dict[str, Any]] = []

    def add(category: str, source: str, excerpt: Any) -> None:
        if any(item["category"] == category and item["source"] == source for item in flags):
            return
        flags.append(
            {
                "category": category,
                "label": RED_FLAG_LABELS.get(category, "reported red-flag symptom"),
                "source": source,
                "excerpt": str(excerpt or "")[:160],
            }
        )

    for field in ("pain_or_niggles", "athlete_notes", "unexpected_fatigue", "last_week_summary"):
        for category in detect_red_flags(weekly.get(field)):
            add(category, f"weekly_checkin.{field}", weekly.get(field))
    explicit = weekly.get("red_flags")
    for item in explicit if isinstance(explicit, list) else []:
        text = str(item or "").strip()
        if not text or _is_none_answer(text):
            continue
        for category in detect_red_flags(text) or ["reported_red_flag"]:
            add(category, "weekly_checkin.red_flags", text)
    above_neck = False
    above_neck_sources: list[str] = []
    symptoms = weekly.get("illness_symptoms")
    symptom_items = symptoms if isinstance(symptoms, list) else [symptoms] if symptoms else []
    for item in symptom_items:
        text = str(item or "").strip()
        if not text or _is_none_answer(text):
            continue
        categories = detect_red_flags(text)
        for category in categories:
            add(category, "weekly_checkin.illness_symptoms", text)
        if not categories:
            # Unclassified symptoms are treated conservatively as above the neck.
            above_neck = True
            above_neck_sources.append(text)
    for flag in snapshot.get("activity_note_flags") or []:
        kind = flag.get("kind")
        excerpt = str(flag.get("excerpt") or "")
        source = f"activity_note {flag.get('date')} {flag.get('activity_name') or ''}".strip()
        if kind == "red_flag":
            for category in detect_red_flags(excerpt) or ["reported_red_flag"]:
                add(category, source, excerpt)
        elif kind == "illness":
            categories = detect_red_flags(excerpt)
            for category in categories:
                add(category, source, excerpt)
            if not categories:
                above_neck = True
                above_neck_sources.append(excerpt)
    niggle_text = str(weekly.get("pain_or_niggles") or "")
    if mentions_above_neck_symptoms(niggle_text):
        above_neck = True
        above_neck_sources.append(niggle_text)
    clearance = (
        "no_training_see_doctor" if flags else "easy_only" if above_neck else "normal"
    )
    return {
        "flags": flags,
        "clearance": clearance,
        "above_neck_sources": above_neck_sources,
    }


def _resolved_baseline(
    snapshot: dict[str, Any], metric_key: str, athlete_field: str
) -> tuple[dict[str, Any] | None, str]:
    derived = snapshot.get("derived_baselines", {}).get(metric_key, {})
    # The normalizer falls back to the baseline when no acute values exist;
    # that is stale data, not a reading.
    has_acute = derived.get("acute_entries") is None or int(derived["acute_entries"]) > 0
    if (
        derived.get("baseline") is not None
        and derived.get("acute") is not None
        and has_acute
        and derived.get("confidence") in {"high", "medium"}
    ):
        return derived, "intervals_derived"

    athlete = snapshot.get("athlete_profile", {})
    athlete_value = athlete.get(athlete_field)
    if athlete_value not in (None, ""):
        acute_value = None
        if metric_key == "hrv":
            acute_value = snapshot.get("avg_hrv")
        elif metric_key == "resting_hr":
            acute_value = snapshot.get("avg_resting_hr")
        elif metric_key == "sleep_hours":
            acute_value = snapshot.get("avg_sleep_hours")
        if acute_value is not None:
            baseline = float(athlete_value)
            delta = round(float(acute_value) - baseline, 2)
            delta_pct = round((delta / baseline) * 100.0, 2) if baseline else None
            return (
                {
                    "baseline": baseline,
                    "acute": round(float(acute_value), 2),
                    "delta": delta,
                    "delta_pct": delta_pct,
                    "baseline_entries": 0,
                    "acute_entries": 0,
                    "confidence": "override",
                    "source": "athlete_profile",
                },
                "athlete_profile_override",
            )

    return None, "missing"


def _mentions_days(text: str) -> list[str]:
    lowered = (text or "").lower()
    return [day for day in DAY_NAMES if day.lower() in lowered]


def _recent_activities(snapshot: dict[str, Any], days: int = 7) -> list[dict[str, Any]]:
    """Raw activities of the last `days` days up to the planning date."""
    activities = (snapshot.get("intervals") or {}).get("activities") or []
    if not activities:
        # Legacy shape: raw activities inside the latest weekly window.
        windows = snapshot.get("weekly_windows") or []
        activities = (windows[-1].get("activities") or []) if windows else []
        if activities:
            return activities
    raw_planning = snapshot.get("planning_date")
    if not raw_planning:
        return activities
    end = parse_date(str(raw_planning))
    start = end - timedelta(days=days - 1)
    selected = []
    for activity in activities:
        raw = str(activity.get("start_date_local") or "")[:10]
        if raw and start <= parse_date(raw) <= end:
            selected.append(activity)
    return selected


def _recent_week_load_distribution(snapshot: dict[str, Any]) -> dict[str, float]:
    activities = _recent_activities(snapshot)
    if not activities:
        return {"early": 0.0, "late": 0.0}
    early = 0.0
    late = 0.0
    for activity in activities:
        load = effective_training_load(activity)
        activity_day = parse_date(
            activity.get("start_date_local", "1970-01-01")
        ).strftime("%A")
        if activity_day in LATE_WEEK_DAYS:
            late += load
        else:
            early += load
    if early + late <= 0:
        return {"early": 0.0, "late": 0.0}
    total = early + late
    return {
        "early": round(early / total, 2),
        "late": round(late / total, 2),
    }


def _subjective_reading(snapshot: dict[str, Any], field: str) -> dict[str, Any]:
    """Latest Intervals wellness fatigue/soreness, scale-aware, None = missing."""
    latest = snapshot.get("latest_wellness") or {}
    value = latest.get(field)
    if value in (None, ""):
        return {"value": None, "scale": None, "severity": "missing"}
    number = _as_number(value, -1)
    if number < 0:
        return {"value": None, "scale": None, "severity": "missing"}
    series = [
        _as_number(item.get(field), 0)
        for item in (snapshot.get("recent_wellness") or [])
        if item.get(field) not in (None, "")
    ]
    ten_scale = number > INTERVALS_SUBJECTIVE_MAX or any(
        item > INTERVALS_SUBJECTIVE_MAX for item in series
    )
    elevated_i, high_i, elevated_t, high_t = SUBJECTIVE_THRESHOLDS[field]
    elevated, high = (elevated_t, high_t) if ten_scale else (elevated_i, high_i)
    severity = "high" if number >= high else "elevated" if number >= elevated else "normal"
    return {
        "value": number,
        "scale": "1-10" if ten_scale else "intervals_1_4",
        "severity": severity,
    }


def _describes_late_week_fatigue(text: str) -> bool:
    lowered = (text or "").lower()
    if not any(token in lowered for token in LATE_WEEK_TOKENS):
        return False
    if FATIGUE_NEGATION_RE.search(lowered):
        return False
    return any(word in lowered for word in FATIGUE_WORDS)


def _injury_history_text(athlete: dict[str, Any]) -> str:
    history = athlete.get("injury_history") or []
    if isinstance(history, str):
        return history
    return "; ".join(str(item) for item in history)


def _mentions_itb(text: str) -> bool:
    return "ITB" in niggle_body_parts(text)


def _readiness_confidence(
    missing_data: list[str], baseline_sources: dict[str, str]
) -> str:
    missing_signals = sum(1 for value in baseline_sources.values() if value == "missing")
    if not missing_data and all(
        value == "intervals_derived" for value in baseline_sources.values()
    ):
        return "high"
    if missing_signals >= 2 or len(missing_data) >= 2:
        return "low"
    return "medium"


def _health_event_label(event: dict[str, Any]) -> str:
    label = f"{event.get('name', event.get('kind', 'Health'))} ({event.get('start_date')}"
    if event.get("end_date") and event.get("end_date") != event.get("start_date"):
        label += f" to {event['end_date']}"
    label += ")"
    if event.get("description"):
        label += f": {event['description']}"
    return label


def _assess_health_events(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Translate recent SICK / INJURED Intervals notes into readiness guidance.

    Returns None when no health events fall inside the lookback window.
    Standard (short) events: ongoing or <= 3 days -> red, 4-7 days -> at least
    yellow and reduce, 8-14 days -> caution only; older ones are ignored.
    Extended events (>= 7 days, or covid/flu/fever): a graded return-to-training
    protocol with no intensity for the first ~7 days back and caution for 28.
    """
    events = []
    for event in snapshot.get("recent_health_events") or []:
        extended = event.get("severity") == "extended"
        limit = EXTENDED_HEALTH_LOOKBACK_DAYS if extended else STANDARD_HEALTH_LOOKBACK_DAYS
        if int(event.get("days_since_end", 99)) <= limit:
            events.append(event)
    if not events:
        return None
    latest = min(events, key=lambda item: item.get("days_since_end", 99))
    days = int(latest.get("days_since_end", 99))
    kinds = {event.get("kind") for event in events}
    extended = any(event.get("severity") == "extended" for event in events)
    label = _health_event_label(latest)
    constraints: list[str] = []
    warnings: list[str] = []
    max_quality: int | None = None

    if latest.get("ongoing") or days <= 3:
        tier = "acute"
        penalty = 45
        minimum_status = "red"
        timing = "is ongoing" if latest.get("ongoing") else f"ended {days} day(s) ago"
        finding = f"Intervals health note {timing}: {label}."
        max_quality = 0
    elif days <= 7:
        tier = "returning"
        penalty = 25 if extended else 20
        minimum_status = "yellow"
        finding = f"Intervals health note ended {days} day(s) ago: {label}."
        max_quality = 0 if extended else None
    elif extended and days <= 14:
        tier = "returning"
        penalty = 15
        minimum_status = "yellow"
        finding = f"Extended illness/injury note ended {days} day(s) ago: {label}."
        max_quality = 1
    else:
        tier = "recent"
        penalty = 8 if extended else 6
        minimum_status = None
        finding = f"Intervals health note ended {days} day(s) ago: {label}."

    if "sick" in kinds and extended and tier != "acute":
        constraints.extend(
            [
                "Extended illness return protocol (graded return to training): "
                "stage 1 (days 1-2 back) 20-30 min easy Z1 only; stage 2 (days 3-4) up to 45-60 min easy Z1-Z2; "
                "stage 3 (days 5-7) normal easy durations, strides allowed; stage 4 (week 2) first moderate session at "
                "about 70-80% of normal volume; stage 5 (weeks 3-4) rebuild intensity gradually.",
                "Advance a stage only after 24 h without symptoms, abnormal HR at easy pace or unusual fatigue; otherwise drop back one stage.",
            ]
        )
        if days <= 7:
            constraints.append("No intensity in the first 7 days back after an extended illness.")
        warnings.append(
            "Extended illness (a week or longer, or flu/covid/fever): returning too fast risks relapse and post-viral fatigue; caution stays in place for 28 days."
        )
    elif "sick" in kinds:
        if tier == "acute":
            constraints.extend(
                [
                    "No intensity until the athlete has been symptom-free for at least 48 hours.",
                    "Only rest or short easy aerobic sessions while symptoms remain; skip training entirely with fever or below-the-neck symptoms.",
                ]
            )
            warnings.append(
                "Recent illness: training through it risks prolonged illness and, with fever, cardiac complications."
            )
        elif tier == "returning":
            constraints.extend(
                [
                    "Return from illness gradually: first 2-3 days easy aerobic only, first quality session no earlier than day 4 back.",
                    "Cap total volume at roughly 70% of a normal week and drop any session where HR runs unusually high for the effort.",
                ]
            )
            warnings.append(
                "Post-illness week: watch for elevated HR at easy pace and unusual fatigue."
            )
        else:
            constraints.append(
                "Recent illness in the last two weeks: hold load rather than progressing it and watch HR drift on easy sessions."
            )
    if "injured" in kinds:
        injury_events = [event for event in events if event.get("kind") == "injured"]
        injury_label = _health_event_label(injury_events[0])
        constraints.append(
            f"Respect the injury note ({injury_label}); avoid loading the affected area and prefer low-impact alternatives."
        )
        warnings.append(
            "Injury logged in Intervals: stop any session that provokes pain rather than training through it."
        )

    markers = []
    if "sick" in kinds:
        markers.append("recent_illness")
    if "injured" in kinds:
        markers.append("recent_injury")

    return {
        "tier": tier,
        "penalty": penalty,
        "minimum_status": minimum_status,
        "finding": finding,
        "constraints": constraints,
        "warnings": warnings,
        "fatigue_markers": markers,
        "days_since_end": days,
        "events": events,
        "return_protocol": "extended" if extended else "standard",
        "max_quality_sessions": max_quality,
    }


def _positive_signals(
    snapshot: dict[str, Any],
    hrv_baseline: dict[str, Any] | None,
    rhr_baseline: dict[str, Any] | None,
    sleep_baseline: dict[str, Any] | None,
    high_load: bool = False,
) -> tuple[int, list[str]]:
    """Readiness is symmetric: strong signals earn points back, not only penalties.

    A high HRV only earns points when load is not high: above-normal HRV under
    a steep ramp or deep negative TSB can be a parasympathetic overreaching
    pattern, which the caller flags instead.
    """
    weekly = snapshot.get("weekly_checkin", {})
    training_load = snapshot.get("training_load", {})
    hrv_gate = training_load.get("hrv_gate", {})
    bonus = 0
    findings: list[str] = []

    if hrv_gate.get("status") == "above_normal" and not high_load:
        bonus += 4
        findings.append(
            f"7-day HRV {hrv_gate.get('acute_mean')} sits above its normal band {hrv_gate.get('normal_band')}."
        )
    elif (
        not high_load
        and hrv_gate.get("status") in (None, "insufficient_data")
        and hrv_baseline
        and (hrv_baseline.get("delta_pct") or 0) >= 5
    ):
        bonus += 4
        findings.append(
            f"HRV is up {hrv_baseline['delta_pct']:.1f}% against the Intervals-derived baseline."
        )
    if (
        rhr_baseline
        and rhr_baseline.get("delta") is not None
        and rhr_baseline["delta"] <= -2
    ):
        bonus += 3
        findings.append(
            f"Resting HR is {abs(rhr_baseline['delta']):.1f} bpm below baseline."
        )
    if sleep_baseline and (sleep_baseline.get("delta") or 0) >= 0.3:
        bonus += 2
        findings.append(f"Sleep is up {sleep_baseline['delta']:.2f}h versus baseline.")
    if _as_number(weekly.get("subjective_readiness"), 0) >= 8:
        bonus += 4
        findings.append(
            f"Subjective readiness is high ({weekly.get('subjective_readiness')}/10)."
        )
    if _as_number(weekly.get("current_motivation"), 0) >= 8:
        bonus += 3
        findings.append(f"Motivation is high ({weekly.get('current_motivation')}/10).")
    quality_sessions = training_load.get("quality_frequency", {}).get("sessions", [])
    felt = [item.get("feel") for item in quality_sessions if item.get("feel") is not None]
    if len(felt) >= 2 and sum(1 for value in felt if value <= 2) / len(felt) >= 0.6:
        bonus += 2
        findings.append(
            "Recent quality sessions mostly felt strong or good (Intervals feel 1-2)."
        )
    return bonus, findings


def _intensity_allowance(
    status: str,
    snapshot: dict[str, Any],
    underload_status: str,
    health: dict[str, Any] | None,
    caps: list[tuple[int, str]] | None = None,
) -> dict[str, Any]:
    """Ceiling of genuine quality sessions that readiness supports this week.

    The head coach decides the actual number. Recovery weeks are handled by the
    macro coach (threshold test only). `caps` are hard ceilings (illness return,
    non-training stress, overreaching HRV pattern) applied last.
    """
    hrv_gate = snapshot.get("training_load", {}).get("hrv_gate", {})
    gate_status = hrv_gate.get("status", "insufficient_data")
    # Stale HRV is missing data: it can never unlock extra intensity.
    extra_allowed = bool(hrv_gate.get("extra_intensity_allowed")) and gate_status != "stale"
    niggle = has_niggle(snapshot.get("weekly_checkin", {}).get("pain_or_niggles"))
    if status == "red":
        maximum = 0
        rationale = "Red readiness: no quality until the cause clears."
    elif status == "yellow":
        maximum = 2 if gate_status == "above_normal" and not niggle and not health else 1
        rationale = (
            "Yellow readiness: one quality session, two only when HRV is clearly above normal."
        )
    else:
        maximum = 2
        rationale = "Green readiness: two quality sessions are the default."
        if extra_allowed and (
            underload_status in {"detected", "watch"} or gate_status == "above_normal"
        ):
            maximum = 3
            rationale = (
                "Green readiness with HRV inside or above its normal band"
                + (
                    " and an underload pattern"
                    if underload_status in {"detected", "watch"}
                    else ""
                )
                + ": a third quality session (or a race-intensity block inside a long session) is supported."
            )
        elif gate_status == "suppressed":
            rationale = "Green overall, but HRV is suppressed: keep two quality sessions, do not add a third."
    for cap, reason in caps or []:
        if cap < maximum:
            maximum = cap
            rationale = f"{rationale} Capped at {cap}: {reason}"
    if gate_status not in {"above_normal", "normal", "suppressed", "stale", "insufficient_data"}:
        gate_status = "insufficient_data"
    return {
        "hrv_gate": gate_status,
        "extra_intensity_allowed": extra_allowed
        and status != "red"
        and not caps
        and gate_status != "stale",
        "max_quality_sessions": maximum,
        "rationale": rationale,
    }


def run(snapshot: dict[str, Any]) -> dict[str, Any]:
    latest = snapshot.get("latest_wellness", {})
    athlete = snapshot["athlete_profile"]
    weekly = snapshot["weekly_checkin"]
    prior_plan_adherence = snapshot.get("prior_plan_adherence", {})
    planning_constraints = snapshot.get("planning_constraints", {})
    athlete_memory = snapshot.get("athlete_memory", {})
    score = 82
    objective = []
    subjective = []
    warnings = []
    constraints = []
    missing_data = []
    override_reasons: list[str] = []
    fatigue_markers: list[str] = []

    hrv_baseline, hrv_source = _resolved_baseline(snapshot, "hrv", "hrv_baseline")
    rhr_baseline, rhr_source = _resolved_baseline(
        snapshot, "resting_hr", "resting_hr_baseline"
    )
    sleep_baseline, sleep_source = _resolved_baseline(
        snapshot, "sleep_hours", "typical_sleep_hours"
    )

    training_load = snapshot.get("training_load") or {}
    ramp = training_load.get("ramp") or {}
    ramp_status = ramp.get("status")
    fitness = training_load.get("fitness") or {}
    hrv_gate = training_load.get("hrv_gate") or {}
    tsb = fitness.get("tsb")
    # Status floors from objective markers / health, applied after the score.
    floors: list[tuple[str, str]] = []
    allowance_caps: list[tuple[int, str]] = []
    load_cap: str | None = None

    for source, label in (
        (hrv_source, "HRV"),
        (rhr_source, "resting HR"),
        (sleep_source, "sleep"),
    ):
        if source == "missing":
            score -= MISSING_SIGNAL_PENALTY
            missing_data.append(
                f"No usable {label} baseline or recent {label} values from Intervals: readiness is judged with less evidence (-{MISSING_SIGNAL_PENALTY})."
            )
    if hrv_gate.get("status") == "stale":
        score -= MISSING_SIGNAL_PENALTY
        missing_data.append(
            "The newest HRV reading is more than two days old: the HRV gate is stale and is not read as "
            f"'all clear' (-{MISSING_SIGNAL_PENALTY})."
        )
    energy_concern: dict[str, Any] = {"flag": False, "weight_change_kg_28d": None, "rationale": ""}
    no_impact_areas: list[str] = []

    if ramp_status == "high":
        score -= 10
        objective.extend(ramp.get("findings", []))
        warnings.append(
            "Load ramp is high (steep CTL ramp or acute:chronic spike): absorb before adding more."
        )
        fatigue_markers.append("ramp_high")
        floors.append(("yellow", "A high load ramp caps readiness at yellow."))
        load_cap = "reduce"
    elif ramp_status == "elevated":
        score -= 5
        objective.extend(ramp.get("findings", []))
        warnings.append(
            "Load ramp is elevated: hold the dose this week rather than stacking another jump."
        )
        fatigue_markers.append("ramp_elevated")
        load_cap = "hold"
    elif ramp_status == "ok":
        objective.append(
            f"Load ramp is safe (7-day load {ramp.get('acute_7d_load')} vs {ramp.get('chronic_weekly_avg_load')}/week chronic, ACWR {ramp.get('acwr')})."
        )
    else:
        change = _as_number(snapshot.get("load_change_pct"), 0)
        if change > 30:
            score -= 5
            objective.append(f"Recent load is up {change}% versus the prior week.")
        elif change > 15:
            score -= 2
            objective.append(f"Recent load is up {change}% versus the prior week.")
        else:
            objective.append(
                f"Recent load is {snapshot.get('recent_week_load')} versus {snapshot.get('prior_week_load')} the week before."
            )
    high_load = ramp_status in {"elevated", "high"} or (
        tsb is not None and _as_number(tsb) < HIGH_LOAD_TSB
    )
    low_load = (tsb is not None and _as_number(tsb) >= 0) or (
        ramp.get("acwr") is not None and _as_number(ramp.get("acwr")) < 0.9
    )

    if sleep_baseline and sleep_baseline.get("baseline") is not None:
        if sleep_baseline.get("delta", 0) <= -0.5:
            score -= 5
            objective.append(
                f"Sleep is down {abs(sleep_baseline['delta']):.2f}h versus the Intervals-derived baseline."
            )
            constraints.append(
                "Protect recovery windows and avoid unnecessary volume creep."
            )
    elif (
        snapshot.get("avg_sleep_hours") is not None
        and snapshot["avg_sleep_hours"] < 7.0
    ):
        score -= 5
        objective.append(
            f"Average sleep is only {snapshot['avg_sleep_hours']}h over the last week."
        )
        constraints.append(
            "Protect recovery windows and avoid unnecessary volume creep."
        )

    load_distribution = _recent_week_load_distribution(snapshot)
    if load_distribution["late"] >= 0.58:
        score -= 4
        objective.append(
            f"Roughly {int(round(load_distribution['late'] * 100))}% of recent load landed late in the week."
        )
        fatigue_markers.append("late_week_load_concentration")

    rhr_delta = rhr_baseline.get("delta") if rhr_baseline else None
    if rhr_delta is not None and rhr_delta >= 5:
        score -= 8
        objective.append(
            f"Resting HR is up {rhr_delta:.1f} bpm against baseline: a strong strain or illness signal."
        )
        fatigue_markers.append("resting_hr_up")
    elif rhr_delta is not None and rhr_delta >= 2:
        score -= 4
        objective.append(
            f"Resting HR is up by {rhr_delta:.1f} bpm against the Intervals-derived baseline."
        )

    hrv_down = bool(
        hrv_baseline
        and hrv_baseline.get("delta_pct") is not None
        and hrv_baseline["delta_pct"] <= -7
    )
    hrv_suppressed = hrv_gate.get("status") == "suppressed" or hrv_down
    if hrv_down:
        score -= 5
        objective.append(
            f"HRV is down {abs(hrv_baseline['delta_pct']):.1f}% against the Intervals-derived baseline."
        )
    elif hrv_gate.get("status") == "suppressed":
        score -= 5
        objective.append(
            f"7-day lnRMSSD sits below its normal band (7-day {hrv_gate.get('acute_mean')} ms vs band {hrv_gate.get('normal_band')})."
        )
    if hrv_suppressed:
        fatigue_markers.append("hrv_down")
    sleep_down = bool(
        sleep_baseline
        and sleep_baseline.get("delta") is not None
        and sleep_baseline["delta"] <= -0.5
    )

    if hrv_gate.get("status") == "above_normal" and high_load:
        fatigue_markers.append("possible_overreaching_hrv_pattern")
        warnings.append(
            "HRV sits above its normal band while load is high (steep ramp or TSB below -20): this can be a "
            "parasympathetic overreaching pattern, not freshness. No bonus and no extra quality session."
        )
        allowance_caps.append((2, "above-normal HRV under high load is not a green light for more intensity."))
    if (hrv_gate.get("status") == "suppressed" or hrv_gate.get("unstable")) and low_load:
        score -= 4
        fatigue_markers.append("non_training_stress")
        warnings.append(
            "HRV is suppressed or unstable while training load is low: suspect illness, poor sleep or life stress rather than training fatigue."
        )
        allowance_caps.append((1, "HRV disturbance without a training cause points to non-training stress."))

    # Return from a training break (holiday, illness, injury): cap the load.
    return_info = training_load.get("return_from_break") or {}
    if return_info.get("status") == "returning":
        reentry_week = return_info.get("reentry_week")
        score -= 6
        fatigue_markers.append("return_from_break")
        objective.append(
            "Returning from a training break"
            + (f" ({return_info.get('days_off')} days off" if return_info.get("days_off") else " (")
            + (f", re-entry week {reentry_week})" if reentry_week else ")")
            + (f": {return_info['rationale']}" if return_info.get("rationale") else ".")
        )
        caps_text = []
        if return_info.get("volume_cap_pct") is not None:
            caps_text.append(f"weekly volume at most {return_info['volume_cap_pct']}% of the pre-break week")
        if return_info.get("run_km_cap") is not None:
            caps_text.append(f"run km at most {return_info['run_km_cap']:g}")
        if return_info.get("long_run_cap_min") is not None:
            caps_text.append(f"long run at most {return_info['long_run_cap_min']:g} min")
        constraints.append(
            "Re-entry after a break: "
            + ("; ".join(caps_text) if caps_text else "rebuild volume gradually")
            + ". Fitness returns faster than tendons and bone: do not chase the pre-break week."
        )
        load_cap = _tighter_load_cap(
            load_cap, "reduce" if not reentry_week or int(reentry_week) <= 1 else "hold"
        )
        if not reentry_week or int(reentry_week) <= 2:
            allowance_caps.append((1, "re-entry after a training break."))

    daily_signals = training_load.get("daily_signals") or snapshot.get("daily_signals") or {}
    daily_status = daily_signals.get("status")
    if daily_status in {"warning", "alert"}:
        score -= 10 if daily_status == "alert" else 5
        fatigue_markers.append(f"daily_signals_{daily_status}")
        objective.extend(daily_signals.get("findings") or [])
        objective.append(
            f"Day-to-day early warning ({daily_status}): resting HR "
            f"{daily_signals.get('resting_hr_delta_bpm')} bpm vs baseline, HRV z {daily_signals.get('hrv_z')} "
            f"on {daily_signals.get('days_flagged', 0)} of the last 3 days."
        )
        if daily_status == "alert":
            floors.append(("red", "Resting HR and HRV both flag on most of the last 3 days."))
        else:
            floors.append(("yellow", "Resting HR or HRV flags on most of the last 3 days."))
        load_cap = _tighter_load_cap(load_cap, "reduce" if daily_status == "alert" else "hold")

    overreaching = training_load.get("overreaching") or snapshot.get("overreaching") or {}
    if overreaching.get("status") == "non_functional_risk":
        fatigue_markers.append("non_functional_overreaching_risk")
        objective.append(
            "Non-functional overreaching risk: "
            + (overreaching.get("rationale") or ", ".join(overreaching.get("markers") or []))
        )
        floors.append(("red", "Non-functional overreaching markers: a recovery week is needed now."))
        load_cap = "reduce"
        constraints.append(
            "Make this a recovery week: easy aerobic work only, volume down 40-50%, until the markers clear."
        )
        allowance_caps.append((0, "non-functional overreaching risk."))
    elif overreaching.get("status") == "functional":
        fatigue_markers.append("functional_overreaching")
        objective.append(
            "Functional overreaching (planned fatigue): "
            + (overreaching.get("rationale") or ", ".join(overreaching.get("markers") or []))
        )
        load_cap = _tighter_load_cap(load_cap, "hold")

    efficiency = training_load.get("efficiency_trend") or snapshot.get("efficiency_trend") or {}
    if efficiency.get("status") == "declining":
        score -= 2
        fatigue_markers.append("efficiency_declining")
        context_bits = [
            f"{label} {value}"
            for label, value in (
                ("decoupling", efficiency.get("decoupling_trend")),
                ("RPE vs load", efficiency.get("rpe_vs_load")),
                ("Feel (1 = strong, 5 = weak)", efficiency.get("feel_trend")),
            )
            if value not in (None, "", {}, [])
        ]
        change = efficiency.get("ef_change_pct")
        objective.append(
            "Aerobic efficiency is declining"
            + (f" ({change:+.1f}% efficiency factor)" if isinstance(change, (int, float)) else "")
            + (f"; context: {'; '.join(str(bit) for bit in context_bits)}" if context_bits else "")
            + ". Check heat, terrain and fuelling before reading it as lost fitness."
        )

    wellness_summary = snapshot.get("recent_wellness_summary") or {}
    for field, label in WELLNESS_FIELDS.items():
        value = _summary_value(wellness_summary.get(field))
        if value is None or value > INTERVALS_SUBJECTIVE_MAX:
            continue
        if value >= WELLNESS_FLAG_LEVEL:
            score -= 3
            fatigue_markers.append(f"wellness_{field}_high")
            subjective.append(
                f"{label} averaged {value:g} on the Intervals 1-4 scale (higher is worse) over the last week."
            )
            if field == "injury":
                warnings.append(
                    f"Intervals wellness injury rating is {value:g}/4: treat it as a niggle and avoid loading the sore area."
                )
    weight_change = _summary_value(wellness_summary.get("weight_trend_kg_28d"))
    if weight_change is not None:
        energy_concern["weight_change_kg_28d"] = round(weight_change, 1)
        if weight_change <= WEIGHT_DROP_CONCERN_KG:
            score -= 3
            fatigue_markers.append("weight_drop")
            energy_concern["flag"] = True
            energy_concern["rationale"] = (
                f"Body weight dropped {abs(weight_change):.1f} kg in 28 days: possible low energy "
                "availability; fuel the training instead of dieting through it."
            )
            warnings.append(energy_concern["rationale"])

    sleep_answer = _optional_number(weekly.get("sleep_quality"))
    if sleep_answer is not None and sleep_answer <= 2:
        score -= 3
        subjective.append(f"Subjective sleep quality is {sleep_answer:g}/5 (5 = great).")
        fatigue_markers.append("poor_sleep_reported")
    work_stress = _optional_number(weekly.get("work_stress"))
    if work_stress is not None and work_stress >= 6:
        score -= 4 if work_stress >= 8 else 2
        subjective.append(f"Work and life stress is {work_stress:g}/10 this week.")
        fatigue_markers.append("work_stress_high")
        if work_stress >= 8:
            constraints.append("High life stress: protect sleep and keep the week simple; stress is stress.")

    # Objective red: several independent physiological alarms at once.
    if hrv_suppressed and rhr_delta is not None and rhr_delta >= 5 and sleep_down:
        floors.append(("red", "HRV suppressed, resting HR up 5+ bpm and sleep down together."))
    if ramp_status == "high" and hrv_suppressed:
        floors.append(("red", "A high load ramp with suppressed HRV: non-functional overreaching risk."))

    sleep_quality = (snapshot.get("recent_wellness_summary") or {}).get("sleep_quality_avg")
    if sleep_quality is not None and _as_number(sleep_quality) >= 3.0:
        score -= 3
        objective.append(
            f"Sleep quality averaged {sleep_quality} on the Intervals 1-4 scale (1 = great, 4 = poor)."
        )

    for field, marker, message in (
        ("fatigue", "latest_fatigue_high", "Fatigue is elevated in the latest wellness entry"),
        ("soreness", "latest_soreness_high", "Soreness is present and worth monitoring"),
    ):
        reading = _subjective_reading(snapshot, field)
        if reading["severity"] in {"elevated", "high"}:
            elevated_penalty, high_penalty = SUBJECTIVE_PENALTIES[field]
            score -= high_penalty if reading["severity"] == "high" else elevated_penalty
            scale = "Intervals 1-4" if reading["scale"] == "intervals_1_4" else "1-10"
            subjective.append(f"{message} ({reading['value']:g} on the {scale} scale).")
            fatigue_markers.append(marker)

    niggle_text = str(weekly.get("pain_or_niggles") or "").strip()
    injury_history = _injury_history_text(athlete)
    niggle = has_niggle(niggle_text)
    niggle_parts = niggle_body_parts(niggle_text) if niggle else []
    symptoms = niggle and bool(ILLNESS_SYMPTOM_RE.search(niggle_text.lower()))
    if symptoms and not niggle_parts:
        score -= 4
        subjective.append(f"Reported symptoms: {niggle_text}")
        warnings.append(
            f"Residual illness symptoms reported ({niggle_text}): above-the-neck only allows easy aerobic work; "
            "chest symptoms, fever or a raised resting HR mean rest."
        )
        fatigue_markers.append("residual_illness_symptoms")
    elif niggle:
        score -= 4
        subjective.append(f"Reported niggle: {niggle_text}")
        part = ", ".join(niggle_parts) if niggle_parts else "reported area"
        warnings.append(
            f"Monitor the {part} ({niggle_text}) during and after the first run sessions; stop and swap to low-impact work if it sharpens."
        )
        constraints.append("Avoid stacking hard run load with heavy strength work.")
        fatigue_markers.append("pain_or_niggles")

    # Structured pain scores (0-10 per body part) and pain in activity notes.
    extra_niggles: list[str] = []
    pain_scores = weekly.get("pain_scores") if isinstance(weekly.get("pain_scores"), dict) else {}
    for part, raw_score in pain_scores.items():
        pain = _optional_number(raw_score)
        if pain is None or pain < PAIN_NIGGLE_SCORE:
            continue
        if part not in niggle_parts:
            niggle_parts.append(str(part))
            extra_niggles.append(f"{part} {pain:g}/10")
        if pain >= PAIN_NO_IMPACT_SCORE:
            no_impact_areas.append(str(part))
    for flag in snapshot.get("activity_note_flags") or []:
        if flag.get("kind") != "pain":
            continue
        part = flag.get("body_part") or "unspecified area"
        if part not in niggle_parts:
            niggle_parts.append(str(part))
            extra_niggles.append(
                f"{part} (activity note {flag.get('date')}: \"{str(flag.get('excerpt') or '')[:80]}\")"
            )
    if extra_niggles:
        if not niggle:
            score -= 4
            fatigue_markers.append("pain_or_niggles")
            constraints.append("Avoid stacking hard run load with heavy strength work.")
        niggle = True
        subjective.append(f"Reported pain: {'; '.join(extra_niggles)}.")
        warnings.append(
            f"Monitor the {', '.join(niggle_parts)} during and after the first sessions; stop and swap to "
            "low-impact work if it sharpens."
        )
    if no_impact_areas:
        score -= 6
        floors.append(("yellow", "Pain of 5/10 or more on a body part caps readiness at yellow."))
        constraints.append(
            f"Pain 5/10 or more on {', '.join(no_impact_areas)}: no impact loading on that area this week "
            "(no running if it is a leg or foot); use bike, swim or strength that does not provoke it."
        )
        fatigue_markers.append("pain_no_impact")
    run_flags = set((training_load.get("run_load") or {}).get("flags", []))
    if run_flags & {"descent_jump", "run_km_jump", "long_run_jump"}:
        objective.extend((training_load.get("run_load") or {}).get("findings", []))
        if _mentions_itb(injury_history) or "ITB" in niggle_parts:
            score -= 3
            warnings.append(
                "Run volume or descent jumped while ITB history is present: cap downhill running, keep descents controlled "
                "and short, and hold run km flat this week."
            )
            constraints.append(
                "ITB protection: no long technical descents and no run-km increase until the jump has been absorbed."
            )
            fatigue_markers.append("itb_risk_run_load_jump")
            if load_cap is None:
                load_cap = "hold"
    if weekly.get("missed_sessions"):
        subjective.append(
            "A missed session last week suggests recovery or life stress was already biting."
        )
    if weekly.get("unexpected_fatigue"):
        subjective.append(weekly["unexpected_fatigue"])

    if _describes_late_week_fatigue(weekly.get("unexpected_fatigue", "")):
        score -= 4
        subjective.append(
            "Fatigue clustered late in the week, which is a classic sign that the stress pattern may already be outrunning recovery."
        )
        fatigue_markers.append("late_week_subjective_fatigue")

    readiness_answer = _optional_number(weekly.get("subjective_readiness"))
    motivation_answer = _optional_number(weekly.get("current_motivation"))
    for answer, label in (
        (readiness_answer, "subjective readiness"),
        (motivation_answer, "motivation"),
    ):
        if answer is None:
            score -= UNANSWERED_CHECKIN_PENALTY
            missing_data.append(
                f"Check-in {label} was not answered: treated as unknown, not as fine "
                f"(-{UNANSWERED_CHECKIN_PENALTY})."
            )
    if readiness_answer is not None and readiness_answer <= 6:
        score -= 4
        subjective.append(
            f"Subjective readiness is only {weekly.get('subjective_readiness')}/10."
        )
        if readiness_answer <= 5:
            override_reasons.append(
                "Subjective readiness is poor enough that it should influence the week even if objective markers look acceptable."
            )
    if motivation_answer is not None and motivation_answer <= 6:
        score -= 3
        subjective.append(
            f"Motivation is only {weekly.get('current_motivation')}/10, which usually means more friction is already present than the metrics show."
        )
        if motivation_answer <= 5:
            override_reasons.append(
                "Motivation is low enough that the week should favor execution over theoretical optimal load."
            )

    if prior_plan_adherence.get("status") == "available":
        adherence_score = float(prior_plan_adherence.get("adherence_score", 1.0) or 1.0)
        objective.append(prior_plan_adherence["summary"])
        if adherence_score < 0.75:
            score -= 5
            constraints.append(
                "Simplify the week until planned work and real life line up again."
            )
        if prior_plan_adherence.get(
            "key_sessions_completed", 0
        ) < prior_plan_adherence.get("key_sessions_planned", 0):
            score -= 4
            subjective.append(
                "At least one prior key session was missed, moved, or notably shortened."
            )
            fatigue_markers.append("key_sessions_not_landing")

    if planning_constraints.get("requested_week_type") == "recovery":
        subjective.append("The athlete explicitly asked for a recovery-style week.")
        constraints.append(
            "Honor the recovery-week request unless live readiness is clearly exceptional."
        )
        override_reasons.append("Athlete explicitly requested a recovery-style week.")

    if athlete_memory.get("status") == "available":
        objective.append(athlete_memory["summary"])
        if athlete_memory.get("fragile_days"):
            constraints.append(
                f"Treat {', '.join(athlete_memory['fragile_days'])} as fragile execution days unless the plan has a strong reason to spend stress there."
            )
        if athlete_memory.get("double_day_profile", {}).get("status") == "fragile":
            score -= 3
            fatigue_markers.append("double_days_fragile")
            constraints.append(
                "Recent history suggests double days are the first place the week starts to break down."
            )

    bonus, positive = _positive_signals(
        snapshot, hrv_baseline, rhr_baseline, sleep_baseline, high_load=high_load
    )
    score = min(100, score + bonus)
    if symptoms and not niggle_parts:
        risk_focus = f"lingering illness symptoms ({niggle_text})"
    elif niggle:
        risk_focus = f"the reported {', '.join(niggle_parts) or 'niggle'}" + (
            f" ({niggle_text})" if niggle_text else ""
        )
    elif injury_history:
        risk_focus = f"a flare of the known {injury_history}"
    else:
        risk_focus = None

    health = _assess_health_events(snapshot)
    if health:
        score -= health["penalty"]
        objective.append(health["finding"])
        warnings.extend(health["warnings"])
        fatigue_markers.extend(health["fatigue_markers"])

    status = "green"
    load_direction = "increase"
    main_risk = "low short-term risk"
    if score < 35:
        status = "red"
        load_direction = "reduce"
        main_risk = "non-functional fatigue" + (
            f" and escalation of {risk_focus}" if risk_focus else ""
        )
        constraints.extend(
            [
                "Use only minimum effective structure.",
                "Replace quality running with controlled aerobic work if symptoms climb.",
            ]
        )
    elif score < 70:
        status = "yellow"
        load_direction = "hold"
        main_risk = "accumulated fatigue" + (
            f" plus {risk_focus}" if risk_focus else ""
        ) + " if intensity stacks poorly"
        constraints.extend(
            [
                "Keep quality within the intensity allowance and protect the session that matters most.",
                "Hold total load flat to slightly down.",
            ]
        )
    else:
        objective.append("Readiness markers support normal progression.")

    if status == "green" and (
        (readiness_answer is not None and readiness_answer <= 5)
        or (motivation_answer is not None and motivation_answer <= 5)
    ):
        status = "yellow"
        load_direction = "hold"
        main_risk = (
            "subjective recovery is lagging even if objective markers are acceptable"
        )
        override_reasons.append(
            "Subjective readiness is worse than the objective markers alone would suggest."
        )
        constraints.append(
            "Keep the week executable and avoid chasing a green-light score that the athlete does not actually feel."
        )

    if health:
        if health["minimum_status"] == "red" and status != "red":
            status = "red"
            load_direction = "reduce"
            override_reasons.append(
                "A recent sick or injured note in Intervals overrides the readiness score."
            )
        elif health["minimum_status"] == "yellow":
            if status == "green":
                status = "yellow"
                override_reasons.append(
                    "A sick or injured note from the past week caps readiness at yellow."
                )
            load_direction = "reduce" if status != "green" else load_direction
        elif status == "green":
            load_direction = "hold"
        if health["tier"] in {"acute", "returning"}:
            main_risk = (
                "relapse or prolonged illness from returning to load too quickly"
                if "recent_illness" in health["fatigue_markers"]
                else "injury aggravation from loading the affected area too soon"
            )
        constraints = health["constraints"] + constraints
        if health.get("max_quality_sessions") is not None:
            allowance_caps.append(
                (
                    int(health["max_quality_sessions"]),
                    f"{health.get('return_protocol', 'standard')} return from a recent health note.",
                )
            )

    for floor, reason in floors:
        if STATUS_RANK[floor] > STATUS_RANK[status]:
            status = floor
            override_reasons.append(reason)
            if floor == "red":
                load_direction = "reduce"
                main_risk = "non-functional overreaching from stacking load on a strained system"
            elif load_direction == "increase":
                load_direction = "hold"

    underload = snapshot.get("training_load", {}).get("underload") or {
        "status": "not_detected",
        "markers": [],
        "recommendation": "No training-load assessment was available.",
    }
    underload_status = underload.get("status", "not_detected")
    if underload_status == "detected":
        warnings.append(
            "Underload detected: the recent dose is too small to drive adaptation. "
            + underload.get("recommendation", "")
        )
        if status == "green":
            load_direction = "increase"
            main_risk = "under-stimulation: fitness stalls or declines if the week repeats the recent dose"
            constraints.append(
                "Progress this week: add quality or extend key-session time-in-zone rather than repeating last week."
            )
        elif status == "yellow" and not health:
            override_reasons.append(
                "Underload markers are present; hold rather than reduce, and resume progression as soon as readiness is green."
            )
    elif underload_status == "watch" and status == "green":
        constraints.append(
            "Two underload markers: progress the key sessions one rung instead of repeating last week's dose."
        )
    elif underload_status == "explained_physiological":
        warnings.append(
            "The low recent dose is explained by physiology (suppressed HRV, raised resting HR or high "
            "fatigue): this is the body asking for less, not an invitation to add load."
        )
        load_cap = _tighter_load_cap(load_cap, "hold")
    if underload.get("ftp_stale_load_undercounted"):
        objective.append(
            "FTP looks stale, so Intervals undercounts bike load: the underload read is less certain."
        )

    # Medical red flags and the neck check override everything above.
    red_flag_info = _red_flag_assessment(snapshot)
    clearance = red_flag_info["clearance"]
    if clearance == "no_training_see_doctor":
        labels = ", ".join(
            dict.fromkeys(flag["label"] for flag in red_flag_info["flags"])
        )
        stop_message = STOP_TRAINING_MESSAGE.format(labels=labels)
        score = min(score, 20)
        status = "red"
        load_direction = "reduce"
        load_cap = "reduce"
        main_risk = f"medical red flag ({labels}): training must stop until a doctor has cleared it"
        subjective.insert(0, stop_message)
        warnings.insert(0, stop_message)
        constraints = [
            stop_message,
            "No structured training, no intensity and no long sessions this week.",
            *constraints,
        ]
        override_reasons.append("Medical red flag reported: no training until cleared by a doctor.")
        fatigue_markers.append("red_flag")
        allowance_caps.append((0, "medical red flag: no training until cleared by a doctor."))
    elif clearance == "easy_only":
        fatigue_markers.append("above_neck_symptoms")
        constraints.insert(
            0,
            "Neck check: above-the-neck symptoms only allow short easy aerobic sessions, no intensity. "
            "If symptoms move below the neck or a fever appears, stop training and see a doctor.",
        )
        if status == "green":
            status = "yellow"
            override_reasons.append("Above-the-neck illness symptoms cap readiness at yellow.")
        load_cap = _tighter_load_cap(load_cap, "hold")
        allowance_caps.append((0, "above-the-neck illness symptoms: easy aerobic only."))
    if load_cap == "reduce" and load_direction != "reduce":
        load_direction = "reduce"
    elif load_cap == "hold" and load_direction == "increase":
        load_direction = "hold"
    if load_cap and main_risk in {"low short-term risk"} | {
        "under-stimulation: fitness stalls or declines if the week repeats the recent dose"
    }:
        main_risk = (
            "ITB flare from the recent jump in run volume or descent"
            if "itb_risk_run_load_jump" in fatigue_markers and ramp_status not in {"elevated", "high"}
            else "load ramping faster than the body can absorb (injury or overreaching risk)"
        )
    intensity_allowance = _intensity_allowance(
        status, snapshot, underload_status, health, allowance_caps
    )
    status = STATUS_ALIASES.get(str(status).lower(), str(status).lower())
    if status not in READINESS_STATUSES:
        status = "yellow"
    load_direction = LOAD_ALIASES.get(str(load_direction).lower(), str(load_direction).lower())
    if load_direction not in LOAD_RECOMMENDATIONS:
        load_direction = "hold"

    baseline_sources = {
        "hrv": hrv_source,
        "resting_hr": rhr_source,
        "sleep_hours": sleep_source,
    }
    readiness_confidence = _readiness_confidence(missing_data, baseline_sources)

    return {
        "agent": "performance_doctor",
        "readiness_status": status,
        "readiness_score": max(score, 0),
        "objective_findings": objective,
        "subjective_findings": subjective,
        "positive_findings": positive,
        "main_risk": main_risk,
        "constraints": list(dict.fromkeys(constraints)),
        "load_recommendation": load_direction,
        "warnings": warnings,
        "baseline_sources": baseline_sources,
        "fatigue_markers": fatigue_markers,
        "override_reasons": override_reasons,
        "readiness_confidence": readiness_confidence,
        "health_events": {
            "status": health["tier"] if health else "none",
            "days_since_end": health["days_since_end"] if health else None,
            "events": health["events"] if health else [],
            "return_protocol": health["return_protocol"] if health else None,
        },
        "ramp_status": ramp_status or "unknown",
        "underload": {
            "status": underload_status,
            "markers": underload.get("markers", []),
            "recommendation": underload.get("recommendation", ""),
        },
        "intensity_allowance": intensity_allowance,
        "missing_data": missing_data,
        "red_flags": red_flag_info["flags"],
        "training_clearance": clearance,
        "no_impact_areas": no_impact_areas,
        "energy_availability_concern": energy_concern,
        "return_from_break": training_load.get("return_from_break")
        or {"status": "none"},
    }
