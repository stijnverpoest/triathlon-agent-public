from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from ..race_selection import distance_bucket, select_priority_race

# Tune-up window: how many days before an A-race a B/C race is most useful.
_TUNE_UP_MIN_DAYS_BEFORE = 14
_TUNE_UP_MAX_DAYS_BEFORE = 35

# Fitness-test window: how far ahead to look for a build-phase race opportunity.
_FITNESS_TEST_LOOKAHEAD = 28


def _tune_up_distance(a_distance: str) -> str:
    if a_distance == "70.3":
        return "Olympic"
    if a_distance == "olympic":
        return "Sprint"
    if a_distance == "sprint":
        return "Sprint"
    return "Olympic"


def _tune_up_purpose(a_distance: str) -> str:
    if a_distance == "70.3":
        return (
            "Full race-day rehearsal of bike pacing, on-bike fueling, and run execution "
            "under fatigue — confirms your race-day systems before the A-race without "
            "requiring a full 70.3 recovery hit."
        )
    if a_distance == "olympic":
        return (
            "Race-pace rehearsal and brick transition practice at a lower fatigue cost "
            "than an Olympic — sharpens race-day execution without deep recovery debt."
        )
    return "Race rehearsal at a shorter distance to confirm fitness and pace feel."


def _build_race_recommendations(
    snapshot: dict[str, Any],
    macro: dict[str, Any],
    planning_date: date,
) -> list[dict[str, Any]]:
    upcoming = sorted(
        [r for r in snapshot.get("races", []) if r.get("days_to_race", 9999) >= 0],
        key=lambda r: r.get("days_to_race", 9999),
    )
    a_races = [r for r in upcoming if r.get("priority") == "A"]
    b_c_races = [r for r in upcoming if r.get("priority") in ("B", "C")]
    phase = macro.get("macro_phase", "build")
    recommendations: list[dict[str, Any]] = []

    seen_tune_up_for: set[str] = set()

    for a_race in a_races:
        days_to_a = a_race.get("days_to_race", 9999)
        a_dist = distance_bucket(a_race.get("distance", ""))
        a_name = a_race.get("name", "A-race")

        # --- Tune-up check ---
        # Is there already a B/C or lower-A race in the tune-up window?
        covering_race = next(
            (
                r
                for r in upcoming
                if r.get("name") != a_name
                and _TUNE_UP_MIN_DAYS_BEFORE
                <= (days_to_a - r.get("days_to_race", 0))
                <= _TUNE_UP_MAX_DAYS_BEFORE
            ),
            None,
        )
        if covering_race:
            recommendations.append({
                "type": "tune_up",
                "for_race": a_name,
                "covered_by": covering_race.get("name"),
                "covered_by_days_to_race": covering_race.get("days_to_race"),
                "suggested_distance": distance_bucket(covering_race.get("distance", "")),
                "enhancement_rationale": (
                    f"{covering_race.get('name')} falls {days_to_a - covering_race.get('days_to_race', 0)} "
                    f"days before {a_name} — ideal tune-up timing. Treat it as a B-race: "
                    f"full race-day execution without a taper."
                ),
            })
            seen_tune_up_for.add(a_name)
        elif days_to_a >= _TUNE_UP_MIN_DAYS_BEFORE and a_name not in seen_tune_up_for:
            suggested = _tune_up_distance(a_dist)
            window_start = planning_date + timedelta(days=days_to_a - _TUNE_UP_MAX_DAYS_BEFORE)
            window_end = planning_date + timedelta(days=days_to_a - _TUNE_UP_MIN_DAYS_BEFORE)
            recommendations.append({
                "type": "tune_up",
                "for_race": a_name,
                "covered_by": None,
                "suggested_distance": suggested,
                "ideal_window": f"{window_start.strftime('%b %d')} – {window_end.strftime('%b %d')}",
                "enhancement_rationale": _tune_up_purpose(a_dist),
            })
            seen_tune_up_for.add(a_name)

    # --- Mid-block fitness test ---
    # Recommend a short race if in base/build phase, first A-race is far enough away
    # that the tune-up window hasn't opened yet, and there's no race in the near window.
    # Skip if we already recommended an uncovered tune-up for the nearest A-race (same ask).
    nearest_a_days = a_races[0].get("days_to_race", 9999) if a_races else 9999
    nearest_a_name = a_races[0].get("name") if a_races else None
    already_recommended_tune_up = any(
        r["type"] == "tune_up"
        and r.get("for_race") == nearest_a_name
        and r.get("covered_by") is None
        for r in recommendations
    )
    near_b_c = next(
        (r for r in b_c_races if r.get("days_to_race", 9999) <= _FITNESS_TEST_LOOKAHEAD),
        None,
    )
    if (
        phase in ("base", "build")
        and nearest_a_days > _TUNE_UP_MAX_DAYS_BEFORE
        and near_b_c is None
        and not already_recommended_tune_up
    ):
        recommendations.append({
            "type": "fitness_test",
            "for_race": a_races[0].get("name") if a_races else None,
            "covered_by": None,
            "suggested_distance": "Sprint",
            "ideal_window": f"next {_FITNESS_TEST_LOOKAHEAD} days",
            "enhancement_rationale": (
                "A short race during the build block acts as a high-quality training "
                "stimulus with real race effort — calibrates current pacing benchmarks, "
                "tests race-day logistics, and provides a mental anchor for the bigger "
                "races ahead. A sprint costs ~48h recovery, not a full training week."
            ),
        })

    return recommendations


