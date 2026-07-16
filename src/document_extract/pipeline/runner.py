"""Document-level orchestration, replay handling, and run outputs."""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from ..artifacts import combine_markdown, summarize_token_usage, write_jsonl
from ..docling_adapter import (
    bbox_dict,
    build_docling_converter,
    convert_pdf,
    item_page_number,
    item_text,
    iter_doc_items,
)
from ..layout.furniture import repeated_furniture_signatures
from ..layout.geometry import bbox_to_normalized_rect
from ..layout.prompt_map import layout_map_stats
from ..prompts import load_page_repair_prompt, load_page_refinement_prompt, load_summary_prompt
from ..runtime import (
    CHECKPOINT_FILENAME,
    REPLAY_STAGES,
    STAGES,
    PageState,
    StatusReporter,
    clear_downstream_artifacts,
    invalidate_from,
    new_page_state,
    stage_index,
    validate_checkpoint,
)
from .stages import (
    _prepare_page,
    _run_finalize,
    _run_page_refine,
    _run_page_repair,
    _run_picture_extract,
    _run_picture_triage,
    _run_table_detect,
    _run_table_extract,
)
from .state import _candidates_from_state, _records_from_state, _visual_candidates_from_state
from ..tables import table_candidate_rows
from ..visual_values import COLLECTOR_VERSION

# Pages per Docling convert() call, which is also the prefetch unit: the next
# chunk is converted in a persistent worker process while the current chunk's
# pages go through the VLM stages.
CONVERT_CHUNK_PAGES = 32

# Persistent Docling worker process. docling-parse holds the GIL for the whole
# PDF-parse phase, so prefetching in a background *thread* stalls the foreground
# page loop instead of overlapping with it. Running convert() in a separate
# process sidesteps the GIL. The converter is expensive to build
# (build_docling_converter loads the OCR and table-structure ML models), so it
# is built once per worker via the pool initializer and reused for every chunk.
_WORKER_CONVERTER: Any | None = None


def _init_convert_worker() -> None:
    """Build the Docling converter once in the persistent worker process.

    This runs in a spawned subprocess, so it must re-establish the driver's
    import order: torch first, so its bundled CUDA/cuDNN DLLs load before docling
    and onnxruntime-gpu (RapidOCR's OCR engine) — otherwise OCR silently falls
    back to CPU on Windows. torch is a driver dependency rather than one of
    document_extract, so its absence is not fatal here.
    """
    try:
        import torch  # noqa: F401, PLC0415
    except ImportError:
        pass
    global _WORKER_CONVERTER
    _WORKER_CONVERTER = build_docling_converter()


def _convert_chunk_in_worker(pdf_path: Path, pages: list[int]) -> Any:
    """Convert one page chunk in the worker using its prebuilt converter.

    Returns the DoclingDocument, which is pickled back across the process
    boundary. It is a pydantic model whose picture images are stored as base64
    data URIs (generate_picture_images=True), so it serializes cheaply.
    """
    return convert_pdf(_WORKER_CONVERTER, pdf_path, pages)


def _chunk_furniture_signatures(
    document: Any,
    chunk: list[int],
    page_sizes: dict[int, tuple[float, float]],
) -> set[tuple[str, str]]:
    """Cross-page repetition signatures for one converted chunk.

    Furniture detection needs to compare pages against each other; the chunk
    (one Docling convert() call) is the widest window in which all items are
    available before the per-page loop consumes them.
    """
    pages_entries: dict[int, list[tuple[str, list[float] | None]]] = {
        page: [] for page in chunk
    }
    chunk_set = set(chunk)
    for item in iter_doc_items(document):
        page = item_page_number(item)
        if page not in chunk_set:
            continue
        text = item_text(item)
        if not text:
            continue
        rect = bbox_to_normalized_rect(bbox_dict(item), page_sizes[page])
        pages_entries[page].append((text, rect))
    return repeated_furniture_signatures(pages_entries)


def ensure_pdf(path: Path) -> Path:
    pdf_path = path.expanduser().resolve()
    if not pdf_path.exists():
        raise FileNotFoundError(f"Missing PDF: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a PDF file: {pdf_path}")
    return pdf_path

def selected_page_numbers(page_count: int, start_page: int, end_page: int) -> list[int]:
    if start_page < 1:
        raise ValueError("--start-page must be at least 1")
    if start_page > page_count:
        raise ValueError(
            f"--start-page {start_page} exceeds the document's {page_count} pages"
        )
    final_end = end_page or page_count
    if final_end < start_page:
        raise ValueError("--end-page must be >= --start-page")
    return list(range(start_page, min(final_end, page_count) + 1))

