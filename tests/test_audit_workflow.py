from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from triathlon_agent.agents import nutritionist, orchestrator, performance_doctor, syntax_guard
from triathlon_agent.athlete_state import (
    AthleteStateError,
    backup_path,
    bootstrap_athlete_state,
    capture_race_results,
    load_athlete_state,
    log_injuries,
    record_week_actuals,
    resolve_open_decisions,
    store_open_decisions,
    update_athlete_state_from_plan,
    write_athlete_state,
)
from triathlon_agent.checkin import build_post_fetch_checkin, merge_checkin_answers
from triathlon_agent.cli import main
from triathlon_agent.compiler import compile_session_to_event
from triathlon_agent.config import load_config
from triathlon_agent.contracts import (
    AGENT_REQUIREMENTS,
    load_schema,
    validate_agent_output,
    validate_final_plan,
)
from triathlon_agent.memory import build_athlete_memory, build_response_profile, rpe_compliance
from triathlon_agent.planning_state import load_planning_state
from triathlon_agent.race_selection import select_priority_race
from triathlon_agent.report import render_weekly_report
from triathlon_agent.utils import (
    detect_red_flags,
    has_niggle,
    is_partial_week,
    read_json,
    write_json,
)
from triathlon_agent.workflow import (
    ApprovalMismatchError,
    MissingCheckinError,
    _load_prior_plan_contexts,
    _require_recorded_checkin,
    adjust_week,
    approve_run,
    finalize_run,
    plan_week,
    prepare_run,
    record_checkin,
)

PROFILE = "fixtures/athlete_profile.json"
CHECKIN = "fixtures/weekly_checkin.json"


