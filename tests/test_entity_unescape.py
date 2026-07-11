"""Checks for HTML-entity unescaping (WP6).

Run from the repository root with ``python tests/test_entity_unescape.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from document_extract.markdown.postprocess import unescape_html_entities


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


def main() -> int:
    # Real case: page 49.
    check(
        unescape_html_entities("brands like Michel &amp; Augustin and others")
        == "brands like Michel & Augustin and others",
        "&amp; decoded",
    )
    check(
        unescape_html_entities("A&P spend and R&I investments")
        == "A&P spend and R&I investments",
        "legit ampersands untouched",
    )
    check(
        unescape_html_entities("&lt;1% and &gt;3.5 stars, &quot;quoted&quot;, it&#39;s, a&nbsp;gap")
        == '<1% and >3.5 stars, "quoted", it\'s, a gap',
        "lt/gt/quot/apos/nbsp decoded",
    )
    check(
        unescape_html_entities("&amp;lt;") == "&lt;",
        "double-escaped entity resolves one level only",
    )
    print("test_entity_unescape: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
