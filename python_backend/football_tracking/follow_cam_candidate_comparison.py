from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

from football_tracking.ai_candidate_comparison import build_candidate_comparison, write_candidate_comparison_report
from football_tracking.camera_motion_audit import (
    PAN_ACCEL_WARN_PX,
    PAN_STEP_WARN_PX,
    ZOOM_STEP_FAIL_PX,
)

FOLLOW_CAM_CANDIDATE_COMPARISON_NAME = "follow_cam_candidate_comparison.json"
FOLLOW_CAM_APPROVAL_ACTIONS = {"adjust_follow_cam", "tracking_rerun_before_follow_cam"}
MIN_CAMERA_PATH_FRAMES = 3
MIN_BALL_CROP_COVERAGE = 0.90
MIN_TARGET_WINDOW_VISIBILITY = 0.80
MAX_COVERAGE_DROP = 0.05
MAX_CAMERA_REGRESSION_RATIO = 0.05
ZOOM_OUT_ONLY_MEAN_CROP_RATIO = 1.20
MIN_RAW_PAN_IMPROVEMENT_RATIO = 0.10
MIN_MOTION_IMPROVEMENT_RATIO = 0.20
TARGET_WINDOW_ROI_KEYS = ("effective_roi", "local_search_roi", "approved_roi", "padded_roi")


def build_follow_cam_candidate_comparison(
    candidate_dir: Path,
    *,
    baseline_dir: Path,
    candidate_id: str,
    approval: dict[str, Any] | None = None,
    baseline_camera_path_name: str = "camera_path.csv",
    candidate_camera_path_name: str = "camera_path.csv",
    baseline_audit_name: str = "camera_motion_audit.json",
    candidate_audit_name: str = "camera_motion_audit.json",
    candidate_dir_relative: str | None = None,
    comparison_report: str = FOLLOW_CAM_CANDIDATE_COMPARISON_NAME,
    candidate_artifacts: list[str] | None = None,
) -> dict[str, Any]:
    candidate_id = _safe_candidate_id(candidate_id)
    baseline_dir = Path(baseline_dir)
    candidate_dir = Path(candidate_dir)
    baseline_audit = _load_json(baseline_dir / baseline_audit_name)
    candidate_audit = _load_json(candidate_dir / candidate_audit_name)
    baseline_rows = _read_camera_path_rows(baseline_dir / baseline_camera_path_name)
    candidate_rows = _read_camera_path_rows(candidate_dir / candidate_camera_path_name)
    metrics = _comparison_metrics(
        baseline_audit=baseline_audit,
        candidate_audit=candidate_audit,
        baseline_rows=baseline_rows,
        candidate_rows=candidate_rows,
        approval=approval,
    )
    checks = _comparison_checks(metrics, approval, candidate_id=candidate_id)
    payload = build_candidate_comparison(
        problem_type="follow_cam",
        baseline={
            "path": str(baseline_dir / baseline_camera_path_name),
            "audit": str(baseline_dir / baseline_audit_name),
            "metrics": metrics.get("baseline", {}),
        },
        candidate={
            "id": candidate_id,
            "path": str(candidate_dir / candidate_camera_path_name),
            "audit": str(candidate_dir / candidate_audit_name),
            "metrics": metrics.get("candidate", {}),
        },
        approval=approval,
        checks=checks,
    )
    payload["metrics"] = metrics
    payload["promotion_eligible"] = payload["summary"]["promotion_eligible"]
    payload["requires_human_confirmation"] = payload["summary"]["requires_human_confirmation"]
    payload["candidate_id"] = candidate_id
    payload["approval_id"] = _approval_id(approval)
    payload["comparison_report"] = comparison_report
    payload["comparison_status"] = payload["summary"]["status"]
    payload["promotion_status"] = "not_promoted"
    payload["consumed_approval_ids"] = _consumed_approval_ids(approval)
    payload["candidate_dir"] = candidate_dir_relative or _parent_dir(candidate_dir / candidate_camera_path_name)
    payload["candidate_artifacts"] = candidate_artifacts or [str(candidate_dir / candidate_camera_path_name)]
    return payload


