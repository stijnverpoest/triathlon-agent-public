from __future__ import annotations

from datetime import date, timedelta
import unittest

from triathlon_agent.agents import performance_doctor, validator_coach
from triathlon_agent.progression import (
    LADDERS,
    assess_progression,
    classify_target,
    next_rung,
    race_distance_key,
    session_dose,
)
from triathlon_agent.threshold_testing import plan_threshold_tests
from triathlon_agent.training_load import (
    assess_fitness,
    assess_hrv_gate,
    assess_intensity_distribution,
    assess_training_load,
    assess_underload,
)


PLANNING_DAY = date(2026, 10, 5)


def _wellness(days: int, *, ctl_start: float, ctl_step: float, tsb: float, hrv) -> list:
    entries = []
    for offset in range(days):
        day = PLANNING_DAY - timedelta(days=days - 1 - offset)
        ctl = ctl_start + ctl_step * offset
        entries.append(
            {
                "id": day.isoformat(),
                "ctl": ctl,
                "atl": ctl - tsb,
                "hrv": hrv(offset) if callable(hrv) else hrv,
            }
        )
    return entries


def _activity(day: date, *, kind: str, minutes: int, zones: list[int], rpe=None, feel=None, name="Ride"):
    return {
        "start_date_local": f"{day.isoformat()}T08:00:00",
        "type": kind,
        "name": name,
        "moving_time": minutes * 60,
        "icu_hr_zone_times": [value * 60 for value in zones],
        "icu_rpe": rpe,
        "feel": feel,
    }


class FitnessAndDistributionTests(unittest.TestCase):
    def test_fitness_reports_ramp_and_positive_tsb_days(self) -> None:
        wellness = _wellness(30, ctl_start=50, ctl_step=-0.1, tsb=10, hrv=60)
        result = assess_fitness(wellness, PLANNING_DAY)
        self.assertEqual(result["status"], "available")
        self.assertLess(result["ctl_ramp_per_week"], 0)
        self.assertEqual(result["days_tsb_above_5_last_14"], 14)

    def test_distribution_uses_three_zone_model(self) -> None:
        activities = [
            _activity(PLANNING_DAY - timedelta(days=3), kind="Run", minutes=60, zones=[30, 30, 0, 0, 0]),
            _activity(PLANNING_DAY - timedelta(days=5), kind="Ride", minutes=60, zones=[20, 20, 10, 10, 0]),
            _activity(PLANNING_DAY - timedelta(days=6), kind="Tennis", minutes=60, zones=[0, 0, 0, 0, 60]),
        ]
        result = assess_intensity_distribution(activities, PLANNING_DAY)
        self.assertAlmostEqual(result["low_pct"], 83.3, places=1)
        self.assertAlmostEqual(result["moderate_pct"], 16.7, places=1)
        self.assertEqual(result["model"], "pyramidal")

    def test_hrv_gate_bands(self) -> None:
        normal = assess_hrv_gate(_wellness(50, ctl_start=50, ctl_step=0, tsb=0, hrv=lambda i: 60 + (i % 3)))
        self.assertEqual(normal["status"], "normal")
        self.assertTrue(normal["extra_intensity_allowed"])

        suppressed = assess_hrv_gate(
            _wellness(50, ctl_start=50, ctl_step=0, tsb=0, hrv=lambda i: 50 if i >= 43 else 60 + (i % 3))
        )
        self.assertEqual(suppressed["status"], "suppressed")
        self.assertFalse(suppressed["extra_intensity_allowed"])

        sparse = assess_hrv_gate(_wellness(5, ctl_start=50, ctl_step=0, tsb=0, hrv=60))
        self.assertEqual(sparse["status"], "insufficient_data")


class UnderloadTests(unittest.TestCase):
    def _load(self, health_events=None, phase=None):
        wellness = _wellness(50, ctl_start=48, ctl_step=-0.1, tsb=12, hrv=lambda i: 60 + (i % 3))
        activities = [
            _activity(PLANNING_DAY - timedelta(days=offset), kind="Run", minutes=45, zones=[25, 20, 0, 0, 0], rpe=2)
            for offset in range(1, 28, 3)
        ]
        return assess_training_load(
            activities=activities,
            wellness=wellness,
            athlete_profile={"typical_training_hours_per_week": 12},
            athlete_state={"active_macro_cycle": {"phase": phase}} if phase else {},
            health_events=health_events or [],
            planning_date=PLANNING_DAY,
        )

    def test_underload_detected_on_flat_easy_low_volume_block(self) -> None:
        underload = self._load()["underload"]
        self.assertEqual(underload["status"], "detected")
        markers = {item["marker"] for item in underload["markers"]}
        self.assertTrue(
            {"tsb_persistently_positive", "ctl_flat_or_falling", "low_quality_frequency", "volume_below_profile"}
            <= markers
        )

    def test_illness_or_taper_explains_underload(self) -> None:
        self.assertEqual(
            self._load(health_events=[{"kind": "sick"}])["underload"]["status"], "explained"
        )
        self.assertEqual(self._load(phase="taper")["underload"]["status"], "explained")

    def test_hrv_marker_alone_is_not_underload(self) -> None:
        result = assess_underload(
            fitness={"status": "available", "ctl_ramp_per_week": 4.0, "days_tsb_above_5_last_14": 0},
            quality={"average_per_week": 2.5, "average_quality_rpe": 8},
            volume={"ratio_to_profile": 1.0},
            hrv_gate={"status": "normal"},
            eftp={"status": "available", "trend": "up", "delta_pct": 4, "window_days": 42},
            health_events=[],
            active_phase="build",
            requested_week_type=None,
        )
        self.assertEqual(result["status"], "not_detected")


