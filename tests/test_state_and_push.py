from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from triathlon_agent.athlete_state import (
    load_athlete_state,
    update_athlete_state_from_plan,
)
from triathlon_agent.contracts import validate_agent_output, validate_final_plan
from triathlon_agent.intervals import find_orphan_coach_events
from triathlon_agent.race_selection import (
    distance_bucket,
    last_completed_a_race,
    race_bucket,
    select_priority_race,
    taper_days,
    transition_days,
)
from triathlon_agent.utils import write_json


def _plan(dates: list[str], block_week: int, phase: str = "build") -> dict:
    return {
        "readiness_status": "green",
        "macro_cycle_decision": {
            "phase": phase,
            "classification": "load",
            "friel_block_week": block_week,
            "friel_cycle_type": "load",
        },
        "race_notes": {
            "priority_race": {
                "name": "Hill Trail",
                "date": "2027-05-08",
                "priority": "A",
                "distance": "Ultra Trail",
                "days_to_race": 200,
            }
        },
        "sessions": [
            {"date": day, "duration_min": 60, "estimated_load": 50} for day in dates
        ],
    }


class RaceSelectionTests(unittest.TestCase):
    def test_buckets_cover_trail_ultra_and_road(self) -> None:
        self.assertEqual(distance_bucket("Half Marathon"), "half_marathon")
        self.assertEqual(distance_bucket("Coastal Half"), "70.3")
        self.assertEqual(distance_bucket("Ironman Frankfurt"), "ironman")
        self.assertEqual(distance_bucket("City 10k"), "10k")
        self.assertEqual(distance_bucket("UTMB OCC ultra trail 55k"), "ultra_trail")
        self.assertEqual(
            race_bucket({"distance": "TrailRun", "name": "Houffa Trail 30k"}), "trail"
        )
        self.assertEqual(
            race_bucket(
                {"distance": "Run", "name": "Hill Trail", "notes": "50 km, 1420 m D+"}
            ),
            "ultra_trail",
        )

    def test_taper_and_transition_scale_with_distance(self) -> None:
        ultra = {"distance": "Ultra Trail 50k"}
        ten_k = {"distance": "10k"}
        self.assertGreater(taper_days(ultra), taper_days(ten_k))
        self.assertGreater(transition_days(ultra), transition_days(ten_k))

    def test_a_race_beats_b_race_matching_profile_target(self) -> None:
        snapshot = {
            "athlete_profile": {"target_race_distance": "Ironman 70.3"},
            "races": [
                {"name": "Local 70.3", "priority": "B", "days_to_race": 20, "distance": "70.3"},
                {"name": "Hill Trail", "priority": "A", "days_to_race": 200, "distance": "Ultra Trail"},
            ],
        }
        self.assertEqual(select_priority_race(snapshot)["name"], "Hill Trail")

    def test_last_completed_a_race_feeds_transition(self) -> None:
        snapshot = {
            "races": [
                {"name": "City Half Marathon", "priority": "A", "days_to_race": -2},
                {"name": "Hill Trail", "priority": "A", "days_to_race": 200},
            ]
        }
        self.assertEqual(
            last_completed_a_race(snapshot)["name"], "City Half Marathon"
        )