def write_follow_cam_candidate_comparison(
    candidate_dir: Path,
    *,
    baseline_dir: Path,
    candidate_id: str,
    approval: dict[str, Any] | None = None,
    baseline_camera_path_name: str = "camera_path.csv",
    candidate_camera_path_name: str = "camera_path.csv",
    baseline_audit_name: str = "camera_motion_audit.json",
    candidate_audit_name: str = "camera_motion_audit.json",
    candidate_dir_relative: str | None = None,
    comparison_report: str = FOLLOW_CAM_CANDIDATE_COMPARISON_NAME,
    candidate_artifacts: list[str] | None = None,
) -> Path:
    payload = build_follow_cam_candidate_comparison(
        candidate_dir,
        baseline_dir=baseline_dir,
        candidate_id=candidate_id,
        approval=approval,
        baseline_camera_path_name=baseline_camera_path_name,
        candidate_camera_path_name=candidate_camera_path_name,
        baseline_audit_name=baseline_audit_name,
        candidate_audit_name=candidate_audit_name,
        candidate_dir_relative=candidate_dir_relative,
        comparison_report=comparison_report,
        candidate_artifacts=candidate_artifacts,
    )
    return write_candidate_comparison_report(Path(candidate_dir), payload, name=FOLLOW_CAM_CANDIDATE_COMPARISON_NAME)


def _comparison_checks(metrics: dict[str, Any], approval: dict[str, Any] | None, *, candidate_id: str) -> list[dict[str, Any]]:
    return [
        _evidence_available_check(metrics),
        _approval_linkage_check(approval, candidate_id=candidate_id),
        _review_events_not_worse_check(metrics),
        _camera_regression_check(metrics),
        _motion_improvement_check(metrics),
        _zoom_step_check(metrics),
        _not_zoom_out_only_check(metrics),
        _target_window_visibility_check(metrics),
        _ball_crop_coverage_check(metrics),
    ]


def _comparison_metrics(
    *,
    baseline_audit: dict[str, Any] | None,
    candidate_audit: dict[str, Any] | None,
    baseline_rows: list[dict[str, Any]] | None,
    candidate_rows: list[dict[str, Any]] | None,
    approval: dict[str, Any] | None,
) -> dict[str, Any]:
    baseline_summary = _audit_summary(baseline_audit)
    candidate_summary = _audit_summary(candidate_audit)
    baseline_path = _path_metrics(baseline_rows)
    candidate_path = _path_metrics(candidate_rows)
    coverage = _coverage_metrics(baseline_rows, candidate_rows)
    camera_regression = _camera_regression_metrics(baseline_summary, candidate_summary)
    target_window_visibility = _target_window_visibility_metrics(approval, candidate_rows)
    return {
        "baseline": {**baseline_summary, **baseline_path},
        "candidate": {**candidate_summary, **candidate_path},
        "ball_crop_coverage": coverage,
        "target_window_visibility": target_window_visibility,
        "camera_regression": camera_regression,
        "p95_pan_improvement_ratio": _decrease_ratio(
            baseline_summary["p95_pan_step_px"],
            candidate_summary["p95_pan_step_px"],
        ),
        "max_accel_improvement_ratio": _decrease_ratio(
            baseline_summary["max_pan_accel_px"],
            candidate_summary["max_pan_accel_px"],
        ),
        "raw_p95_pan_improvement_ratio": _decrease_ratio(
            baseline_path["raw_p95_pan_step_px"],
            candidate_path["raw_p95_pan_step_px"],
        ),
        "mean_crop_height_ratio": _ratio(candidate_path["mean_crop_height"], baseline_path["mean_crop_height"]),
    }


