"""Page refinement, deterministic cleanup, and repair safeguards."""

from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .docling_adapter import is_table_item
from .layout.prompt_map import collapse_ws, layout_map_prompt_json
from .llm import ollama as ollama_client
from .markdown import postprocess as sp
from .markdown.formatting import (
    IMAGE_PLACEHOLDER_RE,
    apply_list_levels_from_layout,
    drop_duplicate_subset_tables,
    drop_empty_header_row,
    drop_orphan_header_tables,
    insert_image_references_and_summaries,
    is_loose_symbol_line,
    mark_redundant_summaries,
    missing_verified_table_ids,
    normalize_headerless_pipe_tables,
    normalize_pipe_tables,
    pipe_row_count,
    realign_collapsed_span_headers,
    reconcile_table_columns,
    replace_deterministic_tables,
    replace_sectioned_tables,
    standalone_value_line_count,
    strip_loose_symbol_lines,
    strip_br_lines,
    unwrap_layout_tables,
)
from .markdown.pipe import split_row
from .models import PictureRecord, TableCandidate
from .tables import verified_tables_prompt_block


_SYMBOL_CODE_RE = re.compile(r"^[A-Z]{1,3}\d{0,2}$")
_SYMBOL_CODE_TOKEN_RE = re.compile(r"\b[A-Z]{1,3}\d{0,2}\b")
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
PLAIN_PAGE_MIN_CHARS = 300


def page_is_plain_prose(
    raw_markdown: str,
    records: list[PictureRecord],
    table_candidates: list[TableCandidate],
    has_docling_table: bool,
    reading_order: dict[str, Any],
) -> bool:
    return (
        len(raw_markdown.strip()) >= PLAIN_PAGE_MIN_CHARS
        and not any(record.summarize or record.summary for record in records)
        and not table_candidates
        and not has_docling_table
        and not reading_order.get("applied")
    )


def _symbol_value_tokens(value: str) -> list[str]:
    """Normalized symbol tokens, keeping non-code summaries as one exact value."""
    normalized = collapse_ws(value).upper()
    parts = [part for part in re.split(r"[\s,]+", normalized) if part]
    if parts and all(_SYMBOL_CODE_RE.fullmatch(part) for part in parts):
        return parts
    return [normalized] if normalized else []


def _geometry_unplaced_symbol_records(
    records: list[PictureRecord], table_candidates: list[TableCandidate] | None
) -> list[PictureRecord]:
    """Symbol records explicitly left without a geometry-backed logical row."""
    unplaced_indices: set[int] = set()
    for candidate in table_candidates or []:
        unplaced_indices.update(
            int(index)
            for index in (candidate.stats or {}).get("symbols_unplaced_geometry", [])
        )
    return sorted(
        [
            record
            for record in records
            if record.index in unplaced_indices
            and record.summary_type == "symbol"
            and record.summary.strip()
        ],
        key=lambda record: record.index,
    )


def _protected_symbol_value_budget(records: list[PictureRecord]) -> dict[str, int]:
    """How many loose code links may be deferred for known geometry deficits."""
    budget: Counter[str] = Counter()
    for record in records:
        for value in _symbol_value_tokens(record.summary):
            if _SYMBOL_CODE_RE.fullmatch(value):
                budget[value] += 1
    return dict(budget)


def _protected_loose_symbol_line(
    line: str, records: list[PictureRecord], protected_records: list[PictureRecord]
) -> bool:
    """Whether a deferred loose link carries a code from a known deficit."""
    if not is_loose_symbol_line(line, records):
        return False
    values = {
        value
        for record in protected_records
        for value in _symbol_value_tokens(record.summary)
        if _SYMBOL_CODE_RE.fullmatch(value)
    }
    upper = line.upper()
    return any(re.search(rf"\b{re.escape(value)}\b", upper) for value in values)


def _pipe_table_symbol_counts(markdown: str, expected_values: set[str]) -> Counter[str]:
    """Count symbol values in actual pipe-table cells, never image-link dumps."""
    counts: Counter[str] = Counter()
    non_codes = {value for value in expected_values if not _SYMBOL_CODE_RE.fullmatch(value)}
    for line in markdown.splitlines():
        stripped = line.strip()
        # Blank pipe rows are non-data here, unlike is_separator_line().
        if not stripped.startswith("|") or set(stripped) <= set("|-: "):
            continue
        for cell in split_row(line):
            clean = _MARKDOWN_IMAGE_RE.sub("", cell)
            normalized = collapse_ws(clean).upper()
            for code in _SYMBOL_CODE_TOKEN_RE.findall(normalized):
                if code in expected_values:
                    counts[code] += 1
            if normalized in non_codes:
                counts[normalized] += 1
    return counts


