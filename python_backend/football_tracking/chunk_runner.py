from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import CancelledError
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import cv2
import yaml

from football_tracking.ai_review_triggers import write_ai_review_trigger_report
from football_tracking.ball_audit import write_ball_audit_report
from football_tracking.chunk_stitcher import stitch_chunk_outputs
from football_tracking.config import AppConfig, load_config
from football_tracking.detector_candidate_contract import (
    CandidateSourceChangedError,
    CandidateSourceSnapshot,
    capture_candidate_source_snapshot,
    remove_runtime_tracking_contract,
    verify_candidate_source_snapshot,
)
from football_tracking.events import write_event_candidate_report
from football_tracking.follow_cam import FollowCamGenerator
from football_tracking.high_recall_reconcile import reconcile_high_recall_outputs
from football_tracking.high_recall_windows import DEFAULT_APPROVED_ROI_PADDING_PX, write_high_recall_window_report
from football_tracking.pipeline import BallTrackingPipeline
from football_tracking.postprocess import TrackPostprocessor
from football_tracking.temporal_chunks import TemporalChunk, plan_temporal_chunks

logger = logging.getLogger(__name__)

@dataclass(frozen=True, slots=True)
class _ChunkJob:
    chunk: TemporalChunk
    chunk_dir: Path
    config_path: Path
    device: str
    command: list[str]


@dataclass(slots=True)
class _RunningChunkProcess:
    job: _ChunkJob
    process: Any
    start_time: float
    stdout_path: Path
    stderr_path: Path
    stdout_file: Any
    stderr_file: Any


@dataclass(frozen=True, slots=True)
class _HighRecallSettings:
    enabled: bool = False
    margin_frames: int = 0
    merge_gap_frames: int = 30
    max_total_frames: int | None = 1800
    mode: str = "sahi"
    output_dir_name: str = "high_recall_windows"
    approved_actions_path: Path | None = None
    approved_only: bool = False
    max_speed_px_per_frame: float = 180.0
    max_jump_px: float = 260.0


def effective_worker_count(
    *,
    requested: int,
    detector_device: str,
    devices: tuple[str, ...],
    allow_gpu_oversubscription: bool,
) -> int:
    """Return the safe worker count for CPU, single-GPU, or multi-GPU chunk execution."""
    safe_requested = max(1, int(requested))
    if allow_gpu_oversubscription:
        return safe_requested
    if devices:
        return max(1, min(safe_requested, len(devices)))
    if str(detector_device).strip().lower().startswith("cuda"):
        return 1
    return safe_requested


def build_chunk_config(
    config: AppConfig,
    chunk: TemporalChunk,
    chunk_output_dir: Path,
    *,
    detector_device: str | None = None,
) -> AppConfig:
    """Copy an AppConfig for one raw-only temporal chunk."""
    chunk_config = deepcopy(config)
    chunk_config.output_dir = chunk_output_dir.resolve()
    chunk_config.runtime.start_frame = chunk.decode_start_frame
    chunk_config.runtime.max_frames = chunk.end_frame - chunk.decode_start_frame + 1
    if detector_device is not None:
        chunk_config.detector.device = detector_device
    return enforce_raw_chunk_config(chunk_config)


def enforce_raw_chunk_config(config: AppConfig) -> AppConfig:
    """Apply the raw-only guarantees required at the chunk worker boundary."""
    config.postprocess.enabled = False
    config.follow_cam.enabled = False
    config.detector.inference_mode = "direct_full_frame"
    config.temporal_chunks.enabled = False
    if hasattr(config, "high_recall_windows"):
        config.high_recall_windows.enabled = False
    config.output.save_csv = True
    config.output.save_debug_jsonl = True
    config.logging.save_debug_jsonl = True
    return config


def build_high_recall_window_config(
    config: AppConfig,
    window: dict[str, Any],
    window_output_dir: Path,
) -> AppConfig:
    """Copy an AppConfig for one high-recall rerun window."""
    settings = _high_recall_settings(config)
    if not _prepare_high_recall_window_for_execution(config, window, mode=settings.mode):
        raise ValueError(f"High-recall window is not executable: {window.get('sahi_policy') or window}")
    window_config = deepcopy(config)
    start_frame = _coerce_int(window.get("start_frame"))
    end_frame = _coerce_int(window.get("end_frame"))
    if start_frame is None or end_frame is None:
        raise ValueError(f"Invalid high-recall window frame range: {window}")
    if end_frame < start_frame:
        start_frame, end_frame = end_frame, start_frame
    window_config.output_dir = window_output_dir.resolve()
    window_config.runtime.start_frame = start_frame
    window_config.runtime.max_frames = end_frame - start_frame + 1
    window_config.detector.inference_mode = str(window.get("mode") or settings.mode)
    effective_roi = _coerce_roi(window.get("effective_roi"))
    if effective_roi is not None:
        window_config.filtering.roi = effective_roi
    return enforce_high_recall_chunk_config(window_config)


def enforce_high_recall_chunk_config(config: AppConfig) -> AppConfig:
    """Apply raw-output guarantees while preserving high-recall detector mode."""
    config.postprocess.enabled = False
    config.follow_cam.enabled = False
    config.temporal_chunks.enabled = False
    detector_mode = str(getattr(config.detector, "inference_mode", "")).strip().lower()
    if detector_mode == "sahi" and hasattr(config.detector, "use_half"):
        config.detector.use_half = False
    if hasattr(config, "high_recall_windows"):
        config.high_recall_windows.enabled = False
    config.output.save_csv = True
    config.output.save_debug_jsonl = True
    config.logging.save_debug_jsonl = True
    return config


