from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from football_tracking.ai_candidate_comparison import CANDIDATE_STATUSES, safe_json_file_name

REGISTRY_REPORT_NAME = "ai_candidate_registry.json"
SCHEMA_VERSION = "1.0"
PROBLEM_TYPES = ("missing_ball", "noise", "follow_cam", "highlight")
PROMOTION_STATUSES = ("not_promoted", "promoted", "rejected", "pending_confirmation")
STATUS_RANK = {"pass": 0, "warn": 1, "unavailable": 2, "fail": 3}


def normalize_candidate_record(record: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise TypeError("candidate record must be a mapping")

    candidate_id = _required_string(record.get("candidate_id"), "candidate_id")
    problem_type = _required_string(record.get("problem_type"), "problem_type")
    if problem_type not in PROBLEM_TYPES:
        raise ValueError(f"problem_type must be one of {PROBLEM_TYPES}: {problem_type}")

    comparison_status = _required_string(record.get("comparison_status"), "comparison_status")
    if comparison_status not in CANDIDATE_STATUSES:
        raise ValueError(f"comparison_status must be one of {CANDIDATE_STATUSES}: {comparison_status}")

    promotion_status = _optional_string(record.get("promotion_status")) or "not_promoted"
    if promotion_status not in PROMOTION_STATUSES:
        raise ValueError(f"promotion_status must be one of {PROMOTION_STATUSES}: {promotion_status}")

    comparison_report = _optional_string(record.get("comparison_report"))
    if comparison_report is not None:
        comparison_report = safe_json_file_name(comparison_report)

    approval_id = _optional_string(record.get("approval_id"))
    if approval_id is not None:
        _validate_identifier(approval_id, "approval_id")
    consumed_approval_ids = _deduped_id_list(record.get("consumed_approval_ids"), "consumed_approval_ids")
    if approval_id is not None and approval_id not in consumed_approval_ids:
        consumed_approval_ids = [approval_id, *consumed_approval_ids]

    return {
        "candidate_id": candidate_id,
        "approval_id": approval_id,
        "problem_type": problem_type,
        "baseline_dir": _optional_path_string(record.get("baseline_dir")),
        "candidate_dir": _optional_path_string(record.get("candidate_dir")),
        "candidate_artifacts": _safe_relative_path_list(record.get("candidate_artifacts"), "candidate_artifacts"),
        "comparison_report": comparison_report,
        "comparison_status": comparison_status,
        "promotion_status": promotion_status,
        "consumed_approval_ids": consumed_approval_ids,
        "warnings": _string_list(record.get("warnings", []), "warnings"),
    }


def build_candidate_registry(
    output_dir: Path,
    records: list[dict[str, Any]] | None = None,
    comparison_reports: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    candidate_records = [normalize_candidate_record(record) for record in records or []]
    candidate_records.extend(
        normalize_candidate_record(_record_from_comparison_report(report, output_dir=output_dir))
        for report in comparison_reports or []
    )
    _validate_unique_candidates(candidate_records)
    summary = _summary(candidate_records)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "output_dir": str(output_dir),
        "summary": summary,
        "candidates": candidate_records,
    }


def write_candidate_registry(
    output_dir: Path,
    records: list[dict[str, Any]] | None = None,
    comparison_reports: list[dict[str, Any]] | None = None,
    report_name: str = REGISTRY_REPORT_NAME,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    registry = build_candidate_registry(output_dir, records=records, comparison_reports=comparison_reports)
    path = output_dir / safe_json_file_name(report_name)
    _write_json(path, registry)
    return registry


def load_candidate_registry(path_or_dir: Path) -> dict[str, Any]:
    path = Path(path_or_dir)
    if path.is_dir():
        path = path / REGISTRY_REPORT_NAME
    if not path.exists():
        return _unavailable_registry(path, artifact_status="missing")
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _unavailable_registry(path, artifact_status="corrupt", error=str(exc))
    if not isinstance(loaded, dict):
        return _unavailable_registry(path, artifact_status="invalid", error="registry payload must be an object")
    try:
        candidates = loaded.get("candidates")
        if not isinstance(candidates, list):
            raise ValueError("registry candidates must be a list")
        normalized_candidates = [normalize_candidate_record(candidate) for candidate in candidates]
        _validate_unique_candidates(normalized_candidates)
    except (TypeError, ValueError) as exc:
        return _unavailable_registry(path, artifact_status="invalid", error=str(exc))
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": loaded.get("generated_at") if isinstance(loaded.get("generated_at"), str) else _utc_now_iso(),
        "output_dir": loaded.get("output_dir") if isinstance(loaded.get("output_dir"), str) else None,
        "path": str(path),
        "artifact_status": "loaded",
        "summary": _summary(normalized_candidates),
        "candidates": normalized_candidates,
    }


def _record_from_comparison_report(report: dict[str, Any], *, output_dir: Path) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise TypeError("comparison report must be a mapping")
    baseline = report.get("baseline") if isinstance(report.get("baseline"), dict) else {}
    candidate = report.get("candidate") if isinstance(report.get("candidate"), dict) else {}
    approval = report.get("approval") if isinstance(report.get("approval"), dict) else {}
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}

    approval_id = report.get("approval_id") or approval.get("approval_id")
    candidate_path = candidate.get("path")
    return {
        "candidate_id": report.get("candidate_id") or candidate.get("id") or candidate.get("candidate_id"),
        "approval_id": approval_id,
        "problem_type": report.get("problem_type"),
        "baseline_dir": report.get("baseline_dir") or _parent_dir(baseline.get("path")),
        "candidate_dir": report.get("candidate_dir") or _parent_dir(candidate_path),
        "candidate_artifacts": _candidate_artifacts_from_report(report, candidate_path, output_dir=output_dir),
        "comparison_report": report.get("comparison_report"),
        "comparison_status": report.get("comparison_status") or summary.get("status") or report.get("status"),
        "promotion_status": report.get("promotion_status") or "not_promoted",
        "consumed_approval_ids": report.get("consumed_approval_ids") or ([approval_id] if isinstance(approval_id, str) else []),
        "warnings": report.get("warnings", []),
    }


