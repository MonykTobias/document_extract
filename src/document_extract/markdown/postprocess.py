"""Deterministic (no-model) post-processing for extracted page markdown.

All functions here are pure and depend only on the standard library so the
pipeline stages and any external metrics harness can import them without
pulling in Docling/fitz/PIL.

They implement Part 2.3 / 2.5 of the improvement plan:

- ``flatten_html_tables``       HTML ``<table>`` -> pipe table with rowspan/colspan
                                expanded by repeating group labels (better for RAG).
- ``normalize_bullets_and_headings``  ``•``/``●`` -> ``- ``; split inline bullets;
                                tidy heading spacing.
- ``extract_uncertainty``       pull the ``## Uncertain mappings`` sentinel block out to
                                a sidecar so it never reaches the final markdown.
- ``footnote_consistency``      warn when body markers and definitions disagree.
- ``completeness_diff``         OCR content lines that went missing from the refined body.
- ``meta_commentary_warnings`` / ``strip_meta_commentary``  process-commentary leaks.
- ``detect_repeated_lines``     decoding-loop / repetition signal.
"""

from __future__ import annotations

import difflib
import re
from html.parser import HTMLParser

# --------------------------------------------------------------------------- #
# Shared regexes / constants
# --------------------------------------------------------------------------- #

BULLET_CHARS = "•●▪◦‣·∙"
# Table cells need a slightly different bullet policy from prose. Strong glyphs
# can form an inline list anywhere in a cell; weak middle dots only form a list
# when they lead the cell, so values such as ``12·000`` stay literal.
GRID_BULLET_CHARS_STRONG = "•●▪◦‣■"
GRID_BULLET_CHARS_WEAK = "·∙"
GRID_BULLET_CHARS = GRID_BULLET_CHARS_STRONG + GRID_BULLET_CHARS_WEAK
_GRID_STRONG_SPLIT_RE = re.compile(rf"[{GRID_BULLET_CHARS_STRONG}]")
_GRID_BULLET_SPLIT_RE = re.compile(rf"[{GRID_BULLET_CHARS}]")
# Unicode bullet at line start, optionally after whitespace.
_LEADING_BULLET_RE = re.compile(rf"^\s*[{BULLET_CHARS}]\s*")
# Inline bullet used as an item separator (has content on both sides).
_INLINE_BULLET_RE = re.compile(rf"\s*[{BULLET_CHARS}]\s+")
# LaTeX-style footnote marker emitted by PaddleOCR: ``$ ^{[1]} $`` / ``$^{[1]}$``.
_LATEX_MARKER_RE = re.compile(r"\$\s*\^\{\s*([^}]*?)\s*\}\s*\$")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_TABLE_BLOCK_RE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_UNCERTAINTY_TITLE_RE = re.compile(r"^\s*#{1,6}\s*uncertain", re.IGNORECASE)
_REDUNDANT_LIST_GLYPH_RE = re.compile(
    rf"^(\s*)-\s+([{GRID_BULLET_CHARS_STRONG}])\s+(\S.*)$"
)

# KPI panels: display values + all-caps captions are not data tables. Keep the
# classifier here because postprocessing must stay stdlib-only.
KPI_MAX_ROWS = 8
KPI_MAX_COLS = 6
KPI_MIN_VALUE_CELLS = 3
KPI_MIN_LABEL_CELLS = 2
KPI_MAX_CELL_CHARS = 120
KPI_MIN_SIGNAL_RATIO = 0.6
_KPI_VALUE_TEXT_RE = re.compile(
    r"^\s*(?:[~≈<>]\s*)?(?:[+-]\s*)?(?:[$\u20ac\u00a3]\s*)?"
    r"\d[\d,.\s]*(?:\s*(?:%|x|pts?|bps?|bn|m|k|stars?))?(?:\s*[$\u20ac\u00a3])?"
    r"\s*(?:\((?:[A-Za-z]|\d{1,2})\))?\s*$",
    re.IGNORECASE,
)
_KPI_PIPE_SEPARATOR_RE = re.compile(r"^\|?[\s:|-]+\|?$")
_KPI_RATING_RE = re.compile(r"^[A-Z][a-zA-Z]{0,3}[+-]?\d?$")
_KPI_RATING_WITH_FOOTNOTE_RE = re.compile(
    r"^([A-Z][a-zA-Z]{0,3}[+-]?\d?)\s*(\(\w{1,2}\))\s+(.+)$"
)
_KPI_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")

# Numeric footnote reference / definition, e.g. ``[1]``.
_FOOTNOTE_MARKER_RE = re.compile(r"\[(\d{1,3})\]")
_FOOTNOTE_DEF_LINE_RE = re.compile(r"^\s*\[(\d{1,3})\]\s+\S")


def _normalise_kpi_currency(text: str) -> str:
    """Repair common mojibake only for matching; emitted text stays verbatim."""
    return text.replace("\u00e2\u201a\u00ac", "\u20ac").replace("\u00c2\u00a3", "\u00a3")


def is_value_text(text: str) -> bool:
    """Whether ``text`` is a short standalone display value, including years."""
    stripped = text.strip()
    return len(stripped) <= 20 and bool(
        _KPI_VALUE_TEXT_RE.fullmatch(_normalise_kpi_currency(stripped))
    )


def _is_kpi_value_cell(text: str) -> bool:
    return is_value_text(text)


def is_kpi_label_text(text: str) -> bool:
    """Whether ``text`` is an all-caps-like KPI caption, not ordinary prose."""
    stripped = text.strip()
    alpha = [ch for ch in stripped if ch.isalpha()]
    words = re.findall(r"[^\W\d_]+", stripped, re.UNICODE)
    return (
        8 <= len(stripped) <= 90
        and len(words) >= 2
        and bool(alpha)
        and sum(ch.isupper() for ch in alpha) / len(alpha) >= 0.7
        and (not any(ch.isdigit() for ch in stripped) or len(words) >= 3)
    )


def _is_kpi_label_cell(text: str) -> bool:
    return is_kpi_label_text(text)


def is_letter_rating(text: str) -> bool:
    """Return True for short credit-rating tokens, never general all-caps text."""
    return bool(_KPI_RATING_RE.fullmatch(text.strip()))


def kpi_panel_stats(rows: list[list[str]]) -> dict[str, object]:
    """Return deterministic shape signals for a compact KPI display panel."""
    clean_rows = [[str(cell or "").strip() for cell in row] for row in rows]
    cells = [cell for row in clean_rows for cell in row if cell]
    value_flags = [[_is_kpi_value_cell(cell) for cell in row] for row in clean_rows]
    label_flags = [[_is_kpi_label_cell(cell) for cell in row] for row in clean_rows]
    value_rows = [
        bool([cell for cell in row if cell])
        and all(flag for cell, flag in zip(row, flags) if cell)
        for row, flags in zip(clean_rows, value_flags)
    ]
    label_rows = [
        bool([cell for cell in row if cell])
        and all(flag for cell, flag in zip(row, flags) if cell)
        for row, flags in zip(clean_rows, label_flags)
    ]
    alternating = any(
        (value_rows[index] and label_rows[index + 1])
        or (label_rows[index] and value_rows[index + 1])
        for index in range(max(0, len(clean_rows) - 1))
    )
    value_cells = sum(flag for row in value_flags for flag in row)
    label_cells = sum(flag for row in label_flags for flag in row)
    non_empty = len(cells)
    return {
        "n_rows": len(clean_rows),
        "n_cols": max((len(row) for row in clean_rows), default=0),
        "non_empty": non_empty,
        "value_cells": value_cells,
        "label_cells": label_cells,
        "long_cells": sum(len(cell) > KPI_MAX_CELL_CHARS for cell in cells),
        "value_rows": value_rows,
        "label_rows": label_rows,
        "alternating": alternating,
        "signal_ratio": round((value_cells + label_cells) / non_empty, 3)
        if non_empty
        else 0.0,
    }


def looks_like_kpi_panel(rows: list[list[str]]) -> tuple[bool, dict[str, object]]:
    """True only for small alternating display-value / caption grids."""
    stats = kpi_panel_stats(rows)
    is_kpi = (
        stats["n_rows"] <= KPI_MAX_ROWS
        and stats["n_cols"] <= KPI_MAX_COLS
        and stats["value_cells"] >= KPI_MIN_VALUE_CELLS
        and stats["label_cells"] >= KPI_MIN_LABEL_CELLS
        and stats["signal_ratio"] >= KPI_MIN_SIGNAL_RATIO
        and stats["long_cells"] == 0
        and stats["alternating"] is True
    )
    return is_kpi, stats


def _pipe_table_rows(block: list[str]) -> list[list[str]]:
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in block
        if not _KPI_PIPE_SEPARATOR_RE.fullmatch(line.strip())
    ]


def _kpi_list_lines(rows: list[list[str]], stats: dict[str, object]) -> list[str]:
    value_rows = list(stats["value_rows"])
    label_rows = list(stats["label_rows"])
    used: set[tuple[int, int]] = set()
    lines: list[str] = []
    row_index = 0
    while row_index < len(rows) - 1:
        top_is_value = bool(value_rows[row_index])
        bottom_is_value = bool(value_rows[row_index + 1])
        top_is_label = bool(label_rows[row_index])
        bottom_is_label = bool(label_rows[row_index + 1])
        if not ((top_is_value and bottom_is_label) or (top_is_label and bottom_is_value)):
            row_index += 1
            continue
        value_row = rows[row_index] if top_is_value else rows[row_index + 1]
        label_row = rows[row_index] if top_is_label else rows[row_index + 1]
        width = max(len(value_row), len(label_row))
        for column in range(width):
            value = value_row[column].strip() if column < len(value_row) else ""
            label = label_row[column].strip() if column < len(label_row) else ""
            if value and label:
                lines.append(f"- {label}: {value}")
                used.add((row_index, column))
                used.add((row_index + 1, column))
        row_index += 2

    non_empty = sum(1 for row in rows for cell in row if cell.strip())
    if len(used) < non_empty / 2:
        return [f"- {cell.strip()}" for row in rows for cell in row if cell.strip()]

    for row_index, row in enumerate(rows):
        for column, cell in enumerate(row):
            if cell.strip() and (row_index, column) not in used:
                lines.append(f"- {cell.strip()}")
    return lines


