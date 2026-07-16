"""Generic audit of tagged-PDF values that are represented visually.

The module deliberately keeps one small, source-neutral record.  It reads
``ActualText`` from tagged content today; later collectors can append evidence
and proposals to the same ``VisualCandidate`` without changing table authority
logic.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict
from typing import Any, Iterable

from .models import PictureRecord, TableCandidate, VisualCandidate


COLLECTOR_VERSION = 1
_WS_RE = re.compile(r"\s+", re.UNICODE)
_PAINT_OPERATORS = {
    b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"sh",
}
_TEXT_SHOW_OPERATORS = {b"Tj", b"TJ", b"'", b'"'}


def normalize_actual_text(raw: Any) -> str | None:
    """Return an output-preserving normalized PDF replacement string.

    The comparison representation is intentionally separate; this function
    never splits values or rewrites case, punctuation, or delimiters.
    """
    value = _decode_pdf_string(raw)
    if value is None:
        return None
    value = value.rstrip("\x00")
    value = unicodedata.normalize("NFC", value)
    value = _WS_RE.sub(" ", value).strip()
    return value or None


def comparison_key(value: Any) -> str:
    """Whitespace/case-folded key used only for equality checks."""
    normalized = normalize_actual_text(value)
    return _WS_RE.sub(" ", normalized or "").strip().casefold()


def collect_tagged_candidates(
    reader: Any, page_number: int, page_size: tuple[float, float]
) -> list[VisualCandidate]:
    """Collect tagged marked-content groups from one PDF page.

    The parser purposefully errs toward diagnostics: malformed strings,
    unresolved parent-tree links, and non-painting content are retained rather
    than guessed into a table cell.
    """
    try:
        page = reader.pages[page_number - 1]
    except Exception:
        return []

    parent_tree = _parent_tree(reader)
    struct_info = _structure_info(reader)
    crop = _page_crop(page, page_size)
    rotation = _page_rotation(page)
    groups: list[dict[str, Any]] = []
    try:
        _walk_content(
            page.get_contents(),
            reader=reader,
            resources=_resolved_dict(page.get("/Resources")),
            owner=_int_value(page.get("/StructParents")),
            ctm=_IDENTITY_MATRIX,
            crop=crop,
            rotation=rotation,
            groups=groups,
        )
    except Exception:
        # A broken page still should not abort document extraction.  Any groups
        # completed before the parser stopped remain useful audit evidence.
        pass

    candidates: list[VisualCandidate] = []
    for index, group in enumerate(groups, start=1):
        actual_seen = bool(group.get("actual_seen"))
        owner = group.get("owner")
        mcid = group.get("mcid")
        parent_entries = _parent_entries(parent_tree, owner, mcid)
        struct = struct_info.get(_object_key(parent_entries[0])) if len(parent_entries) == 1 else None
        raw = group.get("actual_text")
        if raw is None and struct:
            raw = struct.get("actual_text")
            actual_seen = actual_seen or raw is not None
        if not actual_seen:
            continue

        reason_codes = ["marked_content"]
        if mcid is None:
            reason_codes.append("missing_mcid")
        elif len(parent_entries) == 1:
            reason_codes.append("parent_tree_unique")
        elif len(parent_entries) > 1:
            reason_codes.append("parent_tree_ambiguous")
        else:
            reason_codes.append("parent_tree_unresolved")
        if struct and struct.get("source_struct_row") is not None:
            reason_codes.append("structure_cell")

        paint_state = _paint_state(group)
        reason_codes.append(f"paint_{paint_state}")
        rect = _rect_union(group.get("rects", []))
        visible_rect = _visible_rect(rect)
        if rect is not None and visible_rect is None:
            paint_state = "not_visible"
            reason_codes.append("outside_visible_page")
        source_ref = _source_ref(owner, mcid, parent_entries)
        evidence: dict[str, Any] = {
            "source": "tagged_pdf",
            "source_ref": source_ref,
            "norm_rect": visible_rect,
            "paint_state": paint_state,
            "confidence": 1.0 if "parent_tree_unique" in reason_codes else None,
            "reason_codes": reason_codes,
        }
        if struct:
            evidence["structure"] = {
                "tags": list(struct.get("tags", [])),
                "table_ref": struct.get("table_ref"),
                "source_struct_row": struct.get("source_struct_row"),
                "source_struct_col": struct.get("source_struct_col"),
            }

        # A visible text-show operation proves this marked group is ordinary
        # native text, even when its embedded font has no usable Unicode
        # mapping.  The replacement string is then accessibility metadata for
        # text Docling can already recover, not a missing visual-only value.
        # Invisible/off-page text and mixed text/vector scopes remain retained
        # diagnostics rather than being silently discarded.
        if any(group.get("visible_shown_texts", [])) and not group.get("visible_nontext_paint"):
            continue
        decoded = _decode_pdf_string(raw) or ""
        normalized = normalize_actual_text(raw)
        proposals: list[dict[str, Any]] = []
        if normalized is not None:
            proposals.append(
                {
                    "method": "actual_text",
                    "raw_value": decoded,
                    "normalized_value": normalized,
                    "comparison_key": comparison_key(normalized),
                    "confidence": 1.0 if "parent_tree_unique" in reason_codes else None,
                }
            )
        else:
            reason_codes.append("empty_or_malformed_actual_text")
        candidates.append(
            VisualCandidate(
                candidate_id=f"vv{page_number:04d}_{index:03d}",
                page=page_number,
                norm_rect=visible_rect,
                evidence=[evidence],
                proposals=proposals,
                detection_confidence=evidence["confidence"],
                reasons=list(reason_codes),
            )
        )
    return candidates


def merge_picture_evidence(
    candidates: list[VisualCandidate],
    picture_records: Iterable[PictureRecord] | dict[int, PictureRecord],
) -> list[VisualCandidate]:
    """Attach existing picture provenance without changing picture extraction."""
    records = picture_records.values() if isinstance(picture_records, dict) else picture_records
    merged = list(candidates)
    next_index = 1
    for record in records:
        rect = list(record.norm_rect) if record.norm_rect else None
        match = next(
            (
                candidate
                for candidate in merged
                if _rect_overlap(candidate.norm_rect, rect) >= 0.7
            ),
            None,
        )
        if match is None:
            # A picture without a value proposal has no generic semantic signal;
            # keep it out of the visual-value audit until it has one.
            if not (record.summary_type == "symbol" and record.summary.strip()):
                continue
            while any(candidate.candidate_id == f"pv{record.page:04d}_{next_index:03d}" for candidate in merged):
                next_index += 1
            match = VisualCandidate(
                candidate_id=f"pv{record.page:04d}_{next_index:03d}",
                page=record.page,
                norm_rect=rect,
                reasons=["picture_only"],
            )
            next_index += 1
            merged.append(match)
        evidence = {
            "source": "docling_picture",
            "source_ref": f"picture:{record.index}",
            "norm_rect": rect,
            "paint_state": "visible" if rect else "unknown",
            "confidence": record.triage_confidence,
            "geometry_relation": (
                "coextensive" if _rect_iou(match.norm_rect, rect) >= 0.7 else "component_overlap"
            ),
            "reason_codes": ["picture_record"],
        }
        match.evidence.append(evidence)
        if record.summary.strip():
            match.proposals.append(
                {
                    "method": "picture_summary",
                    "source_ref": f"picture:{record.index}",
                    "raw_value": record.summary,
                    "normalized_value": normalize_actual_text(record.summary) or "",
                    "comparison_key": comparison_key(record.summary),
                    "confidence": record.triage_confidence,
                }
            )
    return merged


def associate_table_cells(
    candidates: list[VisualCandidate],
    table_candidates: list[TableCandidate],
    page_size: tuple[float, float],
) -> None:
    """Map only uniquely placed candidates to one output table cell.

    Source structure coordinates remain provenance.  Geometry and normalized
    output-record bands make the actual output mapping, so continuation rows
    and spans cannot be copied directly into a Docling grid.
    """
    layouts = {
        table.candidate_id: _table_layout(table, page_size)
        for table in table_candidates
        if table.kind == "docling_table" and (table.stats or {}).get("grid")
    }
    selectable = [table for table in table_candidates if table.candidate_id in layouts]
    by_structure: dict[str, list[VisualCandidate]] = {}
    for candidate in candidates:
        candidate.target = {}
        _set_affected_tables(candidate, [])
        table_ref = _candidate_structure(candidate).get("table_ref")
        if table_ref:
            by_structure.setdefault(str(table_ref), []).append(candidate)
    structure_tables = {
        table_ref: [
            table
            for table in selectable
            if all(
                _rect_inside(candidate.norm_rect, layouts[table.candidate_id]["table_rect"])
                for candidate in grouped
            )
        ]
        for table_ref, grouped in by_structure.items()
    }

    existing_values: dict[str, str] = {}
    alignment_groups: dict[tuple[str, str], list[VisualCandidate]] = {}
    for candidate in candidates:
        structure = _candidate_structure(candidate)
        table_ref = structure.get("table_ref")
        matching_tables = (
            structure_tables.get(str(table_ref), [])
            if table_ref
            else [
                table
                for table in selectable
                if _rect_inside(candidate.norm_rect, layouts[table.candidate_id]["table_rect"])
            ]
        )
        if len(matching_tables) != 1:
            _set_affected_tables(candidate, matching_tables)
            _append_reason(candidate, "ambiguous_or_missing_table")
            continue
        table = matching_tables[0]
        _set_affected_tables(candidate, [table])
        layout = layouts[table.candidate_id]
        center = _rect_center(candidate.norm_rect)
        if center is None:
            _append_reason(candidate, "missing_candidate_geometry")
            continue
        raw_matches = [
            key for key, rect in layout["cell_rects"].items() if _point_inside(center, rect)
        ]
        targets: set[tuple[int, int]] = set()
        for row, column in raw_matches:
            for record_index, record in enumerate(layout["records"]):
                if row in record["source_rows"] and column < layout["num_cols"]:
                    targets.add((record_index, column))
        if len(targets) != 1:
            _append_reason(candidate, "ambiguous_or_missing_cell")
            continue
        record_index, column_index = targets.pop()
        candidate.target = {
            "kind": "table_cell",
            "table_candidate_id": table.candidate_id,
            "record_index": record_index,
            "column_index": column_index,
            "source_struct_row": structure.get("source_struct_row"),
            "source_struct_col": structure.get("source_struct_col"),
        }
        candidate.placement_confidence = 1.0
        _append_reason(candidate, "unique_table_cell")
        existing_values[candidate.candidate_id] = _record_cell_text(
            layout["records"], record_index, column_index
        )
        if structure.get("source_struct_row") is not None:
            alignment_groups.setdefault(
                (table.candidate_id, str(table_ref or "")), []
            ).append(candidate)

    for grouped in alignment_groups.values():
        by_row: dict[int, set[int]] = {}
        for candidate in grouped:
            try:
                row = int(candidate.target["source_struct_row"])
                record = int(candidate.target["record_index"])
            except (KeyError, TypeError, ValueError):
                continue
            by_row.setdefault(row, set()).add(record)
        latest = -1
        monotonic = True
        for row in sorted(by_row):
            records = by_row[row]
            if min(records) < latest:
                monotonic = False
                break
            latest = max(latest, max(records))
        if monotonic:
            continue
        for candidate in grouped:
            table_id = candidate.target.get("table_candidate_id")
            table = next((item for item in selectable if item.candidate_id == table_id), None)
            _set_affected_tables(candidate, [table] if table else [])
            candidate.target = {}
            candidate.placement_confidence = None
            existing_values.pop(candidate.candidate_id, None)
            _append_reason(candidate, "non_monotonic_structure_alignment")

    for candidate in candidates:
        assess_candidate(candidate, existing_values.get(candidate.candidate_id, ""))
    _resolve_target_conflicts(candidates)


def assess_candidate(candidate: VisualCandidate, existing_cell_text: str) -> None:
    """Apply the small generic trust and conflict decision.

    Tagged replacement text is the primary value.  A picture summary competes
    only when its geometry is coextensive with that tagged object; a component
    summary inside a larger tagged group is corroborating context, not a
    delimiter-based reinterpretation of the opaque tagged scalar.
    """
    proposals = [
        proposal
        for proposal in candidate.proposals
        if proposal.get("method") == "actual_text" and proposal.get("normalized_value")
    ]
    proposal_keys = {
        str(proposal.get("comparison_key") or comparison_key(proposal.get("normalized_value")))
        for proposal in proposals
    }
    if len(proposal_keys) > 1:
        candidate.semantic = "uncertain"
        candidate.resolution = "conflict"
        candidate.resolved_value = ""
        candidate.value_confidence = None
        _append_reason(candidate, "conflicting_proposals")
        return
    picture_keys = {
        str(proposal.get("comparison_key") or comparison_key(proposal.get("normalized_value")))
        for proposal in candidate.proposals
        if proposal.get("method") == "picture_summary"
        and proposal.get("normalized_value")
        and _picture_proposal_is_coextensive(candidate, proposal)
    }
    if proposal_keys and picture_keys and proposal_keys != picture_keys:
        candidate.semantic = "uncertain"
        candidate.resolution = "conflict"
        candidate.resolved_value = ""
        candidate.value_confidence = None
        _append_reason(candidate, "conflicting_coextensive_picture_proposal")
        return
    if not proposals:
        candidate.semantic = "uncertain"
        candidate.resolution = "unresolved"
        candidate.resolved_value = ""
        _append_reason(candidate, "no_usable_actual_text")
        return

    proposal = proposals[0]
    value = str(proposal["normalized_value"])
    candidate.resolved_value = value
    candidate.value_confidence = proposal.get("confidence")
    evidence = [entry for entry in candidate.evidence if entry.get("source") == "tagged_pdf"]
    reason_codes = {
        code for entry in evidence for code in entry.get("reason_codes", [])
    }
    paint_states = {entry.get("paint_state") for entry in evidence}
    if comparison_key(existing_cell_text) == str(proposal.get("comparison_key") or comparison_key(value)):
        candidate.semantic = "informational"
        candidate.semantic_confidence = 1.0
        candidate.resolution = "already_present"
        _append_reason(candidate, "native_value_matches")
        return
    if (
        {"parent_tree_unique", "owner_mcid_unique"} & reason_codes
        and "structure_cell" in reason_codes
        and "visible" in paint_states
        and candidate.target.get("kind") == "table_cell"
    ):
        candidate.semantic = "informational"
        candidate.semantic_confidence = 1.0
        candidate.resolution = "trusted"
        _append_reason(candidate, "trusted_tagged_value")
        return
    candidate.semantic = "uncertain"
    candidate.semantic_confidence = None
    candidate.resolution = "unresolved"
    if "not_visible" in paint_states:
        _append_reason(candidate, "non_painting_tag")
    elif "unknown" in paint_states:
        _append_reason(candidate, "unknown_paint")
    else:
        _append_reason(candidate, "insufficient_generic_evidence")


def _resolve_target_conflicts(candidates: list[VisualCandidate]) -> None:
    """Reject distinct accepted tagged values aimed at one explicit cell."""
    grouped: dict[tuple[str, int, int], list[VisualCandidate]] = {}
    for candidate in candidates:
        target = candidate.target
        if candidate.resolution not in {"trusted", "already_present"} or target.get("kind") != "table_cell":
            continue
        try:
            key = (
                str(target["table_candidate_id"]),
                int(target["record_index"]),
                int(target["column_index"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        grouped.setdefault(key, []).append(candidate)
    for grouped_candidates in grouped.values():
        values = {
            comparison_key(candidate.resolved_value)
            for candidate in grouped_candidates
            if candidate.resolved_value
        }
        if len(values) <= 1:
            continue
        for candidate in grouped_candidates:
            candidate.semantic = "uncertain"
            candidate.semantic_confidence = None
            candidate.resolution = "conflict"
            candidate.value_confidence = None
            _append_reason(candidate, "conflicting_cell_targets")


def update_visual_completeness(
    table_candidates: list[TableCandidate],
    candidates: list[VisualCandidate],
    *,
    mode: str,
) -> None:
    """Persist per-table visual completeness without granting authority."""
    for table in table_candidates:
        observed = [
            candidate
            for candidate in candidates
            if _candidate_affects_table(candidate, table.candidate_id)
        ]
        represented = [
            candidate.candidate_id
            for candidate in observed
            if candidate.resolution == "already_present"
            or candidate.semantic == "non_informational"
        ]
        missing = [
            candidate.candidate_id
            for candidate in observed
            if candidate.resolution == "trusted"
        ]
        unresolved = [
            candidate.candidate_id
            for candidate in observed
            if candidate.resolution in {"unresolved", "conflict"}
        ]
        if not observed:
            status = "not_observed"
            reason_codes = ["no_relevant_visual_candidates"]
        elif missing:
            status = "incomplete"
            reason_codes = ["trusted_value_missing_from_grid"]
        elif unresolved:
            status = "uncertain"
            reason_codes = ["unresolved_visual_evidence"]
        else:
            status = "complete"
            reason_codes = ["all_observed_values_represented"]
        table.stats = {
            **(table.stats or {}),
            "visual_values": {
                "status": status,
                "mode": mode,
                "collector_version": COLLECTOR_VERSION,
                "attempted_sources": ["tagged_pdf", "docling_picture"] if mode != "off" else [],
                "candidate_ids": [candidate.candidate_id for candidate in observed],
                "represented_ids": represented,
                "missing_ids": missing,
                "unresolved_ids": unresolved,
                "reason_codes": reason_codes,
            },
        }


def reconcile_picture_evidence(
    table_candidates: list[TableCandidate], candidates: list[VisualCandidate]
) -> None:
    """Recognize only exact legacy picture placements before recomputing audit.

    Geometry overlap is deliberately insufficient here.  The legacy path can
    place several symbols in the same table, so a tagged proposal is considered
    represented only when its matching picture summary was assigned to its exact
    output cell and the two values agree.  Picture-only candidates have no
    competing tagged value and use the same exact cell assignment.
    """
    tables = {table.candidate_id: table for table in table_candidates}
    for candidate in candidates:
        if candidate.resolution not in {"trusted", "unresolved", ""}:
            continue
        target = candidate.target
        if target.get("kind") != "table_cell":
            continue
        table = tables.get(str(target.get("table_candidate_id") or ""))
        if table is None:
            continue
        for evidence in candidate.evidence:
            if evidence.get("source") != "docling_picture":
                continue
            source_ref = str(evidence.get("source_ref") or "")
            index = _picture_index(source_ref)
            if index is None or not _legacy_picture_targets_cell(table, index, target):
                continue
            picture = _picture_proposal(candidate, source_ref)
            if picture is None:
                continue
            actual = _actual_proposal(candidate)
            if actual is not None and (
                candidate.resolution != "trusted"
                or picture.get("comparison_key") != actual.get("comparison_key")
            ):
                continue
            candidate.semantic = "informational"
            candidate.resolution = "already_present"
            candidate.resolved_value = str(picture.get("normalized_value") or "")
            candidate.semantic_confidence = 1.0
            _append_reason(candidate, "legacy_picture_value_placed")
            break


def _picture_index(source_ref: str) -> int | None:
    try:
        prefix, value = source_ref.rsplit(":", 1)
        return int(value) if prefix == "picture" else None
    except (TypeError, ValueError):
        return None


def _legacy_picture_targets_cell(
    table: TableCandidate, picture_index: int, target: dict[str, Any]
) -> bool:
    stats = table.stats or {}
    assignments = stats.get("symbol_cell_assignments") or {}
    assigned = assignments.get(picture_index, assignments.get(str(picture_index)))
    if isinstance(assigned, dict):
        try:
            return (
                int(assigned.get("record_index")) == int(target.get("record_index"))
                and int(assigned.get("column_index")) == int(target.get("column_index"))
            )
        except (TypeError, ValueError):
            return False
    # Older checkpoints lack the additive cell map.  A row-only assignment is
    # not enough to establish exact cell representation, so leave the audit
    # unresolved instead of promoting it optimistically.
    return False


def _picture_proposal(candidate: VisualCandidate, source_ref: str) -> dict[str, Any] | None:
    return next(
        (
            proposal
            for proposal in candidate.proposals
            if proposal.get("method") == "picture_summary"
            and proposal.get("source_ref") == source_ref
            and proposal.get("normalized_value")
        ),
        None,
    )


def _picture_proposal_is_coextensive(
    candidate: VisualCandidate, proposal: dict[str, Any]
) -> bool:
    source_ref = proposal.get("source_ref")
    return any(
        evidence.get("source") == "docling_picture"
        and evidence.get("source_ref") == source_ref
        and evidence.get("geometry_relation") == "coextensive"
        for evidence in candidate.evidence
    )


def _actual_proposal(candidate: VisualCandidate) -> dict[str, Any] | None:
    return next(
        (
            proposal
            for proposal in candidate.proposals
            if proposal.get("method") == "actual_text" and proposal.get("normalized_value")
        ),
        None,
    )


def visual_audit_summary(
    table_candidates: list[TableCandidate], candidates: list[VisualCandidate], *, mode: str
) -> dict[str, Any]:
    """Compact checkpoint sentinel and console-friendly metrics."""
    statuses = [
        str((table.stats or {}).get("visual_values", {}).get("status", "not_observed"))
        for table in table_candidates
    ]
    return {
        "completed": True,
        "collector_version": COLLECTOR_VERSION,
        "mode": mode,
        "candidate_count": len(candidates),
        "table_status_counts": {status: statuses.count(status) for status in sorted(set(statuses))},
    }


def visual_candidate_rows(candidates: list[VisualCandidate]) -> list[dict[str, Any]]:
    """Serialize compact diagnostics without reducing in-memory precision."""
    return [_round_diagnostic_value(asdict(candidate)) for candidate in candidates]


def apply_trusted_visual_values(
    table_candidates: list[TableCandidate], candidates: list[VisualCandidate], *, mode: str
) -> int:
    """Insert trusted opaque values into their explicit normalized-grid targets.

    The raw grid receives the same scalar after the normalized insert so the
    existing deterministic renderer can still add legacy picture symbols.
    """
    if mode != "enforce":
        return 0
    tables = {table.candidate_id: table for table in table_candidates}
    inserted = 0
    for table_id, table in tables.items():
        chosen = [
            candidate
            for candidate in candidates
            if candidate.resolution == "trusted"
            and candidate.target.get("table_candidate_id") == table_id
            and candidate.target.get("kind") == "table_cell"
        ]
        if not chosen:
            continue
        stats = table.stats or {}
        if (
            stats.get("headerless")
            or stats.get("kpi_panel")
            or stats.get("sectioned_split")
            or stats.get("format") not in {None, "regular_table", "title_detail_table"}
        ):
            continue
        normalized = _normalized_grid(table)
        if normalized is None or normalized.get("classification") not in {
            "regular_table",
            "title_detail_table",
        }:
            continue
        inserted_ids: list[str] = []
        for candidate in chosen:
            value = candidate.resolved_value or next(
                (
                    str(proposal.get("normalized_value"))
                    for proposal in candidate.proposals
                    if proposal.get("method") == "actual_text"
                    and proposal.get("normalized_value")
                ),
                "",
            )
            if not value:
                continue
            try:
                record_index = int(candidate.target["record_index"])
                column_index = int(candidate.target["column_index"])
            except (KeyError, TypeError, ValueError):
                continue
            existing = _record_cell_text(normalized["records"], record_index, column_index)
            if existing == value:
                assess_candidate(candidate, existing)
                continue
            if existing or not _source_grid_can_accept(
                table, normalized, record_index, column_index
            ):
                continue
            if not _place_normalized_value(normalized, record_index, column_index, value):
                continue
            if not _copy_value_to_source_grid(
                table, normalized, record_index, column_index, value
            ):
                continue
            assess_candidate(
                candidate,
                _record_cell_text(normalized["records"], record_index, column_index),
            )
            inserted += 1
            inserted_ids.append(candidate.candidate_id)
        if inserted_ids:
            # A replayed deterministic table may already have Markdown from a
            # prior stage.  Force its supported renderer to rebuild it from the
            # source grid after a successful insert.
            table.verified = False
            table.stats = {
                **(table.stats or {}),
                "visual_values_inserted": sorted(
                    {
                        *list((table.stats or {}).get("visual_values_inserted", [])),
                        *inserted_ids,
                    }
                ),
            }
    return inserted


def _place_normalized_value(
    normalized: dict[str, Any], record_index: int, column_index: int, value: str
) -> bool:
    # Keep the direct helper in tables as the sole insertion policy.  Importing
    # lazily avoids a module cycle while models/tables load during startup.
    from .tables import place_visual_value

    return place_visual_value(normalized, record_index, column_index, value)


def _normalized_grid(table: TableCandidate) -> dict[str, Any] | None:
    from .tables import normalize_table_grid

    return normalize_table_grid((table.stats or {}).get("grid"))


def _copy_value_to_source_grid(
    table: TableCandidate,
    normalized: dict[str, Any],
    record_index: int,
    column_index: int,
    value: str,
) -> bool:
    try:
        source_rows = list(normalized["records"][record_index]["source_rows"])
        grid = (table.stats or {})["grid"]
        rows = grid["rows"]
    except (IndexError, KeyError, TypeError):
        return False
    for source_row in reversed(source_rows):
        if not isinstance(source_row, int) or source_row < 0 or source_row >= len(rows):
            continue
        row = rows[source_row]
        if column_index >= len(row):
            row.extend([""] * (column_index + 1 - len(row)))
        if row[column_index] in {"", value}:
            row[column_index] = value
            for cell in grid.get("cells", []):
                if cell.get("r") == source_row and cell.get("c") == column_index:
                    cell["text"] = value
            return True
    return False


def _source_grid_can_accept(
    table: TableCandidate,
    normalized: dict[str, Any],
    record_index: int,
    column_index: int,
) -> bool:
    """Check source-grid mutability before changing an ephemeral normalized grid."""
    try:
        source_rows = list(normalized["records"][record_index]["source_rows"])
        rows = (table.stats or {})["grid"]["rows"]
    except (IndexError, KeyError, TypeError):
        return False
    for source_row in reversed(source_rows):
        if not isinstance(source_row, int) or source_row < 0 or source_row >= len(rows):
            continue
        row = rows[source_row]
        if column_index >= len(row) or row[column_index] == "":
            return True
    return False


def _table_layout(table: TableCandidate, page_size: tuple[float, float]) -> dict[str, Any]:
    grid = (table.stats or {}).get("grid") or {}
    table_rect = _grid_table_rect(grid, table.bbox, page_size)
    normalized = _normalized_grid(table)
    if normalized is None:
        header_rows = _int_value(grid.get("header_rows")) or 0
        records = [
            {"cells": list(row), "source_rows": [index]}
            for index, row in enumerate(grid.get("rows", []))
            if index >= header_rows
        ]
        normalized = {
            "records": records,
            "num_cols": _grid_columns(grid),
        }
    return {
        "records": normalized["records"],
        "num_cols": int(normalized["num_cols"]),
        "cell_rects": _grid_cell_rects(grid, table_rect, page_size),
        "table_rect": table_rect,
    }


def _grid_table_rect(
    grid: dict[str, Any], fallback: list[float] | None, page_size: tuple[float, float]
) -> list[float] | None:
    """Use unrounded Docling cell geometry for table containment when available."""
    rects = [
        rect
        for cell in grid.get("cells", [])
        if isinstance(cell, dict)
        for rect in [_docling_rect(cell.get("bbox"), page_size)]
        if rect is not None
    ]
    return _rect_union(rects) or fallback


def _grid_cell_rects(
    grid: dict[str, Any], table_bbox: list[float] | None, page_size: tuple[float, float]
) -> dict[tuple[int, int], list[float]]:
    rows = grid.get("rows") or []
    row_count = len(rows)
    column_count = _grid_columns(grid)
    edge_anchors: dict[tuple[int, int], list[float]] = {}
    known: dict[tuple[int, int], list[float]] = {}
    for cell in grid.get("cells", []):
        if not isinstance(cell, dict):
            continue
        rect = _docling_rect(cell.get("bbox"), page_size)
        if rect is None:
            continue
        row_range = _cell_coordinate_range(
            cell, "r", "start_row_offset_idx", "end_row_offset_idx", "row_span"
        )
        column_range = _cell_coordinate_range(
            cell, "c", "start_col_offset_idx", "end_col_offset_idx", "col_span"
        )
        if not row_range or not column_range:
            continue
        anchor = (row_range[0], column_range[0])
        edge_anchors.setdefault(anchor, rect)
        for row in row_range:
            for column in column_range:
                # A spanned Docling cell intentionally remains ambiguous across
                # its logical slots.  Association then withholds a guess rather
                # than copying a structure row directly into the output grid.
                known.setdefault((row, column), rect)
    if not table_bbox or row_count == 0 or column_count == 0:
        return known
    left, top, right, bottom = _ordered_rect(table_bbox)
    x_edges = _grid_edges(edge_anchors, axis=0, count=column_count, low=left, high=right)
    y_edges = _grid_edges(edge_anchors, axis=1, count=row_count, low=top, high=bottom)
    for row in range(row_count):
        for column in range(column_count):
            known.setdefault(
                (row, column),
                [x_edges[column], y_edges[row], x_edges[column + 1], y_edges[row + 1]],
            )
    return known


def _cell_coordinate_range(
    cell: dict[str, Any],
    legacy_key: str,
    start_key: str,
    end_key: str,
    span_key: str,
) -> range:
    """Return inclusive logical coordinates from Docling offsets or spans."""
    start = _int_value(cell.get(start_key))
    if start is None:
        start = _int_value(cell.get(legacy_key))
    if start is None or start < 0:
        return range(0)
    end = _int_value(cell.get(end_key))
    if end is None:
        span = _int_value(cell.get(span_key)) or 1
        end = start + max(span, 1) - 1
    if end < start:
        return range(0)
    return range(start, end + 1)


def _grid_edges(
    known: dict[tuple[int, int], list[float]],
    *,
    axis: int,
    count: int,
    low: float,
    high: float,
) -> list[float]:
    values: list[list[float]] = [[] for _ in range(count + 1)]
    for (row, column), rect in known.items():
        index = column if axis == 0 else row
        if 0 <= index < count:
            values[index].append(rect[axis])
            values[index + 1].append(rect[axis + 2])
    edges: list[float | None] = [None] * (count + 1)
    edges[0], edges[-1] = low, high
    for index, entries in enumerate(values):
        if entries:
            candidate = sum(entries) / len(entries)
            if index not in {0, count}:
                edges[index] = candidate
    anchors = [index for index, value in enumerate(edges) if value is not None]
    for start, end in zip(anchors, anchors[1:]):
        start_value = float(edges[start])
        end_value = float(edges[end])
        for index in range(start + 1, end):
            if edges[index] is None:
                edges[index] = start_value + (end_value - start_value) * (index - start) / (end - start)
    result = [float(value if value is not None else low) for value in edges]
    return [max(low, min(high, value)) for value in result]


def _grid_columns(grid: dict[str, Any]) -> int:
    try:
        return max(int(grid.get("num_cols", 0)), max((len(row) for row in grid.get("rows", [])), default=0))
    except (TypeError, ValueError):
        return max((len(row) for row in grid.get("rows", [])), default=0)


def _record_cell_text(records: list[dict[str, Any]], record_index: int, column_index: int) -> str:
    try:
        cells = records[record_index]["cells"]
        return str(cells[column_index]) if column_index < len(cells) else ""
    except (IndexError, KeyError, TypeError):
        return ""


def _candidate_structure(candidate: VisualCandidate) -> dict[str, Any]:
    for evidence in candidate.evidence:
        structure = evidence.get("structure")
        if isinstance(structure, dict):
            return structure
        if "source_struct_row" in evidence or "source_struct_col" in evidence:
            return {
                "source_struct_row": evidence.get("source_struct_row"),
                "source_struct_col": evidence.get("source_struct_col"),
            }
    return {}


def _set_affected_tables(
    candidate: VisualCandidate, tables: Iterable[TableCandidate | None]
) -> None:
    identifiers = sorted({table.candidate_id for table in tables if table is not None})
    for evidence in candidate.evidence:
        evidence.pop("affected_table_candidate_ids", None)
        if identifiers:
            evidence["affected_table_candidate_ids"] = identifiers


def _candidate_affects_table(candidate: VisualCandidate, table_id: str) -> bool:
    if candidate.target.get("table_candidate_id") == table_id:
        return True
    return any(
        table_id in evidence.get("affected_table_candidate_ids", [])
        for evidence in candidate.evidence
    )


def _append_reason(candidate: VisualCandidate, reason: str) -> None:
    if reason not in candidate.reasons:
        candidate.reasons.append(reason)


def _round_diagnostic_value(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, list):
        return [_round_diagnostic_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _round_diagnostic_value(item) for key, item in value.items()}
    return value


def _decode_pdf_string(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    original = getattr(raw, "original_bytes", None)
    if isinstance(original, bytes):
        raw = original
    if isinstance(raw, bytes):
        for encoding in (
            "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "",
            "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "",
            "utf-8",
            "utf-16-be",
            "latin-1",
        ):
            if not encoding:
                continue
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return None
    try:
        return str(raw)
    except Exception:
        return None


def _parent_tree(reader: Any) -> dict[int, dict[int, list[Any]]]:
    root = _resolved_dict(_reader_root(reader))
    struct_root = _resolved_dict(root.get("/StructTreeRoot")) if root else {}
    tree = _resolved_dict(struct_root.get("/ParentTree")) if struct_root else {}
    values: dict[int, dict[int, list[Any]]] = {}

    def walk(node: Any) -> None:
        node = _resolved_dict(node)
        if not node:
            return
        nums = _resolved_list(node.get("/Nums"))
        for index in range(0, len(nums) - 1, 2):
            owner = _int_value(nums[index])
            entries = _resolved_list(nums[index + 1])
            if owner is None:
                continue
            by_mcid: dict[int, list[Any]] = {}
            for mcid, entry in enumerate(entries):
                if _is_null(entry):
                    continue
                by_mcid.setdefault(mcid, []).append(entry)
            values[owner] = by_mcid
        for child in _resolved_list(node.get("/Kids")):
            walk(child)

    walk(tree)
    return values


def _parent_entries(
    parent_tree: dict[int, dict[int, list[Any]]], owner: int | None, mcid: int | None
) -> list[Any]:
    if owner is None or mcid is None:
        return []
    return list(parent_tree.get(owner, {}).get(mcid, []))


def _structure_info(reader: Any) -> dict[str, dict[str, Any]]:
    root = _resolved_dict(_reader_root(reader))
    struct_root = _resolved_dict(root.get("/StructTreeRoot")) if root else {}
    if not struct_root:
        return {}
    table_cells: dict[str, dict[str, Any]] = {}
    for table in _find_struct_nodes(struct_root.get("/K"), "Table"):
        table_key = _object_key(table)
        table_cells.update(_table_cell_contexts(table, table_key))

    info: dict[str, dict[str, Any]] = {}
    visiting: set[str] = set()

    def walk(node: Any, tags: list[str], context: dict[str, Any], inherited_actual: Any = None) -> None:
        resolved = _resolved_dict(node)
        if not resolved or "/S" not in resolved:
            return
        key = _object_key(node)
        if key in visiting:
            return
        visiting.add(key)
        tag = _name_value(resolved.get("/S"))
        current = dict(context)
        if tag == "Table":
            current["table_ref"] = key
        if key in table_cells:
            current.update(table_cells[key])
        actual = resolved.get("/ActualText", inherited_actual)
        entry = {
            "tags": [*tags, tag],
            "table_ref": current.get("table_ref"),
            "source_struct_row": current.get("source_struct_row"),
            "source_struct_col": current.get("source_struct_col"),
            "actual_text": actual,
        }
        info[key] = entry
        for child in _struct_children(resolved.get("/K")):
            walk(child, entry["tags"], current, actual)
        visiting.remove(key)

    for child in _struct_children(struct_root.get("/K")):
        walk(child, [], {})
    return info


def _find_struct_nodes(value: Any, tag: str) -> list[Any]:
    found: list[Any] = []

    def walk(node: Any) -> None:
        resolved = _resolved_dict(node)
        if not resolved or "/S" not in resolved:
            return
        if _name_value(resolved.get("/S")) == tag:
            found.append(node)
        for child in _struct_children(resolved.get("/K")):
            walk(child)

    for child in _struct_children(value):
        walk(child)
    return found


def _table_cell_contexts(table: Any, table_key: str) -> dict[str, dict[str, Any]]:
    rows = _descendants_with_tag(table, "TR", stop_tag="Table")
    contexts: dict[str, dict[str, Any]] = {}
    active: dict[int, int] = {}
    for row_index, row in enumerate(rows):
        cells = _descendants_with_tag(row, "TD", stop_tag="TR") + _descendants_with_tag(row, "TH", stop_tag="TR")
        # Preserve structural child order when the row mixes heading/body cells.
        cells = sorted(cells, key=lambda cell: _struct_order(row, cell))
        column = 0
        next_active = {slot: remaining - 1 for slot, remaining in active.items() if remaining > 1}
        for cell in cells:
            while column in active:
                column += 1
            resolved = _resolved_dict(cell)
            colspan = max(_int_value(resolved.get("/ColSpan")) or 1, 1)
            rowspan = max(_int_value(resolved.get("/RowSpan")) or 1, 1)
            contexts[_object_key(cell)] = {
                "table_ref": table_key,
                "source_struct_row": row_index,
                "source_struct_col": column,
            }
            for slot in range(column, column + colspan):
                if rowspan > 1:
                    next_active[slot] = max(next_active.get(slot, 0), rowspan - 1)
            column += colspan
        active = next_active
    return contexts


def _descendants_with_tag(node: Any, tag: str, *, stop_tag: str) -> list[Any]:
    out: list[Any] = []

    def walk(value: Any, *, root: bool = False) -> None:
        resolved = _resolved_dict(value)
        if not resolved or "/S" not in resolved:
            return
        current = _name_value(resolved.get("/S"))
        if not root and current == stop_tag:
            return
        if current == tag:
            out.append(value)
            return
        for child in _struct_children(resolved.get("/K")):
            walk(child)

    for child in _struct_children(_resolved_dict(node).get("/K")):
        walk(child)
    return out


def _struct_order(parent: Any, child: Any) -> int:
    target = _object_key(child)
    for index, value in enumerate(_struct_children(_resolved_dict(parent).get("/K"))):
        if _object_key(value) == target:
            return index
    return 1_000_000


_IDENTITY_MATRIX = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _walk_content(
    stream: Any,
    *,
    reader: Any,
    resources: dict[str, Any],
    owner: int | None,
    ctm: tuple[float, float, float, float, float, float],
    crop: tuple[float, float, float, float],
    rotation: int,
    groups: list[dict[str, Any]],
    scopes: list[dict[str, Any]] | None = None,
) -> None:
    try:
        from pypdf.generic import ContentStream  # noqa: PLC0415

        operations = ContentStream(stream, reader).operations
    except Exception:
        return
    scopes = scopes if scopes is not None else []
    scope_start = len(scopes)
    q_stack: list[tuple[float, float, float, float, float, float]] = []
    path_points: list[tuple[float, float]] = []
    text_matrix = _IDENTITY_MATRIX
    line_matrix = _IDENTITY_MATRIX
    font_size = 1.0
    leading = 0.0
    text_mode = 0
    for operands, operator in operations:
        if operator == b"q":
            q_stack.append(ctm)
        elif operator == b"Q":
            if q_stack:
                ctm = q_stack.pop()
        elif operator == b"cm" and len(operands) >= 6:
            ctm = _compose(ctm, _matrix(operands[:6]))
        elif operator == b"BDC":
            tag = _name_value(operands[0]) if operands else ""
            props = _marked_properties(operands[1] if len(operands) > 1 else None, resources)
            scopes.append(
                {
                    "owner": owner,
                    "mcid": _int_value(props.get("/MCID")),
                    "tag": tag,
                    "actual_text": props.get("/ActualText"),
                    "actual_seen": "/ActualText" in props,
                    "rects": [],
                    "shown_texts": [],
                    "visible_shown_texts": [],
                    "paint": False,
                    "visible_paint": False,
                    "visible_nontext_paint": False,
                    "paint_unknown": False,
                }
            )
        elif operator == b"BMC":
            scopes.append(
                {
                    "owner": owner,
                    "mcid": None,
                    "tag": _name_value(operands[0]) if operands else "",
                    "actual_text": None,
                    "actual_seen": False,
                    "rects": [],
                    "shown_texts": [],
                    "visible_shown_texts": [],
                    "paint": False,
                    "visible_paint": False,
                    "visible_nontext_paint": False,
                    "paint_unknown": False,
                }
            )
        elif operator == b"EMC":
            if scopes:
                groups.append(scopes.pop())
        elif operator == b"m" or operator == b"l":
            if len(operands) >= 2:
                path_points.append(_apply(ctm, _number(operands[0]), _number(operands[1])))
        elif operator == b"c" and len(operands) >= 6:
            for index in range(0, 6, 2):
                path_points.append(_apply(ctm, _number(operands[index]), _number(operands[index + 1])))
        elif operator in {b"v", b"y"} and len(operands) >= 4:
            for index in range(0, 4, 2):
                path_points.append(_apply(ctm, _number(operands[index]), _number(operands[index + 1])))
        elif operator == b"re" and len(operands) >= 4:
            x, y, width, height = (_number(value) for value in operands[:4])
            path_points.extend(
                _apply(ctm, px, py)
                for px, py in ((x, y), (x + width, y), (x, y + height), (x + width, y + height))
            )
        elif operator in _PAINT_OPERATORS:
            rect = _points_rect(path_points)
            _mark_paint(scopes, rect, crop, rotation, unknown=operator == b"sh" and rect is None)
            path_points = []
        elif operator == b"n":
            path_points = []
        elif operator == b"BT":
            text_matrix = _IDENTITY_MATRIX
            line_matrix = _IDENTITY_MATRIX
        elif operator == b"Tf" and len(operands) >= 2:
            font_size = max(abs(_number(operands[1])), 1.0)
        elif operator == b"Tm" and len(operands) >= 6:
            text_matrix = _matrix(operands[:6])
            line_matrix = text_matrix
        elif operator in {b"Td", b"TD"} and len(operands) >= 2:
            tx, ty = _number(operands[0]), _number(operands[1])
            if operator == b"TD":
                leading = -ty
            line_matrix = _translate(line_matrix, tx, ty)
            text_matrix = line_matrix
        elif operator == b"T*":
            line_matrix = _translate(line_matrix, 0.0, -leading)
            text_matrix = line_matrix
        elif operator == b"TL" and operands:
            leading = _number(operands[0])
        elif operator == b"Tr" and operands:
            text_mode = int(_number(operands[0]))
        elif operator in _TEXT_SHOW_OPERATORS:
            shown = _shown_text(operands, operator)
            for scope in scopes:
                scope["shown_texts"].append(shown)
            if text_mode not in {3, 7}:
                rect = _text_rect(ctm, text_matrix, font_size, shown)
                if rect is not None and _page_rect_to_normalized(rect, crop, rotation) is not None:
                    for scope in scopes:
                        scope["visible_shown_texts"].append(shown)
                _mark_paint(scopes, rect, crop, rotation, text_paint=True)
            text_matrix = _translate(text_matrix, max(font_size * max(len(shown), 1) * 0.5, 0.0), 0.0)
        elif operator == b"Do" and operands:
            name = str(operands[0])
            xobjects = _resolved_dict(resources.get("/XObject"))
            xobject = xobjects.get(name) if xobjects else None
            resolved = _resolved_dict(xobject)
            subtype = _name_value(resolved.get("/Subtype")) if resolved else ""
            if subtype == "Image":
                rect = _points_rect([_apply(ctm, x, y) for x, y in ((0, 0), (1, 0), (0, 1), (1, 1))])
                _mark_paint(scopes, rect, crop, rotation)
            elif subtype == "Form":
                form_owner = _int_value(resolved.get("/StructParents"))
                form_matrix = _matrix(_resolved_list(resolved.get("/Matrix")) or _IDENTITY_MATRIX)
                form_resources = _resolved_dict(resolved.get("/Resources")) or resources
                _walk_content(
                    xobject,
                    reader=reader,
                    resources=form_resources,
                    owner=form_owner if form_owner is not None else owner,
                    ctm=_compose(ctm, form_matrix),
                    crop=crop,
                    rotation=rotation,
                    groups=groups,
                    scopes=scopes,
                )
            else:
                _mark_paint(scopes, None, crop, rotation, unknown=True)
    # Broken streams occasionally omit their terminal EMC. Keep diagnostics.
    groups.extend(scopes[scope_start:])
    del scopes[scope_start:]


def _mark_paint(
    scopes: list[dict[str, Any]],
    rect: tuple[float, float, float, float] | None,
    crop: tuple[float, float, float, float],
    rotation: int,
    *,
    unknown: bool = False,
    text_paint: bool = False,
) -> None:
    for scope in scopes:
        if unknown:
            scope["paint_unknown"] = True
        else:
            scope["paint"] = True
        if rect is not None:
            normalized = _page_rect_to_normalized(rect, crop, rotation)
            if normalized is not None:
                scope["rects"].append(normalized)
                scope["visible_paint"] = True
                if not text_paint:
                    scope["visible_nontext_paint"] = True


def _marked_properties(value: Any, resources: dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, str) and value.startswith("/"):
        properties = _resolved_dict(resources.get("/Properties"))
        return _resolved_dict(properties.get(value)) if properties else {}
    return _resolved_dict(value)


def _text_rect(
    ctm: tuple[float, float, float, float, float, float],
    text_matrix: tuple[float, float, float, float, float, float],
    size: float,
    text: str,
) -> tuple[float, float, float, float] | None:
    width = size * max(len(text), 1) * 0.5
    points = [
        _apply(ctm, *_apply(text_matrix, x, y))
        for x, y in ((0.0, 0.0), (width, 0.0), (0.0, size), (width, size))
    ]
    return _points_rect(points)


def _shown_text(operands: list[Any], operator: bytes) -> str:
    if operator == b"TJ" and operands:
        values = _resolved_list(operands[0])
        return "".join(_decode_pdf_string(value) or "" for value in values if not isinstance(value, (int, float)))
    if operator == b'"' and len(operands) >= 3:
        return _decode_pdf_string(operands[2]) or ""
    if operator == b"'" and operands:
        return _decode_pdf_string(operands[0]) or ""
    return (_decode_pdf_string(operands[0]) or "") if operands else ""


def _paint_state(group: dict[str, Any]) -> str:
    if group.get("visible_paint"):
        return "visible"
    if group.get("paint_unknown"):
        return "unknown"
    return "not_visible"


def _page_crop(page: Any, fallback_size: tuple[float, float]) -> tuple[float, float, float, float]:
    try:
        box = page.cropbox
        return tuple(float(box[index]) for index in range(4))  # type: ignore[return-value]
    except Exception:
        return (0.0, 0.0, float(fallback_size[0]), float(fallback_size[1]))


def _page_rotation(page: Any) -> int:
    value = _int_value(page.get("/Rotate")) or 0
    return value % 360 if value % 90 == 0 else 0


def _page_rect_to_normalized(
    rect: tuple[float, float, float, float], crop: tuple[float, float, float, float], rotation: int
) -> list[float] | None:
    left, bottom, right, top = _ordered_rect(rect)
    points = [
        _page_point_to_normalized(x, y, crop, rotation)
        for x, y in ((left, bottom), (right, bottom), (left, top), (right, top))
    ]
    return _visible_rect(_points_rect(points))


def _page_point_to_normalized(
    x: float, y: float, crop: tuple[float, float, float, float], rotation: int
) -> tuple[float, float]:
    left, bottom, right, top = crop
    width, height = max(right - left, 1e-9), max(top - bottom, 1e-9)
    ux, uy = x - left, top - y
    if rotation == 90:
        dx, dy, displayed_width, displayed_height = height - uy, ux, height, width
    elif rotation == 180:
        dx, dy, displayed_width, displayed_height = width - ux, height - uy, width, height
    elif rotation == 270:
        dx, dy, displayed_width, displayed_height = uy, width - ux, height, width
    else:
        dx, dy, displayed_width, displayed_height = ux, uy, width, height
    return (dx / displayed_width, dy / displayed_height)


def _docling_rect(bbox: Any, page_size: tuple[float, float]) -> list[float] | None:
    if not isinstance(bbox, dict):
        return None
    try:
        left, top, right, bottom = (float(bbox[key]) for key in ("l", "t", "r", "b"))
    except (KeyError, TypeError, ValueError):
        return None
    width, height = page_size
    if width <= 0 or height <= 0:
        return None
    origin = str(bbox.get("origin", "")).replace("_", "").upper()
    if "BOTTOM" in origin:
        top, bottom = height - bottom, height - top
    return _ordered_rect([left / width, top / height, right / width, bottom / height])


def _rect_overlap(first: list[float] | None, second: list[float] | None) -> float:
    if not first or not second:
        return 0.0
    left, top = max(first[0], second[0]), max(first[1], second[1])
    right, bottom = min(first[2], second[2]), min(first[3], second[3])
    overlap = max(right - left, 0.0) * max(bottom - top, 0.0)
    area = min(max((first[2] - first[0]) * (first[3] - first[1]), 0.0), max((second[2] - second[0]) * (second[3] - second[1]), 0.0))
    return overlap / area if area else 0.0


def _rect_iou(first: list[float] | None, second: list[float] | None) -> float:
    if not first or not second:
        return 0.0
    left, top = max(first[0], second[0]), max(first[1], second[1])
    right, bottom = min(first[2], second[2]), min(first[3], second[3])
    overlap = max(right - left, 0.0) * max(bottom - top, 0.0)
    first_area = max((first[2] - first[0]) * (first[3] - first[1]), 0.0)
    second_area = max((second[2] - second[0]) * (second[3] - second[1]), 0.0)
    union = first_area + second_area - overlap
    return overlap / union if union else 0.0


def _rect_inside(rect: list[float] | None, container: list[float] | None) -> bool:
    center = _rect_center(rect)
    return bool(center and container and _point_inside(center, container, tolerance=0.002))


def _point_inside(point: tuple[float, float], rect: list[float], tolerance: float = 0.0) -> bool:
    left, top, right, bottom = _ordered_rect(rect)
    return left - tolerance <= point[0] <= right + tolerance and top - tolerance <= point[1] <= bottom + tolerance


def _rect_center(rect: list[float] | None) -> tuple[float, float] | None:
    if not rect:
        return None
    return ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)


def _rect_union(rects: Iterable[list[float] | tuple[float, float, float, float]]) -> list[float] | None:
    values = [list(rect) for rect in rects if rect is not None]
    if not values:
        return None
    return [
        min(rect[0] for rect in values),
        min(rect[1] for rect in values),
        max(rect[2] for rect in values),
        max(rect[3] for rect in values),
    ]


def _visible_rect(rect: list[float] | tuple[float, float, float, float] | None) -> list[float] | None:
    if rect is None:
        return None
    left, top, right, bottom = _ordered_rect(rect)
    left, top, right, bottom = max(left, 0.0), max(top, 0.0), min(right, 1.0), min(bottom, 1.0)
    return [left, top, right, bottom] if right > left and bottom > top else None


def _ordered_rect(rect: Iterable[float]) -> list[float]:
    left, top, right, bottom = (float(value) for value in rect)
    return [min(left, right), min(top, bottom), max(left, right), max(top, bottom)]


def _points_rect(points: Iterable[tuple[float, float]]) -> tuple[float, float, float, float] | None:
    values = list(points)
    if not values:
        return None
    return (
        min(point[0] for point in values),
        min(point[1] for point in values),
        max(point[0] for point in values),
        max(point[1] for point in values),
    )


def _matrix(values: Iterable[Any]) -> tuple[float, float, float, float, float, float]:
    numbers = list(values)
    if len(numbers) < 6:
        return _IDENTITY_MATRIX
    return tuple(_number(value) for value in numbers[:6])  # type: ignore[return-value]


def _compose(
    outer: tuple[float, float, float, float, float, float],
    inner: tuple[float, float, float, float, float, float],
) -> tuple[float, float, float, float, float, float]:
    a, b, c, d, e, f = outer
    g, h, i, j, k, l = inner
    return (
        a * g + c * h,
        b * g + d * h,
        a * i + c * j,
        b * i + d * j,
        a * k + c * l + e,
        b * k + d * l + f,
    )


def _translate(
    matrix: tuple[float, float, float, float, float, float], tx: float, ty: float
) -> tuple[float, float, float, float, float, float]:
    return _compose(matrix, (1.0, 0.0, 0.0, 1.0, tx, ty))


def _apply(matrix: tuple[float, float, float, float, float, float], x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return (a * x + c * y + e, b * x + d * y + f)


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int_value(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _reader_root(reader: Any) -> Any:
    trailer = getattr(reader, "trailer", {})
    return trailer.get("/Root") if hasattr(trailer, "get") else None


def _resolved_dict(value: Any) -> dict[str, Any]:
    try:
        value = value.get_object()
    except Exception:
        pass
    return value if hasattr(value, "get") else {}


def _resolved_list(value: Any) -> list[Any]:
    try:
        value = value.get_object()
    except Exception:
        pass
    return list(value) if isinstance(value, (list, tuple)) else []


def _struct_children(value: Any) -> list[Any]:
    values = _resolved_list(value)
    return values if values else ([value] if _resolved_dict(value) else [])


def _name_value(value: Any) -> str:
    return str(value or "").lstrip("/")


def _is_null(value: Any) -> bool:
    return type(value).__name__ == "NullObject"


def _object_key(value: Any) -> str:
    try:
        return f"{value.idnum}:{value.generation}"
    except AttributeError:
        resolved = _resolved_dict(value)
        return f"direct:{id(resolved) if resolved else id(value)}"


def _source_ref(owner: int | None, mcid: int | None, entries: list[Any]) -> str:
    suffix = _object_key(entries[0]) if len(entries) == 1 else "unresolved"
    return f"owner:{owner if owner is not None else 'none'};mcid:{mcid if mcid is not None else 'none'};struct:{suffix}"


__all__ = [
    "COLLECTOR_VERSION",
    "normalize_actual_text",
    "comparison_key",
    "collect_tagged_candidates",
    "merge_picture_evidence",
    "associate_table_cells",
    "assess_candidate",
    "update_visual_completeness",
    "reconcile_picture_evidence",
    "visual_audit_summary",
    "visual_candidate_rows",
    "apply_trusted_visual_values",
]
