from __future__ import annotations

import copy
import unittest

from triathlon_agent.agents import (
    head_coach_planner,
    macro_coach,
    nutritionist,
    phase_alignment_auditor,
    race_director,
    training_philosophy_auditor,
    validator_coach,
)
from triathlon_agent.normalizer import normalize_snapshot
from triathlon_agent.plan_audit import audit_plan
from triathlon_agent.progression import (
    assess_progression,
    classify_target,
    next_rung,
    race_distance_key,
    session_dose,
)
from triathlon_agent.utils import read_json


GREEN = {
    "readiness_status": "green",
    "intensity_allowance": {"max_quality_sessions": 2},
    "underload": {"status": "not_detected"},
}
TRAIL_A_RACE = {
    "name": "Hill Trail 50k",
    "priority": "A",
    "distance": "TrailRun",
    "date": "2027-05-08",
}


def _fixture_snapshot() -> dict:
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


BASE_SNAPSHOT = _fixture_snapshot()


def _snapshot(*, week_start: str = "2027-03-01", races: list | None = None) -> dict:
    snapshot = copy.deepcopy(BASE_SNAPSHOT)
    snapshot["week_start"] = week_start
    snapshot["planning_date"] = week_start
    snapshot["races"] = races or []
    snapshot["planning_constraints"].update(
        time_caps_by_day={},
        strength_only_days=[],
        double_session_capacity_by_day={
            day: True for day in ("Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
        },
    )
    snapshot["periodization_state"] = {
        "next_block_week": 2,
        "recommended_next_week_type": "load",
    }
    snapshot.setdefault("training_load", {})
    snapshot["training_load"]["run_load"] = {
        "longest_run_min_28d": 130,
        "avg_weekly_elevation_gain_m_4w": 500,
    }
    snapshot["training_load"]["ride_load"] = {"longest_ride_min_28d": 150}
    return snapshot


def _plan(snapshot: dict, performance: dict = GREEN) -> tuple[dict, dict, dict]:
    macro = macro_coach.run(snapshot, performance)
    race = race_director.run(snapshot, macro)
    draft = head_coach_planner.run(snapshot, performance, macro, race)
    return macro, race, draft


def _race(days: int, **extra) -> dict:
    return {**TRAIL_A_RACE, "days_to_race": days, **extra}


class ProgressionFixTests(unittest.TestCase):
    def test_uphill_sessions_build_the_uphill_ladder(self) -> None:
        self.assertEqual(classify_target("Run", "Z4 HR", "Hill repeats"), "run_uphill_threshold")
        self.assertEqual(classify_target("Run", "Z4 HR"), "run_threshold")
        session = {
            "discipline": "Run",
            "session_name": "Treadmill incline repeats",
            "blocks": [{"repeat": 4, "steps": [{"duration": "6m", "target": "Z4 HR"}]}],
        }
        self.assertEqual(session_dose(session), {"run_uphill_threshold": 24.0})

    def test_progression_object_wins_over_rescaled_targets(self) -> None:
        session = {
            "discipline": "Ride",
            "progression": {"family": "bike_sweet_spot", "time_in_zone_min": 40},
            "blocks": [{"repeat": 2, "steps": [{"duration": "20m", "target": "75-79% FTP"}]}],
        }
        self.assertEqual(session_dose(session), {"bike_sweet_spot": 40.0})

    def test_gap_steps_back_down_the_ladder(self) -> None:
        last = {"minutes": 36.0, "completed": True, "date": "2026-09-01"}
        common = {
            "phase": "build",
            "block_week": 1,
            "underload_status": "not_detected",
            "readiness_status": "green",
        }
        self.assertEqual(next_rung("run_threshold", last, **common), 4)
        self.assertEqual(next_rung("run_threshold", last, planning_date="2026-09-10", **common), 4)
        self.assertEqual(next_rung("run_threshold", last, planning_date="2026-09-20", **common), 2)
        # 44-day gap: proportional detraining step-back of three rungs.
        self.assertEqual(next_rung("run_threshold", last, planning_date="2026-10-15", **common), 0)

    def test_assess_progression_accepts_planning_date(self) -> None:
        contexts = [
            {
                "final_plan": {
                    "sessions": [
                        {
                            "date": "2026-08-01",
                            "discipline": "Run",
                            "session_name": "T run",
                            "blocks": [{"repeat": 3, "steps": [{"duration": "10m", "target": "98-102% Pace"}]}],
                        }
                    ]
                }
            }
        ]
        history = [{"session_reviews": [{"session_name": "T run", "date": "2026-08-01", "status": "completed"}]}]
        result = assess_progression(contexts, history, planning_date="2026-10-01")
        self.assertEqual(result["last_dose_by_family"]["run_threshold"]["days_since_dose"], 61)
        self.assertLess(result["next_rung_if_green"]["run_threshold"]["time_in_zone_min"], 30)

    def test_race_distance_key_shares_buckets(self) -> None:
        self.assertEqual(race_distance_key("Kalmthoutse Heide trail 21k"), "trail")
        self.assertEqual(race_distance_key("Ironman Hamburg"), "ironman")
        self.assertEqual(race_distance_key("City 10k"), "10k")
        self.assertEqual(race_distance_key("something else"), "generic")


class MacroCoachFixTests(unittest.TestCase):
    def test_ultra_taper_uses_bucket_taper_days(self) -> None:
        macro = macro_coach.run(_snapshot(races=[_race(16)]), GREEN)
        self.assertEqual(macro["macro_phase"], "taper")
        self.assertEqual(macro["taper_days"], 18)
        self.assertEqual(macro_coach.run(_snapshot(races=[_race(60)]), GREEN)["macro_phase"], "race_specific")
        self.assertEqual(macro_coach.run(_snapshot(races=[_race(130)]), GREEN)["macro_phase"], "build")

    def test_persisted_race_phase_does_not_stick_without_race(self) -> None:
        snapshot = _snapshot()
        snapshot["athlete_state"] = {"active_macro_cycle": {"phase": "race_specific"}}
        self.assertEqual(macro_coach.run(snapshot, GREEN)["macro_phase"], "base")

    def test_transition_after_completed_a_race(self) -> None:
        macro = macro_coach.run(_snapshot(races=[_race(-5)]), GREEN)
        self.assertEqual(macro["macro_phase"], "transition")
        self.assertEqual(macro["week_classification"], "recovery")
        self.assertEqual(macro["threshold_tests"]["scheduled"], [])

    def test_block_week_prefers_fresh_periodization_state(self) -> None:
        snapshot = _snapshot(races=[_race(130)])
        snapshot["athlete_state"] = {
            "active_macro_cycle": {"current_block_week": 2, "cycle_type": "load"}
        }
        snapshot["periodization_state"] = {"next_block_week": 3, "recommended_next_week_type": "load"}
        self.assertEqual(macro_coach.run(snapshot, GREEN)["friel_block_week"], 3)

    def test_holiday_week_and_load_week_before_holiday(self) -> None:
        snapshot = _snapshot(races=[_race(130)])
        snapshot["holidays"] = [
            {"name": "Summer", "start_date": "2027-03-01", "end_date": "2027-03-21",
             "in_planning_week": True, "days_overlap_planning_week": 7}
        ]
        macro = macro_coach.run(snapshot, GREEN)
        self.assertEqual(macro["week_classification"], "holiday")
        self.assertEqual(macro["threshold_tests"]["scheduled"], [])

        before = _snapshot(races=[_race(130)])
        before["periodization_state"] = {"next_block_week": 4, "recommended_next_week_type": "recovery"}
        before["holidays"] = [
            {"name": "Summer", "start_date": "2027-03-08", "end_date": "2027-03-28",
             "in_planning_week": False, "days_overlap_planning_week": 0}
        ]
        macro = macro_coach.run(before, GREEN)
        self.assertEqual(macro["friel_cycle_type"], "load")

    def test_phase_alignment_matches_macro_taper_window(self) -> None:
        snapshot = _snapshot(races=[{"name": "70.3", "priority": "A", "distance": "Ironman 70.3", "days_to_race": 18}])
        macro, race, draft = _plan(snapshot)
        self.assertEqual(macro["macro_phase"], "race_specific")
        result = phase_alignment_auditor.run(snapshot, macro, race, draft)
        self.assertNotEqual(result["alignment_status"], "misaligned")


class PlannerFixTests(unittest.TestCase):
    def test_trail_build_week_specificity(self) -> None:
        snapshot = _snapshot(races=[_race(100)])
        snapshot["athlete_profile"]["injury_history"] = ["ITB syndrome"]
        snapshot["athlete_profile"]["weekly_sport_frequency"] = {"bike_sessions": 2, "run_sessions": 4}
        # Four build weeks done: the opening I block is over, uphill threshold leads.
        snapshot["athlete_state"] = {"history": [
            {"week_start": f"2027-02-{day:02d}", "phase": "build", "classification": "build"}
            for day in (1, 8, 15, 22)
        ]}
        _, _, draft = _plan(snapshot)
        sessions = {(s["day"], s["session_name"]): s for s in draft["sessions"]}
        long_run = sessions[("Sunday", "Long Trail Run")]
        # 130 recent (+15 at most); one run-impact progression per week.
        self.assertIn(long_run["duration_min"], (130, 145))
        self.assertIn(("Saturday", "Medium-Long Trail Run"), sessions)
        notes = " ".join(long_run["execution_notes"])
        self.assertIn("D+", notes)
        self.assertIn("ITB caution", notes)
        self.assertIn("Power-hike", notes)
        self.assertIn("treadmill incline", notes)
        rides = [s for s in draft["sessions"] if s["discipline"] == "Ride"]
        self.assertTrue(rides)
        self.assertTrue(all(s["duration_min"] < 120 for s in rides))
        uphill = next(s for s in draft["sessions"] if s.get("progression", {}).get("family") == "run_uphill_threshold")
        self.assertIn("treadmill", " ".join(uphill["execution_notes"]))
        self.assertEqual(draft["week_context"]["weekly_elevation_target_m"], 550)

    def test_race_week_placement_and_taper(self) -> None:
        snapshot = _snapshot(week_start="2027-05-03", races=[_race(5)])
        macro, _, draft = _plan(snapshot)
        by_day = {}
        for session in draft["sessions"]:
            by_day.setdefault(session["day"], []).append(session)
        race = by_day["Saturday"]
        self.assertEqual(len(race), 1)
        self.assertTrue(race[0]["race_day"])
        self.assertFalse(race[0]["push_to_intervals"])
        self.assertTrue(all(s["duration_min"] <= 30 for s in by_day["Friday"]))
        self.assertFalse(any(s["discipline"] in {"Run", "Ride"} for s in by_day.get("Sunday", [])))
        self.assertLessEqual(draft["quality_session_count"], 1)

    def test_taper_shortens_long_sessions(self) -> None:
        build = _plan(_snapshot(races=[_race(100)]))[2]
        taper = _plan(_snapshot(week_start="2027-04-26", races=[_race(12)]))[2]
        long_build = max(s["duration_min"] for s in build["sessions"] if s["discipline"] == "Run")
        long_taper = max(s["duration_min"] for s in taper["sessions"] if s["discipline"] == "Run")
        self.assertLessEqual(long_taper, 0.65 * long_build)
        self.assertLess(taper["weekly_time_budget"]["planned_min"], build["weekly_time_budget"]["planned_min"])

    def test_transition_week_is_easy_and_optional(self) -> None:
        _, _, draft = _plan(_snapshot(week_start="2027-05-10", races=[_race(-2)]))
        self.assertEqual(draft["quality_session_count"], 0)
        workouts = [s for s in draft["sessions"] if s["discipline"] in {"Run", "Ride"}]
        self.assertTrue(all(s["priority"] == "optional" for s in workouts))
        self.assertTrue(all(s["duration_min"] <= 60 for s in workouts))

    def test_brick_after_long_ride_for_70_3_build(self) -> None:
        snapshot = _snapshot(races=[{"name": "Coastal Half", "priority": "A", "distance": "Ironman 70.3", "days_to_race": 100}])
        _, _, draft = _plan(snapshot)
        brick = [s for s in draft["sessions"] if s["session_name"] == "Brick Run off the Bike"]
        self.assertEqual(len(brick), 1)
        self.assertEqual(brick[0]["day"], "Saturday")
        self.assertTrue(10 <= brick[0]["duration_min"] <= 30)

    def test_strength_periodized_by_phase(self) -> None:
        base = _plan(_snapshot(races=[_race(300)]))[2]
        taper = _plan(_snapshot(week_start="2027-04-26", races=[_race(12)]))[2]
        base_strength = [s for s in base["sessions"] if s["discipline"] == "WeightTraining"]
        taper_strength = [s for s in taper["sessions"] if s["discipline"] == "WeightTraining"]
        self.assertTrue(base_strength and base_strength[0]["duration_min"] >= 40)
        self.assertTrue(all(s["duration_min"] <= head_coach_planner.ACTIVATION_STRENGTH_MIN for s in taper_strength))

    def test_tennis_day_gets_no_key_session(self) -> None:
        snapshot = _snapshot(week_start="2026-07-06", races=[_race(300)])
        snapshot["athlete_profile"]["regular_training_schedule"] = {
            "Wednesday": "Easy ride; tennis 1h only in season (May-Sep)."
        }
        self.assertEqual(head_coach_planner.tennis_days(snapshot), ["Wednesday"])
        _, _, draft = _plan(snapshot)
        wednesday = [s for s in draft["sessions"] if s["day"] == "Wednesday"]
        thursday = [s for s in draft["sessions"] if s["day"] == "Thursday"]
        self.assertTrue(any(s["discipline"] == "Tennis" for s in wednesday))
        self.assertFalse(any(s.get("progression") for s in wednesday))
        self.assertFalse(any(s.get("progression") and s["discipline"] == "Run" for s in thursday))
        snapshot["week_start"] = "2026-11-02"
        self.assertEqual(head_coach_planner.tennis_days(snapshot), [])

    def test_travel_days_get_no_key_sessions(self) -> None:
        snapshot = _snapshot(races=[_race(130)])
        snapshot["weekly_checkin"]["time_constraints"] = ["Travel for work on Tuesday and Thursday."]
        snapshot["planning_constraints"]["travel_this_week"] = True
        _, _, draft = _plan(snapshot)
        travel = [s for s in draft["sessions"] if s["day"] in {"Tuesday", "Thursday"}]
        self.assertFalse(any(s.get("progression") or s["discipline"] == "Ride" for s in travel))

    def test_stale_ftp_scales_bike_targets(self) -> None:
        snapshot = _snapshot(races=[{"name": "70.3", "priority": "A", "distance": "Ironman 70.3", "days_to_race": 100}])
        snapshot["threshold_testing"]["bike"].update(stale=True, ftp_target_scale=0.85)
        snapshot["threshold_testing"]["proposed_updates"] = []
        _, _, draft = _plan(snapshot)
        quality_ride = next(s for s in draft["sessions"] if s.get("progression", {}).get("family") == "bike_sweet_spot")
        main = next(b for b in quality_ride["blocks"] if b.get("repeat"))
        self.assertEqual(main["steps"][0]["target"], "75-79% FTP")
        self.assertEqual(session_dose(quality_ride)["bike_sweet_spot"], quality_ride["progression"]["time_in_zone_min"])

    def test_club_swim_day_comes_from_profile_and_open_water_note(self) -> None:
        snapshot = _snapshot(races=[_race(130)])
        snapshot["athlete_profile"]["hard_constraints"] = {"always": ["Thursday club swim is fixed."]}
        snapshot["athlete_profile"]["soft_preferences"] = {}
        snapshot["planning_constraints"]["fixed_swim_days"] = ["Tuesday", "Thursday"]
        snapshot["planning_constraints"]["open_water_mode"] = True
        _, _, draft = _plan(snapshot)
        swims = {s["day"]: s for s in draft["sessions"] if s["discipline"] == "Swim"}
        self.assertEqual(swims["Thursday"]["session_name"], "Club Swim")
        self.assertEqual(swims["Tuesday"]["session_name"], "Pool Swim")
        self.assertIn("sight every", " ".join(swims["Tuesday"]["execution_notes"]))

    def test_unmet_frequency_is_warned(self) -> None:
        snapshot = _snapshot(races=[_race(130)])
        snapshot["athlete_profile"]["weekly_sport_frequency"] = {"bike_sessions": 2, "run_sessions": 7}
        _, _, draft = _plan(snapshot)
        self.assertTrue(any("requested runs" in warning for warning in draft["planner_warnings"]))


class ValidatorFixTests(unittest.TestCase):
    def _weekend(self, *, quality_saturday: bool = False) -> dict:
        saturday = {
            "day": "Saturday", "date": "2027-03-06", "discipline": "Ride",
            "session_name": "Long Endurance Ride", "duration_min": 150, "priority": "key",
            "coggan_level": "L2 Endurance", "expected_if": 0.68, "estimated_tss": 115,
        }
        if quality_saturday:
            saturday.update(coggan_level="L4 Threshold", expected_if=0.85, session_name="Threshold ride")
        sunday = {
            "day": "Sunday", "date": "2027-03-07", "discipline": "Run",
            "session_name": "Long Aerobic Run", "duration_min": 120, "priority": "key",
            "daniels_type": "E", "primary_metric": "HR",
        }
        return {"sessions": [saturday, sunday]}

    def test_deliberate_long_weekend_is_not_stacking(self) -> None:
        snapshot = {"planning_constraints": {}, "prior_plan_adherence": {}, "athlete_memory": {}}
        macro = {"week_classification": "build", "friel_cycle_type": "load", "macro_phase": "build"}
        result = validator_coach.run(snapshot, self._weekend(), GREEN, macro, {})
        self.assertFalse(any("stacked" in w for w in result["weaknesses"]))
        result = validator_coach.run(snapshot, self._weekend(quality_saturday=True), GREEN, macro, {})
        self.assertTrue(any("stacked" in w for w in result["weaknesses"]))
        yellow = {**GREEN, "readiness_status": "yellow"}
        result = validator_coach.run(snapshot, self._weekend(), yellow, macro, {})
        self.assertTrue(any("stacked" in w for w in result["weaknesses"]))

    def test_over_hours_is_info_unless_yellow(self) -> None:
        snapshot = {"planning_constraints": {"available_hours_override_min": 200}, "prior_plan_adherence": {}, "athlete_memory": {}}
        macro = {"week_classification": "build", "friel_cycle_type": "load"}
        # The available-hours override now binds as a cap, whatever readiness says.
        result = validator_coach.run(snapshot, self._weekend(), GREEN, macro, {})
        self.assertTrue(any("available-hours cap" in w for w in result["weaknesses"]))
        self.assertFalse(any("above the athlete" in s for s in result["strengths"]))
        # Typical profile hours stay a soft reference, but >15% above warns even when green.
        snapshot = {"planning_constraints": {}, "athlete_profile": {"typical_training_hours_per_week": 3.5},
                    "prior_plan_adherence": {}, "athlete_memory": {}}
        result = validator_coach.run(snapshot, self._weekend(), GREEN, macro, {})
        self.assertTrue(any("typical" in w for w in result["weaknesses"]))
        self.assertTrue(result["info"])
        result = validator_coach.run(snapshot, self._weekend(), {**GREEN, "readiness_status": "yellow"}, macro, {})
        self.assertTrue(any("not green" in w for w in result["weaknesses"]))
        self.assertIn(result["verdict"], validator_coach.VERDICTS)


class AuditFixTests(unittest.TestCase):
    def _codes(self, snapshot: dict, sessions: list, macro: dict | None = None) -> set:
        macro = macro or {"week_classification": "build", "friel_cycle_type": "load", "macro_phase": "build"}
        return {f["code"] for f in audit_plan(snapshot, GREEN, macro, {"sessions": sessions})["findings"]}

    @staticmethod
    def _run(day: str, minutes: int, target: str = "Z2 HR", name: str = "Run") -> dict:
        return {"day": day, "discipline": "Run", "session_name": f"{day} {name}", "duration_min": minutes,
                "blocks": [{"steps": [{"duration": f"{minutes}m", "target": target}]}]}

    def test_overload_side_checks(self) -> None:
        snapshot = {
            "weekly_windows": [{"week_start": f"w{i}", "minutes": 300} for i in range(4)],
            "training_load": {"run_load": {"longest_run_min_28d": 90}},
        }
        sessions = [self._run("Tuesday", 60), self._run("Thursday", 60), self._run("Saturday", 120), self._run("Sunday", 150)]
        codes = self._codes(snapshot, sessions)
        self.assertIn("weekly_volume_jump", codes)
        self.assertIn("long_run_jump", codes)

    def test_daniels_spacing_and_transition(self) -> None:
        t_run = {
            "day": "Friday", "discipline": "Run", "session_name": "Friday T", "duration_min": 70,
            "blocks": [{"repeat": 4, "steps": [{"duration": "10m", "target": "98-102% Pace"}]}],
            "progression": {"family": "run_threshold", "time_in_zone_min": 40},
        }
        long_ride = {"day": "Saturday", "discipline": "Ride", "session_name": "Long", "duration_min": 150,
                     "blocks": [{"steps": [{"duration": "150m", "target": "65-75% FTP"}]}]}
        codes = self._codes({}, [t_run, long_ride, self._run("Sunday", 100)])
        self.assertIn("daniels_cap_exceeded", codes)
        self.assertIn("quality_before_long_session", codes)
        transition = {"macro_phase": "transition", "week_classification": "recovery", "friel_cycle_type": "recovery"}
        self.assertIn("quality_in_transition", self._codes({}, [t_run], transition))

    def test_moderate_intensity_is_counted(self) -> None:
        ride = {"day": "Saturday", "discipline": "Ride", "session_name": "Long", "duration_min": 120,
                "blocks": [{"steps": [{"duration": "90m", "target": "65-75% FTP"}, {"duration": "30m", "target": "78-84% FTP"}]}]}
        result = audit_plan({}, GREEN, {"macro_phase": "build"}, {"sessions": [ride]})
        self.assertEqual(result["planned_moderate_pct"], 25.0)

    def test_race_week_checks(self) -> None:
        snapshot = {
            "races": [{**TRAIL_A_RACE, "date": "2027-05-08", "days_to_race": 5}],
            "weekly_windows": [{"week_start": f"w{i}", "minutes": 400} for i in range(4)],
        }
        sessions = [
            self._run("Friday", 100, name="long"),
            self._run("Saturday", 60, name="shakeout"),
            self._run("Tuesday", 300, name="volume"),
        ]
        macro = {"macro_phase": "taper", "friel_cycle_type": "taper"}
        codes = self._codes(snapshot, sessions, macro)
        self.assertIn("hard_session_before_race", codes)
        self.assertIn("session_on_race_day", codes)
        self.assertIn("taper_volume_not_reduced", codes)


class SupportAgentFixTests(unittest.TestCase):
    def test_carb_ladder(self) -> None:
        self.assertEqual(nutritionist.carb_range_g_per_h(45), (0, 0))
        self.assertEqual(nutritionist.carb_range_g_per_h(75), (30, 45))
        self.assertEqual(nutritionist.carb_range_g_per_h(120), (45, 60))
        self.assertEqual(nutritionist.carb_range_g_per_h(180), (60, 90))
        self.assertEqual(nutritionist.carb_range_g_per_h(75, hard=True), (45, 60))

    def test_long_trail_run_fueling_has_gut_training(self) -> None:
        snapshot = _snapshot(races=[_race(100)])
        macro, race, draft = _plan(snapshot)
        result = nutritionist.run(snapshot, draft, GREEN, macro, race)
        long_run = next(item for item in result["session_fueling"] if item["session_name"] == "Long Trail Run")
        self.assertIn("Gut training", long_run["guidance"]["during"])
        self.assertNotIn("single gel", long_run["guidance"]["during"])

    def test_race_director_trail_needs_and_bucket_aware_suggestions(self) -> None:
        snapshot = _snapshot(races=[_race(200)])
        macro = macro_coach.run(snapshot, GREEN)
        result = race_director.run(snapshot, macro)
        self.assertTrue(result["race_specific_needs"])
        self.assertIn("18 days", result["taper_notes"])
        text = str(result)
        self.assertNotIn("Coastal", text)
        fitness = [r for r in result["race_recommendations"] if r["type"] == "fitness_test"]
        for item in fitness:
            self.assertNotIn("Sprint", item["suggested_distance"])

    def test_philosophy_auditor_flags_mislabeled_if_and_heavy_recovery(self) -> None:
        snapshot = {"periodization_state": {"classified_weeks": [
            {"week_type": "load", "minutes": 300}, {"week_type": "load", "minutes": 300}
        ]}}
        macro = {"friel_block_week": 4, "friel_cycle_type": "recovery", "week_classification": "recovery"}
        draft = {"sessions": [{
            "day": "Saturday", "discipline": "Ride", "session_name": "Easy", "duration_min": 290,
            "coggan_level": "L2 Endurance", "expected_if": 0.86, "estimated_tss": 300,
        }]}
        result = training_philosophy_auditor.run(snapshot, macro, {}, {"alignment_status": "aligned"}, draft)
        self.assertEqual(result["coggan_alignment"], "adjust")
        self.assertEqual(result["friel_alignment"], "adjust")


if __name__ == "__main__":
    unittest.main()
