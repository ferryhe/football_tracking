from __future__ import annotations

import csv
import json
import math
import os
import sqlite3
import tempfile
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
LOW_MOTION_CONFIDENCE = 0.5

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
    shot_id: str | None
    cut_detected: bool
    motion_confidence: float | None


@dataclass(slots=True)
class CameraMotionStep:
    segment: int
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


def write_streaming_camera_motion_audit_report(
    output_dir: Path,
    *,
    target_width: int = 1920,
    target_height: int = 1080,
    camera_path_name: str = "camera_path.csv",
    report_name: str = "camera_motion_audit.json",
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Audit a long camera path with bounded RAM and an exact disk-backed p95."""

    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / camera_path_name
    report_path = output_dir / report_name
    try:
        with tempfile.TemporaryDirectory(prefix="football-tracking-camera-audit-") as temp_name:
            database = sqlite3.connect(str(Path(temp_name) / "audit.sqlite3"))
            try:
                database.execute("PRAGMA journal_mode=OFF")
                database.execute("PRAGMA synchronous=OFF")
                database.execute("CREATE TABLE pan_steps (value REAL NOT NULL)")
                database.execute(
                    "CREATE TABLE review_events (start_frame INTEGER NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL)"
                )
                summary = _stream_camera_path_audit(
                    csv_path,
                    database,
                    target_width=target_width,
                    target_height=target_height,
                )
                database.commit()
                payload = {
                    "schema_version": SCHEMA_VERSION,
                    "generated_at": generated_at,
                    "summary": summary,
                }
                _write_streaming_audit_json(report_path, payload, database)
                return payload
            finally:
                database.close()
    except CameraMotionAuditUnavailable as exc:
        payload = _build_unavailable_report(exc.reason)
        payload["generated_at"] = generated_at
        _write_json_fsync(report_path, payload)
        return payload


def _stream_camera_path_audit(
    csv_path: Path,
    database: sqlite3.Connection,
    *,
    target_width: int,
    target_height: int,
) -> dict[str, Any]:
    if not csv_path.exists():
        raise CameraMotionAuditUnavailable(f"{csv_path.name} not found")
    try:
        handle = csv_path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise CameraMotionAuditUnavailable(f"{csv_path.name} could not be read") from exc

    frame_count = 0
    cut_count = 0
    low_confidence_count = 0
    max_pan_step = 0.0
    max_pan_accel = 0.0
    max_zoom_step = 0.0
    max_zoom_ratio = 0.0
    previous: CameraMotionRow | None = None
    previous_velocity: tuple[float, float] | None = None
    pending_events: dict[str, dict[str, Any]] = {}
    event_count = 0
    has_warn = False
    has_fail = False
    pan_batch: list[tuple[float]] = []
    try:
        with handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise CameraMotionAuditUnavailable(f"{csv_path.name} is empty")
            if missing_columns := REQUIRED_COLUMNS.difference(reader.fieldnames):
                missing = ", ".join(sorted(missing_columns))
                raise CameraMotionAuditUnavailable(f"{csv_path.name} missing required columns: {missing}")
            for index, raw in enumerate(reader):
                try:
                    current = _parse_row(index, raw)
                except ValueError as exc:
                    raise CameraMotionAuditUnavailable(f"{csv_path.name} contains invalid numeric data") from exc
                if previous is not None and current.frame <= previous.frame:
                    raise CameraMotionAuditUnavailable("camera_path.csv contains duplicate or unordered frame indices")
                frame_count += 1
                if current.motion_confidence is not None and current.motion_confidence < LOW_MOTION_CONFIDENCE:
                    low_confidence_count += 1
                if previous is None:
                    previous = current
                    continue
                if _is_cut_boundary(previous, current):
                    cut_count += 1
                    event_count += _flush_pending_events(database, pending_events)
                    pending_events.clear()
                    previous_velocity = None
                    previous = current
                    continue

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
                step = CameraMotionStep(
                    segment=cut_count,
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
                max_pan_step = max(max_pan_step, step_px)
                if accel_px is not None:
                    max_pan_accel = max(max_pan_accel, accel_px)
                max_zoom_step = max(max_zoom_step, zoom_step_px)
                max_zoom_ratio = max(max_zoom_ratio, zoom_step_ratio)
                pan_batch.append((step_px,))
                if len(pan_batch) >= 1024:
                    database.executemany("INSERT INTO pan_steps(value) VALUES (?)", pan_batch)
                    pan_batch.clear()
                for event_type in (
                    "camera_motion_spike",
                    "camera_acceleration_spike",
                    "camera_zoom_jump",
                ):
                    event = _event_for_step(step, event_type)
                    if event is None:
                        continue
                    has_warn = True
                    if event["severity"] == "fail":
                        has_fail = True
                    pending = pending_events.get(event_type)
                    if pending is None or event["start_frame"] - pending["end_frame"] - 1 > 1:
                        if pending is not None:
                            _insert_stream_event(database, pending)
                            event_count += 1
                        pending_events[event_type] = event
                    else:
                        _merge_event_into(pending, event)
                previous = current
    except CameraMotionAuditUnavailable:
        raise
    except (csv.Error, OSError) as exc:
        raise CameraMotionAuditUnavailable(f"{csv_path.name} could not be read") from exc

    if frame_count == 0:
        raise CameraMotionAuditUnavailable(f"{csv_path.name} is empty")
    if pan_batch:
        database.executemany("INSERT INTO pan_steps(value) VALUES (?)", pan_batch)
    event_count += _flush_pending_events(database, pending_events)
    step_count = int(database.execute("SELECT COUNT(*) FROM pan_steps").fetchone()[0])
    p95 = 0.0
    if step_count:
        rank = max(0, math.ceil(step_count * 0.95) - 1)
        row = database.execute("SELECT value FROM pan_steps ORDER BY value LIMIT 1 OFFSET ?", (rank,)).fetchone()
        p95 = 0.0 if row is None else float(row[0])
    return {
        "status": "fail" if has_fail else "warn" if has_warn else "ok",
        "frame_count": frame_count,
        "max_pan_step_px": _round_metric(max_pan_step),
        "p95_pan_step_px": _round_metric(p95),
        "max_pan_accel_px": _round_metric(max_pan_accel),
        "max_zoom_step_px": _round_metric(max_zoom_step),
        "max_zoom_step_ratio": _round_metric(max_zoom_ratio),
        "review_event_count": event_count,
        "cut_count": cut_count,
        "continuous_segment_count": cut_count + 1,
        "low_confidence_motion_frame_count": low_confidence_count,
    }


def _insert_stream_event(database: sqlite3.Connection, event: dict[str, Any]) -> None:
    database.execute(
        "INSERT INTO review_events(start_frame, type, payload) VALUES (?, ?, ?)",
        (
            int(event["start_frame"]),
            str(event["type"]),
            json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False),
        ),
    )


def _flush_pending_events(
    database: sqlite3.Connection,
    pending_events: dict[str, dict[str, Any]],
) -> int:
    for event in pending_events.values():
        _insert_stream_event(database, event)
    return len(pending_events)


def _write_streaming_audit_json(
    path: Path,
    payload: dict[str, Any],
    database: sqlite3.Connection,
) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("{")
        handle.write('"generated_at":')
        handle.write(json.dumps(payload["generated_at"], ensure_ascii=False, separators=(",", ":")))
        handle.write(',"review_events":[')
        first = True
        cursor = database.execute("SELECT payload FROM review_events ORDER BY start_frame, type")
        for (event_json,) in cursor:
            if not first:
                handle.write(",")
            first = False
            handle.write(str(event_json))
        handle.write('],"schema_version":')
        handle.write(json.dumps(payload["schema_version"], ensure_ascii=False))
        handle.write(',"summary":')
        handle.write(
            json.dumps(payload["summary"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        )
        handle.write("}\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_json_fsync(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


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
    cut_detected = _parse_optional_bool(row.get("CutDetected"))
    motion_confidence = _parse_optional_confidence(row.get("MotionConfidence"))
    return CameraMotionRow(
        order=index,
        frame=frame,
        center_x=center_x,
        center_y=center_y,
        crop_width=crop_width,
        crop_height=crop_height,
        status=row.get("Status") or "",
        pan_mode=row.get("PanMode") or "",
        shot_id=_parse_optional_text(row.get("ShotId")),
        cut_detected=cut_detected,
        motion_confidence=motion_confidence,
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
    cut_count = sum(1 for previous, current in zip(rows, rows[1:]) if _is_cut_boundary(previous, current))
    summary = {
        "status": status,
        "frame_count": len(rows),
        "max_pan_step_px": _round_metric(max(step_values) if step_values else 0.0),
        "p95_pan_step_px": _round_metric(_percentile95(step_values)),
        "max_pan_accel_px": _round_metric(max(accel_values) if accel_values else 0.0),
        "max_zoom_step_px": _round_metric(max(zoom_step_values) if zoom_step_values else 0.0),
        "max_zoom_step_ratio": _round_metric(max(zoom_ratio_values) if zoom_ratio_values else 0.0),
        "review_event_count": len(review_events),
        "cut_count": cut_count,
        "continuous_segment_count": cut_count + 1,
        "low_confidence_motion_frame_count": sum(
            1 for row in rows if row.motion_confidence is not None and row.motion_confidence < LOW_MOTION_CONFIDENCE
        ),
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
            "cut_count": 0,
            "continuous_segment_count": 0,
            "low_confidence_motion_frame_count": 0,
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
    segment = 0
    for previous, current in zip(rows, rows[1:]):
        if _is_cut_boundary(previous, current):
            segment += 1
            previous_velocity = None
            continue
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
                segment=segment,
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
        segment_events: dict[int, list[dict[str, Any]]] = {}
        for step in steps:
            event = _event_for_step(step, event_type)
            if event is not None:
                segment_events.setdefault(step.segment, []).append(event)
        for events in segment_events.values():
            grouped_events.extend(_merge_adjacent_events(events))
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


def _parse_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_optional_bool(value: Any) -> bool:
    if value is None or not str(value).strip():
        return False
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y"}:
        return True
    if normalized in {"0", "false", "no", "n"}:
        return False
    raise ValueError("invalid CutDetected value")


def _parse_optional_confidence(value: Any) -> float | None:
    if value is None or not str(value).strip():
        return None
    confidence = _parse_float(value)
    if confidence is None or not 0.0 <= confidence <= 1.0:
        raise ValueError("invalid MotionConfidence value")
    return confidence


def _is_cut_boundary(previous: CameraMotionRow, current: CameraMotionRow) -> bool:
    if current.cut_detected:
        return True
    return previous.shot_id is not None and current.shot_id is not None and previous.shot_id != current.shot_id


def _round_metric(value: float) -> float:
    return round(float(value), 9)


def _validate_strictly_increasing_frames(rows: list[CameraMotionRow]) -> None:
    previous_frame: int | None = None
    for row in rows:
        if previous_frame is not None and row.frame <= previous_frame:
            raise CameraMotionAuditUnavailable("camera_path.csv contains invalid numeric data")
        previous_frame = row.frame
