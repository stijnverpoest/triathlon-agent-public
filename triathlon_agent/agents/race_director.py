from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from ..coaching_limits import (
    B_RACE_LAST_QUALITY_H,
    post_race_easy_days,
    pre_race_light_days,
)
from ..race_selection import (
    DISTANCE_LABELS,
    calendar_races,
    distance_bucket,
    race_bucket,
    race_distance_km,
    select_priority_race,
    taper_days,
    trail_distance_unknown,
)

# Tune-up window: how many days before an A-race a B/C race is most useful.
_TUNE_UP_MIN_DAYS_BEFORE = 14
_TUNE_UP_MAX_DAYS_BEFORE = 35

# Fitness-test window: how far ahead to look for a build-phase race opportunity.
_FITNESS_TEST_LOOKAHEAD = 28

TRIATHLON_BUCKETS = {"sprint", "olympic", "70.3", "ironman"}
TRAIL_BUCKETS = {"trail", "ultra_trail"}

_TUNE_UP_DISTANCE = {
    "70.3": "Olympic",
    "ironman": "70.3",
    "olympic": "Sprint",
    "sprint": "Sprint",
    "ultra_trail": "Trail 25-35k",
    "trail": "Trail 10-15k or road 10k",
    "marathon": "Half Marathon",
    "half_marathon": "10k",
    "10k": "5k",
    "5k": "5k / parkrun",
}

# Short race that works as a mid-block fitness check without costing a week.
_FITNESS_TEST_DISTANCE = {
    "70.3": "Sprint or Olympic triathlon",
    "ironman": "Olympic triathlon or 70.3 at training effort",
    "olympic": "Sprint triathlon",
    "sprint": "Sprint triathlon or 5k",
    "ultra_trail": "Trail 15-25k or a road 10k",
    "trail": "Trail 10-15k or a road 10k",
    "marathon": "10k or Half Marathon",
    "half_marathon": "5k or 10k",
    "10k": "5k",
    "5k": "5k / parkrun",
}

_FITNESS_TEST_RECOVERY = {
    "ultra_trail": "a 15-25k trail costs 3-4 easy days, not a training week",
    "trail": "a short trail or 10k costs 2-3 easy days",
    "marathon": "a 10k costs ~3 easy days",
    "half_marathon": "a 5k-10k costs ~48-72h",
}

