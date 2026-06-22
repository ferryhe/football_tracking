from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
DEFAULT_MERGE_GAP_FRAMES = 30
DEFAULT_MAX_TOTAL_FRAMES = 1800
LONG_LOST_GAP_MIN_FRAMES = 120
PRIORITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}


def build_high_recall_windows(
    output_dir: Path,
    *,
    margin_frames: int = 0,
    merge_gap_frames: int = DEFAULT_MERGE_GAP_FRAMES,
    max_total_frames: int | None = DEFAULT_MAX_TOTAL_FRAMES,
    total_frames: int | None = None,
    mode: str = "sahi",
    approved_actions_path: Path | None = None,
) -> dict[str, Any]:
    """Build high-recall rerun windows from review/audit/event reports."""
    safe_margin = max(0, int(margin_frames))
    safe_merge_gap = min(DEFAULT_MERGE_GAP_FRAMES, max(0, int(merge_gap_frames)))
    safe_max_total_frames = _positive_budget_or_none(max_total_frames)
    safe_total_frames = _positive_int_or_none(total_frames)

    candidates = _collect_candidate_windows(
        output_dir=output_dir,
        margin_frames=safe_margin,
        total_frames=safe_total_frames,
        mode=str(mode or "sahi"),
        approved_actions_path=approved_actions_path,
    )
    budgeted_candidates, budget_rejected_windows, _ = _apply_frame_budget(
        candidates,
        max_total_frames=safe_max_total_frames,
    )
    merged_windows = _merge_windows(
        budgeted_candidates,
        merge_gap_frames=safe_merge_gap,
        max_window_frames=safe_max_total_frames,
    )
    selected_windows, rejected_windows, status = _apply_frame_budget(
        merged_windows,
        max_total_frames=safe_max_total_frames,
    )
    rejected_windows = [*budget_rejected_windows, *rejected_windows]
    status = _plan_status(selected_windows, rejected_windows)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "settings": {
            "margin_frames": safe_margin,
            "merge_gap_frames": safe_merge_gap,
            "max_total_frames": safe_max_total_frames,
            "total_frames": safe_total_frames,
            "mode": str(mode or "sahi"),
            "approved_actions_path": None if approved_actions_path is None else str(Path(approved_actions_path).resolve()),
        },
        "windows": selected_windows,
        "rejected_windows": rejected_windows,
        "summary": {
            "status": status,
            "candidate_window_count": len(candidates),
            "merged_window_count": len(merged_windows),
            "selected_window_count": len(selected_windows),
            "selected_total_frames": _total_window_frames(selected_windows),
            "rejected_count": len(rejected_windows),
        },
    }


def write_high_recall_window_report(
    output_dir: Path,
    *,
    output_dir_name: str = "high_recall_windows",
    **kwargs: Any,
) -> dict[str, Any]:
    report = build_high_recall_windows(output_dir, **kwargs)
    report_dir = output_dir / output_dir_name
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def _collect_candidate_windows(
    *,
    output_dir: Path,
    margin_frames: int,
    total_frames: int | None,
    mode: str,
    approved_actions_path: Path | None,
) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    windows.extend(
        _normalize_windows(
            _ai_review_trigger_windows(_read_optional_json(output_dir / "ai_review_triggers.json")),
            margin_frames=margin_frames,
            total_frames=total_frames,
            mode=mode,
        )
    )
    windows.extend(
        _normalize_windows(
            _ball_audit_windows(_read_optional_json(output_dir / "ball_audit.json")),
            margin_frames=margin_frames,
            total_frames=total_frames,
            mode=mode,
        )
    )
    windows.extend(
        _normalize_windows(
            _event_candidate_windows(_read_optional_json(output_dir / "event_candidates.json")),
            margin_frames=margin_frames,
            total_frames=total_frames,
            mode=mode,
        )
    )
    if approved_actions_path is not None:
        windows.extend(
            _normalize_windows(
                _approved_action_windows(_read_required_approved_actions(Path(approved_actions_path))),
                margin_frames=margin_frames,
                total_frames=total_frames,
                mode=mode,
            )
        )
    return sorted(windows, key=lambda item: (item["start_frame"], item["end_frame"], -_priority_rank(item["priority"])))


