from __future__ import annotations

import argparse
import inspect
import json
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .llm import ollama as ollama_client
from .runtime import (
    REPLAY_STAGES,
    STAGES,
    PageState,
    StageRecord,
    StatusReporter,
    clear_downstream_artifacts,
    invalidate_from,
    new_page_state,
    relative_page_path,
    resolve_page_path,
    stage_index,
    validate_checkpoint,
)
from .markdown import postprocess as sp
from .prompts import load_prompt, require_placeholders


DEFAULT_IMAGE_SUMMARY_PROMPT = """You are transcribing ONE image cropped from a report page.

The first line of your answer must be exactly `TYPE: <kind>` where <kind> is one of:
photo, table, chart, kpi, infographic, map, diagram

Then, depending on the kind:
- photo: one concise factual sentence describing the scene. Nothing else.
- table or kpi: transcribe ALL visible text and numbers as one or more markdown pipe tables that mirror the visible rows, columns and groups. Repeat a group label on every row it spans. Include any visible title line and total/summary lines as regular rows.
- chart: transcribe every visible axis label, series name, data label and printed value as `Label: value` lines or a small markdown table.
- infographic, map or diagram: transcribe every visible label with its printed value as `- Label: value` bullets, grouped under the visible group headings.

Rules:
- Use only text and numbers visible in the image. Do not calculate, infer, or complete missing values.
- Do not describe colors, layout, or design.
- No commentary about the task.

Context:
{context}
"""

DEFAULT_PICTURE_TABLE_PROMPT = """Transcribe the table-like content in this image as markdown pipe tables only.

Rules:
- Mirror the visible rows, columns and groups; repeat a group label on every row it spans.
- Include any visible title line and total/summary lines as regular rows.
- Use only visible text and numbers. Do not calculate, infer, or complete missing values.
- Leave a cell blank when it is empty or unreadable.
- Output only markdown tables. No commentary.
"""

DEFAULT_PICTURE_VALUES_PROMPT = """List every visible label in this image together with its printed value.

Rules:
- Output `- Label: value` bullets, grouped under the visible group headings.
- Use only visible text and numbers. Do not calculate, infer, or complete missing values.
- No commentary, no design description.
"""

DEFAULT_PICTURE_TRIAGE_PROMPT = """Classify the provided report image for downstream extraction.

Return JSON only, with exactly these fields:
{"type":"photo|table|chart|kpi|infographic|map|diagram|decorative|unclear","confidence":0.0}

Use the visible image, not the metadata. Do not transcribe text. Use a confidence
between 0 and 1. Mark decorative only when the image has no meaningful report
content.
"""

DEFAULT_PICTURE_CHART_PROMPT = """Transcribe the visible chart as compact Markdown.

Include every visible title, axis label, series name, legend entry, data label,
and printed value. Use `Label: value` lines or a small Markdown table. Use only
visible text and numbers. Do not calculate, infer, or describe design.
"""

DEFAULT_PICTURE_GROUPED_VALUES_PROMPT = """Transcribe every visible label and printed value in this infographic, map, or diagram.

Use Markdown bullets grouped under visible headings. Use only visible text and
numbers. Do not calculate, infer, or describe design.
"""

DEFAULT_PICTURE_PHOTO_PROMPT = """Describe this report image in one concise factual sentence.

Mention only clearly visible people, objects, or scene. Do not describe colors,
layout, branding, or design. Do not add commentary.
"""

DEFAULT_PAGE_REFINEMENT_PROMPT = """You are refining a markdown reconstruction of ONE visually complex report page. Your output is markdown.

Inputs:
- the full page image
- a draft markdown reconstruction from Docling
- a compact JSON block map with id, type, normalized bbox, and selected text/captions
- optional pre-verified markdown tables, already transcribed from table-like regions of this page and checked against the extracted text

Your job:
- preserve all real content from the draft
- fix reading order and label-to-value associations against the image
- turn visually structured content into useful markdown
- never invent text or numbers

Rules:
- Keep all meaningful text from the draft unless it is clearly a page number, running header/footer, or decorative junk.
- If numbers, dates, percentages, captions, labels, or short callouts are visually associated with a chart, timeline, infographic, or grouped panel, place them with the correct subject in the body.
- For timelines, keep each milestone grouped together. Do not scatter years and descriptions into unrelated sections.
- Each pre-verified table is authoritative: place it verbatim at the correct position in the page flow, and do not repeat its cell contents again as separate lists, paragraphs, or headings.
- For charts and KPI panels, output explicit `Label: value` lines or a small markdown table when the mapping is visually clear.
- For prose, keep paragraphs.
- For lists, use `- ` bullets.
- Keep heading structure reasonable. Do not turn every isolated number into its own heading.
- Do not calculate, derive, estimate, or infer missing values.
- If something is visible but its association is genuinely unclear, keep the content and place it under `## Uncertain mappings` instead of dropping it.
- Keep every `<!-- image -->` marker from the draft at the position where its image belongs in the reading order. Never delete or invent these markers.
- Do not add any other image links or HTML tags.
- Do not write meta-commentary about the draft or the image.

Draft markdown:
{source_markdown}

Compact layout block map:
{layout_blocks}

Pre-verified tables:
{verified_tables}
"""

DEFAULT_PAGE_REPAIR_PROMPT = """You are repairing a markdown reconstruction of ONE visually complex report page. Your output is markdown.

Inputs:
- the full page image
- the current markdown reconstruction
- a compact JSON block map with id, type, normalized bbox, and selected text/captions
- optional pre-verified markdown tables, already transcribed from table-like regions of this page and checked against the extracted text
- optional unplaced lines that were preserved because the first pass could not place them confidently

Your job:
- keep the current markdown structure where it is already correct
- repair table structure, timeline/group associations, KPI-panel associations, and placement of unplaced content
- do not invent any text or numbers

Rules:
- If the page contains a real table, output it as a proper markdown table when row/column associations are visually clear.
- Each pre-verified table is authoritative: if it is missing from the current markdown, insert it verbatim at the correct position; do not repeat its cell contents as separate lists or paragraphs.
- If the page contains grouped panels, KPI blocks, or category/value summaries, ensure each value is attached to the correct label.
- If the page contains a timeline, keep each year/date with its corresponding milestone text.
- For each unplaced line: either place it in the body where it belongs, or leave it under `## Unplaced content` if the correct position is still unclear.
- Never drop an unplaced line silently.
- Keep headings and prose that are already correct.
- Do not calculate, derive, estimate, or infer missing values.
- Keep every image reference (`![...](...)`) and its following `**Image summary:**` block at its position. Do not add any other image links or HTML tags.
- Do not write meta-commentary.

Current markdown:
{current_markdown}

Compact layout block map:
{layout_blocks}

Pre-verified tables:
{verified_tables}

Unplaced lines:
{unplaced_lines}
"""

DEFAULT_TABLE_REGION_PROMPT = """You are transcribing ONE table-like region cropped from a report page into markdown tables.

Inputs:
- the cropped region image
- a JSON list of text blocks extracted from this region, each with id, normalized bbox, and text — these texts are authoritative

Your job: arrange the given block texts into one or more markdown tables that match the visible row/column structure.

Rules:
- Use the block texts as cell contents. Do not paraphrase them.
- Do not invent, calculate, or infer any text or numbers that are not in the blocks.
- If a label visually spans several rows, repeat it in each of those rows.
- Use the topmost row of column labels as the table header when one is visible.
- Leave a cell blank when it has no visible content.
- If the region is not actually a table or a grouped label/value panel, answer with the single word SKIP.
- Output only markdown tables (or SKIP). No commentary, no extra headings.

Region text blocks:
{region_blocks}
"""

DEFAULT_OLLAMA_MODEL = "hf.co/unsloth/Qwen3-VL-8B-Instruct-GGUF:Q4_K_M"
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
}
IMAGE_PLACEHOLDER_RE = re.compile(
    r"\{\{DOC_IMAGE_[^}]+\}\}|<!--\s*image\s*-->|!\[[^\]]*\]\([^)]*\)",
    re.IGNORECASE,
)
DECORATIVE_LABELS = {"icon", "logo", "decorative", "stamp", "background"}
SUMMARY_LABELS = {"chart", "diagram", "figure", "graph", "map", "table", "infographic"}
NEARBY_BLOCK_DISTANCE = 0.12
PICTURE_MIN_AREA_RATIO = 0.01
PICTURE_DECORATIVE_MAX_AREA_RATIO = 0.05
PICTURE_TABLE_MIN_AREA_RATIO = 0.08

# --- Table-region detection (all coordinates normalized to the page) ---
# A panel gutter is an empty band no block crosses; x is tried before y so
# side-by-side panels split before row logic ever runs.
PANEL_X_GUTTER = 0.025
PANEL_Y_GUTTER = 0.06
PANEL_BANNER_MIN_WIDTH_RATIO = 0.25
PANEL_BANNER_WIDE_WIDTH_RATIO = 0.5
NON_CELL_KINDS = {"footnote", "page_footer", "page_header"}
TABLE_MIN_CELLS = 6
TABLE_MIN_COLUMNS = 2
TABLE_MIN_COLUMN_MEMBERS = 3
TABLE_MIN_ALIGNED_ROWS = 3
TABLE_COLUMN_X_TOLERANCE = 0.025
TABLE_ROW_Y_TOLERANCE = 0.02
TABLE_PROSE_MEDIAN_CHARS = 200
TABLE_LONG_CELL_CHARS = 200
TABLE_LONG_CELL_MAX_RATIO = 0.3
TABLE_TIMELINE_MAX_YEAR_CELLS = 3
TABLE_SHORT_CELL_CHARS = 120
TABLE_VALUE_CELL_CHARS = 160
TABLE_WIDE_CELL_RATIO = 0.7
TABLE_PAGE_SIZED_WIDTH = 0.75
TABLE_PAGE_SIZED_HEIGHT = 0.55
TABLE_SCORE_THRESHOLD = 0.55
TABLE_MERGE_MAX_X_GAP = 0.2
TABLE_MERGE_MIN_Y_OVERLAP = 0.5
TABLE_MERGE_MIN_ANCHOR = 0.9
TABLE_REGIONS_MAX_PER_PAGE = 3
TABLE_NUMERIC_COVERAGE_MIN = 0.9
TABLE_WORD_COVERAGE_MIN = 0.8
NUMERIC_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)*")
WORD_TOKEN_RE = re.compile(r"[^\W\d_]{4,}", re.UNICODE)
VALUE_SIGNAL_RE = re.compile(
    r"(?i)(?:\b(?:19|20)\d{2}\b|\d[\d,.\s]*(?:%|pts?|bps?|bn|m|k|kg|g|t|tons?|"
    r"tonnes?|co2|co2e|eur|usd|gbp|l|ml|ha|m3)\b|[$\u20ac\u00a3]\s*\d|\d[\d,.\s]*[$\u20ac\u00a3])"
)
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")

# --- Divider-aware reading order (all coordinates normalized to the page) ---
# Docling's reading order treats some banded pages as full-height columns and
# interleaves content across horizontal section dividers. These constants
# drive a deterministic recursive region cut (bands first, then columns) that
# re-orders page items only when divider evidence contradicts Docling's order.
READING_MAX_DEPTH = 4
READING_EDGE_EPS = 0.006  # max normalized overshoot of a cell across a cut
READING_BOUNDARY_NUDGE = 0.002
READING_BOUNDARY_MIN_SPACING = 0.01
READING_H_DIVIDER_MIN_SPAN = 0.6  # h-rule must span this fraction of region width
READING_V_DIVIDER_MIN_SPAN = 0.35  # v-rule must span this fraction of region height
READING_BAND_GAP = 0.05  # whitespace-only horizontal band cut
READING_COLUMN_GUTTER = 0.035  # whitespace-only vertical column cut
READING_BANNER_WIDE_RATIO = 0.5
READING_BANNER_GAP = 0.028  # clearance a banner needs above it
READING_RULE_HEADING_GAP = 0.03  # a rule adopts a heading sitting this close above
READING_FULL_PAGE_AREA = 0.85  # cells this large (backgrounds) never block cuts
# Page furniture (running heads, nav bars, page numbers, edge logos) fully
# inside these margin strips keeps its position in Docling's stream instead of
# being re-sorted into a geometric band — moving it is churn, not a fix.
READING_PIN_TOP = 0.09
READING_PIN_BOTTOM = 0.93
READING_PIN_LEFT = 0.06
READING_PIN_RIGHT = 0.94
DIVIDER_MAX_THICKNESS = 0.008
DIVIDER_MIN_COLLECT_SPAN = 0.03
DIVIDER_MERGE_TOL = 0.005
DIVIDER_MERGE_GAP = 0.02


@dataclass
class PictureRecord:
    page: int
    index: int
    placeholder: str
    rel_path: str
    abs_path: str | None
    bbox: dict[str, Any] | None
    area_ratio: float
    classification: str
    caption: str
    summarize: bool = False
    skip_reason: str = ""
    triage_eligible: bool = False
    triage_type: str = ""
    triage_confidence: float | None = None
    triage_action: str = ""
    triage_warnings: list[str] = field(default_factory=list)
    triage_usage: dict[str, Any] | None = None
    summary: str = ""
    summary_type: str = ""
    summary_warnings: list[str] = field(default_factory=list)
    usage: dict[str, Any] | None = None


