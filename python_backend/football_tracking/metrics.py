from __future__ import annotations

import csv
import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2

from football_tracking.accepted_highlights import (
    REPORT_FILE_NAME as ACCEPTED_HIGHLIGHTS_REPORT_FILE_NAME,
)
from football_tracking.accepted_highlights import (
    compact_accepted_highlights_summary,
)
from football_tracking.ai_improvement import compact_ai_improvement_summary
from football_tracking.ai_review_triggers import (
    compact_ai_review_trigger_summary,
    write_ai_review_trigger_report,
)
from football_tracking.ai_visual_review import compact_ai_visual_review_summary
from football_tracking.ball_audit import compact_ball_audit_summary, write_ball_audit_report
from football_tracking.events import compact_event_candidate_summary, write_event_candidate_report
from football_tracking.player_tracks import (
    compact_player_tracks_summary,
    write_player_tracks_artifacts,
)
from football_tracking.review_packets import compact_review_packet_summary
from football_tracking.trial_diagnosis import build_trial_diagnosis

SCHEMA_VERSION = "1.0"
FALSE_POSITIVE_ISLAND_MAX_LENGTH = 2
QUALITY_LONG_LOST_GAP_MIN_FRAMES = 120
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


def build_metrics_report(output_dir: Path, run: dict[str, Any] | None = None) -> dict[str, Any]:
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
    ai_visual_review_report = _read_optional_json(output_dir / "ai_visual_review.json")
    if ai_visual_review_report is not None:
        ai_visual_review_summary = compact_ai_visual_review_summary(ai_visual_review_report)
        if ai_visual_review_summary is not None:
            report["ai_visual_review"] = ai_visual_review_summary
    ai_improvement_report = _read_optional_json(output_dir / "ai_improvement_report.json")
    if ai_improvement_report is not None:
        ai_improvement_summary = compact_ai_improvement_summary(ai_improvement_report)
        if ai_improvement_summary is not None:
            report["ai_improvement"] = ai_improvement_summary
    accepted_highlights_report = _read_optional_json(_accepted_highlights_report_path(output_dir))
    if accepted_highlights_report is not None:
        accepted_highlights_summary = compact_accepted_highlights_summary(accepted_highlights_report)
        if accepted_highlights_summary is not None:
            report["accepted_highlights"] = accepted_highlights_summary
    temporal_chunks_report = _read_optional_json(output_dir / "temporal_chunks_report.json")
    if temporal_chunks_report is not None:
        temporal_chunks_summary = compact_temporal_chunk_summary(temporal_chunks_report)
        if temporal_chunks_summary is not None:
            report["temporal_chunks"] = temporal_chunks_summary
    quality_gate_summary = _build_quality_gate_summary(
        ball_audit_report=ball_audit_report,
        review_packets_report=review_packets_report,
        high_recall_reports=_read_high_recall_reports(output_dir),
    )
    if quality_gate_summary is not None:
        report["quality_gate"] = quality_gate_summary
    if _is_production_trial_run(run):
        diagnosis = build_trial_diagnosis(output_dir, run or {}, metrics_report=report)
        trial_gate = diagnosis.get("trial_signal_gate_v2")
        if isinstance(trial_gate, dict):
            stages = trial_gate.get("stage_counts")
            if isinstance(stages, dict):
                report["detection_stages"] = stages
            report["trial_signal_gate_v2"] = trial_gate
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
        "config_sha256": run.get("config_sha256"),
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
        event_candidate_fps, event_candidate_fps_source = _event_candidate_fps(run)
        write_event_candidate_report(output_dir, fps=event_candidate_fps, fps_source=event_candidate_fps_source)
    except Exception as exc:
        event_candidates_error = f"Failed to write event_candidates.json: {exc}"
    player_tracks_error: str | None = None
    try:
        write_player_tracks_artifacts(output_dir)
    except Exception as exc:
        player_tracks_error = f"Failed to write player track artifacts: {exc}"
    report = build_metrics_report(output_dir, run=run)
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


