"""Table-reconstruction checks: row ownership, invariants, and abstention.

The three Danone fixtures are frozen serializations of a real Docling run plus
the source page's own rule/line geometry; the PDF itself stays external. Every
other case is synthetic so the suite needs no Docling, GPU, PDF, or Ollama.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from document_extract.markdown.formatting import replace_deterministic_tables  # noqa: E402
from document_extract.models import TableCandidate, VisualCandidate  # noqa: E402
from document_extract.table_reconstruction import (  # noqa: E402
    RECONSTRUCTION_VERSION,
    _slot_collisions,
    _tokens,
    _tokens_preserved,
    filter_visible_rule_segments,
    reconcile_table_grid,
)
from document_extract.tables import (  # noqa: E402
    build_table_candidates,
    normalize_table_grid,
    render_deterministic_docling_table,
    render_grid_markdown,
    verify_region_table,
)
from document_extract.visual_values import associate_table_cells  # noqa: E402


def run_lengths(column: list[str]) -> list[int]:
    """Consecutive run lengths of identical cell text (the group spans)."""
    from itertools import groupby

    return [len(list(group)) for _, group in groupby(column)]

FIXTURES = Path(__file__).parent / "fixtures" / "table_reconstruction"


def load(page: int) -> dict:
    return json.loads((FIXTURES / f"page_{page:04d}.json").read_text(encoding="utf-8"))


def reconcile(fixture: dict, scale: float = 1.0) -> dict:
    """Run the reconciler, optionally after scaling every source coordinate.

    Tolerances are relative, so a uniform scale must not change the topology.
    """
    if scale != 1.0:
        fixture = json.loads(json.dumps(fixture))
        width, height = fixture["page_size"]
        fixture["page_size"] = [width * scale, height * scale]
        for cell in fixture["raw_grid"]["cells"]:
            if cell.get("bbox"):
                for key in ("l", "t", "r", "b"):
                    cell["bbox"][key] *= scale
    return reconcile_table_grid(
        fixture["raw_grid"],
        fixture["table_bbox"],
        fixture["geometry"],
        tuple(fixture["page_size"]),
    )


def body(result: dict) -> list[list[str]]:
    grid = result["grid"]
    return grid["rows"][int(grid["header_rows"]) :]


def cell(result: dict, row: int, column: int) -> str:
    return body(result)[row][column]


def synthetic_reconcile(
    *,
    left_rows: tuple[str, str],
    left_lines: list[tuple[int, str, list[float]]],
    left_span: bool = False,
    shared_block: bool = False,
) -> dict:
    """A two-band table with a divider that stops before column zero."""
    rows = [["Group", "Value"], [left_rows[0], "Upper"], [left_rows[1], "Lower"]]
    cells: list[dict] = []
    for row, values in enumerate(rows):
        for column, text in enumerate(values):
            is_left_span = left_span and column == 0 and row > 0
            cells.append(
                {
                    "r": row,
                    "c": column,
                    "text": text,
                    "bbox": {
                        "l": 0 if column == 0 else 55,
                        "t": 10 if is_left_span else (0 if row == 0 else 10 + (row - 1) * 20),
                        "r": 40 if column == 0 else 95,
                        "b": 50 if is_left_span else (10 if row == 0 else 10 + row * 20),
                        "origin": "TOPLEFT",
                    },
                    "column_header": row == 0,
                    "start_row_offset_idx": 1 if is_left_span else row,
                    "end_row_offset_idx": 3 if is_left_span else row + 1,
                    "start_col_offset_idx": column,
                    "end_col_offset_idx": column + 1,
                    "row_span": 2 if is_left_span else 1,
                    "col_span": 1,
                }
            )
    lines = [
        {"id": f"b{block}l0", "block": block, "text": text, "rect": rect}
        for block, text, rect in left_lines
    ]
    lines.extend(
        [
            {
                "id": "b20l0",
                "block": 10 if shared_block else 20,
                "text": "Upper",
                "rect": [0.6, 0.17, 0.9, 0.22],
            },
            {"id": "b21l0", "block": 21, "text": "Lower", "rect": [0.6, 0.38, 0.9, 0.43]},
        ]
    )
    return reconcile_table_grid(
        {"rows": rows, "num_cols": 2, "header_rows": 1, "cells": cells},
        [0.0, 0.1, 1.0, 0.5],
        {"rules": [[0.1, 0.0, 1.0], [0.3, 0.5, 1.0], [0.5, 0.0, 1.0]], "lines": lines},
        (100.0, 100.0),
    )


def test_partial_rule_blocked_by_crossing_text_creates_rowspan() -> None:
    result = synthetic_reconcile(
        left_rows=("One", "Two"),
        left_lines=[(10, "One Two", [0.1, 0.27, 0.4, 0.33])],
    )
    assert body(result) == [["One Two", "Upper"], ["One Two", "Lower"]]
    assert result["audit"]["column_boundaries"]["0"][0]["status"] == "blocked_by_text"


def test_partial_rule_extends_only_through_a_clear_raw_split_corridor() -> None:
    result = synthetic_reconcile(
        left_rows=("One", "Two"),
        left_lines=[
            (10, "One", [0.1, 0.17, 0.4, 0.22]),
            (11, "Two", [0.1, 0.38, 0.4, 0.43]),
        ],
        shared_block=True,
    )
    assert body(result) == [["One", "Upper"], ["Two", "Lower"]]
    assert result["audit"]["column_boundaries"]["0"][0]["status"] == "corridor_extended"
    assert result["audit"]["source_content"]["0"]["component_ids"] == ["b10:c0", "b11:c0"]
    assert result["audit"]["source_content"]["1"]["component_ids"] == ["b10:c1", "b21:c1"]


def test_existing_raw_rowspan_wins_over_a_clear_corridor() -> None:
    result = synthetic_reconcile(
        left_rows=("One Two", "One Two"),
        left_lines=[(10, "One Two", [0.1, 0.17, 0.4, 0.22])],
        left_span=True,
    )
    assert body(result) == [["One Two", "Upper"], ["One Two", "Lower"]]
    assert result["audit"]["column_boundaries"]["0"][0]["status"] == "preserved_raw_span"


def test_non_subsequence_source_text_is_never_inserted() -> None:
    result = synthetic_reconcile(
        left_rows=("One", "Two"),
        left_lines=[
            (10, "Source", [0.1, 0.17, 0.4, 0.22]),
            (11, "Other", [0.1, 0.38, 0.4, 0.43]),
        ],
    )
    assert result["audit"]["source_content"]["0"]["status"] == "untrusted"
    assert "Source" not in " ".join(row[0] for row in body(result))


def test_token_boundary_equivalence_accepts_only_identical_streams() -> None:
    """Glued/split tokens are the same text; a moved word is not."""
    assert _tokens_preserved(
        _tokens("Equipandempower communities ( i.e. , internal)"),
        _tokens("Equip and empower communities (i.e., internal)"),
    )
    # p175: same characters, wrong order. It must stay untrusted.
    assert not _tokens_preserved(
        _tokens("capabilities of the future to in a fast-changing develop economy"),
        _tokens("capabilities of the future to develop in a fast-changing economy"),
    )


def test_danone_183_merges_the_unsupported_row() -> None:
    """No PDF rule sits near y=495, so the final bullet and the initiative tail
    belong to the Academia record rather than a fourth one."""
    result = reconcile(load(183))
    assert result["status"] == "repaired", result["audit"]
    assert len(body(result)) == 3, body(result)

    academia = body(result)[2]
    assert academia[1] == "Academia and Scientific Bodies"
    assert "Danone Ethics Line (DEL)" in academia[2]
    assert academia[3].endswith("the value chain.")
    # The partial rule does not divide Engagement channels: one source-proven
    # rowspan is repeated in the two logical stakeholder records.
    industry, ngo, _ = body(result)
    assert industry[2] == ngo[2]
    assert all(
        phrase in industry[2]
        for phrase in (
            "Strategic partnerships",
            "Regular consultations",
            "multi- stakeholder coalitions",
            "Danone Ethics Line (DEL)",
        )
    )
    assert "Dairy Methane Action Alliance" in ngo[3]
    assert "L3F Livelihoods Fund" in ngo[3]
    # The merged group label spans every record; no rule divides its column.
    assert [row[0] for row in body(result)] == ["Civil Society and Other Organizations"] * 3
    assert result["audit"]["column_boundaries"]["2"][0]["status"] == "blocked_by_text"


def test_danone_184_keeps_its_structure_and_restores_source_text() -> None:
    """The control topology is retained, but Docling's missing sentence tail is
    restored from a source-proven initiative column."""
    fixture = load(184)
    result = reconcile(fixture)
    assert result["status"] == "repaired", result["audit"]
    assert len(body(result)) == 4
    assert [row[0] for row in body(result)] == [
        "Affected Communities",
        "Public Authorities and Political Decision-Makers",
        "Own Workforce Employees",
        "External workforce",
    ]
    assert "suppliers to transition to regenerative agriculture practices." in body(result)[1][2]
    assert result["audit"]["column_boundaries"]["0"][-1]["status"] == "corridor_extended"


def test_danone_185_splits_at_rules_and_keeps_components_whole() -> None:
    result = reconcile(load(185))
    assert result["status"] == "repaired", result["audit"]
    assert len(body(result)) == 4, body(result)

    farmer, supplier, consumer, financial = body(result)

    # The Farmer sentence Docling cut in half is one cell again.
    assert "Agricultural sustainability is one of the identified pillars" in farmer[3]
    assert farmer[3].endswith("under the Partner for Growth.")
    assert "sustainability is one of" not in supplier[3]

    # Supplier content Docling pushed down into the Consumer row stays Supplier.
    # Source-complete reconstruction also restores the omitted bullet prefix.
    assert "Capacity-building workshops" in supplier[2]
    assert "workshops" in supplier[2]
    assert "Road Transport Due Diligence Foundation (RTDD)" in supplier[3]
    assert "workshops" not in consumer[2]
    assert "(RTDD)" not in consumer[3]

    # Consumer bullets stay Consumer.
    assert consumer[2].startswith("■ Local contact centers")
    assert consumer[3].startswith("■ Engagement through local contact centers")

    # One owner, not two overlapping first-column cells.
    assert financial[0] == "Financial Community and Stakeholders"

    # The rule at y=238.12 never crosses column 0, so the Workers label spans
    # the two records its neighbours are divided across.
    assert farmer[0] == "Workers in the value chain"
    assert supplier[0] == "Workers in the value chain"


def test_scale_invariance() -> None:
    """Uniform 0.5x/1x/2x rescaling must not change the reconstructed topology."""
    for page in (183, 184, 185, 190, 199, 200):
        fixture = load(page)
        baseline = reconcile(fixture)
        for scale in (0.5, 2.0):
            scaled = reconcile(fixture, scale=scale)
            assert scaled["status"] == baseline["status"], (page, scale)
            assert scaled["grid"]["rows"] == baseline["grid"]["rows"], (page, scale)


def test_danone_199_spanning_labels_repeat_into_every_covered_row() -> None:
    """Each green group label is one spanning cell repeated identically into
    every KPI row it covers (spans 3/2/2/3/1), and the genuine sub-row value
    join stays a join -- no fragment, no glued cross-row cell."""
    result = reconcile(load(199))
    assert result["status"] == "repaired", result["audit"]
    rows = body(result)
    assert len(rows) == 11
    column0 = [row[0] for row in rows]
    assert run_lengths(column0) == [3, 2, 2, 3, 1]
    # Every label carries its whole text, not a leading/trailing fragment.
    assert "Curb GHG emissions" in column0[0] and "methane reduction" in column0[0]
    assert column0[0] == column0[1] == column0[2]
    assert "Preserve and restore watersheds" in column0[5] and "value chain" in column0[5]
    assert column0[5] == column0[6]
    # Two sub-rows genuinely share one band: that join is allowed to stand.
    assert rows[0][4] == "2030 2050 (b)"
    # Docling glued "recover as much"; the identical source stream is admitted
    # through token-boundary equivalence and replaces it everywhere.
    entry = result["audit"]["source_content"]["0"]
    assert entry["status"] == "verified"
    assert entry["match"] == "token_boundary_equivalent"
    assert all("recover as much as Danone uses" in cell for cell in column0[7:10])
    assert column0[7] == column0[8] == column0[9]
    assert "recoverasmuch" not in " ".join(cell for row in rows for cell in row)


def test_danone_200_no_cell_glues_two_rows() -> None:
    """Group labels span 5/2/2; the collision fallback un-glues the value cells
    Docling stacked into one band, and the emptied neighbours are refilled."""
    result = reconcile(load(200))
    assert result["status"] == "repaired", result["audit"]
    rows = body(result)
    assert len(rows) == 9
    assert run_lengths([row[0] for row in rows]) == [5, 2, 2]
    # None of the v2 glue artefacts survive anywhere in the table.
    flat = [cell for row in rows for cell in row]
    for glued in ("2025 2025", "ongoing ongoing", "15.0% 96.9% (j)", "N/A (a) N/A (a)"):
        assert glued not in flat, glued
    # The column the collision emptied now carries content in every KPI row.
    assert all(row[8].strip() for row in rows[:-1])
    assert result["audit"]["slot_collisions"]
    # Docling split the parenthetical; the source stream is the same characters,
    # so column 0 is admitted through token-boundary equivalence.
    entry = result["audit"]["source_content"]["0"]
    assert entry["status"] == "verified"
    assert entry["match"] == "token_boundary_equivalent"
    label = rows[5][0]
    assert "(i.e., internal, external)" in label
    assert "( i.e. , internal, external)" not in " ".join(flat)


def test_danone_190_repaired_grid_is_authoritative_over_raw() -> None:
    """R5a: a repaired title/detail table is reclassified from the accepted grid,
    so it is no longer 'sectioned' and the deterministic render carries every
    description the reconciler restored -- including Docling's two dropped tails.
    """
    fixture = load(190)
    result = reconcile(fixture)
    assert result["status"] == "repaired", result["audit"]
    assert len(body(result)) == 9

    layout_map = {
        "blocks": [
            {
                "id": "b0002",
                "type": "table",
                "bbox": fixture["table_bbox"],
                "_table_grid": fixture["raw_grid"],
                # Pretend prepare-time misclassified it as sectioned off the raw
                # grid; R5a must override that from the accepted grid.
                "_sectioned_table": {"markdown": "STALE", "split": {}},
            }
        ]
    }
    candidates = build_table_candidates(
        cells=[],
        page_size=tuple(fixture["page_size"]),
        picture_records={},
        layout_map=layout_map,
        page_geometry=fixture["geometry"],
    )
    candidate = candidates[0]
    assert candidate.stats.get("format") != "sectioned_table"
    assert candidate.markdown != "STALE"
    assert (
        render_deterministic_docling_table(candidate, {}, tuple(fixture["page_size"])) is True
    )
    for fragment in (
        "local communities and indigenous peoples",
        "in response to protests or advocacy activities",
    ):
        assert fragment in candidate.markdown, fragment


def _spanning_label_fixture(*, two_components: bool) -> dict:
    """A 3-band, 2-column table whose left label Docling split into two owners.

    Rules cross only the value column, so the label column is one undivided
    region. With one source block the two owners are fragments of one spanning
    cell (merge); with two blocks they are separate cells sharing the region
    (never merged). ``Delta`` is Docling-only, forcing the untrusted path.
    """
    rows = [["Label", "Val"], ["Alpha", "one"], ["Beta Gamma Delta", "two"], ["", "three"]]

    def bbox(left, top, right, bottom):
        return {"l": left, "t": top, "r": right, "b": bottom, "origin": "TOPLEFT"}

    cells = [
        {"r": 0, "c": 0, "text": "Label", "bbox": bbox(2, 2, 18, 5), "column_header": True,
         "start_row_offset_idx": 0, "end_row_offset_idx": 1,
         "start_col_offset_idx": 0, "end_col_offset_idx": 1, "row_span": 1, "col_span": 1},
        {"r": 0, "c": 1, "text": "Val", "bbox": bbox(30, 2, 95, 5), "column_header": True,
         "start_row_offset_idx": 0, "end_row_offset_idx": 1,
         "start_col_offset_idx": 1, "end_col_offset_idx": 2, "row_span": 1, "col_span": 1},
        {"r": 1, "c": 0, "text": "Alpha", "bbox": bbox(2, 14, 18, 19), "column_header": False,
         "start_row_offset_idx": 1, "end_row_offset_idx": 2,
         "start_col_offset_idx": 0, "end_col_offset_idx": 1, "row_span": 1, "col_span": 1},
        {"r": 2, "c": 0, "text": "Beta Gamma Delta", "bbox": bbox(2, 37, 18, 64),
         "column_header": False, "start_row_offset_idx": 2, "end_row_offset_idx": 4,
         "start_col_offset_idx": 0, "end_col_offset_idx": 1, "row_span": 2, "col_span": 1},
        {"r": 3, "c": 0, "text": "Beta Gamma Delta", "bbox": bbox(2, 37, 18, 64),
         "column_header": False, "start_row_offset_idx": 2, "end_row_offset_idx": 4,
         "start_col_offset_idx": 0, "end_col_offset_idx": 1, "row_span": 2, "col_span": 1},
    ]
    for row, text in ((1, "one"), (2, "two"), (3, "three")):
        top = 14 + (row - 1) * 23
        cells.append(
            {"r": row, "c": 1, "text": text, "bbox": bbox(30, top, 95, top + 5),
             "column_header": False, "start_row_offset_idx": row, "end_row_offset_idx": row + 1,
             "start_col_offset_idx": 1, "end_col_offset_idx": 2, "row_span": 1, "col_span": 1}
        )

    left_blocks = (5, 6, 6) if two_components else (5, 5, 5)
    # Header source lines keep the column's alignment above threshold, as on a
    # real page; without them the unmatched header token would abstain the column.
    lines = [
        {"id": "b4c0", "block": 4, "text": "Label", "rect": [0.02, 0.055, 0.18, 0.075]},
        {"id": "b4c1", "block": 4, "text": "Val", "rect": [0.30, 0.055, 0.95, 0.075]},
    ]
    for (block, text, y) in zip(left_blocks, ("Alpha", "Beta", "Gamma"), (0.16, 0.39, 0.62)):
        lines.append({"id": f"b{block}c0", "block": block, "text": text,
                      "rect": [0.02, y - 0.02, 0.18, y + 0.02]})
    for block, text, y in ((7, "one", 0.16), (8, "two", 0.39), (9, "three", 0.62)):
        lines.append({"id": f"b{block}c1", "block": block, "text": text,
                      "rect": [0.30, y - 0.02, 0.95, y + 0.02]})

    grid = {"rows": rows, "num_cols": 2, "header_rows": 1, "cells": cells}
    geometry = {
        "rules": [[0.05, 0.25, 1.0], [0.28, 0.25, 1.0], [0.51, 0.25, 1.0], [0.74, 0.25, 1.0]],
        "lines": lines,
    }
    return reconcile_table_grid(grid, [0.0, 0.05, 1.0, 0.75], geometry, (100.0, 100.0))


def test_r2_merges_one_component_into_a_spanning_cell() -> None:
    result = _spanning_label_fixture(two_components=False)
    assert result["status"] == "repaired", result["audit"]
    column0 = [row[0] for row in body(result)]
    assert column0 == ["Alpha Beta Gamma Delta"] * 3
    assert result["audit"]["merged_fragments"] == [[[1, 0], [2, 0]]]


def test_r2_refuses_to_merge_two_components_sharing_a_region() -> None:
    """Two owners mapping to different source blocks stay two cells (page 183's
    control): the label is not fabricated into one span."""
    result = _spanning_label_fixture(two_components=True)
    assert result["status"] == "repaired", result["audit"]
    column0 = [row[0] for row in body(result)]
    assert column0[0] == "Alpha"
    assert column0[1] == column0[2] == "Beta Gamma Delta"
    assert not result["audit"]["merged_fragments"]


def _mixed_span_fixture() -> tuple[dict, dict]:
    """Two raw rows become three while horizontal and vertical spans survive."""

    def entry(
        row: int,
        column: int,
        text: str,
        rect: tuple[float, float, float, float],
        *,
        row_start: int | None = None,
        row_end: int | None = None,
        col_start: int | None = None,
        col_end: int | None = None,
        header: bool = False,
    ) -> dict:
        row_start = row if row_start is None else row_start
        row_end = row + 1 if row_end is None else row_end
        col_start = column if col_start is None else col_start
        col_end = column + 1 if col_end is None else col_end
        left, top, right, bottom = rect
        return {
            "r": row,
            "c": column,
            "text": text,
            "bbox": {
                "l": left,
                "t": top,
                "r": right,
                "b": bottom,
                "origin": "TOPLEFT",
            },
            "column_header": header,
            "start_row_offset_idx": row_start,
            "end_row_offset_idx": row_end,
            "start_col_offset_idx": col_start,
            "end_col_offset_idx": col_end,
            "row_span": row_end - row_start,
            "col_span": col_end - col_start,
        }

    rows = [
        ["Label", "Baseline", "Baseline", "Result"],
        ["Group", "Wide body", "Wide body", "X"],
        ["Group", "Y Z", "Q R", "W V"],
    ]
    cells = [entry(0, 0, "Label", (2, 2, 18, 8), header=True)]
    cells.extend(
        entry(
            0, column, "Baseline", (24, 2, 70, 8),
            col_start=1, col_end=3, header=True,
        )
        for column in (1, 2)
    )
    cells.append(entry(0, 3, "Result", (76, 2, 98, 8), header=True))
    cells.extend(
        entry(row, 0, "Group", (2, 12, 18, 53), row_start=1, row_end=3)
        for row in (1, 2)
    )
    cells.extend(
        entry(
            1, column, "Wide body", (24, 12, 70, 22), col_start=1, col_end=3
        )
        for column in (1, 2)
    )
    cells.extend(
        [
            entry(1, 3, "X", (76, 12, 98, 22)),
            entry(2, 1, "Y Z", (24, 29, 46, 52)),
            entry(2, 2, "Q R", (50, 29, 70, 52)),
            entry(2, 3, "W V", (76, 29, 98, 52)),
        ]
    )
    grid = {"rows": rows, "num_cols": 4, "header_rows": 1, "cells": cells}
    geometry = {
        "rules": [
            [0.10, 0.22, 1.0],
            [0.25, 0.22, 1.0],
            [0.40, 0.22, 1.0],
            [0.55, 0.22, 1.0],
        ],
        "lines": [
            {"id": "g", "block": 1, "text": "Group", "rect": [0.02, 0.12, 0.18, 0.53]},
            {"id": "wide", "block": 2, "text": "Wide body", "rect": [0.24, 0.14, 0.70, 0.20]},
            {"id": "x", "block": 3, "text": "X", "rect": [0.76, 0.14, 0.98, 0.20]},
            {"id": "y", "block": 4, "text": "Y", "rect": [0.24, 0.29, 0.46, 0.34]},
            {"id": "z", "block": 5, "text": "Z", "rect": [0.24, 0.44, 0.46, 0.49]},
            {"id": "q", "block": 6, "text": "Q", "rect": [0.50, 0.29, 0.70, 0.34]},
            {"id": "r", "block": 7, "text": "R", "rect": [0.50, 0.44, 0.70, 0.49]},
            {"id": "w", "block": 8, "text": "W", "rect": [0.76, 0.29, 0.98, 0.34]},
            {"id": "v", "block": 9, "text": "V", "rect": [0.76, 0.44, 0.98, 0.49]},
        ],
    }
    return grid, geometry


def test_horizontal_spans_survive_row_reconstruction_and_rendering() -> None:
    grid, geometry = _mixed_span_fixture()
    result = reconcile_table_grid(grid, [0.0, 0.10, 1.0, 0.55], geometry, (100.0, 100.0))
    assert result["status"] == "repaired", result["audit"]
    assert result["audit"]["horizontal_span_columns"] == [1, 2]
    assert {
        result["audit"]["source_content"][str(column)]["status"]
        for column in (1, 2)
    } == {"span_preserved"}
    assert body(result) == [
        ["Group", "Wide body", "Wide body", "X"],
        ["Group", "Y", "Q", "W"],
        ["Group", "Z", "R", "V"],
    ]
    assert all(len(row) == 4 for row in result["grid"]["rows"])

    candidate = build_table_candidates(
        cells=[],
        page_size=(100.0, 100.0),
        picture_records={},
        layout_map={
            "blocks": [
                {
                    "id": "b0001",
                    "type": "table",
                    "bbox": [0.0, 0.10, 1.0, 0.55],
                    "_table_grid": grid,
                    "grid_rows": grid["rows"],
                }
            ]
        },
        page_geometry=geometry,
    )[0]
    assert candidate.stats["grid_raw"] == grid
    # A span_preserved column is unresolved source text, so it defers
    # deterministic authority; the reconstructed grid still renders correctly.
    assert render_deterministic_docling_table(candidate, {}, (100.0, 100.0)) is False
    markdown = render_grid_markdown(normalize_table_grid(candidate.stats["grid"]))
    assert "| Label | Baseline | Baseline | Result |" in markdown
    assert "| Group | Wide body | Wide body | X |" in markdown
    assert markdown.count("| Group |") == 3


def test_r4_slot_collision_detection() -> None:
    """A slot join is legal only when its segments share a home band."""
    same = [
        {"col_start": 4, "band_low": 0, "text": "2030", "home_band": 0},
        {"col_start": 4, "band_low": 0, "text": "2050", "home_band": 0},
    ]
    assert _slot_collisions(same) == {}
    displaced = [
        {"col_start": 8, "band_low": 6, "text": "15.0%", "home_band": 6},
        {"col_start": 8, "band_low": 6, "text": "96.9%", "home_band": 7},
    ]
    assert _slot_collisions(displaced) == {8: [6]}


def test_r1_filters_only_invisible_rule_segments() -> None:
    """A rule is kept only where the render shows a line: a drawn line or a
    colour boundary survives, a uniform fill or occluded stroke does not."""
    from PIL import Image, ImageDraw

    width, height, y = 120, 120, 60
    rule = [[0.5, 0.0, 1.0]]
    mlh = 0.06

    uniform = Image.new("RGB", (width, height), (200, 200, 200))
    kept, dropped = filter_visible_rule_segments(rule, uniform, mlh)
    assert kept == [] and len(dropped) == 1

    dark = Image.new("RGB", (width, height), (255, 255, 255))
    ImageDraw.Draw(dark).line((0, y, width, y), fill=(0, 0, 0), width=1)
    assert filter_visible_rule_segments(rule, dark, mlh)[0] == rule

    faint = Image.new("RGB", (width, height), (255, 255, 255))
    ImageDraw.Draw(faint).line((0, y, width, y), fill=(210, 210, 210), width=1)
    assert filter_visible_rule_segments(rule, faint, mlh)[0] == rule

    boundary = Image.new("RGB", (width, height), (255, 255, 255))
    ImageDraw.Draw(boundary).rectangle((0, 0, width, y), fill=(120, 160, 120))
    assert filter_visible_rule_segments(rule, boundary, mlh)[0] == rule


def test_r1_unreadable_image_leaves_rules_untouched() -> None:
    kept, dropped = filter_visible_rule_segments([[0.5, 0.0, 1.0]], "/no/such/file.png", 0.05)
    assert kept is None and dropped == []


def test_r5b_word_coverage_gate_rejects_label_only_transcription() -> None:
    """A VLM answer may add symbols but must keep the grid's prose."""
    grid = {
        "rows": [
            ["Risk", "Description"],
            ["Land rights", "violations by suppliers harm local communities"],
            ["Food safety", "adverse health impacts from contamination hazards"],
        ],
        "num_cols": 2,
        "header_rows": 1,
        "cells": [
            {"r": r, "c": c, "text": text, "start_row_offset_idx": r, "end_row_offset_idx": r + 1,
             "start_col_offset_idx": c, "end_col_offset_idx": c + 1}
            for r, row in enumerate(
                [["Risk", "Description"],
                 ["Land rights", "violations by suppliers harm local communities"],
                 ["Food safety", "adverse health impacts from contamination hazards"]]
            )
            for c, text in enumerate(row)
        ],
    }
    cell_texts = [cell for row in grid["rows"][1:] for cell in row]
    label_only = (
        "| Risk | Description |\n| --- | --- |\n| Land rights |  |\n| Food safety |  |\n"
    )
    faithful = (
        "| Risk | Description |\n| --- | --- |\n"
        "| Land rights | violations by suppliers harm local comunities |\n"
        "| Food safety | adverse health impacts from contamination hazards |\n"
    )
    ok, stats = verify_region_table(label_only, cell_texts, grid)
    assert ok is False and stats["fail"] == "word_coverage_low"
    ok, stats = verify_region_table(faithful, cell_texts, grid)
    assert ok is True, stats


