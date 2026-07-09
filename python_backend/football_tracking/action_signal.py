from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

ACTION_SIGNAL_SCHEMA_VERSION = "1.0"
ACTION_CALIBRATION_SCHEMA_VERSION = "1.0"
ACTION_TRACK_NAME = "action_track.csv"
ACTION_SIGNAL_REPORT_NAME = "action_signal_report.v1.json"
ACTION_SIGNAL_DIAGNOSTICS_NAME = "action_signal_diagnostics.v1.jsonl"
ACTION_DIRECTOR_SOURCE_SHA256 = "7533C69CE3DB28817F9D76DC753F8AB580D9F94EB79DD252E52E8A71CA3DFBD5"
ACTION_CALIBRATION_ASPECT_RATIO_TOLERANCE = 1e-4
ACTION_SIGNAL_SUCCESS_STATUSES = frozenset({"complete", "bounded_complete"})

Point = tuple[float, float]
Polygon = tuple[Point, ...]


@dataclass(frozen=True, slots=True)
class ActionCalibration:
    """Per-match field mask confirmed against three sample frames."""

    source_width: int
    source_height: int
    confirmed_sample_frames: tuple[int, int, int]
    field_polygon: Polygon
    exclusion_polygons: tuple[Polygon, ...] = ()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ActionCalibration:
        if not isinstance(payload, dict):
            raise ValueError("action calibration must be a JSON object")
        if payload.get("schema_version") != ACTION_CALIBRATION_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {ACTION_CALIBRATION_SCHEMA_VERSION!r}")

        source_resolution = payload.get("source_resolution")
        if not isinstance(source_resolution, list) or len(source_resolution) != 2:
            raise ValueError("source_resolution must be [width, height]")
        source_width = _positive_int(source_resolution[0], "source_resolution[0]")
        source_height = _positive_int(source_resolution[1], "source_resolution[1]")

        raw_frames = payload.get("confirmed_sample_frames")
        if not isinstance(raw_frames, list) or len(raw_frames) != 3:
            raise ValueError("confirmed_sample_frames must contain exactly three frame indexes")
        parsed_frames = tuple(
            _nonnegative_int(value, f"confirmed_sample_frames[{index}]") for index, value in enumerate(raw_frames)
        )
        if len(set(parsed_frames)) != 3:
            raise ValueError("confirmed_sample_frames must be distinct")
        frames = (parsed_frames[0], parsed_frames[1], parsed_frames[2])
        if not frames[0] < frames[1] < frames[2]:
            raise ValueError("confirmed_sample_frames must be strictly increasing")

        field_polygon = _parse_polygon(
            payload.get("field_polygon"),
            name="field_polygon",
            source_width=source_width,
            source_height=source_height,
        )
        raw_exclusions = payload.get("exclusion_polygons", [])
        if not isinstance(raw_exclusions, list):
            raise ValueError("exclusion_polygons must be a list")
        exclusions = tuple(
            _parse_polygon(
                raw_polygon,
                name=f"exclusion_polygons[{index}]",
                source_width=source_width,
                source_height=source_height,
            )
            for index, raw_polygon in enumerate(raw_exclusions)
        )
        return cls(
            source_width=source_width,
            source_height=source_height,
            confirmed_sample_frames=frames,
            field_polygon=field_polygon,
            exclusion_polygons=exclusions,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ACTION_CALIBRATION_SCHEMA_VERSION,
            "source_resolution": [self.source_width, self.source_height],
            "confirmed_sample_frames": list(self.confirmed_sample_frames),
            "field_polygon": [[x, y] for x, y in self.field_polygon],
            "exclusion_polygons": [[[x, y] for x, y in polygon] for polygon in self.exclusion_polygons],
        }


