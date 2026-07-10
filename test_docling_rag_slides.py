"""Lightweight tests for docling_rag_slides glue code.

Run: python test_docling_rag_slides.py
"""

from __future__ import annotations

import sys
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace

import docling_rag_slides as drs
import pipeline_runtime as runtime

_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    status = "ok  " if cond else "FAIL"
    print(f"[{status}] {msg}")
    if not cond:
        _failures.append(msg)


def make_record(index: int, *, summary: str = "", area: float = 0.1) -> drs.PictureRecord:
    return drs.PictureRecord(
        page=26,
        index=index,
        placeholder=f"{{{{DOC_IMAGE_p0026_i{index:03d}}}}}",
        rel_path=f"images/picture_p0026_i{index:03d}.png",
        abs_path=None,
        bbox={"l": 0, "t": 0, "r": 10, "b": 10},
        area_ratio=area,
        classification="chart",
        caption="",
        summary=summary,
    )


def test_image_placeholder_replacement_order() -> None:
    records = [
        make_record(1, summary="First chart summary."),
        make_record(2, summary="Second chart summary."),
    ]
    md = "Before\n\n{{DOC_IMAGE_p0026_i001}}\n\nMiddle\n\n<!-- image -->\n\nAfter"
    out = drs.insert_image_references_and_summaries(md, records)
    check("picture_p0026_i001.png" in out, "first image ref inserted")
    check("picture_p0026_i002.png" in out, "second image ref inserted")
    check(
        out.index("picture_p0026_i001.png") < out.index("picture_p0026_i002.png"),
        "image refs preserve placeholder order",
    )
    check(out.count("**Image summary:**") == 2, "summaries inserted under refs")


def test_refine_page_markdown_skip_vlm_returns_source() -> None:
    args = SimpleNamespace(
        skip_vlm=True,
        ollama_base_url="",
        ollama_model="",
        temperature=0.0,
        num_ctx=0,
        num_predict=0,
        auto_num_ctx=False,
    )
    refined, usage = drs.refine_page_markdown(
        source_markdown="# Source\n",
        layout_blocks={"page_size": [100, 100], "blocks": []},
        table_candidates=[],
        page_image_path=drs.Path("page.png"),
        prompt_template=drs.DEFAULT_PAGE_REFINEMENT_PROMPT,
        args=args,
    )
    check(refined == "# Source\n", "skip-vlm keeps Docling markdown unchanged")
    check(usage is None, "skip-vlm produces no VLM usage")


def test_should_run_repair_pass_for_unplaced_content() -> None:
    run = drs.should_run_repair_pass(
        items=[],
        warnings={"content_loss_guard_triggered": True, "unplaced_content_lines": ["1929"]},
        current_markdown="# Page\n",
        records=[],
    )
    check(run, "repair pass triggers when unplaced content remains")


def test_should_run_repair_pass_for_table_item() -> None:
    item = SimpleNamespace(label="table")
    run = drs.should_run_repair_pass(
        items=[item],
        warnings={"content_loss_guard_triggered": False},
        current_markdown="# Page\n",
        records=[],
    )
    check(run, "repair pass triggers on Docling table items")


def test_should_run_repair_pass_candidate_routing() -> None:
    unverified = drs.TableCandidate(
        candidate_id="tc001",
        kind="layout_region",
        bbox=[0.1, 0.1, 0.9, 0.4],
    )
    run = drs.should_run_repair_pass(
        items=[],
        warnings={"content_loss_guard_triggered": False},
        current_markdown="# Page\n",
        records=[],
        table_candidates=[unverified],
    )
    check(not run, "unverified candidates alone do not force a repair pass")

    run = drs.should_run_repair_pass(
        items=[],
        warnings={"content_loss_guard_triggered": False, "verified_tables_missing": ["tc001"]},
        current_markdown="# Page\n",
        records=[],
        table_candidates=[unverified],
    )
    check(run, "repair pass triggers when a verified table is missing from the page")


def test_missing_verified_table_ids() -> None:
    table = "| Region | Growth |\n| --- | --- |\n| Europe | +2.3% |\n| AMEA | +5.6% |"
    verified = drs.TableCandidate(
        candidate_id="tc001",
        kind="layout_region",
        bbox=[0.1, 0.1, 0.9, 0.4],
        markdown=table,
        verified=True,
    )
    present = "# Page\n\n| Region | Growth |\n| --- | --- |\n| Europe | +2.3% |\n| AMEA | +5.6% |\n"
    check(
        drs.missing_verified_table_ids(present, [verified]) == [],
        "verified table found in markdown is not reported missing",
    )
    check(
        drs.missing_verified_table_ids("# Page\n\nNo table here.\n", [verified]) == ["tc001"],
        "verified table absent from markdown is reported missing",
    )
    unverified = drs.TableCandidate(
        candidate_id="tc002",
        kind="layout_region",
        bbox=None,
        markdown=table,
        verified=False,
    )
    check(
        drs.missing_verified_table_ids("# Page\n", [unverified]) == [],
        "unverified candidates are never reported missing",
    )


def test_should_run_repair_pass_for_value_cluster_with_figures() -> None:
    markdown = "# Dashboard\n\n27.3BnEUR\n\n90,000\n\n151\n\n120\n"
    run = drs.should_run_repair_pass(
        items=[],
        warnings={"content_loss_guard_triggered": False},
        current_markdown=markdown,
        records=[make_record(1), make_record(2)],
    )
    check(run, "repair pass triggers on standalone value clusters with multiple figures")


def test_repair_page_markdown_skip_vlm_returns_current() -> None:
    args = SimpleNamespace(
        skip_vlm=True,
        ollama_base_url="",
        ollama_model="",
        temperature=0.0,
        num_ctx=0,
        num_predict=0,
        auto_num_ctx=False,
    )
    repaired, usage = drs.repair_page_markdown(
        current_markdown="# Current\n",
        layout_blocks={"page_size": [100, 100], "blocks": []},
        table_candidates=[],
        page_image_path=drs.Path("page.png"),
        unplaced_lines=["1929"],
        prompt_template=drs.DEFAULT_PAGE_REPAIR_PROMPT,
        args=args,
    )
    check(repaired == "# Current\n", "skip-vlm keeps current markdown in repair pass")
    check(usage is None, "skip-vlm produces no repair-pass usage")


def test_export_page_markdown_prefers_docling_serializer() -> None:
    calls: list[tuple[int, str]] = []

    class FakeDocument:
        def export_to_markdown(self, **kwargs):
            calls.append((kwargs["page_no"], kwargs["image_placeholder"]))
            return "# Docling page\n\n<!-- image -->\n"

    out = drs.export_page_markdown(FakeDocument(), 6, [], {})
    check(out.startswith("# Docling page"), "page export uses Docling markdown when available")
    check(calls == [(6, "<!-- image -->")], "Docling serializer called with page-specific placeholder export")


def test_image_routing() -> None:
    small = make_record(1, area=0.005)
    small.page = 8
    summarize, reason = drs.should_summarize_picture(small)
    check(not summarize and reason == "too_small", "tiny image skipped")

    decorative = make_record(2, area=0.02)
    decorative.page = 8
    decorative.classification = "logo icon"
    summarize, reason = drs.should_summarize_picture(decorative)
    check(not summarize and reason == "decorative", "decorative small image skipped")

    large = make_record(3, area=0.12)
    large.page = 8
    large.classification = ""
    summarize, reason = drs.should_summarize_picture(large)
    check(summarize and reason == "large_picture", "large image summarized")


