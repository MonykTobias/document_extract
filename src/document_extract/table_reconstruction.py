"""Reconcile a Docling table grid against the source page's own geometry.

Docling's ``TableData.grid`` infers row boundaries from local text fragments. On
tables with partial horizontal rules and merged first columns it invents rows
that no PDF rule supports, splits a wrapped paragraph across two of them, and
lets one cell own the tail of its neighbour. Every later stage faithfully copies
that grid, so the repair belongs here: before the grid is normalized, rendered,
or shown to a VLM.

The reconciler answers one question per table -- *which source component belongs
to which row* -- using only evidence the page itself carries:

* **Bands.** Vector rules clipped to the table are the row boundaries. A rule
  only counts when its segments cover most of the table width, so a chart
  gridline or a short decorative stroke cannot invent a row.
* **Components.** A PDF text block (one bullet, one paragraph) is indivisible
  and belongs to the band holding its centre. Blocks, not lines and not centre
  points, are the unit of ownership.
* **Spans.** A partial rule is evidence only for the columns it actually crosses
  and may stop at a vertically merged cell, so a label whose column no rule
  divides spans its bands instead of being cut.

Literal PDF text may restore a source-proven column only when every existing
Docling token occurs in that source stream in order. Otherwise source geometry
can position existing text but cannot add wording. Missing or contradictory
evidence keeps Docling's row, and an unsafe table is marked ``abstained`` so no
later stage treats it as authoritative.
"""

from __future__ import annotations

import unicodedata
from difflib import SequenceMatcher
from typing import Any

from .layout.geometry import bbox_to_normalized_rect
from .layout.reading_order import extract_divider_segments

RECONSTRUCTION_VERSION = 2

# A y position is a row boundary only when the rule segments sitting on it cover
# at least this fraction of the table's width. Partial rules stopping at a
# merged cell stay evidence (page 183's widest gap is ~22% of the table);
# isolated short strokes do not become rows.
RULE_COVERAGE_MIN = 0.5
# Rule y positions within this fraction of the table height are one boundary.
BOUNDARY_MERGE_FRAC = 0.004
# A source line joins the column whose x interval covers this much of it.
LINE_COLUMN_OVERLAP_MIN = 0.5
# A column's Docling text must be this well located in the source stream before
# its geometry is allowed to move anything.
ALIGN_MATCH_MIN = 0.8


# Docling and PyMuPDF disagree on typography for identical source glyphs
# (``suppliers'`` vs ``suppliers’``). Folding these for comparison only keeps
# alignment from collapsing to "abstain" on ordinary punctuation; the text that
# reaches the output is always Docling's.
_PUNCTUATION_FOLD = str.maketrans(
    {
        "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
        "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
        "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
        "―": "-", "−": "-", "­": "",
        " ": " ", " ": " ", " ": " ", "​": "",
    }
)


def _norm(text: str) -> str:
    """Compare-only normalization: NFKC, punctuation folded, case-folded."""
    folded = unicodedata.normalize("NFKC", text).translate(_PUNCTUATION_FOLD)
    # PDF text layers commonly disagree on whether a quoted phrase uses curly
    # double quotes, straight single quotes, or no visible quote at all. Quotes
    # are not content boundaries for the source-subsequence proof.
    folded = folded.replace("'", "").replace('"', "")
    return " ".join(folded.casefold().split())


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def extract_table_page_geometry(
    pdf_page: Any, page_size: tuple[float, float]
) -> dict[str, Any]:
    """Source rules and text blocks/lines for one page, in normalized top-left
    coordinates. Returns empty collections on any extraction failure, which the
    reconciler treats as "no evidence" rather than as a reason to change a grid.
    """
    page_width, page_height = page_size
    if page_width <= 0 or page_height <= 0:
        return {"rules": [], "lines": []}
    rules = extract_divider_segments(pdf_page, page_size)["h"]
    try:
        words = pdf_page.get_text("words")
    except Exception:
        return {"rules": rules, "lines": []}

    grouped: dict[tuple[int, int], list[tuple[int, float, float, float, float, str]]] = {}
    for word in words or []:
        try:
            x0, y0, x1, y1, text, block, line, word_no = word[:8]
        except (TypeError, ValueError):
            continue
        if not str(text).strip():
            continue
        grouped.setdefault((int(block), int(line)), []).append(
            (int(word_no), float(x0), float(y0), float(x1), float(y1), str(text))
        )

    lines: list[dict[str, Any]] = []
    for (block, line), entries in grouped.items():
        entries.sort()
        lines.append(
            {
                "id": f"b{block}l{line}",
                "block": block,
                "text": " ".join(entry[5] for entry in entries),
                "rect": [
                    min(entry[1] for entry in entries) / page_width,
                    min(entry[2] for entry in entries) / page_height,
                    max(entry[3] for entry in entries) / page_width,
                    max(entry[4] for entry in entries) / page_height,
                ],
            }
        )
    lines.sort(key=lambda line: (line["rect"][1], line["rect"][0]))
    return {"rules": rules, "lines": lines}


