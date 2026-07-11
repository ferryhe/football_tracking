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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, BinaryIO, Mapping
from uuid import uuid4

import cv2
import numpy as np

from football_tracking.camera_motion_audit import write_streaming_camera_motion_audit_report
from football_tracking.global_ball_trajectory import (
    GlobalBallTrajectoryError,
    _acquire_source_lease,
    _probe_video_metadata,
    _release_source_lease,
    _verify_source_lease,
)

CAMERA_PATH_NAME = "camera_path.v2.csv"
MOTION_EVIDENCE_NAME = "camera_motion_evidence.v1.jsonl"
DECISIONS_NAME = "hybrid_broadcast_camera_decisions.v1.jsonl"
AUDIT_NAME = "camera_motion_audit.json"
REPORT_NAME = "hybrid_broadcast_camera_report.v1.json"
REPORT_SCHEMA_VERSION = "1.0"
REPORT_ARTIFACT_TYPE = "hybrid_broadcast_camera_report"
ALGORITHM_VERSION = "hybrid-broadcast-camera-v1"

_TRACK_STATUSES = frozenset({"detected", "interpolated", "unknown", "out_of_view"})
_TRACK_COLUMNS = frozenset(
    {
        "Frame",
        "X",
        "Y",
        "Confidence",
        "Status",
        "SelectedCandidateId",
        "Source",
        "Reason",
    }
)
_COPY_CHUNK_BYTES = 1024 * 1024
_MAX_REPORT_BYTES = 4 * 1024 * 1024


class HybridBroadcastCameraError(RuntimeError):
    """Raised when evidence cannot safely produce a hybrid camera path."""


@dataclass(frozen=True, slots=True)
class HybridCameraConfig:
    target_width: int = 1920
    target_height: int = 1080
    analysis_max_dimension: int = 320
    max_features: int = 240
    min_features: int = 12
    min_inliers: int = 8
    ransac_reprojection_threshold: float = 2.5
    phase_correlation_threshold: float = 0.15
    phase_overlap_correlation_threshold: float = 0.45
    minimum_texture_standard_deviation: float = 6.0
    motion_confidence_threshold: float = 0.35
    cut_score_threshold: float = 0.30
    hard_cut_score_threshold: float = 0.72
    cut_inlier_ratio_threshold: float = 0.18
    cut_transform_confidence_threshold: float = 0.25
    minimum_ball_confidence: float = 0.15
    interpolated_weight: float = 0.48
    minimum_crop_height_ratio: float = 0.48
    maximum_crop_height_ratio: float = 0.94
    uncertainty_zoom_out_ratio: float = 0.28
    speed_zoom_out_start_ratio: float = 0.006
    speed_zoom_out_end_ratio: float = 0.035
    ball_screen_x_ratio: float = 0.50
    ball_screen_y_ratio: float = 0.58
    pan_smoothing: float = 0.34
    max_pan_step_x_ratio: float = 0.035
    max_pan_step_y_ratio: float = 0.035
    max_pan_acceleration_x_ratio: float = 0.018
    max_pan_acceleration_y_ratio: float = 0.018
    zoom_smoothing: float = 0.12
    max_zoom_step_ratio: float = 0.012
    unknown_hold_frames: int = 24
    fallback_smoothing: float = 0.06
    target_visibility_margin_ratio: float = 0.05


@dataclass(frozen=True, slots=True)
class BallTrackRow:
    frame_index: int
    x: float | None
    y: float | None
    confidence: float | None
    status: str
    selected_candidate_id: str | None
    source: str
    reason: str


@dataclass(frozen=True, slots=True)
class CameraMotionEvidence:
    frame_index: int
    dx: float | None
    dy: float | None
    scale: float | None
    rotation_degrees: float | None
    confidence: float | None
    inlier_ratio: float | None
    tracked_feature_count: int
    cut_score: float
    cut_before: bool
    method: str
    reject_reason: str | None


@dataclass(slots=True)
class _CameraState:
    center_x: float
    center_y: float
    crop_height: float
    pan_velocity_x: float = 0.0
    pan_velocity_y: float = 0.0
    zoom_velocity: float = 0.0
    shot_id: int = 0
    unknown_streak: int = 0
    previous_ball_x: float | None = None
    previous_ball_y: float | None = None
    previous_ball_frame: int | None = None


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    label: str
    path: Path
    copy_path: Path
    sha256: str
    size: int
    stat_token: tuple[int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class _FramePlan:
    path: dict[str, Any]
    decision: dict[str, Any]


def solve_hybrid_broadcast_camera(
    source_video_path: Path,
    ball_track_path: Path,
    trajectory_report_path: Path,
    output_dir: Path,
    *,
    config: HybridCameraConfig | None = None,
) -> dict[str, Any]:
    """Build one immutable, evidence-bound hybrid broadcast camera generation."""

    source_video_path = Path(source_video_path).resolve()
    ball_track_path = Path(ball_track_path).resolve()
    trajectory_report_path = Path(trajectory_report_path).resolve()
    output_dir = Path(output_dir).resolve()
    validated_config = _validated_config(config or HybridCameraConfig())
    _validate_output_topology(output_dir, [source_video_path, ball_track_path, trajectory_report_path])
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    lock_handle: BinaryIO | None = None
    source_lease: Any | None = None
    temp_root: Path | None = None
    snapshots: list[_FileSnapshot] = []
    published = False
    try:
        lock_handle = _acquire_output_lock(output_dir)
        if output_dir.exists():
            raise HybridBroadcastCameraError(
                "hybrid camera output directory already exists; publish each run to a new immutable generation"
            )
        temp_root = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.hybrid-camera-", dir=output_dir.parent))
        staging_dir = temp_root / "generation"
        staging_dir.mkdir()

        try:
            source_lease = _acquire_source_lease(source_video_path)
        except GlobalBallTrajectoryError as exc:
            raise HybridBroadcastCameraError(str(exc)) from exc
        track_snapshot = _capture_snapshot(
            ball_track_path,
            "global ball track",
            temp_root / "ball_track.snapshot.csv",
        )
        report_snapshot = _capture_snapshot(
            trajectory_report_path,
            "global trajectory report",
            temp_root / "trajectory_report.snapshot.json",
            max_bytes=_MAX_REPORT_BYTES,
        )
        snapshots.extend((track_snapshot, report_snapshot))

        metadata = _probe_video_metadata_safe(source_lease.probe_path)
        trajectory_report = _load_json_object(report_snapshot.copy_path, "global trajectory report")
        _validate_trajectory_bindings(
            trajectory_report,
            source_sha256=source_lease.snapshot.sha256,
            source_size=source_lease.snapshot.size,
            metadata=metadata,
            track_snapshot=track_snapshot,
        )
        summary = _process_video_and_track(
            source_lease.probe_path,
            track_snapshot.copy_path,
            staging_dir,
            metadata,
            validated_config,
        )
        audit = write_streaming_camera_motion_audit_report(
            staging_dir,
            target_width=validated_config.target_width,
            target_height=validated_config.target_height,
            camera_path_name=CAMERA_PATH_NAME,
            report_name=AUDIT_NAME,
            generated_at=None,
        )
        _validate_camera_audit(audit, summary, staging_dir / AUDIT_NAME)
        report = _build_report(
            staging_dir=staging_dir,
            source_lease=source_lease,
            track_snapshot=track_snapshot,
            trajectory_report_snapshot=report_snapshot,
            trajectory_report=trajectory_report,
            metadata=metadata,
            config=validated_config,
            summary=summary,
            audit=audit,
        )
        _fsync_directory(staging_dir)
        for snapshot in snapshots:
            _verify_snapshot_stat(snapshot)
        _publish_generation(staging_dir, output_dir)
        published = True
        try:
            for snapshot in snapshots:
                _verify_snapshot_stat(snapshot)
            _verify_source_lease(source_lease)
            _write_json_commit(output_dir / REPORT_NAME, report)
        except BaseException:
            _discard_published_generation(output_dir)
            published = False
            raise
        return report
    except HybridBroadcastCameraError:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except GlobalBallTrajectoryError as exc:
        raise HybridBroadcastCameraError(str(exc)) from exc
    except Exception as exc:
        raise HybridBroadcastCameraError(str(exc) or exc.__class__.__name__) from exc
    finally:
        if published and not (output_dir / REPORT_NAME).is_file():
            _discard_published_generation(output_dir)
        if source_lease is not None:
            try:
                _release_source_lease(source_lease)
            except BaseException:
                pass
        if lock_handle is not None:
            try:
                _release_output_lock(lock_handle)
            except BaseException:
                pass
        if temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)


