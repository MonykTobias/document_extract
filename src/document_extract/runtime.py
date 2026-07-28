"""Runtime primitives for checkpointing and reporting page-stage progress."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_FILENAME = "page_state.json"

STAGES = (
    "prepare",
    "picture_triage",
    "picture_extract",
    "table_detect",
    "table_extract",
    "page_refine",
    "page_repair",
    "finalize",
)

REPLAY_STAGES = tuple(stage for stage in STAGES if stage != "prepare")


@dataclass
class StageRecord:
    """One completed or failed attempt of a page stage."""

    stage: str
    status: str
    elapsed_seconds: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)
    error_type: str | None = None
    error_message: str | None = None
    recorded_at: float = field(default_factory=time.time)


@dataclass
class PageState:
    """Serializable state needed to continue processing one page.

    Docling's document object and item instances are deliberately excluded:
    they are runtime objects and are not stable checkpoint formats.  All later
    stages use the serialized cells, layout map, Markdown, and image paths.
    """

    schema_version: int
    pdf_name: str
    pdf_size: int
    page: int
    page_index: int
    total_pages: int
    dpi: int
    page_dir: str
    page_size: tuple[float, float]
    status: str = "new"
    completed_stage: str | None = None
    reading_order: dict[str, Any] = field(default_factory=dict)
    has_docling_table: bool = False
    # Normalized furniture texts detected for this page (running headers,
    # footers, chapter tabs); optional so old checkpoints stay loadable.
    furniture_texts: list[str] = field(default_factory=list)
    # "toc" for table-of-contents pages, "" otherwise (additive field).
    page_role: str = ""
    picture_records: list[dict[str, Any]] = field(default_factory=list)
    detection_cells: list[dict[str, Any]] = field(default_factory=list)
    raw_markdown: str = ""
    refined_markdown: str = ""
    final_markdown: str = ""
    # Post-refine snapshot: page_repair overwrites final_markdown/warnings in
    # place, so replaying it needs the pre-repair values preserved separately.
    pre_repair_markdown: str = ""
    pre_repair_warnings: dict[str, Any] = field(default_factory=dict)
    layout_map: dict[str, Any] = field(default_factory=dict)
    repair_layout_map: dict[str, Any] = field(default_factory=dict)
    table_candidates: list[dict[str, Any]] = field(default_factory=list)
    visual_values_mode: str = "off"
    visual_candidates: list[dict[str, Any]] = field(default_factory=list)
    visual_audit: dict[str, Any] = field(default_factory=dict)
    block_rows: list[dict[str, Any]] = field(default_factory=list)
    page_vlm_usage: dict[str, Any] | None = None
    page_repair_usage: dict[str, Any] | None = None
    warnings: dict[str, Any] = field(default_factory=dict)
    artifact_paths: dict[str, str] = field(default_factory=dict)
    stage_history: list[StageRecord] = field(default_factory=list)
    failure: dict[str, Any] | None = None

    @property
    def checkpoint_path(self) -> Path:
        return Path(self.page_dir) / CHECKPOINT_FILENAME

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["stage_history"] = [asdict(record) for record in self.stage_history]
        return payload

    def save(self) -> Path:
        path = self.checkpoint_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "PageState":
        payload = json.loads(path.read_text(encoding="utf-8"))
        # Check the version before constructing: a checkpoint from a newer
        # schema adds fields, so the dataclass call would raise TypeError for an
        # unexpected keyword and this guard could never report the real reason.
        version = payload.get("schema_version")
        if version != CHECKPOINT_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported page checkpoint schema {version}; "
                f"expected {CHECKPOINT_SCHEMA_VERSION}: {path}"
            )
        history = [StageRecord(**entry) for entry in payload.pop("stage_history", [])]
        return cls(stage_history=history, **payload)


def new_page_state(
    *,
    pdf_path: Path,
    page: int,
    page_index: int,
    total_pages: int,
    dpi: int,
    page_dir: Path,
    page_size: tuple[float, float],
    visual_values_mode: str = "off",
) -> PageState:
    return PageState(
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        pdf_name=pdf_path.name,
        pdf_size=pdf_path.stat().st_size,
        page=page,
        page_index=page_index,
        total_pages=total_pages,
        dpi=dpi,
        page_dir=str(page_dir),
        page_size=page_size,
        visual_values_mode=visual_values_mode,
    )


def validate_checkpoint(
    state: PageState,
    *,
    pdf_path: Path,
    page: int,
    dpi: int,
    page_dir: Path,
) -> None:
    """Reject checkpoints from a different source or incompatible run."""

    if state.pdf_name != pdf_path.name or state.pdf_size != pdf_path.stat().st_size:
        raise ValueError(f"Checkpoint source PDF does not match: {state.checkpoint_path}")
    if state.page != page:
        raise ValueError(f"Checkpoint page {state.page} does not match requested page {page}")
    if state.dpi != dpi:
        raise ValueError(f"Checkpoint DPI {state.dpi} does not match requested DPI {dpi}")
    # Checkpoint payloads keep page-local artifact paths.  The absolute
    # directory recorded by a Docker run may legitimately differ on the host
    # that replays the checkpoint, so validate the stable page identity only.
    if Path(state.page_dir).name != page_dir.name:
        raise ValueError(f"Checkpoint page directory does not match: {state.checkpoint_path}")


def relative_page_path(path: str | Path | None, page_dir: Path) -> str | None:
    """Store page-local paths so checkpoints work across host and Docker paths."""

    if not path:
        return None
    candidate = Path(path)
    try:
        return candidate.resolve().relative_to(page_dir.resolve()).as_posix()
    except ValueError:
        return candidate.as_posix()


def resolve_page_path(path: str | Path | None, page_dir: Path) -> str | None:
    if not path:
        return None
    candidate = Path(path)
    if candidate.is_absolute():
        return str(candidate)
    return str((page_dir / candidate).resolve())


class StatusReporter:
    """Print compact, flushed stage status lines for long-running pages."""

    def __init__(self, *, page_index: int, total_pages: int, page: int) -> None:
        self.page_index = page_index
        self.total_pages = total_pages
        self.page = page

    def _prefix(self, stage: str) -> str:
        return f"[{self.page_index}/{self.total_pages} p{self.page:04d}] {stage.upper()}"

    def emit(self, stage: str, status: str, **details: Any) -> None:
        fields = " ".join(
            f"{key}={_format_value(value)}" for key, value in details.items() if value is not None
        )
        line = f"{self._prefix(stage)} {status}"
        if fields:
            line += f" {fields}"
        print(line, flush=True)

    def start(self, stage: str, **details: Any) -> float:
        started = time.perf_counter()
        self.emit(stage, "start", **details)
        return started

    def ok(self, stage: str, started: float, **details: Any) -> float:
        elapsed = time.perf_counter() - started
        self.emit(stage, "ok", seconds=f"{elapsed:.1f}", **details)
        return elapsed

    def skip(self, stage: str, reason: str) -> None:
        self.emit(stage, "skip", reason=reason)

    def fail(self, stage: str, started: float, error: BaseException) -> float:
        elapsed = time.perf_counter() - started
        self.emit(
            stage,
            "fail",
            seconds=f"{elapsed:.1f}",
            error=type(error).__name__,
            message=str(error)[:160],
        )
        return elapsed


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = str(value).replace("\n", " ").replace("\r", " ")
    return text.replace(" ", "_")


def stage_index(stage: str) -> int:
    try:
        return STAGES.index(stage)
    except ValueError as exc:
        raise ValueError(f"Unknown pipeline stage: {stage}") from exc


def invalidate_from(state: PageState, stage: str) -> None:
    """Clear in-memory state owned by a stage and all stages after it."""

    start = stage_index(stage)
    completed = stage_index(state.completed_stage) if state.completed_stage in STAGES else len(STAGES)
    if start > completed:
        return

    if start <= stage_index("picture_triage"):
        for record in state.picture_records:
            record.update(
                {
                    "triage_type": "",
                    "triage_confidence": None,
                    "triage_action": "",
                    "triage_usage": None,
                    "triage_warnings": [],
                }
            )
    if start <= stage_index("picture_extract"):
        for record in state.picture_records:
            record.update(
                {
                    "summary": "",
                    "summary_type": "",
                    "summary_warnings": [],
                    "usage": None,
                }
            )
    if start <= stage_index("table_detect"):
        state.table_candidates = []
        state.visual_values_mode = "off"
        state.visual_candidates = []
        state.visual_audit = {}
    if start <= stage_index("table_extract"):
        for candidate in state.table_candidates:
            # Sectioned tables are rendered and verified at table_detect;
            # replaying table_extract only re-places symbols, it never re-transcribes them.
            if (candidate.get("stats") or {}).get("format") == "sectioned_table":
                continue
            candidate.update(
                {
                    "markdown": "",
                    "verified": False,
                    "warnings": [],
                    "usage": None,
                    "crop_path": None,
                }
            )
    if start <= stage_index("page_refine"):
        state.refined_markdown = ""
        state.final_markdown = ""
        state.warnings = {}
        state.pre_repair_markdown = ""
        state.pre_repair_warnings = {}
    if start <= stage_index("page_repair"):
        # Restore the post-refine snapshot: the checkpoint's final_markdown may
        # already be the repaired version, and the repair pass must re-run
        # against its real input, not its own previous output (or nothing).
        # Old checkpoints without a snapshot keep their final_markdown as the
        # best available approximation.
        if state.pre_repair_markdown:
            state.final_markdown = state.pre_repair_markdown
            state.warnings = dict(state.pre_repair_warnings)
        state.repair_layout_map = {}
        state.page_repair_usage = None
    if start <= stage_index("finalize"):
        state.status = "ready"
        state.failure = None
    state.completed_stage = STAGES[start - 1] if start > 0 else None


def clear_downstream_artifacts(page_dir: Path, stage: str) -> None:
    """Remove only artifacts that would otherwise look current after replay."""

    groups: dict[str, tuple[str, ...]] = {
        "picture_triage": ("image_summaries.jsonl",),
        "picture_extract": ("image_summaries.jsonl",),
        "table_detect": ("table_candidates.json", "table_candidates_overlay.png", "table_reconstruction.json", "table_reconstruction_overlay.png", "visual_candidates.json"),
        "table_extract": ("table_candidates.json", "table_candidates_overlay.png", "table_candidates", "table_reconstruction.json", "table_reconstruction_overlay.png", "visual_candidates.json"),
        "page_refine": ("page_vlm.md", "page_vlm_input.png", "page_repair.md", "docling_final.md", "layout_prompt_map_repair.json"),
        "page_repair": ("page_vlm_input.png", "page_repair.md", "docling_final.md", "layout_prompt_map_repair.json"),
        "finalize": ("docling_final.md", "image_summaries.jsonl"),
    }
    start = stage_index(stage)
    for candidate_stage, names in groups.items():
        if stage_index(candidate_stage) < start:
            continue
        for name in names:
            path = page_dir / name
            if path.is_dir():
                for child in sorted(path.rglob("*"), reverse=True):
                    if child.is_file() or child.is_symlink():
                        child.unlink()
                    elif child.is_dir():
                        child.rmdir()
                path.rmdir()
            elif path.exists():
                path.unlink()


def iter_failed_pages(states: Iterable[PageState]) -> list[PageState]:
    return [state for state in states if state.status == "failed" or state.failure]
