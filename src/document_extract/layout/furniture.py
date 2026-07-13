"""Page-furniture detection: running headers, footers, sidebar chapter tabs.

Furniture is margin content repeated across pages (running headers/footers)
or page-edge navigation marks (chapter tab digits/letters). It enters the
output through two routes — Docling emits some of it as ordinary body items,
and the refine/repair VLM transcribes it from the page image — so detection
is split: this module holds the pure geometry/repetition logic shared by the
pipeline (Docling items) and the offline replay harness (saved layout maps).

All functions are pure and Docling-free: they operate on ``(text, rect)``
pairs where ``rect`` is a normalized ``[left, top, right, bottom]`` rectangle
with a top-left origin (the shape ``bbox_to_normalized_rect`` produces).
"""

from __future__ import annotations

import math
import re

from ..markdown.postprocess import normalize_furniture_text

# Edge bands as fractions of the page. An item whose box sits in one of these
# bands is a furniture *candidate*; repetition across pages confirms it.
FURNITURE_BAND_FRACTION = 0.08
# A banded signature seen on at least this many pages is furniture...
FURNITURE_MIN_PAGES = 3
# ...unless the run is short, where a page-share threshold applies instead.
FURNITURE_SMALL_RUN_PAGES = 12
FURNITURE_SMALL_RUN_SHARE = 0.25

# Sidebar chapter tabs: a bare 1-2 digit number or single capital letter at
# the right page edge ("1".."7", "A"). These are furniture even without
# cross-page repetition.
CHAPTER_TAB_RE = re.compile(r"^(?:\d{1,2}|[A-Z])$")
# A bare dash (hyphen/en/em) is only ever furniture at the right page edge — a
# navigation mark in the sidebar band. Everywhere else a lone "-" is content
# (an empty list bullet, a value placeholder) and must be retained.
RIGHT_EDGE_DASH_RE = re.compile(r"^[-–—]$")


def furniture_band(rect: list[float] | None) -> str:
    """Classify a normalized rect into ``top``/``bottom``/``right``/``body``."""
    if not rect or len(rect) < 4:
        return "body"
    left, top, _, bottom = rect[0], rect[1], rect[2], rect[3]
    if top <= FURNITURE_BAND_FRACTION:
        return "top"
    if bottom >= 1.0 - FURNITURE_BAND_FRACTION:
        return "bottom"
    if left >= 1.0 - FURNITURE_BAND_FRACTION:
        return "right"
    return "body"


def is_chapter_tab(text: str, rect: list[float] | None) -> bool:
    if furniture_band(rect) != "right":
        return False
    stripped = text.strip()
    return bool(CHAPTER_TAB_RE.fullmatch(stripped) or RIGHT_EDGE_DASH_RE.fullmatch(stripped))


def _signature(text: str, rect: list[float] | None) -> tuple[str, str] | None:
    band = furniture_band(rect)
    if band == "body":
        return None
    normalized = normalize_furniture_text(text)
    # Digit stripping collapses numeric fragments ("3.3", "5.7") into
    # letterless signatures ("."), which would then match unrelated lines
    # across pages. Real running headers/footers always contain words.
    if not normalized or not re.search(r"[^\W\d_]", normalized):
        return None
    return (normalized, band)


def repeated_furniture_signatures(
    pages_entries: dict[int, list[tuple[str, list[float] | None]]],
) -> set[tuple[str, str]]:
    """``(normalized_text, band)`` signatures repeated across enough pages.

    ``pages_entries`` maps a page number to that page's ``(text, rect)``
    pairs. A signature counts once per page; repetition across pages (not
    within one page) is what marks margin text as furniture.
    """
    page_count = len(pages_entries)
    if page_count < 2:
        return set()
    threshold = (
        FURNITURE_MIN_PAGES
        if page_count >= FURNITURE_SMALL_RUN_PAGES
        else max(2, math.ceil(FURNITURE_SMALL_RUN_SHARE * page_count))
    )
    seen_on_pages: dict[tuple[str, str], set[int]] = {}
    for page, entries in pages_entries.items():
        for text, rect in entries:
            sig = _signature(text, rect)
            if sig is not None:
                seen_on_pages.setdefault(sig, set()).add(page)
    return {sig for sig, pages in seen_on_pages.items() if len(pages) >= threshold}


def page_furniture_texts(
    entries: list[tuple[str, list[float] | None]],
    signatures: set[tuple[str, str]],
) -> set[str]:
    """Normalized texts of this page's entries that match a furniture signature."""
    texts: set[str] = set()
    for text, rect in entries:
        sig = _signature(text, rect)
        if sig is not None and sig in signatures:
            texts.add(sig[0])
    return texts


def page_strip_texts(
    entries: list[tuple[str, list[float] | None]],
    signatures: set[tuple[str, str]],
    extra_texts: set[str] | None = None,
) -> set[str]:
    """The furniture texts safe to strip from this page's markdown by text alone.

    A section's *first* page carries a real heading whose normalized text
    equals the running header seen on the continuation pages (observed:
    page 22's ``1.6 RISK FACTORS``). Text-level stripping cannot see bands, so
    a text qualifies only when no non-furniture entry on the page shares it —
    otherwise the stripped line could be the genuine heading.
    """
    strip: set[str] = {t for t in (extra_texts or set()) if t}
    kept: set[str] = set()
    for text, rect in entries:
        sig = _signature(text, rect)
        if sig is not None and sig in signatures:
            strip.add(sig[0])
            continue
        if is_chapter_tab(text, rect):
            continue
        normalized = normalize_furniture_text(text)
        if normalized:
            kept.add(normalized)
    return strip - kept


__all__ = [
    "FURNITURE_BAND_FRACTION", "FURNITURE_MIN_PAGES", "CHAPTER_TAB_RE",
    "RIGHT_EDGE_DASH_RE",
    "furniture_band", "is_chapter_tab", "repeated_furniture_signatures",
    "page_furniture_texts", "page_strip_texts",
]
