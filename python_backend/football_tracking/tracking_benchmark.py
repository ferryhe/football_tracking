from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
BENCHMARK_REPORT_NAME = "tracking_benchmark_report.json"

_TRUTHS = ("match_ball", "noise")
_TRUTH_ORIGINS = ("prelabel", "ai_confirmed", "human_confirmed")
_DECISIONS = ("accept", "reject", "abstain")


def build_benchmark_report(
    *,
    candidate_evaluations: list[dict[str, Any]] | None = None,
    frame_evaluations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    validation_errors: list[str] = []
    candidates = _normalize_candidates(candidate_evaluations, validation_errors)
    frames = _normalize_frames(frame_evaluations, validation_errors)
    candidates = _dedupe_evaluations(
        candidates,
        field="candidate_id",
        collection="candidate_evaluations",
        validation_errors=validation_errors,
    )
    frames = _dedupe_evaluations(
        frames,
        field="frame_index",
        collection="frame_evaluations",
        validation_errors=validation_errors,
    )
    confirmed = [item for item in candidates if item["truth_origin"] != "prelabel"]
    automated = [item for item in confirmed if item["decision"] != "abstain"]
    accepted = [item for item in confirmed if item["decision"] == "accept"]
    true_balls = [item for item in confirmed if item["truth"] == "match_ball"]
    visible = [item for item in frames if item["ball_visible"]]
    key_actions = [item for item in frames if item["key_action"]]

    metrics = {
        "auto_accepted_candidate_precision": _metric(
            sum(item["truth"] == "match_ball" for item in accepted),
            len(accepted),
        ),
        "true_ball_false_reject_rate": _metric(
            sum(item["decision"] == "reject" for item in true_balls),
            len(true_balls),
        ),
        "selective_coverage": _metric(len(automated), len(confirmed)),
        "selective_risk": _metric(sum(not _decision_is_correct(item) for item in automated), len(automated)),
        "visible_ball_in_frame": _metric(sum(item["ball_in_frame"] for item in visible), len(visible)),
        "action_coverage": _metric(sum(item["action_in_frame"] for item in key_actions), len(key_actions)),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "summary": _summary(candidates, confirmed, frames, validation_errors),
        "metrics": metrics,
        "risk_coverage_curve": _risk_coverage_curve(confirmed),
        "candidate_evaluations": candidates,
        "frame_evaluations": frames,
        "validation_errors": validation_errors,
    }


def write_benchmark_report(
    output_dir: Path,
    *,
    candidate_evaluations: list[dict[str, Any]] | None = None,
    frame_evaluations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    report = build_benchmark_report(
        candidate_evaluations=candidate_evaluations,
        frame_evaluations=frame_evaluations,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / BENCHMARK_REPORT_NAME).write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return report


def load_benchmark_report(path_or_dir: Path) -> dict[str, Any]:
    path = Path(path_or_dir)
    if path.is_dir():
        path = path / BENCHMARK_REPORT_NAME
    if not path.exists():
        return _empty_report(path, artifact_status="missing")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _invalid_report(path, f"artifact: {exc}")
    if not isinstance(raw, dict):
        return _invalid_report(path, "artifact payload must be an object")

    shape_errors = _envelope_errors(raw)
    candidates = raw.get("candidate_evaluations")
    frames = raw.get("frame_evaluations")
    if "candidate_evaluations" not in raw:
        shape_errors.append("candidate_evaluations: required")
        candidates = []
    elif not isinstance(candidates, list):
        shape_errors.append("candidate_evaluations: must be a list")
        candidates = []
    if "frame_evaluations" not in raw:
        shape_errors.append("frame_evaluations: required")
        frames = []
    elif not isinstance(frames, list):
        shape_errors.append("frame_evaluations: must be a list")
        frames = []
    report = build_benchmark_report(candidate_evaluations=candidates, frame_evaluations=frames)
    if raw.get("schema_version") != SCHEMA_VERSION:
        shape_errors.insert(0, f"schema_version: expected {SCHEMA_VERSION}")
    for error in _string_list(raw.get("validation_errors")):
        if error not in shape_errors and error not in report["validation_errors"]:
            shape_errors.append(error)
    report["validation_errors"] = [*shape_errors, *report["validation_errors"]]
    report["generated_at"] = raw.get("generated_at") if isinstance(raw.get("generated_at"), str) else None
    report["path"] = str(path)
    report["artifact_status"] = "invalid" if report["validation_errors"] else "loaded"
    report["summary"] = _summary(
        report["candidate_evaluations"],
        [item for item in report["candidate_evaluations"] if item["truth_origin"] != "prelabel"],
        report["frame_evaluations"],
        report["validation_errors"],
    )
    return report


def _normalize_candidates(
    values: list[dict[str, Any]] | None,
    validation_errors: list[str],
) -> list[dict[str, Any]]:
    if values is None:
        return []
    if not isinstance(values, list):
        validation_errors.append("candidate_evaluations: must be a list")
        return []
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(values):
        errors: list[str] = []
        if not isinstance(raw, dict):
            validation_errors.append(f"candidate_evaluations[{index}]: must be an object")
            continue
        candidate_id = _required_string(raw.get("candidate_id"), "candidate_id", errors)
        truth = raw.get("truth")
        if truth not in _TRUTHS:
            errors.append(f"truth: must be one of {_TRUTHS}")
        truth_origin = raw.get("truth_origin")
        if truth_origin not in _TRUTH_ORIGINS:
            errors.append(f"truth_origin: must be one of {_TRUTH_ORIGINS}")
        decision = raw.get("decision")
        if decision not in _DECISIONS:
            errors.append(f"decision: must be one of {_DECISIONS}")
        confidence = _probability(raw.get("confidence"), "confidence", errors)
        validation_errors.extend(f"candidate_evaluations[{index}].{error}" for error in errors)
        if not errors:
            result.append(
                {
                    "candidate_id": candidate_id,
                    "truth": truth,
                    "truth_origin": truth_origin,
                    "decision": decision,
                    "confidence": confidence,
                }
            )
    return result


def _envelope_errors(raw: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if "generated_at" not in raw:
        errors.append("generated_at: required")
    elif raw["generated_at"] is not None and not isinstance(raw["generated_at"], str):
        errors.append("generated_at: must be a string or null")
    required_types = {
        "summary": (dict, "an object"),
        "metrics": (dict, "an object"),
        "risk_coverage_curve": (list, "a list"),
        "validation_errors": (list, "a list"),
    }
    for name, (expected_type, description) in required_types.items():
        if name not in raw:
            errors.append(f"{name}: required")
        elif not isinstance(raw[name], expected_type):
            errors.append(f"{name}: must be {description}")
    persisted_errors = raw.get("validation_errors")
    if isinstance(persisted_errors, list) and not all(isinstance(item, str) for item in persisted_errors):
        errors.append("validation_errors: entries must be strings")
    return errors


def _normalize_frames(
    values: list[dict[str, Any]] | None,
    validation_errors: list[str],
) -> list[dict[str, Any]]:
    if values is None:
        return []
    if not isinstance(values, list):
        validation_errors.append("frame_evaluations: must be a list")
        return []
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(values):
        errors: list[str] = []
        if not isinstance(raw, dict):
            validation_errors.append(f"frame_evaluations[{index}]: must be an object")
            continue
        frame_index = _nonnegative_int(raw.get("frame_index"), "frame_index", errors)
        ball_visible = _required_bool(raw.get("ball_visible"), "ball_visible", errors)
        key_action = _required_bool(raw.get("key_action"), "key_action", errors)
        ball_in_frame = _required_bool(raw.get("ball_in_frame"), "ball_in_frame", errors)
        action_in_frame = _required_bool(raw.get("action_in_frame"), "action_in_frame", errors)
        validation_errors.extend(f"frame_evaluations[{index}].{error}" for error in errors)
        if not errors:
            result.append(
                {
                    "frame_index": frame_index,
                    "ball_visible": ball_visible,
                    "ball_in_frame": ball_in_frame,
                    "key_action": key_action,
                    "action_in_frame": action_in_frame,
                }
            )
    return result


def _risk_coverage_curve(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not candidates:
        return []
    ordered = sorted(candidates, key=lambda item: item["confidence"], reverse=True)
    result: list[dict[str, Any]] = []
    selected_count = 0
    error_count = 0
    index = 0
    while index < len(ordered):
        threshold = ordered[index]["confidence"]
        while index < len(ordered) and ordered[index]["confidence"] == threshold:
            item = ordered[index]
            if item["decision"] != "abstain":
                selected_count += 1
                error_count += not _decision_is_correct(item)
            index += 1
        result.append(
            {
                "threshold": threshold,
                "coverage": selected_count / len(candidates),
                "risk": error_count / selected_count if selected_count else None,
                "selected_count": selected_count,
                "evaluation_count": len(candidates),
            }
        )
    return result


def _dedupe_evaluations(
    evaluations: list[dict[str, Any]],
    *,
    field: str,
    collection: str,
    validation_errors: list[str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for index, evaluation in enumerate(evaluations):
        value = evaluation[field]
        if value in seen:
            validation_errors.append(f"{collection}[{index}].{field}: duplicate {field} {value!r}")
            continue
        seen.add(value)
        result.append(evaluation)
    return result


def _decision_is_correct(item: dict[str, Any]) -> bool:
    return (item["decision"] == "accept" and item["truth"] == "match_ball") or (
        item["decision"] == "reject" and item["truth"] == "noise"
    )


def _metric(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "value": numerator / denominator if denominator else None,
        "numerator": numerator,
        "denominator": denominator,
        "available": denominator > 0,
    }


def _summary(
    candidates: list[dict[str, Any]],
    confirmed: list[dict[str, Any]],
    frames: list[dict[str, Any]],
    validation_errors: list[str],
) -> dict[str, Any]:
    if validation_errors:
        status = "invalid"
    elif not candidates and not frames:
        status = "empty"
    else:
        status = "ok"
    return {
        "status": status,
        "candidate_evaluation_count": len(candidates),
        "confirmed_candidate_count": len(confirmed),
        "excluded_prelabel_count": len(candidates) - len(confirmed),
        "frame_evaluation_count": len(frames),
        "validation_error_count": len(validation_errors),
    }


def _empty_report(path: Path, *, artifact_status: str) -> dict[str, Any]:
    report = build_benchmark_report()
    report.update({"generated_at": None, "path": str(path), "artifact_status": artifact_status})
    return report


def _invalid_report(path: Path, error: str) -> dict[str, Any]:
    report = _empty_report(path, artifact_status="invalid")
    report["validation_errors"] = [error]
    report["summary"] = _summary([], [], [], report["validation_errors"])
    return report


def _required_string(value: Any, name: str, errors: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{name}: required")
        return None
    return value.strip()


def _nonnegative_int(value: Any, name: str, errors: list[str]) -> int | None:
    if isinstance(value, bool):
        errors.append(f"{name}: must be a non-negative integer")
        return None
    try:
        parsed_float = float(value)
        parsed = int(parsed_float)
    except (TypeError, ValueError, OverflowError):
        errors.append(f"{name}: must be a non-negative integer")
        return None
    if not math.isfinite(parsed_float) or parsed_float != parsed or parsed < 0:
        errors.append(f"{name}: must be a non-negative integer")
        return None
    return parsed


def _required_bool(value: Any, name: str, errors: list[str]) -> bool | None:
    if not isinstance(value, bool):
        errors.append(f"{name}: must be a boolean")
        return None
    return value


def _probability(value: Any, name: str, errors: list[str]) -> float | None:
    if isinstance(value, bool):
        errors.append(f"{name}: must be between 0 and 1")
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        errors.append(f"{name}: must be between 0 and 1")
        return None
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        errors.append(f"{name}: must be between 0 and 1")
        return None
    return parsed


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
