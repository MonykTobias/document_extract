"""Focused checks for the package boundary and configuration layer.

Run from the repository root with ``python tests/test_package.py``.
"""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from document_extract.cli import parse_args
from document_extract.config import argv_with_config_defaults, config_from_mapping, load_config
from document_extract.prompts import DEFAULT_IMAGE_SUMMARY_PROMPT, load_prompt


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


@contextmanager
def clean_config_environment():
    saved = {
        name: value
        for name, value in os.environ.items()
        if name in {"OLLAMA_BASE_URL", "OLLAMA_MODEL", "OLLAMA_TRIAGE_MODEL"}
        or name.startswith("DOCLING_RAG_")
    }
    for name in saved:
        os.environ.pop(name)
    try:
        yield
    finally:
        for name in tuple(os.environ):
            if name in saved or name.startswith("DOCLING_RAG_"):
                os.environ.pop(name)
        os.environ.update(saved)


def main() -> int:
    with clean_config_environment():
        defaults = load_config()
        check(defaults.runtime.dpi == 200, "bundled runtime defaults load")
        check(defaults.models.num_ctx == 8192, "bundled model defaults load")
        check(
            load_prompt("picture_generic.md") == DEFAULT_IMAGE_SUMMARY_PROMPT,
            "picture prompt resource is verbatim",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            overlay = Path(temp_dir) / "overlay.yaml"
            overlay.write_text(
                "models:\n  num_ctx: 321\npictures:\n  min_area_ratio: 0.02\n",
                encoding="utf-8",
            )
            merged = load_config([overlay])
            check(merged.models.num_ctx == 321, "user YAML overrides bundled model config")
            check(merged.pictures.min_area_ratio == 0.02, "user YAML overrides detection config")

        os.environ["OLLAMA_BASE_URL"] = "http://example.test:11434"
        env_config = load_config()
        check(env_config.models.base_url == os.environ["OLLAMA_BASE_URL"], "environment overrides YAML")
        os.environ.pop("OLLAMA_BASE_URL")

        args = parse_args(
            argv_with_config_defaults(defaults, ["x.pdf", "--ollama-model", "USER_MODEL"])
        )
        check(
            not args.triage_model and (args.triage_model or args.ollama_model) == "USER_MODEL",
            "triage falls back to the CLI Ollama model",
        )
        configured = config_from_mapping({"models": {"triage_model": "TRIAGE_X"}})
        check(
            parse_args(argv_with_config_defaults(configured, ["x.pdf"])).triage_model == "TRIAGE_X",
            "configured triage model is still injected",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
