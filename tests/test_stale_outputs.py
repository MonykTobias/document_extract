"""Check for the stale page-directory warning (WP8).

Run from the repository root with ``python tests/test_stale_outputs.py``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
from pathlib import Path


from document_extract.artifacts import write_text_atomic
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
    check_empty_states_preserve_existing_outputs()
    check_atomic_writes()
    print("test_stale_outputs: all checks passed")
    return 0


def check_atomic_writes() -> None:
    """Aggregate writes swap in atomically and never leave a temp file behind.

    Process-parallel shards all rebuild the document-wide all/ aggregates
    against one output root, so two workers can target the same path at once.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "combined.md"
        target.write_text("old content", encoding="utf-8")

        write_text_atomic(target, "new content")
        check(
            target.read_text(encoding="utf-8") == "new content",
            "an atomic write replaces the file contents",
        )
        check(
            [path.name for path in root.iterdir()] == ["combined.md"],
            "a successful atomic write leaves no temp file",
        )

        original_replace = os.replace

        def failing_replace(*_args, **_kwargs):
            raise OSError("replace refused")

        os.replace = failing_replace
        try:
            write_text_atomic(target, "should not land")
        except OSError:
            check(True, "an atomic write propagates a failed replace")
        finally:
            os.replace = original_replace
        check(
            target.read_text(encoding="utf-8") == "new content",
            "a failed atomic write leaves the original file intact",
        )
        check(
            [path.name for path in root.iterdir()] == ["combined.md"],
            "a failed atomic write cleans up its temp file",
        )


def check_empty_states_preserve_existing_outputs() -> None:
    """A run that completes no pages must not truncate a previous run's outputs.

    ``run_pipeline`` writes run outputs from a ``finally``, and its Docling
    chunk boundary sits outside the per-page error handler, so a conversion
    failure reaches the write with an empty state list.
    """
    seeded = {
        "manifest.json": '[{"page": 1, "status": "completed"}]',
        "blocks.jsonl": '{"block_id": "p0001-b0001"}\n',
        "token_usage.json": '{"totals": {"total_tokens": 1234}}',
        "combined_docling_raw.md": "# raw from the previous run\n",
        "combined_docling_final.md": "# final from the previous run\n",
    }
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name, payload in seeded.items():
            (root / name).write_text(payload, encoding="utf-8")

        _write_run_outputs(root, [], shard=False)

        check(
            all(
                (root / name).read_text(encoding="utf-8") == payload
                for name, payload in seeded.items()
            ),
            "a run with no completed pages leaves existing aggregates byte-identical",
        )
        check(
            not (root / "all").exists(),
            "a run with no completed pages does not rebuild the all/ aggregates",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_run_outputs(root, [], shard=True)
        check(
            not any(root.iterdir()),
            "a sharded run with no completed pages writes nothing at all",
        )


if __name__ == "__main__":
    raise SystemExit(main())
