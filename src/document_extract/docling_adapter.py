"""Docling integration and item inspection for the page pipeline.

This module owns conversion, rasterization, and the defensive inspection of
Docling's evolving object model. Domain stages consume this surface without
knowing Docling API details.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any, Iterable

def rasterize_page(doc: Any, page_number: int, dpi: int, output_path: Path) -> None:
    import fitz  # noqa: PLC0415

    matrix = fitz.Matrix(dpi / 72, dpi / 72)
    page = doc.load_page(page_number - 1)
    page.get_pixmap(matrix=matrix).save(output_path)


def build_docling_converter() -> Any:
    from docling.datamodel.base_models import InputFormat  # noqa: PLC0415
    from docling.datamodel.pipeline_options import (  # noqa: PLC0415
        PdfPipelineOptions,
        RapidOcrOptions,
        TableFormerMode,
        TableStructureOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption  # noqa: PLC0415

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = True
    pipeline_options.ocr_options = make_rapidocr_options(RapidOcrOptions)
    pipeline_options.do_table_structure = True
    pipeline_options.table_structure_options = make_table_options(
        TableStructureOptions, TableFormerMode
    )
    set_if_present(pipeline_options, "generate_picture_images", True)
    set_if_present(pipeline_options, "generate_page_images", False)
    set_if_present(pipeline_options, "images_scale", 2.0)
    set_if_present(pipeline_options, "do_picture_description", False)
    set_if_present(pipeline_options, "do_chart_extraction", False)
    set_if_present(pipeline_options, "do_picture_classification", False)
    configure_cuda_accelerator(pipeline_options)

    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
    )


def set_if_present(obj: Any, attr: str, value: Any) -> None:
    if hasattr(obj, attr):
        setattr(obj, attr, value)


def make_rapidocr_options(options_cls: Any) -> Any:
    for kwargs in (
        {"force_full_page_ocr": False, "lang": ["english"], "bitmap_area_threshold": 0.05},
        {"force_full_page_ocr": False, "lang": ["en"]},
        {"force_full_page_ocr": False},
        {},
    ):
        try:
            options = options_cls(**kwargs)
            set_if_present(options, "force_full_page_ocr", False)
            return options
        except TypeError:
            continue
    options = options_cls()
    set_if_present(options, "force_full_page_ocr", False)
    return options


def make_table_options(options_cls: Any, table_mode_cls: Any) -> Any:
    mode = getattr(table_mode_cls, "ACCURATE", None) or "accurate"
    for kwargs in ({"mode": mode, "do_cell_matching": True}, {"mode": mode}, {}):
        try:
            options = options_cls(**kwargs)
            set_if_present(options, "mode", mode)
            set_if_present(options, "do_cell_matching", True)
            return options
        except TypeError:
            continue
    options = options_cls()
    set_if_present(options, "mode", mode)
    set_if_present(options, "do_cell_matching", True)
    return options


def configure_cuda_accelerator(pipeline_options: Any) -> None:
    try:
        from docling.datamodel.accelerator_options import (  # type: ignore  # noqa: PLC0415
            AcceleratorDevice,
            AcceleratorOptions,
        )
    except Exception:
        try:
            from docling.datamodel.pipeline_options import (  # type: ignore  # noqa: PLC0415
                AcceleratorDevice,
                AcceleratorOptions,
            )
        except Exception:
            return
    pipeline_options.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.CUDA)


def convert_pdf(converter: Any, pdf_path: Path, pages: list[int]) -> Any:
    start_page, end_page = pages[0], pages[-1]
    try:
        return converter.convert(pdf_path, page_range=(start_page, end_page)).document
    except TypeError:
        return converter.convert(pdf_path).document


def iter_doc_items(document: Any, page_number: int | None = None) -> Iterable[Any]:
    iterator = document.iterate_items() if hasattr(document, "iterate_items") else []
    for entry in iterator:
        item = entry[0] if isinstance(entry, tuple) else entry
        if page_number is None or item_page_number(item) == page_number:
            yield item


def item_page_number(item: Any) -> int | None:
    prov = getattr(item, "prov", None) or []
    if not prov:
        return None
    return getattr(prov[0], "page_no", None)


def item_kind(item: Any) -> str:
    label = getattr(item, "label", "")
    if label:
        return str(label).split(".")[-1].lower()
    name = type(item).__name__
    return re.sub(r"item$", "", name, flags=re.IGNORECASE).lower()


def is_picture_item(item: Any) -> bool:
    return "picture" in type(item).__name__.lower() or item_kind(item) == "picture"


def is_table_item(item: Any) -> bool:
    return "table" in type(item).__name__.lower() or item_kind(item) == "table"


def is_heading_item(item: Any) -> bool:
    kind = item_kind(item)
    name = type(item).__name__.lower()
    return "title" in name or "sectionheader" in name or kind in {"title", "section_header"}


def item_text(item: Any) -> str:
    for attr in ("text", "orig", "caption"):
        value = getattr(item, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if hasattr(item, "get_text"):
        try:
            value = item.get_text()
            if isinstance(value, str):
                return value.strip()
        except Exception:
            pass
    return ""


def caption_text(item: Any) -> str:
    for attr in ("caption_text", "caption"):
        value = getattr(item, attr, None)
        if callable(value):
            try:
                out = value()
                if isinstance(out, str):
                    return out.strip()
            except Exception:
                pass
        elif isinstance(value, str):
            return value.strip()
    return ""


def bbox_dict(item: Any) -> dict[str, Any] | None:
    prov = getattr(item, "prov", None) or []
    if not prov:
        return None
    bbox = getattr(prov[0], "bbox", None)
    if bbox is None:
        return None
    values: dict[str, Any] = {}
    for key, aliases in {
        "l": ("l", "left", "x0"),
        "t": ("t", "top", "y0"),
        "r": ("r", "right", "x1"),
        "b": ("b", "bottom", "y1"),
    }.items():
        for alias in aliases:
            if hasattr(bbox, alias):
                values[key] = float(getattr(bbox, alias))
                break
    if len(values) < 4 and isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        values = {"l": float(bbox[0]), "t": float(bbox[1]), "r": float(bbox[2]), "b": float(bbox[3])}
    if len(values) < 4:
        return None
    origin = getattr(bbox, "coord_origin", None) or getattr(bbox, "origin", None)
    if origin is not None:
        values["origin"] = str(origin).split(".")[-1]
    return values


def export_item_markdown(item: Any, document: Any) -> str:
    if not hasattr(item, "export_to_markdown"):
        return ""
    method = item.export_to_markdown
    for kwargs in ({"doc": document}, {"document": document}, {}):
        try:
            return str(method(**kwargs)).strip()
        except TypeError:
            continue
        except Exception:
            return ""
    return ""


def export_page_markdown_via_docling(document: Any, page_number: int) -> str:
    if not hasattr(document, "export_to_markdown"):
        return ""
    try:
        markdown = document.export_to_markdown(
            page_no=page_number,
            image_placeholder="<!-- image -->",
            compact_tables=False,
            traverse_pictures=False,
        )
    except TypeError:
        try:
            page_doc = document.filter(page_nrs={page_number})
            markdown = page_doc.export_to_markdown(
                image_placeholder="<!-- image -->",
                compact_tables=False,
                traverse_pictures=False,
            )
        except Exception:
            return ""
    except Exception:
        return ""
    return str(markdown).strip() + "\n" if markdown else ""


def assert_docling_export_surface(document: Any) -> None:
    if not hasattr(document, "iterate_items"):
        raise RuntimeError("Docling document does not expose iterate_items(); cannot export pages.")
    if hasattr(document, "export_to_markdown"):
        try:
            inspect.signature(document.export_to_markdown)
        except Exception:
            pass

__all__ = [
    "rasterize_page", "build_docling_converter", "set_if_present",
    "make_rapidocr_options", "make_table_options", "configure_cuda_accelerator",
    "convert_pdf", "iter_doc_items", "item_page_number", "item_kind",
    "is_picture_item", "is_table_item", "is_heading_item", "item_text",
    "caption_text", "bbox_dict", "export_item_markdown",
    "export_page_markdown_via_docling", "assert_docling_export_surface",
]
