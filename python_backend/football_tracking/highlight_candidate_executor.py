from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2

from football_tracking.ai_candidate_registry import load_candidate_registry, write_candidate_registry
from football_tracking.highlight_candidate_comparison import (
    HIGHLIGHT_CANDIDATE_COMPARISON_NAME,
    write_highlight_candidate_comparison,
)
from football_tracking.highlight_window_validation import (
    HIGHLIGHT_APPROVAL_ACTIONS,
    HIGHLIGHT_WINDOW_VALIDATION_NAME,
    build_highlight_window_validation,
)
from football_tracking.highlights import render_highlight_clip

APPROVED_ACTIONS_NAME = "ai_improvement_approved_actions.json"
HIGHLIGHT_CANDIDATE_ARTIFACT_NAMES = (
    "highlight.mp4",
    "highlight_report.json",
    HIGHLIGHT_WINDOW_VALIDATION_NAME,
    HIGHLIGHT_CANDIDATE_COMPARISON_NAME,
    "candidate_manifest.json",
)
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def execute_highlight_candidate(
    parent_output_dir: Path,
    approval: dict[str, Any],
    *,
    input_video: Path | str | None,
) -> dict[str, Any]:
    parent_output_dir = Path(parent_output_dir).resolve()
    normalized_approval = _normalized_highlight_approval(approval)
    candidate_id = normalized_approval["candidate_id"]
    candidate_output_dir = highlight_candidate_output_dir(parent_output_dir, candidate_id)
    if candidate_output_dir.exists():
        raise FileExistsError(str(candidate_output_dir))

    video_metadata = _video_metadata(input_video)
    candidate_output_dir.mkdir(parents=True, exist_ok=False)
    try:
        _write_json(candidate_output_dir / APPROVED_ACTIONS_NAME, {"approved_actions": [normalized_approval]})
        validation = build_highlight_window_validation(
            parent_output_dir,
            normalized_approval,
            source_total_frames=video_metadata.get("frame_count"),
        )
        _write_json(candidate_output_dir / HIGHLIGHT_WINDOW_VALIDATION_NAME, validation)
        renderer_report: dict[str, Any] = {}
        if validation["status"] == "pass" and isinstance(validation.get("render_window"), dict):
            render_window = validation["render_window"]
            renderer_report = render_highlight_clip(
                input_video=Path(input_video),
                output_path=candidate_output_dir / "highlight.mp4",
                start_frame=int(render_window["start_frame"]),
                end_frame=int(render_window["end_frame"]),
            )
        _write_highlight_report(
            candidate_output_dir,
            parent_output_dir=parent_output_dir,
            input_video=Path(input_video),
            approval=normalized_approval,
            validation=validation,
            renderer_report=renderer_report,
        )
        return register_highlight_candidate(
            parent_output_dir=parent_output_dir,
            candidate_output_dir=candidate_output_dir,
            approval=normalized_approval,
            candidate_id=candidate_id,
        )
    except Exception:
        if candidate_output_dir.exists():
            shutil.rmtree(candidate_output_dir, ignore_errors=True)
        raise


def highlight_candidate_output_dir(parent_output_dir: Path, candidate_id: str) -> Path:
    parent_output_dir = Path(parent_output_dir).resolve()
    candidate_name = str(candidate_id or "").strip()
    candidate_path = Path(candidate_name)
    if (
        not candidate_name
        or candidate_name in {".", ".."}
        or candidate_name != candidate_id
        or candidate_name.rstrip(" .") != candidate_name
        or candidate_path.is_absolute()
        or candidate_path.name != candidate_name
        or any(separator in candidate_name for separator in ("/", "\\"))
        or ":" in candidate_name
        or ".." in candidate_name
        or ".." in candidate_path.parts
        or any(ord(character) < 32 for character in candidate_name)
        or candidate_path.stem.upper() in _WINDOWS_RESERVED_NAMES
    ):
        raise ValueError("candidate_id must be a safe single directory name for highlight candidate output.")
    candidate_output_dir = (parent_output_dir / "ai_candidates" / "highlight" / candidate_name).resolve()
    try:
        candidate_output_dir.relative_to(parent_output_dir)
    except ValueError as exc:
        raise ValueError("candidate_id output path must stay within the parent output directory.") from exc
    return candidate_output_dir


def register_highlight_candidate(
    *,
    parent_output_dir: Path,
    candidate_output_dir: Path,
    approval: dict[str, Any],
    candidate_id: str,
) -> dict[str, Any]:
    parent_output_dir = Path(parent_output_dir).resolve()
    candidate_output_dir = Path(candidate_output_dir).resolve()
    relative_candidate_dir = candidate_output_dir.relative_to(parent_output_dir).as_posix()
    relative_comparison = f"{relative_candidate_dir}/{HIGHLIGHT_CANDIDATE_COMPARISON_NAME}"
    candidate_artifacts = [f"{relative_candidate_dir}/{name}" for name in HIGHLIGHT_CANDIDATE_ARTIFACT_NAMES]
    comparison_path = write_highlight_candidate_comparison(
        candidate_output_dir,
        baseline_dir=parent_output_dir,
        candidate_id=candidate_id,
        approval=approval,
        candidate_dir_relative=relative_candidate_dir,
        comparison_report=relative_comparison,
        candidate_artifacts=candidate_artifacts,
    )
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    comparison["candidate_manifest"] = f"{relative_candidate_dir}/candidate_manifest.json"
    comparison_path.write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = _candidate_manifest(
        approval,
        candidate_id=candidate_id,
        candidate_dir=relative_candidate_dir,
        candidate_artifacts=candidate_artifacts,
        comparison=comparison,
    )
    _write_json(candidate_output_dir / "candidate_manifest.json", manifest)
    _write_candidate_registry(parent_output_dir, comparison, candidate_id=candidate_id)
    return comparison