def write_high_recall_window_config(
    config: AppConfig,
    window: dict[str, Any],
    window_index: int,
    high_recall_root: Path,
) -> Path:
    window_output_dir = high_recall_root / f"window_{window_index:03d}"
    window_config = build_high_recall_window_config(config, window, window_output_dir)
    config_path = window_output_dir / "chunk_config.yaml"
    window_output_dir.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(_yamlable(window_config), handle, sort_keys=False, allow_unicode=False)
    return config_path


def run_high_recall_chunk(
    config_path: Path,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> int:
    """Run one high-recall window while preserving the configured detector mode."""
    try:
        remove_runtime_tracking_contract(config_path.parent)
        config = enforce_high_recall_chunk_config(load_config(config_path))
        remove_runtime_tracking_contract(config.output_dir)
        BallTrackingPipeline(
            config,
            candidate_source_sha256=config.runtime.candidate_source_sha256,
            candidate_source_stat_token=config.runtime.candidate_source_stat_token,
            verify_candidate_source_content=config.runtime.candidate_source_sha256 is None,
        ).run(progress_callback=progress_callback, should_cancel=should_cancel)
        return 0
    except CancelledError:
        raise
    except Exception:
        logger.exception("High-recall window worker failed for config: %s", config_path)
        return 1


def _yamlable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _yamlable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _yamlable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_yamlable(item) for item in value]
    if hasattr(value, "__dict__"):
        return _yamlable(vars(value))
    return value


def write_chunk_config(
    config: AppConfig,
    chunk: TemporalChunk,
    chunks_root: Path,
    *,
    detector_device: str | None = None,
) -> Path:
    """Write one chunk config under its isolated chunk output directory."""
    chunk_output_dir = chunks_root / chunk.output_dir_name
    chunk_config = build_chunk_config(config, chunk, chunk_output_dir, detector_device=detector_device)
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
        remove_runtime_tracking_contract(config_path.parent)
        config = enforce_raw_chunk_config(load_config(config_path))
        remove_runtime_tracking_contract(config.output_dir)
        BallTrackingPipeline(
            config,
            candidate_source_sha256=config.runtime.candidate_source_sha256,
            candidate_source_stat_token=config.runtime.candidate_source_stat_token,
            verify_candidate_source_content=config.runtime.candidate_source_sha256 is None,
        ).run(progress_callback=progress_callback, should_cancel=should_cancel)
        return 0
    except CancelledError:
        raise
    except Exception:
        logger.exception("Chunk worker failed for config: %s", config_path)
        return 1


