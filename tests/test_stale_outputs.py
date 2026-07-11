"""Check for the stale page-directory warning (WP8).

Run from the repository root with ``python tests/test_stale_outputs.py``.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from document_extract.pipeline.runner import _warn_stale_page_dirs
from document_extract.runtime import CHECKPOINT_SCHEMA_VERSION, PageState


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


def make_state(page_dir: Path) -> PageState:
    return PageState(
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        pdf_name="doc.pdf",
        pdf_size=1,
        page=1,
        page_index=1,
        total_pages=1,
        dpi=200,
        page_dir=str(page_dir),
        page_size=(100.0, 100.0),
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "page_0001").mkdir()
        (root / "page_0189").mkdir()
        (root / "page_0342").mkdir()
        (root / "images").mkdir()  # non page_* dirs are ignored
        states = [make_state(root / "page_0001")]
        stale = _warn_stale_page_dirs(root, states)
        check(stale == ["page_0189", "page_0342"], "stale page dirs listed")
        check((root / "page_0189").exists(), "stale dirs are never deleted")
        check(
            _warn_stale_page_dirs(root, states + [make_state(root / "page_0189"), make_state(root / "page_0342")]) == [],
            "no warning when all dirs belong to the run",
        )
    print("test_stale_outputs: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