def test_picture_triage_json_contract() -> None:
    kind, confidence, warnings = drs.parse_picture_triage(
        "```json\n{\"type\": \"chart\", \"confidence\": 0.91}\n```"
    )
    check(kind == "chart", "triage parses the image type")
    check(confidence == 0.91, "triage parses confidence")
    check(not warnings, "valid triage JSON has no warnings")

    kind, confidence, warnings = drs.parse_picture_triage("not json")
    check(kind == "unclear" and confidence == 0.0, "invalid triage falls back to unclear")
    check("triage_invalid_json" in warnings, "invalid triage JSON is recorded")


def test_picture_triage_routes_specialist_and_decorative() -> None:
    original_call = drs.ollama_client.call_ollama_vlm
    calls: list[dict] = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        answer = (
            '{"type":"chart","confidence":0.94}'
            if len(calls) == 1
            else '{"type":"decorative","confidence":0.99}'
        )
        return answer, {"total_tokens": 4}

    drs.ollama_client.call_ollama_vlm = fake_call
    try:
        chart = make_record(1, area=0.02)
        chart.abs_path = "chart.png"
        decorative = make_record(2, area=0.02)
        decorative.abs_path = "icon.png"
        args = SimpleNamespace(
            skip_vlm=False,
            skip_picture_triage=False,
            triage_model="fast-model",
            triage_num_predict=64,
            triage_confidence=0.65,
            ollama_base_url="",
            ollama_model="main-model",
            num_ctx=0,
            auto_num_ctx=False,
        )
        stats = drs.triage_pictures(records=[chart, decorative], args=args)
        check(stats["calls"] == 2, "visual triage calls every non-tiny candidate")
        check(chart.triage_type == "chart" and chart.triage_action == "specialist", "chart routed to specialist")
        check(not decorative.summarize and decorative.skip_reason == "triage_decorative", "decorative image skipped")
        check(calls[0]["model"] == "fast-model", "triage uses the configured model")
        check(calls[0]["num_predict"] == 64, "triage uses the short output cap")
    finally:
        drs.ollama_client.call_ollama_vlm = original_call


def test_picture_specialist_prompt_contains_typed_contract() -> None:
    record = make_record(1, area=0.2)
    record.triage_type = "chart"
    prompt = drs.picture_specialist_prompt(record, drs.DEFAULT_IMAGE_SUMMARY_PROMPT)
    check("TYPE: chart" in prompt, "specialist prompt preserves the typed output contract")
    check("axis label" in prompt, "chart specialist prompt requests chart fields")


def test_page_state_checkpoint_round_trip_and_paths() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        page_dir = drs.Path(temp_dir) / "page_0001"
        page_dir.mkdir()
        pdf_path = drs.Path(temp_dir) / "source.pdf"
        pdf_path.write_bytes(b"")
        state = runtime.new_page_state(
            pdf_path=pdf_path,
            page=1,
            page_index=1,
            total_pages=1,
            dpi=200,
            page_dir=page_dir,
            page_size=(100.0, 200.0),
        )
        state.pdf_size = 0
        state.artifact_paths["page_image"] = "page.png"
        state.picture_records = [{"index": 1, "rel_path": "images/picture.png"}]
        state.stage_history.append(runtime.StageRecord(stage="prepare", status="ok"))
        path = state.save()
        loaded = runtime.PageState.load(path)
        check(loaded.page == 1 and loaded.page_size == [100.0, 200.0], "page state round-trips")
        check(loaded.stage_history[0].stage == "prepare", "stage history round-trips")
        check(runtime.resolve_page_path("images/picture.png", page_dir).endswith("picture.png"), "relative image paths resolve")


def test_status_reporter_is_compact_and_flushed() -> None:
    output = StringIO()
    reporter = runtime.StatusReporter(page_index=1, total_pages=3, page=7)
    with redirect_stdout(output):
        started = reporter.start("picture_triage", candidates=2)
        reporter.ok("picture_triage", started, calls=2, kind="chart")
        reporter.skip("page_repair", "no_trigger")
    text = output.getvalue()
    check("[1/3 p0007] PICTURE_TRIAGE start candidates=2" in text, "status start is structured")
    check("PICTURE_TRIAGE ok" in text and "calls=2" in text, "status success is structured")
    check("PAGE_REPAIR skip reason=no_trigger" in text, "status skip is readable")


def test_summary_block_format() -> None:
    record = make_record(1, summary="Visible labels: 2015: 77%, 2023: 89%.")
    block = drs.image_block(record)
    check(block.startswith("![Picture p0026-i001]"), "image block starts with markdown ref")
    check("**Image summary:** Visible labels" in block, "summary uses expected marker")


def test_block_sidecar_serialization() -> None:
    bbox = SimpleNamespace(l=1, t=2, r=3, b=4, coord_origin="TOPLEFT")
    prov = [SimpleNamespace(page_no=6, bbox=bbox)]
    title = SimpleNamespace(
        text="Performance",
        label="section_header",
        prov=prov,
        self_ref="#/texts/1",
        level=2,
    )
    para = SimpleNamespace(
        text="Revenue grew visibly in the chart.",
        label="paragraph",
        prov=prov,
        self_ref="#/texts/2",
    )
    rows = drs.block_rows_for_page([title, para], 6, {})
    check(rows[0]["bbox"]["l"] == 1.0, "bbox serialized")
    check(rows[1]["heading_path"] == ["Performance"], "heading path carried forward")
    check(rows[1]["text"].startswith("Revenue grew"), "text snippet serialized")


def fake_item(
    *,
    text: str = "",
    label: str = "text",
    bbox: tuple[float, float, float, float] = (0, 0, 10, 10),
    origin: str = "TOPLEFT",
    caption: str = "",
) -> SimpleNamespace:
    bbox_obj = SimpleNamespace(
        l=bbox[0],
        t=bbox[1],
        r=bbox[2],
        b=bbox[3],
        coord_origin=origin,
    )
    return SimpleNamespace(
        text=text,
        label=label,
        caption=caption,
        prov=[SimpleNamespace(page_no=1, bbox=bbox_obj)],
    )


def test_layout_prompt_map_schema_and_value_text() -> None:
    items = [
        fake_item(text="2017", bbox=(100, 500, 140, 480), origin="BOTTOMLEFT"),
        fake_item(text="A long ordinary paragraph " * 30, bbox=(300, 500, 900, 300)),
    ]
    layout_map = drs.build_layout_prompt_map(items, (1000.0, 800.0), {})
    check(layout_map["page_size"] == [1000.0, 800.0], "layout map has page size")
    check(layout_map["blocks"][0]["id"] == "b0001", "layout block has compact id")
    check(layout_map["blocks"][0]["bbox"] == [0.1, 0.375, 0.14, 0.4], "bottom-left bbox normalized")
    check(layout_map["blocks"][0]["text"] == "2017", "standalone year text included")
    check("text" not in layout_map["blocks"][1], "long plain paragraph text omitted")


