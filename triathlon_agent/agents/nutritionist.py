from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..race_selection import race_bucket


# Carbohydrate ladder (g/h) by session length: (upper bound in minutes, range).
CARB_LADDER: tuple[tuple[float, tuple[int, int]], ...] = (
    (60, (0, 0)),
    (90, (30, 45)),
    (150, (45, 60)),
    (float("inf"), (60, 90)),
)
# Events where the gut must be trained toward 60-90 g/h on long sessions.
GUT_TRAINING_BUCKETS = {"trail", "ultra_trail", "70.3", "ironman", "marathon"}
HEAT_NOTE = (
    "In heat (>25 C) raise fluids to 600-900 ml/h and sodium to 500-1000 mg/h."
)


def carb_range_g_per_h(duration_min: float, *, hard: bool = False) -> tuple[int, int]:
    """Carbohydrate per hour for a session: none <60', 30-45 to 90', 45-60 to 150', 60-90 beyond.

    Hard sessions of an hour or more move one step up the ladder.
    """
    index = next(
        position
        for position, (limit, _) in enumerate(CARB_LADDER)
        if float(duration_min or 0) < limit
    )
    if hard and float(duration_min or 0) >= 60:
        index = min(index + 1, len(CARB_LADDER) - 1)
    return CARB_LADDER[index][1]


def _carb_text(duration: int, hard: bool) -> str:
    low, high = carb_range_g_per_h(duration, hard=hard)
    if not high:
        return "No carbohydrate needed during; water to thirst."
    return f"Aim for {low}-{high}g carbohydrate/hour, starting in the first 30 minutes"


def _gut_training_note(bucket: str, duration: int) -> str:
    if bucket not in GUT_TRAINING_BUCKETS or duration < 90:
        return ""
    return (
        " Gut training: use race-day products and add 5-10 g/h per week on long sessions "
        "toward 75-90 g/h by the race-specific block."
    )


def _weight_kg(snapshot: dict[str, Any]) -> float:
    body_metrics = snapshot.get("athlete_profile", {}).get("body_metrics", {})
    value = body_metrics.get("weight_kg")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 75.0


def _protein_target_g(weight_kg: float) -> int:
    return int(round(max(25.0, min(35.0, weight_kg * 0.35))))


def _contains_hydration_flag(text: str) -> bool:
    lowered = text.lower()
    return any(
        token in lowered
        for token in ["hydration", "dehydrat", "thirst", "fluid", "cramp"]
    )


def _hydration_flag(snapshot: dict[str, Any]) -> bool:
    weekly = snapshot.get("weekly_checkin", {})
    fields = [
        weekly.get("last_week_summary", ""),
        weekly.get("unexpected_fatigue", ""),
        weekly.get("athlete_notes", ""),
    ]
    return any(_contains_hydration_flag(value) for value in fields)


def _sleep_pressure(snapshot: dict[str, Any]) -> bool:
    sleep = snapshot.get("derived_baselines", {}).get("sleep_hours", {})
    delta = sleep.get("delta")
    return delta is not None and float(delta) <= -0.3


WEIGHT_DROP_CONCERN_KG = -1.5
POST_ILLNESS_NOTE = (
    "Post-illness fuelling: eat to appetite plus a little more, keep carbohydrate around every session, "
    "25-30 g protein per meal, drink well (add electrolytes after fever or stomach upset) and never "
    "train fasted while recovering."
)
RETURN_FROM_BREAK_NOTE = (
    "Rebuild week: do not combine the return to training with a diet; match intake to the rising "
    "load so tendons and bone adapt alongside fitness."
)
LOW_ENERGY_NOTE = (
    "Weight is trending down: add a carbohydrate-rich snack around sessions and an extra recovery "
    "meal on long days; raise it with a sports dietitian or doctor if the drop continues."
)


def _weight_change_kg(
    snapshot: dict[str, Any], performance: dict[str, Any]
) -> float | None:
    concern = performance.get("energy_availability_concern") or {}
    value = concern.get("weight_change_kg_28d")
    if value is None:
        value = (snapshot.get("recent_wellness_summary") or {}).get("weight_trend_kg_28d")
        if isinstance(value, dict):
            value = next(
                (value.get(key) for key in ("change_kg", "delta_kg", "value") if value.get(key) is not None),
                None,
            )
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _returning_from_break(snapshot: dict[str, Any], performance: dict[str, Any]) -> bool:
    info = performance.get("return_from_break") or (
        (snapshot.get("training_load") or {}).get("return_from_break") or {}
    )
    return info.get("status") == "returning"


