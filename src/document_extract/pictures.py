"""Picture triage, crop expansion, and typed VLM extraction."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from .docling_adapter import (
    bbox_dict,
    caption_text,
    is_picture_item,
    is_table_item,
)
from .docling_adapter import item_kind
from .layout.geometry import (
    bbox_area_ratio,
    bbox_to_normalized_rect,
    normalized_rect_to_pixel_rect,
    rect_area,
    rect_overlap_ratio,
)
from .llm import ollama as ollama_client
from .markdown import postprocess as sp
from .markdown.formatting import normalize_pipe_tables, pipe_row_count
from .models import PictureRecord
from .prompts import (
    DEFAULT_PICTURE_CHART_PROMPT,
    DEFAULT_PICTURE_GROUPED_VALUES_PROMPT,
    DEFAULT_PICTURE_PHOTO_PROMPT,
    DEFAULT_PICTURE_SYMBOL_PROMPT,
    DEFAULT_PICTURE_TABLE_PROMPT,
    DEFAULT_PICTURE_TRIAGE_PROMPT,
    DEFAULT_PICTURE_VALUES_PROMPT,
)

TRIAGE_TYPES = {
    "photo",
    "table",
    "chart",
    "kpi",
    "infographic",
    "map",
    "diagram",
    "decorative",
    "unclear",
    "symbol",
}
DECORATIVE_LABELS = {"icon", "logo", "decorative", "stamp", "background"}
SUMMARY_LABELS = {"chart", "diagram", "figure", "graph", "map", "table", "infographic"}
PICTURE_MIN_AREA_RATIO = 0.01
PICTURE_EMBED_OVERLAP_MIN = 0.5
PICTURE_SYMBOL_MAX_CHARS = 40
PICTURE_DECORATIVE_MAX_AREA_RATIO = 0.05
PICTURE_TABLE_MIN_AREA_RATIO = 0.08
SUMMARY_TYPES = {"photo", "table", "chart", "kpi", "infographic", "map", "diagram"}
# KPI panels are content-bearing images, but their correct markdown shape is a
# label/value list rather than a pipe table.
SUMMARY_TABLE_TYPES = {"table"}
SUMMARY_TYPE_RE = re.compile(r"^\s*TYPE:\s*([a-z]+)\s*$", re.IGNORECASE)
_SYMBOL_CODE_SEQUENCE_RE = re.compile(r"^([EGS]\d{1,2})(\s+[EGS]\d{1,2})+$")
PICTURE_CROP_NEIGHBOR_GAP = 0.03
PICTURE_CROP_MARGIN = 0.02
PICTURE_SYMBOL_CROP_MARGIN = 0.005

def classification_text(item: Any) -> str:
    parts: list[str] = []
    for attr in ("classification", "picture_class", "meta"):
        value = getattr(item, attr, None)
        if value is None:
            continue
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            parts.extend(str(v) for v in value.values() if v is not None)
        elif isinstance(value, (list, tuple)):
            for entry in value:
                parts.append(str(getattr(entry, "label", entry)))
        else:
            parts.append(str(value))
    return " ".join(parts).lower()


def should_summarize_picture(record: PictureRecord) -> tuple[bool, str]:
    haystack = f"{record.classification} {record.caption}".lower()
    if record.embedded_in == "picture":
        return False, "covered_by_parent"
    if record.embedded_in == "table":
        return True, "table_embedded"
    if record.area_ratio < PICTURE_MIN_AREA_RATIO:
        return False, "too_small"
    if any(label in haystack for label in DECORATIVE_LABELS) and record.area_ratio < PICTURE_DECORATIVE_MAX_AREA_RATIO:
        return False, "decorative"
    if record.area_ratio >= PICTURE_DECORATIVE_MAX_AREA_RATIO:
        return True, "large_picture"
    if any(label in haystack for label in SUMMARY_LABELS):
        return True, "semantic_label"
    return False, "not_semantic"


def should_visual_triage_picture(record: PictureRecord) -> tuple[bool, str]:
    """Apply the cheap prefilter before spending a VLM call on triage.

    The old heuristic skipped small unlabeled images.  Visual triage widens
    that middle band so a chart without useful Docling metadata is not lost,
    while still avoiding tiny page furniture and icons.
    """

    if record.embedded_in == "picture":
        return False, "covered_by_parent"
    if record.embedded_in == "table":
        return True, "table_embedded"
    if not record.abs_path:
        return False, "missing_image"
    if record.area_ratio < PICTURE_MIN_AREA_RATIO:
        return False, "too_small"
    haystack = f"{record.classification} {record.caption}".lower()
    if any(label in haystack for label in DECORATIVE_LABELS) and record.area_ratio < PICTURE_DECORATIVE_MAX_AREA_RATIO:
        return False, "decorative"
    return True, "visual_candidate"


def parse_picture_triage(answer: str) -> tuple[str, float, list[str]]:
    """Parse and validate the tiny JSON contract returned by the triage VLM."""

    cleaned = sp.strip_meta_commentary(ollama_client.strip_markdown_fences(answer)).strip()
    warnings: list[str] = []
    payload: Any = None
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                payload = json.loads(match.group(0))
            except json.JSONDecodeError:
                payload = None
        if payload is None:
            warnings.append("triage_invalid_json")

    if not isinstance(payload, dict):
        return "unclear", 0.0, warnings or ["triage_invalid_payload"]
    kind = str(payload.get("type") or "unclear").strip().lower()
    if kind not in TRIAGE_TYPES:
        warnings.append("triage_unknown_type")
        kind = "unclear"
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
        warnings.append("triage_invalid_confidence")
    confidence = max(0.0, min(1.0, confidence))
    return kind, confidence, warnings


def picture_vlm_image_path(
    record: PictureRecord,
    *,
    page_image_path: Path | None,
    page_size: tuple[float, float] | None,
    cells: list[dict[str, Any]] | None,
) -> Path | None:
    if not record.abs_path:
        return None
    image_path = Path(record.abs_path)
    if page_image_path is None or page_size is None:
        return image_path
    if record.triage_type == "symbol" or (
        record.embedded_in == "table" and record.area_ratio < PICTURE_MIN_AREA_RATIO
    ):
        return picture_symbol_image_path(
            record,
            page_image_path=page_image_path,
            page_size=page_size,
        )
    rect = picture_summary_rect(
        bbox_to_normalized_rect(record.bbox, page_size), cells or []
    )
    crop_path = image_path.with_name(image_path.stem + "_vlm.png")
    if rect and save_region_crop(
        page_image_path=page_image_path,
        bbox=rect,
        crop_path=crop_path,
        margin=0.0,
    ):
        return crop_path
    return image_path


def picture_symbol_image_path(
    record: PictureRecord,
    *,
    page_image_path: Path,
    page_size: tuple[float, float],
) -> Path | None:
    """Crop only an embedded symbol and enlarge tiny badge text for the VLM."""

    if not record.abs_path:
        return None
    rect = bbox_to_normalized_rect(record.bbox, page_size)
    crop_path = Path(record.abs_path).with_name(
        Path(record.abs_path).stem + "_symbol_vlm.png"
    )
    if not save_region_crop(
        page_image_path=page_image_path,
        bbox=rect,
        crop_path=crop_path,
        margin=PICTURE_SYMBOL_CROP_MARGIN,
    ):
        return Path(record.abs_path)

    from PIL import Image  # noqa: PLC0415

    with Image.open(crop_path) as image:
        if min(image.size) < 64:
            enlarged = image.resize(
                (max(1, image.width * 3), max(1, image.height * 3)),
                Image.Resampling.LANCZOS,
            )
            enlarged.save(crop_path)
    return crop_path


def picture_specialist_prompt(record: PictureRecord, generic_prompt: str) -> str:
    """Select a type-specific extraction contract after visual triage."""

    prompt_by_type = {
        "table": DEFAULT_PICTURE_TABLE_PROMPT,
        "kpi": DEFAULT_PICTURE_GROUPED_VALUES_PROMPT,
        "chart": DEFAULT_PICTURE_CHART_PROMPT,
        "infographic": DEFAULT_PICTURE_GROUPED_VALUES_PROMPT,
        "map": DEFAULT_PICTURE_GROUPED_VALUES_PROMPT,
        "diagram": DEFAULT_PICTURE_GROUPED_VALUES_PROMPT,
        "photo": DEFAULT_PICTURE_PHOTO_PROMPT,
    }
    body = prompt_by_type.get(record.triage_type, generic_prompt)
    if body == generic_prompt:
        return body
    return f"The image type is {record.triage_type}.\nYour first line must be exactly `TYPE: {record.triage_type}`.\n\n{body}"


def triage_pictures(
    *,
    records: list[PictureRecord],
    args: argparse.Namespace,
    page_image_path: Path | None = None,
    page_size: tuple[float, float] | None = None,
    cells: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Classify candidate pictures and decide which specialist to run."""

    stats: dict[str, Any] = {
        "candidates": 0,
        "calls": 0,
        "retries": 0,
        "skipped": 0,
        "types": {},
    }
    # (record, crop path) pairs whose classification needs a VLM call. Crops
    # are built in this serial phase; workers only perform the HTTP call.
    pending: list[tuple[PictureRecord, Path]] = []
    for record in records:
        # Tiny icons embedded in a table cell skip VLM triage — they are "symbols" and treated like such
        if record.embedded_in == "table" and record.area_ratio < PICTURE_MIN_AREA_RATIO:
            record.triage_eligible = True
            record.triage_type = "symbol"
            record.triage_confidence = 1.0
            record.triage_warnings = []
            record.triage_usage = None
            stats["candidates"] += 1
            stats["types"]["symbol"] = stats["types"].get("symbol", 0) + 1
            if args.skip_vlm: # for testing without vlm
                record.summarize = False
                record.triage_action = "skip"
                record.skip_reason = "skip_vlm"
                stats["skipped"] += 1
            else:
                record.summarize = True
                record.triage_action = "symbol"
                record.skip_reason = ""
            continue

        # Cheap pre-filter: drop images that are too small, decorative, or
        # missing a file on disk before spending any VLM budget.
        eligible, reason = should_visual_triage_picture(record)
        record.triage_eligible = eligible
        if not eligible:
            record.summarize = False
            record.skip_reason = reason
            record.triage_action = "skip"
            stats["skipped"] += 1
            continue

        stats["candidates"] += 1
        legacy_summarize, legacy_reason = should_summarize_picture(record)

        # --skip-picture-triage: bypass the VLM entirely and use the old
        # metadata-only heuristic to decide whether to summarize.
        if args.skip_picture_triage:
            record.summarize = legacy_summarize
            record.skip_reason = legacy_reason
            record.triage_type = ""
            record.triage_action = "extract" if legacy_summarize else "skip"
            if not legacy_summarize:
                stats["skipped"] += 1
            continue

        # --skip-vlm: no model calls at all this run.
        if args.skip_vlm:
            record.summarize = False
            record.triage_action = "skip"
            record.skip_reason = "skip_vlm"
            stats["skipped"] += 1
            continue

        # Build the best crop available for the triage VLM (expanded to include
        # adjacent text cells so the model sees axis labels, titles, etc.).
        image_path = picture_vlm_image_path(
            record,
            page_image_path=page_image_path,
            page_size=page_size,
            cells=cells,
        )
        if image_path is None:
            record.summarize = False
            record.triage_action = "skip"
            record.skip_reason = "missing_image"
            stats["skipped"] += 1
            continue

        # Single cheap VLM call that classifies the image and returns a
        # confidence score — this drives every downstream routing decision.
        pending.append((record, image_path))

    def call_triage(task: tuple[PictureRecord, Path]) -> tuple[str, dict[str, Any]]:
        _, image_path = task
        return ollama_client.call_ollama_vlm(
            base_url=args.ollama_base_url,
            model=args.triage_model,
            prompt=DEFAULT_PICTURE_TRIAGE_PROMPT,
            image_path=image_path,
            temperature=0.0,
            num_ctx=args.num_ctx,
            num_predict=args.triage_num_predict,
            auto_num_ctx=args.auto_num_ctx,
        )

    results = ollama_client.map_vlm_tasks(
        call_triage, pending, getattr(args, "vlm_concurrency", 1)
    )

    # Parsing, stats, and routing stay serial and in input order so the
    # serial and concurrent paths produce identical records and stats.
    for (record, _image_path), (answer, usage) in zip(pending, results):
        stats["calls"] += 1
        if usage.get("retried"):
            stats["retries"] += 1
        kind, confidence, warnings = parse_picture_triage(answer)
        record.triage_type = kind
        record.triage_confidence = confidence
        record.triage_usage = usage
        record.triage_warnings = warnings
        stats["types"][kind] = stats["types"].get(kind, 0) + 1

        # Route on the triage result: decorative and low-value photos are
        # dropped; everything else proceeds to extraction with either a
        # type-specific specialist prompt (high confidence) or the generic one.
        if kind == "decorative" and confidence >= args.triage_confidence:
            record.summarize = False
            record.triage_action = "skip"
            record.skip_reason = "triage_decorative"
            stats["skipped"] += 1
        elif (
            kind == "photo"
            and not args.photo_summaries
            and confidence >= args.photo_skip_confidence
        ):
            record.summarize = False
            record.triage_action = "skip"
            record.skip_reason = "triage_photo"
            stats["skipped"] += 1
        else:
            record.summarize = True
            record.triage_action = "specialist" if confidence >= args.triage_confidence else "generic"
            record.skip_reason = ""
    return stats


