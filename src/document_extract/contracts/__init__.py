"""The frozen current run and progress contracts.

One vocabulary shared by this producer, the ``claim_evidence`` backend, and the
``gw_detector_v2`` frontend. The JSON Schema documents next to this module are
the human-readable statement of each contract; the validators here are what
actually enforces it, so a producer and a consumer cannot drift apart while
both still "look valid".

Two rules make the contract usable without a compatibility layer:

* an unknown key is rejected, never ignored -- a field a producer invents must
  not be silently dropped by a consumer that then reports success;
* an unknown ``contract_version`` is rejected with an instruction to
  re-extract. There is no reader for superseded output.

Shipped inside the package rather than at the repository root so an installed
consumer can reach the same checked-in examples without a checkout path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

CONTRACTS_DIR = Path(__file__).parent

RUN_CONTRACT = "document_extract/run"
RUN_CONTRACT_VERSION = "1.0"
PROGRESS_CONTRACT = "document_extract/progress"
PROGRESS_CONTRACT_VERSION = "1.0"

COORDINATE_CONVENTION = "normalized_top_left"

# Artifact roles a page directory publishes, and the file name each role maps
# to. `claim_evidence` hashes exactly the evidence-bearing ones into a version
# fingerprint, so adding a role here changes what a rebuild is triggered by.
PAGE_ARTIFACT_ROLES: dict[str, str] = {
    "page_markdown": "docling_final.md",
    "page_image": "page.png",
    "layout_map": "layout_prompt_map.json",
    "table_candidates": "table_candidates.json",
    "page_state": "page_state.json",
    "image_summaries": "image_summaries.jsonl",
}
ROOT_ARTIFACT_ROLES: dict[str, str] = {
    "run": "run.json",
    "manifest": "manifest.json",
    "blocks": "blocks.jsonl",
}
# Everything a citation, a geometry, or a visual re-verification can depend on.
# `image_summaries` is optional on a page with no pictures and is hashed as
# absent there rather than skipped.
EVIDENCE_BEARING_ROLES: tuple[str, ...] = (
    "manifest",
    "blocks",
    "page_markdown",
    "page_image",
    "layout_map",
    "table_candidates",
    "page_state",
    "image_summaries",
)
OPTIONAL_PAGE_ROLES = frozenset({"image_summaries"})

PROGRESS_EVENTS = ("run", "page", "page_stage")
PROGRESS_STATUSES = ("start", "ok", "skip", "fail", "resume")


class ContractError(ValueError):
    """A payload does not satisfy the current contract.

    The message names the offending key or version and never echoes a value:
    these payloads carry file names and model output.
    """


# --- small structural validator ---------------------------------------------
#
# A specification is a mapping of key -> (required, type or nested spec). Fewer
# than a hundred lines, no new dependency, and exact about the one thing the
# contract needs to be exact about: an unknown key is an error.

_Spec = dict[str, tuple[bool, Any]]


def _check(payload: Any, spec: _Spec, where: str) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ContractError(f"{where} must be an object")
    unknown = sorted(set(payload) - set(spec))
    if unknown:
        raise ContractError(f"{where} has unknown key(s): {', '.join(unknown)}")
    missing = sorted(key for key, (required, _) in spec.items() if required and key not in payload)
    if missing:
        raise ContractError(f"{where} is missing required key(s): {', '.join(missing)}")
    for key, value in payload.items():
        _required, expected = spec[key]
        if isinstance(expected, dict):
            _check(value, expected, f"{where}.{key}")
        elif expected is not None and not isinstance(value, expected):
            if value is None and not _required:
                continue
            raise ContractError(f"{where}.{key} has the wrong type")
    return dict(payload)


def _hex64(value: Any, where: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        c not in "0123456789abcdef" for c in value
    ):
        raise ContractError(f"{where} must be a lowercase 64-character SHA-256")


# --- run contract -----------------------------------------------------------

_RUN_SPEC: _Spec = {
    "contract": (True, str),
    "contract_version": (True, str),
    "generated_at": (True, str),
    "source": (True, {
        "filename": (True, str),
        "sha256": (True, str),
        "page_count": (True, int),
    }),
    "selection": (True, {
        "start_page": (True, int),
        "end_page": (True, int),
        "page_count": (True, int),
        "complete_document": (True, bool),
    }),
    "coordinate_convention": (True, str),
    "artifacts": (True, {
        "root": (True, dict),
        "page": (True, dict),
    }),
    "evidence_bearing_roles": (True, list),
    "settings": (True, dict),
    "pages": (True, {
        "completed": (True, int),
        "failed": (True, int),
    }),
    "warnings": (True, {
        "total": (True, int),
        "categories": (True, dict),
        "stale_page_dirs": (True, list),
    }),
}


def validate_run(payload: Any) -> dict[str, Any]:
    """Validate one ``run.json`` payload, or raise ``ContractError``."""
    data = _check(payload, _RUN_SPEC, "run")
    if data["contract"] != RUN_CONTRACT:
        raise ContractError(f"run.contract must be {RUN_CONTRACT!r}")
    if data["contract_version"] != RUN_CONTRACT_VERSION:
        raise ContractError(
            f"run contract version {data['contract_version']!r} is not the current "
            f"{RUN_CONTRACT_VERSION!r}; re-extract the document"
        )
    if data["coordinate_convention"] != COORDINATE_CONVENTION:
        raise ContractError(f"run.coordinate_convention must be {COORDINATE_CONVENTION!r}")
    _hex64(data["source"]["sha256"], "run.source.sha256")

    selection = data["selection"]
    if not 1 <= selection["start_page"] <= selection["end_page"]:
        raise ContractError("run.selection must be a 1-based non-empty page range")
    if selection["page_count"] != selection["end_page"] - selection["start_page"] + 1:
        raise ContractError("run.selection.page_count does not match its own range")
    if selection["end_page"] > data["source"]["page_count"]:
        raise ContractError("run.selection.end_page exceeds the source page count")
    if selection["complete_document"] != (
        selection["start_page"] == 1 and selection["end_page"] == data["source"]["page_count"]
    ):
        raise ContractError("run.selection.complete_document contradicts its page range")

    for scope, expected in (("root", ROOT_ARTIFACT_ROLES), ("page", PAGE_ARTIFACT_ROLES)):
        published = data["artifacts"][scope]
        if set(published) != set(expected):
            raise ContractError(f"run.artifacts.{scope} does not publish the current role set")
        for role, relative in published.items():
            _relative(relative, f"run.artifacts.{scope}.{role}")

    roles = data["evidence_bearing_roles"]
    if list(roles) != list(EVIDENCE_BEARING_ROLES):
        raise ContractError("run.evidence_bearing_roles is not the current role list")
    if data["pages"]["failed"]:
        raise ContractError("run.pages.failed must be 0; a failed page is not indexable")
    return data


def _relative(value: Any, where: str) -> None:
    """A published artifact path is relative, forward-slashed, and contained."""
    if not isinstance(value, str) or not value:
        raise ContractError(f"{where} must be a non-empty relative path")
    if "\\" in value or value.startswith("/") or ".." in Path(value).parts:
        raise ContractError(f"{where} must be a contained posix relative path")
    if Path(value).is_absolute() or Path(value).anchor:
        raise ContractError(f"{where} must not be absolute")


# --- progress contract ------------------------------------------------------

_PROGRESS_SPEC: _Spec = {
    "contract": (True, str),
    "contract_version": (True, str),
    "event": (True, str),
    "status": (True, str),
    "stage": (False, str),
    "page": (False, int),
    "page_index": (False, int),
    "total_pages": (False, int),
    "seconds": (False, (int, float)),
    "error_type": (False, str),
    "detail": (False, dict),
}


def validate_progress_event(payload: Any) -> dict[str, Any]:
    """Validate one JSONL progress event, or raise ``ContractError``."""
    data = _check(payload, _PROGRESS_SPEC, "progress")
    if data["contract"] != PROGRESS_CONTRACT:
        raise ContractError(f"progress.contract must be {PROGRESS_CONTRACT!r}")
    if data["contract_version"] != PROGRESS_CONTRACT_VERSION:
        raise ContractError(
            f"progress contract version {data['contract_version']!r} is not the "
            f"current {PROGRESS_CONTRACT_VERSION!r}"
        )
    if data["event"] not in PROGRESS_EVENTS:
        raise ContractError(f"progress.event must be one of {', '.join(PROGRESS_EVENTS)}")
    if data["status"] not in PROGRESS_STATUSES:
        raise ContractError(f"progress.status must be one of {', '.join(PROGRESS_STATUSES)}")
    if data["event"] in ("page", "page_stage") and "page" not in data:
        raise ContractError("a page event must carry its page number")
    if data["event"] == "page_stage" and not data.get("stage"):
        raise ContractError("a page_stage event must name its stage")
    for key in ("page", "page_index", "total_pages"):
        if isinstance(data.get(key), int) and data[key] < 1:
            raise ContractError(f"progress.{key} must be 1-based")
    return data


def parse_progress_line(line: str) -> dict[str, Any] | None:
    """Read one stdout line as a progress event, or ``None`` for log text.

    This is the only supported way to follow an extraction: a consumer that
    parses the human status lines instead is reading a rendering, not a
    contract, and breaks the first time a message is reworded.
    """
    line = line.strip()
    if not line.startswith("{") or PROGRESS_CONTRACT not in line:
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("contract") != PROGRESS_CONTRACT:
        return None
    return validate_progress_event(payload)


# --- checked-in examples ----------------------------------------------------


def example_path(*parts: str) -> Path:
    return CONTRACTS_DIR.joinpath(*parts)


def load_example(*parts: str) -> Any:
    return json.loads(example_path(*parts).read_text(encoding="utf-8"))


def iter_examples(kind: str, valid: bool) -> Iterable[tuple[str, Any]]:
    """Every checked-in example for ``run`` or ``progress``.

    The same files back the producer's tests here, the backend's, and the
    frontend's, which is what stops three repositories from each believing a
    different shape is correct.
    """
    folder = CONTRACTS_DIR / kind / "examples" / ("valid" if valid else "invalid")
    for path in sorted(folder.glob("*.json")):
        yield path.name, json.loads(path.read_text(encoding="utf-8"))


__all__ = [
    "CONTRACTS_DIR",
    "COORDINATE_CONVENTION",
    "EVIDENCE_BEARING_ROLES",
    "OPTIONAL_PAGE_ROLES",
    "PAGE_ARTIFACT_ROLES",
    "PROGRESS_CONTRACT",
    "PROGRESS_CONTRACT_VERSION",
    "PROGRESS_EVENTS",
    "PROGRESS_STATUSES",
    "ROOT_ARTIFACT_ROLES",
    "RUN_CONTRACT",
    "RUN_CONTRACT_VERSION",
    "ContractError",
    "example_path",
    "iter_examples",
    "load_example",
    "parse_progress_line",
    "validate_progress_event",
    "validate_run",
]
