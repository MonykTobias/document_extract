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
    represented: set[str] = set()
    for record in records:
        if re.search(re.escape(f"]({record.rel_path})"), markdown):
            represented.add(record.rel_path)

    replacements = [
        image_block(record)
        for record in records
        if record.rel_path not in represented
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
        if record.rel_path in represented and record.summary:
            if record.summary.strip() not in out:
                reference = image_reference(record)
                out = out.replace(
                    reference,
                    image_block(record),
                    1,
                )
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
        if candidate.verified and candidate.markdown
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
    out: list[str] = []
    for candidate in table_candidates:
        if not (candidate.verified and candidate.markdown):
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
    "_rows_prefix_subset", "drop_duplicate_subset_tables",
    "missing_verified_table_ids", "pipe_row_count",
]
