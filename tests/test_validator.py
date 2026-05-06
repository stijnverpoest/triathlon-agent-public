from __future__ import annotations

import unittest

from triathlon_agent.agents import validator_coach


class ValidatorCoachTests(unittest.TestCase):
    def test_recovery_week_validator_allows_long_endurance_ride(self) -> None:
        snapshot = {
            "planning_constraints": {
                "no_bike_days": [],
                "fixed_swim_days": [],
                "double_session_capacity_by_day": {},
                "time_caps_by_day": {},
                "available_hours_override_min": None,
                "preferred_long_ride_day": "Saturday",
            },
            "prior_plan_adherence": {"status": "none"},
            "athlete_memory": {"status": "limited"},
        }
        draft_plan = {
            "sessions": [
                {
                    "day": "Saturday",
                    "date": "2026-03-14",
                    "discipline": "Ride",
                    "session_name": "Long Endurance Ride",
                    "duration_min": 150,
                    "priority": "support",
                    "coggan_level": "L2 Endurance",
                    "expected_if": 0.64,
                    "estimated_tss": 102,
                }
            ]
        }
        performance = {"readiness_status": "yellow"}
        macro = {"week_classification": "recovery"}

        result = validator_coach.run(snapshot, draft_plan, performance, macro, {})

        self.assertNotIn(
            "Long Endurance Ride is still too aggressive for a declared recovery week.",
            result["weaknesses"],
        )
        self.assertFalse(
            any(
                edit["action"] == "downgrade_ride_to_endurance"
                for edit in result["proposed_edits"]
            )
        )

    def test_recovery_week_validator_flags_hidden_intensity(self) -> None:
        snapshot = {
            "planning_constraints": {
                "no_bike_days": [],
                "fixed_swim_days": [],
                "double_session_capacity_by_day": {},
                "time_caps_by_day": {},
                "available_hours_override_min": None,
                "preferred_long_ride_day": "Saturday",
            },
            "prior_plan_adherence": {"status": "none"},
            "athlete_memory": {"status": "limited"},
        }
        draft_plan = {
            "sessions": [
                {
                    "day": "Wednesday",
                    "date": "2026-03-11",
                    "discipline": "Ride",
                    "session_name": "Threshold Bike Build",
                    "duration_min": 75,
                    "priority": "key",
                    "expected_if": 0.82,
                    "estimated_tss": 88,
                },
                {
                    "day": "Thursday",
                    "date": "2026-03-12",
                    "discipline": "Run",
                    "session_name": "Threshold Pace Repeats",
                    "duration_min": 60,
                    "priority": "key",
                    "primary_metric": "Pace",
                    "daniels_type": "T",
                },
            ]
        }
        performance = {"readiness_status": "yellow"}
        macro = {"week_classification": "recovery"}
        race = {}

        result = validator_coach.run(snapshot, draft_plan, performance, macro, race)

        self.assertEqual(result["verdict"], "revise")
        actions = {edit["action"] for edit in result["proposed_edits"]}
        self.assertIn("downgrade_ride_to_endurance", actions)
        self.assertIn("downgrade_run_to_easy", actions)


if __name__ == "__main__":
    unittest.main()
