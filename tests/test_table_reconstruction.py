"""Table-reconstruction checks: row ownership, invariants, and abstention.

The three Danone fixtures are frozen serializations of a real Docling run plus
the source page's own rule/line geometry; the PDF itself stays external. Every
other case is synthetic so the suite needs no Docling, GPU, PDF, or Ollama.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from document_extract.markdown.formatting import replace_deterministic_tables  # noqa: E402
from document_extract.models import TableCandidate, VisualCandidate  # noqa: E402
from document_extract.table_reconstruction import (  # noqa: E402
    RECONSTRUCTION_VERSION,
    _tokens,
    reconcile_table_grid,
)
from document_extract.tables import (  # noqa: E402
    build_table_candidates,
    normalize_table_grid,
    render_deterministic_docling_table,
)
from document_extract.visual_values import associate_table_cells  # noqa: E402

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
    for page in (183, 184, 185):
        fixture = load(page)
        baseline = reconcile(fixture)
        for scale in (0.5, 2.0):
            scaled = reconcile(fixture, scale=scale)
            assert scaled["status"] == baseline["status"], (page, scale)
            assert scaled["grid"]["rows"] == baseline["grid"]["rows"], (page, scale)


def test_invariants_hold_on_every_fixture() -> None:
    for page in (183, 184, 185):
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
    for page in (183, 184, 185):
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
            index = 0
            for token in rebuilt[column]:
                if index < len(tokens) and token == tokens[index]:
                    index += 1
            assert index == len(tokens), (page, column)
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
