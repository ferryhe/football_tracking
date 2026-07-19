from __future__ import annotations

import json
import math
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch as mock_patch

import test_ball_annotation_service as ball_service_fixtures
from audited_authority_test_support import patched_audited_t2_probe_bindings
from pydantic import ValidationError
from test_ball_annotation_service import (
    _absent,
    _dismiss_detector_candidate,
    _FakeProbeGateway,
    _present_box,
    _request,
)

from football_tracking.api.schemas import (
    BallAnnotationFinalResultResponse,
    BallAnnotationFrameView,
    BallAnnotationRevisionRequest,
    BallAnnotationRevisionResponse,
    BallAnnotationSessionResponse,
    BallCheckFeasibilityReportView,
    BallFeasibilityCandidateDiagnosticView,
    BallFeasibilityComputedSourceBoundsView,
    BallPropagationJobResponse,
    BallSealedPropagationReportView,
    BallSourceFrameTimingBindingView,
    _ball_feasibility_hoeffding_upper,
    _ball_feasibility_wilson_lower,
)
from football_tracking.ball_annotation_service import BallAnnotationService
from football_tracking.ball_detector_feasibility import (
    _computed_source_px_bounds,
    _score_validated_feasibility,
)
from football_tracking.detector_development_common import canonical_sha256

_GOLDEN_PATH = (
    Path(__file__).resolve().parents[2] / "test_fixtures" / "contracts" / "ball_annotation_api_golden.v1.json"
)