@dataclass
class TableCandidate:
    candidate_id: str
    kind: str
    bbox: list[float] | None
    source_block_ids: list[str] = field(default_factory=list)
    picture_index: int | None = None
    confidence: float = 0.0
    reason: str = ""
    markdown: str = ""
    verified: bool = False
    stats: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    crop_path: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parse marketing PDFs to RAG-ready markdown with Docling and optional Qwen image summaries."
    )
    parser.add_argument("pdf", type=Path, help="PDF file to parse.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs_docling_rag"),
        help="Where outputs will be written.",
    )
    parser.add_argument(
        "--config",
        action="append",
        default=[],
        help="Optional YAML configuration overlay; may be provided more than once.",
    )
    parser.add_argument("--dpi", type=int, default=200, help="Debug raster DPI.")
    parser.add_argument("--start-page", type=int, default=1, help="1-based start page.")
    parser.add_argument(
        "--end-page", type=int, default=0, help="1-based end page, 0 means until the end."
    )
    parser.add_argument(
        "--skip-vlm",
        action="store_true",
        help="Do not call Ollama; keep image references without summaries.",
    )
    parser.add_argument(
        "--ollama-base-url",
        default=os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434"),
        help="Base URL for Ollama.",
    )
    parser.add_argument(
        "--ollama-model",
        default=os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
        help="Ollama multimodal model used for image/chart summaries.",
    )
    parser.add_argument(
        "--triage-model",
        default=os.getenv("OLLAMA_TRIAGE_MODEL"),
        help="Optional faster Ollama model for picture triage; defaults to --ollama-model.",
    )
    parser.add_argument(
        "--triage-num-predict",
        type=int,
        default=64,
        help="Maximum generated tokens for picture triage JSON.",
    )
    parser.add_argument(
        "--triage-confidence",
        type=float,
        default=0.65,
        help="Minimum confidence required for type-specific picture routing.",
    )
    parser.add_argument(
        "--skip-picture-triage",
        action="store_true",
        help="Use the existing heuristic plus generic picture prompt without visual triage.",
    )
    parser.add_argument("--temperature", type=float, default=0.0, help="Ollama temperature.")
    parser.add_argument(
        "--num-ctx",
        type=int,
        default=8192,
        help="Ollama context window. 0 keeps the model default.",
    )
    parser.add_argument(
        "--num-predict",
        type=int,
        default=1200,
        help="Cap generated tokens per image summary. 0 disables the cap.",
    )
    parser.add_argument(
        "--auto-num-ctx",
        action="store_true",
        help="Size the context window per call from the prompt and image estimate.",
    )
    parser.add_argument(
        "--prompt-file",
        type=Path,
        default=None,
        help="Optional full-page VLM refinement prompt. Must contain {source_markdown}.",
    )
    parser.add_argument(
        "--no-divider-reorder",
        action="store_true",
        help="Disable divider-aware reading-order reconstruction; keep Docling's order.",
    )
    parser.add_argument(
        "--resume-from",
        choices=REPLAY_STAGES,
        default=None,
        help="Resume selected pages from a saved page checkpoint at this stage.",
    )
    return parser.parse_args()


def ensure_pdf(path: Path) -> Path:
    pdf_path = path.expanduser().resolve()
    if not pdf_path.exists():
        raise FileNotFoundError(f"Missing PDF: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a PDF file: {pdf_path}")
    return pdf_path


def load_summary_prompt(path: Path | None) -> str:
    if path is None:
        return require_placeholders(load_prompt("picture_generic.md"), "context")
    template = path.expanduser().resolve().read_text(encoding="utf-8")
    if "{context}" not in template:
        template = template.rstrip() + "\n\nContext:\n{context}\n"
    return template


def load_page_refinement_prompt(path: Path | None) -> str:
    if path is None:
        return require_placeholders(
            load_prompt("page_refinement.md"),
            "source_markdown",
            "layout_blocks",
            "verified_tables",
        )
    template = path.expanduser().resolve().read_text(encoding="utf-8")
    if "{source_markdown}" not in template:
        raise ValueError("Page refinement prompt must contain {source_markdown}.")
    if "{layout_blocks}" not in template:
        template = template.rstrip() + "\n\nCompact layout block map:\n{layout_blocks}\n"
    if "{verified_tables}" not in template:
        template = template.rstrip() + "\n\nPre-verified tables:\n{verified_tables}\n"
    return template


def load_page_repair_prompt() -> str:
    return require_placeholders(
        load_prompt("page_repair.md"),
        "current_markdown",
        "layout_blocks",
        "verified_tables",
        "unplaced_lines",
    )


def selected_page_numbers(page_count: int, start_page: int, end_page: int) -> list[int]:
    if start_page < 1:
        raise ValueError("--start-page must be at least 1")
    final_end = end_page or page_count
    if final_end < start_page:
        raise ValueError("--end-page must be >= --start-page")
    return list(range(start_page, min(final_end, page_count) + 1))


def rasterize_page(doc: Any, page_number: int, dpi: int, output_path: Path) -> None:
    import fitz  # noqa: PLC0415

    matrix = fitz.Matrix(dpi / 72, dpi / 72)
    page = doc.load_page(page_number - 1)
    page.get_pixmap(matrix=matrix).save(output_path)


def build_docling_converter() -> Any:
    from docling.datamodel.base_models import InputFormat  # noqa: PLC0415
    from docling.datamodel.pipeline_options import (  # noqa: PLC0415
        PdfPipelineOptions,
        RapidOcrOptions,
        TableFormerMode,
        TableStructureOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption  # noqa: PLC0415

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = True
    pipeline_options.ocr_options = make_rapidocr_options(RapidOcrOptions)
    pipeline_options.do_table_structure = True
    pipeline_options.table_structure_options = make_table_options(
        TableStructureOptions, TableFormerMode
    )
    set_if_present(pipeline_options, "generate_picture_images", True)
    set_if_present(pipeline_options, "generate_page_images", False)
    set_if_present(pipeline_options, "images_scale", 2.0)
    set_if_present(pipeline_options, "do_picture_description", False)
    set_if_present(pipeline_options, "do_chart_extraction", False)
    set_if_present(pipeline_options, "do_picture_classification", False)
    configure_cuda_accelerator(pipeline_options)

    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
    )


def set_if_present(obj: Any, attr: str, value: Any) -> None:
    if hasattr(obj, attr):
        setattr(obj, attr, value)


def make_rapidocr_options(options_cls: Any) -> Any:
    for kwargs in (
        {"force_full_page_ocr": False, "lang": ["english"], "bitmap_area_threshold": 0.05},
        {"force_full_page_ocr": False, "lang": ["en"]},
        {"force_full_page_ocr": False},
        {},
    ):
        try:
            options = options_cls(**kwargs)
            set_if_present(options, "force_full_page_ocr", False)
            return options
        except TypeError:
            continue
    options = options_cls()
    set_if_present(options, "force_full_page_ocr", False)
    return options


def make_table_options(options_cls: Any, table_mode_cls: Any) -> Any:
    mode = getattr(table_mode_cls, "ACCURATE", None) or "accurate"
    for kwargs in ({"mode": mode, "do_cell_matching": True}, {"mode": mode}, {}):
        try:
            options = options_cls(**kwargs)
            set_if_present(options, "mode", mode)
            set_if_present(options, "do_cell_matching", True)
            return options
        except TypeError:
            continue
    options = options_cls()
    set_if_present(options, "mode", mode)
    set_if_present(options, "do_cell_matching", True)
    return options


def configure_cuda_accelerator(pipeline_options: Any) -> None:
    try:
        from docling.datamodel.accelerator_options import (  # type: ignore  # noqa: PLC0415
            AcceleratorDevice,
            AcceleratorOptions,
        )
    except Exception:
        try:
            from docling.datamodel.pipeline_options import (  # type: ignore  # noqa: PLC0415
                AcceleratorDevice,
                AcceleratorOptions,
            )
        except Exception:
            return
    pipeline_options.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.CUDA)


def convert_pdf(converter: Any, pdf_path: Path, pages: list[int]) -> Any:
    start_page, end_page = pages[0], pages[-1]
    try:
        return converter.convert(pdf_path, page_range=(start_page, end_page)).document
    except TypeError:
        return converter.convert(pdf_path).document


def iter_doc_items(document: Any, page_number: int | None = None) -> Iterable[Any]:
    iterator = document.iterate_items() if hasattr(document, "iterate_items") else []
    for entry in iterator:
        item = entry[0] if isinstance(entry, tuple) else entry
        if page_number is None or item_page_number(item) == page_number:
            yield item


def item_page_number(item: Any) -> int | None:
    prov = getattr(item, "prov", None) or []
    if not prov:
        return None
    return getattr(prov[0], "page_no", None)


def item_kind(item: Any) -> str:
    label = getattr(item, "label", "")
    if label:
        return str(label).split(".")[-1].lower()
    name = type(item).__name__
    return re.sub(r"item$", "", name, flags=re.IGNORECASE).lower()


def is_picture_item(item: Any) -> bool:
    return "picture" in type(item).__name__.lower() or item_kind(item) == "picture"


def is_table_item(item: Any) -> bool:
    return "table" in type(item).__name__.lower() or item_kind(item) == "table"


def is_heading_item(item: Any) -> bool:
    kind = item_kind(item)
    name = type(item).__name__.lower()
    return "title" in name or "sectionheader" in name or kind in {"title", "section_header"}


def item_text(item: Any) -> str:
    for attr in ("text", "orig", "caption"):
        value = getattr(item, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if hasattr(item, "get_text"):
        try:
            value = item.get_text()
            if isinstance(value, str):
                return value.strip()
        except Exception:
            pass
    return ""


def caption_text(item: Any) -> str:
    for attr in ("caption_text", "caption"):
        value = getattr(item, attr, None)
        if callable(value):
            try:
                out = value()
                if isinstance(out, str):
                    return out.strip()
            except Exception:
                pass
        elif isinstance(value, str):
            return value.strip()
    return ""


def bbox_dict(item: Any) -> dict[str, Any] | None:
    prov = getattr(item, "prov", None) or []
    if not prov:
        return None
    bbox = getattr(prov[0], "bbox", None)
    if bbox is None:
        return None
    values: dict[str, Any] = {}
    for key, aliases in {
        "l": ("l", "left", "x0"),
        "t": ("t", "top", "y0"),
        "r": ("r", "right", "x1"),
        "b": ("b", "bottom", "y1"),
    }.items():
        for alias in aliases:
            if hasattr(bbox, alias):
                values[key] = float(getattr(bbox, alias))
                break
    if len(values) < 4 and isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        values = {"l": float(bbox[0]), "t": float(bbox[1]), "r": float(bbox[2]), "b": float(bbox[3])}
    if len(values) < 4:
        return None
    origin = getattr(bbox, "coord_origin", None) or getattr(bbox, "origin", None)
    if origin is not None:
        values["origin"] = str(origin).split(".")[-1]
    return values


def bbox_area_ratio(bbox: dict[str, Any] | None, page_size: tuple[float, float]) -> float:
    if not bbox:
        return 0.0
    width = abs(float(bbox["r"]) - float(bbox["l"]))
    height = abs(float(bbox["b"]) - float(bbox["t"]))
    page_area = max(page_size[0] * page_size[1], 1.0)
    return min(1.0, (width * height) / page_area)


def bbox_to_pixel_rect(
    bbox: dict[str, Any] | None,
    *,
    page_size: tuple[float, float],
    image_size: tuple[int, int],
) -> tuple[int, int, int, int] | None:
    if not bbox:
        return None
    page_width, page_height = page_size
    image_width, image_height = image_size
    if page_width <= 0 or page_height <= 0 or image_width <= 0 or image_height <= 0:
        return None

    scale_x = image_width / page_width
    scale_y = image_height / page_height
    left = float(bbox["l"])
    right = float(bbox["r"])
    top = float(bbox["t"])
    bottom = float(bbox["b"])
    origin = str(bbox.get("origin", "")).upper()

    x0 = min(left, right) * scale_x
    x1 = max(left, right) * scale_x
    if origin == "BOTTOMLEFT":
        y0 = (page_height - max(top, bottom)) * scale_y
        y1 = (page_height - min(top, bottom)) * scale_y
    else:
        y0 = min(top, bottom) * scale_y
        y1 = max(top, bottom) * scale_y

    return (
        max(0, int(round(x0))),
        max(0, int(round(y0))),
        min(image_width, int(round(x1))),
        min(image_height, int(round(y1))),
    )


def bbox_to_normalized_rect(
    bbox: dict[str, Any] | None,
    page_size: tuple[float, float],
) -> list[float] | None:
    if not bbox:
        return None
    page_width, page_height = page_size
    if page_width <= 0 or page_height <= 0:
        return None

    left = float(bbox["l"])
    right = float(bbox["r"])
    top = float(bbox["t"])
    bottom = float(bbox["b"])
    origin = str(bbox.get("origin", "")).upper()

    x0 = min(left, right)
    x1 = max(left, right)
    if origin == "BOTTOMLEFT":
        y0 = page_height - max(top, bottom)
        y1 = page_height - min(top, bottom)
    else:
        y0 = min(top, bottom)
        y1 = max(top, bottom)

    rect = [
        max(0.0, min(1.0, x0 / page_width)),
        max(0.0, min(1.0, y0 / page_height)),
        max(0.0, min(1.0, x1 / page_width)),
        max(0.0, min(1.0, y1 / page_height)),
    ]
    return [round(value, 3) for value in rect]


def rect_center(rect: list[float] | None) -> tuple[float, float] | None:
    if not rect:
        return None
    return ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)


def rect_distance(a: list[float] | None, b: list[float] | None) -> float:
    center_a = rect_center(a)
    center_b = rect_center(b)
    if center_a is None or center_b is None:
        return 999.0
    return ((center_a[0] - center_b[0]) ** 2 + (center_a[1] - center_b[1]) ** 2) ** 0.5


def text_has_value_signal(text: str) -> bool:
    return bool(VALUE_SIGNAL_RE.search(text))


def is_timeline_candidate(text: str) -> bool:
    stripped = text.strip()
    return bool(YEAR_RE.fullmatch(stripped) or VALUE_ONLY_RE.match(stripped))


