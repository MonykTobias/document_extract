"""Page-stage coordination for the document extraction pipeline."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from ..artifacts import block_rows_for_page, write_image_summaries
from ..docling_adapter import (
    assert_docling_export_surface,
    bbox_dict,
    is_furniture_item,
    is_picture_item,
    is_table_item,
    item_text,
    iter_doc_items,
    iter_furniture_items,
    rasterize_page,
)
from ..layout.furniture import furniture_band, is_chapter_tab
from ..layout.geometry import bbox_to_normalized_rect
from ..layout.prompt_map import (
    annotate_picture_values,
    build_layout_prompt_map,
    detection_cells_from_items,
    detection_cells_from_layout_map,
)
from ..markdown.postprocess import (
    looks_like_toc,
    normalize_furniture_text,
    strip_furniture_lines,
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
from ..table_reconstruction import (
    extract_table_page_geometry,
    render_reconstruction_overlay,
)
from ..tables import (
    build_table_candidates,
    render_layout_overlay,
    render_table_candidates_overlay,
    table_candidate_rows,
    transcribe_table_candidates,
)
from ..visual_values import (
    apply_trusted_visual_values,
    associate_table_cells,
    collect_tagged_candidates,
    merge_picture_evidence,
    reconcile_picture_evidence,
    update_visual_completeness,
    visual_audit_summary,
    visual_candidate_rows,
)
from .state import (
    _candidates_from_state,
    _records_from_state,
    _sync_page_state,
    _visual_candidates_from_state,
)


def _repair_unplaced_lines(
    warnings: dict[str, Any], records: list[PictureRecord]
) -> list[str]:
    """Combine ordinary and instance-aware table-symbol repair instructions."""
    lines = list(warnings.get("unplaced_content_lines", []))
    records_by_placeholder = {record.placeholder: record for record in records}
    for placeholder in warnings.get("table_symbols_unplaced", []):
        record = records_by_placeholder.get(placeholder)
        if record is None:
            continue
        lines.append(
            "table symbol "
            f"picture_index={record.index} value={record.summary.strip()} is not placed; "
            "use the matching layout block bbox"
        )
    return list(dict.fromkeys(line for line in lines if line))

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


def _split_furniture_items(
    items: list[Any],
    document: Any,
    page_number: int,
    page_size: tuple[float, float],
    furniture_signatures: set[tuple[str, str]],
) -> tuple[list[Any], set[str], int]:
    """Partition a page's items into (kept, furniture_texts, dropped_count).

    Drops Docling-labelled furniture, sidebar chapter tabs, and body items
    matching a cross-page repetition signature. Also collects the texts of the
    page's furniture-layer items (footers the body export never contains) so
    the postprocess strip can remove VLM transcriptions of them.
    """
    kept: list[Any] = []
    texts: set[str] = set()
    kept_texts: set[str] = set()
    dropped = 0
    for item in items:
        if is_picture_item(item) or is_table_item(item):
            kept.append(item)
            continue
        text = item_text(item)
        rect = bbox_to_normalized_rect(bbox_dict(item), page_size)
        if is_furniture_item(item):
            dropped += 1
            normalized = normalize_furniture_text(text)
            if normalized and re.search(r"[^\W\d_]", normalized):
                texts.add(normalized)
            continue
        if text and is_chapter_tab(text, rect):
            dropped += 1
            continue
        normalized = normalize_furniture_text(text) if text else ""
        if normalized and (normalized, furniture_band(rect)) in furniture_signatures:
            dropped += 1
            texts.add(normalized)
            continue
        if normalized:
            kept_texts.add(normalized)
        kept.append(item)
    for furniture in iter_furniture_items(document, page_number):
        normalized = normalize_furniture_text(item_text(furniture))
        if normalized and re.search(r"[^\W\d_]", normalized):
            texts.add(normalized)
    # Text-level stripping cannot see bands: on a section's first page the
    # genuine heading shares the running header's normalized text (observed:
    # page 22 ``1.6 RISK FACTORS``). Never strip a text a kept item still uses.
    return kept, texts - kept_texts, dropped


def _prepare_page(
    *,
    state: PageState,
    reporter: StatusReporter,
    args: argparse.Namespace,
    pdf_doc: Any,
    document: Any,
    page_size: tuple[float, float],
    furniture_signatures: set[tuple[str, str]] | None = None,
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
        items, furniture_texts, furniture_dropped = _split_furniture_items(
            items,
            document,
            state.page,
            page_size,
            furniture_signatures or set(),
        )
        state.furniture_texts = sorted(furniture_texts)
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
        # The Docling-serializer export path renders the page's full body item
        # list, so furniture dropped from ``items`` can still be in the text.
        raw_markdown = strip_furniture_lines(raw_markdown, furniture_texts)
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
        state.page_role = "toc" if looks_like_toc(raw_markdown) else ""
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
            "furniture_dropped": furniture_dropped,
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
    *,
    state: PageState,
    reporter: StatusReporter,
    runtime: dict[str, Any],
    reader: Any | None = None,
    mode: str = "off",
    pdf_page: Any | None = None,
) -> list[TableCandidate]:
    records = runtime.get("records") or _records_from_state(state)
    page_dir = Path(state.page_dir)

    def action() -> dict[str, Any]:
        # A missing reader is recorded by the collector as a failure diagnostic:
        # audit must never fail a page that `off` would complete.
        picture_map = {record.index: record for record in records}
        # Collected once per page and shared by every table on it. A missing
        # page yields no geometry, which reconciliation reads as "no evidence"
        # and leaves each grid exactly as Docling built it.
        page_geometry = (
            extract_table_page_geometry(pdf_page, tuple(state.page_size))
            if pdf_page is not None
            else None
        )
        page_image = page_dir / "page.png"
        candidates = build_table_candidates(
            cells=state.detection_cells,
            page_size=tuple(state.page_size),
            picture_records=picture_map,
            layout_map=state.layout_map,
            page_geometry=page_geometry,
            page_image_path=str(page_image) if page_image.exists() else None,
        )
        _write_reconstruction_sidecar(page_dir, candidates, page_geometry)
        overlay_path = page_dir / "table_reconstruction_overlay.png"
        if not render_reconstruction_overlay(
            page_image_path=page_dir / "page.png",
            candidates=candidates,
            geometry=page_geometry,
            page_size=tuple(state.page_size),
            output_path=overlay_path,
        ):
            overlay_path.unlink(missing_ok=True)
        visual_candidates = []
        if mode != "off":
            visual_candidates = collect_tagged_candidates(
                reader, state.page, tuple(state.page_size)
            )
            visual_candidates = merge_picture_evidence(visual_candidates, records)
            associate_table_cells(visual_candidates, candidates, tuple(state.page_size))
            update_visual_completeness(candidates, visual_candidates, mode=mode)
            state.visual_audit = visual_audit_summary(
                candidates, visual_candidates, mode=mode
            )
            (page_dir / "visual_candidates.json").write_text(
                json.dumps(
                    visual_candidate_rows(visual_candidates),
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        else:
            state.visual_audit = {}
            visual_path = page_dir / "visual_candidates.json"
            if visual_path.exists():
                visual_path.unlink()
        state.visual_values_mode = mode
        runtime["records"] = records
        runtime["candidates"] = candidates
        runtime["visual_candidates"] = visual_candidates
        _sync_page_state(
            state,
            records=records,
            candidates=candidates,
            visual_candidates=visual_candidates,
        )
        (page_dir / "table_candidates.json").write_text(
            json.dumps(table_candidate_rows(candidates), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        render_table_candidates_overlay(
            page_image_path=page_dir / "page.png",
            candidates=candidates,
            output_path=page_dir / "table_candidates_overlay.png",
        )
        audits = [(candidate.stats or {}).get("reconstruction", {}) for candidate in candidates]
        statuses = [audit.get("status") for audit in audits]
        return {
            "candidates": len(candidates),
            "kinds": sorted({candidate.kind for candidate in candidates}),
            "visual_candidates": len(visual_candidates),
            "visual": (state.visual_audit or {}).get("table_status_counts"),
            "tables_unchanged": statuses.count("unchanged"),
            "tables_repaired": statuses.count("repaired"),
            "tables_abstained": statuses.count("abstained"),
            "rules_dropped_invisible": sum(
                len(audit.get("dropped_invisible_rules") or []) for audit in audits
            ),
            "fragments_merged": sum(len(audit.get("merged_fragments") or []) for audit in audits),
            "collision_fallback_columns": sum(
                len(audit.get("slot_collisions") or {}) for audit in audits
            ),
        }

    _execute_stage(state, reporter, "table_detect", action)
    return runtime["candidates"]


def _write_reconstruction_sidecar(
    page_dir: Path,
    candidates: list[TableCandidate],
    page_geometry: dict[str, Any] | None,
) -> None:
    """Per-table reconstruction provenance, kept on repair and abstention alike.

    The full audit lives here rather than on the candidate so checkpoints and
    prompts stay compact. Deterministic ordering keeps two runs byte-identical.
    """
    rows = [
        {
            "candidate_id": candidate.candidate_id,
            "reconstruction_version": (candidate.stats or {}).get("reconstruction_version"),
            **(candidate.stats or {}).get("reconstruction", {}),
        }
        for candidate in candidates
        if (candidate.stats or {}).get("reconstruction")
    ]
    path = page_dir / "table_reconstruction.json"
    if not rows:
        path.unlink(missing_ok=True)
        return
    payload = {
        "source_rules": len((page_geometry or {}).get("rules") or []),
        "source_lines": len((page_geometry or {}).get("lines") or []),
        "tables": rows,
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


def _run_table_extract(
    *, state: PageState, reporter: StatusReporter, args: argparse.Namespace, runtime: dict[str, Any]
) -> list[TableCandidate]:
    candidates = runtime.get("candidates") or _candidates_from_state(state)
    records = runtime.get("records") or _records_from_state(state)
    visual_candidates = runtime.get("visual_candidates") or _visual_candidates_from_state(state)
    mode = state.visual_values_mode or "off"

    def action() -> dict[str, Any]:
        inserted = apply_trusted_visual_values(candidates, visual_candidates, mode=mode)
        if mode != "off":
            # Transcription's authority fast path must see the completeness the
            # insertion just produced, not the detect-time status: a table the
            # insertion completed is authoritative and needs no VLM pass.
            update_visual_completeness(candidates, visual_candidates, mode=mode)
        symbol_records = [
            record
            for record in records
            if record.triage_type == "symbol" and record.summarize and not record.summary
        ]
        if symbol_records:
            from ..prompts import DEFAULT_IMAGE_SUMMARY_PROMPT

            summarize_pictures(
                records=symbol_records,
                prompt_template=DEFAULT_IMAGE_SUMMARY_PROMPT,
                args=args,
                page_image_path=Path(state.page_dir) / "page.png",
                page_size=tuple(state.page_size),
                cells=state.detection_cells,
            )
        transcribe_table_candidates(
            candidates=candidates,
            cells=state.detection_cells,
            page_image_path=Path(state.page_dir) / "page.png",
            page_dir=Path(state.page_dir),
            args=args,
            picture_records=records,
            page_size=tuple(state.page_size),
        )
        runtime["candidates"] = candidates
        runtime["records"] = records
        runtime["visual_candidates"] = visual_candidates
        if mode != "off":
            reconcile_picture_evidence(candidates, visual_candidates)
            update_visual_completeness(candidates, visual_candidates, mode=mode)
            state.visual_audit = visual_audit_summary(
                candidates, visual_candidates, mode=mode
            )
            Path(state.page_dir, "visual_candidates.json").write_text(
                json.dumps(
                    visual_candidate_rows(visual_candidates),
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        _sync_page_state(
            state,
            records=records,
            candidates=candidates,
            visual_candidates=visual_candidates,
        )
        Path(state.page_dir, "table_candidates.json").write_text(
            json.dumps(table_candidate_rows(candidates), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return {
            "candidates": len(candidates),
            "verified": sum(candidate.verified for candidate in candidates),
            "calls": sum(candidate.usage is not None for candidate in candidates),
            "visual_inserted": inserted,
            "visual": (state.visual_audit or {}).get("table_status_counts"),
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
        if runtime.get("items") is not None:
            layout_map = build_layout_prompt_map(
                items=runtime["items"],
                page_size=tuple(state.page_size),
                picture_records=runtime.get("picture_map") or {},
            )
        else:
            layout_map = annotate_picture_values(dict(state.layout_map), records)
        state.layout_map = layout_map
        (page_dir / "layout_prompt_map.json").write_text(
            json.dumps(layout_map, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        refined, usage = refine_page_markdown(
            source_markdown=state.raw_markdown,
            layout_blocks=layout_map,
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
            state.raw_markdown,
            refined,
            records,
            candidates,
            layout_map,
            furniture_texts=set(state.furniture_texts),
            page_role=state.page_role or None,
        )
        missing_tables = missing_verified_table_ids(final_markdown, candidates)
        if missing_tables:
            warnings["verified_tables_missing"] = missing_tables
        state.final_markdown = final_markdown
        state.warnings = warnings
        state.pre_repair_markdown = final_markdown
        state.pre_repair_warnings = dict(warnings)
        _sync_page_state(
            state,
            records=records,
            candidates=candidates,
            layout_map=layout_map,
        )
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
        repair_unplaced_lines = _repair_unplaced_lines(state.warnings, records)
        if has_runtime_items:
            repair_layout_map = build_layout_prompt_map(
                items=items,
                page_size=tuple(state.page_size),
                picture_records=runtime.get("picture_map") or {},
                unplaced_lines=repair_unplaced_lines,
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
            unplaced_lines=repair_unplaced_lines,
            prompt_template=prompt,
            args=args,
        )
        state.page_repair_usage = usage
        if not usage:
            return {"calls": 0, "reason": "skip_vlm"}
        (page_dir / "page_repair.md").write_text(repaired, encoding="utf-8")
        repaired_final, repaired_warnings = postprocess_markdown(
            state.raw_markdown,
            repaired,
            records,
            candidates,
            repair_layout_map,
            furniture_texts=set(state.furniture_texts),
            page_role=state.page_role or None,
        )
        reject_reasons = repair_regression_reasons(
            state.final_markdown,
            repaired_final,
            usage,
            records=records,
            table_candidates=candidates,
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
