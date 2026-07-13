"""Markdown serialization and structural formatting for page artifacts."""

from __future__ import annotations

import re
from typing import Any

from ..docling_adapter import (
    export_item_markdown,
    export_page_markdown_via_docling,
    infer_list_levels,
    is_heading_item,
    is_picture_item,
    is_table_item,
    item_kind,
    item_text,
    table_header_profile,
)
from ..layout.prompt_map import collapse_ws, VALUE_ONLY_RE
from ..models import PictureRecord, TableCandidate

IMAGE_PLACEHOLDER_RE = re.compile(
    r"\{\{DOC_IMAGE_[^}]+\}\}|<!--\s*image\s*-->|!\[[^\]]*\]\([^)]*\)",
    re.IGNORECASE,
)

SUMMARY_DUP_MIN_TOKENS = 12
SUMMARY_DUP_COVERAGE = 0.9

# A picture is right-edge navigation furniture when it sits in the right page
# band, was skipped as decorative/too small, carries no summary, and is not
# embedded in a table. These must not be force-appended as image references.
NAVIGATION_SKIP_REASONS = {"too_small", "triage_decorative", "triage_photo"}
RIGHT_EDGE_MIN_LEFT = 0.9


def _is_right_edge_navigation_image(record: PictureRecord) -> bool:
    rect = record.norm_rect
    if not rect or len(rect) < 4 or record.embedded_in == "table":
        return False
    if rect[0] < RIGHT_EDGE_MIN_LEFT:
        return False
    if record.summary.strip():
        return False
    return record.skip_reason in NAVIGATION_SKIP_REASONS or not record.summarize

def item_to_markdown(
    item: Any,
    document: Any,
    picture_records: dict[int, PictureRecord],
    list_levels: dict[int, int] | None = None,
) -> str:
    if is_picture_item(item):
        # Same placeholder as the Docling serializer path, so the refine
        # prompt's "keep <!-- image --> markers" contract holds either way.
        return "<!-- image -->" if picture_records.get(id(item)) else ""
    if is_table_item(item):
        markdown = export_item_markdown(item, document)
        return markdown or item_text(item)
    text = item_text(item)
    if not text:
        return ""
    if is_heading_item(item):
        level = heading_level(item)
        return f"{'#' * level} {text}"
    if item_kind(item) in {"list_item", "listitem"}:
        level = (list_levels or {}).get(id(item), 0)
        return f"{'  ' * level}- {text}"
    return text


def heading_level(item: Any) -> int:
    level = getattr(item, "level", None)
    try:
        if level:
            level = int(level)
            # Docling's serializer renders a level-1 section_header as "##".
            if item_kind(item) == "section_header":
                return min(max(level + 1, 2), 6)
            return min(max(level, 1), 6)
    except Exception:
        pass
    if "title" in type(item).__name__.lower() or item_kind(item) == "title":
        return 1
    return 2


def _list_line_text(line: str) -> str:
    stripped = line.lstrip()
    match = re.match(r"^(?:[-*+]\s+)(.*)$", stripped)
    return collapse_ws(match.group(1)) if match else ""


def _apply_list_level_specs(
    markdown: str, specs: list[tuple[str, int]]
) -> str:
    if not specs:
        return markdown
    lines = markdown.splitlines()
    used: set[int] = set()
    for index, line in enumerate(lines):
        content = _list_line_text(line)
        if not content:
            continue
        exact = [
            spec_index
            for spec_index, (text, _) in enumerate(specs)
            if spec_index not in used and content == collapse_ws(text)
        ]
        if len(exact) != 1:
            continue
        spec_index = exact[0]
        used.add(spec_index)
        level = max(0, min(int(specs[spec_index][1]), 2))
        lines[index] = f"{'  ' * level}- {content}"
    return "\n".join(lines) + ("\n" if markdown.endswith("\n") else "")


def apply_list_levels_to_markdown(
    markdown: str, items: list[Any], list_levels: dict[int, int]
) -> str:
    specs = [
        (item_text(item), list_levels.get(id(item), 0))
        for item in items
        if item_kind(item) in {"list_item", "listitem"} and item_text(item)
    ]
    return _apply_list_level_specs(markdown, specs)


def apply_list_levels_from_layout(
    markdown: str, layout_map: dict[str, Any] | None
) -> str:
    specs = [
        (str(block.get("text") or ""), int(block.get("list_level", 0)))
        for block in (layout_map or {}).get("blocks", [])
        if block.get("type") == "list_item" and block.get("text")
    ]
    return _apply_list_level_specs(markdown, specs)