def _post_illness(snapshot: dict[str, Any], performance: dict[str, Any]) -> bool:
    health = performance.get("health_events") or {}
    if health.get("status") in {"acute", "returning", "recent"} and any(
        event.get("kind") == "sick" for event in health.get("events") or []
    ):
        return True
    if "residual_illness_symptoms" in (performance.get("fatigue_markers") or []):
        return True
    if performance.get("training_clearance") in {"easy_only", "no_training_see_doctor"}:
        return True
    symptoms = (snapshot.get("weekly_checkin") or {}).get("illness_symptoms")
    return bool(isinstance(symptoms, list) and any(str(item).strip() for item in symptoms))


def _energy_availability_risk(
    snapshot: dict[str, Any], performance: dict[str, Any]
) -> dict[str, str]:
    risk = "low"
    reasons: list[str] = []
    if performance.get("readiness_status") != "green":
        risk = "moderate"
        reasons.append("readiness is below green, so recovery fueling matters more")
    if _sleep_pressure(snapshot):
        risk = "moderate"
        reasons.append(
            "sleep is below baseline, which can reduce appetite and recovery quality"
        )
    weekly = snapshot.get("weekly_checkin", {})
    if (
        weekly.get("available_hours_override_min")
        and weekly["available_hours_override_min"] < 480
    ):
        risk = "moderate"
        reasons.append(
            "compressed training time can tempt underfueling around key sessions"
        )
    if _hydration_flag(snapshot):
        reasons.append(
            "hydration was flagged last week and should be cleaned up intentionally"
        )
    weight_change = _weight_change_kg(snapshot, performance)
    concern = performance.get("energy_availability_concern") or {}
    if concern.get("flag") or (
        weight_change is not None and weight_change <= WEIGHT_DROP_CONCERN_KG
    ):
        risk = "high"
        reasons.append(
            f"body weight dropped {abs(weight_change or 0):.1f} kg in 28 days, a low-energy-availability warning sign"
        )
    if _returning_from_break(snapshot, performance):
        if risk == "low":
            risk = "moderate"
        reasons.append(
            "the athlete is rebuilding after a break, when under-fuelling slows the return and raises injury risk"
        )
    if _post_illness(snapshot, performance):
        if risk == "low":
            risk = "moderate"
        reasons.append("recent illness raises energy and protein needs while the body recovers")
    if not reasons:
        reasons.append(
            "current readiness and weekly structure do not suggest a major low-energy-availability risk"
        )
    return {"status": risk, "rationale": "; ".join(reasons)}


def _is_quality_run(session: dict[str, Any]) -> bool:
    return session.get("discipline") == "Run" and session.get("daniels_type") in {
        "T",
        "I",
        "R",
    }


def _is_long_session(session: dict[str, Any]) -> bool:
    return int(session.get("duration_min", 0)) >= 75


def _is_hard_ride(session: dict[str, Any]) -> bool:
    return session.get("discipline") == "Ride" and (
        session.get("coggan_level") in {"L4 Threshold", "L5 VO2max", "L6 Anaerobic"}
        or float(session.get("expected_if", 0) or 0) >= 0.8
        or (
            session.get("priority") == "key"
            and "endurance" not in str(session.get("session_name", "")).lower()
        )
    )


def _format_range(low: int, high: int, unit: str) -> str:
    return f"{low}-{high}{unit}"


