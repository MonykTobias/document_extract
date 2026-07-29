"""Regression checks for display-value KPI panels misclassified as tables."""

from __future__ import annotations

from pathlib import Path


from document_extract.markdown.formatting import missing_verified_table_ids
from document_extract.markdown.postprocess import (
    convert_kpi_pipe_tables_to_lists,
    looks_like_kpi_panel,
)
from document_extract.models import TableCandidate
from document_extract.prompts import DEFAULT_KPI_PANEL_PROMPT, require_placeholders
from document_extract.refinement import postprocess_markdown
from document_extract.tables import build_table_candidates, verify_kpi_list


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


KPI_ROWS = [
    ["+4.5%", "+2.7%", "13.4%"],
    ["LIKE-FOR-LIKE SALES GROWTH", "VOLUME/ MIX", "RECURRING OPERATING MARGIN"],
    ["+4.6%", "\u20ac2.8 bn", "2.25\u20ac"],
    ["RECURRING EPS", "FREE CASH FLOW", "DIVIDEND PER SHARE"],
]
KPI_TABLE = (
    "| +4.5% | +2.7% | 13.4% |\n"
    "|---|---|---|\n"
    "| LIKE-FOR-LIKE SALES GROWTH | VOLUME/ MIX | RECURRING OPERATING MARGIN |\n"
    "| +4.6% | \u20ac2.8 bn | 2.25\u20ac |\n"
    "| RECURRING EPS | FREE CASH FLOW | DIVIDEND PER SHARE |\n"
)
KPI_LINES = [
    "- LIKE-FOR-LIKE SALES GROWTH: +4.5%",
    "- VOLUME/ MIX: +2.7%",
    "- RECURRING OPERATING MARGIN: 13.4%",
    "- RECURRING EPS: +4.6%",
    "- FREE CASH FLOW: \u20ac2.8 bn",
    "- DIVIDEND PER SHARE: 2.25\u20ac",
]


def test_positive_classifier_and_conversion() -> None:
    is_kpi, stats = looks_like_kpi_panel(KPI_ROWS)
    check(is_kpi, "page-5 KPI grid is classified as a panel")
    check(stats["alternating"], "KPI grid has alternating value and label rows")
    converted, count = convert_kpi_pipe_tables_to_lists(KPI_TABLE)
    check(count == 1 and "|" not in converted, "KPI pipe table converts to a list")
    check(converted.strip().splitlines() == KPI_LINES, "all six page-5 KPI pairs are preserved")

    final, warnings = postprocess_markdown(KPI_TABLE, KPI_TABLE, [])
    check(
        all(line in final for line in KPI_LINES),
        "full postprocess preserves paired KPI lines from a raw table",
    )
    check(warnings.get("kpi_tables_converted") == 1, "postprocess records KPI conversion")


def test_icon_variant_and_candidate_metadata() -> None:
    icon_rows = [
        ["growth chart"],
        KPI_ROWS[1],
        KPI_ROWS[0],
        KPI_ROWS[3],
        KPI_ROWS[2],
    ]
    is_kpi, _ = looks_like_kpi_panel(icon_rows)
    check(is_kpi, "KPI grid remains classified with a decorative icon row")
    icon_table = (
        "| growth chart |\n|---|\n"
        "| LIKE-FOR-LIKE SALES GROWTH | VOLUME/ MIX | RECURRING OPERATING MARGIN |\n"
        "| +4.5% | +2.7% | 13.4% |\n"
        "| RECURRING EPS | FREE CASH FLOW | DIVIDEND PER SHARE |\n"
        "| +4.6% | \u20ac2.8 bn | 2.25\u20ac |\n"
    )
    converted, count = convert_kpi_pipe_tables_to_lists(icon_table)
    check(count == 1 and all(line in converted for line in KPI_LINES), "icon variant keeps all KPI pairs")
    check("- growth chart" in converted, "icon text remains as an unpaired line")

    candidates = build_table_candidates(
        cells=[],
        page_size=(1000.0, 800.0),
        picture_records={},
        layout_map={
            "blocks": [
                {"id": "b0001", "type": "table", "bbox": [0.1, 0.1, 0.9, 0.3], "grid_rows": KPI_ROWS}
            ]
        },
    )
    stats = candidates[0].stats or {}
    check(stats.get("kpi_panel") and stats.get("grid_rows") == KPI_ROWS, "candidate stores additive KPI metadata")


def test_negative_tables_are_unchanged() -> None:
    key_figures = [
        ["", "2024", "2025", "Change"],
        ["Sales", "27,376", "27,283", "(0.3)%"],
        ["Operating income", "3,379", "2,940", "(13.0)%"],
        ["Operating margin", "12.3%", "10.8%", "(157) bps"],
        ["Net income", "2,021", "1,825", "(9.7)%"],
        ["EPS", "3.13", "2.82", "(10.1)%"],
        ["Free cash flow", "3,831", "3,779", "(1.3)%"],
        ["Employees", "95,000", "96,000", "1.1%"],
        ["Plants", "180", "182", "1.1%"],
        ["Countries", "120", "121", "0.8%"],
    ]
    zone_table = [
        ["Europe", "9,568", "9,779", "2.3%"],
        ["North America", "6,579", "6,316", "2.0%"],
        ["Latin America", "3,029", "2,792", "6.0%"],
    ]
    for rows, label in ((key_figures, "key figures"), (zone_table, "zone table"), ([['Description', 'Management measures']], "layout header")):
        is_kpi, _ = looks_like_kpi_panel(rows)
        check(not is_kpi, f"{label} is not classified as a KPI panel")
    unchanged, count = convert_kpi_pipe_tables_to_lists(
        "| Region | 2024 | 2025 |\n|---|---|---|\n| Europe | 9,568 | 9,779 |\n"
    )
    check(count == 0 and "| Europe | 9,568 | 9,779 |" in unchanged, "real data table is unchanged")


def test_kpi_verification_and_repair_presence() -> None:
    ok, stats = verify_kpi_list("\n".join(KPI_LINES), [cell for row in KPI_ROWS for cell in row])
    check(ok and stats["list_lines"] == 6, "faithful KPI list verifies")
    ok, stats = verify_kpi_list("\n".join(KPI_LINES + ["- Invented: 99.9%"]), [cell for row in KPI_ROWS for cell in row])
    check(not ok and stats["fail"] == "invented_numbers", "invented KPI number is rejected")
    ok, stats = verify_kpi_list("| Label | Value |\n|---|---|\n", ["42%"])
    check(not ok and stats["fail"] == "pipe_table", "KPI pipe output is rejected")

    candidate = TableCandidate(
        candidate_id="tc001",
        kind="docling_table",
        bbox=[0.1, 0.1, 0.9, 0.3],
        markdown="\n".join(KPI_LINES),
        verified=True,
        stats={"format": "kpi_list"},
    )
    check(not missing_verified_table_ids("\n".join(KPI_LINES[:4]), [candidate]), "60 percent KPI presence keeps repair disabled")
    check(missing_verified_table_ids(KPI_LINES[0], [candidate]) == ["tc001"], "missing KPI lines trigger repair")


def test_prompt_contract() -> None:
    require_placeholders(DEFAULT_KPI_PANEL_PROMPT, "region_blocks")
    check("Do not output markdown tables" in DEFAULT_KPI_PANEL_PROMPT, "KPI prompt loads with list-only contract")


def main() -> int:
    test_positive_classifier_and_conversion()
    test_icon_variant_and_candidate_metadata()
    test_negative_tables_are_unchanged()
    test_kpi_verification_and_repair_presence()
    test_prompt_contract()
    print("test_kpi_panels: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
