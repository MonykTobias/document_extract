"""Picture triage and VLM extraction compatibility surface."""

from .legacy_pipeline import (
    DEFAULT_IMAGE_SUMMARY_PROMPT,
    parse_picture_triage,
    picture_specialist_prompt,
    should_summarize_picture,
    should_visual_triage_picture,
    summarize_pictures,
    triage_pictures,
)

__all__ = [
    "DEFAULT_IMAGE_SUMMARY_PROMPT",
    "parse_picture_triage",
    "picture_specialist_prompt",
    "should_summarize_picture",
    "should_visual_triage_picture",
    "summarize_pictures",
    "triage_pictures",
]

