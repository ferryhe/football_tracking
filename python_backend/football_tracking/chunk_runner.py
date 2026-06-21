from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import yaml

from football_tracking.config import AppConfig, load_config
from football_tracking.pipeline import BallTrackingPipeline
from football_tracking.temporal_chunks import TemporalChunk

logger = logging.getLogger(__name__)


def build_chunk_config(config: AppConfig, chunk: TemporalChunk, chunk_output_dir: Path) -> AppConfig:
    """Copy an AppConfig for one raw-only temporal chunk."""
    chunk_config = deepcopy(config)
    chunk_config.output_dir = chunk_output_dir.resolve()
    chunk_config.runtime.start_frame = chunk.decode_start_frame
    chunk_config.runtime.max_frames = chunk.end_frame - chunk.decode_start_frame + 1
    return enforce_raw_chunk_config(chunk_config)


def enforce_raw_chunk_config(config: AppConfig) -> AppConfig:
    """Apply the raw-only guarantees required at the chunk worker boundary."""
    config.postprocess.enabled = False
    config.follow_cam.enabled = False
    config.detector.inference_mode = "direct_full_frame"
    config.temporal_chunks.enabled = False
    return config


def _yamlable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _yamlable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _yamlable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_yamlable(item) for item in value]
    return value


def write_chunk_config(config: AppConfig, chunk: TemporalChunk, chunks_root: Path) -> Path:
    """Write one chunk config under its isolated chunk output directory."""
    chunk_output_dir = chunks_root / chunk.output_dir_name
    chunk_config = build_chunk_config(config, chunk, chunk_output_dir)
    config_path = chunk_output_dir / "chunk_config.yaml"
    chunk_output_dir.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(_yamlable(chunk_config), handle, sort_keys=False, allow_unicode=False)
    return config_path


def run_chunk(config_path: Path) -> int:
    """Run one chunk config through the normal pipeline."""
    try:
        config = enforce_raw_chunk_config(load_config(config_path))
        BallTrackingPipeline(config).run()
        return 0
    except Exception:
        logger.exception("Chunk worker failed for config: %s", config_path)
        return 1
