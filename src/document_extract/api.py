"""Programmatic entry point for document extraction."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from .config import AppConfig, apply_detection_config, load_config
from .pipeline.runner import run_pipeline


def _namespace_from_config(
    pdf: Path | str,
    *,
    config: AppConfig,
    config_files: Sequence[Path],
    start_page: int,
    end_page: int,
    output_dir: Path | str,
    skip_vlm: bool,
    visual_values_mode: str,
    refine_mode: str,
    resume_from: str | None,
) -> argparse.Namespace:
    return argparse.Namespace(
        pdf=Path(pdf),
        output_dir=Path(output_dir),
        config=[str(path) for path in config_files],
        dpi=config.runtime.dpi,
        start_page=start_page,
        end_page=end_page,
        skip_vlm=skip_vlm,
        visual_values_mode=visual_values_mode,
        ollama_base_url=config.models.base_url,
        ollama_model=config.models.model,
        triage_model=config.models.triage_model or None,
        triage_num_predict=config.models.triage_num_predict,
        triage_confidence=config.models.triage_confidence,
        photo_skip_confidence=config.models.photo_skip_confidence,
        photo_summaries=config.runtime.photo_summaries,
        skip_picture_triage=config.runtime.skip_picture_triage,
        temperature=config.models.temperature,
        num_ctx=config.models.num_ctx,
        num_predict=config.models.num_predict,
        vlm_concurrency=config.models.vlm_concurrency,
        vlm_page_image_max_px=config.models.vlm_page_image_max_px,
        auto_num_ctx=config.models.auto_num_ctx,
        refine_mode=refine_mode,
        prompt_file=None,
        no_divider_reorder=not config.runtime.divider_reorder,
        resume_from=resume_from,
    )


def run_extraction(
    pdf: Path | str,
    *,
    config: AppConfig | None = None,
    config_files: Sequence[Path] = (),
    start_page: int = 1,
    end_page: int = 0,
    output_dir: Path | str = "outputs_docling_rag",
    skip_vlm: bool = False,
    visual_values_mode: str = "off",
    refine_mode: str = "always",
    resume_from: str | None = None,
) -> int:
    """Run one extraction; use separate processes for concurrent runs."""
    effective_config = config or load_config(config_files)
    apply_detection_config(effective_config)
    return int(
        run_pipeline(
            _namespace_from_config(
                pdf,
                config=effective_config,
                config_files=config_files,
                start_page=start_page,
                end_page=end_page,
                output_dir=output_dir,
                skip_vlm=skip_vlm,
                visual_values_mode=visual_values_mode,
                refine_mode=refine_mode,
                resume_from=resume_from,
            )
        )
    )


__all__ = ["run_extraction"]