def _boundaries(
    rules: list[list[float]], table: list[float]
) -> list[dict[str, Any]]:
    """Rule y positions inside the table that carry enough width to be rows.

    Segments sharing a y are one boundary: their union decides both whether the
    boundary exists at all and which columns it divides.
    """
    left, top, right, bottom = table
    width = right - left
    if width <= 0 or bottom - top <= 0:
        return []
    tolerance = (bottom - top) * BOUNDARY_MERGE_FRAC
    groups: list[dict[str, Any]] = []
    for y, x0, x1 in sorted(rules):
        if not (top - tolerance <= y <= bottom + tolerance):
            continue
        span = (max(x0, left), min(x1, right))
        if span[1] <= span[0]:
            continue
        for group in groups:
            if abs(group["y"] - y) <= tolerance:
                group["spans"].append(span)
                break
        else:
            groups.append({"y": y, "spans": [span]})

    boundaries: list[dict[str, Any]] = []
    for group in groups:
        merged: list[list[float]] = []
        for x0, x1 in sorted(group["spans"]):
            if merged and x0 <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], x1)
            else:
                merged.append([x0, x1])
        covered = sum(x1 - x0 for x0, x1 in merged)
        if covered / width < RULE_COVERAGE_MIN:
            continue
        boundaries.append({"y": group["y"], "spans": merged})
    boundaries.sort(key=lambda boundary: boundary["y"])
    return boundaries


def _owner_key(cell: dict[str, Any]) -> tuple[int, int]:
    row = cell.get("start_row_offset_idx")
    column = cell.get("start_col_offset_idx")
    return (
        int(row) if row is not None else int(cell["r"]),
        int(column) if column is not None else int(cell["c"]),
    )


def _collect_owners(grid: dict[str, Any]) -> list[dict[str, Any]]:
    """One logical cell per Docling owner.

    Docling repeats a merged cell at every position it covers; those repeats are
    one owner with one text, never several records' worth of content.
    """
    owners: dict[tuple[int, int], dict[str, Any]] = {}
    for cell in grid.get("cells", []):
        key = _owner_key(cell)
        owner = owners.get(key)
        if owner is None:
            row_end = cell.get("end_row_offset_idx")
            column_end = cell.get("end_col_offset_idx")
            owner = {
                "key": key,
                "row_start": key[0],
                "row_end": int(row_end) if row_end is not None else key[0] + 1,
                "col_start": key[1],
                "col_end": int(column_end) if column_end is not None else key[1] + 1,
                "text": str(cell.get("text") or ""),
                "column_header": bool(cell.get("column_header")),
                "bboxes": [],
                "positions": [],
            }
            owners[key] = owner
        if cell.get("bbox"):
            owner["bboxes"].append(cell["bbox"])
        owner["positions"].append((int(cell["r"]), int(cell["c"])))
    return [owners[key] for key in sorted(owners)]


def _column_intervals(
    owners: list[dict[str, Any]], page_size: tuple[float, float], num_cols: int
) -> dict[int, list[float]]:
    intervals: dict[int, list[float]] = {}
    for owner in owners:
        if owner["col_end"] - owner["col_start"] != 1:
            continue
        for bbox in owner["bboxes"]:
            rect = bbox_to_normalized_rect(bbox, page_size)
            if not rect:
                continue
            column = owner["col_start"]
            if column >= num_cols:
                continue
            current = intervals.get(column)
            if current is None:
                intervals[column] = [rect[0], rect[2]]
            else:
                current[0] = min(current[0], rect[0])
                current[1] = max(current[1], rect[2])
    return intervals


def _assign_lines_to_columns(
    lines: list[dict[str, Any]],
    intervals: dict[int, list[float]],
    table: list[float],
) -> dict[int, list[dict[str, Any]]]:
    """Each in-table source line joins the one column that covers most of it."""
    left, top, right, bottom = table
    by_column: dict[int, list[dict[str, Any]]] = {}
    for line in lines:
        x0, y0, x1, y1 = line["rect"]
        if x1 <= left or x0 >= right or y1 <= top or y0 >= bottom:
            continue
        width = x1 - x0
        if width <= 0:
            continue
        best_column, best_overlap = None, 0.0
        for column, (cl, cr) in intervals.items():
            overlap = min(x1, cr) - max(x0, cl)
            if overlap > best_overlap:
                best_column, best_overlap = column, overlap
        if best_column is None or best_overlap / width < LINE_COLUMN_OVERLAP_MIN:
            continue
        by_column.setdefault(best_column, []).append(line)
    for column_lines in by_column.values():
        column_lines.sort(key=lambda line: (line["rect"][1], line["rect"][0]))
    return by_column