def save_picture_records(
    *,
    document: Any,
    items: list[Any],
    page_number: int,
    page_dir: Path,
    page_size: tuple[float, float],
) -> dict[int, PictureRecord]:
    images_dir = page_dir / "images"
    records: dict[int, PictureRecord] = {}
    table_rects = [
        bbox_to_normalized_rect(bbox_dict(item), page_size)
        for item in items
        if is_table_item(item)
    ]
    picture_rects = [
        (id(item), bbox_to_normalized_rect(bbox_dict(item), page_size))
        for item in items
        if is_picture_item(item)
    ]
    picture_index = 0
    for item in items:
        if not is_picture_item(item):
            continue
        picture_index += 1
        placeholder = f"{{{{DOC_IMAGE_p{page_number:04d}_i{picture_index:03d}}}}}"
        rel_path = f"images/picture_p{page_number:04d}_i{picture_index:03d}.png"
        abs_path: Path | None = None
        image = get_picture_image(item, document)
        if image is not None:
            images_dir.mkdir(parents=True, exist_ok=True)
            abs_path = page_dir / rel_path
            image.save(abs_path)
        bbox = bbox_dict(item)
        record = PictureRecord(
            page=page_number,
            index=picture_index,
            placeholder=placeholder,
            rel_path=rel_path,
            abs_path=str(abs_path) if abs_path else None,
            bbox=bbox,
            area_ratio=bbox_area_ratio(bbox, page_size),
            classification=classification_text(item),
            caption=caption_text(item),
        )
        picture_rect = bbox_to_normalized_rect(bbox, page_size)
        record.norm_rect = list(picture_rect) if picture_rect else None
        table_overlaps = [
            rect_overlap_ratio(picture_rect, table_rect)
            for table_rect in table_rects
            if table_rect
        ]
        table_overlap = max(table_overlaps, default=0.0)
        if table_overlap >= PICTURE_EMBED_OVERLAP_MIN:
            record.embedded_in = "table"
            record.embed_overlap_ratio = round(table_overlap, 3)
        else:
            parent_overlaps = [
                rect_overlap_ratio(picture_rect, other_rect)
                for other_id, other_rect in picture_rects
                if other_id != id(item)
                and other_rect
                and rect_area(other_rect) > rect_area(picture_rect)
                and rect_area(other_rect) >= PICTURE_MIN_AREA_RATIO
            ]
            parent_overlap = max(parent_overlaps, default=0.0)
            if parent_overlap >= PICTURE_EMBED_OVERLAP_MIN:
                record.embedded_in = "picture"
                record.embed_overlap_ratio = round(parent_overlap, 3)
        record.summarize, record.skip_reason = should_summarize_picture(record)
        record.triage_eligible, _ = should_visual_triage_picture(record)
        records[id(item)] = record
    return records


