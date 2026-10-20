from __future__ import annotations

from datetime import date, timedelta
import unittest

from triathlon_agent.agents import performance_doctor
from triathlon_agent.memory import build_response_profile
from triathlon_agent.normalizer import (
    _classify_periodization_state,
    _extract_holidays,
    _infer_distance_from_text,
    normalize_snapshot,
)
from triathlon_agent.threshold_testing import (
    assess_threshold_testing,
    plan_threshold_tests,
)
from triathlon_agent.training_load import (
    EASY_QUALITY_RPE,
    _is_quality,
    assess_ramp,
    assess_run_load,
    assess_underload,
    assess_volume,
    estimate_training_load,
)
from triathlon_agent.utils import has_niggle, niggle_body_parts


PLANNING_DAY = date(2026, 10, 4)  # Sunday
WEEK_START = date(2026, 10, 5)


def _run(day: date, *, km: float, minutes: int, loss: float = 0, load: float = 50) -> dict:
    return {
        "start_date_local": f"{day.isoformat()}T08:00:00",
        "type": "Run",
        "name": "Run",
        "distance": km * 1000,
        "moving_time": minutes * 60,
        "total_elevation_gain": loss,
        "total_elevation_loss": loss,
        "icu_training_load": load,
    }


def _underload_kwargs(**overrides) -> dict:
    kwargs = {
        "fitness": {
            "status": "available",
            "ctl_ramp_per_week": -0.5,
            "days_tsb_above_5_last_14": 12,
        },
        "quality": {"average_per_week": 0.5, "average_quality_rpe": None},
        "volume": {"ratio_to_reference": 0.5, "reference_hours": 8, "hours_per_week": 4},
        "hrv_gate": {"status": "normal"},
        "eftp": {"status": "available", "trend": "flat", "delta_pct": 0.5, "window_days": 42},
        "health_events": [],
        "active_phase": "build",
        "requested_week_type": None,
    }
    kwargs.update(overrides)
    return kwargs


class UnderloadReworkTests(unittest.TestCase):
    def test_hrv_is_context_only_and_eftp_needs_a_bike_race(self) -> None:
        result = assess_underload(**_underload_kwargs())
        counted = {item["marker"] for item in result["markers"]}
        context = {item["marker"] for item in result["context"]}
        self.assertNotIn("hrv_unchallenged", counted)
        self.assertIn("hrv_unchallenged", context)
        self.assertNotIn("eftp_not_rising", counted)
        self.assertIn("eftp_not_rising", context)
        with_bike = assess_underload(**_underload_kwargs(bike_relevant=True))
        self.assertIn("eftp_not_rising", {item["marker"] for item in with_bike["markers"]})

    def test_quality_rpe_threshold_is_cr10_easy(self) -> None:
        self.assertEqual(EASY_QUALITY_RPE, 4.0)
        result = assess_underload(
            **_underload_kwargs(quality={"average_per_week": 2.5, "average_quality_rpe": 6})
        )
        self.assertNotIn(
            "quality_sessions_feel_easy", {item["marker"] for item in result["markers"]}
        )

    def test_ctl_ramp_veto_caps_at_watch(self) -> None:
        fitness = {"status": "available", "ctl_ramp_per_week": 3.5, "days_tsb_above_5_last_14": 12}
        result = assess_underload(
            **_underload_kwargs(
                fitness=fitness,
                quality={"average_per_week": 0.5, "average_quality_rpe": 3},
            )
        )
        self.assertEqual(result["status"], "watch")
        self.assertTrue(result["vetoes"])

    def test_taper_transition_and_holiday_explain_underload(self) -> None:
        self.assertEqual(assess_underload(**_underload_kwargs())["status"], "detected")
        taper = assess_underload(
            **_underload_kwargs(
                priority_race={"name": "City Half", "distance": "Half Marathon", "days_to_race": 6, "priority": "A"}
            )
        )
        self.assertEqual(taper["status"], "explained")
        transition = assess_underload(
            **_underload_kwargs(
                last_race={"name": "Trail", "distance": "Ultra Trail", "days_to_race": -5, "priority": "A"}
            )
        )
        self.assertEqual(transition["status"], "explained")
        holiday = assess_underload(**_underload_kwargs(holiday_names=["Summer (2026-07-01 to 2026-07-21)"]))
        self.assertEqual(holiday["status"], "explained")

    def test_volume_uses_own_12_week_average_and_endurance_only(self) -> None:
        activities = []
        for offset in range(84):
            day = PLANNING_DAY - timedelta(days=offset)
            minutes = 60 if offset < 28 else 120
            activities.append(_run(day, km=10, minutes=minutes))
            activities.append(
                {"start_date_local": f"{day.isoformat()}T19:00:00", "type": "Tennis", "moving_time": 3600}
            )
        volume = assess_volume(activities, PLANNING_DAY, {"typical_training_hours_per_week": 4})
        self.assertEqual(volume["hours_per_week"], 7.0)
        self.assertEqual(volume["reference_source"], "recent_12_week_average")
        self.assertLess(volume["ratio_to_reference"], 0.7)
        self.assertGreater(volume["ratio_to_profile"], 1.0)