def _evidence_available_check(metrics: dict[str, Any]) -> dict[str, Any]:
    baseline = metrics["baseline"]
    candidate = metrics["candidate"]
    reasons: list[str] = []
    for role, item in (("baseline", baseline), ("candidate", candidate)):
        if item["audit_status"] == "unavailable":
            reasons.append(f"{role} camera_motion_audit unavailable")
        if item["camera_path_frame_count"] < MIN_CAMERA_PATH_FRAMES:
            reasons.append(f"{role} camera_path has fewer than {MIN_CAMERA_PATH_FRAMES} frames")
    if reasons:
        return {"name": "camera_path_evidence_available", "status": "unavailable", "reasons": reasons}
    return {
        "name": "camera_path_evidence_available",
        "status": "pass",
        "baseline_frame_count": baseline["camera_path_frame_count"],
        "candidate_frame_count": candidate["camera_path_frame_count"],
    }


def _approval_linkage_check(approval: dict[str, Any] | None, *, candidate_id: str) -> dict[str, Any]:
    if not isinstance(approval, dict):
        return {"name": "approval_linkage", "status": "unavailable", "reason": "approval provenance is absent"}
    approval_id = _approval_id(approval)
    if approval_id is None:
        return {"name": "approval_linkage", "status": "fail", "reason": "approval_id is required"}
    action = str(approval.get("approved_action") or "")
    if action not in FOLLOW_CAM_APPROVAL_ACTIONS:
        return {
            "name": "approval_linkage",
            "status": "fail",
            "approval_id": approval_id,
            "approved_action": action,
            "reason": "approved_action is not a follow-cam action",
        }
    approval_candidate_id = approval.get("candidate_id")
    if isinstance(approval_candidate_id, str) and approval_candidate_id.strip() and approval_candidate_id.strip() != candidate_id:
        return {
            "name": "approval_linkage",
            "status": "fail",
            "approval_id": approval_id,
            "candidate_id": candidate_id,
            "approval_candidate_id": approval_candidate_id,
            "reason": "approval candidate_id does not match comparison candidate",
        }
    return {"name": "approval_linkage", "status": "pass", "approval_id": approval_id, "candidate_id": candidate_id}


def _review_events_not_worse_check(metrics: dict[str, Any]) -> dict[str, Any]:
    baseline = int(metrics["baseline"]["review_event_count"])
    candidate = int(metrics["candidate"]["review_event_count"])
    status = "pass" if candidate <= baseline else "fail"
    return {
        "name": "review_events_not_worse",
        "status": status,
        "baseline_value": baseline,
        "candidate_value": candidate,
        "reason": "candidate review event count is not worse" if status == "pass" else "candidate adds camera review events",
    }


def _camera_regression_check(metrics: dict[str, Any]) -> dict[str, Any]:
    regression = metrics["camera_regression"]
    regressions = list(regression["regressions"])
    status = "pass" if not regressions else "fail"
    return {
        "name": "camera_regression",
        "status": status,
        "regressions": regressions,
        "max_allowed_regression_ratio": MAX_CAMERA_REGRESSION_RATIO,
        "baseline_review_event_count": regression["baseline_review_event_count"],
        "candidate_review_event_count": regression["candidate_review_event_count"],
        "baseline_max_pan_step_px": regression["baseline_max_pan_step_px"],
        "candidate_max_pan_step_px": regression["candidate_max_pan_step_px"],
        "max_allowed_pan_step_px": regression["max_allowed_pan_step_px"],
        "baseline_max_pan_accel_px": regression["baseline_max_pan_accel_px"],
        "candidate_max_pan_accel_px": regression["candidate_max_pan_accel_px"],
        "max_allowed_pan_accel_px": regression["max_allowed_pan_accel_px"],
        "reason": "candidate camera motion does not regress" if status == "pass" else "candidate camera motion regresses beyond allowed limits",
    }


