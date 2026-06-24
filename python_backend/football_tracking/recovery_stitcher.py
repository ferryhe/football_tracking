from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CSV_HEADER = ["Frame", "X", "Y", "Confidence", "Status"]
STATUS_BY_KEY = {"detected": "Detected", "predicted": "Predicted", "lost": "Lost"}
STITCH_VALID_STATUSES = {"Detected"}
PARENT_VALID_STATUSES = {"Detected", "Predicted"}
SCHEMA_VERSION = "1.0"
REPORT_NAME = "recovery_stitch_report.json"

MIN_STITCH_RUN_FRAMES = 12
ROI_INTERNAL_MAX_STEP_PX = 320.0
MAX_OUTSIDE_ROI_RATIO = 0.05


def build_stitch_metrics(
    parent_rows: list[dict[str, Any]],
    child_rows: list[dict[str, Any]],
    start_frame: int,
    end_frame: int,
    roi: Any,
) -> dict[str, Any]:
    """Measure whether a child localize ROI track is safe to stitch into a bounded window."""
    if end_frame < start_frame:
        start_frame, end_frame = end_frame, start_frame
    frame_count = end_frame - start_frame + 1
    parent_by_frame = _rows_by_frame(parent_rows)
    child_by_frame = _rows_by_frame(child_rows)
    bounds = _roi_bounds(roi)
    blocking_reasons: list[str] = []

    valid_child_frames: list[int] = []
    outside_roi_frames: list[int] = []
    inside_roi_frames: list[int] = []
    if bounds is None:
        blocking_reasons.append("invalid_roi")
    for frame in range(start_frame, end_frame + 1):
        row = child_by_frame.get(frame)
        if not _is_stitch_child_row(row):
            continue
        valid_child_frames.append(frame)
        if bounds is None:
            continue
        x = _parse_float(row.get("X"))
        y = _parse_float(row.get("Y"))
        if x is None or y is None:
            continue
        if _point_inside_roi(float(x), float(y), bounds):
            inside_roi_frames.append(frame)
        else:
            outside_roi_frames.append(frame)

    accepted_frames = inside_roi_frames
    accepted_runs = _contiguous_runs(accepted_frames)
    outside_ratio = len(outside_roi_frames) / len(valid_child_frames) if valid_child_frames else 1.0
    max_step, max_step_pair = _max_internal_step(child_by_frame, valid_child_frames)
    uncovered_frames = [frame for frame in range(start_frame, end_frame + 1) if frame not in set(accepted_frames)]
    parent_lost_frames = [
        frame
        for frame in range(start_frame, end_frame + 1)
        if _normalize_status(parent_by_frame.get(frame, {}).get("Status")) == "Lost"
    ]
    recovered_parent_lost_frames = [frame for frame in parent_lost_frames if frame in set(accepted_frames)]
    replaced_parent_valid_frames = [
        frame
        for frame in accepted_frames
        if _normalize_status(parent_by_frame.get(frame, {}).get("Status")) in PARENT_VALID_STATUSES
    ]

    if len(accepted_frames) < MIN_STITCH_RUN_FRAMES:
        blocking_reasons.append("insufficient_stitch_frames")
    if _longest_run(accepted_runs) < MIN_STITCH_RUN_FRAMES:
        blocking_reasons.append("insufficient_contiguous_stitch_run")
    if outside_ratio > MAX_OUTSIDE_ROI_RATIO:
        blocking_reasons.append("outside_roi_ratio_exceeded")
    if max_step > ROI_INTERNAL_MAX_STEP_PX:
        blocking_reasons.append("roi_internal_step_exceeded")
    if not recovered_parent_lost_frames and not replaced_parent_valid_frames:
        blocking_reasons.append("no_track_improvement")

    status = "pass" if not blocking_reasons else "fail"
    roi_payload = (
        {"left": bounds[0], "top": bounds[1], "right": bounds[2], "bottom": bounds[3]}
        if bounds is not None
        else None
    )
    return {
        "status": status,
        "blocking_reasons": blocking_reasons,
        "required_window": {"start_frame": start_frame, "end_frame": end_frame, "frame_count": frame_count},
        "roi": roi_payload,
        "accepted_frame_count": len(accepted_frames),
        "accepted_frames": accepted_frames,
        "accepted_frame_ranges": [_range(run) for run in accepted_runs],
        "required_window_coverage": {
            "status": "pass" if not uncovered_frames else "fail",
            "uncovered_frame_count": len(uncovered_frames),
            "uncovered_ranges": [_range(run) for run in _contiguous_runs(uncovered_frames)],
        },
        "roi_internal_quality": {
            "status": (
                "pass"
                if bounds is not None
                and len(accepted_frames) >= MIN_STITCH_RUN_FRAMES
                and _longest_run(accepted_runs) >= MIN_STITCH_RUN_FRAMES
                and outside_ratio <= MAX_OUTSIDE_ROI_RATIO
                and max_step <= ROI_INTERNAL_MAX_STEP_PX
                else "fail"
            ),
            "valid_child_frame_count": len(valid_child_frames),
            "inside_roi_frame_count": len(inside_roi_frames),
            "outside_roi_frame_count": len(outside_roi_frames),
            "outside_roi_ratio": round(outside_ratio, 6),
            "outside_roi_ranges": [_range(run) for run in _contiguous_runs(outside_roi_frames)],
            "max_step_px": round(max_step, 4),
            "max_step_frame_pair": max_step_pair,
            "minimum_stitch_run_frames": MIN_STITCH_RUN_FRAMES,
            "max_internal_step_px": ROI_INTERNAL_MAX_STEP_PX,
            "max_outside_roi_ratio": MAX_OUTSIDE_ROI_RATIO,
        },
        "lost_frame_improvement": {
            "status": "pass" if recovered_parent_lost_frames or replaced_parent_valid_frames else "fail",
            "parent_lost_frame_count": len(parent_lost_frames),
            "recovered_parent_lost_frames": len(recovered_parent_lost_frames),
            "recovered_parent_lost_ranges": [_range(run) for run in _contiguous_runs(recovered_parent_lost_frames)],
            "replaced_parent_valid_frame_count": len(replaced_parent_valid_frames),
            "replaced_parent_valid_ranges": [_range(run) for run in _contiguous_runs(replaced_parent_valid_frames)],
        },
        "boundary_transition_warning": _boundary_transition_warning(
            parent_by_frame,
            child_by_frame,
            accepted_frames,
        ),
    }


