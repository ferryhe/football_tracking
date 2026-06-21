from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"
SHORT_TRACKLET_MAX_LENGTH = 2
LOW_CONFIDENCE_THRESHOLD = 0.45
LARGE_JUMP_THRESHOLD_PX = 180.0
LOST_GAP_MIN_FRAMES = 3
CANDIDATE_AMBIGUITY_DELTA = 0.08


def build_ball_audit_report(output_dir: Path) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    tracklets: list[dict[str, Any]] = []
    review_events: list[dict[str, Any]] = []
    all_frames: set[int] = set()

    for source_name, file_name in (("raw", "ball_track.csv"), ("cleaned", "ball_track.cleaned.csv")):
        csv_path = output_dir / file_name
        if not csv_path.exists():
            continue
        rows = _read_track_rows(csv_path)
        all_frames.update(row["frame"] for row in rows if row["frame"] is not None)
        source_tracklets, source_events = _build_source_tracklets(source_name, rows)
        sources.append(
            {
                "name": source_name,
                "path": file_name,
                "row_count": len(rows),
                "tracklet_count": len(source_tracklets),
            }
        )
        tracklets.extend(source_tracklets)
        review_events.extend(source_events)

    review_events.extend(_build_debug_events(output_dir / "debug.jsonl"))
    review_events.extend(_build_cleanup_events(output_dir / "cleanup_report.json"))
    review_events.sort(key=_event_sort_key)

    step_values = [
        tracklet["max_step_px"]
        for tracklet in tracklets
        if isinstance(tracklet.get("max_step_px"), (int, float))
    ]
    summary = {
        "frame_count": len(all_frames),
        "source_count": len(sources),
        "tracklet_count": len(tracklets),
        "suspicious_tracklet_count": sum(1 for tracklet in tracklets if tracklet.get("flags")),
        "review_event_count": len(review_events),
        "lost_gap_count": sum(1 for event in review_events if event.get("type") == "lost_gap"),
        "max_step_px": _round_or_none(max(step_values) if step_values else None, 2),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "sources": sources,
        "tracklets": tracklets,
        "review_events": review_events,
    }


