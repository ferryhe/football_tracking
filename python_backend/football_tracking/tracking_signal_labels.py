from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
TRACKING_SIGNAL_LABELS_REPORT_NAME = "noise_interference_labels.json"

MATCH_BALL_STATES = (
    "confirmed_match_ball",
    "probable_match_ball",
    "occluded",
    "not_visible",
    "off_frame",
    "not_match_ball",
    "unknown",
    "ambiguous",
)
INTERFERENCE_CATEGORIES = (
    "none",
    "player_body",
    "field_background",
    "extra_ball",
    "media_roi",
    "tracking_dynamics",
    "detector_artifact",
    "camera_motion",
    "unknown",
)
INTERFERENCE_SUBTYPES = (
    "none",
    "foot",
    "shoe",
    "head",
    "hand_arm",
    "uniform_logo",
    "leg",
    "field_line",
    "field_spot",
    "goal_net_post",
    "advertising_board",
    "spectator",
    "sideline_equipment",
    "same_pitch_extra_ball",
    "adjacent_field_ball",
    "tile_duplicate",
    "short_false_tracklet",
    "shadow",
    "reflection",
    "motion_blur",
    "lost_gap",
    "large_jump_after_reacquire",
    "roi_empty_turf",
    "empty_turf_roi",
    "candidate_elsewhere",
    "coordinate_mapping_suspect",
    "unknown",
)
ACTION_NAMES = ("localize_ball_roi", "reject_noise")

MATCH_BALL_STATE_ALIASES = {
    "confirmed": "confirmed_match_ball",
    "probable": "probable_match_ball",
    "ball_occluded": "occluded",
    "ball_not_visible": "not_visible",
    "ball_off_frame": "off_frame",
}
INTERFERENCE_CATEGORY_ALIASES = {
    "player_equipment": "player_body",
    "field_marking": "field_background",
    "shadow": "field_background",
    "reflection": "field_background",
    "camera_artifact": "detector_artifact",
    "other": "unknown",
    "ambiguous": "unknown",
}
INTERFERENCE_SUBTYPE_ALIASES = {
    "boot": "shoe",
    "jersey": "uniform_logo",
    "paint": "field_line",
    "ambiguous": "unknown",
    "other": "unknown",
}
_WARNING_ALIASES = {"ambiguous", "other"}
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_UNSAFE_REPORT_NAME_CHARS = set('<>:"/\\|?*')

_FAIL_CLOSED_VALUES = {
    "unknown",
    "ambiguous",
    "roi_empty_turf",
    "empty_turf_roi",
    "candidate_elsewhere",
    "coordinate_mapping_suspect",
}
_CLEAR_INTERFERENCE_CATEGORIES = set(INTERFERENCE_CATEGORIES) - {"none", "unknown"}
_CLEAR_INTERFERENCE_SUBTYPES = set(INTERFERENCE_SUBTYPES) - {
    "none",
    "unknown",
    "roi_empty_turf",
    "empty_turf_roi",
    "candidate_elsewhere",
    "coordinate_mapping_suspect",
}


def normalize_tracking_signal_label(label: dict[str, Any]) -> dict[str, Any]:
    raw = label if isinstance(label, dict) else {}
    validation_errors = _string_list(raw.get("validation_errors"))

    match_ball_state = _enum_value(
        raw.get("match_ball_state"),
        MATCH_BALL_STATES,
        "match_ball_state",
        validation_errors,
        aliases=MATCH_BALL_STATE_ALIASES,
    )
    interference_category = _enum_value(
        raw.get("interference_category"),
        INTERFERENCE_CATEGORIES,
        "interference_category",
        validation_errors,
        aliases=INTERFERENCE_CATEGORY_ALIASES,
    )
    interference_subtype = _enum_value(
        raw.get("interference_subtype"),
        INTERFERENCE_SUBTYPES,
        "interference_subtype",
        validation_errors,
        aliases=INTERFERENCE_SUBTYPE_ALIASES,
    )
    evidence = _evidence_list(raw.get("evidence"), validation_errors)

    normalized = {
        "label_id": _optional_string(raw.get("label_id")),
        "candidate_id": _optional_string(raw.get("candidate_id")),
        "match_ball_state": match_ball_state,
        "interference_category": interference_category,
        "interference_subtype": interference_subtype,
        "evidence": evidence,
    }
    if validation_errors:
        normalized["validation_errors"] = validation_errors
    return normalized