def stitch_recovery_window(
    parent_track_csv: Path,
    child_track_csv: Path,
    output_csv: Path,
    window: dict[str, Any],
    effective_roi: Any,
) -> dict[str, Any]:
    parent_path = Path(parent_track_csv)
    child_path = Path(child_track_csv)
    output_path = Path(output_csv)
    parent_rows = _read_track_csv(parent_path)
    child_rows = _read_track_csv(child_path)
    start_frame = _parse_int(window.get("start_frame"))
    end_frame = _parse_int(window.get("end_frame"))
    if start_frame is None or end_frame is None:
        metrics = _invalid_window_metrics(window)
    else:
        metrics = build_stitch_metrics(parent_rows, child_rows, start_frame, end_frame, effective_roi)

    parent_by_frame = _rows_by_frame(parent_rows)
    child_by_frame = _rows_by_frame(child_rows)
    accepted_frames = metrics.get("accepted_frames") if metrics.get("status") == "pass" else []
    if isinstance(accepted_frames, list):
        for frame in accepted_frames:
            if isinstance(frame, int) and frame in child_by_frame:
                parent_by_frame[frame] = _normalized_csv_row(child_by_frame[frame])
    _write_track_csv(output_path, _rows_from_frame_map(parent_by_frame))

    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "summary": {
            "status": metrics["status"],
            "approval_id": window.get("approval_id"),
            "approved_action": window.get("approved_action"),
            "start_frame": start_frame,
            "end_frame": end_frame,
            "accepted_frame_count": metrics.get("accepted_frame_count", 0),
        },
        "window": _json_ready(window),
        "source_tracks": {
            "parent": str(parent_path),
            "child": str(child_path),
            "output": str(output_path),
        },
        "metrics": metrics,
    }
    _write_json(output_path.parent / REPORT_NAME, report)
    return report


