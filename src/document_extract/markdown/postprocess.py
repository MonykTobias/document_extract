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

# Numeric footnote reference / definition, e.g. ``[1]``.
_FOOTNOTE_MARKER_RE = re.compile(r"\[(\d{1,3})\]")
_FOOTNOTE_DEF_LINE_RE = re.compile(r"^\s*\[(\d{1,3})\]\s+\S")
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


def normalize_bullets_and_headings(markdown: str) -> str:
    """Normalize unicode bullets to ``- `` (splitting inline bullets), and ensure
    headings are separated by blank lines so lists/headings render correctly."""
    out_lines: list[str] = []
    for line in markdown.splitlines():
        if any(ch in line for ch in BULLET_CHARS):
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
    refined_lines = [ln.strip() for ln in refined_flat.splitlines() if ln.strip()]
    line_words = [set(_significant_words(ln)) for ln in refined_lines]
    line_nums = [_numeric_tokens(ln) for ln in refined_lines]
    windows: list[tuple[set[str], set[str]]] = []
    for i in range(len(line_words)):
        words: set[str] = set()
        nums: set[str] = set()
        for j in range(i, min(i + window, len(line_words))):
            words |= line_words[j]
            nums |= line_nums[j]
        windows.append((words, nums))
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


def detect_repeated_lines(text: str) -> tuple[float, bool]:
    """Return ``(repeated_ratio, is_anomalous)`` for a model output.

    ``repeated_ratio`` = 1 - unique/total over non-trivial lines. Anomalous when a
    single line repeats many times or the overall ratio is high on a long output.
    """
    lines = [ln.strip() for ln in text.splitlines() if len(ln.strip()) > 20]
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
