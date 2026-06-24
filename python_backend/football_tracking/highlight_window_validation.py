from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
HIGHLIGHT_WINDOW_VALIDATION_NAME = "highlight_window_validation.json"
HIGHLIGHT_APPROVAL_ACTIONS = {"adjust_highlight_window", "render_suggested_highlight"}
DEFAULT_PRE_BUFFER_FRAMES = 15
DEFAULT_POST_BUFFER_FRAMES = 30


def build_highlight_window_validation(
    baseline_dir: Path,
    approval: dict[str, Any] | None,
    *,
    event_candidates_name: str = "event_candidates.json",
    source_total_frames: int | None = None,
) -> dict[str, Any]:
    baseline_dir = Path(baseline_dir)
    approval_payload = approval if isinstance(approval, dict) else {}
    event_candidates = _load_json(baseline_dir / event_candidates_name)
    candidates_by_id = _event_candidates_by_id(event_candidates)
    event_candidate_id = _event_candidate_id(approval_payload, candidates_by_id)
    event_candidate = candidates_by_id.get(event_candidate_id or "")
    source_frames = _first_int(
        source_total_frames,
        _source_frame_count(event_candidate or {}),
        _source_frame_count(event_candidates or {}),
    )
    last_source_frame = max(0, source_frames - 1) if source_frames is not None and source_frames > 0 else None
    core_window = _candidate_core_window(event_candidate or {})
    baseline_render_window = _baseline_render_window(
        event_candidate or {},
        core_window=core_window,
        last_source_frame=last_source_frame,
    )
    suggested_window = _optional_frame_window(approval_payload.get("suggested_window"))
    render_window = _source_clamped_window(suggested_window, last_source_frame=last_source_frame)
    default_pre, default_post = _default_buffers(event_candidate or {}, core_window, baseline_render_window)
    suggested_pre, suggested_post = _suggested_buffers(core_window, render_window)
    min_tail = _min_tail_frames(event_candidate or {})
    required_tail_end = core_window["end_frame"] + min_tail if core_window is not None else None
    available_tail_end = required_tail_end
    if available_tail_end is not None and last_source_frame is not None:
        available_tail_end = min(available_tail_end, last_source_frame)
    source_end_clamp = (
        suggested_window is not None
        and render_window is not None
        and last_source_frame is not None
        and (
            int(suggested_window["end_frame"]) > last_source_frame
            or (required_tail_end is not None and required_tail_end > last_source_frame)
        )
        and int(render_window["end_frame"]) == last_source_frame
    )
    tail_status = _tail_status(
        render_window,
        required_tail_end=required_tail_end,
        available_tail_end=available_tail_end,
        last_source_frame=last_source_frame,
    )
    core_window_preserved = _core_window_preserved(core_window, render_window)
    actual_tail_frames = _actual_tail_frames(core_window, render_window)
    checks = [
        _approval_action_supported_check(approval_payload),
        _event_candidate_linkage_check(event_candidate_id, event_candidate),
        _suggested_window_valid_check(approval_payload.get("suggested_window"), suggested_window),
        _source_bounds_check(
            suggested_window,
            render_window,
            last_source_frame=last_source_frame,
            source_end_clamp=source_end_clamp,
        ),
        _core_window_preserved_check(core_window, render_window),
        _tail_preserved_check(
            tail_status,
            required_tail_end=required_tail_end,
            actual_tail_frames=actual_tail_frames,
            available_tail_end=available_tail_end,
            render_window=render_window,
        ),
    ]
    expected_frame_count = (
        int(render_window["end_frame"]) - int(render_window["start_frame"]) + 1
        if render_window is not None
        else None
    )
    status = "fail" if any(check["status"] == "fail" for check in checks) else "pass"
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "status": status,
        "approval_id": _optional_string(approval_payload.get("approval_id")),
        "candidate_id": _optional_string(approval_payload.get("candidate_id")),
        "approved_action": _optional_string(approval_payload.get("approved_action")),
        "event_candidate_id": event_candidate_id,
        "core_window": core_window,
        "baseline_render_window": baseline_render_window,
        "suggested_window": suggested_window,
        "render_window": render_window,
        "default_pre_buffer_frames": default_pre,
        "default_post_buffer_frames": default_post,
        "suggested_pre_buffer_frames": suggested_pre,
        "suggested_post_buffer_frames": suggested_post,
        "pre_frame_delta": None if suggested_pre is None else suggested_pre - default_pre,
        "post_frame_delta": None if suggested_post is None else suggested_post - default_post,
        "core_window_preserved": core_window_preserved,
        "required_tail_frames": min_tail,
        "actual_tail_frames": actual_tail_frames,
        "required_tail_end_frame": required_tail_end,
        "available_tail_end_frame": available_tail_end,
        "source_total_frames": source_frames,
        "last_source_frame": last_source_frame,
        "source_end_clamp": source_end_clamp,
        "tail_status": tail_status,
        "expected_frame_count": expected_frame_count,
        "checks": checks,
    }