def convert_kpi_pipe_tables_to_lists(markdown: str) -> tuple[str, int]:
    """Replace KPI-display pipe tables with semantically paired list lines."""
    lines = markdown.splitlines()
    out: list[str] = []
    converted = 0
    index = 0
    while index < len(lines):
        if not lines[index].strip().startswith("|"):
            out.append(lines[index])
            index += 1
            continue
        end = index
        while end < len(lines) and lines[end].strip().startswith("|"):
            end += 1
        block = lines[index:end]
        rows = _pipe_table_rows(block)
        is_kpi, stats = looks_like_kpi_panel(rows)
        if is_kpi:
            out.extend(_kpi_list_lines(rows, stats))
            converted += 1
        else:
            out.extend(block)
        index = end
    return "\n".join(out) + ("\n" if markdown.endswith("\n") else ""), converted


def _split_mixed_kpi_line(text: str) -> tuple[str, str] | None:
    """Return ``(label, value)`` only for a complete same-line KPI pair."""
    stripped = text.strip()
    rating_match = _KPI_RATING_WITH_FOOTNOTE_RE.fullmatch(stripped)
    if rating_match and is_letter_rating(rating_match.group(1)):
        label = rating_match.group(3).strip()
        if is_kpi_label_text(label):
            return label, f"{rating_match.group(1)} {rating_match.group(2)}"

    words = stripped.split()
    # Longest-first prevents ``€2.8 bn`` from becoming value ``€2.8`` and
    # label ``bn FREE CASH FLOW``.
    for split_at in range(len(words) - 1, 0, -1):
        value = " ".join(words[:split_at])
        label = " ".join(words[split_at:])
        if is_value_text(value) and is_kpi_label_text(label):
            return label, value
    for split_at in range(len(words) - 1, 0, -1):
        label = " ".join(words[:split_at])
        value = " ".join(words[split_at:])
        if is_kpi_label_text(label) and is_value_text(value):
            return label, value
    return None


def _classify_kpi_text_line(text: str) -> tuple[str, tuple[str, str] | None]:
    """Classify a non-structural line as VALUE, LABEL, MIXED, or OTHER."""
    content = re.sub(r"^\s*-\s+", "", text).strip()
    if is_value_text(content):
        return "VALUE", None
    # A same-line value + label can also superficially look all-caps-like, so
    # split it before testing the whole line as a label.
    mixed = _split_mixed_kpi_line(content)
    if mixed:
        return "MIXED", mixed
    if is_kpi_label_text(content):
        return "LABEL", None
    return "OTHER", None


def _alternating_kpi_pairs(entries: list[dict[str, object]]) -> list[tuple[int, int]]:
    """Return a >=2-pair alternating VALUE/LABEL prefix, or no pairs."""
    prefix: list[dict[str, object]] = []
    for entry in entries:
        if entry["kind"] not in {"VALUE", "LABEL"}:
            break
        prefix.append(entry)
    if len(prefix) < 4:
        return []
    kinds = [str(entry["kind"]) for entry in prefix]
    if kinds[0] == kinds[1]:
        return []
    expected = (kinds[0], kinds[1])
    pairs: list[tuple[int, int]] = []
    for index in range(0, len(prefix) - 1, 2):
        if (kinds[index], kinds[index + 1]) != expected:
            break
        if expected == ("VALUE", "LABEL"):
            pairs.append((index + 1, index))
        else:
            pairs.append((index, index + 1))
    values = [str(entry["text"]) for entry in entries if entry["kind"] == "VALUE"]
    if values and all(_KPI_YEAR_RE.fullmatch(value.strip()) for value in values):
        return []
    return pairs if len(pairs) >= 2 else []


def _kpi_run_replacement(
    entries: list[dict[str, object]],
) -> tuple[list[str] | None, int]:
    """Build conservative bullet output for one candidate text run."""
    alternating_pairs = _alternating_kpi_pairs(entries)
    mixed_entries = [entry for entry in entries if entry["kind"] == "MIXED"]
    use_mixed = len(mixed_entries) >= 2 or bool(alternating_pairs)
    mixed_pairs = [entry for entry in mixed_entries if use_mixed]
    pair_count = len(alternating_pairs) + len(mixed_pairs)
    if pair_count < 2:
        return None, 0

    emitted: dict[int, str] = {}
    consumed: set[int] = set()
    for label_index, value_index in alternating_pairs:
        label = str(entries[label_index]["text"])
        value = str(entries[value_index]["text"])
        emitted[min(label_index, value_index)] = f"- {label}: {value}"
        consumed.update({label_index, value_index})
    for entry in mixed_pairs:
        index = int(entry["entry_index"])
        label, value = entry["pair"]  # type: ignore[misc]
        emitted[index] = f"- {label}: {value}"
        consumed.add(index)

    replacement: list[str] = []
    for index, entry in enumerate(entries):
        if index in emitted:
            replacement.append(emitted[index])
        elif index not in consumed:
            # A confirmed run may end with an unmatched value/label. Keeping
            # it as a bullet avoids recreating a rendered paragraph blob.
            replacement.append(f"- {entry['text']}")
    return replacement, pair_count


def _is_kpi_text_run_breaker(line: str) -> bool:
    stripped = line.strip()
    return bool(
        stripped.startswith(("#", "|", ">", "![", "<!--"))
        or stripped.startswith("**Image summary:**")
        or re.match(r"^\s*-\s+.+:\s+", line)
        or re.match(r"^\(\w{1,3}\)\s", stripped)
        or (stripped and set(stripped) <= set("|-: "))
    )


def pair_kpi_text_runs(markdown: str) -> tuple[str, int]:
    """Convert loose KPI value/label text runs into ``- LABEL: value`` bullets."""
    lines = markdown.splitlines()
    replacements: list[tuple[int, int, list[str]]] = []
    run: list[dict[str, object]] = []
    pending_blank = False

    def flush() -> None:
        nonlocal run, pending_blank
        if not run:
            pending_blank = False
            return
        replacement, pair_count = _kpi_run_replacement(run)
        if replacement is not None:
            replacements.append(
                (int(run[0]["line_index"]), int(run[-1]["line_index"]) + 1, replacement)
            )
            pair_counts.append(pair_count)
        run = []
        pending_blank = False

    pair_counts: list[int] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("**Image summary:**"):
            flush()
            index += 1
            while index < len(lines) and lines[index].strip():
                index += 1
            continue
        if not stripped:
            if run:
                if pending_blank:
                    flush()
                else:
                    pending_blank = True
            index += 1
            continue
        if _is_kpi_text_run_breaker(line):
            flush()
            index += 1
            continue
        kind, pair = _classify_kpi_text_line(line)
        if kind == "OTHER":
            flush()
            index += 1
            continue
        run.append(
            {
                "line_index": index,
                "entry_index": len(run),
                "kind": kind,
                "text": re.sub(r"^\s*-\s+", "", line).strip(),
                "pair": pair,
            }
        )
        pending_blank = False
        index += 1
    flush()
    if not replacements:
        return markdown, 0

    out: list[str] = []
    cursor = 0
    for start, end, replacement in replacements:
        out.extend(lines[cursor:start])
        out.extend(replacement)
        cursor = end
    out.extend(lines[cursor:])
    return "\n".join(out) + ("\n" if markdown.endswith("\n") else ""), sum(pair_counts)
# Models often render footnote markers with unicode superscripts (``[⁵]``); map
# them back to ASCII digits before analysis.
_SUPERSCRIPT_MAP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")

META_COMMENTARY_PATTERNS = [
    re.compile(r"\bas per (?:the )?image\b", re.IGNORECASE),
    re.compile(r"\bfirst[- ]pass\b", re.IGNORECASE),
    re.compile(r"\bthe image shows\b", re.IGNORECASE),
    re.compile(r"\bshould be included\b", re.IGNORECASE),
    re.compile(r"\bthis text is preserved\b", re.IGNORECASE),
    re.compile(r"\bthe (?:proposed|refined|original) (?:markdown|parse)\b", re.IGNORECASE),
]


# --------------------------------------------------------------------------- #
# HTML table flattening (F2 table class)
# --------------------------------------------------------------------------- #