def build_tracking_signal_labels(output_dir: Path, labels: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    normalized_labels = [normalize_tracking_signal_label(label) for label in labels or []]
    validation_errors = _label_validation_errors(normalized_labels)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "output_dir": str(Path(output_dir)),
        "summary": _summary(normalized_labels, validation_errors),
        "labels": normalized_labels,
        "validation_errors": validation_errors,
    }


def write_tracking_signal_labels(
    output_dir: Path,
    labels: list[dict[str, Any]] | None = None,
    *,
    report_name: str = TRACKING_SIGNAL_LABELS_REPORT_NAME,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    payload = build_tracking_signal_labels(output_dir, labels=labels)
    _write_json(output_dir / _safe_report_name(report_name), payload)
    return payload


def load_tracking_signal_labels(path_or_dir: Path) -> dict[str, Any]:
    path = Path(path_or_dir)
    if path.is_dir():
        path = path / TRACKING_SIGNAL_LABELS_REPORT_NAME
    if not path.exists():
        return _empty_payload(path, artifact_status="missing")
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        payload = _empty_payload(path, artifact_status="invalid")
        payload["validation_errors"] = [f"artifact: {exc}"]
        payload["summary"] = _summary([], payload["validation_errors"])
        return payload
    if not isinstance(loaded, dict):
        payload = _empty_payload(path, artifact_status="invalid")
        payload["validation_errors"] = ["artifact payload must be an object"]
        payload["summary"] = _summary([], payload["validation_errors"])
        return payload

    validation_errors: list[str] = []
    raw_labels = loaded.get("labels")
    normalized_labels: list[dict[str, Any]] = []
    if raw_labels is None:
        raw_labels = []
    if not isinstance(raw_labels, list):
        validation_errors.append("labels must be a list")
        raw_labels = []
    for index, label in enumerate(raw_labels):
        if not isinstance(label, dict):
            validation_errors.append(f"labels[{index}]: label must be an object")
            continue
        normalized_labels.append(normalize_tracking_signal_label(label))
    validation_errors = _label_validation_errors(normalized_labels)
    if raw_labels and isinstance(raw_labels, list):
        for index, label in enumerate(raw_labels):
            if not isinstance(label, dict):
                validation_errors.append(f"labels[{index}]: label must be an object")
    elif loaded.get("labels") is not None and not isinstance(loaded.get("labels"), list):
        validation_errors.append("labels must be a list")
    artifact_errors = _string_list(loaded.get("validation_errors"))
    for error in artifact_errors:
        if error not in validation_errors:
            validation_errors.append(error)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": loaded.get("generated_at") if isinstance(loaded.get("generated_at"), str) else None,
        "output_dir": loaded.get("output_dir") if isinstance(loaded.get("output_dir"), str) else None,
        "path": str(path),
        "artifact_status": "loaded",
        "summary": _summary(normalized_labels, validation_errors),
        "labels": normalized_labels,
        "validation_errors": validation_errors,
    }


def action_eligibility(label: dict[str, Any], action: str) -> dict[str, Any]:
    normalized = normalize_tracking_signal_label(label)
    blocking_reasons = _blocking_reasons(normalized)
    if action not in ACTION_NAMES:
        blocking_reasons.append("unsupported_action")

    executable = False
    if not blocking_reasons and action == "localize_ball_roi":
        executable = normalized["match_ball_state"] in {"confirmed_match_ball", "probable_match_ball"}
    elif not blocking_reasons and action == "reject_noise":
        if normalized["match_ball_state"] in {"confirmed_match_ball", "probable_match_ball"}:
            blocking_reasons.append("match_ball_not_rejectable")
        else:
            executable = (
                normalized["match_ball_state"] == "not_match_ball"
                or normalized["interference_category"] in _CLEAR_INTERFERENCE_CATEGORIES
                or normalized["interference_subtype"] in _CLEAR_INTERFERENCE_SUBTYPES
            )

    if not executable and not blocking_reasons:
        blocking_reasons.append("insufficient_signal")
    return {
        "action": action,
        "mode": "execute" if executable else "review_only",
        "executable": executable,
        "blocking_reasons": _deduped(blocking_reasons),
    }


