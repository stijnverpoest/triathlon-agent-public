from __future__ import annotations

import unittest
from datetime import date

from triathlon_agent.session_library import build_session, reduction_priority


class SessionLibraryTests(unittest.TestCase):
    def test_build_threshold_ride_archetype(self) -> None:
        session = build_session(
            "threshold_ride",
            date(2026, 3, 11),
            75,
            lighter_week=False,
            recovery_week=False,
        )

        self.assertEqual(session["discipline"], "Ride")
        self.assertEqual(session["session_name"], "Threshold Bike Build")
        self.assertEqual(session["coggan_level"], "L4 Threshold")
        self.assertEqual(session["primary_metric"], "%FTP")

    def test_build_club_swim_can_include_distance_note(self) -> None:
        session = build_session(
            "swim_club",
            date(2026, 3, 31),
            60,
            lighter_week=False,
            recovery_week=False,
            club_session_distance_m=3000,
        )

        self.assertEqual(session["duration_min"], 60)
        self.assertIn("Expect roughly 3k total.", session["execution_notes"])

    def test_build_support_ride_archetype(self) -> None:
        session = build_session(
            "support_ride",
            date(2026, 4, 3),
            60,
            lighter_week=False,
            recovery_week=False,
        )

        self.assertEqual(session["discipline"], "Ride")
        self.assertEqual(session["session_name"], "Low-Cadence Strength Ride")
        self.assertEqual(session["coggan_level"], "L3 Tempo")
        self.assertIn("60-70rpm", str(session["blocks"]))

    def test_build_threshold_ride_can_use_goal_specific_variant_context(self) -> None:
        session = build_session(
            "threshold_ride",
            date(2026, 4, 14),
            75,
            lighter_week=False,
            recovery_week=False,
            session_context={
                "planning_date": "2026-04-14",
                "macro_phase": "build",
                "week_classification": "build",
                "priority_race": {"distance": "Ironman 70.3"},
                "primary_performance_goal": "Convert strong bike fitness into a better run.",
                "recent_session_signatures_by_kind": {
                    "threshold_ride": [
                        "ride_threshold_build",
                        "ride_over_under_control",
                    ]
                },
            },
        )

        self.assertEqual(session["session_name"], "Race-Power Cruise Ride")
        self.assertEqual(session["variant_id"], "ride_race_power_cruise")
        self.assertIn("88-92%", str(session["blocks"]))

    def test_build_quality_run_can_choose_caution_variant(self) -> None:
        session = build_session(
            "quality_run",
            date(2026, 4, 15),
            65,
            lighter_week=False,
            recovery_week=False,
            session_context={
                "planning_date": "2026-04-15",
                "macro_phase": "build",
                "week_classification": "absorb",
                "readiness_status": "yellow",
                "recent_session_signatures_by_kind": {
                    "quality_run": [
                        "run_threshold_repeats",
                        "run_cruise_intervals",
                    ]
                },
            },
        )

        self.assertEqual(session["session_name"], "Progression Tempo Run")
        self.assertEqual(session["variant_id"], "run_progression_tempo")
        self.assertIn("95-98% Pace", str(session["blocks"]))

    def test_reduction_priority_prefers_support_before_long_sessions(self) -> None:
        self.assertLess(
            reduction_priority("swim_club"), reduction_priority("long_ride")
        )
        self.assertLess(reduction_priority("strength"), reduction_priority("long_run"))
        self.assertLess(
            reduction_priority("support_ride"), reduction_priority("long_ride")
        )


if __name__ == "__main__":
    unittest.main()
