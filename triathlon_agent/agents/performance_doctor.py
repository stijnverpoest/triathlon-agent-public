from __future__ import annotations

from typing import Any

from ..utils import parse_date


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


def _as_number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _resolved_baseline(
    snapshot: dict[str, Any], metric_key: str, athlete_field: str
) -> tuple[dict[str, Any] | None, str]:
    derived = snapshot.get("derived_baselines", {}).get(metric_key, {})
    if (
        derived.get("baseline") is not None
        and derived.get("acute") is not None
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


def _recent_week_load_distribution(snapshot: dict[str, Any]) -> dict[str, float]:
    weekly_windows = snapshot.get("weekly_windows", [])
    if not weekly_windows:
        return {"early": 0.0, "late": 0.0}
    recent_window = weekly_windows[-1]
    activities = recent_window.get("activities", [])
    if not activities:
        return {"early": 0.0, "late": 0.0}
    early = 0.0
    late = 0.0
    for activity in activities:
        load = _as_number(activity.get("icu_training_load"))
        activity_day = parse_date(
            activity.get("start_date_local", "1970-01-01")
        ).strftime("%A")
        if activity_day in LATE_WEEK_DAYS:
            late += load
        else:
            early += load
    total = max(early + late, 1.0)
    return {
        "early": round(early / total, 2),
        "late": round(late / total, 2),
    }


def _readiness_confidence(
    missing_data: list[str], baseline_sources: dict[str, str]
) -> str:
    if not missing_data and all(
        value == "intervals_derived" for value in baseline_sources.values()
    ):
        return "high"
    if len(missing_data) <= 1:
        return "medium"
    return "low"


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

    if hrv_source == "missing":
        missing_data.append(
            "No usable HRV baseline could be derived from Intervals yet."
        )
    if rhr_source == "missing":
        missing_data.append(
            "No usable resting HR baseline could be derived from Intervals yet."
        )
    if sleep_source == "missing":
        missing_data.append(
            "No usable sleep baseline could be derived from Intervals yet."
        )

    if snapshot.get("load_change_pct", 0) > 8:
        score -= 6
        objective.append(
            f"Recent load is up {snapshot['load_change_pct']}% versus the prior week."
        )
    else:
        objective.append(
            f"Recent load is stable at {snapshot['recent_week_load']} load versus {snapshot['prior_week_load']} last week."
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

    if (
        rhr_baseline
        and rhr_baseline.get("delta") is not None
        and rhr_baseline["delta"] >= 2
    ):
        score -= 4
        objective.append(
            f"Resting HR is up by {rhr_baseline['delta']:.1f} bpm against the Intervals-derived baseline."
        )

    if (
        hrv_baseline
        and hrv_baseline.get("delta_pct") is not None
        and hrv_baseline["delta_pct"] <= -7
    ):
        score -= 5
        objective.append(
            f"HRV is down {abs(hrv_baseline['delta_pct']):.1f}% against the Intervals-derived baseline."
        )
        fatigue_markers.append("hrv_down")

    if _as_number(latest.get("fatigue")) >= 6:
        score -= 6
        subjective.append("Fatigue is elevated in the latest wellness entries.")
        fatigue_markers.append("latest_fatigue_high")
    if _as_number(latest.get("soreness")) >= 4:
        score -= 3
        subjective.append("Soreness is present and worth monitoring.")
        fatigue_markers.append("latest_soreness_high")
    if weekly.get("pain_or_niggles"):
        score -= 4
        subjective.append(weekly["pain_or_niggles"])
        warnings.append(
            "Monitor calf feedback during and after the first few run sessions."
        )
        constraints.append("Avoid stacking hard run load with heavy strength work.")
        fatigue_markers.append("pain_or_niggles")
    if weekly.get("missed_sessions"):
        subjective.append(
            "A missed session last week suggests recovery or life stress was already biting."
        )
    if weekly.get("unexpected_fatigue"):
        subjective.append(weekly["unexpected_fatigue"])

    late_week_mentions = set(
        _mentions_days(weekly.get("last_week_summary", ""))
        + _mentions_days(weekly.get("unexpected_fatigue", ""))
    )
    if late_week_mentions & LATE_WEEK_DAYS:
        score -= 4
        subjective.append(
            "Fatigue clustered late in the week, which is a classic sign that the stress pattern may already be outrunning recovery."
        )
        fatigue_markers.append("late_week_subjective_fatigue")

    if _as_number(weekly.get("subjective_readiness"), 7) <= 6:
        score -= 4
        subjective.append(
            f"Subjective readiness is only {weekly.get('subjective_readiness')}/10."
        )
        if _as_number(weekly.get("subjective_readiness"), 7) <= 5:
            override_reasons.append(
                "Subjective readiness is poor enough that it should influence the week even if objective markers look acceptable."
            )
    if _as_number(weekly.get("current_motivation"), 7) <= 6:
        score -= 3
        subjective.append(
            f"Motivation is only {weekly.get('current_motivation')}/10, which usually means more friction is already present than the metrics show."
        )
        if _as_number(weekly.get("current_motivation"), 7) <= 5:
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

    status = "green"
    load_direction = "increase"
    main_risk = "low short-term risk"
    if score < 35:
        status = "red"
        load_direction = "reduce"
        main_risk = "non-functional fatigue and niggle escalation"
        constraints.extend(
            [
                "Use only minimum effective structure.",
                "Replace quality running with controlled aerobic work if symptoms climb.",
            ]
        )
    elif score < 70:
        status = "yellow"
        load_direction = "hold"
        main_risk = (
            "accumulated run fatigue plus calf tightness if intensity stacks poorly"
        )
        constraints.extend(
            [
                "Keep only one true quality run.",
                "Hold total load flat to slightly down.",
            ]
        )
    else:
        objective.append("Readiness markers support normal progression.")

    if status == "green" and (
        _as_number(weekly.get("subjective_readiness"), 7) <= 5
        or _as_number(weekly.get("current_motivation"), 7) <= 5
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
        "main_risk": main_risk,
        "constraints": list(dict.fromkeys(constraints)),
        "load_recommendation": load_direction,
        "warnings": warnings,
        "baseline_sources": baseline_sources,
        "fatigue_markers": fatigue_markers,
        "override_reasons": override_reasons,
        "readiness_confidence": readiness_confidence,
        "missing_data": missing_data,
    }