def test_invariants_hold_on_every_fixture() -> None:
    for page in (183, 184, 185, 190, 199, 200):
        result = reconcile(load(page))
        grid = result["grid"]
        header_rows = int(grid["header_rows"])
        by_column: dict[int, list[tuple[tuple[int, int], dict]]] = {}
        for entry in grid["cells"]:
            if entry["r"] < header_rows:
                continue
            key = (entry["start_row_offset_idx"], entry["start_col_offset_idx"])
            by_column.setdefault(entry["c"], []).append((key, entry))

        for column, entries in by_column.items():
            owners = {key: entry for key, entry in entries}
            for key, entry in owners.items():
                # Spans are rectangular and contiguous.
                assert entry["end_row_offset_idx"] > entry["start_row_offset_idx"]
                assert entry["row_span"] == (
                    entry["end_row_offset_idx"] - entry["start_row_offset_idx"]
                )
            # Two distinct non-spanning owners never overlap vertically. A
            # spanning owner is excluded by definition: the raw control grid
            # already pairs a rowspan with a cell inside its own span.
            boxes = [
                (entry["start_row_offset_idx"], entry["end_row_offset_idx"])
                for key, entry in owners.items()
                if entry["text"].strip() and entry["row_span"] == 1
            ]
            for i, (top, bottom) in enumerate(boxes):
                for other_top, other_bottom in boxes[i + 1 :]:
                    assert bottom <= other_top or other_bottom <= top, (page, column)