def test_layout_prompt_map_headers_tables_pictures() -> None:
    picture = fake_item(label="picture", bbox=(10, 10, 20, 20), caption="Visible product photo")
    table = fake_item(text="Header A | Header B | 42%", label="table", bbox=(30, 30, 80, 80))
    header = fake_item(text="Milestones", label="section_header", bbox=(90, 90, 150, 110))
    records = {
        id(picture): drs.PictureRecord(
            page=1,
            index=1,
            placeholder="{{DOC_IMAGE_p0001_i001}}",
            rel_path="images/picture.png",
            abs_path=None,
            bbox={},
            area_ratio=0.1,
            classification="",
            caption="Visible product photo",
        )
    }
    layout_map = drs.build_layout_prompt_map([picture, table, header], (1000.0, 800.0), records)
    check(layout_map["blocks"][0]["type"] == "picture", "picture block type preserved")
    check(layout_map["blocks"][0]["caption"] == "Visible product photo", "picture uses caption field")
    check("text" not in layout_map["blocks"][0], "picture does not use text field")
    check("text" in layout_map["blocks"][1], "table text included")
    check("text" in layout_map["blocks"][2], "header text included")


def test_layout_prompt_map_nearby_context_includes_text() -> None:
    text = fake_item(text="Nearby panel text should be retained for mapping. " * 8, bbox=(110, 100, 360, 300))
    picture = fake_item(label="picture", bbox=(100, 160, 180, 240))
    layout_map = drs.build_layout_prompt_map([text, picture], (1000.0, 800.0), {})
    check("text" in layout_map["blocks"][0], "nearby structured context includes text")


def test_page_prompt_layout_and_unplaced_routing() -> None:
    layout_map = {
        "page_size": [100, 100],
        "blocks": [{"id": "b0001", "type": "paragraph", "bbox": [0, 0, 1, 1], "text": "2017"}],
    }
    verified_table = drs.TableCandidate(
        candidate_id="tc001",
        kind="layout_region",
        bbox=[0, 0, 1, 1],
        markdown="| Year | Value |\n| --- | --- |\n| 2017 | 42% |",
        verified=True,
    )
    unverified_table = drs.TableCandidate(
        candidate_id="tc002",
        kind="layout_region",
        bbox=[0, 0, 1, 1],
        markdown="| Junk | Junk |\n| --- | --- |\n| a | b |",
        verified=False,
    )
    first_prompt = drs.format_page_refinement_prompt(
        prompt_template=drs.DEFAULT_PAGE_REFINEMENT_PROMPT,
        source_markdown="Raw draft\n",
        layout_blocks=layout_map,
        table_candidates=[verified_table, unverified_table],
    )
    check("Compact layout block map" in first_prompt, "first pass prompt includes layout map")
    check('"text":"2017"' in first_prompt, "first pass prompt includes compact block text")
    check("| Year | Value |" in first_prompt, "first pass prompt includes verified table")
    check("| Junk | Junk |" not in first_prompt, "unverified table markdown stays out of the prompt")
    check("Unplaced lines:" not in first_prompt, "first pass prompt does not include unplaced lines")

    empty_prompt = drs.format_page_refinement_prompt(
        prompt_template=drs.DEFAULT_PAGE_REFINEMENT_PROMPT,
        source_markdown="Raw draft\n",
        layout_blocks=layout_map,
        table_candidates=[],
    )
    check("(none)" in empty_prompt, "no verified tables renders as (none)")

    repair_prompt = drs.format_page_repair_prompt(
        prompt_template=drs.DEFAULT_PAGE_REPAIR_PROMPT,
        current_markdown="Current\n",
        layout_blocks=layout_map,
        table_candidates=[verified_table],
        unplaced_lines=["2017"],
    )
    check("Unplaced lines:" in repair_prompt, "repair prompt includes unplaced section")
    check("- 2017" in repair_prompt, "repair prompt includes unplaced values")
    check("| Year | Value |" in repair_prompt, "repair prompt includes verified table")


def test_bbox_area_ratio_bottomleft_coords() -> None:
    bbox = {"l": 0.0, "t": 800.0, "r": 1000.0, "b": 0.0, "origin": "BOTTOMLEFT"}
    ratio = drs.bbox_area_ratio(bbox, (1000.0, 800.0))
    check(ratio == 1.0, "bottom-left bbox still yields correct positive area ratio")


def test_bbox_to_pixel_rect_bottomleft_coords() -> None:
    bbox = {"l": 100.0, "t": 700.0, "r": 300.0, "b": 500.0, "origin": "BOTTOMLEFT"}
    rect = drs.bbox_to_pixel_rect(
        bbox,
        page_size=(1000.0, 800.0),
        image_size=(2000, 1600),
    )
    check(rect == (200, 200, 600, 600), "bottom-left bbox converts to expected pixel rectangle")


def test_completeness_guard_appends_missing_lines() -> None:
    raw = "This important sustainability paragraph must remain visible in the final markdown.\n"
    final = "# Slide\n\nDifferent content only.\n"
    guarded, warnings = drs.apply_completeness_guard(raw, final)
    check(warnings["content_loss_guard_triggered"], "guard reports content loss")
    check("## Unplaced content" in guarded, "guard appends unplaced section")
    check("important sustainability paragraph" in guarded, "missing line preserved")


def test_completeness_guard_ignores_image_placeholder_replacement() -> None:
    raw = "# Slide\n\n{{DOC_IMAGE_p0001_i001}}\n"
    final = "# Slide\n\n![Picture p0001-i001](images/picture_p0001_i001.png)\n"
    guarded, warnings = drs.apply_completeness_guard(raw, final)
    check(not warnings["content_loss_guard_triggered"], "image placeholder replacement is not treated as content loss")
    check("## Unplaced content" not in guarded, "no unplaced-content section for image replacement")


def grid_cells(
    *,
    x_offset: float = 0.0,
    y_offset: float = 0.0,
    columns: tuple[float, ...] = (0.05, 0.28, 0.51),
    rows: int = 4,
    id_offset: int = 0,
) -> list[dict[str, object]]:
    """A label/target/value grid shaped like the p12 dashboard sections.

    Column gaps are ~0.02 like real styled tables (below PANEL_X_GUTTER).
    """
    cells: list[dict[str, object]] = []
    headers = ("GOALS", "TARGETS", "2025 RESULTS")
    index = id_offset
    for col, header in zip(columns, headers):
        index += 1
        x = col + x_offset
        y = 0.10 + y_offset
        cells.append(
            {"id": f"b{index:04d}", "rect": [x, y, x + 0.05, y + 0.015], "text": header, "is_heading": False}
        )
    for row in range(rows):
        y = 0.16 + row * 0.08 + y_offset
        texts = (
            f"Goal number {row} about packaging",
            f"Reach {40 + row}% reduction of emissions by 2030",
            f"{50 + row}.4%",
        )
        for col, text in zip(columns, texts):
            index += 1
            x = col + x_offset
            cells.append(
                {"id": f"b{index:04d}", "rect": [x, y, x + 0.21, y + 0.03], "text": text, "is_heading": False}
            )
    return cells


def prose_column_cells() -> list[dict[str, object]]:
    """Three magazine-style prose columns like page 9 — must never be a table."""
    cells: list[dict[str, object]] = []
    paragraph = (
        "Danone is gradually pivoting the way it addresses its categories, looking "
        "at its markets in a consumer and patient centric way, from yogurt to gut "
        "health and protein expertise, and from waters to healthy hydration overall."
    )
    index = 0
    for column, x in enumerate((0.05, 0.30, 0.55)):
        index += 1
        cells.append(
            {
                "id": f"b{index:04d}",
                "rect": [x, 0.15, x + 0.16, 0.17],
                "text": f"Column heading number {column}",
                "is_heading": True,
            }
        )
        for row in range(4):
            index += 1
            # Paragraph tops deliberately staggered across columns.
            y = 0.20 + row * 0.18 + column * 0.05
            cells.append(
                {"id": f"b{index:04d}", "rect": [x, y, x + 0.20, y + 0.15], "text": paragraph, "is_heading": False}
            )
    return cells


