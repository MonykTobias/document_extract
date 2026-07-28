"""Checks that sparse VLM table rows keep their source columns.

A model that omits an internal empty cell writes a row that is indistinguishable
from one missing a trailing cell, and right-padding it moves every later value
one column left — a row total lands under the previous column's header. The
source grid is the coordinate authority; these checks pin that.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from document_extract.markdown.formatting import (
    normalize_pipe_tables,
    reconcile_table_columns,
)
from document_extract.models import TableCandidate
from document_extract.refinement import postprocess_markdown, repair_regression_reasons


HEADER = ["", "Due 0", "Due 1-30", "Due 31-60", "Due 61-90", "Due 91+", "Total"]
GRID = {
    "num_cols": 7,
    "header_rows": 1,
    "rows": [
        HEADER,
        ["A. CATEGORIES", "", "", "", "", "", ""],
        ["Invoices concerned", "20", "", "", "", "", "314"],
        ["Amount concerned", "1.0", "0.6", "0.1", "0.6", "5.3", "6.6"],
        ["Excluded invoices", "", "", "", "", "", "2,164"],
        ["Terms", "", "", "60 days", "60 days", "60 days", "60 days"],
    ],
}


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


def table(*body: str) -> str:
    return "| Due 0 | Due 1-30 | Due 31-60 | Due 61-90 | Due 91+ | Total |\n" + "".join(
        f"{row}\n" for row in body
    )


def rows_of(markdown: str) -> list[str]:
    return [line for line in markdown.splitlines() if line.startswith("|")]


def test_sparse_row_keeps_its_source_column() -> None:
    # The model dropped one internal empty, so `314` sits one cell too early.
    markdown = table(
        "| A. CATEGORIES |  |  |  |  |  |",
        "| Invoices concerned | 20 |  |  |  | 314 |",
        "| Amount concerned | 1.0 | 0.6 | 0.1 | 0.6 | 5.3 | 6.6 |",
        "| Excluded invoices |  |  |  |  | 2,164 |",
    )
    fixed, warnings = reconcile_table_columns(markdown, [candidate()])
    check(
        "| Invoices concerned | 20 |  |  |  |  | 314 |" in fixed,
        "an omitted internal empty no longer shifts the row total",
    )
    check(
        "| Excluded invoices |  |  |  |  |  | 2,164 |" in fixed,
        "a row with only a stub and a total keeps the last column",
    )
    check(
        "| Amount concerned | 1.0 | 0.6 | 0.1 | 0.6 | 5.3 | 6.6 |" in fixed,
        "a fully populated row is untouched",
    )
    check(warnings.get("table_rows_realigned") == 2, "realigned rows are counted")
    again, _ = reconcile_table_columns(fixed, [candidate()])
    check(again == fixed, "reconciliation is idempotent")


def test_already_padded_rows_are_repositioned() -> None:
    """A repair pass re-emits full-width rows that keep the earlier shift."""
    padded = normalize_pipe_tables(
        table(
            "| Invoices concerned | 20 |  |  |  | 314 |",
            "| Amount concerned | 1.0 | 0.6 | 0.1 | 0.6 | 5.3 | 6.6 |",
        )
    )
    check(
        "| Invoices concerned | 20 |  |  |  | 314 |  |" in padded,
        "padding is what moves the total one column left",
    )
    fixed, warnings = reconcile_table_columns(padded, [candidate()])
    check(
        "| Invoices concerned | 20 |  |  |  |  | 314 |" in fixed
        and warnings.get("table_rows_realigned") == 1,
        "a full-width row with a moved value is put back on its source column",
    )


def test_span_row_uses_its_source_columns() -> None:
    """The model repeated a spanning value across the wrong column range."""
    markdown = table(
        "| Amount concerned | 1.0 | 0.6 | 0.1 | 0.6 | 5.3 | 6.6 |",
        "| Terms | 60 days | 60 days | 60 days | 60 days | 60 days |",
    )
    fixed, _ = reconcile_table_columns(markdown, [candidate()])
    check(
        "| Terms |  |  | 60 days | 60 days | 60 days | 60 days |" in fixed,
        "a repeated value spans the source columns, not the leading ones",
    )


def test_column_shapes() -> None:
    """Every sparse shape resolves to its source columns at any table width."""
    grid = {
        "num_cols": 5,
        "header_rows": 1,
        "rows": [
            ["", "c1", "c2", "c3", "c4"],
            ["first only", "1", "", "", ""],
            ["last only", "", "", "", "9"],
            ["first and last", "1", "", "", "9"],
            ["one internal", "", "", "3", ""],
            ["leading empties", "", "", "3", "4"],
            ["several gaps", "1", "", "3", ""],
        ],
    }
    cases = [
        ("| first only | 1 |", "| first only | 1 |  |  |  |"),
        ("| last only | 9 |", "| last only |  |  |  | 9 |"),
        ("| first and last | 1 | 9 |", "| first and last | 1 |  |  | 9 |"),
        ("| one internal | 3 |", "| one internal |  |  | 3 |  |"),
        ("| leading empties | 3 | 4 |", "| leading empties |  |  | 3 | 4 |"),
        ("| several gaps | 1 | 3 |", "| several gaps | 1 |  | 3 |  |"),
    ]
    markdown = "| c1 | c2 | c3 | c4 |\n" + "".join(f"{row}\n" for row, _ in cases)
    fixed, warnings = reconcile_table_columns(markdown, [candidate(grid=grid)])
    for _, expected in cases:
        check(expected in fixed, f"{expected.strip('| ')} resolves to source columns")
    check(
        warnings.get("table_rows_realigned") == 5
        and "ambiguous_table_arity" not in warnings,
        "only the rows that actually move are counted, with no ambiguity left",
    )


def test_unmatched_rows_are_reported_not_guessed() -> None:
    markdown = table(
        "| Invoices concerned | 20 |  |  |  | 314 |",
        "| Amount concerned | 1.0 | 0.6 | 0.1 | 0.6 | 5.3 | 6.6 |",
        "| Unknown row |  |  | 7 |",
    )
    fixed, warnings = reconcile_table_columns(markdown, [candidate()])
    check(
        "| Unknown row |  |  | 7 |" in fixed
        and warnings.get("ambiguous_table_arity") == 1,
        "a row with no source match is diagnosed, never repositioned",
    )


def test_tables_without_a_candidate_are_untouched() -> None:
    markdown = table("| Invoices concerned | 20 |  |  |  | 314 |")
    for candidates in ([], [candidate(verified=True)], None):
        fixed, warnings = reconcile_table_columns(markdown, candidates)
        check(
            fixed == markdown and not warnings,
            f"markdown without a usable candidate is unchanged ({candidates!r:.24})",
        )


# One Docling table holding two stacked subtables that repeat their headers.
STACKED = {
    "num_cols": 4,
    "header_rows": 2,
    "rows": [
        ["", "Received", "Received", "Received"],
        ["", "Count", "Amount", "Total"],
        ["Invoices", "20", "1.0", "314"],
        ["", "Issued", "Issued", "Issued"],
        ["", "Count", "Amount", "Total"],
        ["Invoices", "582", "49.6", "1,059"],
    ],
}


def test_stacked_subtables_use_their_own_header() -> None:
    markdown = (
        "| Count | Amount | Total |\n|---|---|---|\n| Invoices | 20 | 1.0 | 314 |\n"
        "\n"
        "| Count | Amount | Total |\n|---|---|---|\n| Invoices | 582 | 49.6 | 1,059 |\n"
    )
    final, warnings = postprocess_markdown(
        markdown, markdown, [], [candidate(grid=STACKED)]
    )
    first, second = final.split("| Invoices | 20 | 1.0 | 314 |")[0], final.split("314")[-1]
    check(warnings.get("span_headers_realigned") == 2, "both headers are restored")
    check(
        "Received<br>Total" in first and "Issued" not in first,
        "the first subtable keeps the leading header segment",
    )
    check(
        "Issued<br>Total" in second and "Received" not in second,
        "the second subtable is headed by its own repeated segment",
    )


def test_postprocess_and_repair_guard() -> None:
    markdown = table(
        "| A. CATEGORIES |  |  |  |  |  |",
        "| Invoices concerned | 20 |  |  |  | 314 |",
        "| Amount concerned | 1.0 | 0.6 | 0.1 | 0.6 | 5.3 | 6.6 |",
        "| Excluded invoices |  |  |  |  | 2,164 |",
    )
    final, warnings = postprocess_markdown(markdown, markdown, [], [candidate()])
    check(
        "| Invoices concerned | 20 |  |  |  |  | 314 |" in final
        and "| Excluded invoices |  |  |  |  |  | 2,164 |" in final,
        "postprocess places every total under the Total column",
    )
    check(
        warnings.get("table_rows_realigned") == 2,
        "postprocess records the realignment for the page audit",
    )

    # A repair that turns a source-backed row into one the grid cannot place
    # loses the only evidence of where its values belong.
    repaired = final.replace("| 20 |  |  |  |  | 314 |", "| 21 |  | 314 |")
    check(
        "ambiguous_table_arity"
        in repair_regression_reasons(final, repaired, None, table_candidates=[candidate()]),
        "a repair that adds an unplaceable sparse row is rejected",
    )
    check(
        not repair_regression_reasons(final, final, None, table_candidates=[candidate()]),
        "an unchanged repair is still accepted",
    )


def main() -> int:
    test_sparse_row_keeps_its_source_column()
    test_already_padded_rows_are_repositioned()
    test_span_row_uses_its_source_columns()
    test_column_shapes()
    test_unmatched_rows_are_reported_not_guessed()
    test_tables_without_a_candidate_are_untouched()
    test_stacked_subtables_use_their_own_header()
    test_postprocess_and_repair_guard()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
