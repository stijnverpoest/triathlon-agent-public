from __future__ import annotations

import re
from typing import Any


# Distance buckets shared by the normalizer, macro coach, race director, planner
# and progression ladders. Keep the bucket names stable: they are contract keys.
DISTANCE_BUCKETS = (
    "sprint",
    "olympic",
    "70.3",
    "ironman",
    "5k",
    "10k",
    "half_marathon",
    "marathon",
    "trail",
    "ultra_trail",
    "unknown",
)
DISTANCE_LABELS = {
    "sprint": "Sprint",
    "olympic": "Olympic",
    "70.3": "Ironman 70.3",
    "ironman": "Ironman",
    "5k": "5k",
    "10k": "10k",
    "half_marathon": "Half Marathon",
    "marathon": "Marathon",
    "trail": "Trail",
    "ultra_trail": "Ultra Trail",
}
# A trail race at or beyond marathon distance is treated as an ultra build.
ULTRA_TRAIL_MIN_KM = 42.0
# Taper length (days before the race) per bucket. Longer events carry more
# residual fatigue from the peak block, so they taper longer.
TAPER_DAYS_BY_BUCKET = {
    "sprint": 7,
    "5k": 7,
    "10k": 10,
    "olympic": 10,
    "half_marathon": 10,
    "trail": 10,
    "70.3": 14,
    "marathon": 18,
    "ultra_trail": 18,
    "ironman": 21,
    "unknown": 14,
}
# Post-race transition (easy, unstructured) days per bucket.
TRANSITION_DAYS_BY_BUCKET = {
    "sprint": 3,
    "5k": 3,
    "10k": 4,
    "olympic": 5,
    "half_marathon": 7,
    "trail": 7,
    "70.3": 10,
    "marathon": 14,
    "ultra_trail": 14,
    "ironman": 21,
    "unknown": 7,
}

KM_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:k|km)\b")
TRAIL_TOKENS = ("trail", "skyrace", "sky race", "ultra", "utmb")


def race_distance_km(*values: Any) -> float | None:
    """Largest explicit kilometre distance mentioned in the texts, if any."""
    text = " ".join(str(value or "") for value in values).lower()
    distances = []
    for match in KM_RE.finditer(text):
        try:
            distances.append(float(match.group(1).replace(",", ".")))
        except ValueError:
            continue
    return max(distances) if distances else None


def distance_bucket(*values: Any) -> str:
    """Classify a race by its distance/name/notes text into a stable bucket."""
    text = " ".join(str(value or "") for value in values).strip().lower()
    if not text:
        return "unknown"
    km = race_distance_km(text)
    padded = f" {text} "
    if any(token in padded for token in TRAIL_TOKENS) or "trailrun" in text:
        if "ultra" in text or (km is not None and km >= ULTRA_TRAIL_MIN_KM):
            return "ultra_trail"
        # A trail race with no stated distance (or a shorter one) gets the
        # neutral, medium trail bucket; `trail_distance_unknown` says so.
        return "trail"
    # "half marathon" must win over the bare "half" (half-iron) match.
    if "half marathon" in text or "halve marathon" in text or "semi-marathon" in text:
        return "half_marathon"
    if "70.3" in text or "half iron" in text or "half-iron" in text or "middle" in text:
        return "70.3"
    if "140.6" in text or "full distance" in text or (
        "ironman" in text and "70.3" not in text
    ):
        return "ironman"
    if "half" in text and any(token in text for token in ("tri", "iron", "duathlon")):
        return "70.3"
    if "half" in text:
        # A bare "half" (e.g. "Coastal Half") is a half-iron by convention.
        return "70.3" if km is None or km > 30 else "half_marathon"
    if "marathon" in text:
        return "marathon"
    if "olympic" in text or "standard distance" in text:
        return "olympic"
    if "sprint" in text:
        return "sprint"
    if km is not None:
        if km >= 42:
            return "marathon"
        if km >= 20:
            return "half_marathon"
        if km >= 9:
            return "10k"
        return "5k"
    return text


def trail_distance_unknown(race: dict[str, Any] | None) -> bool:
    """A trail race whose distance is not stated anywhere (bucket is a guess)."""
    if not race:
        return False
    texts = (race.get("distance"), race.get("name"), race.get("notes"))
    if race_bucket(race) not in {"trail", "ultra_trail"}:
        return False
    if "ultra" in " ".join(str(value or "") for value in texts).lower():
        return False
    return race_distance_km(*texts) is None and not race.get("distance_km")


def race_bucket(race: dict[str, Any] | None) -> str:
    """Bucket a race dict using its distance, name and notes together."""
    if not race:
        return "unknown"
    bucket = distance_bucket(race.get("distance"))
    if bucket in DISTANCE_BUCKETS and bucket != "unknown":
        # A generic sport label ("Run", "TrailRun") is refined by the name/notes.
        if bucket not in {"ultra_trail", "trail"} or race_distance_km(
            race.get("distance")
        ):
            return bucket
    combined = distance_bucket(
        race.get("distance"), race.get("name"), race.get("notes")
    )
    if combined in DISTANCE_BUCKETS:
        return combined
    return bucket if bucket in DISTANCE_BUCKETS else "unknown"


def distance_label(*values: Any) -> str | None:
    bucket = distance_bucket(*values)
    return DISTANCE_LABELS.get(bucket)


def taper_days(race: dict[str, Any] | None) -> int:
    return TAPER_DAYS_BY_BUCKET.get(race_bucket(race), 14)


def transition_days(race: dict[str, Any] | None) -> int:
    return TRANSITION_DAYS_BY_BUCKET.get(race_bucket(race), 7)


def calendar_races(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Races from the Intervals.icu calendar only (`snapshot["races"]`).

    Races and goals always come from the calendar: the athlete profile, the
    persisted athlete state and other local memory go stale and are never a
    race source.
    """
    return [race for race in snapshot.get("races", []) or [] if isinstance(race, dict)]


def select_priority_race(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Nearest upcoming A-race on the Intervals calendar; then B, then C.

    Only `snapshot["races"]` (built from Intervals RACE events) counts: the
    profile target distance never breaks ties or fills gaps.
    """
    upcoming = [
        race
        for race in calendar_races(snapshot)
        if race.get("days_to_race", 9999) is not None
        and race.get("days_to_race", 9999) >= 0
    ]
    if not upcoming:
        return None
    priority_rank = {"A": 0, "B": 1, "C": 2}

    def sort_key(item: dict[str, Any]) -> tuple[int, int]:
        return (
            priority_rank.get(str(item.get("priority") or "C"), 3),
            int(item.get("days_to_race", 9999)),
        )

    return sorted(upcoming, key=sort_key)[0]


def last_completed_a_race(
    snapshot: dict[str, Any], lookback_days: int = 28
) -> dict[str, Any] | None:
    """Most recent A/B race in the past `lookback_days`, used for transition weeks."""
    past = [
        race
        for race in calendar_races(snapshot)
        if race.get("days_to_race") is not None
        and -lookback_days <= race.get("days_to_race", 9999) < 0
        and race.get("priority") in {"A", "B"}
    ]
    if not past:
        return None
    return max(past, key=lambda item: item.get("days_to_race", -9999))
