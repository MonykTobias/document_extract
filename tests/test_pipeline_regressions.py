"""Focused checks for refactor regressions: config propagation into tables,
page_repair replay state, layout-map picture keying, and retry accounting.

Run from the repository root with ``python tests/test_pipeline_regressions.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from document_extract import pictures, tables
from document_extract.config import apply_detection_config, config_from_mapping
from document_extract.layout.prompt_map import build_layout_prompt_map
from document_extract.models import PictureRecord, TableCandidate, VisualCandidate
from document_extract.pipeline import stages
from document_extract.pipeline.runner import _has_current_visual_audit, selected_page_numbers
from document_extract.pipeline.state import _records_from_state
from document_extract.refinement import page_is_plain_prose
from document_extract.visual_values import COLLECTOR_VERSION
from document_extract.runtime import (
    CHECKPOINT_SCHEMA_VERSION,
    PageState,
    StatusReporter,
    clear_downstream_artifacts,
    invalidate_from,
)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


def make_picture_record(**overrides) -> PictureRecord:
    payload = {
        "page": 1,
        "index": 1,
        "placeholder": "{{DOC_IMAGE_p0001_i001}}",
        "rel_path": "images/picture_p0001_i001.png",
        "abs_path": "images/picture_p0001_i001.png",
        "bbox": {"l": 10.0, "t": 10.0, "r": 60.0, "b": 60.0},
        "area_ratio": 0.3,
        "classification": "chart",
        "caption": "",
    }
    payload.update(overrides)
    return PictureRecord(**payload)


def make_page_state() -> PageState:
    return PageState(
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        pdf_name="doc.pdf",
        pdf_size=1,
        page=1,
        page_index=1,
        total_pages=1,
        dpi=200,
        page_dir="page_0001",
        page_size=(100.0, 100.0),
    )


def check_detection_config_reaches_tables() -> None:
    defaults = (
        pictures.PICTURE_MIN_AREA_RATIO,
        pictures.PICTURE_DECORATIVE_MAX_AREA_RATIO,
        pictures.PICTURE_TABLE_MIN_AREA_RATIO,
    )
    record = make_picture_record()
    rect = [0.1, 0.4, 0.9, 0.6]
    try:
        apply_detection_config(
            config_from_mapping({"pictures": {"table_min_area_ratio": 0.5}})
        )
        is_table_like, reason, _ = tables.picture_record_is_table_like(record, rect, [])
        check(
            not is_table_like and reason == "picture_too_small",
            "pictures.table_min_area_ratio override reaches table detection",
        )
        apply_detection_config(
            config_from_mapping({"pictures": {"table_min_area_ratio": 0.08}})
        )
        is_table_like, _, _ = tables.picture_record_is_table_like(record, rect, [])
        check(is_table_like, "default threshold still accepts a table-like picture")
    finally:
        (
            pictures.PICTURE_MIN_AREA_RATIO,
            pictures.PICTURE_DECORATIVE_MAX_AREA_RATIO,
            pictures.PICTURE_TABLE_MIN_AREA_RATIO,
        ) = defaults


def check_page_repair_replay_state() -> None:
    state = make_page_state()
    state.completed_stage = "finalize"
    state.refined_markdown = "# refined"
    state.pre_repair_markdown = "# refined page"
    state.pre_repair_warnings = {"content_loss_guard_triggered": False}
    state.final_markdown = "# repaired page"
    state.warnings = {"repair_rejected": ["shrunk_output"]}
    state.repair_layout_map = {"blocks": ["stale"]}
    state.page_repair_usage = {"total": 1}
    invalidate_from(state, "page_repair")
    check(
        state.final_markdown == "# refined page",
        "resume from page_repair restores the post-refine markdown",
    )
    check(
        state.warnings == {"content_loss_guard_triggered": False},
        "resume from page_repair restores the post-refine warnings",
    )
    check(
        state.repair_layout_map == {} and state.page_repair_usage is None,
        "resume from page_repair clears stale repair state",
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        repair_map = Path(temp_dir) / "layout_prompt_map_repair.json"
        repair_map.write_text("{}", encoding="utf-8")
        clear_downstream_artifacts(Path(temp_dir), "page_repair")
        check(not repair_map.exists(), "page_repair replay removes its repair layout map")

    state = make_page_state()
    state.completed_stage = "finalize"
    state.pre_repair_markdown = "# refined page"
    state.pre_repair_warnings = {"content_loss_guard_triggered": True}
    state.final_markdown = "# repaired page"
    invalidate_from(state, "page_refine")
    check(
        state.final_markdown == "" and state.pre_repair_markdown == "",
        "resume from page_refine clears markdown and the repair snapshot",
    )

    # Old checkpoint without a snapshot: keep final_markdown instead of wiping it.
    state = make_page_state()
    state.completed_stage = "finalize"
    state.final_markdown = "# repaired page"
    invalidate_from(state, "page_repair")
    check(
        state.final_markdown == "# repaired page",
        "snapshot-less checkpoint keeps final_markdown on page_repair resume",
    )


def check_sectioned_table_survives_table_extract_replay() -> None:
    sectioned = {
        "candidate_id": "tc001",
        "stats": {"format": "sectioned_table"},
        "markdown": "### Section\n\n| A | B |\n|---|---|\n| 1 | 2 |",
        "verified": True,
    }
    plain = {
        "candidate_id": "tc002",
        "stats": {"format": "region"},
        "markdown": "| A | B |\n|---|---|\n| 1 | 2 |",
        "verified": True,
    }
    state = make_page_state()
    state.completed_stage = "finalize"
    state.table_candidates = [sectioned, plain]
    invalidate_from(state, "table_extract")
    check(
        sectioned["verified"] and sectioned["markdown"].startswith("### Section"),
        "replaying table_extract preserves verified sectioned tables",
    )
    check(
        not plain["verified"] and plain["markdown"] == "",
        "replaying table_extract still clears ordinary table candidates",
    )


def check_visual_value_invalidation() -> None:
    state = make_page_state()
    state.completed_stage = "finalize"
    state.visual_values_mode = "enforce"
    state.visual_candidates = [{"candidate_id": "vv001"}]
    state.visual_audit = {"completed": True}
    state.table_candidates = [{"candidate_id": "tc001"}]
    invalidate_from(state, "table_detect")
    check(
        state.visual_values_mode == "off"
        and state.visual_candidates == []
        and state.visual_audit == {},
        "replaying table detection clears visual-value checkpoint state",
    )


def check_visual_resume_audit_freshness() -> None:
    state = make_page_state()
    state.visual_values_mode = "audit"
    state.visual_candidates = []
    state.visual_audit = {
        "completed": True,
        "collector_version": COLLECTOR_VERSION,
        "mode": "audit",
        "candidate_count": 0,
    }
    state.table_candidates = [
        {
            "candidate_id": "tc001",
            "stats": {
                "visual_values": {
                    "mode": "audit",
                    "collector_version": COLLECTOR_VERSION,
                }
            },
        }
    ]
    with tempfile.TemporaryDirectory() as temp:
        page_dir = Path(temp)
        (page_dir / "visual_candidates.json").write_text("[]", encoding="utf-8")
        check(
            _has_current_visual_audit(state, page_dir, "audit"),
            "a matching visual audit can resume after table detection",
        )
        (page_dir / "visual_candidates.json").unlink()
        check(
            not _has_current_visual_audit(state, page_dir, "audit"),
            "a missing visual diagnostic artifact forces table-detect replay",
        )
        (page_dir / "visual_candidates.json").write_text("[]", encoding="utf-8")
        state.table_candidates[0]["stats"]["visual_values"]["collector_version"] = 0
        check(
            not _has_current_visual_audit(state, page_dir, "audit"),
            "a stale per-table visual audit forces table-detect replay",
        )


def check_layout_map_picture_keying() -> None:
    class FakeBBox:
        l, t, r, b = 10.0, 10.0, 60.0, 60.0

    class FakeProv:
        page_no = 1
        bbox = FakeBBox()

    class FakeItem:
        label = "picture"
        prov = [FakeProv()]
        text = ""

    item = FakeItem()
    record = make_picture_record(caption="Sales by region chart")
    layout_map = build_layout_prompt_map(
        items=[item],
        page_size=(100.0, 100.0),
        picture_records={id(item): record},
    )
    check(
        layout_map["blocks"][0].get("caption") == "Sales by region chart",
        "id(item)-keyed picture map enriches layout-map captions",
    )


def check_summary_retry_usage_accounting() -> None:
    calls: list[str] = []

    def fake_call_ollama_vlm(**kwargs):
        calls.append(kwargs["prompt"])
        if len(calls) == 1:
            # Well-typed but shapeless answer: forces the one focused retry.
            return "TYPE: chart", {"prompt_tokens": 100, "output_tokens": 10, "total_tokens": 110}
        return "- A: 1\n- B: 2", {"prompt_tokens": 50, "output_tokens": 20, "total_tokens": 70}

    record = make_picture_record()
    record.summarize = True
    args = argparse.Namespace(
        skip_vlm=False,
        ollama_base_url="http://unused.test",
        ollama_model="test-model",
        temperature=0.0,
        num_ctx=0,
        num_predict=0,
        auto_num_ctx=False,
    )
    original = pictures.ollama_client.call_ollama_vlm
    pictures.ollama_client.call_ollama_vlm = fake_call_ollama_vlm
    try:
        pictures.summarize_pictures(records=[record], prompt_template="{context}", args=args)
    finally:
        pictures.ollama_client.call_ollama_vlm = original
    check(len(calls) == 2, "shapeless summary triggers exactly one retry")
    check(record.summary == "- A: 1\n- B: 2", "retry body accepted as the summary")
    check(
        record.usage["total_tokens"] == 180 and record.usage["prompt_tokens"] == 150,
        "retry usage sums both calls' tokens",
    )
    check(bool(record.usage["retried"]), "retry is visible in usage accounting")


def _enforce_table() -> TableCandidate:
    """A deterministic symbol table one trusted insertion away from complete."""
    return TableCandidate(
        candidate_id="tc001",
        kind="docling_table",
        bbox=[0.0, 0.0, 1.0, 1.0],
        stats={
            "grid": {
                "rows": [
                    ["Policy", "Key contents", "ESRS coverage"],
                    ["Alpha", "version 1.0", ""],
                    ["Beta", "10 Principles", "S1"],
                ],
                "num_cols": 3,
                "header_rows": 1,
                "cells": [],
            },
            "visual_values": {
                "status": "incomplete",
                "mode": "enforce",
                "missing_ids": ["vv001"],
                "reason_codes": ["trusted_value_missing_from_grid"],
            },
        },
    )


def _trusted_visual(table_id: str = "tc001") -> VisualCandidate:
    return VisualCandidate(
        candidate_id="vv001",
        page=1,
        norm_rect=[0.7, 0.3, 0.8, 0.4],
        proposals=[
            {
                "method": "actual_text",
                "raw_value": "E4, E5",
                "normalized_value": "E4, E5",
                "comparison_key": "e4, e5",
                "confidence": 1.0,
            }
        ],
        evidence=[
            {
                "source": "tagged_pdf",
                "norm_rect": [0.7, 0.3, 0.8, 0.4],
                "paint_state": "visible",
                "reason_codes": ["parent_tree_unique", "structure_cell"],
            }
        ],
        target={
            "kind": "table_cell",
            "table_candidate_id": table_id,
            "record_index": 0,
            "column_index": 2,
        },
        resolution="trusted",
        resolved_value="E4, E5",
    )


def check_visual_completeness_precedes_transcription() -> None:
    """Insertion must be reflected in authority before the transcription gate.

    Reading detect-time completeness there re-transcribes a table the insertion
    already completed, and lets a rejected VLM answer overwrite it.
    """
    seen: dict[str, object] = {}

    def fake_transcribe(*, candidates, **kwargs) -> None:
        stats = candidates[0].stats or {}
        seen["status"] = (stats.get("visual_values") or {}).get("status")
        seen["authoritative"] = candidates[0].has_complete_symbol_geometry()

    table = _enforce_table()
    state = make_page_state()
    with tempfile.TemporaryDirectory() as temp:
        state.page_dir = temp
        state.visual_values_mode = "enforce"
        original = stages.transcribe_table_candidates
        stages.transcribe_table_candidates = fake_transcribe
        try:
            stages._run_table_extract(
                state=state,
                reporter=StatusReporter(page_index=1, total_pages=1, page=1),
                args=argparse.Namespace(skip_vlm=True),
                runtime={
                    "candidates": [table],
                    "records": [],
                    "visual_candidates": [_trusted_visual()],
                },
            )
        finally:
            stages.transcribe_table_candidates = original
    check(
        table.stats["grid"]["rows"][1][2] == "E4, E5",
        "the trusted value is inserted during table_extract",
    )
    check(
        seen["status"] == "complete" and seen["authoritative"] is True,
        "transcription sees post-insertion completeness, not the detect-time status",
    )


def check_failed_verify_keeps_deterministic_markdown() -> None:
    """A rejected VLM answer must never survive behind ``verified=True``."""
    deterministic = (
        "| Policy | Key contents | ESRS coverage |\n|---|---|---|\n"
        "| Alpha | version 1.0 | E4, E5 |\n| Beta | 10 Principles | S1 |\n"
    )
    table = _enforce_table()
    table.markdown = deterministic
    table.verified = True
    table.stats["grid"]["rows"][1][2] = "E4, E5"
    table.stats["format"] = "regular_table"
    table.stats["deterministic"] = True
    table.stats["symbol_picture_indices"] = [1]
    # Withheld authority (uncertain) is what routes an already-verified
    # deterministic table into the VLM path at all.
    table.stats["visual_values"]["status"] = "uncertain"
    calls: list[str] = []

    def fake_call_ollama_vlm(**kwargs):
        calls.append(kwargs["prompt"])
        return (
            "| Policy | Key contents | ESRS coverage |\n|---|---|---|\n"
            "| Alpha | version 4321 | E4, E5 |\n| Beta | 10 Principles | S1 |\n"
        ), {"total_tokens": 1}

    def fake_save_region_crop(*, page_image_path, bbox, crop_path) -> bool:
        crop_path.parent.mkdir(parents=True, exist_ok=True)
        crop_path.write_bytes(b"")
        return True

    original_vlm = tables.ollama_client.call_ollama_vlm
    original_crop = tables.save_region_crop
    tables.ollama_client.call_ollama_vlm = fake_call_ollama_vlm
    tables.save_region_crop = fake_save_region_crop
    try:
        with tempfile.TemporaryDirectory() as temp:
            tables.transcribe_table_candidates(
                candidates=[table],
                cells=[],
                page_image_path=Path(temp) / "page.png",
                page_dir=Path(temp),
                args=argparse.Namespace(
                    skip_vlm=False,
                    ollama_base_url="http://unused.test",
                    ollama_model="test-model",
                    temperature=0.0,
                    num_ctx=0,
                    num_predict=0,
                    auto_num_ctx=False,
                ),
                picture_records=[make_picture_record(index=1, summary_type="symbol", summary="E4")],
                page_size=(1.0, 1.0),
            )
            rejected = (Path(temp) / "table_candidates" / "tc001_rejected.md").exists()
    finally:
        tables.ollama_client.call_ollama_vlm = original_vlm
        tables.save_region_crop = original_crop
    check(len(calls) == 2, "a withheld deterministic table is retried by the VLM twice")
    check(
        table.markdown == deterministic and table.verified,
        "a twice-rejected VLM answer leaves the verified deterministic markdown in place",
    )
    check(rejected, "the rejected answer is still written for debugging")


def check_collector_unavailable_does_not_fail_detect() -> None:
    """``audit``/``enforce`` must never fail a page that ``off`` completes."""
    from PIL import Image

    state = make_page_state()
    with tempfile.TemporaryDirectory() as temp:
        state.page_dir = temp
        Image.new("RGB", (10, 10)).save(Path(temp) / "page.png")
        candidates = stages._run_table_detect(
            state=state,
            reporter=StatusReporter(page_index=1, total_pages=1, page=1),
            runtime={},
            reader=None,
            mode="audit",
        )
        rows = json.loads((Path(temp) / "visual_candidates.json").read_text(encoding="utf-8"))
    check(state.status != "failed", "an unusable reader does not fail the detect stage")
    check(
        len(rows) == 1
        and rows[0]["reasons"][:2] == ["collector_error", "collector_unavailable"]
        and not rows[0]["proposals"],
        "the audit records the collector failure as a distinct diagnostic",
    )
    check(
        all(
            (candidate.stats or {}).get("visual_values", {}).get("status") != "complete"
            for candidate in candidates
        ),
        "a failed collector is never reported as complete",
    )


def check_table_detect_checkpoints_record_mutations() -> None:
    from PIL import Image

    cells: list[dict[str, object]] = []
    index = 0
    for column, x in enumerate((0.05, 0.30, 0.55)):
        for row in range(4):
            index += 1
            y = 0.15 + row * 0.16
            cells.append(
                {
                    "id": f"b{index:04d}",
                    "rect": [x, y, x + 0.16, y + 0.03],
                    "text": f"Value {column}-{row}",
                    "is_heading": False,
                }
            )
    record = make_picture_record(
        bbox={"l": 20.0, "t": 30.0, "r": 25.0, "b": 35.0},
        area_ratio=0.0025,
    )
    state = make_page_state()
    state.detection_cells = cells
    state.layout_map = {"blocks": []}
    with tempfile.TemporaryDirectory() as temp:
        state.page_dir = temp
        Image.new("RGB", (10, 10)).save(Path(temp) / "page.png")
        candidates = stages._run_table_detect(
            state=state,
            reporter=StatusReporter(page_index=1, total_pages=1, page=1),
            runtime={"records": [record]},
            reader=None,
            mode="off",
        )
        restored = _records_from_state(state)
    check(
        any(candidate.kind == "layout_region" for candidate in candidates),
        "table detection builds the synthetic layout-region candidate",
    )
    check(
        record.triage_type == "symbol" and record.summarize,
        "table detection upgrades an embedded tiny picture to a symbol",
    )
    check(
        len(restored) == 1
        and restored[0].triage_type == "symbol"
        and restored[0].summarize,
        "table-detect record mutations survive checkpoint reload",
    )


def check_page_selection_bounds() -> None:
    check(selected_page_numbers(10, 2, 4) == [2, 3, 4], "page range selection works")
    try:
        selected_page_numbers(10, 11, 12)
    except ValueError:
        check(True, "start page beyond document length is rejected")
    else:
        raise AssertionError("start page beyond document length was not rejected")


def check_page_vlm_input_image_downscaling() -> None:
    from PIL import Image

    with tempfile.TemporaryDirectory() as temp:
        source = Path(temp) / "page.png"
        Image.new("RGB", (3000, 1500)).save(source)
        downscaled = stages._page_vlm_input_path(source, 1536)
        with Image.open(downscaled) as image:
            check(max(image.size) == 1536, "VLM page image is capped at its configured long side")
        check(
            stages._page_vlm_input_path(source, 0) == source,
            "zero VLM page image cap keeps the original image",
        )


def check_plain_prose_refine_predicate() -> None:
    check(
        page_is_plain_prose([], [], False, {"applied": False}),
        "an empty page is plain prose",
    )
    check(
        not page_is_plain_prose(
            [make_picture_record(summary="Sales chart")], [], False, {"applied": False}
        ),
        "a picture with a summary is not plain prose",
    )
    check(
        not page_is_plain_prose([], [], False, {"applied": True}),
        "a reordered page is not plain prose",
    )


def main() -> int:
    check_detection_config_reaches_tables()
    check_page_repair_replay_state()
    check_sectioned_table_survives_table_extract_replay()
    check_visual_value_invalidation()
    check_visual_resume_audit_freshness()
    check_layout_map_picture_keying()
    check_summary_retry_usage_accounting()
    check_visual_completeness_precedes_transcription()
    check_failed_verify_keeps_deterministic_markdown()
    check_collector_unavailable_does_not_fail_detect()
    check_table_detect_checkpoints_record_mutations()
    check_page_selection_bounds()
    check_page_vlm_input_image_downscaling()
    check_plain_prose_refine_predicate()
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