def normalize_headerless_pipe_tables(
    markdown: str, first_rows: list[tuple[str, ...]]
) -> str:
    """Add an empty Markdown header to confirmed headerless tables."""
    wanted = {
        tuple(collapse_ws(cell).lower() for cell in row)
        for row in first_rows
        if row
    }
    if not wanted:
        return markdown

    lines = markdown.splitlines()
    for start, end, rows in reversed(_pipe_table_spans(lines)):
        if not rows or tuple(rows[0]) not in wanted:
            continue
        raw_lines = lines[start:end]
        data_lines = [
            line
            for line in raw_lines
            if not set(line.strip()) <= set("|-: ")
        ]
        width = max(
            len(line.strip().strip("|").split("|")) for line in data_lines
        )
        blank = "| " + " | ".join([""] * width) + " |"
        separator = "|" + "---|" * width
        lines[start:end] = [blank, separator, *data_lines]
    return "\n".join(lines) + ("\n" if markdown.endswith("\n") else "")


def export_page_markdown(
    document: Any,
    page_number: int,
    items: list[Any],
    picture_records: dict[int, PictureRecord],
    *,
    use_docling_order: bool = True,
) -> str:
    list_levels = infer_list_levels(items)
    headerless_rows = [
        tuple(profile["first_row"])
        for item in items
        for profile in [table_header_profile(item, document)]
        if profile["headerless"] and profile["first_row"]
    ]
    # Docling's serializer emits its own reading order; when the divider-aware
    # pass reordered the items, serialize from the reordered list instead.
    if use_docling_order:
        docling_markdown = export_page_markdown_via_docling(document, page_number)
        if docling_markdown:
            docling_markdown = apply_list_levels_to_markdown(
                docling_markdown, items, list_levels
            )
            return normalize_headerless_pipe_tables(docling_markdown, headerless_rows)

    parts: list[str] = []
    for item in items:
        markdown = item_to_markdown(item, document, picture_records, list_levels)
        if markdown:
            parts.append(markdown.rstrip())
    return "\n\n".join(parts).strip() + "\n"


def image_reference(record: PictureRecord) -> str:
    alt = f"Picture p{record.page:04d}-i{record.index:03d}"
    return f"![{alt}]({record.rel_path})"


