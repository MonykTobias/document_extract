"""Checks for the repair duplication guard and paragraph dedupe (WP2).

The fixture paragraphs are the exact texts from the accepted-but-broken
repair pass on danoneurdaccessible page_0019, where the repair re-emitted a
whole column: one suffix-partial copy plus exact copies of the following
paragraphs and a heading.

Run from the repository root with ``python tests/test_duplicate_guard.py``.
"""

from __future__ import annotations

from pathlib import Path


from document_extract.markdown.postprocess import (
    collapse_duplicate_paragraphs,
    duplicate_paragraph_count,
)
from document_extract.refinement import repair_regression_reasons


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


# Exact texts from outputs/danoneurdaccessible/page_0019/page_repair.md.
FULL_PARA = (
    "The Danone Experts Program is a global framework created by The Group to "
    "recognize and develop strategic expertise within the company. Its ambition "
    "is to ensure that critical scientific, technical, and functional knowledge "
    "is identified, valued, and maintained as a key competitive advantage. The "
    "Program's Fellows and Masters, highly regarded in their fields, publish "
    "regularly with academic peers. In 2025, two members were appointed to "
    "distinguished positions: one to the board of the Soleil Synchrotron (a "
    "scientific infrastructure), and another as Vice President of the Steering "
    "Committee for Ferments du Futur. A third member was promoted to the Ordre "
    "du Mérite Agricole, one of France's most prestigious distinctions."
)
# The duplicated column starts mid-paragraph: a suffix copy of FULL_PARA.
SUFFIX_PARA = FULL_PARA[FULL_PARA.index("competitive advantage.") :]
DANEXPERTS_PARA = (
    "The DanExperts Program ensures that recognition goes beyond awards and "
    "honors by fostering the transfer of knowledge, experience, and skill "
    "across generations in areas critical to Danone's innovation strategy. "
    "Fellows possess world-class expertise with significant internal and "
    "external impact, while Masters bring deep technical and scientific "
    "knowledge validated by functional boards. In 2025, the program also "
    "appointed its first Explorers: exceptional R&I talent beginning their "
    "journey toward mastery."
)
HEADING = "## Product Superiority Program acceleration"
SUPERIORITY_PARA = (
    "Over the past four years, Danone has accelerated its Superiority Program "
    "across all zones and Categories, more than doubling the number of product "
    "consumer tests compared to 2021."
)

PRE_MARKDOWN = (  # single-occurrence version (page_vlm.md shape)
    "## Scientific Recognition, Awards & Honors\n\n"
    f"{FULL_PARA}\n\n{DANEXPERTS_PARA}\n\n{HEADING}\n\n{SUPERIORITY_PARA}\n"
)
REPAIRED_MARKDOWN = (  # duplicated-column version (page_repair.md shape)
    "## Scientific Recognition, Awards & Honors\n\n"
    f"{FULL_PARA}\n\n{DANEXPERTS_PARA}\n\n{HEADING}\n\n{SUPERIORITY_PARA}\n\n"
    "## 1.4 Other elements related to Danone's activity and organization\n\n"
    f"{SUFFIX_PARA}\n\n{DANEXPERTS_PARA}\n\n{HEADING}\n\n{SUPERIORITY_PARA}\n"
)


def check_duplicate_count() -> None:
    check(duplicate_paragraph_count(PRE_MARKDOWN) == 0, "pre markdown has no dupes")
    check(
        duplicate_paragraph_count(REPAIRED_MARKDOWN) >= 2,
        "repaired markdown counts duplicated paragraphs",
    )
    table = "| a | b |\n|---|---|\n| " + "x" * 100 + " | y |\n"
    check(
        duplicate_paragraph_count(table + "\n" + table) == 0,
        "table rows never count as duplicate paragraphs",
    )


def check_regression_reason() -> None:
    reasons = repair_regression_reasons(PRE_MARKDOWN, REPAIRED_MARKDOWN, usage={})
    check(
        "duplicate_content_added" in reasons,
        "repair adding duplicated content is rejected",
    )
    reasons = repair_regression_reasons(PRE_MARKDOWN, PRE_MARKDOWN, usage={})
    check(
        "duplicate_content_added" not in reasons,
        "identical repair not flagged for duplication",
    )


def check_collapse() -> None:
    collapsed, removed = collapse_duplicate_paragraphs(REPAIRED_MARKDOWN)
    check(
        collapsed.count("The DanExperts Program ensures") == 1,
        "DanExperts paragraph kept exactly once",
    )
    check(
        "competitive advantage. The Program's Fellows" not in collapsed.split(FULL_PARA)[1],
        "suffix-partial column copy removed",
    )
    check(collapsed.count(HEADING) == 1, "duplicated heading+paragraph pair collapsed")
    check(len(removed) >= 3, "removed paragraphs reported as warnings")

    untouched, removed = collapse_duplicate_paragraphs(PRE_MARKDOWN)
    check(untouched == PRE_MARKDOWN and not removed, "clean page untouched")


def check_collapse_protections() -> None:
    rows = "| Total | 100% |\n|---|---|\n| Total | 100% |\n"
    kept, removed = collapse_duplicate_paragraphs(rows)
    check(kept == rows and not removed, "identical table cells kept")

    items = "- 100%\n\ntext\n\n- 100%\n"
    kept, removed = collapse_duplicate_paragraphs(items)
    check(kept == items and not removed, "repeated short list items kept")

    shared_prefix = (
        f"{'Alpha ' * 20}one specific ending here.\n\n"
        f"{'Alpha ' * 20}a different ending entirely.\n"
    )
    kept, removed = collapse_duplicate_paragraphs(shared_prefix)
    check(not removed, "two different paragraphs sharing a prefix kept")

    images = "![Picture p0019-i001](images/x.png)\n\ntext\n\n![Picture p0019-i001](images/x.png)\n"
    kept, removed = collapse_duplicate_paragraphs(images)
    check(not removed, "image reference blocks never collapsed")


def main() -> int:
    check_duplicate_count()
    check_regression_reason()
    check_collapse()
    check_collapse_protections()
    print("test_duplicate_guard: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
