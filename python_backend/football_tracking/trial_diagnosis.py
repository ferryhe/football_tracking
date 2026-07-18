from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TRIAL_SIGNAL_GATE_SCHEMA_VERSION = "2.0"
TRIAL_STAGE_COUNTER_SCHEMA_VERSION = "2.0"
DETECTOR_STAGE_EVIDENCE_SCHEMA_VERSION = "1.0"
TRIAL_TUNING_SCHEMA_VERSION = "1.0"
PRODUCTION_TUNING_PATCH_SCHEMA_VERSION = "1.0"
TRACKING_CONTRACT_NAME = "tracking_contract.v2.json"

TRIAL_SIGNAL_THRESHOLD_PROFILE: dict[str, Any] = {
    "profile_id": "trial-signal-conservative",
    "version": "1.1",
    "algorithm_version": "trial-signal-gate-v2.1",
    "matching_rules": {
        "stage_counter_reconciliation": "all_required_counters_present_and_reconciled",
        "track_metric_scope": "raw_and_cleaned_when_postprocess_enabled",
        "follow_cam_scope": "motion_and_action_retention_when_enabled",
        "required_visual_evidence": [
            "wide_context",
            "tight_crop",
            "follow_cam_when_enabled",
            "scale_strata",
            "lighting_strata",
            "attack_transition_windows",
        ],
        "required_integrity": ["media_integrity", "identity_binding"],
        "acceptance_contract": "server_verified_bundle_required",
    },
    "thresholds": {
        "minimum_detected_ratio": 0.50,
        "maximum_predicted_ratio": 0.35,
        "maximum_lost_ratio": 0.25,
        "maximum_longest_lost_streak": 30,
        "maximum_false_positive_islands_per_100_frames": 8.0,
        "maximum_suspicious_tracklet_ratio": 0.35,
        "maximum_step_px": 600.0,
        "maximum_follow_cam_pan_step_px": 90.0,
        "maximum_follow_cam_pan_accel_px": 120.0,
        "maximum_follow_cam_zoom_step_ratio": 0.10,
        "maximum_ai_review_triggers_per_100_frames": 10.0,
        "maximum_event_candidates_per_100_frames": 25.0,
    },
}


_TUNING_CONTROLS: tuple[dict[str, Any], ...] = (
    {
        "path": "detector.allowed_labels",
        "section": "detector",
        "kind": "multi_select",
        "options": ["sports ball", "ball"],
        "runtime_impact": "low",
    },
    {
        "path": "detector.inference_mode",
        "section": "detector",
        "kind": "select",
        "options": ["direct_full_frame", "sahi"],
        "runtime_impact": "high",
    },
    {
        "path": "detector.device",
        "section": "detector",
        "kind": "select",
        "options": ["cpu", "cuda:0"],
        "runtime_impact": "high",
    },
    {
        "path": "detector.use_half",
        "section": "detector",
        "kind": "boolean",
        "runtime_impact": "medium",
    },
    {
        "path": "detector.confidence_threshold",
        "section": "detector",
        "kind": "number",
        "minimum": 0.01,
        "maximum": 0.90,
        "step": 0.01,
        "runtime_impact": "low",
    },
    {
        "path": "detector.image_size",
        "section": "detector",
        "kind": "integer",
        "minimum": 640,
        "maximum": 2560,
        "step": 32,
        "runtime_impact": "high",
    },
    {
        "path": "sahi.slice_height",
        "section": "sahi",
        "kind": "integer",
        "minimum": 320,
        "maximum": 1920,
        "step": 16,
        "runtime_impact": "high",
    },
    {
        "path": "sahi.slice_width",
        "section": "sahi",
        "kind": "integer",
        "minimum": 320,
        "maximum": 1920,
        "step": 32,
        "runtime_impact": "high",
    },
    {
        "path": "sahi.overlap_height_ratio",
        "section": "sahi",
        "kind": "number",
        "minimum": 0.0,
        "maximum": 0.50,
        "step": 0.05,
        "runtime_impact": "medium",
    },
    {
        "path": "sahi.overlap_width_ratio",
        "section": "sahi",
        "kind": "number",
        "minimum": 0.0,
        "maximum": 0.50,
        "step": 0.05,
        "runtime_impact": "medium",
    },
    {
        "path": "sahi.postprocess_match_threshold",
        "section": "sahi",
        "kind": "number",
        "minimum": 0.10,
        "maximum": 0.90,
        "step": 0.05,
        "runtime_impact": "low",
    },
    {
        "path": "filtering.min_confidence",
        "section": "filtering",
        "kind": "number",
        "minimum": 0.0,
        "maximum": 0.90,
        "step": 0.01,
        "runtime_impact": "low",
    },
    {
        "path": "filtering.min_width",
        "section": "filtering",
        "kind": "number",
        "minimum": 1.0,
        "maximum": 100.0,
        "step": 1.0,
        "runtime_impact": "low",
    },
    {
        "path": "filtering.max_width",
        "section": "filtering",
        "kind": "number",
        "minimum": 4.0,
        "maximum": 300.0,
        "step": 1.0,
        "runtime_impact": "low",
    },
    {
        "path": "filtering.min_height",
        "section": "filtering",
        "kind": "number",
        "minimum": 1.0,
        "maximum": 100.0,
        "step": 1.0,
        "runtime_impact": "low",
    },
    {
        "path": "filtering.max_height",
        "section": "filtering",
        "kind": "number",
        "minimum": 4.0,
        "maximum": 300.0,
        "step": 1.0,
        "runtime_impact": "low",
    },
    {
        "path": "filtering.min_aspect_ratio",
        "section": "filtering",
        "kind": "number",
        "minimum": 0.10,
        "maximum": 1.0,
        "step": 0.05,
        "runtime_impact": "low",
    },
    {
        "path": "filtering.max_aspect_ratio",
        "section": "filtering",
        "kind": "number",
        "minimum": 1.0,
        "maximum": 5.0,
        "step": 0.05,
        "runtime_impact": "low",
    },
    {
        "path": "selection.min_accept_score",
        "section": "selection",
        "kind": "number",
        "minimum": 0.01,
        "maximum": 0.95,
        "step": 0.01,
        "runtime_impact": "low",
    },
    {
        "path": "selection.stable_history_length",
        "section": "selection",
        "kind": "integer",
        "minimum": 2,
        "maximum": 60,
        "step": 1,
        "runtime_impact": "low",
    },
    {
        "path": "selection.weights.distance_score",
        "section": "selection",
        "kind": "number",
        "minimum": 0.0,
        "maximum": 1.0,
        "step": 0.01,
        "runtime_impact": "low",
    },
    {
        "path": "selection.weights.direction_score",
        "section": "selection",
        "kind": "number",
        "minimum": 0.0,
        "maximum": 1.0,
        "step": 0.01,
        "runtime_impact": "low",
    },
    {
        "path": "selection.weights.velocity_score",
        "section": "selection",
        "kind": "number",
        "minimum": 0.0,
        "maximum": 1.0,
        "step": 0.01,
        "runtime_impact": "low",
    },
    {
        "path": "selection.weights.acceleration_penalty",
        "section": "selection",
        "kind": "number",
        "minimum": 0.0,
        "maximum": 1.0,
        "step": 0.01,
        "runtime_impact": "low",
    },
    {
        "path": "selection.weights.trajectory_length_bonus",
        "section": "selection",
        "kind": "number",
        "minimum": 0.0,
        "maximum": 1.0,
        "step": 0.01,
        "runtime_impact": "low",
    },
    {
        "path": "selection.weights.confidence",
        "section": "selection",
        "kind": "number",
        "minimum": 0.0,
        "maximum": 1.0,
        "step": 0.01,
        "runtime_impact": "low",
    },
    {
        "path": "selection.priors.enabled",
        "section": "selection",
        "kind": "boolean",
        "runtime_impact": "low",
    },
    {
        "path": "selection.priors.player_foot_radius_px",
        "section": "selection",
        "kind": "number",
        "minimum": 0.0,
        "maximum": 300.0,
        "step": 1.0,
        "runtime_impact": "low",
    },
    {
        "path": "selection.priors.player_foot_bonus",
        "section": "selection",
        "kind": "number",
        "minimum": 0.0,
        "maximum": 0.50,
        "step": 0.01,
        "runtime_impact": "low",
    },
    {
        "path": "selection.priors.recent_player_frame_window",
        "section": "selection",
        "kind": "integer",
        "minimum": 0,
        "maximum": 30,
        "step": 1,
        "runtime_impact": "low",
    },
    {
        "path": "tracking.max_lost_frames",
        "section": "tracking",
        "kind": "integer",
        "minimum": 0,
        "maximum": 120,
        "step": 1,
        "runtime_impact": "low",
    },
    {
        "path": "tracking.match_distance",
        "section": "tracking",
        "kind": "number",
        "minimum": 10.0,
        "maximum": 1000.0,
        "step": 10.0,
        "runtime_impact": "low",
    },
    {
        "path": "tracking.max_speed",
        "section": "tracking",
        "kind": "number",
        "minimum": 10.0,
        "maximum": 2000.0,
        "step": 10.0,
        "runtime_impact": "low",
    },
    {
        "path": "tracking.max_acceleration",
        "section": "tracking",
        "kind": "number",
        "minimum": 10.0,
        "maximum": 2000.0,
        "step": 10.0,
        "runtime_impact": "low",
    },
    {
        "path": "tracking.predicted_confidence_decay",
        "section": "tracking",
        "kind": "number",
        "minimum": 0.10,
        "maximum": 0.99,
        "step": 0.01,
        "runtime_impact": "low",
    },
    {
        "path": "postprocess.max_detected_island_length",
        "section": "postprocess",
        "kind": "integer",
        "minimum": 1,
        "maximum": 10,
        "step": 1,
        "runtime_impact": "low",
    },
    {
        "path": "postprocess.stable_segment_min_length",
        "section": "postprocess",
        "kind": "integer",
        "minimum": 2,
        "maximum": 30,
        "step": 1,
        "runtime_impact": "low",
    },
    {
        "path": "postprocess.min_jump_distance",
        "section": "postprocess",
        "kind": "number",
        "minimum": 20.0,
        "maximum": 2000.0,
        "step": 10.0,
        "runtime_impact": "low",
    },
    {
        "path": "postprocess.low_confidence_threshold",
        "section": "postprocess",
        "kind": "number",
        "minimum": 0.0,
        "maximum": 1.0,
        "step": 0.01,
        "runtime_impact": "low",
    },
)


