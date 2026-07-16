"""Stable data records shared by the document extraction stages."""

from dataclasses import dataclass, field
from typing import Any

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
    # Normalized [left, top, right, bottom] rect (top-left origin); lets later
    # stages test band membership without the page size. None when unavailable.
    norm_rect: list[float] | None = None
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
    summary_redundant: bool = False
    usage: dict[str, Any] | None = None
    embedded_in: str = ""
    embed_overlap_ratio: float = 0.0


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

    def has_complete_symbol_geometry(self) -> bool:
        """Whether geometry has accounted for every expected table-symbol picture.

        Older checkpoints predate the placed-index audit.  They retain the
        historical authority behaviour unless they already carry an explicit
        geometry deficit; newly rendered candidates always carry both lists.
        """
        stats = self.stats or {}
        if stats.get("symbols_unplaced_geometry"):
            return False
        expected = stats.get("symbol_picture_indices")
        placed = stats.get("symbols_placed_geometry")
        if not expected or placed is None:
            return True
        return set(expected).issubset(set(placed))

__all__ = ["PictureRecord", "TableCandidate"]