class AthleteStateTests(unittest.TestCase):
    def test_reapproving_a_week_replaces_its_history_entry(self) -> None:
        state = {"active_macro_cycle": {}, "history": []}
        plan = _plan(["2026-10-13", "2026-10-15"], block_week=1)
        state = update_athlete_state_from_plan(state, plan, "runs/a")
        state = update_athlete_state_from_plan(state, plan, "runs/b")

        self.assertEqual(len(state["history"]), 1)
        self.assertEqual(state["history"][0]["week_start"], "2026-10-12")
        self.assertEqual(state["history"][0]["source_run"], "runs/b")
        self.assertEqual(state["history"][0]["planned_minutes"], 120)

    def test_block_start_date_is_kept_inside_a_block(self) -> None:
        state = {"active_macro_cycle": {}, "history": []}
        state = update_athlete_state_from_plan(
            state, _plan(["2026-10-12"], block_week=1), "runs/w1"
        )
        state = update_athlete_state_from_plan(
            state, _plan(["2026-10-19"], block_week=2), "runs/w2"
        )
        self.assertEqual(state["active_macro_cycle"]["block_start_date"], "2026-10-12")

        state = update_athlete_state_from_plan(
            state, _plan(["2026-11-09"], block_week=1), "runs/w5"
        )
        self.assertEqual(state["active_macro_cycle"]["block_start_date"], "2026-11-09")

    def test_persisted_priority_race_drops_stale_countdown(self) -> None:
        state = update_athlete_state_from_plan(
            {"active_macro_cycle": {}, "history": []},
            _plan(["2026-10-12"], block_week=1),
            "runs/w1",
        )
        self.assertNotIn("days_to_race", state["last_planned_priority_race"])

    def test_loading_legacy_state_dedupes_weeks(self) -> None:
        legacy = {
            "active_macro_cycle": {
                "phase": "build",
                "priority_race": {"name": "Old", "date": "2026-10-18", "days_to_race": 69},
            },
            "history": [
                {"week_start": "2026-08-25", "classification": "recovery"},
                {"week_start": "2026-08-26", "classification": "recovery"},
                {"week_start": "2026-09-01", "classification": "load"},
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "athlete_state.json"
            write_json(path, legacy)
            state = load_athlete_state(path)

        self.assertEqual(
            [item["week_start"] for item in state["history"]],
            ["2026-08-24", "2026-08-31"],
        )
        self.assertNotIn("days_to_race", state["last_planned_priority_race"])
        self.assertNotIn("priority_race", state["active_macro_cycle"])


class ContractEnumTests(unittest.TestCase):
    def test_amber_is_rejected_with_a_hint(self) -> None:
        payload = {
            key: []
            for key in (
                "objective_findings",
                "subjective_findings",
                "constraints",
                "missing_data",
            )
        }
        payload.update(
            {
                "agent": "performance_doctor",
                "readiness_status": "amber",
                "readiness_score": 50,
                "main_risk": "x",
                "load_recommendation": "increase_gradually",
                "baseline_sources": {},
                "underload": {},
                "intensity_allowance": {},
            }
        )
        errors = validate_agent_output("performance_doctor", payload)
        self.assertTrue(any("yellow" in error for error in errors))
        self.assertTrue(any("load_recommendation" in error for error in errors))

    def test_final_plan_phase_must_be_known(self) -> None:
        errors = validate_final_plan(
            {
                "readiness_status": "green",
                "macro_cycle_decision": {
                    "phase": "peak",
                    "classification": "load",
                    "rationale": "x",
                    "friel_block_week": 1,
                    "friel_cycle_type": "load",
                    "phase_focus": "x",
                },
            }
        )
        self.assertTrue(any("macro_cycle_decision.phase" in error for error in errors))


class OrphanEventTests(unittest.TestCase):
    def test_reports_only_superseded_coach_events_in_window(self) -> None:
        existing = [
            {"id": 1, "external_id": "coach-2026-10-14-run-tempo", "category": "WORKOUT", "start_date_local": "2026-10-14T00:00:00", "name": "Tempo"},
            {"id": 2, "external_id": "coach-2026-10-14-run-threshold", "category": "WORKOUT", "start_date_local": "2026-10-14T00:00:00", "name": "Threshold"},
            {"id": 3, "external_id": None, "category": "WORKOUT", "start_date_local": "2026-10-14T00:00:00", "name": "Club swim"},
            {"id": 4, "external_id": "coach-2026-10-18-race", "category": "RACE_A", "start_date_local": "2026-10-18T00:00:00", "name": "Race"},
            {"id": 5, "external_id": "coach-2026-10-09-run-old", "category": "WORKOUT", "start_date_local": "2026-10-09T00:00:00", "name": "Past"},
        ]
        payload = [{"external_id": "coach-2026-10-14-run-threshold", "start_date_local": "2026-10-14T00:00:00"}]

        orphans = find_orphan_coach_events(existing, payload, "2026-10-12", "2026-10-18")

        self.assertEqual([item["id"] for item in orphans], [1])


if __name__ == "__main__":
    unittest.main()
