from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from football_tracking.api.service import ApiService
from football_tracking.detector_development_common import (
    DetectorDevelopmentError,
    atomic_write_json,
    canonical_sha256,
    hash_regular_file,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


class _SimulatedCrash(BaseException):
    pass


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
            "sampled_frame_sha256s": {str(index): SHA_C for index in authority["frame_indices"]},
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


def _child_job(repair_id: str = "repair-test-1") -> dict[str, object]:
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
                "repair_evidence": {"repair_id": repair_id},
                "continuation_execution_binding": continuation,
            },
        },
        "report": {
            "report_sha256": SHA_B,
            "review_proxy_manifest": {
                "manifest_sha256": SHA_C,
                "mapping_sha256": SHA_A,
                "source": {
                    "sha256": SHA_B,
                    "file_identity_sha256": SHA_C,
                    "size_bytes": 1234,
                },
                "proxy": {
                    "sha256": SHA_A,
                    "size_bytes": 123,
                    "width": 2560,
                    "height": 720,
                    "frame_count": 12,
                    "fps": 20.0,
                },
                "expected_frame_indices": [1],
            },
        },
    }


def _replacement_session() -> dict[str, object]:
    return {
        "session_id": "annotation-replacement-one",
        "request_sha256": SHA_A,
        "status": "annotating",
        "retry_from_session_id": "annotation-blocked-1",
        "retry_lineage": {"mode": "review_proxy_decode_upgrade"},
        "attempt_family_sha256": SHA_B,
        "lineage": {
            "development_probe_job_ids": [
                "probe-parent-one",
                "probe-child-one",
            ]
        },
    }


class _FakeContinuationDetector:
    def __init__(
        self,
        *,
        crash_after_child_publish: bool = False,
        repair_id: str = "repair-test-1",
    ) -> None:
        self.plan: dict[str, object] | None = None
        self.child = _child_job(repair_id)
        self.crash_after_child_publish = crash_after_child_publish
        self.crashed = False
        self.child_ids: set[str] = set()

    def review_proxy_upgrade_child_plan(self, _parent: str, *, repair_evidence):
        self.plan = _child_plan(repair_evidence["repair_id"])
        self.child["frozen_request"]["review_proxy_upgrade"]["repair_evidence"] = {
            "repair_id": repair_evidence["repair_id"]
        }
        return deepcopy(self.plan)

    def create_review_proxy_upgrade_child(self, _parent: str, **kwargs):
        self.assert_plan(kwargs["expected_child_plan"])
        self.child_ids.add(self.child["job_id"])
        if self.crash_after_child_publish and not self.crashed:
            self.crashed = True
            raise _SimulatedCrash("child published before ready journal")
        return self._public_child()

    def _public_child(self):
        child = deepcopy(self.child)
        child.pop("resource_sha256", None)
        return child

    def assert_plan(self, value) -> None:
        if self.plan is None or value != self.plan:
            raise AssertionError("child plan changed")

    def get_review_proxy_upgrade_parent(self, _parent: str):
        return {"parent_probe_record_sha256": SHA_A}

    def get_verified_probe(self, _job_id: str):
        return self._public_child()

    def get_verified_probe_job_record(self, _job_id: str):
        return deepcopy(self.child)

    def get_review_proxy_upgrade_child(self, _parent: str):
        return self._public_child() if self.child_ids else None


class _FakeContinuationAnnotation:
    def __init__(self, *, crash_after_session_persist: bool = False) -> None:
        self.session = _replacement_session()
        self.crash_after_session_persist = crash_after_session_persist
        self.crashed = False
        self.session_ids: set[str] = set()
        self.group_publication_ids: set[str] = set()

    def create_review_proxy_replacement_session(self, _blocked: str, _child: str):
        self.session_ids.add(self.session["session_id"])
        if self.crash_after_session_persist and not self.crashed:
            self.crashed = True
            raise _SimulatedCrash("session persisted before groups")
        self.group_publication_ids.add(self.session["session_id"])
        return deepcopy(self.session)

    def get_review_proxy_replacement_commit(self, session_id: str, **_kwargs):
        self.group_publication_ids.add(session_id)
        commit = {
            "session_id": session_id,
            "blocked_session_id": "annotation-blocked-1",
            "child_probe_job_id": "probe-child-one",
            "session_record_sha256": SHA_B,
            "session_creation_authority_sha256": SHA_A,
            "group_publication_sha256": SHA_C,
        }
        return {**commit, "commit_sha256": canonical_sha256(commit)}

    def verify_blocked_review_proxy_parent_immutable(self, _session: str, expected: str):
        if expected != SHA_B:
            raise AssertionError("blocked parent changed")
        return expected

    def verify_ready_review_proxy_replacement(self, **kwargs):
        return {
            "session": deepcopy(self.session),
            "blocked_authority": {},
            "session_creation_authority_sha256": kwargs["session_creation_authority_sha256"],
            "group_publication_sha256": kwargs["group_publication_sha256"],
        }


def _record(repair_id: str = "repair-test-1") -> dict[str, object]:
    blocked_session_id = "annotation-blocked-1"
    request_sha256 = canonical_sha256({"blocked_session_id": blocked_session_id})
    return {
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
        "authority": {
            "blocked_session_id": blocked_session_id,
            "blocked_session_request_sha256": SHA_A,
            "blocked_session_record_sha256": SHA_B,
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
            "locked_profile_id": "locked-profile-one",
            "locked_profile_sha256": SHA_A,
            "frame_indices": [1, 2, 3, 4],
            "sampling_manifest_sha256": SHA_B,
            "temporal_groups_sha256": SHA_C,
            "candidate_evidence_sha256": SHA_A,
            "replacement_request_authority_sha256": SHA_B,
        },
        "low_request_sha256": SHA_B,
        "low_progress": {"completed": 0, "total": 40},
        "continuation_intent": None,
        "child_probe": None,
        "replacement_session": None,
        "result": None,
        "error_code": None,
        "blocker_code": None,
        "recovery_action": None,
        "created_at": "2026-07-18T12:00:00+00:00",
        "updated_at": "2026-07-18T12:00:00+00:00",
    }


