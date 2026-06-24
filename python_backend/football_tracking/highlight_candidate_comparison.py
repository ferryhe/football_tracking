from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from football_tracking.ai_candidate_comparison import build_candidate_comparison, write_candidate_comparison_report
from football_tracking.highlight_window_validation import (
    HIGHLIGHT_APPROVAL_ACTIONS,
    HIGHLIGHT_WINDOW_VALIDATION_NAME,
    build_highlight_window_validation,
)

HIGHLIGHT_CANDIDATE_COMPARISON_NAME = "highlight_candidate_comparison.json"


def build_highlight_candidate_comparison(
    candidate_dir: Path,
    *,
    baseline_dir: Path,
    candidate_id: str,
    approval: dict[str, Any] | None = None,
    candidate_dir_relative: str | None = None,
    comparison_report: str = HIGHLIGHT_CANDIDATE_COMPARISON_NAME,
    candidate_artifacts: list[str] | None = None,
) -> dict[str, Any]:
    candidate_id = _safe_candidate_id(candidate_id)
    candidate_dir = Path(candidate_dir)
    baseline_dir = Path(baseline_dir)
    validation = _load_json(candidate_dir / HIGHLIGHT_WINDOW_VALIDATION_NAME)
    if not isinstance(validation, dict):
        validation = build_highlight_window_validation(baseline_dir, approval)
    highlight_report = _load_json(candidate_dir / "highlight_report.json") or {}
    renderer = highlight_report.get("renderer") if isinstance(highlight_report.get("renderer"), dict) else {}
    render_window = _window(validation.get("render_window")) or _window(highlight_report.get("window"))
    frame_count = _optional_int(renderer.get("frame_count"))
    fps = _optional_float(renderer.get("fps"))
    duration_seconds = round(frame_count / fps, 6) if frame_count is not None and fps is not None and fps > 0 else None
    checks = [
        *_validation_checks(validation),
        _approval_linkage_check(approval, candidate_id=candidate_id),
        _frame_count_match_check(validation, frame_count),
    ]
    relative_candidate_dir = candidate_dir_relative or _parent_dir(candidate_dir / "highlight.mp4")
    artifact_paths = candidate_artifacts or [str(candidate_dir / "highlight.mp4")]
    payload = build_candidate_comparison(
        problem_type="highlight",
        baseline={
            "path": str(baseline_dir / "event_candidates.json"),
            "event_candidate_id": validation.get("event_candidate_id"),
            "render_window": validation.get("baseline_render_window"),
        },
        candidate={
            "id": candidate_id,
            "path": str(candidate_dir / "highlight.mp4"),
            "event_candidate_id": validation.get("event_candidate_id"),
            "render_window": render_window,
        },
        approval=approval,
        checks=checks,
    )
    payload.update(
        {
            "candidate_id": candidate_id,
            "approval_id": _approval_id(approval),
            "event_candidate_id": validation.get("event_candidate_id"),
            "core_window": validation.get("core_window"),
            "baseline_render_window": validation.get("baseline_render_window"),
            "suggested_window": validation.get("suggested_window"),
            "render_window": render_window,
            "default_pre_buffer_frames": validation.get("default_pre_buffer_frames"),
            "default_post_buffer_frames": validation.get("default_post_buffer_frames"),
            "pre_frame_delta": validation.get("pre_frame_delta"),
            "post_frame_delta": validation.get("post_frame_delta"),
            "core_window_preserved": _validation_bool(
                validation,
                "core_window_preserved",
                check_name="core_window_preserved",
                status_as_bool=True,
            ),
            "required_tail_frames": validation.get("required_tail_frames"),
            "actual_tail_frames": validation.get("actual_tail_frames"),
            "source_end_clamp": _validation_bool(validation, "source_end_clamp", check_name="source_bounds"),
            "tail_status": validation.get("tail_status"),
            "frame_count": frame_count,
            "duration_seconds": duration_seconds,
            "comparison_report": comparison_report,
            "comparison_status": payload["summary"]["status"],
            "promotion_status": "not_promoted",
            "consumed_approval_ids": _consumed_approval_ids(approval),
            "candidate_dir": relative_candidate_dir,
            "candidate_artifacts": artifact_paths,
            "highlight_report": f"{relative_candidate_dir}/highlight_report.json" if relative_candidate_dir else "highlight_report.json",
            "window_validation_report": f"{relative_candidate_dir}/{HIGHLIGHT_WINDOW_VALIDATION_NAME}"
            if relative_candidate_dir
            else HIGHLIGHT_WINDOW_VALIDATION_NAME,
        }
    )
    payload["promotion_eligible"] = payload["summary"]["promotion_eligible"]
    payload["requires_human_confirmation"] = payload["summary"]["requires_human_confirmation"]
    return payload


