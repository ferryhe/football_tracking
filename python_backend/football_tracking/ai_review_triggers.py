from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"
PRIORITIES = ("none", "low", "medium", "high")
EVENT_PRIORITIES = {
    "large_jump": "high",
    "candidate_ambiguity": "medium",
    "postprocess_action": "medium",
}
LONG_LOST_GAP_MIN_FRAMES = 8
DENSE_REVIEW_EVENT_MIN_COUNT = 5
DENSE_SUSPICIOUS_TRACKLET_MIN_COUNT = 4
WINDOW_MERGE_MAX_GAP_FRAMES = 2


def build_ai_review_trigger_report(output_dir: Path) -> dict[str, Any]:
    audit_path = output_dir / "ball_audit.json"
    if not audit_path.exists():
        return _empty_report("ball_audit_missing")

    try:
        audit_report = json.loads(audit_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return _empty_report("ball_audit_invalid")
    if not isinstance(audit_report, dict):
        return _empty_report("ball_audit_invalid")

    review_events = audit_report.get("review_events") if isinstance(audit_report.get("review_events"), list) else []
    tracklets = audit_report.get("tracklets") if isinstance(audit_report.get("tracklets"), list) else []
    audit_summary = audit_report.get("summary") if isinstance(audit_report.get("summary"), dict) else {}

    triggers = _event_triggers(review_events)
    triggers.extend(_tracklet_triggers(tracklets, triggers))
    dense_trigger = _dense_noise_trigger(review_events, tracklets, audit_summary)
    if dense_trigger is not None:
        triggers.append(dense_trigger)
    triggers = sorted(triggers, key=_trigger_sort_key)

    summary = _summary_for_triggers(triggers)
    decision = _decision_for_triggers(triggers, summary["max_trigger_priority"])
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "decision": decision,
        "triggers": triggers,
        "summary": summary,
    }