class IsolatedStateCase(unittest.TestCase):
    """Every test gets its own athlete_state dir and temp folder."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.temp = Path(self._temp.name)
        self.state_dir = self.temp / "state"
        self.state_dir.mkdir()
        patcher = mock.patch.dict(
            os.environ, {"TRIATHLON_AGENT_STATE_DIR": str(self.state_dir)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temp.cleanup)
        self.runs = self.temp / "runs"
        self.runs.mkdir()
        self.config = replace(load_config(mode="mock"), runs_dir=self.runs)

    def write_checkin(self, **overrides: object) -> str:
        payload = read_json(CHECKIN)
        payload.update(overrides)
        path = self.temp / f"checkin-{len(list(self.temp.glob('checkin-*')))}.json"
        write_json(path, payload)
        return str(path)


def _doctor_snapshot(**weekly: object) -> dict:
    checkin = {
        "pain_or_niggles": "",
        "missed_sessions": [],
        "unexpected_fatigue": "",
        "subjective_readiness": 8,
        "current_motivation": 8,
    }
    checkin.update(weekly)
    return {
        "athlete_profile": {"name": "Test Athlete"},
        "weekly_checkin": checkin,
        "planning_constraints": {},
        "prior_plan_adherence": {"status": "missing"},
        "athlete_memory": {"status": "missing"},
        "latest_wellness": {"fatigue": 2, "soreness": 1},
        "load_change_pct": 2.0,
        "recent_week_load": 400,
        "prior_week_load": 395,
        "avg_sleep_hours": 7.8,
        "avg_hrv": 68.0,
        "avg_resting_hr": 44.0,
        "weekly_windows": [{"activities": []}],
        "derived_baselines": {
            "hrv": {"baseline": 66.0, "acute": 68.0, "delta": 2.0, "delta_pct": 3.0, "confidence": "high"},
            "resting_hr": {"baseline": 44.0, "acute": 44.0, "delta": 0.0, "delta_pct": 0.0, "confidence": "high"},
            "sleep_hours": {"baseline": 7.6, "acute": 7.8, "delta": 0.2, "delta_pct": 2.6, "confidence": "high"},
        },
        "training_load": {},
    }


# 1 -------------------------------------------------------------------------
class RecordCheckinTests(IsolatedStateCase):
    def test_prepare_does_not_mark_checkin_and_record_checkin_renormalizes(self) -> None:
        run_dir = self.runs / "run"
        prepare_run(self.config, PROFILE, CHECKIN, output_dir=str(run_dir))
        self.assertNotIn(
            "post_fetch_checkin", load_planning_state(run_dir)["completed_steps"]
        )
        result = record_checkin(
            str(run_dir),
            {"subjective_readiness": 3, "pain_scores": {"calf": 4}, "sleep_quality": 2},
        )
        snapshot = read_json(run_dir / "normalized_snapshot.json")
        self.assertEqual(snapshot["weekly_checkin"]["subjective_readiness"], 3)
        self.assertEqual(read_json(run_dir / "weekly_checkin.json")["pain_scores"], {"calf": 4})
        inputs = read_json(run_dir / "inputs.json")
        self.assertEqual(inputs["local_inputs"]["weekly_checkin"]["sleep_quality"], 2)
        self.assertIn("post_fetch_checkin", load_planning_state(run_dir)["completed_steps"])
        self.assertIn("subjective_readiness", result["answered_fields"])

    def test_invalid_answers_and_planning_date_change_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            merge_checkin_answers({"planning_date": "2026-03-08"}, {"sleep_quality": 9})
        with self.assertRaises(ValueError):
            merge_checkin_answers({"planning_date": "2026-03-08"}, {"pain_scores": {"knee": 12}})
        with self.assertRaises(ValueError):
            merge_checkin_answers({"planning_date": "2026-03-08"}, {"planning_date": "2026-03-01"})

    def test_live_finalize_refuses_without_recorded_checkin(self) -> None:
        run_dir = self.runs / "run"
        prepare_run(self.config, PROFILE, CHECKIN, output_dir=str(run_dir))
        state = read_json(run_dir / "planning_state.json")
        state["workflow_mode"] = "live"
        write_json(run_dir / "planning_state.json", state)
        with self.assertRaises(MissingCheckinError):
            finalize_run(str(run_dir))
        _require_recorded_checkin(run_dir, allow_missing_checkin=True)
        state = load_planning_state(run_dir)
        self.assertIn("post_fetch_checkin", state["completed_steps"])
        self.assertIn("finalized_without_recorded_checkin", state["override_flags"])

    def test_cli_record_checkin(self) -> None:
        run_dir = self.runs / "run"
        prepare_run(self.config, PROFILE, CHECKIN, output_dir=str(run_dir))
        answers = self.temp / "answers.json"
        write_json(answers, {"work_stress": 7, "red_flags": []})
        self.assertEqual(main(["record-checkin", str(run_dir), str(answers)]), 0)
        write_json(answers, {"work_stress": 70})
        self.assertEqual(main(["record-checkin", str(run_dir), str(answers)]), 2)

    def test_snapshot_state_never_carries_a_stored_priority_race(self) -> None:
        write_json(
            self.state_dir / "athlete_state.json",
            {
                "priority_race": {"name": "Stale Top Level", "date": "2026-10-18"},
                "active_macro_cycle": {
                    "phase": "race_specific",
                    "priority_race": {"name": "City Half", "date": "2026-10-18"},
                },
            },
        )
        run_dir = self.runs / "run"
        prepare_run(self.config, PROFILE, CHECKIN, output_dir=str(run_dir))
        record_checkin(str(run_dir), {"work_stress": 4})
        state = read_json(run_dir / "normalized_snapshot.json")["athlete_state"]
        self.assertNotIn("priority_race", state)
        self.assertNotIn("priority_race", state["active_macro_cycle"])
        self.assertEqual(state["last_planned_priority_race"]["name"], "City Half")

    def test_next_week_review_reads_the_merged_checkin(self) -> None:
        old = self.runs / "old"
        old.mkdir()
        write_json(old / "approval.json", {"status": "approved", "pushed_at": "2026-03-01T08:00:00"})
        write_json(
            old / "inputs.json",
            {"local_inputs": {"athlete_profile": read_json(PROFILE), "weekly_checkin": {"pain_or_niggles": None}}},
        )
        write_json(old / "weekly_checkin.json", {"pain_or_niggles": "calf tight"})
        write_json(
            old / "final_plan.json",
            {"sessions": [{"date": "2026-03-07", "day": "Saturday", "discipline": "Run", "session_name": "Long Run", "duration_min": 90}]},
        )
        contexts = _load_prior_plan_contexts(self.runs, read_json(PROFILE), "2026-03-08", self.runs / "new")
        self.assertEqual(contexts[0]["weekly_checkin"]["pain_or_niggles"], "calf tight")


# 2 -------------------------------------------------------------------------
class ApprovalHashTests(IsolatedStateCase):
    def test_changed_plan_cannot_be_pushed_and_refinalize_resets(self) -> None:
        run_dir = self.runs / "run"
        plan_week(self.config, PROFILE, CHECKIN, output_dir=str(run_dir), auto_approve=True, dry_run=True)
        approval = read_json(run_dir / "approval.json")
        self.assertEqual(approval["status"], "approved")
        self.assertTrue(approval["final_plan_sha256"])
        # Re-finalizing an unchanged plan keeps the approval.
        finalize_run(str(run_dir))
        self.assertEqual(read_json(run_dir / "approval.json")["status"], "approved")

        plan = read_json(run_dir / "final_plan.json")
        plan["sessions"][1]["duration_min"] += 15
        write_json(run_dir / "final_plan.json", plan)
        with self.assertRaises(ApprovalMismatchError):
            approve_run(self.config, str(run_dir), dry_run=True)

        finalize_run(str(run_dir))
        approval = read_json(run_dir / "approval.json")
        self.assertEqual(approval["status"], "pending")
        self.assertIn("changed", approval["reset_reason"])

    def test_manual_approval_binds_to_current_plan(self) -> None:
        run_dir = self.runs / "run"
        plan_week(self.config, PROFILE, CHECKIN, output_dir=str(run_dir))
        write_json(run_dir / "approval.json", {"status": "approved"})
        approve_run(self.config, str(run_dir), dry_run=True)
        self.assertTrue(read_json(run_dir / "approval.json")["final_plan_sha256"])


# 3 -------------------------------------------------------------------------
class AdherenceDedupeTests(unittest.TestCase):
    def _run(self, root: Path, name: str, day: str, approval: dict, push_status: str | None) -> None:
        run = root / name
        run.mkdir()
        write_json(run / "approval.json", approval)
        write_json(run / "inputs.json", {"local_inputs": {"athlete_profile": read_json(PROFILE), "weekly_checkin": {}}})
        write_json(
            run / "final_plan.json",
            {"sessions": [{"date": day, "day": "X", "discipline": "Ride", "session_name": name, "duration_min": 60}]},
        )
        if push_status:
            write_json(run / "planning_state.json", {"push_status": push_status})

    def test_latest_pushed_run_per_week_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            approved = {"status": "approved", "approved_at": "2026-03-01T08:00:00"}
            self._run(root, "a-first", "2026-03-03", approved, "pushed")
            self._run(root, "b-replan", "2026-03-04", {**approved, "approved_at": "2026-03-03T08:00:00"}, "pushed")
            self._run(root, "c-dryrun", "2026-02-24", approved, "dry_run")
            self._run(root, "d-never", "2026-02-17", approved, None)
            contexts = _load_prior_plan_contexts(root, read_json(PROFILE), "2026-03-08", root / "current")
        names = [Path(item["source_run"]).name for item in contexts]
        self.assertEqual(names, ["b-replan"])


# 4 -------------------------------------------------------------------------
class SchemaContractTests(unittest.TestCase):
    def test_requirements_come_from_schemas(self) -> None:
        self.assertEqual(
            AGENT_REQUIREMENTS["race_director"], load_schema("race_director.json")["required"]
        )
        self.assertNotIn("race_recommendations", AGENT_REQUIREMENTS["race_director"])

    def test_nested_required_keys_are_validated(self) -> None:
        errors = validate_final_plan(
            {
                "athlete_brief": {"week_theme": "x"},
                "nutrition_summary": {"week_strategy": "x"},
                "phase_alignment_check": {},
                "sessions": [
                    {
                        "day": "Monday", "date": "2026-03-09", "discipline": "Swim",
                        "session_name": "Swim", "objective": "x", "duration_min": 30,
                        "intensity_guidance": "easy", "primary_metric": "notes",
                        "blocks": [{"section": "Main Set", "steps": [{"duration": "10m"}]}],
                        "execution_notes": [], "success_criteria": "x", "cut_first": "x",
                        "terrain": "moon",
                    }
                ],
            }
        )
        joined = " | ".join(errors)
        self.assertIn("orchestrator.athlete_brief is missing", joined)
        self.assertIn("tone", joined)
        self.assertIn("orchestrator.nutrition_summary is missing", joined)
        self.assertIn("alignment_status", joined)
        self.assertIn("needs 'duration' and 'target'", joined)
        self.assertIn("terrain", joined)

    def test_race_director_without_recommendations_is_valid(self) -> None:
        payload = {
            "agent": "race_director",
            "priority_race_lens": "x",
            "race_specific_needs": [],
            "mistake_to_avoid": "x",
            "specificity_locations": [],
        }
        self.assertEqual(validate_agent_output("race_director", payload), [])


# 5 -------------------------------------------------------------------------
class MissingCheckinValueTests(unittest.TestCase):
    def test_null_answers_are_unknown_not_fine(self) -> None:
        answered = performance_doctor.run(_doctor_snapshot(subjective_readiness=7, current_motivation=7))
        unknown = performance_doctor.run(_doctor_snapshot(subjective_readiness=None, current_motivation=None))
        self.assertEqual(unknown["readiness_score"], answered["readiness_score"] - 4)
        self.assertEqual(sum("not answered" in item for item in unknown["missing_data"]), 2)
        self.assertFalse(any("not answered" in item for item in answered["missing_data"]))

    def test_template_uses_null(self) -> None:
        template = read_json("data/weekly_checkin.template.json")
        for field in ("current_motivation", "subjective_readiness", "sleep_quality", "work_stress",
                      "illness_symptoms", "red_flags", "pain_scores", "available_hours_hard_cap_min"):
            self.assertIn(field, template)
            self.assertIsNone(template[field])


# 6 + Intervals-only races --------------------------------------------------
class StaleStateTests(unittest.TestCase):
    def _snapshot(self, races: list[dict]) -> dict:
        return {
            "planning_date": "2026-10-04",
            "week_start": "2026-10-05",
            "races": races,
            "periodization_state": {"next_block_week": 1, "recommended_next_week_type": "load"},
        }

    def _state(self, updated: str, phase: str = "race_specific", key: str | None = None) -> dict:
        return {
            "last_updated_at": updated,
            "active_macro_cycle": {
                "phase": phase, "cycle_type": "load", "current_block_week": 3,
                "block_start_date": "2026-09-14", "phase_race_key": key,
            },
            "history": [],
        }

    def test_stale_state_drops_race_specific_phase(self) -> None:
        state = bootstrap_athlete_state(self._snapshot([]), self._state("2026-09-01T10:00:00"))
        self.assertIsNone(state["active_macro_cycle"]["phase"])
        self.assertIn("days before", state["stale_reset"]["reason"])

    def test_fresh_state_for_same_intervals_race_is_kept(self) -> None:
        race = {"name": "Hill Trail", "date": "2027-05-08", "priority": "A", "days_to_race": 216}
        state = bootstrap_athlete_state(
            self._snapshot([race]),
            self._state("2026-10-01T10:00:00", phase="base", key="hill trail|2027-05-08"),
        )
        self.assertEqual(state["active_macro_cycle"]["phase"], "base")
        self.assertEqual(state["current_priority_race"]["name"], "Hill Trail")

    def test_changed_intervals_race_resets_phase_but_keeps_block(self) -> None:
        race = {"name": "Houffa Trail", "date": "2026-11-15", "priority": "A", "days_to_race": 41}
        state = bootstrap_athlete_state(
            self._snapshot([race]),
            self._state("2026-10-01T10:00:00", phase="base", key="hill trail|2027-05-08"),
        )
        self.assertIsNone(state["active_macro_cycle"]["phase"])
        self.assertEqual(state["active_macro_cycle"]["current_block_week"], 3)

    def test_no_race_on_calendar_never_keeps_race_specific(self) -> None:
        state = bootstrap_athlete_state(
            self._snapshot([]), self._state("2026-10-01T10:00:00", key="old race|2026-10-18")
        )
        self.assertIsNone(state["active_macro_cycle"]["phase"])
        self.assertIsNone(state["current_priority_race"])

    def test_legacy_stored_race_is_migrated_and_never_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "athlete_state.json"
            write_json(path, {"active_macro_cycle": {"phase": "race_specific", "priority_race": {"name": "City Half", "date": "2026-10-18"}}})
            state = load_athlete_state(path)
        self.assertNotIn("priority_race", state["active_macro_cycle"])
        self.assertEqual(state["last_planned_priority_race"]["name"], "City Half")
        snapshot = {**self._snapshot([]), "athlete_state": state}
        self.assertIsNone(select_priority_race(snapshot))

    def test_update_records_timestamp_and_informational_race(self) -> None:
        plan = {
            "macro_cycle_decision": {"phase": "base", "friel_block_week": 1},
            "race_notes": {"priority_race": {"name": "Hill Trail", "date": "2027-05-08", "days_to_race": 200}},
            "sessions": [{"date": "2026-10-06", "duration_min": 60}],
        }
        state = update_athlete_state_from_plan({"active_macro_cycle": {}, "history": []}, plan, "runs/x", now=datetime(2026, 10, 4, 18))
        self.assertEqual(state["last_updated_at"], "2026-10-04T18:00:00")
        self.assertEqual(state["active_macro_cycle"]["phase_race_key"], "hill trail|2027-05-08")
        self.assertNotIn("days_to_race", state["last_planned_priority_race"])


# 7 -------------------------------------------------------------------------
class AtomicWriteTests(unittest.TestCase):
    def test_atomic_write_and_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "athlete_state.json"
            write_athlete_state(path, {"version": 1, "history": []})
            write_athlete_state(path, {"version": 2, "history": []})
            self.assertEqual(read_json(backup_path(path))["version"], 1)
            self.assertEqual(read_json(path)["version"], 2)
            self.assertEqual(sorted(p.name for p in Path(temp).iterdir()), ["athlete_state.json", "athlete_state.json.bak"])

    def test_corrupt_or_non_dict_state_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "athlete_state.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(AthleteStateError):
                load_athlete_state(path)
            write_json(path, [1, 2])
            with self.assertRaises(AthleteStateError):
                load_athlete_state(path)


# 9 -------------------------------------------------------------------------
class PartialWeekTests(unittest.TestCase):
    def test_sunday_checkin_counts_week_complete_only_when_done(self) -> None:
        sunday = date(2026, 3, 8)
        swim = [{"start_date_local": "2026-03-08T07:00:00", "type": "Swim"}]
        planned = [
            {"date": "2026-03-08", "discipline": "Swim"},
            {"date": "2026-03-08", "discipline": "Run"},
        ]
        self.assertTrue(is_partial_week(sunday, sunday, swim, today=sunday))
        self.assertTrue(is_partial_week(sunday, sunday, swim, planned_sessions=planned, today=sunday))
        both = swim + [{"start_date_local": "2026-03-08T10:00:00", "type": "Run"}]
        self.assertFalse(is_partial_week(sunday, sunday, both, planned_sessions=planned, today=sunday))
        self.assertFalse(is_partial_week(sunday, sunday, [], today=sunday + timedelta(days=1)))


# 10 ------------------------------------------------------------------------
class RedFlagTests(IsolatedStateCase):
    def test_detection_en_nl_and_not_a_niggle(self) -> None:
        self.assertEqual(detect_red_flags("chest tightness on the climb"), ["chest_pain"])
        self.assertEqual(detect_red_flags("hartkloppingen tijdens de rit"), ["palpitations"])
        self.assertEqual(detect_red_flags("koorts sinds gisteren"), ["fever"])
        self.assertEqual(detect_red_flags("felt dizzy after the run"), ["dizziness_or_fainting"])
        self.assertEqual(detect_red_flags("no chest pain, geen koorts"), [])
        self.assertFalse(has_niggle("chest tightness on the run"))
        self.assertTrue(has_niggle("chest tightness, calf sore"))

    def test_red_flag_forces_red_rest_and_doctor(self) -> None:
        result = performance_doctor.run(_doctor_snapshot(pain_or_niggles="Chest tightness on the hills"))
        self.assertEqual(result["readiness_status"], "red")
        self.assertEqual(result["load_recommendation"], "reduce")
        self.assertEqual(result["intensity_allowance"]["max_quality_sessions"], 0)
        self.assertEqual(result["training_clearance"], "no_training_see_doctor")
        self.assertTrue(any("see a doctor" in item for item in result["constraints"]))
        self.assertNotIn("pain_or_niggles", result["fatigue_markers"])

    def test_activity_note_and_structured_red_flags(self) -> None:
        snapshot = _doctor_snapshot()
        snapshot["activity_note_flags"] = [
            {"date": "2026-03-07", "activity_name": "Run", "kind": "red_flag", "excerpt": "heart racing at easy pace", "body_part": None}
        ]
        self.assertEqual(performance_doctor.run(snapshot)["readiness_status"], "red")
        result = performance_doctor.run(_doctor_snapshot(illness_symptoms=["fever 38.5"]))
        self.assertEqual(result["training_clearance"], "no_training_see_doctor")

    def test_neck_check_above_the_neck_is_easy_only(self) -> None:
        result = performance_doctor.run(_doctor_snapshot(illness_symptoms=["runny nose"]))
        self.assertEqual(result["training_clearance"], "easy_only")
        self.assertEqual(result["readiness_status"], "yellow")
        self.assertEqual(result["intensity_allowance"]["max_quality_sessions"], 0)

    def test_stub_plan_becomes_rest_with_doctor_message_in_report(self) -> None:
        run_dir = self.runs / "run"
        checkin = self.write_checkin(red_flags=["chest pain during the intervals"])
        plan_week(self.config, PROFILE, checkin, output_dir=str(run_dir))
        plan = read_json(run_dir / "final_plan.json")
        self.assertTrue(all(s["discipline"] == "Recovery" for s in plan["sessions"]))
        report = (run_dir / "weekly_report.md").read_text(encoding="utf-8")
        self.assertIn("STOP: Medical Red Flag", report)
        self.assertIn("see a doctor before resuming", report)
        self.assertIn("see a doctor", plan["approval_checkpoint"])


# 11 ------------------------------------------------------------------------
class NewSignalTests(unittest.TestCase):
    def test_return_from_break_caps_load(self) -> None:
        snapshot = _doctor_snapshot()
        snapshot["training_load"]["return_from_break"] = {
            "status": "returning", "days_off": 12, "reentry_week": 1, "volume_cap_pct": 60,
            "run_km_cap": 20.0, "long_run_cap_min": 60, "rationale": "COVID break",
        }
        result = performance_doctor.run(snapshot)
        self.assertEqual(result["load_recommendation"], "reduce")
        self.assertLessEqual(result["intensity_allowance"]["max_quality_sessions"], 1)
        self.assertTrue(any("60%" in item for item in result["constraints"]))
        self.assertEqual(result["return_from_break"]["status"], "returning")

    def test_daily_signals_and_overreaching(self) -> None:
        snapshot = _doctor_snapshot()
        snapshot["training_load"]["daily_signals"] = {"status": "warning", "days_flagged": 2, "findings": []}
        self.assertEqual(performance_doctor.run(snapshot)["readiness_status"], "yellow")
        snapshot["training_load"]["daily_signals"]["status"] = "alert"
        self.assertEqual(performance_doctor.run(snapshot)["readiness_status"], "red")
        snapshot = _doctor_snapshot()
        snapshot["training_load"]["overreaching"] = {"status": "non_functional_risk", "markers": ["hrv"], "rationale": "x"}
        result = performance_doctor.run(snapshot)
        self.assertEqual(result["readiness_status"], "red")
        self.assertEqual(result["intensity_allowance"]["max_quality_sessions"], 0)
        self.assertTrue(any("recovery week" in item for item in result["constraints"]))

    def test_stale_hrv_is_missing_data_not_green(self) -> None:
        snapshot = _doctor_snapshot()
        snapshot["training_load"]["hrv_gate"] = {"status": "stale", "extra_intensity_allowed": True}
        result = performance_doctor.run(snapshot)
        self.assertEqual(result["intensity_allowance"]["hrv_gate"], "stale")
        self.assertFalse(result["intensity_allowance"]["extra_intensity_allowed"])
        self.assertTrue(any("stale" in item for item in result["missing_data"]))
        self.assertEqual(validate_agent_output("performance_doctor", result), [])

    def test_underload_explained_and_efficiency_and_notes(self) -> None:
        snapshot = _doctor_snapshot()
        snapshot["training_load"]["underload"] = {"status": "explained_physiological", "markers": [], "recommendation": "x"}
        snapshot["training_load"]["efficiency_trend"] = {"status": "declining", "ef_change_pct": -4.0}
        snapshot["activity_note_flags"] = [
            {"date": "2026-03-06", "activity_name": "Trail", "kind": "pain", "excerpt": "knee hurt downhill", "body_part": "knee"}
        ]
        result = performance_doctor.run(snapshot)
        self.assertNotEqual(result["load_recommendation"], "increase")
        self.assertTrue(any("efficiency" in item for item in result["objective_findings"]))
        self.assertTrue(any("knee" in item for item in result["subjective_findings"]))

    def test_wellness_weight_sleep_stress_and_pain_scores(self) -> None:
        snapshot = _doctor_snapshot(sleep_quality=2, work_stress=9, pain_scores={"ITB": 6, "calf": 1})
        snapshot["recent_wellness_summary"] = {"stress": {"mean_7d": 3.4}, "weight_trend_kg_28d": -2.1}
        result = performance_doctor.run(snapshot)
        self.assertTrue(result["energy_availability_concern"]["flag"])
        self.assertEqual(result["no_impact_areas"], ["ITB"])
        self.assertNotEqual(result["readiness_status"], "green")
        self.assertIn("work_stress_high", result["fatigue_markers"])
        self.assertIn("poor_sleep_reported", result["fatigue_markers"])
        self.assertIn("wellness_stress_high", result["fatigue_markers"])


# 12 ------------------------------------------------------------------------
class SessionWhyTests(unittest.TestCase):
    def _session(self) -> dict:
        return {
            "date": "2026-03-10", "day": "Tuesday", "discipline": "Run", "session_name": "Easy Trail Run",
            "objective": "Aerobic durability: 45m Z2 at 5:30/km on soft trails",
            "feel_cue": "- Conversational, nose breathing, 140bpm max",
            "rpe_target": "3-4/10",
            "bail_out_rule": "If the ITB twinges, walk 5m and cut the run short",
            "terrain": "trail", "elevation_gain_m": 250,
            "duration_min": 45, "primary_metric": "HR",
            "blocks": [
                {"section": "Warmup", "steps": [{"duration": "10m", "target": "Z1 HR"}]},
                {"section": "Main Set", "steps": [{"duration": "30m", "target": "Z2 HR"}]},
            ],
        }

    def test_preamble_is_plain_text_and_steps_still_parse(self) -> None:
        event = compile_session_to_event(self._session(), run_id="x")
        preamble, steps = event["description"].split("\n\n", 1)
        lines = preamble.splitlines()
        self.assertTrue(lines[0].startswith("Why:"))
        self.assertTrue(any(line.startswith("RPE target: 3-4/10") for line in lines))
        for line in lines:
            self.assertFalse(line.startswith("-"))
            self.assertFalse(syntax_guard.looks_like_step(line), line)
        self.assertIn("- 10m Z1 HR", steps)
        reviewed = syntax_guard.review(event)
        self.assertIn(lines[0], reviewed["final_description"])
        self.assertNotIn("- Why:", reviewed["final_description"])
        self.assertIn("- 30m Z2 HR", reviewed["final_description"])

    def test_syntax_guard_still_prefixes_bare_steps(self) -> None:
        self.assertTrue(syntax_guard.looks_like_step("10m Z2"))
        self.assertFalse(syntax_guard.is_coach_text_line("Why: 10m Z2"))
        reviewed = syntax_guard.review({"type": "Run", "primary_metric": "HR", "description": "Why: easy\nWarmup\n10m Z1 HR"})
        self.assertIn("Why: easy", reviewed["final_description"])
        self.assertIn("- 10m Z1 HR", reviewed["final_description"])


# 13 ------------------------------------------------------------------------
class AdjustWeekTests(IsolatedStateCase):
    def test_adjust_week_replans_and_pushes_only_remaining_days(self) -> None:
        original = self.runs / "orig"
        plan_week(self.config, PROFILE, CHECKIN, output_dir=str(original), auto_approve=True, dry_run=True)
        adjusted = self.runs / "adjusted"
        result = adjust_week(self.config, str(original), "2026-03-12", "work trip Thursday", output_dir=str(adjusted))
        plan = read_json(adjusted / "final_plan.json")
        self.assertEqual(plan["adjustment"]["replaces_run"], str(original))
        self.assertTrue(all(s["date"] >= "2026-03-12" for s in plan["sessions"]))
        context = read_json(adjusted / "adjustment_context.json")
        self.assertTrue(all(row["date"] < "2026-03-12" for row in context["done_so_far"]))
        self.assertTrue(result["done_so_far"])
        self.assertEqual(read_json(adjusted / "approval.json")["status"], "pending")

        finalized = finalize_run(str(adjusted))
        self.assertEqual(finalized["approval_status"], "pending")
        with self.assertRaises(ValueError):
            approve_run(self.config, str(adjusted), dry_run=True)
        finalize_run(str(adjusted), auto_approve=True)
        approve_run(self.config, str(adjusted), dry_run=True)
        payload = read_json(adjusted / "compiled_push_payload.json")
        self.assertTrue(payload)
        self.assertTrue(all(item["start_date_local"][:10] >= "2026-03-12" for item in payload))
        report = (adjusted / "weekly_report.md").read_text(encoding="utf-8")
        self.assertIn("Mid-week adjustment from 2026-03-12", report)

    def test_adjust_requires_an_approved_run(self) -> None:
        original = self.runs / "orig"
        plan_week(self.config, PROFILE, CHECKIN, output_dir=str(original))
        with self.assertRaises(ValueError):
            adjust_week(self.config, str(original), "2026-03-12", "x", output_dir=str(self.runs / "a"))
        self.assertEqual(
            main(["adjust-week", str(original), "--from", "2026-03-12", "--reason", "x", "--mode", "mock", "--output-dir", str(self.runs / "b")]),
            2,
        )


# 14 ------------------------------------------------------------------------
class LongTermMemoryTests(IsolatedStateCase):
    def test_week_actuals_races_and_injuries(self) -> None:
        adherence = {
            "status": "available", "planned_week_start": "2026-09-28", "source_run": "runs/a",
            "key_sessions_planned": 2, "adherence_score": 0.75, "missed_sessions": ["Tempo"],
            "session_reviews": [
                {"session_name": "Long Run", "priority": "key", "status": "completed_on_day", "planned_minutes": 90, "actual_minutes": 92},
                {"session_name": "Tempo", "priority": "key", "status": "missed", "planned_minutes": 60, "actual_minutes": 0},
            ],
        }
        checkin = {"planning_date": "2026-10-04", "pain_or_niggles": "ITB tight", "pain_scores": {"calf": 4},
                   "missed_session_reasons": {"Tempo": "had a cold"}, "race_debrief": "Went out too fast, faded at 30k."}
        state = record_week_actuals({"weekly_actuals": []}, adherence, checkin)
        week = state["weekly_actuals"][0]
        self.assertEqual((week["planned_min"], week["completed_min"], week["adherence_pct"]), (150, 92, 75.0))
        self.assertEqual(week["key_sessions_done"], ["Long Run"])
        self.assertTrue(week["illness"])
        self.assertEqual(week["niggles"], ["ITB", "calf"])

        snapshot = {
            "planning_date": "2026-10-04",
            "races": [{"name": "Houffa Trail 30k", "date": "2026-10-03", "priority": "B", "distance": "Trail 30k", "days_to_race": -2}],
            "intervals": {"activities": [{"start_date_local": "2026-10-03T09:00:00", "moving_time": 11000, "elapsed_time": 11400}]},
            "activity_note_flags": [{"date": "2026-10-03", "kind": "pain", "body_part": "knee", "excerpt": "knee sore"}],
        }
        state = capture_race_results(state, snapshot, checkin)
        race = state["race_results"][0]
        self.assertEqual((race["name"], race["time"]), ("Houffa Trail 30k", "3:10:00"))
        self.assertIn("faded", race["debrief"])
        state = log_injuries(state, snapshot, checkin)
        parts = {(item["body_part"], item["source"]) for item in state["injury_log"]}
        self.assertIn(("ITB", "checkin"), parts)
        self.assertIn(("calf", "checkin_pain_score"), parts)
        self.assertIn(("knee", "activity_note"), parts)

        report = render_weekly_report(
            {"athlete_state": state, "races": []},
            {"readiness_status": "green", "readiness_score": 80, "main_risk": "x", "objective_findings": [], "subjective_findings": []},
            {}, {"priority_race_lens": "x", "race_specific_needs": [], "mistake_to_avoid": "x"},
            read_json(self._plan_path()),
        )
        self.assertIn("## Long-Term Memory", report)
        self.assertIn("Houffa Trail 30k", report)

    def _plan_path(self) -> Path:
        run_dir = self.runs / "plan"
        if not (run_dir / "final_plan.json").exists():
            plan_week(self.config, PROFILE, CHECKIN, output_dir=str(run_dir))
        return run_dir / "final_plan.json"

    def test_real_push_writes_long_term_state(self) -> None:
        run_dir = self.runs / "run"
        plan_week(self.config, PROFILE, self.write_checkin(athlete_notes="Need to decide whether to race the Leuven 10k."),
                  output_dir=str(run_dir), auto_approve=True, dry_run=False)
        state = read_json(self.state_dir / "athlete_state.json")
        self.assertTrue(state["last_updated_at"])
        self.assertNotIn("priority_race", state["active_macro_cycle"])
        self.assertTrue(state["open_decisions"])
        self.assertTrue(read_json(run_dir / "approval.json")["pushed_at"])


# 15 ------------------------------------------------------------------------
class MemoryTests(unittest.TestCase):
    def _week(self, start: str, run: str, adherence: float) -> dict:
        return {
            "status": "available", "planned_week_start": start, "source_run": run, "adherence_score": adherence,
            "day_breakdown": {}, "session_type_breakdown": {}, "missed_sessions": [], "double_days": {},
            "session_reviews": [],
        }

    def test_illness_weeks_are_excluded(self) -> None:
        history = [self._week("2026-09-28", "r2", 0.3), self._week("2026-09-21", "r1", 0.9)]
        memory = build_athlete_memory(
            history, {}, {}, {"missed_session_reasons": {"Long Run": "sick with covid"}},
            checkins_by_run={"r1": {}, "r2": {}},
        )
        self.assertEqual(memory["excluded_health_weeks"], ["2026-09-28"])
        self.assertEqual(memory["weeks_reviewed"], 1)
        self.assertEqual(memory["average_adherence_score"], 0.9)
        event_memory = build_athlete_memory(
            history, {}, {}, {},
            health_events=[{"kind": "sick", "start_date": "2026-09-22", "end_date": "2026-09-24"}],
        )
        self.assertEqual(event_memory["excluded_health_weeks"], ["2026-09-21"])

    def test_rpe_and_feel_in_response_profile(self) -> None:
        self.assertEqual(rpe_compliance({"planned_intensity": "easy", "icu_rpe": 6}), "harder_than_intended")
        self.assertEqual(rpe_compliance({"planned_intensity": "easy", "icu_rpe": 4}), "as_intended")
        self.assertEqual(rpe_compliance({"rpe_target": "3-4/10", "icu_rpe": 6}), "harder_than_intended")
        reviews = [
            {"session_name": f"Easy {i}", "status": "completed_on_day", "planned_intensity": "easy", "icu_rpe": 6, "feel": 5}
            for i in range(3)
        ]
        profile = build_response_profile([{**self._week("2026-09-21", "r1", 1.0), "session_reviews": reviews}])
        self.assertEqual(len(profile["rpe_harder_than_intended"]), 3)
        self.assertEqual(len(profile["feel_weak_sessions"]), 3)
        self.assertTrue(any("harder than intended" in flag for flag in profile["flags"]))
        self.assertTrue(any("1 = strong" in flag for flag in profile["flags"]))


# 16 ------------------------------------------------------------------------
class CheckinQuestionTests(unittest.TestCase):
    def test_questions_cover_safety_body_life_and_debrief(self) -> None:
        checkin = build_post_fetch_checkin(
            "2026-10-04",
            pending_decisions=[{"id": "race-goal", "question": "Race goal for Hill Trail (2027-05-08)?", "due_date": "2026-10-11"}],
            recent_races=[{"name": "Houffa Trail", "date": "2026-10-03"}],
        )
        keys = [question["key"] for question in checkin["questions"]]
        for key in ("red_flag_screen", "illness_symptoms", "pain_or_niggles", "sleep_quality", "work_life_stress",
                    "missed_session_reasons", "available_hours", "upcoming_events", "race_debrief", "decision:race-goal"):
            self.assertIn(key, keys)
        self.assertIn("record-checkin", checkin["intro"])


# 17 ------------------------------------------------------------------------
class OverwriteProtectionTests(IsolatedStateCase):
    def test_athlete_edited_event_is_skipped_unless_forced(self) -> None:
        fixtures = self.temp / "fixtures"
        shutil.copytree("fixtures", fixtures)
        config = replace(self.config, fixture_dir=fixtures)
        first = self.runs / "a-first"
        plan_week(config, PROFILE, CHECKIN, output_dir=str(first), auto_approve=True, dry_run=True)
        approval = read_json(first / "approval.json")
        approval["pushed_at"] = "2026-03-08T19:00:00"
        write_json(first / "approval.json", approval)
        pushed = read_json(first / "compiled_push_payload.json")
        workout = next(item for item in pushed if item.get("category") == "WORKOUT")
        untouched = next(item for item in pushed if item.get("category") == "WORKOUT" and item is not workout)
        events = read_json(fixtures / "events.json")
        events.append({**workout, "id": 777, "name": "My own version", "start_date_local": "2026-03-13T00:00:00"})
        events.append({**untouched, "id": 778})
        write_json(fixtures / "events.json", events)

        second = self.runs / "b-second"
        plan_week(config, PROFILE, CHECKIN, output_dir=str(second), auto_approve=True, dry_run=True)
        report = read_json(second / "edited_events.json")
        self.assertEqual([item["external_id"] for item in report["edited"]], [workout["external_id"]])
        self.assertIn("renamed", report["edited"][0]["changes"])
        sent = {item["external_id"] for item in read_json(second / "compiled_push_payload.json")}
        self.assertNotIn(workout["external_id"], sent)
        self.assertIn(untouched["external_id"], sent)

        result = approve_run(config, str(second), dry_run=True, overwrite_edited=True)
        sent = {item["external_id"] for item in read_json(second / "compiled_push_payload.json")}
        self.assertIn(workout["external_id"], sent)
        self.assertTrue(result["edited_events_overwritten"])


# 18 + race references from Intervals --------------------------------------
class OpenDecisionTests(IsolatedStateCase):
    def test_orchestrator_raises_decision_naming_the_intervals_race(self) -> None:
        snapshot = {
            "week_start": "2026-03-09",
            "weekly_checkin": {"athlete_notes": "I still need to decide my race goal. Long run on Sunday."},
            "races": [{"name": "Brussels Olympic Triathlon", "date": "2026-06-07", "priority": "A", "days_to_race": 90}],
            "athlete_profile": {},
        }
        decisions = orchestrator.build_open_decisions(snapshot)
        self.assertEqual(len(decisions), 1)
        self.assertIn("Brussels Olympic Triathlon (2026-06-07)", decisions[0]["question"])
        self.assertEqual(decisions[0]["due_date"], "2026-03-15")
        snapshot["races"] = []
        self.assertIn("no race is on your Intervals calendar", orchestrator.build_open_decisions(snapshot)[0]["question"])

    def test_contract_store_resolve_and_carry_over(self) -> None:
        errors = validate_final_plan({"open_decisions": [{"question": "", "due_date": "next week"}]})
        self.assertTrue(any("open_decisions[1].question" in error for error in errors))
        self.assertTrue(any("due_date" in error for error in errors))
        plan = {"open_decisions": [{"question": "Enter the Leuven 10k?", "due_date": "2026-03-20", "options": ["yes", "no"]}]}
        state = store_open_decisions({"open_decisions": []}, plan, "runs/a")
        self.assertEqual(state["open_decisions"][0]["status"], "open")
        answered = resolve_open_decisions(state, {"decision_answers": {"Enter the Leuven 10k?": "yes"}})
        self.assertEqual(answered["open_decisions"][0]["status"], "answered")
        overdue = resolve_open_decisions(state, {}, today="2026-03-25")
        self.assertEqual(overdue["open_decisions"][0]["status"], "defaulted")

        write_json(self.state_dir / "athlete_state.json", state)
        run_dir = self.runs / "run"
        plan_week(self.config, PROFILE, CHECKIN, output_dir=str(run_dir))
        context = read_json(run_dir / "run_context.json")
        self.assertTrue(any(q["key"].startswith("decision:") for q in context["post_fetch_checkin"]["questions"]))
        report = (run_dir / "weekly_report.md").read_text(encoding="utf-8")
        self.assertIn("## Open Decisions (your answer needed)", report)
        self.assertIn("Enter the Leuven 10k?", report)
        self.assertIn("Open decision:", report)

    def test_report_race_lens_comes_from_intervals_only(self) -> None:
        plan = read_json(self._plan())
        perf = {"readiness_status": "green", "readiness_score": 80, "main_risk": "x", "objective_findings": [], "subjective_findings": []}
        race = {"priority_race_lens": "x", "race_specific_needs": [], "mistake_to_avoid": "x"}
        state = {"last_planned_priority_race": {"name": "Old Stored Race", "date": "2027-01-01"}}
        report = render_weekly_report(
            {"races": [], "athlete_state": state, "race_calendar_warnings": ["Local race 'X' is not on Intervals and was ignored."]},
            perf, {}, race, plan,
        )
        self.assertIn("No race on your Intervals calendar", report)
        self.assertIn("Local race 'X' is not on Intervals", report)
        self.assertNotIn("Old Stored Race", report)

    def test_report_shows_the_binding_hours_cap_and_what_was_cut(self) -> None:
        plan = read_json(self._plan())
        plan["weekly_time_budget"] = {
            **plan["weekly_time_budget"],
            "mode": "hard_cap",
            "hard_cap_min": 480,
            "cuts": ["Thursday easy run shortened by 20 min"],
        }
        perf = {"readiness_status": "green", "readiness_score": 80, "main_risk": "x", "objective_findings": [], "subjective_findings": []}
        race = {"priority_race_lens": "x", "race_specific_needs": [], "mistake_to_avoid": "x"}
        report = render_weekly_report({"races": []}, perf, {}, race, plan)
        self.assertIn("Hours you have this week (cap): 8h 0m", report)
        self.assertIn("Cut to fit: Thursday easy run shortened by 20 min", report)

    def _plan(self) -> Path:
        run_dir = self.runs / "plan"
        plan_week(self.config, PROFILE, CHECKIN, output_dir=str(run_dir))
        return run_dir / "final_plan.json"


# 19 ------------------------------------------------------------------------
class NutritionTests(unittest.TestCase):
    def test_weight_drop_return_and_post_illness_notes(self) -> None:
        draft = {"sessions": [{"date": "2026-03-10", "day": "Tuesday", "discipline": "Run", "session_name": "Easy Run", "duration_min": 45, "primary_metric": "HR"}]}
        performance = {
            "readiness_status": "yellow",
            "energy_availability_concern": {"flag": True, "weight_change_kg_28d": -2.0},
            "return_from_break": {"status": "returning"},
            "health_events": {"status": "returning", "events": [{"kind": "sick"}]},
        }
        result = nutritionist.run({"weekly_checkin": {}}, draft, performance, {}, {})
        self.assertEqual(result["energy_availability_risk"]["status"], "high")
        concerns = " ".join(result["major_concerns"])
        self.assertIn("Weight is trending down", concerns)
        self.assertIn("Rebuild week", concerns)
        self.assertIn("Post-illness fuelling", concerns)
        plain = nutritionist.run({"weekly_checkin": {}}, draft, {"readiness_status": "green"}, {}, {})
        self.assertEqual(plain["special_notes"], [])


if __name__ == "__main__":
    unittest.main()
