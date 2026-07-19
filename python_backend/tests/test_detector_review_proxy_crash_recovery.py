from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from football_tracking.api.service import ApiService
from football_tracking.ball_annotation_service import (
    BallAnnotationService,
    BallAnnotationServiceError,
)
from football_tracking.ball_detector_feasibility import temporal_group_for_frame
from football_tracking.detector_development_common import (
    DetectorDevelopmentError,
    canonical_sha256,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
BLOCKED_NORMALIZED_REQUEST: dict[str, object] = {
    "data_role": "development",
    "development_probe_job_ids": ["probe-parent-one"],
    "locked_profile_id": "profile-one",
    "target_frame_count": None,
    "sampling_profile_id": "temporal-groups-v1",
    "metric_profile_id": "tiny-ball-feasibility-v1",
    "operator_id": "operator-one",
    "strata_applicability": {"scale": [], "lighting": []},
    "applicable_scale_strata": [],
    "applicable_lighting_strata": [],
    "retry_from_session_id": None,
    "development_package_session_id": None,
    "development_package_sha256": None,
}
BLOCKED_REQUEST_SHA256 = canonical_sha256(BLOCKED_NORMALIZED_REQUEST)
BLOCKED_SESSION_ID = f"annotation-{BLOCKED_REQUEST_SHA256[:16]}-001122334455"


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "service_leases" not in path.relative_to(root).parts
    }


def _group(frame_index: int = 1) -> dict[str, object]:
    return {
        **temporal_group_for_frame(SHA_B, frame_index),
        "frame_index": frame_index,
    }


def _sampling_manifest(group: dict[str, object]) -> dict[str, object]:
    body: dict[str, object] = {
        "groups": [deepcopy(group)],
        "frame_indices": [group["frame_index"]],
        "strata_applicability": {"scale": [], "lighting": []},
    }
    return {**body, "manifest_sha256": canonical_sha256(body)}


def _blocked_session() -> dict[str, object]:
    group = _group()
    session = {
        "schema_version": "1.0",
        "artifact_type": "ball_annotation_session",
        "session_id": BLOCKED_SESSION_ID,
        "idempotency_key": BLOCKED_REQUEST_SHA256,
        "request_sha256": BLOCKED_REQUEST_SHA256,
        "_normalized_session_request": deepcopy(BLOCKED_NORMALIZED_REQUEST),
        "data_role": "development",
        "status": "blocked",
        "stage": "blocked",
        "source": {"sha256": SHA_B},
        "lineage": {
            "parent_trial_id": "trial-one",
            "development_probe_job_ids": ["probe-parent-one"],
            "development_probe_report_sha256s": {"probe-parent-one": SHA_A},
            "development_probe_result_manifest_sha256s": {"probe-parent-one": SHA_B},
            "development_probe_execution_bundle_sha256s": {"probe-parent-one": SHA_C},
            "development_probe_frozen_profiles_sha256s": {"probe-parent-one": SHA_A},
            "decode": {"position_verification": "timing_unverified"},
            "runtime_environment_sha256": SHA_B,
        },
        "locked_profile": {
            "profile_id": "profile-one",
            "profile_sha256": SHA_A,
        },
        "control_profile_id": "control-one",
        "control_profile": {"profile_id": "control-one"},
        "sampling_profile_id": "temporal-groups-v1",
        "metric_profile_id": "tiny-ball-feasibility-v1",
        "metric_profile_sha256": SHA_C,
        "sampling_manifest": _sampling_manifest(group),
        "operator_id": "operator-one",
        "applicable_scale_strata": [],
        "applicable_lighting_strata": [],
        "retry_from_session_id": None,
        "retry_lineage": None,
        "development_package_binding": None,
        "check_probe_job_id": None,
        "check_probe_authority": None,
        "frames": [],
        "revisions": [],
        "final_package": None,
        "final_result": None,
        "error_code": "invalid_source_timing",
        "blocker_code": "review_proxy_required",
        "attempt_family_sha256": None,
        "created_at": "2026-07-18T12:00:00+00:00",
        "updated_at": "2026-07-18T12:00:00+00:00",
    }
    session["attempt_family_sha256"] = BallAnnotationService._attempt_family_sha256(session)
    return session


