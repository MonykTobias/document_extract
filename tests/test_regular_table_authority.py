"""Checks for source-corroborated regular-table authority."""

from __future__ import annotations

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


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


def candidate(
    *,
    status: str = "repaired",
    reason: str | None = None,
    source_status: str = "verified",
    include_audit: bool = True,
    grid: dict = GRID,
) -> TableCandidate:
    stats: dict = {"grid": grid}
    if include_audit:
        reconstruction = {
            "status": status,
            "source_content": {
                str(column): {"status": source_status}
                for column in range(grid["num_cols"])
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
    test_existing_renderer_paths_ignore_the_audit_gate()
    test_postprocess_enforces_the_authoritative_grid()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
