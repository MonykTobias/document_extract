"""Compact layout-map construction for refinement and table detection.

The map is derived from Docling items and intentionally contains selected text
and geometry only; raw Docling objects never enter VLM prompts.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..docling_adapter import (
    bbox_dict,
    caption_text,
    is_heading_item,
    is_picture_item,
    is_table_item,
    item_kind,
    item_text,
)
from .geometry import bbox_area_ratio, bbox_to_normalized_rect, rect_center, rect_distance

NON_CELL_KINDS = {"footnote", "page_footer", "page_header"}
NEARBY_BLOCK_DISTANCE = 0.12
NUMERIC_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)*")
WORD_TOKEN_RE = re.compile(r"[^\W\d_]{4,}", re.UNICODE)
VALUE_SIGNAL_RE = re.compile(
    r"(?i)(?:\b(?:19|20)\d{2}\b|\d[\d,.\s]*(?:%|pts?|bps?|bn|m|k|kg|g|t|tons?|"
    r"tonnes?|co2|co2e|eur|usd|gbp|l|ml|ha|m3)\b|[$\u20ac\u00a3]\s*\d|\d[\d,.\s]*[$\u20ac\u00a3])"
)
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
VALUE_ONLY_RE = re.compile(
    r"^\s*(?:[<>~]?\s*)?(?:\+|-)?(?:\d[\d,.\s]*)(?:%|pts?|bn|m|k|yo|yo\.|\u20ac|\$|\u00a3|cumulated)?\s*$",
    re.IGNORECASE,
)

def text_has_value_signal(text: str) -> bool:
    return bool(VALUE_SIGNAL_RE.search(text))


def is_timeline_candidate(text: str) -> bool:
    stripped = text.strip()
    return bool(YEAR_RE.fullmatch(stripped) or VALUE_ONLY_RE.match(stripped))


def is_table_like_kind(kind: str) -> bool:
    return kind in {"table", "table_cell", "caption"} or "table" in kind


def prompt_block_type(item: Any) -> str:
    kind = item_kind(item)
    if is_picture_item(item):
        return "picture"
    if is_table_item(item):
        return "table"
    if kind == "text":
        return "paragraph"
    return kind


def truncate_prompt_text(text: str, limit: int) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 1)].rstrip() + "..."


def layout_unplaced_match(text: str, unplaced_lines: list[str] | None) -> bool:
    if not text or not unplaced_lines:
        return False
    lower_text = text.lower()
    for line in unplaced_lines:
        lower_line = line.lower().strip()
        if not lower_line:
            continue
        if lower_text in lower_line or lower_line[:120] in lower_text:
            return True
    return False


def timeline_cluster_indices(entries: list[dict[str, Any]]) -> set[int]:
    candidates: list[tuple[int, tuple[float, float]]] = []
    for index, entry in enumerate(entries):
        if not entry["rect"] or not is_timeline_candidate(entry["text"]):
            continue
        center = rect_center(entry["rect"])
        if center is not None:
            candidates.append((index, center))
    clustered: set[int] = set()
    for axis in (0, 1):
        for index, center in candidates:
            aligned = [
                other_index
                for other_index, other_center in candidates
                if abs(center[axis] - other_center[axis]) <= 0.08
            ]
            if len(aligned) >= 3:
                clustered.update(aligned)
    return clustered


def nearby_layout_context(
    entry_index: int,
    entries: list[dict[str, Any]],
    timeline_indices: set[int],
) -> dict[str, Any]:
    entry = entries[entry_index]
    structural_count = 0
    near_structured = False
    for other_index, other in enumerate(entries):
        if other_index == entry_index:
            continue
        distance = rect_distance(entry["rect"], other["rect"])
        if distance > NEARBY_BLOCK_DISTANCE:
            continue
        other_structural = (
            other["is_picture"]
            or other["is_table"]
            or other["is_heading"]
            or other["kind"] == "caption"
            or other_index in timeline_indices
            or text_has_value_signal(other["text"])
        )
        if other_structural:
            structural_count += 1
            near_structured = True
    return {
        "near_structured": near_structured,
        "multiple_candidate_parents": structural_count >= 2,
        "in_timeline_cluster": entry_index in timeline_indices,
    }


def should_include_layout_text(
    *,
    entry: dict[str, Any],
    context: dict[str, Any],
    unplaced_lines: list[str] | None,
) -> bool:
    text = entry["text"].strip()
    if not text:
        return False
    kind = entry["kind"]
    area_ratio = entry["area_ratio"]
    if entry["is_heading"] or is_table_like_kind(kind):
        return True
    if text_has_value_signal(text):
        return True
    if len(text) <= 240 and area_ratio <= 0.03:
        return True
    if context["near_structured"]:
        return True
    if context["multiple_candidate_parents"]:
        return True
    if layout_unplaced_match(text, unplaced_lines):
        return True
    if context["in_timeline_cluster"] or is_timeline_candidate(text):
        return True
    return False


def build_layout_prompt_map(
    items: list[Any],
    page_size: tuple[float, float],
    picture_records: dict[int, PictureRecord],
    unplaced_lines: list[str] | None = None,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        bbox = bbox_dict(item)
        text = item_text(item)
        caption = caption_text(item)
        record = picture_records.get(id(item))
        if record and record.caption:
            caption = record.caption
        kind = item_kind(item)
        rect = bbox_to_normalized_rect(bbox, page_size)
        critical_text = bool(text and (text_has_value_signal(text) or layout_unplaced_match(text, unplaced_lines)))
        if rect is None and not critical_text:
            continue
        entries.append(
            {
                "id": f"b{index:04d}",
                "item": item,
                "kind": kind,
                "type": prompt_block_type(item),
                "bbox": bbox,
                "rect": rect,
                "area_ratio": bbox_area_ratio(bbox, page_size),
                "text": text,
                "caption": caption,
                "is_picture": is_picture_item(item),
                "is_table": is_table_item(item),
                "is_heading": is_heading_item(item),
            }
        )

    timeline_indices = timeline_cluster_indices(entries)
    blocks: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        block: dict[str, Any] = {
            "id": entry["id"],
            "type": entry["type"],
            "bbox": entry["rect"],
        }
        if entry["is_picture"]:
            if entry["caption"]:
                block["caption"] = truncate_prompt_text(entry["caption"], 240)
            blocks.append(block)
            continue

        context = nearby_layout_context(index, entries, timeline_indices)
        if should_include_layout_text(
            entry=entry,
            context=context,
            unplaced_lines=unplaced_lines,
        ):
            limit = 500 if entry["is_table"] else 240
            block["text"] = truncate_prompt_text(entry["text"], limit)
        blocks.append(block)

    return {
        "page_size": [round(page_size[0], 3), round(page_size[1], 3)],
        "blocks": blocks,
    }


def layout_map_stats(layout_map: dict[str, Any]) -> dict[str, int]:
    blocks = layout_map.get("blocks", [])
    return {
        "layout_block_count": len(blocks),
        "layout_text_block_count": sum(
            1 for block in blocks if block.get("text") or block.get("caption")
        ),
    }


def layout_map_prompt_json(layout_map: dict[str, Any]) -> str:
    return json.dumps(layout_map, ensure_ascii=False, separators=(",", ":"))


def collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def detection_cells_from_items(
    items: list[Any], page_size: tuple[float, float]
) -> list[dict[str, Any]]:
    """Text cells with full (untruncated) text for table-region detection.

    Ids match build_layout_prompt_map ids: both enumerate the same item list.
    """
    cells: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        if is_picture_item(item) or is_table_item(item):
            continue
        if item_kind(item) in NON_CELL_KINDS:
            continue
        text = collapse_ws(item_text(item))
        rect = bbox_to_normalized_rect(bbox_dict(item), page_size)
        if not text or rect is None:
            continue
        cells.append(
            {
                "id": f"b{index:04d}",
                "rect": rect,
                "text": text,
                "is_heading": is_heading_item(item),
            }
        )
    return cells


def detection_cells_from_layout_map(layout_map: dict[str, Any]) -> list[dict[str, Any]]:
    """Fallback cell source (tests / offline replay); text may be truncated."""
    cells: list[dict[str, Any]] = []
    for block in layout_map.get("blocks", []):
        if block.get("type") in {"picture", "table"} or block.get("type") in NON_CELL_KINDS:
            continue
        text = collapse_ws(str(block.get("text") or ""))
        rect = block.get("bbox")
        if not text or not rect:
            continue
        cells.append(
            {
                "id": block["id"],
                "rect": rect,
                "text": text,
                "is_heading": block.get("type") in {"title", "section_header"},
            }
        )
    return cells

__all__ = [
    "text_has_value_signal", "is_timeline_candidate", "is_table_like_kind",
    "prompt_block_type", "truncate_prompt_text", "layout_unplaced_match",
    "timeline_cluster_indices", "nearby_layout_context", "should_include_layout_text",
    "build_layout_prompt_map", "layout_map_stats", "layout_map_prompt_json",
    "collapse_ws", "detection_cells_from_items", "detection_cells_from_layout_map",
]