def run_high_recall_windows(
    config: AppConfig,
    *,
    source_total_frames: int | None = None,
    candidate_source_snapshot: CandidateSourceSnapshot | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run high-recall detection only for selected suspicious windows."""
    settings = _high_recall_settings(config)
    high_recall_root = config.output_dir / settings.output_dir_name
    _reset_stale_high_recall_outputs(config.output_dir, high_recall_root)
    if not settings.enabled:
        report = {
            "windows": [],
            "summary": {"status": "disabled", "selected_window_count": 0},
            "execution": {"status": "skipped", "reason": "disabled", "results": []},
        }
        _write_high_recall_report(high_recall_root, report)
        return report

    _raise_if_cancelled(should_cancel)
    if not settings.approved_only:
        _remove_stale_postprocess_artifacts(config)
        write_ball_audit_report(config.output_dir)
        write_ai_review_trigger_report(config.output_dir)
        event_candidate_fps, event_candidate_fps_source = _event_candidate_fps(config)
        write_event_candidate_report(config.output_dir, fps=event_candidate_fps, fps_source=event_candidate_fps_source)
    approved_actions_path = _resolve_configured_approved_actions_path(
        settings.approved_actions_path,
        output_dir=config.output_dir,
    )
    report = write_high_recall_window_report(
        config.output_dir,
        output_dir_name=settings.output_dir_name,
        margin_frames=settings.margin_frames,
        merge_gap_frames=settings.merge_gap_frames,
        max_total_frames=settings.max_total_frames,
        total_frames=source_total_frames,
        mode=settings.mode,
        approved_actions_path=approved_actions_path,
        approved_only=settings.approved_only,
    )

    windows = report.get("windows") if isinstance(report.get("windows"), list) else []
    if not windows:
        execution = {"status": "skipped", "reason": "no_windows", "results": []}
        report["execution"] = execution
        _write_high_recall_report(high_recall_root, report)
        return report

    executable_windows: list[dict[str, Any]] = []
    for source_index, window in enumerate(windows):
        if _prepare_high_recall_window_for_execution(config, window, mode=settings.mode):
            window["source_window_index"] = source_index
            window["execution_window_index"] = len(executable_windows)
            executable_windows.append(window)

    if not executable_windows:
        execution = {"status": "skipped", "reason": "no_executable_windows", "results": []}
        report["execution"] = execution
        _write_high_recall_report(high_recall_root, report)
        return report

    if candidate_source_snapshot is None:
        candidate_source_snapshot = capture_candidate_source_snapshot(config)
    else:
        verify_candidate_source_snapshot(config, candidate_source_snapshot, verify_content=False)
    config.runtime.candidate_source_sha256 = candidate_source_snapshot.sha256
    config.runtime.candidate_source_stat_token = candidate_source_snapshot.stat_token
    results: list[dict[str, Any]] = []
    try:
        for index, window in enumerate(executable_windows):
            _raise_if_cancelled(should_cancel)
            _emit_progress(
                progress_callback,
                {
                    "stage": "high_recall_windows",
                    "window_index": index,
                    "window_count": len(executable_windows),
                    "current_frame": window.get("start_frame"),
                    "total_frames": source_total_frames,
                },
            )
            start_time = time.time()
            config_path = write_high_recall_window_config(config, window, index, high_recall_root)
            exit_code = run_high_recall_chunk(
                config_path,
                progress_callback=progress_callback,
                should_cancel=should_cancel,
            )
            end_time = time.time()
            result = {
                "window_index": index,
                "source_window_index": window.get("source_window_index"),
                "window_dir": str(high_recall_root / f"window_{index:03d}"),
                "config_path": str(config_path),
                "start_time": start_time,
                "end_time": end_time,
                "duration": max(0.0, end_time - start_time),
                "exit_code": exit_code,
            }
            results.append(result)
            if exit_code != 0:
                execution = {
                    "status": "failed",
                    "error": f"High-recall window {index} failed with exit code {exit_code}",
                    "results": results,
                }
                report["execution"] = execution
                _write_high_recall_report(high_recall_root, report)
                raise RuntimeError(execution["error"])
    except CancelledError:
        report["execution"] = {
            "status": "cancelled",
            "error": "High-recall window run cancelled by user.",
            "results": results,
        }
        _write_high_recall_report(high_recall_root, report)
        raise
    except Exception:
        try:
            verify_candidate_source_snapshot(config, candidate_source_snapshot, verify_content=True)
        except CandidateSourceChangedError as source_exc:
            _remove_tracking_contracts(
                child for child in high_recall_root.iterdir() if child.is_dir() and child.name.startswith("window_")
            )
            report["execution"] = {
                "status": "failed",
                "error": f"Candidate source changed during high-recall execution: {source_exc}",
                "results": results,
            }
            _write_high_recall_report(high_recall_root, report)
            raise CandidateSourceChangedError("candidate source changed during high-recall execution") from source_exc
        raise

    execution = {"status": "succeeded", "results": results}
    report["execution"] = execution
    try:
        reconcile_report = reconcile_high_recall_outputs(
            config.output_dir,
            executable_windows,
            high_recall_root=high_recall_root,
            csv_name=config.output.csv_name,
            max_speed_px_per_frame=settings.max_speed_px_per_frame,
            max_jump_px=settings.max_jump_px,
        )
    except Exception as exc:
        report["reconcile"] = {"status": "failed", "error": str(exc)}
        _write_high_recall_report(high_recall_root, report)
        raise
    report["reconcile"] = reconcile_report
    try:
        verify_candidate_source_snapshot(config, candidate_source_snapshot, verify_content=True)
    except CandidateSourceChangedError as exc:
        _remove_tracking_contracts(
            child for child in high_recall_root.iterdir() if child.is_dir() and child.name.startswith("window_")
        )
        report["execution"] = {
            **execution,
            "status": "failed",
            "error": f"Candidate source changed during high-recall execution: {exc}",
        }
        _write_high_recall_report(high_recall_root, report)
        raise CandidateSourceChangedError("candidate source changed during high-recall execution") from exc
    _write_high_recall_report(high_recall_root, report)
    return report


def run_temporal_chunks(
    config: AppConfig,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    """Run temporal chunks, stitch raw outputs, then run global outputs."""
    chunks_root = config.output_dir / config.temporal_chunks.output_dir_name
    _remove_temporal_tracking_contracts(config.output_dir, chunks_root)
    chunks_root.mkdir(parents=True, exist_ok=True)
    candidate_source_snapshot = capture_candidate_source_snapshot(config)
    config.runtime.candidate_source_sha256 = candidate_source_snapshot.sha256
    config.runtime.candidate_source_stat_token = candidate_source_snapshot.stat_token
    _ensure_temporal_raw_outputs(config)
    _reset_temporal_chunks_report(config.output_dir)
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
    total_chunks = len(chunks)
    requested_workers = config.temporal_chunks.max_workers
    worker_count = effective_worker_count(
        requested=requested_workers,
        detector_device=config.detector.device,
        devices=config.temporal_chunks.devices,
        allow_gpu_oversubscription=config.temporal_chunks.allow_gpu_oversubscription,
    )
    jobs = _prepare_chunk_jobs(config, chunks, chunks_root)

    try:
        if worker_count == 1:
            execution = _run_chunk_jobs_in_process(
                jobs,
                output_dir=config.output_dir,
                requested_workers=requested_workers,
                effective_workers=worker_count,
                devices=config.temporal_chunks.devices,
                allow_gpu_oversubscription=config.temporal_chunks.allow_gpu_oversubscription,
                source_total_frames=source_total_frames,
                total_chunks=total_chunks,
                progress_callback=progress_callback,
                should_cancel=should_cancel,
            )
        else:
            execution = _run_chunk_jobs_subprocess(
                jobs,
                output_dir=config.output_dir,
                requested_workers=requested_workers,
                effective_workers=worker_count,
                devices=config.temporal_chunks.devices,
                allow_gpu_oversubscription=config.temporal_chunks.allow_gpu_oversubscription,
                source_total_frames=source_total_frames,
                total_chunks=total_chunks,
                progress_callback=progress_callback,
                should_cancel=should_cancel,
            )
    except BaseException:
        _remove_temporal_tracking_contracts(config.output_dir, chunks_root)
        raise

    _write_execution_report(config.output_dir, execution)
    try:
        _raise_if_cancelled(should_cancel)
    except CancelledError:
        _remove_temporal_tracking_contracts(config.output_dir, chunks_root)
        raise
    try:
        verify_candidate_source_snapshot(config, candidate_source_snapshot, verify_content=True)
    except CandidateSourceChangedError as exc:
        _remove_temporal_tracking_contracts(config.output_dir, chunks_root)
        _write_stitch_report(config.output_dir, status="failed", error=str(exc))
        raise
    _emit_progress(progress_callback, {"stage": "stitch", "chunk_count": total_chunks})
    try:
        stitch_chunk_outputs(
            chunks,
            [job.chunk_dir for job in jobs],
            config.output_dir,
            output_config=config.output,
            candidate_source_sha256=candidate_source_snapshot.sha256,
            chunks_root_name=config.temporal_chunks.output_dir_name,
        )
    except Exception as exc:
        _write_stitch_report(config.output_dir, status="failed", error=str(exc))
        raise
    _write_stitch_report(config.output_dir, status="succeeded")
    _write_execution_report(config.output_dir, execution)
    try:
        verify_candidate_source_snapshot(config, candidate_source_snapshot, verify_content=True)
    except CandidateSourceChangedError as exc:
        _remove_temporal_tracking_contracts(config.output_dir, chunks_root)
        _write_stitch_report(config.output_dir, status="failed", error=str(exc))
        raise

    if config.mock.enabled:
        return

    if _high_recall_settings(config).enabled:
        _raise_if_cancelled(should_cancel)
        try:
            run_high_recall_windows(
                config,
                source_total_frames=source_total_frames,
                candidate_source_snapshot=candidate_source_snapshot,
                progress_callback=progress_callback,
                should_cancel=should_cancel,
            )
        except CandidateSourceChangedError:
            _remove_temporal_tracking_contracts(config.output_dir, chunks_root)
            raise

    try:
        if config.postprocess.enabled:
            _raise_if_cancelled(should_cancel)
            _emit_progress(progress_callback, {"stage": "postprocess", "current_frame": 0, "total_frames": 1})
            TrackPostprocessor(config).run()
            _emit_progress(progress_callback, {"stage": "postprocess", "current_frame": 1, "total_frames": 1})
        if config.follow_cam.enabled:
            _raise_if_cancelled(should_cancel)
            _emit_progress(progress_callback, {"stage": "follow_cam"})
            FollowCamGenerator(config).run(progress_callback=progress_callback, should_cancel=should_cancel)
    finally:
        try:
            verify_candidate_source_snapshot(config, candidate_source_snapshot, verify_content=True)
        except CandidateSourceChangedError:
            contract_dirs = _temporal_tracking_contract_dirs(config.output_dir, chunks_root)
            high_recall_root = config.output_dir / _high_recall_settings(config).output_dir_name
            resolved_high_recall_root = high_recall_root.resolve()
            if (
                high_recall_root.is_dir()
                and resolved_high_recall_root.parent == config.output_dir.resolve()
            ):
                contract_dirs.extend(
                    child
                    for child in high_recall_root.iterdir()
                    if child.is_dir()
                    and child.name.startswith("window_")
                    and child.resolve().parent == resolved_high_recall_root
                )
            _remove_tracking_contracts(contract_dirs)
            raise


def _ensure_temporal_raw_outputs(config: AppConfig) -> None:
    config.output.save_csv = True
    config.output.save_debug_jsonl = True
    config.logging.save_debug_jsonl = True


def _reset_temporal_chunks_report(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "temporal_chunks_report.json"
    if report_path.exists():
        report_path.unlink()


def _remove_tracking_contracts(output_dirs: Iterable[Path]) -> None:
    for output_dir in output_dirs:
        remove_runtime_tracking_contract(Path(output_dir))


def _temporal_tracking_contract_dirs(output_dir: Path, chunks_root: Path) -> list[Path]:
    contract_dirs = [output_dir]
    resolved_output_dir = output_dir.resolve()
    resolved_chunks_root = chunks_root.resolve()
    if resolved_chunks_root.parent != resolved_output_dir:
        raise ValueError("temporal chunk root must be a direct child of output_dir")
    candidate_roots = {resolved_chunks_root: chunks_root}
    if output_dir.is_dir():
        for child in output_dir.iterdir():
            if not child.is_dir():
                continue
            resolved_child = child.resolve()
            if resolved_child.parent == resolved_output_dir:
                candidate_roots[resolved_child] = child
    for root in sorted(candidate_roots.values(), key=lambda path: path.name):
        if not root.is_dir():
            continue
        resolved_root = root.resolve()
        contract_dirs.extend(
            sorted(
                child
                for child in root.iterdir()
                if child.is_dir()
                and child.name.startswith("chunk_")
                and child.resolve().parent == resolved_root
            )
        )
    return contract_dirs


def _remove_temporal_tracking_contracts(output_dir: Path, chunks_root: Path) -> None:
    remove_runtime_tracking_contract(output_dir)
    _remove_tracking_contracts(
        path for path in _temporal_tracking_contract_dirs(output_dir, chunks_root) if path != output_dir
    )


def _prepare_chunk_jobs(config: AppConfig, chunks: list[TemporalChunk], chunks_root: Path) -> list[_ChunkJob]:
    jobs: list[_ChunkJob] = []
    for chunk in chunks:
        device = _device_for_chunk(config, chunk)
        config_path = write_chunk_config(config, chunk, chunks_root, detector_device=device)
        jobs.append(
            _ChunkJob(
                chunk=chunk,
                chunk_dir=chunks_root / chunk.output_dir_name,
                config_path=config_path,
                device=device,
                command=_chunk_worker_command(config_path),
            )
        )
    return jobs


def _device_for_chunk(config: AppConfig, chunk: TemporalChunk) -> str:
    devices = config.temporal_chunks.devices
    if devices:
        return devices[chunk.index % len(devices)]
    return config.detector.device


def _chunk_worker_command(config_path: Path) -> list[str]:
    return [sys.executable, "-m", "football_tracking.chunk_worker", "--config", str(config_path)]


def _python_backend_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    python_backend = str(_python_backend_dir())
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = python_backend if not existing_pythonpath else os.pathsep.join([python_backend, existing_pythonpath])
    return env


def _start_chunk_subprocess(job: _ChunkJob) -> _RunningChunkProcess:
    stdout_path = job.chunk_dir / "worker.stdout.log"
    stderr_path = job.chunk_dir / "worker.stderr.log"
    stdout_file = stdout_path.open("w", encoding="utf-8")
    try:
        stderr_file = stderr_path.open("w", encoding="utf-8")
    except Exception:
        stdout_file.close()
        raise
    try:
        process = subprocess.Popen(
            job.command,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
            cwd=str(_python_backend_dir()),
            env=_subprocess_env(),
        )
    except Exception:
        stdout_file.close()
        stderr_file.close()
        raise
    return _RunningChunkProcess(
        job=job,
        process=process,
        start_time=time.time(),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        stdout_file=stdout_file,
        stderr_file=stderr_file,
    )


def _finalize_running_process(running_process: _RunningChunkProcess, *, exit_code: int) -> dict[str, Any]:
    running_process.stdout_file.close()
    running_process.stderr_file.close()
    return _worker_result(
        running_process.job,
        command=running_process.job.command,
        start_time=running_process.start_time,
        end_time=time.time(),
        exit_code=exit_code,
        stdout=_read_text_if_exists(running_process.stdout_path),
        stderr=_read_text_if_exists(running_process.stderr_path),
    )


def _read_text_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _run_chunk_jobs_in_process(
    jobs: list[_ChunkJob],
    *,
    output_dir: Path,
    requested_workers: int,
    effective_workers: int,
    devices: tuple[str, ...],
    allow_gpu_oversubscription: bool,
    source_total_frames: int,
    total_chunks: int,
    progress_callback: Callable[[dict[str, Any]], None] | None,
    should_cancel: Callable[[], bool] | None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    report_written = False
    try:
        for job in jobs:
            _raise_if_cancelled(should_cancel)
            _emit_chunk_progress(
                progress_callback,
                job.chunk,
                total_chunks=total_chunks,
                source_total_frames=source_total_frames,
            )
            start_time = time.time()
            try:
                exit_code = run_chunk(job.config_path, progress_callback=progress_callback, should_cancel=should_cancel)
            except CancelledError:
                end_time = time.time()
                results.append(
                    _worker_result(
                        job,
                        command=["in_process", str(job.config_path)],
                        start_time=start_time,
                        end_time=end_time,
                        exit_code=-15,
                        stdout="",
                        stderr="cancelled",
                    )
                )
                raise
            end_time = time.time()
            results.append(
                _worker_result(
                    job,
                    command=["in_process", str(job.config_path)],
                    start_time=start_time,
                    end_time=end_time,
                    exit_code=exit_code,
                    stdout="",
                    stderr="",
                )
            )
            if exit_code != 0:
                _write_execution_report(
                    output_dir,
                    _execution_payload(
                        mode="in_process",
                        requested_workers=requested_workers,
                        effective_workers=effective_workers,
                        devices=devices,
                        allow_gpu_oversubscription=allow_gpu_oversubscription,
                        results=results,
                        status="failed",
                        error=f"Temporal chunk {job.chunk.output_dir_name} failed with exit code {exit_code}",
                    ),
                )
                report_written = True
                raise RuntimeError(f"Temporal chunk {job.chunk.output_dir_name} failed with exit code {exit_code}")
    except CancelledError:
        _write_execution_report(
            output_dir,
            _execution_payload(
                mode="in_process",
                requested_workers=requested_workers,
                effective_workers=effective_workers,
                devices=devices,
                allow_gpu_oversubscription=allow_gpu_oversubscription,
                results=results,
                status="cancelled",
                error="Temporal chunk run cancelled by user.",
            ),
        )
        raise
    except Exception as exc:
        if not report_written:
            _write_execution_report(
                output_dir,
                _execution_payload(
                    mode="in_process",
                    requested_workers=requested_workers,
                    effective_workers=effective_workers,
                    devices=devices,
                    allow_gpu_oversubscription=allow_gpu_oversubscription,
                    results=results,
                    status="failed",
                    error=f"Temporal in-process chunk runner failed: {exc}",
                ),
            )
        raise
    return _execution_payload(
        mode="in_process",
        requested_workers=requested_workers,
        effective_workers=effective_workers,
        devices=devices,
        allow_gpu_oversubscription=allow_gpu_oversubscription,
        results=results,
        status="succeeded",
    )


def _run_chunk_jobs_subprocess(
    jobs: list[_ChunkJob],
    *,
    output_dir: Path,
    requested_workers: int,
    effective_workers: int,
    devices: tuple[str, ...],
    allow_gpu_oversubscription: bool,
    source_total_frames: int,
    total_chunks: int,
    progress_callback: Callable[[dict[str, Any]], None] | None,
    should_cancel: Callable[[], bool] | None,
) -> dict[str, Any]:
    pending = list(jobs)
    running: list[_RunningChunkProcess] = []
    results: list[dict[str, Any]] = []
    report_written = False
    try:
        while pending or running:
            if _is_cancelled(should_cancel):
                results.extend(_terminate_running_processes(running))
                execution = _execution_payload(
                    mode="subprocess",
                    requested_workers=requested_workers,
                    effective_workers=effective_workers,
                    devices=devices,
                    allow_gpu_oversubscription=allow_gpu_oversubscription,
                    results=results,
                    status="cancelled",
                    error="Temporal chunk run cancelled by user.",
                )
                _write_execution_report(output_dir, execution)
                report_written = True
                raise CancelledError("Temporal chunk run cancelled by user.")

            while pending and len(running) < effective_workers:
                job = pending.pop(0)
                _emit_chunk_progress(
                    progress_callback,
                    job.chunk,
                    total_chunks=total_chunks,
                    source_total_frames=source_total_frames,
                )
                try:
                    running_process = _start_chunk_subprocess(job)
                except Exception as exc:
                    results.extend(_terminate_running_processes(running))
                    error = f"Unable to start temporal chunk {job.chunk.output_dir_name}: {exc}"
                    execution = _execution_payload(
                        mode="subprocess",
                        requested_workers=requested_workers,
                        effective_workers=effective_workers,
                        devices=devices,
                        allow_gpu_oversubscription=allow_gpu_oversubscription,
                        results=results,
                        status="failed",
                        error=error,
                    )
                    _write_execution_report(output_dir, execution)
                    report_written = True
                    raise RuntimeError(error) from exc
                running.append(running_process)

            completed_any = False
            for running_process in list(running):
                exit_code = running_process.process.poll()
                if exit_code is None:
                    continue
                result = _finalize_running_process(running_process, exit_code=int(exit_code))
                if getattr(running_process.process, "returncode", None) is not None:
                    result["exit_code"] = int(running_process.process.returncode)
                results.append(result)
                running.remove(running_process)
                completed_any = True
                if result["exit_code"] != 0:
                    results.extend(_terminate_running_processes(running))
                    error = (
                        f"Temporal chunk {running_process.job.chunk.output_dir_name} "
                        f"failed with exit code {result['exit_code']}"
                    )
                    execution = _execution_payload(
                        mode="subprocess",
                        requested_workers=requested_workers,
                        effective_workers=effective_workers,
                        devices=devices,
                        allow_gpu_oversubscription=allow_gpu_oversubscription,
                        results=results,
                        status="failed",
                        error=error,
                    )
                    _write_execution_report(output_dir, execution)
                    report_written = True
                    raise RuntimeError(error)

            if running and not completed_any:
                time.sleep(0.05)
    except BaseException as exc:
        if running:
            results.extend(_terminate_running_processes(running))
        if not report_written:
            execution = _execution_payload(
                mode="subprocess",
                requested_workers=requested_workers,
                effective_workers=effective_workers,
                devices=devices,
                allow_gpu_oversubscription=allow_gpu_oversubscription,
                results=results,
                status="failed",
                error=f"Temporal subprocess scheduler failed: {exc}",
            )
            _write_execution_report(output_dir, execution)
        raise

    return _execution_payload(
        mode="subprocess",
        requested_workers=requested_workers,
        effective_workers=effective_workers,
        devices=devices,
        allow_gpu_oversubscription=allow_gpu_oversubscription,
        results=results,
        status="succeeded",
    )


def _emit_chunk_progress(
    progress_callback: Callable[[dict[str, Any]], None] | None,
    chunk: TemporalChunk,
    *,
    total_chunks: int,
    source_total_frames: int,
) -> None:
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


def _worker_result(
    job: _ChunkJob,
    *,
    command: list[str],
    start_time: float,
    end_time: float,
    exit_code: int,
    stdout: str,
    stderr: str,
) -> dict[str, Any]:
    return {
        "chunk": {
            "index": job.chunk.index,
            "name": job.chunk.output_dir_name,
        },
        "chunk_index": job.chunk.index,
        "chunk_name": job.chunk.output_dir_name,
        "config_path": str(job.config_path),
        "command": command,
        "device": job.device,
        "start_time": start_time,
        "end_time": end_time,
        "duration": max(0.0, end_time - start_time),
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
    }


def _execution_payload(
    *,
    mode: str,
    requested_workers: int,
    effective_workers: int,
    devices: tuple[str, ...],
    allow_gpu_oversubscription: bool,
    results: list[dict[str, Any]],
    status: str,
    error: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "mode": mode,
        "requested_workers": requested_workers,
        "effective_workers": effective_workers,
        "allow_gpu_oversubscription": allow_gpu_oversubscription,
        "devices": list(devices),
        "results": results,
    }
    if error is not None:
        payload["error"] = error
    return payload


def _write_execution_report(output_dir: Path, execution: dict[str, Any]) -> None:
    report_path = output_dir / "temporal_chunks_report.json"
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    else:
        report = {}
    report["execution"] = execution
    with report_path.open("w", encoding="utf-8") as report_file:
        json.dump(report, report_file, ensure_ascii=False, indent=2)


def _write_stitch_report(output_dir: Path, *, status: str, error: str | None = None) -> None:
    report_path = output_dir / "temporal_chunks_report.json"
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    else:
        report = {}
    stitch: dict[str, Any] = {"status": status}
    if error is not None:
        stitch["error"] = error
    report["stitch"] = stitch
    with report_path.open("w", encoding="utf-8") as report_file:
        json.dump(report, report_file, ensure_ascii=False, indent=2)


def _write_high_recall_report(high_recall_root: Path, report: dict[str, Any]) -> None:
    high_recall_root.mkdir(parents=True, exist_ok=True)
    with (high_recall_root / "report.json").open("w", encoding="utf-8") as report_file:
        json.dump(report, report_file, ensure_ascii=False, indent=2)


def _reset_high_recall_outputs(high_recall_root: Path) -> None:
    for name in ("report.json", "reconcile_report.json"):
        path = high_recall_root / name
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            logger.warning("Unable to remove stale high-recall report before rerun: %s", path)
    for child in high_recall_root.iterdir():
        if not child.is_dir() or not child.name.startswith("window_"):
            continue
        try:
            shutil.rmtree(child)
        except OSError:
            logger.warning("Unable to remove stale high-recall window directory before rerun: %s", child)


def _reset_stale_high_recall_outputs(output_dir: Path, active_root: Path) -> None:
    active_root.mkdir(parents=True, exist_ok=True)
    roots = [active_root]
    if output_dir.exists():
        for child in output_dir.iterdir():
            if child == active_root or not child.is_dir():
                continue
            if _looks_like_high_recall_root(child):
                roots.append(child)
    for root in roots:
        _reset_high_recall_outputs(root)


def _looks_like_high_recall_root(path: Path) -> bool:
    if (path / "reconcile_report.json").exists():
        return True
    report_path = path / "report.json"
    if not report_path.exists():
        return False
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(report, dict) and any(key in report for key in ("windows", "rejected_windows", "reconcile"))


def _remove_stale_postprocess_artifacts(config: AppConfig) -> None:
    postprocess_config = getattr(config, "postprocess", None)
    stale_names = {
        getattr(postprocess_config, "cleanup_report_name", "cleanup_report.json"),
        getattr(postprocess_config, "cleaned_csv_name", "ball_track.cleaned.csv"),
        getattr(postprocess_config, "cleaned_debug_jsonl_name", "debug.cleaned.jsonl"),
        getattr(postprocess_config, "cleaned_video_name", "annotated.cleaned.mp4"),
    }
    for name in stale_names:
        path = config.output_dir / name
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            logger.warning("Unable to remove stale postprocess artifact before high-recall planning: %s", path)


def _event_candidate_fps(config: AppConfig) -> tuple[float | None, str | None]:
    if getattr(getattr(config, "mock", None), "enabled", False):
        fps = _optional_positive_float(getattr(config.mock, "fps", None))
        return (fps, "mock_source") if fps is not None else (None, None)

    input_video = getattr(config, "input_video", None)
    if not isinstance(input_video, Path):
        return None, None
    capture = cv2.VideoCapture(str(input_video.resolve()))
    try:
        if not capture.isOpened():
            return None, None
        fps = _optional_positive_float(capture.get(cv2.CAP_PROP_FPS))
        return (fps, "input_video") if fps is not None else (None, None)
    finally:
        capture.release()


def _optional_positive_float(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 and parsed == parsed and parsed not in (float("inf"), float("-inf")) else None


def _prepare_high_recall_window_for_execution(config: Any, window: dict[str, Any], *, mode: str) -> bool:
    if window.get("execution_status") == "needs_manual_resolution":
        return False
    if window.get("execution_status") == "executable":
        return True

    requested_mode = str(window.get("mode") or mode or "sahi").strip().lower()
    if requested_mode not in {"sahi", "direct_full_frame"}:
        requested_mode = str(mode or "sahi").strip().lower()
    window["mode"] = requested_mode

    if not _is_approved_roi_action_window(window):
        return True

    base_roi = _coerce_roi(getattr(getattr(config, "filtering", None), "roi", None))
    approved_roi = _approved_roi_from_window(window)
    if approved_roi is None:
        if base_roi is not None:
            effective_roi = list(base_roi)
            window["effective_roi"] = effective_roi
            window["sahi_policy"] = _roi_policy_for_mode(requested_mode, "base_roi_no_ai_roi")
            window["execution_status"] = "executable"
            return True
        window["mode"] = "direct_full_frame"
        window["sahi_policy"] = "direct_full_frame_no_roi"
        window["execution_status"] = "executable"
        return True

    window["approved_roi"] = list(approved_roi)
    padded_roi = _pad_roi(approved_roi, DEFAULT_APPROVED_ROI_PADDING_PX)
    clamped_padded_roi = _clamp_roi_to_frame(padded_roi, _frame_bounds(config))
    if clamped_padded_roi is None:
        _mark_manual_resolution(window, "blocked_empty_roi_intersection")
        return False

    window["padded_roi"] = list(clamped_padded_roi)
    effective_roi = _intersect_rois(clamped_padded_roi, base_roi) if base_roi is not None else clamped_padded_roi
    if effective_roi is None:
        _mark_manual_resolution(window, "blocked_empty_roi_intersection")
        return False

    window["effective_roi"] = list(effective_roi)
    window["sahi_policy"] = _roi_policy_for_mode(requested_mode, "roi")
    window["execution_status"] = "executable"
    return True


def _mark_manual_resolution(window: dict[str, Any], policy: str) -> None:
    window["execution_status"] = "needs_manual_resolution"
    window["needs_manual_resolution"] = True
    window["sahi_policy"] = policy
    window["effective_roi"] = None


def _roi_policy_for_mode(mode: str, suffix: str) -> str:
    normalized_mode = str(mode or "direct_full_frame").strip().lower()
    prefix = "sahi" if normalized_mode == "sahi" else "direct_full_frame"
    return f"{prefix}_{suffix}"


_APPROVED_ROI_ACTIONS = {"targeted_rerun", "rerun_ball_window", "localize_ball_roi"}


def _is_approved_roi_action_window(window: dict[str, Any]) -> bool:
    if window.get("approved_action") in _APPROVED_ROI_ACTIONS:
        return True
    provenance = window.get("approval_provenance")
    if not isinstance(provenance, list):
        return False
    return any(
        isinstance(item, dict) and item.get("approved_action") in _APPROVED_ROI_ACTIONS
        for item in provenance
    )


def _approved_roi_from_window(window: dict[str, Any]) -> tuple[int, int, int, int] | None:
    roi = _coerce_roi(window.get("approved_roi"))
    if roi is not None:
        return roi
    roi = _roi_from_local_search_roi(window.get("local_search_roi"))
    if roi is not None:
        return roi
    provenance = window.get("approval_provenance")
    if isinstance(provenance, list):
        for item in provenance:
            if not isinstance(item, dict):
                continue
            roi = _coerce_roi(item.get("approved_roi")) or _roi_from_local_search_roi(item.get("local_search_roi"))
            if roi is not None:
                return roi
    return None


def _roi_from_local_search_roi(value: Any) -> tuple[int, int, int, int] | None:
    if not isinstance(value, dict):
        return None
    coordinate_space = str(value.get("coordinate_space") or "image").strip().lower()
    if coordinate_space != "image":
        return None
    x = _finite_float(value.get("x"))
    y = _finite_float(value.get("y"))
    width = _positive_float(value.get("width"))
    height = _positive_float(value.get("height"))
    if x is None or y is None or width is None or height is None:
        return None
    roi = (int(round(x)), int(round(y)), int(round(x + width)), int(round(y + height)))
    return roi if _roi_has_area(roi) else None


def _coerce_roi(value: Any) -> tuple[int, int, int, int] | None:
    if value in (None, "", []):
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        roi = tuple(int(item) for item in value)
    except (TypeError, ValueError):
        return None
    return roi if _roi_has_area(roi) else None


def _pad_roi(roi: tuple[int, int, int, int], padding_px: int) -> tuple[int, int, int, int]:
    pad = max(0, int(padding_px))
    return (roi[0] - pad, roi[1] - pad, roi[2] + pad, roi[3] + pad)


def _intersect_rois(
    first: tuple[int, int, int, int],
    second: tuple[int, int, int, int] | None,
) -> tuple[int, int, int, int] | None:
    if second is None:
        return first
    roi = (max(first[0], second[0]), max(first[1], second[1]), min(first[2], second[2]), min(first[3], second[3]))
    return roi if _roi_has_area(roi) else None


def _clamp_roi_to_frame(
    roi: tuple[int, int, int, int],
    frame_bounds: tuple[int, int] | None,
) -> tuple[int, int, int, int] | None:
    if frame_bounds is None:
        return roi if _roi_has_area(roi) else None
    frame_width, frame_height = frame_bounds
    clamped = (
        max(0, min(frame_width, roi[0])),
        max(0, min(frame_height, roi[1])),
        max(0, min(frame_width, roi[2])),
        max(0, min(frame_height, roi[3])),
    )
    return clamped if _roi_has_area(clamped) else None


def _roi_has_area(roi: tuple[int, int, int, int]) -> bool:
    return roi[2] > roi[0] and roi[3] > roi[1]


def _frame_bounds(config: Any) -> tuple[int, int] | None:
    mock_config = getattr(config, "mock", None)
    if bool(getattr(mock_config, "enabled", False)):
        width = _positive_int_value(getattr(mock_config, "frame_width", None))
        height = _positive_int_value(getattr(mock_config, "frame_height", None))
        if width is not None and height is not None:
            return width, height

    input_video = getattr(config, "input_video", None)
    if not isinstance(input_video, Path) or not input_video.exists():
        return None
    runtime = getattr(config, "runtime", None)
    capture_backend_name = str(getattr(runtime, "capture_backend", "CAP_ANY") or "CAP_ANY")
    capture_backend = getattr(cv2, capture_backend_name, cv2.CAP_ANY)
    capture = cv2.VideoCapture(str(input_video.resolve()), capture_backend)
    try:
        if not capture.isOpened():
            return None
        width = _positive_int_value(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = _positive_int_value(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        return (width, height) if width is not None and height is not None else None
    finally:
        capture.release()


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else None


def _positive_float(value: Any) -> float | None:
    parsed = _finite_float(value)
    return parsed if parsed is not None and parsed > 0 else None


def _positive_int_value(value: Any) -> int | None:
    parsed = _finite_float(value)
    if parsed is None:
        return None
    value_int = int(parsed)
    return value_int if value_int > 0 else None


def _high_recall_settings(config: Any) -> _HighRecallSettings:
    raw = getattr(config, "high_recall_windows", None)
    if raw in (None, "", []):
        return _HighRecallSettings()
    output_dir_name = str(_setting(raw, "output_dir_name", "high_recall_windows")).strip()
    output_dir_path = Path(output_dir_name)
    if not output_dir_name or output_dir_path.is_absolute() or output_dir_path.name != output_dir_name:
        raise ValueError("high_recall_windows.output_dir_name must be a single directory name")
    mode = str(_setting(raw, "mode", "sahi") or "sahi").strip().lower()
    if mode not in {"sahi", "direct_full_frame"}:
        raise ValueError(f"Unknown high_recall_windows mode: {mode}")
    return _HighRecallSettings(
        enabled=bool(_setting(raw, "enabled", False)),
        margin_frames=max(0, int(_setting(raw, "margin_frames", 0))),
        merge_gap_frames=min(30, max(0, int(_setting(raw, "merge_gap_frames", 30)))),
        max_total_frames=_positive_int(_setting(raw, "max_total_frames", 1800)),
        mode=mode,
        output_dir_name=output_dir_name,
        approved_actions_path=_optional_approved_actions_path(_setting(raw, "approved_actions_path", None)),
        approved_only=bool(_setting(raw, "approved_only", False)),
        max_speed_px_per_frame=float(_setting(raw, "max_speed_px_per_frame", 180.0)),
        max_jump_px=float(_setting(raw, "max_jump_px", 260.0)),
    )


def _setting(raw: Any, key: str, default: Any) -> Any:
    if isinstance(raw, dict):
        return raw.get(key, default)
    return getattr(raw, key, default)


def _optional_approved_actions_path(value: Any) -> Path | None:
    if value in (None, ""):
        return None
    path_text = str(value).strip()
    if not path_text:
        return None
    return Path(path_text)


def _resolve_configured_approved_actions_path(path: Path | None, *, output_dir: Path) -> Path | None:
    if path is None:
        return None
    return path if path.is_absolute() else output_dir / path


def _positive_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    parsed = _coerce_int(value)
    if parsed is None:
        raise ValueError("high_recall_windows.max_total_frames must be greater than 0 or null")
    if parsed <= 0:
        raise ValueError("high_recall_windows.max_total_frames must be greater than 0 or null")
    return parsed


def _coerce_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return int(parsed) if parsed == parsed and parsed not in (float("inf"), float("-inf")) else None


def _is_cancelled(should_cancel: Callable[[], bool] | None) -> bool:
    return should_cancel is not None and should_cancel()


def _terminate_running_processes(running: list[_RunningChunkProcess]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for running_process in list(running):
        process = running_process.process
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        exit_code = getattr(process, "returncode", None)
        if exit_code is None:
            exit_code = process.poll()
        results.append(_finalize_running_process(running_process, exit_code=-1 if exit_code is None else int(exit_code)))
        running.remove(running_process)
    return results


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
