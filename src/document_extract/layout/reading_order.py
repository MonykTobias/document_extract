"""Deterministic divider-aware reading-order reconstruction.

The algorithm cuts only on strong divider or whitespace evidence, preserves
Docling order in leaf regions, and treats tables and pictures as atomic blocks.
"""

from __future__ import annotations

from typing import Any

from ..docling_adapter import (
    bbox_dict,
    is_heading_item,
    is_picture_item,
    item_kind,
    item_text,
)
from .geometry import (
    axis_gaps,
    bbox_to_normalized_rect,
    partition_cells_at,
    rect_area,
    rect_union,
)
from .prompt_map import collapse_ws

READING_MAX_DEPTH = 4
READING_EDGE_EPS = 0.006  # max normalized overshoot of a cell across a cut
READING_BOUNDARY_NUDGE = 0.002
READING_BOUNDARY_MIN_SPACING = 0.01
READING_H_DIVIDER_MIN_SPAN = 0.6  # h-rule must span this fraction of region width
READING_V_DIVIDER_MIN_SPAN = 0.35  # v-rule must span this fraction of region height
READING_BAND_GAP = 0.05  # whitespace-only horizontal band cut
READING_COLUMN_GUTTER = 0.035  # whitespace-only vertical column cut
READING_BANNER_WIDE_RATIO = 0.5
READING_BANNER_GAP = 0.028  # clearance a banner needs above it
READING_RULE_HEADING_GAP = 0.03  # a rule adopts a heading sitting this close above
READING_FULL_PAGE_AREA = 0.85  # cells this large (backgrounds) never block cuts
# Page furniture (running heads, nav bars, page numbers, edge logos) fully
# inside these margin strips keeps its position in Docling's stream instead of
# being re-sorted into a geometric band — moving it is churn, not a fix.
READING_PIN_TOP = 0.09
READING_PIN_BOTTOM = 0.93
READING_PIN_LEFT = 0.06
READING_PIN_RIGHT = 0.94
DIVIDER_MAX_THICKNESS = 0.008
DIVIDER_MIN_COLLECT_SPAN = 0.03
DIVIDER_MERGE_TOL = 0.005
DIVIDER_MERGE_GAP = 0.02

def _merge_divider_segments(segments: list[list[float]]) -> list[list[float]]:
    """Merge near-collinear overlapping segments (split strokes, dashes)."""
    merged: list[list[float]] = []
    for pos, lo, hi in sorted(segments):
        for seg in merged:
            if (
                abs(seg[0] - pos) <= DIVIDER_MERGE_TOL
                and lo - seg[2] <= DIVIDER_MERGE_GAP
                and seg[1] - hi <= DIVIDER_MERGE_GAP
            ):
                seg[1] = min(seg[1], lo)
                seg[2] = max(seg[2], hi)
                break
        else:
            merged.append([pos, lo, hi])
    return merged


def divider_segments_from_drawings(
    drawings: list[dict[str, Any]] | None, page_size: tuple[float, float]
) -> dict[str, list[list[float]]]:
    """Thin, long strokes/fills from PDF vector graphics, as rule segments.

    Region-relative span and clean-corridor checks happen later, so this only
    needs to be permissive enough: everything long-and-thin is collected, and
    chart gridlines etc. are vetoed at cut time by the atomic cells they cross.
    """
    page_width, page_height = page_size
    if page_width <= 0 or page_height <= 0:
        return {"h": [], "v": []}
    h_raw: list[list[float]] = []
    v_raw: list[list[float]] = []

    def add_span(x0: float, y0: float, x1: float, y1: float) -> None:
        x0, x1 = sorted((x0 / page_width, x1 / page_width))
        y0, y1 = sorted((y0 / page_height, y1 / page_height))
        if y1 - y0 <= DIVIDER_MAX_THICKNESS and x1 - x0 >= DIVIDER_MIN_COLLECT_SPAN:
            h_raw.append([(y0 + y1) / 2, x0, x1])
        elif x1 - x0 <= DIVIDER_MAX_THICKNESS and y1 - y0 >= DIVIDER_MIN_COLLECT_SPAN:
            v_raw.append([(x0 + x1) / 2, y0, y1])

    for drawing in drawings or []:
        entries = drawing.get("items") if isinstance(drawing, dict) else None
        for entry in entries or []:
            if not entry:
                continue
            kind, coords = entry[0], entry[1:]
            try:
                if kind == "l" and len(coords) >= 2:
                    add_span(
                        float(coords[0].x),
                        float(coords[0].y),
                        float(coords[1].x),
                        float(coords[1].y),
                    )
                elif kind == "re" and coords:
                    rect = coords[0]
                    add_span(
                        float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)
                    )
            except (AttributeError, TypeError, ValueError):
                continue
    return {"h": _merge_divider_segments(h_raw), "v": _merge_divider_segments(v_raw)}