def layout_region_candidates(cells: list[dict[str, object]]) -> list[drs.TableCandidate]:
    candidates = drs.build_table_candidates(
        cells=cells,
        page_size=(1000.0, 800.0),
        picture_records={},
        layout_map={"page_size": [1000.0, 800.0], "blocks": []},
    )
    return [c for c in candidates if c.kind == "layout_region"]


def test_detector_accepts_label_value_grid() -> None:
    candidates = layout_region_candidates(grid_cells())
    check(len(candidates) == 1, "label/value grid detected as one region")
    if candidates:
        check(candidates[0].confidence >= drs.TABLE_SCORE_THRESHOLD, "grid confidence above threshold")
        check(len(candidates[0].source_block_ids) == 15, "grid keeps all cells as sources")


def test_detector_rejects_prose_columns() -> None:
    candidates = layout_region_candidates(prose_column_cells())
    check(not candidates, "multi-column prose produces no table candidates")


def test_detector_splits_side_by_side_panels() -> None:
    cells = grid_cells(columns=(0.03, 0.15, 0.27)) + grid_cells(
        x_offset=0.52, columns=(0.03, 0.15, 0.27), id_offset=50
    )
    candidates = layout_region_candidates(cells)
    check(len(candidates) == 2, "side-by-side panels detected as two regions")
    if len(candidates) == 2:
        boxes = sorted(candidate.bbox[0] for candidate in candidates)
        check(boxes[0] < 0.5 < boxes[1], "regions split at the panel gutter")


def test_detector_merges_severed_label_column() -> None:
    """Wide gutter between a sparse label column and its content column (p24/p39)."""
    cells: list[dict[str, object]] = [
        {"id": "h001", "rect": [0.05, 0.10, 0.10, 0.115], "text": "GOALS", "is_heading": False},
        {"id": "h002", "rect": [0.32, 0.10, 0.38, 0.115], "text": "TARGETS", "is_heading": False},
    ]
    for index in range(5):
        y = 0.16 + index * 0.16  # sparse: would shred without single-column protection
        cells.append(
            {
                "id": f"g{index:03d}",
                "rect": [0.05, y, 0.23, y + 0.03],
                "text": f"Goal {index} on packaging and waste",
                "is_heading": True,  # Docling often types styled labels as headings
            }
        )
    for index in range(9):
        y = 0.16 + index * 0.08
        cells.append(
            {
                "id": f"t{index:03d}",
                "rect": [0.32, y, 0.82, y + 0.03],
                "text": f"Reach {40 + index}% reduction of emissions by 2030",
                "is_heading": False,
            }
        )
    candidates = layout_region_candidates(cells)
    check(len(candidates) == 1, "severed label/content columns re-joined as one region")
    if candidates:
        check(
            candidates[0].reason == "merged_split_columns",
            "re-joined region reports the merge reason",
        )
        check(len(candidates[0].source_block_ids) == 16, "merged region keeps all cells")


def test_normalize_pipe_tables() -> None:
    malformed = (
        "Intro text stays.\n\n"
        "| GOALS | TARGETS | 2025 RESULTS |\n"
        "|---|---|\n"
        "| Goal A | Target A | 42% |\n"
        "|  | Target B | 61% |\n\n"
        "Outro stays.\n"
    )
    out = drs.normalize_pipe_tables(malformed)
    check("|---|---|---|" in out.splitlines(), "separator widened to match the 3-column rows")
    check("|---|---|" not in out.splitlines(), "malformed 2-column separator removed")
    check("| Goal A | Target A | 42% |" in out, "data rows unchanged")
    check("Intro text stays." in out and "Outro stays." in out, "prose untouched")

    missing_separator = "| A | B |\n| 1 | 2 |\n| 3 | 4 |\n"
    out = drs.normalize_pipe_tables(missing_separator)
    lines = [line for line in out.splitlines() if line]
    check(lines[1] == "|---|---|", "missing separator inserted after header")
    check(len(lines) == 4, "no rows lost when inserting separator")

    ragged = "| A | B | C |\n|---|---|---|\n| 1 | 2 |\n"
    out = drs.normalize_pipe_tables(ragged)
    check("| 1 | 2 |  |" in out, "short row padded to table width")

    well_formed = "| A | B |\n|---|---|\n| 1 | 2 |\n"
    out = drs.normalize_pipe_tables(well_formed)
    check("|---|---|" in out and "| 1 | 2 |" in out, "well-formed table survives")


def test_parse_typed_summary() -> None:
    kind, body = drs.parse_typed_summary("TYPE: table\n| A | B |\n|---|---|\n| 1 | 2 |")
    check(kind == "table" and body.startswith("| A | B |"), "TYPE line parsed and stripped")
    kind, body = drs.parse_typed_summary("Type: PHOTO\nA farmer walks through a barn.")
    check(kind == "photo", "TYPE parsing is case-insensitive")
    kind, body = drs.parse_typed_summary("Just some text without a type line.")
    check(kind == "" and body.startswith("Just some"), "missing TYPE keeps full body")


def test_summary_shape_ok() -> None:
    table = "| A | B |\n|---|---|\n| 1 | 2 |"
    check(drs.summary_shape_ok("table", table), "table shape accepts pipe table")
    check(not drs.summary_shape_ok("table", "values must be added here"), "table shape rejects prose")
    check(drs.summary_shape_ok("photo", "A farmer walks through a barn."), "photo shape accepts sentence")
    check(not drs.summary_shape_ok("photo", "x" * 500), "photo shape rejects walls of text")
    check(drs.summary_shape_ok("chart", "Milk: 36%\nDairy: 18%"), "chart shape accepts label:value lines")
    check(not drs.summary_shape_ok("diagram", "one line only"), "diagram shape rejects single label-less line")


def test_picture_summary_rect_grows_over_neighbors() -> None:
    picture = [0.03, 0.6, 0.97, 0.96]
    cells = [
        # title just above, horizontally inside the picture
        {"id": "b1", "rect": [0.05, 0.575, 0.20, 0.59], "text": "Breakdown title", "is_heading": False},
        # unrelated paragraph far above
        {"id": "b2", "rect": [0.05, 0.10, 0.40, 0.30], "text": "Far away", "is_heading": False},
    ]
    grown = drs.picture_summary_rect(picture, cells)
    check(grown[1] <= 0.575, "adjacent title above pulled into the crop")
    check(grown[1] > 0.30, "distant text not pulled into the crop")
    check(grown[3] >= 0.97, "margin added below for pixel-only footer lines")


