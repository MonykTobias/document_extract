"""Checks for pseudo-table unwrapping and orphan-table removal (WP4).

The positive fixture mirrors the broken risk-spread table the accepted repair
produced on danoneurdaccessible page_0027 (a two-column layout table with
``<br>``-joined prose cells plus stray ``<br>`` bullet lines outside the
table); the negative fixture is the page_0046 KEY FIGURES data table.

Run from the repository root with ``python tests/test_layout_table_unwrap.py``.
"""

from __future__ import annotations

from pathlib import Path


from document_extract.markdown.formatting import (
    drop_empty_header_row,
    drop_orphan_header_tables,
    strip_br_lines,
    unwrap_layout_tables,
)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


BROKEN_LAYOUT_TABLE = (
    "| Description | Management measures |\n"
    "|---|---|\n"
    "| **medium** Retail shift<br>In a context marked by persistent inflation, "
    "heightened consumer price sensitivity and rapid digital transformation, "
    "the retail landscape continues to evolve significantly. Shoppers are "
    "increasingly prioritizing value, which results in:<br>"
    "- a continued increase and sophistication of private labels;<br>"
    "- a stronger focus on essential Categories; "
    "| Danone's actions to adapt to the retail shifts include:<br>"
    "- enhancing Consumer Value proposition through superior product quality;<br>"
    "- driving its commercial strategy, including embedding the sustainability "
    "agenda in brands' purpose as well as in portfolio development. |\n"
)
OUTSIDE_BR_LINES = (
    "- optimizing processes (including mutualizing purchases via alliances & buying groups);<br>\n"
    "- reducing stock levels to optimize cash.<br><br>As a consequence, Danone "
    "needs to adapt its value proposition to consumers.\n"
)
KEY_FIGURES_TABLE = (
    "|  | Year ended December 31 | Year ended December 31 |\n"
    "|---|---|---|\n"
    "| (in € millions unless stated otherwise) | 2024 | 2025 |\n"
    "| Sales | 27,376 | 27,283 |\n"
    "| Recurring operating margin (a) | 13.0% | 13.4% |\n"
)


def check_unwrap_broken_layout_table() -> None:
    unwrapped, count = unwrap_layout_tables(BROKEN_LAYOUT_TABLE)
    check(count == 1, "broken layout table detected and unwrapped")
    check("|" not in unwrapped, "no pipe table remains after unwrap")
    check("<br>" not in unwrapped, "no <br> artifacts remain after unwrap")
    check("**Description**" in unwrapped, "Description column labelled")
    check("**Management measures**" in unwrapped, "Management measures column labelled")
    for sentence in (
        "**medium** Retail shift",
        "the retail landscape continues to evolve significantly",
        "- a continued increase and sophistication of private labels;",
        "- a stronger focus on essential Categories;",
        "Danone's actions to adapt to the retail shifts include:",
        "- enhancing Consumer Value proposition through superior product quality;",
        "agenda in brands' purpose as well as in portfolio development.",
    ):
        check(sentence in unwrapped, f"content survives: {sentence[:45]!r}")
    description_block = unwrapped.split("**Management measures**")[0]
    check(
        "Retail shift" in description_block
        and "adapt to the retail shifts" not in description_block,
        "column contents stay under their own label",
    )


def check_strip_br_outside_tables() -> None:
    stripped = strip_br_lines(OUTSIDE_BR_LINES)
    check("<br>" not in stripped, "stray <br> tokens removed from plain lines")
    check(
        "- optimizing processes (including mutualizing purchases via alliances & buying groups);"
        in stripped.splitlines(),
        "trailing <br> dropped without content loss",
    )
    check(
        "As a consequence, Danone needs to adapt its value proposition to consumers."
        in stripped,
        "inline <br> became a line break, text intact",
    )
    table_row = "| keep<br>this | cell |\n"
    check(strip_br_lines(table_row) == table_row, "table rows left for unwrap to decide")


def check_data_tables_untouched() -> None:
    unwrapped, count = unwrap_layout_tables(KEY_FIGURES_TABLE)
    check(count == 0 and unwrapped == KEY_FIGURES_TABLE, "KEY FIGURES data table untouched")

    footnote_table = (
        "| Metric | 2024 | 2025 | Note |\n"
        "|---|---|---|---|\n"
        "| Sales | 27,376 | 27,283 | "
        + "A very long footnote cell explaining the like-for-like basis. " * 5
        + "|\n"
    )
    unwrapped, count = unwrap_layout_tables(footnote_table)
    check(count == 0, "numeric table with one long footnote cell untouched")


def check_many_row_categorical_table_untouched() -> None:
    # Real case: page 18's competitor table has long prose cells and no
    # numbers, but its many rows mark it as a data table, not a layout table.
    rows = "".join(
        f"| EDP | Range {i} | " + "A long list of competitor companies. " * 4 + "|\n"
        for i in range(6)
    )
    table = "| Category | Product range | Competitive environment |\n|---|---|---|\n" + rows
    unwrapped, count = unwrap_layout_tables(table)
    check(count == 0 and unwrapped == table, "many-row categorical table untouched")


def check_orphan_tables() -> None:
    with_separator = "Intro.\n\n| Description   | Management measures   |\n|---------------|-----------------------|\n\n## medium\n"
    cleaned, dropped = drop_orphan_header_tables(with_separator)
    check(
        dropped == ["Description Management measures"] and "| Description" not in cleaned,
        "header+separator orphan dropped and reported",
    )
    check("## medium" in cleaned and "Intro." in cleaned, "surrounding content kept")

    lone = "| Description   | Management measures   |\n\n### medium Legal and Regulatory\n"
    cleaned, dropped = drop_orphan_header_tables(lone)
    check(len(dropped) == 1 and "| Description" not in cleaned, "separator-less lone header dropped")

    real = "| Description | Management measures |\n|---|---|\n| risk | action |\n"
    cleaned, dropped = drop_orphan_header_tables(real)
    check(not dropped and cleaned == real, "table with body rows kept")

    other = "| Region | Sales |\n|---|---|\n"
    cleaned, dropped = drop_orphan_header_tables(other)
    check(not dropped, "unrelated header-only table kept (unknown labels)")


def check_empty_tables() -> None:
    empty = "Text.\n\n|  |  |  |\n|---|---|---|\n|  |  |  |\n"
    cleaned, count = drop_empty_header_row(empty)
    check(count == 1 and "|" not in cleaned, "fully empty table dropped")

    # Page 23's canonical headerless form must survive: blank header, real rows.
    headerless = "|  |  |  |\n|---|---|---|\n| Strategic risks | strong | Packaging |\n"
    cleaned, count = drop_empty_header_row(headerless)
    check(count == 0 and cleaned == headerless, "canonical headerless table kept")


def main() -> int:
    check_unwrap_broken_layout_table()
    check_strip_br_outside_tables()
    check_data_tables_untouched()
    check_many_row_categorical_table_untouched()
    check_orphan_tables()
    check_empty_tables()
    print("test_layout_table_unwrap: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
