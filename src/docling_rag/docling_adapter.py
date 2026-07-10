"""Docling-facing compatibility surface.

The implementation still lives in the migration module. Keeping these names
in one adapter makes the eventual algorithm move mechanical and explicit.
"""

from .legacy_pipeline import (
    assert_docling_export_surface,
    build_docling_converter,
    convert_pdf,
    export_page_markdown,
    iter_doc_items,
    rasterize_page,
)

__all__ = [
    "assert_docling_export_surface",
    "build_docling_converter",
    "convert_pdf",
    "export_page_markdown",
    "iter_doc_items",
    "rasterize_page",
]