def get_picture_image(item: Any, document: Any) -> Any | None:
    if hasattr(item, "get_image"):
        for kwargs in ({"doc": document}, {"document": document}, {}):
            try:
                return item.get_image(**kwargs)
            except TypeError:
                continue
            except Exception:
                return None
    image_ref = getattr(item, "image", None)
    for attr in ("pil_image", "image"):
        value = getattr(image_ref, attr, None)
        if value is not None:
            return value
    return None


def parse_typed_summary(answer: str) -> tuple[str, str]:
    """Split a typed summary into (type, body); type is "" when missing."""
    lines = answer.strip().splitlines()
    if lines:
        match = SUMMARY_TYPE_RE.match(lines[0])
        if match and match.group(1).lower() in SUMMARY_TYPES:
            return match.group(1).lower(), "\n".join(lines[1:]).strip()
    return "", answer.strip()


def normalize_symbol_summary(summary: str) -> str:
    """Use the table-cell comma convention for multi-code symbol values."""
    value = summary.strip()
    if _SYMBOL_CODE_SEQUENCE_RE.fullmatch(value):
        return ", ".join(value.split())
    return value


def summary_shape_ok(summary_type: str, body: str) -> bool:
    """Deterministic per-type shape check for an image transcription."""
    if not body:
        return False
    if summary_type == "symbol":
        return "\n" not in body and len(body.strip()) <= PICTURE_SYMBOL_MAX_CHARS
    if summary_type == "photo":
        return len(body) <= 400
    if summary_type in SUMMARY_TABLE_TYPES:
        return pipe_row_count(body) >= 2
    if summary_type in SUMMARY_TYPES:
        value_lines = sum(
            1
            for line in body.splitlines()
            if ":" in line or line.strip().startswith("|")
        )
        return value_lines >= 2 or pipe_row_count(body) >= 2
    return True  # unknown type: accept anything non-empty


