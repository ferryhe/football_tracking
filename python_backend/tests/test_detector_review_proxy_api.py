from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from pydantic import ValidationError

import football_tracking.api.app as api_app_module
from football_tracking.api.app import create_app
from football_tracking.api.dependencies import get_service
from football_tracking.api.schemas import DetectorReviewProxyRepairJobResponse
from football_tracking.api.service import ApiService
from football_tracking.detector_development_common import DetectorDevelopmentError

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


class ApiApplicationLifespanTests(unittest.TestCase):
    def test_module_app_import_does_not_construct_api_service(self) -> None:
        self.assertFalse(hasattr(api_app_module.app.state, "api_service"))

    def test_default_app_constructs_and_closes_service_inside_lifespan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo_root = Path(temporary)
            service = mock.Mock()
            with mock.patch.object(api_app_module, "ApiService", return_value=service) as factory:
                application = api_app_module.create_app(repo_root)
                factory.assert_not_called()
                self.assertFalse(hasattr(application.state, "api_service"))

                with TestClient(application):
                    factory.assert_called_once_with(repo_root)
                    self.assertIs(service, application.state.api_service)
                    service.close.assert_not_called()

                service.close.assert_called_once_with()
                self.assertFalse(hasattr(application.state, "api_service"))

    def test_initialize_service_false_stays_empty_through_lifespan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo_root = Path(temporary)
            with mock.patch.object(api_app_module, "ApiService") as factory:
                application = api_app_module.create_app(repo_root, initialize_service=False)
                with TestClient(application):
                    factory.assert_not_called()
                    self.assertFalse(hasattr(application.state, "api_service"))
                factory.assert_not_called()
                self.assertFalse(hasattr(application.state, "api_service"))


def _queued_repair() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "artifact_type": "detector_review_proxy_repair_job",
        "repair_id": "proxy-repair-1",
        "attempt_root_repair_id": "proxy-repair-1",
        "attempt_number": 1,
        "retry_from_repair_id": None,
        "idempotency_key": SHA_A,
        "request_sha256": SHA_A,
        "status": "queued",
        "stage": "queued",
        "preset_id": "h264-cfr-720p-v1",
        "eligibility": {
            "eligible": True,
            "action": "generate_verified_review_proxy",
            "blocker_code": "review_proxy_required",
        },
        "authority": {
            "blocked_session_id": "annotation-blocked-1",
            "blocked_session_request_sha256": SHA_A,
            "blocked_session_record_sha256": SHA_B,
            "parent_probe_job_id": "probe-parent-1",
            "development_probe_job_ids": ["probe-parent-1"],
            "parent_probe_request_sha256": SHA_B,
            "parent_probe_intent_sha256": SHA_C,
            "parent_probe_semantic_intent_sha256": SHA_D,
            "parent_probe_report_sha256": SHA_D,
            "parent_probe_result_manifest_sha256": SHA_A,
            "parent_probe_record_sha256": SHA_B,
            "parent_execution_bundle_sha256": SHA_C,
            "parent_runtime_environment_sha256": SHA_D,
            "source_frame_evidence_sha256": SHA_A,
            "source_id": "source-1",
            "source_sha256": SHA_C,
            "source_file_identity_sha256": SHA_D,
            "source_size_bytes": 11_258_707_917,
            "source_width": 5120,
            "source_height": 1440,
            "source_frame_count": 104_820,
            "source_fps": 20.0,
            "locked_profile_id": "official-coco-yolo11n-sahi",
            "locked_profile_sha256": SHA_A,
            "frame_indices": [1500, 1560, 1620, 1679, 1739, 1799],
            "sampling_manifest_sha256": SHA_B,
            "temporal_groups_sha256": SHA_C,
            "candidate_evidence_sha256": SHA_D,
            "replacement_request_authority_sha256": SHA_A,
        },
        "progress": {
            "stage_completed": 0,
            "stage_total": 6,
            "source_frames_completed": 0,
            "source_frames_total": 104_820,
            "updated_at": "2026-07-18T12:00:00+00:00",
        },
        "can_cancel": True,
        "can_retry": False,
        "result": None,
        "error_code": None,
        "blocker_code": None,
        "recovery_action": None,
        "created_at": "2026-07-18T12:00:00+00:00",
        "updated_at": "2026-07-18T12:00:00+00:00",
        "status_url": "/api/v1/detector-review-proxy-repairs/proxy-repair-1",
        "cancel_url": "/api/v1/detector-review-proxy-repairs/proxy-repair-1/cancel",
        "retry_url": "/api/v1/detector-review-proxy-repairs/proxy-repair-1/retry",
    }


