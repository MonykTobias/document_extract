"""Offline re-postprocess harness for saved page artifacts.

Replays only the deterministic ``postprocess_markdown`` chain over the
artifacts a previous pipeline run saved (``page_state.json`` per page), so
markdown-cleanup changes can be validated against real pages without GPU,
Ollama, or Docling.

Usage:
    python tools/reprocess_pages.py --pages-root <dir with page_NNNN dirs> --out <dir>

Caveats:
- The replay starts from the stored *refined* markdown (the page_refine
  output). Pages where the live run *accepted a repair pass* replay from the
  pre-repair refined text, so their output legitimately differs from the
  checked-in ``docling_final.md``. Diff harness runs against each other
  (baseline vs after-a-change), not against the live outputs.
- Repair-pass acceptance logic itself cannot be replayed here; unit-test it
  against the saved ``page_repair.md`` texts instead.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from document_extract.pipeline.state import (  # noqa: E402
    _candidates_from_state,
    _records_from_state,
)
from document_extract.refinement import postprocess_markdown  # noqa: E402
from document_extract.runtime import PageState  # noqa: E402

PAGE_DIR_RE = re.compile(r"^page_\d{4}$")


def _read_optional(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def replay_page(page_dir: Path) -> tuple[str, dict]:
    """Re-run the deterministic postprocess chain for one saved page."""
    state = PageState.load(page_dir / "page_state.json")
    # The stored page_dir is the absolute path of the original run; point it
    # at the directory we are actually reading so relative paths resolve.
    state.page_dir = str(page_dir)
    records = _records_from_state(state)
    candidates = _candidates_from_state(state)
    raw = state.raw_markdown or _read_optional(page_dir / "docling_raw.md")
    working = (
        state.refined_markdown
        or _read_optional(page_dir / "page_vlm.md")
        or raw
    )
    final, warnings = postprocess_markdown(
        raw,
        working,
        records,
        candidates,
        state.layout_map,
    )
    return final, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    page_dirs = sorted(
        child
        for child in args.pages_root.iterdir()
        if child.is_dir()
        and PAGE_DIR_RE.match(child.name)
        and (child / "page_state.json").exists()
    )
    if not page_dirs:
        print(f"No page_NNNN dirs with page_state.json under {args.pages_root}")
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    combined: list[str] = []
    warning_rows: dict[str, dict] = {}
    failures = 0
    for page_dir in page_dirs:
        try:
            final, warnings = replay_page(page_dir)
        except Exception as error:  # noqa: BLE001
            failures += 1
            print(f"[fail] {page_dir.name}: {type(error).__name__}: {error}")
            continue
        (args.out / f"{page_dir.name}.md").write_text(final, encoding="utf-8")
        combined.append(f"\n\n===== {page_dir.name} =====\n\n{final}")
        warning_rows[page_dir.name] = warnings

    (args.out / "combined.md").write_text("".join(combined).lstrip(), encoding="utf-8")
    (args.out / "warnings.json").write_text(
        json.dumps(warning_rows, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Reprocessed {len(page_dirs) - failures}/{len(page_dirs)} pages -> {args.out}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
