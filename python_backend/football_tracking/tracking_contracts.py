from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "2.0"
TRACKING_CONTRACT_REPORT_NAME = "tracking_contract.v2.json"

FRAME_STATUSES = ("detected", "interpolated", "unknown", "out_of_view")
LEGACY_STATUS_MAP = {
    "Detected": "detected",
    "Predicted": "interpolated",
    "Lost": "unknown",
}
CLASSIFICATION_LABELS = (
    "match_ball",
    "player_body_or_shoe",
    "field_line_or_mark",
    "sideline_or_spare_ball",
    "equipment_or_background",
    "lighting_shadow_or_blur",
    "unknown",
)
LABEL_ORIGINS = ("prelabel", "ai_confirmed", "human_confirmed")
CONFIRMED_LABEL_ORIGINS = frozenset({"ai_confirmed", "human_confirmed"})
SELECTIVE_DECISIONS = ("accept", "reject", "abstain")
_REQUIRED_LEGACY_COLUMNS = ("Frame", "X", "Y", "Confidence", "Status")


def build_tracking_contract(
    *,
    frames: list[dict[str, Any]] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    classifications: list[dict[str, Any]] | None = None,
    decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    validation_errors: list[str] = []
    normalized_frames = _normalize_records(frames, "frames", _normalize_frame, validation_errors)
    normalized_candidates = _normalize_records(candidates, "candidates", _normalize_candidate, validation_errors)
    normalized_frames = _dedupe_records(
        normalized_frames,
        field="frame_index",
        collection="frames",
        validation_errors=validation_errors,
    )
    normalized_candidates = _dedupe_records(
        normalized_candidates,
        field="candidate_id",
        collection="candidates",
        validation_errors=validation_errors,
    )
    normalized_classifications = _normalize_records(
        classifications,
        "classifications",
        _normalize_classification,
        validation_errors,
    )
    normalized_decisions = _normalize_records(decisions, "decisions", _normalize_decision, validation_errors)
    candidate_ids = {item["candidate_id"] for item in normalized_candidates}
    normalized_classifications = _filter_candidate_references(
        normalized_classifications,
        collection="classifications",
        candidate_ids=candidate_ids,
        validation_errors=validation_errors,
    )
    normalized_decisions = _filter_candidate_references(
        normalized_decisions,
        collection="decisions",
        candidate_ids=candidate_ids,
        validation_errors=validation_errors,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "summary": _summary(
            normalized_frames,
            normalized_candidates,
            normalized_classifications,
            normalized_decisions,
            validation_errors,
        ),
        "frames": normalized_frames,
        "candidates": normalized_candidates,
        "classifications": normalized_classifications,
        "decisions": normalized_decisions,
        "validation_errors": validation_errors,
    }


def write_tracking_contract(
    output_dir: Path,
    *,
    frames: list[dict[str, Any]] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    classifications: list[dict[str, Any]] | None = None,
    decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    payload = build_tracking_contract(
        frames=frames,
        candidates=candidates,
        classifications=classifications,
        decisions=decisions,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / TRACKING_CONTRACT_REPORT_NAME, payload)
    return payload


def load_tracking_contract(path_or_dir: Path) -> dict[str, Any]:
    path = Path(path_or_dir)
    if path.is_dir():
        path = path / TRACKING_CONTRACT_REPORT_NAME
    if not path.exists():
        return _empty_payload(path, artifact_status="missing")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _invalid_payload(path, f"artifact: {exc}")
    if not isinstance(raw, dict):
        return _invalid_payload(path, "artifact payload must be an object")

    collection_errors = _envelope_errors(raw)
    collections: dict[str, list[dict[str, Any]]] = {}
    for name in ("frames", "candidates", "classifications", "decisions"):
        value = raw.get(name)
        if name not in raw:
            collection_errors.append(f"{name}: required")
            value = []
        elif not isinstance(value, list):
            collection_errors.append(f"{name}: must be a list")
            value = []
        collections[name] = value
    payload = build_tracking_contract(**collections)
    if raw.get("schema_version") != SCHEMA_VERSION:
        collection_errors.insert(0, f"schema_version: expected {SCHEMA_VERSION}")
    for error in _string_list(raw.get("validation_errors")):
        if error not in collection_errors and error not in payload["validation_errors"]:
            collection_errors.append(error)
    payload["validation_errors"] = [*collection_errors, *payload["validation_errors"]]
    payload["generated_at"] = raw.get("generated_at") if isinstance(raw.get("generated_at"), str) else None
    payload["path"] = str(path)
    payload["artifact_status"] = "invalid" if payload["validation_errors"] else "loaded"
    payload["summary"] = _summary(
        payload["frames"],
        payload["candidates"],
        payload["classifications"],
        payload["decisions"],
        payload["validation_errors"],
    )
    return payload


def load_legacy_track_csv(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        payload = _empty_payload(path, artifact_status="missing")
        payload.update(
            {
                "source_format": "legacy_csv",
                "source_path": str(path),
                "legacy_columns": [],
            }
        )
        return payload

    validation_errors: list[str] = []
    frames: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            legacy_columns = list(reader.fieldnames or [])
            missing_columns = [name for name in _REQUIRED_LEGACY_COLUMNS if name not in legacy_columns]
            if missing_columns:
                validation_errors.append(f"header: missing required headers {missing_columns}")
            row_count = 0
            for index, row in enumerate(reader):
                row_count += 1
                if missing_columns:
                    continue
                status = row.get("Status")
                mapped_status = LEGACY_STATUS_MAP.get(status or "")
                if mapped_status is None:
                    validation_errors.append(f"rows[{index}].Status: unknown legacy status {status!r}")
                    continue
                candidate = {
                    "frame_index": row.get("Frame"),
                    "status": mapped_status,
                    "x": row.get("X"),
                    "y": row.get("Y"),
                    "confidence": row.get("Confidence"),
                    "source": "legacy_csv",
                    "legacy_status": status,
                    "legacy_row": dict(row),
                }
                normalized, errors = _normalize_frame(candidate)
                validation_errors.extend(f"rows[{index}].{error}" for error in errors)
                if normalized is not None:
                    frames.append(normalized)
            if row_count == 0:
                validation_errors.append("rows: no data rows")
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        payload = _invalid_payload(path, f"artifact: {exc}")
        payload.update(
            {
                "source_format": "legacy_csv",
                "source_path": str(path),
                "legacy_columns": [],
            }
        )
        return payload

    payload = build_tracking_contract(frames=frames)
    payload["source_format"] = "legacy_csv"
    payload["path"] = str(path)
    payload["source_path"] = str(path)
    payload["legacy_columns"] = legacy_columns
    contract_errors = payload["validation_errors"]
    validation_errors.extend(error for error in contract_errors if error not in validation_errors)
    payload["validation_errors"] = validation_errors
    payload["artifact_status"] = "invalid" if validation_errors else "loaded"
    payload["summary"] = _summary(payload["frames"], [], [], [], validation_errors)
    return payload


def _normalize_records(
    records: list[dict[str, Any]] | None,
    name: str,
    normalizer: Any,
    validation_errors: list[str],
) -> list[dict[str, Any]]:
    if records is None:
        return []
    if not isinstance(records, list):
        validation_errors.append(f"{name}: must be a list")
        return []
    result: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            validation_errors.append(f"{name}[{index}]: must be an object")
            continue
        normalized, errors = normalizer(record)
        validation_errors.extend(f"{name}[{index}].{error}" for error in errors)
        if normalized is not None:
            result.append(normalized)
    return result


def _envelope_errors(raw: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if "generated_at" not in raw:
        errors.append("generated_at: required")
    elif raw["generated_at"] is not None and not isinstance(raw["generated_at"], str):
        errors.append("generated_at: must be a string or null")
    if "summary" not in raw:
        errors.append("summary: required")
    elif not isinstance(raw["summary"], dict):
        errors.append("summary: must be an object")
    if "validation_errors" not in raw:
        errors.append("validation_errors: required")
    elif not isinstance(raw["validation_errors"], list):
        errors.append("validation_errors: must be a list")
    elif not all(isinstance(item, str) for item in raw["validation_errors"]):
        errors.append("validation_errors: entries must be strings")
    return errors


def _dedupe_records(
    records: list[dict[str, Any]],
    *,
    field: str,
    collection: str,
    validation_errors: list[str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for index, record in enumerate(records):
        value = record[field]
        if value in seen:
            validation_errors.append(f"{collection}[{index}].{field}: duplicate {field} {value!r}")
            continue
        seen.add(value)
        result.append(record)
    return result


def _filter_candidate_references(
    records: list[dict[str, Any]],
    *,
    collection: str,
    candidate_ids: set[str],
    validation_errors: list[str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        candidate_id = record["candidate_id"]
        if candidate_id not in candidate_ids:
            validation_errors.append(
                f"{collection}[{index}].candidate_id: references absent candidate {candidate_id!r}"
            )
            continue
        result.append(record)
    return result


def _normalize_frame(raw: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    frame_index = _nonnegative_int(raw.get("frame_index"), "frame_index", errors)
    status = raw.get("status")
    if status not in FRAME_STATUSES:
        errors.append(f"status: must be one of {FRAME_STATUSES}")
    x = _optional_finite_float(raw.get("x"), "x", errors)
    y = _optional_finite_float(raw.get("y"), "y", errors)
    if (x is None) != (y is None):
        errors.append("coordinates: x and y must both be present or both be absent")
    if status in {"detected", "interpolated"} and (x is None or y is None):
        errors.append(f"coordinates: required for status {status}")
    confidence = _optional_probability(raw.get("confidence"), "confidence", errors)
    legacy_status = raw.get("legacy_status")
    if legacy_status is not None and legacy_status not in LEGACY_STATUS_MAP:
        errors.append("legacy_status: unsupported legacy status")
    legacy_row = raw.get("legacy_row")
    if legacy_row is not None and not isinstance(legacy_row, dict):
        errors.append("legacy_row: must be an object")
    if errors:
        return None, errors

    result: dict[str, Any] = {"frame_index": frame_index, "status": status}
    if x is not None and y is not None:
        result.update({"x": x, "y": y})
    if confidence is not None:
        result["confidence"] = confidence
    for key in ("source", "reason"):
        value = _optional_string(raw.get(key))
        if value is not None:
            result[key] = value
    if legacy_status is not None:
        result["legacy_status"] = legacy_status
    if legacy_row is not None:
        result["legacy_row"] = dict(legacy_row)
    return result, []


def _normalize_candidate(raw: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    candidate_id = _required_string(raw.get("candidate_id"), "candidate_id", errors)
    frame_index = _nonnegative_int(raw.get("frame_index"), "frame_index", errors)
    bbox = raw.get("bbox")
    parsed_bbox: list[float] | None = None
    if not isinstance(bbox, list) or len(bbox) != 4:
        errors.append("bbox: must contain four coordinates")
    else:
        parsed_bbox = []
        for index, value in enumerate(bbox):
            parsed = _finite_float(value, f"bbox[{index}]", errors)
            if parsed is not None:
                parsed_bbox.append(parsed)
        if len(parsed_bbox) == 4 and (parsed_bbox[2] <= parsed_bbox[0] or parsed_bbox[3] <= parsed_bbox[1]):
            errors.append("bbox: max coordinates must exceed min coordinates")
    confidence = _probability(raw.get("confidence"), "confidence", errors)
    source = _required_string(raw.get("source"), "source", errors)
    if errors:
        return None, errors
    return {
        "candidate_id": candidate_id,
        "frame_index": frame_index,
        "bbox": parsed_bbox,
        "confidence": confidence,
        "source": source,
    }, []


def _normalize_classification(raw: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    candidate_id = _required_string(raw.get("candidate_id"), "candidate_id", errors)
    label = raw.get("label")
    if label not in CLASSIFICATION_LABELS:
        errors.append(f"label: must be one of {CLASSIFICATION_LABELS}")
    label_origin = raw.get("label_origin")
    if label_origin not in LABEL_ORIGINS:
        errors.append(f"label_origin: must be one of {LABEL_ORIGINS}")
    confidence = _optional_probability(raw.get("confidence"), "confidence", errors)
    if errors:
        return None, errors
    result: dict[str, Any] = {
        "candidate_id": candidate_id,
        "label": label,
        "label_origin": label_origin,
        "confirmed": label_origin in CONFIRMED_LABEL_ORIGINS,
    }
    if confidence is not None:
        result["confidence"] = confidence
    return result, []


def _normalize_decision(raw: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    candidate_id = _required_string(raw.get("candidate_id"), "candidate_id", errors)
    decision = raw.get("decision")
    if decision not in SELECTIVE_DECISIONS:
        errors.append(f"decision: must be one of {SELECTIVE_DECISIONS}")
    confidence = _probability(raw.get("confidence"), "confidence", errors)
    if errors:
        return None, errors
    result: dict[str, Any] = {
        "candidate_id": candidate_id,
        "decision": decision,
        "confidence": confidence,
    }
    reason = _optional_string(raw.get("reason"))
    if reason is not None:
        result["reason"] = reason
    return result, []


def _summary(
    frames: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    classifications: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    validation_errors: list[str],
) -> dict[str, Any]:
    record_count = len(frames) + len(candidates) + len(classifications) + len(decisions)
    if validation_errors:
        status = "invalid"
    elif record_count == 0:
        status = "empty"
    else:
        status = "ok"
    return {
        "status": status,
        "frame_count": len(frames),
        "candidate_count": len(candidates),
        "classification_count": len(classifications),
        "decision_count": len(decisions),
        "prelabel_count": sum(item["label_origin"] == "prelabel" for item in classifications),
        "confirmed_label_count": sum(bool(item["confirmed"]) for item in classifications),
        "validation_error_count": len(validation_errors),
    }


def _empty_payload(path: Path, *, artifact_status: str) -> dict[str, Any]:
    payload = build_tracking_contract()
    payload.update({"generated_at": None, "path": str(path), "artifact_status": artifact_status})
    return payload


def _invalid_payload(path: Path, error: str) -> dict[str, Any]:
    payload = _empty_payload(path, artifact_status="invalid")
    payload["validation_errors"] = [error]
    payload["summary"] = _summary([], [], [], [], payload["validation_errors"])
    return payload


def _required_string(value: Any, name: str, errors: list[str]) -> str | None:
    parsed = _optional_string(value)
    if parsed is None:
        errors.append(f"{name}: required")
    return parsed


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


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


def _finite_float(value: Any, name: str, errors: list[str]) -> float | None:
    if isinstance(value, bool):
        errors.append(f"{name}: must be a finite number")
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        errors.append(f"{name}: must be a finite number")
        return None
    if not math.isfinite(parsed):
        errors.append(f"{name}: must be a finite number")
        return None
    return parsed


def _optional_finite_float(value: Any, name: str, errors: list[str]) -> float | None:
    if value in (None, ""):
        return None
    return _finite_float(value, name, errors)


def _probability(value: Any, name: str, errors: list[str]) -> float | None:
    parsed = _finite_float(value, name, errors)
    if parsed is not None and not 0.0 <= parsed <= 1.0:
        errors.append(f"{name}: must be between 0 and 1")
        return None
    return parsed


def _optional_probability(value: Any, name: str, errors: list[str]) -> float | None:
    if value in (None, ""):
        return None
    return _probability(value, name, errors)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
