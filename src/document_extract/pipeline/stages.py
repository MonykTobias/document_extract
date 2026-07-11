"""Page-stage coordination for the document extraction pipeline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..artifacts import block_rows_for_page, write_image_summaries
from ..docling_adapter import (
    assert_docling_export_surface,
    is_table_item,
    iter_doc_items,
    rasterize_page,
)
from ..layout.prompt_map import (
    build_layout_prompt_map,
    detection_cells_from_items,
    detection_cells_from_layout_map,
)
from ..layout.reading_order import extract_divider_segments, reorder_items_for_reading_order
from ..markdown.formatting import export_page_markdown
from ..models import PictureRecord, TableCandidate
from ..pictures import save_picture_records, summarize_pictures, triage_pictures
from ..refinement import (
    missing_verified_table_ids,
    postprocess_markdown,
    refine_page_markdown,
    repair_page_markdown,
    repair_regression_reasons,
    should_run_repair_pass,
)
from ..runtime import PageState, StageRecord, StatusReporter
from ..tables import (
    build_table_candidates,
    render_layout_overlay,
    render_table_candidates_overlay,
    table_candidate_rows,
    transcribe_table_candidates,
)
from .state import (
    _candidates_from_state,
    _records_from_state,
    _sync_page_state,
)

def _execute_stage(
    state: PageState,
    reporter: StatusReporter,
    stage: str,
    action: Any,
) -> dict[str, Any]:
    started = reporter.start(stage)
    state.status = "running"
    try:
        details = action() or {}
    except Exception as error:  # noqa: BLE001
        elapsed = reporter.fail(stage, started, error)
        state.status = "failed"
        state.failure = {
            "stage": stage,
            "error_type": type(error).__name__,
            "error_message": str(error),
        }
        state.stage_history.append(
            StageRecord(
                stage=stage,
                status="failed",
                elapsed_seconds=elapsed,
                error_type=type(error).__name__,
                error_message=str(error),
            )
        )
        state.save()
        raise
    elapsed = reporter.ok(stage, started, **details)
    state.stage_history.append(
        StageRecord(stage=stage, status="ok", elapsed_seconds=elapsed, details=details)
    )
    state.completed_stage = stage
    state.save()
    return details


def _skip_stage(state: PageState, reporter: StatusReporter, stage: str, reason: str) -> None:
    reporter.skip(stage, reason)
    state.stage_history.append(StageRecord(stage=stage, status="skipped", details={"reason": reason}))
    state.completed_stage = stage
    state.save()


def _prepare_page(
    *,
    state: PageState,
    reporter: StatusReporter,
    args: argparse.Namespace,
    pdf_doc: Any,
    document: Any,
    page_size: tuple[float, float],
) -> dict[str, Any]:
    """Prepare one page from a document that was already converted by Docling.

    The runner converts the whole selected page range in a single Docling pass;
    this stage only reads the items for ``state.page`` out of that document.
    """
    runtime: dict[str, Any] = {}
    page_dir = Path(state.page_dir)

    def action() -> dict[str, Any]:
        assert_docling_export_surface(document)
        rasterize_page(pdf_doc, state.page, args.dpi, page_dir / "page.png")
        items = list(iter_doc_items(document, state.page))
        if args.no_divider_reorder:
            reading_order_info: dict[str, Any] = {"applied": False, "disabled": True}
        else:
            dividers = extract_divider_segments(
                pdf_doc.load_page(state.page - 1), page_size
            )
            items, reading_order_info = reorder_items_for_reading_order(
                items, page_size, dividers
            )
        render_layout_overlay(
            page_image_path=page_dir / "page.png",
            items=items,
            page_size=page_size,
            output_path=page_dir / "page_layout_overlay.png",
        )
        picture_map = save_picture_records(
            document=document,
            items=items,
            page_number=state.page,
            page_dir=page_dir,
            page_size=page_size,
        )
        records = list(picture_map.values())
        detection_cells = detection_cells_from_items(items, page_size)
        raw_markdown = export_page_markdown(
            document,
            state.page,
            items,
            picture_map,
            use_docling_order=not reading_order_info["applied"],
        )
        (page_dir / "docling_raw.md").write_text(raw_markdown, encoding="utf-8")
        layout_prompt_map = build_layout_prompt_map(
            items=items,
            page_size=page_size,
            picture_records=picture_map,
        )
        (page_dir / "layout_prompt_map.json").write_text(
            json.dumps(layout_prompt_map, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        if not detection_cells:
            detection_cells = detection_cells_from_layout_map(layout_prompt_map)

        state.reading_order = reading_order_info
        state.has_docling_table = any(is_table_item(item) for item in items)
        state.raw_markdown = raw_markdown
        state.block_rows = block_rows_for_page(items, state.page, picture_map)
        _sync_page_state(
            state,
            records=records,
            detection_cells=detection_cells,
            layout_map=layout_prompt_map,
        )
        runtime.update(
            {
                "document": document,
                "items": items,
                "records": records,
                # Keyed by id(item), the lookup key build_layout_prompt_map uses.
                "picture_map": picture_map,
                "detection_cells": detection_cells,
                "layout_map": layout_prompt_map,
            }
        )
        return {
            "items": len(items),
            "pictures": len(records),
            "text_blocks": len(detection_cells),
            "reordered": reading_order_info.get("moved_items", 0),
        }

    _execute_stage(state, reporter, "prepare", action)
    return runtime


def _run_picture_triage(
    *, state: PageState, reporter: StatusReporter, args: argparse.Namespace, runtime: dict[str, Any]
) -> list[PictureRecord]:
    records = runtime.get("records") or _records_from_state(state)

    def action() -> dict[str, Any]:
        stats = triage_pictures(
            records=records,
            args=args,
            page_image_path=Path(state.page_dir) / "page.png",
            page_size=tuple(state.page_size),
            cells=state.detection_cells,
        )
        runtime["records"] = records
        _sync_page_state(state, records=records)
        return stats

    _execute_stage(state, reporter, "picture_triage", action)
    return records


def _run_picture_extract(
    *,
    state: PageState,
    reporter: StatusReporter,
    args: argparse.Namespace,
    runtime: dict[str, Any],
    prompt: str,
) -> list[PictureRecord]:
    records = runtime.get("records") or _records_from_state(state)

    def action() -> dict[str, Any]:
        summarize_pictures(
            records=records,
            prompt_template=prompt,
            args=args,
            page_image_path=Path(state.page_dir) / "page.png",
            page_size=tuple(state.page_size),
            cells=state.detection_cells,
        )
        runtime["records"] = records
        _sync_page_state(state, records=records)
        calls = sum(record.usage is not None for record in records)
        retries = sum(bool((record.usage or {}).get("retried")) for record in records)
        return {"calls": calls, "retries": retries, "summaries": sum(bool(r.summary) for r in records)}

    _execute_stage(state, reporter, "picture_extract", action)
    return records


def _run_table_detect(
    *, state: PageState, reporter: StatusReporter, runtime: dict[str, Any]
) -> list[TableCandidate]:
    records = runtime.get("records") or _records_from_state(state)
    page_dir = Path(state.page_dir)

    def action() -> dict[str, Any]:
        picture_map = {record.index: record for record in records}
        candidates = build_table_candidates(
            cells=state.detection_cells,
            page_size=tuple(state.page_size),
            picture_records=picture_map,
            layout_map=state.layout_map,
        )
        runtime["candidates"] = candidates
        _sync_page_state(state, candidates=candidates)
        (page_dir / "table_candidates.json").write_text(
            json.dumps(table_candidate_rows(candidates), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        render_table_candidates_overlay(
            page_image_path=page_dir / "page.png",
            candidates=candidates,
            output_path=page_dir / "table_candidates_overlay.png",
        )
        return {
            "candidates": len(candidates),
            "kinds": sorted({candidate.kind for candidate in candidates}),
        }

    _execute_stage(state, reporter, "table_detect", action)
    return runtime["candidates"]


def _run_table_extract(
    *, state: PageState, reporter: StatusReporter, args: argparse.Namespace, runtime: dict[str, Any]
) -> list[TableCandidate]:
    candidates = runtime.get("candidates") or _candidates_from_state(state)

    def action() -> dict[str, Any]:
        transcribe_table_candidates(
            candidates=candidates,
            cells=state.detection_cells,
            page_image_path=Path(state.page_dir) / "page.png",
            page_dir=Path(state.page_dir),
            args=args,
        )
        runtime["candidates"] = candidates
        _sync_page_state(state, candidates=candidates)
        Path(state.page_dir, "table_candidates.json").write_text(
            json.dumps(table_candidate_rows(candidates), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return {
            "candidates": len(candidates),
            "verified": sum(candidate.verified for candidate in candidates),
            "calls": sum(candidate.usage is not None for candidate in candidates),
        }

    _execute_stage(state, reporter, "table_extract", action)
    return candidates


def _run_page_refine(
    *,
    state: PageState,
    reporter: StatusReporter,
    args: argparse.Namespace,
    runtime: dict[str, Any],
    prompt: str,
) -> None:
    records = runtime.get("records") or _records_from_state(state)
    candidates = runtime.get("candidates") or _candidates_from_state(state)
    page_dir = Path(state.page_dir)

    def action() -> dict[str, Any]:
        refined, usage = refine_page_markdown(
            source_markdown=state.raw_markdown,
            layout_blocks=state.layout_map,
            table_candidates=candidates,
            page_image_path=page_dir / "page.png",
            prompt_template=prompt,
            args=args,
        )
        state.refined_markdown = refined
        state.page_vlm_usage = usage
        if usage:
            (page_dir / "page_vlm.md").write_text(refined, encoding="utf-8")
        final_markdown, warnings = postprocess_markdown(
            state.raw_markdown, refined, records, candidates, state.layout_map
        )
        missing_tables = missing_verified_table_ids(final_markdown, candidates)
        if missing_tables:
            warnings["verified_tables_missing"] = missing_tables
        state.final_markdown = final_markdown
        state.warnings = warnings
        state.pre_repair_markdown = final_markdown
        state.pre_repair_warnings = dict(warnings)
        _sync_page_state(state, records=records, candidates=candidates)
        return {
            "calls": 1 if usage else 0,
            "tokens": (usage or {}).get("total_tokens"),
            "warnings": len(warnings),
        }

    _execute_stage(state, reporter, "page_refine", action)


def _run_page_repair(
    *,
    state: PageState,
    reporter: StatusReporter,
    args: argparse.Namespace,
    runtime: dict[str, Any],
    prompt: str,
) -> None:
    records = runtime.get("records") or _records_from_state(state)
    candidates = runtime.get("candidates") or _candidates_from_state(state)
    page_dir = Path(state.page_dir)
    items = runtime.get("items")
    has_runtime_items = items is not None
    items_for_check = items or []
    should_repair = should_run_repair_pass(
        items=items_for_check,
        warnings=state.warnings,
        current_markdown=state.final_markdown,
        records=records,
        table_candidates=candidates,
        has_docling_table=state.has_docling_table if not has_runtime_items else None,
    )
    if not should_repair:
        state.page_repair_usage = None
        _skip_stage(state, reporter, "page_repair", "no_trigger")
        return

    def action() -> dict[str, Any]:
        if has_runtime_items:
            repair_layout_map = build_layout_prompt_map(
                items=items,
                page_size=tuple(state.page_size),
                picture_records=runtime.get("picture_map") or {},
                unplaced_lines=state.warnings.get("unplaced_content_lines", []),
            )
        else:
            repair_layout_map = state.layout_map
        state.repair_layout_map = repair_layout_map
        (page_dir / "layout_prompt_map_repair.json").write_text(
            json.dumps(repair_layout_map, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        repaired, usage = repair_page_markdown(
            current_markdown=state.final_markdown,
            layout_blocks=repair_layout_map,
            table_candidates=candidates,
            page_image_path=page_dir / "page.png",
            unplaced_lines=state.warnings.get("unplaced_content_lines", []),
            prompt_template=prompt,
            args=args,
        )
        state.page_repair_usage = usage
        if not usage:
            return {"calls": 0, "reason": "skip_vlm"}
        (page_dir / "page_repair.md").write_text(repaired, encoding="utf-8")
        repaired_final, repaired_warnings = postprocess_markdown(
            state.raw_markdown, repaired, records, candidates, repair_layout_map
        )
        reject_reasons = repair_regression_reasons(
            state.final_markdown, repaired_final, usage
        )
        if reject_reasons:
            state.warnings["repair_rejected"] = reject_reasons
        else:
            state.final_markdown = repaired_final
            state.warnings = repaired_warnings
            missing_tables = missing_verified_table_ids(state.final_markdown, candidates)
            if missing_tables:
                state.warnings["verified_tables_missing"] = missing_tables
        _sync_page_state(state, records=records, candidates=candidates, repair_layout_map=repair_layout_map)
        return {
            "calls": 1,
            "accepted": not reject_reasons,
            "tokens": usage.get("total_tokens"),
            "warnings": len(state.warnings),
        }

    _execute_stage(state, reporter, "page_repair", action)


def _run_finalize(
    *, state: PageState, reporter: StatusReporter, runtime: dict[str, Any]
) -> None:
    records = runtime.get("records") or _records_from_state(state)
    page_dir = Path(state.page_dir)

    def action() -> dict[str, Any]:
        (page_dir / "docling_final.md").write_text(state.final_markdown, encoding="utf-8")
        write_image_summaries(page_dir / "image_summaries.jsonl", records)
        _sync_page_state(state, records=records)
        return {"warnings": len(state.warnings), "pictures": len(records)}

    _execute_stage(state, reporter, "finalize", action)
    state.status = "completed"
    state.save()


# Public stage names keep the orchestration surface readable; the underscored
# names remain for the runner's explicit dispatch table.
prepare_page = _prepare_page
run_picture_triage = _run_picture_triage
run_picture_extract = _run_picture_extract
run_table_detect = _run_table_detect
run_table_extract = _run_table_extract
run_page_refine = _run_page_refine
run_page_repair = _run_page_repair
run_finalize = _run_finalize

__all__ = [
    "_execute_stage", "_skip_stage", "_prepare_page",
    "_run_picture_triage", "_run_picture_extract", "_run_table_detect",
    "_run_table_extract", "_run_page_refine", "_run_page_repair",
    "_run_finalize", "prepare_page", "run_picture_triage",
    "run_picture_extract", "run_table_detect", "run_table_extract",
    "run_page_refine", "run_page_repair", "run_finalize",
]