def is_localize_ball_roi_window(window: dict[str, Any]) -> bool:
    return any(_localize_entry_is_traceable(entry) for entry in _localize_entries(window))


def stitch_localize_recovery_rows(
    parent_rows: list[dict[str, Any]],
    child_rows: list[dict[str, Any]],
    window: dict[str, Any],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if not is_localize_ball_roi_window(window):
        return _rows_from_frame_map(_rows_by_frame(parent_rows)), {
            "status": "skipped",
            "reason": "not_localize_ball_roi",
            "approval_id": window.get("approval_id"),
            "approved_action": window.get("approved_action"),
            "start_frame": _parse_int(window.get("start_frame")),
            "end_frame": _parse_int(window.get("end_frame")),
            "accepted_frames": [],
            "accepted_frame_count": 0,
            "blocking_reasons": [],
            "metrics": {},
        }
    start_frame = _parse_int(window.get("start_frame"))
    end_frame = _parse_int(window.get("end_frame"))
    roi = _window_effective_roi(window)
    if start_frame is None or end_frame is None:
        metrics = _invalid_window_metrics(window)
    elif roi is None:
        metrics = build_stitch_metrics(parent_rows, child_rows, start_frame, end_frame, None)
        if "invalid_roi" not in metrics["blocking_reasons"]:
            metrics["blocking_reasons"].append("invalid_roi")
        metrics["status"] = "fail"
    else:
        metrics = build_stitch_metrics(parent_rows, child_rows, start_frame, end_frame, roi)

    parent_by_frame = _rows_by_frame(parent_rows)
    child_by_frame = _rows_by_frame(child_rows)
    accepted_frames = metrics.get("accepted_frames") if metrics.get("status") == "pass" else []
    if isinstance(accepted_frames, list):
        for frame in accepted_frames:
            if isinstance(frame, int) and frame in child_by_frame:
                parent_by_frame[frame] = _normalized_csv_row(child_by_frame[frame])

    attempt = {
        "status": metrics["status"],
        "reason": "roi_stitch_accepted" if metrics["status"] == "pass" else "roi_stitch_rejected",
        "approval_id": window.get("approval_id"),
        "approved_action": window.get("approved_action"),
        "start_frame": start_frame,
        "end_frame": end_frame,
        "accepted_frames": metrics.get("accepted_frames", []),
        "accepted_frame_count": metrics.get("accepted_frame_count", 0),
        "accepted_frame_ranges": metrics.get("accepted_frame_ranges", []),
        "blocking_reasons": metrics.get("blocking_reasons", []),
        "metrics": metrics,
    }
    boundary = metrics.get("boundary_transition_warning")
    if isinstance(boundary, dict):
        attempt["boundary_transition_warning"] = bool(boundary.get("warnings"))
        attempt["boundary_transition_violations"] = boundary.get("warnings", [])
    metrics["baseline_lost_frames"] = metrics.get("lost_frame_improvement", {}).get("parent_lost_frame_count", 0)
    required = metrics.get("required_window") if isinstance(metrics.get("required_window"), dict) else {}
    frame_count = int(required.get("frame_count") or 0)
    metrics["candidate_lost_frames"] = max(0, frame_count - int(metrics.get("accepted_frame_count") or 0))
    metrics["lost_gap_reduction_frames"] = max(
        0,
        int(metrics.get("baseline_lost_frames") or 0) - int(metrics.get("candidate_lost_frames") or 0),
    )
    metrics["longest_roi_run_frames"] = _longest_run(_contiguous_runs(metrics.get("accepted_frames", [])))
    metrics["outside_roi_ratio"] = metrics.get("roi_internal_quality", {}).get("outside_roi_ratio", 0.0)
    metrics["max_internal_step_px"] = metrics.get("roi_internal_quality", {}).get("max_step_px", 0.0)
    metrics["changed_frame_count"] = int(metrics.get("accepted_frame_count") or 0)
    return _rows_from_frame_map(parent_by_frame), attempt


def write_recovery_stitch_report(output_dir: Path, attempts: list[dict[str, Any]]) -> dict[str, Any]:
    normalized_attempts = [_json_ready(attempt) for attempt in attempts if isinstance(attempt, dict)]
    accepted_attempts = [attempt for attempt in normalized_attempts if attempt.get("status") == "pass"]
    accepted_frame_count = sum(
        int(
            attempt.get("accepted_frame_count")
            or (attempt.get("metrics") if isinstance(attempt.get("metrics"), dict) else {}).get("changed_frame_count")
            or len(attempt.get("accepted_frames") or [])
        )
        for attempt in accepted_attempts
    )
    boundary_warning_count = sum(
        1
        for attempt in normalized_attempts
        if attempt.get("boundary_transition_warning") is True
        or bool(attempt.get("boundary_transition_violations"))
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "summary": {
            "status": "pass" if accepted_attempts and len(accepted_attempts) == len(normalized_attempts) else "fail",
            "attempt_count": len(normalized_attempts),
            "passed_count": len(accepted_attempts),
            "accepted_attempt_count": len(accepted_attempts),
            "accepted_frame_count": accepted_frame_count,
            "changed_frame_count": accepted_frame_count,
            "boundary_transition_warning_count": boundary_warning_count,
        },
        "windows": normalized_attempts,
        "attempts": normalized_attempts,
    }
    _write_json(Path(output_dir) / REPORT_NAME, report)
    return report


def _invalid_window_metrics(window: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "fail",
        "blocking_reasons": ["invalid_window"],
        "required_window": {
            "start_frame": _parse_int(window.get("start_frame")),
            "end_frame": _parse_int(window.get("end_frame")),
            "frame_count": 0,
        },
        "accepted_frame_count": 0,
        "accepted_frames": [],
        "accepted_frame_ranges": [],
        "required_window_coverage": {"status": "fail", "uncovered_frame_count": 0, "uncovered_ranges": []},
        "roi_internal_quality": {"status": "fail"},
        "lost_frame_improvement": {"status": "fail", "recovered_parent_lost_frames": 0},
        "boundary_transition_warning": {"status": "pass", "warnings": []},
    }


def _localize_entries(window: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if window.get("approved_action") == "localize_ball_roi":
        entries.append(window)
    provenance = window.get("approval_provenance")
    if isinstance(provenance, list):
        entries.extend(
            item
            for item in provenance
            if isinstance(item, dict) and item.get("approved_action") == "localize_ball_roi"
        )
    return entries


def _localize_entry_is_traceable(entry: dict[str, Any]) -> bool:
    if _parse_int(entry.get("start_frame")) is None and not isinstance(entry.get("rerun_scope"), dict):
        return False
    if _window_effective_roi(entry) is None:
        return False
    if isinstance(entry.get("approval_id"), str) and entry["approval_id"].strip():
        return _has_provenance(entry)
    return False


def _has_provenance(entry: dict[str, Any]) -> bool:
    for key in ("source_packet_id", "visual_review_id", "visual_localization_id"):
        if isinstance(entry.get(key), str) and entry[key].strip():
            return True
    provenance = entry.get("provenance")
    if isinstance(provenance, dict):
        return _has_provenance(provenance)
    return False


def _window_effective_roi(window: dict[str, Any]) -> Any:
    for key in ("effective_roi", "padded_roi", "approved_roi"):
        value = window.get(key)
        if _roi_bounds(value) is not None:
            return value
    roi = window.get("local_search_roi")
    if _roi_bounds(roi) is not None:
        return roi
    provenance = window.get("approval_provenance")
    if isinstance(provenance, list):
        for item in provenance:
            if not isinstance(item, dict) or item.get("approved_action") != "localize_ball_roi":
                continue
            nested = _window_effective_roi(item)
            if nested is not None:
                return nested
    return None


def _boundary_transition_warning(
    parent_by_frame: dict[int, dict[str, Any]],
    child_by_frame: dict[int, dict[str, Any]],
    accepted_frames: list[int],
) -> dict[str, Any]:
    if not accepted_frames:
        return {"status": "pass", "warnings": []}
    accepted_set = set(accepted_frames)
    first_frame = min(accepted_frames)
    last_frame = max(accepted_frames)
    warnings: list[dict[str, Any]] = []
    previous_frame = _nearest_parent_valid_frame(parent_by_frame, first_frame, accepted_set, direction=-1)
    if previous_frame is not None:
        warning = _transition_warning(
            parent_by_frame[previous_frame],
            child_by_frame[first_frame],
            "left_boundary",
        )
        if warning is not None:
            warnings.append(warning)
    next_frame = _nearest_parent_valid_frame(parent_by_frame, last_frame, accepted_set, direction=1)
    if next_frame is not None:
        warning = _transition_warning(
            child_by_frame[last_frame],
            parent_by_frame[next_frame],
            "right_boundary",
        )
        if warning is not None:
            warnings.append(warning)
    return {
        "status": "warn" if warnings else "pass",
        "warnings": warnings,
    }


def _nearest_parent_valid_frame(
    parent_by_frame: dict[int, dict[str, Any]],
    frame: int,
    accepted_frames: set[int],
    *,
    direction: int,
) -> int | None:
    frames = sorted(parent_by_frame)
    candidates = reversed(frames) if direction < 0 else frames
    for candidate in candidates:
        if direction < 0 and candidate >= frame:
            continue
        if direction > 0 and candidate <= frame:
            continue
        if candidate in accepted_frames:
            continue
        if _normalize_status(parent_by_frame[candidate].get("Status")) in PARENT_VALID_STATUSES:
            return candidate
    return None


def _transition_warning(previous: dict[str, Any], current: dict[str, Any], boundary: str) -> dict[str, Any] | None:
    previous_frame = _parse_int(previous.get("Frame"))
    current_frame = _parse_int(current.get("Frame"))
    previous_point = (_parse_float(previous.get("X")), _parse_float(previous.get("Y")))
    current_point = (_parse_float(current.get("X")), _parse_float(current.get("Y")))
    if previous_frame is None or current_frame is None or None in previous_point or None in current_point:
        return None
    distance = math.dist(
        (float(previous_point[0]), float(previous_point[1])),
        (float(current_point[0]), float(current_point[1])),
    )
    if distance <= ROI_INTERNAL_MAX_STEP_PX:
        return None
    return {
        "boundary": boundary,
        "previous_frame": previous_frame,
        "current_frame": current_frame,
        "distance_px": round(distance, 4),
        "max_internal_step_px": ROI_INTERNAL_MAX_STEP_PX,
        "reason": "stitch boundary transition exceeds ROI internal step gate",
    }


def _max_internal_step(
    rows_by_frame: dict[int, dict[str, Any]],
    frames: list[int],
) -> tuple[float, dict[str, int] | None]:
    max_step = 0.0
    max_pair: dict[str, int] | None = None
    for previous_frame, current_frame in zip(frames, frames[1:]):
        previous = rows_by_frame[previous_frame]
        current = rows_by_frame[current_frame]
        previous_point = (_parse_float(previous.get("X")), _parse_float(previous.get("Y")))
        current_point = (_parse_float(current.get("X")), _parse_float(current.get("Y")))
        if None in previous_point or None in current_point:
            continue
        distance = math.dist(
            (float(previous_point[0]), float(previous_point[1])),
            (float(current_point[0]), float(current_point[1])),
        )
        if distance > max_step:
            max_step = distance
            max_pair = {"previous_frame": previous_frame, "current_frame": current_frame}
    return max_step, max_pair


def _read_track_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        return [{field: row.get(field, "") for field in CSV_HEADER} for row in reader]


def _write_track_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    with temp_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_HEADER)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in CSV_HEADER})
    temp_path.replace(path)