def _session_fueling(
    session: dict[str, Any],
    weight_kg: float,
    readiness_status: str,
    hydration_flag: bool,
    bucket: str = "unknown",
) -> dict[str, Any]:
    protein_target = _protein_target_g(weight_kg)
    discipline = session["discipline"]
    duration = int(session.get("duration_min", 0))
    primary_metric = session.get("primary_metric", "notes")
    reason_codes: list[str] = []
    gut_note = _gut_training_note(bucket, duration)

    if discipline == "Ride":
        hard = float(session.get("expected_if", 0) or 0) >= 0.8
        carb_low, carb_high = carb_range_g_per_h(duration, hard=hard)
        fluid_low = 500
        fluid_high = 700
        sodium_low = 400
        sodium_high = 700
        if duration >= 120 or hard:
            fluid_low = 600 if hydration_flag else 500
            fluid_high = 850 if hydration_flag else 750
            sodium_low = 500
            sodium_high = 900 if hydration_flag else 800
            reason_codes.extend(["long_or_hard_ride", "carbohydrate_priority"])
        pre = f"Eat roughly {_format_range(int(weight_kg * 0.8), int(weight_kg * 1.1), 'g')} of carbohydrate in the 2-3h before the ride and top up with a small snack if needed."
        carbs = (
            f"Aim for {_format_range(carb_low, carb_high, 'g')} carbohydrate/hour"
            if carb_high
            else "No carbohydrate needed during"
        )
        during = (
            f"{carbs}, {_format_range(fluid_low, fluid_high, 'ml')} fluid/hour, and "
            f"{_format_range(sodium_low, sodium_high, 'mg')} sodium/hour; start early rather than chasing later. "
            f"{HEAT_NOTE}{gut_note}"
        )
        post = f"Take {protein_target}-{protein_target + 5}g protein plus a carb-rich meal within 60 minutes."
        hydration_focus = "high" if duration >= 120 or hydration_flag else "moderate"
        return {
            "date": session["date"],
            "session_name": session["session_name"],
            "discipline": discipline,
            "priority": "Fuel the ride as real work, not as a depleted grind.",
            "guidance": {"pre": pre, "during": during, "post": post},
            "hydration_focus": hydration_focus,
            "reason_codes": reason_codes or ["ride_support"],
        }

    if discipline == "Run" and primary_metric == "Pace":
        pre = f"Take {_format_range(int(weight_kg * 0.7), int(weight_kg * 1.0), 'g')} carbohydrate in the 2-3h before the session so threshold or interval pace stays economical."
        if duration >= 60:
            during = f"{_carb_text(duration, True)}; water to thirst."
        else:
            during = "Water is usually enough during the run itself; add a small carbohydrate top-up only if you start the session later in the day or already depleted."
        post = f"Recover with carbohydrate plus {protein_target}g protein within an hour, especially if another session follows within 24 hours."
        return {
            "date": session["date"],
            "session_name": session["session_name"],
            "discipline": discipline,
            "priority": "Do not start this quality run underfueled.",
            "guidance": {"pre": pre, "during": during, "post": post},
            "hydration_focus": "moderate",
            "reason_codes": ["quality_run", "carbohydrate_priority"],
        }

    if discipline == "Run":
        low, high = carb_range_g_per_h(duration)
        needs_mid_run_fuel = bool(high)
        if needs_mid_run_fuel:
            during = (
                f"Aim for {low}-{high}g carbohydrate/hour from the first 30-40 minutes (gels, chews or drink), "
                f"water to thirst. {HEAT_NOTE}{gut_note}"
            )
        elif readiness_status != "green":
            during = "Water as needed; carry one gel in case energy dips while recovery is shaky."
        else:
            during = "Water as needed; no structured during-fuel is required."
        return {
            "date": session["date"],
            "session_name": session["session_name"],
            "discipline": discipline,
            "priority": "Keep this run fueled enough to stay easy and productive.",
            "guidance": {
                "pre": "Use a light pre-run snack only if hunger is present or if the run follows a long work gap.",
                "during": during,
                "post": f"Normal meal plus about {protein_target}g protein inside 60-90 minutes.",
            },
            "hydration_focus": "moderate"
            if needs_mid_run_fuel or hydration_flag
            else "normal",
            "reason_codes": ["aerobic_run"],
        }

    if discipline == "WeightTraining":
        return {
            "date": session["date"],
            "session_name": session["session_name"],
            "discipline": discipline,
            "priority": "Protein and hydration matter more than chasing extra carbohydrate here.",
            "guidance": {
                "pre": "Normal mixed meal; add carbohydrate only if paired with endurance work the same day.",
                "during": "Water only.",
                "post": f"Aim for {protein_target}g protein plus normal meal quality to support tissue recovery.",
            },
            "hydration_focus": "normal",
            "reason_codes": ["strength_support"],
        }

    return {
        "date": session["date"],
        "session_name": session["session_name"],
        "discipline": discipline,
        "priority": "Normal meal quality and hydration are enough.",
        "guidance": {
            "pre": "Normal meals.",
            "during": "Water as needed.",
            "post": f"Normal meal plus about {protein_target}g protein.",
        },
        "hydration_focus": "normal",
        "reason_codes": ["maintenance"],
    }