def audit_table_symbol_instances(
    markdown: str,
    records: list[PictureRecord],
    table_candidates: list[TableCandidate] | None,
) -> list[PictureRecord]:
    """Return symbol records whose individual table occurrence is still absent.

    Candidate placement metadata identifies the logical row of each placed
    picture. Identical values in one logical cell collapse to one expected
    occurrence; equal values in different rows remain separate.  Legacy or
    non-deterministic records use the same multiset accounting without row
    metadata, so one surviving ``S1`` cannot satisfy every ``S1`` picture.
    """
    records_by_index = {
        record.index: record
        for record in records
        if record.summary_type == "symbol" and record.summary.strip()
    }
    # (value, priority, candidate order, logical row, picture index, record)
    occurrences: list[tuple[str, int, int, int, int, PictureRecord]] = []
    claimed_indices: set[int] = set()
    placed_groups: dict[tuple[str, int, str], tuple[int, int, int, PictureRecord]] = {}
    for candidate_order, candidate in enumerate(table_candidates or []):
        stats = candidate.stats or {}
        raw_indices = set(stats.get("symbol_picture_indices", []))
        raw_indices.update(stats.get("symbols_placed_geometry", []))
        raw_indices.update(stats.get("symbols_unplaced_geometry", []))
        if not raw_indices:
            continue
        assignments = {
            int(index): int(row)
            for index, row in (stats.get("symbol_row_assignments") or {}).items()
        }
        unplaced = {int(index) for index in stats.get("symbols_unplaced_geometry", [])}
        for raw_index in sorted(raw_indices, key=int):
            index = int(raw_index)
            if index in claimed_indices or index not in records_by_index:
                continue
            claimed_indices.add(index)
            record = records_by_index[index]
            for value in _symbol_value_tokens(record.summary):
                if index in unplaced:
                    occurrences.append((value, 1, candidate_order, -1, index, record))
                elif index in assignments:
                    key = (candidate.candidate_id, assignments[index], value)
                    current = placed_groups.get(key)
                    if current is None or index < current[2]:
                        placed_groups[key] = (
                            candidate_order,
                            assignments[index],
                            index,
                            record,
                        )
                else:
                    # Older/interrupted metadata names an expected picture but
                    # not its row. Keep conservative multiset accounting.
                    occurrences.append((value, 2, candidate_order, -1, index, record))
    for (_candidate_id, row, value), (order, _, index, record) in placed_groups.items():
        occurrences.append((value, 0, order, row, index, record))
    for index, record in records_by_index.items():
        if index not in claimed_indices:
            for value in _symbol_value_tokens(record.summary):
                occurrences.append((value, 2, len(table_candidates or []), -1, index, record))

    expected_values = {value for value, *_ in occurrences}
    observed = _pipe_table_symbol_counts(markdown, expected_values)
    unresolved_indices: set[int] = set()
    for value in expected_values:
        available = observed[value]
        for _, _, _, _, index, record in sorted(
            (occurrence for occurrence in occurrences if occurrence[0] == value),
            key=lambda occurrence: (
                occurrence[1], occurrence[2], occurrence[3], occurrence[4]
            ),
        ):
            if available:
                available -= 1
            else:
                unresolved_indices.add(index)
    return [
        record
        for record in sorted(records_by_index.values(), key=lambda record: record.index)
        if record.index in unresolved_indices
    ]