class RampAndRunLoadTests(unittest.TestCase):
    def test_acwr_spike_is_high(self) -> None:
        activities = [
            _run(PLANNING_DAY - timedelta(days=offset), km=8, minutes=45, load=30)
            for offset in range(7, 28)
        ]
        activities += [
            _run(PLANNING_DAY - timedelta(days=offset), km=15, minutes=90, load=110)
            for offset in range(0, 7)
        ]
        ramp = assess_ramp(activities, {"ctl_ramp_per_week": 2.0}, PLANNING_DAY)
        self.assertEqual(ramp["status"], "high")
        self.assertGreater(ramp["acwr"], 1.5)
        for key in ("ctl_ramp_per_week", "week_load_change_pct", "acwr", "acute_7d_load", "chronic_weekly_avg_load", "findings"):
            self.assertIn(key, ramp)

    def test_return_from_recovery_week_is_not_flagged(self) -> None:
        activities = [
            _run(PLANNING_DAY - timedelta(days=offset), km=10, minutes=60, load=60)
            for offset in list(range(0, 7)) + list(range(14, 28))
        ]
        activities += [_run(PLANNING_DAY - timedelta(days=10), km=5, minutes=30, load=40)]
        ramp = assess_ramp(activities, {"ctl_ramp_per_week": 1.0}, PLANNING_DAY)
        self.assertEqual(ramp["status"], "ok")

    def test_run_km_and_descent_jump_flags(self) -> None:
        activities = []
        for week in range(1, 6):
            monday = WEEK_START - timedelta(days=7 * week)
            big = week == 1
            activities.append(
                _run(monday + timedelta(days=6), km=20 if big else 12, minutes=120 if big else 70, loss=600 if big else 100)
            )
            activities.append(_run(monday + timedelta(days=2), km=10, minutes=55, loss=50))
        run_load = assess_run_load(activities, PLANNING_DAY, week_start=WEEK_START)
        self.assertEqual(len(run_load["weeks"]), 8)
        self.assertIn("run_km_jump", run_load["flags"])
        self.assertIn("descent_jump", run_load["flags"])
        self.assertEqual(run_load["weeks"][-1]["longest_run_km"], 20.0)
        self.assertEqual(run_load["longest_run_min_28d"], 120.0)


class EstimatedLoadAndQualityTests(unittest.TestCase):
    def test_tennis_without_load_gets_an_estimate(self) -> None:
        tennis = {"type": "Tennis", "moving_time": 3600, "icu_training_load": None}
        self.assertEqual(estimate_training_load(tennis), 45.0)
        with_hr = {**tennis, "average_heartrate": 140, "lthr": 175}
        self.assertAlmostEqual(estimate_training_load(with_hr), 64.0, places=0)
        self.assertIsNone(estimate_training_load({**tennis, "icu_training_load": 20}))

    def test_hilly_easy_trail_run_uses_pace_zones(self) -> None:
        run = {
            "type": "TrailRun",
            "name": "Long easy trail run",
            "icu_hr_zone_times": [1200, 2400, 1500, 300, 0, 0, 0],
            "pace_zone_times": [2000, 3300, 100, 0, 0, 0, 0],
        }
        self.assertFalse(_is_quality(run))
        self.assertTrue(_is_quality({**run, "pace_zone_times": None}))