def write_highlight_window_validation(
    candidate_dir: Path,
    *,
    baseline_dir: Path,
    approval: dict[str, Any] | None,
    source_total_frames: int | None = None,
    name: str = HIGHLIGHT_WINDOW_VALIDATION_NAME,
) -> Path:
    payload = build_highlight_window_validation(
        baseline_dir,
        approval,
        source_total_frames=source_total_frames,
    )
    path = Path(candidate_dir) / name
    _write_json(path, payload)
    return path


def _approval_action_supported_check(approval: dict[str, Any]) -> dict[str, Any]:
    action = _optional_string(approval.get("approved_action"))
    if action not in HIGHLIGHT_APPROVAL_ACTIONS:
        return {
            "name": "approval_action_supported",
            "status": "fail",
            "approved_action": action,
            "reason": "approved_action is not a highlight action",
        }
    return {"name": "approval_action_supported", "status": "pass", "approved_action": action}


def _event_candidate_linkage_check(
    event_candidate_id: str | None,
    event_candidate: dict[str, Any] | None,
) -> dict[str, Any]:
    if event_candidate_id is None:
        return {"name": "event_candidate_linkage", "status": "fail", "reason": "event_candidate_id is required"}
    if not isinstance(event_candidate, dict):
        return {
            "name": "event_candidate_linkage",
            "status": "fail",
            "event_candidate_id": event_candidate_id,
            "reason": "event_candidate_id was not found in event_candidates.json",
        }
    return {"name": "event_candidate_linkage", "status": "pass", "event_candidate_id": event_candidate_id}


def _suggested_window_valid_check(raw_window: Any, suggested_window: dict[str, int] | None) -> dict[str, Any]:
    if suggested_window is None:
        return {
            "name": "suggested_window_valid",
            "status": "fail",
            "reason": "suggested_window must include non-negative start_frame and end_frame >= start_frame",
            "raw_window": raw_window,
        }
    return {"name": "suggested_window_valid", "status": "pass", "suggested_window": suggested_window}


def _source_bounds_check(
    suggested_window: dict[str, int] | None,
    render_window: dict[str, int] | None,
    *,
    last_source_frame: int | None,
    source_end_clamp: bool,
) -> dict[str, Any]:
    if suggested_window is None:
        return {"name": "source_bounds", "status": "fail", "reason": "suggested_window is invalid"}
    if last_source_frame is None:
        return {"name": "source_bounds", "status": "pass", "source_end_clamp": False}
    if int(suggested_window["start_frame"]) > last_source_frame:
        return {
            "name": "source_bounds",
            "status": "fail",
            "start_frame": int(suggested_window["start_frame"]),
            "last_source_frame": last_source_frame,
            "reason": "suggested_window starts beyond source video end",
        }
    return {
        "name": "source_bounds",
        "status": "pass",
        "last_source_frame": last_source_frame,
        "source_end_clamp": bool(source_end_clamp),
    }


def _core_window_preserved_check(
    core_window: dict[str, int] | None,
    render_window: dict[str, int] | None,
) -> dict[str, Any]:
    if core_window is None:
        return {"name": "core_window_preserved", "status": "fail", "reason": "event candidate has no valid core_window"}
    if render_window is None:
        return {"name": "core_window_preserved", "status": "fail", "core_window": core_window, "reason": "render_window is invalid"}
    preserved = _core_window_preserved(core_window, render_window)
    return {
        "name": "core_window_preserved",
        "status": "pass" if preserved else "fail",
        "core_window": core_window,
        "render_window": render_window,
        "reason": "render_window includes core_window" if preserved else "render_window cuts the event core_window",
    }


def _core_window_preserved(
    core_window: dict[str, int] | None,
    render_window: dict[str, int] | None,
) -> bool:
    return (
        core_window is not None
        and render_window is not None
        and int(render_window["start_frame"]) <= int(core_window["start_frame"])
        and int(render_window["end_frame"]) >= int(core_window["end_frame"])
    )


def _tail_preserved_check(
    tail_status: str,
    *,
    required_tail_end: int | None,
    actual_tail_frames: int | None,
    available_tail_end: int | None,
    render_window: dict[str, int] | None,
) -> dict[str, Any]:
    status = "pass" if tail_status in {"preserved", "source_end_clamped", "not_required"} else "fail"
    return {
        "name": "tail_preserved",
        "status": status,
        "tail_status": tail_status,
        "actual_tail_frames": actual_tail_frames,
        "required_tail_end_frame": required_tail_end,
        "available_tail_end_frame": available_tail_end,
        "render_window": render_window,
        "reason": "render_window preserves the available required tail"
        if status == "pass"
        else "render_window cuts available post-event tail",
    }


def _tail_status(
    render_window: dict[str, int] | None,
    *,
    required_tail_end: int | None,
    available_tail_end: int | None,
    last_source_frame: int | None,
) -> str:
    if required_tail_end is None or available_tail_end is None or render_window is None:
        return "unavailable"
    if required_tail_end <= int(render_window["end_frame"]):
        return "preserved"
    if (
        last_source_frame is not None
        and required_tail_end > last_source_frame
        and int(render_window["end_frame"]) >= last_source_frame
    ):
        return "source_end_clamped"
    if available_tail_end <= int(render_window["end_frame"]):
        return "preserved"
    return "cut_available_tail"