def trial_tuning_schema() -> dict[str, Any]:
    """Return the bounded Step-3 controls; model selection deliberately belongs to T2."""

    return {
        "schema_version": TRIAL_TUNING_SCHEMA_VERSION,
        "patch_schema_version": PRODUCTION_TUNING_PATCH_SCHEMA_VERSION,
        "controls": deepcopy(list(_TUNING_CONTROLS)),
        "actions": [
            {
                "action_code": "return_to_field_setup",
                "target_step": "field_setup",
                "reason_code": "field_geometry_requires_new_calibration",
                "affected_paths": [
                    "filtering.roi",
                    "scene_bias.ground_zones",
                    "scene_bias.negative_rois",
                ],
                "lineage_constraint": ("invalidate_trial_and_downstream_then_create_new_calibration_version"),
            }
        ],
    }


_PROTOTYPE_KEYS = {"__proto__", "prototype", "constructor"}
_PRODUCTION_TRIAL_SYSTEM_LEAVES = {
    "input_video",
    "filtering.roi",
    "postprocess.enabled",
    "follow_cam.enabled",
    "runtime.start_frame",
    "runtime.max_frames",
}
_PRODUCTION_TRIAL_PATCH_ROOTS = {
    "detector",
    "filtering",
    "follow_cam",
    "input_video",
    "metadata",
    "postprocess",
    "runtime",
    "sahi",
    "scene_bias",
    "selection",
    "tracking",
}
_TUNING_CONTROLS_BY_PATH = {control["path"]: control for control in _TUNING_CONTROLS}
_TUNING_RELATIONS = (
    ("filtering.min_width", "filtering.max_width"),
    ("filtering.min_height", "filtering.max_height"),
    ("filtering.min_aspect_ratio", "filtering.max_aspect_ratio"),
)
_MISSING = object()


def production_tuning_values_sha256(values: dict[str, Any]) -> str:
    """Return the digest used by the browser's canonical tuning-version envelope."""

    normalized = _normalize_canonical_json(values)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_production_trial_config_patch(
    patch: dict[str, Any],
    *,
    base_config: dict[str, Any],
    legacy_created_at: str | None = None,
) -> dict[str, Any]:
    """Validate and normalize Step-3 patches before config materialization.

    Versioned patches must exactly describe all schema controls and the sparse
    tuning leaves derived from the resolved base config. Safe legacy tuning
    leaves are upgraded to the same complete version envelope server-side.
    """

    if not isinstance(patch, dict):
        raise ValueError("production_trial config_patch must be an object")
    if not isinstance(base_config, dict):
        raise ValueError("production_trial resolved base config must be an object")
    _assert_safe_json_keys(patch, path="config_patch")
    normalized_patch = deepcopy(patch)

    unsupported_roots = sorted(str(key) for key in patch if key not in _PRODUCTION_TRIAL_PATCH_ROOTS)
    if unsupported_roots:
        raise ValueError(f"Unsupported production_trial config path: {unsupported_roots[0]}")

    tuning_leaves: dict[str, Any] = {}
    for key, value in patch.items():
        if key == "metadata":
            continue
        if key == "scene_bias":
            _validate_scene_bias_patch(value)
            continue
        if key == "input_video":
            if not isinstance(value, str) or not value.strip():
                raise ValueError("production_trial config_patch.input_video must be a non-empty string")
            continue
        _collect_production_trial_leaves(value, prefix=key, tuning_leaves=tuning_leaves)

    metadata = patch.get("metadata", _MISSING)
    tuning_metadata: Any = _MISSING
    if metadata is not _MISSING:
        if not isinstance(metadata, dict):
            raise ValueError("production_trial config_patch.metadata must be an object")
        unsupported_metadata = sorted(
            str(key) for key in metadata if key not in {"production_workflow", "production_tuning"}
        )
        if unsupported_metadata:
            raise ValueError(f"Unsupported production_trial config path: metadata.{unsupported_metadata[0]}")
        if "production_workflow" in metadata and not isinstance(metadata["production_workflow"], dict):
            raise ValueError("metadata.production_workflow must be an object")
        if "production_tuning" in metadata:
            tuning_metadata = metadata["production_tuning"]

    if tuning_metadata is _MISSING:
        if not tuning_leaves:
            return normalized_patch
        values = _complete_legacy_tuning_values(
            tuning_leaves,
            base_config=base_config,
        )
        for path in tuning_leaves:
            base_value = _value_at_path(base_config, path)
            if base_value is not _MISSING and _same_tuning_value(values[path], base_value):
                _delete_value_at_path(normalized_patch, path)
        values_sha256 = production_tuning_values_sha256(values)
        created_at = legacy_created_at or datetime.now(timezone.utc).isoformat()
        _require_nonempty_string(created_at, path="metadata.production_tuning.created_at")
        normalized_metadata = normalized_patch.setdefault("metadata", {})
        normalized_metadata["production_tuning"] = {
            "schema_version": PRODUCTION_TUNING_PATCH_SCHEMA_VERSION,
            "version_id": f"legacy-{values_sha256[:24]}",
            "parent_version_id": None,
            "created_at": created_at,
            "values_sha256": values_sha256,
            "values": values,
            "history": [],
        }
        return normalized_patch

    values = _validate_tuning_version(tuning_metadata)
    _validate_versioned_tuning_leaves(
        tuning_leaves,
        values=values,
        base_config=base_config,
    )
    return normalized_patch


def validate_production_trial_config_patch(
    patch: dict[str, Any],
    *,
    base_config: dict[str, Any],
) -> None:
    """Validate a production-trial patch without returning its normalized form."""

    normalize_production_trial_config_patch(patch, base_config=base_config)


