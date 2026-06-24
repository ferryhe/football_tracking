from __future__ import annotations

import csv
import hashlib
import json
import shutil
import tempfile
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from football_tracking.ai_candidate_registry import load_candidate_registry, write_candidate_registry
from football_tracking.ai_improvement import APPROVED_ACTIONS_FILE_NAME
from football_tracking.ball_audit import build_ball_audit_report
from football_tracking.chunk_runner import run_high_recall_windows
from football_tracking.config import DEFAULT_HIGH_RECALL_MAX_TOTAL_FRAMES, AppConfig, load_config
from football_tracking.high_recall_windows import approved_action_windows_from_report
from football_tracking.metrics import build_metrics_report, write_run_artifacts
from football_tracking.missing_ball_recovery_comparison import write_missing_ball_recovery_comparison

MISSING_BALL_RECOVERY_ACTIONS = {"targeted_rerun", "rerun_ball_window", "localize_ball_roi"}
MISSING_BALL_COMPARISON_NAME = "missing_ball_recovery_comparison.json"
APPROVED_RECOVERY_CONFIG_NAME = "approved_recovery_config.yaml"
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

HighRecallRunner = Callable[..., dict[str, Any] | None]


def select_missing_ball_recovery_actions(artifact: dict[str, Any], approval_ids: list[str]) -> dict[str, Any]:
    selected_ids = [str(item).strip() for item in approval_ids if str(item).strip()]
    if not selected_ids:
        raise ValueError("Approved child recovery requires at least one explicit approved_action_id.")
    actions = artifact.get("approved_actions")
    if not isinstance(actions, list):
        raise ValueError("approved_actions must be a list.")
    actions_by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()
    for action in actions:
        if not isinstance(action, dict):
            continue
        approval_id = str(action.get("approval_id") or "").strip()
        if not approval_id:
            continue
        if approval_id in actions_by_id:
            duplicate_ids.add(approval_id)
        actions_by_id[approval_id] = action
    if duplicate_ids:
        raise ValueError(f"Duplicate approved action IDs: {', '.join(sorted(duplicate_ids))}")
    missing = [approval_id for approval_id in selected_ids if approval_id not in actions_by_id]
    if missing:
        raise ValueError(f"Approved action IDs not found: {', '.join(missing)}")
    selected_artifact = dict(artifact)
    selected_artifact["approved_actions"] = [actions_by_id[approval_id] for approval_id in selected_ids]
    return selected_artifact


def missing_ball_candidate_output_dir(parent_output_dir: Path, candidate_id: str) -> Path:
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
        raise ValueError("candidate_id must be a safe single directory name for missing-ball candidate output.")
    candidate_output_dir = (parent_output_dir / "ai_candidates" / "missing_ball" / candidate_name).resolve()
    try:
        candidate_output_dir.relative_to(parent_output_dir)
    except ValueError as exc:
        raise ValueError("candidate_id output path must stay within the parent output directory.") from exc
    return candidate_output_dir


def validate_output_csv_name(value: str) -> None:
    csv_name = str(value or "").strip()
    path = Path(csv_name)
    if (
        not csv_name
        or csv_name in {".", ".."}
        or path.is_absolute()
        or path.name != csv_name
        or not csv_name.lower().endswith(".csv")
    ):
        raise ValueError("output.csv_name must be a safe single .csv filename.")


