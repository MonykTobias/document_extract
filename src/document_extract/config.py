"""Typed YAML configuration with deterministic override precedence."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, get_type_hints

import yaml


RESOURCE_CONFIG_DIR = Path(__file__).resolve().parent / "resources" / "config"


@dataclass(frozen=True)
class RuntimeConfig:
    output_dir: str = "outputs_docling_rag"
    dpi: int = 200
    start_page: int = 1
    end_page: int = 0
    skip_vlm: bool = False
    skip_picture_triage: bool = False
    photo_summaries: bool = False
    divider_reorder: bool = True


@dataclass(frozen=True)
class ModelConfig:
    base_url: str = "http://host.docker.internal:11434"
    model: str = "hf.co/unsloth/Qwen3-VL-8B-Instruct-GGUF:Q4_K_M"
    triage_model: str = ""
    temperature: float = 0.0
    num_ctx: int = 8192
    num_predict: int = 1200
    auto_num_ctx: bool = False
    triage_num_predict: int = 64
    triage_confidence: float = 0.65
    photo_skip_confidence: float = 0.8


@dataclass(frozen=True)
class PictureConfig:
    min_area_ratio: float = 0.01
    decorative_max_area_ratio: float = 0.05
    table_min_area_ratio: float = 0.08
    nearby_block_distance: float = 0.12
    crop_neighbor_gap: float = 0.03
    crop_margin: float = 0.02


@dataclass(frozen=True)
class TableConfig:
    min_cells: int = 6
    min_columns: int = 2
    min_column_members: int = 3
    min_aligned_rows: int = 3
    column_x_tolerance: float = 0.025
    row_y_tolerance: float = 0.02
    score_threshold: float = 0.55
    regions_max_per_page: int = 3
    numeric_coverage_min: float = 0.9
    word_coverage_min: float = 0.8


@dataclass(frozen=True)
class ReadingOrderConfig:
    max_depth: int = 4
    edge_eps: float = 0.006
    boundary_nudge: float = 0.002
    boundary_min_spacing: float = 0.01
    h_divider_min_span: float = 0.6
    v_divider_min_span: float = 0.35
    band_gap: float = 0.05
    column_gutter: float = 0.035


@dataclass(frozen=True)
class AppConfig:
    runtime: RuntimeConfig
    models: ModelConfig
    pictures: PictureConfig
    tables: TableConfig
    reading_order: ReadingOrderConfig


def load_config(paths: Iterable[Path] = ()) -> AppConfig:
    """Load package defaults, then user files, then environment overrides."""

    merged: dict[str, Any] = {}
    for name in ("default.yaml", "models.yaml", "detection.yaml"):
        merged = deep_merge(merged, _read_yaml(RESOURCE_CONFIG_DIR / name))
    for path in paths:
        merged = deep_merge(merged, _read_yaml(Path(path).expanduser().resolve()))
    apply_environment_overrides(merged)
    return config_from_mapping(merged)


def config_from_mapping(mapping: dict[str, Any]) -> AppConfig:
    _reject_unknown(mapping, {"runtime", "models", "pictures", "tables", "reading_order"}, "root")
    return AppConfig(
        runtime=_section(RuntimeConfig, mapping.get("runtime", {}), "runtime"),
        models=_section(ModelConfig, mapping.get("models", {}), "models"),
        pictures=_section(PictureConfig, mapping.get("pictures", {}), "pictures"),
        tables=_section(TableConfig, mapping.get("tables", {}), "tables"),
        reading_order=_section(ReadingOrderConfig, mapping.get("reading_order", {}), "reading_order"),
    )


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def apply_environment_overrides(mapping: dict[str, Any]) -> None:
    """Apply supported environment variables after YAML overlays.

    The historical ``OLLAMA_*`` names remain supported.  The namespaced
    ``DOCLING_RAG_*`` variants cover every typed setting without exposing the
    internal YAML layout to deployment manifests.
    """

    env_map = {
        "OLLAMA_BASE_URL": ("models", "base_url", str),
        "OLLAMA_MODEL": ("models", "model", str),
        "OLLAMA_TRIAGE_MODEL": ("models", "triage_model", str),
        "DOCLING_RAG_OUTPUT_DIR": ("runtime", "output_dir", str),
        "DOCLING_RAG_DPI": ("runtime", "dpi", int),
        "DOCLING_RAG_START_PAGE": ("runtime", "start_page", int),
        "DOCLING_RAG_END_PAGE": ("runtime", "end_page", int),
        "DOCLING_RAG_SKIP_VLM": ("runtime", "skip_vlm", _env_bool),
        "DOCLING_RAG_SKIP_PICTURE_TRIAGE": ("runtime", "skip_picture_triage", _env_bool),
        "DOCLING_RAG_PHOTO_SUMMARIES": ("runtime", "photo_summaries", _env_bool),
        "DOCLING_RAG_DIVIDER_REORDER": ("runtime", "divider_reorder", _env_bool),
        "DOCLING_RAG_BASE_URL": ("models", "base_url", str),
        "DOCLING_RAG_MODEL": ("models", "model", str),
        "DOCLING_RAG_TRIAGE_MODEL": ("models", "triage_model", str),
        "DOCLING_RAG_TEMPERATURE": ("models", "temperature", float),
        "DOCLING_RAG_NUM_CTX": ("models", "num_ctx", int),
        "DOCLING_RAG_NUM_PREDICT": ("models", "num_predict", int),
        "DOCLING_RAG_AUTO_NUM_CTX": ("models", "auto_num_ctx", _env_bool),
        "DOCLING_RAG_TRIAGE_NUM_PREDICT": ("models", "triage_num_predict", int),
        "DOCLING_RAG_TRIAGE_CONFIDENCE": ("models", "triage_confidence", float),
        "DOCLING_RAG_PHOTO_SKIP_CONFIDENCE": ("models", "photo_skip_confidence", float),
    }
    for env_name, (section, key, converter) in env_map.items():
        raw = os.getenv(env_name)
        if raw is None:
            continue
        try:
            mapping.setdefault(section, {})[key] = converter(raw)
        except ValueError as exc:
            raise ValueError(f"Invalid value for {env_name}: {raw!r}") from exc


def argv_with_config_defaults(config: AppConfig, argv: list[str]) -> list[str]:
    """Add config values only when the user did not pass the CLI option."""

    out = list(argv)
    values: list[tuple[str, Any]] = [
        ("--output-dir", config.runtime.output_dir),
        ("--dpi", config.runtime.dpi),
        ("--start-page", config.runtime.start_page),
        ("--end-page", config.runtime.end_page),
        ("--ollama-base-url", config.models.base_url),
        ("--ollama-model", config.models.model),
        ("--triage-model", config.models.triage_model or config.models.model),
        ("--temperature", config.models.temperature),
        ("--num-ctx", config.models.num_ctx),
        ("--num-predict", config.models.num_predict),
        ("--triage-num-predict", config.models.triage_num_predict),
        ("--triage-confidence", config.models.triage_confidence),
        ("--photo-skip-confidence", config.models.photo_skip_confidence),
    ]
    for option, value in values:
        if not any(arg == option or arg.startswith(option + "=") for arg in out):
            out.extend((option, str(value)))
    if config.models.auto_num_ctx and "--auto-num-ctx" not in out:
        out.append("--auto-num-ctx")
    if config.runtime.skip_vlm and "--skip-vlm" not in out:
        out.append("--skip-vlm")
    if config.runtime.skip_picture_triage and "--skip-picture-triage" not in out:
        out.append("--skip-picture-triage")
    if config.runtime.photo_summaries and "--photo-summaries" not in out:
        out.append("--photo-summaries")
    if not config.runtime.divider_reorder and "--no-divider-reorder" not in out:
        out.append("--no-divider-reorder")
    return out


def apply_detection_config(config: AppConfig) -> None:
    """Apply YAML thresholds to the domain modules that own each algorithm."""

    from . import pictures, tables
    from .layout import prompt_map, reading_order

    picture_map = {
        "PICTURE_MIN_AREA_RATIO": config.pictures.min_area_ratio,
        "PICTURE_DECORATIVE_MAX_AREA_RATIO": config.pictures.decorative_max_area_ratio,
        "PICTURE_TABLE_MIN_AREA_RATIO": config.pictures.table_min_area_ratio,
        "PICTURE_CROP_NEIGHBOR_GAP": config.pictures.crop_neighbor_gap,
        "PICTURE_CROP_MARGIN": config.pictures.crop_margin,
    }
    table_map = {
        "TABLE_MIN_CELLS": config.tables.min_cells,
        "TABLE_MIN_COLUMNS": config.tables.min_columns,
        "TABLE_MIN_COLUMN_MEMBERS": config.tables.min_column_members,
        "TABLE_MIN_ALIGNED_ROWS": config.tables.min_aligned_rows,
        "TABLE_COLUMN_X_TOLERANCE": config.tables.column_x_tolerance,
        "TABLE_ROW_Y_TOLERANCE": config.tables.row_y_tolerance,
        "TABLE_SCORE_THRESHOLD": config.tables.score_threshold,
        "TABLE_REGIONS_MAX_PER_PAGE": config.tables.regions_max_per_page,
        "TABLE_NUMERIC_COVERAGE_MIN": config.tables.numeric_coverage_min,
        "TABLE_WORD_COVERAGE_MIN": config.tables.word_coverage_min,
    }
    reading_map = {
        "READING_MAX_DEPTH": config.reading_order.max_depth,
        "READING_EDGE_EPS": config.reading_order.edge_eps,
        "READING_BOUNDARY_NUDGE": config.reading_order.boundary_nudge,
        "READING_BOUNDARY_MIN_SPACING": config.reading_order.boundary_min_spacing,
        "READING_H_DIVIDER_MIN_SPAN": config.reading_order.h_divider_min_span,
        "READING_V_DIVIDER_MIN_SPAN": config.reading_order.v_divider_min_span,
        "READING_BAND_GAP": config.reading_order.band_gap,
        "READING_COLUMN_GUTTER": config.reading_order.column_gutter,
    }
    for key, value in picture_map.items():
        setattr(pictures, key, value)
    prompt_map.NEARBY_BLOCK_DISTANCE = config.pictures.nearby_block_distance
    for key, value in table_map.items():
        setattr(tables, key, value)
    for key, value in reading_map.items():
        setattr(reading_order, key, value)

def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing configuration file: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Configuration root must be a mapping: {path}")
    return payload


def _section(cls: Any, values: dict[str, Any], name: str) -> Any:
    if not isinstance(values, dict):
        raise ValueError(f"Configuration section must be a mapping: {name}")
    allowed = set(cls.__dataclass_fields__)
    _reject_unknown(values, allowed, name)
    try:
        result = cls(**values)
    except TypeError as exc:
        raise ValueError(f"Invalid configuration in {name}: {exc}") from exc
    _validate_section(result, name)
    return result


def _reject_unknown(values: dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ValueError(f"Unknown configuration keys in {name}: {', '.join(unknown)}")


def _validate_section(value: Any, name: str) -> None:
    type_hints = get_type_hints(type(value))
    for key, item in vars(value).items():
        expected = type_hints[key]
        if expected is bool and not isinstance(item, bool):
            raise ValueError(f"{name}.{key} must be a boolean")
        if expected is int and (not isinstance(item, int) or isinstance(item, bool)):
            raise ValueError(f"{name}.{key} must be an integer")
        if expected is float and (not isinstance(item, (int, float)) or isinstance(item, bool)):
            raise ValueError(f"{name}.{key} must be a number")
        if expected is str and not isinstance(item, str):
            raise ValueError(f"{name}.{key} must be a string")
        if key.endswith("ratio") or key.endswith("threshold") or key.endswith("span") or key.endswith("gap") or key.endswith("tolerance"):
            if not 0 <= item <= 1:
                raise ValueError(f"{name}.{key} must be between 0 and 1")
    if value.__class__.__name__ == "ModelConfig" and not 0 <= value.triage_confidence <= 1:
        raise ValueError("models.triage_confidence must be between 0 and 1")
    if value.__class__.__name__ == "ModelConfig" and not 0 <= value.photo_skip_confidence <= 1:
        raise ValueError("models.photo_skip_confidence must be between 0 and 1")


def _env_bool(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError("expected true/false")
