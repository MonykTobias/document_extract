"""Sidecar, manifest, token-usage, and combined Markdown artifacts."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .docling_adapter import bbox_dict, caption_text, is_heading_item, item_kind, item_text
from .markdown.formatting import heading_level
from .models import PictureRecord

def block_rows_for_page(
    items: list[Any],
    page_number: int,
    picture_records: dict[int, PictureRecord],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    heading_path: list[str] = []
    for index, item in enumerate(items, start=1):
        text = item_text(item)
        if is_heading_item(item) and text:
            level = heading_level(item)
            heading_path = heading_path[: max(0, level - 1)]
            heading_path.append(text)
        record = picture_records.get(id(item))
        rows.append(
            {
                "block_id": f"p{page_number:04d}-b{index:04d}",
                "doc_ref": str(getattr(item, "self_ref", "")),
                "page": page_number,
                "bbox": bbox_dict(item),
                "type": item_kind(item),
                "heading_path": heading_path.copy(),
                "text": text[:500],
                "image_path": record.rel_path if record else None,
                "caption": record.caption if record else caption_text(item),
            }
        )
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def write_image_summaries(path: Path, records: list[PictureRecord]) -> None:
    rows = [asdict(record) for record in records]
    write_jsonl(path, rows)


def summarize_token_usage(manifest: list[dict[str, Any]]) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    for page in manifest:
        page_usage = page.get("page_vlm_usage")
        if page_usage:
            calls.append(
                {
                    "page": page["page"],
                    "stage": "page_vlm",
                    **page_usage,
                }
            )
        repair_usage = page.get("page_repair_usage")
        if repair_usage:
            calls.append(
                {
                    "page": page["page"],
                    "stage": "page_repair",
                    **repair_usage,
                }
            )
        for record in page.get("pictures", []):
            triage_usage = record.get("triage_usage")
            if triage_usage:
                calls.append(
                    {
                        "page": page["page"],
                        "stage": "picture_triage",
                        "picture": record["index"],
                        **triage_usage,
                    }
                )
            usage = record.get("usage")
            if not usage:
                continue
            calls.append(
                {
                    "page": page["page"],
                    "stage": "image_summary",
                    "picture": record["index"],
                    **usage,
                }
            )
        for candidate in page.get("table_candidates", []):
            usage = candidate.get("usage")
            if not usage:
                continue
            calls.append(
                {
                    "page": page["page"],
                    "stage": "table_extraction",
                    "candidate_id": candidate["candidate_id"],
                    "kind": candidate["kind"],
                    **usage,
                }
            )
    totals = {
        "prompt_tokens": sum(int(call.get("prompt_tokens", 0)) for call in calls),
        "output_tokens": sum(int(call.get("output_tokens", 0)) for call in calls),
        "total_tokens": sum(int(call.get("total_tokens", 0)) for call in calls),
    }
    return {
        "note": "Ollama token counts come from prompt_eval_count/eval_count when available.",
        "totals": totals,
        "pages": len({call["page"] for call in calls}),
        "verify_calls": 0,
        "calls": calls,
    }


def combine_markdown(page_dirs: list[Path], combined_path: Path, leaf_name: str) -> None:
    parts: list[str] = []
    for page_dir in page_dirs:
        page_md = page_dir / leaf_name
        if not page_md.exists():
            continue
        parts.append(f"\n\n===== {page_dir.name} =====\n\n")
        parts.append(page_md.read_text(encoding="utf-8"))
    combined_path.write_text("".join(parts).lstrip(), encoding="utf-8")

__all__ = [
    "block_rows_for_page", "write_jsonl", "write_image_summaries",
    "summarize_token_usage", "combine_markdown",
]
