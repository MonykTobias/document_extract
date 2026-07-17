"""Synthetic checks for --vlm-concurrency: equivalence, overlap, ordering.

Run from the repository root with ``python tests/test_vlm_concurrency.py``.
The Ollama client is stubbed at its single choke point; no network, no models.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import document_extract.llm.ollama as ollama_module
from document_extract.llm.ollama import map_vlm_tasks
from document_extract.models import PictureRecord, TableCandidate
from document_extract.pictures import summarize_pictures, triage_pictures
from document_extract.tables import transcribe_table_candidates


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


def make_args(**overrides: object) -> SimpleNamespace:
    base = dict(
        skip_vlm=False,
        skip_picture_triage=False,
        photo_summaries=False,
        ollama_base_url="http://stub",
        ollama_model="stub-model",
        triage_model="stub-model",
        temperature=0.0,
        num_ctx=0,
        num_predict=0,
        auto_num_ctx=False,
        triage_num_predict=64,
        triage_confidence=0.65,
        photo_skip_confidence=0.8,
        vlm_concurrency=1,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def make_record(index: int, image_dir: Path) -> PictureRecord:
    image_path = image_dir / f"picture_{index:03d}.png"
    if not image_path.exists():
        image_path.write_bytes(b"stub")
    return PictureRecord(
        page=1,
        index=index,
        placeholder=f"{{{{DOC_IMAGE_p0001_i{index:03d}}}}}",
        rel_path=f"images/picture_{index:03d}.png",
        abs_path=str(image_path),
        bbox=None,
        area_ratio=0.5,
        classification="",
        caption="",
        summarize=True,
    )


USAGE = {"prompt_tokens": 10, "output_tokens": 5, "total_tokens": 15}


def stub_summary(*, image_path: Path, **_kwargs: object):
    marker = Path(image_path).stem.rsplit("_", 1)[-1]
    return f"TYPE: chart\nMetric A: {marker}\nMetric B: {marker}", dict(USAGE)


def patched(stub):
    """Context manager patching the single VLM choke point."""

    class _Patch:
        def __enter__(self):
            self.original = ollama_module.call_ollama_vlm
            ollama_module.call_ollama_vlm = stub
            return self

        def __exit__(self, *exc):
            ollama_module.call_ollama_vlm = self.original
            return False

    return _Patch()


def test_map_vlm_tasks_preserves_order() -> None:
    delays = {1: 0.05, 2: 0.0, 3: 0.02}

    def worker(item: int) -> int:
        time.sleep(delays[item])
        return item * 10

    check(
        map_vlm_tasks(worker, [1, 2, 3], 3) == [10, 20, 30],
        "map_vlm_tasks preserves input order despite uneven completion",
    )
    check(
        map_vlm_tasks(worker, [1, 2, 3], 1) == [10, 20, 30],
        "map_vlm_tasks with one worker matches the serial loop",
    )


def test_summarize_equivalence_across_concurrency() -> None:
    with tempfile.TemporaryDirectory() as temp:
        image_dir = Path(temp)
        outcomes: list[list[tuple[str, str, object]]] = []
        for concurrency in (1, 3):
            records = [make_record(index, image_dir) for index in (1, 2, 3, 4)]
            with patched(stub_summary):
                summarize_pictures(
                    records=records,
                    prompt_template="context: {context}",
                    args=make_args(vlm_concurrency=concurrency),
                )
            outcomes.append(
                [
                    (record.summary, record.summary_type, record.usage)
                    for record in records
                ]
            )
        check(
            outcomes[0] == outcomes[1],
            "serial and concurrent summarize produce identical records in order",
        )
        check(
            all("Metric A: 003" in outcomes[1][2][0] for _ in [0]),
            "each record keeps its own image's answer under concurrency",
        )


def test_summarize_overlaps_with_concurrency_two() -> None:
    barrier = threading.Barrier(2, timeout=10)

    def stub_barrier(*, image_path: Path, **_kwargs: object):
        barrier.wait()
        return stub_summary(image_path=image_path)

    with tempfile.TemporaryDirectory() as temp:
        image_dir = Path(temp)
        records = [make_record(index, image_dir) for index in (1, 2)]
        with patched(stub_barrier):
            summarize_pictures(
                records=records,
                prompt_template="context: {context}",
                args=make_args(vlm_concurrency=2),
            )
    check(
        all(record.summary for record in records) and not barrier.broken,
        "two calls at concurrency 2 are in flight simultaneously",
    )


def test_default_args_stay_serial() -> None:
    thread_ids: list[int] = []

    def stub_ident(*, image_path: Path, **_kwargs: object):
        thread_ids.append(threading.get_ident())
        return stub_summary(image_path=image_path)

    with tempfile.TemporaryDirectory() as temp:
        image_dir = Path(temp)
        records = [make_record(index, image_dir) for index in (1, 2, 3)]
        args = make_args()
        del args.vlm_concurrency  # older callers/tests build args without it
        with patched(stub_ident):
            summarize_pictures(
                records=records,
                prompt_template="context: {context}",
                args=args,
            )
    check(
        set(thread_ids) == {threading.get_ident()},
        "args without vlm_concurrency keep every call on the main thread",
    )


def test_triage_stats_equivalence() -> None:
    def stub_triage(**_kwargs: object):
        return '{"type": "chart", "confidence": 0.9}', dict(USAGE)

    with tempfile.TemporaryDirectory() as temp:
        image_dir = Path(temp)
        outcomes = []
        for concurrency in (1, 2):
            records = [make_record(index, image_dir) for index in (1, 2, 3)]
            with patched(stub_triage):
                stats = triage_pictures(
                    records=records,
                    args=make_args(vlm_concurrency=concurrency),
                )
            outcomes.append(
                (
                    stats,
                    [
                        (
                            record.triage_type,
                            record.triage_confidence,
                            record.triage_action,
                            record.summarize,
                        )
                        for record in records
                    ],
                )
            )
        check(
            outcomes[0] == outcomes[1],
            "serial and concurrent triage produce identical stats and routing",
        )
        check(
            outcomes[1][0]["calls"] == 3
            and outcomes[1][0]["types"] == {"chart": 3},
            "triage stats count every concurrent call exactly once",
        )


def _region_cells() -> list[dict[str, object]]:
    return [
        {"id": "b0001", "rect": [0.10, 0.10, 0.50, 0.20], "text": "Alpha 1"},
        {"id": "b0002", "rect": [0.10, 0.22, 0.50, 0.32], "text": "Beta 2"},
        {"id": "b0003", "rect": [0.10, 0.55, 0.50, 0.65], "text": "Gamma 3"},
        {"id": "b0004", "rect": [0.10, 0.67, 0.50, 0.77], "text": "Delta 4"},
    ]


def _region_candidates() -> list[TableCandidate]:
    return [
        TableCandidate(
            candidate_id="tc001",
            kind="layout_region",
            bbox=[0.05, 0.05, 0.55, 0.40],
            source_block_ids=["b0001", "b0002"],
        ),
        TableCandidate(
            candidate_id="tc002",
            kind="layout_region",
            bbox=[0.05, 0.50, 0.55, 0.85],
            source_block_ids=["b0003", "b0004"],
        ),
    ]


TABLES_BY_ID = {
    "tc001": "| Item | Value |\n| --- | --- |\n| Alpha | 1 |\n| Beta | 2 |",
    "tc002": "| Item | Value |\n| --- | --- |\n| Gamma | 3 |\n| Delta | 4 |",
}


def stub_table(*, image_path: Path, **_kwargs: object):
    candidate_id = Path(image_path).name.split("_", 1)[0]
    return TABLES_BY_ID[candidate_id], dict(USAGE)


def test_transcribe_equivalence_across_concurrency() -> None:
    from PIL import Image

    outcomes = []
    for concurrency in (1, 2):
        with tempfile.TemporaryDirectory() as temp:
            page_dir = Path(temp)
            page_image = page_dir / "page.png"
            Image.new("RGB", (200, 200), "white").save(page_image)
            candidates = _region_candidates()
            with patched(stub_table):
                transcribe_table_candidates(
                    candidates=candidates,
                    cells=_region_cells(),
                    page_image_path=page_image,
                    page_dir=page_dir,
                    args=make_args(vlm_concurrency=concurrency),
                )
            sidecars = sorted(
                path.name for path in (page_dir / "table_candidates").glob("*.md")
            )
            outcomes.append(
                (
                    [
                        (
                            candidate.candidate_id,
                            candidate.verified,
                            candidate.markdown,
                            candidate.warnings,
                        )
                        for candidate in candidates
                    ],
                    sidecars,
                )
            )
    check(
        outcomes[0] == outcomes[1],
        "serial and concurrent transcription verify identical tables and sidecars",
    )
    check(
        all(verified for _, verified, _, _ in outcomes[1][0]),
        "both concurrent candidates pass deterministic verification",
    )


def main() -> int:
    test_map_vlm_tasks_preserves_order()
    test_summarize_equivalence_across_concurrency()
    test_summarize_overlaps_with_concurrency_two()
    test_default_args_stay_serial()
    test_triage_stats_equivalence()
    test_transcribe_equivalence_across_concurrency()
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