def _build_ball_annotation_contract_examples() -> dict[str, object]:
    uuid_counter = iter(range(1, 1000))
    timestamp_counter = iter(range(1, 1000))

    def deterministic_uuid4() -> SimpleNamespace:
        return SimpleNamespace(hex=f"{next(uuid_counter):032x}")

    def deterministic_utc_now_iso() -> str:
        observed = datetime(2026, 7, 18, tzinfo=timezone.utc) + timedelta(seconds=next(timestamp_counter))
        return observed.isoformat()

    with (
        tempfile.TemporaryDirectory() as temporary,
        mock_patch(
            "football_tracking.ball_annotation_service.uuid.uuid4",
            side_effect=deterministic_uuid4,
        ),
        mock_patch(
            "football_tracking.ball_annotation_service.utc_now_iso",
            side_effect=deterministic_utc_now_iso,
        ),
        patched_audited_t2_probe_bindings() as audit_bindings,
    ):
        repo_root = Path(temporary)
        (repo_root / "data").mkdir()
        gateway = _FakeProbeGateway()
        gateway.audit_bindings = audit_bindings
        service = BallAnnotationService(
            repo_root,
            get_probe=gateway.get_probe,
            create_probe=gateway.create_probe,
            cancel_propagation_probe=gateway.cancel_probe,
            read_probe_artifact=gateway.read_probe_artifact,
        )
        try:
            development = service.create_session(
                _request(
                    development_probe_job_ids=["probe-development"],
                    operator_id="contract-development-operator",
                )
            )
            public_development = BallAnnotationSessionResponse.model_validate(development).model_dump(mode="json")
            seed = next(frame for frame in development["frames"] if frame["frame_index"] == 40)
            candidate = seed["suggested_candidates"][0]
            accepted_revision = service.put_annotation(
                development["session_id"],
                seed["frame_index"],
                {
                    "mutation_id": "contract-accept-detector",
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": {
                        **_present_box(),
                        "provenance": "detector_candidate_human_confirmed",
                    },
                    "suggestion_kind": "detector_candidate",
                    "suggestion_id": candidate["candidate_id"],
                    "accepted_suggestion_job_id": candidate["suggestion_job_id"],
                    "accepted_suggestion_sha256": candidate["suggestion_sha256"],
                },
                if_match=f'"{seed["annotation_etag"]}"',
            )
            public_revision = BallAnnotationRevisionResponse.model_validate(accepted_revision).model_dump(mode="json")
            queued = service.create_propagation_job(
                development["session_id"],
                {
                    "mutation_id": "contract-propagate",
                    "seed_frame_index": seed["frame_index"],
                    "radius_frames": 2,
                    "expected_seed_revision": 1,
                },
                if_match=f'"{accepted_revision["annotation_etag"]}"',
            )
            gateway.complete(queued["neighbor_probe_job_id"])
            ready_job = service.get_propagation_job(development["session_id"], queued["job_id"])
            public_propagation_job = BallPropagationJobResponse.model_validate(ready_job).model_dump(mode="json")
            refreshed = service.get_session(development["session_id"])
            frame_by_index = {frame["frame_index"]: frame for frame in refreshed["frames"]}
            for suggestion in ready_job["suggestions"]:
                target = frame_by_index[suggestion["frame_index"]]
                service.put_annotation(
                    development["session_id"],
                    target["frame_index"],
                    {
                        "mutation_id": f"contract-dismiss-propagation-{target['frame_index']}",
                        "expected_revision": 0,
                        "operation": "set",
                        "undo_revision": None,
                        "annotation": {
                            **_absent(),
                            "provenance": "suggestion_dismissed_manual",
                        },
                        "dismissed_suggestion_kind": "propagation",
                        "dismissed_suggestion_id": suggestion["suggestion_id"],
                        "dismissed_suggestion_job_id": suggestion["suggestion_job_id"],
                        "dismissed_suggestion_sha256": suggestion["suggestion_sha256"],
                    },
                    if_match=f'"{target["annotation_etag"]}"',
                )
            refreshed = service.get_session(development["session_id"])
            for frame in refreshed["frames"]:
                if frame["frame_index"] == 40 or not frame["suggested_candidates"]:
                    continue
                candidate = frame["suggested_candidates"][0]
                service.put_annotation(
                    development["session_id"],
                    frame["frame_index"],
                    {
                        "mutation_id": f"contract-dismiss-detector-{frame['frame_index']}",
                        "expected_revision": frame["annotation_revision"],
                        "operation": "set",
                        "undo_revision": None,
                        "annotation": {
                            **_absent(),
                            "provenance": "suggestion_dismissed_manual",
                        },
                        "dismissed_suggestion_kind": "detector_candidate",
                        "dismissed_suggestion_id": candidate["candidate_id"],
                        "dismissed_suggestion_job_id": candidate["suggestion_job_id"],
                        "dismissed_suggestion_sha256": candidate["suggestion_sha256"],
                    },
                    if_match=f'"{frame["annotation_etag"]}"',
                )
            development_result = service.finalize_session(development["session_id"], "contract-finalize-development")
            public_development_result = BallAnnotationFinalResultResponse.model_validate(development_result).model_dump(
                mode="json"
            )
            public_report = BallSealedPropagationReportView.model_validate(
                development_result["package"]["propagation_reports"][0]
            ).model_dump(mode="json")

            check = service.create_session(
                _request(
                    data_role="check",
                    development_probe_job_ids=["probe-development"],
                    operator_id="contract-check-operator",
                    development_package_session_id=development_result["package"]["session_id"],
                    development_package_sha256=development_result["package"]["package_sha256"],
                )
            )
            public_check_active = BallAnnotationSessionResponse.model_validate(check).model_dump(mode="json")
            gateway.complete(check["check_probe_job_id"])
            check_ready = service.get_session(check["session_id"])
            public_check_ready = BallAnnotationSessionResponse.model_validate(check_ready).model_dump(mode="json")
            check_absent = {**_absent(), "training_use": "excluded"}
            for frame in check_ready["frames"]:
                candidate = frame["suggested_candidates"][0]
                service.put_annotation(
                    check_ready["session_id"],
                    frame["frame_index"],
                    {
                        "mutation_id": f"contract-check-{frame['frame_index']}",
                        "expected_revision": 0,
                        "operation": "set",
                        "undo_revision": None,
                        "annotation": check_absent,
                        **_dismiss_detector_candidate(candidate),
                    },
                    if_match=f'"{frame["annotation_etag"]}"',
                )
            check_result = service.finalize_session(check_ready["session_id"], "contract-finalize-check")
            public_check_result = BallAnnotationFinalResultResponse.model_validate(check_result).model_dump(mode="json")
            final_result_audit_bindings = deepcopy(audit_bindings)

            proxy_root = repo_root / "proxy-golden"
            (proxy_root / "data").mkdir(parents=True)
            proxy_gateway = _FakeProbeGateway()
            proxy_gateway.audit_bindings = audit_bindings
            source_times = [500.0] * len(proxy_gateway.jobs["probe-development"]["report"]["frames"])
            for job_id in ("probe-development", "probe-development-retry"):
                ball_service_fixtures.BallAnnotationServiceTests._set_probe_decoder_times(
                    proxy_gateway.jobs[job_id],
                    source_times,
                )
            ball_service_fixtures.BallAnnotationServiceTests._attach_review_proxy(
                proxy_gateway.jobs["probe-development-retry"],
                [50.0, 100.0, 200.0, 300.0, 400.0, 500.0],
                proxy_gateway.jobs["probe-development"],
            )
            proxy_service = BallAnnotationService(
                proxy_root,
                get_probe=proxy_gateway.get_probe,
                create_probe=proxy_gateway.create_probe,
                read_probe_artifact=proxy_gateway.read_probe_artifact,
            )
            try:
                proxy_session = proxy_service.create_session(_request(operator_id="contract-proxy-operator"))
                public_proxy_session = BallAnnotationSessionResponse.model_validate(proxy_session).model_dump(
                    mode="json"
                )
            finally:
                proxy_service.close()
        finally:
            service.close()

    examples = {
        "schema_version": "1.0",
        "development_session": public_development,
        "development_proxy_session": public_proxy_session,
        "annotation_revision": public_revision,
        "propagation_job": public_propagation_job,
        "sealed_propagation_report": public_report,
        "development_final_result": public_development_result,
        "check_session_active": public_check_active,
        "check_session_ready": public_check_ready,
        "check_final_result": public_check_result,
    }
    BallAnnotationSessionResponse.model_validate(examples["development_session"])
    BallAnnotationSessionResponse.model_validate(examples["development_proxy_session"])
    BallAnnotationRevisionResponse.model_validate(examples["annotation_revision"])
    BallPropagationJobResponse.model_validate(examples["propagation_job"])
    BallSealedPropagationReportView.model_validate(examples["sealed_propagation_report"])
    with patched_audited_t2_probe_bindings(final_result_audit_bindings):
        BallAnnotationFinalResultResponse.model_validate(examples["development_final_result"])
        BallAnnotationFinalResultResponse.model_validate(examples["check_final_result"])
    BallAnnotationSessionResponse.model_validate(examples["check_session_active"])
    BallAnnotationSessionResponse.model_validate(examples["check_session_ready"])
    return examples


