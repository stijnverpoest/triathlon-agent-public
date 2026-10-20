from __future__ import annotations

import base64
import json
from datetime import date, datetime, timedelta
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

    def fetch_activity_intervals(self, activity_id: Any) -> list[dict[str, Any]]:
        raise NotImplementedError

    def update_sport_settings(
        self, updates: list[dict[str, Any]], dry_run: bool = False
    ) -> dict[str, Any]:
        raise NotImplementedError

    def list_events(self, oldest: str, newest: str) -> list[dict[str, Any]]:
        raise NotImplementedError


COACH_EXTERNAL_ID_PREFIX = "coach-"
# Activity comments are scanned for pain / illness / red flags over this window.
ACTIVITY_NOTES_LOOKBACK_DAYS = 14
ACTIVITY_NOTES_MAX_ACTIVITIES = 40


def recent_activity_ids(
    activities: list[dict[str, Any]], planning_date: str, days: int = ACTIVITY_NOTES_LOOKBACK_DAYS
) -> list[str]:
    """Ids of activities in the `days` days up to the planning date (newest first, capped)."""
    try:
        end = date.fromisoformat(str(planning_date)[:10])
    except ValueError:
        return []
    start = end - timedelta(days=days - 1)
    selected = []
    for activity in activities or []:
        raw = str(activity.get("start_date_local") or "")[:10]
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            continue
        if start <= day <= end and activity.get("id") not in (None, ""):
            selected.append((raw, str(activity["id"])))
    selected.sort(reverse=True)
    return [activity_id for _, activity_id in selected[:ACTIVITY_NOTES_MAX_ACTIVITIES]]
PRUNABLE_CATEGORIES = {"WORKOUT", "NOTE"}


def find_orphan_coach_events(
    existing_events: list[dict[str, Any]],
    push_payload: list[dict[str, Any]],
    oldest: str,
    newest: str,
) -> list[dict[str, Any]]:
    """Coach-created planned events in the window that the new payload no longer contains.

    A revised week that renames or drops a session leaves the old workout on the
    calendar next to its replacement. This only *reports* them (events this agent
    created, external_id prefix `coach-`, WORKOUT / NOTE, dated inside
    [oldest, newest]) so the athlete can remove them; nothing is deleted.
    """
    keep_ids = {
        str(item.get("external_id"))
        for item in push_payload
        if item.get("external_id")
    }
    orphans = []
    for event in existing_events:
        external_id = str(event.get("external_id") or "")
        if not external_id.startswith(COACH_EXTERNAL_ID_PREFIX):
            continue
        if external_id in keep_ids:
            continue
        if str(event.get("category") or "").upper() not in PRUNABLE_CATEGORIES:
            continue
        day = str(event.get("start_date_local") or "")[:10]
        if not day or not (oldest <= day <= newest):
            continue
        orphans.append(
            {
                "id": event.get("id"),
                "external_id": external_id,
                "name": event.get("name"),
                "category": event.get("category"),
                "date": day,
            }
        )
    return orphans


def _group_setting_updates(
    updates: list[dict[str, Any]],
) -> dict[Any, dict[str, Any]]:
    grouped: dict[Any, dict[str, Any]] = {}
    for update in updates:
        grouped.setdefault(update["sport_settings_id"], {})[update["field"]] = update[
            "proposed"
        ]
    return grouped


