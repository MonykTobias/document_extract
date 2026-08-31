"""The frozen run contract: shape, rejection rules, and what the producer writes.

Run from the repository root with ``python tests/test_run_contract.py``.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from document_extract.contracts import (
    COORDINATE_CONVENTION,
    EVIDENCE_BEARING_ROLES,
    PAGE_ARTIFACT_ROLES,
    ROOT_ARTIFACT_ROLES,
    RUN_CONTRACT,
    RUN_CONTRACT_VERSION,
    ContractError,
    example_path,
    iter_examples,
    load_example,
    validate_run,
)
from document_extract.pipeline.runner import write_run_contract
from document_extract.runtime import PageState


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


def test_every_valid_example_is_accepted() -> None:
    names = []
    for name, payload in iter_examples("run", valid=True):
        validate_run(payload)
        names.append(name)
    check(len(names) >= 2, f"{len(names)} valid run examples are accepted")
    check(
        "page_range.json" in names,
        "a partial page range is a valid run, not an error",
    )


def test_every_invalid_example_is_rejected() -> None:
    rejected = []
    for name, payload in iter_examples("run", valid=False):
        try:
            validate_run(payload)
        except ContractError as exc:
            rejected.append((name, str(exc)))
            continue
        raise AssertionError(f"{name} was accepted but must be rejected")
    check(len(rejected) >= 8, f"{len(rejected)} invalid run examples are rejected")
    by_name = dict(rejected)
    check(
        "unknown key" in by_name["unknown_key.json"],
        "an unknown key is named in the rejection, not silently dropped",
    )
    check(
        "re-extract" in by_name["unknown_contract_version.json"],
        "an unknown contract version tells the caller to re-extract",
    )


def test_rejection_message_never_echoes_a_value() -> None:
    payload = load_example("run", "examples", "invalid", "absolute_artifact_path.json")
    leaked = payload["artifacts"]["page"]["page_image"]
    try:
        validate_run(payload)
    except ContractError as exc:
        check(
            leaked not in str(exc),
            "the rejected path is not echoed back in the error message",
        )
        return
    raise AssertionError("an absolute artifact path must be rejected")


def test_schema_document_matches_the_validator() -> None:
    schema = json.loads(
        example_path("run", "run.schema.json").read_text(encoding="utf-8")
    )
    check(
        schema["properties"]["contract"]["const"] == RUN_CONTRACT,
        "the schema and the validator name the same contract",
    )
    check(
        schema["properties"]["contract_version"]["const"] == RUN_CONTRACT_VERSION,
        "the schema and the validator name the same version",
    )
    check(
        schema["properties"]["coordinate_convention"]["const"] == COORDINATE_CONVENTION,
        "the schema states the one coordinate convention",
    )
    check(
        set(schema["required"]) == set(schema["properties"]),
        "every documented run key is required",
    )


def test_role_sets_are_the_ones_the_reader_needs() -> None:
    check(
        set(EVIDENCE_BEARING_ROLES) <= set(PAGE_ARTIFACT_ROLES) | set(ROOT_ARTIFACT_ROLES),
        "every evidence-bearing role is a published artifact role",
    )
    check(
        "page_image" in EVIDENCE_BEARING_ROLES,
        "the page image is evidence-bearing: a visual verdict is taken from it",
    )


def _state(page: int, page_dir: Path, status: str = "completed", **warnings) -> PageState:
    state = PageState(
        schema_version=1,
        pdf_name="report.pdf",
        pdf_size=64,
        page=page,
        page_index=page,
        total_pages=2,
        dpi=200,
        page_dir=str(page_dir),
        page_size=(595.0, 842.0),
    )
    state.status = status
    state.warnings = dict(warnings)
    return state


def test_producer_writes_a_valid_contract() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pdf = root / "report.pdf"
        pdf.write_bytes(b"%PDF-1.7\nnot a real pdf, but real bytes\n")
        pages = []
        for page in (1, 2):
            page_dir = root / f"page_{page:04d}"
            page_dir.mkdir()
            pages.append(_state(page, page_dir, content_loss_guard_triggered=page == 2))
        (root / "page_0009").mkdir()  # a leftover from an earlier run

        args = argparse.Namespace(
            dpi=200, refine_mode="auto", visual_values_mode="enforce",
            ollama_model="vlm:test", triage_model="", triage_confidence=0.5,
            num_ctx=16384, num_predict=4000, temperature=0.0,
            vlm_page_image_max_px=1536,
        )
        path = write_run_contract(
            root, pages,
            pdf_path=pdf, source_page_count=2, selected=[1, 2], args=args,
            prompt_texts={
                "page_refinement": "SENTINEL-PROMPT-BODY-1",
                "page_repair": "SENTINEL-PROMPT-BODY-2",
                "picture_summary": "SENTINEL-PROMPT-BODY-3",
            },
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        validate_run(payload)
        check(path.name == "run.json", "the run contract is written as run.json")
        check(
            payload["source"]["filename"] == "report.pdf"
            and str(root) not in json.dumps(payload),
            "the run contract publishes a file name, never the output path",
        )
        check(
            payload["warnings"]["total"] == 1
            and payload["warnings"]["categories"] == {"content_loss_guard_triggered": 1},
            "a warning flag that is false does not count as a warning",
        )
        check(
            payload["warnings"]["stale_page_dirs"] == ["page_0009"],
            "a page directory this run did not produce is declared, not deleted",
        )
        check(
            payload["settings"]["triage_model"] == "vlm:test",
            "an unset triage model falls back to the extraction model",
        )
        check(
            len(payload["settings"]["prompt_sha256"]) == 3
            and "SENTINEL-PROMPT-BODY" not in json.dumps(payload),
            "prompts are fingerprinted by hash, never published as text",
        )


def test_a_failed_page_is_written_and_then_refused() -> None:
    """The run states the failure; the consumer is what refuses to index it."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pdf = root / "report.pdf"
        pdf.write_bytes(b"%PDF-1.7\n")
        good = root / "page_0001"
        good.mkdir()
        bad = root / "page_0002"
        bad.mkdir()
        args = argparse.Namespace(
            dpi=200, refine_mode="auto", visual_values_mode="off",
            ollama_model="vlm:test", triage_model="vlm:test", triage_confidence=0.5,
            num_ctx=16384, num_predict=4000, temperature=0.0,
            vlm_page_image_max_px=1536,
        )
        path = write_run_contract(
            root,
            [_state(1, good), _state(2, bad, status="failed")],
            pdf_path=pdf, source_page_count=2, selected=[1, 2], args=args,
            prompt_texts={"page_refinement": "a", "page_repair": "b", "picture_summary": "c"},
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        check(payload["pages"]["failed"] == 1, "the run reports its failed page honestly")
        try:
            validate_run(payload)
        except ContractError as exc:
            check("failed" in str(exc), "a run with a failed page is not indexable")
            return
        raise AssertionError("a run with a failed page must be refused by a consumer")


def main() -> int:
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            print(f"\n--- {name} ---")
            function()
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
