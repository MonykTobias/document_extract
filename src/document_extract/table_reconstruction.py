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

Text is never synthesized from the PDF extractor: source lines are matched
against Docling's own cell text and only used to decide *where* that text goes.
Anything that does not align keeps Docling's row, and a table whose evidence is
absent or contradictory is returned untouched and marked ``abstained`` so no
later stage treats it as authoritative.
"""

from __future__ import annotations

import unicodedata
from difflib import SequenceMatcher
from typing import Any

from .layout.geometry import bbox_to_normalized_rect
from .layout.reading_order import extract_divider_segments

RECONSTRUCTION_VERSION = 1

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

    block_extent: dict[int, list[float]] = {}
    for column_lines in by_column.values():
        for line in column_lines:
            rect = line["rect"]
            current = block_extent.get(line["block"])
            if current is None:
                block_extent[line["block"]] = [rect[1], rect[3]]
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
            block_band[line["block"]]
            for line in located
            if line is not None and line["block"] in block_band
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

    # Every body owner becomes one or more (band range, text) segments. Bands
    # come first for every owner, then extension, so a merged label can only
    # grow into space no other owner in its column already holds.
    segments: list[dict[str, Any]] = []
    prepared: list[dict[str, Any]] = []
    for owner in owners:
        if owner["row_start"] < header_rows:
            continue
        column = owner["col_start"]
        interval = intervals.get(column) or [table_bbox[0], table_bbox[2]]
        dividing = [_divides(boundary, interval) for boundary in boundaries[1:-1]]
        regions = _regions(dividing, len(bands))
        located = aligned.get(owner["key"]) or []
        home = row_band.get(owner["row_start"], 0)
        fallback = owner_primary.get(owner["key"])
        if fallback is None:
            fallback = home

        # A cell breaks only where its column is genuinely divided.
        groups: list[dict[str, Any]] = []
        tokens = owner["text"].split()
        if not located or len(located) != len(tokens):
            groups = [{"region": regions[fallback], "bands": [fallback], "lines": [], "tokens": tokens}]
        else:
            for token, line in zip(tokens, located):
                band = block_band.get(line["block"]) if line is not None else None
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

        # Inside its own region a cell keeps Docling's row; only a group that
        # geometry moved into another region is placed by its own content.
        for group in groups:
            if group["region"] == regions[home]:
                group["band"] = home
            else:
                group["band"] = int(_median([float(band) for band in group["bands"]]))
        prepared.append(
            {"owner": owner, "groups": groups, "dividing": dividing, "regions": regions}
        )

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
    if kept != rebuilt:
        audit["reason"] = "text_not_preserved"
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
            tokens.extend(_norm(str(cell.get("text") or "")).split())
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

    Shows the rules that became row boundaries (green, spanning only the columns
    they actually cross), the ones the width gate rejected (grey), each accepted
    row band, and the raw Docling cells a repair moved (red). This is what makes
    an abstention reviewable, so it is written on abstention too.
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
        accepted = {round(float(y), 6) for y in audit.get("boundaries") or []}
        for y, x0, x1 in geometry.get("rules") or []:
            if not (top <= y <= bottom):
                continue
            chosen = round(float(y), 6) in accepted
            draw.line(
                (max(x0, left) * width, y * height, min(x1, right) * width, y * height),
                fill="#00d084" if chosen else "#9aa0a6",
                width=3 if chosen else 1,
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

        label = (
            f"{candidate.candidate_id} {audit.get('status', '?')} "
            f"raw={audit.get('raw_body_rows', '?')} -> {audit.get('body_rows', '?')}"
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