_STAGE_RANKS = {
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


def _ready_result() -> dict[str, object]:
    child = {
        "job_id": "probe-child-1",
        "request_sha256": SHA_A,
        "intent_sha256": SHA_B,
        "semantic_intent_sha256": SHA_C,
        "resource_sha256": SHA_D,
        "frozen_profiles_sha256": SHA_A,
        "report_sha256": SHA_B,
        "result_manifest_sha256": SHA_C,
        "execution_bundle_sha256": SHA_D,
        "runtime_environment_sha256": SHA_A,
        "continuation_execution_binding_sha256": SHA_B,
        "continuation_code_bundle_sha256": SHA_C,
        "continuation_runtime_sha256": SHA_D,
        "retry_from_job_id": "probe-parent-1",
        "retry_kind": "review_proxy_decode_upgrade",
        "status_url": "/api/v1/detector-probes/probe-child-1",
        "report_url": "/api/v1/detector-probes/probe-child-1",
    }
    replacement = {
        "session_id": "annotation-replacement-1",
        "request_sha256": SHA_A,
        "status": "annotating",
        "retry_from_session_id": "annotation-blocked-1",
        "retry_mode": "review_proxy_decode_upgrade",
        "attempt_family_sha256": SHA_B,
        "development_probe_job_ids": [
            "probe-parent-1",
            "probe-child-1",
        ],
        "status_url": "/api/v1/ball-annotation-sessions/annotation-replacement-1",
    }
    return {
        "proxy": {
            "review_proxy_id": "proxy-repair-1",
            "review_proxy_manifest_sha256": SHA_A,
            "proxy_media_sha256": SHA_B,
            "proxy_size_bytes": 123,
            "proxy_width": 2560,
            "proxy_height": 720,
            "proxy_frame_count": 104_820,
            "proxy_fps": 20.0,
            "mapping_sha256": SHA_C,
            "sampled_artifact_count": 6,
            "encoder_binding_sha256": SHA_D,
            "repair_execution_binding_sha256": SHA_A,
            "repair_code_bundle_sha256": SHA_B,
            "repair_runtime_sha256": SHA_C,
            "repair_decoder_fingerprint_sha256": SHA_D,
        },
        "child_probe": child,
        "replacement_session": replacement,
        "parent_probe_record_sha256_after": SHA_B,
    }


def _repair_lifecycle(status: str, stage: str) -> dict[str, object]:
    value = deepcopy(_queued_repair())
    rank = _STAGE_RANKS[stage]
    value.update(
        status=status,
        stage=stage,
        can_cancel=status in {"queued", "running"},
        can_retry=False,
        result=None,
        error_code=None,
        blocker_code=None,
        recovery_action=None,
    )
    value["progress"]["stage_completed"] = rank
    value["progress"]["source_frames_completed"] = value["progress"]["source_frames_total"] if rank >= 1 else 0
    if status == "ready":
        value["result"] = _ready_result()
    elif status in {"failed", "blocked"}:
        value["error_code"] = "review_proxy_failed" if rank <= 2 else "post_commit_failure"
        value["blocker_code"] = value["error_code"] if status == "blocked" else None
        value["recovery_action"] = "retry" if rank <= 2 else "resume"
        value["can_retry"] = rank <= 2
    elif status == "cancelled":
        value["can_retry"] = True
    return value


class _FakeService:
    def __init__(self) -> None:
        self.create_requests: list[dict[str, object]] = []
        self.cancelled: list[str] = []
        self.retried: list[str] = []

    def create_detector_review_proxy_repair(self, request: dict[str, object]) -> dict[str, object]:
        self.create_requests.append(request)
        return _queued_repair()

    def get_detector_review_proxy_repair(self, repair_id: str) -> dict[str, object]:
        assert repair_id == "proxy-repair-1"
        return _queued_repair()

    def cancel_detector_review_proxy_repair(self, repair_id: str) -> dict[str, object]:
        self.cancelled.append(repair_id)
        result = _queued_repair()
        result.update(
            {
                "status": "cancelled",
                "stage": "cancelled",
                "can_cancel": False,
                "can_retry": True,
                "updated_at": "2026-07-18T12:01:00+00:00",
            }
        )
        result["progress"]["updated_at"] = result["updated_at"]
        return result

    def retry_detector_review_proxy_repair(self, repair_id: str) -> dict[str, object]:
        self.retried.append(repair_id)
        result = _queued_repair()
        result.update(
            {
                "repair_id": "proxy-repair-2",
                "attempt_root_repair_id": "proxy-repair-1",
                "attempt_number": 2,
                "retry_from_repair_id": "proxy-repair-1",
                "status_url": "/api/v1/detector-review-proxy-repairs/proxy-repair-2",
                "cancel_url": "/api/v1/detector-review-proxy-repairs/proxy-repair-2/cancel",
                "retry_url": "/api/v1/detector-review-proxy-repairs/proxy-repair-2/retry",
            }
        )
        return result


class DetectorReviewProxyRepairApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temporary.name)
        for name in ("config", "data", "outputs", "weights"):
            (self.repo_root / name).mkdir(parents=True, exist_ok=True)
        self.service = _FakeService()
        app = create_app(self.repo_root, initialize_service=False)
        app.dependency_overrides[get_service] = lambda: self.service
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

    def test_public_repair_schema_enforces_complete_lifecycle_matrix(self) -> None:
        valid_cases: list[tuple[str, str]] = []
        valid_cases.extend(("queued", stage) for stage in ("proxy_queued", "queued", "recovered_after_restart"))
        valid_cases.extend(
            ("running", stage)
            for stage in (
                "queued",
                "running",
                "verifying_source",
                "transcoding",
                "independent_verification",
                "recovered_after_restart",
            )
        )
        valid_cases.extend(
            ("committing", stage)
            for stage in (
                "proxy_committing",
                "proxy_ready",
                "continuation_intent",
                "child_probe_ready",
                "replacement_session_ready",
                "groups_published",
            )
        )
        valid_cases.append(("ready", "ready"))
        for status in ("failed", "blocked"):
            valid_cases.append((status, status))
            valid_cases.extend(
                (status, stage)
                for stage in (
                    "proxy_ready",
                    "continuation_intent",
                    "child_probe_ready",
                    "replacement_session_ready",
                    "groups_published",
                )
            )
        valid_cases.append(("cancelled", "cancelled"))

        for status, stage in valid_cases:
            with self.subTest(valid_status=status, valid_stage=stage):
                payload = _repair_lifecycle(status, stage)
                self.assertEqual(
                    payload,
                    DetectorReviewProxyRepairJobResponse.model_validate(payload).model_dump(mode="json"),
                )

        for error_code in ("source_changed", "path_unavailable"):
            with self.subTest(retryable_error_code=error_code):
                retryable = _repair_lifecycle("failed", "failed")
                retryable.update(
                    error_code=error_code,
                    recovery_action="retry",
                    can_retry=True,
                )
                self.assertEqual(
                    retryable,
                    DetectorReviewProxyRepairJobResponse.model_validate(retryable).model_dump(mode="json"),
                )

        resumable_evidence: dict[str, object] | None = None
        for stage in ("proxy_ready", "continuation_intent"):
            with self.subTest(resumable_evidence_stage=stage):
                resumable_evidence = _repair_lifecycle("blocked", stage)
                resumable_evidence.update(
                    error_code="invalid_review_proxy_repair_evidence",
                    blocker_code="invalid_review_proxy_repair_evidence",
                    recovery_action="resume",
                    can_retry=False,
                )
                self.assertEqual(
                    resumable_evidence,
                    DetectorReviewProxyRepairJobResponse.model_validate(resumable_evidence).model_dump(mode="json"),
                )

        invalid_cases: list[tuple[str, dict[str, object]]] = []
        assert resumable_evidence is not None
        resumable_without_action = deepcopy(resumable_evidence)
        resumable_without_action["recovery_action"] = None
        invalid_cases.append(("resumable evidence without action", resumable_without_action))
        resumable_as_retry = deepcopy(resumable_evidence)
        resumable_as_retry.update(recovery_action="retry", can_retry=True)
        invalid_cases.append(("resumable evidence as new retry", resumable_as_retry))
        unknown_stage = _repair_lifecycle("queued", "queued")
        unknown_stage["stage"] = "invented"
        invalid_cases.append(("unknown stage", unknown_stage))
        impossible_combo = _repair_lifecycle("queued", "queued")
        impossible_combo.update(
            stage="ready",
            recovery_action="resume",
            can_retry=True,
            can_cancel=1,
        )
        invalid_cases.append(("impossible public combination", impossible_combo))
        wrong_rank = _repair_lifecycle("committing", "proxy_ready")
        wrong_rank["progress"]["stage_completed"] = 0
        invalid_cases.append(("stage rank", wrong_rank))
        wrong_total = _repair_lifecycle("queued", "queued")
        wrong_total["progress"]["stage_total"] = 5
        invalid_cases.append(("stage total", wrong_total))
        incomplete_durable = _repair_lifecycle("committing", "proxy_ready")
        incomplete_durable["progress"]["source_frames_completed"] = 1
        invalid_cases.append(("durable source completion", incomplete_durable))
        stale_progress = _repair_lifecycle("queued", "queued")
        stale_progress["progress"]["updated_at"] = "stale"
        invalid_cases.append(("progress timestamp", stale_progress))
        integer_retry = _repair_lifecycle("queued", "queued")
        integer_retry["can_retry"] = 1
        invalid_cases.append(("strict retry boolean", integer_retry))
        active_error = _repair_lifecycle("running", "running")
        active_error["error_code"] = "review_proxy_failed"
        invalid_cases.append(("active error", active_error))
        active_recovery = _repair_lifecycle("committing", "proxy_committing")
        active_recovery["recovery_action"] = "resume"
        invalid_cases.append(("active recovery", active_recovery))
        ready_without_result = _repair_lifecycle("ready", "ready")
        ready_without_result["result"] = None
        invalid_cases.append(("ready without result", ready_without_result))
        ready_with_error = _repair_lifecycle("ready", "ready")
        ready_with_error["error_code"] = "forged"
        invalid_cases.append(("ready with error", ready_with_error))
        failed_without_error = _repair_lifecycle("failed", "failed")
        failed_without_error["error_code"] = None
        invalid_cases.append(("failed without error", failed_without_error))
        failed_with_blocker = _repair_lifecycle("failed", "failed")
        failed_with_blocker["blocker_code"] = "forged"
        invalid_cases.append(("failed with blocker", failed_with_blocker))
        blocked_without_blocker = _repair_lifecycle("blocked", "blocked")
        blocked_without_blocker["blocker_code"] = None
        invalid_cases.append(("blocked without blocker", blocked_without_blocker))
        blocked_with_different_blocker = _repair_lifecycle("blocked", "blocked")
        blocked_with_different_blocker["blocker_code"] = "different_blocker"
        invalid_cases.append(("blocked with different blocker", blocked_with_different_blocker))
        nonretryable_rank_zero = _repair_lifecycle("failed", "failed")
        nonretryable_rank_zero["error_code"] = "permanent_failure"
        invalid_cases.append(("nonretryable rank zero", nonretryable_rank_zero))
        path_without_retry = _repair_lifecycle("failed", "failed")
        path_without_retry.update(
            error_code="path_unavailable",
            recovery_action=None,
            can_retry=False,
        )
        invalid_cases.append(("path unavailable without retry", path_without_retry))
        post_effect_retry = _repair_lifecycle("failed", "child_probe_ready")
        post_effect_retry.update(
            error_code="path_unavailable",
            recovery_action="retry",
            can_retry=True,
        )
        invalid_cases.append(("post-effect retry", post_effect_retry))
        cancelled_with_error = _repair_lifecycle("cancelled", "cancelled")
        cancelled_with_error["error_code"] = "cancelled"
        invalid_cases.append(("cancelled error", cancelled_with_error))
        cancelled_with_result = _repair_lifecycle("cancelled", "cancelled")
        cancelled_with_result["result"] = _ready_result()
        invalid_cases.append(("cancelled result", cancelled_with_result))

        for label, payload in invalid_cases:
            with self.subTest(invalid=label), self.assertRaises(ValidationError):
                DetectorReviewProxyRepairJobResponse.model_validate(payload)

    def test_create_get_and_cancel_expose_server_owned_repair_contract(self) -> None:
        response = self.client.post(
            "/api/v1/detector-review-proxy-repairs",
            json={"blocked_session_id": "annotation-blocked-1"},
        )

        self.assertEqual(202, response.status_code, response.text)
        self.assertEqual("no-store", response.headers.get("cache-control"))
        self.assertEqual(
            [{"blocked_session_id": "annotation-blocked-1"}],
            self.service.create_requests,
        )
        payload = response.json()
        self.assertEqual("queued", payload["status"])
        self.assertTrue(payload["eligibility"]["eligible"])
        self.assertEqual(104_820, payload["authority"]["source_frame_count"])
        self.assertIsNone(payload["result"])

        fetched = self.client.get(payload["status_url"])
        self.assertEqual(200, fetched.status_code, fetched.text)
        self.assertEqual("no-store", fetched.headers.get("cache-control"))
        cancelled = self.client.post(payload["cancel_url"])
        self.assertEqual(200, cancelled.status_code, cancelled.text)
        self.assertEqual("cancelled", cancelled.json()["status"])
        self.assertEqual(["proxy-repair-1"], self.service.cancelled)
        retried = self.client.post(payload["retry_url"], json={})
        self.assertEqual(202, retried.status_code, retried.text)
        self.assertEqual(2, retried.json()["attempt_number"])
        self.assertEqual(["proxy-repair-1"], self.service.retried)

    def test_request_forbids_client_media_and_lineage_authority(self) -> None:
        forged = {
            "source_relative_path": "data/forged.mp4",
            "source_sha256": SHA_A,
            "profile_ids": ["forged"],
            "frame_indices": [1],
            "review_proxy_path": "C:/forged.mp4",
            "runtime_environment_sha256": SHA_B,
            "execution_bundle_sha256": SHA_C,
            "retry_kind": "review_proxy_decode_upgrade",
            "parent_probe_job_id": "probe-forged-parent",
        }
        for field, value in forged.items():
            with self.subTest(field=field):
                response = self.client.post(
                    "/api/v1/detector-review-proxy-repairs",
                    json={
                        "blocked_session_id": "annotation-blocked-1",
                        field: value,
                    },
                )
                self.assertEqual(422, response.status_code, response.text)
                self.assertEqual("no-store", response.headers.get("cache-control"))

    def test_all_repair_error_responses_are_no_store(self) -> None:
        cases = (
            (
                "create_detector_review_proxy_repair",
                "post",
                "/api/v1/detector-review-proxy-repairs",
                {"json": {"blocked_session_id": "annotation-blocked-1"}},
            ),
            (
                "get_detector_review_proxy_repair",
                "get",
                "/api/v1/detector-review-proxy-repairs/proxy-repair-1",
                {},
            ),
            (
                "cancel_detector_review_proxy_repair",
                "post",
                "/api/v1/detector-review-proxy-repairs/proxy-repair-1/cancel",
                {},
            ),
            (
                "retry_detector_review_proxy_repair",
                "post",
                "/api/v1/detector-review-proxy-repairs/proxy-repair-1/retry",
                {"json": {}},
            ),
        )
        for service_method, http_method, path, kwargs in cases:
            with self.subTest(path=path):
                original = getattr(self.service, service_method)
                setattr(
                    self.service,
                    service_method,
                    mock.Mock(
                        side_effect=DetectorDevelopmentError(
                            "repair_conflict",
                            "repair conflict",
                            status_code=409,
                        )
                    ),
                )
                try:
                    response = getattr(self.client, http_method)(path, **kwargs)
                finally:
                    setattr(self.service, service_method, original)
                self.assertEqual(409, response.status_code, response.text)
                self.assertEqual("no-store", response.headers.get("cache-control"))

    def test_blocked_session_capability_is_server_verified_and_nullable(self) -> None:
        service = ApiService(self.repo_root)
        session = {
            "session_id": "annotation-blocked-1",
            "data_role": "development",
            "status": "blocked",
            "blocker_code": "review_proxy_required",
        }
        annotation = mock.Mock()
        annotation.get_review_proxy_repair_authority.return_value = {
            "parent_probe_job_id": "probe-parent-1",
            "blocked_session_record_sha256": SHA_A,
            "frame_indices": [1500, 1560],
        }
        detector = mock.Mock()
        detector.get_review_proxy_upgrade_parent.return_value = {
            "parent_probe_job_id": "probe-parent-1",
            "parent_probe_report_sha256": SHA_B,
            "parent_probe_result_manifest_sha256": SHA_C,
            "parent_probe_record_sha256": SHA_D,
            "frame_indices": [1500, 1560],
        }
        try:
            with (
                mock.patch.object(service, "_ball_annotation_service", return_value=annotation),
                mock.patch.object(service, "_detector_development_service", return_value=detector),
            ):
                capable = service._with_review_proxy_repair_capability(session)
                detector.get_review_proxy_upgrade_parent.return_value["frame_indices"] = [1500, 1561]
                mismatched = service._with_review_proxy_repair_capability(session)
                ordinary = service._with_review_proxy_repair_capability(
                    {**session, "status": "annotating", "blocker_code": None}
                )
        finally:
            service.close()

        self.assertEqual(True, capable["review_proxy_repair"]["eligible"])
        self.assertEqual(
            "/api/v1/detector-review-proxy-repairs",
            capable["review_proxy_repair"]["create_url"],
        )
        self.assertIsNone(mismatched["review_proxy_repair"])
        self.assertIsNone(ordinary["review_proxy_repair"])


if __name__ == "__main__":
    unittest.main()
