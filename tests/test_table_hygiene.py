"""End-to-end span propagation through markdown post-processing."""

from __future__ import annotations

from pathlib import Path


from document_extract.markdown.pipe import is_separator_line, split_row
from document_extract.refinement import postprocess_markdown


HTML = """<table>
<tr><th>Metric</th><th colspan="2">Baseline</th></tr>
<tr><td rowspan="2">Shared group</td><td colspan="2">Wide body status label</td></tr>
<tr><td>one</td><td>two</td></tr>
</table>"""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


def main() -> int:
    final, warnings = postprocess_markdown(HTML, HTML, [], [], {})
    check(final.count("Baseline") == 2, "header colspan remains repeated")
    check(final.count("Wide body status label") == 2, "body colspan remains repeated")
    check(final.count("Shared group") == 2, "body rowspan remains repeated")
    rows = [
        split_row(line)
        for line in final.splitlines()
        if line.startswith("|") and not is_separator_line(line)
    ]
    check(rows and {len(row) for row in rows} == {3}, "all logical rows are rectangular")
    check(not warnings.get("content_loss_guard_triggered"), "span expansion loses no content")
    print("test_table_hygiene: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
