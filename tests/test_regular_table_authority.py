"""Checks for source-corroborated regular-table authority."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from document_extract.models import TableCandidate
from document_extract.refinement import postprocess_markdown
from document_extract.tables import (
    normalize_table_grid,
    render_deterministic_docling_table,
    render_grid_markdown,
)


GRID = {
    "num_cols": 7,
    "header_rows": 1,
    "cells": [],
    "rows": [
        [
            "PRIORITIES",
            "KPI",
            "Baseline",
            "Baseline",
            "",
            "Target",
            "2025 Landing",
        ],
        [
            "Offer tastier food",
            "Percentage of dairy volumes sold",
            "2022",
            "88.0%",
            "2025",
            ">= 85% (a)",
            "87.8%",
        ],
        [
            "Offer tastier food",
            "Iron deficiency projects",
            "2022",
            "0",
            "2025",
            "5",
            "8",
        ],
    ],
}


SMALL_GRID = {
    "num_cols": 3,
    "header_rows": 1,
    "cells": [],
    "rows": [
        ["PRIORITIES", "KPI", "2025 Landing"],
        ["Offer tastier food", "Percentage of dairy volumes sold", "87.8%"],
        ["Protect the planet", "Regenerative agriculture hectares", "48,000"],
    ],
}

STATUS_CODES = {
    "V": "verified",
    "U": "untrusted",
    "S": "span_preserved",
    "E": "empty",
}


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


def with_column(grid: dict, column: int, values: list[str]) -> dict:
    """Copy of ``grid`` with its body cells in ``column`` replaced."""
    changed = copy.deepcopy(grid)
    body = changed["rows"][changed["header_rows"]:]
    for row, value in zip(body, values):
        row[column] = value
    return changed


def candidate(
    *,
    status: str = "repaired",
    reason: str | None = None,
    source_status: str = "verified",
    source_statuses: str | None = None,
    include_audit: bool = True,
    grid: dict = GRID,
    grid_raw: dict | None = None,
) -> TableCandidate:
    stats: dict = {"grid": grid}
    if grid_raw is not None:
        stats["grid_raw"] = grid_raw
    if include_audit:
        columns = (
            [STATUS_CODES[code] for code in source_statuses.split()]
            if source_statuses
            else [source_status] * grid["num_cols"]
        )
        reconstruction = {
            "status": status,
            "source_content": {
                str(column): {"status": value} for column, value in enumerate(columns)
            },
        }
        if reason:
            reconstruction["reason"] = reason
        stats["reconstruction"] = reconstruction
    return TableCandidate(
        candidate_id="tc001",
        kind="docling_table",
        bbox=[0.0, 0.0, 1.0, 1.0],
        stats=stats,
    )


def render(table: TableCandidate) -> bool:
    return render_deterministic_docling_table(table, {}, (1.0, 1.0))


def test_corroborated_regular_tables_render() -> None:
    expected = render_grid_markdown(normalize_table_grid(GRID))
    repaired = candidate()
    check(render(repaired), "repaired all-source-verified grid renders")
    check(repaired.verified and repaired.markdown == expected, "all seven columns render verbatim")

    unchanged = candidate(status="unchanged", reason="raw_matches_rules")
    check(render(unchanged), "unchanged raw-matches-rules grid renders")

    empty_column = candidate()
    empty_column.stats["reconstruction"]["source_content"]["4"]["status"] = "empty"
    check(render(empty_column), "empty source column remains eligible")


def test_uncorroborated_regular_tables_stay_deferred() -> None:
    missing_source_content = candidate()
    missing_source_content.stats["reconstruction"].pop("source_content")
    cases = [
        (candidate(status="unchanged", reason="no_geometry"), "no-geometry grid stays deferred"),
        (candidate(status="abstained"), "abstained grid stays deferred"),
        (candidate(source_status="untrusted"), "untrusted source column stays deferred"),
        (candidate(include_audit=False), "old checkpoint without audit stays deferred"),
        (missing_source_content, "audit without source columns stays deferred"),
    ]
    for table, message in cases:
        check(not render(table) and not table.verified, message)


def test_rowwise_unchanged_untrusted_columns_render() -> None:
    # Columns 1, 3 and 6 are untrusted but survived the repair row for row; the
    # repair only moved column 0's priority labels onto their logical rows.
    mixed = candidate(
        source_statuses="V U V U V V U",
        grid_raw=with_column(GRID, 0, ["", "Offer tastier food"]),
    )
    check(render(mixed), "repair with rowwise-unchanged untrusted columns renders")
    check(
        mixed.markdown == render_grid_markdown(normalize_table_grid(GRID)),
        "the accepted grid, not grid_raw, is rendered",
    )

    unchanged = candidate(
        status="unchanged",
        reason="raw_matches_rules",
        source_statuses="V U V",
        grid=SMALL_GRID,
    )
    check(render(unchanged), "unchanged mixed-trust grid renders without grid_raw")


def test_moved_untrusted_cells_stay_deferred() -> None:
    # Same flattened tokens in the untrusted column, different rows.
    moved = candidate(
        source_statuses="V U V",
        grid=SMALL_GRID,
        grid_raw=with_column(SMALL_GRID, 1, ["", "Percentage of dairy volumes sold"]),
    )
    p170 = candidate(
        source_statuses="U V V V V V V",
        grid_raw=with_column(GRID, 0, ["Equipandempower", ""]),
    )
    p175 = candidate(
        source_statuses="U S S V V V V",
        grid_raw=with_column(GRID, 0, ["", "Offer tastier food"]),
    )
    no_evidence = candidate(source_statuses="U U U", grid=SMALL_GRID, grid_raw=SMALL_GRID)
    span_only_evidence = candidate(
        source_statuses="U S S", grid=SMALL_GRID, grid_raw=SMALL_GRID
    )
    partial_audit = candidate(source_statuses="V U V", grid=SMALL_GRID, grid_raw=SMALL_GRID)
    partial_audit.stats["reconstruction"]["source_content"].pop("2")
    missing_raw = candidate(source_statuses="V U V", grid=SMALL_GRID)
    cases = [
        (moved, "rearranged untrusted column stays deferred"),
        (p170, "p170 shape stays deferred"),
        (p175, "p175 shape stays deferred"),
        (no_evidence, "all-untrusted grid stays deferred"),
        (span_only_evidence, "span_preserved alone is not corroboration"),
        (partial_audit, "partial source_content audit stays deferred"),
        (missing_raw, "repaired untrusted column without grid_raw stays deferred"),
        (
            candidate(status="abstained", source_statuses="V U V", grid=SMALL_GRID),
            "abstained mixed-trust grid stays deferred",
        ),
        (
            candidate(
                status="unchanged",
                reason="no_rule_bands",
                source_statuses="V U V",
                grid=SMALL_GRID,
            ),
            "unchanged/no_rule_bands mixed-trust grid stays deferred",
        ),
    ]
    for table, message in cases:
        check(not render(table) and not table.verified, message)


def test_span_preserved_columns_stay_deferred() -> None:
    # A horizontal span made source alignment ambiguous, so the column is raw
    # Docling text (p174's "m3/t", p168's "CO2e") rather than source-verified.
    six_col = {**GRID, "num_cols": 6, "rows": [row[:6] for row in GRID["rows"]]}
    cases = [
        (
            candidate(source_statuses="S S S", grid=SMALL_GRID, grid_raw=SMALL_GRID),
            "all-span audit stays deferred",
        ),
        (
            candidate(source_statuses="V S S", grid=SMALL_GRID, grid_raw=SMALL_GRID),
            "verified plus span audit stays deferred",
        ),
        (
            candidate(
                source_statuses="V V V V S S V",
                grid_raw=with_column(GRID, 0, ["", "Offer tastier food"]),
            ),
            "p168 shape stays deferred",
        ),
        (
            candidate(
                source_statuses="V V V V S S",
                grid=six_col,
                grid_raw=with_column(six_col, 0, ["", "Offer tastier food"]),
            ),
            "p174 shape stays deferred",
        ),
    ]
    for table, message in cases:
        check(not render(table) and not table.verified, message)

    p170 = candidate(
        source_statuses="V V V V V V V",
        grid_raw=with_column(GRID, 0, ["", "Offer tastier food"]),
    )
    check(render(p170) and p170.verified, "p170 keeps deterministic authority")
    p172 = candidate(
        source_statuses="V V V V V V",
        grid=six_col,
        grid_raw=with_column(six_col, 0, ["", "Offer tastier food"]),
    )
    check(render(p172) and p172.verified, "p172 keeps deterministic authority")


def test_existing_renderer_paths_ignore_the_audit_gate() -> None:
    title_detail = {
        "num_cols": 3,
        "header_rows": 1,
        "cells": [],
        "rows": [
            ["Topic", "Type", "Scope"],
            ["Nutrition", "", ""],
            ["Supports healthy diets.", "Positive impact", "Value chain"],
            ["Water", "", ""],
            ["Protects watersheds.", "Positive impact", "Value chain"],
        ],
    }
    bulleted = {
        "num_cols": 3,
        "header_rows": 1,
        "cells": [],
        "rows": [
            ["Topic", "Actions", "Scope"],
            ["Nutrition", "• Improve access • Reduce waste", "Value chain"],
            ["Water", "• Restore rivers • Protect wetlands", "Value chain"],
        ],
    }
    visual_values = candidate(include_audit=False)
    visual_values.stats["visual_values_inserted"] = True
    for table, message in [
        (candidate(include_audit=False, grid=title_detail), "title-detail table still renders"),
        (candidate(include_audit=False, grid=bulleted), "bulleted regular table still renders"),
        (visual_values, "visual-value regular table still renders"),
    ]:
        check(render(table) and table.verified, message)


def test_postprocess_enforces_the_authoritative_grid() -> None:
    table = candidate()
    check(render(table), "corroborated candidate becomes authoritative")
    lossy_vlm = """| PRIORITIES | KPI | Baseline | Target | 2025 Landing |
|---|---|---|---|---|
| Offer tastier food | Percentage of dairy volumes sold | 2022 | >= 85% (a) | 87.8% |
| Iron deficiency projects | 2022 | 0 | 5 | 8 |
"""
    final, warnings = postprocess_markdown(lossy_vlm, lossy_vlm, [], [table])
    check(warnings.get("deterministic_tables_enforced") == 1, "grid authority is reported")
    check(
        (
            "| Offer tastier food | Percentage of dairy volumes sold | 2022 | 88.0% "
            "| 2025 | >= 85% (a) | 87.8% |"
        )
        in final,
        "postprocess restores every dropped source column",
    )
    check(
        "| Offer tastier food | Iron deficiency projects | 2022 | 0 | 2025 | 5 | 8 |"
        in final,
        "postprocess restores the row-spanning priority cell",
    )


def main() -> int:
    test_corroborated_regular_tables_render()
    test_uncorroborated_regular_tables_stay_deferred()
    test_rowwise_unchanged_untrusted_columns_render()
    test_moved_untrusted_cells_stay_deferred()
    test_span_preserved_columns_stay_deferred()
    test_existing_renderer_paths_ignore_the_audit_gate()
    test_postprocess_enforces_the_authoritative_grid()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
