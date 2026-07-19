"""Check for the stale page-directory warning (WP8).

Run from the repository root with ``python tests/test_stale_outputs.py``.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from document_extract.pipeline.runner import _warn_stale_page_dirs, _write_run_outputs
from document_extract.runtime import CHECKPOINT_SCHEMA_VERSION, PageState


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


def make_state(page_dir: Path, page: int = 1) -> PageState:
    return PageState(
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        pdf_name="doc.pdf",
        pdf_size=1,
        page=page,
        page_index=page,
        total_pages=2,
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
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        first = make_state(root / "page_0001", 1)
        second = make_state(root / "page_0002", 2)
        for state in (first, second):
            page_dir = Path(state.page_dir)
            page_dir.mkdir()
            (page_dir / "docling_raw.md").write_text(f"raw {state.page}", encoding="utf-8")
            (page_dir / "docling_final.md").write_text(f"final {state.page}", encoding="utf-8")
            state.status = "completed"
            state.save()
        shard_stdout = io.StringIO()
        with contextlib.redirect_stdout(shard_stdout):
            _write_run_outputs(root, [first], shard=True)
            _write_run_outputs(root, [second], shard=True)
        check("WARNING" not in shard_stdout.getvalue(), "shard runs skip the stale-page warning")
        check(
            (root / "manifest_p0001-p0001.json").exists()
            and (root / "manifest_p0002-p0002.json").exists(),
            "sharded runs keep separate manifests",
        )
        pages = {row["page"] for row in json.loads((root / "all" / "manifest_all.json").read_text(encoding="utf-8"))}
        check(pages == {1, 2}, "all manifest rebuild covers both page shards")
    print("test_stale_outputs: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
