from __future__ import annotations

import csv
import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"
FALSE_POSITIVE_ISLAND_MAX_LENGTH = 2
TRACK_STATUSES = ("Detected", "Predicted", "Lost")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_track_metrics(csv_path: Path) -> dict[str, Any] | None:
    if not csv_path.exists():
        return None

    rows: list[dict[str, Any]] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows.append(
                {
                    "frame": _parse_int(row.get("Frame")),
                    "status": row.get("Status", ""),
                    "point": _parse_point(row.get("X"), row.get("Y")),
                }
            )

    frame_count = len(rows)
    status_counts = {status: 0 for status in TRACK_STATUSES}
    for row in rows:
        status = row["status"]
        if status in status_counts:
            status_counts[status] += 1

    segments = _status_segments([row["status"] for row in rows])
    detected_segments = [segment for segment in segments if segment["status"] == "Detected"]
    lost_segments = [segment for segment in segments if segment["status"] == "Lost"]
    step_lengths = _step_lengths(rows)
    velocity_vectors = _velocity_vectors(rows)
    accel_lengths = [math.dist(previous, current) for previous, current in zip(velocity_vectors, velocity_vectors[1:])]

    return {
        "frame_count": frame_count,
        "status_counts": status_counts,
        "detected": status_counts["Detected"],
        "predicted": status_counts["Predicted"],
        "lost": status_counts["Lost"],
        "detected_ratio": _ratio(status_counts["Detected"], frame_count),
        "predicted_ratio": _ratio(status_counts["Predicted"], frame_count),
        "lost_ratio": _ratio(status_counts["Lost"], frame_count),
        "detected_segments": len(detected_segments),
        "predicted_segments": sum(1 for segment in segments if segment["status"] == "Predicted"),
        "lost_segments": len(lost_segments),
        # Heuristic proxy: short Detected islands are suspicious, not proven false positives.
        "false_positive_island_count": sum(
            1 for segment in detected_segments if segment["length"] <= FALSE_POSITIVE_ISLAND_MAX_LENGTH
        ),
        "false_positive_island_max_length": FALSE_POSITIVE_ISLAND_MAX_LENGTH,
        "reacquire_count": _reacquire_count(segments),
        "longest_lost_streak": max((segment["length"] for segment in lost_segments), default=0),
        "mean_step_px": _mean(step_lengths),
        "max_step_px": max(step_lengths) if step_lengths else None,
        "mean_accel_px": _mean(accel_lengths),
        "max_accel_px": max(accel_lengths) if accel_lengths else None,
    }


def build_metrics_report(output_dir: Path) -> dict[str, Any]:
    tracks: dict[str, Any] = {}
    raw_metrics = compute_track_metrics(output_dir / "ball_track.csv")
    if raw_metrics is not None:
        tracks["raw"] = raw_metrics
    cleaned_metrics = compute_track_metrics(output_dir / "ball_track.cleaned.csv")
    if cleaned_metrics is not None:
        tracks["cleaned"] = cleaned_metrics

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now_iso(),
        "tracks": tracks,
    }
    cleanup_report = _read_optional_json(output_dir / "cleanup_report.json")
    if cleanup_report is not None:
        report["cleanup"] = cleanup_report
    follow_cam_report = _read_optional_json(output_dir / "follow_cam_report.json")
    if follow_cam_report is not None:
        report["follow_cam"] = follow_cam_report
    return report


def build_run_manifest(output_dir: Path, run: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run.get("run_id"),
        "source": run.get("source"),
        "status": run.get("status"),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "completed_at": run.get("completed_at"),
        "config_name": run.get("config_name"),
        "config_path": run.get("config_path"),
        "input_video": run.get("input_video"),
        "output_dir": str(output_dir.resolve()),
        "modules_enabled": run.get("modules_enabled") or {},
        "notes": run.get("notes"),
        "git_commit": _git_commit(output_dir),
        "generated_at": utc_now_iso(),
    }


def write_run_artifacts(output_dir: Path, run: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = build_run_manifest(output_dir, run)
    report = build_metrics_report(output_dir)
    _write_json(output_dir / "run_manifest.json", manifest)
    _write_json(output_dir / "metrics_report.json", report)
    return manifest, report


def stats_from_metrics_report(report: dict[str, Any]) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    tracks = report.get("tracks") if isinstance(report.get("tracks"), dict) else {}
    raw = tracks.get("raw")
    if isinstance(raw, dict):
        stats["raw"] = raw
    cleaned = tracks.get("cleaned")
    if isinstance(cleaned, dict):
        stats["cleaned"] = cleaned
    cleanup = report.get("cleanup")
    if isinstance(cleanup, dict):
        stats["cleanup"] = cleanup
    follow_cam = report.get("follow_cam")
    if isinstance(follow_cam, dict):
        stats["follow_cam"] = follow_cam
    stats["metrics_report"] = {
        "schema_version": report.get("schema_version"),
        "generated_at": report.get("generated_at"),
    }
    return stats


def _parse_int(value: str | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _parse_point(x_value: str | None, y_value: str | None) -> tuple[float, float] | None:
    if x_value in (None, "") or y_value in (None, ""):
        return None
    try:
        return float(x_value), float(y_value)
    except ValueError:
        return None


def _status_segments(statuses: list[str]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for status in statuses:
        if segments and segments[-1]["status"] == status:
            segments[-1]["length"] += 1
        else:
            segments.append({"status": status, "length": 1})
    return segments


def _reacquire_count(segments: list[dict[str, Any]]) -> int:
    return sum(
        1
        for index, segment in enumerate(segments)
        if index > 0 and segment["status"] == "Detected" and segments[index - 1]["status"] != "Detected"
    )


def _step_lengths(rows: list[dict[str, Any]]) -> list[float]:
    lengths: list[float] = []
    for previous, current in zip(rows, rows[1:]):
        previous_point = previous["point"]
        current_point = current["point"]
        if previous_point is None or current_point is None:
            continue
        lengths.append(math.dist(previous_point, current_point))
    return lengths


def _velocity_vectors(rows: list[dict[str, Any]]) -> list[tuple[float, float]]:
    velocities: list[tuple[float, float]] = []
    for previous, current in zip(rows, rows[1:]):
        previous_point = previous["point"]
        current_point = current["point"]
        if previous_point is None or current_point is None:
            continue
        frame_gap = _frame_gap(previous.get("frame"), current.get("frame"))
        velocities.append(
            (
                (current_point[0] - previous_point[0]) / frame_gap,
                (current_point[1] - previous_point[1]) / frame_gap,
            )
        )
    return velocities


def _frame_gap(previous_frame: int | None, current_frame: int | None) -> int:
    if previous_frame is None or current_frame is None:
        return 1
    return max(1, current_frame - previous_frame)


def _ratio(count: int, total: int) -> float:
    return 0.0 if total == 0 else round(count / total, 4)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    return loaded if isinstance(loaded, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _git_commit(anchor: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(anchor.resolve()), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return None
    commit = result.stdout.strip()
    if result.returncode != 0 or not commit:
        return None
    return commit
