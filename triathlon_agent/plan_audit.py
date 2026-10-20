"""Deterministic coaching audit of a finished weekly plan.

In the production flow every agent is an LLM following the prompts, so nothing
guarantees the head coach actually applied `training_intensity_doctrine.md`.
`finalize-run` runs this audit on `final_plan.json` and puts the findings at the
top of the weekly report. It warns; it never blocks approval. All thresholds
come from `coaching_limits.py`, shared with the stub planner and the validator.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from .agents.head_coach_planner import (
    ftp_target_scale,
    impact_niggles,
    is_hard_session,
    niggle_flagged,
    previous_week_sessions,
    race_duration_estimate,
    races_in_week,
    reduced_dose_week,
    run_pace_min_per_km,
    tennis_days,
)
from .agents.macro_coach import phase_windows
from .coaching_limits import (
    B_RACE_AUDIT_QUALITY_H,
    DANIELS_CAPS,
    DANIELS_FAMILY_TYPE,
    LONG_FALLBACK_56D_FACTOR,
    LONG_JUMP_MIN,
    LONG_JUMP_PCT,
    LONG_PROGRESSION_MIN,
    LONG_RUN_FALLBACK_MIN,
    MAX_PLANNED_INTENSITY_PCT,
    MAX_WEEKLY_ELEVATION_GROWTH_PCT,
    MAX_WEEKLY_RUN_GROWTH_PCT,
    MAX_WEEKLY_TOTAL_GROWTH_PCT,
    MIN_GREEN_QUALITY,
    MIN_PLANNED_INTENSITY_PCT,
    MODERATE_FAMILIES,
    PROGRESSION_BUDGET_MAX,
    PROGRESSION_BUDGET_MAX_IMPACT,
    TAPER_VOLUME_BY_WEEKS_OUT,
    TAPER_VOLUME_TOLERANCE_HIGH,
    TAPER_VOLUME_TOLERANCE_LOW,
    VOLUME_PROGRESSION_PCT,
    baseline_week_minutes,
    is_long_session,
    post_race_easy_days,
    safe_float,
)
from .progression import (
    HR_ZONE_RE,
    LADDERS,
    PERCENT_RE,
    _step_minutes,
    classify_target,
    current_rung,
    session_context,
    session_dose,
)
from .race_selection import calendar_races, select_priority_race


DAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
ENDURANCE_DISCIPLINES = {"Ride", "Run"}
# Kept for backwards compatibility; the numbers live in coaching_limits.
MAX_WEEKLY_GROWTH_PCT = MAX_WEEKLY_TOTAL_GROWTH_PCT
PACE_R_MIN_PCT = 111
MODERATE_FTP_RANGE = (76, 85)
PERCENT_FTP_SUB_RE = re.compile(r"(\d+)(?:\s*-\s*(\d+))?\s*%")
# Elevation sums are rounded per session; tolerate this much (m).
ELEVATION_TOLERANCE_M = 50
DURATION_TOLERANCE_MIN = 5


def _is_quality(session: dict[str, Any]) -> bool:
    if session.get("threshold_test") or _is_race(session):
        return False
    if session.get("progression"):
        return True
    return bool(session_dose(session))


def _is_race(session: dict[str, Any]) -> bool:
    return bool(session.get("race_day")) or session.get("zone_focus") == "race"


def _is_tennis(session: dict[str, Any]) -> bool:
    return "tennis" in str(session.get("discipline", "")).lower() or "tennis" in str(
        session.get("session_name", "")
    ).lower()


def _is_optional(session: dict[str, Any]) -> bool:
    return str(session.get("priority") or "") == "optional"


def _day_index(session: dict[str, Any]) -> int | None:
    day = session.get("day")
    return DAY_NAMES.index(day) if day in DAY_NAMES else None


def _is_long(session: dict[str, Any]) -> bool:
    return is_long_session(session)


def _is_heavy_strength(session: dict[str, Any]) -> bool:
    if session.get("discipline") != "WeightTraining":
        return False
    text = f"{session.get('session_name') or ''} {session.get('intensity_guidance') or ''}".lower()
    if any(word in text for word in ("activation", "mobility", "light", "maintenance")):
        return False
    return "heavy" in text or float(session.get("duration_min") or 0) >= 40


def _unscale_ftp(text: str, scale: float | None) -> str:
    """%FTP targets prescribed against a stale FTP, read back on the real threshold."""
    if not scale or "%" not in text or "pace" in text.lower():
        return text

    def replace(match: re.Match[str]) -> str:
        low = round(int(match.group(1)) / scale)
        if match.group(2):
            return f"{low}-{round(int(match.group(2)) / scale)}%"
        return f"{low}%"

    return PERCENT_FTP_SUB_RE.sub(replace, text)


def intensity_minutes(
    session: dict[str, Any], scale: float | None = None
) -> tuple[float, float]:
    """(moderate, high) minutes of one bike/run session, each session counted once.

    A `progression`-tagged session counts through its ladder family (sweet
    spot / race pace = moderate, other ladders = high). Otherwise the steps are
    read; %FTP bands are first scaled back by `ftp_target_scale` so a stale-FTP
    sweet spot (e.g. 75-79% of a too-high FTP) is not miscounted as tempo.
    """
    discipline = str(session.get("discipline") or "")
    if discipline not in ENDURANCE_DISCIPLINES or session.get("threshold_test") or _is_race(session):
        return 0.0, 0.0
    progression = session.get("progression")
    if isinstance(progression, dict) and progression.get("family") in LADDERS:
        minutes = safe_float(progression.get("time_in_zone_min")) or 0.0
        if progression["family"] in MODERATE_FAMILIES:
            return minutes, 0.0
        return 0.0, minutes
    moderate = high = 0.0
    context = session_context(session)
    for block in session.get("blocks", []) or []:
        repeat = int(block.get("repeat") or 1)
        block_context = f"{context} {block.get('section') or ''}"
        for step in block.get("steps", []) or []:
            text = str(step.get("target") or "")
            if discipline == "Ride":
                text = _unscale_ftp(text, scale)
            minutes = repeat * _step_minutes(step.get("duration"))
            family = classify_target(discipline, text, block_context)
            if family is not None:
                if family in MODERATE_FAMILIES:
                    moderate += minutes
                else:
                    high += minutes
                continue
            zone = HR_ZONE_RE.search(text)
            if zone:
                if int(zone.group(1)) == 3:
                    moderate += minutes
                continue
            match = PERCENT_RE.search(text)
            if not match:
                continue
            upper = int(match.group(2) or match.group(1))
            if discipline == "Ride" and MODERATE_FTP_RANGE[0] <= upper <= MODERATE_FTP_RANGE[1]:
                moderate += minutes
            elif "pace" in text.lower() and 85 <= upper < 90:
                moderate += minutes
    return moderate, high


def moderate_minutes(session: dict[str, Any], scale: float | None = None) -> float:
    """Moderate minutes of a session (kept for callers of the old helper)."""
    return intensity_minutes(session, scale)[0]


def _repetition_minutes(session: dict[str, Any]) -> float:
    """Daniels R: run steps at or above ~111% threshold pace."""
    if session.get("discipline") != "Run":
        return 0.0
    total = 0.0
    for block in session.get("blocks", []) or []:
        repeat = int(block.get("repeat") or 1)
        for step in block.get("steps", []) or []:
            text = str(step.get("target") or "")
            match = PERCENT_RE.search(text)
            if match and "pace" in text.lower():
                upper = int(match.group(2) or match.group(1))
                if upper >= PACE_R_MIN_PCT:
                    total += repeat * _step_minutes(step.get("duration"))
    return total


def _baseline_week_minutes(snapshot: dict[str, Any]) -> float | None:
    return baseline_week_minutes(snapshot)


def _recent_long(snapshot: dict[str, Any], load_key: str, key: str) -> float | None:
    load = ((snapshot.get("training_load") or {}).get(load_key)) or {}
    value = safe_float(load.get(key))
    return value if value and value > 0 else None


def _finding(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _week_start(snapshot: dict[str, Any], sessions: list[dict[str, Any]]) -> date | None:
    try:
        return date.fromisoformat(str(snapshot.get("week_start"))[:10])
    except ValueError:
        pass
    for session in sessions:
        try:
            day = date.fromisoformat(str(session.get("date"))[:10])
        except ValueError:
            continue
        return day - timedelta(days=day.weekday())
    return None


def audit_plan(
    snapshot: dict[str, Any],
    performance: dict[str, Any],
    macro: dict[str, Any],
    final_plan: dict[str, Any],
) -> dict[str, Any]:
    sessions = final_plan.get("sessions", []) or []
    quality = [session for session in sessions if _is_quality(session)]
    readiness = str(performance.get("readiness_status", "green"))
    allowance = (performance.get("intensity_allowance") or {}).get(
        "max_quality_sessions"
    )
    underload = (performance.get("underload") or {}).get("status") or (
        (snapshot.get("training_load") or {}).get("underload") or {}
    ).get("status")
    transition = macro.get("macro_phase") == "transition"
    holiday = macro.get("week_classification") == "holiday"
    recovery_week = (
        macro.get("week_classification") in {"recovery", "unload"}
        or macro.get("friel_cycle_type") == "recovery"
        or transition
    )
    taper = (
        macro.get("macro_phase") == "taper" or macro.get("friel_cycle_type") == "taper"
    )
    load_week = not recovery_week and not taper and not holiday
    returning = (
        ((snapshot.get("training_load") or {}).get("return_from_break") or {}).get("status")
        == "returning"
    )
    findings: list[dict[str, str]] = []
    info: list[str] = []
    daniels_capped: set[str] = set()

    # Threshold tests the macro coach scheduled must be in the plan.
    planned_tests = {
        (session.get("threshold_test") or {}).get("discipline")
        for session in sessions
        if session.get("threshold_test")
    }
    for entry in (macro.get("threshold_tests") or {}).get("scheduled", []) or []:
        if entry.get("discipline") not in planned_tests:
            findings.append(
                _finding(
                    "missing_threshold_test",
                    f"The macro coach scheduled a {entry.get('discipline')} threshold test, but the plan does not contain it.",
                )
            )

    if recovery_week and quality:
        findings.append(
            _finding(
                "quality_in_recovery_week",
                "Recovery week contains quality work: "
                + ", ".join(session["session_name"] for session in quality)
                + ".",
            )
        )
    if holiday:
        mandatory = [session for session in quality if not _is_optional(session)]
        if mandatory or len(quality) > 1:
            findings.append(
                _finding(
                    "holiday_not_maintenance",
                    "Holiday weeks are maintenance: at most one quality session and only as optional - "
                    + ", ".join(session["session_name"] for session in quality)
                    + ".",
                )
            )

    # A race in the week is its quality session; a re-entry week starts with one.
    races_planned = sum(1 for session in sessions if _is_race(session))
    dose_week = load_week and not reduced_dose_week(snapshot, final_plan)
    if dose_week and readiness == "green" and len(quality) + races_planned < MIN_GREEN_QUALITY:
        findings.append(
            _finding(
                "underdosed_green_week",
                f"Green load week with only {len(quality)} quality session(s); the doctrine default is {MIN_GREEN_QUALITY}.",
            )
        )
    if allowance is not None:
        if len(quality) > int(allowance):
            findings.append(
                _finding(
                    "over_allowance",
                    f"{len(quality)} quality sessions exceed the readiness allowance of {allowance}.",
                )
            )
        elif (
            load_week
            and readiness == "green"
            and underload == "detected"
            and len(quality) < int(allowance)
        ):
            findings.append(
                _finding(
                    "underload_not_addressed",
                    f"Underload is detected, but the week uses {len(quality)} of {allowance} allowed quality sessions.",
                )
            )

    # Daniels per-session caps (minutes proxy for mileage).
    weekly_run_min = sum(
        float(session.get("duration_min") or 0)
        for session in sessions
        if session.get("discipline") == "Run" and not _is_race(session)
    )
    for session in sessions:
        if session.get("discipline") != "Run" or not weekly_run_min:
            continue
        by_type: dict[str, float] = {}
        for family, minutes in session_dose(session).items():
            daniels = DANIELS_FAMILY_TYPE.get(family)
            if daniels:
                by_type[daniels] = by_type.get(daniels, 0.0) + minutes
        repetition = _repetition_minutes(session)
        if repetition:
            by_type["R"] = repetition
            by_type["I"] = max(0.0, by_type.get("I", 0.0) - repetition)
        for daniels, minutes in by_type.items():
            share, floor = DANIELS_CAPS[daniels]
            cap = max(floor, share * weekly_run_min)
            if minutes >= 0.85 * cap:
                daniels_capped.add(str(session.get("session_name")))
            if minutes > cap:
                findings.append(
                    _finding(
                        "daniels_cap_exceeded",
                        f"{session['session_name']} has {minutes:g}' of Daniels {daniels} work; the cap is "
                        f"~{cap:.0f}' ({int(share * 100)}% of {weekly_run_min:g}' weekly running).",
                    )
                )

    last_doses = (snapshot.get("progression") or {}).get("last_dose_by_family") or {}
    rung_steps: list[str] = []
    for session in quality:
        progression = session.get("progression")
        if not progression:
            findings.append(
                _finding(
                    "missing_progression",
                    f"{session['session_name']} has no `progression` object, so next week cannot progress from it.",
                )
            )
            continue
        family = progression.get("family")
        last = last_doses.get(family)
        if not last or family not in LADDERS:
            continue
        planned = float(progression.get("time_in_zone_min") or 0)
        previous = float(last.get("minutes") or 0)
        planned_rung = safe_float(progression.get("rung"))
        previous_rung = current_rung(family, last)
        stepped = planned > previous + 0.5 or (
            planned_rung is not None and previous_rung is not None and planned_rung > previous_rung
        )
        if stepped and last.get("completed"):
            rung_steps.append(f"{family} rung")
        if not (dose_week and readiness == "green" and last.get("completed")):
            continue
        # Holding a dose at the Daniels cap is correct, not a missed progression.
        if not stepped and session.get("session_name") not in daniels_capped:
            findings.append(
                _finding(
                    "no_progression",
                    f"{session['session_name']} plans {planned:g}' of {family}, "
                    f"not beyond the last completed {previous:g}' ({last.get('session_name')}). "
                    "A green load week should move one rung up or explain why not.",
                )
            )

    quality_days = sorted(
        {DAY_NAMES.index(session["day"]) for session in quality if session.get("day") in DAY_NAMES}
    )
    if any(later - earlier == 1 for earlier, later in zip(quality_days, quality_days[1:])):
        findings.append(
            _finding(
                "consecutive_quality_days",
                "Quality sessions sit on consecutive days.",
            )
        )

    endurance = [
        session
        for session in sessions
        if session.get("discipline") in ENDURANCE_DISCIPLINES and not _is_race(session)
    ]
    endurance_min = sum(
        float(session.get("duration_min") or 0)
        for session in endurance
        if not session.get("threshold_test")
    )
    scale = ftp_target_scale(snapshot)
    moderate_min = high_min = 0.0
    for session in endurance:
        moderate, high = intensity_minutes(session, scale)
        moderate_min += moderate
        high_min += high
    intensity_min = high_min + moderate_min
    intensity_pct = round(100 * intensity_min / endurance_min, 1) if endurance_min else 0.0
    if dose_week and races_planned == 0 and readiness == "green" and endurance_min and intensity_pct < MIN_PLANNED_INTENSITY_PCT:
        findings.append(
            _finding(
                "almost_all_easy",
                f"Only {intensity_pct}% of planned bike/run time is above easy intensity; pyramidal targets about 12-20% moderate plus 3-8% high.",
            )
        )
    if not taper and endurance_min and intensity_pct > MAX_PLANNED_INTENSITY_PCT:
        findings.append(
            _finding(
                "intensity_too_high",
                f"{intensity_pct}% of planned bike/run time is moderate or hard; above ~{MAX_PLANNED_INTENSITY_PCT:g}% "
                "the week stops being pyramidal and the easy days stop being easy.",
            )
        )

    planned_minutes = sum(
        float(session.get("duration_min") or 0)
        for session in sessions
        if session.get("discipline") != "Recovery" and not _is_race(session)
    )
    findings.extend(_overload_findings(snapshot, sessions, planned_minutes, load_week))
    findings.extend(_run_load_findings(snapshot, sessions, load_week))
    findings.extend(_spacing_findings(snapshot, sessions, quality))
    findings.extend(_race_findings(snapshot, macro, sessions, planned_minutes, taper))
    findings.extend(_reentry_findings(snapshot, sessions, planned_minutes, quality))
    findings.extend(_niggle_findings(snapshot, sessions))
    if load_week:
        findings.extend(
            _budget_findings(snapshot, performance, sessions, planned_minutes, rung_steps, readiness, underload)
        )
    if transition and quality:
        findings.append(
            _finding(
                "quality_in_transition",
                "Post-race transition contains quality work: "
                + ", ".join(session["session_name"] for session in quality)
                + ". Keep it easy, short and optional.",
            )
        )
    missing_cues = [
        session["session_name"]
        for session in sessions
        if (session in quality or _is_long(session))
        and not (session.get("rpe_target") and session.get("bail_out_rule"))
    ]
    if missing_cues:
        info.append(
            "Key sessions without rpe_target / bail_out_rule: " + ", ".join(missing_cues) + "."
        )

    return {
        "status": "warn" if findings else "pass",
        "quality_session_count": len(quality),
        "max_quality_sessions": allowance,
        "planned_intensity_pct": intensity_pct,
        "planned_moderate_pct": round(100 * moderate_min / endurance_min, 1) if endurance_min else 0.0,
        "planned_high_pct": round(100 * high_min / endurance_min, 1) if endurance_min else 0.0,
        "findings": findings,
        "info": info,
    }


def _overload_findings(
    snapshot: dict[str, Any],
    sessions: list[dict[str, Any]],
    planned_minutes: float,
    load_week: bool,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    baseline = _baseline_week_minutes(snapshot)
    if baseline and planned_minutes > baseline * (1 + MAX_WEEKLY_TOTAL_GROWTH_PCT / 100):
        growth = round(100 * (planned_minutes / baseline - 1), 1)
        findings.append(
            _finding(
                "weekly_volume_jump",
                f"Planned {planned_minutes:g}' is {growth}% above the recent weekly average of {baseline:.0f}'; "
                f"grow by at most ~{MAX_WEEKLY_TOTAL_GROWTH_PCT:g}% a week.",
            )
        )
    ramp = ((snapshot.get("training_load") or {}).get("ramp")) or {}
    if load_week and ramp.get("status") == "high" and baseline and planned_minutes > baseline:
        findings.append(
            _finding(
                "ramp_high_and_rising",
                "The load ramp is already high (CTL ramp / ACWR), yet the week plans more than the recent average.",
            )
        )
    findings.extend(_long_session_findings(snapshot, sessions))
    return findings


def _longest(sessions: list[dict[str, Any]], discipline: str) -> float:
    return max(
        (
            float(session.get("duration_min") or 0)
            for session in sessions
            if session.get("discipline") == discipline and not _is_race(session)
        ),
        default=0.0,
    )


def recent_long_run(snapshot: dict[str, Any]) -> float | None:
    """Longest run of the last 28 days; a value equal to the longest race is ignored."""
    recent = _recent_long(snapshot, "run_load", "longest_run_min_28d")
    race = _recent_long(snapshot, "run_load", "longest_race_min_28d")
    if recent is not None and race is not None and abs(recent - race) <= 1:
        return None
    return recent


def long_run_fallback_cap(snapshot: dict[str, Any]) -> tuple[float, str]:
    """Cap for the long run when there is no recent long run (same chain as the planner)."""
    load = snapshot.get("training_load") or {}
    run_load = load.get("run_load") or {}
    rfb = load.get("return_from_break") or {}
    caps = [
        safe_float((run_load.get("week_caps") or {}).get("max_long_run_min")),
        safe_float(rfb.get("long_run_cap_min")) if rfb.get("status") == "returning" else None,
    ]
    caps = [value for value in caps if value]
    if caps:
        return min(caps), "the run-load / re-entry cap"
    longest_56 = safe_float(run_load.get("longest_run_min_56d"))
    race = safe_float(run_load.get("longest_race_min_28d"))
    if longest_56 and not (race and abs(longest_56 - race) <= 1):
        return longest_56 * LONG_FALLBACK_56D_FACTOR, "70% of the 56-day longest run"
    return float(LONG_RUN_FALLBACK_MIN), f"the conservative {LONG_RUN_FALLBACK_MIN}' restart"


def _long_session_findings(
    snapshot: dict[str, Any], sessions: list[dict[str, Any]]
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    load = snapshot.get("training_load") or {}
    run_load = load.get("run_load") or {}
    longest_run = _longest(sessions, "Run")
    recent = recent_long_run(snapshot)
    week_cap = safe_float((run_load.get("week_caps") or {}).get("max_long_run_min"))
    if recent:
        limit = recent + max(LONG_JUMP_MIN, LONG_JUMP_PCT * recent)
        if longest_run > limit:
            findings.append(
                _finding(
                    "long_run_jump",
                    f"The longest planned run is {longest_run:g}' against {recent:g}' in the last 28 days; "
                    f"progress by at most max({LONG_JUMP_MIN:g}', {int(LONG_JUMP_PCT * 100)}%).",
                )
            )
        elif week_cap and longest_run > week_cap + DURATION_TOLERANCE_MIN:
            findings.append(
                _finding(
                    "long_run_over_cap",
                    f"The longest planned run ({longest_run:g}') is above the run-load cap of {week_cap:g}'.",
                )
            )
    elif run_load and longest_run:
        cap, basis = long_run_fallback_cap(snapshot)
        if longest_run > cap + DURATION_TOLERANCE_MIN:
            findings.append(
                _finding(
                    "long_run_jump",
                    f"No recent long run on record, yet the plan has a {longest_run:g}' run; "
                    f"restart from {basis} (~{cap:.0f}'), never from a template value.",
                )
            )
    recent_ride = _recent_long(snapshot, "ride_load", "longest_ride_min_28d")
    longest_ride = _longest(sessions, "Ride")
    if recent_ride and longest_ride > recent_ride + max(LONG_JUMP_MIN, LONG_JUMP_PCT * recent_ride):
        findings.append(
            _finding(
                "long_ride_jump",
                f"The longest planned ride is {longest_ride:g}' against {recent_ride:g}' in the last 28 days; "
                f"progress by at most max({LONG_JUMP_MIN:g}', {int(LONG_JUMP_PCT * 100)}%).",
            )
        )
    return findings


def _planned_run_minutes(sessions: list[dict[str, Any]]) -> float:
    return sum(
        float(session.get("duration_min") or 0)
        for session in sessions
        if session.get("discipline") == "Run" and not _is_race(session)
    )


def _planned_run_km(snapshot: dict[str, Any], sessions: list[dict[str, Any]]) -> float:
    pace = run_pace_min_per_km(snapshot.get("training_load") or {})
    total = 0.0
    for session in sessions:
        if session.get("discipline") != "Run" or _is_race(session):
            continue
        km = safe_float(session.get("distance_km"))
        total += km if km is not None else float(session.get("duration_min") or 0) / pace
    return total


def _elevation(sessions: list[dict[str, Any]], key: str) -> float:
    return sum(
        safe_float(session.get(key)) or 0.0
        for session in sessions
        if session.get("discipline") == "Run" and not _is_race(session)
    )


def _run_load_findings(
    snapshot: dict[str, Any], sessions: list[dict[str, Any]], load_week: bool
) -> list[dict[str, str]]:
    """Running-specific load: minutes/km vs recent (~10%/week) and the week caps,
    descent vs `week_caps.max_descent_m`, weekly D+ growth <= ~15%."""
    findings: list[dict[str, str]] = []
    run_load = (snapshot.get("training_load") or {}).get("run_load") or {}
    if not run_load:
        return findings
    caps = run_load.get("week_caps") or {}
    run_min = _planned_run_minutes(sessions)
    recent_min = safe_float(run_load.get("avg_weekly_run_min_4w"))
    limit = recent_min * (1 + MAX_WEEKLY_RUN_GROWTH_PCT / 100) if recent_min else None
    cap_min = safe_float(caps.get("max_run_min"))
    if cap_min:
        limit = cap_min if limit is None else min(limit, cap_min)
    if limit and run_min > limit + DURATION_TOLERANCE_MIN:
        findings.append(
            _finding(
                "run_volume_jump",
                f"Planned running {run_min:g}' is above ~{limit:.0f}' (recent {recent_min or 0:.0f}'/week "
                f"+{MAX_WEEKLY_RUN_GROWTH_PCT:g}% and the run-load cap); running grows ~10% a week at most.",
            )
        )
    cap_km = safe_float(caps.get("max_run_km"))
    if cap_km:
        run_km = _planned_run_km(snapshot, sessions)
        if run_km > cap_km * 1.05:
            findings.append(
                _finding(
                    "run_km_over_cap",
                    f"Planned running is ~{run_km:.0f} km against the week cap of {cap_km:g} km.",
                )
            )
    descent = _elevation(sessions, "elevation_loss_m")
    cap_descent = safe_float(caps.get("max_descent_m"))
    if cap_descent is not None and descent > cap_descent + ELEVATION_TOLERANCE_M:
        findings.append(
            _finding(
                "descent_over_cap",
                f"Planned descent {descent:.0f} m is above the week cap of {cap_descent:.0f} m; "
                "descending is the eccentric (and ITB) load that needs the slowest build.",
            )
        )
    gain = _elevation(sessions, "elevation_gain_m")
    recent_gain = safe_float(run_load.get("avg_weekly_elevation_gain_m_4w"))
    if (
        load_week
        and recent_gain
        and gain > recent_gain * (1 + MAX_WEEKLY_ELEVATION_GROWTH_PCT / 100) + ELEVATION_TOLERANCE_M
    ):
        findings.append(
            _finding(
                "elevation_jump",
                f"Planned climbing {gain:.0f} m D+ vs a recent {recent_gain:.0f} m/week; "
                f"grow weekly D+ by at most ~{MAX_WEEKLY_ELEVATION_GROWTH_PCT:g}%.",
            )
        )
    return findings


def _spacing_findings(
    snapshot: dict[str, Any],
    sessions: list[dict[str, Any]],
    quality: list[dict[str, Any]],
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    long_days = {_day_index(session) for session in sessions if _is_long(session)}
    quality_days = {_day_index(session) for session in quality}
    for session in quality:
        index = _day_index(session)
        if index is not None and index + 1 in long_days:
            findings.append(
                _finding(
                    "quality_before_long_session",
                    f"{session['session_name']} sits the day before a long session; the long day starts tired.",
                )
            )
    for session in sessions:
        index = _day_index(session)
        if index is None or not _is_heavy_strength(session):
            continue
        if index + 1 in quality_days | long_days:
            findings.append(
                _finding(
                    "heavy_strength_before_key",
                    f"{session['session_name']} (heavy legs) is the day before a quality or long session; "
                    "put heavy strength on a quality day or after it, never on the eve of key work.",
                )
            )
    tennis = {
        DAY_NAMES.index(day) for day in tennis_days(snapshot) if day in DAY_NAMES
    } | {_day_index(session) for session in sessions if _is_tennis(session)}
    tennis.discard(None)
    for session in quality:
        index = _day_index(session)
        if index in tennis:
            findings.append(
                _finding(
                    "quality_on_tennis_day",
                    f"{session['session_name']} lands on a tennis day (moderate-hard load).",
                )
            )
        elif (
            index is not None
            and index - 1 in tennis
            and session.get("discipline") == "Run"
        ):
            findings.append(
                _finding(
                    "hard_run_after_tennis",
                    f"{session['session_name']} is a hard run the day after tennis.",
                )
            )
    findings.extend(_week_boundary_findings(snapshot, sessions, quality))
    return findings


def _week_boundary_findings(
    snapshot: dict[str, Any],
    sessions: list[dict[str, Any]],
    quality: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Previous week's Sunday vs this Monday, when prior plan info is in the snapshot."""
    week_start = _week_start(snapshot, sessions)
    if week_start is None:
        return []
    eve = (week_start - timedelta(days=1)).isoformat()

    previous = [
        session
        for session in previous_week_sessions(snapshot)
        if str(session.get("date") or "")[:10] == eve and session.get("status") != "missed"
    ]
    hard_before = [session for session in previous if is_hard_session(session)]
    if not hard_before:
        return []
    monday_hard = [
        session
        for session in sessions
        if _day_index(session) == 0 and (session in quality or _is_long(session))
    ]
    if not monday_hard:
        return []
    return [
        _finding(
            "hard_across_week_boundary",
            f"{monday_hard[0]['session_name']} on Monday follows last Sunday's "
            f"{hard_before[0].get('session_name')}: two hard days in a row across the week boundary.",
        )
    ]


