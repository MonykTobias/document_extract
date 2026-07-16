"""Checks for sectioned-table splitting (Docling tables with spanning section
rows). Fixtures mirror danoneurdaccessible page 154 (first-cell-only section
rows) and page 43 (banner value repeated across the row).

Run with ``python tests/test_sectioned_tables.py``.
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from document_extract.layout.prompt_map import (
    build_layout_prompt_map,
    layout_map_prompt_json,
)
from document_extract.markdown.formatting import (
    missing_verified_table_ids,
    replace_sectioned_tables,
)
from document_extract.markdown.postprocess import (
    render_sectioned_tables,
    split_sectioned_grid,
)
from document_extract.models import PictureRecord, TableCandidate
from document_extract.refinement import postprocess_markdown
from document_extract.tables import (
    build_table_candidates,
    render_sectioned_docling_table,
    transcribe_table_candidates,
    verified_tables_prompt_block,
)

REAL_PAGE_154 = Path(
    r"C:\Users\Tobia\Documents\Tobi&Anna\gw_detector_v2\outputs"
    r"\danoneurdaccessible\page_0154\docling_raw.md"
)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


P154_GRID = [
    ["", "2021", "2022", "2023", "2024", "2025"],
    ["CAPITAL AT YEAR-END", "", "", "", "", ""],
    ["Share capital (in €)", "171,920,622", "168,959,483", "169,443,282", "169,888,498", "170,348,621"],
    ["Number of shares issued", "687,682,489", "675,837,932", "677,773,128", "679,553,991", "681,394,483"],
    ["OPERATIONS AND EARNINGS FOR THE YEAR", "", "", "", "", ""],
    ["(in € millions)", "", "", "", "", ""],
    ["Sales before tax", "635", "699", "890", "1,030", "1,256"],
    ["Income tax (a)", "47", "45", "76", "104", "89"],
    ["Dividends paid (b)", "1,249", "1,291", "1,360", "1,392", "1,533"],
    ["EARNINGS PER SHARE", "", "", "", "", ""],
    ["(in € per share)", "", "", "", "", ""],
    ["Dividend per share", "1.94", "2.00", "2.10", "2.15", "2.25"],
    ["PERSONNEL COSTS", "", "", "", "", ""],
    ["Average number of employees for the year", "1,008", "1,004", "1,042", "1,153", "1,202"],
    ["Payroll expense (in € millions)", "160", "178", "218", "222", "257"],
]

P43_GRID = [
    ["(in percentage)", "Zone (Country)", "Category", "Transaction date (a)", "2024", "2025"],
    ["MAIN COMPANIES CONSOLIDATED FOR THE FIRST TIME DURING THE YEAR"] * 6,
    ["The Akkermansia Company SA (b)", "Europe (Belgium)", "EDP", "June", "-", "100%"],
    ["Kate Farms (c)", "North America", "Specialized Nutrition", "July", "-", "96%"],
    ["MAIN CONSOLIDATED COMPANIES IN WHICH THE GROUP'S OWNERSHIP INTEREST HAS CHANGED"] * 6,
    ["-", "-", "-", "-", "-", "-"],
    ["MAIN COMPANIES NOLONGER FULLY CONSOLIDATED AS OF DECEMBER 31"] * 6,
    ["Danone Dairy Pars (d)", "Middle East (Iran)", "EDP", "December", "100%", "-"],
]


# Page 145: a nested table — SUBSIDIARIES is a parent category (no data of its
# own) over FRENCH/FOREIGN sub-categories; AFFILIATES is another leaf section.
P145_GRID = [
    ["(in percentage)", "Share capital", "% ownership", "Shares held"],
    ["SUBSIDIARIES (AT LEAST 50% OF THE SHARE CAPITAL HELD BY THE COMPANY)"] * 4,
    ["FRENCH SUBSIDIARIES", "", "", ""],
    ["BLEDINA", "129", "100%", "1,602,357"],
    ["COMPAGNIE GERVAIS DANONE", "9,894", "100%", "370,575,203"],
    ["FOREIGN SUBSIDIARIES", "", "", ""],
    ["DANONE ASIA PTE LTD", "680", "88%", "508,451,086"],
    ["AFFILIATES (AT LEAST 10% TO 50% OF THE SHARE CAPITAL HELD BY THE COMPANY)"] * 4,
    ["DANONE FINANCE INTERNATIONAL", "6,083", "33%", "4,034,154"],
]


def _candidate_from_grid(grid, header_rows=0, candidate_id="tc001"):
    split = split_sectioned_grid(grid, header_rows=header_rows)
    assert split is not None
    return TableCandidate(
        candidate_id=candidate_id,
        kind="docling_table",
        bbox=[0.0, 0.0, 1.0, 1.0],
        markdown=render_sectioned_tables(split),
        verified=True,
        stats={
            "format": "sectioned_table",
            "grid": {"rows": grid},
            "sectioned_split": split,
            "section_titles": split["section_titles"],
            "section_qualifiers": split["section_qualifiers"],
            "section_kinds": split["section_kinds"],
        },
    )


def _heading_levels(markdown, titles_substrings):
    """Map: for each wanted title substring, the heading level found in markdown."""
    levels = {}
    for line in markdown.splitlines():
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if not m:
            continue
        for key in titles_substrings:
            if key in m.group(2):
                levels[key] = len(m.group(1))
    return levels


def check_split_group_header() -> None:
    split = split_sectioned_grid(P145_GRID, header_rows=1)
    check(split is not None, "page-145 nested grid recognized as sectioned")
    check(
        split["section_kinds"] == ["group", "data", "data", "data"],
        "parent category detected as a group header, leaves as data sections",
    )
    check(
        split["section_titles"][1] == "FRENCH SUBSIDIARIES",
        "parent and child are NOT merged into one heading",
    )
    # SUBSIDIARIES (parent) has no table; the three leaves each repeat the header.
    markdown = render_sectioned_tables(split)
    check(markdown.count("| (in percentage) | Share capital |") == 3, "one subtable per leaf section")


def check_group_heading_levels_consistent() -> None:
    # A page-145-style VLM output: subtables placed, but sibling headings at
    # inconsistent levels (the reported bug). Enforcement must make them equal.
    candidate = _candidate_from_grid(P145_GRID, header_rows=1)
    vlm_output = (
        "## Equity interests\n\n"
        "### Equity interests held in portfolio\n\n"
        "#### SUBSIDIARIES (AT LEAST 50% OF THE SHARE CAPITAL HELD BY THE COMPANY)\n\n"
        "##### FRENCH SUBSIDIARIES\n\n"
        "| (in percentage) | Share capital | % ownership | Shares held |\n"
        "|---|---|---|---|\n"
        "| BLEDINA | 129 | 100% | 1,602,357 |\n"
        "| COMPAGNIE GERVAIS DANONE | 9,894 | 100% | 370,575,203 |\n\n"
        "#### FOREIGN SUBSIDIARIES\n\n"
        "| (in percentage) | Share capital | % ownership | Shares held |\n"
        "|---|---|---|---|\n"
        "| DANONE ASIA PTE LTD | 680 | 88% | 508,451,086 |\n\n"
        "#### AFFILIATES (AT LEAST 10% TO 50% OF THE SHARE CAPITAL HELD BY THE COMPANY)\n\n"
        "| (in percentage) | Share capital | % ownership | Shares held |\n"
        "|---|---|---|---|\n"
        "| DANONE FINANCE INTERNATIONAL | 6,083 | 33% | 4,034,154 |\n"
    )
    out, enforced = replace_sectioned_tables(vlm_output, [candidate])
    check(enforced == ["tc001"], "inconsistent-level nested table reported as enforced")
    levels = _heading_levels(out, ["SUBSIDIARIES (AT LEAST 50%", "FRENCH", "FOREIGN", "AFFILIATES"])
    check(levels["SUBSIDIARIES (AT LEAST 50%"] == 4, "parent category one deeper than the ### heading above")
    check(
        levels["FRENCH"] == levels["FOREIGN"] == levels["AFFILIATES"] == 5,
        "all leaf sub-categories share one consistent heading level",
    )
    check("| DANONE ASIA PTE LTD |" in out, "leaf data preserved")


def check_split_p154() -> None:
    split = split_sectioned_grid(P154_GRID, header_rows=0)
    check(split is not None, "page-154 grid recognized as sectioned")
    check(
        split["section_titles"]
        == [
            "CAPITAL AT YEAR-END",
            "OPERATIONS AND EARNINGS FOR THE YEAR",
            "EARNINGS PER SHARE",
            "PERSONNEL COSTS",
        ],
        "four section titles detected in order",
    )
    check(
        split["section_qualifiers"] == ["(in € millions)", "(in € per share)"],
        "consecutive banner rows captured as qualifiers",
    )
    check(split["data_row_count"] == 8, "data rows counted across all sections")


def check_split_p43() -> None:
    split = split_sectioned_grid(P43_GRID, header_rows=1)
    check(split is not None, "page-43 banner grid recognized as sectioned")
    check(len(split["section_titles"]) == 3, "three repeated-value banners become sections")
    rowcounts = [len(section["rows"]) for section in split["sections"]]
    check(rowcounts == [2, 1, 1], "all-'-' placeholder row stays a data row, not a banner")
    # Generality: the same layout must split even when Docling never flagged the
    # header row (header_rows=0) despite a non-empty first header cell.
    unflagged = split_sectioned_grid(P43_GRID, header_rows=0)
    check(unflagged is not None, "page-43 layout splits even with an unflagged, filled-first-cell header")


def check_split_negatives() -> None:
    plain = [
        ["", "2024", "2025"],
        ["Revenue", "10", "11"],
        ["Costs", "4", "5"],
        ["Profit", "6", "6"],
    ]
    check(split_sectioned_grid(plain, header_rows=1) is None, "plain table (no section rows) left alone")

    prose_header = [
        # Row 0 has an over-long cell: it is prose, not a header line.
        ["A fairly long descriptive column label that exceeds the cap", "Another long descriptive label here", "2025"],
        ["SECTION HEADING LABEL", "", ""],
        ["Revenue", "10", "11"],
        ["Costs", "4", "5"],
    ]
    check(
        split_sectioned_grid(prose_header, header_rows=0) is None,
        "row 0 with an over-long cell is not accepted as a header",
    )

    stacked_header = [
        ["", "Year ended December 31", "Year ended December 31"],
        ["", "2024", "2023"],
        ["Revenue", "10", "11"],
        ["Costs", "4", "5"],
    ]
    check(
        split_sectioned_grid(stacked_header, header_rows=2) is None,
        "genuine stacked header (no body section rows) is not split",
    )

    two_col = [
        ["", "2025"],
        ["SECTION HEADING LABEL HERE", ""],
        ["Revenue", "10"],
        ["Costs", "5"],
    ]
    check(split_sectioned_grid(two_col, header_rows=0) is None, "under three columns is not split")

    trailing = [
        ["", "2024", "2025"],
        ["CAPITAL", "", ""],
        ["Revenue", "10", "11"],
        ["EMPTY TRAILING SECTION", "", ""],
    ]
    check(
        split_sectioned_grid(trailing, header_rows=0) is None,
        "a trailing section with no data rows aborts the split",
    )

    all_spanning = [
        ["", "2024", "2025"],
        ["ONE", "", ""],
        ["TWO", "", ""],
        ["THREE", "", ""],
    ]
    check(
        split_sectioned_grid(all_spanning, header_rows=0) is None,
        "grid with no data rows is not split",
    )


def check_render() -> None:
    split = split_sectioned_grid(P154_GRID, header_rows=0)
    markdown = render_sectioned_tables(split)
    check(markdown.count("### ") == 4, "one heading per titled section")
    check(
        "### OPERATIONS AND EARNINGS FOR THE YEAR (in € millions)" in markdown,
        "qualifier appended to the section heading",
    )
    check(markdown.count("| 2021 | 2022 | 2023 | 2024 | 2025 |") == 4, "header repeated in every subtable")
    # Escaping: a literal pipe in a cell must not break the table.
    piped = split_sectioned_grid(
        [
            ["", "2024", "2025"],
            ["SECTION HEADING LABEL", "", ""],
            ["a|b", "1", "2"],
            ["c", "3", "4"],
        ],
        header_rows=0,
    )
    rendered = render_sectioned_tables(piped)
    check("a\\|b" in rendered, "literal pipe in a cell is escaped so it cannot spawn a column")
    check(rendered.splitlines()[2].count("|") == rendered.splitlines()[3].count("|"),
          "escaped cell keeps the row's column count aligned with the header")


def check_sectioned_body_bullets_and_raw_twin_anchor() -> None:
    grid = [
        ["Topic", "Details", "ESRS coverage"],
        ["SECTIONED BULLETS", "", ""],
        ["Single", "■ one item", ""],
        ["Multiple", "■ first ■ second", ""],
        ["Lead", "lead ■ first ■ second", ""],
        ["Literal", "literal ■ marker", ""],
        ["Weak", "12·000", ""],
    ]
    split = split_sectioned_grid(grid, header_rows=1)
    rendered = render_sectioned_tables(split)
    check("| Topic | Details | ESRS coverage |" in rendered, "sectioned header remains unmodified")
    check("### SECTIONED BULLETS" in rendered, "section title remains an unmodified heading")
    check("| Single | - one item |" in rendered, "leading single bullet becomes one list item")
    check("| Multiple | - first<br>- second |" in rendered, "multiple bullets become cell-local list items")
    check("| Lead | lead<br>- first<br>- second |" in rendered, "leading prose survives before cell bullets")
    check("literal ■ marker" in rendered, "lone mid-text black square remains literal")
    check("12·000" in rendered, "mid-text weak middle dot remains literal")

    candidate = _candidate_from_grid(grid, header_rows=1, candidate_id="tc-bullets")
    raw_rows = "\n".join("| " + " | ".join(row) + " |" for row in grid)
    raw_lines = raw_rows.splitlines()
    raw_lines.insert(1, "|---|---|---|")
    out, enforced = replace_sectioned_tables("\n".join(raw_lines) + "\n", [candidate])
    check(enforced == ["tc-bullets"], "raw bullet-bearing sectioned twin still anchors and splices")
    check("- first<br>- second" in out, "spliced sectioned twin uses normalized cell bullets")


def _sectioned_structured_grid(rows: list[list[str]]) -> dict[str, object]:
    cells: list[dict[str, object]] = []
    row_height = 1.0 / len(rows)
    num_cols = max(len(row) for row in rows)
    for row_index, row in enumerate(rows):
        for column_index, text in enumerate(row):
            cells.append(
                {
                    "r": row_index,
                    "c": column_index,
                    "text": text,
                    "bbox": {
                        "l": column_index / num_cols,
                        "t": row_index * row_height,
                        "r": (column_index + 1) / num_cols,
                        "b": (row_index + 1) * row_height,
                        "origin": "TOPLEFT",
                    },
                    "column_header": row_index == 0,
                }
            )
    return {"rows": rows, "num_cols": num_cols, "header_rows": 1, "cells": cells}


def _sectioned_symbol(index: int, rect: list[float], value: str) -> PictureRecord:
    return PictureRecord(
        page=1,
        index=index,
        placeholder=f"{{{{DOC_IMAGE_p0001_i{index:03d}}}}}",
        rel_path=f"images/picture_p0001_i{index:03d}.png",
        abs_path=None,
        bbox={"l": rect[0], "t": rect[1], "r": rect[2], "b": rect[3], "origin": "TOPLEFT"},
        area_ratio=0.001,
        classification="",
        caption="",
        summary_type="symbol",
        summary=value,
    )


def check_sectioned_symbol_placement_by_source_row() -> None:
    rows = [
        ["Topic", "Description", "ESRS coverage"],
        ["SECTION A", "", ""],
        ["First A", "Description A", ""],
        ["Second A", "Description B", ""],
        ["SECTION B", "", ""],
        ["First B", "Description C", ""],
        ["Second B", "Description D", ""],
    ]
    structured = _sectioned_structured_grid(rows)
    split = split_sectioned_grid(rows, header_rows=1)
    candidate = TableCandidate(
        candidate_id="tc-symbols",
        kind="docling_table",
        bbox=[0.0, 0.0, 1.0, 1.0],
        markdown=render_sectioned_tables(split),
        verified=True,
        stats={
            "format": "sectioned_table",
            "grid": structured,
            "sectioned_split": split,
            "section_titles": split["section_titles"],
            "section_qualifiers": split["section_qualifiers"],
            "section_kinds": split["section_kinds"],
        },
    )
    first = _sectioned_symbol(1, [0.82, 0.30, 0.90, 0.38], "E1")
    third = _sectioned_symbol(2, [0.82, 0.73, 0.90, 0.81], "S2")
    off_band = _sectioned_symbol(3, [0.82, 0.03, 0.90, 0.07], "G3")
    rendered = render_sectioned_docling_table(
        candidate, {1: first, 2: third, 3: off_band}, (1.0, 1.0)
    )
    check(rendered, "sectioned candidate rerenders after symbols are summarized")
    check("| First A | Description A | E1 |" in candidate.markdown, "symbol lands in the first section row")
    check("| First B | Description C | S2 |" in candidate.markdown, "symbol lands in the later section row")
    check("G3" not in candidate.markdown, "off-band symbol is never sequenced into a section row")
    check(
        (candidate.stats or {}).get("symbols_unplaced_geometry") == [3],
        "off-band symbol is retained as a geometry warning",
    )
    _, warnings = postprocess_markdown("", candidate.markdown, [first, third, off_band], [candidate])
    check(
        warnings.get("table_symbols_unplaced") == [off_band.placeholder],
        "only the off-band sectioned symbol reaches the page warning",
    )


def check_sectioned_cross_record_rowspan_bbox_is_ignored() -> None:
    """The local-rectangle rule must cover sectioned-table placement too."""
    rows = [
        ["Topic", "Description", "ESRS coverage"],
        ["SECTION A", "", ""],
        ["First A", "Description A", ""],
        ["Second A", "Description B", ""],
        ["SECTION B", "", ""],
        ["First B", "Description C", ""],
        ["Second B", "Description D", ""],
    ]
    split = split_sectioned_grid(rows, header_rows=1)
    structured = _sectioned_structured_grid(rows)
    shared_bbox = {"l": 0.0, "t": 2 / 7, "r": 1 / 3, "b": 6 / 7, "origin": "TOPLEFT"}
    for cell in structured["cells"]:
        if cell["c"] == 0 and cell["r"] in {2, 5}:
            cell["bbox"] = dict(shared_bbox)
    candidate = TableCandidate(
        candidate_id="tc-cross-section",
        kind="docling_table",
        bbox=[0.0, 0.0, 1.0, 1.0],
        markdown=render_sectioned_tables(split),
        verified=True,
        stats={"format": "sectioned_table", "grid": structured, "sectioned_split": split},
    )
    first = _sectioned_symbol(1, [0.82, 0.31, 0.90, 0.35], "E1")
    second = _sectioned_symbol(2, [0.82, 0.74, 0.90, 0.78], "S2")
    off_band = _sectioned_symbol(3, [0.82, 0.03, 0.90, 0.07], "G3")
    check(
        render_sectioned_docling_table(candidate, {1: first, 2: second, 3: off_band}, (1.0, 1.0)),
        "sectioned cross-record fixture rerenders",
    )
    check("| First A | Description A | E1 |" in candidate.markdown, "first cross-section row receives E1")
    check("| First B | Description C | S2 |" in candidate.markdown, "second cross-section row receives S2")
    check((candidate.stats or {}).get("symbols_unplaced_geometry") == [3], "only the real off-band symbol remains unplaced")
    render_sectioned_docling_table(candidate, {1: first, 2: second}, (1.0, 1.0))
    check(
        "symbols_unplaced_geometry" not in (candidate.stats or {}),
        "a successful rerender clears stale geometry-deficit stats",
    )

    candidate.stats["symbols_placed_geometry"] = []
    candidate.stats["symbols_unplaced_geometry"] = [1]
    degraded = candidate.markdown.replace("E1", "")
    out, enforced = replace_sectioned_tables(degraded, [candidate])
    check(enforced == [] and out == degraded, "incomplete sectioned candidate cannot overwrite a VLM/raw table")
    check(verified_tables_prompt_block([candidate]) == "(none)", "incomplete sectioned candidate stays out of the prompt block")
    check(missing_verified_table_ids("# Page\n", [candidate]) == [], "incomplete sectioned candidate is not required as verified")


def _sectioned_table_item(grid, *, flag_header=False, bbox=(30, 30, 900, 760)):
    def cell(text, header):
        return SimpleNamespace(text=text, column_header=header)

    grid_cells = [
        [cell(text, header=(flag_header and row_index == 0)) for text in row]
        for row_index, row in enumerate(grid)
    ]
    bbox_obj = SimpleNamespace(l=bbox[0], t=bbox[1], r=bbox[2], b=bbox[3], coord_origin="TOPLEFT")
    return SimpleNamespace(
        label="table",
        text="",
        caption="",
        data=SimpleNamespace(grid=grid_cells),
        prov=[SimpleNamespace(page_no=1, bbox=bbox_obj)],
    )


def check_prompt_map_wiring() -> None:
    item = _sectioned_table_item(P154_GRID)
    layout_map = build_layout_prompt_map([item], (1000.0, 800.0), {})
    block = layout_map["blocks"][0]
    check(block["type"] == "table", "table block present")
    check("_sectioned_table" in block, "sectioned split attached to the table block")
    check(block["_sectioned_table"]["source_row_count"] == len(P154_GRID), "full grid used for detection")
    source_rows = block["_sectioned_table"]["split"]["sections"][0]["source_rows"]
    check(source_rows == [2, 3], "sectioned split retains source rows for later symbol placement")
    check(len(block.get("grid_rows", [])) == 12, "prompt-facing grid_rows stays truncated to 12 rows")
    prompt_json = layout_map_prompt_json(layout_map)
    check("_sectioned_table" not in prompt_json, "private key stripped from the prompt JSON")
    check("2021" in block["_sectioned_table"]["markdown"], "attached markdown carries the header")


def check_build_candidates() -> None:
    split = split_sectioned_grid(P154_GRID, header_rows=0)
    sectioned_block = {
        "id": "b0002",
        "type": "table",
        "bbox": [0.08, 0.14, 0.92, 0.65],
        "grid_rows": P154_GRID[:12],
        "_sectioned_table": {
            "markdown": render_sectioned_tables(split),
            "split": split,
            "section_titles": split["section_titles"],
            "section_qualifiers": split["section_qualifiers"],
            "data_row_count": split["data_row_count"],
            "source_row_count": len(P154_GRID),
        },
    }
    layout_map = {"page_size": [609.0, 793.0], "blocks": [sectioned_block]}
    candidates = build_table_candidates(
        cells=[], page_size=(609.0, 793.0), picture_records={}, layout_map=layout_map
    )
    check(len(candidates) == 1, "one docling_table candidate built")
    candidate = candidates[0]
    check(candidate.verified, "sectioned candidate is verified without a VLM call")
    check(candidate.usage is None, "no VLM usage recorded for a deterministic split")
    check((candidate.stats or {}).get("format") == "sectioned_table", "candidate marked sectioned_table")
    check(
        (candidate.stats or {}).get("sectioned_split", {}).get("sections"),
        "candidate retains the sectioned source-row mapping",
    )
    check("kpi_panel" not in (candidate.stats or {}), "KPI branch skipped for a sectioned table")
    check(candidate.markdown.count("### ") == 4, "candidate markdown holds the subtables")

    # Backward compatibility: an old block without the key behaves as before.
    legacy_block = {"id": "b0002", "type": "table", "bbox": [0.1, 0.1, 0.9, 0.6], "grid_rows": P154_GRID[:12]}
    legacy = build_table_candidates(
        cells=[], page_size=(609.0, 793.0), picture_records={},
        layout_map={"page_size": [609.0, 793.0], "blocks": [legacy_block]},
    )
    check(not legacy[0].verified, "legacy docling table stays unverified")
    check(legacy[0].markdown == "", "legacy docling table gets no injected markdown")


def check_prompt_block_wording() -> None:
    candidate = _candidate_from_grid(P154_GRID)
    block = verified_tables_prompt_block([candidate])
    check("pre-split into" in block, "prompt block describes the subtable group")
    check("do NOT merge them back" in block, "prompt block forbids re-merging")


def check_transcribe_skip() -> None:
    candidate = _candidate_from_grid(P154_GRID)
    candidate.stats["symbol_picture_indices"] = [1]  # would normally reach the VLM
    original_markdown = candidate.markdown
    page_dir = Path(tempfile.mkdtemp())
    args = SimpleNamespace(
        skip_vlm=False,
        ollama_base_url="http://unused",
        ollama_model="unused",
        temperature=0.0,
        num_ctx=1024,
        num_predict=512,
        auto_num_ctx=False,
    )
    try:
        transcribe_table_candidates(
            candidates=[candidate],
            cells=[],
            page_image_path=page_dir / "page.png",
            page_dir=page_dir,
            args=args,
            picture_records={},
            page_size=(1.0, 1.0),
        )
    finally:
        shutil.rmtree(page_dir, ignore_errors=True)
    check(candidate.markdown == original_markdown, "sectioned candidate markdown untouched by transcription")
    check(candidate.verified, "sectioned candidate stays verified")
    check(candidate.usage is None, "no VLM call made for a sectioned candidate")


DEGRADED_154 = """## NOTE 5.6

