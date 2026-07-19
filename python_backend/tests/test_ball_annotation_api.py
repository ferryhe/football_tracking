from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any

from audited_authority_test_support import patched_audited_t2_probe_bindings
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_ball_annotation_service import (
    _absent,
    _FakeProbeGateway,
    _present_box,
    _request,
)

from football_tracking.api.app import create_app
from football_tracking.api.dependencies import get_service
from football_tracking.api.schemas import (
    BallAnnotationFinalResultResponse,
    BallAnnotationSessionResponse,
)
from football_tracking.ball_annotation_service import BallAnnotationService


def _accepted_candidate_annotation() -> dict[str, Any]:
    return {
        **_present_box(),
        "provenance": "detector_candidate_human_confirmed",
    }


def _dismissed_candidate_annotation() -> dict[str, Any]:
    return {
        **_absent(),
        "provenance": "suggestion_dismissed_manual",
    }


def _accepted_candidate_binding(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "suggestion_kind": "detector_candidate",
        "suggestion_id": candidate["candidate_id"],
        "accepted_suggestion_job_id": candidate["suggestion_job_id"],
        "accepted_suggestion_sha256": candidate["suggestion_sha256"],
    }


def _dismissed_candidate_binding(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "dismissed_suggestion_kind": "detector_candidate",
        "dismissed_suggestion_id": candidate["candidate_id"],
        "dismissed_suggestion_job_id": candidate["suggestion_job_id"],
        "dismissed_suggestion_sha256": candidate["suggestion_sha256"],
    }


