from __future__ import annotations

import unittest

from triathlon_agent.agents import syntax_guard


class SyntaxGuardTests(unittest.TestCase):
    def test_rewrites_missing_step_prefix_and_metric(self) -> None:
        workout = {
            "type": "Run",
            "primary_metric": "HR",
            "description": "Warmup\n10m Z1 Pace\nMain Set\n30m Z2 Pace",
        }
        reviewed = syntax_guard.review(workout)
        self.assertEqual(reviewed["status"], "rewritten")
        self.assertIn("- 10m Z1 HR", reviewed["final_description"])
        self.assertIn("- 30m Z2 HR", reviewed["final_description"])
        self.assertTrue(reviewed["push_ready"])


if __name__ == "__main__":
    unittest.main()
