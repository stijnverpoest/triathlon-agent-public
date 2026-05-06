from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


def load_env_file(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, payload: Any) -> None:
    Path(path).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def write_text(path: str | Path, content: str) -> None:
    Path(path).write_text(content, encoding="utf-8")


def ensure_dir(path: str | Path) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def parse_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def parse_datetime(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def iso_date(value: date | datetime) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    return value.isoformat()


def start_of_planned_week(planning_date: str) -> date:
    current = parse_date(planning_date)
    return current + timedelta(days=(7 - current.weekday()) % 7)


def daterange(start_day: date, count: int) -> list[date]:
    return [start_day + timedelta(days=index) for index in range(count)]


def slugify(value: str) -> str:
    clean = []
    for char in value.lower():
        if char.isalnum():
            clean.append(char)
        elif clean and clean[-1] != "-":
            clean.append("-")
    result = "".join(clean).strip("-")
    return result or "item"


def day_name(day_value: date) -> str:
    return day_value.strftime("%A")


def minutes_to_hhmm(minutes: int) -> str:
    hours, rem = divmod(int(minutes), 60)
    return f"{hours}h {rem}m" if hours else f"{rem}m"


def sum_minutes(items: list[dict[str, Any]], field: str = "moving_time") -> int:
    total = 0
    for item in items:
        value = item.get(field, 0)
        try:
            total += int(float(value or 0))
        except (TypeError, ValueError):
            continue
    return total


def chunk_weekly(
    start_day: date, activities: list[dict[str, Any]], weeks: int = 4
) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    for offset in range(weeks):
        week_end = start_day - timedelta(days=7 * offset)
        week_start = week_end - timedelta(days=6)
        selected = []
        for activity in activities:
            activity_day = parse_date(activity["start_date_local"])
            if week_start <= activity_day <= week_end:
                selected.append(activity)
        windows.append(
            {
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "activities": selected,
            }
        )
    windows.reverse()
    return windows
