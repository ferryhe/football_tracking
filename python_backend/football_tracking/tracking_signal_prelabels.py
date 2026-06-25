from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from football_tracking.tracking_signal_labels import (
    build_tracking_signal_labels,
    load_tracking_signal_labels,
    write_tracking_signal_labels,
)

_SOURCE_SPECS = (
    ("ball_audit.json", "review_events"),
    ("ai_review_triggers.json", "triggers"),
    ("camera_motion_audit.json", "review_events"),
)

_TYPE_MAPPINGS = {
    "large_jump": ("unknown", "tracking_dynamics", "large_jump_after_reacquire"),
    "lost_gap": ("not_visible", "tracking_dynamics", "lost_gap"),
    "short_tracklet": ("unknown", "tracking_dynamics", "short_false_tracklet"),
    "candidate_ambiguity": ("ambiguous", "tracking_dynamics", "candidate_ambiguity"),
    "dense_noise_cluster": ("unknown", "detector_artifact", "high_recall_noise_cluster"),
    "camera_motion_spike": ("unknown", "camera_motion", "camera_motion_spike"),
    "camera_acceleration_spike": ("unknown", "camera_motion", "camera_acceleration_spike"),
    "camera_zoom_jump": ("unknown", "camera_motion", "camera_zoom_jump"),
}


def build_tracking_signal_prelabels(output_dir: Path) -> dict[str, Any]:
    output_dir = Path(output_dir)
    labels: list[dict[str, Any]] = []
    for artifact_name, list_key in _SOURCE_SPECS:
        payload = _read_optional_json(output_dir / artifact_name)
        items = payload.get(list_key) if isinstance(payload, dict) else None
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            label = _label_for_event(item, artifact_name=artifact_name, index=index)
            if label is not None:
                labels.append(label)
    return build_tracking_signal_labels(output_dir, labels=_dedupe_labels(labels))


def write_tracking_signal_prelabels(output_dir: Path) -> dict[str, Any]:
    output_dir = Path(output_dir)
    existing = load_tracking_signal_labels(output_dir)
    existing_labels = existing.get("labels") if isinstance(existing.get("labels"), list) else []
    prelabels = build_tracking_signal_prelabels(output_dir)["labels"]
    return write_tracking_signal_labels(output_dir, labels=_dedupe_labels([*existing_labels, *prelabels]))


def _dedupe_labels(labels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen_label_ids: set[str] = set()
    seen_source_ids: set[str] = set()
    seen_event_keys: set[tuple[str, int, int]] = set()
    for label in labels:
        label_id = str(label.get("label_id") or "")
        if label_id and label_id in seen_label_ids:
            continue
        source_id = _label_source_id(label)
        if source_id is not None and source_id in seen_source_ids:
            continue
        event_key = _label_event_key(label)
        if event_key is not None and event_key in seen_event_keys:
            continue
        result.append(label)
        if label_id:
            seen_label_ids.add(label_id)
        if source_id is not None:
            seen_source_ids.add(source_id)
        if event_key is not None:
            seen_event_keys.add(event_key)
    return result


def _label_source_id(label: dict[str, Any]) -> str | None:
    evidence = label.get("evidence")
    if not isinstance(evidence, list):
        return None
    for item in evidence:
        if not isinstance(item, dict):
            continue
        source_id = item.get("source_id")
        if isinstance(source_id, str) and source_id.strip():
            return source_id.strip()
    return None


def _label_event_key(label: dict[str, Any]) -> tuple[str, int, int] | None:
    evidence = label.get("evidence")
    if not isinstance(evidence, list):
        return None
    for item in evidence:
        if not isinstance(item, dict):
            continue
        event_type = item.get("event_type")
        start_frame = _parse_int(item.get("start_frame"))
        end_frame = _parse_int(item.get("end_frame"))
        if isinstance(event_type, str) and event_type.strip() and start_frame is not None and end_frame is not None:
            return event_type.strip(), start_frame, end_frame
    return None


def _label_for_event(event: Any, *, artifact_name: str, index: int) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        return None
    event_type = str(event.get("type") or "")
    mapping = _TYPE_MAPPINGS.get(event_type)
    if mapping is None:
        return None
    start_frame = _parse_int(event.get("start_frame"))
    if start_frame is None:
        start_frame = _parse_int(event.get("frame"))
    end_frame = _parse_int(event.get("end_frame"))
    if end_frame is None:
        end_frame = start_frame
    if start_frame is None or end_frame is None:
        return None
    if end_frame < start_frame:
        start_frame, end_frame = end_frame, start_frame

    source_id = _source_id(event, event_type=event_type, start_frame=start_frame, end_frame=end_frame, index=index)
    match_ball_state, category, subtype = mapping
    return {
        "label_id": f"prelabel:{Path(artifact_name).stem}:{_safe_id(source_id)}",
        "candidate_id": source_id,
        "match_ball_state": match_ball_state,
        "interference_category": category,
        "interference_subtype": subtype,
        "evidence": [
            {
                "source_artifact": artifact_name,
                "source_id": source_id,
                "event_type": event_type,
                "start_frame": start_frame,
                "end_frame": end_frame,
                "reason": str(event.get("reason") or event_type),
                "raw_evidence": event.get("evidence") if isinstance(event.get("evidence"), dict) else {},
            }
        ],
    }


def _source_id(event: dict[str, Any], *, event_type: str, start_frame: int, end_frame: int, index: int) -> str:
    for key in ("id", "event_id", "source_id"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"{event_type}:{index}:{start_frame}-{end_frame}"


def _safe_id(value: str) -> str:
    safe = "".join(char if char.isalnum() else "_" for char in value).strip("_")
    return safe or "event"


def _read_optional_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
