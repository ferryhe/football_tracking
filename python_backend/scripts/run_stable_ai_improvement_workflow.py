from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PYTHON_BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PYTHON_BACKEND_ROOT))

from football_tracking.ai_improvement import write_ai_improvement_report
from football_tracking.ai_improvement_quality_gate import (
    write_ai_improvement_quality_gate,
    write_track_hash_snapshot,
)
from football_tracking.metrics import build_metrics_report
from football_tracking.review_packets import write_review_packet_report


SCHEMA_VERSION = "1.0"
DEFAULT_REPORT_NAME = "stable_ai_improvement_workflow_report.json"
HASH_SNAPSHOT_REPORT_NAME = "ai_improvement_hash_snapshots.json"
QUALITY_GATE_REPORT_NAME = "ai_improvement_quality_gate.json"
AI_IMPROVEMENT_REPORT_NAME = "ai_improvement_report.json"
APPROVED_ACTIONS_NAME = "ai_improvement_approved_actions.json"
MISSING_BALL_APPROVAL_ACTIONS = {"targeted_rerun", "localize_ball_roi"}
FOLLOW_CAM_APPROVAL_ACTIONS = {"adjust_follow_cam", "tracking_rerun_before_follow_cam"}
HIGHLIGHT_APPROVAL_ACTIONS = {"adjust_highlight_window", "render_suggested_highlight"}
SINGLE_ACTION_ID_ACTIONS = FOLLOW_CAM_APPROVAL_ACTIONS | HIGHLIGHT_APPROVAL_ACTIONS

TEMPORAL_CHUNK_SETTINGS = {
    "enabled": True,
    "chunk_frames": 1200,
    "overlap_frames": 80,
    "decode_preroll_frames": 120,
    "merge_strategy": "overlap_quality",
}