def _symbol_deficit_lines(records: list[PictureRecord]) -> list[str]:
    return [
        "table symbol "
        f"picture_index={record.index} value={record.summary.strip()} is not placed"
        for record in records
    ]

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
    unplaced_block = (
        "\n".join(f"- {line}" for line in unplaced_lines)
        if unplaced_lines
        else "(none — every line is already placed; do not output an '## Unplaced content' section)"
    )
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
    final = sp.normalize_pipe_table_cell_bullets(final)
    final = sp.strip_redundant_list_glyphs(final)
    final = sp.normalize_bullets_and_headings(final)
    final = sp.collapse_internal_spaces(final)
    final = sp.demote_datapoint_headings(final)
    flagged_summaries = mark_redundant_summaries(source_markdown, records)
    final = insert_image_references_and_summaries(final, records)
    protected_symbol_records = _geometry_unplaced_symbol_records(records, table_candidates)
    final, loose_symbol_lines_stripped = strip_loose_symbol_lines(
        final,
        records,
        protected_symbol_values=_protected_symbol_value_budget(protected_symbol_records),
    )
    vlm_unplaced_removed = sum(
        1
        for line in final.splitlines()
        if re.fullmatch(r"##\s+Unplaced content\s*", line.strip(), re.IGNORECASE)
    )
    final, vlm_unplaced_entries = sp.extract_unplaced_sections(final)
    vlm_unplaced_survivors: list[str] = []
    protected_loose_survivors: list[str] = []
    vlm_unplaced_dropped = 0
    for entry in vlm_unplaced_entries:
        content = re.sub(r"^[-*]\s+", "", entry.strip())
        if not content:
            continue
        if content.lower() == "(none)":
            vlm_unplaced_dropped += 1
            continue
        if is_loose_symbol_line(content, records):
            if _protected_loose_symbol_line(content, records, protected_symbol_records):
                protected_loose_survivors.append(content)
            else:
                vlm_unplaced_dropped += 1
            continue
        vlm_unplaced_survivors.append(content)
    final = sp.strip_meta_commentary(final)
    final = sp.normalize_footnotes(final)
    # After flatten_html_tables (escaped entities must not turn into real
    # HTML before the table parser runs), before the table/text cleanups.
    final = sp.unescape_html_entities(final)
    # Before normalize_pipe_tables: once a short row is right-padded, whether
    # its missing cell was internal or trailing can no longer be recovered.
    final, table_warnings = reconcile_table_columns(final, table_candidates)
    final = normalize_pipe_tables(final)
    final, realigned_headers = realign_collapsed_span_headers(final, table_candidates)
    if realigned_headers:
        table_warnings["span_headers_realigned"] = realigned_headers
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
    if vlm_unplaced_removed:
        recycled = 0 if is_toc else len(vlm_unplaced_survivors)
        if is_toc:
            vlm_unplaced_dropped += len(vlm_unplaced_survivors)
        warnings["vlm_unplaced_sections"] = {
            "removed": vlm_unplaced_removed,
            "recycled": recycled,
            "dropped": vlm_unplaced_dropped,
        }
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
    if not is_toc:
        uncertain_lines.extend(vlm_unplaced_survivors)
    if flagged_summaries:
        warnings["redundant_image_summaries"] = flagged_summaries
    final, dropped_tables = drop_duplicate_subset_tables(final, table_candidates)
    if dropped_tables:
        warnings["duplicate_tables_dropped"] = dropped_tables
    unplaced_symbol_records = audit_table_symbol_instances(
        final, records, table_candidates
    )
    # Deferred loose links are never valid table cells. Once their fate is
    # audited, remove the image syntax; a remaining deficit becomes explicit
    # text below instead of a silently dropped image reference.
    final, deferred_loose_lines_stripped = strip_loose_symbol_lines(final, records)
    loose_symbol_lines_stripped += deferred_loose_lines_stripped
    if unplaced_symbol_records:
        warnings["table_symbols_unplaced"] = [
            record.placeholder for record in unplaced_symbol_records
        ]
    elif protected_loose_survivors:
        # A complete VLM/raw table made the deferred links redundant after all.
        loose_symbol_lines_stripped += len(protected_loose_survivors)
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
    if unplaced_symbol_records:
        final = sp.merge_unplaced_content(
            final, _symbol_deficit_lines(unplaced_symbol_records)
        )
        warnings["loose_symbol_lines_preserved_as_unplaced"] = len(
            unplaced_symbol_records
        )
    if loose_symbol_lines_stripped:
        warnings["loose_symbol_lines_stripped"] = loose_symbol_lines_stripped
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
        docling = [
            candidate
            for candidate in (table_candidates or [])
            if candidate.kind == "docling_table"
        ]
        # A verified docling table is spliced deterministically in postprocess;
        # repair only earns its call when some table lacks a verified rendering
        # (or detection produced no candidate for the table item at all).
        if not docling or any(not candidate.verified for candidate in docling):
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
    records: list[PictureRecord] | None = None,
    table_candidates: list[TableCandidate] | None = None,
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
    # Value retention is not column retention: a repair may move a total under
    # the previous column's header. Rows the source grid can adjudicate are
    # repositioned deterministically in postprocess; a repair that turns such a
    # row into one the grid can no longer place is the case left to reject.
    if table_candidates:
        _, pre_columns = reconcile_table_columns(pre_body, table_candidates)
        _, repaired_columns = reconcile_table_columns(repaired_body, table_candidates)
        if repaired_columns.get("ambiguous_table_arity", 0) > pre_columns.get(
            "ambiguous_table_arity", 0
        ):
            reasons.append("ambiguous_table_arity")
    if records is not None:
        pre_missing = audit_table_symbol_instances(
            pre_markdown, records, table_candidates
        )
        repaired_missing = audit_table_symbol_instances(
            repaired_markdown, records, table_candidates
        )
        if len(repaired_missing) > len(pre_missing):
            reasons.append("fewer_table_symbol_instances")
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
    "should_run_repair_pass", "repair_page_markdown", "audit_table_symbol_instances",
    "strip_unplaced_section", "repair_regression_reasons", "apply_completeness_guard",
    "page_is_plain_prose",
]
