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
from document_extract.api import _namespace_from_config
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
        try:
            config_from_mapping({"reading_order": {"column_gutter": 35}})
        except ValueError:
            check(True, "out-of-range column gutter is rejected")
        else:
            raise AssertionError("out-of-range column gutter was accepted")

        api_config = config_from_mapping(
            {
                "runtime": {
                    "dpi": 144,
                    "photo_summaries": True,
                    "skip_picture_triage": True,
                    "divider_reorder": False,
                },
                "models": {
                    "base_url": "http://api.test:11434",
                    "model": "MODEL_X",
                    "triage_model": "TRIAGE_X",
                    "temperature": 0.2,
                    "num_ctx": 16384,
                    "num_predict": 2000,
                    "auto_num_ctx": True,
                    "triage_num_predict": 77,
                    "triage_confidence": 0.7,
                    "photo_skip_confidence": 0.9,
                    "vlm_concurrency": 2,
                    "vlm_page_image_max_px": 1536,
                },
            }
        )
        api_args = _namespace_from_config(
            "doc.pdf",
            config=api_config,
            config_files=(Path("overlay.yaml"),),
            start_page=2,
            end_page=4,
            output_dir="out",
            skip_vlm=True,
            visual_values_mode="audit",
            refine_mode="auto",
            resume_from="table_detect",
            shard=False,
        )
        cli_args = parse_args(
            argv_with_config_defaults(
                api_config,
                [
                    "doc.pdf", "--config", "overlay.yaml", "--start-page", "2",
                    "--end-page", "4", "--output-dir", "out", "--skip-vlm",
                    "--visual-values-mode", "audit", "--refine-mode", "auto",
                    "--resume-from", "table_detect",
                ],
            )
        )
        check(vars(api_args) == vars(cli_args), "API and CLI build identical pipeline arguments")

        runtime_config = config_from_mapping(
            {
                "runtime": {
                    "refine_mode": "auto",
                    "start_page": 5,
                    "end_page": 7,
                    "skip_vlm": True,
                    "output_dir": "cfg_out",
                }
            }
        )
        fallback_args = _namespace_from_config(
            "d.pdf",
            config=runtime_config,
            config_files=(),
            start_page=None,
            end_page=None,
            output_dir=None,
            skip_vlm=None,
            visual_values_mode="off",
            refine_mode=None,
            resume_from=None,
            shard=False,
        )
        check(
            vars(fallback_args)
            == vars(parse_args(argv_with_config_defaults(runtime_config, ["d.pdf"]))),
            "None runtime kwargs fall back to config like the CLI",
        )
        override_args = _namespace_from_config(
            "d.pdf",
            config=runtime_config,
            config_files=(),
            start_page=None,
            end_page=None,
            output_dir=None,
            skip_vlm=False,
            visual_values_mode="off",
            refine_mode=None,
            resume_from=None,
            shard=False,
        )
        check(override_args.skip_vlm is False, "explicit skip_vlm=False beats config True")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