@dataclass(frozen=True, slots=True)
class ActionSignalSettings:
    process_width: int = 640
    smoothing: float = 0.18
    min_component_area: int = 3
    max_component_area: int = 900
    background_history: int = 450
    variance_threshold: float = 28.0
    warmup_frames: int = 2
    hold_frames: int = 20
    hold_confidence_decay: float = 0.85

    def __post_init__(self) -> None:
        _config_int(self.process_width, "process_width", positive=True)
        _config_int(self.min_component_area, "min_component_area", positive=True)
        _config_int(self.max_component_area, "max_component_area", positive=True)
        _config_int(self.background_history, "background_history", positive=True)
        _config_int(self.warmup_frames, "warmup_frames", positive=False)
        _config_int(self.hold_frames, "hold_frames", positive=False)
        smoothing = _config_float(self.smoothing, "smoothing")
        variance_threshold = _config_float(self.variance_threshold, "variance_threshold")
        hold_confidence_decay = _config_float(self.hold_confidence_decay, "hold_confidence_decay")
        if not 0.0 < smoothing <= 1.0:
            raise ValueError("smoothing must be in (0, 1]")
        if self.max_component_area < self.min_component_area:
            raise ValueError("max_component_area must be at least min_component_area")
        if variance_threshold <= 0.0:
            raise ValueError("variance_threshold must be positive")
        if not 0.0 <= hold_confidence_decay <= 1.0:
            raise ValueError("hold_confidence_decay must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class ActionMeasurement:
    center_x: float
    center_y: float
    range_width: float
    range_height: float
    confidence: float
    component_count: int
    motion_area: int
    motion_fraction: float


@dataclass(frozen=True, slots=True)
class ActionTrackPoint:
    frame_index: int
    x: float | None
    y: float | None
    range_width: float | None
    range_height: float | None
    confidence: float
    status: str
    component_count: int
    motion_area: int
    reason: str

    def csv_row(self) -> list[str | int]:
        return [
            self.frame_index,
            _csv_number(self.x, 2),
            _csv_number(self.y, 2),
            _csv_number(self.range_width, 2),
            _csv_number(self.range_height, 2),
            f"{self.confidence:.4f}",
            self.status,
            self.component_count,
            self.motion_area,
            self.reason,
        ]


class ActionSignalTracker:
    """Smooth detections and expose only a finite, decaying hold."""

    def __init__(self, *, smoothing: float, hold_frames: int, hold_confidence_decay: float) -> None:
        smoothing = _config_float(smoothing, "smoothing")
        hold_confidence_decay = _config_float(hold_confidence_decay, "hold_confidence_decay")
        _config_int(hold_frames, "hold_frames", positive=False)
        if not 0.0 < smoothing <= 1.0:
            raise ValueError("smoothing must be in (0, 1]")
        if not 0.0 <= hold_confidence_decay <= 1.0:
            raise ValueError("hold_confidence_decay must be in [0, 1]")
        self.smoothing = smoothing
        self.hold_frames = hold_frames
        self.hold_confidence_decay = hold_confidence_decay
        self._smoothed: tuple[float, float, float, float] | None = None
        self._last_detected_confidence = 0.0
        self._missing_frames = 0

    def update(self, frame_index: int, measurement: ActionMeasurement | None) -> ActionTrackPoint:
        if measurement is not None:
            current = (
                measurement.center_x,
                measurement.center_y,
                measurement.range_width,
                measurement.range_height,
            )
            if self._smoothed is None:
                self._smoothed = current
            else:
                self._smoothed = tuple(
                    _lerp(previous, target, self.smoothing)
                    for previous, target in zip(self._smoothed, current, strict=True)
                )
            self._missing_frames = 0
            self._last_detected_confidence = measurement.confidence
            return self._point(
                frame_index,
                confidence=measurement.confidence,
                status="detected",
                component_count=measurement.component_count,
                motion_area=measurement.motion_area,
                reason="foreground_motion",
            )

        self._missing_frames += 1
        if self._smoothed is not None and self._missing_frames <= self.hold_frames:
            confidence = self._last_detected_confidence * self.hold_confidence_decay**self._missing_frames
            return self._point(
                frame_index,
                confidence=confidence,
                status="held",
                component_count=0,
                motion_area=0,
                reason="bounded_hold",
            )

        self._smoothed = None
        self._last_detected_confidence = 0.0
        return ActionTrackPoint(
            frame_index=frame_index,
            x=None,
            y=None,
            range_width=None,
            range_height=None,
            confidence=0.0,
            status="unknown",
            component_count=0,
            motion_area=0,
            reason="no_signal",
        )

    def _point(
        self,
        frame_index: int,
        *,
        confidence: float,
        status: str,
        component_count: int,
        motion_area: int,
        reason: str,
    ) -> ActionTrackPoint:
        assert self._smoothed is not None
        return ActionTrackPoint(
            frame_index=frame_index,
            x=self._smoothed[0],
            y=self._smoothed[1],
            range_width=self._smoothed[2],
            range_height=self._smoothed[3],
            confidence=confidence,
            status=status,
            component_count=component_count,
            motion_area=motion_area,
            reason=reason,
        )


class ActionSignalProcessor:
    def __init__(
        self,
        *,
        calibration: ActionCalibration,
        source_width: int,
        source_height: int,
        settings: ActionSignalSettings,
    ) -> None:
        if source_width <= 0 or source_height <= 0:
            raise ValueError("source dimensions must be positive")
        self.source_width = source_width
        self.source_height = source_height
        self.settings = settings
        self.process_height = max(1, round(source_height * settings.process_width / source_width))
        self.field_mask = build_action_mask(calibration, width=settings.process_width, height=self.process_height)
        self._subtractor = cv2.createBackgroundSubtractorMOG2(
            history=settings.background_history,
            varThreshold=settings.variance_threshold,
            detectShadows=True,
        )
        self._kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self._nonempty_frames = 0
        self._tracker = ActionSignalTracker(
            smoothing=settings.smoothing,
            hold_frames=settings.hold_frames,
            hold_confidence_decay=settings.hold_confidence_decay,
        )

    def process_frame(
        self,
        frame: np.ndarray | None,
        *,
        frame_index: int,
    ) -> tuple[ActionTrackPoint, dict[str, Any]]:
        measurement: ActionMeasurement | None = None
        reason = "empty_frame"
        if frame is not None and frame.size > 0:
            small = cv2.resize(
                frame,
                (self.settings.process_width, self.process_height),
                interpolation=cv2.INTER_AREA,
            )
            foreground = self._subtractor.apply(small)
            self._nonempty_frames += 1
            if self._nonempty_frames <= self.settings.warmup_frames:
                reason = "background_warmup"
            else:
                _, binary = cv2.threshold(foreground, 200, 255, cv2.THRESH_BINARY)
                binary = cv2.bitwise_and(binary, self.field_mask)
                binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, self._kernel)
                binary = cv2.dilate(binary, self._kernel, iterations=1)
                binary = cv2.bitwise_and(binary, self.field_mask)
                measurement = measure_action(
                    binary,
                    source_width=self.source_width,
                    source_height=self.source_height,
                    min_component_area=self.settings.min_component_area,
                    max_component_area=self.settings.max_component_area,
                )
                reason = "foreground_motion" if measurement is not None else "no_foreground_motion"

        result = self._tracker.update(frame_index, measurement)
        diagnostic = {
            "schema_version": ACTION_SIGNAL_SCHEMA_VERSION,
            "frame_index": frame_index,
            "status": result.status,
            "reason": reason,
            "track_reason": result.reason,
            "x": result.x,
            "y": result.y,
            "range_width": result.range_width,
            "range_height": result.range_height,
            "confidence": round(result.confidence, 6),
            "component_count": 0 if measurement is None else measurement.component_count,
            "motion_area": 0 if measurement is None else measurement.motion_area,
            "motion_fraction": 0.0 if measurement is None else round(measurement.motion_fraction, 8),
        }
        return result, diagnostic


