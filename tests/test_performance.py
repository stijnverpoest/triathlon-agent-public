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


    def _green_snapshot(self) -> dict:
        return {
            "athlete_profile": {"name": "Test Athlete"},
            "weekly_checkin": {
                "pain_or_niggles": "",
                "missed_sessions": [],
                "unexpected_fatigue": "",
                "subjective_readiness": 8,
                "current_motivation": 8,
            },
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
        }

    def _sick_event(self, days_since_end: int, ongoing: bool = False) -> dict:
        return {
            "kind": "sick",
            "name": "Sick",
            "description": "Cold, sore throat",
            "start_date": "2026-03-25",
            "end_date": "2026-03-27",
            "days_since_end": days_since_end,
            "ongoing": ongoing,
        }

    def test_green_snapshot_without_health_events_stays_green(self) -> None:
        result = performance_doctor.run(self._green_snapshot())

        self.assertEqual(result["readiness_status"], "green")
        self.assertEqual(result["health_events"]["status"], "none")

    def test_ongoing_sickness_forces_red_and_reduce(self) -> None:
        snapshot = self._green_snapshot()
        snapshot["recent_health_events"] = [self._sick_event(0, ongoing=True)]

        result = performance_doctor.run(snapshot)

        self.assertEqual(result["readiness_status"], "red")
        self.assertEqual(result["load_recommendation"], "reduce")
        self.assertEqual(result["health_events"]["status"], "acute")
        self.assertIn("recent_illness", result["fatigue_markers"])
        self.assertTrue(any("symptom-free" in item for item in result["constraints"]))
        self.assertTrue(any("Cold, sore throat" in item for item in result["objective_findings"]))

    def test_sickness_five_days_ago_caps_at_yellow_with_gradual_return(self) -> None:
        snapshot = self._green_snapshot()
        snapshot["recent_health_events"] = [self._sick_event(5)]

        result = performance_doctor.run(snapshot)

        self.assertEqual(result["readiness_status"], "yellow")
        self.assertEqual(result["load_recommendation"], "reduce")
        self.assertEqual(result["health_events"]["status"], "returning")
        self.assertTrue(any("70%" in item for item in result["constraints"]))

    def test_sickness_ten_days_ago_holds_load(self) -> None:
        snapshot = self._green_snapshot()
        snapshot["recent_health_events"] = [self._sick_event(10)]

        result = performance_doctor.run(snapshot)

        self.assertEqual(result["readiness_status"], "green")
        self.assertEqual(result["load_recommendation"], "hold")
        self.assertEqual(result["health_events"]["status"], "recent")

    def test_injury_note_adds_affected_area_constraint(self) -> None:
        snapshot = self._green_snapshot()
        snapshot["recent_health_events"] = [
            {
                "kind": "injured",
                "name": "Calf strain",
                "description": "Left calf",
                "start_date": "2026-03-26",
                "end_date": "2026-03-26",
                "days_since_end": 6,
                "ongoing": False,
            }
        ]

        result = performance_doctor.run(snapshot)

        self.assertEqual(result["readiness_status"], "yellow")
        self.assertIn("recent_injury", result["fatigue_markers"])
        self.assertTrue(any("Calf strain" in item for item in result["constraints"]))


if __name__ == "__main__":
    unittest.main()