def test_raw_text_is_retained_and_verified_source_text_is_complete() -> None:
    """Raw wording is retained as a subsequence; proven columns equal source."""
    for page in (183, 184, 185, 190, 199, 200):
        fixture = load(page)
        result = reconcile(fixture)

        def stream(grid: dict) -> dict[int, list[str]]:
            out: dict[int, list[str]] = {}
            for column in range(int(grid["num_cols"])):
                seen: set[tuple[int, int]] = set()
                tokens: list[str] = []
                for entry in grid["cells"]:
                    if entry["c"] != column or entry["r"] < int(grid["header_rows"]):
                        continue
                    key = (entry["start_row_offset_idx"], entry["start_col_offset_idx"])
                    if key in seen:
                        continue
                    seen.add(key)
                    tokens.extend(_tokens(str(entry["text"])))
                out[column] = tokens
            return out

        raw = stream(fixture["raw_grid"])
        rebuilt = stream(result["grid"])
        for column, tokens in raw.items():
            # Raw wording survives verbatim, or retokenized into the identical
            # character stream -- never partially.
            assert _tokens_preserved(tokens, rebuilt[column]), (page, column)
        for column, audit in result["audit"]["source_content"].items():
            if audit["status"] == "verified":
                assert audit["output_tokens"] == audit["source_tokens"], (page, column)


