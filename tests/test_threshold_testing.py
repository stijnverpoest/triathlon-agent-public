from __future__ import annotations

from datetime import date
import tempfile
import unittest
from pathlib import Path

from triathlon_agent.agents import (
    head_coach_planner,
    macro_coach,
    performance_doctor,
    race_director,
    syntax_guard,
    validator_coach,
)
from triathlon_agent.compiler import compile_session_to_event
from triathlon_agent.config import load_config
from triathlon_agent.contracts import validate_agent_output
from triathlon_agent.normalizer import normalize_snapshot
from triathlon_agent.threshold_testing import (
    assess_threshold_testing,
    compute_test_result,
    plan_threshold_tests,
    record_threshold_tests,
    testing_preferences,
)
from triathlon_agent.utils import read_json, write_json
from triathlon_agent.workflow import approve_run, finalize_run, plan_week


WEEK_START = date(2026, 3, 9)
SPORT_SETTINGS = {
    "Ride": {"id": 1, "types": ["Ride"], "ftp": 300, "lthr": 165},
    "Run": {"id": 2, "types": ["Run"], "threshold_pace": 3.92, "lthr": 172},
}


def _assess(**overrides):
    kwargs = {
        "athlete_profile": {},
        "weekly_checkin": {"pain_or_niggles": "none"},
        "athlete_state": {},
        "activities": [],
        "races": [],
        "health_events": [],
        "sport_settings": SPORT_SETTINGS,
        "test_intervals": {},
        "week_start": WEEK_START,
    }
    kwargs.update(overrides)
    return assess_threshold_testing(**kwargs)


def _state(bike: str | None, run: str | None) -> dict:
    return {
        "threshold_testing": {
            "bike": {"last_test_date": bike} if bike else None,
            "run": {"last_test_date": run} if run else None,
        }
    }


def _interval(minutes: float, **values) -> dict:
    return {"type": "WORK", "moving_time": int(minutes * 60), **values}


class ThresholdAssessmentTests(unittest.TestCase):
    def test_bike_protocol_defaults_to_coggan_20_with_weekly_override(self) -> None:
        self.assertEqual(testing_preferences({}, {})["bike_protocol"], "coggan_20")
        self.assertEqual(
            testing_preferences({}, {"bike_test_protocol": "friel_30"})[
                "bike_protocol"
            ],
            "friel_30",
        )
        with self.assertRaises(ValueError):
            testing_preferences({}, {"bike_test_protocol": "ramp"})

    def test_retest_window_drives_status(self) -> None:
        cases = {
            None: "overdue",
            "2026-02-17": "not_due",  # 20 days
            "2026-01-18": "due",  # 50 days
            "2025-12-09": "overdue",  # 90 days
        }
        for last_date, expected in cases.items():
            with self.subTest(last_date=last_date):
                result = _assess(athlete_state=_state(last_date, last_date))
                self.assertEqual(result["bike"]["status"], expected)
                self.assertEqual(result["run"]["status"], expected)

    def test_eftp_drift_allows_an_early_bike_retest(self) -> None:
        activities = [
            {
                "id": 1,
                "type": "Ride",
                "name": "Endurance",
                "start_date_local": "2026-03-01T08:00:00",
                "icu_eftp": 260,
            }
        ]
        result = _assess(
            athlete_state=_state("2026-02-07", "2026-02-07"),  # 30 days
            activities=activities,
        )
        self.assertEqual(result["bike"]["status"], "due")
        self.assertIn("eFTP 260", result["bike"]["reason"])
        self.assertEqual(result["run"]["status"], "not_due")

    def test_a_race_and_niggles_block_testing(self) -> None:
        races = [
            {"name": "City Half", "priority": "A", "days_to_race": 15},
        ]
        result = _assess(races=races)
        self.assertEqual(result["bike"]["status"], "blocked")
        self.assertIn("A-race", result["bike"]["blockers"][0])

        niggle = _assess(weekly_checkin={"pain_or_niggles": "ITB tight"})
        self.assertEqual(niggle["bike"]["status"], "overdue")
        self.assertEqual(niggle["run"]["status"], "blocked")

    def test_recent_half_marathon_substitutes_for_the_run_test(self) -> None:
        races = [
            {
                "name": "City Half",
                "date": "2026-03-01",
                "priority": "A",
                "distance": "Half Marathon",
                "days_to_race": -8,
            }
        ]
        activities = [
            {
                "id": 77,
                "type": "Run",
                "name": "City Half",
                "start_date_local": "2026-03-01T10:00:00",
                "distance": 21100,
                "moving_time": 5280,  # 1:28:00 -> 4:10/km
            }
        ]
        result = _assess(races=races, activities=activities)
        self.assertEqual(result["run"]["status"], "not_due")
        self.assertEqual(result["run"]["last_test_source"], "race")
        update = next(
            item for item in result["proposed_updates"] if item["field"] == "threshold_pace"
        )
        self.assertEqual(update["proposed_display"], "4:04/km")
        self.assertEqual(update["confidence"], "medium")
        self.assertTrue(update["apply"])
        # Intervals stores threshold pace in m/s.
        self.assertAlmostEqual(update["proposed"], 4.0984, places=3)