def _normalize_canonical_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize_canonical_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_canonical_json(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Tuning metadata contains a non-finite number")
        if value == 0 or value.is_integer():
            return int(value)
    return value


def _assert_safe_json_keys(value: Any, *, path: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string key")
            if key in _PROTOTYPE_KEYS:
                raise ValueError(f"Unsafe production_trial config path: {path}.{key}")
            _assert_safe_json_keys(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_safe_json_keys(item, path=f"{path}[{index}]")


def _collect_production_trial_leaves(
    value: Any,
    *,
    prefix: str,
    tuning_leaves: dict[str, Any],
) -> None:
    if isinstance(value, dict):
        if not value:
            if prefix not in {
                "detector",
                "filtering",
                "follow_cam",
                "postprocess",
                "runtime",
                "sahi",
                "selection",
                "tracking",
            }:
                raise ValueError(f"Unsupported production_trial config path: {prefix}")
            return
        for key, item in value.items():
            _collect_production_trial_leaves(
                item,
                prefix=f"{prefix}.{key}",
                tuning_leaves=tuning_leaves,
            )
        return
    if prefix in _PRODUCTION_TRIAL_SYSTEM_LEAVES:
        _validate_system_leaf(prefix, value)
        return
    if prefix not in _TUNING_CONTROLS_BY_PATH:
        raise ValueError(f"Unsupported production_trial config path: {prefix}")
    tuning_leaves[prefix] = value


def _validate_system_leaf(path: str, value: Any) -> None:
    if path == "filtering.roi":
        if (
            not isinstance(value, list)
            or len(value) != 4
            or any(not _is_finite_number(item) for item in value)
            or float(value[0]) > float(value[2])
            or float(value[1]) > float(value[3])
        ):
            raise ValueError("filtering.roi must contain ordered finite x1, y1, x2, y2 values")
        return
    if path in {"postprocess.enabled", "follow_cam.enabled"}:
        if not isinstance(value, bool):
            raise ValueError(f"{path} must be boolean")
        return
    if path == "runtime.start_frame":
        if not _is_integer_value(value) or value < 0:
            raise ValueError("runtime.start_frame must be a non-negative integer")
        return
    if path == "runtime.max_frames" and (not _is_integer_value(value) or value <= 0):
        raise ValueError("runtime.max_frames must be a positive integer")


def _validate_scene_bias_patch(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("production_trial config_patch.scene_bias must be an object")
    unsupported = sorted(str(key) for key in value if key not in {"enabled", "ground_zones", "negative_rois"})
    if unsupported:
        raise ValueError(f"Unsupported production_trial config path: scene_bias.{unsupported[0]}")
    if "enabled" in value and not isinstance(value["enabled"], bool):
        raise ValueError("scene_bias.enabled must be boolean")
    for key in ("ground_zones", "negative_rois"):
        if key not in value:
            continue
        zones = value[key]
        if not isinstance(zones, list):
            raise ValueError(f"scene_bias.{key} must be an array")
        for index, zone in enumerate(zones):
            path = f"scene_bias.{key}[{index}]"
            if not isinstance(zone, dict) or set(zone) != {"name", "points"}:
                raise ValueError(f"{path} must contain only name and points")
            if not isinstance(zone["name"], str) or not zone["name"].strip():
                raise ValueError(f"{path}.name must be a non-empty string")
            points = zone["points"]
            if not isinstance(points, list) or len(points) < 3:
                raise ValueError(f"{path}.points must contain at least three points")
            for point in points:
                if not isinstance(point, list) or len(point) != 2 or any(not _is_finite_number(item) for item in point):
                    raise ValueError(f"{path}.points must contain finite [x, y] pairs")


def _complete_legacy_tuning_values(
    tuning_leaves: dict[str, Any],
    *,
    base_config: dict[str, Any],
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for path, control in _TUNING_CONTROLS_BY_PATH.items():
        value = tuning_leaves.get(path, _value_at_path(base_config, path))
        if value is _MISSING:
            raise ValueError(f"Resolved base config is missing tuning control: {path}")
        _validate_tuning_value(control, value, path=path)
        values[path] = deepcopy(value)
    _validate_tuning_relations(values)
    return values


def _validate_versioned_tuning_leaves(
    tuning_leaves: dict[str, Any],
    *,
    values: dict[str, Any],
    base_config: dict[str, Any],
) -> None:
    expected_patch_paths: set[str] = set()
    for path in _TUNING_CONTROLS_BY_PATH:
        base_value = _value_at_path(base_config, path)
        if base_value is _MISSING:
            raise ValueError(f"Resolved base config is missing tuning control: {path}")
        if not _same_tuning_value(values[path], base_value):
            expected_patch_paths.add(path)
            if path not in tuning_leaves or not _same_tuning_value(tuning_leaves[path], values[path]):
                raise ValueError(f"metadata.production_tuning.values does not match config_patch leaf: {path}")
    unexpected = sorted(set(tuning_leaves) - expected_patch_paths)
    if unexpected:
        raise ValueError(f"Versioned tuning patch contains a redundant or inconsistent leaf: {unexpected[0]}")


def _validate_tuning_version(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("metadata.production_tuning must be an object")
    expected_keys = {
        "schema_version",
        "version_id",
        "parent_version_id",
        "created_at",
        "values_sha256",
        "values",
        "history",
    }
    if set(value) != expected_keys:
        raise ValueError("metadata.production_tuning has an invalid structure")
    if value["schema_version"] != PRODUCTION_TUNING_PATCH_SCHEMA_VERSION:
        raise ValueError("Unsupported metadata.production_tuning schema_version")
    _require_nonempty_string(value["version_id"], path="metadata.production_tuning.version_id")
    _require_nonempty_string(value["created_at"], path="metadata.production_tuning.created_at")
    parent_version_id = value["parent_version_id"]
    if parent_version_id is not None:
        _require_nonempty_string(parent_version_id, path="metadata.production_tuning.parent_version_id")
    current_values = _validate_tuning_values(
        value["values"],
        digest=value["values_sha256"],
        path="metadata.production_tuning",
    )
    history = value["history"]
    if not isinstance(history, list):
        raise ValueError("metadata.production_tuning.history must be an array")
    version_ids: list[str] = []
    for index, snapshot in enumerate(history):
        path = f"metadata.production_tuning.history[{index}]"
        if not isinstance(snapshot, dict) or set(snapshot) != {
            "version_id",
            "created_at",
            "values_sha256",
            "values",
        }:
            raise ValueError(f"{path} has an invalid structure")
        _require_nonempty_string(snapshot["version_id"], path=f"{path}.version_id")
        _require_nonempty_string(snapshot["created_at"], path=f"{path}.created_at")
        _validate_tuning_values(snapshot["values"], digest=snapshot["values_sha256"], path=path)
        version_ids.append(snapshot["version_id"])
    if len(version_ids) != len(set(version_ids)) or value["version_id"] in version_ids:
        raise ValueError("metadata.production_tuning version IDs must be unique")
    expected_parent = version_ids[-1] if version_ids else None
    if parent_version_id != expected_parent:
        raise ValueError("metadata.production_tuning parent/history linkage is inconsistent")
    return current_values


def _validate_tuning_values(value: Any, *, digest: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(_TUNING_CONTROLS_BY_PATH):
        raise ValueError(f"{path}.values must contain every trial_tuning_schema control exactly once")
    for control_path, control in _TUNING_CONTROLS_BY_PATH.items():
        _validate_tuning_value(control, value[control_path], path=control_path)
    _validate_tuning_relations(value)
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError(f"{path}.values_sha256 must be a lowercase SHA-256 digest")
    if production_tuning_values_sha256(value) != digest:
        raise ValueError(f"{path}.values_sha256 does not match values")
    return value


def _validate_tuning_value(control: dict[str, Any], value: Any, *, path: str) -> None:
    kind = control["kind"]
    if kind == "boolean":
        valid = isinstance(value, bool)
    elif kind == "select":
        valid = isinstance(value, str) and value in control["options"]
    elif kind == "multi_select":
        valid = (
            isinstance(value, list)
            and bool(value)
            and all(isinstance(item, str) and item in control["options"] for item in value)
            and len(value) == len(set(value))
        )
    elif kind == "integer":
        valid = _is_integer_value(value) and _bounded_step_value(control, float(value))
    else:
        valid = _is_finite_number(value) and _bounded_step_value(control, float(value))
    if not valid:
        raise ValueError(f"Invalid production_trial tuning value: {path}")


def _bounded_step_value(control: dict[str, Any], value: float) -> bool:
    minimum = float(control["minimum"])
    maximum = float(control["maximum"])
    step = float(control["step"])
    if value < minimum or value > maximum:
        return False
    step_count = (value - minimum) / step
    return math.isclose(step_count, round(step_count), rel_tol=0.0, abs_tol=1e-7)


def _validate_tuning_relations(values: dict[str, Any]) -> None:
    for minimum_path, maximum_path in _TUNING_RELATIONS:
        minimum = values.get(minimum_path, _MISSING)
        maximum = values.get(maximum_path, _MISSING)
        if _is_finite_number(minimum) and _is_finite_number(maximum) and float(minimum) > float(maximum):
            raise ValueError(f"{minimum_path} must not exceed {maximum_path}")


def _value_at_path(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _delete_value_at_path(value: dict[str, Any], path: str) -> None:
    current: dict[str, Any] = value
    parents: list[tuple[dict[str, Any], str]] = []
    parts = path.split(".")
    for part in parts[:-1]:
        nested = current.get(part)
        if not isinstance(nested, dict):
            return
        parents.append((current, part))
        current = nested
    current.pop(parts[-1], None)
    for parent, key in reversed(parents):
        nested = parent.get(key)
        if isinstance(nested, dict) and not nested:
            parent.pop(key)


def _same_tuning_value(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if _is_finite_number(left) and _is_finite_number(right):
        return float(left) == float(right)
    return type(left) is type(right) and left == right


def _is_finite_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value))


def _is_integer_value(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int)


def _require_nonempty_string(value: Any, *, path: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a non-empty string")


def collect_trial_stage_counts(
    output_dir: Path,
    tracking_contract: dict[str, Any] | None,
    *,
    tracklet_count: int | None,
    raw_track: dict[str, Any] | None,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    unavailable = {"value": None, "status": "not_collected"}
    summary: dict[str, Any] = {
        "schema_version": TRIAL_STAGE_COUNTER_SCHEMA_VERSION,
        "coverage_status": "not_collected",
        "evaluated_frames": dict(unavailable),
        "detected_frames": dict(unavailable),
        "predicted_frames": dict(unavailable),
        "lost_frames": dict(unavailable),
        "raw_candidates": dict(unavailable),
        "class_mapped_candidates": dict(unavailable),
        "filtered_candidates": dict(unavailable),
        "selected_candidates": dict(unavailable),
        "tracklets": (
            {"value": tracklet_count, "status": "collected"}
            if _nonnegative_int(tracklet_count) is not None
            else dict(unavailable)
        ),
        "rejection_reasons": {},
        "reconciliation": {"status": "not_collected", "reason_codes": []},
    }

    debug_path = output_dir / "debug.jsonl"
    if not _safe_direct_file(output_dir, debug_path) or not isinstance(tracking_contract, dict):
        return summary

    frame_count = 0
    detector_output_count = 0
    class_mapped_candidate_count = 0
    filtered_candidate_count = 0
    selected_candidate_count = 0
    rejection_reasons: Counter[str] = Counter()
    errors: list[str] = []
    detector_stage_evidence_complete = True
    candidate_stage_evidence_complete = True
    selected_stage_evidence_complete = True
    status_evidence_complete = True
    status_counts: Counter[str] = Counter()
    try:
        with debug_path.open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    errors.append(f"debug_json_invalid:{line_number}")
                    detector_stage_evidence_complete = False
                    candidate_stage_evidence_complete = False
                    selected_stage_evidence_complete = False
                    status_evidence_complete = False
                    continue
                if not isinstance(row, dict):
                    errors.append(f"debug_record_invalid:{line_number}")
                    detector_stage_evidence_complete = False
                    candidate_stage_evidence_complete = False
                    selected_stage_evidence_complete = False
                    status_evidence_complete = False
                    continue
                raw_count = _nonnegative_int(row.get("raw_candidate_count"))
                filtered_count = _nonnegative_int(row.get("filtered_candidate_count"))
                reacquire_count = _nonnegative_int(row.get("reacquire_candidate_count"))
                candidate_counters_complete = (
                    raw_count is not None and filtered_count is not None and reacquire_count is not None
                )
                if not candidate_counters_complete:
                    missing_counters = [
                        name
                        for name, value in (
                            ("raw_candidate_count", raw_count),
                            ("filtered_candidate_count", filtered_count),
                            ("reacquire_candidate_count", reacquire_count),
                        )
                        if value is None
                    ]
                    errors.append(f"debug_counter_not_collected:{','.join(missing_counters)}:{line_number}")
                    candidate_stage_evidence_complete = False
                    detector_stage_evidence_complete = False
                frame_exception = row.get("frame_exception")
                if not isinstance(frame_exception, bool):
                    errors.append(f"debug_frame_exception_not_collected:{line_number}")
                    detector_stage_evidence_complete = False
                elif frame_exception:
                    errors.append(f"debug_frame_exception:{line_number}")

                stage_schema = row.get("detector_stage_schema_version")
                model_output_count = _nonnegative_int(row.get("detector_output_count"))
                class_mapped_count = _nonnegative_int(row.get("class_mapped_candidate_count"))
                raw_class_rejections = row.get("class_rejection_counts")
                class_rejections_valid = _valid_rejection_reasons(raw_class_rejections)
                if (
                    stage_schema != DETECTOR_STAGE_EVIDENCE_SCHEMA_VERSION
                    or model_output_count is None
                    or class_mapped_count is None
                    or not class_rejections_valid
                ):
                    errors.append(f"detector_stage_evidence_not_collected:{line_number}")
                    detector_stage_evidence_complete = False
                elif raw_count is not None:
                    assert isinstance(raw_class_rejections, dict)
                    rejected_by_class = sum(int(count) for count in raw_class_rejections.values())
                    if (
                        class_mapped_count != raw_count
                        or class_mapped_count > model_output_count
                        or model_output_count - class_mapped_count != rejected_by_class
                    ):
                        errors.append(f"detector_stage_evidence_mismatch:{line_number}")
                        detector_stage_evidence_complete = False
                    else:
                        detector_output_count += model_output_count
                        class_mapped_candidate_count += class_mapped_count
                        for label, count in raw_class_rejections.items():
                            rejection_reasons[f"class_not_allowed:{label}"] += int(count)

                status = row.get("status")
                if status not in {"Detected", "Predicted", "Lost"}:
                    errors.append(
                        f"debug_status_not_collected:{line_number}"
                        if status is None
                        else f"debug_status_invalid:{line_number}"
                    )
                    status_evidence_complete = False
                else:
                    frame_count += 1
                    status_counts[status] += 1

                raw_selected_count = row.get("selected_candidate_count")
                selected_count = _nonnegative_int(raw_selected_count)
                if selected_count not in {0, 1}:
                    errors.append(
                        f"debug_selected_counter_not_collected:{line_number}"
                        if raw_selected_count is None
                        else f"debug_selected_counter_invalid:{line_number}"
                    )
                    selected_stage_evidence_complete = False
                else:
                    selected_candidate_count += selected_count

                if candidate_counters_complete:
                    assert filtered_count is not None
                    filtered_candidate_count += filtered_count
                raw_rejections = row.get("filter_rejection_counts")
                if isinstance(raw_rejections, dict):
                    for reason, count in raw_rejections.items():
                        parsed_count = _nonnegative_int(count)
                        if isinstance(reason, str) and reason and parsed_count is not None:
                            rejection_reasons[reason] += parsed_count
                        else:
                            errors.append(f"debug_rejection_counter_invalid:{line_number}")
                else:
                    errors.append(
                        f"debug_rejection_counter_invalid:{line_number}"
                        if raw_rejections is not None
                        else f"debug_rejection_counter_not_collected:filter_rejection_counts:{line_number}"
                    )
    except (OSError, UnicodeError):
        return summary

    contract_summary = tracking_contract.get("summary")
    contract_frames = tracking_contract.get("frames")
    contract_candidates = tracking_contract.get("candidates")
    contract_errors = tracking_contract.get("validation_errors")
    contract_frame_count = _mapping_nonnegative_int(contract_summary, "frame_count")
    contract_candidate_count = _mapping_nonnegative_int(contract_summary, "candidate_count")
    validation_error_count = _mapping_nonnegative_int(contract_summary, "validation_error_count")
    if (
        tracking_contract.get("schema_version") != "2.0"
        or not isinstance(contract_frames, list)
        or not isinstance(contract_candidates, list)
        or not isinstance(contract_errors, list)
        or contract_frame_count is None
        or contract_candidate_count is None
        or validation_error_count is None
        or validation_error_count != len(contract_errors)
        or validation_error_count > 0
    ):
        errors.append("tracking_contract_invalid")
    else:
        if contract_frame_count != len(contract_frames):
            errors.append("tracking_contract_frame_count_mismatch")
        if contract_candidate_count != len(contract_candidates):
            errors.append("tracking_contract_candidate_count_mismatch")
        if status_evidence_complete and contract_frame_count != frame_count:
            errors.append("evaluated_frame_count_mismatch")
        if detector_stage_evidence_complete and contract_candidate_count != class_mapped_candidate_count:
            errors.append("class_mapped_candidate_count_mismatch")

    if (
        detector_stage_evidence_complete
        and candidate_stage_evidence_complete
        and filtered_candidate_count > class_mapped_candidate_count
    ):
        errors.append("filtered_candidate_count_exceeds_candidates")
    if (
        selected_stage_evidence_complete
        and candidate_stage_evidence_complete
        and selected_candidate_count > filtered_candidate_count
    ):
        errors.append("selected_candidate_count_exceeds_candidates")
    if (
        selected_stage_evidence_complete
        and status_evidence_complete
        and selected_candidate_count != status_counts["Detected"]
    ):
        errors.append("selected_detected_count_mismatch")

    raw_status_counts = {
        "frame_count": _mapping_nonnegative_int(raw_track, "frame_count"),
        "Detected": _mapping_nonnegative_int(raw_track, "detected"),
        "Predicted": _mapping_nonnegative_int(raw_track, "predicted"),
        "Lost": _mapping_nonnegative_int(raw_track, "lost"),
    }
    if any(value is None for value in raw_status_counts.values()):
        errors.append("raw_track_status_counts_not_collected")
    elif status_evidence_complete:
        if raw_status_counts["frame_count"] != frame_count:
            errors.append("raw_track_frame_count_mismatch")
        for debug_status, suffix in (
            ("Detected", "detected"),
            ("Predicted", "predicted"),
            ("Lost", "lost"),
        ):
            if raw_status_counts[debug_status] != status_counts[debug_status]:
                errors.append(f"raw_track_{suffix}_count_mismatch")

    raw_stage_count = (
        {"value": detector_output_count, "status": "collected"}
        if detector_stage_evidence_complete
        else dict(unavailable)
    )
    mapped_stage_count = (
        {"value": class_mapped_candidate_count, "status": "collected"}
        if detector_stage_evidence_complete
        else dict(unavailable)
    )
    debug_status_stage_counts = {
        f"{name.lower()}_frames": (
            {"value": status_counts[name], "status": "collected"} if status_evidence_complete else dict(unavailable)
        )
        for name in ("Detected", "Predicted", "Lost")
    }
    evaluated_stage_count = (
        {"value": frame_count, "status": "collected"} if status_evidence_complete else dict(unavailable)
    )
    filtered_stage_count = (
        {"value": filtered_candidate_count, "status": "collected"}
        if candidate_stage_evidence_complete
        else dict(unavailable)
    )
    selected_stage_count = (
        {"value": selected_candidate_count, "status": "collected"}
        if selected_stage_evidence_complete
        else dict(unavailable)
    )

    summary.update(
        {
            "coverage_status": "invalid" if errors else "complete",
            "evaluated_frames": evaluated_stage_count,
            **debug_status_stage_counts,
            "raw_candidates": raw_stage_count,
            "class_mapped_candidates": mapped_stage_count,
            "filtered_candidates": filtered_stage_count,
            "selected_candidates": selected_stage_count,
            "rejection_reasons": dict(sorted(rejection_reasons.items())),
            "reconciliation": {
                "status": "mismatch" if errors else "reconciled",
                "reason_codes": sorted(set(errors)),
            },
        }
    )
    return summary


_TRACK_DIAGNOSTIC_FIELDS: tuple[tuple[str, bool, float | None], ...] = (
    ("frame_count", True, None),
    ("detected", True, None),
    ("predicted", True, None),
    ("lost", True, None),
    ("detected_ratio", False, 1.0),
    ("predicted_ratio", False, 1.0),
    ("lost_ratio", False, 1.0),
    ("longest_lost_streak", True, None),
    ("false_positive_island_count", True, None),
    ("max_step_px", False, None),
)


def _numeric_observation(
    source: Any,
    key: str,
    *,
    integer: bool,
    maximum: float | None = None,
) -> dict[str, Any]:
    if not isinstance(source, dict) or key not in source or source[key] is None:
        return {"status": "not_collected", "value": None}
    raw = source[key]
    value = _nonnegative_int(raw) if integer else _finite_number(raw)
    if value is None or float(value) < 0 or (maximum is not None and float(value) > maximum):
        return {"status": "invalid", "value": None}
    return {"status": "collected", "value": value}


def _aggregate_observation_status(observations: list[dict[str, Any]]) -> str:
    statuses = {str(item.get("status")) for item in observations}
    if "invalid" in statuses:
        return "invalid"
    if "not_collected" in statuses:
        return "not_collected"
    return "collected"


def _track_diagnostics(track: Any) -> dict[str, Any]:
    observations = {
        name: _numeric_observation(track, name, integer=integer, maximum=maximum)
        for name, integer, maximum in _TRACK_DIAGNOSTIC_FIELDS
    }
    return {
        "status": _aggregate_observation_status(list(observations.values())),
        **observations,
    }


def _count_and_rate_diagnostics(
    summary: Any,
    key: str,
    *,
    frame_count: int | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    count = _numeric_observation(summary, key, integer=True)
    if count["status"] != "collected" or frame_count is None or frame_count <= 0:
        return count, {"status": "not_collected", "value": None}
    return count, {
        "status": "collected",
        "value": round(float(count["value"]) * 100.0 / frame_count, 4),
    }


def _build_trial_diagnostics(
    *,
    raw_track: Any,
    cleaned_track: Any,
    stage_counts: Any,
    ai_review_summary: Any,
    event_summary: Any,
    follow_cam_summary: Any,
) -> dict[str, Any]:
    raw_diagnostics = _track_diagnostics(raw_track)
    cleaned_diagnostics = _track_diagnostics(cleaned_track)
    frame_count = _mapping_nonnegative_int(raw_track, "frame_count")
    ai_count, ai_rate = _count_and_rate_diagnostics(
        ai_review_summary,
        "trigger_count",
        frame_count=frame_count,
    )
    event_count, event_rate = _count_and_rate_diagnostics(
        event_summary,
        "candidate_count",
        frame_count=frame_count,
    )

    raw_rejections = stage_counts.get("rejection_reasons") if isinstance(stage_counts, dict) else None
    if raw_rejections is None:
        rejection_observation = {"status": "not_collected", "value": None}
    elif _valid_rejection_reasons(raw_rejections):
        rejection_observation = {
            "status": "collected",
            "value": dict(sorted(raw_rejections.items())),
        }
    else:
        rejection_observation = {"status": "invalid", "value": None}

    motion = _follow_cam_motion(follow_cam_summary)
    follow_observations = {
        name: _numeric_observation(motion, name, integer=False)
        for name in ("max_pan_step_px", "max_pan_accel_px", "max_zoom_step_ratio")
    }
    return {
        "raw_track": raw_diagnostics,
        "cleaned_track": cleaned_diagnostics,
        "rejection_reasons": rejection_observation,
        "ai_review_trigger_count": ai_count,
        "ai_review_triggers_per_100_frames": ai_rate,
        "event_candidate_count": event_count,
        "event_candidates_per_100_frames": event_rate,
        "follow_cam": {
            "status": _aggregate_observation_status(list(follow_observations.values())),
            **follow_observations,
        },
    }


def build_trial_signal_gate_v2(
    *,
    run_status: str,
    raw_track: dict[str, Any] | None,
    cleaned_track: dict[str, Any] | None,
    stage_counts: dict[str, Any] | None,
    audit_summary: dict[str, Any] | None,
    raw_tracklet_count: int | None,
    follow_cam_summary: dict[str, Any] | None,
    decoder_failure: bool,
    evidence: dict[str, str] | None,
    ai_review_summary: dict[str, Any] | None = None,
    event_summary: dict[str, Any] | None = None,
    enable_postprocess: bool = True,
    enable_follow_cam: bool = True,
    acceptance_contract_complete: bool = False,
    option_conflicts: list[str] | None = None,
) -> dict[str, Any]:
    profile = deepcopy(TRIAL_SIGNAL_THRESHOLD_PROFILE)
    profile["sha256"] = _sha256_json(profile)
    reasons: list[str] = []
    coverage_complete = True

    if decoder_failure:
        reasons.append("decode_failure")
        coverage_complete = False
    if run_status != "completed":
        reasons.append("run_not_completed")
        coverage_complete = False
    for option_name in option_conflicts or []:
        reasons.append(f"trial_option_conflict:{option_name}")
        coverage_complete = False
    if not _valid_track_metrics(raw_track):
        reasons.append("metrics_not_collected")
        coverage_complete = False
    stage_values: dict[str, int | None] = {}
    if not isinstance(stage_counts, dict):
        reasons.append("stage_counts_not_collected")
        coverage_complete = False
    else:
        reconciliation_reasons = _nested_value(stage_counts, "reconciliation", "reason_codes")
        if (
            stage_counts.get("schema_version") != TRIAL_STAGE_COUNTER_SCHEMA_VERSION
            or stage_counts.get("coverage_status") != "complete"
            or _nested_value(stage_counts, "reconciliation", "status") != "reconciled"
            or not isinstance(reconciliation_reasons, list)
            or bool(reconciliation_reasons)
        ):
            reasons.append(
                "stage_counter_mismatch"
                if _nested_value(stage_counts, "reconciliation", "status") == "mismatch"
                else "stage_counts_not_collected"
            )
            coverage_complete = False
        required_counter_statuses = {
            "evaluated_frames": {"collected"},
            "detected_frames": {"collected"},
            "predicted_frames": {"collected"},
            "lost_frames": {"collected"},
            "raw_candidates": {"collected"},
            "class_mapped_candidates": {"collected"},
            "filtered_candidates": {"collected"},
            "selected_candidates": {"collected"},
            "tracklets": {"collected"},
        }
        for name, accepted_statuses in required_counter_statuses.items():
            item = stage_counts.get(name)
            parsed = (
                _nonnegative_int(item.get("value"))
                if isinstance(item, dict) and item.get("status") in accepted_statuses
                else None
            )
            stage_values[name] = parsed
            if parsed is None:
                reasons.append(f"stage_counter_not_collected:{name}")
                coverage_complete = False
        if not _valid_rejection_reasons(stage_counts.get("rejection_reasons")):
            reasons.append("rejection_reasons_not_collected")
            coverage_complete = False
        if isinstance(reconciliation_reasons, list) and any(
            isinstance(reason, str) and reason.startswith("debug_frame_exception:") for reason in reconciliation_reasons
        ):
            reasons.append("frame_exception")
            coverage_complete = False
    audit_tracklets = _mapping_nonnegative_int(audit_summary, "tracklet_count")
    raw_audit_tracklets = _nonnegative_int(raw_tracklet_count)
    if audit_tracklets is None:
        reasons.append("audit_not_collected")
        coverage_complete = False
    if raw_audit_tracklets is None:
        reasons.append("raw_audit_tracklet_count_not_collected")
        coverage_complete = False

    evaluated_frames = stage_values.get("evaluated_frames")
    raw_candidates = stage_values.get("raw_candidates")
    class_mapped_candidates = stage_values.get("class_mapped_candidates")
    filtered_candidates = stage_values.get("filtered_candidates")
    selected_candidates = stage_values.get("selected_candidates")
    tracklets = stage_values.get("tracklets")
    if evaluated_frames == 0:
        reasons.append("evaluated_frames_zero")
        coverage_complete = False
    if (
        _valid_track_metrics(raw_track)
        and evaluated_frames is not None
        and raw_track["frame_count"] != evaluated_frames
    ):
        reasons.append("track_frame_count_mismatch")
        coverage_complete = False
    if _valid_track_metrics(raw_track):
        for stage_name, track_name in (
            ("detected_frames", "detected"),
            ("predicted_frames", "predicted"),
            ("lost_frames", "lost"),
        ):
            stage_value = stage_values.get(stage_name)
            if stage_value is not None and raw_track[track_name] != stage_value:
                reasons.append(f"track_{track_name}_count_mismatch")
                coverage_complete = False
    if raw_candidates is not None and class_mapped_candidates is not None and class_mapped_candidates > raw_candidates:
        reasons.append("class_mapped_candidate_count_exceeds_detector_output")
        coverage_complete = False
    if (
        class_mapped_candidates is not None
        and filtered_candidates is not None
        and filtered_candidates > class_mapped_candidates
    ):
        reasons.append("filtered_candidate_count_exceeds_class_mapped")
        coverage_complete = False
    if (
        filtered_candidates is not None
        and selected_candidates is not None
        and selected_candidates > filtered_candidates
    ):
        reasons.append("selected_candidate_count_exceeds_filtered")
        coverage_complete = False
    if selected_candidates is not None and tracklets is not None and tracklets > selected_candidates:
        reasons.append("tracklet_count_exceeds_selected_candidates")
        coverage_complete = False
    if tracklets is not None and raw_audit_tracklets is not None and tracklets != raw_audit_tracklets:
        reasons.append("tracklet_count_mismatch")
        coverage_complete = False

    failure_code = "insufficient_evidence"
    if decoder_failure:
        failure_code = "decode_failure"
    elif coverage_complete and raw_candidates == 0:
        reasons.append("zero_candidate")
        failure_code = "no_raw_candidates"
    elif coverage_complete and raw_candidates is not None and raw_candidates > 0 and class_mapped_candidates == 0:
        reasons.append("all_candidates_class_rejected")
        failure_code = "all_candidates_class_rejected"
    elif coverage_complete and raw_candidates is not None and raw_candidates > 0 and filtered_candidates == 0:
        reasons.append("all_candidates_filtered")
        failure_code = "all_candidates_filtered"

    if coverage_complete and tracklets == 0:
        reasons.append("zero_tracklet")
        if failure_code == "insufficient_evidence":
            failure_code = "no_tracklets"
    if (
        coverage_complete
        and isinstance(raw_track, dict)
        and evaluated_frames is not None
        and evaluated_frames > 0
        and raw_track.get("lost") == evaluated_frames
    ):
        reasons.append("all_lost")
        if failure_code == "insufficient_evidence":
            failure_code = "all_lost"

    noisy = False
    unstable = False
    acceptance_metric_reasons: list[str] = []
    if coverage_complete and isinstance(raw_track, dict):
        thresholds = profile["thresholds"]
        frame_count = _nonnegative_int(raw_track.get("frame_count")) or 0
        suspicious = _mapping_nonnegative_int(audit_summary, "suspicious_tracklet_count")
        if audit_tracklets and suspicious is not None:
            noisy = noisy or suspicious / audit_tracklets > thresholds["maximum_suspicious_tracklet_ratio"]

        checked_tracks: list[tuple[str, dict[str, Any]]] = [("raw", raw_track)]
        acceptance_metric_reasons.extend(_required_track_metric_reasons(raw_track, "raw"))
        if enable_postprocess:
            if not _valid_track_metrics(cleaned_track):
                acceptance_metric_reasons.append("cleaned_metrics_not_collected")
            elif isinstance(cleaned_track, dict):
                checked_tracks.append(("cleaned", cleaned_track))
                acceptance_metric_reasons.extend(_required_track_metric_reasons(cleaned_track, "cleaned"))
                if _nonnegative_int(cleaned_track.get("frame_count")) != evaluated_frames:
                    acceptance_metric_reasons.append("cleaned_frame_count_mismatch")

        partial_signal = False
        for _, track_metrics in checked_tracks:
            track_noisy, track_unstable, track_partial = _track_threshold_outcome(track_metrics, thresholds)
            noisy = noisy or track_noisy
            unstable = unstable or track_unstable
            partial_signal = partial_signal or track_partial
        if partial_signal:
            reasons.append("partial_signal")

        motion = _follow_cam_motion(follow_cam_summary)
        if motion is not None:
            unstable = unstable or _exceeds_optional(
                motion.get("max_pan_step_px"), thresholds["maximum_follow_cam_pan_step_px"]
            )
            unstable = unstable or _exceeds_optional(
                motion.get("max_pan_accel_px"), thresholds["maximum_follow_cam_pan_accel_px"]
            )
            unstable = unstable or _exceeds_optional(
                motion.get("max_zoom_step_ratio"), thresholds["maximum_follow_cam_zoom_step_ratio"]
            )
        acceptance_metric_reasons.extend(_required_audit_metric_reasons(audit_summary))
        if enable_follow_cam:
            if motion is None:
                acceptance_metric_reasons.append("follow_cam_motion_not_collected")
            else:
                for name in ("max_pan_step_px", "max_pan_accel_px", "max_zoom_step_ratio"):
                    if _finite_number(motion.get(name)) is None:
                        acceptance_metric_reasons.append(f"follow_cam_metric_not_collected:{name}")

        ai_trigger_count = _mapping_nonnegative_int(ai_review_summary, "trigger_count")
        if ai_trigger_count is None:
            acceptance_metric_reasons.append("ai_review_trigger_budget_not_collected")
        elif (
            frame_count > 0
            and ai_trigger_count * 100.0 / frame_count > thresholds["maximum_ai_review_triggers_per_100_frames"]
        ):
            noisy = True
            reasons.append("ai_review_trigger_budget_exceeded")

        event_candidate_count = _mapping_nonnegative_int(event_summary, "candidate_count")
        if event_candidate_count is None:
            acceptance_metric_reasons.append("event_candidate_budget_not_collected")
        elif (
            frame_count > 0
            and event_candidate_count * 100.0 / frame_count > thresholds["maximum_event_candidates_per_100_frames"]
        ):
            noisy = True
            reasons.append("event_candidate_budget_exceeded")

        if noisy:
            reasons.append("trajectory_noisy")
        if unstable:
            reasons.append("trajectory_unstable")

    hard_signal_failure = any(
        reason in reasons
        for reason in (
            "zero_candidate",
            "all_candidates_class_rejected",
            "all_candidates_filtered",
            "zero_tracklet",
            "all_lost",
        )
    )
    acceptance_metrics_complete = coverage_complete and not acceptance_metric_reasons
    reasons.extend(acceptance_metric_reasons)
    trajectory_acceptable = coverage_complete and not hard_signal_failure and not noisy and not unstable
    signal_acceptable = trajectory_acceptable and acceptance_metrics_complete

    evidence_states = evidence if isinstance(evidence, dict) else {}
    required_evidence = {
        "wide_context": "available",
        "tight_crop": "available",
        "follow_cam": "available" if enable_follow_cam else "not_applicable",
        "follow_cam_action_retention": "complete" if enable_follow_cam else "not_applicable",
        "scale_strata": "complete",
        "lighting_strata": "complete",
        "attack_transition_windows": "complete",
        "media_integrity": "complete",
        "identity_binding": "complete",
    }
    missing_evidence = [name for name, expected in required_evidence.items() if evidence_states.get(name) != expected]
    reasons.extend(f"evidence_not_collected:{name}" for name in missing_evidence)
    evidence_available = not missing_evidence
    if not acceptance_contract_complete:
        reasons.append("acceptance_contract_not_collected")
    quality_acceptable = signal_acceptable and evidence_available and acceptance_contract_complete
    if quality_acceptable:
        failure_code = "acceptable"
        reasons.append("quality_thresholds_passed")
    elif trajectory_acceptable and (
        not acceptance_metrics_complete or not evidence_available or not acceptance_contract_complete
    ):
        failure_code = "insufficient_evidence"
    elif coverage_complete and failure_code == "insufficient_evidence":
        failure_code = "wrong_or_noisy_candidates" if noisy else "unstable_tracking"
    elif (
        coverage_complete
        and noisy
        and failure_code
        not in {
            "no_raw_candidates",
            "all_candidates_class_rejected",
            "all_candidates_filtered",
            "no_tracklets",
            "all_lost",
        }
    ):
        failure_code = "wrong_or_noisy_candidates"

    status = (
        "acceptable"
        if quality_acceptable
        else (
            "insufficient_evidence"
            if not coverage_complete
            or (
                trajectory_acceptable
                and (not acceptance_metrics_complete or not evidence_available or not acceptance_contract_complete)
            )
            else "retune_required"
        )
    )
    diagnostics = _build_trial_diagnostics(
        raw_track=raw_track,
        cleaned_track=cleaned_track,
        stage_counts=stage_counts,
        ai_review_summary=ai_review_summary,
        event_summary=event_summary,
        follow_cam_summary=follow_cam_summary,
    )
    return {
        "schema_version": TRIAL_SIGNAL_GATE_SCHEMA_VERSION,
        "status": status,
        "coverage_complete": coverage_complete,
        "evidence_available": evidence_available,
        "signal_acceptable": signal_acceptable,
        "trajectory_acceptable": trajectory_acceptable,
        "acceptance_metrics_complete": acceptance_metrics_complete,
        "acceptance_contract_complete": acceptance_contract_complete,
        "quality_acceptable": quality_acceptable,
        "operator_confirmation_required": True,
        "reason_codes": _deduplicate(reasons),
        "failure_classification": _failure_classification(failure_code),
        "threshold_profile": profile,
        "stage_counts": deepcopy(stage_counts) if isinstance(stage_counts, dict) else None,
        "trajectory": {
            "raw": deepcopy(raw_track) if isinstance(raw_track, dict) else None,
            "cleaned": deepcopy(cleaned_track) if isinstance(cleaned_track, dict) else None,
            "audit": deepcopy(audit_summary) if isinstance(audit_summary, dict) else None,
            "follow_cam": deepcopy(follow_cam_summary) if isinstance(follow_cam_summary, dict) else None,
        },
        "diagnostics": diagnostics,
        "evidence": dict(evidence_states),
    }


def build_trial_diagnosis(
    output_dir: Path,
    run: dict[str, Any],
    *,
    metrics_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    report = metrics_report if isinstance(metrics_report, dict) else _read_json(output_dir / "metrics_report.json")
    report = report if isinstance(report, dict) else {}
    tracks = report.get("tracks") if isinstance(report.get("tracks"), dict) else {}
    raw = tracks.get("raw") if isinstance(tracks.get("raw"), dict) else None
    cleaned = tracks.get("cleaned") if isinstance(tracks.get("cleaned"), dict) else None
    audit = report.get("ball_audit") if isinstance(report.get("ball_audit"), dict) else None
    full_audit = _read_json(output_dir / "ball_audit.json")
    raw_tracklet_count = _raw_audit_tracklet_count(full_audit)
    contract = _read_json(output_dir / TRACKING_CONTRACT_NAME)
    stored_stages = report.get("detection_stages")
    stages = (
        stored_stages
        if isinstance(stored_stages, dict) and not _safe_direct_file(output_dir, output_dir / "debug.jsonl")
        else collect_trial_stage_counts(
            output_dir,
            contract,
            tracklet_count=raw_tracklet_count,
            raw_track=raw,
        )
    )
    decoder_failure = _decoder_failed(run, report)
    enable_postprocess, postprocess_conflict = _trial_option(
        run,
        "enable_postprocess",
        default=True,
    )
    enable_follow_cam, follow_cam_conflict = _trial_option(
        run,
        "enable_follow_cam",
        default=True,
    )
    evidence = _evidence_status(output_dir)
    if not enable_follow_cam:
        evidence["follow_cam"] = "not_applicable"
        evidence["follow_cam_action_retention"] = "not_applicable"
    gate = build_trial_signal_gate_v2(
        run_status=str(run.get("status") or "unknown"),
        raw_track=raw,
        cleaned_track=cleaned,
        stage_counts=stages,
        audit_summary=audit,
        raw_tracklet_count=raw_tracklet_count,
        follow_cam_summary=report.get("follow_cam") if isinstance(report.get("follow_cam"), dict) else None,
        decoder_failure=decoder_failure,
        evidence=evidence,
        ai_review_summary=_validated_ai_review_budget(output_dir),
        event_summary=_validated_event_candidate_budget(output_dir),
        enable_postprocess=enable_postprocess,
        enable_follow_cam=enable_follow_cam,
        option_conflicts=[
            name
            for name, conflict in (
                ("postprocess", postprocess_conflict),
                ("follow_cam", follow_cam_conflict),
            )
            if conflict
        ],
    )
    quality_gate = report.get("quality_gate")
    legacy_status = quality_gate.get("status") if isinstance(quality_gate, dict) else None
    return {
        "schema_version": "1.0",
        "run_id": str(run.get("run_id") or ""),
        "legacy_quality_gate_status": legacy_status if isinstance(legacy_status, str) else None,
        "trial_signal_gate_v2": gate,
        "tuning_schema_version": TRIAL_TUNING_SCHEMA_VERSION,
    }


def _failure_classification(code: str) -> dict[str, str]:
    detail = {
        "insufficient_evidence": (
            "blocking",
            "Metrics are incomplete or inconsistent.",
            "Collect or repair the missing evidence, then rerun the bounded interval.",
        ),
        "decode_failure": (
            "blocking",
            "The source could not be decoded completely.",
            "Repair decoding or use a verified sequential decode before tuning the detector.",
        ),
        "no_raw_candidates": (
            "blocking",
            "The detector produced no ball candidates.",
            "Adjust detector sensitivity or inference mode and rerun; model comparison is added in T2.",
        ),
        "all_candidates_class_rejected": (
            "blocking",
            "The detector produced objects, but none matched an allowed ball label.",
            "Review the allowed ball labels for this model and rerun the bounded interval.",
        ),
        "all_candidates_filtered": (
            "blocking",
            "Candidates existed but every candidate was filtered out.",
            "Inspect rejection reasons, relax bounded filters, and rerun.",
        ),
        "no_tracklets": (
            "blocking",
            "Candidates did not form a usable tracklet.",
            "Adjust selection or tracking gates and rerun.",
        ),
        "all_lost": (
            "blocking",
            "Every evaluated frame is lost.",
            "Return to diagnosis and adjust the earliest failing stage.",
        ),
        "wrong_or_noisy_candidates": (
            "high",
            "The candidate or track evidence is dominated by short or suspicious signals.",
            "Tighten detector/filter priors and inspect false-positive examples before rerunning.",
        ),
        "unstable_tracking": (
            "high",
            "The trajectory is partial or unstable.",
            "Adjust selection, tracking, or postprocess controls and rerun.",
        ),
        "acceptable": (
            "none",
            "The signal thresholds pass.",
            "Inspect the playable evidence and explicitly confirm it before acceptance.",
        ),
    }
    severity, summary, action = detail.get(code, detail["insufficient_evidence"])
    return {"code": code, "severity": severity, "summary": summary, "recommended_action": action}


def _valid_track_metrics(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    frame_count = _nonnegative_int(value.get("frame_count"))
    detected = _nonnegative_int(value.get("detected"))
    predicted = _nonnegative_int(value.get("predicted"))
    lost = _nonnegative_int(value.get("lost"))
    if None in (frame_count, detected, predicted, lost):
        return False
    assert frame_count is not None and detected is not None and predicted is not None and lost is not None
    if detected + predicted + lost != frame_count:
        return False
    for name, count in (("detected_ratio", detected), ("predicted_ratio", predicted), ("lost_ratio", lost)):
        ratio = _finite_number(value.get(name))
        expected = round(count / frame_count, 4) if frame_count else 0.0
        if ratio is None or ratio < 0 or ratio > 1 or abs(ratio - expected) > 0.0001:
            return False
    return True


def _required_track_metric_reasons(value: dict[str, Any], prefix: str) -> list[str]:
    reasons: list[str] = []
    for name in ("longest_lost_streak", "false_positive_island_count"):
        if _nonnegative_int(value.get(name)) is None:
            reasons.append(f"{prefix}_metric_not_collected:{name}")
    detected = _nonnegative_int(value.get("detected")) or 0
    predicted = _nonnegative_int(value.get("predicted")) or 0
    max_step = value.get("max_step_px")
    if detected + predicted >= 2 and _finite_number(max_step) is None:
        reasons.append(f"{prefix}_metric_not_collected:max_step_px")
    elif max_step is not None and _finite_number(max_step) is None:
        reasons.append(f"{prefix}_metric_invalid:max_step_px")
    return reasons


def _track_threshold_outcome(value: dict[str, Any], thresholds: dict[str, Any]) -> tuple[bool, bool, bool]:
    frame_count = _nonnegative_int(value.get("frame_count")) or 0
    false_positive_islands = _nonnegative_int(value.get("false_positive_island_count"))
    noisy = bool(
        frame_count > 0
        and false_positive_islands is not None
        and false_positive_islands * 100.0 / frame_count > thresholds["maximum_false_positive_islands_per_100_frames"]
    )
    detected_ratio = _finite_number(value.get("detected_ratio"))
    predicted_ratio = _finite_number(value.get("predicted_ratio"))
    lost_ratio = _finite_number(value.get("lost_ratio"))
    longest_lost = _nonnegative_int(value.get("longest_lost_streak"))
    max_step = _finite_number(value.get("max_step_px"))
    partial = bool(detected_ratio is not None and detected_ratio < thresholds["minimum_detected_ratio"])
    unstable = (
        partial
        or bool(predicted_ratio is not None and predicted_ratio > thresholds["maximum_predicted_ratio"])
        or bool(lost_ratio is not None and lost_ratio > thresholds["maximum_lost_ratio"])
        or bool(longest_lost is not None and longest_lost > thresholds["maximum_longest_lost_streak"])
        or bool(max_step is not None and max_step > thresholds["maximum_step_px"])
    )
    return noisy, unstable, partial


def _required_audit_metric_reasons(value: dict[str, Any] | None) -> list[str]:
    if not isinstance(value, dict):
        return ["audit_not_collected"]
    reasons: list[str] = []
    for name in ("tracklet_count", "suspicious_tracklet_count", "review_event_count", "lost_gap_count"):
        if _nonnegative_int(value.get(name)) is None:
            reasons.append(f"audit_metric_not_collected:{name}")
    return reasons


def _trial_option(run: dict[str, Any], name: str, *, default: bool) -> tuple[bool, bool]:
    note_value: bool | None = None
    notes = run.get("notes")
    if isinstance(notes, str):
        try:
            parsed = json.loads(notes)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get(name), bool):
            note_value = parsed[name]
    modules = run.get("modules_enabled")
    module_name = name.removeprefix("enable_")
    if isinstance(modules, dict) and isinstance(modules.get(module_name), bool):
        authoritative = modules[module_name]
        return authoritative, note_value is not None and note_value is not authoritative
    return (note_value if note_value is not None else default), False


def _decoder_failed(run: dict[str, Any], report: dict[str, Any]) -> bool:
    error = str(run.get("error") or "").lower()
    if run.get("status") == "failed" and any(
        token in error for token in ("decode", "decoder", "ffmpeg", "capture", "video frame")
    ):
        return True
    temporal = report.get("temporal_chunks")
    if not isinstance(temporal, dict):
        return False
    return temporal.get("execution_status") in {"failed", "decode_failed"} or temporal.get("stitch_status") in {
        "failed",
        "decode_failed",
    }


def _validated_ai_review_budget(output_dir: Path) -> dict[str, int] | None:
    report = _read_json(output_dir / "ai_review_triggers.json")
    if not isinstance(report, dict) or report.get("schema_version") != "1.0":
        return None
    decision = report.get("decision")
    summary = report.get("summary")
    triggers = report.get("triggers")
    trigger_count = _mapping_nonnegative_int(decision, "trigger_count")
    if (
        trigger_count is None
        or not isinstance(summary, dict)
        or not isinstance(triggers, list)
        or not all(isinstance(item, dict) for item in triggers)
        or trigger_count != len(triggers)
    ):
        return None
    return {"trigger_count": trigger_count}


def _validated_event_candidate_budget(output_dir: Path) -> dict[str, int] | None:
    report = _read_json(output_dir / "event_candidates.json")
    if not isinstance(report, dict) or report.get("schema_version") != "1.0":
        return None
    summary = report.get("summary")
    candidates = report.get("candidates")
    candidate_count = _mapping_nonnegative_int(summary, "candidate_count")
    frame_count = _mapping_nonnegative_int(summary, "frame_count")
    if (
        candidate_count is None
        or frame_count is None
        or not isinstance(candidates, list)
        or not all(isinstance(item, dict) for item in candidates)
        or candidate_count != len(candidates)
    ):
        return None
    return {"candidate_count": candidate_count, "frame_count": frame_count}


def _evidence_status(output_dir: Path) -> dict[str, str]:
    def available(*names: str) -> str:
        return "available" if any(_nonempty_file(output_dir / name) for name in names) else "not_collected"

    return {
        "wide_context": available("annotated.mp4", "annotated.cleaned.mp4"),
        # Ordinary output frames are not evidence that a tight crop shows the
        # selected ball. T5 supplies a source-bound tight-crop evidence contract.
        "tight_crop": "not_collected",
        "follow_cam": available("follow_cam.mp4", "follow_cam.stable.mp4"),
        "follow_cam_action_retention": "not_collected",
        "scale_strata": "not_collected",
        "lighting_strata": "not_collected",
        "attack_transition_windows": "not_collected",
        "media_integrity": "not_collected",
        "identity_binding": "not_collected",
    }


def _follow_cam_motion(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    audit = value.get("camera_motion_audit")
    if not isinstance(audit, dict):
        return None
    summary = audit.get("summary")
    return summary if isinstance(summary, dict) else None


def _exceeds_optional(value: Any, threshold: float) -> bool:
    parsed = _finite_number(value)
    return parsed is not None and parsed > threshold


def _valid_rejection_reasons(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(reason, str) and bool(reason) and _nonnegative_int(count) is not None
        for reason, count in value.items()
    )


def _mapping_nonnegative_int(value: Any, key: str) -> int | None:
    return _nonnegative_int(value.get(key)) if isinstance(value, dict) else None


def _raw_audit_tracklet_count(value: dict[str, Any] | None) -> int | None:
    sources = value.get("sources") if isinstance(value, dict) else None
    if not isinstance(sources, list):
        return None
    raw_sources = [source for source in sources if isinstance(source, dict) and source.get("name") == "raw"]
    if len(raw_sources) != 1:
        return None
    return _mapping_nonnegative_int(raw_sources[0], "tracklet_count")


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _nested_value(value: Any, key: str, nested_key: str) -> Any:
    nested = value.get(key) if isinstance(value, dict) else None
    return nested.get(nested_key) if isinstance(nested, dict) else None


def _nonempty_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink() and path.stat().st_size > 0
    except OSError:
        return False


def _read_json(path: Path) -> dict[str, Any] | None:
    if not _safe_direct_file(path.parent, path):
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _safe_direct_file(root: Path, path: Path) -> bool:
    try:
        return (
            path.parent.resolve(strict=True) == root.resolve(strict=True) and path.is_file() and not path.is_symlink()
        )
    except OSError:
        return False
