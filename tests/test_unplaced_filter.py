"""Checks for unplaced-content filtering and uncertain-mapping routing (WP3).

Fixture lines are the exact strings from the danoneurdaccessible run
(pages 5, 58, 86). Run with ``python tests/test_unplaced_filter.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from document_extract.markdown.postprocess import (
    extract_unplaced_sections,
    filter_unplaced_lines,
    normalize_furniture_text,
)
from document_extract.models import PictureRecord
from document_extract.refinement import apply_completeness_guard, postprocess_markdown


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


REPEATED = (
    "Year ended December 31     Year ended December 31     "
    "Year ended December 31       Year ended December 31"
)
KPI_LABELS = "LIKE-FOR-LIKE SALES GROWTH   VOLUME/ MIX      RECURRING OPERATING MARGIN"
KPI_BODY = (
    "## 2025 Highlights\n\n"
    "LIKE-FOR-LIKE SALES GROWTH\n\n+4.6%\n\n"
    "VOLUME/ MIX\n\n+1.9%\n\n"
    "RECURRING OPERATING MARGIN\n\n13.4%\n"
)
RATING_LINE = "Rating                  -         A-2                   -         A-2"
RATING_BODY = (
    "## Financial security\n\n"
    "| Agency | Short-term | Long-term |\n|---|---|---|\n"
    "| Moody's | P-2 | Baa1 |\n| S&P | | BBB+ |\n"
)


def _symbol_record(value: str = "S4") -> PictureRecord:
    return PictureRecord(
        page=1,
        index=1,
        placeholder="{{DOC_IMAGE_p0001_i001}}",
        rel_path="images/picture_p0001_i001.png",
        abs_path=None,
        bbox=None,
        area_ratio=0.001,
        classification="",
        caption="",
        summary_type="symbol",
        summary=value,
    )


def check_repeated_phrase() -> None:
    kept, dropped = filter_unplaced_lines([REPEATED], "Body text.\n")
    check(dropped == [REPEATED] and not kept, "repeated-phrase fragment dropped")
    two_cols = "Description        Management measures"
    kept, dropped = filter_unplaced_lines([two_cols], "Body text.\n")
    check(kept == [two_cols], "distinct columns are not a repeated phrase")


def check_fuzzy_present() -> None:
    kept, dropped = filter_unplaced_lines([KPI_LABELS], KPI_BODY)
    check(dropped == [KPI_LABELS] and not kept, "KPI label composite already present")
    kept, dropped = filter_unplaced_lines([KPI_LABELS], "Unrelated body.\n")
    check(kept == [KPI_LABELS], "labels kept when body lacks them")
    numbers = "(in € millions except %)      2024      2025"
    body = "| (in € millions except %) | 2024 | 2025 |\n|---|---|---|\n| Sales | 1 | 2 |\n"
    kept, dropped = filter_unplaced_lines([numbers], body)
    check(dropped == [numbers], "flattened table header composite dropped")


def check_keep_real_data() -> None:
    kept, dropped = filter_unplaced_lines([RATING_LINE], RATING_BODY)
    check(kept == [RATING_LINE], "Rating A-2 line kept when body lacks A-2")
    body_with = RATING_BODY.replace("| S&P | | BBB+ |", "| S&P | A-2 | BBB+ |") + (
        "\nRating agencies review Danone regularly.\n"
    )
    kept, dropped = filter_unplaced_lines([RATING_LINE], body_with)
    check(dropped == [RATING_LINE], "Rating line dropped once A-2 is in the body")


def check_furniture_rule() -> None:
    line = "OVERVIEW OF ACTIVITIES, RISK FACTORS 1.2 Strategic Priorities"
    furniture = {normalize_furniture_text(line)}
    kept, dropped = filter_unplaced_lines([line], "Body.\n", furniture)
    check(dropped == [line], "furniture line dropped from unplaced flow")


def check_empty_section_not_emitted() -> None:
    raw = "Heading\n\nSame paragraph on both sides with enough words to count.\n"
    final, warnings = apply_completeness_guard(raw, raw)
    check("## Unplaced content" not in final, "no unplaced section when nothing kept")
    check(
        warnings["content_loss_guard_triggered"] is False,
        "guard not triggered when everything filtered/present",
    )
    raw2 = raw + "\nUnique missing sentence about polar bears and glaciers.\n"
    final, warnings = apply_completeness_guard(raw2, raw)
    check("## Unplaced content" in final, "section emitted for a real missing line")
    check(
        warnings["unplaced_content_lines"]
        == ["Unique missing sentence about polar bears and glaciers."],
        "kept lines recorded in the warning",
    )


def check_uncertain_block_routing() -> None:
    working = (
        "Body paragraph that stays.\n\n"
        "## Uncertain mappings\n\n"
        "![Picture p0057-i002](images/picture_p0057_i002.png)\n"
        "1\n4\n5\n\n"
        "A genuinely uncertain content line with numbers 42%.\n"
    )
    final, warnings = postprocess_markdown("Body paragraph that stays.\n", working, [])
    check("## Uncertain mappings" not in final, "uncertain block removed from final")
    check("uncertain_mappings" in warnings, "uncertain block captured as warning")
    check(
        "![Picture p0057-i002](images/picture_p0057_i002.png)" in final,
        "image reference returned to the body",
    )
    check("\n1\n" not in final and "\n4\n" not in final, "bare tab digits dropped")
    check(
        "A genuinely uncertain content line with numbers 42%." in final
        and "## Unplaced content" in final,
        "uncertain content line preserved via unplaced flow",
    )


def check_vlm_unplaced_sections_are_recycled_or_dropped() -> None:
    untouched = "# Title\n\nBody remains.\n"
    extracted, entries = extract_unplaced_sections(untouched)
    check(extracted == untouched and entries == [], "pages without unplaced sections stay byte-identical")

    record = _symbol_record()
    working = (
        "Body remains.\n\n"
        "## Unplaced content\n\n"
        "(none)\n"
        "- ![S4](#)\n"
        "- A genuinely missing prose detail with value 42%.\n"
    )
    final, warnings = postprocess_markdown("Body remains.\n", working, [record])
    check("(none)" not in final, "literal empty unplaced filler is removed")
    check("![S4](#)" not in final, "placed-symbol unplaced entry is removed")
    check(final.count("## Unplaced content") == 1, "the guard is the sole final section author")
    check(
        "- A genuinely missing prose detail with value 42%." in final,
        "genuine VLM unplaced prose is recycled through the guard",
    )
    check(
        warnings.get("vlm_unplaced_sections") == {
            "removed": 1,
            "recycled": 1,
            "dropped": 2,
        },
        "unplaced-section warning records removed, recycled, and dropped entries",
    )

    stripped, stripped_warnings = postprocess_markdown(
        "Body remains.\n", "Body remains.\n\n![S4](#)\n", [record]
    )
    check("![S4](#)" not in stripped, "loose symbol link is stripped from body output")
    check(
        stripped_warnings.get("loose_symbol_lines_stripped") == 1,
        "loose-symbol warning records the deterministic cleanup",
    )

    toc_final, toc_warnings = postprocess_markdown(
        "Body remains.\n",
        "Body remains.\n\n## Unplaced content\n\n- TOC-only missing detail.\n",
        [],
        page_role="toc",
    )
    check("TOC-only missing detail" not in toc_final, "TOC guard suppresses recycled unplaced prose")
    check(
        toc_warnings.get("vlm_unplaced_sections") == {
            "removed": 1,
            "recycled": 0,
            "dropped": 1,
        },
        "TOC-suppressed survivor is counted as dropped",
    )


def main() -> int:
    check_repeated_phrase()
    check_fuzzy_present()
    check_keep_real_data()
    check_furniture_rule()
    check_empty_section_not_emitted()
    check_uncertain_block_routing()
    check_vlm_unplaced_sections_are_recycled_or_dropped()
    print("test_unplaced_filter: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
