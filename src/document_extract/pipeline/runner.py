"""Stable package runner entrypoint."""

from __future__ import annotations

from typing import Sequence

from ..cli import run


def run_document(argv: Sequence[str] | None = None) -> int:
    return run(argv)

