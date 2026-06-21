from __future__ import annotations

import csv
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"
INPUT_FILE_NAME = "player_detections.jsonl"
JSON_ARTIFACT_NAME = "player_tracks.json"
CSV_ARTIFACT_NAME = "player_tracks.csv"
PLAYER_LABELS = {"person", "player", "referee"}
MAX_FRAME_GAP = 2
MAX_DISTANCE_PX = 90.0


def build_player_tracks_report(output_dir: Path) -> dict[str, Any]:
    detections_path = output_dir / INPUT_FILE_NAME
    detections, malformed_line_count = _read_detections(detections_path)
    tracks = _public_tracks(_build_tracks(detections))
    source_status = _source_status(detections_path, detections)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "source": {
            "path": INPUT_FILE_NAME,
            "status": source_status,
            "detection_count": len(detections),
            "malformed_line_count": malformed_line_count,
        },
        "summary": _summary(detections, tracks),
        "tracks": tracks,
    }


def write_player_tracks_artifacts(output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_player_tracks_report(output_dir)
    (output_dir / JSON_ARTIFACT_NAME).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if any(track.get("samples") for track in report["tracks"]):
        _write_csv(output_dir / CSV_ARTIFACT_NAME, report["tracks"])
    return report


def compact_player_tracks_summary(report: dict[str, Any]) -> dict[str, Any] | None:
    summary = report.get("summary")
    if not isinstance(summary, dict):
        return None
    compact = {
        "schema_version": report.get("schema_version"),
        "generated_at": report.get("generated_at"),
    }
    compact.update(summary)
    return compact


def _read_detections(path: Path) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], 0

    detections: list[dict[str, Any]] = []
    malformed_line_count = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_index, line in enumerate(handle):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    malformed_line_count += 1
                    continue
                if not isinstance(row, dict):
                    malformed_line_count += 1
                    continue
                if "detections" in row and not isinstance(row.get("detections"), list):
                    malformed_line_count += 1
                    continue
                for raw_detection in _line_detections(row):
                    detection = _normalize_detection(raw_detection, order=len(detections), line_index=line_index)
                    if detection is not None:
                        detections.append(detection)
    except (OSError, UnicodeDecodeError):
        return [], 1

    detections.sort(key=lambda item: (item["frame"], item["foot_point"][0], item["foot_point"][1], item["order"]))
    return detections, malformed_line_count


def _line_detections(row: dict[str, Any]) -> list[dict[str, Any]]:
    detections = row.get("detections")
    if isinstance(detections, list):
        prepared: list[dict[str, Any]] = []
        for item in detections:
            if not isinstance(item, dict):
                continue
            detection = item.copy()
            detection.setdefault("frame", row.get("frame"))
            prepared.append(detection)
        return prepared
    return [row]


def _normalize_detection(raw: dict[str, Any], *, order: int, line_index: int) -> dict[str, Any] | None:
    frame = _parse_int(raw.get("frame"))
    if frame is None:
        return None

    label = str(raw.get("label") or "").strip().lower()
    if label not in PLAYER_LABELS:
        return None

    bbox = _parse_bbox(raw.get("bbox"))
    confidence = _parse_float(raw.get("confidence"))
    if bbox is None or confidence is None:
        return None

    team = str(raw.get("team") or "unknown").strip().lower() or "unknown"
    foot_point = ((bbox[0] + bbox[2]) / 2.0, bbox[3])
    return {
        "frame": frame,
        "bbox": bbox,
        "confidence": confidence,
        "label": label,
        "team": team,
        "foot_point": foot_point,
        "order": order,
        "line_index": line_index,
    }


def _build_tracks(detections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tracks: list[dict[str, Any]] = []
    grouped: dict[int, list[dict[str, Any]]] = {}
    for detection in detections:
        grouped.setdefault(detection["frame"], []).append(detection)

    next_track_order = 0
    for frame in sorted(grouped):
        assigned_track_orders: set[int] = set()
        frame_detections = sorted(
            grouped[frame],
            key=lambda item: (item["foot_point"][0], item["foot_point"][1], item["order"]),
        )
        for detection in frame_detections:
            match = _best_track_match(detection, tracks, assigned_track_orders)
            if match is None:
                tracks.append({"track_order": next_track_order, "samples": [detection]})
                next_track_order += 1
                continue
            match["samples"].append(detection)
            assigned_track_orders.add(match["track_order"])
    return tracks


def _best_track_match(
    detection: dict[str, Any],
    tracks: list[dict[str, Any]],
    assigned_track_orders: set[int],
) -> dict[str, Any] | None:
    candidates: list[tuple[float, int, int, dict[str, Any]]] = []
    for track in tracks:
        if track["track_order"] in assigned_track_orders:
            continue
        last_sample = track["samples"][-1]
        frame_gap = detection["frame"] - last_sample["frame"]
        if frame_gap < 1 or frame_gap > MAX_FRAME_GAP:
            continue
        distance = math.dist(detection["foot_point"], last_sample["foot_point"])
        if distance > MAX_DISTANCE_PX:
            continue
        first_sample = track["samples"][0]
        candidates.append((distance, frame_gap, first_sample["order"], track))
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item[0], item[1], item[2], item[3]["track_order"]))[3]


