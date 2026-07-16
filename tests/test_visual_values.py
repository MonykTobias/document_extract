"""Focused synthetic checks for generic tagged-PDF visual-value recovery.

Run from the repository root with ``python tests/test_visual_values.py``.
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import asdict
import argparse
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from document_extract.models import TableCandidate, VisualCandidate
from document_extract.pipeline.state import _sync_page_state, _visual_candidates_from_state
from document_extract.runtime import CHECKPOINT_SCHEMA_VERSION, PageState
from document_extract.tables import place_visual_value
from document_extract.visual_values import (
    apply_trusted_visual_values,
    assess_candidate,
    associate_table_cells,
    collect_tagged_candidates,
    merge_picture_evidence,
    normalize_actual_text,
    reconcile_picture_evidence,
    update_visual_completeness,
)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


def _proposal(value: str) -> dict[str, object]:
    normalized = normalize_actual_text(value)
    return {
        "method": "actual_text",
        "raw_value": value,
        "normalized_value": normalized,
        "comparison_key": " ".join((normalized or "").casefold().split()),
        "confidence": 1.0,
    }


def _candidate(
    candidate_id: str,
    value: str,
    *,
    rect: list[float] | None = None,
    target: bool = True,
) -> VisualCandidate:
    return VisualCandidate(
        candidate_id=candidate_id,
        page=1,
        norm_rect=rect or [0.70, 0.30, 0.80, 0.45],
        evidence=[
            {
                "source": "tagged_pdf",
                "source_ref": "owner=4;mcid=9",
                "norm_rect": rect or [0.70, 0.30, 0.80, 0.45],
                "paint_state": "visible",
                "confidence": 1.0,
                "reason_codes": [
                    "owner_mcid_unique",
                    "structure_cell",
                    "placement_unique",
                ],
                # Collector provenance used by the synthetic table-alignment case.
                "source_struct_row": 1,
                "source_struct_col": 1,
            }
        ],
        proposals=[_proposal(value)],
        target=(
            {
                "kind": "table_cell",
                "table_candidate_id": "tc001",
                "record_index": 0,
                "column_index": 1,
                "source_struct_row": 1,
                "source_struct_col": 1,
            }
            if target
            else {}
        ),
        semantic="informational",
    )


def _grid_cell(r: int, c: int, text: str, bbox: list[float] | None) -> dict[str, object]:
    return {
        "r": r,
        "c": c,
        "text": text,
        "bbox": (
            {"l": bbox[0], "t": bbox[1], "r": bbox[2], "b": bbox[3], "origin": "TOPLEFT"}
            if bbox is not None
            else None
        ),
        "column_header": r == 0,
        "start_row_offset_idx": r,
        "end_row_offset_idx": r,
        "start_col_offset_idx": c,
        "end_col_offset_idx": c,
        "row_span": 1,
        "col_span": 1,
    }


def _table(candidate_id: str = "tc001") -> TableCandidate:
    rows = [["Item", "Signal"], ["Alpha", "native"], ["Beta", "native"]]
    cells = [
        _grid_cell(0, 0, "Item", [0, 0, 50, 20]),
        _grid_cell(0, 1, "Signal", [50, 0, 100, 20]),
        _grid_cell(1, 0, "Alpha", [0, 20, 50, 60]),
        # The association code must infer this target rectangle from the table
        # and its neighboring row/column edges.
        _grid_cell(1, 1, "native", None),
        _grid_cell(2, 0, "Beta", [0, 60, 50, 100]),
        _grid_cell(2, 1, "native", [50, 60, 100, 100]),
    ]
    return TableCandidate(
        candidate_id=candidate_id,
        kind="docling_table",
        bbox=[0.0, 0.0, 1.0, 1.0],
        stats={
            "grid": {
                "rows": rows,
                "num_cols": 2,
                "header_rows": 1,
                "cells": cells,
            }
        },
    )


def _page_state(page_dir: Path) -> PageState:
    return PageState(
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        pdf_name="fixture.pdf",
        pdf_size=1,
        page=1,
        page_index=1,
        total_pages=1,
        dpi=200,
        page_dir=str(page_dir),
        page_size=(100.0, 100.0),
    )


def check_normalization() -> None:
    check(normalize_actual_text("Cafe\u0301") == "Caf\u00e9", "Unicode is normalized to NFC")
    check(
        normalize_actual_text("E4, E5\x00\x00") == "E4, E5",
        "terminal NULs are removed without splitting opaque scalars",
    )
    check(
        normalize_actual_text("Pending\x00".encode("utf-16")) == "Pending",
        "BOM-marked UTF-16 strings decode correctly",
    )


def _write_tagged_pdf(
    path: Path, value: str, *, paint: bool, text_mode: int | None = None
) -> None:
    """Create a tiny tagged page without committing a binary fixture."""
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

    writer = PdfWriter()
    page = writer.add_blank_page(100, 100)
    encoded = value.encode("utf-16").hex().upper().encode("ascii")
    paint_ops = b" 50 50 10 10 re f" if paint else b""
    text_ops = (
        b" BT /F1 12 Tf "
        + str(text_mode).encode("ascii")
        + b" Tr 1 0 0 1 50 50 Tm (native) Tj ET"
        if text_mode is not None
        else b""
    )
    content = DecodedStreamObject()
    content.set_data(
        b"/Span << /MCID 0 /ActualText <"
        + encoded
        + b"> >> BDC"
        + paint_ops
        + text_ops
        + b" EMC"
    )
    page[NameObject("/Contents")] = writer._add_object(content)
    page[NameObject("/StructParents")] = NumberObject(0)
    cell = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/StructElem"),
            NameObject("/S"): NameObject("/TD"),
            NameObject("/K"): NumberObject(0),
        }
    )
    cell_ref = writer._add_object(cell)
    row = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/StructElem"),
            NameObject("/S"): NameObject("/TR"),
            NameObject("/K"): ArrayObject([cell_ref]),
        }
    )
    row_ref = writer._add_object(row)
    table = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/StructElem"),
            NameObject("/S"): NameObject("/Table"),
            NameObject("/K"): ArrayObject([row_ref]),
        }
    )
    table_ref = writer._add_object(table)
    cell[NameObject("/P")] = row_ref
    row[NameObject("/P")] = table_ref
    parent_tree = DictionaryObject(
        {NameObject("/Nums"): ArrayObject([NumberObject(0), ArrayObject([cell_ref])])}
    )
    structure = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/StructTreeRoot"),
            NameObject("/K"): ArrayObject([table_ref]),
            NameObject("/ParentTree"): writer._add_object(parent_tree),
        }
    )
    writer._root_object[NameObject("/StructTreeRoot")] = writer._add_object(structure)
    with path.open("wb") as output:
        writer.write(output)


def check_tagged_collection() -> None:
    from pypdf import PdfReader

    with tempfile.TemporaryDirectory() as temp:
        painted = Path(temp) / "painted.pdf"
        unpainted = Path(temp) / "unpainted.pdf"
        visible_text = Path(temp) / "visible_text.pdf"
        invisible_text = Path(temp) / "invisible_text.pdf"
        mixed_scope = Path(temp) / "mixed_scope.pdf"
        _write_tagged_pdf(painted, "Novel Omega", paint=True)
        _write_tagged_pdf(unpainted, "E4, E5", paint=False)
        _write_tagged_pdf(visible_text, "Native text", paint=False, text_mode=0)
        _write_tagged_pdf(invisible_text, "Hidden text", paint=False, text_mode=3)
        _write_tagged_pdf(mixed_scope, "Mixed marker", paint=True, text_mode=0)
        collected = collect_tagged_candidates(PdfReader(painted), 1, (100.0, 100.0))
        invisible = collect_tagged_candidates(PdfReader(unpainted), 1, (100.0, 100.0))
        native = collect_tagged_candidates(PdfReader(visible_text), 1, (100.0, 100.0))
        hidden = collect_tagged_candidates(PdfReader(invisible_text), 1, (100.0, 100.0))
        mixed = collect_tagged_candidates(PdfReader(mixed_scope), 1, (100.0, 100.0))
    check(
        len(collected) == 1
        and collected[0].proposals[0]["normalized_value"] == "Novel Omega"
        and collected[0].evidence[0]["paint_state"] == "visible",
        "a temporary tagged PDF yields an arbitrary visible opaque value",
    )
    check(
        invisible[0].proposals[0]["normalized_value"] == "E4, E5"
        and invisible[0].evidence[0]["paint_state"] == "not_visible",
        "a tagged value without paint remains a diagnostic and stays unsplit",
    )
    check(not native, "visible native text is not treated as a missing visual value")
    assess_candidate(hidden[0], "")
    check(
        len(hidden) == 1
        and hidden[0].evidence[0]["paint_state"] == "not_visible"
        and hidden[0].resolution == "unresolved",
        "invisible text-show content remains a no-paint diagnostic",
    )
    check(
        len(mixed) == 1 and mixed[0].evidence[0]["paint_state"] == "visible",
        "a mixed visible text/vector scope remains an auditable visual candidate",
    )

    table = _table()
    table.stats["grid"]["rows"][1][1] = ""
    associate_table_cells(collected, [table], (100.0, 100.0))
    candidate = collected[0]
    check(
        candidate.target.get("record_index") == 0
        and candidate.target.get("column_index") == 1
        and candidate.resolution == "trusted",
        "collected tagged evidence reaches a unique empty output cell end-to-end",
    )


def check_assessment() -> None:
    trusted = _candidate("vv001", "Pending")
    assess_candidate(trusted, "")
    check(
        trusted.resolution == "trusted" and trusted.resolved_value == "Pending",
        "visible uniquely placed tagged text is trusted",
    )

    duplicate = _candidate("vv002", "Pending")
    assess_candidate(duplicate, "Pending")
    check(
        duplicate.resolution == "already_present",
        "native cell text suppresses an exact duplicate",
    )

    repeated = _candidate("vv003", "High")
    repeated.proposals.append(_proposal("High"))
    assess_candidate(repeated, "")
    check(repeated.resolution == "trusted", "matching proposals do not create a conflict")

    conflict = _candidate("vv004", "High")
    conflict.proposals.append(_proposal("No"))
    assess_candidate(conflict, "")
    check(
        conflict.resolution == "conflict" and len(conflict.proposals) == 2,
        "conflicting proposals remain retained and unresolved",
    )

    picture_conflict = _candidate("vv004b", "Pending")
    picture_conflict.evidence.append(
        {
            "source": "docling_picture",
            "source_ref": "picture:3",
            "norm_rect": picture_conflict.norm_rect,
            "paint_state": "visible",
            "confidence": 1.0,
            "geometry_relation": "coextensive",
            "reason_codes": ["picture_record"],
        }
    )
    picture_conflict.proposals.append(
        {
            "method": "picture_summary",
            "source_ref": "picture:3",
            "raw_value": "High",
            "normalized_value": "High",
            "comparison_key": "high",
            "confidence": 1.0,
        }
    )
    assess_candidate(picture_conflict, "")
    check(
        picture_conflict.resolution == "conflict",
        "a coextensive picture summary that disagrees blocks tagged insertion",
    )


def check_association() -> None:
    candidate = _candidate("vv005", "Pending", target=False)
    table = _table()
    associate_table_cells([candidate], [table], (100.0, 100.0))
    check(
        candidate.target.get("kind") == "table_cell"
        and candidate.target.get("table_candidate_id") == "tc001"
        and candidate.target.get("record_index") == 0
        and candidate.target.get("column_index") == 1,
        "a null Docling cell bbox is inferred and mapped to one output cell",
    )

    ambiguous = _candidate("vv006", "Pending", target=False)
    associate_table_cells([ambiguous], [_table("tc001"), _table("tc002")], (100.0, 100.0))
    check(
        not ambiguous.target and ambiguous.resolution == "unresolved",
        "equally plausible table targets remain unresolved",
    )


def check_completeness_and_authority() -> None:
    missing = _candidate("vv007", "Pending")
    missing.resolution = "trusted"
    table = _table()
    update_visual_completeness([table], [missing], mode="audit")
    check(
        table.stats["visual_values"]["status"] == "incomplete"
        and table.has_complete_symbol_geometry(),
        "audit records an incomplete table without changing authority",
    )

    update_visual_completeness([table], [missing], mode="enforce")
    check(
        table.stats["visual_values"]["status"] == "incomplete"
        and not table.has_complete_symbol_geometry(),
        "enforce withholds an incomplete deterministic table",
    )

    represented = _candidate("vv008", "Pending")
    represented.resolution = "already_present"
    complete_table = _table("tc003")
    represented.target["table_candidate_id"] = "tc003"
    update_visual_completeness([complete_table], [represented], mode="enforce")
    check(
        complete_table.stats["visual_values"]["status"] == "complete"
        and complete_table.has_complete_symbol_geometry(),
        "represented evidence preserves enforce-mode authority",
    )

    empty_table = _table("tc004")
    update_visual_completeness([empty_table], [], mode="enforce")
    check(
        empty_table.stats["visual_values"]["status"] == "not_observed"
        and empty_table.has_complete_symbol_geometry(),
        "not-observed visual evidence retains legacy authority",
    )

    unresolved = _candidate("vv008b", "Novel Omega", target=False)
    unresolved.evidence[0]["affected_table_candidate_ids"] = ["tc005"]
    affected_table = _table("tc005")
    update_visual_completeness([affected_table], [unresolved], mode="enforce")
    check(
        affected_table.stats["visual_values"]["status"] == "uncertain"
        and not affected_table.has_complete_symbol_geometry(),
        "unresolved evidence inside a table withholds enforce-mode authority",
    )


def check_explicit_insertion() -> None:
    normalized = {
        "num_cols": 2,
        "records": [{"cells": ["Alpha", ""]}],
    }
    check(
        place_visual_value(normalized, 0, 1, "E4, E5"),
        "an explicit opaque scalar inserts into an empty target cell",
    )
    check(
        normalized["records"][0]["cells"][1] == "E4, E5",
        "commas remain part of one opaque scalar",
    )
    check(place_visual_value(normalized, 0, 1, "E4, E5"), "exact replay is idempotent")
    check(
        not place_visual_value(normalized, 0, 1, "Other"),
        "a differing native value is neither overwritten nor joined",
    )

    table = TableCandidate(
        candidate_id="tc001",
        kind="docling_table",
        bbox=[0.0, 0.0, 1.0, 1.0],
        stats={
            "grid": {
                "rows": [
                    ["Item", "Detail", "Signal"],
                    ["Alpha", "first", ""],
                    ["Beta", "second", ""],
                ],
                "num_cols": 3,
                "header_rows": 1,
                "cells": [],
            }
        },
    )
    candidate = _candidate("vv010", "Category B")
    candidate.resolution = "trusted"
    candidate.target["column_index"] = 2
    check(
        apply_trusted_visual_values([table], [candidate], mode="enforce") == 1
        and table.stats["grid"]["rows"][1][2] == "Category B"
        and candidate.resolution == "already_present",
        "enforce copies a trusted scalar into the source grid before rendering",
    )

    sectioned = _table("tc-sectioned")
    sectioned.stats["sectioned_split"] = {"sections": ["unsupported"]}
    blocked = _candidate("vv011", "Novel Omega")
    blocked.target.update({"table_candidate_id": "tc-sectioned", "column_index": 1})
    blocked.resolution = "trusted"
    check(
        apply_trusted_visual_values([sectioned], [blocked], mode="enforce") == 0
        and blocked.resolution == "trusted",
        "unsupported sectioned rendering is never mutated by tagged insertion",
    )


def check_picture_reconciliation() -> None:
    candidate = _candidate("vv012", "Pending")
    candidate.resolution = "trusted"
    candidate.evidence.append(
        {
            "source": "docling_picture",
            "source_ref": "picture:7",
            "norm_rect": candidate.norm_rect,
            "paint_state": "visible",
            "confidence": 1.0,
            "geometry_relation": "coextensive",
            "reason_codes": ["picture_record"],
        }
    )
    candidate.proposals.append(
        {
            "method": "picture_summary",
            "source_ref": "picture:7",
            "raw_value": "Pending",
            "normalized_value": "Pending",
            "comparison_key": "pending",
            "confidence": 1.0,
        }
    )
    table = _table()
    table.stats["symbol_cell_assignments"] = {
        7: {"record_index": 0, "column_index": 1}
    }
    reconcile_picture_evidence([table], [candidate])
    check(
        candidate.resolution == "already_present",
        "legacy picture reconciliation requires and recognizes an exact cell/value match",
    )

    mismatch = _candidate("vv013", "Pending")
    mismatch.resolution = "trusted"
    mismatch.evidence = list(candidate.evidence)
    mismatch.proposals = [
        _proposal("Pending"),
        {
            "method": "picture_summary",
            "source_ref": "picture:7",
            "raw_value": "High",
            "normalized_value": "High",
            "comparison_key": "high",
            "confidence": 1.0,
        },
    ]
    reconcile_picture_evidence([table], [mismatch])
    check(
        mismatch.resolution == "trusted",
        "legacy picture geometry alone never promotes a disagreeing tagged value",
    )


def check_checkpoint_round_trip() -> None:
    source = _candidate("vv009", "Category B")
    with tempfile.TemporaryDirectory() as temp:
        state = _page_state(Path(temp))
        _sync_page_state(state, visual_candidates=[source])
        state.save()
        restored = _visual_candidates_from_state(PageState.load(state.checkpoint_path))
    check(
        len(restored) == 1 and asdict(restored[0]) == asdict(source),
        "VisualCandidate survives a PageState checkpoint round-trip",
    )

    with tempfile.TemporaryDirectory() as temp:
        checkpoint = Path(temp) / "old_state.json"
        checkpoint.write_text(
            '{"schema_version": 1, "pdf_name": "fixture.pdf", "pdf_size": 1, "page": 1, '
            '"page_index": 1, "total_pages": 1, "dpi": 200, "page_dir": ".", '
            '"page_size": [100.0, 100.0]}',
            encoding="utf-8",
        )
        old_state = PageState.load(checkpoint)
    check(
        old_state.visual_values_mode == "off"
        and old_state.visual_candidates == []
        and old_state.visual_audit == {},
        "older checkpoints load safe visual-value defaults",
    )


def check_external_fixture(pdf_path: Path, artifacts: Path | None) -> None:
    from pypdf import PdfReader

    expected_hash = "F8607C4E1C499BE832F952A4B10B01F9F358A5A3DBF2D90AA416124F024A06E0"
    actual_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest().upper()
    check(actual_hash == expected_hash, "external fixture SHA-256 matches its pinned ground truth")
    if artifacts is not None:
        check(artifacts.is_dir(), "external fixture artifact directory is available")
    reader = PdfReader(str(pdf_path))

    def values(page_number: int) -> list[VisualCandidate]:
        page = reader.pages[page_number - 1]
        return collect_tagged_candidates(
            reader, page_number, (float(page.mediabox.width), float(page.mediabox.height))
        )

    counts = {page: len(values(page)) for page in range(183, 201)}
    check(
        sum(counts.values()) == 111 and counts[186] == counts[192] == 0,
        "external tagged visual groups match the expected page range total",
    )
    page_183 = values(183)
    check(
        len(page_183) == 3
        and [item.proposals[0]["normalized_value"] for item in page_183] == ["ALL"] * 3,
        "external page 183 retains its three opaque tagged values",
    )
    page_188 = values(188)
    page_188_values = [item.proposals[0]["normalized_value"] for item in page_188]
    check(
        len(page_188) == 11 and "E5" in page_188_values and "S2" in page_188_values,
        "external page 188 recovers its tagged values without a header vocabulary",
    )
    check(len(values(194)) == 8, "external page 194 collection remains stable")
    page_384_values = [item.proposals[0]["normalized_value"] for item in values(384)]
    check(
        len(page_384_values) == 50 and set(page_384_values) == {"Yes"},
        "external page 384 retains its visible tagged table values",
    )
    if artifacts is not None:
        _check_external_association_and_insertion(reader, artifacts, 183, ["ALL", "ALL", "ALL"])
        _check_external_association_and_insertion(
            reader,
            artifacts,
            188,
            [
                "E2",
                "E2, E5, S3",
                "E3, E4, S3",
                "E4",
                "E4",
                "E4, E5",
                "E5",
                "E5",
                "S1, S2",
                "S2",
                "S1, S2",
            ],
        )


def _check_external_association_and_insertion(
    reader: object, artifacts: Path, page_number: int, expected_values: list[str]
) -> None:
    state = PageState.load(artifacts / f"page_{page_number:04d}" / "page_state.json")
    from document_extract.pipeline.state import _candidates_from_state, _records_from_state

    tables = _candidates_from_state(state)
    records = _records_from_state(state)
    page = reader.pages[page_number - 1]  # type: ignore[attr-defined]
    candidates = collect_tagged_candidates(
        reader, page_number, (float(page.mediabox.width), float(page.mediabox.height))  # type: ignore[attr-defined]
    )
    candidates = merge_picture_evidence(candidates, records)
    associate_table_cells(candidates, tables, (float(page.mediabox.width), float(page.mediabox.height)))
    check(
        [candidate.resolved_value for candidate in candidates] == expected_values
        and all(candidate.resolution == "trusted" for candidate in candidates),
        f"external page {page_number} uniquely associates every expected tagged value",
    )
    inserted = apply_trusted_visual_values(tables, candidates, mode="enforce")
    update_visual_completeness(tables, candidates, mode="enforce")
    table = next(table for table in tables if table.candidate_id == "tc001")
    check(
        inserted == len(expected_values)
        and table.stats["visual_values"]["status"] == "complete"
        and all(candidate.resolution == "already_present" for candidate in candidates),
        f"external page {page_number} enforce insertion is complete and replay-safe",
    )


def check_source_guard() -> None:
    source = (Path(__file__).resolve().parents[1] / "src" / "document_extract" / "visual_values.py").read_text(
        encoding="utf-8"
    ).casefold()
    forbidden = (
        "danone",
        "esrs coverage",
        "workiva",
        "wdesk",
        "f8607c4e1c499be832f952a4b10b01f9f358a5a3dbf2d90aa416124f024a06e0",
    )
    check(
        not any(value in source for value in forbidden),
        "the generic visual-value module contains no fixture-specific literals",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--danone-pdf", type=Path)
    parser.add_argument("--danone-artifacts", type=Path)
    args = parser.parse_args(argv)
    check_normalization()
    check_tagged_collection()
    check_assessment()
    check_association()
    check_completeness_and_authority()
    check_explicit_insertion()
    check_picture_reconciliation()
    check_checkpoint_round_trip()
    check_source_guard()
    if args.danone_pdf is not None:
        check_external_fixture(args.danone_pdf, args.danone_artifacts)
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
