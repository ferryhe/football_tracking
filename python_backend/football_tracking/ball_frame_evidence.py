from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from typing import Any

from football_tracking.ball_detector_annotations import (
    BallAnnotationError,
    annotation_etag,
    validate_ball_annotation,
)
from football_tracking.ball_detector_feasibility import (
    FeasibilityError,
    inherit_temporal_group,
    temporal_group_for_frame,
)
from football_tracking.detector_audited_authority import (
    AUDITED_T2_LEGACY_PROBE_BINDINGS,
)
from football_tracking.detector_development_common import (
    DetectorDevelopmentError,
    canonical_sha256,
    require_safe_id,
    require_sha256,
)
from football_tracking.review_proxy_mapping import (
    ReviewProxyError,
    validate_review_proxy_manifest,
)

TIMING_PROFILE_ID = "verified_decoder_pos_msec_after_frame_position_v1"
TIMING_NOT_COLLECTED_PROFILE_ID = "source_pos_msec_not_collected_proxy_cfr_verified_v1"
POSITION_VERIFICATION = "opencv_next_frame_index_with_0.25_tolerance"
PROXY_INDEX_POSITION_VERIFICATION = "verified_review_proxy_frame_index_mapping_v1"
DECODER_TIMING_OBSERVATION_METHOD = "opencv_cap_prop_pos_msec_after_verified_frame_read"
DISPLAY_TIME_DERIVATION = "frame_index_divided_by_fps_for_display_only_not_source_pts"
_MAX_ABSOLUTE_DECODER_POS_MSEC = 7 * 24 * 60 * 60 * 1000.0
_DECODE_MODES = frozenset(
    {
        "sequential",
        "preroll_verified",
        "direct_verified",
        "sequential_fallback",
    }
)
_LIGHTING_STRATA = frozenset({"bright_sun", "shadow", "backlight", "twilight", "artificial_light"})
_PROBE_RESOURCE_FIELDS = (
    "parent_trial_id",
    "source_id",
    "source_sha256",
    "source_file_identity_sha256",
    "tracking_contract_sha256",
    "base_config_relative_path",
    "base_config_sha256",
    "effective_config_relative_path",
    "effective_config_sha256",
    "trial_intent_sha256",
    "tuning_patch_sha256",
)
_PROBE_AUTHORITY_FIELDS = frozenset(
    {
        "schema_version",
        "artifact_type",
        "job_id",
        "request_sha256",
        "intent_sha256",
        "semantic_intent_sha256",
        "resource_sha256",
        "frozen_profiles_sha256",
        "execution_bundle_sha256",
        "runtime_environment_sha256",
        "retry_from_job_id",
        "retry_kind",
        "frozen_request",
        "frozen_profiles",
        "probe_report_sha256",
        "probe_result_manifest_sha256",
        "probe_report",
        "probe_result_manifest",
        "probe_job_record",
        "canonical_job_record_sha256",
        "audit_anchor_kind",
        "job_record_authority_sha256",
    }
)
_SESSION_REQUEST_AUTHORITY_FIELDS = frozenset(
    {
        "schema_version",
        "artifact_type",
        "session_id",
        "request_sha256",
        "normalized_request",
        "authority_sha256",
    }
)
_NORMALIZED_SESSION_REQUEST_FIELDS = frozenset(
    {
        "data_role",
        "development_probe_job_ids",
        "locked_profile_id",
        "target_frame_count",
        "sampling_profile_id",
        "metric_profile_id",
        "operator_id",
        "strata_applicability",
        "applicable_scale_strata",
        "applicable_lighting_strata",
        "retry_from_session_id",
        "development_package_session_id",
        "development_package_sha256",
    }
)

_TEMPORAL_GROUP_FIELDS = frozenset(
    {
        "group_id",
        "profile_id",
        "source_sha256",
        "seed_frame_index",
        "start_frame",
        "end_frame",
        "derivative_family",
        "canonical_moment_id",
        "derivative_family_id",
        "ancestry_profile",
    }
)
_TIMING_FIELDS = frozenset(
    {
        "schema_version",
        "artifact_type",
        "timing_profile_id",
        "timing_status",
        "source_sha256",
        "runtime_environment_sha256",
        "source_frame_jpeg_sha256",
        "frame_index",
        "decoded_frame_position",
        "fps",
        "effective_decode_mode",
        "decoder_reported_pos_msec",
        "decoder_time_seconds",
        "decoder_timing_observation_method",
        "display_time_seconds",
        "display_time_derivation",
        "true_presentation_timestamp",
        "position_verification",
        "cross_decode_verification",
        "timing_binding_sha256",
    }
)


def build_detector_probe_result_manifest_authority(
    report: Any,
) -> tuple[dict[str, Any], str]:
    """Rebuild the exact lower-service result-manifest file from its report."""

    if not isinstance(report, dict):
        raise BallFrameEvidenceError("detector probe report authority is missing")
    if report.get("schema_version") != "1.0" or report.get("artifact_type") != "detector_probe_report":
        raise BallFrameEvidenceError("detector probe report authority type is invalid")
    job_id = require_safe_id(report.get("job_id"), "detector probe report job_id")
    request_sha256 = _sha256(report.get("request_sha256"), "detector probe report request sha256")
    report_sha256 = _sha256(report.get("report_sha256"), "detector probe report content sha256")
    report_core = {key: value for key, value in report.items() if key != "report_sha256"}
    if canonical_sha256(report_core) != report_sha256:
        raise BallFrameEvidenceError("detector probe report content digest is invalid")
    source = report.get("source")
    lineage = report.get("lineage")
    artifacts = report.get("artifacts")
    if not isinstance(source, dict) or not isinstance(lineage, dict) or not isinstance(artifacts, list):
        raise BallFrameEvidenceError("detector probe report cannot rebuild its result manifest")
    source_file_identity_sha256 = _sha256(
        source.get("file_identity_sha256"),
        "detector probe source file identity sha256",
    )
    frozen_profiles_sha256 = _sha256(
        lineage.get("frozen_profiles_sha256"),
        "detector probe frozen profiles sha256",
    )
    execution_bundle_sha256 = _sha256(
        lineage.get("execution_bundle_sha256"),
        "detector probe execution bundle sha256",
    )
    runtime_environment_sha256 = _sha256(
        lineage.get("runtime_environment_sha256"),
        "detector probe runtime environment sha256",
    )
    report_bytes = _atomic_json_file_bytes(report)
    manifest = {
        "schema_version": "1.0",
        "artifact_type": "detector_probe_result_manifest",
        "job_id": job_id,
        "request_sha256": request_sha256,
        "frozen_profiles_sha256": frozen_profiles_sha256,
        "execution_bundle_sha256": execution_bundle_sha256,
        "runtime_environment_sha256": runtime_environment_sha256,
        "source_file_identity_sha256": source_file_identity_sha256,
        "report_content_sha256": report_sha256,
        "artifacts": deepcopy(artifacts),
        "report_file_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "report_file_size_bytes": len(report_bytes),
    }
    manifest_bytes = _atomic_json_file_bytes(manifest)
    return manifest, hashlib.sha256(manifest_bytes).hexdigest()


def build_detector_probe_inherited_evidence_authority(
    report: Any,
) -> dict[str, str]:
    """Recompute the two immutable evidence digests inherited by a child."""

    if not isinstance(report, dict) or not isinstance(report.get("frames"), list):
        raise BallFrameEvidenceError("detector probe inherited evidence report is invalid")
    source_payload: list[dict[str, Any]] = []
    candidate_payload: list[dict[str, Any]] = []
    for frame in report["frames"]:
        if not isinstance(frame, dict):
            raise BallFrameEvidenceError("detector probe inherited frame is invalid")
        source_payload.append(
            {
                "frame_index": frame.get("frame_index"),
                "source_artifact_url": frame.get("source_artifact_url"),
                "source_frame_sha256": frame.get("source_frame_sha256"),
                "source_frame_size_bytes": frame.get("source_frame_size_bytes"),
            }
        )
        candidate_payload.append(
            {
                "frame_index": frame.get("frame_index"),
                "profile_results": deepcopy(frame.get("profile_results")),
            }
        )
    return {
        "source_frame_evidence_sha256": canonical_sha256(source_payload),
        "candidate_evidence_sha256": canonical_sha256(candidate_payload),
    }


def normalize_detector_probe_candidates(
    *,
    frame_index: int,
    probe_job_id: str,
    profile_id: str,
    raw_candidates: Any,
    width: int,
    height: int,
) -> list[dict[str, Any]]:
    """Normalize report candidates exactly as the online annotation UI does."""

    frame_index = _bounded_frame_index(frame_index, 2**63 - 1, "candidate")
    probe_job_id = _safe_id(probe_job_id, "candidate probe job_id")
    profile_id = _safe_id(profile_id, "candidate profile_id")
    width = _positive_int(width, "candidate source width")
    height = _positive_int(height, "candidate source height")
    if not isinstance(raw_candidates, list) or len(raw_candidates) > 5:
        raise BallFrameEvidenceError("locked profile candidates exceed the fixed budget")
    result: list[dict[str, Any]] = []
    for rank, raw in enumerate(raw_candidates, start=1):
        box = raw.get("bbox_source_px") if isinstance(raw, dict) else None
        confidence = raw.get("confidence") if isinstance(raw, dict) else None
        if (
            not isinstance(box, list)
            or len(box) != 4
            or any(
                isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item))
                for item in box
            )
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0 <= float(confidence) <= 1
        ):
            raise BallFrameEvidenceError("locked profile candidate is invalid")
        left, top, right, bottom = (float(item) for item in box)
        if not (0 <= left < right <= width and 0 <= top < bottom <= height):
            raise BallFrameEvidenceError("locked profile candidate is outside source pixels")
        candidate = {
            "candidate_id": (
                "suggestion-"
                + canonical_sha256(
                    {
                        "frame_index": frame_index,
                        "profile_id": profile_id,
                        "rank": rank,
                        "box": box,
                        "confidence": confidence,
                    }
                )[:24]
            ),
            "profile_id": profile_id,
            "rank": rank,
            "bbox_source_px": {
                "left": left,
                "top": top,
                "right": right,
                "bottom": bottom,
            },
            "confidence": float(confidence),
            "annotation_state": "suggested",
            "training_use": "excluded",
            "truth_status": "unconfirmed_suggestion",
        }
        candidate_sha256 = canonical_sha256(candidate)
        candidate["suggestion_job_id"] = probe_job_id
        candidate["suggestion_sha256"] = candidate_sha256
        result.append(candidate)
    return result


def validate_detector_probe_candidate_accounting(
    profile_result: Any,
) -> list[dict[str, Any]]:
    """Validate the detector report's fixed top-five accounting contract."""

    if (
        not isinstance(profile_result, dict)
        or profile_result.get("status") != "completed"
        or profile_result.get("top_k") != 5
    ):
        raise BallFrameEvidenceError("locked profile frame evidence is incomplete")
    raw_candidates = profile_result.get("raw_candidates")
    candidate_count = profile_result.get("candidate_count")
    filter_reasons = profile_result.get("filter_reasons")
    if (
        not isinstance(raw_candidates, list)
        or len(raw_candidates) > 5
        or isinstance(candidate_count, bool)
        or not isinstance(candidate_count, int)
        or candidate_count < 0
        or not isinstance(filter_reasons, dict)
        or any(
            not isinstance(key, str) or isinstance(count, bool) or not isinstance(count, int) or count <= 0
            for key, count in filter_reasons.items()
        )
    ):
        raise BallFrameEvidenceError("locked candidate accounting is invalid")
    deduplicated_count = candidate_count - filter_reasons.get("duplicate_suppressed_iou", 0)
    if (
        deduplicated_count < 0
        or len(raw_candidates) != min(deduplicated_count, 5)
        or filter_reasons.get("top_k_limit", 0) != max(0, deduplicated_count - 5)
        or profile_result.get("display_candidate") != (raw_candidates[0] if raw_candidates else None)
    ):
        raise BallFrameEvidenceError("locked candidate accounting is inconsistent")
    return deepcopy(raw_candidates)


def build_detector_probe_job_authority(job: Any) -> dict[str, Any]:
    """Freeze the full immutable detector-job authority needed offline."""

    if not isinstance(job, dict):
        raise BallFrameEvidenceError("detector probe job authority is missing")
    if (
        job.get("schema_version") != "1.0"
        or job.get("artifact_type") != "detector_probe_job"
        or job.get("status") != "ready"
        or not isinstance(job.get("frozen_request"), dict)
        or not isinstance(job.get("frozen_profiles"), list)
        or not isinstance(job.get("report"), dict)
    ):
        raise BallFrameEvidenceError("detector probe job authority is invalid")
    job_id = _safe_id(job.get("job_id"), "detector probe authority job_id")
    canonical_job_record_sha256 = canonical_sha256(job)
    frozen = deepcopy(job["frozen_request"])
    frozen_profiles = deepcopy(job["frozen_profiles"])
    report = deepcopy(job["report"])
    anchor = AUDITED_T2_LEGACY_PROBE_BINDINGS.get(job_id)
    request_sha256 = canonical_sha256(frozen)
    intent_sha256 = canonical_sha256({key: item for key, item in frozen.items() if key != "retry_from_job_id"})
    derived_semantic_intent_sha256: str | None = None
    if anchor is None:
        try:
            from football_tracking.detector_probe import (
                semantic_probe_intent_sha256,
            )

            derived_semantic_intent_sha256 = semantic_probe_intent_sha256(frozen)
        except (DetectorDevelopmentError, KeyError, TypeError) as exc:
            raise BallFrameEvidenceError("detector probe semantic intent authority is invalid") from exc
    if any(field not in frozen for field in _PROBE_RESOURCE_FIELDS):
        raise BallFrameEvidenceError("detector probe resource authority is incomplete")
    resource_sha256 = canonical_sha256({field: frozen[field] for field in _PROBE_RESOURCE_FIELDS})
    frozen_profiles_sha256 = canonical_sha256(frozen_profiles)
    execution_bundle_sha256 = _sha256(
        frozen.get("execution_bundle_sha256"),
        "detector probe execution bundle sha256",
    )
    runtime_environment_sha256 = _sha256(
        frozen.get("runtime_environment_sha256"),
        "detector probe runtime environment sha256",
    )
    result_manifest, result_manifest_sha256 = build_detector_probe_result_manifest_authority(report)
    report_sha256 = _sha256(report.get("report_sha256"), "detector probe report sha256")
    lineage = report.get("lineage")
    decode = report.get("decode")
    legacy_shape = bool(
        "review_proxy_manifest" not in report and isinstance(decode, dict) and "frame_timing_observations" not in decode
    )
    if legacy_shape and anchor is None:
        raise BallFrameEvidenceError("unknown legacy detector probe lacks an audited trust anchor")
    recorded_semantic_intent_sha256 = job.get("semantic_intent_sha256")
    semantic_intent_sha256 = recorded_semantic_intent_sha256 if anchor is not None else derived_semantic_intent_sha256
    if (
        not isinstance(lineage, dict)
        or job.get("request_sha256") != request_sha256
        or job.get("intent_sha256") != intent_sha256
        or recorded_semantic_intent_sha256 != semantic_intent_sha256
        or (job.get("resource_sha256") is not None and job.get("resource_sha256") != resource_sha256)
        or (
            job.get("frozen_profiles_sha256") is not None
            and job.get("frozen_profiles_sha256") != frozen_profiles_sha256
        )
        or job.get("result_manifest_sha256") != result_manifest_sha256
        or job.get("retry_from_job_id") != frozen.get("retry_from_job_id")
        or job.get("retry_kind") != frozen.get("retry_kind")
        or report.get("job_id") != job_id
        or report.get("request_sha256") != request_sha256
        or report.get("frozen_profiles") != frozen_profiles
        or lineage.get("intent_sha256") != intent_sha256
        or lineage.get("semantic_intent_sha256") != semantic_intent_sha256
        or lineage.get("frozen_profiles_sha256") != frozen_profiles_sha256
        or lineage.get("execution_bundle_sha256") != execution_bundle_sha256
        or lineage.get("runtime_environment_sha256") != runtime_environment_sha256
    ):
        raise BallFrameEvidenceError("detector probe job/report/frozen authority is inconsistent")
    audit_anchor_kind = "audited_t2_legacy" if anchor is not None else "embedded_job_record"
    if anchor is not None and any(
        actual != anchor[field]
        for field, actual in (
            ("canonical_job_record_sha256", canonical_job_record_sha256),
            ("request_sha256", request_sha256),
            ("report_sha256", report_sha256),
            ("result_manifest_sha256", result_manifest_sha256),
            ("execution_bundle_sha256", execution_bundle_sha256),
            ("runtime_environment_sha256", runtime_environment_sha256),
        )
    ):
        raise BallFrameEvidenceError("audited legacy detector probe changed from its trust anchor")
    body = {
        "schema_version": "1.0",
        "artifact_type": "detector_probe_job_authority",
        "job_id": job_id,
        "request_sha256": request_sha256,
        "intent_sha256": intent_sha256,
        "semantic_intent_sha256": semantic_intent_sha256,
        "resource_sha256": resource_sha256,
        "frozen_profiles_sha256": frozen_profiles_sha256,
        "execution_bundle_sha256": execution_bundle_sha256,
        "runtime_environment_sha256": runtime_environment_sha256,
        "retry_from_job_id": frozen.get("retry_from_job_id"),
        "retry_kind": frozen.get("retry_kind"),
        "frozen_request": frozen,
        "frozen_profiles": frozen_profiles,
        "probe_report_sha256": report_sha256,
        "probe_result_manifest_sha256": result_manifest_sha256,
        "probe_report": report,
        "probe_result_manifest": result_manifest,
        "probe_job_record": deepcopy(job),
        "canonical_job_record_sha256": canonical_job_record_sha256,
        "audit_anchor_kind": audit_anchor_kind,
    }
    return {
        **body,
        "job_record_authority_sha256": canonical_sha256(body),
    }


