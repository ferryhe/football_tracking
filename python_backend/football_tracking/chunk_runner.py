from __future__ import annotations

import logging
from concurrent.futures import CancelledError
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable

import cv2
import yaml

from football_tracking.chunk_stitcher import stitch_chunk_outputs
from football_tracking.config import AppConfig, load_config
from football_tracking.follow_cam import FollowCamGenerator
from football_tracking.pipeline import BallTrackingPipeline
from football_tracking.postprocess import TrackPostprocessor
from football_tracking.temporal_chunks import TemporalChunk, plan_temporal_chunks

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
    config.output.save_csv = True
    config.output.save_debug_jsonl = True
    config.logging.save_debug_jsonl = True
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


def run_chunk(
    config_path: Path,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> int:
    """Run one chunk config through the normal pipeline."""
    try:
        config = enforce_raw_chunk_config(load_config(config_path))
        BallTrackingPipeline(config).run(progress_callback=progress_callback, should_cancel=should_cancel)
        return 0
    except CancelledError:
        raise
    except Exception:
        logger.exception("Chunk worker failed for config: %s", config_path)
        return 1


def run_temporal_chunks(
    config: AppConfig,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    """Run temporal chunks sequentially, stitch raw outputs, then run global outputs."""
    _ensure_temporal_raw_outputs(config)
    source_total_frames = _source_total_frames(config)
    chunks = plan_temporal_chunks(
        source_total_frames=source_total_frames,
        chunk_frames=config.temporal_chunks.chunk_frames,
        overlap_frames=config.temporal_chunks.overlap_frames,
        start_frame=config.runtime.start_frame,
        max_frames=config.runtime.max_frames,
        decode_preroll_frames=config.temporal_chunks.decode_preroll_frames,
    )
    if not chunks:
        raise RuntimeError("Temporal chunk planning produced no chunks.")
    chunks_root = config.output_dir / config.temporal_chunks.output_dir_name
    chunks_root.mkdir(parents=True, exist_ok=True)

    chunk_dirs: list[Path] = []
    total_chunks = len(chunks)
    for chunk in chunks:
        _raise_if_cancelled(should_cancel)
        _emit_progress(
            progress_callback,
            {
                "stage": "temporal_chunks",
                "chunk_index": chunk.index,
                "chunk_count": total_chunks,
                "current_frame": chunk.core_start_frame,
                "total_frames": source_total_frames,
            },
        )
        config_path = write_chunk_config(config, chunk, chunks_root)
        exit_code = run_chunk(config_path, progress_callback=progress_callback, should_cancel=should_cancel)
        if exit_code != 0:
            raise RuntimeError(f"Temporal chunk {chunk.output_dir_name} failed with exit code {exit_code}")
        chunk_dirs.append(chunks_root / chunk.output_dir_name)

    _raise_if_cancelled(should_cancel)
    _emit_progress(progress_callback, {"stage": "stitch", "chunk_count": total_chunks})
    stitch_chunk_outputs(chunks, chunk_dirs, config.output_dir, output_config=config.output)

    if config.mock.enabled:
        return

    if config.postprocess.enabled:
        _raise_if_cancelled(should_cancel)
        _emit_progress(progress_callback, {"stage": "postprocess", "current_frame": 0, "total_frames": 1})
        TrackPostprocessor(config).run()
        _emit_progress(progress_callback, {"stage": "postprocess", "current_frame": 1, "total_frames": 1})
    if config.follow_cam.enabled:
        _raise_if_cancelled(should_cancel)
        _emit_progress(progress_callback, {"stage": "follow_cam"})
        FollowCamGenerator(config).run(progress_callback=progress_callback, should_cancel=should_cancel)


def _ensure_temporal_raw_outputs(config: AppConfig) -> None:
    config.output.save_csv = True
    config.output.save_debug_jsonl = True
    config.logging.save_debug_jsonl = True


def _source_total_frames(config: AppConfig) -> int:
    if config.mock.enabled:
        return int(config.mock.frame_count)

    capture_backend = getattr(cv2, config.runtime.capture_backend, cv2.CAP_ANY)
    capture = cv2.VideoCapture(str(config.input_video), capture_backend)
    if not capture.isOpened():
        raise RuntimeError(f"Unable to open video for temporal chunk planning: {config.input_video}")
    try:
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    finally:
        capture.release()
    if frame_count <= 0:
        raise RuntimeError(f"Unable to determine source frame count: {config.input_video}")
    return frame_count


def _raise_if_cancelled(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel is not None and should_cancel():
        raise CancelledError("Temporal chunk run cancelled by user.")


def _emit_progress(progress_callback: Callable[[dict[str, Any]], None] | None, payload: dict[str, Any]) -> None:
    if progress_callback is not None:
        progress_callback(payload)
