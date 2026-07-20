from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import inspect
import json
import math
import mimetypes
import os
import re
import shutil
import stat
import tempfile
import threading
import unicodedata
import weakref
from collections.abc import Callable
from concurrent.futures import CancelledError
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

import cv2
import numpy as np
import yaml

from football_tracking.action_signal import (
    ACTION_SIGNAL_DIAGNOSTICS_NAME,
    ACTION_SIGNAL_MAX_TERMINAL_SHORTFALL_FRAMES,
    ACTION_SIGNAL_MAX_TERMINAL_SHORTFALL_SECONDS,
    ACTION_SIGNAL_REPORT_NAME,
    ACTION_SIGNAL_SUCCESS_STATUSES,
    ACTION_SIGNAL_TERMINAL_SHORTFALL_LIMITATION,
    ACTION_SIGNAL_TERMINAL_SHORTFALL_REASON,
    ACTION_SIGNAL_TERMINAL_SHORTFALL_STATUS,
    ACTION_TRACK_NAME,
    ActionCalibration,
    generate_action_track,
    validate_calibration_for_video,
)
from football_tracking.ai_candidate_lifecycle import build_ai_candidate_lifecycle
from football_tracking.ai_improvement import (
    APPROVED_ACTIONS_FILE_NAME,
    APPROVED_CONFIG_PATCH_FILE_NAME,
    FOLLOW_CAM_RERENDER_PLAN_FILE_NAME,
    approve_ai_improvement_actions,
    compact_ai_improvement_summary,
    write_ai_improvement_report,
)
from football_tracking.ai_review_triggers import compact_ai_review_trigger_summary
from football_tracking.api.ai_provider import OpenAIResponsesClient, load_provider_settings
from football_tracking.api.broadcast_api import (
    TERMINAL_TAIL_REVIEW_NAME,
    BroadcastApiError,
    build_review_action_envelope,
    build_terminal_tail_review_acknowledgement,
    collect_review_evidence_paths,
    inspect_terminal_tail_review,
    load_bound_json,
    publish_broadcast_facade,
    publish_json_exclusive,
    sha256_file,
    validate_broadcast_quality_report,
    validate_review_queue_activation,
    validate_review_queue_bindings,
)
from football_tracking.ball_annotation_service import BallAnnotationService
from football_tracking.ball_audit import compact_ball_audit_summary
from football_tracking.broadcast_hybrid_orchestration import (
    PUBLIC_ARTIFACTS,
    BroadcastHybridOrchestrationError,
    preflight_recompute_reviewed_trajectory,
    preflight_render_broadcast_trajectory,
    recompute_reviewed_trajectory,
    render_broadcast_trajectory,
    rollback_uncommitted_final_public_artifacts,
)
from football_tracking.calibration import build_pitch_calibration_from_field_polygon
from football_tracking.chunk_runner import run_high_recall_windows, run_temporal_chunks
from football_tracking.config import DEFAULT_HIGH_RECALL_MAX_TOTAL_FRAMES, AppConfig, load_config
from football_tracking.config_lineage import (
    CONFIG_LINEAGE_CONFLICT,
    CONFIG_LINEAGE_MISMATCH,
    CONFIG_LINEAGE_REQUIRED,
    CONFIG_LINEAGE_UNSAFE,
    ConfigLineageError,
    capture_config_bytes,
    capture_regular_file_stat,
    load_config_lineage_reconfirmation,
    reconfirm_config_lineage,
)
from football_tracking.detector_development import DetectorDevelopmentService
from football_tracking.detector_development_common import (
    WINDOWS_RESERVED_NAMES,
    DetectorDevelopmentError,
    atomic_write_json,
    canonical_sha256,
    exact_regular_tree_snapshot,
    hash_regular_file,
    json_object_from_bytes,
    read_regular_bytes,
    require_safe_id,
    require_sha256,
    require_trusted_relative_path,
    secure_mkdirs,
)
from football_tracking.detector_review_proxy import DetectorReviewProxyCoordinator
from football_tracking.events import compact_event_candidate_summary
from football_tracking.final_artifact_manifest import finalize_ai_candidate
from football_tracking.follow_cam import FollowCamGenerator
from football_tracking.global_ball_trajectory import (
    _close_source_lease_handle,
    _open_source_lease_handle,
)
from football_tracking.high_recall_windows import approved_action_windows_from_report
from football_tracking.highlight_window_validation import build_highlight_window_validation
from football_tracking.highlights import render_highlight_clip
from football_tracking.metrics import (
    build_metrics_report,
    compute_track_metrics,
    stats_from_metrics_report,
    write_run_artifacts,
)
from football_tracking.missing_ball_candidate_executor import (
    apply_localize_recovery_stitches,
    assert_parent_fingerprints_unchanged,
    capture_parent_fingerprints,
    combined_recovery_action_window,
    copy_candidate_inputs,
    missing_ball_candidate_artifacts,
    missing_ball_candidate_output_dir,
    preferred_track_path,
    register_missing_ball_candidate,
    traceable_approval_provenance_ids,
    validate_output_csv_name,
    write_candidate_audit,
    write_missing_ball_candidate_manifest,
    write_recovery_config,
    write_run_manifest_and_metrics_preserving_candidate_audit,
)
from football_tracking.missing_ball_recovery_comparison import write_missing_ball_recovery_comparison
from football_tracking.pipeline import BallTrackingPipeline
from football_tracking.player_tracks import compact_player_tracks_summary
from football_tracking.quality import assess_video_quality
from football_tracking.recovery_stitcher import REPORT_NAME as RECOVERY_STITCH_REPORT_NAME
from football_tracking.recovery_stitcher import stitch_recovery_window
from football_tracking.review_evidence_bundle import (
    PROVISIONER_VERSION,
    ReviewEvidenceBundleError,
    activate_review_evidence_bundle,
    discover_review_evidence_bundles,
    revoke_review_evidence_activation,
)
from football_tracking.tracking_contracts import TRACKING_CONTRACT_REPORT_NAME, normalize_tracking_contract_payload
from football_tracking.trial_diagnosis import (
    build_trial_diagnosis,
    normalize_production_trial_config_patch,
    trial_tuning_schema,
)

_REVIEW_PROXY_PRE_SIDE_EFFECT_RETRYABLE_CODES = frozenset(
    {
        "cancelled",
        "continuation_child_plan_changed",
        "path_unavailable",
        "repair_execution_binding_changed",
        "review_proxy_child_terminal",
        "review_proxy_continuation_failed",
        "review_proxy_failed",
        "review_proxy_worker_died",
        "review_proxy_worker_timeout",
        "service_shutting_down",
        "source_changed",
    }
)
_REVIEW_PROXY_SAME_ATTEMPT_RESUMABLE_CODES = frozenset(
    {
        "invalid_review_proxy_repair_evidence",
    }
)

_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

_LIVE_SERVICE_INSTANCES: weakref.WeakValueDictionary[str, Any] = weakref.WeakValueDictionary()

_READY_BROADCAST_DELIVERY_NAMES = (*PUBLIC_ARTIFACTS, "broadcast_quality_report.json")
_MAX_READY_BROADCAST_DEPENDENCIES = 65_536
_MAX_READY_BROADCAST_DELIVERY_CACHE_ENTRIES = 2
_READY_BROADCAST_COPY_CHUNK_BYTES = 1024 * 1024
_READY_BROADCAST_STRONG_OUTPUT_ROOTS = frozenset({"broadcast_generations", "broadcast_status"})
_ReadyIdentityToken = tuple[int, int, int, int, int, int]
_MAX_DETECTOR_CONTRACT_BYTES = 32 * 1024 * 1024

_CONFIRMED_CONFIG_CHANGED_AFTER_CONFIRMATION = "confirmed_config_changed_after_confirmation"
_CONFIG_LINEAGE_BLOCKERS = frozenset(
    {
        CONFIG_LINEAGE_REQUIRED,
        CONFIG_LINEAGE_UNSAFE,
        CONFIG_LINEAGE_MISMATCH,
        CONFIG_LINEAGE_CONFLICT,
    }
)


class _ReviewEvidenceTargetContextError(RuntimeError):
    """A stable, fail-closed review-evidence target context failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ArtifactStatusGenerationConflict(RuntimeError):
    """The requested ready-product generation is no longer authoritative."""


@dataclass(frozen=True)
class _ReadyBroadcastFileLease:
    path: Path
    handle: BinaryIO
    stat_token: tuple[int, int, int, int, int]


@dataclass(frozen=True)
class _YamlConfigSnapshot:
    path: Path
    content: bytes
    sha256: str
    raw: dict[str, Any]


@dataclass
class _ArtifactResponseLease:
    path: Path
    handle: BinaryIO
    stat_token: tuple[int, int, int, int, int]
    on_close: Callable[[], None] | None = None
    closed: bool = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            _close_source_lease_handle(self.handle)
        finally:
            if self.on_close is not None:
                self.on_close()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


_MISSING_VALUE = object()


def _value_at_dotted_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING_VALUE
        current = current[part]
    return current


def _set_dotted_path(value: dict[str, Any], path: str, item: Any) -> None:
    current = value
    parts = path.split(".")
    for part in parts[:-1]:
        nested = current.get(part)
        if not isinstance(nested, dict):
            nested = {}
            current[part] = nested
        current = nested
    current[parts[-1]] = deepcopy(item)


def _flatten_patch_lines(patch: dict[str, Any], prefix: str = "") -> list[str]:
    lines: list[str] = []
    for key, value in patch.items():
        current_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            lines.extend(_flatten_patch_lines(value, current_key))
        else:
            lines.append(f"{current_key}: {value}")
    return lines


def _normalize_config_explain_path(path: str) -> str:
    return re.sub(r"\[\d+\]", "[]", path)


def _is_point_pair(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple)) and len(value) == 2 and all(isinstance(item, (int, float)) for item in value)
    )


def _is_point_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(_is_point_pair(item) for item in value)


def _format_config_explain_value(value: Any) -> str:
    if _is_point_list(value):
        return f"{len(value)} points"
    if isinstance(value, list):
        if not value:
            return "[]"
        if len(value) <= 8 and all(not isinstance(item, (dict, list, tuple)) for item in value):
            return json.dumps(value, ensure_ascii=False)
        return f"{len(value)} items"
    if isinstance(value, dict):
        return "{}" if not value else f"{len(value)} keys"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _flatten_config_explain_items(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        if not value and prefix:
            return [(prefix, value)]
        items: list[tuple[str, Any]] = []
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            items.extend(_flatten_config_explain_items(child, path))
        return items
    if isinstance(value, list):
        if not value or _is_point_list(value) or not all(isinstance(item, dict) for item in value):
            return [(prefix, value)]
        items = []
        for index, child in enumerate(value):
            items.extend(_flatten_config_explain_items(child, f"{prefix}[{index}]"))
        return items
    return [(prefix, value)]


_CONFIG_EXPLAIN_DESCRIPTIONS_EN: dict[str, str] = {
    "input_video": "Source video read by the tracker.",
    "output_dir": "Directory where videos, CSV tracks, and debug files are written.",
    "logging.level": "Log verbosity for the pipeline.",
    "logging.save_debug_jsonl": "Whether to save per-frame debug records.",
    "detector.model_path": "YOLO ball detector weight file.",
    "detector.device": "Compute device used for inference.",
    "detector.confidence_threshold": "Minimum detector confidence before a box is considered.",
    "detector.image_size": "Detector inference image size. Larger can improve small-ball recall but costs speed.",
    "detector.use_half": "Whether to use half precision on GPU.",
    "detector.allowed_labels": "Detector classes accepted as ball candidates.",
    "sahi.slice_height": "Height of each tiled detection slice.",
    "sahi.slice_width": "Width of each tiled detection slice.",
    "sahi.overlap_height_ratio": "Vertical overlap between slices.",
    "sahi.overlap_width_ratio": "Horizontal overlap between slices.",
    "sahi.perform_standard_pred": "Whether to also run full-frame detection besides tiled detection.",
    "sahi.postprocess_type": "Method used to merge duplicate slice detections.",
    "sahi.postprocess_match_metric": "Metric used when matching duplicate boxes.",
    "sahi.postprocess_match_threshold": "Threshold for merging duplicate boxes.",
    "filtering.min_confidence": "Minimum confidence after detector output filtering.",
    "filtering.roi": "Region where detections are allowed. Null means full frame.",
    "scene_bias.enabled": "Whether field and zone priors affect candidate scoring.",
    "scene_bias.ground_zones[].points": "Polygon for the playable ground area.",
    "scene_bias.negative_rois[].points": "Polygon for areas that should be penalized as likely false positives.",
    "scene_bias.positive_rois[].points": "Polygon for areas that should receive a selection bonus.",
    "scene_bias.dynamic_air_recovery.enabled": "Whether recovery mode relaxes filters while the ball is predicted or lost.",
    "scene_bias.dynamic_air_recovery.reacquire_confidence_threshold": "Minimum confidence for reacquiring a lost ball.",
    "scene_bias.dynamic_air_recovery.reacquire_image_size": "Image size used for reacquire detection.",
    "selection.min_accept_score": "Minimum final score required to accept a candidate.",
    "selection.stable_history_length": "Number of historical frames used to judge stable motion.",
    "tracking.max_lost_frames": "Frames to keep predicting before declaring the ball lost too long.",
    "tracking.match_distance": "Distance gate for matching a detection to the current track.",
    "tracking.max_speed": "Maximum allowed ball speed between frames.",
    "tracking.max_acceleration": "Maximum allowed acceleration before penalizing a candidate.",
    "tracking.prediction_mode": "Motion model used when detection is missing.",
    "tracking.predicted_confidence_decay": "How fast confidence decays while predicting.",
    "output.video_name": "Annotated tracking video file name.",
    "output.csv_name": "Raw ball track CSV file name.",
    "output.debug_jsonl_name": "Per-frame debug JSONL file name.",
    "output.save_video": "Whether to render annotated output video.",
    "output.save_csv": "Whether to save track CSV.",
    "output.save_tracking_contract": "Whether to save the candidate-populated V2 tracking contract.",
    "output.draw_radius": "Radius of the drawn ball marker.",
    "runtime.use_gpu_if_available": "Whether to prefer GPU when available.",
    "runtime.start_frame": "First frame to process.",
    "runtime.max_frames": "Maximum number of frames to process. Null means full video.",
    "postprocess.enabled": "Whether to clean bad track segments after raw tracking.",
    "follow_cam.enabled": "Whether to render a 16:9 follow-cam output after tracking.",
    "follow_cam.target_width": "Final follow-cam video width.",
    "follow_cam.target_height": "Final follow-cam video height.",
    "follow_cam.pan_smoothing": "How smoothly the crop center follows the ball.",
    "follow_cam.zoom_smoothing": "How smoothly crop zoom changes.",
}


def _describe_config_path(path: str, value: Any, language: str) -> str:
    normalized = _normalize_config_explain_path(path)
    if language != "zh":
        if normalized in _CONFIG_EXPLAIN_DESCRIPTIONS_EN:
            return _CONFIG_EXPLAIN_DESCRIPTIONS_EN[normalized]
        if ".weights." in normalized:
            return "Selection scoring weight; higher values make this factor more important."
        if normalized.endswith(".points"):
            return "Polygon points used as a spatial prior."
        if normalized.endswith(".name"):
            return "Human-readable name for this zone or artifact."
        if normalized.startswith("detector."):
            return "Detector setting that affects ball detection quality or speed."
        if normalized.startswith("sahi."):
            return "Tiled detection setting for finding small balls in large frames."
        if normalized.startswith("filtering."):
            return "Candidate filtering rule applied after detection."
        if normalized.startswith("scene_bias."):
            return "Scene prior that biases candidate scoring by field location and recovery state."
        if normalized.startswith("selection."):
            return "Candidate scoring and acceptance rule."
        if normalized.startswith("tracking."):
            return "Temporal tracking and prediction rule."
        if normalized.startswith("output."):
            return "Output artifact or drawing option."
        if normalized.startswith("runtime."):
            return "Runtime control for frame range, hardware, or video IO."
        if normalized.startswith("postprocess."):
            return "Postprocess cleanup option for raw tracks."
        if normalized.startswith("follow_cam."):
            return "Final follow-cam render option."
        return "Config value used by the tracking pipeline."

    if normalized in {
        "input_video",
        "output_dir",
        "detector.model_path",
        "detector.device",
        "detector.confidence_threshold",
        "detector.image_size",
        "filtering.roi",
        "runtime.start_frame",
        "runtime.max_frames",
        "postprocess.enabled",
        "follow_cam.enabled",
    }:
        exact = {
            "input_video": "\u8ffd\u8e2a\u5668\u8bfb\u53d6\u7684\u6e90\u89c6\u9891\u8def\u5f84\u3002",
            "output_dir": "\u8f93\u51fa\u89c6\u9891\u3001\u8f68\u8ff9 CSV \u548c\u8c03\u8bd5\u6587\u4ef6\u7684\u76ee\u5f55\u3002",
            "detector.model_path": "YOLO \u7403\u68c0\u6d4b\u6a21\u578b\u6743\u91cd\u6587\u4ef6\u3002",
            "detector.device": "\u63a8\u7406\u4f7f\u7528\u7684\u8ba1\u7b97\u8bbe\u5907\u3002",
            "detector.confidence_threshold": "\u68c0\u6d4b\u6846\u8fdb\u5165\u5019\u9009\u524d\u9700\u8981\u8fbe\u5230\u7684\u6700\u4f4e\u7f6e\u4fe1\u5ea6\u3002",
            "detector.image_size": "\u68c0\u6d4b\u63a8\u7406\u56fe\u50cf\u5c3a\u5bf8\uff0c\u8d8a\u5927\u8d8a\u53ef\u80fd\u627e\u5230\u5c0f\u7403\uff0c\u4f46\u901f\u5ea6\u66f4\u6162\u3002",
            "filtering.roi": "\u5141\u8bb8\u68c0\u6d4b\u7684\u753b\u9762\u533a\u57df\uff0cnull \u8868\u793a\u6574\u5e27\u3002",
            "runtime.start_frame": "\u5f00\u59cb\u5904\u7406\u7684\u7b2c\u4e00\u5e27\u3002",
            "runtime.max_frames": "\u6700\u591a\u5904\u7406\u7684\u5e27\u6570\uff0cnull \u8868\u793a\u6574\u6bb5\u89c6\u9891\u3002",
            "postprocess.enabled": "\u662f\u5426\u5728\u539f\u59cb\u8ffd\u8e2a\u540e\u6e05\u6d17\u5f02\u5e38\u8f68\u8ff9\u6bb5\u3002",
            "follow_cam.enabled": "\u662f\u5426\u5728\u8ffd\u8e2a\u540e\u6e32\u67d3 16:9 \u8ddf\u968f\u955c\u5934\u6210\u54c1\u3002",
        }
        return exact[normalized]
    if ".weights." in normalized:
        return "\u5019\u9009\u70b9\u7efc\u5408\u8bc4\u5206\u6743\u91cd\uff0c\u6570\u503c\u8d8a\u5927\u8fd9\u4e2a\u56e0\u7d20\u8d8a\u91cd\u8981\u3002"
    if normalized.endswith(".points"):
        return "\u7528\u4f5c\u7a7a\u95f4\u5148\u9a8c\u7684\u591a\u8fb9\u5f62\u70b9\u4f4d\u3002"
    if normalized.endswith(".name"):
        return "\u8fd9\u4e2a\u533a\u57df\u6216\u4ea7\u7269\u7684\u540d\u79f0\u3002"
    if normalized.startswith("detector."):
        return "\u68c0\u6d4b\u5668\u53c2\u6570\uff0c\u5f71\u54cd\u627e\u7403\u8d28\u91cf\u6216\u63a8\u7406\u901f\u5ea6\u3002"
    if normalized.startswith("sahi."):
        return "\u5207\u7247\u68c0\u6d4b\u53c2\u6570\uff0c\u7528\u6765\u5728\u5927\u753b\u9762\u91cc\u627e\u5c0f\u7403\u3002"
    if normalized.startswith("filtering."):
        return "\u68c0\u6d4b\u540e\u7684\u5019\u9009\u8fc7\u6ee4\u89c4\u5219\u3002"
    if normalized.startswith("scene_bias."):
        return "\u573a\u666f\u5148\u9a8c\uff0c\u6309\u7403\u573a\u4f4d\u7f6e\u548c\u627e\u56de\u72b6\u6001\u5f71\u54cd\u5019\u9009\u8bc4\u5206\u3002"
    if normalized.startswith("selection."):
        return "\u5019\u9009\u70b9\u8bc4\u5206\u548c\u63a5\u53d7\u89c4\u5219\u3002"
    if normalized.startswith("tracking."):
        return "\u8de8\u5e27\u8ffd\u8e2a\u3001\u5339\u914d\u548c\u9884\u6d4b\u89c4\u5219\u3002"
    if normalized.startswith("output."):
        return "\u8f93\u51fa\u6587\u4ef6\u6216\u753b\u9762\u6807\u6ce8\u9009\u9879\u3002"
    if normalized.startswith("runtime."):
        return "\u8fd0\u884c\u65f6\u53c2\u6570\uff0c\u63a7\u5236\u5e27\u8303\u56f4\u3001\u786c\u4ef6\u6216\u89c6\u9891 IO\u3002"
    if normalized.startswith("postprocess."):
        return "\u539f\u59cb\u8f68\u8ff9\u7684\u540e\u5904\u7406\u6e05\u6d17\u9009\u9879\u3002"
    if normalized.startswith("follow_cam."):
        return "\u6700\u7ec8\u8ddf\u968f\u955c\u5934\u6e32\u67d3\u9009\u9879\u3002"
    return "\u8ffd\u8e2a\u6d41\u6c34\u7ebf\u4f7f\u7528\u7684\u914d\u7f6e\u503c\u3002"


def _build_config_line_explanations(raw: dict[str, Any], language: str) -> list[str]:
    lines: list[str] = []
    for path, value in _flatten_config_explain_items(raw):
        if not path:
            continue
        current_value = _format_config_explain_value(value)
        description = _describe_config_path(path, value, language)
        lines.append(f"{path} = {current_value} - {description}")
    return lines


def _normalize_ai_language(language: str | None) -> str:
    return "zh" if language == "zh" else "en"


def _localized_text(language: str, *, en: str, zh: str) -> str:
    return zh if language == "zh" else en


def _normalize_iso_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _seconds_since_iso(value: Any) -> float | None:
    normalized = _normalize_iso_timestamp(value)
    if normalized is None:
        return None
    try:
        started_at = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds())


def _localized_run_status(language: str, status: str) -> str:
    labels = {
        "en": {
            "queued": "queued",
            "running": "running",
            "completed": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
        },
        "zh": {
            "queued": "\u6392\u961f\u4e2d",
            "running": "\u8fd0\u884c\u4e2d",
            "completed": "\u5df2\u5b8c\u6210",
            "failed": "\u5931\u8d25",
            "cancelled": "\u5df2\u505c\u6b62",
        },
    }
    return labels[language].get(status, status)


def _validated_api_repo_root(repo_root: Path) -> Path:
    raw = Path(repo_root)
    if raw.drive and not raw.root:
        raise DetectorDevelopmentError("invalid_path", "Repository root is ambiguous")
    for part in raw.parts:
        if part == raw.anchor or part in {".", ".."}:
            continue
        if (
            not part
            or ":" in part
            or part.endswith((" ", "."))
            or any(character in '<>"|?*/\\' for character in part)
            or part.rstrip(" .").split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES
        ):
            raise DetectorDevelopmentError("invalid_path", "Repository root is unsafe")
    absolute = Path(os.path.abspath(raw))
    if not absolute.name:
        raise DetectorDevelopmentError("invalid_path", "Repository root is unsafe")
    return secure_mkdirs(absolute.parent, absolute.name)


class ApiService:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = _validated_api_repo_root(repo_root)
        self.config_dir = self.repo_root / "config"
        self.outputs_dir = self.repo_root / "outputs"
        self.run_outputs_dir = self.outputs_dir / "runs"
        self.review_evidence_inbox_dir = self.outputs_dir / "review_evidence_inbox"
        self.target_prelabel_commitment_registry = self.outputs_dir / "target_prelabel_commitments"
        self.data_dir = self.repo_root / "data"
        self.registry_path = self.repo_root / "data" / "run_registry.json"
        self.registry_lock_path = self.repo_root / "data" / "run_registry.lock"
        self.service_lease_dir = self.repo_root / "data" / "service_leases"
        self.generated_config_dir = self.config_dir / "generated"
        self._lock = threading.Lock()
        self._detector_development_lock = threading.Lock()
        self._detector_development: DetectorDevelopmentService | None = None
        self._ball_annotation_lock = threading.Lock()
        self._ball_annotation: BallAnnotationService | None = None
        self._detector_review_proxy_lock = threading.RLock()
        self._detector_review_proxy: DetectorReviewProxyCoordinator | None = None
        repair_root = secure_mkdirs(
            self.repo_root,
            "data",
            "ball_detector_development_v1",
            "review_proxy_continuations",
        )
        self._detector_review_proxy_jobs_root = secure_mkdirs(repair_root, "jobs")
        self._detector_review_proxy_failpoint: Callable[[str], None] | None = None
        self._instance_id = uuid4().hex
        self._service_lease_path = self.service_lease_dir / f"{self._instance_id}.lock"
        self._service_lease_handle = self._acquire_service_lease()
        self._service_lease_finalizer = weakref.finalize(
            self,
            self._release_service_lease_resources,
            self._service_lease_handle,
            self._service_lease_path,
        )
        _LIVE_SERVICE_INSTANCES[self._instance_id] = self
        self._active_threads: dict[str, threading.Thread] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._starting_threads: set[str] = set()
        self._closing = False
        self._lease_waiter_started = False
        self._ready_broadcast_delivery_lock = threading.RLock()
        self._ready_broadcast_delivery_cache: dict[tuple[Path, str], dict[str, Any]] = {}
        self._ready_broadcast_active_responses = 0
        self._ready_broadcast_cleanup_pending = False
        self._ready_broadcast_retired_snapshot_dirs: set[Path] = set()
        self._ready_broadcast_delivery_temp: Path | None = Path(
            tempfile.mkdtemp(prefix=f"football-tracking-ready-{self._instance_id[:8]}-")
        )
        self._ready_broadcast_delivery_temp_finalizer = weakref.finalize(
            self,
            shutil.rmtree,
            self._ready_broadcast_delivery_temp,
            ignore_errors=True,
        )
        try:
            self.provider_settings = load_provider_settings(self.repo_root)
            self.ai_client = OpenAIResponsesClient(self.provider_settings)
            self._ensure_registry_file()
            self._recover_interrupted_broadcast_operations()
            self._recover_interrupted_review_evidence_imports()
            self._recover_review_evidence_revocations()
            self._recover_detector_review_proxy_repairs()
        except BaseException:
            self.close()
            raise

    def close(self, *, timeout: float = 5.0) -> None:
        """Release this service instance's cross-process ownership lease."""

        with self._lock:
            self._closing = True
            for cancel_event in self._cancel_events.values():
                cancel_event.set()
            registered_threads = list(self._active_threads.values())
        with self._detector_development_lock:
            detector_development = self._detector_development
            self._detector_development = None
        with self._ball_annotation_lock:
            ball_annotation = self._ball_annotation
            self._ball_annotation = None
        with self._detector_review_proxy_lock:
            detector_review_proxy = self._detector_review_proxy
            self._detector_review_proxy = None
        if ball_annotation is not None:
            ball_annotation.close()
        if detector_development is not None:
            detector_development.close()
        if detector_review_proxy is not None:
            detector_review_proxy.close()
        current_thread = threading.current_thread()
        for thread in registered_threads:
            if thread is not current_thread and thread.is_alive():
                thread.join(timeout=max(0.0, timeout))
        with self._lock:
            self._prune_inactive_registered_threads_locked()
            if self._active_threads:
                if not self._lease_waiter_started:
                    self._lease_waiter_started = True
                    waiter = threading.Thread(
                        target=self._wait_for_workers_and_release_lease,
                        name=f"football-tracking-service-close-{self._instance_id[:8]}",
                        daemon=True,
                    )
                    waiter.start()
                return
        self._release_service_lease()

    def _wait_for_workers_and_release_lease(self) -> None:
        current_thread = threading.current_thread()
        while True:
            with self._lock:
                self._prune_inactive_registered_threads_locked()
                workers = list(self._active_threads.values())
            if not workers:
                break
            joined_worker = False
            for worker in workers:
                if worker is not current_thread and worker.is_alive():
                    worker.join()
                    joined_worker = True
            if not joined_worker:
                threading.Event().wait(0.01)
        self._release_service_lease()

    def _prune_inactive_registered_threads_locked(self) -> None:
        for run_id, thread in list(self._active_threads.items()):
            if run_id in self._starting_threads:
                continue
            if getattr(thread, "ident", None) is None or not thread.is_alive():
                self._active_threads.pop(run_id, None)
                self._cancel_events.pop(run_id, None)

    def _release_service_lease(self) -> None:
        self._release_ready_broadcast_delivery_resources()
        _LIVE_SERVICE_INSTANCES.pop(self._instance_id, None)
        finalizer = getattr(self, "_service_lease_finalizer", None)
        if finalizer is not None and finalizer.alive:
            finalizer()

    def _release_ready_broadcast_delivery_resources(self) -> None:
        lock = getattr(self, "_ready_broadcast_delivery_lock", None)
        if lock is None:
            return
        with lock:
            cache = getattr(self, "_ready_broadcast_delivery_cache", {})
            entries = list(cache.values())
            cache.clear()
            temp_finalizer = getattr(self, "_ready_broadcast_delivery_temp_finalizer", None)
            self._ready_broadcast_delivery_temp = None
            for entry in entries:
                self._release_ready_broadcast_cache_entry(entry)
            if self._ready_broadcast_active_responses:
                self._ready_broadcast_cleanup_pending = True
                return
            self._cleanup_retired_ready_broadcast_snapshots()
        if temp_finalizer is not None and temp_finalizer.alive:
            temp_finalizer()

    def _invalidate_ready_broadcast_delivery_cache(
        self,
        *,
        output_dir: Path | None = None,
        dependency_path: Path | None = None,
    ) -> None:
        normalized_output = Path(os.path.abspath(output_dir)) if output_dir is not None else None
        normalized_dependency = Path(os.path.abspath(dependency_path)) if dependency_path is not None else None
        with self._ready_broadcast_delivery_lock:
            invalidated: list[dict[str, Any]] = []
            for key, entry in list(self._ready_broadcast_delivery_cache.items()):
                dependencies = entry.get("dependencies", ())
                dependency_tokens = entry.get("dependency_tokens", {})
                matches_output = normalized_output is not None and key[0] == normalized_output
                matches_dependency = normalized_dependency is not None and (
                    normalized_dependency in dependency_tokens
                    or any(
                        isinstance(lease, _ReadyBroadcastFileLease) and lease.path == normalized_dependency
                        for lease in dependencies
                    )
                )
                if not (matches_output or matches_dependency):
                    continue
                invalidated.append(self._ready_broadcast_delivery_cache.pop(key))
            for entry in invalidated:
                self._release_ready_broadcast_cache_entry(entry)

    def _release_ready_broadcast_cache_entry(self, entry: dict[str, Any]) -> None:
        self._release_ready_broadcast_file_leases(entry.get("dependencies", ()))
        self._release_ready_broadcast_file_leases(entry.get("snapshot_leases", ()))
        snapshot_manifest = entry.get("snapshot_manifest")
        if not isinstance(snapshot_manifest, Path):
            return
        snapshot_dir = snapshot_manifest.parent
        # An active response owns its own file handle. POSIX can unlink that
        # snapshot immediately; Windows will leave only the actually leased
        # directory behind for a later retry. Do not retain unrelated evictions
        # merely because some other response is active.
        shutil.rmtree(snapshot_dir, ignore_errors=True)
        if snapshot_dir.exists():
            self._ready_broadcast_retired_snapshot_dirs.add(snapshot_dir)

    def _cleanup_retired_ready_broadcast_snapshots(self) -> None:
        for snapshot_dir in list(self._ready_broadcast_retired_snapshot_dirs):
            shutil.rmtree(snapshot_dir, ignore_errors=True)
            if not snapshot_dir.exists():
                self._ready_broadcast_retired_snapshot_dirs.discard(snapshot_dir)

    def _ready_broadcast_response_released(self) -> None:
        temp_finalizer: Any = None
        with self._ready_broadcast_delivery_lock:
            self._ready_broadcast_active_responses = max(0, self._ready_broadcast_active_responses - 1)
            if self._ready_broadcast_active_responses:
                return
            self._cleanup_retired_ready_broadcast_snapshots()
            if self._ready_broadcast_cleanup_pending:
                self._ready_broadcast_cleanup_pending = False
                temp_finalizer = getattr(self, "_ready_broadcast_delivery_temp_finalizer", None)
        if temp_finalizer is not None and temp_finalizer.alive:
            temp_finalizer()

    def health_summary(self) -> dict[str, Any]:
        runs = self.list_runs()
        active_run = next((run["run_id"] for run in runs if run["status"] in {"queued", "running"}), None)
        return {
            "status": "ok",
            "active_run_id": active_run,
            "config_count": len(self.list_configs()),
            "run_count": len(runs),
        }

    def list_detector_models(self) -> dict[str, Any]:
        return self._detector_development_service().list_models()

    def import_detector_model(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._detector_development_service().import_model(request)

    def create_detector_probe(
        self,
        request: dict[str, Any],
        *,
        _annotation_check_session_id: str | None = None,
        _annotation_sampling_manifest_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Resolve all probe authority from one completed production-trial record."""

        if (_annotation_check_session_id is None) != (_annotation_sampling_manifest_sha256 is None):
            raise DetectorDevelopmentError(
                "invalid_annotation_check_authority",
                "Server-owned annotation check authority is incomplete",
                status_code=409,
            )
        annotation_check_authority = None
        annotation_check_profile_sha256s = None
        if _annotation_check_session_id is not None:
            annotation_check_authority = self._ball_annotation_service().authorize_check_probe_creation(
                _annotation_check_session_id
            )
            supplied_manifest_sha256 = require_sha256(
                _annotation_sampling_manifest_sha256,
                "annotation sampling manifest sha256",
            )
            authority_payload = deepcopy(annotation_check_authority)
            authority_sha256 = authority_payload.pop("authority_sha256", None)
            profile_bindings = annotation_check_authority.get("profile_bindings")
            if isinstance(profile_bindings, list) and len(profile_bindings) == 2:
                try:
                    checked_profile_bindings = [
                        (
                            require_safe_id(binding.get("profile_id"), "annotation profile_id"),
                            require_sha256(binding.get("profile_sha256"), "annotation profile sha256"),
                        )
                        for binding in profile_bindings
                        if isinstance(binding, dict)
                    ]
                except DetectorDevelopmentError:
                    checked_profile_bindings = []
                if len(checked_profile_bindings) == len(profile_bindings) and [
                    item[0] for item in checked_profile_bindings
                ] == sorted({item[0] for item in checked_profile_bindings}):
                    annotation_check_profile_sha256s = dict(checked_profile_bindings)
            if (
                annotation_check_authority.get("session_id") != _annotation_check_session_id
                or annotation_check_authority.get("sampling_manifest_sha256") != supplied_manifest_sha256
                or not isinstance(authority_sha256, str)
                or canonical_sha256(authority_payload) != authority_sha256
                or not isinstance(annotation_check_profile_sha256s, dict)
            ):
                raise DetectorDevelopmentError(
                    "invalid_annotation_check_authority",
                    "Persisted annotation check authority does not match the server request",
                    status_code=409,
                )

        allowed_request_fields = {
            "parent_trial_id",
            "profile_ids",
            "frame_indices",
            "top_k",
            "retry_from_job_id",
        }
        unexpected_fields = sorted(set(request) - allowed_request_fields)
        if unexpected_fields:
            raise DetectorDevelopmentError(
                "forged_probe_authority",
                "Public detector probe requests cannot provide source, contract, config, or runtime authority",
                status_code=400,
            )
        if request.get("top_k", 5) != 5:
            raise DetectorDevelopmentError("invalid_top_k", "Detector probe top_k is fixed at 5", status_code=400)
        parent_trial_id = require_safe_id(request.get("parent_trial_id"), "parent_trial_id")
        try:
            parent = self.get_run(parent_trial_id)
        except KeyError:
            raise
        if parent.get("status") != "completed":
            raise DetectorDevelopmentError(
                "parent_trial_not_completed",
                "Detector probes require a completed parent production trial",
                status_code=409,
            )
        if parent.get("source") != "api":
            raise DetectorDevelopmentError(
                "invalid_parent_trial_source",
                "Detector probes require an API-owned production trial",
                status_code=400,
            )
        note = self._machine_run_note(parent.get("notes"))
        if not isinstance(note, dict) or note.get("purpose") != "production_trial":
            raise DetectorDevelopmentError(
                "invalid_parent_trial_purpose",
                "Detector probes require a production_trial parent",
                status_code=400,
            )
        try:
            self._validate_production_trial_note_contract(note)
        except ValueError as exc:
            raise DetectorDevelopmentError(
                "invalid_parent_trial_lineage",
                "The parent production trial lineage is incomplete",
                status_code=409,
            ) from exc
        if parent_trial_id != f"production_trial_{note['output_id']}":
            raise DetectorDevelopmentError(
                "parent_trial_identity_mismatch",
                "The parent production trial ID does not match its immutable lineage",
                status_code=409,
            )

        source_path = self._detector_probe_source_path(parent)
        config_snapshot = self._detector_probe_config_snapshot(parent, note, source_path)
        base_config_sha256, base_config_path = self._detector_probe_base_config_binding(config_snapshot.raw)
        resolved_effective_config = config_snapshot.raw
        if _value_at_dotted_path(config_snapshot.raw, "metadata.production_tuning") is not _MISSING_VALUE:
            try:
                resolved_effective_config = _jsonable(
                    load_config(
                        config_snapshot.path,
                        raw_config=config_snapshot.raw,
                    )
                )
            except (OSError, TypeError, ValueError) as exc:
                raise DetectorDevelopmentError(
                    "invalid_parent_tuning_lineage",
                    "The parent tuning lineage could not be resolved for canonical validation",
                    status_code=409,
                ) from exc
        tuning_patch_binding, tuning_patch_sha256 = self._detector_probe_tuning_binding(
            config_snapshot.raw,
            base_config=resolved_effective_config,
        )
        output_dir = self._detector_probe_output_dir(parent)
        contract_path = self._detector_probe_contract_path(output_dir)
        try:
            contract_bytes, contract_sha256 = read_regular_bytes(
                contract_path,
                "production trial tracking contract",
                max_bytes=_MAX_DETECTOR_CONTRACT_BYTES,
                trusted_root=output_dir,
            )
            raw_contract = json_object_from_bytes(contract_bytes, "production trial tracking contract")
            raw_summary = raw_contract.get("summary")
            contract = normalize_tracking_contract_payload(
                raw_contract,
                path=contract_path,
            )
        except (DetectorDevelopmentError, OSError, ValueError) as exc:
            raise DetectorDevelopmentError(
                "invalid_parent_tracking_contract",
                "The parent production trial tracking contract is unavailable or invalid",
                status_code=409,
            ) from exc
        source_binding = contract.get("source")
        summary = contract.get("summary")
        if (
            contract.get("schema_version") != "2.0"
            or not isinstance(source_binding, dict)
            or not isinstance(summary, dict)
            or summary.get("status") != "ok"
        ):
            raise DetectorDevelopmentError(
                "invalid_parent_tracking_contract",
                "The parent production trial tracking contract is not a successful V2 contract",
                status_code=409,
            )
        source_sha256 = require_sha256(source_binding.get("video_sha256"), "tracking contract source video_sha256")
        source_signature = self._detector_probe_source_signature(config_snapshot.raw)
        try:
            source_stat = source_path.stat()
        except OSError as exc:
            raise DetectorDevelopmentError(
                "parent_source_unavailable",
                "The parent production trial source is unavailable",
                status_code=409,
            ) from exc
        if (
            Path(source_signature["path"]).resolve() != source_path
            or source_signature["size_bytes"] != source_stat.st_size
        ):
            raise DetectorDevelopmentError(
                "parent_source_signature_mismatch",
                "The parent production trial source signature is stale",
                status_code=409,
            )

        contract_frames = contract.get("frames")
        if not isinstance(contract_frames, list):
            raise DetectorDevelopmentError(
                "invalid_parent_tracking_contract",
                "The parent tracking contract has no authoritative frame set",
                status_code=409,
            )
        authoritative_frames: list[int] = []
        for item in contract_frames:
            frame_index = item.get("frame_index") if isinstance(item, dict) else None
            if isinstance(frame_index, bool) or not isinstance(frame_index, int) or frame_index < 0:
                raise DetectorDevelopmentError(
                    "invalid_parent_tracking_contract",
                    "The parent tracking contract frame set is invalid",
                    status_code=409,
                )
            authoritative_frames.append(frame_index)
        expected_trial_frames = list(range(note["start_frame"], note["start_frame"] + note["max_frames"]))
        if (
            not authoritative_frames
            or authoritative_frames != expected_trial_frames
            or len(set(authoritative_frames)) != len(authoritative_frames)
            or not isinstance(raw_summary, dict)
            or raw_summary.get("frame_count") != note["max_frames"]
        ):
            raise DetectorDevelopmentError(
                "invalid_parent_tracking_contract",
                "The parent tracking contract does not exactly bind the production trial frame window",
                status_code=409,
            )
        requested_frames = request.get("frame_indices")
        annotation_check = annotation_check_authority is not None
        if requested_frames is None:
            if annotation_check:
                raise DetectorDevelopmentError(
                    "invalid_annotation_check_frames",
                    "Server-owned annotation checks require an explicit 20-50 frame set",
                    status_code=400,
                )
            frame_indices = self._default_detector_probe_frames(authoritative_frames)
        else:
            frame_indices = list(requested_frames)
            if annotation_check:
                source_frame_count = source_binding.get("frame_count")
                authority_profile_ids = (
                    [binding.get("profile_id") for binding in profile_bindings]
                    if isinstance(profile_bindings, list)
                    and all(isinstance(binding, dict) for binding in profile_bindings)
                    else None
                )
                valid_annotation_check = (
                    isinstance(source_frame_count, int)
                    and not isinstance(source_frame_count, bool)
                    and 20 <= len(frame_indices) <= 50
                    and frame_indices == sorted(set(frame_indices))
                    and all(
                        isinstance(frame_index, int)
                        and not isinstance(frame_index, bool)
                        and 0 <= frame_index < source_frame_count
                        for frame_index in frame_indices
                    )
                    and annotation_check_authority.get("parent_trial_id") == parent_trial_id
                    and annotation_check_authority.get("source_sha256") == source_sha256
                    and annotation_check_authority.get("source_frame_count") == source_frame_count
                    and annotation_check_authority.get("frame_indices") == frame_indices
                    and authority_profile_ids == request.get("profile_ids")
                )
                if not valid_annotation_check:
                    raise DetectorDevelopmentError(
                        "invalid_annotation_check_frames",
                        "Server-owned annotation check frames must be 20-50 unique sorted source frames",
                        status_code=400,
                    )
            elif not set(frame_indices).issubset(authoritative_frames):
                raise DetectorDevelopmentError(
                    "probe_frames_outside_parent_trial",
                    "Detector probe frames must come from the authoritative parent trial",
                    status_code=400,
                )

        try:
            self._verify_materialized_config_snapshot(config_snapshot)
        except ValueError as exc:
            raise DetectorDevelopmentError(
                "parent_config_changed",
                "The parent production trial configuration changed during probe creation",
                status_code=409,
            ) from exc
        internal_request: dict[str, Any] = {
            "parent_trial_id": parent_trial_id,
            "source_id": f"sha256-{source_sha256[:24]}",
            "source_relative_path": source_path.relative_to(self.repo_root.resolve()).as_posix(),
            "source_sha256": source_sha256,
            "tracking_contract_relative_path": contract_path.relative_to(self.repo_root.resolve()).as_posix(),
            "tracking_contract_sha256": contract_sha256,
            "base_config_relative_path": base_config_path.relative_to(self.repo_root.resolve()).as_posix(),
            "base_config_sha256": base_config_sha256,
            "effective_config_relative_path": config_snapshot.path.relative_to(self.repo_root.resolve()).as_posix(),
            "effective_config_sha256": config_snapshot.sha256,
            "trial_intent_sha256": note["intent_sha256"],
            "tuning_patch_binding": tuning_patch_binding,
            "tuning_patch_sha256": tuning_patch_sha256,
            "profile_ids": list(request["profile_ids"]),
            "frame_indices": frame_indices,
            "top_k": 5,
            "requested_decode_mode": "preroll",
        }
        if annotation_check_authority is not None:
            internal_request["annotation_sampling_manifest_sha256"] = annotation_check_authority[
                "sampling_manifest_sha256"
            ]
        retry_from_job_id = request.get("retry_from_job_id")
        if retry_from_job_id is not None:
            internal_request["retry_from_job_id"] = retry_from_job_id
        development = self._detector_development_service()
        if annotation_check_profile_sha256s is not None:
            return development.create_probe(
                internal_request,
                _expected_profile_sha256s=annotation_check_profile_sha256s,
            )
        return development.create_probe(internal_request)

    def get_detector_probe(self, job_id: str) -> dict[str, Any]:
        return self._detector_development_service().get_probe(job_id)

    def _get_verified_detector_probe(self, job_id: str) -> dict[str, Any]:
        # Annotation packages bind the exact persisted job record.  The public
        # detector-probe view intentionally removes private record fields and
        # adds transport URLs, so it cannot satisfy a full-record trust anchor.
        return self._detector_development_service().get_verified_probe_job_record(job_id)

    def _create_annotation_check_probe(self, request: dict[str, Any]) -> dict[str, Any]:
        internal = deepcopy(request)
        session_id = internal.pop("_annotation_session_id", None)
        manifest_sha256 = internal.pop("annotation_sampling_manifest_sha256", None)
        if session_id is None or manifest_sha256 is None:
            raise DetectorDevelopmentError(
                "missing_sampling_manifest",
                "Server-owned annotation check probes require a persisted session and frozen sampling manifest",
            )
        return self.create_detector_probe(
            internal,
            _annotation_check_session_id=session_id,
            _annotation_sampling_manifest_sha256=manifest_sha256,
        )

    def _create_annotation_propagation_probe(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.create_detector_probe(deepcopy(request))

    def create_ball_annotation_session(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self._ball_annotation_service().create_session(request)
        return self._with_review_proxy_repair_capability(session)

    def get_ball_annotation_session(self, session_id: str) -> dict[str, Any]:
        session = self._ball_annotation_service().get_session(session_id)
        return self._with_review_proxy_repair_capability(session)

    def _with_review_proxy_repair_capability(self, session: dict[str, Any]) -> dict[str, Any]:
        public = deepcopy(session)
        public["review_proxy_repair"] = None
        if (
            public.get("data_role") != "development"
            or public.get("status") != "blocked"
            or public.get("blocker_code") != "review_proxy_required"
        ):
            return public
        try:
            authority = self._ball_annotation_service().get_review_proxy_repair_authority(
                require_safe_id(public.get("session_id"), "annotation session_id")
            )
            parent = self._detector_development_service().get_review_proxy_upgrade_parent(
                authority["parent_probe_job_id"]
            )
        except (DetectorDevelopmentError, KeyError):
            return public
        if authority["frame_indices"] != parent["frame_indices"]:
            return public
        public["review_proxy_repair"] = {
            "eligible": True,
            "action": "generate_verified_review_proxy",
            "create_url": "/api/v1/detector-review-proxy-repairs",
            "parent_probe_job_id": parent["parent_probe_job_id"],
            "parent_probe_report_sha256": parent["parent_probe_report_sha256"],
            "parent_probe_result_manifest_sha256": parent["parent_probe_result_manifest_sha256"],
            "parent_probe_record_sha256": parent["parent_probe_record_sha256"],
            "blocked_session_record_sha256": authority["blocked_session_record_sha256"],
        }
        return public

    def create_detector_review_proxy_repair(self, request: dict[str, Any]) -> dict[str, Any]:
        """Create the one server-authoritative repair for a pristine blocker."""

        if not isinstance(request, dict) or set(request) != {"blocked_session_id"}:
            raise DetectorDevelopmentError(
                "forged_review_proxy_authority",
                "Review-proxy repair creation accepts only blocked_session_id",
                status_code=400,
            )
        blocked_session_id = require_safe_id(request.get("blocked_session_id"), "blocked annotation session_id")
        public_request = {"blocked_session_id": blocked_session_id}
        request_sha256 = canonical_sha256(public_request)
        with self._detector_review_proxy_lock:
            existing = self._find_detector_review_proxy_repair(request_sha256)
            if existing is not None:
                self._start_detector_review_proxy_continuation(existing["repair_id"])
                return self._public_detector_review_proxy_repair(existing)

            annotation_authority = self._ball_annotation_service().get_review_proxy_repair_authority(blocked_session_id)
            parent = self._detector_development_service().get_review_proxy_upgrade_parent(
                annotation_authority["parent_probe_job_id"]
            )
            self._validate_review_proxy_repair_authority(annotation_authority, parent)
            source = annotation_authority["source"]
            low_request = {
                "source_id": source["source_id"],
                "source_relative_path": source["relative_path"],
                "source_sha256": source["sha256"],
                "source_size_bytes": source["size_bytes"],
                "source_width": source["width"],
                "source_height": source["height"],
                "source_frame_count": source["frame_count"],
                "source_fps": source["fps"],
                "sampled_frame_indices": annotation_authority["frame_indices"],
            }
            low = self._detector_review_proxy_coordinator().create_repair(low_request)
            repair_id = require_safe_id(low.get("repair_id"), "repair_id")
            now = _utc_now_iso()
            authority = {
                "blocked_session_id": blocked_session_id,
                "blocked_session_request_sha256": annotation_authority["blocked_session_request_sha256"],
                "blocked_session_record_sha256": annotation_authority["blocked_session_record_sha256"],
                "parent_probe_job_id": parent["parent_probe_job_id"],
                "development_probe_job_ids": deepcopy(annotation_authority["development_probe_job_ids"]),
                "parent_probe_request_sha256": parent["parent_probe_request_sha256"],
                "parent_probe_intent_sha256": parent["parent_probe_intent_sha256"],
                "parent_probe_semantic_intent_sha256": parent["parent_probe_semantic_intent_sha256"],
                "parent_probe_report_sha256": parent["parent_probe_report_sha256"],
                "parent_probe_result_manifest_sha256": parent["parent_probe_result_manifest_sha256"],
                "parent_probe_record_sha256": parent["parent_probe_record_sha256"],
                "parent_execution_bundle_sha256": parent["parent_execution_bundle_sha256"],
                "parent_runtime_environment_sha256": parent["parent_runtime_environment_sha256"],
                "source_frame_evidence_sha256": parent["source_frame_evidence_sha256"],
                "source_id": source["source_id"],
                "source_sha256": source["sha256"],
                "source_file_identity_sha256": source["file_identity_sha256"],
                "source_size_bytes": source["size_bytes"],
                "source_width": source["width"],
                "source_height": source["height"],
                "source_frame_count": source["frame_count"],
                "source_fps": source["fps"],
                "locked_profile_id": annotation_authority["locked_profile"]["profile_id"],
                "locked_profile_sha256": annotation_authority["locked_profile"]["profile_sha256"],
                "frame_indices": deepcopy(annotation_authority["frame_indices"]),
                "sampling_manifest_sha256": annotation_authority["sampling_manifest_sha256"],
                "temporal_groups_sha256": annotation_authority["temporal_groups_sha256"],
                "candidate_evidence_sha256": parent["candidate_evidence_sha256"],
                "replacement_request_authority_sha256": annotation_authority["replacement_request_authority_sha256"],
            }
            record = {
                "schema_version": "1.0",
                "artifact_type": "detector_review_proxy_repair_transaction",
                "repair_id": repair_id,
                "attempt_root_repair_id": repair_id,
                "attempt_number": 1,
                "retry_from_repair_id": None,
                "idempotency_key": request_sha256,
                "request_sha256": request_sha256,
                "status": "queued",
                "stage": "proxy_queued",
                "preset_id": "h264-cfr-720p-v1",
                "eligibility": {
                    "eligible": True,
                    "action": "generate_verified_review_proxy",
                    "blocker_code": "review_proxy_required",
                },
                "authority": authority,
                "low_request_sha256": low["request_sha256"],
                "low_progress": deepcopy(low.get("progress")),
                "continuation_intent": None,
                "child_probe": None,
                "replacement_session": None,
                "result": None,
                "error_code": None,
                "blocker_code": None,
                "recovery_action": None,
                "created_at": now,
                "updated_at": now,
            }
            self._persist_detector_review_proxy_repair(record)
        self._start_detector_review_proxy_continuation(repair_id)
        return self._public_detector_review_proxy_repair(record)

    def get_detector_review_proxy_repair(self, repair_id: str) -> dict[str, Any]:
        repair_id = require_safe_id(repair_id, "repair_id")
        with self._detector_review_proxy_lock:
            record = self._read_detector_review_proxy_repair(repair_id)
            transaction_sha256 = record["transaction_sha256"]
        if record["status"] not in {"ready", "failed", "blocked", "cancelled"} or (
            record.get("recovery_action") == "resume"
        ):
            self._start_detector_review_proxy_continuation(repair_id)
        elif record["status"] == "ready":
            self._verify_ready_detector_review_proxy_repair(record)
            with self._detector_review_proxy_lock:
                current = self._read_detector_review_proxy_repair(repair_id)
                if current.get("status") != "ready" or current.get("transaction_sha256") != transaction_sha256:
                    raise DetectorDevelopmentError(
                        "review_proxy_transaction_changed",
                        "Ready repair transaction changed during verification",
                        status_code=409,
                    )
                record = current
        return self._public_detector_review_proxy_repair(record)

    def _verify_ready_detector_review_proxy_repair(self, record: dict[str, Any]) -> None:
        """Replay every durable lower authority before revealing a ready result."""

        authority = record["authority"]
        annotation = self._ball_annotation_service()
        detector = self._detector_development_service()
        group = self._review_proxy_group_publication_summary(record["group_publication"])
        if group != record["group_publication"]:
            raise DetectorDevelopmentError(
                "group_publication_changed",
                "Stored replacement group witness changed",
                status_code=409,
            )
        replacement = annotation.verify_ready_review_proxy_replacement(
            blocked_session_id=authority["blocked_session_id"],
            blocked_session_record_sha256=authority["blocked_session_record_sha256"],
            replacement_session_id=record["replacement_session"]["session_id"],
            child_probe_job_id=record["child_probe"]["job_id"],
            session_creation_authority_sha256=group["session_creation_authority_sha256"],
            group_publication_sha256=group["group_publication_sha256"],
        )
        current_annotation = replacement["blocked_authority"]
        parent = detector.get_review_proxy_upgrade_parent(authority["parent_probe_job_id"])
        self._validate_review_proxy_repair_authority(current_annotation, parent)
        if self._build_review_proxy_repair_authority(current_annotation, parent) != authority:
            raise DetectorDevelopmentError(
                "review_proxy_authority_changed",
                "Ready repair authority changed after publication",
            )

        low = self._detector_review_proxy_coordinator().get_verified_proxy(record["repair_id"])
        repair_evidence, proxy_media, samples = self._load_verified_review_proxy_evidence(record, low)
        intent = record["continuation_intent"]
        if (
            intent["repair_evidence"] != repair_evidence
            or intent["proxy_media"] != proxy_media
            or sorted(samples) != authority["frame_indices"]
        ):
            raise DetectorDevelopmentError(
                "review_proxy_evidence_changed",
                "Ready repair evidence changed after publication",
            )

        child = detector.get_verified_probe_job_record(record["child_probe"]["job_id"])
        child_summary = self._review_proxy_child_summary(child)
        if child_summary != record["child_probe"]:
            raise DetectorDevelopmentError(
                "review_proxy_child_changed",
                "Ready repair child changed after publication",
            )
        session = replacement["session"]
        session_summary = record["replacement_session"]
        retry_lineage = session.get("retry_lineage")
        lineage = session.get("lineage")
        if (
            session.get("session_id") != session_summary["session_id"]
            or session.get("request_sha256") != session_summary["request_sha256"]
            or session.get("retry_from_session_id") != session_summary["retry_from_session_id"]
            or not isinstance(retry_lineage, dict)
            or retry_lineage.get("mode") != session_summary["retry_mode"]
            or session.get("attempt_family_sha256") != session_summary["attempt_family_sha256"]
            or not isinstance(lineage, dict)
            or lineage.get("development_probe_job_ids") != session_summary["development_probe_job_ids"]
        ):
            raise DetectorDevelopmentError(
                "replacement_session_changed",
                "Ready replacement creation authority changed after publication",
            )
        if (
            replacement["session_creation_authority_sha256"] != group["session_creation_authority_sha256"]
            or replacement["group_publication_sha256"] != group["group_publication_sha256"]
        ):
            raise DetectorDevelopmentError(
                "group_publication_changed",
                "Ready replacement group publication changed",
            )
        expected_proxy_result = self._build_review_proxy_result_proxy(
            record=record,
            low=low,
            child=child,
            proxy_media=proxy_media,
            samples=samples,
        )
        expected_result = {
            "proxy": expected_proxy_result,
            "child_probe": child_summary,
            "replacement_session": session_summary,
            "parent_probe_record_sha256_after": parent["parent_probe_record_sha256"],
        }
        if record.get("result") != expected_result:
            raise DetectorDevelopmentError(
                "review_proxy_result_changed",
                "Ready repair result changed from replayed lower authority",
            )

    def _can_retry_detector_review_proxy_repair(self, record: dict[str, Any]) -> bool:
        if record.get("status") not in {"failed", "blocked", "cancelled"}:
            return False
        if any(
            record.get(field) is not None
            for field in (
                "child_probe",
                "replacement_session",
                "group_publication",
                "result",
            )
        ):
            return False
        rank = self._review_proxy_actual_side_effect_floor(record)
        if record.get("status") == "cancelled":
            return rank == 0
        return rank in {0, 1, 2} and record.get("error_code") in (_REVIEW_PROXY_PRE_SIDE_EFFECT_RETRYABLE_CODES)

    @classmethod
    def _review_proxy_expected_recovery_action(cls, *, status: str, rank: int, error_code: str | None) -> str | None:
        if status not in {"failed", "blocked"}:
            return None
        if rank >= 3:
            return "resume"
        if rank in {1, 2} and error_code in _REVIEW_PROXY_SAME_ATTEMPT_RESUMABLE_CODES:
            return "resume"
        if rank in {0, 1, 2} and error_code in _REVIEW_PROXY_PRE_SIDE_EFFECT_RETRYABLE_CODES:
            return "retry"
        return None

    @staticmethod
    def _review_proxy_group_publication_summary(commit: dict[str, Any]) -> dict[str, Any]:
        try:
            body = {
                "session_id": require_safe_id(commit["session_id"], "replacement session_id"),
                "blocked_session_id": require_safe_id(
                    commit["blocked_session_id"],
                    "blocked annotation session_id",
                ),
                "child_probe_job_id": require_safe_id(
                    commit["child_probe_job_id"],
                    "review-proxy child probe job_id",
                ),
                "session_record_sha256": require_sha256(
                    commit["session_record_sha256"],
                    "replacement session record sha256",
                ),
                "session_creation_authority_sha256": require_sha256(
                    commit["session_creation_authority_sha256"],
                    "replacement session creation authority sha256",
                ),
                "group_publication_sha256": require_sha256(
                    commit["group_publication_sha256"],
                    "replacement group publication sha256",
                ),
            }
            commit_sha256 = require_sha256(
                commit["commit_sha256"],
                "replacement group commit sha256",
            )
        except (KeyError, TypeError, DetectorDevelopmentError) as exc:
            raise DetectorDevelopmentError(
                "group_publication_changed",
                "Replacement temporal-group commit is incomplete",
                status_code=409,
            ) from exc
        if canonical_sha256(body) != commit_sha256:
            raise DetectorDevelopmentError(
                "group_publication_changed",
                "Replacement temporal-group commit digest changed",
                status_code=409,
            )
        return {**body, "commit_sha256": commit_sha256}

    def _inspect_review_proxy_low_job(
        self,
        record: dict[str, Any],
        authority: dict[str, Any],
        journal_rank: int,
    ) -> int:
        """Return the verified proxy-publication floor for one low job."""

        try:
            low = self._detector_review_proxy_coordinator().get_repair(record["repair_id"])
            frozen = low["frozen_request"]
            low_status = low["status"]
        except (DetectorDevelopmentError, KeyError, TypeError) as exc:
            raise DetectorDevelopmentError(
                "review_proxy_side_effect_floor_unverifiable",
                "Durable review-proxy job authority cannot be verified",
                status_code=409,
            ) from exc
        expected_frozen = {
            "source_id": authority.get("source_id"),
            "source_sha256": authority.get("source_sha256"),
            "source_size_bytes": authority.get("source_size_bytes"),
            "source_width": authority.get("source_width"),
            "source_height": authority.get("source_height"),
            "source_frame_count": authority.get("source_frame_count"),
            "source_fps": authority.get("source_fps"),
            "sampled_frame_indices": authority.get("frame_indices"),
        }
        lineage_matches = bool(
            isinstance(frozen, dict)
            and low.get("repair_id") == record.get("repair_id")
            and low.get("request_sha256") == record.get("low_request_sha256")
            and canonical_sha256(frozen) == low.get("request_sha256")
            and low.get("attempt_root_repair_id") == record.get("attempt_root_repair_id")
            and low.get("attempt_number") == record.get("attempt_number")
            and low.get("retry_from_repair_id") == record.get("retry_from_repair_id")
            and all(frozen.get(key) == value for key, value in expected_frozen.items())
        )
        if not lineage_matches or low_status not in {
            "queued",
            "running",
            "committing",
            "ready",
            "failed",
            "blocked",
            "cancelled",
        }:
            raise DetectorDevelopmentError(
                "review_proxy_side_effect_floor_unverifiable",
                "Durable review-proxy job differs from its continuation journal",
                status_code=409,
            )
        low_rank = 1 if low_status == "ready" else 0
        if journal_rank >= 1 and low_rank == 0:
            raise DetectorDevelopmentError(
                "review_proxy_side_effect_floor_unverifiable",
                "Repair journal names a proxy absent from the lower store",
                status_code=409,
            )
        upper_status = record.get("status")
        if journal_rank == 0 and upper_status in {"failed", "blocked", "cancelled"} and low_rank == 0:
            terminal_matches = bool(
                low_status == upper_status
                and (
                    upper_status == "cancelled"
                    and low.get("error_code") == "cancelled"
                    or upper_status in {"failed", "blocked"}
                    and low.get("error_code") == record.get("error_code")
                )
            )
            if not terminal_matches:
                raise DetectorDevelopmentError(
                    "review_proxy_side_effect_floor_unverifiable",
                    "Terminal review-proxy job differs from its continuation journal",
                    status_code=409,
                )
        return low_rank

    def _inspect_review_proxy_lower_side_effects(self, record: dict[str, Any]) -> dict[str, Any]:
        """Read the exact lower-store prefix without mutating either store."""

        journal_rank = self._review_proxy_stage_completed(record)
        authority = record.get("authority")
        intent = record.get("continuation_intent")
        child_plan = intent.get("child_plan") if isinstance(intent, dict) else None
        if not isinstance(authority, dict):
            raise DetectorDevelopmentError(
                "review_proxy_side_effect_floor_unverifiable",
                "Repair lower authority is incomplete",
                status_code=409,
            )
        low_rank = self._inspect_review_proxy_low_job(record, authority, journal_rank)
        parent_job_id = require_safe_id(
            authority.get("parent_probe_job_id"),
            "review-proxy parent probe job_id",
        )
        detector = self._detector_development_service()
        try:
            child = detector.get_review_proxy_upgrade_child(parent_job_id)
        except DetectorDevelopmentError as exc:
            if (
                exc.code == "review_proxy_parent_child_claimed"
                and record.get("child_probe") is None
                and journal_rank < 3
            ):
                return {
                    "rank": 3,
                    "claim_only": True,
                    "child_probe": None,
                    "replacement_session": None,
                    "group_publication": None,
                }
            raise DetectorDevelopmentError(
                "review_proxy_side_effect_floor_unverifiable",
                "Durable review-proxy child authority cannot be verified",
                status_code=409,
            ) from exc
        if child is None:
            if journal_rank >= 3 or record.get("child_probe") is not None:
                raise DetectorDevelopmentError(
                    "review_proxy_side_effect_floor_unverifiable",
                    "Repair journal names a child absent from the lower store",
                    status_code=409,
                )
            return {
                "rank": max(journal_rank, low_rank),
                "claim_only": False,
                "child_probe": None,
                "replacement_session": None,
                "group_publication": None,
            }

        child = detector.get_verified_probe_job_record(
            require_safe_id(child.get("job_id"), "review-proxy child probe job_id")
        )

        child_frozen = child.get("frozen_request")
        child_upgrade = child_frozen.get("review_proxy_upgrade") if isinstance(child_frozen, dict) else None
        child_repair_evidence = child_upgrade.get("repair_evidence") if isinstance(child_upgrade, dict) else None
        try:
            child_repair_id = require_safe_id(
                child_repair_evidence.get("repair_id") if isinstance(child_repair_evidence, dict) else None,
                "review-proxy child repair_id",
            )
        except DetectorDevelopmentError as exc:
            raise DetectorDevelopmentError(
                "review_proxy_side_effect_floor_unverifiable",
                "Durable review-proxy child has no verified repair binding",
                status_code=409,
            ) from exc
        if child_repair_id != record.get("repair_id"):
            if journal_rank >= 3 or record.get("child_probe") is not None:
                raise DetectorDevelopmentError(
                    "review_proxy_side_effect_floor_unverifiable",
                    "Repair journal names a child from a different repair attempt",
                    status_code=409,
                )
            return {
                "rank": max(journal_rank, low_rank),
                "claim_only": False,
                "child_probe": None,
                "replacement_session": None,
                "group_publication": None,
            }

        if not isinstance(child_plan, dict):
            raise DetectorDevelopmentError(
                "review_proxy_side_effect_floor_unverifiable",
                "Durable review-proxy child has no frozen continuation plan",
                status_code=409,
            )
        child_prefix_valid = bool(
            child.get("request_sha256") == child_plan.get("request_sha256")
            and child.get("intent_sha256") == child_plan.get("intent_sha256")
            and child.get("semantic_intent_sha256") == child_plan.get("semantic_intent_sha256")
            and child.get("resource_sha256") == child_plan.get("resource_sha256")
            and child.get("frozen_profiles_sha256") == child_plan.get("frozen_profiles_sha256")
            and child.get("retry_from_job_id") == parent_job_id
            and child.get("retry_kind") == "review_proxy_decode_upgrade"
        )
        if not child_prefix_valid:
            raise DetectorDevelopmentError(
                "review_proxy_side_effect_floor_unverifiable",
                "Durable review-proxy child differs from the frozen continuation",
                status_code=409,
            )
        if child.get("status") != "ready":
            if record.get("child_probe") is not None or journal_rank >= 3:
                raise DetectorDevelopmentError(
                    "review_proxy_side_effect_floor_unverifiable",
                    "Repair journal names an unverified lower child",
                    status_code=409,
                )
            return {
                "rank": 3,
                "claim_only": True,
                "child_probe": None,
                "replacement_session": None,
                "group_publication": None,
            }

        try:
            child_summary = self._review_proxy_child_summary(child)
        except (DetectorDevelopmentError, KeyError, TypeError) as exc:
            raise DetectorDevelopmentError(
                "review_proxy_side_effect_floor_unverifiable",
                "Durable review-proxy child summary cannot be verified",
                status_code=409,
            ) from exc
        expected_child_bindings = {
            "request_sha256": child_plan.get("request_sha256"),
            "intent_sha256": child_plan.get("intent_sha256"),
            "semantic_intent_sha256": child_plan.get("semantic_intent_sha256"),
            "resource_sha256": child_plan.get("resource_sha256"),
            "frozen_profiles_sha256": child_plan.get("frozen_profiles_sha256"),
            "execution_bundle_sha256": child_plan.get("execution_bundle_sha256"),
            "runtime_environment_sha256": child_plan.get("runtime_environment_sha256"),
            "retry_from_job_id": parent_job_id,
        }
        if any(child_summary.get(key) != value for key, value in expected_child_bindings.items()) or (
            record.get("child_probe") is not None and record.get("child_probe") != child_summary
        ):
            raise DetectorDevelopmentError(
                "review_proxy_side_effect_floor_unverifiable",
                "Durable review-proxy child summary changed",
                status_code=409,
            )

        try:
            annotation = self._ball_annotation_service()
            group_witness = record.get("group_publication")
            if group_witness is not None:
                group_witness = self._review_proxy_group_publication_summary(group_witness)
                if group_witness != record.get("group_publication"):
                    raise DetectorDevelopmentError(
                        "group_publication_changed",
                        "Stored replacement group witness changed",
                        status_code=409,
                    )
            replacement = annotation.inspect_review_proxy_replacement_side_effect(
                authority["blocked_session_id"],
                child_probe_job_id=child_summary["job_id"],
                expected_development_probe_job_ids=authority["development_probe_job_ids"],
                blocked_session_record_sha256=authority["blocked_session_record_sha256"],
                expected_group_commit=group_witness,
                replacement_session_witnessed=record.get("replacement_session") is not None,
            )
        except (DetectorDevelopmentError, KeyError, TypeError) as exc:
            if journal_rank < 3 and record.get("child_probe") is None:
                # The verified child alone proves the non-retryable floor. A
                # missing or malformed annotation store must not hide that
                # fact behind a second error or permit a descendant attempt.
                return {
                    "rank": 3,
                    "claim_only": False,
                    "child_probe": child_summary,
                    "replacement_session": None,
                    "group_publication": None,
                }
            raise DetectorDevelopmentError(
                "review_proxy_side_effect_floor_unverifiable",
                "Durable replacement annotation authority cannot be verified",
                status_code=409,
            ) from exc
        if replacement is None:
            if journal_rank >= 4 or record.get("replacement_session") is not None:
                raise DetectorDevelopmentError(
                    "review_proxy_side_effect_floor_unverifiable",
                    "Repair journal names a replacement absent from the lower store",
                    status_code=409,
                )
            return {
                "rank": 3,
                "claim_only": False,
                "child_probe": child_summary,
                "replacement_session": None,
                "group_publication": None,
            }

        try:
            session_summary = self._review_proxy_session_summary(replacement["session"])
        except (DetectorDevelopmentError, KeyError, TypeError) as exc:
            raise DetectorDevelopmentError(
                "review_proxy_side_effect_floor_unverifiable",
                "Durable replacement session summary cannot be verified",
                status_code=409,
            ) from exc
        if record.get("replacement_session") is not None and record.get("replacement_session") != session_summary:
            raise DetectorDevelopmentError(
                "review_proxy_side_effect_floor_unverifiable",
                "Durable replacement session summary changed",
                status_code=409,
            )

        group_commit = replacement.get("group_commit")
        if group_commit is None:
            if journal_rank >= 5 or record.get("group_publication") is not None:
                raise DetectorDevelopmentError(
                    "review_proxy_side_effect_floor_unverifiable",
                    "Repair journal names groups absent from the lower store",
                    status_code=409,
                )
            return {
                "rank": 4,
                "claim_only": False,
                "child_probe": child_summary,
                "replacement_session": session_summary,
                "group_publication": None,
            }
        try:
            group_summary = self._review_proxy_group_publication_summary(group_commit)
        except (KeyError, TypeError) as exc:
            raise DetectorDevelopmentError(
                "review_proxy_side_effect_floor_unverifiable",
                "Durable replacement group commit is incomplete",
                status_code=409,
            ) from exc
        if record.get("group_publication") is not None and record.get("group_publication") != group_summary:
            raise DetectorDevelopmentError(
                "review_proxy_side_effect_floor_unverifiable",
                "Durable replacement group commit changed",
                status_code=409,
            )
        return {
            "rank": 5,
            "claim_only": False,
            "child_probe": child_summary,
            "replacement_session": session_summary,
            "group_publication": group_summary,
        }

    def _reconcile_review_proxy_lower_side_effects(self, record: dict[str, Any]) -> tuple[int, bool]:
        inspection = self._inspect_review_proxy_lower_side_effects(record)
        changed = False
        for key in (
            "child_probe",
            "replacement_session",
            "group_publication",
        ):
            lower_value = inspection[key]
            if lower_value is not None and record.get(key) is None:
                record[key] = deepcopy(lower_value)
                changed = True
        rank = int(inspection["rank"])
        if not inspection["claim_only"]:
            stage = {
                1: "proxy_ready",
                3: "child_probe_ready",
                4: "replacement_session_ready",
                5: "groups_published",
            }.get(rank)
            if stage is not None and self._review_proxy_stage_completed(record) < rank:
                record["stage"] = stage
                changed = True
        return rank, changed

    def _review_proxy_actual_side_effect_floor(self, record: dict[str, Any]) -> int:
        """Return the highest phase proven by the append-only lower stores."""

        return int(self._inspect_review_proxy_lower_side_effects(record)["rank"])

    def retry_detector_review_proxy_repair(self, repair_id: str) -> dict[str, Any]:
        repair_id = require_safe_id(repair_id, "repair_id")
        with self._detector_review_proxy_lock:
            source_record = self._read_detector_review_proxy_repair(repair_id)
            existing = [
                self._read_detector_review_proxy_repair(path.stem)
                for path in sorted(self._detector_review_proxy_jobs_root.glob("*.json"))
                if path.stem != repair_id
            ]
            descendants = [record for record in existing if record.get("retry_from_repair_id") == repair_id]
            if len(descendants) > 1:
                raise DetectorDevelopmentError(
                    "duplicate_review_proxy_retry",
                    "Multiple continuation retries share one parent attempt",
                )
            if descendants:
                return self._public_detector_review_proxy_repair(descendants[0])
            if not self._can_retry_detector_review_proxy_repair(source_record):
                raise DetectorDevelopmentError(
                    "review_proxy_retry_ineligible",
                    "This repair phase cannot create a new attempt",
                    status_code=409,
                )
            authority = deepcopy(source_record["authority"])
            current_annotation = self._ball_annotation_service().get_review_proxy_repair_authority(
                authority["blocked_session_id"]
            )
            current_parent = self._detector_development_service().get_review_proxy_upgrade_parent(
                current_annotation["parent_probe_job_id"]
            )
            self._validate_review_proxy_repair_authority(current_annotation, current_parent)
            if self._build_review_proxy_repair_authority(current_annotation, current_parent) != authority:
                raise DetectorDevelopmentError(
                    "review_proxy_authority_changed",
                    "Retry authority changed after the original attempt",
                    status_code=409,
                )
            rank = self._review_proxy_actual_side_effect_floor(source_record)
            if rank >= 3:
                raise DetectorDevelopmentError(
                    "review_proxy_retry_ineligible",
                    "A durable review-proxy child already exists for this audited parent",
                    status_code=409,
                )
            low = self._detector_review_proxy_coordinator().retry_repair(
                repair_id,
                allow_ready_pre_reveal=rank in {1, 2},
            )
            new_repair_id = require_safe_id(low.get("repair_id"), "retry repair_id")
            root_id = require_safe_id(
                source_record.get("attempt_root_repair_id", repair_id),
                "retry root repair_id",
            )
            attempt_number = int(source_record.get("attempt_number", 1)) + 1
            request_sha256 = canonical_sha256(
                {
                    "artifact_type": "detector_review_proxy_retry_request",
                    "retry_from_repair_id": repair_id,
                    "attempt_root_repair_id": root_id,
                    "attempt_number": attempt_number,
                    "blocked_session_id": authority["blocked_session_id"],
                }
            )
            now = _utc_now_iso()
            record = {
                "schema_version": "1.0",
                "artifact_type": "detector_review_proxy_repair_transaction",
                "repair_id": new_repair_id,
                "attempt_root_repair_id": root_id,
                "attempt_number": attempt_number,
                "retry_from_repair_id": repair_id,
                "idempotency_key": request_sha256,
                "request_sha256": request_sha256,
                "status": "queued",
                "stage": "proxy_queued",
                "preset_id": "h264-cfr-720p-v1",
                "eligibility": deepcopy(source_record["eligibility"]),
                "authority": authority,
                "low_request_sha256": low["request_sha256"],
                "low_progress": deepcopy(low.get("progress")),
                "continuation_intent": None,
                "child_probe": None,
                "replacement_session": None,
                "group_publication": None,
                "result": None,
                "error_code": None,
                "blocker_code": None,
                "recovery_action": None,
                "created_at": now,
                "updated_at": now,
            }
            self._persist_detector_review_proxy_repair(record)
        self._start_detector_review_proxy_continuation(new_repair_id)
        return self._public_detector_review_proxy_repair(record)

    def cancel_detector_review_proxy_repair(self, repair_id: str) -> dict[str, Any]:
        repair_id = require_safe_id(repair_id, "repair_id")
        with self._detector_review_proxy_lock:
            record = self._read_detector_review_proxy_repair(repair_id)
            if record["status"] in {"ready", "failed", "blocked", "cancelled"}:
                return self._public_detector_review_proxy_repair(record)
            if record["status"] == "committing" or record.get("continuation_intent") is not None:
                raise DetectorDevelopmentError(
                    "commit_in_progress",
                    "Review-proxy continuation can no longer be cancelled",
                    status_code=409,
                )
            low = self._detector_review_proxy_coordinator().cancel_repair(repair_id)
            low_status = low.get("status")
            if low_status == "cancelled":
                record.update(
                    status="cancelled",
                    stage="cancelled",
                    error_code=None,
                    blocker_code=None,
                    recovery_action=None,
                    low_progress=deepcopy(low.get("progress")),
                    updated_at=_utc_now_iso(),
                )
            elif low_status in {"committing", "ready"}:
                record.update(
                    status="committing",
                    stage=("proxy_ready" if low_status == "ready" else "proxy_committing"),
                    low_progress=deepcopy(low.get("progress")),
                    updated_at=_utc_now_iso(),
                )
                self._persist_detector_review_proxy_repair(record)
                self._start_detector_review_proxy_continuation(repair_id)
                raise DetectorDevelopmentError(
                    "commit_in_progress",
                    "The review proxy already reached its commit point",
                    status_code=409,
                )
            elif low_status in {"failed", "blocked"}:
                error_code = str(low.get("error_code") or "review_proxy_failed")
                record.update(
                    status=low_status,
                    stage=str(low.get("stage") or low_status),
                    error_code=error_code,
                    blocker_code=(error_code if low_status == "blocked" else None),
                    recovery_action=self._review_proxy_expected_recovery_action(
                        status=low_status,
                        rank=0,
                        error_code=error_code,
                    ),
                    low_progress=deepcopy(low.get("progress")),
                    updated_at=_utc_now_iso(),
                )
            else:
                # A running worker has only received a cancellation request;
                # it has not durably cancelled yet.  Preserve the truthful
                # state and let the watcher observe its terminal transition.
                record.update(
                    status=("queued" if low_status == "queued" else "running"),
                    stage=str(low.get("stage") or "cancelling"),
                    low_progress=deepcopy(low.get("progress")),
                    updated_at=_utc_now_iso(),
                )
            self._persist_detector_review_proxy_repair(record)
        if record["status"] not in {"cancelled", "failed", "blocked"}:
            self._start_detector_review_proxy_continuation(repair_id)
        return self._public_detector_review_proxy_repair(record)

    def _detector_review_proxy_coordinator(
        self,
    ) -> DetectorReviewProxyCoordinator:
        with self._detector_review_proxy_lock:
            if self._closing:
                raise DetectorDevelopmentError(
                    "service_closed",
                    "Detector review-proxy service is closed",
                    status_code=503,
                )
            if self._detector_review_proxy is None:
                self._detector_review_proxy = DetectorReviewProxyCoordinator(self.repo_root)
            return self._detector_review_proxy

    def _persist_detector_review_proxy_repair(self, record: dict[str, Any]) -> None:
        repair_id = require_safe_id(record.get("repair_id"), "repair_id")
        if (
            record.get("schema_version") != "1.0"
            or record.get("artifact_type") != "detector_review_proxy_repair_transaction"
            or record.get("status")
            not in {
                "queued",
                "running",
                "committing",
                "ready",
                "failed",
                "blocked",
                "cancelled",
            }
            or record.get("idempotency_key") != record.get("request_sha256")
            or not self._valid_detector_review_proxy_transaction_phase(record)
        ):
            raise DetectorDevelopmentError(
                "invalid_review_proxy_transaction",
                "Review-proxy continuation transaction is invalid",
            )
        sealed = deepcopy(record)
        sealed.pop("transaction_sha256", None)
        sealed["transaction_sha256"] = canonical_sha256(sealed)
        record["transaction_sha256"] = sealed["transaction_sha256"]
        atomic_write_json(
            self._detector_review_proxy_jobs_root / f"{repair_id}.json",
            sealed,
            trusted_root=self._detector_review_proxy_jobs_root,
        )

    def _read_detector_review_proxy_repair(self, repair_id: str) -> dict[str, Any]:
        repair_id = require_safe_id(repair_id, "repair_id")
        path = self._detector_review_proxy_jobs_root / f"{repair_id}.json"
        if not path.is_file():
            raise DetectorDevelopmentError(
                "review_proxy_repair_not_found",
                "Detector review-proxy repair was not found",
                status_code=404,
            )
        content, _digest = read_regular_bytes(
            path,
            "review-proxy continuation transaction",
            max_bytes=4 * 1024 * 1024,
            trusted_root=self._detector_review_proxy_jobs_root,
        )
        record = json_object_from_bytes(content, "review-proxy continuation transaction")
        sealed_digest = record.get("transaction_sha256")
        canonical_record = deepcopy(record)
        canonical_record.pop("transaction_sha256", None)
        if (
            record.get("repair_id") != repair_id
            or record.get("artifact_type") != "detector_review_proxy_repair_transaction"
            or not isinstance(sealed_digest, str)
            or canonical_sha256(canonical_record) != sealed_digest
            or record.get("idempotency_key") != record.get("request_sha256")
            or record.get("request_sha256") != self._expected_detector_review_proxy_request_sha256(record)
            or record.get("status")
            not in {
                "queued",
                "running",
                "committing",
                "ready",
                "failed",
                "blocked",
                "cancelled",
            }
            or not self._valid_detector_review_proxy_transaction_phase(record)
        ):
            raise DetectorDevelopmentError(
                "invalid_review_proxy_transaction",
                "Persisted review-proxy continuation is invalid",
            )
        return record

    @staticmethod
    def _expected_detector_review_proxy_request_sha256(record: dict[str, Any]) -> str:
        authority = record.get("authority", {})
        if record.get("attempt_number", 1) == 1:
            request = {"blocked_session_id": authority.get("blocked_session_id")}
        else:
            request = {
                "artifact_type": "detector_review_proxy_retry_request",
                "retry_from_repair_id": record.get("retry_from_repair_id"),
                "attempt_root_repair_id": record.get("attempt_root_repair_id"),
                "attempt_number": record.get("attempt_number"),
                "blocked_session_id": authority.get("blocked_session_id"),
            }
        return canonical_sha256(request)

    def _find_detector_review_proxy_repair(self, request_sha256: str) -> dict[str, Any] | None:
        request_sha256 = require_sha256(request_sha256, "review-proxy request sha256")
        found: list[dict[str, Any]] = []
        for path in sorted(self._detector_review_proxy_jobs_root.glob("*.json")):
            record = self._read_detector_review_proxy_repair(path.stem)
            if record.get("request_sha256") == request_sha256:
                found.append(record)
        if len(found) > 1:
            raise DetectorDevelopmentError(
                "duplicate_review_proxy_transaction",
                "Multiple review-proxy continuations share one request",
            )
        return found[0] if found else None

    @staticmethod
    def _review_proxy_stage_completed(record: dict[str, Any]) -> int:
        ranks = {
            "proxy_queued": 0,
            "queued": 0,
            "running": 0,
            "verifying_source": 0,
            "transcoding": 0,
            "independent_verification": 0,
            "recovered_after_restart": 0,
            "proxy_committing": 0,
            "failed": 0,
            "blocked": 0,
            "cancelled": 0,
            "proxy_ready": 1,
            "continuation_intent": 2,
            "child_probe_ready": 3,
            "replacement_session_ready": 4,
            "groups_published": 5,
            "ready": 6,
        }
        return ranks.get(str(record.get("stage")), -1)

    @classmethod
    def _valid_detector_review_proxy_transaction_phase(cls, record: dict[str, Any]) -> bool:
        repair_id = record.get("repair_id")
        attempt_root_repair_id = record.get("attempt_root_repair_id")
        attempt_number = record.get("attempt_number")
        retry_from_repair_id = record.get("retry_from_repair_id")
        lineage_valid = (
            isinstance(attempt_number, int)
            and not isinstance(attempt_number, bool)
            and attempt_number >= 1
            and isinstance(attempt_root_repair_id, str)
            and (
                attempt_number == 1
                and attempt_root_repair_id == repair_id
                and retry_from_repair_id is None
                or attempt_number > 1
                and isinstance(retry_from_repair_id, str)
                and retry_from_repair_id != repair_id
                and attempt_root_repair_id != repair_id
            )
        )
        authority = record.get("authority")
        if not isinstance(authority, dict):
            authority = {}
        try:
            from football_tracking.api.schemas import (
                DetectorReviewProxyRepairAuthorityView,
            )

            authority_valid = (
                isinstance(authority, dict)
                and DetectorReviewProxyRepairAuthorityView.model_validate(authority).model_dump(mode="json")
                == authority
            )
        except Exception:
            authority_valid = False
        intent = record.get("continuation_intent")
        intent_valid = intent is None
        child_plan: dict[str, Any] | None = None
        if isinstance(intent, dict):
            intent_body = deepcopy(intent)
            intent_digest = intent_body.pop("intent_sha256", None)
            child_plan = intent_body.get("child_plan")
            child_plan_valid = False
            if isinstance(child_plan, dict):
                plan_body = deepcopy(child_plan)
                plan_digest = plan_body.pop("plan_sha256", None)
                continuation = plan_body.get("continuation_execution_binding")
                continuation_valid = False
                if isinstance(continuation, dict):
                    continuation_body = deepcopy(continuation)
                    continuation_digest = continuation_body.pop("binding_sha256", None)
                    continuation_valid = canonical_sha256(continuation_body) == continuation_digest
                child_plan_valid = (
                    plan_body.get("artifact_type") == "detector_review_proxy_child_plan"
                    and plan_body.get("repair_id") == repair_id
                    and authority_valid
                    and plan_body.get("parent_probe_job_id") == authority.get("parent_probe_job_id")
                    and canonical_sha256(plan_body) == plan_digest
                    and continuation_valid
                )
            sampled_frame_sha256s = intent_body.get("repair_evidence", {}).get("sampled_frame_sha256s")
            try:
                sampled_frames_valid = (
                    isinstance(sampled_frame_sha256s, dict)
                    and sorted(int(key) for key in sampled_frame_sha256s) == authority.get("frame_indices")
                    and all(
                        isinstance(value, str) and len(value) == 64 and set(value) <= set("0123456789abcdef")
                        for value in sampled_frame_sha256s.values()
                    )
                )
            except (TypeError, ValueError):
                sampled_frames_valid = False
            intent_valid = (
                intent_body.get("artifact_type") == "detector_review_proxy_continuation_intent"
                and intent_body.get("repair_id") == repair_id
                and isinstance(intent_body.get("repair_evidence"), dict)
                and intent_body["repair_evidence"].get("repair_id") == repair_id
                and intent_body["repair_evidence"].get("repair_request_sha256") == record.get("low_request_sha256")
                and isinstance(intent_body.get("proxy_media"), dict)
                and intent_body["repair_evidence"].get("proxy_media_sha256") == intent_body["proxy_media"].get("sha256")
                and intent_body["repair_evidence"].get("proxy_size_bytes")
                == intent_body["proxy_media"].get("size_bytes")
                and intent_body["proxy_media"].get("frame_count") == authority.get("source_frame_count")
                and intent_body["proxy_media"].get("fps") == authority.get("source_fps")
                and sampled_frames_valid
                and authority_valid
                and intent_body.get("authority_sha256") == canonical_sha256(authority)
                and child_plan_valid
                and canonical_sha256(intent_body) == intent_digest
            )
        rank = cls._review_proxy_stage_completed(record)
        present = (
            intent is not None,
            record.get("child_probe") is not None,
            record.get("replacement_session") is not None,
            record.get("group_publication") is not None,
            record.get("result") is not None,
        )
        expected = {
            0: (False, False, False, False, False),
            1: (False, False, False, False, False),
            2: (True, False, False, False, False),
            3: (True, True, False, False, False),
            4: (True, True, True, False, False),
            5: (True, True, True, True, False),
            6: (True, True, True, True, True),
        }.get(rank)
        status = record.get("status")
        stage = record.get("stage")
        status_phase_valid = (
            (status == "queued" and stage in {"proxy_queued", "queued", "recovered_after_restart"})
            or (
                status == "running"
                and stage
                in {
                    "queued",
                    "running",
                    "verifying_source",
                    "transcoding",
                    "independent_verification",
                    "recovered_after_restart",
                }
            )
            or (status == "cancelled" and stage == "cancelled")
            or (status == "committing" and (stage == "proxy_committing" or rank in {1, 2, 3, 4, 5}))
            or (status == "ready" and rank == 6)
            or (status == "failed" and (stage == "failed" or rank in {1, 2, 3, 4, 5}))
            or (status == "blocked" and (stage == "blocked" or rank in {1, 2, 3, 4, 5}))
        )
        child = record.get("child_probe")
        replacement = record.get("replacement_session")
        group = record.get("group_publication")
        result = record.get("result")
        summaries_valid = True
        try:
            from football_tracking.api.schemas import (
                DetectorReviewProxyRepairChildProbeView,
                DetectorReviewProxyRepairResultView,
                DetectorReviewProxyRepairSessionView,
            )

            if child is not None:
                summaries_valid = (
                    DetectorReviewProxyRepairChildProbeView.model_validate(child).model_dump(mode="json") == child
                )
            if summaries_valid and replacement is not None:
                summaries_valid = (
                    DetectorReviewProxyRepairSessionView.model_validate(replacement).model_dump(mode="json")
                    == replacement
                )
            if summaries_valid and group is not None:
                summaries_valid = (
                    isinstance(group, dict)
                    and set(group)
                    == {
                        "session_id",
                        "blocked_session_id",
                        "child_probe_job_id",
                        "session_record_sha256",
                        "session_creation_authority_sha256",
                        "group_publication_sha256",
                        "commit_sha256",
                    }
                    and all(
                        isinstance(value, str) and len(value) == 64 and set(value) <= set("0123456789abcdef")
                        for value in (
                            group["session_record_sha256"],
                            group["session_creation_authority_sha256"],
                            group["group_publication_sha256"],
                            group["commit_sha256"],
                        )
                    )
                    and canonical_sha256({key: value for key, value in group.items() if key != "commit_sha256"})
                    == group["commit_sha256"]
                )
            if summaries_valid and result is not None:
                summaries_valid = (
                    DetectorReviewProxyRepairResultView.model_validate(result).model_dump(mode="json") == result
                    and result.get("child_probe") == child
                    and result.get("replacement_session") == replacement
                    and result.get("parent_probe_record_sha256_after")
                    == record.get("authority", {}).get("parent_probe_record_sha256")
                )
        except Exception:
            summaries_valid = False
        cross_bindings_valid = authority_valid
        if cross_bindings_valid and child is not None:
            continuation = child_plan.get("continuation_execution_binding") if isinstance(child_plan, dict) else None
            cross_bindings_valid = bool(
                isinstance(child_plan, dict)
                and isinstance(continuation, dict)
                and child.get("retry_from_job_id") == authority.get("parent_probe_job_id")
                and child.get("request_sha256") == child_plan.get("request_sha256")
                and child.get("intent_sha256") == child_plan.get("intent_sha256")
                and child.get("semantic_intent_sha256") == child_plan.get("semantic_intent_sha256")
                and child.get("resource_sha256") == child_plan.get("resource_sha256")
                and child.get("frozen_profiles_sha256") == child_plan.get("frozen_profiles_sha256")
                and child.get("execution_bundle_sha256") == child_plan.get("execution_bundle_sha256")
                and child.get("runtime_environment_sha256") == child_plan.get("runtime_environment_sha256")
                and child.get("continuation_execution_binding_sha256") == continuation.get("binding_sha256")
                and child.get("continuation_code_bundle_sha256") == continuation.get("code_bundle_sha256")
                and child.get("continuation_runtime_sha256") == continuation.get("runtime_sha256")
            )
        if cross_bindings_valid and replacement is not None:
            cross_bindings_valid = bool(
                child is not None
                and replacement.get("retry_from_session_id") == authority.get("blocked_session_id")
                and replacement.get("development_probe_job_ids")
                == [
                    *authority.get("development_probe_job_ids", []),
                    child.get("job_id"),
                ]
            )
        if cross_bindings_valid and group is not None:
            cross_bindings_valid = bool(
                replacement is not None
                and child is not None
                and group.get("session_id") == replacement.get("session_id")
                and group.get("blocked_session_id") == authority.get("blocked_session_id")
                and group.get("child_probe_job_id") == child.get("job_id")
            )
        if cross_bindings_valid and result is not None:
            proxy = result.get("proxy") if isinstance(result, dict) else None
            proxy_media = intent.get("proxy_media") if isinstance(intent, dict) else None
            repair_evidence = intent.get("repair_evidence") if isinstance(intent, dict) else None
            cross_bindings_valid = bool(
                isinstance(proxy, dict)
                and isinstance(proxy_media, dict)
                and isinstance(repair_evidence, dict)
                and proxy.get("review_proxy_id") == repair_id
                and proxy.get("proxy_media_sha256") == proxy_media.get("sha256")
                and proxy.get("proxy_size_bytes") == proxy_media.get("size_bytes")
                and proxy.get("proxy_width") == proxy_media.get("width")
                and proxy.get("proxy_height") == proxy_media.get("height")
                and proxy.get("proxy_frame_count") == authority.get("source_frame_count")
                and proxy.get("proxy_frame_count") == proxy_media.get("frame_count")
                and proxy.get("proxy_fps") == authority.get("source_fps")
                and proxy.get("proxy_fps") == proxy_media.get("fps")
                and proxy.get("sampled_artifact_count") == len(authority.get("frame_indices", []))
                and proxy.get("repair_execution_binding_sha256")
                == repair_evidence.get("repair_execution_binding_sha256")
                and proxy.get("repair_code_bundle_sha256") == repair_evidence.get("repair_code_bundle_sha256")
                and proxy.get("repair_runtime_sha256") == repair_evidence.get("repair_runtime_sha256")
                and proxy.get("repair_decoder_fingerprint_sha256")
                == repair_evidence.get("repair_decoder_fingerprint_sha256")
            )
        error_code = record.get("error_code")
        blocker_code = record.get("blocker_code")
        recovery_action = record.get("recovery_action")
        expected_recovery_action = cls._review_proxy_expected_recovery_action(
            status=str(status),
            rank=rank,
            error_code=error_code if isinstance(error_code, str) else None,
        )
        errors_valid = bool(
            (
                status in {"failed", "blocked"}
                and isinstance(error_code, str)
                and bool(error_code)
                and (
                    status == "failed"
                    and blocker_code is None
                    or status == "blocked"
                    and isinstance(blocker_code, str)
                    and bool(blocker_code)
                    and blocker_code == error_code
                )
                and recovery_action == expected_recovery_action
            )
            or (
                status not in {"failed", "blocked"}
                and error_code is None
                and blocker_code is None
                and recovery_action is None
            )
        )
        return bool(
            intent_valid
            and lineage_valid
            and record.get("request_sha256") == cls._expected_detector_review_proxy_request_sha256(record)
            and expected is not None
            and present == expected
            and status_phase_valid
            and summaries_valid
            and cross_bindings_valid
            and errors_valid
        )

    def _public_detector_review_proxy_repair(self, record: dict[str, Any]) -> dict[str, Any]:
        public = {
            key: deepcopy(record[key])
            for key in (
                "schema_version",
                "repair_id",
                "attempt_root_repair_id",
                "attempt_number",
                "retry_from_repair_id",
                "idempotency_key",
                "request_sha256",
                "status",
                "stage",
                "preset_id",
                "eligibility",
                "authority",
                "error_code",
                "blocker_code",
                "recovery_action",
                "created_at",
                "updated_at",
            )
        }
        public["artifact_type"] = "detector_review_proxy_repair_job"
        low_progress = record.get("low_progress")
        source_total = int(record["authority"]["source_frame_count"])
        source_completed = 0
        if isinstance(low_progress, dict):
            low_completed = low_progress.get("completed")
            low_total = low_progress.get("total")
            expected_low_total = source_total * 3 + len(record["authority"]["frame_indices"])
            if (
                isinstance(low_completed, bool)
                or not isinstance(low_completed, int)
                or isinstance(low_total, bool)
                or not isinstance(low_total, int)
                or low_total != expected_low_total
                or not 0 <= low_completed <= low_total
            ):
                raise DetectorDevelopmentError(
                    "invalid_review_proxy_progress",
                    "Review-proxy progress differs from frozen work authority",
                )
            source_completed = min(
                source_total,
                (source_total * low_completed) // low_total,
            )
        if self._review_proxy_stage_completed(record) >= 1:
            source_completed = source_total
        public["progress"] = {
            "stage_completed": self._review_proxy_stage_completed(record),
            "stage_total": 6,
            "source_frames_completed": source_completed,
            "source_frames_total": source_total,
            "updated_at": record["updated_at"],
        }
        journal_rank = self._review_proxy_stage_completed(record)
        actual_floor = self._review_proxy_actual_side_effect_floor(record)
        if record.get("status") in {"failed", "blocked", "cancelled"} and actual_floor > journal_rank:
            raise DetectorDevelopmentError(
                "review_proxy_side_effect_floor_mismatch",
                "Repair journal is behind an immutable continuation side effect",
                status_code=409,
            )
        public["can_cancel"] = record.get("status") in {"queued", "running"}
        public["can_retry"] = self._can_retry_detector_review_proxy_repair(record)
        public["result"] = deepcopy(record.get("result")) if record.get("status") == "ready" else None
        base = f"/api/v1/detector-review-proxy-repairs/{record['repair_id']}"
        public["status_url"] = base
        public["cancel_url"] = f"{base}/cancel"
        public["retry_url"] = f"{base}/retry"
        return public

    def _start_detector_review_proxy_continuation(self, repair_id: str) -> None:
        repair_id = require_safe_id(repair_id, "repair_id")
        key = f"review-proxy-{repair_id}"
        with self._lock:
            if self._closing or key in self._active_threads or key in self._starting_threads:
                return
            thread = threading.Thread(
                target=self._watch_detector_review_proxy_repair,
                args=(repair_id, key),
                name=f"review-proxy-continuation-{repair_id}",
                daemon=True,
            )
            self._active_threads[key] = thread
            self._starting_threads.add(key)
        try:
            thread.start()
        except BaseException:
            with self._lock:
                self._active_threads.pop(key, None)
            raise
        finally:
            with self._lock:
                self._starting_threads.discard(key)

    def _watch_detector_review_proxy_repair(self, repair_id: str, thread_key: str) -> None:
        try:
            wait = threading.Event()
            while not self._closing:
                if self._advance_detector_review_proxy_repair(repair_id):
                    return
                wait.wait(0.1)
        except Exception as exc:
            if self._closing or getattr(exc, "code", None) == "service_closed":
                return
            with self._detector_review_proxy_lock:
                try:
                    record = self._read_detector_review_proxy_repair(repair_id)
                except Exception:
                    return
                if record.get("status") not in {
                    "ready",
                    "failed",
                    "blocked",
                    "cancelled",
                }:
                    try:
                        actual_rank, _reconciled = self._reconcile_review_proxy_lower_side_effects(record)
                    except DetectorDevelopmentError as floor_error:
                        if floor_error.code != "review_proxy_side_effect_floor_unverifiable":
                            raise
                        # Preserve the already valid journal prefix. Read paths
                        # still fail closed against the damaged lower store;
                        # this handler must not erase a committed phase while
                        # recording an unrelated worker failure.
                        actual_rank = self._review_proxy_stage_completed(record)
                    completed_rank = self._review_proxy_stage_completed(record)
                    if actual_rank > completed_rank:
                        # A verified claim without its deterministic job row is
                        # already a non-retryable side effect, but there is no
                        # child summary to journal yet. Keep the same attempt
                        # resumable instead of publishing a false terminal row.
                        record.update(
                            status="committing",
                            error_code=None,
                            blocker_code=None,
                            recovery_action=None,
                            updated_at=_utc_now_iso(),
                        )
                        self._persist_detector_review_proxy_repair(record)
                        return
                    code = str(getattr(exc, "code", "review_proxy_continuation_failed"))
                    blocked = getattr(exc, "status_code", None) == 409
                    terminal_status = "blocked" if blocked else "failed"
                    record.update(
                        status=terminal_status,
                        error_code=code,
                        blocker_code=(code if blocked else None),
                        recovery_action=self._review_proxy_expected_recovery_action(
                            status=terminal_status,
                            rank=completed_rank,
                            error_code=code,
                        ),
                        error_message=str(exc)[:1000],
                        updated_at=_utc_now_iso(),
                    )
                    # Preserve the last durably completed phase whenever
                    # evidence has already been published.  Collapsing stage
                    # to "failed" would make the journal contradict its own
                    # child/session/group bindings and could tempt a restart
                    # to republish them.
                    if completed_rank == 0:
                        record["stage"] = terminal_status
                    self._persist_detector_review_proxy_repair(record)
        finally:
            with self._lock:
                self._active_threads.pop(thread_key, None)

    def _advance_detector_review_proxy_repair(self, repair_id: str) -> bool:
        with self._detector_review_proxy_lock:
            record = self._read_detector_review_proxy_repair(repair_id)
            if record["status"] in {"ready", "failed", "blocked", "cancelled"}:
                if record.get("recovery_action") == "resume" and self._review_proxy_stage_completed(record) >= 1:
                    record.update(
                        status="committing",
                        error_code=None,
                        blocker_code=None,
                        recovery_action=None,
                        updated_at=_utc_now_iso(),
                    )
                    self._persist_detector_review_proxy_repair(record)
                else:
                    return True
        coordinator = self._detector_review_proxy_coordinator()
        low = coordinator.get_repair(repair_id)
        low_status = low.get("status")
        if low_status == "ready":
            verified = coordinator.get_verified_proxy(repair_id)
            self._commit_detector_review_proxy_continuation(repair_id, verified)
            return True
        with self._detector_review_proxy_lock:
            record = self._read_detector_review_proxy_repair(repair_id)
            if record["status"] in {"ready", "failed", "blocked", "cancelled"}:
                return True
            record["low_progress"] = deepcopy(low.get("progress"))
            record["updated_at"] = _utc_now_iso()
            if low_status in {"queued", "running"}:
                record.update(status=low_status, stage=str(low.get("stage") or low_status))
                self._persist_detector_review_proxy_repair(record)
                return False
            if low_status == "committing":
                record.update(status="committing", stage="proxy_committing")
                self._persist_detector_review_proxy_repair(record)
                return False
            if low_status in {"failed", "blocked", "cancelled"}:
                error_code = (
                    str(low.get("error_code") or "review_proxy_failed") if low_status in {"failed", "blocked"} else None
                )
                record.update(
                    status=low_status,
                    stage=str(low.get("stage") or low_status),
                    error_code=error_code,
                    blocker_code=(error_code if low_status == "blocked" else None),
                    recovery_action=self._review_proxy_expected_recovery_action(
                        status=str(low_status),
                        rank=self._review_proxy_stage_completed({"stage": str(low.get("stage") or low_status)}),
                        error_code=error_code,
                    ),
                )
                self._persist_detector_review_proxy_repair(record)
                return True
            raise DetectorDevelopmentError(
                "invalid_review_proxy_status",
                "Detector review proxy reported an invalid status",
            )

    @staticmethod
    def _build_review_proxy_repair_authority(annotation: dict[str, Any], parent: dict[str, Any]) -> dict[str, Any]:
        source = annotation["source"]
        return {
            "blocked_session_id": annotation["blocked_session_id"],
            "blocked_session_request_sha256": annotation["blocked_session_request_sha256"],
            "blocked_session_record_sha256": annotation["blocked_session_record_sha256"],
            "parent_probe_job_id": parent["parent_probe_job_id"],
            "development_probe_job_ids": deepcopy(annotation["development_probe_job_ids"]),
            "parent_probe_request_sha256": parent["parent_probe_request_sha256"],
            "parent_probe_intent_sha256": parent["parent_probe_intent_sha256"],
            "parent_probe_semantic_intent_sha256": parent["parent_probe_semantic_intent_sha256"],
            "parent_probe_report_sha256": parent["parent_probe_report_sha256"],
            "parent_probe_result_manifest_sha256": parent["parent_probe_result_manifest_sha256"],
            "parent_probe_record_sha256": parent["parent_probe_record_sha256"],
            "parent_execution_bundle_sha256": parent["parent_execution_bundle_sha256"],
            "parent_runtime_environment_sha256": parent["parent_runtime_environment_sha256"],
            "source_frame_evidence_sha256": parent["source_frame_evidence_sha256"],
            "source_id": source["source_id"],
            "source_sha256": source["sha256"],
            "source_file_identity_sha256": source["file_identity_sha256"],
            "source_size_bytes": source["size_bytes"],
            "source_width": source["width"],
            "source_height": source["height"],
            "source_frame_count": source["frame_count"],
            "source_fps": source["fps"],
            "locked_profile_id": annotation["locked_profile"]["profile_id"],
            "locked_profile_sha256": annotation["locked_profile"]["profile_sha256"],
            "frame_indices": deepcopy(annotation["frame_indices"]),
            "sampling_manifest_sha256": annotation["sampling_manifest_sha256"],
            "temporal_groups_sha256": annotation["temporal_groups_sha256"],
            "candidate_evidence_sha256": parent["candidate_evidence_sha256"],
            "replacement_request_authority_sha256": annotation["replacement_request_authority_sha256"],
        }

    @staticmethod
    def _validate_review_proxy_repair_authority(annotation: dict[str, Any], parent: dict[str, Any]) -> None:
        annotation_source = annotation.get("source")
        parent_source = parent.get("source")
        locked_profile = annotation.get("locked_profile")
        frozen_profiles = parent.get("frozen_profiles")
        if (
            not isinstance(annotation_source, dict)
            or not isinstance(parent_source, dict)
            or not isinstance(locked_profile, dict)
            or not isinstance(frozen_profiles, list)
            or annotation.get("parent_probe_job_id") != parent.get("parent_probe_job_id")
            or annotation.get("frame_indices") != parent.get("frame_indices")
        ):
            raise DetectorDevelopmentError(
                "review_proxy_authority_mismatch",
                "Blocked session and audited parent authority disagree",
                status_code=409,
            )
        expected_source = {
            "source_id": parent_source.get("source_id"),
            "relative_path": parent_source.get("relative_path"),
            "sha256": parent_source.get("sha256"),
            "file_identity_sha256": parent_source.get("file_identity_sha256"),
            "size_bytes": parent_source.get("size_bytes"),
            "width": parent_source.get("width"),
            "height": parent_source.get("height"),
            "frame_count": parent_source.get("frame_count"),
        }
        if any(annotation_source.get(key) != value for key, value in expected_source.items()):
            raise DetectorDevelopmentError(
                "review_proxy_authority_mismatch",
                "Blocked session source differs from the audited parent",
                status_code=409,
            )
        fps = annotation_source.get("fps")
        if (
            isinstance(fps, bool)
            or not isinstance(fps, (int, float))
            or not math.isclose(
                float(fps),
                float(parent.get("report_decode_fps", 0.0)),
                rel_tol=0.0,
                abs_tol=1e-9,
            )
        ):
            raise DetectorDevelopmentError(
                "review_proxy_authority_mismatch",
                "Blocked session FPS differs from the audited parent",
                status_code=409,
            )
        bound_profile = next(
            (
                item
                for item in frozen_profiles
                if isinstance(item, dict) and item.get("profile_id") == locked_profile.get("profile_id")
            ),
            None,
        )
        if not isinstance(bound_profile, dict) or bound_profile.get("profile_sha256") != locked_profile.get(
            "profile_sha256"
        ):
            raise DetectorDevelopmentError(
                "review_proxy_authority_mismatch",
                "Locked detector profile differs from the audited parent",
                status_code=409,
            )

    def _hit_detector_review_proxy_failpoint(self, stage: str) -> None:
        callback = self._detector_review_proxy_failpoint
        if callback is not None:
            callback(stage)

    def _load_verified_review_proxy_evidence(
        self, record: dict[str, Any], low: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], dict[int, bytes]]:
        repair_id = record["repair_id"]
        report = low.get("report")
        if (
            low.get("status") != "ready"
            or low.get("repair_id") != repair_id
            or low.get("request_sha256") != record.get("low_request_sha256")
            or low.get("attempt_root_repair_id") != record.get("attempt_root_repair_id")
            or low.get("attempt_number") != record.get("attempt_number")
            or low.get("retry_from_repair_id") != record.get("retry_from_repair_id")
            or not isinstance(report, dict)
        ):
            raise DetectorDevelopmentError(
                "review_proxy_result_mismatch",
                "Verified review proxy does not match its continuation",
            )
        self._verify_review_proxy_attempt_lineage(record)
        result_root = require_trusted_relative_path(
            self.repo_root,
            f"data/ball_detector_development_v1/review_proxies/results/{repair_id}",
            "review proxy result root",
            allowed_first_parts={"data"},
        )
        report_bytes, report_file_sha256 = read_regular_bytes(
            result_root / "detector_review_proxy_report.v1.json",
            "review proxy report",
            max_bytes=4 * 1024 * 1024,
            trusted_root=result_root,
        )
        manifest_bytes, manifest_sha256 = read_regular_bytes(
            result_root / "detector_review_proxy_manifest.v1.json",
            "review proxy result manifest",
            max_bytes=4 * 1024 * 1024,
            trusted_root=result_root,
        )
        disk_report = json_object_from_bytes(report_bytes, "review proxy report")
        disk_manifest = json_object_from_bytes(manifest_bytes, "review proxy result manifest")
        if (
            manifest_sha256 != low.get("result_manifest_sha256")
            or report != disk_report
            or disk_report.get("schema_version") != "1.0"
            or disk_report.get("artifact_type") != "detector_review_proxy_report"
            or disk_report.get("repair_id") != repair_id
            or disk_report.get("request_sha256") != record.get("low_request_sha256")
            or disk_manifest.get("schema_version") != "1.0"
            or disk_manifest.get("artifact_type") != "detector_review_proxy_result_manifest"
            or disk_manifest.get("repair_id") != repair_id
            or disk_manifest.get("request_sha256") != record.get("low_request_sha256")
            or disk_manifest.get("report_file_sha256") != report_file_sha256
            or disk_manifest.get("report_file_size_bytes") != len(report_bytes)
            or not isinstance(disk_report.get("integrity"), dict)
            or disk_manifest.get("integrity_sha256") != canonical_sha256(disk_report["integrity"])
        ):
            raise DetectorDevelopmentError(
                "review_proxy_manifest_mismatch",
                "Review proxy manifest changed before continuation",
            )
        # From here onward only the bytes re-read from the immutable published
        # tree are authoritative.  The coordinator's in-memory/public copy was
        # compared above solely to detect a split-brain return value.
        report = disk_report
        proxy = report.get("proxy")
        binding = report.get("repair_execution_binding")
        samples = report.get("sampled_frames")
        if not isinstance(proxy, dict) or not isinstance(binding, dict) or not isinstance(samples, list):
            raise DetectorDevelopmentError(
                "invalid_review_proxy_result",
                "Review proxy evidence is incomplete",
            )
        proxy_path = require_trusted_relative_path(
            result_root,
            proxy.get("relative_path"),
            "review proxy media",
        )
        sealed_proxy_size = proxy.get("size_bytes")
        if (
            isinstance(sealed_proxy_size, bool)
            or not isinstance(sealed_proxy_size, int)
            or not 0 < sealed_proxy_size <= 32 * 1024 * 1024 * 1024
        ):
            raise DetectorDevelopmentError(
                "invalid_review_proxy_result",
                "Review proxy media size authority is invalid",
            )
        sample_paths: list[tuple[int, dict[str, Any], Path]] = []
        for item in samples:
            if not isinstance(item, dict):
                raise DetectorDevelopmentError(
                    "invalid_review_proxy_samples",
                    "Review proxy sample binding is invalid",
                )
            frame_index = item.get("frame_index")
            if isinstance(frame_index, bool) or not isinstance(frame_index, int):
                raise DetectorDevelopmentError(
                    "invalid_review_proxy_samples",
                    "Review proxy sample index is invalid",
                )
            sample_paths.append(
                (
                    frame_index,
                    item,
                    require_trusted_relative_path(
                        result_root,
                        item.get("relative_path"),
                        "review proxy sample",
                    ),
                )
            )
        expected_files = {
            "detector_review_proxy_report.v1.json",
            "detector_review_proxy_manifest.v1.json",
            proxy_path.relative_to(result_root).as_posix(),
            *(path.relative_to(result_root).as_posix() for _, _, path in sample_paths),
        }
        try:
            tree_before = exact_regular_tree_snapshot(
                result_root,
                expected_files,
                "review proxy result tree",
                trusted_root=self.repo_root,
            )
        except DetectorDevelopmentError as exc:
            if exc.code in {
                "unexpected_result_artifact",
                "invalid_result_allowlist",
            }:
                raise DetectorDevelopmentError(
                    "unexpected_review_proxy_artifact",
                    "Review proxy result tree contains unexpected artifacts",
                ) from exc
            raise
        repeated_report_bytes, repeated_report_sha256 = read_regular_bytes(
            result_root / "detector_review_proxy_report.v1.json",
            "review proxy report",
            max_bytes=4 * 1024 * 1024,
            trusted_root=result_root,
        )
        repeated_manifest_bytes, repeated_manifest_sha256 = read_regular_bytes(
            result_root / "detector_review_proxy_manifest.v1.json",
            "review proxy result manifest",
            max_bytes=4 * 1024 * 1024,
            trusted_root=result_root,
        )
        if (
            repeated_report_bytes != report_bytes
            or repeated_report_sha256 != report_file_sha256
            or repeated_manifest_bytes != manifest_bytes
            or repeated_manifest_sha256 != manifest_sha256
        ):
            raise DetectorDevelopmentError(
                "review_proxy_manifest_mismatch",
                "Review proxy documents changed during continuation verification",
            )
        proxy_sha256, proxy_size = hash_regular_file(
            proxy_path,
            "review proxy media",
            max_bytes=sealed_proxy_size,
            trusted_root=result_root,
        )
        if (
            proxy_sha256 != proxy.get("sha256")
            or proxy_size != proxy.get("size_bytes")
            or proxy_sha256 != disk_manifest.get("proxy_sha256")
            or proxy_size != disk_manifest.get("proxy_size_bytes")
        ):
            raise DetectorDevelopmentError(
                "review_proxy_digest_mismatch",
                "Review proxy media changed before continuation",
            )
        sample_bytes: dict[int, bytes] = {}
        sample_sha256s: dict[str, str] = {}
        for frame_index, item, sample_path in sample_paths:
            content, digest = read_regular_bytes(
                sample_path,
                "review proxy sample",
                max_bytes=32 * 1024 * 1024,
                trusted_root=result_root,
            )
            if digest != item.get("sha256") or len(content) != item.get("size_bytes"):
                raise DetectorDevelopmentError(
                    "review_proxy_sample_mismatch",
                    "Review proxy sample changed before continuation",
                )
            sample_bytes[frame_index] = content
            sample_sha256s[str(frame_index)] = digest
        if sorted(sample_bytes) != record["authority"]["frame_indices"]:
            raise DetectorDevelopmentError(
                "review_proxy_sample_mismatch",
                "Review proxy samples differ from frozen frame authority",
            )
        if [sample_sha256s[str(index)] for index in sorted(sample_bytes)] != disk_manifest.get("sample_sha256s"):
            raise DetectorDevelopmentError(
                "review_proxy_sample_mismatch",
                "Review proxy sample manifest changed before continuation",
            )
        try:
            tree_after = exact_regular_tree_snapshot(
                result_root,
                expected_files,
                "review proxy result tree",
                trusted_root=self.repo_root,
            )
        except DetectorDevelopmentError as exc:
            if exc.code in {
                "unexpected_result_artifact",
                "invalid_result_allowlist",
            }:
                raise DetectorDevelopmentError(
                    "unexpected_review_proxy_artifact",
                    "Review proxy result tree contains unexpected artifacts",
                ) from exc
            raise
        if tree_after != tree_before:
            raise DetectorDevelopmentError(
                "source_changed",
                "Review proxy result tree changed during continuation verification",
            )
        binding_without_digest = deepcopy(binding)
        binding_sha256 = binding_without_digest.pop("binding_sha256", None)
        if (
            canonical_sha256(binding_without_digest) != binding_sha256
            or canonical_sha256(binding.get("code_files")) != binding.get("code_bundle_sha256")
            or canonical_sha256(binding.get("runtime")) != binding.get("runtime_sha256")
        ):
            raise DetectorDevelopmentError(
                "repair_execution_binding_changed",
                "Review proxy execution binding is invalid",
            )
        repair_evidence = {
            "schema_version": "1.0",
            "repair_id": repair_id,
            "repair_request_sha256": record["low_request_sha256"],
            "repair_report_sha256": report_file_sha256,
            "repair_result_manifest_sha256": manifest_sha256,
            "proxy_media_sha256": proxy_sha256,
            "proxy_size_bytes": proxy_size,
            "repair_execution_binding_sha256": binding_sha256,
            "repair_code_bundle_sha256": binding["code_bundle_sha256"],
            "repair_runtime_sha256": binding["runtime_sha256"],
            "repair_decoder_fingerprint_sha256": binding["decoder_fingerprint_sha256"],
            "sampled_frame_sha256s": sample_sha256s,
        }
        proxy_media = {
            "sha256": proxy_sha256,
            "size_bytes": proxy_size,
            "width": proxy["width"],
            "height": proxy["height"],
            "frame_count": proxy["frame_count"],
            "fps": float(proxy["fps"]),
        }
        return repair_evidence, proxy_media, sample_bytes

    def _verify_review_proxy_attempt_lineage(self, record: dict[str, Any]) -> None:
        """Bind a retry attempt to its one immutable upper parent."""

        repair_id = require_safe_id(record.get("repair_id"), "repair_id")
        attempt_number = record.get("attempt_number")
        root_id = require_safe_id(
            record.get("attempt_root_repair_id"),
            "attempt root repair_id",
        )
        retry_from = record.get("retry_from_repair_id")
        if isinstance(attempt_number, bool) or not isinstance(attempt_number, int) or attempt_number < 1:
            raise DetectorDevelopmentError(
                "invalid_review_proxy_retry",
                "Review-proxy attempt lineage is invalid",
            )
        if attempt_number == 1:
            if root_id != repair_id or retry_from is not None:
                raise DetectorDevelopmentError(
                    "invalid_review_proxy_retry",
                    "Initial review-proxy attempt lineage is invalid",
                )
            return
        parent_id = require_safe_id(retry_from, "retry parent repair_id")
        with self._detector_review_proxy_lock:
            parent = self._read_detector_review_proxy_repair(parent_id)
            descendants = [
                self._read_detector_review_proxy_repair(path.stem)
                for path in sorted(self._detector_review_proxy_jobs_root.glob("*.json"))
                if path.stem != parent_id
            ]
        matching_descendants = [
            candidate for candidate in descendants if candidate.get("retry_from_repair_id") == parent_id
        ]
        if (
            parent.get("attempt_root_repair_id") != root_id
            or parent.get("attempt_number") != attempt_number - 1
            or parent.get("authority") != record.get("authority")
            or len(matching_descendants) != 1
            or matching_descendants[0].get("repair_id") != repair_id
        ):
            raise DetectorDevelopmentError(
                "invalid_review_proxy_retry",
                "Review-proxy retry changed its immutable parent lineage",
            )

    @staticmethod
    def _build_review_proxy_result_proxy(
        *,
        record: dict[str, Any],
        low: dict[str, Any],
        child: dict[str, Any],
        proxy_media: dict[str, Any],
        samples: dict[int, bytes],
    ) -> dict[str, Any]:
        try:
            manifest = child["report"]["review_proxy_manifest"]
            repair_binding = low["report"]["repair_execution_binding"]
            authority = record["authority"]
            expected_indices = authority["frame_indices"]
            manifest_proxy = manifest["proxy"]
            manifest_source = manifest["source"]
            if (
                sorted(samples) != expected_indices
                or manifest["expected_frame_indices"] != expected_indices
                or manifest_source["sha256"] != authority["source_sha256"]
                or manifest_source["file_identity_sha256"] != authority["source_file_identity_sha256"]
                or manifest_source["size_bytes"] != authority["source_size_bytes"]
                or manifest_proxy["sha256"] != proxy_media["sha256"]
                or manifest_proxy["size_bytes"] != proxy_media["size_bytes"]
                or manifest_proxy["width"] != proxy_media["width"]
                or manifest_proxy["height"] != proxy_media["height"]
                or manifest_proxy["frame_count"] != proxy_media["frame_count"]
                or not math.isclose(
                    float(manifest_proxy["fps"]),
                    float(proxy_media["fps"]),
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
            ):
                raise ValueError("proxy result authority mismatch")
            return {
                "review_proxy_id": record["repair_id"],
                "review_proxy_manifest_sha256": manifest["manifest_sha256"],
                "proxy_media_sha256": proxy_media["sha256"],
                "proxy_size_bytes": proxy_media["size_bytes"],
                "proxy_width": proxy_media["width"],
                "proxy_height": proxy_media["height"],
                "proxy_frame_count": proxy_media["frame_count"],
                "proxy_fps": proxy_media["fps"],
                "mapping_sha256": manifest["mapping_sha256"],
                "sampled_artifact_count": len(samples),
                "encoder_binding_sha256": repair_binding["encoder_preset_sha256"],
                "repair_execution_binding_sha256": repair_binding["binding_sha256"],
                "repair_code_bundle_sha256": repair_binding["code_bundle_sha256"],
                "repair_runtime_sha256": repair_binding["runtime_sha256"],
                "repair_decoder_fingerprint_sha256": repair_binding["decoder_fingerprint_sha256"],
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise DetectorDevelopmentError(
                "review_proxy_result_mismatch",
                "Review-proxy result authority is incomplete or inconsistent",
            ) from exc

    @staticmethod
    def _review_proxy_child_summary(child: dict[str, Any]) -> dict[str, Any]:
        frozen = child.get("frozen_request")
        report = child.get("report")
        upgrade = frozen.get("review_proxy_upgrade") if isinstance(frozen, dict) else None
        continuation = upgrade.get("continuation_execution_binding") if isinstance(upgrade, dict) else None
        if (
            child.get("status") != "ready"
            or not isinstance(frozen, dict)
            or not isinstance(report, dict)
            or not isinstance(continuation, dict)
        ):
            raise DetectorDevelopmentError(
                "review_proxy_child_not_ready",
                "Review-proxy child probe is incomplete",
            )
        job_id = require_safe_id(child.get("job_id"), "child probe job_id")
        return {
            "job_id": job_id,
            "request_sha256": child["request_sha256"],
            "intent_sha256": child["intent_sha256"],
            "semantic_intent_sha256": child["semantic_intent_sha256"],
            "resource_sha256": child["resource_sha256"],
            "frozen_profiles_sha256": child["frozen_profiles_sha256"],
            "report_sha256": report["report_sha256"],
            "result_manifest_sha256": child["result_manifest_sha256"],
            "execution_bundle_sha256": frozen["execution_bundle_sha256"],
            "runtime_environment_sha256": frozen["runtime_environment_sha256"],
            "continuation_execution_binding_sha256": continuation["binding_sha256"],
            "continuation_code_bundle_sha256": continuation["code_bundle_sha256"],
            "continuation_runtime_sha256": continuation["runtime_sha256"],
            "retry_from_job_id": child["retry_from_job_id"],
            "retry_kind": "review_proxy_decode_upgrade",
            "status_url": f"/api/v1/detector-probes/{job_id}",
            "report_url": f"/api/v1/detector-probes/{job_id}",
        }

    @staticmethod
    def _review_proxy_session_summary(session: dict[str, Any]) -> dict[str, Any]:
        retry_lineage = session.get("retry_lineage")
        lineage = session.get("lineage")
        artifact_type = session.get("artifact_type")
        lifecycle = (session.get("status"), session.get("stage"))
        lifecycle_valid = lifecycle in {
            ("annotating", "annotating"),
            ("finalizing", "finalizing"),
            ("finalized", "finalized"),
        }
        if artifact_type is None and session.get("status") == "annotating" and "stage" not in session:
            # Minimal internal test doubles predate the persisted stage field.
            lifecycle_valid = True
        if (
            not lifecycle_valid
            or not isinstance(retry_lineage, dict)
            or retry_lineage.get("mode") != "review_proxy_decode_upgrade"
            or not isinstance(lineage, dict)
        ):
            raise DetectorDevelopmentError(
                "replacement_session_mismatch",
                "Review-proxy replacement session is invalid",
            )
        session_id = require_safe_id(session.get("session_id"), "replacement session_id")
        return {
            "session_id": session_id,
            "request_sha256": session["request_sha256"],
            "status": "annotating",
            "retry_from_session_id": session["retry_from_session_id"],
            "retry_mode": "review_proxy_decode_upgrade",
            "attempt_family_sha256": session["attempt_family_sha256"],
            "development_probe_job_ids": deepcopy(lineage["development_probe_job_ids"]),
            "status_url": f"/api/v1/ball-annotation-sessions/{session_id}",
        }

    def _commit_detector_review_proxy_continuation(self, repair_id: str, low: dict[str, Any]) -> None:
        with self._detector_review_proxy_lock:
            record = self._read_detector_review_proxy_repair(repair_id)
            first_proxy_ready = record.get("stage") not in {
                "proxy_ready",
                "continuation_intent",
                "child_probe_ready",
                "replacement_session_ready",
                "groups_published",
                "ready",
            }
            record.update(
                status="committing",
                low_progress=deepcopy(low.get("progress")),
                updated_at=_utc_now_iso(),
            )
            if self._review_proxy_stage_completed(record) < 1:
                record["stage"] = "proxy_ready"
            self._persist_detector_review_proxy_repair(record)
        if first_proxy_ready:
            self._hit_detector_review_proxy_failpoint("after_proxy_ready")

        repair_evidence, proxy_media, samples = self._load_verified_review_proxy_evidence(record, low)
        detector = self._detector_development_service()
        existing_intent = record.get("continuation_intent")
        if existing_intent is None:
            child_plan = detector.review_proxy_upgrade_child_plan(
                record["authority"]["parent_probe_job_id"],
                repair_evidence=repair_evidence,
            )
            intent_body = {
                "schema_version": "1.0",
                "artifact_type": "detector_review_proxy_continuation_intent",
                "repair_id": repair_id,
                "authority_sha256": canonical_sha256(record["authority"]),
                "repair_evidence": repair_evidence,
                "proxy_media": proxy_media,
                "child_plan": child_plan,
            }
            intent = {**intent_body, "intent_sha256": canonical_sha256(intent_body)}
        else:
            intent = deepcopy(existing_intent)
            child_plan = intent.get("child_plan")
            if (
                not isinstance(child_plan, dict)
                or intent.get("repair_evidence") != repair_evidence
                or intent.get("proxy_media") != proxy_media
            ):
                raise DetectorDevelopmentError(
                    "continuation_intent_changed",
                    "Review-proxy continuation evidence changed after commit",
                )
        with self._detector_review_proxy_lock:
            record = self._read_detector_review_proxy_repair(repair_id)
            committed_intent = record.get("continuation_intent")
            if committed_intent is not None and committed_intent != intent:
                raise DetectorDevelopmentError(
                    "continuation_intent_changed",
                    "Review-proxy continuation intent changed after commit",
                )
            first_intent = committed_intent is None
            record.update(
                status="committing",
                continuation_intent=intent,
                updated_at=_utc_now_iso(),
            )
            if self._review_proxy_stage_completed(record) < 2:
                record["stage"] = "continuation_intent"
            self._persist_detector_review_proxy_repair(record)
        if first_intent:
            self._hit_detector_review_proxy_failpoint("after_continuation_intent")

        existing_child = record.get("child_probe")
        if existing_child is None:
            child_public = detector.create_review_proxy_upgrade_child(
                record["authority"]["parent_probe_job_id"],
                repair_evidence=repair_evidence,
                proxy_media=proxy_media,
                proxy_sample_bytes=samples,
                expected_child_plan=child_plan,
            )
        else:
            child_public = detector.get_review_proxy_upgrade_child(record["authority"]["parent_probe_job_id"])
        if not isinstance(child_public, dict):
            raise DetectorDevelopmentError(
                "child_probe_changed",
                "Review-proxy child is absent after commit",
            )
        child = detector.get_verified_probe_job_record(
            require_safe_id(child_public.get("job_id"), "review-proxy child probe job_id")
        )
        child_summary = self._review_proxy_child_summary(child)
        child_upgrade = child["frozen_request"]["review_proxy_upgrade"]
        expected_child_bindings = {
            "request_sha256": child_plan.get("request_sha256"),
            "intent_sha256": child_plan.get("intent_sha256"),
            "semantic_intent_sha256": child_plan.get("semantic_intent_sha256"),
            "resource_sha256": child_plan.get("resource_sha256"),
            "frozen_profiles_sha256": child_plan.get("frozen_profiles_sha256"),
            "execution_bundle_sha256": child_plan.get("execution_bundle_sha256"),
            "runtime_environment_sha256": child_plan.get("runtime_environment_sha256"),
            "continuation_execution_binding_sha256": child_plan.get("continuation_execution_binding", {}).get(
                "binding_sha256"
            ),
            "retry_from_job_id": record["authority"]["parent_probe_job_id"],
            "retry_kind": "review_proxy_decode_upgrade",
        }
        if (
            not isinstance(child_upgrade, dict)
            or child_upgrade.get("repair_evidence", {}).get("repair_id") != repair_id
            or any(child_summary.get(key) != value for key, value in expected_child_bindings.items())
        ):
            raise DetectorDevelopmentError(
                "child_probe_changed",
                "Review-proxy child differs from its frozen continuation plan",
            )
        with self._detector_review_proxy_lock:
            record = self._read_detector_review_proxy_repair(repair_id)
            committed_child = record.get("child_probe")
            if committed_child is not None and committed_child != child_summary:
                raise DetectorDevelopmentError(
                    "child_probe_changed",
                    "Review-proxy child identity changed after commit",
                )
            first_child = committed_child is None
            record.update(
                child_probe=child_summary,
                updated_at=_utc_now_iso(),
            )
            if self._review_proxy_stage_completed(record) < 3:
                record["stage"] = "child_probe_ready"
            self._persist_detector_review_proxy_repair(record)
        if first_child:
            self._hit_detector_review_proxy_failpoint("after_child_ready")

        annotation = self._ball_annotation_service()
        existing_group = record.get("group_publication")
        if existing_group is not None:
            group_witness = self._review_proxy_group_publication_summary(existing_group)
            if group_witness != existing_group:
                raise DetectorDevelopmentError(
                    "group_publication_changed",
                    "Stored replacement group witness changed",
                    status_code=409,
                )
            replacement = annotation.verify_ready_review_proxy_replacement(
                blocked_session_id=record["authority"]["blocked_session_id"],
                blocked_session_record_sha256=record["authority"]["blocked_session_record_sha256"],
                replacement_session_id=record["replacement_session"]["session_id"],
                child_probe_job_id=child_summary["job_id"],
                session_creation_authority_sha256=group_witness["session_creation_authority_sha256"],
                group_publication_sha256=group_witness["group_publication_sha256"],
            )["session"]
        else:
            replacement = annotation.create_review_proxy_replacement_session(
                record["authority"]["blocked_session_id"], child_summary["job_id"]
            )
        session_summary = self._review_proxy_session_summary(replacement)
        with self._detector_review_proxy_lock:
            record = self._read_detector_review_proxy_repair(repair_id)
            existing_session = record.get("replacement_session")
            if existing_session is not None and existing_session != session_summary:
                raise DetectorDevelopmentError(
                    "replacement_session_changed",
                    "Replacement annotation session changed after commit",
                )
            first_session = record.get("replacement_session") is None
            record.update(
                replacement_session=session_summary,
                updated_at=_utc_now_iso(),
            )
            if self._review_proxy_stage_completed(record) < 4:
                record["stage"] = "replacement_session_ready"
            self._persist_detector_review_proxy_repair(record)
        if first_session:
            self._hit_detector_review_proxy_failpoint("after_replacement_session")

        group_commit = (
            group_witness
            if existing_group is not None
            else annotation.get_review_proxy_replacement_commit(
                session_summary["session_id"],
                blocked_session_id=record["authority"]["blocked_session_id"],
                child_probe_job_id=child_summary["job_id"],
            )
        )
        with self._detector_review_proxy_lock:
            record = self._read_detector_review_proxy_repair(repair_id)
            first_groups = record.get("group_publication") is None
            group_publication = self._review_proxy_group_publication_summary(group_commit)
            if record.get("group_publication") is not None and record.get("group_publication") != group_publication:
                raise DetectorDevelopmentError(
                    "group_publication_changed",
                    "Replacement temporal-group publication changed after commit",
                )
            record.update(
                group_publication=group_publication,
                updated_at=_utc_now_iso(),
            )
            if self._review_proxy_stage_completed(record) < 5:
                record["stage"] = "groups_published"
            self._persist_detector_review_proxy_repair(record)
        if first_groups:
            self._hit_detector_review_proxy_failpoint("after_group_publication")

        parent_after = detector.get_review_proxy_upgrade_parent(record["authority"]["parent_probe_job_id"])[
            "parent_probe_record_sha256"
        ]
        if parent_after != record["authority"]["parent_probe_record_sha256"]:
            raise DetectorDevelopmentError(
                "historical_parent_changed",
                "Historical probe changed during review-proxy continuation",
            )
        annotation.verify_blocked_review_proxy_parent_immutable(
            record["authority"]["blocked_session_id"],
            record["authority"]["blocked_session_record_sha256"],
        )
        proxy_result = self._build_review_proxy_result_proxy(
            record=record,
            low=low,
            child=child,
            proxy_media=proxy_media,
            samples=samples,
        )
        with self._detector_review_proxy_lock:
            record = self._read_detector_review_proxy_repair(repair_id)
            record.update(
                status="ready",
                stage="ready",
                result={
                    "proxy": proxy_result,
                    "child_probe": child_summary,
                    "replacement_session": session_summary,
                    "parent_probe_record_sha256_after": parent_after,
                },
                error_code=None,
                blocker_code=None,
                recovery_action=None,
                updated_at=_utc_now_iso(),
            )
            self._persist_detector_review_proxy_repair(record)

    def _recover_detector_review_proxy_repairs(self) -> None:
        repair_ids: list[str] = []
        for path in sorted(self._detector_review_proxy_jobs_root.glob("*.json")):
            record = self._read_detector_review_proxy_repair(path.stem)
            journal_rank = self._review_proxy_stage_completed(record)
            terminal = record.get("status") in {
                "ready",
                "failed",
                "blocked",
                "cancelled",
            }
            discover_unjournaled_lower = bool(
                terminal and record.get("status") != "ready" and record.get("recovery_action") != "resume"
            )
            actual_rank = journal_rank
            reconciled = False
            if discover_unjournaled_lower:
                actual_rank, reconciled = self._reconcile_review_proxy_lower_side_effects(record)
            lower_ahead = actual_rank > journal_rank
            if lower_ahead:
                record.update(
                    status="committing",
                    error_code=None,
                    blocker_code=None,
                    recovery_action=None,
                    updated_at=_utc_now_iso(),
                )
                self._persist_detector_review_proxy_repair(record)
            elif reconciled:
                self._persist_detector_review_proxy_repair(record)
            if lower_ahead or not terminal or record.get("recovery_action") == "resume":
                repair_ids.append(record["repair_id"])
        for repair_id in repair_ids:
            self._start_detector_review_proxy_continuation(repair_id)

    def read_ball_annotation_frame(self, session_id: str, frame_index: int) -> tuple[bytes, str, str]:
        return self._ball_annotation_service().read_frame(session_id, frame_index)

    def put_ball_annotation(
        self,
        session_id: str,
        frame_index: int,
        request: dict[str, Any],
        *,
        if_match: str | None,
    ) -> dict[str, Any]:
        return self._ball_annotation_service().put_annotation(
            session_id,
            frame_index,
            request,
            if_match=if_match,
        )

    def create_ball_propagation_job(
        self,
        session_id: str,
        request: dict[str, Any],
        *,
        if_match: str | None,
    ) -> dict[str, Any]:
        return self._ball_annotation_service().create_propagation_job(session_id, request, if_match=if_match)

    def get_ball_propagation_job(self, session_id: str, job_id: str) -> dict[str, Any]:
        return self._ball_annotation_service().get_propagation_job(session_id, job_id)

    def cancel_ball_propagation_job(self, session_id: str, job_id: str) -> dict[str, Any]:
        return self._ball_annotation_service().cancel_propagation_job(session_id, job_id)

    def finalize_ball_annotation_session(self, session_id: str, mutation_id: str) -> dict[str, Any]:
        return self._ball_annotation_service().finalize_session(session_id, mutation_id)

    def get_ball_annotation_result(self, session_id: str) -> dict[str, Any]:
        return self._ball_annotation_service().get_final_result(session_id)

    def cancel_detector_probe(self, job_id: str) -> dict[str, Any]:
        return self._detector_development_service().cancel_probe(job_id)

    def read_detector_probe_artifact(self, job_id: str, artifact_id: str) -> tuple[bytes, str, str]:
        safe_job_id = require_safe_id(job_id, "detector probe job_id")
        return self._detector_development_service().read_probe_artifact(safe_job_id, artifact_id)

    def _detector_development_service(self) -> DetectorDevelopmentService:
        with self._detector_development_lock:
            if self._closing:
                raise DetectorDevelopmentError(
                    "service_closed", "Detector development service is closed", status_code=503
                )
            if self._detector_development is None:
                self._detector_development = DetectorDevelopmentService(self.repo_root)
            return self._detector_development

    def _ball_annotation_service(self) -> BallAnnotationService:
        with self._ball_annotation_lock:
            if self._closing:
                raise DetectorDevelopmentError("service_closed", "Ball annotation service is closed", status_code=503)
            if self._ball_annotation is None:
                self._ball_annotation = BallAnnotationService(
                    self.repo_root,
                    get_probe=self._get_verified_detector_probe,
                    create_probe=self._create_annotation_check_probe,
                    create_propagation_probe=self._create_annotation_propagation_probe,
                    cancel_propagation_probe=self.cancel_detector_probe,
                    read_probe_artifact=self.read_detector_probe_artifact,
                )
            return self._ball_annotation

    def _detector_probe_source_path(self, parent: dict[str, Any]) -> Path:
        source = parent.get("input_video")
        if not isinstance(source, str) or not source:
            raise DetectorDevelopmentError(
                "parent_source_unavailable",
                "The parent production trial source is unavailable",
                status_code=409,
            )
        try:
            return self._resolve_safe_descendant(
                self.data_dir,
                Path(source),
                expected_kind="file",
            )
        except (OSError, RuntimeError) as exc:
            raise DetectorDevelopmentError(
                "unsafe_parent_source",
                "The parent production trial source is outside the trusted data root",
                status_code=409,
            ) from exc

    def _detector_probe_output_dir(self, parent: dict[str, Any]) -> Path:
        output = parent.get("output_dir")
        if not isinstance(output, str) or not output:
            raise DetectorDevelopmentError(
                "parent_output_unavailable",
                "The parent production trial output is unavailable",
                status_code=409,
            )
        try:
            return self._resolve_safe_run_output(Path(output))
        except (OSError, RuntimeError) as exc:
            raise DetectorDevelopmentError(
                "unsafe_parent_output",
                "The parent production trial output is outside the trusted output root",
                status_code=409,
            ) from exc

    def _detector_probe_contract_path(self, output_dir: Path) -> Path:
        try:
            return self._resolve_safe_descendant(
                output_dir,
                output_dir / TRACKING_CONTRACT_REPORT_NAME,
                expected_kind="file",
                direct=True,
            )
        except (OSError, RuntimeError) as exc:
            raise DetectorDevelopmentError(
                "parent_tracking_contract_unavailable",
                "The parent production trial has no direct tracking contract",
                status_code=409,
            ) from exc

    def _detector_probe_config_snapshot(
        self,
        parent: dict[str, Any],
        note: dict[str, Any],
        source_path: Path,
    ) -> _YamlConfigSnapshot:
        config_path_raw = parent.get("config_path")
        config_sha256 = parent.get("config_sha256")
        if not isinstance(config_path_raw, str) or not config_path_raw:
            raise DetectorDevelopmentError(
                "parent_config_unavailable",
                "The parent production trial configuration is unavailable",
                status_code=409,
            )
        expected_sha256 = require_sha256(config_sha256, "parent config_sha256")
        try:
            config_path = self._resolve_safe_descendant(
                self.config_dir,
                Path(config_path_raw),
                expected_kind="file",
            )
            snapshot = self._capture_yaml_config_snapshot(config_path)
        except (OSError, RuntimeError, ValueError) as exc:
            raise DetectorDevelopmentError(
                "unsafe_parent_config",
                "The parent production trial configuration is outside the trusted config root",
                status_code=409,
            ) from exc
        if snapshot.sha256 != expected_sha256:
            raise DetectorDevelopmentError(
                "parent_config_digest_mismatch",
                "The parent production trial configuration digest does not match",
                status_code=409,
            )
        workflow = _value_at_dotted_path(snapshot.raw, "metadata.production_workflow")
        if not isinstance(workflow, dict):
            raise DetectorDevelopmentError(
                "parent_config_lineage_mismatch",
                "The parent configuration has no production workflow lineage",
                status_code=409,
            )
        lineage_fields = (
            "schema_version",
            "purpose",
            "workflow_id",
            "submission_id",
            "output_id",
            "generation",
            "start_frame",
            "max_frames",
            "enable_postprocess",
            "enable_follow_cam",
            "calibration_digest",
            "intent_sha256",
        )
        if any(workflow.get(key) != note.get(key) for key in lineage_fields):
            raise DetectorDevelopmentError(
                "parent_config_lineage_mismatch",
                "The parent configuration lineage does not match its run note",
                status_code=409,
            )
        configured_source = snapshot.raw.get("input_video")
        if not isinstance(configured_source, str) or Path(configured_source).resolve() != source_path:
            raise DetectorDevelopmentError(
                "parent_config_source_mismatch",
                "The parent configuration is bound to another source",
                status_code=409,
            )
        return snapshot

    def _detector_probe_base_config_binding(self, config: dict[str, Any]) -> tuple[str, Path]:
        lineage = _value_at_dotted_path(config, "metadata.production_workflow.base_config_lineage")
        if (
            not isinstance(lineage, dict)
            or set(lineage) != {"name", "sha256"}
            or not isinstance(lineage.get("name"), str)
            or not lineage["name"]
        ):
            raise DetectorDevelopmentError(
                "parent_base_config_lineage_missing",
                "The parent configuration has no immutable base-config lineage",
                status_code=409,
            )
        expected_sha256 = require_sha256(lineage.get("sha256"), "base config sha256")
        try:
            base_path, _ = self._resolve_config_path(lineage["name"])
            safe_base_path = self._resolve_safe_descendant(
                self.config_dir,
                base_path,
                expected_kind="file",
            )
            base_snapshot = self._capture_yaml_config_snapshot(safe_base_path)
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            raise DetectorDevelopmentError(
                "parent_base_config_unavailable",
                "The parent base configuration is unavailable",
                status_code=409,
            ) from exc
        if base_snapshot.sha256 != expected_sha256:
            raise DetectorDevelopmentError(
                "parent_base_config_digest_mismatch",
                "The parent base configuration digest does not match its lineage",
                status_code=409,
            )
        return expected_sha256, safe_base_path

    @staticmethod
    def _detector_probe_tuning_binding(
        config: dict[str, Any],
        *,
        base_config: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        tuning = _value_at_dotted_path(config, "metadata.production_tuning")
        if tuning is _MISSING_VALUE:
            binding = {
                "state": "absent",
                "schema_version": "1.0",
                "version_id": None,
                "parent_version_id": None,
                "values_sha256": canonical_sha256({}),
            }
            return binding, canonical_sha256(binding)
        if not isinstance(tuning, dict):
            raise DetectorDevelopmentError(
                "invalid_parent_tuning_lineage",
                "The parent tuning lineage is invalid",
                status_code=409,
            )
        try:
            normalized = normalize_production_trial_config_patch(
                {"metadata": {"production_tuning": deepcopy(tuning)}},
                base_config=base_config,
            )
        except ValueError as exc:
            raise DetectorDevelopmentError(
                "invalid_parent_tuning_lineage",
                "The parent tuning lineage failed canonical validation",
                status_code=409,
            ) from exc
        normalized_tuning = _value_at_dotted_path(normalized, "metadata.production_tuning")
        if normalized_tuning != tuning:
            raise DetectorDevelopmentError(
                "invalid_parent_tuning_lineage",
                "The parent tuning lineage is not canonical",
                status_code=409,
            )
        version_id = tuning.get("version_id")
        parent_version_id = tuning.get("parent_version_id")
        if (
            not isinstance(version_id, str)
            or not version_id.strip()
            or len(version_id) > 120
            or not (
                parent_version_id is None
                or (
                    isinstance(parent_version_id, str)
                    and bool(parent_version_id.strip())
                    and len(parent_version_id) <= 120
                )
            )
        ):
            raise DetectorDevelopmentError(
                "invalid_parent_tuning_lineage",
                "The parent tuning version identity is invalid",
                status_code=409,
            )
        values_sha256 = require_sha256(tuning.get("values_sha256"), "production tuning values_sha256")
        binding = {
            "state": "versioned",
            "schema_version": "1.0",
            "version_id": version_id,
            "parent_version_id": parent_version_id,
            "values_sha256": values_sha256,
        }
        return binding, canonical_sha256(binding)

    @staticmethod
    def _detector_probe_source_signature(config: dict[str, Any]) -> dict[str, Any]:
        signature = _value_at_dotted_path(config, "metadata.production_workflow.source_signature")
        if (
            not isinstance(signature, dict)
            or not isinstance(signature.get("path"), str)
            or not signature["path"]
            or isinstance(signature.get("size_bytes"), bool)
            or not isinstance(signature.get("size_bytes"), int)
            or signature["size_bytes"] <= 0
            or not isinstance(signature.get("modified_at"), str)
            or not signature["modified_at"]
        ):
            raise DetectorDevelopmentError(
                "parent_source_signature_missing",
                "The parent configuration source signature is incomplete",
                status_code=409,
            )
        return signature

    @staticmethod
    def _default_detector_probe_frames(authoritative_frames: list[int]) -> list[int]:
        if len(authoritative_frames) <= 6:
            return authoritative_frames
        final = len(authoritative_frames) - 1
        return sorted({authoritative_frames[round(index * final / 5)] for index in range(6)})

    def list_configs(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for config_path in sorted(self.config_dir.rglob("*.yaml")):
            if config_path.is_file():
                relative_name = config_path.relative_to(self.config_dir).as_posix()
                items.append(self._build_config_summary(config_path, relative_name))
        return items

    def list_input_videos(self) -> dict[str, Any]:
        supported_suffixes = {".mp4", ".mov", ".mkv", ".avi", ".m4v"}
        videos: list[dict[str, Any]] = []
        if self.data_dir.exists():
            for video_path in sorted(self.data_dir.rglob("*"), key=lambda item: item.name.lower()):
                if not video_path.is_file():
                    continue
                if video_path.suffix.lower() not in supported_suffixes:
                    continue
                stat = video_path.stat()
                videos.append(
                    {
                        "name": video_path.relative_to(self.data_dir).as_posix(),
                        "path": str(video_path.resolve()),
                        "size_bytes": stat.st_size,
                        "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    }
                )
        return {
            "root_dir": str(self.data_dir.resolve()),
            "videos": videos,
        }

    def delete_input_video(self, name: str) -> dict[str, Any]:
        video_path = self._resolve_input_video_name(name)
        with self._lock:
            self._assert_path_not_used_by_active_run_locked(input_video=video_path)
            with self._ready_broadcast_delivery_lock:
                self._invalidate_ready_broadcast_delivery_cache(dependency_path=video_path)
                video_path.unlink()
        return {
            "name": name,
            "path": str(video_path),
            "deleted": True,
        }

    def capture_field_preview(self, input_video: str, sample_index: int | None = None) -> dict[str, Any]:
        video_path = self._resolve_input_video_path(input_video)
        preview_sample = self._pick_field_preview_sample(video_path, sample_index=sample_index)
        return self._build_field_preview_response(video_path, preview_sample)

    def suggest_field_setup(
        self,
        input_video: str,
        config_name: str | None = None,
        frame_index: int | None = None,
    ) -> dict[str, Any]:
        video_path = self._resolve_input_video_path(input_video)
        samples = (
            [self._read_video_frame(video_path, frame_index)]
            if frame_index is not None
            else self._sample_video_frames(video_path)
        )
        if not samples:
            raise RuntimeError(f"Unable to read preview frames from input video: {video_path}")

        best_sample: dict[str, Any] | None = None
        config_shape: dict[str, Any] | None = None

        if config_name:
            config_shape = self._load_field_setup_from_config(
                config_name=config_name,
                frame_width=samples[len(samples) // 2]["frame_width"],
                frame_height=samples[len(samples) // 2]["frame_height"],
            )
            if config_shape is not None:
                sample = samples[len(samples) // 2]
                preview_bounds = self._build_preview_bounds(
                    expanded_polygon=config_shape["expanded_polygon"],
                    content_bounds=self._detect_content_bounds(sample["frame"]),
                    frame_width=sample["frame_width"],
                    frame_height=sample["frame_height"],
                )
                best_sample = {
                    **sample,
                    **config_shape,
                    "coverage": 1.0,
                    "confidence": "config",
                    "source": f"config:{config_name}",
                    "preview_bounds": preview_bounds,
                }

        if best_sample is None:
            for sample in samples:
                content_bounds = self._detect_content_bounds(sample["frame"])
                field_polygon, coverage, detected = self._detect_field_polygon(sample["frame"], content_bounds)
                expanded_polygon = self._expand_polygon(
                    field_polygon,
                    frame_width=sample["frame_width"],
                    frame_height=sample["frame_height"],
                    scale_x=1.08,
                    scale_y=1.10,
                )
                candidate = {
                    **sample,
                    "field_polygon": field_polygon,
                    "expanded_polygon": expanded_polygon,
                    "field_roi": self._polygon_bounds(field_polygon),
                    "expanded_roi": self._polygon_bounds(expanded_polygon),
                    "coverage": round(coverage, 4),
                    "confidence": "detected" if detected else "fallback",
                    "source": "field-green-heuristic" if detected else "safe-trapezoid-fallback",
                    "preview_bounds": self._build_preview_bounds(
                        expanded_polygon=expanded_polygon,
                        content_bounds=content_bounds,
                        frame_width=sample["frame_width"],
                        frame_height=sample["frame_height"],
                    ),
                }
                if best_sample is None:
                    best_sample = candidate
                    continue
                if candidate["confidence"] == "detected" and best_sample["confidence"] != "detected":
                    best_sample = candidate
                    continue
                if candidate["coverage"] > best_sample["coverage"]:
                    best_sample = candidate

        if best_sample is None:
            raise RuntimeError(f"Unable to build a field suggestion for input video: {video_path}")
        calibration = build_pitch_calibration_from_field_polygon(
            best_sample["field_polygon"],
            confidence=best_sample["confidence"],
            source=best_sample["source"],
        )
        return {
            "input_video": str(video_path),
            "preview_data_url": self._encode_frame_data_url(self._prepare_preview_frame(best_sample["frame"])),
            "preview_bounds": (0, 0, best_sample["frame_width"], best_sample["frame_height"]),
            "frame_width": best_sample["frame_width"],
            "frame_height": best_sample["frame_height"],
            "frame_index": best_sample["frame_index"],
            "frame_time_seconds": round(best_sample["frame_time_seconds"], 2),
            "sample_index": best_sample["sample_index"],
            "sample_count": best_sample["sample_count"],
            "field_polygon": best_sample["field_polygon"],
            "expanded_polygon": best_sample["expanded_polygon"],
            "field_roi": best_sample["field_roi"],
            "expanded_roi": best_sample["expanded_roi"],
            "confidence": best_sample["confidence"],
            "source": best_sample["source"],
            "field_coverage": best_sample["coverage"],
            "calibration": calibration,
            "config_patch": self._build_field_config_patch(
                field_polygon=best_sample["field_polygon"],
                expanded_polygon=best_sample["expanded_polygon"],
                expanded_roi=best_sample["expanded_roi"],
            ),
        }

    def check_input_quality(self, input_video: str, config_name: str | None = None) -> dict[str, Any]:
        video_path = self._resolve_input_video_path(input_video)
        samples = self._sample_video_frames(video_path)
        if not samples:
            raise RuntimeError(f"Unable to read quality-check frames from input video: {video_path}")

        middle_sample = samples[len(samples) // 2]
        config_shape: dict[str, Any] | None = None
        if config_name:
            config_shape = self._load_field_setup_from_config(
                config_name=config_name,
                frame_width=middle_sample["frame_width"],
                frame_height=middle_sample["frame_height"],
            )

        best_field_polygon: list[tuple[int, int]] | None = None
        best_field_confidence = "fallback"
        best_field_source = "safe-trapezoid-fallback"
        best_coverage = -1.0
        field_coverages: list[float] = []

        if config_shape is not None:
            best_field_polygon = config_shape["field_polygon"]
            best_field_confidence = "config"
            best_field_source = f"config:{config_name}"
            configured_coverage = self._field_polygon_coverage(
                config_shape["field_polygon"],
                frame_width=middle_sample["frame_width"],
                frame_height=middle_sample["frame_height"],
            )

        for sample in samples:
            content_bounds = self._detect_content_bounds(sample["frame"])
            field_polygon, coverage, detected = self._detect_field_polygon(sample["frame"], content_bounds)
            if config_shape is not None:
                field_coverages.append(round(configured_coverage, 4))
            else:
                field_coverages.append(round(coverage if detected else 0.0, 4))
            if config_shape is not None or coverage <= best_coverage:
                continue
            best_field_polygon = field_polygon
            best_field_confidence = "detected" if detected else "fallback"
            best_field_source = "field-green-heuristic" if detected else "safe-trapezoid-fallback"
            best_coverage = coverage

        calibration = build_pitch_calibration_from_field_polygon(
            best_field_polygon,
            confidence=best_field_confidence,
            source=best_field_source,
        )
        calibration_confidence = calibration["confidence"] if calibration else None
        return assess_video_quality(
            input_video=str(video_path),
            samples=samples,
            field_coverages=field_coverages,
            calibration_confidence=calibration_confidence,
        )

    def _field_polygon_coverage(
        self,
        field_polygon: list[tuple[int, int]],
        *,
        frame_width: int,
        frame_height: int,
    ) -> float:
        if not field_polygon:
            return 0.0
        frame_area = max(1.0, float(frame_width * frame_height))
        polygon = np.asarray(field_polygon, dtype=np.float32)
        area = abs(float(cv2.contourArea(polygon)))
        return max(0.0, min(1.0, area / frame_area))

    def _build_field_preview_response(self, video_path: Path, sample: dict[str, Any]) -> dict[str, Any]:
        return {
            "input_video": str(video_path),
            "preview_data_url": self._encode_frame_data_url(self._prepare_preview_frame(sample["frame"])),
            "frame_width": sample["frame_width"],
            "frame_height": sample["frame_height"],
            "frame_index": sample["frame_index"],
            "frame_time_seconds": round(sample["frame_time_seconds"], 2),
            "sample_index": sample["sample_index"],
            "sample_count": sample["sample_count"],
        }

    def get_config(self, name: str) -> dict[str, Any]:
        config_path, relative_name = self._resolve_config_path(name)
        text = config_path.read_text(encoding="utf-8")
        raw = self._load_raw_yaml(config_path)
        resolved = load_config(config_path)
        return {
            "name": relative_name,
            "path": str(config_path),
            "text": text,
            "raw": raw,
            "resolved": _jsonable(resolved),
            "summary": self._build_config_summary(config_path, relative_name),
        }

    def update_config(self, name: str, content: str) -> dict[str, Any]:
        config_path, relative_name = self._resolve_config_path(name)
        loaded = yaml.safe_load(content) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"Invalid config root in {relative_name}")

        tmp_path = config_path.with_name(f".{config_path.name}.{uuid4().hex[:8]}.tmp.yaml")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            load_config(tmp_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

        with self._lock:
            self._assert_path_not_used_by_active_run_locked(config_path=config_path)

        config_path.write_text(content, encoding="utf-8")
        return self.get_config(relative_name)

    def delete_config(self, name: str) -> dict[str, Any]:
        config_path, relative_name = self._resolve_config_path(name)
        with self._lock:
            self._assert_path_not_used_by_active_run_locked(config_path=config_path)
        config_path.unlink()
        return {
            "name": relative_name,
            "path": str(config_path),
            "deleted": True,
        }

    def delete_run_output(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            registry = self._read_registry()
            self._refresh_discovered_runs_locked(registry)
            self._normalize_registry_runs_locked(registry)
            run = next((item for item in registry["runs"] if item.get("run_id") == run_id), None)
            if run is None:
                raise KeyError(run_id)
            if run.get("status") in {"queued", "running"}:
                raise RuntimeError(f"Run is still active and cannot be deleted: {run_id}")
            child = next((item for item in registry["runs"] if item.get("parent_run_id") == run_id), None)
            if child is not None:
                raise RuntimeError(f"Run owns child operation output and cannot be deleted first: {child['run_id']}")
            raw_output_dir = Path(run["output_dir"])
            output_dir = self._resolve_safe_run_output(raw_output_dir, allow_missing=True)
            with self._ready_broadcast_delivery_lock:
                self._invalidate_ready_broadcast_delivery_cache(output_dir=output_dir)
                if output_dir.exists():
                    output_dir = self._resolve_safe_run_output(raw_output_dir)
                    shutil.rmtree(output_dir)
                    parent_dir = output_dir.parent
                    if parent_dir != self.outputs_dir.resolve() and parent_dir.exists():
                        try:
                            safe_parent = self._resolve_safe_descendant(
                                self.outputs_dir,
                                parent_dir,
                                expected_kind="directory",
                            )
                        except RuntimeError:
                            safe_parent = None
                        if safe_parent is not None and not any(safe_parent.iterdir()):
                            safe_parent.rmdir()
                registry["runs"] = [item for item in registry["runs"] if item.get("run_id") != run_id]
                self._write_registry(registry)
        return {
            "name": run_id,
            "path": str(output_dir),
            "deleted": True,
        }

    def derive_config(self, base_config_name: str, output_name: str, patch: dict[str, Any]) -> dict[str, Any]:
        base_path, _ = self._resolve_config_path(base_config_name)
        base_raw = self._load_raw_yaml(base_path)
        merged = _deep_merge(base_raw, patch)
        metadata = merged.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["created_at"] = _utc_now_iso()
        merged["metadata"] = metadata
        output_stem = Path(output_name).name
        output_file_name = output_stem if output_stem.endswith(".yaml") else f"{output_stem}.yaml"
        self.generated_config_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.generated_config_dir / output_file_name
        with output_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(merged, handle, sort_keys=False, allow_unicode=False)
        return self.get_config(output_path.relative_to(self.config_dir).as_posix())

    def list_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            registry = self._read_registry()
            original = deepcopy(registry)
            self._refresh_discovered_runs_locked(registry)
            registry["runs"] = [run for run in registry["runs"] if self._registry_run_has_safe_output(run)]
            self._normalize_registry_runs_locked(registry)
            if registry != original:
                try:
                    self._write_registry(registry)
                except RuntimeError:
                    # A read endpoint must not invalidate or fail an in-flight
                    # cross-process state transition. The next read will refresh
                    # any filesystem discoveries that lost this benign race.
                    registry = self._read_registry()
                    registry["runs"] = [run for run in registry["runs"] if self._registry_run_has_safe_output(run)]
                    self._normalize_registry_runs_locked(registry)
            runs = sorted(
                registry["runs"],
                key=lambda item: self._timestamp_value(self._run_activity_at(item)),
                reverse=True,
            )
        for run in runs:
            self._attach_trial_signal_gate(run)
            if not self._is_ready_broadcast_run(run):
                continue
            # The dedicated artifacts endpoint seals and validates ready delivery.
            # Metadata-only run polling must stay bounded and cannot authorize stale paths.
            run["artifacts"] = []
        return runs

    def list_asset_groups(self) -> list[dict[str, Any]]:
        inputs_payload = self.list_input_videos()
        videos = inputs_payload["videos"]
        configs = self.list_configs()
        runs = self.list_runs()

        input_index = {video["path"]: video for video in videos}
        groups: dict[str, dict[str, Any]] = {}

        def ensure_group(input_path: str | None) -> dict[str, Any]:
            if input_path and input_path in input_index:
                video = input_index[input_path]
                existing = groups.get(input_path)
                if existing is not None:
                    return existing
                created = {
                    "group_id": self._slugify(video["name"]) or "input-group",
                    "title": video["name"],
                    "input_video": video,
                    "last_activity_at": video.get("modified_at"),
                    "run_count": 0,
                    "config_count": 0,
                    "output_count": 0,
                    "runs": [],
                    "configs": [],
                    "outputs": [],
                    "is_unbound": False,
                }
                groups[input_path] = created
                return created

            existing = groups.get("__unbound__")
            if existing is not None:
                return existing
            created = {
                "group_id": "unbound-legacy",
                "title": "Unbound / Legacy",
                "input_video": None,
                "last_activity_at": None,
                "run_count": 0,
                "config_count": 0,
                "output_count": 0,
                "runs": [],
                "configs": [],
                "outputs": [],
                "is_unbound": True,
            }
            groups["__unbound__"] = created
            return created

        for video in videos:
            ensure_group(video["path"])

        for config in configs:
            ensure_group(config.get("input_video")).get("configs", []).append(config)

        for run in runs:
            group = ensure_group(run.get("input_video"))
            group.get("runs", []).append(run)
            if run.get("output_dir"):
                group.get("outputs", []).append(run)

        prepared_groups: list[dict[str, Any]] = []
        for group in groups.values():
            group["configs"] = sorted(
                group["configs"], key=lambda item: self._timestamp_value(item.get("created_at")), reverse=True
            )
            group["runs"] = sorted(
                group["runs"], key=lambda item: self._timestamp_value(self._run_activity_at(item)), reverse=True
            )
            group["outputs"] = sorted(
                group["outputs"], key=lambda item: self._timestamp_value(self._run_activity_at(item)), reverse=True
            )
            group["run_count"] = len(group["runs"])
            group["config_count"] = len(group["configs"])
            group["output_count"] = len(group["outputs"])

            activity_candidates: list[str] = []
            if group.get("input_video"):
                input_modified_at = group["input_video"].get("modified_at")
                if isinstance(input_modified_at, str):
                    activity_candidates.append(input_modified_at)
            activity_candidates.extend(
                value for value in (item.get("created_at") for item in group["configs"]) if isinstance(value, str)
            )
            activity_candidates.extend(
                value for value in (self._run_activity_at(item) for item in group["runs"]) if isinstance(value, str)
            )
            normalized_candidates = [
                candidate for candidate in (_normalize_iso_timestamp(item) for item in activity_candidates) if candidate
            ]
            group["last_activity_at"] = max(normalized_candidates, default=None)

            if group["is_unbound"] and not (group["runs"] or group["configs"] or group["outputs"]):
                continue
            prepared_groups.append(group)

        prepared_groups.sort(
            key=lambda item: (
                1 if item.get("is_unbound") else 0,
                -self._timestamp_value(item.get("last_activity_at")),
            )
        )
        return prepared_groups

    def get_run(self, run_id: str) -> dict[str, Any]:
        snapshot: dict[str, Any] | None = None
        with self._lock:
            registry = self._read_registry()
            for run in registry["runs"]:
                if run["run_id"] == run_id:
                    if not self._registry_run_has_safe_output(run):
                        raise KeyError(run_id)
                    snapshot = deepcopy(run)
                    break
            if snapshot is None:
                original = deepcopy(registry)
                self._refresh_discovered_runs_locked(registry)
                if registry != original:
                    try:
                        self._write_registry(registry)
                    except RuntimeError:
                        registry = self._read_registry()
                for run in registry["runs"]:
                    if run["run_id"] == run_id:
                        if not self._registry_run_has_safe_output(run):
                            raise KeyError(run_id)
                        snapshot = deepcopy(run)
                        break
        if snapshot is None:
            raise KeyError(run_id)

        output_dir = (
            self._resolve_safe_run_output(Path(snapshot["output_dir"]), allow_missing=True)
            if snapshot.get("output_dir")
            else None
        )
        if output_dir is not None and output_dir.is_dir():
            if self._is_ready_broadcast_run(snapshot):
                snapshot["artifacts"] = []
            else:
                snapshot["artifacts"] = self._collect_artifacts(output_dir)
            snapshot["stats"] = self._collect_stats(output_dir)
            self._attach_trial_signal_gate(snapshot)
            self._attach_ai_candidate_lifecycle(snapshot)
        return snapshot

    def get_trial_diagnosis(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        note = self._machine_run_note(run.get("notes"))
        if not isinstance(note, dict) or note.get("purpose") != "production_trial":
            raise ValueError("Trial diagnosis is available only for production_trial runs")
        output_dir_raw = run.get("output_dir")
        if not isinstance(output_dir_raw, str) or not output_dir_raw:
            raise FileNotFoundError(f"Run output is unavailable: {run_id}")
        output_dir = self._resolve_safe_run_output(Path(output_dir_raw))
        metrics_report = self._read_safe_direct_json(output_dir, "metrics_report.json")
        return build_trial_diagnosis(output_dir, run, metrics_report=metrics_report)

    def get_trial_tuning_schema(self) -> dict[str, Any]:
        schema = trial_tuning_schema()
        controls = schema.get("controls")
        if not isinstance(controls, list):
            raise RuntimeError("Production trial tuning schema is unavailable")
        for control in controls:
            if not isinstance(control, dict) or not isinstance(control.get("path"), str):
                raise RuntimeError("Production trial tuning schema is invalid")
            path = control["path"]
            control["description"] = _describe_config_path(path, None, "en")
            control["description_zh"] = _describe_config_path(path, None, "zh")
        return schema

    def list_artifacts(
        self,
        run_id: str,
        *,
        expected_status_generation: str | None = None,
    ) -> list[dict[str, Any]]:
        run = self.get_run(run_id)
        if self._is_ready_broadcast_run(run):
            status_generation = self._ready_broadcast_expected_status_generation(
                run,
                expected_status_generation=expected_status_generation,
            )
            try:
                output_dir = self._resolve_safe_run_output(Path(run["output_dir"]))
            except (KeyError, RuntimeError):
                return []
            return self._ready_broadcast_artifact_summaries(
                output_dir,
                expected_status_generation=status_generation,
            )
        return run.get("artifacts", [])

    def get_artifact_path(
        self,
        run_id: str,
        artifact_name: str,
        *,
        expected_status_generation: str | None = None,
    ) -> Path:
        run = self.get_run(run_id)
        if self._is_ready_broadcast_run(run):
            status_generation = self._ready_broadcast_expected_status_generation(
                run,
                expected_status_generation=expected_status_generation,
            )
            relative = Path(artifact_name)
            normalized_name = relative.as_posix()
            if (
                relative.is_absolute()
                or normalized_name != artifact_name.replace("\\", "/")
                or ".." in relative.parts
                or normalized_name not in _READY_BROADCAST_DELIVERY_NAMES
            ):
                raise FileNotFoundError(artifact_name)
            try:
                output_dir = self._resolve_safe_run_output(Path(run["output_dir"]))
            except (KeyError, RuntimeError) as exc:
                raise FileNotFoundError(artifact_name) from exc
            snapshots = self._ready_broadcast_delivery_snapshots(
                output_dir,
                expected_status_generation=status_generation,
            )
            if snapshots is None or normalized_name not in snapshots:
                raise FileNotFoundError(artifact_name)
            return snapshots[normalized_name]
        try:
            output_dir = self._resolve_safe_run_output(Path(run["output_dir"]))
            candidate = self._resolve_safe_descendant(
                output_dir,
                output_dir / artifact_name,
                expected_kind="file",
            )
        except RuntimeError as exc:
            raise FileNotFoundError(artifact_name) from exc
        allowed = set(self._iter_artifact_paths(output_dir))
        if candidate not in allowed:
            raise FileNotFoundError(artifact_name)
        return candidate

    def acquire_artifact_response_lease(
        self,
        run_id: str,
        artifact_name: str,
        *,
        expected_status_generation: str | None = None,
    ) -> _ArtifactResponseLease:
        run = self.get_run(run_id)
        if not self._is_ready_broadcast_run(run):
            lease = self._acquire_ready_broadcast_file_lease(self.get_artifact_path(run_id, artifact_name))
            return _ArtifactResponseLease(lease.path, lease.handle, lease.stat_token)

        status_generation = self._ready_broadcast_expected_status_generation(
            run,
            expected_status_generation=expected_status_generation,
        )
        relative = Path(artifact_name)
        normalized_name = relative.as_posix()
        if (
            status_generation is None
            or relative.is_absolute()
            or normalized_name != artifact_name.replace("\\", "/")
            or ".." in relative.parts
            or normalized_name not in _READY_BROADCAST_DELIVERY_NAMES
        ):
            raise FileNotFoundError(artifact_name)
        try:
            output_dir = self._resolve_safe_run_output(Path(run["output_dir"]))
        except (KeyError, RuntimeError) as exc:
            raise FileNotFoundError(artifact_name) from exc
        with self._ready_broadcast_delivery_lock:
            snapshots = self._ready_broadcast_delivery_snapshots(
                output_dir,
                expected_status_generation=status_generation,
            )
            entry = self._ready_broadcast_delivery_cache.get((output_dir, status_generation))
            if snapshots is None or entry is None:
                raise FileNotFoundError(artifact_name)
            path = snapshots.get(normalized_name)
            expected_token = entry.get("snapshot_tokens", {}).get(normalized_name)
            if path is None or not isinstance(expected_token, tuple):
                raise FileNotFoundError(artifact_name)
            try:
                lease = self._acquire_ready_broadcast_file_lease(path)
            except (OSError, RuntimeError) as exc:
                raise FileNotFoundError(artifact_name) from exc
            if lease.stat_token != expected_token:
                self._release_ready_broadcast_file_leases((lease,))
                raise FileNotFoundError(artifact_name)
            self._ready_broadcast_active_responses += 1
            return _ArtifactResponseLease(
                lease.path,
                lease.handle,
                lease.stat_token,
                on_close=self._ready_broadcast_response_released,
            )

    @staticmethod
    def _is_ready_broadcast_run(run: dict[str, Any]) -> bool:
        broadcast = run.get("broadcast")
        return (
            run.get("source") == "broadcast_hybrid"
            and isinstance(broadcast, dict)
            and broadcast.get("status") == "ready"
        )

    @staticmethod
    def _ready_broadcast_status_generation(run: dict[str, Any]) -> str | None:
        broadcast = run.get("broadcast")
        if not isinstance(broadcast, dict):
            return None
        status_generation = broadcast.get("status_generation")
        return status_generation if isinstance(status_generation, str) else None

    @classmethod
    def _ready_broadcast_expected_status_generation(
        cls,
        run: dict[str, Any],
        *,
        expected_status_generation: str | None,
    ) -> str:
        authoritative = cls._ready_broadcast_status_generation(run)
        if expected_status_generation is None:
            raise ArtifactStatusGenerationConflict("Ready broadcast artifact access requires status_generation")
        if expected_status_generation != authoritative:
            raise ArtifactStatusGenerationConflict(
                "Ready broadcast status generation conflict: "
                f"requested {expected_status_generation}, authoritative {authoritative or 'missing'}"
            )
        return expected_status_generation

    def _ready_broadcast_artifacts_are_valid(
        self,
        output_dir: Path,
        *,
        expected_status_generation: str | None = None,
    ) -> bool:
        return (
            self._ready_broadcast_delivery_snapshots(
                output_dir,
                expected_status_generation=expected_status_generation,
            )
            is not None
        )

    def _ready_broadcast_artifact_summaries(
        self,
        output_dir: Path,
        *,
        expected_status_generation: str | None,
    ) -> list[dict[str, Any]]:
        snapshots = self._ready_broadcast_delivery_snapshots(
            output_dir,
            expected_status_generation=expected_status_generation,
        )
        if snapshots is None:
            return []
        summaries: list[dict[str, Any]] = []
        for name in _READY_BROADCAST_DELIVERY_NAMES:
            path = snapshots.get(name)
            if path is None:
                return []
            try:
                size_bytes = path.stat().st_size
            except OSError:
                return []
            content_type, _ = mimetypes.guess_type(name)
            summaries.append(
                {
                    "name": name,
                    "path": str(path),
                    "kind": self._artifact_kind(Path(name)),
                    "exists": True,
                    "size_bytes": size_bytes,
                    "content_type": content_type,
                }
            )
        return summaries

    def _ready_broadcast_delivery_snapshots(
        self,
        output_dir: Path,
        *,
        expected_status_generation: str | None,
    ) -> dict[str, Path] | None:
        try:
            output_dir = self._resolve_safe_run_output(output_dir)
        except RuntimeError:
            return None
        if expected_status_generation is None or not re.fullmatch(r"[0-9a-f]{64}", expected_status_generation):
            return None

        with self._ready_broadcast_delivery_lock:
            candidate_keys = [
                key
                for key in self._ready_broadcast_delivery_cache
                if key[0] == output_dir and (expected_status_generation is None or key[1] == expected_status_generation)
            ]
            for key in candidate_keys:
                entry = self._ready_broadcast_delivery_cache.get(key)
                if entry is not None and self._ready_broadcast_delivery_entry_is_current(entry):
                    # Dict insertion order is the bounded cache's LRU order.
                    self._ready_broadcast_delivery_cache.pop(key, None)
                    self._ready_broadcast_delivery_cache[key] = entry
                    return dict(entry["artifacts"])
                if entry is not None:
                    self._ready_broadcast_delivery_cache.pop(key, None)
                    self._release_ready_broadcast_cache_entry(entry)

            try:
                (
                    status_generation,
                    artifacts,
                    dependencies,
                    dependency_tokens,
                    directory_tokens,
                    snapshot_tokens,
                    snapshot_manifest,
                ) = self._build_ready_broadcast_delivery_snapshot(
                    output_dir,
                    expected_status_generation=expected_status_generation,
                )
            except (BroadcastApiError, BroadcastHybridOrchestrationError, OSError, RuntimeError, TypeError, ValueError):
                return None

            # A new status generation supersedes older seals for the same run.
            # Keep two ready runs globally so interleaved Range requests do not
            # repeatedly validate and copy both videos. The small LRU bound keeps
            # open handles and duplicate video storage predictable.
            for stale_key in [key for key in self._ready_broadcast_delivery_cache if key[0] == output_dir]:
                old_entry = self._ready_broadcast_delivery_cache.pop(stale_key)
                self._release_ready_broadcast_cache_entry(old_entry)
            while len(self._ready_broadcast_delivery_cache) >= _MAX_READY_BROADCAST_DELIVERY_CACHE_ENTRIES:
                oldest_key = next(iter(self._ready_broadcast_delivery_cache))
                old_entry = self._ready_broadcast_delivery_cache.pop(oldest_key)
                self._release_ready_broadcast_cache_entry(old_entry)
            key = (output_dir, status_generation)
            partial_entry = {
                "dependencies": dependencies,
                "snapshot_leases": (),
                "snapshot_manifest": snapshot_manifest,
            }
            snapshot_lease_list: list[_ReadyBroadcastFileLease] = []
            snapshot_leases: tuple[_ReadyBroadcastFileLease, ...] = ()
            try:
                output_inventory = self._ready_broadcast_output_inventory(output_dir)
                snapshot_manifest_token = self._artifact_identity_token(snapshot_manifest)
                for path in (snapshot_manifest, *(artifacts[name] for name in _READY_BROADCAST_DELIVERY_NAMES)):
                    snapshot_lease_list.append(self._acquire_ready_broadcast_file_lease(path))
                snapshot_leases = tuple(snapshot_lease_list)
                expected_snapshot_tokens = {
                    snapshot_manifest: snapshot_manifest_token,
                    **{artifacts[name]: snapshot_tokens[name] for name in _READY_BROADCAST_DELIVERY_NAMES},
                }
                if any(lease.stat_token != expected_snapshot_tokens.get(lease.path) for lease in snapshot_leases):
                    raise RuntimeError("ready broadcast snapshot changed while acquiring its immutable lease")
            except (OSError, RuntimeError):
                partial_entry["snapshot_leases"] = tuple(snapshot_lease_list)
                self._release_ready_broadcast_cache_entry(partial_entry)
                return None
            entry = {
                "artifacts": artifacts,
                "dependencies": dependencies,
                "dependency_tokens": dependency_tokens,
                "directory_tokens": directory_tokens,
                "output_dir": output_dir,
                "output_inventory": output_inventory,
                "snapshot_tokens": snapshot_tokens,
                "snapshot_leases": snapshot_leases,
                "snapshot_manifest": snapshot_manifest,
                "snapshot_manifest_token": snapshot_manifest_token,
            }
            self._ready_broadcast_delivery_cache[key] = entry
            if not self._ready_broadcast_delivery_entry_is_current(entry):
                self._ready_broadcast_delivery_cache.pop(key, None)
                self._release_ready_broadcast_cache_entry(entry)
                return None
            return dict(artifacts)

    def _build_ready_broadcast_delivery_snapshot(
        self,
        output_dir: Path,
        *,
        expected_status_generation: str | None,
    ) -> tuple[
        str,
        dict[str, Path],
        tuple[_ReadyBroadcastFileLease, ...],
        dict[Path, _ReadyIdentityToken],
        dict[Path, _ReadyIdentityToken],
        dict[str, tuple[int, int, int, int, int]],
        Path,
    ]:
        dependency_paths = self._ready_broadcast_dependency_paths(output_dir)
        dependency_tokens = self._capture_ready_broadcast_path_tokens(dependency_paths)
        directory_tokens = self._ready_broadcast_directory_tokens(output_dir, dependency_paths)
        required_paths = {
            output_dir / name for name in (*_READY_BROADCAST_DELIVERY_NAMES, "broadcast_artifact_bindings.v1.json")
        }
        if not required_paths.issubset(set(dependency_paths)):
            raise RuntimeError("ready broadcast delivery contract is incomplete")

        dependencies: list[_ReadyBroadcastFileLease] = []
        staging: Path | None = None
        final_dir: Path | None = None
        published = False
        try:
            for path in sorted(required_paths, key=lambda item: str(item).casefold()):
                dependencies.append(self._acquire_ready_broadcast_file_lease(path))
            leases_by_path = {lease.path: lease for lease in dependencies}
            quality_path = output_dir / "broadcast_quality_report.json"
            manifest_path = output_dir / "broadcast_artifact_bindings.v1.json"
            quality_lease = leases_by_path[quality_path]
            manifest_lease = leases_by_path[manifest_path]

            validated_quality = validate_broadcast_quality_report(output_dir, quality_path)
            self._verify_ready_broadcast_file_leases(dependencies)
            self._verify_ready_broadcast_path_tokens(dependency_tokens)
            quality = self._read_ready_broadcast_lease_json(quality_lease, "broadcast quality report")
            manifest = self._read_ready_broadcast_lease_json(manifest_lease, "broadcast final artifact bindings")
            if validated_quality != quality or quality.get("status") != "ready":
                raise RuntimeError("ready broadcast quality report changed during validation")
            status_generation = quality.get("status_generation")
            if not isinstance(status_generation, str) or not re.fullmatch(r"[0-9a-f]{64}", status_generation):
                raise RuntimeError("ready broadcast status generation is invalid")
            if expected_status_generation is not None and status_generation != expected_status_generation:
                raise RuntimeError("ready broadcast status generation is stale")
            if manifest.get("artifact_type") != "broadcast_artifact_bindings":
                raise RuntimeError("ready broadcast final bindings are invalid")
            raw_bindings = manifest.get("artifacts")
            if not isinstance(raw_bindings, dict) or set(raw_bindings) != set(PUBLIC_ARTIFACTS):
                raise RuntimeError("ready broadcast final bindings are incomplete")

            temporary = self._ready_broadcast_delivery_temp
            if temporary is None:
                raise RuntimeError("ready broadcast delivery cache is closed")
            cache_root = temporary
            cache_key = hashlib.sha256(str(output_dir).encode("utf-8")).hexdigest()[:16]
            snapshot_id = uuid4().hex
            final_dir = cache_root / f"{cache_key}-{status_generation}-{snapshot_id}"
            staging = cache_root / f".{final_dir.name}.tmp"
            staging.mkdir()

            snapshot_records: dict[str, dict[str, Any]] = {}
            for name in _READY_BROADCAST_DELIVERY_NAMES:
                source_lease = leases_by_path[output_dir / name]
                expected_sha256: str | None = None
                expected_size: int | None = None
                if name in PUBLIC_ARTIFACTS:
                    binding = raw_bindings.get(name)
                    if not isinstance(binding, dict):
                        raise RuntimeError(f"ready broadcast binding is invalid: {name}")
                    expected_sha256 = binding.get("sha256")
                    expected_size = binding.get("size_bytes")
                    if (
                        not isinstance(expected_sha256, str)
                        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
                        or isinstance(expected_size, bool)
                        or not isinstance(expected_size, int)
                        or expected_size < 0
                    ):
                        raise RuntimeError(f"ready broadcast binding is invalid: {name}")
                digest, size_bytes = self._copy_ready_broadcast_lease(
                    source_lease,
                    staging / name,
                    expected_sha256=expected_sha256,
                    expected_size=expected_size,
                )
                content_type, _ = mimetypes.guess_type(name)
                snapshot_records[name] = {
                    "sha256": digest,
                    "size_bytes": size_bytes,
                    "content_type": content_type,
                }

            self._verify_ready_broadcast_file_leases(dependencies)
            self._verify_ready_broadcast_path_tokens(dependency_tokens)
            self._verify_ready_broadcast_directory_tokens(directory_tokens)
            if self._ready_broadcast_dependency_paths(output_dir) != dependency_paths:
                raise RuntimeError("ready broadcast dependency set changed during validation")
            snapshot_manifest = staging / "delivery_snapshot.v1.json"
            snapshot_payload = {
                "schema_version": "1.0",
                "artifact_type": "ready_broadcast_delivery_snapshot",
                "source_output_dir_sha256": hashlib.sha256(str(output_dir).encode("utf-8")).hexdigest(),
                "status_generation": status_generation,
                "quality_report_sha256": snapshot_records["broadcast_quality_report.json"]["sha256"],
                "final_bindings_sha256": self._hash_ready_broadcast_lease(manifest_lease),
                "artifacts": snapshot_records,
            }
            publish_json_exclusive(snapshot_manifest, snapshot_payload, trusted_root=staging)
            staging.replace(final_dir)
            published = True

            artifacts = {name: final_dir / name for name in _READY_BROADCAST_DELIVERY_NAMES}
            snapshot_tokens = {name: self._artifact_identity_token(path) for name, path in artifacts.items()}
            final_manifest = final_dir / snapshot_manifest.name
            return (
                status_generation,
                artifacts,
                tuple(dependencies),
                dependency_tokens,
                directory_tokens,
                snapshot_tokens,
                final_manifest,
            )
        except BaseException:
            self._release_ready_broadcast_file_leases(dependencies)
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
            if published and final_dir is not None:
                shutil.rmtree(final_dir, ignore_errors=True)
            raise

    def _ready_broadcast_dependency_paths(self, output_dir: Path) -> list[Path]:
        contained_paths, _ = self._ready_broadcast_output_tree(output_dir)
        paths = {
            path
            for path in contained_paths
            if len(path.relative_to(output_dir).parts) == 1
            or path.relative_to(output_dir).parts[0] in _READY_BROADCAST_STRONG_OUTPUT_ROOTS
        }

        queue_path = output_dir / "selective_review_queue.v1.json"
        if queue_path.is_file():
            queue, _ = validate_review_queue_bindings(queue_path)
            queue_bindings = queue.get("bindings")
            if not isinstance(queue_bindings, dict):
                raise RuntimeError("ready broadcast queue bindings are unavailable")
            bound_paths: dict[str, Path] = {}
            for binding_name, raw_binding in queue_bindings.items():
                if not isinstance(binding_name, str) or not isinstance(raw_binding, dict):
                    raise RuntimeError("ready broadcast queue binding is invalid")
                raw_binding_path = raw_binding.get("path")
                if not isinstance(raw_binding_path, str) or not raw_binding_path:
                    raise RuntimeError("ready broadcast queue binding is invalid")
                binding_path = Path(raw_binding_path)
                if not binding_path.is_absolute():
                    binding_path = queue_path.parent / binding_path
                binding_path = self._resolve_nonlink_ready_broadcast_file(
                    binding_path,
                    "ready broadcast queue-bound artifact",
                )
                bound_paths[binding_name] = binding_path
                paths.add(binding_path)
            dataset_path = bound_paths.get("dataset")
            if dataset_path is None:
                raise RuntimeError("ready broadcast dataset binding is unavailable")
            dataset_binding = queue_bindings.get("dataset")
            if not isinstance(dataset_binding, dict):
                raise RuntimeError("ready broadcast dataset binding is unavailable")
            raw_dataset_path = dataset_binding.get("path")
            if not isinstance(raw_dataset_path, str) or not raw_dataset_path:
                raise RuntimeError("ready broadcast dataset binding is unavailable")
            dataset, _ = load_bound_json(dataset_path, "ready broadcast candidate dataset")
            sources = dataset.get("sources")
            if not isinstance(sources, list) or len(sources) != 1 or not isinstance(sources[0], dict):
                raise RuntimeError("ready broadcast candidate dataset source is invalid")
            raw_source_path = sources[0].get("path")
            if not isinstance(raw_source_path, str) or not raw_source_path:
                raise RuntimeError("ready broadcast candidate dataset source is invalid")
            source_path = Path(raw_source_path)
            if not source_path.is_absolute():
                source_path = dataset_path.parent / source_path
            source_path = self._resolve_nonlink_ready_broadcast_file(
                source_path,
                "ready broadcast candidate dataset source",
            )
            paths.add(source_path)
            samples = dataset.get("samples")
            if isinstance(samples, list):
                for raw_sample in samples:
                    if not isinstance(raw_sample, dict):
                        raise RuntimeError("ready broadcast candidate dataset sample is invalid")
                    sample_artifacts = raw_sample.get("artifacts")
                    if not isinstance(sample_artifacts, dict):
                        continue
                    for raw_descriptor in sample_artifacts.values():
                        if not isinstance(raw_descriptor, dict):
                            raise RuntimeError("ready broadcast candidate evidence descriptor is invalid")
                        raw_evidence_path = raw_descriptor.get("path")
                        if not isinstance(raw_evidence_path, str) or not raw_evidence_path:
                            raise RuntimeError("ready broadcast candidate evidence path is invalid")
                        evidence_path = Path(raw_evidence_path)
                        if not evidence_path.is_absolute():
                            evidence_path = dataset_path.parent / evidence_path
                        evidence_path = self._resolve_nonlink_ready_broadcast_file(
                            evidence_path,
                            "ready broadcast candidate evidence",
                        )
                        paths.add(evidence_path)

        registry = self._read_registry()
        for run in registry.get("runs", []):
            if not isinstance(run, dict) or not isinstance(run.get("output_dir"), str):
                continue
            try:
                run_output = self._resolve_safe_run_output(Path(run["output_dir"]), allow_missing=True)
            except RuntimeError:
                continue
            if run_output != output_dir or not isinstance(run.get("input_video"), str):
                continue
            input_video = self._resolve_nonlink_ready_broadcast_file(
                Path(run["input_video"]),
                "ready broadcast source video",
            )
            paths.add(input_video)
            break

        if len(paths) > _MAX_READY_BROADCAST_DEPENDENCIES:
            raise RuntimeError("ready broadcast dependency set exceeds the safe lease bound")
        return sorted(paths, key=lambda item: str(item).casefold())

    def _ready_broadcast_output_tree(self, output_dir: Path) -> tuple[list[Path], tuple[str, ...]]:
        files: list[Path] = []
        inventory: list[str] = []
        stack = [output_dir]
        while stack:
            directory = stack.pop()
            for candidate in directory.iterdir():
                if self._is_link_or_reparse_point(candidate):
                    raise RuntimeError("ready broadcast output contains a link or reparse point")
                relative = candidate.relative_to(output_dir).as_posix()
                if candidate.is_dir():
                    inventory.append(f"{relative}/")
                    stack.append(candidate)
                elif candidate.is_file():
                    inventory.append(relative)
                    files.append(
                        self._resolve_safe_descendant(
                            output_dir,
                            candidate,
                            expected_kind="file",
                        )
                    )
                else:
                    raise RuntimeError("ready broadcast output contains a special file")
        return sorted(files, key=lambda item: str(item).casefold()), tuple(sorted(inventory))

    def _ready_broadcast_output_inventory(self, output_dir: Path) -> tuple[str, ...]:
        _, inventory = self._ready_broadcast_output_tree(output_dir)
        return inventory

    def _capture_ready_broadcast_path_tokens(
        self,
        paths: list[Path],
    ) -> dict[Path, _ReadyIdentityToken]:
        tokens: dict[Path, _ReadyIdentityToken] = {}
        for path in paths:
            before = self._ready_broadcast_path_identity_token(path)
            after = self._ready_broadcast_path_identity_token(path)
            if before != after:
                raise RuntimeError("ready broadcast dependency changed while its identity was captured")
            tokens[path] = after
        return tokens

    def _verify_ready_broadcast_path_tokens(
        self,
        path_tokens: dict[Path, _ReadyIdentityToken],
    ) -> None:
        for path, expected_token in path_tokens.items():
            try:
                if self._ready_broadcast_path_identity_token(path) != expected_token:
                    raise RuntimeError("ready broadcast dependency changed after validation")
            except OSError as exc:
                raise RuntimeError("ready broadcast dependency changed after validation") from exc

    def _ready_broadcast_directory_tokens(
        self,
        output_dir: Path,
        dependency_paths: list[Path],
    ) -> dict[Path, _ReadyIdentityToken]:
        repo_root = self.repo_root.resolve()
        directories = {output_dir, output_dir.parent}
        _, output_inventory = self._ready_broadcast_output_tree(output_dir)
        directories.update(output_dir / relative.rstrip("/") for relative in output_inventory if relative.endswith("/"))
        for dependency_path in dependency_paths:
            directory = dependency_path.parent
            if dependency_path.is_relative_to(output_dir):
                trusted_ancestor = output_dir
            elif dependency_path.is_relative_to(repo_root):
                trusted_ancestor = repo_root
            else:
                trusted_ancestor = directory.parent
            while True:
                directories.add(directory)
                if directory == trusted_ancestor or directory.parent == directory:
                    break
                directory = directory.parent
        tokens: dict[Path, _ReadyIdentityToken] = {}
        for directory in directories:
            if self._is_link_or_reparse_point(directory) or not directory.is_dir():
                raise RuntimeError("ready broadcast dependency ancestor must be a regular directory")
            tokens[directory] = self._ready_broadcast_path_identity_token(directory)
        return tokens

    def _verify_ready_broadcast_directory_tokens(
        self,
        directory_tokens: dict[Path, _ReadyIdentityToken],
    ) -> None:
        for directory, expected_token in directory_tokens.items():
            try:
                if (
                    self._is_link_or_reparse_point(directory)
                    or not directory.is_dir()
                    or self._ready_broadcast_path_identity_token(directory) != expected_token
                ):
                    raise RuntimeError("ready broadcast dependency directory changed during validation")
            except OSError as exc:
                raise RuntimeError("ready broadcast dependency directory changed during validation") from exc

    def _ready_broadcast_path_identity_token(self, path: Path) -> _ReadyIdentityToken:
        path = Path(os.path.abspath(path))
        if self._is_link_or_reparse_point(path):
            raise RuntimeError("ready broadcast identity path must not be a link or reparse point")
        if os.name != "nt":
            metadata = path.stat()
            return (
                int(metadata.st_dev),
                int(metadata.st_ino),
                int(metadata.st_size),
                int(metadata.st_mtime_ns),
                int(metadata.st_ctime_ns),
                int(metadata.st_mode),
            )
        return self._windows_ready_broadcast_path_identity_token(path)

    @staticmethod
    def _windows_ready_broadcast_path_identity_token(path: Path) -> _ReadyIdentityToken:
        import ctypes
        from ctypes import wintypes

        class ByHandleFileInformation(ctypes.Structure):
            _fields_ = (
                ("file_attributes", wintypes.DWORD),
                ("creation_time", wintypes.FILETIME),
                ("last_access_time", wintypes.FILETIME),
                ("last_write_time", wintypes.FILETIME),
                ("volume_serial_number", wintypes.DWORD),
                ("file_size_high", wintypes.DWORD),
                ("file_size_low", wintypes.DWORD),
                ("number_of_links", wintypes.DWORD),
                ("file_index_high", wintypes.DWORD),
                ("file_index_low", wintypes.DWORD),
            )

        class FileBasicInformation(ctypes.Structure):
            _fields_ = (
                ("creation_time", ctypes.c_longlong),
                ("last_access_time", ctypes.c_longlong),
                ("last_write_time", ctypes.c_longlong),
                ("change_time", ctypes.c_longlong),
                ("file_attributes", wintypes.DWORD),
            )

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        get_file_information = kernel32.GetFileInformationByHandle
        get_file_information.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
        get_file_information.restype = wintypes.BOOL
        get_file_information_ex = kernel32.GetFileInformationByHandleEx
        get_file_information_ex.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
        get_file_information_ex.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = create_file(
            str(path),
            0x00000080,  # FILE_READ_ATTRIBUTES
            0x00000007,  # share read, write, and delete while observing identity
            None,
            3,  # OPEN_EXISTING
            0x02200000,  # FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle is None or int(handle) == invalid_handle:
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error), str(path))
        try:
            identity = ByHandleFileInformation()
            basic = FileBasicInformation()
            if not get_file_information(handle, ctypes.byref(identity)):
                error = ctypes.get_last_error()
                raise OSError(error, ctypes.FormatError(error), str(path))
            if not get_file_information_ex(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
                error = ctypes.get_last_error()
                raise OSError(error, ctypes.FormatError(error), str(path))
            file_index = (int(identity.file_index_high) << 32) | int(identity.file_index_low)
            size_bytes = (int(identity.file_size_high) << 32) | int(identity.file_size_low)
            return (
                int(identity.volume_serial_number),
                file_index,
                size_bytes,
                int(basic.last_write_time),
                int(basic.creation_time),
                int(basic.change_time),
            )
        finally:
            close_handle(handle)

    def _acquire_ready_broadcast_file_lease(self, path: Path) -> _ReadyBroadcastFileLease:
        path = Path(os.path.abspath(path))
        if self._is_link_or_reparse_point(path) or not path.is_file():
            raise RuntimeError("ready broadcast dependency must be a regular file")
        handle: BinaryIO | None = None
        try:
            handle = _open_source_lease_handle(path)
            before = os.fstat(handle.fileno())
            current = path.stat()
            if not stat.S_ISREG(before.st_mode) or self._stat_result_token(before) != self._stat_result_token(current):
                raise RuntimeError("ready broadcast dependency changed while acquiring its lease")
            return _ReadyBroadcastFileLease(
                path=path,
                handle=handle,
                stat_token=self._stat_result_token(before),
            )
        except BaseException:
            if handle is not None:
                try:
                    _close_source_lease_handle(handle)
                except BaseException:
                    pass
            raise

    def _resolve_nonlink_ready_broadcast_file(self, path: Path, label: str) -> Path:
        candidate = Path(os.path.abspath(path))
        ancestor = candidate
        while True:
            if self._is_link_or_reparse_point(ancestor):
                raise RuntimeError(f"{label} must not traverse a link or reparse point")
            if ancestor.parent == ancestor:
                break
            ancestor = ancestor.parent
        resolved = candidate.resolve()
        if not resolved.is_file():
            raise RuntimeError(f"{label} is unavailable")
        return resolved

    def _verify_ready_broadcast_file_leases(
        self,
        leases: list[_ReadyBroadcastFileLease] | tuple[_ReadyBroadcastFileLease, ...],
    ) -> None:
        for lease in leases:
            if not self._ready_broadcast_file_lease_is_current(lease):
                raise RuntimeError("ready broadcast dependency changed during validation")

    def _ready_broadcast_file_lease_is_current(self, lease: _ReadyBroadcastFileLease) -> bool:
        try:
            if self._is_link_or_reparse_point(lease.path):
                return False
            return (
                self._stat_result_token(os.fstat(lease.handle.fileno())) == lease.stat_token
                and self._artifact_identity_token(lease.path) == lease.stat_token
            )
        except (OSError, ValueError):
            return False

    @staticmethod
    def _release_ready_broadcast_file_leases(
        leases: list[_ReadyBroadcastFileLease] | tuple[_ReadyBroadcastFileLease, ...],
    ) -> None:
        for lease in reversed(leases):
            try:
                _close_source_lease_handle(lease.handle)
            except BaseException:
                pass

    def _ready_broadcast_delivery_entry_is_current(self, entry: dict[str, Any]) -> bool:
        dependencies = entry.get("dependencies")
        dependency_tokens = entry.get("dependency_tokens")
        snapshot_leases = entry.get("snapshot_leases")
        directory_tokens = entry.get("directory_tokens")
        artifacts = entry.get("artifacts")
        snapshot_tokens = entry.get("snapshot_tokens")
        snapshot_manifest = entry.get("snapshot_manifest")
        snapshot_manifest_token = entry.get("snapshot_manifest_token")
        output_dir = entry.get("output_dir")
        output_inventory = entry.get("output_inventory")
        if (
            not isinstance(dependencies, tuple)
            or not isinstance(dependency_tokens, dict)
            or not isinstance(snapshot_leases, tuple)
            or len(snapshot_leases) != len(_READY_BROADCAST_DELIVERY_NAMES) + 1
            or not isinstance(directory_tokens, dict)
            or not isinstance(artifacts, dict)
            or set(artifacts) != set(_READY_BROADCAST_DELIVERY_NAMES)
            or not isinstance(snapshot_tokens, dict)
            or not isinstance(snapshot_manifest, Path)
            or not isinstance(snapshot_manifest_token, tuple)
            or not isinstance(output_dir, Path)
            or not isinstance(output_inventory, tuple)
        ):
            return False
        if any(not self._ready_broadcast_file_lease_is_current(lease) for lease in snapshot_leases):
            return False
        try:
            if any(not self._ready_broadcast_file_lease_is_current(lease) for lease in dependencies):
                return False
            self._verify_ready_broadcast_path_tokens(dependency_tokens)
            self._verify_ready_broadcast_directory_tokens(directory_tokens)
            if (
                self._is_link_or_reparse_point(snapshot_manifest)
                or self._artifact_identity_token(snapshot_manifest) != snapshot_manifest_token
            ):
                return False
            for name, path in artifacts.items():
                if (
                    not isinstance(path, Path)
                    or path.parent != snapshot_manifest.parent
                    or self._is_link_or_reparse_point(path)
                    or self._artifact_identity_token(path) != snapshot_tokens.get(name)
                ):
                    return False
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        return True

    def _read_ready_broadcast_lease_json(
        self,
        lease: _ReadyBroadcastFileLease,
        label: str,
    ) -> dict[str, Any]:
        lease.handle.seek(0)
        raw = lease.handle.read(16 * 1024 * 1024 + 1)
        lease.handle.seek(0)
        if len(raw) > 16 * 1024 * 1024:
            raise RuntimeError(f"{label} exceeds the safe snapshot bound")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{label} is invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{label} must contain a JSON object")
        return payload

    def _copy_ready_broadcast_lease(
        self,
        lease: _ReadyBroadcastFileLease,
        target: Path,
        *,
        expected_sha256: str | None,
        expected_size: int | None,
    ) -> tuple[str, int]:
        digest = hashlib.sha256()
        size_bytes = 0
        before = self._stat_result_token(os.fstat(lease.handle.fileno()))
        lease.handle.seek(0)
        with target.open("xb") as output_handle:
            while True:
                chunk = lease.handle.read(_READY_BROADCAST_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                output_handle.write(chunk)
                digest.update(chunk)
                size_bytes += len(chunk)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        lease.handle.seek(0)
        actual_sha256 = digest.hexdigest()
        if (
            before != lease.stat_token
            or not self._ready_broadcast_file_lease_is_current(lease)
            or (expected_sha256 is not None and actual_sha256 != expected_sha256)
            or (expected_size is not None and size_bytes != expected_size)
        ):
            raise RuntimeError(f"ready broadcast artifact changed while sealing: {lease.path.name}")
        return actual_sha256, size_bytes

    @staticmethod
    def _hash_ready_broadcast_lease(lease: _ReadyBroadcastFileLease) -> str:
        digest = hashlib.sha256()
        lease.handle.seek(0)
        while True:
            chunk = lease.handle.read(_READY_BROADCAST_COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
        lease.handle.seek(0)
        return digest.hexdigest()

    @staticmethod
    def _stat_result_token(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_size),
            int(metadata.st_mtime_ns),
            int(metadata.st_ctime_ns),
        )

    def get_cleanup_report(self, run_id: str) -> dict[str, Any]:
        return self._load_optional_json_artifact(run_id, "cleanup_report.json")

    def get_follow_cam_report(self, run_id: str) -> dict[str, Any]:
        return self._load_optional_json_artifact(run_id, "follow_cam_report.json")

    def get_ball_audit_report(self, run_id: str) -> dict[str, Any]:
        return self._load_optional_json_artifact(run_id, "ball_audit.json")

    def get_ai_review_triggers_report(self, run_id: str) -> dict[str, Any]:
        return self._load_optional_json_artifact(run_id, "ai_review_triggers.json")

    def get_event_candidates_report(self, run_id: str) -> dict[str, Any]:
        return self._load_optional_json_artifact(run_id, "event_candidates.json")

    def get_player_tracks_report(self, run_id: str) -> dict[str, Any]:
        return self._load_optional_json_artifact(run_id, "player_tracks.json")

    def _get_internal_artifact_path(self, run_id: str, artifact_name: str) -> Path:
        run = self.get_run(run_id)
        expected_status_generation = (
            self._ready_broadcast_status_generation(run) if self._is_ready_broadcast_run(run) else None
        )
        try:
            return self.get_artifact_path(
                run_id,
                artifact_name,
                expected_status_generation=expected_status_generation,
            )
        except ArtifactStatusGenerationConflict as exc:
            raise FileNotFoundError(artifact_name) from exc

    def get_camera_path(self, run_id: str, offset: int, limit: int) -> dict[str, Any]:
        camera_path: Path | None = None
        for name in ("camera_target.csv", "camera_path.v2.csv", "camera_path.csv"):
            try:
                camera_path = self._get_internal_artifact_path(run_id, name)
                break
            except FileNotFoundError:
                continue
        if camera_path is None:
            raise FileNotFoundError("camera_target.csv")
        with camera_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            columns = reader.fieldnames or []
        return {
            "columns": columns,
            "offset": offset,
            "limit": limit,
            "total_rows": len(rows),
            "rows": rows[offset : offset + limit],
        }

    def ai_explain(
        self,
        run_id: str | None,
        config_name: str | None,
        focus: str | None,
        language: str | None = None,
    ) -> dict[str, Any]:
        resolved_language = _normalize_ai_language(language)
        if self.ai_client.is_enabled():
            try:
                return self._ai_explain_with_model(
                    run_id=run_id,
                    config_name=config_name,
                    focus=focus,
                    language=resolved_language,
                )
            except Exception:
                pass
        return self._ai_explain_heuristic(
            run_id=run_id,
            config_name=config_name,
            focus=focus,
            language=resolved_language,
        )

    def ai_recommend(self, run_id: str, objective: str | None, language: str | None = None) -> dict[str, Any]:
        resolved_language = _normalize_ai_language(language)
        if self.ai_client.is_enabled():
            try:
                return self._ai_recommend_with_model(
                    run_id=run_id,
                    objective=objective,
                    language=resolved_language,
                )
            except Exception:
                pass
        return self._ai_recommend_heuristic(run_id=run_id, objective=objective, language=resolved_language)

    def ai_improve(
        self,
        run_id: str,
        objective: str | None = None,
        model: str | None = None,
        dry_run: bool = False,
        max_items: int = 20,
        language: str | None = None,
    ) -> dict[str, Any]:
        run = self.get_run(run_id)
        output_dir = Path(run["output_dir"]).resolve()
        resolved_language = _normalize_ai_language(language)
        report = write_ai_improvement_report(
            output_dir,
            client=self.ai_client,
            model=model,
            dry_run=dry_run,
            max_items=max_items,
            objective=objective,
            language=resolved_language,
        )
        (output_dir / "metrics_report.json").write_text(
            json.dumps(build_metrics_report(output_dir), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self._refresh_run_artifacts_and_stats(run_id, output_dir)
        artifact_path = (output_dir / "ai_improvement_report.json").resolve()
        return {
            "summary": compact_ai_improvement_summary(report) or {},
            "artifact_name": "ai_improvement_report.json",
            "artifact_path": str(artifact_path),
            "improvements": report.get("improvements") if isinstance(report.get("improvements"), list) else [],
            "highlight_adjustments": (
                report.get("highlight_adjustments") if isinstance(report.get("highlight_adjustments"), list) else []
            ),
        }

    def get_ai_improvement_status(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        output_dir = Path(run["output_dir"]).resolve()

        artifacts, payloads = self._collect_ai_improvement_status_artifacts(output_dir)
        ai_report = (
            payloads.get("ai_improvement_report.json")
            if isinstance(payloads.get("ai_improvement_report.json"), dict)
            else {}
        )
        approved_actions_report = (
            payloads.get(APPROVED_ACTIONS_FILE_NAME)
            if isinstance(payloads.get(APPROVED_ACTIONS_FILE_NAME), dict)
            else {}
        )
        final_manifest = (
            payloads.get("final_ai_improvement_artifact_manifest.json")
            if isinstance(payloads.get("final_ai_improvement_artifact_manifest.json"), dict)
            else {}
        )
        registry = (
            payloads.get("ai_candidate_registry.json")
            if isinstance(payloads.get("ai_candidate_registry.json"), dict)
            else {}
        )

        approval_index = self._ai_status_approval_index(approved_actions_report)
        comparison_index = self._ai_status_comparison_index(registry, final_manifest, payloads)
        selected_artifacts = (
            final_manifest.get("final_selected_artifacts")
            if isinstance(final_manifest.get("final_selected_artifacts"), list)
            else []
        )
        selected_candidate_ids = [
            str(item.get("candidate_id"))
            for item in selected_artifacts
            if isinstance(item, dict) and item.get("candidate_id")
        ]
        selected_candidate_id_set = set(selected_candidate_ids)

        groups: dict[str, list[dict[str, Any]]] = {
            "missing_ball": [],
            "noise": [],
            "camera_motion": [],
            "highlights": [],
        }
        seen_keys: set[tuple[str | None, str | None]] = set()
        seen_candidate_ids: set[str] = set()
        seen_improvement_ids: set[str] = set()
        seen_approval_ids: set[str] = set()

        improvements = ai_report.get("improvements") if isinstance(ai_report.get("improvements"), list) else []
        for improvement in improvements:
            if not isinstance(improvement, dict):
                continue
            item = self._build_ai_status_item(
                source=improvement,
                approval_index=approval_index,
                comparison_index=comparison_index,
                selected_candidate_ids=selected_candidate_id_set,
                output_dir=output_dir,
            )
            groups[self._ai_status_problem_group(improvement, item)].append(item)
            seen_keys.add((item.get("improvement_id"), item.get("candidate_id")))
            self._mark_ai_status_item_seen(
                item,
                seen_candidate_ids=seen_candidate_ids,
                seen_improvement_ids=seen_improvement_ids,
                seen_approval_ids=seen_approval_ids,
            )

        for action in approval_index["actions"]:
            key = (action.get("improvement_id"), action.get("candidate_id"))
            candidate_id = str(action.get("candidate_id") or "").strip()
            improvement_id = str(action.get("improvement_id") or "").strip()
            approval_id = str(action.get("approval_id") or "").strip()
            if (
                key in seen_keys
                or (candidate_id and candidate_id in seen_candidate_ids)
                or (improvement_id and improvement_id in seen_improvement_ids)
                or (approval_id and approval_id in seen_approval_ids)
            ):
                continue
            item = self._build_ai_status_item(
                source=action,
                approval_index=approval_index,
                comparison_index=comparison_index,
                selected_candidate_ids=selected_candidate_id_set,
                output_dir=output_dir,
            )
            groups[self._ai_status_problem_group(action, item)].append(item)
            seen_keys.add(key)
            self._mark_ai_status_item_seen(
                item,
                seen_candidate_ids=seen_candidate_ids,
                seen_improvement_ids=seen_improvement_ids,
                seen_approval_ids=seen_approval_ids,
            )

        for manifest_candidate in [
            *self._ai_status_manifest_candidate_sources(final_manifest),
            *self._ai_status_registry_candidate_sources(registry),
        ]:
            candidate_id = str(manifest_candidate.get("candidate_id") or "").strip()
            approval_ids = self._ai_status_source_approval_ids(manifest_candidate)
            if (candidate_id and candidate_id in seen_candidate_ids) or any(
                approval_id in seen_approval_ids for approval_id in approval_ids
            ):
                continue
            item = self._build_ai_status_item(
                source=manifest_candidate,
                approval_index=approval_index,
                comparison_index=comparison_index,
                selected_candidate_ids=selected_candidate_id_set,
                output_dir=output_dir,
            )
            groups[self._ai_status_problem_group(manifest_candidate, item)].append(item)
            self._mark_ai_status_item_seen(
                item,
                seen_candidate_ids=seen_candidate_ids,
                seen_improvement_ids=seen_improvement_ids,
                seen_approval_ids=seen_approval_ids,
            )

        final_manifest_artifact = next(
            (artifact for artifact in artifacts if artifact["name"] == "final_ai_improvement_artifact_manifest.json"),
            None,
        )
        final_manifest_status = self._ai_status_final_manifest_status(final_manifest, final_manifest_artifact)

        return {
            "schema_version": "1.0",
            "run_id": run_id,
            "output_dir": str(output_dir),
            "artifacts": artifacts,
            "items_by_problem_type": groups,
            "final_manifest_status": final_manifest_status,
            "final_selected_artifacts": selected_artifacts,
            "final_selected_artifact_candidate_ids": selected_candidate_ids,
        }

    def _collect_ai_improvement_status_artifacts(self, output_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        expected = [
            ("workflow", "stable_ai_improvement_workflow_report.json"),
            ("workflow", "ai_improvement_report.json"),
            ("approval", APPROVED_ACTIONS_FILE_NAME),
            ("recovery", APPROVED_CONFIG_PATCH_FILE_NAME),
            ("quality_gate", "ai_improvement_quality_gate.json"),
            ("final_manifest", "final_ai_improvement_artifact_manifest.json"),
            ("camera", "camera_motion_audit.json"),
            ("follow_cam", FOLLOW_CAM_RERENDER_PLAN_FILE_NAME),
            ("registry", "ai_candidate_registry.json"),
        ]
        artifacts: list[dict[str, Any]] = []
        payloads: dict[str, Any] = {}
        seen_names: set[str] = set()

        for category, name in expected:
            artifact, payload = self._ai_status_artifact(output_dir, name, category=category)
            artifacts.append(artifact)
            payloads[name] = payload
            seen_names.add(name)

        for path in sorted(output_dir.rglob("*")):
            if not path.is_file():
                continue
            relative_name = path.relative_to(output_dir).as_posix()
            if relative_name in seen_names:
                continue
            if not self._is_ai_status_candidate_artifact(path):
                continue
            category = self._ai_status_candidate_artifact_category(path)
            artifact, payload = self._ai_status_artifact(output_dir, relative_name, category=category)
            problem_type, candidate_id = self._ai_status_candidate_path_parts(path, output_dir)
            artifact["problem_type"] = problem_type
            artifact["candidate_id"] = candidate_id
            artifacts.append(artifact)
            payloads[relative_name] = payload
            seen_names.add(relative_name)

        return artifacts, payloads

    def _ai_status_artifact(self, output_dir: Path, relative_name: str, *, category: str) -> tuple[dict[str, Any], Any]:
        path = (output_dir / relative_name).resolve()
        base = {
            "name": relative_name,
            "category": category,
            "path": str(path) if path.exists() else None,
            "exists": path.exists(),
            "size_bytes": path.stat().st_size if path.exists() else None,
            "content_type": mimetypes.guess_type(str(path))[0] if path.exists() else None,
        }
        if not path.exists():
            return {**base, "status": "unavailable", "summary": f"{relative_name} is not available."}, None
        if path.suffix.lower() != ".json":
            return {**base, "status": "available", "summary": "available"}, None
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            return {**base, "status": "error", "summary": f"Corrupt JSON: {exc}"}, None
        if not isinstance(loaded, dict):
            return {**base, "status": "error", "summary": "Invalid JSON: expected an object."}, None
        return {**base, "status": "available", "summary": self._ai_status_payload_summary(loaded)}, loaded

    def _is_ai_status_candidate_artifact(self, path: Path) -> bool:
        name = path.name
        if name in {
            "missing_ball_recovery_comparison.json",
            "noise_candidate_comparison.json",
            "follow_cam_candidate_comparison.json",
            "highlight_candidate_comparison.json",
            "highlight_report.json",
        }:
            return True
        if path.suffix.lower() == ".mp4" and "highlight" in path.parts:
            return True
        return False

    def _ai_status_candidate_artifact_category(self, path: Path) -> str:
        name = path.name
        if name.endswith("_comparison.json") or name == "missing_ball_recovery_comparison.json":
            return "comparison"
        if name == "highlight_report.json" or (path.suffix.lower() == ".mp4" and "highlight" in path.parts):
            return "highlight"
        return "candidate"

    def _ai_status_candidate_path_parts(self, path: Path, output_dir: Path) -> tuple[str | None, str | None]:
        try:
            relative = path.relative_to(output_dir)
        except ValueError:
            return None, None
        parts = relative.parts
        if len(parts) >= 4 and parts[0] == "ai_candidates":
            return parts[1], parts[2]
        return None, None

    def _ai_status_payload_summary(self, payload: dict[str, Any]) -> str:
        summary = payload.get("summary")
        if isinstance(summary, dict):
            status = summary.get("status") or summary.get("workflow_status")
            if status:
                return str(status)
        status = payload.get("status") or payload.get("comparison_status")
        if status:
            return str(status)
        return "available"

    def _ai_status_approval_index(self, approved_actions_report: dict[str, Any]) -> dict[str, Any]:
        actions = (
            approved_actions_report.get("approved_actions")
            if isinstance(approved_actions_report.get("approved_actions"), list)
            else []
        )
        by_improvement: dict[str, list[dict[str, Any]]] = {}
        by_candidate: dict[str, list[dict[str, Any]]] = {}
        by_approval: dict[str, list[dict[str, Any]]] = {}
        valid_actions: list[dict[str, Any]] = []
        for action in actions:
            if not isinstance(action, dict):
                continue
            valid_actions.append(action)
            improvement_id = str(action.get("improvement_id") or "").strip()
            candidate_id = str(action.get("candidate_id") or "").strip()
            approval_id = str(action.get("approval_id") or "").strip()
            if improvement_id:
                by_improvement.setdefault(improvement_id, []).append(action)
            if candidate_id:
                by_candidate.setdefault(candidate_id, []).append(action)
            if approval_id:
                by_approval.setdefault(approval_id, []).append(action)
        return {
            "actions": valid_actions,
            "by_improvement": by_improvement,
            "by_candidate": by_candidate,
            "by_approval": by_approval,
        }

    def _ai_status_comparison_index(
        self,
        registry: dict[str, Any],
        final_manifest: dict[str, Any],
        payloads: dict[str, Any],
    ) -> dict[str, dict[str, dict[str, Any]]]:
        by_candidate: dict[str, dict[str, Any]] = {}
        by_approval: dict[str, dict[str, Any]] = {}
        for source in (registry.get("candidates"), final_manifest.get("comparison_reports")):
            if not isinstance(source, list):
                continue
            for item in source:
                if isinstance(item, dict):
                    self._ai_status_index_candidate_payload(by_candidate, by_approval, item)
        for payload in payloads.values():
            if isinstance(payload, dict) and self._ai_status_is_comparison_payload(payload):
                self._ai_status_index_candidate_payload(by_candidate, by_approval, payload)
        return {"by_candidate": by_candidate, "by_approval": by_approval}

    def _ai_status_manifest_candidate_sources(self, final_manifest: dict[str, Any]) -> list[dict[str, Any]]:
        candidates_by_id: dict[str, dict[str, Any]] = {}
        for key, promotion_status in (
            ("candidate_outputs", None),
            ("pending_candidates", "pending_confirmation"),
            ("rejected_candidates", "rejected"),
            ("unsupported_candidates", "blocked"),
            ("resolved_noop_candidates", "not_promoted"),
            ("final_selected_artifacts", "promoted"),
        ):
            items = final_manifest.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                candidate_id = str(item.get("candidate_id") or item.get("id") or "").strip()
                identity = candidate_id or str(item.get("approval_id") or "").strip()
                if not identity:
                    continue
                merged = candidates_by_id.setdefault(identity, {})
                merged.update({field: value for field, value in item.items() if value not in (None, "", [])})
                if candidate_id:
                    merged["candidate_id"] = candidate_id
                if promotion_status is not None:
                    merged["promotion_status"] = promotion_status
        return list(candidates_by_id.values())

    def _ai_status_registry_candidate_sources(self, registry: dict[str, Any]) -> list[dict[str, Any]]:
        items = registry.get("candidates")
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def _ai_status_is_comparison_payload(self, payload: dict[str, Any]) -> bool:
        return bool(payload.get("candidate_id")) and (
            "comparison_status" in payload or "comparison_report" in payload or isinstance(payload.get("summary"), dict)
        )

    def _ai_status_index_candidate_payload(
        self,
        by_candidate: dict[str, dict[str, Any]],
        by_approval: dict[str, dict[str, Any]],
        payload: dict[str, Any],
    ) -> None:
        candidate_id = str(payload.get("candidate_id") or payload.get("id") or "").strip()
        if candidate_id:
            current = by_candidate.setdefault(candidate_id, {})
            current.update({key: value for key, value in payload.items() if value not in (None, "", [])})
        for approval_id in self._ai_status_source_approval_ids(payload):
            current = by_approval.setdefault(approval_id, {})
            current.update({key: value for key, value in payload.items() if value not in (None, "", [])})

    def _build_ai_status_item(
        self,
        *,
        source: dict[str, Any],
        approval_index: dict[str, Any],
        comparison_index: dict[str, dict[str, dict[str, Any]]],
        selected_candidate_ids: set[str],
        output_dir: Path,
    ) -> dict[str, Any]:
        improvement_id = str(source.get("id") or source.get("improvement_id") or "").strip() or None
        candidate_id = str(source.get("candidate_id") or "").strip() or None
        approvals = list(approval_index["by_improvement"].get(improvement_id or "", []))
        if candidate_id:
            for action in approval_index["by_candidate"].get(candidate_id, []):
                if action not in approvals:
                    approvals.append(action)
        if approvals and candidate_id is None:
            candidate_id = str(approvals[0].get("candidate_id") or "").strip() or None
        source_approval_ids = self._ai_status_source_approval_ids(source)
        for approval_id in source_approval_ids:
            for action in approval_index["by_approval"].get(approval_id, []):
                if action not in approvals:
                    approvals.append(action)
        approval_ids = self._ai_status_unique_strings(
            [
                *source_approval_ids,
                *[
                    str(action.get("approval_id"))
                    for action in approvals
                    if isinstance(action, dict) and action.get("approval_id")
                ],
            ]
        )
        comparison = self._ai_status_comparison_for_item(
            comparison_index,
            candidate_id=candidate_id,
            approval_ids=approval_ids,
        )
        if candidate_id is None:
            candidate_id = str(comparison.get("candidate_id") or "").strip() or None
        consumed_approval_ids = self._ai_status_string_list(comparison.get("consumed_approval_ids"))
        if not consumed_approval_ids and comparison.get("approval_id"):
            consumed_approval_ids = [str(comparison["approval_id"])]
        approval_ids = self._ai_status_unique_strings([*approval_ids, *consumed_approval_ids])
        comparison_status = self._ai_status_comparison_status(comparison)
        promotion_status = str(source.get("promotion_status") or comparison.get("promotion_status") or "not_promoted")
        if candidate_id in selected_candidate_ids:
            promotion_status = "promoted"
        frame_window = self._ai_status_frame_window(source, approvals)

        return {
            "id": improvement_id or candidate_id,
            "improvement_id": improvement_id,
            "candidate_id": candidate_id,
            "approval_ids": approval_ids,
            "frame_window": frame_window,
            "evidence_ids": self._ai_status_evidence_ids(source, approvals),
            "confidence": source.get("confidence"),
            "false_positive_class": source.get("false_positive_class")
            or next(
                (action.get("false_positive_class") for action in approvals if action.get("false_positive_class")), None
            ),
            "recommended_action": source.get("recommended_action") or source.get("approved_action"),
            "approved_action": next(
                (action.get("approved_action") for action in approvals if action.get("approved_action")), None
            ),
            "approval_status": "approved" if approvals else "none",
            "consumed_approval_ids": consumed_approval_ids,
            "comparison_status": comparison_status,
            "promotion_status": promotion_status,
            "artifact_references": self._ai_status_artifact_references(output_dir, comparison),
        }

    def _ai_status_comparison_for_item(
        self,
        comparison_index: dict[str, dict[str, dict[str, Any]]],
        *,
        candidate_id: str | None,
        approval_ids: list[str],
    ) -> dict[str, Any]:
        if candidate_id:
            comparison = comparison_index["by_candidate"].get(candidate_id)
            if comparison is not None:
                return comparison
        for approval_id in approval_ids:
            comparison = comparison_index["by_approval"].get(approval_id)
            if comparison is not None:
                return comparison
        return {}

    def _ai_status_comparison_status(self, comparison: dict[str, Any]) -> str:
        status = comparison.get("comparison_status")
        if isinstance(status, str) and status:
            return status
        summary = comparison.get("summary")
        if isinstance(summary, dict) and isinstance(summary.get("status"), str):
            return summary["status"]
        return "none"

    def _ai_status_problem_group(self, source: dict[str, Any], item: dict[str, Any]) -> str:
        raw_problem = str(source.get("problem_type") or "").strip()
        if raw_problem in {"missing_ball", "noise"}:
            return raw_problem
        if raw_problem in {"follow_cam", "camera_motion"}:
            return "camera_motion"
        if raw_problem in {"highlight", "highlights"}:
            return "highlights"
        action = str(item.get("recommended_action") or item.get("approved_action") or "").strip()
        if action in {"adjust_highlight_window", "render_suggested_highlight"}:
            return "highlights"
        if action in {"adjust_follow_cam", "tracking_rerun_before_follow_cam", "human_review_camera_motion"}:
            return "camera_motion"
        if action in {"noise_filter_adjustment", "tighten_noise_filter", "reject_noise"} or item.get(
            "false_positive_class"
        ):
            return "noise"
        return "missing_ball"

    def _ai_status_frame_window(
        self,
        source: dict[str, Any],
        approvals: list[dict[str, Any]],
    ) -> dict[str, int] | None:
        for container in [source, *approvals]:
            for key in ("rerun_scope", "suggested_window", "frame_window"):
                window = container.get(key)
                if isinstance(window, dict):
                    normalized = self._ai_status_normalize_window(window)
                    if normalized is not None:
                        return normalized
            normalized = self._ai_status_normalize_window(container)
            if normalized is not None:
                return normalized
        return None

    def _ai_status_normalize_window(self, value: dict[str, Any]) -> dict[str, int] | None:
        if value.get("start_frame") is None or value.get("end_frame") is None:
            return None
        try:
            start_frame = int(value["start_frame"])
            end_frame = int(value["end_frame"])
        except (TypeError, ValueError):
            return None
        if start_frame < 0 or end_frame < start_frame:
            return None
        return {"start_frame": start_frame, "end_frame": end_frame}

    def _ai_status_evidence_ids(self, source: dict[str, Any], approvals: list[dict[str, Any]]) -> list[str]:
        ids: list[str] = []
        for container in [source, *approvals]:
            evidence = container.get("evidence")
            if isinstance(evidence, list):
                for item in evidence:
                    if isinstance(item, str):
                        self._append_unique_string(ids, item)
                    elif isinstance(item, dict):
                        for key in ("id", "packet_id", "source_packet_id", "visual_review_id", "event_candidate_id"):
                            self._append_unique_string(ids, item.get(key))
            evidence_payload = container.get("evidence_payload")
            if isinstance(evidence_payload, dict):
                for key in ("id", "packet_id", "source_packet_id", "visual_review_id", "event_candidate_id"):
                    self._append_unique_string(ids, evidence_payload.get(key))
            for key in ("source_packet_id", "visual_review_id", "camera_motion_event_id", "event_candidate_id"):
                self._append_unique_string(ids, container.get(key))
        return ids

    def _append_unique_string(self, target: list[str], value: Any) -> None:
        if isinstance(value, str) and value.strip() and value.strip() not in target:
            target.append(value.strip())

    def _mark_ai_status_item_seen(
        self,
        item: dict[str, Any],
        *,
        seen_candidate_ids: set[str],
        seen_improvement_ids: set[str],
        seen_approval_ids: set[str],
    ) -> None:
        if item.get("candidate_id"):
            seen_candidate_ids.add(str(item["candidate_id"]))
        if item.get("improvement_id"):
            seen_improvement_ids.add(str(item["improvement_id"]))
        seen_approval_ids.update(self._ai_status_source_approval_ids(item))

    def _ai_status_source_approval_ids(self, source: dict[str, Any]) -> list[str]:
        return self._ai_status_unique_strings(
            [
                source.get("approval_id"),
                *self._ai_status_string_list(source.get("approval_ids")),
                *self._ai_status_string_list(source.get("consumed_approval_ids")),
            ]
        )

    def _ai_status_unique_strings(self, values: list[Any]) -> list[str]:
        unique: list[str] = []
        for value in values:
            if isinstance(value, str) and value.strip() and value.strip() not in unique:
                unique.append(value.strip())
        return unique

    def _ai_status_string_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if str(item).strip()]

    def _ai_status_artifact_references(self, output_dir: Path, comparison: dict[str, Any]) -> list[dict[str, Any]]:
        references: list[dict[str, Any]] = []
        for key in ("comparison_report", "path"):
            value = comparison.get(key)
            if isinstance(value, str) and value.strip():
                self._append_ai_status_artifact_reference(references, output_dir, value.strip(), category="comparison")
        for value in comparison.get("candidate_artifacts") or []:
            if isinstance(value, str) and value.strip():
                self._append_ai_status_artifact_reference(references, output_dir, value.strip(), category="candidate")
        return references

    def _append_ai_status_artifact_reference(
        self,
        references: list[dict[str, Any]],
        output_dir: Path,
        relative_name: str,
        *,
        category: str,
    ) -> None:
        if any(reference["name"] == relative_name for reference in references):
            return
        relative_path = Path(relative_name)
        if relative_path.is_absolute():
            references.append(
                {
                    "name": relative_path.name,
                    "path": None,
                    "status": "error",
                    "category": category,
                }
            )
            return
        path = (output_dir / relative_path).resolve()
        if not self._path_is_relative_to(path, output_dir.resolve()):
            references.append(
                {
                    "name": relative_name,
                    "path": None,
                    "status": "error",
                    "category": category,
                }
            )
            return
        references.append(
            {
                "name": relative_name,
                "path": str(path) if path.exists() else None,
                "status": "available" if path.exists() else "unavailable",
                "category": category,
            }
        )

    def _path_is_relative_to(self, path: Path, root: Path) -> bool:
        try:
            self._normalize_filesystem_path(path).relative_to(self._normalize_filesystem_path(root))
        except ValueError:
            return False
        return True

    def _ai_status_final_manifest_status(
        self,
        final_manifest: dict[str, Any],
        artifact: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not artifact or artifact.get("status") != "available":
            return {
                "status": "unavailable",
                "artifact_status": artifact.get("status", "unavailable") if artifact else "unavailable",
                "summary": artifact.get("summary") if artifact else "final manifest is not available.",
                "path": artifact.get("path") if artifact else None,
            }
        summary = final_manifest.get("summary")
        status = None
        if isinstance(summary, dict):
            status = summary.get("status")
        if not status:
            status = final_manifest.get("status") or "available"
        return {
            "status": str(status),
            "artifact_status": "available",
            "summary": artifact.get("summary"),
            "path": artifact.get("path"),
        }

    def ai_improvement_approve(
        self,
        run_id: str,
        improvement_ids: list[str],
        approved_by: str = "operator",
        rerun_scope_overrides: dict[str, dict[str, Any]] | None = None,
        local_search_roi_overrides: dict[str, dict[str, Any]] | None = None,
        config_patch_overrides: dict[str, dict[str, Any]] | None = None,
        suggested_window_overrides: dict[str, dict[str, Any]] | None = None,
        clip_action_overrides: dict[str, str] | None = None,
        follow_cam_rerender_plan_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        run = self.get_run(run_id)
        output_dir = Path(run["output_dir"]).resolve()
        artifact = approve_ai_improvement_actions(
            output_dir,
            run_id=run_id,
            improvement_ids=improvement_ids,
            approved_by=approved_by,
            approval_source="api",
            rerun_scope_overrides=rerun_scope_overrides,
            local_search_roi_overrides=local_search_roi_overrides,
            config_patch_overrides=config_patch_overrides,
            suggested_window_overrides=suggested_window_overrides,
            clip_action_overrides=clip_action_overrides,
            follow_cam_rerender_plan_overrides=follow_cam_rerender_plan_overrides,
        )
        (output_dir / "metrics_report.json").write_text(
            json.dumps(build_metrics_report(output_dir), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self._refresh_run_artifacts_and_stats(run_id, output_dir)

        artifact_path = (output_dir / APPROVED_ACTIONS_FILE_NAME).resolve()
        config_patch_path = (output_dir / APPROVED_CONFIG_PATCH_FILE_NAME).resolve()
        follow_cam_plan_path = (output_dir / FOLLOW_CAM_RERENDER_PLAN_FILE_NAME).resolve()
        response = {
            **artifact,
            "artifact_name": APPROVED_ACTIONS_FILE_NAME,
            "artifact_path": str(artifact_path),
            "config_patch_artifact_name": None,
            "config_patch_artifact_path": None,
            "follow_cam_rerender_plan_artifact_name": None,
            "follow_cam_rerender_plan_artifact_path": None,
        }
        if config_patch_path.exists():
            response["config_patch_artifact_name"] = APPROVED_CONFIG_PATCH_FILE_NAME
            response["config_patch_artifact_path"] = str(config_patch_path)
        if follow_cam_plan_path.exists():
            response["follow_cam_rerender_plan_artifact_name"] = FOLLOW_CAM_RERENDER_PLAN_FILE_NAME
            response["follow_cam_rerender_plan_artifact_path"] = str(follow_cam_plan_path)
        response["summary"] = self._approval_summary(
            artifact=artifact,
            artifact_path=artifact_path,
            config_patch_path=config_patch_path,
            follow_cam_plan_path=follow_cam_plan_path,
        )
        return response

    def ai_candidate_finalize(
        self,
        *,
        run_id: str,
        problem_type: str,
        candidate_id: str,
        approval_id: str,
        decision: str,
        output_role: str,
        confirm_warn: bool = False,
        note: str | None = None,
    ) -> dict[str, Any]:
        run = self.get_run(run_id)
        output_dir = Path(run["output_dir"]).resolve()
        result = finalize_ai_candidate(
            output_dir,
            problem_type=problem_type,
            candidate_id=candidate_id,
            approval_id=approval_id,
            decision=decision,
            output_role=output_role,
            confirm_warn=confirm_warn,
            note=note,
        )
        self._refresh_run_artifacts_and_stats(run_id, output_dir)
        return result

    def _approval_summary(
        self,
        *,
        artifact: dict[str, Any],
        artifact_path: Path,
        config_patch_path: Path,
        follow_cam_plan_path: Path,
    ) -> dict[str, Any]:
        actions = artifact.get("approved_actions") if isinstance(artifact.get("approved_actions"), list) else []
        action_counts: dict[str, int] = {}
        config_patch_count = 0
        for action in actions:
            if not isinstance(action, dict):
                continue
            action_name = str(action.get("approved_action") or "")
            if action_name:
                action_counts[action_name] = action_counts.get(action_name, 0) + 1
            if isinstance(action.get("config_patch"), dict) and action["config_patch"]:
                config_patch_count += 1

        follow_cam_plan = self._read_optional_json(follow_cam_plan_path)
        requires_tracking_rerun = bool(action_counts.get("tracking_rerun_before_follow_cam"))
        if isinstance(follow_cam_plan, dict):
            requires_tracking_rerun = requires_tracking_rerun or bool(follow_cam_plan.get("requires_tracking_rerun"))
        rerun_window_count = action_counts.get("targeted_rerun", 0) + action_counts.get("rerun_ball_window", 0)
        requires_high_recall_rerun = bool(rerun_window_count)
        requires_highlight_render = bool(
            action_counts.get("adjust_highlight_window") or action_counts.get("render_suggested_highlight")
        )
        requires_follow_cam_rerender = (
            follow_cam_plan_path.exists()
            and bool(action_counts.get("adjust_follow_cam"))
            and not requires_tracking_rerun
        )
        requires_config_apply = config_patch_path.exists()
        requires_execution = any(
            (
                requires_high_recall_rerun,
                requires_tracking_rerun,
                requires_follow_cam_rerender,
                requires_highlight_render,
                requires_config_apply,
            )
        )

        return {
            "approved_action_count": len([action for action in actions if isinstance(action, dict)]),
            "approved_action_counts": action_counts,
            "targeted_rerun_count": rerun_window_count,
            "config_patch_count": config_patch_count,
            "highlight_action_count": action_counts.get("adjust_highlight_window", 0)
            + action_counts.get("render_suggested_highlight", 0),
            "follow_cam_action_count": action_counts.get("adjust_follow_cam", 0)
            + action_counts.get("tracking_rerun_before_follow_cam", 0),
            "requires_execution": requires_execution,
            "requires_high_recall_rerun": requires_high_recall_rerun,
            "requires_tracking_rerun": requires_tracking_rerun,
            "requires_follow_cam_rerender": requires_follow_cam_rerender,
            "requires_highlight_render": requires_highlight_render,
            "artifacts": {
                "approved_actions": self._approval_artifact_summary(
                    APPROVED_ACTIONS_FILE_NAME,
                    artifact_path,
                    exists=True,
                ),
                "config_patch": self._approval_artifact_summary(
                    APPROVED_CONFIG_PATCH_FILE_NAME if config_patch_path.exists() else None,
                    config_patch_path,
                    exists=config_patch_path.exists(),
                ),
                "follow_cam_rerender_plan": self._approval_artifact_summary(
                    FOLLOW_CAM_RERENDER_PLAN_FILE_NAME if follow_cam_plan_path.exists() else None,
                    follow_cam_plan_path,
                    exists=follow_cam_plan_path.exists(),
                ),
            },
        }

    def _approval_artifact_summary(self, name: str | None, path: Path, *, exists: bool) -> dict[str, Any]:
        return {
            "name": name,
            "path": str(path) if exists else None,
            "exists": exists,
        }

    def _ai_explain_heuristic(
        self,
        run_id: str | None,
        config_name: str | None,
        focus: str | None,
        language: str,
    ) -> dict[str, Any]:
        evidence: list[str] = []
        summary_parts: list[str] = []

        if run_id:
            run = self.get_run(run_id)
            raw_stats = run.get("stats", {}).get("raw", {})
            cleaned_stats = run.get("stats", {}).get("cleaned", {})
            summary_parts.append(
                _localized_text(
                    language,
                    en=(
                        f"Run {run_id} is {_localized_run_status(language, run['status'])} with cleaned detected ratio "
                        f"{float(cleaned_stats.get('detected_ratio', raw_stats.get('detected_ratio', 0.0))) * 100:.1f}%."
                    ),
                    zh=(
                        f"\u8fd0\u884c {run_id} \u5f53\u524d\u4e3a{_localized_run_status(language, run['status'])}"
                        f"\uff0c\u6e05\u6d17\u540e\u68c0\u6d4b\u7387\u4e3a "
                        f"{float(cleaned_stats.get('detected_ratio', raw_stats.get('detected_ratio', 0.0))) * 100:.1f}%\u3002"
                    ),
                )
            )
            evidence.extend(
                [
                    _localized_text(
                        language, en=f"Run status={run['status']}", zh=f"\u8fd0\u884c\u72b6\u6001={run['status']}"
                    ),
                    _localized_text(
                        language,
                        en=f"Run config={run.get('config_name')}",
                        zh=f"\u8fd0\u884c\u914d\u7f6e={run.get('config_name')}",
                    ),
                    _localized_text(
                        language,
                        en=f"Raw detected={raw_stats.get('detected')}",
                        zh=f"\u539f\u59cb\u68c0\u6d4b={raw_stats.get('detected')}",
                    ),
                    _localized_text(
                        language,
                        en=f"Raw lost={raw_stats.get('lost')}",
                        zh=f"\u539f\u59cb\u4e22\u5931={raw_stats.get('lost')}",
                    ),
                    _localized_text(
                        language,
                        en=f"Cleaned detected={cleaned_stats.get('detected')}",
                        zh=f"\u6e05\u6d17\u540e\u68c0\u6d4b={cleaned_stats.get('detected')}",
                    ),
                    _localized_text(
                        language,
                        en=f"Cleaned lost={cleaned_stats.get('lost')}",
                        zh=f"\u6e05\u6d17\u540e\u4e22\u5931={cleaned_stats.get('lost')}",
                    ),
                ]
            )

        if config_name:
            config = self.get_config(config_name)
            resolved = config["resolved"]
            config_lines = _build_config_line_explanations(config["raw"], language)
            summary_parts.append(
                _localized_text(
                    language,
                    en=(
                        f"Config {config_name} has {len(config_lines)} explicit YAML entries. "
                        f"postprocess={resolved.get('postprocess', {}).get('enabled')}, "
                        f"follow_cam={resolved.get('follow_cam', {}).get('enabled')}."
                    ),
                    zh=(
                        f"\u914d\u7f6e {config_name} \u6709 {len(config_lines)} \u4e2a\u663e\u5f0f YAML \u914d\u7f6e\u9879\u3002"
                        f"postprocess={resolved.get('postprocess', {}).get('enabled')}\uff0c"
                        f"follow_cam={resolved.get('follow_cam', {}).get('enabled')}\u3002"
                    ),
                )
            )
            evidence.extend(config_lines)

        if focus:
            summary_parts.append(
                _localized_text(
                    language,
                    en=f"Requested focus: {focus}.",
                    zh=f"\u5f53\u524d\u76ee\u6807\uff1a{focus}\u3002",
                )
            )

        if not summary_parts:
            summary_parts.append(
                _localized_text(
                    language,
                    en="No run or config was provided, so AI explanation has no grounded evidence yet.",
                    zh="\u8fd8\u6ca1\u6709\u63d0\u4f9b run \u6216\u914d\u7f6e\uff0c\u6240\u4ee5\u73b0\u5728\u8fd8\u6ca1\u6709\u53ef\u843d\u5730\u7684\u8bc1\u636e\u6458\u8981\u3002",
                )
            )

        return {
            "summary": " ".join(summary_parts),
            "evidence": evidence,
        }

    def _ai_recommend_heuristic(self, run_id: str, objective: str | None, language: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        config_name = run.get("config_name")
        if not config_name:
            raise FileNotFoundError(f"Run {run_id} is not linked to a config.")

        config = self.get_config(config_name)
        raw_stats = run.get("stats", {}).get("raw", {})
        cleaned_stats = run.get("stats", {}).get("cleaned", {})
        follow_cam_stats = run.get("stats", {}).get("follow_cam", {})
        objective_text = (objective or "").strip().lower()

        lost_ratio = float(cleaned_stats.get("lost_ratio", raw_stats.get("lost_ratio", 0.0)) or 0.0)
        detected_ratio = float(cleaned_stats.get("detected_ratio", raw_stats.get("detected_ratio", 0.0)) or 0.0)
        mean_crop_height = float(follow_cam_stats.get("mean_crop_height", 0.0) or 0.0)

        patch: dict[str, Any] = {}
        output_slug = "grounded_recommendation"
        title = _localized_text(
            language,
            en="Grounded Recommendation",
            zh="\u57fa\u4e8e\u8bc1\u636e\u7684\u5efa\u8bae",
        )
        diagnosis = _localized_text(
            language,
            en=(
                f"Detected ratio is {detected_ratio * 100:.1f}% and lost ratio is {lost_ratio * 100:.1f}% "
                f"for run {run_id}."
            ),
            zh=(
                f"\u8fd0\u884c {run_id} \u7684\u68c0\u6d4b\u7387\u4e3a {detected_ratio * 100:.1f}%"
                f"\uff0c\u4e22\u5931\u7387\u4e3a {lost_ratio * 100:.1f}%\u3002"
            ),
        )
        recommendation = _localized_text(
            language,
            en="Stay on the current baseline and make only targeted adjustments.",
            zh="\u5148\u7559\u5728\u5f53\u524d\u57fa\u7ebf\u4e0a\uff0c\u53ea\u505a\u6709\u9488\u5bf9\u6027\u7684\u5c0f\u8c03\u6574\u3002",
        )
        expected_tradeoff = _localized_text(
            language,
            en="Conservative changes keep current gains and avoid reintroducing noisy regressions.",
            zh="\u4fdd\u5b88\u6539\u52a8\u80fd\u5c3d\u91cf\u4fdd\u4f4f\u73b0\u5728\u7684\u6536\u76ca\uff0c\u907f\u514d\u518d\u6b21\u5f15\u5165\u660e\u663e\u566a\u58f0\u56de\u9000\u3002",
        )
        evidence = [
            _localized_text(language, en=f"Run ID={run_id}", zh=f"\u8fd0\u884c ID={run_id}"),
            _localized_text(language, en=f"Config={config_name}", zh=f"\u914d\u7f6e={config_name}"),
            _localized_text(
                language,
                en=f"Cleaned detected ratio={detected_ratio:.4f}",
                zh=f"\u6e05\u6d17\u540e\u68c0\u6d4b\u7387={detected_ratio:.4f}",
            ),
            _localized_text(
                language,
                en=f"Cleaned lost ratio={lost_ratio:.4f}",
                zh=f"\u6e05\u6d17\u540e\u4e22\u5931\u7387={lost_ratio:.4f}",
            ),
        ]

        if any(
            token in objective_text
            for token in [
                "camera",
                "follow",
                "zoom",
                "pan",
                "\u955c\u5934",
                "\u8ddf\u968f",
                "\u8ddf\u62cd",
                "\u5e73\u79fb",
                "\u7f29\u653e",
                "\u76f8\u673a",
            ]
        ):
            current_follow = config["resolved"].get("follow_cam", {})
            patch = {
                "follow_cam": {
                    "glide_pan_smoothing": round(
                        max(0.06, float(current_follow.get("glide_pan_smoothing", 0.10)) - 0.02), 2
                    ),
                    "catch_up_pan_smoothing": round(
                        max(0.16, float(current_follow.get("catch_up_pan_smoothing", 0.22)) - 0.02), 2
                    ),
                    "zoom_out_confirm_frames": int(current_follow.get("zoom_out_confirm_frames", 6)) + 2,
                    "zoom_in_confirm_frames": int(current_follow.get("zoom_in_confirm_frames", 12)) + 2,
                    "zoom_hold_frames_after_change": int(current_follow.get("zoom_hold_frames_after_change", 16)) + 4,
                }
            }
            output_slug = "follow_cam_stabilization"
            title = _localized_text(
                language, en="Follow-Cam Stabilization", zh="\u8ddf\u968f\u955c\u5934\u7a33\u5b9a\u5316"
            )
            diagnosis = _localized_text(
                language,
                en=f"Mean crop height is {mean_crop_height:.1f}px. The fastest win is to make pan and zoom slower to react.",
                zh=(
                    f"\u5e73\u5747\u88c1\u5207\u9ad8\u5ea6\u4e3a {mean_crop_height:.1f}px\u3002"
                    "\u6700\u76f4\u63a5\u7684\u6539\u8fdb\u662f\u5148\u653e\u6162\u5e73\u79fb\u548c\u7f29\u653e\u7684\u53cd\u5e94\u901f\u5ea6\u3002"
                ),
            )
            recommendation = _localized_text(
                language,
                en="Slow pan response first and require longer zoom confirmation before changing crop depth.",
                zh="\u5148\u653e\u6162\u5e73\u79fb\u54cd\u5e94\uff0c\u5e76\u63d0\u9ad8\u7f29\u653e\u786e\u8ba4\u65f6\u95f4\uff0c\u518d\u6539\u53d8\u753b\u9762\u6df1\u5ea6\u3002",
            )
            expected_tradeoff = _localized_text(
                language,
                en="The camera will feel steadier, but fast breaks may take slightly longer to catch up.",
                zh="\u955c\u5934\u4f1a\u66f4\u7a33\uff0c\u4f46\u5feb\u901f\u653b\u9632\u8f6c\u6362\u65f6\u53ef\u80fd\u4f1a\u7a0d\u6162\u4e00\u70b9\u8ddf\u4e0a\u3002",
            )
            evidence.extend(
                [
                    _localized_text(
                        language,
                        en=f"Follow-cam mean crop height={mean_crop_height:.2f}",
                        zh=f"\u8ddf\u968f\u955c\u5934\u5e73\u5747\u88c1\u5207\u9ad8\u5ea6={mean_crop_height:.2f}",
                    ),
                    _localized_text(
                        language,
                        en=f"Follow-cam enabled={run.get('modules_enabled', {}).get('follow_cam')}",
                        zh=f"\u8ddf\u968f\u955c\u5934\u5df2\u542f\u7528={run.get('modules_enabled', {}).get('follow_cam')}",
                    ),
                ]
            )
        elif lost_ratio > 0.18:
            current_dynamic = config["resolved"].get("scene_bias", {}).get("dynamic_air_recovery", {})
            patch = {
                "scene_bias": {
                    "dynamic_air_recovery": {
                        "tentative_reacquire_confidence_threshold": round(
                            min(
                                0.36,
                                float(current_dynamic.get("tentative_reacquire_confidence_threshold", 0.30)) + 0.02,
                            ),
                            2,
                        ),
                        "tentative_reacquire_score_threshold": round(
                            min(0.45, float(current_dynamic.get("tentative_reacquire_score_threshold", 0.38)) + 0.02),
                            2,
                        ),
                    }
                }
            }
            output_slug = "reacquire_tightening"
            title = _localized_text(language, en="Reacquire Tightening", zh="\u91cd\u65b0\u6355\u83b7\u6536\u7d27")
            diagnosis = _localized_text(
                language,
                en="Lost ratio is still material, but global detector loosening is riskier than targeted reacquire tightening.",
                zh="\u4e22\u5931\u7387\u4ecd\u7136\u504f\u9ad8\uff0c\u4f46\u76f4\u63a5\u5168\u5c40\u653e\u5bbd detector \u98ce\u9669\u66f4\u5927\uff0c\u5148\u6536\u7d27\u91cd\u65b0\u6355\u83b7\u4f1a\u66f4\u7a33\u3002",
            )
            recommendation = _localized_text(
                language,
                en="Tighten tentative reacquire acceptance before changing detector sensitivity.",
                zh="\u5148\u6536\u7d27 tentative reacquire \u7684\u63a5\u53d7\u9608\u503c\uff0c\u518d\u8003\u8651\u52a8 detector \u7075\u654f\u5ea6\u3002",
            )
            expected_tradeoff = _localized_text(
                language,
                en="This should suppress noisy far-jump recoveries, but may delay a few true long-gap reacquires.",
                zh="\u8fd9\u4f1a\u538b\u6389\u4e00\u4e9b\u566a\u58f0\u6027\u7684\u8fdc\u8df3\u6062\u590d\uff0c\u4f46\u4e5f\u53ef\u80fd\u8ba9\u5c11\u6570\u771f\u5b9e\u7684\u957f\u95f4\u9694\u91cd\u6355\u7a0d\u5fae\u6162\u4e00\u70b9\u3002",
            )
        else:
            current_post = config["resolved"].get("postprocess", {})
            patch = {
                "postprocess": {
                    "max_detected_island_length": max(1, int(current_post.get("max_detected_island_length", 2))),
                    "low_confidence_threshold": round(
                        min(0.5, float(current_post.get("low_confidence_threshold", 0.40)) + 0.02),
                        2,
                    ),
                }
            }
            output_slug = "post_cleanup_tightening"
            title = _localized_text(language, en="Post-Cleanup Tightening", zh="\u6e05\u6d17\u9636\u6bb5\u6536\u7d27")
            diagnosis = _localized_text(
                language,
                en="Tracking is already strong enough that cleanup is a safer place to shave visible noise.",
                zh="\u8ddf\u8e2a\u4e3b\u4f53\u5df2\u7ecf\u8db3\u591f\u7a33\uff0c\u5148\u5728 cleanup \u73af\u8282\u53bb\u6389\u53ef\u89c1\u566a\u58f0\u4f1a\u66f4\u5b89\u5168\u3002",
            )
            recommendation = _localized_text(
                language,
                en="Prefer small cleanup threshold changes before touching detector or tracker behavior.",
                zh="\u5148\u8c03\u5c0f cleanup \u9608\u503c\uff0c\u5c3d\u91cf\u4e0d\u8981\u5148\u52a8 detector \u6216 tracker \u884c\u4e3a\u3002",
            )
            expected_tradeoff = _localized_text(
                language,
                en="A stricter cleanup pass may hide a few borderline true detections along with short noise islands.",
                zh="\u66f4\u4e25\u7684 cleanup \u53ef\u80fd\u4f1a\u5728\u538b\u6389\u77ed\u566a\u58f0\u6bb5\u7684\u540c\u65f6\uff0c\u4e5f\u85cf\u6389\u5c11\u91cf\u8fb9\u7f18\u771f\u5b9e\u68c0\u6d4b\u3002",
            )

        output_name_suggestion = f"{Path(config_name).stem}_{output_slug}"

        return {
            "title": title,
            "diagnosis": diagnosis,
            "recommendation": recommendation,
            "expected_tradeoff": expected_tradeoff,
            "patch": patch,
            "patch_preview": _flatten_patch_lines(patch),
            "evidence": evidence,
            "output_name_suggestion": output_name_suggestion,
        }

    def _ai_explain_with_model(
        self,
        run_id: str | None,
        config_name: str | None,
        focus: str | None,
        language: str,
    ) -> dict[str, Any]:
        payload = self._build_ai_context(run_id=run_id, config_name=config_name, focus=focus, language=language)
        prompt = json.dumps(payload, ensure_ascii=False, indent=2)
        language_instruction = _localized_text(
            language,
            en="Write all human-readable output in English.",
            zh="Write all human-readable output in Simplified Chinese.",
        )
        instructions = (
            "You are helping operate a football tracking system. "
            "Return strict JSON with keys: summary (string), evidence (array of short strings). "
            "When a config is present, evidence must explain config entries path-by-path in the form "
            "'path = value - meaning and operational impact'. Cover the supplied raw config entries, not just a summary. "
            "Ground every sentence in the provided evidence. "
            "Do not invent artifacts, files, or metrics. "
            f"{language_instruction}"
        )
        response = self.ai_client.create_json_response(
            instructions=instructions,
            prompt=prompt,
            temperature=0.1,
        )
        return {
            "summary": str(response.get("summary", "")),
            "evidence": [str(item) for item in response.get("evidence", []) if str(item).strip()],
        }

    def _ai_recommend_with_model(self, run_id: str, objective: str | None, language: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        config_name = run.get("config_name")
        if not config_name:
            raise FileNotFoundError(f"Run {run_id} is not linked to a config.")

        payload = self._build_ai_context(run_id=run_id, config_name=config_name, focus=objective, language=language)
        prompt = json.dumps(payload, ensure_ascii=False, indent=2)
        language_instruction = _localized_text(
            language,
            en="Write all human-readable fields in English. Keep patch keys and patch_preview paths in code-style English.",
            zh="Write all human-readable fields in Simplified Chinese. Keep patch keys and patch_preview paths in code-style English.",
        )
        instructions = (
            "You are recommending the next config adjustment for a football tracking pipeline. "
            "Return strict JSON with keys: title, diagnosis, recommendation, expected_tradeoff, patch, patch_preview, evidence, output_name_suggestion. "
            "The patch must be a nested object suitable for YAML merge. "
            "Only touch conservative operator-facing parameters in follow_cam, postprocess, scene_bias.dynamic_air_recovery, selection, or tracking. "
            "Do not suggest destructive changes. "
            "Patch preview must be a flat array of 'path: value' strings matching the patch object. "
            "output_name_suggestion must be a short lowercase ASCII slug. "
            f"{language_instruction}"
        )
        response = self.ai_client.create_json_response(
            instructions=instructions,
            prompt=prompt,
            temperature=0.2,
        )
        patch = response.get("patch", {})
        if not isinstance(patch, dict):
            patch = {}
        patch_preview = response.get("patch_preview", [])
        if not isinstance(patch_preview, list) or not patch_preview:
            patch_preview = _flatten_patch_lines(patch)
        output_name_suggestion = str(
            response.get("output_name_suggestion")
            or f"{Path(config_name).stem}_{self._slugify(objective or 'ai_update')}"
        )
        return {
            "title": str(response.get("title", "Model Recommendation")),
            "diagnosis": str(response.get("diagnosis", "")),
            "recommendation": str(response.get("recommendation", "")),
            "expected_tradeoff": str(response.get("expected_tradeoff", "")),
            "patch": patch,
            "patch_preview": [str(item) for item in patch_preview],
            "evidence": [str(item) for item in response.get("evidence", []) if str(item).strip()],
            "output_name_suggestion": output_name_suggestion,
        }

    def ai_config_diff(
        self, base_config_name: str, patch: dict[str, Any], output_name: str | None = None
    ) -> dict[str, Any]:
        resolved_output_name = output_name or f"{Path(base_config_name).stem}_ai_patch"
        return {
            "base_config_name": base_config_name,
            "output_name": resolved_output_name,
            "patch": patch,
            "patch_preview": _flatten_patch_lines(patch),
        }

    def _build_ai_context(
        self,
        run_id: str | None,
        config_name: str | None,
        focus: str | None,
        language: str,
    ) -> dict[str, Any]:
        context: dict[str, Any] = {"focus": focus, "response_language": language}

        if config_name:
            config = self.get_config(config_name)
            context["config"] = {
                "name": config["name"],
                "summary": config["summary"],
                "raw": config["raw"],
                "resolved": {
                    "postprocess": config["resolved"].get("postprocess", {}),
                    "follow_cam": config["resolved"].get("follow_cam", {}),
                    "scene_bias": config["resolved"].get("scene_bias", {}),
                    "selection": config["resolved"].get("selection", {}),
                    "tracking": config["resolved"].get("tracking", {}),
                },
            }

        if run_id:
            run = self.get_run(run_id)
            cleanup = run.get("stats", {}).get("cleanup", {}) or {}
            follow_cam = run.get("stats", {}).get("follow_cam", {}) or {}
            context["run"] = {
                "run_id": run["run_id"],
                "status": run["status"],
                "config_name": run.get("config_name"),
                "modules_enabled": run.get("modules_enabled", {}),
                "raw_stats": run.get("stats", {}).get("raw", {}),
                "cleaned_stats": run.get("stats", {}).get("cleaned", {}),
                "cleanup_summary": {
                    "scrubbed_frame_count": cleanup.get("scrubbed_frame_count"),
                    "scrubbed_segment_count": cleanup.get("scrubbed_segment_count"),
                    "actions_preview": (cleanup.get("actions") or [])[:5],
                },
                "follow_cam_summary": {
                    "track_source": follow_cam.get("track_source"),
                    "target_resolution": follow_cam.get("target_resolution"),
                    "mean_crop_height": follow_cam.get("mean_crop_height"),
                    "min_crop_height": follow_cam.get("min_crop_height"),
                    "max_crop_height": follow_cam.get("max_crop_height"),
                    "status_counts": follow_cam.get("status_counts"),
                },
            }
        return context

    def create_run(self, request: dict[str, Any]) -> dict[str, Any]:
        self._assert_service_open()
        pipeline_mode = str(request.get("pipeline_mode") or "standard")
        machine_note = self._machine_run_note(request.get("notes"))
        is_production_trial = machine_note is not None and machine_note.get("purpose") == "production_trial"
        if is_production_trial and pipeline_mode != "standard":
            raise ValueError("production_trial requires pipeline_mode=standard")
        broadcast_preflight = None
        if pipeline_mode == "broadcast_hybrid":
            broadcast_preflight = self._preflight_broadcast_request(request)
        if self._is_approved_child_run_request(request):
            if pipeline_mode != "standard":
                raise ValueError("approved child recovery cannot be combined with broadcast_hybrid")
            return self._create_approved_child_run(request)
        if not request.get("config_name"):
            raise ValueError("Create run requires config_name.")

        requested_output_name = request.get("output_dir_name")
        run_id = Path(requested_output_name).name if requested_output_name else ""
        if not run_id:
            run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"

        config_path, relative_name = self._resolve_config_path(request["config_name"])
        raw_config_patch = request.get("config_patch")
        config_patch = raw_config_patch if raw_config_patch is not None else {}
        base_config_snapshot: _YamlConfigSnapshot | None = None
        if is_production_trial:
            base_config_snapshot = self._capture_yaml_config_snapshot(config_path)
            base_config = load_config(
                config_path,
                raw_config=base_config_snapshot.raw,
            )
            config_patch = normalize_production_trial_config_patch(
                config_patch,
                base_config=_jsonable(base_config),
                legacy_created_at=_utc_now_iso(),
            )
            config_patch, preflight_output_dir = self._prepare_production_trial_config_patch(
                request=request,
                note=machine_note,
                patch=config_patch,
                base_config=base_config,
                base_config_name=relative_name,
                base_config_sha256=base_config_snapshot.sha256,
                run_id=run_id,
            )
            if preflight_output_dir.exists() and any(preflight_output_dir.iterdir()):
                raise FileExistsError(str(preflight_output_dir))
        output_created = False
        materialized_config_path: Path | None = None
        materialized_config_snapshot: _YamlConfigSnapshot | None = None
        config: AppConfig | None = None
        run_record: dict[str, Any] | None = None
        try:
            # Hold both the in-process lock and the cross-process registry lock
            # from collision preflight through registration. No generated YAML
            # or output directory can be created by a losing concurrent request.
            with self._lock:
                self._assert_service_open_locked()
                with self._registry_file_lock():
                    registry = self._read_registry()
                    if is_production_trial:
                        assert machine_note is not None
                        assert base_config_snapshot is not None
                        self._validate_production_trial_parent_lineage(
                            request=request,
                            note=machine_note,
                            patch=config_patch,
                            base_config_snapshot=base_config_snapshot,
                            registry=registry,
                        )
                    active = next(
                        (item for item in registry["runs"] if item.get("status") in {"queued", "running"}),
                        None,
                    )
                    if active is not None:
                        raise RuntimeError(f"Another run is already active: {active.get('run_id')}")

                    if config_patch:
                        materialized_config = self._materialize_run_config(
                            base_config_path=config_path,
                            base_config_name=relative_name,
                            run_id=run_id,
                            patch=config_patch,
                            suffix="field_setup",
                            exclusive=is_production_trial,
                            base_config_snapshot=base_config_snapshot,
                        )
                        config_path, relative_name, materialized_config_snapshot = materialized_config
                        if is_production_trial:
                            materialized_config_path = config_path
                    config = load_config(
                        config_path,
                        raw_config=(
                            materialized_config_snapshot.raw if materialized_config_snapshot is not None else None
                        ),
                    )

                    if not is_production_trial:
                        if request.get("input_video"):
                            config.input_video = Path(request["input_video"]).resolve()
                        if request.get("enable_postprocess") is not None:
                            config.postprocess.enabled = bool(request["enable_postprocess"])
                        if request.get("enable_follow_cam") is not None:
                            config.follow_cam.enabled = bool(request["enable_follow_cam"])
                        if request.get("start_frame") is not None:
                            config.runtime.start_frame = int(request["start_frame"])
                        if request.get("max_frames") is not None:
                            config.runtime.max_frames = int(request["max_frames"])
                    if pipeline_mode == "broadcast_hybrid":
                        # The hybrid workflow publishes its own audited camera generation and render.
                        # Running the legacy follow-cam here would create a second, misleading deliverable.
                        config.follow_cam.enabled = False

                    expected_output_dir = self._build_run_output_dir(run_id=run_id, input_video=config.input_video)
                    if is_production_trial and config.output_dir.resolve() != expected_output_dir.resolve():
                        raise ValueError("production_trial persisted output_dir does not match effective run")
                    config.output_dir = expected_output_dir
                    if config.output_dir.exists() and any(config.output_dir.iterdir()):
                        raise FileExistsError(str(config.output_dir))
                    output_created = not config.output_dir.exists()
                    config.output_dir.mkdir(parents=True, exist_ok=True)

                    if is_production_trial:
                        assert materialized_config_snapshot is not None
                        self._verify_materialized_config_snapshot(materialized_config_snapshot)

                    run_record = {
                        "run_id": run_id,
                        "source": pipeline_mode if pipeline_mode == "broadcast_hybrid" else "api",
                        "status": "queued",
                        "created_at": _utc_now_iso(),
                        "started_at": None,
                        "completed_at": None,
                        "config_name": relative_name,
                        "config_path": str(config_path),
                        "config_sha256": (materialized_config_snapshot.sha256 if is_production_trial else None),
                        "input_video": str(config.input_video),
                        "parent_run_id": request.get("parent_run_id"),
                        "output_dir": str(config.output_dir),
                        "modules_enabled": {
                            "postprocess": bool(config.postprocess.enabled),
                            "follow_cam": bool(config.follow_cam.enabled),
                            "temporal_chunks": bool(config.temporal_chunks.enabled),
                        },
                        "artifacts": [],
                        "stats": {},
                        "broadcast": (
                            {
                                "status": "tracking",
                                "quality_profile": "stable_broadcast",
                                "max_manual_review_windows": int(request.get("max_manual_review_windows") or 30),
                                "preflight": broadcast_preflight,
                                "owner_pid": os.getpid(),
                                "owner_instance_id": self._instance_id,
                            }
                            if broadcast_preflight is not None
                            else {}
                        ),
                        "progress": self._initial_progress(),
                        "notes": request.get("notes"),
                        "error": None,
                    }
                    self._attach_ai_candidate_lifecycle(run_record)
                    cancel_event = threading.Event()
                    thread = threading.Thread(
                        target=self._execute_run,
                        args=(run_id, config, cancel_event, run_record["source"]),
                        name=f"football-tracking-run-{run_id}",
                        daemon=True,
                    )
                    registry["runs"] = [run for run in registry["runs"] if run["run_id"] != run_id]
                    registry["runs"].append(run_record)
                    self._write_registry_under_file_lock(registry)
                    self._active_threads[run_id] = thread
                    self._cancel_events[run_id] = cancel_event
        except BaseException:
            if output_created and config is not None:
                shutil.rmtree(config.output_dir, ignore_errors=True)
            if materialized_config_path is not None:
                materialized_config_path.unlink(missing_ok=True)
            raise
        assert config is not None and run_record is not None
        try:
            self._start_thread_or_cleanup(
                run_id,
                thread,
                output_dir=config.output_dir,
                remove_output=output_created,
            )
        except BaseException:
            if materialized_config_path is not None:
                materialized_config_path.unlink(missing_ok=True)
            raise
        return run_record

    def _is_approved_child_run_request(self, request: dict[str, Any]) -> bool:
        approved_ids = [str(item).strip() for item in request.get("approved_action_ids") or [] if str(item).strip()]
        artifact_name = str(request.get("approved_actions_artifact_name") or "").strip()
        return bool(approved_ids or artifact_name)

    @staticmethod
    def _machine_run_note(notes: Any) -> dict[str, Any] | None:
        if not isinstance(notes, str) or not notes.strip():
            return None
        try:
            parsed = json.loads(notes)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def _prepare_production_trial_config_patch(
        self,
        *,
        request: dict[str, Any],
        note: dict[str, Any],
        patch: dict[str, Any],
        base_config: AppConfig,
        base_config_name: str,
        base_config_sha256: str,
        run_id: str,
    ) -> tuple[dict[str, Any], Path]:
        """Bind every runtime override to the one YAML that will be executed."""

        prepared = deepcopy(patch)
        workflow = _value_at_dotted_path(prepared, "metadata.production_workflow")
        if workflow is _MISSING_VALUE:
            workflow = {}
        if not isinstance(workflow, dict):
            raise ValueError("metadata.production_workflow must be an object")

        for key in sorted(set(note).intersection(workflow)):
            if _jsonable(note[key]) != _jsonable(workflow[key]):
                raise ValueError(f"production_trial workflow metadata conflict: {key}")

        base_config_lineage = {
            "name": base_config_name,
            "sha256": base_config_sha256,
        }
        provided_base_lineage = workflow.get("base_config_lineage")
        if provided_base_lineage is not None and provided_base_lineage != base_config_lineage:
            raise ValueError("production_trial workflow metadata conflict: base_config_lineage")
        workflow["base_config_lineage"] = base_config_lineage
        _set_dotted_path(prepared, "metadata.production_workflow", workflow)

        workflow_output_name = workflow.get("output_dir_name")
        if workflow_output_name is not None and workflow_output_name != run_id:
            raise ValueError("production_trial workflow metadata conflict: output_dir_name")
        note_output_id = note.get("output_id")
        if isinstance(note_output_id, str) and note_output_id.strip():
            if run_id != f"production_trial_{note_output_id.strip()}":
                raise ValueError("production_trial workflow metadata conflict: output_id")

        binding_specs: tuple[tuple[str, str, str, Any], ...] = (
            ("input_video", "input_video", "source_path", str(base_config.input_video)),
            (
                "postprocess.enabled",
                "enable_postprocess",
                "enable_postprocess",
                base_config.postprocess.enabled,
            ),
            (
                "follow_cam.enabled",
                "enable_follow_cam",
                "enable_follow_cam",
                base_config.follow_cam.enabled,
            ),
            ("runtime.start_frame", "start_frame", "start_frame", base_config.runtime.start_frame),
            ("runtime.max_frames", "max_frames", "max_frames", base_config.runtime.max_frames),
        )

        effective: dict[str, Any] = {}
        for config_path, request_key, note_key, fallback in binding_specs:
            candidates: list[Any] = []
            if request_key in request and request[request_key] is not None:
                candidates.append(request[request_key])
            patch_value = _value_at_dotted_path(prepared, config_path)
            if patch_value is not _MISSING_VALUE:
                candidates.append(patch_value)
            if note_key in note:
                candidates.append(note[note_key])
            if note_key in workflow:
                candidates.append(workflow[note_key])
            if config_path == "input_video":
                source_signature = workflow.get("source_signature")
                if isinstance(source_signature, dict) and "path" in source_signature:
                    candidates.append(source_signature["path"])
            normalized = [self._normalize_production_trial_runtime_binding(config_path, value) for value in candidates]
            if normalized and any(value != normalized[0] for value in normalized[1:]):
                raise ValueError(f"production_trial runtime binding conflict: {config_path}")
            effective[config_path] = (
                normalized[0] if normalized else self._normalize_production_trial_runtime_binding(config_path, fallback)
            )

        input_video = Path(effective["input_video"])
        source_signature = workflow.get("source_signature")
        if source_signature is not None:
            try:
                source_stat = input_video.stat()
            except OSError as exc:
                raise ValueError("production_trial source signature cannot be verified") from exc
            actual_source_signature = {
                "path": str(input_video),
                "size_bytes": source_stat.st_size,
                "modified_at": datetime.fromtimestamp(
                    source_stat.st_mtime,
                    tz=timezone.utc,
                ).isoformat(),
            }
            if source_signature != actual_source_signature:
                raise ValueError("production_trial source signature is stale or invalid")
        output_dir = self._build_run_output_dir(run_id=run_id, input_video=input_video)
        for path, value in effective.items():
            _set_dotted_path(prepared, path, value)
        prepared["output_dir"] = str(output_dir)
        return prepared, output_dir

    @staticmethod
    def _normalize_production_trial_runtime_binding(path: str, value: Any) -> Any:
        if path == "input_video":
            if not isinstance(value, (str, Path)) or not str(value).strip():
                raise ValueError("production_trial runtime binding input_video must be a path")
            return str(Path(value).resolve())
        if path in {"postprocess.enabled", "follow_cam.enabled"}:
            if not isinstance(value, bool):
                raise ValueError(f"production_trial runtime binding {path} must be boolean")
            return value
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"production_trial runtime binding {path} must be an integer")
        if path == "runtime.start_frame" and value < 0:
            raise ValueError("production_trial runtime binding runtime.start_frame must be non-negative")
        if path == "runtime.max_frames" and value <= 0:
            raise ValueError("production_trial runtime binding runtime.max_frames must be positive")
        return value

    @staticmethod
    def _validate_production_trial_note_contract(note: dict[str, Any]) -> None:
        required_strings = (
            "workflow_id",
            "submission_id",
            "output_id",
        )
        if note.get("schema_version") != "1.0" or note.get("purpose") != "production_trial":
            raise ValueError("production_trial note contract is incomplete")
        if any(not isinstance(note.get(key), str) or not str(note[key]).strip() for key in required_strings):
            raise ValueError("production_trial note contract is incomplete")
        for key in ("calibration_digest", "intent_sha256"):
            value = note.get(key)
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError("production_trial note contract is incomplete")
        generation = note.get("generation")
        start_frame = note.get("start_frame")
        max_frames = note.get("max_frames")
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
            or isinstance(start_frame, bool)
            or not isinstance(start_frame, int)
            or start_frame < 0
            or isinstance(max_frames, bool)
            or not isinstance(max_frames, int)
            or max_frames <= 0
            or not isinstance(note.get("enable_postprocess"), bool)
            or not isinstance(note.get("enable_follow_cam"), bool)
        ):
            raise ValueError("production_trial note contract is incomplete")
        legacy_restart_run_id = note.get("legacy_restart_run_id")
        if legacy_restart_run_id is not None and (
            not isinstance(legacy_restart_run_id, str) or not legacy_restart_run_id.strip()
        ):
            raise ValueError("production_trial legacy_restart_run_id is invalid")

    def _validate_production_trial_parent_lineage(
        self,
        *,
        request: dict[str, Any],
        note: dict[str, Any],
        patch: dict[str, Any],
        base_config_snapshot: _YamlConfigSnapshot,
        registry: dict[str, Any],
    ) -> None:
        self._validate_production_trial_note_contract(note)
        parent_run_id = str(request.get("parent_run_id") or "").strip()
        current_tuning = _value_at_dotted_path(patch, "metadata.production_tuning")
        if current_tuning is _MISSING_VALUE:
            current_tuning = None
        current_workflow = _value_at_dotted_path(patch, "metadata.production_workflow")
        current_signature = current_workflow.get("source_signature") if isinstance(current_workflow, dict) else None
        if not isinstance(current_workflow, dict) or not isinstance(current_signature, dict):
            raise ValueError("production_trial source identity is unavailable")
        current_input = _value_at_dotted_path(patch, "input_video")
        if not isinstance(current_input, str):
            raise ValueError("production_trial source identity is unavailable")
        if not parent_run_id:
            if note.get("generation") != 1:
                raise ValueError("production_trial root generation must be 1")
            workflow_id = note["workflow_id"]
            workflow_runs: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for candidate in registry.get("runs", []):
                candidate_note = self._machine_run_note(candidate.get("notes"))
                if (
                    isinstance(candidate_note, dict)
                    and candidate_note.get("purpose") == "production_trial"
                    and candidate_note.get("workflow_id") == workflow_id
                ):
                    workflow_runs.append((candidate, candidate_note))
            if any(candidate.get("config_sha256") is not None for candidate, _ in workflow_runs):
                raise ValueError("production_trial existing workflow requires parent_run_id")

            legacy_restart_run_id = note.get("legacy_restart_run_id")
            legacy_runs = [candidate for candidate, _ in workflow_runs if candidate.get("config_sha256") is None]
            if legacy_runs and legacy_restart_run_id is None:
                raise ValueError("production_trial legacy restart must be explicit")
            if legacy_restart_run_id is None:
                return
            legacy = next(
                (
                    candidate
                    for candidate in registry.get("runs", [])
                    if candidate.get("run_id") == legacy_restart_run_id
                ),
                None,
            )
            if legacy is None:
                raise ValueError("production_trial legacy restart target does not exist")
            if legacy.get("config_sha256") is not None:
                raise ValueError("production_trial legacy restart target has an immutable config digest")
            if legacy.get("status") not in {"completed", "failed", "cancelled"}:
                raise ValueError("production_trial legacy restart target must be terminal")
            legacy_note = self._machine_run_note(legacy.get("notes"))
            if (
                not isinstance(legacy_note, dict)
                or legacy_note.get("purpose") != "production_trial"
                or legacy_note.get("workflow_id") != workflow_id
            ):
                raise ValueError("production_trial legacy restart workflow does not match")
            self._validate_legacy_restart_topology(
                workflow_runs=workflow_runs,
                target_run_id=legacy_restart_run_id,
            )
            if legacy_note.get("calibration_digest") != note.get("calibration_digest"):
                raise ValueError("production_trial legacy restart calibration does not match")
            legacy_input = legacy.get("input_video")
            if not isinstance(legacy_input, str) or Path(legacy_input).resolve() != Path(current_input).resolve():
                raise ValueError("production_trial legacy restart source does not match")
            legacy_config_name = legacy.get("config_name")
            if not isinstance(legacy_config_name, str) or not legacy_config_name:
                raise ValueError("production_trial legacy restart source identity is unavailable")
            try:
                legacy_config_path, _ = self._resolve_config_path(legacy_config_name)
                legacy_config_snapshot = self._capture_yaml_config_snapshot(legacy_config_path)
                legacy_raw = legacy_config_snapshot.raw
                self._verify_source_config_snapshot(
                    legacy_config_snapshot,
                    error="production_trial legacy restart source identity is unavailable",
                )
            except (FileNotFoundError, ValueError, RuntimeError) as exc:
                raise ValueError("production_trial legacy restart source identity is unavailable") from exc
            legacy_workflow = _value_at_dotted_path(legacy_raw, "metadata.production_workflow")
            legacy_signature = legacy_workflow.get("source_signature") if isinstance(legacy_workflow, dict) else None
            if not isinstance(legacy_signature, dict) or legacy_signature != current_signature:
                raise ValueError("production_trial legacy restart source does not match")
            legacy_base_lineage = (
                legacy_workflow.get("base_config_lineage") if isinstance(legacy_workflow, dict) else None
            )
            if legacy_base_lineage is not None and (
                not isinstance(legacy_base_lineage, dict)
                or legacy_base_lineage != current_workflow.get("base_config_lineage")
            ):
                raise ValueError("production_trial legacy restart base config lineage does not match")
            return

        parent = next(
            (item for item in registry.get("runs", []) if item.get("run_id") == parent_run_id),
            None,
        )
        if parent is None:
            raise ValueError("production_trial parent run does not exist")
        if parent.get("status") not in {"completed", "failed", "cancelled"}:
            raise ValueError("production_trial parent run must be terminal")
        if parent.get("source") != "api":
            raise ValueError("production_trial parent run has the wrong source")
        parent_note = self._machine_run_note(parent.get("notes"))
        if not isinstance(parent_note, dict) or parent_note.get("purpose") != "production_trial":
            raise ValueError("production_trial parent run has the wrong purpose")
        if note.get("legacy_restart_run_id") is not None:
            raise ValueError("production_trial child cannot declare legacy_restart_run_id")

        generation = note.get("generation")
        parent_generation = parent_note.get("generation")
        if (
            note.get("schema_version") != "1.0"
            or parent_note.get("schema_version") != "1.0"
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or isinstance(parent_generation, bool)
            or not isinstance(parent_generation, int)
            or generation != parent_generation + 1
        ):
            raise ValueError("production_trial parent generation does not match")
        calibration_digest = note.get("calibration_digest")
        if (
            not isinstance(calibration_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", calibration_digest) is None
            or calibration_digest != parent_note.get("calibration_digest")
        ):
            raise ValueError("production_trial parent calibration does not match")
        parent_output_id = parent_note.get("output_id")
        if (
            not isinstance(parent_output_id, str)
            or not parent_output_id.strip()
            or parent_run_id != f"production_trial_{parent_output_id}"
        ):
            raise ValueError("production_trial parent identity does not match")
        existing_child = next(
            (item for item in registry.get("runs", []) if item.get("parent_run_id") == parent_run_id),
            None,
        )
        if existing_child is not None:
            raise ValueError("production_trial parent run already has a child")

        workflow_id = note.get("workflow_id")
        parent_workflow_id = parent_note.get("workflow_id")
        if not isinstance(workflow_id, str) or not workflow_id.strip() or workflow_id != parent_workflow_id:
            raise ValueError("production_trial parent workflow does not match")

        parent_input = parent.get("input_video")
        if (
            not isinstance(current_input, str)
            or not isinstance(parent_input, str)
            or Path(current_input).resolve() != Path(parent_input).resolve()
        ):
            raise ValueError("production_trial parent source does not match")

        parent_config_name = parent.get("config_name")
        if not isinstance(parent_config_name, str) or not parent_config_name:
            raise ValueError("production_trial parent config is unavailable")
        try:
            parent_config_path, _ = self._resolve_config_path(parent_config_name)
            parent_config_snapshot = self._capture_yaml_config_snapshot(parent_config_path)
            parent_config_sha256 = parent.get("config_sha256")
            if (
                not isinstance(parent_config_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", parent_config_sha256) is None
                or parent_config_snapshot.sha256 != parent_config_sha256
            ):
                raise ValueError("production_trial parent config digest does not match")
            parent_raw = parent_config_snapshot.raw
            self._verify_source_config_snapshot(
                parent_config_snapshot,
                error="production_trial parent config digest does not match",
            )
        except ValueError as exc:
            if str(exc) == "production_trial parent config digest does not match":
                raise
            raise ValueError("production_trial parent config is unavailable") from exc
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            raise ValueError("production_trial parent config is unavailable") from exc

        parent_workflow = _value_at_dotted_path(parent_raw, "metadata.production_workflow")
        current_base_lineage = current_workflow.get("base_config_lineage")
        parent_base_lineage = parent_workflow.get("base_config_lineage") if isinstance(parent_workflow, dict) else None
        if not isinstance(current_base_lineage, dict) or current_base_lineage != parent_base_lineage:
            raise ValueError("production_trial parent base config lineage does not match")
        parent_signature = parent_workflow.get("source_signature") if isinstance(parent_workflow, dict) else None
        if not isinstance(current_signature, dict) or not isinstance(parent_signature, dict):
            raise ValueError("production_trial parent source identity is unavailable")
        if current_signature != parent_signature:
            raise ValueError("production_trial parent source does not match")

        current_raw = _deep_merge(base_config_snapshot.raw, patch)
        for protected_path in (
            "filtering.roi",
            "scene_bias.enabled",
            "scene_bias.ground_zones",
            "scene_bias.negative_rois",
        ):
            if _value_at_dotted_path(current_raw, protected_path) != _value_at_dotted_path(
                parent_raw,
                protected_path,
            ):
                raise ValueError(f"production_trial parent calibration geometry does not match: {protected_path}")

        parent_tuning = _value_at_dotted_path(parent_raw, "metadata.production_tuning")
        if parent_tuning is _MISSING_VALUE:
            parent_tuning = None
        if current_tuning == parent_tuning:
            return
        if parent_tuning is None:
            if isinstance(current_tuning, dict) and (
                current_tuning.get("parent_version_id") is None and current_tuning.get("history") == []
            ):
                return
            raise ValueError("production_trial tuning history must append parent")
        if not isinstance(parent_tuning, dict) or not isinstance(current_tuning, dict):
            raise ValueError("production_trial tuning history must append parent")
        parent_snapshot = {
            "version_id": parent_tuning.get("version_id"),
            "created_at": parent_tuning.get("created_at"),
            "values_sha256": parent_tuning.get("values_sha256"),
            "values": parent_tuning.get("values"),
        }
        expected_history = [*(parent_tuning.get("history") or []), parent_snapshot]
        current_history = current_tuning.get("history")
        if (
            not isinstance(current_history, list)
            or len(current_history) < len(expected_history)
            or current_history[: len(expected_history)] != expected_history
            or not current_history
            or current_tuning.get("parent_version_id") != current_history[-1].get("version_id")
        ):
            raise ValueError("production_trial tuning history must append parent")

    def _validate_legacy_restart_topology(
        self,
        *,
        workflow_runs: list[tuple[dict[str, Any], dict[str, Any]]],
        target_run_id: str,
    ) -> None:
        error = "production_trial legacy restart target is not the unique authoritative terminal tip"
        runs_by_id: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for candidate, candidate_note in workflow_runs:
            run_id = candidate.get("run_id")
            if (
                not isinstance(run_id, str)
                or not run_id.strip()
                or run_id in runs_by_id
                or candidate.get("config_sha256") is not None
            ):
                raise ValueError(error)
            runs_by_id[run_id] = (candidate, candidate_note)
        if target_run_id not in runs_by_id:
            raise ValueError(error)

        children: dict[str, list[str]] = {run_id: [] for run_id in runs_by_id}
        roots: list[str] = []
        for run_id, (candidate, candidate_note) in runs_by_id.items():
            generation = candidate_note.get("generation")
            if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
                raise ValueError(error)
            parent_run_id = str(candidate.get("parent_run_id") or "").strip()
            if not parent_run_id:
                roots.append(run_id)
                if generation != 1:
                    raise ValueError(error)
                continue
            if parent_run_id not in runs_by_id:
                raise ValueError(error)
            parent_generation = runs_by_id[parent_run_id][1].get("generation")
            if (
                isinstance(parent_generation, bool)
                or not isinstance(parent_generation, int)
                or generation != parent_generation + 1
            ):
                raise ValueError(error)
            children[parent_run_id].append(run_id)

        if len(roots) != 1 or any(len(child_ids) > 1 for child_ids in children.values()):
            raise ValueError(error)
        leaves = [run_id for run_id, child_ids in children.items() if not child_ids]
        if len(leaves) != 1:
            raise ValueError(error)
        tip_run_id = leaves[0]
        visited: set[str] = set()
        cursor = roots[0]
        while cursor not in visited:
            visited.add(cursor)
            child_ids = children[cursor]
            if not child_ids:
                break
            cursor = child_ids[0]
        if visited != set(runs_by_id) or cursor != tip_run_id:
            raise ValueError(error)
        tip = runs_by_id[tip_run_id][0]
        if tip_run_id != target_run_id or tip.get("status") not in {
            "completed",
            "failed",
            "cancelled",
        }:
            raise ValueError(error)

    def _preflight_production_full_parent(
        self,
        request: dict[str, Any],
        note: dict[str, Any],
        *,
        config_path: Path,
        config: AppConfig,
        calibration: ActionCalibration,
        video_path: Path,
        source_stat: os.stat_result,
    ) -> None:
        required_strings = (
            "workflow_id",
            "submission_id",
            "output_id",
            "accepted_trial_run_id",
            "confirmed_config_name",
        )
        required_hashes = (
            "accepted_trial_request_sha256",
            "expected_config_sha256",
            "config_patch_sha256",
            "calibration_digest",
        )
        if note.get("schema_version") != "1.0" or any(
            not isinstance(note.get(name), str) or not str(note[name]).strip() for name in required_strings
        ):
            raise ValueError("production_full notes do not match schema version 1.0")
        if any(
            not isinstance(note.get(name), str) or re.fullmatch(r"[0-9a-f]{64}", str(note[name])) is None
            for name in required_hashes
        ):
            raise ValueError("production_full notes require lowercase SHA-256 lineage fields")
        generation = note.get("generation")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise ValueError("production_full notes require a positive generation")
        source_signature = note.get("source_signature")
        if (
            not isinstance(source_signature, dict)
            or not isinstance(source_signature.get("path"), str)
            or isinstance(source_signature.get("size_bytes"), bool)
            or not isinstance(source_signature.get("size_bytes"), int)
            or source_signature["size_bytes"] < 0
            or not isinstance(source_signature.get("modified_at"), str)
            or not source_signature["modified_at"].strip()
        ):
            raise ValueError("production_full notes require a valid source_signature")
        expected_output_name = f"production_full_{note['output_id']}"
        if request.get("output_dir_name") != expected_output_name:
            raise ValueError(f"production_full output_dir_name must be {expected_output_name}")
        if request.get("config_patch"):
            raise ValueError("production_full requires a confirmed config without config_patch")
        if request.get("config_name") != note["confirmed_config_name"]:
            raise ValueError("production_full confirmed_config_name does not match config_name")
        request_start_frame = request.get("start_frame")
        if (
            isinstance(request_start_frame, bool)
            or not isinstance(request_start_frame, int)
            or request_start_frame != 0
        ):
            raise ValueError("production_full requires request start_frame=0")
        if "max_frames" not in request or request.get("max_frames") is not None:
            raise ValueError("production_full requires request max_frames=null")
        if request.get("enable_follow_cam") is not False:
            raise ValueError("production_full requires request enable_follow_cam=false")
        if (
            not isinstance(request.get("enable_postprocess"), bool)
            or request.get("enable_postprocess") is not config.postprocess.enabled
        ):
            raise ValueError("production_full enable_postprocess must match the confirmed configuration")

        confirmed_raw = self._load_raw_yaml(config_path)
        runtime_raw = confirmed_raw.get("runtime")
        follow_cam_raw = confirmed_raw.get("follow_cam")
        output_raw = confirmed_raw.get("output")
        if (
            not isinstance(runtime_raw, dict)
            or isinstance(runtime_raw.get("start_frame"), bool)
            or not isinstance(runtime_raw.get("start_frame"), int)
            or runtime_raw.get("start_frame") != 0
            or "max_frames" not in runtime_raw
            or runtime_raw.get("max_frames") is not None
            or not isinstance(follow_cam_raw, dict)
            or follow_cam_raw.get("enabled") is not False
            or not isinstance(output_raw, dict)
            or output_raw.get("save_tracking_contract") is not True
        ):
            raise RuntimeError("Confirmed production configuration execution invariants are invalid")

        def normalized_polygon(value: Any) -> list[list[float]] | None:
            if not isinstance(value, list):
                return None
            normalized: list[list[float]] = []
            for point in value:
                if (
                    not isinstance(point, (list, tuple))
                    or len(point) != 2
                    or any(isinstance(coordinate, bool) for coordinate in point)
                    or any(not isinstance(coordinate, (int, float)) for coordinate in point)
                    or any(not np.isfinite(float(coordinate)) for coordinate in point)
                ):
                    return None
                normalized.append([float(point[0]), float(point[1])])
            return normalized

        expected_field_polygon = [[float(x), float(y)] for x, y in calibration.field_polygon]
        expected_exclusions = [[[float(x), float(y)] for x, y in polygon] for polygon in calibration.exclusion_polygons]
        filtering_raw = confirmed_raw.get("filtering")
        scene_bias_raw = confirmed_raw.get("scene_bias")
        ground_zones = scene_bias_raw.get("ground_zones") if isinstance(scene_bias_raw, dict) else None
        negative_rois = scene_bias_raw.get("negative_rois") if isinstance(scene_bias_raw, dict) else None
        production_zones = (
            [zone for zone in ground_zones if isinstance(zone, dict) and zone.get("name") == "production_field"]
            if isinstance(ground_zones, list)
            else []
        )
        expected_roi = [
            min(point[0] for point in expected_field_polygon),
            min(point[1] for point in expected_field_polygon),
            max(point[0] for point in expected_field_polygon),
            max(point[1] for point in expected_field_polygon),
        ]
        raw_roi = filtering_raw.get("roi") if isinstance(filtering_raw, dict) else None
        normalized_roi = (
            [float(value) for value in raw_roi]
            if isinstance(raw_roi, list)
            and len(raw_roi) == 4
            and all(not isinstance(value, bool) and isinstance(value, (int, float)) for value in raw_roi)
            and all(np.isfinite(float(value)) for value in raw_roi)
            else None
        )
        geometry_matches = (
            isinstance(scene_bias_raw, dict)
            and scene_bias_raw.get("enabled") is True
            and isinstance(ground_zones, list)
            and len(ground_zones) == 1
            and len(production_zones) == 1
            and set(production_zones[0]) == {"name", "points"}
            and normalized_polygon(production_zones[0].get("points")) == expected_field_polygon
            and isinstance(negative_rois, list)
            and len(negative_rois) == len(expected_exclusions)
            and all(
                isinstance(zone, dict)
                and set(zone) == {"name", "points"}
                and zone.get("name") == f"production_exclusion_{index + 1}"
                and normalized_polygon(zone.get("points")) == expected_polygon
                for index, (zone, expected_polygon) in enumerate(zip(negative_rois, expected_exclusions, strict=True))
            )
            and normalized_roi == expected_roi
        )
        if not geometry_matches:
            raise RuntimeError("Confirmed production configuration geometry does not match calibration_confirmation")

        parent_run_id = request.get("parent_run_id")
        if not isinstance(parent_run_id, str) or not parent_run_id.strip():
            raise ValueError("production_full requires parent_run_id")
        if parent_run_id != note["accepted_trial_run_id"]:
            raise ValueError("production_full parent_run_id must equal notes.accepted_trial_run_id")
        try:
            parent = self.get_run(parent_run_id)
        except KeyError as exc:
            raise FileNotFoundError(f"Accepted production trial run not found: {parent_run_id}") from exc
        if parent.get("status") != "completed":
            raise RuntimeError(f"Accepted production trial must be completed: {parent_run_id}")
        if parent.get("source") != "api":
            raise RuntimeError("Accepted production trial source is not a standard production trial")

        parent_note_raw = parent.get("notes")
        try:
            parent_note = json.loads(parent_note_raw) if isinstance(parent_note_raw, str) else None
        except (TypeError, ValueError):
            parent_note = None
        if not isinstance(parent_note, dict):
            raise RuntimeError("Accepted production trial lineage note is unavailable")
        parent_note_matches = (
            parent_note.get("schema_version") == "1.0"
            and parent_note.get("purpose") == "production_trial"
            and parent_note.get("workflow_id") == note["workflow_id"]
            and parent_note.get("calibration_digest") == note["calibration_digest"]
            and isinstance(parent_note.get("submission_id"), str)
            and bool(parent_note["submission_id"].strip())
            and isinstance(parent_note.get("generation"), int)
            and not isinstance(parent_note["generation"], bool)
            and parent_note["generation"] > 0
            and isinstance(parent_note.get("intent_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", parent_note["intent_sha256"]) is not None
            and isinstance(parent_note.get("output_id"), str)
            and bool(parent_note["output_id"].strip())
            and isinstance(parent_note.get("start_frame"), int)
            and not isinstance(parent_note["start_frame"], bool)
            and parent_note["start_frame"] >= 0
            and isinstance(parent_note.get("max_frames"), int)
            and not isinstance(parent_note["max_frames"], bool)
            and parent_note["max_frames"] > 0
            and isinstance(parent_note.get("enable_postprocess"), bool)
            and isinstance(parent_note.get("enable_follow_cam"), bool)
            and parent_run_id == f"production_trial_{parent_note['output_id']}"
        )
        if not parent_note_matches:
            raise RuntimeError("Accepted production trial lineage does not match production_full notes")

        actual_source_signature = {
            "path": str(video_path),
            "size_bytes": source_stat.st_size,
            "modified_at": datetime.fromtimestamp(source_stat.st_mtime, tz=timezone.utc).isoformat(),
        }
        if source_signature != actual_source_signature:
            raise RuntimeError("production_full source signature is stale or invalid")
        parent_input = parent.get("input_video")
        if not isinstance(parent_input, str) or Path(parent_input).resolve() != video_path:
            raise RuntimeError("Accepted production trial source does not match production_full source")
        if config.input_video.resolve() != video_path:
            raise RuntimeError("Confirmed production configuration source does not match production_full source")

        try:
            parent_config_path, _ = self._resolve_run_config_reference(parent)
            parent_config = self._load_raw_yaml(parent_config_path)
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError("Accepted production trial configuration evidence is unavailable") from exc
        parent_metadata = parent_config.get("metadata")
        parent_workflow = parent_metadata.get("production_workflow") if isinstance(parent_metadata, dict) else None
        if (
            not isinstance(parent_workflow, dict)
            or any(parent_workflow.get(key) != value for key, value in parent_note.items())
            or parent_workflow.get("source_signature") != source_signature
            or parent_workflow.get("output_dir_name") != parent_run_id
            or Path(str(parent_config.get("input_video") or "")).resolve() != video_path
        ):
            raise RuntimeError("Accepted production trial configuration lineage is invalid")

        config_text_sha256 = hashlib.sha256(config_path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        if config_text_sha256 != note["expected_config_sha256"]:
            raise RuntimeError("Confirmed production configuration digest does not match production_full notes")
        confirmed_metadata = confirmed_raw.get("metadata")
        confirmed_workflow = (
            confirmed_metadata.get("production_workflow") if isinstance(confirmed_metadata, dict) else None
        )
        expected_confirmed_lineage = {
            "schema_version": "1.0",
            "workflow_id": note["workflow_id"],
            "accepted_trial_run_id": parent_run_id,
            "calibration_digest": note["calibration_digest"],
            "source_signature": source_signature,
            "trial_intent_sha256": parent_note["intent_sha256"],
            "trial_request_sha256": note["accepted_trial_request_sha256"],
            "patch_sha256": note["config_patch_sha256"],
        }
        if not isinstance(confirmed_workflow, dict) or any(
            confirmed_workflow.get(key) != value for key, value in expected_confirmed_lineage.items()
        ):
            raise RuntimeError("Confirmed configuration lineage does not bind the accepted production trial")

        parent_output_raw = parent.get("output_dir")
        if not isinstance(parent_output_raw, str) or not parent_output_raw:
            raise RuntimeError("Accepted production trial diagnosis evidence is unavailable")
        try:
            parent_output = self._resolve_safe_run_output(Path(parent_output_raw))
            parent_metrics = self._read_safe_direct_json(parent_output, "metrics_report.json")
            parent_diagnosis = build_trial_diagnosis(
                parent_output,
                parent,
                metrics_report=parent_metrics,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError("Accepted production trial diagnosis evidence is unavailable") from exc
        parent_gate = parent_diagnosis.get("trial_signal_gate_v2")
        gate_profile = parent_gate.get("threshold_profile") if isinstance(parent_gate, dict) else None
        gate_failure = parent_gate.get("failure_classification") if isinstance(parent_gate, dict) else None
        gate_profile_sha = str(gate_profile.get("sha256") or "") if isinstance(gate_profile, dict) else ""
        gate_profile_payload = (
            {key: value for key, value in gate_profile.items() if key != "sha256"}
            if isinstance(gate_profile, dict)
            else {}
        )
        expected_gate_profile_sha = hashlib.sha256(
            json.dumps(
                gate_profile_payload,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        required_gate_flags = (
            "coverage_complete",
            "evidence_available",
            "trajectory_acceptable",
            "signal_acceptable",
            "acceptance_metrics_complete",
            "acceptance_contract_complete",
            "quality_acceptable",
        )
        if (
            not isinstance(parent_gate, dict)
            or parent_gate.get("schema_version") != "2.0"
            or parent_gate.get("status") != "acceptable"
            or any(parent_gate.get(name) is not True for name in required_gate_flags)
            or parent_gate.get("operator_confirmation_required") is not True
            or not isinstance(gate_profile, dict)
            or not isinstance(gate_profile.get("profile_id"), str)
            or not gate_profile["profile_id"]
            or not isinstance(gate_profile.get("version"), str)
            or not gate_profile["version"]
            or not isinstance(gate_profile.get("algorithm_version"), str)
            or not gate_profile["algorithm_version"]
            or not isinstance(gate_profile.get("matching_rules"), dict)
            or not gate_profile["matching_rules"]
            or not isinstance(gate_profile.get("thresholds"), dict)
            or not gate_profile["thresholds"]
            or re.fullmatch(r"[0-9a-f]{64}", gate_profile_sha) is None
            or not hmac.compare_digest(gate_profile_sha, expected_gate_profile_sha)
            or not isinstance(gate_failure, dict)
            or gate_failure.get("code") != "acceptable"
            or gate_failure.get("severity") != "none"
        ):
            raise RuntimeError("Accepted production trial does not have a complete server-verified acceptance contract")

    def _preflight_broadcast_request(self, request: dict[str, Any]) -> dict[str, Any]:
        if self._is_approved_child_run_request(request):
            raise ValueError("approved child recovery cannot be combined with broadcast_hybrid")
        if request.get("quality_profile") != "stable_broadcast":
            raise ValueError("broadcast_hybrid requires quality_profile='stable_broadcast'")
        max_windows = request.get("max_manual_review_windows")
        if not isinstance(max_windows, int) or isinstance(max_windows, bool) or not 1 <= max_windows <= 30:
            raise ValueError("max_manual_review_windows must be an integer between 1 and 30")
        calibration_raw = request.get("calibration_confirmation")
        if not isinstance(calibration_raw, dict):
            raise ValueError("broadcast_hybrid requires calibration_confirmation")
        calibration = ActionCalibration.from_dict(_jsonable({"schema_version": "1.0", **calibration_raw}))
        config_name = request.get("config_name")
        if not isinstance(config_name, str) or not config_name.strip():
            raise ValueError("broadcast_hybrid requires config_name")
        config_path, _ = self._resolve_config_path(config_name)
        config = load_config(config_path)
        config_raw = self._load_raw_yaml(config_path)
        config_metadata = config_raw.get("metadata")
        machine_note = self._machine_run_note(request.get("notes"))
        output_dir_name = request.get("output_dir_name")
        has_production_identity = (
            isinstance(output_dir_name, str)
            and output_dir_name.startswith("production_full_")
            or isinstance(config_metadata, dict)
            and "production_workflow" in config_metadata
            or machine_note is not None
            and machine_note.get("purpose") == "production_full"
        )
        production_full_note: dict[str, Any] | None = None
        if has_production_identity:
            if (
                machine_note is None
                or machine_note.get("schema_version") != "1.0"
                or machine_note.get("purpose") != "production_full"
            ):
                raise ValueError(
                    "Production-identified broadcast runs require schema_version=1.0 production_full notes"
                )
            production_full_note = machine_note
        config_patch = request.get("config_patch") or {}
        if not isinstance(config_patch, dict):
            raise ValueError("config_patch must be an object")
        patched_input = config_patch.get("input_video", config.input_video)
        input_video = request.get("input_video") or patched_input
        raw_video_path = Path(str(input_video))
        if not raw_video_path.is_absolute():
            raw_video_path = self.repo_root / raw_video_path
        video_path = self._resolve_input_video_path(str(raw_video_path))
        source_stat = video_path.stat()
        if production_full_note is not None:
            self._preflight_production_full_parent(
                request,
                production_full_note,
                config_path=config_path,
                config=config,
                calibration=calibration,
                video_path=video_path,
                source_stat=source_stat,
            )
        output_patch = config_patch.get("output") or {}
        if not isinstance(output_patch, dict):
            raise ValueError("config_patch.output must be an object")
        save_tracking_contract = output_patch.get(
            "save_tracking_contract",
            config.output.save_tracking_contract,
        )
        if save_tracking_contract is not True:
            raise ValueError("broadcast_hybrid requires output.save_tracking_contract=true")
        runtime_patch = config_patch.get("runtime") or {}
        if not isinstance(runtime_patch, dict):
            raise ValueError("config_patch.runtime must be an object")
        effective_start_frame = (
            request.get("start_frame")
            if request.get("start_frame") is not None
            else runtime_patch.get("start_frame", config.runtime.start_frame)
        )
        effective_max_frames = (
            request.get("max_frames")
            if request.get("max_frames") is not None
            else runtime_patch.get("max_frames", config.runtime.max_frames)
        )
        if isinstance(effective_start_frame, bool) or not isinstance(effective_start_frame, int):
            raise ValueError("broadcast_hybrid start_frame must be an integer")
        if effective_start_frame != 0:
            raise ValueError("broadcast_hybrid requires a full-video run with start_frame=0")
        if effective_max_frames is not None and (
            isinstance(effective_max_frames, bool) or not isinstance(effective_max_frames, int)
        ):
            raise ValueError("broadcast_hybrid max_frames must be an integer or null")

        capture = cv2.VideoCapture(str(video_path))
        try:
            if not capture.isOpened():
                raise ValueError(f"broadcast source video is not decodable: {video_path}")
            source_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            source_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            source_frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0) or None
            fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
            if source_width <= 0 or source_height <= 0 or not np.isfinite(fps) or fps <= 0.0:
                raise ValueError("broadcast source video metadata is invalid")
            if effective_max_frames is not None and (
                source_frame_count is None or effective_max_frames < source_frame_count
            ):
                raise ValueError("broadcast_hybrid max_frames must cover the complete source video")
            validate_calibration_for_video(
                calibration,
                source_width=source_width,
                source_height=source_height,
                total_source_frames=source_frame_count,
            )
        finally:
            capture.release()
        return {
            "input_video": str(video_path),
            "source_resolution": [source_width, source_height],
            "source_frame_count": source_frame_count,
            "fps": fps,
            "source_size_bytes": source_stat.st_size,
            "source_mtime_ns": source_stat.st_mtime_ns,
            "calibration": calibration.to_dict(),
            "classifier_status": "missing_until_hash_bound_predictions_are_supplied",
            "selective_policy_status": "missing_until_qualified_evidence_is_supplied",
        }

    def _create_approved_child_run(self, request: dict[str, Any]) -> dict[str, Any]:
        parent_run_id = str(request.get("parent_run_id") or "").strip()
        if not parent_run_id:
            raise ValueError("Approved child recovery requires parent_run_id.")
        parent_run = self.get_run(parent_run_id)
        if parent_run.get("status") != "completed":
            raise RuntimeError(f"Parent run must be completed before approved child rerun: {parent_run_id}")

        parent_output_dir = Path(parent_run["output_dir"]).resolve()
        artifact_path = self._resolve_approved_actions_artifact_path(
            parent_output_dir,
            request.get("approved_actions_artifact_name"),
        )
        artifact = self._load_approved_actions_artifact(artifact_path)
        selected_artifact = self._select_approved_actions_artifact(
            artifact,
            request.get("approved_action_ids") or [],
        )
        selected_artifact["source_approved_actions_path"] = str(artifact_path)
        known_packet_ids, known_visual_ids, known_visual_localization_ids = self._traceable_approval_provenance_ids(
            parent_output_dir
        )
        executable_windows = approved_action_windows_from_report(
            selected_artifact,
            mode="sahi",
            known_source_packet_ids=known_packet_ids,
            known_visual_review_ids=known_visual_ids,
            known_visual_localization_ids=known_visual_localization_ids,
        )
        if not executable_windows:
            raise ValueError("Approved child recovery requires at least one executable approved recovery action.")
        source_total_frames = self._run_record_source_total_frames(parent_run)
        if self._has_localize_window(executable_windows) and source_total_frames is None:
            raise ValueError("Approved child recovery with localize_ball_roi requires a known source frame count.")
        if self._has_full_video_localize_window(executable_windows, source_total_frames):
            raise ValueError("Approved child recovery rejects full-video localize_ball_roi scope.")
        if self._has_source_clamped_invalid_localize_window(executable_windows, source_total_frames):
            raise ValueError(
                "Approved child recovery rejects localize_ball_roi outside the source-clamped frame window."
            )
        missing_candidate_ids = self._recovery_actions_without_candidate_id(selected_artifact)
        if missing_candidate_ids:
            raise ValueError(
                "Approved child recovery requires candidate_id for selected recovery actions: "
                + ", ".join(missing_candidate_ids)
            )
        recovery_candidate_ids = self._recovery_candidate_ids(selected_artifact)
        if not recovery_candidate_ids:
            raise ValueError("Approved child recovery requires selected recovery actions to include candidate_id.")
        if len(recovery_candidate_ids) > 1:
            raise ValueError("Approved child recovery requires selected recovery actions to share one candidate_id.")
        candidate_id = next(iter(recovery_candidate_ids))
        selected_frame_budget = sum(
            int(window["end_frame"]) - int(window["start_frame"]) + 1 for window in executable_windows
        )

        config_path, relative_name = self._resolve_run_config_reference(parent_run)
        config = load_config(config_path)
        self._validate_output_csv_name(config.output.csv_name)
        input_video = parent_run.get("input_video") or str(config.input_video)
        if not input_video:
            raise FileNotFoundError(f"Parent run {parent_run_id} is not linked to an input video.")
        config.input_video = self._resolve_input_video_path(input_video)

        run_id = self._validated_output_run_id(
            request.get("output_dir_name"),
            default=f"approved_{parent_run_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}",
        )
        config.output_dir = self._missing_ball_candidate_output_dir(parent_output_dir, candidate_id)
        if config.output_dir.exists():
            raise FileExistsError(str(config.output_dir))

        artifact_relative_path = APPROVED_ACTIONS_FILE_NAME
        child_artifact_path = config.output_dir / artifact_relative_path
        child_config_path = config.output_dir / "approved_recovery_config.yaml"
        parent_fingerprints = self._capture_parent_run_fingerprints(
            parent_run,
            parent_output_dir,
            config_path,
            input_video_path=Path(config.input_video).resolve(),
        )

        config.postprocess.enabled = False
        config.follow_cam.enabled = False
        config.temporal_chunks.enabled = False
        config.high_recall_windows.enabled = True
        config.high_recall_windows.margin_frames = 0
        config.high_recall_windows.merge_gap_frames = 0
        config.high_recall_windows.approved_actions_path = artifact_relative_path
        config.high_recall_windows.approved_only = True
        max_approved_frames = config.high_recall_windows.max_total_frames or DEFAULT_HIGH_RECALL_MAX_TOTAL_FRAMES
        if max_approved_frames is not None and selected_frame_budget > int(max_approved_frames):
            raise ValueError(
                f"Approved child recovery frame budget {selected_frame_budget} exceeds "
                f"high_recall_windows.max_total_frames {max_approved_frames}."
            )
        config.high_recall_windows.max_total_frames = selected_frame_budget

        run_record = {
            "run_id": run_id,
            "source": "approved_child_rerun",
            "status": "queued",
            "created_at": _utc_now_iso(),
            "started_at": None,
            "completed_at": None,
            "config_name": relative_name,
            "config_path": str(child_config_path),
            "input_video": str(config.input_video),
            "parent_run_id": parent_run_id,
            "output_dir": str(config.output_dir),
            "modules_enabled": {
                "postprocess": False,
                "follow_cam": False,
                "temporal_chunks": False,
                "high_recall_windows": True,
            },
            "artifacts": self._collect_artifacts(config.output_dir),
            "stats": self._collect_stats(config.output_dir),
            "progress": self._initial_progress(),
            "notes": request.get("notes"),
            "error": None,
        }
        self._attach_ai_candidate_lifecycle(run_record)

        thread: threading.Thread | None = None
        with self._lock:
            self._assert_service_open_locked()
            self._assert_no_active_run_locked()
            output_created = False
            registry_written = False
            if config.output_dir.exists():
                raise FileExistsError(str(config.output_dir))
            try:
                registry = self._read_registry()
                if any(run.get("run_id") == run_id for run in registry.get("runs", [])):
                    raise ValueError(f"Run already exists: {run_id}")
                config.output_dir.mkdir(parents=True, exist_ok=False)
                output_created = True
                self._copy_approved_child_inputs(
                    parent_output_dir=parent_output_dir,
                    child_output_dir=config.output_dir,
                    child_artifact_path=child_artifact_path,
                    selected_artifact=selected_artifact,
                    csv_name=config.output.csv_name,
                )
                self._write_approved_child_config(child_config_path, config)
                registry["runs"].append(run_record)
                self._write_registry(registry)
                registry_written = True
                cancel_event = threading.Event()
                thread = threading.Thread(
                    target=self._execute_approved_child_run,
                    args=(run_id, config, parent_run_id, parent_fingerprints, source_total_frames, cancel_event),
                    name=f"football-tracking-approved-child-{run_id}",
                    daemon=True,
                )
                self._active_threads[run_id] = thread
                self._cancel_events[run_id] = cancel_event
            except Exception:
                self._active_threads.pop(run_id, None)
                self._cancel_events.pop(run_id, None)
                if registry_written:
                    try:
                        registry = self._read_registry()
                        registry["runs"] = [run for run in registry["runs"] if run["run_id"] != run_id]
                        self._write_registry(registry)
                    except Exception:
                        pass
                if output_created and config.output_dir.exists():
                    shutil.rmtree(config.output_dir, ignore_errors=True)
                raise
        assert thread is not None
        self._start_thread_or_cleanup(
            run_id,
            thread,
            output_dir=config.output_dir,
            remove_output=True,
        )
        return run_record

    def _validated_output_run_id(self, requested_name: Any, *, default: str) -> str:
        raw_name = str(requested_name or "").strip()
        if not raw_name:
            return default
        output_name = Path(raw_name).name
        if output_name != raw_name or output_name in {".", ".."}:
            raise ValueError("output_dir_name must be a safe single directory name.")
        return output_name

    def _missing_ball_candidate_output_dir(self, parent_output_dir: Path, candidate_id: str) -> Path:
        return missing_ball_candidate_output_dir(parent_output_dir, candidate_id)

    def _validate_output_csv_name(self, value: str) -> None:
        validate_output_csv_name(value)

    def _run_record_source_total_frames(self, run: dict[str, Any]) -> int | None:
        stats = run.get("stats") if isinstance(run.get("stats"), dict) else {}
        for source_name in ("cleaned", "raw"):
            source_stats = stats.get(source_name) if isinstance(stats.get(source_name), dict) else {}
            frame_count = self._optional_int(source_stats.get("frame_count"))
            if frame_count is not None and frame_count > 0:
                return frame_count
        output_dir = Path(str(run.get("output_dir") or ""))
        for name in ("ball_track.cleaned.csv", "ball_track.csv"):
            frame_count = self._track_frame_count(output_dir / name)
            if frame_count is not None and frame_count > 0:
                return frame_count
        return None

    def _has_full_video_localize_window(self, windows: list[dict[str, Any]], source_total_frames: int | None) -> bool:
        if source_total_frames is None or source_total_frames <= 0:
            return False
        localize_windows: list[dict[str, Any]] = []
        for window in windows:
            if not self._approved_window_is_localize_ball_roi(window):
                continue
            localize_windows.append(window)
            start_frame = self._optional_int(window.get("start_frame"))
            end_frame = self._optional_int(window.get("end_frame"))
            if (
                start_frame is not None
                and end_frame is not None
                and start_frame <= 0
                and end_frame >= source_total_frames - 1
            ):
                return True
        return self._localize_windows_cover_full_video(localize_windows, source_total_frames)

    def _has_localize_window(self, windows: list[dict[str, Any]]) -> bool:
        return any(self._approved_window_is_localize_ball_roi(window) for window in windows)

    def _has_source_clamped_invalid_localize_window(
        self,
        windows: list[dict[str, Any]],
        source_total_frames: int | None,
    ) -> bool:
        if source_total_frames is None or source_total_frames <= 0:
            return False
        for window in windows:
            if not self._approved_window_is_localize_ball_roi(window):
                continue
            start_frame = self._optional_int(window.get("start_frame"))
            end_frame = self._optional_int(window.get("end_frame"))
            if start_frame is None or end_frame is None:
                return True
            if end_frame < start_frame:
                start_frame, end_frame = end_frame, start_frame
            if start_frame >= source_total_frames:
                return True
            end_frame = min(end_frame, source_total_frames - 1)
            roi = window.get("local_search_roi") if isinstance(window.get("local_search_roi"), dict) else {}
            roi_frame = self._optional_int(roi.get("frame"))
            if roi_frame is None or roi_frame < start_frame or roi_frame > end_frame:
                return True
        return False

    def _localize_windows_cover_full_video(self, windows: list[dict[str, Any]], source_total_frames: int) -> bool:
        coverage_end = -1
        for window in sorted(windows, key=lambda item: (int(item["start_frame"]), int(item["end_frame"]))):
            start = int(window["start_frame"])
            end = int(window["end_frame"])
            if start > coverage_end + 1:
                return False
            coverage_end = max(coverage_end, end)
            if coverage_end >= source_total_frames - 1:
                return True
        return False

    def _approved_window_is_localize_ball_roi(self, window: dict[str, Any]) -> bool:
        if window.get("approved_action") == "localize_ball_roi":
            return True
        provenance = window.get("approval_provenance")
        if not isinstance(provenance, list):
            return False
        return any(isinstance(item, dict) and item.get("approved_action") == "localize_ball_roi" for item in provenance)

    def _track_frame_count(self, path: Path) -> int | None:
        if not path.exists():
            return None
        try:
            with path.open("r", newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                max_frame: int | None = None
                for row in reader:
                    frame = self._optional_int(row.get("Frame"))
                    if frame is not None and (max_frame is None or frame > max_frame):
                        max_frame = frame
                return None if max_frame is None else max_frame + 1
        except OSError:
            return None

    def _traceable_approval_provenance_ids(
        self,
        output_dir: Path,
    ) -> tuple[set[str] | None, set[str] | None, set[str] | None]:
        return traceable_approval_provenance_ids(output_dir)

    def _recovery_candidate_ids(self, artifact: dict[str, Any]) -> set[str]:
        candidate_ids: set[str] = set()
        actions = artifact.get("approved_actions") if isinstance(artifact.get("approved_actions"), list) else []
        for action in actions:
            if not isinstance(action, dict) or action.get("approved_action") not in {
                "localize_ball_roi",
                "targeted_rerun",
                "rerun_ball_window",
            }:
                continue
            candidate_id = action.get("candidate_id")
            if isinstance(candidate_id, str) and candidate_id.strip():
                candidate_ids.add(candidate_id.strip())
        return candidate_ids

    def _recovery_actions_without_candidate_id(self, artifact: dict[str, Any]) -> list[str]:
        missing: list[str] = []
        actions = artifact.get("approved_actions") if isinstance(artifact.get("approved_actions"), list) else []
        for action in actions:
            if not isinstance(action, dict) or action.get("approved_action") not in {
                "localize_ball_roi",
                "targeted_rerun",
                "rerun_ball_window",
            }:
                continue
            candidate_id = action.get("candidate_id")
            if not isinstance(candidate_id, str) or not candidate_id.strip():
                missing.append(str(action.get("approval_id") or action.get("improvement_id") or "<unknown>"))
        return missing

    def _read_json_file(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return loaded if isinstance(loaded, dict) else None

    def _list_dicts(self, value: Any) -> list[dict[str, Any]]:
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    def _add_string_value(self, target: set[str], value: Any) -> None:
        if isinstance(value, str) and value.strip():
            target.add(value.strip())

    def _resolve_approved_actions_artifact_path(self, parent_output_dir: Path, requested_name: Any) -> Path:
        raw_name = str(requested_name or APPROVED_ACTIONS_FILE_NAME).strip()
        if not raw_name:
            raise ValueError("approved_actions_artifact_name must not be empty.")
        raw_path = Path(raw_name)
        candidate = raw_path if raw_path.is_absolute() else parent_output_dir / raw_path
        resolved = candidate.resolve()
        if resolved != parent_output_dir and parent_output_dir not in resolved.parents:
            raise ValueError("approved_actions_artifact_name must stay within the parent output directory.")
        if not resolved.is_file():
            raise FileNotFoundError(f"Approved actions artifact not found: {raw_name}")
        return resolved

    def _load_approved_actions_artifact(self, artifact_path: Path) -> dict[str, Any]:
        try:
            loaded = json.loads(artifact_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{artifact_path.name} corrupt: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"{artifact_path.name} invalid: expected JSON object.")
        if not isinstance(loaded.get("approved_actions"), list):
            raise ValueError(f"{artifact_path.name} invalid: approved_actions must be a list.")
        return loaded

    def _select_approved_actions_artifact(self, artifact: dict[str, Any], approval_ids: list[Any]) -> dict[str, Any]:
        actions = artifact.get("approved_actions")
        if not isinstance(actions, list):
            raise ValueError("Approved actions artifact invalid: approved_actions must be a list.")
        selected_ids = [str(item).strip() for item in approval_ids if str(item).strip()]
        if not selected_ids:
            raise ValueError("Approved child recovery requires at least one explicit approved_action_id.")
        actions_by_id: dict[str, dict[str, Any]] = {}
        duplicate_ids: set[str] = set()
        for action in actions:
            if not isinstance(action, dict):
                continue
            approval_id = str(action.get("approval_id") or "").strip()
            if not approval_id:
                continue
            if approval_id in actions_by_id:
                duplicate_ids.add(approval_id)
            actions_by_id[approval_id] = action
        if duplicate_ids:
            raise ValueError(f"Duplicate approved action IDs: {', '.join(sorted(duplicate_ids))}")
        missing = [approval_id for approval_id in selected_ids if approval_id not in actions_by_id]
        if missing:
            raise ValueError(f"Approved action IDs not found: {', '.join(missing)}")
        selected_actions = [actions_by_id[approval_id] for approval_id in selected_ids]
        selected_artifact = dict(artifact)
        selected_artifact["approved_actions"] = selected_actions
        return selected_artifact

    def _copy_approved_child_inputs(
        self,
        *,
        parent_output_dir: Path,
        child_output_dir: Path,
        child_artifact_path: Path,
        selected_artifact: dict[str, Any],
        csv_name: str,
    ) -> None:
        copy_candidate_inputs(
            parent_output_dir=parent_output_dir,
            candidate_output_dir=child_output_dir,
            selected_artifact=selected_artifact,
            csv_name=csv_name,
        )
        child_artifact_path.parent.mkdir(parents=True, exist_ok=True)
        child_artifact_path.write_text(
            json.dumps(selected_artifact, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _write_approved_child_config(self, config_path: Path, config: AppConfig) -> None:
        write_recovery_config(config_path, config)

    def _capture_parent_run_fingerprints(
        self,
        parent_run: dict[str, Any],
        parent_output_dir: Path,
        config_path: Path | None,
        *,
        input_video_path: Path | None = None,
    ) -> dict[str, tuple[int, str] | None]:
        watched_paths: list[Path] = []
        run_config_path = Path(parent_run["config_path"]).resolve() if parent_run.get("config_path") else config_path
        if run_config_path is not None:
            watched_paths.append(run_config_path)
        if input_video_path is not None:
            watched_paths.append(input_video_path)
        return capture_parent_fingerprints(parent_output_dir, watched_paths=watched_paths)

    def _assert_parent_fingerprints_unchanged(self, fingerprints: dict[str, tuple[int, str] | None]) -> None:
        assert_parent_fingerprints_unchanged(fingerprints)

    def _parent_fingerprint_error(self, fingerprints: dict[str, tuple[int, str] | None]) -> str | None:
        try:
            self._assert_parent_fingerprints_unchanged(fingerprints)
        except RuntimeError as exc:
            return str(exc)
        return None

    def _file_fingerprint(self, path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return path.stat().st_size, digest.hexdigest()

    def create_follow_cam_render(self, source_run_id: str, request: dict[str, Any]) -> dict[str, Any]:
        self._assert_service_open()
        source_run = self.get_run(source_run_id)
        if source_run.get("status") != "completed":
            raise RuntimeError(f"Run must be completed before rendering a deliverable: {source_run_id}")
        source_output_dir = Path(source_run["output_dir"]).resolve()
        self._assert_follow_cam_only_render_allowed(source_output_dir)

        config_path, relative_name = self._resolve_run_config_reference(source_run)
        config = load_config(config_path)
        input_video = source_run.get("input_video") or str(config.input_video)
        if not input_video:
            raise FileNotFoundError(f"Run {source_run_id} is not linked to an input video.")

        config.input_video = self._resolve_input_video_path(input_video)
        requested_output_name = request.get("output_dir_name")
        run_id = Path(requested_output_name).name if requested_output_name else ""
        if not run_id:
            run_id = f"deliver_{source_run_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"

        config.output_dir = self._build_run_output_dir(run_id=run_id, input_video=config.input_video)
        if config.output_dir.exists() and any(config.output_dir.iterdir()):
            raise FileExistsError(str(config.output_dir))
        output_created = not config.output_dir.exists()
        config.output_dir.mkdir(parents=True, exist_ok=True)

        config.postprocess.enabled = False
        config.follow_cam.enabled = True
        config.follow_cam.prefer_cleaned_track = bool(request.get("prefer_cleaned_track", True))
        config.follow_cam.draw_ball_marker = bool(request.get("draw_ball_marker", False))
        config.follow_cam.draw_frame_text = bool(request.get("draw_frame_text", False))
        config.follow_cam.target_width = max(320, int(request.get("target_width") or 1920))
        config.follow_cam.target_height = max(180, int(request.get("target_height") or 1080))
        config.follow_cam.output_video_name = self._resolve_render_video_name(
            request.get("output_video_name"),
            default_name="deliverable_16x9.mp4",
        )

        self._prepare_follow_cam_render_inputs(
            source_output_dir=source_output_dir, render_output_dir=config.output_dir, config=config
        )

        render_notes = request.get("notes") or (
            f"Standalone 16:9 render from {source_run_id} | "
            f"marker={int(config.follow_cam.draw_ball_marker)} | "
            f"annotation={int(config.follow_cam.draw_frame_text)}"
        )
        run_record = {
            "run_id": run_id,
            "source": "follow_cam_render",
            "status": "queued",
            "created_at": _utc_now_iso(),
            "started_at": None,
            "completed_at": None,
            "config_name": relative_name,
            "config_path": str(config_path),
            "input_video": str(config.input_video),
            "parent_run_id": source_run_id,
            "output_dir": str(config.output_dir),
            "modules_enabled": {
                "postprocess": False,
                "follow_cam": True,
            },
            "artifacts": self._collect_artifacts(config.output_dir),
            "stats": self._collect_stats(config.output_dir),
            "progress": self._initial_progress(),
            "notes": render_notes,
            "error": None,
        }
        self._attach_ai_candidate_lifecycle(run_record)

        with self._lock:
            self._assert_service_open_locked()
            self._assert_no_active_run_locked()
            registry = self._read_registry()
            registry["runs"] = [run for run in registry["runs"] if run["run_id"] != run_id]
            registry["runs"].append(run_record)
            self._write_registry(registry)
            cancel_event = threading.Event()
            thread = threading.Thread(
                target=self._execute_follow_cam_render,
                args=(run_id, config, source_run_id, cancel_event),
                name=f"football-tracking-render-{run_id}",
                daemon=True,
            )
            self._active_threads[run_id] = thread
            self._cancel_events[run_id] = cancel_event
        self._start_thread_or_cleanup(
            run_id,
            thread,
            output_dir=config.output_dir,
            remove_output=output_created,
        )
        return run_record

    def create_highlight_render(self, source_run_id: str, request: dict[str, Any]) -> dict[str, Any]:
        self._assert_service_open()
        source_run = self.get_run(source_run_id)
        if source_run.get("status") != "completed":
            raise RuntimeError(f"Run must be completed before rendering a highlight: {source_run_id}")

        config_path, relative_name = self._resolve_run_config_reference(source_run)
        config = load_config(config_path)
        input_video = source_run.get("input_video") or str(config.input_video)
        if not input_video:
            raise FileNotFoundError(f"Run {source_run_id} is not linked to an input video.")
        config.input_video = self._resolve_input_video_path(input_video)
        highlight_selection = self._resolve_highlight_selection(
            source_run_id,
            request,
            source_total_frames=self._source_video_frame_count(config.input_video),
        )

        requested_output_name = request.get("output_dir_name")
        run_id = Path(requested_output_name).name if requested_output_name else ""
        if not run_id:
            run_id = f"highlight_{source_run_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"

        config.output_dir = self._build_run_output_dir(run_id=run_id, input_video=config.input_video)
        if config.output_dir.exists() and any(config.output_dir.iterdir()):
            raise FileExistsError(str(config.output_dir))
        output_created = not config.output_dir.exists()
        config.output_dir.mkdir(parents=True, exist_ok=True)

        config.postprocess.enabled = False
        config.follow_cam.enabled = False
        output_video_name = self._resolve_render_video_name(
            request.get("output_video_name"),
            default_name="highlight.mp4",
        )

        source_output_dir = Path(source_run["output_dir"]).resolve()
        self._prepare_highlight_render_inputs(
            source_output_dir=source_output_dir, render_output_dir=config.output_dir, config=config
        )

        window = highlight_selection["window"]
        render_notes = request.get("notes") or (
            f"Highlight render from {source_run_id} | frames={window['start_frame']}-{window['end_frame']}"
        )
        run_record = {
            "run_id": run_id,
            "source": "highlight_render",
            "status": "queued",
            "created_at": _utc_now_iso(),
            "started_at": None,
            "completed_at": None,
            "config_name": relative_name,
            "config_path": str(config_path),
            "input_video": str(config.input_video),
            "parent_run_id": source_run_id,
            "output_dir": str(config.output_dir),
            "modules_enabled": {
                "postprocess": False,
                "follow_cam": False,
            },
            "artifacts": self._collect_artifacts(config.output_dir),
            "stats": self._collect_stats(config.output_dir),
            "progress": self._initial_progress(),
            "notes": render_notes,
            "error": None,
        }
        self._attach_ai_candidate_lifecycle(run_record)

        with self._lock:
            self._assert_service_open_locked()
            self._assert_no_active_run_locked()
            registry = self._read_registry()
            registry["runs"] = [run for run in registry["runs"] if run["run_id"] != run_id]
            registry["runs"].append(run_record)
            self._write_registry(registry)
            cancel_event = threading.Event()
            thread = threading.Thread(
                target=self._execute_highlight_render,
                args=(run_id, config, source_run_id, output_video_name, highlight_selection, cancel_event),
                name=f"football-tracking-highlight-{run_id}",
                daemon=True,
            )
            self._active_threads[run_id] = thread
            self._cancel_events[run_id] = cancel_event
        self._start_thread_or_cleanup(
            run_id,
            thread,
            output_dir=config.output_dir,
            remove_output=output_created,
        )
        return run_record

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            self._assert_service_open_locked()
            with self._registry_transaction() as registry:
                for run in registry["runs"]:
                    if run["run_id"] != run_id:
                        continue
                    status = run.get("status")
                    if status not in {"queued", "running"}:
                        raise RuntimeError(f"Run is not active: {run_id}")
                    broadcast = run.get("broadcast") if isinstance(run.get("broadcast"), dict) else {}
                    if broadcast.get("commit_started") is True:
                        raise RuntimeError(f"Run commit has already started and can no longer be cancelled: {run_id}")
                    run["broadcast"] = {**broadcast, "cancel_requested": True}
                    cancel_event = self._cancel_events.get(run_id)
                    owner_active = self._owner_lease_is_active(
                        broadcast.get("owner_pid"),
                        broadcast.get("owner_instance_id"),
                    )
                    if cancel_event is not None:
                        cancel_event.set()
                    if cancel_event is not None or owner_active:
                        run["progress"] = self._cancelling_progress(run.get("progress"), run.get("started_at"))
                    else:
                        completed_at = _utc_now_iso()
                        run.update(
                            {
                                "status": "cancelled",
                                "completed_at": completed_at,
                                "progress": self._cancelled_progress(run.get("progress"), run.get("started_at")),
                            }
                        )
                        output_dir = Path(run["output_dir"]).resolve()
                        artifact_error = self._write_run_artifacts(output_dir, run)
                        run["artifacts"] = self._collect_artifacts(output_dir)
                        run["stats"] = self._collect_stats(output_dir)
                        run["error"] = self._append_artifact_error(run.get("error"), artifact_error)
                    return deepcopy(run)
                raise KeyError(run_id)

    def reconfirm_broadcast_config_lineage(self, run_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Append one independently reviewed canonical configuration generation."""

        self._assert_service_open()
        with self._lock:
            self._assert_service_open_locked()
            with self._registry_transaction() as registry:
                parent = next((item for item in registry["runs"] if item.get("run_id") == run_id), None)
                if parent is None:
                    raise KeyError(run_id)
                if parent.get("source") != "broadcast_hybrid":
                    raise RuntimeError(f"Run is not a broadcast_hybrid run: {run_id}")
                if parent.get("status") != "completed":
                    raise RuntimeError(f"Broadcast run must be completed before review operations: {run_id}")
                output_dir = Path(parent["output_dir"]).resolve()
                if self.outputs_dir.resolve() not in output_dir.parents:
                    raise RuntimeError(f"Broadcast run output is outside the outputs root: {run_id}")
                return self._reconfirm_broadcast_config_lineage_in_transaction(
                    run_id,
                    request,
                    parent=parent,
                    registry=registry,
                )

    def _reconfirm_broadcast_config_lineage_in_transaction(
        self,
        run_id: str,
        request: dict[str, Any],
        *,
        parent: dict[str, Any],
        registry: dict[str, Any],
    ) -> dict[str, Any]:
        challenge = self._broadcast_config_lineage_reconfirmation_authority(
            parent,
            registry=registry,
        )
        confirmed_name = challenge["confirmed_config_name"]
        confirmed_sha256 = challenge["confirmed_text_sha256"]
        config_path_value = parent.get("config_path")
        assert isinstance(config_path_value, str)
        authoritative_workflow_bindings = challenge["workflow_bindings"]
        if (
            request.get("target_run_id") != run_id
            or request.get("target_run_id") != challenge["target_run_id"]
            or request.get("confirmed_config_name") != confirmed_name
            or request.get("confirmed_text_sha256") != confirmed_sha256
        ):
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                "config lineage reconfirmation challenge does not match current server-derived authority",
            )
        workflow_bindings = request.get("workflow_bindings")
        if not isinstance(workflow_bindings, dict):
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                "config lineage workflow bindings are malformed",
            )
        if workflow_bindings != authoritative_workflow_bindings:
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                "config lineage workflow bindings do not match server-derived authority",
            )
        operator_id = request.get("operator_id")
        reviewer_id = request.get("reviewer_id")
        observed_raw_sha256 = request.get("expected_observed_raw_sha256")
        try:
            generation = reconfirm_config_lineage(
                trusted_config_root=self.config_dir,
                observed_config_path=Path(config_path_value),
                lineage_root=self._config_lineage_root(),
                target_run_id=run_id,
                confirmed_config_name=confirmed_name,
                confirmed_text_sha256=confirmed_sha256,
                expected_observed_raw_sha256=observed_raw_sha256,
                workflow_bindings=authoritative_workflow_bindings,
                operator_id=operator_id,
                reviewer_id=reviewer_id,
            )
        except ConfigLineageError:
            raise
        manifest_sha256 = generation.manifest_sha256
        canonical_snapshot_sha256 = generation.canonical_snapshot_sha256
        if (
            not isinstance(manifest_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None
            or not isinstance(canonical_snapshot_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", canonical_snapshot_sha256) is None
        ):
            raise ConfigLineageError(
                CONFIG_LINEAGE_UNSAFE,
                "config lineage secure read digests are unavailable",
            )
        operation_id = f"config-lineage-{generation.generation_id.removeprefix('lineage-')}"
        operation = {
            "run_id": operation_id,
            "source": "config_lineage_reconfirmation",
            "status": "completed",
            "created_at": _utc_now_iso(),
            "started_at": None,
            "completed_at": _utc_now_iso(),
            "config_name": confirmed_name,
            "config_path": config_path_value,
            "input_video": parent.get("input_video"),
            "parent_run_id": run_id,
            "output_dir": str(generation.generation_dir),
            "modules_enabled": {},
            "artifacts": [],
            "stats": {},
            "progress": None,
            "notes": json.dumps(generation.manifest["projection"], sort_keys=True, separators=(",", ":")),
            "error": None,
            "broadcast": {
                "operation": "config_lineage_reconfirmation",
                "generation_id": generation.generation_id,
                "manifest_sha256": manifest_sha256,
                "canonical_snapshot_sha256": canonical_snapshot_sha256,
                "workflow_bindings": deepcopy(authoritative_workflow_bindings),
                "operator_id": operator_id,
                "reviewer_id": reviewer_id,
                "idempotent": generation.idempotent,
            },
        }
        existing = next(
            (
                item
                for item in registry["runs"]
                if item.get("source") == "config_lineage_reconfirmation" and item.get("parent_run_id") == run_id
            ),
            None,
        )
        if existing is not None:
            existing_metadata = existing.get("broadcast") if isinstance(existing.get("broadcast"), dict) else {}
            if (
                existing.get("run_id") != operation_id
                or existing_metadata.get("generation_id") != generation.generation_id
                or existing_metadata.get("manifest_sha256") != manifest_sha256
                or existing_metadata.get("workflow_bindings") != authoritative_workflow_bindings
            ):
                raise ConfigLineageError(
                    CONFIG_LINEAGE_CONFLICT,
                    "config lineage reconfirmation conflict",
                )
            operation = deepcopy(existing)
        else:
            registry["runs"].append(operation)
        return {
            "run_id": run_id,
            "status": "reconfirmed",
            "generation_id": generation.generation_id,
            "manifest_sha256": manifest_sha256,
            **generation.manifest["projection"],
        }

    def _broadcast_config_lineage_reconfirmation_authority(
        self,
        parent: dict[str, Any],
        *,
        registry: dict[str, Any],
    ) -> dict[str, Any]:
        """Derive current reconfirmation authority without trusting client input."""

        notes = parent.get("notes")
        if notes is None:
            raise ConfigLineageError(
                CONFIG_LINEAGE_REQUIRED,
                "review evidence target confirmation record is required",
            )
        try:
            confirmation = json.loads(notes) if isinstance(notes, str) else notes
        except json.JSONDecodeError as exc:
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                "review evidence target confirmation record is malformed",
            ) from exc
        if not isinstance(confirmation, dict):
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                "review evidence target confirmation record is malformed",
            )
        confirmed_name = confirmation.get("confirmed_config_name")
        confirmed_sha256 = confirmation.get("expected_config_sha256")
        if confirmed_name is None:
            raise ConfigLineageError(
                CONFIG_LINEAGE_REQUIRED,
                "review evidence target confirmation record has no config name",
            )
        if (
            not isinstance(confirmed_name, str)
            or not confirmed_name.strip()
            or confirmed_name != confirmed_name.strip()
        ):
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                "review evidence target confirmation config name is malformed",
            )
        if confirmed_sha256 is None:
            raise ConfigLineageError(
                CONFIG_LINEAGE_REQUIRED,
                "review evidence target confirmation record has no config SHA-256",
            )
        if not isinstance(confirmed_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", confirmed_sha256) is None:
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                "review evidence target confirmation config SHA-256 is malformed",
            )
        config_path_value = parent.get("config_path")
        if config_path_value is None:
            raise ConfigLineageError(
                CONFIG_LINEAGE_REQUIRED,
                "review evidence target confirmed config is required",
            )
        if not isinstance(config_path_value, str) or not config_path_value.strip():
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                "review evidence target confirmed config authority is malformed",
            )
        try:
            authoritative_workflow_bindings = self._derive_config_lineage_workflow_bindings(
                parent,
                Path(config_path_value),
                registry=registry,
            )
        except ConfigLineageError:
            raise
        except (ValueError, RuntimeError) as exc:
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                "config lineage registry authority does not match the target",
            ) from exc
        target_run_id = parent.get("run_id")
        if not isinstance(target_run_id, str) or not target_run_id or target_run_id != target_run_id.strip():
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                "review evidence target run identity is malformed",
            )
        return {
            "target_run_id": target_run_id,
            "confirmed_config_name": confirmed_name,
            "confirmed_text_sha256": confirmed_sha256,
            "workflow_bindings": authoritative_workflow_bindings,
        }

    def _broadcast_config_lineage_reconfirmation_challenge(
        self,
        parent: dict[str, Any],
        *,
        registry: dict[str, Any],
    ) -> dict[str, Any]:
        """Build a current server-authored challenge without trusting client-derived lineage."""

        authority = self._broadcast_config_lineage_reconfirmation_authority(
            parent,
            registry=registry,
        )
        config_path_value = parent.get("config_path")
        assert isinstance(config_path_value, str)
        try:
            _, inspection = capture_config_bytes(
                self.config_dir,
                Path(config_path_value),
            )
        except ConfigLineageError:
            raise
        if inspection.confirmed_text_sha256 != authority["confirmed_text_sha256"]:
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                "confirmed production config canonical digest does not match the target confirmation",
            )
        return {
            **authority,
            "expected_observed_raw_sha256": inspection.observed_raw_sha256,
        }

    def get_broadcast_review_evidence(self, run_id: str) -> dict[str, Any]:
        parent, output_dir = self._broadcast_run_output(run_id)
        queue_path = output_dir / "selective_review_queue.v1.json"
        queue: dict[str, Any] | None = None
        queue_sha256: str | None = None
        try:
            with self._lock:
                with self._registry_file_lock():
                    registry = self._read_registry()
                    parent = self._review_evidence_parent_from_registry(registry, run_id, output_dir)
                    target = self._review_evidence_target(
                        run_id,
                        output_dir,
                        parent=parent,
                        registry=registry,
                    )
                    if queue_path.is_file():
                        queue, queue_sha256 = self._validate_current_review_queue_locked(
                            run_id,
                            output_dir,
                            parent,
                            queue_path,
                            registry=registry,
                        )
        except _ReviewEvidenceTargetContextError as exc:
            blocker: ConfigLineageError = ConfigLineageError(exc.code, str(exc))
            challenge: dict[str, Any] | None = None
            if exc.code == CONFIG_LINEAGE_REQUIRED:
                try:
                    with self._lock:
                        with self._registry_file_lock():
                            challenge_registry = self._read_registry()
                            challenge_parent = self._review_evidence_parent_from_registry(
                                challenge_registry,
                                run_id,
                                output_dir,
                            )
                            challenge = self._broadcast_config_lineage_reconfirmation_challenge(
                                challenge_parent,
                                registry=challenge_registry,
                            )
                except ConfigLineageError as challenge_error:
                    blocker = challenge_error
            recovery_action = (
                "reconfirm_production_config"
                if blocker.code == CONFIG_LINEAGE_REQUIRED and challenge is not None
                else "inspect_production_config_lineage"
            )
            return {
                "run_id": run_id,
                "status": "blocked",
                "active_job_id": None,
                "generation_id": None,
                "queue_sha256": None,
                "stage": "confirmation_invalidated",
                "progress_percent": 0.0,
                "blocker_code": blocker.code,
                "error_code": blocker.code,
                "recovery_action": recovery_action,
                "retryable": False,
                "can_cancel": False,
                "bundles": [],
                "blocking_reasons": [blocker.code],
                "message": str(blocker),
                "config_lineage_reconfirmation": challenge,
            }
        except BroadcastApiError as exc:
            return {
                "run_id": run_id,
                "status": "blocked",
                "active_job_id": None,
                "generation_id": None,
                "queue_sha256": None,
                "stage": "validation_failed",
                "progress_percent": 0.0,
                "blocker_code": "invalid_or_stale_selective_review_evidence",
                "error_code": "invalid_or_stale_selective_review_evidence",
                "recovery_action": "inspect_or_replace_review_evidence_generation",
                "retryable": False,
                "can_cancel": False,
                "bundles": [],
                "blocking_reasons": ["invalid_or_stale_selective_review_evidence"],
                "message": str(exc),
            }
        if queue is not None:
            assert queue_sha256 is not None
            activation = queue.get("activation") if isinstance(queue.get("activation"), dict) else {}
            return {
                "run_id": run_id,
                "status": "ready",
                "active_job_id": None,
                "generation_id": activation.get("generation_id"),
                "queue_sha256": queue_sha256,
                "stage": "ready",
                "progress_percent": 100.0,
                "blocker_code": None,
                "error_code": None,
                "recovery_action": None,
                "retryable": False,
                "can_cancel": False,
                "bundles": [],
                "blocking_reasons": [],
                "message": None,
            }

        children = self._review_evidence_children(run_id)
        active = next((child for child in reversed(children) if child.get("status") in {"queued", "running"}), None)
        if active is not None:
            metadata = active.get("broadcast") if isinstance(active.get("broadcast"), dict) else {}
            operation_status = metadata.get("operation_status")
            status = (
                operation_status if operation_status in {"queued", "copying", "validating", "committing"} else "copying"
            )
            progress = active.get("progress") if isinstance(active.get("progress"), dict) else {}
            request = metadata.get("request") if isinstance(metadata.get("request"), dict) else {}
            return {
                "run_id": run_id,
                "status": status,
                "active_job_id": active["run_id"],
                "generation_id": None,
                "queue_sha256": None,
                "retry_from_job_id": request.get("retry_from_job_id"),
                "stage": status,
                "progress_percent": float(progress.get("percent") or 0.0),
                "blocker_code": None,
                "error_code": None,
                "recovery_action": None,
                "retryable": False,
                "can_cancel": metadata.get("commit_started") is not True,
                "bundles": [],
                "blocking_reasons": [],
                "message": None,
            }

        bundles = discover_review_evidence_bundles(
            self.review_evidence_inbox_dir,
            run_id=run_id,
            source_sha256=target["source_sha256"],
            root_contract_sha256=target["root_contract_sha256"],
            expected_target=target,
        )
        latest = children[-1] if children else None
        if latest is not None and latest.get("status") in {"failed", "cancelled"}:
            metadata = latest.get("broadcast") if isinstance(latest.get("broadcast"), dict) else {}
            operation_status = metadata.get("operation_status")
            lifecycle = (
                "cancelled"
                if latest.get("status") == "cancelled"
                else "blocked"
                if operation_status == "blocked"
                else "failed"
            )
            error_code = metadata.get("error_code")
            request = metadata.get("request") if isinstance(metadata.get("request"), dict) else {}
            return {
                "run_id": run_id,
                "status": lifecycle,
                "active_job_id": latest["run_id"],
                "generation_id": None,
                "queue_sha256": None,
                "retry_from_job_id": latest["run_id"],
                "stage": lifecycle,
                "progress_percent": 0.0,
                "blocker_code": error_code if lifecycle == "blocked" else None,
                "error_code": error_code,
                "recovery_action": "retry_review_evidence_import",
                "retryable": True,
                "can_cancel": False,
                "bundles": bundles,
                "blocking_reasons": [error_code] if isinstance(error_code, str) and error_code else [],
                "message": latest.get("error"),
            }
        available = [bundle for bundle in bundles if bundle.get("status") == "available"]
        invalid_codes = sorted(
            {
                str(bundle["error_code"])
                for bundle in bundles
                if bundle.get("status") == "invalid" and bundle.get("error_code")
            }
        )
        blocker_code = None if available else "review_evidence_bundle_not_available"
        capacity = None
        if available:
            first_available = available[0]
            capacity = {
                key: first_available.get(key)
                for key in (
                    "total_size_bytes",
                    "required_free_bytes",
                    "available_free_bytes",
                    "attempt_quota_bytes",
                    "capacity_status",
                    "retention",
                    "provisioner_limits",
                )
            }
        return {
            "run_id": run_id,
            "status": "available" if available else "not_available",
            "active_job_id": None,
            "generation_id": None,
            "queue_sha256": None,
            "stage": "available" if available else "not_available",
            "progress_percent": 0.0,
            "blocker_code": blocker_code,
            "error_code": None,
            "recovery_action": "start_review_evidence_import" if available else "provision_qualified_review_evidence",
            "retryable": False,
            "can_cancel": False,
            "bundles": bundles,
            "capacity": capacity,
            "blocking_reasons": [] if available else ["review_evidence_bundle_not_available", *invalid_codes],
            "message": None,
        }

    def import_broadcast_review_evidence(self, run_id: str, request: dict[str, Any]) -> dict[str, Any]:
        self._assert_service_open()
        parent, output_dir = self._broadcast_run_output(run_id)
        target = self._review_evidence_target(run_id, output_dir, parent=parent)
        bundle_id = request.get("bundle_id")
        if not isinstance(bundle_id, str) or not bundle_id.strip():
            raise ValueError("bundle_id is required")
        bundle_id = bundle_id.strip()
        manifest_sha256 = request.get("bundle_manifest_sha256")
        if not isinstance(manifest_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None:
            raise ValueError("bundle_manifest_sha256 must be a lowercase SHA-256")
        retry_from_job_id = request.get("retry_from_job_id")
        if retry_from_job_id is not None and (not isinstance(retry_from_job_id, str) or not retry_from_job_id.strip()):
            raise ValueError("retry_from_job_id must be non-empty text")
        bundles = discover_review_evidence_bundles(
            self.review_evidence_inbox_dir,
            run_id=run_id,
            source_sha256=target["source_sha256"],
            root_contract_sha256=target["root_contract_sha256"],
            expected_target=target,
        )
        matches = [
            item
            for item in bundles
            if item.get("status") == "available"
            and item.get("bundle_id") == bundle_id
            and item.get("bundle_manifest_sha256") == manifest_sha256
        ]
        if len(matches) != 1:
            raise RuntimeError("compatible review evidence bundle is unavailable or ambiguous")
        bundle = matches[0]
        manifest_sha256 = str(bundle["bundle_manifest_sha256"])
        request_identity = {
            "parent_run_id": run_id,
            "bundle_id": bundle_id,
            "bundle_manifest_sha256": manifest_sha256,
            "target": target,
        }
        request_digest = hashlib.sha256(
            json.dumps(request_identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        child_output: Path | None = None
        operation_run_id = ""
        with self._lock:
            self._assert_service_open_locked()
            with self._registry_transaction() as registry:
                existing = [
                    item
                    for item in registry["runs"]
                    if item.get("source") == "broadcast_review_evidence_import"
                    and item.get("parent_run_id") == run_id
                    and isinstance(item.get("broadcast"), dict)
                    and item["broadcast"].get("request_digest") == request_digest
                ]
                completed = next(
                    (
                        item
                        for item in reversed(existing)
                        if item.get("status") == "completed"
                        and not (
                            isinstance(item.get("broadcast"), dict)
                            and isinstance(item["broadcast"].get("result"), dict)
                            and item["broadcast"]["result"].get("status") == "revoked"
                        )
                    ),
                    None,
                )
                if completed is not None:
                    return self._review_evidence_import_response(completed)
                active_same = next(
                    (item for item in reversed(existing) if item.get("status") in {"queued", "running"}), None
                )
                if active_same is not None:
                    return self._review_evidence_import_response(active_same)
                if retry_from_job_id is None and existing:
                    raise RuntimeError("a terminal review evidence import requires explicit retry_from_job_id")
                if retry_from_job_id is not None:
                    retry = next((item for item in existing if item.get("run_id") == retry_from_job_id), None)
                    retry_metadata = retry.get("broadcast") if isinstance(retry, dict) else None
                    if (
                        retry is None
                        or retry.get("status") not in {"failed", "cancelled"}
                        or not isinstance(retry_metadata, dict)
                        or retry_metadata.get("operation_status") not in {"failed", "cancelled", "blocked"}
                    ):
                        raise RuntimeError("retry_from_job_id must identify a matching terminal import")
                active = next((item for item in registry["runs"] if item.get("status") in {"queued", "running"}), None)
                if active is not None:
                    raise RuntimeError(f"Another run is already active: {active.get('run_id')}")
                current_parent = next((item for item in registry["runs"] if item.get("run_id") == run_id), None)
                if (
                    current_parent is None
                    or current_parent.get("source") != "broadcast_hybrid"
                    or current_parent.get("status") != "completed"
                ):
                    raise RuntimeError("review evidence parent must remain a completed broadcast_hybrid run")
                if (output_dir / "review_decisions.json").exists():
                    raise RuntimeError("review evidence is fixed by existing review decisions")
                operation_run_id = f"{run_id}-review-evidence-{uuid4().hex[:8]}"
                input_video = Path(parent["input_video"]).resolve() if parent.get("input_video") else None
                child_output = self._build_run_output_dir(run_id=operation_run_id, input_video=input_video)
                child_output.mkdir(parents=True, exist_ok=False)
                created_at = _utc_now_iso()
                child = {
                    "run_id": operation_run_id,
                    "source": "broadcast_review_evidence_import",
                    "status": "queued",
                    "created_at": created_at,
                    "started_at": None,
                    "completed_at": None,
                    "config_name": parent.get("config_name"),
                    "config_path": parent.get("config_path"),
                    "input_video": parent.get("input_video"),
                    "parent_run_id": run_id,
                    "output_dir": str(child_output),
                    "modules_enabled": {"broadcast_hybrid": True},
                    "artifacts": [],
                    "stats": {},
                    "broadcast": {
                        "operation": "review_evidence_import",
                        "operation_status": "queued",
                        "parent_run_id": run_id,
                        "owner_pid": os.getpid(),
                        "owner_instance_id": self._instance_id,
                        "request": {
                            "bundle_id": bundle_id,
                            "bundle_manifest_sha256": manifest_sha256,
                            "retry_from_job_id": retry_from_job_id,
                        },
                        "request_digest": request_digest,
                        "inbox_entry": bundle["inbox_entry"],
                        "target": target,
                        "commit_started": False,
                    },
                    "progress": self._initial_progress(),
                    "notes": f"Broadcast review evidence import for {run_id}",
                    "error": None,
                }
                self._attach_ai_candidate_lifecycle(child)
                registry["runs"].append(child)
            cancel_event = threading.Event()
            thread = threading.Thread(
                target=self._execute_review_evidence_import,
                args=(operation_run_id, cancel_event),
                name=f"football-tracking-review-evidence-{operation_run_id}",
                daemon=True,
            )
            self._active_threads[operation_run_id] = thread
            self._cancel_events[operation_run_id] = cancel_event
        assert child_output is not None
        try:
            self._start_thread_or_cleanup(operation_run_id, thread, output_dir=child_output, remove_output=True)
        except BaseException:
            raise
        return {
            "run_id": operation_run_id,
            "parent_run_id": run_id,
            "status": "queued",
            "reason": "review_evidence_import_queued",
            "artifact": None,
            "generation_id": None,
            "details": {"bundle_manifest_sha256": manifest_sha256},
        }

    def _execute_review_evidence_import(self, operation_run_id: str, cancel_event: threading.Event) -> None:
        child_output: Path | None = None
        parent_run_id = ""
        try:
            with self._lock:
                with self._registry_transaction() as registry:
                    child = next((item for item in registry["runs"] if item.get("run_id") == operation_run_id), None)
                    if child is None or child.get("source") != "broadcast_review_evidence_import":
                        raise RuntimeError("review evidence import lineage disappeared")
                    metadata = child.get("broadcast") if isinstance(child.get("broadcast"), dict) else {}
                    if child.get("status") != "queued":
                        raise RuntimeError("review evidence import is no longer queued")
                    if cancel_event.is_set() or metadata.get("cancel_requested") is True:
                        raise CancelledError()
                    parent_run_id = str(child.get("parent_run_id") or "")
                    child_output = Path(child["output_dir"]).resolve()
                    child.update(
                        {
                            "status": "running",
                            "started_at": _utc_now_iso(),
                            "broadcast": {
                                **metadata,
                                "operation_status": "copying",
                                "owner_pid": os.getpid(),
                                "owner_instance_id": self._instance_id,
                                "worker_exited": False,
                            },
                            "progress": {
                                **self._initial_progress(),
                                "stage": "review_evidence_copying",
                                "percent": 10.0,
                            },
                        }
                    )
                    metadata = deepcopy(child["broadcast"])
            parent = self.get_run(parent_run_id)
            parent_output = Path(parent["output_dir"]).resolve()
            inbox_entry = metadata.get("inbox_entry")
            if not isinstance(inbox_entry, str) or Path(inbox_entry).name != inbox_entry:
                raise ReviewEvidenceBundleError("unsafe_bundle_path", "review evidence inbox identity is invalid")
            bundle_dir = self.review_evidence_inbox_dir / inbox_entry

            def should_cancel() -> bool:
                if cancel_event.is_set():
                    return True
                with self._registry_file_lock():
                    registry = self._read_registry()
                current = next(
                    (item for item in registry["runs"] if item.get("run_id") == operation_run_id),
                    None,
                )
                current_metadata = current.get("broadcast") if isinstance(current, dict) else None
                return not isinstance(current_metadata, dict) or current_metadata.get("cancel_requested") is True

            def stage_updated(stage: str, percent: float) -> None:
                with self._lock:
                    with self._registry_transaction() as registry:
                        current = next(
                            (item for item in registry["runs"] if item.get("run_id") == operation_run_id), None
                        )
                        if current is None:
                            raise RuntimeError("review evidence import disappeared during validation")
                        current_metadata = (
                            current.get("broadcast") if isinstance(current.get("broadcast"), dict) else {}
                        )
                        if current_metadata.get("commit_started") is True:
                            raise RuntimeError("review evidence import validation advanced after commit")
                        current["broadcast"] = {**current_metadata, "operation_status": stage}
                        current["progress"] = {
                            **self._initial_progress(),
                            "stage": f"review_evidence_{stage}",
                            "percent": percent,
                        }

            def commit_started() -> None:
                with self._lock:
                    with self._registry_transaction() as registry:
                        current = next(
                            (item for item in registry["runs"] if item.get("run_id") == operation_run_id), None
                        )
                        current_parent = next(
                            (item for item in registry["runs"] if item.get("run_id") == parent_run_id), None
                        )
                        if current is None:
                            raise RuntimeError("review evidence import disappeared before commit")
                        if current_parent is None:
                            raise RuntimeError("review evidence parent disappeared before commit")
                        current_metadata = (
                            current.get("broadcast") if isinstance(current.get("broadcast"), dict) else {}
                        )
                        if cancel_event.is_set() or current_metadata.get("cancel_requested") is True:
                            raise CancelledError()
                        current_target = self._review_evidence_target(
                            parent_run_id,
                            parent_output,
                            parent=current_parent,
                            registry=registry,
                        )
                        if current_target != target:
                            raise ReviewEvidenceBundleError(
                                "target_binding_mismatch",
                                "review evidence target context changed before commit",
                            )
                        current["broadcast"] = {
                            **current_metadata,
                            "operation_status": "committing",
                            "commit_started": True,
                        }
                        current["progress"] = {
                            **self._initial_progress(),
                            "stage": "review_evidence_committing",
                            "percent": 90.0,
                        }

            target = metadata.get("target") if isinstance(metadata.get("target"), dict) else {}
            activation = activate_review_evidence_bundle(
                bundle_dir,
                parent_output,
                expected_run_id=parent_run_id,
                expected_source_sha256=str(target.get("source_sha256") or ""),
                expected_root_contract_sha256=str(target.get("root_contract_sha256") or ""),
                expected_target=target,
                expected_bundle_id=str(metadata.get("request", {}).get("bundle_id") or ""),
                expected_bundle_manifest_sha256=str(metadata.get("request", {}).get("bundle_manifest_sha256") or ""),
                should_cancel=should_cancel,
                on_stage=stage_updated,
                on_commit_started=commit_started,
                trusted_prelabel_commitment_root=self.target_prelabel_commitment_registry,
            )
            report = {
                "schema_version": "1.0",
                "artifact_type": "broadcast_review_evidence_import_report",
                "status": "succeeded",
                "operation_run_id": operation_run_id,
                "parent_run_id": parent_run_id,
                "request_digest": metadata.get("request_digest"),
                "generation_id": activation.generation_id,
                "queue_sha256": activation.queue_sha256,
                "idempotent_activation": activation.idempotent,
            }
            assert child_output is not None
            report_sha256 = publish_json_exclusive(
                child_output / "review_evidence_import_report.v1.json", report, trusted_root=child_output
            )
            quality_report = publish_broadcast_facade(parent_output)
            completed_at = _utc_now_iso()
            with self._lock:
                with self._registry_transaction() as registry:
                    child = next((item for item in registry["runs"] if item.get("run_id") == operation_run_id), None)
                    parent = next((item for item in registry["runs"] if item.get("run_id") == parent_run_id), None)
                    if child is None or parent is None:
                        raise RuntimeError("review evidence import registry lineage disappeared after commit")
                    child_metadata = child.get("broadcast") if isinstance(child.get("broadcast"), dict) else {}
                    child.update(
                        {
                            "status": "completed",
                            "completed_at": completed_at,
                            "error": None,
                            "broadcast": {
                                **child_metadata,
                                "operation_status": "completed",
                                "worker_exited": True,
                                "result": {
                                    "status": "completed",
                                    "review_evidence_generation_id": activation.generation_id,
                                    "queue_sha256": activation.queue_sha256,
                                    "report_sha256": report_sha256,
                                },
                            },
                            "progress": self._completed_progress(child.get("progress"), child.get("started_at")),
                        }
                    )
                    parent_broadcast = parent.get("broadcast") if isinstance(parent.get("broadcast"), dict) else {}
                    preserved_blockers = [
                        reason
                        for reason in parent_broadcast.get("blocking_reasons", [])
                        if reason
                        not in {
                            "missing_qualified_selective_review_queue",
                            "invalid_or_stale_selective_review_evidence",
                            "review_evidence_bundle_not_available",
                        }
                    ]
                    merged_blockers = list(
                        dict.fromkeys([*preserved_blockers, *(quality_report.get("blocking_reasons") or [])])
                    )
                    merged_limitations = list(
                        dict.fromkeys(
                            [
                                *(parent_broadcast.get("limitations") or []),
                                *(quality_report.get("limitations") or []),
                            ]
                        )
                    )
                    parent["broadcast"] = {
                        **parent_broadcast,
                        "status": "needs_review",
                        "blocking_reasons": merged_blockers,
                        "limitations": merged_limitations,
                        "status_generation": quality_report.get("status_generation"),
                        "review_evidence": {
                            "status": "ready",
                            "generation_id": activation.generation_id,
                            "queue_sha256": activation.queue_sha256,
                            "operation_run_id": operation_run_id,
                        },
                    }
                    child["artifacts"] = self._collect_artifacts(child_output)
                    child["stats"] = self._collect_stats(child_output)
                    parent["artifacts"] = self._collect_artifacts(parent_output)
                    parent["stats"] = self._collect_stats(parent_output)
        except (CancelledError, ReviewEvidenceBundleError, RuntimeError, OSError, ValueError) as exc:
            error_code = (
                "review_evidence_import_cancelled"
                if isinstance(exc, CancelledError)
                else (
                    exc.code
                    if isinstance(exc, (ReviewEvidenceBundleError, _ReviewEvidenceTargetContextError))
                    else "review_evidence_import_failed"
                )
            )
            cancelled = error_code == "review_evidence_import_cancelled"
            blocked_codes = {
                "insufficient_review_evidence_capacity",
                "review_evidence_fixed",
                "review_evidence_conflict",
                "target_binding_mismatch",
                _CONFIRMED_CONFIG_CHANGED_AFTER_CONFIRMATION,
                *_CONFIG_LINEAGE_BLOCKERS,
            }
            with self._lock:
                with self._registry_transaction() as registry:
                    child = next((item for item in registry["runs"] if item.get("run_id") == operation_run_id), None)
                    if child is not None and child.get("status") in {"queued", "running"}:
                        metadata = child.get("broadcast") if isinstance(child.get("broadcast"), dict) else {}
                        child.update(
                            {
                                "status": "cancelled" if cancelled else "failed",
                                "completed_at": _utc_now_iso(),
                                "error": str(exc) or error_code,
                                "broadcast": {
                                    **metadata,
                                    "operation_status": (
                                        "cancelled"
                                        if cancelled
                                        else "blocked"
                                        if error_code in blocked_codes
                                        else "failed"
                                    ),
                                    "error_code": error_code,
                                    "worker_exited": True,
                                },
                                "progress": (
                                    self._cancelled_progress(child.get("progress"), child.get("started_at"))
                                    if cancelled
                                    else self._failed_progress(child.get("progress"), child.get("started_at"))
                                ),
                            }
                        )
                        if child_output is not None:
                            artifact_error = self._write_run_artifacts(child_output, child)
                            child["error"] = self._append_artifact_error(child.get("error"), artifact_error)
                            child["artifacts"] = self._collect_artifacts(child_output)
                            child["stats"] = self._collect_stats(child_output)
        finally:
            with self._lock:
                self._active_threads.pop(operation_run_id, None)
                self._cancel_events.pop(operation_run_id, None)

    def _review_evidence_target(
        self,
        run_id: str,
        output_dir: Path,
        *,
        parent: dict[str, Any] | None = None,
        registry: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        contract_path = output_dir / TRACKING_CONTRACT_REPORT_NAME
        try:
            contract, contract_sha256 = load_bound_json(contract_path, "review evidence target contract")
        except BroadcastApiError as exc:
            raise RuntimeError(f"review evidence target contract is unavailable: {exc}") from exc
        source = contract.get("source") if isinstance(contract.get("source"), dict) else {}
        source_sha256 = source.get("video_sha256")
        if not isinstance(source_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None:
            raise RuntimeError("review evidence target contract has no source video SHA-256")
        parent = parent or self.get_run(run_id)
        broadcast = parent.get("broadcast") if isinstance(parent.get("broadcast"), dict) else {}
        quality_profile = broadcast.get("quality_profile")
        max_windows = broadcast.get("max_manual_review_windows")
        if not isinstance(quality_profile, str) or not quality_profile:
            raise RuntimeError("review evidence target quality profile is unavailable")
        if isinstance(max_windows, bool) or not isinstance(max_windows, int) or not 1 <= max_windows <= 30:
            raise RuntimeError("review evidence target review window limit is unavailable")
        action_binding_path = output_dir / "action_signal_binding.v1.json"
        if not action_binding_path.is_file():
            raise RuntimeError("review evidence target action-signal binding is unavailable")
        action_signal_binding_sha256 = sha256_file(action_binding_path)
        config_confirmation = self._review_evidence_config_confirmation(parent, registry=registry)
        confirmed_config_sha256 = config_confirmation["confirmed_text_sha256"]
        candidate_rows = contract.get("candidates")
        if not isinstance(candidate_rows, list) or not candidate_rows:
            raise RuntimeError("review evidence target candidate population is empty")
        population = []
        seen_candidate_ids: set[str] = set()
        for raw_candidate in candidate_rows:
            if not isinstance(raw_candidate, dict):
                raise RuntimeError("review evidence target candidate population is invalid")
            candidate_id = raw_candidate.get("candidate_id")
            if not isinstance(candidate_id, str) or not candidate_id or candidate_id in seen_candidate_ids:
                raise RuntimeError("review evidence target candidate identity is invalid")
            seen_candidate_ids.add(candidate_id)
            raw_bbox = raw_candidate.get("bbox")
            raw_confidence = raw_candidate.get("confidence")
            if (
                not isinstance(raw_bbox, list)
                or len(raw_bbox) != 4
                or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in raw_bbox)
                or isinstance(raw_confidence, bool)
                or not isinstance(raw_confidence, (int, float))
            ):
                raise RuntimeError("review evidence target candidate geometry is invalid")
            identity = {
                "candidate_id": candidate_id,
                "frame_index": raw_candidate.get("frame_index"),
                "bbox": [float(value) for value in raw_bbox],
                "detector_source": raw_candidate.get("source"),
                "confidence": float(raw_confidence),
            }
            fingerprint = hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
            ).hexdigest()
            population.append({"candidate_id": candidate_id, "candidate_fingerprint": fingerprint})
        population.sort(key=lambda row: row["candidate_id"])
        candidate_population_sha256 = hashlib.sha256(
            json.dumps(population, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()
        profile_context = {
            "quality_profile": quality_profile,
            "max_manual_review_windows": max_windows,
            "confirmed_config_sha256": confirmed_config_sha256,
            "action_signal_binding_sha256": action_signal_binding_sha256,
            "preflight": broadcast.get("preflight"),
        }
        profile_digest = hashlib.sha256(
            json.dumps(profile_context, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()
        target = {
            "run_id": run_id,
            "source_sha256": source_sha256,
            "root_contract_sha256": contract_sha256,
            "action_signal_binding_sha256": action_signal_binding_sha256,
            "confirmed_config_sha256": confirmed_config_sha256,
            "profile_digest": profile_digest,
            "quality_profile": quality_profile,
            "max_review_windows": max_windows,
            "max_manual_review_windows": max_windows,
            "provisioner_version": PROVISIONER_VERSION,
            "candidate_population_sha256": candidate_population_sha256,
            "candidate_population_count": len(population),
        }
        projection = config_confirmation.get("projection")
        if isinstance(projection, dict):
            target["config_lineage"] = projection
        return target

    @staticmethod
    def _review_evidence_parent_from_registry(
        registry: dict[str, Any],
        run_id: str,
        output_dir: Path,
    ) -> dict[str, Any]:
        parent = next((item for item in registry["runs"] if item.get("run_id") == run_id), None)
        if parent is None or parent.get("source") != "broadcast_hybrid":
            raise KeyError(run_id)
        if parent.get("status") != "completed":
            raise RuntimeError(f"Broadcast run must be completed before review operations: {run_id}")
        if Path(parent["output_dir"]).resolve() != output_dir:
            raise RuntimeError("broadcast review output changed during validation")
        return parent

    def _validate_current_review_queue_locked(
        self,
        run_id: str,
        output_dir: Path,
        parent: dict[str, Any],
        queue_path: Path,
        *,
        registry: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        queue, queue_sha256 = validate_review_queue_activation(output_dir, queue_path)
        if not isinstance(queue.get("activation"), dict):
            return queue, queue_sha256
        target_before = self._review_evidence_target(
            run_id,
            output_dir,
            parent=parent,
            registry=registry,
        )
        queue, queue_sha256 = validate_review_queue_activation(
            output_dir,
            queue_path,
            expected_target=target_before,
        )
        target_after = self._review_evidence_target(
            run_id,
            output_dir,
            parent=parent,
            registry=registry,
        )
        if target_after != target_before:
            raise BroadcastApiError("review evidence target changed during activation validation")
        return queue, queue_sha256

    @staticmethod
    def _apply_revoked_review_evidence_state(
        parent: dict[str, Any],
        quality_report: dict[str, Any],
        *,
        generation_id: str,
        queue_sha256: str,
        revoked_at: Any,
    ) -> None:
        parent_broadcast = parent.get("broadcast") if isinstance(parent.get("broadcast"), dict) else {}
        preserved_blockers = [
            reason
            for reason in parent_broadcast.get("blocking_reasons", [])
            if reason
            not in {
                "missing_qualified_selective_review_queue",
                "invalid_or_stale_selective_review_evidence",
                "review_evidence_bundle_not_available",
            }
        ]
        parent["broadcast"] = {
            **parent_broadcast,
            "status": quality_report.get("status"),
            "blocking_reasons": list(
                dict.fromkeys([*preserved_blockers, *(quality_report.get("blocking_reasons") or [])])
            ),
            "limitations": list(
                dict.fromkeys(
                    [
                        *(parent_broadcast.get("limitations") or []),
                        *(quality_report.get("limitations") or []),
                    ]
                )
            ),
            "status_generation": quality_report.get("status_generation"),
            "review_evidence": {
                "status": "revoked",
                "generation_id": generation_id,
                "queue_sha256": queue_sha256,
                "revoked_at": revoked_at,
            },
        }

    def _review_evidence_confirmed_config_sha256(self, parent: dict[str, Any]) -> str:
        return self._review_evidence_config_confirmation(parent)["confirmed_text_sha256"]

    def _review_evidence_config_confirmation(
        self,
        parent: dict[str, Any],
        *,
        registry: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        notes = parent.get("notes")
        try:
            confirmation = json.loads(notes) if isinstance(notes, str) else notes
        except json.JSONDecodeError as exc:
            raise RuntimeError("review evidence target confirmation record is unavailable") from exc
        if not isinstance(confirmation, dict):
            raise RuntimeError("review evidence target confirmation record is unavailable")
        expected_sha256 = confirmation.get("expected_config_sha256")
        if not isinstance(expected_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
            raise RuntimeError("review evidence target confirmation record has no config SHA-256")

        config_path_value = parent.get("config_path")
        config_path = Path(config_path_value) if isinstance(config_path_value, str) else None
        if config_path is None:
            raise RuntimeError("review evidence target confirmed config is unavailable")
        try:
            _raw_config, inspection = capture_config_bytes(self.config_dir, config_path)
        except (OSError, ConfigLineageError) as exc:
            code = exc.code if isinstance(exc, ConfigLineageError) else CONFIG_LINEAGE_UNSAFE
            raise _ReviewEvidenceTargetContextError(
                code,
                "confirmed config lineage snapshot is unsafe or unreadable",
            ) from exc
        if inspection.observed_raw_sha256 == expected_sha256:
            return {"confirmed_text_sha256": expected_sha256, "projection": None}
        if inspection.confirmed_text_sha256 != expected_sha256:
            raise _ReviewEvidenceTargetContextError(
                CONFIG_LINEAGE_MISMATCH,
                "confirmed config lineage snapshot does not match the confirmed text",
            )
        if registry is None:
            with self._lock:
                registry = self._read_registry()
        child = self._config_lineage_child_from_registry(
            registry,
            str(parent.get("run_id") or ""),
        )
        if child is None:
            raise _ReviewEvidenceTargetContextError(
                CONFIG_LINEAGE_REQUIRED,
                "confirmed config lineage reconfirmation is required",
            )
        metadata = child.get("broadcast") if isinstance(child.get("broadcast"), dict) else {}
        workflow_bindings = metadata.get("workflow_bindings")
        if not isinstance(workflow_bindings, dict):
            raise _ReviewEvidenceTargetContextError(
                CONFIG_LINEAGE_MISMATCH,
                "config lineage operation has no authoritative workflow bindings",
            )
        try:
            authoritative_workflow_bindings = self._derive_config_lineage_workflow_bindings(
                parent,
                config_path,
                registry=registry,
            )
            if workflow_bindings != authoritative_workflow_bindings:
                raise ValueError("config lineage operation workflow bindings differ from server-derived authority")
        except (ValueError, RuntimeError, ConfigLineageError) as exc:
            raise _ReviewEvidenceTargetContextError(CONFIG_LINEAGE_MISMATCH, str(exc)) from exc
        try:
            generation = load_config_lineage_reconfirmation(
                self._config_lineage_root(),
                target_run_id=str(parent.get("run_id") or ""),
                trusted_config_root=self.config_dir,
                observed_config_path=config_path,
                confirmed_config_name=str(confirmation.get("confirmed_config_name") or config_path.name),
                confirmed_text_sha256=expected_sha256,
                expected_workflow_bindings=authoritative_workflow_bindings,
            )
        except ConfigLineageError as exc:
            raise _ReviewEvidenceTargetContextError(exc.code, str(exc)) from exc
        manifest_sha256 = generation.manifest_sha256
        canonical_snapshot_sha256 = generation.canonical_snapshot_sha256
        if (
            not isinstance(manifest_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None
            or not isinstance(canonical_snapshot_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", canonical_snapshot_sha256) is None
        ):
            raise _ReviewEvidenceTargetContextError(
                CONFIG_LINEAGE_UNSAFE,
                "config lineage secure read digests are unavailable",
            )
        if (
            metadata.get("generation_id") != generation.generation_id
            or metadata.get("manifest_sha256") != manifest_sha256
            or metadata.get("canonical_snapshot_sha256") != canonical_snapshot_sha256
        ):
            raise _ReviewEvidenceTargetContextError(
                CONFIG_LINEAGE_MISMATCH,
                "config lineage operation and immutable generation do not match",
            )
        projection = {
            "confirmed_text_sha256": expected_sha256,
            "observed_raw_sha256": inspection.observed_raw_sha256,
            "canonical_snapshot_sha256": generation.manifest["projection"]["canonical_snapshot_sha256"],
            "generation_id": generation.generation_id,
            "manifest_sha256": manifest_sha256,
            "historical_raw_snapshot_observed": False,
        }
        return {"confirmed_text_sha256": expected_sha256, "projection": projection}

    def _config_lineage_root(self) -> Path:
        return self.outputs_dir / "config_lineage_reconfirmations"

    def _config_lineage_child(self, parent_run_id: str) -> dict[str, Any] | None:
        with self._lock:
            registry = self._read_registry()
            return self._config_lineage_child_from_registry(registry, parent_run_id)

    @staticmethod
    def _config_lineage_child_from_registry(
        registry: dict[str, Any],
        parent_run_id: str,
    ) -> dict[str, Any] | None:
        matches = [
            deepcopy(item)
            for item in registry["runs"]
            if item.get("source") == "config_lineage_reconfirmation"
            and item.get("parent_run_id") == parent_run_id
            and item.get("status") == "completed"
        ]
        return matches[-1] if matches else None

    def _derive_config_lineage_workflow_bindings(
        self,
        parent: dict[str, Any],
        config_path: Path,
        *,
        registry: dict[str, Any],
    ) -> dict[str, Any]:
        """Rebuild config-lineage workflow identity only from server-owned evidence."""

        try:
            raw_config, inspection = capture_config_bytes(self.config_dir, config_path)
            loaded = yaml.safe_load(inspection.canonical_bytes.decode("utf-8")) or {}
        except (OSError, UnicodeDecodeError, yaml.YAMLError, ConfigLineageError) as exc:
            raise ConfigLineageError(
                CONFIG_LINEAGE_UNSAFE,
                "server cannot read authoritative production workflow metadata",
            ) from exc
        if not isinstance(loaded, dict):
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                "confirmed config must contain an object",
            )
        metadata = loaded.get("metadata")
        workflow = metadata.get("production_workflow") if isinstance(metadata, dict) else None
        required_workflow_fields = {
            "workflow_id",
            "accepted_trial_run_id",
            "trial_request_sha256",
            "trial_intent_sha256",
            "trial_patch_sha256",
            "patch_sha256",
            "calibration_digest",
            "source_signature",
        }
        if not isinstance(workflow, dict) or not required_workflow_fields.issubset(workflow):
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                "confirmed config production_workflow metadata is incomplete",
            )
        workflow_id = self._config_lineage_authority_text(
            workflow.get("workflow_id"),
            "workflow_id",
        )
        accepted_trial_run_id = self._config_lineage_authority_text(
            workflow.get("accepted_trial_run_id"),
            "accepted_trial_run_id",
        )
        authoritative_hashes = {
            field: self._config_lineage_authority_sha256(workflow.get(field), field)
            for field in (
                "trial_request_sha256",
                "trial_intent_sha256",
                "trial_patch_sha256",
                "patch_sha256",
                "calibration_digest",
            )
        }
        source_signature = workflow.get("source_signature")
        if not isinstance(source_signature, dict) or set(source_signature) != {
            "path",
            "size_bytes",
            "modified_at",
        }:
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                "confirmed config source_signature must exactly bind path, size_bytes, and modified_at",
            )
        source_path_value = source_signature.get("path")
        source_size = source_signature.get("size_bytes")
        source_modified_at = source_signature.get("modified_at")
        if (
            not isinstance(source_path_value, str)
            or not source_path_value
            or isinstance(source_size, bool)
            or not isinstance(source_size, int)
            or source_size < 0
            or not isinstance(source_modified_at, str)
            or not source_modified_at.strip()
        ):
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                "confirmed config source_signature values are invalid",
            )
        parent_source_value = parent.get("input_video")
        if not isinstance(parent_source_value, str) or not parent_source_value:
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                "target full run has no authoritative source path",
            )
        expected_source = Path(os.path.abspath(parent_source_value))
        declared_source = Path(os.path.abspath(source_path_value))
        try:
            source_stat = capture_regular_file_stat(expected_source)
        except ConfigLineageError as exc:
            raise ConfigLineageError(
                CONFIG_LINEAGE_UNSAFE,
                "authoritative production source is unavailable",
            ) from exc
        actual_modified_at = datetime.fromtimestamp(
            source_stat["mtime_ns"] / 1_000_000_000,
            tz=timezone.utc,
        ).isoformat()
        if (
            declared_source != expected_source
            or source_stat["size_bytes"] != source_size
            or actual_modified_at != source_modified_at
        ):
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                "confirmed config source_signature does not match the actual production source",
            )
        source_signature_sha256 = hashlib.sha256(
            json.dumps(
                source_signature,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

        runs = [
            item for item in registry.get("runs", []) if isinstance(item, dict) and isinstance(item.get("run_id"), str)
        ]
        runs_by_id = {item["run_id"]: item for item in runs}
        accepted_trial = runs_by_id.get(accepted_trial_run_id)
        if accepted_trial is None:
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                "authoritative accepted trial is unavailable",
            )
        accepted_notes = self._config_lineage_authority_notes(
            accepted_trial,
            "accepted trial",
        )
        if (
            accepted_notes.get("purpose") != "trial"
            or accepted_notes.get("workflow_id") != workflow_id
            or accepted_notes.get("trial_intent_sha256") != authoritative_hashes["trial_intent_sha256"]
            or accepted_notes.get("calibration_digest") != authoritative_hashes["calibration_digest"]
        ):
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                "accepted trial notes do not match confirmed production workflow metadata",
            )

        full_runs: list[dict[str, Any]] = []
        for run in runs:
            try:
                notes = self._config_lineage_authority_notes(run, "full run")
            except ConfigLineageError:
                continue
            if (
                notes.get("purpose") == "production_full"
                and notes.get("workflow_id") == workflow_id
                and run.get("status") in {"failed", "completed"}
            ):
                full_runs.append(run)
        if len(full_runs) != 2 or str(parent.get("run_id") or "") not in {
            str(run.get("run_id") or "") for run in full_runs
        }:
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                "authoritative workflow must contain exactly the failed and completed full runs",
            )
        for full_run in full_runs:
            notes = self._config_lineage_authority_notes(full_run, "full run")
            if (
                notes.get("workflow_id") != workflow_id
                or notes.get("expected_config_sha256") != inspection.confirmed_text_sha256
                or notes.get("calibration_digest") != authoritative_hashes["calibration_digest"]
                or notes.get("source_signature_sha256") != source_signature_sha256
                or notes.get("accepted_trial_run_id") != accepted_trial_run_id
                or notes.get("trial_request_sha256") != authoritative_hashes["trial_request_sha256"]
            ):
                raise ConfigLineageError(
                    CONFIG_LINEAGE_MISMATCH,
                    "full-run notes do not match confirmed production workflow metadata",
                )
        full_runs.sort(
            key=lambda run: (
                0 if run.get("status") == "failed" else 1,
                str(run.get("run_id") or ""),
            )
        )
        result = {
            "workflow_id": workflow_id,
            "accepted_trial": self._config_lineage_run_binding(
                accepted_trial,
                accepted_trial=True,
            ),
            "request": {"sha256": authoritative_hashes["trial_request_sha256"]},
            "intent": {"sha256": authoritative_hashes["trial_intent_sha256"]},
            "trial_patch": {"sha256": authoritative_hashes["trial_patch_sha256"]},
            "production_patch": {"sha256": authoritative_hashes["patch_sha256"]},
            "calibration": {"sha256": authoritative_hashes["calibration_digest"]},
            "source_signature": {"sha256": source_signature_sha256},
            "historical_full_runs": [self._config_lineage_run_binding(run, accepted_trial=False) for run in full_runs],
        }
        try:
            self._validate_config_lineage_registry_bindings(
                str(parent.get("run_id") or ""),
                result,
                registry=registry,
            )
        except (ValueError, RuntimeError) as exc:
            raise ConfigLineageError(CONFIG_LINEAGE_MISMATCH, str(exc)) from exc
        return result

    @staticmethod
    def _config_lineage_authority_notes(
        run: dict[str, Any],
        label: str,
    ) -> dict[str, Any]:
        notes = run.get("notes")
        try:
            parsed = json.loads(notes) if isinstance(notes, str) else notes
        except json.JSONDecodeError as exc:
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                f"{label} notes are invalid",
            ) from exc
        if not isinstance(parsed, dict):
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                f"{label} notes are unavailable",
            )
        return parsed

    @staticmethod
    def _config_lineage_authority_text(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                f"production_workflow {label} must be non-empty trimmed text",
            )
        return value

    @staticmethod
    def _config_lineage_authority_sha256(value: Any, label: str) -> str:
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                f"production_workflow {label} must be a lowercase SHA-256",
            )
        return value

    def _validate_config_lineage_registry_bindings(
        self,
        parent_run_id: str,
        workflow_bindings: dict[str, Any],
        *,
        registry: dict[str, Any],
    ) -> None:
        historical = workflow_bindings.get("historical_full_runs")
        accepted = workflow_bindings.get("accepted_trial")
        if not isinstance(historical, list) or len(historical) != 2 or not isinstance(accepted, dict):
            raise ValueError("config lineage workflow history is incomplete")
        runs_by_id = {
            item.get("run_id"): item
            for item in registry.get("runs", [])
            if isinstance(item, dict) and isinstance(item.get("run_id"), str)
        }
        if parent_run_id not in {item.get("run_id") for item in historical if isinstance(item, dict)}:
            raise ValueError("config lineage history must include the target full run")
        accepted_run = runs_by_id.get(accepted.get("run_id"))
        if accepted_run is None:
            raise ValueError("config lineage accepted trial is unavailable")
        self._validate_config_lineage_run_identity(accepted, accepted_run, accepted_trial=True)
        for declared in historical:
            if not isinstance(declared, dict):
                raise ValueError("config lineage historical full-run identity is invalid")
            run = runs_by_id.get(declared.get("run_id"))
            if run is None:
                raise ValueError("config lineage historical full run is unavailable")
            self._validate_config_lineage_run_identity(declared, run, accepted_trial=False)

    @staticmethod
    def _validate_config_lineage_run_identity(
        declared: dict[str, Any],
        run: dict[str, Any],
        *,
        accepted_trial: bool,
    ) -> None:
        expected = ApiService._config_lineage_run_binding(run, accepted_trial=accepted_trial)
        if declared != expected:
            raise ValueError("config lineage run, notes, status, or generation identity mismatch")

    @staticmethod
    def _config_lineage_run_binding(
        run: dict[str, Any],
        *,
        accepted_trial: bool,
    ) -> dict[str, Any]:
        notes = run.get("notes")
        notes_bytes = (
            notes.encode("utf-8")
            if isinstance(notes, str)
            else json.dumps(notes, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        )
        broadcast = run.get("broadcast") if isinstance(run.get("broadcast"), dict) else {}
        stable_identity = {
            "run_id": run.get("run_id"),
            "source": run.get("source"),
            "status": run.get("status"),
            "created_at": run.get("created_at"),
            "completed_at": run.get("completed_at"),
            "config_name": run.get("config_name"),
            "config_path": run.get("config_path"),
            "input_video": run.get("input_video"),
            "parent_run_id": run.get("parent_run_id"),
            "output_dir": run.get("output_dir"),
            "submission_id": broadcast.get("submission_id"),
            "generation_id": broadcast.get("generation_id"),
            "notes_sha256": hashlib.sha256(notes_bytes).hexdigest(),
        }
        record_sha256 = hashlib.sha256(
            json.dumps(
                stable_identity,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        result = {
            "run_id": run.get("run_id"),
            "record_sha256": record_sha256,
            "notes_sha256": stable_identity["notes_sha256"],
        }
        if not accepted_trial:
            result.update(
                {
                    "submission_id": broadcast.get("submission_id"),
                    "generation_id": broadcast.get("generation_id"),
                    "status": run.get("status"),
                }
            )
        return result

    def _review_evidence_children(self, parent_run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            registry = self._read_registry()
            children = [
                deepcopy(item)
                for item in registry["runs"]
                if item.get("source") == "broadcast_review_evidence_import"
                and item.get("parent_run_id") == parent_run_id
            ]
        return sorted(children, key=lambda item: str(item.get("created_at") or ""))

    def _recover_interrupted_review_evidence_imports(self) -> None:
        """Convert orphaned imports into an authoritative completion or an explicit retryable failure."""

        with self._lock:
            with self._registry_transaction() as registry:
                for child in registry["runs"]:
                    if child.get("source") != "broadcast_review_evidence_import" or child.get("status") == "completed":
                        continue
                    metadata = child.get("broadcast") if isinstance(child.get("broadcast"), dict) else {}
                    was_active = child.get("status") in {"queued", "running"}
                    if was_active and self._owner_lease_is_active(
                        metadata.get("owner_pid"), metadata.get("owner_instance_id")
                    ):
                        continue
                    parent = next(
                        (item for item in registry["runs"] if item.get("run_id") == child.get("parent_run_id")), None
                    )
                    completed_at = _utc_now_iso()
                    recovered_result: dict[str, Any] | None = None
                    target_context_error: _ReviewEvidenceTargetContextError | None = None
                    if parent is not None:
                        parent_output = Path(parent["output_dir"]).resolve()
                        queue_path = parent_output / "selective_review_queue.v1.json"
                        if queue_path.is_file():
                            try:
                                queue, queue_sha256 = self._validate_current_review_queue_locked(
                                    str(child.get("parent_run_id") or ""),
                                    parent_output,
                                    parent,
                                    queue_path,
                                    registry=registry,
                                )
                                activation = (
                                    queue.get("activation") if isinstance(queue.get("activation"), dict) else {}
                                )
                                request = metadata.get("request") if isinstance(metadata.get("request"), dict) else {}
                                generation_id = activation.get("generation_id")
                                activation_manifest_path = (
                                    parent_output
                                    / "review_evidence"
                                    / "generations"
                                    / str(generation_id)
                                    / "review_evidence_activation.v1.json"
                                )
                                activation_manifest, _ = load_bound_json(
                                    activation_manifest_path,
                                    "review evidence activation",
                                )
                                request_identity = (
                                    activation_manifest.get("request_identity")
                                    if isinstance(activation_manifest.get("request_identity"), dict)
                                    else {}
                                )
                                if activation.get("bundle_id") == request.get("bundle_id") and request_identity.get(
                                    "bundle_manifest_sha256"
                                ) == request.get("bundle_manifest_sha256"):
                                    recovered_result = {
                                        "status": "completed",
                                        "review_evidence_generation_id": activation.get("generation_id"),
                                        "queue_sha256": queue_sha256,
                                    }
                            except _ReviewEvidenceTargetContextError as exc:
                                target_context_error = exc
                            except (BroadcastApiError, OSError, ValueError):
                                recovered_result = None
                    output_dir = Path(child["output_dir"]).resolve()
                    if target_context_error is not None:
                        blocker = ConfigLineageError(target_context_error.code, str(target_context_error))
                        challenge: dict[str, Any] | None = None
                        if target_context_error.code == CONFIG_LINEAGE_REQUIRED:
                            assert parent is not None
                            try:
                                challenge = self._broadcast_config_lineage_reconfirmation_challenge(
                                    parent,
                                    registry=registry,
                                )
                            except ConfigLineageError as challenge_error:
                                blocker = challenge_error
                        recovery_action = (
                            "reconfirm_production_config"
                            if blocker.code == CONFIG_LINEAGE_REQUIRED and challenge is not None
                            else "inspect_production_config_lineage"
                        )
                        error = str(blocker) or blocker.code
                        child.update(
                            {
                                "status": "failed",
                                "completed_at": child.get("completed_at") or completed_at,
                                "error": error,
                                "broadcast": {
                                    **metadata,
                                    "operation_status": "blocked",
                                    "blocker_code": blocker.code,
                                    "error_code": blocker.code,
                                    "recovery_action": recovery_action,
                                    "recovered": True,
                                    "worker_exited": True,
                                },
                                "progress": self._failed_progress(
                                    child.get("progress"),
                                    child.get("started_at"),
                                ),
                            }
                        )
                        assert parent is not None
                        parent_broadcast = parent.get("broadcast") if isinstance(parent.get("broadcast"), dict) else {}
                        parent_review_evidence = (
                            parent_broadcast.get("review_evidence")
                            if isinstance(parent_broadcast.get("review_evidence"), dict)
                            else {}
                        )
                        preserved_blockers = [
                            reason
                            for reason in parent_broadcast.get("blocking_reasons", [])
                            if reason
                            not in {
                                "missing_qualified_selective_review_queue",
                                "invalid_or_stale_selective_review_evidence",
                                "review_evidence_bundle_not_available",
                                *_CONFIG_LINEAGE_BLOCKERS,
                            }
                        ]
                        parent["broadcast"] = {
                            **parent_broadcast,
                            "status": "needs_review",
                            "blocking_reasons": list(dict.fromkeys([*preserved_blockers, blocker.code])),
                            "review_evidence": {
                                **parent_review_evidence,
                                "status": "blocked",
                                "operation_run_id": child.get("run_id"),
                                "blocker_code": blocker.code,
                                "error_code": blocker.code,
                                "recovery_action": recovery_action,
                                "message": error,
                                "config_lineage_reconfirmation": challenge,
                            },
                        }
                    elif recovered_result is not None:
                        child.update(
                            {
                                "status": "completed",
                                "completed_at": completed_at,
                                "error": None,
                                "broadcast": {
                                    **metadata,
                                    "operation_status": "completed",
                                    "recovered": True,
                                    "worker_exited": True,
                                    "commit_started": True,
                                    "result": recovered_result,
                                },
                                "progress": self._completed_progress(child.get("progress"), child.get("started_at")),
                            }
                        )
                        quality_report = publish_broadcast_facade(Path(parent["output_dir"]).resolve())
                        parent_broadcast = parent.get("broadcast") if isinstance(parent.get("broadcast"), dict) else {}
                        preserved_blockers = [
                            reason
                            for reason in parent_broadcast.get("blocking_reasons", [])
                            if reason
                            not in {
                                "missing_qualified_selective_review_queue",
                                "invalid_or_stale_selective_review_evidence",
                                "review_evidence_bundle_not_available",
                            }
                        ]
                        parent["broadcast"] = {
                            **parent_broadcast,
                            "status": "needs_review",
                            "blocking_reasons": list(
                                dict.fromkeys([*preserved_blockers, *(quality_report.get("blocking_reasons") or [])])
                            ),
                            "limitations": list(
                                dict.fromkeys(
                                    [
                                        *(parent_broadcast.get("limitations") or []),
                                        *(quality_report.get("limitations") or []),
                                    ]
                                )
                            ),
                            "status_generation": quality_report.get("status_generation"),
                            "review_evidence": {
                                "status": "ready",
                                "generation_id": recovered_result["review_evidence_generation_id"],
                                "queue_sha256": recovered_result["queue_sha256"],
                                "operation_run_id": child["run_id"],
                            },
                        }
                        parent_output = Path(parent["output_dir"]).resolve()
                        parent["artifacts"] = self._collect_artifacts(parent_output)
                        parent["stats"] = self._collect_stats(parent_output)
                        import_report_path = output_dir / "review_evidence_import_report.v1.json"
                        if not import_report_path.exists():
                            publish_json_exclusive(
                                import_report_path,
                                {
                                    "schema_version": "1.0",
                                    "artifact_type": "broadcast_review_evidence_import_report",
                                    "status": "succeeded",
                                    "operation_run_id": child["run_id"],
                                    "parent_run_id": child.get("parent_run_id"),
                                    "request_digest": metadata.get("request_digest"),
                                    "generation_id": recovered_result["review_evidence_generation_id"],
                                    "queue_sha256": recovered_result["queue_sha256"],
                                    "idempotent_activation": True,
                                    "recovered": True,
                                },
                                trusted_root=output_dir,
                            )
                    else:
                        if not was_active:
                            continue
                        error = "Review evidence import was interrupted before an authoritative root commit."
                        child.update(
                            {
                                "status": "failed",
                                "completed_at": completed_at,
                                "error": error,
                                "broadcast": {
                                    **metadata,
                                    "operation_status": "failed",
                                    "error_code": "review_evidence_import_interrupted",
                                    "recovered": True,
                                    "worker_exited": True,
                                },
                                "progress": self._failed_progress(child.get("progress"), child.get("started_at")),
                            }
                        )
                    artifact_error = self._write_run_artifacts(output_dir, child)
                    child["error"] = self._append_artifact_error(child.get("error"), artifact_error)
                    child["artifacts"] = self._collect_artifacts(output_dir)
                    child["stats"] = self._collect_stats(output_dir)

    def _recover_review_evidence_revocations(self) -> None:
        """Reconcile an authoritative filesystem revocation into child and parent registry state."""

        with self._lock:
            with self._registry_transaction() as registry:
                for child in registry["runs"]:
                    if child.get("source") != "broadcast_review_evidence_import":
                        continue
                    metadata = child.get("broadcast") if isinstance(child.get("broadcast"), dict) else {}
                    result = metadata.get("result") if isinstance(metadata.get("result"), dict) else {}
                    generation_id = result.get("review_evidence_generation_id")
                    queue_sha256 = result.get("queue_sha256")
                    if (
                        not isinstance(generation_id, str)
                        or not isinstance(queue_sha256, str)
                        or result.get("status") == "revoked"
                    ):
                        continue
                    parent = next(
                        (item for item in registry["runs"] if item.get("run_id") == child.get("parent_run_id")),
                        None,
                    )
                    if parent is None:
                        continue
                    parent_output = Path(parent["output_dir"]).resolve()
                    revocation_path = (
                        parent_output
                        / "review_evidence"
                        / "generations"
                        / generation_id
                        / "review_evidence_revocation.v1.json"
                    )
                    if not revocation_path.is_file():
                        continue
                    try:
                        revocation, _ = load_bound_json(revocation_path, "review evidence revocation")
                    except BroadcastApiError:
                        continue
                    if (
                        revocation.get("artifact_type") != "broadcast_review_evidence_revocation"
                        or revocation.get("generation_id") != generation_id
                        or revocation.get("queue_sha256") != queue_sha256
                    ):
                        continue
                    root_queue_path = parent_output / "selective_review_queue.v1.json"
                    if root_queue_path.exists():
                        try:
                            revocation = revoke_review_evidence_activation(
                                parent_output,
                                generation_id=generation_id,
                                expected_queue_sha256=queue_sha256,
                            )
                        except ReviewEvidenceBundleError:
                            continue
                    child["broadcast"] = {
                        **metadata,
                        "result": {
                            **result,
                            "status": "revoked",
                            "revoked_at": revocation.get("revoked_at"),
                        },
                    }
                    quality_report = publish_broadcast_facade(parent_output)
                    self._apply_revoked_review_evidence_state(
                        parent,
                        quality_report,
                        generation_id=generation_id,
                        queue_sha256=queue_sha256,
                        revoked_at=revocation.get("revoked_at"),
                    )
                    parent["artifacts"] = self._collect_artifacts(parent_output)
                    parent["stats"] = self._collect_stats(parent_output)

    @staticmethod
    def _review_evidence_import_response(child: dict[str, Any]) -> dict[str, Any]:
        metadata = child.get("broadcast") if isinstance(child.get("broadcast"), dict) else {}
        result = metadata.get("result") if isinstance(metadata.get("result"), dict) else {}
        status = child.get("status")
        return {
            "run_id": child["run_id"],
            "parent_run_id": child.get("parent_run_id"),
            "status": "completed" if status == "completed" else "queued",
            "reason": "review_evidence_import_completed" if status == "completed" else "review_evidence_import_queued",
            "artifact": "selective_review_queue.v1.json" if status == "completed" else None,
            "generation_id": result.get("review_evidence_generation_id"),
            "details": {"queue_sha256": result.get("queue_sha256")},
        }

    def revoke_broadcast_review_evidence(
        self,
        run_id: str,
        generation_id: str,
        expected_queue_sha256: str,
    ) -> dict[str, Any]:
        """Revoke one exact, unconsumed review-evidence activation."""

        self._assert_service_open()
        if not isinstance(generation_id, str) or re.fullmatch(r"review-evidence-[0-9a-f]{24}", generation_id) is None:
            raise ValueError("generation_id must identify an immutable review evidence generation")
        if not isinstance(expected_queue_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", expected_queue_sha256) is None:
            raise ValueError("queue_sha256 must be a lowercase SHA-256")
        with self._lock:
            self._assert_service_open_locked()
            with self._registry_transaction() as registry:
                parent = next((item for item in registry["runs"] if item.get("run_id") == run_id), None)
                if parent is None or parent.get("source") != "broadcast_hybrid":
                    raise KeyError(run_id)
                active_import = next(
                    (
                        item
                        for item in registry["runs"]
                        if item.get("source") == "broadcast_review_evidence_import"
                        and item.get("parent_run_id") == run_id
                        and item.get("status") in {"queued", "running"}
                    ),
                    None,
                )
                if active_import is not None:
                    raise RuntimeError("review evidence activation is still being committed")
                output_dir = Path(parent["output_dir"]).resolve()
                matching_child = None
                for child in reversed(registry["runs"]):
                    metadata = child.get("broadcast") if isinstance(child.get("broadcast"), dict) else {}
                    result = metadata.get("result") if isinstance(metadata.get("result"), dict) else {}
                    if (
                        child.get("source") == "broadcast_review_evidence_import"
                        and child.get("parent_run_id") == run_id
                        and result.get("review_evidence_generation_id") == generation_id
                        and result.get("queue_sha256") == expected_queue_sha256
                    ):
                        matching_child = child
                        break
                if matching_child is None:
                    raise RuntimeError("review evidence activation has no authoritative import lineage")
                try:
                    report = revoke_review_evidence_activation(
                        output_dir,
                        generation_id=generation_id,
                        expected_queue_sha256=expected_queue_sha256,
                    )
                except ReviewEvidenceBundleError as exc:
                    raise RuntimeError(str(exc)) from exc
                metadata = matching_child.get("broadcast") if isinstance(matching_child.get("broadcast"), dict) else {}
                result = metadata.get("result") if isinstance(metadata.get("result"), dict) else {}
                matching_child["broadcast"] = {
                    **metadata,
                    "result": {**result, "status": "revoked", "revoked_at": report["revoked_at"]},
                }
                quality_report = publish_broadcast_facade(output_dir)
                self._apply_revoked_review_evidence_state(
                    parent,
                    quality_report,
                    generation_id=generation_id,
                    queue_sha256=expected_queue_sha256,
                    revoked_at=report["revoked_at"],
                )
                parent["artifacts"] = self._collect_artifacts(output_dir)
                parent["stats"] = self._collect_stats(output_dir)
        return {
            "run_id": run_id,
            "status": "revoked",
            "generation_id": generation_id,
            "queue_sha256": expected_queue_sha256,
            "revoked_at": report["revoked_at"],
        }

    def get_broadcast_review_windows(self, run_id: str) -> dict[str, Any]:
        run, output_dir = self._broadcast_run_output(run_id)
        try:
            terminal_tail_review = inspect_terminal_tail_review(output_dir)
        except BroadcastApiError as exc:
            terminal_tail_review = {
                "status": "invalid",
                "reason": str(exc),
                "evidence": None,
            }

        def unavailable(reason: str, **details: Any) -> dict[str, Any]:
            return {
                **self._broadcast_needs_review(run_id, reason, **details),
                "terminal_tail_review": terminal_tail_review,
            }

        queue_path = output_dir / "selective_review_queue.v1.json"
        with self._lock:
            with self._registry_file_lock():
                registry = self._read_registry()
                run = self._review_evidence_parent_from_registry(registry, run_id, output_dir)
                if not queue_path.is_file():
                    return unavailable("missing_qualified_selective_review_queue")
                try:
                    queue, queue_sha256 = self._validate_current_review_queue_locked(
                        run_id,
                        output_dir,
                        run,
                        queue_path,
                        registry=registry,
                    )
                except (BroadcastApiError, RuntimeError) as exc:
                    return unavailable("invalid_or_stale_selective_review_evidence", message=str(exc))
                items = queue.get("items")
                if not isinstance(items, list):
                    return unavailable("invalid_selective_review_queue_items")
                broadcast = run.get("broadcast")
                if not isinstance(broadcast, dict):
                    broadcast = {}
                configured_limit = broadcast.get("max_manual_review_windows", 30)
                if not isinstance(configured_limit, int) or isinstance(configured_limit, bool):
                    configured_limit = 30
                if len(items) > configured_limit or queue.get("review_item_count") != len(items):
                    return unavailable("invalid_selective_review_queue_window_count")
                return {
                    "run_id": run_id,
                    "status": "ready",
                    "reason": None,
                    "queue_sha256": queue_sha256,
                    "review_item_count": len(items),
                    "items": items,
                    "terminal_tail_review": terminal_tail_review,
                }

    def submit_broadcast_review_actions(self, run_id: str, request: dict[str, Any]) -> dict[str, Any]:
        self._assert_service_open()
        _, output_dir = self._broadcast_run_output(run_id)
        expected_queue_sha256 = request.get("queue_sha256")
        if not isinstance(expected_queue_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", expected_queue_sha256) is None:
            raise ValueError("queue_sha256 must be a lowercase SHA-256")
        window_state = self.get_broadcast_review_windows(run_id)
        if window_state.get("status") != "ready":
            raise RuntimeError(str(window_state.get("reason") or "broadcast review windows are unavailable"))
        queue_path = output_dir / "selective_review_queue.v1.json"
        if not queue_path.is_file():
            raise RuntimeError("missing qualified selective review queue")
        raw_actions = request.get("actions")
        if not isinstance(raw_actions, list):
            raise ValueError("review action submission actions must be a list")
        if any(isinstance(action, dict) and action.get("action") == "correct_trajectory" for action in raw_actions):
            raise RuntimeError("correct_trajectory is not supported by the global trajectory solver")
        try:
            with self._lock:
                with self._registry_file_lock():
                    registry = self._read_registry()
                    parent = self._review_evidence_parent_from_registry(registry, run_id, output_dir)
                    if any(
                        item.get("source") == "broadcast_review_evidence_import"
                        and item.get("parent_run_id") == run_id
                        and item.get("status") in {"queued", "running"}
                        for item in registry["runs"]
                    ):
                        raise RuntimeError("review evidence activation is still being committed")
                    _, current_queue_sha256 = self._validate_current_review_queue_locked(
                        run_id,
                        output_dir,
                        parent,
                        queue_path,
                        registry=registry,
                    )
                    if current_queue_sha256 != expected_queue_sha256:
                        raise RuntimeError("selective review queue changed after review windows were loaded")
                    envelope = build_review_action_envelope(queue_path, raw_actions, trusted_root=output_dir)
                    decisions_path = output_dir / "review_decisions.json"
                    decisions_sha256 = publish_json_exclusive(decisions_path, envelope, trusted_root=output_dir)
        except _ReviewEvidenceTargetContextError as exc:
            raise RuntimeError(f"review evidence target changed from current run context: {exc}") from exc
        except BroadcastApiError as exc:
            raise RuntimeError(f"invalid or stale selective review evidence: {exc}") from exc
        self._refresh_broadcast_facade_state(run_id, output_dir)
        return {
            "run_id": run_id,
            "status": "completed",
            "reason": "review_actions_accepted",
            "artifact": "review_decisions.json",
            "generation_id": None,
            "details": {"review_decisions_sha256": decisions_sha256},
        }

    def submit_broadcast_terminal_tail_review(self, run_id: str, request: dict[str, Any]) -> dict[str, Any]:
        self._assert_service_open()
        parent, output_dir = self._broadcast_run_output(run_id)
        raw_parent_broadcast = parent.get("broadcast")
        parent_broadcast: dict[str, Any] = raw_parent_broadcast if isinstance(raw_parent_broadcast, dict) else {}
        if parent_broadcast.get("status") == "ready" or (output_dir / "broadcast_quality_report.json").is_file():
            raise RuntimeError("ready broadcast artifacts are immutable; start a new run for revised review actions")
        reviewer_id = request.get("reviewer_id")
        decision = request.get("decision")
        expected_evidence_sha256 = request.get("evidence_sha256")
        if not isinstance(reviewer_id, str) or not reviewer_id.strip():
            raise ValueError("reviewer_id must not be blank")
        if decision != "accept_terminal_shortfall":
            raise ValueError("unsupported terminal-tail review decision")
        if (
            not isinstance(expected_evidence_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_evidence_sha256) is None
        ):
            raise ValueError("evidence_sha256 must be a lowercase SHA-256")
        try:
            acknowledgement = build_terminal_tail_review_acknowledgement(
                output_dir,
                decision=decision,
                reviewer_id=reviewer_id,
                evidence_sha256=expected_evidence_sha256,
            )
            acknowledgement_path = output_dir / TERMINAL_TAIL_REVIEW_NAME
            try:
                acknowledgement_sha256 = publish_json_exclusive(
                    acknowledgement_path,
                    acknowledgement,
                    trusted_root=output_dir,
                )
            except BroadcastApiError:
                # A same-evidence retry or concurrent identical request reuses the
                # immutable winner; a different reviewer/decision remains a conflict.
                acknowledgement = build_terminal_tail_review_acknowledgement(
                    output_dir,
                    decision=decision,
                    reviewer_id=reviewer_id,
                    evidence_sha256=expected_evidence_sha256,
                )
                acknowledgement_sha256 = sha256_file(acknowledgement_path)
        except BroadcastApiError as exc:
            raise RuntimeError(f"invalid or stale terminal-tail review evidence: {exc}") from exc
        quality_report = self._refresh_broadcast_facade_state(run_id, output_dir)
        if quality_report.get("status") != "needs_review":
            raise RuntimeError("terminal-tail acknowledgement produced an unexpected broadcast state")
        return {
            "run_id": run_id,
            "status": "completed",
            "reason": "terminal_tail_review_accepted",
            "artifact": TERMINAL_TAIL_REVIEW_NAME,
            "generation_id": None,
            "details": {"terminal_tail_review_sha256": acknowledgement_sha256},
        }

    def recompute_broadcast_trajectory(self, run_id: str, request: dict[str, Any]) -> dict[str, Any]:
        self._assert_service_open()
        parent, output_dir = self._broadcast_run_output(run_id)
        parent_broadcast = parent.get("broadcast")
        if not isinstance(parent_broadcast, dict):
            parent_broadcast = {}
        if parent_broadcast.get("status") == "ready" or (output_dir / "broadcast_quality_report.json").is_file():
            raise RuntimeError("ready broadcast artifacts are immutable; start a new run for revised review actions")
        terminal_tail_frozen_inputs = self._terminal_tail_frozen_inputs(output_dir)
        expected_decisions_sha256 = request.get("review_decisions_sha256")
        if not isinstance(expected_decisions_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_decisions_sha256
        ):
            raise ValueError("review_decisions_sha256 must be a lowercase SHA-256")
        try:
            frozen_inputs = preflight_recompute_reviewed_trajectory(output_dir)
        except BroadcastHybridOrchestrationError as exc:
            raise RuntimeError(str(exc)) from exc
        if frozen_inputs["review_decisions_sha256"] != expected_decisions_sha256:
            raise RuntimeError("review decisions changed after they were accepted")
        frozen_inputs = {
            **frozen_inputs,
            **terminal_tail_frozen_inputs,
            "parent_run_id": run_id,
            "parent_output_dir": str(output_dir),
        }
        return self._queue_broadcast_operation(
            parent=parent,
            operation="recompute",
            request={},
            frozen_inputs=frozen_inputs,
        )

    def render_broadcast_hybrid(self, run_id: str, request: dict[str, Any]) -> dict[str, Any]:
        self._assert_service_open()
        parent, output_dir = self._broadcast_run_output(run_id)
        parent_broadcast = parent.get("broadcast")
        if not isinstance(parent_broadcast, dict):
            parent_broadcast = {}
        if parent_broadcast.get("status") == "ready" or (output_dir / "broadcast_quality_report.json").is_file():
            raise RuntimeError("broadcast render is already ready and immutable")
        terminal_tail_frozen_inputs = self._terminal_tail_frozen_inputs(output_dir)
        generation_id = request.get("trajectory_generation_id")
        if not isinstance(generation_id, str) or not re.fullmatch(r"trajectory-[0-9a-f]{24}", generation_id):
            raise ValueError("trajectory_generation_id must identify a completed immutable trajectory generation")
        target_width = request.get("target_width")
        target_height = request.get("target_height")
        if (
            isinstance(target_width, bool)
            or not isinstance(target_width, int)
            or isinstance(target_height, bool)
            or not isinstance(target_height, int)
        ):
            raise ValueError("target dimensions must be integers")
        if not 320 <= target_width <= 7680 or not 180 <= target_height <= 4320:
            raise ValueError("target dimensions are outside the supported render bounds")
        try:
            frozen_inputs = preflight_render_broadcast_trajectory(
                output_dir,
                generation_id,
                target_width=target_width,
                target_height=target_height,
            )
        except BroadcastHybridOrchestrationError as exc:
            raise RuntimeError(str(exc)) from exc
        frozen_inputs = {
            **frozen_inputs,
            **terminal_tail_frozen_inputs,
            "parent_run_id": run_id,
            "parent_output_dir": str(output_dir),
        }
        return self._queue_broadcast_operation(
            parent=parent,
            operation="render",
            request={
                "trajectory_generation_id": generation_id,
                "target_width": target_width,
                "target_height": target_height,
            },
            frozen_inputs=frozen_inputs,
        )

    def _queue_broadcast_operation(
        self,
        *,
        parent: dict[str, Any],
        operation: str,
        request: dict[str, Any],
        frozen_inputs: dict[str, Any],
    ) -> dict[str, Any]:
        if operation not in {"recompute", "render"}:
            raise ValueError(f"unsupported broadcast operation: {operation}")
        parent_run_id = str(parent["run_id"])
        operation_run_id = f"{parent_run_id}-{operation}-{uuid4().hex[:8]}"
        input_video = Path(parent["input_video"]).resolve() if parent.get("input_video") else None
        operation_output = self._build_run_output_dir(run_id=operation_run_id, input_video=input_video)
        if operation_output.exists():
            raise RuntimeError(f"broadcast operation output already exists: {operation_output}")
        created_at = _utc_now_iso()
        child = {
            "run_id": operation_run_id,
            "source": f"broadcast_hybrid_{operation}",
            "status": "queued",
            "created_at": created_at,
            "started_at": None,
            "completed_at": None,
            "config_name": parent.get("config_name"),
            "config_path": parent.get("config_path"),
            "input_video": parent.get("input_video"),
            "parent_run_id": parent_run_id,
            "output_dir": str(operation_output),
            "modules_enabled": {"broadcast_hybrid": True},
            "artifacts": [],
            "stats": {},
            "broadcast": {
                "operation": operation,
                "operation_status": "queued",
                "parent_run_id": parent_run_id,
                "owner_pid": os.getpid(),
                "owner_instance_id": self._instance_id,
                "request": request,
                "frozen_inputs": frozen_inputs,
            },
            "progress": self._initial_progress(),
            "notes": f"Broadcast hybrid {operation} operation for {parent_run_id}",
            "error": None,
        }
        self._attach_ai_candidate_lifecycle(child)
        try:
            operation_output.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise RuntimeError(f"broadcast operation output already exists: {operation_output}") from exc
        try:
            with self._lock:
                self._assert_service_open_locked()
                with self._registry_transaction() as registry:
                    active = next(
                        (item for item in registry["runs"] if item.get("status") in {"queued", "running"}),
                        None,
                    )
                    if active is not None:
                        raise RuntimeError(f"Another run is already active: {active.get('run_id')}")
                    current_parent = next(
                        (item for item in registry["runs"] if item.get("run_id") == parent_run_id), None
                    )
                    if (
                        current_parent is None
                        or current_parent.get("source") != "broadcast_hybrid"
                        or current_parent.get("status") != "completed"
                    ):
                        raise RuntimeError("broadcast operation parent must remain a completed broadcast_hybrid run")
                    if any(item.get("run_id") == operation_run_id for item in registry["runs"]):
                        raise RuntimeError(f"broadcast operation run already exists: {operation_run_id}")
                    registry["runs"].append(child)
                cancel_event = threading.Event()
                thread = threading.Thread(
                    target=self._execute_broadcast_operation,
                    args=(operation_run_id, parent_run_id, operation, request, frozen_inputs, cancel_event),
                    name=f"football-tracking-broadcast-{operation}-{operation_run_id}",
                    daemon=True,
                )
                self._active_threads[operation_run_id] = thread
                self._cancel_events[operation_run_id] = cancel_event
        except BaseException:
            shutil.rmtree(operation_output, ignore_errors=True)
            raise
        self._start_thread_or_cleanup(
            operation_run_id,
            thread,
            output_dir=operation_output,
            remove_output=True,
        )
        return {
            "run_id": operation_run_id,
            "parent_run_id": parent_run_id,
            "status": "queued",
            "reason": f"broadcast_{operation}_queued",
            "artifact": None,
            "generation_id": None,
            "details": {},
        }

    def _begin_broadcast_operation_execution(
        self,
        *,
        operation_run_id: str,
        parent_run_id: str,
        operation: str,
        frozen_inputs: dict[str, Any],
        cancel_event: threading.Event,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Atomically claim a queued child without overwriting remote cancellation."""

        with self._lock:
            with self._registry_transaction() as registry:
                child = next((item for item in registry["runs"] if item.get("run_id") == operation_run_id), None)
                parent = next((item for item in registry["runs"] if item.get("run_id") == parent_run_id), None)
                if child is None or parent is None:
                    raise BroadcastHybridOrchestrationError("broadcast operation registry lineage disappeared")
                metadata = child.get("broadcast") if isinstance(child.get("broadcast"), dict) else {}
                if (
                    child.get("source") != f"broadcast_hybrid_{operation}"
                    or child.get("parent_run_id") != parent_run_id
                    or metadata.get("operation") != operation
                    or metadata.get("frozen_inputs") != frozen_inputs
                ):
                    raise BroadcastHybridOrchestrationError("broadcast operation queue lineage changed")
                if cancel_event.is_set() or metadata.get("cancel_requested") is True:
                    raise CancelledError()
                if child.get("status") != "queued":
                    raise BroadcastHybridOrchestrationError("broadcast operation is no longer queued")
                started_at = _utc_now_iso()
                child.update(
                    {
                        "status": "running",
                        "started_at": started_at,
                        "error": None,
                        "broadcast": {
                            **metadata,
                            "owner_pid": os.getpid(),
                            "owner_instance_id": self._instance_id,
                            "operation_status": "running",
                            "worker_exited": False,
                        },
                        "progress": {
                            **self._initial_progress(),
                            "stage": f"broadcast_{operation}",
                        },
                    }
                )
                return deepcopy(child), deepcopy(parent)

    def _execute_broadcast_operation(
        self,
        operation_run_id: str,
        parent_run_id: str,
        operation: str,
        request: dict[str, Any],
        frozen_inputs: dict[str, Any],
        cancel_event: threading.Event,
    ) -> None:
        operation_output: Path | None = None
        parent_output: Path | None = None
        result: dict[str, Any] | None = None
        quality_report: dict[str, Any] | None = None
        completed_child: dict[str, Any] | None = None
        operation_report_published = False
        ready_root_authoritative = False
        try:
            queued_child = self.get_run(operation_run_id)
            operation_output = Path(queued_child["output_dir"]).resolve()
            child, parent = self._begin_broadcast_operation_execution(
                operation_run_id=operation_run_id,
                parent_run_id=parent_run_id,
                operation=operation,
                frozen_inputs=frozen_inputs,
                cancel_event=cancel_event,
            )
            parent_output = Path(parent["output_dir"]).resolve()
            if frozen_inputs.get("parent_run_id") != parent_run_id or frozen_inputs.get("parent_output_dir") != str(
                parent_output
            ):
                raise BroadcastHybridOrchestrationError("broadcast parent identity changed after queueing")

            def cancellation_requested() -> bool:
                if cancel_event.is_set():
                    return True
                with self._lock:
                    registry = self._read_registry()
                    current = next(
                        (item for item in registry["runs"] if item.get("run_id") == operation_run_id),
                        None,
                    )
                    metadata = current.get("broadcast") if isinstance(current, dict) else None
                    return isinstance(metadata, dict) and metadata.get("cancel_requested") is True

            if cancellation_requested():
                raise CancelledError()

            def begin_commit() -> None:
                self._begin_broadcast_operation_commit(
                    operation_run_id=operation_run_id,
                    parent_run_id=parent_run_id,
                    parent_output=parent_output,
                    cancel_event=cancel_event,
                )

            if operation == "recompute":
                current_inputs = preflight_recompute_reviewed_trajectory(parent_output)
                current_inputs = {
                    **current_inputs,
                    **self._terminal_tail_frozen_inputs(parent_output),
                    "parent_run_id": parent_run_id,
                    "parent_output_dir": str(parent_output),
                }
                if current_inputs != frozen_inputs:
                    raise BroadcastHybridOrchestrationError(
                        "broadcast recompute inputs changed after the operation was queued"
                    )
                result = recompute_reviewed_trajectory(
                    parent_output,
                    should_cancel=cancellation_requested,
                    before_commit=begin_commit,
                )
                trajectory_generation_id = result.get("trajectory_generation_id")
                if not isinstance(trajectory_generation_id, str):
                    raise BroadcastHybridOrchestrationError("recompute did not return a trajectory generation")
                preflight_render_broadcast_trajectory(parent_output, trajectory_generation_id)
            else:
                current_inputs = preflight_render_broadcast_trajectory(
                    parent_output,
                    request["trajectory_generation_id"],
                    target_width=request["target_width"],
                    target_height=request["target_height"],
                )
                current_inputs = {
                    **current_inputs,
                    **self._terminal_tail_frozen_inputs(parent_output),
                    "parent_run_id": parent_run_id,
                    "parent_output_dir": str(parent_output),
                }
                if current_inputs != frozen_inputs:
                    raise BroadcastHybridOrchestrationError(
                        "broadcast render inputs changed after the operation was queued"
                    )
                result = render_broadcast_trajectory(
                    parent_output,
                    request["trajectory_generation_id"],
                    target_width=request["target_width"],
                    target_height=request["target_height"],
                    should_cancel=cancellation_requested,
                    before_commit=begin_commit,
                )
            try:
                quality_report = publish_broadcast_facade(parent_output)
            except Exception as exc:
                if operation == "render" and (parent_output / "broadcast_artifact_bindings.v1.json").is_file():
                    try:
                        rollback_uncommitted_final_public_artifacts(parent_output)
                    except Exception as rollback_exc:
                        raise RuntimeError(
                            f"{exc} | failed to quarantine unready final artifacts: {rollback_exc}"
                        ) from exc
                raise
            if operation == "render" and quality_report.get("status") != "ready":
                if (parent_output / "broadcast_artifact_bindings.v1.json").is_file():
                    rollback_uncommitted_final_public_artifacts(parent_output)
                raise BroadcastHybridOrchestrationError("render completed without a ready broadcast facade")
            if operation == "recompute" and quality_report.get("status") != "needs_review":
                raise BroadcastHybridOrchestrationError("recompute produced an unexpected final facade state")

            existing = self.get_run(operation_run_id)
            completed_at = _utc_now_iso()
            existing_broadcast = existing.get("broadcast")
            if not isinstance(existing_broadcast, dict):
                existing_broadcast = {}
            completed_child = {
                **existing,
                "status": "completed",
                "completed_at": completed_at,
                "error": None,
                "broadcast": {
                    **existing_broadcast,
                    "operation_status": "completed",
                    "commit_started": True,
                    "result": _jsonable(result),
                },
                "progress": self._completed_progress(existing.get("progress"), existing.get("started_at")),
            }
            metadata_warnings: list[str] = []
            artifact_error = self._write_run_artifacts(operation_output, completed_child)
            if artifact_error is not None:
                metadata_warnings.append(artifact_error)
            operation_report = {
                "schema_version": "1.0",
                "artifact_type": "broadcast_operation_report",
                "status": "succeeded",
                "operation": operation,
                "parent_run_id": parent_run_id,
                "frozen_inputs": frozen_inputs,
                "result": _jsonable(result),
                "quality_status_generation": quality_report.get("status_generation"),
                "metadata_warnings": metadata_warnings,
            }
            try:
                publish_json_exclusive(
                    operation_output / "broadcast_operation_report.v1.json",
                    operation_report,
                    trusted_root=operation_output,
                )
                operation_report_published = True
                completed_child["broadcast"]["operation_report_status"] = "available"
            except (OSError, BroadcastApiError) as report_exc:
                if operation != "render" or quality_report.get("status") != "ready":
                    raise
                validated_quality = validate_broadcast_quality_report(
                    parent_output,
                    parent_output / "broadcast_quality_report.json",
                )
                if validated_quality.get("status_generation") != quality_report.get("status_generation"):
                    raise BroadcastApiError("authoritative ready facade changed before registry commit") from report_exc
                ready_root_authoritative = True
                if isinstance(report_exc, BroadcastApiError):
                    completed_child.update(
                        {
                            "status": "failed",
                            "error": f"Ready render committed with conflicting operation metadata: {report_exc}",
                            "progress": self._failed_progress(
                                completed_child.get("progress"), completed_child.get("started_at")
                            ),
                        }
                    )
                    completed_child["broadcast"]["operation_status"] = "metadata_conflict"
                    completed_child["broadcast"]["operation_report_status"] = "conflict"
                else:
                    metadata_warnings.append(
                        f"Ready render is authoritative; operation report publication failed: {report_exc}"
                    )
                    completed_child["broadcast"]["operation_report_status"] = "missing_after_ready_commit"
            if metadata_warnings:
                completed_child["broadcast"]["metadata_warnings"] = metadata_warnings
            completed_child["artifacts"] = self._collect_artifacts(operation_output)
            completed_child["stats"] = self._collect_stats(operation_output)
            self._attach_ai_candidate_lifecycle(completed_child)
            self._commit_broadcast_operation_registry(
                parent_run_id=parent_run_id,
                operation_run_id=operation_run_id,
                operation=operation,
                result=result,
                quality_report=quality_report,
                parent_output=parent_output,
                completed_child=completed_child,
            )
        except CancelledError:
            if operation_output is not None:
                try:
                    self._finish_broadcast_operation_failure(
                        operation_run_id,
                        parent_run_id,
                        operation,
                        operation_output,
                        status="cancelled",
                        error=None,
                    )
                except Exception:
                    pass
        except BaseException as exc:
            if (
                parent_output is not None
                and result is not None
                and quality_report is not None
                and completed_child is not None
                and (operation_report_published or ready_root_authoritative)
            ):
                child_broadcast = completed_child.get("broadcast")
                if isinstance(child_broadcast, dict):
                    warnings = child_broadcast.setdefault("metadata_warnings", [])
                    if isinstance(warnings, list):
                        warnings.append(f"Post-commit registry update required retry: {exc}")
                try:
                    self._commit_broadcast_operation_registry(
                        parent_run_id=parent_run_id,
                        operation_run_id=operation_run_id,
                        operation=operation,
                        result=result,
                        quality_report=quality_report,
                        parent_output=parent_output,
                        completed_child=completed_child,
                    )
                except Exception:
                    try:
                        if ready_root_authoritative:
                            self._commit_broadcast_operation_registry_atomic(
                                parent_run_id=parent_run_id,
                                operation_run_id=operation_run_id,
                                operation=operation,
                                result=result,
                                quality_report=quality_report,
                                parent_output=parent_output,
                                completed_child=completed_child,
                            )
                        else:
                            self._reconcile_broadcast_operation_report(operation_run_id)
                    except Exception as reconciliation_exc:
                        if ready_root_authoritative:
                            self._request_broadcast_reconciliation_retry(
                                operation_run_id,
                                error=str(reconciliation_exc),
                                mode="ready_without_report",
                            )
            else:
                if operation_output is not None:
                    try:
                        self._finish_broadcast_operation_failure(
                            operation_run_id,
                            parent_run_id,
                            operation,
                            operation_output,
                            status="failed",
                            error=str(exc),
                        )
                    except Exception:
                        pass
        finally:
            with self._lock:
                self._active_threads.pop(operation_run_id, None)
                self._cancel_events.pop(operation_run_id, None)

    def _begin_broadcast_operation_commit(
        self,
        *,
        operation_run_id: str,
        parent_run_id: str,
        parent_output: Path,
        cancel_event: threading.Event,
    ) -> None:
        with self._lock:
            with self._registry_transaction() as registry:
                child = next((item for item in registry["runs"] if item.get("run_id") == operation_run_id), None)
                parent = next((item for item in registry["runs"] if item.get("run_id") == parent_run_id), None)
                if child is None or parent is None:
                    raise BroadcastHybridOrchestrationError("broadcast operation registry lineage disappeared")
                child_broadcast = child.get("broadcast") if isinstance(child.get("broadcast"), dict) else {}
                if cancel_event.is_set() or child_broadcast.get("cancel_requested") is True:
                    raise CancelledError()
                if child.get("status") != "running":
                    raise BroadcastHybridOrchestrationError("broadcast operation is not running at commit")
                if (
                    parent.get("source") != "broadcast_hybrid"
                    or parent.get("status") != "completed"
                    or Path(parent["output_dir"]).resolve() != parent_output
                ):
                    raise BroadcastHybridOrchestrationError("broadcast parent changed before commit")
                child["broadcast"] = {
                    **child_broadcast,
                    "operation_status": "committing",
                    "commit_started": True,
                }
                progress = (
                    child.get("progress") if isinstance(child.get("progress"), dict) else self._initial_progress()
                )
                child["progress"] = {**progress, "stage": "committing", "updated_at": _utc_now_iso()}

    def _commit_broadcast_operation_registry(
        self,
        *,
        parent_run_id: str,
        operation_run_id: str,
        operation: str,
        result: dict[str, Any],
        quality_report: dict[str, Any],
        parent_output: Path,
        completed_child: dict[str, Any],
    ) -> None:
        self._commit_broadcast_operation_registry_atomic(
            parent_run_id=parent_run_id,
            operation_run_id=operation_run_id,
            operation=operation,
            result=result,
            quality_report=quality_report,
            parent_output=parent_output,
            completed_child=completed_child,
        )

    def _commit_broadcast_operation_registry_atomic(
        self,
        *,
        parent_run_id: str,
        operation_run_id: str,
        operation: str,
        result: dict[str, Any],
        quality_report: dict[str, Any],
        parent_output: Path,
        completed_child: dict[str, Any],
    ) -> None:
        parent_update = self.get_run(parent_run_id)
        self._apply_recovered_broadcast_parent(
            parent_update,
            operation_run_id=operation_run_id,
            operation=operation,
            result=result,
            quality_report=quality_report,
            parent_output=parent_output,
        )
        with self._lock:
            with self._registry_transaction() as registry:
                parent = next((item for item in registry["runs"] if item.get("run_id") == parent_run_id), None)
                child_index = next(
                    (index for index, item in enumerate(registry["runs"]) if item.get("run_id") == operation_run_id),
                    None,
                )
                if parent is None or child_index is None:
                    raise BroadcastHybridOrchestrationError(
                        "broadcast operation registry lineage disappeared at commit"
                    )
                current_child = registry["runs"][child_index]
                current_metadata = (
                    current_child.get("broadcast") if isinstance(current_child.get("broadcast"), dict) else {}
                )
                if (
                    current_child.get("status") != "running"
                    or current_metadata.get("commit_started") is not True
                    or current_metadata.get("cancel_requested") is True
                ):
                    raise BroadcastHybridOrchestrationError("broadcast child changed during commit")
                if (
                    parent.get("source") != "broadcast_hybrid"
                    or parent.get("status") != "completed"
                    or Path(parent["output_dir"]).resolve() != parent_output
                ):
                    raise BroadcastHybridOrchestrationError("broadcast parent changed during commit")
                for key in ("broadcast", "artifacts", "stats", "ai_candidate_lifecycle"):
                    if key in parent_update:
                        parent[key] = deepcopy(parent_update[key])
                replacement = deepcopy(completed_child)
                replacement_metadata = (
                    replacement.get("broadcast") if isinstance(replacement.get("broadcast"), dict) else {}
                )
                replacement["broadcast"] = {**current_metadata, **replacement_metadata}
                registry["runs"][child_index] = replacement

    def _finish_broadcast_operation_failure(
        self,
        operation_run_id: str,
        parent_run_id: str,
        operation: str,
        operation_output: Path,
        *,
        status: str,
        error: str | None,
    ) -> None:
        existing = self.get_run(operation_run_id)
        completed_at = _utc_now_iso()
        existing_broadcast = existing.get("broadcast")
        if not isinstance(existing_broadcast, dict):
            existing_broadcast = {}
        partial = {
            **existing,
            "status": status,
            "completed_at": completed_at,
            "error": error,
            "broadcast": {
                **existing_broadcast,
                "operation_status": status,
            },
            "progress": (
                self._cancelled_progress(existing.get("progress"), existing.get("started_at"))
                if status == "cancelled"
                else self._failed_progress(existing.get("progress"), existing.get("started_at"))
            ),
        }
        artifact_error = self._write_run_artifacts(operation_output, partial)
        partial["error"] = self._append_artifact_error(error, artifact_error)
        partial["artifacts"] = self._collect_artifacts(operation_output)
        partial["stats"] = self._collect_stats(operation_output)
        self._attach_ai_candidate_lifecycle(partial)
        with self._lock:
            with self._registry_transaction() as registry:
                child_index = next(
                    (index for index, item in enumerate(registry["runs"]) if item.get("run_id") == operation_run_id),
                    None,
                )
                if child_index is None:
                    raise KeyError(operation_run_id)
                current = registry["runs"][child_index]
                if current.get("status") == "completed":
                    return
                current_metadata = current.get("broadcast") if isinstance(current.get("broadcast"), dict) else {}
                partial_metadata = partial.get("broadcast") if isinstance(partial.get("broadcast"), dict) else {}
                partial["broadcast"] = {**current_metadata, **partial_metadata}
                registry["runs"][child_index] = partial
                parent = next((item for item in registry["runs"] if item.get("run_id") == parent_run_id), None)
                if parent is not None:
                    broadcast = parent.get("broadcast") if isinstance(parent.get("broadcast"), dict) else {}
                    parent["broadcast"] = {
                        **broadcast,
                        "last_operation": {
                            "operation_run_id": operation_run_id,
                            "operation": operation,
                            "status": status,
                            "error": error,
                        },
                    }

    def _broadcast_run_output(self, run_id: str) -> tuple[dict[str, Any], Path]:
        run = self.get_run(run_id)
        if run.get("source") != "broadcast_hybrid":
            raise RuntimeError(f"Run is not a broadcast_hybrid run: {run_id}")
        if run.get("status") != "completed":
            raise RuntimeError(f"Broadcast run must be completed before review operations: {run_id}")
        output_dir = Path(run["output_dir"]).resolve()
        if self.outputs_dir.resolve() not in output_dir.parents:
            raise RuntimeError(f"Broadcast run output is outside the outputs root: {run_id}")
        return run, output_dir

    def _broadcast_needs_review(
        self,
        run_id: str,
        reason: str,
        *,
        artifact: str | None = None,
        generation_id: str | None = None,
        **details: Any,
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "status": "needs_review",
            "reason": reason,
            "artifact": artifact,
            "generation_id": generation_id,
            "details": details,
        }

    def _broadcast_action_contract_evidence(
        self,
        *,
        contract: dict[str, Any],
        contract_path: Path,
        contract_sha256: str,
        input_video: Path,
        preflight: dict[str, Any],
        config: AppConfig,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        normalized = normalize_tracking_contract_payload(contract, path=contract_path)
        if normalized.get("artifact_status") != "loaded" or normalized.get("validation_errors") != []:
            raise RuntimeError("broadcast tracking contract failed deep validation")
        source = normalized.get("source")
        summary = normalized.get("summary")
        frames = normalized.get("frames")
        if not isinstance(source, dict) or not isinstance(summary, dict) or not isinstance(frames, list):
            raise RuntimeError("broadcast tracking contract is incomplete")

        expected_frame_count = source.get("frame_count")
        verified_frame_count = summary.get("frame_count")
        source_width = source.get("width")
        source_height = source.get("height")
        source_fps = source.get("fps")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (expected_frame_count, source_width, source_height)
        ):
            raise RuntimeError("broadcast tracking contract source metadata is invalid")
        if (
            isinstance(verified_frame_count, bool)
            or not isinstance(verified_frame_count, int)
            or verified_frame_count <= 0
            or not isinstance(source_fps, (int, float))
            or isinstance(source_fps, bool)
            or not math.isfinite(float(source_fps))
            or float(source_fps) <= 0.0
        ):
            raise RuntimeError("broadcast tracking contract frame evidence is invalid")
        assert isinstance(expected_frame_count, int)
        assert isinstance(verified_frame_count, int)
        assert isinstance(source_width, int)
        assert isinstance(source_height, int)
        assert isinstance(source_fps, (int, float))
        if len(frames) != verified_frame_count or any(
            not isinstance(frame, dict) or frame.get("frame_index") != index for index, frame in enumerate(frames)
        ):
            raise RuntimeError("broadcast tracking contract frames are not contiguous from frame 0")

        preflight_resolution = preflight.get("source_resolution")
        if preflight_resolution != [source_width, source_height]:
            raise RuntimeError("broadcast tracking contract source resolution changed after preflight")
        if preflight.get("source_frame_count") != expected_frame_count:
            raise RuntimeError("broadcast tracking contract source frame count changed after preflight")
        preflight_fps = preflight.get("fps")
        if (
            not isinstance(preflight_fps, (int, float))
            or isinstance(preflight_fps, bool)
            or not math.isclose(float(preflight_fps), float(source_fps), rel_tol=0.0, abs_tol=1e-6)
        ):
            raise RuntimeError("broadcast tracking contract source FPS changed after preflight")
        source_size_bytes = preflight.get("source_size_bytes")
        if (
            isinstance(source_size_bytes, bool)
            or not isinstance(source_size_bytes, int)
            or source_size_bytes <= 0
            or input_video.stat().st_size != source_size_bytes
        ):
            raise RuntimeError("broadcast source size changed after preflight")
        if source.get("video_sha256") != sha256_file(input_video):
            raise RuntimeError("broadcast tracking contract source SHA-256 does not match the input video")

        shortfall = expected_frame_count - verified_frame_count
        if shortfall < 0:
            raise RuntimeError("broadcast tracking contract contains more frames than the source metadata")
        contract_evidence = {
            "tracking_contract_sha256": contract_sha256,
            "source_video_sha256": source["video_sha256"],
            "source_width": source_width,
            "source_height": source_height,
            "source_fps": float(source_fps),
            "reported_frame_count": expected_frame_count,
            "verified_frame_count": verified_frame_count,
        }
        if shortfall == 0:
            return contract_evidence, None
        if (
            int(config.runtime.start_frame) != 0
            or config.runtime.max_frames is not None
            or not bool(config.temporal_chunks.enabled)
        ):
            raise RuntimeError("broadcast terminal frame shortfall is only allowed for an unbounded full-source run")
        if (
            shortfall > ACTION_SIGNAL_MAX_TERMINAL_SHORTFALL_FRAMES
            or shortfall / float(source_fps) > ACTION_SIGNAL_MAX_TERMINAL_SHORTFALL_SECONDS + 1e-9
        ):
            raise RuntimeError("broadcast terminal frame shortfall exceeds the fail-closed policy")

        try:
            temporal_path = self._resolve_safe_descendant(
                contract_path.parent,
                contract_path.parent / "temporal_chunks_report.json",
                expected_kind="file",
                direct=True,
            )
        except RuntimeError:
            raise RuntimeError("broadcast terminal frame shortfall has no temporal-chunk audit evidence")
        temporal, temporal_sha256 = load_bound_json(temporal_path, "broadcast temporal chunks report")
        events = temporal.get("boundary_events")
        event = events[0] if isinstance(events, list) and len(events) == 1 else None
        chunks = temporal.get("chunks")
        source_chunk_names = temporal.get("source_chunk_names")
        execution = temporal.get("execution")
        stitch = temporal.get("stitch")
        results = execution.get("results") if isinstance(execution, dict) else None
        ordered_chunks = (
            chunks if isinstance(chunks, list) and chunks and all(isinstance(chunk, dict) for chunk in chunks) else []
        )
        expected_chunk_names = [chunk.get("name") for chunk in ordered_chunks]
        contiguous_cores = bool(ordered_chunks) and ordered_chunks[0].get("core_start_frame") == 0
        if contiguous_cores:
            previous_core_end = -1
            for index, chunk in enumerate(ordered_chunks):
                core_start = chunk.get("core_start_frame")
                core_end = chunk.get("core_end_frame")
                if (
                    chunk.get("index") != index
                    or not isinstance(chunk.get("name"), str)
                    or isinstance(core_start, bool)
                    or not isinstance(core_start, int)
                    or isinstance(core_end, bool)
                    or not isinstance(core_end, int)
                    or core_start != previous_core_end + 1
                    or core_end < core_start
                ):
                    contiguous_cores = False
                    break
                previous_core_end = core_end
            contiguous_cores = contiguous_cores and previous_core_end == expected_frame_count - 1
        ordered_results = (
            isinstance(results, list)
            and len(results) == len(ordered_chunks)
            and all(
                isinstance(result, dict)
                and isinstance(result.get("chunk"), dict)
                and result["chunk"].get("index") == index
                and result["chunk"].get("name") == expected_chunk_names[index]
                and result.get("chunk_index") == index
                and result.get("chunk_name") == expected_chunk_names[index]
                and result.get("exit_code") == 0
                for index, result in enumerate(results)
            )
        )
        final_chunk = ordered_chunks[-1] if ordered_chunks else None
        if (
            temporal.get("frame_count") != verified_frame_count
            or temporal.get("chunk_count") != len(ordered_chunks)
            or source_chunk_names != expected_chunk_names
            or not contiguous_cores
            or not isinstance(event, dict)
            or event.get("type") != "truncated_final_tail"
            or not isinstance(final_chunk, dict)
            or event.get("chunk_index") != final_chunk.get("index")
            or event.get("chunk_name") != final_chunk.get("name")
            or event.get("first_missing_frame") != verified_frame_count
            or event.get("last_missing_frame") != expected_frame_count - 1
            or event.get("missing_frame_count") != shortfall
            or event.get("planned_core_end_frame") != expected_frame_count - 1
            or event.get("stitched_core_end_frame") != verified_frame_count - 1
            or final_chunk.get("core_end_frame") != expected_frame_count - 1
            or not isinstance(stitch, dict)
            or stitch.get("status") != "succeeded"
            or not isinstance(execution, dict)
            or execution.get("status") != "succeeded"
            or not ordered_results
        ):
            raise RuntimeError("broadcast terminal frame shortfall audit evidence is invalid")
        return contract_evidence, {
            **contract_evidence,
            "temporal_chunks_report_sha256": temporal_sha256,
            "first_missing_frame": verified_frame_count,
            "last_missing_frame": expected_frame_count - 1,
            "missing_frame_count": shortfall,
            "missing_duration_seconds": shortfall / float(source_fps),
            "policy": {
                "max_missing_frames": ACTION_SIGNAL_MAX_TERMINAL_SHORTFALL_FRAMES,
                "max_missing_seconds": ACTION_SIGNAL_MAX_TERMINAL_SHORTFALL_SECONDS,
                "requires_manual_review": True,
            },
        }

    def _run_broadcast_action_signal(
        self,
        run_id: str,
        config: AppConfig,
        should_cancel: Callable[[], bool],
        progress_plan: dict[str, tuple[float, float]],
    ) -> None:
        existing = self.get_run(run_id)
        broadcast: dict[str, Any] = existing["broadcast"] if isinstance(existing.get("broadcast"), dict) else {}
        preflight: dict[str, Any] = broadcast["preflight"] if isinstance(broadcast.get("preflight"), dict) else {}
        calibration_raw = preflight.get("calibration")
        if not isinstance(calibration_raw, dict):
            raise RuntimeError("broadcast calibration preflight is unavailable")
        calibration = ActionCalibration.from_dict(calibration_raw)

        output_dir = Path(config.output_dir).resolve()
        calibration_path = output_dir / "action_calibration.v1.json"
        try:
            contract_path = self._resolve_safe_descendant(
                output_dir,
                output_dir / TRACKING_CONTRACT_REPORT_NAME,
                expected_kind="file",
                direct=True,
            )
        except RuntimeError:
            raise RuntimeError("broadcast tracking contract is unavailable after tracking")
        contract, contract_sha256 = load_bound_json(contract_path, "broadcast tracking contract")
        source = contract.get("source") if isinstance(contract.get("source"), dict) else {}
        source_sha256 = source.get("video_sha256")
        if (
            not isinstance(source_sha256, str)
            or len(source_sha256) != 64
            or any(character not in "0123456789abcdef" for character in source_sha256)
        ):
            raise RuntimeError("broadcast tracking contract has no canonical source video sha256")

        produced_paths = [
            calibration_path,
            output_dir / ACTION_TRACK_NAME,
            output_dir / ACTION_SIGNAL_DIAGNOSTICS_NAME,
            output_dir / ACTION_SIGNAL_REPORT_NAME,
            output_dir / "action_signal_binding.v1.json",
        ]
        if any(path.exists() for path in produced_paths):
            raise RuntimeError("broadcast action-signal artifacts already exist")

        input_video = Path(config.input_video).resolve()
        source_stat = self._broadcast_source_stat_token(input_video)
        contract_evidence, terminal_shortfall_evidence = self._broadcast_action_contract_evidence(
            contract=contract,
            contract_path=contract_path,
            contract_sha256=contract_sha256,
            input_video=input_video,
            preflight=preflight,
            config=config,
        )
        expected_terminal_shortfall = (
            int(terminal_shortfall_evidence["missing_frame_count"]) if terminal_shortfall_evidence is not None else 0
        )
        try:
            publish_json_exclusive(calibration_path, calibration.to_dict(), trusted_root=output_dir)

            def update_action_progress(update: dict[str, Any]) -> None:
                self._update_run_progress(
                    run_id,
                    {
                        "stage": "action_signal",
                        "current_frame": update.get("frame_count", 0),
                        "total_frames": update.get("expected_frame_count"),
                        "percent": update.get("percent", 0.0),
                    },
                    progress_plan,
                )

            report = generate_action_track(
                input_video=input_video,
                calibration=calibration,
                output_dir=output_dir,
                start_frame=int(config.runtime.start_frame),
                max_frames=config.runtime.max_frames,
                calibration_source=calibration_path,
                progress_callback=update_action_progress,
                should_cancel=should_cancel,
                expected_terminal_shortfall_frames=expected_terminal_shortfall,
                max_terminal_shortfall_seconds=(
                    ACTION_SIGNAL_MAX_TERMINAL_SHORTFALL_SECONDS if expected_terminal_shortfall else 0.0
                ),
            )
            if should_cancel():
                raise CancelledError()
            if report.get("status") not in ACTION_SIGNAL_SUCCESS_STATUSES:
                raise RuntimeError(
                    "broadcast action signal did not complete: "
                    f"{report.get('termination_reason', report.get('status', 'unknown'))}"
                )
            if (
                report.get("source_resolution")
                != [contract_evidence["source_width"], contract_evidence["source_height"]]
                or report.get("source_frame_count") != contract_evidence["reported_frame_count"]
                or report.get("frame_count") != contract_evidence["verified_frame_count"]
                or not isinstance(report.get("fps"), (int, float))
                or isinstance(report.get("fps"), bool)
                or not math.isclose(
                    float(report["fps"]),
                    float(contract_evidence["source_fps"]),
                    rel_tol=0.0,
                    abs_tol=1e-6,
                )
            ):
                raise RuntimeError("broadcast action signal does not match the tracking contract frame evidence")
            limitations = report.get("limitations")
            if terminal_shortfall_evidence is None:
                if report.get("status") == ACTION_SIGNAL_TERMINAL_SHORTFALL_STATUS or limitations:
                    raise RuntimeError("broadcast action signal reported an untrusted terminal frame shortfall")
            else:
                limitation = limitations[0] if isinstance(limitations, list) and len(limitations) == 1 else None
                if (
                    report.get("status") != ACTION_SIGNAL_TERMINAL_SHORTFALL_STATUS
                    or report.get("termination_reason") != ACTION_SIGNAL_TERMINAL_SHORTFALL_REASON
                    or not isinstance(limitation, dict)
                    or limitation.get("code") != ACTION_SIGNAL_TERMINAL_SHORTFALL_LIMITATION
                    or limitation.get("reported_frame_count", report.get("expected_frame_count"))
                    != terminal_shortfall_evidence["reported_frame_count"]
                    or limitation.get("decoded_frame_count") != terminal_shortfall_evidence["verified_frame_count"]
                    or limitation.get("missing_terminal_frames") != terminal_shortfall_evidence["missing_frame_count"]
                    or limitation.get("requires_manual_review") is not True
                ):
                    raise RuntimeError("broadcast action signal terminal shortfall does not match its trusted audit")
            if self._broadcast_source_stat_token(input_video) != source_stat:
                raise RuntimeError("broadcast source video changed during action-signal generation")
            if sha256_file(input_video) != source_sha256:
                raise RuntimeError("broadcast action signal source does not match the tracking contract")

            binding = {
                "schema_version": "1.0",
                "artifact_type": "broadcast_action_signal_binding",
                "generated_at": _utc_now_iso(),
                "source": {
                    "video_sha256": source_sha256,
                    "tracking_contract_sha256": contract_sha256,
                },
                "artifacts": {
                    path.name: {"sha256": sha256_file(path), "size_bytes": path.stat().st_size}
                    for path in produced_paths[:-1]
                },
                "terminal_shortfall_evidence": terminal_shortfall_evidence,
            }
            publish_json_exclusive(produced_paths[-1], binding, trusted_root=output_dir)
        except BaseException:
            for path in produced_paths:
                path.unlink(missing_ok=True)
            raise

    def _broadcast_source_stat_token(self, path: Path) -> tuple[int, int, int, int, int]:
        stat = Path(path).stat()
        return (
            int(stat.st_dev),
            int(stat.st_ino),
            int(stat.st_size),
            int(stat.st_mtime_ns),
            int(stat.st_ctime_ns),
        )

    def _execute_run(
        self,
        run_id: str,
        config: AppConfig,
        cancel_event: threading.Event,
        source: str = "api",
    ) -> None:
        progress_plan = self._progress_stage_plan(
            tracking=True,
            postprocess=bool(config.postprocess.enabled),
            render=bool(config.follow_cam.enabled),
            action_signal=source == "broadcast_hybrid",
        )
        started_at = _utc_now_iso()
        self._update_run(
            run_id,
            {
                "status": "running",
                "started_at": started_at,
                "error": None,
                "progress": self._build_progress_payload(
                    {"stage": "tracking", "current_frame": 0, "total_frames": None},
                    progress_plan,
                    started_at=started_at,
                ),
            },
        )
        try:

            def cancellation_requested() -> bool:
                if cancel_event.is_set():
                    return True
                with self._lock:
                    registry = self._read_registry()
                    current = next((item for item in registry["runs"] if item.get("run_id") == run_id), None)
                metadata = current.get("broadcast") if isinstance(current, dict) else None
                requested = isinstance(metadata, dict) and metadata.get("cancel_requested") is True
                if requested:
                    cancel_event.set()
                return requested

            runner = self._tracking_runner(config)
            self._run_with_optional_progress(
                runner,
                lambda update: self._update_run_progress(run_id, update, progress_plan),
                cancellation_requested,
            )
            if cancellation_requested():
                raise CancelledError()
            if source == "broadcast_hybrid":
                self._run_broadcast_action_signal(run_id, config, cancellation_requested, progress_plan)
            if cancellation_requested():
                raise CancelledError()
            broadcast_report = publish_broadcast_facade(config.output_dir) if source == "broadcast_hybrid" else None
            if cancellation_requested():
                raise CancelledError()
            existing = self.get_run(run_id)
            updated = self._build_run_snapshot(
                run_id=run_id,
                source=source,
                status="completed",
                created_at=existing["created_at"],
                config_name=existing.get("config_name"),
                config_path=existing.get("config_path"),
                input_video=str(config.input_video),
                parent_run_id=existing.get("parent_run_id"),
                output_dir=config.output_dir,
                modules_enabled={
                    "postprocess": bool(config.postprocess.enabled),
                    "follow_cam": bool(config.follow_cam.enabled),
                    "temporal_chunks": bool(config.temporal_chunks.enabled),
                    "broadcast_hybrid": source == "broadcast_hybrid",
                },
                notes=existing.get("notes"),
                started_at=existing.get("started_at"),
                completed_at=_utc_now_iso(),
                progress=self._completed_progress(existing.get("progress"), existing.get("started_at")),
                config_sha256=existing.get("config_sha256"),
            )
            if broadcast_report is not None:
                previous_broadcast = existing.get("broadcast", {})
                broadcast_status = {
                    **(previous_broadcast if isinstance(previous_broadcast, dict) else {}),
                    "status": broadcast_report.get("status"),
                    "blocking_reasons": broadcast_report.get("blocking_reasons", []),
                    "limitations": broadcast_report.get("limitations", []),
                    "status_generation": broadcast_report.get("status_generation"),
                }
                updated["broadcast"] = broadcast_status
                updated.setdefault("stats", {})["broadcast"] = {
                    key: value for key, value in broadcast_status.items() if key != "preflight"
                }
            self._replace_run(run_id, updated)
        except CancelledError:
            existing = self.get_run(run_id)
            completed_at = _utc_now_iso()
            partial_run = {
                **existing,
                "status": "cancelled",
                "completed_at": completed_at,
                "error": None,
            }
            artifact_error = self._write_run_artifacts(config.output_dir, partial_run)
            cancelled_patch: dict[str, Any] = {
                "status": "cancelled",
                "completed_at": completed_at,
                "error": self._append_artifact_error(None, artifact_error),
                "artifacts": self._collect_artifacts(config.output_dir),
                "stats": self._collect_stats(config.output_dir),
                "progress": self._cancelled_progress(existing.get("progress"), existing.get("started_at")),
            }
            if source == "broadcast_hybrid":
                cancelled_patch["broadcast"] = {
                    **(existing.get("broadcast") if isinstance(existing.get("broadcast"), dict) else {}),
                    "status": "cancelled",
                }
            self._update_run(
                run_id,
                cancelled_patch,
            )
        except Exception as exc:
            existing = self.get_run(run_id)
            completed_at = _utc_now_iso()
            partial_run = {
                **existing,
                "status": "failed",
                "completed_at": completed_at,
                "error": str(exc),
            }
            artifact_error = self._write_run_artifacts(config.output_dir, partial_run)
            failed_patch: dict[str, Any] = {
                "status": "failed",
                "completed_at": completed_at,
                "error": self._append_artifact_error(str(exc), artifact_error),
                "artifacts": self._collect_artifacts(config.output_dir),
                "stats": self._collect_stats(config.output_dir),
                "progress": self._failed_progress(existing.get("progress"), existing.get("started_at")),
            }
            if source == "broadcast_hybrid":
                failed_patch["broadcast"] = {
                    **(existing.get("broadcast") if isinstance(existing.get("broadcast"), dict) else {}),
                    "status": "failed",
                    "error": str(exc),
                }
            self._update_run(
                run_id,
                failed_patch,
            )
        finally:
            with self._lock:
                self._active_threads.pop(run_id, None)
                self._cancel_events.pop(run_id, None)

    def _execute_approved_child_run(
        self,
        run_id: str,
        config: AppConfig,
        parent_run_id: str,
        parent_fingerprints: dict[str, tuple[int, str] | None],
        source_total_frames: int | None,
        cancel_event: threading.Event,
    ) -> None:
        progress_plan = self._progress_stage_plan(tracking=True, postprocess=False, render=False)
        started_at = _utc_now_iso()
        self._update_run(
            run_id,
            {
                "status": "running",
                "started_at": started_at,
                "error": None,
                "progress": self._build_progress_payload(
                    {"stage": "high_recall_windows", "current_frame": 0, "total_frames": None},
                    progress_plan,
                    started_at=started_at,
                ),
            },
        )
        try:
            high_recall_report: dict[str, Any] | None = None

            def run_child_high_recall(progress_callback=None, should_cancel=None) -> None:
                nonlocal high_recall_report
                report = run_high_recall_windows(
                    config,
                    source_total_frames=source_total_frames,
                    progress_callback=progress_callback,
                    should_cancel=should_cancel,
                )
                high_recall_report = report if isinstance(report, dict) else None
                windows = report.get("windows") if isinstance(report, dict) else None
                execution = report.get("execution") if isinstance(report, dict) else None
                execution_status = execution.get("status") if isinstance(execution, dict) else None
                if not windows or execution_status == "skipped":
                    raise RuntimeError("Approved child recovery produced no executable windows.")

            self._run_with_optional_progress(
                run_child_high_recall,
                lambda update: self._update_run_progress(run_id, update, progress_plan),
                cancel_event.is_set,
            )
            parent_output_dir = Path(self.get_run(parent_run_id)["output_dir"]).resolve()
            selected_artifact = self._load_approved_actions_artifact(config.output_dir / APPROVED_ACTIONS_FILE_NAME)
            apply_localize_recovery_stitches(
                parent_output_dir=parent_output_dir,
                candidate_output_dir=config.output_dir,
                selected_artifact=selected_artifact,
                csv_name=config.output.csv_name,
                high_recall_report=high_recall_report,
            )
            self._write_approved_child_candidate_audit(config)
            comparison_registration = self._write_approved_child_missing_ball_comparison(
                parent_run_id,
                config,
                high_recall_report=high_recall_report,
            )
            self._assert_parent_fingerprints_unchanged(parent_fingerprints)
            existing = self.get_run(run_id)
            updated = self._build_run_snapshot(
                run_id=run_id,
                source="approved_child_rerun",
                status="completed",
                created_at=existing["created_at"],
                config_name=existing.get("config_name"),
                config_path=existing.get("config_path"),
                input_video=str(config.input_video),
                parent_run_id=parent_run_id,
                output_dir=config.output_dir,
                modules_enabled={
                    "postprocess": False,
                    "follow_cam": False,
                    "temporal_chunks": False,
                    "high_recall_windows": True,
                },
                notes=existing.get("notes"),
                started_at=existing.get("started_at"),
                completed_at=_utc_now_iso(),
                progress=self._completed_progress(existing.get("progress"), existing.get("started_at")),
                config_sha256=existing.get("config_sha256"),
                write_artifacts=False,
            )
            artifact_error = self._write_run_manifest_and_metrics_preserving_candidate_audit(config.output_dir, updated)
            updated["artifacts"] = self._collect_artifacts(config.output_dir)
            updated["stats"] = self._collect_stats(config.output_dir)
            if artifact_error is not None:
                updated["status"] = "failed"
                updated["error"] = artifact_error
                updated["progress"] = self._failed_progress(existing.get("progress"), existing.get("started_at"))
            elif comparison_registration is not None:
                self._register_approved_child_missing_ball_candidate(**comparison_registration)
            self._replace_run(run_id, updated)
        except CancelledError:
            existing = self.get_run(run_id)
            completed_at = _utc_now_iso()
            mutation_error = self._parent_fingerprint_error(parent_fingerprints)
            status = "failed" if mutation_error else "cancelled"
            partial_run = {
                **existing,
                "status": status,
                "completed_at": completed_at,
                "error": mutation_error,
            }
            artifact_error = self._write_run_artifacts(config.output_dir, partial_run)
            self._update_run(
                run_id,
                {
                    "status": status,
                    "completed_at": completed_at,
                    "error": self._append_artifact_error(mutation_error, artifact_error),
                    "artifacts": self._collect_artifacts(config.output_dir),
                    "stats": self._collect_stats(config.output_dir),
                    "progress": (
                        self._failed_progress(existing.get("progress"), existing.get("started_at"))
                        if mutation_error
                        else self._cancelled_progress(existing.get("progress"), existing.get("started_at"))
                    ),
                },
            )
        except Exception as exc:
            existing = self.get_run(run_id)
            completed_at = _utc_now_iso()
            mutation_error = self._parent_fingerprint_error(parent_fingerprints)
            error = self._append_artifact_error(str(exc), mutation_error)
            partial_run = {
                **existing,
                "status": "failed",
                "completed_at": completed_at,
                "error": error,
            }
            artifact_error = self._write_run_artifacts(config.output_dir, partial_run)
            self._update_run(
                run_id,
                {
                    "status": "failed",
                    "completed_at": completed_at,
                    "error": self._append_artifact_error(error, artifact_error),
                    "artifacts": self._collect_artifacts(config.output_dir),
                    "stats": self._collect_stats(config.output_dir),
                    "progress": self._failed_progress(existing.get("progress"), existing.get("started_at")),
                },
            )
        finally:
            with self._lock:
                self._active_threads.pop(run_id, None)
                self._cancel_events.pop(run_id, None)

    def _write_approved_child_missing_ball_comparison(
        self,
        parent_run_id: str,
        config: AppConfig,
        *,
        high_recall_report: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        parent_run = self.get_run(parent_run_id)
        parent_output_dir = Path(parent_run["output_dir"]).resolve()
        selected_artifact_path = config.output_dir / APPROVED_ACTIONS_FILE_NAME
        selected_artifact = self._load_approved_actions_artifact(selected_artifact_path)
        recovery_actions = [
            action
            for action in selected_artifact.get("approved_actions", [])
            if (
                isinstance(action, dict)
                and action.get("approved_action") in {"localize_ball_roi", "targeted_rerun", "rerun_ball_window"}
                and isinstance(action.get("candidate_id"), str)
                and action.get("candidate_id", "").strip()
            )
        ]
        if not recovery_actions:
            return None
        recovery_actions = self._recovery_actions_with_execution_roi(recovery_actions, high_recall_report)
        approval = dict(recovery_actions[0])
        approval["related_approvals"] = recovery_actions
        candidate_id = str(approval.get("candidate_id") or "").strip()
        if not candidate_id:
            raise RuntimeError("Approved child recovery comparison requires candidate_id.")
        baseline_track = self._preferred_track_path(parent_output_dir, csv_name=config.output.csv_name)
        candidate_track = self._preferred_track_path(config.output_dir, csv_name=config.output.csv_name)
        self._ensure_localize_stitch_report_from_candidate_track(
            baseline_track=baseline_track,
            candidate_track=candidate_track,
            candidate_output_dir=config.output_dir,
            recovery_actions=recovery_actions,
        )
        comparison_path = write_missing_ball_recovery_comparison(
            config.output_dir,
            baseline_track,
            candidate_track,
            candidate_id=candidate_id,
            approval=approval,
            target_window=self._combined_recovery_action_window(recovery_actions),
            candidate_audit_path=config.output_dir / "ball_audit.json",
            require_candidate_audit=True,
            review_packets_path=config.output_dir / "review_packets.json",
            require_packet_coverage=True,
            recovery_stitch_report_path=config.output_dir / "recovery_stitch_report.json",
        )
        return {
            "parent_output_dir": parent_output_dir,
            "candidate_output_dir": config.output_dir,
            "comparison_path": comparison_path,
            "candidate_id": candidate_id,
        }

    def _ensure_localize_stitch_report_from_candidate_track(
        self,
        *,
        baseline_track: Path,
        candidate_track: Path,
        candidate_output_dir: Path,
        recovery_actions: list[dict[str, Any]],
    ) -> None:
        if (candidate_output_dir / RECOVERY_STITCH_REPORT_NAME).exists():
            return
        if not baseline_track.exists() or not candidate_track.exists():
            return
        if self._file_fingerprint(baseline_track) == self._file_fingerprint(candidate_track):
            return
        for action in recovery_actions:
            if action.get("approved_action") != "localize_ball_roi":
                continue
            effective_roi = (
                action.get("effective_roi")
                or action.get("padded_roi")
                or action.get("approved_roi")
                or action.get("local_search_roi")
            )
            temp_output = candidate_track.with_name(f".{candidate_track.name}.stitch.tmp")
            report = stitch_recovery_window(baseline_track, candidate_track, temp_output, dict(action), effective_roi)
            summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
            if summary.get("status") == "pass":
                temp_output.replace(candidate_track)
            elif temp_output.exists():
                temp_output.unlink()
            return

    def _register_approved_child_missing_ball_candidate(
        self,
        *,
        parent_output_dir: Path,
        candidate_output_dir: Path,
        comparison_path: Path,
        candidate_id: str,
    ) -> None:
        register_missing_ball_candidate(
            parent_output_dir=parent_output_dir,
            candidate_output_dir=candidate_output_dir,
            comparison_path=comparison_path,
            candidate_id=candidate_id,
        )

    def _missing_ball_candidate_artifacts(
        self,
        *,
        parent_output_dir: Path,
        candidate_output_dir: Path,
        comparison_path: Path,
        comparison_payload: dict[str, Any],
    ) -> list[str]:
        return missing_ball_candidate_artifacts(
            parent_output_dir=parent_output_dir,
            candidate_output_dir=candidate_output_dir,
            comparison_path=comparison_path,
            comparison_payload=comparison_payload,
        )

    def _write_missing_ball_candidate_manifest(
        self,
        *,
        parent_output_dir: Path,
        candidate_output_dir: Path,
        comparison_path: Path,
        manifest_path: Path,
        comparison_payload: dict[str, Any],
    ) -> None:
        write_missing_ball_candidate_manifest(
            parent_output_dir=parent_output_dir,
            candidate_output_dir=candidate_output_dir,
            comparison_path=comparison_path,
            manifest_path=manifest_path,
            comparison_payload=comparison_payload,
        )

    def _append_unique_string(self, target: list[str], value: Any) -> None:
        if isinstance(value, str) and value.strip() and value.strip() not in target:
            target.append(value.strip())

    def _recovery_actions_with_execution_roi(
        self,
        actions: list[dict[str, Any]],
        high_recall_report: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        from football_tracking.missing_ball_candidate_executor import _recovery_actions_with_execution_roi

        return _recovery_actions_with_execution_roi(actions, high_recall_report)

    def _write_approved_child_candidate_audit(self, config: AppConfig) -> None:
        write_candidate_audit(config.output_dir, csv_name=config.output.csv_name)

    def _write_run_manifest_and_metrics_preserving_candidate_audit(
        self, output_dir: Path, run: dict[str, Any]
    ) -> str | None:
        try:
            write_run_manifest_and_metrics_preserving_candidate_audit(output_dir, run)
            return None
        except Exception as exc:
            return str(exc)

    def _preferred_track_path(self, output_dir: Path, *, csv_name: str) -> Path:
        return preferred_track_path(output_dir, csv_name=csv_name)

    def _combined_recovery_action_window(self, actions: list[dict[str, Any]]) -> dict[str, int] | None:
        return combined_recovery_action_window(actions)

    def _execute_follow_cam_render(
        self,
        run_id: str,
        config: AppConfig,
        parent_run_id: str,
        cancel_event: threading.Event,
    ) -> None:
        progress_plan = self._progress_stage_plan(tracking=False, postprocess=False, render=True)
        started_at = _utc_now_iso()
        self._update_run(
            run_id,
            {
                "status": "running",
                "started_at": started_at,
                "error": None,
                "progress": self._build_progress_payload(
                    {"stage": "render", "current_frame": 0, "total_frames": None},
                    progress_plan,
                    started_at=started_at,
                ),
            },
        )
        try:
            self._run_with_optional_progress(
                FollowCamGenerator(config).run,
                lambda update: self._update_run_progress(run_id, update, progress_plan),
                cancel_event.is_set,
            )
            existing = self.get_run(run_id)
            updated = self._build_run_snapshot(
                run_id=run_id,
                source="follow_cam_render",
                status="completed",
                created_at=existing["created_at"],
                config_name=existing.get("config_name"),
                config_path=existing.get("config_path"),
                input_video=str(config.input_video),
                parent_run_id=parent_run_id,
                output_dir=config.output_dir,
                modules_enabled={"postprocess": False, "follow_cam": True},
                notes=existing.get("notes"),
                started_at=existing.get("started_at"),
                completed_at=_utc_now_iso(),
                progress=self._completed_progress(existing.get("progress"), existing.get("started_at")),
                config_sha256=existing.get("config_sha256"),
            )
            self._replace_run(run_id, updated)
        except CancelledError:
            existing = self.get_run(run_id)
            completed_at = _utc_now_iso()
            partial_run = {
                **existing,
                "status": "cancelled",
                "completed_at": completed_at,
                "error": None,
            }
            artifact_error = self._write_run_artifacts(config.output_dir, partial_run)
            self._update_run(
                run_id,
                {
                    "status": "cancelled",
                    "completed_at": completed_at,
                    "error": self._append_artifact_error(None, artifact_error),
                    "artifacts": self._collect_artifacts(config.output_dir),
                    "stats": self._collect_stats(config.output_dir),
                    "progress": self._cancelled_progress(existing.get("progress"), existing.get("started_at")),
                },
            )
        except Exception as exc:
            existing = self.get_run(run_id)
            completed_at = _utc_now_iso()
            partial_run = {
                **existing,
                "status": "failed",
                "completed_at": completed_at,
                "error": str(exc),
            }
            artifact_error = self._write_run_artifacts(config.output_dir, partial_run)
            self._update_run(
                run_id,
                {
                    "status": "failed",
                    "completed_at": completed_at,
                    "error": self._append_artifact_error(str(exc), artifact_error),
                    "artifacts": self._collect_artifacts(config.output_dir),
                    "stats": self._collect_stats(config.output_dir),
                    "progress": self._failed_progress(existing.get("progress"), existing.get("started_at")),
                },
            )
        finally:
            with self._lock:
                self._active_threads.pop(run_id, None)
                self._cancel_events.pop(run_id, None)

    def _execute_highlight_render(
        self,
        run_id: str,
        config: AppConfig,
        parent_run_id: str,
        output_video_name: str,
        selection: dict[str, Any],
        cancel_event: threading.Event,
    ) -> None:
        progress_plan = self._progress_stage_plan(tracking=False, postprocess=False, render=True)
        started_at = _utc_now_iso()
        self._update_run(
            run_id,
            {
                "status": "running",
                "started_at": started_at,
                "error": None,
                "progress": self._build_progress_payload(
                    {"stage": "render", "current_frame": 0, "total_frames": None},
                    progress_plan,
                    started_at=started_at,
                ),
            },
        )
        try:
            window = selection["window"]
            output_path = config.output_dir / output_video_name

            def run_highlight(
                progress_callback=None,
                should_cancel=None,
            ) -> None:
                renderer_report = render_highlight_clip(
                    input_video=config.input_video,
                    output_path=output_path,
                    start_frame=int(window["start_frame"]),
                    end_frame=int(window["end_frame"]),
                    progress_callback=progress_callback,
                    should_cancel=should_cancel,
                )
                self._write_highlight_report(
                    output_dir=config.output_dir,
                    source_run_id=parent_run_id,
                    input_video=config.input_video,
                    output_video_name=output_video_name,
                    selection=selection,
                    renderer_report=renderer_report,
                )

            self._run_with_optional_progress(
                run_highlight,
                lambda update: self._update_run_progress(run_id, update, progress_plan),
                cancel_event.is_set,
            )
            existing = self.get_run(run_id)
            updated = self._build_run_snapshot(
                run_id=run_id,
                source="highlight_render",
                status="completed",
                created_at=existing["created_at"],
                config_name=existing.get("config_name"),
                config_path=existing.get("config_path"),
                input_video=str(config.input_video),
                parent_run_id=parent_run_id,
                output_dir=config.output_dir,
                modules_enabled={"postprocess": False, "follow_cam": False},
                notes=existing.get("notes"),
                started_at=existing.get("started_at"),
                completed_at=_utc_now_iso(),
                progress=self._completed_progress(existing.get("progress"), existing.get("started_at")),
                config_sha256=existing.get("config_sha256"),
            )
            self._replace_run(run_id, updated)
        except CancelledError:
            existing = self.get_run(run_id)
            completed_at = _utc_now_iso()
            partial_run = {
                **existing,
                "status": "cancelled",
                "completed_at": completed_at,
                "error": None,
            }
            artifact_error = self._write_run_artifacts(config.output_dir, partial_run)
            self._update_run(
                run_id,
                {
                    "status": "cancelled",
                    "completed_at": completed_at,
                    "error": self._append_artifact_error(None, artifact_error),
                    "artifacts": self._collect_artifacts(config.output_dir),
                    "stats": self._collect_stats(config.output_dir),
                    "progress": self._cancelled_progress(existing.get("progress"), existing.get("started_at")),
                },
            )
        except Exception as exc:
            existing = self.get_run(run_id)
            completed_at = _utc_now_iso()
            partial_run = {
                **existing,
                "status": "failed",
                "completed_at": completed_at,
                "error": str(exc),
            }
            artifact_error = self._write_run_artifacts(config.output_dir, partial_run)
            self._update_run(
                run_id,
                {
                    "status": "failed",
                    "completed_at": completed_at,
                    "error": self._append_artifact_error(str(exc), artifact_error),
                    "artifacts": self._collect_artifacts(config.output_dir),
                    "stats": self._collect_stats(config.output_dir),
                    "progress": self._failed_progress(existing.get("progress"), existing.get("started_at")),
                },
            )
        finally:
            with self._lock:
                self._active_threads.pop(run_id, None)
                self._cancel_events.pop(run_id, None)

    def _assert_no_active_run_locked(self) -> None:
        running = list(self._active_threads)
        if running:
            raise RuntimeError(f"Another run is already active: {running[0]}")

    def _assert_service_open(self) -> None:
        with self._lock:
            self._assert_service_open_locked()

    def _assert_service_open_locked(self) -> None:
        if self._closing:
            raise RuntimeError("API service is closed and cannot accept mutations or queue new work")

    def _start_thread_or_cleanup(
        self,
        run_id: str,
        thread: threading.Thread,
        *,
        output_dir: Path | None = None,
        remove_output: bool = False,
    ) -> None:
        try:
            with self._lock:
                self._assert_service_open_locked()
                self._starting_threads.add(run_id)
            try:
                thread.start()
            finally:
                with self._lock:
                    self._starting_threads.discard(run_id)
        except Exception as exc:
            cleanup_error: str | None = None
            with self._lock:
                try:
                    with self._registry_transaction() as registry:
                        registry["runs"] = [run for run in registry["runs"] if run["run_id"] != run_id]
                except Exception as registry_exc:
                    cleanup_error = f"Failed to clean queued run registry after thread start failure: {registry_exc}"
                if remove_output and output_dir is not None and output_dir.exists():
                    shutil.rmtree(output_dir, ignore_errors=True)
                self._active_threads.pop(run_id, None)
                self._cancel_events.pop(run_id, None)
            if cleanup_error:
                raise RuntimeError(f"{exc} | {cleanup_error}") from exc
            raise

    def _run_with_optional_progress(self, runner, progress_callback, should_cancel) -> None:
        parameters = inspect.signature(runner).parameters
        kwargs: dict[str, Any] = {}
        if "progress_callback" in parameters:
            kwargs["progress_callback"] = progress_callback
        if "should_cancel" in parameters:
            kwargs["should_cancel"] = should_cancel
        runner(**kwargs)

    def _tracking_runner(self, config: AppConfig):
        if config.temporal_chunks.enabled:
            return lambda progress_callback=None, should_cancel=None: run_temporal_chunks(
                config,
                progress_callback=progress_callback,
                should_cancel=should_cancel,
            )
        return BallTrackingPipeline(config).run

    def _update_run(self, run_id: str, patch: dict[str, Any]) -> None:
        with self._lock:
            with self._registry_transaction() as registry:
                for run in registry["runs"]:
                    if run["run_id"] == run_id:
                        run.update(patch)
                        if "stats" in patch or "artifacts" in patch:
                            self._attach_ai_candidate_lifecycle(run)
                        return
                raise KeyError(run_id)

    def _update_run_progress(
        self, run_id: str, update: dict[str, Any], progress_plan: dict[str, tuple[float, float]]
    ) -> None:
        with self._lock:
            with self._registry_transaction() as registry:
                for run in registry["runs"]:
                    if run["run_id"] == run_id:
                        run["progress"] = self._build_progress_payload(
                            update,
                            progress_plan,
                            started_at=run.get("started_at"),
                        )
                        return
                raise KeyError(run_id)

    def _progress_stage_plan(
        self,
        *,
        tracking: bool,
        postprocess: bool,
        render: bool,
        action_signal: bool = False,
    ) -> dict[str, tuple[float, float]]:
        weights: list[tuple[str, float]] = []
        if tracking:
            weights.append(("tracking", 1.0))
        if postprocess:
            weights.append(("postprocess", 0.12))
        if action_signal:
            weights.append(("action_signal", 0.25))
        if render:
            weights.append(("render", 0.45))
        if not weights:
            weights.append(("tracking", 1.0))
        total_weight = sum(weight for _, weight in weights)
        cursor = 0.0
        plan: dict[str, tuple[float, float]] = {}
        for stage, weight in weights:
            start = cursor / total_weight
            cursor += weight
            plan[stage] = (start, cursor / total_weight)
        if tracking and "tracking" in plan:
            plan["temporal_chunks"] = plan["tracking"]
            plan["stitch"] = plan["tracking"]
        return plan

    def _initial_progress(self) -> dict[str, Any]:
        return {
            "stage": "queued",
            "current_frame": None,
            "total_frames": None,
            "percent": 0.0,
            "eta_seconds": None,
            "elapsed_seconds": None,
            "updated_at": _utc_now_iso(),
        }

    def _build_progress_payload(
        self,
        update: dict[str, Any],
        progress_plan: dict[str, tuple[float, float]],
        *,
        started_at: str | None,
    ) -> dict[str, Any]:
        stage = str(update.get("stage") or "tracking")
        current_frame = self._optional_int(update.get("current_frame"))
        total_frames = self._optional_int(update.get("total_frames"))
        if total_frames is not None and total_frames <= 0:
            total_frames = None
        if current_frame is not None and total_frames is not None:
            current_frame = max(0, min(current_frame, total_frames))

        stage_percent = 0.0
        if current_frame is not None and total_frames:
            stage_percent = min(100.0, max(0.0, (current_frame / total_frames) * 100.0))
        elif isinstance(update.get("percent"), (int, float)):
            stage_percent = min(100.0, max(0.0, float(update["percent"])))

        stage_start, stage_end = progress_plan.get(stage, (0.0, 1.0))
        percent = min(99.0, max(0.0, (stage_start + (stage_end - stage_start) * (stage_percent / 100.0)) * 100.0))
        elapsed_seconds = _seconds_since_iso(started_at)
        eta_seconds = None
        if elapsed_seconds is not None and percent > 0.5:
            eta_seconds = max(0.0, elapsed_seconds * ((100.0 - percent) / percent))
        return {
            "stage": stage,
            "current_frame": current_frame,
            "total_frames": total_frames,
            "percent": round(percent, 2),
            "chunk_index": self._optional_int(update.get("chunk_index")),
            "chunk_count": self._optional_int(update.get("chunk_count")),
            "eta_seconds": None if eta_seconds is None else round(eta_seconds, 1),
            "elapsed_seconds": None if elapsed_seconds is None else round(elapsed_seconds, 1),
            "updated_at": _utc_now_iso(),
        }

    def _completed_progress(self, current: Any, started_at: str | None) -> dict[str, Any]:
        payload = dict(current) if isinstance(current, dict) else self._initial_progress()
        elapsed_seconds = _seconds_since_iso(started_at)
        payload.update(
            {
                "stage": "completed",
                "percent": 100.0,
                "eta_seconds": 0.0,
                "elapsed_seconds": None if elapsed_seconds is None else round(elapsed_seconds, 1),
                "updated_at": _utc_now_iso(),
            }
        )
        return payload

    def _failed_progress(self, current: Any, started_at: str | None) -> dict[str, Any]:
        payload = dict(current) if isinstance(current, dict) else self._initial_progress()
        elapsed_seconds = _seconds_since_iso(started_at)
        payload.update(
            {
                "stage": "failed",
                "eta_seconds": None,
                "elapsed_seconds": None if elapsed_seconds is None else round(elapsed_seconds, 1),
                "updated_at": _utc_now_iso(),
            }
        )
        return payload

    def _cancelling_progress(self, current: Any, started_at: str | None) -> dict[str, Any]:
        payload = dict(current) if isinstance(current, dict) else self._initial_progress()
        elapsed_seconds = _seconds_since_iso(started_at)
        payload.update(
            {
                "stage": "cancelling",
                "eta_seconds": None,
                "elapsed_seconds": None if elapsed_seconds is None else round(elapsed_seconds, 1),
                "updated_at": _utc_now_iso(),
            }
        )
        return payload

    def _cancelled_progress(self, current: Any, started_at: str | None) -> dict[str, Any]:
        payload = dict(current) if isinstance(current, dict) else self._initial_progress()
        elapsed_seconds = _seconds_since_iso(started_at)
        payload.update(
            {
                "stage": "cancelled",
                "eta_seconds": None,
                "elapsed_seconds": None if elapsed_seconds is None else round(elapsed_seconds, 1),
                "updated_at": _utc_now_iso(),
            }
        )
        return payload

    def _optional_int(self, value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _replace_run(self, run_id: str, replacement: dict[str, Any]) -> None:
        self._attach_ai_candidate_lifecycle(replacement)
        with self._lock:
            with self._registry_transaction() as registry:
                for index, run in enumerate(registry["runs"]):
                    if run["run_id"] != run_id:
                        continue
                    metadata = run.get("broadcast") if isinstance(run.get("broadcast"), dict) else {}
                    if (
                        replacement.get("source") == "broadcast_hybrid"
                        and replacement.get("status") == "completed"
                        and metadata.get("cancel_requested") is True
                    ):
                        raise CancelledError()
                    registry["runs"][index] = replacement
                    return
        raise KeyError(run_id)

    def _refresh_run_artifacts_and_stats(self, run_id: str, output_dir: Path) -> None:
        with self._lock:
            with self._registry_transaction() as registry:
                for run in registry["runs"]:
                    if run["run_id"] != run_id:
                        continue
                    run["artifacts"] = self._collect_artifacts(output_dir)
                    run["stats"] = self._collect_stats(output_dir)
                    self._attach_ai_candidate_lifecycle(run)
                    return
                raise KeyError(run_id)

    def _refresh_broadcast_facade_state(self, run_id: str, output_dir: Path) -> dict[str, Any]:
        quality_report = publish_broadcast_facade(output_dir)
        with self._lock:
            with self._registry_transaction() as registry:
                for run in registry["runs"]:
                    if run.get("run_id") != run_id:
                        continue
                    broadcast = run.get("broadcast") if isinstance(run.get("broadcast"), dict) else {}
                    if broadcast.get("status") == "ready":
                        raise RuntimeError("ready broadcast artifacts are immutable")
                    run["broadcast"] = {
                        **broadcast,
                        "status": quality_report.get("status"),
                        "blocking_reasons": quality_report.get("blocking_reasons", []),
                        "limitations": quality_report.get("limitations", []),
                        "review_evidence": quality_report.get("review_evidence", {}),
                        "status_generation": quality_report.get("status_generation"),
                    }
                    run["artifacts"] = self._collect_artifacts(output_dir)
                    run["stats"] = self._collect_stats(output_dir)
                    run.setdefault("stats", {})["broadcast"] = {
                        key: value for key, value in run["broadcast"].items() if key != "preflight"
                    }
                    self._attach_ai_candidate_lifecycle(run)
                    return quality_report
                raise KeyError(run_id)

    @staticmethod
    def _require_terminal_tail_review_gate(output_dir: Path) -> str | None:
        try:
            state = inspect_terminal_tail_review(output_dir)
        except BroadcastApiError as exc:
            raise RuntimeError(f"invalid terminal-tail review evidence: {exc}") from exc
        if state.get("status") == "required":
            raise RuntimeError("terminal decoder shortfall requires operator review")
        if state.get("status") == "invalid":
            raise RuntimeError(str(state.get("reason") or "terminal-tail review evidence is invalid"))
        if state.get("status") == "accepted":
            digest = state.get("acknowledgement_sha256")
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise RuntimeError("terminal-tail acknowledgement digest is invalid")
            return digest
        if state.get("status") == "not_required":
            return None
        raise RuntimeError("terminal-tail review state is invalid")

    @classmethod
    def _terminal_tail_frozen_inputs(cls, output_dir: Path) -> dict[str, str]:
        acknowledgement_sha256 = cls._require_terminal_tail_review_gate(output_dir)
        return {"terminal_tail_review_sha256": acknowledgement_sha256} if acknowledgement_sha256 is not None else {}

    def _ensure_registry_file(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.registry_path.exists():
            self._write_registry({"runs": []})

    def _recover_interrupted_broadcast_operations(self) -> None:
        """Resolve orphaned broadcast children left queued/running by a service restart."""

        recovered_threads: list[tuple[str, str, str, Path, threading.Thread]] = []
        with self._lock:
            with self._registry_file_lock():
                registry = self._read_registry()
                changed = False
                for run in registry["runs"]:
                    source = run.get("source")
                    if source == "broadcast_hybrid" and run.get("status") in {"queued", "running"}:
                        broadcast_metadata = run.get("broadcast")
                        if not isinstance(broadcast_metadata, dict):
                            broadcast_metadata = {}
                        if self._owner_lease_is_active(
                            broadcast_metadata.get("owner_pid"),
                            broadcast_metadata.get("owner_instance_id"),
                        ):
                            continue
                        changed = True
                        cancel_requested = broadcast_metadata.get("cancel_requested") is True
                        completed_at = _utc_now_iso()
                        error = (
                            None
                            if cancel_requested
                            else "Initial broadcast run was interrupted by a service restart and cannot be resumed safely."
                        )
                        run.update(
                            {
                                "status": "cancelled" if cancel_requested else "failed",
                                "completed_at": completed_at,
                                "error": error,
                                "broadcast": {
                                    **broadcast_metadata,
                                    "status": "cancelled" if cancel_requested else "failed",
                                    "recovered": False,
                                    "recovery_status": "interrupted_initial_run",
                                },
                                "progress": (
                                    self._cancelled_progress(run.get("progress"), run.get("started_at"))
                                    if cancel_requested
                                    else self._failed_progress(run.get("progress"), run.get("started_at"))
                                ),
                            }
                        )
                        output_dir = Path(run["output_dir"]).resolve()
                        artifact_error = self._write_run_artifacts(output_dir, run)
                        run["error"] = self._append_artifact_error(error, artifact_error)
                        run["artifacts"] = self._collect_artifacts(output_dir)
                        run["stats"] = self._collect_stats(output_dir)
                        self._attach_ai_candidate_lifecycle(run)
                        continue
                    if source not in {"broadcast_hybrid_recompute", "broadcast_hybrid_render"}:
                        continue
                    if run.get("status") not in {"queued", "running"}:
                        continue
                    broadcast_metadata = run.get("broadcast")
                    if not isinstance(broadcast_metadata, dict):
                        broadcast_metadata = {}
                    output_dir = Path(run["output_dir"]).resolve()
                    report_path = output_dir / "broadcast_operation_report.v1.json"
                    report_exists = report_path.is_file()
                    report = self._read_optional_json(report_path)
                    has_commit_report = (
                        isinstance(report, dict)
                        and report.get("artifact_type") == "broadcast_operation_report"
                        and report.get("status") == "succeeded"
                    )
                    owner_pid = broadcast_metadata.get("owner_pid")
                    owner_alive = self._owner_lease_is_active(
                        owner_pid,
                        broadcast_metadata.get("owner_instance_id"),
                    )
                    if (
                        owner_alive
                        and broadcast_metadata.get("worker_exited") is not True
                        and (not has_commit_report or broadcast_metadata.get("operation_status") == "reconciling")
                    ):
                        continue
                    changed = True
                    parent = next(
                        (item for item in registry["runs"] if item.get("run_id") == run.get("parent_run_id")),
                        None,
                    )
                    cancel_requested = broadcast_metadata.get("cancel_requested") is True
                    report_operation = report.get("operation") if isinstance(report, dict) else None
                    report_frozen_inputs = broadcast_metadata.get("frozen_inputs")
                    parent_run_id = str(run.get("parent_run_id") or "")
                    if (
                        has_commit_report
                        and not cancel_requested
                        and parent is not None
                        and parent_run_id
                        and report_operation in {"recompute", "render"}
                        and source == f"broadcast_hybrid_{report_operation}"
                        and isinstance(report_frozen_inputs, dict)
                    ):
                        cancel_event = threading.Event()
                        run.update(
                            {
                                "status": "running",
                                "completed_at": None,
                                "error": None,
                                "broadcast": {
                                    **broadcast_metadata,
                                    "owner_pid": os.getpid(),
                                    "owner_instance_id": self._instance_id,
                                    "operation_status": "reconciling",
                                    "commit_started": True,
                                    "recovered": True,
                                    "worker_exited": False,
                                },
                            }
                        )
                        thread = threading.Thread(
                            target=self._reconcile_broadcast_operation_report,
                            args=(str(run["run_id"]),),
                            name=f"football-tracking-broadcast-reconcile-{report_operation}-{run['run_id']}",
                            daemon=True,
                        )
                        self._active_threads[str(run["run_id"])] = thread
                        self._cancel_events[str(run["run_id"])] = cancel_event
                        recovered_threads.append(
                            (str(run["run_id"]), parent_run_id, report_operation, output_dir, thread)
                        )
                        continue
                    if (
                        not report_exists
                        and not cancel_requested
                        and source == "broadcast_hybrid_render"
                        and broadcast_metadata.get("commit_started") is True
                        and parent is not None
                        and parent_run_id
                        and (Path(parent["output_dir"]).resolve() / "broadcast_quality_report.json").is_file()
                    ):
                        cancel_event = threading.Event()
                        run.update(
                            {
                                "status": "running",
                                "completed_at": None,
                                "error": None,
                                "broadcast": {
                                    **broadcast_metadata,
                                    "owner_pid": os.getpid(),
                                    "owner_instance_id": self._instance_id,
                                    "operation_status": "reconciling",
                                    "commit_started": True,
                                    "recovered": True,
                                    "worker_exited": False,
                                },
                            }
                        )
                        thread = threading.Thread(
                            target=self._reconcile_ready_render_without_operation_report,
                            args=(str(run["run_id"]),),
                            name=f"football-tracking-broadcast-ready-reconcile-{run['run_id']}",
                            daemon=True,
                        )
                        self._active_threads[str(run["run_id"])] = thread
                        self._cancel_events[str(run["run_id"])] = cancel_event
                        recovered_threads.append((str(run["run_id"]), parent_run_id, "render", output_dir, thread))
                        continue
                    if not report_exists and not cancel_requested and parent is not None:
                        operation = broadcast_metadata.get("operation")
                        request = broadcast_metadata.get("request")
                        frozen_inputs = broadcast_metadata.get("frozen_inputs")
                        parent_run_id = str(run.get("parent_run_id") or "")
                        if (
                            operation in {"recompute", "render"}
                            and source == f"broadcast_hybrid_{operation}"
                            and isinstance(request, dict)
                            and isinstance(frozen_inputs, dict)
                            and parent_run_id
                        ):
                            cancel_event = threading.Event()
                            run.update(
                                {
                                    "status": "queued",
                                    "started_at": None,
                                    "completed_at": None,
                                    "error": None,
                                    "broadcast": {
                                        **broadcast_metadata,
                                        "owner_pid": os.getpid(),
                                        "owner_instance_id": self._instance_id,
                                        "operation_status": "queued",
                                        "commit_started": broadcast_metadata.get("commit_started") is True,
                                        "recovered": True,
                                        "worker_exited": False,
                                    },
                                    "progress": self._initial_progress(),
                                }
                            )
                            thread = threading.Thread(
                                target=self._execute_broadcast_operation,
                                args=(
                                    str(run["run_id"]),
                                    parent_run_id,
                                    operation,
                                    deepcopy(request),
                                    deepcopy(frozen_inputs),
                                    cancel_event,
                                ),
                                name=f"football-tracking-broadcast-recovery-{operation}-{run['run_id']}",
                                daemon=True,
                            )
                            self._active_threads[str(run["run_id"])] = thread
                            self._cancel_events[str(run["run_id"])] = cancel_event
                            recovered_threads.append((str(run["run_id"]), parent_run_id, operation, output_dir, thread))
                            continue
                    recovered = False
                    if (
                        isinstance(report, dict)
                        and report.get("artifact_type") == "broadcast_operation_report"
                        and report.get("status") == "succeeded"
                        and report.get("parent_run_id") == run.get("parent_run_id")
                        and parent is not None
                        and parent.get("source") == "broadcast_hybrid"
                        and parent.get("status") == "completed"
                    ):
                        operation = report.get("operation")
                        expected_source = f"broadcast_hybrid_{operation}"
                        frozen_inputs = (
                            run.get("broadcast", {}).get("frozen_inputs")
                            if isinstance(run.get("broadcast"), dict)
                            else None
                        )
                        if (
                            operation in {"recompute", "render"}
                            and source == expected_source
                            and report.get("frozen_inputs") == frozen_inputs
                        ):
                            try:
                                parent_output = Path(parent["output_dir"]).resolve()
                                quality_report = publish_broadcast_facade(parent_output)
                                if quality_report.get("status_generation") != report.get("quality_status_generation"):
                                    raise BroadcastApiError("recovered broadcast quality generation changed")
                                result = report.get("result")
                                if not isinstance(result, dict) or result.get("status") != "completed":
                                    raise BroadcastApiError("recovered broadcast operation result is invalid")
                                trajectory_generation_id = result.get("trajectory_generation_id")
                                if not isinstance(trajectory_generation_id, str):
                                    raise BroadcastApiError("recovered trajectory generation id is invalid")
                                preflight_render_broadcast_trajectory(parent_output, trajectory_generation_id)
                                if operation == "render":
                                    final_manifest, _ = load_bound_json(
                                        parent_output / "broadcast_artifact_bindings.v1.json",
                                        "recovered final artifact bindings",
                                    )
                                    final_ids = final_manifest.get("generation_ids")
                                    if not isinstance(final_ids, dict) or any(
                                        result.get(key) != final_ids.get(key.removesuffix("_generation_id"))
                                        for key in (
                                            "trajectory_generation_id",
                                            "camera_generation_id",
                                            "render_generation_id",
                                        )
                                    ):
                                        raise BroadcastApiError("recovered final generation ids are invalid")
                                    if quality_report.get("status") != "ready":
                                        raise BroadcastApiError("recovered render facade is not ready")
                                elif quality_report.get("status") != "needs_review":
                                    raise BroadcastApiError("recovered recompute facade state is invalid")
                                self._apply_recovered_broadcast_parent(
                                    parent,
                                    operation_run_id=str(run["run_id"]),
                                    operation=operation,
                                    result=result,
                                    quality_report=quality_report,
                                    parent_output=parent_output,
                                    recovered=True,
                                )
                                recovered = True
                            except (OSError, ValueError, BroadcastApiError, BroadcastHybridOrchestrationError):
                                recovered = False
                    completed_at = _utc_now_iso()
                    broadcast = run.get("broadcast") if isinstance(run.get("broadcast"), dict) else {}
                    if recovered:
                        run.update(
                            {
                                "status": "completed",
                                "completed_at": completed_at,
                                "error": None,
                                "broadcast": {**broadcast, "operation_status": "completed", "recovered": True},
                                "progress": self._completed_progress(run.get("progress"), run.get("started_at")),
                            }
                        )
                    elif cancel_requested:
                        error = None
                        run.update(
                            {
                                "status": "cancelled",
                                "completed_at": completed_at,
                                "error": None,
                                "broadcast": {**broadcast, "operation_status": "cancelled", "recovered": False},
                                "progress": self._cancelled_progress(run.get("progress"), run.get("started_at")),
                            }
                        )
                    else:
                        error = "Broadcast operation was interrupted by a service restart before a valid commit report."
                        run.update(
                            {
                                "status": "failed",
                                "completed_at": completed_at,
                                "error": error,
                                "broadcast": {**broadcast, "operation_status": "failed", "recovered": False},
                                "progress": self._failed_progress(run.get("progress"), run.get("started_at")),
                            }
                        )
                        if parent is not None:
                            parent_broadcast = (
                                parent.get("broadcast") if isinstance(parent.get("broadcast"), dict) else {}
                            )
                            parent["broadcast"] = {
                                **parent_broadcast,
                                "last_operation": {
                                    "operation_run_id": run["run_id"],
                                    "operation": str(source).removeprefix("broadcast_hybrid_"),
                                    "status": "failed",
                                    "error": error,
                                },
                            }
                    artifact_error = self._write_run_artifacts(output_dir, run)
                    run["error"] = self._append_artifact_error(run.get("error"), artifact_error)
                    run["artifacts"] = self._collect_artifacts(output_dir)
                    run["stats"] = self._collect_stats(output_dir)
                    self._attach_ai_candidate_lifecycle(run)
                if changed:
                    self._write_registry_under_file_lock(registry)
        for operation_run_id, parent_run_id, operation, output_dir, thread in recovered_threads:
            try:
                thread.start()
            except Exception as exc:
                with self._lock:
                    self._active_threads.pop(operation_run_id, None)
                    self._cancel_events.pop(operation_run_id, None)
                self._finish_broadcast_operation_failure(
                    operation_run_id,
                    parent_run_id,
                    operation,
                    output_dir,
                    status="failed",
                    error=f"Unable to restart interrupted broadcast operation: {exc}",
                )

    def _reconcile_ready_render_without_operation_report(self, operation_run_id: str) -> None:
        operation_output: Path | None = None
        parent_run_id = ""
        authoritative_ready_validated = False
        try:
            child = self.get_run(operation_run_id)
            operation_output = Path(child["output_dir"]).resolve()
            metadata = child.get("broadcast") if isinstance(child.get("broadcast"), dict) else {}
            parent_run_id = str(child.get("parent_run_id") or "")
            request = metadata.get("request")
            frozen_inputs = metadata.get("frozen_inputs")
            if (
                child.get("source") != "broadcast_hybrid_render"
                or metadata.get("operation") != "render"
                or metadata.get("commit_started") is not True
                or not isinstance(request, dict)
                or not isinstance(frozen_inputs, dict)
            ):
                raise BroadcastApiError("ready render recovery metadata is incomplete")
            parent = self.get_run(parent_run_id)
            if parent.get("source") != "broadcast_hybrid" or parent.get("status") != "completed":
                raise BroadcastApiError("ready render recovery parent is unavailable")
            parent_output = Path(parent["output_dir"]).resolve()
            quality_report = validate_broadcast_quality_report(
                parent_output,
                parent_output / "broadcast_quality_report.json",
            )
            if quality_report.get("status") != "ready":
                raise BroadcastApiError("ready render recovery quality report is not ready")
            trajectory_generation_id = request.get("trajectory_generation_id")
            target_width = request.get("target_width")
            target_height = request.get("target_height")
            if (
                not isinstance(trajectory_generation_id, str)
                or not isinstance(target_width, int)
                or isinstance(target_width, bool)
                or not isinstance(target_height, int)
                or isinstance(target_height, bool)
            ):
                raise BroadcastApiError("ready render recovery request is invalid")
            current_inputs = preflight_render_broadcast_trajectory(
                parent_output,
                trajectory_generation_id,
                target_width=target_width,
                target_height=target_height,
            )
            current_inputs = {
                **current_inputs,
                **self._terminal_tail_frozen_inputs(parent_output),
                "parent_run_id": parent_run_id,
                "parent_output_dir": str(parent_output),
            }
            if current_inputs != frozen_inputs:
                raise BroadcastApiError("ready render recovery inputs changed")
            final_manifest, _ = load_bound_json(
                parent_output / "broadcast_artifact_bindings.v1.json",
                "ready render final artifact bindings",
            )
            final_ids = final_manifest.get("generation_ids")
            if not isinstance(final_ids, dict) or final_ids.get("trajectory") != trajectory_generation_id:
                raise BroadcastApiError("ready render recovery generation lineage is invalid")
            camera_generation_id = final_ids.get("camera")
            render_generation_id = final_ids.get("render")
            if not isinstance(camera_generation_id, str) or not isinstance(render_generation_id, str):
                raise BroadcastApiError("ready render recovery final generation ids are invalid")
            result = {
                "status": "completed",
                "trajectory_generation_id": trajectory_generation_id,
                "camera_generation_id": camera_generation_id,
                "render_generation_id": render_generation_id,
                "broadcast_video": str(parent_output / "broadcast.mp4"),
                "final_bindings": final_manifest,
                "limitations": quality_report.get("limitations", []),
            }
            operation_report = {
                "schema_version": "1.0",
                "artifact_type": "broadcast_operation_report",
                "status": "succeeded",
                "operation": "render",
                "parent_run_id": parent_run_id,
                "frozen_inputs": frozen_inputs,
                "result": result,
                "quality_status_generation": quality_report.get("status_generation"),
                "metadata_warnings": ["Recovered from authoritative ready facade after operation report loss."],
            }
            metadata_warnings = list(operation_report["metadata_warnings"])
            current_metadata = child.get("broadcast") if isinstance(child.get("broadcast"), dict) else {}
            completed_at = _utc_now_iso()
            completed_child = {
                **child,
                "status": "completed",
                "completed_at": completed_at,
                "error": None,
                "broadcast": {
                    **current_metadata,
                    "operation_status": "completed",
                    "operation_report_status": "available",
                    "commit_started": True,
                    "recovered": True,
                    "result": result,
                    "metadata_warnings": metadata_warnings,
                },
                "progress": self._completed_progress(child.get("progress"), child.get("started_at")),
            }
            artifact_error = self._write_run_artifacts(operation_output, completed_child)
            if artifact_error is not None:
                metadata_warnings.append(artifact_error)
                operation_report["metadata_warnings"] = metadata_warnings
            authoritative_ready_validated = True

            report_status = "available"
            metadata_conflict: str | None = None
            try:
                publish_json_exclusive(
                    operation_output / "broadcast_operation_report.v1.json",
                    operation_report,
                    trusted_root=operation_output,
                )
            except OSError as exc:
                report_status = "missing_after_ready_commit"
                metadata_warnings.append(f"Operation report repair failed: {exc}")
            except BroadcastApiError as exc:
                report_status = "conflict"
                metadata_conflict = str(exc)

            if report_status != "available":
                completed_child.update(
                    {
                        "status": "failed" if metadata_conflict is not None else "completed",
                        "error": (
                            f"Ready render recovered with conflicting operation metadata: {metadata_conflict}"
                            if metadata_conflict is not None
                            else None
                        ),
                        "broadcast": {
                            **completed_child["broadcast"],
                            "operation_status": ("metadata_conflict" if metadata_conflict is not None else "completed"),
                            "operation_report_status": report_status,
                            "metadata_warnings": metadata_warnings,
                        },
                        "progress": (
                            self._failed_progress(child.get("progress"), child.get("started_at"))
                            if metadata_conflict is not None
                            else completed_child["progress"]
                        ),
                    }
                )
                artifact_error = self._write_run_artifacts(operation_output, completed_child)
                if artifact_error is not None:
                    completed_child["broadcast"]["metadata_warnings"].append(artifact_error)
            completed_child["artifacts"] = self._collect_artifacts(operation_output)
            completed_child["stats"] = self._collect_stats(operation_output)
            self._attach_ai_candidate_lifecycle(completed_child)
            self._commit_broadcast_operation_registry_atomic(
                parent_run_id=parent_run_id,
                operation_run_id=operation_run_id,
                operation="render",
                result=result,
                quality_report=quality_report,
                parent_output=parent_output,
                completed_child=completed_child,
            )
        except BaseException as exc:
            if operation_output is not None and parent_run_id:
                try:
                    if authoritative_ready_validated:
                        self._request_broadcast_reconciliation_retry(
                            operation_run_id,
                            error=str(exc),
                            mode="ready_without_report",
                        )
                    else:
                        self._finish_broadcast_operation_failure(
                            operation_run_id,
                            parent_run_id,
                            "render",
                            operation_output,
                            status="failed",
                            error=str(exc),
                        )
                except Exception:
                    pass
        finally:
            with self._lock:
                self._active_threads.pop(operation_run_id, None)
                self._cancel_events.pop(operation_run_id, None)

    def _reconcile_broadcast_operation_report(self, operation_run_id: str) -> None:
        operation_output: Path | None = None
        parent_run_id = ""
        operation = "recovery"
        commit_report_validated = False
        try:
            child = self.get_run(operation_run_id)
            if child.get("status") == "completed":
                return
            operation_output = Path(child["output_dir"]).resolve()
            metadata = child.get("broadcast") if isinstance(child.get("broadcast"), dict) else {}
            parent_run_id = str(child.get("parent_run_id") or "")
            operation = str(metadata.get("operation") or "")
            frozen_inputs = metadata.get("frozen_inputs")
            report, _ = load_bound_json(
                operation_output / "broadcast_operation_report.v1.json",
                "broadcast operation commit report",
            )
            if (
                report.get("schema_version") != "1.0"
                or report.get("artifact_type") != "broadcast_operation_report"
                or report.get("status") != "succeeded"
                or report.get("parent_run_id") != parent_run_id
                or report.get("operation") != operation
                or report.get("frozen_inputs") != frozen_inputs
                or operation not in {"recompute", "render"}
            ):
                raise BroadcastApiError("broadcast operation commit report lineage is invalid")
            parent = self.get_run(parent_run_id)
            if parent.get("source") != "broadcast_hybrid" or parent.get("status") != "completed":
                raise BroadcastApiError("broadcast operation parent is unavailable during reconciliation")
            parent_output = Path(parent["output_dir"]).resolve()
            quality_report = publish_broadcast_facade(parent_output)
            if quality_report.get("status_generation") != report.get("quality_status_generation"):
                raise BroadcastApiError("recovered broadcast quality generation changed")
            result = report.get("result")
            if not isinstance(result, dict) or result.get("status") != "completed":
                raise BroadcastApiError("recovered broadcast operation result is invalid")
            trajectory_generation_id = result.get("trajectory_generation_id")
            if not isinstance(trajectory_generation_id, str):
                raise BroadcastApiError("recovered trajectory generation id is invalid")
            preflight_render_broadcast_trajectory(parent_output, trajectory_generation_id)
            if operation == "render":
                final_manifest, _ = load_bound_json(
                    parent_output / "broadcast_artifact_bindings.v1.json",
                    "recovered final artifact bindings",
                )
                final_ids = final_manifest.get("generation_ids")
                if not isinstance(final_ids, dict) or any(
                    result.get(key) != final_ids.get(key.removesuffix("_generation_id"))
                    for key in (
                        "trajectory_generation_id",
                        "camera_generation_id",
                        "render_generation_id",
                    )
                ):
                    raise BroadcastApiError("recovered final generation ids are invalid")
                if quality_report.get("status") != "ready":
                    raise BroadcastApiError("recovered render facade is not ready")
            elif quality_report.get("status") != "needs_review":
                raise BroadcastApiError("recovered recompute facade state is invalid")
            commit_report_validated = True

            completed_at = _utc_now_iso()
            current_metadata = child.get("broadcast") if isinstance(child.get("broadcast"), dict) else {}
            raw_warnings = report.get("metadata_warnings", [])
            metadata_warnings = (
                [item for item in raw_warnings if isinstance(item, str)] if isinstance(raw_warnings, list) else []
            )
            completed_child = {
                **child,
                "status": "completed",
                "completed_at": completed_at,
                "error": None,
                "broadcast": {
                    **current_metadata,
                    "operation_status": "completed",
                    "commit_started": True,
                    "recovered": True,
                    "result": _jsonable(result),
                },
                "progress": self._completed_progress(child.get("progress"), child.get("started_at")),
            }
            artifact_error = self._write_run_artifacts(operation_output, completed_child)
            if artifact_error is not None:
                metadata_warnings.append(artifact_error)
            if metadata_warnings:
                completed_child["broadcast"]["metadata_warnings"] = metadata_warnings
            completed_child["artifacts"] = self._collect_artifacts(operation_output)
            completed_child["stats"] = self._collect_stats(operation_output)
            self._attach_ai_candidate_lifecycle(completed_child)
            self._commit_broadcast_operation_registry_atomic(
                parent_run_id=parent_run_id,
                operation_run_id=operation_run_id,
                operation=operation,
                result=result,
                quality_report=quality_report,
                parent_output=parent_output,
                completed_child=completed_child,
            )
        except BaseException as exc:
            if operation_output is not None and parent_run_id:
                try:
                    if commit_report_validated:
                        self._request_broadcast_reconciliation_retry(
                            operation_run_id,
                            error=str(exc),
                            mode="operation_report",
                        )
                    else:
                        self._finish_broadcast_operation_failure(
                            operation_run_id,
                            parent_run_id,
                            operation,
                            operation_output,
                            status="failed",
                            error=str(exc),
                        )
                except Exception:
                    pass
        finally:
            with self._lock:
                self._active_threads.pop(operation_run_id, None)
                self._cancel_events.pop(operation_run_id, None)

    def _mark_broadcast_operation_reconciliation_required(self, operation_run_id: str, error: str) -> None:
        with self._lock:
            with self._registry_transaction() as registry:
                child = next((item for item in registry["runs"] if item.get("run_id") == operation_run_id), None)
                if child is None:
                    raise KeyError(operation_run_id)
                if child.get("status") == "completed":
                    return
                metadata = child.get("broadcast") if isinstance(child.get("broadcast"), dict) else {}
                progress = (
                    child.get("progress") if isinstance(child.get("progress"), dict) else self._initial_progress()
                )
                child.update(
                    {
                        "status": "running",
                        "completed_at": None,
                        "error": error,
                        "broadcast": {
                            **metadata,
                            "operation_status": "reconciling",
                            "commit_started": True,
                            "worker_exited": True,
                            "reconciliation_error": error,
                        },
                        "progress": {**progress, "stage": "reconciling", "updated_at": _utc_now_iso()},
                    }
                )

    def _request_broadcast_reconciliation_retry(self, operation_run_id: str, *, error: str, mode: str) -> None:
        try:
            self._mark_broadcast_operation_reconciliation_required(operation_run_id, error)
        except Exception:
            pass
        try:
            self._schedule_broadcast_reconciliation_retry(operation_run_id, mode=mode)
        except Exception:
            pass

    def _schedule_broadcast_reconciliation_retry(self, operation_run_id: str, *, mode: str) -> None:
        if mode not in {"operation_report", "ready_without_report"}:
            raise ValueError(f"unsupported reconciliation retry mode: {mode}")
        retry_key = f"{operation_run_id}:reconciliation-retry"
        with self._lock:
            if self._closing or retry_key in self._active_threads:
                return
            cancel_event = threading.Event()
            thread = threading.Thread(
                target=self._retry_broadcast_reconciliation,
                args=(retry_key, operation_run_id, mode, cancel_event),
                name=f"football-tracking-broadcast-retry-{operation_run_id}",
                daemon=True,
            )
            self._active_threads[retry_key] = thread
            self._cancel_events[retry_key] = cancel_event
            self._starting_threads.add(retry_key)
        try:
            thread.start()
        except Exception:
            with self._lock:
                self._active_threads.pop(retry_key, None)
                self._cancel_events.pop(retry_key, None)
            raise
        finally:
            with self._lock:
                self._starting_threads.discard(retry_key)

    def _retry_broadcast_reconciliation(
        self,
        retry_key: str,
        operation_run_id: str,
        mode: str,
        cancel_event: threading.Event,
    ) -> None:
        delay_seconds = 0.05
        try:
            while not cancel_event.wait(delay_seconds):
                if mode == "operation_report":
                    self._reconcile_broadcast_operation_report(operation_run_id)
                else:
                    self._reconcile_ready_render_without_operation_report(operation_run_id)
                try:
                    child = self.get_run(operation_run_id)
                except KeyError:
                    return
                except Exception:
                    delay_seconds = min(5.0, delay_seconds * 2.0)
                    continue
                if child.get("status") in {"completed", "failed", "cancelled"}:
                    return
                delay_seconds = min(5.0, delay_seconds * 2.0)
        finally:
            with self._lock:
                self._active_threads.pop(retry_key, None)
                self._cancel_events.pop(retry_key, None)

    def _resume_interrupted_broadcast_operation(
        self,
        *,
        run: dict[str, Any],
        parent: dict[str, Any],
        operation_output: Path,
    ) -> dict[str, Any]:
        metadata = run.get("broadcast")
        if not isinstance(metadata, dict):
            metadata = {}
        operation = metadata.get("operation")
        request = metadata.get("request")
        frozen_inputs = metadata.get("frozen_inputs")
        if (
            operation not in {"recompute", "render"}
            or not isinstance(request, dict)
            or not isinstance(frozen_inputs, dict)
        ):
            raise BroadcastApiError("interrupted broadcast operation metadata is incomplete")
        parent_run_id = str(parent.get("run_id") or "")
        parent_output = Path(parent["output_dir"]).resolve()
        if (
            parent.get("source") != "broadcast_hybrid"
            or parent.get("status") != "completed"
            or frozen_inputs.get("parent_run_id") != parent_run_id
            or frozen_inputs.get("parent_output_dir") != str(parent_output)
        ):
            raise BroadcastApiError("interrupted broadcast parent lineage changed")
        if operation == "recompute":
            current_inputs = preflight_recompute_reviewed_trajectory(parent_output)
            current_inputs = {
                **current_inputs,
                **self._terminal_tail_frozen_inputs(parent_output),
                "parent_run_id": parent_run_id,
                "parent_output_dir": str(parent_output),
            }
            if current_inputs != frozen_inputs:
                raise BroadcastApiError("interrupted recompute inputs changed")
            result = recompute_reviewed_trajectory(parent_output)
            trajectory_generation_id = result.get("trajectory_generation_id")
            if not isinstance(trajectory_generation_id, str):
                raise BroadcastApiError("resumed recompute generation is invalid")
            preflight_render_broadcast_trajectory(parent_output, trajectory_generation_id)
        else:
            generation_id = request.get("trajectory_generation_id")
            target_width = request.get("target_width")
            target_height = request.get("target_height")
            if (
                not isinstance(generation_id, str)
                or not isinstance(target_width, int)
                or not isinstance(target_height, int)
            ):
                raise BroadcastApiError("interrupted render request is invalid")
            current_inputs = preflight_render_broadcast_trajectory(
                parent_output,
                generation_id,
                target_width=target_width,
                target_height=target_height,
            )
            current_inputs = {
                **current_inputs,
                **self._terminal_tail_frozen_inputs(parent_output),
                "parent_run_id": parent_run_id,
                "parent_output_dir": str(parent_output),
            }
            if current_inputs != frozen_inputs:
                raise BroadcastApiError("interrupted render inputs changed")
            result = render_broadcast_trajectory(
                parent_output,
                generation_id,
                target_width=target_width,
                target_height=target_height,
            )
        try:
            quality_report = publish_broadcast_facade(parent_output)
        except Exception:
            if operation == "render" and (parent_output / "broadcast_artifact_bindings.v1.json").is_file():
                rollback_uncommitted_final_public_artifacts(parent_output)
            raise
        if operation == "render" and quality_report.get("status") != "ready":
            if (parent_output / "broadcast_artifact_bindings.v1.json").is_file():
                rollback_uncommitted_final_public_artifacts(parent_output)
            raise BroadcastApiError("resumed render facade is not ready")
        if operation == "recompute" and quality_report.get("status") != "needs_review":
            raise BroadcastApiError("resumed recompute facade state is invalid")
        report = {
            "schema_version": "1.0",
            "artifact_type": "broadcast_operation_report",
            "status": "succeeded",
            "operation": operation,
            "parent_run_id": parent_run_id,
            "frozen_inputs": frozen_inputs,
            "result": _jsonable(result),
            "quality_status_generation": quality_report.get("status_generation"),
            "recovered_after_restart": True,
        }
        publish_json_exclusive(
            operation_output / "broadcast_operation_report.v1.json",
            report,
            trusted_root=operation_output,
        )
        return report

    def _apply_recovered_broadcast_parent(
        self,
        parent: dict[str, Any],
        *,
        operation_run_id: str,
        operation: str,
        result: dict[str, Any],
        quality_report: dict[str, Any],
        parent_output: Path,
        recovered: bool = False,
    ) -> None:
        broadcast = parent.get("broadcast")
        if not isinstance(broadcast, dict):
            broadcast = {}
        broadcast = {
            **broadcast,
            "status": quality_report.get("status") if operation == "render" else "trajectory_ready",
            "blocking_reasons": quality_report.get("blocking_reasons", []),
            "limitations": quality_report.get("limitations", []),
            "status_generation": quality_report.get("status_generation"),
            "last_operation": {
                "operation_run_id": operation_run_id,
                "operation": operation,
                "status": "completed",
                "recovered": recovered,
            },
        }
        for key in ("trajectory_generation_id", "camera_generation_id", "render_generation_id"):
            if result.get(key):
                broadcast[key] = result[key]
        parent["broadcast"] = broadcast
        parent["artifacts"] = self._collect_artifacts(parent_output)
        parent["stats"] = self._collect_stats(parent_output)
        parent.setdefault("stats", {})["broadcast"] = {
            key: value for key, value in broadcast.items() if key != "preflight"
        }
        self._attach_ai_candidate_lifecycle(parent)

    def _acquire_service_lease(self):
        self.service_lease_dir.mkdir(parents=True, exist_ok=True)
        path = self._service_lease_path
        handle = path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            self._lock_file_handle(handle, blocking=False)
        except BaseException:
            handle.close()
            raise
        return handle

    @staticmethod
    def _release_service_lease_resources(handle: Any, path: Path) -> None:
        try:
            if not handle.closed:
                try:
                    ApiService._unlock_file_handle(handle)
                except (OSError, ValueError):
                    pass
                handle.close()
        finally:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass

    def _owner_lease_is_active(self, owner_pid: Any, owner_instance_id: Any) -> bool:
        if isinstance(owner_instance_id, str) and owner_instance_id:
            if owner_instance_id in _LIVE_SERVICE_INSTANCES:
                return True
            path = self.service_lease_dir / f"{owner_instance_id}.lock"
            if not path.is_file():
                return False
            with path.open("a+b") as handle:
                handle.seek(0)
                try:
                    self._lock_file_handle(handle, blocking=False)
                except OSError:
                    return True
                else:
                    self._unlock_file_handle(handle)
                    return False
        return isinstance(owner_pid, int) and not isinstance(owner_pid, bool) and self._process_is_alive(owner_pid)

    @staticmethod
    def _lock_file_handle(handle, *, blocking: bool) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            msvcrt.locking(handle.fileno(), mode, 1)
        else:
            import fcntl

            flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            fcntl.flock(handle.fileno(), flags)

    @staticmethod
    def _unlock_file_handle(handle) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _process_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if pid == os.getpid():
            return True
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _read_registry(self) -> dict[str, Any]:
        if not self.registry_path.exists():
            return {"runs": [], "_revision": 0}
        with self.registry_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict) or "runs" not in raw:
            return {"runs": []}
        if not isinstance(raw["runs"], list):
            raw["runs"] = []
        revision = raw.get("_revision", 0)
        raw["_revision"] = revision if isinstance(revision, int) and not isinstance(revision, bool) else 0
        return raw

    def _write_registry(self, registry: dict[str, Any]) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        with self._registry_file_lock():
            self._write_registry_under_file_lock(registry)

    def _write_registry_under_file_lock(self, registry: dict[str, Any]) -> None:
        current_revision = 0
        if self.registry_path.exists():
            try:
                with self.registry_path.open("r", encoding="utf-8") as handle:
                    current = json.load(handle)
                raw_revision = current.get("_revision", 0) if isinstance(current, dict) else 0
                if isinstance(raw_revision, int) and not isinstance(raw_revision, bool):
                    current_revision = raw_revision
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                current_revision = 0
        expected_revision = registry.get("_revision")
        if (
            isinstance(expected_revision, int)
            and not isinstance(expected_revision, bool)
            and expected_revision != current_revision
        ):
            raise RuntimeError("Run registry changed concurrently; retry the operation")
        registry["_revision"] = current_revision + 1
        descriptor, raw_temp = tempfile.mkstemp(
            prefix=f".{self.registry_path.name}.",
            suffix=".tmp",
            dir=self.registry_path.parent,
        )
        temp_path = Path(raw_temp)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(registry, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.registry_path)
        finally:
            temp_path.unlink(missing_ok=True)

    @contextmanager
    def _registry_transaction(self):
        """Read, mutate, and replace the registry under one cross-process lock."""

        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        with self._registry_file_lock():
            registry = self._read_registry()
            yield registry
            self._write_registry_under_file_lock(registry)

    @contextmanager
    def _registry_file_lock(self):
        self.registry_lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.registry_lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _refresh_discovered_runs_locked(self, registry: dict[str, Any]) -> None:
        known_by_output: dict[Path, dict[str, Any]] = {}
        for run in registry["runs"]:
            if not self._registry_run_has_safe_output(run):
                continue
            safe_output = self._resolve_safe_run_output(Path(run["output_dir"]), allow_missing=True)
            known_by_output[safe_output] = run
        config_index = self._build_config_output_index()
        for output_dir in self._iter_output_run_dirs():
            try:
                resolved_output_dir = self._resolve_safe_run_output(output_dir)
            except RuntimeError:
                continue
            if not any(output_dir.iterdir()):
                continue
            if resolved_output_dir in known_by_output:
                run = known_by_output[resolved_output_dir]
                run["artifacts"] = self._collect_artifacts(resolved_output_dir)
                run["stats"] = self._collect_stats(resolved_output_dir)
                self._attach_ai_candidate_lifecycle(run)
                continue

            config_meta = config_index.get(resolved_output_dir)
            registry["runs"].append(
                self._build_run_snapshot(
                    run_id=f"scan_{output_dir.name}",
                    source="filesystem_scan",
                    status="completed",
                    created_at=datetime.fromtimestamp(resolved_output_dir.stat().st_mtime, tz=timezone.utc).isoformat(),
                    config_name=None if config_meta is None else config_meta["name"],
                    config_path=None if config_meta is None else str(config_meta["path"]),
                    input_video=None if config_meta is None else config_meta["input_video"],
                    parent_run_id=None,
                    output_dir=resolved_output_dir,
                    modules_enabled=self._collect_module_flags(resolved_output_dir, config_meta),
                    notes=None,
                    write_artifacts=False,
                )
            )

    def _normalize_registry_runs_locked(self, registry: dict[str, Any]) -> None:
        for run in registry["runs"]:
            created_at = _normalize_iso_timestamp(run.get("created_at"))
            started_at = _normalize_iso_timestamp(run.get("started_at"))
            completed_at = _normalize_iso_timestamp(run.get("completed_at"))
            output_dir = None
            if self._registry_run_has_safe_output(run):
                output_dir = self._resolve_safe_run_output(Path(run["output_dir"]), allow_missing=True)
            output_mtime = None
            if output_dir is not None and output_dir.exists():
                output_mtime = datetime.fromtimestamp(output_dir.stat().st_mtime, tz=timezone.utc).isoformat()

            if created_at and run.get("created_at") != created_at:
                run["created_at"] = created_at
            if started_at and run.get("started_at") != started_at:
                run["started_at"] = started_at
            if completed_at and run.get("completed_at") != completed_at:
                run["completed_at"] = completed_at

            status = run.get("status")
            if status == "running" and not run.get("started_at"):
                run["started_at"] = created_at or output_mtime or _utc_now_iso()
            if status in {"completed", "failed", "cancelled"} and not run.get("completed_at"):
                run["completed_at"] = output_mtime or started_at or created_at or _utc_now_iso()
            if status == "completed" and not run.get("started_at"):
                run["started_at"] = created_at or run.get("completed_at")

    def _build_run_snapshot(
        self,
        *,
        run_id: str,
        source: str,
        status: str,
        created_at: str,
        config_name: str | None,
        config_path: str | None,
        input_video: str | None,
        parent_run_id: str | None,
        output_dir: Path,
        modules_enabled: dict[str, bool],
        notes: str | None,
        started_at: str | None = None,
        completed_at: str | None = None,
        progress: dict[str, Any] | None = None,
        config_sha256: str | None = None,
        write_artifacts: bool = True,
    ) -> dict[str, Any]:
        normalized_created_at = _normalize_iso_timestamp(created_at) or created_at
        normalized_started_at = _normalize_iso_timestamp(started_at) if started_at else None
        normalized_completed_at = _normalize_iso_timestamp(completed_at) if completed_at else None
        if status in {"completed", "failed", "cancelled"} and normalized_completed_at is None:
            normalized_completed_at = normalized_started_at or normalized_created_at
        if config_sha256 is not None:
            if (
                re.fullmatch(r"[0-9a-f]{64}", config_sha256) is None
                or not isinstance(config_path, str)
                or not config_path
            ):
                raise ValueError("Run config digest is invalid")
            try:
                current_config_sha256 = sha256_file(Path(config_path))
            except OSError as exc:
                raise ValueError("Run config digest cannot be verified") from exc
            if current_config_sha256 != config_sha256:
                raise ValueError("Run config digest changed during execution")
        snapshot = {
            "run_id": run_id,
            "source": source,
            "status": status,
            "created_at": normalized_created_at,
            "started_at": normalized_started_at,
            "completed_at": normalized_completed_at,
            "config_name": config_name,
            "config_path": config_path,
            "config_sha256": config_sha256,
            "input_video": input_video,
            "parent_run_id": parent_run_id,
            "output_dir": str(output_dir.resolve()),
            "modules_enabled": modules_enabled,
            "artifacts": [] if write_artifacts else self._collect_artifacts(output_dir),
            "stats": {} if write_artifacts else self._collect_stats(output_dir),
            "progress": progress,
            "notes": notes,
            "error": None,
        }
        self._attach_ai_candidate_lifecycle(snapshot)
        if write_artifacts:
            artifact_error = self._write_run_artifacts(output_dir, snapshot)
            snapshot["artifacts"] = self._collect_artifacts(output_dir)
            snapshot["stats"] = self._collect_stats(output_dir)
            self._attach_ai_candidate_lifecycle(snapshot)
            if artifact_error is not None:
                snapshot["status"] = "failed"
                snapshot["error"] = artifact_error
                snapshot["progress"] = self._failed_progress(progress, snapshot.get("started_at"))
        return snapshot

    def _build_config_output_index(self) -> dict[Path, dict[str, Any]]:
        index: dict[Path, dict[str, Any]] = {}
        for config_path in self.config_dir.rglob("*.yaml"):
            try:
                config = load_config(config_path)
            except Exception:
                continue
            index[config.output_dir.resolve()] = {
                "name": config_path.relative_to(self.config_dir).as_posix(),
                "path": config_path,
                "input_video": str(config.input_video),
                "postprocess_enabled": bool(config.postprocess.enabled),
                "follow_cam_enabled": bool(config.follow_cam.enabled),
            }
        return index

    def _build_config_summary(self, config_path: Path, relative_name: str) -> dict[str, Any]:
        raw = self._load_raw_yaml(config_path)
        metadata = raw.get("metadata") if isinstance(raw, dict) else None
        created_at = (
            _normalize_iso_timestamp(metadata.get("created_at") if isinstance(metadata, dict) else None)
            or _normalize_iso_timestamp(raw.get("created_at") if isinstance(raw, dict) else None)
            or datetime.fromtimestamp(config_path.stat().st_ctime, tz=timezone.utc).isoformat()
        )
        try:
            config = load_config(config_path)
            input_video = str(config.input_video)
            output_dir = str(config.output_dir)
            detector_model_path = str(config.detector.model_path)
            postprocess_enabled = bool(config.postprocess.enabled)
            follow_cam_enabled = bool(config.follow_cam.enabled)
            exists = {
                "input_video": config.input_video.exists(),
                "output_dir": config.output_dir.exists(),
                "detector_model_path": config.detector.model_path.exists(),
            }
        except Exception:
            input_video = str(raw.get("input_video", "")) or None
            output_dir = str(raw.get("output_dir", "")) or None
            detector_model_path = str((raw.get("detector") or {}).get("model_path", "")) or None
            postprocess_enabled = bool((raw.get("postprocess") or {}).get("enabled", False))
            follow_cam_enabled = bool((raw.get("follow_cam") or {}).get("enabled", False))
            exists = {"input_video": False, "output_dir": False, "detector_model_path": False}
        return {
            "name": relative_name,
            "path": str(config_path),
            "created_at": created_at,
            "input_video": input_video,
            "output_dir": output_dir,
            "detector_model_path": detector_model_path,
            "postprocess_enabled": postprocess_enabled,
            "follow_cam_enabled": follow_cam_enabled,
            "exists": exists,
        }

    def _resolve_config_path(self, name: str) -> tuple[Path, str]:
        candidate = (self.config_dir / name).resolve()
        if self.config_dir.resolve() in candidate.parents and candidate.exists() and candidate.is_file():
            return candidate, candidate.relative_to(self.config_dir).as_posix()
        if not name.endswith(".yaml"):
            candidate = (self.config_dir / f"{name}.yaml").resolve()
            if self.config_dir.resolve() in candidate.parents and candidate.exists() and candidate.is_file():
                return candidate, candidate.relative_to(self.config_dir).as_posix()
        raise FileNotFoundError(name)

    def _resolve_run_config_reference(self, run: dict[str, Any]) -> tuple[Path, str]:
        config_path_raw = run.get("config_path")
        if config_path_raw:
            config_path = Path(config_path_raw).resolve()
            if config_path.exists() and config_path.is_file():
                try:
                    relative_name = config_path.relative_to(self.config_dir.resolve()).as_posix()
                except ValueError:
                    relative_name = run.get("config_name") or config_path.name
                return config_path, relative_name
        config_name = run.get("config_name")
        if config_name:
            return self._resolve_config_path(config_name)
        raise FileNotFoundError(f"Run {run.get('run_id')} is not linked to a readable config.")

    def _resolve_input_video_name(self, name: str) -> Path:
        candidate = (self.data_dir / name).resolve()
        data_root = self.data_dir.resolve()
        if candidate != data_root and data_root not in candidate.parents:
            raise FileNotFoundError(f"Input video must live under {data_root}: {name}")
        if not candidate.exists() or not candidate.is_file():
            raise FileNotFoundError(f"Input video not found: {name}")
        return candidate

    def _resolve_input_video_path(self, input_video: str) -> Path:
        candidate = Path(input_video).resolve()
        data_root = self.data_dir.resolve()
        if candidate != data_root and data_root not in candidate.parents:
            raise FileNotFoundError(f"Input video must live under {data_root}: {input_video}")
        if not candidate.exists() or not candidate.is_file():
            raise FileNotFoundError(f"Input video not found: {input_video}")
        return candidate

    def _resolve_highlight_selection(
        self,
        source_run_id: str,
        request: dict[str, Any],
        *,
        source_total_frames: int | None = None,
    ) -> dict[str, Any]:
        approved_action_id = request.get("approved_action_id")
        if approved_action_id:
            return self._resolve_approved_highlight_selection(
                source_run_id,
                str(approved_action_id),
                request,
                source_total_frames=source_total_frames,
            )

        candidate_id = request.get("candidate_id")
        if candidate_id:
            report = self.get_event_candidates_report(source_run_id)
            candidates_raw = report.get("candidates")
            candidates = candidates_raw if isinstance(candidates_raw, list) else []
            candidate = self._candidate_by_id(candidates, str(candidate_id))
            core_window = self._candidate_core_window(candidate)
            if core_window is None:
                raise RuntimeError(f"Event candidate has no usable frame window: {candidate_id}")
            pre_roll = self._optional_int(request.get("pre_roll_frames"))
            post_roll = self._optional_int(request.get("post_roll_frames"))
            selection_source = "candidate_render_window"
            if pre_roll is not None or post_roll is not None:
                pre_roll = max(0, pre_roll if pre_roll is not None else 0)
                post_roll = max(0, post_roll if post_roll is not None else 0)
                start_frame = max(0, core_window["start_frame"] - pre_roll)
                end_frame = core_window["end_frame"] + post_roll
                selection_source = "manual_candidate_roll"
            else:
                render_window = self._window_payload(candidate.get("render_window"))
                if render_window is None:
                    start_frame = max(0, core_window["start_frame"] - 15)
                    end_frame = core_window["end_frame"] + 30
                    selection_source = "legacy_candidate_roll"
                else:
                    start_frame = render_window["start_frame"]
                    end_frame = render_window["end_frame"]
            return self._highlight_selection_payload(
                candidate_id=str(candidate_id),
                candidate=candidate,
                start_frame=start_frame,
                end_frame=end_frame,
                request=request,
                selection_source=selection_source,
            )

        start_frame = self._optional_int(request.get("start_frame"))
        end_frame = self._optional_int(request.get("end_frame"))
        if start_frame is None or end_frame is None:
            raise RuntimeError("Highlight render requires candidate_id or start_frame/end_frame.")
        return self._highlight_selection_payload(
            candidate_id=None,
            candidate=None,
            start_frame=start_frame,
            end_frame=end_frame,
            request=request,
            selection_source="explicit_frame_window",
        )

    def _resolve_approved_highlight_selection(
        self,
        source_run_id: str,
        approved_action_id: str,
        request: dict[str, Any],
        *,
        source_total_frames: int | None = None,
    ) -> dict[str, Any]:
        source_run = self.get_run(source_run_id)
        source_output_dir = Path(source_run["output_dir"]).resolve()
        approved_actions_path = source_output_dir / "ai_improvement_approved_actions.json"
        artifact = self._read_optional_json(approved_actions_path)
        if not isinstance(artifact, dict):
            raise FileNotFoundError("Approved AI improvement actions not found for highlight render.")
        actions = artifact.get("approved_actions")
        if not isinstance(actions, list):
            raise RuntimeError("ai_improvement_approved_actions.json does not contain approved_actions.")
        action = next(
            (
                item
                for item in actions
                if isinstance(item, dict) and str(item.get("approval_id") or "") == approved_action_id
            ),
            None,
        )
        if action is None:
            raise FileNotFoundError(f"Approved highlight action not found: {approved_action_id}")
        if action.get("approved_action") not in {"adjust_highlight_window", "render_suggested_highlight"}:
            raise RuntimeError(f"Approved action is not a highlight render action: {approved_action_id}")
        candidate_id = action.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise RuntimeError(f"Approved highlight action has no candidate_id: {approved_action_id}")
        window = self._window_payload(action.get("suggested_window"))
        if window is None:
            raise RuntimeError(f"Approved highlight action has no suggested_window: {approved_action_id}")

        report = self.get_event_candidates_report(source_run_id)
        candidates_raw = report.get("candidates")
        candidates = candidates_raw if isinstance(candidates_raw, list) else []
        event_candidate_id = self._approved_highlight_event_candidate_id(action, candidates)
        candidate = self._candidate_by_id(candidates, event_candidate_id)
        approval = {
            "approval_id": action.get("approval_id"),
            "improvement_id": action.get("improvement_id"),
            "approved_action": action.get("approved_action"),
            "candidate_id": candidate_id.strip(),
            "event_candidate_id": event_candidate_id,
            "clip_action": action.get("clip_action"),
            "approved_by": action.get("approved_by"),
            "approved_at": action.get("approved_at"),
            "source_approved_actions": "ai_improvement_approved_actions.json",
            "provenance": action.get("provenance") if isinstance(action.get("provenance"), dict) else {},
        }
        validation = self._validate_approved_highlight_window(
            source_output_dir=source_output_dir,
            candidate=candidate,
            approval={**action, "candidate_id": candidate_id.strip(), "event_candidate_id": event_candidate_id},
            window=window,
            approved_action_id=approved_action_id,
            source_total_frames=source_total_frames,
        )
        render_window = validation.get("render_window") if isinstance(validation.get("render_window"), dict) else window
        selection = self._highlight_selection_payload(
            candidate_id=candidate_id.strip(),
            candidate=candidate,
            start_frame=int(render_window["start_frame"]),
            end_frame=int(render_window["end_frame"]),
            request=request,
            selection_source="approved_ai_suggested_window",
            approval=approval,
        )
        if validation.get("tail_status") == "source_end_clamped":
            selection["warnings"] = [
                warning
                for warning in selection.get("warnings", [])
                if isinstance(warning, str) and "minimum post-event tail" not in warning
            ]
        return selection

    def _highlight_selection_payload(
        self,
        *,
        candidate_id: str | None,
        candidate: dict[str, Any] | None,
        start_frame: int,
        end_frame: int,
        request: dict[str, Any],
        selection_source: str,
        approval: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if start_frame < 0 or end_frame < start_frame:
            raise RuntimeError(f"Invalid highlight frame window: {start_frame}-{end_frame}")
        window = {"start_frame": start_frame, "end_frame": end_frame}
        return {
            "candidate_id": candidate_id,
            "candidate": candidate,
            "window": window,
            "selection_source": selection_source,
            "approval": approval,
            "warnings": self._highlight_window_warnings(candidate, window),
            "request": {
                "start_frame": request.get("start_frame"),
                "end_frame": request.get("end_frame"),
                "pre_roll_frames": request.get("pre_roll_frames"),
                "post_roll_frames": request.get("post_roll_frames"),
                "approved_action_id": request.get("approved_action_id"),
            },
        }

    def _approved_highlight_event_candidate_id(self, action: dict[str, Any], candidates: list[Any]) -> str:
        for key in ("event_candidate_id", "source_event_candidate_id"):
            value = action.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        legacy_candidate_id = action.get("candidate_id")
        if isinstance(legacy_candidate_id, str) and legacy_candidate_id.strip():
            legacy = legacy_candidate_id.strip()
            if any(isinstance(item, dict) and item.get("id") == legacy for item in candidates):
                return legacy
        raise RuntimeError("Approved highlight action requires event_candidate_id.")

    def _validate_approved_highlight_window(
        self,
        *,
        source_output_dir: Path,
        candidate: dict[str, Any],
        approval: dict[str, Any],
        window: dict[str, int],
        approved_action_id: str,
        source_total_frames: int | None = None,
    ) -> dict[str, Any]:
        validation = build_highlight_window_validation(
            source_output_dir,
            approval,
            source_total_frames=source_total_frames,
        )
        warnings = self._highlight_window_warnings(candidate, window)
        if validation.get("tail_status") == "source_end_clamped":
            warnings = [warning for warning in warnings if "minimum post-event tail" not in warning]
        if validation.get("status") == "pass" and not warnings:
            return validation
        validation_reasons = [
            str(check.get("reason"))
            for check in validation.get("checks", [])
            if isinstance(check, dict) and check.get("status") == "fail" and check.get("reason")
        ]
        reasons = warnings or validation_reasons
        if reasons:
            raise RuntimeError(
                f"Approved highlight action has invalid suggested_window: {approved_action_id}. " + " ".join(reasons)
            )
        return validation

    def _source_video_frame_count(self, input_video: Path) -> int | None:
        capture = cv2.VideoCapture(str(input_video))
        if not capture.isOpened():
            return None
        try:
            frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            return frame_count if frame_count > 0 else None
        finally:
            capture.release()

    def _candidate_by_id(self, candidates: list[Any], candidate_id: str) -> dict[str, Any]:
        candidate = next(
            (item for item in candidates if isinstance(item, dict) and item.get("id") == candidate_id),
            None,
        )
        if candidate is None:
            raise FileNotFoundError(f"Event candidate not found: {candidate_id}")
        return candidate

    def _candidate_core_window(self, candidate: dict[str, Any]) -> dict[str, int] | None:
        core_window = self._window_payload(candidate.get("core_window"))
        if core_window is not None:
            return core_window
        start_frame = self._optional_int(candidate.get("start_frame"))
        end_frame = self._optional_int(candidate.get("end_frame"))
        if start_frame is None or end_frame is None or end_frame < start_frame:
            return None
        return {"start_frame": start_frame, "end_frame": end_frame}

    def _window_payload(self, value: Any) -> dict[str, int] | None:
        if not isinstance(value, dict):
            return None
        start_frame = self._optional_int(value.get("start_frame"))
        end_frame = self._optional_int(value.get("end_frame"))
        if start_frame is None or end_frame is None or start_frame < 0 or end_frame < start_frame:
            return None
        return {"start_frame": start_frame, "end_frame": end_frame}

    def _highlight_window_warnings(self, candidate: dict[str, Any] | None, window: dict[str, int]) -> list[str]:
        if not isinstance(candidate, dict):
            return []
        core_window = self._candidate_core_window(candidate)
        if core_window is None:
            return ["Event candidate has no core_window; highlight tail could not be verified."]
        warnings: list[str] = []
        if window["start_frame"] > core_window["start_frame"] or window["end_frame"] < core_window["end_frame"]:
            warnings.append("Highlight window does not include the event candidate core_window.")
        min_tail = None
        if isinstance(candidate.get("buffer_policy"), dict):
            min_tail = self._optional_int(candidate["buffer_policy"].get("min_tail_frames"))
            if min_tail is None:
                min_tail = self._optional_int(candidate["buffer_policy"].get("min_post_event_frames"))
            if min_tail is None:
                min_tail = self._optional_int(candidate["buffer_policy"].get("post_buffer_frames"))
        if min_tail is not None and min_tail > 0:
            required_end = core_window["end_frame"] + min_tail
            if window["end_frame"] < required_end:
                warnings.append(
                    f"Highlight window does not preserve the minimum post-event tail: end_frame {window['end_frame']} < {required_end}."
                )
        return warnings

    def _prepare_highlight_render_inputs(
        self, source_output_dir: Path, render_output_dir: Path, config: AppConfig
    ) -> None:
        self._prepare_follow_cam_render_inputs(
            source_output_dir=source_output_dir,
            render_output_dir=render_output_dir,
            config=config,
        )
        event_candidates = source_output_dir / "event_candidates.json"
        if event_candidates.exists() and event_candidates.is_file():
            shutil.copy2(event_candidates, render_output_dir / event_candidates.name)

    def _write_highlight_report(
        self,
        *,
        output_dir: Path,
        source_run_id: str,
        input_video: Path,
        output_video_name: str,
        selection: dict[str, Any],
        renderer_report: dict[str, Any],
    ) -> None:
        payload = {
            "schema_version": "1.0",
            "source_run_id": source_run_id,
            "input_video": str(input_video),
            "output_video": output_video_name,
            "candidate_id": selection.get("candidate_id"),
            "candidate_type": (
                None if not isinstance(selection.get("candidate"), dict) else selection["candidate"].get("type")
            ),
            "window": selection["window"],
            "selection_source": selection.get("selection_source"),
            "request": selection.get("request") or {},
            "approval": selection.get("approval"),
            "warnings": selection.get("warnings") or [],
            "renderer": renderer_report,
            "candidate": selection.get("candidate"),
        }
        (output_dir / "highlight_report.json").write_text(
            json.dumps(_jsonable(payload), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _assert_follow_cam_only_render_allowed(self, source_output_dir: Path) -> None:
        plan_path = source_output_dir / FOLLOW_CAM_RERENDER_PLAN_FILE_NAME
        if not plan_path.exists():
            return
        plan = self._read_optional_json(plan_path)
        if plan is None:
            raise RuntimeError("follow_cam_rerender_plan.json is corrupt or invalid; cannot determine rerender safety.")
        if not isinstance(plan, dict) or not bool(plan.get("requires_tracking_rerun")):
            return
        scope = plan.get("tracking_rerun_scope")
        scope_text = f" scope={scope}" if isinstance(scope, dict) else ""
        raise RuntimeError(
            "follow-cam rerender plan requires tracking rerun before follow-cam-only render; "
            f"run the approved tracking rerun first.{scope_text}"
        )

    def _prepare_follow_cam_render_inputs(
        self, source_output_dir: Path, render_output_dir: Path, config: AppConfig
    ) -> None:
        raw_csv = source_output_dir / config.output.csv_name
        cleaned_csv = source_output_dir / config.postprocess.cleaned_csv_name
        copied_any = False
        for candidate in (raw_csv, cleaned_csv):
            if not candidate.exists() or not candidate.is_file():
                continue
            shutil.copy2(candidate, render_output_dir / candidate.name)
            copied_any = True
        if not copied_any:
            raise FileNotFoundError(f"Run output does not contain a usable track CSV: {source_output_dir}")

    def _assert_path_not_used_by_active_run_locked(
        self,
        *,
        input_video: Path | None = None,
        config_path: Path | None = None,
    ) -> None:
        registry = self._read_registry()
        for run in registry["runs"]:
            if run.get("status") not in {"queued", "running"}:
                continue
            if input_video and run.get("input_video"):
                run_input = Path(run["input_video"]).resolve()
                if run_input == input_video.resolve():
                    raise RuntimeError(f"File is used by active run: {run['run_id']}")
            if config_path and run.get("config_path"):
                run_config = Path(run["config_path"]).resolve()
                if run_config == config_path.resolve():
                    raise RuntimeError(f"Config is used by active run: {run['run_id']}")

    def _sample_video_frames(self, video_path: Path) -> list[dict[str, Any]]:
        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            return []

        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fractions = [0.18, 0.5, 0.82] if frame_count > 3 else [0.5]
        samples: list[dict[str, Any]] = []
        warmup_frames = 48

        for index, fraction in enumerate(fractions, start=1):
            frame_index = max(0, int(round((max(frame_count, 1) - 1) * fraction)))
            ok, frame = self._read_frame_with_warmup(capture, frame_index, warmup_frames)
            if not ok or frame is None:
                continue
            frame_height, frame_width = frame.shape[:2]
            samples.append(
                {
                    "frame": frame,
                    "frame_index": frame_index,
                    "frame_time_seconds": frame_index / fps if fps > 0 else 0.0,
                    "frame_width": int(frame_width),
                    "frame_height": int(frame_height),
                    "sample_index": index,
                    "sample_count": len(fractions),
                }
            )

        if not samples:
            capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = capture.read()
            if ok and frame is not None:
                frame_height, frame_width = frame.shape[:2]
                samples.append(
                    {
                        "frame": frame,
                        "frame_index": 0,
                        "frame_time_seconds": 0.0,
                        "frame_width": int(frame_width),
                        "frame_height": int(frame_height),
                        "sample_index": 1,
                        "sample_count": 1,
                    }
                )

        capture.release()
        return samples

    def _pick_field_preview_sample(self, video_path: Path, sample_index: int | None = None) -> dict[str, Any]:
        samples = self._sample_video_frames(video_path)
        if not samples:
            raise RuntimeError(f"Unable to read preview frames from input video: {video_path}")
        if sample_index is not None:
            clamped_index = max(1, min(int(sample_index), len(samples)))
            return samples[clamped_index - 1]
        return samples[len(samples) // 2]

    def _read_video_frame(self, video_path: Path, frame_index: int) -> dict[str, Any]:
        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            raise RuntimeError(f"Unable to open input video: {video_path}")
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        clamped_index = max(0, min(frame_index, max(frame_count - 1, 0)))
        ok, frame = self._read_frame_with_warmup(capture, clamped_index, warmup_frames=48)
        capture.release()
        if not ok or frame is None:
            raise RuntimeError(f"Unable to read preview frame {clamped_index} from input video: {video_path}")
        frame_height, frame_width = frame.shape[:2]
        return {
            "frame": frame,
            "frame_index": clamped_index,
            "frame_time_seconds": clamped_index / fps if fps > 0 else 0.0,
            "frame_width": int(frame_width),
            "frame_height": int(frame_height),
            "sample_index": 1,
            "sample_count": 1,
        }

    def _read_frame_with_warmup(self, capture: Any, frame_index: int, warmup_frames: int) -> tuple[bool, Any]:
        seek_start = max(0, frame_index - warmup_frames)
        capture.set(cv2.CAP_PROP_POS_FRAMES, seek_start)
        ok = False
        frame = None
        for _ in range(frame_index - seek_start + 1):
            ok, frame = capture.read()
            if not ok or frame is None:
                break
        return ok, frame

    def _detect_field_polygon(
        self,
        frame: Any,
        content_bounds: tuple[int, int, int, int],
    ) -> tuple[list[tuple[int, int]], float, bool]:
        frame_height, frame_width = frame.shape[:2]
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, (28, 28, 20), (96, 255, 255))
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

        content_x1, content_y1, content_x2, content_y2 = content_bounds
        content_width = max(1, content_x2 - content_x1)
        content_height = max(1, content_y2 - content_y1)
        content_mask = mask[content_y1:content_y2, content_x1:content_x2]
        coverage = float(cv2.countNonZero(content_mask)) / float(content_width * content_height)

        band_height = max(6, int(round(content_height * 0.03)))
        top_span = self._mask_row_span(
            mask, content_x1, content_x2, int(round(content_y1 + content_height * 0.18)), band_height
        )
        bottom_span = self._mask_row_span(
            mask, content_x1, content_x2, int(round(content_y1 + content_height * 0.92)), band_height
        )
        upper_contour = self._estimate_upper_field_contour(mask, content_bounds, point_count=7)

        if coverage >= 0.08 and upper_contour and bottom_span:
            polygon = self._clip_polygon(
                upper_contour
                + [
                    (content_x2, max(bottom_span[2], int(round(content_y1 + content_height * 0.96)))),
                    (content_x1, max(bottom_span[2], int(round(content_y1 + content_height * 0.96)))),
                ],
                frame_width=frame_width,
                frame_height=frame_height,
            )
            return polygon, coverage, True

        if coverage >= 0.08 and top_span and bottom_span:
            polygon = self._clip_polygon(
                self._polygon_to_nine_point_field(
                    [
                        (top_span[0], top_span[2]),
                        (top_span[1], top_span[2]),
                        (bottom_span[1], bottom_span[2]),
                        (bottom_span[0], bottom_span[2]),
                    ]
                ),
                frame_width=frame_width,
                frame_height=frame_height,
            )
            return polygon, coverage, True

        return self._default_field_polygon(content_bounds), 0.0, False

    def _estimate_upper_field_contour(
        self,
        mask: Any,
        content_bounds: tuple[int, int, int, int],
        *,
        point_count: int,
    ) -> list[tuple[int, int]] | None:
        content_x1, content_y1, content_x2, content_y2 = content_bounds
        content_width = max(1, content_x2 - content_x1)
        content_height = max(1, content_y2 - content_y1)
        search_y2 = min(mask.shape[0], int(round(content_y1 + content_height * 0.72)))
        if search_y2 <= content_y1:
            return None

        slice_half_width = max(18, int(round(content_width * 0.06)))
        ratios = np.linspace(0.0, 1.0, point_count)
        points: list[tuple[int, int]] = []

        for index, ratio in enumerate(ratios):
            anchor_x = int(round(content_x1 + ratio * content_width))
            slice_x1 = max(content_x1, anchor_x - slice_half_width)
            slice_x2 = min(content_x2, anchor_x + slice_half_width)
            if slice_x2 <= slice_x1:
                return None

            band = mask[content_y1:search_y2, slice_x1:slice_x2]
            band_points = cv2.findNonZero(band)
            if band_points is None:
                return None

            normalized_points = band_points.reshape(-1, 2)
            ys = normalized_points[:, 1]
            xs = normalized_points[:, 0]
            percentile = 8 if index in {0, point_count - 1} else 12
            point_y = content_y1 + int(round(float(np.percentile(ys, percentile))))
            if index == 0:
                point_x = slice_x1 + int(round(float(np.percentile(xs, 8))))
            elif index == point_count - 1:
                point_x = slice_x1 + int(round(float(np.percentile(xs, 92))))
            else:
                point_x = anchor_x
            points.append((point_x, point_y))

        smoothed: list[tuple[int, int]] = []
        for index, (point_x, _) in enumerate(points):
            neighbor_ys = [points[neighbor][1] for neighbor in range(max(0, index - 1), min(len(points), index + 2))]
            smoothed.append((point_x, int(round(sum(neighbor_ys) / len(neighbor_ys)))))
        return smoothed

    def _detect_content_bounds(self, frame: Any) -> tuple[int, int, int, int]:
        frame_height, frame_width = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        _, threshold = cv2.threshold(gray, 10, 255, cv2.THRESH_BINARY)
        points = cv2.findNonZero(threshold)
        if points is None:
            return (0, 0, frame_width, frame_height)
        x, y, width, height = cv2.boundingRect(points)
        return (x, y, x + width, y + height)

    def _mask_row_span(
        self,
        mask: Any,
        x1: int,
        x2: int,
        y_center: int,
        band_height: int,
    ) -> tuple[int, int, int] | None:
        y1 = max(0, y_center - band_height)
        y2 = min(mask.shape[0], y_center + band_height)
        if y2 <= y1 or x2 <= x1:
            return None
        band = mask[y1:y2, x1:x2]
        points = cv2.findNonZero(band)
        if points is None:
            return None
        xs = points.reshape(-1, 2)[:, 0]
        return (x1 + int(xs.min()), x1 + int(xs.max()), int(round((y1 + y2) / 2.0)))

    def _default_field_polygon(self, content_bounds: tuple[int, int, int, int]) -> list[tuple[int, int]]:
        x1, y1, x2, y2 = content_bounds
        width = max(1, x2 - x1)
        height = max(1, y2 - y1)
        if width / float(height) >= 2.6:
            return self._polygon_to_nine_point_field(
                [
                    (int(round(x1 + width * 0.18)), int(round(y1 + height * 0.18))),
                    (int(round(x1 + width * 0.82)), int(round(y1 + height * 0.18))),
                    (int(round(x1 + width * 0.98)), int(round(y1 + height * 0.96))),
                    (int(round(x1 + width * 0.02)), int(round(y1 + height * 0.96))),
                ]
            )
        return self._polygon_to_nine_point_field(
            self._roi_to_polygon(
                (
                    int(round(x1 + width * 0.08)),
                    int(round(y1 + height * 0.10)),
                    int(round(x2 - width * 0.08)),
                    int(round(y2 - height * 0.06)),
                )
            )
        )

    def _roi_to_polygon(self, roi: tuple[int, int, int, int]) -> list[tuple[int, int]]:
        x1, y1, x2, y2 = roi
        return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]

    def _polygon_to_nine_point_field(self, polygon: list[tuple[int, int]]) -> list[tuple[int, int]]:
        if len(polygon) != 4:
            return polygon
        top_left, top_right, bottom_right, bottom_left = polygon
        top_ratios = [0.0, 0.18, 0.36, 0.5, 0.64, 0.82, 1.0]
        top_edge = [
            (
                int(round(top_left[0] + (top_right[0] - top_left[0]) * ratio)),
                int(round(top_left[1] + (top_right[1] - top_left[1]) * ratio)),
            )
            for ratio in top_ratios
        ]
        return top_edge + [bottom_right, bottom_left]

    def _polygon_bounds(self, polygon: list[tuple[int, int]]) -> tuple[int, int, int, int]:
        xs = [point[0] for point in polygon]
        ys = [point[1] for point in polygon]
        return (min(xs), min(ys), max(xs), max(ys))

    def _clip_polygon(
        self,
        polygon: list[tuple[int, int]],
        *,
        frame_width: int,
        frame_height: int,
    ) -> list[tuple[int, int]]:
        max_x = max(0, frame_width - 1)
        max_y = max(0, frame_height - 1)
        return [
            (
                max(0, min(max_x, int(round(x)))),
                max(0, min(max_y, int(round(y)))),
            )
            for x, y in polygon
        ]

    def _expand_polygon(
        self,
        polygon: list[tuple[int, int]],
        *,
        frame_width: int,
        frame_height: int,
        scale_x: float,
        scale_y: float,
    ) -> list[tuple[int, int]]:
        bounds = self._polygon_bounds(polygon)
        center_x = (bounds[0] + bounds[2]) / 2.0
        center_y = (bounds[1] + bounds[3]) / 2.0
        expanded: list[tuple[int, int]] = []
        for x, y in polygon:
            expanded.append(
                (
                    int(round(center_x + (x - center_x) * scale_x)),
                    int(round(center_y + (y - center_y) * scale_y)),
                )
            )
        return self._clip_polygon(expanded, frame_width=frame_width, frame_height=frame_height)

    def _build_preview_bounds(
        self,
        *,
        expanded_polygon: list[tuple[int, int]],
        content_bounds: tuple[int, int, int, int],
        frame_width: int,
        frame_height: int,
    ) -> tuple[int, int, int, int]:
        content_x1, content_y1, content_x2, content_y2 = content_bounds
        box_x1, box_y1, box_x2, box_y2 = self._polygon_bounds(expanded_polygon)
        pad_x = max(12, int(round((content_x2 - content_x1) * 0.04)))
        pad_y = max(12, int(round((content_y2 - content_y1) * 0.04)))
        return (
            max(0, max(content_x1, box_x1 - pad_x)),
            max(0, max(content_y1, box_y1 - pad_y)),
            min(frame_width, min(content_x2, box_x2 + pad_x)),
            min(frame_height, min(content_y2, box_y2 + pad_y)),
        )

    def _normalize_points(self, raw_points: Any) -> list[tuple[int, int]]:
        if not isinstance(raw_points, list):
            return []
        points: list[tuple[int, int]] = []
        for raw_point in raw_points:
            if not isinstance(raw_point, list) or len(raw_point) != 2:
                return []
            points.append((int(raw_point[0]), int(raw_point[1])))
        return points

    def _load_field_setup_from_config(
        self,
        *,
        config_name: str,
        frame_width: int,
        frame_height: int,
    ) -> dict[str, Any] | None:
        config_path, _ = self._resolve_config_path(config_name)
        raw = self._load_raw_yaml(config_path)
        filtering_raw = raw.get("filtering") or {}
        scene_bias_raw = raw.get("scene_bias") or {}
        ground_zones = scene_bias_raw.get("ground_zones") or []
        positive_rois = scene_bias_raw.get("positive_rois") or []

        field_polygon: list[tuple[int, int]] = []
        expanded_polygon: list[tuple[int, int]] = []

        for zone in ground_zones:
            if not isinstance(zone, dict):
                continue
            field_polygon = self._normalize_points(zone.get("points"))
            if field_polygon:
                break
            roi = zone.get("roi")
            if isinstance(roi, list) and len(roi) == 4:
                field_polygon = self._roi_to_polygon(tuple(int(value) for value in roi))
                break

        for zone in positive_rois:
            if not isinstance(zone, dict):
                continue
            expanded_polygon = self._normalize_points(zone.get("points"))
            if expanded_polygon:
                break
            roi = zone.get("roi")
            if isinstance(roi, list) and len(roi) == 4:
                expanded_polygon = self._roi_to_polygon(tuple(int(value) for value in roi))
                break

        if not expanded_polygon:
            roi = filtering_raw.get("roi")
            if isinstance(roi, list) and len(roi) == 4:
                expanded_polygon = self._roi_to_polygon(tuple(int(value) for value in roi))

        if not field_polygon:
            roi = filtering_raw.get("roi")
            if isinstance(roi, list) and len(roi) == 4:
                field_polygon = self._roi_to_polygon(tuple(int(value) for value in roi))

        if not field_polygon:
            return None

        field_polygon = self._clip_polygon(field_polygon, frame_width=frame_width, frame_height=frame_height)
        if not expanded_polygon:
            expanded_polygon = self._expand_polygon(
                field_polygon,
                frame_width=frame_width,
                frame_height=frame_height,
                scale_x=1.08,
                scale_y=1.10,
            )
        else:
            expanded_polygon = self._clip_polygon(expanded_polygon, frame_width=frame_width, frame_height=frame_height)

        return {
            "field_polygon": field_polygon,
            "expanded_polygon": expanded_polygon,
            "field_roi": self._polygon_bounds(field_polygon),
            "expanded_roi": self._polygon_bounds(expanded_polygon),
        }

    def _pad_roi(
        self,
        roi: tuple[int, int, int, int],
        frame_width: int,
        frame_height: int,
        pad_x_ratio: float,
        pad_y_ratio: float,
    ) -> tuple[int, int, int, int]:
        x1, y1, x2, y2 = roi
        pad_x = int(round(frame_width * pad_x_ratio))
        pad_y = int(round(frame_height * pad_y_ratio))
        return (
            max(0, x1 - pad_x),
            max(0, y1 - pad_y),
            min(frame_width, x2 + pad_x),
            min(frame_height, y2 + pad_y),
        )

    def _expand_roi(
        self,
        roi: tuple[int, int, int, int],
        *,
        frame_width: int,
        frame_height: int,
        padding_x_ratio: float,
        padding_y_ratio: float,
    ) -> tuple[int, int, int, int]:
        return self._pad_roi(
            roi,
            frame_width=frame_width,
            frame_height=frame_height,
            pad_x_ratio=padding_x_ratio,
            pad_y_ratio=padding_y_ratio,
        )

    def _encode_frame_data_url(self, frame: Any) -> str:
        ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        if not ok:
            raise RuntimeError("Unable to encode preview frame.")
        payload = base64.b64encode(encoded.tobytes()).decode("ascii")
        return f"data:image/jpeg;base64,{payload}"

    def _prepare_preview_frame(self, frame: Any, max_width: int = 1600) -> Any:
        frame_height, frame_width = frame.shape[:2]
        if frame_width <= max_width:
            return frame
        scale = max_width / float(frame_width)
        target_size = (max_width, max(1, int(round(frame_height * scale))))
        return cv2.resize(frame, target_size, interpolation=cv2.INTER_AREA)

    def _build_field_config_patch(
        self,
        *,
        field_polygon: list[tuple[int, int]],
        expanded_polygon: list[tuple[int, int]],
        expanded_roi: tuple[int, int, int, int],
    ) -> dict[str, Any]:
        expanded_width = max(1, expanded_roi[2] - expanded_roi[0])
        expanded_height = max(1, expanded_roi[3] - expanded_roi[1])
        return {
            "filtering": {
                "roi": list(expanded_roi),
            },
            "scene_bias": {
                "enabled": True,
                "ground_zones": [
                    {
                        "name": "field_core",
                        "points": [list(point) for point in field_polygon],
                    }
                ],
                "positive_rois": [
                    {
                        "name": "field_buffer",
                        "points": [list(point) for point in expanded_polygon],
                    }
                ],
                "dynamic_air_recovery": {
                    "enabled": True,
                    "edge_reentry_expand_x": float(expanded_width),
                    "edge_reentry_expand_y": float(expanded_height),
                },
            },
        }

    def _materialize_run_config(
        self,
        *,
        base_config_path: Path,
        base_config_name: str,
        run_id: str,
        patch: dict[str, Any],
        suffix: str,
        exclusive: bool = False,
        base_config_snapshot: _YamlConfigSnapshot | None = None,
    ) -> tuple[Path, str, _YamlConfigSnapshot]:
        output_name = f"{Path(base_config_name).stem}_{suffix}_{run_id}.yaml"
        self.generated_config_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.generated_config_dir / output_name
        if exclusive and output_path.exists():
            raise FileExistsError(str(output_path))
        source_snapshot = base_config_snapshot or self._capture_yaml_config_snapshot(base_config_path)
        if base_config_snapshot is not None:
            self._verify_source_config_snapshot(
                source_snapshot,
                error="production_trial base config changed during creation",
            )
        merged = _deep_merge(source_snapshot.raw, patch)
        content = yaml.safe_dump(
            merged,
            sort_keys=False,
            allow_unicode=False,
        ).encode("utf-8")
        snapshot = _YamlConfigSnapshot(
            path=output_path,
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            raw=self._parse_yaml_config_bytes(content, output_path),
        )
        published = False
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self.generated_config_dir,
                prefix=f".{output_name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if exclusive:
                os.link(temporary_path, output_path)
                published = True
                temporary_path.unlink()
            else:
                os.replace(temporary_path, output_path)
                published = True
            temporary_path = None
            self._verify_materialized_config_snapshot(snapshot)
        except BaseException:
            if published:
                output_path.unlink(missing_ok=True)
            raise
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return (
            output_path,
            output_path.relative_to(self.config_dir).as_posix(),
            snapshot,
        )

    def _capture_yaml_config_snapshot(self, config_path: Path) -> _YamlConfigSnapshot:
        try:
            content = config_path.read_bytes()
        except OSError as exc:
            raise ValueError(f"Config snapshot is unavailable: {config_path}") from exc
        return _YamlConfigSnapshot(
            path=config_path,
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            raw=self._parse_yaml_config_bytes(content, config_path),
        )

    @staticmethod
    def _parse_yaml_config_bytes(content: bytes, config_path: Path) -> dict[str, Any]:
        try:
            loaded = yaml.safe_load(content.decode("utf-8")) or {}
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise ValueError(f"Invalid config YAML in {config_path}") from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"Invalid config root in {config_path}")
        return loaded

    @staticmethod
    def _verify_source_config_snapshot(
        snapshot: _YamlConfigSnapshot,
        *,
        error: str,
    ) -> None:
        try:
            current_sha256 = sha256_file(snapshot.path)
        except OSError as exc:
            raise ValueError(error) from exc
        if current_sha256 != snapshot.sha256:
            raise ValueError(error)

    @staticmethod
    def _verify_materialized_config_snapshot(snapshot: _YamlConfigSnapshot) -> None:
        try:
            persisted = snapshot.path.read_bytes()
        except OSError as exc:
            raise ValueError("production_trial materialized config snapshot changed") from exc
        if persisted != snapshot.content or hashlib.sha256(persisted).hexdigest() != snapshot.sha256:
            raise ValueError("production_trial materialized config snapshot changed")

    def _build_run_output_dir(self, *, run_id: str, input_video: Path | None) -> Path:
        input_group = self._input_group_slug(input_video)
        return self._resolve_safe_descendant(
            self.outputs_dir,
            self.run_outputs_dir / input_group / run_id,
            expected_kind="directory",
            allow_missing=True,
        )

    def _resolve_render_video_name(self, requested_name: Any, *, default_name: str) -> str:
        raw_name = str(requested_name or default_name).strip() or default_name
        output_name = Path(raw_name).name
        if output_name != raw_name:
            raise RuntimeError("Output video name must be a file name, not a path.")
        if Path(output_name).suffix.lower() != ".mp4":
            raise RuntimeError("Output video name must end with .mp4.")
        reserved_names = {
            "ball_track.csv",
            "ball_track.cleaned.csv",
            "camera_path.csv",
            "run_manifest.json",
            "metrics_report.json",
            "cleanup_report.json",
            "follow_cam_report.json",
            "ball_audit.json",
            "ai_review_triggers.json",
            "event_candidates.json",
            "player_tracks.json",
            "highlight_report.json",
        }
        if output_name in reserved_names:
            raise RuntimeError(f"Output video name is reserved: {output_name}")
        return output_name

    def _input_group_slug(self, input_video: Path | None) -> str:
        if input_video is None:
            return "unbound"
        resolved_input = input_video.resolve()
        try:
            relative = resolved_input.relative_to(self.data_dir.resolve())
            slug_source = str(relative.with_suffix("")).replace("\\", "/").replace("/", "_")
        except ValueError:
            slug_source = resolved_input.stem
        return self._slugify(slug_source) or "input"

    def _run_activity_at(self, run: dict[str, Any]) -> str | None:
        return (
            _normalize_iso_timestamp(run.get("completed_at"))
            or _normalize_iso_timestamp(run.get("started_at"))
            or _normalize_iso_timestamp(run.get("created_at"))
        )

    def _timestamp_value(self, value: str | None) -> float:
        normalized = _normalize_iso_timestamp(value)
        if not normalized:
            return 0.0
        return datetime.fromisoformat(normalized).timestamp()

    @staticmethod
    def _is_link_or_reparse_point(path: Path) -> bool:
        try:
            metadata = Path(path).lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
        return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)

    @staticmethod
    def _normalize_filesystem_path(path: Path) -> Path:
        """Normalize Windows extended-length and ordinary absolute paths to one lexical form."""

        text = os.path.abspath(os.fspath(path))
        if os.name == "nt":
            folded = text.casefold()
            if folded.startswith("\\\\?\\unc\\"):
                text = "\\\\" + text[8:]
            elif folded.startswith("\\\\?\\"):
                text = text[4:]
        return Path(text)

    def _resolve_safe_descendant(
        self,
        root: Path,
        candidate: Path,
        *,
        expected_kind: str,
        direct: bool = False,
        allow_missing: bool = False,
    ) -> Path:
        """Resolve a non-reparse descendant while preserving lexical containment checks."""

        lexical_root = self._normalize_filesystem_path(root)
        lexical_candidate = self._normalize_filesystem_path(candidate)
        if self._is_link_or_reparse_point(lexical_root):
            raise RuntimeError(f"Artifact root cannot be a link or reparse point: {lexical_root}")
        try:
            relative = lexical_candidate.relative_to(lexical_root)
        except ValueError as exc:
            raise RuntimeError(f"Path must stay under {lexical_root}: {lexical_candidate}") from exc
        if not relative.parts or (direct and len(relative.parts) != 1):
            raise RuntimeError(f"Path must be a direct descendant of {lexical_root}: {lexical_candidate}")

        current = lexical_root
        for part in relative.parts:
            current = current / part
            if self._is_link_or_reparse_point(current):
                raise RuntimeError(f"Path cannot traverse a link or reparse point: {current}")

        resolved_root = self._normalize_filesystem_path(lexical_root.resolve())
        resolved_candidate = self._normalize_filesystem_path(lexical_candidate.resolve())
        if not self._path_is_relative_to(resolved_candidate, resolved_root) or resolved_candidate == resolved_root:
            raise RuntimeError(f"Resolved path must stay under {resolved_root}: {resolved_candidate}")
        current = lexical_root
        if self._is_link_or_reparse_point(current):
            raise RuntimeError(f"Artifact root cannot be a link or reparse point: {current}")
        for part in relative.parts:
            current = current / part
            if self._is_link_or_reparse_point(current):
                raise RuntimeError(f"Path cannot traverse a link or reparse point: {current}")
        if not allow_missing or resolved_candidate.exists():
            if expected_kind == "directory" and not resolved_candidate.is_dir():
                raise RuntimeError(f"Expected an artifact directory: {resolved_candidate}")
            if expected_kind == "file" and not resolved_candidate.is_file():
                raise RuntimeError(f"Expected an artifact file: {resolved_candidate}")
        return resolved_candidate

    def _resolve_safe_run_output(self, output_dir: Path, *, allow_missing: bool = False) -> Path:
        return self._resolve_safe_descendant(
            self.outputs_dir,
            output_dir,
            expected_kind="directory",
            allow_missing=allow_missing,
        )

    def _registry_run_has_safe_output(self, run: dict[str, Any]) -> bool:
        raw_output_dir = run.get("output_dir")
        if not isinstance(raw_output_dir, str) or not raw_output_dir:
            return False
        try:
            self._resolve_safe_run_output(Path(raw_output_dir), allow_missing=True)
        except RuntimeError:
            return False
        return True

    def _iter_output_run_dirs(self) -> list[Path]:
        if not self.outputs_dir.is_dir() or self._is_link_or_reparse_point(self.outputs_dir):
            return []
        discovered: list[Path] = []
        for child in sorted(self.outputs_dir.iterdir(), key=lambda item: item.name):
            try:
                self._resolve_safe_descendant(
                    self.outputs_dir,
                    child,
                    expected_kind="directory",
                    direct=True,
                )
            except RuntimeError:
                continue
            if child.name == "api_runs":
                discovered.extend(
                    sorted(
                        (item for item in child.iterdir() if self._is_safe_direct_directory(child, item)),
                        key=lambda item: item.name,
                    )
                )
                continue
            if child.name == "runs":
                for input_group_dir in sorted(
                    (item for item in child.iterdir() if self._is_safe_direct_directory(child, item)),
                    key=lambda item: item.name,
                ):
                    discovered.extend(
                        sorted(
                            (
                                item
                                for item in input_group_dir.iterdir()
                                if self._is_safe_direct_directory(input_group_dir, item)
                            ),
                            key=lambda item: item.name,
                        )
                    )
                continue
            if child.name == "_scratch":
                continue
            discovered.append(child)
        return discovered

    def _is_safe_direct_directory(self, root: Path, candidate: Path) -> bool:
        try:
            self._resolve_safe_descendant(
                root,
                candidate,
                expected_kind="directory",
                direct=True,
            )
        except RuntimeError:
            return False
        return True

    def _is_safe_direct_file(self, root: Path, candidate: Path) -> bool:
        try:
            self._resolve_safe_descendant(
                root,
                candidate,
                expected_kind="file",
                direct=True,
            )
        except RuntimeError:
            return False
        return True

    def _read_safe_direct_json(self, root: Path, name: str) -> dict[str, Any] | None:
        try:
            path = self._resolve_safe_descendant(
                root,
                root / name,
                expected_kind="file",
                direct=True,
            )
        except RuntimeError:
            return None
        return self._read_optional_json(path)

    def _load_raw_yaml(self, config_path: Path) -> dict[str, Any]:
        with config_path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"Invalid config root in {config_path}")
        return loaded

    def _collect_module_flags(self, output_dir: Path, config_meta: dict[str, Any] | None) -> dict[str, bool]:
        if config_meta is not None:
            return {
                "postprocess": bool(config_meta.get("postprocess_enabled", False)),
                "follow_cam": bool(config_meta.get("follow_cam_enabled", False)),
                "temporal_chunks": (output_dir / "temporal_chunks_report.json").exists(),
                "broadcast_hybrid": (output_dir / "action_signal_binding.v1.json").exists(),
            }
        return {
            "postprocess": (output_dir / "cleanup_report.json").exists(),
            "follow_cam": (output_dir / "follow_cam_report.json").exists(),
            "temporal_chunks": (output_dir / "temporal_chunks_report.json").exists(),
            "broadcast_hybrid": (output_dir / "action_signal_binding.v1.json").exists(),
        }

    def _collect_artifacts(self, output_dir: Path) -> list[dict[str, Any]]:
        try:
            output_dir = self._resolve_safe_run_output(output_dir)
        except RuntimeError:
            return []
        artifacts: list[dict[str, Any]] = []
        for artifact_path in self._iter_artifact_paths(output_dir):
            try:
                artifact_path = self._resolve_safe_descendant(
                    output_dir,
                    artifact_path,
                    expected_kind="file",
                )
            except RuntimeError:
                continue
            relative_name = artifact_path.relative_to(output_dir).as_posix()
            content_type, _ = mimetypes.guess_type(str(artifact_path))
            artifacts.append(
                {
                    "name": relative_name,
                    "path": str(artifact_path.resolve()),
                    "kind": self._artifact_kind(artifact_path),
                    "exists": artifact_path.exists(),
                    "size_bytes": artifact_path.stat().st_size,
                    "content_type": content_type,
                }
            )
        return artifacts

    def _iter_artifact_paths(self, output_dir: Path) -> list[Path]:
        try:
            output_dir = self._resolve_safe_run_output(output_dir)
        except RuntimeError:
            return []
        artifact_paths = [
            item.resolve()
            for item in output_dir.iterdir()
            if not item.name.startswith(".") and self._is_safe_direct_file(output_dir, item)
        ]
        chunk_names = self._temporal_chunk_names(output_dir)
        chunk_roots, allow_nested_contracts = self._temporal_chunk_artifact_roots(output_dir)
        for chunk_root in chunk_roots:
            artifact_paths.extend(
                item.resolve()
                for chunk_dir in sorted(
                    (
                        item
                        for item in chunk_root.iterdir()
                        if item.name in chunk_names and self._is_safe_direct_directory(chunk_root, item)
                    ),
                    key=lambda item: item.name,
                )
                for item in chunk_dir.iterdir()
                if self._is_temporal_chunk_artifact(
                    chunk_root,
                    item,
                    allow_tracking_contract=allow_nested_contracts,
                )
            )
        artifact_paths.extend(self._broadcast_nested_artifact_paths(output_dir))
        normalized_root = self._normalize_filesystem_path(output_dir)
        normalized: dict[str, Path] = {}
        for artifact_path in artifact_paths:
            candidate = self._normalize_filesystem_path(artifact_path)
            try:
                relative = candidate.relative_to(normalized_root)
            except ValueError:
                continue
            if any(part.startswith(".") for part in relative.parts):
                continue
            normalized[relative.as_posix()] = candidate
        return [normalized[name] for name in sorted(normalized)]

    def _broadcast_nested_artifact_paths(self, output_dir: Path) -> list[Path]:
        """Expand only status and source-report files named by trusted broadcast manifests."""

        try:
            output_dir = self._resolve_safe_run_output(output_dir)
        except RuntimeError:
            return []
        paths: set[Path] = set()
        try:
            queue_path = self._resolve_safe_descendant(
                output_dir,
                output_dir / "selective_review_queue.v1.json",
                expected_kind="file",
                direct=True,
            )
            for evidence_path in collect_review_evidence_paths(queue_path, output_dir):
                paths.add(
                    self._resolve_safe_descendant(
                        output_dir,
                        evidence_path,
                        expected_kind="file",
                    )
                )
        except (RuntimeError, BroadcastApiError):
            pass

        try:
            status_root = self._resolve_safe_descendant(
                output_dir,
                output_dir / "broadcast_status",
                expected_kind="directory",
                direct=True,
            )
        except RuntimeError:
            status_root = None
        if status_root is not None:
            for generation in status_root.iterdir():
                if len(generation.name) != 64 or any(
                    character not in "0123456789abcdef" for character in generation.name
                ):
                    continue
                try:
                    safe_generation = self._resolve_safe_descendant(
                        status_root,
                        generation,
                        expected_kind="directory",
                        direct=True,
                    )
                    report_path = self._resolve_safe_descendant(
                        safe_generation,
                        safe_generation / "broadcast_quality_report.json",
                        expected_kind="file",
                        direct=True,
                    )
                    report = validate_broadcast_quality_report(output_dir, report_path)
                except (RuntimeError, BroadcastApiError):
                    continue
                if report.get("status_generation") == generation.name:
                    paths.add(report_path)

        try:
            quality_path = self._resolve_safe_descendant(
                output_dir,
                output_dir / "broadcast_quality_report.json",
                expected_kind="file",
                direct=True,
            )
            manifest_path = self._resolve_safe_descendant(
                output_dir,
                output_dir / "broadcast_artifact_bindings.v1.json",
                expected_kind="file",
                direct=True,
            )
            quality, _ = load_bound_json(quality_path, "broadcast quality report")
            manifest, manifest_sha256 = load_bound_json(manifest_path, "broadcast final artifact bindings")
        except (RuntimeError, BroadcastApiError):
            return sorted(paths)
        stable_quality = {
            key: value for key, value in quality.items() if key not in {"generated_at", "status_generation"}
        }
        expected_status_generation = hashlib.sha256(
            json.dumps(
                stable_quality,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        if (
            quality.get("artifact_type") != "broadcast_quality_report"
            or quality.get("status") != "ready"
            or quality.get("status_generation") != expected_status_generation
            or quality.get("final_bindings")
            != {"path": "broadcast_artifact_bindings.v1.json", "sha256": manifest_sha256}
            or manifest.get("artifact_type") != "broadcast_artifact_bindings"
        ):
            return sorted(paths)
        bindings = manifest.get("artifacts")
        if not isinstance(bindings, dict):
            return sorted(paths)
        for raw_binding in bindings.values():
            if not isinstance(raw_binding, dict):
                continue
            raw_report = raw_binding.get("source_report")
            if not isinstance(raw_report, dict):
                continue
            raw_path = raw_report.get("path")
            expected_sha256 = raw_report.get("sha256")
            if (
                not isinstance(raw_path, str)
                or not raw_path
                or not isinstance(expected_sha256, str)
                or len(expected_sha256) != 64
            ):
                continue
            relative = Path(raw_path)
            if relative.is_absolute() or ".." in relative.parts:
                continue
            try:
                candidate = self._resolve_safe_descendant(
                    output_dir,
                    output_dir / relative,
                    expected_kind="file",
                )
            except RuntimeError:
                continue
            if sha256_file(candidate) == expected_sha256:
                paths.add(candidate)
        return sorted(paths)

    @staticmethod
    def _artifact_identity_token(path: Path) -> tuple[int, int, int, int, int]:
        metadata = Path(path).stat()
        return (
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_size),
            int(metadata.st_mtime_ns),
            int(metadata.st_ctime_ns),
        )

    def _temporal_chunk_artifact_roots(self, output_dir: Path) -> tuple[list[Path], bool]:
        report = self._read_safe_direct_json(output_dir, "temporal_chunks_report.json")
        if report is None:
            return [], False
        if "chunks_root_name" not in report:
            return self._legacy_temporal_chunk_artifact_roots(output_dir), False
        root_name = report.get("chunks_root_name")
        if (
            not isinstance(root_name, str)
            or not root_name
            or root_name in {".", ".."}
            or "/" in root_name
            or "\\" in root_name
            or Path(root_name).name != root_name
        ):
            return [], False
        try:
            root = self._resolve_safe_descendant(
                output_dir,
                output_dir / root_name,
                expected_kind="directory",
                direct=True,
            )
        except RuntimeError:
            return [], False
        stitch = report.get("stitch")
        allow_nested_contracts = isinstance(stitch, dict) and stitch.get("status") == "succeeded"
        return [root], allow_nested_contracts

    def _legacy_temporal_chunk_artifact_roots(self, output_dir: Path) -> list[Path]:
        chunk_names = self._temporal_chunk_names(output_dir)
        if not chunk_names:
            return []
        roots: dict[Path, Path] = {}
        for candidate_root in output_dir.iterdir():
            try:
                resolved_root = self._resolve_safe_descendant(
                    output_dir,
                    candidate_root,
                    expected_kind="directory",
                    direct=True,
                )
            except RuntimeError:
                continue
            if any(
                self._is_safe_direct_directory(resolved_root, resolved_root / chunk_name) for chunk_name in chunk_names
            ):
                roots[resolved_root] = resolved_root
        return sorted(roots.values(), key=lambda item: item.relative_to(output_dir).as_posix())

    def _temporal_chunk_names(self, output_dir: Path) -> set[str]:
        report = self._read_safe_direct_json(output_dir, "temporal_chunks_report.json")
        if report is None:
            return set()
        names: set[str] = set()

        def add_name(value: Any) -> None:
            if (
                isinstance(value, str)
                and value.startswith("chunk_")
                and value not in {".", ".."}
                and "/" not in value
                and "\\" not in value
                and Path(value).name == value
            ):
                names.add(value)

        chunks = report.get("chunks")
        if isinstance(chunks, list):
            for chunk in chunks:
                if isinstance(chunk, dict):
                    add_name(chunk.get("name"))
        source_chunk_names = report.get("source_chunk_names")
        if isinstance(source_chunk_names, list):
            for name in source_chunk_names:
                add_name(name)
        return names

    def _is_temporal_chunk_artifact(
        self,
        chunk_root: Path,
        artifact_path: Path,
        *,
        allow_tracking_contract: bool,
    ) -> bool:
        try:
            relative = artifact_path.relative_to(chunk_root)
        except ValueError:
            return False
        if len(relative.parts) != 2:
            return False
        chunk_name = relative.parts[0]
        if not chunk_name.startswith("chunk_"):
            return False
        try:
            resolved_chunk_dir = self._resolve_safe_descendant(
                chunk_root,
                chunk_root / chunk_name,
                expected_kind="directory",
                direct=True,
            )
            self._resolve_safe_descendant(
                resolved_chunk_dir,
                artifact_path,
                expected_kind="file",
                direct=True,
            )
        except RuntimeError:
            return False
        if artifact_path.suffix.lower() in {".csv", ".jsonl"}:
            return True
        if artifact_path.name == TRACKING_CONTRACT_REPORT_NAME:
            return allow_tracking_contract
        return artifact_path.name in {
            "chunk_config.yaml",
            "worker.stdout.log",
            "worker.stderr.log",
        }

    def _artifact_kind(self, artifact_path: Path) -> str:
        suffix = artifact_path.suffix.lower()
        if suffix == ".mp4":
            return "video"
        if suffix == ".csv":
            return "csv"
        if suffix == ".jsonl":
            return "jsonl"
        if suffix == ".json":
            return "json"
        return "file"

    def _attach_ai_candidate_lifecycle(self, run: dict[str, Any]) -> None:
        output_dir = run.get("output_dir")
        if not output_dir:
            return
        lifecycle = self._collect_ai_candidate_lifecycle(Path(output_dir))
        run["ai_candidate_lifecycle"] = lifecycle
        stats = run.get("stats") if isinstance(run.get("stats"), dict) else {}
        stats["ai_candidate_lifecycle"] = lifecycle["summary"]
        run["stats"] = stats

    @staticmethod
    def _attach_trial_signal_gate(run: dict[str, Any]) -> None:
        stats = run.get("stats")
        gate = stats.get("trial_signal_gate_v2") if isinstance(stats, dict) else None
        run["trial_signal_gate_v2"] = deepcopy(gate) if isinstance(gate, dict) else None

    def _collect_ai_candidate_lifecycle(self, output_dir: Path) -> dict[str, Any]:
        return build_ai_candidate_lifecycle(output_dir)

    def _collect_stats(self, output_dir: Path) -> dict[str, Any]:
        try:
            output_dir = self._resolve_safe_run_output(output_dir)
        except RuntimeError:
            return {}
        metrics_report = self._read_safe_direct_json(output_dir, "metrics_report.json")
        stats = stats_from_metrics_report(metrics_report) if metrics_report is not None else {}
        raw_track = output_dir / "ball_track.csv"
        cleaned_track = output_dir / "ball_track.cleaned.csv"
        raw_summary = stats.get("raw") or (
            self._summarize_track_csv(raw_track) if self._is_safe_direct_file(output_dir, raw_track) else None
        )
        cleaned_summary = stats.get("cleaned") or (
            self._summarize_track_csv(cleaned_track) if self._is_safe_direct_file(output_dir, cleaned_track) else None
        )
        cleanup_report = self._read_safe_direct_json(output_dir, "cleanup_report.json")
        follow_cam_report = self._read_safe_direct_json(output_dir, "follow_cam_report.json")
        ball_audit_report = self._read_safe_direct_json(output_dir, "ball_audit.json")
        ai_review_trigger_report = self._read_safe_direct_json(output_dir, "ai_review_triggers.json")
        event_candidate_report = self._read_safe_direct_json(output_dir, "event_candidates.json")
        player_tracks_report = self._read_safe_direct_json(output_dir, "player_tracks.json")
        ai_improvement_report = self._read_safe_direct_json(output_dir, "ai_improvement_report.json")
        if raw_summary is not None:
            stats["raw"] = raw_summary
        if cleaned_summary is not None:
            stats["cleaned"] = cleaned_summary
        if cleanup_report is not None:
            stats["cleanup"] = cleanup_report
        if follow_cam_report is not None:
            stats["follow_cam"] = follow_cam_report
        if ball_audit_report is not None and "ball_audit" not in stats:
            ball_audit_summary = compact_ball_audit_summary(ball_audit_report)
            if ball_audit_summary is not None:
                stats["ball_audit"] = ball_audit_summary
        if ai_review_trigger_report is not None and "ai_review_triggers" not in stats:
            ai_review_trigger_summary = compact_ai_review_trigger_summary(ai_review_trigger_report)
            if ai_review_trigger_summary is not None:
                stats["ai_review_triggers"] = ai_review_trigger_summary
        if event_candidate_report is not None and "event_candidates" not in stats:
            event_candidate_summary = compact_event_candidate_summary(event_candidate_report)
            if event_candidate_summary is not None:
                stats["event_candidates"] = event_candidate_summary
        if player_tracks_report is not None and "player_tracks" not in stats:
            player_tracks_summary = compact_player_tracks_summary(player_tracks_report)
            if player_tracks_summary is not None:
                stats["player_tracks"] = player_tracks_summary
        if ai_improvement_report is not None and "ai_improvement" not in stats:
            ai_improvement_summary = compact_ai_improvement_summary(ai_improvement_report)
            if ai_improvement_summary is not None:
                stats["ai_improvement"] = ai_improvement_summary
        return stats

    def _summarize_track_csv(self, csv_path: Path) -> dict[str, Any] | None:
        return compute_track_metrics(csv_path)

    def _write_run_artifacts(self, output_dir: Path, run: dict[str, Any]) -> str | None:
        try:
            write_run_artifacts(output_dir=output_dir, run=run)
        except Exception as exc:
            return f"Failed to write run manifest/metrics: {exc}"
        return None

    def _append_artifact_error(self, existing_error: str | None, artifact_error: str | None) -> str | None:
        if artifact_error is None:
            return existing_error
        if existing_error:
            return f"{existing_error} | {artifact_error}"
        return artifact_error

    def _load_optional_json_artifact(self, run_id: str, name: str) -> dict[str, Any]:
        artifact_path = self._get_internal_artifact_path(run_id, name)
        try:
            with artifact_path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            raise FileNotFoundError(name) from exc
        if not isinstance(loaded, dict):
            raise FileNotFoundError(name)
        return loaded

    def _read_optional_json(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return None
        return loaded if isinstance(loaded, dict) else None

    def _slugify(self, text: str) -> str:
        normalized = unicodedata.normalize("NFKD", text.strip()).encode("ascii", "ignore").decode("ascii")
        cleaned = "".join(char.lower() if char.isalnum() else "_" for char in normalized)
        collapsed = "_".join(filter(None, cleaned.split("_")))
        return collapsed[:48] or "ai_update"