def load_action_calibration(path: Path) -> ActionCalibration:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to load action calibration {path}: {exc}") from exc
    return ActionCalibration.from_dict(payload)


def validate_calibration_for_video(
    calibration: ActionCalibration,
    *,
    source_width: int,
    source_height: int,
    total_source_frames: int | None,
) -> None:
    """Accept exact or proportionally scaled video within a 1e-4 aspect-ratio tolerance."""
    if source_width <= 0 or source_height <= 0:
        raise ValueError("video source dimensions must be positive")
    calibration_ratio = calibration.source_width / calibration.source_height
    video_ratio = source_width / source_height
    if (source_width, source_height) != (calibration.source_width, calibration.source_height) and not math.isclose(
        calibration_ratio,
        video_ratio,
        rel_tol=ACTION_CALIBRATION_ASPECT_RATIO_TOLERANCE,
        abs_tol=ACTION_CALIBRATION_ASPECT_RATIO_TOLERANCE,
    ):
        raise ValueError(
            "calibration source_resolution has an incompatible aspect ratio: "
            f"{calibration.source_width}x{calibration.source_height} vs {source_width}x{source_height}"
        )
    if total_source_frames is not None:
        outside = [frame for frame in calibration.confirmed_sample_frames if frame >= total_source_frames]
        if outside:
            raise ValueError(
                f"confirmed_sample_frames outside video frame range [0, {total_source_frames - 1}]: {outside}"
            )


