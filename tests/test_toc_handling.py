"""Checks for TOC detection and section-number restoration (WP7).

Fixtures mirror the danoneurdaccessible raw TOC tables (pages 2/7) and the
KEY FIGURES financial table (page 46) as the negative.

Run from the repository root with ``python tests/test_toc_handling.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from document_extract.markdown.postprocess import (
    looks_like_toc,
    restore_toc_section_numbers,
)
from document_extract.refinement import postprocess_markdown


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


TOC_RAW = (
    "## Contents\n\n"
    "| OVERVIEW OF ACTIVITIES, RISK FACTORS | OVERVIEW OF ACTIVITIES, RISK FACTORS |  |\n"
    "|---|---|---|\n"
    "| 1.1 | Presentation of Danone | 6 |\n"
    "| 1.2 | Strategic Priorities | 7 |\n"
    "| 1.3 | Description and Strategy of the Zones | 11 |\n"
    "| 2.1 | Business highlights in 2025 | 42 |\n"
    "| 2.2 | Consolidated net income review | 45 |\n"
    "| 2.3 | Free cash flow | 52 |\n"
    "| 2.7 Documents available to the public | 2.7 Documents available to the public | 61 |\n"
    "| 3.1 | Consolidated financial statements and Notes | 64 |\n"
)
FINANCIAL_RAW = (
    "## KEY FIGURES\n\n"
    "|  | Year ended December 31 | Year ended December 31 |\n"
    "|---|---|---|\n"
    "| (in € millions) | 2024 | 2025 |\n"
    "| Sales | 27,376 | 27,283 |\n"
    "| Recurring operating income | 3,558 | 3,665 |\n"
    "| Operating income | 3,379 | 2,940 |\n"
    "| Net income | 2,580 | 2,020 |\n"
    "| Free cash flow | 3,100 | 3,000 |\n"
    "| Dividend | 2.10 | 2.15 |\n"
    "| Shares | 646 | 646 |\n"
    "| Employees | 90 | 89 |\n"
)


def check_detection() -> None:
    check(looks_like_toc(TOC_RAW), "TOC table page detected")
    check(not looks_like_toc(FINANCIAL_RAW), "financial table page not a TOC")
    small = "\n".join(
        f"| 3.{i} | SECTION TITLE {i} | {60 + i} |" for i in range(1, 9)
    )
    check(
        looks_like_toc("| A | B | C |\n|---|---|---|\n" + small),
        "small chapter-divider TOC detected via numbered-row share",
    )
    check(not looks_like_toc("Some prose.\n\nMore prose.\n"), "prose page not a TOC")


def check_restoration() -> None:
    # The VLM flattened "2.1 Business highlights in 2025" into "2. ..." etc.
    vlm = (
        "## Contents\n\n"
        "1. OVERVIEW OF ACTIVITIES, RISK FACTORS\n"
        "   - 1.1 Presentation of Danone\n"
        "2. Business highlights in 2025\n"
        "3. Consolidated net income review\n"
        "4. Free cash flow\n"
        "5. Documents available to the public\n"
    )
    restored = restore_toc_section_numbers(vlm, TOC_RAW)
    check("- 2.1 Business highlights in 2025" in restored, "2.1 restored from raw")
    check("- 2.2 Consolidated net income review" in restored, "2.2 restored from raw")
    check("- 2.3 Free cash flow" in restored, "2.3 restored from raw")
    check(
        "- 2.7 Documents available to the public" in restored,
        "inline number+title cell restored",
    )
    check(
        "   - 1.1 Presentation of Danone" in restored,
        "already-correct unordered entries untouched",
    )
    check(
        "- OVERVIEW OF ACTIVITIES, RISK FACTORS" in restored,
        "unmatched entry keeps title, drops fabricated number",
    )
    check(
        not any(line.strip().startswith(("2.", "3.", "4.", "5."))
                and line.strip()[1] == "." for line in restored.splitlines()
                if line.strip() and line.strip()[0].isdigit()),
        "no ordered-list markers remain (renderers cannot renumber)",
    )


def check_guard_suppression() -> None:
    vlm = "## Contents\n\n1. Presentation of Danone\n2. Strategic Priorities\n"
    final, warnings = postprocess_markdown(TOC_RAW, vlm, [], page_role="toc")
    check("## Unplaced content" not in final, "no unplaced dump on TOC pages")
    check(
        warnings.get("unplaced_suppressed_toc") is True,
        "suppression recorded in warnings",
    )
    check("- 1.1 Presentation of Danone" in final, "section number restored in chain")

    final, warnings = postprocess_markdown(TOC_RAW, vlm, [], page_role=None)
    check("## Unplaced content" in final, "guard still active on non-TOC pages")


def main() -> int:
    check_detection()
    check_restoration()
    check_guard_suppression()
    print("test_toc_handling: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