def run_workflow(
    *,
    output_dir: Path,
    input_video: Path | None = None,
    dry_run: bool = False,
    model: str | None = None,
    parallel_mode: str = "temporal",
    approved_actions_path: Path | None = None,
    approval_ids: list[str] | None = None,
    approved_action_id: str | None = None,
    quality_gate_mode: str | None = None,
    report_name: str = DEFAULT_REPORT_NAME,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    if not output_dir.exists() or not output_dir.is_dir():
        raise ValueError("output_dir must be an existing directory")
    if parallel_mode not in {"temporal", "none"}:
        raise ValueError("parallel_mode must be temporal or none")

    gate_mode = quality_gate_mode or ("dry-run" if dry_run else "artifact-only")
    if gate_mode not in {"dry-run", "artifact-only", "real"}:
        raise ValueError("quality_gate_mode must be dry-run, artifact-only, or real")

    approval_ids = [item for item in (approval_ids or []) if item]
    selection_is_dry_run = dry_run or gate_mode == "dry-run"
    warnings: list[str] = []
    stages: list[dict[str, Any]] = []
    approval_intent = _approval_intent(
        output_dir=output_dir,
        approved_actions_path=approved_actions_path,
        approval_ids=approval_ids,
        approved_action_id=approved_action_id,
        warnings=warnings,
    )
    approved_payload = _load_selected_approved_actions(
        approved_actions_path=approved_actions_path,
        approval_ids=approval_ids,
        approved_action_id=approved_action_id,
        fail_on_selection_error=not selection_is_dry_run,
        warnings=warnings,
    )

    stages.append(_metrics_artifacts_refresh(output_dir=output_dir, dry_run=dry_run, warnings=warnings))

    before_snapshot = write_track_hash_snapshot(output_dir, "before_review")
    stages.append(
        {
            "name": "before_review_hash_snapshot",
            "status": "succeeded",
            "artifact": HASH_SNAPSHOT_REPORT_NAME,
            "snapshot": _snapshot_summary(before_snapshot),
        }
    )

    stages.append(
        _review_packets_stage(
            output_dir=output_dir,
            input_video=input_video,
            dry_run=dry_run,
            warnings=warnings,
        )
    )
    stages.append(_visual_review_stage(dry_run=dry_run))
    stages.append(
        _ai_improvement_stage(
            output_dir=output_dir,
            dry_run=dry_run,
            gate_mode=gate_mode,
            model=model,
        )
    )

    after_snapshot = write_track_hash_snapshot(output_dir, "after_ai_improvement")
    stages.append(
        {
            "name": "after_ai_improvement_hash_snapshot",
            "status": "succeeded",
            "artifact": HASH_SNAPSHOT_REPORT_NAME,
            "snapshot": _snapshot_summary(after_snapshot),
        }
    )

    bounded_windows = _bounded_approved_windows(
        approved_payload.get("approved_actions", []),
        approval_ids=approval_ids,
        approved_action_id=approved_action_id,
        approved_actions_path=approved_actions_path,
    )
    stages.append(
        _approved_child_rerun_stage(
            dry_run=dry_run,
            approval_intent=approval_intent,
            bounded_windows=bounded_windows,
        )
    )
    stages.append(_follow_cam_rerender_plan_stage(approval_intent=approval_intent, approved_payload=approved_payload))
    stages.append(_highlight_render_stage(approval_intent=approval_intent, approved_payload=approved_payload))

    gate_approved_payload = _quality_gate_approved_payload(
        approved_payload,
        approval_ids=approval_ids,
        approved_action_id=approved_action_id,
    )
    gate_kwargs: dict[str, Any] = {"report_name": QUALITY_GATE_REPORT_NAME, "mode": gate_mode}
    if gate_approved_payload:
        gate_kwargs["approved_actions_payload"] = gate_approved_payload
    quality_gate = write_ai_improvement_quality_gate(output_dir, **gate_kwargs)
    stages.append(
        {
            "name": "quality_gate",
            "status": quality_gate["summary"]["status"],
            "mode": gate_mode,
            "artifact": QUALITY_GATE_REPORT_NAME,
            "summary": quality_gate["summary"],
        }
    )

    produced_artifacts = _produced_artifacts(output_dir, extra_names=[report_name])
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "dry_run": dry_run,
        "model": model,
        "parallel_mode": parallel_mode,
        "inputs": {
            "output_dir": str(output_dir),
            "input_video": str(input_video) if input_video is not None else None,
            "approved_actions_path": str(approved_actions_path) if approved_actions_path is not None else None,
            "approval_ids": approval_ids,
            "approved_action_id": approved_action_id,
            "approval_intent": approval_intent,
            "approval_selection": _approval_selection_summary(approved_payload, approved_actions_path=approved_actions_path),
        },
        "stages": stages,
        "produced_artifacts": produced_artifacts,
        "quality_gate": {
            "mode": gate_mode,
            "summary": quality_gate["summary"],
            "artifact": QUALITY_GATE_REPORT_NAME,
        },
        "strategy": _strategy(parallel_mode),
        "warnings": warnings,
    }
    (output_dir / report_name).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the stable AI improvement workflow against existing artifacts.")
    parser.add_argument("--output-dir", required=True, type=Path, help="Existing tracking output directory.")
    parser.add_argument("--input-video", type=Path, default=None, help="Optional source video path.")
    parser.add_argument("--dry-run", action="store_true", help="Plan/provider-safe workflow mode.")
    parser.add_argument("--model", default=None, help="Model recorded and passed to AI improvement when enabled.")
    parser.add_argument("--parallel-mode", choices=("temporal", "none"), default="temporal")
    parser.add_argument("--approved-actions-path", type=Path, default=None, help="Explicit approved actions JSON path.")
    parser.add_argument("--approval-ids", default=None, help="Comma-separated or JSON-list approval ids.")
    parser.add_argument("--approved-action-id", default=None, help="Explicit approved highlight or camera follow-up action id.")
    parser.add_argument(
        "--mode",
        choices=("dry-run", "artifact-only", "real"),
        default=None,
        help="Quality gate mode. Defaults to dry-run with --dry-run, otherwise artifact-only.",
    )
    parser.add_argument("--report-name", default=DEFAULT_REPORT_NAME, help="Workflow report JSON file name.")
    args = parser.parse_args(argv)

    if not args.output_dir.exists() or not args.output_dir.is_dir():
        parser.error("--output-dir must be an existing directory.")
    try:
        approval_ids = _parse_approval_ids(args.approval_ids)
        report = run_workflow(
            output_dir=args.output_dir,
            input_video=args.input_video,
            dry_run=args.dry_run,
            model=args.model,
            parallel_mode=args.parallel_mode,
            approved_actions_path=args.approved_actions_path,
            approval_ids=approval_ids,
            approved_action_id=args.approved_action_id,
            quality_gate_mode=args.mode,
            report_name=args.report_name,
        )
    except ValueError as exc:
        parser.error(str(exc))

    print(json.dumps({"stable_ai_improvement_workflow": report["quality_gate"]["summary"]}, ensure_ascii=False, indent=2))
    failed = report["quality_gate"]["summary"].get("status") == "fail"
    return 1 if failed and not args.dry_run and (args.mode == "real") else 0