RACE_NEEDS: dict[str, dict[str, Any]] = {
    "olympic": {
        "needs": [
            "sustained bike power at controlled discomfort",
            "run durability off a moderate fatigue background",
            "repeatable race fueling habits for 2 to 3 hour work",
        ],
        "mistake": "adding too much top-end intensity instead of building repeatable race rhythm",
        "locations": [
            "The main ride should touch sustained, race-relevant power when the week is not a recovery week.",
            "The long ride should keep reinforcing controlled endurance and fueling habits.",
            "The main run should reinforce threshold discipline rather than raw speed when intensity is present.",
        ],
    },
    "sprint": {
        "needs": [
            "high power and VO2max on the bike",
            "fast transitions and running off the bike from the gun",
            "a sharp, controlled swim start",
        ],
        "mistake": "training only long and steady when the race is decided above threshold",
        "locations": [
            "VO2/threshold bike intervals carry the race demand.",
            "Short bricks rehearse running hard off the bike.",
        ],
    },
    "70.3": {
        "needs": [
            "strong aerobic durability across all three disciplines",
            "bike power that stays controlled enough to unlock the run",
            "race-day fueling execution over 4 to 5 hours",
            "steady half-marathon pacing under fatigue",
        ],
        "mistake": "treating the week like short-course training and overloading intensity instead of durability",
        "locations": [
            "The main ride should reinforce pacing discipline and fueling under controlled pressure.",
            "Weekend endurance plus a brick run keeps the bike-to-run durability chain alive.",
            "Run quality should stay economical and durable rather than overly sharp.",
        ],
    },
    "ironman": {
        "needs": [
            "very long aerobic durability on the bike and run",
            "low-variability bike pacing well under threshold",
            "gut tolerance for 60-90 g carbohydrate per hour for 10+ hours",
            "marathon run-walk discipline off the bike",
        ],
        "mistake": "chasing threshold numbers instead of long steady durability and fueling",
        "locations": [
            "The long ride and brick are the key sessions; intensity serves them.",
            "Fueling is trained in every long session, not only on race day.",
        ],
    },
    "trail": {
        "needs": [
            "climbing strength: uphill threshold and power-hiking",
            "eccentric quad strength and downhill skill",
            "aerobic durability on hilly, technical terrain",
            "race-pace fueling and kit rehearsal on long runs",
        ],
        "mistake": "training only flat road pace and arriving with no climbing or descending legs",
        "locations": [
            "Uphill repeats (or treadmill incline / stairs on flat terrain) carry the climbing demand.",
            "The weekend long run builds vertical gain and controlled descending progressively.",
            "Strength work includes eccentric/downhill prep (step-downs, eccentric squats).",
        ],
    },
    "ultra_trail": {
        "needs": [
            "long aerobic durability: time on feet over 4-7 hours",
            "power-hiking and climbing economy on steep grades",
            "eccentric quad resilience for long descents",
            "gut training to 60-90 g carbohydrate per hour with race products",
            "back-to-back weekend fatigue resistance",
        ],
        "mistake": "running long flat road miles fast instead of building time on feet, vertical and descending legs",
        "locations": [
            "Back-to-back weekend runs (Saturday medium-long, Sunday long) build fatigue resistance.",
            "Uphill threshold and power-hiking on climbs, treadmill incline or stairs when the terrain is flat.",
            "Progressive downhill exposure plus eccentric strength protects the quads (and the ITB).",
            "Every long run rehearses race fueling and kit.",
        ],
    },
    "half_marathon": {
        "needs": [
            "lactate threshold pace (Daniels T) held for longer segments",
            "economical half-marathon race pace",
            "long-run durability to 100-120 minutes",
        ],
        "mistake": "racing every session instead of building threshold and steady race-pace volume",
        "locations": [
            "T cruise intervals and race-pace segments carry the specificity.",
            "The long run ends with steady race-pace minutes on tired legs.",
        ],
    },
    "marathon": {
        "needs": [
            "marathon-pace durability over long segments",
            "glycogen economy and in-race fueling (60-90 g/h)",
            "long-run volume to 2.5-3 hours",
        ],
        "mistake": "too much fast running and too little marathon-pace and long-run durability",
        "locations": [
            "Marathon-pace blocks inside the long run are the key session.",
            "T work supports, it does not replace race-pace volume.",
        ],
    },
    "10k": {
        "needs": ["economical threshold running", "VO2max intervals", "pace discipline", "fresh calves"],
        "mistake": "only steady running without VO2max and threshold sharpening",
        "locations": [
            "The main run should stay economical and pace-controlled.",
            "The bike should support durability without stealing freshness from the run.",
        ],
    },
    "5k": {
        "needs": ["VO2max and repetition speed", "threshold support", "running economy"],
        "mistake": "too much slow volume and no sharp repetition work",
        "locations": ["I and R sessions carry the race demand; keep the rest easy."],
    },
}


def _tune_up_distance(bucket: str) -> str:
    return _TUNE_UP_DISTANCE.get(bucket, "Olympic")


def _tune_up_purpose(bucket: str, a_name: str) -> str:
    if bucket in {"70.3", "ironman"}:
        return (
            "Full race-day rehearsal of bike pacing, on-bike fueling, and run execution "
            f"under fatigue: confirms your race-day systems before {a_name} without "
            "requiring a full long-course recovery hit."
        )
    if bucket == "olympic":
        return (
            "Race-pace rehearsal and brick transition practice at a lower fatigue cost "
            "than an Olympic: sharpens race-day execution without deep recovery debt."
        )
    if bucket in TRAIL_BUCKETS:
        return (
            "Shorter trail race as the last long effort: rehearses fueling, kit, "
            "climbing and descending on race terrain without an ultra-sized recovery hit."
        )
    if bucket in {"marathon", "half_marathon", "10k", "5k"}:
        return "Shorter road race to confirm race pace, pre-race routine and fueling without a long recovery."
    return "Race rehearsal at a shorter distance to confirm fitness and pace feel."


