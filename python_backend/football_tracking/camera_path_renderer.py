from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import shutil
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import cv2

from football_tracking.global_ball_trajectory import (
    GlobalBallTrajectoryError,
    _acquire_source_lease,
    _release_source_lease,
    _verify_source_lease,
)

CAMERA_PATH_NAME = "camera_path.v2.csv"
REPORT_NAME = "hybrid_broadcast_camera_report.v1.json"
REPORT_ARTIFACT_TYPE = "hybrid_broadcast_camera_report"
REPORT_SCHEMA_VERSION = "1.0"
MAX_REPORT_BYTES = 4 * 1024 * 1024

REQUIRED_COLUMNS = frozenset(
    {
        "Frame",
        "CenterX",
        "CenterY",
        "CropX1",
        "CropY1",
        "CropX2",
        "CropY2",
        "CropWidth",
        "CropHeight",
        "Status",
        "PanMode",
    }
)


class CameraPathRenderError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CameraPathRenderResult:
    frame_count: int
    target_width: int
    target_height: int
    status_counts: dict[str, int]
    output_video_path: Path | None


@dataclass(frozen=True, slots=True)
class _SourceMetadata:
    sha256: str
    width: int
    height: int
    fps: float
    frame_count: int


@dataclass(frozen=True, slots=True)
class _CameraPathRow:
    frame: int
    center_x: float
    center_y: float
    crop_x1: int
    crop_y1: int
    crop_x2: int
    crop_y2: int
    crop_width: int
    crop_height: int
    status: str
    pan_mode: str


def render_camera_path_video(
    source_video_path: Path,
    camera_path_path: Path,
    hybrid_report_path: Path,
    output_video_path: Path | None = None,
    *,
    target_width: int,
    target_height: int,
    codec: str = "mp4v",
    capture: Any | None = None,
    writer: Any | None = None,
) -> CameraPathRenderResult:
    """Render an audited camera path without inferring or replacing path states."""

    source_video_path = Path(source_video_path).resolve()
    camera_path_path = Path(camera_path_path).resolve()
    hybrid_report_path = Path(hybrid_report_path).resolve()
    resolved_output = Path(output_video_path).resolve() if output_video_path is not None else None
    _validate_arguments(
        source_video_path,
        camera_path_path,
        hybrid_report_path,
        resolved_output,
        target_width,
        target_height,
        codec,
        capture,
        writer,
    )
    source_lease: Any | None = None
    snapshot_root: Path | None = None
    output_lock: Any | None = None
    try:
        if resolved_output is not None:
            output_lock = _acquire_render_output_lock(resolved_output)
            if resolved_output.exists():
                raise CameraPathRenderError(f"output video already exists: {resolved_output}")
        try:
            source_lease = _acquire_source_lease(source_video_path)
        except GlobalBallTrajectoryError as exc:
            raise CameraPathRenderError(str(exc)) from exc
        snapshot_root = Path(tempfile.mkdtemp(prefix="football-tracking-camera-path-render-"))
        path_snapshot = snapshot_root / CAMERA_PATH_NAME
        path_stat, path_sha256, path_size = _capture_path_snapshot(camera_path_path, path_snapshot)
        report_stat = _stable_stat(hybrid_report_path, REPORT_NAME)
        report = _load_report(hybrid_report_path)
        source_metadata = _validate_report_bindings(
            report,
            source_sha256=source_lease.snapshot.sha256,
            source_size=source_lease.snapshot.size,
            camera_path_sha256=path_sha256,
            camera_path_size=path_size,
        )
        active_capture = capture if capture is not None else cv2.VideoCapture(str(source_lease.probe_path))
        active_writer: Any | None = writer
        pending_output: Path | None = None
        published_output: Path | None = None
        render_failed = True
        try:
            _validate_capture(active_capture, source_metadata)
            if active_writer is None:
                assert resolved_output is not None
                pending_output = resolved_output.with_name(
                    f".{resolved_output.stem}.pending-{uuid4().hex}{resolved_output.suffix}"
                )
                resolved_output.parent.mkdir(parents=True, exist_ok=True)
                fourcc = cv2.VideoWriter.fourcc(*codec)
                active_writer = cv2.VideoWriter(
                    str(pending_output),
                    fourcc,
                    source_metadata.fps,
                    (target_width, target_height),
                )
            _validate_writer(active_writer)
            result = _render_rows(
                active_capture,
                active_writer,
                path_snapshot,
                source_metadata,
                target_width=target_width,
                target_height=target_height,
            )
            render_failed = False
        finally:
            release_failure = _release_resources(active_writer, active_capture)
            if (render_failed or release_failure is not None) and pending_output is not None:
                try:
                    pending_output.unlink(missing_ok=True)
                except OSError:
                    pass
            if not render_failed and release_failure is not None:
                if isinstance(release_failure, (KeyboardInterrupt, SystemExit)):
                    raise release_failure
                raise CameraPathRenderError("unable to release video resources") from release_failure

        try:
            _validate_unchanged(camera_path_path, path_stat, CAMERA_PATH_NAME)
            if _sha256_file(camera_path_path) != path_sha256:
                raise CameraPathRenderError(f"{CAMERA_PATH_NAME} changed during rendering")
            _validate_unchanged(hybrid_report_path, report_stat, REPORT_NAME)
            _verify_source_lease(source_lease)
            if pending_output is not None:
                assert resolved_output is not None
                if not pending_output.is_file() or pending_output.stat().st_size <= 0:
                    raise CameraPathRenderError("rendered video is missing or empty")
                _validate_rendered_video(
                    pending_output,
                    frame_count=source_metadata.frame_count,
                    width=target_width,
                    height=target_height,
                    fps=source_metadata.fps,
                )
                _fsync_file(pending_output)
                _publish_rendered_video(pending_output, resolved_output)
                published_output = resolved_output
        except BaseException:
            if pending_output is not None:
                pending_output.unlink(missing_ok=True)
            raise

        return CameraPathRenderResult(
            frame_count=result.frame_count,
            target_width=target_width,
            target_height=target_height,
            status_counts=result.status_counts,
            output_video_path=published_output,
        )
    except GlobalBallTrajectoryError as exc:
        raise CameraPathRenderError(str(exc)) from exc
    finally:
        if source_lease is not None:
            try:
                _release_source_lease(source_lease)
            except BaseException:
                pass
        if snapshot_root is not None:
            shutil.rmtree(snapshot_root, ignore_errors=True)
        if output_lock is not None:
            try:
                _release_render_output_lock(output_lock)
            except BaseException:
                pass