class NigglesAndParsingTests(unittest.TestCase):
    def test_no_niggle_phrases(self) -> None:
        for text in ("none", "geen", "ITB fine", "no pain", "niets", "", "Pending athlete check-in."):
            self.assertFalse(has_niggle(text), text)
        self.assertTrue(has_niggle("ITB a bit sore on downhills"))
        self.assertTrue(has_niggle("ITB fine but left calf tight"))
        self.assertEqual(niggle_body_parts("ITB fine but left calf tight"), ["calf"])

    def test_trail_and_ultra_distances(self) -> None:
        self.assertEqual(_infer_distance_from_text("TrailRun", "Hill Trail", "50 km"), "Ultra Trail")
        self.assertEqual(_infer_distance_from_text("Run", "Hill Trail", "50 km"), "Ultra Trail")
        self.assertNotEqual(_infer_distance_from_text("Run", "Night 110k"), "10k")
        self.assertEqual(_infer_distance_from_text("Run", "City Half Marathon"), "Half Marathon")

    def test_partial_week_is_never_recovery(self) -> None:
        windows = [
            {"week_start": f"w{index}", "load": 400.0, "minutes": 500, "partial": False}
            for index in range(5)
        ]
        windows.append({"week_start": "w5", "load": 150.0, "minutes": 200, "partial": True})
        state = _classify_periodization_state(windows)
        self.assertEqual(state["classified_weeks"][-1]["week_type"], "in_progress")
        self.assertEqual(state["consecutive_load_weeks"], 5)
        self.assertEqual(state["last_week_type"], "load")

    def test_holidays_are_extracted_with_planning_week_overlap(self) -> None:
        events = [
            {
                "category": "HOLIDAY",
                "name": "Ardennes",
                "start_date_local": "2026-10-08T00:00:00",
                "end_date_local": "2026-10-13T00:00:00",
            },
            {"category": "NOTE", "name": "x", "start_date_local": "2026-10-06T00:00:00"},
        ]
        holidays = _extract_holidays(events, PLANNING_DAY, WEEK_START)
        self.assertEqual(len(holidays), 1)
        self.assertTrue(holidays[0]["in_planning_week"])
        self.assertEqual(holidays[0]["days_overlap_planning_week"], 4)
        self.assertEqual(holidays[0]["end_date"], "2026-10-12")


def _doctor_snapshot(**overrides) -> dict:
    snapshot = {
        "athlete_profile": {"name": "Test", "injury_history": ["ITB syndrome"]},
        "weekly_checkin": {
            "pain_or_niggles": "none",
            "unexpected_fatigue": "",
            "last_week_summary": "Long run on Sunday went well, Friday swim too.",
            "subjective_readiness": 8,
            "current_motivation": 8,
        },
        "planning_date": PLANNING_DAY.isoformat(),
        "planning_constraints": {},
        "prior_plan_adherence": {},
        "athlete_memory": {},
        "latest_wellness": {},
        "recent_wellness": [],
        "load_change_pct": 0,
        "recent_week_load": 300,
        "prior_week_load": 300,
        "weekly_windows": [],
        "intervals": {"activities": []},
        "derived_baselines": {
            "hrv": {"baseline": 60, "acute": 60, "delta": 0, "delta_pct": 0, "confidence": "high", "acute_entries": 7},
            "resting_hr": {"baseline": 44, "acute": 44, "delta": 0, "delta_pct": 0, "confidence": "high", "acute_entries": 7},
            "sleep_hours": {"baseline": 7.5, "acute": 7.5, "delta": 0, "delta_pct": 0, "confidence": "high", "acute_entries": 7},
        },
        "training_load": {
            "ramp": {"status": "ok", "acwr": 1.0, "acute_7d_load": 300, "chronic_weekly_avg_load": 300, "findings": []},
            "fitness": {"tsb": -5},
            "hrv_gate": {"status": "normal", "extra_intensity_allowed": True},
        },
    }
    snapshot.update(overrides)
    return snapshot