def _validate_unique_candidates(records: list[dict[str, Any]]) -> None:
    seen_candidates: set[str] = set()
    approval_owner: dict[str, str] = {}
    for record in records:
        candidate_id = str(record["candidate_id"])
        if candidate_id in seen_candidates:
            raise ValueError(f"duplicate candidate_id in registry: {candidate_id}")
        seen_candidates.add(candidate_id)
        for approval_id in record["consumed_approval_ids"]:
            owner = approval_owner.get(approval_id)
            if owner is not None and owner != candidate_id:
                raise ValueError(f"approval_id consumed by multiple candidates: {approval_id}")
            approval_owner[approval_id] = candidate_id


def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    counts_by_problem_type = {problem_type: 0 for problem_type in PROBLEM_TYPES}
    counts_by_comparison_status = {status: 0 for status in CANDIDATE_STATUSES}
    warning_count = 0
    statuses: list[str] = []
    for record in records:
        counts_by_problem_type[record["problem_type"]] += 1
        comparison_status = record["comparison_status"]
        counts_by_comparison_status[comparison_status] += 1
        statuses.append(comparison_status)
        warning_count += len(record.get("warnings", []))
    return {
        "status": _worst_status(statuses),
        "candidate_count": len(records),
        "counts_by_problem_type": counts_by_problem_type,
        "counts_by_comparison_status": counts_by_comparison_status,
        "warning_count": warning_count,
    }


def _unavailable_registry(path: Path, *, artifact_status: str, error: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "path": str(path),
        "artifact_status": artifact_status,
        "summary": {
            "status": "unavailable",
            "candidate_count": 0,
            "counts_by_problem_type": {problem_type: 0 for problem_type in PROBLEM_TYPES},
            "counts_by_comparison_status": {status: 0 for status in CANDIDATE_STATUSES},
            "warning_count": 1,
        },
        "candidates": [],
    }
    if error is not None:
        payload["error"] = error
    return payload


def _worst_status(statuses: list[str]) -> str:
    if not statuses:
        return "pass"
    return max(statuses, key=lambda status: STATUS_RANK[status])


def _required_string(value: Any, field_name: str) -> str:
    result = _optional_string(value)
    if result is None:
        raise ValueError(f"{field_name} must be a non-empty string")
    _validate_identifier(result, field_name)
    return result


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _optional_path_string(value: Any) -> str | None:
    if isinstance(value, Path):
        return str(value)
    return _optional_string(value)


def _string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name} must be a list of strings")
        result.append(item.strip())
    return result


def _deduped_id_list(value: Any, field_name: str) -> list[str]:
    result: list[str] = []
    for item in _string_list(value, field_name):
        _validate_identifier(item, field_name)
        if item not in result:
            result.append(item)
    return result


def _safe_relative_path_list(value: Any, field_name: str) -> list[str]:
    return [_safe_relative_path(item, field_name) for item in _string_list(value, field_name)]


def _safe_relative_path(value: str, field_name: str) -> str:
    path = Path(value)
    if path.is_absolute() or path.drive or ".." in path.parts or "\\" in value:
        raise ValueError(f"{field_name} must contain safe relative paths")
    return value.strip()


def _validate_identifier(value: str, field_name: str) -> None:
    if "/" in value or "\\" in value or ".." in value:
        raise ValueError(f"{field_name} must be a safe identifier")


def _candidate_artifacts_from_report(report: dict[str, Any], candidate_path: Any, *, output_dir: Path) -> list[str]:
    raw_artifacts = report.get("candidate_artifacts")
    if isinstance(raw_artifacts, list):
        return [_artifact_path_from_report(item, output_dir=output_dir) for item in raw_artifacts]
    if isinstance(candidate_path, str):
        return [_artifact_path_from_report(candidate_path, output_dir=output_dir)]
    return []


def _artifact_path_from_report(value: Any, *, output_dir: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("candidate_artifacts must be a list of strings")
    path = Path(value)
    if path.is_absolute():
        try:
            return str(path.resolve().relative_to(Path(output_dir).resolve()))
        except ValueError as exc:
            raise ValueError("candidate_artifacts must stay under output_dir") from exc
    return value.strip()


def _parent_dir(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parent = Path(value).parent
    if str(parent) == ".":
        return None
    return str(parent)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