def _build_race_recommendations(
    snapshot: dict[str, Any],
    macro: dict[str, Any],
    week_start: date,
) -> list[dict[str, Any]]:
    """Race suggestions; `days_to_race` is relative to `week_start` (normalizer)."""
    upcoming = sorted(
        [r for r in calendar_races(snapshot) if r.get("days_to_race", 9999) >= 0],
        key=lambda r: r.get("days_to_race", 9999),
    )
    a_races = [r for r in upcoming if r.get("priority") == "A"]
    b_c_races = [r for r in upcoming if r.get("priority") in ("B", "C")]
    phase = macro.get("macro_phase", "build")
    recommendations: list[dict[str, Any]] = []

    seen_tune_up_for: set[str] = set()

    for a_race in a_races:
        days_to_a = a_race.get("days_to_race", 9999)
        a_bucket = race_bucket(a_race)
        a_name = a_race.get("name", "A-race")

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
                "suggested_distance": race_bucket(covering_race),
                "enhancement_rationale": (
                    f"{covering_race.get('name')} falls {days_to_a - covering_race.get('days_to_race', 0)} "
                    f"days before {a_name}: ideal tune-up timing. Treat it as a B-race: "
                    "full race-day execution after a mini-taper (3-4 lighter days, last quality "
                    "72h+ before), then an easy day and 2-4 easy days scaled by race duration."
                ),
            })
            seen_tune_up_for.add(a_name)
        elif days_to_a >= _TUNE_UP_MIN_DAYS_BEFORE and a_name not in seen_tune_up_for:
            window_start = week_start + timedelta(days=days_to_a - _TUNE_UP_MAX_DAYS_BEFORE)
            window_end = week_start + timedelta(days=days_to_a - _TUNE_UP_MIN_DAYS_BEFORE)
            recommendations.append({
                "type": "tune_up",
                "for_race": a_name,
                "covered_by": None,
                "suggested_distance": _tune_up_distance(a_bucket),
                "ideal_window": f"{window_start.strftime('%b %d')} - {window_end.strftime('%b %d')}",
                "enhancement_rationale": _tune_up_purpose(a_bucket, a_name),
            })
            seen_tune_up_for.add(a_name)

    # --- Mid-block fitness test ---
    nearest_a = a_races[0] if a_races else None
    nearest_a_days = nearest_a.get("days_to_race", 9999) if nearest_a else 9999
    nearest_a_name = nearest_a.get("name") if nearest_a else None
    nearest_bucket = race_bucket(nearest_a) if nearest_a else "unknown"
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
        recovery_cost = _FITNESS_TEST_RECOVERY.get(
            nearest_bucket, "a short race costs ~48h recovery, not a full training week"
        )
        recommendations.append({
            "type": "fitness_test",
            "for_race": nearest_a_name,
            "covered_by": None,
            "suggested_distance": _FITNESS_TEST_DISTANCE.get(nearest_bucket, "Sprint"),
            "ideal_window": f"next {_FITNESS_TEST_LOOKAHEAD} days",
            "enhancement_rationale": (
                "A short race during the build block acts as a high-quality training "
                "stimulus with real race effort: calibrates current pacing benchmarks, "
                "tests race-day logistics, and provides a mental anchor for the bigger "
                f"races ahead; {recovery_cost}."
            ),
        })

    return recommendations


def _week_start(snapshot: dict[str, Any]) -> date:
    for key in ("week_start", "planning_date"):
        try:
            return date.fromisoformat(str(snapshot.get(key))[:10])
        except ValueError:
            continue
    return date.today()


def race_duration_minutes(race: dict[str, Any]) -> int:
    """Rough race duration for recovery scaling (trail ~8.5 min/km)."""
    bucket = race_bucket(race)
    km = race_distance_km(race.get("distance"), race.get("name"), race.get("notes"))
    fixed = {
        "sprint": 80,
        "olympic": 150,
        "70.3": 330,
        "ironman": 720,
        "5k": 25,
        "10k": 50,
        "half_marathon": 110,
        "marathon": 240,
    }
    if bucket in TRAIL_BUCKETS:
        return int(round((km or (50 if bucket == "ultra_trail" else 25)) * 8.5 / 5.0) * 5)
    return fixed.get(bucket, 90)


def _b_c_race_handling(
    snapshot: dict[str, Any], priority_race: dict[str, Any]
) -> list[dict[str, Any]]:
    """How each upcoming B/C race on the calendar is trained around."""
    handling = []
    for race in calendar_races(snapshot):
        days = race.get("days_to_race")
        if days is None or int(days) < 0 or race.get("priority") not in {"B", "C"}:
            continue
        if race.get("name") == priority_race.get("name"):
            continue
        duration = race_duration_minutes(race)
        priority = race.get("priority")
        easy_after = post_race_easy_days(priority, duration)
        light_before = pre_race_light_days(priority)
        if priority == "B":
            rule = (
                f"Mini-taper: {light_before}-{light_before + 1} lighter days, last quality "
                f"{B_RACE_LAST_QUALITY_H}h+ before, an opener the day before; then "
                f"{easy_after} easy days, scaled to ~{duration} min of racing."
            )
        else:
            rule = (
                f"Train through: nothing hard the day before, {easy_after} easy day(s) after "
                f"(~{duration} min of racing)."
            )
        handling.append(
            {
                "name": race.get("name"),
                "priority": priority,
                "days_to_race": int(days),
                "estimated_duration_min": duration,
                "light_days_before": light_before,
                "easy_days_after": easy_after,
                "rule": rule,
            }
        )
    return sorted(handling, key=lambda item: item["days_to_race"])