def build_action_mask(calibration: ActionCalibration, *, width: int, height: int) -> np.ndarray:
    if width <= 0 or height <= 0:
        raise ValueError("mask dimensions must be positive")
    mask = np.zeros((height, width), dtype=np.uint8)
    cv2.fillPoly(mask, [_scaled_polygon(calibration.field_polygon, calibration, width, height)], 255)
    for polygon in calibration.exclusion_polygons:
        cv2.fillPoly(mask, [_scaled_polygon(polygon, calibration, width, height)], 0)
    return mask


def measure_action(
    binary: np.ndarray,
    *,
    source_width: int,
    source_height: int,
    min_component_area: int,
    max_component_area: int,
) -> ActionMeasurement | None:
    if binary.size == 0 or binary.ndim != 2:
        return None
    component_count, _, stats, centroids = cv2.connectedComponentsWithStats(
        (binary > 0).astype(np.uint8),
        connectivity=8,
    )
    scale_x = source_width / binary.shape[1]
    scale_y = source_height / binary.shape[0]
    weighted_x = 0.0
    weighted_y = 0.0
    total_weight = 0.0
    accepted_area = 0
    accepted_count = 0
    min_x = float(source_width)
    min_y = float(source_height)
    max_x = 0.0
    max_y = 0.0
    for component_index in range(1, component_count):
        area = int(stats[component_index, cv2.CC_STAT_AREA])
        if area < min_component_area or area > max_component_area:
            continue
        x = int(stats[component_index, cv2.CC_STAT_LEFT])
        y = int(stats[component_index, cv2.CC_STAT_TOP])
        width = int(stats[component_index, cv2.CC_STAT_WIDTH])
        height = int(stats[component_index, cv2.CC_STAT_HEIGHT])
        center_x, center_y = centroids[component_index]
        weight = math.sqrt(area)
        weighted_x += float(center_x) * scale_x * weight
        weighted_y += float(center_y) * scale_y * weight
        total_weight += weight
        accepted_area += area
        accepted_count += 1
        min_x = min(min_x, x * scale_x)
        min_y = min(min_y, y * scale_y)
        max_x = max(max_x, min(float(source_width), (x + width) * scale_x))
        max_y = max(max_y, min(float(source_height), (y + height) * scale_y))
    if total_weight <= 0.0:
        return None

    motion_fraction = accepted_area / float(binary.shape[0] * binary.shape[1])
    confidence = min(0.95, 0.35 + min(0.55, 3.0 * math.sqrt(motion_fraction)) + min(0.05, accepted_count / 100.0))
    return ActionMeasurement(
        center_x=min(float(source_width), max(0.0, weighted_x / total_weight)),
        center_y=min(float(source_height), max(0.0, weighted_y / total_weight)),
        range_width=max(0.0, max_x - min_x),
        range_height=max(0.0, max_y - min_y),
        confidence=confidence,
        component_count=accepted_count,
        motion_area=accepted_area,
        motion_fraction=motion_fraction,
    )


