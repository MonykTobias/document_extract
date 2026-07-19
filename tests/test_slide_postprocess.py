"""Quick Markdown post-processing tests against synthetic cases and artifacts.

Run: python test_slide_postprocess.py
Exits non-zero on failure. Real-artifact checks are skipped if the baseline
output directory is absent.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from document_extract.layout.prompt_map import NUMERIC_TOKEN_RE
from document_extract.markdown import postprocess as sp
from document_extract.refinement import postprocess_markdown

BASELINE = Path("outputs_paddleocr_vl_qwen/danoneiar2025v1")
NEWRUN = Path("outputs_paddleocr_vl_qwen/danoneiar2025v2")

_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    status = "ok  " if cond else "FAIL"
    print(f"[{status}] {msg}")
    if not cond:
        _failures.append(msg)


# --------------------------------------------------------------------------- #
# Synthetic unit cases
# --------------------------------------------------------------------------- #

def test_flatten_rowspan():
    html = (
        "<table border=1><tr><td>GOALS</td><td>TARGETS</td></tr>"
        '<tr><td rowspan="2">Offer tastier food</td>'
        "<td>Maintain 85% $ ^{[1]} $</td></tr>"
        "<tr><td>Reach 88% &amp; more $ ^{[4]} $</td></tr></table>"
    )
    out = sp.flatten_html_tables(html)
    check("| GOALS | TARGETS |" in out, "rowspan: header row present")
    check(out.count("Offer tastier food") == 2, "rowspan: group label repeated into both rows")
    check("[1]" in out and "[4]" in out, "rowspan: latex footnote markers -> [1]/[4]")
    check("&" in out and "&amp;" not in out, "rowspan: html entity unescaped")


def test_flatten_colspan():
    html = (
        "<table><tr><td>H1</td><td>H2</td><td>H3</td></tr>"
        '<tr><td colspan="2">wide</td><td>x</td></tr></table>'
    )
    out = sp.flatten_html_tables(html)
    check(out.count("wide") == 2, "colspan: spanned label repeated across columns")


def test_inline_bullets():
    md = "OUR TARGETS: • Reduce water • Reuse water • Recycle"
    out = sp.normalize_bullets_and_headings(md)
    lines = [l for l in out.splitlines() if l.strip()]
    check(sum(l.startswith("- ") for l in lines) == 3, "inline bullets split into 3 list items")
    check(any("OUR TARGETS:" in l and not l.startswith("- ") for l in lines), "prefix kept as its own line")


def test_collapse_internal_spaces():
    markdown = "consumer  trends  and  unlock\n  - indented  item\n| a  | b  |\n"
    collapsed = sp.collapse_internal_spaces(markdown)
    check("consumer trends and unlock" in collapsed, "prose internal spaces collapse")
    check("  - indented item" in collapsed, "list indentation stays intact")
    check("| a  | b  |" in collapsed, "pipe-row cell padding stays intact")


def test_redundant_list_glyphs():
    markdown = (
        "- ■ ongoing uncertainties are tracked.\n"
        "  - • indented variant stays a list item.\n"
        "- ■ = fully covered legend.\n"
        "| Label | - ■ pipe-row value |\n"
        "- plain list item.\n"
    )
    out = sp.strip_redundant_list_glyphs(markdown)
    check("- ongoing uncertainties are tracked." in out, "black square after list marker is removed")
    check("  - indented variant stays a list item." in out, "indented redundant bullet is removed")
    check("- ■ = fully covered legend." in out, "legend glyph is preserved")
    check("| Label | - ■ pipe-row value |" in out, "pipe-row glyph is preserved")
    check("- plain list item." in out, "ordinary list item is unchanged")
    check(sp.strip_redundant_list_glyphs(out) == out, "redundant-glyph strip is idempotent")

    final, _ = postprocess_markdown(
        "- • ongoing uncertainties are tracked.\n",
        "- • ongoing uncertainties are tracked.\n",
        [],
    )
    check(
        final.strip() == "- ongoing uncertainties are tracked.",
        "postprocess strips before prose bullet normalization can split the list",
    )


def test_extract_uncertainty():
    md = (
        "# Title\n\nBody text.\n\n## Uncertain mappings\n"
        "- region A: unclear\n- region B: unclear\n\n## Footnotes\n[1] note\n"
    )
    kept, side = sp.extract_uncertainty(md)
    check("Uncertain mappings" not in kept, "uncertainty removed from body")
    check("## Footnotes" in kept, "heading after uncertainty block retained")
    check("region A" in side, "uncertainty captured into sidecar")


def test_footnote_consistency():
    good = "Body with [1] and [2].\n\n[1] def one\n[2] def two\n"
    check(sp.footnote_consistency(good) == [], "consistent footnotes -> no warnings")
    bad = "Body with [5] and [6] and [7].\n\n[6] def\n[7] def\n"  # p12-style: [5] gone
    warns = sp.footnote_consistency(bad)
    check(any("without definitions" in w and "5" in w for w in warns), "missing [5] definition flagged")


def test_completeness_diff():
    ocr = "This is an important introductory paragraph about sustainability goals.\n"
    refined = "# Slide\n\nCompletely different restructured content about boxes.\n"
    missing = sp.completeness_diff(ocr, refined)
    check(len(missing) == 1, "dropped intro paragraph flagged as missing")
    refined2 = "# Slide\n\nThis is an important introductory paragraph about sustainability goals.\n"
    check(sp.completeness_diff(ocr, refined2) == [], "present paragraph not flagged")


def test_completeness_diff_keeps_short_value_lines():
    check(sp.completeness_diff("2017\n", "") == ["2017"], "standalone year flagged as missing")
    check(sp.completeness_diff("6\n", "") == [], "bare page number ignored")
    check(sp.completeness_diff("29.8%\n", "") == ["29.8%"], "percentage flagged as missing")
    check(sp.completeness_diff("2.25 EUR\n", "") == ["2.25 EUR"], "currency value flagged as missing")
    check(sp.completeness_diff("27.3Bn\n", "") == ["27.3Bn"], "unit value flagged as missing")


def test_numeric_tokens_match_table_verification():
    for value in ("1.234,5", "-46,3", "18%"):
        table_tokens = {token.replace(",", ".") for token in NUMERIC_TOKEN_RE.findall(value)}
        check(
            table_tokens == sp._numeric_tokens(value),
            f"numeric token agreement for {value}",
        )


def test_completeness_diff_scattered_words_not_coverage():
    # p26 regression: a global word bag counted "Dairy ingredients: 18%" as present
    # because "dairy" and "ingredients" appeared in unrelated lines far apart.
    ocr = "Dairy ingredients: 18% of total emissions footprint\n"
    refined = (
        "# Emissions\n\nMilk: scaling low carbon dairy farming programs.\n\n"
        "Packaging: circular systems.\n\nLogistics and transport decarbonization.\n\n"
        "Other: 2% of total emissions footprint categories.\n\n"
        "Ingredients: sourcing key commodities sustainably every year.\n"
    )
    missing = sp.completeness_diff(ocr, refined)
    check(len(missing) == 1, "scattered-word masking fixed: dropped line still flagged")
    # A legitimate split across two adjacent lines must NOT be flagged.
    refined_split = (
        "# Emissions\n\nDairy ingredients: 18%\nof total emissions footprint\n"
    )
    check(
        sp.completeness_diff(ocr, refined_split) == [],
        "line split across adjacent lines not flagged",
    )


def test_merge_unplaced_content():
    md = "# Title\n\nBody text.\n"
    out = sp.merge_unplaced_content(md, ["Lost line one 123", "Body text."])
    check("## Unplaced content" in out, "unplaced section created")
    check("- Lost line one 123" in out, "missing line appended")
    check(out.count("Body text.") == 1, "already-present line not duplicated")
    out2 = sp.merge_unplaced_content(out, ["Second lost line 456"])
    check(out2.count("## Unplaced content") == 1, "existing section extended, not duplicated")
    check("- Second lost line 456" in out2, "second line appended into existing section")


def test_normalize_footnotes():
    md = (
        "# Title\n\nBody with [1] marker.\n\n"
        "[1] Awaiting new standards definition\n\n"
        "More body.\n\n* Between 2020 & 2030 in absolute value\n\n"
        "## Footnotes\n* Between 2020 & 2030 in absolute value\n"
    )
    out = sp.normalize_footnotes(md)
    check(out.count("## Footnotes") == 1, "single footnotes section")
    check(out.count("Between 2020 & 2030") == 1, "duplicated definition deduped")
    check(
        out.index("[1] Awaiting") > out.index("## Footnotes"),
        "mid-body numeric definition moved into the section",
    )
    check("Body with [1] marker." in out, "body reference untouched")
    no_fn = "# Title\n\nJust body text.\n"
    check(sp.normalize_footnotes(no_fn) == no_fn, "page without footnotes unchanged")


def test_mark_figure_regions():
    md = (
        '<div style="text-align: center;"><img src="imgs/img_in_chart_box_1_2_3_4.jpg" '
        'alt="Image" width="19%" /></div>\n\nText.\n\n'
        '<div style="text-align: center;"><img src="imgs/img_in_image_box_5_6_7_8.jpg" '
        'alt="Image" width="11%" /></div>\n'
    )
    out = sp.mark_figure_regions(md)
    check("[CHART HERE" in out, "chart box becomes CHART marker")
    check("[FIGURE HERE" in out, "image box becomes FIGURE marker")
    check("<img" not in out, "no raw img tags left in prompt input")
    echoed = "Body.\n[CHART HERE — the first pass could not read this chart.]\nMore.\n"
    stripped = sp.strip_figure_markers(echoed)
    check("[CHART HERE" not in stripped and "Body." in stripped, "marker echo stripped")


def test_demote_datapoint_headings():
    md = (
        "## Independence rate[1]\n\n### 2015: 77%\n\n### 2023: 89%\n\n"
        "## OUR TARGETS\n\n### Milk: 36%\n\nProgram text under the group.\n"
    )
    out = sp.demote_datapoint_headings(md)
    check("- 2015: 77%" in out and "- 2023: 89%" in out, "empty datapoint headings demoted to list items")
    check("### Milk: 36%" in out, "group heading with body text kept as heading")
    check("## Independence rate[1]" in out, "chart title heading kept")


def test_repeated_lines():
    loop = "\n".join(["The same long uncertain mapping bullet repeated again."] * 30)
    ratio, anom = sp.detect_repeated_lines(loop)
    check(anom and ratio > 0.9, "decoding loop detected")
    normal = "# Title\n\nA sentence here.\n\nAnother distinct sentence there.\n"
    _, anom2 = sp.detect_repeated_lines(normal)
    check(not anom2, "normal text not flagged as loop")


def test_repeated_lines_ignores_structural_table_rows():
    header = "| This long repeated section header must not count as a decoding loop | Value |"
    separator = "|---|---|"
    distinct_bodies = [
        f"| Distinct body row {index} contains enough descriptive content to remain unique | {index} |"
        for index in range(8)
    ]
    sectioned = "\n".join(
        line
        for index, body in enumerate(distinct_bodies)
        for line in (header, separator, body)
    )
    ratio, anomalous = sp.detect_repeated_lines(sectioned)
    check(not anomalous and ratio == 0.0, "repeated section headers and separators are structural")

    repeated_body = "| Repeated body row with enough content to indicate a real decoding loop | x |"
    _, body_anomalous = sp.detect_repeated_lines("\n".join([header, separator] + [repeated_body] * 6))
    check(body_anomalous, "repeated table body rows still trigger the decoder-loop guard")
    prose = "The same long prose sentence repeats and should remain a decoder-loop signal."
    _, prose_anomalous = sp.detect_repeated_lines("\n".join([prose] * 5))
    check(prose_anomalous, "repeated prose still triggers the decoder-loop guard")
    first = "First long prose line repeated below to exercise the ratio-only branch."
    second = "Second long prose line repeated below to exercise the ratio-only branch."
    ratio, ratio_anomalous = sp.detect_repeated_lines("\n".join([first] * 4 + [second] * 4))
    check(ratio_anomalous and ratio > 0.5, "the repeated-ratio branch remains active")


def test_meta_commentary():
    md = "Real content line.\nNote: this text is preserved as per image and should be included.\n"
    warns = sp.meta_commentary_warnings(md)
    check(len(warns) >= 1, "meta-commentary line flagged")
    stripped = sp.strip_meta_commentary(md)
    check("Real content line." in stripped and "should be included" not in stripped,
          "meta-commentary stripped, content kept")


# --------------------------------------------------------------------------- #
# Real-artifact checks (baseline reproduction)
# --------------------------------------------------------------------------- #

def _read(page: int, name: str) -> str | None:
    p = BASELINE / f"page_{page:04d}" / name
    return p.read_text(encoding="utf-8") if p.exists() else None


def test_real_p24_table():
    ocr = _read(24, "paddleocr_vl.md")
    if ocr is None:
        print("[skip] p24 artifact not found")
        return
    out = sp.flatten_html_tables(ocr)
    check("<table" not in out, "p24: OCR html table flattened")
    check(out.count("Offer tastier and healthier food and drinks") >= 3,
          "p24: rowspan goal label repeated per target row")
    check("[1]" in out and "[4]" in out, "p24: footnote markers [1]/[4] preserved in cells")


def test_real_p51_loop():
    refined = _read(51, "qwen_refined.md")
    if refined is None:
        print("[skip] p51 artifact not found")
        return
    _, anom = sp.detect_repeated_lines(refined)
    check(anom, "p51: refinement decoding loop detected")


def test_real_p12_footnotes():
    verified = _read(12, "qwen_verified.md") or _read(12, "qwen_refined.md")
    if verified is None:
        print("[skip] p12 artifact not found")
        return
    # p12's corruption is semantic (right marker, wrong definition text), which pure
    # marker-set consistency cannot see. What the lint MUST do is parse the unicode
    # superscript markers so the structural cases elsewhere are caught.
    refs, defs = sp.footnote_labels(verified)
    print(f"       p12 refs={sorted(refs)} defs={sorted(defs)}")
    check("5" in refs, "p12: superscript body reference [5] parsed")
    check({"1", "5", "9"} <= defs, "p12: superscript footnote definitions parsed")


def test_real_newrun_p26_diff_catches_dropped_columns():
    ocr_p = NEWRUN / "page_0026" / "paddleocr_vl.md"
    ref_p = NEWRUN / "page_0026" / "qwen_refined.md"
    if not (ocr_p.exists() and ref_p.exists()):
        print("[skip] new-run p26 artifacts not found")
        return
    ocr = ocr_p.read_text(encoding="utf-8")
    body, _ = sp.extract_uncertainty(ref_p.read_text(encoding="utf-8"))
    missing = sp.completeness_diff(ocr, body)
    joined = " | ".join(missing).lower()
    check("dairy ingredients: 18%" in joined, "p26: dropped 'Dairy ingredients: 18%' detected")
    check("non-dairy ingredients: 10%" in joined, "p26: dropped 'Non-dairy ingredients: 10%' detected")
    check("logistics" in joined, "p26: dropped Logistics program text detected")
    check("co-manufacturing" in joined, "p26: dropped Co-manufacturing program text detected")
    # Enforcement: everything detected must survive into the merged output.
    enforced = sp.merge_unplaced_content(body, missing)
    check(
        sp.completeness_diff(ocr, enforced) == [],
        "p26: after unplaced-content enforcement nothing is missing",
    )


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILED:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