def test_summarize_pictures_typed_retry() -> None:
    original_call = drs.ollama_client.call_ollama_vlm
    prompts: list[str] = []
    answers = iter(
        [
            "TYPE: table\nThe values must be added here.",  # fails table shape
            "| Scope | Share |\n|---|---|\n| Scope 1 & 2 | 5% |\n| Scope 3 | 31% |",
        ]
    )

    def fake_call(**kwargs):
        prompts.append(kwargs.get("prompt", ""))
        return next(answers), {"total_tokens": 7}

    drs.ollama_client.call_ollama_vlm = fake_call
    try:
        record = make_record(1, area=0.3)
        record.abs_path = "fake.png"
        record.summarize = True
        args = SimpleNamespace(
            skip_vlm=False, ollama_base_url="", ollama_model="", temperature=0.0,
            num_ctx=0, num_predict=0, auto_num_ctx=False,
        )
        drs.summarize_pictures(records=[record], prompt_template=drs.DEFAULT_IMAGE_SUMMARY_PROMPT, args=args)
        check(len(prompts) == 2, "failed table shape triggers one focused retry")
        check("markdown pipe tables only" in prompts[1], "retry uses the focused table prompt")
        check(record.summary_type == "table", "summary type recorded")
        check("| Scope 1 & 2 | 5% |" in record.summary, "retry transcription stored")
        check("TYPE:" not in record.summary, "TYPE line stripped from stored summary")
        check(not record.summary_warnings, "successful retry leaves no warning")
    finally:
        drs.ollama_client.call_ollama_vlm = original_call


def test_summarize_pictures_calls_vlm() -> None:
    original_call = drs.ollama_client.call_ollama_vlm
    calls: list[str] = []

    def fake_call(**kwargs):
        calls.append(str(kwargs.get("image_path")))
        return "Visible values: Scope 1 & 2: 5%, Scope 3: 31%.", {"total_tokens": 9}

    drs.ollama_client.call_ollama_vlm = fake_call
    try:
        record = make_record(1, area=0.3)
        record.abs_path = "fake.png"
        record.summarize = True
        skipped = make_record(2, area=0.001)
        skipped.summarize = False
        args = SimpleNamespace(
            skip_vlm=False, ollama_base_url="", ollama_model="", temperature=0.0,
            num_ctx=0, num_predict=0, auto_num_ctx=False,
        )
        drs.summarize_pictures(records=[record, skipped], prompt_template=drs.DEFAULT_IMAGE_SUMMARY_PROMPT, args=args)
        check(len(calls) == 1, "summary requested only for routed pictures")
        check("Scope 1 & 2: 5%" in record.summary, "summary stored on the record")
        check(not skipped.summary, "skipped picture stays unsummarized")
    finally:
        drs.ollama_client.call_ollama_vlm = original_call


def test_drop_duplicate_subset_tables() -> None:
    verified = drs.TableCandidate(
        candidate_id="tc001",
        kind="layout_region",
        bbox=None,
        verified=True,
        markdown=(
            "| GOALS | TARGETS | 2025 RESULTS |\n| --- | --- | --- |\n"
            "| Goal A | Target A | 42% |\n| Goal B | Target B | 61% |"
        ),
    )
    markdown = (
        "# Page\n\n"
        "| GOALS | TARGETS |\n| --- | --- |\n| Goal A | Target A |\n| Goal B | Target B |\n\n"
        "| GOALS | TARGETS | 2025 RESULTS |\n| --- | --- | --- |\n"
        "| Goal A | Target A | 42% |\n| Goal B | Target B | 61% |\n\n"
        "| Other | Table |\n| --- | --- |\n| keep | me |\n| and | this |\n"
    )
    out, dropped = drs.drop_duplicate_subset_tables(markdown, [verified])
    check(dropped == 1, "column-subset duplicate dropped")
    check("| Goal A | Target A | 42% |" in out, "full verified table kept")
    check("| Goal A | Target A |\n" not in out, "2-column twin removed")
    check("| keep | me |" in out, "unrelated table untouched")

    out, dropped = drs.drop_duplicate_subset_tables(markdown, [])
    check(dropped == 0 and out == markdown, "no verified tables leaves markdown unchanged")

    twin = (
        "# Page\n\n"
        "| GOALS | TARGETS | 2025 RESULTS |\n| --- | --- | --- |\n"
        "| Goal A | Target A | 42% |\n| Goal B | Target B | 61% |\n\n"
        "| GOALS | TARGETS | 2025 RESULTS |\n| --- | --- | --- |\n"
        "| Goal A | Target A | 42% |\n| Goal B | Target B | 61% |\n"
    )
    out, dropped = drs.drop_duplicate_subset_tables(twin, [verified])
    check(dropped == 1, "identical twin deduplicated")
    check(out.count("| Goal A | Target A | 42% |") == 1, "exactly one instance survives")


def test_detector_rejects_timeline() -> None:
    cells: list[dict[str, object]] = []
    for index, x in enumerate((0.05, 0.17, 0.29, 0.41, 0.53, 0.65), start=1):
        cells.append(
            {"id": f"b{index:04d}", "rect": [x, 0.50, x + 0.05, 0.515], "text": str(1929 + index * 10), "is_heading": False}
        )
        cells.append(
            {
                "id": f"b{index + 50:04d}",
                "rect": [x, 0.55, x + 0.09, 0.60],
                "text": f"Milestone description for year {index}",
                "is_heading": False,
            }
        )
    candidates = layout_region_candidates(cells)
    check(not candidates, "timelines are not table candidates")


def test_detector_splits_sections_at_banners() -> None:
    banner = {
        "id": "b0999",
        "rect": [0.05, 0.44, 0.60, 0.46],
        "text": "THRIVING PEOPLE & COMMUNITIES",
        "is_heading": True,
    }
    top = grid_cells()
    bottom = grid_cells(y_offset=0.37, id_offset=100)
    candidates = layout_region_candidates(top + [banner] + bottom)
    check(len(candidates) == 2, "all-caps banner splits stacked sections into two regions")


def test_picture_table_detection_and_hero_rejection() -> None:
    table_record = drs.PictureRecord(
        page=1,
        index=1,
        placeholder="{{DOC_IMAGE_p0001_i001}}",
        rel_path="images/table.png",
        abs_path=None,
        bbox={"l": 100.0, "t": 450.0, "r": 900.0, "b": 620.0},
        area_ratio=0.17,
        classification="",
        caption="",
    )
    hero_record = drs.PictureRecord(
        page=1,
        index=2,
        placeholder="{{DOC_IMAGE_p0001_i002}}",
        rel_path="images/hero.png",
        abs_path=None,
        bbox={"l": 100.0, "t": 50.0, "r": 900.0, "b": 300.0},
        area_ratio=0.25,
        classification="photo",
        caption="",
    )
    candidates = drs.build_table_candidates(
        cells=[],
        page_size=(1000.0, 800.0),
        picture_records={1: table_record, 2: hero_record},
        layout_map={"page_size": [1000.0, 800.0], "blocks": []},
    )
    picture_candidates = [candidate for candidate in candidates if candidate.kind == "picture_table"]
    check(len(picture_candidates) == 1, "only the lower wide picture becomes a table candidate")
    check(picture_candidates[0].picture_index == 1, "picture table candidate keeps picture index")


