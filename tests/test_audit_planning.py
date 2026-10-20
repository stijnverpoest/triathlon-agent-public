"""Coaching-audit fixes for planning (progression, planner, audit, validator).

One test class per audit item; snapshots are built by hand from the fixtures
so the tests do not depend on the SIGNALS agent's new fields being produced.
"""

from __future__ import annotations

import copy
import unittest
from datetime import date

from triathlon_agent import coaching_limits
from triathlon_agent.agents import (
    head_coach_planner,
    macro_coach,
    phase_alignment_auditor,
    race_director,
    validator_coach,
)
from triathlon_agent.normalizer import normalize_snapshot
from triathlon_agent.plan_audit import audit_plan, intensity_minutes
from triathlon_agent.progression import (
    LADDERS,
    focus_families,
    gap_step_back,
    last_dose_by_family,
    ladder_dose,
    missed_session_rule,
    next_rung,
    phase_load_weeks,
    quality_history,
)
from triathlon_agent.race_selection import (
    distance_bucket,
    select_priority_race,
    trail_distance_unknown,
)
from triathlon_agent.threshold_testing import (
    RUN_RACE_SUBSTITUTES,
    _bike_threshold_state,
    _early_trigger,
    _eftp_interim_update,
    plan_threshold_tests,
)
from triathlon_agent.utils import read_json


GREEN = {
    "readiness_status": "green",
    "intensity_allowance": {"max_quality_sessions": 2},
    "underload": {"status": "not_detected"},
}
LOAD_MACRO = {"week_classification": "build", "friel_cycle_type": "load", "macro_phase": "build"}
SEASON = (
    ("Kalmthout Trail 21k", "C", "2026-12-13", "TrailRun 21k"),
    ("Trail 30k", "C", "2027-01-23", "TrailRun 30k"),
    ("Trail 35k", "B", "2027-03-20", "TrailRun 35k"),
    ("Hill Trail 50k", "A", "2027-05-08", "TrailRun 50k"),
)


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


BASE = _fixture_snapshot()


