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
    insert_image_references_and_summaries,
    replace_deterministic_tables,
)
from document_extract.markdown.postprocess import (
    filter_unplaced_lines,
    split_sectioned_grid,
)
from document_extract.models import PictureRecord, TableCandidate
from document_extract.refinement import postprocess_markdown
from document_extract.tables import (
    build_table_candidates,
    normalize_table_grid,
    render_deterministic_docling_table,
    render_grid_markdown,
    verify_region_table,
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
    test_symbol_value_never_becomes_a_standalone_line()
    test_right_edge_navigation_image_is_omitted_content_image_kept()
    test_placed_coverage_value_is_not_reported_unplaced()
    test_right_edge_dash_run_but_lone_dash_kept()
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
    print("test_policy_table_regressions: all checks passed")


if __name__ == "__main__":
    _run_all()
