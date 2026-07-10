"""Table detection and verification compatibility surface."""

from .legacy_pipeline import (
    build_table_candidates,
    evaluate_table_regions,
    transcribe_table_candidates,
    verify_region_table,
)

__all__ = [
    "build_table_candidates",
    "evaluate_table_regions",
    "transcribe_table_candidates",
    "verify_region_table",
]