def test_abstains_without_geometry() -> None:
    """No rules, no lines, no change: absent evidence is never a reason to act."""
    fixture = load(185)
    for geometry in ({"rules": [], "lines": []}, None):
        result = reconcile_table_grid(
            fixture["raw_grid"], fixture["table_bbox"], geometry, tuple(fixture["page_size"])
        )
        assert result["status"] == "unchanged"
        assert result["grid"] == fixture["raw_grid"]


def test_short_rules_do_not_invent_rows() -> None:
    """A decorative stroke covering little of the table width is not a row."""
    fixture = load(184)
    geometry = json.loads(json.dumps(fixture["geometry"]))
    left, _, right, _ = fixture["table_bbox"]
    middle = (left + right) / 2
    geometry["rules"].append([0.33, middle, middle + 0.02])
    result = reconcile_table_grid(
        fixture["raw_grid"], fixture["table_bbox"], geometry, tuple(fixture["page_size"])
    )
    assert len(body(result)) == 4
    assert 0.33 not in result["audit"]["boundaries"]


def test_borderless_table_is_left_alone() -> None:
    """Rows supported by whitespace alone carry no boundary evidence here, so the
    reconciler must not collapse them into one band."""
    cells = []
    rows = [["Name", "Value"], ["alpha", "1"], ["beta", "2"], ["gamma", "3"]]
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            cells.append(
                {
                    "r": r, "c": c, "text": text,
                    "bbox": {"l": 50 + c * 100, "t": 50 + r * 20, "r": 140 + c * 100,
                             "b": 65 + r * 20, "origin": "TOPLEFT"},
                    "column_header": r == 0,
                    "start_row_offset_idx": r, "end_row_offset_idx": r + 1,
                    "start_col_offset_idx": c, "end_col_offset_idx": c + 1,
                    "row_span": 1, "col_span": 1,
                }
            )
    grid = {"rows": rows, "num_cols": 2, "header_rows": 1, "cells": cells}
    result = reconcile_table_grid(
        grid, [0.0, 0.0, 1.0, 1.0], {"rules": [], "lines": []}, (400.0, 400.0)
    )
    assert result["status"] == "unchanged"
    assert result["grid"] == grid


