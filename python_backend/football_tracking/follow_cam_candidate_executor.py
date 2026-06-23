from __future__ import annotations

import json
import shutil
from dataclasses import asdict, fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from football_tracking.ai_candidate_registry import load_candidate_registry, write_candidate_registry
from football_tracking.config import AppConfig, load_config
from football_tracking.follow_cam import FollowCamGenerator
from football_tracking.follow_cam_candidate_comparison import (
    FOLLOW_CAM_APPROVAL_ACTIONS,
    FOLLOW_CAM_CANDIDATE_COMPARISON_NAME,
    write_follow_cam_candidate_comparison,
)

FOLLOW_CAM_CANDIDATE_ARTIFACT_NAMES = (
    "follow_cam.mp4",
    "camera_path.csv",
    "follow_cam_report.json",
    "camera_motion_audit.json",
    FOLLOW_CAM_CANDIDATE_COMPARISON_NAME,
    "candidate_manifest.json",
)
APPROVED_FOLLOW_CAM_CONFIG_NAME = "approved_follow_cam_config.yaml"
APPROVED_ACTIONS_NAME = "ai_improvement_approved_actions.json"
TRACKING_PROBLEM_TYPES = {"missing_ball", "noise"}
TRACKING_COMPARISON_NAMES = {"missing_ball_recovery_comparison.json", "noise_candidate_comparison.json"}
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def execute_follow_cam_candidate(
    parent_output_dir: Path,
    approval: dict[str, Any],
    *,
    config_path: Path,
    input_video: Path | str,
) -> dict[str, Any]:
    parent_output_dir = Path(parent_output_dir).resolve()
    normalized_approval = _normalized_follow_cam_approval(approval)
    candidate_id = normalized_approval["candidate_id"]
    candidate_output_dir = follow_cam_candidate_output_dir(parent_output_dir, candidate_id)
    if candidate_output_dir.exists():
        raise FileExistsError(str(candidate_output_dir))

    config = load_config(config_path)
    _apply_follow_cam_config_patch(config, normalized_approval)
    config.input_video = Path(input_video)
    config.output_dir = candidate_output_dir
    config.follow_cam.enabled = True
    config.follow_cam.output_video_name = "follow_cam.mp4"
    config.follow_cam.camera_path_name = "camera_path.csv"
    config.follow_cam.report_name = "follow_cam_report.json"

    linked_tracking_candidate = _linked_tracking_candidate(parent_output_dir, normalized_approval)

    candidate_output_dir.mkdir(parents=True, exist_ok=False)
    try:
        _copy_track_inputs(
            parent_output_dir=parent_output_dir,
            candidate_output_dir=candidate_output_dir,
            linked_tracking_candidate=linked_tracking_candidate,
        )
        _write_json(candidate_output_dir / APPROVED_ACTIONS_NAME, {"approved_actions": [normalized_approval]})
        _write_follow_cam_config(candidate_output_dir / APPROVED_FOLLOW_CAM_CONFIG_NAME, config)
        FollowCamGenerator(config).run()
        _assert_required_render_artifacts(candidate_output_dir)
        comparison = register_follow_cam_candidate(
            parent_output_dir=parent_output_dir,
            candidate_output_dir=candidate_output_dir,
            approval=normalized_approval,
            candidate_id=candidate_id,
            linked_tracking_candidate=linked_tracking_candidate,
        )
        return comparison
    except Exception:
        if candidate_output_dir.exists():
            shutil.rmtree(candidate_output_dir, ignore_errors=True)
        raise


def follow_cam_candidate_output_dir(parent_output_dir: Path, candidate_id: str) -> Path:
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
        raise ValueError("candidate_id must be a safe single directory name for follow-cam candidate output.")
    candidate_output_dir = (parent_output_dir / "ai_candidates" / "follow_cam" / candidate_name).resolve()
    try:
        candidate_output_dir.relative_to(parent_output_dir)
    except ValueError as exc:
        raise ValueError("candidate_id output path must stay within the parent output directory.") from exc
    return candidate_output_dir


