from __future__ import annotations

# ruff: noqa: E402
import argparse
import csv
import json
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

PYTHON_BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PYTHON_BACKEND_ROOT.parent
sys.path.insert(0, str(PYTHON_BACKEND_ROOT))

from football_tracking.ai_improvement import write_ai_improvement_report
from football_tracking.ai_improvement_quality_gate import (
    write_ai_improvement_quality_gate,
    write_track_hash_snapshot,
)
from football_tracking.ai_visual_review import write_ai_visual_review_report
from football_tracking.ai_visual_localization import REPORT_NAME as AI_VISUAL_LOCALIZATION_REPORT_NAME
from football_tracking.ai_visual_localization import write_ai_visual_localization_report
from football_tracking.chunk_runner import run_high_recall_windows
from football_tracking.config import load_config
from football_tracking.final_artifact_manifest import FINALIZATION_OUTPUT_ROLES, write_final_artifact_manifest
from football_tracking.follow_cam_candidate_comparison import FOLLOW_CAM_CANDIDATE_COMPARISON_NAME
from football_tracking.follow_cam_candidate_executor import execute_follow_cam_candidate
from football_tracking.highlight_candidate_comparison import HIGHLIGHT_CANDIDATE_COMPARISON_NAME
from football_tracking.highlight_candidate_executor import execute_highlight_candidate
from football_tracking.metrics import build_metrics_report
from football_tracking.missing_ball_candidate_executor import execute_missing_ball_candidate
from football_tracking.noise_candidate_comparison import execute_noise_cleanup_candidate
from football_tracking.review_packets import write_review_packet_report

SCHEMA_VERSION = "1.0"
DEFAULT_REPORT_NAME = "stable_ai_improvement_workflow_report.json"
HASH_SNAPSHOT_REPORT_NAME = "ai_improvement_hash_snapshots.json"
QUALITY_GATE_REPORT_NAME = "ai_improvement_quality_gate.json"
AI_IMPROVEMENT_REPORT_NAME = "ai_improvement_report.json"
APPROVED_ACTIONS_NAME = "ai_improvement_approved_actions.json"
MISSING_BALL_RESOLUTION_NAME = "missing_ball_resolution.json"
FINAL_ARTIFACT_MANIFEST_NAME = "final_ai_improvement_artifact_manifest.json"
MISSING_BALL_APPROVAL_ACTIONS = {"targeted_rerun", "rerun_ball_window", "localize_ball_roi"}
NOOP_NOT_VISIBLE_ACTIONS = {"manual_review", "resolve_not_visible", "not_visible", "mark_ball_not_visible"}
NOISE_APPROVAL_ACTIONS = {"noise_filter_adjustment", "tighten_noise_filter", "reject_noise"}
FOLLOW_CAM_APPROVAL_ACTIONS = {"adjust_follow_cam", "tracking_rerun_before_follow_cam"}
HIGHLIGHT_APPROVAL_ACTIONS = {"adjust_highlight_window", "render_suggested_highlight"}
SINGLE_ACTION_ID_ACTIONS = FOLLOW_CAM_APPROVAL_ACTIONS | HIGHLIGHT_APPROVAL_ACTIONS
WORKFLOW_PROBLEM_TYPES = ("missing_ball", "noise", "follow_cam", "highlight")

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
    candidate_intent: str | None = None,
    targeted_localization_windows: list[str] | None = None,
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
    if candidate_intent is not None and candidate_intent not in {"review_only", "suggest_candidates", "prepare_approved_candidates"}:
        raise ValueError("candidate_intent must be review_only, suggest_candidates, or prepare_approved_candidates")

    approval_ids = _normalize_approval_ids(approval_ids or [])
    targeted_localization_windows = list(targeted_localization_windows or [])
    approved_action_id = _normalize_approval_id(approved_action_id)
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

    workflow_started_at = _utc_now_iso()
    workflow_timer = perf_counter()
    stages.append(
        _timed_stage(
            lambda: _metrics_artifacts_refresh(output_dir=output_dir, dry_run=dry_run, warnings=warnings)
        )
    )

    stages.append(
        _timed_stage(
            lambda: _hash_snapshot_stage(output_dir, "before_review")
        )
    )

    stages.append(
        _timed_stage(
            lambda: _review_packets_stage(
                output_dir=output_dir,
                input_video=input_video,
                dry_run=dry_run,
                warnings=warnings,
            )
        )
    )
    stages.append(
        _timed_stage(
            lambda: _visual_review_stage(
                output_dir=output_dir,
                dry_run=dry_run,
                gate_mode=gate_mode,
                model=model,
                warnings=warnings,
            )
        )
    )
    if targeted_localization_windows:
        stages.append(
            _timed_stage(
                lambda: _targeted_visual_localization_stage(
                    output_dir=output_dir,
                    input_video=input_video,
                    windows=targeted_localization_windows,
                    dry_run=dry_run,
                    gate_mode=gate_mode,
                    model=model,
                    warnings=warnings,
                )
            )
        )
    if approved_actions_path is not None and not approval_ids and approved_action_id is None:
        warnings.append("approved actions path supplied without approval ids; no approval action will execute by path alone.")
    stages.append(
        _timed_stage(
            lambda: _ai_improvement_stage(
                output_dir=output_dir,
                dry_run=dry_run,
                gate_mode=gate_mode,
                model=model,
                candidate_intent=candidate_intent,
            )
        )
    )

    stages.append(
        _timed_stage(
            lambda: _hash_snapshot_stage(output_dir, "after_ai_improvement")
        )
    )

    bounded_windows = _bounded_approved_windows(
        approved_payload.get("approved_actions", []),
        approval_ids=approval_ids,
        approved_action_id=approved_action_id,
        approved_actions_path=approved_actions_path,
    )
    dispatcher_stage = _timed_stage(
        lambda: _selected_approval_dispatcher_stage(
            output_dir=output_dir,
            input_video=input_video,
            approved_payload=approved_payload,
            dry_run=dry_run,
        )
    )
    stages.append(dispatcher_stage)
    stages.append(
        _timed_stage(
            lambda: _approved_child_rerun_stage(
                dry_run=dry_run,
                approval_intent=approval_intent,
                bounded_windows=bounded_windows,
                missing_ball_execution_path=dispatcher_stage.get("missing_ball_execution_path"),
            )
        )
    )
    stages.append(
        _timed_stage(
            lambda: _follow_cam_rerender_plan_stage(
                approval_intent=approval_intent,
                approved_payload=approved_payload,
                follow_cam_execution_path=dispatcher_stage.get("follow_cam_candidate_execution_path"),
            )
        )
    )
    stages.append(
        _timed_stage(
            lambda: _highlight_render_stage(
                approval_intent=approval_intent,
                approved_payload=approved_payload,
                highlight_execution_path=dispatcher_stage.get("highlight_candidate_execution_path"),
            )
        )
    )
    noop_stage, resolved_noop_candidates = _timed_stage(
        lambda: _missing_ball_noop_resolution_stage(output_dir, approved_payload)
    )
    stages.append(noop_stage)

    gate_approved_payload = _quality_gate_approved_payload(
        approved_payload,
        approval_ids=approval_ids,
        approved_action_id=approved_action_id,
    )
    gate_kwargs: dict[str, Any] = {"report_name": QUALITY_GATE_REPORT_NAME, "mode": gate_mode}
    if gate_approved_payload:
        gate_kwargs["approved_actions_payload"] = gate_approved_payload
    pre_manifest_quality_gate_stage, quality_gate = _timed_stage(
        lambda: _quality_gate_stage(
            output_dir=output_dir,
            gate_kwargs=gate_kwargs,
            gate_mode=gate_mode,
            workflow_status=_workflow_status(stages),
            name="pre_manifest_quality_gate",
        )
    )
    stages.append(pre_manifest_quality_gate_stage)
    manifest_stage = _timed_stage(
        lambda: _final_artifact_manifest_stage(
            output_dir,
            approved_payload=approved_payload,
            dispatcher_stage=dispatcher_stage,
            resolved_noop_candidates=resolved_noop_candidates,
            quality_gate_summary=quality_gate["summary"],
        )
    )
    stages.append(manifest_stage)
    final_workflow_status = _workflow_status(stages)
    quality_gate_stage, quality_gate = _timed_stage(
        lambda: _quality_gate_stage(
            output_dir=output_dir,
            gate_kwargs=gate_kwargs,
            gate_mode=gate_mode,
            workflow_status=final_workflow_status,
            name="quality_gate",
            sync_final_manifest=True,
        )
    )
    stages.append(quality_gate_stage)
    workflow_finished_at = _utc_now_iso()

    approval_selection = _approval_selection_summary(approved_payload, approved_actions_path=approved_actions_path)
    final_manifest = _read_json_if_available(output_dir / FINAL_ARTIFACT_MANIFEST_NAME)
    workflow_summary = _workflow_summary(
        approved_payload=approved_payload,
        approval_selection=approval_selection,
        final_manifest=final_manifest,
        quality_gate_summary=quality_gate["summary"],
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
            "candidate_intent": candidate_intent,
            "targeted_localization_windows": targeted_localization_windows,
            "approval_intent": approval_intent,
            "approval_selection": approval_selection,
        },
        "stages": stages,
        "stage_timing": _stage_timing_summary(
            stages,
            started_at=workflow_started_at,
            finished_at=workflow_finished_at,
            total_elapsed_seconds=perf_counter() - workflow_timer,
        ),
        "workflow_summary": workflow_summary,
        "produced_artifacts": produced_artifacts,
        "quality_gate": {
            "mode": gate_mode,
            "summary": quality_gate["summary"],
            "checks": quality_gate.get("checks", {}),
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
        "--targeted-localization-window",
        action="append",
        default=[],
        help="Request AI visual localization evidence for a bounded window, formatted start:end:label.",
    )
    parser.add_argument(
        "--candidate-intent",
        choices=("review_only", "suggest_candidates", "prepare_approved_candidates"),
        default=None,
        help="AI candidate intent, distinct from quality-gate mode.",
    )
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
            candidate_intent=args.candidate_intent,
            targeted_localization_windows=args.targeted_localization_window,
            quality_gate_mode=args.mode,
            report_name=args.report_name,
        )
    except ValueError as exc:
        parser.error(str(exc))

    print(json.dumps({"stable_ai_improvement_workflow": report["quality_gate"]["summary"]}, ensure_ascii=False, indent=2))
    failed = report["quality_gate"]["summary"].get("status") == "fail" or report["quality_gate"]["summary"].get("workflow_status") == "failed"
    return 1 if failed and not args.dry_run else 0