class ReadinessSymmetryTests(unittest.TestCase):
    def _snapshot(self, **training_load):
        return {
            "athlete_profile": {"name": "Test"},
            "weekly_checkin": {"subjective_readiness": 9, "current_motivation": 9},
            "planning_constraints": {},
            "prior_plan_adherence": {},
            "athlete_memory": {},
            "latest_wellness": {},
            "load_change_pct": 0,
            "recent_week_load": 300,
            "prior_week_load": 300,
            "weekly_windows": [],
            "derived_baselines": {
                "hrv": {"baseline": 60, "acute": 66, "delta": 6, "delta_pct": 10},
                "resting_hr": {"baseline": 44, "acute": 41, "delta": -3, "delta_pct": -6},
                "sleep_hours": {"baseline": 7.0, "acute": 7.5, "delta": 0.5, "delta_pct": 7},
            },
            "training_load": training_load,
        }

    def test_strong_signals_add_points(self) -> None:
        result = performance_doctor.run(self._snapshot(hrv_gate={"status": "above_normal", "extra_intensity_allowed": True}))
        self.assertGreater(result["readiness_score"], 82)
        self.assertTrue(result["positive_findings"])

    def test_green_with_underload_and_open_hrv_gate_allows_three_and_increases(self) -> None:
        result = performance_doctor.run(
            self._snapshot(
                hrv_gate={"status": "normal", "extra_intensity_allowed": True},
                underload={"status": "detected", "markers": [], "recommendation": "Progress."},
            )
        )
        self.assertEqual(result["readiness_status"], "green")
        self.assertEqual(result["load_recommendation"], "increase")
        self.assertEqual(result["intensity_allowance"]["max_quality_sessions"], 3)
        self.assertEqual(result["underload"]["status"], "detected")

    def test_suppressed_hrv_keeps_default_two(self) -> None:
        result = performance_doctor.run(
            self._snapshot(hrv_gate={"status": "suppressed", "extra_intensity_allowed": False})
        )
        self.assertEqual(result["intensity_allowance"]["max_quality_sessions"], 2)


class ProgressionTests(unittest.TestCase):
    def test_ladders_never_fall_in_time_in_zone(self) -> None:
        for family, ladder in LADDERS.items():
            totals = [reps * minutes for reps, minutes in ladder["rungs"]]
            self.assertEqual(totals, sorted(totals), family)

    def test_targets_map_to_families(self) -> None:
        self.assertEqual(classify_target("Ride", "88-93% FTP"), "bike_sweet_spot")
        self.assertEqual(classify_target("Ride", "95-100% FTP"), "bike_threshold")
        self.assertEqual(classify_target("Ride", "106-120% FTP"), "bike_vo2")
        self.assertEqual(classify_target("Ride", "65-75% FTP"), None)
        self.assertEqual(classify_target("Run", "98-102% Pace"), "run_threshold")
        self.assertEqual(classify_target("Run", "106-110% Pace"), "run_interval")
        self.assertEqual(classify_target("Run", "Z2 HR"), None)

    def test_session_dose_counts_repeats(self) -> None:
        session = {
            "discipline": "Run",
            "blocks": [
                {"section": "Warmup", "steps": [{"duration": "15m", "target": "70% Pace"}]},
                {"section": "Main Set", "repeat": 3, "steps": [{"duration": "10m", "target": "98-102% Pace"}, {"duration": "2m", "target": "65% Pace"}]},
            ],
        }
        self.assertEqual(session_dose(session), {"run_threshold": 30.0})

    def test_history_progresses_from_last_completed_dose(self) -> None:
        contexts = [
            {
                "final_plan": {
                    "sessions": [
                        {
                            "date": "2026-09-15",
                            "discipline": "Run",
                            "session_name": "T run",
                            "blocks": [{"repeat": 3, "steps": [{"duration": "10m", "target": "98-102% Pace"}]}],
                        }
                    ]
                }
            }
        ]
        history = [{"session_reviews": [{"session_name": "T run", "date": "2026-09-15", "status": "completed"}]}]
        result = assess_progression(contexts, history)
        self.assertEqual(result["last_dose_by_family"]["run_threshold"]["minutes"], 30.0)
        self.assertEqual(result["next_rung_if_green"]["run_threshold"]["time_in_zone_min"], 36)

    def test_next_rung_rules(self) -> None:
        last = {"minutes": 30.0, "completed": True}
        common = {"phase": "build", "block_week": 1}
        self.assertEqual(next_rung("run_threshold", last, underload_status="not_detected", readiness_status="green", **common), 3)
        self.assertEqual(next_rung("run_threshold", last, underload_status="detected", readiness_status="green", **common), 4)
        self.assertEqual(next_rung("run_threshold", last, underload_status="detected", readiness_status="yellow", **common), 2)

    def test_race_distance_keys(self) -> None:
        self.assertEqual(race_distance_key("City Half Marathon"), "half_marathon")
        self.assertEqual(race_distance_key("Ironman 70.3"), "70.3")
        self.assertEqual(race_distance_key("UTMB OCC ultra trail 55k"), "ultra_trail")
        self.assertEqual(race_distance_key("Olympic"), "olympic")


