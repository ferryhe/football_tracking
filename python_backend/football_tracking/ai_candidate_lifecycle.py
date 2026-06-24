from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from football_tracking.ai_candidate_comparison import CANDIDATE_STATUSES, comparison_payload_status
from football_tracking.ai_candidate_registry import REGISTRY_REPORT_NAME, load_candidate_registry
from football_tracking.final_artifact_manifest import FINAL_ARTIFACT_MANIFEST_NAME

SCHEMA_VERSION = "1.0"

AI_CANDIDATE_LIFECYCLE_REPORT_NAME = "ai_candidate_lifecycle.json"
AI_IMPROVEMENT_REPORT_NAME = "ai_improvement_report.json"
APPROVED_ACTIONS_REPORT_NAME = "ai_improvement_approved_actions.json"
QUALITY_GATE_REPORT_NAME = "ai_improvement_quality_gate.json"
MISSING_BALL_RESOLUTION_REPORT_NAME = "missing_ball_resolution.json"
_RESERVED_LIFECYCLE_OUTPUT_NAMES = {
    AI_IMPROVEMENT_REPORT_NAME,
    APPROVED_ACTIONS_REPORT_NAME,
    REGISTRY_REPORT_NAME,
    QUALITY_GATE_REPORT_NAME,
    FINAL_ARTIFACT_MANIFEST_NAME,
    MISSING_BALL_RESOLUTION_REPORT_NAME,
}

STAGES = (
    "review_only",
    "proposed",
    "approved",
    "pending_execution",
    "executed",
    "compared",
    "gated",
    "finalized",
)
COMPARISON_STATUSES = ("pass", "warn", "fail", "unavailable", "none")
PROMOTION_STATUSES = ("not_promoted", "pending_confirmation", "promoted", "rejected", "blocked")
RESOLUTION_STATUSES = ("none", "resolved_not_visible", "candidate_output")
BLOCKING_REASONS = (
    "missing_evidence",
    "unsafe_window",
    "unsupported_type",
    "missing_candidate_id",
    "missing_comparison",
    "failed_quality_gate",
    "pending_api_execution",
    "pending_human_confirmation",
)

_STAGE_RANK = {stage: index for index, stage in enumerate(STAGES)}
_COMPARISON_RANK = {"none": 0, "pass": 1, "warn": 2, "unavailable": 3, "fail": 4}
_PROMOTION_RANK = {
    "not_promoted": 0,
    "promoted": 1,
    "pending_confirmation": 2,
    "rejected": 3,
    "blocked": 4,
}
_RESOLUTION_RANK = {"none": 0, "candidate_output": 1, "resolved_not_visible": 2}


def build_ai_candidate_lifecycle(output_dir: Path) -> dict[str, Any]:
    output_dir = Path(output_dir)
    state: dict[str, Any] = {
        "output_dir": str(output_dir),
        "stage": "review_only",
        "approved_action_count": 0,
        "comparison_report_count": 0,
        "generated_at_values": [],
        "blocking_reasons": [],
        "artifacts": {},
        "candidates": {},
    }

    ai_report = _load_artifact(output_dir / AI_IMPROVEMENT_REPORT_NAME)
    approved_actions = _load_artifact(output_dir / APPROVED_ACTIONS_REPORT_NAME)
    registry_artifact = _load_artifact(output_dir / REGISTRY_REPORT_NAME)
    quality_gate = _load_artifact(output_dir / QUALITY_GATE_REPORT_NAME)
    final_manifest = _load_artifact(output_dir / FINAL_ARTIFACT_MANIFEST_NAME)
    missing_ball_resolution = _load_artifact(output_dir / MISSING_BALL_RESOLUTION_REPORT_NAME)
    artifacts = {
        AI_IMPROVEMENT_REPORT_NAME: ai_report,
        APPROVED_ACTIONS_REPORT_NAME: approved_actions,
        REGISTRY_REPORT_NAME: registry_artifact,
        QUALITY_GATE_REPORT_NAME: quality_gate,
        FINAL_ARTIFACT_MANIFEST_NAME: final_manifest,
        MISSING_BALL_RESOLUTION_REPORT_NAME: missing_ball_resolution,
    }
    state["artifacts"] = {name: artifact["status"] for name, artifact in artifacts.items()}
    for artifact in artifacts.values():
        _collect_generated_at(state, artifact.get("payload"))

    comparisons = _discover_comparison_reports(output_dir)
    state["comparison_report_count"] = len(comparisons)

    _apply_ai_report(state, ai_report.get("payload"))
    _apply_approved_actions(state, approved_actions.get("payload"))
    _apply_registry(state, output_dir, registry_artifact, comparisons)
    _apply_orphan_comparisons(state, comparisons)
    _apply_quality_gate(state, quality_gate.get("payload"))
    _apply_missing_ball_resolution(state, missing_ball_resolution.get("payload"))
    _apply_final_manifest(state, final_manifest.get("payload"), output_dir, comparisons)
    _finalize_pending_execution(state)

    candidates = [_public_candidate(candidate) for candidate in state["candidates"].values()]
    candidates.sort(key=lambda item: (item["candidate_id"] is None, item["candidate_id"] or "", item["source_key"]))
    for candidate in candidates:
        candidate.pop("source_key", None)

    summary = _summary(state, candidates)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _latest_generated_at(state["generated_at_values"]),
        "output_dir": str(output_dir),
        "summary": summary,
        "candidates": candidates,
        "artifacts": state["artifacts"],
    }


