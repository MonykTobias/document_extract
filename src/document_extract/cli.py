"""CLI parser and configuration bridge for the public package entrypoint."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

from .config import apply_detection_config, argv_with_config_defaults, load_config
from .pipeline.runner import run_pipeline
from .prompts import DEFAULT_OLLAMA_MODEL
from .runtime import REPLAY_STAGES

def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parse marketing PDFs to RAG-ready markdown with Docling and optional Qwen image summaries."
    )
    parser.add_argument("pdf", type=Path, help="PDF file to parse.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs_docling_rag"),
        help="Where outputs will be written.",
    )
    parser.add_argument(
        "--config",
        action="append",
        default=[],
        help="Optional YAML configuration overlay; may be provided more than once.",
    )
    parser.add_argument("--dpi", type=int, default=200, help="Debug raster DPI.")
    parser.add_argument("--start-page", type=int, default=1, help="1-based start page.")
    parser.add_argument(
        "--end-page", type=int, default=0, help="1-based end page, 0 means until the end."
    )
    parser.add_argument(
        "--skip-vlm",
        action="store_true",
        help="Do not call Ollama; keep image references without summaries.",
    )
    parser.add_argument(
        "--visual-values-mode",
        choices=("off", "audit", "enforce"),
        default="off",
        help="Tagged-PDF visual-value handling mode.",
    )
    parser.add_argument(
        "--ollama-base-url",
        default=os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434"),
        help="Base URL for Ollama.",
    )
    parser.add_argument(
        "--ollama-model",
        default=os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
        help="Ollama multimodal model used for image/chart summaries.",
    )
    parser.add_argument(
        "--triage-model",
        default=os.getenv("OLLAMA_TRIAGE_MODEL"),
        help="Optional faster Ollama model for picture triage; defaults to --ollama-model.",
    )
    parser.add_argument(
        "--triage-num-predict",
        type=int,
        default=64,
        help="Maximum generated tokens for picture triage JSON.",
    )
    parser.add_argument(
        "--triage-confidence",
        type=float,
        default=0.65,
        help="Minimum confidence required for type-specific picture routing.",
    )
    parser.add_argument(
        "--photo-skip-confidence",
        type=float,
        default=0.8,
        help="Triage confidence at or above which photo-type images skip extraction entirely.",
    )
    parser.add_argument(
        "--photo-summaries",
        action="store_true",
        help="Also transcribe photo-type images (skipped by default after confident triage).",
    )
    parser.add_argument(
        "--skip-picture-triage",
        action="store_true",
        help="Use the existing heuristic plus generic picture prompt without visual triage.",
    )
    parser.add_argument("--temperature", type=float, default=0.0, help="Ollama temperature.")
    parser.add_argument(
        "--num-ctx",
        type=int,
        default=8192,
        help="Ollama context window. 0 keeps the model default.",
    )
    parser.add_argument(
        "--num-predict",
        type=int,
        default=1200,
        help="Cap generated tokens per image summary. 0 disables the cap.",
    )
    parser.add_argument(
        "--vlm-concurrency",
        type=int,
        default=1,
        help="Concurrent Ollama calls for independent per-picture/per-table "
        "requests. 1 keeps the serial behavior. Real parallelism needs "
        "OLLAMA_NUM_PARALLEL on the server, and each slot allocates its own "
        "num_ctx KV cache.",
    )
    parser.add_argument(
        "--auto-num-ctx",
        action="store_true",
        help="Size the context window per call from the prompt and image estimate.",
    )
    parser.add_argument(
        "--prompt-file",
        type=Path,
        default=None,
        help="Optional full-page VLM refinement prompt. Must contain {source_markdown}.",
    )
    parser.add_argument(
        "--no-divider-reorder",
        action="store_true",
        help="Disable divider-aware reading-order reconstruction; keep Docling's order.",
    )
    parser.add_argument(
        "--resume-from",
        choices=REPLAY_STAGES,
        default=None,
        help="Resume selected pages from a saved page checkpoint at this stage.",
    )
    return parser.parse_args(argv)

def run(argv: Sequence[str] | None = None) -> int:
    original = list(sys.argv[1:] if argv is None else argv)
    config = load_config(_config_paths(original))
    apply_detection_config(config)
    translated = argv_with_config_defaults(config, original)
    args = parse_args(translated)
    return int(run_pipeline(args))

def _config_paths(argv: list[str]) -> list[Path]:
    paths: list[Path] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--config" and index + 1 < len(argv):
            paths.append(Path(argv[index + 1]))
            index += 2
            continue
        if arg.startswith("--config="):
            paths.append(Path(arg.split("=", 1)[1]))
        index += 1
    return paths

__all__ = ["parse_args", "run"]