class _BallAnnotationApiFacade:
    def __init__(self, service: BallAnnotationService) -> None:
        self.service = service

    def create_ball_annotation_session(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.service.create_session(request)

    def get_ball_annotation_session(self, session_id: str) -> dict[str, Any]:
        return self.service.get_session(session_id)

    def read_ball_annotation_frame(self, session_id: str, frame_index: int) -> tuple[bytes, str, str]:
        return self.service.read_frame(session_id, frame_index)

    def put_ball_annotation(
        self,
        session_id: str,
        frame_index: int,
        request: dict[str, Any],
        *,
        if_match: str | None,
    ) -> dict[str, Any]:
        return self.service.put_annotation(session_id, frame_index, request, if_match=if_match)

    def create_ball_propagation_job(
        self,
        session_id: str,
        request: dict[str, Any],
        *,
        if_match: str | None,
    ) -> dict[str, Any]:
        return self.service.create_propagation_job(session_id, request, if_match=if_match)

    def get_ball_propagation_job(self, session_id: str, job_id: str) -> dict[str, Any]:
        return self.service.get_propagation_job(session_id, job_id)

    def cancel_ball_propagation_job(self, session_id: str, job_id: str) -> dict[str, Any]:
        return self.service.cancel_propagation_job(session_id, job_id)

    def finalize_ball_annotation_session(self, session_id: str, mutation_id: str) -> dict[str, Any]:
        return self.service.finalize_session(session_id, mutation_id)

    def get_ball_annotation_result(self, session_id: str) -> dict[str, Any]:
        return self.service.get_final_result(session_id)


class BallAnnotationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temporary.name)
        (self.repo_root / "data").mkdir()
        self._audit_patch = patched_audited_t2_probe_bindings()
        audit_bindings = self._audit_patch.__enter__()
        self.gateway = _FakeProbeGateway()
        self.gateway.audit_bindings = audit_bindings
        self.service = BallAnnotationService(
            self.repo_root,
            get_probe=self.gateway.get_probe,
            create_probe=self.gateway.create_probe,
            read_probe_artifact=self.gateway.read_probe_artifact,
        )
        app = create_app(initialize_service=False)
        app.dependency_overrides[get_service] = lambda: _BallAnnotationApiFacade(self.service)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.service.close()
        self._audit_patch.__exit__(None, None, None)
        self.temporary.cleanup()

    def _development_binding(self) -> dict[str, Any]:
        session = self.service.create_session(
            _request(
                development_probe_job_ids=["probe-development"],
                operator_id="api-development-binding",
            )
        )
        for index, frame in enumerate(session["frames"]):
            candidate = frame["suggested_candidates"][0]
            self.service.put_annotation(
                session["session_id"],
                frame["frame_index"],
                {
                    "mutation_id": f"api-development-{frame['frame_index']}",
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": (
                        _accepted_candidate_annotation() if index == 0 else _dismissed_candidate_annotation()
                    ),
                    **(
                        _accepted_candidate_binding(candidate)
                        if index == 0
                        else _dismissed_candidate_binding(candidate)
                    ),
                },
                if_match=f'"{frame["annotation_etag"]}"',
            )
        return self.service.finalize_session(session["session_id"], "api-development-finalize")["package"]

    def _check_request(self) -> dict[str, Any]:
        package = self._development_binding()
        return _request(
            data_role="check",
            development_probe_job_ids=["probe-development"],
            development_package_session_id=package["session_id"],
            development_package_sha256=package["package_sha256"],
            operator_id="api-check",
        )

    def test_check_session_async_probe_lifecycle_is_publicly_parseable(self) -> None:
        response = self.client.post("/api/v1/ball-annotation-sessions", json=self._check_request())
        self.assertEqual(202, response.status_code, response.text)
        created = response.json()
        self.assertEqual("check_probe_queued", created["status"])
        self.assertIsNotNone(created["check_probe_job_id"])
        self.assertIsNone(created["check_probe_authority"])
        self.assertEqual([], created["frames"])
        self.assertEqual("no-store", response.headers["cache-control"])

        sampling = deepcopy(created)
        sampling.update({"status": "sampling_locked", "stage": "sampling_locked"})
        BallAnnotationSessionResponse.model_validate(sampling)
        forged_pre_ready = deepcopy(created)
        forged_pre_ready["check_probe_authority"] = {"job_id": "forged-authority"}
        with self.assertRaises(ValidationError):
            BallAnnotationSessionResponse.model_validate(forged_pre_ready)

        job_id = created["check_probe_job_id"]
        for probe_status, session_status in (
            ("running", "check_probe_running"),
            ("committing", "check_probe_committing"),
        ):
            self.gateway.jobs[job_id]["status"] = probe_status
            active = self.client.get(f"/api/v1/ball-annotation-sessions/{created['session_id']}")
            self.assertEqual(200, active.status_code, active.text)
            self.assertEqual(session_status, active.json()["status"])
            self.assertIsNone(active.json()["check_probe_authority"])
            self.assertEqual("no-store", active.headers["cache-control"])

        self.gateway.complete(job_id)
        ready = self.client.get(f"/api/v1/ball-annotation-sessions/{created['session_id']}")
        self.assertEqual(200, ready.status_code, ready.text)
        self.assertEqual("annotating", ready.json()["status"])
        self.assertEqual(job_id, ready.json()["check_probe_authority"]["job_id"])
        forged_ready = ready.json()
        forged_ready["check_probe_authority"] = None
        with self.assertRaises(ValidationError):
            BallAnnotationSessionResponse.model_validate(forged_ready)

    def test_check_session_blocked_before_probe_ready_has_no_authority(self) -> None:
        created = self.client.post("/api/v1/ball-annotation-sessions", json=self._check_request()).json()
        job_id = created["check_probe_job_id"]
        self.gateway.jobs[job_id].update(
            {
                "status": "failed",
                "error_code": "synthetic_infrastructure_failure",
            }
        )

        blocked = self.client.get(f"/api/v1/ball-annotation-sessions/{created['session_id']}")

        self.assertEqual(200, blocked.status_code, blocked.text)
        self.assertEqual("blocked", blocked.json()["status"])
        self.assertEqual(job_id, blocked.json()["check_probe_job_id"])
        self.assertIsNone(blocked.json()["check_probe_authority"])
        self.assertEqual([], blocked.json()["frames"])

    def test_check_finalize_rejects_manual_annotations_with_pending_candidates(self) -> None:
        created = self.client.post("/api/v1/ball-annotation-sessions", json=self._check_request()).json()
        self.gateway.complete(created["check_probe_job_id"])
        ready_response = self.client.get(f"/api/v1/ball-annotation-sessions/{created['session_id']}")
        self.assertEqual(200, ready_response.status_code, ready_response.text)
        ready = ready_response.json()
        self.assertTrue(
            all(
                candidate["decision"] == "pending"
                for frame in ready["frames"]
                for candidate in frame["suggested_candidates"]
            )
        )
        for frame in ready["frames"]:
            annotated = self.client.put(
                f"/api/v1/ball-annotation-sessions/{ready['session_id']}/annotations/{frame['frame_index']}",
                json={
                    "mutation_id": f"api-unresolved-check-{frame['frame_index']}",
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": {**_absent(), "training_use": "excluded"},
                },
                headers={"If-Match": f'"{frame["annotation_etag"]}"'},
            )
            self.assertEqual(200, annotated.status_code, annotated.text)

        finalized = self.client.post(
            f"/api/v1/ball-annotation-sessions/{ready['session_id']}/finalize",
            json={"mutation_id": "api-finalize-unresolved-check"},
        )

        self.assertEqual(409, finalized.status_code, finalized.text)
        self.assertEqual("suggestion_decisions_incomplete", finalized.json()["detail"]["code"])

    def test_session_frame_annotation_finalize_and_result_routes(self) -> None:
        response = self.client.post(
            "/api/v1/ball-annotation-sessions",
            json=_request(development_probe_job_ids=["probe-development"]),
        )
        self.assertEqual(202, response.status_code, response.text)
        session = response.json()
        self.assertEqual(6, session["sampling_manifest"]["target_frame_count"])
        self.assertEqual("no-store", response.headers["cache-control"])

        frame = session["frames"][0]
        image = self.client.get(frame["frame_url"])
        self.assertEqual(200, image.status_code)
        self.assertEqual("image/jpeg", image.headers["content-type"])
        self.assertEqual(str(len(image.content)), image.headers["content-length"])
        self.assertEqual("no-store", image.headers["cache-control"])
        self.assertEqual(f'"{frame["source_frame_sha256"]}"', image.headers["etag"])
        self.assertEqual(frame["source_frame_sha256"], image.headers["x-content-sha256"])
        self.assertEqual(str(frame["frame_index"]), image.headers["x-source-frame-index"])

        body = {
            "mutation_id": "api-mutation-zero",
            "expected_revision": 0,
            "operation": "set",
            "undo_revision": None,
            "annotation": _dismissed_candidate_annotation(),
            **_dismissed_candidate_binding(frame["suggested_candidates"][0]),
        }
        missing = self.client.put(
            f"/api/v1/ball-annotation-sessions/{session['session_id']}/annotations/{frame['frame_index']}",
            json=body,
        )
        self.assertEqual(428, missing.status_code)
        self.assertEqual({"code", "message"}, set(missing.json()["detail"]))
        self.assertEqual("precondition_required", missing.json()["detail"]["code"])
        stale = self.client.put(
            f"/api/v1/ball-annotation-sessions/{session['session_id']}/annotations/{frame['frame_index']}",
            json=body,
            headers={"If-Match": f'"{"0" * 64}"'},
        )
        self.assertEqual(412, stale.status_code)
        self.assertEqual("precondition_failed", stale.json()["detail"]["code"])
        accepted = self.client.put(
            f"/api/v1/ball-annotation-sessions/{session['session_id']}/annotations/{frame['frame_index']}",
            json=body,
            headers={"If-Match": f'"{frame["annotation_etag"]}"'},
        )
        self.assertEqual(200, accepted.status_code, accepted.text)
        self.assertEqual(f'"{accepted.json()["annotation_etag"]}"', accepted.headers["etag"])

        refreshed = self.client.get(f"/api/v1/ball-annotation-sessions/{session['session_id']}").json()
        for pending in refreshed["frames"]:
            if pending["frame_index"] == frame["frame_index"]:
                continue
            put = self.client.put(
                f"/api/v1/ball-annotation-sessions/{session['session_id']}/annotations/{pending['frame_index']}",
                json={
                    "mutation_id": f"api-{pending['frame_index']}",
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": _dismissed_candidate_annotation(),
                    **_dismissed_candidate_binding(pending["suggested_candidates"][0]),
                },
                headers={"If-Match": f'"{pending["annotation_etag"]}"'},
            )
            self.assertEqual(200, put.status_code, put.text)
        finalized = self.client.post(
            f"/api/v1/ball-annotation-sessions/{session['session_id']}/finalize",
            json={"mutation_id": "api-finalize"},
        )
        self.assertEqual(200, finalized.status_code, finalized.text)
        result = self.client.get(f"/api/v1/ball-annotation-sessions/{session['session_id']}/result")
        self.assertEqual(finalized.json(), result.json())

    def test_result_before_finalization_is_api_safe_conflict(self) -> None:
        created = self.client.post(
            "/api/v1/ball-annotation-sessions",
            json=_request(development_probe_job_ids=["probe-development"]),
        ).json()

        response = self.client.get(f"/api/v1/ball-annotation-sessions/{created['session_id']}/result")

        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual({"code", "message"}, set(response.json()["detail"]))
        self.assertEqual("result_not_ready", response.json()["detail"]["code"])

    def test_openapi_declares_binary_frame_headers_and_preconditions(self) -> None:
        paths = self.client.get("/openapi.json").json()["paths"]
        frame_get = paths["/api/v1/ball-annotation-sessions/{session_id}/frames/{frame_index}"]["get"]
        frame_ok = frame_get["responses"]["200"]
        self.assertEqual(
            {"image/jpeg"},
            set(frame_ok["content"]),
        )
        self.assertEqual(
            {"type": "string", "format": "binary"},
            frame_ok["content"]["image/jpeg"]["schema"],
        )
        self.assertEqual(
            {
                "Content-Length",
                "ETag",
                "X-Content-SHA256",
                "X-Source-Frame-Index",
                "Cache-Control",
            },
            set(frame_ok["headers"]),
        )

        annotation_put = paths["/api/v1/ball-annotation-sessions/{session_id}/annotations/{frame_index}"]["put"]
        self.assertIn("ETag", annotation_put["responses"]["200"]["headers"])
        for status_code in ("412", "428"):
            self.assertEqual(
                "#/components/schemas/BallApiErrorResponse",
                annotation_put["responses"][status_code]["content"]["application/json"]["schema"]["$ref"],
            )

        result_get = paths["/api/v1/ball-annotation-sessions/{session_id}/result"]["get"]
        self.assertEqual(
            "#/components/schemas/BallApiErrorResponse",
            result_get["responses"]["409"]["content"]["application/json"]["schema"]["$ref"],
        )

    def test_safety_critical_response_views_forbid_nested_extra_fields(self) -> None:
        session = self.service.create_session(_request(development_probe_job_ids=["probe-development"]))
        BallAnnotationSessionResponse.model_validate(session)
        for field_name in ("lineage", "sampling_manifest"):
            forged = deepcopy(session)
            forged[field_name]["untrusted_extra"] = True
            with self.subTest(field_name=field_name), self.assertRaises(ValidationError):
                BallAnnotationSessionResponse.model_validate(forged)

        for index, frame in enumerate(session["frames"]):
            candidate = frame["suggested_candidates"][0]
            self.service.put_annotation(
                session["session_id"],
                frame["frame_index"],
                {
                    "mutation_id": f"schema-{frame['frame_index']}",
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": (
                        _accepted_candidate_annotation() if index == 0 else _dismissed_candidate_annotation()
                    ),
                    **(
                        _accepted_candidate_binding(candidate)
                        if index == 0
                        else _dismissed_candidate_binding(candidate)
                    ),
                },
                if_match=f'"{frame["annotation_etag"]}"',
            )
        final_result = self.service.finalize_session(session["session_id"], "schema-finalize")
        finalized_session = self.service.get_session(session["session_id"])
        BallAnnotationSessionResponse.model_validate(finalized_session)
        forged_pointer = deepcopy(finalized_session)
        forged_pointer["final_package"]["untrusted_extra"] = True
        with self.assertRaises(ValidationError):
            BallAnnotationSessionResponse.model_validate(forged_pointer)

        BallAnnotationFinalResultResponse.model_validate(final_result)

        for field_name in ("package", "feasibility_report"):
            forged = deepcopy(final_result)
            forged[field_name]["untrusted_extra"] = True
            with self.subTest(final_field=field_name), self.assertRaises(ValidationError):
                BallAnnotationFinalResultResponse.model_validate(forged)

        check = self.service.create_session(
            _request(
                data_role="check",
                development_probe_job_ids=["probe-development"],
                development_package_session_id=final_result["package"]["session_id"],
                development_package_sha256=final_result["package"]["package_sha256"],
            )
        )
        self.gateway.complete(check["check_probe_job_id"])
        check = self.service.get_session(check["session_id"])
        BallAnnotationSessionResponse.model_validate(check)
        forged_authority = deepcopy(check)
        forged_authority["check_probe_authority"]["untrusted_extra"] = True
        with self.assertRaises(ValidationError):
            BallAnnotationSessionResponse.model_validate(forged_authority)

        check_absent = {**_absent(), "training_use": "excluded"}
        for frame in check["frames"]:
            candidate = frame["suggested_candidates"][0]
            self.service.put_annotation(
                check["session_id"],
                frame["frame_index"],
                {
                    "mutation_id": f"check-schema-{frame['frame_index']}",
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": check_absent,
                    **_dismissed_candidate_binding(candidate),
                },
                if_match=f'"{frame["annotation_etag"]}"',
            )
        check_result = self.service.finalize_session(check["session_id"], "check-schema-finalize")
        BallAnnotationFinalResultResponse.model_validate(check_result)
        self.assertEqual("insufficient_evidence", check_result["feasibility_report"]["status"])
        forged_check_report = deepcopy(check_result)
        forged_check_report["feasibility_report"]["support"]["untrusted_extra"] = True
        with self.assertRaises(ValidationError):
            BallAnnotationFinalResultResponse.model_validate(forged_check_report)

    def test_session_schema_rejects_frame_role_and_identity_bypasses(self) -> None:
        session = self.service.create_session(_request(development_probe_job_ids=["probe-development"]))
        primary = deepcopy(session["frames"][0])

        false_primary = deepcopy(session)
        false_primary["frames"][0]["primary_sample"] = False
        false_primary["frames"][0]["frame_role"] = "propagation_target"

        missing_primary = deepcopy(session)
        missing_primary["frames"][0].pop("primary_sample")

        duplicate = deepcopy(session)
        duplicate["frames"].append(deepcopy(primary))

        false_with_true_clone = deepcopy(false_primary)
        false_with_true_clone["frames"].append(deepcopy(primary))

        integer_bool = deepcopy(session)
        integer_bool["frames"][0]["primary_sample"] = 1

        for name, forged in (
            ("false_primary", false_primary),
            ("missing_primary", missing_primary),
            ("duplicate", duplicate),
            ("false_with_true_clone", false_with_true_clone),
            ("integer_bool", integer_bool),
        ):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                BallAnnotationSessionResponse.model_validate(forged)

    def test_public_schema_rejects_internal_authority_and_role_target_confusion(self) -> None:
        forged = self.client.post(
            "/api/v1/ball-annotation-sessions",
            json={
                **_request(development_probe_job_ids=["probe-development"]),
                "annotation_sampling_manifest_sha256": "f" * 64,
            },
        )
        self.assertEqual(422, forged.status_code)
        development_target = self.client.post(
            "/api/v1/ball-annotation-sessions",
            json=_request(
                development_probe_job_ids=["probe-development"],
                target_frame_count=20,
            ),
        )
        self.assertEqual(422, development_target.status_code)
        check_request = _request(
            data_role="check",
            development_probe_job_ids=["probe-development"],
        )
        check_request["target_frame_count"] = None
        check_without_target = self.client.post(
            "/api/v1/ball-annotation-sessions",
            json=check_request,
        )
        self.assertEqual(422, check_without_target.status_code)
        zero_quota = _request(
            data_role="check",
            development_probe_job_ids=["probe-development"],
        )
        zero_quota["strata_applicability"]["lighting"][0].update({"quota": 0, "frame_intervals": []})
        zero_quota["strata_applicability"]["lighting"][1].update(
            {
                "quota": 20,
                "frame_intervals": [{"start_frame": 0, "end_frame": 199}],
            }
        )
        rejected_zero_quota = self.client.post("/api/v1/ball-annotation-sessions", json=zero_quota)
        self.assertEqual(422, rejected_zero_quota.status_code)

        insufficient_quota = _request(
            data_role="check",
            development_probe_job_ids=["probe-development"],
        )
        insufficient_quota["strata_applicability"]["lighting"][0]["quota"] = 2
        insufficient_quota["strata_applicability"]["lighting"][1]["quota"] = 18
        rejected_insufficient_quota = self.client.post("/api/v1/ball-annotation-sessions", json=insufficient_quota)
        self.assertEqual(422, rejected_insufficient_quota.status_code)

    def test_check_point_only_cannot_bypass_backend_through_api(self) -> None:
        created = self.client.post(
            "/api/v1/ball-annotation-sessions",
            json=self._check_request(),
        ).json()
        self.gateway.complete(created["check_probe_job_id"])
        ready = self.client.get(f"/api/v1/ball-annotation-sessions/{created['session_id']}").json()
        frame = ready["frames"][0]
        point_only = {
            "point_source_px": {"x": 12.0, "y": 12.0},
            "bbox_source_px": None,
            "presence": "present",
            "visibility": "visible",
            "training_use": "excluded",
            "annotation_state": "confirmed",
            "scale_stratum": "far",
            "lighting_tag": "bright_sun",
            "motion_occlusion_tags": [],
            "provenance": "manual_human_annotation",
        }
        response = self.client.put(
            f"/api/v1/ball-annotation-sessions/{created['session_id']}/annotations/{frame['frame_index']}",
            json={
                "mutation_id": "point-only",
                "expected_revision": 0,
                "operation": "set",
                "undo_revision": None,
                "annotation": point_only,
            },
            headers={"If-Match": f'"{frame["annotation_etag"]}"'},
        )
        self.assertEqual(400, response.status_code, response.text)
        self.assertEqual("invalid_annotation", response.json()["detail"]["code"])


if __name__ == "__main__":
    unittest.main()