def _motion_improvement_check(metrics: dict[str, Any]) -> dict[str, Any]:
    candidate = metrics["candidate"]
    p95_improvement = float(metrics["p95_pan_improvement_ratio"])
    accel_improvement = float(metrics["max_accel_improvement_ratio"])
    below_warn = candidate["p95_pan_step_px"] < PAN_STEP_WARN_PX and candidate["max_pan_accel_px"] < PAN_ACCEL_WARN_PX
    improved = p95_improvement >= MIN_MOTION_IMPROVEMENT_RATIO or accel_improvement >= MIN_MOTION_IMPROVEMENT_RATIO
    if below_warn or improved:
        status = "pass"
        reason = "camera motion improved enough or is below warn thresholds"
    else:
        status = "fail"
        reason = "candidate does not improve p95 pan or max acceleration enough"
    return {
        "name": "motion_improvement",
        "status": status,
        "baseline_p95_pan_step_px": metrics["baseline"]["p95_pan_step_px"],
        "candidate_p95_pan_step_px": candidate["p95_pan_step_px"],
        "baseline_max_pan_accel_px": metrics["baseline"]["max_pan_accel_px"],
        "candidate_max_pan_accel_px": candidate["max_pan_accel_px"],
        "p95_pan_improvement_ratio": p95_improvement,
        "max_accel_improvement_ratio": accel_improvement,
        "reason": reason,
    }


def _zoom_step_check(metrics: dict[str, Any]) -> dict[str, Any]:
    value = float(metrics["candidate"]["max_zoom_step_px"])
    status = "pass" if value < ZOOM_STEP_FAIL_PX else "fail"
    return {
        "name": "max_zoom_step_within_fail_threshold",
        "status": status,
        "candidate_value": value,
        "fail_threshold": ZOOM_STEP_FAIL_PX,
        "reason": "candidate zoom step is below fail threshold" if status == "pass" else "candidate zoom step exceeds fail threshold",
    }


def _not_zoom_out_only_check(metrics: dict[str, Any]) -> dict[str, Any]:
    crop_ratio = float(metrics["mean_crop_height_ratio"])
    raw_improvement = float(metrics["raw_p95_pan_improvement_ratio"])
    if crop_ratio >= ZOOM_OUT_ONLY_MEAN_CROP_RATIO and raw_improvement < MIN_RAW_PAN_IMPROVEMENT_RATIO:
        status = "fail"
        reason = "candidate appears smoother only because the crop zooms out"
    else:
        status = "pass"
        reason = "candidate motion improvement is not explained solely by zooming out"
    return {
        "name": "not_zoom_out_only",
        "status": status,
        "mean_crop_height_ratio": crop_ratio,
        "raw_p95_pan_improvement_ratio": raw_improvement,
        "reason": reason,
    }


def _ball_crop_coverage_check(metrics: dict[str, Any]) -> dict[str, Any]:
    coverage = metrics["ball_crop_coverage"]
    baseline = float(coverage["baseline_coverage"])
    candidate = float(coverage["candidate_coverage"])
    matched = int(coverage["matched_frame_count"])
    if matched == 0:
        return {"name": "ball_crop_coverage", "status": "unavailable", "reason": "no matching tracked frames"}
    drop = baseline - candidate
    if candidate < MIN_BALL_CROP_COVERAGE or drop > MAX_COVERAGE_DROP:
        status = "fail"
        reason = "candidate crop hides too many tracked ball frames"
    else:
        status = "pass"
        reason = "candidate preserves tracked ball crop coverage"
    return {
        "name": "ball_crop_coverage",
        "status": status,
        "baseline_value": baseline,
        "candidate_value": candidate,
        "coverage_drop": round(drop, 6),
        "minimum_candidate_coverage": MIN_BALL_CROP_COVERAGE,
        "maximum_allowed_drop": MAX_COVERAGE_DROP,
        "matched_frame_count": matched,
        "reason": reason,
    }


def _target_window_visibility_check(metrics: dict[str, Any]) -> dict[str, Any]:
    visibility = metrics["target_window_visibility"]
    sample_count = int(visibility["sample_count"])
    if sample_count == 0:
        return {
            "name": "target_window_visibility",
            "status": "unavailable",
            "reason": "no target window, ROI, or TrackX/TrackY samples were available",
            "sample_count": 0,
            "minimum_visibility": MIN_TARGET_WINDOW_VISIBILITY,
        }
    ratio = float(visibility["visibility_ratio"])
    status = "pass" if ratio >= MIN_TARGET_WINDOW_VISIBILITY else "fail"
    return {
        "name": "target_window_visibility",
        "status": status,
        "baseline_value": MIN_TARGET_WINDOW_VISIBILITY,
        "candidate_value": ratio,
        "visibility_ratio": ratio,
        "visible_sample_count": int(visibility["visible_sample_count"]),
        "hidden_sample_count": int(visibility["hidden_sample_count"]),
        "sample_count": sample_count,
        "minimum_visibility": MIN_TARGET_WINDOW_VISIBILITY,
        "hidden_samples": visibility["hidden_samples"],
        "target_windows": visibility["target_windows"],
        "reason": "candidate crop keeps the target window visible" if status == "pass" else "candidate crop hides the target in too many sampled frames",
    }


