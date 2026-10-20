from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..coaching_limits import (
    LONG_SESSION_MIN,
    MAX_WEEKLY_TOTAL_GROWTH_PCT,
    OVER_REFERENCE_WARN_PCT,
    baseline_week_minutes,
    is_health_reason,
    safe_float,
)
from .head_coach_planner import LIGHT_STRENGTH_MAX_MIN, hard_cap_minutes, reduced_dose_week


DAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
# Validator verdict vocabulary, mildest first. The prompts' "revise materially"
# is the `revise` verdict; `unsafe` means the week must not be pushed as is.
VERDICTS = ("keep", "revise lightly", "revise", "unsafe")
VERDICT_RANK = {verdict: rank for rank, verdict in enumerate(VERDICTS)}
# Planned hours this far above the athlete's typical week are a life-load risk
# (9-5 job, ~7 h sleep: hours compete with sleep and work), whatever readiness says.
OVER_REFERENCE_RISK_PCT = OVER_REFERENCE_WARN_PCT


def _bump_verdict(current: str, proposed: str) -> str:
    if VERDICT_RANK[proposed] > VERDICT_RANK[current]:
        return proposed
    return current


def _choose_support_session_for_trim(
    day_sessions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for discipline in ("Swim", "WeightTraining", "Run", "Ride"):
        for session in day_sessions:
            if session.get("priority") == "key":
                continue
            if session.get("discipline") == discipline:
                return session
    return None


def _threshold_test(session: dict[str, Any]) -> bool:
    return bool(session.get("threshold_test"))


def _quality_run(session: dict[str, Any]) -> bool:
    if _threshold_test(session):
        return False
    return session.get("discipline") == "Run" and (
        session.get("primary_metric") == "Pace"
        or session.get("daniels_type") in {"T", "I", "R"}
    )


def _quality_ride(session: dict[str, Any]) -> bool:
    if session.get("discipline") != "Ride" or _threshold_test(session):
        return False
    if session.get("progression"):
        return True
    if session.get("coggan_level") in {"L4 Threshold", "L5 VO2max", "L6 Anaerobic"}:
        return True
    return float(session.get("expected_if", 0) or 0) >= 0.78


def _session_reference(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_name": session["session_name"],
        "date": session["date"],
        "day": session["day"],
    }


def _week_similarity(
    sessions: list[dict[str, Any]], prior_plan: dict[str, Any]
) -> float:
    prior_sessions = prior_plan.get("sessions", [])
    if not sessions or not prior_sessions:
        return 0.0
    current = [
        (session.get("day"), session.get("discipline"), session.get("session_name"))
        for session in sessions
        if session.get("discipline") != "Recovery"
    ]
    prior = [
        (session.get("day"), session.get("discipline"), session.get("session_name"))
        for session in prior_sessions
        if session.get("discipline") != "Recovery"
    ]
    if not current or not prior:
        return 0.0
    overlap = sum(1 for item in current if item in prior)
    size = max(len(current), len(prior))
    return overlap / size if size else 0.0


def _is_recovery_week(snapshot: dict[str, Any], macro: dict[str, Any]) -> bool:
    return (
        macro.get("week_classification") in {"recovery", "unload", "holiday"}
        or macro.get("friel_cycle_type") == "recovery"
        or macro.get("macro_phase") == "transition"
        or snapshot.get("planning_constraints", {}).get("requested_week_type")
        == "recovery"
    )


def _is_taper(macro: dict[str, Any]) -> bool:
    return macro.get("macro_phase") == "taper" or macro.get("friel_cycle_type") == "taper"


def _light_strength_week(
    snapshot: dict[str, Any], macro: dict[str, Any], performance: dict[str, Any]
) -> bool:
    """Same rule as the planner's strength prescription (<= LIGHT_STRENGTH_MAX_MIN)."""
    return (
        performance.get("readiness_status") != "green"
        or macro.get("week_classification") in {"absorb", "sharpen"}
        or _is_recovery_week(snapshot, macro)
        or _is_taper(macro)
    )


def _is_long_durability(session: dict[str, Any]) -> bool:
    if _threshold_test(session) or _quality_run(session) or session.get("progression"):
        return False
    if session.get("discipline") == "Ride" and _quality_ride(session):
        return False
    minimum = LONG_SESSION_MIN.get(str(session.get("discipline")))
    return minimum is not None and int(session.get("duration_min", 0) or 0) >= minimum


def _reference_minutes(snapshot: dict[str, Any]) -> int | None:
    """The athlete's typical week (soft reference): profile hours first."""
    hours = safe_float((snapshot.get("athlete_profile", {}) or {}).get("typical_training_hours_per_week"))
    if hours:
        return int(hours * 60)
    constraints = snapshot.get("planning_constraints", {}) or {}
    value = safe_float((constraints.get("weekly_time_budget_policy") or {}).get("reference_min"))
    return int(value) if value else None


def _health_explained_misses(snapshot: dict[str, Any]) -> bool:
    reasons = (snapshot.get("weekly_checkin") or {}).get("missed_session_reasons") or {}
    return isinstance(reasons, dict) and bool(reasons) and all(
        is_health_reason(reason) for reason in reasons.values()
    )


def _check_scheduled_tests_present(
    sessions: list[dict[str, Any]],
    macro: dict[str, Any],
    verdict: str,
    weaknesses: list[str],
    edits: list[dict[str, Any]],
) -> str:
    planned = {
        (session.get("threshold_test") or {}).get("discipline")
        for session in sessions
        if session.get("threshold_test")
    }
    for entry in (macro.get("threshold_tests") or {}).get("scheduled", []):
        if entry.get("discipline") in planned:
            continue
        verdict = _bump_verdict(verdict, "revise")
        weaknesses.append(
            f"The macro coach scheduled a {entry['discipline']} threshold test this recovery week, but the plan does not contain it."
        )
        edits.append(
            {
                "id": f"add-{entry['discipline']}-threshold-test",
                "action": "add_threshold_test",
                "summary": f"Add the {entry['discipline']} threshold test ({entry.get('protocol')}) late in the recovery week.",
            }
        )
    return verdict


def _check_quality_dose(
    quality_sessions: list[dict[str, Any]],
    performance: dict[str, Any],
    recovery_week: bool,
    verdict: str,
    strengths: list[str],
    weaknesses: list[str],
    *,
    sessions: list[dict[str, Any]] | None = None,
    reduced: bool = False,
) -> str:
    """Flag under-dosing as firmly as over-dosing."""
    if recovery_week:
        return verdict
    allowance = performance.get("intensity_allowance") or {}
    maximum = allowance.get("max_quality_sessions")
    if maximum is None:
        return verdict
    # A race in the week counts as its quality session.
    count = len(quality_sessions)
    races = sum(1 for session in sessions or [] if session.get("race_day"))
    underload = (performance.get("underload") or {}).get("status")
    if count > int(maximum):
        verdict = _bump_verdict(verdict, "revise")
        weaknesses.append(
            f"{count} quality sessions exceed the readiness allowance of {maximum}."
        )
    elif performance.get("readiness_status") == "green" and count + races < min(2, int(maximum)) and not reduced:
        verdict = _bump_verdict(verdict, "revise lightly")
        weaknesses.append(
            f"Only {count} quality session(s) in a green load week; the allowance is {maximum}. The week is under-dosed."
        )
    elif underload == "detected" and count < int(maximum):
        verdict = _bump_verdict(verdict, "revise lightly")
        weaknesses.append(
            f"Underload is detected but the week uses {count} of {maximum} allowed quality sessions; add one or extend time-in-zone."
        )
    else:
        strengths.append(
            f"{count} quality session(s) match the readiness allowance of {maximum}."
        )
    return verdict


def _check_threshold_tests(
    sessions: list[dict[str, Any]],
    snapshot: dict[str, Any],
    macro: dict[str, Any],
    performance: dict[str, Any],
    verdict: str,
    strengths: list[str],
    weaknesses: list[str],
) -> str:
    tests = [session for session in sessions if _threshold_test(session)]
    if not tests:
        return verdict
    testing = snapshot.get("threshold_testing", {})
    recovery_week = _is_recovery_week(snapshot, macro)
    block_week_1 = any(
        entry.get("placement") == "block_week_1"
        for entry in (macro.get("threshold_tests") or {}).get("scheduled", [])
    )
    if not recovery_week and not block_week_1:
        verdict = _bump_verdict(verdict, "revise")
        weaknesses.append(
            "Threshold tests belong at the end of a recovery week (Friel rest & test) or in block week 1 "
            "straight after it, not inside a load week."
        )
    if performance.get("readiness_status") == "red":
        verdict = _bump_verdict(verdict, "unsafe")
        weaknesses.append("A maximal threshold test is planned while readiness is red.")
    if len(tests) > 2:
        verdict = _bump_verdict(verdict, "revise")
        weaknesses.append("More than two threshold tests in one week is excessive.")
    test_days = sorted(
        DAY_NAMES.index(session["day"])
        for session in tests
        if session["day"] in DAY_NAMES
    )
    if any(b - a < 2 for a, b in zip(test_days, test_days[1:])):
        verdict = _bump_verdict(verdict, "revise")
        weaknesses.append("Two threshold tests are less than 48h apart.")
    for session in tests:
        discipline = session["threshold_test"].get("discipline")
        blockers = (testing.get(discipline) or {}).get("blockers", [])
        if blockers:
            verdict = _bump_verdict(verdict, "revise")
            weaknesses.append(
                f"{session['session_name']} conflicts with a testing blocker: {blockers[0]}"
            )
        index = DAY_NAMES.index(session["day"])
        neighbours = {
            DAY_NAMES[offset]
            for offset in (index - 1, index + 1)
            if 0 <= offset < len(DAY_NAMES)
        }
        hard_neighbours = [
            other["session_name"]
            for other in sessions
            if other["day"] in neighbours
            and not _threshold_test(other)
            and (_quality_run(other) or _quality_ride(other))
        ]
        if hard_neighbours:
            verdict = _bump_verdict(verdict, "revise lightly")
            weaknesses.append(
                f"{session['session_name']} sits next to hard work ({', '.join(hard_neighbours)}); keep the days around a test easy."
            )
    if recovery_week and verdict in {"keep", "revise lightly"}:
        strengths.append(
            "Threshold testing lands late in the recovery week, fresh and without hard work alongside, so the result is trustworthy."
        )
    return verdict


def run(
    snapshot: dict[str, Any],
    draft_plan: dict[str, Any],
    performance: dict[str, Any],
    macro: dict[str, Any],
    race: dict[str, Any],
) -> dict[str, Any]:
    del race
    constraints = snapshot.get("planning_constraints", {})
    prior_plan_adherence = snapshot.get("prior_plan_adherence", {})
    athlete_memory = snapshot.get("athlete_memory", {})
    sessions = draft_plan.get("sessions", [])
    edits = []
    strengths: list[str] = []
    weaknesses: list[str] = []
    verdict = "keep"

    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    total_minutes = 0
    key_sessions = [session for session in sessions if session.get("priority") == "key"]
    for session in sessions:
        by_day[session["day"]].append(session)
        total_minutes += int(session.get("duration_min", 0) or 0)

    no_bike_days = set(constraints.get("no_bike_days", []))
    violating_bike_days = sorted(
        {
            session["day"]
            for session in sessions
            if session.get("discipline") == "Ride" and session["day"] in no_bike_days
        }
    )
    if violating_bike_days:
        verdict = _bump_verdict(verdict, "revise")
        weaknesses.append(
            f"Bike sessions violate the no-bike rule on {', '.join(violating_bike_days)}."
        )
    elif no_bike_days:
        strengths.append(
            f"The week respects the no-bike constraint on {', '.join(sorted(no_bike_days))}."
        )

    fixed_swim_days = set(constraints.get("fixed_swim_days", []))
    missing_swim_days = sorted(
        day
        for day in fixed_swim_days
        if not any(
            session.get("discipline") == "Swim" for session in by_day.get(day, [])
        )
    )
    if missing_swim_days:
        verdict = _bump_verdict(verdict, "revise")
        weaknesses.append(
            f"Fixed swim routine is missing on {', '.join(missing_swim_days)}."
        )
    elif fixed_swim_days:
        strengths.append(
            f"The fixed swim routine is preserved on {', '.join(sorted(fixed_swim_days))}."
        )

    double_capacity = constraints.get("double_session_capacity_by_day", {})
    double_violations = sorted(
        day
        for day, day_sessions in by_day.items()
        if len(day_sessions) > 1 and double_capacity.get(day) is False
    )
    if double_violations:
        verdict = _bump_verdict(verdict, "revise")
        weaknesses.append(
            f"The plan stacks doubles on {', '.join(double_violations)} even though those days are marked as no-double days."
        )

    time_caps = constraints.get("time_caps_by_day", {})
    for day, cap in time_caps.items():
        day_total = sum(
            int(session.get("duration_min", 0) or 0) for session in by_day.get(day, [])
        )
        if day_total <= int(cap):
            continue
        verdict = _bump_verdict(verdict, "revise lightly")
        weaknesses.append(
            f"{day} totals {day_total} minutes against a {cap}-minute cap."
        )
        support_session = _choose_support_session_for_trim(by_day.get(day, []))
        if support_session is not None:
            edits.append(
                {
                    **_session_reference(support_session),
                    "id": f"trim-{day.lower()}-{support_session['session_name'].lower().replace(' ', '-')}",
                    "action": "shorten_session",
                    "summary": f"Trim {support_session['session_name']} to make {day} fit the stated time cap.",
                    "new_duration_min": max(
                        20,
                        int(support_session.get("duration_min", 0) or 0)
                        - (day_total - int(cap)),
                    ),
                }
            )

    info: list[str] = []
    hard_cap = hard_cap_minutes(snapshot)
    if hard_cap and total_minutes > hard_cap:
        verdict = _bump_verdict(verdict, "revise")
        weaknesses.append(
            f"Planned total {total_minutes} min exceeds the available-hours cap of {hard_cap} min from the check-in; "
            "that cap binds: cut easy volume first, then the secondary quality, and trim the long session last."
        )
    weekly_reference = _reference_minutes(snapshot)
    if weekly_reference and total_minutes > weekly_reference:
        over_pct = round(100.0 * (total_minutes - weekly_reference) / weekly_reference, 1)
        if over_pct > OVER_REFERENCE_RISK_PCT:
            verdict = _bump_verdict(verdict, "revise lightly")
            not_green = performance.get("readiness_status") in {"yellow", "red"}
            weaknesses.append(
                f"Planned total {total_minutes} min is {over_pct}% above the athlete's typical {weekly_reference} min"
                + (" while readiness is not green" if not_green else "")
                + "; with a 9-5 job and ~7 h sleep the extra hours come out of sleep and recovery."
            )
        info.append(
            f"Planned total {total_minutes} min is {over_pct}% above the athlete's {weekly_reference}-min reference; "
            "life load (work, sleep, family) is part of the training load, so check it is realistic."
        )
    baseline = baseline_week_minutes(snapshot)
    trained = sum(
        int(session.get("duration_min", 0) or 0)
        for session in sessions
        if session.get("discipline") != "Recovery" and not session.get("race_day")
    )
    if baseline and trained > baseline * (1 + MAX_WEEKLY_TOTAL_GROWTH_PCT / 100):
        verdict = _bump_verdict(verdict, "revise lightly")
        weaknesses.append(
            f"Planned {trained} min is {round(100 * (trained / baseline - 1), 1)}% above the recent loaded "
            f"weekly average of {baseline:.0f} min; grow by at most ~{MAX_WEEKLY_TOTAL_GROWTH_PCT:g}% a week."
        )

    quality_sessions = [
        session
        for session in sessions
        if _quality_run(session) or _quality_ride(session)
    ]
    recovery_week = _is_recovery_week(snapshot, macro)
    verdict = _check_quality_dose(
        quality_sessions, performance, recovery_week or _is_taper(macro), verdict, strengths, weaknesses,
        sessions=sessions, reduced=reduced_dose_week(snapshot, draft_plan),
    )
    if recovery_week:
        verdict = _check_scheduled_tests_present(
            sessions, macro, verdict, weaknesses, edits
        )
        for session in quality_sessions:
            verdict = _bump_verdict(verdict, "revise")
            weaknesses.append(
                f"{session['session_name']} is still too aggressive for a declared recovery week."
            )
            if _quality_ride(session):
                edits.append(
                    {
                        **_session_reference(session),
                        "id": f"downgrade-ride-{session['date']}",
                        "action": "downgrade_ride_to_endurance",
                        "summary": f"Downgrade {session['session_name']} to endurance so the recovery week does not turn into disguised build work.",
                    }
                )
            elif _quality_run(session):
                edits.append(
                    {
                        **_session_reference(session),
                        "id": f"downgrade-run-{session['date']}",
                        "action": "downgrade_run_to_easy",
                        "summary": f"Downgrade {session['session_name']} to easy aerobic running so the recovery week remains truly restorative.",
                    }
                )

    verdict = _check_threshold_tests(
        sessions, snapshot, macro, performance, verdict, strengths, weaknesses
    )

    key_by_day: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for session in key_sessions:
        if session["day"] in DAY_NAMES and not _threshold_test(session):
            key_by_day[DAY_NAMES.index(session["day"])].append(session)
    ordered_key_days = sorted(key_by_day)
    deliberate_long_pair = False
    stacked = False
    for first, second in zip(ordered_key_days, ordered_key_days[1:]):
        if second - first != 1:
            continue
        pair = key_by_day[first] + key_by_day[second]
        # A planned long ride + long run weekend (or trail back-to-back) is a
        # deliberate durability pair, not stacking, when both stay aerobic.
        if (
            all(_is_long_durability(session) for session in pair)
            and performance.get("readiness_status") == "green"
            and not recovery_week
            and not _is_taper(macro)
        ):
            deliberate_long_pair = True
            continue
        stacked = True
    if stacked:
        verdict = _bump_verdict(verdict, "revise lightly")
        weaknesses.append(
            "Key sessions are stacked on back-to-back days, which is harder to execute well in real life."
        )
    elif deliberate_long_pair:
        strengths.append(
            "The back-to-back long sessions are a deliberate aerobic durability pair, with no quality inside them."
        )


    preferred_long_ride_day = constraints.get("preferred_long_ride_day")
    long_ride = next(
        (
            session
            for session in sessions
            if session.get("discipline") == "Ride"
            and "long" in session.get("session_name", "").lower()
        ),
        None,
    )
    if (
        long_ride
        and preferred_long_ride_day
        and long_ride.get("day") != preferred_long_ride_day
    ):
        verdict = _bump_verdict(verdict, "revise lightly")
        weaknesses.append(
            f"The long ride moved away from the preferred {preferred_long_ride_day} without a strong visible reason."
        )

    if athlete_memory.get("status") == "available":
        strengths.append(athlete_memory["summary"])
        fragile_days = set(athlete_memory.get("fragile_days", []))
        for session in key_sessions:
            if session["day"] in fragile_days:
                verdict = _bump_verdict(verdict, "revise lightly")
                weaknesses.append(
                    f"{session['session_name']} lands on {session['day']}, which recent history marks as a fragile execution day."
                )
        if athlete_memory.get("double_day_profile", {}).get("status") == "fragile":
            risky_double_days = [
                day
                for day, day_sessions in by_day.items()
                if len(day_sessions) > 1 and day not in fixed_swim_days
            ]
            if risky_double_days:
                verdict = _bump_verdict(verdict, "revise lightly")
                weaknesses.append(
                    f"Recent history says double days are fragile, so {', '.join(sorted(risky_double_days))} should stay simple."
                )
                for day in risky_double_days:
                    support_session = _choose_support_session_for_trim(by_day[day])
                    if support_session is not None:
                        edits.append(
                            {
                                **_session_reference(support_session),
                                "id": f"remove-{support_session['date']}-{support_session['session_name'].lower().replace(' ', '-')}",
                                "action": "remove_session",
                                "summary": f"Remove {support_session['session_name']} so {day} stays executable in real life.",
                            }
                        )

    if prior_plan_adherence.get("status") == "available":
        strengths.append(prior_plan_adherence["summary"])
        if _health_explained_misses(snapshot):
            info.append(
                "Last week's misses were illness/injury, not unreliability; they do not lower confidence in the athlete."
            )
        elif prior_plan_adherence.get("adherence_score", 1.0) < 0.7:
            verdict = _bump_verdict(verdict, "revise lightly")
            weaknesses.append(
                "Recent adherence was shaky enough that this week should stay highly executable and uncluttered."
            )

    prior_history = snapshot.get("prior_plan_history", [])
    if prior_history:
        similarity = _week_similarity(sessions, prior_history[0].get("final_plan", {}))
        if similarity >= 0.75:
            verdict = _bump_verdict(verdict, "revise lightly")
            weaknesses.append(
                "This week mirrors the most recent approved week too closely, so it needs a stronger rationale or a clearer change in emphasis."
            )

    strength_session = next(
        (
            session
            for session in sessions
            if session.get("discipline") == "WeightTraining"
        ),
        None,
    )
    if (
        strength_session
        and _light_strength_week(snapshot, macro, performance)
        and int(strength_session.get("duration_min", 0) or 0) > LIGHT_STRENGTH_MAX_MIN
    ):
        verdict = _bump_verdict(verdict, "revise lightly")
        weaknesses.append(
            "Strength work is still a little too ambitious relative to readiness and the rest of the week."
        )
        edits.append(
            {
                **_session_reference(strength_session),
                "id": "reduce-friday-strength",
                "action": "reduce_strength_load",
                "summary": "Convert the gym session into short activation work so it does not steal quality from the weekend.",
                "new_session_name": "Activation Strength",
                "new_duration_min": LIGHT_STRENGTH_MAX_MIN,
            }
        )

    strengths = list(dict.fromkeys(strengths))
    weaknesses = list(dict.fromkeys(weaknesses))
    deduped_edits = []
    seen_edit_ids: set[str] = set()
    for edit in edits:
        if edit["id"] in seen_edit_ids:
            continue
        seen_edit_ids.add(edit["id"])
        deduped_edits.append(edit)

    simpler_alternative = "If life stress spikes, keep the main bike, the main run, and the long ride, and make the extra support sessions purely restorative."
    if verdict == "keep" and not weaknesses:
        strengths.append(
            "The week is realistic to execute without needing heroics or perfect scheduling."
        )

    return {
        "agent": "validator_coach",
        "strengths": strengths,
        "weaknesses": weaknesses,
        "proposed_edits": deduped_edits,
        "simpler_alternative": simpler_alternative,
        "verdict": verdict,
        "info": list(dict.fromkeys(info)),
    }