def picture_summary_rect(
    rect: list[float] | None,
    cells: list[dict[str, Any]],
) -> list[float] | None:
    """Crop rect for summarizing a picture: its bbox grown to swallow adjacent
    text lines (title above, total line below, axis labels beside) plus a
    margin for text that lives only in pixels right at the bbox edge.
    """
    if not rect:
        return None
    grown = list(rect)
    for cell in cells:
        other = cell["rect"]
        x_overlap = min(grown[2], other[2]) - max(grown[0], other[0])
        y_overlap = min(grown[3], other[3]) - max(grown[1], other[1])
        x_gap = max(grown[0], other[0]) - min(grown[2], other[2])
        y_gap = max(grown[1], other[1]) - min(grown[3], other[3])
        vertical_neighbor = (
            y_gap <= PICTURE_CROP_NEIGHBOR_GAP
            and x_overlap >= 0.5 * (other[2] - other[0])
        )
        horizontal_neighbor = (
            x_gap <= PICTURE_CROP_NEIGHBOR_GAP
            and y_overlap >= 0.5 * (other[3] - other[1])
        )
        if vertical_neighbor or horizontal_neighbor:
            grown = [
                min(grown[0], other[0]),
                min(grown[1], other[1]),
                max(grown[2], other[2]),
                max(grown[3], other[3]),
            ]
    return [
        max(0.0, grown[0] - PICTURE_CROP_MARGIN),
        max(0.0, grown[1] - PICTURE_CROP_MARGIN),
        min(1.0, grown[2] + PICTURE_CROP_MARGIN),
        min(1.0, grown[3] + PICTURE_CROP_MARGIN),
    ]