def _daily_targets_for_sessions(
    day_sessions: list[dict[str, Any]],
    session_fueling: list[dict[str, Any]],
    protein_target: int,
    hydration_flag: bool,
) -> dict[str, Any]:
    carb_focus = "low"
    hydration_focus = "normal"
    sodium_focus = "optional"

    if (
        any(session.get("priority") == "key" for session in day_sessions)
        or len(day_sessions) >= 2
    ):
        carb_focus = "high"
    elif any(_is_long_session(session) for session in day_sessions):
        carb_focus = "moderate_high"
    elif any(session["discipline"] in {"Ride", "Run"} for session in day_sessions):
        carb_focus = "moderate"

    if hydration_flag or any(
        item["hydration_focus"] == "high" for item in session_fueling
    ):
        hydration_focus = "high"
        sodium_focus = "planned"
    elif any(item["hydration_focus"] == "moderate" for item in session_fueling):
        hydration_focus = "moderate"

    return {
        "carb_focus": carb_focus,
        "protein_target_g": protein_target,
        "hydration_focus": hydration_focus,
        "sodium_focus": sodium_focus,
    }


def _daily_reason_codes(
    day_sessions: list[dict[str, Any]],
    readiness_status: str,
    hydration_flag: bool,
) -> list[str]:
    reasons: list[str] = []
    if any(session.get("priority") == "key" for session in day_sessions):
        reasons.append("key_session")
    if len(day_sessions) >= 2:
        reasons.append("double_day")
    if any(_is_long_session(session) for session in day_sessions):
        reasons.append("long_session")
    if any(_is_quality_run(session) for session in day_sessions):
        reasons.append("quality_run")
    if any(_is_hard_ride(session) for session in day_sessions):
        reasons.append("hard_ride")
    if readiness_status != "green":
        reasons.append("readiness_support")
    if hydration_flag:
        reasons.append("hydration_focus")
    return reasons


def _note_body(
    day_priority: str,
    daily_targets: dict[str, Any],
    main_fueling: dict[str, Any],
    hydration_guidance: str,
) -> str:
    guidance = main_fueling["guidance"]
    return "\n".join(
        [
            f"Priority: {day_priority}",
            f"Targets: carbs={daily_targets['carb_focus']}, protein~{daily_targets['protein_target_g']}g, hydration={daily_targets['hydration_focus']}",
            f"Pre: {guidance['pre']}",
            f"During: {guidance['during']}",
            f"Post: {guidance['post']}",
            f"Hydration: {hydration_guidance}",
        ]
    )


def _meaningful_note(reason_codes: list[str], day_priority: str) -> bool:
    del day_priority
    meaningful_codes = {
        "key_session",
        "double_day",
        "long_session",
        "quality_run",
        "hard_ride",
        "hydration_focus",
    }
    return any(code in meaningful_codes for code in reason_codes)