def validate_detector_probe_job_authority(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _PROBE_AUTHORITY_FIELDS:
        raise BallFrameEvidenceError("detector probe authority fields are invalid")
    probe_job_record = value.get("probe_job_record")
    if not isinstance(probe_job_record, dict) or value.get("canonical_job_record_sha256") != canonical_sha256(
        probe_job_record
    ):
        raise BallFrameEvidenceError("detector probe canonical job record authority is invalid")
    rebuilt = build_detector_probe_job_authority(probe_job_record)
    if rebuilt != value:
        raise BallFrameEvidenceError("detector probe authority changed from its immutable job record")
    return rebuilt


def verify_detector_probe_review_proxy_inheritance(
    child_report: Any,
    parent_report: Any,
    *,
    parent_probe_result_manifest_sha256: str,
) -> dict[str, str]:
    """Verify child semantic evidence against its embedded audited parent."""

    if not isinstance(child_report, dict) or not isinstance(parent_report, dict):
        raise BallFrameEvidenceError("review proxy child or parent report authority is invalid")
    parent_job_id = _safe_id(parent_report.get("job_id"), "historical parent probe job_id")
    child_job_id = _safe_id(child_report.get("job_id"), "review proxy child probe job_id")
    parent_report_sha256 = _sha256(
        parent_report.get("report_sha256"),
        "historical parent report sha256",
    )
    inherited_digests = build_detector_probe_inherited_evidence_authority(parent_report)
    lineage = child_report.get("lineage")
    upgrade = lineage.get("review_proxy_upgrade") if isinstance(lineage, dict) else None
    inherited = upgrade.get("inherited_evidence") if isinstance(upgrade, dict) else None
    if (
        not isinstance(inherited, dict)
        or inherited.get("parent_probe_job_id") != parent_job_id
        or inherited.get("parent_probe_report_sha256") != parent_report_sha256
        or inherited.get("parent_probe_result_manifest_sha256") != parent_probe_result_manifest_sha256
        or inherited.get("source_frame_evidence_sha256") != inherited_digests["source_frame_evidence_sha256"]
        or inherited.get("candidate_evidence_sha256") != inherited_digests["candidate_evidence_sha256"]
    ):
        raise BallFrameEvidenceError("review proxy child inherited digests changed from parent")
    child_frames = child_report.get("frames")
    parent_frames = parent_report.get("frames")
    if (
        not isinstance(child_frames, list)
        or not isinstance(parent_frames, list)
        or [frame.get("frame_index") for frame in child_frames] != [frame.get("frame_index") for frame in parent_frames]
    ):
        raise BallFrameEvidenceError("review proxy child frame set changed from parent")
    child_prefix = f"/api/v1/detector-probes/{child_job_id}/artifacts/"
    parent_prefix = f"/api/v1/detector-probes/{parent_job_id}/artifacts/"

    def relocated_artifact_id(value: Any, prefix: str) -> str:
        if not isinstance(value, str) or not value.startswith(prefix):
            raise BallFrameEvidenceError("review proxy inherited artifact URL is invalid")
        return _safe_id(value[len(prefix) :], "inherited artifact_id")

    for child, parent in zip(child_frames, parent_frames, strict=True):
        if not isinstance(child, dict) or not isinstance(parent, dict):
            raise BallFrameEvidenceError("review proxy inherited frame is invalid")
        if any(
            child.get(field) != parent.get(field)
            for field in (
                "frame_index",
                "source_width",
                "source_height",
                "source_frame_sha256",
                "source_frame_size_bytes",
                "media_integrity",
            )
        ) or relocated_artifact_id(child.get("source_artifact_url"), child_prefix) != relocated_artifact_id(
            parent.get("source_artifact_url"), parent_prefix
        ):
            raise BallFrameEvidenceError("review proxy child source evidence changed from parent")
        child_profiles = child.get("profile_results")
        parent_profiles = parent.get("profile_results")
        if (
            not isinstance(child_profiles, list)
            or not isinstance(parent_profiles, list)
            or len(child_profiles) != len(parent_profiles)
        ):
            raise BallFrameEvidenceError("review proxy child candidate evidence changed from parent")
        for child_profile, parent_profile in zip(child_profiles, parent_profiles, strict=True):
            if not isinstance(child_profile, dict) or not isinstance(parent_profile, dict):
                raise BallFrameEvidenceError("review proxy child candidate row is invalid")
            child_semantic = deepcopy(child_profile)
            parent_semantic = deepcopy(parent_profile)
            child_url = child_semantic.pop("raw_overlay_artifact_url", None)
            parent_url = parent_semantic.pop("raw_overlay_artifact_url", None)
            if child_url is None and parent_url is None:
                overlay_identity_matches = True
            elif child_url is not None and parent_url is not None:
                overlay_identity_matches = relocated_artifact_id(child_url, child_prefix) == relocated_artifact_id(
                    parent_url, parent_prefix
                )
            else:
                overlay_identity_matches = False
            if child_semantic != parent_semantic or not overlay_identity_matches:
                raise BallFrameEvidenceError("review proxy child candidate evidence changed from parent")
    return inherited_digests


def _atomic_json_file_bytes(value: Any) -> bytes:
    try:
        content = (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
        return content.encode("utf-8")
    except (UnicodeEncodeError, TypeError, ValueError) as exc:
        raise BallFrameEvidenceError("detector probe authority is not canonical JSON") from exc


_PROXY_CORE_FIELDS = frozenset(
    {
        "proxy",
        "map_sha256",
        "source_frame",
        "proxy_frame",
        "map_time_tolerance_msec",
        "declared_offset_msec",
    }
)
_PROXY_FIELDS = frozenset(
    {
        *_PROXY_CORE_FIELDS,
        "schema_version",
        "artifact_type",
        "time_mapping",
        "binding_sha256",
    }
)
_PROBE_INPUT_FIELDS = frozenset(
    {
        "probe_job_id",
        "probe_report_sha256",
        "probe_result_manifest_sha256",
        "artifact_id",
    }
)
_PROBE_FIELDS = frozenset(
    {
        *_PROBE_INPUT_FIELDS,
        "schema_version",
        "artifact_type",
        "artifact_sha256",
        "artifact_size_bytes",
        "artifact_media_type",
        "binding_sha256",
    }
)
_PROPAGATION_INPUT_FIELDS = frozenset(
    {
        "propagation_job_id",
        "neighbor_probe_job_id",
        "neighbor_probe_report_sha256",
        "neighbor_probe_result_manifest_sha256",
        "neighbor_artifact_id",
        "propagation_intent_sha256",
        "seed_binding_sha256",
        "tracker_profile_sha256",
        "propagation_report_sha256",
        "propagation_frame_result_sha256",
        "suggestion_id",
        "suggestion_sha256",
    }
)
_PROPAGATION_FIELDS = frozenset(
    {
        *_PROPAGATION_INPUT_FIELDS,
        "schema_version",
        "artifact_type",
        "neighbor_artifact_sha256",
        "neighbor_artifact_size_bytes",
        "temporal_group_derivative_binding_sha256",
        "binding_sha256",
    }
)
_ROW_FIELDS = frozenset(
    {
        "schema_version",
        "artifact_type",
        "frame_index",
        "frame_role",
        "source",
        "source_frame_jpeg",
        "temporal_group",
        "probe_evidence",
        "timing_binding",
        "proxy_binding",
        "propagation_evidence",
        "effective_revision",
        "effective_annotation_sha256",
        "revision_chain_sha256",
        "frame_evidence_sha256",
    }
)
_PROPAGATION_REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "artifact_type",
        "job_id",
        "session_id",
        "intent_sha256",
        "mutation_id",
        "seed_frame_index",
        "expected_seed_revision",
        "radius_frames",
        "seed_binding",
        "seed_binding_sha256",
        "target_frame_indices",
        "tracker_profile",
        "tracker_profile_sha256",
        "neighbor_probe_job_id",
        "neighbor_probe_report_sha256",
        "neighbor_probe_result_manifest_sha256",
        "frame_results",
        "suggestions",
        "summary",
        "decision_counts",
        "created_at",
        "updated_at",
        "report_sha256",
    }
)


class BallFrameEvidenceError(ValueError):
    """Exact source-frame evidence is incomplete, inconsistent, or resealed."""


def build_source_frame_timing_binding(
    *,
    source_sha256: str,
    runtime_environment_sha256: str,
    source_frame_jpeg_sha256: str,
    frame_index: int,
    decoded_frame_position: int,
    fps: float,
    effective_decode_mode: str,
    decoder_reported_pos_msec: float | None,
    decoder_timing_observation_method: str | None,
    position_verification: str,
    true_presentation_timestamp: dict[str, Any],
    cross_decode_verification: dict[str, Any] | None = None,
    timing_status: str = "observed",
) -> dict[str, Any]:
    """Seal raw decoder POS_MSEC captured after an exact verified frame read."""

    source_digest = _sha256(source_sha256, "timing source sha256")
    runtime_digest = _sha256(runtime_environment_sha256, "timing runtime environment sha256")
    jpeg_digest = _sha256(source_frame_jpeg_sha256, "timing source frame JPEG sha256")
    normalized_index = _nonnegative_int(frame_index, "timing frame_index")
    normalized_position = _nonnegative_int(decoded_frame_position, "decoded frame position")
    normalized_fps = _finite_positive(fps, "timing fps")
    decode_mode = _decode_mode(effective_decode_mode)
    if timing_status == "observed":
        raw_pos_msec = _decoder_pos_msec(decoder_reported_pos_msec, "decoder reported POS_MSEC")
        if decoder_timing_observation_method != DECODER_TIMING_OBSERVATION_METHOD:
            raise BallFrameEvidenceError("decoder timing observation method is unsupported")
        timing_profile_id = TIMING_PROFILE_ID
    elif timing_status == "not_collected":
        if (
            decoder_reported_pos_msec is not None
            or decoder_timing_observation_method is not None
            or cross_decode_verification is not None
        ):
            raise BallFrameEvidenceError("uncollected source timing cannot claim decoder observations")
        raw_pos_msec = None
        timing_profile_id = TIMING_NOT_COLLECTED_PROFILE_ID
    else:
        raise BallFrameEvidenceError("source timing status is unsupported")
    if normalized_position != normalized_index:
        raise BallFrameEvidenceError("decoded frame position must equal the verified source frame index")
    expected_position_verification = (
        POSITION_VERIFICATION if timing_status == "observed" else PROXY_INDEX_POSITION_VERIFICATION
    )
    if position_verification != expected_position_verification:
        raise BallFrameEvidenceError("source frame position verification is unsupported")
    true_pts = _true_presentation_timestamp(true_presentation_timestamp)
    cross_decode = (
        _build_cross_decode_verification(
            cross_decode_verification,
            frame_index=normalized_index,
            source_frame_jpeg_sha256=jpeg_digest,
            decoder_reported_pos_msec=raw_pos_msec,
            effective_decode_mode=decode_mode,
        )
        if timing_status == "observed"
        else None
    )
    binding: dict[str, Any] = {
        "schema_version": "1.0",
        "artifact_type": "ball_source_frame_timing_binding",
        "timing_profile_id": timing_profile_id,
        "timing_status": timing_status,
        "source_sha256": source_digest,
        "runtime_environment_sha256": runtime_digest,
        "source_frame_jpeg_sha256": jpeg_digest,
        "frame_index": normalized_index,
        "decoded_frame_position": normalized_position,
        "fps": normalized_fps,
        "effective_decode_mode": decode_mode,
        "decoder_reported_pos_msec": raw_pos_msec,
        "decoder_time_seconds": (None if raw_pos_msec is None else raw_pos_msec / 1000.0),
        "decoder_timing_observation_method": (
            DECODER_TIMING_OBSERVATION_METHOD if timing_status == "observed" else None
        ),
        "display_time_seconds": normalized_index / normalized_fps,
        "display_time_derivation": DISPLAY_TIME_DERIVATION,
        "true_presentation_timestamp": true_pts,
        "position_verification": expected_position_verification,
        "cross_decode_verification": cross_decode,
    }
    binding["timing_binding_sha256"] = canonical_sha256(binding)
    return binding


