"""Prompt resources and the validated prompt contracts used by the pipeline."""

from __future__ import annotations

from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent / "resources" / "prompts"

def load_prompt(name: str) -> str:
    path = PROMPT_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Missing packaged prompt: {path}")
    return path.read_text(encoding="utf-8")

def require_placeholders(template: str, *names: str) -> str:
    missing = [name for name in names if "{" + name + "}" not in template]
    if missing:
        raise ValueError(f"Prompt is missing placeholders: {', '.join(missing)}")
    return template

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

# Resource files are authoritative; these names preserve the existing prompt
# constants while making packaged prompt edits visible to every caller.
DEFAULT_IMAGE_SUMMARY_PROMPT = load_prompt("picture_generic.md")
DEFAULT_PICTURE_TABLE_PROMPT = load_prompt("picture_table.md")
DEFAULT_PICTURE_TRIAGE_PROMPT = load_prompt("picture_triage.md")
DEFAULT_PICTURE_CHART_PROMPT = load_prompt("picture_chart.md")
DEFAULT_PICTURE_GROUPED_VALUES_PROMPT = load_prompt("picture_grouped_values.md")
DEFAULT_PICTURE_PHOTO_PROMPT = load_prompt("picture_photo.md")
DEFAULT_PICTURE_SYMBOL_PROMPT = load_prompt("picture_symbol.md")
DEFAULT_PAGE_REFINEMENT_PROMPT = load_prompt("page_refinement.md")
DEFAULT_PAGE_REPAIR_PROMPT = load_prompt("page_repair.md")
DEFAULT_TABLE_REGION_PROMPT = load_prompt("table_region.md")
DEFAULT_KPI_PANEL_PROMPT = load_prompt("kpi_panel.md")

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

__all__ = [
    "DEFAULT_IMAGE_SUMMARY_PROMPT",
    "DEFAULT_PICTURE_TABLE_PROMPT",
    "DEFAULT_PICTURE_VALUES_PROMPT",
    "DEFAULT_PICTURE_TRIAGE_PROMPT",
    "DEFAULT_PICTURE_CHART_PROMPT",
    "DEFAULT_PICTURE_GROUPED_VALUES_PROMPT",
    "DEFAULT_PICTURE_PHOTO_PROMPT",
    "DEFAULT_PICTURE_SYMBOL_PROMPT",
    "DEFAULT_PAGE_REFINEMENT_PROMPT",
    "DEFAULT_PAGE_REPAIR_PROMPT",
    "DEFAULT_TABLE_REGION_PROMPT",
    "DEFAULT_KPI_PANEL_PROMPT",
    "DEFAULT_OLLAMA_MODEL",
    "load_prompt",
    "require_placeholders",
    "load_summary_prompt",
    "load_page_refinement_prompt",
    "load_page_repair_prompt",
]
