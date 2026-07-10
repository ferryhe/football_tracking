from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from football_tracking.api.broadcast_api import validate_broadcast_quality_report
from football_tracking.broadcast_hybrid_orchestration import validate_final_broadcast_artifacts
from football_tracking.media_integrity import inspect_frame

TOOL_VERSION = "broadcast-acceptance-v1"
REPORT_NAME = "broadcast_acceptance_report.v1.json"
PROGRESS_NAME = "broadcast_acceptance_progress.v1.json"
DEFAULT_SEGMENT_FRAMES = 1_000
_HASH_CHUNK_BYTES = 1024 * 1024
_MAX_JSON_BYTES = 16 * 1024 * 1024
_WINDOWS_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class BroadcastAcceptanceError(RuntimeError):
    """Raised when a ready broadcast fails independent acceptance."""


class BroadcastAcceptanceUnavailable(BroadcastAcceptanceError):
    """Raised when an acceptance dependency is unavailable."""


def validate_broadcast_run(
    run_dir: Path,
    *,
    ffmpeg_executable: str | Path | None = None,
    segment_frames: int = DEFAULT_SEGMENT_FRAMES,
    resume: bool = False,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run one acceptance writer at a time for a resolved run directory."""

    raw_run_dir = Path(run_dir)
    resolved_run_dir = raw_run_dir.resolve()
    if not resolved_run_dir.is_dir() or _is_link_or_reparse(raw_run_dir):
        return _base_report(
            resolved_run_dir,
            status="fail",
            errors=[_error("invalid_run_directory", "run_dir must be an existing non-linked directory")],
        )
    try:
        lock = _acquire_acceptance_lock(resolved_run_dir)
    except BroadcastAcceptanceError as exc:
        return _base_report(
            resolved_run_dir,
            status="unavailable",
            errors=[_error("acceptance_writer_busy", _safe_exception_message(exc))],
        )
    try:
        run_dir_token = _directory_token(resolved_run_dir)
        return _validate_broadcast_run_locked(
            raw_run_dir,
            ffmpeg_executable=ffmpeg_executable,
            segment_frames=segment_frames,
            resume=resume,
            progress_callback=progress_callback,
            expected_run_dir_token=run_dir_token,
        )
    finally:
        _release_acceptance_lock(lock)


def _validate_broadcast_run_locked(
    run_dir: Path,
    *,
    ffmpeg_executable: str | Path | None = None,
    segment_frames: int = DEFAULT_SEGMENT_FRAMES,
    resume: bool = False,
    expected_run_dir_token: tuple[int, int],
    progress_callback: Callable[[dict[str, Any]], None] | None,
) -> dict[str, Any]:
    """Validate a ready broadcast and atomically publish an independent report.

    A ``KeyboardInterrupt`` is deliberately allowed to propagate. Completed segments
    remain in the atomic progress checkpoint, while no new final report is published.
    """

    raw_run_dir = Path(run_dir)
    resolved_run_dir = raw_run_dir.resolve()
    report_path = resolved_run_dir / REPORT_NAME
    _verify_directory_identity(resolved_run_dir, expected_run_dir_token)
    if _is_link_or_reparse(report_path):
        raise BroadcastAcceptanceError(f"refusing to replace linked artifact: {REPORT_NAME}")
    report_path.unlink(missing_ok=True)
    if not isinstance(segment_frames, int) or isinstance(segment_frames, bool) or segment_frames <= 0:
        report = _base_report(
            resolved_run_dir,
            status="fail",
            errors=[_error("invalid_segment_size", "segment_frames must be a positive integer")],
        )
        _publish_report_if_possible(resolved_run_dir, report, expected_run_dir_token)
        return report

    try:
        context = _validate_lineage(raw_run_dir)
    except Exception as exc:
        report = _base_report(
            resolved_run_dir,
            status="fail",
            errors=[_error("lineage_validation_failed", _safe_exception_message(exc))],
        )
        _publish_report_if_possible(resolved_run_dir, report, expected_run_dir_token)
        return report

    report: dict[str, Any]
    try:
        executable = _resolve_ffmpeg(ffmpeg_executable)
        ffmpeg_identity = _ffmpeg_identity(executable)
        opencv_identity = _opencv_decoder_identity(context["output_video"])
        source_probe = _probe_media(context["source_video"], executable)
        output_probe = _probe_media(context["output_video"], executable)
        frame_count, fps, width, height = _video_metadata(context["output_video"])

        plan = _build_segment_plan(frame_count, segment_frames)
        plan_sha256 = _canonical_sha256(
            {"frame_count": frame_count, "segment_frames": segment_frames, "segments": plan}
        )
        identity = {
            "tool_version": TOOL_VERSION,
            "quality_report_sha256": context["quality_report_sha256"],
            "source_video_sha256": context["source_video_sha256"],
            "output_video_sha256": context["output_video_sha256"],
            "segment_plan_sha256": plan_sha256,
            "ffmpeg_sha256": ffmpeg_identity["sha256"],
            "ffmpeg_version": ffmpeg_identity["version"],
            "opencv_version": opencv_identity["version"],
            "opencv_backend": opencv_identity["backend"],
        }
        sample_frames = _sample_frames(plan, frame_count)
        checkpoint, reusable = _prepare_checkpoint(
            resolved_run_dir / PROGRESS_NAME,
            identity=identity,
            plan=plan,
            sample_frames=sample_frames,
            allow_resume=resume,
        )
        _atomic_write_json(
            resolved_run_dir / PROGRESS_NAME,
            checkpoint,
            expected_parent_token=expected_run_dir_token,
        )

        reused_count = 0
        for planned in plan:
            index = planned["index"]
            if index in reusable:
                reused_count += 1
                continue
            try:
                completed = _decode_segment(
                    context["output_video"],
                    planned,
                    sample_frames,
                    width=width,
                    height=height,
                    frame_count=frame_count,
                )
                if not _is_reusable_segment(completed, planned, sample_frames):
                    raise BroadcastAcceptanceError(f"segment {index} returned an incomplete validation result")
            except KeyboardInterrupt:
                raise
            except BroadcastAcceptanceUnavailable:
                checkpoint["segments"][index] = {**planned, "status": "unavailable"}
                checkpoint["status"] = "failed"
                checkpoint["updated_at"] = _utc_now_iso()
                _atomic_write_json(
                    resolved_run_dir / PROGRESS_NAME,
                    checkpoint,
                    expected_parent_token=expected_run_dir_token,
                )
                raise
            except Exception as exc:
                checkpoint["segments"][index] = {
                    **planned,
                    "status": "failed",
                    "error": _safe_exception_message(exc),
                }
                checkpoint["status"] = "failed"
                checkpoint["updated_at"] = _utc_now_iso()
                _atomic_write_json(
                    resolved_run_dir / PROGRESS_NAME,
                    checkpoint,
                    expected_parent_token=expected_run_dir_token,
                )
                raise BroadcastAcceptanceError(
                    f"segment {index} validation failed: {_safe_exception_message(exc)}"
                ) from exc
            checkpoint["segments"][index] = completed
            checkpoint["updated_at"] = _utc_now_iso()
            _atomic_write_json(
                resolved_run_dir / PROGRESS_NAME,
                checkpoint,
                expected_parent_token=expected_run_dir_token,
            )

        checkpoint["status"] = "completed"
        checkpoint["updated_at"] = _utc_now_iso()
        _atomic_write_json(
            resolved_run_dir / PROGRESS_NAME,
            checkpoint,
            expected_parent_token=expected_run_dir_token,
        )

        _emit_progress(progress_callback, {"stage": "strict_ffmpeg_decode", "status": "started"})
        strict_decode = _full_decode_video(context["output_video"], executable)
        _emit_progress(progress_callback, {"stage": "strict_ffmpeg_decode", "status": "completed"})
        if strict_decode.get("frame_count") != frame_count:
            raise BroadcastAcceptanceError("strict ffmpeg decode frame count differs from the accepted output")

        duration_validation = _evaluate_durations(
            source_probe,
            output_probe,
            fps=fps,
            limitations=context["quality_report"]["limitations"],
        )
        media_errors, media_warnings = _media_integrity_findings(checkpoint["segments"])
        errors = [*duration_validation["errors"], *media_errors]
        if _ffmpeg_identity(executable) != ffmpeg_identity:
            raise BroadcastAcceptanceError("ffmpeg executable identity changed during acceptance")
        final_context = _validate_lineage(raw_run_dir)
        _verify_identity_unchanged(final_context, identity)

        report = _base_report(
            resolved_run_dir,
            status="fail" if errors else "pass",
            errors=errors,
        )
        report.update(
            {
                "identity": identity,
                "lineage": {
                    "quality_report": {
                        "path": "broadcast_quality_report.json",
                        "sha256": context["quality_report_sha256"],
                    },
                    "source_video": {
                        "path": str(context["source_video"]),
                        "sha256": context["source_video_sha256"],
                    },
                    "output_video": {
                        "path": "broadcast.mp4",
                        "sha256": context["output_video_sha256"],
                    },
                },
                "media_probe": {"source": source_probe, "output": output_probe},
                "decoder_identity": {"ffmpeg": ffmpeg_identity, "opencv": opencv_identity},
                "duration_validation": duration_validation,
                "frame_validation": {
                    "status": "pass" if not media_errors else "fail",
                    "frame_count": frame_count,
                    "fps": fps,
                    "width": width,
                    "height": height,
                    "segment_frames": segment_frames,
                    "segment_plan_sha256": plan_sha256,
                    "segment_count": len(plan),
                    "resume_requested": resume,
                    "reused_segment_count": reused_count,
                    "decoded_frame_count": sum(int(item["decoded_frames"]) for item in checkpoint["segments"]),
                    "strict_ffmpeg_decode": strict_decode,
                    "sample_frames": sorted(sample_frames),
                    "sample_results": _collect_sample_results(checkpoint["segments"]),
                    "warnings": media_warnings,
                },
                "limitations": list(context["quality_report"]["limitations"]),
            }
        )
    except BroadcastAcceptanceUnavailable as exc:
        report = _base_report(
            resolved_run_dir,
            status="unavailable",
            errors=[_error("acceptance_dependency_unavailable", _safe_exception_message(exc))],
        )
    except Exception as exc:
        report = _base_report(
            resolved_run_dir,
            status="fail",
            errors=[_error("acceptance_validation_failed", _safe_exception_message(exc))],
        )

    _atomic_write_json(report_path, report, expected_parent_token=expected_run_dir_token)
    return report


def _validate_lineage(run_dir: Path) -> dict[str, Any]:
    manifest = validate_final_broadcast_artifacts(run_dir)
    run_dir = Path(run_dir).resolve()
    quality_path = run_dir / "broadcast_quality_report.json"
    quality = validate_broadcast_quality_report(run_dir, quality_path)

    quality_snapshot, quality_sha256 = _load_json_object(quality_path, "broadcast quality report")
    if quality_snapshot != quality:
        raise BroadcastAcceptanceError("broadcast quality report changed after lineage validation")
    limitations = quality.get("limitations")
    if not isinstance(limitations, list) or any(not isinstance(item, str) or not item for item in limitations):
        raise BroadcastAcceptanceError("broadcast quality report limitations are invalid")
    capabilities = _required_mapping(quality.get("capabilities"), "broadcast quality capabilities")
    audio_preserved = capabilities.get("source_audio_preserved")
    if not isinstance(audio_preserved, bool):
        raise BroadcastAcceptanceError("broadcast quality audio capability is invalid")
    if ("source_audio_not_preserved" in limitations) == audio_preserved:
        raise BroadcastAcceptanceError("broadcast quality audio capability contradicts its limitations")

    artifacts = _required_mapping(manifest.get("artifacts"), "final artifact bindings")
    output_binding = _required_mapping(artifacts.get("broadcast.mp4"), "broadcast output binding")
    report_binding = _required_mapping(output_binding.get("source_report"), "broadcast render report binding")
    report_relative = _required_relative_path(report_binding.get("path"), "broadcast render report path")
    render_report_path = (run_dir / report_relative).resolve()
    try:
        render_report_path.relative_to(run_dir)
    except ValueError as exc:
        raise BroadcastAcceptanceError("broadcast render report is outside run_dir") from exc
    render_report, render_report_sha256 = _load_json_object(render_report_path, "broadcast render report")
    if render_report_sha256 != _required_sha256(report_binding.get("sha256"), "broadcast render report sha256"):
        raise BroadcastAcceptanceError("broadcast render report changed after lineage validation")

    source = _required_mapping(render_report.get("source_video"), "broadcast render source video")
    source_path = Path(_required_text(source.get("path"), "broadcast render source path")).resolve()
    source_sha256 = _stable_sha256(source_path, "source video")
    if source_sha256 != _required_sha256(source.get("sha256"), "broadcast render source sha256"):
        raise BroadcastAcceptanceError("source video does not match the validated render lineage")

    output_path = (run_dir / "broadcast.mp4").resolve()
    if output_path.parent != run_dir:
        raise BroadcastAcceptanceError("broadcast output must be a direct run artifact")
    output_sha256 = _stable_sha256(output_path, "broadcast output")
    if output_sha256 != _required_sha256(output_binding.get("sha256"), "broadcast output sha256"):
        raise BroadcastAcceptanceError("broadcast output changed after lineage validation")

    return {
        "quality_report": quality,
        "quality_report_sha256": quality_sha256,
        "source_video": source_path,
        "source_video_sha256": source_sha256,
        "output_video": output_path,
        "output_video_sha256": output_sha256,
    }


def _resolve_ffmpeg(explicit: str | Path | None) -> str:
    if explicit is not None:
        candidate = str(explicit).strip()
        if not candidate:
            raise BroadcastAcceptanceUnavailable("explicit ffmpeg executable is empty")
        resolved = _resolve_executable(candidate)
        if resolved is None:
            raise BroadcastAcceptanceUnavailable("explicit ffmpeg executable is unavailable")
        return resolved

    try:
        import imageio_ffmpeg  # pyright: ignore[reportMissingImports]

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, OSError, RuntimeError):
        bundled = None
    if bundled:
        resolved = _resolve_executable(str(bundled))
        if resolved is not None:
            return resolved
    from_path = _resolve_executable("ffmpeg")
    if from_path:
        return from_path
    raise BroadcastAcceptanceUnavailable("ffmpeg is not available on PATH and no bundled executable was found")


def _resolve_executable(candidate: str) -> str | None:
    path = Path(candidate).expanduser()
    if path.is_absolute() or path.parent != Path("."):
        return str(path.resolve()) if path.is_file() else None
    found = shutil.which(candidate)
    if not found:
        return None
    resolved = Path(found).resolve()
    return str(resolved) if resolved.is_file() else None


def _ffmpeg_identity(executable: str) -> dict[str, str]:
    path = Path(executable).resolve()
    digest = _stable_sha256(path, "ffmpeg executable")
    try:
        completed = subprocess.run(
            [str(path), "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BroadcastAcceptanceUnavailable("ffmpeg version probe failed") from exc
    lines = completed.stdout.splitlines()
    first_line = lines[0].strip() if lines else ""
    if completed.returncode != 0 or not first_line.startswith("ffmpeg version "):
        raise BroadcastAcceptanceUnavailable("ffmpeg version contract is unavailable")
    return {"path": str(path), "sha256": digest, "version": first_line[:300]}


def _opencv_decoder_identity(path: Path) -> dict[str, str]:
    try:
        import cv2
    except ImportError as exc:
        raise BroadcastAcceptanceUnavailable("OpenCV is unavailable") from exc
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise BroadcastAcceptanceUnavailable("broadcast output could not be opened by OpenCV")
        try:
            backend = str(capture.getBackendName())
        except (AttributeError, cv2.error):
            backend = str(int(capture.get(cv2.CAP_PROP_BACKEND)))
    finally:
        capture.release()
    return {"version": str(cv2.__version__), "backend": backend}


def _probe_media(path: Path, executable: str) -> dict[str, Any]:
    video = _probe_stream(path, executable, stream="video", required=True)
    frame_count, fps, width, height = _video_metadata(path)
    progress_frame_count = video.get("frame_count")
    if progress_frame_count is not None and progress_frame_count != frame_count:
        raise BroadcastAcceptanceError(
            f"video frame count differs between ffmpeg progress ({progress_frame_count}) and OpenCV ({frame_count})"
        )
    last_packet_timestamp = video.get("duration_seconds")
    video.update(
        {
            "frame_count": frame_count,
            "ffmpeg_progress_frame_count": progress_frame_count,
            "duration_seconds": round(frame_count / fps, 6),
            "last_packet_timestamp_seconds": last_packet_timestamp,
            "fps": fps,
            "width": width,
            "height": height,
        }
    )
    return {
        "path": str(Path(path).resolve()),
        "video": video,
        "audio": _probe_stream(path, executable, stream="audio", required=False),
    }


def _probe_stream(
    path: Path,
    executable: str,
    *,
    stream: str,
    required: bool,
    copy_stream: bool = True,
) -> dict[str, Any]:
    is_video = stream == "video"
    if not is_video and stream != "audio":
        raise ValueError(f"unsupported media stream: {stream}")
    command = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-stats_period",
        "60",
        "-progress",
        "pipe:1",
        "-nostats",
    ]
    if not copy_stream:
        command.extend(["-err_detect", "explode", "-xerror"])
    command.extend(
        [
            "-i",
            str(Path(path).resolve()),
            "-map",
            "0:v:0" if is_video else "0:a:0",
            "-an" if is_video else "-vn",
        ]
    )
    if copy_stream:
        command.extend(["-c", "copy"])
    command.extend(["-f", "null", "-"])
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise BroadcastAcceptanceUnavailable("ffmpeg could not be started") from exc

    if completed.returncode != 0:
        missing_marker = "matches no streams" in completed.stderr or "does not contain any stream" in completed.stderr
        if not required and missing_marker and not completed.stdout.strip():
            return {"present": False, "duration_seconds": None, "frame_count": None}
        raise BroadcastAcceptanceError(f"ffmpeg {stream} probe failed")

    values, ended = _parse_ffmpeg_progress(completed.stdout)
    if not ended:
        raise BroadcastAcceptanceError(f"ffmpeg {stream} probe did not publish a final progress record")
    duration = _progress_duration(values)
    if duration is None or not math.isfinite(duration) or duration < 0:
        raise BroadcastAcceptanceError(f"ffmpeg {stream} duration is unavailable")
    frame_count: int | None = None
    if is_video:
        raw_frames = values.get("frame")
        try:
            frame_count = int(raw_frames) if raw_frames is not None else None
        except ValueError as exc:
            raise BroadcastAcceptanceError("ffmpeg video frame count is invalid") from exc
        if frame_count is not None and frame_count <= 0:
            frame_count = None
        if not copy_stream and frame_count is None:
            raise BroadcastAcceptanceError("ffmpeg video frame count is unavailable")
    return {
        "present": True,
        "duration_seconds": round(duration, 6),
        "frame_count": frame_count,
    }


def _full_decode_video(path: Path, executable: str) -> dict[str, Any]:
    return _probe_stream(path, executable, stream="video", required=True, copy_stream=False)


def _parse_ffmpeg_progress(payload: str) -> tuple[dict[str, str], bool]:
    values: dict[str, str] = {}
    ended = False
    for raw_line in payload.splitlines():
        if "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            values[key] = value
        if key == "progress" and value == "end":
            ended = True
    return values, ended


def _progress_duration(values: dict[str, str]) -> float | None:
    for key in ("out_time_us", "out_time_ms"):
        raw = values.get(key)
        if raw is not None:
            try:
                return int(raw) / 1_000_000.0
            except ValueError:
                return None
    raw_time = values.get("out_time")
    if raw_time is None:
        return None
    parts = raw_time.split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]) * 3600.0 + int(parts[1]) * 60.0 + float(parts[2])
    except ValueError:
        return None


def _video_metadata(path: Path) -> tuple[int, float, int, int]:
    try:
        import cv2
    except ImportError as exc:
        raise BroadcastAcceptanceUnavailable("OpenCV is unavailable") from exc
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise BroadcastAcceptanceUnavailable("broadcast output could not be opened by OpenCV")
        raw_frame_count = float(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        width = int(round(float(capture.get(cv2.CAP_PROP_FRAME_WIDTH))))
        height = int(round(float(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))))
    finally:
        capture.release()
    frame_count = int(round(raw_frame_count))
    if (
        not math.isfinite(raw_frame_count)
        or frame_count <= 0
        or abs(raw_frame_count - frame_count) > 0.01
        or not math.isfinite(fps)
        or fps <= 0
        or width <= 0
        or height <= 0
    ):
        raise BroadcastAcceptanceError("broadcast output metadata is incomplete or invalid")
    return frame_count, fps, width, height


def _build_segment_plan(frame_count: int, segment_frames: int) -> list[dict[str, int]]:
    return [
        {
            "index": index,
            "start_frame": start,
            "end_frame_exclusive": min(start + segment_frames, frame_count),
        }
        for index, start in enumerate(range(0, frame_count, segment_frames))
    ]


def _sample_frames(plan: list[dict[str, int]], frame_count: int) -> set[int]:
    samples = {0, frame_count // 2, frame_count - 1}
    samples.update(
        min(
            segment["end_frame_exclusive"] - 1,
            segment["start_frame"] + (segment["end_frame_exclusive"] - segment["start_frame"]) // 2,
        )
        for segment in plan
    )
    return samples


def _prepare_checkpoint(
    path: Path,
    *,
    identity: dict[str, str],
    plan: list[dict[str, int]],
    sample_frames: set[int],
    allow_resume: bool,
) -> tuple[dict[str, Any], set[int]]:
    reusable: set[int] = set()
    existing: dict[str, Any] | None = None
    if allow_resume and path.is_file() and not _is_link_or_reparse(path):
        try:
            existing, _ = _load_json_object(path, "broadcast acceptance progress")
        except (BroadcastAcceptanceError, OSError):
            existing = None

    existing_segments: list[Any] = []
    if (
        existing is not None
        and existing.get("schema_version") == "1.0"
        and existing.get("artifact_type") == "broadcast_acceptance_progress"
        and existing.get("tool_version") == TOOL_VERSION
        and existing.get("identity") == identity
        and isinstance(existing.get("segments"), list)
    ):
        existing_segments = existing["segments"]

    segments: list[dict[str, Any]] = []
    for planned in plan:
        index = planned["index"]
        candidate = existing_segments[index] if index < len(existing_segments) else None
        if _is_reusable_segment(candidate, planned, sample_frames):
            assert isinstance(candidate, dict)
            segments.append(candidate)
            reusable.add(index)
        else:
            segments.append({**planned, "status": "pending"})
    checkpoint = {
        "schema_version": "1.0",
        "artifact_type": "broadcast_acceptance_progress",
        "tool_version": TOOL_VERSION,
        "identity": identity,
        "status": "in_progress",
        "segments": segments,
        "updated_at": _utc_now_iso(),
    }
    return checkpoint, reusable


def _is_reusable_segment(candidate: Any, planned: dict[str, int], sample_frames: set[int]) -> bool:
    if not isinstance(candidate, dict):
        return False
    expected_samples = {
        str(frame) for frame in sample_frames if planned["start_frame"] <= frame < planned["end_frame_exclusive"]
    }
    sample_results = candidate.get("sample_results")
    if not isinstance(sample_results, dict) or set(sample_results) != expected_samples:
        return False
    dimensions = candidate.get("dimensions")
    if (
        not isinstance(dimensions, dict)
        or not isinstance(dimensions.get("width"), int)
        or isinstance(dimensions.get("width"), bool)
        or dimensions["width"] <= 0
        or not isinstance(dimensions.get("height"), int)
        or isinstance(dimensions.get("height"), bool)
        or dimensions["height"] <= 0
    ):
        return False
    if any(
        not isinstance(result, dict)
        or result.get("frame_index") != int(frame)
        or result.get("status") != "ok"
        or result.get("likely_corrupt") is not False
        or not isinstance(result.get("low_information"), bool)
        or not isinstance(result.get("reasons"), list)
        for frame, result in sample_results.items()
    ):
        return False
    return bool(
        candidate.get("status") == "completed"
        and all(candidate.get(key) == value for key, value in planned.items())
        and candidate.get("decoded_frames") == planned["end_frame_exclusive"] - planned["start_frame"]
    )


def _decode_segment(
    path: Path,
    segment: dict[str, int],
    sample_frames: set[int],
    *,
    width: int,
    height: int,
    frame_count: int,
) -> dict[str, Any]:
    try:
        import cv2
    except ImportError as exc:
        raise BroadcastAcceptanceUnavailable("OpenCV is unavailable") from exc

    start = segment["start_frame"]
    end = segment["end_frame_exclusive"]
    capture = cv2.VideoCapture(str(path))
    decoded = 0
    samples: dict[str, Any] = {}
    try:
        if not capture.isOpened():
            raise BroadcastAcceptanceUnavailable(f"segment {segment['index']} could not open the output video")
        if start and not capture.set(cv2.CAP_PROP_POS_FRAMES, start):
            raise BroadcastAcceptanceError(f"segment {segment['index']} could not seek to its exact start frame")
        position = float(capture.get(cv2.CAP_PROP_POS_FRAMES))
        if not math.isfinite(position) or abs(position - start) > 0.01:
            raise BroadcastAcceptanceError(f"segment {segment['index']} started at frame {position}, expected {start}")
        for frame_index in range(start, end):
            ok, frame = capture.read()
            if not ok or frame is None or getattr(frame, "size", 0) == 0:
                raise BroadcastAcceptanceError(f"output frame {frame_index} is not decodable")
            frame_height, frame_width = frame.shape[:2]
            if int(frame_width) != width or int(frame_height) != height:
                raise BroadcastAcceptanceError(f"output frame {frame_index} dimensions changed")
            decoded += 1
            if frame_index in sample_frames:
                inspection = inspect_frame(frame)
                inspection["frame_index"] = frame_index
                samples[str(frame_index)] = inspection
            next_position = float(capture.get(cv2.CAP_PROP_POS_FRAMES))
            if not math.isfinite(next_position) or abs(next_position - (frame_index + 1)) > 0.01:
                raise BroadcastAcceptanceError(f"decoder lost exact frame position after frame {frame_index}")
        if end == frame_count:
            extra_ok, extra_frame = capture.read()
            if extra_ok or extra_frame is not None:
                raise BroadcastAcceptanceError("output contains frames beyond its declared frame count")
    finally:
        capture.release()

    return {
        **segment,
        "status": "completed",
        "decoded_frames": decoded,
        "sample_results": samples,
        "dimensions": {"width": width, "height": height},
    }


def _evaluate_durations(
    source: dict[str, Any],
    output: dict[str, Any],
    *,
    fps: float,
    limitations: list[str],
) -> dict[str, Any]:
    tolerance = max(1.0 / fps, 0.1)
    errors: list[dict[str, str]] = []
    known_limitations: list[str] = []
    source_video = _required_duration(source, "video", "source")
    output_video = _required_duration(output, "video", "output")
    if abs(source_video - output_video) > tolerance:
        errors.append(_error("video_duration_mismatch", "source and output video durations differ beyond tolerance"))

    source_audio = _optional_duration(source, "audio", "source")
    output_audio = _optional_duration(output, "audio", "output")
    audio_not_preserved = "source_audio_not_preserved" in limitations
    if source_audio is not None and abs(source_video - source_audio) > tolerance:
        errors.append(_error("source_audio_video_duration_mismatch", "source audio and video durations differ"))
    if source_audio is None and output_audio is not None:
        errors.append(_error("unexpected_output_audio", "output contains audio but the source does not"))
    elif source_audio is not None and output_audio is None:
        if audio_not_preserved:
            known_limitations.append("source_audio_not_preserved")
        else:
            errors.append(_error("source_audio_missing_from_output", "source audio is missing from the output"))
    elif source_audio is not None and output_audio is not None:
        if audio_not_preserved:
            errors.append(
                _error(
                    "output_audio_conflicts_with_quality_limitation",
                    "output audio contradicts the source_audio_not_preserved quality limitation",
                )
            )
        if abs(source_audio - output_audio) > tolerance:
            errors.append(
                _error("audio_duration_mismatch", "source and output audio durations differ beyond tolerance")
            )
        if abs(output_video - output_audio) > tolerance:
            errors.append(_error("output_audio_video_duration_mismatch", "output audio and video durations differ"))

    return {
        "status": "fail" if errors else ("known_limitation" if known_limitations else "pass"),
        "tolerance_seconds": round(tolerance, 6),
        "known_limitations": known_limitations,
        "errors": errors,
    }


def _required_duration(media: dict[str, Any], stream: str, label: str) -> float:
    raw_stream = _required_mapping(media.get(stream), f"{label} {stream} probe")
    if raw_stream.get("present") is not True:
        raise BroadcastAcceptanceError(f"{label} {stream} stream is unavailable")
    duration = raw_stream.get("duration_seconds")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not math.isfinite(float(duration)):
        raise BroadcastAcceptanceError(f"{label} {stream} duration is invalid")
    return float(duration)


def _optional_duration(media: dict[str, Any], stream: str, label: str) -> float | None:
    raw_stream = _required_mapping(media.get(stream), f"{label} {stream} probe")
    if raw_stream.get("present") is False:
        if raw_stream.get("duration_seconds") is not None:
            raise BroadcastAcceptanceError(f"absent {label} {stream} cannot have a duration")
        return None
    return _required_duration(media, stream, label)


def _media_integrity_findings(segments: list[dict[str, Any]]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, Any]] = []
    for frame, result in _collect_sample_results(segments).items():
        if result.get("likely_corrupt"):
            errors.append(_error("sample_frame_corrupt", f"sample frame {frame} failed media integrity inspection"))
        elif result.get("low_information"):
            warnings.append({"code": "sample_frame_low_information", "frame_index": int(frame)})
    return errors, warnings


def _collect_sample_results(segments: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        frame: result
        for segment in segments
        for frame, result in _required_mapping(segment.get("sample_results"), "segment sample results").items()
    }


def _verify_identity_unchanged(context: dict[str, Any], identity: dict[str, str]) -> None:
    current = {
        "quality_report_sha256": context["quality_report_sha256"],
        "source_video_sha256": context["source_video_sha256"],
        "output_video_sha256": context["output_video_sha256"],
    }
    if any(identity[key] != value for key, value in current.items()):
        raise BroadcastAcceptanceError("acceptance identity changed during validation")


def _base_report(run_dir: Path, *, status: str, errors: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "artifact_type": "broadcast_acceptance_report",
        "tool_version": TOOL_VERSION,
        "status": status,
        "generated_at": _utc_now_iso(),
        "run_dir": str(run_dir),
        "errors": errors,
    }


def _publish_report_if_possible(
    run_dir: Path,
    report: dict[str, Any],
    expected_run_dir_token: tuple[int, int],
) -> None:
    if run_dir.is_dir() and not _is_link_or_reparse(run_dir):
        _atomic_write_json(
            run_dir / REPORT_NAME,
            report,
            expected_parent_token=expected_run_dir_token,
        )


def _atomic_write_json(
    path: Path,
    payload: dict[str, Any],
    *,
    expected_parent_token: tuple[int, int] | None = None,
) -> None:
    path = Path(path)
    if expected_parent_token is not None:
        _verify_directory_identity(path.parent, expected_parent_token)
    if _is_link_or_reparse(path):
        raise BroadcastAcceptanceError(f"refusing to replace linked artifact: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if expected_parent_token is not None:
            _verify_directory_identity(path.parent, expected_parent_token)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _acquire_acceptance_lock(run_dir: Path) -> BinaryIO:
    run_dir = Path(run_dir)
    normalized = os.path.normcase(os.path.normpath(str(run_dir)))
    lock_key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    lock_path = run_dir.parent / f".broadcast-acceptance-{lock_key}.lock"
    if _is_link_or_reparse(lock_path):
        raise BroadcastAcceptanceError("broadcast acceptance lock must not be linked")
    handle = lock_path.open("a+b")
    try:
        if lock_path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, ValueError) as exc:
        handle.close()
        raise BroadcastAcceptanceError("another broadcast acceptance writer is active") from exc
    return handle


def _release_acceptance_lock(handle: BinaryIO) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _directory_token(path: Path) -> tuple[int, int]:
    current = Path(path).stat()
    return int(current.st_dev), int(current.st_ino)


def _verify_directory_identity(path: Path, expected: tuple[int, int]) -> None:
    try:
        current = _directory_token(path)
    except OSError as exc:
        raise BroadcastAcceptanceError("broadcast run directory became unavailable") from exc
    if current != expected:
        raise BroadcastAcceptanceError("broadcast run directory identity changed during acceptance")


def _load_json_object(path: Path, label: str) -> tuple[dict[str, Any], str]:
    raw = Path(path)
    if _is_link_or_reparse(raw):
        raise BroadcastAcceptanceError(f"{label} must not be a symlink or reparse point")
    try:
        if raw.stat().st_size > _MAX_JSON_BYTES:
            raise BroadcastAcceptanceError(f"{label} exceeds the JSON size bound")
        snapshot = raw.read_bytes()
    except OSError as exc:
        raise BroadcastAcceptanceError(f"{label} is unavailable") from exc
    try:
        payload = json.loads(snapshot.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BroadcastAcceptanceError(f"{label} is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise BroadcastAcceptanceError(f"{label} must be a JSON object")
    digest = hashlib.sha256(snapshot).hexdigest()
    if _stable_sha256(raw, label) != digest:
        raise BroadcastAcceptanceError(f"{label} changed while it was read")
    return payload, digest


def _stable_sha256(path: Path, label: str) -> str:
    raw = Path(path)
    if _is_link_or_reparse(raw):
        raise BroadcastAcceptanceError(f"{label} must not be a symlink or reparse point")
    try:
        before = _stat_token(raw)
        digest = hashlib.sha256()
        with raw.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
                digest.update(chunk)
        after = _stat_token(raw)
    except OSError as exc:
        raise BroadcastAcceptanceError(f"{label} is unavailable") from exc
    if before != after:
        raise BroadcastAcceptanceError(f"{label} changed while it was hashed")
    return digest.hexdigest()


def _stat_token(path: Path) -> tuple[int, int, int, int, int]:
    current = path.stat()
    return (
        int(current.st_dev),
        int(current.st_ino),
        int(current.st_size),
        int(current.st_mtime_ns),
        int(current.st_ctime_ns),
    )


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = Path(path).lstat()
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _WINDOWS_REPARSE_ATTRIBUTE)


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _required_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BroadcastAcceptanceError(f"{label} must be an object")
    return value


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BroadcastAcceptanceError(f"{label} must be non-empty text")
    return value.strip()


def _required_relative_path(value: Any, label: str) -> Path:
    path = Path(_required_text(value, label))
    if path.is_absolute() or ".." in path.parts:
        raise BroadcastAcceptanceError(f"{label} must be run-relative")
    return path


def _required_sha256(value: Any, label: str) -> str:
    text = _required_text(value, label).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise BroadcastAcceptanceError(f"{label} must be a SHA-256 digest")
    return text


def _error(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _emit_progress(
    callback: Callable[[dict[str, Any]], None] | None,
    payload: dict[str, Any],
) -> None:
    if callback is not None:
        callback(payload)


def _safe_exception_message(exc: BaseException) -> str:
    message = " ".join(str(exc).split())
    return message[:500] or exc.__class__.__name__


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
