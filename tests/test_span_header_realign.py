"""Checks for geometry-backed recovery of collapsed table span headers."""

from __future__ import annotations

from pathlib import Path


from document_extract.markdown.formatting import (
    normalize_pipe_tables,
    realign_collapsed_span_headers,
)
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


def candidate(
    *, candidate_id: str = "docling-1", verified: bool = False, grid: dict = GRID
) -> TableCandidate:
    return TableCandidate(
        candidate_id=candidate_id,
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


def test_realigns_matching_twin_spans() -> None:
    restored, count = realign_collapsed_span_headers(
        COLLAPSED + "\n" + COLLAPSED, [candidate()]
    )
    expected = (
        "| PRIORITIES | KPI | Baseline | Baseline | Target | Target | 2025 Landing |"
    )
    check(
        count == 2 and restored.count(expected) == 2,
        "matching twin spans are restored",
    )
    _, repeat_count = realign_collapsed_span_headers(restored, [candidate()])
    check(repeat_count == 0, "twin restoration is idempotent")


def test_unused_candidates_are_preferred_before_reuse() -> None:
    grids = [
        {
            "num_cols": 3,
            "header_rows": 1,
            "rows": [["X", "X", "Y"], ["Row", "1", "2"]],
        },
        {
            "num_cols": 3,
            "header_rows": 1,
            "rows": [["X", "Y", "Y"], ["Row", "1", "2"]],
        },
    ]
    collapsed = """| X | Y |
|---|---|
| Row | 1 | 2 |

| X | Y |
|---|---|
| Row | 1 | 2 |
"""
    restored, count = realign_collapsed_span_headers(
        collapsed,
        [
            candidate(candidate_id="a", grid=grids[0]),
            candidate(candidate_id="b", grid=grids[1]),
        ],
    )
    check(
        count == 2
        and "| X | X | Y |\n|---|---|\n| Row | 1 | 2 |\n\n| X | Y | Y |"
        in restored,
        "unused candidate is preferred for the second matching span",
    )


def test_reuses_candidate_when_no_fresh_match_exists() -> None:
    grid = {
        "num_cols": 3,
        "header_rows": 1,
        "rows": [
            ["X", "X", "Y"],
            ["Alpha", "1", "2"],
            ["Beta", "3", "4"],
            ["Gamma", "5", "6"],
            ["Delta", "7", "8"],
        ],
    }
    collapsed = """| X | Y |
|---|---|
| Alpha | 1 | 2 |
| Beta | 3 | 4 |

| X | Y |
|---|---|
| Gamma | 5 | 6 |
| Delta | 7 | 8 |
"""
    restored, count = realign_collapsed_span_headers(collapsed, [candidate(grid=grid)])
    check(
        count == 2 and restored.count("| X | X | Y |") == 2,
        "used candidate is reused when no fresh candidate matches",
    )


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
        and (
            "| PRIORITIES | KPI | Baseline<br>Year | Baseline<br>Value | "
            "Target<br>Year | Target<br>Value | 2025 Landing |"
        )
        in restored,
        "multi-row grid preserves spanning and leaf header labels",
    )

    final, warnings = postprocess_markdown(COLLAPSED, COLLAPSED, [], [candidate()])
    check(warnings.get("span_headers_realigned") == 1, "postprocess records realignment")
    check(
        "| PRIORITIES | KPI | Baseline | Baseline | Target | Target | 2025 Landing |"
        in final,
        "postprocess keeps every restored span label duplicated",
    )


def main() -> int:
    test_realigns_collapsed_and_padded_headers()
    test_realign_guards()
    test_realigns_matching_twin_spans()
    test_unused_candidates_are_preferred_before_reuse()
    test_reuses_candidate_when_no_fresh_match_exists()
    test_multirow_header_and_postprocess_order()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