def test_version_is_exported() -> None:
    assert isinstance(RECONSTRUCTION_VERSION, int)


def test_candidate_chain_consumes_the_accepted_grid() -> None:
    """build_table_candidates -> normalize_table_grid -> render: the repaired
    grid flows through, and the preserved raw grid is never what gets rendered.
    """
    fixture = load(183)
    page_size = tuple(fixture["page_size"])
    layout_map = {
        "blocks": [
            {
                "id": "b0002",
                "type": "table",
                "bbox": fixture["table_bbox"],
                "_table_grid": fixture["raw_grid"],
            }
        ]
    }
    candidates = build_table_candidates(
        cells=[],
        page_size=page_size,
        picture_records={},
        layout_map=layout_map,
        page_geometry=fixture["geometry"],
    )
    stats = candidates[0].stats
    assert stats["reconstruction"]["status"] == "repaired"
    assert stats["reconstruction_version"] == RECONSTRUCTION_VERSION

    # Raw stays as evidence, and still normalizes to the bad four records.
    assert len(normalize_table_grid(stats["grid_raw"])["records"]) == 4
    accepted = normalize_table_grid(stats["grid"])
    assert len(accepted["records"]) == 3

    assert render_deterministic_docling_table(candidates[0], {}, page_size) is True
    assert candidates[0].verified
    data_rows = [
        line
        for line in candidates[0].markdown.splitlines()
        if line.startswith("|") and set(line) - set("|-: ")
    ]
    assert len(data_rows) == 4  # header + three records