def write_ball_audit_report(output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_ball_audit_report(output_dir)
    (output_dir / "ball_audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def compact_ball_audit_summary(report: dict[str, Any]) -> dict[str, Any] | None:
    summary = report.get("summary")
    if not isinstance(summary, dict):
        return None
    compact = {
        "schema_version": report.get("schema_version"),
        "generated_at": report.get("generated_at"),
    }
    compact.update(summary)
    return compact


def _read_track_rows(csv_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for index, row in enumerate(reader):
            rows.append(
                {
                    "order": index,
                    "frame": _parse_int(row.get("Frame")),
                    "x": _parse_float(row.get("X")),
                    "y": _parse_float(row.get("Y")),
                    "confidence": _parse_float(row.get("Confidence")),
                    "status": row.get("Status") or "",
                }
            )
    return sorted(
        rows,
        key=lambda item: (
            item["frame"] is None,
            item["order"] if item["frame"] is None else item["frame"],
            item["order"],
        ),
    )


def _build_source_tracklets(source: str, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tracklets: list[dict[str, Any]] = []
    review_events: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []

    for row in rows:
        is_tracklet_row = _is_tracklet_row(row)
        is_contiguous = not current or _is_contiguous(current[-1], row)
        if is_tracklet_row and is_contiguous:
            current.append(row)
            continue
        if current:
            tracklets.append(_make_tracklet(source, current))
            current = []
        if is_tracklet_row:
            current.append(row)

    if current:
        tracklets.append(_make_tracklet(source, current))

    for tracklet in tracklets:
        review_events.extend(_tracklet_flag_events(tracklet))
    review_events.extend(_lost_gap_events(source, tracklets))
    return tracklets, review_events


def _make_tracklet(source: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    start_frame = rows[0]["frame"]
    end_frame = rows[-1]["frame"]
    confidences = [row["confidence"] for row in rows if row["confidence"] is not None]
    step_lengths = [
        math.dist((previous["x"], previous["y"]), (current["x"], current["y"]))
        for previous, current in zip(rows, rows[1:])
    ]
    max_step = max(step_lengths) if step_lengths else None
    status_counts: dict[str, int] = {}
    for row in rows:
        status = row["status"]
        status_counts[status] = status_counts.get(status, 0) + 1
    mean_confidence = _mean(confidences)
    flags = _tracklet_flags(len(rows), mean_confidence, max_step)
    return {
        "id": f"{source}:{start_frame}-{end_frame}",
        "source": source,
        "start_frame": start_frame,
        "end_frame": end_frame,
        "length": len(rows),
        "status_counts": status_counts,
        "mean_confidence": _round_or_none(mean_confidence, 4),
        "start_point": _point_payload(rows[0]),
        "end_point": _point_payload(rows[-1]),
        "max_step_px": _round_or_none(max_step, 2),
        "flags": flags,
        "suspicion_score": _suspicion_score(flags),
    }


def _tracklet_flags(length: int, mean_confidence: float | None, max_step: float | None) -> list[str]:
    flags: list[str] = []
    if length <= SHORT_TRACKLET_MAX_LENGTH:
        flags.append("short_tracklet")
    if mean_confidence is not None and mean_confidence < LOW_CONFIDENCE_THRESHOLD:
        flags.append("low_confidence")
    if max_step is not None and max_step >= LARGE_JUMP_THRESHOLD_PX:
        flags.append("large_jump")
    return flags


def _tracklet_flag_events(tracklet: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for flag in tracklet["flags"]:
        severity = "fail" if flag == "large_jump" else "warn"
        evidence = {
            "tracklet_id": tracklet["id"],
            "length": tracklet["length"],
            "mean_confidence": tracklet["mean_confidence"],
            "max_step_px": tracklet["max_step_px"],
            "flags": tracklet["flags"],
        }
        events.append(
            {
                "source": tracklet["source"],
                "type": flag,
                "severity": severity,
                "start_frame": tracklet["start_frame"],
                "end_frame": tracklet["end_frame"],
                "frame_count": tracklet["length"],
                "reason": _flag_reason(flag),
                "evidence": evidence,
            }
        )
    return events


def _lost_gap_events(source: str, tracklets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for previous, current in zip(tracklets, tracklets[1:]):
        previous_end = previous.get("end_frame")
        current_start = current.get("start_frame")
        if not isinstance(previous_end, int) or not isinstance(current_start, int):
            continue
        gap = current_start - previous_end - 1
        if gap < LOST_GAP_MIN_FRAMES:
            continue
        events.append(
            {
                "source": source,
                "type": "lost_gap",
                "severity": "warn",
                "start_frame": previous_end + 1,
                "end_frame": current_start - 1,
                "frame_count": gap,
                "reason": f"Ball track is lost for {gap} frames between tracklets.",
                "evidence": {
                    "previous_tracklet_id": previous["id"],
                    "next_tracklet_id": current["id"],
                    "gap_frames": gap,
                },
            }
        )
    return events


def _build_debug_events(debug_path: Path) -> list[dict[str, Any]]:
    if not debug_path.exists():
        return []
    events: list[dict[str, Any]] = []
    with debug_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            scores = _candidate_scores(row.get("candidate_scores"))
            if len(scores) < 2:
                continue
            best, second = scores[0], scores[1]
            delta = round(best - second, 4)
            if delta > CANDIDATE_AMBIGUITY_DELTA:
                continue
            frame = _parse_int(row.get("frame"))
            events.append(
                {
                    "source": "raw",
                    "type": "candidate_ambiguity",
                    "severity": "warn",
                    "start_frame": frame,
                    "end_frame": frame,
                    "frame_count": 1,
                    "reason": "Top two ball candidate scores are too close to review automatically.",
                    "evidence": {
                        "best_score": round(best, 4),
                        "second_score": round(second, 4),
                        "score_delta": delta,
                        "candidate_count": len(scores),
                    },
                }
            )
    return events


def _build_cleanup_events(cleanup_path: Path) -> list[dict[str, Any]]:
    if not cleanup_path.exists():
        return []
    try:
        loaded = json.loads(cleanup_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, dict) or not isinstance(loaded.get("actions"), list):
        return []
    events: list[dict[str, Any]] = []
    for action in loaded["actions"]:
        if not isinstance(action, dict):
            continue
        start_frame = _parse_int(action.get("start_frame"))
        if start_frame is None:
            start_frame = _parse_int(action.get("frame"))
        end_frame = _parse_int(action.get("end_frame"))
        if end_frame is None:
            end_frame = start_frame
        frame_count = _parse_int(action.get("island_length"))
        if frame_count is None and isinstance(start_frame, int) and isinstance(end_frame, int):
            frame_count = max(1, end_frame - start_frame + 1)
        events.append(
            {
                "source": "postprocess",
                "type": "postprocess_action",
                "severity": "info",
                "start_frame": start_frame,
                "end_frame": end_frame,
                "frame_count": frame_count or 1,
                "reason": str(action.get("reason") or action.get("action") or "postprocess action"),
                "evidence": {"action": action},
            }
        )
    return events


def _is_tracklet_row(row: dict[str, Any]) -> bool:
    return row.get("status") != "Lost" and row.get("x") is not None and row.get("y") is not None


def _is_contiguous(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_frame = previous.get("frame")
    current_frame = current.get("frame")
    if not isinstance(previous_frame, int) or not isinstance(current_frame, int):
        return True
    return current_frame == previous_frame + 1


def _candidate_scores(raw_scores: Any) -> list[float]:
    if not isinstance(raw_scores, list):
        return []
    scores: list[float] = []
    for item in raw_scores:
        if not isinstance(item, dict):
            continue
        score = _parse_float(item.get("total_score"))
        if score is not None:
            scores.append(score)
    return sorted(scores, reverse=True)


def _flag_reason(flag: str) -> str:
    reasons = {
        "short_tracklet": "Tracklet is too short to trust without review.",
        "low_confidence": "Mean detector confidence is below the conservative audit threshold.",
        "large_jump": "Tracklet contains a frame-to-frame jump above the conservative pixel threshold.",
    }
    return reasons.get(flag, "Tracklet should be reviewed.")


def _suspicion_score(flags: list[str]) -> float:
    weights = {
        "short_tracklet": 0.35,
        "low_confidence": 0.35,
        "large_jump": 0.50,
    }
    return round(min(1.0, sum(weights.get(flag, 0.0) for flag in flags)), 4)


def _event_sort_key(event: dict[str, Any]) -> tuple[int, int, str, str]:
    start_frame = event.get("start_frame")
    frame_sort = start_frame if isinstance(start_frame, int) else 10**12
    severity_order = {"fail": 0, "warn": 1, "info": 2}
    return (frame_sort, severity_order.get(str(event.get("severity")), 3), str(event.get("source")), str(event.get("type")))


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
        return float(value)
    except (TypeError, ValueError):
        return None


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _round_or_none(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    return round(value, digits)
