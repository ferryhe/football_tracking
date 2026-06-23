from __future__ import annotations

import json
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
ARTIFACT_ROLES = ("baseline", "candidate", "final")
CANDIDATE_STATUSES = ("pass", "warn", "fail", "unavailable")
STATUS_RANK = {"pass": 0, "warn": 1, "unavailable": 2, "fail": 3}
_SAFE_FILENAME_CHARS = set(string.ascii_letters + string.digits + "-_.")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def build_candidate_comparison(
    *,
    problem_type: str,
    baseline: dict[str, Any] | str | Path,
    candidate: dict[str, Any] | str | Path,
    checks: list[dict[str, Any]],
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_checks = [_normalize_check(check) for check in checks]
    summary = summarize_candidate_checks(normalized_checks)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "problem_type": problem_type,
        "baseline": _artifact_ref(baseline, role="baseline"),
        "candidate": _artifact_ref(candidate, role="candidate"),
        "approval": approval,
        "summary": summary,
        "checks": normalized_checks,
    }


def write_candidate_comparison_report(
    output_dir: Path,
    payload: dict[str, Any],
    *,
    name: str | None = None,
) -> Path:
    output_dir = Path(output_dir)
    report_name = safe_json_file_name(
        name or f"{_safe_name(str(payload.get('problem_type') or 'candidate'))}_comparison.json"
    )
    path = output_dir / report_name
    _write_json(path, payload)
    return path


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


def _normalize_check(check: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(check, dict):
        raise TypeError("comparison checks must be mappings")
    result = _json_ready(check)
    status = result.get("status")
    if status not in CANDIDATE_STATUSES:
        raise ValueError(f"comparison check status must be one of {CANDIDATE_STATUSES}: {status}")
    return result


def summarize_candidate_checks(checks: Any) -> dict[str, Any]:
    if not isinstance(checks, list):
        raise TypeError("comparison checks must be a list")
    if not checks:
        raise ValueError("candidate comparison requires at least one check")
    failed_count = sum(1 for check in checks if check.get("status") == "fail")
    warning_count = sum(1 for check in checks if check.get("status") == "warn")
    unavailable_count = sum(1 for check in checks if check.get("status") == "unavailable")
    passed_count = sum(1 for check in checks if check.get("status") == "pass")
    if failed_count:
        status = "fail"
    elif unavailable_count:
        status = "unavailable"
    elif warning_count:
        status = "warn"
    else:
        status = "pass"
    return {
        "status": status,
        "check_count": len(checks),
        "passed_check_count": passed_count,
        "failed_check_count": failed_count,
        "warning_count": warning_count,
        "unavailable_count": unavailable_count,
        "requires_human_confirmation": status in {"warn", "unavailable"},
        "promotion_eligible": status == "pass",
    }


def comparison_payload_status(payload: dict[str, Any]) -> dict[str, Any]:
    checks = payload.get("checks")
    if not isinstance(checks, list) or not checks:
        check_summary = {
            "status": "unavailable",
            "failed_check_count": 0,
            "warning_count": 0,
            "unavailable_count": 1,
            "artifact_status": "invalid_checks",
        }
    else:
        normalized_checks: list[dict[str, Any]] = []
        artifact_status = "loaded"
        for check in checks:
            if not isinstance(check, dict):
                artifact_status = "invalid_checks"
                break
            try:
                normalized_checks.append(_normalize_check(check))
            except ValueError:
                artifact_status = "invalid_checks"
                break
        if artifact_status != "loaded":
            observed_statuses = [
                check.get("status")
                for check in checks
                if isinstance(check, dict) and check.get("status") in CANDIDATE_STATUSES
            ]
            status = _worst_status([*observed_statuses, "unavailable"])
            check_summary = {
                "status": status,
                "failed_check_count": sum(1 for status_value in observed_statuses if status_value == "fail"),
                "warning_count": sum(1 for status_value in observed_statuses if status_value == "warn"),
                "unavailable_count": sum(1 for status_value in observed_statuses if status_value == "unavailable") + 1,
                "artifact_status": artifact_status,
            }
        else:
            summary = summarize_candidate_checks(normalized_checks)
            check_summary = {
                "status": summary["status"],
                "failed_check_count": summary["failed_check_count"],
                "warning_count": summary["warning_count"],
                "unavailable_count": summary["unavailable_count"],
                "artifact_status": "loaded",
            }

    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    summary_status = summary.get("status") or payload.get("status")
    artifact_status = str(check_summary["artifact_status"])
    status = str(check_summary["status"])
    if artifact_status != "loaded":
        if summary_status in CANDIDATE_STATUSES:
            status = _worst_status([status, str(summary_status)])
        else:
            status = _worst_status([status, "unavailable"])
    elif summary_status not in CANDIDATE_STATUSES:
        status = _worst_status([status, "unavailable"])
        artifact_status = "invalid_summary"
    elif summary_status != status:
        status = _worst_status([status, str(summary_status)])
        artifact_status = "summary_check_mismatch"
    return {
        "status": status,
        "failed_check_count": check_summary["failed_check_count"],
        "warning_count": check_summary["warning_count"],
        "unavailable_count": check_summary["unavailable_count"],
        "artifact_status": artifact_status,
    }


def _worst_status(statuses: list[Any]) -> str:
    valid_statuses = [str(status) for status in statuses if status in CANDIDATE_STATUSES]
    if not valid_statuses:
        return "unavailable"
    return max(valid_statuses, key=lambda status: STATUS_RANK[status])


def _summary(checks: list[dict[str, Any]]) -> dict[str, Any]:
    return summarize_candidate_checks(checks)


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


def _safe_name(value: str) -> str:
    safe = "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in value.strip())
    return safe or "candidate"


def safe_json_file_name(value: str) -> str:
    path = Path(value)
    if (
        not value
        or path.is_absolute()
        or path.name != value
        or "/" in value
        or "\\" in value
        or not value.endswith(".json")
        or any(character not in _SAFE_FILENAME_CHARS for character in value)
        or path.stem.upper() in _WINDOWS_RESERVED_NAMES
    ):
        raise ValueError("report name must be a safe JSON file name")
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
