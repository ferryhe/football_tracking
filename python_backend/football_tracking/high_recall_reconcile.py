from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CSV_HEADER = ["Frame", "X", "Y", "Confidence", "Status"]
STATUS_BY_KEY = {"detected": "Detected", "predicted": "Predicted", "lost": "Lost"}
VALID_STATUSES = {"Detected", "Predicted"}
SCHEMA_VERSION = "1.0"


def reconcile_high_recall_window(
    main_rows: list[dict[str, Any]],
    high_recall_rows: list[dict[str, Any]],
    window: dict[str, Any],
    *,
    max_speed_px_per_frame: float = 180.0,
    max_jump_px: float = 260.0,
) -> dict[str, Any]:
    """Accept high-recall rows only when they fill gaps and pass jump gates."""
    start_frame = _parse_int(window.get("start_frame"))
    end_frame = _parse_int(window.get("end_frame"))
    if start_frame is None or end_frame is None:
        return _rejected_result(main_rows, window, "invalid_window")
    if end_frame < start_frame:
        start_frame, end_frame = end_frame, start_frame

    main_by_frame = _rows_by_frame(main_rows)
    high_by_frame = _rows_by_frame(high_recall_rows)
    proposed_by_frame = {frame: dict(row) for frame, row in main_by_frame.items()}
    accepted_frames: list[int] = []

    for frame in range(start_frame, end_frame + 1):
        high_row = high_by_frame.get(frame)
        if high_row is None or not _is_valid_track_row(high_row):
            continue
        main_row = proposed_by_frame.get(frame)
        if main_row is not None and _is_valid_track_row(main_row):
            continue
        proposed_by_frame[frame] = _normalized_csv_row(high_row)
        accepted_frames.append(frame)

    if not accepted_frames:
        return _rejected_result(main_rows, window, "no_continuity_improvement")

    proposed_rows = _rows_from_frame_map(proposed_by_frame)
    violations = _jump_gate_violations(
        proposed_rows,
        touched_frames=set(accepted_frames),
        max_speed_px_per_frame=max_speed_px_per_frame,
        max_jump_px=max_jump_px,
    )
    if violations:
        result = _rejected_result(main_rows, window, "jump_gate_failed")
        result["gate_violations"] = violations
        return result

    return {
        "accepted": True,
        "reason": "accepted",
        "window": _window_clue(window),
        "accepted_frames": accepted_frames,
        "rows": proposed_rows,
        "review_packet_clues": [],
        "gate_violations": [],
    }


def reconcile_high_recall_outputs(
    output_dir: Path,
    windows: list[dict[str, Any]],
    *,
    high_recall_root: Path | None = None,
    csv_name: str = "ball_track.csv",
    max_speed_px_per_frame: float = 180.0,
    max_jump_px: float = 260.0,
) -> dict[str, Any]:
    """Merge accepted high-recall window CSVs into the main output CSV."""
    root = high_recall_root or (output_dir / "high_recall_windows")
    main_csv_path = output_dir / csv_name
    main_rows = read_track_csv(main_csv_path)

    results: list[dict[str, Any]] = []
    current_rows = main_rows
    for index, window in enumerate(windows):
        window_dir = root / f"window_{index:03d}"
        window_csv_path = window_dir / csv_name
        if not window_csv_path.exists():
            result = _rejected_result(current_rows, window, "high_recall_csv_missing")
            result["window_dir"] = str(window_dir)
            results.append(_report_result(result))
            continue
        high_recall_rows = read_track_csv(window_csv_path)
        result = reconcile_high_recall_window(
            current_rows,
            high_recall_rows,
            window,
            max_speed_px_per_frame=max_speed_px_per_frame,
            max_jump_px=max_jump_px,
        )
        if result["accepted"]:
            current_rows = result["rows"]
        result["window_dir"] = str(window_dir)
        results.append(_report_result(result))

    accepted_count = sum(1 for result in results if result.get("accepted"))
    if accepted_count:
        write_track_csv(main_csv_path, current_rows)

    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "settings": {
            "csv_name": csv_name,
            "max_speed_px_per_frame": max_speed_px_per_frame,
            "max_jump_px": max_jump_px,
        },
        "summary": {
            "window_count": len(windows),
            "accepted_count": accepted_count,
            "rejected_count": len(results) - accepted_count,
            "accepted_frame_count": sum(len(result.get("accepted_frames") or []) for result in results),
        },
        "windows": results,
        "review_packet_clues": [
            clue
            for result in results
            for clue in result.get("review_packet_clues", [])
            if isinstance(clue, dict)
        ],
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "reconcile_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def read_track_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        return [
            {field: row.get(field, "") for field in CSV_HEADER}
            for row in reader
        ]