def season_snapshot(week: str, *, races: tuple = SEASON, **run_load: object) -> dict:
    """A sample athlete: 12 h/week, trail 50k A-race, ITB history, flat terrain."""
    start = date.fromisoformat(week)
    snapshot = copy.deepcopy(BASE)
    snapshot["week_start"] = week
    snapshot["planning_date"] = week
    snapshot["races"] = [
        {
            "name": name,
            "priority": priority,
            "date": day,
            "distance": distance,
            "days_to_race": (date.fromisoformat(day) - start).days,
        }
        for name, priority, day, distance in races
    ]
    snapshot["athlete_profile"]["injury_history"] = ["ITB syndrome"]
    snapshot["athlete_profile"]["typical_training_hours_per_week"] = 12
    snapshot["athlete_profile"]["weekly_sport_frequency"] = {"bike_sessions": 3, "run_sessions": 4}
    snapshot["planning_constraints"].update(
        time_caps_by_day={},
        strength_only_days=[],
        available_hours_override_min=None,
        double_session_capacity_by_day={
            day: True for day in ("Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
        },
    )
    snapshot["weekly_checkin"]["available_hours_override_min"] = None
    snapshot["periodization_state"] = {"next_block_week": 2, "recommended_next_week_type": "load"}
    snapshot["weekly_windows"] = [{"week_start": f"w{i}", "minutes": 680, "load": 500} for i in range(4)]
    snapshot["training_load"]["run_load"] = {
        "longest_run_min_28d": 105,
        "longest_run_min_56d": 110,
        "avg_weekly_run_min_4w": 240,
        "avg_weekly_run_km_4w": 38,
        "avg_weekly_elevation_gain_m_4w": 300,
        "avg_weekly_elevation_loss_m_4w": 280,
        "week_caps": {"max_run_min": 265, "max_run_km": 42, "max_descent_m": 320, "max_long_run_min": 120},
        **run_load,
    }
    snapshot["training_load"]["ride_load"] = {"longest_ride_min_28d": 120}
    return snapshot


def plan(snapshot: dict, performance: dict = GREEN) -> tuple[dict, dict, dict]:
    macro = macro_coach.run(snapshot, performance)
    race = race_director.run(snapshot, macro)
    draft = head_coach_planner.run(snapshot, performance, macro, race)
    return macro, race, draft


def codes(snapshot: dict, sessions: list, macro: dict = LOAD_MACRO, performance: dict = GREEN) -> set:
    return {f["code"] for f in audit_plan(snapshot, performance, macro, {"sessions": sessions})["findings"]}


def run_session(day: str, minutes: int, *, name: str = "Run", target: str = "Z2 HR", **extra) -> dict:
    return {
        "day": day, "discipline": "Run", "session_name": f"{day} {name}", "duration_min": minutes,
        "blocks": [{"steps": [{"duration": f"{minutes}m", "target": target}]}], **extra,
    }


def quality_run(day: str, family: str = "run_threshold", rung: int = 2) -> dict:
    dose = ladder_dose(family, rung)
    return {
        "day": day, "discipline": "Run", "session_name": f"{day} {family}", "duration_min": 60,
        "blocks": [{"repeat": dose["repeats"], "steps": [{"duration": f"{dose['rep_min']}m", "target": "98-102% Pace"}]}],
        "progression": dose,
    }


def _history_for(family: str, dose: dict, day: str, status: str = "completed", ratio: float | None = None) -> tuple:
    session = {"date": day, "discipline": "Run", "session_name": f"{family} {day}", "progression": dose,
               "blocks": []}
    review = {"session_name": session["session_name"], "date": day, "status": status}
    if ratio is not None:
        review["completion_ratio"] = ratio
    return {"final_plan": {"sessions": [session]}}, {"session_reviews": [review]}


# --- 1 / 18: progression ladders --------------------------------------------------


class LadderProgressionTests(unittest.TestCase):
    COMMON = {"phase": "build", "block_week": 1, "underload_status": "not_detected", "readiness_status": "green"}

    def test_ladder_totals_never_decrease(self) -> None:
        for family, ladder in LADDERS.items():
            totals = [reps * minutes for reps, minutes in ladder["rungs"]]
            self.assertEqual(totals, sorted(totals), family)

    def _simulate(self, family: str, *, with_rung: bool) -> int:
        contexts: list = []
        reviews: list = []
        last = None
        top = len(LADDERS[family]["rungs"]) - 1
        for week in range(len(LADDERS[family]["rungs"]) + 3):
            rung = next_rung(family, last, **self.COMMON)
            if rung == top:
                return week
            dose = ladder_dose(family, rung)
            if not with_rung:
                dose = {key: value for key, value in dose.items() if key != "rung"}
            day = f"2026-{1 + week // 4:02d}-{1 + (week % 4) * 7:02d}"
            context, review = _history_for(family, dose, day)
            contexts.append(context)
            reviews.append(review)
            last = last_dose_by_family(quality_history(contexts, reviews)).get(family)
        self.fail(f"{family} never reached its top rung")

    def test_every_ladder_reaches_the_top_by_rung_index(self) -> None:
        for family in LADDERS:
            self.assertLessEqual(self._simulate(family, with_rung=True), len(LADDERS[family]["rungs"]) + 1)

    def test_minutes_only_history_does_not_get_stuck_on_equal_totals(self) -> None:
        for family in ("bike_sweet_spot", "bike_threshold", "run_interval", "run_race_pace"):
            self._simulate(family, with_rung=False)

    def test_proportional_detraining_step_back(self) -> None:
        self.assertEqual([gap_step_back(days) for days in (10, 15, 30, 45, 60)], [0, 1, 2, 3, None])
        last = {"minutes": 50.0, "rung": 6, "completed": True, "date": "2026-01-01"}
        self.assertEqual(next_rung("run_threshold", last, planning_date="2026-02-15", **self.COMMON), 3)
        # > 56 days: restart at the phase entry rung (build = 2).
        self.assertEqual(next_rung("run_threshold", last, planning_date="2026-03-15", **self.COMMON), 2)


# --- 6: partial sessions -----------------------------------------------------------------


class PartialSessionTests(unittest.TestCase):
    def test_partial_session_repeats_the_rung(self) -> None:
        dose = ladder_dose("run_threshold", 3)
        context, review = _history_for("run_threshold", dose, "2026-09-01", "shortened", 0.6)
        last = last_dose_by_family(quality_history([context], [review]))["run_threshold"]
        self.assertFalse(last["completed"])
        self.assertEqual(next_rung("run_threshold", last, **LadderProgressionTests.COMMON), 3)

    def test_nearly_complete_partial_counts_as_done(self) -> None:
        dose = ladder_dose("run_threshold", 3)
        context, review = _history_for("run_threshold", dose, "2026-09-01", "partial", 0.95)
        last = last_dose_by_family(quality_history([context], [review]))["run_threshold"]
        self.assertTrue(last["completed"])
        self.assertEqual(next_rung("run_threshold", last, **LadderProgressionTests.COMMON), 4)


# --- 2: stale-FTP double count ----------------------------------------------------------


class IntensityCountTests(unittest.TestCase):
    def test_progression_session_counted_once_via_family(self) -> None:
        session = {
            "discipline": "Ride", "duration_min": 100,
            "progression": {"family": "bike_sweet_spot", "time_in_zone_min": 40},
            "blocks": [{"repeat": 2, "steps": [{"duration": "20m", "target": "75-79% FTP"}]}],
        }
        self.assertEqual(intensity_minutes(session), (40.0, 0.0))

    def test_ftp_scale_applies_when_parsing_blocks(self) -> None:
        session = {
            "discipline": "Ride", "session_name": "SS", "duration_min": 100,
            "blocks": [{"repeat": 2, "steps": [{"duration": "20m", "target": "75-79% FTP"}]}],
        }
        # Unscaled 75-79% would be tempo; on a x0.85 stale FTP it is sweet spot (88-93%).
        self.assertEqual(intensity_minutes(session, 0.85), (40.0, 0.0))
        snapshot = {"threshold_testing": {"bike": {"stale": True, "ftp_target_scale": 0.85}, "proposed_updates": []}}
        result = audit_plan(snapshot, GREEN, LOAD_MACRO, {"sessions": [session]})
        self.assertEqual(result["planned_moderate_pct"], 40.0)
        self.assertEqual(result["planned_high_pct"], 0.0)


# --- 3 / 4: long-session fallback ----------------------------------------------------------


class LongRunFallbackTests(unittest.TestCase):
    def _long_run(self, snapshot: dict) -> int:
        return next(s["duration_min"] for s in plan(snapshot)[2]["sessions"] if s["session_name"].startswith("Long"))

    def test_fallbacks_never_use_a_template_value(self) -> None:
        caps = season_snapshot("2026-10-12", longest_run_min_28d=None, week_caps={"max_long_run_min": 60})
        self.assertLessEqual(self._long_run(caps), 60)
        older = season_snapshot("2026-10-12", longest_run_min_28d=None, week_caps={}, longest_run_min_56d=120)
        self.assertEqual(self._long_run(older), 85)  # 0.7 x 120
        nothing = season_snapshot("2026-10-12", longest_run_min_28d=None, week_caps={}, longest_run_min_56d=None)
        self.assertEqual(self._long_run(nothing), coaching_limits.LONG_RUN_FALLBACK_MIN)

    def test_race_counted_as_long_run_is_ignored(self) -> None:
        snapshot = season_snapshot(
            "2026-10-12", longest_run_min_28d=180, longest_race_min_28d=180, longest_run_min_56d=180, week_caps={}
        )
        self.assertEqual(self._long_run(snapshot), coaching_limits.LONG_RUN_FALLBACK_MIN)

    def test_audit_checks_long_run_without_recent_history(self) -> None:
        snapshot = {"training_load": {"run_load": {"longest_run_min_56d": 100}}}
        self.assertIn("long_run_jump", codes(snapshot, [run_session("Sunday", 150)]))
        self.assertNotIn("long_run_jump", codes(snapshot, [run_session("Sunday", 70)]))


# --- 5: sawtooth support doses -------------------------------------------------------------


class SupportDoseTests(unittest.TestCase):
    def test_load_weeks_in_phase_continue_across_blocks(self) -> None:
        snapshot = {"week_start": "2027-03-01", "athlete_state": {"history": [
            {"week_start": "2027-02-01", "phase": "build", "classification": "build"},
            {"week_start": "2027-02-08", "phase": "build", "classification": "build"},
            {"week_start": "2027-02-15", "phase": "build", "classification": "recovery"},
            {"week_start": "2027-02-22", "phase": "build", "classification": "build"},
        ]}}
        self.assertEqual(phase_load_weeks(snapshot, "build", block_week=1), 3)
        snapshot["training_load"] = {"return_from_break": {"status": "returning"}}
        self.assertEqual(phase_load_weeks(snapshot, "build", block_week=1), 1)

    def test_brick_builds_on_completed_load_weeks(self) -> None:
        snapshot = season_snapshot("2027-03-01", races=(("Half", "A", "2027-06-20", "Ironman 70.3"),))
        snapshot["periodization_state"] = {"next_block_week": 1, "recommended_next_week_type": "load"}
        brick = lambda draft: next(s["duration_min"] for s in draft["sessions"] if s["session_name"].startswith("Brick"))  # noqa: E731
        fresh = brick(plan(snapshot)[2])
        snapshot["athlete_state"] = {"history": [
            {"week_start": f"2027-02-{day:02d}", "phase": "build", "classification": "build"} for day in (1, 8, 15)
        ]}
        self.assertGreater(brick(plan(snapshot)[2]), fresh)


# --- 7: race director window --------------------------------------------------------------


class RaceDirectorTests(unittest.TestCase):
    def test_tune_up_window_is_relative_to_week_start(self) -> None:
        snapshot = {"week_start": "2027-01-04", "planning_date": "2026-12-30",
                    "races": [{"name": "A", "priority": "A", "days_to_race": 100, "distance": "Marathon"}]}
        result = race_director.run(snapshot, {"macro_phase": "base"})
        tune_up = next(r for r in result["race_recommendations"] if r["type"] == "tune_up")
        self.assertTrue(tune_up["ideal_window"].startswith("Mar 10"))  # 2027-01-04 + 65 days

    def test_b_c_race_handling_and_trail_without_distance(self) -> None:
        snapshot = season_snapshot("2027-02-01")
        result = race_director.run(snapshot, macro_coach.run(snapshot, GREEN))
        b_race = next(item for item in result["b_c_race_handling"] if item["priority"] == "B")
        self.assertIn("Mini-taper", b_race["rule"])
        self.assertGreaterEqual(b_race["easy_days_after"], 3)
        self.assertEqual(distance_bucket("Trail des Fagnes"), "trail")
        unknown = {"races": [{"name": "Trail des Fagnes", "priority": "A", "days_to_race": 90, "distance": "TrailRun"}]}
        self.assertTrue(trail_distance_unknown(unknown["races"][0]))
        info = race_director.run(unknown, {"macro_phase": "build"})["info"]
        self.assertTrue(any("no distance" in item for item in info))


# --- 8 / 13 / 22: threshold testing -------------------------------------------------------


class ThresholdTests(unittest.TestCase):
    def test_drift_rule_is_consistent_and_missing_dates_are_tolerated(self) -> None:
        settings = {"Ride": {"id": 1, "ftp": 300}}
        activities = [{"type": "Ride", "start_date_local": "2026-10-01", "icu_rolling_ftp": 285}]  # exactly -5%
        self.assertIsNone(_early_trigger("bike", settings, activities, [{"kind": "sick", "days_since_end": 20}]))
        self.assertFalse(_bike_threshold_state(settings, activities)["stale"])

    def test_ftp_far_above_eftp_proposes_apply_true(self) -> None:
        settings = {"Ride": {"id": 1, "ftp": 300}}
        activities = [{"type": "Ride", "start_date_local": "2026-10-01", "icu_rolling_ftp": 260}]
        bike = _bike_threshold_state(settings, activities)
        update = _eftp_interim_update(bike, settings, [], date(2026, 10, 12))
        self.assertTrue(update["apply"])
        self.assertIn("approve", update["apply_reason"])

    def test_ftp_too_low_scales_targets_up(self) -> None:
        settings = {"Ride": {"id": 1, "ftp": 250}}
        activities = [{"type": "Ride", "start_date_local": "2026-10-01", "icu_rolling_ftp": 280}]
        bike = _bike_threshold_state(settings, activities)
        update = _eftp_interim_update(bike, settings, [], date(2026, 10, 12))
        self.assertFalse(update["apply"])
        snapshot = {"threshold_testing": {"bike": bike, "proposed_updates": [update]}}
        self.assertEqual(head_coach_planner.ftp_target_scale(snapshot), 1.08)

    def _testing(self, bike: dict, run: dict) -> dict:
        base = {"protocol": "coggan_20", "blockers": [], "reason": "r"}
        return {"bike": {**base, **bike}, "run": {**base, "protocol": "friel_30_run", **run}}

    def test_tests_only_when_due_and_relevant(self) -> None:
        due = {"status": "due", "urgency": "due"}
        testing = self._testing(due, due)
        trail = plan_threshold_tests(testing, recovery_week=True, readiness_status="green", priority_bucket="ultra_trail")
        self.assertEqual([item["discipline"] for item in trail["scheduled"]], ["run"])
        stale = self._testing({**due, "stale": True}, {"status": "not_due"})
        trail = plan_threshold_tests(stale, recovery_week=True, readiness_status="green", priority_bucket="ultra_trail")
        self.assertEqual([item["discipline"] for item in trail["scheduled"]], ["bike"])
        none = plan_threshold_tests(
            stale, recovery_week=True, readiness_status="green", priority_bucket="ultra_trail",
            bike_targets_prescribed=False,
        )
        self.assertEqual(none["scheduled"], [])
        rfb = plan_threshold_tests(testing, recovery_week=True, readiness_status="green", return_from_break=True)
        self.assertEqual(rfb["scheduled"], [])
        routine = self._testing({"status": "not_due", "days_since_last_test": 30}, {"status": "not_due"})
        self.assertEqual(plan_threshold_tests(routine, recovery_week=True, readiness_status="green")["scheduled"], [])
        self.assertNotIn("5k", RUN_RACE_SUBSTITUTES)
        self.assertIn("marathon", RUN_RACE_SUBSTITUTES)


# --- 9 + calendar-only races -----------------------------------------------------------------


class CalendarOnlyRaceTests(unittest.TestCase):
    def test_profile_target_is_never_a_race_source(self) -> None:
        snapshot = {"athlete_profile": {"target_race_distance": "Ironman 70.3"}, "races": []}
        self.assertIsNone(select_priority_race(snapshot))
        snapshot["races"] = [
            {"name": "Local 70.3", "priority": "B", "days_to_race": 20, "distance": "70.3"},
            {"name": "Trail", "priority": "B", "days_to_race": 10, "distance": "Trail 30k"},
        ]
        self.assertEqual(select_priority_race(snapshot)["name"], "Trail")

    def test_no_calendar_race_plans_a_general_base_week(self) -> None:
        snapshot = season_snapshot("2026-10-12", races=())
        snapshot["athlete_profile"]["target_race_distance"] = "Ultra Trail 50k"
        macro, race, draft = plan(snapshot)
        self.assertEqual(macro["macro_phase"], "base")
        self.assertEqual(draft["week_context"]["race_bucket"], "unknown")
        self.assertTrue(any("No race on your Intervals calendar" in note for note in draft["planner_notes"]))
        self.assertTrue(any("not on your Intervals calendar" in item for item in race["info"]))

    def test_stored_phase_and_race_lose_to_the_calendar(self) -> None:
        snapshot = season_snapshot("2026-10-12")
        snapshot["athlete_state"] = {"active_macro_cycle": {
            "phase": "race_specific", "priority_race": {"name": "Old 70.3", "days_to_race": 20}}}
        macro = macro_coach.run(snapshot, GREEN)
        self.assertEqual(macro["macro_phase"], "base")
        self.assertIn("calendar wins", macro["rationale"])


# --- 10 / 15 / 21 / 23: planner shape --------------------------------------------------------


class TrailPlannerTests(unittest.TestCase):
    def test_back_to_back_weekend_is_gradual_with_one_run_quality(self) -> None:
        _, _, draft = plan(season_snapshot("2027-01-04"))
        sessions = draft["sessions"]
        medium = next(s for s in sessions if s["session_name"].startswith("Medium-Long"))
        self.assertLessEqual(medium["duration_min"], 40)
        run_quality = [s for s in sessions if s.get("progression") and s["discipline"] == "Run"]
        self.assertEqual(len(run_quality), 1)
        self.assertTrue(any(s["session_name"] == "Easy Aerobic Run" for s in sessions))
        run_min = sum(s["duration_min"] for s in sessions if s["discipline"] == "Run")
        self.assertLessEqual(run_min, 265)

    def test_progression_budget_one_impact_element(self) -> None:
        _, _, draft = plan(season_snapshot("2027-01-04"))
        budget = draft["progression_budget"]
        self.assertLessEqual(len(budget["progressing"]), coaching_limits.PROGRESSION_BUDGET_MAX)
        impact = {"long run duration", "medium-long run", "descent"}
        self.assertLessEqual(len([item for item in budget["progressing"] if item in impact]), 1)
        self.assertTrue(budget["held"])

    def test_terrain_and_descent_fields(self) -> None:
        _, _, draft = plan(season_snapshot("2027-03-08"))
        runs = [s for s in draft["sessions"] if s["discipline"] == "Run" and not s.get("race_day")]
        self.assertTrue(all("terrain" in s and "elevation_gain_m" in s for s in runs))
        self.assertLessEqual(sum(s["elevation_loss_m"] for s in runs), 320)
        self.assertLessEqual(sum(s["elevation_gain_m"] for s in runs), draft["week_context"]["weekly_elevation_target_m"])
        uphill = [s for s in runs if s.get("progression", {}).get("family") == "run_uphill_threshold"]
        self.assertTrue(all(s["terrain"] == "treadmill_incline" and s["elevation_loss_m"] == 0 for s in uphill))

    def test_every_session_has_feel_cues(self) -> None:
        _, _, draft = plan(season_snapshot("2027-01-04"))
        for session in draft["sessions"]:
            for key in ("rpe_target", "feel_cue", "bail_out_rule"):
                self.assertTrue(session.get(key), (session["session_name"], key))
        long_run = next(s for s in draft["sessions"] if s["session_name"].startswith("Long Trail Run"))
        self.assertIn("walk downhill", long_run["bail_out_rule"])

    def test_heavy_strength_never_on_the_eve_of_key_work(self) -> None:
        _, _, draft = plan(season_snapshot("2026-10-12"))
        result = audit_plan(season_snapshot("2026-10-12"), GREEN, macro_coach.run(season_snapshot("2026-10-12"), GREEN), draft)
        self.assertNotIn("heavy_strength_before_key", {f["code"] for f in result["findings"]})

    def test_realistic_season_weeks_pass_the_audit_and_validator(self) -> None:
        for week in ("2026-10-12", "2026-12-07", "2027-01-04", "2027-03-15", "2027-03-22", "2027-04-26", "2027-05-03"):
            snapshot = season_snapshot(week)
            macro, race, draft = plan(snapshot)
            audit = audit_plan(snapshot, GREEN, macro, draft)
            self.assertEqual(audit["findings"], [], week)
            self.assertEqual(validator_coach.run(snapshot, draft, GREEN, macro, race)["verdict"], "keep", week)


# --- 11: run-specific audits -------------------------------------------------------------


class RunAuditTests(unittest.TestCase):
    def test_run_volume_descent_and_elevation(self) -> None:
        snapshot = season_snapshot("2027-01-04")
        sessions = [
            run_session("Tuesday", 90), run_session("Thursday", 90),
            run_session("Sunday", 110, elevation_gain_m=600, elevation_loss_m=600),
        ]
        found = codes(snapshot, sessions)
        self.assertIn("run_volume_jump", found)
        self.assertIn("descent_over_cap", found)
        self.assertIn("elevation_jump", found)

    def test_heavy_strength_and_week_boundary(self) -> None:
        strength = {"day": "Friday", "discipline": "WeightTraining", "session_name": "Strength (Heavy)", "duration_min": 40}
        self.assertIn("heavy_strength_before_key", codes({}, [strength, run_session("Saturday", 120)]))
        snapshot = {
            "week_start": "2027-01-04",
            "prior_plan_history": [{"final_plan": {"sessions": [
                {"date": "2027-01-03", "discipline": "Run", "session_name": "Long Run", "duration_min": 130}
            ]}}],
        }
        monday = {**quality_run("Monday"), "date": "2027-01-04"}
        self.assertIn("hard_across_week_boundary", codes(snapshot, [monday]))

    def test_b_and_c_race_rules(self) -> None:
        b_week = {"week_start": "2027-03-15", "races": [
            {"name": "Trail 35k", "priority": "B", "date": "2027-03-20", "days_to_race": 5, "distance": "Trail 35k"}]}
        self.assertIn("quality_before_b_race", codes(b_week, [quality_run("Thursday")]))
        self.assertNotIn("quality_before_b_race", codes(b_week, [quality_run("Tuesday")]))
        c_week = {"week_start": "2026-12-07", "races": [
            {"name": "21k", "priority": "C", "date": "2026-12-13", "days_to_race": 6, "distance": "Trail 21k"}]}
        self.assertIn("hard_session_before_race", codes(c_week, [quality_run("Saturday")]))
        after = {"week_start": "2027-03-22", "races": [
            {"name": "Trail 35k", "priority": "B", "date": "2027-03-20", "days_to_race": -2, "distance": "Trail 35k"}]}
        self.assertIn("hard_after_race", codes(after, [quality_run("Tuesday")]))

    def test_intensity_ceiling_and_reentry_caps(self) -> None:
        hard = [quality_run("Tuesday", rung=6), quality_run("Thursday", rung=6), run_session("Saturday", 60)]
        self.assertIn("intensity_too_high", codes({}, hard))
        snapshot = {"training_load": {"return_from_break": {
            "status": "returning", "reentry_week": 1, "volume_cap_pct": 50, "pre_break_weekly_min": 400,
            "run_km_cap": 10, "long_run_cap_min": 50}}}
        found = codes(snapshot, [quality_run("Tuesday"), quality_run("Thursday"), run_session("Sunday", 120)])
        self.assertTrue({"reentry_volume_over_cap", "reentry_run_over_cap", "reentry_long_run_over_cap", "reentry_quality"} <= found)

    def test_progression_budget_audit(self) -> None:
        snapshot = {
            "progression": {"last_dose_by_family": {
                "run_threshold": {"minutes": 25, "rung": 1, "completed": True},
                "run_interval": {"minutes": 15, "rung": 0, "completed": True},
            }},
            "training_load": {"run_load": {"longest_run_min_28d": 100, "avg_weekly_run_min_4w": 400}},
        }
        sessions = [quality_run("Tuesday", rung=2), quality_run("Thursday", "run_interval", 1), run_session("Sunday", 112)]
        self.assertIn("progression_budget_exceeded", codes(snapshot, sessions))
        underload = {**GREEN, "underload": {"status": "detected"}}
        self.assertNotIn("progression_budget_exceeded", codes(snapshot, sessions, performance=underload))


# --- 12 / 14: shared limits and binding hours ------------------------------------------------


class SharedLimitTests(unittest.TestCase):
    def test_audit_and_validator_agree_on_growth(self) -> None:
        snapshot = {"weekly_windows": [{"week_start": f"w{i}", "minutes": 400} for i in range(4)],
                    "planning_constraints": {}}
        sessions = [run_session("Saturday", 240), {**run_session("Sunday", 230), "date": "2027-01-10"}]
        for session in sessions:
            session.setdefault("date", "2027-01-09")
        self.assertIn("weekly_volume_jump", codes(snapshot, sessions))  # 470' = +17.5%
        result = validator_coach.run(snapshot, {"sessions": sessions}, GREEN, LOAD_MACRO, {})
        self.assertTrue(any("grow by at most ~15%" in w for w in result["weaknesses"]))
        self.assertIs(validator_coach.LONG_SESSION_MIN, coaching_limits.LONG_SESSION_MIN)
        self.assertIs(head_coach_planner.TAPER_VOLUME_BY_WEEKS_OUT, coaching_limits.TAPER_VOLUME_BY_WEEKS_OUT)

    def test_hard_cap_binds_and_says_what_was_cut(self) -> None:
        snapshot = season_snapshot("2027-01-04")
        snapshot["weekly_checkin"]["available_hours_hard_cap_min"] = 360
        _, race, draft = plan(snapshot)
        budget = draft["weekly_time_budget"]
        self.assertEqual(budget["mode"], "hard_cap")
        self.assertLessEqual(budget["planned_min"], 360)
        self.assertTrue(budget["cuts"])
        self.assertTrue(any(s["session_name"].startswith("Long Trail Run") for s in draft["sessions"]))

    def test_validator_warns_above_typical_hours_even_when_green(self) -> None:
        snapshot = {"athlete_profile": {"typical_training_hours_per_week": 4}, "planning_constraints": {}}
        sessions = [{**run_session("Saturday", 150), "date": "2027-01-09"},
                    {**run_session("Sunday", 150), "date": "2027-01-10"}]
        result = validator_coach.run(snapshot, {"sessions": sessions}, GREEN, LOAD_MACRO, {})
        self.assertTrue(any("typical" in w for w in result["weaknesses"]))


# --- 16 / 17 / 19 / 20: periodization and adaptation ---------------------------------------


class PeriodizationTests(unittest.TestCase):
    def test_b_race_week_gets_a_mini_taper(self) -> None:
        _, _, draft = plan(season_snapshot("2027-03-15"))
        quality_days = {s["day"] for s in draft["sessions"] if s.get("progression")}
        self.assertFalse(quality_days & {"Wednesday", "Thursday", "Friday"})
        self.assertEqual(coaching_limits.post_race_easy_days("B", 300), 4)
        self.assertEqual(coaching_limits.post_race_easy_days("C", 60), 1)

    def test_focus_changes_by_phase_with_build_i_block(self) -> None:
        self.assertEqual(focus_families("ultra_trail", "base")[0], "run_threshold")
        self.assertEqual(focus_families("ultra_trail", "build", i_block=True)[0], "run_interval")
        self.assertEqual(focus_families("ultra_trail", "build")[0], "run_uphill_threshold")
        self.assertEqual(focus_families("ultra_trail", "race_specific")[0], "run_uphill_threshold")

    def test_two_to_one_rhythm_after_a_break_and_season_checkpoints(self) -> None:
        snapshot = season_snapshot("2026-10-12")
        snapshot["periodization_state"] = {"next_block_week": 3, "recommended_next_week_type": "load"}
        snapshot["training_load"]["return_from_break"] = {"status": "returning", "reentry_week": 3}
        macro = macro_coach.run(snapshot, GREEN)
        self.assertEqual(macro["friel_cycle_type"], "recovery")
        self.assertEqual(macro["block_rhythm"], "2:1")
        winter = macro_coach.run(season_snapshot("2026-12-07"), GREEN)
        self.assertEqual(winter["macro_phase"], "build")  # B race 35k is a checkpoint in its build window
        self.assertTrue(any(c["priority"] == "B" for c in winter["season_checkpoints"]))

    def test_b_race_quality_flagged_by_phase_alignment(self) -> None:
        macro = {"macro_phase": "race_specific", "season_checkpoints": [
            {"name": "Trail 35k", "priority": "B", "days_to_race": 5}]}
        draft = {"sessions": [quality_run("Thursday")]}
        result = phase_alignment_auditor.run({}, macro, {"priority_race": {"days_to_race": 54}}, draft)
        self.assertTrue(any("mini-taper" in item for item in result["required_adjustments"]))

    def test_holiday_is_maintenance_not_recovery(self) -> None:
        snapshot = season_snapshot("2026-07-13")
        snapshot["holidays"] = [{"name": "Summer", "start_date": "2026-07-06", "end_date": "2026-07-31",
                                 "days_overlap_planning_week": 7}]
        snapshot["weekly_checkin"]["athlete_notes"] = "Two weeks in the Alps."
        macro, _, draft = plan(snapshot)
        self.assertEqual(macro["friel_cycle_type"], "maintenance")
        self.assertEqual(draft["quality_session_count"], 0)
        workouts = [s for s in draft["sessions"] if s["discipline"] in {"Run", "Ride"}]
        self.assertTrue(all(s["priority"] == "optional" for s in workouts))
        self.assertTrue(any("trail-camp" in s["session_name"] for s in workouts))
        strength = [s for s in draft["sessions"] if s["discipline"] == "WeightTraining"]
        self.assertTrue(all(s["duration_min"] <= head_coach_planner.LIGHT_STRENGTH_MAX_MIN for s in strength))

    def test_missed_sessions_and_pain(self) -> None:
        self.assertEqual(missed_session_rule(1, 4)["action"], "move")
        self.assertEqual(missed_session_rule(3, 5)["action"], "drop_and_repeat")
        snapshot = season_snapshot("2027-01-04")
        snapshot["prior_plan_adherence"] = {"status": "available", "adherence_score": 0.4, "summary": "s"}
        snapshot["weekly_checkin"]["missed_session_reasons"] = {"Thursday T": "sick with flu"}
        self.assertNotEqual(macro_coach.run(snapshot, GREEN)["week_classification"], "absorb")
        snapshot["weekly_checkin"]["pain_scores"] = {"knee": 4}
        macro, _, draft = plan(snapshot)
        runs = [s for s in draft["sessions"] if s["discipline"] == "Run"]
        self.assertEqual(runs, [])
        self.assertTrue(draft["missed_session_policy"]["last_week_health_misses"])
        pain = codes(snapshot, [run_session("Tuesday", 40, elevation_loss_m=100)], macro)
        self.assertTrue({"running_with_pain", "descent_with_pain"} <= pain)


if __name__ == "__main__":
    unittest.main()
