"""Document-level orchestration, replay handling, and run outputs."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from ..artifacts import combine_markdown, summarize_token_usage, write_jsonl
from ..docling_adapter import build_docling_converter
from ..layout.prompt_map import layout_map_stats
from ..prompts import load_page_repair_prompt, load_page_refinement_prompt, load_summary_prompt
from ..runtime import (
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
from .state import _candidates_from_state, _records_from_state
from ..tables import table_candidate_rows

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


def run_pipeline(args: argparse.Namespace) -> int:
    import fitz  # noqa: PLC0415

    if not args.triage_model:
        args.triage_model = args.ollama_model
    if not 0.0 <= args.triage_confidence <= 1.0:
        raise ValueError("--triage-confidence must be between 0 and 1")
    pdf_path = ensure_pdf(args.pdf)
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
        converter = None if args.resume_from else build_docling_converter()
        total_pages = len(pages)
        run_started_at = time.perf_counter()
        states: list[PageState] = []

        for page_index, page_number in enumerate(pages, start=1):
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
                    clear_downstream_artifacts(page_dir, args.resume_from)
                    invalidate_from(state, args.resume_from)
                    state.save()
                    runtime: dict[str, Any] = {
                        "items": None,
                        "records": _records_from_state(state),
                        "candidates": _candidates_from_state(state),
                    }
                    start_index = stage_index(args.resume_from)
                    reporter.emit("page", "resume", from_stage=args.resume_from)
                else:
                    state = new_page_state(
                        pdf_path=pdf_path,
                        page=page_number,
                        page_index=page_index,
                        total_pages=total_pages,
                        dpi=args.dpi,
                        page_dir=page_dir,
                        page_size=page_sizes[page_number],
                    )
                    runtime = _prepare_page(
                        state=state,
                        reporter=reporter,
                        args=args,
                        pdf_doc=pdf_doc,
                        converter=converter,
                        page_size=page_sizes[page_number],
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
                        state=state, reporter=reporter, runtime=runtime
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