def execute_missing_ball_candidate(
    parent_output_dir: Path,
    selected_artifact: dict[str, Any],
    *,
    config_path: Path,
    input_video: Path | str,
    source_total_frames: int | None,
    runner: HighRecallRunner | None = None,
    run_id: str | None = None,
    parent_run_id: str | None = None,
    config_name: str | None = None,
    notes: Any = None,
) -> dict[str, Any]:
    parent_output_dir = Path(parent_output_dir).resolve()
    recovery_actions = _selected_recovery_actions(selected_artifact)
    if not recovery_actions:
        raise ValueError("Approved child recovery requires at least one executable approved recovery action.")
    known_packet_ids, known_visual_ids, known_visual_localization_ids = traceable_approval_provenance_ids(
        parent_output_dir
    )
    executable_windows = approved_action_windows_from_report(
        selected_artifact,
        mode="sahi",
        known_source_packet_ids=known_packet_ids,
        known_visual_review_ids=known_visual_ids,
        known_visual_localization_ids=known_visual_localization_ids,
    )
    if not executable_windows:
        raise ValueError("Approved child recovery requires at least one executable approved recovery action.")
    validate_recovery_selection(selected_artifact, executable_windows, source_total_frames=source_total_frames)
    candidate_id = _single_recovery_candidate_id(selected_artifact)
    candidate_output_dir = missing_ball_candidate_output_dir(parent_output_dir, candidate_id)
    if candidate_output_dir.exists():
        raise FileExistsError(str(candidate_output_dir))

    config = load_config(config_path)
    validate_output_csv_name(config.output.csv_name)
    config.input_video = Path(input_video)
    config.output_dir = candidate_output_dir
    selected_frame_budget = sum(int(window["end_frame"]) - int(window["start_frame"]) + 1 for window in executable_windows)
    _configure_recovery_run(config, selected_frame_budget)

    parent_fingerprints = capture_parent_fingerprints(
        parent_output_dir,
        watched_paths=[Path(config_path), Path(input_video)],
    )
    child_artifact_path = candidate_output_dir / APPROVED_ACTIONS_FILE_NAME
    child_config_path = candidate_output_dir / APPROVED_RECOVERY_CONFIG_NAME
    candidate_output_dir.mkdir(parents=True, exist_ok=False)
    try:
        copy_candidate_inputs(
            parent_output_dir=parent_output_dir,
            candidate_output_dir=candidate_output_dir,
            selected_artifact=selected_artifact,
            csv_name=config.output.csv_name,
        )
        child_artifact_path.write_text(json.dumps(selected_artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write_recovery_config(child_config_path, config)
        high_recall_report = _run_recovery(config, runner=runner, source_total_frames=source_total_frames)
        write_candidate_audit(candidate_output_dir, csv_name=config.output.csv_name)
        run_snapshot = _candidate_run_snapshot(
            run_id=run_id or f"missing_ball_{candidate_id}",
            parent_run_id=parent_run_id,
            config_name=config_name,
            config_path=child_config_path,
            input_video=Path(input_video),
            output_dir=candidate_output_dir,
            notes=notes,
        )
        write_run_manifest_and_metrics_preserving_candidate_audit(candidate_output_dir, run_snapshot)
        assert_parent_fingerprints_unchanged(parent_fingerprints)
        registration = write_missing_ball_candidate_comparison_and_manifest(
            parent_output_dir=parent_output_dir,
            candidate_output_dir=candidate_output_dir,
            selected_artifact=selected_artifact,
            csv_name=config.output.csv_name,
            high_recall_report=high_recall_report,
        )
        return registration
    except Exception:
        if candidate_output_dir.exists():
            shutil.rmtree(candidate_output_dir, ignore_errors=True)
        raise


def validate_recovery_selection(
    selected_artifact: dict[str, Any],
    executable_windows: list[dict[str, Any]],
    *,
    source_total_frames: int | None,
) -> None:
    if _has_localize_window(executable_windows) and source_total_frames is None:
        raise ValueError("Approved child recovery with localize_ball_roi requires a known source frame count.")
    if _has_full_video_localize_window(executable_windows, source_total_frames):
        raise ValueError("Approved child recovery rejects full-video localize_ball_roi scope.")
    if _has_source_clamped_invalid_localize_window(executable_windows, source_total_frames):
        raise ValueError("Approved child recovery rejects localize_ball_roi outside the source-clamped frame window.")
    missing_candidate_ids = _recovery_actions_without_candidate_id(selected_artifact)
    if missing_candidate_ids:
        raise ValueError(
            "Approved child recovery requires candidate_id for selected recovery actions: "
            + ", ".join(missing_candidate_ids)
        )
    _single_recovery_candidate_id(selected_artifact)


def copy_candidate_inputs(
    *,
    parent_output_dir: Path,
    candidate_output_dir: Path,
    selected_artifact: dict[str, Any],
    csv_name: str,
) -> None:
    track_names = ["ball_track.csv", "ball_track.cleaned.csv"]
    if csv_name and csv_name not in track_names:
        track_names.insert(0, csv_name)
    for name in track_names:
        source_path = parent_output_dir / name
        if source_path.is_file():
            shutil.copy2(source_path, candidate_output_dir / name)
    for name in ("review_packets.json", "ai_visual_review.json", "ai_visual_localization.json"):
        source_path = parent_output_dir / name
        if source_path.is_file():
            shutil.copy2(source_path, candidate_output_dir / name)


def write_recovery_config(config_path: Path, config: AppConfig) -> None:
    with Path(config_path).open("w", encoding="utf-8") as handle:
        yaml.safe_dump(_jsonable(config), handle, sort_keys=False, allow_unicode=False)


def write_candidate_audit(candidate_output_dir: Path, *, csv_name: str) -> None:
    candidate_track = preferred_track_path(candidate_output_dir, csv_name=csv_name)
    if not candidate_track.exists():
        audit_payload = build_ball_audit_report(candidate_output_dir)
    else:
        with tempfile.TemporaryDirectory() as temp_name:
            audit_source_dir = Path(temp_name)
            shutil.copy2(candidate_track, audit_source_dir / "ball_track.csv")
            audit_payload = build_ball_audit_report(audit_source_dir)
    (candidate_output_dir / "ball_audit.json").write_text(
        json.dumps(audit_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_run_manifest_and_metrics_preserving_candidate_audit(output_dir: Path, run: dict[str, Any]) -> None:
    existing_audit = _read_json(output_dir / "ball_audit.json")
    write_run_artifacts(output_dir, run)
    if existing_audit is not None:
        (output_dir / "ball_audit.json").write_text(
            json.dumps(existing_audit, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (output_dir / "metrics_report.json").write_text(
            json.dumps(build_metrics_report(output_dir), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def write_missing_ball_candidate_comparison_and_manifest(
    *,
    parent_output_dir: Path,
    candidate_output_dir: Path,
    selected_artifact: dict[str, Any],
    csv_name: str,
    high_recall_report: dict[str, Any] | None,
) -> dict[str, Any]:
    recovery_actions = _recovery_actions_with_execution_roi(_selected_recovery_actions(selected_artifact), high_recall_report)
    approval = dict(recovery_actions[0])
    approval["related_approvals"] = recovery_actions
    candidate_id = str(approval.get("candidate_id") or "").strip()
    baseline_track = preferred_track_path(parent_output_dir, csv_name=csv_name)
    candidate_track = preferred_track_path(candidate_output_dir, csv_name=csv_name)
    comparison_path = write_missing_ball_recovery_comparison(
        candidate_output_dir,
        baseline_track,
        candidate_track,
        candidate_id=candidate_id,
        approval=approval,
        target_window=combined_recovery_action_window(recovery_actions),
        candidate_audit_path=candidate_output_dir / "ball_audit.json",
        require_candidate_audit=True,
        review_packets_path=candidate_output_dir / "review_packets.json",
        require_packet_coverage=True,
    )
    return register_missing_ball_candidate(
        parent_output_dir=parent_output_dir,
        candidate_output_dir=candidate_output_dir,
        comparison_path=comparison_path,
        candidate_id=candidate_id,
    )


def register_missing_ball_candidate(
    *,
    parent_output_dir: Path,
    candidate_output_dir: Path,
    comparison_path: Path,
    candidate_id: str,
) -> dict[str, Any]:
    parent_output_dir = Path(parent_output_dir).resolve()
    candidate_output_dir = Path(candidate_output_dir).resolve()
    comparison_path = Path(comparison_path).resolve()
    comparison_payload = json.loads(comparison_path.read_text(encoding="utf-8"))
    comparison_payload["comparison_report"] = comparison_path.relative_to(parent_output_dir).as_posix()
    comparison_payload["candidate_dir"] = candidate_output_dir.relative_to(parent_output_dir).as_posix()
    manifest_path = candidate_output_dir / "candidate_manifest.json"
    manifest_relative_path = manifest_path.relative_to(parent_output_dir).as_posix()
    comparison_payload["candidate_manifest"] = manifest_relative_path
    candidate_artifacts = missing_ball_candidate_artifacts(
        parent_output_dir=parent_output_dir,
        candidate_output_dir=candidate_output_dir,
        comparison_path=comparison_path,
        comparison_payload=comparison_payload,
    )
    if manifest_relative_path not in candidate_artifacts:
        candidate_artifacts.append(manifest_relative_path)
    comparison_payload["candidate_artifacts"] = candidate_artifacts
    write_missing_ball_candidate_manifest(
        parent_output_dir=parent_output_dir,
        candidate_output_dir=candidate_output_dir,
        comparison_path=comparison_path,
        manifest_path=manifest_path,
        comparison_payload=comparison_payload,
    )
    comparison_path.write_text(json.dumps(comparison_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    existing = load_candidate_registry(parent_output_dir)
    artifact_status = existing.get("artifact_status")
    if artifact_status not in {"loaded", "missing"}:
        raise RuntimeError(f"Cannot update parent ai_candidate_registry.json while it is {artifact_status}.")
    existing_records = existing.get("candidates") if artifact_status == "loaded" else []
    records = [
        record
        for record in existing_records
        if isinstance(record, dict) and record.get("candidate_id") != candidate_id
    ]
    write_candidate_registry(parent_output_dir, records=records, comparison_reports=[comparison_payload])
    return comparison_payload


def missing_ball_candidate_artifacts(
    *,
    parent_output_dir: Path,
    candidate_output_dir: Path,
    comparison_path: Path,
    comparison_payload: dict[str, Any],
) -> list[str]:
    artifacts: list[str] = []

    def add_path(raw_path: Any) -> None:
        if not isinstance(raw_path, str) or not raw_path.strip():
            return
        raw_path_obj = Path(raw_path)
        candidate_paths = (
            [raw_path_obj.resolve()]
            if raw_path_obj.is_absolute()
            else [(parent_output_dir / raw_path_obj).resolve(), (candidate_output_dir / raw_path_obj).resolve()]
        )
        for path in candidate_paths:
            try:
                path.relative_to(candidate_output_dir)
                relative = path.relative_to(parent_output_dir)
            except ValueError:
                continue
            relative_text = relative.as_posix()
            if relative_text not in artifacts:
                artifacts.append(relative_text)
            return

    for raw_artifact in comparison_payload.get("candidate_artifacts", []):
        add_path(raw_artifact)
    candidate = comparison_payload.get("candidate")
    if isinstance(candidate, dict):
        add_path(candidate.get("path"))
    for path in (
        candidate_output_dir / "ball_track.csv",
        candidate_output_dir / "ball_track.cleaned.csv",
        comparison_path,
        candidate_output_dir / "candidate_manifest.json",
        candidate_output_dir / "ball_audit.json",
        candidate_output_dir / "metrics_report.json",
        candidate_output_dir / "run_manifest.json",
        candidate_output_dir / APPROVED_ACTIONS_FILE_NAME,
        candidate_output_dir / APPROVED_RECOVERY_CONFIG_NAME,
    ):
        if path.exists():
            add_path(str(path))
    return artifacts


def write_missing_ball_candidate_manifest(
    *,
    parent_output_dir: Path,
    candidate_output_dir: Path,
    comparison_path: Path,
    manifest_path: Path,
    comparison_payload: dict[str, Any],
) -> None:
    approval = comparison_payload.get("approval") if isinstance(comparison_payload.get("approval"), dict) else {}
    related = approval.get("related_approvals") if isinstance(approval.get("related_approvals"), list) else []
    approval_items = [item for item in related if isinstance(item, dict)] or ([approval] if approval else [])
    source_packet_ids: list[str] = []
    visual_review_ids: list[str] = []
    visual_localization_ids: list[str] = []
    effective_rois: list[dict[str, Any]] = []
    for item in approval_items:
        _append_unique_string(source_packet_ids, item.get("source_packet_id"))
        _append_unique_string(visual_review_ids, item.get("visual_review_id"))
        _append_unique_string(visual_localization_ids, item.get("visual_localization_id"))
        effective_roi = item.get("effective_roi")
        if isinstance(effective_roi, list) and len(effective_roi) == 4:
            effective_rois.append({"approval_id": item.get("approval_id"), "effective_roi": list(effective_roi)})
    metrics = comparison_payload.get("metrics") if isinstance(comparison_payload.get("metrics"), dict) else {}
    target_window = metrics.get("target_window") if isinstance(metrics.get("target_window"), dict) else None
    baseline = comparison_payload.get("baseline") if isinstance(comparison_payload.get("baseline"), dict) else {}
    manifest = {
        "schema_version": "1.0",
        "generated_at": _utc_now_iso(),
        "candidate_id": comparison_payload.get("candidate_id"),
        "problem_type": "missing_ball",
        "candidate_dir": candidate_output_dir.relative_to(parent_output_dir).as_posix(),
        "baseline_output_dir": str(parent_output_dir),
        "baseline_track": baseline.get("path"),
        "source_approval_ids": comparison_payload.get("consumed_approval_ids")
        if isinstance(comparison_payload.get("consumed_approval_ids"), list)
        else [],
        "frame_window": target_window,
        "evidence_ids": {
            "source_packet_ids": source_packet_ids,
            "visual_review_ids": visual_review_ids,
            "visual_localization_ids": visual_localization_ids,
        },
        "effective_rois": effective_rois,
        "comparison_report": comparison_path.relative_to(parent_output_dir).as_posix(),
        "comparison_status": comparison_payload.get("comparison_status"),
        "generated_artifacts": comparison_payload.get("candidate_artifacts")
        if isinstance(comparison_payload.get("candidate_artifacts"), list)
        else [],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def traceable_approval_provenance_ids(output_dir: Path) -> tuple[set[str] | None, set[str] | None, set[str] | None]:
    packet_ids: set[str] = set()
    visual_review_ids: set[str] = set()
    visual_localization_ids: set[str] = set()
    review_packets = _read_json(output_dir / "review_packets.json")
    if isinstance(review_packets, dict):
        for packet in _list_dicts(review_packets.get("packets")):
            for key in ("packet_id", "source_packet_id", "id"):
                _add_string_value(packet_ids, packet.get(key))
            source = packet.get("source") if isinstance(packet.get("source"), dict) else {}
            for key in ("source_packet_id", "id"):
                _add_string_value(packet_ids, source.get(key))
    visual_review = _read_json(output_dir / "ai_visual_review.json")
    if isinstance(visual_review, dict):
        for item in _list_dicts(visual_review.get("reviews")):
            _add_string_value(visual_review_ids, item.get("visual_review_id"))
            for key in ("source_packet_id", "packet_id"):
                _add_string_value(packet_ids, item.get(key))
            review = item.get("review") if isinstance(item.get("review"), dict) else {}
            _add_string_value(visual_review_ids, review.get("visual_review_id"))
            for key in ("source_packet_id", "packet_id"):
                _add_string_value(packet_ids, review.get(key))
            provenance = review.get("provenance") if isinstance(review.get("provenance"), dict) else {}
            _add_string_value(visual_review_ids, provenance.get("visual_review_id"))
            _add_string_value(packet_ids, provenance.get("source_packet_id"))
    visual_localization = _read_json(output_dir / "ai_visual_localization.json")
    if isinstance(visual_localization, dict):
        for item in _visual_localization_items(visual_localization):
            for source in _visual_localization_sources(item):
                _add_string_value(visual_localization_ids, source.get("visual_localization_id"))
    return packet_ids, visual_review_ids, visual_localization_ids


def capture_parent_fingerprints(parent_output_dir: Path, *, watched_paths: list[Path] | None = None) -> dict[str, tuple[int, str] | None]:
    paths = [
        parent_output_dir / "ball_track.csv",
        parent_output_dir / "ball_track.cleaned.csv",
        parent_output_dir / "follow_cam.mp4",
        parent_output_dir / "highlight.mp4",
        *(watched_paths or []),
    ]
    return {str(path.resolve()): _file_fingerprint(path) if path.is_file() else None for path in paths}


def assert_parent_fingerprints_unchanged(fingerprints: dict[str, tuple[int, str] | None]) -> None:
    for path_text, expected in fingerprints.items():
        path = Path(path_text)
        if expected is None:
            if path.exists():
                raise RuntimeError(f"Parent run artifact changed during approved child rerun: {path.name}")
            continue
        if not path.is_file() or _file_fingerprint(path) != expected:
            raise RuntimeError(f"Parent run artifact changed during approved child rerun: {path.name}")


def preferred_track_path(output_dir: Path, *, csv_name: str) -> Path:
    preferred = output_dir / csv_name
    if preferred.exists():
        return preferred
    raw = output_dir / "ball_track.csv"
    if raw.exists():
        return raw
    cleaned = output_dir / "ball_track.cleaned.csv"
    if cleaned.exists():
        return cleaned
    return preferred


def combined_recovery_action_window(actions: list[dict[str, Any]]) -> dict[str, int] | None:
    windows: list[dict[str, int]] = []
    for action in actions:
        window = action.get("rerun_scope") if isinstance(action.get("rerun_scope"), dict) else action
        start = _optional_int(window.get("start_frame")) if isinstance(window, dict) else None
        end = _optional_int(window.get("end_frame")) if isinstance(window, dict) else None
        if start is not None and end is not None:
            windows.append({"start_frame": min(start, end), "end_frame": max(start, end)})
    if not windows:
        return None
    return {
        "start_frame": min(window["start_frame"] for window in windows),
        "end_frame": max(window["end_frame"] for window in windows),
    }


def _configure_recovery_run(config: AppConfig, selected_frame_budget: int) -> None:
    config.postprocess.enabled = False
    config.follow_cam.enabled = False
    config.temporal_chunks.enabled = False
    config.high_recall_windows.enabled = True
    config.high_recall_windows.margin_frames = 0
    config.high_recall_windows.merge_gap_frames = 0
    config.high_recall_windows.approved_actions_path = APPROVED_ACTIONS_FILE_NAME
    config.high_recall_windows.approved_only = True
    max_approved_frames = config.high_recall_windows.max_total_frames or DEFAULT_HIGH_RECALL_MAX_TOTAL_FRAMES
    if max_approved_frames is not None and selected_frame_budget > int(max_approved_frames):
        raise ValueError(
            f"Approved child recovery frame budget {selected_frame_budget} exceeds "
            f"high_recall_windows.max_total_frames {max_approved_frames}."
        )
    config.high_recall_windows.max_total_frames = selected_frame_budget


def _run_recovery(config: AppConfig, *, runner: HighRecallRunner | None, source_total_frames: int | None) -> dict[str, Any] | None:
    selected_runner = runner or run_high_recall_windows
    report = selected_runner(config, source_total_frames=source_total_frames)
    high_recall_report = report if isinstance(report, dict) else None
    windows = high_recall_report.get("windows") if isinstance(high_recall_report, dict) else None
    execution = high_recall_report.get("execution") if isinstance(high_recall_report, dict) else None
    execution_status = execution.get("status") if isinstance(execution, dict) else None
    if not windows or execution_status == "skipped":
        raise RuntimeError("Approved child recovery produced no executable windows.")
    return high_recall_report


def _candidate_run_snapshot(
    *,
    run_id: str,
    parent_run_id: str | None,
    config_name: str | None,
    config_path: Path,
    input_video: Path,
    output_dir: Path,
    notes: Any,
) -> dict[str, Any]:
    now = _utc_now_iso()
    return {
        "run_id": run_id,
        "source": "approved_child_rerun",
        "status": "completed",
        "created_at": now,
        "started_at": now,
        "completed_at": now,
        "config_name": config_name,
        "config_path": str(config_path),
        "input_video": str(input_video),
        "parent_run_id": parent_run_id,
        "output_dir": str(output_dir),
        "modules_enabled": {
            "postprocess": False,
            "follow_cam": False,
            "temporal_chunks": False,
            "high_recall_windows": True,
        },
        "notes": notes,
        "error": None,
    }


def _selected_recovery_actions(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    actions = artifact.get("approved_actions") if isinstance(artifact.get("approved_actions"), list) else []
    return [
        action
        for action in actions
        if isinstance(action, dict) and action.get("approved_action") in MISSING_BALL_RECOVERY_ACTIONS
    ]


def _single_recovery_candidate_id(artifact: dict[str, Any]) -> str:
    candidate_ids: set[str] = set()
    for action in _selected_recovery_actions(artifact):
        candidate_id = action.get("candidate_id")
        if isinstance(candidate_id, str) and candidate_id.strip():
            candidate_ids.add(candidate_id.strip())
    if not candidate_ids:
        raise ValueError("Approved child recovery requires selected recovery actions to include candidate_id.")
    if len(candidate_ids) > 1:
        raise ValueError("Approved child recovery requires selected recovery actions to share one candidate_id.")
    return next(iter(candidate_ids))


def _recovery_actions_without_candidate_id(artifact: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for action in _selected_recovery_actions(artifact):
        candidate_id = action.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            missing.append(str(action.get("approval_id") or action.get("improvement_id") or "<unknown>"))
    return missing


def _has_localize_window(windows: list[dict[str, Any]]) -> bool:
    return any(_approved_window_is_localize_ball_roi(window) for window in windows)


def _has_full_video_localize_window(windows: list[dict[str, Any]], source_total_frames: int | None) -> bool:
    if source_total_frames is None or source_total_frames <= 0:
        return False
    localize_windows: list[dict[str, Any]] = []
    for window in windows:
        if not _approved_window_is_localize_ball_roi(window):
            continue
        localize_windows.append(window)
        start_frame = _optional_int(window.get("start_frame"))
        end_frame = _optional_int(window.get("end_frame"))
        if start_frame is not None and end_frame is not None and start_frame <= 0 and end_frame >= source_total_frames - 1:
            return True
    return _localize_windows_cover_full_video(localize_windows, source_total_frames)


def _has_source_clamped_invalid_localize_window(windows: list[dict[str, Any]], source_total_frames: int | None) -> bool:
    if source_total_frames is None or source_total_frames <= 0:
        return False
    for window in windows:
        if not _approved_window_is_localize_ball_roi(window):
            continue
        start_frame = _optional_int(window.get("start_frame"))
        end_frame = _optional_int(window.get("end_frame"))
        if start_frame is None or end_frame is None:
            return True
        if end_frame < start_frame:
            start_frame, end_frame = end_frame, start_frame
        if start_frame >= source_total_frames:
            return True
        end_frame = min(end_frame, source_total_frames - 1)
        roi = window.get("local_search_roi") if isinstance(window.get("local_search_roi"), dict) else {}
        roi_frame = _optional_int(roi.get("frame"))
        if roi_frame is None or roi_frame < start_frame or roi_frame > end_frame:
            return True
    return False


def _localize_windows_cover_full_video(windows: list[dict[str, Any]], source_total_frames: int) -> bool:
    coverage_end = -1
    for window in sorted(windows, key=lambda item: (int(item["start_frame"]), int(item["end_frame"]))):
        start = int(window["start_frame"])
        end = int(window["end_frame"])
        if start > coverage_end + 1:
            return False
        coverage_end = max(coverage_end, end)
        if coverage_end >= source_total_frames - 1:
            return True
    return False


def _approved_window_is_localize_ball_roi(window: dict[str, Any]) -> bool:
    if window.get("approved_action") == "localize_ball_roi":
        return True
    provenance = window.get("approval_provenance")
    if not isinstance(provenance, list):
        return False
    return any(isinstance(item, dict) and item.get("approved_action") == "localize_ball_roi" for item in provenance)


def _recovery_actions_with_execution_roi(
    actions: list[dict[str, Any]],
    high_recall_report: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    effective_roi_by_approval_id: dict[str, list[Any]] = {}
    windows = high_recall_report.get("windows") if isinstance(high_recall_report, dict) else None
    if isinstance(windows, list):
        for window in windows:
            if not isinstance(window, dict):
                continue
            effective_roi = window.get("effective_roi")
            if not isinstance(effective_roi, list) or len(effective_roi) != 4:
                continue
            approval_ids: set[str] = set()
            approval_id = window.get("approval_id")
            if isinstance(approval_id, str) and approval_id.strip():
                approval_ids.add(approval_id.strip())
            provenance = window.get("approval_provenance")
            if isinstance(provenance, list):
                for item in provenance:
                    if not isinstance(item, dict):
                        continue
                    provenance_approval_id = item.get("approval_id")
                    if isinstance(provenance_approval_id, str) and provenance_approval_id.strip():
                        approval_ids.add(provenance_approval_id.strip())
            for approval_id_value in approval_ids:
                effective_roi_by_approval_id[approval_id_value] = list(effective_roi)
    enriched: list[dict[str, Any]] = []
    for action in actions:
        copied = dict(action)
        approval_id = str(copied.get("approval_id") or "").strip()
        if approval_id and "effective_roi" not in copied and approval_id in effective_roi_by_approval_id:
            copied["effective_roi"] = effective_roi_by_approval_id[approval_id]
        enriched.append(copied)
    return enriched


def _file_fingerprint(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return path.stat().st_size, digest.hexdigest()


def _append_unique_string(target: list[str], value: Any) -> None:
    if isinstance(value, str) and value.strip() and value.strip() not in target:
        target.append(value.strip())


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _list_dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _visual_localization_items(report: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for key in ("requests", "localizations", "reviews"):
        items.extend(_list_dicts(report.get(key)))
    return items


def _visual_localization_sources(item: dict[str, Any]) -> list[dict[str, Any]]:
    sources = [item]
    for key in ("localization", "review", "provenance"):
        value = item.get(key)
        if isinstance(value, dict):
            sources.append(value)
    frames = item.get("frames")
    if isinstance(frames, list):
        sources.extend(frame for frame in frames if isinstance(frame, dict))
    return sources


def _add_string_value(target: set[str], value: Any) -> None:
    if isinstance(value, str) and value.strip():
        target.add(value.strip())


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