def extract_divider_segments(
    pdf_page: Any, page_size: tuple[float, float]
) -> dict[str, list[list[float]]]:
    """Rule segments from a fitz page's vector drawings; empty on any failure."""
    try:
        drawings = pdf_page.get_drawings()
    except Exception:
        return {"h": [], "v": []}
    return divider_segments_from_drawings(drawings, page_size)


def _owns_row(cell: dict[str, Any], cells: list[dict[str, Any]]) -> bool:
    """True when no other cell shares this cell's horizontal strip."""
    top, bottom = cell["rect"][1], cell["rect"][3]
    for other in cells:
        if other is cell:
            continue
        overlap = min(bottom, other["rect"][3]) - max(top, other["rect"][1])
        limit = max(
            0.002, 0.3 * min(bottom - top, other["rect"][3] - other["rect"][1])
        )
        if overlap > limit:
            return False
    return True


def _clearance_above(cell: dict[str, Any], cells: list[dict[str, Any]]) -> float:
    """Vertical whitespace between this cell and the nearest cell fully above it."""
    top = cell["rect"][1]
    prev_bottom: float | None = None
    for other in cells:
        if other is cell:
            continue
        bottom = other["rect"][3]
        if bottom <= top + 0.002 and (prev_bottom is None or bottom > prev_bottom):
            prev_bottom = bottom
    if prev_bottom is None:
        return 1.0
    return top - prev_bottom


def _banner_boundaries(
    cells: list[dict[str, Any]], region: list[float]
) -> list[float]:
    """Cut boundaries just above wide section-banner headings.

    Only headings spanning most of the region width and owning their full
    horizontal strip qualify: a narrow heading that merely happens to have
    whitespace beside it (e.g. a panel title in a multi-panel dashboard) is
    not band evidence on its own — narrow banners only produce cuts when a
    graphical rule adopts them via _lift_boundary_above_headings.
    """
    region_width = max(region[2] - region[0], 0.001)
    bounds: list[float] = []
    for cell in cells:
        if not cell["is_heading"]:
            continue
        if cell["rect"][2] - cell["rect"][0] < READING_BANNER_WIDE_RATIO * region_width:
            continue
        if not _owns_row(cell, cells):
            continue
        if _clearance_above(cell, cells) < READING_BANNER_GAP:
            continue
        bounds.append(cell["rect"][1] - READING_BOUNDARY_NUDGE)
    return bounds


def _lift_boundary_above_headings(
    boundary: float, cells: list[dict[str, Any]]
) -> float:
    """A rule drawn under a section heading separates bands *above* the heading."""
    for _ in range(2):
        adopted = [
            cell
            for cell in cells
            if cell["is_heading"]
            and cell["rect"][3] <= boundary + READING_EDGE_EPS
            and boundary - cell["rect"][3] <= READING_RULE_HEADING_GAP
            and _owns_row(cell, cells)
        ]
        if not adopted:
            break
        boundary = min(cell["rect"][1] for cell in adopted) - READING_BOUNDARY_NUDGE
    return boundary


def _boundary_is_clean(
    cells: list[dict[str, Any]], axis: int, boundary: float
) -> bool:
    """No cell straddles the cut by more than the edge tolerance."""
    for cell in cells:
        lo, hi = cell["rect"][axis], cell["rect"][axis + 2]
        if lo < boundary - READING_EDGE_EPS and hi > boundary + READING_EDGE_EPS:
            return False
    return True


