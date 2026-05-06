from __future__ import annotations

import base64
import json
from datetime import datetime
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .utils import read_json


class IntervalsAdapterError(RuntimeError):
    pass


class BaseIntervalsAdapter:
    def fetch_snapshot(self, planning_date: str) -> dict[str, Any]:
        raise NotImplementedError

    def push_workouts(
        self, workouts: list[dict[str, Any]], dry_run: bool = False
    ) -> dict[str, Any]:
        raise NotImplementedError


class MockIntervalsAdapter(BaseIntervalsAdapter):
    def __init__(self, fixture_dir: str | Path) -> None:
        self.fixture_dir = Path(fixture_dir)

    def fetch_snapshot(self, planning_date: str) -> dict[str, Any]:
        return {
            "profile": read_json(self.fixture_dir / "intervals_profile.json"),
            "sport_settings": read_json(self.fixture_dir / "sport_settings.json"),
            "wellness": read_json(self.fixture_dir / "wellness.json"),
            "activities": read_json(self.fixture_dir / "activities.json"),
            "events": read_json(self.fixture_dir / "events.json"),
            "planning_date": planning_date,
            "fetch_timestamp": datetime.now().isoformat(timespec="seconds"),
        }

    def push_workouts(
        self, workouts: list[dict[str, Any]], dry_run: bool = False
    ) -> dict[str, Any]:
        results = []
        for index, workout in enumerate(workouts, start=1):
            results.append(
                {
                    "status": "dry-run" if dry_run else "mock-pushed",
                    "mock_event_id": 50000 + index,
                    "external_id": workout.get("external_id"),
                    "name": workout.get("name"),
                    "type": workout.get("type"),
                }
            )
        return {"mode": "mock", "dry_run": dry_run, "results": results}


class LiveIntervalsAdapter(BaseIntervalsAdapter):
    def __init__(self, base_url: str, api_key: str, athlete_id: str = "0") -> None:
        if not api_key:
            raise IntervalsAdapterError("INTERVALS_API_KEY is required for live mode.")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.athlete_id = athlete_id

    def _headers(self) -> dict[str, str]:
        token = base64.b64encode(f"API_KEY:{self.api_key}".encode("utf-8")).decode(
            "ascii"
        )
        return {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "User-Agent": "triathlon-agent/1.0",
        }

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        payload: Any = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            encoded = urllib.parse.urlencode(params, doseq=True)
            url = f"{url}?{encoded}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, headers=self._headers(), method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else None
        except (
            urllib.error.HTTPError
        ) as exc:  # pragma: no cover - exercised only in live mode
            raise IntervalsAdapterError(
                f"Intervals API error {exc.code}: {exc.read().decode('utf-8')}"
            ) from exc
        except (
            urllib.error.URLError
        ) as exc:  # pragma: no cover - exercised only in live mode
            raise IntervalsAdapterError(
                f"Intervals connection failed: {exc.reason}"
            ) from exc

    def fetch_snapshot(self, planning_date: str) -> dict[str, Any]:
        params_42_days = {"oldest": planning_date}
        # planning_date is used as newest while oldest is set below where needed
        del params_42_days
        newest = planning_date
        from datetime import timedelta

        from .utils import parse_date

        plan_day = parse_date(planning_date)
        oldest_activities = (plan_day - timedelta(days=84)).isoformat()
        oldest_wellness = (plan_day - timedelta(days=90)).isoformat()
        newest_events = (plan_day + timedelta(days=365)).isoformat()
        oldest_events = (plan_day - timedelta(days=14)).isoformat()

        return {
            "profile": self._request("GET", f"/athlete/{self.athlete_id}/profile"),
            "sport_settings": self._request(
                "GET", f"/athlete/{self.athlete_id}/sport-settings"
            ),
            "wellness": self._request(
                "GET",
                f"/athlete/{self.athlete_id}/wellness",
                {"oldest": oldest_wellness, "newest": newest},
            ),
            "activities": self._request(
                "GET",
                f"/athlete/{self.athlete_id}/activities",
                {"oldest": oldest_activities, "newest": newest},
            ),
            "events": self._request(
                "GET",
                f"/athlete/{self.athlete_id}/events",
                {"oldest": oldest_events, "newest": newest_events},
            ),
            "planning_date": planning_date,
            "fetch_timestamp": datetime.now().isoformat(timespec="seconds"),
        }

    def push_workouts(
        self, workouts: list[dict[str, Any]], dry_run: bool = False
    ) -> dict[str, Any]:
        if dry_run:
            return {"mode": "live", "dry_run": True, "results": workouts}
        response = self._request(
            "POST",
            f"/athlete/{self.athlete_id}/events/bulk",
            {"upsert": "true"},
            workouts,
        )
        return {"mode": "live", "dry_run": False, "results": response}


def build_intervals_adapter(
    mode: str,
    fixture_dir: str | Path,
    base_url: str,
    api_key: str | None,
    athlete_id: str,
) -> BaseIntervalsAdapter:
    if mode == "mock":
        return MockIntervalsAdapter(fixture_dir)
    return LiveIntervalsAdapter(
        base_url=base_url, api_key=api_key or "", athlete_id=athlete_id
    )