def run(
    snapshot: dict[str, Any],
    draft_plan: dict[str, Any],
    performance: dict[str, Any],
    macro: dict[str, Any],
    race: dict[str, Any],
) -> dict[str, Any]:
    bucket = race_bucket((race or {}).get("priority_race"))
    weight_kg = _weight_kg(snapshot)
    protein_target = _protein_target_g(weight_kg)
    readiness_status = str(performance.get("readiness_status", "green"))
    hydration_flag = _hydration_flag(snapshot)
    energy_risk = _energy_availability_risk(snapshot, performance)

    sessions_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for session in draft_plan["sessions"]:
        sessions_by_date[session["date"]].append(session)

    session_fueling: list[dict[str, Any]] = []
    daily_priorities: list[dict[str, Any]] = []
    daily_targets: list[dict[str, Any]] = []
    daily_notes: list[dict[str, Any]] = []

    for date in sorted(sessions_by_date):
        day_sessions = sessions_by_date[date]
        day = day_sessions[0]["day"]
        fueling_for_day = [
            _session_fueling(
                session, weight_kg, readiness_status, hydration_flag, bucket
            )
            for session in day_sessions
            if session["discipline"] != "Recovery"
        ]
        session_fueling.extend(fueling_for_day)

        reason_codes = _daily_reason_codes(
            day_sessions, readiness_status, hydration_flag
        )
        if any(session.get("priority") == "key" for session in day_sessions):
            day_priority = "Fuel the key work properly and avoid turning the day into a glycogen deficit test."
        elif len(day_sessions) >= 2:
            day_priority = (
                "Fuel both sessions well enough that the second one still has quality."
            )
        elif any(_is_long_session(session) for session in day_sessions):
            day_priority = "Treat this as a meaningful fueling day, not as routine background training."
        elif readiness_status != "green":
            day_priority = "Keep recovery nutrition tidy so lower readiness does not turn into low energy availability."
        else:
            day_priority = "Prioritize normal meal quality and hydration."

        targets = _daily_targets_for_sessions(
            day_sessions, fueling_for_day, protein_target, hydration_flag
        )
        daily_targets.append({"date": date, "day": day, **targets})
        daily_priorities.append(
            {
                "date": date,
                "day": day,
                "priority": day_priority,
                "session_names": [session["session_name"] for session in day_sessions],
                "reason_codes": reason_codes,
            }
        )

        main_fueling = max(
            fueling_for_day,
            key=lambda item: len(item.get("reason_codes", [])),
            default=None,
        )
        if main_fueling and _meaningful_note(reason_codes, day_priority):
            hydration_line = (
                "Start hydrated, replace fluids quickly after training, and add sodium on the longer or sweatier sessions."
                if hydration_flag or targets["hydration_focus"] != "normal"
                else "Start sessions hydrated and replace fluids to thirst afterward."
            )
            daily_notes.append(
                {
                    "date": date,
                    "day": day,
                    "title": "Nutrition Targets",
                    "body": _note_body(
                        day_priority, targets, main_fueling, hydration_line
                    ),
                    "reason_codes": reason_codes,
                    "push_to_intervals": True,
                    "external_id": f"coach-{date}-note-nutrition",
                }
            )

    major_concerns = []
    if hydration_flag:
        major_concerns.append(
            "Hydration was flagged in the weekly context, so key-session fluid and sodium execution should be deliberate this week."
        )
    if energy_risk["status"] != "low":
        major_concerns.append(energy_risk["rationale"])
    special_notes: list[str] = []
    weight_change = _weight_change_kg(snapshot, performance)
    if (performance.get("energy_availability_concern") or {}).get("flag") or (
        weight_change is not None and weight_change <= WEIGHT_DROP_CONCERN_KG
    ):
        special_notes.append(LOW_ENERGY_NOTE)
    if _returning_from_break(snapshot, performance):
        special_notes.append(RETURN_FROM_BREAK_NOTE)
    if _post_illness(snapshot, performance):
        special_notes.append(POST_ILLNESS_NOTE)
    major_concerns.extend(special_notes)

    quality_run_days = sum(
        1
        for sessions in sessions_by_date.values()
        if any(_is_quality_run(session) for session in sessions)
    )
    hard_ride_days = sum(
        1
        for sessions in sessions_by_date.values()
        if any(_is_hard_ride(session) for session in sessions)
    )
    long_session_days = sum(
        1
        for sessions in sessions_by_date.values()
        if any(_is_long_session(session) for session in sessions)
    )
    if macro.get("week_classification") == "recovery":
        week_strategy = f"Fuel this recovery week according to the real stress: keep everyday intake steady, support the double days so the second session never starts flat, and treat the longer weekend endurance work as the main carbohydrate priority while keeping daily protein around {protein_target}g."
    elif quality_run_days or hard_ride_days:
        week_strategy = f"Fuel this {macro.get('week_classification', 'training')} week according to the actual stress: prioritize carbohydrate around the {quality_run_days} quality run day(s), the {hard_ride_days} harder ride day(s), and the {long_session_days} longer session day(s) while keeping daily protein around {protein_target}g after meaningful work."
    else:
        week_strategy = f"Fuel this {macro.get('week_classification', 'training')} week steadily: match carbohydrate to the longer aerobic sessions, keep hydration tidy, and keep daily protein around {protein_target}g after meaningful work."

    hydration_targets = {
        "default_session_fluids_ml_per_hour": "400-650",
        "key_endurance_fluids_ml_per_hour": "500-850",
        "long_ride_sodium_mg_per_hour": "500-900",
    }

    return {
        "agent": "nutritionist",
        "week_strategy": week_strategy,
        "daily_priorities": daily_priorities,
        "session_fueling": session_fueling,
        "daily_targets": daily_targets,
        "hydration_targets": hydration_targets,
        "energy_availability_risk": energy_risk,
        "daily_notes": daily_notes,
        "hydration_guidance": "Start sessions hydrated, use the higher end of the fluid range on key and long sessions, and tighten sodium execution when rides run long or sweaty.",
        "recovery_priorities": f"Hit about {protein_target}g protein after meaningful sessions and avoid stacking two underfueled days together."
        + (f" {' '.join(special_notes)}" if special_notes else ""),
        "major_concerns": major_concerns,
        "special_notes": special_notes,
        "confidence": 0.84,
    }
