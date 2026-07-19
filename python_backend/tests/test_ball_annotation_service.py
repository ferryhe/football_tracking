from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

from audited_authority_test_support import patched_audited_t2_probe_bindings

from football_tracking.api.schemas import BallAnnotationPackageView
from football_tracking.ball_annotation_service import (
    BallAnnotationService,
    BallAnnotationServiceError,
)
from football_tracking.ball_detector_feasibility import (
    sample_unseen_temporal_groups,
    temporal_group_for_frame,
)
from football_tracking.ball_frame_evidence import (
    BallFrameEvidenceError,
    _attempt_family_authority,
    build_detector_probe_inherited_evidence_authority,
    build_detector_probe_result_manifest_authority,
    verify_frame_evidence_package,
)
from football_tracking.detector_development_common import (
    canonical_json_bytes,
    canonical_sha256,
)
from football_tracking.detector_development_common import (
    read_regular_bytes as development_read_regular_bytes,
)
from football_tracking.detector_probe import semantic_probe_intent_sha256
from football_tracking.review_proxy_mapping import build_review_proxy_manifest

LOCKED_PROFILE = "official-coco-yolo11s-sahi"
CONTROL_PROFILE = "current-coco-yolov8n-direct"
_ACTIVE_FAKE_AUDIT_BINDINGS: dict[str, dict[str, str]] | None = None
_JPEG_FIXTURE = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAIBAQEBAQIBAQECAgICAgQDAgICAgUEBAMEBgUGBgYFBgYGBwkIBgcJBwYGCAsI"
    "CQoKCgoKBggLDAsKDAkKCgr/2wBDAQICAgICAgUDAwUKBwYHCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoK"
    "CgoKCgoKCgoKCgoKCgr/wAARCAAgAEADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAA"
    "AgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6"
    "Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG"
    "x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREA"
    "AgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5"
    "OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPE"
    "xcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD5Hoor6A+H3w+/YQ1H9hDxd478d/FvXrT4"
    "xWmvQR6FoUNgpQoVk8uKOPftnt5VEjT3LMj27xRKsZyiXv8AOWHw8sTKSUkrJvVpbK9lfdvoj+TcLhZ4ucoxlFWi5e80r2V7"
    "K+7fRHz/AEUUVgcoUUUUAFFFFABRRRQAUUUUAFFFFABRRRQB/9k=",
    validate=True,
)


def _jpeg() -> bytes:
    return _JPEG_FIXTURE


def _audited_probe_binding(job: dict[str, Any]) -> dict[str, str]:
    report = job["report"]
    return {
        "canonical_job_record_sha256": canonical_sha256(job),
        "request_sha256": job["request_sha256"],
        "report_sha256": report["report_sha256"],
        "result_manifest_sha256": job["result_manifest_sha256"],
        "execution_bundle_sha256": report["lineage"]["execution_bundle_sha256"],
        "runtime_environment_sha256": report["lineage"]["runtime_environment_sha256"],
    }


def _set_job_runtime_execution_authority(
    job: dict[str, Any],
    *,
    runtime_environment_sha256: str,
    execution_bundle_sha256: str,
) -> None:
    frozen = job["frozen_request"]
    frozen["runtime_environment_sha256"] = runtime_environment_sha256
    frozen["execution_bundle_sha256"] = execution_bundle_sha256
    job["request_sha256"] = canonical_sha256(frozen)
    job["intent_sha256"] = canonical_sha256({key: value for key, value in frozen.items() if key != "retry_from_job_id"})
    job["semantic_intent_sha256"] = semantic_probe_intent_sha256(frozen)
    report = job["report"]
    report["request_sha256"] = job["request_sha256"]
    report["lineage"].update(
        {
            "runtime_environment_sha256": runtime_environment_sha256,
            "execution_bundle_sha256": execution_bundle_sha256,
            "intent_sha256": job["intent_sha256"],
            "semantic_intent_sha256": job["semantic_intent_sha256"],
        }
    )
    report["report_sha256"] = canonical_sha256({key: value for key, value in report.items() if key != "report_sha256"})
    _manifest, job["result_manifest_sha256"] = build_detector_probe_result_manifest_authority(report)


def _reseal_annotation_package(package: dict[str, Any]) -> None:
    package["detector_candidate_evidence_sha256"] = canonical_sha256(package["detector_candidate_evidence"])
    package["attempt_family_sha256"] = canonical_sha256(_attempt_family_authority(package))
    package["package_sha256"] = canonical_sha256(
        {key: value for key, value in package.items() if key != "package_sha256"}
    )


def _profile(profile_id: str, marker: str) -> dict[str, Any]:
    return {
        "profile_id": profile_id,
        "profile_sha256": marker * 64,
        "model_id": f"model-{marker}",
        "model_version": "1.0",
        "model_descriptor_sha256": chr(ord(marker) + 1) * 64,
        "model_descriptor": {
            "weights": {"sha256": chr(ord(marker) + 2) * 64, "size_bytes": 7},
        },
    }


class _FakeProbeGateway:
    def __init__(self) -> None:
        self.artifacts: dict[tuple[str, str], bytes] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.create_requests: list[dict[str, Any]] = []
        self.job_requests: dict[str, dict[str, Any]] = {}
        self.jobs_by_request_sha256: dict[str, str] = {}
        self.cancel_requests: list[str] = []
        self.cancel_error: Exception | None = None
        self.on_create: Callable[[dict[str, Any]], None] | None = None
        self.audit_bindings = _ACTIVE_FAKE_AUDIT_BINDINGS
        self.unaudited_job_ids: set[str] = set()
        self.jobs["probe-development"] = self._ready_job("probe-development", [0, 40, 80, 120, 160, 199])
        self.jobs["probe-development-retry"] = self._ready_job(
            "probe-development-retry", [0, 40, 80, 120, 160, 199], retry_from="probe-development"
        )

    def _ready_job(
        self,
        job_id: str,
        frame_indices: list[int],
        *,
        retry_from: str | None = None,
        source_sha256: str = "a" * 64,
    ) -> dict[str, Any]:
        payload = _jpeg()
        profiles = [_profile(CONTROL_PROFILE, "b"), _profile(LOCKED_PROFILE, "d")]
        frames = []
        artifacts = []
        for frame_index in frame_indices:
            decoder_pos_msec = -50.0 if frame_index == 0 else frame_index * 50.0
            artifact_id = f"source-{frame_index:09d}"
            self.artifacts[(job_id, artifact_id)] = payload
            artifacts.append(
                {
                    "artifact_id": artifact_id,
                    "relative_path": f"frames/{frame_index:09d}.jpg",
                    "media_type": "image/jpeg",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size_bytes": len(payload),
                    "width": 64,
                    "height": 32,
                }
            )
            candidate = {
                "frame_index": frame_index,
                "bbox_source_px": [10.0, 10.0, 14.0, 14.0],
                "confidence": 0.8,
                "class_name": "ball",
                "checkpoint_class_name": "sports ball",
                "source": "yolo_sahi",
                "coordinate_reason": "sahi_tile_offset_applied",
                "merge_reason": "retained_top_k",
            }
            frames.append(
                {
                    "frame_index": frame_index,
                    "source_width": 64,
                    "source_height": 32,
                    "requested_decode_mode": "preroll",
                    "effective_decode_mode": "preroll_verified",
                    "decoded_frame_position": frame_index,
                    "decoder_reported_pos_msec": decoder_pos_msec,
                    "decoder_timing_observation_method": "opencv_cap_prop_pos_msec_after_verified_frame_read",
                    "media_integrity": {
                        "status": "ok",
                        "width": 64,
                        "height": 32,
                        "gray": False,
                        "low_information": False,
                        "likely_corrupt": False,
                    },
                    "source_artifact_url": f"/api/v1/detector-probes/{job_id}/artifacts/{artifact_id}",
                    "source_frame_sha256": hashlib.sha256(payload).hexdigest(),
                    "source_frame_size_bytes": len(payload),
                    "profile_results": [
                        {
                            "profile_id": profile["profile_id"],
                            "profile_sha256": profile["profile_sha256"],
                            "status": "completed",
                            "candidate_count": 1 if profile["profile_id"] == LOCKED_PROFILE else 0,
                            "top_k": 5,
                            "raw_candidates": [candidate] if profile["profile_id"] == LOCKED_PROFILE else [],
                            "display_candidate": candidate if profile["profile_id"] == LOCKED_PROFILE else None,
                            "filter_reasons": {},
                            "failure_code": None,
                        }
                        for profile in profiles
                    ],
                }
            )
        report: dict[str, Any] = {
            "schema_version": "1.0",
            "artifact_type": "detector_probe_report",
            "job_id": job_id,
            "request_sha256": "1" * 64,
            "source": {
                "source_id": "source-one",
                "relative_path": "inputs/source.mp4",
                "sha256": source_sha256,
                "file_identity_sha256": "2" * 64,
                "size_bytes": 1234,
                "width": 64,
                "height": 32,
                "frame_count": 200,
                "tracking_contract_relative_path": "outputs/tracking_contract_v2.json",
                "tracking_contract_sha256": "3" * 64,
            },
            "lineage": {
                "parent_trial_id": "production_trial_one",
                "runtime_environment_sha256": "4" * 64,
                "execution_bundle_sha256": "6" * 64,
                "frozen_profiles_sha256": canonical_sha256(profiles),
            },
            "frozen_profiles": profiles,
            "top_k": 5,
            "frames": frames,
            "decode": {
                "width": 64,
                "height": 32,
                "frame_count": 200,
                "fps": 20.0,
                "requested_decode_mode": "preroll",
                "effective_decode_mode": "preroll_verified",
                "verified_frame_indices": frame_indices,
                "position_verification": "opencv_next_frame_index_with_0.25_tolerance",
                "frame_timing_observations": [
                    {
                        "frame_index": frame_index,
                        "decoder_reported_pos_msec": (-50.0 if frame_index == 0 else frame_index * 50.0),
                        "observation_method": "opencv_cap_prop_pos_msec_after_verified_frame_read",
                    }
                    for frame_index in frame_indices
                ],
            },
            "artifacts": artifacts,
        }
        report["report_sha256"] = canonical_sha256(report)
        frozen_request = {
            "parent_trial_id": "production_trial_one",
            "source_id": "source-one",
            "source_relative_path": "inputs/source.mp4",
            "source_sha256": source_sha256,
            "source_file_identity_sha256": "2" * 64,
            "source_size_bytes": 1234,
            "source_width": 64,
            "source_height": 32,
            "source_frame_count": 200,
            "tracking_contract_relative_path": "outputs/tracking_contract_v2.json",
            "tracking_contract_sha256": "3" * 64,
            "base_config_relative_path": "config/base.yaml",
            "base_config_sha256": "9" * 64,
            "effective_config_relative_path": "config/effective.yaml",
            "effective_config_sha256": "a" * 64,
            "trial_intent_sha256": "b" * 64,
            "tuning_patch_binding": {
                "state": "absent",
                "schema_version": "1.0",
                "version_id": None,
                "parent_version_id": None,
                "values_sha256": "c" * 64,
            },
            "tuning_patch_sha256": "d" * 64,
            "profile_ids": sorted(profile["profile_id"] for profile in profiles),
            "frozen_profiles_sha256": canonical_sha256(profiles),
            "profile_sha256s": {profile["profile_id"]: profile["profile_sha256"] for profile in profiles},
            "profile_bindings": [
                {
                    "profile_id": profile["profile_id"],
                    "profile_sha256": profile["profile_sha256"],
                    "model_id": profile["model_id"],
                    "model_version": profile["model_version"],
                    "model_descriptor_sha256": profile["model_descriptor_sha256"],
                    "weights_sha256": profile["model_descriptor"]["weights"]["sha256"],
                    "weights_size_bytes": profile["model_descriptor"]["weights"]["size_bytes"],
                }
                for profile in sorted(profiles, key=lambda item: item["profile_id"])
            ],
            "execution_bundle": {"fixture": "current"},
            "execution_bundle_sha256": "6" * 64,
            "runtime_environment_sha256": "4" * 64,
            "frame_indices": frame_indices,
            "top_k": 5,
            "requested_decode_mode": "preroll",
            "annotation_sampling_manifest_sha256": None,
            "retry_from_job_id": retry_from,
        }
        request_sha256 = canonical_sha256(frozen_request)
        intent_sha256 = canonical_sha256(
            {key: value for key, value in frozen_request.items() if key != "retry_from_job_id"}
        )
        semantic_intent_sha256 = semantic_probe_intent_sha256(frozen_request)
        report["request_sha256"] = request_sha256
        report["lineage"].update(
            {
                "intent_sha256": intent_sha256,
                "semantic_intent_sha256": semantic_intent_sha256,
            }
        )
        report["report_sha256"] = canonical_sha256(
            {key: value for key, value in report.items() if key != "report_sha256"}
        )
        _manifest, result_manifest_sha256 = build_detector_probe_result_manifest_authority(report)
        return {
            "schema_version": "1.0",
            "artifact_type": "detector_probe_job",
            "job_id": job_id,
            "request_sha256": request_sha256,
            "intent_sha256": intent_sha256,
            "semantic_intent_sha256": semantic_intent_sha256,
            "status": "ready",
            "retry_from_job_id": retry_from,
            "frozen_request": frozen_request,
            "frozen_profiles": profiles,
            "report": report,
            "result_manifest_sha256": result_manifest_sha256,
        }

    def get_probe(self, job_id: str) -> dict[str, Any]:
        if job_id not in self.jobs:
            raise KeyError(job_id)
        job = self.jobs[job_id]
        if self.audit_bindings is not None and job.get("status") == "ready" and job_id not in self.unaudited_job_ids:
            self.audit_bindings[job_id] = _audited_probe_binding(job)
        return deepcopy(self.jobs[job_id])

    def create_probe(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.on_create is not None:
            self.on_create(request)
        self.create_requests.append(deepcopy(request))
        request_sha256 = canonical_sha256(request)
        existing_job_id = self.jobs_by_request_sha256.get(request_sha256)
        if existing_job_id is not None:
            return deepcopy(self.jobs[existing_job_id])
        job_id = f"probe-check-{len(self.jobs_by_request_sha256) + 1}"
        self.jobs[job_id] = {
            "schema_version": "1.0",
            "artifact_type": "detector_probe_job",
            "job_id": job_id,
            "status": "queued",
            "retry_from_job_id": request.get("retry_from_job_id"),
            "report": None,
            "result_manifest_sha256": None,
        }
        self.job_requests[job_id] = deepcopy(request)
        self.jobs_by_request_sha256[request_sha256] = job_id
        return deepcopy(self.jobs[job_id])

    def complete(self, job_id: str) -> None:
        request = self.job_requests[job_id]
        completed = self._ready_job(job_id, request["frame_indices"])
        frozen_request = deepcopy(completed["frozen_request"])
        for field in (
            "parent_trial_id",
            "profile_ids",
            "frame_indices",
            "top_k",
            "annotation_sampling_manifest_sha256",
            "retry_from_job_id",
        ):
            if field in request:
                frozen_request[field] = deepcopy(request[field])
        completed["request_sha256"] = canonical_sha256(frozen_request)
        completed["intent_sha256"] = canonical_sha256(
            {key: value for key, value in frozen_request.items() if key != "retry_from_job_id"}
        )
        completed["semantic_intent_sha256"] = semantic_probe_intent_sha256(frozen_request)
        completed["retry_from_job_id"] = frozen_request.get("retry_from_job_id")
        completed["retry_kind"] = frozen_request.get("retry_kind")
        completed["frozen_request"] = frozen_request
        completed["report"]["request_sha256"] = completed["request_sha256"]
        completed["report"]["lineage"].update(
            {
                "execution_bundle_sha256": "6" * 64,
                "frozen_profiles_sha256": canonical_sha256(completed["report"]["frozen_profiles"]),
                "intent_sha256": completed["intent_sha256"],
                "semantic_intent_sha256": completed["semantic_intent_sha256"],
            }
        )
        completed["report"]["report_sha256"] = canonical_sha256(
            {key: value for key, value in completed["report"].items() if key != "report_sha256"}
        )
        _manifest, completed["result_manifest_sha256"] = build_detector_probe_result_manifest_authority(
            completed["report"]
        )
        self.jobs[job_id] = completed

    def cancel_probe(self, job_id: str) -> dict[str, Any]:
        self.cancel_requests.append(job_id)
        if self.cancel_error is not None:
            raise self.cancel_error
        job = self.jobs[job_id]
        if job["status"] not in {"ready", "failed", "blocked", "cancelled"}:
            job.update(
                {
                    "status": "cancelled",
                    "stage": "cancelled",
                    "error_code": "cancelled",
                }
            )
        return deepcopy(job)

    def read_probe_artifact(self, job_id: str, artifact_id: str) -> tuple[bytes, str, str]:
        content = self.artifacts[(job_id, artifact_id)]
        return content, "image/jpeg", hashlib.sha256(content).hexdigest()


def _request(**patch: Any) -> dict[str, Any]:
    request = {
        "data_role": "development",
        "development_probe_job_ids": ["probe-development", "probe-development-retry"],
        "locked_profile_id": LOCKED_PROFILE,
        "target_frame_count": None,
        "sampling_profile_id": "tiny_ball_temporal_groups_v1",
        "metric_profile_id": "tiny_ball_feasibility_metric_v1",
        "operator_id": "operator-one",
        "strata_applicability": None,
        "retry_from_session_id": None,
        "development_package_session_id": None,
        "development_package_sha256": None,
    }
    request.update(patch)
    if request["data_role"] == "check" and request["target_frame_count"] is None:
        request["target_frame_count"] = 20
    if request["strata_applicability"] is None:
        check = request["data_role"] == "check"
        target = request["target_frame_count"] if check else 0
        first_quota = target // 2
        request["strata_applicability"] = {
            "scale": [
                {
                    "stratum": name,
                    "status": "applicable",
                    "evidence_note": f"pre-reveal scale review {name}",
                }
                for name in ("near", "mid", "far")
            ],
            "lighting": [
                {
                    "stratum": name,
                    "status": ("applicable" if name in {"bright_sun", "shadow"} else "not_applicable"),
                    "evidence_note": f"pre-reveal lighting review {name}",
                    "quota": (
                        first_quota
                        if check and name == "bright_sun"
                        else target - first_quota
                        if check and name == "shadow"
                        else 0
                    ),
                    "frame_intervals": (
                        [{"start_frame": 0, "end_frame": 99}]
                        if check and name == "bright_sun"
                        else [{"start_frame": 100, "end_frame": 199}]
                        if check and name == "shadow"
                        else []
                    ),
                }
                for name in (
                    "bright_sun",
                    "shadow",
                    "backlight",
                    "twilight",
                    "artificial_light",
                )
            ],
        }
    return request


def _absent() -> dict[str, Any]:
    return {
        "point_source_px": None,
        "bbox_source_px": None,
        "presence": "absent",
        "visibility": "not_applicable",
        "training_use": "background",
        "annotation_state": "confirmed",
        "scale_stratum": "not_applicable",
        "lighting_tag": "bright_sun",
        "motion_occlusion_tags": [],
        "provenance": "manual_human_annotation",
    }


def _check_absent() -> dict[str, Any]:
    return {**_absent(), "training_use": "excluded"}


def _present_box() -> dict[str, Any]:
    return {
        "point_source_px": {"x": 12.0, "y": 12.0},
        "bbox_source_px": {
            "left": 10.0,
            "top": 10.0,
            "right": 14.0,
            "bottom": 14.0,
        },
        "presence": "present",
        "visibility": "visible",
        "training_use": "positive",
        "annotation_state": "confirmed",
        "scale_stratum": "far",
        "lighting_tag": "bright_sun",
        "motion_occlusion_tags": ["ground"],
        "provenance": "manual_human_annotation",
    }


def _confirmed_from_suggestion(suggestion: dict[str, Any], *, shift_x: float = 0.0) -> dict[str, Any]:
    point = deepcopy(suggestion["point_source_px"])
    box = deepcopy(suggestion["bbox_source_px"])
    if point is not None:
        point["x"] += shift_x
    if box is not None:
        box["left"] += shift_x
        box["right"] += shift_x
    return {
        "point_source_px": point,
        "bbox_source_px": box,
        "presence": "present",
        "visibility": suggestion["visibility"],
        "training_use": "positive",
        "annotation_state": "confirmed",
        "scale_stratum": "far",
        "lighting_tag": "bright_sun",
        "motion_occlusion_tags": ["ground"],
        "provenance": "propagation_suggestion_human_confirmed",
    }


def _accept_detector_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "suggestion_kind": "detector_candidate",
        "suggestion_id": candidate["candidate_id"],
        "accepted_suggestion_job_id": candidate["suggestion_job_id"],
        "accepted_suggestion_sha256": candidate["suggestion_sha256"],
    }


def _dismiss_detector_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "dismissed_suggestion_kind": "detector_candidate",
        "dismissed_suggestion_id": candidate["candidate_id"],
        "dismissed_suggestion_job_id": candidate["suggestion_job_id"],
        "dismissed_suggestion_sha256": candidate["suggestion_sha256"],
    }


def _accept_propagation_suggestion(
    suggestion: dict[str, Any],
) -> dict[str, Any]:
    return {
        "suggestion_kind": "propagation",
        "suggestion_id": suggestion["suggestion_id"],
        "accepted_suggestion_job_id": suggestion["suggestion_job_id"],
        "accepted_suggestion_sha256": suggestion["suggestion_sha256"],
    }


def _dismiss_propagation_suggestion(
    suggestion: dict[str, Any],
) -> dict[str, Any]:
    return {
        "dismissed_suggestion_kind": "propagation",
        "dismissed_suggestion_id": suggestion["suggestion_id"],
        "dismissed_suggestion_job_id": suggestion["suggestion_job_id"],
        "dismissed_suggestion_sha256": suggestion["suggestion_sha256"],
    }


class BallAnnotationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        global _ACTIVE_FAKE_AUDIT_BINDINGS
        self.temp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp.name)
        (self.repo_root / "data").mkdir()
        self._audit_patch = patched_audited_t2_probe_bindings()
        audit_bindings = self._audit_patch.__enter__()
        _ACTIVE_FAKE_AUDIT_BINDINGS = audit_bindings
        self.gateway = _FakeProbeGateway()
        self._development_binding_counter = 0
        self.service = BallAnnotationService(
            self.repo_root,
            get_probe=self.gateway.get_probe,
            create_probe=self.gateway.create_probe,
            cancel_propagation_probe=self.gateway.cancel_probe,
            read_probe_artifact=self.gateway.read_probe_artifact,
        )

    def tearDown(self) -> None:
        global _ACTIVE_FAKE_AUDIT_BINDINGS
        self.service.close()
        _ACTIVE_FAKE_AUDIT_BINDINGS = None
        self._audit_patch.__exit__(None, None, None)
        self.temp.cleanup()

    @property
    def _registry_path(self) -> Path:
        return (
            self.repo_root
            / "data"
            / "ball_detector_development_v1"
            / "annotation_sessions"
            / "temporal_group_registry.json"
        )

    @staticmethod
    def _seal_registry(registry: dict[str, Any]) -> dict[str, Any]:
        sealed = deepcopy(registry)
        sealed.pop("registry_sha256", None)
        sealed["entries"].sort(
            key=lambda entry: (
                entry["source_sha256"],
                entry["start_frame"],
                entry["end_frame"],
                entry["group_id"],
                entry["session_id"],
            )
        )
        sealed["registry_sha256"] = canonical_sha256(sealed)
        return sealed

    @staticmethod
    def _coherently_tamper_final_report(result_root: Path) -> None:
        report_path = result_root / "feasibility_report.v1.json"
        manifest_path = result_root / "final_result_manifest.v1.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["authorizations"]["full_run_authorized"] = True
        report["report_sha256"] = canonical_sha256(
            {key: value for key, value in report.items() if key != "report_sha256"}
        )
        report_path.write_text(
            json.dumps(
                report,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        report_bytes = report_path.read_bytes()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(
            {
                "report_file_sha256": hashlib.sha256(report_bytes).hexdigest(),
                "report_file_size_bytes": len(report_bytes),
                "report_sha256": report["report_sha256"],
            }
        )
        manifest["manifest_sha256"] = canonical_sha256(
            {key: value for key, value in manifest.items() if key != "manifest_sha256"}
        )
        manifest_path.write_text(
            json.dumps(
                manifest,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )

    @staticmethod
    def _set_probe_decoder_times(job: dict[str, Any], times_msec: list[float]) -> None:
        report = job["report"]
        if len(times_msec) != len(report["frames"]):
            raise AssertionError("decoder timing fixture length differs")
        for frame, timing, value in zip(
            report["frames"],
            report["decode"]["frame_timing_observations"],
            times_msec,
            strict=True,
        ):
            frame["decoder_reported_pos_msec"] = value
            timing["decoder_reported_pos_msec"] = value
        report["report_sha256"] = canonical_sha256(
            {key: value for key, value in report.items() if key != "report_sha256"}
        )
        _manifest, job["result_manifest_sha256"] = build_detector_probe_result_manifest_authority(report)

    @staticmethod
    def _make_audited_legacy_timing_absent(job: dict[str, Any]) -> dict[str, dict[str, str]]:
        report = job["report"]
        report.pop("review_proxy_manifest", None)
        report["decode"].pop("frame_timing_observations")
        for frame in report["frames"]:
            frame.pop("decoder_reported_pos_msec")
            frame.pop("decoder_timing_observation_method")
        report["report_sha256"] = canonical_sha256(
            {key: value for key, value in report.items() if key != "report_sha256"}
        )
        _manifest, job["result_manifest_sha256"] = build_detector_probe_result_manifest_authority(report)
        return {
            job["job_id"]: {
                "canonical_job_record_sha256": canonical_sha256(job),
                "request_sha256": job["request_sha256"],
                "report_sha256": report["report_sha256"],
                "result_manifest_sha256": job["result_manifest_sha256"],
                "execution_bundle_sha256": report["lineage"]["execution_bundle_sha256"],
                "runtime_environment_sha256": report["lineage"]["runtime_environment_sha256"],
            }
        }

    @staticmethod
    def _attach_review_proxy(
        job: dict[str, Any],
        proxy_times_msec: list[float],
        parent_job: dict[str, Any],
    ) -> None:
        report = job["report"]
        frames = report["frames"]
        if len(proxy_times_msec) != len(frames):
            raise AssertionError("proxy timing fixture length differs")
        job["retry_from_job_id"] = parent_job["job_id"]
        job["frozen_request"]["retry_from_job_id"] = parent_job["job_id"]
        decoder_fingerprint_sha256 = "7" * 64
        report["lineage"]["execution_bundle"] = {
            "execution_environment": {"decoder_fingerprint_sha256": decoder_fingerprint_sha256}
        }
        source = report["source"]
        decode = report["decode"]
        # Current repair children do not invent historical source timing from
        # frame_index/fps.  They bind exact source frame identity and a separate
        # verified proxy CFR timeline.
        proxy_times_msec = [frame["frame_index"] / float(decode["fps"]) * 1000.0 for frame in frames]
        decode["position_verification"] = "verified_review_proxy_frame_index_mapping_v1"
        for frame in frames:
            frame["decoder_reported_pos_msec"] = None
            frame["decoder_timing_observation_method"] = None
        decode["frame_timing_observations"] = None
        frozen = job["frozen_request"]
        parent_report = parent_job["report"]
        parent_result_manifest, parent_result_manifest_sha256 = build_detector_probe_result_manifest_authority(
            parent_report
        )
        parent_job["result_manifest_sha256"] = parent_result_manifest_sha256
        inherited_digests = build_detector_probe_inherited_evidence_authority(parent_report)
        upgrade = {
            "schema_version": "1.0",
            "retry_kind": "review_proxy_decode_upgrade",
            "inherited_evidence": {
                "parent_probe_job_id": job["retry_from_job_id"],
                "parent_probe_request_sha256": parent_job["request_sha256"],
                "parent_probe_intent_sha256": parent_job["intent_sha256"],
                "parent_probe_semantic_intent_sha256": parent_job["semantic_intent_sha256"],
                "parent_probe_report_sha256": parent_report["report_sha256"],
                "parent_probe_result_manifest_sha256": (parent_result_manifest_sha256),
                "parent_probe_record_sha256": canonical_sha256(
                    {
                        "job_id": parent_job["job_id"],
                        "request_sha256": parent_job["request_sha256"],
                        "report_sha256": parent_report["report_sha256"],
                        "result_manifest": parent_result_manifest,
                    }
                ),
                "parent_execution_bundle_sha256": parent_report["lineage"]["execution_bundle_sha256"],
                "parent_runtime_environment_sha256": parent_report["lineage"]["runtime_environment_sha256"],
                **inherited_digests,
            },
            "repair_evidence": {
                "repair_decoder_fingerprint_sha256": decoder_fingerprint_sha256,
            },
        }
        frozen["retry_kind"] = "review_proxy_decode_upgrade"
        frozen["review_proxy_upgrade"] = deepcopy(upgrade)
        job["retry_kind"] = "review_proxy_decode_upgrade"
        job["request_sha256"] = canonical_sha256(frozen)
        job["intent_sha256"] = canonical_sha256(
            {key: value for key, value in frozen.items() if key != "retry_from_job_id"}
        )
        job["semantic_intent_sha256"] = semantic_probe_intent_sha256(frozen)
        report["request_sha256"] = job["request_sha256"]
        report["lineage"]["review_proxy_upgrade"] = deepcopy(upgrade)
        report["lineage"]["retry_kind"] = "review_proxy_decode_upgrade"
        report["lineage"]["intent_sha256"] = job["intent_sha256"]
        report["lineage"]["semantic_intent_sha256"] = job["semantic_intent_sha256"]
        report["review_proxy_manifest"] = build_review_proxy_manifest(
            source={
                "sha256": source["sha256"],
                "file_identity_sha256": source["file_identity_sha256"],
                "size_bytes": source["size_bytes"],
                "width": source["width"],
                "height": source["height"],
                "fps": decode["fps"],
                "frame_count": source["frame_count"],
                "codec": "h264",
            },
            proxy={
                "sha256": "8" * 64,
                "size_bytes": 987,
                "width": source["width"] // 2,
                "height": source["height"] // 2,
                "fps": decode["fps"],
                "frame_count": source["frame_count"],
                "codec": "h264",
            },
            mappings=[
                {
                    "source_frame_index": frame["frame_index"],
                    "source_timing_status": "not_collected",
                    "source_decoder_pos_msec": None,
                    "proxy_frame_index": frame["frame_index"],
                    "proxy_timing_basis": "verified_cfr_frame_index_time_v1",
                    "proxy_cfr_time_msec": proxy_time,
                    "source_frame_sha256": frame["source_frame_sha256"],
                    "proxy_frame_sha256": canonical_sha256(
                        {
                            "proxy_frame_index": frame["frame_index"],
                            "proxy_time_msec": proxy_time,
                        }
                    ),
                    "media_integrity": {
                        "status": "ok",
                        "gray": False,
                        "low_information": False,
                        "likely_corrupt": False,
                    },
                }
                for frame, proxy_time in zip(frames, proxy_times_msec, strict=True)
            ],
            expected_frame_indices=[frame["frame_index"] for frame in frames],
            decoder_fingerprint_sha256=decoder_fingerprint_sha256,
            requested_decode_mode=decode["requested_decode_mode"],
            effective_decode_mode=decode["effective_decode_mode"],
            map_time_tolerance_msec=0.1,
            declared_offset_msec=0.0,
        )
        report["report_sha256"] = canonical_sha256(
            {key: value for key, value in report.items() if key != "report_sha256"}
        )
        _manifest, job["result_manifest_sha256"] = build_detector_probe_result_manifest_authority(report)

    def _eligible_development_result(self, *, development_probe_job_ids: list[str] | None = None) -> dict[str, Any]:
        self._development_binding_counter += 1
        jobs = development_probe_job_ids or ["probe-development"]
        session = self.service.create_session(
            _request(
                development_probe_job_ids=jobs,
                operator_id=(f"operator-development-binding-{self._development_binding_counter}"),
            )
        )
        for index, frame in enumerate(session["frames"]):
            candidate = frame["suggested_candidates"][0]
            self.service.put_annotation(
                session["session_id"],
                frame["frame_index"],
                {
                    "mutation_id": (f"development-binding-{self._development_binding_counter}-{frame['frame_index']}"),
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": _present_box() if index == 0 else _absent(),
                    **(_accept_detector_candidate(candidate) if index == 0 else _dismiss_detector_candidate(candidate)),
                },
                if_match=f'"{frame["annotation_etag"]}"',
            )
        result = self.service.finalize_session(
            session["session_id"],
            f"finalize-development-binding-{self._development_binding_counter}",
        )
        self.assertTrue(result["package"]["may_seed_dataset_expansion"])
        return result

    def _check_request(self, **patch: Any) -> dict[str, Any]:
        patch.pop("data_role", None)
        jobs = patch.get("development_probe_job_ids", ["probe-development"])
        result = self._eligible_development_result(development_probe_job_ids=jobs)
        return _request(
            data_role="check",
            development_package_session_id=result["package"]["session_id"],
            development_package_sha256=result["package"]["package_sha256"],
            **patch,
        )

    def _assert_check_probe_authorization_tamper_fails_before_job(
        self,
        *,
        operator_id: str,
        tamper: Callable[[str], None],
        expected_code: str,
    ) -> None:
        request = self._check_request(
            development_probe_job_ids=["probe-development"],
            operator_id=operator_id,
        )
        observed_session_ids: list[str] = []

        def reject_before_job(probe_request: dict[str, Any]) -> None:
            self.assertEqual([], self.gateway.create_requests)
            self.assertEqual({}, self.gateway.jobs_by_request_sha256)
            session_id = probe_request["_annotation_session_id"]
            observed_session_ids.append(session_id)
            tamper(session_id)
            self.service.authorize_check_probe_creation(session_id)

        self.gateway.on_create = reject_before_job
        try:
            with self.assertRaises(BallAnnotationServiceError) as rejected:
                self.service.create_session(request)
        finally:
            self.gateway.on_create = None
        self.assertEqual(expected_code, rejected.exception.code)
        self.assertEqual(1, len(observed_session_ids))
        self.assertEqual([], self.gateway.create_requests)
        self.assertEqual({}, self.gateway.jobs_by_request_sha256)

    @staticmethod
    def _retry_request(previous: dict[str, Any], **patch: Any) -> dict[str, Any]:
        return _request(
            data_role="check",
            development_package_session_id=previous["development_package_binding"]["session_id"],
            development_package_sha256=previous["development_package_binding"]["package_sha256"],
            retry_from_session_id=previous["session_id"],
            **patch,
        )

    def _ready_propagation(
        self,
        *,
        service: BallAnnotationService | None = None,
        gateway: _FakeProbeGateway | None = None,
        mutation_suffix: str = "default",
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        service = service or self.service
        gateway = gateway or self.gateway
        session = service.create_session(
            _request(
                development_probe_job_ids=["probe-development"],
                operator_id=f"operator-{mutation_suffix}",
            )
        )
        seed = next(frame for frame in session["frames"] if frame["frame_index"] == 40)
        revision = service.put_annotation(
            session["session_id"],
            seed["frame_index"],
            {
                "mutation_id": f"seed-{mutation_suffix}",
                "expected_revision": 0,
                "operation": "set",
                "undo_revision": None,
                "annotation": _present_box(),
                **_accept_detector_candidate(seed["suggested_candidates"][0]),
            },
            if_match=f'"{seed["annotation_etag"]}"',
        )
        queued = service.create_propagation_job(
            session["session_id"],
            {
                "mutation_id": f"propagate-{mutation_suffix}",
                "seed_frame_index": 40,
                "radius_frames": 2,
                "expected_seed_revision": 1,
            },
            if_match=f'"{revision["annotation_etag"]}"',
        )
        gateway.complete(queued["neighbor_probe_job_id"])
        ready = service.get_propagation_job(session["session_id"], queued["job_id"])
        return session, seed, revision, ready

    def _capacity_session(
        self,
        *,
        seed_count: int,
        suffix: str,
    ) -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
        probe_job_id = f"probe-capacity-{suffix}"
        frame_indices = [5 + 9 * index for index in range(seed_count)]
        self.gateway.jobs[probe_job_id] = self.gateway._ready_job(probe_job_id, frame_indices)
        session = self.service.create_session(
            _request(
                development_probe_job_ids=[probe_job_id],
                operator_id=f"operator-capacity-{suffix}",
            )
        )
        revisions: dict[int, dict[str, Any]] = {}
        for frame in session["frames"]:
            revisions[frame["frame_index"]] = self.service.put_annotation(
                session["session_id"],
                frame["frame_index"],
                {
                    "mutation_id": f"capacity-seed-{suffix}-{frame['frame_index']}",
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": _present_box(),
                },
                if_match=f'"{frame["annotation_etag"]}"',
            )
        return session, revisions

    def _queue_capacity_propagation(
        self,
        session: dict[str, Any],
        revisions: dict[int, dict[str, Any]],
        frame_index: int,
        *,
        mutation_id: str,
    ) -> dict[str, Any]:
        return self.service.create_propagation_job(
            session["session_id"],
            {
                "mutation_id": mutation_id,
                "seed_frame_index": frame_index,
                "radius_frames": 1,
                "expected_seed_revision": 1,
            },
            if_match=f'"{revisions[frame_index]["annotation_etag"]}"',
        )

    def test_development_session_aggregates_same_source_retry_once_and_recovers_after_refresh(self) -> None:
        created = self.service.create_session(_request())
        self.assertEqual("annotating", created["status"])
        self.assertEqual(6, len(created["frames"]))
        self.assertEqual(6, created["sampling_manifest"]["target_frame_count"])
        self.assertEqual(6, len({frame["frame_index"] for frame in created["frames"]}))
        self.assertTrue(
            all(
                candidate["annotation_state"] == "suggested"
                for frame in created["frames"]
                for candidate in frame["suggested_candidates"]
            )
        )
        self.assertTrue(
            all(
                candidate["training_use"] == "excluded"
                for frame in created["frames"]
                for candidate in frame["suggested_candidates"]
            )
        )
        first_frame = created["frames"][0]
        self.assertEqual(-50.0, first_frame["decoder_reported_pos_msec"])
        self.assertEqual(-0.05, first_frame["decoder_time_seconds"])
        self.assertEqual(0.0, first_frame["display_time_seconds"])
        self.assertNotIn("source_artifact_url", json.dumps(created))

        refreshed_service = BallAnnotationService(
            self.repo_root,
            get_probe=self.gateway.get_probe,
            create_probe=self.gateway.create_probe,
            read_probe_artifact=self.gateway.read_probe_artifact,
        )
        try:
            refreshed = refreshed_service.get_session(created["session_id"])
        finally:
            refreshed_service.close()
        self.assertEqual(created, refreshed)

    def test_review_proxy_primary_authority_rejects_role_and_identity_bypasses(self) -> None:
        created = self.service.create_session(_request(development_probe_job_ids=["probe-development"]))
        stored = self.service._load_session(created["session_id"])
        primary = stored["frames"][0]

        false_primary = deepcopy(stored)
        false_primary["frames"][0]["primary_sample"] = False
        false_primary["frames"][0]["frame_role"] = "propagation_target"

        missing_primary = deepcopy(stored)
        missing_primary["frames"][0].pop("primary_sample")

        duplicate = deepcopy(stored)
        duplicate["frames"].append(deepcopy(primary))

        false_with_true_clone = deepcopy(false_primary)
        false_with_true_clone["frames"].append(deepcopy(primary))

        for name, forged in (
            ("false_primary", false_primary),
            ("missing_primary", missing_primary),
            ("duplicate", duplicate),
            ("false_with_true_clone", false_with_true_clone),
        ):
            with self.subTest(name=name), self.assertRaises(BallAnnotationServiceError) as rejected:
                self.service._review_proxy_primary_frames(forged)
            self.assertEqual("replacement_session_mismatch", rejected.exception.code)

    def test_supplemental_frames_do_not_publish_detector_candidates(self) -> None:
        _session, _seed, _revision, ready = self._ready_propagation(mutation_suffix="supplemental-detector-boundary")
        current = self.service.get_session(ready["session_id"])
        supplemental = next(frame for frame in current["frames"] if frame["frame_role"] == "propagation_target")
        self.assertEqual([], supplemental["suggested_candidates"])
        self.assertTrue(supplemental["propagation_suggestions"])

        with self.assertRaises(BallAnnotationServiceError) as rejected:
            self.service.put_annotation(
                current["session_id"],
                supplemental["frame_index"],
                {
                    "mutation_id": "reject-supplemental-detector-candidate",
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": _present_box(),
                    "suggestion_kind": "detector_candidate",
                    "suggestion_id": "suggestion-not-bound",
                    "accepted_suggestion_job_id": ready["neighbor_probe_job_id"],
                    "accepted_suggestion_sha256": "a" * 64,
                },
                if_match=f'"{supplemental["annotation_etag"]}"',
            )
        self.assertEqual("suggestion_not_found", rejected.exception.code)

        suggestion = supplemental["propagation_suggestions"][0]
        confirmed = self.service.put_annotation(
            current["session_id"],
            supplemental["frame_index"],
            {
                "mutation_id": "confirm-supplemental-propagation",
                "expected_revision": 0,
                "operation": "set",
                "undo_revision": None,
                "annotation": {
                    **_present_box(),
                    "provenance": "propagation_suggestion_human_confirmed",
                },
                **_accept_propagation_suggestion(suggestion),
            },
            if_match=f'"{supplemental["annotation_etag"]}"',
        )
        self.assertEqual(1, confirmed["revision"])
        stored = self.service._load_session(current["session_id"])
        detector_evidence = self.service._build_detector_candidate_evidence(stored)
        self.assertNotIn(
            supplemental["frame_index"],
            {row["frame_index"] for row in detector_evidence},
        )

    def test_session_rejects_client_authority_non_ready_and_source_mismatch(self) -> None:
        for field, value in (
            ("source_sha256", "f" * 64),
            ("frame_indices", [1, 2]),
            ("check_probe_job_id", "probe-forged"),
            ("candidate_artifact_url", "/forged"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(BallAnnotationServiceError, "client authority"):
                self.service.create_session(_request(**{field: value}))

        self.gateway.jobs["probe-running"] = {"job_id": "probe-running", "status": "running", "report": None}
        with self.assertRaisesRegex(BallAnnotationServiceError, "ready"):
            self.service.create_session(_request(development_probe_job_ids=["probe-running"]))

        self.gateway.jobs["probe-other-source"] = self.gateway._ready_job(
            "probe-other-source",
            [100],
            retry_from="probe-development",
            source_sha256="f" * 64,
        )
        with self.assertRaisesRegex(BallAnnotationServiceError, "source, decode, profile"):
            self.service.create_session(_request(development_probe_job_ids=["probe-development", "probe-other-source"]))

    def test_check_locks_unseen_groups_before_server_creates_probe_then_retires_all_profiles(self) -> None:
        observed_lock: list[dict[str, Any]] = []

        def assert_lock_precedes_probe(request: dict[str, Any]) -> None:
            files = list(
                (self.repo_root / "data" / "ball_detector_development_v1" / "annotation_sessions" / "sessions").glob(
                    "*.json"
                )
            )
            persisted_sessions = [json.loads(path.read_text(encoding="utf-8")) for path in files]
            persisted = next(item for item in persisted_sessions if item["data_role"] == "check")
            self.assertEqual("sampling_locked", persisted["status"])
            self.assertIsNone(persisted["check_probe_job_id"])
            self.assertEqual(20, len(persisted["sampling_manifest"]["groups"]))
            authority = self.service.authorize_check_probe_creation(persisted["session_id"])
            self.assertEqual(persisted["session_id"], request["_annotation_session_id"])
            self.assertEqual(
                persisted["sampling_manifest"]["manifest_sha256"],
                request["annotation_sampling_manifest_sha256"],
            )
            self.assertEqual(persisted["sampling_manifest"]["frame_indices"], request["frame_indices"])
            self.assertEqual(persisted["session_id"], authority["session_id"])
            self.assertEqual(
                persisted["sampling_manifest"]["manifest_sha256"],
                authority["sampling_manifest_sha256"],
            )
            self.assertEqual(persisted["sampling_manifest"]["frame_indices"], authority["frame_indices"])
            observed_lock.append(persisted)

        self.gateway.on_create = assert_lock_precedes_probe
        created = self.service.create_session(self._check_request(development_probe_job_ids=["probe-development"]))
        self.assertEqual("check_probe_queued", created["status"])
        self.assertEqual(
            "tiny_ball_temporal_block_hash_v1",
            created["sampling_manifest"]["selection_profile_id"],
        )
        self.assertEqual(
            "predeclared_frame_intervals_and_quota_v1",
            created["sampling_manifest"]["lighting_stratification_mode"],
        )
        self.assertEqual(
            created["sampling_manifest"]["selection_seed_sha256"],
            canonical_sha256(created["sampling_manifest"]["selection_authority"]),
        )
        self.assertEqual(
            {"bright_sun": 10, "shadow": 10},
            dict(
                sorted(
                    {
                        name: sum(
                            group["pre_reveal_lighting_stratum"] == name
                            for group in created["sampling_manifest"]["groups"]
                        )
                        for name in ("bright_sun", "shadow")
                    }.items()
                )
            ),
        )
        self.assertEqual(1, len(observed_lock))
        probe_request = self.gateway.create_requests[0]
        self.assertEqual(
            sorted([LOCKED_PROFILE, CONTROL_PROFILE]),
            probe_request["profile_ids"],
        )
        self.assertEqual(observed_lock[0]["session_id"], probe_request["_annotation_session_id"])
        self.assertEqual(
            observed_lock[0]["sampling_manifest"]["manifest_sha256"],
            probe_request["annotation_sampling_manifest_sha256"],
        )
        self.assertEqual(
            observed_lock[0]["sampling_manifest"]["frame_indices"],
            probe_request["frame_indices"],
        )
        development_groups = observed_lock[0]["sampling_manifest"]["excluded_development_groups"]
        for check_group in observed_lock[0]["sampling_manifest"]["groups"]:
            self.assertTrue(
                all(
                    check_group["end_frame"] < group["start_frame"] or group["end_frame"] < check_group["start_frame"]
                    for group in development_groups
                )
            )

        job_id = created["check_probe_job_id"]
        self.gateway.complete(job_id)
        ready = self.service.get_session(created["session_id"])
        self.assertEqual("annotating", ready["status"])
        self.assertEqual(20, len(ready["frames"]))
        registry = json.loads(
            (
                self.repo_root
                / "data"
                / "ball_detector_development_v1"
                / "annotation_sessions"
                / "temporal_group_registry.json"
            ).read_text(encoding="utf-8")
        )
        session_entries = [entry for entry in registry["entries"] if entry["session_id"] == created["session_id"]]
        self.assertTrue(session_entries)
        self.assertTrue(all(entry["state"] == "revealed" for entry in session_entries))
        self.assertTrue(all(entry["retired_for_all_profiles"] is True for entry in session_entries))

    def test_check_samples_complete_source_when_development_frames_are_clustered(self) -> None:
        job = self.gateway._ready_job(
            "probe-clustered-development",
            [1500, 1560, 1620, 1680, 1740, 1799],
        )
        job["report"]["source"]["frame_count"] = 104_820
        job["report"]["decode"]["frame_count"] = 104_820
        job["report"]["report_sha256"] = canonical_sha256(
            {key: value for key, value in job["report"].items() if key != "report_sha256"}
        )
        _manifest, job["result_manifest_sha256"] = build_detector_probe_result_manifest_authority(job["report"])
        self.gateway.jobs["probe-clustered-development"] = job
        applicability = _request(data_role="check")["strata_applicability"]
        applicability["lighting"][0]["frame_intervals"] = [{"start_frame": 0, "end_frame": 52_409}]
        applicability["lighting"][1]["frame_intervals"] = [{"start_frame": 52_410, "end_frame": 104_819}]
        created = self.service.create_session(
            self._check_request(
                development_probe_job_ids=["probe-clustered-development"],
                strata_applicability=applicability,
            )
        )
        manifest = created["sampling_manifest"]
        self.assertEqual(0, manifest["candidate_universe_start_frame"])
        self.assertEqual(104_819, manifest["candidate_universe_end_frame"])
        self.assertEqual(
            104_820,
            manifest["candidate_universe_authority"]["candidate_frame_count"],
        )
        self.assertNotIn("candidate_frame_indices", manifest["candidate_universe_authority"])
        self.assertTrue(any(frame < 1500 or frame > 1799 for frame in manifest["frame_indices"]))
        probe_request = self.gateway.create_requests[-1]
        self.assertEqual(created["session_id"], probe_request["_annotation_session_id"])
        self.assertEqual(
            manifest["manifest_sha256"],
            probe_request["annotation_sampling_manifest_sha256"],
        )
        self.assertEqual(manifest["frame_indices"], probe_request["frame_indices"])

    def test_check_probe_authorization_rejects_tampered_sampling_lock_before_job(self) -> None:
        def tamper(session_id: str) -> None:
            path = (
                self.repo_root
                / "data"
                / "ball_detector_development_v1"
                / "annotation_sessions"
                / "sampling_locks"
                / f"{session_id}.json"
            )
            sampling_lock = json.loads(path.read_text(encoding="utf-8"))
            sampling_lock["lock_sha256"] = "f" * 64
            path.write_text(
                json.dumps(sampling_lock, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )

        self._assert_check_probe_authorization_tamper_fails_before_job(
            operator_id="operator-check-authority-lock-tamper",
            tamper=tamper,
            expected_code="sampling_lock_conflict",
        )

    def test_check_probe_authorization_rejects_tampered_manifest_before_job(self) -> None:
        def tamper(session_id: str) -> None:
            path = (
                self.repo_root
                / "data"
                / "ball_detector_development_v1"
                / "annotation_sessions"
                / "sessions"
                / f"{session_id}.json"
            )
            session = json.loads(path.read_text(encoding="utf-8"))
            session["sampling_manifest"]["frame_indices"][0] += 1
            path.write_text(
                json.dumps(session, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )

        self._assert_check_probe_authorization_tamper_fails_before_job(
            operator_id="operator-check-authority-manifest-tamper",
            tamper=tamper,
            expected_code="invalid_check_probe_authority",
        )

    def test_check_probe_authorization_rejects_tampered_registry_before_job(self) -> None:
        def tamper(session_id: str) -> None:
            registry = json.loads(self._registry_path.read_text(encoding="utf-8"))
            for entry in registry["entries"]:
                if entry["session_id"] == session_id:
                    entry["session_id"] = "annotation-foreign-reservation"
            registry = self._seal_registry(registry)
            self._registry_path.write_text(
                json.dumps(registry, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )

        self._assert_check_probe_authorization_tamper_fails_before_job(
            operator_id="operator-check-authority-registry-tamper",
            tamper=tamper,
            expected_code="check_probe_reservation_mismatch",
        )

    def test_sampling_selection_authority_excludes_operator_and_free_text(self) -> None:
        request = _request(data_role="check")
        first = BallAnnotationService._sampling_selection_authority(
            attempt_family_sha256="f" * 64,
            development_package_sha256="e" * 64,
            source_sha256="a" * 64,
            locked_profile=_profile(LOCKED_PROFILE, "d"),
            request=request,
        )
        changed_free_text = deepcopy(request)
        changed_free_text["operator_id"] = "another-operator"
        for row in changed_free_text["strata_applicability"]["scale"]:
            row["evidence_note"] = f"different free text {row['stratum']}"
        for row in changed_free_text["strata_applicability"]["lighting"]:
            row["evidence_note"] = f"different free text {row['stratum']}"
        second = BallAnnotationService._sampling_selection_authority(
            attempt_family_sha256="f" * 64,
            development_package_sha256="e" * 64,
            source_sha256="a" * 64,
            locked_profile=_profile(LOCKED_PROFILE, "d"),
            request=changed_free_text,
        )
        self.assertEqual(first, second)
        self.assertNotIn("operator", json.dumps(first))
        self.assertNotIn("evidence_note", json.dumps(first))
        first_groups = sample_unseen_temporal_groups(
            source_sha256="a" * 64,
            candidate_frame_indices=range(200),
            target_count=20,
            excluded_group_ids=set(),
            reserved_group_ids=set(),
            seed=canonical_sha256(first),
            lighting_strata=[row for row in request["strata_applicability"]["lighting"] if row["quota"] > 0],
        )
        second_groups = sample_unseen_temporal_groups(
            source_sha256="a" * 64,
            candidate_frame_indices=range(200),
            target_count=20,
            excluded_group_ids=set(),
            reserved_group_ids=set(),
            seed=canonical_sha256(second),
            lighting_strata=[row for row in changed_free_text["strata_applicability"]["lighting"] if row["quota"] > 0],
        )
        self.assertEqual(first_groups, second_groups)

        changed_sampling = deepcopy(request)
        changed_sampling["strata_applicability"]["lighting"][0]["quota"] = 8
        changed_sampling["strata_applicability"]["lighting"][1]["quota"] = 12
        changed = BallAnnotationService._sampling_selection_authority(
            attempt_family_sha256="f" * 64,
            development_package_sha256="e" * 64,
            source_sha256="a" * 64,
            locked_profile=_profile(LOCKED_PROFILE, "d"),
            request=changed_sampling,
        )
        self.assertNotEqual(canonical_sha256(first), canonical_sha256(changed))
        changed_package = BallAnnotationService._sampling_selection_authority(
            attempt_family_sha256="f" * 64,
            development_package_sha256="0" * 64,
            source_sha256="a" * 64,
            locked_profile=_profile(LOCKED_PROFILE, "d"),
            request=request,
        )
        self.assertNotEqual(canonical_sha256(first), canonical_sha256(changed_package))

    def test_one_development_package_and_selection_authority_allow_only_one_original_check(self) -> None:
        request = self._check_request(
            development_probe_job_ids=["probe-development"],
            operator_id="operator-one-time-check",
        )
        created = self.service.create_session(request)
        repeated = self.service.create_session(deepcopy(request))
        self.assertEqual(created["session_id"], repeated["session_id"])
        session_path = (
            self.repo_root
            / "data"
            / "ball_detector_development_v1"
            / "annotation_sessions"
            / "sessions"
            / f"{created['session_id']}.json"
        )

        for status in ("check_probe_queued", "finalized", "blocked"):
            stored = json.loads(session_path.read_text(encoding="utf-8"))
            stored["status"] = status
            stored["stage"] = status
            if status == "blocked":
                stored["error_code"] = "synthetic_infrastructure_failure"
                stored["blocker_code"] = None
            session_path.write_text(json.dumps(stored), encoding="utf-8")
            changed = deepcopy(request)
            changed["operator_id"] = f"operator-conflict-{status}"
            for row in changed["strata_applicability"]["scale"]:
                row["evidence_note"] = f"changed note {status} {row['stratum']}"
            with self.subTest(status=status), self.assertRaises(BallAnnotationServiceError) as conflict:
                self.service.create_session(changed)
            self.assertEqual("check_attempt_already_exists", conflict.exception.code)

        retry = self.service.create_session(
            self._retry_request(
                created,
                development_probe_job_ids=["probe-development"],
                operator_id="operator-explicit-check-retry",
            )
        )
        self.assertEqual(created["session_id"], retry["retry_from_session_id"])
        self.assertEqual(created["sampling_manifest"], retry["sampling_manifest"])

    def test_registry_is_digest_bound_exact_and_rejects_corruption_overlap_and_duplicates(self) -> None:
        created = self.service.create_session(_request(development_probe_job_ids=["probe-development"]))
        original = json.loads(self._registry_path.read_text(encoding="utf-8"))
        self.assertEqual(
            original["registry_sha256"],
            canonical_sha256({key: value for key, value in original.items() if key != "registry_sha256"}),
        )

        corrupted = deepcopy(original)
        corrupted["entries"][0]["updated_at"] += "tampered"
        self._registry_path.write_text(json.dumps(corrupted), encoding="utf-8")
        with self.assertRaises(BallAnnotationServiceError) as digest_error:
            self.service._read_registry()
        self.assertEqual("invalid_group_registry", digest_error.exception.code)

        mutations: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
            ("extra-field", lambda value: value.update({"forged": True})),
            (
                "retirement-state",
                lambda value: value["entries"][0].update({"state": "reserved"}),
            ),
            (
                "development-scored",
                lambda value: value["entries"][0].update({"state": "scored"}),
            ),
            (
                "canonical-span",
                lambda value: value["entries"][0].update({"start_frame": value["entries"][0]["start_frame"] + 1}),
            ),
            (
                "duplicate",
                lambda value: value["entries"].append(deepcopy(value["entries"][0])),
            ),
            (
                "unsafe-session",
                lambda value: value["entries"][0].update({"session_id": "../unsafe"}),
            ),
        ]
        for label, mutate in mutations:
            with self.subTest(label=label):
                value = deepcopy(original)
                mutate(value)
                value = self._seal_registry(value)
                self._registry_path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(BallAnnotationServiceError) as invalid:
                    self.service._read_registry()
                self.assertEqual("invalid_group_registry", invalid.exception.code)

        legacy = deepcopy(original)
        legacy.pop("registry_sha256")
        self._registry_path.write_text(json.dumps(legacy), encoding="utf-8")
        with self.assertRaises(BallAnnotationServiceError) as migration:
            self.service._read_registry()
        self.assertEqual("group_registry_migration_required", migration.exception.code)

        same_session_overlap = deepcopy(original)
        canonical = temporal_group_for_frame(created["source"]["sha256"], 1)
        template = same_session_overlap["entries"][0]
        same_session_overlap["entries"].append(
            {
                **canonical,
                "frame_index": 1,
                "session_id": template["session_id"],
                "data_role": template["data_role"],
                "state": template["state"],
                "retired_for_all_profiles": template["retired_for_all_profiles"],
                "created_at": template["created_at"],
                "updated_at": template["updated_at"],
            }
        )
        same_session_overlap = self._seal_registry(same_session_overlap)
        self._registry_path.write_text(json.dumps(same_session_overlap), encoding="utf-8")
        self.assertEqual(
            same_session_overlap["registry_sha256"],
            self.service._read_registry()["registry_sha256"],
        )

        overlapping = deepcopy(original)
        canonical = temporal_group_for_frame(created["source"]["sha256"], 1)
        template = overlapping["entries"][0]
        overlapping["entries"].append(
            {
                **canonical,
                "frame_index": 1,
                "session_id": "overlap-session",
                "data_role": "development",
                "state": "revealed",
                "retired_for_all_profiles": True,
                "created_at": template["created_at"],
                "updated_at": template["updated_at"],
            }
        )
        overlapping = self._seal_registry(overlapping)
        self._registry_path.write_text(json.dumps(overlapping), encoding="utf-8")
        with self.assertRaises(BallAnnotationServiceError) as overlap_error:
            self.service._read_registry()
        self.assertEqual("invalid_group_registry", overlap_error.exception.code)
        self._registry_path.write_text(json.dumps(original), encoding="utf-8")

    def test_registry_transition_is_monotonic_and_revealed_orphans_are_never_discarded(self) -> None:
        created = self.service.create_session(
            self._check_request(
                development_probe_job_ids=["probe-development"],
                operator_id="operator-registry-transition",
            )
        )
        self.service._transition_session_groups(created["session_id"], "revealed")
        self.service._transition_session_groups(created["session_id"], "scored")
        with self.assertRaises(BallAnnotationServiceError) as regression:
            self.service._transition_session_groups(created["session_id"], "revealed")
        self.assertEqual("invalid_group_transition", regression.exception.code)
        session_path = (
            self.repo_root
            / "data"
            / "ball_detector_development_v1"
            / "annotation_sessions"
            / "sessions"
            / f"{created['session_id']}.json"
        )
        session_path.unlink()
        self.service.close()
        self.service = BallAnnotationService(
            self.repo_root,
            get_probe=self.gateway.get_probe,
            create_probe=self.gateway.create_probe,
            read_probe_artifact=self.gateway.read_probe_artifact,
        )
        registry = json.loads(self._registry_path.read_text(encoding="utf-8"))
        entries = [entry for entry in registry["entries"] if entry["session_id"] == created["session_id"]]
        self.assertTrue(entries)
        self.assertTrue(all(entry["state"] == "scored" for entry in entries))

    def test_registry_allows_same_session_overlap_but_rejects_cross_session_overlap(self) -> None:
        source_sha256 = "a" * 64
        same_session_groups = [
            {
                **temporal_group_for_frame(source_sha256, frame_index),
                "frame_index": frame_index,
            }
            for frame_index in (0, 1)
        ]
        self.assertGreaterEqual(
            same_session_groups[0]["end_frame"],
            same_session_groups[1]["start_frame"],
        )
        self.service._record_groups(
            "same-session-overlap",
            source_sha256,
            same_session_groups,
            data_role="development",
            state="revealed",
        )
        registry = self.service._read_registry()
        self.assertEqual(
            2,
            sum(entry["session_id"] == "same-session-overlap" for entry in registry["entries"]),
        )
        cross_session = {
            **temporal_group_for_frame(source_sha256, 2),
            "frame_index": 2,
        }
        with self.assertRaises(BallAnnotationServiceError) as conflict:
            self.service._record_groups(
                "cross-session-overlap",
                source_sha256,
                [cross_session],
                data_role="development",
                state="revealed",
            )
        self.assertEqual("temporal_group_conflict", conflict.exception.code)

    def test_blocked_check_retry_clears_old_authority_until_new_probe_is_ready(self) -> None:
        created = self.service.create_session(
            self._check_request(
                development_probe_job_ids=["probe-development"],
                operator_id="operator-blocked-check",
            )
        )
        old_probe_job_id = created["check_probe_job_id"]
        self.gateway.jobs[old_probe_job_id].update({"status": "failed", "error_code": "synthetic_probe_failure"})
        blocked = self.service.get_session(created["session_id"])
        self.assertEqual("blocked", blocked["status"])

        # Simulate a legacy/partial blocked record that retained old revealed
        # authority.  The retry must not expose it as its own probe authority.
        session_path = (
            self.repo_root
            / "data"
            / "ball_detector_development_v1"
            / "annotation_sessions"
            / "sessions"
            / f"{created['session_id']}.json"
        )
        stored = json.loads(session_path.read_text(encoding="utf-8"))
        stored["check_probe_authority"] = {
            "job_id": "old-revealed-probe",
            "request_sha256": "1" * 64,
            "intent_sha256": "2" * 64,
            "result_manifest_sha256": "3" * 64,
            "report_sha256": "4" * 64,
            "parent_trial_id": "production_trial_one",
            "runtime_environment_sha256": "5" * 64,
            "execution_bundle_sha256": "6" * 64,
            "frozen_profiles_sha256": "7" * 64,
            "locked_profile": deepcopy(blocked["locked_profile"]),
            "control_profile": deepcopy(blocked["control_profile"]),
        }
        session_path.write_text(json.dumps(stored), encoding="utf-8")

        changed_authority = self._retry_request(
            created,
            development_probe_job_ids=["probe-development"],
            operator_id="operator-blocked-check-retry-changed",
        )
        changed_authority["strata_applicability"]["lighting"][0]["frame_intervals"] = [
            {"start_frame": 0, "end_frame": 89}
        ]
        changed_authority["strata_applicability"]["lighting"][1]["frame_intervals"] = [
            {"start_frame": 90, "end_frame": 199}
        ]
        with self.assertRaises(BallAnnotationServiceError) as changed:
            self.service.create_session(changed_authority)
        self.assertEqual("retry_lineage_mismatch", changed.exception.code)

        retry = self.service.create_session(
            self._retry_request(
                created,
                development_probe_job_ids=["probe-development"],
                operator_id="operator-blocked-check-retry",
            )
        )
        self.assertIn(retry["status"], {"check_probe_queued", "check_probe_running"})
        self.assertIsNone(retry["check_probe_authority"])
        self.assertNotEqual(old_probe_job_id, retry["check_probe_job_id"])
        active = self.service.get_session(retry["session_id"])
        self.assertIsNone(active["check_probe_authority"])

        self.gateway.complete(retry["check_probe_job_id"])
        replacement = self.gateway.jobs[retry["check_probe_job_id"]]
        _set_job_runtime_execution_authority(
            replacement,
            runtime_environment_sha256="9" * 64,
            execution_bundle_sha256="8" * 64,
        )
        ready = self.service.get_session(retry["session_id"])
        self.assertEqual("annotating", ready["status"])
        self.assertEqual(retry["check_probe_job_id"], ready["check_probe_authority"]["job_id"])
        self.assertEqual(
            "9" * 64,
            ready["check_probe_authority"]["runtime_environment_sha256"],
        )
        self.assertEqual(
            "8" * 64,
            ready["check_probe_authority"]["execution_bundle_sha256"],
        )
        self.assertEqual(created["attempt_family_sha256"], ready["attempt_family_sha256"])
        self.assertNotEqual("old-revealed-probe", ready["check_probe_authority"]["job_id"])
        self.assertEqual(blocked["sampling_manifest"], ready["sampling_manifest"])
        for frame in ready["frames"]:
            candidate = frame["suggested_candidates"][0]
            self.service.put_annotation(
                ready["session_id"],
                frame["frame_index"],
                {
                    "mutation_id": f"changed-runtime-check-{frame['frame_index']}",
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": _check_absent(),
                    **_dismiss_detector_candidate(candidate),
                },
                if_match=f'"{frame["annotation_etag"]}"',
            )
        finalized = self.service.finalize_session(
            ready["session_id"],
            "finalize-changed-runtime-check",
        )
        package = finalized["package"]
        self.assertEqual(
            "9" * 64,
            package["check_probe_authority"]["runtime_environment_sha256"],
        )
        self.assertTrue(
            all(
                row["timing_binding"]["runtime_environment_sha256"] == "9" * 64
                for row in verify_frame_evidence_package(package)
            )
        )

    def test_retry_setup_failure_restores_previous_reservations_exactly(self) -> None:
        created = self.service.create_session(
            self._check_request(
                development_probe_job_ids=["probe-development"],
                operator_id="operator-retry-rollback-source",
            )
        )
        self.gateway.jobs[created["check_probe_job_id"]].update(
            {"status": "failed", "error_code": "synthetic_probe_failure"}
        )
        blocked = self.service.get_session(created["session_id"])
        self.assertEqual("blocked", blocked["status"])
        registry_path = (
            self.repo_root
            / "data"
            / "ball_detector_development_v1"
            / "annotation_sessions"
            / "temporal_group_registry.json"
        )
        before = json.loads(registry_path.read_text(encoding="utf-8"))
        with (
            patch.object(
                self.service,
                "_persist_sampling_lock",
                side_effect=OSError("synthetic lock publication failure"),
            ),
            self.assertRaises(OSError),
        ):
            self.service.create_session(
                self._retry_request(
                    created,
                    development_probe_job_ids=["probe-development"],
                    operator_id="operator-retry-rollback-target",
                )
            )
        self.assertEqual(before, json.loads(registry_path.read_text(encoding="utf-8")))
        session_files = list(
            (self.repo_root / "data" / "ball_detector_development_v1" / "annotation_sessions" / "sessions").glob(
                "*.json"
            )
        )
        check_session_ids = [
            path.stem for path in session_files if json.loads(path.read_text(encoding="utf-8"))["data_role"] == "check"
        ]
        self.assertEqual([created["session_id"]], check_session_ids)

    def test_retry_setup_restart_rolls_back_before_lock_and_replays_after_lock(self) -> None:
        created = self.service.create_session(
            self._check_request(
                development_probe_job_ids=["probe-development"],
                operator_id="operator-retry-restart-source",
            )
        )
        self.gateway.jobs[created["check_probe_job_id"]].update(
            {"status": "failed", "error_code": "synthetic_probe_failure"}
        )
        self.service.get_session(created["session_id"])
        registry_path = (
            self.repo_root
            / "data"
            / "ball_detector_development_v1"
            / "annotation_sessions"
            / "temporal_group_registry.json"
        )
        before = json.loads(registry_path.read_text(encoding="utf-8"))

        def crash_before_lock(stage: str) -> None:
            if stage == "after_session_persist":
                raise RuntimeError("synthetic process death before lock")

        self.service._session_setup_failpoint = crash_before_lock
        with (
            patch.object(self.service, "_discard_unstarted_session", return_value=None),
            self.assertRaises(RuntimeError),
        ):
            self.service.create_session(
                self._retry_request(
                    created,
                    development_probe_job_ids=["probe-development"],
                    operator_id="operator-retry-restart-before-lock",
                )
            )
        self.service._session_setup_failpoint = None
        recovered = BallAnnotationService(
            self.repo_root,
            get_probe=self.gateway.get_probe,
            create_probe=self.gateway.create_probe,
            read_probe_artifact=self.gateway.read_probe_artifact,
        )
        recovered.close()
        self.assertEqual(before, json.loads(registry_path.read_text(encoding="utf-8")))

        def crash_after_lock(stage: str) -> None:
            if stage == "after_sampling_lock_persist":
                raise RuntimeError("synthetic process death after lock")

        self.service._session_setup_failpoint = crash_after_lock
        with (
            patch.object(self.service, "_discard_unstarted_session", return_value=None),
            self.assertRaises(RuntimeError),
        ):
            self.service.create_session(
                self._retry_request(
                    created,
                    development_probe_job_ids=["probe-development"],
                    operator_id="operator-retry-restart-after-lock",
                )
            )
        self.service._session_setup_failpoint = None
        sessions_root = self.repo_root / "data" / "ball_detector_development_v1" / "annotation_sessions" / "sessions"
        retry_ids = [
            path.stem
            for path in sessions_root.glob("*.json")
            if path.stem != created["session_id"]
            and json.loads(path.read_text(encoding="utf-8"))["data_role"] == "check"
        ]
        self.assertEqual(1, len(retry_ids))
        recovered = BallAnnotationService(
            self.repo_root,
            get_probe=self.gateway.get_probe,
            create_probe=self.gateway.create_probe,
            read_probe_artifact=self.gateway.read_probe_artifact,
        )
        recovered.close()
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        retry_entries = [
            entry
            for entry in registry["entries"]
            if entry["source_sha256"] == created["source"]["sha256"]
            and entry["group_id"] in {group["group_id"] for group in created["sampling_manifest"]["groups"]}
        ]
        self.assertTrue(retry_entries)
        self.assertTrue(all(entry["session_id"] == retry_ids[0] for entry in retry_entries))

    def test_initial_check_setup_restart_heals_before_lock_on_exact_replay(self) -> None:
        request = self._check_request(
            development_probe_job_ids=["probe-development"],
            operator_id="operator-initial-restart-before-lock",
        )

        def crash(stage: str) -> None:
            if stage == "after_session_persist":
                raise RuntimeError("synthetic process death before initial lock")

        self.service._session_setup_failpoint = crash
        with (
            patch.object(self.service, "_discard_unstarted_session", return_value=None),
            self.assertRaises(RuntimeError),
        ):
            self.service.create_session(request)
        self.service.close()
        self.gateway.create_requests.clear()
        recovered = BallAnnotationService(
            self.repo_root,
            get_probe=self.gateway.get_probe,
            create_probe=self.gateway.create_probe,
            read_probe_artifact=self.gateway.read_probe_artifact,
        )
        try:
            replayed = recovered.create_session(request)
            stored = recovered._load_session(replayed["session_id"])
            sampling_lock = recovered._get_sampling_lock(replayed["session_id"])
            registry = json.loads(self._registry_path.read_text(encoding="utf-8"))
            entries = [entry for entry in registry["entries"] if entry["session_id"] == replayed["session_id"]]
            self.assertEqual("check_probe_queued", replayed["status"])
            self.assertEqual(1, len(self.gateway.create_requests))
            self.assertNotIn("_initial_check_setup_transaction", stored)
            self.assertEqual(
                stored["sampling_manifest"]["manifest_sha256"],
                sampling_lock["sampling_manifest_sha256"],
            )
            self.assertEqual(
                {group["group_id"] for group in stored["sampling_manifest"]["groups"]},
                {entry["group_id"] for entry in entries},
            )
            self.assertTrue(all(entry["state"] == "reserved" for entry in entries))
        finally:
            recovered.close()

    def test_initial_check_setup_restart_heals_after_lock_on_exact_replay(self) -> None:
        request = self._check_request(
            development_probe_job_ids=["probe-development"],
            operator_id="operator-initial-restart-after-lock",
        )

        def crash(stage: str) -> None:
            if stage == "after_sampling_lock_persist":
                raise RuntimeError("synthetic process death after initial lock")

        self.service._session_setup_failpoint = crash
        with (
            patch.object(self.service, "_discard_unstarted_session", return_value=None),
            self.assertRaises(RuntimeError),
        ):
            self.service.create_session(request)
        self.service.close()
        self.gateway.create_requests.clear()
        recovered = BallAnnotationService(
            self.repo_root,
            get_probe=self.gateway.get_probe,
            create_probe=self.gateway.create_probe,
            read_probe_artifact=self.gateway.read_probe_artifact,
        )
        try:
            replayed = recovered.create_session(request)
            stored = recovered._load_session(replayed["session_id"])
            recovered._get_sampling_lock(replayed["session_id"])
            self.assertEqual("check_probe_queued", replayed["status"])
            self.assertEqual(1, len(self.gateway.create_requests))
            self.assertNotIn("_initial_check_setup_transaction", stored)
            session_paths = list(
                (self.repo_root / "data" / "ball_detector_development_v1" / "annotation_sessions" / "sessions").glob(
                    "*.json"
                )
            )
            matching = [
                path
                for path in session_paths
                if json.loads(path.read_text(encoding="utf-8")).get("request_sha256")
                == canonical_sha256(recovered._normalize_session_request(request))
            ]
            self.assertEqual(1, len(matching))
        finally:
            recovered.close()

    def test_tampered_initial_setup_fails_closed_and_explicit_retry_rehomes_reservations(self) -> None:
        request = self._check_request(
            development_probe_job_ids=["probe-development"],
            operator_id="operator-initial-tamper-source",
        )

        def crash(stage: str) -> None:
            if stage == "after_session_persist":
                raise RuntimeError("synthetic process death before tamper")

        self.service._session_setup_failpoint = crash
        with (
            patch.object(self.service, "_discard_unstarted_session", return_value=None),
            self.assertRaises(RuntimeError),
        ):
            self.service.create_session(request)
        self.service.close()
        sessions_root = self.repo_root / "data" / "ball_detector_development_v1" / "annotation_sessions" / "sessions"
        initial_path = next(
            path
            for path in sessions_root.glob("*.json")
            if json.loads(path.read_text(encoding="utf-8")).get("data_role") == "check"
        )
        tampered = json.loads(initial_path.read_text(encoding="utf-8"))
        tampered["_initial_check_setup_transaction"]["transaction_sha256"] = "f" * 64
        initial_path.write_text(
            json.dumps(tampered, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

        recovered = BallAnnotationService(
            self.repo_root,
            get_probe=self.gateway.get_probe,
            create_probe=self.gateway.create_probe,
            read_probe_artifact=self.gateway.read_probe_artifact,
        )
        try:
            blocked = recovered._load_session(initial_path.stem)
            self.assertEqual("blocked", blocked["status"])
            self.assertEqual("sampling_lock_conflict", blocked["blocker_code"])
            self.assertEqual([], blocked["frames"])
            with self.assertRaises(BallAnnotationServiceError) as unavailable:
                recovered.get_session(blocked["session_id"])
            self.assertEqual("sampling_lock_not_found", unavailable.exception.code)

            retry = recovered.create_session(
                self._retry_request(
                    blocked,
                    development_probe_job_ids=["probe-development"],
                    operator_id="operator-initial-tamper-retry",
                )
            )
            self.assertEqual("check_probe_queued", retry["status"])
            recovered._get_sampling_lock(retry["session_id"])
            registry = json.loads(self._registry_path.read_text(encoding="utf-8"))
            entries = [
                entry
                for entry in registry["entries"]
                if entry["group_id"] in {group["group_id"] for group in blocked["sampling_manifest"]["groups"]}
            ]
            self.assertTrue(entries)
            self.assertTrue(
                all(entry["session_id"] == retry["session_id"] and entry["state"] == "reserved" for entry in entries)
            )
        finally:
            recovered.close()

    def test_ready_check_invalid_timing_blocks_before_reveal_and_verified_proxy_retry_recovers(self) -> None:
        invalid_development_times = [0.0] * len(self.gateway.jobs["probe-development"]["report"]["frames"])
        for job_id in ("probe-development", "probe-development-retry"):
            self._set_probe_decoder_times(
                self.gateway.jobs[job_id],
                invalid_development_times,
            )
        self._attach_review_proxy(
            self.gateway.jobs["probe-development-retry"],
            [float(index * 100) for index in range(len(invalid_development_times))],
            self.gateway.jobs["probe-development"],
        )
        created = self.service.create_session(
            self._check_request(
                development_probe_job_ids=[
                    "probe-development",
                    "probe-development-retry",
                ],
                operator_id="operator-ready-timing-gate",
            )
        )
        check_job_id = created["check_probe_job_id"]
        self.gateway.complete(check_job_id)
        invalid_check_times = [0.0] * len(self.gateway.jobs[check_job_id]["report"]["frames"])
        self._set_probe_decoder_times(
            self.gateway.jobs[check_job_id],
            invalid_check_times,
        )

        blocked = self.service.get_session(created["session_id"])
        self.assertEqual("blocked", blocked["status"])
        self.assertEqual("invalid_source_timing", blocked["error_code"])
        self.assertEqual("review_proxy_required", blocked["blocker_code"])
        self.assertEqual([], blocked["frames"])
        self.assertIsNone(blocked["check_probe_authority"])
        registry = json.loads(self._registry_path.read_text(encoding="utf-8"))
        blocked_entries = [entry for entry in registry["entries"] if entry["session_id"] == blocked["session_id"]]
        self.assertTrue(blocked_entries)
        self.assertTrue(
            all(
                entry["state"] == "reserved" and entry["retired_for_all_profiles"] is False for entry in blocked_entries
            )
        )

        retry = self.service.create_session(
            self._retry_request(
                blocked,
                development_probe_job_ids=[
                    "probe-development",
                    "probe-development-retry",
                ],
                operator_id="operator-ready-timing-proxy-retry",
            )
        )
        retry_job_id = retry["check_probe_job_id"]
        self.gateway.complete(retry_job_id)
        self._set_probe_decoder_times(
            self.gateway.jobs[retry_job_id],
            [0.0] * len(self.gateway.jobs[retry_job_id]["report"]["frames"]),
        )
        self._attach_review_proxy(
            self.gateway.jobs[retry_job_id],
            [float(index * 10) for index in range(len(self.gateway.jobs[retry_job_id]["report"]["frames"]))],
            self.gateway.jobs[check_job_id],
        )
        recovered = self.service.get_session(retry["session_id"])
        self.assertEqual("annotating", recovered["status"])
        self.assertTrue(all(isinstance(frame["proxy_binding"], dict) for frame in recovered["frames"]))
        registry = json.loads(self._registry_path.read_text(encoding="utf-8"))
        recovered_entries = [entry for entry in registry["entries"] if entry["session_id"] == recovered["session_id"]]
        self.assertTrue(recovered_entries)
        self.assertTrue(all(entry["state"] == "revealed" for entry in recovered_entries))

        for frame in recovered["frames"]:
            candidate = frame["suggested_candidates"][0]
            self.service.put_annotation(
                recovered["session_id"],
                frame["frame_index"],
                {
                    "mutation_id": f"repaired-check-{frame['frame_index']}",
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": _check_absent(),
                    **_dismiss_detector_candidate(candidate),
                },
                if_match=f'"{frame["annotation_etag"]}"',
            )
        finalized = self.service.finalize_session(
            recovered["session_id"],
            "finalize-repaired-check",
        )
        package = finalized["package"]
        verify_frame_evidence_package(package)
        BallAnnotationPackageView.model_validate(package)
        self.assertEqual(
            [check_job_id, retry_job_id],
            [authority["job_id"] for authority in package["detector_probe_authorities"]],
        )
        self.assertTrue(
            all(
                evidence["candidate_origin"]["probe_job_id"] == check_job_id
                and evidence["review_media"]["probe_job_id"] == retry_job_id
                for evidence in package["detector_candidate_evidence"]
            )
        )

        for confused_role, confused_job_id, report_field, result_field in (
            (
                "candidate_origin",
                retry_job_id,
                "probe_report_sha256",
                "probe_result_manifest_sha256",
            ),
            (
                "review_media",
                check_job_id,
                "probe_report_sha256",
                "probe_result_manifest_sha256",
            ),
        ):
            with self.subTest(confused_role=confused_role):
                forged = deepcopy(package)
                record = forged["detector_candidate_evidence"][0]
                confused_authority = next(
                    authority
                    for authority in forged["detector_probe_authorities"]
                    if authority["job_id"] == confused_job_id
                )
                record[confused_role]["probe_job_id"] = confused_job_id
                record[confused_role][report_field] = confused_authority["probe_report_sha256"]
                record[confused_role][result_field] = confused_authority["probe_result_manifest_sha256"]
                _reseal_annotation_package(forged)
                with self.assertRaises(BallFrameEvidenceError):
                    verify_frame_evidence_package(forged)
                with self.assertRaises(ValueError):
                    BallAnnotationPackageView.model_validate(forged)

    def test_check_lighting_intervals_reject_overlap_gap_and_out_of_range(self) -> None:
        development = self._eligible_development_result()
        for mode, first_end, second_start, second_end in (
            ("overlap", 100, 100, 199),
            ("gap", 98, 100, 199),
            ("out-of-range", 99, 100, 200),
        ):
            with self.subTest(mode=mode):
                request = _request(
                    data_role="check",
                    development_probe_job_ids=["probe-development"],
                    development_package_session_id=development["package"]["session_id"],
                    development_package_sha256=development["package"]["package_sha256"],
                    operator_id=f"operator-lighting-{mode}",
                )
                request["strata_applicability"]["lighting"][0]["frame_intervals"] = [
                    {"start_frame": 0, "end_frame": first_end}
                ]
                request["strata_applicability"]["lighting"][1]["frame_intervals"] = [
                    {"start_frame": second_start, "end_frame": second_end}
                ]
                with self.assertRaises(BallAnnotationServiceError) as invalid:
                    self.service.create_session(request)
                self.assertEqual("invalid_lighting_sampling_authority", invalid.exception.code)

    def test_check_rejects_structurally_insufficient_lighting_quota_before_reveal(self) -> None:
        request = self._check_request(
            development_probe_job_ids=["probe-development"],
            operator_id="operator-insufficient-lighting-quota",
        )
        request["strata_applicability"]["lighting"][0]["quota"] = 2
        request["strata_applicability"]["lighting"][1]["quota"] = 18
        with self.assertRaises(BallAnnotationServiceError) as invalid:
            self.service.create_session(request)
        self.assertEqual("predeclared_insufficient_quota", invalid.exception.code)
        self.assertEqual(400, invalid.exception.status_code)

    def test_check_rejects_lighting_interval_without_full_temporal_family_support(self) -> None:
        request = self._check_request(
            development_probe_job_ids=["probe-development"],
            operator_id="operator-boundary-family-shortfall",
        )
        request["strata_applicability"]["lighting"][0]["frame_intervals"] = [{"start_frame": 0, "end_frame": 4}]
        request["strata_applicability"]["lighting"][1]["frame_intervals"] = [{"start_frame": 5, "end_frame": 199}]
        with self.assertRaises(BallAnnotationServiceError) as invalid:
            self.service.create_session(request)
        self.assertEqual("predeclared_sampling_infeasible", invalid.exception.code)
        self.assertEqual(409, invalid.exception.status_code)

    def test_frame_bytes_and_annotation_revisions_are_hash_bound_concurrent_and_append_only(self) -> None:
        session = self.service.create_session(_request(development_probe_job_ids=["probe-development"]))
        session_id = session["session_id"]
        frame = session["frames"][0]
        content, media_type, digest = self.service.read_frame(session_id, frame["frame_index"])
        self.assertEqual("image/jpeg", media_type)
        self.assertEqual(frame["source_frame_sha256"], digest)
        self.assertEqual(digest, hashlib.sha256(content).hexdigest())

        payload = {
            "mutation_id": "mutation-one",
            "expected_revision": 0,
            "operation": "set",
            "undo_revision": None,
            "annotation": _absent(),
        }
        first = self.service.put_annotation(
            session_id,
            frame["frame_index"],
            payload,
            if_match=f'"{frame["annotation_etag"]}"',
        )
        self.assertEqual(1, first["revision"])
        repeated = self.service.put_annotation(
            session_id,
            frame["frame_index"],
            payload,
            if_match=f'"{frame["annotation_etag"]}"',
        )
        self.assertEqual(first, repeated)
        with self.assertRaises(BallAnnotationServiceError) as stale:
            self.service.put_annotation(
                session_id,
                frame["frame_index"],
                {**payload, "mutation_id": "mutation-stale"},
                if_match=f'"{frame["annotation_etag"]}"',
            )
        self.assertEqual(412, stale.exception.status_code)

        deleted = self.service.put_annotation(
            session_id,
            frame["frame_index"],
            {
                "mutation_id": "mutation-delete",
                "expected_revision": 1,
                "operation": "delete",
                "undo_revision": None,
                "annotation": None,
            },
            if_match=f'"{first["annotation_etag"]}"',
        )
        self.assertIsNone(deleted["effective_annotation"])
        undone = self.service.put_annotation(
            session_id,
            frame["frame_index"],
            {
                "mutation_id": "mutation-undo",
                "expected_revision": 2,
                "operation": "undo",
                "undo_revision": 2,
                "annotation": None,
            },
            if_match=f'"{deleted["annotation_etag"]}"',
        )
        self.assertEqual("absent", undone["effective_annotation"]["presence"])
        raw = json.loads(
            (
                self.repo_root
                / "data"
                / "ball_detector_development_v1"
                / "annotation_sessions"
                / "sessions"
                / f"{session_id}.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual([1, 2, 3], [revision["revision"] for revision in raw["revisions"]])

    def test_revision_caps_reject_before_write_and_survive_restart(self) -> None:
        request = _request(
            development_probe_job_ids=["probe-development"],
            operator_id="operator-revision-resource-cap",
        )
        session = self.service.create_session(request)
        session_id = session["session_id"]
        session_path = (
            self.repo_root
            / "data"
            / "ball_detector_development_v1"
            / "annotation_sessions"
            / "sessions"
            / f"{session_id}.json"
        )
        first_frame = session["frames"][0]
        second_frame = session["frames"][1]

        with (
            patch(
                "football_tracking.ball_annotation_service._MAX_REVISIONS_PER_FRAME",
                1,
            ),
            patch(
                "football_tracking.ball_annotation_service._MAX_REVISIONS_PER_SESSION",
                1,
            ),
        ):
            first = self.service.put_annotation(
                session_id,
                first_frame["frame_index"],
                {
                    "mutation_id": "resource-cap-first-revision",
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": _absent(),
                },
                if_match=f'"{first_frame["annotation_etag"]}"',
            )
            boundary_bytes = session_path.read_bytes()

            attempts = (
                (
                    first_frame,
                    first["annotation_etag"],
                    1,
                    "resource-cap-per-frame",
                ),
                (
                    second_frame,
                    second_frame["annotation_etag"],
                    0,
                    "resource-cap-total",
                ),
            )
            for frame, etag, revision, mutation_id in attempts:
                with self.subTest(mutation_id=mutation_id), self.assertRaises(BallAnnotationServiceError) as rejected:
                    self.service.put_annotation(
                        session_id,
                        frame["frame_index"],
                        {
                            "mutation_id": mutation_id,
                            "expected_revision": revision,
                            "operation": "set",
                            "undo_revision": None,
                            "annotation": _absent(),
                        },
                        if_match=f'"{etag}"',
                    )
                self.assertEqual("resource_limit_exceeded", rejected.exception.code)
                self.assertEqual(409, rejected.exception.status_code)
                self.assertEqual(boundary_bytes, session_path.read_bytes())

            self.service.close()
            self.service = BallAnnotationService(
                self.repo_root,
                get_probe=self.gateway.get_probe,
                create_probe=self.gateway.create_probe,
                cancel_propagation_probe=self.gateway.cancel_probe,
                read_probe_artifact=self.gateway.read_probe_artifact,
            )
            recovered = self.service.get_session(session_id)
            self.assertEqual(1, recovered["frames"][0]["annotation_revision"])
            self.assertEqual(session_id, self.service.create_session(request)["session_id"])

    def test_canonical_session_size_cap_rejects_without_poisoning_session(self) -> None:
        request = _request(
            development_probe_job_ids=["probe-development"],
            operator_id="operator-session-byte-cap",
        )
        session = self.service.create_session(request)
        session_id = session["session_id"]
        session_path = (
            self.repo_root
            / "data"
            / "ball_detector_development_v1"
            / "annotation_sessions"
            / "sessions"
            / f"{session_id}.json"
        )
        persisted = json.loads(session_path.read_text(encoding="utf-8"))
        exact_boundary = len(canonical_json_bytes(persisted))
        boundary_bytes = session_path.read_bytes()
        frame = session["frames"][0]

        with patch(
            "football_tracking.ball_annotation_service._MAX_SESSION_CANONICAL_BYTES",
            exact_boundary,
        ):
            with self.assertRaises(BallAnnotationServiceError) as rejected:
                self.service.put_annotation(
                    session_id,
                    frame["frame_index"],
                    {
                        "mutation_id": "session-byte-cap-overflow",
                        "expected_revision": 0,
                        "operation": "set",
                        "undo_revision": None,
                        "annotation": _absent(),
                    },
                    if_match=f'"{frame["annotation_etag"]}"',
                )
            self.assertEqual("resource_limit_exceeded", rejected.exception.code)
            self.assertEqual(409, rejected.exception.status_code)
            self.assertEqual(boundary_bytes, session_path.read_bytes())

            self.service.close()
            self.service = BallAnnotationService(
                self.repo_root,
                get_probe=self.gateway.get_probe,
                create_probe=self.gateway.create_probe,
                cancel_propagation_probe=self.gateway.cancel_probe,
                read_probe_artifact=self.gateway.read_probe_artifact,
            )
            self.assertEqual(session_id, self.service.get_session(session_id)["session_id"])
            self.assertEqual(session_id, self.service.create_session(request)["session_id"])

    def test_final_package_revision_chain_respects_session_caps(self) -> None:
        session = self.service.create_session(
            _request(
                development_probe_job_ids=["probe-development"],
                operator_id="operator-final-package-revision-cap",
            )
        )
        with (
            patch(
                "football_tracking.ball_annotation_service._MAX_REVISIONS_PER_FRAME",
                1,
            ),
            patch(
                "football_tracking.ball_annotation_service._MAX_REVISIONS_PER_SESSION",
                len(session["frames"]),
            ),
        ):
            for index, frame in enumerate(session["frames"]):
                candidate = frame["suggested_candidates"][0]
                self.service.put_annotation(
                    session["session_id"],
                    frame["frame_index"],
                    {
                        "mutation_id": f"bounded-final-{frame['frame_index']}",
                        "expected_revision": 0,
                        "operation": "set",
                        "undo_revision": None,
                        "annotation": _present_box() if index == 0 else _absent(),
                        **(
                            _accept_detector_candidate(candidate)
                            if index == 0
                            else _dismiss_detector_candidate(candidate)
                        ),
                    },
                    if_match=f'"{frame["annotation_etag"]}"',
                )
            result = self.service.finalize_session(session["session_id"], "finalize-bounded-revisions")

        revisions = result["package"]["revision_chain"]
        self.assertEqual(len(session["frames"]), len(revisions))
        self.assertEqual(
            {frame["frame_index"] for frame in session["frames"]},
            {revision["frame_index"] for revision in revisions},
        )
        self.assertLess(len(canonical_json_bytes(result["package"])), 64 * 1024 * 1024)

    def test_detector_candidate_decisions_are_server_derived_and_probe_bound(self) -> None:
        session = self.service.create_session(_request(development_probe_job_ids=["probe-development"]))
        self.assertTrue(
            all(
                candidate["decision"] == "pending"
                for frame in session["frames"]
                for candidate in frame["suggested_candidates"]
            )
        )
        accepted_frame = session["frames"][0]
        candidate = accepted_frame["suggested_candidates"][0]
        incomplete = {
            "mutation_id": "incomplete-detector-binding",
            "expected_revision": 0,
            "operation": "set",
            "undo_revision": None,
            "annotation": _present_box(),
            "suggestion_kind": "detector_candidate",
            "suggestion_id": candidate["candidate_id"],
        }
        with self.assertRaises(BallAnnotationServiceError) as missing_binding:
            self.service.put_annotation(
                session["session_id"],
                accepted_frame["frame_index"],
                incomplete,
                if_match=f'"{accepted_frame["annotation_etag"]}"',
            )
        self.assertEqual("invalid_suggestion", missing_binding.exception.code)
        self.assertEqual(400, missing_binding.exception.status_code)
        for field, value in (
            ("accepted_suggestion_job_id", "probe-wrong"),
            ("accepted_suggestion_sha256", "f" * 64),
        ):
            with self.subTest(field=field), self.assertRaises(BallAnnotationServiceError) as tampered:
                self.service.put_annotation(
                    session["session_id"],
                    accepted_frame["frame_index"],
                    {
                        **incomplete,
                        "mutation_id": f"tampered-{field}",
                        **_accept_detector_candidate(candidate),
                        field: value,
                    },
                    if_match=f'"{accepted_frame["annotation_etag"]}"',
                )
            self.assertEqual("suggestion_binding_mismatch", tampered.exception.code)
            self.assertEqual(409, tampered.exception.status_code)
        other_candidate = session["frames"][1]["suggested_candidates"][0]
        with self.assertRaises(BallAnnotationServiceError) as cross_frame:
            self.service.put_annotation(
                session["session_id"],
                accepted_frame["frame_index"],
                {
                    **incomplete,
                    "mutation_id": "cross-frame-detector-binding",
                    **_accept_detector_candidate(other_candidate),
                },
                if_match=f'"{accepted_frame["annotation_etag"]}"',
            )
        self.assertEqual("suggestion_not_found", cross_frame.exception.code)
        accepted = self.service.put_annotation(
            session["session_id"],
            accepted_frame["frame_index"],
            {
                "mutation_id": "accept-detector-candidate",
                "expected_revision": 0,
                "operation": "set",
                "undo_revision": None,
                "annotation": {
                    **_present_box(),
                    "provenance": "suggestion_dismissed_manual",
                },
                **_accept_detector_candidate(candidate),
            },
            if_match=f'"{accepted_frame["annotation_etag"]}"',
        )
        self.assertEqual("detector_candidate", accepted["accepted_suggestion_kind"])
        self.assertEqual(candidate["candidate_id"], accepted["accepted_suggestion_id"])
        self.assertEqual("probe-development", accepted["accepted_suggestion_job_id"])
        self.assertEqual(
            candidate["suggestion_sha256"],
            accepted["accepted_suggestion_sha256"],
        )
        self.assertEqual(
            "detector_candidate_human_confirmed",
            accepted["effective_annotation"]["provenance"],
        )
        with self.assertRaises(BallAnnotationServiceError) as repeated:
            self.service.put_annotation(
                session["session_id"],
                accepted_frame["frame_index"],
                {
                    "mutation_id": "accept-detector-candidate-twice",
                    "expected_revision": 1,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": _present_box(),
                    **_accept_detector_candidate(candidate),
                },
                if_match=f'"{accepted["annotation_etag"]}"',
            )
        self.assertEqual("suggestion_already_decided", repeated.exception.code)

        dismissed_frame = session["frames"][1]
        dismissed_candidate = dismissed_frame["suggested_candidates"][0]
        dismissed = self.service.put_annotation(
            session["session_id"],
            dismissed_frame["frame_index"],
            {
                "mutation_id": "dismiss-detector-candidate",
                "expected_revision": 0,
                "operation": "set",
                "undo_revision": None,
                "annotation": _absent(),
                **_dismiss_detector_candidate(dismissed_candidate),
            },
            if_match=f'"{dismissed_frame["annotation_etag"]}"',
        )
        self.assertEqual("detector_candidate", dismissed["dismissed_suggestion_kind"])
        self.assertEqual(
            dismissed_candidate["candidate_id"],
            dismissed["dismissed_suggestion_id"],
        )
        self.assertEqual(
            "suggestion_dismissed_manual",
            dismissed["effective_annotation"]["provenance"],
        )
        self.assertEqual(
            len(session["frames"]) - 2,
            self.service.get_session(session["session_id"])["progress"]["unconfirmed_suggestions"],
        )
        refreshed = self.service.get_session(session["session_id"])
        decisions = {
            frame["frame_index"]: frame["suggested_candidates"][0]["decision"] for frame in refreshed["frames"]
        }
        self.assertEqual("accepted", decisions[accepted_frame["frame_index"]])
        self.assertEqual("dismissed", decisions[dismissed_frame["frame_index"]])
        self.assertTrue(
            all(
                decision == "pending"
                for frame_index, decision in decisions.items()
                if frame_index not in {accepted_frame["frame_index"], dismissed_frame["frame_index"]}
            )
        )

    def test_direct_service_annotation_validation_cannot_be_bypassed(self) -> None:
        session = self.service.create_session(_request(development_probe_job_ids=["probe-development"]))
        frame = session["frames"][0]
        invalid_annotations = [
            {
                **_present_box(),
                "point_source_px": None,
                "bbox_source_px": None,
            },
            {**_present_box(), "annotation_state": "suggested"},
        ]
        for index, annotation in enumerate(invalid_annotations):
            with self.subTest(index=index), self.assertRaises(BallAnnotationServiceError) as invalid:
                self.service.put_annotation(
                    session["session_id"],
                    frame["frame_index"],
                    {
                        "mutation_id": f"invalid-direct-{index}",
                        "expected_revision": 0,
                        "operation": "set",
                        "undo_revision": None,
                        "annotation": annotation,
                    },
                    if_match=f'"{frame["annotation_etag"]}"',
                )
            self.assertEqual("invalid_annotation", invalid.exception.code)

        for index, current in enumerate(session["frames"]):
            candidate = current["suggested_candidates"][0]
            self.service.put_annotation(
                session["session_id"],
                current["frame_index"],
                {
                    "mutation_id": f"valid-after-negative-{current['frame_index']}",
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": _present_box() if index == 0 else _absent(),
                    **(_accept_detector_candidate(candidate) if index == 0 else _dismiss_detector_candidate(candidate)),
                },
                if_match=f'"{current["annotation_etag"]}"',
            )
        development = self.service.finalize_session(session["session_id"], "finalize-direct-negative-development")
        check = self.service.create_session(
            _request(
                data_role="check",
                development_probe_job_ids=["probe-development"],
                development_package_session_id=development["package"]["session_id"],
                development_package_sha256=development["package"]["package_sha256"],
            )
        )
        self.gateway.unaudited_job_ids.add(check["check_probe_job_id"])
        self.gateway.complete(check["check_probe_job_id"])
        check = self.service.get_session(check["session_id"])
        check_frame = check["frames"][0]
        with self.assertRaises(BallAnnotationServiceError) as training_bypass:
            self.service.put_annotation(
                check["session_id"],
                check_frame["frame_index"],
                {
                    "mutation_id": "invalid-check-positive",
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": _present_box(),
                },
                if_match=f'"{check_frame["annotation_etag"]}"',
            )
        self.assertEqual("invalid_annotation", training_bypass.exception.code)

    def test_check_manual_annotations_with_unresolved_candidates_cannot_finalize(self) -> None:
        development = self._eligible_development_result()
        check = self.service.create_session(
            _request(
                data_role="check",
                development_probe_job_ids=["probe-development"],
                operator_id="operator-check-unresolved",
                development_package_session_id=development["package"]["session_id"],
                development_package_sha256=development["package"]["package_sha256"],
            )
        )
        self.gateway.complete(check["check_probe_job_id"])
        check = self.service.get_session(check["session_id"])
        for frame in check["frames"]:
            self.service.put_annotation(
                check["session_id"],
                frame["frame_index"],
                {
                    "mutation_id": f"unresolved-check-{frame['frame_index']}",
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": _check_absent(),
                },
                if_match=f'"{frame["annotation_etag"]}"',
            )

        with self.assertRaises(BallAnnotationServiceError) as incomplete:
            self.service.finalize_session(check["session_id"], "finalize-unresolved-check")

        self.assertEqual("suggestion_decisions_incomplete", incomplete.exception.code)
        self.assertEqual(409, incomplete.exception.status_code)
        current = self.service.get_session(check["session_id"])
        self.assertEqual("annotating", current["status"])
        self.assertEqual(len(current["frames"]), current["progress"]["unconfirmed_suggestions"])
        self.assertTrue(
            all(
                candidate["decision"] == "pending"
                for frame in current["frames"]
                for candidate in frame["suggested_candidates"]
            )
        )

    def test_real_check_finalization_seals_explicit_candidate_decisions(self) -> None:
        development = self._eligible_development_result()
        check = self.service.create_session(
            _request(
                data_role="check",
                development_probe_job_ids=["probe-development"],
                operator_id="operator-real-check-finalization",
                development_package_session_id=development["package"]["session_id"],
                development_package_sha256=development["package"]["package_sha256"],
            )
        )
        self.gateway.unaudited_job_ids.add(check["check_probe_job_id"])
        self.gateway.complete(check["check_probe_job_id"])
        check = self.service.get_session(check["session_id"])
        for index, frame in enumerate(check["frames"]):
            candidate = frame["suggested_candidates"][0]
            annotation = _check_absent()
            decision = _dismiss_detector_candidate(candidate)
            if index < 2:
                annotation = {**_present_box(), "training_use": "excluded"}
                if index == 1:
                    annotation["point_source_px"]["x"] += 0.5
                    annotation["bbox_source_px"]["left"] += 0.5
                    annotation["bbox_source_px"]["right"] += 0.5
                decision = _accept_detector_candidate(candidate)
            self.service.put_annotation(
                check["session_id"],
                frame["frame_index"],
                {
                    "mutation_id": f"real-check-{frame['frame_index']}",
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": annotation,
                    **decision,
                },
                if_match=f'"{frame["annotation_etag"]}"',
            )

        result = self.service.finalize_session(check["session_id"], "finalize-real-check")
        verify_frame_evidence_package(result["package"])
        self.assertEqual(
            "embedded_job_record",
            result["package"]["detector_probe_authorities"][0]["audit_anchor_kind"],
        )
        self.assertEqual("check", result["package"]["data_role"])
        self.assertEqual(
            development["package"]["package_sha256"],
            result["package"]["development_package_binding"]["package_sha256"],
        )
        evidence = result["package"]["detector_candidate_evidence"]
        self.assertEqual(len(check["frames"]), len(evidence))
        self.assertEqual(2, sum(row["decision"]["decision"] == "accepted_human_annotation" for row in evidence))
        self.assertEqual(
            len(check["frames"]) - 2,
            sum(row["decision"]["decision"] == "dismissed_manual_annotation" for row in evidence),
        )
        self.assertFalse(any(row["decision"] is None for row in evidence))
        validation = result["package"]["dataset_expansion_eligibility"]["validation_evidence"]
        self.assertEqual(0, validation["pending_detector_candidate_count"])
        self.assertEqual(0, validation["pending_propagation_suggestion_count"])
        self.assertEqual(0, validation["pending_suggestion_decision_count"])
        self.assertEqual(
            result["package"]["dataset_expansion_eligibility"],
            result["feasibility_report"]["sealed_evidence"]["dataset_expansion_eligibility"],
        )
        self.assertIn(
            result["feasibility_report"]["status"],
            {
                "insufficient_evidence",
                "feasibility_passed",
                "feasibility_failed",
            },
        )
        forged = deepcopy(result["package"])
        forged["detector_candidate_evidence"][0]["candidate_origin"]["probe_job_id"] = "probe-wrong-check-origin"
        _reseal_annotation_package(forged)
        with self.assertRaises(BallFrameEvidenceError):
            verify_frame_evidence_package(forged)

    def test_offline_candidate_membership_rejects_coherent_candidate_and_lineage_tamper(
        self,
    ) -> None:
        package = self._eligible_development_result()["package"]
        verify_frame_evidence_package(package)

        for mutation in ("bbox", "confidence", "rank", "candidate_id"):
            with self.subTest(mutation=mutation):
                forged = deepcopy(package)
                record = forged["detector_candidate_evidence"][0]
                candidate = record["candidate"]
                if mutation == "bbox":
                    candidate["bbox_source_px"]["left"] += 1.0
                elif mutation == "confidence":
                    candidate["confidence"] = 0.7
                elif mutation == "rank":
                    candidate["rank"] = 2
                else:
                    candidate["candidate_id"] = "suggestion-invented"
                candidate_sha256 = canonical_sha256(
                    {
                        key: value
                        for key, value in candidate.items()
                        if key
                        not in {
                            "suggestion_job_id",
                            "suggestion_sha256",
                        }
                    }
                )
                candidate["suggestion_sha256"] = candidate_sha256
                record["candidate_sha256"] = candidate_sha256
                _reseal_annotation_package(forged)
                with self.assertRaises(BallFrameEvidenceError):
                    verify_frame_evidence_package(forged)

        for map_field in (
            "development_probe_report_sha256s",
            "development_probe_result_manifest_sha256s",
            "development_probe_execution_bundle_sha256s",
            "development_probe_frozen_profiles_sha256s",
        ):
            for mutation in ("missing", "extra", "value"):
                with self.subTest(
                    map_field=map_field,
                    mutation=mutation,
                ):
                    forged = deepcopy(package)
                    values = forged["lineage"][map_field]
                    if mutation == "missing":
                        values.pop("probe-development")
                    elif mutation == "extra":
                        values["probe-unbound-extra"] = "f" * 64
                    else:
                        values["probe-development"] = "f" * 64
                    _reseal_annotation_package(forged)
                    with self.assertRaisesRegex(
                        BallFrameEvidenceError,
                        "exact lineage digest maps",
                    ):
                        verify_frame_evidence_package(forged)

        for mutation in ("missing", "extra"):
            with self.subTest(authority_mutation=mutation):
                forged = deepcopy(package)
                if mutation == "missing":
                    forged["detector_probe_authorities"] = []
                else:
                    forged["detector_probe_authorities"].append(deepcopy(forged["detector_probe_authorities"][0]))
                _reseal_annotation_package(forged)
                with self.assertRaises(BallFrameEvidenceError):
                    verify_frame_evidence_package(forged)

        forged = deepcopy(package)
        forged["detector_candidate_evidence"].pop()
        _reseal_annotation_package(forged)
        with self.assertRaisesRegex(
            BallFrameEvidenceError,
            "collection is incomplete or invented",
        ):
            verify_frame_evidence_package(forged)

        forged = deepcopy(package)
        invented = deepcopy(forged["detector_candidate_evidence"][0])
        invented["candidate"]["candidate_id"] = "suggestion-invented-primary"
        invented_sha256 = canonical_sha256(
            {
                key: value
                for key, value in invented["candidate"].items()
                if key not in {"suggestion_job_id", "suggestion_sha256"}
            }
        )
        invented["candidate"]["suggestion_sha256"] = invented_sha256
        invented["candidate_sha256"] = invented_sha256
        forged["detector_candidate_evidence"].append(invented)
        forged["detector_candidate_evidence"].sort(
            key=lambda record: (
                record["frame_index"],
                record["candidate"]["rank"],
                record["candidate"]["candidate_id"],
            )
        )
        _reseal_annotation_package(forged)
        with self.assertRaises(BallFrameEvidenceError):
            verify_frame_evidence_package(forged)

    def test_offline_candidate_membership_rejects_invented_supplemental_record(
        self,
    ) -> None:
        session, _seed, _revision, _ready = self._ready_propagation(mutation_suffix="offline-supplemental-membership")
        current = self.service.get_session(session["session_id"])
        for frame in current["frames"]:
            pending_detector = next(
                (candidate for candidate in frame["suggested_candidates"] if candidate["decision"] == "pending"),
                None,
            )
            pending_propagation = next(
                (
                    suggestion
                    for suggestion in frame["propagation_suggestions"]
                    if suggestion["pending_human_confirmation"]
                ),
                None,
            )
            if pending_detector is None and pending_propagation is None:
                continue
            if pending_detector is not None:
                decision = _dismiss_detector_candidate(pending_detector)
            else:
                decision = _dismiss_propagation_suggestion(pending_propagation)
            self.service.put_annotation(
                current["session_id"],
                frame["frame_index"],
                {
                    "mutation_id": (f"offline-supplemental-{frame['frame_index']}"),
                    "expected_revision": frame["annotation_revision"],
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": _absent(),
                    **decision,
                },
                if_match=f'"{frame["annotation_etag"]}"',
            )
        current = self.service.get_session(current["session_id"])
        self.assertEqual(
            0,
            current["progress"]["unconfirmed_suggestions"],
            [
                (
                    frame["frame_index"],
                    [item["decision"] for item in frame["suggested_candidates"]],
                    [item["pending_human_confirmation"] for item in frame["propagation_suggestions"]],
                )
                for frame in current["frames"]
            ],
        )
        package = self.service.finalize_session(
            current["session_id"],
            "finalize-offline-supplemental-membership",
        )["package"]
        verify_frame_evidence_package(package)
        supplemental_index = package["supplemental_frame_indices"][0]
        forged = deepcopy(package)
        invented = deepcopy(forged["detector_candidate_evidence"][0])
        invented["frame_index"] = supplemental_index
        invented["candidate"]["candidate_id"] = "suggestion-invented-supplemental"
        invented_sha256 = canonical_sha256(
            {
                key: value
                for key, value in invented["candidate"].items()
                if key not in {"suggestion_job_id", "suggestion_sha256"}
            }
        )
        invented["candidate"]["suggestion_sha256"] = invented_sha256
        invented["candidate_sha256"] = invented_sha256
        forged["detector_candidate_evidence"].append(invented)
        forged["detector_candidate_evidence"].sort(
            key=lambda record: (
                record["frame_index"],
                record["candidate"]["rank"],
                record["candidate"]["candidate_id"],
            )
        )
        _reseal_annotation_package(forged)

        with self.assertRaises(BallFrameEvidenceError):
            verify_frame_evidence_package(forged)

    def test_offline_candidate_history_cannot_be_downgraded_by_clearing_both_collections(
        self,
    ) -> None:
        forged = deepcopy(self._eligible_development_result()["package"])
        forged["detector_probe_authorities"] = []
        forged["detector_candidate_evidence"] = []
        forged["detector_candidate_evidence_sha256"] = canonical_sha256([])
        validation = forged["dataset_expansion_eligibility"]["validation_evidence"]
        validation["pending_detector_candidate_count"] = 0
        validation["pending_suggestion_decision_count"] = 0
        _reseal_annotation_package(forged)

        with self.assertRaisesRegex(
            BallFrameEvidenceError,
            "differ from exact package lineage",
        ):
            verify_frame_evidence_package(forged)

    def test_session_request_and_frozen_profiles_reject_resealed_profile_switches(
        self,
    ) -> None:
        package = self._eligible_development_result()["package"]
        verify_frame_evidence_package(package)
        BallAnnotationPackageView.model_validate(package)

        for field, forged_value in (
            ("model_id", "model-forged"),
            ("model_version", "forged-version"),
            ("model_descriptor_sha256", "f" * 64),
            ("weights_sha256", "0" * 64),
        ):
            with self.subTest(binding_field=field):
                forged = deepcopy(package)
                forged["locked_profile"][field] = forged_value
                _reseal_annotation_package(forged)
                with self.assertRaisesRegex(
                    BallFrameEvidenceError,
                    "profile selection differs from frozen authority",
                ):
                    verify_frame_evidence_package(forged)
                with self.assertRaises(ValueError):
                    BallAnnotationPackageView.model_validate(forged)

        forged = deepcopy(package)
        original_locked = deepcopy(forged["locked_profile"])
        original_control = deepcopy(forged["control_profile"])
        forged["locked_profile"] = original_control
        forged["control_profile_id"] = original_locked["profile_id"]
        forged["control_profile"] = original_locked
        manifest = forged["sampling_manifest"]
        manifest["locked_profile_id"] = original_control["profile_id"]
        manifest["locked_profile_sha256"] = original_control["profile_sha256"]
        manifest["manifest_sha256"] = canonical_sha256(
            {key: value for key, value in manifest.items() if key != "manifest_sha256"}
        )
        request_authority = forged["session_request_authority"]
        normalized_request = request_authority["normalized_request"]
        normalized_request["locked_profile_id"] = original_control["profile_id"]
        request_authority["request_sha256"] = canonical_sha256(normalized_request)
        request_authority["authority_sha256"] = canonical_sha256(
            {key: value for key, value in request_authority.items() if key != "authority_sha256"}
        )
        _reseal_annotation_package(forged)
        with self.assertRaisesRegex(
            BallFrameEvidenceError,
            "session identity differs from its request authority",
        ):
            verify_frame_evidence_package(forged)
        with self.assertRaises(ValueError):
            BallAnnotationPackageView.model_validate(forged)

    def test_historical_audited_origin_with_embedded_proxy_media_finalizes_offline(
        self,
    ) -> None:
        invalid_times = [0.0] * len(self.gateway.jobs["probe-development"]["report"]["frames"])
        for job_id in (
            "probe-development",
            "probe-development-retry",
        ):
            self._set_probe_decoder_times(
                self.gateway.jobs[job_id],
                invalid_times,
            )
        self._attach_review_proxy(
            self.gateway.jobs["probe-development-retry"],
            [0.0, 2_000.0, 4_000.0, 6_000.0, 8_000.0, 9_950.0],
            self.gateway.jobs["probe-development"],
        )
        self.gateway.unaudited_job_ids.add("probe-development-retry")
        session = self.service.create_session(
            _request(
                development_probe_job_ids=[
                    "probe-development",
                    "probe-development-retry",
                ],
                operator_id="operator-historical-offline",
            )
        )
        for frame in session["frames"]:
            candidate = frame["suggested_candidates"][0]
            self.service.put_annotation(
                session["session_id"],
                frame["frame_index"],
                {
                    "mutation_id": f"historical-offline-{frame['frame_index']}",
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": _absent(),
                    **_dismiss_detector_candidate(candidate),
                },
                if_match=f'"{frame["annotation_etag"]}"',
            )
        result = self.service.finalize_session(
            session["session_id"],
            "finalize-historical-offline",
        )
        authorities = {authority["job_id"]: authority for authority in result["package"]["detector_probe_authorities"]}
        self.assertEqual(
            "audited_t2_legacy",
            authorities["probe-development"]["audit_anchor_kind"],
        )
        self.assertEqual(
            "embedded_job_record",
            authorities["probe-development-retry"]["audit_anchor_kind"],
        )
        verify_frame_evidence_package(result["package"])
        for mutation in ("origin_to_child", "media_to_parent"):
            with self.subTest(mutation=mutation):
                forged = deepcopy(result["package"])
                record = forged["detector_candidate_evidence"][0]
                if mutation == "origin_to_child":
                    record["candidate_origin"].update(
                        {
                            "probe_job_id": "probe-development-retry",
                            "probe_report_sha256": authorities["probe-development-retry"]["probe_report_sha256"],
                            "probe_result_manifest_sha256": authorities["probe-development-retry"][
                                "probe_result_manifest_sha256"
                            ],
                        }
                    )
                    record["candidate"]["suggestion_job_id"] = "probe-development-retry"
                else:
                    record["review_media"].update(
                        {
                            "probe_job_id": "probe-development",
                            "probe_report_sha256": authorities["probe-development"]["probe_report_sha256"],
                            "probe_result_manifest_sha256": authorities["probe-development"][
                                "probe_result_manifest_sha256"
                            ],
                            "proxy_binding_sha256": None,
                        }
                    )
                _reseal_annotation_package(forged)
                with self.assertRaises(BallFrameEvidenceError):
                    verify_frame_evidence_package(forged)

    def test_development_request_cannot_smuggle_check_retry_lineage(self) -> None:
        request = _request(retry_from_session_id="annotation-check-blocked")
        with self.assertRaises(BallAnnotationServiceError) as invalid:
            self.service.create_session(request)
        self.assertEqual("invalid_retry", invalid.exception.code)

    def test_finalize_is_immutable_idempotent_and_never_qualifies_or_trains(self) -> None:
        session = self.service.create_session(_request(development_probe_job_ids=["probe-development"]))
        session_id = session["session_id"]
        for frame in session["frames"]:
            candidate = frame["suggested_candidates"][0]
            self.service.put_annotation(
                session_id,
                frame["frame_index"],
                {
                    "mutation_id": f"mutation-{frame['frame_index']}",
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": _absent(),
                    **_dismiss_detector_candidate(candidate),
                },
                if_match=f'"{frame["annotation_etag"]}"',
            )
        first = self.service.finalize_session(session_id, "finalize-one")
        second = self.service.finalize_session(session_id, "finalize-two")
        self.assertEqual(first, second)
        self.assertEqual("ball_annotation_package", first["package"]["artifact_type"])
        self.assertEqual("not_applicable", first["feasibility_report"]["status"])
        self.assertFalse(first["package"]["training_eligible"])
        self.assertFalse(first["package"]["may_seed_dataset_expansion"])
        self.assertFalse(first["feasibility_report"]["authorizations"]["trial_eligible"])
        self.assertFalse(first["feasibility_report"]["authorizations"]["may_expand_to_100_300_boxes"])
        refreshed = self.service.get_session(session_id)
        self.assertEqual("finalized", refreshed["status"])
        with self.assertRaisesRegex(BallAnnotationServiceError, "finalized"):
            self.service.put_annotation(
                session_id,
                session["frames"][0]["frame_index"],
                {
                    "mutation_id": "after-finalize",
                    "expected_revision": 1,
                    "operation": "delete",
                    "undo_revision": None,
                    "annotation": None,
                },
                if_match=f'"{session["frames"][0]["annotation_etag"]}"',
            )

    def test_unresolved_detector_candidates_cannot_seed_dataset_expansion(self) -> None:
        session = self.service.create_session(_request(development_probe_job_ids=["probe-development"]))
        for index, frame in enumerate(session["frames"]):
            self.service.put_annotation(
                session["session_id"],
                frame["frame_index"],
                {
                    "mutation_id": f"pending-detector-{frame['frame_index']}",
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": _present_box() if index == 0 else _absent(),
                },
                if_match=f'"{frame["annotation_etag"]}"',
            )
        with self.assertRaises(BallAnnotationServiceError) as incomplete:
            self.service.finalize_session(session["session_id"], "finalize-pending-detector")
        self.assertEqual("suggestion_decisions_incomplete", incomplete.exception.code)
        self.assertEqual(409, incomplete.exception.status_code)
        refreshed = self.service.get_session(session["session_id"])
        self.assertEqual("annotating", refreshed["status"])
        self.assertEqual(
            len(session["frames"]),
            refreshed["progress"]["unconfirmed_suggestions"],
        )

    def test_annotation_requires_one_quoted_strong_if_match(self) -> None:
        session = self.service.create_session(_request(development_probe_job_ids=["probe-development"]))
        frame = session["frames"][0]
        payload = {
            "mutation_id": "strict-etag",
            "expected_revision": 0,
            "operation": "set",
            "undo_revision": None,
            "annotation": _absent(),
        }
        with self.assertRaises(BallAnnotationServiceError) as missing:
            self.service.put_annotation(session["session_id"], frame["frame_index"], payload, if_match=None)
        self.assertEqual(428, missing.exception.status_code)
        with self.assertRaises(BallAnnotationServiceError) as unquoted:
            self.service.put_annotation(
                session["session_id"],
                frame["frame_index"],
                payload,
                if_match=frame["annotation_etag"],
            )
        self.assertEqual(400, unquoted.exception.status_code)

    def test_development_retry_uses_latest_runtime_and_execution_authority(self) -> None:
        root = self.repo_root / "latest-runtime"
        (root / "data").mkdir(parents=True)
        gateway = _FakeProbeGateway()
        retry = gateway.jobs["probe-development-retry"]
        _set_job_runtime_execution_authority(
            retry,
            runtime_environment_sha256="9" * 64,
            execution_bundle_sha256="8" * 64,
        )
        service = BallAnnotationService(
            root,
            get_probe=gateway.get_probe,
            create_probe=gateway.create_probe,
            read_probe_artifact=gateway.read_probe_artifact,
        )
        try:
            session = service.create_session(_request())
            self.assertEqual(
                "9" * 64,
                session["lineage"]["runtime_environment_sha256"],
            )
            self.assertEqual(
                "8" * 64,
                session["lineage"]["development_probe_execution_bundle_sha256s"]["probe-development-retry"],
            )
            stored = service._load_session(session["session_id"])
            self.assertTrue(
                all(
                    frame["_probe_job_id"] == "probe-development-retry"
                    and frame["_runtime_environment_sha256"] == "9" * 64
                    for frame in stored["frames"]
                )
            )
            self.assertEqual(
                session["attempt_family_sha256"],
                service._attempt_family_sha256(stored),
            )
        finally:
            service.close()

    def test_development_retry_allows_only_verified_proxy_upgrade_after_invalid_timing(self) -> None:
        root = self.repo_root / "verified-proxy-upgrade"
        (root / "data").mkdir(parents=True)
        gateway = _FakeProbeGateway()
        invalid_times = [0.0] * len(gateway.jobs["probe-development"]["report"]["frames"])
        for job_id in (
            "probe-development",
            "probe-development-retry",
        ):
            self._set_probe_decoder_times(gateway.jobs[job_id], invalid_times)
        self._attach_review_proxy(
            gateway.jobs["probe-development-retry"],
            [float(index * 100) for index in range(len(invalid_times))],
            gateway.jobs["probe-development"],
        )
        service = BallAnnotationService(
            root,
            get_probe=gateway.get_probe,
            create_probe=gateway.create_probe,
            read_probe_artifact=gateway.read_probe_artifact,
        )
        try:
            session = service.create_session(_request())
            self.assertEqual("annotating", session["status"])
            self.assertTrue(all(isinstance(frame["proxy_binding"], dict) for frame in session["frames"]))
            stored = service._load_session(session["session_id"])
            self.assertTrue(
                all(
                    frame["_probe_job_id"] == "probe-development-retry" and isinstance(frame["_proxy_binding"], dict)
                    for frame in stored["frames"]
                )
            )
        finally:
            service.close()

    def test_audited_legacy_missing_timing_accepts_strict_verified_proxy_upgrade(self) -> None:
        root = self.repo_root / "audited-legacy-verified-proxy-upgrade"
        (root / "data").mkdir(parents=True)
        gateway = _FakeProbeGateway()
        legacy_bindings = self._make_audited_legacy_timing_absent(gateway.jobs["probe-development"])
        service = BallAnnotationService(
            root,
            get_probe=gateway.get_probe,
            create_probe=gateway.create_probe,
            read_probe_artifact=gateway.read_probe_artifact,
        )
        try:
            with patched_audited_t2_probe_bindings(legacy_bindings):
                blocked = service.create_session(
                    _request(
                        development_probe_job_ids=["probe-development"],
                        operator_id="operator-audited-legacy-blocked",
                    )
                )
                self.assertEqual("blocked", blocked["status"])
                self.assertEqual("review_proxy_required", blocked["blocker_code"])
                self.assertEqual([], blocked["frames"])

                retry = gateway.jobs["probe-development-retry"]
                expected_times = [
                    frame["frame_index"] / float(retry["report"]["decode"]["fps"]) * 1000.0
                    for frame in retry["report"]["frames"]
                ]
                self._attach_review_proxy(
                    retry,
                    expected_times,
                    gateway.jobs["probe-development"],
                )
                upgraded = service.create_session(
                    _request(
                        development_probe_job_ids=[
                            "probe-development",
                            "probe-development-retry",
                        ],
                        retry_from_session_id=blocked["session_id"],
                        operator_id="operator-audited-legacy-upgraded",
                    )
                )

            self.assertEqual("annotating", upgraded["status"])
            self.assertEqual(
                "review_proxy_decode_upgrade",
                upgraded["retry_lineage"]["mode"],
            )
            self.assertEqual(
                [None] * len(expected_times),
                [frame["decoder_reported_pos_msec"] for frame in upgraded["frames"]],
            )
            self.assertEqual(blocked["source"], upgraded["source"])
            self.assertEqual(blocked["locked_profile"], upgraded["locked_profile"])
            self.assertEqual(
                blocked["sampling_manifest"]["groups"],
                upgraded["sampling_manifest"]["groups"],
            )
            self.assertEqual(
                ["probe-development", "probe-development-retry"],
                upgraded["lineage"]["development_probe_job_ids"],
            )
            for frame, expected_time in zip(upgraded["frames"], expected_times, strict=True):
                proxy = frame["proxy_binding"]
                self.assertIsInstance(proxy, dict)
                self.assertEqual(frame["frame_index"], proxy["source_frame"]["frame_index"])
                self.assertEqual(frame["source_frame_sha256"], proxy["source_frame"]["sha256"])
                self.assertIsNone(proxy["source_frame"]["decoder_reported_pos_msec"])
                self.assertEqual(
                    expected_time,
                    proxy["proxy_frame"]["cfr_time_msec"],
                )
            original = service.get_session(blocked["session_id"])
            self.assertEqual("blocked", original["status"])
            self.assertEqual([], original["frames"])
        finally:
            service.close()

    def test_audited_legacy_missing_timing_rejects_unverified_or_mismatched_upgrade(self) -> None:
        for scenario in ("missing_proxy", "proxy_timing_mismatch"):
            with self.subTest(scenario=scenario):
                root = self.repo_root / f"audited-legacy-{scenario}"
                (root / "data").mkdir(parents=True)
                gateway = _FakeProbeGateway()
                legacy_bindings = self._make_audited_legacy_timing_absent(gateway.jobs["probe-development"])
                service = BallAnnotationService(
                    root,
                    get_probe=gateway.get_probe,
                    create_probe=gateway.create_probe,
                    read_probe_artifact=gateway.read_probe_artifact,
                )
                try:
                    with patched_audited_t2_probe_bindings(legacy_bindings):
                        blocked = service.create_session(
                            _request(
                                development_probe_job_ids=["probe-development"],
                                operator_id=f"operator-audited-{scenario}-blocked",
                            )
                        )
                        retry = gateway.jobs["probe-development-retry"]
                        expected_code = "retry_frame_mismatch"
                        if scenario == "proxy_timing_mismatch":
                            original_times = [0.0] * len(retry["report"]["frames"])
                            self._attach_review_proxy(
                                retry,
                                original_times,
                                gateway.jobs["probe-development"],
                            )
                            manifest = retry["report"]["review_proxy_manifest"]
                            manifest["mappings"][1]["proxy_cfr_time_msec"] += 1.0
                            manifest["mapping_sha256"] = canonical_sha256(manifest["mappings"])
                            manifest["manifest_sha256"] = canonical_sha256(
                                {key: value for key, value in manifest.items() if key != "manifest_sha256"}
                            )
                            retry["report"]["report_sha256"] = canonical_sha256(
                                {key: value for key, value in retry["report"].items() if key != "report_sha256"}
                            )
                            expected_code = "invalid_review_proxy"
                        with self.assertRaises(BallAnnotationServiceError) as rejected:
                            service.create_session(
                                _request(
                                    development_probe_job_ids=[
                                        "probe-development",
                                        "probe-development-retry",
                                    ],
                                    retry_from_session_id=blocked["session_id"],
                                    operator_id=f"operator-audited-{scenario}-retry",
                                )
                            )
                    self.assertEqual(expected_code, rejected.exception.code)
                finally:
                    service.close()

    def test_blocked_development_session_accepts_late_verified_proxy_without_revealing_original(self) -> None:
        root = self.repo_root / "late-verified-proxy-upgrade"
        (root / "data").mkdir(parents=True)
        gateway = _FakeProbeGateway()
        invalid_times = [0.0] * len(gateway.jobs["probe-development"]["report"]["frames"])
        self._set_probe_decoder_times(
            gateway.jobs["probe-development"],
            invalid_times,
        )
        service = BallAnnotationService(
            root,
            get_probe=gateway.get_probe,
            create_probe=gateway.create_probe,
            read_probe_artifact=gateway.read_probe_artifact,
        )
        try:
            blocked_request = _request(
                development_probe_job_ids=["probe-development"],
                operator_id="operator-late-proxy-blocked",
            )
            blocked = service.create_session(blocked_request)
            self.assertEqual("blocked", blocked["status"])
            self.assertEqual("review_proxy_required", blocked["blocker_code"])
            self.assertEqual([], blocked["frames"])
            registry = json.loads(
                (
                    root
                    / "data"
                    / "ball_detector_development_v1"
                    / "annotation_sessions"
                    / "temporal_group_registry.json"
                ).read_text(encoding="utf-8")
            )
            self.assertFalse(any(entry["session_id"] == blocked["session_id"] for entry in registry["entries"]))

            self._set_probe_decoder_times(
                gateway.jobs["probe-development-retry"],
                invalid_times,
            )
            self._attach_review_proxy(
                gateway.jobs["probe-development-retry"],
                [float(index * 100) for index in range(len(invalid_times))],
                gateway.jobs["probe-development"],
            )
            upgraded = service.create_session(
                _request(
                    development_probe_job_ids=[
                        "probe-development",
                        "probe-development-retry",
                    ],
                    retry_from_session_id=blocked["session_id"],
                    operator_id="operator-late-proxy-upgrade",
                )
            )
            self.assertEqual("annotating", upgraded["status"])
            self.assertEqual(blocked["session_id"], upgraded["retry_from_session_id"])
            self.assertEqual(
                "review_proxy_decode_upgrade",
                upgraded["retry_lineage"]["mode"],
            )
            self.assertTrue(all(isinstance(frame["proxy_binding"], dict) for frame in upgraded["frames"]))
            original = service.get_session(blocked["session_id"])
            self.assertEqual("blocked", original["status"])
            self.assertEqual([], original["frames"])
            registry = json.loads(
                (
                    root
                    / "data"
                    / "ball_detector_development_v1"
                    / "annotation_sessions"
                    / "temporal_group_registry.json"
                ).read_text(encoding="utf-8")
            )
            upgraded_entries = [entry for entry in registry["entries"] if entry["session_id"] == upgraded["session_id"]]
            self.assertEqual(
                {group["group_id"] for group in blocked["sampling_manifest"]["groups"]},
                {entry["group_id"] for entry in upgraded_entries},
            )
            self.assertTrue(all(entry["state"] == "revealed" for entry in upgraded_entries))
            self.assertFalse(any(entry["session_id"] == blocked["session_id"] for entry in registry["entries"]))
        finally:
            service.close()

    def test_review_proxy_repair_authority_is_server_derived_from_a_pristine_blocked_session(self) -> None:
        invalid_times = [0.0] * len(self.gateway.jobs["probe-development"]["report"]["frames"])
        self._set_probe_decoder_times(self.gateway.jobs["probe-development"], invalid_times)
        blocked = self.service.create_session(
            _request(
                development_probe_job_ids=["probe-development"],
                operator_id="operator-repair-authority",
            )
        )

        authority = self.service.get_review_proxy_repair_authority(blocked["session_id"])

        self.assertEqual("probe-development", authority["parent_probe_job_id"])
        self.assertEqual(blocked["request_sha256"], authority["blocked_session_request_sha256"])
        self.assertEqual(
            blocked["sampling_manifest"]["manifest_sha256"],
            authority["sampling_manifest_sha256"],
        )
        self.assertEqual(
            [frame["frame_index"] for frame in self.gateway.jobs["probe-development"]["report"]["frames"]],
            authority["frame_indices"],
        )
        self.assertEqual([], blocked["frames"])

    def test_review_proxy_repair_authority_rejects_any_reveal_or_terminal_drift(self) -> None:
        invalid_times = [0.0] * len(self.gateway.jobs["probe-development"]["report"]["frames"])
        self._set_probe_decoder_times(self.gateway.jobs["probe-development"], invalid_times)
        blocked = self.service.create_session(
            _request(
                development_probe_job_ids=["probe-development"],
                operator_id="operator-repair-guards",
            )
        )
        session_path = (
            self.repo_root
            / "data"
            / "ball_detector_development_v1"
            / "annotation_sessions"
            / "sessions"
            / f"{blocked['session_id']}.json"
        )
        pristine = json.loads(session_path.read_text(encoding="utf-8"))
        mutations = {
            "non-development": lambda value: value.__setitem__("data_role", "check"),
            "non-blocked": lambda value: value.__setitem__("status", "annotating"),
            "wrong-blocker": lambda value: value.__setitem__("blocker_code", "other"),
            "frames-revealed": lambda value: value.__setitem__("frames", [{}]),
            "revision-exists": lambda value: value.__setitem__("revisions", [{}]),
            "final-package-exists": lambda value: value.__setitem__("final_package", {}),
            "final-result-exists": lambda value: value.__setitem__("final_result", {}),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                changed = deepcopy(pristine)
                mutate(changed)
                session_path.write_text(json.dumps(changed, sort_keys=True), encoding="utf-8")
                with self.assertRaises(BallAnnotationServiceError) as rejected:
                    self.service.get_review_proxy_repair_authority(blocked["session_id"])
                self.assertEqual("review_proxy_repair_ineligible", rejected.exception.code)
        session_path.write_text(json.dumps(pristine, sort_keys=True), encoding="utf-8")
        self.service._record_groups(
            blocked["session_id"],
            blocked["source"]["sha256"],
            blocked["sampling_manifest"]["groups"],
            data_role="development",
            state="revealed",
        )
        with self.assertRaises(BallAnnotationServiceError) as revealed:
            self.service.get_review_proxy_repair_authority(blocked["session_id"])
        self.assertEqual("review_proxy_repair_ineligible", revealed.exception.code)

    def test_development_retry_rejects_proxy_drift_when_timing_was_valid(self) -> None:
        root = self.repo_root / "unexpected-proxy-drift"
        (root / "data").mkdir(parents=True)
        gateway = _FakeProbeGateway()
        retry = gateway.jobs["probe-development-retry"]
        self._attach_review_proxy(
            retry,
            [float(frame["decoder_reported_pos_msec"]) for frame in retry["report"]["frames"]],
            gateway.jobs["probe-development"],
        )
        service = BallAnnotationService(
            root,
            get_probe=gateway.get_probe,
            create_probe=gateway.create_probe,
            read_probe_artifact=gateway.read_probe_artifact,
        )
        try:
            with self.assertRaises(BallAnnotationServiceError) as rejected:
                service.create_session(_request())
            self.assertEqual("retry_frame_mismatch", rejected.exception.code)
        finally:
            service.close()

    def test_development_retry_rejects_profile_decode_candidate_and_timing_drift(self) -> None:
        for mutation in ("control", "decode", "candidate", "timing"):
            root = self.repo_root / f"retry-drift-{mutation}"
            (root / "data").mkdir(parents=True)
            gateway = _FakeProbeGateway()
            retry = gateway.jobs["probe-development-retry"]
            if mutation == "control":
                control = next(
                    profile
                    for profile in retry["report"]["frozen_profiles"]
                    if profile["profile_id"] == CONTROL_PROFILE
                )
                control["model_descriptor"]["weights"]["sha256"] = "9" * 64
                retry["report"]["lineage"]["frozen_profiles_sha256"] = canonical_sha256(
                    retry["report"]["frozen_profiles"]
                )
            elif mutation == "decode":
                retry["report"]["decode"]["width"] = 63
            elif mutation == "candidate":
                locked = retry["report"]["frames"][0]["profile_results"][1]
                locked["raw_candidates"][0]["bbox_source_px"] = [
                    11.0,
                    10.0,
                    15.0,
                    14.0,
                ]
                locked["display_candidate"] = deepcopy(locked["raw_candidates"][0])
            else:
                retry["report"]["frames"][1]["decoder_reported_pos_msec"] += 1.0
            retry["report"]["report_sha256"] = canonical_sha256(
                {key: value for key, value in retry["report"].items() if key != "report_sha256"}
            )
            service = BallAnnotationService(
                root,
                get_probe=gateway.get_probe,
                create_probe=gateway.create_probe,
                read_probe_artifact=gateway.read_probe_artifact,
            )
            try:
                with self.subTest(mutation=mutation), self.assertRaises(BallAnnotationServiceError):
                    service.create_session(_request(operator_id=f"operator-{mutation}"))
            finally:
                service.close()

    def test_failed_group_registration_does_not_leave_idempotent_half_session(self) -> None:
        with patch.object(
            self.service,
            "_record_groups",
            side_effect=BallAnnotationServiceError("forced", "forced registration failure"),
        ):
            with self.assertRaisesRegex(BallAnnotationServiceError, "forced registration"):
                self.service.create_session(_request(development_probe_job_ids=["probe-development"]))
        sessions = list(
            (self.repo_root / "data" / "ball_detector_development_v1" / "annotation_sessions" / "sessions").glob(
                "*.json"
            )
        )
        self.assertEqual([], sessions)

    def test_finalize_rejects_effective_suggestion(self) -> None:
        session = self.service.create_session(_request(development_probe_job_ids=["probe-development"]))
        for index, frame in enumerate(session["frames"]):
            annotation = _absent()
            if index == 0:
                annotation = {**_check_absent(), "annotation_state": "suggested"}
            self.service.put_annotation(
                session["session_id"],
                frame["frame_index"],
                {
                    "mutation_id": f"suggestion-{frame['frame_index']}",
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": annotation,
                },
                if_match=f'"{frame["annotation_etag"]}"',
            )
        with self.assertRaisesRegex(BallAnnotationServiceError, "human-confirmed"):
            self.service.finalize_session(session["session_id"], "finalize-suggestion")

    def test_check_finalize_rejects_report_changed_after_reveal(self) -> None:
        created = self.service.create_session(self._check_request(development_probe_job_ids=["probe-development"]))
        self.gateway.complete(created["check_probe_job_id"])
        session = self.service.get_session(created["session_id"])
        for frame in session["frames"]:
            candidate = frame["suggested_candidates"][0]
            self.service.put_annotation(
                session["session_id"],
                frame["frame_index"],
                {
                    "mutation_id": f"check-{frame['frame_index']}",
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": _check_absent(),
                    **_dismiss_detector_candidate(candidate),
                },
                if_match=f'"{frame["annotation_etag"]}"',
            )
        job = self.gateway.jobs[created["check_probe_job_id"]]
        candidate = job["report"]["frames"][0]["profile_results"][1]["raw_candidates"][0]
        candidate["bbox_source_px"] = [11.0, 10.0, 15.0, 14.0]
        job["report"]["report_sha256"] = canonical_sha256(
            {key: value for key, value in job["report"].items() if key != "report_sha256"}
        )
        with self.assertRaises(BallAnnotationServiceError) as tampered:
            self.service.finalize_session(session["session_id"], "finalize-tampered")
        self.assertEqual("invalid_feasibility_evidence", tampered.exception.code)

    def test_final_result_manifest_collections_enforce_bounded_canonical_identities(self) -> None:
        def frames(*indices: int) -> list[dict[str, Any]]:
            return [
                {
                    "frame_index": index,
                    "relative_path": f"frames/{index:09d}.jpg",
                }
                for index in indices
            ]

        def reports(*ordinals: int) -> list[dict[str, Any]]:
            return [
                {
                    "job_id": f"propagation-{ordinal:03d}",
                    "relative_path": f"propagation_reports/propagation-{ordinal:03d}.v1.json",
                }
                for ordinal in ordinals
            ]

        self.service._validate_final_result_manifest_collections(frames(0), [])
        self.service._validate_final_result_manifest_collections(
            frames(*range(70)),
            reports(*range(20)),
        )

        invalid_cases: dict[str, tuple[Any, Any]] = {
            "frame-collection-missing": (None, []),
            "frame-collection-empty": ([], []),
            "frame-collection-over-limit": (frames(*range(71)), []),
            "frame-entry-not-object": ([None], []),
            "frame-index-bool": ([{"frame_index": True, "relative_path": "frames/000000001.jpg"}], []),
            "frame-index-negative": ([{"frame_index": -1, "relative_path": "frames/-00000001.jpg"}], []),
            "frame-index-ten-digits": (
                [{"frame_index": 1_000_000_000, "relative_path": "frames/1000000000.jpg"}],
                [],
            ),
            "frame-path-not-zero-padded": ([{"frame_index": 1, "relative_path": "frames/1.jpg"}], []),
            "frame-path-backslash": ([{"frame_index": 1, "relative_path": "frames\\000000001.jpg"}], []),
            "frame-path-traversal": ([{"frame_index": 1, "relative_path": "frames/../000000001.jpg"}], []),
            "frame-order": (frames(1, 0), []),
            "frame-duplicate": (frames(0, 0), []),
            "report-collection-missing": (frames(0), None),
            "report-collection-over-limit": (frames(0), reports(*range(21))),
            "report-entry-not-object": (frames(0), [None]),
            "report-job-unsafe": (
                frames(0),
                [{"job_id": "../unsafe", "relative_path": "propagation_reports/../unsafe.v1.json"}],
            ),
            "report-path-wrong": (
                frames(0),
                [{"job_id": "propagation-000", "relative_path": "reports/propagation-000.v1.json"}],
            ),
            "report-order": (frames(0), reports(1, 0)),
            "report-duplicate": (frames(0), reports(0, 0)),
        }
        for label, (frame_media, propagation_report_files) in invalid_cases.items():
            with self.subTest(label=label):
                with self.assertRaises(BallAnnotationServiceError) as rejected:
                    self.service._validate_final_result_manifest_collections(
                        frame_media,
                        propagation_report_files,
                    )
                self.assertEqual("invalid_final_result", rejected.exception.code)
                self.assertEqual(409, rejected.exception.status_code)

    def test_oversized_final_result_manifest_rejects_before_tree_or_content_reads(self) -> None:
        def frames(count: int) -> list[dict[str, Any]]:
            return [
                {
                    "frame_index": index,
                    "relative_path": f"frames/{index:09d}.jpg",
                }
                for index in range(count)
            ]

        def reports(count: int) -> list[dict[str, Any]]:
            return [
                {
                    "job_id": f"propagation-{index:03d}",
                    "relative_path": f"propagation_reports/propagation-{index:03d}.v1.json",
                }
                for index in range(count)
            ]

        for label, frame_media, propagation_report_files in (
            ("frames-71", frames(71), reports(0)),
            ("reports-21", frames(1), reports(21)),
        ):
            with self.subTest(label=label):
                session_id = f"annotation-{label}"
                root = self.service._final_results_root / session_id
                root.mkdir()
                manifest = {
                    "schema_version": "1.0",
                    "artifact_type": "ball_annotation_final_result_manifest",
                    "session_id": session_id,
                    "frame_media": frame_media,
                    "frame_media_sha256": canonical_sha256(frame_media),
                    "propagation_report_files": propagation_report_files,
                    "propagation_report_files_sha256": canonical_sha256(propagation_report_files),
                }
                manifest["manifest_sha256"] = canonical_sha256(manifest)
                manifest_path = root / "final_result_manifest.v1.json"
                manifest_path.write_text(
                    json.dumps(
                        manifest,
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                    newline="\n",
                )

                with (
                    patch(
                        "football_tracking.ball_annotation_service.read_regular_bytes",
                        wraps=development_read_regular_bytes,
                    ) as read_spy,
                    patch("football_tracking.ball_annotation_service.exact_regular_tree_snapshot") as snapshot_spy,
                ):
                    with self.assertRaises(BallAnnotationServiceError) as rejected:
                        self.service._read_final_result_dir(root, session_id)
                self.assertEqual("invalid_final_result", rejected.exception.code)
                self.assertEqual(409, rejected.exception.status_code)
                snapshot_spy.assert_not_called()
                self.assertEqual(
                    [manifest_path],
                    [Path(call.args[0]) for call in read_spy.call_args_list],
                )

    def test_final_result_recovers_atomically_across_publication_failpoints(self) -> None:
        self.service.close()
        for stage in (
            "after_intent",
            "before_publish",
            "after_publish",
            "after_session_commit",
        ):
            with self.subTest(stage=stage):
                root = self.repo_root / stage
                (root / "data").mkdir(parents=True)
                gateway = _FakeProbeGateway()

                def failpoint(observed: str, *, expected: str = stage) -> None:
                    if observed == expected:
                        raise RuntimeError(f"crash at {observed}")

                interrupted = BallAnnotationService(
                    root,
                    get_probe=gateway.get_probe,
                    create_probe=gateway.create_probe,
                    read_probe_artifact=gateway.read_probe_artifact,
                    finalize_failpoint=failpoint,
                )
                session = interrupted.create_session(
                    _request(
                        development_probe_job_ids=["probe-development"],
                        operator_id=f"operator-{stage}",
                    )
                )
                for frame in session["frames"]:
                    candidate = frame["suggested_candidates"][0]
                    interrupted.put_annotation(
                        session["session_id"],
                        frame["frame_index"],
                        {
                            "mutation_id": f"{stage}-{frame['frame_index']}",
                            "expected_revision": 0,
                            "operation": "set",
                            "undo_revision": None,
                            "annotation": _absent(),
                            **_dismiss_detector_candidate(candidate),
                        },
                        if_match=f'"{frame["annotation_etag"]}"',
                    )
                with self.assertRaisesRegex(RuntimeError, stage):
                    interrupted.finalize_session(session["session_id"], f"finalize-{stage}")
                interrupted.close()

                recovered = BallAnnotationService(
                    root,
                    get_probe=gateway.get_probe,
                    create_probe=gateway.create_probe,
                    read_probe_artifact=gateway.read_probe_artifact,
                )
                try:
                    refreshed = recovered.get_session(session["session_id"])
                    result = recovered.get_final_result(session["session_id"])
                finally:
                    recovered.close()
                self.assertEqual("finalized", refreshed["status"])
                self.assertEqual(
                    refreshed["final_package"]["package_sha256"],
                    result["package"]["package_sha256"],
                )
                final_root = root / "data" / "ball_detector_development_v1" / "annotation_sessions" / "final_results"
                self.assertEqual(
                    [session["session_id"]],
                    sorted(path.name for path in final_root.iterdir() if not path.name.startswith(".staging-")),
                )
                self.assertFalse(any(final_root.glob(".staging-*")))

    def test_after_publish_recovery_rejects_coherently_rehashed_result(self) -> None:
        self.service.close()
        root = self.repo_root / "coherent-after-publish"
        (root / "data").mkdir(parents=True)
        gateway = _FakeProbeGateway()

        def failpoint(stage: str) -> None:
            if stage == "after_publish":
                raise RuntimeError("crash after publish")

        interrupted = BallAnnotationService(
            root,
            get_probe=gateway.get_probe,
            create_probe=gateway.create_probe,
            read_probe_artifact=gateway.read_probe_artifact,
            finalize_failpoint=failpoint,
        )
        session = interrupted.create_session(
            _request(
                development_probe_job_ids=["probe-development"],
                operator_id="operator-coherent-after-publish",
            )
        )
        for frame in session["frames"]:
            candidate = frame["suggested_candidates"][0]
            interrupted.put_annotation(
                session["session_id"],
                frame["frame_index"],
                {
                    "mutation_id": f"coherent-after-{frame['frame_index']}",
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": _absent(),
                    **_dismiss_detector_candidate(candidate),
                },
                if_match=f'"{frame["annotation_etag"]}"',
            )
        with self.assertRaisesRegex(RuntimeError, "after publish"):
            interrupted.finalize_session(session["session_id"], "finalize-coherent-after-publish")
        interrupted.close()

        sealed_root = (
            root
            / "data"
            / "ball_detector_development_v1"
            / "annotation_sessions"
            / "final_results"
            / session["session_id"]
        )
        self._coherently_tamper_final_report(sealed_root)
        recovered = BallAnnotationService(
            root,
            get_probe=gateway.get_probe,
            create_probe=gateway.create_probe,
            read_probe_artifact=gateway.read_probe_artifact,
        )
        try:
            with self.assertRaises(BallAnnotationServiceError) as rejected:
                recovered.get_session(session["session_id"])
            self.assertEqual("invalid_final_result", rejected.exception.code)
        finally:
            recovered.close()

    def test_finalized_anchor_rejects_coherently_rehashed_result(self) -> None:
        result = self._eligible_development_result()
        session_id = result["package"]["session_id"]
        sealed_root = (
            self.repo_root
            / "data"
            / "ball_detector_development_v1"
            / "annotation_sessions"
            / "final_results"
            / session_id
        )
        self._coherently_tamper_final_report(sealed_root)
        with self.assertRaises(BallAnnotationServiceError) as rejected:
            self.service.get_final_result(session_id)
        self.assertEqual("invalid_final_result", rejected.exception.code)

    def test_final_result_rejects_cross_session_report_swap(self) -> None:
        first = self._eligible_development_result()
        second_repo = self.repo_root / "report-swap-source"
        (second_repo / "data").mkdir(parents=True)
        second_gateway = _FakeProbeGateway()
        second_service = BallAnnotationService(
            second_repo,
            get_probe=second_gateway.get_probe,
            create_probe=second_gateway.create_probe,
            read_probe_artifact=second_gateway.read_probe_artifact,
        )
        try:
            second_session = second_service.create_session(
                _request(
                    development_probe_job_ids=["probe-development"],
                    operator_id="operator-report-swap-source",
                )
            )
            for frame in second_session["frames"]:
                candidate = frame["suggested_candidates"][0]
                second_service.put_annotation(
                    second_session["session_id"],
                    frame["frame_index"],
                    {
                        "mutation_id": f"report-swap-{frame['frame_index']}",
                        "expected_revision": 0,
                        "operation": "set",
                        "undo_revision": None,
                        "annotation": _absent(),
                        **_dismiss_detector_candidate(candidate),
                    },
                    if_match=f'"{frame["annotation_etag"]}"',
                )
            second = second_service.finalize_session(second_session["session_id"], "finalize-report-swap-source")
        finally:
            second_service.close()
        final_results_root = (
            self.repo_root / "data" / "ball_detector_development_v1" / "annotation_sessions" / "final_results"
        )
        first_root = final_results_root / first["package"]["session_id"]
        second_root = (
            second_repo
            / "data"
            / "ball_detector_development_v1"
            / "annotation_sessions"
            / "final_results"
            / second["package"]["session_id"]
        )
        swapped_report = (second_root / "feasibility_report.v1.json").read_bytes()
        (first_root / "feasibility_report.v1.json").write_bytes(swapped_report)
        report = json.loads(swapped_report)
        manifest_path = first_root / "final_result_manifest.v1.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(
            {
                "report_file_sha256": hashlib.sha256(swapped_report).hexdigest(),
                "report_file_size_bytes": len(swapped_report),
                "report_sha256": report["report_sha256"],
                "report_status": report["status"],
            }
        )
        manifest["manifest_sha256"] = canonical_sha256(
            {key: value for key, value in manifest.items() if key != "manifest_sha256"}
        )
        manifest_path.write_text(
            json.dumps(
                manifest,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        with self.assertRaises(BallAnnotationServiceError) as rejected:
            self.service.get_final_result(first["package"]["session_id"])
        self.assertEqual("invalid_final_result", rejected.exception.code)

    def test_finalization_requires_untampered_upstream_frame_bytes(self) -> None:
        for mode in ("missing", "tampered"):
            with self.subTest(mode=mode):
                root = self.repo_root / f"upstream-{mode}"
                (root / "data").mkdir(parents=True)
                gateway = _FakeProbeGateway()
                service = BallAnnotationService(
                    root,
                    get_probe=gateway.get_probe,
                    create_probe=gateway.create_probe,
                    read_probe_artifact=gateway.read_probe_artifact,
                )
                try:
                    session = service.create_session(
                        _request(
                            development_probe_job_ids=["probe-development"],
                            operator_id=f"operator-upstream-{mode}",
                        )
                    )
                    for frame in session["frames"]:
                        candidate = frame["suggested_candidates"][0]
                        service.put_annotation(
                            session["session_id"],
                            frame["frame_index"],
                            {
                                "mutation_id": (f"upstream-{mode}-{frame['frame_index']}"),
                                "expected_revision": 0,
                                "operation": "set",
                                "undo_revision": None,
                                "annotation": _absent(),
                                **_dismiss_detector_candidate(candidate),
                            },
                            if_match=f'"{frame["annotation_etag"]}"',
                        )
                    stored = service._load_session(session["session_id"])
                    bound = stored["frames"][0]
                    artifact_key = (
                        bound["_probe_job_id"],
                        bound["_artifact_id"],
                    )
                    if mode == "missing":
                        gateway.artifacts.pop(artifact_key)
                        expected_code = "frame_unavailable"
                    else:
                        gateway.artifacts[artifact_key] = b"changed-frame"
                        expected_code = "frame_digest_mismatch"
                    with self.assertRaises(BallAnnotationServiceError) as failed:
                        service.finalize_session(
                            session["session_id"],
                            f"finalize-upstream-{mode}",
                        )
                    self.assertEqual(expected_code, failed.exception.code)
                    destination = (
                        root
                        / "data"
                        / "ball_detector_development_v1"
                        / "annotation_sessions"
                        / "final_results"
                        / session["session_id"]
                    )
                    self.assertFalse(destination.exists())
                finally:
                    service.close()

    def test_finalized_frames_survive_upstream_deletion_and_reject_sealed_tamper(self) -> None:
        result = self._eligible_development_result()
        session_id = result["package"]["session_id"]
        frame_media = result["package"]["frame_media"][0]
        gateway_artifacts = dict(self.gateway.artifacts)
        self.gateway.artifacts.clear()
        content, media_type, digest = self.service.read_frame(session_id, frame_media["frame_index"])
        self.assertEqual("image/jpeg", media_type)
        self.assertEqual(frame_media["sha256"], digest)
        self.assertEqual(digest, hashlib.sha256(content).hexdigest())
        self.assertEqual(
            result["package"]["package_sha256"],
            self.service.get_final_result(session_id)["package"]["package_sha256"],
        )

        sealed_frame = (
            self.repo_root
            / "data"
            / "ball_detector_development_v1"
            / "annotation_sessions"
            / "final_results"
            / session_id
            / frame_media["relative_path"]
        )
        sealed_frame.write_bytes(b"tampered-sealed-frame")
        for operation in (
            lambda: self.service.get_final_result(session_id),
            lambda: self.service.read_frame(session_id, frame_media["frame_index"]),
        ):
            with self.assertRaises(BallAnnotationServiceError) as rejected:
                operation()
            self.assertEqual("invalid_final_result", rejected.exception.code)
        self.gateway.artifacts.update(gateway_artifacts)

    def test_final_result_rejects_tampered_or_unexpected_sealed_files(self) -> None:
        self.service.close()
        for mode in ("tampered-package", "unexpected-file"):
            with self.subTest(mode=mode):
                root = self.repo_root / mode
                (root / "data").mkdir(parents=True)
                gateway = _FakeProbeGateway()
                service = BallAnnotationService(
                    root,
                    get_probe=gateway.get_probe,
                    create_probe=gateway.create_probe,
                    read_probe_artifact=gateway.read_probe_artifact,
                )
                session = service.create_session(
                    _request(
                        development_probe_job_ids=["probe-development"],
                        operator_id=f"operator-{mode}",
                    )
                )
                for frame in session["frames"]:
                    candidate = frame["suggested_candidates"][0]
                    service.put_annotation(
                        session["session_id"],
                        frame["frame_index"],
                        {
                            "mutation_id": f"{mode}-{frame['frame_index']}",
                            "expected_revision": 0,
                            "operation": "set",
                            "undo_revision": None,
                            "annotation": _absent(),
                            **_dismiss_detector_candidate(candidate),
                        },
                        if_match=f'"{frame["annotation_etag"]}"',
                    )
                service.finalize_session(session["session_id"], f"finalize-{mode}")
                sealed_root = (
                    root
                    / "data"
                    / "ball_detector_development_v1"
                    / "annotation_sessions"
                    / "final_results"
                    / session["session_id"]
                )
                if mode == "tampered-package":
                    package_path = sealed_root / "annotation_package.v1.json"
                    package_path.write_bytes(package_path.read_bytes() + b"\n")
                else:
                    (sealed_root / "unexpected.txt").write_text("not allowlisted", encoding="utf-8")
                with self.assertRaises(BallAnnotationServiceError) as rejected:
                    service.get_final_result(session["session_id"])
                self.assertEqual("invalid_final_result", rejected.exception.code)
                service.close()

    def test_frame_digest_mismatch_fails_closed(self) -> None:
        session = self.service.create_session(_request(development_probe_job_ids=["probe-development"]))
        frame = session["frames"][0]
        raw_session_path = (
            self.repo_root
            / "data"
            / "ball_detector_development_v1"
            / "annotation_sessions"
            / "sessions"
            / f"{session['session_id']}.json"
        )
        raw = json.loads(raw_session_path.read_text(encoding="utf-8"))
        stored = next(item for item in raw["frames"] if item["frame_index"] == frame["frame_index"])
        self.gateway.artifacts[(stored["_probe_job_id"], stored["_artifact_id"])] = b"not-the-bound-frame"
        with self.assertRaisesRegex(BallAnnotationServiceError, "digest"):
            self.service.read_frame(session["session_id"], frame["frame_index"])

    def test_propagation_reserves_twenty_supplemental_frames_before_async_commit(self) -> None:
        session, revisions = self._capacity_session(seed_count=11, suffix="supplemental-reservations")
        frame_indices = sorted(revisions)
        queued = [
            self._queue_capacity_propagation(
                session,
                revisions,
                frame_index,
                mutation_id=f"reserve-supplemental-{frame_index}",
            )
            for frame_index in frame_indices[:10]
        ]
        self.assertEqual(10, len(queued))
        self.assertTrue(all(job["status"] == "waiting_probe" for job in queued))
        self.assertEqual(0, self.service.get_session(session["session_id"])["progress"]["supplemental_total_frames"])

        exact_retry = self._queue_capacity_propagation(
            session,
            revisions,
            frame_indices[0],
            mutation_id=f"reserve-supplemental-{frame_indices[0]}",
        )
        semantic_retry = self._queue_capacity_propagation(
            session,
            revisions,
            frame_indices[0],
            mutation_id="reserve-supplemental-semantic-retry",
        )
        self.assertEqual(queued[0]["job_id"], exact_retry["job_id"])
        self.assertEqual(queued[0]["job_id"], semantic_retry["job_id"])
        self.assertEqual(10, len(self.gateway.create_requests))

        with self.assertRaises(BallAnnotationServiceError) as rejected:
            self._queue_capacity_propagation(
                session,
                revisions,
                frame_indices[10],
                mutation_id="reserve-supplemental-over-limit",
            )
        self.assertEqual("supplemental_frame_limit", rejected.exception.code)
        self.assertEqual(409, rejected.exception.status_code)
        self.assertEqual(10, len(self.gateway.create_requests))

        for job in queued:
            self.gateway.complete(job["neighbor_probe_job_id"])
            ready = self.service.get_propagation_job(session["session_id"], job["job_id"])
            self.assertEqual("ready", ready["status"])
        refreshed = self.service.get_session(session["session_id"])
        self.assertEqual(20, refreshed["progress"]["supplemental_total_frames"])
        self.assertEqual(
            10,
            len({job_id for frame in refreshed["frames"] for job_id in frame.get("propagation_job_ids", [])}),
        )

    def test_propagation_rejects_twenty_first_report_capable_async_job(self) -> None:
        session, revisions = self._capacity_session(seed_count=21, suffix="report-reservations")
        frame_indices = sorted(revisions)
        with patch(
            "football_tracking.ball_annotation_service._MAX_SUPPLEMENTAL_FRAMES_PER_SESSION",
            100,
        ):
            queued = [
                self._queue_capacity_propagation(
                    session,
                    revisions,
                    frame_index,
                    mutation_id=f"reserve-report-{frame_index}",
                )
                for frame_index in frame_indices[:20]
            ]
            self.assertEqual(20, len(queued))
            self.assertEqual(
                0,
                self.service.get_session(session["session_id"])["progress"]["supplemental_total_frames"],
            )
            with self.assertRaises(BallAnnotationServiceError) as rejected:
                self._queue_capacity_propagation(
                    session,
                    revisions,
                    frame_indices[20],
                    mutation_id="reserve-report-over-limit",
                )
        self.assertEqual("propagation_report_limit", rejected.exception.code)
        self.assertEqual(409, rejected.exception.status_code)
        self.assertEqual(20, len(self.gateway.create_requests))

    def test_committing_propagation_restart_cannot_exceed_supplemental_limit(self) -> None:
        session, revisions = self._capacity_session(seed_count=11, suffix="commit-supplemental")
        frame_indices = sorted(revisions)
        with patch(
            "football_tracking.ball_annotation_service._MAX_SUPPLEMENTAL_FRAMES_PER_SESSION",
            100,
        ):
            queued = [
                self._queue_capacity_propagation(
                    session,
                    revisions,
                    frame_index,
                    mutation_id=f"commit-supplemental-{frame_index}",
                )
                for frame_index in frame_indices
            ]
        for job in queued[:10]:
            self.gateway.complete(job["neighbor_probe_job_id"])
            self.assertEqual(
                "ready",
                self.service.get_propagation_job(session["session_id"], job["job_id"])["status"],
            )
        self.assertEqual(20, self.service.get_session(session["session_id"])["progress"]["supplemental_total_frames"])

        crashed = False

        def crash_after_intent(stage: str) -> None:
            nonlocal crashed
            if stage == "after_propagation_commit_intent" and not crashed:
                crashed = True
                raise RuntimeError("crash after overbooked propagation intent")

        self.service._propagation_failpoint = crash_after_intent
        overflow = queued[10]
        self.gateway.complete(overflow["neighbor_probe_job_id"])
        with self.assertRaisesRegex(RuntimeError, "overbooked propagation intent"):
            self.service.get_propagation_job(session["session_id"], overflow["job_id"])
        self.service.close()
        self.service = BallAnnotationService(
            self.repo_root,
            get_probe=self.gateway.get_probe,
            create_probe=self.gateway.create_probe,
            create_propagation_probe=self.gateway.create_probe,
            cancel_propagation_probe=self.gateway.cancel_probe,
            read_probe_artifact=self.gateway.read_probe_artifact,
        )

        failed = self.service.get_propagation_job(session["session_id"], overflow["job_id"])
        self.assertEqual("failed", failed["status"])
        self.assertEqual("supplemental_frame_limit", failed["error_code"])
        refreshed = self.service.get_session(session["session_id"])
        self.assertEqual(20, refreshed["progress"]["supplemental_total_frames"])
        self.assertNotIn(
            overflow["job_id"],
            {job_id for frame in refreshed["frames"] for job_id in frame.get("propagation_job_ids", [])},
        )

    def test_committing_propagation_restart_cannot_exceed_producing_job_limit(self) -> None:
        session, revisions = self._capacity_session(seed_count=21, suffix="commit-reports")
        frame_indices = sorted(revisions)
        overflow = self._queue_capacity_propagation(
            session,
            revisions,
            frame_indices[20],
            mutation_id="commit-report-overflow",
        )
        stored = self.service._load_session(session["session_id"])
        for index, frame in enumerate(stored["frames"][:20]):
            frame["propagation_job_ids"] = [f"legacy-producer-{index}"]
        self.service._persist_session(stored)

        crashed = False

        def crash_after_intent(stage: str) -> None:
            nonlocal crashed
            if stage == "after_propagation_commit_intent" and not crashed:
                crashed = True
                raise RuntimeError("crash after report-overflow intent")

        self.service._propagation_failpoint = crash_after_intent
        self.gateway.complete(overflow["neighbor_probe_job_id"])
        with self.assertRaisesRegex(RuntimeError, "report-overflow intent"):
            self.service.get_propagation_job(session["session_id"], overflow["job_id"])
        self.service.close()
        self.service = BallAnnotationService(
            self.repo_root,
            get_probe=self.gateway.get_probe,
            create_probe=self.gateway.create_probe,
            create_propagation_probe=self.gateway.create_probe,
            cancel_propagation_probe=self.gateway.cancel_probe,
            read_probe_artifact=self.gateway.read_probe_artifact,
        )

        failed = self.service.get_propagation_job(session["session_id"], overflow["job_id"])
        self.assertEqual("failed", failed["status"])
        self.assertEqual("propagation_report_limit", failed["error_code"])
        refreshed = self.service.get_session(session["session_id"])
        self.assertEqual(0, refreshed["progress"]["supplemental_total_frames"])
        self.assertEqual(
            {f"legacy-producer-{index}" for index in range(20)},
            {job_id for frame in refreshed["frames"] for job_id in frame.get("propagation_job_ids", [])},
        )

    def test_propagation_uses_verified_neighbor_probe_and_adds_confirmable_supplemental_frames(self) -> None:
        session = self.service.create_session(_request(development_probe_job_ids=["probe-development"]))
        seed = next(frame for frame in session["frames"] if frame["frame_index"] == 40)
        revision = self.service.put_annotation(
            session["session_id"],
            seed["frame_index"],
            {
                "mutation_id": "seed-box",
                "expected_revision": 0,
                "operation": "set",
                "undo_revision": None,
                "annotation": _present_box(),
            },
            if_match=f'"{seed["annotation_etag"]}"',
        )
        request = {
            "mutation_id": "propagate-seed-box",
            "seed_frame_index": seed["frame_index"],
            "radius_frames": 2,
            "expected_seed_revision": 1,
        }
        with self.assertRaises(BallAnnotationServiceError) as missing:
            self.service.create_propagation_job(session["session_id"], request, if_match=None)
        self.assertEqual(428, missing.exception.status_code)
        queued = self.service.create_propagation_job(
            session["session_id"],
            request,
            if_match=f'"{revision["annotation_etag"]}"',
        )
        self.assertEqual("waiting_probe", queued["status"])
        self.assertEqual([38, 39, 41, 42], queued["target_frame_indices"])
        self.gateway.complete(queued["neighbor_probe_job_id"])
        ready = self.service.get_propagation_job(session["session_id"], queued["job_id"])
        self.assertEqual("ready", ready["status"])
        self.assertGreater(ready["summary"]["succeeded_frame_count"], 0)
        refreshed = self.service.get_session(session["session_id"])
        self.assertEqual(
            [0, 40, 80, 120, 160, 199],
            refreshed["sampling_manifest"]["frame_indices"],
        )
        supplemental = [frame for frame in refreshed["frames"] if frame["frame_role"] == "propagation_target"]
        self.assertEqual([38, 39, 41, 42], [frame["frame_index"] for frame in supplemental])
        self.assertTrue(all(not frame["primary_sample"] for frame in supplemental))
        self.assertTrue(all(frame["temporal_group_id"] == seed["temporal_group_id"] for frame in supplemental))
        self.assertEqual(4, refreshed["progress"]["supplemental_total_frames"])
        for frame in supplemental:
            content, media_type, digest = self.service.read_frame(session["session_id"], frame["frame_index"])
            self.assertEqual("image/jpeg", media_type)
            self.assertEqual(frame["source_frame_sha256"], digest)
            self.assertEqual(digest, hashlib.sha256(content).hexdigest())

        repeated = self.service.get_propagation_job(session["session_id"], queued["job_id"])
        self.assertEqual(ready, repeated)
        self.assertEqual(
            4,
            len(
                [
                    frame
                    for frame in self.service.get_session(session["session_id"])["frames"]
                    if frame["frame_role"] == "propagation_target"
                ]
            ),
        )

    def test_propagation_semantic_idempotency_and_target_conflicts(self) -> None:
        session = self.service.create_session(
            _request(
                development_probe_job_ids=["probe-development"],
                operator_id="operator-propagation-semantic-idempotency",
            )
        )
        seed = next(frame for frame in session["frames"] if frame["frame_index"] == 40)
        revision = self.service.put_annotation(
            session["session_id"],
            seed["frame_index"],
            {
                "mutation_id": "semantic-idempotency-seed",
                "expected_revision": 0,
                "operation": "set",
                "undo_revision": None,
                "annotation": _present_box(),
            },
            if_match=f'"{seed["annotation_etag"]}"',
        )
        request = {
            "mutation_id": "semantic-propagation-first",
            "seed_frame_index": 40,
            "radius_frames": 1,
            "expected_seed_revision": 1,
        }
        first = self.service.create_propagation_job(
            session["session_id"],
            request,
            if_match=f'"{revision["annotation_etag"]}"',
        )
        create_count = len(self.gateway.create_requests)
        repeated = self.service.create_propagation_job(
            session["session_id"],
            {**request, "mutation_id": "semantic-propagation-second"},
            if_match=f'"{revision["annotation_etag"]}"',
        )
        self.assertEqual(first["job_id"], repeated["job_id"])
        self.assertEqual(create_count, len(self.gateway.create_requests))
        propagation_root = (
            self.repo_root / "data" / "ball_detector_development_v1" / "annotation_sessions" / "propagation_jobs"
        )
        self.assertEqual(1, len(list(propagation_root.glob("*.json"))))

        with self.assertRaises(BallAnnotationServiceError) as conflict:
            self.service.create_propagation_job(
                session["session_id"],
                {
                    "mutation_id": "semantic-propagation-overlap",
                    "seed_frame_index": 40,
                    "radius_frames": 2,
                    "expected_seed_revision": 1,
                },
                if_match=f'"{revision["annotation_etag"]}"',
            )
        self.assertEqual("propagation_target_conflict", conflict.exception.code)
        self.assertEqual(409, conflict.exception.status_code)

    def test_propagation_confirmation_is_human_truth_and_updates_both_views_once(self) -> None:
        session, _seed, _revision, ready = self._ready_propagation(mutation_suffix="confirm")
        suggestion = min(
            ready["suggestions"],
            key=lambda item: abs(item["frame_index"] - ready["seed_frame_index"]),
        )
        refreshed = self.service.get_session(session["session_id"])
        frame = next(item for item in refreshed["frames"] if item["frame_index"] == suggestion["frame_index"])
        pending_before = refreshed["progress"]["unconfirmed_propagation_suggestions"]
        body = {
            "mutation_id": "confirm-propagation-once",
            "expected_revision": 0,
            "operation": "set",
            "undo_revision": None,
            "annotation": _confirmed_from_suggestion(suggestion),
            **_accept_propagation_suggestion(suggestion),
        }
        incomplete = {
            key: value
            for key, value in body.items()
            if key
            not in {
                "accepted_suggestion_job_id",
                "accepted_suggestion_sha256",
            }
        }
        with self.assertRaises(BallAnnotationServiceError) as missing_binding:
            self.service.put_annotation(
                session["session_id"],
                frame["frame_index"],
                incomplete,
                if_match=f'"{frame["annotation_etag"]}"',
            )
        self.assertEqual("invalid_suggestion", missing_binding.exception.code)
        self.assertEqual(400, missing_binding.exception.status_code)
        for field, value in (
            ("accepted_suggestion_job_id", "propagation-wrong"),
            ("accepted_suggestion_sha256", "f" * 64),
        ):
            with self.subTest(field=field), self.assertRaises(BallAnnotationServiceError) as tampered:
                self.service.put_annotation(
                    session["session_id"],
                    frame["frame_index"],
                    {**body, "mutation_id": f"tampered-propagation-{field}", field: value},
                    if_match=f'"{frame["annotation_etag"]}"',
                )
            self.assertEqual("suggestion_binding_mismatch", tampered.exception.code)
            self.assertEqual(409, tampered.exception.status_code)
        accepted = self.service.put_annotation(
            session["session_id"],
            frame["frame_index"],
            body,
            if_match=f'"{frame["annotation_etag"]}"',
        )
        self.assertEqual(suggestion["suggestion_id"], accepted["accepted_suggestion_id"])
        self.assertEqual(ready["job_id"], accepted["accepted_suggestion_job_id"])
        self.assertEqual(64, len(accepted["accepted_suggestion_sha256"]))

        after = self.service.get_session(session["session_id"])
        after_frame = next(item for item in after["frames"] if item["frame_index"] == suggestion["frame_index"])
        frame_suggestion = next(
            item
            for item in after_frame["propagation_suggestions"]
            if item["suggestion_id"] == suggestion["suggestion_id"]
        )
        self.assertFalse(frame_suggestion["pending_human_confirmation"])
        self.assertEqual(0.0, frame_suggestion["human_confirmation"]["center_error_px"])
        self.assertEqual(
            pending_before - 1,
            after["progress"]["unconfirmed_propagation_suggestions"],
        )
        job = self.service.get_propagation_job(session["session_id"], ready["job_id"])
        job_suggestion = next(
            item for item in job["suggestions"] if item["suggestion_id"] == suggestion["suggestion_id"]
        )
        result = next(item for item in job["frame_results"] if item["suggestion_id"] == suggestion["suggestion_id"])
        self.assertEqual(
            frame_suggestion["human_confirmation"],
            job_suggestion["human_confirmation"],
        )
        self.assertEqual(job_suggestion["human_confirmation"], result["human_confirmation"])
        self.assertEqual(1, job["summary"]["human_validated_frame_count"])
        self.assertEqual(1, job["summary"]["human_validated_safe_span_frames"])

        repeated = self.service.put_annotation(
            session["session_id"],
            frame["frame_index"],
            body,
            if_match=f'"{frame["annotation_etag"]}"',
        )
        self.assertEqual(accepted, repeated)
        with self.assertRaises(BallAnnotationServiceError) as duplicate:
            self.service.put_annotation(
                session["session_id"],
                frame["frame_index"],
                {**body, "mutation_id": "confirm-propagation-twice", "expected_revision": 1},
                if_match=f'"{accepted["annotation_etag"]}"',
            )
        self.assertEqual("suggestion_already_decided", duplicate.exception.code)
        self.assertEqual(409, duplicate.exception.status_code)

        second = next(item for item in ready["suggestions"] if item["suggestion_id"] != suggestion["suggestion_id"])
        current = next(
            item
            for item in self.service.get_session(session["session_id"])["frames"]
            if item["frame_index"] == second["frame_index"]
        )
        with self.assertRaises(BallAnnotationServiceError) as absent:
            self.service.put_annotation(
                session["session_id"],
                second["frame_index"],
                {
                    "mutation_id": "reject-absent-suggestion",
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": _absent(),
                    **_accept_propagation_suggestion(second),
                },
                if_match=f'"{current["annotation_etag"]}"',
            )
        self.assertEqual("suggestion_not_localizable", absent.exception.code)
        dismissed = self.service.put_annotation(
            session["session_id"],
            second["frame_index"],
            {
                "mutation_id": "dismiss-propagation-suggestion",
                "expected_revision": 0,
                "operation": "set",
                "undo_revision": None,
                "annotation": _absent(),
                **_dismiss_propagation_suggestion(second),
            },
            if_match=f'"{current["annotation_etag"]}"',
        )
        self.assertEqual(
            second["suggestion_job_id"],
            dismissed["dismissed_suggestion_job_id"],
        )
        self.assertEqual(
            second["suggestion_sha256"],
            dismissed["dismissed_suggestion_sha256"],
        )

    def test_confirmation_intent_recovers_after_session_write_before_job_write(self) -> None:
        self.service.close()
        root = self.repo_root / "confirmation-recovery"
        (root / "data").mkdir(parents=True)
        gateway = _FakeProbeGateway()
        crashed = False

        def fail_once(stage: str) -> None:
            nonlocal crashed
            if stage == "after_confirmation_intent" and not crashed:
                crashed = True
                raise RuntimeError("crash after confirmation intent")

        interrupted = BallAnnotationService(
            root,
            get_probe=gateway.get_probe,
            create_probe=gateway.create_probe,
            create_propagation_probe=gateway.create_probe,
            cancel_propagation_probe=gateway.cancel_probe,
            read_probe_artifact=gateway.read_probe_artifact,
            confirmation_failpoint=fail_once,
        )
        session, _seed, _seed_revision, ready = self._ready_propagation(
            service=interrupted,
            gateway=gateway,
            mutation_suffix="confirmation-recovery",
        )
        suggestion = ready["suggestions"][0]
        frame = next(
            item
            for item in interrupted.get_session(session["session_id"])["frames"]
            if item["frame_index"] == suggestion["frame_index"]
        )
        body = {
            "mutation_id": "recover-confirmation",
            "expected_revision": 0,
            "operation": "set",
            "undo_revision": None,
            "annotation": _confirmed_from_suggestion(suggestion, shift_x=1.0),
            **_accept_propagation_suggestion(suggestion),
        }
        with self.assertRaisesRegex(RuntimeError, "confirmation intent"):
            interrupted.put_annotation(
                session["session_id"],
                frame["frame_index"],
                body,
                if_match=f'"{frame["annotation_etag"]}"',
            )
        interrupted.close()

        recovered = BallAnnotationService(
            root,
            get_probe=gateway.get_probe,
            create_probe=gateway.create_probe,
            create_propagation_probe=gateway.create_probe,
            cancel_propagation_probe=gateway.cancel_probe,
            read_probe_artifact=gateway.read_probe_artifact,
        )
        try:
            restored = recovered.get_session(session["session_id"])
            restored_frame = next(
                item for item in restored["frames"] if item["frame_index"] == suggestion["frame_index"]
            )
            restored_suggestion = next(
                item
                for item in restored_frame["propagation_suggestions"]
                if item["suggestion_id"] == suggestion["suggestion_id"]
            )
            self.assertFalse(restored_suggestion["pending_human_confirmation"])
            self.assertTrue(restored_suggestion["human_confirmation"]["corrected"])
            job = recovered.get_propagation_job(session["session_id"], ready["job_id"])
            self.assertEqual(1, job["summary"]["human_validated_frame_count"])
            self.assertEqual(0, job["summary"]["human_validated_safe_span_frames"])
            repeated = recovered.put_annotation(
                session["session_id"],
                frame["frame_index"],
                body,
                if_match=f'"{frame["annotation_etag"]}"',
            )
            self.assertEqual(1, repeated["revision"])
            self.assertEqual(
                1,
                recovered.get_propagation_job(session["session_id"], ready["job_id"])["summary"][
                    "human_validated_frame_count"
                ],
            )
        finally:
            recovered.close()

    def test_propagation_probe_creation_and_commit_are_restart_idempotent(self) -> None:
        self.service.close()
        for fail_stage in (
            "after_neighbor_probe_create",
            "after_propagation_session_commit",
        ):
            with self.subTest(fail_stage=fail_stage):
                root = self.repo_root / fail_stage
                (root / "data").mkdir(parents=True)
                gateway = _FakeProbeGateway()
                triggered = False

                def fail_once(stage: str, *, expected: str = fail_stage) -> None:
                    nonlocal triggered
                    if stage == expected and not triggered:
                        triggered = True
                        raise RuntimeError(f"crash at {stage}")

                interrupted = BallAnnotationService(
                    root,
                    get_probe=gateway.get_probe,
                    create_probe=gateway.create_probe,
                    create_propagation_probe=gateway.create_probe,
                    cancel_propagation_probe=gateway.cancel_probe,
                    read_probe_artifact=gateway.read_probe_artifact,
                    propagation_failpoint=fail_once,
                )
                session = interrupted.create_session(
                    _request(
                        development_probe_job_ids=["probe-development"],
                        operator_id=f"operator-{fail_stage}",
                    )
                )
                seed = next(item for item in session["frames"] if item["frame_index"] == 40)
                seed_revision = interrupted.put_annotation(
                    session["session_id"],
                    40,
                    {
                        "mutation_id": f"seed-{fail_stage}",
                        "expected_revision": 0,
                        "operation": "set",
                        "undo_revision": None,
                        "annotation": _present_box(),
                    },
                    if_match=f'"{seed["annotation_etag"]}"',
                )
                request = {
                    "mutation_id": f"propagation-{fail_stage}",
                    "seed_frame_index": 40,
                    "radius_frames": 2,
                    "expected_seed_revision": 1,
                }
                if fail_stage == "after_neighbor_probe_create":
                    with self.assertRaisesRegex(RuntimeError, fail_stage):
                        interrupted.create_propagation_job(
                            session["session_id"],
                            request,
                            if_match=f'"{seed_revision["annotation_etag"]}"',
                        )
                    job_path = next(
                        (
                            root / "data" / "ball_detector_development_v1" / "annotation_sessions" / "propagation_jobs"
                        ).glob("*.json")
                    )
                    job_id = job_path.stem
                    child_id = next(iter(gateway.jobs_by_request_sha256.values()))
                else:
                    queued = interrupted.create_propagation_job(
                        session["session_id"],
                        request,
                        if_match=f'"{seed_revision["annotation_etag"]}"',
                    )
                    job_id = queued["job_id"]
                    child_id = queued["neighbor_probe_job_id"]
                    gateway.complete(child_id)
                    with self.assertRaisesRegex(RuntimeError, fail_stage):
                        interrupted.get_propagation_job(session["session_id"], job_id)
                interrupted.close()

                recovered = BallAnnotationService(
                    root,
                    get_probe=gateway.get_probe,
                    create_probe=gateway.create_probe,
                    create_propagation_probe=gateway.create_probe,
                    cancel_propagation_probe=gateway.cancel_probe,
                    read_probe_artifact=gateway.read_probe_artifact,
                )
                try:
                    resumed = recovered.get_propagation_job(session["session_id"], job_id)
                    if resumed["status"] == "waiting_probe":
                        self.assertEqual(child_id, resumed["neighbor_probe_job_id"])
                        gateway.complete(child_id)
                        resumed = recovered.get_propagation_job(session["session_id"], job_id)
                    self.assertEqual("ready", resumed["status"])
                    self.assertEqual(
                        4,
                        len(
                            [
                                item
                                for item in recovered.get_session(session["session_id"])["frames"]
                                if item["frame_role"] == "propagation_target"
                            ]
                        ),
                    )
                    self.assertEqual(1, len(gateway.jobs_by_request_sha256))
                finally:
                    recovered.close()

    def test_propagation_cancel_cancels_child_and_never_revives(self) -> None:
        session = self.service.create_session(
            _request(
                development_probe_job_ids=["probe-development"],
                operator_id="operator-cancel",
            )
        )
        seed = next(item for item in session["frames"] if item["frame_index"] == 40)
        revision = self.service.put_annotation(
            session["session_id"],
            40,
            {
                "mutation_id": "cancel-seed",
                "expected_revision": 0,
                "operation": "set",
                "undo_revision": None,
                "annotation": _present_box(),
            },
            if_match=f'"{seed["annotation_etag"]}"',
        )
        queued = self.service.create_propagation_job(
            session["session_id"],
            {
                "mutation_id": "cancel-propagation",
                "seed_frame_index": 40,
                "radius_frames": 2,
                "expected_seed_revision": 1,
            },
            if_match=f'"{revision["annotation_etag"]}"',
        )
        cancelled = self.service.cancel_propagation_job(session["session_id"], queued["job_id"])
        self.assertEqual("cancelled", cancelled["status"])
        self.assertEqual("cancelled", cancelled["neighbor_probe_cancel_status"])
        self.assertEqual([queued["neighbor_probe_job_id"]], self.gateway.cancel_requests)
        self.gateway.complete(queued["neighbor_probe_job_id"])
        still_cancelled = self.service.get_propagation_job(session["session_id"], queued["job_id"])
        self.assertEqual("cancelled", still_cancelled["status"])
        self.assertFalse(
            any(
                item["frame_role"] == "propagation_target"
                for item in self.service.get_session(session["session_id"])["frames"]
            )
        )

        second_session = self.service.get_session(session["session_id"])
        second_seed = next(item for item in second_session["frames"] if item["frame_index"] == 80)
        second_revision = self.service.put_annotation(
            second_session["session_id"],
            80,
            {
                "mutation_id": "cancel-failure-seed",
                "expected_revision": 0,
                "operation": "set",
                "undo_revision": None,
                "annotation": _present_box(),
            },
            if_match=f'"{second_seed["annotation_etag"]}"',
        )
        second = self.service.create_propagation_job(
            second_session["session_id"],
            {
                "mutation_id": "cancel-failure-propagation",
                "seed_frame_index": 80,
                "radius_frames": 2,
                "expected_seed_revision": 1,
            },
            if_match=f'"{second_revision["annotation_etag"]}"',
        )
        self.gateway.cancel_error = RuntimeError("child cancellation unavailable")
        failed_cancel = self.service.cancel_propagation_job(second_session["session_id"], second["job_id"])
        self.assertEqual("cancelled", failed_cancel["status"])
        self.assertEqual("cancel_failed", failed_cancel["neighbor_probe_cancel_status"])
        self.assertEqual(
            "child_cancel_failed",
            failed_cancel["neighbor_probe_cancel_error_code"],
        )

    def test_pending_propagation_fails_closed_when_seed_is_deleted_or_replaced(self) -> None:
        session = self.service.create_session(
            _request(
                development_probe_job_ids=["probe-development"],
                operator_id="operator-stale-propagation",
            )
        )
        for seed_index, replacement in ((40, None), (80, _present_box())):
            with self.subTest(seed_index=seed_index):
                seed = next(
                    item
                    for item in self.service.get_session(session["session_id"])["frames"]
                    if item["frame_index"] == seed_index
                )
                seed_revision = self.service.put_annotation(
                    session["session_id"],
                    seed_index,
                    {
                        "mutation_id": f"stale-seed-{seed_index}",
                        "expected_revision": 0,
                        "operation": "set",
                        "undo_revision": None,
                        "annotation": _present_box(),
                    },
                    if_match=f'"{seed["annotation_etag"]}"',
                )
                queued = self.service.create_propagation_job(
                    session["session_id"],
                    {
                        "mutation_id": f"stale-propagation-{seed_index}",
                        "seed_frame_index": seed_index,
                        "radius_frames": 2,
                        "expected_seed_revision": 1,
                    },
                    if_match=f'"{seed_revision["annotation_etag"]}"',
                )
                if replacement is None:
                    mutation = {
                        "mutation_id": f"delete-seed-{seed_index}",
                        "expected_revision": 1,
                        "operation": "delete",
                        "undo_revision": None,
                        "annotation": None,
                    }
                else:
                    replacement = deepcopy(replacement)
                    replacement["point_source_px"]["x"] += 1.0
                    replacement["bbox_source_px"]["left"] += 1.0
                    replacement["bbox_source_px"]["right"] += 1.0
                    mutation = {
                        "mutation_id": f"replace-seed-{seed_index}",
                        "expected_revision": 1,
                        "operation": "set",
                        "undo_revision": None,
                        "annotation": replacement,
                    }
                self.service.put_annotation(
                    session["session_id"],
                    seed_index,
                    mutation,
                    if_match=f'"{seed_revision["annotation_etag"]}"',
                )
                self.gateway.complete(queued["neighbor_probe_job_id"])
                failed = self.service.get_propagation_job(session["session_id"], queued["job_id"])
                self.assertEqual("failed", failed["status"])
                self.assertEqual("propagation_seed_authority_stale", failed["error_code"])
                self.assertEqual([], failed["suggestions"])
                self.assertEqual([], failed["frame_results"])
        self.assertFalse(
            any(
                item["frame_role"] == "propagation_target"
                for item in self.service.get_session(session["session_id"])["frames"]
            )
        )

    def test_committing_propagation_revalidates_seed_before_session_publish(self) -> None:
        triggered = False

        def crash_after_commit_intent(stage: str) -> None:
            nonlocal triggered
            if stage == "after_propagation_commit_intent" and not triggered:
                triggered = True
                raise RuntimeError("crash after propagation commit intent")

        self.service._propagation_failpoint = crash_after_commit_intent
        session = self.service.create_session(
            _request(
                development_probe_job_ids=["probe-development"],
                operator_id="operator-committing-stale",
            )
        )
        seed = next(item for item in session["frames"] if item["frame_index"] == 40)
        seed_revision = self.service.put_annotation(
            session["session_id"],
            40,
            {
                "mutation_id": "committing-stale-seed",
                "expected_revision": 0,
                "operation": "set",
                "undo_revision": None,
                "annotation": _present_box(),
            },
            if_match=f'"{seed["annotation_etag"]}"',
        )
        queued = self.service.create_propagation_job(
            session["session_id"],
            {
                "mutation_id": "committing-stale-propagation",
                "seed_frame_index": 40,
                "radius_frames": 2,
                "expected_seed_revision": 1,
            },
            if_match=f'"{seed_revision["annotation_etag"]}"',
        )
        self.gateway.complete(queued["neighbor_probe_job_id"])
        with self.assertRaisesRegex(RuntimeError, "commit intent"):
            self.service.get_propagation_job(session["session_id"], queued["job_id"])

        replacement = _present_box()
        replacement["point_source_px"]["x"] += 1.0
        replacement["bbox_source_px"]["left"] += 1.0
        replacement["bbox_source_px"]["right"] += 1.0
        self.service.put_annotation(
            session["session_id"],
            40,
            {
                "mutation_id": "committing-stale-replacement",
                "expected_revision": 1,
                "operation": "set",
                "undo_revision": None,
                "annotation": replacement,
            },
            if_match=f'"{seed_revision["annotation_etag"]}"',
        )
        failed = self.service.get_propagation_job(session["session_id"], queued["job_id"])
        self.assertEqual("failed", failed["status"])
        self.assertEqual("propagation_seed_authority_stale", failed["error_code"])
        self.assertEqual([], failed["suggestions"])
        self.assertEqual([], failed["frame_results"])
        self.assertFalse(
            any(
                item["frame_role"] == "propagation_target"
                for item in self.service.get_session(session["session_id"])["frames"]
            )
        )

    def test_pending_propagation_is_cancelled_before_session_finalization(self) -> None:
        session = self.service.create_session(
            _request(
                development_probe_job_ids=["probe-development"],
                operator_id="operator-finalize-pending",
            )
        )
        seed = next(item for item in session["frames"] if item["frame_index"] == 40)
        seed_revision = self.service.put_annotation(
            session["session_id"],
            40,
            {
                "mutation_id": "finalize-pending-seed",
                "expected_revision": 0,
                "operation": "set",
                "undo_revision": None,
                "annotation": _present_box(),
                **_accept_detector_candidate(seed["suggested_candidates"][0]),
            },
            if_match=f'"{seed["annotation_etag"]}"',
        )
        queued = self.service.create_propagation_job(
            session["session_id"],
            {
                "mutation_id": "finalize-pending-propagation",
                "seed_frame_index": 40,
                "radius_frames": 2,
                "expected_seed_revision": 1,
            },
            if_match=f'"{seed_revision["annotation_etag"]}"',
        )
        current = self.service.get_session(session["session_id"])
        for frame in current["frames"]:
            if frame["frame_index"] == 40:
                continue
            candidate = frame["suggested_candidates"][0]
            self.service.put_annotation(
                session["session_id"],
                frame["frame_index"],
                {
                    "mutation_id": f"finalize-pending-{frame['frame_index']}",
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": _absent(),
                    **_dismiss_detector_candidate(candidate),
                },
                if_match=f'"{frame["annotation_etag"]}"',
            )
        result = self.service.finalize_session(session["session_id"], "finalize-with-pending-propagation")
        self.assertEqual(session["session_id"], result["package"]["session_id"])
        failed = self.service.get_propagation_job(session["session_id"], queued["job_id"])
        self.assertEqual("failed", failed["status"])
        self.assertEqual("propagation_session_finalized", failed["error_code"])
        self.assertEqual("cancelled", failed["neighbor_probe_cancel_status"])
        self.gateway.complete(queued["neighbor_probe_job_id"])
        self.assertEqual(
            "failed",
            self.service.get_propagation_job(session["session_id"], queued["job_id"])["status"],
        )


if __name__ == "__main__":
    unittest.main()
