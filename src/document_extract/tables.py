"""Table-region detection, verification, transcription, and overlays."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .layout.geometry import (
    axis_gaps,
    bbox_to_pixel_rect,
    bbox_to_normalized_rect,
    cluster_axis_values,
    nearest_cluster_index,
    normalized_rect_to_pixel_rect,
    partition_cells_at,
    rect_aspect_ratio,
    rect_distance,
    rect_overlap_ratio,
    rect_union,
)
from .layout.prompt_map import (
    NUMERIC_TOKEN_RE,
    WORD_TOKEN_RE,
    VALUE_ONLY_RE,
    VALUE_SIGNAL_RE,
    YEAR_RE,
    collapse_ws,
    text_has_value_signal,
    truncate_prompt_text,
)
from .llm import ollama as ollama_client
from .markdown import postprocess as sp
from .markdown.formatting import normalize_pipe_tables
from .models import PictureRecord, TableCandidate
# Accessed via the module so apply_detection_config's runtime overrides of the
# picture thresholds are visible here; a from-import would freeze the defaults.
from . import pictures
from .pictures import save_region_crop
from .prompts import DEFAULT_KPI_PANEL_PROMPT, DEFAULT_TABLE_REGION_PROMPT
from .docling_adapter import bbox_dict, item_kind

PANEL_X_GUTTER = 0.025
PANEL_Y_GUTTER = 0.06
PANEL_BANNER_MIN_WIDTH_RATIO = 0.25
PANEL_BANNER_WIDE_WIDTH_RATIO = 0.5
NON_CELL_KINDS = {"footnote", "page_footer", "page_header"}
TABLE_MIN_CELLS = 6
TABLE_MIN_COLUMNS = 2
TABLE_MIN_COLUMN_MEMBERS = 3
TABLE_MIN_ALIGNED_ROWS = 3
TABLE_COLUMN_X_TOLERANCE = 0.025
TABLE_ROW_Y_TOLERANCE = 0.02
TABLE_PROSE_MEDIAN_CHARS = 200
TABLE_LONG_CELL_CHARS = 200
TABLE_LONG_CELL_MAX_RATIO = 0.3
TABLE_TIMELINE_MAX_YEAR_CELLS = 3
TABLE_SHORT_CELL_CHARS = 120
TABLE_VALUE_CELL_CHARS = 160
TABLE_WIDE_CELL_RATIO = 0.7
TABLE_PAGE_SIZED_WIDTH = 0.75
TABLE_PAGE_SIZED_HEIGHT = 0.55
TABLE_SCORE_THRESHOLD = 0.55
TABLE_MERGE_MAX_X_GAP = 0.2
TABLE_MERGE_MIN_Y_OVERLAP = 0.5
TABLE_MERGE_MIN_ANCHOR = 0.9
TABLE_REGIONS_MAX_PER_PAGE = 3
TABLE_NUMERIC_COVERAGE_MIN = 0.9
TABLE_WORD_COVERAGE_MIN = 0.8

# Deterministic grid-shape normalization (title/detail + regular tables).
GRID_MIN_RECORDS = 2
GRID_MIN_CONTENT_COLUMNS = 2
GRID_TITLE_DETAIL_MIN_PAIRS = 2
GRID_HEADER_CELL_MAX_CHARS = 60
GRID_SYMBOL_ROW_TOLERANCE = 0.006
# Delimiter for several symbol values sharing one coverage cell (``E1, E2, S3``).
GRID_SYMBOL_JOIN = ", "
# Re-export the pure table-bullet primitives for existing callers. Their home
# is ``markdown.postprocess`` so sectioned-table rendering can use the exact
# same semantics without importing this heavier module.
GRID_BULLET_CHARS_STRONG = sp.GRID_BULLET_CHARS_STRONG
GRID_BULLET_CHARS_WEAK = sp.GRID_BULLET_CHARS_WEAK
GRID_BULLET_CHARS = sp.GRID_BULLET_CHARS
_cell_with_bullets = sp.cell_with_bullets
_became_bullet_list = sp.became_bullet_list

def segment_panels(cells: list[dict[str, Any]], depth: int = 0) -> list[list[dict[str, Any]]]:
    """Recursive XY-cut: split at vertical gutters first, then horizontal ones.

    Multi-column prose splits into single-column panels (which can never look
    like tables), while a real table's columns stay together because its
    banner/header rows span across the column gaps.
    """
    if len(cells) < TABLE_MIN_CELLS or depth >= 3:
        return [cells]
    # Never y-split a single-column group: a severed label column has sparse
    # row spacing and would shred into fragments, losing its only chance of
    # being re-joined with its content column by the merge pass.
    x_clusters = cluster_axis_values(
        [cell["rect"][0] for cell in cells], TABLE_COLUMN_X_TOLERANCE
    )
    if depth > 0 and len(x_clusters) < 2:
        return [cells]
    for axis, min_gap in ((0, PANEL_X_GUTTER), (1, PANEL_Y_GUTTER)):
        boundaries = axis_gaps(cells, axis, min_gap)
        if not boundaries:
            continue
        groups = partition_cells_at(cells, axis, boundaries)
        if len(groups) > 1:
            out: list[list[dict[str, Any]]] = []
            for group in groups:
                out.extend(segment_panels(group, depth + 1))
            return out
    return [cells]


def is_banner_cell(cell: dict[str, Any], panel_width: float) -> bool:
    """Section banners: heading blocks that are wide, or all-caps and not tiny.

    The all-caps escape matters because a banner's *text* bbox can be much
    narrower than the colored band it sits on (e.g. "THRIVING PEOPLE &
    COMMUNITIES" centered on a full-width bar).
    """
    if not cell.get("is_heading"):
        return False
    width = cell["rect"][2] - cell["rect"][0]
    if width < PANEL_BANNER_MIN_WIDTH_RATIO * panel_width:
        return False
    if width >= PANEL_BANNER_WIDE_WIDTH_RATIO * panel_width:
        return True
    text = cell["text"]
    return len(text) >= 8 and text.upper() == text and any(ch.isalpha() for ch in text)


def split_panel_at_banners(cells: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split a panel at section-banner heading blocks."""
    if len(cells) < TABLE_MIN_CELLS:
        return [cells]
    union = rect_union([cell["rect"] for cell in cells])
    if not union:
        return [cells]
    # In a single-column panel (e.g. a severed label column) every cell looks
    # "banner-wide" relative to the narrow panel — never split those.
    x_clusters = cluster_axis_values(
        [cell["rect"][0] for cell in cells], TABLE_COLUMN_X_TOLERANCE
    )
    if len(x_clusters) < 2:
        return [cells]
    panel_width = max(union[2] - union[0], 0.001)
    banners = sorted(
        (cell for cell in cells if is_banner_cell(cell, panel_width)),
        key=lambda cell: (cell["rect"][1] + cell["rect"][3]) / 2,
    )
    if not banners:
        return [cells]
    banner_ids = {cell["id"] for cell in banners}
    boundaries = [(cell["rect"][1] + cell["rect"][3]) / 2 for cell in banners]
    groups: list[list[dict[str, Any]]] = [[] for _ in range(len(boundaries) + 1)]
    for cell in cells:
        if cell["id"] in banner_ids:
            continue
        center_y = (cell["rect"][1] + cell["rect"][3]) / 2
        groups[sum(1 for boundary in boundaries if center_y > boundary)].append(cell)
    return [group for group in groups if group]


def region_grid_cells(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cells that can be table cells: everything except near-full-width lines.

    Headings stay in: Docling regularly labels short styled row labels as
    section_header; true section banners are wide and removed here or already
    consumed by split_panel_at_banners.
    """
    union = rect_union([cell["rect"] for cell in cells])
    if not union:
        return []
    region_width = max(union[2] - union[0], 0.001)
    return [
        cell
        for cell in cells
        if cell["rect"][2] - cell["rect"][0] <= TABLE_WIDE_CELL_RATIO * region_width
    ]


def is_value_cell(text: str) -> bool:
    stripped = text.strip()
    if VALUE_ONLY_RE.match(stripped):
        return True
    return len(stripped) <= TABLE_VALUE_CELL_CHARS and bool(VALUE_SIGNAL_RE.search(stripped))


def region_grid_rows(cells: list[dict[str, Any]]) -> list[list[str]]:
    """Cluster region cells into reading-order rows for KPI shape checks."""
    if not cells:
        return []
    row_centers = cluster_axis_values(
        [(cell["rect"][1] + cell["rect"][3]) / 2 for cell in cells],
        TABLE_ROW_Y_TOLERANCE,
    )
    rows: list[list[dict[str, Any]]] = [[] for _ in row_centers]
    for cell in cells:
        center_y = (cell["rect"][1] + cell["rect"][3]) / 2
        rows[nearest_cluster_index(center_y, row_centers)].append(cell)
    return [
        [str(cell.get("text", "")) for cell in sorted(row, key=lambda cell: cell["rect"][0])]
        for row in rows
        if row
    ]


def score_table_region(
    cells: list[dict[str, Any]], *, allow_page_sized: bool = False
) -> tuple[float, dict[str, Any]]:
    """Score how table-like a region is; 0.0 with stats["reject"] on hard veto.

    Discriminative signals (vs. multi-column prose): short cells, cross-column
    row alignment on left edges, and value-dominated columns. Prose columns
    have long cells and rows that never align across columns.
    """
    stats: dict[str, Any] = {"cells": len(cells)}
    if len(cells) < TABLE_MIN_CELLS:
        stats["reject"] = "too_few_cells"
        return 0.0, stats
    grid_cells = region_grid_cells(cells)
    stats["grid_cells"] = len(grid_cells)
    if len(grid_cells) < TABLE_MIN_CELLS:
        stats["reject"] = "too_few_grid_cells"
        return 0.0, stats

    lengths = sorted(len(cell["text"]) for cell in grid_cells)
    median_chars = lengths[len(lengths) // 2]
    stats["median_cell_chars"] = median_chars
    if median_chars > TABLE_PROSE_MEDIAN_CHARS:
        stats["reject"] = "prose_region"
        return 0.0, stats
    long_cell_ratio = sum(
        1 for cell in grid_cells if len(cell["text"]) > TABLE_LONG_CELL_CHARS
    ) / len(grid_cells)
    stats["long_cell_ratio"] = round(long_cell_ratio, 3)
    if long_cell_ratio > TABLE_LONG_CELL_MAX_RATIO:
        stats["reject"] = "prose_region"
        return 0.0, stats
    year_cells = sum(1 for cell in grid_cells if YEAR_RE.fullmatch(cell["text"].strip()))
    stats["year_cells"] = year_cells
    if year_cells > TABLE_TIMELINE_MAX_YEAR_CELLS:
        stats["reject"] = "timeline_region"
        return 0.0, stats
    union = rect_union([cell["rect"] for cell in grid_cells])
    if (
        not allow_page_sized
        and union
        and union[2] - union[0] > TABLE_PAGE_SIZED_WIDTH
        and union[3] - union[1] > TABLE_PAGE_SIZED_HEIGHT
    ):
        # Implicit styled tables in report layouts live inside panels; a region
        # covering most of the page is almost always merged mixed content.
        # (Genuine full-page ruled tables come in via Docling's table items.
        # Merged label/content unions are exempt: their own acceptance rules
        # demand a header row and near-perfect label alignment instead.)
        stats["reject"] = "page_sized_region"
        return 0.0, stats

    column_centers = cluster_axis_values(
        [cell["rect"][0] for cell in grid_cells], TABLE_COLUMN_X_TOLERANCE
    )
    column_members: dict[int, list[dict[str, Any]]] = {}
    for cell in grid_cells:
        column_members.setdefault(
            nearest_cluster_index(cell["rect"][0], column_centers), []
        ).append(cell)
    major_columns = {
        index: members
        for index, members in column_members.items()
        if len(members) >= TABLE_MIN_COLUMN_MEMBERS
    }
    stats["columns"] = len(major_columns)
    if len(major_columns) < TABLE_MIN_COLUMNS:
        stats["reject"] = "not_enough_columns"
        return 0.0, stats

    aligned_cells = [
        (column_index, cell)
        for column_index, members in major_columns.items()
        for cell in members
    ]
    row_centers = cluster_axis_values(
        [(cell["rect"][1] + cell["rect"][3]) / 2 for _, cell in aligned_cells],
        TABLE_ROW_Y_TOLERANCE,
    )
    row_assignments: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    for column_index, cell in aligned_cells:
        row_index = nearest_cluster_index(
            (cell["rect"][1] + cell["rect"][3]) / 2, row_centers
        )
        row_assignments.setdefault(row_index, []).append((column_index, cell))
    multi_column_rows = {
        row_index
        for row_index, members in row_assignments.items()
        if len({column_index for column_index, _ in members}) >= 2
    }
    aligned_rows = len(multi_column_rows)
    stats["rows"] = len(row_assignments)
    stats["aligned_rows"] = aligned_rows
    if aligned_rows < TABLE_MIN_ALIGNED_ROWS:
        stats["reject"] = "not_enough_aligned_rows"
        return 0.0, stats
    # Rowspan-aware alignment: in a real table at least one column (e.g. the
    # label column) has nearly all of its cells sitting on multi-column rows,
    # even when other columns add continuation rows. In multi-column prose no
    # column's paragraph tops line up with another column.
    column_row_hits: dict[int, int] = {}
    column_totals: dict[int, int] = {}
    for column_index, cell in aligned_cells:
        row_index = nearest_cluster_index(
            (cell["rect"][1] + cell["rect"][3]) / 2, row_centers
        )
        column_totals[column_index] = column_totals.get(column_index, 0) + 1
        if row_index in multi_column_rows:
            column_row_hits[column_index] = column_row_hits.get(column_index, 0) + 1
    anchor_alignment = max(
        column_row_hits.get(column_index, 0) / total
        for column_index, total in column_totals.items()
    )
    stats["anchor_alignment"] = round(anchor_alignment, 3)

    value_columns = sum(
        1
        for members in major_columns.values()
        if sum(1 for cell in members if is_value_cell(cell["text"])) >= 0.6 * len(members)
    )
    stats["value_columns"] = value_columns
    short_cell_ratio = sum(
        1 for cell in grid_cells if len(cell["text"]) <= TABLE_SHORT_CELL_CHARS
    ) / len(grid_cells)
    stats["short_cell_ratio"] = round(short_cell_ratio, 3)

    region_top = min(cell["rect"][1] for cell in grid_cells)
    region_bottom = max(cell["rect"][3] for cell in grid_cells)
    top_limit = region_top + 0.25 * max(region_bottom - region_top, 0.001)
    # Header labels like GOALS/TARGETS are plain short text cells; aligned
    # section headings across prose columns must NOT count as a header row.
    header_row = any(
        len({column_index for column_index, _ in members}) >= 2
        and row_centers[row_index] <= top_limit
        and all(
            len(cell["text"]) <= 40 and not cell.get("is_heading")
            for _, cell in members
        )
        for row_index, members in row_assignments.items()
    )
    stats["header_row"] = header_row

    score = 0.3
    if anchor_alignment >= 0.7:
        score += 0.2
    elif anchor_alignment >= 0.5:
        score += 0.1
    if value_columns >= 1:
        score += 0.2
    if short_cell_ratio >= 0.7:
        score += 0.1
    if header_row:
        score += 0.1
    if len(major_columns) >= 3 and short_cell_ratio >= 0.7:
        score += 0.1
    return round(min(score, 0.95), 3), stats


def add_table_candidate(
    candidates: list[TableCandidate],
    *,
    kind: str,
    bbox: list[float] | None,
    source_block_ids: list[str] | None = None,
    picture_index: int | None = None,
    confidence: float,
    reason: str,
    stats: dict[str, Any] | None = None,
) -> TableCandidate:
    candidate = TableCandidate(
        candidate_id=f"tc{len(candidates) + 1:03d}",
        kind=kind,
        bbox=bbox,
        source_block_ids=source_block_ids or [],
        picture_index=picture_index,
        confidence=round(confidence, 3),
        reason=reason,
        stats=stats,
    )
    candidates.append(candidate)
    return candidate


def picture_record_rect(record: PictureRecord, page_size: tuple[float, float]) -> list[float] | None:
    return bbox_to_normalized_rect(record.bbox, page_size)


def nearby_table_signal_blocks(
    rect: list[float] | None, blocks: list[dict[str, Any]], distance: float = 0.18
) -> list[dict[str, Any]]:
    if not rect:
        return []
    out: list[dict[str, Any]] = []
    for block in blocks:
        if not block.get("bbox") or block.get("type") == "picture":
            continue
        text = str(block.get("text") or block.get("caption") or "").strip()
        if not text:
            continue
        if rect_distance(rect, block["bbox"]) <= distance and (
            text_has_value_signal(text)
            or block.get("type") in {"title", "section_header", "caption"}
        ):
            out.append(block)
    return out


def picture_record_is_table_like(
    record: PictureRecord,
    rect: list[float] | None,
    nearby_blocks: list[dict[str, Any]],
) -> tuple[bool, str, float]:
    haystack = f"{record.classification} {record.caption}".lower()
    if record.area_ratio < pictures.PICTURE_TABLE_MIN_AREA_RATIO:
        return False, "picture_too_small", 0.0
    if any(label in haystack for label in pictures.DECORATIVE_LABELS):
        return False, "decorative_picture", 0.0
    aspect = rect_aspect_ratio(rect)
    has_semantic_label = any(label in haystack for label in pictures.SUMMARY_LABELS)
    has_nearby_signal = bool(nearby_blocks)
    wide_lower_region = bool(rect and aspect >= 1.8 and rect[1] >= 0.32)
    if not (wide_lower_region or has_semantic_label or has_nearby_signal):
        return False, "no_table_signal", 0.0
    confidence = 0.55
    reasons: list[str] = []
    if aspect >= 2.2:
        confidence += 0.15
        reasons.append("wide_region")
    if wide_lower_region:
        confidence += 0.1
        reasons.append("lower_wide_region")
    if has_semantic_label:
        confidence += 0.15
        reasons.append("semantic_label")
    if has_nearby_signal:
        confidence += 0.15
        reasons.append("nearby_structured_text")
    return True, ",".join(reasons) or "picture_table_signal", min(confidence, 0.95)


def regions_horizontally_adjacent(
    a_cells: list[dict[str, Any]], b_cells: list[dict[str, Any]]
) -> bool:
    a = rect_union([cell["rect"] for cell in a_cells])
    b = rect_union([cell["rect"] for cell in b_cells])
    if not a or not b:
        return False
    left, right = (a, b) if a[0] <= b[0] else (b, a)
    gap = right[0] - left[2]
    if gap > TABLE_MERGE_MAX_X_GAP or gap < -0.05:
        return False
    overlap = min(a[3], b[3]) - max(a[1], b[1])
    min_height = min(a[3] - a[1], b[3] - b[1])
    return min_height > 0 and overlap / min_height >= TABLE_MERGE_MIN_Y_OVERLAP


def merge_adjacent_regions(
    regions: list[list[dict[str, Any]]],
) -> list[tuple[float, dict[str, Any], list[dict[str, Any]]]]:
    """Re-join rejected regions that a wide gutter split apart, and re-score.

    A goals|targets layout with a wide empty band between the columns gets
    severed by the XY-cut into two single-column regions that can never score
    as tables individually; their union can. The scorer stays the gatekeeper,
    so joining two prose columns still fails on alignment/length.
    """
    ordered = sorted(
        regions,
        key=lambda region: (rect_union([cell["rect"] for cell in region]) or [1.0])[0],
    )
    merged: list[tuple[float, dict[str, Any], list[dict[str, Any]]]] = []
    used: set[int] = set()
    for i in range(len(ordered)):
        if i in used:
            continue
        for j in range(i + 1, len(ordered)):
            if j in used:
                continue
            if not regions_horizontally_adjacent(ordered[i], ordered[j]):
                continue
            union_region = ordered[i] + ordered[j]
            score, stats = score_table_region(union_region, allow_page_sized=True)
            # A merge is a rescue path, so demand stronger evidence than the
            # plain threshold: a true label column aligns (nearly) every cell
            # with a content row, and severed label/content tables carry a
            # header label row (GOALS/TARGETS style). Coincidentally aligned
            # prose columns fail both.
            if (
                score >= TABLE_SCORE_THRESHOLD
                and stats.get("anchor_alignment", 0.0) >= TABLE_MERGE_MIN_ANCHOR
                and stats.get("header_row")
            ):
                stats["merged_regions"] = 2
                merged.append((score, stats, union_region))
                used.update((i, j))
                break
    return merged


def evaluate_table_regions(
    cells: list[dict[str, Any]],
) -> tuple[
    list[tuple[float, dict[str, Any], list[dict[str, Any]], str]],
    list[tuple[float, dict[str, Any], list[dict[str, Any]]]],
]:
    """Segment the page and score every region; returns (accepted, rejected).

    Accepted entries are sorted by score and carry the detection reason;
    rejected ones are kept for debugging/replay.
    """
    accepted: list[tuple[float, dict[str, Any], list[dict[str, Any]], str]] = []
    rejected: list[tuple[float, dict[str, Any], list[dict[str, Any]]]] = []
    leftovers: list[list[dict[str, Any]]] = []
    for panel in segment_panels(cells):
        for region in split_panel_at_banners(panel):
            score, stats = score_table_region(region)
            if score >= TABLE_SCORE_THRESHOLD:
                accepted.append((score, stats, region, "table_shaped_region"))
            else:
                rejected.append((score, stats, region))
                leftovers.append(region)
    for score, stats, region in merge_adjacent_regions(leftovers):
        accepted.append((score, stats, region, "merged_split_columns"))
    accepted.sort(key=lambda entry: entry[0], reverse=True)
    return accepted, rejected


def _grid_header_row_count(structured: dict[str, Any]) -> int:
    """How many leading grid rows are the column header.

    Trusts Docling's ``header_rows`` when it flagged one; otherwise accepts row 0
    as a header when it reads like a column-label row (>= 2 short non-empty
    cells), which covers tables Docling left unmarked.
    """
    rows = structured.get("rows") or []
    header_rows = int(structured.get("header_rows") or 0)
    if header_rows >= 1:
        return min(header_rows, len(rows))
    if rows:
        first = [str(cell).strip() for cell in rows[0]]
        non_empty = [cell for cell in first if cell]
        if len(non_empty) >= 2 and all(
            len(cell) <= GRID_HEADER_CELL_MAX_CHARS for cell in non_empty
        ):
            return 1
    return 0


def _grid_spanning_label(row: list[str]) -> str | None:
    """A colspan super-header: a value repeated across >= 2 adjacent columns."""
    values = [str(cell).strip() for cell in row]
    for index in range(len(values) - 1):
        if values[index] and values[index + 1] == values[index]:
            return values[index]
    return None


def _merge_grid_header(
    rows: list[list[str]], header_rows: int, num_cols: int
) -> list[str]:
    """Collapse multi-row headers into one flat header row.

    Per column the deepest (last) non-empty header cell wins, so a stacked
    header ("Location in the value chain" over "Upstream") keeps the specific
    label. A spanning super-header left with an empty leading column is recovered
    into column 0 (the impact/policy-name column), matching the visible table.
    """
    header = [""] * num_cols
    header_block = rows[:header_rows]
    for row in header_block:
        for column in range(num_cols):
            value = str(row[column]).strip() if column < len(row) else ""
            if value:
                header[column] = value
    if header and not header[0]:
        for row in header_block:
            span = _grid_spanning_label(row)
            if span:
                header[0] = span
                break
    return header


def _grid_is_title_only(cells: list[str]) -> bool:
    return bool(cells) and bool(cells[0].strip()) and not any(c.strip() for c in cells[1:])


def _grid_is_detail(cells: list[str]) -> bool:
    return bool(cells) and bool(cells[0].strip()) and any(c.strip() for c in cells[1:])


def _merge_title_detail_cell(title: str, detail_md: str) -> str:
    """First cell of a merged title/detail record: ``title<br>detail``."""
    title = title.strip()
    if not detail_md:
        return title
    if not title:
        return detail_md
    return f"{title}<br>{detail_md}"


def _grid_content_column_count(records: list[dict[str, Any]], num_cols: int) -> int:
    """Columns carrying data in >= 2 records (used for the real-table gate)."""
    count = 0
    for column in range(num_cols):
        filled = sum(
            1
            for record in records
            if column < len(record["cells"]) and record["cells"][column].strip()
        )
        if filled >= 2:
            count += 1
    return count


def normalize_table_grid(structured: dict[str, Any] | None) -> dict[str, Any] | None:
    """Classify a Docling grid and produce normalized logical records.

    Returns ``None`` for anything that is not a real ``regular_table`` or
    ``title_detail_table`` (leave it to the existing flow). A
    ``title_detail_table`` is a header plus repeated pairs where a
    first-column-only title row is immediately followed by a populated detail
    row; each pair merges into one record whose first cell is ``title<br>detail``
    (bullets preserved). Every remaining row stays one record. A real table needs
    a stable header, >= 2 logical records, and >= 2 content columns.
    """
    if not structured:
        return None
    rows = [[str(cell).strip() for cell in row] for row in (structured.get("rows") or [])]
    if not rows:
        return None
    num_cols = int(structured.get("num_cols") or max((len(row) for row in rows), default=0))
    if num_cols < GRID_MIN_CONTENT_COLUMNS + 1:
        return None
    header_rows = _grid_header_row_count(structured)
    if header_rows < 1:
        return None
    header = _merge_grid_header(rows, header_rows, num_cols)
    body = [row + [""] * (num_cols - len(row)) for row in rows[header_rows:]]
    if not body:
        return None

    records: list[dict[str, Any]] = []
    pair_merges = 0
    bulleted = False
    index = 0
    while index < len(body):
        current = body[index]
        following = body[index + 1] if index + 1 < len(body) else None
        if _grid_is_title_only(current) and following is not None and _grid_is_detail(following):
            detail = following[0]
            detail_md = _cell_with_bullets(detail)
            bulleted |= _became_bullet_list(detail, detail_md)
            rest: list[str] = []
            for cell in following[1:]:
                transformed = _cell_with_bullets(cell)
                bulleted |= _became_bullet_list(cell, transformed)
                rest.append(transformed)
            merged = [_merge_title_detail_cell(current[0], detail_md)] + rest
            records.append(
                {"cells": merged, "source_rows": [header_rows + index, header_rows + index + 1]}
            )
            pair_merges += 1
            index += 2
        else:
            cells: list[str] = []
            for cell in current:
                transformed = _cell_with_bullets(cell)
                bulleted |= _became_bullet_list(cell, transformed)
                cells.append(transformed)
            records.append(
                {"cells": cells, "source_rows": [header_rows + index]}
            )
            index += 1

    if len(records) < GRID_MIN_RECORDS:
        return None
    if _grid_content_column_count(records, num_cols) < GRID_MIN_CONTENT_COLUMNS:
        return None
    classification = (
        "title_detail_table" if pair_merges >= GRID_TITLE_DETAIL_MIN_PAIRS else "regular_table"
    )
    return {
        "classification": classification,
        "header": header,
        "records": records,
        "num_cols": num_cols,
        "header_rows": header_rows,
        "bulleted": bulleted,
    }


def _grid_coverage_column(
    header: list[str], records: list[dict[str, Any]], num_cols: int
) -> int | None:
    """The column table symbols belong in: a ``coverage`` header, else a trailing
    column empty across every record."""
    for column, label in enumerate(header):
        if "coverage" in label.lower():
            return column
    last = num_cols - 1
    if last >= 1 and all(
        last >= len(record["cells"]) or not record["cells"][last].strip()
        for record in records
    ):
        return last
    return None


def _rect_key(rect: list[float]) -> tuple[float, float, float, float]:
    """A stable full-rectangle key for Docling grid cells.

    Matching only vertical bounds would incorrectly merge ordinary cells that
    happen to share one visual row.  Rounded normalized coordinates are already
    the stable representation written to checkpoints.
    """
    return tuple(round(float(value), 6) for value in rect)  # type: ignore[return-value]


def _record_y_band(
    record_index: int,
    record: dict[str, Any],
    cell_rects_by_row: dict[int, list[list[float]]],
    rect_owner_records: dict[tuple[float, float, float, float], set[int]],
) -> tuple[float, float] | None:
    """Build a row band only from rectangles local to one logical record.

    Docling can repeat a merged cell's full bbox on every source row it spans.
    Such a rectangle belongs to several normalized records and must not expand
    any of their bands; a repeated title/detail bbox remains valid because both
    source rows normalize into the same record.
    """
    tops: list[float] = []
    bottoms: list[float] = []
    for row_index in record["source_rows"]:
        for rect in cell_rects_by_row.get(row_index, []):
            if rect_owner_records.get(_rect_key(rect)) != {record_index}:
                continue
            tops.append(rect[1])
            bottoms.append(rect[3])
    if not tops:
        return None
    return (min(tops), max(bottoms))


def _order_symbols_reading(symbols: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort symbols sharing one cell into reading order: top-to-bottom by row
    (rows separated by half a badge height), then left-to-right within a row.

    Rects are normalized top-origin ``[l, t, r, b]``. Geometry-less symbols (which
    the caller has already routed to ``unplaced``, so unusual here) keep their
    arrival order after the placed ones.
    """
    with_rect = [symbol for symbol in symbols if symbol.get("rect")]
    without_rect = [symbol for symbol in symbols if not symbol.get("rect")]
    if not with_rect:
        return symbols
    heights = sorted(symbol["rect"][3] - symbol["rect"][1] for symbol in with_rect)
    tolerance = heights[len(heights) // 2] / 2
    rows: list[list[dict[str, Any]]] = []
    for symbol in sorted(with_rect, key=lambda s: s["rect"][1]):
        if rows and symbol["rect"][1] - rows[-1][0]["rect"][1] <= tolerance:
            rows[-1].append(symbol)
        else:
            rows.append([symbol])
    ordered: list[dict[str, Any]] = []
    for row in rows:
        ordered.extend(sorted(row, key=lambda s: s["rect"][0]))
    return ordered + without_rect


def place_grid_symbols(
    normalized: dict[str, Any],
    symbols: list[dict[str, Any]],
    cell_rects_by_row: dict[int, list[list[float]]],
) -> tuple[list[int], dict[int, int]]:
    """Inject each symbol's value into the coverage cell of the row that vertically
    contains it. Returns the picture indices left unplaced and each successful
    ``picture_index -> logical_record_index`` assignment.

    A symbol is placed only when its vertical center falls inside exactly one
    record's row band; ambiguous or geometry-less symbols are returned unplaced
    (never assigned by sequence), per the escalation rule. Several symbols sharing
    one cell are ordered by visual reading order and joined with ``GRID_SYMBOL_JOIN``
    (``E1, E2, S3``), de-duplicating repeated values.
    """
    records = normalized["records"]
    target = _grid_coverage_column(normalized["header"], records, normalized["num_cols"])
    source_row_records: dict[int, set[int]] = {}
    for record_index, record in enumerate(records):
        for row_index in record["source_rows"]:
            source_row_records.setdefault(row_index, set()).add(record_index)
    rect_owner_records: dict[tuple[float, float, float, float], set[int]] = {}
    for row_index, rects in cell_rects_by_row.items():
        for rect in rects:
            rect_owner_records.setdefault(_rect_key(rect), set()).update(
                source_row_records.get(row_index, set())
            )
    bands = [
        (
            _record_y_band(
                record_index, record, cell_rects_by_row, rect_owner_records
            ),
            record_index,
        )
        for record_index, record in enumerate(records)
    ]
    unplaced: list[int] = []
    assignments: dict[int, int] = {}
    # Collect matched symbols per record first, so several symbols sharing one
    # cell can be visually ordered and comma-joined rather than appended in
    # arrival order with a single space.
    matched_by_record: dict[int, list[dict[str, Any]]] = {}
    for symbol in symbols:
        rect = symbol.get("rect")
        value = str(symbol.get("value", "")).strip()
        if target is None or not rect or not value:
            unplaced.append(symbol["index"])
            continue
        center = (rect[1] + rect[3]) / 2
        matches = [
            record_index
            for band, record_index in bands
            if band
            and band[0] - GRID_SYMBOL_ROW_TOLERANCE <= center <= band[1] + GRID_SYMBOL_ROW_TOLERANCE
        ]
        if len(matches) != 1:
            unplaced.append(symbol["index"])
            continue
        assignments[symbol["index"]] = matches[0]
        matched_by_record.setdefault(matches[0], []).append(symbol)
    for record_index, matched in matched_by_record.items():
        cells = records[record_index]["cells"]
        while len(cells) <= target:
            cells.append("")
        existing = cells[target].strip()
        values: list[str] = []
        seen: set[str] = set()
        for value in ([existing] if existing else []) + [
            str(symbol.get("value", "")).strip() for symbol in _order_symbols_reading(matched)
        ]:
            if value and value not in seen:
                seen.add(value)
                values.append(value)
        cells[target] = GRID_SYMBOL_JOIN.join(values)
    return sorted(unplaced), dict(sorted(assignments.items()))


def _symbol_cell_assignments(
    assignments: dict[int, int], column_index: int | None
) -> dict[int, dict[str, int]]:
    """Persist the exact legacy output cell for each geometry placement."""
    if column_index is None:
        return {}
    return {
        picture_index: {"record_index": record_index, "column_index": column_index}
        for picture_index, record_index in assignments.items()
    }


def place_visual_value(
    normalized: dict[str, Any], record_index: int, column_index: int, value: str
) -> bool:
    """Place one opaque scalar in an explicitly targeted normalized-grid cell.

    A matching value is already complete and succeeds without changing the
    grid. A different nonempty value is left untouched: generic code must not
    invent a delimiter or overwrite native text.
    """
    records = normalized.get("records")
    try:
        num_cols = int(normalized.get("num_cols", 0))
    except (TypeError, ValueError):
        return False
    if (
        not isinstance(records, list)
        or not isinstance(value, str)
        or not value
        or record_index < 0
        or record_index >= len(records)
        or column_index < 0
        or column_index >= num_cols
    ):
        return False
    record = records[record_index]
    if not isinstance(record, dict) or not isinstance(record.get("cells"), list):
        return False
    cells = record["cells"]
    if len(cells) <= column_index:
        cells.extend([""] * (column_index + 1 - len(cells)))
    existing = cells[column_index]
    if existing == value:
        return True
    if existing != "":
        return False
    cells[column_index] = value
    return True


def render_grid_markdown(normalized: dict[str, Any]) -> str:
    """Render a normalized grid as one markdown pipe table (cells escaped)."""
    num_cols = normalized["num_cols"]

    def _row(cells: list[str]) -> str:
        padded = [str(cell) for cell in cells] + [""] * (num_cols - len(cells))
        return "| " + " | ".join(cell.replace("|", "\\|") for cell in padded[:num_cols]) + " |"

    lines = [_row(normalized["header"]), "|" + "---|" * num_cols]
    lines.extend(_row(record["cells"]) for record in normalized["records"])
    return "\n".join(lines) + "\n"


def build_table_candidates(
    *,
    cells: list[dict[str, Any]],
    page_size: tuple[float, float],
    picture_records: dict[int, PictureRecord],
    layout_map: dict[str, Any],
) -> list[TableCandidate]:
    candidates: list[TableCandidate] = []
    blocks = layout_map.get("blocks", [])

    for block in blocks:
        if block.get("type") != "table":
            continue
        candidate_stats: dict[str, Any] = {}
        # Retain the full structured Docling grid on every table candidate (not
        # just KPI ones): deterministic rendering, source-complete verification,
        # and symbol placement all read from it.
        grid = block.get("_table_grid")
        if grid:
            candidate_stats["grid"] = grid
        if block.get("headerless") and block.get("first_row"):
            candidate_stats.update({
                "headerless": True,
                "first_row": list(block["first_row"]),
            })
        sectioned = block.get("_sectioned_table") or {}
        if sectioned.get("markdown"):
            # A section-banded table (page 154 / page 43): pre-split into labeled
            # subtables deterministically. This wins over the KPI check — a
            # >=3-col, multi-row section table is never a KPI panel — and needs
            # no VLM call, so it stays authoritative even under --skip-vlm.
            candidate_stats.update(
                {
                    "format": "sectioned_table",
                    "section_titles": list(sectioned.get("section_titles", [])),
                    "section_qualifiers": list(sectioned.get("section_qualifiers", [])),
                    "section_kinds": list(sectioned.get("section_kinds", [])),
                    "sectioned_source_rows": sectioned.get("source_row_count"),
                    "sectioned_split": sectioned.get("split"),
                }
            )
        else:
            grid_rows = block.get("grid_rows") or []
            if grid_rows:
                is_kpi, kpi_stats = sp.looks_like_kpi_panel(grid_rows)
                if is_kpi:
                    candidate_stats.update(
                        {
                            "kpi_panel": True,
                            "kpi_stats": kpi_stats,
                            "grid_rows": grid_rows,
                        }
                    )
        candidate = add_table_candidate(
            candidates,
            kind="docling_table",
            bbox=block.get("bbox"),
            source_block_ids=[block["id"]],
            confidence=0.95,
            reason="docling_table_item",
            stats=candidate_stats or None,
        )
        if sectioned.get("markdown"):
            candidate.markdown = sectioned["markdown"]
            candidate.verified = True

    scored_regions, _ = evaluate_table_regions(cells)
    for score, stats, region, reason in scored_regions[:TABLE_REGIONS_MAX_PER_PAGE]:
        bbox = rect_union([cell["rect"] for cell in region])
        if any(
            candidate.kind == "docling_table"
            and rect_overlap_ratio(candidate.bbox, bbox) > 0.6
            for candidate in candidates
        ):
            continue
        candidate_stats = dict(stats)
        grid_rows = region_grid_rows(region)
        is_kpi, kpi_stats = sp.looks_like_kpi_panel(grid_rows)
        if is_kpi:
            candidate_stats.update(
                {
                    "kpi_panel": True,
                    "kpi_stats": kpi_stats,
                    "grid_rows": grid_rows,
                }
            )
        add_table_candidate(
            candidates,
            kind="layout_region",
            bbox=bbox,
            source_block_ids=[cell["id"] for cell in region],
            confidence=score,
            reason=reason,
            stats=candidate_stats,
        )

    for record in picture_records.values():
        rect = picture_record_rect(record, page_size)
        nearby_blocks = nearby_table_signal_blocks(rect, blocks)
        is_table_like, reason, confidence = picture_record_is_table_like(record, rect, nearby_blocks)
        if not is_table_like:
            continue
        source_block_ids = [block["id"] for block in nearby_blocks[:12]]
        matched_picture_block = next(
            (
                block
                for block in blocks
                if block.get("type") == "picture" and rect_overlap_ratio(block.get("bbox"), rect) > 0.8
            ),
            None,
        )
        if matched_picture_block:
            source_block_ids.insert(0, matched_picture_block["id"])
        add_table_candidate(
            candidates,
            kind="picture_table",
            bbox=rect,
            source_block_ids=source_block_ids,
            picture_index=record.index,
            confidence=confidence,
            reason=reason,
        )

    for candidate in candidates:
        symbols = [
            record
            for record in picture_records.values()
            if record.summary_type == "symbol"
            and record.summary.strip()
            and rect_overlap_ratio(
                picture_record_rect(record, page_size), candidate.bbox
            ) >= pictures.PICTURE_EMBED_OVERLAP_MIN
        ]
        if symbols:
            candidate.stats = {
                **(candidate.stats or {}),
                "symbol_picture_indices": [record.index for record in symbols],
            }

    # Layout-region detection happens after picture extraction. Upgrade any
    # tiny picture found inside an accepted region in time for table_extract.
    for candidate in candidates:
        if candidate.kind != "layout_region":
            continue
        for record in picture_records.values():
            if record.area_ratio >= pictures.PICTURE_MIN_AREA_RATIO:
                continue
            rect = picture_record_rect(record, page_size)
            overlap = rect_overlap_ratio(rect, candidate.bbox)
            if overlap < pictures.PICTURE_EMBED_OVERLAP_MIN:
                continue
            record.embedded_in = "table"
            record.embed_overlap_ratio = max(record.embed_overlap_ratio, round(overlap, 3))
            record.summarize = True
            record.triage_eligible = True
            record.triage_type = "symbol"
            record.triage_action = "symbol"
            record.skip_reason = ""
            candidate.stats = {
                **(candidate.stats or {}),
                "symbol_picture_indices": sorted(
                    set((candidate.stats or {}).get("symbol_picture_indices", []))
                    | {record.index}
                ),
            }

    return candidates


def render_deterministic_docling_table(
    candidate: TableCandidate,
    records_by_index: dict[int, PictureRecord],
    page_size: tuple[float, float],
) -> bool:
    """Render a structurally valid Docling table straight from its grid, injecting
    in-cell symbols by row geometry. Returns True when the candidate was rendered.

    Runs at transcription time (not detection) so the table's coverage symbols
    are already summarized. Scoped to the title/detail signature, to regular
    tables that carry in-cell symbols (the ESRS impact/policy tables), and to
    regular tables whose normalization produced genuine ``- item<br>- item``
    bullet cells, leaving existing regular/headerless/KPI/sectioned handling
    untouched.
    """
    stats = candidate.stats or {}
    if candidate.kind != "docling_table" or candidate.verified:
        return False
    existing_format = stats.get("format")
    resumable_deterministic = (
        existing_format in {"regular_table", "title_detail_table"}
        and stats.get("deterministic")
    )
    if (
        stats.get("headerless")
        or stats.get("kpi_panel")
        or (existing_format and not resumable_deterministic)
    ):
        return False
    normalized = normalize_table_grid(stats.get("grid"))
    if normalized is None:
        return False
    symbols = [
        {
            "index": record.index,
            "value": record.summary.strip(),
            "rect": picture_record_rect(record, page_size),
        }
        for record in records_by_index.values()
        if record.summary_type == "symbol"
        and record.summary.strip()
        and rect_overlap_ratio(
            picture_record_rect(record, page_size), candidate.bbox
        ) >= pictures.PICTURE_EMBED_OVERLAP_MIN
    ]
    if (
        normalized["classification"] == "regular_table"
        and not symbols
        and not normalized.get("bulleted")
        and not stats.get("visual_values_inserted")
    ):
        return False
    cell_rects_by_row: dict[int, list[list[float]]] = {}
    for cell in (stats.get("grid") or {}).get("cells", []):
        normalized_rect = (
            bbox_to_normalized_rect(cell.get("bbox"), page_size)
            if cell.get("bbox")
            else None
        )
        if normalized_rect:
            cell_rects_by_row.setdefault(cell["r"], []).append(normalized_rect)
    symbol_column = _grid_coverage_column(
        normalized["header"], normalized["records"], normalized["num_cols"]
    )
    unplaced, assignments = place_grid_symbols(normalized, symbols, cell_rects_by_row)
    cell_assignments = _symbol_cell_assignments(assignments, symbol_column)
    candidate.markdown = render_grid_markdown(normalized)
    candidate.verified = True
    candidate.stats = {
        **stats,
        "format": normalized["classification"],
        "deterministic": True,
        "symbol_picture_indices": sorted(
            set(stats.get("symbol_picture_indices", []))
            | {symbol["index"] for symbol in symbols}
        ),
        "symbols_placed_geometry": sorted(assignments),
        "symbol_row_assignments": assignments,
        "symbol_cell_assignments": cell_assignments,
    }
    candidate.stats.pop("symbols_unplaced_geometry", None)
    if unplaced:
        candidate.stats["symbols_unplaced_geometry"] = unplaced
    return True


def render_sectioned_docling_table(
    candidate: TableCandidate,
    records_by_index: dict[int, PictureRecord],
    page_size: tuple[float, float],
) -> bool:
    """Place summarized symbols into a verified sectioned Docling table.

    Sectioned candidates are already rendered at layout-map time. Their stored
    split keeps each body row's source-grid index, allowing the existing geometry
    placement routine to fill coverage cells after picture summaries arrive.
    Older checkpoints without that additive mapping remain untouched.
    """
    stats = candidate.stats or {}
    if (
        candidate.kind != "docling_table"
        or not candidate.verified
        or stats.get("format") != "sectioned_table"
    ):
        return False
    split = stats.get("sectioned_split")
    grid = stats.get("grid")
    if not isinstance(split, dict) or not isinstance(grid, dict):
        return False
    header = split.get("header")
    sections = split.get("sections")
    if not isinstance(header, list) or not isinstance(sections, list):
        return False
    try:
        num_cols = int(split.get("n_cols"))
    except (TypeError, ValueError):
        return False
    symbols = [
        {
            "index": record.index,
            "value": record.summary.strip(),
            "rect": picture_record_rect(record, page_size),
        }
        for record in records_by_index.values()
        if record.summary_type == "symbol"
        and record.summary.strip()
        and rect_overlap_ratio(
            picture_record_rect(record, page_size), candidate.bbox
        ) >= pictures.PICTURE_EMBED_OVERLAP_MIN
    ]
    if not symbols:
        return False
    section_records: list[dict[str, Any]] = []
    for section in sections:
        if not isinstance(section, dict):
            return False
        rows = section.get("rows")
        source_rows = section.get("source_rows")
        if not isinstance(rows, list) or not isinstance(source_rows, list):
            return False
        if len(rows) != len(source_rows):
            return False
        for row, source_row in zip(rows, source_rows):
            if not isinstance(row, list):
                return False
            try:
                row_index = int(source_row)
            except (TypeError, ValueError):
                return False
            section_records.append({"cells": row, "source_rows": [row_index]})
    if not section_records:
        return False
    cell_rects_by_row: dict[int, list[list[float]]] = {}
    for cell in grid.get("cells", []):
        if not isinstance(cell, dict) or not cell.get("bbox"):
            continue
        try:
            row_index = int(cell["r"])
        except (KeyError, TypeError, ValueError):
            continue
        normalized_rect = bbox_to_normalized_rect(cell["bbox"], page_size)
        if normalized_rect:
            cell_rects_by_row.setdefault(row_index, []).append(normalized_rect)
    normalized = {
        "header": [str(cell) for cell in header],
        "records": section_records,
        "num_cols": num_cols,
    }
    symbol_column = _grid_coverage_column(
        normalized["header"], normalized["records"], normalized["num_cols"]
    )
    unplaced, assignments = place_grid_symbols(normalized, symbols, cell_rects_by_row)
    cell_assignments = _symbol_cell_assignments(assignments, symbol_column)
    candidate.markdown = sp.render_sectioned_tables(split)
    candidate.stats = {
        **stats,
        "deterministic": True,
        "symbol_picture_indices": sorted(
            set(stats.get("symbol_picture_indices", []))
            | {symbol["index"] for symbol in symbols}
        ),
        "symbols_placed_geometry": sorted(assignments),
        "symbol_row_assignments": assignments,
        "symbol_cell_assignments": cell_assignments,
    }
    candidate.stats.pop("symbols_unplaced_geometry", None)
    if unplaced:
        candidate.stats["symbols_unplaced_geometry"] = unplaced
    return True


def verified_tables_prompt_block(candidates: list[TableCandidate]) -> str:
    """Only verified region tables reach a prompt; everything else is debug-only."""
    parts: list[str] = []
    for candidate in candidates:
        if not (
            candidate.verified
            and candidate.markdown.strip()
            and candidate.has_complete_symbol_geometry()
        ):
            continue
        candidate_format = (candidate.stats or {}).get("format")
        if candidate_format == "kpi_list":
            parts.append(
                "KPI panel at bbox "
                f"{candidate.bbox} (label/value list — authoritative; keep as list lines, "
                f"do NOT render as a table):\n{candidate.markdown.strip()}"
            )
        elif candidate_format == "sectioned_table":
            parts.append(
                f"Table at bbox {candidate.bbox}, pre-split into `###`-titled subtables "
                "that repeat the same column headers (authoritative; place ALL subtables "
                "verbatim and in order, as markdown tables — do NOT merge them back into "
                "one table, convert them to lists, drop the repeated header rows, or turn "
                f"a section title into anything other than its `###` heading):\n{candidate.markdown.strip()}"
            )
        elif candidate_format == "title_detail_table":
            parts.append(
                f"Table at bbox {candidate.bbox} (authoritative; place this exact table "
                "verbatim at its position). Each row's first cell already joins the bold "
                "title and its detail text with `<br>`: keep that single cell as one cell — "
                "do NOT split the title and detail into separate rows, do NOT turn a title "
                "into a `### heading`, a section banner, a repeated header row, or a separate "
                "field/list block. Keep the one header row exactly once and never repeat "
                f"header labels in body rows:\n{candidate.markdown.strip()}"
            )
        elif candidate_format == "regular_table":
            parts.append(
                f"Table at bbox {candidate.bbox} (authoritative; place this exact table "
                "verbatim at its position, as a single markdown table). Keep the one header "
                "row exactly once, never repeat header labels in body rows, and do NOT "
                "convert any row into a `### heading`, field block, or list:\n"
                f"{candidate.markdown.strip()}"
            )
        else:
            parts.append(
                f"Table for region at bbox {candidate.bbox}:\n{candidate.markdown.strip()}"
            )
    return "\n\n".join(parts) if parts else "(none)"


def numeric_tokens(text: str) -> set[str]:
    return {token.replace(",", ".") for token in NUMERIC_TOKEN_RE.findall(text or "")}


def word_tokens(text: str) -> set[str]:
    return {token.lower() for token in WORD_TOKEN_RE.findall(text or "")}


def _grid_expected_shape(grid: dict[str, Any]) -> tuple[int, list[str], int, int]:
    """(num_cols, flat header labels, min logical records, raw body rows) for a grid.

    ``min logical records`` is the count after title/detail merging (0 when the
    grid does not classify as a real table); ``raw body rows`` is the unmerged
    count. A faithful transcription lands between the two.
    """
    rows = [[str(cell).strip() for cell in row] for row in (grid.get("rows") or [])]
    num_cols = int(grid.get("num_cols") or max((len(row) for row in rows), default=0))
    header_rows = _grid_header_row_count(grid)
    header = _merge_grid_header(rows, header_rows, num_cols) if header_rows else []
    header_labels = [collapse_ws(cell).lower() for cell in header if cell.strip()]
    normalized = normalize_table_grid(grid)
    min_records = len(normalized["records"]) if normalized else 0
    raw_body = max(len(rows) - header_rows, 0)
    return num_cols, header_labels, min_records, raw_body


def verify_region_table(
    markdown: str, cell_texts: list[str], grid: dict[str, Any] | None = None
) -> tuple[bool, dict[str, Any]]:
    """Deterministic acceptance check for a VLM-transcribed region table.

    The table must be structurally a pipe table, must not contain numbers
    absent from the source cells, and must cover (nearly) all source numbers —
    or, for numberless tables, most of the source words.

    When a Docling ``grid`` is supplied (only for docling-table crops that fell
    through to the VLM after the deterministic renderer declined), it must also
    match the source shape: a single header block, the source column count, no
    header labels repeated in a body row, and a body-row count between the merged
    and raw record counts. Region/KPI crops pass ``grid=None`` and are unaffected.
    """
    stats: dict[str, Any] = {}
    rows = [
        line.strip()
        for line in markdown.splitlines()
        if line.strip().startswith("|") and not set(line.strip()) <= set("|-: ")
    ]
    stats["table_rows"] = len(rows)
    if len(rows) < 2:
        stats["fail"] = "no_table_structure"
        return False, stats

    if grid:
        row_cells = [
            [collapse_ws(cell) for cell in row.strip("|").split("|")] for row in rows
        ]
        num_cols, header_labels, min_records, raw_body = _grid_expected_shape(grid)
        table_cols = max((len(cells) for cells in row_cells), default=0)
        stats["source_columns"] = num_cols
        stats["table_columns"] = table_cols
        separator_blocks = sum(
            1
            for line in markdown.splitlines()
            if line.strip().startswith("|")
            and set(line.strip()) <= set("|-: ")
            and any(ch in "-:" for ch in line)
        )
        if separator_blocks > 1:
            stats["fail"] = "multiple_header_blocks"
            return False, stats
        if num_cols and table_cols != num_cols:
            stats["fail"] = "column_count_mismatch"
            return False, stats
        if header_labels:
            for cells in row_cells[1:]:
                present = {cell.lower() for cell in cells if cell}
                if present and set(header_labels) <= present:
                    stats["fail"] = "repeated_header_in_body"
                    return False, stats
        body_rows = max(len(row_cells) - 1, 0)
        stats["logical_records"] = body_rows
        if min_records and not (min_records <= body_rows <= max(raw_body, min_records)):
            stats["fail"] = "record_count_mismatch"
            return False, stats

    source_numbers = set().union(*(numeric_tokens(text) for text in cell_texts)) if cell_texts else set()
    table_numbers = numeric_tokens(markdown)
    invented = sorted(table_numbers - source_numbers)
    stats["invented_numbers"] = invented
    if invented:
        stats["fail"] = "invented_numbers"
        return False, stats

    if source_numbers:
        missing = sorted(source_numbers - table_numbers)
        coverage = 1 - len(missing) / len(source_numbers)
        stats["numeric_coverage"] = round(coverage, 3)
        stats["missing_numbers"] = missing
        if coverage < TABLE_NUMERIC_COVERAGE_MIN:
            stats["fail"] = "missing_numbers"
            return False, stats
        return True, stats

    source_words = set().union(*(word_tokens(text) for text in cell_texts)) if cell_texts else set()
    if source_words:
        table_words = word_tokens(markdown)
        coverage = len(source_words & table_words) / len(source_words)
        stats["word_coverage"] = round(coverage, 3)
        if coverage < TABLE_WORD_COVERAGE_MIN:
            stats["fail"] = "missing_words"
            return False, stats
    return True, stats


def verify_kpi_list(markdown: str, cell_texts: list[str]) -> tuple[bool, dict[str, Any]]:
    """Accept a faithful KPI label/value list, never a pipe table."""
    stats: dict[str, Any] = {}
    lines = markdown.splitlines()
    if any(line.lstrip().startswith("|") for line in lines):
        stats["fail"] = "pipe_table"
        return False, stats
    list_lines = sum(
        bool(re.match(r"^\s*-\s+[^:|]{2,80}:\s*\S", line))
        for line in lines
    )
    stats["list_lines"] = list_lines
    if list_lines < 2:
        stats["fail"] = "no_kpi_list_structure"
        return False, stats

    source_numbers = set().union(*(numeric_tokens(text) for text in cell_texts)) if cell_texts else set()
    list_numbers = numeric_tokens(markdown)
    invented = sorted(list_numbers - source_numbers)
    stats["invented_numbers"] = invented
    if invented:
        stats["fail"] = "invented_numbers"
        return False, stats
    if source_numbers:
        missing = sorted(source_numbers - list_numbers)
        coverage = 1 - len(missing) / len(source_numbers)
        stats["numeric_coverage"] = round(coverage, 3)
        stats["missing_numbers"] = missing
        if coverage < TABLE_NUMERIC_COVERAGE_MIN:
            stats["fail"] = "missing_numbers"
            return False, stats
    return True, stats


def table_candidate_rows(candidates: list[TableCandidate]) -> list[dict[str, Any]]:
    return [asdict(candidate) for candidate in candidates]


def render_layout_overlay(
    *,
    page_image_path: Path,
    items: list[Any],
    page_size: tuple[float, float],
    output_path: Path,
) -> None:
    from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415

    color_map = {
        "table": "#ff5a36",
        "picture": "#2d8cff",
        "title": "#00a86b",
        "section_header": "#00a86b",
        "list_item": "#a855f7",
        "paragraph": "#f0b100",
    }

    image = Image.open(page_image_path).convert("RGB")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    for item in items:
        bbox = bbox_dict(item)
        rect = bbox_to_pixel_rect(bbox, page_size=page_size, image_size=image.size)
        if not rect:
            continue
        kind = item_kind(item)
        color = color_map.get(kind, "#ffffff")
        draw.rectangle(rect, outline=color, width=3)
        label = kind[:24]
        text_box = draw.textbbox((0, 0), label, font=font)
        text_w = text_box[2] - text_box[0]
        text_h = text_box[3] - text_box[1]
        x0, y0, _, _ = rect
        bg_rect = (x0, max(0, y0 - text_h - 4), x0 + text_w + 6, y0)
        draw.rectangle(bg_rect, fill=color)
        draw.text((x0 + 3, bg_rect[1] + 1), label, fill="black", font=font)

    image.save(output_path)


def render_table_candidates_overlay(
    *,
    page_image_path: Path,
    candidates: list[TableCandidate],
    output_path: Path,
) -> None:
    from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415

    image = Image.open(page_image_path).convert("RGB")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    color_map = {
        "docling_table": "#ff5a36",
        "layout_region": "#00d084",
        "picture_table": "#36a3ff",
    }
    for candidate in candidates:
        rect = normalized_rect_to_pixel_rect(candidate.bbox, image.size)
        if not rect:
            continue
        color = color_map.get(candidate.kind, "#ffffff")
        draw.rectangle(rect, outline=color, width=4)
        label = f"{candidate.candidate_id}:{candidate.kind}"
        text_box = draw.textbbox((0, 0), label, font=font)
        text_w = text_box[2] - text_box[0]
        text_h = text_box[3] - text_box[1]
        x0, y0, _, _ = rect
        bg_rect = (x0, max(0, y0 - text_h - 5), x0 + text_w + 6, y0)
        draw.rectangle(bg_rect, fill=color)
        draw.text((x0 + 3, bg_rect[1] + 1), label, fill="black", font=font)
    image.save(output_path)


def region_blocks_prompt_json(cells: list[dict[str, Any]]) -> str:
    payload = [
        {
            "id": cell["id"],
            "bbox": cell["rect"],
            "text": truncate_prompt_text(cell["text"], 300),
        }
        for cell in cells
    ]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def transcribe_table_candidates(
    *,
    candidates: list[TableCandidate],
    cells: list[dict[str, Any]],
    page_image_path: Path,
    page_dir: Path,
    args: argparse.Namespace,
    picture_records: list[PictureRecord] | dict[int, PictureRecord] | None = None,
    page_size: tuple[float, float] | None = None,
) -> None:
    """Crop each accepted layout region, transcribe it with the VLM using the
    region's cell texts as authoritative content, and verify deterministically.

    Failed or skipped transcriptions leave the candidate unverified; the page
    then flows through the pipeline exactly as it would without a candidate.
    """
    cells_by_id = {cell["id"]: cell for cell in cells}
    if isinstance(picture_records, dict):
        records_by_index = picture_records
    else:
        records_by_index = {
            record.index: record for record in (picture_records or [])
        }
    table_dir = page_dir / "table_candidates"
    if table_dir.exists():
        shutil.rmtree(table_dir)
    DETERMINISTIC_FORMATS = {"sectioned_table", "regular_table", "title_detail_table"}
    for candidate in candidates:
        # Sectioned candidates are verified at layout-map time. Once picture
        # summaries are available, inject their geometry-matched values before
        # the verified-candidate fast path skips further transcription.
        render_sectioned_docling_table(
            candidate, records_by_index, page_size or (1.0, 1.0)
        )
        # Render structurally valid title/detail and symbol-bearing regular Docling
        # tables straight from the grid now that coverage symbols are summarized.
        render_deterministic_docling_table(
            candidate, records_by_index, page_size or (1.0, 1.0)
        )
        # A docling table already transcribed and verified deterministically at
        # build time (sectioned split) or just now (grid render) is authoritative;
        # never let a VLM pass overwrite it (a symbol picture overlapping it would
        # otherwise reach the transcription below). Symbols the geometry pass could
        # not place surface via the table_symbols_unplaced warning instead.
        if (
            candidate.verified
            and candidate.has_complete_symbol_geometry()
            and (candidate.stats or {}).get("format") in DETERMINISTIC_FORMATS
        ):
            continue
        symbol_indices = (candidate.stats or {}).get("symbol_picture_indices", [])
        is_kpi = bool((candidate.stats or {}).get("kpi_panel"))
        if candidate.kind != "layout_region" and not symbol_indices and not is_kpi:
            continue
        if candidate.kind == "layout_region":
            region_cells = [
                cells_by_id[block_id]
                for block_id in candidate.source_block_ids
                if block_id in cells_by_id
            ]
        else:
            region_cells = [
                cell
                for cell in cells
                if rect_overlap_ratio(cell.get("rect"), candidate.bbox) > 0.1
            ]
        # Feed the verifier the COMPLETE Docling grid (full structured text, not
        # the truncated grid_rows) so it cannot reject a valid table for numbers
        # that live in cells the overlapping non-table cells omitted.
        grid_stats = (candidate.stats or {}).get("grid") or {}
        grid_rows = grid_stats.get("rows") or (candidate.stats or {}).get("grid_rows", [])
        if candidate.kind == "docling_table" and grid_rows:
            for row_index, row in enumerate(grid_rows):
                for column_index, text in enumerate(row):
                    if str(text).strip():
                        region_cells.append(
                            {
                                "id": f"tbl{row_index}c{column_index}",
                                "rect": candidate.bbox,
                                "text": str(text).strip(),
                            }
                        )
        pseudo_cells: list[dict[str, Any]] = []
        for index in symbol_indices:
            record = records_by_index.get(index)
            if record is None or not record.summary.strip():
                continue
            pseudo_cells.append(
                {
                    "id": f"sym{index}",
                    "rect": picture_record_rect(record, page_size or (1.0, 1.0)),
                    "text": record.summary.strip(),
                }
            )
        region_cells.extend(cell for cell in pseudo_cells if cell["rect"])
        if not region_cells:
            candidate.warnings.append("missing_region_cells")
            continue
        crop_path = table_dir / f"{candidate.candidate_id}_crop.png"
        if not save_region_crop(
            page_image_path=page_image_path, bbox=candidate.bbox, crop_path=crop_path
        ):
            candidate.warnings.append("crop_failed")
            continue
        candidate.crop_path = str(crop_path)
        if args.skip_vlm:
            continue

        cell_texts = [cell["text"] for cell in region_cells]
        prompt_template = DEFAULT_KPI_PANEL_PROMPT if is_kpi else DEFAULT_TABLE_REGION_PROMPT
        prompt = prompt_template.format(
            region_blocks=region_blocks_prompt_json(region_cells)
        )
        for attempt in range(2):
            answer, usage = ollama_client.call_ollama_vlm(
                base_url=args.ollama_base_url,
                model=args.ollama_model,
                prompt=prompt,
                image_path=crop_path,
                temperature=args.temperature,
                num_ctx=args.num_ctx,
                num_predict=args.num_predict,
                auto_num_ctx=args.auto_num_ctx,
            )
            markdown = sp.strip_meta_commentary(ollama_client.strip_markdown_fences(answer))
            candidate.usage = usage
            if collapse_ws(markdown).upper() == "SKIP":
                candidate.warnings.append("vlm_skip")
                break
            if not is_kpi:
                markdown = normalize_pipe_tables(markdown)
            if is_kpi:
                ok, verify_stats = verify_kpi_list(markdown, cell_texts)
            else:
                # Only docling-table crops carry a grid to check the shape against;
                # region crops pass None and keep the numeric/word-coverage check.
                grid_for_verify = (
                    (candidate.stats or {}).get("grid")
                    if candidate.kind == "docling_table"
                    else None
                )
                ok, verify_stats = verify_region_table(
                    markdown, cell_texts, grid=grid_for_verify
                )
            candidate.stats = {**(candidate.stats or {}), "verify": verify_stats}
            if ok:
                candidate.markdown = markdown
                candidate.verified = True
                if is_kpi:
                    candidate.stats["format"] = "kpi_list"
                suffix = "kpi" if is_kpi else "table"
                (table_dir / f"{candidate.candidate_id}_{suffix}.md").write_text(
                    markdown.rstrip() + "\n", encoding="utf-8"
                )
                break
            candidate.markdown = markdown
            (table_dir / f"{candidate.candidate_id}_rejected.md").write_text(
                markdown.rstrip() + "\n", encoding="utf-8"
            )
            if attempt == 0:
                feedback: list[str] = []
                if verify_stats.get("invented_numbers"):
                    feedback.append(
                        "it contained numbers that are not in the blocks: "
                        + ", ".join(verify_stats["invented_numbers"][:10])
                    )
                if verify_stats.get("missing_numbers"):
                    feedback.append(
                        "it dropped these numbers from the blocks: "
                        + ", ".join(verify_stats["missing_numbers"][:10])
                    )
                structural_feedback = {
                    "column_count_mismatch": (
                        "the table must have exactly "
                        f"{verify_stats.get('source_columns')} columns"
                    ),
                    "repeated_header_in_body": "it repeated the header row inside the body",
                    "multiple_header_blocks": "it must be one single table, not several",
                    "record_count_mismatch": "it dropped or added table rows",
                }.get(verify_stats.get("fail"))
                if structural_feedback:
                    feedback.append(structural_feedback)
                if not feedback:
                    feedback.append(
                        "it was not a valid KPI label/value list"
                        if is_kpi
                        else "it was not a well-formed markdown table"
                    )
                prompt = (
                    prompt.rstrip()
                    + "\n\nYour previous attempt was rejected because "
                    + "; ".join(feedback)
                    + ". Transcribe the region again using every block text exactly once."
                )
            else:
                candidate.warnings.append("verification_failed")

__all__ = [
    "segment_panels", "is_banner_cell", "split_panel_at_banners",
    "region_grid_cells", "region_grid_rows", "is_value_cell", "score_table_region",
    "add_table_candidate", "picture_record_rect",
    "nearby_table_signal_blocks", "picture_record_is_table_like",
    "regions_horizontally_adjacent", "merge_adjacent_regions",
    "evaluate_table_regions", "build_table_candidates",
    "normalize_table_grid", "place_grid_symbols", "place_visual_value", "render_grid_markdown",
    "render_deterministic_docling_table", "render_sectioned_docling_table",
    "verified_tables_prompt_block", "numeric_tokens", "word_tokens",
    "verify_region_table", "verify_kpi_list", "table_candidate_rows", "render_layout_overlay",
    "render_table_candidates_overlay", "region_blocks_prompt_json",
    "transcribe_table_candidates",
]
