from __future__ import annotations

import csv
import json
import math
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from football_tracking.ai_contracts import (
    AI_CLIP_ACTIONS,
    AI_FAILURE_TAGS,
    AI_RECOMMENDED_ACTIONS,
    AI_ROOT_CAUSE_MODULES,
)

SCHEMA_VERSION = "1.0"
REPORT_FILE_NAME = "ai_improvement_report.json"
APPROVED_ACTIONS_FILE_NAME = "ai_improvement_approved_actions.json"
APPROVED_CONFIG_PATCH_FILE_NAME = "ai_improvement_approved_config_patch.json"
FOLLOW_CAM_RERENDER_PLAN_FILE_NAME = "follow_cam_rerender_plan.json"
MAX_CONTEXT_ITEMS = 100
CAMERA_TRACK_CONTEXT_RADIUS_FRAMES = 12
FAST_PLAY_MIN_TRACK_STEP_PX = 80.0
TRACK_JUMP_REVIEW_MIN_STEP_PX = 160.0

_SOURCE_ARTIFACTS: tuple[tuple[str, str], ...] = (
    ("ball_audit", "ball_audit.json"),
    ("ai_review_triggers", "ai_review_triggers.json"),
    ("review_packets", "review_packets.json"),
    ("ai_visual_review", "ai_visual_review.json"),
    ("camera_motion_audit", "camera_motion_audit.json"),
    ("event_candidates", "event_candidates.json"),
)

_VALID_STATUSES = {"ok", "needs_rerun", "unavailable", "error"}
_REQUIRED_IMPROVEMENT_FIELDS = (
    "priority",
    "area",
    "failure_tags",
    "root_cause_module",
    "recommended_action",
    "confidence",
)
_MISSING_BALL_TAGS = {"ball_lost", "missing_ball", "lost_gap", "ball_not_visible", "missed_ball"}
_KNOWN_FALSE_POSITIVE_CLASSES = {
    "advertising_board",
    "extra_ball",
    "foot_confusion",
    "player_head",
    "shoe_confusion",
    "sideline_confusion",
    "wall_background_drift",
    "unknown",
}
_PROVIDER_PATH_KEYS = {
    "output_dir",
    "path",
    "artifact_path",
    "input_video",
    "follow_cam_video",
    "contact_sheet",
    "crop_sheet",
    "clip",
}


def build_ai_improvement_context(output_dir: Path, max_items: int = 20) -> dict[str, Any]:
    if max_items < 1:
        raise ValueError("max_items must be at least 1.")
    if max_items > MAX_CONTEXT_ITEMS:
        raise ValueError(f"max_items must be at most {MAX_CONTEXT_ITEMS}.")

    output_dir = Path(output_dir)
    artifacts: dict[str, Any] = {}
    provenance_artifacts: dict[str, Any] = {}
    artifact_status: dict[str, str] = {}
    source_artifacts: dict[str, str | None] = {}
    warnings: list[str] = []

    for artifact_key, file_name in _SOURCE_ARTIFACTS:
        path = output_dir / file_name
        loaded, status, warning = _read_optional_json(path)
        artifact_status[artifact_key] = "available" if status == "loaded" else status
        source_artifacts[artifact_key] = file_name if status == "loaded" else None
        if warning is not None:
            warnings.append(warning)
        if loaded is not None:
            if artifact_key in {"review_packets", "ai_visual_review"}:
                provenance_artifacts[artifact_key] = loaded
            artifacts[artifact_key] = _limit_artifact_payload(artifact_key, _strip_data_urls(loaded), max_items=max_items)

    if isinstance(artifacts.get("camera_motion_audit"), dict):
        artifacts["camera_motion_audit"] = _enrich_camera_motion_audit_context(
            artifacts["camera_motion_audit"],
            output_dir=output_dir,
        )

    return {
        "output_dir": str(output_dir.resolve()),
        "max_items": max_items,
        "source_artifacts": source_artifacts,
        "artifact_status": artifact_status,
        "available_artifact_count": len(artifacts),
        "traceable_provenance": _traceable_provenance_payload({"artifacts": provenance_artifacts}),
        "artifacts": artifacts,
        "warnings": warnings,
    }


