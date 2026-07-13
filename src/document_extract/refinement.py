"""Page refinement, deterministic cleanup, and repair safeguards."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from .docling_adapter import is_table_item
from .layout.prompt_map import layout_map_prompt_json
from .llm import ollama as ollama_client
from .markdown import postprocess as sp
from .markdown.formatting import (
    IMAGE_PLACEHOLDER_RE,
    apply_list_levels_from_layout,
    drop_duplicate_subset_tables,
    drop_empty_header_row,
    drop_orphan_header_tables,
    insert_image_references_and_summaries,
    mark_redundant_summaries,
    missing_verified_table_ids,
    normalize_headerless_pipe_tables,
    normalize_pipe_tables,
    pipe_row_count,
    replace_deterministic_tables,
    replace_sectioned_tables,
    standalone_value_line_count,
    strip_br_lines,
    unwrap_layout_tables,
)
from .models import PictureRecord, TableCandidate
from .tables import verified_tables_prompt_block

def format_page_refinement_prompt(
    *,
    prompt_template: str,
    source_markdown: str,
    layout_blocks: dict[str, Any],
    table_candidates: list[TableCandidate],
) -> str:
    return prompt_template.format(
        source_markdown=source_markdown,
        layout_blocks=layout_map_prompt_json(layout_blocks),
        verified_tables=verified_tables_prompt_block(table_candidates),
    )


def format_page_repair_prompt(
    *,
    prompt_template: str,
    current_markdown: str,
    layout_blocks: dict[str, Any],
    table_candidates: list[TableCandidate],
    unplaced_lines: list[str],
) -> str:
    unplaced_block = "\n".join(f"- {line}" for line in unplaced_lines) if unplaced_lines else "(none)"
    return prompt_template.format(
        current_markdown=current_markdown,
        layout_blocks=layout_map_prompt_json(layout_blocks),
        verified_tables=verified_tables_prompt_block(table_candidates),
        unplaced_lines=unplaced_block,
    )


def refine_page_markdown(
    *,
    source_markdown: str,
    layout_blocks: dict[str, Any],
    table_candidates: list[TableCandidate],
    page_image_path: Path,
    prompt_template: str,
    args: argparse.Namespace,
) -> tuple[str, dict[str, Any] | None]:
    if args.skip_vlm:
        return source_markdown, None
    prompt = format_page_refinement_prompt(
        prompt_template=prompt_template,
        source_markdown=source_markdown,
        layout_blocks=layout_blocks,
        table_candidates=table_candidates,
    )
    answer, usage = ollama_client.call_ollama_vlm(
        base_url=args.ollama_base_url,
        model=args.ollama_model,
        prompt=prompt,
        image_path=page_image_path,
        temperature=args.temperature,
        num_ctx=args.num_ctx,
        num_predict=args.num_predict,
        auto_num_ctx=args.auto_num_ctx,
    )
    refined = sp.strip_meta_commentary(ollama_client.strip_markdown_fences(answer))
    return refined or source_markdown, usage


def postprocess_markdown(
    source_markdown: str,
    working_markdown: str,
    records: list[PictureRecord],
    table_candidates: list[TableCandidate] | None = None,
    layout_blocks: dict[str, Any] | None = None,
    furniture_texts: set[str] | None = None,
    page_role: str | None = None,
) -> tuple[str, dict[str, Any]]:
    is_toc = page_role == "toc"
    final = sp.flatten_html_tables(working_markdown)
    final = sp.normalize_bullets_and_headings(final)
    final = sp.demote_datapoint_headings(final)
    flagged_summaries = mark_redundant_summaries(source_markdown, records)
    final = insert_image_references_and_summaries(final, records)
    final = sp.strip_meta_commentary(final)
    final = sp.normalize_footnotes(final)
    # After flatten_html_tables (escaped entities must not turn into real
    # HTML before the table parser runs), before the table/text cleanups.
    final = sp.unescape_html_entities(final)
    final = normalize_pipe_tables(final)
    table_warnings: dict[str, int] = {}
    final, kpi_count = sp.convert_kpi_pipe_tables_to_lists(final)
    if kpi_count:
        table_warnings["kpi_tables_converted"] = kpi_count
    final, kpi_text_pairs = sp.pair_kpi_text_runs(final)
    if kpi_text_pairs:
        table_warnings["kpi_text_pairs"] = kpi_text_pairs
    final, orphan_texts = drop_orphan_header_tables(final)
    if orphan_texts:
        table_warnings["orphan_header_tables_dropped"] = len(orphan_texts)
    final, unwrapped_count = unwrap_layout_tables(final)
    if unwrapped_count:
        table_warnings["layout_tables_unwrapped"] = unwrapped_count
    final = strip_br_lines(final)
    final, empty_count = drop_empty_header_row(final)
    if empty_count:
        table_warnings["empty_tables_dropped"] = empty_count
    headerless_rows = [
        tuple((candidate.stats or {}).get("first_row", []))
        for candidate in (table_candidates or [])
        if candidate.kind == "docling_table"
        and (candidate.stats or {}).get("headerless")
        and (candidate.stats or {}).get("first_row")
    ]
    final = normalize_headerless_pipe_tables(final, headerless_rows)
    final = sp.dedupe_span_header_cells(final)
    final = sp.collapse_banner_rows(final)
    # Deterministically guarantee section-banded docling tables appear as their
    # pre-split subtables (splicing them in if the VLM degraded them into
    # headings + lists). Must precede drop_duplicate_subset_tables and the
    # completeness guard.
    final, enforced_sectioned = replace_sectioned_tables(final, table_candidates)
    if enforced_sectioned:
        table_warnings["sectioned_tables_enforced"] = len(enforced_sectioned)
    # Same guarantee for regular/title_detail tables rendered from the grid at
    # transcription time: splice the authoritative table over its raw Docling
    # twin (or a VLM-degraded remnant) so it lands verbatim even under --skip-vlm.
    final, enforced_deterministic = replace_deterministic_tables(final, table_candidates)
    if enforced_deterministic:
        table_warnings["deterministic_tables_enforced"] = len(enforced_deterministic)
    final = apply_list_levels_from_layout(final, layout_blocks)
    if is_toc:
        # The VLM flattens TOC section numbers into sequential ordered lists;
        # restore the real numbers from the raw TOC table.
        final = sp.restore_toc_section_numbers(final, source_markdown)
    # The refine/repair VLM re-transcribes margin furniture from the page
    # image even when the Docling items were dropped in prepare.
    final = sp.strip_furniture_lines(final, furniture_texts)

    warnings: dict[str, Any] = {}
    warnings.update(table_warnings)
    # Route the model's `## Uncertain mappings` block to a sidecar warning
    # instead of the final markdown. Image references inside it return to the
    # body (images are never sidecar content); its text lines re-enter the
    # unplaced flow below so genuinely uncertain content is not lost.
    final, uncertain_sidecar = sp.extract_uncertainty(final)
    uncertain_lines: list[str] = []
    if uncertain_sidecar:
        warnings["uncertain_mappings"] = uncertain_sidecar
        uncertain_images: list[str] = []
        for line in uncertain_sidecar.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if sp.IMAGE_PLACEHOLDER_LINE_RE.match(stripped):
                uncertain_images.append(stripped)
                continue
            content = re.sub(r"^[-*]\s+", "", stripped)
            # Bare chapter-tab transcriptions ("1", "4") are junk, not content.
            if re.fullmatch(r"\d{1,2}|[A-Z]", content):
                continue
            uncertain_lines.append(content)
        if uncertain_images:
            final = final.rstrip() + "\n\n" + "\n\n".join(uncertain_images) + "\n"
    if flagged_summaries:
        warnings["redundant_image_summaries"] = flagged_summaries
    unplaced_symbols = [
        record.placeholder
        for record in records
        if record.summary_type == "symbol"
        and record.summary.strip()
        and not any(
            line.strip().startswith("|") and record.summary.strip() in line
            for line in final.splitlines()
        )
    ]
    if unplaced_symbols:
        warnings["table_symbols_unplaced"] = unplaced_symbols
    final, dropped_tables = drop_duplicate_subset_tables(final, table_candidates)
    if dropped_tables:
        warnings["duplicate_tables_dropped"] = dropped_tables
    # Dedupe before the completeness guard: the guard diffs against the raw
    # markdown, which contains each paragraph once, so dropped copies are not
    # re-appended as missing content.
    final, dropped_paragraphs = sp.collapse_duplicate_paragraphs(final)
    if dropped_paragraphs:
        warnings["duplicate_paragraphs_dropped"] = [
            paragraph[:80] for paragraph in dropped_paragraphs
        ]
    # Strip furniture from the raw side too: checkpoints written before the
    # prepare-stage filter existed still carry furniture in raw_markdown, and
    # the guard must not re-append it as "missing" content. Orphan layout-
    # table headers were removed deliberately; the guard must not resurrect
    # them either, so their texts join the filter set.
    guard_filter_texts = {
        sp.normalize_furniture_text(text) for text in orphan_texts
    } | (furniture_texts or set())
    if is_toc:
        # A restructured TOC always diffs against its raw table, so the guard
        # would re-append the whole raw TOC as unplaced noise. The full TOC
        # stays available in docling_raw.md.
        guard_warnings: dict[str, Any] = {
            "content_loss_guard_triggered": False,
            "unplaced_suppressed_toc": True,
        }
    else:
        final, guard_warnings = apply_completeness_guard(
            sp.strip_furniture_lines(source_markdown, furniture_texts),
            final,
            furniture_texts=guard_filter_texts,
            extra_lines=uncertain_lines,
        )
    warnings.update(guard_warnings)
    footnote_warnings = sp.footnote_consistency(final)
    if footnote_warnings:
        warnings["footnotes"] = footnote_warnings
    meta_warnings = sp.meta_commentary_warnings(final)
    if meta_warnings:
        warnings["meta_commentary"] = meta_warnings
    return final, warnings


def should_run_repair_pass(
    *,
    items: list[Any],
    warnings: dict[str, Any],
    current_markdown: str,
    records: list[PictureRecord],
    table_candidates: list[TableCandidate] | None = None,
    has_docling_table: bool | None = None,
) -> bool:
    if warnings.get("content_loss_guard_triggered"):
        return True
    if warnings.get("verified_tables_missing"):
        return True
    if warnings.get("table_symbols_unplaced"):
        return True
    table_present = (
        has_docling_table
        if has_docling_table is not None
        else any(is_table_item(item) for item in items)
    )
    if table_present:
        return True
    if len(records) >= 2 and standalone_value_line_count(current_markdown) >= 3:
        return True
    return False


def repair_page_markdown(
    *,
    current_markdown: str,
    layout_blocks: dict[str, Any],
    table_candidates: list[TableCandidate],
    page_image_path: Path,
    unplaced_lines: list[str],
    prompt_template: str,
    args: argparse.Namespace,
) -> tuple[str, dict[str, Any] | None]:
    if args.skip_vlm:
        return current_markdown, None
    prompt = format_page_repair_prompt(
        prompt_template=prompt_template,
        current_markdown=current_markdown,
        layout_blocks=layout_blocks,
        table_candidates=table_candidates,
        unplaced_lines=unplaced_lines,
    )
    answer, usage = ollama_client.call_ollama_vlm(
        base_url=args.ollama_base_url,
        model=args.ollama_model,
        prompt=prompt,
        image_path=page_image_path,
        temperature=args.temperature,
        num_ctx=args.num_ctx,
        num_predict=args.num_predict,
        auto_num_ctx=args.auto_num_ctx,
    )
    repaired = sp.strip_meta_commentary(ollama_client.strip_markdown_fences(answer))
    return repaired or current_markdown, usage


def strip_unplaced_section(markdown: str) -> str:
    lines = markdown.splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        if re.match(r"^##\s+Unplaced content\s*$", line.strip()):
            skipping = True
            continue
        if skipping and re.match(r"^#{1,2}\s+\S", line):
            skipping = False
        if not skipping:
            out.append(line)
    return "\n".join(out)


def repair_regression_reasons(
    pre_markdown: str,
    repaired_markdown: str,
    usage: dict[str, Any] | None,
) -> list[str]:
    """Why a repair result must be rejected in favor of the pre-repair markdown.

    A repair pass re-emits the whole page, so a truncated or lossy generation
    silently replaces a good result — this guard makes that impossible.
    """
    reasons: list[str] = []
    for key in ("length_capped", "decoding_anomaly", "context_overflow"):
        if usage and usage.get(key):
            reasons.append(key)
    pre_body = strip_unplaced_section(pre_markdown)
    repaired_body = strip_unplaced_section(repaired_markdown)
    if len(repaired_body) < 0.7 * len(pre_body):
        reasons.append("shrunk_output")
    if pipe_row_count(repaired_body) < pipe_row_count(pre_body):
        reasons.append("fewer_table_rows")
    if sp.completeness_diff(pre_markdown, repaired_markdown):
        reasons.append("content_loss_vs_pre_repair")
    # A repair that duplicates content is as broken as one that loses it:
    # an accepted repair once re-emitted a whole column twice (page 19).
    if sp.duplicate_paragraph_count(repaired_body) > sp.duplicate_paragraph_count(
        pre_body
    ):
        reasons.append("duplicate_content_added")
    return reasons


def apply_completeness_guard(
    raw_markdown: str,
    final_markdown: str,
    furniture_texts: set[str] | None = None,
    extra_lines: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    warnings: dict[str, Any] = {}
    raw_for_diff = IMAGE_PLACEHOLDER_RE.sub("", raw_markdown)
    final_for_diff = IMAGE_PLACEHOLDER_RE.sub("", final_markdown)
    missing = sp.completeness_diff(raw_for_diff, final_for_diff)
    seen = {re.sub(r"\s+", " ", line).strip().lower() for line in missing}
    for line in extra_lines or []:
        normalized = re.sub(r"\s+", " ", line).strip().lower()
        if normalized not in seen:
            seen.add(normalized)
            missing.append(line)
    kept, dropped = sp.filter_unplaced_lines(missing, final_markdown, furniture_texts)
    if kept:
        final = sp.merge_unplaced_content(final_markdown, kept)
        warnings["content_loss_guard_triggered"] = True
        warnings["unplaced_content_lines"] = kept
    else:
        final = final_markdown
        warnings["content_loss_guard_triggered"] = False
    if dropped:
        warnings["unplaced_lines_filtered"] = dropped
    return final, warnings

__all__ = [
    "format_page_refinement_prompt", "format_page_repair_prompt",
    "refine_page_markdown", "postprocess_markdown",
    "should_run_repair_pass", "repair_page_markdown",
    "strip_unplaced_section", "repair_regression_reasons", "apply_completeness_guard",
]
