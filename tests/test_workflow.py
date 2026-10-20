from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import tempfile
import unittest
from pathlib import Path

from triathlon_agent.agents import (
    head_coach_planner,
    macro_coach,
    nutritionist,
    orchestrator,
    phase_alignment_auditor,
    performance_doctor,
    race_director,
    training_philosophy_auditor,
    validator_coach,
)
from triathlon_agent.config import load_config
from triathlon_agent.run_manifest import agent_output_path
from triathlon_agent.utils import read_json
from triathlon_agent.workflow import (
    StalePlanningDateError,
    approve_run,
    finalize_run,
    plan_week,
    prepare_run,
)
from triathlon_agent.utils import write_json


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config(mode="mock")
        self.athlete_profile = "fixtures/athlete_profile.json"
        self.weekly_checkin = "fixtures/weekly_checkin.json"

    def test_plan_week_creates_draft_and_pending_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "run"
            result = plan_week(
                self.config,
                athlete_profile_path=self.athlete_profile,
                weekly_checkin_path=self.weekly_checkin,
                output_dir=str(output_dir),
            )

            self.assertEqual(result["approval_status"], "pending")
            self.assertTrue((output_dir / "weekly_report.md").exists())
            self.assertTrue((output_dir / "athlete_brief.md").exists())
            self.assertTrue((output_dir / "final_plan.json").exists())
            self.assertFalse((output_dir / "compiled_workouts.json").exists())

            report = (output_dir / "weekly_report.md").read_text(encoding="utf-8")
            athlete_brief = (output_dir / "athlete_brief.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("## Athlete Brief", report)
            self.assertIn("Protect if busy", athlete_brief)
            self.assertIn("## Coach Note", report)
            self.assertIn("## Weekly Volume", report)
            self.assertIn("## Coaching Audit", report)
            self.assertIn("## Strategic Overview", report)
            self.assertIn("## Seven-Day Plan", report)
            self.assertIn("## Nutrition Guidance", report)
            self.assertIn("## Nutrition Note Previews", report)
            self.assertIn("## Weekly Focus Note Previews", report)
            self.assertIn("## Readiness / Risk Status", report)
            self.assertIn("## Changes vs Prior Week", report)
            self.assertIn("## Macro-Cycle Decision", report)
            self.assertIn("## Phase Alignment Check", report)
            self.assertIn("## Training Philosophy Check", report)
            self.assertIn("FTP Test", report)
            self.assertNotIn("Daniels quality run", report)
            self.assertNotIn("one clear bike quality session", report)

            final_plan = read_json(output_dir / "final_plan.json")
            self.assertIn("validator_adjustments_incorporated", final_plan)
            self.assertEqual(final_plan["readiness_status"], "yellow")
            self.assertIn("athlete_brief", final_plan)
            self.assertIn("weekly_time_budget", final_plan)
            self.assertIn("weekly_focus_notes", final_plan)
            # Focus notes are rebuilt from sessions at finalize time; first note
            # must match the first session's date and name.
            first_session = final_plan["sessions"][0]
            self.assertEqual(
                final_plan["weekly_focus_notes"][0]["date"], first_session["date"]
            )
            self.assertIn(
                first_session["session_name"],
                final_plan["weekly_focus_notes"][0]["title"],
            )
            self.assertEqual(
                len(final_plan["athlete_brief"]["protect_if_busy"]),
                len(set(final_plan["athlete_brief"]["protect_if_busy"])),
            )
            self.assertIn("Brussels Olympic Triathlon", report)

    def test_prepare_run_rejects_stale_planning_date_in_live_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            checkin_data = read_json(self.weekly_checkin)
            checkin_data["planning_date"] = "2020-01-01"
            checkin_path = Path(temp_dir) / "weekly_checkin.json"
            write_json(checkin_path, checkin_data)
            live_config = replace(self.config, mode="live", intervals_api_key="test-key")

            with self.assertRaises(StalePlanningDateError):
                prepare_run(
                    live_config,
                    athlete_profile_path=self.athlete_profile,
                    weekly_checkin_path=str(checkin_path),
                    output_dir=str(Path(temp_dir) / "run"),
                )

    def test_prepare_and_finalize_support_agent_orchestration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "run"
            prepared = prepare_run(
                self.config,
                athlete_profile_path=self.athlete_profile,
                weekly_checkin_path=self.weekly_checkin,
                output_dir=str(output_dir),
            )

            self.assertTrue((output_dir / "normalized_snapshot.json").exists())
            self.assertTrue((output_dir / "run_context.json").exists())
            self.assertTrue((output_dir / "planning_state.json").exists())
            self.assertEqual(prepared["run_dir"], str(output_dir))

            snapshot = read_json(output_dir / "normalized_snapshot.json")
            perf = performance_doctor.run(snapshot)
            macro = macro_coach.run(snapshot, perf)
            race = race_director.run(snapshot, macro)
            draft = head_coach_planner.run(snapshot, perf, macro, race)
            phase = phase_alignment_auditor.run(snapshot, macro, race, draft)
            philosophy = training_philosophy_auditor.run(
                snapshot, macro, race, phase, draft
            )
            validator = validator_coach.run(snapshot, draft, perf, macro, race)
            nutrition = nutritionist.run(snapshot, draft, perf, macro, race)
            final_plan = orchestrator.run(
                snapshot,
                perf,
                macro,
                race,
                phase,
                philosophy,
                draft,
                validator,
                nutrition,
            )

            write_json(agent_output_path(output_dir, "performance_doctor"), perf)
            write_json(agent_output_path(output_dir, "macro_coach"), macro)
            write_json(agent_output_path(output_dir, "race_director"), race)
            write_json(agent_output_path(output_dir, "phase_alignment_auditor"), phase)
            write_json(
                agent_output_path(output_dir, "training_philosophy_auditor"),
                philosophy,
            )
            write_json(agent_output_path(output_dir, "head_coach_planner"), draft)
            write_json(agent_output_path(output_dir, "validator_coach"), validator)
            write_json(agent_output_path(output_dir, "nutritionist"), nutrition)
            write_json(output_dir / "final_plan.json", final_plan)

            finalized = finalize_run(str(output_dir))

            self.assertEqual(finalized["approval_status"], "pending")
            self.assertTrue((output_dir / "weekly_report.md").exists())
            self.assertTrue((output_dir / "athlete_brief.md").exists())
            self.assertTrue(
                (output_dir / "agent_outputs" / "orchestrator.json").exists()
            )
            report = (output_dir / "weekly_report.md").read_text(encoding="utf-8")
            self.assertIn("## Approval Checkpoint", report)

    def test_prepare_run_loads_multiple_approved_history_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runs_dir = Path(temp_dir)
            config = replace(self.config, runs_dir=runs_dir)

            for run_name, session_date in (
                ("old-1", "2026-02-28"),
                ("old-2", "2026-03-07"),
            ):
                run_dir = runs_dir / run_name
                run_dir.mkdir(parents=True, exist_ok=True)
                write_json(
                    run_dir / "approval.json",
                    {
                        "status": "approved",
                        "approved_at": "2026-03-01T08:00:00",
                        "pushed_at": "2026-03-01T08:05:00",
                    },
                )
                write_json(
                    run_dir / "inputs.json",
                    {
                        "local_inputs": {
                            "athlete_profile": read_json(self.athlete_profile),
                            "weekly_checkin": {"planning_date": session_date},
                        }
                    },
                )
                write_json(
                    run_dir / "final_plan.json",
                    {
                        "sessions": [
                            {
                                "date": session_date,
                                "day": "Saturday",
                                "discipline": "Ride",
                                "session_name": "Long Ride with Race Rhythm",
                                "duration_min": 120,
                                "priority": "key",
                            }
                        ]
                    },
                )

            current_run = runs_dir / "current"
            prepare_run(
                config,
                athlete_profile_path=self.athlete_profile,
                weekly_checkin_path=self.weekly_checkin,
                output_dir=str(current_run),
            )

            snapshot = read_json(current_run / "normalized_snapshot.json")

            self.assertEqual(len(snapshot["prior_plan_history"]), 2)
            self.assertEqual(snapshot["athlete_memory"]["weeks_reviewed"], 2)
            self.assertEqual(
                snapshot["prior_plan_history"][0]["source_run"], str(runs_dir / "old-2")
            )

    def test_approve_run_compiles_and_pushes_in_dry_run_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "run"
            plan_week(
                self.config,
                athlete_profile_path=self.athlete_profile,
                weekly_checkin_path=self.weekly_checkin,
                output_dir=str(output_dir),
            )

            write_json(
                output_dir / "approval.json",
                {
                    "status": "approved",
                    "approved_at": datetime.now().isoformat(timespec="seconds"),
                    "requires_approval_before_push": True,
                },
            )

            approve_run(self.config, str(output_dir), dry_run=True)

            compiled = read_json(output_dir / "compiled_workouts.json")
            compiled_notes = read_json(output_dir / "compiled_notes.json")
            compiled_payload = read_json(output_dir / "compiled_push_payload.json")
            push_result = read_json(output_dir / "push_result.json")
            approval = read_json(output_dir / "approval.json")

            self.assertEqual(approval["status"], "approved")
            final_plan = read_json(output_dir / "final_plan.json")
            self.assertEqual(
                len(compiled),
                sum(1 for s in final_plan["sessions"] if s["discipline"] != "Recovery"),
            )
            self.assertGreater(len(compiled_notes), 0)
            self.assertEqual(len(compiled_payload), len(compiled) + len(compiled_notes))
            self.assertEqual(push_result["mode"], "mock")
            self.assertTrue(push_result["dry_run"])

            ride = next(item for item in compiled if item["type"] == "Ride")
            swim = next(item for item in compiled if item["type"] == "Swim")
            easy_run = next(
                item for item in compiled if item["name"] == "Easy Aerobic Run"
            )
            self.assertIn("%", ride["description"])
            self.assertIn("Easy swim", swim["description"])
            self.assertIn("HR", easy_run["description"])
            self.assertTrue(all(item["category"] == "NOTE" for item in compiled_notes))
            self.assertTrue(
                all(item["syntax_review"]["push_ready"] for item in compiled)
            )

    def test_approve_run_rejects_pending_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "run"
            plan_week(
                self.config,
                athlete_profile_path=self.athlete_profile,
                weekly_checkin_path=self.weekly_checkin,
                output_dir=str(output_dir),
            )

            with self.assertRaises(ValueError):
                approve_run(self.config, str(output_dir), dry_run=True)


if __name__ == "__main__":
    unittest.main()
