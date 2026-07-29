"""The frozen JSONL progress contract, and what the reporter actually emits.

Run from the repository root with ``python tests/test_progress_contract.py``.
"""

from __future__ import annotations

import contextlib
import io
import json

from document_extract.contracts import (
    PROGRESS_CONTRACT,
    PROGRESS_CONTRACT_VERSION,
    PROGRESS_EVENTS,
    PROGRESS_STATUSES,
    ContractError,
    example_path,
    iter_examples,
    parse_progress_line,
    validate_progress_event,
)
from document_extract.runtime import StatusReporter, emit_progress


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


def emitted(fn) -> list[dict]:
    """Every contract event one call writes to stdout, in order."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        fn()
    return [
        event
        for line in buffer.getvalue().splitlines()
        if (event := parse_progress_line(line)) is not None
    ]


def test_every_valid_example_is_accepted() -> None:
    names = [name for name, payload in iter_examples("progress", valid=True)
             if validate_progress_event(payload)]
    check(len(names) >= 6, f"{len(names)} valid progress examples are accepted")
    check("run_start.json" in names, "a run-level event is part of the contract")


def test_every_invalid_example_is_rejected() -> None:
    rejected = {}
    for name, payload in iter_examples("progress", valid=False):
        try:
            validate_progress_event(payload)
        except ContractError as exc:
            rejected[name] = str(exc)
            continue
        raise AssertionError(f"{name} was accepted but must be rejected")
    check(len(rejected) >= 6, f"{len(rejected)} invalid progress examples are rejected")
    check(
        "unknown key" in rejected["unknown_key.json"],
        "an unknown key is rejected rather than forwarded unreviewed",
    )
    check(
        "2.0" in rejected["unknown_contract_version.json"],
        "an unknown contract version is rejected by version, not by shape",
    )


def test_schema_document_matches_the_validator() -> None:
    schema = json.loads(
        example_path("progress", "progress.schema.json").read_text(encoding="utf-8")
    )
    check(
        schema["properties"]["contract"]["const"] == PROGRESS_CONTRACT
        and schema["properties"]["contract_version"]["const"] == PROGRESS_CONTRACT_VERSION,
        "the schema and the validator name the same contract and version",
    )
    check(
        tuple(schema["properties"]["event"]["enum"]) == PROGRESS_EVENTS
        and tuple(schema["properties"]["status"]["enum"]) == PROGRESS_STATUSES,
        "the schema and the validator share one event/status vocabulary",
    )


def test_a_log_line_is_not_a_progress_event() -> None:
    for line in (
        "[3/494 p0003] TABLE_DETECT ok seconds=1.2",
        "Prefetching pages 1-32 with Docling (32 pages, one parse)...",
        "",
        "{not json at all",
        '{"contract":"something/else","event":"page"}',
    ):
        check(
            parse_progress_line(line) is None,
            f"human log text is not read as progress: {line[:40]!r}",
        )


def test_reporter_publishes_both_renderings() -> None:
    reporter = StatusReporter(page_index=2, total_pages=4, page=7)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        reporter.emit("table_detect", "ok", seconds="1.5", candidates=3)
    lines = buffer.getvalue().splitlines()
    check(len(lines) == 2, "one status line and one contract event are written")
    check(
        lines[0].startswith("[2/4 p0007] TABLE_DETECT ok"),
        "the human status line is unchanged",
    )
    event = parse_progress_line(lines[1])
    check(event is not None, "the second line is a valid contract event")
    check(
        event["event"] == "page_stage" and event["stage"] == "table_detect"
        and event["page"] == 7 and event["page_index"] == 2 and event["total_pages"] == 4,
        "the event carries the page, its position, and the stage",
    )
    check(event["seconds"] == 1.5, "seconds is published as a number, not a string")
    check(event["detail"] == {"candidates": 3}, "extra counts travel in detail")


def test_page_completion_is_a_page_event() -> None:
    reporter = StatusReporter(page_index=1, total_pages=1, page=1)
    events = emitted(lambda: reporter.emit("page", "ok", seconds="12.0"))
    check(len(events) == 1 and events[0]["event"] == "page", "'page' is a page event")
    check("stage" not in events[0], "a page event carries no stage name")


def test_a_failure_publishes_its_type_and_not_its_message() -> None:
    events = emitted(
        lambda: emit_progress(
            "page", "fail", page=3, page_index=3, total_pages=4,
            error="ConnectionError",
            message="connection to postgresql://user:secret@10.0.0.1 refused",
            output_dir=r"C:\Users\Tobia\runtime\outputs",
        )
    )
    check(len(events) == 1, "a failure publishes exactly one event")
    event = events[0]
    check(event["error_type"] == "ConnectionError", "the exception class is published")
    payload = json.dumps(event)
    check("secret" not in payload and "10.0.0.1" not in payload,
          "a raw exception message never reaches the progress stream")
    check("C:\\\\Users" not in payload and "runtime" not in payload,
          "a local path never reaches the progress stream")


def test_an_unpublishable_event_is_dropped_not_raised() -> None:
    """Progress is an observation; watching a run must not be able to fail it."""
    events = emitted(lambda: emit_progress("chunk", "start", total_pages=4))
    check(events == [], "an event outside the contract is dropped silently")
    events = emitted(lambda: emit_progress("page_stage", "ok", page=1))
    check(events == [], "a page_stage event with no stage is dropped silently")


def main() -> int:
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            print(f"\n--- {name} ---")
            function()
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
