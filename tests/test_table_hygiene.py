"""Checks for table hygiene: span-header dedupe and banner-row collapse (WP5).

Fixture rows are the exact strings from danoneurdaccessible pages 46/48
(header) and 43 (banner). Run with ``python tests/test_table_hygiene.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from document_extract.markdown.postprocess import (
    collapse_banner_rows,
    dedupe_span_header_cells,
)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


def pipe_counts(markdown: str) -> list[int]:
    return [
        line.count("|")
        for line in markdown.splitlines()
        if line.strip().startswith("|")
    ]


HEADER_TABLE = (
    "|  | Year ended December 31 | Year ended December 31 | Year ended December 31 | Year ended December 31 |\n"
    "|---|---|---|---|---|\n"
    "| (in € millions unless stated otherwise) | 2024 | 2025 | Reported changes | Like-for-like changes (a) |\n"
    "| Sales | 27,376 | 27,283 | (0.3)% | +4.5% |\n"
)
BANNER_TABLE = (
    "| Company | Country | Activity | Year | Method | Share |\n"
    "|---|---|---|---|---|---|\n"
    "| MAIN COMPANIES CONSOLIDATED FOR THE FIRST TIME DURING THE YEAR | MAIN COMPANIES CONSOLIDATED FOR THE FIRST TIME DURING THE YEAR | MAIN COMPANIES CONSOLIDATED FOR THE FIRST TIME DURING THE YEAR | MAIN COMPANIES CONSOLIDATED FOR THE FIRST TIME DURING THE YEAR | MAIN COMPANIES CONSOLIDATED FOR THE FIRST TIME DURING THE YEAR | MAIN COMPANIES CONSOLIDATED FOR THE FIRST TIME DURING THE YEAR |\n"
    "| Functional Formularies | United States | Medical nutrition | 2024 | Full | 100% |\n"
    "| - | - | - | - | - | - |\n"
)


def check_header_dedupe() -> None:
    deduped = dedupe_span_header_cells(HEADER_TABLE)
    check(
        deduped.count("Year ended December 31") == 1,
        "span header label kept exactly once",
    )
    header = deduped.splitlines()[0]
    check(header.count("|") == 6, "header column count preserved")
    check(
        "| (in € millions unless stated otherwise) | 2024 | 2025 |" in deduped,
        "body rows untouched by header dedupe",
    )
    check(
        pipe_counts(deduped) == pipe_counts(HEADER_TABLE),
        "all rows keep their pipe counts",
    )

    distinct = "| A | B | A |\n|---|---|---|\n| 1 | 2 | 3 |\n"
    check(
        dedupe_span_header_cells(distinct) == distinct,
        "non-adjacent repeats in header left alone",
    )


def check_banner_collapse() -> None:
    collapsed = collapse_banner_rows(BANNER_TABLE)
    check(
        collapsed.count("MAIN COMPANIES CONSOLIDATED FOR THE FIRST TIME DURING THE YEAR") == 1,
        "banner value kept exactly once",
    )
    banner_line = collapsed.splitlines()[2]
    check(banner_line.count("|") == 7, "banner row column count preserved")
    check(
        "| - | - | - | - | - | - |" in collapsed,
        "short repeated placeholder row untouched",
    )
    check(
        "| Functional Formularies | United States |" in collapsed,
        "data rows untouched",
    )
    check(
        pipe_counts(collapsed) == pipe_counts(BANNER_TABLE),
        "all rows keep their pipe counts",
    )

    header_only = "| X | X |\n|---|---|\n| a | b |\n"
    check(
        collapse_banner_rows(header_only) == header_only,
        "header rows are not banner-collapsed",
    )


def main() -> int:
    check_header_dedupe()
    check_banner_collapse()
    print("test_table_hygiene: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