def _rehash_check_report(final_result: dict[str, object]) -> None:
    report = final_result["feasibility_report"]
    report["report_sha256"] = canonical_sha256({key: value for key, value in report.items() if key != "report_sha256"})


def _rebuild_report_contradictions(report: dict[str, object]) -> None:
    report["frames"].sort(key=lambda row: row["frame_index"])
    report["contradictions"] = [
        {
            "frame_index": frame["frame_index"],
            "diagnostic_codes": frame["diagnostic_codes"],
        }
        for frame in report["frames"]
        if frame["diagnostic_codes"]
    ]


def _absent_lighting_stratum_metric(frame_count: int) -> dict[str, object]:
    point = 1.0 if frame_count else 0.0
    return {
        "support": {
            "localizable_positives": 0,
            "confirmed_absent": frame_count,
            "evaluable_frames": frame_count,
        },
        "top1_recall": {
            "raw": {"numerator": 0, "denominator": 0},
            "point_estimate": 0.0,
            "one_sided_95_lower": 0.0,
        },
        "top5_recall": {
            "raw": {"numerator": 0, "denominator": 0},
            "point_estimate": 0.0,
            "one_sided_95_lower": 0.0,
        },
        "candidate_totals": {
            "false": frame_count,
            "scored": frame_count,
            "raw": frame_count,
        },
        "false_candidates_per_evaluable_frame": {
            "raw": {"numerator": frame_count, "denominator": frame_count},
            "point_estimate": point,
            "one_sided_95_upper": _ball_feasibility_hoeffding_upper(point, frame_count),
        },
        "exploratory_small_n": True,
    }