def _align_column(
    owners: list[dict[str, Any]], lines: list[dict[str, Any]]
) -> dict[tuple[int, int], list[dict[str, Any] | None]] | None:
    """Locate each of a column's Docling tokens in its source line stream.

    Returns one source line per Docling token, per owner, or ``None`` when too
    little of the column matches to trust the result -- ligature/OCR drift, or a
    column whose text the line filter never saw. Callers then keep Docling's own
    row for that column rather than guessing.

    The match is a subsequence, not a tiling, because the two streams genuinely
    disagree in both directions: Docling drops whole source lines (observed: an
    entire bullet missing from its grid), and the source carries glyphs Docling
    renders differently. Source lines only ever say *where* Docling's text sits;
    the text that reaches the output is always Docling's own.
    """
    tokens: list[str] = []
    spans: list[tuple[tuple[int, int], int, int]] = []
    for owner in owners:
        start = len(tokens)
        tokens.extend(owner["text"].split())
        spans.append((owner["key"], start, len(tokens)))

    source_tokens: list[str] = []
    source_lines: list[dict[str, Any]] = []
    for line in lines:
        for token in line["text"].split():
            source_tokens.append(token)
            source_lines.append(line)
    if not tokens or not source_tokens:
        return None

    matcher = SequenceMatcher(
        None,
        [_norm(token) for token in tokens],
        [_norm(token) for token in source_tokens],
        autojunk=False,  # 'the'/'and' repeat far too often to be treated as junk
    )
    located: list[dict[str, Any] | None] = [None] * len(tokens)
    matched = 0
    for start, source_start, length in matcher.get_matching_blocks():
        for offset in range(length):
            located[start + offset] = source_lines[source_start + offset]
            matched += 1
    if matched / len(tokens) < ALIGN_MATCH_MIN:
        return None

    # A token Docling has but the source stream does not follows its neighbours;
    # it never invents a band of its own.
    for index in range(len(located)):
        if located[index] is None and index:
            located[index] = located[index - 1]
    for index in range(len(located) - 1, -1, -1):
        if located[index] is None and index + 1 < len(located):
            located[index] = located[index + 1]

    return {key: located[start:end] for key, start, end in spans}


def _band_of(y: float, bands: list[tuple[float, float]]) -> int:
    for index, (top, bottom) in enumerate(bands):
        if y < bottom:
            return index
    return len(bands) - 1


def _divides(boundary: dict[str, Any], interval: list[float]) -> bool:
    """True when this boundary's rules actually cross the column.

    A rule that stops short of a column is no evidence for a row there; that is
    what lets a merged label span bands its neighbours are divided across.
    """
    centre = (interval[0] + interval[1]) / 2
    return any(x0 <= centre <= x1 for x0, x1 in boundary["spans"])


def _regions(dividing: list[bool], count: int) -> list[int]:
    """Map each band to the region its column is actually divided into.

    Only a boundary whose rules cross this column cuts it. Inside one region the
    page draws no line and leaves no gap, so geometry has nothing to say about
    where one row ends -- observed on page 183, whose column 2 bullet list runs
    straight through a boundary its neighbours are divided at. There Docling's
    own row assignment is the only evidence, and it stands. Between regions,
    geometry decides.
    """
    regions: list[int] = []
    index = 0
    for band in range(count):
        if band and dividing[band - 1]:
            index += 1
        regions.append(index)
    return regions


def _source_components(
    by_column: dict[int, list[dict[str, Any]]],
    bands: list[tuple[float, float]],
) -> dict[int, list[dict[str, Any]]]:
    """PDF text components, split at column boundaries.

    PyMuPDF's block number is not a table-cell identifier: a single block can
    contain text from several columns (the header is a common example).  A
    component is therefore one source block *within one assigned column*.
    """
    components: dict[int, list[dict[str, Any]]] = {}
    for column, lines in by_column.items():
        grouped: dict[int, list[dict[str, Any]]] = {}
        for line in lines:
            centre = (line["rect"][1] + line["rect"][3]) / 2
            if not bands[0][0] <= centre <= bands[-1][1]:
                continue
            grouped.setdefault(int(line["block"]), []).append(line)
        for block, block_lines in grouped.items():
            block_lines.sort(key=lambda line: (line["rect"][1], line["rect"][0]))
            rect = _union([line["rect"] for line in block_lines])
            if not rect:
                continue
            components.setdefault(column, []).append(
                {
                    "id": f"b{block}:c{column}",
                    "block": block,
                    "column": column,
                    "text": " ".join(line["text"] for line in block_lines),
                    "rect": rect,
                    "lines": block_lines,
                    "band": _band_of((rect[1] + rect[3]) / 2, bands),
                }
            )
    for column_components in components.values():
        column_components.sort(key=lambda component: (component["rect"][1], component["rect"][0]))
    return components


def _tokens(text: str) -> list[str]:
    tokens = _norm(text).split()
    # A duplicated bullet glyph is a known extractor artefact, not two content
    # tokens. Folding it here lets a source-complete recovery remove the visual
    # duplicate without treating that removal as lost wording.
    return [
        token
        for index, token in enumerate(tokens)
        if token != "■" or not index or tokens[index - 1] != "■"
    ]


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    """Whether every normalized Docling token occurs in source order."""
    if not needle:
        return True
    index = 0
    for token in haystack:
        if token == needle[index]:
            index += 1
            if index == len(needle):
                return True
    return False


def _raw_column_tokens(
    owners: list[dict[str, Any]], column: int, header_rows: int
) -> list[str]:
    """Owner-deduplicated raw body tokens in Docling's logical order."""
    tokens: list[str] = []
    for owner in owners:
        if owner["row_start"] < header_rows or owner["col_start"] != column:
            continue
        if owner["col_end"] - owner["col_start"] != 1:
            continue
        tokens.extend(_tokens(owner["text"]))
    return tokens


