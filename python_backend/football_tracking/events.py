from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
EVENT_CANDIDATES_NAME = "event_candidates.json"
MIN_SEGMENT_FRAMES = 3
MIN_DETECTED_FRAMES = 2
SPEED_BURST_MIN_PX_PER_FRAME = 32.0
GOAL_ZONE_EDGE_RATIO = 0.08
GOAL_ZONE_MIN_TRACK_SPAN_PX = 300.0
DEFAULT_PRE_ROLL_FRAMES = 15
DEFAULT_POST_ROLL_FRAMES = 30
VALID_TRACK_STATUSES = {"Detected", "Predicted"}


def build_event_candidate_report(output_dir: Path) -> dict[str, Any]:
    csv_path, source_name = _resolve_track_csv(output_dir)
    if csv_path is None:
        return _empty_report()

    rows = _read_track_rows(csv_path)
    valid_rows = [row for row in rows if _is_valid_track_row(row)]
    candidates = _build_candidates(source_name, valid_rows)
    counts_by_type: dict[str, int] = {}
    for candidate in candidates:
        event_type = str(candidate["type"])
        counts_by_type[event_type] = counts_by_type.get(event_type, 0) + 1

    frames = [row["frame"] for row in rows if isinstance(row.get("frame"), int)]
    summary = {
        "frame_count": len(rows),
        "detected_frame_count": sum(1 for row in valid_rows if row.get("status") == "Detected"),
        "candidate_count": len(candidates),
        "counts_by_type": dict(sorted(counts_by_type.items())),
        "min_frame": min(frames) if frames else None,
        "max_frame": max(frames) if frames else None,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {"name": source_name, "path": csv_path.name, "row_count": len(rows)},
        "summary": summary,
        "candidates": candidates,
    }


def write_event_candidate_report(output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_event_candidate_report(output_dir)
    (output_dir / EVENT_CANDIDATES_NAME).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def compact_event_candidate_summary(report: dict[str, Any]) -> dict[str, Any] | None:
    summary = report.get("summary")
    source = report.get("source")
    if not isinstance(summary, dict) or not isinstance(source, dict):
        return None

    candidates = report.get("candidates")
    scores: list[float] = []
    start_frames: list[int] = []
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            score = candidate.get("score")
            if isinstance(score, (int, float)):
                scores.append(float(score))
            start_frame = candidate.get("start_frame")
            if isinstance(start_frame, int):
                start_frames.append(start_frame)

    return {
        "schema_version": report.get("schema_version"),
        "source_name": source.get("name"),
        "source_path": source.get("path"),
        "frame_count": summary.get("frame_count", 0),
        "detected_frame_count": summary.get("detected_frame_count", 0),
        "candidate_count": summary.get("candidate_count", 0),
        "counts_by_type": summary.get("counts_by_type") or {},
        "max_score": round(max(scores), 4) if scores else None,
        "first_candidate_frame": min(start_frames) if start_frames else None,
    }


def _empty_report() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {"name": "none", "path": None, "row_count": 0},
        "summary": {
            "frame_count": 0,
            "detected_frame_count": 0,
            "candidate_count": 0,
            "counts_by_type": {},
            "min_frame": None,
            "max_frame": None,
        },
        "candidates": [],
    }


def _resolve_track_csv(output_dir: Path) -> tuple[Path | None, str]:
    cleaned_path = output_dir / "ball_track.cleaned.csv"
    if cleaned_path.exists():
        return cleaned_path, "cleaned"
    raw_path = output_dir / "ball_track.csv"
    if raw_path.exists():
        return raw_path, "raw"
    return None, "none"