def _validated_config(config: HybridCameraConfig) -> HybridCameraConfig:
    integer_positive = {
        "target_width",
        "target_height",
        "analysis_max_dimension",
        "max_features",
        "min_features",
        "min_inliers",
    }
    for name in integer_positive:
        value = getattr(config, name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise HybridBroadcastCameraError(f"{name} must be a positive integer")
    if isinstance(config.unknown_hold_frames, bool) or not isinstance(config.unknown_hold_frames, int):
        raise HybridBroadcastCameraError("unknown_hold_frames must be a non-negative integer")
    if config.unknown_hold_frames < 0:
        raise HybridBroadcastCameraError("unknown_hold_frames must be a non-negative integer")
    if config.min_inliers > config.max_features or config.min_features > config.max_features:
        raise HybridBroadcastCameraError("feature and inlier bounds cannot exceed max_features")

    probabilities = {
        "motion_confidence_threshold",
        "phase_correlation_threshold",
        "phase_overlap_correlation_threshold",
        "cut_score_threshold",
        "hard_cut_score_threshold",
        "cut_inlier_ratio_threshold",
        "cut_transform_confidence_threshold",
        "minimum_ball_confidence",
        "interpolated_weight",
        "minimum_crop_height_ratio",
        "maximum_crop_height_ratio",
        "uncertainty_zoom_out_ratio",
        "ball_screen_x_ratio",
        "ball_screen_y_ratio",
        "pan_smoothing",
        "max_pan_step_x_ratio",
        "max_pan_step_y_ratio",
        "max_pan_acceleration_x_ratio",
        "max_pan_acceleration_y_ratio",
        "zoom_smoothing",
        "max_zoom_step_ratio",
        "fallback_smoothing",
        "target_visibility_margin_ratio",
    }
    for name in probabilities:
        value = getattr(config, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise HybridBroadcastCameraError(f"{name} must be finite")
        if not 0.0 <= float(value) <= 1.0:
            raise HybridBroadcastCameraError(f"{name} must be in [0, 1]")
    positive_floats = {
        "ransac_reprojection_threshold",
        "minimum_texture_standard_deviation",
        "speed_zoom_out_start_ratio",
        "speed_zoom_out_end_ratio",
    }
    for name in positive_floats:
        value = getattr(config, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise HybridBroadcastCameraError(f"{name} must be finite and positive")
        if float(value) <= 0.0:
            raise HybridBroadcastCameraError(f"{name} must be finite and positive")
    if config.minimum_crop_height_ratio > config.maximum_crop_height_ratio:
        raise HybridBroadcastCameraError("minimum_crop_height_ratio cannot exceed maximum_crop_height_ratio")
    if config.cut_score_threshold > config.hard_cut_score_threshold:
        raise HybridBroadcastCameraError("cut_score_threshold cannot exceed hard_cut_score_threshold")
    if config.speed_zoom_out_start_ratio >= config.speed_zoom_out_end_ratio:
        raise HybridBroadcastCameraError("speed zoom-out start must be below its end")
    return config


def _validate_output_topology(output_dir: Path, inputs: list[Path]) -> None:
    for input_path in inputs:
        if input_path == output_dir or output_dir in input_path.parents:
            raise HybridBroadcastCameraError("hybrid camera output directory cannot contain an input artifact")


def _probe_video_metadata_safe(path: Path) -> dict[str, Any]:
    try:
        metadata = _probe_video_metadata(path)
    except GlobalBallTrajectoryError as exc:
        raise HybridBroadcastCameraError(str(exc)) from exc
    return metadata


def _process_video_and_track(
    source_probe_path: Path,
    track_path: Path,
    staging_dir: Path,
    metadata: Mapping[str, Any],
    config: HybridCameraConfig,
) -> dict[str, Any]:
    capture = cv2.VideoCapture(str(source_probe_path))
    if not capture.isOpened():
        capture.release()
        raise HybridBroadcastCameraError("unable to open leased source video for hybrid camera solving")

    expected_width = int(metadata["width"])
    expected_height = int(metadata["height"])
    expected_count = int(metadata["frame_count"])
    state = _initial_state(expected_width, expected_height, config)
    status_counts: Counter[str] = Counter()
    mode_counts: Counter[str] = Counter()
    fallback_counts: Counter[str] = Counter()
    cut_count = 0
    longest_unknown_run = 0
    target_coverage_count = 0
    target_eligible_count = 0
    low_confidence_motion_count = 0
    max_features_observed = 0
    previous_gray: np.ndarray | None = None

    path_file = staging_dir / CAMERA_PATH_NAME
    evidence_file = staging_dir / MOTION_EVIDENCE_NAME
    decisions_file = staging_dir / DECISIONS_NAME
    try:
        with (
            track_path.open("r", encoding="utf-8-sig", newline="") as track_handle,
            path_file.open("x", encoding="utf-8", newline="") as path_handle,
            evidence_file.open("x", encoding="utf-8", newline="\n") as evidence_handle,
            decisions_file.open("x", encoding="utf-8", newline="\n") as decisions_handle,
        ):
            reader = csv.DictReader(track_handle)
            _validate_track_header(reader.fieldnames)
            writer = csv.DictWriter(path_handle, fieldnames=_camera_path_columns(), lineterminator="\n")
            writer.writeheader()

            for expected_frame in range(expected_count):
                raw_row = next(reader, None)
                if raw_row is None:
                    raise HybridBroadcastCameraError(
                        f"global ball track ended before source frame domain at frame {expected_frame}"
                    )
                if None in raw_row:
                    raise HybridBroadcastCameraError("global ball track contains extra row fields")
                ball = _parse_track_row(raw_row, expected_frame, expected_width, expected_height)
                ok, frame = capture.read()
                if not ok or frame is None:
                    raise HybridBroadcastCameraError(f"source video decode ended early at frame {expected_frame}")
                if frame.ndim < 2 or frame.shape[1] != expected_width or frame.shape[0] != expected_height:
                    raise HybridBroadcastCameraError(f"source video frame dimensions changed at frame {expected_frame}")
                current_gray = _analysis_gray(frame, config.analysis_max_dimension)
                motion = _estimate_camera_motion(
                    expected_frame,
                    previous_gray,
                    current_gray,
                    expected_width,
                    expected_height,
                    config,
                )
                previous_gray = current_gray
                plan = _plan_frame(ball, motion, state, expected_width, expected_height, config)
                writer.writerow(_format_camera_path_row(plan.path))
                _write_json_line(evidence_handle, _motion_payload(motion))
                _write_json_line(decisions_handle, plan.decision)

                status_counts[ball.status] += 1
                mode_counts[str(plan.path["EvidenceMode"])] += 1
                fallback_reason = str(plan.path["FallbackReason"] or "")
                if fallback_reason:
                    fallback_counts[fallback_reason] += 1
                if motion.cut_before:
                    cut_count += 1
                if motion.confidence is not None and motion.confidence < config.motion_confidence_threshold:
                    low_confidence_motion_count += 1
                max_features_observed = max(max_features_observed, motion.tracked_feature_count)
                if ball.status in {"unknown", "out_of_view"}:
                    longest_unknown_run = max(longest_unknown_run, state.unknown_streak)
                    if plan.path["TargetX"] is not None or plan.path["TargetY"] is not None:
                        raise HybridBroadcastCameraError("unknown/out_of_view frame received a fabricated ball target")
                if (
                    ball.status in {"detected", "interpolated"}
                    and ball.confidence is not None
                    and ball.confidence >= config.minimum_ball_confidence
                ):
                    target_eligible_count += 1
                    if bool(plan.path["TargetVisible"]):
                        target_coverage_count += 1

            if next(reader, None) is not None:
                raise HybridBroadcastCameraError("global ball track contains frames outside the source frame domain")
            extra_ok, _ = capture.read()
            if extra_ok:
                raise HybridBroadcastCameraError("source decoder produced frames outside bound source metadata")

            for handle in (path_handle, evidence_handle, decisions_handle):
                handle.flush()
                os.fsync(handle.fileno())
    finally:
        capture.release()

    if sum(status_counts.values()) != expected_count:
        raise HybridBroadcastCameraError("camera path row count does not match the source frame domain")
    return {
        "row_count": expected_count,
        "status_counts": dict(sorted(status_counts.items())),
        "evidence_mode_counts": dict(sorted(mode_counts.items())),
        "fallback_counts": dict(sorted(fallback_counts.items())),
        "cut_count": cut_count,
        "shot_count": cut_count + (1 if expected_count else 0),
        "longest_unknown_run": longest_unknown_run,
        "target_eligible_count": target_eligible_count,
        "target_visible_count": target_coverage_count,
        "target_coverage": 1.0 if target_eligible_count == 0 else target_coverage_count / target_eligible_count,
        "low_confidence_motion_frame_count": low_confidence_motion_count,
        "max_features_observed": max_features_observed,
        "no_ball_targets_for_unknown": True,
    }


def _validate_track_header(fieldnames: Sequence[str] | None) -> None:
    if fieldnames is None:
        raise HybridBroadcastCameraError("global ball track is empty")
    if len(fieldnames) != len(set(fieldnames)):
        raise HybridBroadcastCameraError("global ball track contains duplicate columns")
    if missing := _TRACK_COLUMNS.difference(fieldnames):
        raise HybridBroadcastCameraError(f"global ball track missing required columns: {', '.join(sorted(missing))}")


def _parse_track_row(
    raw: Mapping[str, Any],
    expected_frame: int,
    width: int,
    height: int,
) -> BallTrackRow:
    frame_index = _parse_integer_text(raw.get("Frame"), "track Frame")
    if frame_index != expected_frame:
        raise HybridBroadcastCameraError(
            f"global ball track frame domain is not contiguous: expected {expected_frame}, got {frame_index}"
        )
    status = _required_text(raw.get("Status"), "track Status")
    if status not in _TRACK_STATUSES:
        raise HybridBroadcastCameraError(f"unsupported global ball track status: {status}")
    confidence = _optional_probability_text(raw.get("Confidence"), "track Confidence")
    x = _optional_float_text(raw.get("X"), "track X")
    y = _optional_float_text(raw.get("Y"), "track Y")
    selected = str(raw.get("SelectedCandidateId") or "").strip() or None
    source = _required_text(raw.get("Source"), "track Source")
    reason = _required_text(raw.get("Reason"), "track Reason")

    if status in {"unknown", "out_of_view"}:
        if x is not None or y is not None or selected is not None or confidence is not None:
            raise HybridBroadcastCameraError(f"{status} track row must not contain ball coordinates or identity")
    else:
        if confidence is None:
            raise HybridBroadcastCameraError(f"{status} track row requires finite confidence")
        if x is None or y is None:
            raise HybridBroadcastCameraError(f"{status} track row requires finite ball coordinates")
        if not (0.0 <= x < width and 0.0 <= y < height):
            raise HybridBroadcastCameraError("global ball track coordinate lies outside the source frame")
        if status == "detected" and selected is None:
            raise HybridBroadcastCameraError("detected track row requires SelectedCandidateId")
        if status == "interpolated" and selected is not None:
            raise HybridBroadcastCameraError("interpolated track row cannot claim a selected detection candidate")
    return BallTrackRow(frame_index, x, y, confidence, status, selected, source, reason)


def _analysis_gray(frame: np.ndarray, max_dimension: int) -> np.ndarray:
    height, width = frame.shape[:2]
    scale = min(1.0, max_dimension / max(width, height))
    if scale < 1.0:
        frame = cv2.resize(
            frame,
            (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
            interpolation=cv2.INTER_AREA,
        )
    if frame.ndim == 2:
        return frame
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


def _estimate_camera_motion(
    frame_index: int,
    previous_gray: np.ndarray | None,
    current_gray: np.ndarray,
    source_width: int,
    source_height: int,
    config: HybridCameraConfig,
) -> CameraMotionEvidence:
    if previous_gray is None:
        return CameraMotionEvidence(
            frame_index,
            None,
            None,
            None,
            None,
            None,
            None,
            0,
            0.0,
            False,
            "shot_start",
            "no_previous_frame",
        )
    if previous_gray.shape != current_gray.shape:
        raise HybridBroadcastCameraError("analysis frame dimensions changed during source decode")

    cut_score, previous_texture, current_texture = _structural_cut_score(previous_gray, current_gray)
    corners = cv2.goodFeaturesToTrack(
        previous_gray,
        maxCorners=config.max_features,
        qualityLevel=0.01,
        minDistance=5.0,
        blockSize=5,
    )
    if corners is None or len(corners) < config.min_features:
        phase_motion = _phase_translation_motion(
            frame_index,
            previous_gray,
            current_gray,
            source_width,
            source_height,
            cut_score,
            config,
            0 if corners is None else len(corners),
        )
        if phase_motion is not None:
            return phase_motion
        hard_cut = (
            previous_texture >= config.minimum_texture_standard_deviation
            and current_texture >= config.minimum_texture_standard_deviation
            and cut_score >= config.hard_cut_score_threshold
        )
        reject_reason = "too_few_background_features"
        if min(previous_texture, current_texture) < config.minimum_texture_standard_deviation:
            reject_reason = "low_texture_or_photometric_change"
        return CameraMotionEvidence(
            frame_index,
            None,
            None,
            None,
            None,
            None,
            None,
            0 if corners is None else len(corners),
            cut_score,
            hard_cut,
            "appearance_cut" if hard_cut else "insufficient_features",
            None if hard_cut else reject_reason,
        )

    ordered = np.asarray(corners, dtype=np.float32).reshape(-1, 2)
    order = np.lexsort((ordered[:, 1], ordered[:, 0]))
    previous_points = ordered[order].reshape(-1, 1, 2)
    current_points, statuses, _errors = cv2.calcOpticalFlowPyrLK(
        previous_gray,
        current_gray,
        previous_points,
        np.empty_like(previous_points),
        winSize=(21, 21),
        maxLevel=3,
        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
    )
    if current_points is None or statuses is None:
        return _phase_translation_motion(
            frame_index,
            previous_gray,
            current_gray,
            source_width,
            source_height,
            cut_score,
            config,
            len(previous_points),
        ) or _rejected_motion(frame_index, cut_score, "optical_flow_failed")
    keep = statuses.reshape(-1).astype(bool)
    previous_kept = previous_points.reshape(-1, 2)[keep]
    current_kept = current_points.reshape(-1, 2)[keep]
    finite = np.isfinite(previous_kept).all(axis=1) & np.isfinite(current_kept).all(axis=1)
    previous_kept = previous_kept[finite]
    current_kept = current_kept[finite]
    tracked = len(previous_kept)
    if tracked < config.min_features:
        return _phase_translation_motion(
            frame_index,
            previous_gray,
            current_gray,
            source_width,
            source_height,
            cut_score,
            config,
            tracked,
        ) or _rejected_motion(frame_index, cut_score, "too_few_tracked_features", tracked)

    cv2.setRNGSeed(0)
    matrix, inlier_mask = cv2.estimateAffinePartial2D(
        previous_kept,
        current_kept,
        method=cv2.RANSAC,
        ransacReprojThreshold=config.ransac_reprojection_threshold,
        maxIters=2000,
        confidence=0.99,
        refineIters=10,
    )
    if matrix is None or inlier_mask is None:
        return _phase_translation_motion(
            frame_index,
            previous_gray,
            current_gray,
            source_width,
            source_height,
            cut_score,
            config,
            tracked,
        ) or _rejected_motion(frame_index, cut_score, "affine_estimation_failed", tracked)
    inliers = inlier_mask.reshape(-1).astype(bool)
    inlier_count = int(np.count_nonzero(inliers))
    inlier_ratio = inlier_count / tracked
    if inlier_count < config.min_inliers:
        phase_motion = _phase_translation_motion(
            frame_index,
            previous_gray,
            current_gray,
            source_width,
            source_height,
            cut_score,
            config,
            tracked,
        )
        if phase_motion is not None:
            return phase_motion
        cut = (
            previous_texture >= config.minimum_texture_standard_deviation
            and current_texture >= config.minimum_texture_standard_deviation
            and cut_score >= config.cut_score_threshold
            and inlier_ratio < config.cut_inlier_ratio_threshold
        )
        return CameraMotionEvidence(
            frame_index,
            None,
            None,
            None,
            None,
            None,
            inlier_ratio,
            tracked,
            cut_score,
            cut,
            "appearance_cut" if cut else "rejected_affine",
            None if cut else "too_few_affine_inliers",
        )

    a = float(matrix[0, 0])
    b = float(matrix[1, 0])
    transform_scale = math.hypot(a, b)
    rotation = math.degrees(math.atan2(b, a))
    if not (math.isfinite(transform_scale) and 0.75 <= transform_scale <= 1.25 and abs(rotation) <= 15.0):
        return _rejected_motion(frame_index, cut_score, "implausible_affine", tracked, inlier_ratio)
    predicted = cv2.transform(previous_kept[inliers].reshape(-1, 1, 2), matrix).reshape(-1, 2)
    residual = np.linalg.norm(predicted - current_kept[inliers], axis=1)
    median_residual = float(np.median(residual)) if residual.size else config.ransac_reprojection_threshold
    residual_quality = math.exp(-median_residual / max(1e-6, config.ransac_reprojection_threshold))
    feature_quality = min(1.0, tracked / max(1, config.min_features * 2))
    confidence = max(0.0, min(1.0, inlier_ratio * residual_quality * feature_quality))
    if confidence < config.cut_transform_confidence_threshold:
        phase_motion = _phase_translation_motion(
            frame_index,
            previous_gray,
            current_gray,
            source_width,
            source_height,
            cut_score,
            config,
            tracked,
        )
        if phase_motion is not None:
            return phase_motion
    cut = (
        previous_texture >= config.minimum_texture_standard_deviation
        and current_texture >= config.minimum_texture_standard_deviation
        and cut_score >= config.cut_score_threshold
        and (
            inlier_ratio < config.cut_inlier_ratio_threshold
            or (cut_score >= config.hard_cut_score_threshold and confidence < config.cut_transform_confidence_threshold)
        )
    )
    if cut:
        return CameraMotionEvidence(
            frame_index,
            None,
            None,
            None,
            None,
            confidence,
            inlier_ratio,
            tracked,
            cut_score,
            True,
            "appearance_cut",
            None,
        )

    analysis_height, analysis_width = current_gray.shape[:2]
    dx = float(matrix[0, 2]) * source_width / analysis_width
    dy = float(matrix[1, 2]) * source_height / analysis_height
    return CameraMotionEvidence(
        frame_index,
        dx,
        dy,
        transform_scale,
        rotation,
        confidence,
        inlier_ratio,
        tracked,
        cut_score,
        False,
        "partial_affine",
        None if confidence >= config.motion_confidence_threshold else "motion_confidence_below_threshold",
    )


def _structural_cut_score(
    previous_gray: np.ndarray,
    current_gray: np.ndarray,
) -> tuple[float, float, float]:
    previous = previous_gray.astype(np.float32)
    current = current_gray.astype(np.float32)
    previous_mean = float(np.mean(previous))
    current_mean = float(np.mean(current))
    previous_std = float(np.std(previous))
    current_std = float(np.std(current))
    correlation_change = 0.0
    if previous_std > 1e-6 and current_std > 1e-6:
        correlation = float(
            np.mean((previous - previous_mean) * (current - current_mean)) / (previous_std * current_std)
        )
        correlation_change = max(0.0, min(1.0, 1.0 - correlation))
    previous_edges = cv2.Sobel(previous, cv2.CV_32F, 1, 1, ksize=3)
    current_edges = cv2.Sobel(current, cv2.CV_32F, 1, 1, ksize=3)
    edge_change = min(1.0, float(np.mean(np.abs(previous_edges - current_edges))) / 255.0)
    return max(correlation_change, edge_change), previous_std, current_std


def _phase_translation_motion(
    frame_index: int,
    previous_gray: np.ndarray,
    current_gray: np.ndarray,
    source_width: int,
    source_height: int,
    cut_score: float,
    config: HybridCameraConfig,
    tracked_features: int,
) -> CameraMotionEvidence | None:
    try:
        height, width = previous_gray.shape[:2]
        window = cv2.createHanningWindow((width, height), cv2.CV_32F)
        shift, response = cv2.phaseCorrelate(
            previous_gray.astype(np.float32),
            current_gray.astype(np.float32),
            window,
        )
    except (cv2.error, ValueError):
        return None
    response = float(response)
    base_shift_x = float(shift[0])
    base_shift_y = float(shift[1])
    if not all(math.isfinite(value) for value in (response, base_shift_x, base_shift_y)):
        return None
    if response < config.phase_correlation_threshold:
        return None
    candidates: list[tuple[float, float, float]] = []
    for shift_x in (base_shift_x - width, base_shift_x, base_shift_x + width):
        for shift_y in (base_shift_y - height, base_shift_y, base_shift_y + height):
            if abs(shift_x) > width * 0.75 or abs(shift_y) > height * 0.75:
                continue
            correlation = _translation_overlap_correlation(previous_gray, current_gray, shift_x, shift_y)
            if correlation is not None:
                candidates.append((correlation, shift_x, shift_y))
    if not candidates:
        return None
    overlap_correlation, shift_x, shift_y = max(
        candidates,
        key=lambda item: (round(item[0], 9), -abs(item[1]) - abs(item[2]), -item[1], -item[2]),
    )
    if overlap_correlation < config.phase_overlap_correlation_threshold:
        return None
    confidence = max(0.0, min(1.0, response * max(0.0, overlap_correlation)))
    if abs(shift_x) > width * 0.45 or abs(shift_y) > height * 0.45:
        return CameraMotionEvidence(
            frame_index,
            None,
            None,
            None,
            None,
            confidence,
            None,
            tracked_features,
            cut_score,
            False,
            "coherent_motion_out_of_bounds",
            "coherent_translation_exceeds_motion_bound",
        )
    return CameraMotionEvidence(
        frame_index,
        shift_x * source_width / width,
        shift_y * source_height / height,
        1.0,
        0.0,
        confidence,
        None,
        tracked_features,
        cut_score,
        False,
        "phase_translation",
        None if confidence >= config.motion_confidence_threshold else "motion_confidence_below_threshold",
    )


def _translation_overlap_correlation(
    previous_gray: np.ndarray,
    current_gray: np.ndarray,
    shift_x: float,
    shift_y: float,
) -> float | None:
    height, width = previous_gray.shape[:2]
    dx = int(round(shift_x))
    dy = int(round(shift_y))
    overlap_width = width - abs(dx)
    overlap_height = height - abs(dy)
    if overlap_width < max(8, int(width * 0.2)) or overlap_height < max(8, int(height * 0.2)):
        return None
    previous_x = max(0, -dx)
    current_x = max(0, dx)
    previous_y = max(0, -dy)
    current_y = max(0, dy)
    previous = previous_gray[
        previous_y : previous_y + overlap_height,
        previous_x : previous_x + overlap_width,
    ].astype(np.float32)
    current = current_gray[
        current_y : current_y + overlap_height,
        current_x : current_x + overlap_width,
    ].astype(np.float32)
    previous_std = float(np.std(previous))
    current_std = float(np.std(current))
    if previous_std < 1e-6 or current_std < 1e-6:
        return None
    correlation = float(
        np.mean((previous - float(np.mean(previous))) * (current - float(np.mean(current))))
        / (previous_std * current_std)
    )
    return max(-1.0, min(1.0, correlation))


def _rejected_motion(
    frame_index: int,
    cut_score: float,
    reason: str,
    tracked: int = 0,
    inlier_ratio: float | None = None,
) -> CameraMotionEvidence:
    return CameraMotionEvidence(
        frame_index,
        None,
        None,
        None,
        None,
        None,
        inlier_ratio,
        tracked,
        cut_score,
        False,
        "rejected",
        reason,
    )


def _initial_state(width: int, height: int, config: HybridCameraConfig) -> _CameraState:
    _minimum, maximum = _crop_height_bounds(width, height, config)
    return _CameraState(width * 0.5, height * 0.5, maximum)


def _plan_frame(
    ball: BallTrackRow,
    motion: CameraMotionEvidence,
    state: _CameraState,
    width: int,
    height: int,
    config: HybridCameraConfig,
) -> _FramePlan:
    aspect = config.target_width / config.target_height
    minimum_crop, maximum_crop = _crop_height_bounds(width, height, config)
    previous_center_x = state.center_x
    previous_center_y = state.center_y
    previous_crop_height = state.crop_height
    previous_pan_velocity_x = state.pan_velocity_x
    previous_pan_velocity_y = state.pan_velocity_y
    previous_zoom_velocity = state.zoom_velocity
    discontinuity = motion.cut_before or ball.frame_index == 0
    trusted_motion = (
        not motion.cut_before
        and motion.confidence is not None
        and motion.confidence >= config.motion_confidence_threshold
        and motion.dx is not None
        and motion.dy is not None
        and motion.scale is not None
        and motion.rotation_degrees is not None
    )
    ball_weight = _ball_weight(ball, config)
    ball_used = ball_weight > 0.0 and ball.x is not None and ball.y is not None

    if motion.cut_before:
        state.shot_id += 1
        state.pan_velocity_x = 0.0
        state.pan_velocity_y = 0.0
        state.zoom_velocity = 0.0
        state.previous_ball_x = None
        state.previous_ball_y = None
        state.previous_ball_frame = None
        state.unknown_streak = 0
        prior_x, prior_y, prior_crop = width * 0.5, height * 0.5, maximum_crop
    elif trusted_motion:
        assert motion.scale is not None
        prior_x, prior_y = _apply_motion_transform(state.center_x, state.center_y, motion)
        prior_crop = max(minimum_crop, min(maximum_crop, state.crop_height * float(motion.scale)))
    else:
        prior_x, prior_y, prior_crop = state.center_x, state.center_y, state.crop_height

    if ball.status == "out_of_view":
        state.previous_ball_x = None
        state.previous_ball_y = None
        state.previous_ball_frame = None
    elif not ball_used and state.previous_ball_x is not None and state.previous_ball_y is not None:
        if trusted_motion:
            state.previous_ball_x, state.previous_ball_y = _apply_motion_transform(
                state.previous_ball_x,
                state.previous_ball_y,
                motion,
            )
        else:
            state.previous_ball_x = None
            state.previous_ball_y = None
            state.previous_ball_frame = None

    speed = _ball_residual_speed(ball, motion, state, trusted_motion, width, height)
    target_x: float | None = None
    target_y: float | None = None
    target_source = "none"
    target_confidence = 0.0
    fallback_reason = ""
    visibility_clamped = False

    if ball_used:
        assert ball.x is not None
        assert ball.y is not None
        state.unknown_streak = 0
        target_x = float(ball.x)
        target_y = float(ball.y)
        target_source = f"global_{ball.status}"
        target_confidence = ball_weight
        desired_crop = _desired_ball_crop(ball, speed, minimum_crop, maximum_crop, width, config)
        ball_center_x = target_x - (config.ball_screen_x_ratio - 0.5) * desired_crop * aspect
        ball_center_y = target_y - (config.ball_screen_y_ratio - 0.5) * desired_crop
        desired_x = _lerp(prior_x, ball_center_x, ball_weight)
        desired_y = _lerp(prior_y, ball_center_y, ball_weight)
        desired_crop = _lerp(prior_crop, desired_crop, ball_weight)
        if motion.cut_before:
            evidence_mode = "scene_cut_ball"
            pan_mode = "scene_cut"
        elif trusted_motion:
            evidence_mode = "ball_and_camera_motion"
            pan_mode = "hybrid"
        else:
            evidence_mode = "ball_only"
            pan_mode = "ball_glide"
    else:
        state.unknown_streak += 1
        if ball.status in {"detected", "interpolated"}:
            fallback_reason = "ball_confidence_below_threshold"
        elif ball.status == "out_of_view":
            fallback_reason = "explicit_out_of_view_wide_fallback"
        elif motion.cut_before:
            fallback_reason = "shot_default_wide"
        elif trusted_motion:
            fallback_reason = "camera_motion_only_no_ball"
        elif state.unknown_streak <= config.unknown_hold_frames:
            fallback_reason = "bounded_camera_hold"
        else:
            fallback_reason = "wide_home_fallback"

        if motion.cut_before or ball.status == "out_of_view":
            desired_x, desired_y, desired_crop = width * 0.5, height * 0.5, maximum_crop
            evidence_mode = "scene_cut_no_ball" if motion.cut_before else "wide_home_fallback"
            pan_mode = "scene_cut" if motion.cut_before else "fallback"
        elif trusted_motion:
            desired_x, desired_y, desired_crop = prior_x, prior_y, prior_crop
            evidence_mode = "camera_motion_only"
            pan_mode = "source_motion"
        elif state.unknown_streak <= config.unknown_hold_frames:
            desired_x, desired_y, desired_crop = state.center_x, state.center_y, state.crop_height
            evidence_mode = "bounded_hold"
            pan_mode = "hold"
        else:
            desired_x = _lerp(state.center_x, width * 0.5, config.fallback_smoothing)
            desired_y = _lerp(state.center_y, height * 0.5, config.fallback_smoothing)
            desired_crop = _lerp(state.crop_height, maximum_crop, config.fallback_smoothing)
            evidence_mode = "wide_home_fallback"
            pan_mode = "fallback"

    if discontinuity:
        next_crop = max(minimum_crop, min(maximum_crop, desired_crop))
        crop_width_int, crop_height_int = _integer_crop_dimensions(next_crop, width, height, aspect)
        crop_width = float(crop_width_int)
        next_crop = float(crop_height_int)
        next_x, next_y = _clamp_center(desired_x, desired_y, crop_width, next_crop, width, height)
        if ball_used and target_x is not None and target_y is not None:
            next_x, next_y, visibility_clamped = _ensure_target_visible(
                next_x,
                next_y,
                crop_width,
                next_crop,
                target_x,
                target_y,
                width,
                height,
                config.target_visibility_margin_ratio,
            )
        if ball.frame_index == 0:
            pan_mode = "shot_start"
    else:
        zoom_candidate, _zoom_velocity = _bounded_axis_step(
            previous_crop_height,
            desired_crop,
            previous_zoom_velocity,
            max(0.0, height * config.max_zoom_step_ratio - 0.51),
            max(0.0, height * config.max_zoom_step_ratio - 0.51),
            config.zoom_smoothing,
        )
        zoom_candidate = max(minimum_crop, min(maximum_crop, zoom_candidate))
        crop_candidates = [zoom_candidate]
        if not math.isclose(zoom_candidate, previous_crop_height, abs_tol=1e-9):
            crop_candidates.append(previous_crop_height)
        selected: tuple[float, float, float, float, bool] | None = None
        for crop_candidate in crop_candidates:
            crop_width_int, crop_height_int = _integer_crop_dimensions(crop_candidate, width, height, aspect)
            crop_width = float(crop_width_int)
            crop_height = float(crop_height_int)
            realized_zoom_velocity = crop_height - previous_crop_height
            zoom_limit = height * config.max_zoom_step_ratio
            if (
                abs(realized_zoom_velocity) > zoom_limit + 1e-6
                or abs(realized_zoom_velocity - previous_zoom_velocity) > zoom_limit + 1e-6
            ):
                continue
            candidate_x, candidate_y = _clamp_center(
                desired_x,
                desired_y,
                crop_width,
                crop_height,
                width,
                height,
            )
            candidate_visibility_clamped = False
            if ball_used and target_x is not None and target_y is not None:
                candidate_x, candidate_y, candidate_visibility_clamped = _ensure_target_visible(
                    candidate_x,
                    candidate_y,
                    crop_width,
                    crop_height,
                    target_x,
                    target_y,
                    width,
                    height,
                    config.target_visibility_margin_ratio,
                )
            bounded_x = _bounded_axis_center(
                current=previous_center_x,
                desired=candidate_x,
                previous_velocity=previous_pan_velocity_x,
                max_step=width * config.max_pan_step_x_ratio,
                max_acceleration=width * config.max_pan_acceleration_x_ratio,
                smoothing=config.pan_smoothing,
                crop_size=crop_width_int,
                frame_size=width,
            )
            bounded_y = _bounded_axis_center(
                current=previous_center_y,
                desired=candidate_y,
                previous_velocity=previous_pan_velocity_y,
                max_step=height * config.max_pan_step_y_ratio,
                max_acceleration=height * config.max_pan_acceleration_y_ratio,
                smoothing=config.pan_smoothing,
                crop_size=crop_height_int,
                frame_size=height,
            )
            if bounded_x is not None and bounded_y is not None:
                selected = (
                    bounded_x,
                    bounded_y,
                    crop_width,
                    crop_height,
                    candidate_visibility_clamped,
                )
                break
        if selected is None:
            raise HybridBroadcastCameraError("camera state cannot satisfy frame-boundary motion budgets")
        next_x, next_y, crop_width, next_crop, visibility_clamped = selected
    crop_x1, crop_y1, crop_x2, crop_y2 = _crop_box(next_x, next_y, next_crop, width, height, aspect)
    next_x = (crop_x1 + crop_x2) / 2.0
    next_y = (crop_y1 + crop_y2) / 2.0
    next_crop = float(crop_y2 - crop_y1)
    target_visible = (
        target_x is not None
        and target_y is not None
        and crop_x1 <= target_x < crop_x2
        and crop_y1 <= target_y < crop_y2
    )
    if ball_used and not target_visible:
        fallback_reason = "bounded_pan_target_not_yet_visible"

    if discontinuity:
        state.pan_velocity_x = 0.0
        state.pan_velocity_y = 0.0
        state.zoom_velocity = 0.0
    else:
        state.pan_velocity_x = next_x - previous_center_x
        state.pan_velocity_y = next_y - previous_center_y
        state.zoom_velocity = next_crop - previous_crop_height
        _validate_realized_motion_budget(
            state,
            previous_pan_velocity_x,
            previous_pan_velocity_y,
            previous_zoom_velocity,
            width,
            height,
            config,
        )

    state.center_x = next_x
    state.center_y = next_y
    state.crop_height = next_crop
    if ball_used:
        state.previous_ball_x = target_x
        state.previous_ball_y = target_y
        state.previous_ball_frame = ball.frame_index

    path = {
        "Frame": ball.frame_index,
        "SchemaVersion": "2.0",
        "ShotId": state.shot_id,
        "CutDetected": motion.cut_before,
        "CenterX": next_x,
        "CenterY": next_y,
        "CropX1": crop_x1,
        "CropY1": crop_y1,
        "CropX2": crop_x2,
        "CropY2": crop_y2,
        "CropWidth": crop_x2 - crop_x1,
        "CropHeight": crop_y2 - crop_y1,
        "Status": ball.status,
        "TrackX": ball.x,
        "TrackY": ball.y,
        "TrackConfidence": ball.confidence,
        "SelectedCandidateId": ball.selected_candidate_id,
        "TargetX": target_x,
        "TargetY": target_y,
        "TargetSource": target_source,
        "TargetConfidence": target_confidence,
        "TargetVisible": target_visible,
        "MotionDx": motion.dx,
        "MotionDy": motion.dy,
        "MotionScale": motion.scale,
        "MotionRotationDegrees": motion.rotation_degrees,
        "MotionConfidence": motion.confidence,
        "MotionMethod": motion.method,
        "MotionRejectReason": motion.reject_reason,
        "EvidenceMode": evidence_mode,
        "FallbackReason": fallback_reason,
        "PanMode": pan_mode,
        "PanVelocityX": state.pan_velocity_x,
        "PanVelocityY": state.pan_velocity_y,
        "ZoomVelocity": state.zoom_velocity,
        "VisibilityClampApplied": visibility_clamped,
    }
    decision = {
        "record_type": "frame",
        "frame_index": ball.frame_index,
        "shot_id": state.shot_id,
        "cut_before": motion.cut_before,
        "ball": {
            "status": ball.status,
            "confidence": ball.confidence,
            "x": ball.x,
            "y": ball.y,
            "selected_candidate_id": ball.selected_candidate_id,
            "source": ball.source,
            "reason": ball.reason,
        },
        "camera_motion": _motion_payload(motion),
        "fusion": {
            "target_x": target_x,
            "target_y": target_y,
            "target_source": target_source,
            "target_confidence": target_confidence,
            "evidence_mode": evidence_mode,
            "fallback_reason": fallback_reason or None,
            "visibility_clamp_applied": visibility_clamped,
        },
        "camera": {
            "center_x": next_x,
            "center_y": next_y,
            "crop_x1": crop_x1,
            "crop_y1": crop_y1,
            "crop_x2": crop_x2,
            "crop_y2": crop_y2,
            "pan_mode": pan_mode,
            "pan_velocity_x": state.pan_velocity_x,
            "pan_velocity_y": state.pan_velocity_y,
            "zoom_velocity": state.zoom_velocity,
            "target_visible": target_visible,
        },
    }
    return _FramePlan(path, decision)


def _ball_weight(ball: BallTrackRow, config: HybridCameraConfig) -> float:
    if (
        ball.status not in {"detected", "interpolated"}
        or ball.confidence is None
        or ball.confidence < config.minimum_ball_confidence
    ):
        return 0.0
    normalized = (ball.confidence - config.minimum_ball_confidence) / max(1e-9, 1.0 - config.minimum_ball_confidence)
    weight = 0.25 + 0.75 * normalized
    if ball.status == "interpolated":
        weight *= config.interpolated_weight
    return max(0.0, min(1.0, weight))


def _ball_residual_speed(
    ball: BallTrackRow,
    motion: CameraMotionEvidence,
    state: _CameraState,
    trusted_motion: bool,
    width: int,
    height: int,
) -> float:
    if (
        ball.x is None
        or ball.y is None
        or state.previous_ball_x is None
        or state.previous_ball_y is None
        or state.previous_ball_frame is None
    ):
        return 0.0
    frame_delta = max(1, ball.frame_index - state.previous_ball_frame)
    previous_x, previous_y = state.previous_ball_x, state.previous_ball_y
    if trusted_motion:
        previous_x, previous_y = _apply_motion_transform(previous_x, previous_y, motion)
    dx = (ball.x - previous_x) / frame_delta / max(1.0, width)
    dy = (ball.y - previous_y) / frame_delta / max(1.0, height)
    return math.hypot(dx, dy)


def _desired_ball_crop(
    ball: BallTrackRow,
    speed_ratio: float,
    minimum: float,
    maximum: float,
    width: int,
    config: HybridCameraConfig,
) -> float:
    assert ball.confidence is not None
    speed_weight = _normalize(
        speed_ratio,
        config.speed_zoom_out_start_ratio,
        config.speed_zoom_out_end_ratio,
    )
    confidence_weight = 1.0 - ball.confidence
    uncertainty = confidence_weight * config.uncertainty_zoom_out_ratio
    if ball.status == "interpolated":
        uncertainty = max(uncertainty, config.uncertainty_zoom_out_ratio)
    ratio = max(0.0, min(1.0, speed_weight + uncertainty))
    _ = width
    return minimum + (maximum - minimum) * ratio


def _apply_motion_transform(x: float, y: float, motion: CameraMotionEvidence) -> tuple[float, float]:
    assert motion.scale is not None
    assert motion.rotation_degrees is not None
    assert motion.dx is not None
    assert motion.dy is not None
    radians = math.radians(motion.rotation_degrees)
    cosine = math.cos(radians) * motion.scale
    sine = math.sin(radians) * motion.scale
    return cosine * x - sine * y + motion.dx, sine * x + cosine * y + motion.dy


def _bounded_axis_step(
    current: float,
    desired: float,
    previous_velocity: float,
    max_step: float,
    max_acceleration: float,
    smoothing: float,
) -> tuple[float, float]:
    requested = max(-max_step, min(max_step, (desired - current) * smoothing))
    velocity = previous_velocity + max(
        -max_acceleration,
        min(max_acceleration, requested - previous_velocity),
    )
    return current + velocity, velocity


def _bounded_axis_center(
    *,
    current: float,
    desired: float,
    previous_velocity: float,
    max_step: float,
    max_acceleration: float,
    smoothing: float,
    crop_size: int,
    frame_size: int,
) -> float | None:
    minimum_velocity = max(-max_step, previous_velocity - max_acceleration)
    maximum_velocity = min(max_step, previous_velocity + max_acceleration)
    half_crop = crop_size / 2.0
    left_bound = half_crop
    right_bound = frame_size - half_crop
    safe_left_speed = _maximum_safe_outward_speed(max(0.0, current - left_bound), max_acceleration, max_step)
    safe_right_speed = _maximum_safe_outward_speed(max(0.0, right_bound - current), max_acceleration, max_step)
    minimum_velocity = max(minimum_velocity, -safe_left_speed)
    maximum_velocity = min(maximum_velocity, safe_right_speed)
    if minimum_velocity > maximum_velocity + 1e-9:
        return None

    requested_velocity = max(
        minimum_velocity,
        min(maximum_velocity, (desired - current) * smoothing),
    )
    minimum_center = current + minimum_velocity
    maximum_center = current + maximum_velocity
    minimum_left = max(0, math.ceil(minimum_center - half_crop - 1e-9))
    maximum_left = min(frame_size - crop_size, math.floor(maximum_center - half_crop + 1e-9))
    if minimum_left > maximum_left:
        return None
    requested_left = int(round(current + requested_velocity - half_crop))
    selected_left = max(minimum_left, min(maximum_left, requested_left))
    return selected_left + half_crop


def _maximum_safe_outward_speed(distance: float, acceleration: float, max_step: float) -> float:
    if distance <= 0.0 or acceleration <= 0.0 or max_step <= 0.0:
        return 0.0
    low = 0.0
    high = min(max_step, distance)
    for _ in range(60):
        candidate = (low + high) / 2.0
        required = candidate + _future_stopping_distance(candidate, acceleration)
        if required <= distance:
            low = candidate
        else:
            high = candidate
    return low


def _future_stopping_distance(speed: float, acceleration: float) -> float:
    if speed <= acceleration or acceleration <= 0.0:
        return 0.0
    steps = max(0, math.ceil(speed / acceleration) - 1)
    return max(0.0, steps * speed - acceleration * steps * (steps + 1) / 2.0)


def _validate_realized_motion_budget(
    state: _CameraState,
    previous_pan_velocity_x: float,
    previous_pan_velocity_y: float,
    previous_zoom_velocity: float,
    width: int,
    height: int,
    config: HybridCameraConfig,
) -> None:
    checks = (
        (abs(state.pan_velocity_x), width * config.max_pan_step_x_ratio, "pan x step"),
        (abs(state.pan_velocity_y), height * config.max_pan_step_y_ratio, "pan y step"),
        (
            abs(state.pan_velocity_x - previous_pan_velocity_x),
            width * config.max_pan_acceleration_x_ratio,
            "pan x acceleration",
        ),
        (
            abs(state.pan_velocity_y - previous_pan_velocity_y),
            height * config.max_pan_acceleration_y_ratio,
            "pan y acceleration",
        ),
        (abs(state.zoom_velocity), height * config.max_zoom_step_ratio, "zoom step"),
        (
            abs(state.zoom_velocity - previous_zoom_velocity),
            height * config.max_zoom_step_ratio,
            "zoom acceleration",
        ),
    )
    for actual, limit, label in checks:
        if actual > limit + 1e-6:
            raise HybridBroadcastCameraError(f"realized {label} exceeds its configured bound")


def _crop_height_bounds(width: int, height: int, config: HybridCameraConfig) -> tuple[float, float]:
    aspect = config.target_width / config.target_height
    feasible_height = min(float(height), width / aspect)
    minimum = max(1.0, feasible_height * config.minimum_crop_height_ratio)
    maximum = max(minimum, feasible_height * config.maximum_crop_height_ratio)
    return minimum, maximum


def _clamp_center(
    x: float,
    y: float,
    crop_width: float,
    crop_height: float,
    width: int,
    height: int,
) -> tuple[float, float]:
    return (
        max(crop_width / 2.0, min(width - crop_width / 2.0, x)),
        max(crop_height / 2.0, min(height - crop_height / 2.0, y)),
    )


def _ensure_target_visible(
    center_x: float,
    center_y: float,
    crop_width: float,
    crop_height: float,
    target_x: float,
    target_y: float,
    width: int,
    height: int,
    margin_ratio: float,
) -> tuple[float, float, bool]:
    margin_x = min(crop_width * margin_ratio, max(0.0, crop_width / 2.0 - 1.0))
    margin_y = min(crop_height * margin_ratio, max(0.0, crop_height / 2.0 - 1.0))
    minimum_x = center_x - crop_width / 2.0 + margin_x
    maximum_x = center_x + crop_width / 2.0 - margin_x
    minimum_y = center_y - crop_height / 2.0 + margin_y
    maximum_y = center_y + crop_height / 2.0 - margin_y
    next_x = center_x
    next_y = center_y
    if target_x < minimum_x:
        next_x -= minimum_x - target_x
    elif target_x > maximum_x:
        next_x += target_x - maximum_x
    if target_y < minimum_y:
        next_y -= minimum_y - target_y
    elif target_y > maximum_y:
        next_y += target_y - maximum_y
    next_x, next_y = _clamp_center(next_x, next_y, crop_width, crop_height, width, height)
    return next_x, next_y, not math.isclose(next_x, center_x) or not math.isclose(next_y, center_y)


def _crop_box(
    center_x: float,
    center_y: float,
    crop_height: float,
    width: int,
    height: int,
    aspect: float,
) -> tuple[int, int, int, int]:
    integer_width, integer_height = _integer_crop_dimensions(crop_height, width, height, aspect)
    left = int(round(center_x - integer_width / 2.0))
    top = int(round(center_y - integer_height / 2.0))
    left = max(0, min(width - integer_width, left))
    top = max(0, min(height - integer_height, top))
    return left, top, left + integer_width, top + integer_height


def _integer_crop_dimensions(
    crop_height: float,
    width: int,
    height: int,
    aspect: float,
) -> tuple[int, int]:
    integer_height = max(1, min(height, int(round(crop_height))))
    integer_width = max(1, min(width, int(round(integer_height * aspect))))
    integer_height = max(1, min(height, int(round(integer_width / aspect))))
    return integer_width, integer_height


def _camera_path_columns() -> list[str]:
    return [
        "Frame",
        "SchemaVersion",
        "ShotId",
        "CutDetected",
        "CenterX",
        "CenterY",
        "CropX1",
        "CropY1",
        "CropX2",
        "CropY2",
        "CropWidth",
        "CropHeight",
        "Status",
        "TrackX",
        "TrackY",
        "TrackConfidence",
        "SelectedCandidateId",
        "TargetX",
        "TargetY",
        "TargetSource",
        "TargetConfidence",
        "TargetVisible",
        "MotionDx",
        "MotionDy",
        "MotionScale",
        "MotionRotationDegrees",
        "MotionConfidence",
        "MotionMethod",
        "MotionRejectReason",
        "EvidenceMode",
        "FallbackReason",
        "PanMode",
        "PanVelocityX",
        "PanVelocityY",
        "ZoomVelocity",
        "VisibilityClampApplied",
    ]


def _format_camera_path_row(row: Mapping[str, Any]) -> dict[str, Any]:
    floats = {
        "CenterX": 6,
        "CenterY": 6,
        "TrackX": 6,
        "TrackY": 6,
        "TrackConfidence": 6,
        "TargetX": 6,
        "TargetY": 6,
        "TargetConfidence": 6,
        "MotionDx": 6,
        "MotionDy": 6,
        "MotionScale": 9,
        "MotionRotationDegrees": 6,
        "MotionConfidence": 6,
        "PanVelocityX": 6,
        "PanVelocityY": 6,
        "ZoomVelocity": 6,
    }
    formatted = dict(row)
    for field, precision in floats.items():
        value = row[field]
        formatted[field] = "" if value is None else f"{float(value):.{precision}f}"
    for field in ("CutDetected", "TargetVisible", "VisibilityClampApplied"):
        formatted[field] = "1" if bool(row[field]) else "0"
    formatted["SelectedCandidateId"] = row["SelectedCandidateId"] or ""
    formatted["MotionRejectReason"] = row["MotionRejectReason"] or ""
    return formatted


def _motion_payload(motion: CameraMotionEvidence) -> dict[str, Any]:
    status = "cut" if motion.cut_before else "accepted"
    if motion.confidence is None or motion.dx is None:
        status = "unknown" if not motion.cut_before else "cut"
    elif motion.reject_reason is not None:
        status = "rejected_low_confidence"
    return {
        "frame_index": motion.frame_index,
        "status": status,
        "dx": _rounded_optional(motion.dx),
        "dy": _rounded_optional(motion.dy),
        "scale": _rounded_optional(motion.scale, 9),
        "rotation_degrees": _rounded_optional(motion.rotation_degrees),
        "confidence": _rounded_optional(motion.confidence),
        "inlier_ratio": _rounded_optional(motion.inlier_ratio),
        "tracked_feature_count": motion.tracked_feature_count,
        "cut_score": round(motion.cut_score, 6),
        "cut_before": motion.cut_before,
        "method": motion.method,
        "reject_reason": motion.reject_reason,
    }


def _rounded_optional(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(float(value), digits)


def _validate_trajectory_bindings(
    report: Mapping[str, Any],
    *,
    source_sha256: str,
    source_size: int,
    metadata: Mapping[str, Any],
    track_snapshot: _FileSnapshot,
) -> None:
    if report.get("schema_version") != "1.0" or report.get("artifact_type") != "global_ball_trajectory_report":
        raise HybridBroadcastCameraError("invalid global trajectory report identity")
    if report.get("status") != "succeeded" or report.get("complete") is not True:
        raise HybridBroadcastCameraError("global trajectory report is not a completed successful generation")
    source = _required_mapping(report.get("source_video"), "trajectory source_video")
    if _required_sha256(source.get("sha256"), "trajectory source sha256") != source_sha256:
        raise HybridBroadcastCameraError("source video sha256 does not match the global trajectory")
    if "size" in source and _positive_integer(source.get("size"), "trajectory source size") != source_size:
        raise HybridBroadcastCameraError("source video size does not match the global trajectory")
    for field in ("width", "height", "frame_count"):
        if _positive_integer(source.get(field), f"trajectory source {field}") != int(metadata[field]):
            raise HybridBroadcastCameraError(f"source video {field} does not match the global trajectory")
    report_fps = _positive_float(source.get("fps"), "trajectory source fps")
    if not math.isclose(report_fps, float(metadata["fps"]), rel_tol=1e-6, abs_tol=1e-6):
        raise HybridBroadcastCameraError("source video fps does not match the global trajectory")
    artifacts = _required_mapping(report.get("artifacts"), "trajectory artifacts")
    track_binding = _required_mapping(artifacts.get("ball_track.v2.csv"), "trajectory ball track artifact")
    if _required_sha256(track_binding.get("sha256"), "trajectory ball track sha256") != track_snapshot.sha256:
        raise HybridBroadcastCameraError("ball track sha256 does not match the global trajectory report")
    if _nonnegative_integer(track_binding.get("size"), "trajectory ball track size") != track_snapshot.size:
        raise HybridBroadcastCameraError("ball track size does not match the global trajectory report")


def _validate_camera_audit(
    audit: Mapping[str, Any],
    solve_summary: Mapping[str, Any],
    audit_path: Path,
) -> None:
    audit_summary = _required_mapping(audit.get("summary"), "camera motion audit summary")
    status = audit_summary.get("status")
    if status not in {"ok", "warn", "fail"}:
        raise HybridBroadcastCameraError("camera motion audit is unavailable")
    frame_count = _nonnegative_integer(audit_summary.get("frame_count"), "camera audit frame_count")
    cut_count = _nonnegative_integer(audit_summary.get("cut_count"), "camera audit cut_count")
    if frame_count != int(solve_summary["row_count"]):
        raise HybridBroadcastCameraError("camera motion audit frame_count does not match the solved path")
    if cut_count != int(solve_summary["cut_count"]):
        raise HybridBroadcastCameraError("camera motion audit cut_count does not match the solved path")
    if not audit_path.is_file() or audit_path.stat().st_size <= 0:
        raise HybridBroadcastCameraError("camera motion audit artifact is missing or empty")


def _build_report(
    *,
    staging_dir: Path,
    source_lease: Any,
    track_snapshot: _FileSnapshot,
    trajectory_report_snapshot: _FileSnapshot,
    trajectory_report: Mapping[str, Any],
    metadata: Mapping[str, Any],
    config: HybridCameraConfig,
    summary: Mapping[str, Any],
    audit: Mapping[str, Any],
) -> dict[str, Any]:
    artifacts: dict[str, dict[str, Any]] = {}
    for name in (CAMERA_PATH_NAME, MOTION_EVIDENCE_NAME, DECISIONS_NAME, AUDIT_NAME):
        path = staging_dir / name
        artifacts[name] = {"sha256": _sha256_file(path), "size": path.stat().st_size}
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "artifact_type": REPORT_ARTIFACT_TYPE,
        "status": "succeeded",
        "complete": True,
        "algorithm": {
            "version": ALGORITHM_VERSION,
            "configuration": asdict(config),
            "motion_model": "deterministic_bounded_partial_affine",
            "fusion_model": "confidence_weighted_ball_and_source_camera_motion",
        },
        "source_video": {
            "path": str(source_lease.snapshot.path),
            "sha256": source_lease.snapshot.sha256,
            "size": source_lease.snapshot.size,
            **dict(metadata),
        },
        "inputs": {
            "global_ball_track": {
                "path": str(track_snapshot.path),
                "sha256": track_snapshot.sha256,
                "size": track_snapshot.size,
            },
            "global_trajectory_report": {
                "path": str(trajectory_report_snapshot.path),
                "sha256": trajectory_report_snapshot.sha256,
                "size": trajectory_report_snapshot.size,
                "algorithm_version": _required_mapping(trajectory_report.get("algorithm"), "trajectory algorithm").get(
                    "version"
                ),
            },
        },
        "rendering": {
            "target_width": config.target_width,
            "target_height": config.target_height,
            "target_aspect_ratio": config.target_width / config.target_height,
        },
        "summary": {
            **dict(summary),
            "camera_motion_audit": dict(audit.get("summary", {})),
        },
        "artifacts": artifacts,
    }


def _capture_snapshot(
    path: Path,
    label: str,
    copy_path: Path,
    *,
    max_bytes: int | None = None,
) -> _FileSnapshot:
    if not path.is_file():
        raise HybridBroadcastCameraError(f"{label} does not exist: {path}")
    before = path.stat()
    if max_bytes is not None and int(before.st_size) > max_bytes:
        raise HybridBroadcastCameraError(f"{label} exceeds the size limit")
    digest = hashlib.sha256()
    copied = 0
    with path.open("rb") as source, copy_path.open("xb") as target:
        while True:
            chunk = source.read(_COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            target.write(chunk)
            copied += len(chunk)
            if max_bytes is not None and copied > max_bytes:
                raise HybridBroadcastCameraError(f"{label} exceeds the size limit")
        target.flush()
        os.fsync(target.fileno())
    after = path.stat()
    token = _stat_token(before)
    if token != _stat_token(after) or copied != int(after.st_size):
        raise HybridBroadcastCameraError(f"{label} changed while its stable snapshot was captured")
    return _FileSnapshot(label, path, copy_path, digest.hexdigest(), copied, token)


def _verify_snapshot_stat(snapshot: _FileSnapshot) -> None:
    try:
        current = snapshot.path.stat()
    except OSError as exc:
        raise HybridBroadcastCameraError(f"{snapshot.label} changed during camera solving") from exc
    if _stat_token(current) != snapshot.stat_token:
        raise HybridBroadcastCameraError(f"{snapshot.label} changed during camera solving")


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        if path.stat().st_size > _MAX_REPORT_BYTES:
            raise HybridBroadcastCameraError(f"{label} exceeds the size limit")
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle, object_pairs_hook=_reject_duplicate_keys)
    except HybridBroadcastCameraError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise HybridBroadcastCameraError(f"{label} is not valid unambiguous JSON") from exc
    if not isinstance(value, dict):
        raise HybridBroadcastCameraError(f"{label} must be a JSON object")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _required_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HybridBroadcastCameraError(f"{label} must be an object")
    return value


def _required_sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise HybridBroadcastCameraError(f"{label} must be a lowercase sha256")
    return value


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HybridBroadcastCameraError(f"{label} must be a positive integer")
    return value


def _nonnegative_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HybridBroadcastCameraError(f"{label} must be a non-negative integer")
    return value


def _positive_float(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise HybridBroadcastCameraError(f"{label} must be finite and positive")
    if float(value) <= 0.0:
        raise HybridBroadcastCameraError(f"{label} must be finite and positive")
    return float(value)


def _parse_integer_text(value: Any, label: str) -> int:
    text = str(value or "").strip()
    if not text or any(character not in "0123456789" for character in text):
        raise HybridBroadcastCameraError(f"{label} must be a non-negative integer")
    return int(text)


def _parse_probability_text(value: Any, label: str) -> float:
    parsed = _required_float_text(value, label)
    if not 0.0 <= parsed <= 1.0:
        raise HybridBroadcastCameraError(f"{label} must be in [0, 1]")
    return parsed


def _optional_probability_text(value: Any, label: str) -> float | None:
    text = str(value or "").strip()
    return None if text == "" else _parse_probability_text(text, label)


def _optional_float_text(value: Any, label: str) -> float | None:
    text = str(value or "").strip()
    return None if text == "" else _required_float_text(text, label)


def _required_float_text(value: Any, label: str) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise HybridBroadcastCameraError(f"{label} must be finite") from exc
    if not math.isfinite(parsed):
        raise HybridBroadcastCameraError(f"{label} must be finite")
    return parsed


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HybridBroadcastCameraError(f"{label} must be non-empty text")
    return value.strip()


def _normalize(value: float, start: float, end: float) -> float:
    if end <= start:
        return 0.0
    return max(0.0, min(1.0, (value - start) / (end - start)))


def _lerp(current: float, target: float, alpha: float) -> float:
    return current + (target - current) * max(0.0, min(1.0, alpha))


def _write_json_line(handle: Any, value: Mapping[str, Any]) -> None:
    handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")


def _write_json_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_json_commit(path: Path, value: Mapping[str, Any]) -> None:
    if path.exists():
        raise HybridBroadcastCameraError("hybrid camera commit report already exists")
    pending = path.parent / f".{path.name}.pending-{uuid4().hex}"
    try:
        _write_json_exclusive(pending, value)
        os.replace(pending, path)
        _fsync_directory(path.parent)
    finally:
        try:
            pending.unlink(missing_ok=True)
        except OSError:
            pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _stat_token(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_size),
        int(value.st_mtime_ns),
        int(getattr(value, "st_ctime_ns", 0)),
        int(getattr(value, "st_ino", 0)),
        int(getattr(value, "st_dev", 0)),
    )


def _lock_path(output_dir: Path) -> Path:
    scope = hashlib.sha256(str(output_dir).casefold().encode("utf-8")).hexdigest()
    return Path(tempfile.gettempdir()) / f"football-tracking-hybrid-camera-{scope}.lock"


def _acquire_output_lock(output_dir: Path) -> BinaryIO:
    handle = _lock_path(output_dir).open("a+b")
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
            raise HybridBroadcastCameraError(f"hybrid camera output is already locked: {output_dir}") from exc
        raise
    return handle


def _release_output_lock(handle: BinaryIO) -> None:
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


def _publish_generation(staging_dir: Path, output_dir: Path) -> None:
    if output_dir.exists():
        raise HybridBroadcastCameraError("immutable hybrid camera generation already exists")
    published = False
    try:
        os.replace(staging_dir, output_dir)
        published = True
        _fsync_directory(output_dir.parent)
    except BaseException:
        if published:
            _discard_published_generation(output_dir)
        raise


def _discard_published_generation(output_dir: Path) -> None:
    if not output_dir.exists():
        return
    quarantine = output_dir.parent / f".{output_dir.name}.invalid-{uuid4().hex}"
    try:
        os.replace(output_dir, quarantine)
    except OSError:
        shutil.rmtree(output_dir, ignore_errors=True)
        return
    shutil.rmtree(quarantine, ignore_errors=True)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