def generate_action_track(
    *,
    input_video: Path,
    calibration: ActionCalibration,
    output_dir: Path,
    settings: ActionSignalSettings | None = None,
    start_frame: int = 0,
    max_frames: int | None = None,
    calibration_source: Path | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    progress_interval_frames: int = 250,
) -> dict[str, Any]:
    settings = settings or ActionSignalSettings()
    input_video = Path(input_video).resolve()
    output_dir = Path(output_dir).resolve()
    start_frame = _nonnegative_int(start_frame, "start_frame")
    max_frames = None if max_frames is None else _nonnegative_int(max_frames, "max_frames")
    progress_interval_frames = _positive_int(progress_interval_frames, "progress_interval_frames")
    temporary_paths: list[Path] = []
    capture = cv2.VideoCapture(str(input_video))
    try:
        if not capture.isOpened():
            raise RuntimeError(f"Unable to open input video: {input_video}")
        source_width = _capture_dimension(capture, cv2.CAP_PROP_FRAME_WIDTH, "width")
        source_height = _capture_dimension(capture, cv2.CAP_PROP_FRAME_HEIGHT, "height")
        fps = _capture_fps(capture)
        total_source_frames = _capture_frame_count(capture)
        validate_calibration_for_video(
            calibration,
            source_width=source_width,
            source_height=source_height,
            total_source_frames=total_source_frames,
        )
        if total_source_frames is not None and start_frame > total_source_frames:
            raise ValueError(f"start_frame {start_frame} exceeds source frame count {total_source_frames}")
        expected_frame_count = _expected_frames(total_source_frames, start_frame, max_frames)
        seek_mode = "not_required"
        if expected_frame_count != 0:
            capture, seek_mode = _position_capture(capture, input_video, start_frame)

        output_dir.mkdir(parents=True, exist_ok=True)
        track_path = output_dir / ACTION_TRACK_NAME
        diagnostics_path = output_dir / ACTION_SIGNAL_DIAGNOSTICS_NAME
        report_path = output_dir / ACTION_SIGNAL_REPORT_NAME
        track_temp = _temporary_path(track_path)
        diagnostics_temp = _temporary_path(diagnostics_path)
        temporary_paths.extend((track_temp, diagnostics_temp))
        processor = ActionSignalProcessor(
            calibration=calibration,
            source_width=source_width,
            source_height=source_height,
            settings=settings,
        )
        status_counts = {"detected": 0, "held": 0, "unknown": 0}
        frame_count = 0
        max_components = 0
        read_failed = False
        _emit_progress(
            progress_callback,
            {
                "event": "started",
                "status": "running",
                "start_frame": start_frame,
                "expected_frame_count": expected_frame_count,
                "seek_mode": seek_mode,
            },
        )
        with (
            track_temp.open("w", encoding="utf-8-sig", newline="") as track_file,
            diagnostics_temp.open("w", encoding="utf-8") as diagnostics_file,
        ):
            writer = csv.writer(track_file)
            writer.writerow(
                [
                    "Frame",
                    "X",
                    "Y",
                    "RangeWidth",
                    "RangeHeight",
                    "Confidence",
                    "Status",
                    "ComponentCount",
                    "MotionArea",
                    "Reason",
                ]
            )
            while expected_frame_count is None or frame_count < expected_frame_count:
                ok, frame = capture.read()
                if not ok:
                    read_failed = True
                    break
                frame_index = start_frame + frame_count
                result, diagnostic = processor.process_frame(frame, frame_index=frame_index)
                writer.writerow(result.csv_row())
                diagnostics_file.write(
                    json.dumps(
                        diagnostic,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    + "\n"
                )
                status_counts[result.status] += 1
                max_components = max(max_components, int(diagnostic["component_count"]))
                frame_count += 1
                if frame_count % progress_interval_frames == 0:
                    _emit_progress(
                        progress_callback,
                        _progress_payload(frame_count, frame_index, expected_frame_count),
                    )

        status, termination_reason = _termination(
            total_source_frames=total_source_frames,
            start_frame=start_frame,
            max_frames=max_frames,
            expected_frame_count=expected_frame_count,
            frame_count=frame_count,
            read_failed=read_failed,
        )
        calibration_payload = calibration.to_dict()
        calibration_json = json.dumps(
            calibration_payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        calibration_digest = hashlib.sha256(calibration_json.encode("utf-8")).hexdigest().upper()
        report = {
            "schema_version": ACTION_SIGNAL_SCHEMA_VERSION,
            "artifact_type": "action_signal_report",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "termination_reason": termination_reason,
            "input_video": str(input_video),
            "output_dir": str(output_dir),
            "source_resolution": [source_width, source_height],
            "source_frame_count": total_source_frames,
            "process_resolution": [settings.process_width, processor.process_height],
            "fps": fps,
            "start_frame": start_frame,
            "expected_frame_count": expected_frame_count,
            "frame_count": frame_count,
            "seek_mode": seek_mode,
            "settings": asdict(settings),
            "calibration": {
                "source": None if calibration_source is None else str(Path(calibration_source).resolve()),
                "sha256": calibration_digest,
                "aspect_ratio_tolerance": ACTION_CALIBRATION_ASPECT_RATIO_TOLERANCE,
                "contract": calibration_payload,
            },
            "status_counts": status_counts,
            "max_components": max_components,
            "artifacts": {
                "track": ACTION_TRACK_NAME,
                "diagnostics": ACTION_SIGNAL_DIAGNOSTICS_NAME,
            },
            "provenance": {
                "absorbed_source": "python_backend/scripts/action_director.py",
                "source_sha256": ACTION_DIRECTOR_SOURCE_SHA256,
                "source_status": "replaced_by_tested_module",
            },
        }
        report_temp = _temporary_path(report_path)
        temporary_paths.append(report_temp)
        report_temp.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        _publish_artifact_set(
            [
                (track_temp, track_path),
                (diagnostics_temp, diagnostics_path),
                (report_temp, report_path),
            ]
        )
        _emit_progress(
            progress_callback,
            {
                "event": "completed",
                "status": status,
                "termination_reason": termination_reason,
                "frame_count": frame_count,
                "expected_frame_count": expected_frame_count,
                "report": str(report_path),
            },
        )
        return report
    except Exception as exc:
        _emit_progress(
            progress_callback,
            {
                "event": "failed",
                "status": "failed",
                "termination_reason": _failure_reason(exc),
                "error": str(exc),
            },
        )
        raise
    finally:
        capture.release()
        for temporary_path in temporary_paths:
            temporary_path.unlink(missing_ok=True)


def _position_capture(capture: Any, input_video: Path, start_frame: int) -> tuple[Any, str]:
    if start_frame == 0:
        try:
            if math.isclose(_capture_position(capture), 0.0, abs_tol=0.25):
                return capture, "from_start"
        except RuntimeError:
            pass
    if capture.set(cv2.CAP_PROP_POS_FRAMES, start_frame):
        try:
            actual_position = _capture_position(capture)
            if math.isclose(actual_position, float(start_frame), abs_tol=0.25):
                return capture, "direct"
        except RuntimeError:
            pass

    capture.release()
    fallback = cv2.VideoCapture(str(input_video))
    try:
        if not fallback.isOpened():
            raise RuntimeError("Unable to reopen input video for sequential seek fallback")
        if not math.isclose(_capture_position(fallback), 0.0, abs_tol=0.25):
            raise RuntimeError("Sequential seek fallback did not start at frame 0")
        for skipped in range(start_frame):
            ok, _ = fallback.read()
            if not ok:
                raise RuntimeError(f"Sequential seek fallback ended at frame {skipped}, before {start_frame}")
        actual_position = _capture_position(fallback)
        if not math.isclose(actual_position, float(start_frame), abs_tol=0.25):
            raise RuntimeError(f"Sequential seek fallback reported frame {actual_position}, expected {start_frame}")
    except Exception:
        fallback.release()
        raise
    return fallback, "sequential_fallback"


def _expected_frames(total_source_frames: int | None, start_frame: int, max_frames: int | None) -> int | None:
    if total_source_frames is None:
        return max_frames
    available = max(0, total_source_frames - start_frame)
    return available if max_frames is None else min(available, max_frames)


def _termination(
    *,
    total_source_frames: int | None,
    start_frame: int,
    max_frames: int | None,
    expected_frame_count: int | None,
    frame_count: int,
    read_failed: bool,
) -> tuple[str, str]:
    if total_source_frames is None and frame_count == 0:
        return "failed", "no_decodable_frames"
    if expected_frame_count is not None and frame_count < expected_frame_count:
        return "truncated", "premature_read_failure"
    if total_source_frames is None and read_failed and max_frames is not None:
        return "truncated", "premature_read_failure"
    if max_frames is not None:
        available = None if total_source_frames is None else max(0, total_source_frames - start_frame)
        if available is None or max_frames <= available:
            return "bounded_complete", "max_frames_reached"
    if total_source_frames is None and read_failed:
        return "complete", "end_of_stream"
    return "complete", "source_frame_count_reached"


def _progress_payload(frame_count: int, frame_index: int, expected_frame_count: int | None) -> dict[str, Any]:
    percent = None
    if expected_frame_count:
        percent = round(100.0 * frame_count / expected_frame_count, 3)
    return {
        "event": "progress",
        "status": "running",
        "frame_count": frame_count,
        "source_frame": frame_index,
        "expected_frame_count": expected_frame_count,
        "percent": percent,
    }


def _emit_progress(callback: Callable[[dict[str, Any]], None] | None, payload: dict[str, Any]) -> None:
    if callback is None:
        return
    try:
        callback(payload)
    except Exception:
        return


def _failure_reason(exc: Exception) -> str:
    message = str(exc).lower()
    if "seek" in message or "frame 0" in message:
        return "seek_failed"
    if isinstance(exc, ValueError) or "fps" in message or "dimension" in message:
        return "validation_failed"
    return "processing_failed"


def _temporary_path(final_path: Path) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{final_path.name}.",
        suffix=".tmp",
        dir=final_path.parent,
    )
    os.close(descriptor)
    return Path(raw_path)


def _publish_artifact_set(staged: list[tuple[Path, Path]]) -> None:
    backups: dict[Path, Path] = {}
    published: list[Path] = []
    try:
        for _, final_path in staged:
            if final_path.exists():
                backup = final_path.with_name(f".{final_path.name}.{uuid.uuid4().hex}.bak")
                os.replace(final_path, backup)
                backups[final_path] = backup
        for temporary_path, final_path in staged:
            os.replace(temporary_path, final_path)
            published.append(final_path)
    except Exception:
        for final_path in published:
            final_path.unlink(missing_ok=True)
        for final_path, backup in reversed(tuple(backups.items())):
            if backup.exists():
                os.replace(backup, final_path)
        raise
    else:
        for backup in backups.values():
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                pass


def _capture_dimension(capture: Any, prop: int, name: str) -> int:
    value = _finite_capture_value(capture, prop, f"source {name}")
    rounded = round(value)
    if value <= 0.0 or not math.isclose(value, rounded, abs_tol=1e-3):
        raise RuntimeError(f"Invalid source {name}: {value!r}")
    return int(rounded)


def _capture_fps(capture: Any) -> float:
    fps = _finite_capture_value(capture, cv2.CAP_PROP_FPS, "source FPS")
    if fps <= 0.0:
        raise RuntimeError(f"Invalid source FPS: {fps!r}")
    return fps


def _capture_frame_count(capture: Any) -> int | None:
    value = _finite_capture_value(capture, cv2.CAP_PROP_FRAME_COUNT, "source frame count")
    rounded = round(value)
    if value < 0.0 or not math.isclose(value, rounded, abs_tol=1e-3):
        raise RuntimeError(f"Invalid source frame count: {value!r}")
    return None if rounded == 0 else int(rounded)


def _capture_position(capture: Any) -> float:
    value = _finite_capture_value(capture, cv2.CAP_PROP_POS_FRAMES, "source frame position")
    if value < 0.0:
        raise RuntimeError(f"Invalid source frame position: {value!r}")
    return value


def _finite_capture_value(capture: Any, prop: int, name: str) -> float:
    try:
        value = float(capture.get(prop))
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError(f"Invalid {name}") from exc
    if not math.isfinite(value):
        raise RuntimeError(f"Invalid {name}: value must be finite")
    return value


def _scaled_polygon(
    polygon: Polygon,
    calibration: ActionCalibration,
    width: int,
    height: int,
) -> np.ndarray:
    points = [
        [
            min(width - 1, max(0, round(x * width / calibration.source_width))),
            min(height - 1, max(0, round(y * height / calibration.source_height))),
        ]
        for x, y in polygon
    ]
    return np.asarray(points, dtype=np.int32)


def _parse_polygon(
    value: Any,
    *,
    name: str,
    source_width: int,
    source_height: int,
) -> Polygon:
    if not isinstance(value, list) or len(value) < 3:
        raise ValueError(f"{name} must contain at least three points")
    points: list[Point] = []
    for index, raw_point in enumerate(value):
        if not isinstance(raw_point, list) or len(raw_point) != 2:
            raise ValueError(f"{name}[{index}] must be [x, y]")
        x = _finite_float(raw_point[0], f"{name}[{index}][0]")
        y = _finite_float(raw_point[1], f"{name}[{index}][1]")
        if not 0.0 <= x <= source_width or not 0.0 <= y <= source_height:
            raise ValueError(f"{name}[{index}] must lie inside source_resolution")
        points.append((x, y))
    if abs(cv2.contourArea(np.asarray(points, dtype=np.float32))) <= 1e-6:
        raise ValueError(f"{name} must enclose a non-zero area")
    return tuple(points)


def _positive_int(value: Any, name: str) -> int:
    parsed = _nonnegative_int(value, name)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    try:
        parsed_float = float(value)
        parsed = int(parsed_float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if not math.isfinite(parsed_float) or parsed_float != parsed or parsed < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return parsed


def _finite_float(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be a finite number")
    return parsed


def _config_int(value: Any, name: str, *, positive: bool) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be a {qualifier} integer")
    if (positive and value <= 0) or (not positive and value < 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be a {qualifier} integer")


def _config_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be a finite number")
    return parsed


def _lerp(current: float, target: float, alpha: float) -> float:
    return current + (target - current) * alpha


def _csv_number(value: float | None, digits: int) -> str:
    return "" if value is None else f"{value:.{digits}f}"