def _validate_arguments(
    source_video_path: Path,
    camera_path_path: Path,
    hybrid_report_path: Path,
    output_video_path: Path | None,
    target_width: int,
    target_height: int,
    codec: str,
    capture: Any | None,
    writer: Any | None,
) -> None:
    for path, label in (
        (source_video_path, "source video"),
        (camera_path_path, CAMERA_PATH_NAME),
        (hybrid_report_path, REPORT_NAME),
    ):
        if not path.is_file():
            raise CameraPathRenderError(f"{label} not found: {path}")
    if camera_path_path.name != CAMERA_PATH_NAME:
        raise CameraPathRenderError(f"camera path must be named {CAMERA_PATH_NAME}")
    if hybrid_report_path.name != REPORT_NAME:
        raise CameraPathRenderError(f"hybrid report must be named {REPORT_NAME}")
    if isinstance(target_width, bool) or not isinstance(target_width, int) or target_width <= 0:
        raise CameraPathRenderError("target_width must be a positive integer")
    if isinstance(target_height, bool) or not isinstance(target_height, int) or target_height <= 0:
        raise CameraPathRenderError("target_height must be a positive integer")
    if writer is None and output_video_path is None:
        raise CameraPathRenderError("output_video_path is required when writer is not injected")
    if writer is not None and output_video_path is not None:
        raise CameraPathRenderError("output_video_path and an injected writer are mutually exclusive")
    if capture is not None and output_video_path is not None:
        raise CameraPathRenderError("output_video_path and an injected capture are mutually exclusive")
    if len(codec) != 4:
        raise CameraPathRenderError("codec must contain exactly four characters")
    if output_video_path is not None and output_video_path in {
        source_video_path,
        camera_path_path,
        hybrid_report_path,
    }:
        raise CameraPathRenderError("output video must not overwrite an input")
    if camera_path_path.parent != hybrid_report_path.parent:
        raise CameraPathRenderError("camera path and hybrid report must belong to the same generation")
    if output_video_path is not None and (
        output_video_path == hybrid_report_path.parent or hybrid_report_path.parent in output_video_path.parents
    ):
        raise CameraPathRenderError("output video must not modify the immutable hybrid camera generation")