def _manifest_row(state: PageState) -> dict[str, Any]:
    page_dir = Path(state.page_dir)
    records = _records_from_state(state)
    candidates = _candidates_from_state(state)
    return {
        "page": state.page,
        "page_dir": page_dir.name,
        "status": state.status,
        "failure": state.failure,
        "page_role": state.page_role or None,
        "docling_raw": str(page_dir / "docling_raw.md"),
        "docling_final": str(page_dir / "docling_final.md"),
        "layout_prompt_map": str(page_dir / "layout_prompt_map.json"),
        "layout_prompt_map_repair": str(page_dir / "layout_prompt_map_repair.json")
        if state.repair_layout_map
        else None,
        "reading_order": state.reading_order,
        **layout_map_stats(state.layout_map),
        "table_candidates_path": str(page_dir / "table_candidates.json"),
        "table_candidate_count": len(candidates),
        "table_candidate_kinds": sorted({candidate.kind for candidate in candidates}),
        "table_candidates": table_candidate_rows(candidates),
        "visual_audit": state.visual_audit,
        "visual_candidate_count": len(state.visual_candidates),
        "table_extraction_usage": [
            {
                "candidate_id": candidate.candidate_id,
                "kind": candidate.kind,
                "usage": candidate.usage,
            }
            for candidate in candidates
            if candidate.usage
        ],
        "page_vlm_usage": state.page_vlm_usage,
        "page_repair_usage": state.page_repair_usage,
        "picture_count": len(records),
        "pictures": [asdict(record) for record in records],
        "warnings": state.warnings,
        "stage_history": [asdict(record) for record in state.stage_history],
    }


def _warn_stale_page_dirs(output_root: Path, states: list[PageState]) -> list[str]:
    """List page_* dirs under the output root that this run did not produce.

    Leftovers from an older run (different page range) sit next to current
    pages and would silently pollute a RAG index built from the directory.
    Never deletes — the user decides.
    """
    current = {Path(state.page_dir).name for state in states}
    stale = sorted(
        child.name
        for child in output_root.glob("page_*")
        if child.is_dir() and child.name not in current
    )
    if stale:
        print(
            f"WARNING: {len(stale)} page dir(s) in {output_root} are not part of "
            f"this run (stale from an earlier run?): {', '.join(stale)}",
            flush=True,
        )
    return stale


