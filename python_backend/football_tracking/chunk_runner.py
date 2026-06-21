from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from concurrent.futures import CancelledError
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
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
    """Run temporal chunks, stitch raw outputs, then run global outputs."""
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
    chunks_root = config.output_dir / config.temporal_chunks.output_dir_name
    chunks_root.mkdir(parents=True, exist_ok=True)

    total_chunks = len(chunks)
    requested_workers = config.temporal_chunks.max_workers
    worker_count = effective_worker_count(
        requested=requested_workers,
        detector_device=config.detector.device,
        devices=config.temporal_chunks.devices,
        allow_gpu_oversubscription=config.temporal_chunks.allow_gpu_oversubscription,
    )
    jobs = _prepare_chunk_jobs(config, chunks, chunks_root)

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

    _write_execution_report(config.output_dir, execution)
    _raise_if_cancelled(should_cancel)
    _emit_progress(progress_callback, {"stage": "stitch", "chunk_count": total_chunks})
    try:
        stitch_chunk_outputs(chunks, [job.chunk_dir for job in jobs], config.output_dir, output_config=config.output)
    except Exception as exc:
        _write_stitch_report(config.output_dir, status="failed", error=str(exc))
        raise
    _write_stitch_report(config.output_dir, status="succeeded")
    _write_execution_report(config.output_dir, execution)

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


def _reset_temporal_chunks_report(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "temporal_chunks_report.json"
    if report_path.exists():
        report_path.unlink()


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
