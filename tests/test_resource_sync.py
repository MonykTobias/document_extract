"""Checks that editable and packaged resources remain identical."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from document_extract.prompts import DEFAULT_PICTURE_VALUES_PROMPT, load_prompt


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


def check_mirror(editable: Path, packaged: Path) -> None:
    editable_names = {path.name for path in editable.iterdir() if path.is_file()}
    packaged_names = {path.name for path in packaged.iterdir() if path.is_file()}
    check(editable_names == packaged_names, f"{editable.name} names match packaged resources")
    for name in sorted(editable_names):
        check(
            (editable / name).read_bytes() == (packaged / name).read_bytes(),
            f"{editable.name}/{name} matches packaged resource",
        )


def main() -> int:
    resources = ROOT / "src" / "document_extract" / "resources"
    check_mirror(ROOT / "config", resources / "config")
    check_mirror(ROOT / "prompts", resources / "prompts")
    check(
        DEFAULT_PICTURE_VALUES_PROMPT == load_prompt("picture_values.md"),
        "picture values prompt loads from its resource",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