def _owner_spans_boundary(
    owner: dict[str, Any],
    owners: list[dict[str, Any]],
    row_band: dict[int, int],
    boundary: int,
    header_rows: int,
) -> bool:
    """Whether a raw rowspan already crosses the two adjacent source bands."""
    if owner["row_end"] - owner["row_start"] <= 1:
        return False
    # Docling can emit a nominal rowspan plus another non-empty owner inside
    # it (page 184). That is contradictory evidence, not a real merged cell.
    if any(
        other["key"] != owner["key"]
        and other["col_start"] == owner["col_start"]
        and other["col_end"] - other["col_start"] == 1
        and owner["row_start"] < other["row_start"] < owner["row_end"]
        and other["text"].strip()
        for other in owners
    ):
        return False
    owner_bands = [
        row_band[row]
        for row in range(max(header_rows, owner["row_start"]), owner["row_end"])
        if row in row_band
    ]
    return bool(owner_bands) and min(owner_bands) <= boundary < max(owner_bands)


def _raw_split_at_boundary(
    owners: list[dict[str, Any]],
    column: int,
    row_band: dict[int, int],
    boundary: int,
    header_rows: int,
) -> bool:
    """Whether independent, non-spanning raw owners propose this split."""
    above = below = False
    for owner in owners:
        conflicting_span = (
            owner["row_end"] - owner["row_start"] > 1
            and any(
                other["key"] != owner["key"]
                and other["col_start"] == column
                and other["col_end"] - other["col_start"] == 1
                and owner["row_start"] < other["row_start"] < owner["row_end"]
                and other["text"].strip()
                for other in owners
            )
        )
        if (
            owner["row_start"] < header_rows
            or owner["col_start"] != column
            or owner["col_end"] - owner["col_start"] != 1
            or (
                owner["row_end"] - owner["row_start"] != 1
                and not conflicting_span
            )
        ):
            continue
        band = row_band.get(owner["row_start"])
        if band == boundary:
            above = True
        elif band == boundary + 1:
            below = True
    return above and below


def _corridor_is_clear(lines: list[dict[str, Any]], y: float) -> bool:
    """Whether source text leaves a local, scale-independent row corridor."""
    heights = [line["rect"][3] - line["rect"][1] for line in lines]
    if not heights:
        return False
    half_height = _median(heights) / 2
    return not any(
        line["rect"][1] - half_height <= y <= line["rect"][3] + half_height
        for line in lines
    )


