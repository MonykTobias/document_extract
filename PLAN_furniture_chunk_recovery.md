# Spike: chunk-boundary furniture recovery (finding 3.12)

**Status: design only. No implementation is proposed yet — this needs a
maintainer decision and a corpus diff before any code lands.**

## Current behaviour

`run_pipeline` converts pages in chunks of `CONVERT_CHUNK_PAGES = 32`.
`furniture_entries` accumulates `(text, rect)` pairs across chunks
(`furniture_entries.update(...)`, `pipeline/runner.py:405`) and signatures are
recomputed over **all** accumulated entries at 407-408, so evidence strengthens
monotonically as the run proceeds.

The gap is one-directional: pages already processed in an earlier chunk are
never re-stripped. A running header that first appears near the end of chunk 1
is not yet repeated often enough to clear `FURNITURE_MIN_PAGES` while those
pages are being prepared, so it leaks into their Markdown — and later chunks,
which do have the evidence, cannot retroactively clean them.

A single-page run gets no stripping at all: `repeated_furniture_signatures`
returns `set()` when `page_count < 2` (`layout/furniture.py:100`).

This is deliberate, marked with a `ponytail:` comment at `runner.py:406`, and
**already pinned by tests**: `test_furniture_stripping.py::check_chunk_boundary_evidence`
asserts both "single-page final chunk has no evidence" and "accumulated chunks
retain footer evidence". `check_late_debut_header_leaks_in_first_chunk` (added
with this document) pins the leak itself. Any implementation must knowingly
rewrite both.

## Smallest reproducible fixture

`tests/test_furniture_stripping.py::check_late_debut_header_leaks_in_first_chunk`.
Pure, Docling-free, ~15 lines: a footer that debuts on page 31 of a 32-page
chunk has only 2 occurrences inside that chunk (below the threshold of 3), so
it is not stripped there; over the full 40-page run it clears the threshold.
That is exactly the leak, with no fixture files involved.

## Design candidates

| Design | Memory | Runtime | Verdict |
|---|---|---|---|
| Full two-pass (convert everything, then process) | Holds **N** `DoclingDocument` chunks; the current design deliberately caps at 2 (`runner.py:358`) | Destroys the prefetch overlap that hides Docling time behind the VLM calls | **Rejected** — memory regression plus loss of the core performance property |
| Pre-scan pass (convert all chunks for entries, then re-convert per chunk) | Bounded | Doubles total Docling conversion (~20-70 s fixed cost per `convert()`, 7× for a 200-page run) | **Rejected** — unacceptable cost |
| **Post-hoc re-strip** — after the page loop, recompute signatures over all accumulated entries and re-apply `strip_furniture_lines` to the affected pages' saved Markdown | `furniture_entries` only, a cost already paid today (a few MB for 200 pages) | One O(pages) text pass at the end; **no re-conversion, no VLM calls** | **Recommended candidate** |

### Open problems with the recommended candidate

Both must be solved before implementation, and neither is trivial:

1. **Checkpoint consistency.** A re-strip must update `docling_final.md`,
   `state.final_markdown`, and `state.furniture_texts` together, or a later
   `--resume-from` replays from Markdown that disagrees with the checkpoint.
2. **Completeness-guard interaction.** `apply_completeness_guard` ran with the
   smaller signature set, so a line the guard already re-appended under
   `## Unplaced content` could be removed by the re-strip, or worse, kept while
   its body copy is removed. The re-strip has to run against the same filter
   set the guard used, or the guard has to be re-run.

## Corpus validation protocol

Required before any implementation merges.

Two full runs over the same page range of the 96-page reference corpus,
identical config, separate output directories, **Ollama restarted between
runs** (recorded measurements show 3-4× per-call timing swings from Ollama
session state; never compare wall-clock across sessions). Diff
`docling_final.md` per page.

**Acceptable changes**
- Furniture lines removed from chunk-1 pages that previously leaked.

**Unacceptable changes — any one blocks the merge**
- Any change to table Markdown.
- Any genuine section heading removed. The `page_strip_texts` first-page-
  heading protection must hold (the observed `1.6 RISK FACTORS` case, where a
  section's first page carries a heading whose normalized text equals the
  running header on continuation pages).
- Any change on pages in chunk 2 or later.
- Any change to `warnings` other than furniture counts.

## Checkpoint and resume preservation

`state.furniture_texts` is a checkpointed field with a documented "optional so
old checkpoints stay loadable" contract. Any design must:

- keep old checkpoints loadable and **not** bump `CHECKPOINT_SCHEMA_VERSION`;
- work on resume, where `_prepare_page` never runs and `furniture_texts` comes
  from the checkpoint rather than from live signatures.

## Decision required

Approve the post-hoc re-strip design and grant corpus/GPU access, or leave the
finding open with its existing `ponytail:` marker. Leaving it open is a
legitimate outcome: the leak is bounded to pages in the first convert chunk
whose furniture debuts late, and the marker already records the ceiling.
