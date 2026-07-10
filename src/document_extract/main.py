"""Thin public CLI entrypoint.

The legacy module is intentionally kept as an internal compatibility layer
for this migration. Future algorithm moves can happen behind this stable API.
"""

from __future__ import annotations

from typing import Sequence

from .pipeline.runner import run_document


def main(argv: Sequence[str] | None = None) -> int:
    """Run the pipeline and return its process exit code."""

    return int(run_document(argv))


def cli() -> None:
    raise SystemExit(main())