def test_abstained_grid_is_never_spliced() -> None:
    """An abstained table keeps the raw markdown: no verification, no splice."""
    fixture = load(183)
    candidate = TableCandidate(
        candidate_id="tc001",
        kind="docling_table",
        bbox=fixture["table_bbox"],
        source_block_ids=["b0002"],
        confidence=0.95,
        reason="docling_table_item",
        stats={"grid": fixture["raw_grid"], "reconstruction": {"status": "abstained"}},
    )
    assert (
        render_deterministic_docling_table(candidate, {}, tuple(fixture["page_size"])) is False
    )
    assert not candidate.verified

    markdown = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    spliced, enforced = replace_deterministic_tables(markdown, [candidate])
    assert spliced == markdown and enforced == []


def test_visual_values_associate_against_the_reconstructed_grid() -> None:
    """The tagged coverage markers still land one-per-record once the rows are
    the source's rows -- three records now, not four."""
    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "docling_grid_page183.json").read_text(
            encoding="utf-8"
        )
    )

    def build(cls, rows):
        fields = set(cls.__dataclass_fields__)
        return [cls(**{k: v for k, v in row.items() if k in fields}) for row in rows]

    fixture = load(183)
    page_size = (float(payload["page_size"][0]), float(payload["page_size"][1]))
    tables = build(TableCandidate, payload["table_candidates"])
    result = reconcile(fixture)
    assert result["status"] == "repaired"
    tables[0].stats = {**(tables[0].stats or {}), "grid": result["grid"]}

    visuals = build(VisualCandidate, payload["visual_candidates"])
    associate_table_cells(visuals, tables, page_size)
    assert all(candidate.target.get("column_index") == 4 for candidate in visuals)
    assert sorted(candidate.target.get("record_index") for candidate in visuals) == [0, 1, 2]