def _build_positive_check_report() -> dict[str, object]:
    half_side = math.sqrt(2.0)
    annotations: list[dict[str, object]] = []
    candidates: dict[int, list[dict[str, object]]] = {}
    for frame_index in range(20):
        if frame_index < 15:
            annotation = {
                "frame_index": frame_index,
                "point_source_px": {"x": 12.0, "y": 12.0},
                "bbox_source_px": {
                    "left": 12.0 - half_side,
                    "top": 12.0 - half_side,
                    "right": 12.0 + half_side,
                    "bottom": 12.0 + half_side,
                },
                "presence": "present",
                "visibility": "visible",
                "training_use": "excluded",
                "annotation_state": "confirmed",
                "scale_stratum": "far",
                "lighting_tag": "bright_sun",
                "motion_occlusion_tags": [],
                "provenance": "manual_human_annotation",
            }
        else:
            annotation = {
                "frame_index": frame_index,
                **{**_absent(), "training_use": "excluded"},
            }
        annotations.append(annotation)
        candidates[frame_index] = [{"bbox_source_px": [10.0, 10.0, 14.0, 14.0]}]
    report = _score_validated_feasibility(
        session_id="positive-check-session",
        source_sha256="a" * 64,
        locked_profile_id="positive-check-profile",
        locked_profile_sha256="b" * 64,
        metric_profile_id="tiny_ball_feasibility_metric_v1",
        sampling_manifest_sha256="c" * 64,
        annotations=annotations,
        candidates_by_frame=candidates,
        source_height=360,
        frozen_lighting_by_frame={index: "bright_sun" for index in range(20)},
        attempt_family_sha256="d" * 64,
        development_package_binding={
            "session_id": "positive-development-session",
            "package_sha256": "e" * 64,
            "attempt_family_sha256": "d" * 64,
        },
        applicable_scale_strata=["far"],
        applicable_lighting_strata=["bright_sun"],
    )
    report["sealed_evidence"] = {
        "annotation_package_sha256": "f" * 64,
        "sampling_manifest_sha256": "c" * 64,
        "sampling_lock_sha256": "1" * 64,
        "check_probe_job_id": "positive-check-probe",
        "check_probe_report_sha256": "2" * 64,
        "attempt_family_sha256": "d" * 64,
        "development_annotation_session_id": "positive-development-session",
        "development_annotation_package_sha256": "e" * 64,
        "dataset_expansion_eligibility": {
            "eligible": False,
            "reasons": ["check_role_is_evaluation_only"],
            "validation_evidence": {
                "all_frames_human_confirmed": True,
                "all_primary_roles_complete": True,
                "all_supplemental_roles_complete": True,
                "exact_frame_media_sha256": "3" * 64,
                "frame_evidence_sha256": "4" * 64,
                "revision_chain_sha256": "5" * 64,
                "pending_detector_candidate_count": 0,
                "pending_propagation_suggestion_count": 0,
                "pending_suggestion_decision_count": 0,
                "localizable_positive_seed_count": 15,
            },
        },
    }
    report["report_sha256"] = canonical_sha256({key: value for key, value in report.items() if key != "report_sha256"})
    BallCheckFeasibilityReportView.model_validate(report)
    return report