def run(snapshot: dict[str, Any], macro: dict[str, Any]) -> dict[str, Any]:
    priority_race = select_priority_race(snapshot) or {
        "name": "No target race",
        "days_to_race": 9999,
        "distance": "unknown",
        "priority": "C",
    }
    distance = priority_race.get("distance", "Olympic").lower()
    week_classification = macro.get("week_classification", "build")
    needs = []
    mistake = "generic training disconnected from event demands"
    specificity_locations: list[str] = []
    if "olympic" in distance:
        needs = [
            "sustained bike power at controlled discomfort",
            "run durability off a moderate fatigue background",
            "repeatable race fueling habits for 2 to 3 hour work",
        ]
        mistake = "adding too much top-end intensity instead of building repeatable race rhythm"
        specificity_locations = [
            "The main ride should touch sustained, race-relevant power when the week is not a recovery week.",
            "The long ride should keep reinforcing controlled endurance and fueling habits.",
            "The main run should reinforce threshold discipline rather than raw speed when intensity is present.",
        ]
    elif "70.3" in distance or "half" in distance or "middle" in distance:
        needs = [
            "strong aerobic durability across all three disciplines",
            "bike power that stays controlled enough to unlock the run",
            "race-day fueling execution over 4 to 5 hours",
            "steady half-marathon pacing under fatigue",
        ]
        mistake = "treating the week like short-course training and overloading intensity instead of durability"
        specificity_locations = [
            "The main ride should reinforce pacing discipline and fueling under controlled pressure.",
            "Weekend endurance should keep the bike-to-run durability chain alive.",
            "Run quality should stay economical and durable rather than overly sharp.",
        ]
    elif "10k" in distance:
        needs = ["economical threshold running", "pace discipline", "fresh calves"]
        specificity_locations = [
            "The main run should stay economical and pace-controlled.",
            "The bike should support durability without stealing freshness from the run.",
        ]

    if week_classification == "recovery":
        mistake = "turning a recovery week into a disguised build week"
        specificity_locations = [
            "Keep the main ride clearly aerobic while preserving feel for the race demands ahead.",
            "Use the weekend to preserve durability without adding race-rhythm density.",
            "Let the run stay easy enough that freshness, not sharpness, is the output of the week.",
        ]

    try:
        planning_date = date.fromisoformat(str(snapshot.get("planning_date", date.today())))
    except ValueError:
        planning_date = date.today()

    race_recommendations = _build_race_recommendations(snapshot, macro, planning_date)

    return {
        "agent": "race_director",
        "priority_race": priority_race,
        "priority_race_lens": f"{priority_race.get('name')} in {priority_race.get('days_to_race')} days",
        "race_specific_needs": needs,
        "mistake_to_avoid": mistake,
        "specificity_locations": specificity_locations,
        "taper_notes": "No taper yet; stay in build-to-absorb territory.",
        "race_recommendations": race_recommendations,
    }
