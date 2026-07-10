"""Focused checks for refactor regressions: config propagation into tables,
page_repair replay state, layout-map picture keying, and retry accounting.

Run from the repository root with ``python tests/test_pipeline_regressions.py``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from document_extract import pictures, tables
from document_extract.config import apply_detection_config, config_from_mapping
from document_extract.layout.prompt_map import build_layout_prompt_map
from document_extract.models import PictureRecord
from document_extract.pipeline.runner import selected_page_numbers
from document_extract.runtime import CHECKPOINT_SCHEMA_VERSION, PageState, invalidate_from


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


def make_picture_record(**overrides) -> PictureRecord:
    payload = {
        "page": 1,
        "index": 1,
        "placeholder": "{{DOC_IMAGE_p0001_i001}}",
        "rel_path": "images/picture_p0001_i001.png",
        "abs_path": "images/picture_p0001_i001.png",
        "bbox": {"l": 10.0, "t": 10.0, "r": 60.0, "b": 60.0},
        "area_ratio": 0.3,
        "classification": "chart",
        "caption": "",
    }
    payload.update(overrides)
    return PictureRecord(**payload)


def make_page_state() -> PageState:
    return PageState(
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        pdf_name="doc.pdf",
        pdf_size=1,
        page=1,
        page_index=1,
        total_pages=1,
        dpi=200,
        page_dir="page_0001",
        page_size=(100.0, 100.0),
    )


def check_detection_config_reaches_tables() -> None:
    defaults = (
        pictures.PICTURE_MIN_AREA_RATIO,
        pictures.PICTURE_DECORATIVE_MAX_AREA_RATIO,
        pictures.PICTURE_TABLE_MIN_AREA_RATIO,
    )
    record = make_picture_record()
    rect = [0.1, 0.4, 0.9, 0.6]
    try:
        apply_detection_config(
            config_from_mapping({"pictures": {"table_min_area_ratio": 0.5}})
        )
        is_table_like, reason, _ = tables.picture_record_is_table_like(record, rect, [])
        check(
            not is_table_like and reason == "picture_too_small",
            "pictures.table_min_area_ratio override reaches table detection",
        )
        apply_detection_config(
            config_from_mapping({"pictures": {"table_min_area_ratio": 0.08}})
        )
        is_table_like, _, _ = tables.picture_record_is_table_like(record, rect, [])
        check(is_table_like, "default threshold still accepts a table-like picture")
    finally:
        (
            pictures.PICTURE_MIN_AREA_RATIO,
            pictures.PICTURE_DECORATIVE_MAX_AREA_RATIO,
            pictures.PICTURE_TABLE_MIN_AREA_RATIO,
        ) = defaults


def check_page_repair_replay_state() -> None:
    state = make_page_state()
    state.completed_stage = "finalize"
    state.refined_markdown = "# refined"
    state.pre_repair_markdown = "# refined page"
    state.pre_repair_warnings = {"content_loss_guard_triggered": False}
    state.final_markdown = "# repaired page"
    state.warnings = {"repair_rejected": ["shrunk_output"]}
    invalidate_from(state, "page_repair")
    check(
        state.final_markdown == "# refined page",
        "resume from page_repair restores the post-refine markdown",
    )
    check(
        state.warnings == {"content_loss_guard_triggered": False},
        "resume from page_repair restores the post-refine warnings",
    )

    state = make_page_state()
    state.completed_stage = "finalize"
    state.pre_repair_markdown = "# refined page"
    state.pre_repair_warnings = {"content_loss_guard_triggered": True}
    state.final_markdown = "# repaired page"
    invalidate_from(state, "page_refine")
    check(
        state.final_markdown == "" and state.pre_repair_markdown == "",
        "resume from page_refine clears markdown and the repair snapshot",
    )

    # Old checkpoint without a snapshot: keep final_markdown instead of wiping it.
    state = make_page_state()
    state.completed_stage = "finalize"
    state.final_markdown = "# repaired page"
    invalidate_from(state, "page_repair")
    check(
        state.final_markdown == "# repaired page",
        "snapshot-less checkpoint keeps final_markdown on page_repair resume",
    )


def check_layout_map_picture_keying() -> None:
    class FakeBBox:
        l, t, r, b = 10.0, 10.0, 60.0, 60.0

    class FakeProv:
        page_no = 1
        bbox = FakeBBox()

    class FakeItem:
        label = "picture"
        prov = [FakeProv()]
        text = ""

    item = FakeItem()
    record = make_picture_record(caption="Sales by region chart")
    layout_map = build_layout_prompt_map(
        items=[item],
        page_size=(100.0, 100.0),
        picture_records={id(item): record},
    )
    check(
        layout_map["blocks"][0].get("caption") == "Sales by region chart",
        "id(item)-keyed picture map enriches layout-map captions",
    )


def check_summary_retry_usage_accounting() -> None:
    calls: list[str] = []

    def fake_call_ollama_vlm(**kwargs):
        calls.append(kwargs["prompt"])
        if len(calls) == 1:
            # Well-typed but shapeless answer: forces the one focused retry.
            return "TYPE: chart", {"prompt_tokens": 100, "output_tokens": 10, "total_tokens": 110}
        return "- A: 1\n- B: 2", {"prompt_tokens": 50, "output_tokens": 20, "total_tokens": 70}

    record = make_picture_record()
    record.summarize = True
    args = argparse.Namespace(
        skip_vlm=False,
        ollama_base_url="http://unused.test",
        ollama_model="test-model",
        temperature=0.0,
        num_ctx=0,
        num_predict=0,
        auto_num_ctx=False,
    )
    original = pictures.ollama_client.call_ollama_vlm
    pictures.ollama_client.call_ollama_vlm = fake_call_ollama_vlm
    try:
        pictures.summarize_pictures(records=[record], prompt_template="{context}", args=args)
    finally:
        pictures.ollama_client.call_ollama_vlm = original
    check(len(calls) == 2, "shapeless summary triggers exactly one retry")
    check(record.summary == "- A: 1\n- B: 2", "retry body accepted as the summary")
    check(
        record.usage["total_tokens"] == 180 and record.usage["prompt_tokens"] == 150,
        "retry usage sums both calls' tokens",
    )
    check(bool(record.usage["retried"]), "retry is visible in usage accounting")


def check_page_selection_bounds() -> None:
    check(selected_page_numbers(10, 2, 4) == [2, 3, 4], "page range selection works")
    try:
        selected_page_numbers(10, 11, 12)
    except ValueError:
        check(True, "start page beyond document length is rejected")
    else:
        raise AssertionError("start page beyond document length was not rejected")


def main() -> int:
    check_detection_config_reaches_tables()
    check_page_repair_replay_state()
    check_layout_map_picture_keying()
    check_summary_retry_usage_accounting()
    check_page_selection_bounds()
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