def _public_tracks(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered_tracks = sorted(tracks, key=_track_sort_key)
    public_tracks: list[dict[str, Any]] = []
    for index, track in enumerate(ordered_tracks, start=1):
        samples = sorted(track["samples"], key=lambda item: (item["frame"], item["order"]))
        public_samples = [_public_sample(sample) for sample in samples]
        step_lengths = [
            math.dist(previous["foot_point"], current["foot_point"])
            for previous, current in zip(samples, samples[1:])
        ]
        public_tracks.append(
            {
                "id": f"P{index:03d}",
                "start_frame": samples[0]["frame"],
                "end_frame": samples[-1]["frame"],
                "length": len(samples),
                "team": _track_team(samples),
                "mean_confidence": _round(_mean([sample["confidence"] for sample in samples]), 4),
                "first_foot_point": _point_payload(samples[0]["foot_point"]),
                "last_foot_point": _point_payload(samples[-1]["foot_point"]),
                "max_step_px": _round(max(step_lengths), 2) if step_lengths else None,
                "samples": public_samples,
            }
        )
    return public_tracks


def _track_sort_key(track: dict[str, Any]) -> tuple[int, float, float, int]:
    first_sample = track["samples"][0]
    return (
        first_sample["frame"],
        first_sample["foot_point"][0],
        first_sample["foot_point"][1],
        track["track_order"],
    )


def _public_sample(sample: dict[str, Any]) -> dict[str, Any]:
    return {
        "frame": sample["frame"],
        "bbox": [_round(value, 2) for value in sample["bbox"]],
        "foot_point": _point_payload(sample["foot_point"]),
        "confidence": _round(sample["confidence"], 4),
        "label": sample["label"],
        "team": sample["team"],
    }


def _track_team(samples: list[dict[str, Any]]) -> str:
    teams = [sample["team"] for sample in samples if sample.get("team") and sample["team"] != "unknown"]
    if not teams:
        return "unknown"
    counts = Counter(teams)
    first_index = {team: teams.index(team) for team in counts}
    return min(counts, key=lambda team: (-counts[team], first_index[team]))


def _summary(detections: list[dict[str, Any]], tracks: list[dict[str, Any]]) -> dict[str, Any]:
    track_lengths = [track["length"] for track in tracks]
    team_counts = Counter(str(track.get("team") or "unknown") for track in tracks)
    return {
        "frame_count": len({detection["frame"] for detection in detections}),
        "detection_count": len(detections),
        "track_count": len(tracks),
        "active_track_count": sum(1 for length in track_lengths if length >= 2),
        "mean_track_length": _round(_mean(track_lengths), 4) if track_lengths else 0.0,
        "longest_track_length": max(track_lengths, default=0),
        "teams": dict(sorted(team_counts.items())),
    }


def _write_csv(path: Path, tracks: list[dict[str, Any]]) -> None:
    fieldnames = ["Frame", "TrackId", "FootX", "FootY", "X1", "Y1", "X2", "Y2", "Confidence", "Label", "Team"]
    rows: list[dict[str, Any]] = []
    for track in tracks:
        for sample in track["samples"]:
            bbox = sample["bbox"]
            foot_point = sample["foot_point"]
            rows.append(
                {
                    "Frame": sample["frame"],
                    "TrackId": track["id"],
                    "FootX": f"{foot_point['x']:.2f}",
                    "FootY": f"{foot_point['y']:.2f}",
                    "X1": f"{bbox[0]:.2f}",
                    "Y1": f"{bbox[1]:.2f}",
                    "X2": f"{bbox[2]:.2f}",
                    "Y2": f"{bbox[3]:.2f}",
                    "Confidence": f"{sample['confidence']:.4f}",
                    "Label": sample["label"],
                    "Team": sample["team"],
                }
            )
    rows.sort(key=lambda item: (int(item["Frame"]), item["TrackId"]))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _source_status(path: Path, detections: list[dict[str, Any]]) -> str:
    if not path.exists():
        return "missing"
    return "loaded" if detections else "empty"


def _parse_bbox(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    bbox: list[float] = []
    for item in value:
        parsed = _parse_float(item)
        if parsed is None:
            return None
        bbox.append(parsed)
    if bbox[2] < bbox[0] or bbox[3] < bbox[1]:
        return None
    return bbox


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


def _point_payload(point: tuple[float, float]) -> dict[str, float]:
    return {"x": _round(point[0], 2), "y": _round(point[1], 2)}


def _mean(values: list[float] | list[int]) -> float:
    return float(sum(values) / len(values))


def _round(value: float, digits: int) -> float:
    return round(float(value), digits)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