def is_table_like_kind(kind: str) -> bool:
    return kind in {"table", "table_cell", "caption"} or "table" in kind


def prompt_block_type(item: Any) -> str:
    kind = item_kind(item)
    if is_picture_item(item):
        return "picture"
    if is_table_item(item):
        return "table"
    if kind == "text":
        return "paragraph"
    return kind


def truncate_prompt_text(text: str, limit: int) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 1)].rstrip() + "..."


def layout_unplaced_match(text: str, unplaced_lines: list[str] | None) -> bool:
    if not text or not unplaced_lines:
        return False
    lower_text = text.lower()
    for line in unplaced_lines:
        lower_line = line.lower().strip()
        if not lower_line:
            continue
        if lower_text in lower_line or lower_line[:120] in lower_text:
            return True
    return False


def timeline_cluster_indices(entries: list[dict[str, Any]]) -> set[int]:
    candidates: list[tuple[int, tuple[float, float]]] = []
    for index, entry in enumerate(entries):
        if not entry["rect"] or not is_timeline_candidate(entry["text"]):
            continue
        center = rect_center(entry["rect"])
        if center is not None:
            candidates.append((index, center))
    clustered: set[int] = set()
    for axis in (0, 1):
        for index, center in candidates:
            aligned = [
                other_index
                for other_index, other_center in candidates
                if abs(center[axis] - other_center[axis]) <= 0.08
            ]
            if len(aligned) >= 3:
                clustered.update(aligned)
    return clustered


def nearby_layout_context(
    entry_index: int,
    entries: list[dict[str, Any]],
    timeline_indices: set[int],
) -> dict[str, Any]:
    entry = entries[entry_index]
    structural_count = 0
    near_structured = False
    for other_index, other in enumerate(entries):
        if other_index == entry_index:
            continue
        distance = rect_distance(entry["rect"], other["rect"])
        if distance > NEARBY_BLOCK_DISTANCE:
            continue
        other_structural = (
            other["is_picture"]
            or other["is_table"]
            or other["is_heading"]
            or other["kind"] == "caption"
            or other_index in timeline_indices
            or text_has_value_signal(other["text"])
        )
        if other_structural:
            structural_count += 1
            near_structured = True
    return {
        "near_structured": near_structured,
        "multiple_candidate_parents": structural_count >= 2,
        "in_timeline_cluster": entry_index in timeline_indices,
    }


def should_include_layout_text(
    *,
    entry: dict[str, Any],
    context: dict[str, Any],
    unplaced_lines: list[str] | None,
) -> bool:
    text = entry["text"].strip()
    if not text:
        return False
    kind = entry["kind"]
    area_ratio = entry["area_ratio"]
    if entry["is_heading"] or is_table_like_kind(kind):
        return True
    if text_has_value_signal(text):
        return True
    if len(text) <= 240 and area_ratio <= 0.03:
        return True
    if context["near_structured"]:
        return True
    if context["multiple_candidate_parents"]:
        return True
    if layout_unplaced_match(text, unplaced_lines):
        return True
    if context["in_timeline_cluster"] or is_timeline_candidate(text):
        return True
    return False