def insert_image_references_and_summaries(
    markdown: str, records: list[PictureRecord]
) -> str:
    def symbol_in_table(record: PictureRecord, text: str) -> bool:
        value = record.summary.strip()
        return bool(value) and any(
            line.strip().startswith("|") and value in line
            for line in text.splitlines()
        )

    represented: set[str] = set()
    for record in records:
        if re.search(re.escape(f"]({record.rel_path})"), markdown):
            represented.add(record.rel_path)

    replacements = [
        # A table symbol belongs in a table cell only: never emit its value as a
        # standalone line. If placement succeeded it is already in a `|` row; if
        # not, it surfaces via the table_symbols_unplaced warning + repair.
        "" if record.summary_type == "symbol"
        else image_block(record)
        for record in records
        if record.rel_path not in represented
        # Right-edge decorative/too-small navigation marks have no placeholder
        # left in the refined body (the model drops them as furniture); never
        # resurrect them as trailing image references.
        and not _is_right_edge_navigation_image(record)
    ]

    def replace_match(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.lstrip().startswith("!["):
            return token
        if not replacements:
            return ""
        return replacements.pop(0)

    out = IMAGE_PLACEHOLDER_RE.sub(replace_match, markdown)
    for record in records:
        reference = image_reference(record)
        if record.rel_path in represented and record.summary_type == "symbol":
            # Drop the image reference for a symbol: its value lives in a table
            # cell when placed, otherwise it is left unplaced (never a loose line).
            out = out.replace(reference, "", 1)
        elif record.rel_path in represented and record.summary:
            if record.summary.strip() not in out:
                out = out.replace(reference, image_block(record), 1)
    if replacements:
        out = out.rstrip() + "\n\n" + "\n\n".join(replacements) + "\n"
    return re.sub(r"\n{3,}", "\n\n", out).strip() + "\n"


def _overlap_tokens(text: str) -> list[str]:
    """Words plus numbers-with-separators, so '13,158' and '4.5%' stay whole."""
    return re.findall(r"[^\W\d_]+|\d+(?:[.,]\d+)*%?", text.lower())


def mark_redundant_summaries(source_markdown: str, records: list[PictureRecord]) -> list[str]:
    """Flag summaries whose tokens are already covered by the raw page text.

    Returns the placeholders of newly flagged records (for the page warning).
    """
    page_tokens = set(_overlap_tokens(source_markdown))
    newly_flagged: list[str] = []
    for record in records:
        if not record.summary or record.summary_redundant:
            continue
        summary_tokens = _overlap_tokens(record.summary)
        if len(summary_tokens) < SUMMARY_DUP_MIN_TOKENS:
            continue
        distinct = set(summary_tokens)
        coverage = len(distinct & page_tokens) / len(distinct)
        if coverage >= SUMMARY_DUP_COVERAGE:
            record.summary_redundant = True
            if "summary_duplicates_page_text" not in record.summary_warnings:
                record.summary_warnings.append("summary_duplicates_page_text")
            newly_flagged.append(record.placeholder)
    return newly_flagged


def image_block(record: PictureRecord) -> str:
    if record.summary_type == "symbol":
        return record.summary.strip()
    block = image_reference(record)
    if record.summary and not record.summary_redundant:
        block += f"\n\n**Image summary:** {record.summary.strip()}"
    return block


def standalone_value_line_count(markdown: str) -> int:
    count = 0
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        if VALUE_ONLY_RE.match(line):
            count += 1
    return count


def normalize_pipe_tables(markdown: str) -> str:
    """Deterministically repair pipe tables the model emitted malformed.

    Fixes separator rows whose column count differs from the data rows
    (which breaks rendering), inserts a missing separator after the header,
    drops stray extra separators, and pads ragged rows to the table width.
    Cell contents are never changed.
    """
    lines = markdown.splitlines()
    out: list[str] = []
    block: list[str] = []

    def flush() -> None:
        if not block:
            return
        rows = [
            [cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in block
            if not set(line.strip()) <= set("|-: ")
        ]
        if len(rows) < 2:
            out.extend(block)
            block.clear()
            return
        width = max(len(row) for row in rows)
        padded = [row + [""] * (width - len(row)) for row in rows]
        out.append("| " + " | ".join(padded[0]) + " |")
        out.append("|" + "---|" * width)
        out.extend("| " + " | ".join(row) + " |" for row in padded[1:])
        block.clear()

    for line in lines:
        if line.strip().startswith("|"):
            block.append(line)
        else:
            flush()
            out.append(line)
    flush()
    return "\n".join(out) + ("\n" if markdown.endswith("\n") else "")


# --------------------------------------------------------------------------- #
# Pseudo-table unwrapping (F4): ruled two-column *layout* pages (Description /
# Management measures risk spreads) that TableFormer serialized as data tables.
# --------------------------------------------------------------------------- #

_SEPARATOR_ROW_RE = re.compile(r"^\|?[\s:|-]+\|?$")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
# The known layout-table header pair; matched case-insensitively.
_LAYOUT_HEADER_PAIR = ("description", "management measures")
# Trigger thresholds: prose in disguise, not data.
_LAYOUT_CELL_MAX_CHARS = 200
_LAYOUT_MEAN_CELL_CHARS = 120
# A table with this many numeric cells is a data table however long its cells.
_NUMERIC_CELL_GUARD = 3
# Layout tables collapse a page into 1-2 giant rows; a long-celled table with
# MANY rows is a real categorical data table (page 18's competitor table).
_LAYOUT_MAX_BODY_ROWS = 3


def _split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_separator_line(line: str) -> bool:
    stripped = line.strip()
    # Requires a dash/colon: an all-blank row ("|  |  |") is a row, not a rule.
    return (
        stripped.startswith("|")
        and set(stripped) <= set("|-: ")
        and any(ch in "-:" for ch in stripped)
    )


def _raw_pipe_blocks(lines: list[str]) -> list[tuple[int, int]]:
    """(start, end_exclusive) spans of consecutive ``|``-prefixed lines."""
    blocks: list[tuple[int, int]] = []
    start: int | None = None
    for index, line in enumerate(lines + [""]):
        if line.strip().startswith("|"):
            if start is None:
                start = index
        elif start is not None:
            blocks.append((start, index))
            start = None
    return blocks


def _is_layout_header(cells: list[str]) -> bool:
    non_empty = [collapse_ws(cell).lower() for cell in cells if cell.strip()]
    return tuple(non_empty) == _LAYOUT_HEADER_PAIR


def _numeric_cell(cell: str) -> bool:
    text = cell.strip()
    if not text or not any(ch.isdigit() for ch in text):
        return False
    alpha = sum(ch.isalpha() for ch in text)
    return len(text) <= 20 and alpha <= 4


def drop_orphan_header_tables(markdown: str) -> tuple[str, list[str]]:
    """Remove header-only pipe tables with no content.

    Docling emits the ruled risk-spread layout as a table; on some pages only
    the header survives: ``| Description | Management measures |`` plus a
    separator and zero body rows (pages 29/33), or even a lone header line
    with no separator at all (page 31). All-empty headers count too.

    Returns the cleaned markdown and the dropped header texts (cells joined),
    so the completeness guard can be told not to re-append them as missing.
    """
    lines = markdown.splitlines()
    drop: set[int] = set()
    dropped_texts: list[str] = []
    for start, end in _raw_pipe_blocks(lines):
        rows = [
            _split_row(line)
            for line in lines[start:end]
            if not _is_separator_line(line)
        ]
        if len(rows) != 1:
            continue
        cells = rows[0]
        if all(not cell for cell in cells) or _is_layout_header(cells):
            drop.update(range(start, end))
            dropped_texts.append(" ".join(cell for cell in cells if cell))
    if not drop:
        return markdown, []
    kept = "\n".join(line for index, line in enumerate(lines) if index not in drop)
    return re.sub(r"\n{3,}", "\n\n", kept).strip() + "\n", dropped_texts


def _unwrap_cell(cell: str) -> list[str]:
    """One table cell -> markdown lines (paragraphs / list items)."""
    out: list[str] = []
    for fragment in _BR_RE.split(cell):
        fragment = fragment.replace("\\|", "|").strip()
        if not fragment:
            continue
        if fragment.startswith("- "):
            out.append(fragment)
        else:
            out.append(fragment)
            out.append("")
    while out and not out[-1]:
        out.pop()
    return out


def unwrap_layout_tables(markdown: str) -> tuple[str, int]:
    """Turn pipe tables that are prose in disguise back into flowing markdown.

    Trigger: <= 3 columns AND (a cell over 200 chars, a ``<br>`` inside a
    cell, or mean non-empty cell length over 120). Guard: >= 3 numeric cells
    means a real data table (long footnote cells included), never unwrapped.

    With a distinct-label header (``Description`` / ``Management measures``),
    each column becomes a ``**<label>**`` block followed by that column's
    cells in row order; without one, cells flow row by row.
    """
    lines = markdown.splitlines()
    unwrapped = 0
    for start, end in reversed(_raw_pipe_blocks(lines)):
        block_lines = lines[start:end]
        has_separator = any(_is_separator_line(line) for line in block_lines)
        rows = [
            _split_row(line) for line in block_lines if not _is_separator_line(line)
        ]
        if not rows:
            continue
        n_cols = max(len(row) for row in rows)
        if n_cols > 3:
            continue
        cells = [cell for row in rows for cell in row if cell.strip()]
        if not cells:
            continue
        if sum(_numeric_cell(cell) for cell in cells) >= _NUMERIC_CELL_GUARD:
            continue
        if len(rows) - (1 if has_separator else 0) > _LAYOUT_MAX_BODY_ROWS:
            continue
        max_len = max(len(cell) for cell in cells)
        mean_len = sum(len(cell) for cell in cells) / len(cells)
        has_br = any(_BR_RE.search(cell) for cell in cells)
        if not (max_len > _LAYOUT_CELL_MAX_CHARS or has_br or mean_len > _LAYOUT_MEAN_CELL_CHARS):
            continue

        header = rows[0] if has_separator else []
        header_labels = [collapse_ws(cell) for cell in header]
        distinct = [label for label in header_labels if label]
        usable_header = (
            bool(distinct)
            and len(set(label.lower() for label in distinct)) == len(distinct)
            and all(len(label) <= 60 for label in distinct)
            and not any(_numeric_cell(label) for label in distinct)
        )
        body = rows[1:] if has_separator else rows

        out: list[str] = []
        if usable_header:
            for column, label in enumerate(header_labels):
                if not label:
                    continue
                out.append(f"**{label}**")
                out.append("")
                for row in body:
                    if column < len(row) and row[column].strip():
                        out.extend(_unwrap_cell(row[column]))
                        out.append("")
        else:
            for row in body:
                for cell in row:
                    if cell.strip():
                        out.extend(_unwrap_cell(cell))
                        out.append("")
        while out and not out[-1]:
            out.pop()
        lines[start:end] = out
        unwrapped += 1
    if not unwrapped:
        return markdown, 0
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text + "\n", unwrapped


def strip_br_lines(markdown: str) -> str:
    """Remove ``<br>`` artifacts from non-table lines.

    The repair model spills ``<br>``-joined fragments outside tables
    (page 27): trailing ``<br>`` tokens are dropped and inline ones become
    line breaks. Table rows are left alone — ``unwrap_layout_tables`` decides
    their fate.
    """
    out: list[str] = []
    for line in markdown.splitlines():
        if line.lstrip().startswith("|") or not _BR_RE.search(line):
            out.append(line)
            continue
        for fragment in _BR_RE.split(line):
            if fragment.strip():
                out.append(fragment.rstrip())
    return "\n".join(out) + ("\n" if markdown.endswith("\n") else "")


def drop_empty_header_row(markdown: str) -> tuple[str, int]:
    """Remove pipe tables that contain no content at all.

    Note: a *blank header + separator + real rows* table (page 23) is the
    pipeline's canonical headerless form — ``normalize_headerless_pipe_tables``
    produces it and the repair prompt protects it — so only tables whose every
    cell is empty are dropped.
    """
    lines = markdown.splitlines()
    drop: set[int] = set()
    dropped = 0
    for start, end in _raw_pipe_blocks(lines):
        rows = [
            _split_row(line)
            for line in lines[start:end]
            if not _is_separator_line(line)
        ]
        if rows and all(not cell for row in rows for cell in row):
            drop.update(range(start, end))
            dropped += 1
    if not drop:
        return markdown, 0
    kept = "\n".join(line for index, line in enumerate(lines) if index not in drop)
    return re.sub(r"\n{3,}", "\n\n", kept).strip() + "\n", dropped


def _pipe_table_spans(
    lines: list[str],
) -> list[tuple[int, int, list[tuple[str, ...]]]]:
    """(start, end_exclusive, data_rows) for each pipe table; rows normalized."""
    spans: list[tuple[int, int, list[tuple[str, ...]]]] = []
    start: int | None = None
    rows: list[tuple[str, ...]] = []
    for index, line in enumerate(lines + [""]):
        stripped = line.strip()
        if stripped.startswith("|"):
            if start is None:
                start = index
            if not set(stripped) <= set("|-: "):
                cells = tuple(
                    collapse_ws(cell).lower() for cell in stripped.strip("|").split("|")
                )
                rows.append(cells)
        elif start is not None:
            spans.append((start, index, rows))
            start, rows = None, []
    return spans


def _rows_prefix_subset(
    subset_rows: list[tuple[str, ...]], superset_rows: list[tuple[str, ...]]
) -> bool:
    """True when (nearly) every subset row is a column-prefix of a superset row."""
    if len(subset_rows) < 2:
        return False
    hits = sum(
        1
        for row in subset_rows
        if any(row == other[: len(row)] for other in superset_rows)
    )
    return hits >= 0.8 * len(subset_rows)


def _apply_span_edits(
    lines: list[str], edits: list[tuple[int, int, list[str] | None]]
) -> str:
    """Apply (start, end, replacement|None-to-delete) span edits over ``lines``."""
    for start, end, replacement in sorted(edits, key=lambda edit: edit[0], reverse=True):
        lines[start:end] = replacement if replacement is not None else []
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def _sectioned_candidate_rows(candidate: TableCandidate) -> set[tuple[str, ...]]:
    return {
        row
        for _, _, rows in _pipe_table_spans(candidate.markdown.splitlines())
        for row in rows
    }


def _matches_section_title(text: str, titles: set[str]) -> bool:
    """A heading text that is a section title, optionally with a parenthetical
    qualifier appended by the renderer ("OPERATIONS … (in € millions)")."""
    normalized = collapse_ws(text).lower()
    return any(
        normalized == title or normalized.startswith(title + " (")
        for title in titles
    )


def _sectioned_base_level(lines: list[str], pos: int, titles: set[str]) -> int:
    """Heading depth for a sectioned table's own headings: one deeper than the
    nearest preceding heading that is not itself one of the table's sections."""
    for index in range(pos - 1, -1, -1):
        match = re.match(r"^(#{1,6})\s+(.+)$", lines[index].strip())
        if match and not _matches_section_title(match.group(2), titles):
            return len(match.group(1)) + 1
    return 3


def _relevel_sectioned_markdown(
    markdown: str, kinds: list[str], base_level: int
) -> list[str]:
    """Rewrite the heading levels of a rendered sectioned table so all sibling
    sub-categories share one depth: group (parent) headers at ``base_level``,
    data sections one deeper when any group header exists."""
    group_level = base_level
    data_level = base_level + 1 if "group" in kinds else base_level
    out: list[str] = []
    kind_index = 0
    for line in markdown.splitlines():
        match = re.match(r"^#{1,6}\s+(.*)$", line)
        if match and kind_index < len(kinds):
            level = group_level if kinds[kind_index] == "group" else data_level
            out.append("#" * max(1, min(level, 6)) + " " + match.group(1))
            kind_index += 1
        else:
            out.append(line)
    return out


def _enforce_sectioned_candidate(
    markdown: str, candidate: TableCandidate
) -> str | None:
    """Guarantee one sectioned candidate appears as its subtables; None = no change.

    Finds the contiguous span of page content belonging to the candidate — its
    subtable/twin pipe blocks plus any `### Section` headings, `- label:` list
    remnants and qualifier lines the VLM emitted — and replaces the whole span
    with the authoritative split, re-leveled so sibling section headings are
    consistent. Falls back to surgical splicing when unrelated content is
    interleaved, so nothing outside the table is disturbed.
    """
    candidate_rows = _sectioned_candidate_rows(candidate)
    if not candidate_rows:
        return None
    stats = candidate.stats or {}
    titles = {collapse_ws(title).lower() for title in stats.get("section_titles", [])}
    quals = {collapse_ws(q).lower() for q in stats.get("section_qualifiers", [])}
    kinds = list(stats.get("section_kinds", []))
    label_cells = {row[0] for row in candidate_rows if row and row[0]}
    lines = markdown.splitlines()

    anchor_pipe: set[int] = set()
    for start, end, rows in _pipe_table_spans(lines):
        if rows and sum(1 for row in rows if row in candidate_rows) / len(rows) >= 0.5:
            anchor_pipe.update(range(start, end))

    def anchor_of(index: int) -> bool | None:
        stripped = lines[index].strip()
        if not stripped:
            return None  # blank line: neutral, may sit inside the region
        if index in anchor_pipe:
            return True
        heading = re.match(r"^#{1,6}\s+(.+)$", stripped)
        if heading and _matches_section_title(heading.group(1), titles):
            return True
        listed = re.match(r"^[-*]\s+(.+)$", stripped)
        if listed:
            content = listed.group(1)
            label = content.split(":", 1)[0] if ":" in content else content
            if collapse_ws(label).lower() in label_cells:
                return True
        if collapse_ws(stripped).lower() in quals:
            return True
        return False

    flags = [anchor_of(index) for index in range(len(lines))]
    anchors = [index for index, flag in enumerate(flags) if flag is True]
    if not anchors:
        base = _sectioned_base_level(lines, len(lines), titles)
        leveled = _relevel_sectioned_markdown(candidate.markdown, kinds, base)
        return _apply_span_edits(lines, [(len(lines), len(lines), ["", *leveled])])

    lo, hi = min(anchors), max(anchors)
    base = _sectioned_base_level(lines, lo, titles)
    leveled = _relevel_sectioned_markdown(candidate.markdown, kinds, base)
    if all(flags[index] is not False for index in range(lo, hi + 1)):
        # Clean region: nothing but the table's own headings/rows/remnants.
        return _apply_span_edits(lines, [(lo, hi + 1, ["", *leveled, ""])])
    # Unrelated content is interleaved: remove only the anchor lines and splice
    # the authoritative split at the first of them, leaving everything else.
    out: list[str] = []
    inserted = False
    for index, line in enumerate(lines):
        if lo <= index <= hi and flags[index] is True:
            if not inserted:
                out.extend(["", *leveled, ""])
                inserted = True
            continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


def replace_sectioned_tables(
    markdown: str, table_candidates: list[TableCandidate] | None
) -> tuple[str, list[str]]:
    """Ensure every verified sectioned-table candidate appears as its subtables.

    Runs before ``drop_duplicate_subset_tables`` (so un-split twins are removed
    before that pass could mistake a subtable for their prefix-subset) and
    before the completeness guard (so the split table's header row never lands
    in ``## Unplaced content``). Returns (markdown, enforced_candidate_ids).
    """
    enforced: list[str] = []
    for candidate in table_candidates or []:
        if not (candidate.verified and candidate.markdown):
            continue
        if (candidate.stats or {}).get("format") != "sectioned_table":
            continue
        updated = _enforce_sectioned_candidate(markdown, candidate)
        if updated is not None and updated != markdown:
            markdown = updated
            enforced.append(candidate.candidate_id)
    return markdown, enforced


# Formats produced only by the transcription-time grid renderer
# (render_deterministic_docling_table); sectioned tables have their own path.
_DETERMINISTIC_FORMATS = {"regular_table", "title_detail_table"}


def _deterministic_anchor_texts(candidate: TableCandidate) -> set[str]:
    """Normalized cell texts that mark this deterministic table in the markdown.

    Two sources so the table is found however it survived: the raw Docling grid
    cells (what ``--skip-vlm`` leaves in the source markdown as the unmerged
    twin) and the candidate's own rendered rows, including the atomic pieces of a
    merged ``title<br>detail`` cell (what a degraded VLM re-emits as a heading or
    list item).
    """
    texts: set[str] = set()
    grid = (candidate.stats or {}).get("grid") or {}
    for row in grid.get("rows") or []:
        for cell in row:
            value = collapse_ws(str(cell)).lower()
            if value:
                texts.add(value)
    for _, _, rows in _pipe_table_spans(candidate.markdown.splitlines()):
        for row in rows:
            for cell in row:
                if not cell:
                    continue
                texts.add(cell)
                for piece in _BR_RE.split(cell):
                    fragment = re.sub(r"^-\s+", "", piece).strip()
                    if fragment:
                        texts.add(fragment)
    return texts


def _enforce_deterministic_candidate(
    markdown: str, candidate: TableCandidate
) -> str | None:
    """Splice one deterministic docling table (regular/title_detail) in verbatim.

    Locates the candidate's raw Docling twin — or a VLM-degraded remnant of it
    (a title turned into a heading, rows spilled into a list) — by cell overlap
    and replaces that contiguous region with the authoritative rendered table.
    Returns ``None`` when no trace of the table is found: genuine loss is left to
    the completeness guard / repair pass and never appended, so a deterministic
    table is never duplicated.
    """
    texts = _deterministic_anchor_texts(candidate)
    if not texts:
        return None
    lines = markdown.splitlines()

    anchor_pipe: set[int] = set()
    for start, end, rows in _pipe_table_spans(lines):
        cells = [cell for row in rows for cell in row if cell]
        if not cells:
            continue
        matched = sum(1 for cell in cells if cell in texts)
        if matched >= 2 and matched >= 0.6 * len(cells):
            anchor_pipe.update(range(start, end))

    def anchor_of(index: int) -> bool | None:
        stripped = lines[index].strip()
        if not stripped:
            return None  # blank line: neutral, may sit inside the region
        if index in anchor_pipe:
            return True
        heading = re.match(r"^#{1,6}\s+(.+)$", stripped)
        if heading and collapse_ws(heading.group(1)).lower() in texts:
            return True
        listed = re.match(r"^[-*]\s+(.+)$", stripped)
        if listed:
            content = listed.group(1)
            label = content.split(":", 1)[0] if ":" in content else content
            if collapse_ws(label).lower() in texts:
                return True
        return False

    flags = [anchor_of(index) for index in range(len(lines))]
    anchors = [index for index, flag in enumerate(flags) if flag is True]
    if not anchors:
        return None
    lo, hi = min(anchors), max(anchors)
    table_lines = candidate.markdown.rstrip("\n").splitlines()
    if all(flags[index] is not False for index in range(lo, hi + 1)):
        # Clean region: nothing but the table's own rows/degraded remnants.
        return _apply_span_edits(lines, [(lo, hi + 1, ["", *table_lines, ""])])
    # Unrelated content is interleaved: drop only the anchor lines and splice the
    # authoritative table at the first of them, leaving everything else in place.
    out: list[str] = []
    inserted = False
    for index, line in enumerate(lines):
        if lo <= index <= hi and flags[index] is True:
            if not inserted:
                out.extend(["", *table_lines, ""])
                inserted = True
            continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


def replace_deterministic_tables(
    markdown: str, table_candidates: list[TableCandidate] | None
) -> tuple[str, list[str]]:
    """Ensure every deterministically rendered regular/title_detail table lands verbatim.

    Runs in the same slot as ``replace_sectioned_tables`` (before
    ``drop_duplicate_subset_tables`` and the completeness guard). These tables
    are rendered from the Docling grid at transcription time; without this the
    merged table only reaches the refine VLM prompt, so under ``--skip-vlm`` (or
    when the VLM degrades it) the raw twin would survive in its place. Returns
    (markdown, enforced_candidate_ids).
    """
    enforced: list[str] = []
    for candidate in table_candidates or []:
        if not (candidate.verified and candidate.markdown):
            continue
        if (candidate.stats or {}).get("format") not in _DETERMINISTIC_FORMATS:
            continue
        updated = _enforce_deterministic_candidate(markdown, candidate)
        if updated is not None and updated != markdown:
            markdown = updated
            enforced.append(candidate.candidate_id)
    return markdown, enforced


def drop_duplicate_subset_tables(
    markdown: str, table_candidates: list[TableCandidate] | None
) -> tuple[str, int]:
    """Remove pipe tables that duplicate a verified table with fewer columns/rows.

    The refine model sometimes serializes a region itself AND places the
    injected pre-verified table, yielding e.g. a GOALS|TARGETS twin of the
    GOALS|TARGETS|RESULTS table. Only tables that are column-prefix subsets of
    a verified-backed table are removed, so unrelated tables are never touched.
    """
    verified_rowsets = [
        set(rows)
        for candidate in table_candidates or []
        if candidate.verified
        and candidate.markdown
        and (candidate.stats or {}).get("format") != "kpi_list"
        for rows in [
            [
                row
                for _, _, table_rows in _pipe_table_spans(candidate.markdown.splitlines())
                for row in table_rows
            ]
        ]
        if rows
    ]
    if not verified_rowsets:
        return markdown, 0

    lines = markdown.splitlines()
    tables = _pipe_table_spans(lines)
    if len(tables) < 2:
        return markdown, 0

    def is_backed(rows: list[tuple[str, ...]]) -> bool:
        row_set = set(rows)
        return any(
            len(row_set & verified_rows) >= 0.5 * len(verified_rows)
            for verified_rows in verified_rowsets
        )

    backed = [is_backed(rows) for _, _, rows in tables]
    drop: set[int] = set()
    for i, (_, _, rows_i) in enumerate(tables):
        if i in drop:
            continue
        for j, (_, _, rows_j) in enumerate(tables):
            if i == j or j in drop or not backed[j]:
                continue
            if _rows_prefix_subset(rows_i, rows_j):
                drop.add(i)
                break
    if not drop:
        return markdown, 0

    dropped_lines = {
        index for table_index in drop for index in range(*tables[table_index][:2])
    }
    kept = "\n".join(
        line for index, line in enumerate(lines) if index not in dropped_lines
    )
    return re.sub(r"\n{3,}", "\n\n", kept).strip() + "\n", len(drop)


def missing_verified_table_ids(
    current_markdown: str, table_candidates: list[TableCandidate]
) -> list[str]:
    """Ids of verified tables whose rows mostly did not survive into the markdown."""
    present_rows = {
        collapse_ws(line)
        for line in current_markdown.splitlines()
        if line.strip().startswith("|")
    }
    present_kpi_lines = {
        collapse_ws(line).lower()
        for line in current_markdown.splitlines()
        if re.match(r"^\s*-\s+[^:|]{2,80}:\s*\S", line)
    }
    out: list[str] = []
    for candidate in table_candidates:
        if not (candidate.verified and candidate.markdown):
            continue
        if (candidate.stats or {}).get("format") == "kpi_list":
            kpi_lines = [
                collapse_ws(line).lower()
                for line in candidate.markdown.splitlines()
                if re.match(r"^\s*-\s+[^:|]{2,80}:\s*\S", line)
            ]
            hits = sum(line in present_kpi_lines for line in kpi_lines)
            if not kpi_lines or hits < 0.6 * len(kpi_lines):
                out.append(candidate.candidate_id)
            continue
        rows = [
            collapse_ws(line)
            for line in candidate.markdown.splitlines()
            if line.strip().startswith("|") and not set(line.strip()) <= set("|-: ")
        ]
        if not rows:
            continue
        hits = sum(1 for row in rows if row in present_rows)
        if hits < 0.5 * len(rows):
            out.append(candidate.candidate_id)
    return out


def pipe_row_count(markdown: str) -> int:
    return sum(
        1
        for line in markdown.splitlines()
        if line.strip().startswith("|") and not set(line.strip()) <= set("|-: ")
    )

__all__ = [
    "IMAGE_PLACEHOLDER_RE", "item_to_markdown", "heading_level",
    "export_page_markdown", "apply_list_levels_to_markdown",
    "apply_list_levels_from_layout", "normalize_headerless_pipe_tables",
    "image_reference",
    "insert_image_references_and_summaries", "image_block",
    "mark_redundant_summaries",
    "standalone_value_line_count", "normalize_pipe_tables", "_pipe_table_spans",
    "drop_orphan_header_tables", "unwrap_layout_tables", "strip_br_lines",
    "drop_empty_header_row",
    "_rows_prefix_subset", "drop_duplicate_subset_tables", "replace_sectioned_tables",
    "replace_deterministic_tables",
    "missing_verified_table_ids", "pipe_row_count",
]
