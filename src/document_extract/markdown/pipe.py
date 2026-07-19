"""Shared parsing for Markdown pipe-table lines."""

from __future__ import annotations

import re
from collections.abc import Iterable


def split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def is_separator_line(line: str) -> bool:
    stripped = line.strip()
    return (
        stripped.startswith("|")
        and set(stripped) <= set("|-: ")
        and any(ch in "-:" for ch in stripped)
    )


def iter_pipe_blocks(lines: Iterable[str]) -> list[tuple[int, int]]:
    """Return spans of consecutive ``|``-prefixed lines."""
    blocks: list[tuple[int, int]] = []
    start: int | None = None
    for index, line in enumerate([*lines, ""]):
        if line.strip().startswith("|"):
            if start is None:
                start = index
        elif start is not None:
            blocks.append((start, index))
            start = None
    return blocks


def iter_table_spans(
    lines: Iterable[str],
) -> list[tuple[int, int, list[tuple[str, ...]]]]:
    """Return pipe-table spans with whitespace-normalized data rows."""
    spans: list[tuple[int, int, list[tuple[str, ...]]]] = []
    start: int | None = None
    rows: list[tuple[str, ...]] = []
    for index, line in enumerate([*lines, ""]):
        stripped = line.strip()
        if stripped.startswith("|"):
            if start is None:
                start = index
            if not set(stripped) <= set("|-: "):
                rows.append(tuple(re.sub(r"\s+", " ", cell).strip().lower() for cell in split_row(line)))
        elif start is not None:
            spans.append((start, index, rows))
            start, rows = None, []
    return spans


__all__ = ["split_row", "is_separator_line", "iter_pipe_blocks", "iter_table_spans"]