def _ai_review_trigger_windows(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(report, dict):
        return []

    windows: list[dict[str, Any]] = []
    decision = report.get("decision")
    if isinstance(decision, dict):
        decision_priority = _normalize_priority(decision.get("priority"), "medium")
        recommended = decision.get("recommended_review_windows")
        if isinstance(recommended, list):
            for item in recommended:
                if not isinstance(item, dict):
                    continue
                windows.append(
                    _raw_window(
                        item,
                        source="ai_review_triggers",
                        priority=decision_priority,
                        reason=f"ai_review: {item.get('reason') or decision.get('reason') or 'recommended_review'}",
                    )
                )

    triggers = report.get("triggers")
    if isinstance(triggers, list):
        for trigger in triggers:
            if not isinstance(trigger, dict):
                continue
            if str(trigger.get("type") or "") == "dense_noise_cluster":
                continue
            windows.append(
                _raw_window(
                    trigger,
                    source="ai_review_triggers",
                    priority=_normalize_priority(trigger.get("priority"), "medium"),
                    reason=f"ai_review: {trigger.get('type') or trigger.get('reason') or 'trigger'}",
                )
            )
    return windows


def _ball_audit_windows(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(report, dict):
        return []

    windows: list[dict[str, Any]] = []
    review_events = report.get("review_events")
    if isinstance(review_events, list):
        for event in review_events:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or "review_event")
            windows.append(
                _raw_window(
                    event,
                    source="ball_audit",
                    priority=_ball_audit_event_priority(event),
                    reason=f"ball_audit: {event_type}",
                )
            )

    tracklets = report.get("tracklets")
    if isinstance(tracklets, list):
        for tracklet in tracklets:
            if not isinstance(tracklet, dict) or not _is_suspicious_tracklet(tracklet):
                continue
            tracklet_id = tracklet.get("id") or f"{tracklet.get('start_frame')}-{tracklet.get('end_frame')}"
            windows.append(
                _raw_window(
                    tracklet,
                    source="ball_audit",
                    priority=_tracklet_priority(tracklet),
                    reason=f"ball_audit: suspicious_tracklet:{tracklet_id}",
                )
            )
    return windows


def _event_candidate_windows(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(report, dict):
        return []

    windows: list[dict[str, Any]] = []
    candidates = report.get("candidates")
    if not isinstance(candidates, list):
        return windows
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        raw_window = candidate.get("render_window") if isinstance(candidate.get("render_window"), dict) else candidate
        event_type = str(candidate.get("type") or "event_candidate")
        raw_payload = dict(raw_window)
        raw_payload.setdefault("reason", candidate.get("reason"))
        windows.append(
            _raw_window(
                raw_payload,
                source="event_candidates",
                priority=_event_candidate_priority(candidate),
                reason=f"event_candidates: {event_type}",
            )
        )
    return windows


def _approved_action_windows(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(report, dict):
        return []
    actions = report.get("approved_actions")
    if not isinstance(actions, list):
        raise ValueError("ai_improvement_approved_actions.json invalid: approved_actions must be a list.")

    windows: list[dict[str, Any]] = []
    for index, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            raise ValueError(f"ai_improvement_approved_actions.json invalid: approved_actions[{index}] must be an object.")
        if action.get("approved_action") != "targeted_rerun":
            continue
        action_label = str(action.get("approval_id") or action.get("improvement_id") or index)
        approval_id = _required_string(action.get("approval_id"), f"approved targeted_rerun action {action_label} approval_id")
        improvement_id = _required_string(
            action.get("improvement_id"),
            f"approved targeted_rerun action {action_label} improvement_id",
        )
        rerun_scope = action.get("rerun_scope")
        rerun_window = _required_frame_window(
            rerun_scope,
            f"approved targeted_rerun action {action_label} rerun_scope",
        )
        raw_payload = dict(rerun_window)
        raw_payload.update(
            {
                "approval_id": approval_id,
                "improvement_id": improvement_id,
                "approval_source": action.get("approval_source"),
                "approved_action": action.get("approved_action"),
                "rerun_scope": rerun_window,
                "source_packet_id": action.get("source_packet_id"),
                "visual_review_id": action.get("visual_review_id"),
                "local_search_roi": action.get("local_search_roi"),
                "provenance": action.get("provenance"),
            }
        )
        windows.append(
            _raw_window(
                raw_payload,
                source="ai_improvement",
                priority="none",
                reason=f"ai_improvement: approved targeted_rerun:{action.get('improvement_id') or 'unknown'}",
            )
        )
    return windows


def _raw_window(item: dict[str, Any], *, source: str, priority: str, reason: str) -> dict[str, Any]:
    window = {
        "start_frame": item.get("start_frame"),
        "end_frame": item.get("end_frame"),
        "source": source,
        "priority": priority,
        "reason": reason,
    }
    for key in (
        "approval_id",
        "improvement_id",
        "approval_source",
        "approved_action",
        "rerun_scope",
        "source_packet_id",
        "visual_review_id",
        "local_search_roi",
        "provenance",
    ):
        if item.get(key) not in (None, "", {}):
            window[key] = item[key]
    return window


def _normalize_windows(
    raw_windows: list[dict[str, Any]],
    *,
    margin_frames: int,
    total_frames: int | None,
    mode: str,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw_window in raw_windows:
        start_frame = _parse_int(raw_window.get("start_frame"))
        end_frame = _parse_int(raw_window.get("end_frame"))
        if start_frame is None and end_frame is None:
            continue
        if start_frame is None:
            start_frame = end_frame
        if end_frame is None:
            end_frame = start_frame
        if start_frame is None or end_frame is None:
            continue
        if end_frame < start_frame:
            start_frame, end_frame = end_frame, start_frame
        start_frame = max(0, start_frame - margin_frames)
        end_frame = end_frame + margin_frames
        if total_frames is not None:
            if start_frame >= total_frames:
                continue
            end_frame = min(total_frames - 1, end_frame)
        if end_frame < start_frame:
            continue
        source = str(raw_window.get("source") or "unknown")
        reason = str(raw_window.get("reason") or source)
        normalized.append(
            {
                "start_frame": start_frame,
                "end_frame": end_frame,
                "reason": reason,
                "mode": mode,
                "priority": _normalize_priority(raw_window.get("priority"), "medium"),
                "sources": [source],
                **_window_metadata(raw_window),
            }
        )
    return normalized


def _window_metadata(raw_window: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key in (
        "approval_id",
        "improvement_id",
        "approval_source",
        "approved_action",
        "rerun_scope",
        "source_packet_id",
        "visual_review_id",
        "local_search_roi",
        "provenance",
    ):
        if raw_window.get(key) not in (None, "", {}):
            metadata[key] = raw_window[key]
    approval_entry = _approval_provenance_entry(raw_window)
    if approval_entry:
        metadata["approval_provenance"] = [approval_entry]
    return metadata


def _approval_provenance_entry(window: dict[str, Any]) -> dict[str, Any] | None:
    if window.get("approval_id") in (None, "") and window.get("improvement_id") in (None, ""):
        return None
    entry: dict[str, Any] = {}
    for key in (
        "approval_id",
        "improvement_id",
        "approval_source",
        "approved_action",
        "rerun_scope",
        "source_packet_id",
        "visual_review_id",
        "local_search_roi",
        "provenance",
    ):
        if window.get(key) not in (None, "", {}):
            entry[key] = window[key]
    return entry or None


def _merge_windows(
    windows: list[dict[str, Any]],
    *,
    merge_gap_frames: int,
    max_window_frames: int | None = None,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for window in sorted(windows, key=lambda item: (item["start_frame"], item["end_frame"])):
        if not merged:
            merged.append(_copy_window(window))
            continue
        previous = merged[-1]
        gap = window["start_frame"] - previous["end_frame"] - 1
        if gap > merge_gap_frames:
            merged.append(_copy_window(window))
            continue
        merged_end = max(previous["end_frame"], window["end_frame"])
        if max_window_frames is not None and merged_end - previous["start_frame"] + 1 > max_window_frames:
            merged.append(_copy_window(window))
            continue
        previous["end_frame"] = max(previous["end_frame"], window["end_frame"])
        previous["priority"] = _max_priority(previous["priority"], window["priority"])
        previous["reason"] = _join_unique_reasons(previous["reason"], window["reason"])
        previous["sources"] = _append_unique(previous["sources"], window["sources"])
        _merge_window_metadata(previous, window)
        if previous.get("mode") != window.get("mode"):
            previous["mode"] = f"{previous.get('mode')},{window.get('mode')}"
    return merged


def _merge_window_metadata(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    target["approval_provenance"] = _merge_approval_provenance(
        target.get("approval_provenance"),
        incoming.get("approval_provenance"),
    )
    if not target["approval_provenance"]:
        target.pop("approval_provenance", None)
    elif not isinstance(target.get("approval_id"), str):
        first = target["approval_provenance"][0]
        for key in (
            "approval_id",
            "improvement_id",
            "approval_source",
            "approved_action",
            "rerun_scope",
            "source_packet_id",
            "visual_review_id",
            "local_search_roi",
            "provenance",
        ):
            if first.get(key) not in (None, "", {}):
                target[key] = first[key]


def _merge_approval_provenance(existing: Any, incoming: Any) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source in (existing, incoming):
        if not isinstance(source, list):
            continue
        for item in source:
            if not isinstance(item, dict):
                continue
            key = (str(item.get("approval_id") or ""), str(item.get("improvement_id") or ""))
            if key in seen:
                continue
            seen.add(key)
            merged.append(dict(item))
    return merged


def _apply_frame_budget(
    windows: list[dict[str, Any]],
    *,
    max_total_frames: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    if max_total_frames is None or _total_window_frames(windows) <= max_total_frames:
        return [_copy_window(window) for window in windows], [], "succeeded"

    selected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    used_frames = 0
    priority_order = sorted(
        windows,
        key=lambda item: (-_budget_priority_rank(item), _window_frame_count(item), item["start_frame"]),
    )
    for window in priority_order:
        window_frames = _window_frame_count(window)
        if used_frames + window_frames <= max_total_frames:
            selected.append(_copy_window(window))
            used_frames += window_frames
        else:
            rejected_window = _copy_window(window)
            rejected_window["rejection_reason"] = "max_total_frames_exceeded"
            rejected.append(rejected_window)

    selected.sort(key=lambda item: (item["start_frame"], item["end_frame"]))
    rejected.sort(key=lambda item: (item["start_frame"], item["end_frame"]))
    return selected, rejected, "capped" if selected else "rejected"


def _plan_status(selected: list[dict[str, Any]], rejected: list[dict[str, Any]]) -> str:
    if rejected:
        return "capped" if selected else "rejected"
    return "succeeded"


def _copy_window(window: dict[str, Any]) -> dict[str, Any]:
    copied = dict(window)
    copied["sources"] = list(window.get("sources") or [])
    if isinstance(window.get("approval_provenance"), list):
        copied["approval_provenance"] = [
            dict(item) for item in window["approval_provenance"] if isinstance(item, dict)
        ]
    return copied


def _budget_priority_rank(window: dict[str, Any]) -> int:
    rank = _priority_rank(str(window.get("priority") or "none"))
    if _is_long_lost_gap_window(window):
        rank += len(PRIORITY_RANK)
    return rank


def _is_long_lost_gap_window(window: dict[str, Any]) -> bool:
    reason = str(window.get("reason") or "").lower()
    return "lost_gap" in reason and _window_frame_count(window) >= LONG_LOST_GAP_MIN_FRAMES


def _ball_audit_event_priority(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or "")
    severity = str(event.get("severity") or "").casefold()
    if event_type == "large_jump" or severity == "fail":
        return "high"
    if event_type in {"lost_gap", "candidate_ambiguity", "postprocess_action"} or severity == "warn":
        return "medium"
    return "low"


def _is_suspicious_tracklet(tracklet: dict[str, Any]) -> bool:
    flags = tracklet.get("flags")
    if isinstance(flags, list) and flags:
        return True
    score = _parse_float(tracklet.get("suspicion_score"))
    return score is not None and score >= 0.35


def _tracklet_priority(tracklet: dict[str, Any]) -> str:
    score = _parse_float(tracklet.get("suspicion_score")) or 0.0
    flags = tracklet.get("flags") if isinstance(tracklet.get("flags"), list) else []
    if score >= 0.7 or "large_jump" in flags:
        return "high"
    if score >= 0.35 or flags:
        return "medium"
    return "low"


def _event_candidate_priority(candidate: dict[str, Any]) -> str:
    score = _parse_float(candidate.get("score")) or 0.0
    event_type = str(candidate.get("type") or "")
    if event_type == "goal_candidate" or score >= 0.75:
        return "high"
    if score >= 0.55:
        return "medium"
    return "low"


def _normalize_priority(value: Any, default: str) -> str:
    priority = str(value or default).strip().lower()
    return priority if priority in PRIORITY_RANK else default


def _max_priority(first: str, second: str) -> str:
    return first if _priority_rank(first) >= _priority_rank(second) else second


def _priority_rank(priority: str) -> int:
    return PRIORITY_RANK.get(str(priority or "none").lower(), 0)


def _join_unique_reasons(first: str, second: str) -> str:
    reasons: list[str] = []
    for reason in [*str(first).split("; "), *str(second).split("; ")]:
        if reason and reason not in reasons:
            reasons.append(reason)
    return "; ".join(reasons)


def _append_unique(existing: list[str], incoming: list[str]) -> list[str]:
    values = list(existing)
    for value in incoming:
        if value not in values:
            values.append(value)
    return values


def _window_frame_count(window: dict[str, Any]) -> int:
    return max(0, int(window["end_frame"]) - int(window["start_frame"]) + 1)


def _total_window_frames(windows: list[dict[str, Any]]) -> int:
    return sum(_window_frame_count(window) for window in windows)


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _read_required_approved_actions(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"{path.name} missing: pass an existing approved actions artifact path.") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path.name} corrupt: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"{path.name} invalid: expected JSON object.")
    if not isinstance(loaded.get("approved_actions"), list):
        raise ValueError(f"{path.name} invalid: approved_actions must be a list.")
    return loaded


def _required_frame_window(value: Any, label: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} requires rerun_scope with start_frame and end_frame.")
    start_frame = _strict_int(value.get("start_frame"))
    end_frame = _strict_int(value.get("end_frame"))
    if start_frame is None or end_frame is None:
        raise ValueError(f"{label} requires integer start_frame and end_frame.")
    if start_frame < 0 or end_frame < 0:
        raise ValueError(f"{label} frames must be non-negative.")
    if end_frame < start_frame:
        raise ValueError(f"{label}.end_frame must be greater than or equal to start_frame.")
    return {"start_frame": start_frame, "end_frame": end_frame}


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required.")
    return value.strip()


def _strict_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and stripped.lstrip("-").isdigit():
            return int(stripped)
    return None


def _positive_int_or_none(value: int | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(float(value))
    except (OverflowError, TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_budget_or_none(value: int | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(float(value))
    except (OverflowError, TypeError, ValueError):
        raise ValueError("max_total_frames must be greater than 0 or None") from None
    if parsed <= 0:
        raise ValueError("max_total_frames must be greater than 0 or None")
    return parsed


def _parse_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return int(parsed) if math.isfinite(parsed) else None


def _parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
