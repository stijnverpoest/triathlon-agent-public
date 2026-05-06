from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .utils import load_env_file


@dataclass
class AppConfig:
    mode: str
    intervals_base_url: str
    intervals_api_key: str | None
    intervals_athlete_id: str
    runs_dir: Path
    prompt_dir: Path
    fixture_dir: Path


def load_config(mode: str | None = None) -> AppConfig:
    load_env_file()
    resolved_mode = mode or os.environ.get("TRIATHLON_AGENT_MODE", "mock")
    return AppConfig(
        mode=resolved_mode,
        intervals_base_url=os.environ.get(
            "INTERVALS_BASE_URL", "https://intervals.icu/api/v1"
        ),
        intervals_api_key=os.environ.get("INTERVALS_API_KEY") or None,
        intervals_athlete_id=os.environ.get("INTERVALS_ATHLETE_ID", "0"),
        runs_dir=Path(os.environ.get("TRIATHLON_AGENT_RUNS_DIR", "runs")),
        prompt_dir=Path("prompts"),
        fixture_dir=Path("fixtures"),
    )
