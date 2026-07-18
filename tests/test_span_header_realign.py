"""Checks for geometry-backed recovery of collapsed table span headers."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from document_extract.markdown.formatting import (
    normalize_pipe_tables,
    realign_collapsed_span_headers,
)
from document_extract.markdown.postprocess import dedupe_span_header_cells
from document_extract.models import TableCandidate
from document_extract.refinement import postprocess_markdown


GRID = {
    "num_cols": 7,
    "header_rows": 1,
    "rows": [
        [
            "PRIORITIES",
            "KPI",
            "Baseline",
            "Baseline",
            "Target",
            "Target",
            "2025 Landing",
        ],
        ["Priority A", "Metric Alpha", "2020", "1", "2030", "2", "3"],
        ["Priority B", "Metric Beta", "2021", "4", "2031", "5", "6"],
    ],
}

COLLAPSED = """| PRIORITIES | KPI | Baseline | Target | 2025 Landing |
|---|---|---|---|---|
| Priority A | Metric Alpha | 2020 | 1 | 2030 | 2 | 3 |
| Priority B | Metric Beta | 2021 | 4 | 2031 | 5 | 6 |
"""

PADDED = """| PRIORITIES | KPI | Baseline | Target | 2025 Landing |  |  |
|---|---|---|---|---|---|---|
| Priority A | Metric Alpha | 2020 | 1 | 2030 | 2 | 3 |
| Priority B | Metric Beta | 2021 | 4 | 2031 | 5 | 6 |
"""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


def candidate(*, verified: bool = False, grid: dict = GRID) -> TableCandidate:
    return TableCandidate(
        candidate_id="docling-1",
        kind="docling_table",
        bbox=None,
        verified=verified,
        stats={"grid": grid},
    )


def test_realigns_collapsed_and_padded_headers() -> None:
    normalized = normalize_pipe_tables(COLLAPSED)
    restored, count = realign_collapsed_span_headers(normalized, [candidate()])
    expected = (
        "| PRIORITIES | KPI | Baseline | Baseline | Target | Target | 2025 Landing |"
    )
    check(count == 1 and expected in restored, "collapsed header restored from grid")
    deduped = dedupe_span_header_cells(restored)
    check(
        "| PRIORITIES | KPI | Baseline |  | Target |  | 2025 Landing |" in deduped,
        "restored header keeps span labels in their first physical columns",
    )

    restored, count = realign_collapsed_span_headers(PADDED, [candidate()])
    check(count == 1 and expected in restored, "right-padded collapsed header restored")


def test_realign_guards() -> None:
    normalized = normalize_pipe_tables(COLLAPSED)
    wrong_width = normalized.replace(
        "| Priority A | Metric Alpha | 2020 | 1 | 2030 | 2 | 3 |",
        "| Priority A | Metric Alpha | 2020 | 1 | 2030 | 2 |",
    )
    for markdown, guarded_candidate, message in [
        (wrong_width, candidate(), "body width mismatch is untouched"),
        (
            normalized.replace("| PRIORITIES |", "| WRONG HEADER |"),
            candidate(),
            "nonmatching header is untouched",
        ),
        (normalized, candidate(verified=True), "verified candidate is untouched"),
        (
            normalized.replace("2020 | 1 | 2030 | 2 | 3", "1990 | 7 | 1995 | 8 | 9")
            .replace("2021 | 4 | 2031 | 5 | 6", "1991 | 10 | 1996 | 11 | 12"),
            candidate(),
            "unrelated body is untouched",
        ),
    ]:
        restored, count = realign_collapsed_span_headers(markdown, [guarded_candidate])
        check(count == 0 and restored == markdown, message)


def test_multirow_header_and_postprocess_order() -> None:
    multirow = {
        **GRID,
        "header_rows": 2,
        "rows": [
            ["", "KPI", "Baseline", "Baseline", "Target", "Target", "2025 Landing"],
            ["PRIORITIES", "KPI", "Year", "Value", "Year", "Value", "2025 Landing"],
            *GRID["rows"][1:],
        ],
    }
    collapsed = COLLAPSED.replace(
        "Baseline | Target | 2025 Landing", "Year | Value | 2025 Landing"
    )
    restored, count = realign_collapsed_span_headers(
        normalize_pipe_tables(collapsed), [candidate(grid=multirow)]
    )
    check(
        count == 1
        and "| PRIORITIES | KPI | Year | Value | Year | Value | 2025 Landing |"
        in restored,
        "multi-row grid uses its deepest header labels",
    )

    final, warnings = postprocess_markdown(COLLAPSED, COLLAPSED, [], [candidate()])
    check(warnings.get("span_headers_realigned") == 1, "postprocess records realignment")
    check(
        "| PRIORITIES | KPI | Baseline |  | Target |  | 2025 Landing |"
        in final,
        "postprocess normalizes, realigns, then deduplicates the header",
    )


def main() -> int:
    test_realigns_collapsed_and_padded_headers()
    test_realign_guards()
    test_multirow_header_and_postprocess_order()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