def test_verify_region_table() -> None:
    cells = ["GOALS", "TARGETS", "Curb emissions", "-21% vs. 2020", "Reach 42% by 2030"]
    good = "| GOALS | TARGETS |\n| --- | --- |\n| Curb emissions | -21% vs. 2020 |\n| | Reach 42% by 2030 |"
    ok, stats = drs.verify_region_table(good, cells)
    check(ok, "faithful table transcription verifies")

    invented = good.replace("42%", "43%")
    ok, stats = drs.verify_region_table(invented, cells)
    check(not ok and stats["fail"] == "invented_numbers", "invented number rejected")

    missing = "| GOALS | TARGETS |\n| --- | --- |\n| Curb emissions | -21% vs. 2020 |"
    ok, stats = drs.verify_region_table(missing, cells)
    check(not ok and stats["fail"] == "missing_numbers", "dropped numbers rejected")

    ok, stats = drs.verify_region_table("No table at all.", cells)
    check(not ok and stats["fail"] == "no_table_structure", "non-table output rejected")

    wordy_cells = ["Category", "Description", "Alpha item", "Beta item", "Gamma item"]
    wordy = "| Category | Description |\n| --- | --- |\n| Alpha item | Beta item |\n| Gamma item | |"
    ok, stats = drs.verify_region_table(wordy, wordy_cells)
    check(ok, "numberless table verified via word coverage")


def region_transcribe_env(answers: list[str]):
    calls: list[str] = []

    def fake_call(**kwargs):
        calls.append(kwargs.get("prompt", ""))
        return answers[min(len(calls) - 1, len(answers) - 1)], {"total_tokens": 10}

    def fake_crop(*, page_image_path, bbox, crop_path, margin=0.01):
        crop_path.parent.mkdir(parents=True, exist_ok=True)
        crop_path.write_bytes(b"png")
        return True

    return calls, fake_call, fake_crop


def make_region_candidate() -> tuple[drs.TableCandidate, list[dict[str, object]]]:
    cells = [
        {"id": "b0001", "rect": [0.1, 0.1, 0.2, 0.12], "text": "Europe", "is_heading": False},
        {"id": "b0002", "rect": [0.3, 0.1, 0.4, 0.12], "text": "+2.3%", "is_heading": False},
    ]
    candidate = drs.TableCandidate(
        candidate_id="tc001",
        kind="layout_region",
        bbox=[0.1, 0.1, 0.4, 0.12],
        source_block_ids=["b0001", "b0002"],
    )
    return candidate, cells


def test_transcribe_verifies_and_persists() -> None:
    answers = ["| Region | Growth |\n| --- | --- |\n| Europe | +2.3% |"]
    calls, fake_call, fake_crop = region_transcribe_env(answers)
    original_call, original_crop = drs.ollama_client.call_ollama_vlm, drs.save_region_crop
    drs.ollama_client.call_ollama_vlm, drs.save_region_crop = fake_call, fake_crop
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            page_dir = drs.Path(temp_dir)
            candidate, cells = make_region_candidate()
            args = SimpleNamespace(
                skip_vlm=False, ollama_base_url="", ollama_model="", temperature=0.0,
                num_ctx=0, num_predict=0, auto_num_ctx=False,
            )
            drs.transcribe_table_candidates(
                candidates=[candidate], cells=cells,
                page_image_path=page_dir / "page.png", page_dir=page_dir, args=args,
            )
            check(candidate.verified, "faithful transcription marks candidate verified")
            check(len(calls) == 1, "verified transcription needs one call")
            check(
                (page_dir / "table_candidates" / "tc001_table.md").exists(),
                "verified table persisted as sidecar",
            )
    finally:
        drs.ollama_client.call_ollama_vlm, drs.save_region_crop = original_call, original_crop


def test_transcribe_retries_then_discards() -> None:
    bad = "| Region | Growth |\n| --- | --- |\n| Europe | +9.9% |"
    calls, fake_call, fake_crop = region_transcribe_env([bad, bad])
    original_call, original_crop = drs.ollama_client.call_ollama_vlm, drs.save_region_crop
    drs.ollama_client.call_ollama_vlm, drs.save_region_crop = fake_call, fake_crop
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            page_dir = drs.Path(temp_dir)
            candidate, cells = make_region_candidate()
            args = SimpleNamespace(
                skip_vlm=False, ollama_base_url="", ollama_model="", temperature=0.0,
                num_ctx=0, num_predict=0, auto_num_ctx=False,
            )
            drs.transcribe_table_candidates(
                candidates=[candidate], cells=cells,
                page_image_path=page_dir / "page.png", page_dir=page_dir, args=args,
            )
            check(not candidate.verified, "unfaithful transcription stays unverified")
            check(len(calls) == 2, "failed verification triggers exactly one retry")
            check("9.9" in calls[1], "retry feedback names the invented number")
            check("verification_failed" in candidate.warnings, "failure recorded as warning")
    finally:
        drs.ollama_client.call_ollama_vlm, drs.save_region_crop = original_call, original_crop


def test_transcribe_respects_vlm_skip_answer() -> None:
    calls, fake_call, fake_crop = region_transcribe_env(["SKIP"])
    original_call, original_crop = drs.ollama_client.call_ollama_vlm, drs.save_region_crop
    drs.ollama_client.call_ollama_vlm, drs.save_region_crop = fake_call, fake_crop
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            page_dir = drs.Path(temp_dir)
            candidate, cells = make_region_candidate()
            args = SimpleNamespace(
                skip_vlm=False, ollama_base_url="", ollama_model="", temperature=0.0,
                num_ctx=0, num_predict=0, auto_num_ctx=False,
            )
            drs.transcribe_table_candidates(
                candidates=[candidate], cells=cells,
                page_image_path=page_dir / "page.png", page_dir=page_dir, args=args,
            )
            check(not candidate.verified, "SKIP answer leaves candidate unverified")
            check(len(calls) == 1, "SKIP answer is not retried")
            check("vlm_skip" in candidate.warnings, "SKIP recorded as warning")
    finally:
        drs.ollama_client.call_ollama_vlm, drs.save_region_crop = original_call, original_crop


def test_repair_regression_reasons() -> None:
    pre = (
        "# Page\n\n| A | B |\n| --- | --- |\n| Goal one | 42% |\n| Goal two | 61% |\n\n"
        "A closing paragraph with details.\n"
    )
    check(
        drs.repair_regression_reasons(pre, pre, {"length_capped": False}) == [],
        "identical repair output passes the guard",
    )
    truncated = "# Page\n\n| A | B |\n| --- | --- |\n| Goal one | 42% |\n"
    reasons = drs.repair_regression_reasons(pre, truncated, None)
    check("fewer_table_rows" in reasons, "lost table rows rejected")
    check("content_loss_vs_pre_repair" in reasons, "lost content rejected")
    reasons = drs.repair_regression_reasons(pre, pre, {"length_capped": True})
    check(reasons == ["length_capped"], "length-capped repair rejected on usage alone")
    padded = truncated + "\n## Unplaced content\n\n- 61%\n- Goal two\n- A closing paragraph with details.\n"
    reasons = drs.repair_regression_reasons(pre, padded, None)
    check("fewer_table_rows" in reasons, "unplaced-content padding does not hide lost rows")


def test_token_usage_includes_table_extraction() -> None:
    usage = drs.summarize_token_usage(
        [
            {
                "page": 1,
                "table_candidates": [
                    {
                        "candidate_id": "tc001",
                        "kind": "picture_table",
                        "usage": {"prompt_tokens": 3, "output_tokens": 4, "total_tokens": 7},
                    }
                ],
            }
        ]
    )
    stages = [call["stage"] for call in usage["calls"]]
    check("table_extraction" in stages, "token usage includes graphical table extraction")