def _replacement_session(blocked: dict[str, object]) -> dict[str, object]:
    lineage = deepcopy(blocked["lineage"])
    lineage.update(
        {
            "development_probe_job_ids": ["probe-parent-one", "probe-child-one"],
            "development_probe_report_sha256s": {
                "probe-parent-one": SHA_A,
                "probe-child-one": SHA_B,
            },
            "development_probe_result_manifest_sha256s": {
                "probe-parent-one": SHA_B,
                "probe-child-one": SHA_C,
            },
            "development_probe_execution_bundle_sha256s": {
                "probe-parent-one": SHA_C,
                "probe-child-one": SHA_A,
            },
            "development_probe_frozen_profiles_sha256s": {
                "probe-parent-one": SHA_A,
                "probe-child-one": SHA_A,
            },
            "decode": {"position_verification": "verified_review_proxy_frame_index_mapping_v1"},
        }
    )
    normalized_request = {
        "data_role": "development",
        "development_probe_job_ids": sorted(["probe-parent-one", "probe-child-one"]),
        "locked_profile_id": "profile-one",
        "target_frame_count": None,
        "sampling_profile_id": blocked["sampling_profile_id"],
        "metric_profile_id": blocked["metric_profile_id"],
        "operator_id": blocked["operator_id"],
        "strata_applicability": deepcopy(blocked["sampling_manifest"]["strata_applicability"]),
        "applicable_scale_strata": [],
        "applicable_lighting_strata": [],
        "retry_from_session_id": blocked["session_id"],
        "development_package_session_id": None,
        "development_package_sha256": None,
    }
    request_sha256 = canonical_sha256(normalized_request)
    session_id = f"annotation-{request_sha256[:16]}-0123456789ab"
    replacement = deepcopy(blocked)
    replacement.update(
        {
            "session_id": session_id,
            "idempotency_key": request_sha256,
            "request_sha256": request_sha256,
            "_normalized_session_request": deepcopy(normalized_request),
            "status": "annotating",
            "stage": "annotating",
            "lineage": lineage,
            "retry_from_session_id": blocked["session_id"],
            "retry_lineage": {
                "mode": "review_proxy_decode_upgrade",
                "previous_session_id": blocked["session_id"],
                "previous_error_code": blocked["error_code"],
                "previous_blocker_code": blocked["blocker_code"],
                "previous_lineage_sha256": canonical_sha256(blocked["lineage"]),
                "current_lineage_sha256": canonical_sha256(lineage),
                "sampling_manifest_sha256": blocked["sampling_manifest"]["manifest_sha256"],
            },
            "frames": [
                {
                    "frame_index": 1,
                    "frame_role": "primary_sample",
                    "primary_sample": True,
                    "annotation_revision": 0,
                    "current_annotation": None,
                    "suggested_candidates": [],
                    "propagation_job_ids": [],
                    "propagation_suggestions": [],
                }
            ],
            "error_code": None,
            "blocker_code": None,
            "created_at": "2026-07-18T12:01:00+00:00",
            "updated_at": "2026-07-18T12:01:00+00:00",
        }
    )
    replacement["attempt_family_sha256"] = BallAnnotationService._attempt_family_sha256(replacement)
    return replacement


def _continuation_binding() -> dict[str, object]:
    code_files = {"football_tracking/detector_probe.py": SHA_A}
    runtime = {"python_version": "3.11-test"}
    body: dict[str, object] = {
        "schema_version": "1.0",
        "artifact_type": "detector_review_proxy_continuation_execution_binding",
        "code_files": code_files,
        "code_bundle_sha256": canonical_sha256(code_files),
        "runtime": runtime,
        "runtime_sha256": canonical_sha256(runtime),
    }
    return {**body, "binding_sha256": canonical_sha256(body)}


def _child_plan(repair_id: str) -> dict[str, object]:
    body: dict[str, object] = {
        "schema_version": "1.0",
        "artifact_type": "detector_review_proxy_child_plan",
        "parent_probe_job_id": "probe-parent-one",
        "repair_id": repair_id,
        "request_sha256": SHA_A,
        "intent_sha256": SHA_B,
        "semantic_intent_sha256": SHA_C,
        "resource_sha256": SHA_A,
        "execution_bundle_sha256": SHA_B,
        "runtime_environment_sha256": SHA_C,
        "frozen_profiles_sha256": SHA_A,
        "continuation_execution_binding": _continuation_binding(),
    }
    return {**body, "plan_sha256": canonical_sha256(body)}


def _continuation_intent(repair_id: str, authority: dict[str, object]) -> dict[str, object]:
    body: dict[str, object] = {
        "schema_version": "1.0",
        "artifact_type": "detector_review_proxy_continuation_intent",
        "repair_id": repair_id,
        "authority_sha256": canonical_sha256(authority),
        "repair_evidence": {
            "schema_version": "1.0",
            "repair_id": repair_id,
            "repair_request_sha256": SHA_B,
            "repair_report_sha256": SHA_A,
            "repair_result_manifest_sha256": SHA_B,
            "proxy_media_sha256": SHA_A,
            "proxy_size_bytes": 123,
            "repair_execution_binding_sha256": SHA_B,
            "repair_code_bundle_sha256": SHA_C,
            "repair_runtime_sha256": SHA_A,
            "repair_decoder_fingerprint_sha256": SHA_B,
            "sampled_frame_sha256s": {"1": SHA_C},
        },
        "proxy_media": {
            "sha256": SHA_A,
            "size_bytes": 123,
            "width": 2560,
            "height": 720,
            "frame_count": 12,
            "fps": 20.0,
        },
        "child_plan": _child_plan(repair_id),
    }
    return {**body, "intent_sha256": canonical_sha256(body)}