def test_reconciler_output_is_byte_stable() -> None:
    """Pin the whole reconciler result -- grid and audit -- for every fixture.

    The named checks above cover the behaviour that matters; this one is the
    refactoring net. reconcile_table_grid is a single 470-line function, so any
    structural change to it must leave every fixture's serialized output
    untouched. A digest mismatch means behaviour moved, not that the digest is
    stale: investigate before updating it.

    RECONSTRUCTION_VERSION is pinned alongside because bumping it makes
    _stale_reconstruction force a table_detect replay of every existing
    checkpoint.
    """
    expected = {
        183: "971c492aa3917291",
        184: "270d8e57f64ea162",
        185: "47616c49665b49b5",
        190: "7bb5db2a71c3fe25",
        199: "48563fa97d0de369",
        200: "5825c65144544006",
    }
    assert RECONSTRUCTION_VERSION == 5, (
        f"RECONSTRUCTION_VERSION moved to {RECONSTRUCTION_VERSION}; that "
        "invalidates every saved checkpoint's reconstruction state"
    )
    for page, digest in expected.items():
        result = reconcile(load(page))
        blob = json.dumps(result, sort_keys=True, ensure_ascii=False, default=str)
        actual = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
        assert actual == digest, (
            f"page {page} reconciler output changed ({actual} != {digest}); "
            f"status={result['status']}"
        )


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as error:
            failures += 1
            print(f"FAIL {test.__name__}: {error}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