def write_ai_candidate_lifecycle_report(
    output_dir: Path,
    *,
    report_name: str = AI_CANDIDATE_LIFECYCLE_REPORT_NAME,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    if report_name in _RESERVED_LIFECYCLE_OUTPUT_NAMES:
        raise ValueError(f"report_name must not overwrite lifecycle input artifact: {report_name}")
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_ai_candidate_lifecycle(output_dir)
    (output_dir / report_name).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _apply_ai_report(state: dict[str, Any], payload: Any) -> None:
    if not isinstance(payload, dict):
        return
    _advance_state_stage(state, "review_only")
    improvements = _dict_items(payload.get("improvements"))
    highlight_adjustments = _dict_items(payload.get("highlight_adjustments"))
    proposals = [*improvements, *highlight_adjustments]
    if not proposals:
        return
    _advance_state_stage(state, "proposed")
    for item in proposals:
        candidate_id = _optional_string(item.get("candidate_id"))
        improvement_id = _optional_string(item.get("id")) or _optional_string(item.get("improvement_id"))
        candidate = _ensure_candidate(
            state,
            candidate_id,
            fallback_key=_missing_candidate_key("improvement", improvement_id, len(state["candidates"])),
        )
        _set_problem_type(candidate, _infer_problem_type(item))
        _append_unique(candidate["improvement_ids"], improvement_id)
        _advance_candidate_stage(candidate, "proposed")


def _apply_approved_actions(state: dict[str, Any], payload: Any) -> None:
    if not isinstance(payload, dict):
        return
    actions = _dict_items(payload.get("approved_actions"))
    state["approved_action_count"] = len(actions)
    if not actions:
        return
    _advance_state_stage(state, "approved")
    for index, action in enumerate(actions):
        approval_id = _optional_string(action.get("approval_id"))
        candidate_id = _optional_string(action.get("candidate_id"))
        improvement_id = _optional_string(action.get("improvement_id"))
        candidate = _ensure_candidate(
            state,
            candidate_id,
            fallback_key=_missing_candidate_key("approval", improvement_id, index, approval_id=approval_id),
        )
        _append_unique(candidate["approval_ids"], approval_id)
        _append_unique(candidate["improvement_ids"], improvement_id)
        _set_problem_type(candidate, _infer_problem_type(action))
        if _approval_requires_execution(action):
            candidate["execution_required"] = True
        _advance_candidate_stage(candidate, "approved")
        if candidate_id is None and _approval_needs_candidate_id(action):
            _add_blocking_reason(candidate, "missing_candidate_id")


def _apply_registry(
    state: dict[str, Any],
    output_dir: Path,
    registry_artifact: dict[str, Any],
    comparisons: list[dict[str, Any]],
) -> None:
    if registry_artifact.get("status") != "loaded":
        return
    registry = load_candidate_registry(output_dir)
    if registry.get("artifact_status") != "loaded":
        return
    candidates = registry.get("candidates")
    if not isinstance(candidates, list):
        return
    comparison_by_relative_path = {
        item["relative_path"]: item
        for item in comparisons
        if isinstance(item.get("relative_path"), str) and item.get("relative_path")
    }
    comparison_by_candidate = _comparison_by_candidate(comparisons)
    for record in _dict_items(candidates):
        candidate_id = _optional_string(record.get("candidate_id"))
        candidate = _ensure_candidate(state, candidate_id, fallback_key=f"registry:{len(state['candidates'])}")
        _set_problem_type(candidate, _optional_string(record.get("problem_type")))
        _append_unique(candidate["approval_ids"], _optional_string(record.get("approval_id")))
        for approval_id in _string_items(record.get("consumed_approval_ids")):
            _append_unique(candidate["approval_ids"], approval_id)
        for artifact_path in _string_items(record.get("candidate_artifacts")):
            _append_unique(candidate["artifact_paths"], artifact_path)
        _advance_candidate_stage(candidate, "executed")

        registry_promotion = _optional_string(record.get("promotion_status"))
        if registry_promotion == "pending_confirmation":
            _set_promotion_status(candidate, "pending_confirmation")
            _add_blocking_reason(candidate, "pending_human_confirmation")
        elif registry_promotion in {"promoted", "rejected"}:
            _set_promotion_status(candidate, registry_promotion)

        comparison_report = _optional_string(record.get("comparison_report"))
        if comparison_report is not None:
            candidate["comparison_report_authoritative"] = True
            comparison = comparison_by_relative_path.get(_normalized_relative_path(comparison_report))
        elif candidate_id is not None:
            candidate_comparisons = comparison_by_candidate.get(candidate_id, [])
            comparison = _worst_comparison(candidate_comparisons)
        else:
            comparison = None
        if comparison is not None:
            _apply_comparison_to_candidate(candidate, comparison)
            continue
        if comparison_report is not None:
            candidate["comparison_report_missing"] = True
        _set_comparison_status(candidate, "unavailable")
        _add_blocking_reason(candidate, "missing_comparison")


def _apply_orphan_comparisons(state: dict[str, Any], comparisons: list[dict[str, Any]]) -> None:
    for comparison in comparisons:
        candidate_id = _optional_string(comparison.get("candidate_id"))
        if candidate_id is None:
            continue
        candidate = _ensure_candidate(state, candidate_id, fallback_key=f"comparison:{candidate_id}")
        if candidate.get("comparison_report_authoritative"):
            continue
        _set_problem_type(candidate, _optional_string(comparison.get("problem_type")))
        _apply_comparison_to_candidate(candidate, comparison)


def _apply_quality_gate(state: dict[str, Any], payload: Any) -> None:
    if not isinstance(payload, dict):
        return
    gated_any_candidate = False
    for candidate in state["candidates"].values():
        if _candidate_has_post_approval_evidence(candidate):
            _advance_candidate_stage(candidate, "gated")
            gated_any_candidate = True
    if gated_any_candidate or not state["candidates"]:
        _advance_state_stage(state, "gated")
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    gate_status = _optional_string(summary.get("status"))
    if gate_status == "fail":
        _add_state_blocking_reason(state, "failed_quality_gate")
        for candidate in state["candidates"].values():
            _add_blocking_reason(candidate, "failed_quality_gate")
        return
    candidate_comparisons = summary.get("candidate_comparisons")
    if isinstance(candidate_comparisons, dict):
        comparison_status = _optional_string(candidate_comparisons.get("status"))
        if comparison_status in CANDIDATE_STATUSES:
            state["quality_gate_comparison_status"] = comparison_status


def _apply_missing_ball_resolution(state: dict[str, Any], payload: Any) -> None:
    if not isinstance(payload, dict):
        return
    resolutions = _dict_items(payload.get("resolutions"))
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    summary_status = _optional_string(summary.get("status"))
    if summary_status == "resolved_not_visible":
        _advance_state_stage(state, "finalized")
    for index, resolution in enumerate(resolutions):
        candidate_id = _optional_string(resolution.get("candidate_id"))
        approval_id = _optional_string(resolution.get("approval_id"))
        candidate = _ensure_candidate(
            state,
            candidate_id,
            fallback_key=f"resolution:{candidate_id or approval_id or index}",
        )
        _set_problem_type(candidate, _optional_string(resolution.get("problem_type")) or "missing_ball")
        _append_unique(candidate["approval_ids"], approval_id)
        if _is_resolved_not_visible_resolution(resolution):
            _advance_candidate_stage(candidate, "finalized")
            _set_resolution_status(candidate, "resolved_not_visible")
            continue
        if _optional_string(resolution.get("status")) == "resolved_not_visible":
            _add_blocking_reason(candidate, "missing_evidence")


def _apply_final_manifest(
    state: dict[str, Any],
    payload: Any,
    output_dir: Path,
    discovered_comparisons: list[dict[str, Any]],
) -> None:
    if not isinstance(payload, dict):
        return
    _advance_state_stage(state, "finalized")
    comparison_refs = [
        _comparison_summary_from_ref(item, output_dir=output_dir)
        for item in _dict_items(payload.get("comparison_reports"))
    ]
    comparison_refs = [item for item in comparison_refs if item is not None]
    if comparison_refs:
        discovered_keys = {
            key
            for item in discovered_comparisons
            for key in [_comparison_report_key(item, output_dir=output_dir)]
            if key is not None
        }
        new_refs = [
            item
            for item in comparison_refs
            if _comparison_report_key(item, output_dir=output_dir) not in discovered_keys
        ]
        state["comparison_report_count"] += len(new_refs)
    comparison_by_candidate = _comparison_by_candidate([*discovered_comparisons, *comparison_refs])
    comparison_missing_rejections = {
        candidate_id
        for item in _dict_items(payload.get("rejected_candidates"))
        for candidate_id in [_optional_string(item.get("candidate_id"))]
        if candidate_id is not None and _optional_string(item.get("reason")) == "comparison_missing"
    }

    for item in _dict_items(payload.get("candidate_outputs")):
        candidate_id = _optional_string(item.get("id")) or _optional_string(item.get("candidate_id"))
        if candidate_id is None:
            continue
        candidate = _ensure_candidate(state, candidate_id, fallback_key=f"manifest-candidate:{candidate_id}")
        _set_problem_type(candidate, _optional_string(item.get("problem_type")))
        _advance_candidate_stage(candidate, "executed")
        comparison = _worst_comparison(comparison_by_candidate.get(candidate_id, []))
        if comparison is not None:
            _apply_comparison_to_candidate(candidate, comparison)
        elif candidate_id not in comparison_missing_rejections:
            _set_comparison_status(candidate, "unavailable")
            _add_blocking_reason(candidate, "missing_comparison")

    for item in _dict_items(payload.get("final_selected_artifacts")):
        candidate_id = _optional_string(item.get("candidate_id"))
        if candidate_id is None:
            candidate = _ensure_candidate(state, None, fallback_key=f"final-missing:{len(state['candidates'])}")
            _add_blocking_reason(candidate, "missing_candidate_id")
            _set_promotion_status(candidate, "blocked")
            continue
        candidate = _ensure_candidate(state, candidate_id, fallback_key=f"final:{candidate_id}")
        _advance_candidate_stage(candidate, "finalized")
        _force_promotion_status(candidate, "promoted")
        _remove_blocking_reason(candidate, "pending_human_confirmation")
        _set_resolution_status(candidate, "candidate_output")
        comparison = _worst_comparison(comparison_by_candidate.get(candidate_id, []))
        if comparison is not None:
            _apply_comparison_to_candidate(candidate, comparison)
        elif candidate_id not in comparison_missing_rejections:
            _set_comparison_status(candidate, "unavailable")
            _add_blocking_reason(candidate, "missing_comparison")

    for item in _dict_items(payload.get("rejected_candidates")):
        candidate_id = _optional_string(item.get("candidate_id"))
        candidate = _ensure_candidate(
            state,
            candidate_id,
            fallback_key=f"rejected:{candidate_id or len(state['candidates'])}",
        )
        _append_unique(candidate["approval_ids"], _optional_string(item.get("approval_id")))
        for approval_id in _string_items(item.get("approval_ids")):
            _append_unique(candidate["approval_ids"], approval_id)
        _advance_candidate_stage(candidate, "finalized")
        _set_promotion_status(candidate, "rejected")
        _apply_manifest_rejection_reason(candidate, _optional_string(item.get("reason")))
        comparison_status = _optional_string(item.get("comparison_status"))
        if comparison_status in CANDIDATE_STATUSES:
            _set_comparison_status(candidate, comparison_status)

    for item in _dict_items(payload.get("pending_candidates")):
        candidate_id = _optional_string(item.get("candidate_id"))
        candidate = _ensure_candidate(
            state,
            candidate_id,
            fallback_key=f"pending:{candidate_id or len(state['candidates'])}",
        )
        _advance_candidate_stage(candidate, "finalized")
        _append_unique(candidate["approval_ids"], _optional_string(item.get("approval_id")))
        for approval_id in _string_items(item.get("approval_ids")):
            _append_unique(candidate["approval_ids"], approval_id)
        _set_problem_type(candidate, _optional_string(item.get("problem_type")))
        comparison_status = _optional_string(item.get("comparison_status"))
        item_status = _optional_string(item.get("status"))
        execution_status = _optional_string(item.get("execution_status"))
        is_blocked_pending = item_status == "blocked" or execution_status == "blocked"
        if comparison_status in {"pass", "warn", "fail"}:
            _set_comparison_status(candidate, comparison_status)
        if comparison_status == "unavailable" and is_blocked_pending:
            _set_comparison_status(candidate, comparison_status)
            _add_blocking_reason(candidate, "missing_comparison")
        if comparison_status == "warn":
            _set_promotion_status(candidate, "pending_confirmation")
            _add_blocking_reason(candidate, "pending_human_confirmation")
        if is_blocked_pending:
            _set_promotion_status(candidate, "blocked")
            _add_blocking_reason(candidate, "missing_evidence")

    for item in _dict_items(payload.get("unsupported_candidates")):
        candidate_id = _optional_string(item.get("candidate_id"))
        candidate = _ensure_candidate(
            state,
            candidate_id,
            fallback_key=f"unsupported:{candidate_id or len(state['candidates'])}",
        )
        _advance_candidate_stage(candidate, "finalized")
        _set_promotion_status(candidate, "blocked")
        _add_blocking_reason(candidate, "unsupported_type")

    for item in _dict_items(payload.get("resolved_noop_candidates")):
        candidate_id = _optional_string(item.get("candidate_id"))
        approval_id = _optional_string(item.get("approval_id"))
        candidate = _ensure_candidate(
            state,
            candidate_id,
            fallback_key=f"resolved-noop:{candidate_id or approval_id or len(state['candidates'])}",
        )
        _append_unique(candidate["approval_ids"], approval_id)
        _advance_candidate_stage(candidate, "finalized")
        _set_resolution_status(candidate, "resolved_not_visible")

    quality_gate_status = payload.get("quality_gate_status")
    if isinstance(quality_gate_status, dict) and quality_gate_status.get("status") == "fail":
        _add_state_blocking_reason(state, "failed_quality_gate")
        for candidate in state["candidates"].values():
            _add_blocking_reason(candidate, "failed_quality_gate")


def _finalize_pending_execution(state: dict[str, Any]) -> None:
    for candidate in state["candidates"].values():
        if (
            candidate["approval_ids"]
            and candidate["execution_required"]
            and not _candidate_has_post_approval_evidence(candidate)
            and candidate["resolution_status"] == "none"
        ):
            _force_candidate_stage(candidate, "pending_execution")
            _add_blocking_reason(candidate, "pending_api_execution")
        if candidate["comparison_status"] == "warn" and candidate["promotion_status"] == "not_promoted":
            _set_promotion_status(candidate, "pending_confirmation")
            _add_blocking_reason(candidate, "pending_human_confirmation")


def _summary(state: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    stage = state["stage"]
    comparison_statuses: list[str] = []
    promotion_statuses: list[str] = []
    resolution_statuses: list[str] = []
    blocking_reasons: list[str] = []
    for candidate in candidates:
        stage = _max_stage(stage, candidate["stage"])
        comparison_statuses.append(candidate["comparison_status"])
        promotion_statuses.append(candidate["promotion_status"])
        resolution_statuses.append(candidate["resolution_status"])
        for reason in candidate["blocking_reasons"]:
            _append_unique(blocking_reasons, reason)
    if not comparison_statuses and state.get("quality_gate_comparison_status") in CANDIDATE_STATUSES:
        comparison_statuses.append(state["quality_gate_comparison_status"])
    for reason in state["blocking_reasons"]:
        _append_unique(blocking_reasons, reason)
    return {
        "stage": stage,
        "comparison_status": _summary_comparison_status(comparison_statuses),
        "promotion_status": _summary_promotion_status(promotion_statuses),
        "resolution_status": _summary_resolution_status(resolution_statuses),
        "blocking_reasons": _ordered_blocking_reasons(blocking_reasons),
        "candidate_count": len(candidates),
        "approved_action_count": state["approved_action_count"],
        "comparison_report_count": state["comparison_report_count"],
    }


def _new_candidate(candidate_id: str | None, source_key: str) -> dict[str, Any]:
    return {
        "source_key": source_key,
        "candidate_id": candidate_id,
        "problem_type": None,
        "improvement_ids": [],
        "approval_ids": [],
        "artifact_paths": [],
        "stage": "review_only",
        "comparison_status": "none",
        "promotion_status": "not_promoted",
        "resolution_status": "none",
        "blocking_reasons": [],
        "comparison_report_authoritative": False,
        "comparison_report_missing": False,
        "execution_required": False,
    }


def _ensure_candidate(state: dict[str, Any], candidate_id: str | None, *, fallback_key: str) -> dict[str, Any]:
    key = f"candidate:{candidate_id}" if candidate_id else fallback_key
    candidates = state["candidates"]
    candidate = candidates.get(key)
    if candidate is None:
        candidate = _new_candidate(candidate_id, key)
        candidates[key] = candidate
    elif candidate_id and candidate.get("candidate_id") is None:
        candidate["candidate_id"] = candidate_id
    return candidate


def _public_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    result = {
        "source_key": candidate["source_key"],
        "candidate_id": candidate["candidate_id"],
        "problem_type": candidate["problem_type"],
        "improvement_ids": list(candidate["improvement_ids"]),
        "approval_ids": list(candidate["approval_ids"]),
        "artifact_paths": list(candidate["artifact_paths"]),
        "stage": candidate["stage"],
        "comparison_status": candidate["comparison_status"],
        "promotion_status": candidate["promotion_status"],
        "resolution_status": candidate["resolution_status"],
        "blocking_reasons": _ordered_blocking_reasons(candidate["blocking_reasons"]),
    }
    return result


def _apply_comparison_to_candidate(candidate: dict[str, Any], comparison: dict[str, Any]) -> None:
    _set_comparison_status(candidate, str(comparison["status"]))
    _advance_candidate_stage(candidate, "compared")
    _set_problem_type(candidate, _optional_string(comparison.get("problem_type")))
    _append_unique(candidate["approval_ids"], _optional_string(comparison.get("approval_id")))
    for approval_id in _string_items(comparison.get("consumed_approval_ids")):
        _append_unique(candidate["approval_ids"], approval_id)


def _discover_comparison_reports(output_dir: Path) -> list[dict[str, Any]]:
    paths: list[Path] = []
    if output_dir.exists():
        paths.extend(path for path in output_dir.glob("*.json") if _could_be_comparison_report(path))
    candidate_root = output_dir / "ai_candidates"
    if candidate_root.exists():
        paths.extend(path for path in candidate_root.rglob("*.json") if _could_be_comparison_report(path))
    summaries: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for path in sorted(paths, key=lambda item: item.as_posix()):
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        payload = _read_json(path)
        summary = _comparison_summary_from_payload(payload, path=path, output_dir=output_dir)
        if summary is not None:
            summaries.append(summary)
    return summaries


def _could_be_comparison_report(path: Path) -> bool:
    return path.name.endswith(".json") and "comparison" in path.name


def _comparison_summary_from_payload(payload: Any, *, path: Path, output_dir: Path) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if not _is_candidate_comparison_payload(payload):
        return None
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    status = summary.get("status") or payload.get("comparison_status") or payload.get("status")
    if status not in CANDIDATE_STATUSES and "checks" not in payload:
        return None
    try:
        status_payload = comparison_payload_status(payload)
    except (TypeError, ValueError):
        return None
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else {}
    candidate_id = _optional_string(payload.get("candidate_id")) or _optional_string(candidate.get("id")) or _optional_string(
        candidate.get("candidate_id")
    )
    try:
        relative_path = path.resolve().relative_to(output_dir.resolve()).as_posix()
    except ValueError:
        relative_path = path.as_posix()
    return {
        "path": str(path),
        "relative_path": relative_path,
        "candidate_id": candidate_id,
        "problem_type": _optional_string(payload.get("problem_type")),
        "approval_id": _optional_string(payload.get("approval_id")),
        "consumed_approval_ids": _string_items(payload.get("consumed_approval_ids")),
        "status": status_payload["status"],
        "artifact_status": status_payload["artifact_status"],
    }


def _comparison_summary_from_ref(value: dict[str, Any], *, output_dir: Path) -> dict[str, Any] | None:
    status = _optional_string(value.get("status"))
    if status not in CANDIDATE_STATUSES:
        summary = value.get("summary") if isinstance(value.get("summary"), dict) else {}
        status = _optional_string(summary.get("status"))
    if status not in CANDIDATE_STATUSES:
        return None
    candidate_id = _optional_string(value.get("candidate_id"))
    path = _optional_string(value.get("path"))
    return {
        "path": path,
        "relative_path": _relative_comparison_path(path, output_dir=output_dir),
        "candidate_id": candidate_id,
        "problem_type": _optional_string(value.get("problem_type")),
        "approval_id": _optional_string(value.get("approval_id")),
        "consumed_approval_ids": _string_items(value.get("consumed_approval_ids")),
        "status": status,
        "artifact_status": _optional_string(value.get("artifact_status")) or "manifest_ref",
    }


def _comparison_by_candidate(comparisons: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for comparison in comparisons:
        candidate_id = _optional_string(comparison.get("candidate_id"))
        if candidate_id is None:
            continue
        result.setdefault(candidate_id, []).append(comparison)
    return result


def _worst_comparison(comparisons: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not comparisons:
        return None
    return max(comparisons, key=lambda item: _COMPARISON_RANK[str(item.get("status") or "none")])


def _comparison_report_key(item: dict[str, Any], *, output_dir: Path) -> str | None:
    relative_path = _optional_string(item.get("relative_path"))
    if relative_path is not None:
        return _normalized_relative_path(relative_path)
    return _relative_comparison_path(_optional_string(item.get("path")), output_dir=output_dir)


def _relative_comparison_path(value: str | None, *, output_dir: Path) -> str | None:
    if value is None:
        return None
    path = Path(value)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(output_dir.resolve()).as_posix()
        except ValueError:
            return str(path.resolve())
    return _normalized_relative_path(value)


def _apply_manifest_rejection_reason(candidate: dict[str, Any], reason: str | None) -> None:
    if reason is None:
        return
    if reason in {"missing_candidate_id", "unknown_candidate_id"}:
        _add_blocking_reason(candidate, "missing_candidate_id")
    elif reason in {"comparison_missing"}:
        _add_blocking_reason(candidate, "missing_comparison")
    elif reason in {"comparison_unavailable"}:
        _add_blocking_reason(candidate, "missing_evidence")
    elif reason in {"requires_human_confirmation"}:
        _set_promotion_status(candidate, "pending_confirmation")
        _add_blocking_reason(candidate, "pending_human_confirmation")
    elif "unsafe" in reason or "invalid_window" in reason or "full_video" in reason:
        _add_blocking_reason(candidate, "unsafe_window")
    elif "unsupported" in reason:
        _set_promotion_status(candidate, "blocked")
        _add_blocking_reason(candidate, "unsupported_type")


def _is_resolved_not_visible_resolution(item: dict[str, Any]) -> bool:
    if _optional_string(item.get("status")) != "resolved_not_visible":
        return False
    start_frame = _optional_int(item.get("start_frame"))
    end_frame = _optional_int(item.get("end_frame"))
    if start_frame is None or end_frame is None or end_frame < start_frame:
        return False
    evidence = item.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return False
    likely_region = item.get("likely_ball_region") if isinstance(item.get("likely_ball_region"), dict) else {}
    description = str(likely_region.get("description") or item.get("resolution") or "").lower()
    return "not_visible" in description or "ball_not_visible" in json.dumps(evidence, ensure_ascii=True).lower()


def _approval_needs_candidate_id(action: dict[str, Any]) -> bool:
    return _approval_requires_execution(action)


def _approval_requires_execution(action: dict[str, Any]) -> bool:
    return _optional_string(action.get("approved_action")) in {
        "targeted_rerun",
        "rerun_ball_window",
        "mark_ball_not_visible",
        "localize_ball_roi",
        "noise_filter_adjustment",
        "tighten_noise_filter",
        "reject_noise",
        "adjust_follow_cam",
        "tracking_rerun_before_follow_cam",
        "adjust_highlight_window",
        "render_suggested_highlight",
    }


def _missing_candidate_key(
    prefix: str,
    improvement_id: str | None,
    fallback_index: int,
    *,
    approval_id: str | None = None,
) -> str:
    if improvement_id is not None:
        return f"improvement:{improvement_id}"
    if approval_id is not None:
        return f"{prefix}:{approval_id}"
    return f"{prefix}:{fallback_index}"


def _is_candidate_comparison_payload(payload: dict[str, Any]) -> bool:
    checks = payload.get("checks")
    if isinstance(checks, list):
        return (
            isinstance(payload.get("candidate"), dict)
            or isinstance(payload.get("baseline"), dict)
            or _optional_string(payload.get("problem_type")) is not None
            or _optional_string(payload.get("candidate_id")) is not None
        )
    has_known_candidate_status = (
        _optional_string(payload.get("comparison_status")) in CANDIDATE_STATUSES
        or _optional_string(payload.get("status")) in CANDIDATE_STATUSES
    )
    return (
        has_known_candidate_status
        and _optional_string(payload.get("candidate_id")) is not None
        and (
            _optional_string(payload.get("problem_type")) is not None
            or isinstance(payload.get("candidate"), dict)
            or isinstance(payload.get("baseline"), dict)
            or _optional_string(payload.get("candidate_dir")) is not None
        )
    )


def _infer_problem_type(item: dict[str, Any]) -> str | None:
    problem_type = _optional_string(item.get("problem_type"))
    if problem_type is not None:
        return problem_type
    text = " ".join(
        str(value)
        for value in (
            item.get("approved_action"),
            item.get("recommended_action"),
            item.get("area"),
            item.get("root_cause_module"),
            " ".join(_string_items(item.get("failure_tags"))),
        )
        if value
    ).lower()
    if any(
        token in text
        for token in (
            "missing_ball",
            "ball_lost",
            "lost_gap",
            "targeted_rerun",
            "rerun_ball_window",
            "localize_ball_roi",
            "mark_ball_not_visible",
        )
    ):
        return "missing_ball"
    if any(token in text for token in ("noise", "false_positive", "reject_noise", "filter")):
        return "noise"
    if any(token in text for token in ("follow_cam", "camera")):
        return "follow_cam"
    if "highlight" in text:
        return "highlight"
    return None


def _candidate_has_post_approval_evidence(candidate: dict[str, Any]) -> bool:
    if candidate["resolution_status"] != "none":
        return True
    if candidate["comparison_status"] != "none":
        return True
    if candidate["artifact_paths"]:
        return True
    return _STAGE_RANK[candidate["stage"]] >= _STAGE_RANK["executed"]


def _load_artifact(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"status": "missing", "payload": None, "path": str(path)}
    payload = _read_json(path)
    if payload is None:
        return {"status": "ignored", "payload": None, "path": str(path)}
    return {"status": "loaded", "payload": payload, "path": str(path)}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _dict_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
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


def _normalized_relative_path(value: str | None) -> str | None:
    if value is None:
        return None
    return Path(value.replace("\\", "/")).as_posix()


def _collect_generated_at(state: dict[str, Any], payload: Any) -> None:
    if not isinstance(payload, dict):
        return
    generated_at = _optional_string(payload.get("generated_at"))
    if generated_at is not None:
        state["generated_at_values"].append(generated_at)


def _latest_generated_at(values: list[str]) -> str | None:
    return max(values) if values else None


def _advance_state_stage(state: dict[str, Any], stage: str) -> None:
    state["stage"] = _max_stage(state["stage"], stage)


def _advance_candidate_stage(candidate: dict[str, Any], stage: str) -> None:
    candidate["stage"] = _max_stage(candidate["stage"], stage)


def _force_candidate_stage(candidate: dict[str, Any], stage: str) -> None:
    candidate["stage"] = stage


def _max_stage(left: str, right: str) -> str:
    return right if _STAGE_RANK[right] > _STAGE_RANK[left] else left


def _set_comparison_status(candidate: dict[str, Any], status: str) -> None:
    if status not in COMPARISON_STATUSES:
        return
    current = candidate["comparison_status"]
    candidate["comparison_status"] = status if _COMPARISON_RANK[status] > _COMPARISON_RANK[current] else current


def _set_promotion_status(candidate: dict[str, Any], status: str) -> None:
    if status not in PROMOTION_STATUSES:
        return
    current = candidate["promotion_status"]
    candidate["promotion_status"] = status if _PROMOTION_RANK[status] > _PROMOTION_RANK[current] else current


def _force_promotion_status(candidate: dict[str, Any], status: str) -> None:
    if status in PROMOTION_STATUSES:
        candidate["promotion_status"] = status


def _set_resolution_status(candidate: dict[str, Any], status: str) -> None:
    if status not in RESOLUTION_STATUSES:
        return
    current = candidate["resolution_status"]
    candidate["resolution_status"] = status if _RESOLUTION_RANK[status] > _RESOLUTION_RANK[current] else current


def _set_problem_type(candidate: dict[str, Any], problem_type: str | None) -> None:
    if problem_type is not None and candidate["problem_type"] is None:
        candidate["problem_type"] = problem_type


def _add_blocking_reason(candidate: dict[str, Any], reason: str) -> None:
    if reason in BLOCKING_REASONS:
        _append_unique(candidate["blocking_reasons"], reason)


def _add_state_blocking_reason(state: dict[str, Any], reason: str) -> None:
    if reason in BLOCKING_REASONS:
        _append_unique(state["blocking_reasons"], reason)


def _remove_blocking_reason(candidate: dict[str, Any], reason: str) -> None:
    candidate["blocking_reasons"] = [item for item in candidate["blocking_reasons"] if item != reason]


def _append_unique(items: list[str], value: str | None) -> None:
    if value is not None and value not in items:
        items.append(value)


def _summary_comparison_status(statuses: list[str]) -> str:
    valid = [status for status in statuses if status in COMPARISON_STATUSES and status != "none"]
    if not valid:
        return "none"
    return max(valid, key=lambda status: _COMPARISON_RANK[status])


def _summary_promotion_status(statuses: list[str]) -> str:
    valid = [status for status in statuses if status in PROMOTION_STATUSES]
    if not valid:
        return "not_promoted"
    return max(valid, key=lambda status: _PROMOTION_RANK[status])


def _summary_resolution_status(statuses: list[str]) -> str:
    valid = [status for status in statuses if status in RESOLUTION_STATUSES]
    if not valid:
        return "none"
    return max(valid, key=lambda status: _RESOLUTION_RANK[status])


def _ordered_blocking_reasons(reasons: list[str]) -> list[str]:
    present = set(reasons)
    return [reason for reason in BLOCKING_REASONS if reason in present]