class DoctorReadinessTests(unittest.TestCase):
    def test_baseline_snapshot_is_green_without_false_penalties(self) -> None:
        result = performance_doctor.run(_doctor_snapshot())
        self.assertEqual(result["readiness_status"], "green")
        self.assertNotIn("late_week_subjective_fatigue", result["fatigue_markers"])
        self.assertNotIn("pain_or_niggles", result["fatigue_markers"])
        self.assertNotIn("calf", result["main_risk"])

    def test_missing_wellness_signals_cost_points_and_confidence(self) -> None:
        full = performance_doctor.run(_doctor_snapshot())
        missing = {
            key: {"baseline": None, "acute": None, "confidence": "missing"}
            for key in ("hrv", "resting_hr", "sleep_hours")
        }
        result = performance_doctor.run(_doctor_snapshot(derived_baselines=missing))
        self.assertEqual(full["readiness_score"] - result["readiness_score"], 9)
        self.assertEqual(result["readiness_confidence"], "low")
        stale = _doctor_snapshot()
        stale["derived_baselines"]["hrv"]["acute_entries"] = 0
        self.assertEqual(performance_doctor.run(stale)["baseline_sources"]["hrv"], "missing")

    def test_niggle_text_is_echoed_not_a_hardcoded_calf(self) -> None:
        snapshot = _doctor_snapshot()
        snapshot["weekly_checkin"]["pain_or_niggles"] = "Right knee sore after descents"
        result = performance_doctor.run(snapshot)
        self.assertIn("pain_or_niggles", result["fatigue_markers"])
        self.assertTrue(any("knee" in item for item in result["warnings"]))
        self.assertFalse(any("calf" in item.lower() for item in result["warnings"]))

    def test_late_week_fatigue_only_from_fatigue_text(self) -> None:
        snapshot = _doctor_snapshot()
        snapshot["weekly_checkin"]["unexpected_fatigue"] = "Very tired on Saturday and Sunday."
        self.assertIn(
            "late_week_subjective_fatigue", performance_doctor.run(snapshot)["fatigue_markers"]
        )
        snapshot["weekly_checkin"]["unexpected_fatigue"] = "Busy Friday at work, not tired."
        self.assertNotIn(
            "late_week_subjective_fatigue", performance_doctor.run(snapshot)["fatigue_markers"]
        )

    def test_late_week_load_split_reads_raw_activities(self) -> None:
        activities = [
            _run(date(2026, 9, 29), km=5, minutes=30, load=20),
            _run(date(2026, 10, 3), km=20, minutes=120, load=120),
            _run(date(2026, 10, 4), km=15, minutes=90, load=90),
        ]
        result = performance_doctor.run(_doctor_snapshot(intervals={"activities": activities}))
        self.assertIn("late_week_load_concentration", result["fatigue_markers"])

    def test_intervals_fatigue_scale(self) -> None:
        snapshot = _doctor_snapshot(
            latest_wellness={"fatigue": 3, "soreness": None},
            recent_wellness=[{"fatigue": 2}, {"fatigue": 3}],
        )
        result = performance_doctor.run(snapshot)
        self.assertIn("latest_fatigue_high", result["fatigue_markers"])
        self.assertNotIn("latest_soreness_high", result["fatigue_markers"])

    def test_above_normal_hrv_under_high_load_is_not_a_bonus(self) -> None:
        snapshot = _doctor_snapshot()
        snapshot["training_load"]["hrv_gate"] = {"status": "above_normal", "extra_intensity_allowed": True}
        snapshot["training_load"]["fitness"] = {"tsb": -28}
        result = performance_doctor.run(snapshot)
        self.assertIn("possible_overreaching_hrv_pattern", result["fatigue_markers"])
        self.assertLessEqual(result["intensity_allowance"]["max_quality_sessions"], 2)
        self.assertFalse(any("above its normal band" in item for item in result["positive_findings"]))

    def test_suppressed_hrv_on_low_load_is_non_training_stress(self) -> None:
        snapshot = _doctor_snapshot()
        snapshot["training_load"]["hrv_gate"] = {"status": "suppressed", "extra_intensity_allowed": False}
        snapshot["training_load"]["fitness"] = {"tsb": 8}
        result = performance_doctor.run(snapshot)
        self.assertIn("non_training_stress", result["fatigue_markers"])
        self.assertLessEqual(result["intensity_allowance"]["max_quality_sessions"], 1)

    def test_ramp_status_drives_status_and_load(self) -> None:
        snapshot = _doctor_snapshot()
        snapshot["training_load"]["ramp"] = {"status": "high", "acwr": 1.7, "findings": ["spike"]}
        result = performance_doctor.run(snapshot)
        self.assertIn(result["readiness_status"], {"yellow", "red"})
        self.assertEqual(result["load_recommendation"], "reduce")
        snapshot["training_load"]["ramp"] = {"status": "elevated", "acwr": 1.35, "findings": ["up"]}
        elevated = performance_doctor.run(snapshot)
        self.assertIn(elevated["load_recommendation"], {"hold", "reduce"})

    def test_objective_markers_can_reach_red(self) -> None:
        snapshot = _doctor_snapshot()
        snapshot["derived_baselines"]["hrv"].update({"acute": 52, "delta": -8, "delta_pct": -13})
        snapshot["derived_baselines"]["resting_hr"].update({"acute": 50, "delta": 6})
        snapshot["derived_baselines"]["sleep_hours"].update({"acute": 6.6, "delta": -0.9})
        result = performance_doctor.run(snapshot)
        self.assertEqual(result["readiness_status"], "red")
        self.assertEqual(result["load_recommendation"], "reduce")

    def test_extended_illness_return_protocol(self) -> None:
        event = {
            "kind": "sick",
            "name": "COVID",
            "description": "covid, fever",
            "start_date": "2026-09-10",
            "end_date": "2026-09-20",
            "days_since_end": 10,
            "ongoing": False,
            "duration_days": 11,
            "severity": "extended",
        }
        result = performance_doctor.run(_doctor_snapshot(recent_health_events=[event]))
        self.assertEqual(result["readiness_status"], "yellow")
        self.assertEqual(result["health_events"]["return_protocol"], "extended")
        self.assertLessEqual(result["intensity_allowance"]["max_quality_sessions"], 1)
        self.assertTrue(any("stage 1" in item for item in result["constraints"]))
        old_cold = {**event, "severity": "standard", "days_since_end": 20}
        cold = performance_doctor.run(_doctor_snapshot(recent_health_events=[old_cold]))
        self.assertEqual(cold["health_events"]["status"], "none")

    def test_itb_history_with_descent_jump_warns(self) -> None:
        snapshot = _doctor_snapshot()
        snapshot["training_load"]["run_load"] = {"flags": ["descent_jump"], "findings": ["D- jumped"]}
        result = performance_doctor.run(snapshot)
        self.assertIn("itb_risk_run_load_jump", result["fatigue_markers"])
        self.assertNotEqual(result["load_recommendation"], "increase")


