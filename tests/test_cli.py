from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from triathlon_agent.cli import main


class CliTests(unittest.TestCase):
    def test_prepare_run_command_creates_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "prepared-run"
            exit_code = main(
                [
                    "prepare-run",
                    "--mode",
                    "mock",
                    "--athlete-profile",
                    "fixtures/athlete_profile.json",
                    "--weekly-checkin",
                    "fixtures/weekly_checkin.json",
                    "--output-dir",
                    str(output_dir),
                ]
            )
            self.assertEqual(exit_code, 0)
            self.assertTrue((output_dir / "normalized_snapshot.json").exists())
            self.assertTrue((output_dir / "run_context.json").exists())
            run_context = json.loads((output_dir / "run_context.json").read_text())
            self.assertTrue(run_context["post_fetch_checkin"]["required_before_draft"])
            self.assertGreaterEqual(len(run_context["post_fetch_checkin"]["questions"]), 11)

    def test_freeform_plan_my_week_phrase_prepares_but_does_not_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "phrase-run"
            exit_code = main(
                [
                    "plan my week",
                    "--mode",
                    "mock",
                    "--athlete-profile",
                    "fixtures/athlete_profile.json",
                    "--weekly-checkin",
                    "fixtures/weekly_checkin.json",
                    "--output-dir",
                    str(output_dir),
                ]
            )
            self.assertEqual(exit_code, 0)
            self.assertTrue((output_dir / "normalized_snapshot.json").exists())
            self.assertTrue((output_dir / "run_context.json").exists())
            self.assertFalse((output_dir / "weekly_report.md").exists())

    def test_live_plan_week_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "live-run"
            exit_code = main(
                [
                    "plan-week",
                    "--mode",
                    "live",
                    "--athlete-profile",
                    "fixtures/athlete_profile.json",
                    "--weekly-checkin",
                    "fixtures/weekly_checkin.json",
                    "--output-dir",
                    str(output_dir),
                ]
            )
            self.assertEqual(exit_code, 2)
            self.assertFalse(output_dir.exists())


if __name__ == "__main__":
    unittest.main()
