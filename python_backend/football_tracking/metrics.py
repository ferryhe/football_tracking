from __future__ import annotations

import csv
import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from football_tracking.ai_review_triggers import (
    compact_ai_review_trigger_summary,
    write_ai_review_trigger_report,
)
from football_tracking.ball_audit import compact_ball_audit_summary, write_ball_audit_report
from football_tracking.events import compact_event_candidate_summary, write_event_candidate_report
from football_tracking.player_tracks import (
    compact_player_tracks_summary,
    write_player_tracks_artifacts,
)
from football_tracking.review_packets import compact_review_packet_summary

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
    ball_audit_report = _read_optional_json(output_dir / "ball_audit.json")
    if ball_audit_report is not None:
        ball_audit_summary = compact_ball_audit_summary(ball_audit_report)
        if ball_audit_summary is not None:
            report["ball_audit"] = ball_audit_summary
    ai_review_trigger_report = _read_optional_json(output_dir / "ai_review_triggers.json")
    if ai_review_trigger_report is not None:
        ai_review_trigger_summary = compact_ai_review_trigger_summary(ai_review_trigger_report)
        if ai_review_trigger_summary is not None:
            report["ai_review_triggers"] = ai_review_trigger_summary
    event_candidate_report = _read_optional_json(output_dir / "event_candidates.json")
    if event_candidate_report is not None:
        event_candidate_summary = compact_event_candidate_summary(event_candidate_report)
        if event_candidate_summary is not None:
            report["event_candidates"] = event_candidate_summary
    player_tracks_report = _read_optional_json(output_dir / "player_tracks.json")
    if player_tracks_report is not None:
        player_tracks_summary = compact_player_tracks_summary(player_tracks_report)
        if player_tracks_summary is not None:
            report["player_tracks"] = player_tracks_summary
    review_packets_report = _read_optional_json(output_dir / "review_packets.json")
    if review_packets_report is not None:
        review_packets_summary = compact_review_packet_summary(review_packets_report)
        if review_packets_summary is not None:
            report["review_packets"] = review_packets_summary
    temporal_chunks_report = _read_optional_json(output_dir / "temporal_chunks_report.json")
    if temporal_chunks_report is not None:
        temporal_chunks_summary = compact_temporal_chunk_summary(temporal_chunks_report)
        if temporal_chunks_summary is not None:
            report["temporal_chunks"] = temporal_chunks_summary
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
    ball_audit_error: str | None = None
    try:
        write_ball_audit_report(output_dir)
    except Exception as exc:
        ball_audit_error = f"Failed to write ball_audit.json: {exc}"
    ai_review_triggers_error: str | None = None
    try:
        write_ai_review_trigger_report(output_dir)
    except Exception as exc:
        ai_review_triggers_error = f"Failed to write ai_review_triggers.json: {exc}"
    event_candidates_error: str | None = None
    try:
        write_event_candidate_report(output_dir)
    except Exception as exc:
        event_candidates_error = f"Failed to write event_candidates.json: {exc}"
    player_tracks_error: str | None = None
    try:
        write_player_tracks_artifacts(output_dir)
    except Exception as exc:
        player_tracks_error = f"Failed to write player track artifacts: {exc}"
    report = build_metrics_report(output_dir)
    if ball_audit_error is not None:
        report["ball_audit_error"] = ball_audit_error
    if ai_review_triggers_error is not None:
        report["ai_review_triggers_error"] = ai_review_triggers_error
    if event_candidates_error is not None:
        report["event_candidates_error"] = event_candidates_error
    if player_tracks_error is not None:
        report["player_tracks_error"] = player_tracks_error
    _write_json(output_dir / "run_manifest.json", manifest)
    _write_json(output_dir / "metrics_report.json", report)
    return manifest, report


def stats_from_metrics_report(report: dict[str, Any]) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    tracks_raw = report.get("tracks")
    tracks = tracks_raw if isinstance(tracks_raw, dict) else {}
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
    ball_audit = report.get("ball_audit")
    if isinstance(ball_audit, dict):
        stats["ball_audit"] = ball_audit
    ai_review_triggers = report.get("ai_review_triggers")
    if isinstance(ai_review_triggers, dict):
        stats["ai_review_triggers"] = ai_review_triggers
    event_candidates = report.get("event_candidates")
    if isinstance(event_candidates, dict):
        stats["event_candidates"] = event_candidates
    player_tracks = report.get("player_tracks")
    if isinstance(player_tracks, dict):
        stats["player_tracks"] = player_tracks
    review_packets = report.get("review_packets")
    if isinstance(review_packets, dict):
        stats["review_packets"] = review_packets
    temporal_chunks = report.get("temporal_chunks")
    if isinstance(temporal_chunks, dict):
        stats["temporal_chunks"] = temporal_chunks
    stats["metrics_report"] = {
        "schema_version": report.get("schema_version"),
        "generated_at": report.get("generated_at"),
    }
    return stats


def compact_temporal_chunk_summary(report: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(report, dict):
        return None

    summary: dict[str, Any] = {"enabled": True}
    chunks = report.get("chunks")
    chunk_count = _coerce_int(report.get("chunk_count"))
    if chunk_count is None and isinstance(chunks, list):
        chunk_count = len(chunks)
    if chunk_count is not None:
        summary["chunk_count"] = chunk_count

    execution = report.get("execution")
    if isinstance(execution, dict):
        requested_workers = _coerce_int(execution.get("requested_workers"))
        effective_workers = _coerce_int(execution.get("effective_workers"))
        if effective_workers is not None:
            summary["effective_workers"] = effective_workers
        if requested_workers is not None:
            summary["requested_workers"] = requested_workers
        if isinstance(execution.get("mode"), str):
            summary["execution_mode"] = execution["mode"]
        if isinstance(execution.get("status"), str):
            summary["execution_status"] = execution["status"]

    stitch = report.get("stitch")
    if isinstance(stitch, dict) and isinstance(stitch.get("status"), str):
        summary["stitch_status"] = stitch["status"]

    merged_frame_count = _coerce_int(report.get("frame_count"))
    if merged_frame_count is not None:
        summary["merged_frame_count"] = merged_frame_count

    overlap_frames = _temporal_overlap_frames(report)
    if overlap_frames is not None:
        summary["overlap_frames"] = overlap_frames

    boundary_events = report.get("boundary_events")
    if isinstance(boundary_events, list):
        summary["boundary_review_event_count"] = len(boundary_events)

    return summary


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


def _coerce_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _temporal_overlap_frames(report: dict[str, Any]) -> int | None:
    explicit = _coerce_int(report.get("overlap_frames"))
    if explicit is not None:
        return explicit

    chunks = report.get("chunks")
    if not isinstance(chunks, list):
        return None

    overlaps: list[int] = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        start_frame = _coerce_int(chunk.get("start_frame"))
        end_frame = _coerce_int(chunk.get("end_frame"))
        core_start_frame = _coerce_int(chunk.get("core_start_frame"))
        core_end_frame = _coerce_int(chunk.get("core_end_frame"))
        if start_frame is not None and core_start_frame is not None and core_start_frame > start_frame:
            overlaps.append(core_start_frame - start_frame)
        if end_frame is not None and core_end_frame is not None and end_frame > core_end_frame:
            overlaps.append(end_frame - core_end_frame)
    return max(overlaps) if overlaps else None


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
    try:
        with path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
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
