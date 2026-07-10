"""Thin public CLI entrypoint."""

from __future__ import annotations

from typing import Sequence

from .pipeline.runner import run_document


def main(argv: Sequence[str] | None = None) -> int:
    """Run the pipeline and return its process exit code."""

    return int(run_document(argv))


def cli() -> None:
    raise SystemExit(main())