PROFILE_GOAL_FIELDS = (
    "target_race_distance",
    "primary_performance_goal",
    "secondary_goal",
    "goals",
    "target_races",
    "race_goals",
)
RACE_WORDS = (
    "race",
    "trail",
    "marathon",
    "triathlon",
    "ironman",
    "70.3",
    "ultra",
    "10k",
    "5k",
    "olympic",
    "sprint",
)
STOP_WORDS = {"race", "trail", "with", "from", "into", "deeper", "hold", "build", "the"}


def _profile_race_notes(snapshot: dict[str, Any]) -> list[str]:
    """Races named in the profile but absent from the Intervals calendar: ignored, said so.

    Races and goals come only from the Intervals calendar; profile text goes
    stale, so it is never planned for.
    """
    profile = snapshot.get("athlete_profile") or {}
    races = calendar_races(snapshot)
    calendar_text = " ".join(
        f"{race.get('name') or ''} {race.get('distance') or ''}".lower() for race in races
    )
    calendar_buckets = {race_bucket(race) for race in races}
    notes: list[str] = []
    for field in PROFILE_GOAL_FIELDS:
        value = profile.get(field)
        for item in value if isinstance(value, list) else [value]:
            text = str(item or "").strip()
            lowered = text.lower()
            if not text:
                continue
            racy = any(word in lowered for word in RACE_WORDS) or distance_bucket(text) in DISTANCE_LABELS
            if not racy:
                continue
            words = [
                word.strip(".,;:()'\"")
                for word in lowered.split()
                if len(word.strip(".,;:()'\"")) >= 4 and word.strip(".,;:()'\"") not in STOP_WORDS
            ]
            named = any(word in calendar_text for word in words)
            if named or distance_bucket(text) in calendar_buckets:
                continue
            notes.append(
                f"The profile mentions '{text}', which is not on your Intervals calendar; ignored. "
                "Add it as a RACE event in Intervals.icu if it is a real goal."
            )
    return list(dict.fromkeys(notes))


def _taper_notes(priority_race: dict[str, Any], bucket: str) -> str:
    days = int(priority_race.get("days_to_race", 9999))
    if days >= 9999:
        return "No target race on the calendar; no taper planned."
    window = taper_days(priority_race)
    label = DISTANCE_LABELS.get(bucket, "race")
    if days <= 6:
        return (
            f"Race week ({label}): volume ~40-50% of build, one short opener with a few race-pace "
            "minutes, nothing hard the day before, race-day fueling and kit fixed."
        )
    if days <= window:
        return (
            f"Taper ({window} days for a {label}): cut volume progressively to ~60-75% of build "
            "and shorten the long sessions ~40%, but keep short intensity touches so the legs stay sharp."
        )
    return (
        f"No taper yet; the {label} taper starts {window} days before {priority_race.get('name', 'the race')} "
        f"(in {days - window} days). Stay in build-to-absorb territory."
    )


def run(snapshot: dict[str, Any], macro: dict[str, Any]) -> dict[str, Any]:
    priority_race = select_priority_race(snapshot) or {
        "name": "No target race",
        "days_to_race": 9999,
        "distance": "unknown",
        "priority": "C",
    }
    bucket = race_bucket(priority_race)
    week_classification = macro.get("week_classification", "build")
    profile = RACE_NEEDS.get(bucket, {})
    needs = list(profile.get("needs", []))
    mistake = profile.get("mistake", "generic training disconnected from event demands")
    specificity_locations: list[str] = list(profile.get("locations", []))

    if week_classification in {"recovery", "holiday"}:
        mistake = "turning a recovery week into a disguised build week"
        specificity_locations = [
            "Keep the main ride clearly aerobic while preserving feel for the race demands ahead.",
            "Use the weekend to preserve durability without adding race-rhythm density.",
            "Let the run stay easy enough that freshness, not sharpness, is the output of the week.",
        ]

    race_recommendations = _build_race_recommendations(
        snapshot, macro, _week_start(snapshot)
    )
    info = _profile_race_notes(snapshot)
    if not select_priority_race(snapshot):
        info.append("No race on your Intervals calendar: planning a general base week.")
    if trail_distance_unknown(priority_race):
        info.append(
            f"{priority_race.get('name', 'The race')} has no distance on the Intervals calendar: "
            "planned as a medium trail race; add the distance to the event for a precise build."
        )

    return {
        "agent": "race_director",
        "priority_race": priority_race,
        "priority_race_bucket": bucket,
        "priority_race_lens": f"{priority_race.get('name')} in {priority_race.get('days_to_race')} days",
        "race_specific_needs": needs,
        "mistake_to_avoid": mistake,
        "specificity_locations": specificity_locations,
        "taper_notes": _taper_notes(priority_race, bucket),
        "race_recommendations": race_recommendations,
        "b_c_race_handling": _b_c_race_handling(snapshot, priority_race),
        "info": info,
    }
