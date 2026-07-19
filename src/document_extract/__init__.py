"""Packaged entrypoint for the Docling/RAG slide pipeline."""

from .api import run_extraction

__version__ = "0.1.0"

__all__ = ["run_extraction"]