def _load_report(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_REPORT_BYTES:
            raise CameraPathRenderError(f"{REPORT_NAME} exceeds the size limit")
        with path.open("r", encoding="utf-8") as handle:
            report = json.load(handle, object_pairs_hook=_object_without_duplicate_keys)
    except CameraPathRenderError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CameraPathRenderError(f"unable to read {REPORT_NAME}") from exc
    if not isinstance(report, dict):
        raise CameraPathRenderError(f"{REPORT_NAME} must contain a JSON object")
    if report.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise CameraPathRenderError("unsupported hybrid report schema_version")
    if report.get("artifact_type") != REPORT_ARTIFACT_TYPE:
        raise CameraPathRenderError("invalid hybrid report artifact_type")
    if report.get("status") != "succeeded" or report.get("complete") is not True:
        raise CameraPathRenderError("hybrid report is not complete and succeeded")
    return report


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise CameraPathRenderError(f"duplicate JSON key in {REPORT_NAME}: {key}")
        value[key] = item
    return value


def _validate_report_bindings(
    report: dict[str, Any],
    *,
    source_sha256: str,
    source_size: int,
    camera_path_sha256: str,
    camera_path_size: int,
) -> _SourceMetadata:
    source = _require_mapping(report.get("source_video"), "source_video")
    metadata = _SourceMetadata(
        sha256=_require_sha256(source.get("sha256"), "source_video.sha256"),
        width=_require_positive_int(source.get("width"), "source_video.width"),
        height=_require_positive_int(source.get("height"), "source_video.height"),
        fps=_require_positive_float(source.get("fps"), "source_video.fps"),
        frame_count=_require_positive_int(source.get("frame_count"), "source_video.frame_count"),
    )
    if source_size <= 0:
        raise CameraPathRenderError("source video is empty")
    if source_sha256 != metadata.sha256:
        raise CameraPathRenderError("source video sha256 does not match hybrid report")

    artifacts = _require_mapping(report.get("artifacts"), "artifacts")
    path_artifact = _require_mapping(artifacts.get(CAMERA_PATH_NAME), f"artifacts.{CAMERA_PATH_NAME}")
    expected_path_hash = _require_sha256(
        path_artifact.get("sha256"),
        f"artifacts.{CAMERA_PATH_NAME}.sha256",
    )
    expected_path_size = _require_nonnegative_int(
        path_artifact.get("size"),
        f"artifacts.{CAMERA_PATH_NAME}.size",
    )
    if camera_path_size != expected_path_size:
        raise CameraPathRenderError(f"{CAMERA_PATH_NAME} size does not match hybrid report")
    if camera_path_sha256 != expected_path_hash:
        raise CameraPathRenderError(f"{CAMERA_PATH_NAME} sha256 does not match hybrid report")
    return metadata


def _validate_capture(capture: Any, metadata: _SourceMetadata) -> None:
    if capture is None or not capture.isOpened():
        raise CameraPathRenderError("unable to open source video")
    actual_width = _capture_int(capture, cv2.CAP_PROP_FRAME_WIDTH, "width")
    actual_height = _capture_int(capture, cv2.CAP_PROP_FRAME_HEIGHT, "height")
    actual_frame_count = _capture_int(capture, cv2.CAP_PROP_FRAME_COUNT, "frame_count")
    actual_fps = float(capture.get(cv2.CAP_PROP_FPS))
    if not math.isfinite(actual_fps) or actual_fps <= 0:
        raise CameraPathRenderError("source video reports invalid fps")
    if (actual_width, actual_height, actual_frame_count) != (
        metadata.width,
        metadata.height,
        metadata.frame_count,
    ):
        raise CameraPathRenderError("source video metadata does not match hybrid report")
    if not math.isclose(actual_fps, metadata.fps, rel_tol=1e-6, abs_tol=1e-6):
        raise CameraPathRenderError("source video fps does not match hybrid report")


def _capture_int(capture: Any, property_id: int, label: str) -> int:
    raw = float(capture.get(property_id))
    if not math.isfinite(raw) or raw <= 0 or not math.isclose(raw, round(raw), abs_tol=1e-6):
        raise CameraPathRenderError(f"source video reports invalid {label}")
    return int(round(raw))


def _validate_writer(writer: Any) -> None:
    if writer is None or not writer.isOpened():
        raise CameraPathRenderError("unable to open camera path video writer")


def _release_resources(writer: Any | None, capture: Any | None) -> BaseException | None:
    failure: BaseException | None = None
    for resource in (writer, capture):
        if resource is None:
            continue
        try:
            resource.release()
        except BaseException as exc:  # pragma: no cover - OpenCV release failures are backend-specific.
            if failure is None:
                failure = exc
    return failure


def _render_rows(
    capture: Any,
    writer: Any,
    camera_path_path: Path,
    metadata: _SourceMetadata,
    *,
    target_width: int,
    target_height: int,
) -> CameraPathRenderResult:
    status_counts: Counter[str] = Counter()
    try:
        with camera_path_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            _validate_header(reader.fieldnames)
            for expected_frame in range(metadata.frame_count):
                try:
                    raw_row = next(reader)
                except StopIteration as exc:
                    raise CameraPathRenderError(f"{CAMERA_PATH_NAME} ends before frame {expected_frame}") from exc
                if None in raw_row:
                    raise CameraPathRenderError(f"{CAMERA_PATH_NAME} contains extra row fields")
                row = _parse_row(
                    raw_row,
                    expected_frame=expected_frame,
                    source_width=metadata.width,
                    source_height=metadata.height,
                    target_width=target_width,
                    target_height=target_height,
                )
                ok, frame = capture.read()
                if not ok or frame is None:
                    raise CameraPathRenderError(f"source video ends before frame {expected_frame}")
                if frame.ndim < 2 or frame.shape[:2] != (metadata.height, metadata.width):
                    raise CameraPathRenderError(f"source video frame {expected_frame} has unexpected dimensions")
                crop = frame[row.crop_y1 : row.crop_y2, row.crop_x1 : row.crop_x2]
                if crop.shape[:2] != (row.crop_height, row.crop_width):
                    raise CameraPathRenderError(f"camera path crop is invalid at frame {expected_frame}")
                rendered = cv2.resize(crop, (target_width, target_height), interpolation=cv2.INTER_LINEAR)
                writer.write(rendered)
                status_counts[row.status] += 1
            try:
                next(reader)
            except StopIteration:
                pass
            else:
                raise CameraPathRenderError(f"{CAMERA_PATH_NAME} contains frames outside the source domain")
            extra_ok, _ = capture.read()
            if extra_ok:
                raise CameraPathRenderError("source decoder produced frames outside bound source metadata")
    except CameraPathRenderError:
        raise
    except (OSError, UnicodeError, csv.Error) as exc:
        raise CameraPathRenderError(f"unable to read {CAMERA_PATH_NAME}") from exc
    return CameraPathRenderResult(
        frame_count=metadata.frame_count,
        target_width=target_width,
        target_height=target_height,
        status_counts=dict(sorted(status_counts.items())),
        output_video_path=None,
    )


def _validate_header(fieldnames: Sequence[str] | None) -> None:
    if fieldnames is None:
        raise CameraPathRenderError(f"{CAMERA_PATH_NAME} is empty")
    if len(fieldnames) != len(set(fieldnames)):
        raise CameraPathRenderError(f"{CAMERA_PATH_NAME} contains duplicate columns")
    if missing := REQUIRED_COLUMNS.difference(fieldnames):
        raise CameraPathRenderError(f"{CAMERA_PATH_NAME} missing required columns: {', '.join(sorted(missing))}")


def _parse_row(
    raw: dict[str, str],
    *,
    expected_frame: int,
    source_width: int,
    source_height: int,
    target_width: int,
    target_height: int,
) -> _CameraPathRow:
    frame = _parse_int(raw.get("Frame"), "Frame")
    if frame != expected_frame:
        raise CameraPathRenderError(
            f"{CAMERA_PATH_NAME} frame domain must be contiguous from zero; expected {expected_frame}, got {frame}"
        )
    row = _CameraPathRow(
        frame=frame,
        center_x=_parse_float(raw.get("CenterX"), "CenterX"),
        center_y=_parse_float(raw.get("CenterY"), "CenterY"),
        crop_x1=_parse_int(raw.get("CropX1"), "CropX1"),
        crop_y1=_parse_int(raw.get("CropY1"), "CropY1"),
        crop_x2=_parse_int(raw.get("CropX2"), "CropX2"),
        crop_y2=_parse_int(raw.get("CropY2"), "CropY2"),
        crop_width=_parse_int(raw.get("CropWidth"), "CropWidth"),
        crop_height=_parse_int(raw.get("CropHeight"), "CropHeight"),
        status=_parse_text(raw.get("Status"), "Status"),
        pan_mode=_parse_text(raw.get("PanMode"), "PanMode"),
    )
    if not (0 <= row.crop_x1 < row.crop_x2 <= source_width):
        raise CameraPathRenderError(f"camera path crop x bounds are invalid at frame {frame}")
    if not (0 <= row.crop_y1 < row.crop_y2 <= source_height):
        raise CameraPathRenderError(f"camera path crop y bounds are invalid at frame {frame}")
    if row.crop_width != row.crop_x2 - row.crop_x1 or row.crop_height != row.crop_y2 - row.crop_y1:
        raise CameraPathRenderError(f"camera path crop dimensions are inconsistent at frame {frame}")
    if abs(row.center_x - (row.crop_x1 + row.crop_x2) / 2.0) > 0.51:
        raise CameraPathRenderError(f"camera path CenterX is inconsistent at frame {frame}")
    if abs(row.center_y - (row.crop_y1 + row.crop_y2) / 2.0) > 0.51:
        raise CameraPathRenderError(f"camera path CenterY is inconsistent at frame {frame}")
    expected_width = row.crop_height * target_width / target_height
    if abs(row.crop_width - expected_width) > 1.0:
        raise CameraPathRenderError(f"camera path crop aspect ratio is invalid at frame {frame}")
    return row


def _parse_int(value: Any, label: str) -> int:
    if value is None or value == "":
        raise CameraPathRenderError(f"camera path {label} is missing")
    text = str(value).strip()
    try:
        parsed = int(text)
    except ValueError as exc:
        raise CameraPathRenderError(f"camera path {label} must be an integer") from exc
    return parsed


def _parse_float(value: Any, label: str) -> float:
    if value is None or value == "":
        raise CameraPathRenderError(f"camera path {label} is missing")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise CameraPathRenderError(f"camera path {label} must be finite") from exc
    if not math.isfinite(parsed):
        raise CameraPathRenderError(f"camera path {label} must be finite")
    return parsed


def _parse_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise CameraPathRenderError(f"camera path {label} must be non-empty canonical text")
    return value


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CameraPathRenderError(f"hybrid report {label} must be an object")
    return value


def _require_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CameraPathRenderError(f"hybrid report {label} must be a positive integer")
    return value


def _require_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CameraPathRenderError(f"hybrid report {label} must be a non-negative integer")
    return value


def _require_positive_float(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CameraPathRenderError(f"hybrid report {label} must be a positive finite number")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise CameraPathRenderError(f"hybrid report {label} must be a positive finite number")
    return parsed


def _require_sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CameraPathRenderError(f"hybrid report {label} must be a lowercase sha256")
    return value


def _capture_path_snapshot(path: Path, copy_path: Path) -> tuple[os.stat_result, str, int]:
    try:
        before = path.stat()
        if not path.is_file():
            raise CameraPathRenderError(f"{CAMERA_PATH_NAME} is not a regular file")
        digest = hashlib.sha256()
        copied = 0
        with path.open("rb") as source, copy_path.open("xb") as target:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                target.write(chunk)
                copied += len(chunk)
            target.flush()
            os.fsync(target.fileno())
        after = path.stat()
    except CameraPathRenderError:
        raise
    except OSError as exc:
        raise CameraPathRenderError(f"unable to capture stable {CAMERA_PATH_NAME}") from exc
    if _stat_token(before) != _stat_token(after) or copied != int(after.st_size):
        raise CameraPathRenderError(f"{CAMERA_PATH_NAME} changed while its stable snapshot was captured")
    return after, digest.hexdigest(), copied


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _stable_stat(path: Path, label: str) -> os.stat_result:
    try:
        before = path.stat()
        if not path.is_file():
            raise CameraPathRenderError(f"{label} is not a regular file")
        after = path.stat()
    except OSError as exc:
        raise CameraPathRenderError(f"unable to inspect {label}") from exc
    if _stat_token(before) != _stat_token(after):
        raise CameraPathRenderError(f"{label} changed while it was inspected")
    return after


def _validate_unchanged(path: Path, expected: os.stat_result, label: str) -> None:
    try:
        actual = path.stat()
    except OSError as exc:
        raise CameraPathRenderError(f"{label} changed during rendering") from exc
    if _stat_token(actual) != _stat_token(expected):
        raise CameraPathRenderError(f"{label} changed during rendering")


def _stat_token(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        getattr(value, "st_ctime_ns", 0),
    )


def _validate_rendered_video(
    path: Path,
    *,
    frame_count: int,
    width: int,
    height: int,
    fps: float,
) -> None:
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise CameraPathRenderError("rendered video cannot be reopened")
        actual_width = _capture_int(capture, cv2.CAP_PROP_FRAME_WIDTH, "rendered width")
        actual_height = _capture_int(capture, cv2.CAP_PROP_FRAME_HEIGHT, "rendered height")
        actual_count = _capture_int(capture, cv2.CAP_PROP_FRAME_COUNT, "rendered frame_count")
        actual_fps = float(capture.get(cv2.CAP_PROP_FPS))
        if (actual_width, actual_height, actual_count) != (width, height, frame_count):
            raise CameraPathRenderError("rendered video metadata does not match the requested path")
        if not math.isfinite(actual_fps) or not math.isclose(actual_fps, fps, rel_tol=1e-4, abs_tol=1e-3):
            raise CameraPathRenderError("rendered video fps does not match the source")
        for frame_index in range(frame_count):
            ok, frame = capture.read()
            if not ok or frame is None:
                raise CameraPathRenderError(f"rendered video ends before frame {frame_index}")
            if frame.ndim < 2 or frame.shape[:2] != (height, width):
                raise CameraPathRenderError(f"rendered video frame {frame_index} has unexpected dimensions")
        extra_ok, _ = capture.read()
        if extra_ok:
            raise CameraPathRenderError("rendered video contains frames outside the camera path domain")
    finally:
        capture.release()


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _publish_rendered_video(pending: Path, output: Path) -> None:
    if output.exists():
        raise CameraPathRenderError(f"output video already exists: {output}")
    published = False
    try:
        os.link(pending, output)
        published = True
        pending.unlink()
        _fsync_directory(output.parent)
    except BaseException:
        if published:
            try:
                output.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            pending.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _render_lock_path(output: Path) -> Path:
    scope = hashlib.sha256(str(output).casefold().encode("utf-8")).hexdigest()
    return Path(tempfile.gettempdir()) / f"football-tracking-camera-render-{scope}.lock"


def _acquire_render_output_lock(output: Path) -> Any:
    handle = _render_lock_path(output).open("a+b")
    try:
        handle.seek(0)
        if not handle.read(1):
            handle.seek(0)
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException as exc:
        handle.close()
        if isinstance(exc, Exception):
            raise CameraPathRenderError(f"output video is already locked: {output}") from exc
        raise
    return handle


def _release_render_output_lock(handle: Any) -> None:
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


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
