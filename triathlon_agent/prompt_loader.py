from __future__ import annotations

from pathlib import Path


def load_prompts(prompt_dir: str | Path) -> dict[str, str]:
    directory = Path(prompt_dir)
    prompts: dict[str, str] = {}
    for path in sorted(directory.glob("*.md")):
        prompts[path.name] = path.read_text(encoding="utf-8")
    return prompts