def _race_offsets(snapshot: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    offsets = []
    for race in calendar_races(snapshot):
        try:
            offsets.append((int(race.get("days_to_race")), race))
        except (TypeError, ValueError):
            continue
    return offsets


def _race_findings(
    snapshot: dict[str, Any],
    macro: dict[str, Any],
    sessions: list[dict[str, Any]],
    planned_minutes: float,
    taper: bool,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    priority = select_priority_race(snapshot)
    in_taper_window = taper
    days_to_race = None
    if priority is not None:
        window, _, _ = phase_windows(priority)
        days_to_race = int(priority.get("days_to_race", 9999))
        in_taper_window = in_taper_window or days_to_race <= window
    baseline = _baseline_week_minutes(snapshot)
    if in_taper_window and baseline:
        weeks_out = max(0, (days_to_race if days_to_race is not None else 14) // 7)
        expected = TAPER_VOLUME_BY_WEEKS_OUT.get(weeks_out, 0.75)
        if planned_minutes > baseline * (expected + TAPER_VOLUME_TOLERANCE_HIGH):
            findings.append(
                _finding(
                    "taper_volume_not_reduced",
                    f"The race is inside its taper window but the week plans {planned_minutes:g}' against a recent "
                    f"{baseline:.0f}'; this taper week should be ~{int(expected * 100)}% of it "
                    "(ladder 75% -> 60% -> 45% to race week) while keeping short intensity.",
                )
            )
        elif planned_minutes < baseline * max(0.2, expected - TAPER_VOLUME_TOLERANCE_LOW):
            findings.append(
                _finding(
                    "taper_too_deep",
                    f"The taper week plans only {planned_minutes:g}' against a recent {baseline:.0f}'; "
                    f"cutting far below ~{int(expected * 100)}% risks going flat.",
                )
            )
    for day, race in races_in_week(snapshot).items():
        index = DAY_NAMES.index(day)
        for session in sessions:
            session_index = _day_index(session)
            if _is_race(session) or session_index is None:
                continue
            hard = _is_quality(session) or _is_long(session)
            if session_index == index and (hard or float(session.get("duration_min") or 0) > 30):
                findings.append(
                    _finding(
                        "session_on_race_day",
                        f"{session['session_name']} is planned on race day ({race.get('name')}).",
                    )
                )
            elif session_index == index - 1 and hard:
                findings.append(
                    _finding(
                        "hard_session_before_race",
                        f"{session['session_name']} is hard or long the day before {race.get('name')}; keep only a short opener.",
                    )
                )
    findings.extend(_b_c_race_findings(snapshot, sessions))
    return findings


def _b_c_race_findings(
    snapshot: dict[str, Any], sessions: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """B race: no quality in the last 48h; any race: easy days after, scaled by duration."""

    findings: list[dict[str, str]] = []
    quality_by_day: dict[int, list[dict[str, Any]]] = {}
    hard_by_day: dict[int, list[dict[str, Any]]] = {}
    for session in sessions:
        index = _day_index(session)
        if index is None or _is_race(session):
            continue
        if _is_quality(session):
            quality_by_day.setdefault(index, []).append(session)
        if _is_quality(session) or _is_long(session):
            hard_by_day.setdefault(index, []).append(session)
    window_days = B_RACE_AUDIT_QUALITY_H // 24
    for offset, race in _race_offsets(snapshot):
        name = race.get("name") or "the race"
        priority = race.get("priority")
        if priority == "B" and 0 <= offset <= 6 + window_days:
            for day in range(offset - window_days, offset - 1):
                for session in quality_by_day.get(day, []):
                    findings.append(
                        _finding(
                            "quality_before_b_race",
                            f"{session['session_name']} is within {B_RACE_AUDIT_QUALITY_H}h of B-race {name}; "
                            "a B race gets a mini-taper (last quality 72h+ before).",
                        )
                    )
        if -10 <= offset <= 6:
            easy_days = post_race_easy_days(priority, race_duration_estimate(race))
            for day in range(offset + 1, offset + 1 + easy_days):
                for session in hard_by_day.get(day, []):
                    findings.append(
                        _finding(
                            "hard_after_race",
                            f"{session['session_name']} is hard or long {day - offset} day(s) after {name}; "
                            f"keep {easy_days} easy day(s) after a race of that length.",
                        )
                    )
    return findings


def _reentry_findings(
    snapshot: dict[str, Any],
    sessions: list[dict[str, Any]],
    planned_minutes: float,
    quality: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Return-from-break caps: total volume, run km, long run and quality count."""
    load = snapshot.get("training_load") or {}
    rfb = load.get("return_from_break") or {}
    if rfb.get("status") != "returning":
        return []
    findings: list[dict[str, str]] = []
    pre = safe_float(rfb.get("pre_break_weekly_min"))
    pct = safe_float(rfb.get("volume_cap_pct"))
    if pre and pct and planned_minutes > pre * pct / 100 * 1.05:
        findings.append(
            _finding(
                "reentry_volume_over_cap",
                f"Return from a break: {planned_minutes:g}' planned vs the re-entry cap of "
                f"{pre * pct / 100:.0f}' ({pct:g}% of the pre-break {pre:.0f}').",
            )
        )
    km_cap = safe_float(rfb.get("run_km_cap"))
    if km_cap:
        run_km = _planned_run_km(snapshot, sessions)
        if run_km > km_cap * 1.05:
            findings.append(
                _finding(
                    "reentry_run_over_cap",
                    f"Return from a break: ~{run_km:.0f} km of running planned vs the re-entry cap of {km_cap:g} km.",
                )
            )
    long_cap = safe_float(rfb.get("long_run_cap_min"))
    longest = _longest(sessions, "Run")
    if long_cap and longest > long_cap + DURATION_TOLERANCE_MIN:
        findings.append(
            _finding(
                "reentry_long_run_over_cap",
                f"Return from a break: the longest run is {longest:g}' vs the re-entry cap of {long_cap:g}'.",
            )
        )
    week = rfb.get("reentry_week")
    if len(quality) > 1 and week in (None, 1, 2):
        findings.append(
            _finding(
                "reentry_quality",
                f"Return from a break (re-entry week {week or 1}): {len(quality)} quality sessions; one at most.",
            )
        )
    return findings


def _niggle_findings(
    snapshot: dict[str, Any], sessions: list[dict[str, Any]]
) -> list[dict[str, str]]:
    parts = impact_niggles(snapshot)
    if not parts:
        return []
    runs = [
        session
        for session in sessions
        if session.get("discipline") == "Run" and not _is_race(session)
    ]
    findings = []
    if runs:
        findings.append(
            _finding(
                "running_with_pain",
                f"Pain reported ({', '.join(parts)}), yet the plan keeps running: "
                + ", ".join(session["session_name"] for session in runs)
                + ". Swap impact sessions to bike or aqua jogging until it settles.",
            )
        )
    if _elevation(sessions, "elevation_loss_m") > 0:
        findings.append(
            _finding(
                "descent_with_pain",
                "Pain reported, yet the plan includes descending; remove downhill work this week.",
            )
        )
    return findings


def progression_elements(
    snapshot: dict[str, Any],
    sessions: list[dict[str, Any]],
    planned_minutes: float,
    rung_steps: list[str],
) -> list[tuple[str, bool]]:
    """(element, run impact?) for everything that progresses this week."""
    elements: list[tuple[str, bool]] = [(step, False) for step in rung_steps]
    load = snapshot.get("training_load") or {}
    run_load = load.get("run_load") or {}
    recent_run = recent_long_run(snapshot)
    if recent_run and _longest(sessions, "Run") > recent_run + LONG_PROGRESSION_MIN:
        elements.append(("long run duration", True))
    recent_ride = _recent_long(snapshot, "ride_load", "longest_ride_min_28d")
    if recent_ride and _longest(sessions, "Ride") > recent_ride + LONG_PROGRESSION_MIN:
        elements.append(("long ride duration", False))
    recent_run_min = safe_float(run_load.get("avg_weekly_run_min_4w"))
    if recent_run_min and _planned_run_minutes(sessions) > recent_run_min * (1 + VOLUME_PROGRESSION_PCT / 100):
        elements.append(("run volume", True))
    recent_loss = safe_float(run_load.get("avg_weekly_elevation_loss_m_4w"))
    descent = _elevation(sessions, "elevation_loss_m")
    if recent_loss and descent > recent_loss * (1 + VOLUME_PROGRESSION_PCT / 100) + ELEVATION_TOLERANCE_M:
        elements.append(("descent", True))
    baseline = _baseline_week_minutes(snapshot)
    if baseline and planned_minutes > baseline * (1 + VOLUME_PROGRESSION_PCT / 100):
        elements.append(("added volume", False))
    return elements


def _budget_findings(
    snapshot: dict[str, Any],
    performance: dict[str, Any],
    sessions: list[dict[str, Any]],
    planned_minutes: float,
    rung_steps: list[str],
    readiness: str,
    underload: str | None,
) -> list[dict[str, str]]:
    """Progress one thing at a time (max 2 elements, max 1 of them run impact)."""
    elements = progression_elements(snapshot, sessions, planned_minutes, rung_steps)
    exempt = underload == "detected" and readiness == "green" and not niggle_flagged(snapshot)
    if exempt:
        return []
    impact = [name for name, is_impact in elements if is_impact]
    if len(elements) <= PROGRESSION_BUDGET_MAX and len(impact) <= PROGRESSION_BUDGET_MAX_IMPACT:
        return []
    return [
        _finding(
            "progression_budget_exceeded",
            f"{len(elements)} elements progress this week ({', '.join(name for name, _ in elements)}), "
            f"{len(impact)} of them run impact; progress at most {PROGRESSION_BUDGET_MAX} things, "
            f"at most {PROGRESSION_BUDGET_MAX_IMPACT} impact-related, and hold the rest.",
        )
    ]