def _timed_stage(build_stage: Callable[[], Any]) -> Any:
    started_at = _utc_now_iso()
    timer = perf_counter()
    result = build_stage()
    finished_at = _utc_now_iso()
    elapsed_seconds = perf_counter() - timer
    if isinstance(result, tuple) and result and isinstance(result[0], dict):
        return (_stamp_stage_timing(result[0], started_at, finished_at, elapsed_seconds), *result[1:])
    if isinstance(result, dict):
        return _stamp_stage_timing(result, started_at, finished_at, elapsed_seconds)
    return result


def _stamp_stage_timing(
    stage: dict[str, Any],
    started_at: str | None = None,
    finished_at: str | None = None,
    elapsed_seconds: float = 0.0,
) -> dict[str, Any]:
    if started_at is None:
        started_at = _utc_now_iso()
    if finished_at is None:
        finished_at = started_at
    stage["started_at"] = started_at
    stage["finished_at"] = finished_at
    stage["elapsed_seconds"] = round(max(0.0, elapsed_seconds), 6)
    return stage


def _stage_timing_summary(
    stages: list[dict[str, Any]],
    *,
    started_at: str,
    finished_at: str,
    total_elapsed_seconds: float,
) -> dict[str, Any]:
    return {
        "started_at": started_at,
        "finished_at": finished_at,
        "total_elapsed_seconds": round(max(0.0, total_elapsed_seconds), 6),
        "stage_count": len(stages),
        "stages": [
            {
                "name": stage.get("name"),
                "status": stage.get("status"),
                "started_at": stage.get("started_at"),
                "finished_at": stage.get("finished_at"),
                "elapsed_seconds": stage.get("elapsed_seconds"),
            }
            for stage in stages
        ],
    }


def _hash_snapshot_stage(output_dir: Path, stage_name: str) -> dict[str, Any]:
    snapshot = write_track_hash_snapshot(output_dir, stage_name)
    return {
        "name": f"{stage_name}_hash_snapshot",
        "status": "succeeded",
        "artifact": HASH_SNAPSHOT_REPORT_NAME,
        "snapshot": _snapshot_summary(snapshot),
    }