def ro_cell(
    index: int,
    rect: list[float],
    *,
    heading: bool = False,
    kind: str = "paragraph",
    text: str = "x",
) -> dict:
    return {
        "index": index,
        "rect": rect,
        "is_heading": heading,
        "is_picture": kind == "picture",
        "kind": kind,
        "text": text,
    }


def test_reading_order_page8_bands() -> None:
    # danoneurdaccessible page 8: two rule-separated bands (ACTIVITIES,
    # MAIN MARKETS), each with a left text column and a right chart column.
    # Docling read the page column-major and pushed the ACTIVITIES band's
    # right column below the MAIN MARKETS text. Geometry from the saved
    # layout_prompt_map.json; rules from the PDF's vector drawings.
    cells = [
        ro_cell(0, [0.084, 0.096, 0.517, 0.115], heading=True, text="1.1 PRESENTATION OF DANONE"),
        ro_cell(1, [0.084, 0.142, 0.199, 0.157], heading=True, text="ACTIVITIES"),
        ro_cell(2, [0.084, 0.184, 0.49, 0.205]),
        ro_cell(3, [0.084, 0.212, 0.489, 0.308]),
        ro_cell(4, [0.084, 0.316, 0.49, 0.436]),
        ro_cell(5, [0.084, 0.444, 0.489, 0.465]),
        ro_cell(6, [0.084, 0.558, 0.249, 0.572], heading=True, text="MAIN MARKETS"),
        ro_cell(7, [0.084, 0.599, 0.335, 0.608]),
        ro_cell(8, [0.084, 0.615, 0.489, 0.649]),
        ro_cell(9, [0.084, 0.656, 0.49, 0.703]),
        ro_cell(10, [0.084, 0.71, 0.49, 0.769]),
        ro_cell(11, [0.084, 0.776, 0.489, 0.797]),
        ro_cell(12, [0.084, 0.805, 0.489, 0.851]),
        ro_cell(13, [0.514, 0.184, 0.754, 0.193], heading=True, text="CONSOLIDATED SALES BY CATEGORY"),
        ro_cell(14, [0.514, 0.199, 0.586, 0.208]),
        ro_cell(15, [0.54, 0.225, 0.884, 0.392], kind="picture"),
        ro_cell(16, [0.514, 0.419, 0.92, 0.44]),
        ro_cell(17, [0.514, 0.447, 0.762, 0.457]),
        ro_cell(18, [0.514, 0.464, 0.837, 0.473]),
        ro_cell(19, [0.514, 0.48, 0.744, 0.489]),
        ro_cell(20, [0.514, 0.496, 0.747, 0.506]),
        ro_cell(21, [0.514, 0.512, 0.776, 0.522]),
        ro_cell(22, [0.533, 0.64, 0.874, 0.838], kind="picture"),
        ro_cell(23, [0.514, 0.599, 0.828, 0.624], kind="caption", text="CONSOLIDATED SALES BY GEOGRAPHICAL ZONE (in € millions)"),
    ]
    dividers = {"h": [[0.161, 0.084, 0.916], [0.577, 0.084, 0.916]], "v": []}
    order = drs.reading_order_permutation(cells, dividers)
    expected = list(range(6)) + list(range(13, 22)) + list(range(6, 13)) + [23, 22]
    check(order == expected, "page 8: bands complete before MAIN MARKETS starts")
    check(order[-2:] == [23, 22], "page 8: chart title caption precedes its picture")


def test_reading_order_page14_bands() -> None:
    # danoneurdaccessible page 14: NORTH AMERICA and CNAO bands, rule under
    # each banner. Docling interleaved the NA right column into the CNAO band.
    cells = [
        ro_cell(0, [0.083, 0.034, 0.435, 0.045], heading=True, text="OVERVIEW OF ACTIVITIES, RISK FACTORS"),
        ro_cell(1, [0.083, 0.052, 0.334, 0.061]),
        ro_cell(2, [0.084, 0.095, 0.264, 0.11], heading=True, text="NORTH AMERICA"),
        ro_cell(3, [0.084, 0.138, 0.343, 0.151], heading=True, text="Market and Zone description"),
        ro_cell(4, [0.084, 0.159, 0.489, 0.192]),
        ro_cell(5, [0.084, 0.2, 0.489, 0.221]),
        ro_cell(6, [0.084, 0.228, 0.489, 0.275]),
        ro_cell(7, [0.084, 0.282, 0.49, 0.328], text=""),
        ro_cell(8, [0.084, 0.336, 0.49, 0.369]),
        ro_cell(9, [0.084, 0.377, 0.49, 0.398]),
        ro_cell(10, [0.084, 0.405, 0.489, 0.427]),
        ro_cell(11, [0.084, 0.459, 0.505, 0.474], heading=True, text="CNAO (CHINA, NORTH ASIA & OCEANIA)"),
        ro_cell(12, [0.084, 0.502, 0.343, 0.515], heading=True, text="Market and Zone description"),
        ro_cell(13, [0.084, 0.523, 0.489, 0.594]),
        ro_cell(14, [0.084, 0.605, 0.489, 0.639]),
        ro_cell(15, [0.084, 0.646, 0.49, 0.754], text=""),
        ro_cell(16, [0.084, 0.762, 0.49, 0.796]),
        ro_cell(17, [0.514, 0.137, 0.92, 0.208]),
        ro_cell(18, [0.514, 0.219, 0.92, 0.277]),
        ro_cell(19, [0.514, 0.293, 0.591, 0.305], heading=True, text="Strategy"),
        ro_cell(20, [0.514, 0.313, 0.92, 0.397], text=""),
        ro_cell(21, [0.533, 0.501, 0.92, 0.559], text=""),
        ro_cell(22, [0.514, 0.567, 0.92, 0.663]),
        ro_cell(23, [0.514, 0.674, 0.92, 0.782]),
    ]
    dividers = {"h": [[0.114, 0.084, 0.916], [0.478, 0.084, 0.916]], "v": []}
    order = drs.reading_order_permutation(cells, dividers)
    expected = [0, 1] + list(range(2, 11)) + [17, 18, 19, 20] + list(range(11, 17)) + [21, 22, 23]
    check(order == expected, "page 14: NA band complete before CNAO band starts")
    check(order[:2] == [0, 1], "page 14: pinned page header stays first")


def test_reading_order_vertical_divider_panels() -> None:
    # Two side-by-side panels Docling read row-major. A drawn vertical rule
    # (or a wide whitespace gutter) groups each panel before the other.
    def panels(right_x0: float) -> list[dict]:
        rows = [(0.10, 0.22), (0.24, 0.36), (0.38, 0.50)]
        cells = []
        for row, (top, bottom) in enumerate(rows):
            cells.append(ro_cell(2 * row, [0.05, top, 0.493, bottom]))
            cells.append(ro_cell(2 * row + 1, [right_x0, top, 0.95, bottom]))
        return cells

    v_rule = {"h": [], "v": [[0.5, 0.08, 0.52]]}
    order = drs.reading_order_permutation(panels(0.507), v_rule)
    check(order == [0, 2, 4, 1, 3, 5], "vertical rule groups panels before rows")
    check(
        drs.reading_order_permutation(panels(0.507), {"h": [], "v": []}) is None,
        "narrow gutter without a rule leaves row order unchanged",
    )
    order = drs.reading_order_permutation(panels(0.55), {"h": [], "v": []})
    check(order == [0, 2, 4, 1, 3, 5], "wide whitespace gutter groups panels")