def validate_source_frame_timing_binding(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _TIMING_FIELDS:
        raise BallFrameEvidenceError("source frame timing binding fields are invalid")
    expected = build_source_frame_timing_binding(
        source_sha256=value.get("source_sha256"),
        runtime_environment_sha256=value.get("runtime_environment_sha256"),
        source_frame_jpeg_sha256=value.get("source_frame_jpeg_sha256"),
        frame_index=value.get("frame_index"),
        decoded_frame_position=value.get("decoded_frame_position"),
        fps=value.get("fps"),
        effective_decode_mode=value.get("effective_decode_mode"),
        decoder_reported_pos_msec=value.get("decoder_reported_pos_msec"),
        decoder_timing_observation_method=value.get("decoder_timing_observation_method"),
        position_verification=value.get("position_verification"),
        true_presentation_timestamp=value.get("true_presentation_timestamp"),
        cross_decode_verification=_cross_decode_core(value.get("cross_decode_verification")),
        timing_status=value.get("timing_status"),
    )
    if value.get("schema_version") != "1.0" or value.get("artifact_type") != "ball_source_frame_timing_binding":
        raise BallFrameEvidenceError("source frame timing binding type is invalid")
    if (
        value.get("timing_profile_id") not in {TIMING_PROFILE_ID, TIMING_NOT_COLLECTED_PROFILE_ID}
        or value.get("display_time_derivation") != DISPLAY_TIME_DERIVATION
    ):
        raise BallFrameEvidenceError("source frame timing semantics changed")
    if value != expected:
        raise BallFrameEvidenceError("source frame timing binding is not canonical")
    return deepcopy(expected)


def build_nullable_proxy_binding(value: Any) -> dict[str, Any] | None:
    """Build one sealed proxy-frame map binding, or preserve explicit ``None``."""

    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _PROXY_CORE_FIELDS:
        raise BallFrameEvidenceError("proxy binding core fields are invalid")
    proxy = _proxy_media(value.get("proxy"))
    source_frame = _mapped_frame(value.get("source_frame"), "source", allow_not_collected=True)
    proxy_frame = _mapped_proxy_cfr_frame(value.get("proxy_frame"))
    if source_frame["frame_index"] != proxy_frame["frame_index"]:
        raise BallFrameEvidenceError("proxy mapping must preserve the exact verified frame index")
    time_tolerance_msec = _bounded_tolerance_msec(value.get("map_time_tolerance_msec"), "proxy map time tolerance")
    declared_offset_msec = _decoder_pos_msec(value.get("declared_offset_msec"), "proxy map declared offset")
    if source_frame["timing_status"] == "not_collected":
        observed_offset_msec = None
        residual_msec = None
        mapping_method = "exact_frame_index_to_verified_proxy_cfr_v1"
    else:
        observed_offset_msec = proxy_frame["cfr_time_msec"] - source_frame["decoder_reported_pos_msec"]
        residual_msec = observed_offset_msec - declared_offset_msec
        if abs(residual_msec) > time_tolerance_msec:
            raise BallFrameEvidenceError("proxy frame timing exceeds the declared map offset tolerance")
        mapping_method = "explicit_per_frame_decoder_pos_msec_map_v1"
    binding: dict[str, Any] = {
        "schema_version": "1.0",
        "artifact_type": "ball_review_proxy_frame_binding",
        "proxy": proxy,
        "map_sha256": _sha256(value.get("map_sha256"), "proxy map sha256"),
        "source_frame": source_frame,
        "proxy_frame": proxy_frame,
        "map_time_tolerance_msec": time_tolerance_msec,
        "declared_offset_msec": declared_offset_msec,
        "time_mapping": {
            "method": mapping_method,
            "source_timing_status": source_frame["timing_status"],
            "proxy_timing_basis": proxy_frame["timing_basis"],
            "declared_offset_msec": declared_offset_msec,
            "observed_offset_msec": observed_offset_msec,
            "residual_msec": residual_msec,
            "tolerance_msec": time_tolerance_msec,
        },
    }
    binding["binding_sha256"] = canonical_sha256(binding)
    return binding


def validate_nullable_proxy_binding(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _PROXY_FIELDS:
        raise BallFrameEvidenceError("sealed proxy binding fields are invalid")
    if value.get("schema_version") != "1.0" or value.get("artifact_type") != "ball_review_proxy_frame_binding":
        raise BallFrameEvidenceError("sealed proxy binding type is invalid")
    expected = build_nullable_proxy_binding({key: deepcopy(value[key]) for key in _PROXY_CORE_FIELDS})
    if value != expected:
        raise BallFrameEvidenceError("sealed proxy binding is not canonical")
    return deepcopy(expected)


def build_frame_evidence_row(
    *,
    frame_role: str,
    source: dict[str, Any],
    frame_index: int,
    source_frame_jpeg_sha256: str,
    source_frame_jpeg_size_bytes: int,
    temporal_group: dict[str, Any],
    probe_evidence: dict[str, Any],
    timing_binding: dict[str, Any],
    proxy_binding: dict[str, Any] | None,
    effective_annotation: dict[str, Any],
    revision_chain: list[dict[str, Any]],
    propagation_evidence: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build one immutable source-JPEG and annotation-lineage evidence row."""

    role = _frame_role(frame_role)
    source_binding = _source_binding(source)
    index = _nonnegative_int(frame_index, "frame evidence frame_index")
    jpeg_sha256 = _sha256(source_frame_jpeg_sha256, "source frame JPEG sha256")
    jpeg_size = _positive_int(source_frame_jpeg_size_bytes, "source frame JPEG size")
    annotation = _associated_annotation(effective_annotation, index)
    revisions = _associated_revisions(revision_chain, index)
    timing = validate_source_frame_timing_binding(timing_binding)
    _require_timing_matches(timing, source_binding, index, jpeg_sha256)
    proxy = validate_nullable_proxy_binding(proxy_binding)
    _require_proxy_matches(proxy, timing, jpeg_sha256, source_binding)
    group = _temporal_group(
        temporal_group,
        role=role,
        source_sha256=source_binding["sha256"],
        frame_index=index,
    )
    probe = _build_probe_evidence(
        probe_evidence,
        artifact_sha256=jpeg_sha256,
        artifact_size_bytes=jpeg_size,
    )
    propagation = _build_propagation_evidence(
        propagation_evidence,
        role=role,
        probe=probe,
        temporal_group=group,
        artifact_sha256=jpeg_sha256,
        artifact_size_bytes=jpeg_size,
    )
    row: dict[str, Any] = {
        "schema_version": "1.0",
        "artifact_type": "ball_sealed_frame_evidence",
        "frame_index": index,
        "frame_role": role,
        "source": source_binding,
        "source_frame_jpeg": {
            "sha256": jpeg_sha256,
            "size_bytes": jpeg_size,
            "media_type": "image/jpeg",
        },
        "temporal_group": group,
        "probe_evidence": probe,
        "timing_binding": timing,
        "proxy_binding": proxy,
        "propagation_evidence": propagation,
        "effective_revision": revisions[-1]["revision"],
        "effective_annotation_sha256": canonical_sha256(annotation),
        "revision_chain_sha256": canonical_sha256(revisions),
    }
    row["frame_evidence_sha256"] = canonical_sha256(row)
    return row


def validate_frame_evidence_row(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _ROW_FIELDS:
        raise BallFrameEvidenceError("sealed frame evidence fields are invalid")
    if value.get("schema_version") != "1.0" or value.get("artifact_type") != "ball_sealed_frame_evidence":
        raise BallFrameEvidenceError("sealed frame evidence type is invalid")
    role = _frame_role(value.get("frame_role"))
    source = _source_binding(value.get("source"))
    frame_index = _nonnegative_int(value.get("frame_index"), "frame evidence frame_index")
    source_frame_jpeg = value.get("source_frame_jpeg")
    if not isinstance(source_frame_jpeg, dict) or set(source_frame_jpeg) != {
        "sha256",
        "size_bytes",
        "media_type",
    }:
        raise BallFrameEvidenceError("source frame JPEG binding fields are invalid")
    jpeg_sha256 = _sha256(source_frame_jpeg.get("sha256"), "source frame JPEG sha256")
    jpeg_size = _positive_int(source_frame_jpeg.get("size_bytes"), "source frame JPEG size")
    if source_frame_jpeg.get("media_type") != "image/jpeg":
        raise BallFrameEvidenceError("source frame evidence must be an exact JPEG")
    group = _temporal_group(
        value.get("temporal_group"),
        role=role,
        source_sha256=source["sha256"],
        frame_index=frame_index,
    )
    timing = validate_source_frame_timing_binding(value.get("timing_binding"))
    _require_timing_matches(timing, source, frame_index, jpeg_sha256)
    proxy = validate_nullable_proxy_binding(value.get("proxy_binding"))
    _require_proxy_matches(proxy, timing, jpeg_sha256, source)
    probe = _validate_probe_evidence(
        value.get("probe_evidence"),
        artifact_sha256=jpeg_sha256,
        artifact_size_bytes=jpeg_size,
    )
    propagation = _validate_propagation_evidence(
        value.get("propagation_evidence"),
        role=role,
        probe=probe,
        temporal_group=group,
        artifact_sha256=jpeg_sha256,
        artifact_size_bytes=jpeg_size,
    )
    effective_digest = _sha256(
        value.get("effective_annotation_sha256"),
        "effective annotation sha256",
    )
    effective_revision = _positive_int(value.get("effective_revision"), "effective annotation revision")
    revision_digest = _sha256(value.get("revision_chain_sha256"), "revision chain sha256")
    normalized: dict[str, Any] = {
        "schema_version": "1.0",
        "artifact_type": "ball_sealed_frame_evidence",
        "frame_index": frame_index,
        "frame_role": role,
        "source": source,
        "source_frame_jpeg": {
            "sha256": jpeg_sha256,
            "size_bytes": jpeg_size,
            "media_type": "image/jpeg",
        },
        "temporal_group": group,
        "probe_evidence": probe,
        "timing_binding": timing,
        "proxy_binding": proxy,
        "propagation_evidence": propagation,
        "effective_revision": effective_revision,
        "effective_annotation_sha256": effective_digest,
        "revision_chain_sha256": revision_digest,
    }
    normalized["frame_evidence_sha256"] = canonical_sha256(normalized)
    if value != normalized:
        raise BallFrameEvidenceError("sealed frame evidence is not canonical")
    return deepcopy(normalized)


def verify_frame_evidence_package(package: Any) -> list[dict[str, Any]]:
    """Verify package structure; seeding also requires the server's finalized-session pointer."""

    if not isinstance(package, dict):
        raise BallFrameEvidenceError("annotation package must be an object")
    package_sha256 = _sha256(package.get("package_sha256"), "annotation package sha256")
    if package_sha256 != canonical_sha256({key: value for key, value in package.items() if key != "package_sha256"}):
        raise BallFrameEvidenceError("annotation package digest is invalid")
    data_role = package.get("data_role")
    if data_role not in {"development", "check"}:
        raise BallFrameEvidenceError("annotation package data_role is invalid")
    source = _package_source(package.get("source"))
    primary_indices, sampling_groups = _sampling_manifest(package.get("sampling_manifest"), source, data_role=data_role)
    supplemental_indices = _frame_index_list(
        package.get("supplemental_frame_indices"),
        "supplemental frame indices",
        allow_empty=True,
        frame_count=source["frame_count"],
    )
    if set(primary_indices) & set(supplemental_indices):
        raise BallFrameEvidenceError("primary sampling manifest and supplemental frame list overlap")
    if data_role == "check" and supplemental_indices:
        raise BallFrameEvidenceError("sealed check packages cannot contain supplemental frames")

    annotations = package.get("effective_annotations")
    if not isinstance(annotations, list) or not annotations:
        raise BallFrameEvidenceError("effective annotations are missing")
    annotation_by_frame: dict[int, dict[str, Any]] = {}
    annotation_order: list[int] = []
    for annotation in annotations:
        if not isinstance(annotation, dict):
            raise BallFrameEvidenceError("effective annotation must be an object")
        frame_index = _bounded_frame_index(annotation.get("frame_index"), source["frame_count"], "annotation")
        if frame_index in annotation_by_frame:
            raise BallFrameEvidenceError("effective annotation frames are duplicated")
        annotation_by_frame[frame_index] = annotation
        annotation_order.append(frame_index)
    if annotation_order != sorted(annotation_order):
        raise BallFrameEvidenceError("effective annotation frames must be ordered")

    revisions = package.get("revision_chain")
    if not isinstance(revisions, list):
        raise BallFrameEvidenceError("revision chain must be a list")
    revisions_by_frame: dict[int, list[dict[str, Any]]] = {}
    for revision in revisions:
        if not isinstance(revision, dict):
            raise BallFrameEvidenceError("annotation revision must be an object")
        frame_index = _bounded_frame_index(revision.get("frame_index"), source["frame_count"], "revision")
        revisions_by_frame.setdefault(frame_index, []).append(revision)
    if set(revisions_by_frame) != set(annotation_by_frame):
        raise BallFrameEvidenceError(
            "every effective annotation requires a nonempty revision chain and no extra revision frame"
        )

    raw_rows = package.get("frame_evidence")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise BallFrameEvidenceError("sealed frame evidence rows are missing")
    if _sha256(package.get("frame_evidence_sha256"), "frame evidence collection sha256") != canonical_sha256(raw_rows):
        raise BallFrameEvidenceError("frame evidence collection digest is invalid")
    rows = [validate_frame_evidence_row(row) for row in raw_rows]
    row_indices = [row["frame_index"] for row in rows]
    if row_indices != sorted(set(row_indices)):
        raise BallFrameEvidenceError("sealed frame evidence rows must be unique and ordered")
    if set(row_indices) != set(annotation_by_frame):
        raise BallFrameEvidenceError("every effective annotation and revision frame requires exactly one evidence row")
    rows_by_index = {row["frame_index"]: row for row in rows}
    detector_probe_authorities = _sealed_detector_probe_authorities(
        package.get("detector_probe_authorities"),
        data_role=data_role,
        lineage=package.get("lineage"),
        check_probe_job_id=package.get("check_probe_job_id"),
        check_probe_authority=package.get("check_probe_authority"),
    )
    _sealed_session_request_authority(
        package.get("session_request_authority"),
        session_id=package.get("session_id"),
        data_role=data_role,
        lineage=package.get("lineage"),
        locked_profile=package.get("locked_profile"),
        operator_id=package.get("operator_id"),
        sampling_profile_id=package.get("sampling_profile_id"),
        metric_profile_id=package.get("metric_profile_id"),
        sampling_manifest=package.get("sampling_manifest"),
        development_package_binding=package.get("development_package_binding"),
    )
    _verify_sealed_profile_selection(
        locked_profile=package.get("locked_profile"),
        control_profile_id=package.get("control_profile_id"),
        control_profile=package.get("control_profile"),
        probe_authorities=detector_probe_authorities,
    )
    pending_detector_evidence_count = _sealed_detector_candidate_evidence(
        package.get("detector_candidate_evidence"),
        collection_sha256=package.get("detector_candidate_evidence_sha256"),
        rows_by_index=rows_by_index,
        locked_profile=package.get("locked_profile"),
        source=source,
        revisions=revisions,
        data_role=data_role,
        lineage=package.get("lineage"),
        proxy_authority=package.get("frame_review_proxy_authority"),
        probe_authorities=detector_probe_authorities,
        check_probe_authority=package.get("check_probe_authority"),
    )
    propagation_reports = _sealed_propagation_reports(
        package.get("propagation_reports"),
        collection_sha256=package.get("propagation_reports_sha256"),
        session_id=package.get("session_id"),
        data_role=data_role,
    )
    producing_job_ids = {
        row["propagation_evidence"]["propagation_job_id"] for row in rows if row.get("propagation_evidence") is not None
    }
    if not producing_job_ids.issubset(propagation_reports):
        raise BallFrameEvidenceError("sealed propagation reports do not match supplemental producer jobs")
    pending_propagation_report_count = sum(
        report["decision_counts"]["pending"] for report in propagation_reports.values()
    )
    for row in rows:
        evidence = row.get("propagation_evidence")
        if evidence is None:
            continue
        report = propagation_reports[evidence["propagation_job_id"]]
        if (
            evidence.get("propagation_report_sha256") != report["report_sha256"]
            or evidence.get("propagation_intent_sha256") != report["intent_sha256"]
            or evidence.get("seed_binding_sha256") != report["seed_binding_sha256"]
            or evidence.get("tracker_profile_sha256") != report["tracker_profile_sha256"]
            or evidence.get("neighbor_probe_job_id") != report["neighbor_probe_job_id"]
            or evidence.get("neighbor_probe_report_sha256") != report["neighbor_probe_report_sha256"]
            or evidence.get("neighbor_probe_result_manifest_sha256") != report["neighbor_probe_result_manifest_sha256"]
        ):
            raise BallFrameEvidenceError("supplemental frame changed its sealed propagation report")
    frame_media = package.get("frame_media")
    if (
        not isinstance(frame_media, list)
        or package.get("frame_media_sha256") != canonical_sha256(frame_media)
        or [entry.get("frame_index") for entry in frame_media if isinstance(entry, dict)] != row_indices
    ):
        raise BallFrameEvidenceError("immutable frame media manifest is missing, unordered, or changed")
    rows_by_index = {row["frame_index"]: row for row in rows}
    for entry in frame_media:
        if not isinstance(entry, dict) or set(entry) != {
            "frame_index",
            "relative_path",
            "sha256",
            "size_bytes",
            "media_type",
            "width",
            "height",
        }:
            raise BallFrameEvidenceError("immutable frame media fields are invalid")
        frame_index = entry.get("frame_index")
        if isinstance(frame_index, bool) or not isinstance(frame_index, int):
            raise BallFrameEvidenceError("immutable frame media index is invalid")
        row = rows_by_index.get(frame_index)
        if (
            row is None
            or entry.get("relative_path") != f"frames/{frame_index:09d}.jpg"
            or entry.get("sha256") != row["source_frame_jpeg"]["sha256"]
            or entry.get("size_bytes") != row["source_frame_jpeg"]["size_bytes"]
            or entry.get("media_type") != "image/jpeg"
            or entry.get("width") != source["width"]
            or entry.get("height") != source["height"]
        ):
            raise BallFrameEvidenceError("immutable frame media does not match sealed frame evidence")
    attempt_family_sha256 = _sha256(package.get("attempt_family_sha256"), "attempt family sha256")
    development_binding = package.get("development_package_binding")
    if data_role == "development":
        if development_binding is not None:
            raise BallFrameEvidenceError("development package cannot bind another development package")
        if attempt_family_sha256 != canonical_sha256(_attempt_family_authority(package)):
            raise BallFrameEvidenceError("development attempt-family authority is invalid")
    else:
        if (
            not isinstance(development_binding, dict)
            or set(development_binding) != {"session_id", "package_sha256", "attempt_family_sha256"}
            or development_binding.get("attempt_family_sha256") != attempt_family_sha256
        ):
            raise BallFrameEvidenceError("check package development attempt-family binding is invalid")
        _safe_id(
            development_binding.get("session_id"),
            "development package session_id",
        )
        _sha256(
            development_binding.get("package_sha256"),
            "development package sha256",
        )
    eligibility = package.get("dataset_expansion_eligibility")
    if not isinstance(eligibility, dict) or set(eligibility) != {
        "eligible",
        "reasons",
        "validation_evidence",
    }:
        raise BallFrameEvidenceError("dataset expansion eligibility contract is invalid")
    validation = eligibility.get("validation_evidence")
    if not isinstance(validation, dict) or set(validation) != {
        "all_frames_human_confirmed",
        "all_primary_roles_complete",
        "all_supplemental_roles_complete",
        "exact_frame_media_sha256",
        "frame_evidence_sha256",
        "revision_chain_sha256",
        "pending_propagation_suggestion_count",
        "pending_detector_candidate_count",
        "pending_suggestion_decision_count",
        "localizable_positive_seed_count",
    }:
        raise BallFrameEvidenceError("dataset expansion validation evidence is invalid")
    localizable_count = sum(
        annotation.get("presence") == "present"
        and (annotation.get("point_source_px") is not None or annotation.get("bbox_source_px") is not None)
        for annotation in annotations
    )
    pending_propagation_count = validation.get("pending_propagation_suggestion_count")
    pending_detector_count = validation.get("pending_detector_candidate_count")
    pending_count = validation.get("pending_suggestion_decision_count")
    reasons = eligibility.get("reasons")
    expected_eligible = data_role == "development" and localizable_count > 0 and pending_count == 0
    if (
        validation.get("all_frames_human_confirmed") is not True
        or validation.get("all_primary_roles_complete") is not True
        or validation.get("all_supplemental_roles_complete") is not True
        or validation.get("exact_frame_media_sha256") != package.get("frame_media_sha256")
        or validation.get("frame_evidence_sha256") != package.get("frame_evidence_sha256")
        or validation.get("revision_chain_sha256") != canonical_sha256(revisions)
        or isinstance(pending_count, bool)
        or not isinstance(pending_count, int)
        or pending_count < 0
        or isinstance(pending_propagation_count, bool)
        or not isinstance(pending_propagation_count, int)
        or pending_propagation_count < 0
        or isinstance(pending_detector_count, bool)
        or not isinstance(pending_detector_count, int)
        or pending_detector_count < 0
        or pending_count != pending_propagation_count + pending_detector_count
        or pending_detector_count != 0
        or pending_propagation_count != 0
        or pending_count != 0
        or pending_detector_count != pending_detector_evidence_count
        or pending_propagation_count != pending_propagation_report_count
        or validation.get("localizable_positive_seed_count") != localizable_count
        or not isinstance(reasons, list)
        or len(reasons) != len(set(reasons))
        or eligibility.get("eligible") is not expected_eligible
        or package.get("may_seed_dataset_expansion") is not expected_eligible
    ):
        raise BallFrameEvidenceError("dataset expansion eligibility evidence is inconsistent")
    expected_reasons = []
    if data_role == "check":
        expected_reasons.append("check_role_is_evaluation_only")
    if pending_count:
        expected_reasons.append("pending_suggestion_decisions")
    if data_role == "development" and localizable_count == 0:
        expected_reasons.append("no_localizable_positive_seed")
    if reasons != expected_reasons:
        raise BallFrameEvidenceError("dataset expansion eligibility reasons are not canonical")
    primary_row_indices = [row["frame_index"] for row in rows if row["frame_role"] == "primary"]
    supplemental_row_indices = [row["frame_index"] for row in rows if row["frame_role"] == "supplemental"]
    if primary_row_indices != primary_indices:
        raise BallFrameEvidenceError("primary frame evidence does not match the frozen sampling manifest")
    if supplemental_row_indices != supplemental_indices:
        raise BallFrameEvidenceError("supplemental frame evidence does not match its separate frame list")
    lineage = package.get("lineage")
    if not isinstance(lineage, dict):
        raise BallFrameEvidenceError("package runtime lineage is missing")
    if data_role == "check":
        check_probe_authority = package.get("check_probe_authority")
        if not isinstance(check_probe_authority, dict):
            raise BallFrameEvidenceError("check probe authority is missing")
        runtime_environment_sha256 = _sha256(
            check_probe_authority.get("runtime_environment_sha256"),
            "check probe runtime environment sha256",
        )
    else:
        runtime_environment_sha256 = _sha256(
            lineage.get("runtime_environment_sha256"),
            "package runtime environment sha256",
        )
    proxy_authority = package.get("frame_review_proxy_authority")
    proxy_rows = [row for row in rows if row.get("proxy_binding") is not None]
    if proxy_authority is None:
        if proxy_rows:
            raise BallFrameEvidenceError("frame proxy evidence lacks frozen child manifest authority")
    else:
        proxy_authority_fields = {
            "probe_job_id",
            "probe_report_sha256",
            "probe_result_manifest_sha256",
            "probe_report",
            "probe_result_manifest",
            "review_proxy_manifest",
        }
        if (
            not isinstance(proxy_authority, dict)
            or not proxy_authority_fields.issubset(proxy_authority)
            or set(proxy_authority) - {"historical_probe_authority"} != proxy_authority_fields
        ):
            raise BallFrameEvidenceError("frame proxy manifest authority fields are invalid")
        probe_job_id = require_safe_id(proxy_authority.get("probe_job_id"), "proxy authority probe job_id")
        probe_report_sha256 = _sha256(
            proxy_authority.get("probe_report_sha256"),
            "proxy authority probe report sha256",
        )
        probe_result_manifest_sha256 = _sha256(
            proxy_authority.get("probe_result_manifest_sha256"),
            "proxy authority probe result manifest sha256",
        )
        probe_report = proxy_authority.get("probe_report")
        probe_result_manifest = proxy_authority.get("probe_result_manifest")
        historical = proxy_authority.get("historical_probe_authority")
        rebuilt_result_manifest, rebuilt_result_manifest_sha256 = build_detector_probe_result_manifest_authority(
            probe_report
        )
        if (
            probe_report.get("job_id") != probe_job_id
            or probe_report.get("report_sha256") != probe_report_sha256
            or probe_result_manifest != rebuilt_result_manifest
            or rebuilt_result_manifest_sha256 != probe_result_manifest_sha256
        ):
            raise BallFrameEvidenceError("frame proxy authority changed from its child report/result manifest")
        upgrade = probe_report.get("lineage", {}).get("review_proxy_upgrade")
        historical_job_id = None
        historical_report_sha256 = None
        historical_result_sha256 = None
        if isinstance(upgrade, dict):
            if not isinstance(historical, dict) or set(historical) != {
                "probe_job_id",
                "probe_report_sha256",
                "probe_result_manifest_sha256",
                "probe_report",
                "probe_result_manifest",
                "source_frame_evidence_sha256",
                "candidate_evidence_sha256",
            }:
                raise BallFrameEvidenceError("frame proxy historical authority fields are invalid")
            historical_job_id = _safe_id(
                historical.get("probe_job_id"),
                "historical probe job_id",
            )
            historical_report_sha256 = _sha256(
                historical.get("probe_report_sha256"),
                "historical probe report sha256",
            )
            historical_result_sha256 = _sha256(
                historical.get("probe_result_manifest_sha256"),
                "historical probe result manifest sha256",
            )
            historical_report = historical.get("probe_report")
            historical_result = historical.get("probe_result_manifest")
            rebuilt_historical_result, rebuilt_historical_result_sha256 = (
                build_detector_probe_result_manifest_authority(historical_report)
            )
            historical_digests = build_detector_probe_inherited_evidence_authority(historical_report)
            if (
                historical_report.get("job_id") != historical_job_id
                or historical_report.get("report_sha256") != historical_report_sha256
                or historical_result != rebuilt_historical_result
                or rebuilt_historical_result_sha256 != historical_result_sha256
                or historical.get("source_frame_evidence_sha256") != historical_digests["source_frame_evidence_sha256"]
                or historical.get("candidate_evidence_sha256") != historical_digests["candidate_evidence_sha256"]
            ):
                raise BallFrameEvidenceError("frame proxy historical report/result authority is invalid")
            verify_detector_probe_review_proxy_inheritance(
                probe_report,
                historical_report,
                parent_probe_result_manifest_sha256=(historical_result_sha256),
            )
        elif historical is not None:
            raise BallFrameEvidenceError("ordinary frame proxy cannot publish historical child authority")
        try:
            manifest = validate_review_proxy_manifest(proxy_authority.get("review_proxy_manifest"))
        except ReviewProxyError as exc:
            raise BallFrameEvidenceError("frame proxy manifest authority is not canonical") from exc
        if probe_report.get("review_proxy_manifest") != manifest:
            raise BallFrameEvidenceError("frame proxy manifest changed from its child probe report")
        mapping_sha256 = manifest["mapping_sha256"]
        expected_frame_indices = manifest["expected_frame_indices"]
        mappings = manifest["mappings"]
        proxy_media = manifest["proxy"]
        lineage_job_ids = lineage.get("development_probe_job_ids")
        report_sha256s = lineage.get("development_probe_report_sha256s")
        result_manifest_sha256s = lineage.get("development_probe_result_manifest_sha256s")
        if data_role == "development":
            proxy_authority_invalid = (
                not isinstance(lineage_job_ids, list)
                or probe_job_id not in lineage_job_ids
                or not isinstance(report_sha256s, dict)
                or report_sha256s.get(probe_job_id) != probe_report_sha256
                or not isinstance(result_manifest_sha256s, dict)
                or result_manifest_sha256s.get(probe_job_id) != probe_result_manifest_sha256
                or (
                    historical_job_id is not None
                    and (
                        historical_job_id not in lineage_job_ids
                        or report_sha256s.get(historical_job_id) != historical_report_sha256
                        or result_manifest_sha256s.get(historical_job_id) != historical_result_sha256
                    )
                )
            )
        else:
            child_authority = detector_probe_authorities.get(probe_job_id)
            historical_authority = (
                detector_probe_authorities.get(historical_job_id) if historical_job_id is not None else None
            )
            proxy_authority_invalid = (
                child_authority is None
                or child_authority.get("probe_report_sha256") != probe_report_sha256
                or child_authority.get("probe_result_manifest_sha256") != probe_result_manifest_sha256
                or (
                    historical_job_id is not None
                    and (
                        historical_authority is None
                        or historical_authority.get("probe_report_sha256") != historical_report_sha256
                        or historical_authority.get("probe_result_manifest_sha256") != historical_result_sha256
                    )
                )
            )
        if (
            proxy_authority_invalid
            or manifest["source"]["sha256"] != source["sha256"]
            or manifest["source"]["width"] != source["width"]
            or manifest["source"]["height"] != source["height"]
            or manifest["source"]["frame_count"] != source["frame_count"]
            or not math.isclose(
                manifest["source"]["fps"],
                source["fps"],
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            raise BallFrameEvidenceError("frame proxy manifest authority changed from development lineage")
        rows_by_index = {row["frame_index"]: row for row in proxy_rows}
        if sorted(rows_by_index) != expected_frame_indices:
            raise BallFrameEvidenceError("frame proxy rows differ from the frozen child mapping")
        for mapping in mappings:
            frame_index = mapping["source_frame_index"]
            row = rows_by_index[frame_index]
            proxy = row["proxy_binding"]
            if (
                row["probe_evidence"]["probe_job_id"] != probe_job_id
                or row["probe_evidence"]["probe_report_sha256"] != probe_report_sha256
                or row["probe_evidence"]["probe_result_manifest_sha256"] != probe_result_manifest_sha256
                or proxy["map_sha256"] != mapping_sha256
                or proxy["proxy"]
                != {
                    "sha256": proxy_media.get("sha256"),
                    "size_bytes": proxy_media.get("size_bytes"),
                    "width": proxy_media.get("width"),
                    "height": proxy_media.get("height"),
                }
                or proxy["source_frame"]
                != {
                    "frame_index": mapping.get("source_frame_index"),
                    "timing_status": mapping.get("source_timing_status"),
                    "decoder_reported_pos_msec": mapping.get("source_decoder_pos_msec"),
                    "sha256": mapping.get("source_frame_sha256"),
                }
                or proxy["proxy_frame"]
                != {
                    "frame_index": mapping.get("proxy_frame_index"),
                    "timing_basis": mapping.get("proxy_timing_basis"),
                    "cfr_time_msec": mapping.get("proxy_cfr_time_msec"),
                    "sha256": mapping.get("proxy_frame_sha256"),
                }
            ):
                raise BallFrameEvidenceError("frame proxy row changed from the frozen child manifest")

    for row in rows:
        frame_index = row["frame_index"]
        if (
            row["source"]["sha256"] != source["sha256"]
            or row["source"]["width"] != source["width"]
            or row["source"]["height"] != source["height"]
            or row["timing_binding"]["runtime_environment_sha256"] != runtime_environment_sha256
            or not math.isclose(
                row["timing_binding"]["fps"],
                source["fps"],
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            raise BallFrameEvidenceError("frame evidence changed the package source/decode binding")
        if row["effective_annotation_sha256"] != canonical_sha256(annotation_by_frame[frame_index]):
            raise BallFrameEvidenceError("frame evidence effective annotation digest is invalid")
        if row["revision_chain_sha256"] != canonical_sha256(revisions_by_frame.get(frame_index, [])):
            raise BallFrameEvidenceError("frame evidence revision digest is invalid")
        _verify_revision_chain_truth(
            package=package,
            source=source,
            annotation=annotation_by_frame[frame_index],
            revisions=revisions_by_frame[frame_index],
            effective_revision=row["effective_revision"],
            row=row,
        )
        if row["frame_role"] == "primary":
            expected_group = sampling_groups[frame_index]
            if row["temporal_group"] != expected_group:
                raise BallFrameEvidenceError("primary frame temporal group changed from the sampling manifest")
            _verify_primary_probe_lineage(package, row, data_role=data_role)
        else:
            source_group = {key: row["temporal_group"][key] for key in _TEMPORAL_GROUP_FIELDS}
            manifest_group = next(
                (group for group in sampling_groups.values() if group["group_id"] == source_group["group_id"]),
                None,
            )
            if source_group != manifest_group:
                raise BallFrameEvidenceError("supplemental temporal ancestry is absent from the primary manifest")
    if len(rows) >= 2:
        observed_times = [row["timing_binding"]["decoder_reported_pos_msec"] for row in rows]
        source_timing_valid = all(value is not None for value in observed_times)
        if source_timing_valid:
            source_timing_valid = not any(
                current <= previous for previous, current in zip(observed_times, observed_times[1:])
            )
        proxy_times = [
            row["proxy_binding"]["proxy_frame"]["cfr_time_msec"] for row in rows if row["proxy_binding"] is not None
        ]
        proxy_timing_valid = len(proxy_times) == len(rows) and not any(
            current <= previous for previous, current in zip(proxy_times, proxy_times[1:])
        )
        if not source_timing_valid and not proxy_timing_valid:
            raise BallFrameEvidenceError(
                "source or verified proxy POS_MSEC must strictly increase across distinct sealed frames"
            )
    proxy_rows_by_map: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        proxy = row["proxy_binding"]
        if proxy is not None:
            proxy_rows_by_map.setdefault(proxy["map_sha256"], []).append(proxy)
        for proxy_rows in proxy_rows_by_map.values():
            if len(proxy_rows) < 2:
                continue
            observed_times = [proxy["proxy_frame"]["cfr_time_msec"] for proxy in proxy_rows]
            if any(current <= previous for previous, current in zip(observed_times, observed_times[1:])):
                raise BallFrameEvidenceError("review proxy map time must strictly increase across distinct frames")
    return deepcopy(rows)


def _verify_primary_probe_lineage(package: dict[str, Any], row: dict[str, Any], *, data_role: str) -> None:
    probe = row["probe_evidence"]
    if data_role == "development":
        lineage = package.get("lineage")
        if not isinstance(lineage, dict):
            raise BallFrameEvidenceError("development probe lineage is missing")
        report_digests = lineage.get("development_probe_report_sha256s")
        result_digests = lineage.get("development_probe_result_manifest_sha256s")
        if not isinstance(report_digests, dict) or not isinstance(result_digests, dict):
            raise BallFrameEvidenceError("development probe digest lineage is missing")
        job_id = probe["probe_job_id"]
        if (
            report_digests.get(job_id) != probe["probe_report_sha256"]
            or result_digests.get(job_id) != probe["probe_result_manifest_sha256"]
        ):
            raise BallFrameEvidenceError("primary frame probe evidence changed from development lineage")
        return
    authority = package.get("check_probe_authority")
    if not isinstance(authority, dict):
        raise BallFrameEvidenceError("check probe authority is missing")
    if (
        authority.get("job_id") != probe["probe_job_id"]
        or authority.get("report_sha256") != probe["probe_report_sha256"]
        or authority.get("result_manifest_sha256") != probe["probe_result_manifest_sha256"]
    ):
        raise BallFrameEvidenceError("primary frame probe evidence changed from check authority")


def _sampling_manifest(
    value: Any, source: dict[str, Any], *, data_role: str
) -> tuple[list[int], dict[int, dict[str, Any]]]:
    if not isinstance(value, dict):
        raise BallFrameEvidenceError("sampling manifest is missing")
    manifest_sha256 = _sha256(value.get("manifest_sha256"), "sampling manifest sha256")
    if manifest_sha256 != canonical_sha256({key: item for key, item in value.items() if key != "manifest_sha256"}):
        raise BallFrameEvidenceError("sampling manifest digest is invalid")
    if value.get("source_sha256") != source["sha256"]:
        raise BallFrameEvidenceError("sampling manifest source changed")
    frame_indices = _frame_index_list(
        value.get("frame_indices"),
        "primary sampling frame indices",
        allow_empty=False,
        frame_count=source["frame_count"],
    )
    groups = value.get("groups")
    if not isinstance(groups, list) or len(groups) != len(frame_indices):
        raise BallFrameEvidenceError("sampling temporal groups are incomplete")
    groups_by_frame: dict[int, dict[str, Any]] = {}
    for raw_group in groups:
        expected_fields = {
            *_TEMPORAL_GROUP_FIELDS,
            "frame_index",
        }
        if data_role == "check":
            expected_fields.add("pre_reveal_lighting_stratum")
        if not isinstance(raw_group, dict) or set(raw_group) != expected_fields:
            raise BallFrameEvidenceError("sampling temporal group fields are invalid")
        if data_role == "check" and raw_group.get("pre_reveal_lighting_stratum") not in _LIGHTING_STRATA:
            raise BallFrameEvidenceError("check sampling temporal group lighting stratum is invalid")
        frame_index = _bounded_frame_index(raw_group.get("frame_index"), source["frame_count"], "sampling group")
        if frame_index in groups_by_frame:
            raise BallFrameEvidenceError("sampling temporal groups are duplicated")
        group = {key: deepcopy(raw_group[key]) for key in _TEMPORAL_GROUP_FIELDS}
        expected = _temporal_group(
            group,
            role="primary",
            source_sha256=source["sha256"],
            frame_index=frame_index,
        )
        groups_by_frame[frame_index] = expected
    if list(groups_by_frame) != frame_indices:
        raise BallFrameEvidenceError("sampling temporal groups do not match frozen frame order")
    return frame_indices, groups_by_frame


def _package_source(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BallFrameEvidenceError("package source binding is missing")
    return {
        "sha256": _sha256(value.get("sha256"), "package source sha256"),
        "width": _positive_int(value.get("width"), "package source width"),
        "height": _positive_int(value.get("height"), "package source height"),
        "frame_count": _positive_int(value.get("frame_count"), "package source frame count"),
        "fps": _finite_positive(value.get("fps"), "package source fps"),
    }


def _attempt_family_authority(package: dict[str, Any]) -> dict[str, Any]:
    lineage = package.get("lineage")
    sampling_manifest = package.get("sampling_manifest")
    if not isinstance(lineage, dict) or not isinstance(sampling_manifest, dict):
        raise BallFrameEvidenceError("attempt-family lineage is missing")
    required_lineage = (
        "parent_trial_id",
        "development_probe_job_ids",
        "development_probe_report_sha256s",
        "development_probe_result_manifest_sha256s",
        "development_probe_execution_bundle_sha256s",
        "development_probe_frozen_profiles_sha256s",
        "decode",
        "runtime_environment_sha256",
    )
    if any(key not in lineage for key in required_lineage):
        raise BallFrameEvidenceError("attempt-family probe lineage is incomplete")
    return {
        "schema_version": "1.0",
        "artifact_type": "ball_annotation_attempt_family_authority",
        "source": deepcopy(package.get("source")),
        "locked_profile": deepcopy(package.get("locked_profile")),
        "control_profile_id": package.get("control_profile_id"),
        "control_profile": deepcopy(package.get("control_profile")),
        "parent_trial_id": lineage["parent_trial_id"],
        "development_probe_job_ids": deepcopy(lineage["development_probe_job_ids"]),
        "development_probe_report_sha256s": deepcopy(lineage["development_probe_report_sha256s"]),
        "development_probe_result_manifest_sha256s": deepcopy(lineage["development_probe_result_manifest_sha256s"]),
        "development_probe_execution_bundle_sha256s": deepcopy(lineage["development_probe_execution_bundle_sha256s"]),
        "development_probe_frozen_profiles_sha256s": deepcopy(lineage["development_probe_frozen_profiles_sha256s"]),
        "decode": deepcopy(lineage["decode"]),
        "runtime_environment_sha256": lineage["runtime_environment_sha256"],
        "development_sampling_manifest_sha256": sampling_manifest.get("manifest_sha256"),
        "development_sampling_groups": deepcopy(sampling_manifest.get("groups")),
        "sampling_profile_id": package.get("sampling_profile_id"),
        "metric_profile_id": package.get("metric_profile_id"),
        "metric_profile_sha256": package.get("metric_profile_sha256"),
    }


def _source_binding(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"sha256", "width", "height"}:
        raise BallFrameEvidenceError("frame evidence source fields are invalid")
    return {
        "sha256": _sha256(value.get("sha256"), "frame evidence source sha256"),
        "width": _positive_int(value.get("width"), "frame evidence source width"),
        "height": _positive_int(value.get("height"), "frame evidence source height"),
    }


def _temporal_group(
    value: Any,
    *,
    role: str,
    source_sha256: str,
    frame_index: int,
) -> dict[str, Any]:
    expected_fields = (
        _TEMPORAL_GROUP_FIELDS
        if role == "primary"
        else frozenset({*_TEMPORAL_GROUP_FIELDS, "derivative", "derivative_binding_sha256"})
    )
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise BallFrameEvidenceError("complete temporal group fields are invalid")
    seed_frame_index = _nonnegative_int(value.get("seed_frame_index"), "temporal group seed frame")
    try:
        source_group = temporal_group_for_frame(source_sha256, seed_frame_index)
    except (DetectorDevelopmentError, FeasibilityError) as exc:
        raise BallFrameEvidenceError("temporal group source authority is invalid") from exc
    actual_source_group = {key: deepcopy(value[key]) for key in _TEMPORAL_GROUP_FIELDS}
    if actual_source_group != source_group:
        raise BallFrameEvidenceError("temporal group is not canonical")
    if role == "primary":
        if seed_frame_index != frame_index:
            raise BallFrameEvidenceError("primary frame must own its canonical temporal group")
        return source_group
    if not source_group["start_frame"] <= frame_index <= source_group["end_frame"]:
        raise BallFrameEvidenceError("supplemental frame is outside its inherited temporal group")
    derivative = value.get("derivative")
    if not isinstance(derivative, dict) or derivative.get("artifact_type") != "propagation":
        raise BallFrameEvidenceError("supplemental temporal group must be inherited by propagation")
    try:
        expected = inherit_temporal_group(
            source_group,
            artifact_type="propagation",
            artifact_id=derivative.get("artifact_id"),
        )
    except (DetectorDevelopmentError, FeasibilityError) as exc:
        raise BallFrameEvidenceError("supplemental temporal ancestry is invalid") from exc
    if value != expected:
        raise BallFrameEvidenceError("supplemental temporal group inheritance changed")
    return expected


def _build_probe_evidence(value: Any, *, artifact_sha256: str, artifact_size_bytes: int) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _PROBE_INPUT_FIELDS:
        raise BallFrameEvidenceError("probe evidence input fields are invalid")
    evidence: dict[str, Any] = {
        "schema_version": "1.0",
        "artifact_type": "ball_source_frame_probe_evidence",
        "probe_job_id": _safe_id(value.get("probe_job_id"), "probe job_id"),
        "probe_report_sha256": _sha256(value.get("probe_report_sha256"), "probe report sha256"),
        "probe_result_manifest_sha256": _sha256(
            value.get("probe_result_manifest_sha256"),
            "probe result manifest sha256",
        ),
        "artifact_id": _safe_id(value.get("artifact_id"), "probe artifact_id"),
        "artifact_sha256": artifact_sha256,
        "artifact_size_bytes": artifact_size_bytes,
        "artifact_media_type": "image/jpeg",
    }
    evidence["binding_sha256"] = canonical_sha256(evidence)
    return evidence


def _validate_probe_evidence(value: Any, *, artifact_sha256: str, artifact_size_bytes: int) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _PROBE_FIELDS:
        raise BallFrameEvidenceError("sealed probe evidence fields are invalid")
    if value.get("schema_version") != "1.0" or value.get("artifact_type") != "ball_source_frame_probe_evidence":
        raise BallFrameEvidenceError("sealed probe evidence type is invalid")
    expected = _build_probe_evidence(
        {key: value[key] for key in _PROBE_INPUT_FIELDS},
        artifact_sha256=artifact_sha256,
        artifact_size_bytes=artifact_size_bytes,
    )
    if value != expected:
        raise BallFrameEvidenceError("sealed probe evidence is not canonical")
    return expected


def _build_propagation_evidence(
    value: Any,
    *,
    role: str,
    probe: dict[str, Any],
    temporal_group: dict[str, Any],
    artifact_sha256: str,
    artifact_size_bytes: int,
) -> dict[str, Any] | None:
    if role == "primary":
        if value is not None:
            raise BallFrameEvidenceError("primary frame evidence cannot carry propagation lineage")
        return None
    if not isinstance(value, dict) or set(value) != _PROPAGATION_INPUT_FIELDS:
        raise BallFrameEvidenceError("supplemental propagation evidence fields are invalid")
    suggestion_id = value.get("suggestion_id")
    raw_suggestion_sha256 = value.get("suggestion_sha256")
    if (suggestion_id is None) != (raw_suggestion_sha256 is None):
        raise BallFrameEvidenceError("propagation suggestion id and digest must both be present or both be null")
    if suggestion_id is not None:
        suggestion_id = _safe_id(suggestion_id, "propagation suggestion_id")
        suggestion_sha256 = _sha256(raw_suggestion_sha256, "propagation suggestion sha256")
    else:
        suggestion_sha256 = None
    evidence: dict[str, Any] = {
        "schema_version": "1.0",
        "artifact_type": "ball_supplemental_propagation_evidence",
        "propagation_job_id": _safe_id(value.get("propagation_job_id"), "propagation job_id"),
        "neighbor_probe_job_id": _safe_id(value.get("neighbor_probe_job_id"), "neighbor probe job_id"),
        "neighbor_probe_report_sha256": _sha256(
            value.get("neighbor_probe_report_sha256"),
            "neighbor probe report sha256",
        ),
        "neighbor_probe_result_manifest_sha256": _sha256(
            value.get("neighbor_probe_result_manifest_sha256"),
            "neighbor probe result manifest sha256",
        ),
        "neighbor_artifact_id": _safe_id(value.get("neighbor_artifact_id"), "neighbor artifact_id"),
        "neighbor_artifact_sha256": artifact_sha256,
        "neighbor_artifact_size_bytes": artifact_size_bytes,
        "propagation_intent_sha256": _sha256(
            value.get("propagation_intent_sha256"),
            "propagation intent sha256",
        ),
        "seed_binding_sha256": _sha256(
            value.get("seed_binding_sha256"),
            "propagation seed binding sha256",
        ),
        "tracker_profile_sha256": _sha256(
            value.get("tracker_profile_sha256"),
            "propagation tracker profile sha256",
        ),
        "propagation_report_sha256": _sha256(
            value.get("propagation_report_sha256"),
            "propagation report sha256",
        ),
        "propagation_frame_result_sha256": _sha256(
            value.get("propagation_frame_result_sha256"),
            "propagation frame result sha256",
        ),
        "suggestion_id": suggestion_id,
        "suggestion_sha256": suggestion_sha256,
        "temporal_group_derivative_binding_sha256": _sha256(
            temporal_group.get("derivative_binding_sha256"),
            "temporal derivative binding sha256",
        ),
    }
    if (
        evidence["neighbor_probe_job_id"] != probe["probe_job_id"]
        or evidence["neighbor_probe_report_sha256"] != probe["probe_report_sha256"]
        or evidence["neighbor_probe_result_manifest_sha256"] != probe["probe_result_manifest_sha256"]
        or evidence["neighbor_artifact_id"] != probe["artifact_id"]
        or temporal_group.get("derivative", {}).get("artifact_id") != evidence["neighbor_artifact_id"]
    ):
        raise BallFrameEvidenceError("supplemental neighbor probe lineage does not match frame evidence")
    evidence["binding_sha256"] = canonical_sha256(evidence)
    return evidence


def _validate_propagation_evidence(
    value: Any,
    *,
    role: str,
    probe: dict[str, Any],
    temporal_group: dict[str, Any],
    artifact_sha256: str,
    artifact_size_bytes: int,
) -> dict[str, Any] | None:
    if role == "primary":
        if value is not None:
            raise BallFrameEvidenceError("primary frame evidence cannot carry propagation lineage")
        return None
    if not isinstance(value, dict) or set(value) != _PROPAGATION_FIELDS:
        raise BallFrameEvidenceError("sealed supplemental propagation evidence fields are invalid")
    if value.get("schema_version") != "1.0" or value.get("artifact_type") != "ball_supplemental_propagation_evidence":
        raise BallFrameEvidenceError("sealed supplemental propagation evidence type is invalid")
    expected = _build_propagation_evidence(
        {key: value[key] for key in _PROPAGATION_INPUT_FIELDS},
        role=role,
        probe=probe,
        temporal_group=temporal_group,
        artifact_sha256=artifact_sha256,
        artifact_size_bytes=artifact_size_bytes,
    )
    if value != expected:
        raise BallFrameEvidenceError("sealed supplemental propagation evidence is not canonical")
    return expected


def _build_cross_decode_verification(
    value: Any,
    *,
    frame_index: int,
    source_frame_jpeg_sha256: str,
    decoder_reported_pos_msec: float,
    effective_decode_mode: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "method",
        "tolerance_msec",
        "observations",
    }:
        raise BallFrameEvidenceError("cross-decode timing evidence fields are invalid")
    if value.get("method") != "decoder_pos_msec_and_frame_digest_agreement_v1":
        raise BallFrameEvidenceError("cross-decode timing method is invalid")
    tolerance_msec = _bounded_tolerance_msec(value.get("tolerance_msec"), "cross-decode timing tolerance")
    raw_observations = value.get("observations")
    if not isinstance(raw_observations, list) or not 2 <= len(raw_observations) <= 4:
        raise BallFrameEvidenceError("cross-decode timing requires two to four observations")
    observations: list[dict[str, Any]] = []
    modes: set[str] = set()
    for observation in raw_observations:
        if not isinstance(observation, dict) or set(observation) != {
            "effective_decode_mode",
            "decoded_frame_position",
            "decoder_reported_pos_msec",
            "source_frame_jpeg_sha256",
        }:
            raise BallFrameEvidenceError("cross-decode timing observation fields are invalid")
        mode = _decode_mode(observation.get("effective_decode_mode"))
        if mode in modes:
            raise BallFrameEvidenceError("cross-decode timing observations require distinct decode modes")
        modes.add(mode)
        position = _nonnegative_int(
            observation.get("decoded_frame_position"),
            "cross-decode frame position",
        )
        pos_msec = _decoder_pos_msec(
            observation.get("decoder_reported_pos_msec"),
            "cross-decode decoder POS_MSEC",
        )
        jpeg_sha256 = _sha256(
            observation.get("source_frame_jpeg_sha256"),
            "cross-decode source frame JPEG sha256",
        )
        if (
            position != frame_index
            or jpeg_sha256 != source_frame_jpeg_sha256
            or abs(pos_msec - decoder_reported_pos_msec) > tolerance_msec
        ):
            raise BallFrameEvidenceError("cross-decode observations disagree on position, time, or frame bytes")
        observations.append(
            {
                "effective_decode_mode": mode,
                "decoded_frame_position": position,
                "decoder_reported_pos_msec": pos_msec,
                "source_frame_jpeg_sha256": jpeg_sha256,
            }
        )
    if effective_decode_mode not in modes:
        raise BallFrameEvidenceError("cross-decode evidence omits the effective artifact decode path")
    observations.sort(key=lambda item: item["effective_decode_mode"])
    result: dict[str, Any] = {
        "method": "decoder_pos_msec_and_frame_digest_agreement_v1",
        "tolerance_msec": tolerance_msec,
        "observations": observations,
    }
    result["verification_sha256"] = canonical_sha256(result)
    return result


def _cross_decode_core(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "method",
        "tolerance_msec",
        "observations",
        "verification_sha256",
    }:
        raise BallFrameEvidenceError("sealed cross-decode timing fields are invalid")
    return {
        "method": value["method"],
        "tolerance_msec": value["tolerance_msec"],
        "observations": deepcopy(value["observations"]),
    }


def _require_timing_matches(
    timing: dict[str, Any],
    source: dict[str, Any],
    frame_index: int,
    source_frame_jpeg_sha256: str,
) -> None:
    if (
        timing["source_sha256"] != source["sha256"]
        or timing["source_frame_jpeg_sha256"] != source_frame_jpeg_sha256
        or timing["frame_index"] != frame_index
        or timing["decoded_frame_position"] != frame_index
    ):
        raise BallFrameEvidenceError("source frame timing does not match sealed frame evidence")


def _require_proxy_matches(
    proxy: dict[str, Any] | None,
    timing: dict[str, Any],
    source_frame_jpeg_sha256: str,
    source: dict[str, Any],
) -> None:
    if proxy is None:
        if timing["timing_status"] == "not_collected":
            raise BallFrameEvidenceError("uncollected source timing requires verified proxy CFR evidence")
        return
    source_frame = proxy["source_frame"]
    proxy_media = proxy["proxy"]
    scale_x = proxy_media["width"] / source["width"]
    scale_y = proxy_media["height"] / source["height"]
    source_time_matches = source_frame["timing_status"] == timing["timing_status"] and (
        source_frame["decoder_reported_pos_msec"] is None
        and timing["decoder_reported_pos_msec"] is None
        or source_frame["decoder_reported_pos_msec"] is not None
        and timing["decoder_reported_pos_msec"] is not None
        and math.isclose(
            source_frame["decoder_reported_pos_msec"],
            timing["decoder_reported_pos_msec"],
            rel_tol=0.0,
            abs_tol=proxy["map_time_tolerance_msec"],
        )
    )
    if (
        source_frame["frame_index"] != timing["frame_index"]
        or not source_time_matches
        or source_frame["sha256"] != source_frame_jpeg_sha256
        or not math.isclose(scale_x, scale_y, rel_tol=0.0, abs_tol=1e-12)
    ):
        raise BallFrameEvidenceError("proxy map source frame does not match sealed source JPEG/timing")


def _proxy_media(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "sha256",
        "size_bytes",
        "width",
        "height",
    }:
        raise BallFrameEvidenceError("proxy media binding fields are invalid")
    return {
        "sha256": _sha256(value.get("sha256"), "proxy media sha256"),
        "size_bytes": _positive_int(value.get("size_bytes"), "proxy media size"),
        "width": _positive_int(value.get("width"), "proxy width"),
        "height": _positive_int(value.get("height"), "proxy height"),
    }


def _mapped_frame(value: Any, label: str, *, allow_not_collected: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) not in (
        {
            "frame_index",
            "decoder_reported_pos_msec",
            "sha256",
        },
        {
            "frame_index",
            "timing_status",
            "decoder_reported_pos_msec",
            "sha256",
        },
    ):
        raise BallFrameEvidenceError(f"{label} mapped frame fields are invalid")
    raw_timing = value.get("decoder_reported_pos_msec")
    timing_status = value.get("timing_status", "observed")
    if allow_not_collected and timing_status == "not_collected" and raw_timing is None:
        decoder_pos_msec = None
    else:
        if timing_status != "observed":
            raise BallFrameEvidenceError(f"{label} mapped timing status is invalid")
        decoder_pos_msec = _decoder_pos_msec(raw_timing, f"{label} mapped decoder POS_MSEC")
    return {
        "frame_index": _nonnegative_int(value.get("frame_index"), f"{label} mapped frame_index"),
        "timing_status": timing_status,
        "decoder_reported_pos_msec": decoder_pos_msec,
        "sha256": _sha256(value.get("sha256"), f"{label} mapped frame sha256"),
    }


def _mapped_proxy_cfr_frame(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "frame_index",
        "timing_basis",
        "cfr_time_msec",
        "sha256",
    }:
        raise BallFrameEvidenceError("proxy mapped frame fields are invalid")
    if value.get("timing_basis") != "verified_cfr_frame_index_time_v1":
        raise BallFrameEvidenceError("proxy mapped timing basis is invalid")
    return {
        "frame_index": _nonnegative_int(value.get("frame_index"), "proxy mapped frame_index"),
        "timing_basis": "verified_cfr_frame_index_time_v1",
        "cfr_time_msec": _decoder_pos_msec(value.get("cfr_time_msec"), "proxy mapped verified CFR time"),
        "sha256": _sha256(value.get("sha256"), "proxy mapped frame sha256"),
    }


def _associated_annotation(value: Any, frame_index: int) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("frame_index") != frame_index:
        raise BallFrameEvidenceError("effective annotation is not bound to the evidence frame")
    return deepcopy(value)


def _associated_revisions(value: Any, frame_index: int) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise BallFrameEvidenceError("frame evidence requires its revision chain")
    revisions: list[dict[str, Any]] = []
    for expected_revision, revision in enumerate(value, start=1):
        if not isinstance(revision, dict) or revision.get("frame_index") != frame_index:
            raise BallFrameEvidenceError("annotation revision is not bound to the evidence frame")
        revision_number = _positive_int(revision.get("revision"), "annotation revision number")
        if (
            revision_number != expected_revision
            or "supersedes_revision" not in revision
            or revision.get("supersedes_revision") != (expected_revision - 1 or None)
        ):
            raise BallFrameEvidenceError("per-frame annotation revisions must be contiguous with exact supersession")
        revisions.append(deepcopy(revision))
    return revisions


def _verify_effective_revision(
    annotation: dict[str, Any],
    revisions: list[dict[str, Any]],
    effective_revision: int,
) -> None:
    frame_index = annotation["frame_index"]
    normalized = _associated_revisions(revisions, frame_index)
    last = normalized[-1]
    expected_effective = {key: deepcopy(value) for key, value in annotation.items() if key != "frame_index"}
    if effective_revision != last["revision"] or last.get("effective_annotation") != expected_effective:
        raise BallFrameEvidenceError("revision chain does not end in the sealed effective annotation/revision")


_SEALED_REVISION_FIELDS = frozenset(
    {
        "schema_version",
        "artifact_type",
        "revision_id",
        "session_id",
        "frame_index",
        "revision",
        "operation",
        "mutation_id",
        "mutation_sha256",
        "expected_revision",
        "supersedes_revision",
        "undo_revision",
        "accepted_suggestion_kind",
        "accepted_suggestion_id",
        "accepted_suggestion_job_id",
        "accepted_suggestion_sha256",
        "dismissed_suggestion_kind",
        "dismissed_suggestion_id",
        "dismissed_suggestion_job_id",
        "dismissed_suggestion_sha256",
        "previous_effective_annotation",
        "effective_annotation",
        "operator_id",
        "annotation_etag",
        "created_at",
    }
)


def _verify_revision_chain_truth(
    *,
    package: dict[str, Any],
    source: dict[str, Any],
    annotation: dict[str, Any],
    revisions: list[dict[str, Any]],
    effective_revision: int,
    row: dict[str, Any],
) -> None:
    frame_index = annotation["frame_index"]
    normalized = _associated_revisions(revisions, frame_index)
    session_id = _safe_id(package.get("session_id"), "annotation session_id")
    operator_id = _safe_id(package.get("operator_id"), "annotation operator_id")
    previous_effective: dict[str, Any] | None = None
    by_revision: dict[int, dict[str, Any]] = {}
    for expected_revision, revision in enumerate(normalized, start=1):
        if set(revision) != _SEALED_REVISION_FIELDS:
            raise BallFrameEvidenceError("sealed annotation revision fields are invalid")
        operation = revision.get("operation")
        expected_previous_revision = expected_revision - 1
        if (
            revision.get("schema_version") != "1.0"
            or revision.get("artifact_type") != "ball_annotation_revision"
            or revision.get("session_id") != session_id
            or revision.get("operator_id") != operator_id
            or revision.get("expected_revision") != expected_previous_revision
            or revision.get("supersedes_revision") != (expected_previous_revision or None)
            or revision.get("revision_id")
            != f"revision-{canonical_sha256({'session_id': session_id, 'frame_index': frame_index, 'revision': expected_revision})[:24]}"
            or revision.get("previous_effective_annotation") != previous_effective
            or operation not in {"set", "delete", "undo"}
            or not isinstance(revision.get("created_at"), str)
            or not revision["created_at"]
        ):
            raise BallFrameEvidenceError("sealed annotation revision authority or prior state is invalid")
        mutation_id = _safe_id(revision.get("mutation_id"), "annotation mutation_id")
        accepted_kind = revision.get("accepted_suggestion_kind")
        accepted = (
            revision.get("accepted_suggestion_id"),
            revision.get("accepted_suggestion_job_id"),
            revision.get("accepted_suggestion_sha256"),
        )
        dismissed_kind = revision.get("dismissed_suggestion_kind")
        dismissed = (
            revision.get("dismissed_suggestion_id"),
            revision.get("dismissed_suggestion_job_id"),
            revision.get("dismissed_suggestion_sha256"),
        )
        if any(value is not None for value in accepted) and not all(value is not None for value in accepted):
            raise BallFrameEvidenceError("accepted propagation suggestion lineage is partial")
        if accepted[0] is not None:
            if accepted_kind not in {"detector_candidate", "propagation"}:
                raise BallFrameEvidenceError("accepted suggestion kind is invalid")
            _safe_id(accepted[0], "accepted suggestion_id")
            _safe_id(accepted[1], "accepted suggestion job_id")
            _sha256(accepted[2], "accepted suggestion sha256")
        elif accepted_kind is not None:
            raise BallFrameEvidenceError("accepted suggestion kind has no suggestion reference")
        if any(value is not None for value in dismissed) and not all(value is not None for value in dismissed):
            raise BallFrameEvidenceError("dismissed propagation suggestion lineage is partial")
        if accepted[0] is not None and dismissed[0] is not None:
            raise BallFrameEvidenceError("one revision cannot accept and dismiss a suggestion")
        if dismissed[0] is not None:
            if dismissed_kind not in {"detector_candidate", "propagation"}:
                raise BallFrameEvidenceError("dismissed suggestion kind is invalid")
            _safe_id(dismissed[0], "dismissed suggestion_id")
            _safe_id(dismissed[1], "dismissed suggestion job_id")
            _sha256(dismissed[2], "dismissed suggestion sha256")
        elif dismissed_kind is not None:
            raise BallFrameEvidenceError("dismissed suggestion kind has no suggestion reference")
        undo_revision = revision.get("undo_revision")
        if operation == "set":
            if undo_revision is not None or not isinstance(revision.get("effective_annotation"), dict):
                raise BallFrameEvidenceError("sealed set revision is invalid")
            try:
                effective = validate_ball_annotation(
                    revision["effective_annotation"],
                    width=source["width"],
                    height=source["height"],
                    data_role=package["data_role"],
                )
            except BallAnnotationError as exc:
                raise BallFrameEvidenceError("sealed set annotation contract is invalid") from exc
            if effective != revision["effective_annotation"]:
                raise BallFrameEvidenceError("sealed set annotation is not canonical")
            expected_provenance = (
                "suggestion_dismissed_manual"
                if dismissed[0] is not None
                else "detector_candidate_human_confirmed"
                if accepted_kind == "detector_candidate"
                else "propagation_suggestion_human_confirmed"
                if accepted_kind == "propagation"
                else "manual_human_annotation"
            )
            if effective.get("provenance") != expected_provenance:
                raise BallFrameEvidenceError("sealed annotation provenance is not server-derived")
            request_annotation = effective
        elif operation == "delete":
            if (
                undo_revision is not None
                or revision.get("effective_annotation") is not None
                or accepted != (None, None, None)
                or dismissed != (None, None, None)
            ):
                raise BallFrameEvidenceError("sealed delete revision is invalid")
            effective = None
            request_annotation = None
        else:
            if (
                undo_revision != expected_previous_revision
                or undo_revision not in by_revision
                or accepted != (None, None, None)
                or dismissed != (None, None, None)
            ):
                raise BallFrameEvidenceError("sealed undo revision is invalid")
            effective = deepcopy(by_revision[undo_revision]["previous_effective_annotation"])
            if revision.get("effective_annotation") != effective:
                raise BallFrameEvidenceError("sealed undo transition does not restore prior truth")
            request_annotation = None
        request = {
            "mutation_id": mutation_id,
            "expected_revision": expected_previous_revision,
            "operation": operation,
            "undo_revision": undo_revision,
            "annotation": request_annotation,
            "suggestion_kind": accepted_kind,
            "suggestion_id": accepted[0],
            "accepted_suggestion_job_id": accepted[1],
            "accepted_suggestion_sha256": accepted[2],
            "dismissed_suggestion_kind": dismissed_kind,
            "dismissed_suggestion_id": dismissed[0],
            "dismissed_suggestion_job_id": dismissed[1],
            "dismissed_suggestion_sha256": dismissed[2],
        }
        if revision.get("mutation_sha256") != canonical_sha256(
            {
                "session_id": session_id,
                "frame_index": frame_index,
                "request": request,
            }
        ):
            raise BallFrameEvidenceError("sealed annotation mutation digest is invalid")
        expected_etag = annotation_etag(session_id, frame_index, expected_revision, effective)
        if revision.get("annotation_etag") != expected_etag:
            raise BallFrameEvidenceError("sealed annotation revision ETag is invalid")
        for decision_kind, decision, label in (
            (accepted_kind, accepted, "accepted"),
            (dismissed_kind, dismissed, "dismissed"),
        ):
            if decision[0] is None:
                continue
            if decision_kind == "detector_candidate":
                _safe_id(
                    decision[1],
                    f"{label} detector candidate probe job_id",
                )
                _sha256(
                    decision[2],
                    f"{label} detector candidate sha256",
                )
            elif (
                row.get("propagation_evidence") is None
                or row["propagation_evidence"].get("suggestion_id") != decision[0]
                or row["propagation_evidence"].get("propagation_job_id") != decision[1]
                or row["propagation_evidence"].get("suggestion_sha256") != decision[2]
            ):
                raise BallFrameEvidenceError(f"{label} suggestion changed from sealed propagation evidence")
        revision_id = _safe_id(revision.get("revision_id"), "revision_id")
        if not revision_id.startswith("revision-"):
            raise BallFrameEvidenceError("sealed revision identity is invalid")
        previous_effective = deepcopy(effective)
        by_revision[expected_revision] = revision
    expected_final = {key: deepcopy(value) for key, value in annotation.items() if key != "frame_index"}
    if effective_revision != len(normalized) or previous_effective != expected_final:
        raise BallFrameEvidenceError("revision chain does not end in the sealed effective annotation/revision")


def _sealed_detector_probe_authorities(
    value: Any,
    *,
    data_role: str,
    lineage: Any,
    check_probe_job_id: Any,
    check_probe_authority: Any,
) -> dict[str, dict[str, Any]]:
    if value is None:
        value = []
    if not isinstance(value, list):
        raise BallFrameEvidenceError("sealed detector probe authorities must be a list")
    if data_role == "development":
        expected_job_ids = lineage.get("development_probe_job_ids") if isinstance(lineage, dict) else None
        if not isinstance(expected_job_ids, list) or not expected_job_ids:
            raise BallFrameEvidenceError("development detector probe authority lineage is invalid")
        expected_job_ids = [_safe_id(job_id, "development detector probe job_id") for job_id in expected_job_ids]
    else:
        current_check_job_id = _safe_id(check_probe_job_id, "check detector probe job_id")
    authorities = [validate_detector_probe_job_authority(authority) for authority in value]
    actual_job_ids = [authority["job_id"] for authority in authorities]
    if len(set(actual_job_ids)) != len(actual_job_ids):
        raise BallFrameEvidenceError("sealed detector probe authorities differ from exact package lineage")
    if data_role == "development":
        if actual_job_ids != expected_job_ids:
            raise BallFrameEvidenceError("sealed detector probe authorities differ from exact package lineage")
    else:
        if not authorities or actual_job_ids[-1] != current_check_job_id:
            raise BallFrameEvidenceError("sealed detector probe authorities differ from exact package lineage")
        current_authority = authorities[-1]
        current_report = current_authority["probe_report"]
        current_lineage = current_report.get("lineage")
        proxy_upgrade = current_lineage.get("review_proxy_upgrade") if isinstance(current_lineage, dict) else None
        if proxy_upgrade is None:
            expected_job_ids = [current_check_job_id]
        else:
            parent_job_id = _safe_id(
                current_authority.get("retry_from_job_id"),
                "check review proxy parent probe job_id",
            )
            if current_authority.get("retry_kind") != "review_proxy_decode_upgrade":
                raise BallFrameEvidenceError("sealed check review proxy retry kind is invalid")
            expected_job_ids = [parent_job_id, current_check_job_id]
        if actual_job_ids != expected_job_ids:
            raise BallFrameEvidenceError("sealed detector probe authorities differ from exact package lineage")
        if proxy_upgrade is not None:
            parent_authority = authorities[0]
            verify_detector_probe_review_proxy_inheritance(
                current_report,
                parent_authority["probe_report"],
                parent_probe_result_manifest_sha256=parent_authority["probe_result_manifest_sha256"],
            )
    authorities_by_job = {authority["job_id"]: authority for authority in authorities}
    if data_role == "development":
        for map_field, authority_field in (
            (
                "development_probe_report_sha256s",
                "probe_report_sha256",
            ),
            (
                "development_probe_result_manifest_sha256s",
                "probe_result_manifest_sha256",
            ),
            (
                "development_probe_execution_bundle_sha256s",
                "execution_bundle_sha256",
            ),
            (
                "development_probe_frozen_profiles_sha256s",
                "frozen_profiles_sha256",
            ),
        ):
            values = lineage.get(map_field)
            if (
                not isinstance(values, dict)
                or set(values) != set(expected_job_ids)
                or any(values[job_id] != authorities_by_job[job_id][authority_field] for job_id in expected_job_ids)
            ):
                raise BallFrameEvidenceError("sealed detector probe authority differs from exact lineage digest maps")
    else:
        authority = authorities[-1]
        if (
            not isinstance(check_probe_authority, dict)
            or check_probe_authority.get("job_id") != authority["job_id"]
            or check_probe_authority.get("request_sha256") != authority["request_sha256"]
            or check_probe_authority.get("intent_sha256") != authority["intent_sha256"]
            or check_probe_authority.get("report_sha256") != authority["probe_report_sha256"]
            or check_probe_authority.get("result_manifest_sha256") != authority["probe_result_manifest_sha256"]
            or check_probe_authority.get("execution_bundle_sha256") != authority["execution_bundle_sha256"]
            or check_probe_authority.get("runtime_environment_sha256") != authority["runtime_environment_sha256"]
            or check_probe_authority.get("frozen_profiles_sha256") != authority["frozen_profiles_sha256"]
        ):
            raise BallFrameEvidenceError("sealed check probe authority differs from exact job authority")
    return authorities_by_job


def _sealed_session_request_authority(
    value: Any,
    *,
    session_id: Any,
    data_role: str,
    lineage: Any,
    locked_profile: Any,
    operator_id: Any,
    sampling_profile_id: Any,
    metric_profile_id: Any,
    sampling_manifest: Any,
    development_package_binding: Any,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _SESSION_REQUEST_AUTHORITY_FIELDS:
        raise BallFrameEvidenceError("sealed session request authority fields are invalid")
    authority_body = {key: item for key, item in value.items() if key != "authority_sha256"}
    expected_session_id = _safe_id(session_id, "annotation session_id")
    request_sha256 = _sha256(value.get("request_sha256"), "annotation session request sha256")
    normalized = value.get("normalized_request")
    if (
        value.get("schema_version") != "1.0"
        or value.get("artifact_type") != "ball_annotation_session_request_authority"
        or value.get("session_id") != expected_session_id
        or value.get("authority_sha256") != canonical_sha256(authority_body)
        or not isinstance(normalized, dict)
        or set(normalized) != _NORMALIZED_SESSION_REQUEST_FIELDS
        or canonical_sha256(normalized) != request_sha256
    ):
        raise BallFrameEvidenceError("sealed session request authority is invalid")
    expected_prefix = f"annotation-{request_sha256[:16]}-"
    session_suffix = expected_session_id.removeprefix(expected_prefix)
    if (
        not expected_session_id.startswith(expected_prefix)
        or len(session_suffix) != 12
        or any(character not in "0123456789abcdef" for character in session_suffix)
    ):
        raise BallFrameEvidenceError("annotation session identity differs from its request authority")
    jobs = normalized.get("development_probe_job_ids")
    normalized_role = normalized.get("data_role")
    if (
        normalized_role != data_role
        or not isinstance(jobs, list)
        or jobs != sorted(set(jobs))
        or not isinstance(lineage, dict)
        or set(jobs) != set(lineage.get("development_probe_job_ids", []))
        or normalized.get("locked_profile_id")
        != (locked_profile.get("profile_id") if isinstance(locked_profile, dict) else None)
        or normalized.get("operator_id") != operator_id
        or normalized.get("sampling_profile_id") != sampling_profile_id
        or normalized.get("metric_profile_id") != metric_profile_id
        or not isinstance(sampling_manifest, dict)
        or normalized.get("strata_applicability") != sampling_manifest.get("strata_applicability")
    ):
        raise BallFrameEvidenceError("sealed session request selection differs from the package")
    for job_id in jobs:
        _safe_id(job_id, "session request development probe job_id")
    _safe_id(normalized.get("locked_profile_id"), "session request locked profile_id")
    _safe_id(normalized.get("operator_id"), "session request operator_id")
    retry_from_session_id = normalized.get("retry_from_session_id")
    if retry_from_session_id is not None:
        _safe_id(retry_from_session_id, "session request retry_from_session_id")
    applicability = normalized.get("strata_applicability")
    scales = applicability.get("scale") if isinstance(applicability, dict) else None
    lights = applicability.get("lighting") if isinstance(applicability, dict) else None
    if not isinstance(scales, list) or not isinstance(lights, list):
        raise BallFrameEvidenceError("sealed session request strata authority is invalid")
    expected_scales = [
        row.get("stratum") for row in scales if isinstance(row, dict) and row.get("status") == "applicable"
    ]
    expected_lights = [
        row.get("stratum") for row in lights if isinstance(row, dict) and row.get("status") == "applicable"
    ]
    if (
        normalized.get("applicable_scale_strata") != expected_scales
        or normalized.get("applicable_lighting_strata") != expected_lights
    ):
        raise BallFrameEvidenceError("sealed session request strata selection is invalid")
    target_frame_count = normalized.get("target_frame_count")
    development_session_id = normalized.get("development_package_session_id")
    development_package_sha256 = normalized.get("development_package_sha256")
    if data_role == "development":
        if (
            target_frame_count is not None
            or development_session_id is not None
            or development_package_sha256 is not None
            or development_package_binding is not None
        ):
            raise BallFrameEvidenceError("development session request authority is invalid")
    elif (
        isinstance(target_frame_count, bool)
        or not isinstance(target_frame_count, int)
        or target_frame_count != sampling_manifest.get("target_frame_count")
        or not isinstance(development_package_binding, dict)
        or development_session_id != development_package_binding.get("session_id")
        or development_package_sha256 != development_package_binding.get("package_sha256")
    ):
        raise BallFrameEvidenceError("check session request authority is invalid")
    return normalized


def _frozen_profile_binding(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BallFrameEvidenceError("detector frozen profile binding is invalid")
    descriptor = value.get("model_descriptor")
    weights = descriptor.get("weights") if isinstance(descriptor, dict) else None
    model_version = value.get("model_version")
    if not isinstance(model_version, str) or not model_version:
        raise BallFrameEvidenceError("detector frozen profile model version is invalid")
    return {
        "profile_id": _safe_id(value.get("profile_id"), "detector frozen profile_id"),
        "profile_sha256": _sha256(value.get("profile_sha256"), "detector frozen profile sha256"),
        "model_id": _safe_id(value.get("model_id"), "detector frozen model_id"),
        "model_version": model_version,
        "model_descriptor_sha256": _sha256(
            value.get("model_descriptor_sha256"),
            "detector frozen model descriptor sha256",
        ),
        "weights_sha256": _sha256(
            weights.get("sha256") if isinstance(weights, dict) else None,
            "detector frozen weights sha256",
        ),
    }


def _verify_sealed_profile_selection(
    *,
    locked_profile: Any,
    control_profile_id: Any,
    control_profile: Any,
    probe_authorities: dict[str, dict[str, Any]],
) -> None:
    if not isinstance(locked_profile, dict) or not isinstance(control_profile, dict):
        raise BallFrameEvidenceError("sealed package profile selection is invalid")
    locked_profile_id = _safe_id(locked_profile.get("profile_id"), "locked profile_id")
    expected_control_profile_id = _safe_id(control_profile_id, "control profile_id")
    if control_profile.get("profile_id") != expected_control_profile_id:
        raise BallFrameEvidenceError("sealed control profile identity is invalid")
    expected_profile_bindings: dict[str, dict[str, Any]] | None = None
    for authority in probe_authorities.values():
        frozen_profiles = authority.get("frozen_profiles")
        if not isinstance(frozen_profiles, list):
            raise BallFrameEvidenceError("detector authority frozen profiles are invalid")
        bindings = [_frozen_profile_binding(profile) for profile in frozen_profiles]
        bindings_by_id = {binding["profile_id"]: binding for binding in bindings}
        if len(bindings_by_id) != len(bindings):
            raise BallFrameEvidenceError("detector authority frozen profiles are duplicated")
        if expected_profile_bindings is None:
            expected_profile_bindings = bindings_by_id
        elif bindings_by_id != expected_profile_bindings:
            raise BallFrameEvidenceError("detector authority frozen profile sets differ")
    if expected_profile_bindings is None:
        raise BallFrameEvidenceError("sealed package has no detector profile authority")
    remaining_profile_ids = sorted(set(expected_profile_bindings) - {locked_profile_id})
    preferred_control_profile_id = "current-coco-yolov8n-direct"
    deterministic_control_profile_id = (
        preferred_control_profile_id
        if preferred_control_profile_id in remaining_profile_ids
        else remaining_profile_ids[0]
        if remaining_profile_ids
        else None
    )
    if (
        expected_profile_bindings.get(locked_profile_id) != locked_profile
        or expected_control_profile_id != deterministic_control_profile_id
        or expected_profile_bindings.get(expected_control_profile_id) != control_profile
    ):
        raise BallFrameEvidenceError("sealed package profile selection differs from frozen authority")


def _detector_probe_authority_frame(
    authority: dict[str, Any],
    *,
    frame_index: int,
    artifact_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    report = authority["probe_report"]
    frames = report.get("frames")
    artifacts = report.get("artifacts")
    if not isinstance(frames, list) or not isinstance(artifacts, list):
        raise BallFrameEvidenceError("detector probe authority lacks report frame artifacts")
    matching_frames = [frame for frame in frames if isinstance(frame, dict) and frame.get("frame_index") == frame_index]
    matching_artifacts = [
        artifact for artifact in artifacts if isinstance(artifact, dict) and artifact.get("artifact_id") == artifact_id
    ]
    expected_url = f"/api/v1/detector-probes/{authority['job_id']}/artifacts/{artifact_id}"
    if (
        len(matching_frames) != 1
        or len(matching_artifacts) != 1
        or matching_frames[0].get("source_artifact_url") != expected_url
    ):
        raise BallFrameEvidenceError("detector probe authority does not contain the exact report frame artifact")
    frame = matching_frames[0]
    artifact = matching_artifacts[0]
    if (
        artifact.get("media_type") != "image/jpeg"
        or artifact.get("sha256") != frame.get("source_frame_sha256")
        or artifact.get("size_bytes") != frame.get("source_frame_size_bytes")
        or artifact.get("width") != frame.get("source_width")
        or artifact.get("height") != frame.get("source_height")
    ):
        raise BallFrameEvidenceError("detector probe report frame changed from its source artifact")
    return frame, artifact


def _detector_probe_frame_candidates(
    authority: dict[str, Any],
    *,
    frame: dict[str, Any],
    locked_profile: dict[str, Any],
) -> list[dict[str, Any]]:
    profile_results = frame.get("profile_results")
    matching = (
        [
            result
            for result in profile_results
            if isinstance(result, dict) and result.get("profile_id") == locked_profile.get("profile_id")
        ]
        if isinstance(profile_results, list)
        else []
    )
    if len(matching) != 1 or matching[0].get("profile_sha256") != locked_profile.get("profile_sha256"):
        raise BallFrameEvidenceError("detector probe authority lacks the exact locked profile result")
    raw_candidates = validate_detector_probe_candidate_accounting(matching[0])
    return normalize_detector_probe_candidates(
        frame_index=frame.get("frame_index"),
        probe_job_id=authority["job_id"],
        profile_id=locked_profile.get("profile_id"),
        raw_candidates=raw_candidates,
        width=frame.get("source_width"),
        height=frame.get("source_height"),
    )


def _sealed_detector_candidate_evidence(
    value: Any,
    *,
    collection_sha256: Any,
    rows_by_index: dict[int, dict[str, Any]],
    locked_profile: Any,
    source: dict[str, Any],
    revisions: list[dict[str, Any]],
    data_role: str,
    lineage: Any,
    proxy_authority: Any,
    probe_authorities: dict[str, dict[str, Any]],
    check_probe_authority: Any,
) -> int:
    if (
        not isinstance(value, list)
        or collection_sha256 != canonical_sha256(value)
        or not isinstance(locked_profile, dict)
    ):
        raise BallFrameEvidenceError("sealed detector candidate evidence is invalid")
    candidates: dict[tuple[int, str], dict[str, Any]] = {}
    previous_sort_key: tuple[int, int, str] | None = None
    for record in value:
        if not isinstance(record, dict) or set(record) != {
            "frame_index",
            "candidate_origin",
            "review_media",
            "candidate",
            "candidate_sha256",
            "decision",
        }:
            raise BallFrameEvidenceError("sealed detector candidate record fields are invalid")
        frame_index = _bounded_frame_index(record.get("frame_index"), source["frame_count"], "candidate")
        row = rows_by_index.get(frame_index)
        origin = record.get("candidate_origin")
        media = record.get("review_media")
        if (
            not isinstance(origin, dict)
            or set(origin)
            != {
                "probe_job_id",
                "probe_report_sha256",
                "probe_result_manifest_sha256",
                "source_artifact_id",
                "candidate_evidence_sha256",
            }
            or not isinstance(media, dict)
            or set(media)
            != {
                "probe_job_id",
                "probe_report_sha256",
                "probe_result_manifest_sha256",
                "source_artifact_id",
                "proxy_binding_sha256",
            }
        ):
            raise BallFrameEvidenceError("sealed detector candidate provenance fields are invalid")
        origin_job_id = _safe_id(origin.get("probe_job_id"), "candidate origin probe job_id")
        origin_report_sha256 = _sha256(
            origin.get("probe_report_sha256"),
            "candidate origin report sha256",
        )
        origin_result_sha256 = _sha256(
            origin.get("probe_result_manifest_sha256"),
            "candidate origin result manifest sha256",
        )
        origin_artifact_id = _safe_id(
            origin.get("source_artifact_id"),
            "candidate origin source artifact_id",
        )
        candidate_evidence_sha256 = _sha256(
            origin.get("candidate_evidence_sha256"),
            "candidate origin evidence sha256",
        )
        media_job_id = _safe_id(media.get("probe_job_id"), "candidate review media probe job_id")
        media_report_sha256 = _sha256(
            media.get("probe_report_sha256"),
            "candidate review media report sha256",
        )
        media_result_sha256 = _sha256(
            media.get("probe_result_manifest_sha256"),
            "candidate review media result manifest sha256",
        )
        media_artifact_id = _safe_id(
            media.get("source_artifact_id"),
            "candidate review media source artifact_id",
        )
        expected_proxy_sha256 = (
            canonical_sha256(row["proxy_binding"]) if row is not None and row.get("proxy_binding") is not None else None
        )
        if media.get("proxy_binding_sha256") is not None:
            _sha256(
                media.get("proxy_binding_sha256"),
                "candidate review proxy binding sha256",
            )
        origin_authority = probe_authorities.get(origin_job_id)
        media_authority = probe_authorities.get(media_job_id)
        if (
            origin_authority is None
            or media_authority is None
            or (data_role == "development" and origin_authority.get("audit_anchor_kind") != "audited_t2_legacy")
            or origin_authority.get("probe_report_sha256") != origin_report_sha256
            or origin_authority.get("probe_result_manifest_sha256") != origin_result_sha256
            or media_authority.get("probe_report_sha256") != media_report_sha256
            or media_authority.get("probe_result_manifest_sha256") != media_result_sha256
        ):
            raise BallFrameEvidenceError("sealed detector candidate lacks an audited origin authority")
        historical = proxy_authority.get("historical_probe_authority") if isinstance(proxy_authority, dict) else None
        if data_role == "check":
            if not isinstance(check_probe_authority, dict):
                raise BallFrameEvidenceError("sealed check candidate differs from frozen check authority")
            if historical is None:
                expected_origin_job_id = check_probe_authority.get("job_id")
                expected_origin_report_sha256 = check_probe_authority.get("report_sha256")
                expected_origin_result_sha256 = check_probe_authority.get("result_manifest_sha256")
            elif isinstance(historical, dict):
                expected_origin_job_id = historical.get("probe_job_id")
                expected_origin_report_sha256 = historical.get("probe_report_sha256")
                expected_origin_result_sha256 = historical.get("probe_result_manifest_sha256")
            else:
                raise BallFrameEvidenceError("sealed check candidate differs from frozen check authority")
            if (
                origin_job_id != expected_origin_job_id
                or origin_report_sha256 != expected_origin_report_sha256
                or origin_result_sha256 != expected_origin_result_sha256
                or media_job_id != check_probe_authority.get("job_id")
                or media_report_sha256 != check_probe_authority.get("report_sha256")
                or media_result_sha256 != check_probe_authority.get("result_manifest_sha256")
            ):
                raise BallFrameEvidenceError("sealed check candidate differs from frozen check authority")
        origin_inherited = build_detector_probe_inherited_evidence_authority(origin_authority["probe_report"])
        if candidate_evidence_sha256 != origin_inherited["candidate_evidence_sha256"]:
            raise BallFrameEvidenceError("sealed detector candidate origin evidence digest is invalid")
        origin_frame, _origin_artifact = _detector_probe_authority_frame(
            origin_authority,
            frame_index=frame_index,
            artifact_id=origin_artifact_id,
        )
        media_frame, media_artifact = _detector_probe_authority_frame(
            media_authority,
            frame_index=frame_index,
            artifact_id=media_artifact_id,
        )
        if not isinstance(lineage, dict):
            raise BallFrameEvidenceError("sealed detector candidate lineage is invalid")
        lineage_jobs = lineage.get("development_probe_job_ids")
        lineage_reports = lineage.get("development_probe_report_sha256s")
        lineage_results = lineage.get("development_probe_result_manifest_sha256s")
        if data_role == "development" and (
            not isinstance(lineage_jobs, list)
            or origin_job_id not in lineage_jobs
            or media_job_id not in lineage_jobs
            or not isinstance(lineage_reports, dict)
            or lineage_reports.get(origin_job_id) != origin_report_sha256
            or lineage_reports.get(media_job_id) != media_report_sha256
            or not isinstance(lineage_results, dict)
            or lineage_results.get(origin_job_id) != origin_result_sha256
            or lineage_results.get(media_job_id) != media_result_sha256
        ):
            raise BallFrameEvidenceError("sealed detector candidate provenance changed from lineage")
        row_probe = row.get("probe_evidence") if row is not None else None
        if (
            not isinstance(row_probe, dict)
            or row.get("frame_role") != "primary"
            or row_probe.get("probe_job_id") != media_job_id
            or row_probe.get("probe_report_sha256") != media_report_sha256
            or row_probe.get("probe_result_manifest_sha256") != media_result_sha256
            or row_probe.get("artifact_id") != media_artifact_id
            or media.get("proxy_binding_sha256") != expected_proxy_sha256
            or origin_artifact_id != media_artifact_id
            or media_frame.get("source_frame_sha256") != row["source_frame_jpeg"]["sha256"]
            or media_frame.get("source_frame_size_bytes") != row["source_frame_jpeg"]["size_bytes"]
            or media_artifact.get("sha256") != row["source_frame_jpeg"]["sha256"]
            or media_artifact.get("size_bytes") != row["source_frame_jpeg"]["size_bytes"]
        ):
            raise BallFrameEvidenceError("sealed detector candidate review media is invalid")
        if historical is None:
            if (
                origin_job_id != media_job_id
                or origin_report_sha256 != media_report_sha256
                or origin_result_sha256 != media_result_sha256
            ):
                raise BallFrameEvidenceError("direct detector candidate origin differs from review media")
        else:
            if (
                origin_job_id != historical.get("probe_job_id")
                or origin_report_sha256 != historical.get("probe_report_sha256")
                or origin_result_sha256 != historical.get("probe_result_manifest_sha256")
                or candidate_evidence_sha256 != historical.get("candidate_evidence_sha256")
                or media_job_id != proxy_authority.get("probe_job_id")
                or media_report_sha256 != proxy_authority.get("probe_report_sha256")
                or media_result_sha256 != proxy_authority.get("probe_result_manifest_sha256")
            ):
                raise BallFrameEvidenceError("sealed detector candidate origin changed from proxy inheritance")
        candidate = record.get("candidate")
        if not isinstance(candidate, dict) or set(candidate) != {
            "candidate_id",
            "profile_id",
            "rank",
            "bbox_source_px",
            "confidence",
            "annotation_state",
            "training_use",
            "truth_status",
            "suggestion_job_id",
            "suggestion_sha256",
        }:
            raise BallFrameEvidenceError("sealed detector candidate fields are invalid")
        candidate_id = _safe_id(candidate.get("candidate_id"), "detector candidate_id")
        rank = candidate.get("rank")
        confidence = candidate.get("confidence")
        box = candidate.get("bbox_source_px")
        expected_candidates = _detector_probe_frame_candidates(
            origin_authority,
            frame=origin_frame,
            locked_profile=locked_profile,
        )
        if (
            row is None
            or candidate.get("profile_id") != locked_profile.get("profile_id")
            or candidate.get("suggestion_job_id") != origin_job_id
            or isinstance(rank, bool)
            or not isinstance(rank, int)
            or not 1 <= rank <= 5
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0 <= float(confidence) <= 1
            or candidate.get("annotation_state") != "suggested"
            or candidate.get("training_use") != "excluded"
            or candidate.get("truth_status") != "unconfirmed_suggestion"
            or not isinstance(box, dict)
            or set(box) != {"left", "top", "right", "bottom"}
            or any(
                isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item))
                for item in box.values()
            )
            or not (
                0 <= float(box["left"]) < float(box["right"]) <= source["width"]
                and 0 <= float(box["top"]) < float(box["bottom"]) <= source["height"]
            )
            or record.get("candidate_sha256")
            != canonical_sha256(
                {key: item for key, item in candidate.items() if key not in {"suggestion_job_id", "suggestion_sha256"}}
            )
            or candidate.get("suggestion_sha256") != record.get("candidate_sha256")
            or rank > len(expected_candidates)
            or expected_candidates[rank - 1] != candidate
        ):
            raise BallFrameEvidenceError("sealed detector candidate authority is invalid")
        sort_key = (frame_index, rank, candidate_id)
        if previous_sort_key is not None and sort_key <= previous_sort_key:
            raise BallFrameEvidenceError("sealed detector candidates must be unique and ordered")
        previous_sort_key = sort_key
        decision = record.get("decision")
        if decision is not None:
            if not isinstance(decision, dict) or set(decision) != {
                "decision",
                "revision_id",
                "revision",
                "operator_id",
                "decided_at",
            }:
                raise BallFrameEvidenceError("detector candidate decision fields are invalid")
            if decision.get("decision") not in {
                "accepted_human_annotation",
                "dismissed_manual_annotation",
            }:
                raise BallFrameEvidenceError("detector candidate decision is invalid")
            _safe_id(decision.get("revision_id"), "candidate revision_id")
            _positive_int(decision.get("revision"), "candidate revision")
            _safe_id(decision.get("operator_id"), "candidate operator_id")
            if not isinstance(decision.get("decided_at"), str):
                raise BallFrameEvidenceError("detector candidate decision timestamp is invalid")
        key = (frame_index, candidate_id)
        if key in candidates:
            raise BallFrameEvidenceError("sealed detector candidate identity is duplicated")
        candidates[key] = record
    expected_candidate_keys: set[tuple[int, str]] = set()
    historical = proxy_authority.get("historical_probe_authority") if isinstance(proxy_authority, dict) else None
    for frame_index, row in rows_by_index.items():
        if row.get("frame_role") != "primary":
            continue
        probe = row.get("probe_evidence")
        if not isinstance(probe, dict):
            raise BallFrameEvidenceError("primary frame lacks detector probe evidence")
        origin_job_id = (
            historical.get("probe_job_id")
            if row.get("proxy_binding") is not None and isinstance(historical, dict)
            else probe.get("probe_job_id")
        )
        authority = probe_authorities.get(origin_job_id)
        if authority is None:
            raise BallFrameEvidenceError("primary frame lacks detector origin authority")
        origin_frame, _artifact = _detector_probe_authority_frame(
            authority,
            frame_index=frame_index,
            artifact_id=probe.get("artifact_id"),
        )
        for candidate in _detector_probe_frame_candidates(
            authority,
            frame=origin_frame,
            locked_profile=locked_profile,
        ):
            expected_candidate_keys.add((frame_index, candidate["candidate_id"]))
    if set(candidates) != expected_candidate_keys:
        raise BallFrameEvidenceError("sealed detector candidate collection is incomplete or invented")
    seen_decisions: set[tuple[int, str]] = set()
    for revision in revisions:
        for prefix, expected_decision in (
            ("accepted", "accepted_human_annotation"),
            ("dismissed", "dismissed_manual_annotation"),
        ):
            if revision.get(f"{prefix}_suggestion_kind") != "detector_candidate":
                continue
            key = (
                revision.get("frame_index"),
                revision.get(f"{prefix}_suggestion_id"),
            )
            record = candidates.get(key)
            expected = {
                "decision": expected_decision,
                "revision_id": revision.get("revision_id"),
                "revision": revision.get("revision"),
                "operator_id": revision.get("operator_id"),
                "decided_at": revision.get("created_at"),
            }
            if (
                record is None
                or key in seen_decisions
                or record.get("candidate_origin", {}).get("probe_job_id") != revision.get(f"{prefix}_suggestion_job_id")
                or record.get("candidate_sha256") != revision.get(f"{prefix}_suggestion_sha256")
                or record.get("decision") != expected
            ):
                raise BallFrameEvidenceError("detector candidate revision binding is invalid")
            seen_decisions.add(key)
    if {key for key, record in candidates.items() if record.get("decision") is not None} != seen_decisions:
        raise BallFrameEvidenceError("detector candidate decision has no sealed revision")
    if data_role != "development":
        return 0
    return sum(record.get("decision") is None for record in candidates.values())


def _sealed_propagation_reports(
    value: Any,
    *,
    collection_sha256: Any,
    session_id: Any,
    data_role: str,
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list) or collection_sha256 != canonical_sha256(value):
        raise BallFrameEvidenceError("sealed propagation report collection is invalid")
    if data_role == "check" and value:
        raise BallFrameEvidenceError("check packages cannot contain propagation reports")
    expected_session_id = _safe_id(session_id, "annotation session_id")
    reports: dict[str, dict[str, Any]] = {}
    previous_job_id: str | None = None
    for report in value:
        if (
            not isinstance(report, dict)
            or set(report) != _PROPAGATION_REPORT_FIELDS
            or report.get("schema_version") != "1.0"
            or report.get("artifact_type") != "ball_propagation_report"
            or report.get("session_id") != expected_session_id
            or report.get("report_sha256")
            != canonical_sha256({key: item for key, item in report.items() if key != "report_sha256"})
        ):
            raise BallFrameEvidenceError("sealed propagation report authority is invalid")
        job_id = _safe_id(report.get("job_id"), "propagation report job_id")
        if previous_job_id is not None and job_id <= previous_job_id:
            raise BallFrameEvidenceError("sealed propagation reports must be unique and ordered")
        previous_job_id = job_id
        _sha256(report.get("intent_sha256"), "propagation intent sha256")
        _safe_id(report.get("mutation_id"), "propagation mutation_id")
        seed_frame_index = _nonnegative_int(report.get("seed_frame_index"), "propagation seed frame")
        expected_seed_revision = _positive_int(
            report.get("expected_seed_revision"),
            "propagation seed revision",
        )
        radius = report.get("radius_frames")
        if (
            isinstance(radius, bool)
            or not isinstance(radius, int)
            or radius
            not in {
                1,
                2,
            }
        ):
            raise BallFrameEvidenceError("propagation report radius is invalid")
        seed_binding = report.get("seed_binding")
        if (
            not isinstance(seed_binding, dict)
            or seed_binding.get("frame_index") != seed_frame_index
            or seed_binding.get("annotation_revision") != expected_seed_revision
            or report.get("seed_binding_sha256") != canonical_sha256(seed_binding)
        ):
            raise BallFrameEvidenceError("propagation report seed binding is invalid")
        tracker_profile = report.get("tracker_profile")
        tracker_profile_sha256 = _sha256(
            report.get("tracker_profile_sha256"),
            "propagation tracker profile sha256",
        )
        if (
            not isinstance(tracker_profile, dict)
            or tracker_profile.get("profile_sha256") != tracker_profile_sha256
            or canonical_sha256({key: item for key, item in tracker_profile.items() if key != "profile_sha256"})
            != tracker_profile_sha256
        ):
            raise BallFrameEvidenceError("propagation report tracker profile is invalid")
        target_indices = report.get("target_frame_indices")
        if (
            not isinstance(target_indices, list)
            or not 1 <= len(target_indices) <= 4
            or target_indices != sorted(set(target_indices))
            or any(isinstance(index, bool) or not isinstance(index, int) or index < 0 for index in target_indices)
        ):
            raise BallFrameEvidenceError("propagation report target frames are invalid")
        expected_intent = {
            "session_id": expected_session_id,
            "mutation_id": report["mutation_id"],
            "seed_frame_index": seed_frame_index,
            "radius_frames": radius,
            "expected_seed_revision": expected_seed_revision,
            "seed_binding": seed_binding,
            "target_frame_indices": target_indices,
        }
        if canonical_sha256(expected_intent) != report["intent_sha256"]:
            raise BallFrameEvidenceError("propagation report intent is invalid")
        _safe_id(
            report.get("neighbor_probe_job_id"),
            "propagation neighbor probe job_id",
        )
        _sha256(
            report.get("neighbor_probe_report_sha256"),
            "propagation neighbor report sha256",
        )
        _sha256(
            report.get("neighbor_probe_result_manifest_sha256"),
            "propagation neighbor manifest sha256",
        )
        frame_results = report.get("frame_results")
        suggestions = report.get("suggestions")
        summary = report.get("summary")
        counts = report.get("decision_counts")
        if (
            not isinstance(frame_results, list)
            or [item.get("frame_index") for item in frame_results] != target_indices
            or any(not isinstance(item, dict) for item in frame_results)
            or not isinstance(suggestions, list)
            or any(not isinstance(item, dict) for item in suggestions)
            or not isinstance(summary, dict)
            or not isinstance(counts, dict)
            or set(counts) != {"confirmed", "dismissed", "pending"}
        ):
            raise BallFrameEvidenceError("propagation report results are invalid")
        confirmed = sum(isinstance(item.get("human_confirmation"), dict) for item in frame_results)
        dismissed = sum(isinstance(item.get("human_decision"), dict) for item in frame_results)
        pending = sum(item.get("pending_human_confirmation") is True for item in frame_results)
        successes = sum(item.get("status") == "success" for item in frame_results)
        if (
            counts
            != {
                "confirmed": confirmed,
                "dismissed": dismissed,
                "pending": pending,
            }
            or confirmed + dismissed + pending != successes
            or summary.get("succeeded_frame_count") != successes
            or summary.get("human_validated_frame_count") != confirmed
            or summary.get("human_dismissed_frame_count") != dismissed
            or summary.get("pending_human_confirmation_count") != pending
            or summary.get("pending_human_confirmation") is not (pending > 0)
            or not isinstance(report.get("created_at"), str)
            or not isinstance(report.get("updated_at"), str)
        ):
            raise BallFrameEvidenceError("propagation report decision accounting is invalid")
        reports[job_id] = report
    return reports


def _frame_index_list(
    value: Any,
    label: str,
    *,
    allow_empty: bool,
    frame_count: int,
) -> list[int]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise BallFrameEvidenceError(f"{label} are invalid")
    result = [_bounded_frame_index(item, frame_count, label) for item in value]
    if result != sorted(set(result)):
        raise BallFrameEvidenceError(f"{label} must be unique and ordered")
    return result


def _frame_role(value: Any) -> str:
    if value not in {"primary", "supplemental"}:
        raise BallFrameEvidenceError("frame_role must be primary or supplemental")
    return value


def _sha256(value: Any, label: str) -> str:
    try:
        return require_sha256(value, label)
    except DetectorDevelopmentError as exc:
        raise BallFrameEvidenceError(str(exc)) from exc


def _safe_id(value: Any, label: str) -> str:
    try:
        return require_safe_id(value, label)
    except DetectorDevelopmentError as exc:
        raise BallFrameEvidenceError(str(exc)) from exc


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BallFrameEvidenceError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BallFrameEvidenceError(f"{label} must be a non-negative integer")
    return value


def _bounded_frame_index(value: Any, frame_count: int, label: str) -> int:
    frame_index = _nonnegative_int(value, f"{label} frame_index")
    if frame_index >= frame_count:
        raise BallFrameEvidenceError(f"{label} frame_index is outside the source")
    return frame_index


def _decode_mode(value: Any) -> str:
    if not isinstance(value, str) or value not in _DECODE_MODES:
        raise BallFrameEvidenceError("effective decode mode is invalid")
    return value


def _true_presentation_timestamp(value: Any) -> dict[str, Any]:
    expected = {
        "status": "not_collected",
        "value_seconds": None,
        "method": None,
    }
    if value != expected:
        raise BallFrameEvidenceError(
            "true presentation timestamp must remain explicitly not_collected without a separately verified contract"
        )
    return deepcopy(expected)


def _decoder_pos_msec(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or abs(float(value)) > _MAX_ABSOLUTE_DECODER_POS_MSEC
    ):
        raise BallFrameEvidenceError(f"{label} must be finite and within the bounded media window")
    return float(value)


def _bounded_tolerance_msec(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1000.0
    ):
        raise BallFrameEvidenceError(f"{label} must be finite and between 0 and 1000 milliseconds")
    return float(value)


def _finite_nonnegative(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise BallFrameEvidenceError(f"{label} must be finite and non-negative")
    return float(value)


def _finite_positive(value: Any, label: str) -> float:
    result = _finite_nonnegative(value, label)
    if result <= 0:
        raise BallFrameEvidenceError(f"{label} must be positive")
    return result


__all__ = [
    "BallFrameEvidenceError",
    "DECODER_TIMING_OBSERVATION_METHOD",
    "DISPLAY_TIME_DERIVATION",
    "POSITION_VERIFICATION",
    "TIMING_PROFILE_ID",
    "build_detector_probe_inherited_evidence_authority",
    "build_detector_probe_job_authority",
    "build_detector_probe_result_manifest_authority",
    "build_frame_evidence_row",
    "build_nullable_proxy_binding",
    "build_source_frame_timing_binding",
    "normalize_detector_probe_candidates",
    "validate_detector_probe_candidate_accounting",
    "validate_detector_probe_job_authority",
    "validate_frame_evidence_row",
    "validate_nullable_proxy_binding",
    "validate_source_frame_timing_binding",
    "verify_frame_evidence_package",
]
