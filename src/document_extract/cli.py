"""CLI/config bridge used while the legacy pipeline is being split."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

from . import legacy_pipeline
from .config import apply_detection_config, legacy_argv_defaults, load_config


def run(argv: Sequence[str] | None = None) -> int:
    original = list(sys.argv[1:] if argv is None else argv)
    config_paths = _config_paths(original)
    config = load_config(config_paths)
    apply_detection_config(config, legacy_pipeline)
    translated = legacy_argv_defaults(config, original)
    old_argv = sys.argv
    sys.argv = [old_argv[0], *translated]
    try:
        return int(legacy_pipeline.main())
    finally:
        sys.argv = old_argv


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
