from __future__ import annotations

import unittest

from triathlon_agent.intervals import LiveIntervalsAdapter


class IntervalsAdapterTests(unittest.TestCase):
    def test_live_headers_include_custom_user_agent(self) -> None:
        adapter = LiveIntervalsAdapter(
            base_url="https://intervals.icu/api/v1",
            api_key="test-key",
            athlete_id="0",
        )

        headers = adapter._headers()

        self.assertEqual(headers["User-Agent"], "triathlon-agent/1.0")


if __name__ == "__main__":
    unittest.main()
