from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from football_tracking.ai_candidate_comparison import (
    ARTIFACT_ROLES,
    CANDIDATE_STATUSES,
    SCHEMA_VERSION,
    comparison_payload_status,
    safe_json_file_name,
)

FINAL_ARTIFACT_MANIFEST_NAME = "final_ai_improvement_artifact_manifest.json"
STATUS_RANK = {"pass": 0, "warn": 1, "unavailable": 2, "fail": 3}


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
    return {
        "path": result.get("path") or result.get("report_path"),
        "problem_type": result.get("problem_type"),
        "candidate_id": result.get("candidate_id") or candidate.get("id") or candidate.get("candidate_id"),
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


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