def build_ai_improvement_report(
    output_dir: Path,
    *,
    client: Any = None,
    model: str | None = None,
    dry_run: bool = False,
    max_items: int = 20,
    objective: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    context = build_ai_improvement_context(output_dir, max_items=max_items)
    selected_model, model_selection_source = _select_model(client, model)

    if context["available_artifact_count"] <= 0:
        return _report(
            output_dir=output_dir,
            context=context,
            model=selected_model,
            model_selection_source=model_selection_source,
            dry_run=dry_run,
            status="unavailable",
            warnings=context["warnings"],
        )

    if dry_run:
        return _dry_run_report(
            output_dir=output_dir,
            context=context,
            model=selected_model,
            model_selection_source=model_selection_source,
        )

    active_client = client
    if active_client is None:
        active_client = _build_default_client()
        selected_model, model_selection_source = _select_model(active_client, model)

    if hasattr(active_client, "is_enabled") and not active_client.is_enabled():
        return _report(
            output_dir=output_dir,
            context=context,
            model=selected_model,
            model_selection_source=model_selection_source,
            dry_run=False,
            status="unavailable",
            warnings=[*context["warnings"], "OpenAI provider is not configured."],
        )

    try:
        response = active_client.create_json_response(
            instructions=_instructions(language=language),
            prompt=_prompt(context=context, objective=objective, language=language),
            model=selected_model,
            temperature=0.1,
        )
        improvements, highlight_adjustments, validation_warnings, summary_status, primary_issue = _validate_model_report(
            response,
            context=context,
        )
        improvements, visual_warnings = _merge_visual_review_localization(improvements, context)
        validation_warnings.extend(visual_warnings)
    except Exception as exc:
        return _report(
            output_dir=output_dir,
            context=context,
            model=selected_model,
            model_selection_source=model_selection_source,
            dry_run=False,
            status="error",
            warnings=context["warnings"],
            error=_safe_error_message(exc, _client_api_key(active_client)),
        )

    return _report(
        output_dir=output_dir,
        context=context,
        model=selected_model,
        model_selection_source=model_selection_source,
        dry_run=False,
        status=summary_status,
        primary_issue=primary_issue,
        improvements=improvements,
        highlight_adjustments=highlight_adjustments,
        warnings=[*context["warnings"], *validation_warnings],
    )


def write_ai_improvement_report(
    output_dir: Path,
    *,
    client: Any = None,
    model: str | None = None,
    dry_run: bool = False,
    max_items: int = 20,
    objective: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    report = build_ai_improvement_report(
        output_dir,
        client=client,
        model=model,
        dry_run=dry_run,
        max_items=max_items,
        objective=objective,
        language=language,
    )
    _write_json(output_dir / REPORT_FILE_NAME, report)
    return report


def approve_ai_improvement_actions(
    output_dir: Path,
    *,
    run_id: str,
    improvement_ids: list[str],
    approved_by: str = "operator",
    approval_source: str = "api",
    rerun_scope_overrides: dict[str, dict[str, Any]] | None = None,
    local_search_roi_overrides: dict[str, dict[str, Any]] | None = None,
    config_patch_overrides: dict[str, dict[str, Any]] | None = None,
    suggested_window_overrides: dict[str, dict[str, Any]] | None = None,
    clip_action_overrides: dict[str, str] | None = None,
    follow_cam_rerender_plan_overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    if not improvement_ids:
        raise ValueError("At least one improvement_id is required.")
    report = _read_required_report(output_dir / REPORT_FILE_NAME)
    improvements = report.get("improvements")
    if not isinstance(improvements, list):
        raise ValueError("ai_improvement_report.json does not contain improvements.")
    by_id = {
        str(item.get("id")): item
        for item in improvements
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item.get("id")
    }
    selected_improvements: list[dict[str, Any]] = []
    for improvement_id in improvement_ids:
        if improvement_id not in by_id:
            raise ValueError(f"Unknown improvement_id: {improvement_id}")
        selected_improvements.append(by_id[improvement_id])

    highlight_candidates: dict[str, dict[str, Any]] = {}
    if _approval_needs_highlight_candidates(selected_improvements):
        event_candidates_payload, _event_candidates_status, event_candidates_warning = _read_optional_json(
            output_dir / "event_candidates.json"
        )
        if event_candidates_payload is None:
            raise ValueError(event_candidates_warning or "event_candidates.json could not be loaded.")
        highlight_candidates = _event_candidate_lookup(event_candidates_payload)
    roi_provenance_context = _roi_provenance_context_from_output_dir(output_dir)

    warnings: list[str] = []
    approved_actions: list[dict[str, Any]] = []
    config_patch_items: list[dict[str, Any]] = []
    approved_at = _utc_now_iso()
    for index, (improvement_id, improvement) in enumerate(zip(improvement_ids, selected_improvements), start=1):
        action, action_warnings = _approved_action_entry(
            improvement,
            approval_id=f"approval_{index:03d}",
            approved_by=approved_by,
            approval_source=approval_source,
            approved_at=approved_at,
            model=report.get("model") if isinstance(report.get("model"), str) else None,
            rerun_scope_override=(rerun_scope_overrides or {}).get(improvement_id),
            local_search_roi_override=(local_search_roi_overrides or {}).get(improvement_id),
            config_patch_override=(config_patch_overrides or {}).get(improvement_id),
            suggested_window_override=(suggested_window_overrides or {}).get(improvement_id),
            clip_action_override=(clip_action_overrides or {}).get(improvement_id),
            follow_cam_rerender_plan_override=(follow_cam_rerender_plan_overrides or {}).get(improvement_id),
            highlight_candidates=highlight_candidates,
            roi_provenance_context=roi_provenance_context,
        )
        warnings.extend(action_warnings)
        approved_actions.append(action)
        if action.get("config_patch"):
            config_patch_items.append(
                {
                    "approval_id": action["approval_id"],
                    "improvement_id": action["improvement_id"],
                    "approved_action": action["approved_action"],
                    "config_patch": action["config_patch"],
                }
            )

    artifact = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": approved_at,
        "run_id": run_id,
        "source_report": REPORT_FILE_NAME,
        "approved_by": approved_by,
        "approved_actions": approved_actions,
        "warnings": warnings,
    }
    _write_json(output_dir / APPROVED_ACTIONS_FILE_NAME, artifact)
    stale_config_patch_path = output_dir / APPROVED_CONFIG_PATCH_FILE_NAME
    if config_patch_items:
        _write_json(
            stale_config_patch_path,
            {
                "schema_version": SCHEMA_VERSION,
                "generated_at": approved_at,
                "run_id": run_id,
                "source_approved_actions": APPROVED_ACTIONS_FILE_NAME,
                "patches": config_patch_items,
                "merged_config_patch": _merge_config_patches(
                    [item["config_patch"] for item in config_patch_items if isinstance(item.get("config_patch"), dict)]
                ),
            },
        )
    elif stale_config_patch_path.exists():
        stale_config_patch_path.unlink()
    stale_follow_cam_plan_path = output_dir / FOLLOW_CAM_RERENDER_PLAN_FILE_NAME
    follow_cam_plan = _follow_cam_rerender_plan(
        run_id=run_id,
        generated_at=approved_at,
        approved_actions=approved_actions,
        improvements_by_id=by_id,
    )
    if follow_cam_plan is not None:
        _write_json(stale_follow_cam_plan_path, follow_cam_plan)
    elif stale_follow_cam_plan_path.exists():
        stale_follow_cam_plan_path.unlink()
    return artifact


def _approval_needs_highlight_candidates(improvements: list[dict[str, Any]]) -> bool:
    return any(
        str(improvement.get("recommended_action") or "") in {"adjust_highlight_window", "render_suggested_highlight"}
        for improvement in improvements
    )


def compact_ai_improvement_summary(report: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(report, dict):
        return None
    summary = report.get("summary")
    if not isinstance(summary, dict):
        return None
    status = summary.get("status")
    if not isinstance(status, str):
        return None
    compact = {
        "status": status,
        "primary_issue": summary.get("primary_issue") if isinstance(summary.get("primary_issue"), str) else None,
        "improvement_count": _safe_int(summary.get("improvement_count")),
        "targeted_rerun_count": _safe_int(summary.get("targeted_rerun_count")),
        "config_patch_count": _safe_int(summary.get("config_patch_count")),
        "highlight_adjustment_count": _safe_int(summary.get("highlight_adjustment_count")),
    }
    camera_summary = _compact_camera_improvement_counts(report, summary)
    if camera_summary:
        compact.update(camera_summary)
    return compact


def _compact_camera_improvement_counts(report: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    camera_count = _safe_int(summary.get("camera_improvement_count"))
    severity_counts = summary.get("camera_severity_counts")
    action_counts = summary.get("camera_action_counts")
    if not isinstance(severity_counts, dict) or not isinstance(action_counts, dict):
        improvements = report.get("improvements") if isinstance(report.get("improvements"), list) else []
        camera_items = [
            item
            for item in improvements
            if isinstance(item, dict)
            and (
                item.get("area") == "camera_motion"
                or item.get("recommended_action")
                in {"adjust_follow_cam", "tracking_rerun_before_follow_cam", "human_review_camera_motion"}
            )
        ]
        camera_count = len(camera_items)
        severity_counts = {}
        action_counts = {}
        for item in camera_items:
            severity = item.get("camera_motion_severity")
            if not isinstance(severity, str):
                evidence_payload = item.get("evidence_payload") if isinstance(item.get("evidence_payload"), dict) else {}
                severity = evidence_payload.get("camera_motion_severity") if isinstance(evidence_payload.get("camera_motion_severity"), str) else None
            if isinstance(severity, str) and severity:
                severity_counts[severity] = severity_counts.get(severity, 0) + 1
            action = item.get("recommended_action")
            if isinstance(action, str) and action:
                action_counts[action] = action_counts.get(action, 0) + 1
    result: dict[str, Any] = {}
    if camera_count > 0:
        result["camera_improvement_count"] = camera_count
    if isinstance(severity_counts, dict) and severity_counts:
        result["camera_severity_counts"] = {str(key): _safe_int(value) for key, value in severity_counts.items()}
    if isinstance(action_counts, dict) and action_counts:
        result["camera_action_counts"] = {str(key): _safe_int(value) for key, value in action_counts.items()}
    return result


def _follow_cam_rerender_plan(
    *,
    run_id: str,
    generated_at: str,
    approved_actions: list[dict[str, Any]],
    improvements_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    camera_actions = [
        action
        for action in approved_actions
        if action.get("approved_action") in {"adjust_follow_cam", "tracking_rerun_before_follow_cam"}
    ]
    if not camera_actions:
        return None
    tracking_actions = [
        action
        for action in camera_actions
        if action.get("approved_action") == "tracking_rerun_before_follow_cam"
    ]
    action = tracking_actions[0] if tracking_actions else camera_actions[0]
    improvement = improvements_by_id.get(str(action.get("improvement_id") or ""), {})
    approved_action = str(action.get("approved_action") or "")
    requires_tracking_rerun = approved_action == "tracking_rerun_before_follow_cam"
    reason = str(improvement.get("diagnosis") or "")
    if requires_tracking_rerun:
        reason = f"Track rerun is required before follow-cam rerender. {reason}".strip()
    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "run_id": run_id,
        "source": "ai_improvement_approved_action",
        "source_approved_actions": APPROVED_ACTIONS_FILE_NAME,
        "approval_id": action.get("approval_id"),
        "improvement_id": action.get("improvement_id"),
        "approved_action": approved_action,
        "approved_camera_action_count": len(camera_actions),
        "camera_motion_event_id": action.get("camera_motion_event_id"),
        "requires_tracking_rerun": requires_tracking_rerun,
        "recommended_config_patch": action.get("config_patch") if isinstance(action.get("config_patch"), dict) else {},
        "reason": reason,
        "provenance": action.get("provenance") if isinstance(action.get("provenance"), dict) else {},
    }
    if requires_tracking_rerun and isinstance(action.get("rerun_scope"), dict):
        plan["tracking_rerun_scope"] = dict(action["rerun_scope"])
    if isinstance(action.get("follow_cam_rerender_plan"), dict):
        for key, value in action["follow_cam_rerender_plan"].items():
            if key not in {"requires_tracking_rerun", "recommended_config_patch", "tracking_rerun_scope"}:
                plan[str(key)] = value
    if requires_tracking_rerun:
        plan["recommended_config_patch"] = {}
    return plan


def _approved_action_entry(
    improvement: dict[str, Any],
    *,
    approval_id: str,
    approved_by: str,
    approval_source: str,
    approved_at: str,
    model: str | None,
    rerun_scope_override: dict[str, Any] | None,
    local_search_roi_override: dict[str, Any] | None,
    config_patch_override: dict[str, Any] | None,
    suggested_window_override: dict[str, Any] | None,
    clip_action_override: str | None,
    follow_cam_rerender_plan_override: dict[str, Any] | None,
    highlight_candidates: dict[str, dict[str, Any]] | None = None,
    roi_provenance_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    improvement_id = str(improvement.get("id") or "")
    approved_action = str(improvement.get("recommended_action") or "")
    if approved_action not in AI_RECOMMENDED_ACTIONS:
        raise ValueError(f"Improvement {improvement_id} recommended_action is unsupported: {approved_action}")

    warnings: list[str] = []
    patch_source = config_patch_override if config_patch_override is not None else improvement.get("config_patch")
    config_patch, patch_warnings = _filter_config_patch(patch_source if isinstance(patch_source, dict) else {})
    warnings.extend(patch_warnings)
    evidence_payload = improvement.get("evidence_payload") if isinstance(improvement.get("evidence_payload"), dict) else {}

    action: dict[str, Any] = {
        "approval_id": approval_id,
        "improvement_id": improvement_id,
        "approved_action": approved_action,
        "approval_source": approval_source,
        "approved_at": approved_at,
        "approved_by": approved_by,
        "provenance": {
            "source": "ai_improvement",
            "improvement_id": improvement_id,
            "model": model,
            "confidence": improvement.get("confidence"),
        },
    }
    for key in (
        "source_packet_id",
        "visual_review_id",
        "candidate_id",
        "frame_dimensions",
        "false_positive_class",
        "camera_motion_event_id",
        "camera_motion_severity",
    ):
        value = improvement.get(key, evidence_payload.get(key))
        if value not in (None, ""):
            action[key] = value
    for key in ("start_frame", "end_frame"):
        if key in improvement:
            value = _optional_int(improvement.get(key))
            if value is not None and value >= 0:
                action[key] = value

    rerun_scope_source = rerun_scope_override if rerun_scope_override is not None else improvement.get("rerun_scope")
    if isinstance(rerun_scope_source, dict):
        action["rerun_scope"] = _frame_window(rerun_scope_source, f"Approval {approval_id} rerun_scope")
    local_roi_source = local_search_roi_override if local_search_roi_override is not None else improvement.get("local_search_roi")
    if isinstance(local_roi_source, dict):
        action["local_search_roi"] = _local_search_roi(local_roi_source, 0)
    if config_patch:
        action["config_patch"] = config_patch

    if approved_action == "targeted_rerun" and "rerun_scope" not in action:
        raise ValueError(f"Approval {approval_id} targeted_rerun requires rerun_scope.")
    if approved_action == "localize_ball_roi" and "local_search_roi" not in action:
        raise ValueError(f"Approval {approval_id} localize_ball_roi requires local_search_roi.")
    if approved_action in {"localize_ball_roi", "targeted_rerun"} and "local_search_roi" in action:
        provenance_item = dict(improvement)
        provenance_item.update(action)
        if not _has_traceable_packet_or_visual_provenance(provenance_item, roi_provenance_context):
            raise ValueError(
                f"Approval {approval_id} local_search_roi requires traceable packet or visual review provenance."
            )
    if approved_action in {"noise_filter_adjustment", "tighten_noise_filter", "reject_noise"}:
        if "false_positive_class" not in action:
            raise ValueError(f"Approval {approval_id} {approved_action} requires false_positive_class.")
        if "start_frame" not in action or "end_frame" not in action:
            raise ValueError(f"Approval {approval_id} {approved_action} requires start_frame and end_frame.")
        if int(action["end_frame"]) < int(action["start_frame"]):
            raise ValueError(f"Approval {approval_id} end_frame must be greater than or equal to start_frame.")
        if approved_action == "noise_filter_adjustment" and "config_patch" not in action:
            raise ValueError(f"Approval {approval_id} noise_filter_adjustment requires config_patch.")

    if approved_action in {"adjust_highlight_window", "render_suggested_highlight"}:
        candidate_id = action.get("candidate_id") or improvement.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise ValueError(f"Approval {approval_id} {approved_action} requires candidate_id.")
        action["candidate_id"] = candidate_id.strip()
        window_source = suggested_window_override if suggested_window_override is not None else improvement.get("suggested_window")
        action["suggested_window"] = _frame_window(window_source, f"Approval {approval_id} suggested_window")
        clip_action = clip_action_override if clip_action_override is not None else improvement.get("clip_action")
        if not isinstance(clip_action, str) or clip_action not in AI_CLIP_ACTIONS:
            raise ValueError(f"Approval {approval_id} {approved_action} requires supported clip_action.")
        action["clip_action"] = clip_action
        _validate_highlight_window_invariants(
            action["suggested_window"],
            candidate_id=action["candidate_id"],
            candidates=highlight_candidates,
            label=f"Approval {approval_id} suggested_window",
        )

    if follow_cam_rerender_plan_override is not None:
        action["follow_cam_rerender_plan"] = dict(follow_cam_rerender_plan_override)
    elif isinstance(improvement.get("follow_cam_rerender_plan"), dict):
        action["follow_cam_rerender_plan"] = dict(improvement["follow_cam_rerender_plan"])
    if approved_action == "adjust_follow_cam" and "config_patch" not in action and "follow_cam_rerender_plan" not in action:
        raise ValueError(f"Approval {approval_id} adjust_follow_cam requires config_patch or follow_cam_rerender_plan.")
    if approved_action == "tracking_rerun_before_follow_cam" and "rerun_scope" not in action:
        raise ValueError(f"Approval {approval_id} tracking_rerun_before_follow_cam requires rerun_scope.")
    if approved_action == "human_review_camera_motion":
        if "camera_motion_event_id" not in action:
            raise ValueError(f"Approval {approval_id} human_review_camera_motion requires camera_motion_event_id.")
        if "start_frame" not in action or "end_frame" not in action:
            raise ValueError(f"Approval {approval_id} human_review_camera_motion requires start_frame and end_frame.")

    return action, warnings


def _merge_visual_review_localization(
    improvements: list[dict[str, Any]],
    context: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    visual_reviews = _visual_reviews_by_packet_id(context)
    if not visual_reviews:
        return improvements, []
    warnings: list[str] = []
    merged: list[dict[str, Any]] = []
    for index, improvement in enumerate(improvements, start=1):
        item = dict(improvement)
        source_packet_id = _improvement_source_packet_id(item)
        if source_packet_id is None or source_packet_id not in visual_reviews:
            merged.append(item)
            continue
        review = visual_reviews[source_packet_id]
        evidence_payload = dict(item.get("evidence_payload") if isinstance(item.get("evidence_payload"), dict) else {})
        evidence_payload["source_packet_id"] = source_packet_id
        for key in ("visual_review_id", "frame_dimensions"):
            value = review.get(key)
            if value not in (None, "", {}):
                evidence_payload[key] = value
        provenance = review.get("provenance") if isinstance(review.get("provenance"), dict) else {"source": "ai_visual_review"}
        evidence_payload["local_search_roi_provenance"] = provenance
        item["evidence_payload"] = evidence_payload

        visible = _visual_review_says_visible(review)
        if visible is False:
            item.pop("local_search_roi", None)
            item["likely_ball_region"] = {"description": "not visible", "confidence": 0.0}
            if item.get("recommended_action") == "localize_ball_roi":
                item["recommended_action"] = "manual_review"
                warnings.append(
                    f"Improvement {item.get('id') or index} normalized from localize_ball_roi to manual_review "
                    "because ai_visual_review marked the ball not visible."
                )
            merged.append(item)
            continue

        visual_roi = review.get("local_search_roi")
        if isinstance(visual_roi, dict) and _visual_roi_is_better(item.get("local_search_roi"), visual_roi):
            try:
                item["local_search_roi"] = _local_search_roi(visual_roi, index)
            except ValueError as exc:
                warnings.append(str(exc))
        visual_region = review.get("likely_ball_region")
        if isinstance(visual_region, dict) and _likely_region_is_not_visible(item.get("likely_ball_region")):
            try:
                item["likely_ball_region"] = _likely_ball_region(visual_region, index)
            except ValueError as exc:
                warnings.append(str(exc))
        merged.append(item)
    return merged, warnings


def _visual_reviews_by_packet_id(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    artifacts = context.get("artifacts") if isinstance(context.get("artifacts"), dict) else {}
    visual_report = artifacts.get("ai_visual_review") if isinstance(artifacts.get("ai_visual_review"), dict) else {}
    reviews = visual_report.get("reviews") if isinstance(visual_report.get("reviews"), list) else []
    by_packet: dict[str, dict[str, Any]] = {}
    for item in reviews:
        if not isinstance(item, dict):
            continue
        review = item.get("review") if isinstance(item.get("review"), dict) else item
        packet_id = review.get("source_packet_id") or item.get("source_packet_id") or item.get("packet_id") or review.get("packet_id")
        if not isinstance(packet_id, str) or not packet_id:
            continue
        flattened = dict(review)
        for key in ("packet_id", "visual_review_id", "frame_dimensions", "provenance"):
            if key not in flattened and key in item:
                flattened[key] = item[key]
        by_packet[packet_id] = flattened
    return by_packet


def _roi_provenance_context_from_output_dir(output_dir: Path) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    for artifact_key, file_name in (("review_packets", "review_packets.json"), ("ai_visual_review", "ai_visual_review.json")):
        payload, status, _warning = _read_optional_json(output_dir / file_name)
        if status == "loaded" and payload is not None:
            artifacts[artifact_key] = payload
    return {"artifacts": artifacts}


def _improvement_source_packet_id(improvement: dict[str, Any]) -> str | None:
    for value in (
        improvement.get("source_packet_id"),
        (improvement.get("evidence_payload") or {}).get("source_packet_id") if isinstance(improvement.get("evidence_payload"), dict) else None,
    ):
        if isinstance(value, str) and value:
            return value
    evidence = improvement.get("evidence")
    if isinstance(evidence, list):
        for item in evidence:
            if isinstance(item, dict):
                value = item.get("source_packet_id") or item.get("packet_id")
                if isinstance(value, str) and value:
                    return value
    return None


def _visual_review_says_visible(review: dict[str, Any]) -> bool | None:
    if isinstance(review.get("visible"), bool):
        return bool(review["visible"])
    match_value = str(review.get("match_ball_visible") or "").casefold()
    if match_value in {"yes", "partial"}:
        return True
    if match_value == "no":
        return False
    region = review.get("likely_ball_region") if isinstance(review.get("likely_ball_region"), dict) else {}
    if str(region.get("description") or "").strip().casefold() == "not visible":
        return False
    return None


def _has_valid_roi(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        _local_search_roi(value, 0)
    except ValueError:
        return False
    return True


def _visual_roi_is_better(existing: Any, visual_roi: dict[str, Any]) -> bool:
    if not _has_valid_roi(existing):
        return True
    return _roi_confidence(visual_roi) > _roi_confidence(existing)


def _roi_confidence(value: Any) -> float:
    if not isinstance(value, dict):
        return -1.0
    confidence = value.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(float(confidence)):
        return -1.0
    return float(confidence)


def _likely_region_is_not_visible(value: Any) -> bool:
    if not isinstance(value, dict):
        return True
    return str(value.get("description") or "").strip().casefold() == "not visible"


def _packet_id_for_window(context: dict[str, Any], *, start_frame: int, end_frame: int) -> str | None:
    artifacts = context.get("artifacts") if isinstance(context.get("artifacts"), dict) else {}
    packet_report = artifacts.get("review_packets") if isinstance(artifacts.get("review_packets"), dict) else {}
    packets = packet_report.get("packets") if isinstance(packet_report.get("packets"), list) else []
    best_packet: tuple[int, str] | None = None
    for packet in packets:
        if not isinstance(packet, dict):
            continue
        packet_id = packet.get("packet_id")
        if not isinstance(packet_id, str) or not packet_id:
            continue
        packet_range = _packet_range(packet)
        if packet_range is None:
            continue
        overlap = _range_overlap((start_frame, end_frame), packet_range)
        if overlap <= 0:
            continue
        if best_packet is None or overlap > best_packet[0]:
            best_packet = (overlap, packet_id)
    return None if best_packet is None else best_packet[1]


def _packet_range(packet: dict[str, Any]) -> tuple[int, int] | None:
    for key in ("source", "window"):
        value = packet.get(key)
        if isinstance(value, dict):
            start = _optional_int(value.get("start_frame"))
            end = _optional_int(value.get("end_frame"))
            if start is not None and end is not None:
                return (start, end) if start <= end else (end, start)
    return None


def _range_overlap(first: tuple[int, int], second: tuple[int, int]) -> int:
    start = max(first[0], second[0])
    end = min(first[1], second[1])
    return max(0, end - start + 1)


def _read_required_report(path: Path) -> dict[str, Any]:
    loaded, status, warning = _read_optional_json(path)
    if loaded is None:
        raise FileNotFoundError(warning or path.name)
    if status != "loaded":
        raise FileNotFoundError(warning or path.name)
    return loaded


def _merge_config_patches(patches: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for patch in patches:
        merged = _deep_merge(merged, patch)
    return merged


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _enrich_camera_motion_audit_context(payload: dict[str, Any], *, output_dir: Path) -> dict[str, Any]:
    track_rows = _read_ball_track_rows(output_dir / "ball_track.csv")
    enriched = dict(payload)
    for event_key in ("review_events", "events", "motion_events"):
        events = enriched.get(event_key)
        if not isinstance(events, list):
            continue
        enriched_events: list[Any] = []
        for index, event in enumerate(events, start=1):
            if not isinstance(event, dict):
                enriched_events.append(event)
                continue
            enriched_event = dict(event)
            if not isinstance(enriched_event.get("id"), str):
                enriched_event["id"] = f"cam_event_{index:03d}"
            track_context = _camera_event_track_context(enriched_event, track_rows)
            if track_context is not None:
                enriched_event["nearby_ball_track"] = track_context
            enriched_events.append(enriched_event)
        enriched[event_key] = enriched_events
    return enriched


def _read_ball_track_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for raw in reader:
                frame = _optional_int(raw.get("Frame") or raw.get("frame"))
                if frame is None:
                    continue
                rows.append(
                    {
                        "frame": frame,
                        "status": _normalize_track_status(raw.get("Status") or raw.get("status")),
                        "point": _track_point(raw),
                    }
                )
    except (csv.Error, OSError, UnicodeDecodeError):
        return []
    return sorted(rows, key=lambda row: row["frame"])


def _track_point(raw: dict[str, Any]) -> tuple[float, float] | None:
    x = _optional_number(raw.get("X") or raw.get("x"))
    y = _optional_number(raw.get("Y") or raw.get("y"))
    if x is None or y is None:
        return None
    return x, y


def _normalize_track_status(value: Any) -> str:
    status = str(value or "").strip().casefold()
    if status == "detected":
        return "Detected"
    if status == "predicted":
        return "Predicted"
    if status == "lost":
        return "Lost"
    return "unknown"


def _camera_event_track_context(event: dict[str, Any], track_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    event_range = _event_frame_range(event)
    if event_range is None:
        return None
    window = {
        "start_frame": max(0, event_range[0] - CAMERA_TRACK_CONTEXT_RADIUS_FRAMES),
        "end_frame": event_range[1] + CAMERA_TRACK_CONTEXT_RADIUS_FRAMES,
    }
    nearby = [row for row in track_rows if window["start_frame"] <= int(row["frame"]) <= window["end_frame"]]
    if not nearby:
        return {
            "window": window,
            "status_counts": {},
            "frame_count": 0,
            "has_tracking_issue": False,
            "stable_detected": False,
            "max_step_px": None,
            "classification": "no_track_context",
        }

    status_counts: dict[str, int] = {}
    for row in nearby:
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    max_step = _max_track_step_px(nearby)
    has_status_issue = any(status_counts.get(status, 0) > 0 for status in ("Lost", "Predicted"))
    stable_detected = bool(nearby) and status_counts.get("Detected", 0) == len(nearby)
    classification = "tracking_issue" if has_status_issue else "ambiguous_status"
    if stable_detected:
        classification = "stable_detected"
    if stable_detected and max_step is not None and max_step >= TRACK_JUMP_REVIEW_MIN_STEP_PX:
        classification = "track_jump_review"
    elif stable_detected and max_step is not None and max_step >= FAST_PLAY_MIN_TRACK_STEP_PX:
        classification = "acceptable_fast_play"

    return {
        "window": window,
        "status_counts": status_counts,
        "frame_count": len(nearby),
        "has_tracking_issue": has_status_issue,
        "stable_detected": stable_detected,
        "max_step_px": round(max_step, 4) if max_step is not None else None,
        "classification": classification,
    }


def _event_frame_range(event: dict[str, Any]) -> tuple[int, int] | None:
    start = _optional_int(event.get("start_frame"))
    end = _optional_int(event.get("end_frame"))
    if start is None and end is None:
        frame = _optional_int(event.get("frame"))
        if frame is None:
            return None
        start = frame
        end = frame
    elif start is None:
        start = end
    elif end is None:
        end = start
    if start is None or end is None:
        return None
    return (start, end) if start <= end else (end, start)


def _max_track_step_px(rows: list[dict[str, Any]]) -> float | None:
    max_step: float | None = None
    previous: dict[str, Any] | None = None
    for row in rows:
        point = row.get("point")
        if point is None:
            previous = row
            continue
        if previous is not None and previous.get("point") is not None:
            step = math.dist(previous["point"], point)
            max_step = step if max_step is None else max(max_step, step)
        previous = row
    return max_step


def _optional_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _dry_run_report(
    *,
    output_dir: Path,
    context: dict[str, Any],
    model: str | None,
    model_selection_source: str,
) -> dict[str, Any]:
    lost_gap = _first_lost_gap(context)
    if lost_gap is None:
        camera_improvement = _dry_run_camera_improvement(context)
        if camera_improvement is not None:
            return _report(
                output_dir=output_dir,
                context=context,
                model=model,
                model_selection_source=model_selection_source,
                dry_run=True,
                status="needs_rerun",
                primary_issue="camera_motion",
                improvements=[camera_improvement],
                warnings=context["warnings"],
            )
        return _report(
            output_dir=output_dir,
            context=context,
            model=model,
            model_selection_source=model_selection_source,
            dry_run=True,
            status="ok",
            warnings=context["warnings"],
        )

    start_frame = _safe_int(lost_gap.get("start_frame"))
    end_frame = _safe_int(lost_gap.get("end_frame"))
    if end_frame < start_frame:
        start_frame, end_frame = end_frame, start_frame
    midpoint = start_frame + max(0, end_frame - start_frame) // 2
    improvement = {
        "id": "imp_001",
        "priority": "P1" if str(lost_gap.get("severity") or "") != "fail" else "P0",
        "area": "tracking",
        "failure_tags": ["ball_lost"],
        "root_cause_module": "reacquisition",
        "start_frame": start_frame,
        "end_frame": end_frame,
        "diagnosis": "dry-run: deterministic lost-gap review suggests a targeted rerun window.",
        "recommended_action": "targeted_rerun",
        "config_patch": {},
        "rerun_scope": {
            "start_frame": max(0, start_frame - 30),
            "end_frame": end_frame + 30,
        },
        "likely_ball_region": {
            "frame": midpoint,
            "description": "not visible",
            "confidence": 0.0,
        },
        "evidence": [str(lost_gap.get("reason") or "lost gap artifact")],
        "confidence": 0.4,
    }
    packet_id = _packet_id_for_window(context, start_frame=start_frame, end_frame=end_frame)
    if packet_id is not None:
        improvement["source_packet_id"] = packet_id
        improvement["evidence"].append({"source_packet_id": packet_id})
    improvements, visual_warnings = _merge_visual_review_localization([improvement], context)
    return _report(
        output_dir=output_dir,
        context=context,
        model=model,
        model_selection_source=model_selection_source,
        dry_run=True,
        status="needs_rerun",
        primary_issue="tracking",
        improvements=improvements,
        warnings=[*context["warnings"], *visual_warnings],
    )


def _dry_run_camera_improvement(context: dict[str, Any]) -> dict[str, Any] | None:
    event = _first_camera_motion_event(context)
    if event is None:
        return None
    event_range = _event_frame_range(event)
    if event_range is None:
        return None
    track_context = event.get("nearby_ball_track") if isinstance(event.get("nearby_ball_track"), dict) else {}
    classification = str(track_context.get("classification") or "")
    if classification == "acceptable_fast_play":
        return None

    event_id = str(event.get("id") or "cam_event_001")
    severity = str(event.get("severity") or "warn")
    evidence_payload = {
        "camera_motion_event_id": event_id,
        "camera_motion_event_type": event.get("type"),
        "camera_motion_severity": severity,
        "nearby_ball_track": track_context,
    }
    evidence = [
        str(event.get("reason") or "camera motion audit event"),
        {
            "camera_motion_event_id": event_id,
            "nearby_ball_track": track_context,
        },
    ]
    priority = "P0" if severity == "fail" else "P1"

    if classification in {"no_track_context", "track_jump_review", "ambiguous_status"}:
        diagnosis = (
            "dry-run: camera motion event has no readable nearby ball-track context; human review should inspect the camera spike before rerender."
            if classification == "no_track_context"
            else (
                "dry-run: camera motion event has ambiguous nearby ball-track status; human review should inspect the camera spike before rerender."
                if classification == "ambiguous_status"
                else "dry-run: camera motion event has stable Detected status but an extreme nearby ball-track jump; human review should confirm whether this is a tracking jump before accepting it as fast play."
            )
        )
        return {
            "id": "imp_camera_001",
            "priority": priority,
            "area": "camera_motion",
            "failure_tags": ["camera_catchup_spike"],
            "root_cause_module": "follow_cam",
            "start_frame": event_range[0],
            "end_frame": event_range[1],
            "diagnosis": diagnosis,
            "recommended_action": "human_review_camera_motion",
            "config_patch": {},
            "camera_motion_event_id": event_id,
            "camera_motion_severity": severity,
            "evidence_payload": evidence_payload,
            "evidence": evidence,
            "confidence": 0.35 if classification in {"no_track_context", "ambiguous_status"} else 0.45,
        }

    if track_context.get("has_tracking_issue") is True or classification == "tracking_issue":
        return {
            "id": "imp_camera_001",
            "priority": priority,
            "area": "camera_motion",
            "failure_tags": ["camera_catchup_spike", "ball_lost"],
            "root_cause_module": "follow_cam",
            "start_frame": event_range[0],
            "end_frame": event_range[1],
            "diagnosis": "dry-run: camera motion event overlaps nearby Lost/Predicted ball-track status; rerun tracking before rerendering follow-cam.",
            "recommended_action": "tracking_rerun_before_follow_cam",
            "config_patch": {},
            "rerun_scope": {
                "start_frame": max(0, event_range[0] - CAMERA_TRACK_CONTEXT_RADIUS_FRAMES),
                "end_frame": event_range[1] + CAMERA_TRACK_CONTEXT_RADIUS_FRAMES,
            },
            "camera_motion_event_id": event_id,
            "camera_motion_severity": severity,
            "evidence_payload": evidence_payload,
            "evidence": evidence,
            "confidence": 0.55,
        }

    if track_context.get("stable_detected") is True or classification == "stable_detected":
        return {
            "id": "imp_camera_001",
            "priority": priority,
            "area": "camera_motion",
            "failure_tags": ["camera_catchup_spike"],
            "root_cause_module": "follow_cam",
            "start_frame": event_range[0],
            "end_frame": event_range[1],
            "diagnosis": "dry-run: ball track is stable Detected near the camera motion event, so follow-cam smoothing should be reviewed before rerender.",
            "recommended_action": "adjust_follow_cam",
            "config_patch": {"follow_cam": {"glide_pan_smoothing": 0.18}},
            "follow_cam_rerender_plan": {
                "requires_tracking_rerun": False,
                "reason": "Stable detected tracking near camera spike.",
            },
            "camera_motion_event_id": event_id,
            "camera_motion_severity": severity,
            "evidence_payload": evidence_payload,
            "evidence": evidence,
            "confidence": 0.5,
        }

    return None


def _first_camera_motion_event(context: dict[str, Any]) -> dict[str, Any] | None:
    artifacts = context.get("artifacts") if isinstance(context.get("artifacts"), dict) else {}
    camera_audit = artifacts.get("camera_motion_audit") if isinstance(artifacts.get("camera_motion_audit"), dict) else {}
    for event_key in ("review_events", "events", "motion_events"):
        events = camera_audit.get(event_key)
        if not isinstance(events, list):
            continue
        for event in events:
            if isinstance(event, dict) and _event_frame_range(event) is not None:
                return event
    return None


def _validate_model_report(
    response: Any,
    *,
    context: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], str, str | None]:
    if not isinstance(response, dict):
        raise ValueError("Model response must be a JSON object.")

    summary = response.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("Model response summary must be a JSON object.")
    status = summary.get("status")
    if not isinstance(status, str) or status not in _VALID_STATUSES:
        raise ValueError("Model response summary.status must be one of: ok, needs_rerun, unavailable, error.")

    raw_improvements = response.get("improvements")
    if raw_improvements is None:
        raw_improvements = []
    if not isinstance(raw_improvements, list):
        raise ValueError("Model response improvements must be a list.")

    warnings: list[str] = []
    improvements = []
    highlight_candidates = _highlight_candidate_lookup_from_context(context)
    for index, raw in enumerate(raw_improvements, start=1):
        improvement, patch_warnings = _validate_improvement(raw, index, context=context)
        if improvement.get("recommended_action") in {"adjust_highlight_window", "render_suggested_highlight"}:
            _validate_highlight_window_invariants(
                improvement["suggested_window"],
                candidate_id=str(improvement["candidate_id"]),
                candidates=highlight_candidates,
                label=f"Improvement {index} suggested_window",
            )
        warnings.extend(patch_warnings)
        improvements.append(improvement)

    raw_highlight_adjustments = response.get("highlight_adjustments")
    if raw_highlight_adjustments is None:
        raw_highlight_adjustments = []
    if not isinstance(raw_highlight_adjustments, list):
        raise ValueError("Model response highlight_adjustments must be a list.")
    highlight_adjustments = []
    for index, raw in enumerate(raw_highlight_adjustments, start=1):
        adjustment = _validate_highlight_adjustment(raw, index)
        _validate_highlight_window_invariants(
            adjustment["suggested_window"],
            candidate_id=adjustment["candidate_id"],
            candidates=highlight_candidates,
            label=f"Highlight adjustment {index} suggested_window",
        )
        highlight_adjustments.append(adjustment)

    if status == "ok" and (improvements or highlight_adjustments):
        status = "needs_rerun"
        warnings.append("summary.status normalized from ok to needs_rerun because actions were returned.")

    primary_issue = summary.get("primary_issue")
    return improvements, highlight_adjustments, warnings, status, primary_issue if isinstance(primary_issue, str) else None


def _validate_improvement(raw: Any, index: int, *, context: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(raw, dict):
        raise ValueError(f"Improvement {index} must be a JSON object.")
    missing = [field for field in _REQUIRED_IMPROVEMENT_FIELDS if field not in raw]
    if missing:
        raise ValueError(f"Improvement {index} missing required fields: {', '.join(missing)}")

    failure_tags = raw.get("failure_tags")
    if not isinstance(failure_tags, list):
        raise ValueError(f"Improvement {index} failure_tags must be a list.")
    normalized_tags = [str(item) for item in failure_tags if str(item).strip()]
    if not normalized_tags:
        raise ValueError(f"Improvement {index} failure_tags must contain at least one tag.")
    invalid_tags = [tag for tag in normalized_tags if tag not in AI_FAILURE_TAGS]
    if invalid_tags:
        raise ValueError(f"Improvement {index} failure_tags contain unsupported values: {', '.join(invalid_tags)}")
    confidence = _confidence(raw.get("confidence"), f"Improvement {index} confidence")
    recommended_action = _required_string(raw, "recommended_action", index)
    if recommended_action not in AI_RECOMMENDED_ACTIONS:
        raise ValueError(f"Improvement {index} recommended_action is unsupported: {recommended_action}")
    root_cause_module = _required_string(raw, "root_cause_module", index)
    if root_cause_module not in AI_ROOT_CAUSE_MODULES:
        raise ValueError(f"Improvement {index} root_cause_module is unsupported: {root_cause_module}")

    rerun_scope = raw.get("rerun_scope")
    if recommended_action == "targeted_rerun":
        if not isinstance(rerun_scope, dict):
            raise ValueError(f"Improvement {index} targeted_rerun requires rerun_scope.")
        rerun_scope = _frame_window(rerun_scope, f"Improvement {index} rerun_scope")
    elif isinstance(rerun_scope, dict):
        rerun_scope = _frame_window(rerun_scope, f"Improvement {index} rerun_scope")
    else:
        rerun_scope = None

    likely_ball_region = _likely_ball_region(raw.get("likely_ball_region"), index)
    local_search_roi = _local_search_roi(raw.get("local_search_roi"), index)
    if _is_missing_ball_improvement(normalized_tags, raw) and likely_ball_region is None and local_search_roi is None:
        raise ValueError(f"Improvement {index} missing-ball suggestions require likely_ball_region or local_search_roi.")

    config_patch_raw = raw.get("config_patch") if isinstance(raw.get("config_patch"), dict) else {}
    config_patch, patch_warnings = _filter_config_patch(config_patch_raw)

    item: dict[str, Any] = {
        "id": str(raw.get("id") or f"imp_{index:03d}"),
        "priority": _required_string(raw, "priority", index),
        "area": _required_string(raw, "area", index),
        "failure_tags": normalized_tags,
        "root_cause_module": root_cause_module,
        "diagnosis": str(raw.get("diagnosis") or ""),
        "recommended_action": recommended_action,
        "config_patch": config_patch,
        "evidence": _evidence(raw.get("evidence")),
        "confidence": confidence,
    }
    for key in ("source_packet_id", "visual_review_id", "candidate_id", "camera_motion_event_id", "camera_motion_severity"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            item[key] = value.strip()
    if isinstance(raw.get("frame_dimensions"), dict):
        item["frame_dimensions"] = dict(raw["frame_dimensions"])
    if isinstance(raw.get("evidence_payload"), dict):
        item["evidence_payload"] = dict(raw["evidence_payload"])
    if isinstance(raw.get("false_positive_class"), str) and raw["false_positive_class"].strip():
        false_positive_class = raw["false_positive_class"].strip()
        if false_positive_class not in _KNOWN_FALSE_POSITIVE_CLASSES:
            patch_warnings.append(
                f"Improvement {index} false_positive_class normalized to unknown: {false_positive_class}"
            )
            false_positive_class = "unknown"
        item["false_positive_class"] = false_positive_class
    for key in ("start_frame", "end_frame"):
        value = _optional_int(raw.get(key))
        if value is not None:
            if value < 0:
                raise ValueError(f"Improvement {index} {key} must be non-negative.")
            item[key] = value
    if rerun_scope is not None:
        item["rerun_scope"] = rerun_scope
    if likely_ball_region is not None:
        item["likely_ball_region"] = likely_ball_region
    if local_search_roi is not None:
        item["local_search_roi"] = local_search_roi
    if isinstance(raw.get("suggested_window"), dict):
        item["suggested_window"] = _frame_window(raw["suggested_window"], f"Improvement {index} suggested_window")
    if isinstance(raw.get("clip_action"), str):
        clip_action = raw["clip_action"].strip()
        if clip_action not in AI_CLIP_ACTIONS:
            raise ValueError(f"Improvement {index} clip_action is unsupported: {clip_action}")
        item["clip_action"] = clip_action
    if isinstance(raw.get("follow_cam_rerender_plan"), dict):
        item["follow_cam_rerender_plan"] = dict(raw["follow_cam_rerender_plan"])
    _validate_action_specific_improvement(item, index, context=context)
    return item, patch_warnings


def _validate_action_specific_improvement(
    item: dict[str, Any],
    index: int,
    *,
    context: dict[str, Any] | None = None,
) -> None:
    action = item.get("recommended_action")
    if action == "localize_ball_roi":
        if "local_search_roi" not in item:
            raise ValueError(f"Improvement {index} localize_ball_roi requires local_search_roi.")
        if not _has_traceable_packet_or_visual_provenance(item, context):
            raise ValueError(
                f"Improvement {index} localize_ball_roi requires source_packet_id or visual_review_id provenance "
                "that matches review_packets.json or ai_visual_review.json."
            )
    if action == "targeted_rerun" and "local_search_roi" in item:
        if not _has_traceable_packet_or_visual_provenance(item, context):
            raise ValueError(
                f"Improvement {index} local_search_roi requires traceable packet or visual review provenance."
            )
    if action in {"noise_filter_adjustment", "tighten_noise_filter", "reject_noise"}:
        if not item.get("false_positive_class"):
            raise ValueError(f"Improvement {index} {action} requires false_positive_class.")
        if "start_frame" not in item or "end_frame" not in item:
            raise ValueError(f"Improvement {index} {action} requires start_frame and end_frame.")
        if int(item["end_frame"]) < int(item["start_frame"]):
            raise ValueError(f"Improvement {index} end_frame must be greater than or equal to start_frame.")
    if action == "noise_filter_adjustment" and not item.get("config_patch"):
        raise ValueError(f"Improvement {index} noise_filter_adjustment requires a safe config_patch.")
    if action in {"adjust_highlight_window", "render_suggested_highlight"}:
        if not isinstance(item.get("candidate_id"), str) or not str(item.get("candidate_id")).strip():
            raise ValueError(f"Improvement {index} {action} requires candidate_id.")
        if "suggested_window" not in item:
            raise ValueError(f"Improvement {index} {action} requires suggested_window.")
        if item.get("clip_action") not in AI_CLIP_ACTIONS:
            raise ValueError(f"Improvement {index} {action} requires supported clip_action.")
    if action == "adjust_follow_cam" and not item.get("config_patch") and not item.get("follow_cam_rerender_plan"):
        raise ValueError(f"Improvement {index} adjust_follow_cam requires config_patch or follow_cam_rerender_plan.")
    if action == "tracking_rerun_before_follow_cam" and "rerun_scope" not in item:
        raise ValueError(f"Improvement {index} tracking_rerun_before_follow_cam requires rerun_scope.")
    if action == "human_review_camera_motion":
        if not isinstance(item.get("camera_motion_event_id"), str) or not str(item.get("camera_motion_event_id")).strip():
            raise ValueError(f"Improvement {index} human_review_camera_motion requires camera_motion_event_id.")
        if "start_frame" not in item or "end_frame" not in item:
            raise ValueError(f"Improvement {index} human_review_camera_motion requires start_frame and end_frame.")
        if not item.get("evidence"):
            raise ValueError(f"Improvement {index} human_review_camera_motion requires evidence.")


def _has_traceable_packet_or_visual_provenance(item: dict[str, Any], context: dict[str, Any] | None) -> bool:
    packet_ids, visual_review_ids = _traceable_packet_and_visual_ids(context)
    for packet_id in _packet_provenance_values(item):
        if packet_id in packet_ids:
            return True
    for visual_review_id in _visual_review_provenance_values(item):
        if visual_review_id in visual_review_ids:
            return True
    return False


def _packet_provenance_values(item: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in ("source_packet_id", "packet_id"):
        if isinstance(item.get(key), str) and item[key].strip():
            values.add(item[key].strip())
    evidence_payload = item.get("evidence_payload")
    if isinstance(evidence_payload, dict):
        for key in ("source_packet_id", "packet_id"):
            if isinstance(evidence_payload.get(key), str) and evidence_payload[key].strip():
                values.add(evidence_payload[key].strip())
        provenance = evidence_payload.get("local_search_roi_provenance")
        if isinstance(provenance, dict):
            for key in ("source_packet_id", "packet_id"):
                if isinstance(provenance.get(key), str) and provenance[key].strip():
                    values.add(provenance[key].strip())
    evidence = item.get("evidence")
    if isinstance(evidence, list):
        for evidence_item in evidence:
            if not isinstance(evidence_item, dict):
                continue
            for key in ("source_packet_id", "packet_id"):
                if isinstance(evidence_item.get(key), str) and evidence_item[key].strip():
                    values.add(evidence_item[key].strip())
    return values


def _visual_review_provenance_values(item: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    if isinstance(item.get("visual_review_id"), str) and item["visual_review_id"].strip():
        values.add(item["visual_review_id"].strip())
    evidence_payload = item.get("evidence_payload")
    if isinstance(evidence_payload, dict):
        if isinstance(evidence_payload.get("visual_review_id"), str) and evidence_payload["visual_review_id"].strip():
            values.add(evidence_payload["visual_review_id"].strip())
        provenance = evidence_payload.get("local_search_roi_provenance")
        if isinstance(provenance, dict):
            if isinstance(provenance.get("visual_review_id"), str) and provenance["visual_review_id"].strip():
                values.add(provenance["visual_review_id"].strip())
    evidence = item.get("evidence")
    if isinstance(evidence, list):
        for evidence_item in evidence:
            if not isinstance(evidence_item, dict):
                continue
            for key in ("visual_review_id",):
                if isinstance(evidence_item.get(key), str) and evidence_item[key].strip():
                    values.add(evidence_item[key].strip())
    return values


def _traceable_packet_and_visual_ids(context: dict[str, Any] | None) -> tuple[set[str], set[str]]:
    if not isinstance(context, dict):
        return set(), set()
    provenance = context.get("traceable_provenance")
    if isinstance(provenance, dict):
        packet_values = provenance.get("packet_ids")
        visual_values = provenance.get("visual_review_ids")
        packet_ids = {value.strip() for value in packet_values if isinstance(value, str) and value.strip()} if isinstance(packet_values, list) else set()
        visual_review_ids = {value.strip() for value in visual_values if isinstance(value, str) and value.strip()} if isinstance(visual_values, list) else set()
        if packet_ids or visual_review_ids:
            return packet_ids, visual_review_ids
    artifacts = context.get("artifacts") if isinstance(context.get("artifacts"), dict) else {}
    packet_ids: set[str] = set()
    visual_review_ids: set[str] = set()

    packet_report = artifacts.get("review_packets") if isinstance(artifacts.get("review_packets"), dict) else {}
    packets = packet_report.get("packets") if isinstance(packet_report.get("packets"), list) else []
    for packet in packets:
        if not isinstance(packet, dict):
            continue
        for key in ("packet_id", "id", "source_packet_id"):
            value = packet.get(key)
            if isinstance(value, str) and value.strip():
                packet_ids.add(value.strip())

    visual_report = artifacts.get("ai_visual_review") if isinstance(artifacts.get("ai_visual_review"), dict) else {}
    reviews = visual_report.get("reviews") if isinstance(visual_report.get("reviews"), list) else []
    for item in reviews:
        if not isinstance(item, dict):
            continue
        review = item.get("review") if isinstance(item.get("review"), dict) else item
        for source in (item, review):
            for key in ("source_packet_id", "packet_id"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    packet_ids.add(value.strip())
            value = source.get("visual_review_id")
            if isinstance(value, str) and value.strip():
                visual_review_ids.add(value.strip())

    return packet_ids, visual_review_ids


def _traceable_provenance_payload(context: dict[str, Any]) -> dict[str, list[str]]:
    packet_ids, visual_review_ids = _traceable_packet_and_visual_ids(context)
    return {
        "packet_ids": sorted(packet_ids),
        "visual_review_ids": sorted(visual_review_ids),
    }


def _validate_highlight_adjustment(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"Highlight adjustment {index} must be a JSON object.")
    candidate_id = raw.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ValueError(f"Highlight adjustment {index} requires candidate_id.")
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"Highlight adjustment {index} requires reason.")
    return {
        "candidate_id": candidate_id.strip(),
        "current_window": _frame_window(raw.get("current_window"), f"Highlight adjustment {index} current_window"),
        "suggested_window": _frame_window(raw.get("suggested_window"), f"Highlight adjustment {index} suggested_window"),
        "reason": reason.strip(),
        "confidence": _confidence(raw.get("confidence"), f"Highlight adjustment {index} confidence"),
        **_optional_clip_action(raw.get("clip_action"), index),
    }


def _highlight_candidate_lookup_from_context(context: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(context, dict):
        return {}
    artifacts = context.get("artifacts")
    event_candidates = artifacts.get("event_candidates") if isinstance(artifacts, dict) else None
    return _event_candidate_lookup(event_candidates)


def _event_candidate_lookup(event_candidates: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(event_candidates, dict):
        return {}
    candidates = event_candidates.get("candidates")
    if not isinstance(candidates, list):
        return {}
    lookup: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_id = candidate.get("id")
        if isinstance(candidate_id, str) and candidate_id.strip():
            lookup[candidate_id.strip()] = candidate
    return lookup


def _validate_highlight_window_invariants(
    window: dict[str, int],
    *,
    candidate_id: str,
    candidates: dict[str, dict[str, Any]] | None,
    label: str,
) -> None:
    if not candidates:
        raise ValueError(f"{label} cannot be validated because event_candidates.json has no matching candidates.")
    candidate = candidates.get(candidate_id)
    if candidate is None:
        raise ValueError(f"{label} references unknown candidate_id: {candidate_id}.")
    core_window = _candidate_core_window(candidate)
    if core_window is None:
        raise ValueError(f"{label} requires candidate {candidate_id} to include core_window.")
    if window["start_frame"] > core_window["start_frame"] or window["end_frame"] < core_window["end_frame"]:
        raise ValueError(f"{label} must include candidate core_window {core_window}.")
    min_tail = _candidate_min_post_event_frames(candidate)
    if min_tail > 0:
        required_end = core_window["end_frame"] + min_tail
        if window["end_frame"] < required_end:
            raise ValueError(
                f"{label} must preserve the minimum post-event tail for {candidate_id}: "
                f"end_frame {window['end_frame']} < {required_end}."
            )


def _candidate_core_window(candidate: dict[str, Any]) -> dict[str, int] | None:
    core_window = _optional_frame_window(candidate.get("core_window"))
    if core_window is not None:
        return core_window
    start_frame = _optional_int(candidate.get("start_frame"))
    end_frame = _optional_int(candidate.get("end_frame"))
    if start_frame is None or end_frame is None or start_frame < 0 or end_frame < start_frame:
        return None
    return {"start_frame": start_frame, "end_frame": end_frame}


def _optional_frame_window(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    start_frame = _optional_int(value.get("start_frame"))
    end_frame = _optional_int(value.get("end_frame"))
    if start_frame is None or end_frame is None or start_frame < 0 or end_frame < start_frame:
        return None
    return {"start_frame": start_frame, "end_frame": end_frame}


def _candidate_min_post_event_frames(candidate: dict[str, Any]) -> int:
    buffer_policy = candidate.get("buffer_policy")
    if not isinstance(buffer_policy, dict):
        return 0
    value = _optional_int(buffer_policy.get("min_tail_frames"))
    if value is None:
        value = _optional_int(buffer_policy.get("min_post_event_frames"))
    if value is None:
        value = _optional_int(buffer_policy.get("post_buffer_frames"))
    return max(0, value or 0)


def _optional_clip_action(value: Any, index: int) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Highlight adjustment {index} clip_action must be a string.")
    action = value.strip()
    if action not in AI_CLIP_ACTIONS:
        raise ValueError(f"Highlight adjustment {index} clip_action is unsupported: {action}")
    return {"clip_action": action}


def _report(
    *,
    output_dir: Path,
    context: dict[str, Any],
    model: str | None,
    dry_run: bool,
    status: str,
    model_selection_source: str,
    primary_issue: str | None = None,
    improvements: list[dict[str, Any]] | None = None,
    highlight_adjustments: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    improvements = improvements or []
    highlight_adjustments = highlight_adjustments or []
    if status not in _VALID_STATUSES:
        status = "error"
    summary = _summary(
        status=status,
        primary_issue=primary_issue,
        improvements=improvements,
        highlight_adjustments=highlight_adjustments,
    )
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "output_dir": str(output_dir.resolve()),
        "model": model,
        "model_selection": {
            "model": model,
            "source": model_selection_source,
        },
        "dry_run": bool(dry_run),
        "source_artifacts": context.get("source_artifacts") or {},
        "artifact_status": context.get("artifact_status") or {},
        "summary": summary,
        "improvements": improvements,
        "highlight_adjustments": highlight_adjustments,
        "warnings": warnings or [],
    }
    if error is not None:
        report["error"] = error
    return report


def _summary(
    *,
    status: str,
    primary_issue: str | None,
    improvements: list[dict[str, Any]],
    highlight_adjustments: list[dict[str, Any]],
) -> dict[str, Any]:
    summary = {
        "status": status,
        "primary_issue": primary_issue,
        "improvement_count": len(improvements),
        "targeted_rerun_count": sum(1 for item in improvements if item.get("recommended_action") == "targeted_rerun"),
        "config_patch_count": sum(1 for item in improvements if bool(item.get("config_patch"))),
        "highlight_adjustment_count": len(highlight_adjustments),
    }
    camera_counts = _camera_summary_counts(improvements)
    if camera_counts:
        summary.update(camera_counts)
    return summary


def _camera_summary_counts(improvements: list[dict[str, Any]]) -> dict[str, Any]:
    camera_items = [
        item
        for item in improvements
        if item.get("area") == "camera_motion"
        or item.get("recommended_action") in {"adjust_follow_cam", "tracking_rerun_before_follow_cam", "human_review_camera_motion"}
    ]
    if not camera_items:
        return {}
    severity_counts: dict[str, int] = {}
    action_counts: dict[str, int] = {}
    for item in camera_items:
        action = item.get("recommended_action")
        if isinstance(action, str) and action:
            action_counts[action] = action_counts.get(action, 0) + 1
        severity = item.get("camera_motion_severity")
        if not isinstance(severity, str):
            evidence_payload = item.get("evidence_payload") if isinstance(item.get("evidence_payload"), dict) else {}
            severity = evidence_payload.get("camera_motion_severity") if isinstance(evidence_payload.get("camera_motion_severity"), str) else None
        if isinstance(severity, str) and severity:
            severity_counts[severity] = severity_counts.get(severity, 0) + 1
    return {
        "camera_improvement_count": len(camera_items),
        "camera_severity_counts": severity_counts,
        "camera_action_counts": action_counts,
    }


def _instructions(language: str | None) -> str:
    language_instruction = (
        "Write human-readable fields in Simplified Chinese."
        if language == "zh"
        else "Write human-readable fields in English."
    )
    return (
        "You are diagnosing football tracking run artifacts and producing an advisory improvement report. "
        "Return strict JSON only with keys: summary, improvements, highlight_adjustments. "
        "summary.status must be one of ok, needs_rerun, unavailable, error. "
        "Each improvement must include priority, area, failure_tags, root_cause_module, recommended_action, confidence. "
        "targeted_rerun improvements must include rerun_scope. "
        "Missing-ball suggestions must use targeted_rerun, localize_ball_roi, or likely_ball_region.description='not visible'; "
        "localize_ball_roi and any local_search_roi require a traceable source_packet_id or visual_review_id from the supplied packet evidence. "
        "Noise suggestions must include false_positive_class, bounded start_frame/end_frame, and use noise_filter_adjustment "
        "with a safe config_patch or reject_noise for confirmed false positives. "
        "Camera-motion suggestions must use adjust_follow_cam for stable Detected tracking with sudden camera movement, "
        "tracking_rerun_before_follow_cam when the camera event overlaps Lost/Predicted or nearby tracking issues, "
        "or human_review_camera_motion when evidence is ambiguous; continuous stable high-speed play is evidence, not an automatic failure. "
        "Highlight suggestions must include candidate.core_window, preserve candidate.buffer_policy.min_tail_frames, "
        "and do not trim result tail after a shot or goal. "
        "config_patch is advisory only and may only suggest known fields under follow_cam, postprocess, "
        "scene_bias.dynamic_air_recovery, selection, or tracking. "
        "Do not include image base64 or claim files that are not present in the supplied context. "
        f"{language_instruction}"
    )


def _provider_safe_context(context: dict[str, Any]) -> dict[str, Any]:
    safe = _redact_provider_paths(context)
    if isinstance(safe, dict):
        safe.pop("warnings", None)
        safe.pop("traceable_provenance", None)
    return safe if isinstance(safe, dict) else {}


def _prompt(*, context: dict[str, Any], objective: str | None, language: str | None) -> str:
    payload = {
        "objective": objective,
        "response_language": "zh" if language == "zh" else "en",
        "context": _provider_safe_context(context),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _redact_provider_paths(value: Any, key_hint: str = "") -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_provider_paths(child, str(key)) for key, child in value.items()}
    if isinstance(value, list):
        return [_redact_provider_paths(item, key_hint) for item in value]
    if isinstance(value, str):
        lowered = key_hint.casefold()
        if lowered in _PROVIDER_PATH_KEYS or lowered.endswith("_path"):
            return "<redacted-path>"
        if value.startswith("data:"):
            return "<redacted-data-url>"
        return _redact_path_substrings(value)
    return value


def _redact_path_substrings(value: str) -> str:
    value = re.sub(r"[A-Za-z]:[\\/][^\s\"']+", "<redacted-path>", value)
    return re.sub(
        r"(?<!\w)/(?:Users|home|tmp|Project|workspace|workspaces|mnt|var|private)/[^\s\"']+",
        "<redacted-path>",
        value,
    )


def _read_optional_json(path: Path) -> tuple[dict[str, Any] | None, str, str | None]:
    if not path.exists():
        return None, "missing", f"{path.name} missing"
    try:
        with path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        return None, "corrupt", f"{path.name} corrupt: {_safe_error_message(exc)}"
    if not isinstance(loaded, dict):
        return None, "invalid", f"{path.name} invalid: expected JSON object"
    return loaded, "loaded", None


def _limit_artifact_payload(artifact_key: str, payload: dict[str, Any], *, max_items: int) -> dict[str, Any]:
    list_keys_by_artifact = {
        "ball_audit": {"review_events", "tracklets", "sources"},
        "ai_review_triggers": {"triggers"},
        "review_packets": {"packets"},
        "ai_visual_review": {"reviews", "errors"},
        "camera_motion_audit": {"events", "review_events", "motion_events"},
        "event_candidates": {"candidates"},
    }
    list_keys = list_keys_by_artifact.get(artifact_key, set())
    limited: dict[str, Any] = {}
    for key, value in payload.items():
        if key in list_keys and isinstance(value, list):
            limited[key] = value[:max_items]
        elif isinstance(value, list):
            limited[key] = value[:max_items]
        else:
            limited[key] = value
    return limited


def _strip_data_urls(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _strip_data_urls(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_strip_data_urls(item) for item in value]
    if isinstance(value, str) and value.startswith("data:"):
        return "<redacted-data-url>"
    return value


def _first_lost_gap(context: dict[str, Any]) -> dict[str, Any] | None:
    artifacts = context.get("artifacts") if isinstance(context.get("artifacts"), dict) else {}
    ball_audit = artifacts.get("ball_audit") if isinstance(artifacts.get("ball_audit"), dict) else {}
    events = ball_audit.get("review_events") if isinstance(ball_audit.get("review_events"), list) else []
    for event in events:
        if isinstance(event, dict) and str(event.get("type") or "").casefold() == "lost_gap":
            return event

    triggers_report = artifacts.get("ai_review_triggers") if isinstance(artifacts.get("ai_review_triggers"), dict) else {}
    triggers = triggers_report.get("triggers") if isinstance(triggers_report.get("triggers"), list) else []
    for trigger in triggers:
        if isinstance(trigger, dict) and str(trigger.get("type") or "").casefold() == "lost_gap":
            return trigger
    return None


def _is_missing_ball_improvement(failure_tags: list[str], raw: dict[str, Any]) -> bool:
    if raw.get("recommended_action") == "tracking_rerun_before_follow_cam":
        return False
    lowered_tags = {tag.casefold() for tag in failure_tags}
    if lowered_tags & _MISSING_BALL_TAGS:
        return True
    text = " ".join(
        str(raw.get(key) or "")
        for key in ("area", "root_cause_module", "diagnosis", "recommended_action")
    ).casefold()
    return "missing ball" in text or "lost ball" in text or "lost_gap" in text


def _likely_ball_region(value: Any, index: int) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"Improvement {index} likely_ball_region must be an object.")
    description = value.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"Improvement {index} likely_ball_region requires description.")
    region = {"description": description.strip()}
    frame = _optional_int(value.get("frame"))
    if frame is not None:
        if frame < 0:
            raise ValueError(f"Improvement {index} likely_ball_region frame must be non-negative.")
        region["frame"] = frame
    if "confidence" in value:
        region["confidence"] = _confidence(value.get("confidence"), f"Improvement {index} likely_ball_region confidence")
    return region


def _local_search_roi(value: Any, index: int) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"Improvement {index} local_search_roi must be an object.")
    required = ("coordinate_space", "frame", "x", "y", "width", "height", "confidence")
    missing = [key for key in required if key not in value]
    if missing:
        raise ValueError(f"Improvement {index} local_search_roi missing fields: {', '.join(missing)}")
    coordinate_space = value.get("coordinate_space")
    if coordinate_space != "image":
        raise ValueError(f"Improvement {index} local_search_roi coordinate_space must be image.")
    width = _positive_number(value.get("width"), f"Improvement {index} local_search_roi width")
    height = _positive_number(value.get("height"), f"Improvement {index} local_search_roi height")
    return {
        "coordinate_space": coordinate_space,
        "frame": _required_nonnegative_int(value.get("frame"), f"Improvement {index} local_search_roi frame"),
        "x": _nonnegative_number(value.get("x"), f"Improvement {index} local_search_roi x"),
        "y": _nonnegative_number(value.get("y"), f"Improvement {index} local_search_roi y"),
        "width": width,
        "height": height,
        "confidence": _confidence(value.get("confidence"), f"Improvement {index} local_search_roi confidence"),
    }


def _frame_window(value: Any, label: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object.")
    start = _required_int(value.get("start_frame"), f"{label}.start_frame")
    end = _required_int(value.get("end_frame"), f"{label}.end_frame")
    if start < 0 or end < 0:
        raise ValueError(f"{label} frames must be non-negative.")
    if end < start:
        raise ValueError(f"{label}.end_frame must be greater than or equal to start_frame.")
    return {"start_frame": start, "end_frame": end}


def _required_string(raw: dict[str, Any], key: str, index: int) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Improvement {index} {key} must be a non-empty string.")
    return value.strip()


def _evidence(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, (str, dict))]


def _confidence(value: Any, label: str) -> float:
    parsed = _number(value, label)
    if parsed < 0.0 or parsed > 1.0:
        raise ValueError(f"{label} must be between 0 and 1.")
    return round(parsed, 4)


def _positive_number(value: Any, label: str) -> float:
    parsed = _number(value, label)
    if parsed <= 0:
        raise ValueError(f"{label} must be positive.")
    return parsed


def _nonnegative_number(value: Any, label: str) -> float:
    parsed = _number(value, label)
    if parsed < 0:
        raise ValueError(f"{label} must be non-negative.")
    return parsed


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number.")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite.")
    return parsed


def _required_int(value: Any, label: str) -> int:
    parsed = _optional_int(value)
    if parsed is None:
        raise ValueError(f"{label} must be an integer.")
    return parsed


def _required_nonnegative_int(value: Any, label: str) -> int:
    parsed = _required_int(value, label)
    if parsed < 0:
        raise ValueError(f"{label} must be non-negative.")
    return parsed


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not re.fullmatch(r"[+-]?\d+", stripped):
            return None
        return int(stripped)
    return None


def _safe_int(value: Any) -> int:
    parsed = _optional_int(value)
    return 0 if parsed is None else max(0, parsed)


def _filter_config_patch(patch: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    filtered, warnings = _filter_config_patch_node(patch, prefix="")
    return filtered if isinstance(filtered, dict) else {}, warnings


def _filter_config_patch_node(value: Any, *, prefix: str) -> tuple[Any | None, list[str]]:
    warnings: list[str] = []
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{prefix}.{key_text}" if prefix else key_text
            if not key_text or "." in key_text:
                warnings.append(f"config_patch path rejected by allowlist: {child_path}")
                continue
            filtered_child, child_warnings = _filter_config_patch_node(child, prefix=child_path)
            warnings.extend(child_warnings)
            if filtered_child is not None:
                result[key_text] = filtered_child
        return (result if result else None), warnings

    validator = _CONFIG_PATCH_VALIDATORS.get(prefix)
    if validator is None:
        warnings.append(f"config_patch path rejected by allowlist: {prefix}")
        return None, warnings
    if not validator(value):
        warnings.append(f"config_patch path rejected by validator: {prefix}")
        return None, warnings
    return value, warnings


def _number_between(minimum: float, maximum: float) -> Callable[[Any], bool]:
    def validate(value: Any) -> bool:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        parsed = float(value)
        return math.isfinite(parsed) and minimum <= parsed <= maximum

    return validate


def _int_between(minimum: int, maximum: int) -> Callable[[Any], bool]:
    def validate(value: Any) -> bool:
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        return minimum <= value <= maximum

    return validate


def _bool_value(value: Any) -> bool:
    return isinstance(value, bool)


def _enum_value(*values: str) -> Callable[[Any], bool]:
    allowed = set(values)

    def validate(value: Any) -> bool:
        return isinstance(value, str) and value in allowed

    return validate


_CONFIG_PATCH_VALIDATORS: dict[str, Callable[[Any], bool]] = {
    "follow_cam.enabled": _bool_value,
    "follow_cam.pan_smoothing": _number_between(0.0, 1.0),
    "follow_cam.zoom_smoothing": _number_between(0.0, 1.0),
    "follow_cam.glide_pan_smoothing": _number_between(0.0, 1.0),
    "follow_cam.glide_max_pan_per_frame_x": _number_between(0.0, 500.0),
    "follow_cam.glide_max_pan_per_frame_y": _number_between(0.0, 500.0),
    "follow_cam.catch_up_pan_smoothing": _number_between(0.0, 1.0),
    "follow_cam.zoom_out_confirm_frames": _int_between(0, 240),
    "follow_cam.zoom_in_confirm_frames": _int_between(0, 240),
    "follow_cam.zoom_hold_frames_after_change": _int_between(0, 300),
    "follow_cam.catch_up_max_pan_per_frame_x": _number_between(0.0, 500.0),
    "follow_cam.catch_up_max_pan_per_frame_y": _number_between(0.0, 500.0),
    "follow_cam.predicted_pan_decay": _number_between(0.0, 1.0),
    "follow_cam.dead_zone_ratio_x": _number_between(0.0, 0.8),
    "follow_cam.dead_zone_ratio_y": _number_between(0.0, 0.8),
    "follow_cam.max_pan_per_frame_x": _number_between(0.0, 500.0),
    "follow_cam.max_pan_per_frame_y": _number_between(0.0, 500.0),
    "follow_cam.max_zoom_in_per_frame": _number_between(0.0, 240.0),
    "follow_cam.max_zoom_out_per_frame": _number_between(0.0, 240.0),
    "postprocess.enabled": _bool_value,
    "postprocess.max_detected_island_length": _int_between(0, 60),
    "postprocess.low_confidence_threshold": _number_between(0.0, 1.0),
    "scene_bias.dynamic_air_recovery.enabled": _bool_value,
    "scene_bias.dynamic_air_recovery.tentative_reacquire_confidence_threshold": _number_between(0.0, 1.0),
    "scene_bias.dynamic_air_recovery.tentative_reacquire_score_threshold": _number_between(0.0, 1.0),
    "scene_bias.dynamic_air_recovery.reacquire_confidence_threshold": _number_between(0.0, 1.0),
    "scene_bias.dynamic_air_recovery.reacquire_image_size": _int_between(320, 4096),
    "scene_bias.dynamic_air_recovery.edge_reentry_expand_x": _number_between(0.0, 6000.0),
    "scene_bias.dynamic_air_recovery.edge_reentry_expand_y": _number_between(0.0, 3000.0),
    "selection.min_accept_score": _number_between(0.0, 1.0),
    "selection.stable_history_length": _int_between(1, 120),
    "selection.priors.player_foot_radius_px": _number_between(0.0, 1000.0),
    "selection.priors.player_foot_bonus": _number_between(-1.0, 1.0),
    "selection.priors.recent_player_frame_window": _int_between(0, 120),
    "selection.priors.pitch_boundary_penalty": _number_between(-1.0, 1.0),
    "selection.priors.pitch_boundary_margin_m": _number_between(0.0, 20.0),
    "tracking.max_lost_frames": _int_between(0, 1000),
    "tracking.match_distance": _number_between(0.0, 10000.0),
    "tracking.max_speed": _number_between(0.0, 10000.0),
    "tracking.max_acceleration": _number_between(0.0, 10000.0),
    "tracking.prediction_mode": _enum_value("none", "linear", "constant_velocity"),
    "tracking.predicted_confidence_decay": _number_between(0.0, 1.0),
}


def _select_model(client: Any, model: str | None) -> tuple[str | None, str]:
    if model:
        return model, "explicit"
    settings = getattr(client, "settings", None)
    improvement_model = getattr(settings, "improvement_model", None)
    if isinstance(improvement_model, str) and improvement_model:
        return improvement_model, "improvement_model"
    chat_model = getattr(settings, "chat_model", None)
    if isinstance(chat_model, str) and chat_model:
        return chat_model, "chat_model_fallback"
    return None, "unknown"


def _build_default_client() -> Any:
    from football_tracking.api.ai_provider import OpenAIResponsesClient, load_provider_settings

    repo_root = Path(__file__).resolve().parents[2]
    return OpenAIResponsesClient(load_provider_settings(repo_root))


def _client_api_key(client: Any) -> str:
    settings = getattr(client, "settings", None)
    api_key = getattr(settings, "api_key", "")
    return api_key if isinstance(api_key, str) else ""


def _safe_error_message(exc: Exception, api_key: str = "") -> str:
    message = str(exc)
    message = re.sub(r"data:[^\s\"']*;base64,[^\s\"']+", "data:<redacted-base64>", message)
    message = re.sub(r"Bearer\s+[^\s\"']+", "Bearer <redacted>", message, flags=re.IGNORECASE)
    message = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-<redacted>", message)
    if api_key:
        message = message.replace(api_key, "<redacted-api-key>")
    return message


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
