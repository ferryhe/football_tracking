from __future__ import annotations

import csv
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
DEFAULT_PRE_ROLL_FRAMES = 30
DEFAULT_POST_ROLL_FRAMES = 30
MAX_TRIGGER_WINDOW_FRAMES = 600
LONG_LOST_GAP_MIN_FRAMES = 120
LONG_LOST_GAP_RESERVED_PACKETS = 1
FRAME_SAMPLES_PER_PACKET = 5
CONTACT_SHEET_SEEK_PREROLL_FRAMES = 120

PRIORITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}
TRIGGER_TYPE_RANK = {
    "large_jump": 0,
    "lost_gap": 1,
    "candidate_ambiguity": 2,
    "postprocess_action": 3,
    "suspicious_tracklet": 4,
}


def build_review_packet_report(
    output_dir: Path,
    *,
    input_video: Path | None = None,
    follow_cam_video: Path | None = None,
    max_packets: int = 12,
    include_media: bool = True,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    track_rows = _read_track_rows(_preferred_track_path(output_dir))
    frame_bounds = _frame_bounds(track_rows)
    sources = _packet_sources(output_dir, max_packets=max_packets)
    input_video = _resolve_input_video(output_dir, input_video)
    follow_cam_video = _resolve_follow_cam_video(output_dir, follow_cam_video)

    packets: list[dict[str, Any]] = []
    packet_root = output_dir / "review_packets"
    for index, source in enumerate(sources[: max(0, max_packets)], start=1):
        window = _expanded_window(source, frame_bounds)
        track_summary = _track_summary(track_rows, window["start_frame"], window["end_frame"])
        decision = _decision_for_source(source, track_summary)
        packet_id = _packet_id(index, source)
        packet_dir = packet_root / packet_id
        media, media_warnings = (
            _write_packet_media(
                packet_dir=packet_dir,
                packet_id=packet_id,
                input_video=input_video,
                follow_cam_video=follow_cam_video,
                track_rows=track_rows,
                start_frame=window["start_frame"],
                end_frame=window["end_frame"],
            )
            if include_media
            else ({}, [])
        )
        packet = {
            "packet_id": packet_id,
            "source": source,
            "window": window,
            "track_summary": track_summary,
            "decision": decision,
            "media": media,
            "media_warnings": media_warnings,
        }
        packets.append(packet)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "output_dir": str(output_dir),
        "summary": {
            "packet_count": len(packets),
            "counts_by_label": dict(sorted(Counter(packet["decision"]["label"] for packet in packets).items())),
            "media_packet_count": sum(1 for packet in packets if packet["media"]),
        },
        "packets": packets,
    }


