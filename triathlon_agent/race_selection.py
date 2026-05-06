from __future__ import annotations

from typing import Any


def distance_bucket(value: Any) -> str:
    text = str(value or "").strip().lower()
    if any(token in text for token in ("70.3", "half", "middle")):
        return "70.3"
    if "olympic" in text:
        return "olympic"
    if "sprint" in text:
        return "sprint"
    if not text:
        return "unknown"
    return text


def select_priority_race(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    upcoming = [
        race
        for race in snapshot.get("races", [])
        if race.get("days_to_race", 9999) >= 0
    ]
    if not upcoming:
        return None

    athlete_profile = snapshot.get("athlete_profile", {})
    target_bucket = distance_bucket(athlete_profile.get("target_race_distance"))

    def sort_key(item: dict[str, Any]) -> tuple[int, int]:
        return (0 if item.get("priority") == "A" else 1, item.get("days_to_race", 9999))

    preferred_a = sorted(
        [
            race
            for race in upcoming
            if race.get("priority") == "A"
            and distance_bucket(race.get("distance")) == target_bucket
        ],
        key=sort_key,
    )
    if preferred_a:
        return preferred_a[0]

    preferred_any = sorted(
        [
            race
            for race in upcoming
            if distance_bucket(race.get("distance")) == target_bucket
        ],
        key=sort_key,
    )
    if preferred_any:
        return preferred_any[0]

    a_races = sorted(
        [race for race in upcoming if race.get("priority") == "A"], key=sort_key
    )
    if a_races:
        return a_races[0]

    return sorted(upcoming, key=sort_key)[0]
