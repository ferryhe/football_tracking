from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from football_tracking.ai_candidate_registry import load_candidate_registry
from football_tracking.ai_candidate_comparison import (
    ARTIFACT_ROLES,
    CANDIDATE_STATUSES,
    SCHEMA_VERSION,
    comparison_payload_status,
    safe_json_file_name,
)

FINAL_ARTIFACT_MANIFEST_NAME = "final_ai_improvement_artifact_manifest.json"
STATUS_RANK = {"pass": 0, "warn": 1, "unavailable": 2, "fail": 3}
FINALIZATION_OUTPUT_ROLES = {
    "missing_ball_track": {
        "problem_type": "missing_ball",
        "type": "track",
        "singleton": True,
        "preferred_names": ("ball_track.csv",),
    },
    "noise_cleaned_track": {
        "problem_type": "noise",
        "type": "track",
        "singleton": True,
        "preferred_names": ("ball_track.cleaned.csv",),
    },
    "follow_cam_video": {
        "problem_type": "follow_cam",
        "type": "video",
        "singleton": True,
        "preferred_names": ("follow_cam.mp4",),
    },
    "highlight_clip": {
        "problem_type": "highlight",
        "type": "clip",
        "singleton": False,
        "preferred_names": ("highlight.mp4",),
    },
}


def build_final_artifact_manifest(
    *,
    baseline_output: dict[str, Any] | str | Path,
    candidate_outputs: list[dict[str, Any]],
    final_artifacts: list[dict[str, Any]],
    consumed_approvals: list[dict[str, Any]] | None = None,
    comparison_reports: list[dict[str, Any] | str | Path] | None = None,
    quality_gate_status: dict[str, Any] | str | None = None,
    rejected_candidates: list[dict[str, Any]] | None = None,
    pending_candidates: list[dict[str, Any]] | None = None,
    unsupported_candidates: list[dict[str, Any]] | None = None,
    resolved_noop_candidates: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    baseline = _artifact_ref(baseline_output, role="baseline")
    candidates = [_artifact_ref(item, role="candidate") for item in candidate_outputs]
    candidate_ids = _candidate_output_ids(candidates)
    comparisons = [_comparison_ref(item) for item in (comparison_reports or [])]
    comparison_status_by_candidate = _comparison_status_by_candidate(comparisons)

    manifest_warnings = list(warnings or [])
    consumed = [_json_ready(item) for item in (consumed_approvals or [])]
    rejected = [_json_ready(item) for item in (rejected_candidates or [])]
    pending = [_json_ready(item) for item in (pending_candidates or [])]
    unsupported = [_json_ready(item) for item in (unsupported_candidates or [])]
    resolved_noop = [_json_ready(item) for item in (resolved_noop_candidates or [])]
    selected: list[dict[str, Any]] = []

    for artifact in final_artifacts:
        final_ref = _artifact_ref(artifact, role="final")
        candidate_id = _final_candidate_id(final_ref)
        if candidate_id is None:
            rejected.append(_rejection(None, "missing_candidate_id", final_ref, "unavailable"))
            continue
        if candidate_id not in candidate_ids:
            rejected.append(_rejection(candidate_id, "unknown_candidate_id", final_ref, "unavailable"))
            continue
        comparison_status = comparison_status_by_candidate.get(candidate_id)
        if comparison_status == "fail":
            rejected.append(_rejection(candidate_id, "comparison_failed", final_ref, comparison_status))
            continue
        if comparison_status == "unavailable":
            rejected.append(_rejection(candidate_id, "comparison_unavailable", final_ref, comparison_status))
            continue
        if comparison_status == "warn" and (
            final_ref.get("requires_human_confirmation") is not True
            or not _has_consumed_human_confirmation(candidate_id, consumed)
        ):
            rejected.append(_rejection(candidate_id, "requires_human_confirmation", final_ref, comparison_status))
            manifest_warnings.append(f"{candidate_id} requires human confirmation before promotion")
            continue
        if comparison_status is None:
            rejected.append(_rejection(candidate_id, "comparison_missing", final_ref, "unavailable"))
            continue
        selected.append(final_ref)

    videos = _media_refs([baseline, *candidates, *selected], media_type="video")
    clips = _media_refs([baseline, *candidates, *selected], media_type="clip")
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "baseline_output": baseline,
        "candidate_outputs": candidates,
        "final_selected_artifacts": selected,
        "consumed_approvals": consumed,
        "comparison_reports": comparisons,
        "quality_gate_status": _quality_gate_status(quality_gate_status),
        "rejected_candidates": rejected,
        "pending_candidates": pending,
        "unsupported_candidates": unsupported,
        "resolved_noop_candidates": resolved_noop,
        "warnings": manifest_warnings,
        "videos": videos,
        "clips": clips,
        "summary": {
            "candidate_output_count": len(candidates),
            "final_artifact_count": len(selected),
            "rejected_candidate_count": len(rejected),
            "pending_candidate_count": len(pending),
            "unsupported_candidate_count": len(unsupported),
            "resolved_noop_candidate_count": len(resolved_noop),
            "comparison_counts_by_problem_type": _counts_by_key(comparisons, "problem_type"),
            "comparison_counts_by_status": _counts_by_key(comparisons, "status"),
            "warning_count": len(manifest_warnings),
        },
    }


