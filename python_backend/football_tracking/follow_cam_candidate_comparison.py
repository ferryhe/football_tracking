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
MAX_COVERAGE_DROP = 0.05
ZOOM_OUT_ONLY_MEAN_CROP_RATIO = 1.20
MIN_RAW_PAN_IMPROVEMENT_RATIO = 0.10
MIN_MOTION_IMPROVEMENT_RATIO = 0.20


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
        _motion_improvement_check(metrics),
        _zoom_step_check(metrics),
        _not_zoom_out_only_check(metrics),
        _ball_crop_coverage_check(metrics),
    ]


def _comparison_metrics(
    *,
    baseline_audit: dict[str, Any] | None,
    candidate_audit: dict[str, Any] | None,
    baseline_rows: list[dict[str, Any]] | None,
    candidate_rows: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    baseline_summary = _audit_summary(baseline_audit)
    candidate_summary = _audit_summary(candidate_audit)
    baseline_path = _path_metrics(baseline_rows)
    candidate_path = _path_metrics(candidate_rows)
    coverage = _coverage_metrics(baseline_rows, candidate_rows)
    return {
        "baseline": {**baseline_summary, **baseline_path},
        "candidate": {**candidate_summary, **candidate_path},
        "ball_crop_coverage": coverage,
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