SPORT_SETTINGS = {
    "Ride": {"id": 1, "types": ["Ride"], "ftp": 290},
    "Run": {"id": 2, "types": ["Run"], "threshold_pace": 4.08},
    "Swim": {"id": 3, "types": ["Swim"], "threshold_pace": 0.93, "pace_units": "SECS_100M"},
}


def _assess_testing(**overrides) -> dict:
    kwargs = {
        "athlete_profile": {},
        "weekly_checkin": {"pain_or_niggles": "ITB fine"},
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


class ThresholdFixTests(unittest.TestCase):
    def test_rolling_eftp_beats_single_ride_pm_ftp(self) -> None:
        activities = [
            {"type": "Ride", "start_date_local": "2026-09-20T08:00:00", "icu_rolling_ftp": 266, "icu_pm_ftp": 225},
            {"type": "Ride", "start_date_local": "2026-10-01T08:00:00", "icu_pm_ftp": 177},
        ]
        result = _assess_testing(activities=activities)
        bike = result["bike"]
        self.assertEqual(bike["eftp_rolling"], 266)
        self.assertTrue(bike["stale"])
        self.assertEqual(bike["threshold_status"], "stale")
        self.assertEqual(bike["ftp_target_scale"], round(266 / 290, 3))
        interim = next(
            item for item in result["proposed_updates"] if item["source"] == "eftp_interim"
        )
        self.assertFalse(interim["apply"])
        self.assertEqual(interim["proposed"], 266)
        self.assertEqual(result["run"]["blockers"], [])

    def test_swim_is_tracked_but_never_scheduled(self) -> None:
        result = _assess_testing()
        self.assertIn("swim", result)
        self.assertFalse(result["swim"]["auto_schedule"])
        decision = plan_threshold_tests(result, recovery_week=True, readiness_status="green")
        self.assertNotIn("swim", {item["discipline"] for item in decision["scheduled"]})

    def test_block_week_one_test_is_opt_in(self) -> None:
        testing = _assess_testing(
            block_context={"last_week_type": "recovery", "next_block_week": 1}
        )
        default = plan_threshold_tests(testing, recovery_week=False, readiness_status="green")
        self.assertEqual(default["scheduled"], [])
        allowed = plan_threshold_tests(
            testing, recovery_week=False, readiness_status="green", allow_block_week_1=True
        )
        self.assertTrue(allowed["scheduled"])
        self.assertEqual(allowed["scheduled"][0]["placement"], "block_week_1")
        amber = plan_threshold_tests(testing, recovery_week=True, readiness_status="amber")
        self.assertEqual([item["discipline"] for item in amber["scheduled"]], ["bike"])


class AdherenceAndMemoryTests(unittest.TestCase):
    def _snapshot(self) -> dict:
        plan = {
            "readiness_status": "green",
            "sessions": [
                {
                    "date": "2026-09-29",
                    "day": "Tuesday",
                    "discipline": "Run",
                    "session_name": "Threshold Run",
                    "duration_min": 60,
                    "priority": "key",
                    "daniels_type": "T",
                },
                {
                    "date": "2026-10-01",
                    "day": "Thursday",
                    "discipline": "Run",
                    "session_name": "Easy Run",
                    "duration_min": 45,
                    "priority": "support",
                    "daniels_type": "E",
                },
            ],
        }
        activities = [
            {
                "start_date_local": "2026-09-29T07:00:00",
                "type": "Run",
                "name": "Threshold Run",
                "moving_time": 3600,
                "icu_training_load": 60,
                "pace_zone_times": [1800, 1500, 200, 100, 0, 0, 0],
                "decoupling": 7.2,
                "average_feels_like": 27.0,
                "icu_rpe": 5,
                "session_rpe": 300,
            },
            {
                "start_date_local": "2026-10-01T07:00:00",
                "type": "Run",
                "name": "Easy Run",
                "moving_time": 2700,
                "icu_training_load": 40,
                "pace_zone_times": [600, 900, 600, 600, 0, 0, 0],
            },
            {
                "start_date_local": "2026-09-30T18:00:00",
                "type": "Ride",
                "name": "Group ride",
                "moving_time": 5400,
                "icu_training_load": 90,
            },
            {
                "start_date_local": "2026-09-30T20:00:00",
                "type": "Tennis",
                "name": "Tennis",
                "moving_time": 3600,
                "icu_training_load": None,
            },
        ]
        return normalize_snapshot(
            {
                "athlete_profile": {"name": "Test"},
                "weekly_checkin": {"planning_date": "2026-10-04", "pain_or_niggles": "calf tight"},
                "race_calendar": {"races": []},
                "prior_plan_contexts": [
                    {
                        "source_run": "runs/x",
                        "final_plan": plan,
                        "weekly_checkin": {"pain_or_niggles": "left calf sore"},
                    }
                ],
            },
            {
                "activities": activities,
                "wellness": [
                    {"id": "2026-10-03", "sleepQuality": 3, "sleepScore": 70},
                    {"id": "2026-10-04", "sleepQuality": 2, "sleepScore": 80},
                ],
                "events": [],
                "sport_settings": [],
            },
        )

    def test_intensity_compliance_unplanned_and_signals(self) -> None:
        snapshot = self._snapshot()
        adherence = snapshot["prior_plan_adherence"]
        reviews = {item["session_name"]: item for item in adherence["session_reviews"]}
        self.assertEqual(reviews["Threshold Run"]["intensity_compliance"], "easier_than_planned")
        self.assertEqual(reviews["Easy Run"]["intensity_compliance"], "harder_than_planned")
        self.assertTrue(reviews["Threshold Run"]["decoupling_flag"])
        self.assertIn("decoupling", reviews["Threshold Run"]["interpretation"])
        self.assertTrue(reviews["Threshold Run"]["heat_flag"])
        self.assertEqual(reviews["Threshold Run"]["session_rpe"], 300)
        self.assertEqual([item["type"] for item in adherence["unplanned_sessions"]], ["Ride"])
        self.assertEqual(adherence["unplanned_load"], 90.0)
        self.assertEqual(snapshot["recent_wellness_summary"]["sleep_quality_latest_label"], "good")
        other = snapshot["training_load"]["other_load"]
        self.assertEqual(other["racket_sessions_7d"], 1)
        self.assertEqual(other["estimated_load_7d"], 45.0)
        self.assertEqual(snapshot["recent_week_load"], 235.0)
        for key in ("ramp", "run_load", "ride_load", "other_load"):
            self.assertIn(key, snapshot["training_load"])
        self.assertIn("holidays", snapshot)
        self.assertIn("ftp_target_scale", snapshot["threshold_testing"]["bike"])

    def test_response_profile(self) -> None:
        profile = self._snapshot()["athlete_memory"]["response_profile"]
        self.assertEqual(profile["key_sessions_reviewed"], 1)
        self.assertEqual(profile["key_completed_easier"], 1)
        self.assertEqual(profile["typical_green_week_load"], 235.0)
        self.assertEqual(profile["repeated_niggles"], ["calf"])
        self.assertEqual(build_response_profile([])["key_sessions_reviewed"], 0)


if __name__ == "__main__":
    unittest.main()