def test_reading_order_rule_adopts_heading() -> None:
    # A full-width rule under a (narrow, mixed-case) section heading cuts the
    # page into bands above the heading; without the rule, or without the
    # heading (a table row separator), nothing changes.
    def cells(second_is_heading: bool) -> list[dict]:
        return [
            ro_cell(0, [0.08, 0.10, 0.30, 0.115], heading=True, text="Section one"),
            ro_cell(1, [0.08, 0.13, 0.48, 0.30]),
            ro_cell(2, [0.08, 0.33, 0.30, 0.345], heading=second_is_heading, text="Section two"),
            ro_cell(3, [0.08, 0.37, 0.48, 0.55]),
            ro_cell(4, [0.52, 0.13, 0.92, 0.30]),
            ro_cell(5, [0.52, 0.37, 0.92, 0.55]),
        ]

    rule = {"h": [[0.35, 0.08, 0.92]], "v": []}
    order = drs.reading_order_permutation(cells(True), rule)
    check(order == [0, 1, 4, 2, 3, 5], "rule under heading cuts bands above the heading")
    check(
        drs.reading_order_permutation(cells(True), {"h": [], "v": []}) is None,
        "without the rule the order is unchanged",
    )
    check(
        drs.reading_order_permutation(cells(False), rule) is None,
        "heading-less rule (row separator) does not cut",
    )


def test_reading_order_caption_position() -> None:
    # Docling streams captions after their picture. A caption sitting visually
    # above the picture (chart title) must be read first; a caption below the
    # picture (classic figure caption) must stay after it.
    def cells(caption_rect: list[float]) -> list[dict]:
        return [
            ro_cell(0, [0.08, 0.10, 0.30, 0.115], heading=True, text="Section one"),
            ro_cell(1, [0.08, 0.13, 0.48, 0.30]),
            ro_cell(2, [0.08, 0.33, 0.30, 0.345], heading=True, text="Section two"),
            ro_cell(3, [0.08, 0.37, 0.48, 0.55]),
            ro_cell(4, [0.52, 0.13, 0.92, 0.30]),
            ro_cell(5, [0.52, 0.40, 0.92, 0.52], kind="picture"),
            ro_cell(6, caption_rect, kind="caption"),
        ]

    rule = {"h": [[0.35, 0.08, 0.92]], "v": []}
    order = drs.reading_order_permutation(cells([0.52, 0.37, 0.92, 0.39]), rule)
    check(order == [0, 1, 4, 2, 3, 6, 5], "caption above picture is read before it")
    order = drs.reading_order_permutation(cells([0.52, 0.53, 0.92, 0.55]), rule)
    check(order == [0, 1, 4, 2, 3, 5, 6], "caption below picture stays after it")


def test_reading_order_identity_for_plain_columns() -> None:
    # Continuous two-column prose without dividers: Docling's column-major
    # order must come back unchanged (None) even though a narrow gutter exists.
    cells = [
        ro_cell(0, [0.08, 0.10, 0.35, 0.12], heading=True, text="Introduction"),
        ro_cell(1, [0.08, 0.14, 0.488, 0.40]),
        ro_cell(2, [0.08, 0.42, 0.488, 0.70]),
        ro_cell(3, [0.512, 0.10, 0.92, 0.38]),
        ro_cell(4, [0.512, 0.40, 0.92, 0.68]),
    ]
    check(
        drs.reading_order_permutation(cells, {"h": [], "v": []}) is None,
        "plain two-column page keeps Docling order",
    )


def test_divider_segments_from_drawings() -> None:
    point = lambda x, y: SimpleNamespace(x=x, y=y)  # noqa: E731
    rect = lambda x0, y0, x1, y1: SimpleNamespace(x0=x0, y0=y0, x1=x1, y1=y1)  # noqa: E731
    drawings = [
        {"items": [("l", point(59.4, 300.0), point(500.0, 300.4))]},
        {"items": [("l", point(505.0, 300.2), point(940.0, 300.0))]},
        {"items": [("re", rect(500.0, 80.0, 501.5, 700.0))]},
        {"items": [("l", point(100.0, 50.0), point(115.0, 50.0))]},
        {"items": [("re", rect(100.0, 400.0, 800.0, 460.0))]},
    ]
    segments = drs.divider_segments_from_drawings(drawings, (1000.0, 800.0))
    check(len(segments["h"]) == 1, "split h-strokes merged into one rule")
    y, x0, x1 = segments["h"][0]
    check(abs(y - 0.375) < 0.01 and x0 < 0.06 and x1 > 0.93, "merged h-rule spans both strokes")
    check(len(segments["v"]) == 1, "thin tall rect becomes a v-rule")
    x, y0, y1 = segments["v"][0]
    check(abs(x - 0.5) < 0.01 and abs(y0 - 0.1) < 0.01 and abs(y1 - 0.875) < 0.01, "v-rule normalized")


def test_reorder_items_for_reading_order() -> None:
    # Item-level wrapper: pinned margin furniture stays leading, an item
    # without geometry stays glued to its predecessor, bands reorder.
    items = [
        fake_item(text="Running header", bbox=(80, 16, 300, 40)),
        fake_item(text="Alpha", label="section_header", bbox=(80, 100, 300, 112)),
        fake_item(text="Left one", bbox=(80, 120, 480, 240)),
        fake_item(text="Beta", label="section_header", bbox=(80, 264, 300, 276)),
        fake_item(text="Left two", bbox=(80, 296, 480, 440)),
        fake_item(text="Right one", bbox=(520, 120, 920, 240)),
        SimpleNamespace(text="No geometry", label="text", caption="", prov=[]),
        fake_item(text="Right two", bbox=(520, 296, 920, 440)),
    ]
    dividers = {"h": [[0.35, 0.08, 0.92]], "v": []}
    out, info = drs.reorder_items_for_reading_order(items, (1000.0, 800.0), dividers)
    check(info["applied"], "reorder applied for banded page")
    texts = [drs.item_text(item) for item in out]
    check(
        texts == ["Running header", "Alpha", "Left one", "Right one", "No geometry", "Beta", "Left two", "Right two"],
        "bands reordered, furniture pinned, rect-less item glued",
    )
    check(info["moved_items"] == 4, "moved item count reported")

    same, info = drs.reorder_items_for_reading_order(items, (1000.0, 800.0), {"h": [], "v": []})
    check(same is items and not info["applied"], "no dividers: original item list returned")


def test_export_page_markdown_item_order() -> None:
    picture = fake_item(label="picture", bbox=(10, 10, 200, 200))
    para = fake_item(text="Body text", label="text", bbox=(10, 210, 200, 400))
    heading = fake_item(text="Alpha", label="section_header", bbox=(10, 420, 200, 440))
    heading.level = 1
    records = {id(picture): make_record(1)}

    class FakeDocument:
        def export_to_markdown(self, **kwargs):
            return "# docling order\n"

    out = drs.export_page_markdown(
        FakeDocument(), 1, [para, picture, heading], records, use_docling_order=False
    )
    check("docling order" not in out, "reordered page skips the Docling serializer")
    check("<!-- image -->" in out, "picture serialized as the standard image marker")
    check(out.index("Body text") < out.index("<!-- image -->"), "items serialized in given order")
    check("## Alpha" in out, "section_header level 1 renders as ## like Docling")


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILED:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
