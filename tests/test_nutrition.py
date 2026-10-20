from __future__ import annotations

import unittest

from triathlon_agent.agents import (
    head_coach_planner,
    macro_coach,
    nutritionist,
    performance_doctor,
    race_director,
)
from triathlon_agent.normalizer import normalize_snapshot
from triathlon_agent.utils import read_json


class NutritionTests(unittest.TestCase):
    def _snapshot(self) -> dict:
        return normalize_snapshot(
            {
                "athlete_profile": read_json("fixtures/athlete_profile.json"),
                "weekly_checkin": read_json("fixtures/weekly_checkin.json"),
                "race_calendar": {"races": []},
                # Recent tests keep the recovery week free of threshold testing here.
                "athlete_state": {
                    "threshold_testing": {
                        "bike": {"last_test_date": "2026-02-20"},
                        "run": {"last_test_date": "2026-02-20"},
                    }
                },
            },
            {
                "profile": read_json("fixtures/intervals_profile.json"),
                "sport_settings": read_json("fixtures/sport_settings.json"),
                "wellness": read_json("fixtures/wellness.json"),
                "activities": read_json("fixtures/activities.json"),
                "events": read_json("fixtures/events.json"),
                "planning_date": "2026-03-08",
            },
        )

    def test_personalized_notes_only_exist_for_meaningful_days(self) -> None:
        snapshot = self._snapshot()
        performance = performance_doctor.run(snapshot)
        macro = macro_coach.run(snapshot, performance)
        race = race_director.run(snapshot, macro)
        draft = head_coach_planner.run(snapshot, performance, macro, race)

        nutrition = nutritionist.run(snapshot, draft, performance, macro, race)

        note_dates = {item["date"] for item in nutrition["daily_notes"]}
        key_dates = {
            session["date"]
            for session in draft["sessions"]
            if session.get("priority") == "key"
        }
        self.assertTrue(key_dates)
        self.assertTrue(key_dates <= note_dates)
        self.assertNotIn("2026-03-09", note_dates)
        self.assertEqual(nutrition["energy_availability_risk"]["status"], "moderate")

    def test_session_fueling_uses_body_weight_and_hydration_context(self) -> None:
        snapshot = self._snapshot()
        performance = performance_doctor.run(snapshot)
        macro = macro_coach.run(snapshot, performance)
        race = race_director.run(snapshot, macro)
        draft = head_coach_planner.run(snapshot, performance, macro, race)

        nutrition = nutritionist.run(snapshot, draft, performance, macro, race)

        long_ride = next(
            item
            for item in nutrition["session_fueling"]
            if item["session_name"]
            in {"Long Ride with Race Rhythm", "Long Endurance Ride"}
        )
        easy_run = next(
            item
            for item in nutrition["session_fueling"]
            if item["session_name"] == "Easy Aerobic Run"
        )
        saturday_targets = next(
            item for item in nutrition["daily_targets"] if item["date"] == "2026-03-14"
        )

        long_ride_session = next(
            session
            for session in draft["sessions"]
            if session["session_name"] == long_ride["session_name"]
        )
        low, high = nutritionist.carb_range_g_per_h(long_ride_session["duration_min"])
        self.assertIn(f"{low}-{high}g", long_ride["guidance"]["during"])
        self.assertIn("26g protein", easy_run["guidance"]["post"])
        self.assertEqual(saturday_targets["protein_target_g"], 26)
        self.assertEqual(
            saturday_targets["hydration_focus"],
            "high" if long_ride_session["duration_min"] >= 120 else "moderate",
        )


if __name__ == "__main__":
    unittest.main()