def _record(
    blocked_session_record_sha256: str = SHA_B,
    repair_id: str = "repair-crash-one",
) -> dict[str, object]:
    blocked_session_id = BLOCKED_SESSION_ID
    request_sha256 = canonical_sha256({"blocked_session_id": blocked_session_id})
    authority: dict[str, object] = {
        "blocked_session_id": blocked_session_id,
        "blocked_session_request_sha256": BLOCKED_REQUEST_SHA256,
        "blocked_session_record_sha256": blocked_session_record_sha256,
        "parent_probe_job_id": "probe-parent-one",
        "development_probe_job_ids": ["probe-parent-one"],
        "parent_probe_request_sha256": SHA_A,
        "parent_probe_intent_sha256": SHA_B,
        "parent_probe_semantic_intent_sha256": SHA_C,
        "parent_probe_report_sha256": SHA_A,
        "parent_probe_result_manifest_sha256": SHA_B,
        "parent_probe_record_sha256": SHA_A,
        "parent_execution_bundle_sha256": SHA_B,
        "parent_runtime_environment_sha256": SHA_C,
        "source_frame_evidence_sha256": SHA_A,
        "source_id": "source-one",
        "source_sha256": SHA_B,
        "source_file_identity_sha256": SHA_C,
        "source_size_bytes": 1234,
        "source_width": 5120,
        "source_height": 1440,
        "source_frame_count": 12,
        "source_fps": 20.0,
        "locked_profile_id": "profile-one",
        "locked_profile_sha256": SHA_A,
        "frame_indices": [1],
        "sampling_manifest_sha256": SHA_B,
        "temporal_groups_sha256": SHA_C,
        "candidate_evidence_sha256": SHA_A,
        "replacement_request_authority_sha256": SHA_B,
    }
    return {
        "schema_version": "1.0",
        "artifact_type": "detector_review_proxy_repair_transaction",
        "repair_id": repair_id,
        "attempt_root_repair_id": repair_id,
        "attempt_number": 1,
        "retry_from_repair_id": None,
        "idempotency_key": request_sha256,
        "request_sha256": request_sha256,
        "status": "failed",
        "stage": "continuation_intent",
        "preset_id": "h264-cfr-720p-v1",
        "eligibility": {
            "eligible": True,
            "action": "generate_verified_review_proxy",
            "blocker_code": "review_proxy_required",
        },
        "authority": authority,
        "low_request_sha256": SHA_B,
        "low_progress": {"completed": 37, "total": 37},
        "continuation_intent": _continuation_intent(repair_id, authority),
        "child_probe": None,
        "replacement_session": None,
        "result": None,
        "error_code": "review_proxy_failed",
        "blocker_code": None,
        "recovery_action": "retry",
        "created_at": "2026-07-18T12:00:00+00:00",
        "updated_at": "2026-07-18T12:00:00+00:00",
    }


def _child_job() -> dict[str, object]:
    continuation = _continuation_binding()
    return {
        "job_id": "probe-child-one",
        "request_sha256": SHA_A,
        "intent_sha256": SHA_B,
        "semantic_intent_sha256": SHA_C,
        "resource_sha256": SHA_A,
        "frozen_profiles_sha256": SHA_A,
        "status": "ready",
        "retry_from_job_id": "probe-parent-one",
        "retry_kind": "review_proxy_decode_upgrade",
        "result_manifest_sha256": SHA_A,
        "frozen_request": {
            "execution_bundle_sha256": SHA_B,
            "runtime_environment_sha256": SHA_C,
            "review_proxy_upgrade": {
                "repair_evidence": {"repair_id": "repair-crash-one"},
                "continuation_execution_binding": continuation,
            },
        },
        "report": {"report_sha256": SHA_B},
    }


class _ReadyChildDetector:
    def get_review_proxy_upgrade_child(self, _parent_job_id: str):
        child = deepcopy(_child_job())
        child.pop("resource_sha256")
        return child

    def get_verified_probe_job_record(self, _job_id: str):
        return deepcopy(_child_job())


class _ClaimOnlyDetector:
    def get_review_proxy_upgrade_child(self, _parent_job_id: str):
        raise DetectorDevelopmentError(
            "review_proxy_parent_child_claimed",
            "durable claim exists before its job row",
            status_code=409,
        )


class ReviewProxyCrashRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temporary.name)
        for name in ("config", "data", "outputs", "weights"):
            (self.repo_root / name).mkdir(parents=True, exist_ok=True)
        self.api = ApiService(self.repo_root)
        self.low_proxy_floor = mock.patch.object(
            self.api,
            "_inspect_review_proxy_low_job",
            return_value=1,
        )
        self.low_proxy_floor.start()
        self.addCleanup(self.low_proxy_floor.stop)
        self.annotation = BallAnnotationService(
            self.repo_root,
            get_probe=lambda _job_id: {},
            create_probe=lambda _request: {},
            read_probe_artifact=lambda _job_id, _path: (b"", "", ""),
        )

    def tearDown(self) -> None:
        self.annotation.close()
        self.api.close()
        self.temporary.cleanup()

    def _publish_replacement(self, *, groups: bool) -> tuple[dict[str, object], dict[str, object]]:
        blocked = _blocked_session()
        replacement = _replacement_session(blocked)
        self.annotation._persist_session(blocked)
        self.annotation._persist_session(replacement)
        if groups:
            self.annotation._record_groups(
                replacement["session_id"],
                SHA_B,
                replacement["sampling_manifest"]["groups"],
                data_role="development",
                state="revealed",
            )
        return blocked, replacement

    def _inspect(self) -> dict[str, object] | None:
        return self.annotation.inspect_review_proxy_replacement_side_effect(
            BLOCKED_SESSION_ID,
            child_probe_job_id="probe-child-one",
            expected_development_probe_job_ids=["probe-parent-one"],
            blocked_session_record_sha256=self._blocked_record_sha256(),
        )

    def _blocked_record_sha256(self) -> str:
        path = self.annotation._sessions_root / f"{BLOCKED_SESSION_ID}.json"
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _write_session_unchecked(self, session: dict[str, object]) -> None:
        path = self.annotation._sessions_root / f"{session['session_id']}.json"
        path.write_text(
            json.dumps(session, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def _rank_five_record(
        self,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        _blocked, replacement = self._publish_replacement(groups=True)
        inspection = self._inspect()
        self.assertIsNotNone(inspection)
        self.assertIsNotNone(inspection["group_commit"])
        record = _record(self._blocked_record_sha256())
        record.update(
            status="failed",
            stage="groups_published",
            child_probe=ApiService._review_proxy_child_summary(_child_job()),
            replacement_session=ApiService._review_proxy_session_summary(replacement),
            group_publication=ApiService._review_proxy_group_publication_summary(inspection["group_commit"]),
            error_code="review_proxy_failed",
            blocker_code=None,
            recovery_action="resume",
        )
        self.api._persist_detector_review_proxy_repair(record)
        return record, replacement, inspection["group_commit"]

    def _inspect_with_group_witness(
        self,
        group_commit: dict[str, object],
    ) -> dict[str, object] | None:
        return self.annotation.inspect_review_proxy_replacement_side_effect(
            BLOCKED_SESSION_ID,
            child_probe_job_id="probe-child-one",
            expected_development_probe_job_ids=["probe-parent-one"],
            blocked_session_record_sha256=self._blocked_record_sha256(),
            expected_group_commit=group_commit,
            replacement_session_witnessed=True,
        )

    def test_read_only_inspection_distinguishes_replacement_and_exact_groups(self) -> None:
        self._publish_replacement(groups=False)
        before = _tree_bytes(self.repo_root)
        replacement_only = self._inspect()
        self.assertIsNotNone(replacement_only)
        self.assertIsNone(replacement_only["group_commit"])
        self.assertEqual(before, _tree_bytes(self.repo_root))

        replacement = replacement_only["session"]
        self.annotation._record_groups(
            replacement["session_id"],
            SHA_B,
            replacement["sampling_manifest"]["groups"],
            data_role="development",
            state="revealed",
        )
        before_groups = _tree_bytes(self.repo_root)
        with_groups = self._inspect()
        self.assertIsNotNone(with_groups["group_commit"])
        self.assertEqual(before_groups, _tree_bytes(self.repo_root))

    def test_rank_five_get_accepts_finalizing_and_finalized_without_writes(self) -> None:
        record, replacement, _group_commit = self._rank_five_record()
        replacement_path = self.annotation._sessions_root / f"{replacement['session_id']}.json"
        for lifecycle in ("finalizing", "finalized"):
            with self.subTest(lifecycle=lifecycle):
                advanced = deepcopy(replacement)
                advanced.update(
                    status=lifecycle,
                    stage=lifecycle,
                    revisions=[{"frame_index": 1, "revision": 1}],
                    final_package={"package_sha256": SHA_A},
                    final_result={"result_sha256": SHA_B},
                    updated_at="2026-07-18T12:02:00+00:00",
                )
                self.annotation._persist_session(advanced)
                before = _tree_bytes(self.repo_root)
                with (
                    mock.patch.object(
                        self.api,
                        "_detector_development_service",
                        return_value=_ReadyChildDetector(),
                    ),
                    mock.patch.object(
                        self.api,
                        "_ball_annotation_service",
                        return_value=self.annotation,
                    ),
                    mock.patch.object(
                        self.api,
                        "_start_detector_review_proxy_continuation",
                    ) as start,
                ):
                    public = self.api.get_detector_review_proxy_repair(record["repair_id"])

                self.assertEqual("groups_published", public["stage"])
                start.assert_called_once_with(record["repair_id"])
                self.assertEqual(before, _tree_bytes(self.repo_root))
                self.assertEqual(advanced, self.annotation._read_json(replacement_path, "session", 1024 * 1024))

    def test_ready_replacement_verification_accepts_advanced_lifecycle_without_writes(self) -> None:
        _record_value, replacement, group_commit = self._rank_five_record()
        for lifecycle in ("finalizing", "finalized"):
            with self.subTest(lifecycle=lifecycle):
                advanced = deepcopy(replacement)
                advanced.update(
                    status=lifecycle,
                    stage=lifecycle,
                    revisions=[{"frame_index": 1, "revision": 1}],
                    final_package={"package_sha256": SHA_A},
                    final_result={"result_sha256": SHA_B},
                )
                self.annotation._persist_session(advanced)
                before = _tree_bytes(self.repo_root)
                verified = self.annotation.verify_ready_review_proxy_replacement(
                    blocked_session_id=BLOCKED_SESSION_ID,
                    blocked_session_record_sha256=self._blocked_record_sha256(),
                    replacement_session_id=replacement["session_id"],
                    child_probe_job_id="probe-child-one",
                    session_creation_authority_sha256=group_commit["session_creation_authority_sha256"],
                    group_publication_sha256=group_commit["group_publication_sha256"],
                )
                self.assertEqual(lifecycle, verified["session"]["status"])
                self.assertEqual(before, _tree_bytes(self.repo_root))

    def test_witnessed_inspection_rejects_lifecycle_and_creation_authority_tampering(self) -> None:
        _record_value, replacement, group_commit = self._rank_five_record()
        replacement_path = self.annotation._sessions_root / f"{replacement['session_id']}.json"
        pristine = replacement_path.read_bytes()

        def mismatched_lifecycle(session: dict[str, object]) -> None:
            session.update(status="finalizing", stage="annotating")

        def unsupported_lifecycle(session: dict[str, object]) -> None:
            session.update(status="blocked", stage="blocked")

        def request_authority(session: dict[str, object]) -> None:
            session["_normalized_session_request"]["operator_id"] = "forged-operator"

        def primary_frame_authority(session: dict[str, object]) -> None:
            session["frames"][0]["frame_role"] = "propagation_target"

        cases = {
            "mismatched_lifecycle": mismatched_lifecycle,
            "unsupported_lifecycle": unsupported_lifecycle,
            "normalized_request": request_authority,
            "primary_frame": primary_frame_authority,
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                replacement_path.write_bytes(pristine)
                forged = self.annotation._read_json(replacement_path, "session", 1024 * 1024)
                mutate(forged)
                self._write_session_unchecked(forged)
                before = _tree_bytes(self.repo_root)
                with self.assertRaises(BallAnnotationServiceError) as rejected:
                    self._inspect_with_group_witness(group_commit)
                self.assertEqual(409, rejected.exception.status_code)
                self.assertEqual(before, _tree_bytes(self.repo_root))

    def test_witnessed_inspection_rejects_commit_and_registry_tampering(self) -> None:
        _record_value, _replacement, group_commit = self._rank_five_record()
        registry_path = self.annotation._registry_path
        pristine_registry = registry_path.read_bytes()

        tampered_commits: dict[str, dict[str, object]] = {}
        for field, value in (
            ("session_creation_authority_sha256", SHA_C),
            ("group_publication_sha256", SHA_A),
            ("child_probe_job_id", "probe-child-other"),
        ):
            forged = deepcopy(group_commit)
            forged[field] = value
            forged_body = {key: item for key, item in forged.items() if key != "commit_sha256"}
            forged["commit_sha256"] = canonical_sha256(forged_body)
            tampered_commits[field] = forged
        bad_self_digest = deepcopy(group_commit)
        bad_self_digest["commit_sha256"] = SHA_A
        tampered_commits["commit_sha256"] = bad_self_digest

        for label, forged in tampered_commits.items():
            with self.subTest(label=label):
                before = _tree_bytes(self.repo_root)
                with self.assertRaises(BallAnnotationServiceError) as rejected:
                    self._inspect_with_group_witness(forged)
                self.assertEqual(409, rejected.exception.status_code)
                self.assertEqual(before, _tree_bytes(self.repo_root))

        registry = self.annotation._read_registry()
        created_at = datetime.fromisoformat(registry["entries"][0]["created_at"])
        registry["entries"][0]["updated_at"] = (created_at + timedelta(seconds=1)).isoformat()
        self.annotation._write_registry(registry)
        before_registry_check = _tree_bytes(self.repo_root)
        with self.assertRaises(BallAnnotationServiceError) as registry_rejected:
            self._inspect_with_group_witness(group_commit)
        self.assertEqual(409, registry_rejected.exception.status_code)
        self.assertEqual(before_registry_check, _tree_bytes(self.repo_root))
        registry_path.write_bytes(pristine_registry)

    def test_rank_five_get_rejects_a_stored_commit_with_invalid_self_digest_without_writes(self) -> None:
        record, _replacement, _group_commit = self._rank_five_record()
        forged = deepcopy(record)
        forged["group_publication"]["commit_sha256"] = SHA_A
        forged.pop("transaction_sha256", None)
        forged["transaction_sha256"] = canonical_sha256(forged)
        repair_path = self.api._detector_review_proxy_jobs_root / f"{record['repair_id']}.json"
        repair_path.write_text(
            json.dumps(forged, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        before = _tree_bytes(self.repo_root)

        with self.assertRaises(DetectorDevelopmentError) as rejected:
            self.api.get_detector_review_proxy_repair(record["repair_id"])

        self.assertEqual("invalid_review_proxy_transaction", rejected.exception.code)
        self.assertEqual(before, _tree_bytes(self.repo_root))

    def test_pristine_orphan_with_tampered_creation_authority_cannot_publish_groups(self) -> None:
        _blocked, replacement = self._publish_replacement(groups=False)
        replacement_path = self.annotation._sessions_root / f"{replacement['session_id']}.json"
        pristine = replacement_path.read_bytes()

        def request_authority(session: dict[str, object]) -> None:
            session["_normalized_session_request"]["operator_id"] = "forged-operator"

        def primary_frame_authority(session: dict[str, object]) -> None:
            session["frames"][0]["frame_role"] = "propagation_target"

        for label, mutate in {
            "normalized_request": request_authority,
            "primary_frame": primary_frame_authority,
        }.items():
            with self.subTest(label=label):
                replacement_path.write_bytes(pristine)
                forged = self.annotation._read_json(replacement_path, "session", 1024 * 1024)
                mutate(forged)
                self._write_session_unchecked(forged)
                before = _tree_bytes(self.repo_root)
                with self.assertRaises(BallAnnotationServiceError) as rejected:
                    self.annotation.create_review_proxy_replacement_session(
                        BLOCKED_SESSION_ID,
                        "probe-child-one",
                    )
                self.assertEqual(409, rejected.exception.status_code)
                self.assertEqual(before, _tree_bytes(self.repo_root))

    def test_pristine_orphan_path_body_session_id_mismatch_cannot_publish_groups(self) -> None:
        _blocked, replacement = self._publish_replacement(groups=False)
        replacement_path = self.annotation._sessions_root / f"{replacement['session_id']}.json"
        forged = deepcopy(replacement)
        forged["session_id"] = f"annotation-{replacement['request_sha256'][:16]}-fedcba987654"
        replacement_path.write_text(
            json.dumps(forged, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        before = _tree_bytes(self.repo_root)
        registry_before = self.annotation._registry_path.read_bytes()

        with self.assertRaises(BallAnnotationServiceError) as rejected:
            self.annotation.create_review_proxy_replacement_session(
                BLOCKED_SESSION_ID,
                "probe-child-one",
            )

        self.assertEqual(409, rejected.exception.status_code)
        self.assertEqual(registry_before, self.annotation._registry_path.read_bytes())
        self.assertEqual(before, _tree_bytes(self.repo_root))

    def test_coherently_resealed_renamed_orphan_cannot_publish_groups(self) -> None:
        _blocked, replacement = self._publish_replacement(groups=False)
        original_path = self.annotation._sessions_root / f"{replacement['session_id']}.json"
        forged = deepcopy(replacement)
        forged_request = deepcopy(forged["_normalized_session_request"])
        forged_request["operator_id"] = "forged-operator"
        forged_request_sha256 = canonical_sha256(forged_request)
        forged.update(
            {
                "session_id": f"annotation-{forged_request_sha256[:16]}-fedcba987654",
                "idempotency_key": forged_request_sha256,
                "request_sha256": forged_request_sha256,
                "_normalized_session_request": forged_request,
                "operator_id": "forged-operator",
            }
        )
        original_path.unlink()
        self._write_session_unchecked(forged)
        before = _tree_bytes(self.repo_root)
        registry_before = self.annotation._registry_path.read_bytes()

        with self.assertRaises(BallAnnotationServiceError) as rejected:
            self.annotation.create_review_proxy_replacement_session(
                BLOCKED_SESSION_ID,
                "probe-child-one",
            )

        self.assertEqual(409, rejected.exception.status_code)
        self.assertEqual(registry_before, self.annotation._registry_path.read_bytes())
        self.assertEqual(before, _tree_bytes(self.repo_root))

    def test_pristine_orphan_attempt_family_tampering_cannot_publish_groups(self) -> None:
        _blocked, replacement = self._publish_replacement(groups=False)
        forged = deepcopy(replacement)
        forged["attempt_family_sha256"] = SHA_A
        self._write_session_unchecked(forged)
        before = _tree_bytes(self.repo_root)
        registry_before = self.annotation._registry_path.read_bytes()

        with self.assertRaises(BallAnnotationServiceError) as rejected:
            self.annotation.create_review_proxy_replacement_session(
                BLOCKED_SESSION_ID,
                "probe-child-one",
            )

        self.assertEqual(409, rejected.exception.status_code)
        self.assertEqual(registry_before, self.annotation._registry_path.read_bytes())
        self.assertEqual(before, _tree_bytes(self.repo_root))

    def test_pristine_exact_orphan_can_publish_missing_groups(self) -> None:
        _blocked, replacement = self._publish_replacement(groups=False)
        expected_creation_authority = self.annotation._review_proxy_session_creation_authority(replacement)

        with mock.patch.object(
            self.annotation,
            "_derive_review_proxy_replacement_creation_authority",
            return_value=expected_creation_authority,
        ):
            recovered = self.annotation.create_review_proxy_replacement_session(
                BLOCKED_SESSION_ID,
                "probe-child-one",
            )

        self.assertEqual(replacement["session_id"], recovered["session_id"])
        registry = self.annotation._read_registry()
        rows = [entry for entry in registry["entries"] if entry["session_id"] == replacement["session_id"]]
        self.assertEqual(
            {group["group_id"] for group in replacement["sampling_manifest"]["groups"]},
            {entry["group_id"] for entry in rows},
        )
        self.assertTrue(all(entry["state"] == "revealed" for entry in rows))

    def test_startup_recovery_rejects_path_body_session_id_mismatch_without_registry_write(self) -> None:
        _blocked, replacement = self._publish_replacement(groups=False)
        replacement_path = self.annotation._sessions_root / f"{replacement['session_id']}.json"
        forged = deepcopy(replacement)
        forged["session_id"] = f"annotation-{replacement['request_sha256'][:16]}-fedcba987654"
        replacement_path.write_text(
            json.dumps(forged, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        self.annotation.close()
        before = _tree_bytes(self.repo_root)
        registry_before = self.annotation._registry_path.read_bytes()

        with self.assertRaises(BallAnnotationServiceError) as rejected:
            BallAnnotationService(
                self.repo_root,
                get_probe=lambda _job_id: {},
                create_probe=lambda _request: {},
                read_probe_artifact=lambda _job_id, _path: (b"", "", ""),
            )

        self.assertEqual(409, rejected.exception.status_code)
        self.assertEqual(registry_before, self.annotation._registry_path.read_bytes())
        self.assertEqual(before, _tree_bytes(self.repo_root))

    def test_startup_recovery_rejects_coherently_resealed_renamed_orphan_without_registry_write(self) -> None:
        _blocked, replacement = self._publish_replacement(groups=False)
        original_path = self.annotation._sessions_root / f"{replacement['session_id']}.json"
        forged = deepcopy(replacement)
        forged_request = deepcopy(forged["_normalized_session_request"])
        forged_request["operator_id"] = "forged-operator"
        forged_request_sha256 = canonical_sha256(forged_request)
        forged.update(
            {
                "session_id": f"annotation-{forged_request_sha256[:16]}-fedcba987654",
                "idempotency_key": forged_request_sha256,
                "request_sha256": forged_request_sha256,
                "_normalized_session_request": forged_request,
                "operator_id": "forged-operator",
            }
        )
        original_path.unlink()
        self._write_session_unchecked(forged)
        self.annotation.close()
        before = _tree_bytes(self.repo_root)
        registry_before = self.annotation._registry_path.read_bytes()

        with self.assertRaises(BallAnnotationServiceError) as rejected:
            BallAnnotationService(
                self.repo_root,
                get_probe=lambda _job_id: {},
                create_probe=lambda _request: {},
                read_probe_artifact=lambda _job_id, _path: (b"", "", ""),
            )

        self.assertEqual(409, rejected.exception.status_code)
        self.assertEqual(registry_before, self.annotation._registry_path.read_bytes())
        self.assertEqual(before, _tree_bytes(self.repo_root))

    def test_startup_recovery_publishes_groups_for_exact_pristine_orphan(self) -> None:
        _blocked, replacement = self._publish_replacement(groups=False)
        expected_creation_authority = self.annotation._review_proxy_session_creation_authority(replacement)
        self.annotation.close()

        with mock.patch.object(
            BallAnnotationService,
            "_derive_review_proxy_replacement_creation_authority",
            return_value=expected_creation_authority,
        ):
            self.annotation = BallAnnotationService(
                self.repo_root,
                get_probe=lambda _job_id: {},
                create_probe=lambda _request: {},
                read_probe_artifact=lambda _job_id, _path: (b"", "", ""),
            )

        registry = self.annotation._read_registry()
        rows = [entry for entry in registry["entries"] if entry["session_id"] == replacement["session_id"]]
        self.assertEqual(
            {group["group_id"] for group in replacement["sampling_manifest"]["groups"]},
            {entry["group_id"] for entry in rows},
        )
        self.assertTrue(all(entry["state"] == "revealed" for entry in rows))

    def test_advanced_orphan_without_upper_witness_fails_closed_without_group_synthesis(self) -> None:
        _blocked, replacement = self._publish_replacement(groups=False)
        advanced = deepcopy(replacement)
        advanced.update(
            status="finalizing",
            stage="finalizing",
            revisions=[{"frame_index": 1, "revision": 1}],
            final_package={"package_sha256": SHA_A},
        )
        self.annotation._persist_session(advanced)
        before = _tree_bytes(self.repo_root)
        with self.assertRaises(BallAnnotationServiceError) as inspect_rejected:
            self._inspect()
        self.assertEqual(409, inspect_rejected.exception.status_code)
        self.assertEqual(before, _tree_bytes(self.repo_root))

        with self.assertRaises(BallAnnotationServiceError) as adoption_rejected:
            self.annotation.create_review_proxy_replacement_session(
                BLOCKED_SESSION_ID,
                "probe-child-one",
            )
        self.assertEqual(409, adoption_rejected.exception.status_code)
        self.assertEqual(before, _tree_bytes(self.repo_root))

    def test_inspection_fails_closed_for_malformed_multiple_and_wrong_groups(self) -> None:
        blocked, replacement = self._publish_replacement(groups=False)
        replacement_path = self.annotation._sessions_root / f"{replacement['session_id']}.json"
        pristine = replacement_path.read_bytes()
        cases = {
            "malformed": lambda: replacement_path.write_text("{", encoding="utf-8"),
            "wrong_child": lambda: self._write_session_unchecked(
                {
                    **deepcopy(replacement),
                    "lineage": {"development_probe_job_ids": ["probe-parent-one", "probe-other"]},
                }
            ),
            "multiple": lambda: self._write_session_unchecked(
                {
                    **deepcopy(replacement),
                    "session_id": f"annotation-{replacement['request_sha256'][:16]}-abcdefabcdef",
                }
            ),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                replacement_path.write_bytes(pristine)
                second = (
                    self.annotation._sessions_root
                    / f"annotation-{replacement['request_sha256'][:16]}-abcdefabcdef.json"
                )
                second.unlink(missing_ok=True)
                mutate()
                before = _tree_bytes(self.repo_root)
                with self.assertRaises(BallAnnotationServiceError) as rejected:
                    self._inspect()
                self.assertEqual(409, rejected.exception.status_code)
                self.assertEqual(before, _tree_bytes(self.repo_root))

        replacement_path.write_bytes(pristine)
        (self.annotation._sessions_root / f"annotation-{replacement['request_sha256'][:16]}-abcdefabcdef.json").unlink(
            missing_ok=True
        )
        wrong_group = _group(10)
        self.annotation._record_groups(
            replacement["session_id"],
            SHA_B,
            [wrong_group],
            data_role="development",
            state="revealed",
        )
        before = _tree_bytes(self.repo_root)
        with self.assertRaises(BallAnnotationServiceError) as wrong_registry:
            self._inspect()
        self.assertEqual(409, wrong_registry.exception.status_code)
        self.assertEqual(before, _tree_bytes(self.repo_root))

        registry_path = self.annotation._registry_path
        registry_path.write_text("{", encoding="utf-8")
        before_malformed_registry = _tree_bytes(self.repo_root)
        with self.assertRaises(BallAnnotationServiceError) as malformed_registry:
            self._inspect()
        self.assertEqual(409, malformed_registry.exception.status_code)
        self.assertEqual(before_malformed_registry, _tree_bytes(self.repo_root))

    def test_get_and_retry_reject_lower_floor_without_writes_or_descendants(self) -> None:
        self._publish_replacement(groups=True)
        record = _record(self._blocked_record_sha256())
        self.api._persist_detector_review_proxy_repair(record)
        before = _tree_bytes(self.repo_root)
        coordinator = mock.Mock()
        with (
            mock.patch.object(
                self.api,
                "_detector_development_service",
                return_value=_ReadyChildDetector(),
            ),
            mock.patch.object(
                self.api,
                "_ball_annotation_service",
                return_value=self.annotation,
            ),
            mock.patch.object(
                self.api,
                "_detector_review_proxy_coordinator",
                return_value=coordinator,
            ),
        ):
            with self.assertRaises(DetectorDevelopmentError) as get_error:
                self.api.get_detector_review_proxy_repair(record["repair_id"])
            with self.assertRaises(DetectorDevelopmentError) as retry_error:
                self.api.retry_detector_review_proxy_repair(record["repair_id"])

        self.assertEqual("review_proxy_side_effect_floor_mismatch", get_error.exception.code)
        self.assertEqual(409, get_error.exception.status_code)
        self.assertEqual("review_proxy_retry_ineligible", retry_error.exception.code)
        self.assertEqual(409, retry_error.exception.status_code)
        coordinator.retry_repair.assert_not_called()
        self.assertEqual(before, _tree_bytes(self.repo_root))
        self.assertEqual(
            [record["repair_id"]],
            [path.stem for path in self.api._detector_review_proxy_jobs_root.glob("*.json")],
        )

    def test_startup_recovery_reconstructs_exact_lower_prefix_before_resuming(self) -> None:
        self._publish_replacement(groups=True)
        record = _record(self._blocked_record_sha256())
        self.api._persist_detector_review_proxy_repair(record)
        with (
            mock.patch.object(
                self.api,
                "_detector_development_service",
                return_value=_ReadyChildDetector(),
            ),
            mock.patch.object(
                self.api,
                "_ball_annotation_service",
                return_value=self.annotation,
            ),
            mock.patch.object(self.api, "_start_detector_review_proxy_continuation") as start,
        ):
            self.api._recover_detector_review_proxy_repairs()

        recovered = self.api._read_detector_review_proxy_repair(record["repair_id"])
        self.assertEqual("committing", recovered["status"])
        self.assertEqual("groups_published", recovered["stage"])
        self.assertEqual("probe-child-one", recovered["child_probe"]["job_id"])
        self.assertEqual(
            _replacement_session(_blocked_session())["session_id"],
            recovered["replacement_session"]["session_id"],
        )
        self.assertIsNotNone(recovered["group_publication"])
        start.assert_called_once_with(record["repair_id"])

    def test_watcher_records_the_actual_lower_floor_not_the_stale_journal_rank(self) -> None:
        self._publish_replacement(groups=True)
        record = _record(self._blocked_record_sha256())
        record.update(
            status="committing",
            error_code=None,
            blocker_code=None,
            recovery_action=None,
        )
        self.api._persist_detector_review_proxy_repair(record)
        with (
            mock.patch.object(
                self.api,
                "_detector_development_service",
                return_value=_ReadyChildDetector(),
            ),
            mock.patch.object(
                self.api,
                "_ball_annotation_service",
                return_value=self.annotation,
            ),
            mock.patch.object(
                self.api,
                "_advance_detector_review_proxy_repair",
                side_effect=RuntimeError("failure after lower publication"),
            ),
        ):
            self.api._watch_detector_review_proxy_repair(record["repair_id"], "test-lower-floor")

        recovered = self.api._read_detector_review_proxy_repair(record["repair_id"])
        self.assertEqual("failed", recovered["status"])
        self.assertEqual("groups_published", recovered["stage"])
        self.assertEqual("resume", recovered["recovery_action"])
        self.assertIsNotNone(recovered["child_probe"])
        self.assertIsNotNone(recovered["replacement_session"])
        self.assertIsNotNone(recovered["group_publication"])

    def test_claim_without_job_is_a_non_retryable_floor_and_startup_resumes_same_attempt(self) -> None:
        record = _record()
        self.api._persist_detector_review_proxy_repair(record)
        before = _tree_bytes(self.repo_root)
        with mock.patch.object(
            self.api,
            "_detector_development_service",
            return_value=_ClaimOnlyDetector(),
        ):
            with self.assertRaises(DetectorDevelopmentError) as get_error:
                self.api.get_detector_review_proxy_repair(record["repair_id"])
            with self.assertRaises(DetectorDevelopmentError) as retry_error:
                self.api.retry_detector_review_proxy_repair(record["repair_id"])
        self.assertEqual("review_proxy_side_effect_floor_mismatch", get_error.exception.code)
        self.assertEqual("review_proxy_retry_ineligible", retry_error.exception.code)
        self.assertEqual(before, _tree_bytes(self.repo_root))

        with (
            mock.patch.object(
                self.api,
                "_detector_development_service",
                return_value=_ClaimOnlyDetector(),
            ),
            mock.patch.object(self.api, "_start_detector_review_proxy_continuation") as start,
        ):
            self.api._recover_detector_review_proxy_repairs()
        recovered = self.api._read_detector_review_proxy_repair(record["repair_id"])
        self.assertEqual("committing", recovered["status"])
        self.assertEqual("continuation_intent", recovered["stage"])
        self.assertIsNone(recovered["child_probe"])
        start.assert_called_once_with(record["repair_id"])


if __name__ == "__main__":
    unittest.main()