def build_layout_prompt_map(
    items: list[Any],
    page_size: tuple[float, float],
    picture_records: dict[int, PictureRecord],
    unplaced_lines: list[str] | None = None,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        bbox = bbox_dict(item)
        text = item_text(item)
        caption = caption_text(item)
        record = picture_records.get(id(item))
        if record and record.caption:
            caption = record.caption
        kind = item_kind(item)
        rect = bbox_to_normalized_rect(bbox, page_size)
        critical_text = bool(text and (text_has_value_signal(text) or layout_unplaced_match(text, unplaced_lines)))
        if rect is None and not critical_text:
            continue
        entries.append(
            {
                "id": f"b{index:04d}",
                "item": item,
                "kind": kind,
                "type": prompt_block_type(item),
                "bbox": bbox,
                "rect": rect,
                "area_ratio": bbox_area_ratio(bbox, page_size),
                "text": text,
                "caption": caption,
                "is_picture": is_picture_item(item),
                "is_table": is_table_item(item),
                "is_heading": is_heading_item(item),
            }
        )

    timeline_indices = timeline_cluster_indices(entries)
    blocks: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        block: dict[str, Any] = {
            "id": entry["id"],
            "type": entry["type"],
            "bbox": entry["rect"],
        }
        if entry["is_picture"]:
            if entry["caption"]:
                block["caption"] = truncate_prompt_text(entry["caption"], 240)
            blocks.append(block)
            continue

        context = nearby_layout_context(index, entries, timeline_indices)
        if should_include_layout_text(
            entry=entry,
            context=context,
            unplaced_lines=unplaced_lines,
        ):
            limit = 500 if entry["is_table"] else 240
            block["text"] = truncate_prompt_text(entry["text"], limit)
        blocks.append(block)

    return {
        "page_size": [round(page_size[0], 3), round(page_size[1], 3)],
        "blocks": blocks,
    }


def layout_map_stats(layout_map: dict[str, Any]) -> dict[str, int]:
    blocks = layout_map.get("blocks", [])
    return {
        "layout_block_count": len(blocks),
        "layout_text_block_count": sum(
            1 for block in blocks if block.get("text") or block.get("caption")
        ),
    }


def layout_map_prompt_json(layout_map: dict[str, Any]) -> str:
    return json.dumps(layout_map, ensure_ascii=False, separators=(",", ":"))


def rect_area(rect: list[float] | None) -> float:
    if not rect:
        return 0.0
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def rect_aspect_ratio(rect: list[float] | None) -> float:
    if not rect:
        return 0.0
    height = max(rect[3] - rect[1], 0.001)
    return max(rect[2] - rect[0], 0.0) / height


def rect_union(rects: list[list[float]]) -> list[float] | None:
    if not rects:
        return None
    return [
        round(min(rect[0] for rect in rects), 3),
        round(min(rect[1] for rect in rects), 3),
        round(max(rect[2] for rect in rects), 3),
        round(max(rect[3] for rect in rects), 3),
    ]


def rect_intersection_area(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b:
        return 0.0
    x0 = max(a[0], b[0])
    y0 = max(a[1], b[1])
    x1 = min(a[2], b[2])
    y1 = min(a[3], b[3])
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def rect_overlap_ratio(a: list[float] | None, b: list[float] | None) -> float:
    area = min(rect_area(a), rect_area(b))
    if area <= 0:
        return 0.0
    return rect_intersection_area(a, b) / area


def normalized_rect_to_pixel_rect(
    rect: list[float] | None, image_size: tuple[int, int]
) -> tuple[int, int, int, int] | None:
    if not rect:
        return None
    image_width, image_height = image_size
    x0 = int(round(rect[0] * image_width))
    y0 = int(round(rect[1] * image_height))
    x1 = int(round(rect[2] * image_width))
    y1 = int(round(rect[3] * image_height))
    if x1 <= x0 or y1 <= y0:
        return None
    return (
        max(0, x0),
        max(0, y0),
        min(image_width, x1),
        min(image_height, y1),
    )


def collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def detection_cells_from_items(
    items: list[Any], page_size: tuple[float, float]
) -> list[dict[str, Any]]:
    """Text cells with full (untruncated) text for table-region detection.

    Ids match build_layout_prompt_map ids: both enumerate the same item list.
    """
    cells: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        if is_picture_item(item) or is_table_item(item):
            continue
        if item_kind(item) in NON_CELL_KINDS:
            continue
        text = collapse_ws(item_text(item))
        rect = bbox_to_normalized_rect(bbox_dict(item), page_size)
        if not text or rect is None:
            continue
        cells.append(
            {
                "id": f"b{index:04d}",
                "rect": rect,
                "text": text,
                "is_heading": is_heading_item(item),
            }
        )
    return cells


def detection_cells_from_layout_map(layout_map: dict[str, Any]) -> list[dict[str, Any]]:
    """Fallback cell source (tests / offline replay); text may be truncated."""
    cells: list[dict[str, Any]] = []
    for block in layout_map.get("blocks", []):
        if block.get("type") in {"picture", "table"} or block.get("type") in NON_CELL_KINDS:
            continue
        text = collapse_ws(str(block.get("text") or ""))
        rect = block.get("bbox")
        if not text or not rect:
            continue
        cells.append(
            {
                "id": block["id"],
                "rect": rect,
                "text": text,
                "is_heading": block.get("type") in {"title", "section_header"},
            }
        )
    return cells


def cluster_axis_values(values: list[float], tolerance: float) -> list[float]:
    if not values:
        return []
    clusters: list[list[float]] = []
    for value in sorted(values):
        if not clusters or abs(value - (sum(clusters[-1]) / len(clusters[-1]))) > tolerance:
            clusters.append([value])
        else:
            clusters[-1].append(value)
    return [sum(cluster) / len(cluster) for cluster in clusters]


def nearest_cluster_index(value: float, clusters: list[float]) -> int:
    if not clusters:
        return 0
    return min(range(len(clusters)), key=lambda index: abs(clusters[index] - value))


def axis_gaps(cells: list[dict[str, Any]], axis: int, min_gap: float) -> list[float]:
    """Midpoints of empty bands along an axis that no cell interval crosses."""
    intervals = sorted((cell["rect"][axis], cell["rect"][axis + 2]) for cell in cells)
    if not intervals:
        return []
    gaps: list[float] = []
    max_end = intervals[0][1]
    for start, end in intervals[1:]:
        if start - max_end >= min_gap:
            gaps.append((max_end + start) / 2)
        max_end = max(max_end, end)
    return gaps


def partition_cells_at(
    cells: list[dict[str, Any]], axis: int, boundaries: list[float]
) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = [[] for _ in range(len(boundaries) + 1)]
    for cell in cells:
        center = (cell["rect"][axis] + cell["rect"][axis + 2]) / 2
        groups[sum(1 for boundary in boundaries if center > boundary)].append(cell)
    return [group for group in groups if group]


def segment_panels(cells: list[dict[str, Any]], depth: int = 0) -> list[list[dict[str, Any]]]:
    """Recursive XY-cut: split at vertical gutters first, then horizontal ones.

    Multi-column prose splits into single-column panels (which can never look
    like tables), while a real table's columns stay together because its
    banner/header rows span across the column gaps.
    """
    if len(cells) < TABLE_MIN_CELLS or depth >= 3:
        return [cells]
    # Never y-split a single-column group: a severed label column has sparse
    # row spacing and would shred into fragments, losing its only chance of
    # being re-joined with its content column by the merge pass.
    x_clusters = cluster_axis_values(
        [cell["rect"][0] for cell in cells], TABLE_COLUMN_X_TOLERANCE
    )
    if depth > 0 and len(x_clusters) < 2:
        return [cells]
    for axis, min_gap in ((0, PANEL_X_GUTTER), (1, PANEL_Y_GUTTER)):
        boundaries = axis_gaps(cells, axis, min_gap)
        if not boundaries:
            continue
        groups = partition_cells_at(cells, axis, boundaries)
        if len(groups) > 1:
            out: list[list[dict[str, Any]]] = []
            for group in groups:
                out.extend(segment_panels(group, depth + 1))
            return out
    return [cells]


def is_banner_cell(cell: dict[str, Any], panel_width: float) -> bool:
    """Section banners: heading blocks that are wide, or all-caps and not tiny.

    The all-caps escape matters because a banner's *text* bbox can be much
    narrower than the colored band it sits on (e.g. "THRIVING PEOPLE &
    COMMUNITIES" centered on a full-width bar).
    """
    if not cell.get("is_heading"):
        return False
    width = cell["rect"][2] - cell["rect"][0]
    if width < PANEL_BANNER_MIN_WIDTH_RATIO * panel_width:
        return False
    if width >= PANEL_BANNER_WIDE_WIDTH_RATIO * panel_width:
        return True
    text = cell["text"]
    return len(text) >= 8 and text.upper() == text and any(ch.isalpha() for ch in text)


def split_panel_at_banners(cells: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split a panel at section-banner heading blocks."""
    if len(cells) < TABLE_MIN_CELLS:
        return [cells]
    union = rect_union([cell["rect"] for cell in cells])
    if not union:
        return [cells]
    # In a single-column panel (e.g. a severed label column) every cell looks
    # "banner-wide" relative to the narrow panel — never split those.
    x_clusters = cluster_axis_values(
        [cell["rect"][0] for cell in cells], TABLE_COLUMN_X_TOLERANCE
    )
    if len(x_clusters) < 2:
        return [cells]
    panel_width = max(union[2] - union[0], 0.001)
    banners = sorted(
        (cell for cell in cells if is_banner_cell(cell, panel_width)),
        key=lambda cell: (cell["rect"][1] + cell["rect"][3]) / 2,
    )
    if not banners:
        return [cells]
    banner_ids = {cell["id"] for cell in banners}
    boundaries = [(cell["rect"][1] + cell["rect"][3]) / 2 for cell in banners]
    groups: list[list[dict[str, Any]]] = [[] for _ in range(len(boundaries) + 1)]
    for cell in cells:
        if cell["id"] in banner_ids:
            continue
        center_y = (cell["rect"][1] + cell["rect"][3]) / 2
        groups[sum(1 for boundary in boundaries if center_y > boundary)].append(cell)
    return [group for group in groups if group]


# --- Divider-aware reading order -------------------------------------------
# Segments are normalized triples: h = [y, x0, x1], v = [x, y0, y1].


def _merge_divider_segments(segments: list[list[float]]) -> list[list[float]]:
    """Merge near-collinear overlapping segments (split strokes, dashes)."""
    merged: list[list[float]] = []
    for pos, lo, hi in sorted(segments):
        for seg in merged:
            if (
                abs(seg[0] - pos) <= DIVIDER_MERGE_TOL
                and lo - seg[2] <= DIVIDER_MERGE_GAP
                and seg[1] - hi <= DIVIDER_MERGE_GAP
            ):
                seg[1] = min(seg[1], lo)
                seg[2] = max(seg[2], hi)
                break
        else:
            merged.append([pos, lo, hi])
    return merged


def divider_segments_from_drawings(
    drawings: list[dict[str, Any]] | None, page_size: tuple[float, float]
) -> dict[str, list[list[float]]]:
    """Thin, long strokes/fills from PDF vector graphics, as rule segments.

    Region-relative span and clean-corridor checks happen later, so this only
    needs to be permissive enough: everything long-and-thin is collected, and
    chart gridlines etc. are vetoed at cut time by the atomic cells they cross.
    """
    page_width, page_height = page_size
    if page_width <= 0 or page_height <= 0:
        return {"h": [], "v": []}
    h_raw: list[list[float]] = []
    v_raw: list[list[float]] = []

    def add_span(x0: float, y0: float, x1: float, y1: float) -> None:
        x0, x1 = sorted((x0 / page_width, x1 / page_width))
        y0, y1 = sorted((y0 / page_height, y1 / page_height))
        if y1 - y0 <= DIVIDER_MAX_THICKNESS and x1 - x0 >= DIVIDER_MIN_COLLECT_SPAN:
            h_raw.append([(y0 + y1) / 2, x0, x1])
        elif x1 - x0 <= DIVIDER_MAX_THICKNESS and y1 - y0 >= DIVIDER_MIN_COLLECT_SPAN:
            v_raw.append([(x0 + x1) / 2, y0, y1])

    for drawing in drawings or []:
        entries = drawing.get("items") if isinstance(drawing, dict) else None
        for entry in entries or []:
            if not entry:
                continue
            kind, coords = entry[0], entry[1:]
            try:
                if kind == "l" and len(coords) >= 2:
                    add_span(
                        float(coords[0].x),
                        float(coords[0].y),
                        float(coords[1].x),
                        float(coords[1].y),
                    )
                elif kind == "re" and coords:
                    rect = coords[0]
                    add_span(
                        float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)
                    )
            except (AttributeError, TypeError, ValueError):
                continue
    return {"h": _merge_divider_segments(h_raw), "v": _merge_divider_segments(v_raw)}


def extract_divider_segments(
    pdf_page: Any, page_size: tuple[float, float]
) -> dict[str, list[list[float]]]:
    """Rule segments from a fitz page's vector drawings; empty on any failure."""
    try:
        drawings = pdf_page.get_drawings()
    except Exception:
        return {"h": [], "v": []}
    return divider_segments_from_drawings(drawings, page_size)


def _owns_row(cell: dict[str, Any], cells: list[dict[str, Any]]) -> bool:
    """True when no other cell shares this cell's horizontal strip."""
    top, bottom = cell["rect"][1], cell["rect"][3]
    for other in cells:
        if other is cell:
            continue
        overlap = min(bottom, other["rect"][3]) - max(top, other["rect"][1])
        limit = max(
            0.002, 0.3 * min(bottom - top, other["rect"][3] - other["rect"][1])
        )
        if overlap > limit:
            return False
    return True


def _clearance_above(cell: dict[str, Any], cells: list[dict[str, Any]]) -> float:
    """Vertical whitespace between this cell and the nearest cell fully above it."""
    top = cell["rect"][1]
    prev_bottom: float | None = None
    for other in cells:
        if other is cell:
            continue
        bottom = other["rect"][3]
        if bottom <= top + 0.002 and (prev_bottom is None or bottom > prev_bottom):
            prev_bottom = bottom
    if prev_bottom is None:
        return 1.0
    return top - prev_bottom


def _banner_boundaries(
    cells: list[dict[str, Any]], region: list[float]
) -> list[float]:
    """Cut boundaries just above wide section-banner headings.

    Only headings spanning most of the region width and owning their full
    horizontal strip qualify: a narrow heading that merely happens to have
    whitespace beside it (e.g. a panel title in a multi-panel dashboard) is
    not band evidence on its own — narrow banners only produce cuts when a
    graphical rule adopts them via _lift_boundary_above_headings.
    """
    region_width = max(region[2] - region[0], 0.001)
    bounds: list[float] = []
    for cell in cells:
        if not cell["is_heading"]:
            continue
        if cell["rect"][2] - cell["rect"][0] < READING_BANNER_WIDE_RATIO * region_width:
            continue
        if not _owns_row(cell, cells):
            continue
        if _clearance_above(cell, cells) < READING_BANNER_GAP:
            continue
        bounds.append(cell["rect"][1] - READING_BOUNDARY_NUDGE)
    return bounds


def _lift_boundary_above_headings(
    boundary: float, cells: list[dict[str, Any]]
) -> float:
    """A rule drawn under a section heading separates bands *above* the heading."""
    for _ in range(2):
        adopted = [
            cell
            for cell in cells
            if cell["is_heading"]
            and cell["rect"][3] <= boundary + READING_EDGE_EPS
            and boundary - cell["rect"][3] <= READING_RULE_HEADING_GAP
            and _owns_row(cell, cells)
        ]
        if not adopted:
            break
        boundary = min(cell["rect"][1] for cell in adopted) - READING_BOUNDARY_NUDGE
    return boundary


def _boundary_is_clean(
    cells: list[dict[str, Any]], axis: int, boundary: float
) -> bool:
    """No cell straddles the cut by more than the edge tolerance."""
    for cell in cells:
        lo, hi = cell["rect"][axis], cell["rect"][axis + 2]
        if lo < boundary - READING_EDGE_EPS and hi > boundary + READING_EDGE_EPS:
            return False
    return True


def _clean_boundaries(
    bounds: list[float],
    cells: list[dict[str, Any]],
    axis: int,
    region: list[float],
) -> list[float]:
    lo, hi = (region[1], region[3]) if axis == 1 else (region[0], region[2])
    out: list[float] = []
    for boundary in sorted(bounds):
        if not lo + READING_EDGE_EPS < boundary < hi - READING_EDGE_EPS:
            continue
        if out and boundary - out[-1] < READING_BOUNDARY_MIN_SPACING:
            continue
        if _boundary_is_clean(cells, axis, boundary):
            out.append(boundary)
    return out


def _horizontal_boundaries(
    cells: list[dict[str, Any]],
    region: list[float],
    dividers: dict[str, list[list[float]]],
) -> list[float]:
    region_width = max(region[2] - region[0], 0.001)
    bounds: list[float] = []
    for pos, lo, hi in dividers.get("h", []):
        if not region[1] + READING_EDGE_EPS < pos < region[3] - READING_EDGE_EPS:
            continue
        if min(hi, region[2]) - max(lo, region[0]) < READING_H_DIVIDER_MIN_SPAN * region_width:
            continue
        # A rule only counts as a section divider when its banner heading sits
        # right above it (the heading + underline pattern). Heading-less lines
        # (table row separators, decorative strips between timeline rows) are
        # not band evidence — those layouts only split at the wide-whitespace
        # or wide-banner boundaries below.
        lifted = _lift_boundary_above_headings(pos, cells)
        if lifted < pos - READING_BOUNDARY_NUDGE / 2:
            bounds.append(lifted)
    bounds.extend(_banner_boundaries(cells, region))
    bounds.extend(axis_gaps(cells, 1, READING_BAND_GAP))
    return _clean_boundaries(bounds, cells, 1, region)


def _vertical_boundaries(
    cells: list[dict[str, Any]],
    region: list[float],
    dividers: dict[str, list[list[float]]],
) -> list[float]:
    region_height = max(region[3] - region[1], 0.001)
    bounds: list[float] = []
    for pos, lo, hi in dividers.get("v", []):
        if not region[0] + READING_EDGE_EPS < pos < region[2] - READING_EDGE_EPS:
            continue
        if min(hi, region[3]) - max(lo, region[1]) < READING_V_DIVIDER_MIN_SPAN * region_height:
            continue
        bounds.append(pos)
    bounds.extend(axis_gaps(cells, 0, READING_COLUMN_GUTTER))
    return _clean_boundaries(bounds, cells, 0, region)


def _order_region_cells(
    cells: list[dict[str, Any]],
    dividers: dict[str, list[list[float]]],
    depth: int = 0,
) -> list[dict[str, Any]]:
    """Recursive divider-aware region cut; leaves keep Docling's own order.

    Horizontal band cuts are tried before vertical column cuts, so a divider
    that separates stacked sections wins over the full-height column reading
    Docling defaulted to. Within a leaf region the original order is kept,
    which makes the whole pass a no-op on pages Docling already got right.
    """
    by_docling_order = sorted(cells, key=lambda cell: cell["index"])
    if len(cells) <= 1 or depth >= READING_MAX_DEPTH:
        return by_docling_order
    # Full-page backgrounds and hero images must not veto or absorb cuts.
    blocking = [cell for cell in cells if cell["blocking"]]
    if len(blocking) < 2:
        return by_docling_order
    region = rect_union([cell["rect"] for cell in blocking])
    if not region:
        return by_docling_order
    for axis, boundaries in (
        (1, _horizontal_boundaries(blocking, region, dividers)),
        (0, _vertical_boundaries(blocking, region, dividers)),
    ):
        if not boundaries:
            continue
        groups = partition_cells_at(cells, axis, boundaries)
        if len(groups) < 2:
            continue
        ordered: list[dict[str, Any]] = []
        for group in groups:
            ordered.extend(_order_region_cells(group, dividers, depth + 1))
        return ordered
    return by_docling_order


def _normalize_caption_order(
    order: list[int], cells_by_index: dict[int, dict[str, Any]]
) -> list[int]:
    """Read a caption before its picture when it sits visually above it.

    Docling streams caption items after their picture regardless of where the
    caption is placed, so a chart whose title is typed as a caption would
    otherwise serialize as image-then-title. Captions below their picture
    (classic figure captions) keep their position.
    """
    out = list(order)
    for _ in range(2):  # a picture can carry a short stack of caption lines
        swapped = False
        for pos in range(len(out) - 1):
            picture = cells_by_index.get(out[pos])
            caption = cells_by_index.get(out[pos + 1])
            if not picture or not caption:
                continue
            if not picture.get("is_picture") or caption.get("kind") != "caption":
                continue
            pic_rect = picture.get("rect")
            cap_rect = caption.get("rect")
            if not pic_rect or not cap_rect:
                continue
            x_overlap = min(cap_rect[2], pic_rect[2]) - max(cap_rect[0], pic_rect[0])
            caption_above = (cap_rect[1] + cap_rect[3]) / 2 < (pic_rect[1] + pic_rect[3]) / 2
            if caption_above and x_overlap > 0:
                out[pos], out[pos + 1] = out[pos + 1], out[pos]
                swapped = True
        if not swapped:
            break
    return out


def reading_order_permutation(
    cells: list[dict[str, Any]],
    dividers: dict[str, list[list[float]]] | None,
) -> list[int] | None:
    """Divider-aware reading order over page cells; None when unchanged.

    Cells carry {index, rect (normalized, may be None), is_heading, text}.
    Cells without geometry, and margin furniture (running heads, nav bars,
    page numbers, edge logos), stay glued to the cell they followed in
    Docling's stream — moving furniture is churn, not a fix.
    """
    dividers = dividers or {"h": [], "v": []}
    anchors: list[dict[str, Any]] = []
    followers: dict[int, list[int]] = {}
    leading: list[int] = []
    last_anchor: int | None = None
    for cell in cells:
        rect = cell.get("rect")
        pinned_margin_furniture = rect is not None and (
            rect[3] <= READING_PIN_TOP
            or rect[1] >= READING_PIN_BOTTOM
            or rect[2] <= READING_PIN_LEFT
            or rect[0] >= READING_PIN_RIGHT
        )
        if rect is None or pinned_margin_furniture:
            if last_anchor is None:
                leading.append(cell["index"])
            else:
                followers.setdefault(last_anchor, []).append(cell["index"])
            continue
        last_anchor = cell["index"]
        anchors.append(
            {
                **cell,
                "blocking": rect_area(rect) < READING_FULL_PAGE_AREA,
            }
        )
    if len(anchors) < 4:
        return None
    ordered = _order_region_cells(anchors, dividers)
    order = [cell["index"] for cell in ordered]
    if order == [cell["index"] for cell in anchors]:
        return None
    full_order = list(leading)
    for index in order:
        full_order.append(index)
        full_order.extend(followers.get(index, []))
    full_order = _normalize_caption_order(
        full_order, {cell["index"]: cell for cell in cells}
    )
    if full_order == sorted(full_order):
        return None
    return full_order


def reorder_items_for_reading_order(
    items: list[Any],
    page_size: tuple[float, float],
    dividers: dict[str, list[list[float]]] | None,
) -> tuple[list[Any], dict[str, Any]]:
    """Re-order page items along divider-separated layout regions.

    Returns the (possibly reordered) items plus an info dict for the manifest.
    When the computed order matches Docling's, the original list is returned
    unchanged so callers keep using Docling's own markdown serializer.
    """
    dividers = dividers or {"h": [], "v": []}
    info: dict[str, Any] = {
        "applied": False,
        "moved_items": 0,
        "h_divider_count": len(dividers.get("h", [])),
        "v_divider_count": len(dividers.get("v", [])),
    }
    cells = [
        {
            "index": index,
            "rect": bbox_to_normalized_rect(bbox_dict(item), page_size),
            "is_heading": is_heading_item(item),
            "is_picture": is_picture_item(item),
            "kind": item_kind(item),
            "text": collapse_ws(item_text(item)),
        }
        for index, item in enumerate(items)
    ]
    full_order = reading_order_permutation(cells, dividers)
    if full_order is None:
        return items, info
    info["applied"] = True
    info["moved_items"] = sum(
        1 for position, index in enumerate(full_order) if position != index
    )
    return [items[index] for index in full_order], info


def region_grid_cells(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cells that can be table cells: everything except near-full-width lines.

    Headings stay in: Docling regularly labels short styled row labels as
    section_header; true section banners are wide and removed here or already
    consumed by split_panel_at_banners.
    """
    union = rect_union([cell["rect"] for cell in cells])
    if not union:
        return []
    region_width = max(union[2] - union[0], 0.001)
    return [
        cell
        for cell in cells
        if cell["rect"][2] - cell["rect"][0] <= TABLE_WIDE_CELL_RATIO * region_width
    ]


def is_value_cell(text: str) -> bool:
    stripped = text.strip()
    if VALUE_ONLY_RE.match(stripped):
        return True
    return len(stripped) <= TABLE_VALUE_CELL_CHARS and bool(VALUE_SIGNAL_RE.search(stripped))


def score_table_region(
    cells: list[dict[str, Any]], *, allow_page_sized: bool = False
) -> tuple[float, dict[str, Any]]:
    """Score how table-like a region is; 0.0 with stats["reject"] on hard veto.

    Discriminative signals (vs. multi-column prose): short cells, cross-column
    row alignment on left edges, and value-dominated columns. Prose columns
    have long cells and rows that never align across columns.
    """
    stats: dict[str, Any] = {"cells": len(cells)}
    if len(cells) < TABLE_MIN_CELLS:
        stats["reject"] = "too_few_cells"
        return 0.0, stats
    grid_cells = region_grid_cells(cells)
    stats["grid_cells"] = len(grid_cells)
    if len(grid_cells) < TABLE_MIN_CELLS:
        stats["reject"] = "too_few_grid_cells"
        return 0.0, stats

    lengths = sorted(len(cell["text"]) for cell in grid_cells)
    median_chars = lengths[len(lengths) // 2]
    stats["median_cell_chars"] = median_chars
    if median_chars > TABLE_PROSE_MEDIAN_CHARS:
        stats["reject"] = "prose_region"
        return 0.0, stats
    long_cell_ratio = sum(
        1 for cell in grid_cells if len(cell["text"]) > TABLE_LONG_CELL_CHARS
    ) / len(grid_cells)
    stats["long_cell_ratio"] = round(long_cell_ratio, 3)
    if long_cell_ratio > TABLE_LONG_CELL_MAX_RATIO:
        stats["reject"] = "prose_region"
        return 0.0, stats
    year_cells = sum(1 for cell in grid_cells if YEAR_RE.fullmatch(cell["text"].strip()))
    stats["year_cells"] = year_cells
    if year_cells > TABLE_TIMELINE_MAX_YEAR_CELLS:
        stats["reject"] = "timeline_region"
        return 0.0, stats
    union = rect_union([cell["rect"] for cell in grid_cells])
    if (
        not allow_page_sized
        and union
        and union[2] - union[0] > TABLE_PAGE_SIZED_WIDTH
        and union[3] - union[1] > TABLE_PAGE_SIZED_HEIGHT
    ):
        # Implicit styled tables in report layouts live inside panels; a region
        # covering most of the page is almost always merged mixed content.
        # (Genuine full-page ruled tables come in via Docling's table items.
        # Merged label/content unions are exempt: their own acceptance rules
        # demand a header row and near-perfect label alignment instead.)
        stats["reject"] = "page_sized_region"
        return 0.0, stats

    column_centers = cluster_axis_values(
        [cell["rect"][0] for cell in grid_cells], TABLE_COLUMN_X_TOLERANCE
    )
    column_members: dict[int, list[dict[str, Any]]] = {}
    for cell in grid_cells:
        column_members.setdefault(
            nearest_cluster_index(cell["rect"][0], column_centers), []
        ).append(cell)
    major_columns = {
        index: members
        for index, members in column_members.items()
        if len(members) >= TABLE_MIN_COLUMN_MEMBERS
    }
    stats["columns"] = len(major_columns)
    if len(major_columns) < TABLE_MIN_COLUMNS:
        stats["reject"] = "not_enough_columns"
        return 0.0, stats

    aligned_cells = [
        (column_index, cell)
        for column_index, members in major_columns.items()
        for cell in members
    ]
    row_centers = cluster_axis_values(
        [(cell["rect"][1] + cell["rect"][3]) / 2 for _, cell in aligned_cells],
        TABLE_ROW_Y_TOLERANCE,
    )
    row_assignments: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    for column_index, cell in aligned_cells:
        row_index = nearest_cluster_index(
            (cell["rect"][1] + cell["rect"][3]) / 2, row_centers
        )
        row_assignments.setdefault(row_index, []).append((column_index, cell))
    multi_column_rows = {
        row_index
        for row_index, members in row_assignments.items()
        if len({column_index for column_index, _ in members}) >= 2
    }
    aligned_rows = len(multi_column_rows)
    stats["rows"] = len(row_assignments)
    stats["aligned_rows"] = aligned_rows
    if aligned_rows < TABLE_MIN_ALIGNED_ROWS:
        stats["reject"] = "not_enough_aligned_rows"
        return 0.0, stats
    # Rowspan-aware alignment: in a real table at least one column (e.g. the
    # label column) has nearly all of its cells sitting on multi-column rows,
    # even when other columns add continuation rows. In multi-column prose no
    # column's paragraph tops line up with another column.
    column_row_hits: dict[int, int] = {}
    column_totals: dict[int, int] = {}
    for column_index, cell in aligned_cells:
        row_index = nearest_cluster_index(
            (cell["rect"][1] + cell["rect"][3]) / 2, row_centers
        )
        column_totals[column_index] = column_totals.get(column_index, 0) + 1
        if row_index in multi_column_rows:
            column_row_hits[column_index] = column_row_hits.get(column_index, 0) + 1
    anchor_alignment = max(
        column_row_hits.get(column_index, 0) / total
        for column_index, total in column_totals.items()
    )
    stats["anchor_alignment"] = round(anchor_alignment, 3)

    value_columns = sum(
        1
        for members in major_columns.values()
        if sum(1 for cell in members if is_value_cell(cell["text"])) >= 0.6 * len(members)
    )
    stats["value_columns"] = value_columns
    short_cell_ratio = sum(
        1 for cell in grid_cells if len(cell["text"]) <= TABLE_SHORT_CELL_CHARS
    ) / len(grid_cells)
    stats["short_cell_ratio"] = round(short_cell_ratio, 3)

    region_top = min(cell["rect"][1] for cell in grid_cells)
    region_bottom = max(cell["rect"][3] for cell in grid_cells)
    top_limit = region_top + 0.25 * max(region_bottom - region_top, 0.001)
    # Header labels like GOALS/TARGETS are plain short text cells; aligned
    # section headings across prose columns must NOT count as a header row.
    header_row = any(
        len({column_index for column_index, _ in members}) >= 2
        and row_centers[row_index] <= top_limit
        and all(
            len(cell["text"]) <= 40 and not cell.get("is_heading")
            for _, cell in members
        )
        for row_index, members in row_assignments.items()
    )
    stats["header_row"] = header_row

    score = 0.3
    if anchor_alignment >= 0.7:
        score += 0.2
    elif anchor_alignment >= 0.5:
        score += 0.1
    if value_columns >= 1:
        score += 0.2
    if short_cell_ratio >= 0.7:
        score += 0.1
    if header_row:
        score += 0.1
    if len(major_columns) >= 3 and short_cell_ratio >= 0.7:
        score += 0.1
    return round(min(score, 0.95), 3), stats


def add_table_candidate(
    candidates: list[TableCandidate],
    *,
    kind: str,
    bbox: list[float] | None,
    source_block_ids: list[str] | None = None,
    picture_index: int | None = None,
    confidence: float,
    reason: str,
    stats: dict[str, Any] | None = None,
) -> TableCandidate:
    candidate = TableCandidate(
        candidate_id=f"tc{len(candidates) + 1:03d}",
        kind=kind,
        bbox=bbox,
        source_block_ids=source_block_ids or [],
        picture_index=picture_index,
        confidence=round(confidence, 3),
        reason=reason,
        stats=stats,
    )
    candidates.append(candidate)
    return candidate


def picture_record_rect(record: PictureRecord, page_size: tuple[float, float]) -> list[float] | None:
    return bbox_to_normalized_rect(record.bbox, page_size)


def nearby_table_signal_blocks(
    rect: list[float] | None, blocks: list[dict[str, Any]], distance: float = 0.18
) -> list[dict[str, Any]]:
    if not rect:
        return []
    out: list[dict[str, Any]] = []
    for block in blocks:
        if not block.get("bbox") or block.get("type") == "picture":
            continue
        text = str(block.get("text") or block.get("caption") or "").strip()
        if not text:
            continue
        if rect_distance(rect, block["bbox"]) <= distance and (
            text_has_value_signal(text)
            or block.get("type") in {"title", "section_header", "caption"}
        ):
            out.append(block)
    return out


def picture_record_is_table_like(
    record: PictureRecord,
    rect: list[float] | None,
    nearby_blocks: list[dict[str, Any]],
) -> tuple[bool, str, float]:
    haystack = f"{record.classification} {record.caption}".lower()
    if record.area_ratio < PICTURE_TABLE_MIN_AREA_RATIO:
        return False, "picture_too_small", 0.0
    if any(label in haystack for label in DECORATIVE_LABELS):
        return False, "decorative_picture", 0.0
    aspect = rect_aspect_ratio(rect)
    has_semantic_label = any(label in haystack for label in SUMMARY_LABELS)
    has_nearby_signal = bool(nearby_blocks)
    wide_lower_region = bool(rect and aspect >= 1.8 and rect[1] >= 0.32)
    if not (wide_lower_region or has_semantic_label or has_nearby_signal):
        return False, "no_table_signal", 0.0
    confidence = 0.55
    reasons: list[str] = []
    if aspect >= 2.2:
        confidence += 0.15
        reasons.append("wide_region")
    if wide_lower_region:
        confidence += 0.1
        reasons.append("lower_wide_region")
    if has_semantic_label:
        confidence += 0.15
        reasons.append("semantic_label")
    if has_nearby_signal:
        confidence += 0.15
        reasons.append("nearby_structured_text")
    return True, ",".join(reasons) or "picture_table_signal", min(confidence, 0.95)


def regions_horizontally_adjacent(
    a_cells: list[dict[str, Any]], b_cells: list[dict[str, Any]]
) -> bool:
    a = rect_union([cell["rect"] for cell in a_cells])
    b = rect_union([cell["rect"] for cell in b_cells])
    if not a or not b:
        return False
    left, right = (a, b) if a[0] <= b[0] else (b, a)
    gap = right[0] - left[2]
    if gap > TABLE_MERGE_MAX_X_GAP or gap < -0.05:
        return False
    overlap = min(a[3], b[3]) - max(a[1], b[1])
    min_height = min(a[3] - a[1], b[3] - b[1])
    return min_height > 0 and overlap / min_height >= TABLE_MERGE_MIN_Y_OVERLAP


def merge_adjacent_regions(
    regions: list[list[dict[str, Any]]],
) -> list[tuple[float, dict[str, Any], list[dict[str, Any]]]]:
    """Re-join rejected regions that a wide gutter split apart, and re-score.

    A goals|targets layout with a wide empty band between the columns gets
    severed by the XY-cut into two single-column regions that can never score
    as tables individually; their union can. The scorer stays the gatekeeper,
    so joining two prose columns still fails on alignment/length.
    """
    ordered = sorted(
        regions,
        key=lambda region: (rect_union([cell["rect"] for cell in region]) or [1.0])[0],
    )
    merged: list[tuple[float, dict[str, Any], list[dict[str, Any]]]] = []
    used: set[int] = set()
    for i in range(len(ordered)):
        if i in used:
            continue
        for j in range(i + 1, len(ordered)):
            if j in used:
                continue
            if not regions_horizontally_adjacent(ordered[i], ordered[j]):
                continue
            union_region = ordered[i] + ordered[j]
            score, stats = score_table_region(union_region, allow_page_sized=True)
            # A merge is a rescue path, so demand stronger evidence than the
            # plain threshold: a true label column aligns (nearly) every cell
            # with a content row, and severed label/content tables carry a
            # header label row (GOALS/TARGETS style). Coincidentally aligned
            # prose columns fail both.
            if (
                score >= TABLE_SCORE_THRESHOLD
                and stats.get("anchor_alignment", 0.0) >= TABLE_MERGE_MIN_ANCHOR
                and stats.get("header_row")
            ):
                stats["merged_regions"] = 2
                merged.append((score, stats, union_region))
                used.update((i, j))
                break
    return merged


def evaluate_table_regions(
    cells: list[dict[str, Any]],
) -> tuple[
    list[tuple[float, dict[str, Any], list[dict[str, Any]], str]],
    list[tuple[float, dict[str, Any], list[dict[str, Any]]]],
]:
    """Segment the page and score every region; returns (accepted, rejected).

    Accepted entries are sorted by score and carry the detection reason;
    rejected ones are kept for debugging/replay.
    """
    accepted: list[tuple[float, dict[str, Any], list[dict[str, Any]], str]] = []
    rejected: list[tuple[float, dict[str, Any], list[dict[str, Any]]]] = []
    leftovers: list[list[dict[str, Any]]] = []
    for panel in segment_panels(cells):
        for region in split_panel_at_banners(panel):
            score, stats = score_table_region(region)
            if score >= TABLE_SCORE_THRESHOLD:
                accepted.append((score, stats, region, "table_shaped_region"))
            else:
                rejected.append((score, stats, region))
                leftovers.append(region)
    for score, stats, region in merge_adjacent_regions(leftovers):
        accepted.append((score, stats, region, "merged_split_columns"))
    accepted.sort(key=lambda entry: entry[0], reverse=True)
    return accepted, rejected


def build_table_candidates(
    *,
    cells: list[dict[str, Any]],
    page_size: tuple[float, float],
    picture_records: dict[int, PictureRecord],
    layout_map: dict[str, Any],
) -> list[TableCandidate]:
    candidates: list[TableCandidate] = []
    blocks = layout_map.get("blocks", [])

    for block in blocks:
        if block.get("type") != "table":
            continue
        add_table_candidate(
            candidates,
            kind="docling_table",
            bbox=block.get("bbox"),
            source_block_ids=[block["id"]],
            confidence=0.95,
            reason="docling_table_item",
        )

    scored_regions, _ = evaluate_table_regions(cells)
    for score, stats, region, reason in scored_regions[:TABLE_REGIONS_MAX_PER_PAGE]:
        bbox = rect_union([cell["rect"] for cell in region])
        if any(
            candidate.kind == "docling_table"
            and rect_overlap_ratio(candidate.bbox, bbox) > 0.6
            for candidate in candidates
        ):
            continue
        add_table_candidate(
            candidates,
            kind="layout_region",
            bbox=bbox,
            source_block_ids=[cell["id"] for cell in region],
            confidence=score,
            reason=reason,
            stats=stats,
        )

    for record in picture_records.values():
        rect = picture_record_rect(record, page_size)
        nearby_blocks = nearby_table_signal_blocks(rect, blocks)
        is_table_like, reason, confidence = picture_record_is_table_like(record, rect, nearby_blocks)
        if not is_table_like:
            continue
        source_block_ids = [block["id"] for block in nearby_blocks[:12]]
        matched_picture_block = next(
            (
                block
                for block in blocks
                if block.get("type") == "picture" and rect_overlap_ratio(block.get("bbox"), rect) > 0.8
            ),
            None,
        )
        if matched_picture_block:
            source_block_ids.insert(0, matched_picture_block["id"])
        add_table_candidate(
            candidates,
            kind="picture_table",
            bbox=rect,
            source_block_ids=source_block_ids,
            picture_index=record.index,
            confidence=confidence,
            reason=reason,
        )

    return candidates


def verified_tables_prompt_block(candidates: list[TableCandidate]) -> str:
    """Only verified region tables reach a prompt; everything else is debug-only."""
    parts = [
        f"Table for region at bbox {candidate.bbox}:\n{candidate.markdown.strip()}"
        for candidate in candidates
        if candidate.verified and candidate.markdown.strip()
    ]
    return "\n\n".join(parts) if parts else "(none)"


def numeric_tokens(text: str) -> set[str]:
    return {token.replace(",", ".") for token in NUMERIC_TOKEN_RE.findall(text or "")}


def word_tokens(text: str) -> set[str]:
    return {token.lower() for token in WORD_TOKEN_RE.findall(text or "")}


def verify_region_table(
    markdown: str, cell_texts: list[str]
) -> tuple[bool, dict[str, Any]]:
    """Deterministic acceptance check for a VLM-transcribed region table.

    The table must be structurally a pipe table, must not contain numbers
    absent from the source cells, and must cover (nearly) all source numbers —
    or, for numberless tables, most of the source words.
    """
    stats: dict[str, Any] = {}
    rows = [
        line.strip()
        for line in markdown.splitlines()
        if line.strip().startswith("|") and not set(line.strip()) <= set("|-: ")
    ]
    stats["table_rows"] = len(rows)
    if len(rows) < 2:
        stats["fail"] = "no_table_structure"
        return False, stats

    source_numbers = set().union(*(numeric_tokens(text) for text in cell_texts)) if cell_texts else set()
    table_numbers = numeric_tokens(markdown)
    invented = sorted(table_numbers - source_numbers)
    stats["invented_numbers"] = invented
    if invented:
        stats["fail"] = "invented_numbers"
        return False, stats

    if source_numbers:
        missing = sorted(source_numbers - table_numbers)
        coverage = 1 - len(missing) / len(source_numbers)
        stats["numeric_coverage"] = round(coverage, 3)
        stats["missing_numbers"] = missing
        if coverage < TABLE_NUMERIC_COVERAGE_MIN:
            stats["fail"] = "missing_numbers"
            return False, stats
        return True, stats

    source_words = set().union(*(word_tokens(text) for text in cell_texts)) if cell_texts else set()
    if source_words:
        table_words = word_tokens(markdown)
        coverage = len(source_words & table_words) / len(source_words)
        stats["word_coverage"] = round(coverage, 3)
        if coverage < TABLE_WORD_COVERAGE_MIN:
            stats["fail"] = "missing_words"
            return False, stats
    return True, stats


def table_candidate_rows(candidates: list[TableCandidate]) -> list[dict[str, Any]]:
    return [asdict(candidate) for candidate in candidates]


def render_layout_overlay(
    *,
    page_image_path: Path,
    items: list[Any],
    page_size: tuple[float, float],
    output_path: Path,
) -> None:
    from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415

    color_map = {
        "table": "#ff5a36",
        "picture": "#2d8cff",
        "title": "#00a86b",
        "section_header": "#00a86b",
        "list_item": "#a855f7",
        "paragraph": "#f0b100",
    }

    image = Image.open(page_image_path).convert("RGB")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    for item in items:
        bbox = bbox_dict(item)
        rect = bbox_to_pixel_rect(bbox, page_size=page_size, image_size=image.size)
        if not rect:
            continue
        kind = item_kind(item)
        color = color_map.get(kind, "#ffffff")
        draw.rectangle(rect, outline=color, width=3)
        label = kind[:24]
        text_box = draw.textbbox((0, 0), label, font=font)
        text_w = text_box[2] - text_box[0]
        text_h = text_box[3] - text_box[1]
        x0, y0, _, _ = rect
        bg_rect = (x0, max(0, y0 - text_h - 4), x0 + text_w + 6, y0)
        draw.rectangle(bg_rect, fill=color)
        draw.text((x0 + 3, bg_rect[1] + 1), label, fill="black", font=font)

    image.save(output_path)


def render_table_candidates_overlay(
    *,
    page_image_path: Path,
    candidates: list[TableCandidate],
    output_path: Path,
) -> None:
    from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415

    image = Image.open(page_image_path).convert("RGB")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    color_map = {
        "docling_table": "#ff5a36",
        "layout_region": "#00d084",
        "picture_table": "#36a3ff",
    }
    for candidate in candidates:
        rect = normalized_rect_to_pixel_rect(candidate.bbox, image.size)
        if not rect:
            continue
        color = color_map.get(candidate.kind, "#ffffff")
        draw.rectangle(rect, outline=color, width=4)
        label = f"{candidate.candidate_id}:{candidate.kind}"
        text_box = draw.textbbox((0, 0), label, font=font)
        text_w = text_box[2] - text_box[0]
        text_h = text_box[3] - text_box[1]
        x0, y0, _, _ = rect
        bg_rect = (x0, max(0, y0 - text_h - 5), x0 + text_w + 6, y0)
        draw.rectangle(bg_rect, fill=color)
        draw.text((x0 + 3, bg_rect[1] + 1), label, fill="black", font=font)
    image.save(output_path)


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


def picture_specialist_prompt(record: PictureRecord, generic_prompt: str) -> str:
    """Select a type-specific extraction contract after visual triage."""

    prompt_by_type = {
        "table": DEFAULT_PICTURE_TABLE_PROMPT,
        "kpi": DEFAULT_PICTURE_TABLE_PROMPT,
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
    for record in records:
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
        if args.skip_picture_triage:
            record.summarize = legacy_summarize
            record.skip_reason = legacy_reason
            record.triage_type = ""
            record.triage_action = "extract" if legacy_summarize else "skip"
            if not legacy_summarize:
                stats["skipped"] += 1
            continue
        if args.skip_vlm:
            record.summarize = False
            record.triage_action = "skip"
            record.skip_reason = "skip_vlm"
            stats["skipped"] += 1
            continue

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
        prompt = DEFAULT_PICTURE_TRIAGE_PROMPT
        answer, usage = ollama_client.call_ollama_vlm(
            base_url=args.ollama_base_url,
            model=args.triage_model,
            prompt=prompt,
            image_path=image_path,
            temperature=0.0,
            num_ctx=args.num_ctx,
            num_predict=args.triage_num_predict,
            auto_num_ctx=args.auto_num_ctx,
        )
        stats["calls"] += 1
        if usage.get("retried"):
            stats["retries"] += 1
        kind, confidence, warnings = parse_picture_triage(answer)
        record.triage_type = kind
        record.triage_confidence = confidence
        record.triage_usage = usage
        record.triage_warnings = warnings
        stats["types"][kind] = stats["types"].get(kind, 0) + 1
        if kind == "decorative" and confidence >= args.triage_confidence:
            record.summarize = False
            record.triage_action = "skip"
            record.skip_reason = "triage_decorative"
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


def item_to_markdown(item: Any, document: Any, picture_records: dict[int, PictureRecord]) -> str:
    if is_picture_item(item):
        # Same placeholder as the Docling serializer path, so the refine
        # prompt's "keep <!-- image --> markers" contract holds either way.
        return "<!-- image -->" if picture_records.get(id(item)) else ""
    if is_table_item(item):
        markdown = export_item_markdown(item, document)
        return markdown or item_text(item)
    text = item_text(item)
    if not text:
        return ""
    if is_heading_item(item):
        level = heading_level(item)
        return f"{'#' * level} {text}"
    if item_kind(item) in {"list_item", "listitem"}:
        return f"- {text}"
    return text


def heading_level(item: Any) -> int:
    level = getattr(item, "level", None)
    try:
        if level:
            level = int(level)
            # Docling's serializer renders a level-1 section_header as "##".
            if item_kind(item) == "section_header":
                return min(max(level + 1, 2), 6)
            return min(max(level, 1), 6)
    except Exception:
        pass
    if "title" in type(item).__name__.lower() or item_kind(item) == "title":
        return 1
    return 2


def export_item_markdown(item: Any, document: Any) -> str:
    if not hasattr(item, "export_to_markdown"):
        return ""
    method = item.export_to_markdown
    for kwargs in ({"doc": document}, {"document": document}, {}):
        try:
            return str(method(**kwargs)).strip()
        except TypeError:
            continue
        except Exception:
            return ""
    return ""


def export_page_markdown_via_docling(document: Any, page_number: int) -> str:
    if not hasattr(document, "export_to_markdown"):
        return ""
    try:
        markdown = document.export_to_markdown(
            page_no=page_number,
            image_placeholder="<!-- image -->",
            compact_tables=False,
            traverse_pictures=False,
        )
    except TypeError:
        try:
            page_doc = document.filter(page_nrs={page_number})
            markdown = page_doc.export_to_markdown(
                image_placeholder="<!-- image -->",
                compact_tables=False,
                traverse_pictures=False,
            )
        except Exception:
            return ""
    except Exception:
        return ""
    return str(markdown).strip() + "\n" if markdown else ""


def export_page_markdown(
    document: Any,
    page_number: int,
    items: list[Any],
    picture_records: dict[int, PictureRecord],
    *,
    use_docling_order: bool = True,
) -> str:
    # Docling's serializer emits its own reading order; when the divider-aware
    # pass reordered the items, serialize from the reordered list instead.
    if use_docling_order:
        docling_markdown = export_page_markdown_via_docling(document, page_number)
        if docling_markdown:
            return docling_markdown

    parts: list[str] = []
    for item in items:
        markdown = item_to_markdown(item, document, picture_records)
        if markdown:
            parts.append(markdown.strip())
    return "\n\n".join(parts).strip() + "\n"


def image_reference(record: PictureRecord) -> str:
    alt = f"Picture p{record.page:04d}-i{record.index:03d}"
    return f"![{alt}]({record.rel_path})"


def insert_image_references_and_summaries(
    markdown: str, records: list[PictureRecord]
) -> str:
    replacements = [image_block(record) for record in records]

    def replace_match(_: re.Match[str]) -> str:
        if not replacements:
            return ""
        return replacements.pop(0)

    out = IMAGE_PLACEHOLDER_RE.sub(replace_match, markdown)
    if replacements:
        out = out.rstrip() + "\n\n" + "\n\n".join(replacements) + "\n"
    return re.sub(r"\n{3,}", "\n\n", out).strip() + "\n"


def image_block(record: PictureRecord) -> str:
    block = image_reference(record)
    if record.summary:
        block += f"\n\n**Image summary:** {record.summary.strip()}"
    return block


SUMMARY_TYPES = {"photo", "table", "chart", "kpi", "infographic", "map", "diagram"}
SUMMARY_TABLE_TYPES = {"table", "kpi"}
SUMMARY_TYPE_RE = re.compile(r"^\s*TYPE:\s*([a-z]+)\s*$", re.IGNORECASE)
PICTURE_CROP_NEIGHBOR_GAP = 0.03
PICTURE_CROP_MARGIN = 0.02


def parse_typed_summary(answer: str) -> tuple[str, str]:
    """Split a typed summary into (type, body); type is "" when missing."""
    lines = answer.strip().splitlines()
    if lines:
        match = SUMMARY_TYPE_RE.match(lines[0])
        if match and match.group(1).lower() in SUMMARY_TYPES:
            return match.group(1).lower(), "\n".join(lines[1:]).strip()
    return "", answer.strip()


def summary_shape_ok(summary_type: str, body: str) -> bool:
    """Deterministic per-type shape check for an image transcription."""
    if not body:
        return False
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
    for record in records:
        if not record.summarize or not record.abs_path:
            continue
        image_path = picture_vlm_image_path(
            record,
            page_image_path=page_image_path,
            page_size=page_size,
            cells=cells,
        )
        if image_path is None:
            record.summary_warnings.append("missing_image")
            continue
        context = {
            "page": record.page,
            "picture": record.index,
            "caption": record.caption,
            "classification": record.classification,
        }
        prompt = picture_specialist_prompt(
            record,
            prompt_template.format(context=json.dumps(context, ensure_ascii=False)),
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
        summary_type, body = parse_typed_summary(
            sp.strip_meta_commentary(ollama_client.strip_markdown_fences(answer))
        )
        record.summary_type = summary_type
        if not summary_shape_ok(summary_type, body) and summary_type in SUMMARY_TYPES:
            # One focused retry with the type-specific contract.
            retry_prompt = (
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
            record.usage = retry_usage
            retry_body = sp.strip_meta_commentary(
                ollama_client.strip_markdown_fences(retry_answer)
            ).strip()
            if summary_shape_ok(summary_type, retry_body):
                body = retry_body
            else:
                body = retry_body if len(retry_body) > len(body) else body
                record.summary_warnings.append("summary_shape_failed")
        if summary_type in SUMMARY_TABLE_TYPES:
            body = normalize_pipe_tables(body)
        record.summary = body.strip()


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


def region_blocks_prompt_json(cells: list[dict[str, Any]]) -> str:
    payload = [
        {
            "id": cell["id"],
            "bbox": cell["rect"],
            "text": truncate_prompt_text(cell["text"], 300),
        }
        for cell in cells
    ]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def transcribe_table_candidates(
    *,
    candidates: list[TableCandidate],
    cells: list[dict[str, Any]],
    page_image_path: Path,
    page_dir: Path,
    args: argparse.Namespace,
) -> None:
    """Crop each accepted layout region, transcribe it with the VLM using the
    region's cell texts as authoritative content, and verify deterministically.

    Failed or skipped transcriptions leave the candidate unverified; the page
    then flows through the pipeline exactly as it would without a candidate.
    """
    cells_by_id = {cell["id"]: cell for cell in cells}
    table_dir = page_dir / "table_candidates"
    if table_dir.exists():
        shutil.rmtree(table_dir)
    for candidate in candidates:
        if candidate.kind != "layout_region":
            continue
        region_cells = [
            cells_by_id[block_id]
            for block_id in candidate.source_block_ids
            if block_id in cells_by_id
        ]
        if not region_cells:
            candidate.warnings.append("missing_region_cells")
            continue
        crop_path = table_dir / f"{candidate.candidate_id}_crop.png"
        if not save_region_crop(
            page_image_path=page_image_path, bbox=candidate.bbox, crop_path=crop_path
        ):
            candidate.warnings.append("crop_failed")
            continue
        candidate.crop_path = str(crop_path)
        if args.skip_vlm:
            continue

        cell_texts = [cell["text"] for cell in region_cells]
        prompt = DEFAULT_TABLE_REGION_PROMPT.format(
            region_blocks=region_blocks_prompt_json(region_cells)
        )
        for attempt in range(2):
            answer, usage = ollama_client.call_ollama_vlm(
                base_url=args.ollama_base_url,
                model=args.ollama_model,
                prompt=prompt,
                image_path=crop_path,
                temperature=args.temperature,
                num_ctx=args.num_ctx,
                num_predict=args.num_predict,
                auto_num_ctx=args.auto_num_ctx,
            )
            markdown = sp.strip_meta_commentary(ollama_client.strip_markdown_fences(answer))
            candidate.usage = usage
            if collapse_ws(markdown).upper() == "SKIP":
                candidate.warnings.append("vlm_skip")
                break
            markdown = normalize_pipe_tables(markdown)
            ok, verify_stats = verify_region_table(markdown, cell_texts)
            candidate.stats = {**(candidate.stats or {}), "verify": verify_stats}
            if ok:
                candidate.markdown = markdown
                candidate.verified = True
                (table_dir / f"{candidate.candidate_id}_table.md").write_text(
                    markdown.rstrip() + "\n", encoding="utf-8"
                )
                break
            candidate.markdown = markdown
            (table_dir / f"{candidate.candidate_id}_rejected.md").write_text(
                markdown.rstrip() + "\n", encoding="utf-8"
            )
            if attempt == 0:
                feedback: list[str] = []
                if verify_stats.get("invented_numbers"):
                    feedback.append(
                        "it contained numbers that are not in the blocks: "
                        + ", ".join(verify_stats["invented_numbers"][:10])
                    )
                if verify_stats.get("missing_numbers"):
                    feedback.append(
                        "it dropped these numbers from the blocks: "
                        + ", ".join(verify_stats["missing_numbers"][:10])
                    )
                if not feedback:
                    feedback.append("it was not a well-formed markdown table")
                prompt = (
                    prompt.rstrip()
                    + "\n\nYour previous attempt was rejected because "
                    + "; ".join(feedback)
                    + ". Transcribe the region again using every block text exactly once."
                )
            else:
                candidate.warnings.append("verification_failed")


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
) -> tuple[str, dict[str, Any]]:
    final = sp.flatten_html_tables(working_markdown)
    final = sp.normalize_bullets_and_headings(final)
    final = sp.demote_datapoint_headings(final)
    final = insert_image_references_and_summaries(final, records)
    final = sp.strip_meta_commentary(final)
    final = sp.normalize_footnotes(final)
    final = normalize_pipe_tables(final)

    warnings: dict[str, Any] = {}
    final, dropped_tables = drop_duplicate_subset_tables(final, table_candidates)
    if dropped_tables:
        warnings["duplicate_tables_dropped"] = dropped_tables
    final, guard_warnings = apply_completeness_guard(source_markdown, final)
    warnings.update(guard_warnings)
    footnote_warnings = sp.footnote_consistency(final)
    if footnote_warnings:
        warnings["footnotes"] = footnote_warnings
    meta_warnings = sp.meta_commentary_warnings(final)
    if meta_warnings:
        warnings["meta_commentary"] = meta_warnings
    return final, warnings


VALUE_ONLY_RE = re.compile(
    r"^\s*(?:[<>~]?\s*)?(?:\+|-)?(?:\d[\d,.\s]*)(?:%|pts?|bn|m|k|yo|yo\.|€|\$|£|cumulated)?\s*$",
    re.IGNORECASE,
)


def standalone_value_line_count(markdown: str) -> int:
    count = 0
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        if VALUE_ONLY_RE.match(line):
            count += 1
    return count


def normalize_pipe_tables(markdown: str) -> str:
    """Deterministically repair pipe tables the model emitted malformed.

    Fixes separator rows whose column count differs from the data rows
    (which breaks rendering), inserts a missing separator after the header,
    drops stray extra separators, and pads ragged rows to the table width.
    Cell contents are never changed.
    """
    lines = markdown.splitlines()
    out: list[str] = []
    block: list[str] = []

    def flush() -> None:
        if not block:
            return
        rows = [
            [cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in block
            if not set(line.strip()) <= set("|-: ")
        ]
        if len(rows) < 2:
            out.extend(block)
            block.clear()
            return
        width = max(len(row) for row in rows)
        padded = [row + [""] * (width - len(row)) for row in rows]
        out.append("| " + " | ".join(padded[0]) + " |")
        out.append("|" + "---|" * width)
        out.extend("| " + " | ".join(row) + " |" for row in padded[1:])
        block.clear()

    for line in lines:
        if line.strip().startswith("|"):
            block.append(line)
        else:
            flush()
            out.append(line)
    flush()
    return "\n".join(out) + ("\n" if markdown.endswith("\n") else "")


def _pipe_table_spans(
    lines: list[str],
) -> list[tuple[int, int, list[tuple[str, ...]]]]:
    """(start, end_exclusive, data_rows) for each pipe table; rows normalized."""
    spans: list[tuple[int, int, list[tuple[str, ...]]]] = []
    start: int | None = None
    rows: list[tuple[str, ...]] = []
    for index, line in enumerate(lines + [""]):
        stripped = line.strip()
        if stripped.startswith("|"):
            if start is None:
                start = index
            if not set(stripped) <= set("|-: "):
                cells = tuple(
                    collapse_ws(cell).lower() for cell in stripped.strip("|").split("|")
                )
                rows.append(cells)
        elif start is not None:
            spans.append((start, index, rows))
            start, rows = None, []
    return spans


def _rows_prefix_subset(
    subset_rows: list[tuple[str, ...]], superset_rows: list[tuple[str, ...]]
) -> bool:
    """True when (nearly) every subset row is a column-prefix of a superset row."""
    if len(subset_rows) < 2:
        return False
    hits = sum(
        1
        for row in subset_rows
        if any(row == other[: len(row)] for other in superset_rows)
    )
    return hits >= 0.8 * len(subset_rows)


def drop_duplicate_subset_tables(
    markdown: str, table_candidates: list[TableCandidate] | None
) -> tuple[str, int]:
    """Remove pipe tables that duplicate a verified table with fewer columns/rows.

    The refine model sometimes serializes a region itself AND places the
    injected pre-verified table, yielding e.g. a GOALS|TARGETS twin of the
    GOALS|TARGETS|RESULTS table. Only tables that are column-prefix subsets of
    a verified-backed table are removed, so unrelated tables are never touched.
    """
    verified_rowsets = [
        set(rows)
        for candidate in table_candidates or []
        if candidate.verified and candidate.markdown
        for rows in [
            [
                row
                for _, _, table_rows in _pipe_table_spans(candidate.markdown.splitlines())
                for row in table_rows
            ]
        ]
        if rows
    ]
    if not verified_rowsets:
        return markdown, 0

    lines = markdown.splitlines()
    tables = _pipe_table_spans(lines)
    if len(tables) < 2:
        return markdown, 0

    def is_backed(rows: list[tuple[str, ...]]) -> bool:
        row_set = set(rows)
        return any(
            len(row_set & verified_rows) >= 0.5 * len(verified_rows)
            for verified_rows in verified_rowsets
        )

    backed = [is_backed(rows) for _, _, rows in tables]
    drop: set[int] = set()
    for i, (_, _, rows_i) in enumerate(tables):
        if i in drop:
            continue
        for j, (_, _, rows_j) in enumerate(tables):
            if i == j or j in drop or not backed[j]:
                continue
            if _rows_prefix_subset(rows_i, rows_j):
                drop.add(i)
                break
    if not drop:
        return markdown, 0

    dropped_lines = {
        index for table_index in drop for index in range(*tables[table_index][:2])
    }
    kept = "\n".join(
        line for index, line in enumerate(lines) if index not in dropped_lines
    )
    return re.sub(r"\n{3,}", "\n\n", kept).strip() + "\n", len(drop)


def missing_verified_table_ids(
    current_markdown: str, table_candidates: list[TableCandidate]
) -> list[str]:
    """Ids of verified tables whose rows mostly did not survive into the markdown."""
    present_rows = {
        collapse_ws(line)
        for line in current_markdown.splitlines()
        if line.strip().startswith("|")
    }
    out: list[str] = []
    for candidate in table_candidates:
        if not (candidate.verified and candidate.markdown):
            continue
        rows = [
            collapse_ws(line)
            for line in candidate.markdown.splitlines()
            if line.strip().startswith("|") and not set(line.strip()) <= set("|-: ")
        ]
        if not rows:
            continue
        hits = sum(1 for row in rows if row in present_rows)
        if hits < 0.5 * len(rows):
            out.append(candidate.candidate_id)
    return out


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


def pipe_row_count(markdown: str) -> int:
    return sum(
        1
        for line in markdown.splitlines()
        if line.strip().startswith("|") and not set(line.strip()) <= set("|-: ")
    )


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
    return reasons


def apply_completeness_guard(
    raw_markdown: str, final_markdown: str
) -> tuple[str, dict[str, Any]]:
    warnings: dict[str, Any] = {}
    raw_for_diff = IMAGE_PLACEHOLDER_RE.sub("", raw_markdown)
    final_for_diff = IMAGE_PLACEHOLDER_RE.sub("", final_markdown)
    missing = sp.completeness_diff(raw_for_diff, final_for_diff)
    if missing:
        final = sp.merge_unplaced_content(final_markdown, missing)
        warnings["content_loss_guard_triggered"] = True
        warnings["unplaced_content_lines"] = missing
    else:
        final = final_markdown
        warnings["content_loss_guard_triggered"] = False
    return final, warnings


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


def assert_docling_export_surface(document: Any) -> None:
    if not hasattr(document, "iterate_items"):
        raise RuntimeError("Docling document does not expose iterate_items(); cannot export pages.")
    if hasattr(document, "export_to_markdown"):
        try:
            inspect.signature(document.export_to_markdown)
        except Exception:
            pass


def _picture_state_rows(records: list[PictureRecord], page_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        row = asdict(record)
        row["abs_path"] = relative_page_path(record.abs_path, page_dir)
        rows.append(row)
    return rows


def _records_from_state(state: PageState) -> list[PictureRecord]:
    fields = set(PictureRecord.__dataclass_fields__)
    records: list[PictureRecord] = []
    page_dir = Path(state.page_dir)
    for row in state.picture_records:
        payload = {key: value for key, value in row.items() if key in fields}
        payload["abs_path"] = resolve_page_path(payload.get("abs_path"), page_dir)
        payload.setdefault("triage_eligible", False)
        payload.setdefault("triage_type", "")
        payload.setdefault("triage_confidence", None)
        payload.setdefault("triage_action", "")
        payload.setdefault("triage_warnings", [])
        payload.setdefault("triage_usage", None)
        records.append(PictureRecord(**payload))
    return records


def _candidate_state_rows(candidates: list[TableCandidate], page_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        row = asdict(candidate)
        row["crop_path"] = relative_page_path(candidate.crop_path, page_dir)
        rows.append(row)
    return rows


def _candidates_from_state(state: PageState) -> list[TableCandidate]:
    fields = set(TableCandidate.__dataclass_fields__)
    page_dir = Path(state.page_dir)
    candidates: list[TableCandidate] = []
    for row in state.table_candidates:
        payload = {key: value for key, value in row.items() if key in fields}
        payload["crop_path"] = resolve_page_path(payload.get("crop_path"), page_dir)
        candidates.append(TableCandidate(**payload))
    return candidates


def _sync_page_state(
    state: PageState,
    *,
    records: list[PictureRecord] | None = None,
    detection_cells: list[dict[str, Any]] | None = None,
    layout_map: dict[str, Any] | None = None,
    repair_layout_map: dict[str, Any] | None = None,
    candidates: list[TableCandidate] | None = None,
) -> None:
    page_dir = Path(state.page_dir)
    if records is not None:
        state.picture_records = _picture_state_rows(records, page_dir)
    if detection_cells is not None:
        state.detection_cells = detection_cells
    if layout_map is not None:
        state.layout_map = layout_map
    if repair_layout_map is not None:
        state.repair_layout_map = repair_layout_map
    if candidates is not None:
        state.table_candidates = _candidate_state_rows(candidates, page_dir)
    state.artifact_paths.update(
        {
            "page_image": "page.png",
            "layout_overlay": "page_layout_overlay.png",
            "raw_markdown": "docling_raw.md",
            "layout_map": "layout_prompt_map.json",
            "repair_layout_map": "layout_prompt_map_repair.json",
            "table_candidates": "table_candidates.json",
            "table_overlay": "table_candidates_overlay.png",
            "final_markdown": "docling_final.md",
            "image_summaries": "image_summaries.jsonl",
            "checkpoint": "page_state.json",
        }
    )


def _execute_stage(
    state: PageState,
    reporter: StatusReporter,
    stage: str,
    action: Any,
) -> dict[str, Any]:
    started = reporter.start(stage)
    state.status = "running"
    try:
        details = action() or {}
    except Exception as error:  # noqa: BLE001
        elapsed = reporter.fail(stage, started, error)
        state.status = "failed"
        state.failure = {
            "stage": stage,
            "error_type": type(error).__name__,
            "error_message": str(error),
        }
        state.stage_history.append(
            StageRecord(
                stage=stage,
                status="failed",
                elapsed_seconds=elapsed,
                error_type=type(error).__name__,
                error_message=str(error),
            )
        )
        state.save()
        raise
    elapsed = reporter.ok(stage, started, **details)
    state.stage_history.append(
        StageRecord(stage=stage, status="ok", elapsed_seconds=elapsed, details=details)
    )
    state.completed_stage = stage
    state.save()
    return details


def _skip_stage(state: PageState, reporter: StatusReporter, stage: str, reason: str) -> None:
    reporter.skip(stage, reason)
    state.stage_history.append(StageRecord(stage=stage, status="skipped", details={"reason": reason}))
    state.completed_stage = stage
    state.save()


def _prepare_page(
    *,
    state: PageState,
    reporter: StatusReporter,
    args: argparse.Namespace,
    pdf_doc: Any,
    converter: Any,
    page_size: tuple[float, float],
) -> dict[str, Any]:
    runtime: dict[str, Any] = {}
    page_dir = Path(state.page_dir)

    def action() -> dict[str, Any]:
        document = convert_pdf(converter, Path(args.pdf), [state.page])
        assert_docling_export_surface(document)
        rasterize_page(pdf_doc, state.page, args.dpi, page_dir / "page.png")
        items = list(iter_doc_items(document, state.page))
        if args.no_divider_reorder:
            reading_order_info: dict[str, Any] = {"applied": False, "disabled": True}
        else:
            dividers = extract_divider_segments(
                pdf_doc.load_page(state.page - 1), page_size
            )
            items, reading_order_info = reorder_items_for_reading_order(
                items, page_size, dividers
            )
        render_layout_overlay(
            page_image_path=page_dir / "page.png",
            items=items,
            page_size=page_size,
            output_path=page_dir / "page_layout_overlay.png",
        )
        picture_map = save_picture_records(
            document=document,
            items=items,
            page_number=state.page,
            page_dir=page_dir,
            page_size=page_size,
        )
        records = list(picture_map.values())
        detection_cells = detection_cells_from_items(items, page_size)
        raw_markdown = export_page_markdown(
            document,
            state.page,
            items,
            picture_map,
            use_docling_order=not reading_order_info["applied"],
        )
        (page_dir / "docling_raw.md").write_text(raw_markdown, encoding="utf-8")
        layout_prompt_map = build_layout_prompt_map(
            items=items,
            page_size=page_size,
            picture_records=picture_map,
        )
        (page_dir / "layout_prompt_map.json").write_text(
            json.dumps(layout_prompt_map, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        if not detection_cells:
            detection_cells = detection_cells_from_layout_map(layout_prompt_map)

        state.reading_order = reading_order_info
        state.has_docling_table = any(is_table_item(item) for item in items)
        state.raw_markdown = raw_markdown
        state.block_rows = block_rows_for_page(items, state.page, picture_map)
        _sync_page_state(
            state,
            records=records,
            detection_cells=detection_cells,
            layout_map=layout_prompt_map,
        )
        runtime.update(
            {
                "document": document,
                "items": items,
                "records": records,
                "detection_cells": detection_cells,
                "layout_map": layout_prompt_map,
            }
        )
        return {
            "items": len(items),
            "pictures": len(records),
            "text_blocks": len(detection_cells),
            "reordered": reading_order_info.get("moved_items", 0),
        }

    _execute_stage(state, reporter, "prepare", action)
    return runtime


def _run_picture_triage(
    *, state: PageState, reporter: StatusReporter, args: argparse.Namespace, runtime: dict[str, Any]
) -> list[PictureRecord]:
    records = runtime.get("records") or _records_from_state(state)

    def action() -> dict[str, Any]:
        stats = triage_pictures(
            records=records,
            args=args,
            page_image_path=Path(state.page_dir) / "page.png",
            page_size=tuple(state.page_size),
            cells=state.detection_cells,
        )
        runtime["records"] = records
        _sync_page_state(state, records=records)
        return stats

    _execute_stage(state, reporter, "picture_triage", action)
    return records


def _run_picture_extract(
    *,
    state: PageState,
    reporter: StatusReporter,
    args: argparse.Namespace,
    runtime: dict[str, Any],
    prompt: str,
) -> list[PictureRecord]:
    records = runtime.get("records") or _records_from_state(state)

    def action() -> dict[str, Any]:
        summarize_pictures(
            records=records,
            prompt_template=prompt,
            args=args,
            page_image_path=Path(state.page_dir) / "page.png",
            page_size=tuple(state.page_size),
            cells=state.detection_cells,
        )
        runtime["records"] = records
        _sync_page_state(state, records=records)
        calls = sum(record.usage is not None for record in records)
        retries = sum(bool((record.usage or {}).get("retried")) for record in records)
        return {"calls": calls, "retries": retries, "summaries": sum(bool(r.summary) for r in records)}

    _execute_stage(state, reporter, "picture_extract", action)
    return records


def _run_table_detect(
    *, state: PageState, reporter: StatusReporter, runtime: dict[str, Any]
) -> list[TableCandidate]:
    records = runtime.get("records") or _records_from_state(state)
    page_dir = Path(state.page_dir)

    def action() -> dict[str, Any]:
        picture_map = {record.index: record for record in records}
        candidates = build_table_candidates(
            cells=state.detection_cells,
            page_size=tuple(state.page_size),
            picture_records=picture_map,
            layout_map=state.layout_map,
        )
        runtime["candidates"] = candidates
        _sync_page_state(state, candidates=candidates)
        (page_dir / "table_candidates.json").write_text(
            json.dumps(table_candidate_rows(candidates), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        render_table_candidates_overlay(
            page_image_path=page_dir / "page.png",
            candidates=candidates,
            output_path=page_dir / "table_candidates_overlay.png",
        )
        return {
            "candidates": len(candidates),
            "kinds": sorted({candidate.kind for candidate in candidates}),
        }

    _execute_stage(state, reporter, "table_detect", action)
    return runtime["candidates"]


def _run_table_extract(
    *, state: PageState, reporter: StatusReporter, args: argparse.Namespace, runtime: dict[str, Any]
) -> list[TableCandidate]:
    candidates = runtime.get("candidates") or _candidates_from_state(state)

    def action() -> dict[str, Any]:
        transcribe_table_candidates(
            candidates=candidates,
            cells=state.detection_cells,
            page_image_path=Path(state.page_dir) / "page.png",
            page_dir=Path(state.page_dir),
            args=args,
        )
        runtime["candidates"] = candidates
        _sync_page_state(state, candidates=candidates)
        Path(state.page_dir, "table_candidates.json").write_text(
            json.dumps(table_candidate_rows(candidates), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return {
            "candidates": len(candidates),
            "verified": sum(candidate.verified for candidate in candidates),
            "calls": sum(candidate.usage is not None for candidate in candidates),
        }

    _execute_stage(state, reporter, "table_extract", action)
    return candidates


def _run_page_refine(
    *,
    state: PageState,
    reporter: StatusReporter,
    args: argparse.Namespace,
    runtime: dict[str, Any],
    prompt: str,
) -> None:
    records = runtime.get("records") or _records_from_state(state)
    candidates = runtime.get("candidates") or _candidates_from_state(state)
    page_dir = Path(state.page_dir)

    def action() -> dict[str, Any]:
        refined, usage = refine_page_markdown(
            source_markdown=state.raw_markdown,
            layout_blocks=state.layout_map,
            table_candidates=candidates,
            page_image_path=page_dir / "page.png",
            prompt_template=prompt,
            args=args,
        )
        state.refined_markdown = refined
        state.page_vlm_usage = usage
        if usage:
            (page_dir / "page_vlm.md").write_text(refined, encoding="utf-8")
        final_markdown, warnings = postprocess_markdown(
            state.raw_markdown, refined, records, candidates
        )
        missing_tables = missing_verified_table_ids(final_markdown, candidates)
        if missing_tables:
            warnings["verified_tables_missing"] = missing_tables
        state.final_markdown = final_markdown
        state.warnings = warnings
        _sync_page_state(state, records=records, candidates=candidates)
        return {
            "calls": 1 if usage else 0,
            "tokens": (usage or {}).get("total_tokens"),
            "warnings": len(warnings),
        }

    _execute_stage(state, reporter, "page_refine", action)


def _run_page_repair(
    *,
    state: PageState,
    reporter: StatusReporter,
    args: argparse.Namespace,
    runtime: dict[str, Any],
    prompt: str,
) -> None:
    records = runtime.get("records") or _records_from_state(state)
    candidates = runtime.get("candidates") or _candidates_from_state(state)
    page_dir = Path(state.page_dir)
    items = runtime.get("items")
    has_runtime_items = items is not None
    items_for_check = items or []
    should_repair = should_run_repair_pass(
        items=items_for_check,
        warnings=state.warnings,
        current_markdown=state.final_markdown,
        records=records,
        table_candidates=candidates,
        has_docling_table=state.has_docling_table if not has_runtime_items else None,
    )
    if not should_repair:
        state.page_repair_usage = None
        _skip_stage(state, reporter, "page_repair", "no_trigger")
        return

    def action() -> dict[str, Any]:
        if has_runtime_items:
            picture_map = {record.index: record for record in records}
            repair_layout_map = build_layout_prompt_map(
                items=items,
                page_size=tuple(state.page_size),
                picture_records=picture_map,
                unplaced_lines=state.warnings.get("unplaced_content_lines", []),
            )
        else:
            repair_layout_map = state.layout_map
        state.repair_layout_map = repair_layout_map
        (page_dir / "layout_prompt_map_repair.json").write_text(
            json.dumps(repair_layout_map, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        repaired, usage = repair_page_markdown(
            current_markdown=state.final_markdown,
            layout_blocks=repair_layout_map,
            table_candidates=candidates,
            page_image_path=page_dir / "page.png",
            unplaced_lines=state.warnings.get("unplaced_content_lines", []),
            prompt_template=prompt,
            args=args,
        )
        state.page_repair_usage = usage
        if not usage:
            return {"calls": 0, "reason": "skip_vlm"}
        (page_dir / "page_repair.md").write_text(repaired, encoding="utf-8")
        repaired_final, repaired_warnings = postprocess_markdown(
            state.raw_markdown, repaired, records, candidates
        )
        reject_reasons = repair_regression_reasons(
            state.final_markdown, repaired_final, usage
        )
        if reject_reasons:
            state.warnings["repair_rejected"] = reject_reasons
        else:
            state.final_markdown = repaired_final
            state.warnings = repaired_warnings
            missing_tables = missing_verified_table_ids(state.final_markdown, candidates)
            if missing_tables:
                state.warnings["verified_tables_missing"] = missing_tables
        _sync_page_state(state, records=records, candidates=candidates, repair_layout_map=repair_layout_map)
        return {
            "calls": 1,
            "accepted": not reject_reasons,
            "tokens": usage.get("total_tokens"),
            "warnings": len(state.warnings),
        }

    _execute_stage(state, reporter, "page_repair", action)


def _run_finalize(
    *, state: PageState, reporter: StatusReporter, runtime: dict[str, Any]
) -> None:
    records = runtime.get("records") or _records_from_state(state)
    page_dir = Path(state.page_dir)

    def action() -> dict[str, Any]:
        (page_dir / "docling_final.md").write_text(state.final_markdown, encoding="utf-8")
        write_image_summaries(page_dir / "image_summaries.jsonl", records)
        _sync_page_state(state, records=records)
        return {"warnings": len(state.warnings), "pictures": len(records)}

    _execute_stage(state, reporter, "finalize", action)
    state.status = "completed"
    state.save()


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


def main() -> int:
    import fitz  # noqa: PLC0415

    args = parse_args()
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


if __name__ == "__main__":
    raise SystemExit(main())