def reconcile_table_grid(
    grid: dict[str, Any] | None,
    table_bbox: list[float] | None,
    geometry: dict[str, Any] | None,
    page_size: tuple[float, float],
) -> dict[str, Any]:
    """Rebuild a Docling grid's body rows from the page's rule bands.

    Returns ``{"status", "grid", "audit"}``. ``unchanged`` hands back the exact
    input grid (valid tables must stay byte-identical), ``repaired`` a grid whose
    rows follow the source rules, and ``abstained`` the untouched input grid when
    the evidence cannot justify either.
    """
    audit: dict[str, Any] = {"version": RECONSTRUCTION_VERSION}
    if not grid or not table_bbox or not geometry:
        return {"status": "unchanged", "grid": grid, "audit": {**audit, "reason": "no_geometry"}}
    rows = grid.get("rows") or []
    num_cols = int(grid.get("num_cols") or 0)
    header_rows = int(grid.get("header_rows") or 0)
    if not rows or num_cols < 1 or header_rows >= len(rows):
        return {"status": "unchanged", "grid": grid, "audit": {**audit, "reason": "no_body"}}

    boundaries = _boundaries(geometry.get("rules") or [], table_bbox)
    audit["boundaries"] = [round(boundary["y"], 6) for boundary in boundaries]
    if len(boundaries) < 2:
        return {"status": "unchanged", "grid": grid, "audit": {**audit, "reason": "no_rule_bands"}}

    edges = [boundary["y"] for boundary in boundaries]
    bands = list(zip(edges[:-1], edges[1:]))
    audit["band_count"] = len(bands)

    owners = _collect_owners(grid)
    intervals = _column_intervals(owners, page_size, num_cols)
    by_column = _assign_lines_to_columns(geometry.get("lines") or [], intervals, table_bbox)

    block_extent: dict[tuple[int, int], list[float]] = {}
    for column, column_lines in by_column.items():
        for line in column_lines:
            rect = line["rect"]
            key = (column, int(line["block"]))
            current = block_extent.get(key)
            if current is None:
                block_extent[key] = [rect[1], rect[3]]
            else:
                current[0] = min(current[0], rect[1])
                current[1] = max(current[1], rect[3])
    block_band = {
        block: _band_of((top + bottom) / 2, bands)
        for block, (top, bottom) in block_extent.items()
    }

    aligned: dict[tuple[int, int], list[dict[str, Any]]] = {}
    unaligned_columns: list[int] = []
    for column in sorted(intervals):
        column_owners = [
            owner
            for owner in owners
            if owner["col_start"] == column and owner["col_end"] - owner["col_start"] == 1
        ]
        result = _align_column(column_owners, by_column.get(column, []))
        if result is None:
            unaligned_columns.append(column)
            continue
        aligned.update(result)
    audit["unaligned_columns"] = unaligned_columns

    # Fallback band for anything unaligned: the row's own dominant band, taken
    # from the cells that did align. Never a single cell's centre.
    owner_primary: dict[tuple[int, int], int] = {}
    for owner in owners:
        if owner["column_header"] or owner["row_start"] < header_rows:
            continue
        located = aligned.get(owner["key"]) or []
        band_values = [
            block_band[(owner["col_start"], int(line["block"]))]
            for line in located
            if line is not None and (owner["col_start"], int(line["block"])) in block_band
        ]
        if band_values:
            owner_primary[owner["key"]] = int(_median([float(v) for v in band_values]))
            continue
        rects = [
            rect
            for rect in (bbox_to_normalized_rect(bbox, page_size) for bbox in owner["bboxes"])
            if rect
        ]
        if rects and owner["row_end"] - owner["row_start"] == 1:
            owner_primary[owner["key"]] = _band_of(
                _median([(rect[1] + rect[3]) / 2 for rect in rects]), bands
            )

    row_band: dict[int, int] = {}
    for row in range(header_rows, len(rows)):
        candidates = [
            band
            for owner in owners
            if owner["row_start"] == row
            and owner["row_end"] == row + 1
            and (band := owner_primary.get(owner["key"])) is not None
        ]
        if candidates:
            row_band[row] = int(_median([float(band) for band in candidates]))
    if not row_band:
        return {"status": "abstained", "grid": grid, "audit": {**audit, "reason": "no_row_evidence"}}
    # A row whose every cell lacked geometry (all-empty, or bbox-less coverage
    # cells) follows the last row that had some.
    previous = row_band[min(row_band)]
    for row in range(header_rows, len(rows)):
        previous = row_band.setdefault(row, previous)

    # A boundary can be real for one column and a span for the next. A drawn
    # rule wins where it crosses a column. Elsewhere, only extend it through a
    # text-free corridor when Docling independently proposed two adjacent
    # owners. This retains genuine raw rowspans while repairing split cells.
    source_components = _source_components(by_column, bands)
    effective_dividing: dict[int, list[bool]] = {}
    boundary_audit: dict[str, list[dict[str, Any]]] = {}
    for column in range(num_cols):
        interval = intervals.get(column) or [table_bbox[0], table_bbox[2]]
        column_lines = [
            line
            for line in by_column.get(column, [])
            if bands[0][0] <= (line["rect"][1] + line["rect"][3]) / 2 <= bands[-1][1]
        ]
        states: list[dict[str, Any]] = []
        dividing: list[bool] = []
        for index, boundary in enumerate(boundaries[1:-1]):
            if _divides(boundary, interval):
                dividing.append(True)
                states.append({"y": round(boundary["y"], 6), "status": "rule_crosses"})
                continue
            if not _corridor_is_clear(column_lines, boundary["y"]):
                dividing.append(False)
                states.append({"y": round(boundary["y"], 6), "status": "blocked_by_text"})
                continue
            if any(
                _owner_spans_boundary(owner, owners, row_band, index, header_rows)
                for owner in owners
                if owner["col_start"] == column and owner["col_end"] - owner["col_start"] == 1
            ):
                dividing.append(False)
                states.append({"y": round(boundary["y"], 6), "status": "preserved_raw_span"})
                continue
            if _raw_split_at_boundary(owners, column, row_band, index, header_rows) and _corridor_is_clear(
                column_lines, boundary["y"]
            ):
                dividing.append(True)
                states.append({"y": round(boundary["y"], 6), "status": "corridor_extended"})
                continue
            dividing.append(False)
            states.append({"y": round(boundary["y"], 6), "status": "blocked_by_text"})
        effective_dividing[column] = dividing
        boundary_audit[str(column)] = states
    audit["column_boundaries"] = boundary_audit
    audit["column_intervals"] = {
        str(column): [round(value, 6) for value in interval]
        for column, interval in intervals.items()
    }

    # A source-proven column is rebuilt from literal PDF components. The strict
    # subsequence gate is intentionally separate from alignment's permissive
    # 80% positioning threshold: a single unmatched Docling token is enough to
    # keep source text out of the output.
    source_audit: dict[str, dict[str, Any]] = {}
    source_verified: dict[int, list[dict[str, Any]]] = {}
    for column in range(num_cols):
        raw_tokens = _raw_column_tokens(owners, column, header_rows)
        components = source_components.get(column, [])
        source_tokens = [token for component in components for token in _tokens(component["text"])]
        entry: dict[str, Any] = {
            "raw_tokens": len(raw_tokens),
            "source_tokens": len(source_tokens),
            "component_ids": [component["id"] for component in components],
        }
        if not raw_tokens:
            entry["status"] = "empty"
        elif not components:
            entry.update({"status": "untrusted", "reason": "no_source_components"})
        elif not _is_subsequence(raw_tokens, source_tokens):
            entry.update({"status": "untrusted", "reason": "raw_not_source_subsequence"})
        else:
            regions = _regions(effective_dividing[column], len(bands))
            crossing = [
                component["id"]
                for component in components
                if any(
                    regions[index] != regions[index + 1]
                    and component["rect"][1] < boundary["y"] < component["rect"][3]
                    for index, boundary in enumerate(boundaries[1:-1])
                )
            ]
            if crossing:
                entry.update(
                    {
                        "status": "untrusted",
                        "reason": "source_component_crosses_effective_cut",
                        "crossing_components": crossing,
                    }
                )
            else:
                entry["status"] = "verified"
                entry["restored_components"] = [
                    component["id"]
                    for component in components
                    if not _is_subsequence(_tokens(component["text"]), raw_tokens)
                ]
                source_verified[column] = components
        source_audit[str(column)] = entry
    audit["source_content"] = source_audit

    # Source-verified regions become one deterministic owner each. Components
    # are not deduplicated: each PDF component is admitted exactly once, while
    # _build_grid repeats the same owner in each covered logical row.
    segments: list[dict[str, Any]] = []
    for column, components in source_verified.items():
        regions = _regions(effective_dividing[column], len(bands))
        for region in sorted(set(regions)):
            component_group = [
                component for component in components if regions[component["band"]] == region
            ]
            if not component_group:
                continue
            region_bands = [band for band, value in enumerate(regions) if value == region]
            rects = [component["rect"] for component in component_group]
            segments.append(
                {
                    "owner": ("source", column, region),
                    "col_start": column,
                    "col_end": column + 1,
                    "band_low": min(region_bands),
                    "band_high": max(region_bands),
                    "text": " ".join(component["text"] for component in component_group),
                    "order": (min(region_bands), 0),
                    "top": min(rect[1] for rect in rects),
                    "rect": _union(rects),
                    "column_header": False,
                }
            )

    # Untrusted columns retain the former Docling-only construction. They may
    # use source geometry to place existing text, but never receive PDF text.
    prepared: list[dict[str, Any]] = []
    for owner in owners:
        if owner["row_start"] < header_rows:
            continue
        column = owner["col_start"]
        if column in source_verified:
            continue
        regions = _regions(effective_dividing.get(column, []), len(bands))
        located = aligned.get(owner["key"]) or []
        home = row_band.get(owner["row_start"], 0)
        fallback = owner_primary.get(owner["key"])
        if fallback is None:
            fallback = home

        groups: list[dict[str, Any]] = []
        tokens = owner["text"].split()
        if not located or len(located) != len(tokens):
            groups = [{"region": regions[fallback], "bands": [fallback], "lines": [], "tokens": tokens}]
        else:
            for token, line in zip(tokens, located):
                band = (
                    block_band.get((column, int(line["block"])))
                    if line is not None
                    else None
                )
                if band is None:
                    band = groups[-1]["bands"][-1] if groups else fallback
                region = regions[band]
                if groups and groups[-1]["region"] == region:
                    groups[-1]["tokens"].append(token)
                    groups[-1]["bands"].append(band)
                    if line is not None:
                        groups[-1]["lines"].append(line)
                else:
                    groups.append(
                        {
                            "region": region,
                            "bands": [band],
                            "lines": [line] if line is not None else [],
                            "tokens": [token],
                        }
                    )
        if not groups:
            groups = [{"region": regions[fallback], "bands": [fallback], "lines": [], "tokens": tokens}]

        for group in groups:
            if group["region"] == regions[home]:
                group["band"] = home
            else:
                group["band"] = int(_median([float(band) for band in group["bands"]]))
        prepared.append({"owner": owner, "groups": groups, "regions": regions})

    # Which band each column's owners hold on their own evidence.
    claimed: dict[int, dict[int, set[tuple[int, int]]]] = {}
    for entry in prepared:
        owner = entry["owner"]
        for group in entry["groups"]:
            if not " ".join(group["tokens"]).strip():
                continue
            claimed.setdefault(owner["col_start"], {}).setdefault(group["band"], set()).add(
                owner["key"]
            )

    for entry in prepared:
        owner, groups, regions = entry["owner"], entry["groups"], entry["regions"]
        column_claims = claimed.get(owner["col_start"], {})
        for index, group in enumerate(groups):
            low = high = group["band"]
            if len(groups) == 1 and " ".join(group["tokens"]).strip():
                # A lone owner grows to fill its region: no rule divides those
                # bands here, so the cell is merged across them. It stops at any
                # band another owner holds -- a label is never repeated into a
                # row that has its own content.
                while (
                    low > 0
                    and regions[low - 1] == group["region"]
                    and not column_claims.get(low - 1, set()) - {owner["key"]}
                ):
                    low -= 1
                while (
                    high < len(bands) - 1
                    and regions[high + 1] == group["region"]
                    and not column_claims.get(high + 1, set()) - {owner["key"]}
                ):
                    high += 1
            rects = [line["rect"] for line in group["lines"]] or [
                rect
                for rect in (bbox_to_normalized_rect(bbox, page_size) for bbox in owner["bboxes"])
                if rect
            ]
            segments.append(
                {
                    "owner": owner["key"],
                    "col_start": owner["col_start"],
                    "col_end": owner["col_end"],
                    "band_low": low,
                    "band_high": high,
                    "text": " ".join(group["tokens"]),
                    "order": (group["band"], index),
                    "top": min((rect[1] for rect in rects), default=0.0),
                    "rect": _union(rects),
                    "column_header": owner["column_header"],
                }
            )

    used_bands = sorted(
        {
            band
            for segment in segments
            if segment["text"].strip()
            for band in range(segment["band_low"], segment["band_high"] + 1)
        }
    )
    if not used_bands:
        return {"status": "abstained", "grid": grid, "audit": {**audit, "reason": "no_content_bands"}}

    band_row = {band: header_rows + index for index, band in enumerate(used_bands)}
    for segment in segments:
        segment["band_low"] = max(segment["band_low"], used_bands[0])
        segment["band_high"] = min(segment["band_high"], used_bands[-1])
        while segment["band_low"] not in band_row and segment["band_low"] < used_bands[-1]:
            segment["band_low"] += 1
        while segment["band_high"] not in band_row and segment["band_high"] > used_bands[0]:
            segment["band_high"] -= 1

    new_grid = _build_grid(
        grid, segments, band_row, used_bands, header_rows, num_cols, page_size
    )
    audit.update(
        {
            "raw_body_rows": len(rows) - header_rows,
            "body_rows": len(used_bands),
            "segments": len(segments),
        }
    )

    kept = _column_text(grid, header_rows, num_cols)
    rebuilt = _column_text(new_grid, header_rows, num_cols)
    for column in range(num_cols):
        if not _is_subsequence(kept[column], rebuilt[column]):
            audit["reason"] = "raw_text_not_preserved"
            audit["failed_column"] = column
            return {"status": "abstained", "grid": grid, "audit": audit}
        if column in source_verified:
            expected = [
                token
                for component in source_verified[column]
                for token in _tokens(component["text"])
            ]
            if rebuilt[column] != expected:
                audit["reason"] = "source_text_not_preserved"
                audit["failed_column"] = column
                return {"status": "abstained", "grid": grid, "audit": audit}
            source_audit[str(column)]["output_tokens"] = len(rebuilt[column])
        elif kept[column] != rebuilt[column]:
            audit["reason"] = "untrusted_text_changed"
            audit["failed_column"] = column
            return {"status": "abstained", "grid": grid, "audit": audit}

    if _same_shape(grid, new_grid):
        return {"status": "unchanged", "grid": grid, "audit": {**audit, "reason": "raw_matches_rules"}}
    return {"status": "repaired", "grid": new_grid, "audit": audit}