def _metrics_artifacts_refresh(*, output_dir: Path, dry_run: bool, warnings: list[str]) -> dict[str, Any]:
    if dry_run:
        return {
            "name": "metrics_artifacts_refresh",
            "status": "planned",
            "mutates_heavy_artifacts": False,
            "artifact": "metrics_report.json",
        }
    try:
        metrics = build_metrics_report(output_dir)
    except Exception as exc:  # pragma: no cover - defensive CLI reporting
        warnings.append(f"metrics/artifacts refresh unavailable: {exc}")
        return {"name": "metrics_artifacts_refresh", "status": "unavailable", "error": str(exc)}
    (output_dir / "metrics_report.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"name": "metrics_artifacts_refresh", "status": "succeeded", "artifact": "metrics_report.json"}


def _review_packets_stage(
    *,
    output_dir: Path,
    input_video: Path | None,
    dry_run: bool,
    warnings: list[str],
) -> dict[str, Any]:
    if dry_run:
        return {
            "name": "review_packets",
            "status": "planned",
            "artifact": "review_packets.json",
            "media": "not_generated_in_dry_run",
        }
    if input_video is None or not input_video.exists():
        warnings.append("input video missing or not supplied; review packet media stays artifact-only.")
        return {
            "name": "review_packets",
            "status": "artifact-only",
            "artifact": "review_packets.json",
            "video_status": "unavailable",
        }
    report = write_review_packet_report(output_dir, input_video=input_video, include_media=True)
    return {
        "name": "review_packets",
        "status": "succeeded",
        "artifact": "review_packets.json",
        "video_status": "available",
        "summary": report.get("summary", {}),
    }


def _visual_review_stage(*, dry_run: bool) -> dict[str, Any]:
    return {
        "name": "visual_review",
        "status": "planned" if dry_run else "skipped",
        "enabled": False,
        "reason": "Optional provider-backed visual review is not enabled by this recipe run.",
    }


def _ai_improvement_stage(*, output_dir: Path, dry_run: bool, gate_mode: str, model: str | None) -> dict[str, Any]:
    provider_dry_run = dry_run or gate_mode != "real"
    report = write_ai_improvement_report(output_dir, model=model, dry_run=provider_dry_run)
    return {
        "name": "ai_improvement",
        "status": "succeeded",
        "artifact": AI_IMPROVEMENT_REPORT_NAME,
        "dry_run": provider_dry_run,
        "model": report.get("model") or model,
        "summary": report.get("summary", {}),
    }


def _approval_intent(
    *,
    output_dir: Path,
    approved_actions_path: Path | None,
    approval_ids: list[str],
    approved_action_id: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    implicit_path = output_dir / APPROVED_ACTIONS_NAME
    explicit = approved_actions_path is not None or bool(approval_ids) or bool(approved_action_id)
    if implicit_path.exists() and approved_actions_path is None:
        warnings.append(f"{APPROVED_ACTIONS_NAME} exists but was not passed explicitly; no approval action will execute by presence.")
    return {
        "has_explicit_approval_intent": explicit,
        "approved_actions_path_explicit": approved_actions_path is not None,
        "approval_ids_explicit": bool(approval_ids),
        "approved_action_id_explicit": bool(approved_action_id),
        "approved_action_id": approved_action_id,
    }


def _load_selected_approved_actions(
    *,
    approved_actions_path: Path | None,
    approval_ids: list[str],
    approved_action_id: str | None,
    fail_on_selection_error: bool,
    warnings: list[str],
) -> dict[str, Any]:
    duplicate_ids = _duplicate_items(approval_ids)
    if duplicate_ids:
        _handle_approval_selection_error(
            f"Duplicate approval ids: {', '.join(duplicate_ids)}",
            fail=fail_on_selection_error,
            warnings=warnings,
        )
    requested_ids = _unique_items(approval_ids)
    single_requested_ids = [approved_action_id] if approved_action_id else []
    all_requested_ids = _unique_items([*requested_ids, *single_requested_ids])
    if approved_actions_path is None:
        if approval_ids:
            _handle_approval_selection_error(
                "--approval-ids requires --approved-actions-path in this CLI workflow.",
                fail=fail_on_selection_error,
                warnings=warnings,
            )
        return {}
    try:
        loaded = json.loads(Path(approved_actions_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        _handle_approval_selection_error(
            f"approved actions could not be loaded: {exc}",
            fail=fail_on_selection_error,
            warnings=warnings,
        )
        return {}
    if not isinstance(loaded, dict):
        _handle_approval_selection_error(
            "approved actions artifact is not a JSON object.",
            fail=fail_on_selection_error,
            warnings=warnings,
        )
        return {}
    actions = loaded.get("approved_actions")
    if not isinstance(actions, list):
        _handle_approval_selection_error(
            "approved actions artifact has no approved_actions list.",
            fail=fail_on_selection_error,
            warnings=warnings,
        )
        return {}
    if any(not isinstance(action, dict) for action in actions):
        _handle_approval_selection_error(
            "approved_actions entries must be objects.",
            fail=fail_on_selection_error,
            warnings=warnings,
        )
        return {}
    selected = [action for action in actions if isinstance(action, dict)]
    missing_id_indexes = [
        index
        for index, action in enumerate(selected)
        if not isinstance(action.get("approval_id"), str) or not action.get("approval_id").strip()
    ]
    if missing_id_indexes:
        _handle_approval_selection_error(
            "approved_actions entries require approval_id must be a non-empty string.",
            fail=fail_on_selection_error,
            warnings=warnings,
        )
        return {}
    unsupported_single_action_ids = [
        str(action.get("approval_id") or "")
        for action in selected
        if str(action.get("approval_id") or "") in single_requested_ids
        and str(action.get("approved_action") or "") not in SINGLE_ACTION_ID_ACTIONS
    ]
    if unsupported_single_action_ids:
        _handle_approval_selection_error(
            f"--approved-action-id only supports follow-up actions; unsupported ids: {', '.join(unsupported_single_action_ids)}",
            fail=fail_on_selection_error,
            warnings=warnings,
        )
    available_ids = {str(action.get("approval_id") or "") for action in selected}
    available_ids.discard("")
    artifact_duplicate_ids = _duplicate_items([str(action.get("approval_id") or "") for action in selected if str(action.get("approval_id") or "")])
    if artifact_duplicate_ids:
        _handle_approval_selection_error(
            f"Duplicate approval ids in artifact: {', '.join(artifact_duplicate_ids)}",
            fail=fail_on_selection_error,
            warnings=warnings,
        )
    unknown_ids = [approval_id for approval_id in all_requested_ids if approval_id not in available_ids]
    if unknown_ids:
        _handle_approval_selection_error(
            f"Unknown approval ids: {', '.join(unknown_ids)}",
            fail=fail_on_selection_error,
            warnings=warnings,
        )
    if all_requested_ids:
        wanted = set(all_requested_ids)
        selected = [action for action in selected if str(action.get("approval_id") or "") in wanted]
    if single_requested_ids:
        selected = [
            action
            for action in selected
            if str(action.get("approval_id") or "") not in single_requested_ids
            or str(action.get("approved_action") or "") in SINGLE_ACTION_ID_ACTIONS
        ]
    consumed_ids = [str(action.get("approval_id") or "") for action in selected if str(action.get("approval_id") or "")]
    skipped_ids = [approval_id for approval_id in sorted(available_ids) if approval_id not in set(consumed_ids)]
    return {
        **loaded,
        "approved_actions": selected,
        "approval_selection": {
            "approval_source": "path",
            "requested_ids": requested_ids,
            "single_action_ids": single_requested_ids,
            "consumed_ids": consumed_ids,
            "skipped_ids": skipped_ids,
            "skipped_reasons": {approval_id: "not_requested" for approval_id in skipped_ids},
            "unknown_ids": unknown_ids,
            "source": "path",
            "source_path": str(approved_actions_path),
        },
    }


def _handle_approval_selection_error(message: str, *, fail: bool, warnings: list[str]) -> None:
    if fail:
        raise ValueError(message)
    warnings.append(message)


def _unique_items(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _duplicate_items(items: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for item in items:
        if item in seen and item not in duplicates:
            duplicates.append(item)
        seen.add(item)
    return duplicates


def _bounded_approved_windows(
    actions: Any,
    *,
    approval_ids: list[str],
    approved_action_id: str | None,
    approved_actions_path: Path | None,
) -> list[dict[str, Any]]:
    if not isinstance(actions, list):
        return []
    if approved_action_id and not approval_ids:
        return []
    allowed_ids = set(approval_ids) if approval_ids else None
    if approved_actions_path is None:
        allowed_ids = set()
    windows: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        approval_id = str(action.get("approval_id") or "")
        if allowed_ids is not None and approval_id not in allowed_ids:
            continue
        if str(action.get("approved_action") or "") not in {"targeted_rerun", "localize_ball_roi"}:
            continue
        scope = action.get("rerun_scope") if isinstance(action.get("rerun_scope"), dict) else action
        start = _optional_int(scope.get("start_frame"))
        end = _optional_int(scope.get("end_frame"))
        if start is None or end is None or end < start:
            continue
        windows.append(
            {
                "approval_id": str(action.get("approval_id") or ""),
                "start_frame": start,
                "end_frame": end,
                "has_roi": isinstance(action.get("local_search_roi"), dict),
            }
        )
    return windows


def _approved_child_rerun_stage(
    *,
    dry_run: bool,
    approval_intent: dict[str, Any],
    bounded_windows: list[dict[str, Any]],
) -> dict[str, Any]:
    if not approval_intent["has_explicit_approval_intent"]:
        return {
            "name": "approved_child_rerun",
            "status": "skipped",
            "reason": "No explicit approved actions path or approval ids supplied.",
            "runs_full_video_sahi": False,
        }
    if not bounded_windows:
        return {
            "name": "approved_child_rerun",
            "status": "skipped",
            "reason": "Explicit approval intent supplied, but no bounded approved rerun windows were loaded.",
            "runs_full_video_sahi": False,
        }
    return {
        "name": "approved_child_rerun",
        "status": "planned",
        "dry_run": dry_run,
        "rerun_mode": "sahi_roi" if any(window.get("has_roi") for window in bounded_windows) else "direct_full_frame",
        "bounded_windows": [{key: window[key] for key in ("approval_id", "start_frame", "end_frame")} for window in bounded_windows],
        "runs_full_video_sahi": False,
    }


def _follow_cam_rerender_plan_stage(*, approval_intent: dict[str, Any], approved_payload: dict[str, Any]) -> dict[str, Any]:
    actions = approved_payload.get("approved_actions") if isinstance(approved_payload.get("approved_actions"), list) else []
    follow_cam_actions = [
        action for action in actions if isinstance(action, dict) and str(action.get("approved_action") or "") in FOLLOW_CAM_APPROVAL_ACTIONS
    ]
    if not approval_intent["has_explicit_approval_intent"] or not follow_cam_actions:
        return {"name": "follow_cam_rerender_plan", "status": "skipped", "artifact": "follow_cam_rerender_plan.json"}
    return {
        "name": "follow_cam_rerender_plan",
        "status": "planned",
        "artifact": "follow_cam_rerender_plan.json",
        "approved_action_count": len(follow_cam_actions),
    }


def _highlight_render_stage(*, approval_intent: dict[str, Any], approved_payload: dict[str, Any]) -> dict[str, Any]:
    actions = approved_payload.get("approved_actions") if isinstance(approved_payload.get("approved_actions"), list) else []
    approved_action_id = approval_intent.get("approved_action_id") if isinstance(approval_intent.get("approved_action_id"), str) else None
    highlight_actions = [
        action
        for action in actions
        if isinstance(action, dict)
        and str(action.get("approval_id") or "") == approved_action_id
        and str(action.get("approved_action") or "") in HIGHLIGHT_APPROVAL_ACTIONS
    ]
    if not approval_intent["approved_action_id_explicit"] or not highlight_actions:
        return {"name": "highlight_render", "status": "skipped", "artifact": "highlight_report.json"}
    return {
        "name": "highlight_render",
        "status": "planned",
        "artifact": "highlight_report.json",
        "requires_explicit_approved_action_id": True,
        "approved_action_count": len(highlight_actions),
    }


def _strategy(parallel_mode: str) -> dict[str, Any]:
    temporal_enabled = parallel_mode == "temporal"
    return {
        "parallel_mode": parallel_mode,
        "full_video_speed_strategy": "temporal_chunks" if temporal_enabled else "single_run",
        "temporal_chunk_settings": TEMPORAL_CHUNK_SETTINGS if temporal_enabled else {"enabled": False},
        "sahi_roi_policy": {
            "full_video_sahi": "do_not_run_full_video_sahi",
            "targeted_sahi_roi": "explicit_bounded_approved_windows_only",
            "reason": "Use SAHI/ROI for bounded recovery windows, not broad full-video slicing.",
        },
    }


def _snapshot_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    files = snapshot.get("files") if isinstance(snapshot.get("files"), dict) else {}
    return {
        "stage_name": snapshot.get("stage_name"),
        "files": {
            name: {"status": details.get("status"), "sha256": details.get("sha256")}
            for name, details in files.items()
            if isinstance(details, dict)
        },
    }


def _produced_artifacts(output_dir: Path, *, extra_names: list[str]) -> list[str]:
    names = [
        "metrics_report.json",
        "review_packets.json",
        "ai_visual_review.json",
        AI_IMPROVEMENT_REPORT_NAME,
        HASH_SNAPSHOT_REPORT_NAME,
        "follow_cam_rerender_plan.json",
        "highlight_report.json",
        QUALITY_GATE_REPORT_NAME,
        *extra_names,
    ]
    return [name for name in names if (output_dir / name).exists() or name in extra_names]


def _approval_selection_summary(approved_payload: dict[str, Any], *, approved_actions_path: Path | None) -> dict[str, Any]:
    selection = approved_payload.get("approval_selection")
    if isinstance(selection, dict):
        return {
            "approval_source": selection.get("approval_source", selection.get("source")),
            "source": selection.get("source"),
            "source_path": selection.get("source_path"),
            "requested_ids": selection.get("requested_ids", []),
            "single_action_ids": selection.get("single_action_ids", []),
            "consumed_ids": selection.get("consumed_ids", []),
            "skipped_ids": selection.get("skipped_ids", []),
            "skipped_reasons": selection.get("skipped_reasons", {}),
            "unknown_ids": selection.get("unknown_ids", []),
        }
    return {
        "approval_source": "none",
        "source": "none",
        "source_path": str(approved_actions_path) if approved_actions_path is not None else None,
        "requested_ids": [],
        "single_action_ids": [],
        "consumed_ids": [],
        "skipped_ids": [],
        "skipped_reasons": {},
        "unknown_ids": [],
    }


def _quality_gate_approved_payload(
    approved_payload: dict[str, Any],
    *,
    approval_ids: list[str],
    approved_action_id: str | None,
) -> dict[str, Any]:
    if not approved_payload:
        return {}
    actions = approved_payload.get("approved_actions")
    if not isinstance(actions, list):
        return approved_payload
    if approved_action_id and not approval_ids:
        actions = [
            action
            for action in actions
            if not (
                isinstance(action, dict)
                and str(action.get("approved_action") or "") in {"targeted_rerun", "localize_ball_roi"}
            )
        ]
    return {**approved_payload, "approved_actions": actions}


def _parse_approval_ids(value: str | None) -> list[str]:
    if value is None or not value.strip():
        return []
    stripped = value.strip()
    if stripped.startswith("["):
        loaded = json.loads(stripped)
        if not isinstance(loaded, list):
            raise ValueError("--approval-ids JSON form must be a list")
        return [str(item).strip() for item in loaded if str(item).strip()]
    return [item.strip() for item in stripped.split(",") if item.strip()]


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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