class BallAnnotationSchemaContractTests(unittest.TestCase):
    def setUp(self) -> None:
        golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
        audit_bindings: dict[str, dict[str, str]] = {}
        for result_name in (
            "development_final_result",
            "check_final_result",
        ):
            for authority in golden.get(result_name, {}).get("package", {}).get("detector_probe_authorities", []):
                if authority.get("audit_anchor_kind") != "audited_t2_legacy":
                    continue
                audit_bindings[authority["job_id"]] = {
                    "canonical_job_record_sha256": authority["canonical_job_record_sha256"],
                    "request_sha256": authority["request_sha256"],
                    "report_sha256": authority["probe_report_sha256"],
                    "result_manifest_sha256": authority["probe_result_manifest_sha256"],
                    "execution_bundle_sha256": authority["execution_bundle_sha256"],
                    "runtime_environment_sha256": authority["runtime_environment_sha256"],
                }
        self._audit_patch = patched_audited_t2_probe_bindings(audit_bindings)
        self._audit_patch.__enter__()

    def tearDown(self) -> None:
        self._audit_patch.__exit__(None, None, None)

    def _assert_only_final_cross_binding_rejects(self, forged: dict[str, object]) -> None:
        BallCheckFeasibilityReportView.model_validate(forged["feasibility_report"])
        with self.assertRaises(ValidationError):
            BallAnnotationFinalResultResponse.model_validate(forged)

    @staticmethod
    def _accepted_revision_request() -> dict[str, object]:
        return {
            "mutation_id": "accept-suggestion-contract",
            "expected_revision": 0,
            "operation": "set",
            "undo_revision": None,
            "annotation": {
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
                "provenance": "detector_candidate_human_confirmed",
            },
            "suggestion_kind": "detector_candidate",
            "suggestion_id": "suggestion-contract",
            "accepted_suggestion_job_id": "probe-contract",
            "accepted_suggestion_sha256": "a" * 64,
            "dismissed_suggestion_kind": None,
            "dismissed_suggestion_id": None,
            "dismissed_suggestion_job_id": None,
            "dismissed_suggestion_sha256": None,
        }

    def test_revision_request_accepts_only_a_complete_bound_suggestion_tuple(
        self,
    ) -> None:
        request = self._accepted_revision_request()

        validated = BallAnnotationRevisionRequest.model_validate(request)

        self.assertEqual("probe-contract", validated.accepted_suggestion_job_id)
        self.assertEqual("a" * 64, validated.accepted_suggestion_sha256)
        for missing in (
            "suggestion_kind",
            "suggestion_id",
            "accepted_suggestion_job_id",
            "accepted_suggestion_sha256",
        ):
            with self.subTest(missing=missing):
                incomplete = deepcopy(request)
                incomplete[missing] = None
                with self.assertRaises(ValidationError):
                    BallAnnotationRevisionRequest.model_validate(incomplete)

    def test_revision_request_rejects_conflicting_decisions_state_and_provenance(
        self,
    ) -> None:
        request = self._accepted_revision_request()
        for patch in (
            {
                "dismissed_suggestion_kind": "detector_candidate",
                "dismissed_suggestion_id": "suggestion-other",
                "dismissed_suggestion_job_id": "probe-contract",
                "dismissed_suggestion_sha256": "b" * 64,
            },
            {
                "annotation": {
                    **request["annotation"],
                    "provenance": "manual_human_annotation",
                }
            },
            {
                "annotation": {
                    **request["annotation"],
                    "annotation_state": "suggested",
                    "training_use": "excluded",
                }
            },
        ):
            with self.subTest(patch=patch):
                invalid = deepcopy(request)
                invalid.update(patch)
                with self.assertRaises(ValidationError):
                    BallAnnotationRevisionRequest.model_validate(invalid)

    def test_low_height_metric_bounds_preserve_honest_empty_bands(self) -> None:
        bounds = _computed_source_px_bounds(32)

        validated = BallFeasibilityComputedSourceBoundsView.model_validate(bounds)

        self.assertGreater(
            validated.plausible_diagonal_min_source_px,
            validated.far_diagonal_max_source_px,
        )
        self.assertGreater(
            validated.plausible_diagonal_min_source_px,
            validated.mid_diagonal_max_source_px,
        )

    def test_metric_bounds_reject_reordered_or_recomputed_thresholds(self) -> None:
        bounds = _computed_source_px_bounds(32)
        for patch in (
            {"far_diagonal_max_source_px": 0.9},
            {"mid_diagonal_max_source_px": 0.7},
            {"matching_radius_cap_source_px": 4.1},
            {"plausible_diagonal_max_source_px": 2.5},
        ):
            with self.subTest(patch=patch):
                forged = deepcopy(bounds)
                forged.update(patch)
                with self.assertRaises(ValidationError):
                    BallFeasibilityComputedSourceBoundsView.model_validate(forged)

    def test_shared_api_golden_is_generated_from_actual_service_outputs(self) -> None:
        actual = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))

        expected = _build_ball_annotation_contract_examples()

        self.assertEqual(expected, actual)
        BallAnnotationSessionResponse.model_validate(actual["development_session"])
        proxy_session = BallAnnotationSessionResponse.model_validate(actual["development_proxy_session"])
        self.assertTrue(all(frame.proxy_binding is not None for frame in proxy_session.frames))
        BallAnnotationRevisionResponse.model_validate(actual["annotation_revision"])
        BallPropagationJobResponse.model_validate(actual["propagation_job"])
        BallSealedPropagationReportView.model_validate(actual["sealed_propagation_report"])
        BallAnnotationFinalResultResponse.model_validate(actual["development_final_result"])
        BallAnnotationSessionResponse.model_validate(actual["check_session_active"])
        BallAnnotationSessionResponse.model_validate(actual["check_session_ready"])
        BallAnnotationFinalResultResponse.model_validate(actual["check_final_result"])

    def test_public_proxy_frame_requires_exact_source_and_proxy_binding(self) -> None:
        golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
        frame = deepcopy(golden["development_proxy_session"]["frames"][0])
        self.assertEqual("not_collected", frame["source_timing_status"])

        missing = deepcopy(frame)
        missing["proxy_binding"] = None
        with self.assertRaises(ValidationError):
            BallAnnotationFrameView.model_validate(missing)

        for target, field, value in (
            ("source_frame", "sha256", "0" * 64),
            ("source_frame", "frame_index", frame["frame_index"] + 1),
            ("proxy_frame", "frame_index", frame["frame_index"] + 1),
        ):
            with self.subTest(target=target, field=field):
                forged = deepcopy(frame)
                forged["proxy_binding"][target][field] = value
                binding = forged["proxy_binding"]
                binding["binding_sha256"] = canonical_sha256(
                    {key: item for key, item in binding.items() if key != "binding_sha256"}
                )
                with self.assertRaises(ValidationError):
                    BallAnnotationFrameView.model_validate(forged)

    def test_public_candidate_decision_is_required_and_progress_bound(self) -> None:
        golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
        session = golden["development_session"]
        self.assertTrue(
            all(
                candidate["decision"] == "pending"
                for frame in session["frames"]
                for candidate in frame["suggested_candidates"]
            )
        )
        missing = deepcopy(session)
        missing["frames"][0]["suggested_candidates"][0].pop("decision")
        with self.assertRaises(ValidationError):
            BallAnnotationSessionResponse.model_validate(missing)
        inconsistent = deepcopy(session)
        inconsistent["frames"][0]["suggested_candidates"][0]["decision"] = "accepted"
        with self.assertRaises(ValidationError):
            BallAnnotationSessionResponse.model_validate(inconsistent)

    def test_true_pts_is_explicit_and_coherent_rehash_cannot_forge_collection(self) -> None:
        golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
        timing = golden["development_final_result"]["package"]["frame_evidence"][0]["timing_binding"]
        self.assertEqual(
            {
                "status": "not_collected",
                "value_seconds": None,
                "method": None,
            },
            timing["true_presentation_timestamp"],
        )
        BallSourceFrameTimingBindingView.model_validate(timing)

        forged = deepcopy(golden["development_final_result"])
        package = forged["package"]
        row = package["frame_evidence"][0]
        row["timing_binding"]["true_presentation_timestamp"] = {
            "status": "verified",
            "value_seconds": row["display_time_seconds"] if "display_time_seconds" in row else 0.0,
            "method": "frame_index_divided_by_fps",
        }
        row["timing_binding"]["timing_binding_sha256"] = canonical_sha256(
            {key: value for key, value in row["timing_binding"].items() if key != "timing_binding_sha256"}
        )
        row["frame_evidence_sha256"] = canonical_sha256(
            {key: value for key, value in row.items() if key != "frame_evidence_sha256"}
        )
        package["frame_evidence_sha256"] = canonical_sha256(package["frame_evidence"])
        package["dataset_expansion_eligibility"]["validation_evidence"]["frame_evidence_sha256"] = package[
            "frame_evidence_sha256"
        ]
        package["package_sha256"] = canonical_sha256(
            {key: value for key, value in package.items() if key != "package_sha256"}
        )
        report = forged["feasibility_report"]
        report["sealed_evidence"]["annotation_package_sha256"] = package["package_sha256"]
        report["sealed_evidence"]["dataset_expansion_eligibility"] = deepcopy(package["dataset_expansion_eligibility"])
        report["report_sha256"] = canonical_sha256(
            {key: value for key, value in report.items() if key != "report_sha256"}
        )

        with self.assertRaises(ValidationError):
            BallAnnotationFinalResultResponse.model_validate(forged)

    def test_final_development_result_rejects_coherently_rehashed_pending_detector_decision(
        self,
    ) -> None:
        golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
        forged = deepcopy(golden["development_final_result"])
        package = forged["package"]
        package["detector_candidate_evidence"][0]["decision"] = None
        package["detector_candidate_evidence_sha256"] = canonical_sha256(package["detector_candidate_evidence"])
        eligibility = package["dataset_expansion_eligibility"]
        eligibility["eligible"] = False
        eligibility["reasons"] = ["pending_suggestion_decisions"]
        validation = eligibility["validation_evidence"]
        validation["pending_detector_candidate_count"] = 1
        validation["pending_suggestion_decision_count"] = 1
        package["may_seed_dataset_expansion"] = False
        package["package_sha256"] = canonical_sha256(
            {key: value for key, value in package.items() if key != "package_sha256"}
        )
        report = forged["feasibility_report"]
        report["sealed_evidence"]["annotation_package_sha256"] = package["package_sha256"]
        report["sealed_evidence"]["dataset_expansion_eligibility"] = deepcopy(eligibility)
        report["report_sha256"] = canonical_sha256(
            {key: value for key, value in report.items() if key != "report_sha256"}
        )

        with self.assertRaises(ValidationError):
            BallAnnotationFinalResultResponse.model_validate(forged)

    def test_check_report_rejects_rehashed_status_and_authorization_forgery(
        self,
    ) -> None:
        golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
        forged = deepcopy(golden["check_final_result"])
        report = forged["feasibility_report"]
        report["status"] = "feasibility_passed"
        report["authorizations"]["may_expand_to_100_300_boxes"] = True
        _rehash_check_report(forged)

        with self.assertRaises(ValidationError):
            BallAnnotationFinalResultResponse.model_validate(forged)

    def test_check_report_rejects_self_consistent_rehashed_metrics_not_in_frames(
        self,
    ) -> None:
        golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
        forged = deepcopy(golden["check_final_result"])
        report = forged["feasibility_report"]
        metrics = report["metrics"]
        for metric_name in (
            "false_candidates_per_evaluable_frame",
            "candidates_per_evaluable_frame",
            "raw_candidates_per_evaluable_frame",
        ):
            metrics[metric_name]["raw"]["numerator"] = 20
            metrics[metric_name]["point_estimate"] = 2.0
        metrics["false_candidates_per_evaluable_frame"]["one_sided_95_upper"] += 1.0
        _rehash_check_report(forged)

        with self.assertRaises(ValidationError):
            BallAnnotationFinalResultResponse.model_validate(forged)

    def test_check_report_rejects_coherent_rehashed_stratum_not_in_frames(
        self,
    ) -> None:
        golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
        forged = deepcopy(golden["check_final_result"])
        report = forged["feasibility_report"]
        report["strata_metrics"]["scale"]["near"] = {
            "support": {
                "localizable_positives": 1,
                "confirmed_absent": 0,
                "evaluable_frames": 1,
            },
            "top1_recall": {
                "raw": {"numerator": 1, "denominator": 1},
                "point_estimate": 1.0,
                "one_sided_95_lower": _ball_feasibility_wilson_lower(1, 1),
            },
            "top5_recall": {
                "raw": {"numerator": 1, "denominator": 1},
                "point_estimate": 1.0,
                "one_sided_95_lower": _ball_feasibility_wilson_lower(1, 1),
            },
            "candidate_totals": {"false": 0, "scored": 1, "raw": 1},
            "false_candidates_per_evaluable_frame": {
                "raw": {"numerator": 0, "denominator": 1},
                "point_estimate": 0.0,
                "one_sided_95_upper": _ball_feasibility_hoeffding_upper(0.0, 1),
            },
            "exploratory_small_n": True,
        }
        _rehash_check_report(forged)

        with self.assertRaises(ValidationError):
            BallAnnotationFinalResultResponse.model_validate(forged)

    def test_check_report_rejects_rehashed_hit_not_in_candidate_diagnostics(
        self,
    ) -> None:
        golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
        forged = deepcopy(golden["check_final_result"])
        report = forged["feasibility_report"]
        frame = next(row for row in report["frames"] if row["metric_eligible"] and row["candidate_diagnostics"])
        frame["top5_hit"] = True
        _rehash_check_report(forged)

        with self.assertRaises(ValidationError):
            BallAnnotationFinalResultResponse.model_validate(forged)

    def test_check_report_rejects_rehashed_resolution_not_in_diagnostics(
        self,
    ) -> None:
        golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
        for mutation in ("count", "reasons"):
            with self.subTest(mutation=mutation):
                forged = deepcopy(golden["check_final_result"])
                report = forged["feasibility_report"]
                resolution = report["resolution"]
                if mutation == "count":
                    resolution["raw_lighting_mismatch_count"] = 1
                else:
                    resolution["raw_lighting_mismatch_count"] = 0
                    resolution["reason_codes"] = []
                    resolution["requires_new_attempt"] = False
                    report["support"]["missing"].remove("lighting_strata_mismatch")
                _rehash_check_report(forged)

                with self.assertRaises(ValidationError):
                    BallAnnotationFinalResultResponse.model_validate(forged)

    def test_candidate_diagnostic_rejects_unbound_or_false_match_measurements(
        self,
    ) -> None:
        BallFeasibilityCandidateDiagnosticView.model_validate(
            {
                "rank": 1,
                "matched": False,
                "center_distance_source_px": None,
                "iou": None,
                "evaluation_radius_source_px": None,
            }
        )
        invalid_measurements = (
            {
                "matched": True,
                "center_distance_source_px": None,
                "iou": None,
                "evaluation_radius_source_px": None,
            },
            {
                "matched": False,
                "center_distance_source_px": 1.0,
                "iou": None,
                "evaluation_radius_source_px": None,
            },
            {
                "matched": True,
                "center_distance_source_px": 100.0,
                "iou": 0.0,
                "evaluation_radius_source_px": 1.0,
            },
            {
                "matched": False,
                "center_distance_source_px": 0.0,
                "iou": 1.0,
                "evaluation_radius_source_px": 1.0,
            },
            {
                "matched": False,
                "center_distance_source_px": 1.0,
                "iou": 0.0,
                "evaluation_radius_source_px": 0.0,
            },
        )
        for measurement in invalid_measurements:
            with self.subTest(measurement=measurement), self.assertRaises(ValidationError):
                BallFeasibilityCandidateDiagnosticView.model_validate({"rank": 1, **measurement})

    def test_check_report_rejects_rehashed_metric_radius_not_from_truth_box(
        self,
    ) -> None:
        report = _build_positive_check_report()
        forged = deepcopy(report)
        diagnostic = forged["frames"][0]["candidate_diagnostics"][0]
        diagnostic["evaluation_radius_source_px"] += 1.0
        forged["report_sha256"] = canonical_sha256(
            {key: value for key, value in forged.items() if key != "report_sha256"}
        )

        with self.assertRaises(ValidationError):
            BallCheckFeasibilityReportView.model_validate(forged)

    def test_final_check_rejects_rehashed_report_frame_translation(self) -> None:
        golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
        forged = deepcopy(golden["check_final_result"])
        report = forged["feasibility_report"]
        for frame in report["frames"]:
            frame["frame_index"] += 1000
        _rebuild_report_contradictions(report)
        _rehash_check_report(forged)

        self._assert_only_final_cross_binding_rejects(forged)

    def test_final_check_rejects_rehashed_single_frame_swap(self) -> None:
        golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
        forged = deepcopy(golden["check_final_result"])
        report = forged["feasibility_report"]
        report["frames"][0]["frame_index"], report["frames"][-1]["frame_index"] = (
            report["frames"][-1]["frame_index"],
            report["frames"][0]["frame_index"],
        )
        _rebuild_report_contradictions(report)
        _rehash_check_report(forged)

        self._assert_only_final_cross_binding_rejects(forged)

    def test_final_check_rejects_rehashed_annotation_and_lighting_mismatch(
        self,
    ) -> None:
        golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
        for mismatch in ("annotation", "lighting"):
            with self.subTest(mismatch=mismatch):
                forged = deepcopy(golden["check_final_result"])
                report = forged["feasibility_report"]
                frame = next(row for row in report["frames"] if row["metric_eligible"])
                if mismatch == "annotation":
                    frame["motion_occlusion_tags"] = ["ground"]
                else:
                    frame["observed_lighting_tag"] = "shadow"
                    frame["frozen_lighting_stratum"] = "shadow"
                    report["strata_metrics"]["lighting"]["bright_sun"] = _absent_lighting_stratum_metric(9)
                    report["strata_metrics"]["lighting"]["shadow"] = _absent_lighting_stratum_metric(1)
                _rehash_check_report(forged)

                self._assert_only_final_cross_binding_rejects(forged)

    def test_final_check_rejects_rehashed_supplemental_substitution(self) -> None:
        golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
        forged = deepcopy(golden["check_final_result"])
        supplemental_index = golden["development_final_result"]["package"]["supplemental_frame_indices"][0]
        primary_indices = forged["package"]["sampling_manifest"]["frame_indices"]
        self.assertNotIn(supplemental_index, primary_indices)
        report = forged["feasibility_report"]
        report["frames"][0]["frame_index"] = supplemental_index
        _rebuild_report_contradictions(report)
        _rehash_check_report(forged)

        self._assert_only_final_cross_binding_rejects(forged)


if __name__ == "__main__":
    unittest.main()