def write_final_artifact_manifest(
    output_dir: Path,
    *,
    baseline_output: dict[str, Any] | str | Path,
    candidate_outputs: list[dict[str, Any]],
    final_artifacts: list[dict[str, Any]],
    consumed_approvals: list[dict[str, Any]] | None = None,
    comparison_reports: list[dict[str, Any] | str | Path] | None = None,
    quality_gate_status: dict[str, Any] | str | None = None,
    rejected_candidates: list[dict[str, Any]] | None = None,
    pending_candidates: list[dict[str, Any]] | None = None,
    unsupported_candidates: list[dict[str, Any]] | None = None,
    resolved_noop_candidates: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
    name: str = FINAL_ARTIFACT_MANIFEST_NAME,
) -> dict[str, Any]:
    manifest_name = _safe_manifest_name(name)
    payload = build_final_artifact_manifest(
        baseline_output=baseline_output,
        candidate_outputs=candidate_outputs,
        final_artifacts=final_artifacts,
        consumed_approvals=consumed_approvals,
        comparison_reports=comparison_reports,
        quality_gate_status=quality_gate_status,
        rejected_candidates=rejected_candidates,
        pending_candidates=pending_candidates,
        unsupported_candidates=unsupported_candidates,
        resolved_noop_candidates=resolved_noop_candidates,
        warnings=warnings,
    )
    _write_json(Path(output_dir) / manifest_name, payload)
    return payload