def register_follow_cam_candidate(
    *,
    parent_output_dir: Path,
    candidate_output_dir: Path,
    approval: dict[str, Any],
    candidate_id: str,
    linked_tracking_candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parent_output_dir = Path(parent_output_dir).resolve()
    candidate_output_dir = Path(candidate_output_dir).resolve()
    relative_candidate_dir = candidate_output_dir.relative_to(parent_output_dir).as_posix()
    relative_comparison = f"{relative_candidate_dir}/{FOLLOW_CAM_CANDIDATE_COMPARISON_NAME}"
    candidate_artifacts = [f"{relative_candidate_dir}/{name}" for name in FOLLOW_CAM_CANDIDATE_ARTIFACT_NAMES]
    comparison_path = write_follow_cam_candidate_comparison(
        candidate_output_dir,
        baseline_dir=parent_output_dir,
        candidate_id=candidate_id,
        approval=approval,
        candidate_dir_relative=relative_candidate_dir,
        comparison_report=relative_comparison,
        candidate_artifacts=candidate_artifacts,
    )
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    if linked_tracking_candidate is not None:
        comparison["linked_tracking_candidate"] = linked_tracking_candidate
    comparison["candidate_manifest"] = f"{relative_candidate_dir}/candidate_manifest.json"
    comparison_path.write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    manifest = _candidate_manifest(
        approval,
        candidate_id=candidate_id,
        candidate_dir=relative_candidate_dir,
        candidate_artifacts=candidate_artifacts,
        comparison=comparison,
        linked_tracking_candidate=linked_tracking_candidate,
    )
    _write_json(candidate_output_dir / "candidate_manifest.json", manifest)
    _write_candidate_registry(parent_output_dir, comparison, candidate_id=candidate_id)
    return comparison


def _normalized_follow_cam_approval(approval: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(approval, dict):
        raise TypeError("follow-cam approval must be a mapping")
    action = str(approval.get("approved_action") or "")
    if action not in FOLLOW_CAM_APPROVAL_ACTIONS:
        raise ValueError("approved_action must be a follow-cam action")
    approval_id = _required_string(approval.get("approval_id"), "approval_id")
    candidate_id = _required_string(approval.get("candidate_id"), "candidate_id")
    normalized = dict(approval)
    normalized["approval_id"] = approval_id
    normalized["candidate_id"] = _safe_candidate_id(candidate_id)
    normalized["approved_action"] = action
    return normalized


def _apply_follow_cam_config_patch(config: AppConfig, approval: dict[str, Any]) -> None:
    patch = approval.get("config_patch")
    if patch is None:
        patch = approval.get("follow_cam_config_patch")
    if patch is None:
        return
    if not isinstance(patch, dict):
        raise ValueError("follow-cam config_patch must be an object")
    follow_cam_patch = patch.get("follow_cam") if isinstance(patch.get("follow_cam"), dict) else patch
    if not isinstance(follow_cam_patch, dict):
        raise ValueError("follow-cam config_patch.follow_cam must be an object")
    allowed_keys = {field.name for field in fields(config.follow_cam)}
    for key, value in follow_cam_patch.items():
        if key not in allowed_keys:
            raise ValueError(f"Unknown follow_cam config patch key: {key}")
        if key in {"output_video_name", "camera_path_name", "report_name", "enabled"}:
            continue
        setattr(config.follow_cam, key, value)


def _linked_tracking_candidate(parent_output_dir: Path, approval: dict[str, Any]) -> dict[str, Any] | None:
    action = str(approval.get("approved_action") or "")
    if action != "tracking_rerun_before_follow_cam":
        return None
    linked_id = _first_string(
        approval,
        (
            "linked_tracking_candidate_id",
            "tracking_candidate_id",
            "source_candidate_id",
        ),
    )
    if linked_id is None:
        raise ValueError("tracking_rerun_before_follow_cam requires linked passed tracking candidate evidence")
    candidate = _find_passed_tracking_candidate(parent_output_dir, linked_id)
    if candidate is None:
        raise ValueError("tracking_rerun_before_follow_cam requires linked passed tracking candidate evidence")
    return candidate


def _find_passed_tracking_candidate(parent_output_dir: Path, candidate_id: str) -> dict[str, Any] | None:
    for comparison_path in sorted((parent_output_dir / "ai_candidates").rglob("*comparison.json")):
        if comparison_path.name not in TRACKING_COMPARISON_NAMES:
            continue
        payload = _load_json(comparison_path)
        if not isinstance(payload, dict):
            continue
        payload_candidate_id = _first_string(payload, ("candidate_id",))
        candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else {}
        payload_candidate_id = payload_candidate_id or _first_string(candidate, ("id", "candidate_id"))
        problem_type = _first_string(payload, ("problem_type",))
        status = _first_string(payload, ("comparison_status", "status"))
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        status = status or _first_string(summary, ("status",))
        if payload_candidate_id != candidate_id or problem_type not in TRACKING_PROBLEM_TYPES or status != "pass":
            continue
        track_path = _tracking_candidate_track_path(parent_output_dir, payload, comparison_path)
        if track_path is None or not track_path.exists():
            continue
        return {
            "candidate_id": candidate_id,
            "problem_type": problem_type,
            "comparison_report": comparison_path.relative_to(parent_output_dir).as_posix(),
            "track_path": track_path.relative_to(parent_output_dir).as_posix(),
        }
    return None


def _tracking_candidate_track_path(parent_output_dir: Path, payload: dict[str, Any], comparison_path: Path) -> Path | None:
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else {}
    candidates: list[str] = []
    for value in (candidate.get("path"), payload.get("candidate_track")):
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    for value in payload.get("candidate_artifacts", []) if isinstance(payload.get("candidate_artifacts"), list) else []:
        if isinstance(value, str) and Path(value).name in {"ball_track.csv", "ball_track.cleaned.csv"}:
            candidates.append(value)
    candidate_dir = payload.get("candidate_dir")
    if isinstance(candidate_dir, str) and candidate_dir.strip():
        for name in ("ball_track.cleaned.csv", "ball_track.csv"):
            candidates.append(f"{candidate_dir.rstrip('/')}/{name}")
    candidates.append(str(comparison_path.parent / "ball_track.cleaned.csv"))
    candidates.append(str(comparison_path.parent / "ball_track.csv"))
    for candidate_path in candidates:
        path = Path(candidate_path)
        resolved = path if path.is_absolute() else parent_output_dir / path
        if resolved.exists():
            try:
                resolved.resolve().relative_to(parent_output_dir.resolve())
            except ValueError:
                continue
            return resolved.resolve()
    return None


def _copy_track_inputs(
    *,
    parent_output_dir: Path,
    candidate_output_dir: Path,
    linked_tracking_candidate: dict[str, Any] | None,
) -> None:
    if linked_tracking_candidate is not None:
        source = parent_output_dir / str(linked_tracking_candidate["track_path"])
        shutil.copy2(source, candidate_output_dir / "ball_track.csv")
        if source.name == "ball_track.cleaned.csv":
            shutil.copy2(source, candidate_output_dir / "ball_track.cleaned.csv")
        return
    copied = False
    for name in ("ball_track.csv", "ball_track.cleaned.csv"):
        source = parent_output_dir / name
        if source.is_file():
            shutil.copy2(source, candidate_output_dir / name)
            copied = True
    if not copied:
        raise FileNotFoundError("No baseline track CSV available for follow-cam candidate generation.")


def _assert_required_render_artifacts(candidate_output_dir: Path) -> None:
    missing = [
        name
        for name in ("follow_cam.mp4", "camera_path.csv", "follow_cam_report.json", "camera_motion_audit.json")
        if not (candidate_output_dir / name).exists()
    ]
    if missing:
        raise FileNotFoundError(f"follow-cam candidate render did not produce required artifacts: {', '.join(missing)}")


def _candidate_manifest(
    approval: dict[str, Any],
    *,
    candidate_id: str,
    candidate_dir: str,
    candidate_artifacts: list[str],
    comparison: dict[str, Any],
    linked_tracking_candidate: dict[str, Any] | None,
) -> dict[str, Any]:
    manifest = {
        "schema_version": "1.0",
        "generated_at": _utc_now_iso(),
        "candidate_id": candidate_id,
        "approval_id": approval.get("approval_id"),
        "problem_type": "follow_cam",
        "approved_action": approval.get("approved_action"),
        "candidate_dir": candidate_dir,
        "candidate_artifacts": candidate_artifacts,
        "comparison_report": comparison.get("comparison_report"),
        "comparison_status": comparison.get("comparison_status"),
        "promotion_status": "not_promoted",
        "consumed_approval_ids": comparison.get("consumed_approval_ids", []),
    }
    if linked_tracking_candidate is not None:
        manifest["linked_tracking_candidate"] = linked_tracking_candidate
    return manifest


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


def _write_follow_cam_config(config_path: Path, config: AppConfig) -> None:
    with config_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(_jsonable(config), handle, sort_keys=False, allow_unicode=False)


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


def _first_string(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


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


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