def _camera_regression_metrics(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    baseline_review_events = int(baseline["review_event_count"])
    candidate_review_events = int(candidate["review_event_count"])
    baseline_step = float(baseline["max_pan_step_px"])
    candidate_step = float(candidate["max_pan_step_px"])
    baseline_accel = float(baseline["max_pan_accel_px"])
    candidate_accel = float(candidate["max_pan_accel_px"])
    max_allowed_step = _regression_limit(baseline_step)
    max_allowed_accel = _regression_limit(baseline_accel)
    regressions: list[str] = []
    if candidate_review_events > baseline_review_events:
        regressions.append("review_event_count")
    if candidate_step > max_allowed_step + 1e-9:
        regressions.append("max_pan_step_px")
    if candidate_accel > max_allowed_accel + 1e-9:
        regressions.append("max_pan_accel_px")
    return {
        "max_allowed_regression_ratio": MAX_CAMERA_REGRESSION_RATIO,
        "baseline_review_event_count": baseline_review_events,
        "candidate_review_event_count": candidate_review_events,
        "baseline_max_pan_step_px": baseline_step,
        "candidate_max_pan_step_px": candidate_step,
        "max_allowed_pan_step_px": round(max_allowed_step, 6),
        "baseline_max_pan_accel_px": baseline_accel,
        "candidate_max_pan_accel_px": candidate_accel,
        "max_allowed_pan_accel_px": round(max_allowed_accel, 6),
        "regressions": regressions,
    }


def _target_window_visibility_metrics(
    approval: dict[str, Any] | None,
    candidate_rows: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    rows_by_frame = _rows_by_frame(candidate_rows)
    samples = _target_visibility_samples(approval, rows_by_frame)
    visible = [sample for sample in samples if sample["in_crop"]]
    hidden = [sample for sample in samples if not sample["in_crop"]]
    sample_count = len(samples)
    return {
        "minimum_visibility": MIN_TARGET_WINDOW_VISIBILITY,
        "sample_count": sample_count,
        "visible_sample_count": len(visible),
        "hidden_sample_count": len(hidden),
        "visibility_ratio": round(len(visible) / sample_count, 6) if sample_count else 0.0,
        "target_windows": _target_windows_payload(approval, rows_by_frame, samples),
        "hidden_samples": hidden[:20],
        "sample_sources": sorted({str(sample["source"]) for sample in samples}),
    }


def _target_visibility_samples(
    approval: dict[str, Any] | None,
    rows_by_frame: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    windows = _target_windows(approval)
    roi_specs = _roi_target_specs(approval)
    samples: list[dict[str, Any]] = []
    for spec in roi_specs:
        for frame in _frames_for_roi_spec(spec, windows, rows_by_frame):
            sample = _visibility_sample(
                frame,
                float(spec["x"]),
                float(spec["y"]),
                rows_by_frame.get(frame),
                source=str(spec["source"]),
            )
            samples.append(sample)

    for start, end in windows:
        for frame in range(start, end + 1):
            row = rows_by_frame.get(frame)
            x = _optional_float(row.get("TrackX")) if isinstance(row, dict) else None
            y = _optional_float(row.get("TrackY")) if isinstance(row, dict) else None
            if x is None or y is None:
                samples.append(_missing_visibility_sample(frame, source="candidate_camera_path_track", reason="missing_track_point"))
                continue
            samples.append(_visibility_sample(frame, x, y, row, source="candidate_camera_path_track"))
    return sorted(samples, key=lambda sample: (int(sample["frame"]), str(sample["source"])))


def _visibility_sample(
    frame: int,
    x: float,
    y: float,
    row: dict[str, Any] | None,
    *,
    source: str,
) -> dict[str, Any]:
    crop = _crop_bounds(row)
    in_crop = bool(crop is not None and crop[0] <= x <= crop[2] and crop[1] <= y <= crop[3])
    return {
        "frame": frame,
        "target_x": round(float(x), 6),
        "target_y": round(float(y), 6),
        "in_crop": in_crop,
        "source": source,
    }


def _missing_visibility_sample(frame: int, *, source: str, reason: str) -> dict[str, Any]:
    return {
        "frame": frame,
        "target_x": None,
        "target_y": None,
        "in_crop": False,
        "source": source,
        "reason": reason,
    }


def _frames_for_roi_spec(
    spec: dict[str, Any],
    windows: list[tuple[int, int]],
    rows_by_frame: dict[int, dict[str, Any]],
) -> list[int]:
    frame = spec.get("frame")
    if isinstance(frame, int):
        return [frame]
    start = spec.get("start_frame")
    end = spec.get("end_frame")
    if isinstance(start, int) and isinstance(end, int):
        return list(range(start, end + 1))
    if windows:
        frames: list[int] = []
        for start_frame, end_frame in windows:
            frames.extend(range(start_frame, end_frame + 1))
        return sorted(set(frames))
    return sorted(rows_by_frame)


def _roi_target_specs(approval: dict[str, Any] | None) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for entry in _target_scope_entries(approval):
        window = _window_from_mapping(entry)
        for key in TARGET_WINDOW_ROI_KEYS:
            center = _roi_center(entry.get(key))
            if center is None:
                continue
            frame = _roi_frame(entry.get(key), fallback=entry.get("frame"))
            spec = {
                "source": key,
                "x": center[0],
                "y": center[1],
                "frame": frame,
                "start_frame": window[0] if window is not None else None,
                "end_frame": window[1] if window is not None else None,
            }
            identity = (
                spec["source"],
                spec["frame"],
                spec["start_frame"],
                spec["end_frame"],
                round(float(spec["x"]), 6),
                round(float(spec["y"]), 6),
            )
            if identity in seen:
                continue
            seen.add(identity)
            specs.append(spec)
    return specs


def _target_windows(approval: dict[str, Any] | None) -> list[tuple[int, int]]:
    windows: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for entry in _target_scope_entries(approval):
        window = _window_from_mapping(entry)
        if window is None or window in seen:
            continue
        seen.add(window)
        windows.append(window)
    return _merge_windows(windows)


def _merge_windows(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(windows):
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _target_windows_payload(
    approval: dict[str, Any] | None,
    rows_by_frame: dict[int, dict[str, Any]],
    samples: list[dict[str, Any]],
) -> list[dict[str, int]]:
    windows = _target_windows(approval)
    if not windows and samples:
        frames = [int(sample["frame"]) for sample in samples]
        windows = [(min(frames), max(frames))]
    return [
        {"start_frame": start, "end_frame": end, "frame_count": end - start + 1}
        for start, end in windows
    ]


def _target_scope_entries(approval: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(approval, dict):
        return []
    entries = [approval]
    for key in ("target_window", "required_window", "rerun_scope", "window"):
        value = approval.get(key)
        if isinstance(value, dict):
            entries.append(value)
    return entries


def _window_from_mapping(value: dict[str, Any]) -> tuple[int, int] | None:
    start = _optional_int(value.get("start_frame"))
    end = _optional_int(value.get("end_frame"))
    if start is not None and end is not None:
        return (start, end) if start <= end else (end, start)
    return None


def _roi_center(value: Any) -> tuple[float, float] | None:
    bounds = _roi_bounds(value)
    if bounds is None:
        return None
    left, top, right, bottom = bounds
    return (left + right) / 2.0, (top + bottom) / 2.0


def _roi_bounds(value: Any) -> tuple[float, float, float, float] | None:
    if isinstance(value, list) and len(value) == 4:
        parsed = [_optional_float(item) for item in value]
        if any(item is None for item in parsed):
            return None
        left, top, right, bottom = [float(item) for item in parsed if item is not None]
    elif isinstance(value, dict):
        left = _optional_float(value.get("left"))
        top = _optional_float(value.get("top"))
        right = _optional_float(value.get("right"))
        bottom = _optional_float(value.get("bottom"))
        if None in (left, top, right, bottom):
            x = _optional_float(value.get("x"))
            y = _optional_float(value.get("y"))
            width = _optional_float(value.get("width"))
            height = _optional_float(value.get("height"))
            if x is None or y is None or width is None or height is None:
                return None
            left, top, right, bottom = x, y, x + width, y + height
        left, top, right, bottom = float(left), float(top), float(right), float(bottom)
    else:
        return None
    if right < left or bottom < top:
        return None
    return left, top, right, bottom


def _roi_frame(value: Any, *, fallback: Any = None) -> int | None:
    if isinstance(value, dict):
        parsed = _optional_int(value.get("frame"))
        if parsed is not None:
            return parsed
    return _optional_int(fallback)


def _crop_bounds(row: dict[str, Any] | None) -> tuple[float, float, float, float] | None:
    if not isinstance(row, dict):
        return None
    x1 = _optional_float(row.get("CropX1"))
    y1 = _optional_float(row.get("CropY1"))
    x2 = _optional_float(row.get("CropX2"))
    y2 = _optional_float(row.get("CropY2"))
    if None in (x1, y1, x2, y2):
        return None
    left, right = sorted((float(x1), float(x2)))
    top, bottom = sorted((float(y1), float(y2)))
    return left, top, right, bottom


def _audit_summary(audit: dict[str, Any] | None) -> dict[str, Any]:
    summary = audit.get("summary") if isinstance(audit, dict) and isinstance(audit.get("summary"), dict) else {}
    return {
        "audit_status": str(summary.get("status") or "unavailable"),
        "audit_frame_count": _int(summary.get("frame_count")),
        "review_event_count": _int(summary.get("review_event_count")),
        "max_pan_step_px": _float(summary.get("max_pan_step_px")),
        "p95_pan_step_px": _float(summary.get("p95_pan_step_px")),
        "max_pan_accel_px": _float(summary.get("max_pan_accel_px")),
        "max_zoom_step_px": _float(summary.get("max_zoom_step_px")),
        "max_zoom_step_ratio": _float(summary.get("max_zoom_step_ratio")),
    }


def _path_metrics(rows: list[dict[str, Any]] | None) -> dict[str, Any]:
    rows = rows or []
    crop_heights = [_float(row.get("CropHeight")) for row in rows]
    crop_heights = [value for value in crop_heights if value > 0]
    return {
        "camera_path_frame_count": len(rows),
        "mean_crop_height": round(sum(crop_heights) / len(crop_heights), 6) if crop_heights else 0.0,
        "raw_p95_pan_step_px": _percentile95(_raw_pan_steps(rows)),
    }


def _coverage_metrics(
    baseline_rows: list[dict[str, Any]] | None,
    candidate_rows: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    baseline_by_frame = _rows_by_frame(baseline_rows)
    candidate_by_frame = _rows_by_frame(candidate_rows)
    baseline_tracked_frames = [
        frame
        for frame in sorted(baseline_by_frame)
        if _has_track_point(baseline_by_frame[frame])
    ]
    baseline_visible = sum(1 for frame in baseline_tracked_frames if _track_in_crop(baseline_by_frame[frame]))
    candidate_visible = sum(
        1
        for frame in baseline_tracked_frames
        if frame in candidate_by_frame and _track_in_crop(candidate_by_frame[frame])
    )
    denominator = len(baseline_tracked_frames)
    candidate_tracked = sum(
        1
        for frame in baseline_tracked_frames
        if frame in candidate_by_frame and _has_track_point(candidate_by_frame[frame])
    )
    return {
        "matched_frame_count": denominator,
        "baseline_tracked_frame_count": denominator,
        "candidate_tracked_frame_count": candidate_tracked,
        "candidate_missing_track_frames": max(0, denominator - candidate_tracked),
        "baseline_visible_frames": baseline_visible,
        "candidate_visible_frames": candidate_visible,
        "baseline_coverage": round(baseline_visible / denominator, 6) if denominator else 0.0,
        "candidate_coverage": round(candidate_visible / denominator, 6) if denominator else 0.0,
    }


def _read_camera_path_rows(path: Path) -> list[dict[str, Any]] | None:
    if not path.exists():
        return None
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except (OSError, csv.Error, UnicodeDecodeError):
        return None


def _raw_pan_steps(rows: list[dict[str, Any]]) -> list[float]:
    steps: list[float] = []
    previous: dict[str, Any] | None = None
    for row in rows:
        frame = _optional_int(row.get("Frame"))
        center_x = _optional_float(row.get("CenterX"))
        center_y = _optional_float(row.get("CenterY"))
        if frame is None or center_x is None or center_y is None:
            previous = None
            continue
        if previous is not None:
            previous_frame = _optional_int(previous.get("Frame"))
            previous_x = _optional_float(previous.get("CenterX"))
            previous_y = _optional_float(previous.get("CenterY"))
            if previous_frame is not None and previous_x is not None and previous_y is not None:
                frame_delta = max(1, frame - previous_frame)
                steps.append(math.hypot(center_x - previous_x, center_y - previous_y) / frame_delta)
        previous = row
    return steps


def _rows_by_frame(rows: list[dict[str, Any]] | None) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for row in rows or []:
        frame = _optional_int(row.get("Frame"))
        if frame is not None:
            result[frame] = row
    return result


def _has_track_point(row: dict[str, Any]) -> bool:
    return _optional_float(row.get("TrackX")) is not None and _optional_float(row.get("TrackY")) is not None


def _track_in_crop(row: dict[str, Any]) -> bool:
    x = _optional_float(row.get("TrackX"))
    y = _optional_float(row.get("TrackY"))
    x1 = _optional_float(row.get("CropX1"))
    y1 = _optional_float(row.get("CropY1"))
    x2 = _optional_float(row.get("CropX2"))
    y2 = _optional_float(row.get("CropY2"))
    if None in (x, y, x1, y1, x2, y2):
        return False
    return bool(x1 <= x <= x2 and y1 <= y <= y2)


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _safe_candidate_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("candidate_id must be a non-empty string")
    candidate_id = value.strip()
    path = Path(candidate_id)
    if (
        candidate_id != value
        or candidate_id in {".", ".."}
        or path.name != candidate_id
        or any(separator in candidate_id for separator in ("/", "\\"))
        or ":" in candidate_id
        or ".." in candidate_id
        or candidate_id.rstrip(" .") != candidate_id
        or any(ord(character) < 32 for character in candidate_id)
    ):
        raise ValueError("candidate_id must be a safe identifier")
    return candidate_id


def _approval_id(approval: dict[str, Any] | None) -> str | None:
    if not isinstance(approval, dict):
        return None
    value = approval.get("approval_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _consumed_approval_ids(approval: dict[str, Any] | None) -> list[str]:
    approval_id = _approval_id(approval)
    return [approval_id] if approval_id is not None else []


def _parent_dir(path: Path) -> str | None:
    parent = Path(path).parent
    return None if str(parent) == "." else str(parent)


def _percentile95(values: list[float]) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    index = max(0, math.ceil(len(sorted_values) * 0.95) - 1)
    return round(float(sorted_values[index]), 6)


def _decrease_ratio(baseline: float, candidate: float) -> float:
    if baseline <= 0:
        return 0.0
    return round((baseline - candidate) / baseline, 6)


def _ratio(value: float, baseline: float) -> float:
    if baseline <= 0:
        return 0.0
    return round(value / baseline, 6)


def _regression_limit(baseline: float) -> float:
    if baseline <= 0:
        return 0.0
    return baseline * (1.0 + MAX_CAMERA_REGRESSION_RATIO)


def _int(value: Any) -> int:
    parsed = _optional_int(value)
    return 0 if parsed is None else parsed


def _float(value: Any) -> float:
    parsed = _optional_float(value)
    return 0.0 if parsed is None else float(parsed)


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None
