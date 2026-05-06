from __future__ import annotations

import unittest

from triathlon_agent.compiler import compile_nutrition_notes, compile_session_to_event


class CompilerTests(unittest.TestCase):
    def test_swim_placeholder_can_compile_with_empty_description(self) -> None:
        session = {
            "date": "2026-03-12",
            "discipline": "Swim",
            "session_name": "Club Swim",
            "objective": "Keep the club swim visible on the Intervals calendar.",
            "duration_min": 60,
            "primary_metric": "notes",
            "blocks": [],
            "intervals_placeholder": True,
            "external_id": "coach-2026-03-12-swim-club",
        }
        event = compile_session_to_event(session, run_id="unused")
        self.assertEqual(event["type"], "Swim")
        self.assertEqual(event["description"], "")
        self.assertEqual(event["external_id"], "coach-2026-03-12-swim-club")

    def test_nutrition_notes_compile_as_note_events(self) -> None:
        nutrition_summary = {
            "daily_notes": [
                {
                    "date": "2026-03-10",
                    "day": "Tuesday",
                    "title": "Nutrition Targets",
                    "body": "Priority: Fuel both sessions well.",
                    "push_to_intervals": True,
                    "external_id": "coach-2026-03-10-note-nutrition",
                },
                {
                    "date": "2026-03-11",
                    "day": "Wednesday",
                    "title": "Nutrition Targets",
                    "body": "",
                    "push_to_intervals": True,
                    "external_id": "coach-2026-03-11-note-nutrition",
                },
            ]
        }

        events = compile_nutrition_notes(nutrition_summary, run_id="unused")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["category"], "NOTE")
        self.assertTrue(events[0]["show_as_note"])
        self.assertEqual(events[0]["external_id"], "coach-2026-03-10-note-nutrition")


if __name__ == "__main__":
    unittest.main()