def summarize_pictures(
    *,
    records: list[PictureRecord],
    prompt_template: str,
    args: argparse.Namespace,
    page_image_path: Path | None = None,
    page_size: tuple[float, float] | None = None,
    cells: list[dict[str, Any]] | None = None,
) -> None:
    if args.skip_vlm:
        return

    # Each worker owns exactly one record: it mutates only that record and
    # writes only that record's crop files, so concurrent extraction is safe.
    # Callers derive call/retry counts from record.usage afterwards, so there
    # is no shared accumulator here.
    def extract_one(record: PictureRecord) -> None:
        image_path = picture_vlm_image_path(
            record,
            page_image_path=page_image_path,
            page_size=page_size,
            cells=cells,
        )
        if image_path is None:
            record.summary_warnings.append("missing_image")
            return
        context = {
            "page": record.page,
            "picture": record.index,
            "caption": record.caption,
            "classification": record.classification,
        }
        is_symbol = record.triage_type == "symbol"
        prompt = picture_specialist_prompt(
            record,
            DEFAULT_PICTURE_SYMBOL_PROMPT
            if is_symbol
            else prompt_template.format(context=json.dumps(context, ensure_ascii=False)),
        )
        answer, usage = ollama_client.call_ollama_vlm(
            base_url=args.ollama_base_url,
            model=args.ollama_model,
            prompt=prompt,
            image_path=image_path,
            temperature=args.temperature,
            num_ctx=args.num_ctx,
            num_predict=args.num_predict,
            auto_num_ctx=args.auto_num_ctx,
        )
        record.usage = usage
        cleaned_answer = sp.strip_meta_commentary(
            ollama_client.strip_markdown_fences(answer)
        ).strip()
        summary_type, body = parse_typed_summary(cleaned_answer)
        if is_symbol:
            summary_type = "symbol"
            body_lines = body.splitlines()
            if body_lines and re.match(
                r"^\s*TYPE:\s*symbol\s*$", body_lines[0], re.IGNORECASE
            ):
                body = "\n".join(body.splitlines()[1:]).strip()
        record.summary_type = summary_type
        if not summary_shape_ok(summary_type, body) and (
            summary_type in SUMMARY_TYPES or is_symbol
        ):
            # One focused retry with the type-specific contract.
            retry_prompt = (
                DEFAULT_PICTURE_SYMBOL_PROMPT
                if is_symbol
                else
                DEFAULT_PICTURE_TABLE_PROMPT
                if summary_type in SUMMARY_TABLE_TYPES
                else DEFAULT_PICTURE_VALUES_PROMPT
            )
            retry_answer, retry_usage = ollama_client.call_ollama_vlm(
                base_url=args.ollama_base_url,
                model=args.ollama_model,
                prompt=retry_prompt,
                image_path=image_path,
                temperature=args.temperature,
                num_ctx=args.num_ctx,
                num_predict=args.num_predict,
                auto_num_ctx=args.auto_num_ctx,
            )
            combined_usage = dict(retry_usage)
            for token_key in ("prompt_tokens", "output_tokens", "total_tokens"):
                combined_usage[token_key] = usage.get(token_key, 0) + retry_usage.get(token_key, 0)
            combined_usage["retried"] = True
            record.usage = combined_usage
            retry_body = sp.strip_meta_commentary(
                ollama_client.strip_markdown_fences(retry_answer)
            ).strip()
            if summary_shape_ok(summary_type, retry_body):
                body = retry_body
            elif is_symbol:
                body = ""
                record.summarize = False
                record.triage_action = "skip"
                record.skip_reason = "symbol_shape_failed"
                record.summary_warnings.append("symbol_shape_failed")
            else:
                body = retry_body if len(retry_body) > len(body) else body
                record.summary_warnings.append("summary_shape_failed")
        if summary_type in SUMMARY_TABLE_TYPES:
            body = normalize_pipe_tables(body)
        record.summary = (
            normalize_symbol_summary(body)
            if summary_type == "symbol"
            else body.strip()
        )

    ollama_client.map_vlm_tasks(
        extract_one,
        [record for record in records if record.summarize and record.abs_path],
        getattr(args, "vlm_concurrency", 1),
    )


