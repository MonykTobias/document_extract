"""Regression checks for the Danone policy/impact table repair (pages 189-196).

Covers the deterministic grid pipeline added for title/detail and regular
policy tables: the stale-record fix, full-grid provenance, one-header
normalization, title/detail row merging, bullet preservation, symbol placement
by row geometry, source-complete verification, and the no-standalone-symbol /
no-unplaced-coverage guarantees.

Run from the repository root with
``python tests/test_policy_table_regressions.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from document_extract.layout.prompt_map import build_layout_prompt_map
from document_extract.markdown.formatting import (
    _distinctive_fragments,
    _enforce_deterministic_candidate,
    _list_fragments,
    drop_duplicate_subset_tables,
    insert_image_references_and_summaries,
    missing_verified_table_ids,
    replace_deterministic_tables,
    strip_loose_symbol_lines,
)
from document_extract.markdown.postprocess import (
    filter_unplaced_lines,
    split_sectioned_grid,
)
from document_extract.models import PictureRecord, TableCandidate, VisualCandidate
from document_extract.refinement import postprocess_markdown
from document_extract.visual_values import apply_trusted_visual_values
from document_extract.tables import (
    _cell_with_bullets,
    build_table_candidates,
    normalize_table_grid,
    render_deterministic_docling_table,
    render_grid_markdown,
    verify_region_table,
    verified_tables_prompt_block,
)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


# --------------------------------------------------------------------------- #
# Fake Docling items (duck-typed against docling_adapter's inspection surface)
# --------------------------------------------------------------------------- #


class _Box:
    def __init__(self, rect: list[float]) -> None:
        self.l, self.t, self.r, self.b = rect
        self.coord_origin = "TOPLEFT"


class _Prov:
    def __init__(self, rect: list[float]) -> None:
        self.bbox = _Box(rect)
        self.page_no = 1


class _PictureItem:
    label = "picture"

    def __init__(self, rect: list[float]) -> None:
        self.prov = [_Prov(rect)]
        self.text = ""


def _picture(index: int, rect: list[float], value: str) -> tuple[_PictureItem, PictureRecord]:
    item = _PictureItem(rect)
    record = PictureRecord(
        page=1,
        index=index,
        placeholder=f"{{{{DOC_IMAGE_p0001_i{index:03d}}}}}",
        rel_path=f"images/picture_p0001_i{index:03d}.png",
        abs_path=None,
        bbox={"l": rect[0], "t": rect[1], "r": rect[2], "b": rect[3], "origin": "TOPLEFT"},
        area_ratio=0.001,
        classification="",
        caption="",
        norm_rect=list(rect),
        summary_type="symbol",
        summary=value,
    )
    return item, record


def _structured_grid(rows: list[list[str]], header_rows: int, row_bands: list[tuple[float, float]]):
    """Structured grid dict with a bbox on every cell (top-left, normalized)."""
    cells = []
    for r, row in enumerate(rows):
        top, bottom = row_bands[r]
        span = 1.0 / max(len(row), 1)
        for c, text in enumerate(row):
            cells.append(
                {
                    "r": r,
                    "c": c,
                    "text": text,
                    "bbox": {
                        "l": c * span,
                        "t": top,
                        "r": (c + 1) * span,
                        "b": bottom,
                        "origin": "TOPLEFT",
                    },
                    "column_header": r < header_rows,
                }
            )
    return {
        "rows": rows,
        "num_cols": max(len(row) for row in rows),
        "header_rows": header_rows,
        "cells": cells,
    }


# --------------------------------------------------------------------------- #
# Milestone 1: stale-record fix + full-grid provenance
# --------------------------------------------------------------------------- #


def test_picture_blocks_keep_their_own_index_and_value() -> None:
    items: list[_PictureItem] = []
    records: dict[int, PictureRecord] = {}
    for index in range(1, 6):
        top = 0.10 * index
        item, record = _picture(index, [0.9, top, 0.98, top + 0.03], f"S{index}")
        items.append(item)
        records[id(item)] = record

    layout_map = build_layout_prompt_map(items, (1.0, 1.0), records)
    picture_blocks = [b for b in layout_map["blocks"] if b.get("picture_index") is not None]
    check(len(picture_blocks) == 5, "every picture item produced a block")
    seen = {(b["picture_index"], b.get("value")) for b in picture_blocks}
    check(
        seen == {(1, "S1"), (2, "S2"), (3, "S3"), (4, "S4"), (5, "S5")},
        "each picture block keeps its own index and value (no stale record reuse)",
    )


def test_full_grid_reaches_stats_and_verifier() -> None:
    grid = _structured_grid(
        [
            ["Policy", "Reference", "ESRS coverage"],
            ["FOOD WASTE REPORTING GUIDELINES", "version 1.0 of June 2016", ""],
            ["OTHER POLICY", "effective 2025", ""],
        ],
        header_rows=1,
        row_bands=[(0.10, 0.13), (0.20, 0.30), (0.32, 0.42)],
    )
    candidates = build_table_candidates(
        cells=[],
        page_size=(1.0, 1.0),
        picture_records={},
        layout_map={"blocks": [{"id": "b0001", "type": "table", "bbox": [0.05, 0.05, 0.95, 0.5], "_table_grid": grid}]},
    )
    stats = candidates[0].stats or {}
    check(stats.get("grid", {}).get("rows") == grid["rows"], "full structured grid stored on the candidate")

    cell_texts = [cell["text"] for row in grid["rows"] for cell in [{"text": row_cell} for row_cell in row]]
    markdown = (
        "| Policy | Reference | ESRS coverage |\n|---|---|---|\n"
        "| FOOD WASTE REPORTING GUIDELINES | version 1.0 of June 2016 |  |\n"
        "| OTHER POLICY | effective 2025 |  |\n"
    )
    ok, verify_stats = verify_region_table(markdown, cell_texts)
    check(ok, "a table containing 1.0 and 2025 verifies when the full grid is the source")
    check(not verify_stats.get("invented_numbers"), "no source number is falsely 'invented'")


# --------------------------------------------------------------------------- #
# Milestone 2: title/detail + regular classification and rendering
# --------------------------------------------------------------------------- #


IMPACT_ROWS = [
    ["", "", "Location in the value chain", "Location in the value chain", "Location in the value chain", ""],
    ["", "Type", "Upstream", "Own operations", "Downstream", "ESRS coverage"],
    ["Working hours and adequate rest", "", "", "", "", ""],
    ["Excessive working hours can harm health.", "Negative impact", "x", "x", "x", ""],
    ["Adequate wage", "", "", "", "", ""],
    ["Workers not paid an adequate wage suffer.", "Negative impact", "x", "x", "x", ""],
    ["Collective bargaining", "", "", "", "", ""],
    ["Rights to association are not always recognized.", "Negative impact", "x", "x", "x", ""],
]
IMPACT_BANDS = [
    (0.24, 0.27), (0.27, 0.30),
    (0.30, 0.33), (0.33, 0.40),
    (0.42, 0.45), (0.45, 0.52),
    (0.54, 0.57), (0.57, 0.64),
]


def test_title_detail_normalizes_to_one_row_and_one_header() -> None:
    grid = _structured_grid(IMPACT_ROWS, header_rows=2, row_bands=IMPACT_BANDS)
    norm = normalize_table_grid(grid)
    check(norm is not None, "impact grid normalizes")
    check(norm["classification"] == "title_detail_table", "alternating title/detail grid is a title_detail_table")
    check(
        norm["header"] == ["Location in the value chain", "Type", "Upstream", "Own operations", "Downstream", "ESRS coverage"],
        "the two header rows collapse into one, super-header recovered into column 0",
    )
    check(len(norm["records"]) == 3, "three title/detail pairs merge into three records")
    first = norm["records"][0]["cells"][0]
    check(
        first == "Working hours and adequate rest<br>Excessive working hours can harm health.",
        "title and detail join in one cell with <br>",
    )
    markdown = render_grid_markdown(norm)
    header_lines = [ln for ln in markdown.splitlines() if "ESRS coverage" in ln]
    check(len(header_lines) == 1, "the header appears exactly once (no repeated header rows)")


def test_bullet_detail_survives_as_one_record() -> None:
    rows = [
        ["", "Type", "ESRS coverage"],
        ["Poor animal welfare", "", ""],
        ["Overcrowding harms welfare: • injury • disease • stress", "Negative impact", ""],
        ["Public engagement", "", ""],
        ["A lack of policy hinders progress.", "Negative impact", ""],
    ]
    bands = [(0.20, 0.23), (0.24, 0.27), (0.27, 0.34), (0.36, 0.39), (0.39, 0.46)]
    grid = _structured_grid(rows, header_rows=1, row_bands=bands)
    norm = normalize_table_grid(grid)
    check(norm["classification"] == "title_detail_table", "bullet-bearing grid stays a title_detail_table (never sectioned)")
    cell0 = norm["records"][0]["cells"][0]
    check("<br>- injury" in cell0 and "<br>- disease" in cell0, "inline bullets are preserved as <br>- items")
    check(cell0.startswith("Poor animal welfare<br>Overcrowding harms welfare:"), "lead prose before bullets is retained")
    check(len(norm["records"]) == 2, "both bullet records stay in one table as two rows")


def test_single_header_title_detail_is_not_sectioned() -> None:
    # Page 190: the "Location in the value chain" super-header sits OUTSIDE the
    # grid (a separate heading), so the grid has a single header row and an
    # alternating title -> single-detail-row body. It must stay one title/detail
    # table, never a stack of `###`-titled single-record subtables.
    rows = [
        ["", "Type", "Upstream", "Own operations", "Downstream", "ESRS coverage"],
        ["Protection of whistleblowers", "", "", "", "", ""],
        ["Failure to provide secure mechanisms discourages reporting.", "Negative impact", "x", "x", "x", ""],
        ["Misuse or breach of personal data", "", "", "", "", ""],
        ["Breaches of confidentiality harm individual rights.", "Negative impact", "x", "x", "x", ""],
    ]
    check(split_sectioned_grid(rows, header_rows=1) is None, "alternating title/detail grid is not sectioned")
    norm = normalize_table_grid({"rows": rows, "num_cols": 6, "header_rows": 1, "cells": []})
    check(norm["classification"] == "title_detail_table", "it normalizes as a title/detail table")
    check(len(norm["records"]) == 2, "each title/detail pair is one record")


def test_genuine_sectioned_table_still_splits() -> None:
    # A spanning title that governs >= 2 complete data rows stays a sectioned
    # table (existing behavior preserved by the new discriminator).
    rows = [
        ["Company", "2024", "2025"],
        ["EUROPE", "", ""],
        ["Alpha", "1", "2"],
        ["Beta", "3", "4"],
        ["ASIA", "", ""],
        ["Gamma", "5", "6"],
        ["Delta", "7", "8"],
    ]
    split = split_sectioned_grid(rows, header_rows=1)
    check(split is not None and split["section_titles"] == ["EUROPE", "ASIA"], "multi-row sections still split")


def test_regular_policy_grid_stays_a_table() -> None:
    rows = [
        ["Danone's Policies", "Key contents", "Scope", "ESRS coverage"],
        ["FOOD WASTE REPORTING GUIDELINES", "version 1.0 of June 2016", "Own operations", ""],
        ["HUMAN RIGHTS POLICY", "10 Principles of the UN Global Compact", "Own operations", ""],
        ["SUSTAINABILITY PRINCIPLES", "Social and ethical standards", "Value chain", ""],
    ]
    bands = [(0.10, 0.13), (0.20, 0.28), (0.30, 0.38), (0.40, 0.48)]
    grid = _structured_grid(rows, header_rows=1, row_bands=bands)
    norm = normalize_table_grid(grid)
    check(norm is not None and norm["classification"] == "regular_table", "a normal policy grid is a regular_table")
    check(len(norm["records"]) == 3, "every policy row is one record")
    check(norm["records"][0]["cells"][0] == "FOOD WASTE REPORTING GUIDELINES", "the first policy row is retained verbatim")


def _detect_and_render(grid, table_bbox, picture_records):
    """Detect candidates, then run the transcription-time deterministic render
    (which is where coverage symbols are available)."""
    candidates = build_table_candidates(
        cells=[],
        page_size=(1.0, 1.0),
        picture_records=picture_records,
        layout_map={"blocks": [{"id": "b0001", "type": "table", "bbox": table_bbox, "_table_grid": grid}]},
    )
    for candidate in candidates:
        render_deterministic_docling_table(candidate, picture_records, (1.0, 1.0))
    return candidates[0]


def test_regular_grid_without_symbols_is_left_alone() -> None:
    rows = [
        ["Metric", "2024", "2025"],
        ["Sales", "27,376", "27,283"],
        ["Net income", "2,021", "1,825"],
    ]
    grid = _structured_grid(rows, header_rows=1, row_bands=[(0.1, 0.13), (0.2, 0.23), (0.24, 0.27)])
    candidate = _detect_and_render(grid, [0.05, 0.05, 0.95, 0.4], {})
    check(not candidate.verified, "a symbol-free regular table is not deterministically rendered (existing flow preserved)")
    check((candidate.stats or {}).get("format") is None, "no deterministic format is stamped on it")


def test_end_to_end_candidate_places_symbols_in_coverage_cell() -> None:
    grid = _structured_grid(IMPACT_ROWS, header_rows=2, row_bands=IMPACT_BANDS)
    table_bbox = [0.05, 0.20, 0.99, 0.66]
    # Two symbols, each vertically centered on a merged record's row band.
    _, rec1 = _picture(1, [0.90, 0.35, 0.98, 0.39], "S1")   # record 0 band 0.30-0.40
    _, rec2 = _picture(2, [0.90, 0.47, 0.98, 0.51], "S2")   # record 1 band 0.42-0.52
    candidate = _detect_and_render(grid, table_bbox, {1: rec1, 2: rec2})
    check(candidate.verified, "the title/detail table is deterministically verified")
    check((candidate.stats or {}).get("format") == "title_detail_table", "candidate format is title_detail_table")
    body = [ln for ln in candidate.markdown.splitlines() if ln.startswith("| Working") or ln.startswith("| Adequate")]
    check(body and body[0].rstrip().endswith("S1 |"), "S1 lands in the first record's coverage cell")
    check(body[1].rstrip().endswith("S2 |"), "S2 lands in the second record's coverage cell")
    check("symbols_unplaced_geometry" not in (candidate.stats or {}), "no symbol is left unplaced")


def test_ambiguous_symbol_is_left_unplaced_not_sequenced() -> None:
    grid = _structured_grid(IMPACT_ROWS, header_rows=2, row_bands=IMPACT_BANDS)
    table_bbox = [0.05, 0.20, 0.99, 0.66]
    # A symbol centered in the gap between record bands matches no single row.
    _, rec = _picture(1, [0.90, 0.405, 0.98, 0.415], "S9")   # center 0.41, between 0.40 and 0.42
    candidate = _detect_and_render(grid, table_bbox, {1: rec})
    check("S9" not in candidate.markdown, "an unmatched symbol is never written into a cell by sequence")
    check((candidate.stats or {}).get("symbols_unplaced_geometry") == [1], "the unplaced symbol is recorded for repair")


def test_cross_record_rowspan_bbox_does_not_expand_record_bands() -> None:
    """A Docling merged-cell bbox repeated across records must be ignored."""
    rows = [
        ["Policy", "Details", "ESRS coverage"],
        ["Policy one", "Detail one", ""],
        ["Policy two", "Detail two", ""],
        ["Policy three", "Detail three", ""],
        ["Policy four", "Detail four", ""],
        ["Policy five", "Detail five", ""],
    ]
    bands = [(0.10, 0.14), (0.20, 0.25), (0.28, 0.33), (0.36, 0.41), (0.44, 0.49), (0.52, 0.57)]
    grid = _structured_grid(rows, header_rows=1, row_bands=bands)
    # Mirrors p200: one first-column cell bbox is repeated on five different
    # source rows even though every row is its own normalized logical record.
    merged_bbox = {"l": 0.0, "t": 0.20, "r": 1 / 3, "b": 0.57, "origin": "TOPLEFT"}
    for cell in grid["cells"]:
        if cell["c"] == 0 and 1 <= cell["r"] <= 5:
            cell["bbox"] = dict(merged_bbox)
    records = {
        index: _picture(index, [0.88, bands[index][0] + 0.01, 0.96, bands[index][0] + 0.04], "S1")[1]
        for index in range(1, 6)
    }
    candidate = _detect_and_render(grid, [0.0, 0.1, 1.0, 0.6], records)
    coverage_rows = [line for line in candidate.markdown.splitlines() if line.startswith("| Policy ") and "Details" not in line]
    check(len(coverage_rows) == 5 and all(line.rstrip().endswith("S1 |") for line in coverage_rows),
          "each repeated S1 lands in its own logical coverage row")
    stats = candidate.stats or {}
    check(stats.get("symbols_placed_geometry") == [1, 2, 3, 4, 5], "all row-local placements are audited")
    check("symbols_unplaced_geometry" not in stats, "cross-record bbox reuse creates no ambiguity")
    candidate.verified = False
    candidate.markdown = ""
    candidate.stats["symbols_unplaced_geometry"] = [999]
    candidate.stats.pop("symbols_placed_geometry", None)
    check(
        render_deterministic_docling_table(candidate, records, (1.0, 1.0)),
        "an incomplete deterministic checkpoint candidate is rerendered",
    )
    check(
        "symbols_unplaced_geometry" not in (candidate.stats or {}),
        "checkpoint rerender clears its stale geometry deficit",
    )


def test_bbox_reused_inside_one_title_detail_record_is_kept() -> None:
    """A repeated bbox remains valid when both rows normalize into one record."""
    grid = _structured_grid(IMPACT_ROWS, header_rows=2, row_bands=IMPACT_BANDS)
    shared_bbox = {"l": 0.0, "t": 0.30, "r": 1.0, "b": 0.40, "origin": "TOPLEFT"}
    for cell in grid["cells"]:
        if cell["r"] in {2, 3}:
            cell["bbox"] = dict(shared_bbox)
    _, symbol = _picture(1, [0.90, 0.34, 0.98, 0.38], "S1")
    candidate = _detect_and_render(grid, [0.05, 0.20, 0.99, 0.66], {1: symbol})
    check("S1" in candidate.markdown, "same-record repeated bbox still supplies a usable band")
    check((candidate.stats or {}).get("symbol_row_assignments") == {1: 0}, "placement retains its logical-record audit")
    check(
        (candidate.stats or {}).get("symbol_cell_assignments") == {1: {"record_index": 0, "column_index": 5}},
        "placement retains its exact output-cell audit",
    )


def test_incomplete_deterministic_candidate_is_not_authoritative() -> None:
    rows = [
        ["Danone's Policies", "Key contents", "Scope", "ESRS coverage"],
        ["FOOD WASTE REPORTING GUIDELINES", "version 1.0", "Own operations", ""],
        ["HUMAN RIGHTS POLICY", "10 Principles", "Own operations", ""],
    ]
    candidate = _detect_and_render(
        _structured_grid(rows, header_rows=1, row_bands=[(0.10, 0.13), (0.20, 0.30), (0.32, 0.42)]),
        [0.05, 0.05, 0.99, 0.5],
        {1: _picture(1, [0.90, 0.23, 0.98, 0.27], "E5")[1]},
    )
    candidate.stats["symbols_placed_geometry"] = []
    candidate.stats["symbols_unplaced_geometry"] = [1]
    source = _raw_twin_markdown(rows, header_rows=1)
    out, enforced = replace_deterministic_tables(source, [candidate])
    check(enforced == [] and out == source, "incomplete regular candidate cannot overwrite a VLM/raw table")
    check(verified_tables_prompt_block([candidate]) == "(none)", "incomplete candidate is omitted from the authoritative prompt block")
    check(missing_verified_table_ids("# Page\n", [candidate]) == [], "incomplete candidate is not reported as a required verified table")
    _, dropped = drop_duplicate_subset_tables(source + "\n" + source, [candidate])
    check(dropped == 0, "incomplete candidate cannot back duplicate-table deletion")


def test_visual_enforce_gate_uses_shared_authority_callers() -> None:
    rows = [
        ["Policy", "Detail", "Signal"],
        ["Alpha", "First", ""],
        ["Beta", "Second", ""],
    ]
    candidate = _detect_and_render(
        _structured_grid(rows, header_rows=1, row_bands=[(0.10, 0.13), (0.20, 0.30), (0.32, 0.42)]),
        [0.05, 0.05, 0.99, 0.5],
        {1: _picture(1, [0.90, 0.23, 0.98, 0.27], "X1")[1]},
    )
    source = _raw_twin_markdown(rows, header_rows=1)
    candidate.stats["visual_values"] = {
        "status": "incomplete",
        "mode": "enforce",
        "collector_version": 1,
    }
    out, enforced = replace_deterministic_tables(source, [candidate])
    check(
        enforced == [] and out == source,
        "enforce visual incompleteness preserves the existing page table",
    )
    check(
        verified_tables_prompt_block([candidate]) == "(none)",
        "visual incompleteness is withheld from the shared authoritative prompt",
    )
    _, dropped = drop_duplicate_subset_tables(source + "\n" + source, [candidate])
    check(dropped == 0, "visual incompleteness cannot back duplicate-table deletion")

    candidate.stats["visual_values"]["mode"] = "audit"
    out, enforced = replace_deterministic_tables(source, [candidate])
    check(
        candidate.has_complete_symbol_geometry()
        and verified_tables_prompt_block([candidate]) != "(none)"
        and enforced == []
        and out == source,
        "audit records incompleteness without changing legacy table authority",
    )


# --------------------------------------------------------------------------- #
# Milestone 3: no standalone symbol lines, no unplaced coverage values,
# right-edge navigation images omitted
# --------------------------------------------------------------------------- #


def test_symbol_value_never_becomes_a_standalone_line() -> None:
    # An unplaced symbol whose image reference survived: its value must not leak
    # as a loose line, and its lone image link is dropped.
    record = PictureRecord(
        page=1,
        index=1,
        placeholder="{{DOC_IMAGE_p0001_i001}}",
        rel_path="images/picture_p0001_i001.png",
        abs_path=None,
        bbox=None,
        area_ratio=0.001,
        classification="",
        caption="",
        norm_rect=[0.85, 0.5, 0.9, 0.53],
        summary_type="symbol",
        summary="G1",
    )
    markdown = "Some prose paragraph.\n\n![Picture p0001-i001](images/picture_p0001_i001.png)\n"
    out = insert_image_references_and_summaries(markdown, [record])
    check("G1" not in out, "an unplaced symbol value is not emitted as a standalone line")
    check("picture_p0001_i001" not in out, "the symbol's image reference is dropped from the body")


def test_loose_symbol_lines_are_stripped_without_touching_real_images() -> None:
    _, s4 = _picture(1, [0.90, 0.20, 0.96, 0.24], "S4")
    _, g1 = _picture(2, [0.90, 0.30, 0.96, 0.34], "G1")
    _, multi = _picture(3, [0.90, 0.40, 0.96, 0.44], "S1, S2, S3")
    markdown = (
        "![S4](#)\n"
        "![G1](#) ![G1](#) ![G1](#)\n"
        "![S1 S2 S3](#)\n"
        "![S1, S2, S3](#)\n"
        "![S4](#) ![Z9](#)\n"
        "![Image summary: S4](#) (from block b0011, position [0.1, 0.2])\n"
        "![Picture p0001-i099](images/picture_p0001_i099.png)\n"
        "**Image summary:** a genuine content image.\n"
        "Prose mentioning S4 must stay.\n"
        "| Coverage | ![S4](#) |\n"
        "![Z9](#)\n"
    )
    out, count = strip_loose_symbol_lines(markdown, [s4, g1, multi])
    check(count == 6, "all standalone real-symbol dump shapes are stripped")
    check("![Picture p0001-i099]" in out, "genuine picture reference is retained")
    check("Prose mentioning S4 must stay." in out, "ordinary prose is retained")
    check("| Coverage | ![S4](#) |" in out, "pipe-row image token is retained")
    check("![Z9](#)" in out, "unanchored code-shaped image token is retained")
    second, second_count = strip_loose_symbol_lines(out, [s4, g1, multi])
    check(second == out and second_count == 0, "loose-symbol stripping is idempotent")


def test_right_edge_navigation_image_is_omitted_content_image_kept() -> None:
    nav = PictureRecord(
        page=1, index=1, placeholder="{{DOC_IMAGE_p0001_i001}}",
        rel_path="images/picture_p0001_i001.png", abs_path=None, bbox=None,
        area_ratio=0.0005, classification="", caption="",
        norm_rect=[0.96, 0.5, 0.99, 0.53], skip_reason="too_small",
    )
    content = PictureRecord(
        page=1, index=2, placeholder="{{DOC_IMAGE_p0001_i002}}",
        rel_path="images/picture_p0001_i002.png", abs_path=None, bbox=None,
        area_ratio=0.2, classification="chart", caption="",
        norm_rect=[0.2, 0.4, 0.7, 0.7], summarize=True, summary="A revenue chart.",
    )
    out = insert_image_references_and_summaries("Body text.\n", [nav, content])
    check("picture_p0001_i001" not in out, "right-edge too-small navigation image is omitted")
    check("picture_p0001_i002" in out, "a genuine non-edge content image is retained")


def test_placed_coverage_value_is_not_reported_unplaced() -> None:
    final_markdown = (
        "| Policy | Scope | ESRS coverage |\n|---|---|---|\n"
        "| FOOD WASTE REPORTING GUIDELINES | Own operations | E5 |\n"
        "| HUMAN RIGHTS POLICY | Own operations | S1 |\n"
    )
    # The completeness guard would consider these coverage cells "missing" if the
    # diff flagged them; the unplaced filter must drop them because they are
    # already present in a table row.
    kept, dropped = filter_unplaced_lines(["E5", "S1"], final_markdown)
    check(kept == [], "coverage symbols already in a table cell are not kept as unplaced")
    check(set(dropped) == {"E5", "S1"}, "they are recorded as filtered instead")


def test_right_edge_dash_run_but_lone_dash_kept() -> None:
    # Delegates to the shared postprocess stripper; covered in depth in
    # test_furniture_stripping. Here we only confirm a lone dash is content.
    from document_extract.markdown.postprocess import strip_furniture_lines

    kept = strip_furniture_lines("A paragraph.\n\n- a real bullet item\n")
    check("- a real bullet item" in kept, "a real list bullet is never stripped")


# --------------------------------------------------------------------------- #
# Phase 1: multiple symbol labels join in visual order, comma-separated
# --------------------------------------------------------------------------- #


def _policy_grid_with_coverage():
    rows = [
        ["Danone's Policies", "Key contents", "ESRS coverage"],
        ["FOOD WASTE REPORTING GUIDELINES", "version 1.0 of June 2016", ""],
        ["HUMAN RIGHTS POLICY", "10 Principles", ""],
    ]
    bands = [(0.10, 0.13), (0.30, 0.41), (0.45, 0.55)]
    return _structured_grid(rows, header_rows=1, row_bands=bands)


def test_multiple_symbols_join_in_visual_reading_order() -> None:
    # Page 185 shape: a badge cluster in one coverage cell. Arrival (picture-index)
    # order is scrambled (E1, E3, E2 -- the old space-joined bug); the placed cell
    # must read in visual order (top->bottom, left->right), comma-separated.
    grid = _policy_grid_with_coverage()
    table_bbox = [0.05, 0.08, 0.99, 0.58]
    _, e1 = _picture(1, [0.88, 0.31, 0.92, 0.34], "E1")  # top-left
    _, e3 = _picture(2, [0.94, 0.37, 0.98, 0.40], "E3")  # bottom-right
    _, e2 = _picture(3, [0.88, 0.37, 0.92, 0.40], "E2")  # bottom-left
    candidate = _detect_and_render(grid, table_bbox, {1: e1, 2: e3, 3: e2})
    check((candidate.stats or {}).get("format") == "regular_table", "symbol-bearing regular table renders")
    row = [ln for ln in candidate.markdown.splitlines() if ln.startswith("| FOOD WASTE")]
    check(
        bool(row) and row[0].rstrip().endswith("E1, E2, E3 |"),
        "badges join comma-separated in visual reading order, not picture-index order",
    )
    check("symbols_unplaced_geometry" not in (candidate.stats or {}), "all three badges are placed")


def test_repeated_symbol_value_is_de_duplicated() -> None:
    grid = _policy_grid_with_coverage()
    _, a = _picture(1, [0.88, 0.33, 0.92, 0.36], "S1")
    _, b = _picture(2, [0.94, 0.33, 0.98, 0.36], "S1")  # same value, same row
    candidate = _detect_and_render(grid, [0.05, 0.08, 0.99, 0.58], {1: a, 2: b})
    row = [ln for ln in candidate.markdown.splitlines() if ln.startswith("| FOOD WASTE")]
    check(bool(row) and row[0].rstrip().endswith("S1 |"), "a duplicated symbol value appears once")
    check("S1, S1" not in candidate.markdown, "repeated values are de-duplicated, not comma-doubled")


def test_single_symbol_gains_no_delimiter() -> None:
    grid = _policy_grid_with_coverage()
    _, sym = _picture(1, [0.90, 0.34, 0.96, 0.37], "E5")
    candidate = _detect_and_render(grid, [0.05, 0.08, 0.99, 0.58], {1: sym})
    row = [ln for ln in candidate.markdown.splitlines() if ln.startswith("| FOOD WASTE")]
    coverage = row[0].split("|")[-2] if row else ""
    check(coverage.strip() == "E5", "a lone symbol value carries no comma delimiter")


def test_multi_code_symbol_value_stays_comma_joined_in_a_coverage_cell() -> None:
    grid = _policy_grid_with_coverage()
    _, symbol = _picture(1, [0.90, 0.34, 0.96, 0.37], "S1, S2, S3")
    candidate = _detect_and_render(grid, [0.05, 0.08, 0.99, 0.58], {1: symbol})
    row = [line for line in candidate.markdown.splitlines() if line.startswith("| FOOD WASTE")]
    check(
        bool(row) and row[0].rstrip().endswith("S1, S2, S3 |"),
        "a multi-code symbol remains comma-joined when placed in a cell",
    )


def test_tagged_inserted_cell_is_not_appended_to_by_legacy_symbols() -> None:
    # Page 188 shape: a tagged value recovered into a coverage cell ("E4, E5")
    # overlaps a legacy picture symbol carrying one of its codes ("E4"). The
    # whole-string dedup in place_grid_symbols cannot see that membership, so
    # the cell must be left alone rather than joined into "E4, E5, E4".
    grid = _policy_grid_with_coverage()
    table_bbox = [0.05, 0.08, 0.99, 0.58]
    candidates = build_table_candidates(
        cells=[],
        page_size=(1.0, 1.0),
        picture_records={},
        layout_map={"blocks": [{"id": "b0001", "type": "table", "bbox": table_bbox, "_table_grid": grid}]},
    )
    candidate = candidates[0]
    visual = VisualCandidate(
        candidate_id="vv001",
        page=1,
        norm_rect=[0.88, 0.33, 0.92, 0.36],
        proposals=[
            {
                "method": "actual_text",
                "raw_value": "E4, E5",
                "normalized_value": "E4, E5",
                "comparison_key": "e4, e5",
                "confidence": 1.0,
            }
        ],
        target={
            "kind": "table_cell",
            "table_candidate_id": candidate.candidate_id,
            "record_index": 0,
            "column_index": 2,
        },
        resolution="trusted",
        resolved_value="E4, E5",
    )
    check(
        apply_trusted_visual_values([candidate], [visual], mode="enforce") == 1,
        "the tagged scalar is inserted into the coverage cell",
    )
    _, symbol = _picture(1, [0.88, 0.33, 0.92, 0.36], "E4")  # same record band
    render_deterministic_docling_table(candidate, {1: symbol}, (1.0, 1.0))
    row = [line for line in candidate.markdown.splitlines() if line.startswith("| FOOD WASTE")]
    check(
        bool(row) and row[0].rstrip().endswith("E4, E5 |"),
        "a legacy symbol never re-appends a code already inside the inserted scalar",
    )
    check(
        (candidate.stats or {}).get("symbol_cell_assignments") == {1: {"record_index": 0, "column_index": 2}},
        "the skipped symbol keeps its placement audit",
    )


def test_comma_joined_coverage_symbols_count_as_placed() -> None:
    # The unplaced-symbol guard (refinement.py) treats a symbol as placed when its
    # value is a substring of a pipe-table line; commas between joined values must
    # not hide any of them from that check.
    final = "| Policy | ESRS coverage |\n|---|---|\n| FOOD WASTE GUIDELINES | E1, E2, E3 |\n"
    for value in ("E1", "E2", "E3"):
        placed = any(
            line.strip().startswith("|") and value in line for line in final.splitlines()
        )
        check(placed, f"{value} in a comma-joined coverage cell is recognized as placed")


# --------------------------------------------------------------------------- #
# Phase 2: bullet lists inside body cells + lifted 400-char truncation
# --------------------------------------------------------------------------- #


def test_black_square_bullets_are_preserved() -> None:
    # U+25A0 BLACK SQUARE (the glyph the real Danone document uses) must split.
    rows = [
        ["", "Type", "ESRS coverage"],
        ["Poor animal welfare", "", ""],
        ["Overcrowding harms welfare: ■ injury ■ disease ■ stress", "Negative impact", ""],
        ["Public engagement", "", ""],
        ["A lack of policy hinders progress.", "Negative impact", ""],
    ]
    bands = [(0.20, 0.23), (0.24, 0.27), (0.27, 0.34), (0.36, 0.39), (0.39, 0.46)]
    grid = _structured_grid(rows, header_rows=1, row_bands=bands)
    norm = normalize_table_grid(grid)
    cell0 = norm["records"][0]["cells"][0]
    check(
        "<br>- injury" in cell0 and "<br>- disease" in cell0 and "<br>- stress" in cell0,
        "U+25A0 bullets split into <br>- items",
    )
    check(cell0.startswith("Poor animal welfare<br>Overcrowding harms welfare:"), "lead prose retained")


def test_regular_table_body_cells_get_bullet_lists() -> None:
    # Pages 183-185 shape: bulleted lists live in ordinary regular-table body cells,
    # not only in title/detail detail-cells. Every column must bullet.
    rows = [
        ["Stakeholder", "Engagement channels", "Example initiatives"],
        [
            "Farmers",
            "■ Strategic partnerships ■ Co-design of projects",
            "■ Regenerative agriculture ■ Soil health programs",
        ],
        [
            "Employees",
            "■ Annual survey ■ Works councils",
            "■ Training ■ Safety campaigns",
        ],
    ]
    bands = [(0.10, 0.13), (0.20, 0.30), (0.32, 0.42)]
    grid = _structured_grid(rows, header_rows=1, row_bands=bands)
    norm = normalize_table_grid(grid)
    check(norm is not None and norm["classification"] == "regular_table", "stakeholder grid stays a regular_table")
    check(len(norm["records"]) == 2, "row count unchanged after bulleting")
    check(all(len(record["cells"]) == 3 for record in norm["records"]), "column count unchanged")
    check(
        norm["records"][0]["cells"][1] == "- Strategic partnerships<br>- Co-design of projects",
        "a leading-bullet cell becomes a <br>-joined - item list",
    )
    check(
        norm["records"][0]["cells"][2].startswith("- Regenerative agriculture<br>- Soil health programs"),
        "the second bulleted column is converted too",
    )


def test_double_glyph_collapses_to_one_item() -> None:
    rows = [
        ["Topic", "Notes", "ESRS coverage"],
        ["Welfare", "■ ■ Single surviving item", ""],
        ["Other", "Plain note", ""],
    ]
    bands = [(0.10, 0.13), (0.20, 0.23), (0.24, 0.27)]
    grid = _structured_grid(rows, header_rows=1, row_bands=bands)
    norm = normalize_table_grid(grid)
    check(norm["records"][0]["cells"][1] == "- Single surviving item", "a double glyph collapses to one item")


def test_single_mid_text_glyph_is_left_alone() -> None:
    rows = [
        ["Metric", "Value", "ESRS coverage"],
        ["Pro· forma revenue", "1,234", ""],
        ["Net income", "567", ""],
    ]
    bands = [(0.10, 0.13), (0.20, 0.23), (0.24, 0.27)]
    grid = _structured_grid(rows, header_rows=1, row_bands=bands)
    norm = normalize_table_grid(grid)
    cell = norm["records"][0]["cells"][0]
    check(cell == "Pro· forma revenue", "a lone mid-text glyph does not create a fake list")
    check("<br>" not in cell and not cell.startswith("- "), "no bullet markup is introduced")


class _FakeGridCell:
    def __init__(self, text: str, header: bool = False) -> None:
        self.text = text
        self.bbox = None
        self.column_header = header


class _FakeGridData:
    def __init__(self, grid: list[list[_FakeGridCell]]) -> None:
        self.grid = grid


class _FakeTableItem:
    def __init__(self, grid: list[list[_FakeGridCell]]) -> None:
        self.data = _FakeGridData(grid)


def test_long_cell_survives_uncapped_end_to_end() -> None:
    from document_extract.docling_adapter import table_grid_structured

    long_text = (
        "Danone works to equip dairy farmer partners with the knowledge and tools needed "
        "to transition toward regenerative agriculture across the value chain. "
    ) * 4
    long_text = " ".join(long_text.split())
    check(len(long_text) > 400, "the fixture cell is longer than the old 400-char cap")

    item = _FakeTableItem(
        [
            [_FakeGridCell("Topic", header=True), _FakeGridCell("Type", header=True), _FakeGridCell("ESRS coverage", header=True)],
            [_FakeGridCell("Farmer support"), _FakeGridCell(""), _FakeGridCell("")],
            [_FakeGridCell(long_text), _FakeGridCell("Positive impact"), _FakeGridCell("")],
            [_FakeGridCell("Water stewardship"), _FakeGridCell(""), _FakeGridCell("")],
            [_FakeGridCell("Protecting shared water resources."), _FakeGridCell("Positive impact"), _FakeGridCell("")],
        ]
    )
    structured = table_grid_structured(item)
    check(len(structured["rows"][2][0]) > 400, "table_grid_structured no longer truncates a cell at 400 chars")
    check(structured["rows"][2][0] == long_text, "the full cell text round-trips through the grid")

    candidates = build_table_candidates(
        cells=[],
        page_size=(1.0, 1.0),
        picture_records={},
        layout_map={
            "blocks": [
                {"id": "b0001", "type": "table", "bbox": [0.05, 0.2, 0.99, 0.7], "_table_grid": structured}
            ]
        },
    )
    candidate = candidates[0]
    render_deterministic_docling_table(candidate, {}, (1.0, 1.0))
    check((candidate.stats or {}).get("format") == "title_detail_table", "the long-cell title/detail table renders")
    check(long_text in candidate.markdown, "the un-truncated cell survives rendering")

    raw_twin = _raw_twin_markdown(structured["rows"], header_rows=1)
    out, enforced = replace_deterministic_tables("## Impacts\n\n" + raw_twin, [candidate])
    check(enforced == [candidate.candidate_id], "the long-cell table splices over its raw twin")
    check(long_text in out, "the un-truncated cell is present after the splice")


BULLET_IMPACT_ROWS = [
    ["", "Type", "ESRS coverage"],
    ["Animal welfare", "", ""],
    ["Poor conditions harm animals: ■ injury ■ disease", "Negative impact", ""],
    ["Community relations", "", ""],
    ["Weak ties reduce trust.", "Negative impact", ""],
]
BULLET_IMPACT_BANDS = [(0.20, 0.23), (0.24, 0.27), (0.27, 0.34), (0.36, 0.39), (0.39, 0.46)]


def test_bullet_cell_survives_postprocess_end_to_end() -> None:
    grid = _structured_grid(BULLET_IMPACT_ROWS, header_rows=1, row_bands=BULLET_IMPACT_BANDS)
    candidate = _detect_and_render(grid, [0.05, 0.18, 0.99, 0.5], {})
    check((candidate.stats or {}).get("format") == "title_detail_table", "bullet title/detail renders deterministically")
    source = "## Impacts\n\n" + _raw_twin_markdown(BULLET_IMPACT_ROWS, header_rows=1)
    final, warnings = postprocess_markdown(
        source,
        source,
        [],
        [candidate],
        {"page_size": [1.0, 1.0], "blocks": []},
        furniture_texts=set(),
        page_role=None,
    )
    check(warnings.get("deterministic_tables_enforced") == 1, "enforcement warning recorded")
    check(
        "Poor conditions harm animals:<br>- injury<br>- disease" in final,
        "the in-cell bullet list survives full postprocess",
    )
    check(not warnings.get("duplicate_tables_dropped"), "the bulleted table is not dropped as a duplicate")


# --------------------------------------------------------------------------- #
# B1: deterministic splice for regular/title_detail verified tables
# --------------------------------------------------------------------------- #


def _raw_twin_markdown(rows: list[list[str]], header_rows: int) -> str:
    """The raw Docling grid as one pipe table (what --skip-vlm leaves in place)."""
    lines = ["| " + " | ".join(cell for cell in row) + " |" for row in rows]
    lines.insert(header_rows, "|" + "---|" * len(rows[0]))
    return "\n".join(lines) + "\n"


def test_deterministic_title_detail_replaces_raw_twin() -> None:
    grid = _structured_grid(IMPACT_ROWS, header_rows=2, row_bands=IMPACT_BANDS)
    candidate = _detect_and_render(grid, [0.05, 0.20, 0.99, 0.66], {})
    check(
        candidate.verified and (candidate.stats or {}).get("format") == "title_detail_table",
        "the title/detail candidate is deterministically rendered",
    )
    source = "## Human rights impacts\n\n" + _raw_twin_markdown(IMPACT_ROWS, header_rows=2)
    out, enforced = replace_deterministic_tables(source, [candidate])
    check(enforced == [candidate.candidate_id], "the raw twin is reported as enforced")
    check(
        "Working hours and adequate rest<br>Excessive working hours can harm health." in out,
        "the merged title/detail cell replaces the split title/detail rows",
    )
    check(
        "| Working hours and adequate rest |  |  |  |  |  |" not in out,
        "the standalone title-only row is gone",
    )
    header = "| Location in the value chain | Type | Upstream | Own operations | Downstream | ESRS coverage |"
    check(out.count(header) == 1, "the merged header appears exactly once")
    check("## Human rights impacts" in out, "unrelated heading preserved")


def test_deterministic_regular_table_with_symbols_replaces_twin() -> None:
    rows = [
        ["Danone's Policies", "Key contents", "Scope", "ESRS coverage"],
        ["FOOD WASTE REPORTING GUIDELINES", "version 1.0 of June 2016", "Own operations", ""],
        ["HUMAN RIGHTS POLICY", "10 Principles of the UN Global Compact", "Own operations", ""],
    ]
    bands = [(0.10, 0.13), (0.20, 0.28), (0.30, 0.38)]
    grid = _structured_grid(rows, header_rows=1, row_bands=bands)
    _, sym = _picture(1, [0.90, 0.22, 0.98, 0.26], "E5")  # centered on record 0 band
    candidate = _detect_and_render(grid, [0.05, 0.05, 0.99, 0.42], {1: sym})
    check(
        (candidate.stats or {}).get("format") == "regular_table",
        "the symbol-bearing regular table is deterministically rendered",
    )
    source = _raw_twin_markdown(rows, header_rows=1)
    out, enforced = replace_deterministic_tables(source, [candidate])
    check(enforced == [candidate.candidate_id], "the regular twin is enforced")
    food_row = [ln for ln in out.splitlines() if ln.startswith("| FOOD WASTE")]
    check(bool(food_row) and food_row[0].rstrip().endswith("E5 |"), "the placed E5 symbol survives the splice")


def test_deterministic_table_missing_is_not_appended() -> None:
    grid = _structured_grid(IMPACT_ROWS, header_rows=2, row_bands=IMPACT_BANDS)
    candidate = _detect_and_render(grid, [0.05, 0.20, 0.99, 0.66], {})
    unrelated = "## Some other page\n\nA paragraph with no table here.\n"
    out, enforced = replace_deterministic_tables(unrelated, [candidate])
    check(enforced == [], "a table with no twin in the markdown is not enforced")
    check(out == unrelated, "unrelated markdown is left untouched (never duplicated in)")


def test_deterministic_table_postprocess_end_to_end() -> None:
    grid = _structured_grid(IMPACT_ROWS, header_rows=2, row_bands=IMPACT_BANDS)
    candidate = _detect_and_render(grid, [0.05, 0.20, 0.99, 0.66], {})
    source = "## Human rights impacts\n\n" + _raw_twin_markdown(IMPACT_ROWS, header_rows=2)
    # --skip-vlm: the working markdown is the raw source markdown unchanged.
    final, warnings = postprocess_markdown(
        source,
        source,
        [],  # records
        [candidate],
        {"page_size": [1.0, 1.0], "blocks": []},
        furniture_texts=set(),
        page_role=None,
    )
    check(warnings.get("deterministic_tables_enforced") == 1, "enforcement warning recorded")
    check(
        "Working hours and adequate rest<br>Excessive working hours can harm health." in final,
        "the merged title/detail cell is present after full postprocess",
    )
    check(
        "| Working hours and adequate rest |  |  |  |  |  |" not in final,
        "the split title-only row does not survive postprocess",
    )
    check(not warnings.get("duplicate_tables_dropped"), "the merged table is not a duplicate subset")


# --------------------------------------------------------------------------- #
# B2: structural invariants in the VLM fallback verifier (grid-bearing crops)
# --------------------------------------------------------------------------- #


POLICY_GRID = {
    "rows": [
        ["Danone's Policies", "Key contents", "Scope", "ESRS coverage"],
        ["FOOD WASTE REPORTING GUIDELINES", "version 1.0 of June 2016", "Own operations", "E5"],
        ["HUMAN RIGHTS POLICY", "10 Principles", "Own operations", "S1"],
        ["SUSTAINABILITY PRINCIPLES", "Social standards", "Value chain", "G1"],
    ],
    "num_cols": 4,
    "header_rows": 1,
    "cells": [],
}
POLICY_CELL_TEXTS = [cell for row in POLICY_GRID["rows"] for cell in row]


def test_grid_verifier_accepts_faithful_transcription() -> None:
    markdown = (
        "| Danone's Policies | Key contents | Scope | ESRS coverage |\n|---|---|---|---|\n"
        "| FOOD WASTE REPORTING GUIDELINES | version 1.0 of June 2016 | Own operations | E5 |\n"
        "| HUMAN RIGHTS POLICY | 10 Principles | Own operations | S1 |\n"
        "| SUSTAINABILITY PRINCIPLES | Social standards | Value chain | G1 |\n"
    )
    ok, stats = verify_region_table(markdown, POLICY_CELL_TEXTS, grid=POLICY_GRID)
    check(ok, "a shape-faithful docling-table transcription verifies")
    check(stats.get("source_columns") == 4 and stats.get("table_columns") == 4, "column counts recorded")


def test_grid_verifier_rejects_wrong_column_count() -> None:
    markdown = (
        "| Danone's Policies | Key contents | ESRS coverage |\n|---|---|---|\n"
        "| FOOD WASTE REPORTING GUIDELINES | version 1.0 of June 2016 | E5 |\n"
        "| HUMAN RIGHTS POLICY | 10 Principles | S1 |\n"
        "| SUSTAINABILITY PRINCIPLES | Social standards | G1 |\n"
    )
    ok, stats = verify_region_table(markdown, POLICY_CELL_TEXTS, grid=POLICY_GRID)
    check(not ok and stats.get("fail") == "column_count_mismatch", "a table missing a source column is rejected")


def test_grid_verifier_rejects_repeated_header_in_body() -> None:
    markdown = (
        "| Danone's Policies | Key contents | Scope | ESRS coverage |\n|---|---|---|---|\n"
        "| FOOD WASTE REPORTING GUIDELINES | version 1.0 of June 2016 | Own operations | E5 |\n"
        "| Danone's Policies | Key contents | Scope | ESRS coverage |\n"
        "| HUMAN RIGHTS POLICY | 10 Principles | Own operations | S1 |\n"
    )
    ok, stats = verify_region_table(markdown, POLICY_CELL_TEXTS, grid=POLICY_GRID)
    check(not ok and stats.get("fail") == "repeated_header_in_body", "a header repeated in the body is rejected")


def test_grid_verifier_rejects_dropped_rows() -> None:
    markdown = (
        "| Danone's Policies | Key contents | Scope | ESRS coverage |\n|---|---|---|---|\n"
        "| FOOD WASTE REPORTING GUIDELINES | version 1.0 of June 2016 | Own operations | E5 |\n"
    )
    ok, stats = verify_region_table(markdown, POLICY_CELL_TEXTS, grid=POLICY_GRID)
    check(not ok and stats.get("fail") == "record_count_mismatch", "a transcription that drops records is rejected")


def test_grid_verifier_untouched_without_grid() -> None:
    # A region/KPI crop passes grid=None: the structural checks never run, so a
    # narrower-than-source table with faithful numbers still verifies.
    markdown = (
        "| Danone's Policies | Key contents | ESRS coverage |\n|---|---|---|\n"
        "| FOOD WASTE REPORTING GUIDELINES | version 1.0 of June 2016 | E5 |\n"
        "| HUMAN RIGHTS POLICY | 10 Principles | S1 |\n"
        "| SUSTAINABILITY PRINCIPLES | Social standards | G1 |\n"
    )
    ok, stats = verify_region_table(markdown, POLICY_CELL_TEXTS)
    check(ok, "without a grid the region verifier keeps its numeric/word-only contract")
    check("source_columns" not in stats, "no structural stats are recorded for a gridless crop")


# --------------------------------------------------------------------------- #
# Follow-up v2: deterministic bullets for symbol-free regular tables +
# leading-only middle-dot rule
# --------------------------------------------------------------------------- #


def test_symbol_free_regular_table_renders_bullets_end_to_end() -> None:
    # Pages 183-185: a regular table whose body cells carry ■-lists but that has
    # no legacy/PictureRecord coverage badge. Bullet normalization must now
    # drive a deterministic render; previously this table was dropped
    # (verified=False, markdown="").
    rows = [
        ["Stakeholder", "Engagement channels", "Example initiatives"],
        ["Farmers", "■ Strategic partnerships ■ Co-design", "Regenerative agriculture"],
        ["Employees", "■ Annual survey ■ Works councils", "Training programs"],
    ]
    bands = [(0.10, 0.13), (0.20, 0.30), (0.32, 0.42)]
    grid = _structured_grid(rows, header_rows=1, row_bands=bands)
    norm = normalize_table_grid(grid)
    check(norm["classification"] == "regular_table" and norm["bulleted"] is True, "grid normalizes as a bulleted regular_table")
    candidate = _detect_and_render(grid, [0.05, 0.05, 0.99, 0.5], {})
    check(candidate.verified, "a symbol-free bulleted regular table is now deterministically rendered")
    check((candidate.stats or {}).get("format") == "regular_table", "it is stamped regular_table")
    check(
        "- Strategic partnerships<br>- Co-design" in candidate.markdown,
        "its black-square list body cell renders as a <br>-joined - item list",
    )
    source = _raw_twin_markdown(rows, header_rows=1)
    out, enforced = replace_deterministic_tables(source, [candidate])
    check(enforced == [candidate.candidate_id], "the bulleted regular table splices over its raw twin")
    check("- Strategic partnerships<br>- Co-design" in out, "the bulleted cell is present after the splice")


def test_regular_table_with_symbol_and_bullets() -> None:
    # Combined path: a placed coverage badge AND a multi-item ■ cell in the same
    # regular table. Both must survive — the coverage cell shows the symbol and
    # the engagement cell renders a - item list.
    rows = [
        ["Stakeholder", "Engagement channels", "Scope", "ESRS coverage"],
        ["Farmers", "■ Strategic partnerships ■ Co-design", "Value chain", ""],
        ["Employees", "■ Annual survey ■ Works councils", "Own operations", ""],
    ]
    bands = [(0.10, 0.13), (0.20, 0.30), (0.32, 0.42)]
    grid = _structured_grid(rows, header_rows=1, row_bands=bands)
    _, sym = _picture(1, [0.90, 0.23, 0.98, 0.27], "E5")  # centered on record 0 band
    candidate = _detect_and_render(grid, [0.05, 0.05, 0.99, 0.5], {1: sym})
    check((candidate.stats or {}).get("format") == "regular_table", "symbol+bullet regular table renders deterministically")
    farmers = [ln for ln in candidate.markdown.splitlines() if ln.startswith("| Farmers")]
    check(bool(farmers) and farmers[0].rstrip().endswith("E5 |"), "the coverage badge lands in the Farmers coverage cell")
    check(
        "- Strategic partnerships<br>- Co-design" in farmers[0],
        "the same row's engagement cell still renders a - item list",
    )
    check("symbols_unplaced_geometry" not in (candidate.stats or {}), "the badge is placed")


def test_bullet_free_regular_table_without_symbols_still_deferred() -> None:
    # Regression guard: a plain numeric regular table with neither bullets nor
    # symbols must stay deferred to the existing flow (bulleted flag stays False).
    rows = [
        ["Metric", "2024", "2025"],
        ["Sales", "27,376", "27,283"],
        ["Net income", "2,021", "1,825"],
    ]
    bands = [(0.1, 0.13), (0.2, 0.23), (0.24, 0.27)]
    grid = _structured_grid(rows, header_rows=1, row_bands=bands)
    norm = normalize_table_grid(grid)
    check(norm["classification"] == "regular_table" and norm["bulleted"] is False, "a numeric grid gains no bullet flag")
    candidate = _detect_and_render(grid, [0.05, 0.05, 0.95, 0.4], {})
    check(not candidate.verified, "it is not deterministically rendered")
    check((candidate.stats or {}).get("format") is None, "no deterministic format is stamped on it")


def test_source_dash_list_does_not_trigger_takeover() -> None:
    # A cell that already begins with a source ``- `` dash is returned unchanged by
    # _cell_with_bullets, so the bulleted flag must stay False (no accidental
    # capture of a non-grid dash-list table).
    check(_cell_with_bullets("- already a list") == "- already a list", "a source dash cell is untouched")
    rows = [
        ["Item", "Notes", "Owner"],
        ["Alpha", "- already a list", "Team A"],
        ["Beta", "- another list", "Team B"],
    ]
    bands = [(0.1, 0.13), (0.2, 0.23), (0.24, 0.27)]
    grid = _structured_grid(rows, header_rows=1, row_bands=bands)
    norm = normalize_table_grid(grid)
    check(norm["bulleted"] is False, "a pre-dashed cell does not set the bulleted flag")


def test_repeated_strong_bullets_still_split() -> None:
    for glyph in "•▪◦‣■":
        cell = f"{glyph} a {glyph} b"
        check(_cell_with_bullets(cell) == "- a<br>- b", f"a repeated strong bullet U+{ord(glyph):04X} still splits")


def test_leading_weak_glyph_is_a_bullet() -> None:
    check(_cell_with_bullets("· Foo · Bar") == "- Foo<br>- Bar", "a leading middle-dot (U+00B7) marks a weak-bulleted list")
    check(_cell_with_bullets("∙ Alpha ∙ Beta") == "- Alpha<br>- Beta", "a leading bullet-operator (U+2219) marks a weak-bulleted list")


def test_multiple_midtext_middot_unchanged() -> None:
    check(_cell_with_bullets("12·000 · EUR net") == "12·000 · EUR net", "mid-text middle dots never make a fake list")
    check(
        _cell_with_bullets("CuSO4·5H2O and MgSO4·7H2O") == "CuSO4·5H2O and MgSO4·7H2O",
        "chemical formulae with middle dots are left verbatim",
    )


def test_strong_list_preserves_midtext_weak() -> None:
    check(
        _cell_with_bullets("■ x ■ CuSO4·5H2O") == "- x<br>- CuSO4·5H2O",
        "a strong-triggered item keeps its mid-text weak glyph literal",
    )


# --------------------------------------------------------------------------- #
# Tier-1 anchor distinctiveness: a small unrelated table whose only shared cells
# are generic labels / coverage codes must not be spliced over by a large,
# unrelated verified candidate; a genuine distinctive twin still splices.
# --------------------------------------------------------------------------- #


def test_generic_cell_overlap_does_not_splice_unrelated_table() -> None:
    # The large candidate and the small page table share only generic strings
    # (`Financial`, `E1, E3`, `Workers in the value chain`, `E1, E2, E3`,
    # `ESRS coverage`) — ratio 5/6 clears the 0.6 gate, but only one shared cell
    # carries unique content, so the splice must abstain (no takeover).
    candidate = TableCandidate(
        candidate_id="tcX",
        kind="table",
        bbox=[0.05, 0.05, 0.95, 0.5],
        verified=True,
        stats={"format": "regular_table"},
        markdown=(
            "| Metric | Value | ESRS coverage |\n"
            "|---|---|---|\n"
            "| Revenue growth in constant currency across all regions | 4.2% | E1, E3 |\n"
            "| Financial | Shareholders and investors dialogue program | E1, E2, E3 |\n"
            "| Workers in the value chain | Long-term contractual partnerships | E1 |\n"
        ),
    )
    page = (
        "## Some unrelated section\n\n"
        "| Category | ESRS coverage |\n"
        "|---|---|\n"
        "| Financial | E1, E3 |\n"
        "| Workers in the value chain | E1, E2, E3 |\n"
    )
    check(
        _enforce_deterministic_candidate(page, candidate) is None,
        "a generic-cell-only overlap does not anchor the candidate (returns None)",
    )
    out, enforced = replace_deterministic_tables(page, [candidate])
    check(enforced == [], "the unrelated candidate is not enforced")
    check(out == page, "the small foreign table is left untouched (not spliced over)")


def test_distinctive_twin_still_splices() -> None:
    # A genuine twin: the page carries the raw split title/detail rows and the
    # candidate the authoritative merged form. Their shared cells are long/unique,
    # so >= 2 are distinctive and the splice proceeds (currently-anchoring
    # behavior preserved by the distinctiveness floor).
    candidate = TableCandidate(
        candidate_id="tcTwin",
        kind="table",
        bbox=[0.05, 0.2, 0.99, 0.66],
        verified=True,
        stats={"format": "title_detail_table"},
        markdown=(
            "| Topic | Type |\n"
            "|---|---|\n"
            "| Farmer support and regenerative agriculture<br>Long-term programs across the value chain | Positive impact |\n"
            "| Community water stewardship initiatives<br>Protecting shared water resources locally | Positive impact |\n"
        ),
    )
    page = (
        "## Impacts\n\n"
        "| Topic | Type |\n"
        "|---|---|\n"
        "| Farmer support and regenerative agriculture |  |\n"
        "| Long-term programs across the value chain | Positive impact |\n"
        "| Community water stewardship initiatives |  |\n"
        "| Protecting shared water resources locally | Positive impact |\n"
    )
    out, enforced = replace_deterministic_tables(page, [candidate])
    check(enforced == [candidate.candidate_id], "the distinctive twin is enforced")
    check(
        "Farmer support and regenerative agriculture<br>Long-term programs across the value chain" in out,
        "the merged authoritative cell replaces the raw split title/detail rows",
    )
    check(
        "| Farmer support and regenerative agriculture |  |" not in out,
        "the standalone raw title row is gone",
    )
    check("## Impacts" in out, "the unrelated heading is preserved")


# --------------------------------------------------------------------------- #
# Follow-up v3: list-fragment fallback for VLM-restructured deterministic grids
# --------------------------------------------------------------------------- #


_FUZZY_ITEMS = (
    "Build long-term regenerative partnerships",
    "Expand farmer training support",
    "Facilitate quarterly advisory meetings",
    "Co-design local water projects",
    "Improve collective bargaining frameworks",
    "Strengthen workplace wellbeing services",
    "Run annual listening surveys",
    "Establish employee feedback councils",
)


def _fuzzy_candidate() -> TableCandidate:
    square = "\u25a0"
    rows = [
        ["Owner", "Actions", "Contact", "Scope", "Codes"],
        [
            "Farmers",
            f"{square} {_FUZZY_ITEMS[0]} {square} {_FUZZY_ITEMS[1]}",
            f"{square} {_FUZZY_ITEMS[2]} {square} {_FUZZY_ITEMS[3]}",
            "Value chain",
            "",
        ],
        [
            "Employees",
            f"{square} {_FUZZY_ITEMS[4]} {square} {_FUZZY_ITEMS[5]}",
            f"{square} {_FUZZY_ITEMS[6]} {square} {_FUZZY_ITEMS[7]}",
            "Own operations",
            "",
        ],
    ]
    grid = _structured_grid(
        rows,
        header_rows=1,
        row_bands=[(0.10, 0.13), (0.20, 0.32), (0.34, 0.46)],
    )
    candidate = _detect_and_render(grid, [0.05, 0.05, 0.95, 0.50], {})
    check(candidate.verified, "fuzzy fixture produces a verified candidate")
    return candidate


def _restructured_vlm_table(reverse_rows: bool = False) -> str:
    square = "\u25a0"
    rows = [
        (
            "Farmers",
            f"{square} {_FUZZY_ITEMS[0]}<br>{square} {_FUZZY_ITEMS[1]}",
            f"{square} {_FUZZY_ITEMS[2]}<br>{square} {_FUZZY_ITEMS[3]}",
            "Value chain",
        ),
        (
            "Employees",
            f"{square} {_FUZZY_ITEMS[4]}<br>{square} {_FUZZY_ITEMS[5]}",
            f"{square} {_FUZZY_ITEMS[6]}<br>{square} {_FUZZY_ITEMS[7]}",
            "Own operations",
        ),
    ]
    if reverse_rows:
        rows.reverse()
    body = "\n".join("| " + " | ".join(row) + " |" for row in rows)
    return (
        "| Owner | Actions | Contact | Scope |\n"
        "|---|---|---|---|\n"
        f"{body}\n"
    )


def test_restructured_vlm_table_fuzzy_anchored() -> None:
    candidate = _fuzzy_candidate()
    vlm = "Introductory prose.\n\n" + _restructured_vlm_table() + "\nClosing prose.\n"

    out, enforced = replace_deterministic_tables(vlm, [candidate])
    check(enforced == [candidate.candidate_id], "a restructured VLM table is fuzzy-anchored and enforced")
    check(
        out.count("| Owner | Actions | Contact | Scope | Codes |") == 1,
        "the authoritative five-column header appears exactly once",
    )
    check("\u25a0" not in out, "no raw black-square list marker survives the replacement")
    check(missing_verified_table_ids(out, [candidate]) == [], "the fuzzy-enforced table is not reported missing")
    check("Introductory prose." in out and "Closing prose." in out, "surrounding prose is preserved")


def test_fuzzy_anchors_all_duplicate_copies() -> None:
    candidate = _fuzzy_candidate()
    table = _restructured_vlm_table()
    page = f"Before table.\n\n{table}\nBetween tables.\n\n{table}\nAfter table.\n"

    out, enforced = replace_deterministic_tables(page, [candidate])
    check(enforced == [candidate.candidate_id], "duplicate degraded copies are enforced once")
    check(
        out.count("| Owner | Actions | Contact | Scope | Codes |") == 1,
        "all matching copies collapse to one authoritative table",
    )
    check("\u25a0" not in out, "duplicate degraded list markers are removed")
    check(
        all(text in out for text in ("Before table.", "Between tables.", "After table.")),
        "prose around duplicate tables is retained",
    )


def test_list_fragment_forms_are_equivalent() -> None:
    first = "Build sufficiently detailed partner programs"
    second = "Expand sufficiently detailed training support"
    forms = [
        f"\u25a0 {first} \u25a0 {second}",
        f"- {first}<br>- {second}",
        f"  - {first}  <br>  - {second}  ",
    ]
    expected = _distinctive_fragments([forms[0]])
    check(
        all(_distinctive_fragments([form]) == expected for form in forms[1:]),
        "strong-glyph, markdown-list, and whitespace variants share fragments",
    )


def test_weak_middot_fragments_not_split() -> None:
    check(
        _list_fragments("12\u00b7000 units delivered") == ["12\u00b7000 units delivered"],
        "a numeric middle dot remains inside one fragment",
    )
    check(
        _list_fragments("CuSO4\u00b75H2O reagent grade") == ["cuso4\u00b75h2o reagent grade"],
        "a chemical middle dot remains inside one fragment",
    )


def test_fuzzy_matches_reordered_rows() -> None:
    candidate = _fuzzy_candidate()
    out, enforced = replace_deterministic_tables(_restructured_vlm_table(reverse_rows=True), [candidate])
    check(enforced == [candidate.candidate_id], "row order does not prevent a fragment fallback match")
    check(
        out.index("| Farmers |") < out.index("| Employees |"),
        "the replacement restores the deterministic candidate row order",
    )


def test_fuzzy_ignores_generic_headers() -> None:
    candidate = _fuzzy_candidate()
    page = (
        "| Owner | Actions | Contact | Scope |\n|---|---|---|---|\n"
        "| Alpha | Routine status update | Daily note | Global |\n"
        "| Beta | Standard review process | Weekly note | Regional |\n"
    )
    out, enforced = replace_deterministic_tables(page, [candidate])
    check(enforced == [], "generic matching headers do not trigger the fragment fallback")
    check(out == page, "a generic foreign table remains byte-identical")


def test_fuzzy_rejects_unrelated_table() -> None:
    candidate = _fuzzy_candidate()
    page = (
        "| Owner | Actions | Contact | Scope |\n|---|---|---|---|\n"
        "| Gamma | \u25a0 Develop unrelated market planning narrative | "
        "\u25a0 Coordinate unrelated supplier engagement | Global |\n"
        "| Delta | \u25a0 Maintain unrelated governance reporting | "
        "\u25a0 Review unrelated compliance documentation | Regional |\n"
    )
    out, enforced = replace_deterministic_tables(page, [candidate])
    check(enforced == [], "a table with unrelated distinctive fragments is not enforced")
    check(out == page, "an unrelated table remains untouched")


def test_fuzzy_rejects_below_min_matches() -> None:
    candidate = _fuzzy_candidate()
    page = (
        "| Owner | Actions | Contact | Scope |\n|---|---|---|---|\n"
        f"| Gamma | \u25a0 {_FUZZY_ITEMS[0]}<br>\u25a0 {_FUZZY_ITEMS[1]} | "
        f"\u25a0 {_FUZZY_ITEMS[2]} | Global |\n"
    )
    out, enforced = replace_deterministic_tables(page, [candidate])
    check(enforced == [], "three distinctive fragments stay below the fuzzy match floor")
    check(out == page, "a below-floor partial match is not replaced")


def test_fuzzy_rejects_low_precision_span() -> None:
    candidate = _fuzzy_candidate()
    foreign = [
        "Document a separate procurement improvement initiative",
        "Publish a separate community engagement roadmap",
        "Measure a separate supplier resilience program",
        "Coordinate a separate climate adaptation workshop",
        "Review a separate regional governance strategy",
    ]
    page = (
        "| Owner | Actions | Contact | Scope |\n|---|---|---|---|\n"
        f"| Gamma | \u25a0 {_FUZZY_ITEMS[0]}<br>\u25a0 {_FUZZY_ITEMS[1]} | "
        f"\u25a0 {_FUZZY_ITEMS[2]}<br>\u25a0 {_FUZZY_ITEMS[3]} | "
        f"\u25a0 {foreign[0]}<br>\u25a0 {foreign[1]}<br>\u25a0 {foreign[2]}<br>"
        f"\u25a0 {foreign[3]}<br>\u25a0 {foreign[4]} |\n"
    )
    out, enforced = replace_deterministic_tables(page, [candidate])
    check(enforced == [], "a mostly foreign span fails the fuzzy precision gate")
    check(out == page, "a low-precision span is not replaced")


def test_exact_match_still_wins() -> None:
    candidate = _fuzzy_candidate()
    source_rows = (candidate.stats or {})["grid"]["rows"]
    source = _raw_twin_markdown(source_rows, header_rows=1)
    out, enforced = replace_deterministic_tables(source, [candidate])
    check(enforced == [candidate.candidate_id], "an exact raw-grid twin remains enforceable")
    check("\u25a0" not in out, "the exact path still replaces raw list glyphs")


def test_title_detail_detail_normalized_once() -> None:
    rows = [
        ["", "Type", "Coverage"],
        ["Animal welfare", "", ""],
        ["\u25a0 Improve shelter design \u25a0 Improve welfare monitoring", "Negative impact", ""],
        ["Water stewardship", "", ""],
        ["Protect shared water resources", "Positive impact", ""],
    ]
    grid = _structured_grid(
        rows,
        header_rows=1,
        row_bands=[(0.10, 0.13), (0.20, 0.23), (0.24, 0.30), (0.32, 0.35), (0.36, 0.42)],
    )
    normalized = normalize_table_grid(grid)
    first = normalized["records"][0]["cells"][0]
    check(normalized["bulleted"] is True, "title-detail bullet normalization records one bullet takeover")
    check(
        first == "Animal welfare<br>- Improve shelter design<br>- Improve welfare monitoring",
        "the pre-normalized detail is merged without a second bullet transformation",
    )


def _run_all() -> None:
    test_picture_blocks_keep_their_own_index_and_value()
    test_full_grid_reaches_stats_and_verifier()
    test_title_detail_normalizes_to_one_row_and_one_header()
    test_bullet_detail_survives_as_one_record()
    test_single_header_title_detail_is_not_sectioned()
    test_genuine_sectioned_table_still_splits()
    test_regular_policy_grid_stays_a_table()
    test_regular_grid_without_symbols_is_left_alone()
    test_end_to_end_candidate_places_symbols_in_coverage_cell()
    test_ambiguous_symbol_is_left_unplaced_not_sequenced()
    test_cross_record_rowspan_bbox_does_not_expand_record_bands()
    test_bbox_reused_inside_one_title_detail_record_is_kept()
    test_incomplete_deterministic_candidate_is_not_authoritative()
    test_visual_enforce_gate_uses_shared_authority_callers()
    test_symbol_value_never_becomes_a_standalone_line()
    test_loose_symbol_lines_are_stripped_without_touching_real_images()
    test_right_edge_navigation_image_is_omitted_content_image_kept()
    test_placed_coverage_value_is_not_reported_unplaced()
    test_right_edge_dash_run_but_lone_dash_kept()
    # Phase 1: multiple symbol labels join in visual order, comma-separated
    test_multiple_symbols_join_in_visual_reading_order()
    test_repeated_symbol_value_is_de_duplicated()
    test_single_symbol_gains_no_delimiter()
    test_multi_code_symbol_value_stays_comma_joined_in_a_coverage_cell()
    test_tagged_inserted_cell_is_not_appended_to_by_legacy_symbols()
    test_comma_joined_coverage_symbols_count_as_placed()
    # Phase 2: bullet lists inside body cells + lifted 400-char truncation
    test_black_square_bullets_are_preserved()
    test_regular_table_body_cells_get_bullet_lists()
    test_double_glyph_collapses_to_one_item()
    test_single_mid_text_glyph_is_left_alone()
    test_long_cell_survives_uncapped_end_to_end()
    test_bullet_cell_survives_postprocess_end_to_end()
    # B1: deterministic splice for regular/title_detail verified tables
    test_deterministic_title_detail_replaces_raw_twin()
    test_deterministic_regular_table_with_symbols_replaces_twin()
    test_deterministic_table_missing_is_not_appended()
    test_deterministic_table_postprocess_end_to_end()
    # B2: structural invariants in the VLM fallback verifier
    test_grid_verifier_accepts_faithful_transcription()
    test_grid_verifier_rejects_wrong_column_count()
    test_grid_verifier_rejects_repeated_header_in_body()
    test_grid_verifier_rejects_dropped_rows()
    test_grid_verifier_untouched_without_grid()
    # Follow-up v2: deterministic bullets for symbol-free regular tables +
    # leading-only middle-dot rule
    test_symbol_free_regular_table_renders_bullets_end_to_end()
    test_regular_table_with_symbol_and_bullets()
    test_bullet_free_regular_table_without_symbols_still_deferred()
    test_source_dash_list_does_not_trigger_takeover()
    test_repeated_strong_bullets_still_split()
    test_leading_weak_glyph_is_a_bullet()
    test_multiple_midtext_middot_unchanged()
    test_strong_list_preserves_midtext_weak()
    # Tier-1 anchor distinctiveness floor (generic-cell false-positive splice)
    test_generic_cell_overlap_does_not_splice_unrelated_table()
    test_distinctive_twin_still_splices()
    # Follow-up v3: list-aware fallback when a VLM restructures a grid table
    test_restructured_vlm_table_fuzzy_anchored()
    test_fuzzy_anchors_all_duplicate_copies()
    test_list_fragment_forms_are_equivalent()
    test_weak_middot_fragments_not_split()
    test_fuzzy_matches_reordered_rows()
    test_fuzzy_ignores_generic_headers()
    test_fuzzy_rejects_unrelated_table()
    test_fuzzy_rejects_below_min_matches()
    test_fuzzy_rejects_low_precision_span()
    test_exact_match_still_wins()
    test_title_detail_detail_normalized_once()
    print("test_policy_table_regressions: all checks passed")


if __name__ == "__main__":
    _run_all()
