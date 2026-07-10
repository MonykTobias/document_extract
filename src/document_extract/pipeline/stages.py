"""Named page-stage functions.

The implementations are still delegated to the verified migration module;
this explicit surface lets later changes move one stage at a time.
"""

from ..legacy_pipeline import (
    _prepare_page,
    _run_finalize,
    _run_page_refine,
    _run_page_repair,
    _run_picture_extract,
    _run_picture_triage,
    _run_table_detect,
    _run_table_extract,
)

__all__ = [
    "_prepare_page",
    "_run_finalize",
    "_run_page_refine",
    "_run_page_repair",
    "_run_picture_extract",
    "_run_picture_triage",
    "_run_table_detect",
    "_run_table_extract",
]