### CAPITAL AT YEAR-END
- Share capital (in €): 171,920,622 | 168,959,483 | 169,443,282 | 169,888,498 | 170,348,621
- Number of shares issued: 687,682,489 | 675,837,932 | 677,773,128 | 679,553,991 | 681,394,483

### OPERATIONS AND EARNINGS FOR THE YEAR
(in € millions)
- Sales before tax: 635 | 699 | 890 | 1,030 | 1,256
- Income tax (a): 47 | 45 | 76 | 104 | 89
- Dividends paid (b): 1,249 | 1,291 | 1,360 | 1,392 | 1,533

### EARNINGS PER SHARE
(in € per share)
- Dividend per share: 1.94 | 2.00 | 2.10 | 2.15 | 2.25

### PERSONNEL COSTS
- Average number of employees for the year: 1,008 | 1,004 | 1,042 | 1,153 | 1,202
- Payroll expense (in € millions): 160 | 178 | 218 | 222 | 257

(a) Income (expense).
"""


def check_replace_degradation() -> None:
    candidate = _candidate_from_grid(P154_GRID)
    out, enforced = replace_sectioned_tables(DEGRADED_154, [candidate])
    check(enforced == ["tc001"], "degraded table reported as enforced")
    check("| 2021 | 2022 | 2023 | 2024 | 2025 |" in out, "authoritative header spliced in")
    check("- Sales before tax:" not in out, "list-line remnant removed")
    check("(in € millions)" not in out.split("###")[0], "stray qualifier line removed")
    check("(a) Income (expense)." in out, "unrelated footnote preserved")
    check("## NOTE 5.6" in out, "unrelated heading preserved")
    check(missing_verified_table_ids(out, [candidate]) == [], "no verified table reported missing")


def check_replace_twin() -> None:
    candidate = _candidate_from_grid(P154_GRID)
    twin_rows = "\n".join(
        "| " + " | ".join(row) + " |" for row in P154_GRID
    )
    twin = (
        "## NOTE 5.6\n\n"
        + twin_rows.replace(
            "|  | 2021", "|  | 2021"
        )
    )
    # Insert a separator after the header to make it a real pipe table.
    lines = twin.splitlines()
    header_index = next(i for i, line in enumerate(lines) if "2021" in line)
    lines.insert(header_index + 1, "|---|---|---|---|---|---|")
    twin = "\n".join(lines) + "\n\n(a) Income (expense).\n"
    out, enforced = replace_sectioned_tables(twin, [candidate])
    check(enforced == ["tc001"], "twin table reported as enforced")
    check(out.count("### ") == 4, "twin replaced by four subtables")
    check("| CAPITAL AT YEAR-END |  |" not in out, "combined section row gone")
    check("(a) Income (expense)." in out, "unrelated footnote preserved")


def check_replace_placed_removes_twin() -> None:
    candidate = _candidate_from_grid(P154_GRID)
    twin_rows = "\n".join("| " + " | ".join(row) + " |" for row in P154_GRID)
    lines = twin_rows.splitlines()
    lines.insert(1, "|---|---|---|---|---|---|")
    twin = "\n".join(lines)
    placed = "## Head\n\n" + candidate.markdown + "\n\n" + twin + "\n"
    out, enforced = replace_sectioned_tables(placed, [candidate])
    check(enforced == ["tc001"], "placed+twin reported as enforced")
    check(out.count("### ") == 4, "only the four placed subtables remain")
    check("| CAPITAL AT YEAR-END |  |" not in out, "leftover twin removed")


def check_replace_identity() -> None:
    plain = "## X\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    out, enforced = replace_sectioned_tables(plain, [])
    check(out == plain and enforced == [], "no sectioned candidates leaves markdown untouched")
    # A non-sectioned verified candidate must also be ignored.
    other = TableCandidate(
        candidate_id="tc009", kind="layout_region", bbox=[0, 0, 1, 1],
        markdown="| a | b |\n|---|---|\n| 1 | 2 |\n", verified=True, stats={"format": "kpi_list"},
    )
    out2, enforced2 = replace_sectioned_tables(plain, [other])
    check(out2 == plain and enforced2 == [], "non-sectioned candidate ignored by enforcement")


def check_postprocess_end_to_end() -> None:
    candidate = _candidate_from_grid(P154_GRID)
    raw_rows = "\n".join("| " + " | ".join(row) + " |" for row in P154_GRID)
    raw_lines = raw_rows.splitlines()
    raw_lines.insert(1, "|---|---|---|---|---|---|")
    source_markdown = "## NOTE 5.6\n\n" + "\n".join(raw_lines) + "\n"
    final, warnings = postprocess_markdown(
        source_markdown,
        DEGRADED_154,
        [],  # records
        [candidate],
        {"page_size": [609.0, 793.0], "blocks": []},
        furniture_texts=set(),
        page_role=None,
    )
    check(final.count("### ") == 4, "postprocess yields four subtables")
    check(final.count("| 2021 | 2022 | 2023 | 2024 | 2025 |") == 4, "each subtable repeats the header")
    check("## Unplaced content" not in final, "year header no longer dumped as unplaced")
    check(not warnings.get("verified_tables_missing"), "verified table present after postprocess")
    check(warnings.get("sectioned_tables_enforced") == 1, "enforcement warning recorded")


def _parse_pipe_table(markdown: str) -> list[list[str]]:
    grid: list[list[str]] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if set(stripped) <= set("|-: "):
            continue
        grid.append([cell.strip() for cell in stripped.strip("|").split("|")])
    return grid


def check_real_artifact_p154() -> None:
    if not REAL_PAGE_154.exists():
        print("[skip] page-154 artifact absent; skipping real-data check")
        return
    grid = _parse_pipe_table(REAL_PAGE_154.read_text(encoding="utf-8"))
    split = split_sectioned_grid(grid, header_rows=0)
    check(split is not None, "real page-154 raw table is recognized as sectioned")
    for title in ["CAPITAL AT YEAR-END", "OPERATIONS AND EARNINGS FOR THE YEAR",
                  "EARNINGS PER SHARE", "PERSONNEL COSTS"]:
        check(title in split["section_titles"], f"section '{title}' detected in real artifact")
    rendered = render_sectioned_tables(split)
    source_numbers = set(re.findall(r"\d[\d,.]*", "\n".join(" ".join(r) for r in grid)))
    rendered_numbers = set(re.findall(r"\d[\d,.]*", rendered))
    missing = source_numbers - rendered_numbers
    check(not missing, "every numeric token from the raw table survives the split")


def main() -> int:
    check_split_p154()
    check_split_p43()
    check_split_group_header()
    check_group_heading_levels_consistent()
    check_split_negatives()
    check_render()
    check_sectioned_body_bullets_and_raw_twin_anchor()
    check_sectioned_symbol_placement_by_source_row()
    check_sectioned_cross_record_rowspan_bbox_is_ignored()
    check_prompt_map_wiring()
    check_build_candidates()
    check_prompt_block_wording()
    check_transcribe_skip()
    check_replace_degradation()
    check_replace_twin()
    check_replace_placed_removes_twin()
    check_replace_identity()
    check_postprocess_end_to_end()
    check_real_artifact_p154()
    print("test_sectioned_tables: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