def _read_track_rows(csv_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for order, row in enumerate(reader):
            rows.append(
                {
                    "order": order,
                    "frame": _parse_int(row.get("Frame")),
                    "x": _parse_float(row.get("X")),
                    "y": _parse_float(row.get("Y")),
                    "confidence": _parse_float(row.get("Confidence")),
                    "status": _normalize_status(row.get("Status")),
                }
            )
    return sorted(
        rows,
        key=lambda row: (
            row["frame"] is None,
            row["order"] if row["frame"] is None else row["frame"],
            row["order"],
        ),
    )


def _build_candidates(source_name: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []

    all_x = [float(row["x"]) for row in rows if row.get("x") is not None]
    min_x = min(all_x) if all_x else 0.0
    max_x = max(all_x) if all_x else 0.0
    x_span = max_x - min_x

    candidates: list[dict[str, Any]] = []
    for segment in _segments(rows):
        candidate = _candidate_from_segment(
            source_name=source_name,
            segment=segment,
            min_x=min_x,
            x_span=x_span,
        )
        if candidate is not None:
            candidates.append(candidate)
    return sorted(candidates, key=lambda item: (item["start_frame"], item["end_frame"], item["type"]))


def _candidate_from_segment(
    *,
    source_name: str,
    segment: list[dict[str, Any]],
    min_x: float,
    x_span: float,
) -> dict[str, Any] | None:
    if len(segment) < MIN_SEGMENT_FRAMES:
        return None
    detected_count = sum(1 for row in segment if row.get("status") == "Detected")
    if detected_count < MIN_DETECTED_FRAMES:
        return None

    speeds = _segment_speeds(segment)
    if not speeds:
        return None
    max_speed = max(item["speed"] for item in speeds)
    if max_speed < SPEED_BURST_MIN_PX_PER_FRAME:
        return None

    start_frame = int(segment[0]["frame"])
    end_frame = int(segment[-1]["frame"])
    confidences = [float(row["confidence"]) for row in segment if row.get("confidence") is not None]
    mean_confidence = _mean(confidences)
    goal_side = _goal_side(segment, min_x=min_x, x_span=x_span)
    event_type = "goal_candidate" if goal_side is not None else "shot_candidate"

    reason = "Sustained ball track contains a speed burst."
    if goal_side is not None:
        reason = f"Sustained ball track contains a speed burst near {goal_side} goal zone."

    score = _candidate_score(max_speed=max_speed, mean_confidence=mean_confidence, goal_side=goal_side)
    return {
        "id": f"{source_name}:{event_type}:{start_frame}-{end_frame}",
        "type": event_type,
        "label": "candidate",
        "start_frame": start_frame,
        "end_frame": end_frame,
        "frame_count": len(segment),
        "score": score,
        "reason": reason,
        "render_window": {
            "start_frame": max(0, start_frame - DEFAULT_PRE_ROLL_FRAMES),
            "end_frame": end_frame + DEFAULT_POST_ROLL_FRAMES,
        },
        "evidence": {
            "track_source": source_name,
            "status_counts": _status_counts(segment),
            "max_speed_px_per_frame": round(max_speed, 2),
            "mean_speed_px_per_frame": round(_mean([item["speed"] for item in speeds]) or 0.0, 2),
            "mean_confidence": round(mean_confidence, 4) if mean_confidence is not None else None,
            "goal_side": goal_side,
            "start_point": _point_payload(segment[0]),
            "end_point": _point_payload(segment[-1]),
        },
    }


def _segments(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    segments: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for row in rows:
        if current and not _is_contiguous(current[-1], row):
            segments.append(current)
            current = []
        current.append(row)
    if current:
        segments.append(current)
    return segments


def _segment_speeds(segment: list[dict[str, Any]]) -> list[dict[str, float]]:
    speeds: list[dict[str, float]] = []
    for previous, current in zip(segment, segment[1:]):
        frame_gap = max(1, int(current["frame"]) - int(previous["frame"]))
        speed = math.dist((float(previous["x"]), float(previous["y"])), (float(current["x"]), float(current["y"]))) / frame_gap
        speeds.append({"frame": float(current["frame"]), "speed": speed})
    return speeds


def _goal_side(segment: list[dict[str, Any]], *, min_x: float, x_span: float) -> str | None:
    if x_span < GOAL_ZONE_MIN_TRACK_SPAN_PX:
        return None
    start_x = float(segment[0]["x"])
    end_x = float(segment[-1]["x"])
    direction = end_x - start_x
    end_ratio = (end_x - min_x) / x_span
    if direction > 0 and end_ratio >= 1.0 - GOAL_ZONE_EDGE_RATIO:
        return "right"
    if direction < 0 and end_ratio <= GOAL_ZONE_EDGE_RATIO:
        return "left"
    return None


def _candidate_score(*, max_speed: float, mean_confidence: float | None, goal_side: str | None) -> float:
    speed_score = min(1.0, max_speed / 120.0)
    confidence_score = 0.0 if mean_confidence is None else max(0.0, min(1.0, mean_confidence))
    goal_bonus = 0.10 if goal_side is not None else 0.0
    return round(min(0.99, 0.45 + speed_score * 0.35 + confidence_score * 0.10 + goal_bonus), 4)


def _status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _is_valid_track_row(row: dict[str, Any]) -> bool:
    return (
        row.get("status") in VALID_TRACK_STATUSES
        and row.get("frame") is not None
        and row.get("x") is not None
        and row.get("y") is not None
    )


def _is_contiguous(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_frame = previous.get("frame")
    current_frame = current.get("frame")
    if not isinstance(previous_frame, int) or not isinstance(current_frame, int):
        return True
    return current_frame == previous_frame + 1


def _point_payload(row: dict[str, Any]) -> dict[str, float]:
    return {"x": round(float(row["x"]), 2), "y": round(float(row["y"]), 2)}


def _parse_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _normalize_status(value: Any) -> str:
    raw = str(value or "").strip()
    normalized = {
        "detected": "Detected",
        "predicted": "Predicted",
        "lost": "Lost",
    }.get(raw.casefold())
    return normalized or raw


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)