def finalize_ai_candidate(
    output_dir: Path,
    *,
    problem_type: str,
    candidate_id: str,
    approval_id: str,
    decision: str,
    output_role: str,
    confirm_warn: bool = False,
    note: str | None = None,
    operator_note: str | None = None,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    if decision not in {"promote", "reject"}:
        raise ValueError("decision must be promote or reject")
    role_spec = FINALIZATION_OUTPUT_ROLES.get(output_role)
    if role_spec is None:
        raise ValueError(f"unsupported output_role: {output_role}")
    if role_spec["problem_type"] != problem_type:
        raise ValueError(f"output_role {output_role} is not valid for problem_type {problem_type}")
    candidate_id = _required_identifier(candidate_id, "candidate_id")
    approval_id = _required_identifier(approval_id, "approval_id")

    manifest_path = output_dir / FINAL_ARTIFACT_MANIFEST_NAME
    manifest = _load_finalization_manifest(manifest_path, output_dir=output_dir)
    _merge_registry_context(manifest, output_dir)
    _refresh_manifest_comparisons_from_disk(manifest, output_dir)

    candidate = _candidate_output_for_id(manifest, candidate_id)
    if candidate is None:
        raise ValueError(f"unknown candidate: {candidate_id}")
    candidate_problem_type = _optional_string(candidate.get("problem_type"))
    if candidate_problem_type is not None and candidate_problem_type != problem_type:
        raise ValueError(f"candidate {candidate_id} is not a {problem_type} candidate")

    comparison = _finalization_comparison_for_candidate(
        manifest,
        candidate_id,
        problem_type,
        approval_id=approval_id if decision == "promote" else None,
    )
    approval_found = _manifest_has_approval_for_candidate(
        manifest,
        candidate_id=candidate_id,
        approval_id=approval_id,
        comparison=comparison,
    )
    if not approval_found:
        raise ValueError(f"missing approval for candidate {candidate_id}: {approval_id}")

    _validate_candidate_paths(output_dir, candidate)
    comparison_status = _optional_string(comparison.get("status")) if comparison is not None else None
    if decision == "promote":
        finalization_block = _promotion_blocking_reason(manifest, candidate=candidate, candidate_id=candidate_id, approval_id=approval_id)
        if finalization_block is not None:
            raise ValueError(f"candidate {candidate_id} cannot be promoted: {finalization_block}")
        if comparison is None:
            raise ValueError(f"missing comparison for candidate {candidate_id}")
        if comparison_status == "warn" and not confirm_warn:
            raise ValueError(f"candidate {candidate_id} requires explicit warning confirmation")
        if comparison_status != "pass" and not (comparison_status == "warn" and confirm_warn):
            raise ValueError(f"candidate {candidate_id} comparison status is not promotable: {comparison_status}")

    decision_entry = _operator_decision(
        decision=decision,
        problem_type=problem_type,
        candidate_id=candidate_id,
        approval_id=approval_id,
        output_role=output_role,
        comparison_status=comparison_status or "none",
        confirm_warn=confirm_warn,
        note=operator_note if operator_note is not None else note,
    )
    decision_entry = _upsert_operator_decision(manifest, decision_entry)

    if decision == "promote":
        selected_artifact = _promoted_artifact_ref(
            output_dir,
            candidate,
            comparison=comparison or {},
            problem_type=problem_type,
            candidate_id=candidate_id,
            approval_id=approval_id,
            output_role=output_role,
            role_spec=role_spec,
            comparison_status=str(comparison_status),
            decision_entry=decision_entry,
        )
        _upsert_promoted_artifact(manifest, selected_artifact, role_spec=role_spec)
        _remove_matching_rejections(manifest, candidate_id=candidate_id, approval_id=approval_id)
    else:
        _remove_matching_final_selection(manifest, candidate_id=candidate_id, approval_id=approval_id, output_role=output_role)
        _upsert_rejected_candidate(
            manifest,
            {
                "candidate_id": candidate_id,
                "candidate_ids": [candidate_id],
                "approval_id": approval_id,
                "approval_ids": [approval_id],
                "problem_type": problem_type,
                "output_role": output_role,
                "reason": "operator_rejected",
                "status": "rejected",
                "comparison_status": comparison_status or "none",
                "operator_decision": decision_entry,
            },
        )
    _remove_candidate_from_status_lists(manifest, candidate_id=candidate_id, approval_id=approval_id)

    _refresh_manifest_derived_fields(manifest)
    _write_json(manifest_path, manifest)
    from football_tracking.ai_candidate_lifecycle import build_ai_candidate_lifecycle

    return {
        "manifest": manifest,
        "decision": decision_entry,
        "lifecycle": build_ai_candidate_lifecycle(output_dir),
    }


def _artifact_ref(value: dict[str, Any] | str | Path, *, role: str) -> dict[str, Any]:
    if role not in ARTIFACT_ROLES:
        raise ValueError(f"Unknown artifact role: {role}")
    if isinstance(value, (str, Path)):
        return {"role": role, "path": str(value)}
    if not isinstance(value, dict):
        raise TypeError("artifact reference must be a mapping or path")
    result = _json_ready(value)
    result["role"] = role
    return result


def _comparison_ref(value: dict[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(value, (str, Path)):
        return {"path": str(value), "status": "unavailable", "candidate_id": None}
    if not isinstance(value, dict):
        raise TypeError("comparison report reference must be a mapping or path")
    result = _json_ready(value)
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    status_payload = comparison_payload_status(result)
    candidate = result.get("candidate") if isinstance(result.get("candidate"), dict) else {}
    approval = result.get("approval") if isinstance(result.get("approval"), dict) else {}
    approval_id = result.get("approval_id") or approval.get("approval_id")
    consumed_approval_ids = _string_list(result.get("consumed_approval_ids"))
    if isinstance(approval_id, str) and approval_id.strip() and approval_id.strip() not in consumed_approval_ids:
        consumed_approval_ids = [approval_id.strip(), *consumed_approval_ids]
    return {
        "path": result.get("path") or result.get("report_path"),
        "problem_type": result.get("problem_type"),
        "candidate_id": result.get("candidate_id") or candidate.get("id") or candidate.get("candidate_id"),
        "approval_id": approval_id,
        "consumed_approval_ids": consumed_approval_ids,
        "candidate_dir": result.get("candidate_dir"),
        "candidate_artifacts": _string_list(result.get("candidate_artifacts")),
        "comparison_report": result.get("comparison_report"),
        "status": status_payload["status"],
        "summary": summary,
        "artifact_status": status_payload["artifact_status"],
    }


def _comparison_status_by_candidate(comparisons: list[dict[str, Any]]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for item in comparisons:
        candidate_id = item.get("candidate_id")
        status = item.get("status")
        if not isinstance(candidate_id, str) or not candidate_id.strip() or status not in CANDIDATE_STATUSES:
            continue
        existing = statuses.get(candidate_id)
        if existing is None or STATUS_RANK[status] > STATUS_RANK[existing]:
            statuses[candidate_id] = status
    return statuses


def _counts_by_key(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = item.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        normalized = value.strip()
        counts[normalized] = counts.get(normalized, 0) + 1
    return dict(sorted(counts.items()))


def _quality_gate_status(value: dict[str, Any] | str | None) -> dict[str, Any]:
    if isinstance(value, dict):
        return _json_ready(value)
    if isinstance(value, str):
        return {"status": value}
    return {"status": "unavailable"}


def _final_candidate_id(artifact: dict[str, Any]) -> str | None:
    value = artifact.get("candidate_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _candidate_output_ids(candidates: list[dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for candidate in candidates:
        for key in ("id", "candidate_id"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                result.add(value.strip())
                break
    return result


def _has_consumed_human_confirmation(candidate_id: str, approvals: list[dict[str, Any]]) -> bool:
    for approval in approvals:
        if approval.get("candidate_id") != candidate_id:
            continue
        if approval.get("approval_type") != "human_confirmation":
            continue
        if approval.get("status") == "approved":
            return True
    return False


def _load_finalization_manifest(path: Path, *, output_dir: Path) -> dict[str, Any]:
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"final artifact manifest is corrupt: {path}") from exc
        if not isinstance(loaded, dict):
            raise ValueError("final artifact manifest must be a JSON object")
        payload = loaded
    else:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "baseline_output": {"role": "baseline", "path": str(output_dir), "status": "baseline"},
            "candidate_outputs": [],
            "final_selected_artifacts": [],
            "consumed_approvals": [],
            "comparison_reports": [],
            "quality_gate_status": {"status": "unavailable"},
            "rejected_candidates": [],
            "pending_candidates": [],
            "unsupported_candidates": [],
            "resolved_noop_candidates": [],
            "warnings": [],
            "operator_decisions": [],
        }
    payload.setdefault("schema_version", SCHEMA_VERSION)
    payload.setdefault("baseline_output", {"role": "baseline", "path": str(output_dir), "status": "baseline"})
    for key in (
        "candidate_outputs",
        "final_selected_artifacts",
        "consumed_approvals",
        "comparison_reports",
        "rejected_candidates",
        "pending_candidates",
        "unsupported_candidates",
        "resolved_noop_candidates",
        "warnings",
        "operator_decisions",
    ):
        if key not in payload:
            payload[key] = []
        elif not isinstance(payload.get(key), list):
            raise ValueError(f"final artifact manifest field {key} must be a list")
    if not isinstance(payload.get("quality_gate_status"), dict):
        if "quality_gate_status" in payload:
            raise ValueError("final artifact manifest field quality_gate_status must be an object")
        payload["quality_gate_status"] = {"status": "unavailable"}
    return payload


def _merge_registry_context(manifest: dict[str, Any], output_dir: Path) -> None:
    registry = load_candidate_registry(output_dir)
    if registry.get("artifact_status") != "loaded":
        return
    for record in _dict_items(registry.get("candidates")):
        candidate_id = _optional_string(record.get("candidate_id"))
        if candidate_id is None:
            continue
        if _candidate_output_for_id(manifest, candidate_id) is None:
            manifest["candidate_outputs"].append(
                {
                    "id": candidate_id,
                    "candidate_id": candidate_id,
                    "problem_type": record.get("problem_type"),
                    "path": record.get("candidate_dir"),
                    "candidate_artifacts": record.get("candidate_artifacts") if isinstance(record.get("candidate_artifacts"), list) else [],
                    "status": record.get("comparison_status"),
                }
            )
        if _optional_string(record.get("approval_id")) is not None:
            _append_unique_mapping(
                manifest["consumed_approvals"],
                {
                    "approval_id": record.get("approval_id"),
                    "candidate_id": candidate_id,
                    "problem_type": record.get("problem_type"),
                    "status": "approved",
                },
                key_fields=("approval_id", "candidate_id"),
            )
        comparison_report = _optional_string(record.get("comparison_report"))
        if comparison_report is None:
            continue
        loaded_comparison = _read_json(output_dir / comparison_report)
        if loaded_comparison is not None:
            _upsert_comparison_report(manifest, _comparison_ref({**loaded_comparison, "path": comparison_report}))
        else:
            _upsert_comparison_report(
                manifest,
                {
                    "path": comparison_report,
                    "problem_type": record.get("problem_type"),
                    "candidate_id": candidate_id,
                    "approval_id": record.get("approval_id"),
                    "consumed_approval_ids": record.get("consumed_approval_ids")
                    if isinstance(record.get("consumed_approval_ids"), list)
                    else [],
                    "candidate_dir": record.get("candidate_dir"),
                    "candidate_artifacts": record.get("candidate_artifacts") if isinstance(record.get("candidate_artifacts"), list) else [],
                    "status": record.get("comparison_status") if record.get("comparison_status") in CANDIDATE_STATUSES else "unavailable",
                    "summary": {},
                    "artifact_status": "registry_ref",
                },
            )


def _refresh_manifest_comparisons_from_disk(manifest: dict[str, Any], output_dir: Path) -> None:
    manifest["comparison_reports"] = [
        _refreshed_comparison_report(output_dir, item)
        for item in _dict_items(manifest.get("comparison_reports"))
    ]


def _refreshed_comparison_report(output_dir: Path, item: dict[str, Any]) -> dict[str, Any]:
    report_path = _optional_string(item.get("path")) or _optional_string(item.get("comparison_report"))
    if report_path is None:
        return _unavailable_comparison_report(item, artifact_status="missing_path")
    try:
        resolved = _contained_output_path(output_dir, report_path, field_name="comparison_reports")
        relative_path = resolved.relative_to(Path(output_dir).resolve()).as_posix()
    except ValueError:
        return _unavailable_comparison_report(item, artifact_status="unsafe_path")
    loaded = _read_json(resolved)
    if loaded is None:
        return _unavailable_comparison_report(item, artifact_status="missing_or_invalid")
    try:
        return _comparison_ref({**loaded, "path": relative_path})
    except (TypeError, ValueError):
        return _unavailable_comparison_report(item, artifact_status="missing_or_invalid")


def _unavailable_comparison_report(item: dict[str, Any], *, artifact_status: str) -> dict[str, Any]:
    result = _json_ready(item)
    result["status"] = "unavailable"
    result["artifact_status"] = artifact_status
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    result["summary"] = {**summary, "status": "unavailable"}
    return result


def _upsert_comparison_report(manifest: dict[str, Any], report: dict[str, Any]) -> None:
    key = _comparison_report_identity(report)
    reports: list[dict[str, Any]] = []
    replaced = False
    for item in _dict_items(manifest.get("comparison_reports")):
        if _comparison_report_identity(item) == key:
            if not replaced:
                reports.append(report)
                replaced = True
            continue
        reports.append(item)
    if not replaced:
        reports.append(report)
    manifest["comparison_reports"] = reports


def _comparison_report_identity(item: dict[str, Any]) -> str:
    path_value = _optional_string(item.get("path")) or _optional_string(item.get("comparison_report"))
    if path_value is not None:
        return f"path:{_normalized_path_text(path_value)}"
    candidate_id = _optional_string(item.get("candidate_id")) or ""
    problem_type = _optional_string(item.get("problem_type")) or ""
    approval_ids = sorted(_comparison_approval_ids(item))
    return f"candidate:{candidate_id}:{problem_type}:{','.join(approval_ids)}"


def _candidate_output_for_id(manifest: dict[str, Any], candidate_id: str) -> dict[str, Any] | None:
    for item in _dict_items(manifest.get("candidate_outputs")):
        item_id = _optional_string(item.get("candidate_id")) or _optional_string(item.get("id"))
        if item_id == candidate_id:
            return item
    return None


def _finalization_comparison_for_candidate(
    manifest: dict[str, Any],
    candidate_id: str,
    problem_type: str,
    *,
    approval_id: str | None = None,
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for item in _dict_items(manifest.get("comparison_reports")):
        item_candidate_id = _optional_string(item.get("candidate_id"))
        item_problem_type = _optional_string(item.get("problem_type"))
        if item_candidate_id != candidate_id:
            continue
        if item_problem_type is not None and item_problem_type != problem_type:
            continue
        status = _optional_string(item.get("status"))
        if status not in CANDIDATE_STATUSES:
            summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
            status = _optional_string(summary.get("status"))
        if status not in CANDIDATE_STATUSES:
            continue
        normalized = _json_ready(item)
        normalized["status"] = status
        if approval_id is not None and approval_id not in _comparison_approval_ids(normalized):
            continue
        candidates.append(normalized)
    if not candidates:
        return None
    return max(candidates, key=lambda item: STATUS_RANK[str(item["status"])])


def _manifest_has_approval_for_candidate(
    manifest: dict[str, Any],
    *,
    candidate_id: str,
    approval_id: str,
    comparison: dict[str, Any] | None,
) -> bool:
    if comparison is not None and approval_id in _comparison_approval_ids(comparison):
        return True
    for item in _dict_items(manifest.get("consumed_approvals")):
        item_approval_id = _optional_string(item.get("approval_id"))
        if item_approval_id != approval_id:
            continue
        item_candidate_id = _optional_string(item.get("candidate_id"))
        if item_candidate_id == candidate_id:
            return True
    return False


def _promotion_blocking_reason(
    manifest: dict[str, Any],
    *,
    candidate: dict[str, Any],
    candidate_id: str,
    approval_id: str,
) -> str | None:
    if _is_review_only_candidate(candidate):
        return "review_only"
    for item in _dict_items(manifest.get("unsupported_candidates")):
        if _manifest_status_item_matches(item, candidate_id=candidate_id, approval_id=approval_id):
            return "unsupported_type"
    return None


def _is_review_only_candidate(candidate: dict[str, Any]) -> bool:
    for key in ("candidate_intent", "intent", "stage", "status"):
        if _optional_string(candidate.get(key)) == "review_only":
            return True
    return False


def _manifest_status_item_matches(item: dict[str, Any], *, candidate_id: str, approval_id: str) -> bool:
    if _optional_string(item.get("candidate_id")) == candidate_id:
        return True
    if candidate_id in _string_list(item.get("candidate_ids")):
        return True
    if _optional_string(item.get("approval_id")) == approval_id:
        return True
    return approval_id in _string_list(item.get("approval_ids"))


def _comparison_approval_ids(comparison: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    approval_id = _optional_string(comparison.get("approval_id"))
    if approval_id is not None:
        result.add(approval_id)
    for item in _string_list(comparison.get("consumed_approval_ids")):
        result.add(item)
    approval = comparison.get("approval") if isinstance(comparison.get("approval"), dict) else {}
    approval_id = _optional_string(approval.get("approval_id"))
    if approval_id is not None:
        result.add(approval_id)
    return result


def _validate_candidate_paths(output_dir: Path, candidate: dict[str, Any]) -> None:
    for key in ("path", "candidate_dir"):
        value = _optional_string(candidate.get(key))
        if value is not None:
            _contained_output_path(output_dir, value, field_name=key)
    for value in _string_list(candidate.get("candidate_artifacts")):
        _contained_output_path(output_dir, value, field_name="candidate_artifacts")


def _promoted_artifact_ref(
    output_dir: Path,
    candidate: dict[str, Any],
    *,
    comparison: dict[str, Any],
    problem_type: str,
    candidate_id: str,
    approval_id: str,
    output_role: str,
    role_spec: dict[str, Any],
    comparison_status: str,
    decision_entry: dict[str, Any],
) -> dict[str, Any]:
    path = _selected_artifact_path(output_dir, candidate, comparison=comparison, role_spec=role_spec)
    selection_key = _selection_key(candidate, comparison, candidate_id=candidate_id, output_role=output_role)
    return {
        "candidate_id": candidate_id,
        "approval_id": approval_id,
        "problem_type": problem_type,
        "output_role": output_role,
        "selection_key": selection_key,
        "path": path,
        "type": role_spec["type"],
        "status": "selected",
        "comparison_status": comparison_status,
        "operator_decision": decision_entry,
    }


def _selected_artifact_path(
    output_dir: Path,
    candidate: dict[str, Any],
    *,
    comparison: dict[str, Any],
    role_spec: dict[str, Any],
) -> str:
    preferred_names = tuple(str(item) for item in role_spec["preferred_names"])
    artifact_paths = [
        *_string_list(candidate.get("candidate_artifacts")),
        *_string_list(candidate.get("generated_artifacts")),
        *_string_list(comparison.get("candidate_artifacts")),
        *_string_list(comparison.get("generated_artifacts")),
    ]
    candidate_payload = comparison.get("candidate") if isinstance(comparison.get("candidate"), dict) else {}
    for value in (
        _optional_string(candidate.get("artifact_path")),
        _optional_string(candidate.get("path")),
        _optional_string(candidate_payload.get("path")),
    ):
        if value is not None:
            artifact_paths.append(value)
    candidate_dirs = [
        _optional_string(candidate.get("candidate_dir")),
        _optional_string(candidate.get("path")),
        _optional_string(comparison.get("candidate_dir")),
    ]
    for candidate_dir in candidate_dirs:
        if candidate_dir is None:
            continue
        path = Path(candidate_dir)
        if path.suffix:
            continue
        clean_candidate_dir = candidate_dir.rstrip("/").rstrip(chr(92))
        for preferred_name in preferred_names:
            artifact_paths.append(f"{clean_candidate_dir}/{preferred_name}")
    for preferred_name in preferred_names:
        for value in artifact_paths:
            if Path(value).name == preferred_name:
                return _relative_output_path(output_dir, value, field_name="final_selected_artifacts")
    for value in artifact_paths:
        return _relative_output_path(output_dir, value, field_name="final_selected_artifacts")
    raise ValueError("candidate does not expose an artifact path for final selection")


def _selection_key(
    candidate: dict[str, Any],
    comparison: dict[str, Any],
    *,
    candidate_id: str,
    output_role: str,
) -> str:
    if output_role != "highlight_clip":
        return output_role
    for source in (candidate, comparison):
        for key in ("event_id", "highlight_id", "highlight_candidate_id", "selection_key"):
            value = _optional_string(source.get(key))
            if value is not None:
                return value
    return candidate_id


def _operator_decision(
    *,
    decision: str,
    problem_type: str,
    candidate_id: str,
    approval_id: str,
    output_role: str,
    comparison_status: str,
    confirm_warn: bool,
    note: str | None,
) -> dict[str, Any]:
    confirmation_status = "confirmed" if comparison_status == "warn" and confirm_warn else "not_required"
    if comparison_status == "warn" and not confirm_warn:
        confirmation_status = "missing"
    return {
        "decision_id": f"{decision}:{problem_type}:{candidate_id}:{approval_id}:{output_role}",
        "decided_at": _utc_now_iso(),
        "decision": decision,
        "approval_id": approval_id,
        "candidate_id": candidate_id,
        "problem_type": problem_type,
        "output_role": output_role,
        "comparison_status": comparison_status,
        "confirm_warn": bool(confirm_warn),
        "confirmation_status": confirmation_status,
        "note": note or "",
    }


def _upsert_operator_decision(manifest: dict[str, Any], decision_entry: dict[str, Any]) -> dict[str, Any]:
    decisions = manifest.setdefault("operator_decisions", [])
    existing = _find_by_decision_id(decisions, str(decision_entry["decision_id"]))
    if existing is not None:
        existing["note"] = decision_entry["note"]
        existing["confirm_warn"] = decision_entry["confirm_warn"]
        existing["confirmation_status"] = decision_entry["confirmation_status"]
        return existing
    decisions.append(decision_entry)
    return decision_entry


def _upsert_promoted_artifact(
    manifest: dict[str, Any],
    selected_artifact: dict[str, Any],
    *,
    role_spec: dict[str, Any],
) -> None:
    selected = _dict_items(manifest.get("final_selected_artifacts"))
    if role_spec["singleton"]:
        selected = [
            item
            for item in selected
            if _optional_string(item.get("output_role")) != selected_artifact["output_role"]
        ]
    else:
        selected = [
            item
            for item in selected
            if not (
                _optional_string(item.get("output_role")) == selected_artifact["output_role"]
                and _optional_string(item.get("selection_key")) == selected_artifact["selection_key"]
            )
        ]
    decision_id = selected_artifact["operator_decision"]["decision_id"]
    if not any(
        isinstance(item.get("operator_decision"), dict)
        and item["operator_decision"].get("decision_id") == decision_id
        for item in selected
    ):
        selected.append(selected_artifact)
    manifest["final_selected_artifacts"] = selected


def _upsert_rejected_candidate(manifest: dict[str, Any], rejection: dict[str, Any]) -> None:
    decision_id = rejection["operator_decision"]["decision_id"]
    rejected = [
        item
        for item in _dict_items(manifest.get("rejected_candidates"))
        if not (
            isinstance(item.get("operator_decision"), dict)
            and item["operator_decision"].get("decision_id") == decision_id
        )
    ]
    rejected.append(rejection)
    manifest["rejected_candidates"] = rejected


def _remove_matching_rejections(manifest: dict[str, Any], *, candidate_id: str, approval_id: str) -> None:
    manifest["rejected_candidates"] = [
        item
        for item in _dict_items(manifest.get("rejected_candidates"))
        if not (
            _optional_string(item.get("candidate_id")) == candidate_id
            and (
                _optional_string(item.get("approval_id")) == approval_id
                or approval_id in _string_list(item.get("approval_ids"))
            )
        )
    ]


def _remove_matching_final_selection(
    manifest: dict[str, Any],
    *,
    candidate_id: str,
    approval_id: str,
    output_role: str,
) -> None:
    manifest["final_selected_artifacts"] = [
        item
        for item in _dict_items(manifest.get("final_selected_artifacts"))
        if not (
            _optional_string(item.get("candidate_id")) == candidate_id
            and _optional_string(item.get("approval_id")) == approval_id
            and _optional_string(item.get("output_role")) == output_role
        )
    ]


def _remove_candidate_from_status_lists(manifest: dict[str, Any], *, candidate_id: str, approval_id: str) -> None:
    for key in ("pending_candidates", "unsupported_candidates"):
        manifest[key] = [
            item
            for item in _dict_items(manifest.get(key))
            if not (
                _optional_string(item.get("candidate_id")) == candidate_id
                or _optional_string(item.get("approval_id")) == approval_id
                or approval_id in _string_list(item.get("approval_ids"))
            )
        ]


def _refresh_manifest_derived_fields(manifest: dict[str, Any]) -> None:
    manifest["schema_version"] = SCHEMA_VERSION
    manifest["generated_at"] = _utc_now_iso()
    baseline = manifest.get("baseline_output") if isinstance(manifest.get("baseline_output"), dict) else {}
    candidates = _dict_items(manifest.get("candidate_outputs"))
    selected = _dict_items(manifest.get("final_selected_artifacts"))
    comparisons = _dict_items(manifest.get("comparison_reports"))
    rejected = _dict_items(manifest.get("rejected_candidates"))
    pending = _dict_items(manifest.get("pending_candidates"))
    unsupported = _dict_items(manifest.get("unsupported_candidates"))
    resolved_noop = _dict_items(manifest.get("resolved_noop_candidates"))
    warnings = [item for item in manifest.get("warnings", []) if isinstance(item, str)]
    manifest["baseline_output"] = baseline
    manifest["candidate_outputs"] = candidates
    manifest["final_selected_artifacts"] = selected
    manifest["comparison_reports"] = comparisons
    manifest["rejected_candidates"] = rejected
    manifest["pending_candidates"] = pending
    manifest["unsupported_candidates"] = unsupported
    manifest["resolved_noop_candidates"] = resolved_noop
    manifest["warnings"] = warnings
    manifest["videos"] = _media_refs([baseline, *candidates, *selected], media_type="video")
    manifest["clips"] = _media_refs([baseline, *candidates, *selected], media_type="clip")
    manifest["summary"] = {
        "candidate_output_count": len(candidates),
        "final_artifact_count": len(selected),
        "rejected_candidate_count": len(rejected),
        "pending_candidate_count": len(pending),
        "unsupported_candidate_count": len(unsupported),
        "resolved_noop_candidate_count": len(resolved_noop),
        "comparison_counts_by_problem_type": _counts_by_key(comparisons, "problem_type"),
        "comparison_counts_by_status": _counts_by_key(comparisons, "status"),
        "warning_count": len(warnings),
        "operator_decision_count": len(_dict_items(manifest.get("operator_decisions"))),
    }


def _find_by_decision_id(items: list[Any], decision_id: str) -> dict[str, Any] | None:
    for item in items:
        if isinstance(item, dict) and item.get("decision_id") == decision_id:
            return item
    return None


def _append_unique_mapping(items: list[Any], value: dict[str, Any], *, key_fields: tuple[str, ...]) -> None:
    value_key = tuple(value.get(field) for field in key_fields)
    for item in items:
        if isinstance(item, dict) and tuple(item.get(field) for field in key_fields) == value_key:
            return
    items.append(value)


def _required_identifier(value: Any, field_name: str) -> str:
    result = _optional_string(value)
    if result is None or "/" in result or "\\" in result or ".." in result:
        raise ValueError(f"{field_name} must be a safe identifier")
    return result


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _dict_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _relative_output_path(output_dir: Path, value: str, *, field_name: str) -> str:
    resolved = _contained_output_path(output_dir, value, field_name=field_name)
    return resolved.relative_to(Path(output_dir).resolve()).as_posix()


def _contained_output_path(output_dir: Path, value: str, *, field_name: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field_name} must not be empty")
    normalized_raw = raw.replace("\\", "/")
    path = Path(normalized_raw)
    if ":" in normalized_raw and not path.is_absolute():
        raise ValueError(f"{field_name} contains an unsafe path")
    if ".." in path.parts:
        raise ValueError(f"{field_name} contains path traversal")
    output_root = Path(output_dir).resolve()
    resolved = path.resolve() if path.is_absolute() else (output_root / path).resolve()
    if not _is_relative_to(resolved, output_root):
        raise ValueError(f"{field_name} is outside output_dir")
    return resolved


def _normalized_path_text(value: str) -> str:
    return Path(value.replace("\\", "/")).as_posix()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _rejection(
    candidate_id: str | None,
    reason: str,
    artifact: dict[str, Any],
    comparison_status: str,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "reason": reason,
        "comparison_status": comparison_status,
        "artifact": artifact,
    }


def _media_refs(artifacts: list[dict[str, Any]], *, media_type: str) -> list[dict[str, Any]]:
    return [artifact for artifact in artifacts if artifact.get("type") == media_type]


def _json_ready(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    return value


def _safe_manifest_name(value: str) -> str:
    try:
        return safe_json_file_name(value)
    except ValueError as exc:
        raise ValueError("manifest name must be a simple file name") from exc


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
