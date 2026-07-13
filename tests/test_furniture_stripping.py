"""Checks for page-furniture detection and stripping (WP1).

Run from the repository root with ``python tests/test_furniture_stripping.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from document_extract.layout.furniture import (
    furniture_band,
    is_chapter_tab,
    page_furniture_texts,
    page_strip_texts,
    repeated_furniture_signatures,
)
from document_extract.markdown.postprocess import (
    normalize_furniture_text,
    strip_furniture_lines,
)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


HEADER = "OVERVIEW OF ACTIVITIES, RISK FACTORS"
TOP_RECT = [0.083, 0.034, 0.435, 0.045]
BODY_RECT = [0.1, 0.4, 0.5, 0.45]
RIGHT_RECT = [0.968, 0.506, 0.984, 0.524]


def check_signature_detection() -> None:
    pages = {
        1: [(HEADER, TOP_RECT), ("Body paragraph about packaging.", BODY_RECT)],
        2: [(HEADER, TOP_RECT), ("Another body paragraph.", BODY_RECT)],
        3: [(HEADER, TOP_RECT)],
    }
    signatures = repeated_furniture_signatures(pages)
    sig = (normalize_furniture_text(HEADER), "top")
    check(sig in signatures, "top-band item repeated on 3 pages is furniture")

    body_pages = {
        1: [(HEADER, BODY_RECT)],
        2: [(HEADER, BODY_RECT)],
        3: [(HEADER, BODY_RECT)],
    }
    check(
        not repeated_furniture_signatures(body_pages),
        "body-band repetition is not furniture",
    )

    one_page = {
        1: [(HEADER, TOP_RECT)],
        2: [("Different header", TOP_RECT)],
        3: [("Yet another", TOP_RECT)],
    }
    check(
        (normalize_furniture_text(HEADER), "top")
        not in repeated_furniture_signatures(one_page),
        "top-band item on a single page is not furniture",
    )

    texts = page_furniture_texts(pages[1], signatures)
    check(
        normalize_furniture_text(HEADER) in texts
        and len(texts) == 1,
        "page furniture texts contain only the banded repeated item",
    )


def check_section_start_protection() -> None:
    # Real case: pages 22/26/30 — "1.6 Risk factors" is a running-header line
    # on continuation pages, but page 22 carries the genuine section heading
    # with the same normalized text just below the band. The strip set for a
    # page must exclude texts that a kept (non-furniture) entry still uses.
    header_rect = [0.083, 0.052, 0.177, 0.061]  # in the top band
    real_rect = [0.084, 0.096, 0.337, 0.115]  # below the band
    pages = {
        22: [("1.6 Risk factors", header_rect), ("1.6 RISK FACTORS", real_rect)],
        26: [("1.6 Risk factors", header_rect)],
        30: [("1.6 Risk factors", header_rect)],
    }
    signatures = repeated_furniture_signatures(pages)
    check(
        (normalize_furniture_text("1.6 Risk factors"), "top") in signatures,
        "running section header repeated on 3 pages is a signature",
    )
    check(
        not page_strip_texts(pages[22], signatures),
        "section-start page keeps its genuine heading text",
    )
    check(
        normalize_furniture_text("1.6 Risk factors")
        in page_strip_texts(pages[26], signatures),
        "continuation page still strips the running header text",
    )
    letterless = {
        1: [("3.3", TOP_RECT)],
        2: [("3.3", TOP_RECT)],
        3: [("3.3", TOP_RECT)],
    }
    check(
        not repeated_furniture_signatures(letterless),
        "letterless (digit-only) texts never become signatures",
    )


def check_footer_normalization() -> None:
    a = normalize_furniture_text("4 DANONE - UNIVERSAL REGISTRATION DOCUMENT 2025")
    b = normalize_furniture_text("38 DANONE - UNIVERSAL REGISTRATION DOCUMENT 2025")
    check(a == b and a, "footer signature is page-number invariant")


def check_chapter_tabs() -> None:
    check(is_chapter_tab("4", RIGHT_RECT), "right-band digit is a chapter tab")
    check(is_chapter_tab("A", RIGHT_RECT), "right-band capital is a chapter tab")
    check(not is_chapter_tab("4", BODY_RECT), "body digit is not a chapter tab")
    check(not is_chapter_tab("42abc", RIGHT_RECT), "right-band word is not a tab")
    check(furniture_band([0.1, 0.95, 0.4, 0.99]) == "bottom", "bottom band detected")


def check_strip_tab_runs() -> None:
    # Real case: page_0047 — the VLM transcribed the sidebar tab column
    # ``4 5 6 7`` after the page's last image reference.
    markdown = (
        "Body text about Latin America.\n\n"
        "![Picture p0047-i001](images/picture_p0047_i001.png)\n"
        "4\n5\n6\n7\n\n"
        "## Unplaced content\n\n- Year ended December 31\n"
    )
    stripped = strip_furniture_lines(markdown, set())
    for tab in ("4", "5", "6", "7"):
        check(
            f"\n{tab}\n" not in stripped,
            f"tab line {tab!r} removed after image reference",
        )
    check("Body text about Latin America." in stripped, "body text kept")
    check("- Year ended December 31" in stripped, "unplaced bullet kept")

    # Runs with blank lines between the tabs are still one run.
    spaced = "Intro.\n\n![img](x.png)\n\n4\n\n5\n\n6\n\n7\n"
    stripped = strip_furniture_lines(spaced, set())
    check(
        all(f"\n{t}\n" not in stripped for t in ("4", "5", "6", "7")),
        "blank-separated tab run removed",
    )


def check_strip_protections() -> None:
    # Real case: page_0065 — TOC page numbers ``159`` must survive.
    markdown = "## SECTION\n\n159\n159\n\n- item\n"
    check(
        strip_furniture_lines(markdown, set()).count("159") == 2,
        "3-digit lines are never treated as tab runs",
    )

    lone = "Some text.\n\n2\n\nMore text.\n"
    check(
        "\n2\n" in strip_furniture_lines(lone, set()),
        "a lone bare digit in the body is kept",
    )

    end_run = "Some text.\n\n4\n5\n"
    stripped = strip_furniture_lines(end_run, set())
    check(
        "\n4\n" not in stripped and "\n5\n" not in stripped,
        "tab run at the end of the body removed without an image anchor",
    )

    mid_run = "Some text.\n\n4\n5\n\nMore text after.\n"
    stripped = strip_furniture_lines(mid_run, set())
    check(
        "\n4\n" in stripped and "\n5\n" in stripped,
        "mid-body digit run without image anchor kept",
    )


def check_strip_named_furniture() -> None:
    texts = {normalize_furniture_text(HEADER)}
    markdown = f"## {HEADER}\n\nReal paragraph.\n\n{HEADER}\n"
    stripped = strip_furniture_lines(markdown, texts)
    check(HEADER not in stripped, "furniture heading and plain line removed")
    check("Real paragraph." in stripped, "body paragraph kept")

    kept = strip_furniture_lines(f"## {HEADER}\n\nReal paragraph.\n", set())
    check(HEADER in kept, "heading kept when not in the furniture set")

    footer = "Body.\n\n4 DANONE - UNIVERSAL REGISTRATION DOCUMENT 2025\n"
    stripped = strip_furniture_lines(
        footer,
        {normalize_furniture_text("38 DANONE - UNIVERSAL REGISTRATION DOCUMENT 2025")},
    )
    check("REGISTRATION DOCUMENT" not in stripped, "footer line removed via signature")

    table = f"| {HEADER} | 5 |\n|---|---|\n| a | b |\n"
    check(
        HEADER in strip_furniture_lines(table, texts),
        "table rows are never stripped",
    )

    # Real case: pages 31/33 — Docling merges the running header into ONE item
    # ("OVERVIEW ... 1.6 Risk factors") while the VLM writes it as two heading
    # lines; the strip must match the concatenation.
    combined = {normalize_furniture_text(f"{HEADER} 1.6 Risk factors")}
    two_lines = f"# {HEADER}\n\n## 1.6 Risk factors\n\nBody paragraph.\n"
    stripped = strip_furniture_lines(two_lines, combined)
    check(
        HEADER not in stripped and "Risk factors" not in stripped,
        "split running header matched across two lines",
    )
    check("Body paragraph." in stripped, "body kept after span match")
    partial = f"# {HEADER}\n\nUnrelated line.\n"
    check(
        HEADER in strip_furniture_lines(partial, combined),
        "prefix alone does not match a combined signature",
    )


def check_right_edge_dash() -> None:
    # Real case: page 193 — bare ``-`` navigation marks sit in the right sidebar
    # band. They are furniture only there; a ``-`` anywhere else is content.
    check(is_chapter_tab("-", RIGHT_RECT), "right-band lone dash is navigation furniture")
    check(is_chapter_tab("–", RIGHT_RECT), "right-band en-dash is navigation furniture")
    check(not is_chapter_tab("-", BODY_RECT), "body lone dash is not furniture")

    # A trailing run mixing dashes and tabs after the last image is stripped.
    markdown = (
        "Body paragraph.\n\n"
        "![Picture p0193-i001](images/picture_p0193_i001.png)\n"
        "-\n-\n-\n"
    )
    stripped = strip_furniture_lines(markdown, set())
    check("\n-\n" not in stripped and not stripped.rstrip().endswith("-"), "trailing dash run removed")
    check("Body paragraph." in stripped, "body kept alongside the dash run")

    # A real list bullet and a lone dash in prose are never stripped.
    kept = strip_furniture_lines("Intro.\n\n- a genuine bullet\n\nOutro.\n", set())
    check("- a genuine bullet" in kept, "a real list bullet with content is preserved")
    lone = strip_furniture_lines("Some text.\n\n-\n\nMore text.\n", set())
    check("\n-\n" in lone, "a single lone dash in the body is kept (run < 2)")


def main() -> int:
    check_signature_detection()
    check_section_start_protection()
    check_footer_normalization()
    check_chapter_tabs()
    check_strip_tab_runs()
    check_strip_protections()
    check_strip_named_furniture()
    check_right_edge_dash()
    print("test_furniture_stripping: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