class RaceDistanceInferenceTests(unittest.TestCase):
    def test_half_marathon_is_not_read_as_a_half_iron(self) -> None:
        from triathlon_agent.normalizer import _infer_distance_from_text

        self.assertEqual(
            _infer_distance_from_text("Run", "City Half Marathon 2026"),
            "Half Marathon",
        )
        self.assertEqual(
            _infer_distance_from_text("Triathlon", "Coastal Half"), "Ironman 70.3"
        )


class ThresholdResultTests(unittest.TestCase):
    def test_coggan_20_uses_best_20_minute_power_times_095(self) -> None:
        test = {
            "discipline": "bike",
            "date": "2026-03-07",
            "activity_id": 5,
            "name": "FTP Test (20 min)",
            "protocol": "coggan_20",
            "source": "test",
        }
        intervals = [
            _interval(5, average_watts=360, average_heartrate=165),
            _interval(20, average_watts=296, average_heartrate=171),
        ]
        result = compute_test_result(test, intervals)
        self.assertEqual(result["status"], "computed")
        self.assertEqual(result["values"]["ftp"]["value"], 281)
        self.assertEqual(result["values"]["lthr"]["confidence"], "low")

    def test_friel_30_run_uses_final_20_lap_for_lthr(self) -> None:
        test = {
            "discipline": "run",
            "date": "2026-03-07",
            "activity_id": 6,
            "name": "Threshold Run Test (30 min)",
            "protocol": "friel_30_run",
            "source": "test",
        }
        intervals = [
            {**_interval(15, average_heartrate=140, distance=3000), "type": "RECOVERY"},
            _interval(10, average_heartrate=165, distance=2450),
            _interval(20, average_heartrate=172, distance=4850),
        ]
        result = compute_test_result(test, intervals)
        self.assertEqual(result["values"]["lthr"]["value"], 172)
        self.assertEqual(result["values"]["lthr"]["confidence"], "high")
        self.assertAlmostEqual(result["values"]["threshold_pace"]["value"], 246.6, places=1)

    def test_missing_laps_need_review_instead_of_guessing(self) -> None:
        test = {
            "discipline": "bike",
            "date": "2026-03-07",
            "activity_id": 7,
            "name": "FTP test",
            "protocol": "coggan_20",
            "source": "test",
        }
        self.assertEqual(compute_test_result(test, [])["status"], "needs_review")

    def test_completed_test_produces_guarded_sport_setting_updates(self) -> None:
        activities = [
            {
                "id": 5,
                "type": "VirtualRide",
                "name": "FTP Test (20 min)",
                "start_date_local": "2026-03-07T18:00:00",
            }
        ]
        result = _assess(
            activities=activities,
            test_intervals={"5": [_interval(20, average_watts=268, average_heartrate=171)]},
        )
        self.assertEqual(result["bike"]["status"], "not_due")
        updates = {item["field"]: item for item in result["proposed_updates"]}
        self.assertEqual(updates["ftp"]["proposed"], 255)
        self.assertEqual(updates["ftp"]["delta_pct"], -15.0)
        self.assertTrue(updates["ftp"]["apply"])
        self.assertFalse(updates["lthr"]["apply"])
        errors = validate_agent_output(
            "orchestrator",
            {"threshold_testing": {"threshold_updates": result["proposed_updates"]}},
        )
        self.assertFalse([error for error in errors if "threshold_testing" in error])

    def test_implausible_change_is_not_auto_applied(self) -> None:
        activities = [
            {
                "id": 5,
                "type": "Ride",
                "name": "FTP test",
                "start_date_local": "2026-03-07T18:00:00",
            }
        ]
        result = _assess(
            activities=activities,
            test_intervals={"5": [_interval(20, average_watts=200)]},
        )
        update = result["proposed_updates"][0]
        self.assertFalse(update["apply"])
        self.assertIn("implausibly large", update["apply_reason"])

    def test_record_threshold_tests_keeps_latest_per_discipline(self) -> None:
        completed = [
            {
                "discipline": "bike",
                "date": "2026-03-07",
                "protocol": "coggan_20",
                "source": "test",
                "activity_id": 5,
                "values": {"ftp": {"value": 281}},
            }
        ]
        applied = [{"discipline": "bike", "field": "ftp"}]
        state = record_threshold_tests(_state("2026-01-01", None), completed, applied)
        bike = state["threshold_testing"]["bike"]
        self.assertEqual(bike["last_test_date"], "2026-03-07")
        self.assertEqual(bike["values"], {"ftp": 281})
        self.assertEqual(bike["applied_fields"], ["ftp"])
        again = record_threshold_tests(state, completed, applied)
        self.assertEqual(len(again["threshold_testing"]["history"]), 1)


