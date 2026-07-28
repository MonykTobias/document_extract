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


def assert_formattable(template: str, *names: str) -> str:
    """Fail now if ``str.format`` would fail when the prompt is rendered.

    Prompts are rendered with ``str.format``, so a literal brace -- a JSON
    example is the obvious case -- raises KeyError once per page, after the
    expensive Docling conversion has already run. Rendering with dummy values
    here turns that into one actionable error before any work starts.
    """
    try:
        template.format(**{name: "" for name in names})
    except (KeyError, IndexError, ValueError) as error:
        raise ValueError(
            f"Prompt template has an unescaped brace ({error}). "
            "Literal { and } must be written as {{ and }}."
        ) from error
    return template


DEFAULT_OLLAMA_MODEL = "hf.co/unsloth/Qwen3-VL-8B-Instruct-GGUF:Q4_K_M"

# Resource files are authoritative; these names preserve the existing prompt
# constants while making packaged prompt edits visible to every caller.
DEFAULT_IMAGE_SUMMARY_PROMPT = load_prompt("picture_generic.md")
DEFAULT_PICTURE_TABLE_PROMPT = load_prompt("picture_table.md")
DEFAULT_PICTURE_VALUES_PROMPT = load_prompt("picture_values.md")
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
    """Load the picture-summary prompt, optionally from a caller-supplied file.

    No shipped entry point passes a path -- there is no CLI flag for it and the
    pipeline calls this with ``None`` -- but the function is a public export, so
    the parameter stays for external callers. Wiring a ``--summary-prompt-file``
    flag would be a feature, and it would need the same brace validation
    ``assert_formattable`` applies to the refinement prompts.
    """
    if path is None:
        return require_placeholders(load_prompt("picture_generic.md"), "context")
    template = path.expanduser().resolve().read_text(encoding="utf-8")
    if "{context}" not in template:
        template = template.rstrip() + "\n\nContext:\n{context}\n"
    return assert_formattable(template, "context")


_REFINEMENT_PLACEHOLDERS = ("source_markdown", "layout_blocks", "verified_tables")
_REPAIR_PLACEHOLDERS = (
    "current_markdown",
    "layout_blocks",
    "verified_tables",
    "unplaced_lines",
)


def load_page_refinement_prompt(path: Path | None) -> str:
    if path is None:
        return assert_formattable(
            require_placeholders(
                load_prompt("page_refinement.md"), *_REFINEMENT_PLACEHOLDERS
            ),
            *_REFINEMENT_PLACEHOLDERS,
        )
    template = path.expanduser().resolve().read_text(encoding="utf-8")
    if "{source_markdown}" not in template:
        raise ValueError("Page refinement prompt must contain {source_markdown}.")
    if "{layout_blocks}" not in template:
        template = template.rstrip() + "\n\nCompact layout block map:\n{layout_blocks}\n"
    if "{verified_tables}" not in template:
        template = template.rstrip() + "\n\nPre-verified tables:\n{verified_tables}\n"
    return assert_formattable(template, *_REFINEMENT_PLACEHOLDERS)


def load_page_repair_prompt() -> str:
    return assert_formattable(
        require_placeholders(load_prompt("page_repair.md"), *_REPAIR_PLACEHOLDERS),
        *_REPAIR_PLACEHOLDERS,
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
    "assert_formattable",
    "load_summary_prompt",
    "load_page_refinement_prompt",
    "load_page_repair_prompt",
]