def _normalized_highlight_approval(approval: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(approval, dict):
        raise TypeError("highlight approval must be a mapping")
    action = str(approval.get("approved_action") or "")
    if action not in HIGHLIGHT_APPROVAL_ACTIONS:
        raise ValueError("approved_action must be a highlight action")
    normalized = dict(approval)
    normalized["approval_id"] = _required_string(approval.get("approval_id"), "approval_id")
    normalized["candidate_id"] = _safe_candidate_id(approval.get("candidate_id"))
    normalized["approved_action"] = action
    if not isinstance(approval.get("suggested_window"), dict):
        raise ValueError("highlight approval requires suggested_window")
    normalized["suggested_window"] = dict(approval["suggested_window"])
    return normalized


def _write_highlight_report(
    candidate_output_dir: Path,
    *,
    parent_output_dir: Path,
    input_video: Path,
    approval: dict[str, Any],
    validation: dict[str, Any],
    renderer_report: dict[str, Any],
) -> None:
    payload = {
        "schema_version": "1.0",
        "generated_at": _utc_now_iso(),
        "source_output_dir": str(parent_output_dir),
        "input_video": str(input_video),
        "output_video": "highlight.mp4",
        "candidate_id": approval.get("candidate_id"),
        "event_candidate_id": validation.get("event_candidate_id"),
        "approved_action": approval.get("approved_action"),
        "window": validation.get("render_window"),
        "suggested_window": validation.get("suggested_window"),
        "selection_source": "approved_ai_suggested_window",
        "approval": approval,
        "window_validation": {
            "path": HIGHLIGHT_WINDOW_VALIDATION_NAME,
            "status": validation.get("status"),
            "tail_status": validation.get("tail_status"),
            "source_end_clamp": validation.get("source_end_clamp"),
        },
        "renderer": renderer_report,
    }
    _write_json(candidate_output_dir / "highlight_report.json", payload)


def _candidate_manifest(
    approval: dict[str, Any],
    *,
    candidate_id: str,
    candidate_dir: str,
    candidate_artifacts: list[str],
    comparison: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "generated_at": _utc_now_iso(),
        "candidate_id": candidate_id,
        "approval_id": approval.get("approval_id"),
        "problem_type": "highlight",
        "event_candidate_id": comparison.get("event_candidate_id"),
        "approved_action": approval.get("approved_action"),
        "candidate_dir": candidate_dir,
        "candidate_artifacts": candidate_artifacts,
        "comparison_report": comparison.get("comparison_report"),
        "comparison_status": comparison.get("comparison_status"),
        "promotion_status": "not_promoted",
        "consumed_approval_ids": comparison.get("consumed_approval_ids", []),
        "render_window": comparison.get("render_window"),
        "frame_count": comparison.get("frame_count"),
        "duration_seconds": comparison.get("duration_seconds"),
        "tail_status": comparison.get("tail_status"),
        "source_end_clamp": comparison.get("source_end_clamp"),
    }


def _write_candidate_registry(parent_output_dir: Path, comparison: dict[str, Any], *, candidate_id: str) -> None:
    loaded = load_candidate_registry(parent_output_dir)
    artifact_status = loaded.get("artifact_status")
    if artifact_status not in {"loaded", "missing"}:
        raise RuntimeError(f"Cannot update parent ai_candidate_registry.json while it is {artifact_status}.")
    existing_records = loaded.get("candidates") if artifact_status == "loaded" and isinstance(loaded.get("candidates"), list) else []
    records = [
        record
        for record in existing_records
        if isinstance(record, dict) and record.get("candidate_id") != candidate_id
    ]
    write_candidate_registry(parent_output_dir, records=records, comparison_reports=[comparison])


def _video_metadata(input_video: Path | str | None) -> dict[str, Any]:
    if input_video is None:
        raise RuntimeError("Unable to open input video for highlight render: input_video is required")
    capture = cv2.VideoCapture(str(input_video))
    if not capture.isOpened():
        raise RuntimeError(f"Unable to open input video for highlight render: {input_video}")
    try:
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        return {
            "frame_count": frame_count if frame_count > 0 else None,
            "fps": float(capture.get(cv2.CAP_PROP_FPS) or 0.0),
        }
    finally:
        capture.release()


def _safe_candidate_id(value: Any) -> str:
    candidate_id = _required_string(value, "candidate_id")
    path = Path(candidate_id)
    if (
        candidate_id in {".", ".."}
        or path.name != candidate_id
        or any(separator in candidate_id for separator in ("/", "\\"))
        or ":" in candidate_id
        or ".." in candidate_id
        or candidate_id.rstrip(" .") != candidate_id
        or any(ord(character) < 32 for character in candidate_id)
        or path.stem.upper() in _WINDOWS_RESERVED_NAMES
    ):
        raise ValueError("candidate_id must be a safe identifier")
    return candidate_id


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