def _quality_gate_stage(
    *,
    output_dir: Path,
    gate_kwargs: dict[str, Any],
    gate_mode: str,
    workflow_status: str,
    name: str = "quality_gate",
    sync_final_manifest: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    quality_gate = write_ai_improvement_quality_gate(output_dir, **gate_kwargs)
    quality_gate["summary"]["workflow_status"] = workflow_status
    (output_dir / QUALITY_GATE_REPORT_NAME).write_text(
        json.dumps(quality_gate, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if sync_final_manifest:
        _sync_final_manifest_quality_gate_status(output_dir, quality_gate["summary"])
    return (
        {
            "name": name,
            "status": quality_gate["summary"]["status"],
            "mode": gate_mode,
            "artifact": QUALITY_GATE_REPORT_NAME,
            "summary": quality_gate["summary"],
        },
        quality_gate,
    )


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


def _visual_review_stage(
    *,
    output_dir: Path,
    dry_run: bool,
    gate_mode: str,
    model: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    if dry_run:
        return {
            "name": "visual_review",
            "status": "planned",
            "enabled": False,
            "artifact": "ai_visual_review.json",
            "reason": "Dry-run records the visual review stage without calling a provider.",
        }
    if gate_mode != "real":
        return {
            "name": "visual_review",
            "status": "skipped",
            "enabled": False,
            "artifact": "ai_visual_review.json",
            "reason": "Provider-backed visual review runs only in real mode.",
        }
    try:
        report = write_ai_visual_review_report(output_dir, model=model, dry_run=False)
    except Exception as exc:  # pragma: no cover - defensive CLI reporting
        warnings.append(f"visual review unavailable: {exc}")
        return {
            "name": "visual_review",
            "status": "unavailable",
            "enabled": True,
            "artifact": "ai_visual_review.json",
            "error": str(exc),
        }
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    summary_status = summary.get("status") if isinstance(summary.get("status"), str) else "ok"
    stage_status = "succeeded" if summary_status in {"ok", "warn"} else summary_status
    return {
        "name": "visual_review",
        "status": stage_status,
        "enabled": True,
        "artifact": "ai_visual_review.json",
        "model": report.get("model") or model,
        "model_selection": report.get("model_selection") if isinstance(report.get("model_selection"), dict) else {},
        "summary": summary,
    }


def _targeted_visual_localization_stage(
    *,
    output_dir: Path,
    input_video: Path | None,
    windows: list[str],
    dry_run: bool,
    gate_mode: str,
    model: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    if not windows:
        return {
            "name": "targeted_visual_localization",
            "status": "skipped",
            "enabled": False,
            "artifact": AI_VISUAL_LOCALIZATION_REPORT_NAME,
            "reason": "No targeted localization windows were requested.",
        }
    if input_video is None or not input_video.exists():
        warnings.append("input video missing or not supplied; targeted visual localization is unavailable.")
    provider_dry_run = dry_run or gate_mode != "real"
    try:
        report = write_ai_visual_localization_report(
            output_dir,
            input_video,
            windows,
            model=model,
            dry_run=provider_dry_run,
        )
    except Exception as exc:  # pragma: no cover - defensive CLI reporting
        warnings.append(f"targeted visual localization unavailable: {exc}")
        return {
            "name": "targeted_visual_localization",
            "status": "unavailable",
            "enabled": True,
            "artifact": AI_VISUAL_LOCALIZATION_REPORT_NAME,
            "error": str(exc),
            "requested_windows": windows,
        }
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    summary_status = summary.get("status") if isinstance(summary.get("status"), str) else "ok"
    stage_status = "succeeded" if summary_status in {"ok", "warn", "planned"} else summary_status
    return {
        "name": "targeted_visual_localization",
        "status": stage_status,
        "enabled": True,
        "artifact": AI_VISUAL_LOCALIZATION_REPORT_NAME,
        "requested_windows": windows,
        "provider_dry_run": provider_dry_run,
        "provider_mode": "dry-run" if provider_dry_run else "real",
        "model": report.get("model") or model,
        "model_selection": report.get("model_selection") if isinstance(report.get("model_selection"), dict) else {},
        "summary": summary,
    }


def _ai_improvement_stage(
    *,
    output_dir: Path,
    dry_run: bool,
    gate_mode: str,
    model: str | None,
    candidate_intent: str | None,
) -> dict[str, Any]:
    provider_dry_run = dry_run or gate_mode != "real"
    resolved_candidate_intent = candidate_intent or ("review_only" if provider_dry_run else "suggest_candidates")
    report = write_ai_improvement_report(
        output_dir,
        model=model,
        dry_run=provider_dry_run,
        candidate_intent=resolved_candidate_intent,
    )
    provider_mode = report.get("provider_mode") if isinstance(report.get("provider_mode"), str) else ("dry-run" if provider_dry_run else "real")
    return {
        "name": "ai_improvement",
        "status": "succeeded",
        "artifact": AI_IMPROVEMENT_REPORT_NAME,
        "dry_run": provider_dry_run,
        "provider_dry_run": provider_dry_run,
        "provider_mode": provider_mode,
        "candidate_intent": report.get("candidate_intent") or resolved_candidate_intent,
        "model": report.get("model") or model,
        "model_selection": report.get("model_selection") if isinstance(report.get("model_selection"), dict) else {},
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
    approval_ids = _normalize_approval_ids(approval_ids)
    duplicate_ids = _duplicate_items(approval_ids)
    if duplicate_ids:
        _handle_approval_selection_error(
            f"Duplicate approval ids: {', '.join(duplicate_ids)}",
            fail=fail_on_selection_error,
            warnings=warnings,
        )
    requested_ids = _unique_items(approval_ids)
    approved_action_id = _normalize_approval_id(approved_action_id)
    single_requested_ids = [approved_action_id] if approved_action_id else []
    all_requested_ids = _unique_items([*requested_ids, *single_requested_ids])
    if approved_actions_path is None:
        if approval_ids:
            _handle_approval_selection_error(
                "--approval-ids requires --approved-actions-path in this CLI workflow.",
                fail=fail_on_selection_error,
                warnings=warnings,
            )
        if approved_action_id:
            _handle_approval_selection_error(
                "--approved-action-id requires --approved-actions-path in this CLI workflow.",
                fail=fail_on_selection_error,
                warnings=warnings,
            )
            return {
                "approval_selection": {
                    "approval_source": "none",
                    "requested_ids": [],
                    "single_action_ids": single_requested_ids,
                    "consumed_ids": [],
                    "skipped_ids": [],
                    "skipped_reasons": {},
                    "unknown_ids": [],
                    "source": "none",
                    "source_path": None,
                }
            }
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
            "approved_actions entries must include a non-empty string approval_id.",
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
    else:
        selected = []
    if single_requested_ids:
        selected = [
            action
            for action in selected
            if str(action.get("approval_id") or "") not in single_requested_ids
            or str(action.get("approved_action") or "") in SINGLE_ACTION_ID_ACTIONS
        ]
    selected = [_normalize_public_approved_action(action) for action in selected]
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


def _normalize_public_approved_action(action: dict[str, Any]) -> dict[str, Any]:
    item = dict(action)
    if item.get("approved_action") == "targeted_rerun":
        item["approved_action"] = "rerun_ball_window"
        item.setdefault("legacy_approved_action", "targeted_rerun")
    return item


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
        if str(action.get("approved_action") or "") not in {"targeted_rerun", "rerun_ball_window", "localize_ball_roi"}:
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
    missing_ball_execution_path: Any = None,
) -> dict[str, Any]:
    if not approval_intent["has_explicit_approval_intent"]:
        return {
            "name": "approved_child_rerun",
            "status": "skipped",
            "reason": "No explicit approved actions path or approval ids supplied.",
            "runs_full_video_sahi": False,
        }
    if isinstance(missing_ball_execution_path, dict) and missing_ball_execution_path.get("status") in {
        "succeeded",
        "failed",
        "partial_failure",
        "planned",
    }:
        return {
            "name": "approved_child_rerun",
            "status": missing_ball_execution_path.get("status"),
            "dry_run": dry_run,
            "execution_status": missing_ball_execution_path.get("execution_status", "not_run"),
            "api_required": False,
            "approval_ids": missing_ball_execution_path.get("approval_ids", []),
            "candidate_ids": missing_ball_execution_path.get("candidate_ids", []),
            "candidate_outputs": missing_ball_execution_path.get("candidate_outputs", []),
            "comparison_reports": missing_ball_execution_path.get("comparison_reports", []),
            "errors": missing_ball_execution_path.get("errors", []),
            "intended_rerun_mode": "sahi_roi" if any(window.get("has_roi") for window in bounded_windows) else "direct_full_frame",
            "bounded_windows": [{key: window[key] for key in ("approval_id", "start_frame", "end_frame")} for window in bounded_windows],
            "runs_full_video_sahi": False,
            "strategy": missing_ball_execution_path.get("strategy", "bounded_missing_ball_recovery_candidate"),
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
        "status": "pending_api_required",
        "dry_run": dry_run,
        "execution_status": "not_run",
        "api_required": True,
        "reason": (
            "Bounded missing-ball approvals require the PR #46 API/service child execution path; "
            "this stable workflow records the selected windows but does not mutate tracking artifacts."
        ),
        "required_executor": "api_missing_ball_candidate_execution",
        "intended_rerun_mode": "sahi_roi" if any(window.get("has_roi") for window in bounded_windows) else "direct_full_frame",
        "bounded_windows": [{key: window[key] for key in ("approval_id", "start_frame", "end_frame")} for window in bounded_windows],
        "runs_full_video_sahi": False,
    }


def _selected_approval_dispatcher_stage(
    *,
    output_dir: Path,
    input_video: Path | None,
    approved_payload: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    actions = approved_payload.get("approved_actions") if isinstance(approved_payload.get("approved_actions"), list) else []
    missing_ball_actions: list[dict[str, Any]] = []
    noise_actions: list[dict[str, Any]] = []
    follow_cam_actions: list[dict[str, Any]] = []
    highlight_actions: list[dict[str, Any]] = []
    unsupported_actions: list[dict[str, Any]] = []
    noop_actions: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        approved_action = str(action.get("approved_action") or "")
        if approved_action in MISSING_BALL_APPROVAL_ACTIONS:
            missing_ball_actions.append(action)
        elif approved_action in NOISE_APPROVAL_ACTIONS:
            noise_actions.append(action)
        elif approved_action in FOLLOW_CAM_APPROVAL_ACTIONS:
            follow_cam_actions.append(action)
        elif approved_action in HIGHLIGHT_APPROVAL_ACTIONS:
            highlight_actions.append(action)
        elif _is_not_visible_noop_action(action):
            noop_actions.append(action)
        else:
            unsupported_actions.append(_unsupported_action_summary(action))
    missing_ball_path = _missing_ball_candidate_execution_path(
        output_dir=output_dir,
        input_video=input_video,
        actions=missing_ball_actions,
        approved_payload=approved_payload,
        dry_run=dry_run,
    )
    noise_path = _noise_candidate_execution_path(output_dir=output_dir, actions=noise_actions, dry_run=dry_run)
    follow_cam_path = _follow_cam_candidate_execution_path(
        output_dir=output_dir,
        input_video=input_video,
        actions=follow_cam_actions,
        dry_run=dry_run,
    )
    highlight_path = _highlight_candidate_execution_path(
        output_dir=output_dir,
        input_video=input_video,
        actions=highlight_actions,
        dry_run=dry_run,
    )
    dispatcher_failed = (
        bool(unsupported_actions)
        or noise_path.get("status") == "failed"
        or highlight_path.get("status")
        in {
            "failed",
            "partial_failure",
        }
        or follow_cam_path.get("status")
        in {
            "failed",
            "partial_failure",
        }
        or missing_ball_path.get("status")
        in {
            "failed",
            "partial_failure",
        }
    )
    return {
        "name": "selected_approval_dispatcher",
        "status": "failed" if dispatcher_failed else "completed",
        "missing_ball_execution_path": missing_ball_path,
        "noop_resolution_path": {
            "status": "supported" if noop_actions else "skipped",
            "approval_ids": _approval_ids(noop_actions),
            "executor": "missing_ball_resolution",
        },
        "noise_candidate_execution_path": noise_path,
        "follow_cam_candidate_execution_path": follow_cam_path,
        "highlight_candidate_execution_path": highlight_path,
        "unsupported_actions": unsupported_actions,
    }


def _missing_ball_candidate_execution_path(
    *,
    output_dir: Path,
    input_video: Path | None,
    actions: list[dict[str, Any]],
    approved_payload: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    if not actions:
        return {
            "status": "skipped",
            "approval_ids": [],
            "candidate_ids": [],
            "execution_status": "not_run",
            "reason": "No selected missing-ball recovery approvals.",
            "runs_full_video_sahi": False,
            "strategy": "bounded_missing_ball_recovery_candidate",
        }
    approval_ids = _approval_ids(actions)
    candidate_ids = _missing_ball_candidate_ids(actions)
    if dry_run:
        return {
            "status": "planned",
            "approval_ids": approval_ids,
            "candidate_ids": candidate_ids,
            "execution_status": "not_run",
            "reason": "Dry-run records selected missing-ball recovery approvals without writing candidate artifacts.",
            "runs_full_video_sahi": False,
            "strategy": "bounded_missing_ball_recovery_candidate",
        }
    try:
        config_path = _recovery_config_path(output_dir)
        resolved_input_video = input_video or _recovery_input_video(output_dir, config_path)
        if resolved_input_video is None:
            raise ValueError("Selected missing-ball recovery requires input_video or run_manifest/config input_video.")
        reports: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        source_total_frames = _source_total_frames(output_dir)
        for candidate_id, group_actions in _group_missing_ball_actions_by_candidate_id(actions).items():
            group_approval_ids = _approval_ids(group_actions)
            try:
                reports.append(
                    execute_missing_ball_candidate(
                        output_dir,
                        {**approved_payload, "approved_actions": _executor_missing_ball_actions(group_actions)},
                        config_path=config_path,
                        input_video=resolved_input_video,
                        source_total_frames=source_total_frames,
                        runner=run_high_recall_windows,
                    )
                )
            except Exception as exc:
                errors.append(
                    {
                        "candidate_id": candidate_id,
                        "candidate_ids": [candidate_id] if candidate_id else [],
                        "approval_ids": group_approval_ids,
                        "status": "failed",
                        "execution_status": "failed",
                        "error": str(exc),
                    }
                )
        candidate_outputs = [
            {
                "id": report.get("candidate_id"),
                "candidate_id": report.get("candidate_id"),
                "problem_type": "missing_ball",
                "path": report.get("candidate_dir"),
                "status": report.get("comparison_status"),
            }
            for report in reports
            if isinstance(report.get("candidate_id"), str)
        ]
        comparison_reports = [{**report, "path": report.get("comparison_report")} for report in reports]
        status = "partial_failure" if reports and errors else "failed" if errors else "succeeded"
        return {
            "status": status,
            "approval_ids": approval_ids,
            "candidate_ids": candidate_ids,
            "execution_status": "partial_failure" if status == "partial_failure" else "failed" if errors else "executed",
            "candidate_outputs": candidate_outputs,
            "comparison_reports": comparison_reports,
            "errors": errors,
            "runs_full_video_sahi": False,
            "strategy": "bounded_missing_ball_recovery_candidate",
        }
    except Exception as exc:
        return {
            "status": "failed",
            "approval_ids": approval_ids,
            "candidate_ids": candidate_ids,
            "execution_status": "failed",
            "candidate_outputs": [],
            "comparison_reports": [],
            "errors": [
                {
                    "candidate_id": candidate_ids[0] if len(candidate_ids) == 1 else None,
                    "approval_ids": approval_ids,
                    "candidate_ids": candidate_ids,
                    "status": "failed",
                    "execution_status": "failed",
                    "error": str(exc),
                }
            ],
            "runs_full_video_sahi": False,
            "strategy": "bounded_missing_ball_recovery_candidate",
        }


def _noise_candidate_execution_path(*, output_dir: Path, actions: list[dict[str, Any]], dry_run: bool) -> dict[str, Any]:
    if not actions:
        return {
            "status": "skipped",
            "approval_ids": [],
            "candidate_ids": [],
            "execution_status": "not_run",
            "reason": "No selected noise cleanup approvals.",
        }
    approval_ids = _approval_ids(actions)
    if dry_run:
        candidate_ids = [
            candidate_id
            for action in actions
            if (candidate_id := _noise_candidate_id(action)) is not None
        ]
        return {
            "status": "planned",
            "approval_ids": approval_ids,
            "candidate_ids": candidate_ids,
            "execution_status": "not_run",
            "reason": "Dry-run records selected noise cleanup approvals without writing candidate artifacts.",
        }
    reports: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for action in actions:
        try:
            reports.append(execute_noise_cleanup_candidate(output_dir, action))
        except Exception as exc:  # pragma: no cover - defensive workflow reporting
            errors.append(
                {
                    "approval_id": _action_approval_id(action),
                    "candidate_id": _noise_candidate_id(action),
                    "error": str(exc),
                }
            )
    candidate_outputs = [
        {
            "id": report.get("candidate_id"),
            "candidate_id": report.get("candidate_id"),
            "problem_type": "noise",
            "path": report.get("candidate_dir"),
            "status": report.get("comparison_status"),
        }
        for report in reports
        if isinstance(report.get("candidate_id"), str)
    ]
    comparison_reports = [{**report, "path": report.get("comparison_report")} for report in reports]
    return {
        "status": "failed" if errors else "succeeded",
        "approval_ids": approval_ids,
        "candidate_ids": [str(report.get("candidate_id")) for report in reports if isinstance(report.get("candidate_id"), str)],
        "execution_status": "failed" if errors else "executed",
        "candidate_outputs": candidate_outputs,
        "comparison_reports": comparison_reports,
        "errors": errors,
        "runs_full_video_sahi": False,
        "strategy": "bounded_noise_cleanup_candidate",
    }


def _follow_cam_candidate_execution_path(
    *,
    output_dir: Path,
    input_video: Path | None,
    actions: list[dict[str, Any]],
    dry_run: bool,
) -> dict[str, Any]:
    if not actions:
        return {
            "status": "skipped",
            "approval_ids": [],
            "candidate_ids": [],
            "execution_status": "not_run",
            "reason": "No selected follow-cam approvals.",
        }
    approval_ids = _approval_ids(actions)
    candidate_ids = [
        candidate_id
        for action in actions
        if (candidate_id := _follow_cam_candidate_id(action)) is not None
    ]
    blocked = [
        {
            "approval_id": _action_approval_id(action),
            "candidate_id": _follow_cam_candidate_id(action),
            "error": "tracking_rerun_before_follow_cam requires linked passed tracking candidate evidence",
        }
        for action in actions
        if _follow_cam_action_requires_tracking_link(action)
        and _follow_cam_linked_tracking_candidate_id(action) is None
    ]
    blocked_approval_ids = {
        item["approval_id"]
        for item in blocked
        if isinstance(item.get("approval_id"), str)
    }
    executable_actions = [
        action
        for action in actions
        if _action_approval_id(action) not in blocked_approval_ids
    ]
    if blocked and not executable_actions:
        return {
            "status": "blocked",
            "approval_ids": approval_ids,
            "candidate_ids": candidate_ids,
            "execution_status": "blocked",
            "reason": "linked_tracking_candidate_evidence_required",
            "errors": [],
            "blocked": blocked,
        }
    if dry_run:
        return {
            "status": "planned" if not blocked else "partial_failure",
            "approval_ids": approval_ids,
            "candidate_ids": candidate_ids,
            "execution_status": "not_run" if not blocked else "partial_failure",
            "reason": "Dry-run records selected follow-cam approvals without rendering candidate artifacts."
            if not blocked
            else "linked_tracking_candidate_evidence_required",
            "blocked": blocked,
        }
    try:
        config_path = _recovery_config_path(output_dir)
        resolved_input_video = input_video or _recovery_input_video(output_dir, config_path)
        if resolved_input_video is None:
            raise ValueError("Selected follow-cam candidate requires input_video or run_manifest/config input_video.")
    except Exception as exc:
        return {
            "status": "failed",
            "approval_ids": approval_ids,
            "candidate_ids": candidate_ids,
            "execution_status": "failed",
            "candidate_outputs": [],
            "comparison_reports": [],
            "errors": [
                {
                    "approval_id": _action_approval_id(action),
                    "candidate_id": _follow_cam_candidate_id(action),
                    "approval_ids": [_action_approval_id(action)] if _action_approval_id(action) else [],
                    "candidate_ids": [_follow_cam_candidate_id(action)] if _follow_cam_candidate_id(action) else [],
                    "status": "failed",
                    "execution_status": "failed",
                    "error": str(exc),
                }
                for action in executable_actions
            ],
            "blocked": blocked,
        }
    reports: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for action in executable_actions:
        try:
            reports.append(
                execute_follow_cam_candidate(
                    output_dir,
                    action,
                    config_path=config_path,
                    input_video=resolved_input_video,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive workflow reporting
            error = {
                "approval_id": _action_approval_id(action),
                "candidate_id": _follow_cam_candidate_id(action),
                "error": str(exc),
            }
            if "requires linked passed tracking candidate evidence" in str(exc):
                blocked.append(error)
            else:
                errors.append(error)
    candidate_outputs = [
        {
            "id": report.get("candidate_id"),
            "candidate_id": report.get("candidate_id"),
            "problem_type": "follow_cam",
            "path": report.get("candidate_dir"),
            "type": "video",
            "status": report.get("comparison_status"),
            "candidate_artifacts": report.get("candidate_artifacts", []),
        }
        for report in reports
        if isinstance(report.get("candidate_id"), str)
    ]
    comparison_reports = [{**report, "path": report.get("comparison_report")} for report in reports]
    if errors:
        status = "partial_failure" if reports else "failed"
        execution_status = "partial_failure" if reports else "failed"
    elif blocked:
        status = "blocked" if not reports else "partial_failure"
        execution_status = "blocked" if not reports else "partial_failure"
    else:
        status = "succeeded"
        execution_status = "executed"
    return {
        "status": status,
        "approval_ids": approval_ids,
        "candidate_ids": [str(report.get("candidate_id")) for report in reports if isinstance(report.get("candidate_id"), str)]
        or candidate_ids,
        "execution_status": execution_status,
        "candidate_outputs": candidate_outputs,
        "comparison_reports": comparison_reports,
        "errors": errors,
        "blocked": blocked,
        "reason": "linked_tracking_candidate_evidence_required" if blocked and not errors else None,
        "strategy": "follow_cam_candidate_render",
    }


def _highlight_candidate_execution_path(
    *,
    output_dir: Path,
    input_video: Path | None,
    actions: list[dict[str, Any]],
    dry_run: bool,
) -> dict[str, Any]:
    if not actions:
        return {
            "status": "skipped",
            "approval_ids": [],
            "candidate_ids": [],
            "execution_status": "not_run",
            "reason": "No selected highlight approvals.",
        }
    approval_ids = _approval_ids(actions)
    candidate_ids = [
        candidate_id
        for action in actions
        if (candidate_id := _highlight_candidate_id(action)) is not None
    ]
    if dry_run:
        return {
            "status": "planned",
            "approval_ids": approval_ids,
            "candidate_ids": candidate_ids,
            "execution_status": "not_run",
            "reason": "Dry-run records selected highlight approvals without rendering candidate artifacts.",
        }
    resolved_input_video = input_video or _run_manifest_input_video(output_dir)
    reports: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for action in actions:
        try:
            reports.append(
                execute_highlight_candidate(
                    output_dir,
                    action,
                    input_video=resolved_input_video,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive workflow reporting
            errors.append(
                {
                    "approval_id": _action_approval_id(action),
                    "candidate_id": _highlight_candidate_id(action),
                    "error": str(exc),
                }
            )
    candidate_outputs = [
        {
            "id": report.get("candidate_id"),
            "candidate_id": report.get("candidate_id"),
            "problem_type": "highlight",
            "path": report.get("candidate_dir"),
            "type": "clip",
            "status": report.get("comparison_status"),
            "candidate_artifacts": report.get("candidate_artifacts", []),
        }
        for report in reports
        if isinstance(report.get("candidate_id"), str)
    ]
    comparison_reports = [{**report, "path": report.get("comparison_report")} for report in reports]
    status = "partial_failure" if reports and errors else "failed" if errors else "succeeded"
    return {
        "status": status,
        "approval_ids": approval_ids,
        "candidate_ids": [str(report.get("candidate_id")) for report in reports if isinstance(report.get("candidate_id"), str)]
        or candidate_ids,
        "execution_status": "partial_failure" if status == "partial_failure" else "failed" if errors else "executed",
        "candidate_outputs": candidate_outputs,
        "comparison_reports": comparison_reports,
        "errors": errors,
        "strategy": "highlight_candidate_render",
    }


def _group_missing_ball_actions_by_candidate_id(actions: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for action in actions:
        candidate_id = _missing_ball_candidate_id(action)
        grouped.setdefault(candidate_id or "", []).append(action)
    return grouped


def _missing_ball_candidate_ids(actions: list[dict[str, Any]]) -> list[str]:
    candidate_ids: list[str] = []
    for action in actions:
        candidate_id = _missing_ball_candidate_id(action)
        if candidate_id is not None and candidate_id not in candidate_ids:
            candidate_ids.append(candidate_id)
    return candidate_ids


def _follow_cam_rerender_plan_stage(
    *,
    approval_intent: dict[str, Any],
    approved_payload: dict[str, Any],
    follow_cam_execution_path: Any = None,
) -> dict[str, Any]:
    if isinstance(follow_cam_execution_path, dict) and follow_cam_execution_path.get("status") in {
        "succeeded",
        "failed",
        "partial_failure",
        "planned",
        "blocked",
    }:
        status = str(follow_cam_execution_path.get("status"))
        return {
            "name": "follow_cam_rerender_plan",
            "status": status,
            "artifact": FOLLOW_CAM_CANDIDATE_COMPARISON_NAME,
            "reason": follow_cam_execution_path.get("reason"),
            "execution_status": follow_cam_execution_path.get("execution_status", "not_run"),
            "approval_ids": follow_cam_execution_path.get("approval_ids", []),
            "candidate_ids": follow_cam_execution_path.get("candidate_ids", []),
            "candidate_outputs": follow_cam_execution_path.get("candidate_outputs", []),
            "comparison_reports": follow_cam_execution_path.get("comparison_reports", []),
            "errors": follow_cam_execution_path.get("errors", []),
            "blocked": follow_cam_execution_path.get("blocked", []),
        }
    actions = approved_payload.get("approved_actions") if isinstance(approved_payload.get("approved_actions"), list) else []
    approved_action_id = approval_intent.get("approved_action_id") if isinstance(approval_intent.get("approved_action_id"), str) else None
    follow_cam_actions = [
        action
        for action in actions
        if isinstance(action, dict)
        and str(action.get("approval_id") or "") == approved_action_id
        and str(action.get("approved_action") or "") in FOLLOW_CAM_APPROVAL_ACTIONS
    ]
    selected_follow_cam_without_single_id = any(
        isinstance(action, dict) and str(action.get("approved_action") or "") in FOLLOW_CAM_APPROVAL_ACTIONS for action in actions
    )
    reason = "unsupported_candidate_type" if (selected_follow_cam_without_single_id or follow_cam_actions) else "no selected follow-cam action"
    return {"name": "follow_cam_rerender_plan", "status": "skipped", "artifact": "follow_cam_rerender_plan.json", "reason": reason}


def _highlight_render_stage(
    *,
    approval_intent: dict[str, Any],
    approved_payload: dict[str, Any],
    highlight_execution_path: Any = None,
) -> dict[str, Any]:
    if isinstance(highlight_execution_path, dict) and highlight_execution_path.get("status") in {
        "succeeded",
        "failed",
        "partial_failure",
        "planned",
    }:
        return {
            "name": "highlight_render",
            "status": highlight_execution_path.get("status"),
            "artifact": HIGHLIGHT_CANDIDATE_COMPARISON_NAME,
            "reason": highlight_execution_path.get("reason"),
            "execution_status": highlight_execution_path.get("execution_status", "not_run"),
            "approval_ids": highlight_execution_path.get("approval_ids", []),
            "candidate_ids": highlight_execution_path.get("candidate_ids", []),
            "candidate_outputs": highlight_execution_path.get("candidate_outputs", []),
            "comparison_reports": highlight_execution_path.get("comparison_reports", []),
            "errors": highlight_execution_path.get("errors", []),
        }
    actions = approved_payload.get("approved_actions") if isinstance(approved_payload.get("approved_actions"), list) else []
    approved_action_id = approval_intent.get("approved_action_id") if isinstance(approval_intent.get("approved_action_id"), str) else None
    highlight_actions = [
        action
        for action in actions
        if isinstance(action, dict)
        and str(action.get("approval_id") or "") == approved_action_id
        and str(action.get("approved_action") or "") in HIGHLIGHT_APPROVAL_ACTIONS
    ]
    selected_highlight = any(
        isinstance(action, dict) and str(action.get("approved_action") or "") in HIGHLIGHT_APPROVAL_ACTIONS for action in actions
    )
    reason = "highlight_candidate_execution_not_run" if (selected_highlight or highlight_actions) else "no selected highlight action"
    return {"name": "highlight_render", "status": "skipped", "artifact": "highlight_report.json", "reason": reason}


def _missing_ball_noop_resolution_stage(output_dir: Path, approved_payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    actions = approved_payload.get("approved_actions") if isinstance(approved_payload.get("approved_actions"), list) else []
    noop_actions = [action for action in actions if isinstance(action, dict) and _is_not_visible_noop_action(action)]
    if not noop_actions:
        preserved = _resolved_noop_candidates_from_resolution(output_dir)
        return (
            {
                "name": "missing_ball_noop_resolution",
                "status": "skipped",
                "artifact": MISSING_BALL_RESOLUTION_NAME,
                "reason": "No selected evidence-backed not_visible approvals.",
                "preserved_resolution_count": len(preserved),
            },
            preserved,
        )
    requested_resolutions = [_resolution_from_action(action) for action in noop_actions]
    resolutions = [resolution for resolution in requested_resolutions if _resolution_has_not_visible_evidence(output_dir, resolution)]
    rejected_resolutions = [resolution for resolution in requested_resolutions if resolution not in resolutions]
    if not resolutions:
        _discard_artifact(output_dir / MISSING_BALL_RESOLUTION_NAME)
        return (
            {
                "name": "missing_ball_noop_resolution",
                "status": "failed",
                "artifact": MISSING_BALL_RESOLUTION_NAME,
                "reason": "Selected not_visible approvals lack covering packet or visual evidence.",
                "rejected_approval_ids": [item["approval_id"] for item in rejected_resolutions],
            },
            [],
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "summary": {
            "status": "resolved_not_visible",
            "resolution_count": len(resolutions),
            "consumed_approval_ids": [item["approval_id"] for item in resolutions],
        },
        "resolutions": resolutions,
    }
    (output_dir / MISSING_BALL_RESOLUTION_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return (
        {
            "name": "missing_ball_noop_resolution",
            "status": "succeeded",
            "artifact": MISSING_BALL_RESOLUTION_NAME,
            "resolution_count": len(resolutions),
            "consumed_approval_ids": payload["summary"]["consumed_approval_ids"],
            "rejected_approval_ids": [item["approval_id"] for item in rejected_resolutions],
        },
        [
            {
                "candidate_id": item["candidate_id"],
                "approval_id": item["approval_id"],
                "problem_type": "missing_ball",
                "status": "resolved_not_visible",
                "start_frame": item["start_frame"],
                "end_frame": item["end_frame"],
            }
            for item in resolutions
        ],
    )


def _final_artifact_manifest_stage(
    output_dir: Path,
    *,
    approved_payload: dict[str, Any],
    dispatcher_stage: dict[str, Any],
    resolved_noop_candidates: list[dict[str, Any]],
    quality_gate_summary: dict[str, Any],
) -> dict[str, Any]:
    actions = approved_payload.get("approved_actions") if isinstance(approved_payload.get("approved_actions"), list) else []
    existing_finalization = _existing_finalization_context(output_dir)
    missing_ball_path = dispatcher_stage.get("missing_ball_execution_path")
    pending_candidates = []
    if isinstance(missing_ball_path, dict) and missing_ball_path.get("status") == "pending_api_required":
        missing_ids = {
            approval_id
            for approval_id in missing_ball_path.get("approval_ids", [])
            if isinstance(approval_id, str) and approval_id.strip()
        }
        pending_candidates = [_pending_candidate_summary(action) for action in actions if _action_approval_id(action) in missing_ids]
    unsupported = dispatcher_stage.get("unsupported_actions")
    unsupported_candidates = unsupported if isinstance(unsupported, list) else []
    noise_path = dispatcher_stage.get("noise_candidate_execution_path")
    follow_cam_path = dispatcher_stage.get("follow_cam_candidate_execution_path")
    highlight_path = dispatcher_stage.get("highlight_candidate_execution_path")
    missing_ball_path = dispatcher_stage.get("missing_ball_execution_path")
    missing_ball_candidate_outputs = (
        missing_ball_path.get("candidate_outputs", [])
        if isinstance(missing_ball_path, dict) and isinstance(missing_ball_path.get("candidate_outputs"), list)
        else []
    )
    missing_ball_comparison_reports = (
        missing_ball_path.get("comparison_reports", [])
        if isinstance(missing_ball_path, dict) and isinstance(missing_ball_path.get("comparison_reports"), list)
        else []
    )
    missing_ball_rejected_candidates = (
        [_missing_ball_rejected_candidate_summary(item) for item in missing_ball_path.get("errors", [])]
        if isinstance(missing_ball_path, dict) and isinstance(missing_ball_path.get("errors"), list)
        else []
    )
    noise_candidate_outputs = (
        noise_path.get("candidate_outputs", [])
        if isinstance(noise_path, dict) and isinstance(noise_path.get("candidate_outputs"), list)
        else []
    )
    noise_comparison_reports = (
        noise_path.get("comparison_reports", [])
        if isinstance(noise_path, dict) and isinstance(noise_path.get("comparison_reports"), list)
        else []
    )
    noise_rejected_candidates = (
        [_noise_rejected_candidate_summary(item) for item in noise_path.get("errors", [])]
        if isinstance(noise_path, dict) and isinstance(noise_path.get("errors"), list)
        else []
    )
    follow_cam_candidate_outputs = (
        follow_cam_path.get("candidate_outputs", [])
        if isinstance(follow_cam_path, dict) and isinstance(follow_cam_path.get("candidate_outputs"), list)
        else []
    )
    follow_cam_comparison_reports = (
        follow_cam_path.get("comparison_reports", [])
        if isinstance(follow_cam_path, dict) and isinstance(follow_cam_path.get("comparison_reports"), list)
        else []
    )
    follow_cam_rejected_candidates = (
        [_follow_cam_rejected_candidate_summary(item) for item in follow_cam_path.get("errors", [])]
        if isinstance(follow_cam_path, dict) and isinstance(follow_cam_path.get("errors"), list)
        else []
    )
    follow_cam_blocked_candidates = (
        [_follow_cam_blocked_candidate_summary(item) for item in follow_cam_path.get("blocked", [])]
        if isinstance(follow_cam_path, dict) and isinstance(follow_cam_path.get("blocked"), list)
        else []
    )
    highlight_candidate_outputs = (
        highlight_path.get("candidate_outputs", [])
        if isinstance(highlight_path, dict) and isinstance(highlight_path.get("candidate_outputs"), list)
        else []
    )
    highlight_comparison_reports = (
        highlight_path.get("comparison_reports", [])
        if isinstance(highlight_path, dict) and isinstance(highlight_path.get("comparison_reports"), list)
        else []
    )
    highlight_rejected_candidates = (
        [_highlight_rejected_candidate_summary(item) for item in highlight_path.get("errors", [])]
        if isinstance(highlight_path, dict) and isinstance(highlight_path.get("errors"), list)
        else []
    )
    pending_candidates = _unique_json_dicts(
        [
            *existing_finalization["pending_candidates"],
            *pending_candidates,
            *_executed_pending_candidate_summaries(
                [*missing_ball_candidate_outputs, *noise_candidate_outputs, *follow_cam_candidate_outputs, *highlight_candidate_outputs],
                [*missing_ball_comparison_reports, *noise_comparison_reports, *follow_cam_comparison_reports, *highlight_comparison_reports],
            ),
            *follow_cam_blocked_candidates,
        ]
    )
    manifest = write_final_artifact_manifest(
        output_dir,
        baseline_output={"path": str(output_dir), "status": "baseline"},
        candidate_outputs=_unique_json_dicts(
            [
                *existing_finalization["candidate_outputs"],
                *missing_ball_candidate_outputs,
                *noise_candidate_outputs,
                *follow_cam_candidate_outputs,
                *highlight_candidate_outputs,
            ]
        ),
        final_artifacts=existing_finalization["final_selected_artifacts"],
        consumed_approvals=_unique_json_dicts(
            [*existing_finalization["consumed_approvals"], *[action for action in actions if isinstance(action, dict)]]
        ),
        comparison_reports=_unique_json_dicts(
            [
                *existing_finalization["comparison_reports"],
                *missing_ball_comparison_reports,
                *noise_comparison_reports,
                *follow_cam_comparison_reports,
                *highlight_comparison_reports,
            ]
        ),
        quality_gate_status=quality_gate_summary,
        rejected_candidates=[
            *missing_ball_rejected_candidates,
            *noise_rejected_candidates,
            *follow_cam_rejected_candidates,
            *highlight_rejected_candidates,
        ],
        pending_candidates=pending_candidates,
        unsupported_candidates=unsupported_candidates,
        resolved_noop_candidates=_unique_json_dicts(
            [*existing_finalization["resolved_noop_candidates"], *resolved_noop_candidates]
        ),
    )
    return {
        "name": "final_artifact_manifest",
        "status": "succeeded",
        "artifact": FINAL_ARTIFACT_MANIFEST_NAME,
        "summary": manifest.get("summary", {}),
    }


def _sync_final_manifest_quality_gate_status(output_dir: Path, quality_gate_summary: dict[str, Any]) -> None:
    path = output_dir / FINAL_ARTIFACT_MANIFEST_NAME
    payload = _read_json_if_available(path)
    if not isinstance(payload, dict):
        return
    payload["quality_gate_status"] = dict(quality_gate_summary)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _workflow_summary(
    *,
    approved_payload: dict[str, Any],
    approval_selection: dict[str, Any],
    final_manifest: dict[str, Any],
    quality_gate_summary: dict[str, Any],
) -> dict[str, Any]:
    actions = approved_payload.get("approved_actions") if isinstance(approved_payload.get("approved_actions"), list) else []
    selected_approval_ids = _approval_ids([action for action in actions if isinstance(action, dict)])
    consumed_approval_ids = _string_values(approval_selection.get("consumed_ids"))
    candidate_outputs = _dict_list(final_manifest.get("candidate_outputs"))
    comparison_reports = _dict_list(final_manifest.get("comparison_reports"))
    pending_finalization = _dict_list(final_manifest.get("pending_candidates"))
    final_selected_artifacts = _dict_list(final_manifest.get("final_selected_artifacts"))
    rejected_candidates = _dict_list(final_manifest.get("rejected_candidates"))
    unsupported_candidates = _dict_list(final_manifest.get("unsupported_candidates"))
    return {
        "selected_approval_ids": selected_approval_ids,
        "consumed_approval_ids": consumed_approval_ids,
        "candidate_outputs": candidate_outputs,
        "comparison_reports": comparison_reports,
        "quality_gate_status": dict(quality_gate_summary),
        "final_manifest": {
            "artifact": FINAL_ARTIFACT_MANIFEST_NAME,
            "summary": final_manifest.get("summary", {}) if isinstance(final_manifest.get("summary"), dict) else {},
        },
        "final_selected_artifacts": final_selected_artifacts,
        "rejected_candidates": rejected_candidates,
        "unsupported_candidates": unsupported_candidates,
        "finalization_requirements": _finalization_requirements_summary(
            actions=[action for action in actions if isinstance(action, dict)],
            selected_approval_ids=selected_approval_ids,
            consumed_approval_ids=consumed_approval_ids,
            candidate_outputs=candidate_outputs,
            comparison_reports=comparison_reports,
            pending_finalization=pending_finalization,
        ),
    }


def _finalization_requirements_summary(
    *,
    actions: list[dict[str, Any]],
    selected_approval_ids: list[str],
    consumed_approval_ids: list[str],
    candidate_outputs: list[dict[str, Any]],
    comparison_reports: list[dict[str, Any]],
    pending_finalization: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "requires_explicit_finalization": bool(pending_finalization),
        "pending_finalization_count": len(pending_finalization),
        "pending_finalization": pending_finalization,
        "output_roles_by_problem_type": _output_roles_by_problem_type(),
        "by_problem_type": _workflow_counts_by_problem_type(
            actions=actions,
            selected_approval_ids=set(selected_approval_ids),
            consumed_approval_ids=set(consumed_approval_ids),
            candidate_outputs=candidate_outputs,
            comparison_reports=comparison_reports,
            pending_finalization=pending_finalization,
        ),
    }


def _workflow_counts_by_problem_type(
    *,
    actions: list[dict[str, Any]],
    selected_approval_ids: set[str],
    consumed_approval_ids: set[str],
    candidate_outputs: list[dict[str, Any]],
    comparison_reports: list[dict[str, Any]],
    pending_finalization: list[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    counts = {
        problem_type: {
            "selected_approval_count": 0,
            "consumed_approval_count": 0,
            "candidate_output_count": 0,
            "comparison_report_count": 0,
            "pending_finalization_count": 0,
        }
        for problem_type in WORKFLOW_PROBLEM_TYPES
    }
    for action in actions:
        problem_type = _action_problem_type(action)
        if problem_type not in counts:
            continue
        approval_id = _action_approval_id(action)
        if approval_id in selected_approval_ids:
            counts[problem_type]["selected_approval_count"] += 1
        if approval_id in consumed_approval_ids:
            counts[problem_type]["consumed_approval_count"] += 1
    for item in candidate_outputs:
        problem_type = _first_string(item, ("problem_type",))
        if problem_type in counts:
            counts[problem_type]["candidate_output_count"] += 1
    for item in comparison_reports:
        problem_type = _first_string(item, ("problem_type",))
        if problem_type in counts:
            counts[problem_type]["comparison_report_count"] += 1
    for item in pending_finalization:
        problem_type = _first_string(item, ("problem_type",))
        if problem_type in counts:
            counts[problem_type]["pending_finalization_count"] += 1
    return counts


def _output_roles_by_problem_type() -> dict[str, str]:
    roles: dict[str, str] = {}
    for role, spec in FINALIZATION_OUTPUT_ROLES.items():
        problem_type = spec.get("problem_type") if isinstance(spec, dict) else None
        if isinstance(problem_type, str) and problem_type in WORKFLOW_PROBLEM_TYPES:
            roles[problem_type] = role
    return {problem_type: roles[problem_type] for problem_type in WORKFLOW_PROBLEM_TYPES if problem_type in roles}


def _action_problem_type(action: dict[str, Any]) -> str:
    explicit = _first_string(action, ("problem_type",))
    if explicit in WORKFLOW_PROBLEM_TYPES:
        return explicit
    approved_action = str(action.get("approved_action") or "")
    if approved_action in MISSING_BALL_APPROVAL_ACTIONS or _is_not_visible_noop_action(action):
        return "missing_ball"
    return _problem_type_for_action(approved_action)


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


def _is_not_visible_noop_action(action: dict[str, Any]) -> bool:
    approved_action = str(action.get("approved_action") or "")
    if approved_action not in NOOP_NOT_VISIBLE_ACTIONS:
        return False
    if str(action.get("resolution") or "").casefold().replace(" ", "_") == "not_visible":
        return True
    region = action.get("likely_ball_region") if isinstance(action.get("likely_ball_region"), dict) else {}
    description = str(region.get("description") or "").casefold().replace(" ", "_")
    status = str(region.get("status") or action.get("status") or "").casefold()
    return "not_visible" in description or status == "resolved_not_visible"


def _approval_ids(actions: list[dict[str, Any]]) -> list[str]:
    return [approval_id for action in actions if (approval_id := _action_approval_id(action))]


def _executor_missing_ball_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    executor_actions: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        item = dict(action)
        if item.get("approved_action") == "rerun_ball_window":
            item["approved_action"] = "targeted_rerun"
            item["normalized_approved_action"] = "rerun_ball_window"
        executor_actions.append(item)
    return executor_actions


def _existing_finalization_context(output_dir: Path) -> dict[str, list[dict[str, Any]]]:
    payload = _read_json_if_available(output_dir / FINAL_ARTIFACT_MANIFEST_NAME)
    return {
        "candidate_outputs": _dict_list(payload.get("candidate_outputs")),
        "final_selected_artifacts": _dict_list(payload.get("final_selected_artifacts")),
        "consumed_approvals": _dict_list(payload.get("consumed_approvals")),
        "comparison_reports": _dict_list(payload.get("comparison_reports")),
        "pending_candidates": _dict_list(payload.get("pending_candidates")),
        "resolved_noop_candidates": _dict_list(payload.get("resolved_noop_candidates")),
    }


def _dict_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _unique_json_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        key = json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _action_approval_id(action: dict[str, Any]) -> str | None:
    value = action.get("approval_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _unsupported_action_summary(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "approval_id": _action_approval_id(action),
        "problem_type": str(action.get("problem_type") or _problem_type_for_action(str(action.get("approved_action") or ""))),
        "approved_action": str(action.get("approved_action") or ""),
        "reason": "unsupported_candidate_type",
    }


def _pending_candidate_summary(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": str(action.get("candidate_id") or action.get("approval_id") or ""),
        "approval_id": _action_approval_id(action),
        "problem_type": "missing_ball",
        "approved_action": str(action.get("approved_action") or ""),
        "status": "pending_api_required",
        "execution_status": "not_run",
        "api_required": True,
        "required_executor": "api_missing_ball_candidate_execution",
    }


def _executed_pending_candidate_summaries(
    candidate_outputs: list[Any],
    comparison_reports: list[Any],
) -> list[dict[str, Any]]:
    comparison_by_candidate: dict[str, dict[str, Any]] = {}
    for report in comparison_reports:
        if not isinstance(report, dict):
            continue
        candidate_id = _first_string(report, ("candidate_id",))
        if candidate_id is None:
            candidate = report.get("candidate") if isinstance(report.get("candidate"), dict) else {}
            candidate_id = _first_string(candidate, ("id", "candidate_id"))
        if candidate_id is not None:
            comparison_by_candidate[candidate_id] = report

    pending: list[dict[str, Any]] = []
    seen: set[str] = set()
    for output in candidate_outputs:
        if not isinstance(output, dict):
            continue
        candidate_id = _first_string(output, ("candidate_id", "id"))
        if candidate_id is None or candidate_id in seen:
            continue
        seen.add(candidate_id)
        comparison = comparison_by_candidate.get(candidate_id, {})
        status = str(comparison.get("status") or output.get("comparison_status") or "unavailable")
        pending.append(
            {
                "candidate_id": candidate_id,
                "approval_id": _first_string(comparison, ("approval_id",)) or _first_string(output, ("approval_id",)),
                "approval_ids": _string_values(comparison.get("consumed_approval_ids")),
                "problem_type": _first_string(output, ("problem_type",)) or _first_string(comparison, ("problem_type",)),
                "status": "pending_finalization",
                "comparison_status": status,
                "requires_finalize_ai_candidate": True,
            }
        )
    return pending


def _missing_ball_rejected_candidate_summary(error: dict[str, Any]) -> dict[str, Any]:
    candidate_id = _first_string(error, ("candidate_id",))
    if candidate_id is None:
        candidate_ids = error.get("candidate_ids")
        if isinstance(candidate_ids, list):
            candidate_id = next((item.strip() for item in candidate_ids if isinstance(item, str) and item.strip()), None)
    approval_ids = [item for item in error.get("approval_ids", []) if isinstance(item, str) and item.strip()]
    return {
        "candidate_id": candidate_id,
        "candidate_ids": [candidate_id] if candidate_id else [],
        "approval_ids": approval_ids,
        "problem_type": "missing_ball",
        "reason": "comparison_unavailable",
        "error": str(error.get("error") or "Missing-ball recovery candidate execution failed."),
        "status": "rejected",
        "execution_status": str(error.get("execution_status") or error.get("status") or "failed"),
        "comparison_status": "unavailable",
    }


def _noise_rejected_candidate_summary(error: dict[str, Any]) -> dict[str, Any]:
    candidate_id = _first_string(error, ("candidate_id",))
    if candidate_id is None:
        candidate_ids = error.get("candidate_ids")
        if isinstance(candidate_ids, list):
            candidate_id = next((item.strip() for item in candidate_ids if isinstance(item, str) and item.strip()), None)
    approval_id = _first_string(error, ("approval_id",))
    approval_ids = [item for item in error.get("approval_ids", []) if isinstance(item, str) and item.strip()]
    if approval_id is not None and approval_id not in approval_ids:
        approval_ids.insert(0, approval_id)
    return {
        "candidate_id": candidate_id,
        "candidate_ids": [candidate_id] if candidate_id else [],
        "approval_id": approval_id,
        "approval_ids": approval_ids,
        "problem_type": "noise",
        "reason": "comparison_unavailable",
        "error": str(error.get("error") or "Noise cleanup candidate execution failed."),
        "status": "rejected",
        "execution_status": str(error.get("execution_status") or error.get("status") or "failed"),
        "comparison_status": "unavailable",
    }


def _follow_cam_rejected_candidate_summary(error: dict[str, Any]) -> dict[str, Any]:
    candidate_id = _first_string(error, ("candidate_id",))
    if candidate_id is None:
        candidate_ids = error.get("candidate_ids")
        if isinstance(candidate_ids, list):
            candidate_id = next((item.strip() for item in candidate_ids if isinstance(item, str) and item.strip()), None)
    approval_id = _first_string(error, ("approval_id",))
    approval_ids = [item for item in error.get("approval_ids", []) if isinstance(item, str) and item.strip()]
    if approval_id is not None and approval_id not in approval_ids:
        approval_ids.insert(0, approval_id)
    return {
        "candidate_id": candidate_id,
        "candidate_ids": [candidate_id] if candidate_id else [],
        "approval_id": approval_id,
        "approval_ids": approval_ids,
        "problem_type": "follow_cam",
        "reason": "comparison_unavailable",
        "error": str(error.get("error") or "Follow-cam candidate execution failed."),
        "status": "rejected",
        "execution_status": str(error.get("execution_status") or error.get("status") or "failed"),
        "comparison_status": "unavailable",
    }


def _follow_cam_blocked_candidate_summary(error: dict[str, Any]) -> dict[str, Any]:
    candidate_id = _first_string(error, ("candidate_id",))
    approval_id = _first_string(error, ("approval_id",))
    return {
        "candidate_id": candidate_id,
        "candidate_ids": [candidate_id] if candidate_id else [],
        "approval_id": approval_id,
        "approval_ids": [approval_id] if approval_id else [],
        "problem_type": "follow_cam",
        "approved_action": "tracking_rerun_before_follow_cam",
        "status": "blocked",
        "execution_status": "blocked",
        "comparison_status": "unavailable",
        "reason": "linked_tracking_candidate_evidence_required",
        "requires_linked_tracking_candidate": True,
    }


def _highlight_rejected_candidate_summary(error: dict[str, Any]) -> dict[str, Any]:
    candidate_id = _first_string(error, ("candidate_id",))
    approval_id = _first_string(error, ("approval_id",))
    approval_ids = [item for item in error.get("approval_ids", []) if isinstance(item, str) and item.strip()]
    if approval_id is not None and approval_id not in approval_ids:
        approval_ids.insert(0, approval_id)
    return {
        "candidate_id": candidate_id,
        "candidate_ids": [candidate_id] if candidate_id else [],
        "approval_id": approval_id,
        "approval_ids": approval_ids,
        "problem_type": "highlight",
        "reason": "comparison_unavailable",
        "error": str(error.get("error") or "Highlight candidate execution failed."),
        "status": "rejected",
        "execution_status": str(error.get("execution_status") or error.get("status") or "failed"),
        "comparison_status": "unavailable",
    }


def _problem_type_for_action(approved_action: str) -> str:
    if approved_action in NOISE_APPROVAL_ACTIONS:
        return "noise"
    if approved_action in FOLLOW_CAM_APPROVAL_ACTIONS:
        return "follow_cam"
    if approved_action in HIGHLIGHT_APPROVAL_ACTIONS:
        return "highlight"
    return "unknown"


def _workflow_status(stages: list[dict[str, Any]]) -> str:
    return "failed" if any(stage.get("status") == "failed" for stage in stages) else "completed"


def _noise_candidate_id(action: dict[str, Any]) -> str | None:
    value = action.get("candidate_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _follow_cam_candidate_id(action: dict[str, Any]) -> str | None:
    value = action.get("candidate_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _highlight_candidate_id(action: dict[str, Any]) -> str | None:
    value = action.get("candidate_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _follow_cam_action_requires_tracking_link(action: dict[str, Any]) -> bool:
    return str(action.get("approved_action") or "") == "tracking_rerun_before_follow_cam"


def _follow_cam_linked_tracking_candidate_id(action: dict[str, Any]) -> str | None:
    return _first_string(
        action,
        (
            "linked_tracking_candidate_id",
            "tracking_candidate_id",
            "source_candidate_id",
        ),
    )


def _missing_ball_candidate_id(action: dict[str, Any]) -> str | None:
    value = action.get("candidate_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _resolution_from_action(action: dict[str, Any]) -> dict[str, Any]:
    scope = action.get("rerun_scope") if isinstance(action.get("rerun_scope"), dict) else action
    start = _optional_int(scope.get("start_frame"))
    end = _optional_int(scope.get("end_frame"))
    approval_id = _action_approval_id(action) or ""
    candidate_id = str(action.get("candidate_id") or approval_id or "resolved_not_visible").strip()
    source_packet_id = str(action.get("source_packet_id") or "")
    resolution = {
        "candidate_id": candidate_id,
        "approval_id": approval_id,
        "problem_type": "missing_ball",
        "status": "resolved_not_visible",
        "start_frame": start if start is not None else 0,
        "end_frame": end if end is not None else start if start is not None else 0,
        "source_packet_id": source_packet_id,
        "likely_ball_region": {"description": "not_visible"},
        "evidence": action.get("evidence") if isinstance(action.get("evidence"), list) else [],
    }
    if "visual_review_id" in action:
        resolution["visual_review_id"] = action["visual_review_id"]
    if not resolution["evidence"] and source_packet_id:
        resolution["evidence"] = [{"source_packet_id": source_packet_id, "reason": "approved not_visible resolution"}]
    return resolution


def _resolved_noop_candidates_from_resolution(output_dir: Path) -> list[dict[str, Any]]:
    payload = _read_json_if_available(output_dir / MISSING_BALL_RESOLUTION_NAME)
    resolutions = payload.get("resolutions") if isinstance(payload.get("resolutions"), list) else []
    preserved: list[dict[str, Any]] = []
    for resolution in resolutions:
        if not isinstance(resolution, dict):
            continue
        if str(resolution.get("status") or "") != "resolved_not_visible":
            continue
        if not _resolution_has_not_visible_evidence(output_dir, resolution):
            continue
        preserved.append(
            {
                "candidate_id": str(resolution.get("candidate_id") or ""),
                "approval_id": str(resolution.get("approval_id") or ""),
                "problem_type": "missing_ball",
                "status": "resolved_not_visible",
                "start_frame": _optional_int(resolution.get("start_frame")) or 0,
                "end_frame": _optional_int(resolution.get("end_frame")) or 0,
            }
        )
    return preserved


def _resolution_has_not_visible_evidence(output_dir: Path, resolution: dict[str, Any]) -> bool:
    evidence_items = resolution.get("evidence") if isinstance(resolution.get("evidence"), list) else []
    for evidence in evidence_items:
        if (
            isinstance(evidence, dict)
            and _payload_says_not_visible(evidence)
            and _evidence_window_covers_resolution(evidence, resolution)
        ):
            return True
    review_packets = _read_json_if_available(output_dir / "review_packets.json")
    for packet in review_packets.get("packets", []) if isinstance(review_packets.get("packets"), list) else []:
        if not isinstance(packet, dict):
            continue
        if _first_string(packet, ("packet_id", "id", "source_packet_id")) != resolution.get("source_packet_id"):
            continue
        if _payload_says_not_visible(packet) and _evidence_window_covers_resolution(packet, resolution):
            return True
    visual_review = _read_json_if_available(output_dir / "ai_visual_review.json")
    visual_ids = {value for value in (resolution.get("visual_review_id"),) if isinstance(value, str) and value.strip()}
    for item in visual_review.get("reviews", []) if isinstance(visual_review.get("reviews"), list) else []:
        if not isinstance(item, dict):
            continue
        review = item.get("review") if isinstance(item.get("review"), dict) else item
        item_visual_ids = {
            value
            for value in (
                _first_string(item, ("visual_review_id", "id")),
                _first_string(review, ("visual_review_id", "id")),
            )
            if value is not None
        }
        packet_id = _first_string(review, ("source_packet_id", "packet_id"))
        if (
            (visual_ids and item_visual_ids & visual_ids)
            or (packet_id is not None and packet_id == resolution.get("source_packet_id"))
        ) and _payload_says_not_visible(review) and _evidence_window_covers_resolution(item, resolution):
            return True
    return False


def _evidence_window_covers_resolution(evidence: dict[str, Any], resolution: dict[str, Any]) -> bool:
    evidence_window = _window_from_payload(evidence)
    resolution_window = _window_from_payload(resolution)
    if evidence_window is None or resolution_window is None:
        return False
    return evidence_window["start_frame"] <= resolution_window["start_frame"] and evidence_window["end_frame"] >= resolution_window["end_frame"]


def _window_from_payload(payload: dict[str, Any]) -> dict[str, int] | None:
    source = payload.get("window") if isinstance(payload.get("window"), dict) else payload
    start = _optional_int(source.get("start_frame"))
    end = _optional_int(source.get("end_frame"))
    if start is None or end is None or end < start:
        return None
    return {"start_frame": start, "end_frame": end}


def _first_string(mapping: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _string_values(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _payload_says_not_visible(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_payload_says_not_visible(item) for item in value.values())
    if isinstance(value, list):
        return any(_payload_says_not_visible(item) for item in value)
    if isinstance(value, str):
        normalized = value.casefold().replace(" ", "_").replace("-", "_")
        return "not_visible" in normalized or "ball_not_visible" in normalized
    return False


def _read_json_if_available(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _discard_artifact(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


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
        AI_VISUAL_LOCALIZATION_REPORT_NAME,
        AI_IMPROVEMENT_REPORT_NAME,
        HASH_SNAPSHOT_REPORT_NAME,
        "follow_cam_rerender_plan.json",
        "highlight_report.json",
        MISSING_BALL_RESOLUTION_NAME,
        FINAL_ARTIFACT_MANIFEST_NAME,
        QUALITY_GATE_REPORT_NAME,
        "ai_candidate_registry.json",
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
                and str(action.get("approved_action") or "") in {"targeted_rerun", "rerun_ball_window", "localize_ball_roi"}
            )
        ]
    return {**approved_payload, "approved_actions": actions}


def _recovery_config_path(output_dir: Path) -> Path:
    run_manifest = _read_json_if_available(output_dir / "run_manifest.json")
    config_path = run_manifest.get("config_path")
    if isinstance(config_path, str) and config_path.strip():
        path = _resolve_run_manifest_path(config_path, output_dir=output_dir, must_exist=True)
        if path is not None:
            return path
    fallback = _resolve_run_manifest_path(Path("config") / "default.yaml", output_dir=output_dir, must_exist=True)
    if fallback is not None:
        return fallback
    raise ValueError("Selected missing-ball recovery requires run_manifest.json with a valid config_path.")


def _recovery_input_video(output_dir: Path, config_path: Path) -> Path | None:
    run_manifest = _read_json_if_available(output_dir / "run_manifest.json")
    manifest_video = run_manifest.get("input_video")
    if isinstance(manifest_video, str) and manifest_video.strip():
        return _resolve_run_manifest_path(manifest_video, output_dir=output_dir, must_exist=False)
    try:
        config = load_config(config_path)
    except Exception:
        return None
    return Path(config.input_video) if config.input_video else None


def _run_manifest_input_video(output_dir: Path) -> Path | None:
    run_manifest = _read_json_if_available(output_dir / "run_manifest.json")
    manifest_video = run_manifest.get("input_video")
    if isinstance(manifest_video, str) and manifest_video.strip():
        return _resolve_run_manifest_path(manifest_video, output_dir=output_dir, must_exist=False)
    try:
        config_path = _recovery_config_path(output_dir)
    except Exception:
        return None
    return _recovery_input_video(output_dir, config_path)


def _resolve_run_manifest_path(value: str | Path, *, output_dir: Path, must_exist: bool) -> Path | None:
    path = Path(value)
    if path.is_absolute():
        return path if not must_exist or path.exists() else None
    candidates = [Path(output_dir) / path, REPO_ROOT / path]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None if must_exist else candidates[0]


def _source_total_frames(output_dir: Path) -> int | None:
    metrics = _read_json_if_available(output_dir / "metrics_report.json")
    tracks = metrics.get("tracks") if isinstance(metrics.get("tracks"), dict) else {}
    for source_name in ("cleaned", "raw"):
        source = tracks.get(source_name) if isinstance(tracks.get(source_name), dict) else {}
        frame_count = _optional_int(source.get("frame_count"))
        if frame_count is not None and frame_count > 0:
            return frame_count
    for name in ("ball_track.cleaned.csv", "ball_track.csv"):
        frame_count = _track_frame_count(output_dir / name)
        if frame_count is not None and frame_count > 0:
            return frame_count
    return None


def _track_frame_count(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        max_frame: int | None = None
        with path.open("r", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None or "Frame" not in reader.fieldnames:
                return None
            for row in reader:
                frame = _optional_int(row.get("Frame"))
                if frame is not None and (max_frame is None or frame > max_frame):
                    max_frame = frame
        return None if max_frame is None else max_frame + 1
    except OSError:
        return None


def _parse_approval_ids(value: str | None) -> list[str]:
    if value is None or not value.strip():
        return []
    stripped = value.strip()
    if stripped.startswith("["):
        loaded = json.loads(stripped)
        if not isinstance(loaded, list):
            raise ValueError("--approval-ids JSON form must be a list")
        return _normalize_approval_ids([str(item) for item in loaded])
    return _normalize_approval_ids(stripped.split(","))


def _normalize_approval_ids(items: list[str]) -> list[str]:
    return [normalized for item in items if (normalized := _normalize_approval_id(item))]


def _normalize_approval_id(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


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