def _clean_boundaries(
    bounds: list[float],
    cells: list[dict[str, Any]],
    axis: int,
    region: list[float],
) -> list[float]:
    lo, hi = (region[1], region[3]) if axis == 1 else (region[0], region[2])
    out: list[float] = []
    for boundary in sorted(bounds):
        if not lo + READING_EDGE_EPS < boundary < hi - READING_EDGE_EPS:
            continue
        if out and boundary - out[-1] < READING_BOUNDARY_MIN_SPACING:
            continue
        if _boundary_is_clean(cells, axis, boundary):
            out.append(boundary)
    return out


def _horizontal_boundaries(
    cells: list[dict[str, Any]],
    region: list[float],
    dividers: dict[str, list[list[float]]],
) -> list[float]:
    region_width = max(region[2] - region[0], 0.001)
    bounds: list[float] = []
    for pos, lo, hi in dividers.get("h", []):
        if not region[1] + READING_EDGE_EPS < pos < region[3] - READING_EDGE_EPS:
            continue
        if min(hi, region[2]) - max(lo, region[0]) < READING_H_DIVIDER_MIN_SPAN * region_width:
            continue
        # A rule only counts as a section divider when its banner heading sits
        # right above it (the heading + underline pattern). Heading-less lines
        # (table row separators, decorative strips between timeline rows) are
        # not band evidence — those layouts only split at the wide-whitespace
        # or wide-banner boundaries below.
        lifted = _lift_boundary_above_headings(pos, cells)
        if lifted < pos - READING_BOUNDARY_NUDGE / 2:
            bounds.append(lifted)
    bounds.extend(_banner_boundaries(cells, region))
    bounds.extend(axis_gaps(cells, 1, READING_BAND_GAP))
    return _clean_boundaries(bounds, cells, 1, region)


def _vertical_boundaries(
    cells: list[dict[str, Any]],
    region: list[float],
    dividers: dict[str, list[list[float]]],
) -> list[float]:
    region_height = max(region[3] - region[1], 0.001)
    bounds: list[float] = []
    for pos, lo, hi in dividers.get("v", []):
        if not region[0] + READING_EDGE_EPS < pos < region[2] - READING_EDGE_EPS:
            continue
        if min(hi, region[3]) - max(lo, region[1]) < READING_V_DIVIDER_MIN_SPAN * region_height:
            continue
        bounds.append(pos)
    bounds.extend(axis_gaps(cells, 0, READING_COLUMN_GUTTER))
    return _clean_boundaries(bounds, cells, 0, region)


def _order_region_cells(
    cells: list[dict[str, Any]],
    dividers: dict[str, list[list[float]]],
    depth: int = 0,
) -> list[dict[str, Any]]:
    """Recursive divider-aware region cut; leaves keep Docling's own order.

    Horizontal band cuts are tried before vertical column cuts, so a divider
    that separates stacked sections wins over the full-height column reading
    Docling defaulted to. Within a leaf region the original order is kept,
    which makes the whole pass a no-op on pages Docling already got right.
    """
    by_docling_order = sorted(cells, key=lambda cell: cell["index"])
    if len(cells) <= 1 or depth >= READING_MAX_DEPTH:
        return by_docling_order
    # Full-page backgrounds and hero images must not veto or absorb cuts.
    blocking = [cell for cell in cells if cell["blocking"]]
    if len(blocking) < 2:
        return by_docling_order
    region = rect_union([cell["rect"] for cell in blocking])
    if not region:
        return by_docling_order
    for axis, boundaries in (
        (1, _horizontal_boundaries(blocking, region, dividers)),
        (0, _vertical_boundaries(blocking, region, dividers)),
    ):
        if not boundaries:
            continue
        groups = partition_cells_at(cells, axis, boundaries)
        if len(groups) < 2:
            continue
        ordered: list[dict[str, Any]] = []
        for group in groups:
            ordered.extend(_order_region_cells(group, dividers, depth + 1))
        return ordered
    return by_docling_order


