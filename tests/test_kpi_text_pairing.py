"""Regression checks for deterministic loose KPI label/value pairing."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from document_extract.markdown.postprocess import (
    _classify_kpi_text_line,
    is_kpi_label_text,
    is_letter_rating,
    is_value_text,
    pair_kpi_text_runs,
)
from document_extract.refinement import apply_completeness_guard, postprocess_markdown


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


PANEL_ONE = (
    "### FINANCIAL INDICATORS\n\n"
    "+4.5%\nLIKE-FOR-LIKE SALES GROWTH\n"
    "+2.7%\nVOLUME / MIX\n"
    "13.4%\nRECURRING OPERATING MARGIN\n"
    "+4.6%\nRECURRING EPS\n"
    "\u20ac2.8 bn\nFREE CASH FLOW\n"
    "2.25\u20ac\nDIVIDEND PER SHARE\n\n"
)
PANEL_ONE_LINES = [
    "- LIKE-FOR-LIKE SALES GROWTH: +4.5%",
    "- VOLUME / MIX: +2.7%",
    "- RECURRING OPERATING MARGIN: 13.4%",
    "- RECURRING EPS: +4.6%",
    "- FREE CASH FLOW: \u20ac2.8 bn",
    "- DIVIDEND PER SHARE: 2.25\u20ac",
]
PANEL_TWO = (
    "AAA (a) AWARDED BY CDP\n"
    "98.0% EMPLOYEES COVERED BY B CORP™ CERTIFICATION\n"
    "87.8% OF VOLUMES SOLD RATES ≥ 3.5 STARS BY THE HEALTH STAR RATING SYSTEM\n\n"
    "(a) Scores obtained as part of CDP Climate Change.\n"
)
PANEL_TWO_LINES = [
    "- AWARDED BY CDP: AAA (a)",
    "- EMPLOYEES COVERED BY B CORP™ CERTIFICATION: 98.0%",
    "- OF VOLUMES SOLD RATES ≥ 3.5 STARS BY THE HEALTH STAR RATING SYSTEM: 87.8%",
]
KEY_FIGURES = (
    "## Key financial figures\n\n"
    "| Metric | 2024 | 2025 |\n|---|---|---|\n| Sales | 27,376 | 27,283 |\n"
)


def test_shared_predicates() -> None:
    for value in ("+4.5%", "€2.8 bn", "2.25€", "98.0%", "87.8%", "3,003", "2025", "13.4% (a)"):
        check(is_value_text(value), f"value predicate accepts {value}")
    for text in ("27,283 employees worked", "a much too long display value " * 2):
        check(not is_value_text(text), f"value predicate rejects prose: {text[:20]}")
    for label in (
        "LIKE-FOR-LIKE SALES GROWTH",
        "VOLUME / MIX",
        "FREE CASH FLOW",
        "EMPLOYEES COVERED BY B CORP™ CERTIFICATION",
    ):
        check(is_kpi_label_text(label), f"label predicate accepts {label}")
    for text in ("Sales", "2025", "In 2025, Danone grew", "EU"):
        check(not is_kpi_label_text(text), f"label predicate rejects {text}")
    check(is_letter_rating("AAA") and is_letter_rating("Baa1"), "rating predicate accepts ratings")
    check(not is_letter_rating("EU DIRECTIVE"), "rating predicate rejects prose")


def test_alternating_panel() -> None:
    paired, count = pair_kpi_text_runs(PANEL_ONE)
    expected = "### FINANCIAL INDICATORS\n\n" + "\n".join(PANEL_ONE_LINES) + "\n\n"
    check(count == 6, "alternating panel emits six pairs")
    check(paired == expected, "heading and surrounding blank lines are preserved")


def test_same_line_panel() -> None:
    paired, count = pair_kpi_text_runs(PANEL_TWO)
    expected = "\n".join(PANEL_TWO_LINES) + "\n\n(a) Scores obtained as part of CDP Climate Change.\n"
    check(count == 3, "same-line panel emits three pairs")
    check(paired == expected, "same-line pairs and footnote definition are preserved")


def test_full_page_idempotency_and_table_preservation() -> None:
    raw = PANEL_ONE + "## SUSTAINABILITY INDICATORS\n\n" + PANEL_TWO + "\n" + KEY_FIGURES
    paired, count = pair_kpi_text_runs(raw)
    check(count == 9, "both page-5 panels are paired")
    check(KEY_FIGURES in paired, "real key-figures table is byte-identical")
    rerun, repeat_count = pair_kpi_text_runs(paired)
    check(rerun == paired and repeat_count == 0, "pairing pass is idempotent")


def test_guards_and_leftovers() -> None:
    years = "2023\nRECORD YEAR\n2024\nANOTHER YEAR\n"
    check(pair_kpi_text_runs(years) == (years, 0), "all-year timeline is untouched")
    prose = "A prose paragraph before.\n\n98.0% EMPLOYEES COVERED BY B CORP™ CERTIFICATION\n\nA prose paragraph after.\n"
    check(pair_kpi_text_runs(prose) == (prose, 0), "isolated mixed line in prose is untouched")
    directive = "EU DIRECTIVE 2019/904 ON SINGLE-USE PLASTICS\n"
    check(pair_kpi_text_runs(directive) == (directive, 0), "unmarked rating-like prose is untouched")
    labels_only = "MAIN MARKETS\nSTRATEGIC PRIORITIES\nFINANCIAL INDICATORS\n"
    check(pair_kpi_text_runs(labels_only) == (labels_only, 0), "label-only section list is untouched")
    broken = "+4.5%\nLIKE-FOR-LIKE SALES GROWTH\n+2.7%\nVOLUME / MIX\n13.4%\n"
    paired, count = pair_kpi_text_runs(broken)
    check(count == 2 and paired.endswith("- 13.4%\n"), "confirmed run bulletizes an unmatched tail")


def test_completeness_guard_and_postprocess_warnings() -> None:
    paired, _ = pair_kpi_text_runs(PANEL_ONE)
    guarded, warnings = apply_completeness_guard(PANEL_ONE, paired)
    check("## Unplaced content" not in guarded, "guard does not re-append paired KPI source lines")
    check(not warnings["content_loss_guard_triggered"], "guard remains quiet for paired KPI lines")
    final, warnings = postprocess_markdown(PANEL_ONE, PANEL_ONE, [])
    check(all(line in final for line in PANEL_ONE_LINES), "postprocess wires loose KPI pairing")
    check(warnings.get("kpi_text_pairs") == 6, "postprocess records emitted KPI pairs")


def test_bare_year_is_never_a_same_line_value() -> None:
    """A divider title ending in a year must not become ``label: value``.

    Observed on page 40: the title paired as
    ``BUSINESS HIGHLIGHTS IN 2025 AND OUTLOOK FOR: 2026``, which also pushed the
    run to the two-pair threshold and colonized the page footer -- the colon then
    defeated the later furniture strip, so the footer survived into the output.
    """
    title = "BUSINESS HIGHLIGHTS IN 2025 AND OUTLOOK FOR 2026"
    kind, pair = _classify_kpi_text_line(title)
    check(kind == "LABEL" and pair is None, "a title ending in a year is a label, not a pair")

    divider_page = (
        "![Picture p0040-i001](images/picture_p0040_i001.png)\n\n"
        "2\n\n"
        f"{title}\n\n"
        "DANONE - UNIVERSAL REGISTRATION DOCUMENT 2025 38\n"
    )
    out, pairs = pair_kpi_text_runs(divider_page)
    check(pairs == 0, "a divider page yields no KPI pairs")
    check(out == divider_page, "a divider page is left byte-identical")
    check(":" not in out, "no colon is fabricated on a divider page")


def main() -> int:
    test_shared_predicates()
    test_alternating_panel()
    test_same_line_panel()
    test_full_page_idempotency_and_table_preservation()
    test_guards_and_leftovers()
    test_completeness_guard_and_postprocess_warnings()
    test_bare_year_is_never_a_same_line_value()
    print("test_kpi_text_pairing: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