def write_review_packet_report(
    output_dir: Path,
    *,
    input_video: Path | None = None,
    follow_cam_video: Path | None = None,
    max_packets: int = 12,
    include_media: bool = True,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    report = build_review_packet_report(
        output_dir,
        input_video=input_video,
        follow_cam_video=follow_cam_video,
        max_packets=max_packets,
        include_media=include_media,
    )
    packet_root = output_dir / "review_packets"
    packet_root.mkdir(parents=True, exist_ok=True)
    for packet in report["packets"]:
        packet_dir = packet_root / packet["packet_id"]
        packet_dir.mkdir(parents=True, exist_ok=True)
        _write_json(packet_dir / "manifest.json", packet)
    _write_json(output_dir / "review_packets.json", report)
    return report


def compact_review_packet_summary(report: dict[str, Any]) -> dict[str, Any] | None:
    summary = report.get("summary")
    if not isinstance(summary, dict):
        return None
    counts_by_label = summary.get("counts_by_label")
    return {
        "schema_version": report.get("schema_version"),
        "generated_at": report.get("generated_at"),
        "packet_count": _safe_summary_int(summary.get("packet_count")),
        "media_packet_count": _safe_summary_int(summary.get("media_packet_count")),
        "counts_by_label": counts_by_label if isinstance(counts_by_label, dict) else {},
    }


def _packet_sources(output_dir: Path, *, max_packets: int) -> list[dict[str, Any]]:
    if max_packets <= 0:
        return []
    event_sources = _event_candidate_sources(output_dir)
    trigger_sources = [*_trigger_sources(output_dir), *_high_recall_rejection_sources(output_dir)]
    reserved_sources = _reserved_long_lost_gap_sources(trigger_sources, max_packets=max_packets)
    reserved_keys = {_source_key(source) for source in reserved_sources}
    trigger_sources = [source for source in trigger_sources if _source_key(source) not in reserved_keys]
    remaining_slots = max(0, max_packets - len(reserved_sources))
    if remaining_slots <= 0:
        return _dedupe_sources(reserved_sources)[:max_packets]

    if not event_sources or not trigger_sources:
        selected = _dedupe_sources([*event_sources, *trigger_sources])[:remaining_slots]
        return _dedupe_sources([*selected, *reserved_sources])[:max_packets]

    trigger_quota = min(len(trigger_sources), max(1, remaining_slots // 2))
    event_quota = remaining_slots - trigger_quota
    if event_quota <= 0 and event_sources:
        event_quota = 1
        trigger_quota = max(0, remaining_slots - event_quota)

    selected = [
        *event_sources[:event_quota],
        *trigger_sources[:trigger_quota],
        *event_sources[event_quota:],
        *trigger_sources[trigger_quota:],
    ]
    return _dedupe_sources([*selected[:remaining_slots], *reserved_sources])[:max_packets]


def _dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, int | None, int | None]] = set()
    for source in sources:
        key = _source_key(source)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(source)
    return deduped


def _source_key(source: dict[str, Any]) -> tuple[str, int | None, int | None]:
    return (str(source.get("kind")), source.get("start_frame"), source.get("end_frame"))


def _reserved_long_lost_gap_sources(sources: list[dict[str, Any]], *, max_packets: int) -> list[dict[str, Any]]:
    if max_packets <= 0:
        return []
    long_lost_gaps = [
        source
        for source in sources
        if _source_represents_lost_gap(source)
        and _frame_count(int(source["start_frame"]), int(source["end_frame"])) >= LONG_LOST_GAP_MIN_FRAMES
    ]
    long_lost_gaps.sort(
        key=lambda item: (
            -_frame_count(int(item["start_frame"]), int(item["end_frame"])),
            -_priority_rank(str(item.get("priority") or "none")),
            int(item["start_frame"]),
        )
    )
    return long_lost_gaps[: min(max_packets, LONG_LOST_GAP_RESERVED_PACKETS)]


def _event_candidate_sources(output_dir: Path) -> list[dict[str, Any]]:
    report = _read_optional_json(output_dir / "event_candidates.json")
    candidates = report.get("candidates") if isinstance(report, dict) else None
    if not isinstance(candidates, list):
        return []
    sources: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        start = _parse_int(candidate.get("start_frame"))
        end = _parse_int(candidate.get("end_frame"))
        if start is None or end is None:
            continue
        render_window = candidate.get("render_window") if isinstance(candidate.get("render_window"), dict) else {}
        sources.append(
            {
                "kind": "event_candidate",
                "id": str(candidate.get("id") or f"candidate:{start}-{end}"),
                "type": str(candidate.get("type") or "event_candidate"),
                "priority": "high" if str(candidate.get("type")) == "goal_candidate" else "medium",
                "start_frame": start,
                "end_frame": end,
                "score": _parse_float(candidate.get("score")) or 0.0,
                "reason": str(candidate.get("reason") or "Event candidate selected for review."),
                "render_window": {
                    "start_frame": _parse_int(render_window.get("start_frame")),
                    "end_frame": _parse_int(render_window.get("end_frame")),
                },
                "evidence": candidate.get("evidence") if isinstance(candidate.get("evidence"), dict) else {},
            }
        )
    return sorted(
        sources,
        key=lambda item: (
            0 if item["type"] == "goal_candidate" else 1,
            -float(item.get("score") or 0.0),
            int(item["start_frame"]),
        ),
    )


def _trigger_sources(output_dir: Path) -> list[dict[str, Any]]:
    report = _read_optional_json(output_dir / "ai_review_triggers.json")
    triggers = report.get("triggers") if isinstance(report, dict) else None
    if not isinstance(triggers, list):
        return []
    sources: list[dict[str, Any]] = []
    for trigger in triggers:
        if not isinstance(trigger, dict):
            continue
        trigger_type = str(trigger.get("type") or "")
        if trigger_type == "dense_noise_cluster":
            continue
        start = _parse_int(trigger.get("start_frame"))
        end = _parse_int(trigger.get("end_frame"))
        if start is None or end is None:
            continue
        frame_count = _frame_count(start, end)
        if frame_count > MAX_TRIGGER_WINDOW_FRAMES and not _is_long_lost_gap_trigger(trigger_type, frame_count):
            continue
        sources.append(
            {
                "kind": "trigger",
                "id": str(trigger.get("id") or f"trigger:{trigger_type}:{start}-{end}"),
                "type": trigger_type,
                "priority": str(trigger.get("priority") or "none"),
                "start_frame": start,
                "end_frame": end,
                "score": _priority_rank(str(trigger.get("priority") or "none")),
                "reason": str(trigger.get("reason") or "Audit trigger selected for review."),
                "evidence": trigger.get("evidence") if isinstance(trigger.get("evidence"), dict) else {},
            }
        )
    return sorted(
        sources,
        key=lambda item: (
            -_priority_rank(str(item.get("priority") or "none")),
            TRIGGER_TYPE_RANK.get(str(item.get("type") or ""), 99),
            int(item["start_frame"]),
        ),
    )


def _is_long_lost_gap_trigger(trigger_type: str, frame_count: int) -> bool:
    return trigger_type == "lost_gap" and frame_count >= LONG_LOST_GAP_MIN_FRAMES


def _high_recall_rejection_sources(output_dir: Path) -> list[dict[str, Any]]:
    clues: list[dict[str, Any]] = []
    for report_root in _high_recall_report_roots(output_dir):
        wrapper = _read_optional_json(report_root / "report.json") or {}
        standalone_reconcile = _read_optional_json(report_root / "reconcile_report.json")
        if isinstance(standalone_reconcile, dict):
            reports = [standalone_reconcile]
        else:
            reconcile = wrapper.get("reconcile") if isinstance(wrapper, dict) else None
            reports = [reconcile] if isinstance(reconcile, dict) else []

        rejected_windows = wrapper.get("rejected_windows") if isinstance(wrapper, dict) else None
        if isinstance(rejected_windows, list):
            for window in rejected_windows:
                if not isinstance(window, dict):
                    continue
                clue = dict(window)
                clue.setdefault("rejection_reason", "planning_rejected")
                clues.append(clue)

        for report in reports:
            report_clues = report.get("review_packet_clues") if isinstance(report, dict) else None
            if isinstance(report_clues, list):
                clues.extend(clue for clue in report_clues if isinstance(clue, dict))
                continue
            for result in report.get("windows", []) if isinstance(report, dict) else []:
                if not isinstance(result, dict) or result.get("accepted"):
                    continue
                window = result.get("window") if isinstance(result.get("window"), dict) else {}
                clue = dict(window)
                clue["rejection_reason"] = result.get("reason")
                clues.append(clue)

    sources: list[dict[str, Any]] = []
    for index, clue in enumerate(clues):
        if not isinstance(clue, dict):
            continue
        start = _parse_int(clue.get("start_frame"))
        end = _parse_int(clue.get("end_frame"))
        if start is None or end is None:
            continue
        priority = str(clue.get("priority") or "medium")
        rejection_reason = str(clue.get("rejection_reason") or "rejected")
        window_reason = str(clue.get("reason") or "")
        sources.append(
            {
                "kind": "high_recall_rejection",
                "id": f"high_recall_rejection:{index}:{start}-{end}",
                "type": "high_recall_rejected",
                "priority": priority,
                "start_frame": start,
                "end_frame": end,
                "score": _priority_rank(priority),
                "reason": f"High-recall window rejected: {rejection_reason}",
                "evidence": {
                    "rejection_reason": rejection_reason,
                    "window_reason": window_reason,
                },
            }
        )
    return sorted(
        sources,
        key=lambda item: (
            -_priority_rank(str(item.get("priority") or "none")),
            int(item["start_frame"]),
        ),
    )


def _high_recall_report_roots(output_dir: Path) -> list[Path]:
    roots: list[Path] = []
    default_root = output_dir / "high_recall_windows"
    if (default_root / "report.json").exists() or (default_root / "reconcile_report.json").exists():
        roots.append(default_root)

    if not output_dir.exists():
        return roots
    for child in output_dir.iterdir():
        if child == default_root or not child.is_dir():
            continue
        wrapper = _read_optional_json(child / "report.json")
        if isinstance(wrapper, dict) and (
            "rejected_windows" in wrapper or "reconcile" in wrapper or "windows" in wrapper
        ):
            roots.append(child)
        elif (child / "reconcile_report.json").exists():
            roots.append(child)
    return roots


def _expanded_window(source: dict[str, Any], frame_bounds: tuple[int | None, int | None]) -> dict[str, int]:
    render_window = source.get("render_window") if isinstance(source.get("render_window"), dict) else {}
    start = _parse_int(render_window.get("start_frame"))
    end = _parse_int(render_window.get("end_frame"))
    if start is None:
        start = (_parse_int(source.get("start_frame")) or 0) - DEFAULT_PRE_ROLL_FRAMES
    if end is None:
        end = (_parse_int(source.get("end_frame")) or start) + DEFAULT_POST_ROLL_FRAMES

    min_frame, max_frame = frame_bounds
    if min_frame is not None:
        start = max(min_frame, start)
    else:
        start = max(0, start)
    if max_frame is not None:
        end = min(max_frame, end)
    end = max(start, end)
    return {"start_frame": int(start), "end_frame": int(end), "frame_count": _frame_count(start, end)}


def _track_summary(rows: list[dict[str, Any]], start_frame: int, end_frame: int) -> dict[str, Any]:
    window_rows = [row for row in rows if start_frame <= row["frame"] <= end_frame]
    status_counts = Counter(row["status"] for row in window_rows)
    point_rows = [row for row in window_rows if row["point"] is not None]
    steps = [
        math.dist(previous["point"], current["point"])
        for previous, current in zip(point_rows, point_rows[1:])
        if previous["point"] is not None and current["point"] is not None
    ]
    confidences = [row["confidence"] for row in point_rows if row["confidence"] is not None]
    frame_count = len(window_rows)
    return {
        "frame_count": frame_count,
        "status_counts": dict(sorted(status_counts.items())),
        "detected_ratio": _ratio(status_counts.get("Detected", 0), frame_count),
        "predicted_ratio": _ratio(status_counts.get("Predicted", 0), frame_count),
        "lost_ratio": _ratio(status_counts.get("Lost", 0), frame_count),
        "mean_confidence": _round_or_none(_mean(confidences), 4),
        "max_step_px": _round_or_none(max(steps) if steps else None, 2),
        "mean_step_px": _round_or_none(_mean(steps), 2),
    }


def _decision_for_source(source: dict[str, Any], track_summary: dict[str, Any]) -> dict[str, Any]:
    source_kind = str(source.get("kind") or "")
    source_type = str(source.get("type") or "")
    score = float(source.get("score") or 0.0)
    detected_ratio = float(track_summary.get("detected_ratio") or 0.0)
    lost_ratio = float(track_summary.get("lost_ratio") or 0.0)
    max_step = _parse_float(track_summary.get("max_step_px")) or 0.0

    if source_kind == "event_candidate" and score >= 0.85 and detected_ratio >= 0.25:
        return _decision(
            "highlight_worthy",
            min(0.98, max(0.70, score)),
            f"{source_type} score {score:.2f} with detected ratio {detected_ratio:.2f}.",
        )
    if source_type == "lost_gap" and lost_ratio >= 0.45:
        return _decision("ball_not_visible", 0.74, f"Lost ratio {lost_ratio:.2f} dominates this review window.")
    if source_type == "postprocess_action":
        return _decision("reject_noise", 0.70, "Postprocess replaced this segment, so treat it as likely noise.")
    if source_type in {"large_jump", "candidate_ambiguity", "suspicious_tracklet"}:
        return _decision("needs_ai_review", 0.76, f"{source_type} trigger with max step {max_step:.2f}px.")
    if source_kind == "high_recall_rejection":
        return _decision("needs_ai_review", 0.78, str(source.get("reason") or "High-recall rerun rejected."))
    if detected_ratio >= 0.70 and max_step <= 180.0:
        return _decision("accept_track", 0.68, "Track is mostly detected with no large jumps in this window.")
    return _decision("manual_review", 0.55, "Mixed evidence; ask AI or a human to inspect the packet media.")


def _decision(label: str, confidence: float, reason: str) -> dict[str, Any]:
    return {
        "label": label,
        "confidence": round(confidence, 4),
        "reason": reason,
    }


def _write_packet_media(
    *,
    packet_dir: Path,
    packet_id: str,
    input_video: Path | None,
    follow_cam_video: Path | None,
    track_rows: list[dict[str, Any]],
    start_frame: int,
    end_frame: int,
) -> tuple[dict[str, str], list[str]]:
    packet_dir.mkdir(parents=True, exist_ok=True)
    media: dict[str, str] = {}
    warnings: list[str] = []
    if input_video is not None and input_video.exists():
        contact_path = packet_dir / "contact_sheet.jpg"
        crop_path = packet_dir / "crop_sheet.jpg"
        if _write_contact_sheets(
            video_path=input_video,
            track_rows=track_rows,
            start_frame=start_frame,
            end_frame=end_frame,
            contact_path=contact_path,
            crop_path=crop_path,
        ):
            media["contact_sheet"] = str(contact_path)
            media["crop_sheet"] = str(crop_path)
        else:
            warnings.append("contact_sheet_failed")
    clip_source = follow_cam_video if follow_cam_video is not None and follow_cam_video.exists() else input_video
    if clip_source is not None and clip_source.exists():
        clip_path = packet_dir / f"{packet_id}.mp4"
        if _write_clip(clip_source, clip_path, start_frame, end_frame):
            media["clip"] = str(clip_path)
        else:
            warnings.append("clip_failed")
    return media, warnings


def _write_contact_sheets(
    *,
    video_path: Path,
    track_rows: list[dict[str, Any]],
    start_frame: int,
    end_frame: int,
    contact_path: Path,
    crop_path: Path,
) -> bool:
    import cv2

    samples = _sample_frames(start_frame, end_frame, FRAME_SAMPLES_PER_PACKET)
    row_by_frame = {row["frame"]: row for row in track_rows}
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        return False
    thumbs: list[Any] = []
    crops: list[Any] = []
    sample_set = set(samples)
    first_sample = min(samples)
    last_sample = max(samples)
    seek_start = max(0, first_sample - CONTACT_SHEET_SEEK_PREROLL_FRAMES)
    capture.set(cv2.CAP_PROP_POS_FRAMES, seek_start)
    frame_index = seek_start - 1
    while frame_index < last_sample:
        ok, frame = capture.read()
        if not ok:
            break
        frame_index += 1
        if frame_index not in sample_set:
            continue
        row = row_by_frame.get(frame_index)
        marked = frame.copy()
        crop = _blank_crop()
        if row is not None and row["point"] is not None:
            x, y = row["point"]
            status = row["status"]
            color = _status_color(status)
            center = (int(round(x)), int(round(y)))
            cv2.circle(marked, center, 40, color, 8)
            cv2.drawMarker(marked, center, color, cv2.MARKER_CROSS, 90, 6)
            crop = _crop_around(marked, center)
        _draw_label(marked, f"frame {frame_index}")
        _draw_label(crop, f"frame {frame_index}", scale=0.8, y=44)
        thumbs.append(cv2.resize(marked, (960, 270), interpolation=cv2.INTER_AREA))
        crops.append(crop)
        if len(thumbs) >= len(samples):
            break
    capture.release()
    if not thumbs:
        return False
    contact_ok = cv2.imwrite(str(contact_path), cv2.vconcat(thumbs))
    crop_rows: list[Any] = []
    for index in range(0, len(crops), 2):
        row = crops[index : index + 2]
        if len(row) == 1:
            row.append(_blank_crop())
        crop_rows.append(cv2.hconcat(row))
    crop_ok = cv2.imwrite(str(crop_path), cv2.vconcat(crop_rows))
    if not contact_ok or not crop_ok or not _is_nonempty_file(contact_path) or not _is_nonempty_file(crop_path):
        contact_path.unlink(missing_ok=True)
        crop_path.unlink(missing_ok=True)
        return False
    return True


def _write_clip(video_path: Path, output_path: Path, start_frame: int, end_frame: int) -> bool:
    import cv2

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        return False
    fps = capture.get(cv2.CAP_PROP_FPS) or 20.0
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if width <= 0 or height <= 0:
        capture.release()
        return False
    if frame_count > 0:
        end_frame = min(end_frame, frame_count - 1)
        if start_frame >= frame_count:
            capture.release()
            return False
    writer = cv2.VideoWriter(str(output_path), cv2.VideoWriter.fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        capture.release()
        return False
    seek_start = max(0, start_frame - CONTACT_SHEET_SEEK_PREROLL_FRAMES)
    capture.set(cv2.CAP_PROP_POS_FRAMES, seek_start)
    frame_index = seek_start - 1
    written = 0
    while frame_index < end_frame:
        ok, frame = capture.read()
        if not ok:
            break
        frame_index += 1
        if frame_index < start_frame:
            continue
        writer.write(frame)
        written += 1
    writer.release()
    capture.release()
    if written == 0 or not _is_nonempty_file(output_path):
        output_path.unlink(missing_ok=True)
        return False
    return True


def _preferred_track_path(output_dir: Path) -> Path:
    cleaned = output_dir / "ball_track.cleaned.csv"
    return cleaned if cleaned.exists() else output_dir / "ball_track.csv"


def _read_track_rows(csv_path: Path) -> list[dict[str, Any]]:
    if not csv_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            frame = _parse_int(row.get("Frame"))
            if frame is None:
                continue
            point = _parse_point(row.get("X"), row.get("Y"))
            rows.append(
                {
                    "frame": frame,
                    "point": point,
                    "confidence": _parse_float(row.get("Confidence")),
                    "status": row.get("Status") or "",
                }
            )
    return sorted(rows, key=lambda item: item["frame"])


def _resolve_input_video(output_dir: Path, input_video: Path | None) -> Path | None:
    if input_video is not None:
        return input_video if input_video.exists() else None
    manifest = _read_optional_json(output_dir / "run_manifest.json")
    raw_path = manifest.get("input_video") if isinstance(manifest, dict) else None
    if not isinstance(raw_path, str) or not raw_path:
        return None
    candidate = Path(raw_path)
    if candidate.exists():
        return candidate
    for parent in [output_dir, *output_dir.parents]:
        resolved = parent / candidate
        if resolved.exists():
            return resolved
    return None


def _resolve_follow_cam_video(output_dir: Path, follow_cam_video: Path | None) -> Path | None:
    if follow_cam_video is not None:
        return follow_cam_video if follow_cam_video.exists() else None
    for pattern in ("follow_cam*.720p.mp4", "follow_cam*.mp4", "follow_cam.mp4"):
        matches = sorted(output_dir.glob(pattern), key=lambda path: path.stat().st_size if path.exists() else 0, reverse=True)
        for match in matches:
            if match.exists() and match.stat().st_size > 0:
                return match
    return None


def _frame_bounds(rows: list[dict[str, Any]]) -> tuple[int | None, int | None]:
    if not rows:
        return None, None
    frames = [row["frame"] for row in rows]
    return min(frames), max(frames)


def _sample_frames(start_frame: int, end_frame: int, count: int) -> list[int]:
    if count <= 1 or end_frame <= start_frame:
        return [start_frame]
    step = (end_frame - start_frame) / float(count - 1)
    return sorted({int(round(start_frame + step * index)) for index in range(count)})


def _packet_id(index: int, source: dict[str, Any]) -> str:
    token = str(source.get("type") or source.get("kind") or "packet")
    token = "".join(ch if ch.isalnum() else "_" for ch in token).strip("_") or "packet"
    return f"packet_{index:03d}_{token}_{source.get('start_frame')}_{source.get('end_frame')}"


def _parse_point(x_value: Any, y_value: Any) -> tuple[float, float] | None:
    x = _parse_float(x_value)
    y = _parse_float(y_value)
    if x is None or y is None:
        return None
    return x, y


def _parse_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
        if not math.isfinite(parsed):
            return None
        return int(parsed)
    except (OverflowError, TypeError, ValueError):
        return None


def _parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _frame_count(start_frame: int, end_frame: int) -> int:
    return max(0, end_frame - start_frame + 1)


def _source_represents_lost_gap(source: dict[str, Any]) -> bool:
    if str(source.get("type") or "").casefold() == "lost_gap":
        return True
    values = [source.get("reason")]
    evidence = source.get("evidence")
    if isinstance(evidence, dict):
        values.extend([evidence.get("reason"), evidence.get("window_reason"), evidence.get("trigger_type")])
    return any("lost_gap" in str(value or "").casefold() for value in values)


def _ratio(numerator: int | float, denominator: int | float) -> float:
    if denominator <= 0:
        return 0.0
    return round(float(numerator) / float(denominator), 4)


def _priority_rank(priority: str) -> int:
    return PRIORITY_RANK.get(priority, 0)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _round_or_none(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def _safe_summary_int(value: Any) -> int:
    parsed = _parse_int(value)
    return 0 if parsed is None else max(0, parsed)


def _read_optional_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_nonempty_file(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def _blank_crop() -> Any:
    import numpy as np

    return np.zeros((360, 360, 3), dtype=np.uint8)


def _crop_around(frame: Any, center: tuple[int, int]) -> Any:
    import cv2

    half = 180
    x, y = center
    x1, y1 = max(0, x - half), max(0, y - half)
    x2, y2 = min(frame.shape[1], x + half), min(frame.shape[0], y + half)
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return _blank_crop()
    return cv2.resize(crop, (360, 360), interpolation=cv2.INTER_CUBIC)


def _draw_label(frame: Any, label: str, *, scale: float = 1.2, y: int = 52) -> None:
    import cv2

    cv2.putText(frame, label, (18, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 5, cv2.LINE_AA)
    cv2.putText(frame, label, (18, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 2, cv2.LINE_AA)


def _status_color(status: str) -> tuple[int, int, int]:
    if status == "Detected":
        return 0, 255, 0
    if status == "Predicted":
        return 0, 220, 255
    return 0, 0, 255