def write_ai_review_trigger_report(output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_ai_review_trigger_report(output_dir)
    (output_dir / "ai_review_triggers.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def compact_ai_review_trigger_summary(report: dict[str, Any]) -> dict[str, Any] | None:
    decision = report.get("decision")
    summary = report.get("summary")
    if not isinstance(decision, dict) or not isinstance(summary, dict):
        return None
    windows = decision.get("recommended_review_windows")
    return {
        "schema_version": report.get("schema_version"),
        "generated_at": report.get("generated_at"),
        "needs_ai_review": bool(decision.get("needs_ai_review")),
        "priority": decision.get("priority", "none"),
        "reason": decision.get("reason"),
        "trigger_count": int(decision.get("trigger_count") or 0),
        "recommended_window_count": len(windows) if isinstance(windows, list) else 0,
        "counts_by_type": summary.get("counts_by_type") if isinstance(summary.get("counts_by_type"), dict) else {},
        "counts_by_priority": summary.get("counts_by_priority")
        if isinstance(summary.get("counts_by_priority"), dict)
        else {},
        "max_trigger_priority": summary.get("max_trigger_priority", "none"),
    }


def _empty_report(reason: str) -> dict[str, Any]:
    summary = {
        "counts_by_type": {},
        "counts_by_priority": {},
        "max_trigger_priority": "none",
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "decision": {
            "needs_ai_review": False,
            "priority": "none",
            "reason": reason,
            "trigger_count": 0,
            "recommended_review_windows": [],
        },
        "triggers": [],
        "summary": summary,
    }


def _event_triggers(review_events: list[Any]) -> list[dict[str, Any]]:
    triggers: list[dict[str, Any]] = []
    for index, raw_event in enumerate(review_events):
        if not isinstance(raw_event, dict):
            continue
        event_type = str(raw_event.get("type") or "")
        priority = _priority_for_event(raw_event)
        if priority is None:
            continue
        start_frame, end_frame, frame_count = _window_from_item(raw_event)
        triggers.append(
            {
                "id": f"event:{index}:{event_type}:{_frame_token(start_frame)}-{_frame_token(end_frame)}",
                "type": event_type,
                "priority": priority,
                "source": str(raw_event.get("source") or "ball_audit"),
                "start_frame": start_frame,
                "end_frame": end_frame,
                "frame_count": frame_count,
                "reason": str(raw_event.get("reason") or _default_reason(event_type)),
                "evidence": _event_evidence(raw_event, index),
            }
        )
    return triggers


def _tracklet_triggers(tracklets: list[Any], existing_triggers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    represented_windows: dict[tuple[Any, Any, Any], str] = {}
    for trigger in existing_triggers:
        key = (trigger.get("source"), trigger.get("start_frame"), trigger.get("end_frame"))
        priority = str(trigger.get("priority") or "none")
        if _priority_rank(priority) > _priority_rank(represented_windows.get(key, "none")):
            represented_windows[key] = priority
    triggers: list[dict[str, Any]] = []
    for raw_tracklet in tracklets:
        if not isinstance(raw_tracklet, dict):
            continue
        score = _parse_float(raw_tracklet.get("suspicion_score"))
        if score is None or score < 0.35:
            continue
        start_frame, end_frame, frame_count = _window_from_item(raw_tracklet)
        source = str(raw_tracklet.get("source") or "ball_audit")
        priority = "high" if score >= 0.7 else "low"
        represented_priority = represented_windows.get((source, start_frame, end_frame))
        if represented_priority is not None and _priority_rank(represented_priority) >= _priority_rank(priority):
            continue
        tracklet_id = str(raw_tracklet.get("id") or f"{source}:{_frame_token(start_frame)}-{_frame_token(end_frame)}")
        triggers.append(
            {
                "id": f"tracklet:{tracklet_id}",
                "type": "suspicious_tracklet",
                "priority": priority,
                "source": source,
                "start_frame": start_frame,
                "end_frame": end_frame,
                "frame_count": frame_count,
                "reason": f"Tracklet suspicion score {score:.2f} crosses the deterministic review threshold.",
                "evidence": {
                    "tracklet_id": tracklet_id,
                    "suspicion_score": round(score, 4),
                    "flags": raw_tracklet.get("flags") if isinstance(raw_tracklet.get("flags"), list) else [],
                    "mean_confidence": raw_tracklet.get("mean_confidence"),
                    "max_step_px": raw_tracklet.get("max_step_px"),
                },
            }
        )
    return triggers


def _dense_noise_trigger(
    review_events: list[Any],
    tracklets: list[Any],
    audit_summary: dict[str, Any],
) -> dict[str, Any] | None:
    review_event_count = _parse_int(audit_summary.get("review_event_count"))
    if review_event_count is None:
        review_event_count = sum(1 for item in review_events if isinstance(item, dict))
    suspicious_tracklet_count = _parse_int(audit_summary.get("suspicious_tracklet_count"))
    if suspicious_tracklet_count is None:
        suspicious_tracklet_count = sum(
            1
            for item in tracklets
            if isinstance(item, dict) and (_parse_float(item.get("suspicion_score")) or 0.0) >= 0.35
        )
    if review_event_count < DENSE_REVIEW_EVENT_MIN_COUNT and suspicious_tracklet_count < DENSE_SUSPICIOUS_TRACKLET_MIN_COUNT:
        return None

    window_items = [item for item in review_events if isinstance(item, dict)]
    if not window_items:
        window_items = [item for item in tracklets if isinstance(item, dict)]
    start_frame, end_frame = _min_max_window(window_items)
    frame_count = _frame_count(start_frame, end_frame, None)
    return {
        "id": f"dense_noise_cluster:{_frame_token(start_frame)}-{_frame_token(end_frame)}",
        "type": "dense_noise_cluster",
        "priority": "high",
        "source": "ball_audit",
        "start_frame": start_frame,
        "end_frame": end_frame,
        "frame_count": frame_count,
        "reason": "Dense audit signals indicate a noisy segment that should be reviewed before trusting automation.",
        "evidence": {
            "review_event_count": review_event_count,
            "suspicious_tracklet_count": suspicious_tracklet_count,
        },
    }


def _priority_for_event(event: dict[str, Any]) -> str | None:
    event_type = str(event.get("type") or "")
    if event_type == "lost_gap":
        frame_count = _parse_int(event.get("frame_count")) or 0
        return "medium" if frame_count >= LONG_LOST_GAP_MIN_FRAMES else "low"
    return EVENT_PRIORITIES.get(event_type)


def _summary_for_triggers(triggers: list[dict[str, Any]]) -> dict[str, Any]:
    counts_by_type = Counter(str(trigger.get("type") or "") for trigger in triggers)
    counts_by_priority = Counter(str(trigger.get("priority") or "none") for trigger in triggers)
    max_priority = _max_priority(trigger.get("priority") for trigger in triggers)
    return {
        "counts_by_type": dict(sorted(counts_by_type.items())),
        "counts_by_priority": dict(sorted(counts_by_priority.items())),
        "max_trigger_priority": max_priority,
    }


def _decision_for_triggers(triggers: list[dict[str, Any]], max_priority: str) -> dict[str, Any]:
    if not triggers:
        return {
            "needs_ai_review": False,
            "priority": "none",
            "reason": "no_triggers",
            "trigger_count": 0,
            "recommended_review_windows": [],
        }
    return {
        "needs_ai_review": True,
        "priority": max_priority,
        "reason": f"{max_priority}_priority_triggers",
        "trigger_count": len(triggers),
        "recommended_review_windows": _recommended_windows(triggers),
    }


def _recommended_windows(triggers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    windows = [
        {
            "start_frame": trigger["start_frame"],
            "end_frame": trigger["end_frame"],
            "reasons": [str(trigger.get("type") or trigger.get("reason") or "review")],
        }
        for trigger in triggers
        if isinstance(trigger.get("start_frame"), int) and isinstance(trigger.get("end_frame"), int)
    ]
    windows.sort(key=lambda item: (item["start_frame"], item["end_frame"]))
    merged: list[dict[str, Any]] = []
    for window in windows:
        if not merged:
            merged.append(window)
            continue
        previous = merged[-1]
        gap = window["start_frame"] - previous["end_frame"] - 1
        if gap <= WINDOW_MERGE_MAX_GAP_FRAMES:
            previous["end_frame"] = max(previous["end_frame"], window["end_frame"])
            for reason in window["reasons"]:
                if reason not in previous["reasons"]:
                    previous["reasons"].append(reason)
            continue
        merged.append(window)
    return [
        {
            "start_frame": item["start_frame"],
            "end_frame": item["end_frame"],
            "reason": "; ".join(item["reasons"]),
        }
        for item in merged
    ]


def _event_evidence(event: dict[str, Any], index: int) -> dict[str, Any]:
    evidence = event.get("evidence")
    payload = evidence.copy() if isinstance(evidence, dict) else {}
    payload["review_event_index"] = index
    return payload


def _window_from_item(item: dict[str, Any]) -> tuple[int | None, int | None, int]:
    start_frame = _parse_int(item.get("start_frame"))
    end_frame = _parse_int(item.get("end_frame"))
    if end_frame is None:
        end_frame = start_frame
    if start_frame is None:
        start_frame = end_frame
    return start_frame, end_frame, _frame_count(start_frame, end_frame, _parse_int(item.get("frame_count") or item.get("length")))


def _min_max_window(items: list[dict[str, Any]]) -> tuple[int | None, int | None]:
    starts: list[int] = []
    ends: list[int] = []
    for item in items:
        start_frame, end_frame, _ = _window_from_item(item)
        if start_frame is not None:
            starts.append(start_frame)
        if end_frame is not None:
            ends.append(end_frame)
    if not starts or not ends:
        return None, None
    return min(starts), max(ends)


def _frame_count(start_frame: int | None, end_frame: int | None, fallback: int | None) -> int:
    if fallback is not None and fallback > 0:
        return fallback
    if isinstance(start_frame, int) and isinstance(end_frame, int):
        return max(1, end_frame - start_frame + 1)
    return 1


def _trigger_sort_key(trigger: dict[str, Any]) -> tuple[int, int, str, str]:
    start_frame = trigger.get("start_frame")
    frame_sort = start_frame if isinstance(start_frame, int) else 10**12
    return (
        frame_sort,
        _priority_rank(str(trigger.get("priority") or "none")),
        str(trigger.get("type") or ""),
        str(trigger.get("id") or ""),
    )


def _max_priority(priorities: Any) -> str:
    best = "none"
    for priority in priorities:
        current = str(priority or "none")
        if _priority_rank(current) > _priority_rank(best):
            best = current
    return best if best in PRIORITIES else "none"


def _priority_rank(priority: str) -> int:
    try:
        return PRIORITIES.index(priority)
    except ValueError:
        return 0


def _default_reason(trigger_type: str) -> str:
    return f"{trigger_type or 'audit'} trigger requires review."


def _frame_token(value: int | None) -> str:
    return "none" if value is None else str(value)


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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