def write_highlight_candidate_comparison(
    candidate_dir: Path,
    *,
    baseline_dir: Path,
    candidate_id: str,
    approval: dict[str, Any] | None = None,
    candidate_dir_relative: str | None = None,
    comparison_report: str = HIGHLIGHT_CANDIDATE_COMPARISON_NAME,
    candidate_artifacts: list[str] | None = None,
) -> Path:
    payload = build_highlight_candidate_comparison(
        candidate_dir,
        baseline_dir=baseline_dir,
        candidate_id=candidate_id,
        approval=approval,
        candidate_dir_relative=candidate_dir_relative,
        comparison_report=comparison_report,
        candidate_artifacts=candidate_artifacts,
    )
    return write_candidate_comparison_report(Path(candidate_dir), payload, name=HIGHLIGHT_CANDIDATE_COMPARISON_NAME)


def _validation_checks(validation: dict[str, Any]) -> list[dict[str, Any]]:
    checks = validation.get("checks") if isinstance(validation.get("checks"), list) else []
    result: list[dict[str, Any]] = []
    for check in checks:
        if isinstance(check, dict) and check.get("status") in {"pass", "warn", "fail", "unavailable"}:
            result.append(dict(check))
    if not result:
        result.append(
            {
                "name": "highlight_window_validation_available",
                "status": "unavailable",
                "reason": "highlight_window_validation.json is missing or invalid",
            }
        )
    return result


def _validation_bool(
    validation: dict[str, Any],
    key: str,
    *,
    check_name: str,
    status_as_bool: bool = False,
) -> bool | None:
    value = validation.get(key)
    if isinstance(value, bool):
        return value
    for check in validation.get("checks", []) if isinstance(validation.get("checks"), list) else []:
        if not isinstance(check, dict) or check.get("name") != check_name:
            continue
        check_value = check.get(key)
        if isinstance(check_value, bool):
            return check_value
        if status_as_bool and check.get("status") in {"pass", "fail"}:
            return check.get("status") == "pass"
    return None


def _approval_linkage_check(approval: dict[str, Any] | None, *, candidate_id: str) -> dict[str, Any]:
    if not isinstance(approval, dict):
        return {"name": "approval_linkage", "status": "unavailable", "reason": "approval provenance is absent"}
    approval_id = _approval_id(approval)
    if approval_id is None:
        return {"name": "approval_linkage", "status": "fail", "reason": "approval_id is required"}
    action = _optional_string(approval.get("approved_action"))
    if action not in HIGHLIGHT_APPROVAL_ACTIONS:
        return {
            "name": "approval_linkage",
            "status": "fail",
            "approval_id": approval_id,
            "approved_action": action,
            "reason": "approved_action is not a highlight action",
        }
    approval_candidate_id = _optional_string(approval.get("candidate_id"))
    if approval_candidate_id is not None and approval_candidate_id != candidate_id:
        return {
            "name": "approval_linkage",
            "status": "fail",
            "approval_id": approval_id,
            "candidate_id": candidate_id,
            "approval_candidate_id": approval_candidate_id,
            "reason": "approval candidate_id does not match comparison candidate",
        }
    return {"name": "approval_linkage", "status": "pass", "approval_id": approval_id, "candidate_id": candidate_id}


def _frame_count_match_check(validation: dict[str, Any], frame_count: int | None) -> dict[str, Any]:
    expected = _optional_int(validation.get("expected_frame_count"))
    if expected is None or frame_count is None:
        return {
            "name": "frame_count_match",
            "status": "unavailable",
            "expected_frame_count": expected,
            "actual_frame_count": frame_count,
            "reason": "frame count evidence is missing",
        }
    status = "pass" if frame_count == expected else "fail"
    return {
        "name": "frame_count_match",
        "status": status,
        "expected_frame_count": expected,
        "actual_frame_count": frame_count,
        "reason": "rendered frame count matches render_window"
        if status == "pass"
        else "rendered frame count does not match render_window",
    }


def _window(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    start = _optional_int(value.get("start_frame"))
    end = _optional_int(value.get("end_frame"))
    if start is None or end is None or start < 0 or end < start:
        return None
    return {"start_frame": start, "end_frame": end}


def _safe_candidate_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("candidate_id must be a non-empty string")
    candidate_id = value.strip()
    path = Path(candidate_id)
    if (
        candidate_id != value
        or candidate_id in {".", ".."}
        or path.name != candidate_id
        or any(separator in candidate_id for separator in ("/", "\\"))
        or ":" in candidate_id
        or ".." in candidate_id
        or candidate_id.rstrip(" .") != candidate_id
        or any(ord(character) < 32 for character in candidate_id)
    ):
        raise ValueError("candidate_id must be a safe identifier")
    return candidate_id


def _approval_id(approval: dict[str, Any] | None) -> str | None:
    if not isinstance(approval, dict):
        return None
    return _optional_string(approval.get("approval_id"))


def _consumed_approval_ids(approval: dict[str, Any] | None) -> list[str]:
    approval_id = _approval_id(approval)
    return [approval_id] if approval_id is not None else []


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


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _parent_dir(path: Path) -> str | None:
    parent = Path(path).parent
    return None if str(parent) == "." else str(parent)


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None
