"""Load packaged prompt templates with placeholder validation."""

from __future__ import annotations

from pathlib import Path


PROMPT_DIR = Path(__file__).resolve().parent / "resources" / "prompts"


def load_prompt(name: str) -> str:
    path = PROMPT_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Missing packaged prompt: {path}")
    return path.read_text(encoding="utf-8")


def require_placeholders(template: str, *names: str) -> str:
    missing = [name for name in names if "{" + name + "}" not in template]
    if missing:
        raise ValueError(f"Prompt is missing placeholders: {', '.join(missing)}")
    return template

