"""Checkpoint-domain conversion and page-relative artifact synchronization."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..models import PictureRecord, TableCandidate, VisualCandidate
from ..runtime import PageState, relative_page_path, resolve_page_path

def _picture_state_rows(records: list[PictureRecord], page_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        row = asdict(record)
        row["abs_path"] = relative_page_path(record.abs_path, page_dir)
        rows.append(row)
    return rows


def _records_from_state(state: PageState) -> list[PictureRecord]:
    fields = set(PictureRecord.__dataclass_fields__)
    records: list[PictureRecord] = []
    page_dir = Path(state.page_dir)
    for row in state.picture_records:
        payload = {key: value for key, value in row.items() if key in fields}
        payload["abs_path"] = resolve_page_path(payload.get("abs_path"), page_dir)
        records.append(PictureRecord(**payload))
    return records


def _candidate_state_rows(candidates: list[TableCandidate], page_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        row = asdict(candidate)
        row["crop_path"] = relative_page_path(candidate.crop_path, page_dir)
        rows.append(row)
    return rows


def _candidates_from_state(state: PageState) -> list[TableCandidate]:
    fields = set(TableCandidate.__dataclass_fields__)
    page_dir = Path(state.page_dir)
    candidates: list[TableCandidate] = []
    for row in state.table_candidates:
        payload = {key: value for key, value in row.items() if key in fields}
        payload["crop_path"] = resolve_page_path(payload.get("crop_path"), page_dir)
        candidates.append(TableCandidate(**payload))
    return candidates


def _visual_candidate_state_rows(candidates: list[VisualCandidate]) -> list[dict[str, Any]]:
    return [asdict(candidate) for candidate in candidates]


def _visual_candidates_from_state(state: PageState) -> list[VisualCandidate]:
    fields = set(VisualCandidate.__dataclass_fields__)
    candidates: list[VisualCandidate] = []
    for row in state.visual_candidates:
        payload = {key: value for key, value in row.items() if key in fields}
        candidates.append(VisualCandidate(**payload))
    return candidates


def _sync_page_state(
    state: PageState,
    *,
    records: list[PictureRecord] | None = None,
    detection_cells: list[dict[str, Any]] | None = None,
    layout_map: dict[str, Any] | None = None,
    repair_layout_map: dict[str, Any] | None = None,
    candidates: list[TableCandidate] | None = None,
    visual_candidates: list[VisualCandidate] | None = None,
) -> None:
    page_dir = Path(state.page_dir)
    if records is not None:
        state.picture_records = _picture_state_rows(records, page_dir)
    if detection_cells is not None:
        state.detection_cells = detection_cells
    if layout_map is not None:
        state.layout_map = layout_map
    if repair_layout_map is not None:
        state.repair_layout_map = repair_layout_map
    if candidates is not None:
        state.table_candidates = _candidate_state_rows(candidates, page_dir)
    if visual_candidates is not None:
        state.visual_candidates = _visual_candidate_state_rows(visual_candidates)
    state.artifact_paths.update(
        {
            "page_image": "page.png",
            "vlm_page_image": "page_vlm_input.png",
            "layout_overlay": "page_layout_overlay.png",
            "raw_markdown": "docling_raw.md",
            "layout_map": "layout_prompt_map.json",
            "repair_layout_map": "layout_prompt_map_repair.json",
            "table_candidates": "table_candidates.json",
            "table_overlay": "table_candidates_overlay.png",
            "table_reconstruction": "table_reconstruction.json",
            "table_reconstruction_overlay": "table_reconstruction_overlay.png",
            "visual_candidates": "visual_candidates.json",
            "final_markdown": "docling_final.md",
            "image_summaries": "image_summaries.jsonl",
            "checkpoint": "page_state.json",
        }
    )

__all__ = [
    "_picture_state_rows", "_records_from_state", "_candidate_state_rows",
    "_candidates_from_state", "_visual_candidate_state_rows",
    "_visual_candidates_from_state", "_sync_page_state",
]