def write_track_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    with temp_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_HEADER)
        writer.writeheader()
        for row in _rows_from_frame_map(_rows_by_frame(rows)):
            writer.writerow({field: row.get(field, "") for field in CSV_HEADER})
    temp_path.replace(path)


def _report_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "accepted": bool(result.get("accepted")),
        "reason": result.get("reason"),
        "window": result.get("window"),
        "window_dir": result.get("window_dir"),
        "accepted_frames": result.get("accepted_frames", []),
        "review_packet_clues": result.get("review_packet_clues", []),
        "gate_violations": result.get("gate_violations", []),
    }


def _rejected_result(rows: list[dict[str, Any]], window: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "accepted": False,
        "reason": reason,
        "window": _window_clue(window),
        "accepted_frames": [],
        "rows": [dict(row) for row in rows],
        "review_packet_clues": [_window_clue(window, rejection_reason=reason)],
        "gate_violations": [],
    }


def _window_clue(window: dict[str, Any], *, rejection_reason: str | None = None) -> dict[str, Any]:
    clue = {
        "start_frame": _parse_int(window.get("start_frame")),
        "end_frame": _parse_int(window.get("end_frame")),
        "reason": str(window.get("reason") or ""),
        "priority": str(window.get("priority") or "none"),
    }
    if rejection_reason is not None:
        clue["rejection_reason"] = rejection_reason
    return clue


def _rows_by_frame(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    by_frame: dict[int, dict[str, Any]] = {}
    for row in rows:
        frame = _parse_int(row.get("Frame"))
        if frame is None:
            continue
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


def _is_valid_track_row(row: dict[str, Any]) -> bool:
    return (
        _normalize_status(row.get("Status")) in VALID_STATUSES
        and _parse_float(row.get("X")) is not None
        and _parse_float(row.get("Y")) is not None
    )


def _normalize_status(value: Any) -> str | None:
    status = str(value or "").strip()
    if not status:
        return None
    return STATUS_BY_KEY.get(status.casefold())


def _jump_gate_violations(
    rows: list[dict[str, Any]],
    *,
    touched_frames: set[int],
    max_speed_px_per_frame: float,
    max_jump_px: float,
) -> list[dict[str, Any]]:
    valid_rows = [row for row in rows if _is_valid_track_row(row)]
    violations: list[dict[str, Any]] = []
    for previous, current in zip(valid_rows, valid_rows[1:]):
        previous_frame = _parse_int(previous.get("Frame"))
        current_frame = _parse_int(current.get("Frame"))
        if previous_frame is None or current_frame is None:
            continue
        if previous_frame not in touched_frames and current_frame not in touched_frames:
            continue
        previous_point = (_parse_float(previous.get("X")), _parse_float(previous.get("Y")))
        current_point = (_parse_float(current.get("X")), _parse_float(current.get("Y")))
        if None in previous_point or None in current_point:
            continue
        frame_gap = max(1, current_frame - previous_frame)
        distance = math.dist(
            (float(previous_point[0]), float(previous_point[1])),
            (float(current_point[0]), float(current_point[1])),
        )
        speed = distance / frame_gap
        if distance > max_jump_px or speed > max_speed_px_per_frame:
            violations.append(
                {
                    "previous_frame": previous_frame,
                    "current_frame": current_frame,
                    "distance_px": round(distance, 4),
                    "speed_px_per_frame": round(speed, 4),
                    "max_jump_px": max_jump_px,
                    "max_speed_px_per_frame": max_speed_px_per_frame,
                }
            )
    return violations


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
        value = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