class ThresholdPlanningTests(unittest.TestCase):
    def _testing(self, bike: dict, run: dict) -> dict:
        base = {"protocol": "coggan_20", "blockers": [], "reason": "r"}
        return {
            "bike": {**base, **bike},
            "run": {**base, "protocol": "friel_30_run", **run},
        }

    def test_tests_only_go_in_recovery_weeks(self) -> None:
        testing = self._testing({"status": "due", "urgency": "due"}, {"status": "not_due"})
        decision = plan_threshold_tests(
            testing, recovery_week=False, readiness_status="green"
        )
        self.assertEqual(decision["scheduled"], [])
        self.assertEqual(decision["deferred"][0]["discipline"], "bike")

    def test_due_disciplines_alternate_oldest_first(self) -> None:
        testing = self._testing(
            {"status": "due", "urgency": "due", "last_test_date": "2026-01-20"},
            {"status": "due", "urgency": "due", "last_test_date": "2026-01-10"},
        )
        decision = plan_threshold_tests(
            testing, recovery_week=True, readiness_status="green"
        )
        self.assertEqual([item["discipline"] for item in decision["scheduled"]], ["run"])
        self.assertEqual(decision["deferred"][0]["discipline"], "bike")

    def test_both_overdue_are_both_scheduled(self) -> None:
        testing = self._testing(
            {"status": "overdue", "urgency": "overdue"},
            {"status": "overdue", "urgency": "overdue"},
        )
        decision = plan_threshold_tests(
            testing, recovery_week=True, readiness_status="green"
        )
        self.assertEqual(
            [item["discipline"] for item in decision["scheduled"]], ["bike", "run"]
        )

    def test_readiness_gates_testing(self) -> None:
        testing = self._testing(
            {"status": "overdue", "urgency": "overdue"},
            {"status": "overdue", "urgency": "overdue"},
        )
        yellow = plan_threshold_tests(testing, recovery_week=True, readiness_status="yellow")
        self.assertEqual([item["discipline"] for item in yellow["scheduled"]], ["bike"])
        red = plan_threshold_tests(testing, recovery_week=True, readiness_status="red")
        self.assertEqual(red["scheduled"], [])