def _blocking_reasons(label: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if label.get("validation_errors"):
        reasons.append("validation_errors")
    for field in ("match_ball_state", "interference_category", "interference_subtype"):
        value = str(label.get(field) or "unknown")
        if value in _FAIL_CLOSED_VALUES:
            reasons.append(value)
    return _deduped(reasons)


def _summary(labels: list[dict[str, Any]], validation_errors: list[str]) -> dict[str, Any]:
    return {
        "status": _summary_status(labels, validation_errors),
        "label_count": len(labels),
        "validation_error_count": len(validation_errors),
        "counts_by_match_ball_state": _counts_by_key(labels, "match_ball_state"),
        "counts_by_interference_category": _counts_by_key(labels, "interference_category"),
    }


def _summary_status(labels: list[dict[str, Any]], validation_errors: list[str]) -> str:
    if validation_errors:
        return "warn"
    if not labels:
        return "empty"
    return "ok"


def _empty_payload(path: Path, *, artifact_status: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": None,
        "path": str(path),
        "artifact_status": artifact_status,
        "summary": _summary([], []),
        "labels": [],
        "validation_errors": [],
    }


def _enum_value(
    value: Any,
    allowed: tuple[str, ...],
    field_name: str,
    validation_errors: list[str],
    *,
    aliases: dict[str, str] | None = None,
) -> str:
    text = _optional_string(value)
    if text in (aliases or {}):
        if text in _WARNING_ALIASES:
            validation_errors.append(f"{field_name}: deprecated enum {text} normalized to {(aliases or {})[text]}")
        return (aliases or {})[text]
    if text in allowed:
        return text
    if text is None:
        validation_errors.append(f"{field_name}: missing")
    else:
        validation_errors.append(f"{field_name}: unknown enum {text}")
    return "unknown"


def _evidence_list(value: Any, validation_errors: list[str]) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        validation_errors.append("evidence: must be a list")
        return []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if isinstance(item, dict):
            ready, keep = _json_ready(item, validation_errors, path=f"evidence[{index}]")
            if isinstance(ready, dict):
                result.append(ready)
            elif not keep:
                validation_errors.append(f"evidence[{index}]: entry contains no serializable fields")
        else:
            validation_errors.append("evidence: entries must be objects")
    return result


def _label_validation_errors(labels: list[dict[str, Any]]) -> list[str]:
    result: list[str] = []
    for label in labels:
        for error in label.get("validation_errors", []):
            if isinstance(error, str) and error not in result:
                result.append(error)
    return result


def _counts_by_key(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = item.get(key)
        if not isinstance(value, str) or not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _deduped(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _json_ready(value: Any, validation_errors: list[str], *, path: str) -> tuple[Any, bool]:
    if value is None or isinstance(value, str | bool | int):
        return value, True
    if isinstance(value, float):
        if math.isfinite(value):
            return value, True
        validation_errors.append(f"{path}: non-finite float is not JSON serializable")
        return None, False
    if isinstance(value, Path):
        return str(value), True
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            child_path = f"{path}.{key}"
            ready, keep = _json_ready(item, validation_errors, path=child_path)
            if keep:
                result[str(key)] = ready
        return result, True
    if isinstance(value, list):
        result = []
        for index, item in enumerate(value):
            ready, keep = _json_ready(item, validation_errors, path=f"{path}[{index}]")
            if keep:
                result.append(ready)
        return result, True
    if isinstance(value, tuple):
        result = []
        for index, item in enumerate(value):
            ready, keep = _json_ready(item, validation_errors, path=f"{path}[{index}]")
            if keep:
                result.append(ready)
        return result, True
    validation_errors.append(f"{path}: value is not JSON serializable")
    return None, False


def _safe_report_name(value: str) -> str:
    name = Path(value).name
    stem = Path(value).stem.upper()
    if (
        name != value
        or Path(value).suffix.lower() != ".json"
        or any(char in value for char in _UNSAFE_REPORT_NAME_CHARS)
        or stem in _WINDOWS_RESERVED_NAMES
    ):
        raise ValueError("report_name must be a simple JSON file name")
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