def save_region_crop(
    *,
    page_image_path: Path,
    bbox: list[float] | None,
    crop_path: Path,
    margin: float = 0.02,
) -> bool:
    from PIL import Image  # noqa: PLC0415

    if not bbox:
        return False
    image = Image.open(page_image_path)
    padded = [
        max(0.0, bbox[0] - margin),
        max(0.0, bbox[1] - margin),
        min(1.0, bbox[2] + margin),
        min(1.0, bbox[3] + margin),
    ]
    rect = normalized_rect_to_pixel_rect(padded, image.size)
    if not rect:
        return False
    crop_path.parent.mkdir(parents=True, exist_ok=True)
    image.crop(rect).save(crop_path)
    return True

__all__ = [
    "DECORATIVE_LABELS", "SUMMARY_LABELS", "PICTURE_MIN_AREA_RATIO",
    "PICTURE_EMBED_OVERLAP_MIN", "PICTURE_SYMBOL_MAX_CHARS",
    "PICTURE_DECORATIVE_MAX_AREA_RATIO", "PICTURE_TABLE_MIN_AREA_RATIO",
    "classification_text", "should_summarize_picture",
    "should_visual_triage_picture", "parse_picture_triage",
    "picture_vlm_image_path", "picture_specialist_prompt", "triage_pictures",
    "save_picture_records", "get_picture_image", "parse_typed_summary",
    "normalize_symbol_summary",
    "summary_shape_ok", "picture_summary_rect", "summarize_pictures",
    "save_region_crop",
]