class ThresholdWeekIntegrationTests(unittest.TestCase):
    def _snapshot(self, weekly_overrides: dict | None = None) -> dict:
        weekly_checkin = read_json("fixtures/weekly_checkin.json")
        weekly_checkin.update(weekly_overrides or {})
        return normalize_snapshot(
            {
                "athlete_profile": read_json("fixtures/athlete_profile.json"),
                "weekly_checkin": weekly_checkin,
                "race_calendar": {"races": []},
                "athlete_state": {},
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

    def _plan(self, snapshot: dict, readiness: str | None = None):
        performance = performance_doctor.run(snapshot)
        if readiness:
            performance = {**performance, "readiness_status": readiness}
        macro = macro_coach.run(snapshot, performance)
        race = race_director.run(snapshot, macro)
        draft = head_coach_planner.run(snapshot, performance, macro, race)
        return performance, macro, race, draft

    def test_recovery_week_ends_with_bike_test_and_compiles_for_intervals(self) -> None:
        snapshot = self._snapshot()
        performance, macro, race, draft = self._plan(snapshot)

        self.assertEqual(macro["week_classification"], "recovery")
        self.assertEqual(macro["threshold_tests"]["scheduled"][0]["discipline"], "bike")
        self.assertEqual(validate_agent_output("macro_coach", macro), [])
        tests = [item for item in draft["sessions"] if item.get("threshold_test")]
        self.assertEqual(len(tests), 1)
        bike_test = tests[0]
        self.assertEqual(bike_test["day"], "Saturday")
        self.assertEqual(bike_test["session_name"], "FTP Test (20 min)")
        self.assertEqual(bike_test["duration_min"], 71)
        self.assertFalse(
            any(
                item["day"] == "Saturday" and "Long" in item["session_name"]
                for item in draft["sessions"]
                if item["discipline"] == "Ride"
            )
        )
        self.assertFalse(
            any(
                item["day"] == "Friday" and item["discipline"] == "WeightTraining"
                for item in draft["sessions"]
            )
        )

        workout = compile_session_to_event(bike_test, run_id="test")
        reviewed = syntax_guard.review(workout)
        description = reviewed["final_description"]
        self.assertIn("Blowout\n- 5m freeride", description)
        self.assertIn("Test Effort\n- 20m freeride", description)
        self.assertNotIn("- Test Effort", description)

        validator = validator_coach.run(snapshot, draft, performance, macro, race)
        self.assertFalse(
            any("recovery week" in item for item in validator["weaknesses"]),
            validator["weaknesses"],
        )

    def test_both_overdue_tests_stay_48h_apart(self) -> None:
        snapshot = self._snapshot({"pain_or_niggles": "none"})
        _, macro, _, draft = self._plan(snapshot, readiness="green")
        self.assertEqual(len(macro["threshold_tests"]["scheduled"]), 2)
        tests = {
            item["threshold_test"]["discipline"]: item
            for item in draft["sessions"]
            if item.get("threshold_test")
        }
        self.assertEqual(tests["run"]["day"], "Sunday")
        self.assertEqual(tests["run"]["duration_min"], 59)
        days = head_coach_planner.DAY_NAMES
        self.assertGreaterEqual(
            days.index(tests["run"]["day"]) - days.index(tests["bike"]["day"]), 2
        )

    def test_friel_30_option_b_from_weekly_checkin(self) -> None:
        snapshot = self._snapshot({"bike_test_protocol": "friel_30"})
        _, _, _, draft = self._plan(snapshot)
        bike_test = next(item for item in draft["sessions"] if item.get("threshold_test"))
        self.assertEqual(bike_test["session_name"], "FTP + LTHR Test (30 min)")
        self.assertEqual(bike_test["duration_min"], 61)

    def test_validator_flags_a_test_inside_a_load_week(self) -> None:
        snapshot = self._snapshot()
        performance, macro, race, draft = self._plan(snapshot)
        load_macro = {**macro, "week_classification": "build", "friel_cycle_type": "load"}
        validator = validator_coach.run(snapshot, draft, performance, load_macro, race)
        self.assertEqual(validator["verdict"], "revise")
        self.assertTrue(
            any("recovery week (Friel" in item for item in validator["weaknesses"])
        )


class ThresholdApprovalTests(unittest.TestCase):
    def test_approve_dry_run_previews_sport_setting_updates(self) -> None:
        config = load_config(mode="mock")
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "run"
            plan_week(
                config,
                athlete_profile_path="fixtures/athlete_profile.json",
                weekly_checkin_path="fixtures/weekly_checkin.json",
                output_dir=str(output_dir),
                auto_approve=True,
                dry_run=True,
            )
            final_plan = read_json(output_dir / "final_plan.json")
            self.assertIn("threshold_testing", final_plan)
            final_plan["threshold_testing"]["threshold_updates"] = [
                {
                    "discipline": "bike",
                    "sport": "Ride",
                    "sport_settings_id": 1,
                    "field": "ftp",
                    "current": 260,
                    "proposed": 270,
                    "apply": True,
                },
                {
                    "discipline": "bike",
                    "sport": "Ride",
                    "sport_settings_id": 1,
                    "field": "lthr",
                    "current": 165,
                    "proposed": 171,
                    "apply": False,
                },
            ]
            write_json(output_dir / "final_plan.json", final_plan)
            # An edited plan needs a fresh approval before it can be pushed.
            finalize_run(str(output_dir), auto_approve=True)

            result = approve_run(config, str(output_dir), dry_run=True)
            update_result = read_json(result["threshold_update_result"])
            self.assertEqual(
                update_result["results"],
                [
                    {
                        "status": "dry-run",
                        "sport_settings_id": 1,
                        "payload": {"ftp": 270},
                    }
                ],
            )


if __name__ == "__main__":
    unittest.main()