def _actual_tail_frames(
    core_window: dict[str, int] | None,
    render_window: dict[str, int] | None,
) -> int | None:
    if core_window is None or render_window is None:
        return None
    return max(0, int(render_window["end_frame"]) - int(core_window["end_frame"]))


def _event_candidate_id(approval: dict[str, Any], candidates_by_id: dict[str, dict[str, Any]]) -> str | None:
    for key in ("event_candidate_id", "source_event_candidate_id"):
        value = _optional_string(approval.get(key))
        if value is not None:
            return value
    return None


def _event_candidates_by_id(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    source_frames = _source_frame_count(payload or {})
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(candidates, list):
        return result
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_id = _optional_string(candidate.get("id"))
        if candidate_id is None:
            continue
        copied = dict(candidate)
        if source_frames is not None and _source_frame_count(copied) is None:
            copied["_event_candidates_source_frame_count"] = source_frames
        result[candidate_id] = copied
    return result


def _candidate_core_window(candidate: dict[str, Any]) -> dict[str, int] | None:
    core = _optional_frame_window(candidate.get("core_window"))
    if core is not None:
        return core
    start = _optional_int(candidate.get("start_frame"))
    end = _optional_int(candidate.get("end_frame"))
    if start is None or end is None or start < 0 or end < start:
        return None
    return {"start_frame": start, "end_frame": end}


def _baseline_render_window(
    candidate: dict[str, Any],
    *,
    core_window: dict[str, int] | None,
    last_source_frame: int | None,
) -> dict[str, int] | None:
    render = _optional_frame_window(candidate.get("render_window"))
    if render is not None:
        return render
    if core_window is None:
        return None
    pre, post = _default_buffers(candidate, core_window, None)
    end = int(core_window["end_frame"]) + post
    if last_source_frame is not None:
        end = min(end, last_source_frame)
    return {"start_frame": max(0, int(core_window["start_frame"]) - pre), "end_frame": end}


def _default_buffers(
    candidate: dict[str, Any],
    core_window: dict[str, int] | None,
    baseline_render_window: dict[str, int] | None,
) -> tuple[int, int]:
    policy = candidate.get("buffer_policy") if isinstance(candidate.get("buffer_policy"), dict) else {}
    pre = _optional_int(policy.get("pre_buffer_frames"))
    post = _optional_int(policy.get("post_buffer_frames"))
    if pre is None and core_window is not None and baseline_render_window is not None:
        pre = max(0, int(core_window["start_frame"]) - int(baseline_render_window["start_frame"]))
    if post is None and core_window is not None and baseline_render_window is not None:
        post = max(0, int(baseline_render_window["end_frame"]) - int(core_window["end_frame"]))
    return max(0, pre if pre is not None else DEFAULT_PRE_BUFFER_FRAMES), max(0, post if post is not None else DEFAULT_POST_BUFFER_FRAMES)


def _suggested_buffers(
    core_window: dict[str, int] | None,
    render_window: dict[str, int] | None,
) -> tuple[int | None, int | None]:
    if core_window is None or render_window is None:
        return None, None
    return (
        int(core_window["start_frame"]) - int(render_window["start_frame"]),
        int(render_window["end_frame"]) - int(core_window["end_frame"]),
    )


def _min_tail_frames(candidate: dict[str, Any]) -> int:
    policy = candidate.get("buffer_policy") if isinstance(candidate.get("buffer_policy"), dict) else {}
    for key in ("min_tail_frames", "min_post_event_frames", "post_buffer_frames"):
        value = _optional_int(policy.get(key))
        if value is not None:
            return max(0, value)
    return 0


def _source_clamped_window(
    window: dict[str, int] | None,
    *,
    last_source_frame: int | None,
) -> dict[str, int] | None:
    if window is None:
        return None
    start = int(window["start_frame"])
    end = int(window["end_frame"])
    if last_source_frame is not None:
        if start > last_source_frame:
            return None
        end = min(end, last_source_frame)
    if end < start:
        return None
    return {"start_frame": start, "end_frame": end}


def _optional_frame_window(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    start = _optional_int(value.get("start_frame"))
    end = _optional_int(value.get("end_frame"))
    if start is None or end is None or start < 0 or end < start:
        return None
    return {"start_frame": start, "end_frame": end}


def _source_frame_count(payload: dict[str, Any]) -> int | None:
    for key in ("total_source_frames", "source_frame_count", "source_total_frames", "video_frame_count"):
        value = _optional_int(payload.get(key))
        if value is not None:
            return value
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    for key in ("total_source_frames", "source_frame_count", "source_total_frames", "video_frame_count"):
        value = _optional_int(summary.get(key))
        if value is not None:
            return value
    return _optional_int(payload.get("_event_candidates_source_frame_count"))


def _first_int(*values: int | None) -> int | None:
    for value in values:
        if value is not None:
            return value
    return None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
