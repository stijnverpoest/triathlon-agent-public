from __future__ import annotations

import unittest

from triathlon_agent.agents import performance_doctor


class PerformanceDoctorTests(unittest.TestCase):
    def test_subjective_override_can_downgrade_green_objective_picture(self) -> None:
        snapshot = {
            "athlete_profile": {"name": "Test Athlete", "typical_sleep_hours": 7.5},
            "weekly_checkin": {
                "pain_or_niggles": "",
                "missed_sessions": [],
                "unexpected_fatigue": "Really tired on Friday and Saturday morning.",
                "subjective_readiness": 4,
                "current_motivation": 4,
            },
            "planning_constraints": {},
            "prior_plan_adherence": {
                "status": "available",
                "summary": "Completed 5/6 planned sessions.",
                "adherence_score": 0.9,
                "key_sessions_planned": 2,
                "key_sessions_completed": 2,
            },
            "athlete_memory": {
                "status": "available",
                "summary": "Reviewed 3 prior weeks with average adherence 0.84.",
                "fragile_days": ["Friday"],
                "double_day_profile": {"status": "mixed"},
            },
            "latest_wellness": {"fatigue": 2, "soreness": 1},
            "load_change_pct": 3.0,
            "recent_week_load": 420,
            "prior_week_load": 408,
            "avg_sleep_hours": 7.4,
            "avg_hrv": 67.0,
            "avg_resting_hr": 44.0,
            "weekly_windows": [
                {
                    "activities": [
                        {
                            "start_date_local": "2026-03-02T07:00:00",
                            "icu_training_load": 30,
                        },
                        {
                            "start_date_local": "2026-03-06T07:00:00",
                            "icu_training_load": 70,
                        },
                        {
                            "start_date_local": "2026-03-07T07:00:00",
                            "icu_training_load": 85,
                        },
                    ]
                }
            ],
            "derived_baselines": {
                "hrv": {
                    "baseline": 66.0,
                    "acute": 67.0,
                    "delta": 1.0,
                    "delta_pct": 1.5,
                    "confidence": "high",
                },
                "resting_hr": {
                    "baseline": 44.0,
                    "acute": 44.0,
                    "delta": 0.0,
                    "delta_pct": 0.0,
                    "confidence": "high",
                },
                "sleep_hours": {
                    "baseline": 7.5,
                    "acute": 7.4,
                    "delta": -0.1,
                    "delta_pct": -1.3,
                    "confidence": "high",
                },
            },
        }

        result = performance_doctor.run(snapshot)

        self.assertEqual(result["readiness_status"], "yellow")
        self.assertEqual(result["readiness_confidence"], "high")
        self.assertTrue(result["override_reasons"])
        self.assertIn("late_week_subjective_fatigue", result["fatigue_markers"])

    def test_missing_avg_sleep_does_not_create_false_penalty(self) -> None:
        snapshot = {
            "athlete_profile": {"name": "Test Athlete"},
            "weekly_checkin": {
                "pain_or_niggles": "",
                "missed_sessions": [],
                "unexpected_fatigue": "",
                "subjective_readiness": 7,
                "current_motivation": 7,
            },
            "planning_constraints": {},
            "prior_plan_adherence": {"status": "missing"},
            "athlete_memory": {"status": "missing"},
            "latest_wellness": {"fatigue": None, "soreness": None},
            "load_change_pct": 2.0,
            "recent_week_load": 320,
            "prior_week_load": 310,
            "avg_sleep_hours": None,
            "avg_hrv": None,
            "avg_resting_hr": None,
            "weekly_windows": [{"activities": []}],
            "derived_baselines": {
                "hrv": {"baseline": None, "acute": None, "confidence": "missing"},
                "resting_hr": {
                    "baseline": None,
                    "acute": None,
                    "confidence": "missing",
                },
                "sleep_hours": {
                    "baseline": None,
                    "acute": None,
                    "confidence": "missing",
                },
            },
        }

        result = performance_doctor.run(snapshot)

        self.assertFalse(
            any(
                "Average sleep is only" in item for item in result["objective_findings"]
            )
        )


if __name__ == "__main__":
    unittest.main()