def _union(rects: list[list[float]]) -> list[float] | None:
    if not rects:
        return None
    return [
        min(rect[0] for rect in rects),
        min(rect[1] for rect in rects),
        max(rect[2] for rect in rects),
        max(rect[3] for rect in rects),
    ]


def _column_text(grid: dict[str, Any], header_rows: int, num_cols: int) -> list[list[str]]:
    """Per-column body token stream, deduplicated across a merged cell's repeats.

    Reconstruction only ever moves text up or down inside its column, so this is
    invariant across a correct repair and catches any drop or duplication.
    """
    streams: list[list[str]] = []
    for column in range(num_cols):
        tokens: list[str] = []
        seen: set[tuple[int, int]] = set()
        for cell in grid.get("cells", []):
            if int(cell["c"]) != column or int(cell["r"]) < header_rows:
                continue
            key = _owner_key(cell)
            if key in seen:
                continue
            seen.add(key)
            tokens.extend(_tokens(str(cell.get("text") or "")))
        streams.append(tokens)
    return streams


def _same_shape(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (left.get("rows") or []) == (right.get("rows") or [])


def _build_grid(
    grid: dict[str, Any],
    segments: list[dict[str, Any]],
    band_row: dict[int, int],
    used_bands: list[int],
    header_rows: int,
    num_cols: int,
    page_size: tuple[float, float],
) -> dict[str, Any]:
    """Dense rows plus per-position cells, in ``table_grid_structured``'s shape.

    Reconstructed bboxes go back out in Docling's own units (PDF points,
    top-left origin) so every downstream reader normalizes them exactly once.
    """
    page_width, page_height = page_size
    by_slot: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for segment in segments:
        if segment["band_low"] not in band_row or segment["band_high"] not in band_row:
            continue
        # An empty owner claims nothing: letting it hold a slot would punch a
        # hole through a merged neighbour that legitimately spans into it.
        if not segment["text"].strip():
            continue
        by_slot.setdefault((segment["band_low"], segment["col_start"]), []).append(segment)

    total_rows = header_rows + len(used_bands)
    rows: list[list[str]] = [["" for _ in range(num_cols)] for _ in range(total_rows)]
    cells: list[dict[str, Any]] = []

    for cell in grid.get("cells", []):
        if int(cell["r"]) >= header_rows:
            continue
        cells.append(dict(cell))
        if int(cell["c"]) < num_cols:
            rows[int(cell["r"])][int(cell["c"])] = str(cell.get("text") or "")

    merged: list[dict[str, Any]] = []
    for (band, column), group in sorted(by_slot.items()):
        group.sort(key=lambda segment: (segment["top"], segment["order"]))
        text = " ".join(segment["text"] for segment in group if segment["text"].strip())
        merged.append(
            {
                "band_low": band,
                "band_high": max(segment["band_high"] for segment in group),
                "col_start": column,
                "col_end": max(segment["col_end"] for segment in group),
                "text": text,
                "rect": _union([segment["rect"] for segment in group if segment["rect"]]),
            }
        )

    for entry in merged:
        row_start = band_row[entry["band_low"]]
        row_end = band_row[entry["band_high"]] + 1
        rect = entry["rect"]
        bbox = (
            None
            if not rect
            else {
                "l": rect[0] * page_width,
                "t": rect[1] * page_height,
                "r": rect[2] * page_width,
                "b": rect[3] * page_height,
                "origin": "TOPLEFT",
            }
        )
        for row in range(row_start, row_end):
            for column in range(entry["col_start"], min(entry["col_end"], num_cols)):
                rows[row][column] = entry["text"]
                cells.append(
                    {
                        "r": row,
                        "c": column,
                        "text": entry["text"],
                        "bbox": bbox,
                        "column_header": False,
                        "start_row_offset_idx": row_start,
                        "end_row_offset_idx": row_end,
                        "start_col_offset_idx": entry["col_start"],
                        "end_col_offset_idx": entry["col_end"],
                        "row_span": row_end - row_start,
                        "col_span": entry["col_end"] - entry["col_start"],
                    }
                )

    covered = {(cell["r"], cell["c"]) for cell in cells}
    for row in range(header_rows, total_rows):
        for column in range(num_cols):
            if (row, column) in covered:
                continue
            cells.append(
                {
                    "r": row,
                    "c": column,
                    "text": "",
                    "bbox": None,
                    "column_header": False,
                    "start_row_offset_idx": row,
                    "end_row_offset_idx": row + 1,
                    "start_col_offset_idx": column,
                    "end_col_offset_idx": column + 1,
                    "row_span": 1,
                    "col_span": 1,
                }
            )
    cells.sort(key=lambda cell: (cell["r"], cell["c"]))
    return {
        "rows": rows,
        "num_cols": num_cols,
        "header_rows": header_rows,
        "cells": cells,
    }


def render_reconstruction_overlay(
    *,
    page_image_path: Any,
    candidates: list[Any],
    geometry: dict[str, Any] | None,
    page_size: tuple[float, float],
    output_path: Any,
) -> bool:
    """Draw the evidence a reconciliation acted on, over the page image.

    Shows direct rule cuts (green), corridor extensions (blue), and blocked
    partial cuts (yellow), plus raw cells changed by a repair (red). This is
    written on abstention too, so geometry decisions stay reviewable.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return False
    reconstructed = [
        candidate
        for candidate in candidates
        if (getattr(candidate, "stats", None) or {}).get("reconstruction")
    ]
    if not reconstructed or not geometry:
        return False
    try:
        image = Image.open(page_image_path).convert("RGB")
    except Exception:
        return False

    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    width, height = image.size

    for candidate in reconstructed:
        stats = candidate.stats or {}
        audit = stats.get("reconstruction") or {}
        table = getattr(candidate, "bbox", None)
        if not table:
            continue
        left, top, right, bottom = table
        draw.rectangle(
            (left * width, top * height, right * width, bottom * height),
            outline="#36a3ff",
            width=3,
        )
        for y, x0, x1 in geometry.get("rules") or []:
            if not (top <= y <= bottom):
                continue
            draw.line(
                (max(x0, left) * width, y * height, min(x1, right) * width, y * height),
                fill="#9aa0a6",
                width=1,
            )
        colors = {
            "rule_crosses": "#00d084",
            "corridor_extended": "#36a3ff",
            "blocked_by_text": "#f4c542",
            "preserved_raw_span": "#f4c542",
        }
        intervals = audit.get("column_intervals") or {}
        for column, states in (audit.get("column_boundaries") or {}).items():
            interval = intervals.get(str(column))
            if not interval or len(interval) != 2:
                continue
            for state in states:
                y = float(state.get("y", -1.0))
                if not top <= y <= bottom:
                    continue
                draw.line(
                    (interval[0] * width, y * height, interval[1] * width, y * height),
                    fill=colors.get(state.get("status"), "#9aa0a6"),
                    width=3,
                )

        # Raw cells whose row the repair changed.
        for cell in (stats.get("grid_raw") or {}).get("cells", []):
            box = cell.get("bbox")
            if not box:
                continue
            rect = bbox_to_normalized_rect(box, page_size)
            if rect:
                draw.rectangle(
                    (rect[0] * width, rect[1] * height, rect[2] * width, rect[3] * height),
                    outline="#ff5a36",
                    width=1,
                )

        restored = sum(
            len(value.get("restored_components") or [])
            for value in (audit.get("source_content") or {}).values()
        )
        label = (
            f"{candidate.candidate_id} {audit.get('status', '?')} "
            f"raw={audit.get('raw_body_rows', '?')} -> {audit.get('body_rows', '?')} "
            f"source+={restored}"
        )
        draw.text((left * width + 3, top * height + 3), label, fill="#ff5a36", font=font)

    try:
        image.save(output_path)
    except Exception:
        return False
    return True


__all__ = [
    "RECONSTRUCTION_VERSION",
    "extract_table_page_geometry",
    "reconcile_table_grid",
    "render_reconstruction_overlay",
]
