"""Offline re-postprocess harness for saved page artifacts.

Replays only the deterministic ``postprocess_markdown`` chain over the
artifacts a previous pipeline run saved (``page_state.json`` per page), so
markdown-cleanup changes can be validated against real pages without GPU,
Ollama, or Docling.

Usage:
    python tools/reprocess_pages.py --pages-root <dir with page_NNNN dirs> --out <dir>

Furniture: the live pipeline detects page furniture from Docling items
(cross-page band repetition) plus Docling's furniture content layer (footers).
Neither source exists offline, so the harness emulates them: band signatures
are rebuilt from the saved ``layout_prompt_map.json`` blocks, and footer texts
from cross-page repetition of short plain lines at the very top/bottom of the
refined markdown. Pass ``--no-furniture`` to disable both.

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

from document_extract.layout.furniture import (  # noqa: E402
    page_strip_texts,
    repeated_furniture_signatures,
)
from document_extract.markdown.postprocess import (  # noqa: E402
    IMAGE_PLACEHOLDER_LINE_RE,
    looks_like_toc,
    normalize_furniture_text,
)
from document_extract.pipeline.state import (  # noqa: E402
    _candidates_from_state,
    _records_from_state,
)
from document_extract.refinement import postprocess_markdown  # noqa: E402
from document_extract.runtime import PageState  # noqa: E402

PAGE_DIR_RE = re.compile(r"^page_\d{4}$")
# Footer emulation: only short plain paragraph lines qualify (headings,
# bullets, table rows, and image refs are handled by other mechanisms or are
# legitimate repeated structure like ``## Footnotes``).
FOOTER_EDGE_LINES = 3
FOOTER_MIN_PAGES = 3
FOOTER_MAX_CHARS = 80


def _read_optional(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _load_page(page_dir: Path) -> dict:
    state = PageState.load(page_dir / "page_state.json")
    # The stored page_dir is the absolute path of the original run; point it
    # at the directory we are actually reading so relative paths resolve.
    state.page_dir = str(page_dir)
    raw = state.raw_markdown or _read_optional(page_dir / "docling_raw.md")
    working = (
        state.refined_markdown
        or _read_optional(page_dir / "page_vlm.md")
        or raw
    )
    layout_path = page_dir / "layout_prompt_map.json"
    layout_entries: list[tuple[str, list[float] | None]] = []
    if layout_path.exists():
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
        layout_entries = [
            (str(block.get("text") or ""), block.get("bbox"))
            for block in layout.get("blocks", [])
            if block.get("text")
        ]
    return {
        "dir": page_dir,
        "state": state,
        "raw": raw,
        "working": working,
        "layout_entries": layout_entries,
    }


def _edge_plain_lines(markdown: str) -> set[str]:
    """Normalized short plain lines among the first/last few content lines."""
    lines = [ln.strip() for ln in markdown.splitlines() if ln.strip()]
    edge = lines[:FOOTER_EDGE_LINES] + lines[-FOOTER_EDGE_LINES:]
    out: set[str] = set()
    for line in edge:
        if len(line) > FOOTER_MAX_CHARS:
            continue
        if line.startswith(("#", "-", "*", "|", ">", "!")):
            continue
        if IMAGE_PLACEHOLDER_LINE_RE.match(line):
            continue
        normalized = normalize_furniture_text(line)
        # Same letter requirement as the pipeline's signature builder: digit
        # stripping must not fabricate letterless catch-all signatures.
        if normalized and re.search(r"[^\W\d_]", normalized):
            out.add(normalized)
    return out


def _offline_footer_texts(pages: list[dict]) -> set[str]:
    counts: dict[str, int] = {}
    for page in pages:
        for normalized in _edge_plain_lines(page["working"]):
            counts[normalized] = counts.get(normalized, 0) + 1
    return {text for text, count in counts.items() if count >= FOOTER_MIN_PAGES}


def replay_page(page: dict, furniture_texts: set[str]) -> tuple[str, dict]:
    """Re-run the deterministic postprocess chain for one saved page."""
    state = page["state"]
    records = _records_from_state(state)
    candidates = _candidates_from_state(state)
    # Old checkpoints predate the page_role field; recompute like prepare does.
    page_role = state.page_role or ("toc" if looks_like_toc(page["raw"]) else "")
    final, warnings = postprocess_markdown(
        page["raw"],
        page["working"],
        records,
        candidates,
        state.layout_map,
        furniture_texts=furniture_texts,
        page_role=page_role or None,
    )
    return final, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--no-furniture", action="store_true")
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

    pages: list[dict] = []
    failures = 0
    for page_dir in page_dirs:
        try:
            pages.append(_load_page(page_dir))
        except Exception as error:  # noqa: BLE001
            failures += 1
            print(f"[fail-load] {page_dir.name}: {type(error).__name__}: {error}")

    signatures: set[tuple[str, str]] = set()
    footer_texts: set[str] = set()
    if not args.no_furniture:
        signatures = repeated_furniture_signatures(
            {index: page["layout_entries"] for index, page in enumerate(pages)}
        )
        footer_texts = _offline_footer_texts(pages)

    args.out.mkdir(parents=True, exist_ok=True)
    combined: list[str] = []
    warning_rows: dict[str, dict] = {}
    for page in pages:
        name = page["dir"].name
        furniture = page_strip_texts(
            page["layout_entries"], signatures, extra_texts=footer_texts
        )
        try:
            final, warnings = replay_page(page, furniture)
        except Exception as error:  # noqa: BLE001
            failures += 1
            print(f"[fail] {name}: {type(error).__name__}: {error}")
            continue
        (args.out / f"{name}.md").write_text(final, encoding="utf-8")
        combined.append(f"\n\n===== {name} =====\n\n{final}")
        warning_rows[name] = warnings

    (args.out / "combined.md").write_text("".join(combined).lstrip(), encoding="utf-8")
    (args.out / "warnings.json").write_text(
        json.dumps(warning_rows, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Reprocessed {len(pages) - failures}/{len(page_dirs)} pages -> {args.out}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
