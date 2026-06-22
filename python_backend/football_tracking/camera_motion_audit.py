from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
PAN_STEP_WARN_PX = 90.0
PAN_STEP_FAIL_PX = 140.0
PAN_ACCEL_WARN_PX = 80.0
PAN_ACCEL_FAIL_PX = 140.0
ZOOM_STEP_WARN_PX = 24.0
ZOOM_STEP_FAIL_PX = 48.0

REQUIRED_COLUMNS = {
    "Frame",
    "CenterX",
    "CenterY",
    "CropWidth",
    "CropHeight",
    "Status",
    "PanMode",
}


@dataclass(slots=True)
class CameraMotionRow:
    order: int
    frame: int
    center_x: float
    center_y: float
    crop_width: float
    crop_height: float
    status: str
    pan_mode: str


@dataclass(slots=True)
class CameraMotionStep:
    frame: int
    frame_delta: int
    step_px: float
    vx: float
    vy: float
    accel_px: float | None
    zoom_step_px: float
    zoom_step_ratio: float
    status: str
    pan_mode: str


class CameraMotionAuditUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def write_camera_motion_audit_report(
    output_dir: Path,
    *,
    target_width: int = 1920,
    target_height: int = 1080,
    camera_path_name: str = "camera_path.csv",
    report_name: str = "camera_motion_audit.json",
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / camera_path_name
    try:
        rows = _read_camera_path_rows(csv_path)
        payload = _build_available_report(rows, target_width=target_width, target_height=target_height)
    except CameraMotionAuditUnavailable as exc:
        payload = _build_unavailable_report(exc.reason)

    (output_dir / report_name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def _read_camera_path_rows(csv_path: Path) -> list[CameraMotionRow]:
    if not csv_path.exists():
        raise CameraMotionAuditUnavailable(f"{csv_path.name} not found")
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise CameraMotionAuditUnavailable(f"{csv_path.name} is empty")
            if missing_columns := REQUIRED_COLUMNS.difference(reader.fieldnames):
                missing = ", ".join(sorted(missing_columns))
                raise CameraMotionAuditUnavailable(f"{csv_path.name} missing required columns: {missing}")
            rows = [_parse_row(index, row) for index, row in enumerate(reader)]
    except CameraMotionAuditUnavailable:
        raise
    except (csv.Error, ValueError) as exc:
        raise CameraMotionAuditUnavailable(f"{csv_path.name} contains invalid numeric data") from exc
    except OSError as exc:
        raise CameraMotionAuditUnavailable(f"{csv_path.name} could not be read") from exc
    if not rows:
        raise CameraMotionAuditUnavailable(f"{csv_path.name} is empty")
    _validate_strictly_increasing_frames(rows)
    return sorted(rows, key=lambda row: (row.frame, row.order))


def _parse_row(index: int, row: dict[str, str]) -> CameraMotionRow:
    frame = _parse_int(row.get("Frame"))
    center_x = _parse_float(row.get("CenterX"))
    center_y = _parse_float(row.get("CenterY"))
    crop_width = _parse_float(row.get("CropWidth"))
    crop_height = _parse_float(row.get("CropHeight"))
    if (
        frame is None
        or center_x is None
        or center_y is None
        or crop_width is None
        or crop_width <= 0
        or crop_height is None
        or crop_height <= 0
    ):
        raise ValueError("invalid camera path row")
    return CameraMotionRow(
        order=index,
        frame=frame,
        center_x=center_x,
        center_y=center_y,
        crop_width=crop_width,
        crop_height=crop_height,
        status=row.get("Status") or "",
        pan_mode=row.get("PanMode") or "",
    )


def _build_available_report(
    rows: list[CameraMotionRow],
    *,
    target_width: int,
    target_height: int,
) -> dict[str, Any]:
    steps = _motion_steps(rows, target_width=target_width, target_height=target_height)
    review_events = _build_review_events(steps)
    status = _summary_status(review_events)
    step_values = [step.step_px for step in steps]
    accel_values = [step.accel_px for step in steps if step.accel_px is not None]
    zoom_step_values = [step.zoom_step_px for step in steps]
    zoom_ratio_values = [step.zoom_step_ratio for step in steps]
    summary = {
        "status": status,
        "frame_count": len(rows),
        "max_pan_step_px": _round_metric(max(step_values) if step_values else 0.0),
        "p95_pan_step_px": _round_metric(_percentile95(step_values)),
        "max_pan_accel_px": _round_metric(max(accel_values) if accel_values else 0.0),
        "max_zoom_step_px": _round_metric(max(zoom_step_values) if zoom_step_values else 0.0),
        "max_zoom_step_ratio": _round_metric(max(zoom_ratio_values) if zoom_ratio_values else 0.0),
        "review_event_count": len(review_events),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "review_events": review_events,
    }


def _build_unavailable_report(reason: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "status": "unavailable",
            "reason": reason,
            "frame_count": 0,
            "max_pan_step_px": 0.0,
            "p95_pan_step_px": 0.0,
            "max_pan_accel_px": 0.0,
            "max_zoom_step_px": 0.0,
            "max_zoom_step_ratio": 0.0,
            "review_event_count": 0,
        },
        "review_events": [],
    }


def _motion_steps(
    rows: list[CameraMotionRow],
    *,
    target_width: int,
    target_height: int,
) -> list[CameraMotionStep]:
    steps: list[CameraMotionStep] = []
    previous_velocity: tuple[float, float] | None = None
    for previous, current in zip(rows, rows[1:]):
        frame_delta = max(1, current.frame - previous.frame)
        scale_x = target_width / max(1.0, current.crop_width)
        scale_y = target_height / max(1.0, current.crop_height)
        vx = (current.center_x - previous.center_x) * scale_x / frame_delta
        vy = (current.center_y - previous.center_y) * scale_y / frame_delta
        step_px = math.hypot(vx, vy)
        accel_px = None
        if previous_velocity is not None:
            accel_px = math.hypot(vx - previous_velocity[0], vy - previous_velocity[1])
        previous_velocity = (vx, vy)
        zoom_step_px = abs(current.crop_height - previous.crop_height) / frame_delta
        zoom_step_ratio = zoom_step_px / max(1.0, previous.crop_height)
        steps.append(
            CameraMotionStep(
                frame=current.frame,
                frame_delta=frame_delta,
                step_px=step_px,
                vx=vx,
                vy=vy,
                accel_px=accel_px,
                zoom_step_px=zoom_step_px,
                zoom_step_ratio=zoom_step_ratio,
                status=current.status,
                pan_mode=current.pan_mode,
            )
        )
    return steps


def _build_review_events(steps: list[CameraMotionStep]) -> list[dict[str, Any]]:
    grouped_events: list[dict[str, Any]] = []
    for event_type in ("camera_motion_spike", "camera_acceleration_spike", "camera_zoom_jump"):
        events = [_event_for_step(step, event_type) for step in steps]
        grouped_events.extend(_merge_adjacent_events([event for event in events if event is not None]))
    return sorted(grouped_events, key=lambda event: (event["start_frame"], event["type"]))


def _event_for_step(step: CameraMotionStep, event_type: str) -> dict[str, Any] | None:
    if event_type == "camera_motion_spike":
        if step.step_px < PAN_STEP_WARN_PX:
            return None
        severity = "fail" if step.step_px >= PAN_STEP_FAIL_PX else "warn"
        reason = "Output-space camera pan step exceeds the review threshold."
    elif event_type == "camera_acceleration_spike":
        if step.accel_px is None or step.accel_px < PAN_ACCEL_WARN_PX:
            return None
        severity = "fail" if step.accel_px >= PAN_ACCEL_FAIL_PX else "warn"
        reason = "Output-space camera velocity changes abruptly between adjacent steps."
    elif event_type == "camera_zoom_jump":
        if step.zoom_step_px < ZOOM_STEP_WARN_PX:
            return None
        severity = "fail" if step.zoom_step_px >= ZOOM_STEP_FAIL_PX else "warn"
        reason = "Camera crop height changes abruptly between adjacent frames."
    else:
        return None

    return {
        "type": event_type,
        "severity": severity,
        "start_frame": step.frame,
        "end_frame": step.frame,
        "frame_count": 1,
        "reason": reason,
        "evidence": _evidence_for_step(step),
    }


def _evidence_for_step(step: CameraMotionStep) -> dict[str, Any]:
    return {
        "max_step_px": _round_metric(step.step_px),
        "max_accel_px": _round_metric(step.accel_px or 0.0),
        "max_zoom_step_px": _round_metric(step.zoom_step_px),
        "max_zoom_step_ratio": _round_metric(step.zoom_step_ratio),
        "pan_modes": [step.pan_mode],
        "statuses": [step.status],
    }


def _merge_adjacent_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for event in events:
        frame_gap = event["start_frame"] - merged[-1]["end_frame"] - 1 if merged else 0
        if not merged or frame_gap > 1:
            merged.append(event)
            continue
        _merge_event_into(merged[-1], event)
    return merged


def _merge_event_into(target: dict[str, Any], event: dict[str, Any]) -> None:
    target["end_frame"] = max(target["end_frame"], event["end_frame"])
    target["frame_count"] = target["end_frame"] - target["start_frame"] + 1
    if event["severity"] == "fail":
        target["severity"] = "fail"
    target_evidence = target["evidence"]
    event_evidence = event["evidence"]
    for metric in ("max_step_px", "max_accel_px", "max_zoom_step_px", "max_zoom_step_ratio"):
        target_evidence[metric] = max(target_evidence[metric], event_evidence[metric])
    for field in ("pan_modes", "statuses"):
        for value in event_evidence[field]:
            if value not in target_evidence[field]:
                target_evidence[field].append(value)


def _summary_status(events: list[dict[str, Any]]) -> str:
    severities = {event["severity"] for event in events}
    if "fail" in severities:
        return "fail"
    if "warn" in severities:
        return "warn"
    return "ok"


def _percentile95(values: list[float]) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    index = max(0, math.ceil(len(sorted_values) * 0.95) - 1)
    return sorted_values[index]


def _parse_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _round_metric(value: float) -> float:
    return round(float(value), 9)


def _validate_strictly_increasing_frames(rows: list[CameraMotionRow]) -> None:
    previous_frame: int | None = None
    for row in rows:
        if previous_frame is not None and row.frame <= previous_frame:
            raise CameraMotionAuditUnavailable("camera_path.csv contains invalid numeric data")
        previous_frame = row.frame
