from __future__ import annotations

import unittest

from triathlon_agent.agents import (
    head_coach_planner,
    macro_coach,
    phase_alignment_auditor,
    performance_doctor,
    race_director,
    training_philosophy_auditor,
)
from triathlon_agent.normalizer import normalize_snapshot
from triathlon_agent.race_selection import distance_bucket, select_priority_race
from triathlon_agent.utils import read_json


class DoctrineTests(unittest.TestCase):
    def test_macro_coach_enforces_recovery_after_three_load_weeks(self) -> None:
        snapshot = {
            "races": [{"priority": "A", "days_to_race": 90}],
            "weekly_windows": [],
            "periodization_state": {
                "consecutive_load_weeks": 3,
                "last_week_type": "load",
                "recommended_next_week_type": "recovery",
                "next_block_week": 4,
            },
        }
        performance = {"readiness_status": "green"}

        result = macro_coach.run(snapshot, performance)

        self.assertEqual(result["friel_cycle_type"], "recovery")
        self.assertEqual(result["friel_block_week"], 4)
        self.assertEqual(result["week_classification"], "recovery")

    def test_macro_coach_recovery_week_survives_rising_load(
        self,
    ) -> None:
        snapshot = {
            "races": [{"priority": "A", "days_to_race": 90}],
            "weekly_windows": [
                {"load": 510.0},
                {"load": 587.0},
                {"load": 565.0},
            ],
            "periodization_state": {
                "consecutive_load_weeks": 3,
                "last_week_type": "load",
                "recommended_next_week_type": "recovery",
                "next_block_week": 4,
            },
            "planning_constraints": {},
            "prior_plan_adherence": {},
        }
        performance = {"readiness_status": "green"}

        result = macro_coach.run(snapshot, performance)

        # Rising load must not override a due recovery week (3:1 rhythm).
        self.assertEqual(result["week_classification"], "recovery")
        self.assertEqual(result["friel_cycle_type"], "recovery")
        self.assertEqual(result["friel_block_week"], 4)

    def test_macro_and_race_director_prefer_a_race_over_b_race(self) -> None:
        snapshot = {
            "athlete_profile": {"target_race_distance": "Ironman 70.3"},
            "races": [
                {
                    "name": "Olympic Tune-Up",
                    "priority": "B",
                    "days_to_race": 40,
                    "distance": "Olympic",
                },
                {
                    "name": "Main 70.3",
                    "priority": "A",
                    "days_to_race": 80,
                    "distance": "Ironman 70.3",
                },
            ],
            "weekly_windows": [],
            "periodization_state": {},
            "planning_constraints": {},
            "prior_plan_adherence": {},
        }
        performance = {"readiness_status": "green"}

        macro = macro_coach.run(snapshot, performance)
        race = race_director.run(snapshot, macro)

        self.assertEqual(macro["macro_phase"], "build")
        self.assertEqual(race["priority_race"]["name"], "Main 70.3")
        self.assertIn("Main 70.3", race["priority_race_lens"])

    def test_half_marathon_does_not_count_as_target_70_3(self) -> None:
        snapshot = {
            "athlete_profile": {"target_race_distance": "Ironman 70.3"},
            "races": [
                {
                    "name": "Hill Trail 50k",
                    "priority": "A",
                    "days_to_race": 30,
                    "distance": "TrailRun",
                },
                {
                    "name": "City Half Marathon",
                    "priority": "B",
                    "days_to_race": 60,
                    "distance": "Half Marathon",
                },
            ],
        }

        self.assertEqual(distance_bucket("Half Marathon"), "half_marathon")
        self.assertEqual(distance_bucket("Coastal Half"), "70.3")
        self.assertEqual(distance_bucket("TrailRun"), "trail")  # no distance stated: neutral trail bucket
        self.assertEqual(
            select_priority_race(snapshot)["name"], "Hill Trail 50k"
        )

    def test_race_director_suggests_trail_tune_up_for_ultra_trail(self) -> None:
        snapshot = {
            "athlete_profile": {"target_race_distance": "Ironman 70.3"},
            "races": [
                {
                    "name": "Hill Trail 50k",
                    "priority": "A",
                    "days_to_race": 120,
                    "distance": "TrailRun",
                },
            ],
            "weekly_windows": [],
            "periodization_state": {},
            "planning_constraints": {},
            "prior_plan_adherence": {},
        }
        macro = macro_coach.run(snapshot, {"readiness_status": "green"})

        race = race_director.run(snapshot, macro)

        tune_ups = [
            item
            for item in race.get("race_recommendations", [])
            if item["type"] == "tune_up"
        ]
        self.assertEqual(tune_ups[0]["suggested_distance"], "Trail 25-35k")

    def test_training_philosophy_auditor_passes_for_stub_plan(self) -> None:
        snapshot = normalize_snapshot(
            {
                "athlete_profile": read_json("fixtures/athlete_profile.json"),
                "weekly_checkin": read_json("fixtures/weekly_checkin.json"),
                "race_calendar": {"races": []},
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
        performance = performance_doctor.run(snapshot)
        macro = macro_coach.run(snapshot, performance)
        race = race_director.run(snapshot, macro)
        draft = head_coach_planner.run(snapshot, performance, macro, race)
        phase = phase_alignment_auditor.run(snapshot, macro, race, draft)

        result = training_philosophy_auditor.run(snapshot, macro, race, phase, draft)

        self.assertEqual(result["overall_status"], "pass")
        self.assertEqual(result["daniels_alignment"], "pass")
        self.assertEqual(result["coggan_alignment"], "pass")
        self.assertEqual(result["friel_alignment"], "pass")

    def test_head_coach_uses_profile_club_swim_duration(self) -> None:
        athlete_profile = read_json("fixtures/athlete_profile.json")
        athlete_profile.setdefault("swim_profile", {})["club_session_duration_min"] = 60
        athlete_profile["swim_profile"]["club_session_distance_m"] = 3000
        snapshot = normalize_snapshot(
            {
                "athlete_profile": athlete_profile,
                "weekly_checkin": read_json("fixtures/weekly_checkin.json"),
                "race_calendar": {"races": []},
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
        performance = {"readiness_status": "green"}
        macro = {"week_classification": "build"}
        race = {"primary_race": None}

        draft = head_coach_planner.run(snapshot, performance, macro, race)

        club_swim = next(
            session
            for session in draft["sessions"]
            if session["day"] == "Tuesday" and session["discipline"] == "Swim"
        )
        self.assertEqual(club_swim["duration_min"], 60)
        self.assertIn("Expect roughly 3k total.", club_swim["execution_notes"])

    def test_head_coach_can_add_friday_support_ride_when_requested(self) -> None:
        athlete_profile = read_json("fixtures/athlete_profile.json")
        athlete_profile["strength_days"] = ["Thursday"]
        athlete_profile["known_constraints"] = [
            item
            for item in athlete_profile.get("known_constraints", [])
            if "Friday gym only" not in item
            and "Wednesday capped at 45 minutes" not in item
        ]
        weekly_checkin = read_json("fixtures/weekly_checkin.json")
        weekly_checkin["time_constraints"] = [
            "Friday should include a medium support ride rather than being fully off."
        ]
        weekly_checkin["athlete_notes"] = (
            "Around 10 hours is a guideline, not a hard cap. Friday should not be fully off."
        )
        weekly_checkin["available_hours_override_min"] = None
        weekly_checkin["time_caps_by_day"] = {}
        snapshot = normalize_snapshot(
            {
                "athlete_profile": athlete_profile,
                "weekly_checkin": weekly_checkin,
                "race_calendar": {"races": []},
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
        performance = {"readiness_status": "green"}
        macro = {"week_classification": "build"}
        race = {"primary_race": None}

        draft = head_coach_planner.run(snapshot, performance, macro, race)

        friday_ride = next(
            session
            for session in draft["sessions"]
            if session["day"] == "Friday" and session["discipline"] == "Ride"
        )
        self.assertEqual(friday_ride["session_name"], "Endurance Ride")
        self.assertEqual(friday_ride["duration_min"], 55)

    def test_head_coach_adds_third_ride_for_green_build_when_profile_requests_it(
        self,
    ) -> None:
        athlete_profile = read_json("fixtures/athlete_profile.json")
        athlete_profile["weekly_sport_frequency"] = {
            "bike_sessions": 3,
            "run_sessions": 3,
        }
        athlete_profile["strength_days"] = ["Thursday"]
        athlete_profile["known_constraints"] = [
            item
            for item in athlete_profile.get("known_constraints", [])
            if "Friday gym only" not in item
            and "Wednesday capped at 45 minutes" not in item
        ]
        weekly_checkin = read_json("fixtures/weekly_checkin.json")
        weekly_checkin["time_constraints"] = ["Tuesday club swim is fixed."]
        weekly_checkin["athlete_notes"] = "Keep the week green and executable."
        weekly_checkin["time_caps_by_day"] = {}
        weekly_checkin["double_session_capacity_by_day"] = {
            "Tuesday": True,
            "Friday": True,
        }
        snapshot = normalize_snapshot(
            {
                "athlete_profile": athlete_profile,
                "weekly_checkin": weekly_checkin,
                "race_calendar": {"races": []},
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
        performance = {"readiness_status": "green", "readiness_score": 80}
        macro = {"week_classification": "build", "macro_phase": "build"}
        race = {"priority_race": None, "race_specific_needs": []}

        draft = head_coach_planner.run(snapshot, performance, macro, race)

        ride_count = sum(
            1 for session in draft["sessions"] if session["discipline"] == "Ride"
        )
        self.assertEqual(ride_count, 3)
        self.assertTrue(
            any(
                session["day"] == "Friday" and session["discipline"] == "Ride"
                for session in draft["sessions"]
            )
        )

    def _fixture_snapshot(self) -> dict:
        return normalize_snapshot(
            {
                "athlete_profile": read_json("fixtures/athlete_profile.json"),
                "weekly_checkin": read_json("fixtures/weekly_checkin.json"),
                "race_calendar": {"races": []},
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

    def test_head_coach_quality_count_follows_intensity_allowance(self) -> None:
        snapshot = self._fixture_snapshot()
        snapshot["planning_constraints"].update(
            time_caps_by_day={},
            strength_only_days=[],
            double_session_capacity_by_day={
                day: True for day in ("Tuesday", "Wednesday", "Thursday", "Friday")
            },
        )
        macro = {"week_classification": "build", "macro_phase": "build", "friel_block_week": 2}
        race = {"priority_race": {"distance": "Ironman 70.3"}, "race_specific_needs": []}

        def quality_count(maximum: int) -> int:
            performance = {
                "readiness_status": "green",
                "intensity_allowance": {"max_quality_sessions": maximum},
            }
            draft = head_coach_planner.run(snapshot, performance, macro, race)
            return sum(1 for session in draft["sessions"] if session.get("progression"))

        self.assertEqual(quality_count(1), 1)
        self.assertEqual(quality_count(2), 2)
        self.assertEqual(quality_count(3), 3)

    def test_head_coach_mixes_bike_and_run_quality_for_triathlon(self) -> None:
        snapshot = self._fixture_snapshot()
        performance = {
            "readiness_status": "green",
            "intensity_allowance": {"max_quality_sessions": 2},
        }
        macro = {"week_classification": "build", "macro_phase": "build", "friel_block_week": 1}
        race = {"priority_race": {"distance": "Ironman 70.3"}, "race_specific_needs": []}

        draft = head_coach_planner.run(snapshot, performance, macro, race)

        quality = [s for s in draft["sessions"] if s.get("progression")]
        self.assertEqual({s["discipline"] for s in quality}, {"Ride", "Run"})
        # The fixture check-in's available_hours_override_min (630') now binds.
        self.assertEqual(draft["weekly_time_budget"]["mode"], "hard_cap")

    def test_head_coach_progresses_one_rung_on_last_completed_dose(self) -> None:
        snapshot = self._fixture_snapshot()
        snapshot["progression"] = {
            "last_dose_by_family": {
                "bike_sweet_spot": {"minutes": 40.0, "completed": True},
                "run_threshold": {"minutes": 30.0, "completed": False},
            }
        }
        performance = {
            "readiness_status": "green",
            "intensity_allowance": {"max_quality_sessions": 2},
        }
        macro = {"week_classification": "build", "macro_phase": "build", "friel_block_week": 2}
        race = {"priority_race": {"distance": "Ironman 70.3"}, "race_specific_needs": []}

        draft = head_coach_planner.run(snapshot, performance, macro, race)

        doses = {
            s["progression"]["family"]: s["progression"]
            for s in draft["sessions"]
            if s.get("progression")
        }
        # 40' completed -> next rung (3x15 = 45').
        self.assertEqual(doses["bike_sweet_spot"]["time_in_zone_min"], 45)
        # 30' missed -> repeat the rung (3x10 = 30'), never progress; the Daniels
        # T cap (~10% of weekly running) may hold it lower on a low-run week.
        self.assertLessEqual(doses["run_threshold"]["time_in_zone_min"], 30)

    def test_head_coach_does_not_trim_to_weekly_cap(self) -> None:
        snapshot = self._fixture_snapshot()
        snapshot["planning_constraints"]["available_hours_override_min"] = 180
        snapshot["planning_constraints"]["time_caps_by_day"] = {}
        performance = {
            "readiness_status": "green",
            "intensity_allowance": {"max_quality_sessions": 2},
        }
        macro = {"week_classification": "build", "macro_phase": "build", "friel_block_week": 3}
        race = {"priority_race": {"distance": "Ironman 70.3"}, "race_specific_needs": []}

        draft = head_coach_planner.run(snapshot, performance, macro, race)

        long_ride = next(s for s in draft["sessions"] if s["session_name"].startswith("Long"))
        # The available-hours override is now a binding cap: easy volume and the
        # secondary quality go first, the long ride is trimmed last, and the cuts are named.
        recent = snapshot["training_load"]["ride_load"]["longest_ride_min_28d"]
        self.assertLessEqual(long_ride["duration_min"], recent * 1.15)
        self.assertEqual(draft["weekly_time_budget"]["mode"], "hard_cap")
        self.assertTrue(draft["weekly_time_budget"]["cuts"])


if __name__ == "__main__":
    unittest.main()
