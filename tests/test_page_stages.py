"""Stage-wiring checks for prepare, refine, repair, and finalize.

These stages coordinate the refine-mode skip, the full-resolution table
exemption, the pre-repair snapshot, and the repair accept/reject decision --
the logic the pipeline depends on most and the only part of ``pipeline.stages``
that had no coverage. ``_run_table_detect`` and ``_run_table_extract`` are
covered by ``test_pipeline_regressions.py``.

Everything here is deterministic and synthetic: no Docling, Ollama, Docker, or
GPU. ``_prepare_page`` and ``run_pipeline`` are deliberately not covered end to
end -- both need a real ``DoclingDocument`` and a ``fitz`` page, and faking
those convincingly would test the fake rather than the code. ``_prepare_page``
is reached here through ``_split_furniture_items``, and ``run_pipeline``'s
confirmed failure mode is covered directly in ``test_stale_outputs.py``.

Run from the repository root with ``python tests/test_page_stages.py``.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
from pathlib import Path
from typing import Any


from document_extract.markdown.postprocess import normalize_furniture_text
from document_extract.models import PictureRecord, TableCandidate
from document_extract.pipeline import stages
from document_extract.runtime import CHECKPOINT_SCHEMA_VERSION, PageState, StatusReporter


_failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"[ok] {message}")
    else:
        _failures.append(message)
        print(f"[FAIL] {message}")


# Must clear refinement.PLAIN_PAGE_MIN_CHARS for the plain-page predicate.
PROSE = (
    "This page carries nothing but running prose. "
    "It has no pictures, no tables, and no reordering, which is exactly the "
    "shape refine-mode auto is allowed to pass through untouched, so the "
    "paragraph has to clear the minimum character budget on its own. "
    "A second sentence keeps it comfortably above that threshold without "
    "introducing any structure the postprocess chain would want to change."
)


def make_state(page_dir: Path, **overrides: Any) -> PageState:
    state = PageState(
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        pdf_name="doc.pdf",
        pdf_size=1,
        page=1,
        page_index=1,
        total_pages=1,
        dpi=200,
        page_dir=str(page_dir),
        page_size=(100.0, 100.0),
    )
    state.raw_markdown = PROSE
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def make_args(**overrides: Any) -> argparse.Namespace:
    args = argparse.Namespace(
        skip_vlm=True,
        skip_picture_triage=False,
        photo_summaries=False,
        refine_mode="auto",
        vlm_page_image_max_px=0,
        temperature=0.0,
        num_ctx=8192,
        num_predict=1200,
        auto_num_ctx=False,
        vlm_concurrency=1,
        triage_num_predict=64,
        triage_confidence=0.65,
        photo_skip_confidence=0.8,
        ollama_base_url="http://localhost:11434",
        ollama_model="model",
        triage_model="model",
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def reporter() -> StatusReporter:
    return StatusReporter(page_index=1, total_pages=1, page=1)


@contextlib.contextmanager
def quiet():
    """Stage functions print progress lines; keep the check output readable."""
    with contextlib.redirect_stdout(io.StringIO()):
        yield


def stage_details(state: PageState, stage: str) -> dict[str, Any]:
    for record in reversed(state.stage_history):
        if record.stage == stage:
            return record.details
    raise AssertionError(f"stage {stage} never ran")


def stage_status(state: PageState, stage: str) -> str:
    for record in reversed(state.stage_history):
        if record.stage == stage:
            return record.status
    raise AssertionError(f"stage {stage} never ran")


class FakeItem:
    """Minimal stand-in for a Docling item: label, text, and a provenance bbox."""

    def __init__(self, label: str, text: str, rect: list[float]) -> None:
        self.label = label
        self.text = text
        left, top, right, bottom = rect
        # bbox_to_normalized_rect divides by the page size, so a 100x100 page
        # turns these into the normalized rect the furniture bands expect.
        self.prov = [
            type(
                "Prov",
                (),
                {
                    "page_no": 1,
                    "bbox": type(
                        "Bbox",
                        (),
                        {
                            "l": left * 100,
                            "t": top * 100,
                            "r": right * 100,
                            "b": bottom * 100,
                            "coord_origin": "TOPLEFT",
                        },
                    )(),
                },
            )()
        ]


def check_plain_page_skips_refinement() -> None:
    """refine-mode auto must pass a plain prose page through with no VLM call."""
    with tempfile.TemporaryDirectory() as temp:
        page_dir = Path(temp)
        state = make_state(page_dir)
        with quiet():
            stages._run_page_refine(
                state=state,
                reporter=reporter(),
                args=make_args(refine_mode="auto"),
                runtime={},
                prompt="{source_markdown}{layout_blocks}{verified_tables}",
            )
        details = stage_details(state, "page_refine")
        check(
            details.get("refine_skipped") == "plain_page",
            "refine-mode auto records the plain-page skip",
        )
        check(
            state.page_vlm_usage is None and details.get("calls") == 0,
            "a skipped refinement makes no VLM call",
        )
        check(
            state.refined_markdown == state.raw_markdown,
            "a skipped refinement keeps the Docling markdown verbatim",
        )
        check(
            not (page_dir / "page_vlm.md").exists(),
            "a skipped refinement writes no page_vlm.md",
        )


def check_refine_mode_always_never_skips() -> None:
    """The same plain page must still be refined under refine-mode always."""
    calls: list[Path] = []

    def fake_refine(*, page_image_path: Path, source_markdown: str, **_kwargs: Any):
        calls.append(page_image_path)
        return source_markdown, {"total_tokens": 7}

    with tempfile.TemporaryDirectory() as temp:
        page_dir = Path(temp)
        (page_dir / "page.png").write_bytes(b"not really a png")
        state = make_state(page_dir)
        original = stages.refine_page_markdown
        stages.refine_page_markdown = fake_refine
        try:
            with quiet():
                stages._run_page_refine(
                    state=state,
                    reporter=reporter(),
                    args=make_args(refine_mode="always"),
                    runtime={},
                    prompt="p",
                )
        finally:
            stages.refine_page_markdown = original
        check(len(calls) == 1, "refine-mode always calls the VLM on a plain page")
        check(
            "refine_skipped" not in stage_details(state, "page_refine"),
            "refine-mode always records no skip",
        )
        check(
            (page_dir / "page_vlm.md").exists(),
            "a refinement with usage writes page_vlm.md",
        )


def check_full_res_exemption_overrides_the_cap() -> None:
    """Table and TOC pages must reach the VLM at full resolution."""
    from PIL import Image

    seen: list[Path] = []

    def fake_refine(*, page_image_path: Path, source_markdown: str, **_kwargs: Any):
        seen.append(page_image_path)
        return source_markdown, {"total_tokens": 1}

    def run(**state_overrides: Any) -> tuple[Path, dict[str, Any]]:
        seen.clear()
        with tempfile.TemporaryDirectory() as temp:
            page_dir = Path(temp)
            Image.new("RGB", (3000, 1500)).save(page_dir / "page.png")
            state = make_state(page_dir, **state_overrides)
            original = stages.refine_page_markdown
            stages.refine_page_markdown = fake_refine
            try:
                with quiet():
                    stages._run_page_refine(
                        state=state,
                        reporter=reporter(),
                        args=make_args(
                            refine_mode="always", vlm_page_image_max_px=1536
                        ),
                        runtime=dict(state_overrides.pop("runtime", {})),
                        prompt="p",
                    )
            finally:
                stages.refine_page_markdown = original
            return seen[0], stage_details(state, "page_refine")

    table = TableCandidate("t1", "layout_region", None)

    used, details = run(table_candidates=[_candidate_row(table)])
    check(
        used.name == "page.png" and details.get("page_image") == "full_res_table",
        "a page with table candidates keeps the full-resolution image",
    )

    used, details = run(page_role="toc")
    check(
        used.name == "page.png" and details.get("page_image") == "full_res_table",
        "a TOC page keeps the full-resolution image",
    )

    used, details = run()
    check(
        used.name == "page_vlm_input.png" and "page_image" not in details,
        "an ordinary page uses the downscaled image",
    )


def _candidate_row(candidate: TableCandidate) -> dict[str, Any]:
    """The checkpoint shape _candidates_from_state reads back."""
    return {
        "candidate_id": candidate.candidate_id,
        "kind": candidate.kind,
        "bbox": candidate.bbox,
        "source_block_ids": [],
        "picture_index": None,
        "confidence": 0.0,
        "reason": "",
        "markdown": candidate.markdown,
        "verified": candidate.verified,
        "stats": candidate.stats,
        "warnings": [],
        "usage": None,
        "crop_path": None,
    }


def check_pre_repair_snapshot_is_written() -> None:
    """Refine must snapshot its result so page_repair can be replayed."""
    with tempfile.TemporaryDirectory() as temp:
        state = make_state(Path(temp))
        with quiet():
            stages._run_page_refine(
                state=state,
                reporter=reporter(),
                args=make_args(refine_mode="auto"),
                runtime={},
                prompt="p",
            )
        check(
            state.pre_repair_markdown == state.final_markdown
            and state.pre_repair_markdown != "",
            "refine snapshots final_markdown into pre_repair_markdown",
        )
        check(
            state.pre_repair_warnings == state.warnings,
            "refine snapshots the warnings alongside the markdown",
        )


def check_repair_skipped_without_trigger() -> None:
    """A clean page must not spend a repair call."""
    with tempfile.TemporaryDirectory() as temp:
        state = make_state(Path(temp), final_markdown=PROSE, warnings={})
        with quiet():
            stages._run_page_repair(
                state=state,
                reporter=reporter(),
                args=make_args(),
                runtime={},
                prompt="p",
            )
        check(
            stage_status(state, "page_repair") == "skipped"
            and stage_details(state, "page_repair").get("reason") == "no_trigger",
            "a page with no repair trigger skips the repair stage",
        )
        check(state.page_repair_usage is None, "a skipped repair records no usage")


def check_repair_rejection_preserves_pre_repair_markdown() -> None:
    """A truncated repair must be rejected and the pre-repair text kept."""
    good = "# Heading\n\n" + PROSE + "\n\n" + PROSE

    def fake_repair(*, current_markdown: str, **_kwargs: Any):
        return "# Heading\n\ntruncated", {"total_tokens": 3, "length_capped": True}

    with tempfile.TemporaryDirectory() as temp:
        page_dir = Path(temp)
        (page_dir / "page.png").write_bytes(b"png")
        state = make_state(
            page_dir,
            final_markdown=good,
            pre_repair_markdown=good,
            warnings={"content_loss_guard_triggered": True},
        )
        original = stages.repair_page_markdown
        stages.repair_page_markdown = fake_repair
        try:
            with quiet():
                stages._run_page_repair(
                    state=state,
                    reporter=reporter(),
                    args=make_args(),
                    runtime={},
                    prompt="p",
                )
        finally:
            stages.repair_page_markdown = original
        check(
            state.warnings.get("repair_rejected"),
            "a regressing repair is rejected with a recorded reason",
        )
        check(
            state.final_markdown == good,
            "a rejected repair leaves final_markdown at the pre-repair text",
        )
        check(
            stage_details(state, "page_repair").get("accepted") is False,
            "a rejected repair is reported as not accepted",
        )


def check_repair_acceptance_replaces_markdown() -> None:
    """A clean repair must replace the markdown and write its artifact."""
    before = "# Heading\n\n" + PROSE
    after = "# Heading\n\n" + PROSE + "\n\nRecovered trailing sentence for the page."

    def fake_repair(*, current_markdown: str, **_kwargs: Any):
        return after, {"total_tokens": 11}

    with tempfile.TemporaryDirectory() as temp:
        page_dir = Path(temp)
        (page_dir / "page.png").write_bytes(b"png")
        state = make_state(
            page_dir,
            raw_markdown=after,
            final_markdown=before,
            pre_repair_markdown=before,
            warnings={
                "content_loss_guard_triggered": True,
                "table_rows_realigned": 2,
            },
        )
        original = stages.repair_page_markdown
        stages.repair_page_markdown = fake_repair
        try:
            with quiet():
                stages._run_page_repair(
                    state=state,
                    reporter=reporter(),
                    args=make_args(),
                    runtime={},
                    prompt="p",
                )
        finally:
            stages.repair_page_markdown = original
        check(
            "repair_rejected" not in state.warnings,
            "a clean repair is not rejected",
        )
        check(
            "Recovered trailing sentence" in state.final_markdown,
            "an accepted repair replaces final_markdown",
        )
        check(
            (page_dir / "page_repair.md").exists(),
            "an accepted repair writes page_repair.md",
        )
        check(
            state.warnings.get("table_rows_realigned") == 2,
            "an accepted repair cannot erase a structural table warning",
        )


def check_furniture_split_drops_and_keeps() -> None:
    """Furniture partitioning must drop margin repeats and protect real headings."""
    body = FakeItem("text", "Body paragraph that belongs to the page.", [0.1, 0.4, 0.9, 0.5])
    footer = FakeItem("text", "Company annual report", [0.1, 0.95, 0.9, 0.99])
    tab = FakeItem("text", "4", [0.95, 0.4, 0.99, 0.45])
    heading = FakeItem("section_header", "1.6 Risk Factors", [0.1, 0.3, 0.9, 0.35])
    header = FakeItem("text", "1.6 Risk Factors", [0.1, 0.01, 0.9, 0.05])

    # Build the signatures the way the runner does, so the test does not encode
    # normalize_furniture_text's rules (it strips digits, among other things).
    signatures = {
        (normalize_furniture_text(footer.text), "bottom"),
        (normalize_furniture_text(header.text), "top"),
    }

    kept, texts, dropped = stages._split_furniture_items(
        [body, footer, tab, heading, header],
        object(),
        1,
        (100.0, 100.0),
        signatures,
        set(),
    )
    kept_texts = {item.text for item in kept}
    check(
        "Body paragraph that belongs to the page." in kept_texts,
        "a body item is kept",
    )
    check(footer not in kept, "a repeated bottom-band item is dropped as furniture")
    check(tab not in kept, "a right-edge digit is dropped as a chapter tab")
    check(heading in kept, "a real section heading in the body band is kept")
    check(
        normalize_furniture_text(footer.text) in texts,
        "the dropped footer's text is returned for markdown stripping",
    )
    check(
        normalize_furniture_text(header.text) not in texts,
        "a text a kept heading still uses is never returned for stripping",
    )
    check(dropped == 3, "the dropped count covers footer, tab, and running header")


def check_finalize_writes_artifacts() -> None:
    """Finalize must write the page's outputs and mark the page completed."""
    with tempfile.TemporaryDirectory() as temp:
        page_dir = Path(temp)
        record = PictureRecord(
            page=1,
            index=1,
            placeholder="{{DOC_IMAGE_p0001_i001}}",
            rel_path="images/picture_p0001_i001.png",
            abs_path=None,
            bbox=None,
            area_ratio=0.2,
            classification="",
            caption="",
            summary="a chart",
        )
        state = make_state(page_dir, final_markdown="# Final\n", warnings={"a": 1})
        with quiet():
            stages._run_finalize(
                state=state, reporter=reporter(), runtime={"records": [record]}
            )
        check(
            (page_dir / "docling_final.md").read_text(encoding="utf-8") == "# Final\n",
            "finalize writes docling_final.md from final_markdown",
        )
        rows = [
            json.loads(line)
            for line in (page_dir / "image_summaries.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        check(
            len(rows) == 1 and rows[0]["summary"] == "a chart",
            "finalize writes one image_summaries row per record",
        )
        check(state.status == "completed", "finalize marks the page completed")


def check_triage_and_extract_respect_skip_vlm() -> None:
    """--skip-vlm must reach the picture stages without any model call."""
    with tempfile.TemporaryDirectory() as temp:
        page_dir = Path(temp)
        (page_dir / "page.png").write_bytes(b"png")
        record = PictureRecord(
            page=1,
            index=1,
            placeholder="{{DOC_IMAGE_p0001_i001}}",
            rel_path="images/picture_p0001_i001.png",
            abs_path=str(page_dir / "picture.png"),
            bbox={"l": 10.0, "t": 10.0, "r": 60.0, "b": 60.0},
            area_ratio=0.25,
            classification="chart",
            caption="",
        )
        state = make_state(page_dir)
        with quiet():
            stages._run_picture_triage(
                state=state,
                reporter=reporter(),
                args=make_args(skip_vlm=True),
                runtime={"records": [record]},
            )
        check(
            record.skip_reason == "skip_vlm" and record.triage_action == "skip",
            "skip-vlm marks a triage candidate as skipped without a call",
        )
        check(
            stage_details(state, "picture_triage").get("calls") == 0,
            "skip-vlm triage reports no calls",
        )

        with quiet():
            stages._run_picture_extract(
                state=state,
                reporter=reporter(),
                args=make_args(skip_vlm=True),
                runtime={"records": [record]},
                prompt="{context}",
            )
        check(
            stage_details(state, "picture_extract").get("calls") == 0
            and record.summary == "",
            "skip-vlm extraction produces no summary and no calls",
        )


def main() -> int:
    check_plain_page_skips_refinement()
    check_refine_mode_always_never_skips()
    check_full_res_exemption_overrides_the_cap()
    check_pre_repair_snapshot_is_written()
    check_repair_skipped_without_trigger()
    check_repair_rejection_preserves_pre_repair_markdown()
    check_repair_acceptance_replaces_markdown()
    check_furniture_split_drops_and_keeps()
    check_finalize_writes_artifacts()
    check_triage_and_extract_respect_skip_vlm()
    print()
    if _failures:
        print(f"{len(_failures)} FAILED:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