class RestAndTestTests(unittest.TestCase):
    def _testing(self, bike_days: int, run_days: int, *, recent_a_race=None) -> dict:
        def item(days):
            return {
                "status": "not_due" if days < 42 else "due",
                "urgency": "not_due" if days < 42 else "due",
                "reason": "",
                "blockers": [],
                "days_since_last_test": days,
                "last_test_date": (PLANNING_DAY - timedelta(days=days)).isoformat(),
                "protocol": "coggan_20",
            }

        return {"bike": item(bike_days), "run": {**item(run_days), "protocol": "friel_30_run"}, "recent_a_race": recent_a_race}

    def test_every_recovery_week_gets_a_test(self) -> None:
        result = plan_threshold_tests(self._testing(30, 45), recovery_week=True, readiness_status="green")
        # Only the discipline that is due (>= 42 days) is tested; the bike (30 d) is not.
        self.assertEqual(len(result["scheduled"]), 1)
        self.assertEqual(result["scheduled"][0]["discipline"], "run")

    def test_no_test_when_both_tested_this_month(self) -> None:
        result = plan_threshold_tests(self._testing(10, 14), recovery_week=True, readiness_status="green")
        self.assertEqual(result["scheduled"], [])
        self.assertIn("less than 42 days", result["rationale"])

    def test_both_disciplines_after_a_race(self) -> None:
        result = plan_threshold_tests(
            self._testing(45, 50, recent_a_race={"name": "A", "days_since": 10}),
            recovery_week=True,
            readiness_status="green",
        )
        self.assertEqual({entry["discipline"] for entry in result["scheduled"]}, {"bike", "run"})

    def test_load_week_never_tests(self) -> None:
        result = plan_threshold_tests(self._testing(60, 60), recovery_week=False, readiness_status="green")
        self.assertEqual(result["scheduled"], [])


class ValidatorDoseTests(unittest.TestCase):
    def _session(self, day: str, date_text: str, **extra) -> dict:
        return {"day": day, "date": date_text, "session_name": f"{day} session", "duration_min": 60, **extra}

    def test_validator_flags_underdosed_green_week(self) -> None:
        sessions = [
            self._session("Tuesday", "2026-10-06", discipline="Run", primary_metric="Pace", daniels_type="T", priority="key"),
            self._session("Thursday", "2026-10-08", discipline="Run", primary_metric="HR", daniels_type="E"),
        ]
        performance = {"readiness_status": "green", "intensity_allowance": {"max_quality_sessions": 3}}
        result = validator_coach.run(
            {"planning_constraints": {}}, {"sessions": sessions}, performance,
            {"week_classification": "build", "friel_cycle_type": "load"}, {},
        )
        self.assertTrue(any("under-dosed" in item for item in result["weaknesses"]))

    def test_validator_flags_missing_scheduled_test(self) -> None:
        sessions = [self._session("Sunday", "2026-10-11", discipline="Run", primary_metric="HR", daniels_type="E")]
        macro = {
            "week_classification": "recovery",
            "friel_cycle_type": "recovery",
            "threshold_tests": {"scheduled": [{"discipline": "bike", "protocol": "coggan_20"}]},
        }
        result = validator_coach.run(
            {"planning_constraints": {}}, {"sessions": sessions}, {"readiness_status": "green"}, macro, {}
        )
        self.assertEqual(result["verdict"], "revise")
        self.assertTrue(any("bike threshold test" in item for item in result["weaknesses"]))


if __name__ == "__main__":
    unittest.main()
