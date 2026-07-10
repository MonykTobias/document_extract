"""Coordinate and rectangle helpers used by layout and detection stages.

Docling bounding boxes may use a top-left or bottom-left origin. Public
normalized rectangles always use [left, top, right, bottom] in page-relative
coordinates with a top-left origin.
"""

from __future__ import annotations

from typing import Any

def bbox_area_ratio(bbox: dict[str, Any] | None, page_size: tuple[float, float]) -> float:
    if not bbox:
        return 0.0
    width = abs(float(bbox["r"]) - float(bbox["l"]))
    height = abs(float(bbox["b"]) - float(bbox["t"]))
    page_area = max(page_size[0] * page_size[1], 1.0)
    return min(1.0, (width * height) / page_area)


def bbox_to_pixel_rect(
    bbox: dict[str, Any] | None,
    *,
    page_size: tuple[float, float],
    image_size: tuple[int, int],
) -> tuple[int, int, int, int] | None:
    if not bbox:
        return None
    page_width, page_height = page_size
    image_width, image_height = image_size
    if page_width <= 0 or page_height <= 0 or image_width <= 0 or image_height <= 0:
        return None

    scale_x = image_width / page_width
    scale_y = image_height / page_height
    left = float(bbox["l"])
    right = float(bbox["r"])
    top = float(bbox["t"])
    bottom = float(bbox["b"])
    origin = str(bbox.get("origin", "")).upper()

    x0 = min(left, right) * scale_x
    x1 = max(left, right) * scale_x
    if origin == "BOTTOMLEFT":
        y0 = (page_height - max(top, bottom)) * scale_y
        y1 = (page_height - min(top, bottom)) * scale_y
    else:
        y0 = min(top, bottom) * scale_y
        y1 = max(top, bottom) * scale_y

    return (
        max(0, int(round(x0))),
        max(0, int(round(y0))),
        min(image_width, int(round(x1))),
        min(image_height, int(round(y1))),
    )


def bbox_to_normalized_rect(
    bbox: dict[str, Any] | None,
    page_size: tuple[float, float],
) -> list[float] | None:
    if not bbox:
        return None
    page_width, page_height = page_size
    if page_width <= 0 or page_height <= 0:
        return None

    left = float(bbox["l"])
    right = float(bbox["r"])
    top = float(bbox["t"])
    bottom = float(bbox["b"])
    origin = str(bbox.get("origin", "")).upper()

    x0 = min(left, right)
    x1 = max(left, right)
    if origin == "BOTTOMLEFT":
        y0 = page_height - max(top, bottom)
        y1 = page_height - min(top, bottom)
    else:
        y0 = min(top, bottom)
        y1 = max(top, bottom)

    rect = [
        max(0.0, min(1.0, x0 / page_width)),
        max(0.0, min(1.0, y0 / page_height)),
        max(0.0, min(1.0, x1 / page_width)),
        max(0.0, min(1.0, y1 / page_height)),
    ]
    return [round(value, 3) for value in rect]


def rect_center(rect: list[float] | None) -> tuple[float, float] | None:
    if not rect:
        return None
    return ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)


def rect_distance(a: list[float] | None, b: list[float] | None) -> float:
    center_a = rect_center(a)
    center_b = rect_center(b)
    if center_a is None or center_b is None:
        return 999.0
    return ((center_a[0] - center_b[0]) ** 2 + (center_a[1] - center_b[1]) ** 2) ** 0.5


def rect_area(rect: list[float] | None) -> float:
    if not rect:
        return 0.0
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def rect_aspect_ratio(rect: list[float] | None) -> float:
    if not rect:
        return 0.0
    height = max(rect[3] - rect[1], 0.001)
    return max(rect[2] - rect[0], 0.0) / height


def rect_union(rects: list[list[float]]) -> list[float] | None:
    if not rects:
        return None
    return [
        round(min(rect[0] for rect in rects), 3),
        round(min(rect[1] for rect in rects), 3),
        round(max(rect[2] for rect in rects), 3),
        round(max(rect[3] for rect in rects), 3),
    ]


def rect_intersection_area(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b:
        return 0.0
    x0 = max(a[0], b[0])
    y0 = max(a[1], b[1])
    x1 = min(a[2], b[2])
    y1 = min(a[3], b[3])
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def rect_overlap_ratio(a: list[float] | None, b: list[float] | None) -> float:
    area = min(rect_area(a), rect_area(b))
    if area <= 0:
        return 0.0
    return rect_intersection_area(a, b) / area


def normalized_rect_to_pixel_rect(
    rect: list[float] | None, image_size: tuple[int, int]
) -> tuple[int, int, int, int] | None:
    if not rect:
        return None
    image_width, image_height = image_size
    x0 = int(round(rect[0] * image_width))
    y0 = int(round(rect[1] * image_height))
    x1 = int(round(rect[2] * image_width))
    y1 = int(round(rect[3] * image_height))
    if x1 <= x0 or y1 <= y0:
        return None
    return (
        max(0, x0),
        max(0, y0),
        min(image_width, x1),
        min(image_height, y1),
    )


def cluster_axis_values(values: list[float], tolerance: float) -> list[float]:
    if not values:
        return []
    clusters: list[list[float]] = []
    for value in sorted(values):
        if not clusters or abs(value - (sum(clusters[-1]) / len(clusters[-1]))) > tolerance:
            clusters.append([value])
        else:
            clusters[-1].append(value)
    return [sum(cluster) / len(cluster) for cluster in clusters]


def nearest_cluster_index(value: float, clusters: list[float]) -> int:
    if not clusters:
        return 0
    return min(range(len(clusters)), key=lambda index: abs(clusters[index] - value))


def axis_gaps(cells: list[dict[str, Any]], axis: int, min_gap: float) -> list[float]:
    """Midpoints of empty bands along an axis that no cell interval crosses."""
    intervals = sorted((cell["rect"][axis], cell["rect"][axis + 2]) for cell in cells)
    if not intervals:
        return []
    gaps: list[float] = []
    max_end = intervals[0][1]
    for start, end in intervals[1:]:
        if start - max_end >= min_gap:
            gaps.append((max_end + start) / 2)
        max_end = max(max_end, end)
    return gaps


def partition_cells_at(
    cells: list[dict[str, Any]], axis: int, boundaries: list[float]
) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = [[] for _ in range(len(boundaries) + 1)]
    for cell in cells:
        center = (cell["rect"][axis] + cell["rect"][axis + 2]) / 2
        groups[sum(1 for boundary in boundaries if center > boundary)].append(cell)
    return [group for group in groups if group]

__all__ = [
    "bbox_area_ratio", "bbox_to_pixel_rect", "bbox_to_normalized_rect",
    "rect_center", "rect_distance", "rect_area", "rect_aspect_ratio",
    "rect_union", "rect_intersection_area", "rect_overlap_ratio",
    "normalized_rect_to_pixel_rect", "cluster_axis_values",
    "nearest_cluster_index", "axis_gaps", "partition_cells_at",
]