def _write_all_outputs(output_root: Path) -> None:
    """Rebuild manifest, blocks, and combined Markdown from every page_* dir on disk.

    Covers pages outside the current run's page range so a partial run never
    replaces the document-wide aggregates with a subset.
    """
    all_page_dirs = sorted(
        (d for d in output_root.glob("page_*") if d.is_dir()),
        key=lambda d: int(d.name.split("_", 1)[1]) if d.name.split("_", 1)[1].isdigit() else 0,
    )
    all_out = output_root / "all"
    all_out.mkdir(exist_ok=True)

    all_states: list[PageState] = []
    for page_dir in all_page_dirs:
        checkpoint = page_dir / CHECKPOINT_FILENAME
        if not checkpoint.exists():
            continue
        try:
            state = PageState.load(checkpoint)
        except Exception:  # noqa: BLE001
            continue
        all_states.append(state)

    (all_out / "manifest_all.json").write_text(
        json.dumps([_manifest_row(state) for state in all_states], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    write_jsonl(
        all_out / "blocks_all.jsonl",
        [row for state in all_states if state.status == "completed" for row in state.block_rows],
    )
    combine_markdown(all_page_dirs, all_out / "combined_docling_final_all.md", "docling_final.md")


def _write_run_outputs(output_root: Path, states: list[PageState]) -> None:
    ordered = sorted(states, key=lambda state: state.page)
    manifest = [_manifest_row(state) for state in ordered]
    successful = [state for state in ordered if state.status == "completed"]
    raw_dirs = [Path(state.page_dir) for state in ordered if (Path(state.page_dir) / "docling_raw.md").exists()]
    final_dirs = [Path(state.page_dir) for state in successful]
    block_rows = [row for state in successful for row in state.block_rows]
    write_jsonl(output_root / "blocks.jsonl", block_rows)
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (output_root / "token_usage.json").write_text(
        json.dumps(summarize_token_usage(manifest), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    combine_markdown(raw_dirs, output_root / "combined_docling_raw.md", "docling_raw.md")
    combine_markdown(final_dirs, output_root / "combined_docling_final.md", "docling_final.md")
    _write_all_outputs(output_root)
    _warn_stale_page_dirs(output_root, ordered)


def _has_current_visual_audit(
    state: PageState, page_dir: Path, visual_values_mode: str
) -> bool:
    """Return whether a resumed non-off run has complete collector state."""
    audit = state.visual_audit or {}
    if (
        not audit.get("completed")
        or audit.get("collector_version") != COLLECTOR_VERSION
        or audit.get("mode") != visual_values_mode
        or audit.get("candidate_count") != len(state.visual_candidates)
        or not (page_dir / "visual_candidates.json").exists()
    ):
        return False
    return all(
        (candidate.get("stats") or {}).get("visual_values", {}).get("mode")
        == visual_values_mode
        and (candidate.get("stats") or {}).get("visual_values", {}).get("collector_version")
        == COLLECTOR_VERSION
        for candidate in state.table_candidates
    )


def run_pipeline(args: argparse.Namespace) -> int:
    import fitz  # noqa: PLC0415

    visual_values_mode = getattr(args, "visual_values_mode", "off")
    if visual_values_mode not in {"off", "audit", "enforce"}:
        raise ValueError("--visual-values-mode must be off, audit, or enforce")
    if not args.triage_model:
        args.triage_model = args.ollama_model
    if not 0.0 <= args.triage_confidence <= 1.0:
        raise ValueError("--triage-confidence must be between 0 and 1")
    pdf_path = ensure_pdf(args.pdf)
    visual_reader = None
    if visual_values_mode != "off":
        from pypdf import PdfReader  # noqa: PLC0415

        visual_reader = PdfReader(str(pdf_path))
    output_root = args.output_dir.expanduser().resolve() / pdf_path.stem
    output_root.mkdir(parents=True, exist_ok=True)
    page_refinement_prompt = load_page_refinement_prompt(args.prompt_file)
    page_repair_prompt = load_page_repair_prompt()
    image_summary_prompt = load_summary_prompt(None)

    with fitz.open(pdf_path) as pdf_doc:
        pages = selected_page_numbers(len(pdf_doc), args.start_page, args.end_page)
        page_sizes = {
            page: (
                float(pdf_doc.load_page(page - 1).rect.width),
                float(pdf_doc.load_page(page - 1).rect.height),
            )
            for page in pages
        }
        # Docling's convert() re-parses the whole PDF on every call (~20-70s fixed
        # cost) before running the ML models on the requested range, and
        # docling-parse holds the GIL for that whole parse phase. Pages are
        # converted in chunks to amortize the parse, and the next chunk is
        # prefetched in a persistent worker process while the main thread runs
        # the VLM stages for the current chunk, hiding the Docling time behind
        # the VLM calls. A separate process (not a thread) is required so the
        # GIL-holding parse overlaps with the foreground page loop instead of
        # stalling it. The converter is built once inside the worker (via the
        # pool initializer), so it is never built or held in the main process.
        # At most one convert() is in flight at a time, and at most two chunk
        # documents are held in memory (current + prefetched).
        use_prefetch = not args.resume_from
        chunks = [
            pages[i : i + CONVERT_CHUNK_PAGES]
            for i in range(0, len(pages), CONVERT_CHUNK_PAGES)
        ]

        def _submit_chunk(pool: ProcessPoolExecutor, chunk: list[int]) -> Future:
            print(
                f"Prefetching pages {chunk[0]}-{chunk[-1]} with Docling "
                f"({len(chunk)} pages, one parse)...",
                flush=True,
            )
            return pool.submit(_convert_chunk_in_worker, pdf_path, chunk)

        pool = (
            ProcessPoolExecutor(max_workers=1, initializer=_init_convert_worker)
            if use_prefetch
            else None
        )
        pending: Future | None = (
            _submit_chunk(pool, chunks[0]) if pool is not None else None
        )
        chunk_index = -1
        document = None
        chunk_last_page = 0
        chunk_furniture: set[tuple[str, str]] = set()
        total_pages = len(pages)
        run_started_at = time.perf_counter()
        states: list[PageState] = []

        try:
            for page_index, page_number in enumerate(pages, start=1):
                if pool is not None and page_number > chunk_last_page:
                    chunk_index += 1
                    chunk = chunks[chunk_index]
                    wait_started = time.perf_counter()
                    # Re-raises background conversion errors at the chunk boundary.
                    document = pending.result()
                    print(
                        f"Docling chunk {chunk[0]}-{chunk[-1]} ready "
                        f"(main thread waited {time.perf_counter() - wait_started:.1f}s)",
                        flush=True,
                    )
                    chunk_last_page = chunk[-1]
                    chunk_furniture = _chunk_furniture_signatures(
                        document, chunk, page_sizes
                    )
                    if chunk_index + 1 < len(chunks):
                        pending = _submit_chunk(pool, chunks[chunk_index + 1])
                page_started_at = time.perf_counter()
                page_dir = output_root / f"page_{page_number:04d}"
                page_dir.mkdir(parents=True, exist_ok=True)
                reporter = StatusReporter(
                    page_index=page_index, total_pages=total_pages, page=page_number
                )
                state: PageState | None = None
                try:
                    if args.resume_from:
                        state = PageState.load(page_dir / "page_state.json")
                        validate_checkpoint(
                            state,
                            pdf_path=pdf_path,
                            page=page_number,
                            dpi=args.dpi,
                            page_dir=page_dir,
                        )
                        state.page_dir = str(page_dir)
                        effective_resume_from = args.resume_from
                        if stage_index(effective_resume_from) > stage_index("table_detect"):
                            audit = state.visual_audit or {}
                            stale_visual_state = (
                                visual_values_mode == "off"
                                and (
                                    state.visual_values_mode != "off"
                                    or bool(audit)
                                    or any(
                                        (candidate.get("stats") or {}).get("visual_values")
                                        for candidate in state.table_candidates
                                    )
                                )
                            ) or (
                                visual_values_mode != "off"
                                and not _has_current_visual_audit(
                                    state, page_dir, visual_values_mode
                                )
                            )
                            if stale_visual_state:
                                effective_resume_from = "table_detect"
                        clear_downstream_artifacts(page_dir, effective_resume_from)
                        invalidate_from(state, effective_resume_from)
                        state.visual_values_mode = visual_values_mode
                        state.save()
                        runtime: dict[str, Any] = {
                            "items": None,
                            "records": _records_from_state(state),
                            "candidates": _candidates_from_state(state),
                            "visual_candidates": _visual_candidates_from_state(state),
                        }
                        start_index = stage_index(effective_resume_from)
                        reporter.emit("page", "resume", from_stage=effective_resume_from)
                    else:
                        state = new_page_state(
                            pdf_path=pdf_path,
                            page=page_number,
                            page_index=page_index,
                            total_pages=total_pages,
                            dpi=args.dpi,
                            page_dir=page_dir,
                            page_size=page_sizes[page_number],
                            visual_values_mode=visual_values_mode,
                        )
                        runtime = _prepare_page(
                            state=state,
                            reporter=reporter,
                            args=args,
                            pdf_doc=pdf_doc,
                            document=document,
                            page_size=page_sizes[page_number],
                            furniture_signatures=chunk_furniture,
                        )
                        start_index = stage_index("picture_triage")

                    stage_functions = {
                        "picture_triage": lambda: _run_picture_triage(
                            state=state, reporter=reporter, args=args, runtime=runtime
                        ),
                        "picture_extract": lambda: _run_picture_extract(
                            state=state,
                            reporter=reporter,
                            args=args,
                            runtime=runtime,
                            prompt=image_summary_prompt,
                        ),
                        "table_detect": lambda: _run_table_detect(
                            state=state,
                            reporter=reporter,
                            runtime=runtime,
                            reader=visual_reader,
                            mode=visual_values_mode,
                        ),
                        "table_extract": lambda: _run_table_extract(
                            state=state, reporter=reporter, args=args, runtime=runtime
                        ),
                        "page_refine": lambda: _run_page_refine(
                            state=state,
                            reporter=reporter,
                            args=args,
                            runtime=runtime,
                            prompt=page_refinement_prompt,
                        ),
                        "page_repair": lambda: _run_page_repair(
                            state=state,
                            reporter=reporter,
                            args=args,
                            runtime=runtime,
                            prompt=page_repair_prompt,
                        ),
                        "finalize": lambda: _run_finalize(
                            state=state, reporter=reporter, runtime=runtime
                        ),
                    }
                    for stage in STAGES[start_index:]:
                        stage_functions[stage]()
                    state.status = "completed"
                    state.save()
                    reporter.emit(
                        "page",
                        "ok",
                        seconds=f"{time.perf_counter() - page_started_at:.1f}",
                        total=f"{time.perf_counter() - run_started_at:.1f}",
                    )
                except Exception as error:  # noqa: BLE001
                    if state is None:
                        state = new_page_state(
                            pdf_path=pdf_path,
                            page=page_number,
                            page_index=page_index,
                            total_pages=total_pages,
                            dpi=args.dpi,
                            page_dir=page_dir,
                            page_size=page_sizes[page_number],
                            visual_values_mode=visual_values_mode,
                        )
                    if state.status != "failed":
                        state.status = "failed"
                        state.failure = {
                            "stage": args.resume_from or "prepare",
                            "error_type": type(error).__name__,
                            "error_message": str(error),
                        }
                        state.save()
                        reporter.emit("page", "fail", error=type(error).__name__)
                states.append(state)
        finally:
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)

    _write_run_outputs(output_root, states)
    failed = [state for state in states if state.status == "failed"]
    print(
        f"Done. Outputs written to {output_root} "
        f"pages={len(states)} failed={len(failed)}",
        flush=True,
    )
    return 1 if failed else 0

def run_document(argv: Sequence[str] | None = None) -> int:
    """Run the argument-based public API without mutating process arguments."""
    from ..cli import run
    return int(run(argv))


__all__ = [
    "ensure_pdf", "selected_page_numbers", "run_pipeline",
    "run_document",
]
