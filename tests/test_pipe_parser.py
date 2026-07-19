"""Small checks for shared Markdown pipe-table parsing."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from document_extract.markdown.pipe import (
    is_separator_line,
    iter_pipe_blocks,
    iter_table_spans,
    split_row,
)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


def main() -> int:
    lines = ["intro", "| A | B |", "|---|:--:|", "| 1 | 2 |", "outro"]
    check(split_row(lines[1]) == ["A", "B"], "pipe rows split and trim cells")
    check(is_separator_line(lines[2]), "separator rows require a dash or colon")
    check(not is_separator_line("|  |  |"), "blank rows are not separators")
    check(iter_pipe_blocks(lines) == [(1, 4)], "consecutive pipe rows form one block")
    check(
        iter_table_spans(lines) == [(1, 4, [("a", "b"), ("1", "2")])],
        "table spans omit rule rows and normalize data cells",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