class _TableGridParser(HTMLParser):
    """Collect ``<tr>/<td>/<th>`` cells with their span attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict]] = []
        self._cur_row: list[dict] | None = None
        self._in_cell = False
        self._parts: list[str] = []
        self._colspan = 1
        self._rowspan = 1
        self._is_header = False

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "tr":
            self._cur_row = []
        elif tag in ("td", "th"):
            self._in_cell = True
            self._parts = []
            self._colspan = _safe_int(a.get("colspan"), 1)
            self._rowspan = _safe_int(a.get("rowspan"), 1)
            self._is_header = tag == "th"
        elif tag == "br" and self._in_cell:
            self._parts.append(" ")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag.lower() == "br" and self._in_cell:
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("td", "th") and self._in_cell:
            if self._cur_row is None:
                self._cur_row = []
            self._cur_row.append(
                {
                    "text": "".join(self._parts),
                    "colspan": max(1, self._colspan),
                    "rowspan": max(1, self._rowspan),
                    "header": self._is_header,
                }
            )
            self._in_cell = False
        elif tag == "tr" and self._cur_row is not None:
            self.rows.append(self._cur_row)
            self._cur_row = None

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._parts.append(data)


def _safe_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _clean_cell_text(text: str) -> str:
    text = _LATEX_MARKER_RE.sub(r"\1", text)
    text = _HTML_TAG_RE.sub("", text)
    text = text.replace(" ", " ")
    text = re.sub(r"\s+", " ", text).strip()
    # Escape pipes so the cell cannot break the markdown table.
    return text.replace("|", "\\|")


def _expand_grid(rows: list[list[dict]]) -> list[list[str]]:
    """Expand rowspan/colspan into a dense matrix, repeating spanned labels."""
    matrix: dict[tuple[int, int], str] = {}
    carry: dict[int, list] = {}  # column -> [remaining_rows, text]
    max_col = 0
    for r in range(len(rows)):
        cells = rows[r]
        ci = 0
        c = 0
        while True:
            has_future_carry = any(col >= c and info[0] > 0 for col, info in carry.items())
            if ci >= len(cells) and not has_future_carry:
                break
            if c in carry and carry[c][0] > 0:
                carry[c][0] -= 1
                matrix[(r, c)] = carry[c][1]
                c += 1
                continue
            if ci < len(cells):
                cell = cells[ci]
                ci += 1
                for _ in range(cell["colspan"]):
                    matrix[(r, c)] = cell["text"]
                    if cell["rowspan"] > 1:
                        carry[c] = [cell["rowspan"] - 1, cell["text"]]
                    c += 1
            else:
                # No cell here but a carried column lies further right; leave a gap.
                c += 1
        max_col = max(max_col, c)

    grid: list[list[str]] = []
    for r in range(len(rows)):
        grid.append([_clean_cell_text(matrix.get((r, c), "")) for c in range(max_col)])
    return grid


def _grid_to_pipe_table(grid: list[list[str]]) -> str:
    if not grid:
        return ""
    n_cols = max(len(row) for row in grid)
    if n_cols == 0:
        return ""
    header = grid[0] + [""] * (n_cols - len(grid[0]))
    body = grid[1:]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * n_cols) + " |",
    ]
    for row in body:
        padded = row + [""] * (n_cols - len(row))
        lines.append("| " + " | ".join(padded) + " |")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Sectioned tables: a single Docling table whose body carries spanning "section
# header" rows — either only the first cell filled (page 154's five-year note)
# or one value repeated across the whole row (page 43's colspan banners). The
# VLM tends to degrade these into `### Section` headings + `- label: value`
# lists, orphaning the column header. Splitting deterministically into one
# labeled subtable per section keeps a well-formed table with every column's
# header context preserved.
# --------------------------------------------------------------------------- #

SECTIONED_MIN_COLS = 3          # label column + >= 2 value columns
SECTIONED_MIN_ROWS = 4          # header + section + >= 2 data rows
SECTIONED_MIN_DATA_ROWS = 2
SECTIONED_MAX_TITLE_CHARS = 120
SECTIONED_HEADER_CELL_MAX_CHARS = 40   # heuristic header-row fallback


def _sectioned_section_title(cells: list[str]) -> str | None:
    """Section title if this body row spans the table as a group header.

    Two shapes: only the first cell filled, or a single value expanded across
    the row by Docling's colspan handling. Returns ``None`` for ordinary data
    rows (including the all-``-`` placeholder row on page 43).
    """
    stripped = [cell.strip() for cell in cells]
    non_empty = [cell for cell in stripped if cell]
    if not non_empty:
        return None
    first = stripped[0]
    if first and not any(stripped[1:]):
        return first if len(first) <= SECTIONED_MAX_TITLE_CHARS else None
    if (
        len(non_empty) >= 2
        and len(set(non_empty)) == 1
        and any(ch.isalpha() for ch in non_empty[0])
        and BANNER_CELL_MIN_CHARS <= len(non_empty[0]) <= SECTIONED_MAX_TITLE_CHARS
    ):
        return non_empty[0]
    return None


def _sectioned_header_row_ok(row: list[str], header_rows: int | None) -> bool:
    """Whether grid row 0 is a usable single column-header row.

    Only body rows (``grid[1:]``) are ever treated as section headers, so a
    genuine stacked/multi-row header stays intact: its second row is data-like,
    never a section row, and the table simply is not split.
    """
    if _sectioned_section_title(row) is not None:
        return False
    if header_rows and header_rows >= 1:
        return True
    # header_rows in (0, None): Docling did not flag a header row. Accept row 0
    # as the header when it is a plausible header line — mostly filled with
    # short cells — whether or not its label cell is blank (page 154 leaves it
    # blank; page 43 fills it with "(in percentage)"). The real gate is the
    # presence of body section rows, which ordinary tables never have.
    stripped = [cell.strip() for cell in row]
    non_empty = [cell for cell in stripped if cell]
    if not stripped or len(non_empty) * 2 < len(stripped):
        return False
    return all(len(cell) <= SECTIONED_HEADER_CELL_MAX_CHARS for cell in non_empty)


def _sectioned_is_qualifier(title: str) -> bool:
    """A parenthetical unit note ("(in € millions)") attached to a section head,
    not a section of its own."""
    return title.strip().startswith("(")


def split_sectioned_grid(
    grid: list[list[str]], *, header_rows: int | None = None
) -> dict[str, object] | None:
    """Split a section-banded Docling grid into labeled subtables.

    Returns ``None`` (leave the table alone) unless the grid has a single header
    row and at least one titled section that owns data rows. A spanning row with
    no data rows before the next spanning row is kept as a *group header* (a
    parent category such as page 145's "SUBSIDIARIES"), rendered as a bare
    heading above its child sections.
    """
    rows = [[str(cell).strip() for cell in row] for row in grid]
    if len(rows) < SECTIONED_MIN_ROWS:
        return None
    n_cols = max((len(row) for row in rows), default=0)
    if n_cols < SECTIONED_MIN_COLS:
        return None
    header = rows[0] + [""] * (n_cols - len(rows[0]))
    if not _sectioned_header_row_ok(header, header_rows):
        return None

    sections: list[dict[str, object]] = [
        {"title": None, "qualifiers": [], "rows": [], "source_rows": []}
    ]
    for source_row, row in enumerate(rows[1:], start=1):
        padded = row + [""] * (n_cols - len(row))
        title = _sectioned_section_title(padded)
        if title is not None:
            current = sections[-1]
            if (
                _sectioned_is_qualifier(title)
                and current["title"] is not None
                and not current["rows"]
            ):
                # "(in € millions)" directly under a just-opened section heading.
                current["qualifiers"].append(title)  # type: ignore[union-attr]
            else:
                sections.append(
                    {
                        "title": title,
                        "qualifiers": [],
                        "rows": [],
                        "source_rows": [],
                    }
                )
        else:
            sections[-1]["rows"].append(padded)  # type: ignore[union-attr]
            sections[-1]["source_rows"].append(source_row)  # type: ignore[union-attr]

    # Drop the empty untitled lead section and any dangling trailing banner(s)
    # that never received data rows.
    sections = [s for s in sections if s["title"] is not None or s["rows"]]
    while sections and sections[-1]["title"] is not None and not sections[-1]["rows"]:
        sections.pop()
    for section in sections:
        section["is_group"] = section["title"] is not None and not section["rows"]

    if not any(s["title"] is not None and s["rows"] for s in sections):
        return None  # need at least one titled section that carries data
    data_row_count = sum(len(s["rows"]) for s in sections)  # type: ignore[arg-type]
    if data_row_count < SECTIONED_MIN_DATA_ROWS:
        return None
    # Reserve sectioning for a spanning title that governs two or more complete
    # data rows. An alternating "title row -> single detail row" pattern (page
    # 190's impact table, whose super-header sits outside the grid) is a
    # title/detail table, handled deterministically by the table normalizer —
    # never split it into one bare-heading subtable per record.
    data_sections = [s for s in sections if s["title"] is not None and s["rows"]]
    if data_sections and max(len(s["rows"]) for s in data_sections) < 2:  # type: ignore[arg-type]
        return None

    return {
        "header": header,
        "n_cols": n_cols,
        "sections": sections,
        "section_titles": [s["title"] for s in sections if s["title"] is not None],
        "section_qualifiers": [
            qualifier
            for s in sections
            for qualifier in s["qualifiers"]  # type: ignore[union-attr]
        ],
        # One "group"/"data" entry per titled section, in order — lets the
        # enforcement pass assign consistent heading levels.
        "section_kinds": [
            "group" if s["is_group"] else "data"
            for s in sections
            if s["title"] is not None
        ],
        "data_row_count": data_row_count,
    }


def sectioned_heading_line(title: str, qualifiers: list[str], level: int) -> str:
    heading = title
    if qualifiers:
        heading = heading + " " + " ".join(qualifiers)
    return "#" * max(1, min(level, 6)) + " " + heading


def render_sectioned_tables(split: dict[str, object], base_level: int = 3) -> str:
    """Render a :func:`split_sectioned_grid` result as titled pipe subtables.

    Each data subtable repeats the original column header verbatim under its
    ``###`` heading; a group header renders as a bare heading above its
    children. ``base_level`` sets the depth: group headers sit at
    ``base_level``, data sections one deeper when any group header exists (so
    sibling sub-categories share one level), otherwise at ``base_level``.
    """
    header = list(split["header"])  # type: ignore[arg-type]
    n_cols = int(split["n_cols"])  # type: ignore[call-overload]
    kinds = list(split.get("section_kinds", []))  # type: ignore[union-attr]
    group_level = base_level
    data_level = base_level + 1 if "group" in kinds else base_level
    blocks: list[str] = []
    for section in split["sections"]:  # type: ignore[union-attr]
        title = section["title"]
        if title is not None:
            level = group_level if section.get("is_group") else data_level
            blocks.append(
                sectioned_heading_line(str(title), [str(q) for q in section["qualifiers"]], level)
            )
        if section["rows"]:
            body_rows = [
                [cell_with_bullets(str(cell)) for cell in row]
                for row in section["rows"]
            ]
            grid = [header, *body_rows]
            escaped = [
                [cell.replace("|", "\\|") for cell in (row + [""] * (n_cols - len(row)))]
                for row in grid
            ]
            blocks.append(_grid_to_pipe_table(escaped))
    return "\n\n".join(blocks)


def flatten_html_tables(markdown: str) -> str:
    """Replace every ``<table>`` block with a rowspan/colspan-expanded pipe table.

    Group labels that spanned multiple rows/columns in the HTML are repeated into
    every cell they covered, so each resulting row is self-contained (RAG-friendly).
    Malformed tables that yield no grid are left untouched.
    """

    def _replace(match: re.Match) -> str:
        block = match.group(0)
        parser = _TableGridParser()
        try:
            parser.feed(block)
            parser.close()
        except Exception:
            return block
        rows = [row for row in parser.rows if row]
        if not rows:
            return block
        grid = _expand_grid(rows)
        table = _grid_to_pipe_table(grid)
        if not table:
            return block
        return "\n" + table + "\n"

    return _TABLE_BLOCK_RE.sub(_replace, markdown)


# --------------------------------------------------------------------------- #
# Bullet / heading normalization (F9 hygiene)
# --------------------------------------------------------------------------- #


def cell_with_bullets(cell: str) -> str:
    """Preserve inline table-cell lists as ``<br>- item`` fragments.

    A cell is a list only when it starts with a bullet glyph or contains at
    least two strong glyphs. This keeps a lone mid-text square and weak middle
    dots in numeric or chemical values literal.
    """
    cell = cell.strip()
    strong_count = sum(cell.count(ch) for ch in GRID_BULLET_CHARS_STRONG)
    leading_bullet = bool(cell) and cell[0] in GRID_BULLET_CHARS
    if strong_count < 2 and not leading_bullet:
        return cell
    leading_weak = bool(cell) and cell[0] in GRID_BULLET_CHARS_WEAK
    split_re = _GRID_BULLET_SPLIT_RE if leading_weak else _GRID_STRONG_SPLIT_RE
    parts = [part.strip() for part in split_re.split(cell)]
    out: list[str] = []
    for index, part in enumerate(parts):
        if not part:
            continue
        if index == 0 and not leading_bullet:
            out.append(part)
        else:
            out.append(f"- {part}")
    return "<br>".join(out) if out else cell


def became_bullet_list(before: str, after: str) -> bool:
    """Whether :func:`cell_with_bullets` performed a genuine list conversion."""
    return after != before.strip() and (after.startswith("- ") or "<br>- " in after)


def strip_redundant_list_glyphs(markdown: str) -> str:
    """Remove a duplicated strong bullet after an ordinary Markdown list marker.

    This deliberately skips pipe rows and legend lines such as ``- ■ = fully
    covered``. It must run before prose bullet normalization, where ``- • x``
    would otherwise be split into malformed lines.
    """
    lines: list[str] = []
    changed = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("|"):
            lines.append(line)
            continue
        match = _REDUNDANT_LIST_GLYPH_RE.match(line)
        if match and not match.group(3).lstrip().startswith("="):
            lines.append(f"{match.group(1)}- {match.group(3)}")
            changed = True
        else:
            lines.append(line)
    if not changed:
        return markdown
    suffix = "\n" if markdown.endswith("\n") else ""
    return "\n".join(lines) + suffix


def _split_inline_bullets(line: str) -> list[str]:
    """Turn ``Prefix: • a • b`` into a prefix line plus ``- a`` / ``- b`` lines."""
    stripped = _LEADING_BULLET_RE.sub("", line, count=1)
    leading_bullet = stripped != line
    # Split on inline bullet separators.
    parts = _INLINE_BULLET_RE.split(stripped)
    parts = [p.strip() for p in parts]
    if len(parts) == 1:
        # No inline separator; single (possibly leading-bullet) item.
        return [f"- {parts[0]}"] if leading_bullet and parts[0] else [line]

    out: list[str] = []
    first = parts[0]
    if leading_bullet:
        if first:
            out.append(f"- {first}")
    else:
        # First chunk is a prefix (e.g. a label ending in ':'), keep as its own line.
        if first:
            out.append(first)
    for item in parts[1:]:
        if item:
            out.append(f"- {item}")
    return out or [line]


def normalize_pipe_table_cell_bullets(markdown: str) -> str:
    """Apply :func:`cell_with_bullets` to every data cell in pipe table rows.

    ``flatten_html_tables`` handles HTML tables; this covers pipe tables that
    the VLM emits directly (e.g. page 184's ■-bulleted stakeholder table).
    Separator rows (``|---|...|``) are left untouched.
    """
    _SEP_RE = re.compile(r"^\|[\s|:-]+\|?\s*$")
    lines: list[str] = []
    for line in markdown.splitlines():
        if not line.lstrip().startswith("|") or _SEP_RE.match(line):
            lines.append(line)
            continue
        cells = line.split("|")
        # cells[0] and cells[-1] are the empty strings outside the outer pipes
        cells = [cells[0]] + [cell_with_bullets(c) for c in cells[1:-1]] + [cells[-1]]
        lines.append("|".join(cells))
    suffix = "\n" if markdown.endswith("\n") else ""
    return "\n".join(lines) + suffix


def normalize_bullets_and_headings(markdown: str) -> str:
    """Normalize unicode bullets to ``- `` (splitting inline bullets), and ensure
    headings are separated by blank lines so lists/headings render correctly."""
    out_lines: list[str] = []
    for line in markdown.splitlines():
        if line.lstrip().startswith("|"):
            out_lines.append(line)  # pipe table rows handled by normalize_pipe_table_cell_bullets
        elif any(ch in line for ch in BULLET_CHARS):
            out_lines.extend(_split_inline_bullets(line))
        else:
            out_lines.append(line)

    # Ensure a blank line before each heading (unless at top / already blank).
    spaced: list[str] = []
    for line in out_lines:
        if _HEADING_RE.match(line) and spaced and spaced[-1].strip() != "":
            spaced.append("")
        spaced.append(line)

    text = "\n".join(spaced)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


_DATAPOINT_HEADING_RE = re.compile(
    r"^[^:|#]{1,40}:\s*[~≈<>+-]?[\d][\d.,]*\s*(?:%|€|\$|£|pp|pts?|years?|yrs?|x)?\s*(?:\[[^\]]{1,4}\])?$"
)


def demote_datapoint_headings(markdown: str) -> str:
    """Turn empty `### Label: value` heading sections into `- Label: value` items.

    Models transcribing charts tend to emit one heading per data point
    (`### 2015: 77%`) despite instructions. A heading whose text is a short
    label:number pair AND whose section is empty (the next line of content is
    another heading or EOF) is a data point, not a section — demote it to a list
    item. Headings with body content under them (e.g. `### Milk: 36%` followed by
    a program paragraph) are left alone.
    """
    lines = markdown.splitlines()
    out: list[str] = []
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m and _DATAPOINT_HEADING_RE.match(m.group(2).strip()):
            section_empty = True
            for nxt in lines[i + 1 :]:
                if not nxt.strip():
                    continue
                section_empty = bool(_HEADING_RE.match(nxt))
                break
            if section_empty:
                out.append(f"- {m.group(2).strip()}")
                continue
        out.append(line)
    return "\n".join(out)


def heading_warnings(markdown: str, ocr_markdown: str | None = None) -> list[str]:
    """Warn when a page has no heading, or lost heading levels vs the OCR pass."""
    warnings: list[str] = []
    refined_levels = _heading_levels(markdown)
    if not refined_levels:
        warnings.append("no heading present on page")
    if ocr_markdown is not None:
        ocr_levels = _heading_levels(ocr_markdown)
        if ocr_levels and len(refined_levels) < len(ocr_levels):
            warnings.append(
                f"refined has fewer heading levels ({sorted(refined_levels)}) "
                f"than OCR ({sorted(ocr_levels)})"
            )
    return warnings


def _heading_levels(markdown: str) -> set[int]:
    levels: set[int] = set()
    for line in markdown.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            levels.add(len(m.group(1)))
    return levels


# --------------------------------------------------------------------------- #
# Uncertainty block lifecycle (F4)
# --------------------------------------------------------------------------- #


def extract_uncertainty(markdown: str) -> tuple[str, str]:
    """Split off any ``## Uncertain mappings`` block.

    Returns ``(markdown_without_block, sidecar_text)``. The block runs from its
    heading to the next heading of equal-or-higher level (fewer/equal ``#``) or EOF.
    """
    lines = markdown.splitlines()
    keep: list[str] = []
    sidecar: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        m = _HEADING_RE.match(line)
        if m and _UNCERTAINTY_TITLE_RE.match(line):
            level = len(m.group(1))
            sidecar.append(line)
            i += 1
            while i < n:
                nxt = _HEADING_RE.match(lines[i])
                if nxt and len(nxt.group(1)) <= level:
                    break
                sidecar.append(lines[i])
                i += 1
            sidecar.append("")
            continue
        keep.append(line)
        i += 1

    kept = re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()
    if kept:
        kept += "\n"
    return kept, "\n".join(sidecar).strip()


# --------------------------------------------------------------------------- #
# Footnote consistency (F7)
# --------------------------------------------------------------------------- #


def footnote_consistency(markdown: str) -> list[str]:
    """Compare body footnote references against their definitions.

    A definition is a line like ``[1] some text``. Everything else that looks like
    ``[n]`` is treated as a body reference. Returns human-readable warnings.

    Note: this catches *structural* problems (a dropped/renumbered definition, a
    reference with no definition, gaps). It cannot detect a *semantic* swap where
    the marker is right but the definition text is wrong — that is the refinement
    prompt's completeness/footnote contract's job, not the lint's.
    """
    markdown = markdown.translate(_SUPERSCRIPT_MAP)
    def_labels: list[str] = []
    ref_labels: set[str] = set()
    for line in markdown.splitlines():
        def_match = _FOOTNOTE_DEF_LINE_RE.match(line)
        if def_match:
            def_labels.append(def_match.group(1))
            continue
        for marker in _FOOTNOTE_MARKER_RE.findall(line):
            ref_labels.add(marker)

    warnings: list[str] = []
    def_set = set(def_labels)
    duplicates = sorted({d for d in def_labels if def_labels.count(d) > 1})
    if duplicates:
        warnings.append(f"duplicate footnote definitions: {duplicates}")
    missing_defs = sorted(ref_labels - def_set, key=_int_key)
    if missing_defs:
        warnings.append(f"footnote references without definitions: {missing_defs}")
    # Orphan definitions (defined but not referenced on the slide) are intentionally
    # NOT warned: footnote blocks legitimately define notes for markers whose in-body
    # superscript OCR dropped, so that signal is too noisy to be useful.
    # Non-sequential definition set (e.g. renumbering dropped [5]).
    if def_set:
        nums = sorted(int(d) for d in def_set)
        gaps = [n for n in range(nums[0], nums[-1] + 1) if n not in nums]
        if gaps:
            warnings.append(f"footnote definition gaps (possible renumber): {gaps}")
    return warnings


def footnote_labels(markdown: str) -> tuple[set[str], set[str]]:
    """Return ``(reference_labels, definition_labels)`` (ASCII digits, superscripts
    normalized). Useful for the eval harness and tests."""
    markdown = markdown.translate(_SUPERSCRIPT_MAP)
    defs: set[str] = set()
    refs: set[str] = set()
    for line in markdown.splitlines():
        m = _FOOTNOTE_DEF_LINE_RE.match(line)
        if m:
            defs.add(m.group(1))
            continue
        refs.update(_FOOTNOTE_MARKER_RE.findall(line))
    return refs, defs


def _int_key(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


# --------------------------------------------------------------------------- #
# Completeness diff (F5)
# --------------------------------------------------------------------------- #

# Words too generic to count toward matching a content line.
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
    "our", "your", "their", "its", "has", "have", "had", "not", "but", "all",
    "per", "each", "into", "over", "under", "than", "then", "they", "them",
    "which", "who", "whom", "will", "shall", "can", "may", "these", "those",
    "of", "to", "in", "on", "at", "by", "as", "is", "be", "or", "an", "a",
}


def _significant_words(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9%€$£.,-]*[A-Za-z0-9][A-Za-z0-9%€$£.,-]*", text.lower())
    out: list[str] = []
    for w in words:
        w = w.strip(".,")
        if len(w) < 4:
            # Keep short numeric/percentage tokens; drop tiny words.
            if not any(ch.isdigit() for ch in w):
                continue
        if w in _STOPWORDS:
            continue
        out.append(w)
    return out


_NUMERIC_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)?")
_YEAR_ONLY_RE = re.compile(r"^(?:19|20)\d{2}$")
_BARE_SHORT_NUMBER_RE = re.compile(r"^\d{1,3}$")
_SHORT_VALUE_SIGNAL_RE = re.compile(
    r"(?i)(?:%|[$\u20ac\u00a3]|(?:\d[\d,.\s]*)(?:pts?|bps?|bn|m|k|kg|g|t|tons?|"
    r"tonnes?|co2|co2e|eur|usd|gbp|l|ml|ha|m3)\b|\d+[,.]\d+)"
)


def _numeric_tokens(text: str) -> set[str]:
    """Digit groups normalized for matching ('18%' -> '18', '-46,3' -> '46.3')."""
    return {tok.replace(",", ".") for tok in _NUMERIC_TOKEN_RE.findall(text)}


def _is_meaningful_short_value_line(text: str) -> bool:
    stripped = text.strip()
    if _YEAR_ONLY_RE.fullmatch(stripped):
        return True
    if _BARE_SHORT_NUMBER_RE.fullmatch(stripped):
        return False
    return bool(_SHORT_VALUE_SIGNAL_RE.search(stripped))


def _flatten_for_text(markdown: str) -> str:
    """Flatten tables to their cell text, then strip HTML so table content counts."""
    flattened = flatten_html_tables(markdown)
    flattened = _HTML_TAG_RE.sub(" ", flattened)
    flattened = _LATEX_MARKER_RE.sub(r"\1", flattened)
    return flattened


def ocr_content_lines(ocr_markdown: str) -> list[str]:
    """Extract meaningful content lines from an OCR markdown pass."""
    # Normalize bullets first so an inline-bulleted OCR line splits into the same
    # per-item lines the refined output uses (per-item completeness granularity).
    text = normalize_bullets_and_headings(_flatten_for_text(ocr_markdown))
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if re.fullmatch(r"!\[[^\]]*\]\([^)]*\)", line):
            continue
        # Strip markdown table pipes / heading markers / bullets for the content view.
        line = re.sub(r"^[#>\-*|\s]+", "", line)
        line = line.replace("|", " ").strip()
        line = re.sub(r"^-{2,}$", "", line).strip()
        if not line:
            continue
        sig = _significant_words(line)
        if len(sig) < 3:
            # Preserve standalone years/values, but still ignore bare page numbers.
            if not _is_meaningful_short_value_line(line):
                continue
        lines.append(line)
    return lines


def _body_windows(
    flattened_text: str, window: int = 3
) -> list[tuple[set[str], set[str]]]:
    """Per-line ``(significant_words, numeric_tokens)`` unions over a sliding
    window of adjacent lines. Shared by the completeness diff (is a raw line
    missing from the body?) and the unplaced filter (is an unplaced line in
    fact present?) — the two are near-inverse tests."""
    lines = [ln.strip() for ln in flattened_text.splitlines() if ln.strip()]
    line_words = [set(_significant_words(ln)) for ln in lines]
    line_nums = [_numeric_tokens(ln) for ln in lines]
    windows: list[tuple[set[str], set[str]]] = []
    for i in range(len(line_words)):
        words: set[str] = set()
        nums: set[str] = set()
        for j in range(i, min(i + window, len(line_words))):
            words |= line_words[j]
            nums |= line_nums[j]
        windows.append((words, nums))
    return windows


def completeness_diff(
    ocr_markdown: str,
    refined_markdown: str,
    *,
    coverage_threshold: float = 0.6,
    window: int = 3,
) -> list[str]:
    """Return OCR content lines whose words are largely absent from the refined body.

    Coverage is computed per *window of adjacent refined lines* (default 3), not
    against a global word bag: a global bag lets a dropped line count as present
    when its words happen to be scattered across unrelated parts of the page
    (observed: "Dairy ingredients: 18%" masked by "dairy farming" + "ingredients"
    elsewhere). Windows still tolerate legitimate rephrasing, merging, and a line
    being split across up to ``window`` lines. difflib fallback for near-verbatim
    matches survives unchanged.
    """
    refined_flat = _flatten_for_text(refined_markdown)
    windows = _body_windows(refined_flat, window)
    refined_lower = refined_flat.lower()

    missing: list[str] = []
    for line in ocr_content_lines(ocr_markdown):
        sig = set(_significant_words(line))
        if not sig:
            continue
        nums = _numeric_tokens(line)
        coverage = 0.0
        for win_words, win_nums in windows:
            # Numbers are decisive: a window that shares none of the line's
            # numeric tokens cannot claim the line, however much prose overlaps.
            if nums and not (nums & win_nums):
                continue
            coverage = max(coverage, len(sig & win_words) / len(sig))
        if coverage >= coverage_threshold:
            continue
        # Fallback: substring / fuzzy match of the raw line against refined text.
        probe = line.lower()[:80]
        if probe and probe in refined_lower:
            continue
        if difflib.SequenceMatcher(None, probe, refined_lower).find_longest_match(
            0, len(probe), 0, len(refined_lower)
        ).size >= max(12, int(len(probe) * 0.7)):
            continue
        missing.append(line)
    return missing


# --------------------------------------------------------------------------- #
# Unplaced-content enforcement (completeness covenant, in code)
# --------------------------------------------------------------------------- #

UNPLACED_TITLE = "## Unplaced content"
_UNPLACED_SECTION_TITLE_RE = re.compile(r"^##\s+Unplaced content\s*$", re.IGNORECASE)

# Coverage a part's alphabetic tokens need inside one body window to count as
# already present (stricter than the 0.6 the completeness diff uses to call a
# line missing, so the two tests cannot flap).
UNPLACED_PRESENT_COVERAGE = 0.85


def extract_unplaced_sections(markdown: str) -> tuple[str, list[str]]:
    """Remove ``## Unplaced content`` sections and return their nonblank lines.

    The deterministic completeness guard is the only final author of this
    section. Model-authored copies are recycled through that guard, which keeps
    genuine missing prose while dropping empty or already-placed entries.
    """
    lines = markdown.splitlines()
    out: list[str] = []
    captured: list[str] = []
    in_section = False
    found = False
    for line in lines:
        if _UNPLACED_SECTION_TITLE_RE.fullmatch(line.strip()):
            in_section = True
            found = True
            continue
        if in_section and re.match(r"^#{1,2}\s+\S", line):
            in_section = False
        if in_section:
            stripped = line.strip()
            if stripped:
                captured.append(stripped)
            continue
        out.append(line)
    if not found:
        return markdown, []
    suffix = "\n" if markdown.endswith("\n") and out else ""
    return "\n".join(out) + suffix, captured


def _part_present(
    part: str,
    windows: list[tuple[set[str], set[str]]],
    body_lower: str,
) -> bool:
    """Is one whitespace-column part of an unplaced line present in the body?"""
    alpha = {
        word
        for word in _significant_words(part)
        if not any(ch.isdigit() for ch in word)
    }
    nums = _numeric_tokens(part)
    if alpha:
        for words, window_nums in windows:
            if nums and not nums <= window_nums:
                continue
            if len(alpha & words) / len(alpha) >= UNPLACED_PRESENT_COVERAGE:
                return True
        return False
    if nums:
        # Numeric-ish parts ("2024", "A-2"): require the literal text, so a
        # value the table extraction dropped is never declared present.
        return re.sub(r"\s+", " ", part.lower()).strip() in body_lower
    return True  # punctuation-only separators ("-") never block a drop


def filter_unplaced_lines(
    lines: list[str],
    final_markdown: str,
    furniture_texts: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Split completeness-guard lines into (kept, dropped).

    The guard's diff has false negatives on reformatted content: tables get
    flattened and TOCs restructured, so their raw lines look "missing" and get
    re-appended as noise. Dropped are:

    - repeated-phrase fragments: the line is one phrase repeated >= 2 times
      across wide-space columns ("Year ended December 31" x6, page 86);
    - already-present lines: every wide-space-separated part is covered by
      some 3-line body window (labels of a KPI panel each sit next to their
      own value, page 5) — numeric parts must appear literally;
    - page-furniture lines (running headers/footers, WP1 signatures).

    Everything else is kept: real data the table extraction lost must survive
    (page 58's ``Rating - A-2`` line is the only place A-2 exists).
    """
    flattened = _flatten_for_text(final_markdown)
    windows = _body_windows(flattened)
    body_lower = re.sub(r"\s+", " ", flattened.lower())
    furniture = {t for t in (furniture_texts or set()) if t}

    kept: list[str] = []
    dropped: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        parts = [part.strip() for part in re.split(r"\s{2,}", stripped) if part.strip()]
        if len(parts) >= 2 and len(set(parts)) == 1:
            dropped.append(line)
            continue
        if normalize_furniture_text(stripped) in furniture:
            dropped.append(line)
            continue
        if parts and all(_part_present(part, windows, body_lower) for part in parts):
            dropped.append(line)
            continue
        kept.append(line)
    return kept, dropped


def merge_unplaced_content(markdown: str, missing_lines: list[str]) -> str:
    """Deterministically append content lines that no model pass placed.

    This turns the completeness covenant from a prompt hope into a code
    guarantee: anything the diff still reports missing after refine/merge-back/
    verify is appended under a final ``## Unplaced content`` section instead of
    being silently lost. Lines already present (case-insensitive) are skipped;
    an existing section is extended rather than duplicated.
    """
    to_add = [ln.strip() for ln in missing_lines if ln.strip()]
    if not to_add:
        return markdown
    existing_lower = markdown.lower()
    to_add = [ln for ln in to_add if ln.lower() not in existing_lower]
    if not to_add:
        return markdown

    items = [f"- {ln}" for ln in to_add]
    lines = markdown.rstrip("\n").splitlines()
    for i, line in enumerate(lines):
        if line.strip().lower() == UNPLACED_TITLE.lower():
            # Extend the existing section: insert before the next heading (or EOF).
            j = i + 1
            while j < len(lines) and not _HEADING_RE.match(lines[j]):
                j += 1
            return "\n".join(lines[:j] + items + lines[j:]) + "\n"
    body = "\n".join(lines).rstrip()
    return f"{body}\n\n{UNPLACED_TITLE}\n\n" + "\n".join(items) + "\n"


# --------------------------------------------------------------------------- #
# Footnote shape normalization (one ## Footnotes section per page, deduped)
# --------------------------------------------------------------------------- #

_FOOTNOTES_TITLE_RE = re.compile(r"^\s*#{1,6}\s*footnotes?\s*$", re.IGNORECASE)


def normalize_footnotes(markdown: str) -> str:
    """Give every page the same footnote shape: one ``## Footnotes`` section at
    the very end holding all ``[n] ...`` definition lines, exact duplicates removed.

    - ``[n] definition`` lines found mid-body are moved into the section.
    - Lines inside an existing Footnotes section are kept as definitions whatever
      their marker style (``[1]``, ``*``, ``(a)``).
    - A body line that duplicates a section definition verbatim is dropped (the
      model was told to *collect* definitions but often copies instead of moving).
    - Pages without any definition are returned unchanged.
    """
    lines = markdown.splitlines()
    body: list[str] = []
    defs: list[str] = []
    section_defs: set[str] = set()

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        m = _HEADING_RE.match(line)
        if m and _FOOTNOTES_TITLE_RE.match(line):
            level = len(m.group(1))
            i += 1
            while i < n:
                nxt = _HEADING_RE.match(lines[i])
                if nxt and len(nxt.group(1)) <= level:
                    break
                text = lines[i].strip()
                if text:
                    defs.append(text)
                    section_defs.add(text.lower())
                i += 1
            continue
        body.append(line)
        i += 1

    # Pull numeric definition lines out of the body.
    remaining: list[str] = []
    for line in body:
        stripped = line.strip()
        if _FOOTNOTE_DEF_LINE_RE.match(stripped):
            defs.append(stripped)
            continue
        # Drop body copies of definitions that already live in a Footnotes section.
        if stripped and stripped.lower() in section_defs:
            continue
        remaining.append(line)

    if not defs:
        return markdown

    seen: set[str] = set()
    unique_defs: list[str] = []
    for d in defs:
        key = d.lower()
        if key in seen:
            continue
        seen.add(key)
        unique_defs.append(d)

    text = re.sub(r"\n{3,}", "\n\n", "\n".join(remaining)).strip()
    out = f"{text}\n\n## Footnotes\n" if text else "## Footnotes\n"
    return out + "\n".join(unique_defs) + "\n"


# --------------------------------------------------------------------------- #
# Figure-region markers (chart transcription cue for the refinement prompt)
# --------------------------------------------------------------------------- #

CHART_MARKER = (
    "[CHART HERE — the first pass could not read this chart. Look at this region "
    "of the image and transcribe every printed label and printed value at this "
    "position. Do not skip it.]"
)
FIGURE_MARKER = (
    "[FIGURE HERE — if this figure contains printed text, data, or a caption, "
    "transcribe it at this position; if it is a decorative photo, write nothing.]"
)

_IMG_DIV_RE = re.compile(
    r"<div[^>]*>\s*(<img\b[^>]*>)\s*</div>",
    re.IGNORECASE | re.DOTALL,
)
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_IMG_SRC_RE = re.compile(r"src=\"([^\"]+)\"", re.IGNORECASE)
_IMG_WIDTH_RE = re.compile(r"width=\"(\d+)%\"", re.IGNORECASE)
_MARKER_ECHO_RE = re.compile(r"^.*\[(?:CHART|FIGURE) HERE\b.*$", re.MULTILINE)
# A figure spanning a large share of the slide is almost never decorative — big
# regions on presentation slides are diagrams/infographics that carry data.
LARGE_FIGURE_WIDTH_PCT = 40


def mark_figure_regions(ocr_markdown: str) -> str:
    """Replace OCR image placeholders with explicit transcription markers.

    PaddleOCR-VL saves chart crops as ``img_in_chart_box_*`` and other figures as
    ``img_in_image_box_*``; the ``<img>`` tags mark exactly where the first pass
    saw a graphic it could not read. Turning them into imperative [CHART HERE] /
    [FIGURE HERE] markers gives the refinement model a positioned to-do item
    instead of an ignorable HTML tag. Chart boxes and large figures (>=
    ``LARGE_FIGURE_WIDTH_PCT``% of the slide width) get the mandatory CHART
    marker; small figures keep the decorative escape hatch. Only for the prompt
    input — never saved.
    """

    def _marker(img_tag: str) -> str:
        src = _IMG_SRC_RE.search(img_tag)
        name = src.group(1).rsplit("/", 1)[-1].lower() if src else ""
        width = _IMG_WIDTH_RE.search(img_tag)
        is_large = bool(width and int(width.group(1)) >= LARGE_FIGURE_WIDTH_PCT)
        return CHART_MARKER if ("chart" in name or is_large) else FIGURE_MARKER

    out = _IMG_DIV_RE.sub(lambda m: _marker(m.group(1)), ocr_markdown)
    out = _IMG_TAG_RE.sub(lambda m: _marker(m.group(0)), out)
    return out


def strip_figure_markers(markdown: str) -> str:
    """Remove any [CHART HERE]/[FIGURE HERE] marker lines a model echoed back."""
    return re.sub(r"\n{3,}", "\n\n", _MARKER_ECHO_RE.sub("", markdown)).strip() + "\n"


# --------------------------------------------------------------------------- #
# OCR artifact normalization (LaTeX markers, bare text divs)
# --------------------------------------------------------------------------- #

_TEXT_DIV_RE = re.compile(r"<div[^>]*>\s*([^<>]*?)\s*</div>", re.IGNORECASE | re.DOTALL)
_LATEX_SUBSCRIPT_RE = re.compile(r"\$\s*_\{\s*([^}]*?)\s*\}\s*\$")
_LATEX_CIRC_RE = re.compile(r"\$\s*\^?\{?\s*\\circ\s*\}?\s*\$")
_LATEX_UNDERLINE_RE = re.compile(r"\$\s*\\underline\{\\text\{([^}]*)\}\}\s*\$")
_CARET_MARKER_RE = re.compile(r"\^(\[[^\]]{1,4}\])")


def normalize_ocr_artifacts(markdown: str) -> str:
    """Clean PaddleOCR-VL serialization artifacts that survive model passes:

    - ``$ ^{[1]} $`` / ``$ ^{*} $``  -> ``[1]`` / ``*``   (superscript markers)
    - ``$ _{2} $``                   -> ``2``             (subscripts, e.g. CO2)
    - ``$ ^{\\circ} $``              -> ``°``
    - ``$ \\underline{\\text{X}} $`` -> ``X``
    - ``^[1]`` / ``^[d]``            -> ``[1]`` / ``[d]`` (caret footnote markers)
    - ``<div ...>caption text</div>``-> ``caption text``  (bare centered captions)
    """
    markdown = _LATEX_UNDERLINE_RE.sub(r"\1", markdown)
    markdown = _LATEX_CIRC_RE.sub("°", markdown)
    markdown = _LATEX_MARKER_RE.sub(r"\1", markdown)
    markdown = _LATEX_SUBSCRIPT_RE.sub(r"\1", markdown)
    markdown = _CARET_MARKER_RE.sub(r"\1", markdown)
    markdown = _TEXT_DIV_RE.sub(r"\1", markdown)
    return markdown


# --------------------------------------------------------------------------- #
# TOC pages (F7): detection, section-number restoration
# --------------------------------------------------------------------------- #

_SECTION_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)+$")
_PAGE_NUMBER_CELL_RE = re.compile(r"^\d{1,3}$")
_ORDERED_ITEM_RE = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.+)$")
_LEADER_LINE_RE = re.compile(r"\S(?:\.{3,}|\s{2,})\d{1,3}\s*$")
_CONTENTS_HEADING_RE = re.compile(r"^#{1,6}\s*contents\b", re.IGNORECASE)

TOC_MIN_NUMBERED_LINES = 8
TOC_STRONG_NUMBERED_LINES = 15
# Small chapter-divider TOCs (page 65) miss both thresholds above, but nearly
# every table row is a numbered entry; on financial pages at most ~40% are.
TOC_NUMBERED_ROW_SHARE = 0.8


def _toc_row_cells(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or set(stripped) <= set("|-: "):
        return None
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def looks_like_toc(raw_markdown: str) -> bool:
    """Is this page a table of contents (per its Docling raw markdown)?

    Docling renders these TOCs as pipe tables whose rows end in a 1-3-digit
    page number; plain layouts use dotted/space leaders instead. Detection:
    >= 8 such lines AND (a ``Contents`` heading OR >= 15 such lines).
    """
    numbered_lines = 0
    table_rows = 0
    has_contents = False
    for line in raw_markdown.splitlines():
        stripped = line.strip()
        if _CONTENTS_HEADING_RE.match(stripped):
            has_contents = True
            continue
        cells = _toc_row_cells(line)
        if cells is not None:
            table_rows += 1
            non_empty = [cell for cell in cells if cell]
            if (
                len(non_empty) >= 2
                and _PAGE_NUMBER_CELL_RE.fullmatch(non_empty[-1])
                and any(not _PAGE_NUMBER_CELL_RE.fullmatch(cell) for cell in non_empty[:-1])
            ):
                numbered_lines += 1
        elif _LEADER_LINE_RE.search(stripped):
            numbered_lines += 1
    if numbered_lines < TOC_MIN_NUMBERED_LINES:
        return False
    if has_contents or numbered_lines >= TOC_STRONG_NUMBERED_LINES:
        return True
    return table_rows > 0 and numbered_lines / table_rows >= TOC_NUMBERED_ROW_SHARE


def _toc_section_map(raw_markdown: str) -> dict[str, str]:
    """normalized title -> section number, from the raw TOC table rows."""
    mapping: dict[str, str] = {}

    def _add(number: str, title: str) -> None:
        key = re.sub(r"\s+", " ", title).strip().lower()
        if key and key not in mapping:
            mapping[key] = number

    for line in raw_markdown.splitlines():
        cells = _toc_row_cells(line)
        if not cells:
            continue
        for index, cell in enumerate(cells):
            if _SECTION_NUMBER_RE.fullmatch(cell):
                for title in cells[index + 1 :]:
                    if title and not _PAGE_NUMBER_CELL_RE.fullmatch(title):
                        _add(cell, title)
                        break
            else:
                inline = re.match(r"^(\d+(?:\.\d+)+)\s+(\S.*)$", cell)
                if inline:
                    _add(inline.group(1), inline.group(2))
    return mapping


def restore_toc_section_numbers(markdown: str, raw_markdown: str) -> str:
    """Undo the VLM's sequential renumbering of TOC entries.

    The model flattens ``2.1 Business highlights`` into ordered-list items
    (``2. Business highlights``), and markdown renderers renumber ordered
    lists anyway. Every ordered item on a TOC page becomes an unordered item;
    the true section number is restored from the raw TOC table when the title
    matches, otherwise the fabricated number is dropped (the full TOC remains
    available in docling_raw.md).
    """
    mapping = _toc_section_map(raw_markdown)
    out: list[str] = []
    for line in markdown.splitlines():
        match = _ORDERED_ITEM_RE.match(line)
        if not match:
            out.append(line)
            continue
        indent, title = match.group(1), match.group(3).strip()
        number = mapping.get(re.sub(r"\s+", " ", title).lower())
        if number:
            out.append(f"{indent}- {number} {title}")
        else:
            out.append(f"{indent}- {title}")
    return "\n".join(out) + ("\n" if markdown.endswith("\n") else "")


# --------------------------------------------------------------------------- #
# HTML entity leaks (F8): "Michel &amp; Augustin" (page 49)
# --------------------------------------------------------------------------- #

# Ordered so &amp; is decoded LAST: a double-escaped "&amp;lt;" resolves to
# the literal "&lt;" text rather than "<".
_HTML_ENTITIES = (
    ("&lt;", "<"),
    ("&gt;", ">"),
    ("&nbsp;", " "),
    ("&quot;", '"'),
    ("&#39;", "'"),
    ("&amp;", "&"),
)


def unescape_html_entities(markdown: str) -> str:
    """Decode the handful of HTML entities that leak through model passes.

    Plain ampersands in real content (``A&P``, ``R&I``) are untouched — only
    the escaped forms are decoded.
    """
    for entity, char in _HTML_ENTITIES:
        markdown = markdown.replace(entity, char)
    return markdown


# --------------------------------------------------------------------------- #
# Table hygiene (F5): span labels repeated into every cell by _expand_grid
# --------------------------------------------------------------------------- #

# A repeated body-row value shorter than this is legitimate data ("- - -"
# placeholder rows); banner rows are long category titles.
BANNER_CELL_MIN_CHARS = 20

_TABLE_SEPARATOR_LINE_RE = re.compile(r"^\|[\s:|-]*-[\s:|-]*\|?$")


def _table_row_cells(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or _TABLE_SEPARATOR_LINE_RE.match(stripped):
        return None
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def _render_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def dedupe_span_header_cells(markdown: str) -> str:
    """In table *header* rows, blank adjacent repeats of the same label.

    Expanding a colspan repeats its label into every covered cell — right for
    data cells (each row stays self-contained), noise in the header row
    ("Year ended December 31" x4-6, pages 46/48/50/86): the first cell keeps
    the label, the repeats become empty.
    """
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        if not _TABLE_SEPARATOR_LINE_RE.match(line.strip()) or index == 0:
            continue
        cells = _table_row_cells(lines[index - 1])
        if not cells:
            continue
        changed = False
        for cell_index in range(len(cells) - 1, 0, -1):
            if cells[cell_index] and cells[cell_index] == cells[cell_index - 1]:
                cells[cell_index] = ""
                changed = True
        if changed:
            lines[index - 1] = _render_row(cells)
    return "\n".join(lines) + ("\n" if markdown.endswith("\n") else "")


def collapse_banner_rows(markdown: str) -> str:
    """Collapse body rows whose every non-empty cell repeats one long value.

    A row-spanning banner ("MAIN COMPANIES CONSOLIDATED FOR THE FIRST TIME
    DURING THE YEAR" x6, page 43) gets its value once in the first cell, the
    rest blanked. Short repeated cells ("- | - | -") are legitimate data and
    are left alone. Column counts never change.
    """
    lines = markdown.splitlines()
    in_body = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("|"):
            in_body = False
            continue
        if _TABLE_SEPARATOR_LINE_RE.match(stripped):
            in_body = True
            continue
        if not in_body:
            continue
        cells = _table_row_cells(line)
        if not cells:
            continue
        non_empty = [cell for cell in cells if cell]
        if (
            len(non_empty) >= 2
            and len(set(non_empty)) == 1
            and len(non_empty[0]) >= BANNER_CELL_MIN_CHARS
        ):
            value = non_empty[0]
            lines[index] = _render_row([value] + [""] * (len(cells) - 1))
    return "\n".join(lines) + ("\n" if markdown.endswith("\n") else "")


# --------------------------------------------------------------------------- #
# Duplicated-content detection and removal (F2)
# --------------------------------------------------------------------------- #

# Below this normalized length a repeated block is treated as legitimate
# structure (short headings, "- 100%" list items), not duplicated content.
DUPLICATE_PARAGRAPH_MIN_CHARS = 80
DUPLICATE_PARAGRAPH_MIN_WORDS = 15


def _split_blocks(markdown: str) -> list[list[str]]:
    """Blank-line-separated blocks, each a list of raw lines."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in markdown.splitlines():
        if line.strip():
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _block_norm(block: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(block).lower()).strip()


def _block_is_table(block: list[str]) -> bool:
    return block[0].lstrip().startswith("|")


def _block_is_heading(block: list[str]) -> bool:
    return len(block) == 1 and bool(_HEADING_RE.match(block[0]))


def _block_is_image(block: list[str]) -> bool:
    return all(
        IMAGE_PLACEHOLDER_LINE_RE.match(line.strip())
        or line.strip().startswith("**Image summary:**")
        for line in block
    )


def _block_dedupe_eligible(block: list[str]) -> bool:
    if _block_is_table(block) or _block_is_image(block) or _block_is_heading(block):
        return False
    norm = _block_norm(block)
    return (
        len(norm) >= DUPLICATE_PARAGRAPH_MIN_CHARS
        or len(norm.split()) >= DUPLICATE_PARAGRAPH_MIN_WORDS
    )


def duplicate_paragraph_count(markdown: str) -> int:
    """Extra occurrences of long normalized paragraphs (repair-loss signal).

    Counts the sum of repeat occurrences of >=80-char paragraphs; table rows,
    image blocks, and headings are ignored.
    """
    counts: dict[str, int] = {}
    for block in _split_blocks(markdown):
        if _block_is_table(block) or _block_is_image(block) or _block_is_heading(block):
            continue
        norm = _block_norm(block)
        if len(norm) < DUPLICATE_PARAGRAPH_MIN_CHARS:
            continue
        counts[norm] = counts.get(norm, 0) + 1
    return sum(count - 1 for count in counts.values() if count > 1)


def collapse_duplicate_paragraphs(markdown: str) -> tuple[str, list[str]]:
    """Remove later occurrences of paragraphs already present on the page.

    Deterministic safety net for duplicated content from any source (observed:
    an accepted repair pass re-emitted a whole column, page 19). Rules:

    - a block repeats when its normalized text was already seen, or (>=80
      chars) is a suffix of an already-seen paragraph — the column duplication
      started mid-paragraph, producing a suffix copy;
    - table rows, image blocks, and short blocks (< 80 chars and < 15 words)
      are never touched, so legitimate repeated list items survive;
    - a heading immediately followed by a removed duplicate paragraph is
      removed with it when the same heading+paragraph pair appeared earlier.

    Returns the cleaned markdown and the removed paragraphs (as warnings).
    """
    blocks = _split_blocks(markdown)
    seen_norms: set[str] = set()
    seen_long: list[str] = []
    seen_pairs: set[tuple[str, str]] = set()
    removed: list[str] = []
    kept: list[list[str]] = []

    def _is_duplicate(norm: str) -> bool:
        if norm in seen_norms:
            return True
        if len(norm) >= DUPLICATE_PARAGRAPH_MIN_CHARS:
            return any(previous.endswith(norm) for previous in seen_long)
        return False

    index = 0
    while index < len(blocks):
        block = blocks[index]
        norm = _block_norm(block)

        if _block_is_heading(block) and index + 1 < len(blocks):
            nxt = blocks[index + 1]
            nxt_norm = _block_norm(nxt)
            if (
                _block_dedupe_eligible(nxt)
                and _is_duplicate(nxt_norm)
                and (norm, nxt_norm) in seen_pairs
            ):
                removed.append(norm)
                removed.append(nxt_norm)
                index += 2
                continue

        if _block_dedupe_eligible(block) and _is_duplicate(norm):
            removed.append(norm)
            index += 1
            continue

        if _block_dedupe_eligible(block):
            seen_norms.add(norm)
            if len(norm) >= DUPLICATE_PARAGRAPH_MIN_CHARS:
                seen_long.append(norm)
            if kept and _block_is_heading(kept[-1]):
                seen_pairs.add((_block_norm(kept[-1]), norm))
        kept.append(block)
        index += 1

    if not removed:
        return markdown, []
    text = "\n\n".join("\n".join(block) for block in kept).strip()
    return (text + "\n" if text else ""), removed


# --------------------------------------------------------------------------- #
# Page furniture (running headers/footers, sidebar chapter tabs)
# --------------------------------------------------------------------------- #

# A bare 1-2 digit number, single capital letter, or lone dash on its own line;
# a *run* of these after the page's images (or at the very end) is a sidebar
# chapter-tab / navigation column the VLM transcribed top-to-bottom. 3-digit
# numbers are deliberately excluded: they are real content (TOC page numbers
# like ``159``). A lone dash only strips as part of such a trailing run — a
# single ``-`` line, or a ``-`` in body prose, is never touched.
_TAB_LINE_RE = re.compile(r"^(?:\d{1,2}|[A-Z]|[-–—])$")
# A line that is exactly one image reference/placeholder (local copy: this
# module must stay stdlib-only, so it cannot import formatting's constant).
IMAGE_PLACEHOLDER_LINE_RE = re.compile(
    r"^(?:!\[[^\]]*\]\([^)]*\)|<!--\s*image\s*-->|\{\{DOC_IMAGE_[^}]+\}\})$",
    re.IGNORECASE,
)


def normalize_furniture_text(text: str) -> str:
    """Repetition signature for margin text: lowercase, digits stripped,
    whitespace collapsed — so ``4 DANONE - ... 2025`` and ``38 DANONE - ...
    2025`` share one signature across pages."""
    text = re.sub(r"\d+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def strip_furniture_lines(
    markdown: str, furniture_texts: set[str] | None = None
) -> str:
    """Remove page-furniture lines the models transcribed into the body.

    Two rules, both line-scoped so body prose is never touched:

    - a standalone line (or ``#``-heading line) whose normalized text equals a
      known furniture signature for this page is dropped;
    - a run of >= 2 consecutive standalone chapter-tab lines (bare 1-2 digit
      numbers or single capitals, blanks between them allowed) is dropped when
      it sits after the last image reference on the page or at the very end of
      the body. A single such line is never dropped.
    """
    texts = {t for t in (furniture_texts or set()) if t}
    lines = markdown.splitlines()

    def _line_normalized(line: str) -> str | None:
        stripped = line.strip()
        if not stripped or stripped.startswith("|"):
            return None
        return normalize_furniture_text(re.sub(r"^#{1,6}\s+", "", stripped))

    # A furniture text may span several markdown lines: Docling merges the
    # running header into one item ("OVERVIEW ... 1.6 Risk factors") while the
    # VLM emits it as two heading lines. Match 1..4 consecutive content lines
    # (concatenated) against each furniture text, not just single lines.
    kept: list[str] = []
    index = 0
    while index < len(lines):
        normalized = _line_normalized(lines[index])
        span_end = -1
        if normalized:
            parts = [normalized]
            last_content = index
            while len(parts) <= 4:
                if " ".join(parts) in texts:
                    span_end = last_content
                    break
                probe = last_content + 1
                while probe < len(lines) and not lines[probe].strip():
                    probe += 1
                if probe >= len(lines):
                    break
                nxt = _line_normalized(lines[probe])
                if not nxt:
                    break
                parts.append(nxt)
                last_content = probe
        if span_end >= 0:
            index = span_end + 1
            continue
        kept.append(lines[index])
        index += 1

    last_image_index = -1
    for index, line in enumerate(kept):
        if IMAGE_PLACEHOLDER_LINE_RE.match(line.strip()):
            last_image_index = index

    drop: set[int] = set()
    index = 0
    while index < len(kept):
        if not _TAB_LINE_RE.fullmatch(kept[index].strip()):
            index += 1
            continue
        run = [index]
        probe = index + 1
        while probe < len(kept):
            stripped = kept[probe].strip()
            if not stripped:
                probe += 1
                continue
            if _TAB_LINE_RE.fullmatch(stripped):
                run.append(probe)
                probe += 1
                continue
            break
        if len(run) >= 2:
            after_images = last_image_index >= 0 and run[0] > last_image_index
            at_end = all(not line.strip() for line in kept[run[-1] + 1 :])
            if after_images or at_end:
                drop.update(range(run[0], run[-1] + 1))
        index = probe if len(run) >= 2 else index + 1

    if drop:
        kept = [line for i, line in enumerate(kept) if i not in drop]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return text + "\n" if text else ""


# --------------------------------------------------------------------------- #
# Meta-commentary leaks (F9)
# --------------------------------------------------------------------------- #


def meta_commentary_warnings(markdown: str) -> list[str]:
    warnings: list[str] = []
    for i, line in enumerate(markdown.splitlines(), 1):
        for pat in META_COMMENTARY_PATTERNS:
            if pat.search(line):
                warnings.append(f"line {i}: meta-commentary -> {line.strip()[:80]}")
                break
    return warnings


def strip_meta_commentary(markdown: str) -> str:
    """Conservatively drop standalone process-commentary lines from final markdown.

    Only removes a line when it *both* matches a meta pattern *and* reads like a
    process note (starts with ``Note``/``>`` /``-`` and talks about the parse),
    so genuine content mentioning "image" is preserved.
    """
    out: list[str] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        low = stripped.lower().lstrip(">-* ")
        is_meta = any(pat.search(stripped) for pat in META_COMMENTARY_PATTERNS)
        looks_like_note = (
            low.startswith("note")
            or low.startswith("footnotes (as per")
            or "should be included" in low
            or "as per image" in low
            or "this text is preserved" in low
        )
        if is_meta and looks_like_note:
            continue
        out.append(line)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return text + "\n" if text else ""


# --------------------------------------------------------------------------- #
# Decoding-loop / repetition detection (F6)
# --------------------------------------------------------------------------- #

_PIPE_SEPARATOR_ROW_RE = re.compile(r"^\|?[\s|:-]+\|?$")


def detect_repeated_lines(text: str) -> tuple[float, bool]:
    """Return ``(repeated_ratio, is_anomalous)`` for a model output.

    ``repeated_ratio`` = 1 - unique/total over non-trivial lines. Anomalous when a
    single line repeats many times or the overall ratio is high on a long output.
    """
    physical_lines = [line.strip() for line in text.splitlines()]
    structural_rows: set[int] = set()
    for index, line in enumerate(physical_lines):
        if _PIPE_SEPARATOR_ROW_RE.fullmatch(line):
            structural_rows.add(index)
            if index and physical_lines[index - 1].startswith("|"):
                structural_rows.add(index - 1)
    lines = [
        line
        for index, line in enumerate(physical_lines)
        if index not in structural_rows and len(line) > 20
    ]
    total = len(lines)
    if total == 0:
        return 0.0, False
    counts: dict[str, int] = {}
    for ln in lines:
        counts[ln] = counts.get(ln, 0) + 1
    unique = len(counts)
    ratio = 1.0 - unique / total
    max_repeat = max(counts.values())
    anomalous = (total >= 8 and ratio > 0.5) or max_repeat >= 5
    return round(ratio, 3), anomalous