class MockIntervalsAdapter(BaseIntervalsAdapter):
    def __init__(self, fixture_dir: str | Path) -> None:
        self.fixture_dir = Path(fixture_dir)

    def fetch_snapshot(self, planning_date: str) -> dict[str, Any]:
        messages_path = self.fixture_dir / "activity_messages.json"
        return {
            "profile": read_json(self.fixture_dir / "intervals_profile.json"),
            "sport_settings": read_json(self.fixture_dir / "sport_settings.json"),
            "wellness": read_json(self.fixture_dir / "wellness.json"),
            "activities": read_json(self.fixture_dir / "activities.json"),
            "events": read_json(self.fixture_dir / "events.json"),
            # Optional fixture: {"<activity id>": [{"content": "..."}]}.
            "activity_messages": (
                read_json(messages_path) if messages_path.exists() else {}
            ),
            "activity_messages_errors": [],
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

    def fetch_activity_intervals(self, activity_id: Any) -> list[dict[str, Any]]:
        path = self.fixture_dir / "activity_intervals.json"
        if not path.exists():
            return []
        return read_json(path).get(str(activity_id), [])

    def update_sport_settings(
        self, updates: list[dict[str, Any]], dry_run: bool = False
    ) -> dict[str, Any]:
        results = [
            {
                "status": "dry-run" if dry_run else "mock-updated",
                "sport_settings_id": settings_id,
                "payload": payload,
            }
            for settings_id, payload in _group_setting_updates(updates).items()
        ]
        return {"mode": "mock", "dry_run": dry_run, "results": results}

    def list_events(self, oldest: str, newest: str) -> list[dict[str, Any]]:
        events = read_json(self.fixture_dir / "events.json")
        return [
            event
            for event in events
            if oldest <= str(event.get("start_date_local") or "")[:10] <= newest
        ]


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
        # 28 days back keeps a recent A-race (post-race transition) and a long
        # illness note visible to the normalizer.
        oldest_events = (plan_day - timedelta(days=28)).isoformat()

        activities = self._request(
            "GET",
            f"/athlete/{self.athlete_id}/activities",
            {"oldest": oldest_activities, "newest": newest},
        )
        messages, message_errors = self.fetch_activity_messages(
            recent_activity_ids(activities or [], planning_date)
        )
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
            "activities": activities,
            "events": self._request(
                "GET",
                f"/athlete/{self.athlete_id}/events",
                {"oldest": oldest_events, "newest": newest_events},
            ),
            # Activity descriptions arrive with the activities; comments are a
            # separate, best-effort call per recent activity.
            "activity_messages": messages,
            "activity_messages_errors": message_errors,
            "planning_date": planning_date,
            "fetch_timestamp": datetime.now().isoformat(timespec="seconds"),
        }

    def fetch_activity_messages(
        self, activity_ids: list[str]
    ) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
        """Comments per activity. Failures are recorded, never fatal."""
        messages: dict[str, list[dict[str, Any]]] = {}
        errors: list[str] = []
        for activity_id in activity_ids:
            try:
                payload = self._request("GET", f"/activity/{activity_id}/messages")
            except (IntervalsAdapterError, OSError, ValueError) as exc:
                errors.append(f"activity {activity_id} comments: {exc}")
                if len(errors) >= 3 and not messages:
                    errors.append("Stopped fetching activity comments after repeated failures.")
                    break
                continue
            if isinstance(payload, dict):
                payload = payload.get("messages") or []
            if payload:
                messages[str(activity_id)] = [
                    item if isinstance(item, dict) else {"content": str(item)}
                    for item in payload
                ]
        return messages, errors

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

    def fetch_activity_intervals(
        self, activity_id: Any
    ) -> list[dict[str, Any]]:  # pragma: no cover - exercised only in live mode
        payload = self._request("GET", f"/activity/{activity_id}/intervals")
        if isinstance(payload, dict):
            return payload.get("icu_intervals") or []
        return payload or []

    def update_sport_settings(
        self, updates: list[dict[str, Any]], dry_run: bool = False
    ) -> dict[str, Any]:
        grouped = _group_setting_updates(updates)
        if dry_run:
            return {
                "mode": "live",
                "dry_run": True,
                "results": [
                    {"status": "dry-run", "sport_settings_id": key, "payload": value}
                    for key, value in grouped.items()
                ],
            }
        results = []
        for settings_id, payload in grouped.items():
            # One failing sport must not hide the others; record and continue.
            try:
                response = self._request(
                    "PUT",
                    f"/athlete/{self.athlete_id}/sport-settings/{settings_id}",
                    payload=payload,
                )
                results.append(
                    {
                        "status": "updated",
                        "sport_settings_id": settings_id,
                        "payload": payload,
                        "response": response,
                    }
                )
            except IntervalsAdapterError as exc:
                results.append(
                    {
                        "status": "error",
                        "sport_settings_id": settings_id,
                        "payload": payload,
                        "error": str(exc),
                    }
                )
        return {"mode": "live", "dry_run": False, "results": results}


    def list_events(
        self, oldest: str, newest: str
    ) -> list[dict[str, Any]]:  # pragma: no cover - exercised only in live mode
        payload = self._request(
            "GET",
            f"/athlete/{self.athlete_id}/events",
            {"oldest": oldest, "newest": newest},
        )
        return payload or []


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
