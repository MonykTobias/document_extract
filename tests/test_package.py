"""Focused checks for the package boundary and configuration layer.

Run from ``v2-docling`` with ``python tests/test_package.py``.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from docling_rag import legacy_pipeline
from docling_rag.config import load_config
from docling_rag.prompts import load_prompt


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"[ok] {message}")


def main() -> int:
    defaults = load_config()
    check(defaults.runtime.dpi == 200, "bundled runtime defaults load")
    check(defaults.models.num_ctx == 8192, "bundled model defaults load")
    check(
        load_prompt("picture_generic.md") == legacy_pipeline.DEFAULT_IMAGE_SUMMARY_PROMPT,
        "picture prompt resource is verbatim",
    )

    old_base_url = os.environ.pop("OLLAMA_BASE_URL", None)
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            overlay = Path(temp_dir) / "overlay.yaml"
            overlay.write_text(
                "models:\n  num_ctx: 321\npictures:\n  min_area_ratio: 0.02\n",
                encoding="utf-8",
            )
            merged = load_config([overlay])
            check(merged.models.num_ctx == 321, "user YAML overrides bundled model config")
            check(merged.pictures.min_area_ratio == 0.02, "user YAML overrides detection config")
    finally:
        if old_base_url is not None:
            os.environ["OLLAMA_BASE_URL"] = old_base_url

    os.environ["OLLAMA_BASE_URL"] = "http://example.test:11434"
    try:
        env_config = load_config()
        check(env_config.models.base_url == os.environ["OLLAMA_BASE_URL"], "environment overrides YAML")
    finally:
        if old_base_url is None:
            os.environ.pop("OLLAMA_BASE_URL", None)
        else:
            os.environ["OLLAMA_BASE_URL"] = old_base_url
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
