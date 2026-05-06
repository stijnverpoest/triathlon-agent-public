from __future__ import annotations

import unittest

from triathlon_agent.normalizer import normalize_snapshot
from triathlon_agent.utils import read_json


class NormalizerTests(unittest.TestCase):
    def test_derived_baselines_are_computed_from_wellness_history(self) -> None:
        local_inputs = {
            "athlete_profile": {
                "name": "Test",
                "hrv_baseline": None,
                "resting_hr_baseline": None,
            },
            "weekly_checkin": {
                "planning_date": "2026-03-08",
                "missed_sessions": [],
                "unexpected_fatigue": "",
            },
            "race_calendar": {"races": []},
        }
        wellness = []
        for day in range(1, 57):
            wellness.append(
                {
                    "id": f"2026-01-{day:02d}"
                    if day <= 31
                    else f"2026-02-{day - 31:02d}",
                    "hrv": 70 if day <= 49 else 63,
                    "restingHR": 42 if day <= 49 else 46,
                    "sleepSecs": 8 * 3600 if day <= 49 else int(7.1 * 3600),
                    "ctl": 50,
                    "atl": 55,
                }
            )
        intervals_data = {
            "activities": [],
            "wellness": wellness,
            "events": [],
            "sport_settings": [],
            "planning_date": "2026-03-08",
        }

        snapshot = normalize_snapshot(local_inputs, intervals_data)

        self.assertEqual(snapshot["derived_baselines"]["hrv"]["baseline"], 70.0)
        self.assertLess(snapshot["derived_baselines"]["hrv"]["acute"], 70.0)
        self.assertEqual(snapshot["derived_baselines"]["resting_hr"]["baseline"], 42.0)
        self.assertGreater(snapshot["derived_baselines"]["resting_hr"]["acute"], 42.0)
        self.assertEqual(snapshot["derived_baselines"]["hrv"]["confidence"], "high")

    def test_planning_constraints_are_normalized_from_profile_and_checkin(self) -> None:
        snapshot = normalize_snapshot(
            {
                "athlete_profile": read_json("fixtures/athlete_profile.json"),
                "weekly_checkin": read_json("fixtures/weekly_checkin.json"),
                "race_calendar": {"races": []},
            },
            {
                "activities": [],
                "wellness": [],
                "events": [],
                "sport_settings": [],
                "planning_date": "2026-03-08",
            },
        )

        constraints = snapshot["planning_constraints"]

        self.assertEqual(constraints["days_off"], ["Monday"])
        self.assertEqual(constraints["time_caps_by_day"]["Wednesday"], 45)
        self.assertEqual(constraints["time_caps_by_day"]["Friday"], 30)
        self.assertIn("Tuesday", constraints["fixed_swim_days"])
        self.assertIn("Thursday", constraints["fixed_swim_days"])
        self.assertIn("Tuesday", constraints["preferred_double_days"])
        self.assertIn("Friday", constraints["strength_only_days"])
        self.assertEqual(constraints["constraint_confidence"]["hard"], "high")
        self.assertTrue(
            any(rule["kind"] == "fixed_swim_day" for rule in constraints["hard_rules"])
        )
        self.assertTrue(
            any(
                preference["kind"] == "preferred_long_ride_day"
                for preference in constraints["soft_preferences"]
            )
        )

    def test_weekly_checkin_can_override_default_ride_and_run_days(self) -> None:
        weekly_checkin = read_json("fixtures/weekly_checkin.json")
        weekly_checkin["time_constraints"] = ["Tuesday club swim is fixed."]
        weekly_checkin["athlete_notes"] = (
            "Keep the Tuesday club swim, add a ride on Tuesday as well, and include a run on Wednesday."
        )

        snapshot = normalize_snapshot(
            {
                "athlete_profile": read_json("fixtures/athlete_profile.json"),
                "weekly_checkin": weekly_checkin,
                "race_calendar": {"races": []},
            },
            {
                "activities": [],
                "wellness": [],
                "events": [],
                "sport_settings": [],
                "planning_date": "2026-03-08",
            },
        )

        constraints = snapshot["planning_constraints"]

        self.assertNotIn("Tuesday", constraints["no_bike_days"])
        self.assertEqual(constraints["preferred_ride_days"][0], "Tuesday")
        self.assertEqual(constraints["preferred_run_days"][0], "Wednesday")

    def test_time_budget_can_be_treated_as_flexible_guideline(self) -> None:
        weekly_checkin = read_json("fixtures/weekly_checkin.json")
        weekly_checkin["athlete_notes"] = (
            "Around 10 hours is a guideline, not a hard cap. Optimize the ROI this week."
        )

        snapshot = normalize_snapshot(
            {
                "athlete_profile": read_json("fixtures/athlete_profile.json"),
                "weekly_checkin": weekly_checkin,
                "race_calendar": {"races": []},
            },
            {
                "activities": [],
                "wellness": [],
                "events": [],
                "sport_settings": [],
                "planning_date": "2026-03-08",
            },
        )

        policy = snapshot["planning_constraints"]["weekly_time_budget_policy"]

        self.assertEqual(policy["mode"], "flexible")
        self.assertEqual(policy["requested_min"], 630)

    def test_pragmatic_completion_treats_small_cooldown_trim_as_complete(self) -> None:
        local_inputs = {
            "athlete_profile": {"name": "Test Athlete"},
            "weekly_checkin": {
                "planning_date": "2026-03-08",
                "missed_sessions": [],
                "unexpected_fatigue": "",
                "open_water_mode": False,
                "time_constraints": [],
                "travel_this_week": False,
                "current_motivation": 7,
                "subjective_readiness": 7,
                "pain_or_niggles": "",
                "athlete_notes": "",
                "available_hours_override_min": None,
                "double_session_capacity_by_day": {},
                "time_caps_by_day": {},
            },
            "race_calendar": {"races": []},
            "prior_plan_context": {
                "source_run": "runs/old-run",
                "final_plan": {
                    "sessions": [
                        {
                            "date": "2026-03-02",
                            "day": "Monday",
                            "discipline": "Run",
                            "session_name": "Aerobic Reset Run",
                            "duration_min": 60,
                            "priority": "support",
                            "blocks": [
                                {
                                    "section": "Main Set",
                                    "steps": [{"duration": "45m", "target": "Z2 HR"}],
                                },
                                {
                                    "section": "Cooldown",
                                    "steps": [{"duration": "15m", "target": "Z1 HR"}],
                                },
                            ],
                        }
                    ]
                },
            },
            "prior_plan_contexts": [
                {
                    "source_run": "runs/old-run",
                    "final_plan": {
                        "sessions": [
                            {
                                "date": "2026-03-02",
                                "day": "Monday",
                                "discipline": "Run",
                                "session_name": "Aerobic Reset Run",
                                "duration_min": 60,
                                "priority": "support",
                                "blocks": [
                                    {
                                        "section": "Main Set",
                                        "steps": [
                                            {"duration": "45m", "target": "Z2 HR"}
                                        ],
                                    },
                                    {
                                        "section": "Cooldown",
                                        "steps": [
                                            {"duration": "15m", "target": "Z1 HR"}
                                        ],
                                    },
                                ],
                            }
                        ]
                    },
                }
            ],
        }
        intervals_data = {
            "activities": [
                {
                    "start_date_local": "2026-03-02T07:00:00",
                    "type": "Run",
                    "moving_time": 3540,
                    "average_heartrate": 143,
                    "icu_training_load": 42,
                }
            ],
            "wellness": [],
            "events": [],
            "sport_settings": [],
            "planning_date": "2026-03-08",
        }

        snapshot = normalize_snapshot(local_inputs, intervals_data)

        adherence = snapshot["prior_plan_adherence"]
        review = adherence["session_reviews"][0]

        self.assertEqual(review["status"], "completed_pragmatic")
        self.assertEqual(adherence["pragmatic_completions_count"], 1)
        self.assertEqual(adherence["missed_sessions_count"], 0)

    def test_data_profile_no_longer_marks_tuesday_as_no_bike_day(self) -> None:
        snapshot = normalize_snapshot(
            {
                "athlete_profile": read_json("data/athlete_profile.json"),
                "weekly_checkin": read_json("fixtures/weekly_checkin.json"),
                "race_calendar": {"races": []},
            },
            {
                "activities": [],
                "wellness": [],
                "events": [],
                "sport_settings": [],
                "planning_date": "2026-03-08",
            },
        )

        self.assertNotIn("Tuesday", snapshot["planning_constraints"]["no_bike_days"])

    def test_no_bike_access_phrase_marks_tuesday_unavailable_for_bike(self) -> None:
        weekly_checkin = read_json("fixtures/weekly_checkin.json")
        weekly_checkin["time_constraints"] = [
            "Tuesday should be swim/run only; there is no bike access on Tuesday."
        ]
        weekly_checkin["athlete_notes"] = "No bike access on Tuesday."

        snapshot = normalize_snapshot(
            {
                "athlete_profile": read_json("fixtures/athlete_profile.json"),
                "weekly_checkin": weekly_checkin,
                "race_calendar": {"races": []},
            },
            {
                "activities": [],
                "wellness": [],
                "events": [],
                "sport_settings": [],
                "planning_date": "2026-03-08",
            },
        )

        self.assertIn("Tuesday", snapshot["planning_constraints"]["no_bike_days"])

    def test_prior_plan_adherence_is_summarized_against_actual_activities(self) -> None:
        local_inputs = {
            "athlete_profile": {"name": "Test Athlete"},
            "weekly_checkin": {
                "planning_date": "2026-03-08",
                "missed_sessions": [],
                "unexpected_fatigue": "",
                "open_water_mode": False,
                "time_constraints": [],
                "travel_this_week": False,
                "current_motivation": 7,
                "subjective_readiness": 7,
                "pain_or_niggles": "",
                "athlete_notes": "",
                "available_hours_override_min": None,
                "double_session_capacity_by_day": {},
                "time_caps_by_day": {},
            },
            "race_calendar": {"races": []},
            "prior_plan_context": {
                "source_run": "runs/old-run",
                "final_plan": {
                    "sessions": [
                        {
                            "date": "2026-03-02",
                            "day": "Monday",
                            "discipline": "Run",
                            "session_name": "Aerobic Reset Run",
                            "duration_min": 40,
                            "priority": "support",
                        },
                        {
                            "date": "2026-03-03",
                            "day": "Tuesday",
                            "discipline": "Ride",
                            "session_name": "Threshold Bike Build",
                            "duration_min": 60,
                            "priority": "key",
                        },
                    ]
                },
            },
            "prior_plan_contexts": [
                {
                    "source_run": "runs/old-run",
                    "final_plan": {
                        "sessions": [
                            {
                                "date": "2026-03-02",
                                "day": "Monday",
                                "discipline": "Run",
                                "session_name": "Aerobic Reset Run",
                                "duration_min": 40,
                                "priority": "support",
                            },
                            {
                                "date": "2026-03-03",
                                "day": "Tuesday",
                                "discipline": "Ride",
                                "session_name": "Threshold Bike Build",
                                "duration_min": 60,
                                "priority": "key",
                            },
                        ]
                    },
                },
                {
                    "source_run": "runs/older-run",
                    "final_plan": {
                        "sessions": [
                            {
                                "date": "2026-02-24",
                                "day": "Tuesday",
                                "discipline": "Swim",
                                "session_name": "Club Swim",
                                "duration_min": 40,
                                "priority": "support",
                            },
                            {
                                "date": "2026-02-28",
                                "day": "Saturday",
                                "discipline": "Ride",
                                "session_name": "Long Ride with Race Rhythm",
                                "duration_min": 120,
                                "priority": "key",
                            },
                        ]
                    },
                },
            ],
        }
        intervals_data = {
            "activities": [
                {
                    "start_date_local": "2026-03-02T07:00:00",
                    "type": "Run",
                    "moving_time": 2400,
                    "icu_training_load": 35,
                },
                {
                    "start_date_local": "2026-03-04T07:00:00",
                    "type": "VirtualRide",
                    "moving_time": 3600,
                    "icu_training_load": 70,
                },
                {
                    "start_date_local": "2026-02-24T07:00:00",
                    "type": "Swim",
                    "moving_time": 2400,
                    "icu_training_load": 20,
                },
                {
                    "start_date_local": "2026-02-28T07:00:00",
                    "type": "Ride",
                    "moving_time": 7200,
                    "icu_training_load": 110,
                },
            ],
            "wellness": [],
            "events": [],
            "sport_settings": [],
            "planning_date": "2026-03-08",
        }

        snapshot = normalize_snapshot(local_inputs, intervals_data)

        adherence = snapshot["prior_plan_adherence"]

        self.assertEqual(adherence["status"], "available")
        self.assertEqual(adherence["completed_on_planned_day"], 1)
        self.assertEqual(adherence["completed_on_other_day"], 1)
        self.assertEqual(adherence["key_sessions_completed"], 1)
        self.assertEqual(adherence["missed_sessions_count"], 0)
        self.assertIn("Completed 2/2 planned sessions", adherence["summary"])
        self.assertEqual(len(snapshot["prior_plan_history"]), 2)
        self.assertEqual(snapshot["athlete_memory"]["weeks_reviewed"], 2)
        self.assertIn("Tuesday", snapshot["athlete_memory"]["reliable_days"])
        self.assertEqual(
            snapshot["recent_session_signatures_by_kind"]["threshold_ride"][0],
            "Threshold Bike Build",
        )
        self.assertIn(
            "Long Ride with Race Rhythm",
            snapshot["recent_session_signatures_by_kind"]["long_ride"],
        )

    def test_recent_wellness_averages_skip_missing_numeric_values(self) -> None:
        snapshot = normalize_snapshot(
            {
                "athlete_profile": {"name": "Test"},
                "weekly_checkin": {
                    "planning_date": "2026-03-30",
                    "missed_sessions": [],
                    "unexpected_fatigue": "",
                },
                "race_calendar": {"races": []},
            },
            {
                "activities": [],
                "wellness": [
                    {
                        "id": "2026-03-24",
                        "hrv": 64,
                        "restingHR": 45,
                        "sleepSecs": 27000,
                    },
                    {
                        "id": "2026-03-25",
                        "hrv": None,
                        "restingHR": None,
                        "sleepSecs": None,
                    },
                    {
                        "id": "2026-03-26",
                        "hrv": 68,
                        "restingHR": 43,
                        "sleepSecs": 28800,
                    },
                ],
                "events": [],
                "sport_settings": [],
                "planning_date": "2026-03-30",
            },
        )

        self.assertEqual(snapshot["avg_hrv"], 66.0)
        self.assertEqual(snapshot["avg_resting_hr"], 44.0)
        self.assertEqual(snapshot["avg_sleep_hours"], 7.75)

    def test_prior_plan_adherence_allows_two_day_moves_and_trail_runs(self) -> None:
        snapshot = normalize_snapshot(
            {
                "athlete_profile": {"name": "Test Athlete"},
                "weekly_checkin": {
                    "planning_date": "2026-03-30",
                    "missed_sessions": [],
                    "unexpected_fatigue": "",
                    "open_water_mode": False,
                    "time_constraints": [],
                    "travel_this_week": False,
                    "current_motivation": 7,
                    "subjective_readiness": 7,
                    "pain_or_niggles": "",
                    "athlete_notes": "",
                    "available_hours_override_min": None,
                    "double_session_capacity_by_day": {},
                    "time_caps_by_day": {},
                },
                "race_calendar": {"races": []},
                "prior_plan_contexts": [
                    {
                        "source_run": "runs/prior-week",
                        "final_plan": {
                            "sessions": [
                                {
                                    "date": "2026-03-25",
                                    "day": "Wednesday",
                                    "discipline": "Ride",
                                    "session_name": "Endurance Ride",
                                    "duration_min": 60,
                                    "priority": "support",
                                },
                                {
                                    "date": "2026-03-26",
                                    "day": "Thursday",
                                    "discipline": "Swim",
                                    "session_name": "Pool Swim",
                                    "duration_min": 30,
                                    "priority": "support",
                                },
                                {
                                    "date": "2026-03-29",
                                    "day": "Sunday",
                                    "discipline": "Run",
                                    "session_name": "Long Easy Run",
                                    "duration_min": 70,
                                    "priority": "key",
                                    "primary_metric": "HR",
                                },
                            ]
                        },
                    }
                ],
            },
            {
                "activities": [
                    {
                        "start_date_local": "2026-03-27T18:00:00",
                        "type": "VirtualRide",
                        "moving_time": 3600,
                        "icu_training_load": 50,
                    },
                    {
                        "start_date_local": "2026-03-28T10:00:00",
                        "type": "Swim",
                        "moving_time": 2100,
                        "icu_training_load": 25,
                    },
                    {
                        "start_date_local": "2026-03-29T09:00:00",
                        "type": "TrailRun",
                        "moving_time": 4500,
                        "icu_training_load": 80,
                    },
                ],
                "wellness": [],
                "events": [],
                "sport_settings": [],
                "planning_date": "2026-03-30",
            },
        )

        adherence = snapshot["prior_plan_adherence"]

        self.assertEqual(adherence["completed_on_planned_day"], 1)
        self.assertEqual(adherence["completed_on_other_day"], 2)
        self.assertEqual(adherence["missed_sessions_count"], 0)
        self.assertIn("Endurance Ride", adherence["moved_sessions"])
        self.assertIn("Pool Swim", adherence["moved_sessions"])
        self.assertEqual(adherence["key_sessions_completed"], 1)


if __name__ == "__main__":
    unittest.main()
