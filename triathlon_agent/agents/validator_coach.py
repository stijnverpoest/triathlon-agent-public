from __future__ import annotations

from collections import defaultdict
from typing import Any


DAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
VERDICT_RANK = {"keep": 0, "revise lightly": 1, "revise": 2, "unsafe": 3}


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


def _quality_run(session: dict[str, Any]) -> bool:
    return session.get("discipline") == "Run" and (
        session.get("primary_metric") == "Pace"
        or session.get("daniels_type") in {"T", "I", "R"}
    )


def _quality_ride(session: dict[str, Any]) -> bool:
    if session.get("discipline") != "Ride":
        return False
    if session.get("coggan_level") in {"L4 Threshold", "L5 VO2max", "L6 Anaerobic"}:
        return True
    if float(session.get("expected_if", 0) or 0) >= 0.78:
        return True
    if (
        session.get("priority") == "key"
        and "endurance" not in str(session.get("session_name", "")).lower()
    ):
        return True
    return False


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

    weekly_cap = constraints.get("available_hours_override_min")
    if weekly_cap not in (None, "") and total_minutes > int(weekly_cap):
        verdict = _bump_verdict(verdict, "revise lightly")
        weaknesses.append(
            f"Planned total is {total_minutes} minutes against a {weekly_cap}-minute weekly cap."
        )
    elif weekly_cap not in (None, ""):
        strengths.append("The total planned time fits the stated weekly time budget.")

    quality_sessions = [
        session
        for session in sessions
        if _quality_run(session) or _quality_ride(session)
    ]
    if macro.get("week_classification") == "recovery":
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

    ordered_key_days = [
        DAY_NAMES.index(session["day"])
        for session in key_sessions
        if session["day"] in DAY_NAMES
    ]
    ordered_key_days.sort()
    if any(b - a == 1 for a, b in zip(ordered_key_days, ordered_key_days[1:])):
        verdict = _bump_verdict(verdict, "revise lightly")
        weaknesses.append(
            "Key sessions are stacked on back-to-back days, which is harder to execute well in real life."
        )

    if len(quality_sessions) >= 3 and macro.get("week_classification") != "build":
        verdict = _bump_verdict(verdict, "revise lightly")
        weaknesses.append(
            "There is too much moderate-to-hard density for a non-build week."
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
        if prior_plan_adherence.get("adherence_score", 1.0) < 0.7:
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
        and (
            performance.get("readiness_status") != "green"
            or macro.get("week_classification") in {"recovery", "absorb"}
        )
        and int(strength_session.get("duration_min", 0) or 0) > 25
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
                "new_duration_min": 25,
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
    }