def _low_job_for(
    record: dict[str, object],
    *,
    status: str,
    error_code: str | None,
) -> dict[str, object]:
    authority = record["authority"]
    frozen = {
        "source_id": authority["source_id"],
        "source_sha256": authority["source_sha256"],
        "source_size_bytes": authority["source_size_bytes"],
        "source_width": authority["source_width"],
        "source_height": authority["source_height"],
        "source_frame_count": authority["source_frame_count"],
        "source_fps": authority["source_fps"],
        "sampled_frame_indices": authority["frame_indices"],
    }
    request_sha256 = canonical_sha256(frozen)
    record["low_request_sha256"] = request_sha256
    return {
        "repair_id": record["repair_id"],
        "attempt_root_repair_id": record["attempt_root_repair_id"],
        "attempt_number": record["attempt_number"],
        "retry_from_repair_id": record["retry_from_repair_id"],
        "request_sha256": request_sha256,
        "frozen_request": frozen,
        "status": status,
        "error_code": error_code,
    }


class DetectorReviewProxyServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temporary.name)
        for name in ("config", "data", "outputs", "weights"):
            (self.repo_root / name).mkdir(parents=True, exist_ok=True)
        self.service = ApiService(self.repo_root)

    def tearDown(self) -> None:
        self.service.close()
        self.temporary.cleanup()

    def test_pre_side_effect_path_failures_are_retryable_but_digest_mismatch_is_not(self) -> None:
        for error_code in ("source_changed", "path_unavailable"):
            with self.subTest(error_code=error_code):
                retryable = _record(f"repair-{error_code.replace('_', '-')}")
                retryable.update(
                    status="failed",
                    stage="failed",
                    error_code=error_code,
                    blocker_code=None,
                    recovery_action="retry",
                )
                with mock.patch.object(
                    self.service,
                    "_review_proxy_actual_side_effect_floor",
                    return_value=0,
                ):
                    self.assertTrue(self.service._can_retry_detector_review_proxy_repair(retryable))
                self.assertEqual(
                    "retry",
                    self.service._review_proxy_expected_recovery_action(
                        status="failed",
                        rank=0,
                        error_code=error_code,
                    ),
                )

        digest_mismatch = _record("repair-digest-mismatch")
        digest_mismatch.update(
            status="failed",
            stage="failed",
            error_code="source_digest_or_size_mismatch",
            blocker_code=None,
            recovery_action=None,
        )
        with mock.patch.object(
            self.service,
            "_review_proxy_actual_side_effect_floor",
            return_value=0,
        ):
            self.assertFalse(self.service._can_retry_detector_review_proxy_repair(digest_mismatch))
        self.assertIsNone(
            self.service._review_proxy_expected_recovery_action(
                status="failed",
                rank=0,
                error_code="source_digest_or_size_mismatch",
            )
        )

    def test_path_unavailable_retry_stops_before_durable_child_side_effects(self) -> None:
        record = _record("repair-path-unavailable-boundary")
        record.update(
            status="failed",
            stage="failed",
            error_code="path_unavailable",
            blocker_code=None,
            recovery_action="retry",
        )
        for rank, expected_retry, expected_action in ((2, True, "retry"), (3, False, "resume")):
            with (
                self.subTest(rank=rank),
                mock.patch.object(
                    self.service,
                    "_review_proxy_actual_side_effect_floor",
                    return_value=rank,
                ),
            ):
                self.assertIs(
                    expected_retry,
                    self.service._can_retry_detector_review_proxy_repair(record),
                )
                self.assertEqual(
                    expected_action,
                    self.service._review_proxy_expected_recovery_action(
                        status="failed",
                        rank=rank,
                        error_code="path_unavailable",
                    ),
                )

    def test_low_path_unavailable_is_persisted_as_retryable_upper_failure(self) -> None:
        repair_id = "repair-path-unavailable-propagation"
        record = _record(repair_id)
        low_record = _low_job_for(record, status="failed", error_code="path_unavailable")
        self.service._persist_detector_review_proxy_repair(record)
        coordinator = mock.Mock()
        coordinator.get_repair.return_value = low_record

        with mock.patch.object(
            self.service,
            "_detector_review_proxy_coordinator",
            return_value=coordinator,
        ):
            self.assertTrue(self.service._advance_detector_review_proxy_repair(repair_id))

        persisted = self.service._read_detector_review_proxy_repair(repair_id)
        self.assertEqual("failed", persisted["status"])
        self.assertEqual("path_unavailable", persisted["error_code"])
        self.assertEqual("retry", persisted["recovery_action"])
        with mock.patch.object(
            self.service,
            "_review_proxy_actual_side_effect_floor",
            return_value=0,
        ):
            public = self.service._public_detector_review_proxy_repair(persisted)
        self.assertTrue(public["can_retry"])

    def test_low_terminal_error_mismatch_fails_closed(self) -> None:
        record = _record("repair-terminal-mismatch")
        record.update(
            status="failed",
            stage="failed",
            error_code="source_changed",
            blocker_code=None,
            recovery_action="retry",
        )
        low_record = _low_job_for(
            record,
            status="failed",
            error_code="source_digest_or_size_mismatch",
        )
        coordinator = mock.Mock()
        coordinator.get_repair.return_value = low_record
        detector = mock.Mock()
        detector.get_review_proxy_upgrade_child.return_value = None

        with (
            mock.patch.object(
                self.service,
                "_detector_review_proxy_coordinator",
                return_value=coordinator,
            ),
            mock.patch.object(
                self.service,
                "_detector_development_service",
                return_value=detector,
            ),
            self.assertRaisesRegex(
                DetectorDevelopmentError,
                "Terminal review-proxy job differs",
            ),
        ):
            self.service._can_retry_detector_review_proxy_repair(record)

    def test_invalid_proxy_evidence_after_proxy_commit_resumes_same_attempt(self) -> None:
        repair_id = "repair-invalid-evidence-resume"
        record = _record(repair_id)
        record.update(
            status="blocked",
            stage="proxy_ready",
            error_code="invalid_review_proxy_repair_evidence",
            blocker_code="invalid_review_proxy_repair_evidence",
            recovery_action="resume",
            low_progress={"completed": 37, "total": 37},
        )
        self.service._persist_detector_review_proxy_repair(record)
        low_record = _low_job_for(record, status="ready", error_code=None)
        coordinator = mock.Mock()
        coordinator.get_repair.return_value = low_record
        coordinator.get_verified_proxy.return_value = low_record

        with (
            mock.patch.object(
                self.service,
                "_detector_review_proxy_coordinator",
                return_value=coordinator,
            ),
            mock.patch.object(
                self.service,
                "_commit_detector_review_proxy_continuation",
            ) as commit,
        ):
            self.assertTrue(self.service._advance_detector_review_proxy_repair(repair_id))

        commit.assert_called_once_with(repair_id, low_record)
        coordinator.retry_repair.assert_not_called()
        persisted = self.service._read_detector_review_proxy_repair(repair_id)
        self.assertEqual("committing", persisted["status"])
        self.assertEqual("proxy_ready", persisted["stage"])
        self.assertIsNone(persisted["error_code"])
        self.assertIsNone(persisted["blocker_code"])
        self.assertIsNone(persisted["recovery_action"])
        with mock.patch.object(
            self.service,
            "_review_proxy_actual_side_effect_floor",
            return_value=1,
        ):
            self.assertFalse(self.service._can_retry_detector_review_proxy_repair(record))
        for rank in (1, 2):
            with self.subTest(resumable_rank=rank):
                self.assertEqual(
                    "resume",
                    self.service._review_proxy_expected_recovery_action(
                        status="blocked",
                        rank=rank,
                        error_code="invalid_review_proxy_repair_evidence",
                    ),
                )
        self.assertIsNone(
            self.service._review_proxy_expected_recovery_action(
                status="blocked",
                rank=0,
                error_code="invalid_review_proxy_repair_evidence",
            )
        )
        self.assertIsNone(
            self.service._review_proxy_expected_recovery_action(
                status="blocked",
                rank=1,
                error_code="source_digest_or_size_mismatch",
            )
        )

    def test_low_ready_is_reconciled_as_proxy_ready(self) -> None:
        record = _record("repair-low-ready")
        low_record = _low_job_for(record, status="ready", error_code=None)
        coordinator = mock.Mock()
        coordinator.get_repair.return_value = low_record
        detector = mock.Mock()
        detector.get_review_proxy_upgrade_child.return_value = None

        with (
            mock.patch.object(
                self.service,
                "_detector_review_proxy_coordinator",
                return_value=coordinator,
            ),
            mock.patch.object(
                self.service,
                "_detector_development_service",
                return_value=detector,
            ),
        ):
            rank, changed = self.service._reconcile_review_proxy_lower_side_effects(record)

        self.assertEqual(1, rank)
        self.assertTrue(changed)
        self.assertEqual("proxy_ready", record["stage"])

    def test_startup_ignores_a_later_attempt_child_when_recovering_an_older_attempt(
        self,
    ) -> None:
        older_id = "repair-older-failure"
        older = _record(older_id)
        older.update(
            status="failed",
            stage="failed",
            error_code="path_unavailable",
            blocker_code=None,
            recovery_action="retry",
        )
        self.service._persist_detector_review_proxy_repair(older)

        current_id = "repair-current-child"
        current = _record(current_id)
        current.update(
            status="failed",
            stage="continuation_intent",
            continuation_intent=_continuation_intent(current_id, current["authority"]),
            error_code="review_proxy_continuation_failed",
            blocker_code=None,
            recovery_action="retry",
        )
        self.service._persist_detector_review_proxy_repair(current)

        detector = _FakeContinuationDetector(repair_id=current_id)
        detector.child_ids.add("probe-child-one")
        annotation = mock.Mock()
        annotation.inspect_review_proxy_replacement_side_effect.return_value = None
        with (
            mock.patch.object(
                self.service,
                "_detector_development_service",
                return_value=detector,
            ),
            mock.patch.object(
                self.service,
                "_ball_annotation_service",
                return_value=annotation,
            ),
            mock.patch.object(
                self.service,
                "_inspect_review_proxy_low_job",
                return_value=0,
            ),
            mock.patch.object(self.service, "_start_detector_review_proxy_continuation") as start,
        ):
            self.service._recover_detector_review_proxy_repairs()

        recovered_older = self.service._read_detector_review_proxy_repair(older_id)
        recovered_current = self.service._read_detector_review_proxy_repair(current_id)
        self.assertEqual("failed", recovered_older["status"])
        self.assertEqual("failed", recovered_older["stage"])
        self.assertIsNone(recovered_older["child_probe"])
        self.assertEqual("committing", recovered_current["status"])
        self.assertEqual("child_probe_ready", recovered_current["stage"])
        self.assertEqual("probe-child-one", recovered_current["child_probe"]["job_id"])
        start.assert_called_once_with(current_id)

        detector.review_proxy_upgrade_child_plan = mock.Mock(
            side_effect=AssertionError("a committed intent must not be regenerated")
        )
        detector.create_review_proxy_upgrade_child = mock.Mock(
            side_effect=AssertionError("a committed child must not be recreated")
        )
        annotation.create_review_proxy_replacement_session.side_effect = _SimulatedCrash(
            "stop after verified child replay"
        )
        committed_intent = recovered_current["continuation_intent"]
        with (
            mock.patch.object(
                self.service,
                "_load_verified_review_proxy_evidence",
                return_value=(
                    deepcopy(committed_intent["repair_evidence"]),
                    deepcopy(committed_intent["proxy_media"]),
                    {index: b"sample" for index in recovered_current["authority"]["frame_indices"]},
                ),
            ),
            mock.patch.object(
                self.service,
                "_detector_development_service",
                return_value=detector,
            ),
            mock.patch.object(
                self.service,
                "_ball_annotation_service",
                return_value=annotation,
            ),
            self.assertRaisesRegex(_SimulatedCrash, "verified child replay"),
        ):
            self.service._commit_detector_review_proxy_continuation(
                current_id,
                {"progress": {"completed": 37, "total": 37}},
            )
        detector.review_proxy_upgrade_child_plan.assert_not_called()
        detector.create_review_proxy_upgrade_child.assert_not_called()

    def test_retry_allows_verified_low_ready_before_upper_proxy_ready_journal(self) -> None:
        repair_id = "repair-low-ready-gap"
        record = _record(repair_id)
        record.update(
            status="failed",
            stage="failed",
            error_code="source_changed",
            blocker_code=None,
            recovery_action="retry",
        )
        low_record = _low_job_for(record, status="ready", error_code=None)
        self.service._persist_detector_review_proxy_repair(record)
        coordinator = mock.Mock()
        coordinator.get_repair.return_value = low_record
        coordinator.retry_repair.return_value = {
            "repair_id": "repair-low-ready-gap-retry",
            "request_sha256": SHA_A,
            "progress": {"completed": 0, "total": 40},
        }
        detector = mock.Mock()
        detector.get_review_proxy_upgrade_child.return_value = None
        detector.get_review_proxy_upgrade_parent.return_value = {"parent": "verified"}
        annotation = mock.Mock()
        annotation.get_review_proxy_repair_authority.return_value = {
            "parent_probe_job_id": record["authority"]["parent_probe_job_id"],
        }

        with (
            mock.patch.object(
                self.service,
                "_detector_review_proxy_coordinator",
                return_value=coordinator,
            ),
            mock.patch.object(
                self.service,
                "_detector_development_service",
                return_value=detector,
            ),
            mock.patch.object(
                self.service,
                "_ball_annotation_service",
                return_value=annotation,
            ),
            mock.patch.object(self.service, "_validate_review_proxy_repair_authority"),
            mock.patch.object(
                self.service,
                "_build_review_proxy_repair_authority",
                return_value=record["authority"],
            ),
            mock.patch.object(self.service, "_start_detector_review_proxy_continuation"),
            mock.patch.object(
                self.service,
                "_public_detector_review_proxy_repair",
                side_effect=lambda value: deepcopy(value),
            ),
        ):
            retried = self.service.retry_detector_review_proxy_repair(repair_id)

        self.assertEqual("repair-low-ready-gap-retry", retried["repair_id"])
        coordinator.retry_repair.assert_called_once_with(
            repair_id,
            allow_ready_pre_reveal=True,
        )

    def test_transaction_digest_rejects_authority_phase_child_and_session_tampering(
        self,
    ) -> None:
        record = _record()
        self.service._persist_detector_review_proxy_repair(record)
        path = self.service._detector_review_proxy_jobs_root / "repair-test-1.json"
        original = path.read_bytes()
        cases = {
            "authority": lambda value: value["authority"].update(parent_probe_record_sha256=SHA_C),
            "phase": lambda value: value.update(status="committing", stage="continuation_intent"),
            "child": lambda value: value.update(child_probe={"job_id": "probe-forged"}),
            "replacement": lambda value: value.update(replacement_session={"session_id": "annotation-forged"}),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                value = json.loads(original)
                mutate(value)
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(
                    DetectorDevelopmentError,
                    "Persisted review-proxy continuation is invalid",
                ):
                    self.service._read_detector_review_proxy_repair("repair-test-1")
                path.write_bytes(original)

    def test_resealed_logically_impossible_transaction_is_rejected_on_write_and_read(
        self,
    ) -> None:
        base = _record()
        intent = _continuation_intent(str(base["repair_id"]), base["authority"])
        cases = {
            "ready_without_prefix": {
                "status": "ready",
                "stage": "ready",
                "result": {},
            },
            "child_stage_without_child": {
                "status": "committing",
                "stage": "child_probe_ready",
                "continuation_intent": intent,
            },
            "intent_stage_without_intent": {
                "status": "committing",
                "stage": "continuation_intent",
            },
            "queued_with_intent": {
                "status": "queued",
                "stage": "queued",
                "continuation_intent": intent,
            },
            "unknown_stage": {
                "status": "queued",
                "stage": "forged-stage",
            },
        }
        path = self.service._detector_review_proxy_jobs_root / "repair-test-1.json"
        for label, patch in cases.items():
            with self.subTest(label=label):
                impossible = deepcopy(base)
                impossible.update(deepcopy(patch))
                with self.assertRaisesRegex(
                    DetectorDevelopmentError,
                    "continuation transaction is invalid",
                ):
                    self.service._persist_detector_review_proxy_repair(impossible)
                impossible["transaction_sha256"] = canonical_sha256(impossible)
                # Re-seal correctly after excluding the digest itself.
                body = deepcopy(impossible)
                body.pop("transaction_sha256")
                impossible["transaction_sha256"] = canonical_sha256(body)
                path.write_text(json.dumps(impossible), encoding="utf-8")
                with self.assertRaisesRegex(
                    DetectorDevelopmentError,
                    "Persisted review-proxy continuation is invalid",
                ):
                    self.service._read_detector_review_proxy_repair("repair-test-1")
                path.unlink()

    def test_cancel_requires_low_authoritative_cancelled_state(self) -> None:
        cases = {
            "cancelled": ("cancelled", False, "cancelled"),
            "running": ("running", False, "running"),
            "committing": ("committing", True, "committing"),
            "ready": ("ready", True, "committing"),
        }
        for label, (low_status, raises, expected_upper) in cases.items():
            with self.subTest(label=label):
                repair_id = f"repair-{label}"
                record = _record(repair_id)
                low_record = _low_job_for(
                    record,
                    status=low_status,
                    error_code="cancelled" if low_status == "cancelled" else None,
                )
                self.service._persist_detector_review_proxy_repair(record)
                low = mock.Mock()
                low.get_repair.return_value = low_record
                low.cancel_repair.return_value = {
                    "repair_id": repair_id,
                    "status": low_status,
                    "stage": low_status,
                    "progress": {"completed": 3, "total": 40},
                }
                with (
                    mock.patch.object(
                        self.service,
                        "_detector_review_proxy_coordinator",
                        return_value=low,
                    ),
                    mock.patch.object(
                        self.service,
                        "_start_detector_review_proxy_continuation",
                    ) as start,
                ):
                    if raises:
                        with self.assertRaisesRegex(DetectorDevelopmentError, "commit point"):
                            self.service.cancel_detector_review_proxy_repair(repair_id)
                        start.assert_called_once_with(repair_id)
                    else:
                        public = self.service.cancel_detector_review_proxy_repair(repair_id)
                        self.assertEqual(expected_upper, public["status"])
                persisted = self.service._read_detector_review_proxy_repair(repair_id)
                self.assertEqual(expected_upper, persisted["status"])
                if low_status != "cancelled":
                    self.assertNotEqual("cancelled", persisted["status"])

    def test_source_frame_progress_is_a_truthful_ratio_of_low_work_units(
        self,
    ) -> None:
        record = _record()
        with mock.patch.object(
            self.service,
            "_review_proxy_actual_side_effect_floor",
            return_value=0,
        ):
            for completed, expected in ((0, 0), (1, 0), (20, 6), (39, 11), (40, 12)):
                with self.subTest(completed=completed):
                    value = deepcopy(record)
                    value["low_progress"] = {"completed": completed, "total": 40}
                    public = self.service._public_detector_review_proxy_repair(value)
                    self.assertEqual(expected, public["progress"]["source_frames_completed"])
            forged = deepcopy(record)
            forged["low_progress"] = {"completed": 20, "total": 39}
            with self.assertRaisesRegex(DetectorDevelopmentError, "frozen work authority"):
                self.service._public_detector_review_proxy_repair(forged)

    def _published_low_proxy(self) -> tuple[dict[str, object], dict[str, object]]:
        repair_id = "repair-published"
        root = self.repo_root / "data" / "ball_detector_development_v1" / "review_proxies" / "results" / repair_id
        (root / "samples").mkdir(parents=True)
        proxy_bytes = b"proxy-media"
        sample_bytes = b"sample-jpeg"
        (root / "review_proxy.mp4").write_bytes(proxy_bytes)
        (root / "samples" / "frame_0000000001.jpg").write_bytes(sample_bytes)
        proxy_sha = canonical_sha256({"not": "the file"})
        import hashlib

        proxy_sha = hashlib.sha256(proxy_bytes).hexdigest()
        sample_sha = hashlib.sha256(sample_bytes).hexdigest()
        code_files = {"football_tracking/example.py": SHA_A}
        runtime = {"python_version": "3.11-test"}
        encoder_preset = {"codec": "libx264"}
        decoder = {"ffmpeg_sha256": SHA_B}
        binding = {
            "schema_version": "1.0",
            "artifact_type": "detector_review_proxy_repair_execution_binding",
            "code_files": code_files,
            "code_bundle_sha256": canonical_sha256(code_files),
            "runtime": runtime,
            "runtime_sha256": canonical_sha256(runtime),
            "ffmpeg": {"sha256": SHA_B},
            "encoder_preset": encoder_preset,
            "encoder_preset_sha256": canonical_sha256(encoder_preset),
            "decoder_fingerprint": decoder,
            "decoder_fingerprint_sha256": canonical_sha256(decoder),
        }
        binding["binding_sha256"] = canonical_sha256(binding)
        integrity = {"full_proxy_decode_verified": True}
        report = {
            "schema_version": "1.0",
            "artifact_type": "detector_review_proxy_report",
            "repair_id": repair_id,
            "request_sha256": SHA_B,
            "proxy": {
                "relative_path": "review_proxy.mp4",
                "sha256": proxy_sha,
                "size_bytes": len(proxy_bytes),
                "width": 2560,
                "height": 720,
                "frame_count": 12,
                "fps": 20.0,
            },
            "sampled_frames": [
                {
                    "frame_index": 1,
                    "relative_path": "samples/frame_0000000001.jpg",
                    "sha256": sample_sha,
                    "size_bytes": len(sample_bytes),
                }
            ],
            "repair_execution_binding": binding,
            "integrity": integrity,
        }
        atomic_write_json(
            root / "detector_review_proxy_report.v1.json",
            report,
            trusted_root=root,
        )
        report_sha, report_size = hash_regular_file(
            root / "detector_review_proxy_report.v1.json",
            "test report",
            trusted_root=root,
        )
        manifest = {
            "schema_version": "1.0",
            "artifact_type": "detector_review_proxy_result_manifest",
            "repair_id": repair_id,
            "request_sha256": SHA_B,
            "proxy_sha256": proxy_sha,
            "proxy_size_bytes": len(proxy_bytes),
            "sample_sha256s": [sample_sha],
            "integrity_sha256": canonical_sha256(integrity),
            "report_file_sha256": report_sha,
            "report_file_size_bytes": report_size,
        }
        atomic_write_json(
            root / "detector_review_proxy_manifest.v1.json",
            manifest,
            trusted_root=root,
        )
        manifest_sha, _ = hash_regular_file(
            root / "detector_review_proxy_manifest.v1.json",
            "test manifest",
            trusted_root=root,
        )
        low = {
            "repair_id": repair_id,
            "attempt_root_repair_id": repair_id,
            "attempt_number": 1,
            "retry_from_repair_id": None,
            "request_sha256": SHA_B,
            "status": "ready",
            "report": deepcopy(report),
            "result_manifest_sha256": manifest_sha,
        }
        record = _record(repair_id)
        record["low_request_sha256"] = SHA_B
        record["authority"]["frame_indices"] = [1]
        return record, low

    def test_continuation_uses_reparsed_published_report_not_low_memory_copy(
        self,
    ) -> None:
        record, low = self._published_low_proxy()
        repair, proxy, samples = self.service._load_verified_review_proxy_evidence(record, low)
        self.assertEqual(SHA_B, repair["repair_request_sha256"])
        self.assertEqual(2560, proxy["width"])
        self.assertEqual([1], sorted(samples))

        for field, value in (
            ("attempt_root_repair_id", "repair-forged-root"),
            ("attempt_number", 2),
            ("retry_from_repair_id", "repair-forged-parent"),
            ("request_sha256", SHA_A),
        ):
            inconsistent_lineage = deepcopy(low)
            inconsistent_lineage[field] = value
            with (
                self.subTest(low_lineage=field),
                self.assertRaisesRegex(
                    DetectorDevelopmentError,
                    "does not match its continuation",
                ),
            ):
                self.service._load_verified_review_proxy_evidence(record, inconsistent_lineage)

        inconsistent = deepcopy(low)
        inconsistent["report"]["proxy"]["width"] = 1280
        with self.assertRaisesRegex(DetectorDevelopmentError, "manifest changed"):
            self.service._load_verified_review_proxy_evidence(record, inconsistent)

        result_root = (
            self.repo_root / "data" / "ball_detector_development_v1" / "review_proxies" / "results" / "repair-published"
        )
        (result_root / "unexpected.txt").write_text("forged", encoding="utf-8")
        with self.assertRaisesRegex(DetectorDevelopmentError, "unexpected artifacts"):
            self.service._load_verified_review_proxy_evidence(record, low)

    def test_ready_replay_reconstructs_exact_proxy_result(self) -> None:
        repair_id = "repair-ready-exact-result"
        record = _record(repair_id)
        record["authority"]["frame_indices"] = [1]
        record["low_progress"] = {"completed": 37, "total": 37}
        self.service._persist_detector_review_proxy_repair(record)
        detector = _FakeContinuationDetector(repair_id=repair_id)
        annotation = _FakeContinuationAnnotation()
        repair_evidence = {
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
        }
        proxy_media = {
            "sha256": SHA_A,
            "size_bytes": 123,
            "width": 2560,
            "height": 720,
            "frame_count": 12,
            "fps": 20.0,
        }
        samples = {1: b"sample"}
        low = {
            "repair_id": repair_id,
            "attempt_root_repair_id": repair_id,
            "attempt_number": 1,
            "retry_from_repair_id": None,
            "request_sha256": SHA_B,
            "status": "ready",
            "progress": {"completed": 37, "total": 37},
            "report": {
                "repair_execution_binding": {
                    "encoder_preset_sha256": SHA_A,
                    "binding_sha256": SHA_B,
                    "code_bundle_sha256": SHA_C,
                    "runtime_sha256": SHA_A,
                    "decoder_fingerprint_sha256": SHA_B,
                }
            },
        }
        with (
            mock.patch.object(
                self.service,
                "_load_verified_review_proxy_evidence",
                return_value=(repair_evidence, proxy_media, samples),
            ),
            mock.patch.object(
                self.service,
                "_detector_development_service",
                return_value=detector,
            ),
            mock.patch.object(
                self.service,
                "_ball_annotation_service",
                return_value=annotation,
            ),
        ):
            self.service._commit_detector_review_proxy_continuation(repair_id, low)

        path = self.service._detector_review_proxy_jobs_root / f"{repair_id}.json"
        original = path.read_bytes()
        coordinator = mock.Mock()
        coordinator.get_verified_proxy.return_value = low
        cases = {
            "review_proxy_manifest_sha256": SHA_A,
            "mapping_sha256": SHA_B,
            "encoder_binding_sha256": SHA_C,
        }
        for field, forged_value in cases.items():
            with self.subTest(field=field):
                forged = json.loads(original)
                forged["result"]["proxy"][field] = forged_value
                body = deepcopy(forged)
                body.pop("transaction_sha256")
                forged["transaction_sha256"] = canonical_sha256(body)
                path.write_text(json.dumps(forged), encoding="utf-8")
                with (
                    mock.patch.object(
                        self.service,
                        "_detector_review_proxy_coordinator",
                        return_value=coordinator,
                    ),
                    mock.patch.object(
                        self.service,
                        "_load_verified_review_proxy_evidence",
                        return_value=(repair_evidence, proxy_media, samples),
                    ),
                    mock.patch.object(
                        self.service,
                        "_detector_development_service",
                        return_value=detector,
                    ),
                    mock.patch.object(
                        self.service,
                        "_ball_annotation_service",
                        return_value=annotation,
                    ),
                    mock.patch.object(
                        self.service,
                        "_validate_review_proxy_repair_authority",
                    ),
                    mock.patch.object(
                        self.service,
                        "_build_review_proxy_repair_authority",
                        return_value=record["authority"],
                    ),
                ):
                    with self.assertRaisesRegex(
                        DetectorDevelopmentError,
                        "result changed from replayed lower authority",
                    ):
                        self.service.get_detector_review_proxy_repair(repair_id)
                path.write_bytes(original)

        for field in ("resource_sha256", "frozen_profiles_sha256"):
            with self.subTest(child_plan_field=field):
                forged = json.loads(original)
                forged["continuation_intent"]["child_plan"][field] = SHA_B
                plan_body = deepcopy(forged["continuation_intent"]["child_plan"])
                plan_body.pop("plan_sha256")
                forged["continuation_intent"]["child_plan"]["plan_sha256"] = canonical_sha256(plan_body)
                intent_body = deepcopy(forged["continuation_intent"])
                intent_body.pop("intent_sha256")
                forged["continuation_intent"]["intent_sha256"] = canonical_sha256(intent_body)
                body = deepcopy(forged)
                body.pop("transaction_sha256")
                forged["transaction_sha256"] = canonical_sha256(body)
                path.write_text(json.dumps(forged), encoding="utf-8")
                with self.assertRaises(DetectorDevelopmentError):
                    self.service.get_detector_review_proxy_repair(repair_id)
                path.write_bytes(original)

    def test_retry_attempt_lineage_binds_parent_and_single_descendant(self) -> None:
        parent = _record("repair-lineage-root")
        self.service._persist_detector_review_proxy_repair(parent)
        child = _record("repair-lineage-child")
        child.update(
            attempt_root_repair_id=parent["repair_id"],
            attempt_number=2,
            retry_from_repair_id=parent["repair_id"],
            authority=deepcopy(parent["authority"]),
        )
        child["request_sha256"] = self.service._expected_detector_review_proxy_request_sha256(child)
        child["idempotency_key"] = child["request_sha256"]
        self.service._persist_detector_review_proxy_repair(child)
        self.service._verify_review_proxy_attempt_lineage(child)

        for label, mutate in (
            (
                "root",
                lambda value: value.update(attempt_root_repair_id="repair-different-root"),
            ),
            (
                "number",
                lambda value: value.update(attempt_number=3),
            ),
            (
                "authority",
                lambda value: value["authority"].update(source_sha256=SHA_C),
            ),
        ):
            forged = deepcopy(child)
            mutate(forged)
            with (
                self.subTest(label=label),
                self.assertRaisesRegex(
                    DetectorDevelopmentError,
                    "immutable parent lineage",
                ),
            ):
                self.service._verify_review_proxy_attempt_lineage(forged)

        duplicate = deepcopy(child)
        duplicate["repair_id"] = "repair-lineage-duplicate"
        duplicate["request_sha256"] = self.service._expected_detector_review_proxy_request_sha256(duplicate)
        duplicate["idempotency_key"] = duplicate["request_sha256"]
        self.service._persist_detector_review_proxy_repair(duplicate)
        with self.assertRaisesRegex(DetectorDevelopmentError, "immutable parent lineage"):
            self.service._verify_review_proxy_attempt_lineage(child)

    def test_retry_rejects_coherently_downgraded_upper_after_child_side_effect(
        self,
    ) -> None:
        repair_id = "repair-durable-child-floor"
        record = _record(repair_id)
        record.update(
            status="failed",
            stage="continuation_intent",
            continuation_intent=_continuation_intent(repair_id, record["authority"]),
            error_code="review_proxy_failed",
            blocker_code=None,
            recovery_action="retry",
        )
        self.service._persist_detector_review_proxy_repair(record)
        path = self.service._detector_review_proxy_jobs_root / f"{repair_id}.json"
        before = path.read_bytes()
        detector = _FakeContinuationDetector(repair_id=repair_id)
        detector.child_ids.add("probe-child-one")
        low = mock.Mock()
        errors: list[str] = []

        def attempt_retry() -> None:
            try:
                self.service.retry_detector_review_proxy_repair(repair_id)
            except DetectorDevelopmentError as exc:
                errors.append(exc.code)

        with (
            mock.patch.object(
                self.service,
                "_detector_development_service",
                return_value=detector,
            ),
            mock.patch.object(
                self.service,
                "_detector_review_proxy_coordinator",
                return_value=low,
            ),
            mock.patch.object(
                self.service,
                "_inspect_review_proxy_low_job",
                return_value=1,
            ),
        ):
            workers = [threading.Thread(target=attempt_retry) for _ in range(4)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=5.0)
                self.assertFalse(worker.is_alive())
            with self.assertRaises(DetectorDevelopmentError) as inconsistent_get:
                self.service.get_detector_review_proxy_repair(repair_id)

        self.assertEqual(
            ["review_proxy_retry_ineligible"] * 4,
            sorted(errors),
        )
        self.assertEqual(
            "review_proxy_side_effect_floor_mismatch",
            inconsistent_get.exception.code,
        )
        self.assertEqual(409, inconsistent_get.exception.status_code)
        low.retry_repair.assert_not_called()
        self.assertEqual(before, path.read_bytes())

    def test_post_publish_failures_preserve_readable_monotonic_phase(self) -> None:
        child_summary = ApiService._review_proxy_child_summary(_child_job())
        session_summary = ApiService._review_proxy_session_summary(_replacement_session())
        phases = {
            "child_probe_ready": {
                "child_probe": child_summary,
            },
            "replacement_session_ready": {
                "child_probe": child_summary,
                "replacement_session": session_summary,
            },
            "groups_published": {
                "child_probe": child_summary,
                "replacement_session": session_summary,
                "group_publication": {
                    "session_id": "annotation-replacement-one",
                    "blocked_session_id": "annotation-blocked-1",
                    "child_probe_job_id": "probe-child-one",
                    "session_record_sha256": SHA_B,
                    "session_creation_authority_sha256": SHA_A,
                    "group_publication_sha256": SHA_C,
                    "commit_sha256": canonical_sha256(
                        {
                            "session_id": "annotation-replacement-one",
                            "blocked_session_id": "annotation-blocked-1",
                            "child_probe_job_id": "probe-child-one",
                            "session_record_sha256": SHA_B,
                            "session_creation_authority_sha256": SHA_A,
                            "group_publication_sha256": SHA_C,
                        }
                    ),
                },
            },
        }
        for terminal, error in (
            ("failed", RuntimeError("ordinary failure")),
            (
                "blocked",
                DetectorDevelopmentError("continuation_blocked", "blocked", status_code=409),
            ),
        ):
            for phase, bindings in phases.items():
                with self.subTest(terminal=terminal, phase=phase):
                    repair_id = f"repair-{terminal}-{phase}"
                    record = _record(repair_id)
                    record.update(
                        status="committing",
                        stage=phase,
                        continuation_intent=_continuation_intent(repair_id, record["authority"]),
                        **deepcopy(bindings),
                    )
                    self.service._persist_detector_review_proxy_repair(record)
                    with mock.patch.object(
                        self.service,
                        "_advance_detector_review_proxy_repair",
                        side_effect=error,
                    ):
                        self.service._watch_detector_review_proxy_repair(repair_id, f"test-{repair_id}")
                    persisted = self.service._read_detector_review_proxy_repair(repair_id)
                    self.assertEqual(terminal, persisted["status"])
                    self.assertEqual(phase, persisted["stage"])
                    self.assertEqual("resume", persisted["recovery_action"])
                    with mock.patch.object(
                        self.service,
                        "_start_detector_review_proxy_continuation",
                    ) as restart:
                        self.service._recover_detector_review_proxy_repairs()
                    restart.assert_any_call(repair_id)

    def test_six_crash_seams_and_upper_journal_windows_converge_on_one_lineage(
        self,
    ) -> None:
        parent_roots = [
            self.repo_root / "data" / "audited-parent-a",
            self.repo_root / "data" / "audited-parent-b",
        ]
        for index, root in enumerate(parent_roots):
            root.mkdir(parents=True, exist_ok=True)
            (root / "report.json").write_bytes(f"parent-{index}".encode())

        def tree_digest(root: Path) -> str:
            return canonical_sha256(
                {
                    path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted(root.rglob("*"))
                    if path.is_file()
                }
            )

        parent_before = [tree_digest(root) for root in parent_roots]
        scenarios = (
            ("proxy_ready_to_intent", "after_proxy_ready", None),
            ("intent_to_child", "after_continuation_intent", None),
            ("child_publish_to_ready", None, "child_lower"),
            ("child_to_replacement", "after_child_ready", None),
            ("replacement_to_groups", None, "session_lower"),
            ("groups_to_upper_ready", "after_group_publication", None),
            ("child_side_effect_to_upper_journal", None, "child_upper"),
            ("replacement_side_effect_to_upper_journal", None, "replacement_upper"),
            ("group_side_effect_to_upper_journal", None, "group_upper"),
        )
        for label, failpoint_stage, seam in scenarios:
            with self.subTest(label=label):
                repair_id = f"repair-{label}"
                record = _record(repair_id)
                record["authority"].update(
                    {
                        "blocked_session_record_sha256": SHA_B,
                        "parent_probe_job_id": "probe-parent-one",
                        "parent_probe_record_sha256": SHA_A,
                        "frame_indices": [1],
                    }
                )
                record["low_progress"] = {"completed": 37, "total": 37}
                self.service._persist_detector_review_proxy_repair(record)
                detector = _FakeContinuationDetector(crash_after_child_publish=seam == "child_lower")
                annotation = _FakeContinuationAnnotation(crash_after_session_persist=seam == "session_lower")
                repair_evidence = {
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
                }
                proxy_media = {
                    "sha256": SHA_A,
                    "size_bytes": 123,
                    "width": 2560,
                    "height": 720,
                    "frame_count": 12,
                    "fps": 20.0,
                }
                samples = {1: b"sample"}
                repair_binding = {
                    "encoder_preset_sha256": SHA_A,
                    "binding_sha256": SHA_B,
                    "code_bundle_sha256": SHA_C,
                    "runtime_sha256": SHA_A,
                    "decoder_fingerprint_sha256": SHA_B,
                }
                low = {
                    "status": "ready",
                    "progress": {"completed": 37, "total": 37},
                    "report": {"repair_execution_binding": repair_binding},
                }
                fired = False

                def failpoint(stage: str) -> None:
                    nonlocal fired
                    if not fired and stage == failpoint_stage:
                        fired = True
                        raise _SimulatedCrash(stage)

                self.service._detector_review_proxy_failpoint = failpoint
                original_persist = self.service._persist_detector_review_proxy_repair
                journal_crashed = False

                def persist_with_seam(value):
                    nonlocal journal_crashed
                    should_crash = (
                        seam == "child_upper"
                        and value.get("child_probe") is not None
                        or seam == "replacement_upper"
                        and value.get("replacement_session") is not None
                        or seam == "group_upper"
                        and value.get("group_publication") is not None
                    )
                    if should_crash and not journal_crashed:
                        journal_crashed = True
                        raise _SimulatedCrash("side effect before upper journal")
                    return original_persist(value)

                with (
                    mock.patch.object(
                        self.service,
                        "_load_verified_review_proxy_evidence",
                        return_value=(repair_evidence, proxy_media, samples),
                    ),
                    mock.patch.object(
                        self.service,
                        "_detector_development_service",
                        return_value=detector,
                    ),
                    mock.patch.object(
                        self.service,
                        "_ball_annotation_service",
                        return_value=annotation,
                    ),
                    mock.patch.object(
                        self.service,
                        "_persist_detector_review_proxy_repair",
                        side_effect=persist_with_seam,
                    ),
                ):
                    with self.assertRaises(_SimulatedCrash):
                        self.service._commit_detector_review_proxy_continuation(repair_id, low)

                self.service.close()
                with mock.patch.object(ApiService, "_start_detector_review_proxy_continuation"):
                    restarted = ApiService(self.repo_root)
                self.service = restarted
                with (
                    mock.patch.object(
                        restarted,
                        "_load_verified_review_proxy_evidence",
                        return_value=(repair_evidence, proxy_media, samples),
                    ),
                    mock.patch.object(
                        restarted,
                        "_detector_development_service",
                        return_value=detector,
                    ),
                    mock.patch.object(
                        restarted,
                        "_ball_annotation_service",
                        return_value=annotation,
                    ),
                ):
                    restarted._commit_detector_review_proxy_continuation(repair_id, low)
                committed = restarted._read_detector_review_proxy_repair(repair_id)
                self.assertEqual("ready", committed["status"])
                self.assertEqual({"probe-child-one"}, detector.child_ids)
                self.assertEqual({"annotation-replacement-one"}, annotation.session_ids)
                self.assertEqual(
                    {"annotation-replacement-one"},
                    annotation.group_publication_ids,
                )
                self.assertEqual(committed["child_probe"], committed["result"]["child_probe"])
                self.assertEqual(
                    committed["replacement_session"],
                    committed["result"]["replacement_session"],
                )
                self.assertEqual(parent_before, [tree_digest(root) for root in parent_roots])


if __name__ == "__main__":
    unittest.main()