def _event_candidate_fps(run: dict[str, Any]) -> tuple[float | None, str | None]:
    for key in ("fps", "video_fps"):
        fps = _optional_positive_float(run.get(key))
        if fps is not None:
            return fps, key
    input_video = run.get("input_video")
    if not isinstance(input_video, str) or not input_video.strip():
        return None, None
    capture = cv2.VideoCapture(str(Path(input_video).resolve()))
    try:
        if not capture.isOpened():
            return None, None
        fps = _optional_positive_float(capture.get(cv2.CAP_PROP_FPS))
        return (fps, "input_video") if fps is not None else (None, None)
    finally:
        capture.release()


def _optional_positive_float(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


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
    quality_gate = report.get("quality_gate")
    if isinstance(quality_gate, dict):
        stats["quality_gate"] = quality_gate
    detection_stages = report.get("detection_stages")
    if isinstance(detection_stages, dict):
        stats["detection_stages"] = detection_stages
    trial_signal_gate = report.get("trial_signal_gate_v2")
    if isinstance(trial_signal_gate, dict):
        stats["trial_signal_gate_v2"] = trial_signal_gate
    ai_visual_review = report.get("ai_visual_review")
    if isinstance(ai_visual_review, dict):
        stats["ai_visual_review"] = ai_visual_review
    ai_improvement = report.get("ai_improvement")
    if isinstance(ai_improvement, dict):
        stats["ai_improvement"] = ai_improvement
    accepted_highlights = report.get("accepted_highlights")
    if isinstance(accepted_highlights, dict):
        stats["accepted_highlights"] = accepted_highlights
    temporal_chunks = report.get("temporal_chunks")
    if isinstance(temporal_chunks, dict):
        stats["temporal_chunks"] = temporal_chunks
    stats["metrics_report"] = {
        "schema_version": report.get("schema_version"),
        "generated_at": report.get("generated_at"),
    }
    return stats


def _is_production_trial_run(run: dict[str, Any] | None) -> bool:
    if not isinstance(run, dict):
        return False
    notes = run.get("notes")
    if not isinstance(notes, str):
        return False
    try:
        parsed = json.loads(notes)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and parsed.get("purpose") == "production_trial"


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


def _build_quality_gate_summary(
    *,
    ball_audit_report: dict[str, Any] | None,
    review_packets_report: dict[str, Any] | None,
    high_recall_reports: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not isinstance(ball_audit_report, dict):
        return None

    long_lost_gaps = _long_lost_gap_ranges(ball_audit_report)
    covered_gaps = [gap for gap in long_lost_gaps if _gap_has_review_packet(gap, review_packets_report)]
    unreviewed_gaps = [gap for gap in long_lost_gaps if gap not in covered_gaps]
    high_recall_budget_rejections = _lost_gap_budget_rejection_ranges(
        high_recall_reports,
        long_lost_gaps=unreviewed_gaps,
    )
    status = "stable"
    if unreviewed_gaps or high_recall_budget_rejections:
        status = "needs_review"
    elif long_lost_gaps:
        status = "review_ready"

    return {
        "status": status,
        "long_lost_gap_scope": "between_tracklets",
        "long_lost_gap_count": len(long_lost_gaps),
        "reviewed_long_lost_gap_count": len(covered_gaps),
        "unreviewed_long_lost_gap_count": len(unreviewed_gaps),
        "unreviewed_long_lost_gaps": [list(gap) for gap in unreviewed_gaps],
        "high_recall_lost_gap_budget_rejection_count": len(high_recall_budget_rejections),
        "high_recall_lost_gap_budget_rejections": [list(gap) for gap in high_recall_budget_rejections],
    }


def _long_lost_gap_ranges(report: dict[str, Any]) -> list[tuple[int, int]]:
    review_events = report.get("review_events")
    if not isinstance(review_events, list):
        return []
    gaps: list[tuple[int, int]] = []
    for event in review_events:
        if not isinstance(event, dict) or event.get("type") != "lost_gap":
            continue
        start = _parse_int(event.get("start_frame"))
        end = _parse_int(event.get("end_frame"))
        if start is None or end is None:
            continue
        if end < start:
            start, end = end, start
        frame_count = _parse_int(event.get("frame_count"))
        if frame_count is None:
            frame_count = end - start + 1
        if frame_count >= QUALITY_LONG_LOST_GAP_MIN_FRAMES:
            gaps.append((start, end))
    return sorted(set(gaps))


def _gap_has_review_packet(gap: tuple[int, int], report: dict[str, Any] | None) -> bool:
    if not isinstance(report, dict):
        return False
    packets = report.get("packets")
    if not isinstance(packets, list):
        return False
    for packet in packets:
        if not isinstance(packet, dict):
            continue
        source = packet.get("source") if isinstance(packet.get("source"), dict) else {}
        if not _mapping_mentions_lost_gap(source):
            continue
        source_range = _range_from_mapping(source)
        window = packet.get("window") if isinstance(packet.get("window"), dict) else {}
        window_range = _range_from_mapping(window)
        if (source_range is not None and _range_covers(source_range, gap)) or (
            window_range is not None and _range_covers(window_range, gap)
        ):
            return True
    return False


def _lost_gap_budget_rejection_ranges(
    reports: list[dict[str, Any]],
    *,
    long_lost_gaps: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    if not long_lost_gaps:
        return []
    ranges: list[tuple[int, int]] = []
    for report in reports:
        rejected_windows = report.get("rejected_windows")
        if not isinstance(rejected_windows, list):
            continue
        for window in rejected_windows:
            if not isinstance(window, dict):
                continue
            if not _mapping_mentions_lost_gap(window) or window.get("rejection_reason") != "max_total_frames_exceeded":
                continue
            parsed_range = _range_from_mapping(window)
            if (
                parsed_range is not None
                and _range_frame_count(parsed_range) >= QUALITY_LONG_LOST_GAP_MIN_FRAMES
                and any(_range_covers(parsed_range, gap) for gap in long_lost_gaps)
            ):
                ranges.append(parsed_range)
    return sorted(set(ranges))


def _read_high_recall_reports(output_dir: Path) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    seen: set[Path] = set()
    default_report = output_dir / "high_recall_windows" / "report.json"
    if default_report.exists():
        seen.add(default_report.resolve())
        loaded = _read_optional_json(default_report)
        if isinstance(loaded, dict):
            reports.append(loaded)
    if not output_dir.exists():
        return reports
    for child in output_dir.iterdir():
        if not child.is_dir():
            continue
        report_path = child / "report.json"
        if not report_path.exists():
            continue
        resolved = report_path.resolve()
        if resolved in seen:
            continue
        loaded = _read_optional_json(report_path)
        if isinstance(loaded, dict) and (
            "rejected_windows" in loaded or "windows" in loaded or "reconcile" in loaded
        ):
            reports.append(loaded)
            seen.add(resolved)
    return reports


def _range_from_mapping(mapping: dict[str, Any]) -> tuple[int, int] | None:
    start = _parse_int(mapping.get("start_frame"))
    end = _parse_int(mapping.get("end_frame"))
    if start is None or end is None:
        return None
    return (start, end) if start <= end else (end, start)


def _range_covers(container: tuple[int, int], contained: tuple[int, int]) -> bool:
    return container[0] <= contained[0] and container[1] >= contained[1]


def _range_frame_count(value: tuple[int, int]) -> int:
    return abs(value[1] - value[0]) + 1


def _mapping_mentions_lost_gap(mapping: dict[str, Any]) -> bool:
    if str(mapping.get("type") or "").casefold() == "lost_gap":
        return True
    values = [mapping.get("reason")]
    evidence = mapping.get("evidence")
    if isinstance(evidence, dict):
        values.extend([evidence.get("reason"), evidence.get("window_reason"), evidence.get("trigger_type")])
    return any("lost_gap" in str(value or "").casefold() for value in values)


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


def _accepted_highlights_report_path(output_dir: Path) -> Path:
    default_path = output_dir / "highlights_ai_accepted" / ACCEPTED_HIGHLIGHTS_REPORT_FILE_NAME
    if default_path.exists():
        return default_path
    if output_dir.exists():
        for child in sorted(output_dir.iterdir(), key=lambda item: item.name):
            if not child.is_dir():
                continue
            candidate = child / ACCEPTED_HIGHLIGHTS_REPORT_FILE_NAME
            if candidate.exists():
                return candidate
    return default_path


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