def _normalize_caption_order(
    order: list[int], cells_by_index: dict[int, dict[str, Any]]
) -> list[int]:
    """Read a caption before its picture when it sits visually above it.

    Docling streams caption items after their picture regardless of where the
    caption is placed, so a chart whose title is typed as a caption would
    otherwise serialize as image-then-title. Captions below their picture
    (classic figure captions) keep their position.
    """
    out = list(order)
    for _ in range(2):  # a picture can carry a short stack of caption lines
        swapped = False
        for pos in range(len(out) - 1):
            picture = cells_by_index.get(out[pos])
            caption = cells_by_index.get(out[pos + 1])
            if not picture or not caption:
                continue
            if not picture.get("is_picture") or caption.get("kind") != "caption":
                continue
            pic_rect = picture.get("rect")
            cap_rect = caption.get("rect")
            if not pic_rect or not cap_rect:
                continue
            x_overlap = min(cap_rect[2], pic_rect[2]) - max(cap_rect[0], pic_rect[0])
            caption_above = (cap_rect[1] + cap_rect[3]) / 2 < (pic_rect[1] + pic_rect[3]) / 2
            if caption_above and x_overlap > 0:
                out[pos], out[pos + 1] = out[pos + 1], out[pos]
                swapped = True
        if not swapped:
            break
    return out


def reading_order_permutation(
    cells: list[dict[str, Any]],
    dividers: dict[str, list[list[float]]] | None,
) -> list[int] | None:
    """Divider-aware reading order over page cells; None when unchanged.

    Cells carry {index, rect (normalized, may be None), is_heading, text}.
    Cells without geometry, and margin furniture (running heads, nav bars,
    page numbers, edge logos), stay glued to the cell they followed in
    Docling's stream — moving furniture is churn, not a fix.
    """
    dividers = dividers or {"h": [], "v": []}
    anchors: list[dict[str, Any]] = []
    followers: dict[int, list[int]] = {}
    leading: list[int] = []
    last_anchor: int | None = None
    for cell in cells:
        rect = cell.get("rect")
        pinned_margin_furniture = rect is not None and (
            rect[3] <= READING_PIN_TOP
            or rect[1] >= READING_PIN_BOTTOM
            or rect[2] <= READING_PIN_LEFT
            or rect[0] >= READING_PIN_RIGHT
        )
        if rect is None or pinned_margin_furniture:
            if last_anchor is None:
                leading.append(cell["index"])
            else:
                followers.setdefault(last_anchor, []).append(cell["index"])
            continue
        last_anchor = cell["index"]
        anchors.append(
            {
                **cell,
                "blocking": rect_area(rect) < READING_FULL_PAGE_AREA,
            }
        )
    if len(anchors) < 4:
        return None
    ordered = _order_region_cells(anchors, dividers)
    order = [cell["index"] for cell in ordered]
    if order == [cell["index"] for cell in anchors]:
        return None
    full_order = list(leading)
    for index in order:
        full_order.append(index)
        full_order.extend(followers.get(index, []))
    full_order = _normalize_caption_order(
        full_order, {cell["index"]: cell for cell in cells}
    )
    if full_order == sorted(full_order):
        return None
    return full_order


def reorder_items_for_reading_order(
    items: list[Any],
    page_size: tuple[float, float],
    dividers: dict[str, list[list[float]]] | None,
) -> tuple[list[Any], dict[str, Any]]:
    """Re-order page items along divider-separated layout regions.

    Returns the (possibly reordered) items plus an info dict for the manifest.
    When the computed order matches Docling's, the original list is returned
    unchanged so callers keep using Docling's own markdown serializer.
    """
    dividers = dividers or {"h": [], "v": []}
    info: dict[str, Any] = {
        "applied": False,
        "moved_items": 0,
        "h_divider_count": len(dividers.get("h", [])),
        "v_divider_count": len(dividers.get("v", [])),
    }
    cells = [
        {
            "index": index,
            "rect": bbox_to_normalized_rect(bbox_dict(item), page_size),
            "is_heading": is_heading_item(item),
            "is_picture": is_picture_item(item),
            "kind": item_kind(item),
            "text": collapse_ws(item_text(item)),
        }
        for index, item in enumerate(items)
    ]
    full_order = reading_order_permutation(cells, dividers)
    if full_order is None:
        return items, info
    info["applied"] = True
    info["moved_items"] = sum(
        1 for position, index in enumerate(full_order) if position != index
    )
    return [items[index] for index in full_order], info

__all__ = [
    "divider_segments_from_drawings", "extract_divider_segments",
    "reading_order_permutation", "reorder_items_for_reading_order",
]
