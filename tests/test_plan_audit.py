from __future__ import annotations

import unittest

from triathlon_agent.plan_audit import audit_plan


def _quality(day: str, family: str = "run_threshold", minutes: int = 36, progression: bool = True) -> dict:
    session = {
        "day": day,
        "date": "2026-10-13",
        "discipline": "Run" if family.startswith("run") else "Ride",
        "session_name": f"{day} {family}",
        "duration_min": 70,
        "blocks": [
            {"section": "Warmup", "steps": [{"duration": "15m", "target": "70% Pace"}]},
            {
                "section": "Main Set",
                "repeat": 3,
                "steps": [
                    {"duration": f"{minutes // 3}m", "target": "98-102% Pace" if family.startswith("run") else "95-100% FTP"},
                    {"duration": "2m", "target": "65% Pace" if family.startswith("run") else "55-60% FTP"},
                ],
            },
        ],
    }
    if progression:
        session["progression"] = {"family": family, "rung": 3, "time_in_zone_min": minutes}
    return session


def _easy(day: str, minutes: int = 120) -> dict:
    return {
        "day": day,
        "date": "2026-10-14",
        "discipline": "Ride",
        "session_name": f"{day} endurance",
        "duration_min": minutes,
        "blocks": [{"section": "Aerobic Base", "steps": [{"duration": f"{minutes}m", "target": "65-75% FTP"}]}],
    }


def _easy_run(day: str, minutes: int) -> dict:
    return {
        "day": day,
        "date": "2026-10-18",
        "discipline": "Run",
        "session_name": f"{day} easy run",
        "duration_min": minutes,
        "blocks": [{"section": "Easy", "steps": [{"duration": f"{minutes}m", "target": "Z2 HR"}]}],
    }


GREEN = {
    "readiness_status": "green",
    "intensity_allowance": {"max_quality_sessions": 3},
    "underload": {"status": "not_detected"},
}
LOAD_MACRO = {"week_classification": "build", "friel_cycle_type": "load", "macro_phase": "build"}


class PlanAuditTests(unittest.TestCase):
    def _codes(self, sessions, *, performance=GREEN, macro=LOAD_MACRO, snapshot=None) -> set[str]:
        result = audit_plan(snapshot or {}, performance, macro, {"sessions": sessions})
        return {finding["code"] for finding in result["findings"]}

    def test_clean_green_week_passes(self) -> None:
        # 36' of T needs ~360' of weekly running to stay under the Daniels 10% cap.
        sessions = [_quality("Tuesday"), _quality("Thursday", "bike_threshold"), _easy("Saturday", 90), _easy_run("Sunday", 300)]
        result = audit_plan({}, GREEN, LOAD_MACRO, {"sessions": sessions})
        self.assertEqual(result["status"], "pass", result["findings"])
        self.assertEqual(result["quality_session_count"], 2)

    def test_underdosed_green_week(self) -> None:
        self.assertIn("underdosed_green_week", self._codes([_quality("Tuesday"), _easy("Saturday")]))

    def test_underload_not_addressed(self) -> None:
        performance = {**GREEN, "underload": {"status": "detected"}}
        sessions = [_quality("Tuesday"), _quality("Thursday", "bike_threshold"), _easy("Saturday", 90)]
        self.assertIn("underload_not_addressed", self._codes(sessions, performance=performance))

    def test_over_allowance(self) -> None:
        performance = {**GREEN, "intensity_allowance": {"max_quality_sessions": 1}}
        sessions = [_quality("Tuesday"), _quality("Thursday", "bike_threshold")]
        self.assertIn("over_allowance", self._codes(sessions, performance=performance))

    def test_missing_progression_object(self) -> None:
        sessions = [_quality("Tuesday", progression=False), _quality("Thursday", "bike_threshold")]
        self.assertIn("missing_progression", self._codes(sessions))

    def test_no_progression_on_last_completed_dose(self) -> None:
        snapshot = {
            "progression": {
                "last_dose_by_family": {
                    "run_threshold": {"minutes": 36.0, "completed": True, "session_name": "T run"}
                }
            }
        }
        sessions = [_quality("Tuesday", minutes=36), _quality("Thursday", "bike_threshold"), _easy_run("Sunday", 360)]
        self.assertIn("no_progression", self._codes(sessions, snapshot=snapshot))
        snapshot["progression"]["last_dose_by_family"]["run_threshold"]["completed"] = False
        self.assertNotIn("no_progression", self._codes(sessions, snapshot=snapshot))

    def test_consecutive_quality_days(self) -> None:
        sessions = [_quality("Tuesday"), _quality("Wednesday", "bike_threshold")]
        self.assertIn("consecutive_quality_days", self._codes(sessions))

    def test_almost_all_easy(self) -> None:
        sessions = [_quality("Tuesday", minutes=12), _quality("Thursday", "bike_threshold", minutes=12), _easy("Saturday", 600)]
        self.assertIn("almost_all_easy", self._codes(sessions))

    def test_recovery_week_checks(self) -> None:
        macro = {
            "week_classification": "recovery",
            "friel_cycle_type": "recovery",
            "threshold_tests": {"scheduled": [{"discipline": "bike", "protocol": "coggan_20"}]},
        }
        codes = self._codes([_quality("Tuesday"), _easy("Saturday")], macro=macro)
        self.assertIn("missing_threshold_test", codes)
        self.assertIn("quality_in_recovery_week", codes)
        self.assertNotIn("underdosed_green_week", codes)

    def test_taper_skips_dose_checks(self) -> None:
        macro = {"week_classification": "sharpen", "friel_cycle_type": "taper", "macro_phase": "taper"}
        codes = self._codes([_quality("Tuesday"), _easy("Saturday")], macro=macro)
        self.assertNotIn("underdosed_green_week", codes)


if __name__ == "__main__":
    unittest.main()