def _rows_by_frame(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    by_frame: dict[int, dict[str, Any]] = {}
    for row in rows:
        frame = _parse_int(row.get("Frame"))
        if frame is not None:
            by_frame[frame] = dict(row)
    return by_frame


def _rows_from_frame_map(rows_by_frame: dict[int, dict[str, Any]]) -> list[dict[str, str]]:
    return [_csv_row(rows_by_frame[frame]) for frame in sorted(rows_by_frame)]


def _csv_row(row: dict[str, Any]) -> dict[str, str]:
    return {field: "" if row.get(field) is None else str(row.get(field, "")) for field in CSV_HEADER}


def _normalized_csv_row(row: dict[str, Any]) -> dict[str, str]:
    frame = _parse_int(row.get("Frame"))
    x = _parse_float(row.get("X"))
    y = _parse_float(row.get("Y"))
    confidence = _parse_float(row.get("Confidence"))
    status = _normalize_status(row.get("Status")) or str(row.get("Status") or "")
    return {
        "Frame": "" if frame is None else str(frame),
        "X": "" if x is None else str(float(x)),
        "Y": "" if y is None else str(float(y)),
        "Confidence": "" if confidence is None else str(float(confidence)),
        "Status": status,
    }


def _is_stitch_child_row(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    return (
        _normalize_status(row.get("Status")) in STITCH_VALID_STATUSES
        and _parse_float(row.get("X")) is not None
        and _parse_float(row.get("Y")) is not None
    )


def _normalize_status(value: Any) -> str | None:
    status = str(value or "").strip()
    if not status:
        return None
    return STATUS_BY_KEY.get(status.casefold())


def _roi_bounds(value: Any) -> tuple[float, float, float, float] | None:
    if isinstance(value, list) and len(value) == 4:
        parsed = [_parse_float(item) for item in value]
        if any(item is None for item in parsed):
            return None
        left, top, right, bottom = [float(item) for item in parsed if item is not None]
    elif isinstance(value, dict):
        left = _parse_float(value.get("left"))
        top = _parse_float(value.get("top"))
        right = _parse_float(value.get("right"))
        bottom = _parse_float(value.get("bottom"))
        if None in (left, top, right, bottom):
            x = _parse_float(value.get("x"))
            y = _parse_float(value.get("y"))
            width = _parse_float(value.get("width"))
            height = _parse_float(value.get("height"))
            if x is None or y is None or width is None or height is None:
                return None
            left, top, right, bottom = x, y, x + width, y + height
        left, top, right, bottom = float(left), float(top), float(right), float(bottom)
    else:
        return None
    if right < left or bottom < top:
        return None
    return left, top, right, bottom


def _point_inside_roi(x: float, y: float, bounds: tuple[float, float, float, float]) -> bool:
    left, top, right, bottom = bounds
    return left <= x <= right and top <= y <= bottom


def _contiguous_runs(frames: list[int]) -> list[list[int]]:
    if not frames:
        return []
    runs: list[list[int]] = []
    current = [frames[0]]
    for frame in frames[1:]:
        if frame == current[-1] + 1:
            current.append(frame)
        else:
            runs.append(current)
            current = [frame]
    runs.append(current)
    return runs


def _range(run: list[int]) -> dict[str, int]:
    return {"start_frame": run[0], "end_frame": run[-1], "frame_count": len(run)}


def _longest_run(runs: list[list[int]]) -> int:
    return max((len(run) for run in runs), default=0)


def _parse_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (OverflowError, TypeError, ValueError):
        return None


def _parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(_json_ready(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _json_ready(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
