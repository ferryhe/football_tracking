from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable
from unittest import mock

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

import football_tracking.api.service as service_module
import football_tracking.config_lineage as config_lineage_module
from football_tracking.api.broadcast_api import BroadcastApiError
from football_tracking.api.dependencies import get_service
from football_tracking.api.routes.broadcast import router as broadcast_router
from football_tracking.api.schemas import BroadcastReviewEvidenceImportRequest
from football_tracking.api.service import ApiService
from football_tracking.candidate_annotations import (
    ADJUDICATION_QUEUE_NAME,
    sample_evidence_sha256,
)
from football_tracking.config_lineage import (
    CONFIG_LINEAGE_CONFLICT,
    CONFIG_LINEAGE_MISMATCH,
    CONFIG_LINEAGE_REQUIRED,
    CONFIG_LINEAGE_UNSAFE,
    ConfigLineageError,
)
from football_tracking.review_evidence_bundle import (
    BUNDLE_MANIFEST_NAME,
    ReviewEvidenceBundleError,
    _resolve_declared_manifest_path,
    activate_review_evidence_bundle,
    build_review_evidence_bundle,
    discover_review_evidence_bundles,
    revoke_review_evidence_activation,
    sha256_file,
    validate_review_evidence_bundle,
)
from football_tracking.tracking_contracts import build_tracking_contract


class ReviewEvidenceBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self._strict_patches = [
            mock.patch("football_tracking.review_evidence_bundle.validate_candidate_annotation_package"),
            mock.patch("football_tracking.review_evidence_bundle.validate_candidate_classifier_package"),
            mock.patch("football_tracking.review_evidence_bundle.validate_selective_policy_evidence_binding"),
            mock.patch(
                "football_tracking.review_evidence_bundle.validate_selective_policy_application_binding",
                return_value={
                    "candidate_ids": ["candidate-1"],
                    "candidate_population_sha256": self._fixture_population_sha256(),
                },
            ),
        ]
        for patcher in self._strict_patches:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self._strict_patches):
            patcher.stop()
        self.temp.cleanup()

    def test_policy_authority_resolves_only_unique_model_development_dependency(self) -> None:
        model_sha256 = "1" * 64
        policy_sha256 = "2" * 64
        target_sha256 = "3" * 64
        package_roots = {
            "model_development": PurePosixPath("model-development"),
            "policy_qualification": PurePosixPath("policy-qualification"),
            "target_application": PurePosixPath("target-application"),
        }
        inventory = {
            "model-development/model/model_manifest.v1.json": {"sha256": model_sha256},
            "policy-qualification/policy/selective_policy.v1.json": {"sha256": policy_sha256},
            "target-application/target_labels.v1.json": {"sha256": target_sha256},
        }
        declared = {
            path: package_name
            for path, package_name in (
                (
                    "model-development/model/model_manifest.v1.json",
                    "model_development",
                ),
                (
                    "policy-qualification/policy/selective_policy.v1.json",
                    "policy_qualification",
                ),
                (
                    "target-application/target_labels.v1.json",
                    "target_application",
                ),
            )
        }

        resolved, dependency_package = _resolve_declared_manifest_path(
            "model_manifest.v1.json",
            raw_sha256=model_sha256,
            semantic_key="model_manifest",
            declaring_manifest=PurePosixPath("policy-qualification/policy/selective_policy.v1.json"),
            declaring_package="policy_qualification",
            package_root=package_roots["policy_qualification"],
            package_roots=package_roots,
            inventory=inventory,
            already_declared=declared,
            label="policy authority manifest path",
        )

        self.assertEqual(
            PurePosixPath("model-development/model/model_manifest.v1.json"),
            resolved,
        )
        self.assertEqual("model_development", dependency_package)
        for declaring_package, dependency_sha256, dependency_name in (
            ("model_development", policy_sha256, "selective_policy.v1.json"),
            ("policy_qualification", target_sha256, "target_labels.v1.json"),
        ):
            with (
                self.subTest(
                    declaring_package=declaring_package,
                    dependency_name=dependency_name,
                ),
                self.assertRaises(ReviewEvidenceBundleError) as caught,
            ):
                _resolve_declared_manifest_path(
                    dependency_name,
                    raw_sha256=dependency_sha256,
                    semantic_key="dependency",
                    declaring_manifest=package_roots[declaring_package] / "manifest.json",
                    declaring_package=declaring_package,
                    package_root=package_roots[declaring_package],
                    package_roots=package_roots,
                    inventory=inventory,
                    already_declared=declared,
                    label=f"{declaring_package} authority manifest path",
                )
            self.assertEqual("undeclared_non_target_artifact", caught.exception.code)

        annotation_contract_sha256 = "4" * 64
        annotation_derived_sha256 = "5" * 64
        annotation_direct = "model-development/annotations/tracking_contract.v2.json"
        annotation_authoritative = "model-development/dataset/tracking_contract.v2.json"
        inventory.update(
            {
                annotation_direct: {"sha256": annotation_derived_sha256},
                annotation_authoritative: {"sha256": annotation_contract_sha256},
            }
        )
        declared[annotation_direct] = "model_development"
        declared[annotation_authoritative] = "model_development"
        with self.assertRaises(ReviewEvidenceBundleError) as strict_caught:
            _resolve_declared_manifest_path(
                "tracking_contract.v2.json",
                raw_sha256=annotation_contract_sha256,
                semantic_key="source_contract",
                declaring_manifest=PurePosixPath("model-development/annotations/annotation_resolution.v1.json"),
                declaring_package="model_development",
                package_root=package_roots["model_development"],
                package_roots=package_roots,
                inventory=inventory,
                already_declared=declared,
                label="strict authority manifest path",
            )
        self.assertEqual(
            "undeclared_non_target_artifact",
            strict_caught.exception.code,
        )

        relocated, relocated_package = _resolve_declared_manifest_path(
            "tracking_contract.v2.json",
            raw_sha256=annotation_contract_sha256,
            semantic_key="source_contract",
            declaring_manifest=PurePosixPath("model-development/annotations/annotation_resolution.v1.json"),
            declaring_package="model_development",
            package_root=package_roots["model_development"],
            package_roots=package_roots,
            inventory=inventory,
            already_declared=declared,
            label="annotation source contract path",
            allow_declared_relocation_on_mismatch=True,
        )
        self.assertEqual(PurePosixPath(annotation_authoritative), relocated)
        self.assertEqual("model_development", relocated_package)

    def test_config_lineage_route_returns_stable_typed_blocker(self) -> None:
        class BlockedService:
            def __init__(self, code: str) -> None:
                self.code = code

            def reconfirm_broadcast_config_lineage(
                self,
                run_id: str,
                request: dict[str, object],
            ) -> dict[str, object]:
                del run_id, request
                raise ConfigLineageError(
                    self.code,
                    f"config lineage blocked: {self.code}",
                )

        app = FastAPI()
        app.include_router(broadcast_router)
        client = TestClient(app)
        payload = {
            "target_run_id": "run-1",
            "confirmed_config_name": "fixture.yaml",
            "confirmed_text_sha256": "b" * 64,
            "expected_observed_raw_sha256": "1" * 64,
            "workflow_bindings": {},
            "operator_id": "operator-1",
            "reviewer_id": "reviewer-1",
        }
        for code in (
            CONFIG_LINEAGE_REQUIRED,
            CONFIG_LINEAGE_UNSAFE,
            CONFIG_LINEAGE_MISMATCH,
            CONFIG_LINEAGE_CONFLICT,
        ):
            with self.subTest(code=code):
                app.dependency_overrides[get_service] = lambda code=code: BlockedService(code)
                response = client.post(
                    "/runs/run-1/broadcast/config-lineage-reconfirmation",
                    json=payload,
                )
                self.assertEqual(409, response.status_code)
                self.assertEqual(
                    {
                        "status": "blocked",
                        "blocker_code": code,
                        "detail": f"config lineage blocked: {code}",
                        "retryable": False,
                    },
                    response.json(),
                )

    def test_config_lineage_route_requires_complete_independent_identities(self) -> None:
        class UnexpectedService:
            def reconfirm_broadcast_config_lineage(
                self,
                run_id: str,
                request: dict[str, object],
            ) -> dict[str, object]:
                raise AssertionError(f"unexpected service call for {run_id}: {request}")

        app = FastAPI()
        app.include_router(broadcast_router)
        app.dependency_overrides[get_service] = UnexpectedService
        client = TestClient(app)
        payload = {
            "target_run_id": "run-1",
            "confirmed_config_name": "fixture.yaml",
            "confirmed_text_sha256": "b" * 64,
            "expected_observed_raw_sha256": "1" * 64,
            "workflow_bindings": {},
            "operator_id": "operator-1",
        }

        missing = client.post(
            "/runs/run-1/broadcast/config-lineage-reconfirmation",
            json=payload,
        )
        same_identity = client.post(
            "/runs/run-1/broadcast/config-lineage-reconfirmation",
            json={**payload, "reviewer_id": "operator-1"},
        )
        whitespace = client.post(
            "/runs/run-1/broadcast/config-lineage-reconfirmation",
            json={**payload, "reviewer_id": " reviewer-1 "},
        )

        self.assertEqual(422, missing.status_code)
        self.assertEqual(422, same_identity.status_code)
        self.assertEqual(422, whitespace.status_code)

    def test_config_lineage_service_classifies_authority_and_registry_failures(self) -> None:
        repo = self.root / "lineage-service-repo"
        for name in ("config", "data", "outputs", "weights"):
            (repo / name).mkdir(parents=True)
        service = ApiService(repo)
        output_dir = repo / "outputs" / "run-fixture"
        output_dir.mkdir()
        config_path = repo / "config" / "fixture.yaml"
        config_path.write_text("input_video: fixture.mp4\n", encoding="utf-8")
        registry = service._read_registry()
        registry["runs"].append(
            {
                "run_id": "run-fixture",
                "source": "broadcast_hybrid",
                "status": "completed",
                "config_name": "fixture.yaml",
                "config_path": str(config_path),
                "input_video": str(repo / "data" / "fixture.mp4"),
                "parent_run_id": None,
                "output_dir": str(output_dir),
                "notes": json.dumps(
                    {
                        "confirmed_config_name": "fixture.yaml",
                        "expected_config_sha256": "b" * 64,
                    }
                ),
                "broadcast": {},
            }
        )
        service._write_registry(registry)
        request = {
            "target_run_id": "run-fixture",
            "confirmed_config_name": "fixture.yaml",
            "confirmed_text_sha256": "b" * 64,
            "expected_observed_raw_sha256": "1" * 64,
            "workflow_bindings": {},
            "operator_id": "operator-1",
            "reviewer_id": "reviewer-1",
        }
        try:
            registry = service._read_registry()
            parent = next(item for item in registry["runs"] if item["run_id"] == "run-fixture")
            parent["notes"] = None
            service._write_registry(registry)
            with self.assertRaises(ConfigLineageError) as missing:
                service.reconfirm_broadcast_config_lineage("run-fixture", request)
            self.assertEqual(CONFIG_LINEAGE_REQUIRED, missing.exception.code)

            registry = service._read_registry()
            parent = next(item for item in registry["runs"] if item["run_id"] == "run-fixture")
            parent["notes"] = "{"
            service._write_registry(registry)
            with self.assertRaises(ConfigLineageError) as malformed:
                service.reconfirm_broadcast_config_lineage("run-fixture", request)
            self.assertEqual(CONFIG_LINEAGE_MISMATCH, malformed.exception.code)

            registry = service._read_registry()
            parent = next(item for item in registry["runs"] if item["run_id"] == "run-fixture")
            parent["notes"] = json.dumps(
                {
                    "confirmed_config_name": "fixture.yaml",
                    "expected_config_sha256": "b" * 64,
                }
            )
            parent["config_path"] = None
            service._write_registry(registry)
            with self.assertRaises(ConfigLineageError) as missing_config:
                service.reconfirm_broadcast_config_lineage("run-fixture", request)
            self.assertEqual(CONFIG_LINEAGE_REQUIRED, missing_config.exception.code)

            registry = service._read_registry()
            parent = next(item for item in registry["runs"] if item["run_id"] == "run-fixture")
            parent["config_path"] = []
            service._write_registry(registry)
            with self.assertRaises(ConfigLineageError) as malformed_config:
                service.reconfirm_broadcast_config_lineage("run-fixture", request)
            self.assertEqual(CONFIG_LINEAGE_MISMATCH, malformed_config.exception.code)

            registry = service._read_registry()
            parent = next(item for item in registry["runs"] if item["run_id"] == "run-fixture")
            parent["config_path"] = str(config_path)
            service._write_registry(registry)
            with mock.patch.object(
                service,
                "_derive_config_lineage_workflow_bindings",
                side_effect=ValueError("registry binding mismatch"),
            ):
                with self.assertRaises(ConfigLineageError) as mismatch:
                    service.reconfirm_broadcast_config_lineage("run-fixture", request)
            self.assertEqual(CONFIG_LINEAGE_MISMATCH, mismatch.exception.code)

            current_authority = {
                "target_run_id": "run-fixture",
                "confirmed_config_name": "renamed-fixture.yaml",
                "confirmed_text_sha256": "c" * 64,
                "workflow_bindings": {},
            }
            current_request = {
                **request,
                **current_authority,
            }
            stale_echoes = {
                "target_run_id": "stale-run",
                "confirmed_config_name": "fixture.yaml",
                "confirmed_text_sha256": "b" * 64,
            }
            publication = mock.Mock()
            with (
                mock.patch.object(
                    service,
                    "_broadcast_config_lineage_reconfirmation_authority",
                    return_value=current_authority,
                ),
                mock.patch(
                    "football_tracking.api.service.reconfirm_config_lineage",
                    side_effect=publication,
                ),
            ):
                for field, stale_value in stale_echoes.items():
                    with (
                        self.subTest(stale_challenge_field=field),
                        self.assertRaisesRegex(
                            ConfigLineageError,
                            "challenge does not match current server-derived authority",
                        ),
                    ):
                        service.reconfirm_broadcast_config_lineage(
                            "run-fixture",
                            {**current_request, field: stale_value},
                        )
            publication.assert_not_called()

            with (
                mock.patch.object(
                    service,
                    "_derive_config_lineage_workflow_bindings",
                    return_value={},
                ),
                mock.patch(
                    "football_tracking.api.service.reconfirm_config_lineage",
                    side_effect=ConfigLineageError(CONFIG_LINEAGE_UNSAFE, "unsafe config path"),
                ),
            ):
                with self.assertRaises(ConfigLineageError) as unsafe:
                    service.reconfirm_broadcast_config_lineage("run-fixture", request)
            self.assertEqual(CONFIG_LINEAGE_UNSAFE, unsafe.exception.code)

            with (
                mock.patch.object(
                    service,
                    "_derive_config_lineage_workflow_bindings",
                    return_value={},
                ),
                mock.patch(
                    "football_tracking.api.service.reconfirm_config_lineage",
                    side_effect=ConfigLineageError(CONFIG_LINEAGE_CONFLICT, "different generation exists"),
                ),
            ):
                with self.assertRaises(ConfigLineageError) as conflict:
                    service.reconfirm_broadcast_config_lineage("run-fixture", request)
            self.assertEqual(CONFIG_LINEAGE_CONFLICT, conflict.exception.code)

            circular_bindings = {name: {} for name in config_lineage_module._REQUIRED_WORKFLOW_BINDINGS}
            circular_bindings["workflow_id"] = "workflow-1"
            circular_bindings["historical_full_runs"] = []
            circular_bindings["request"] = circular_bindings
            serialization_request = {
                **request,
                "workflow_bindings": circular_bindings,
            }

            def validate_workflow_bindings(**kwargs: object) -> None:
                config_lineage_module._validated_workflow_bindings(
                    kwargs["workflow_bindings"],
                )

            with (
                mock.patch.object(
                    service,
                    "_derive_config_lineage_workflow_bindings",
                    return_value=circular_bindings,
                ),
                mock.patch(
                    "football_tracking.api.service.reconfirm_config_lineage",
                    side_effect=validate_workflow_bindings,
                ),
            ):
                with self.assertRaises(ConfigLineageError) as serialization:
                    service.reconfirm_broadcast_config_lineage(
                        "run-fixture",
                        serialization_request,
                    )
            self.assertEqual(CONFIG_LINEAGE_MISMATCH, serialization.exception.code)
            self.assertEqual(
                "config lineage snapshot mismatch: workflow bindings must be JSON-serializable",
                str(serialization.exception),
            )

            registry = service._read_registry()
            registry["runs"].append(
                {
                    "run_id": "config-lineage-different",
                    "source": "config_lineage_reconfirmation",
                    "status": "completed",
                    "parent_run_id": "run-fixture",
                    "broadcast": {
                        "generation_id": "lineage-differentdifferentdiffe",
                        "manifest_sha256": "f" * 64,
                        "workflow_bindings": {},
                    },
                }
            )
            service._write_registry(registry)
            fake_generation = mock.Mock(
                generation_id="lineage-aaaaaaaaaaaaaaaaaaaaaaaa",
                generation_dir=repo / "outputs" / "generation",
                manifest={
                    "projection": {
                        "confirmed_text_sha256": "b" * 64,
                        "observed_raw_sha256": "1" * 64,
                        "canonical_snapshot_sha256": "c" * 64,
                        "lineage_generation_id": "lineage-aaaaaaaaaaaaaaaaaaaaaaaa",
                        "historical_raw_snapshot_observed": False,
                    }
                },
                idempotent=True,
                manifest_sha256="d" * 64,
                canonical_snapshot_sha256="c" * 64,
            )
            with (
                mock.patch.object(
                    service,
                    "_derive_config_lineage_workflow_bindings",
                    return_value={},
                ),
                mock.patch(
                    "football_tracking.api.service.reconfirm_config_lineage",
                    return_value=fake_generation,
                ),
            ):
                with self.assertRaises(ConfigLineageError) as existing_conflict:
                    service.reconfirm_broadcast_config_lineage("run-fixture", request)
            self.assertEqual(CONFIG_LINEAGE_CONFLICT, existing_conflict.exception.code)
        finally:
            service.close()

    def test_config_lineage_reconfirmation_serializes_authority_publication_and_registration(self) -> None:
        repo = self.root / "lineage-transaction-repo"
        for name in ("config", "data", "outputs", "weights"):
            (repo / name).mkdir(parents=True)
        first = ApiService(repo)
        second = ApiService(repo)
        output_dir = repo / "outputs" / "run-fixture"
        output_dir.mkdir()
        config_path = repo / "config" / "fixture.yaml"
        config_path.write_text("input_video: fixture.mp4\n", encoding="utf-8")
        confirmation = {
            "confirmed_config_name": "fixture.yaml",
            "expected_config_sha256": "b" * 64,
        }
        registry = first._read_registry()
        registry["runs"].append(
            {
                "run_id": "run-fixture",
                "source": "broadcast_hybrid",
                "status": "completed",
                "config_name": "fixture.yaml",
                "config_path": str(config_path),
                "input_video": str(repo / "data" / "fixture.mp4"),
                "parent_run_id": None,
                "output_dir": str(output_dir),
                "notes": json.dumps(confirmation),
                "broadcast": {},
            }
        )
        first._write_registry(registry)
        request = {
            "target_run_id": "run-fixture",
            "confirmed_config_name": "fixture.yaml",
            "confirmed_text_sha256": "b" * 64,
            "expected_observed_raw_sha256": "1" * 64,
            "workflow_bindings": {},
            "operator_id": "operator-1",
            "reviewer_id": "reviewer-1",
        }
        fake_generation = mock.Mock(
            generation_id="lineage-aaaaaaaaaaaaaaaaaaaaaaaa",
            generation_dir=repo / "outputs" / "generation",
            manifest={
                "projection": {
                    "confirmed_text_sha256": "b" * 64,
                    "observed_raw_sha256": "1" * 64,
                    "canonical_snapshot_sha256": "c" * 64,
                    "lineage_generation_id": "lineage-aaaaaaaaaaaaaaaaaaaaaaaa",
                    "historical_raw_snapshot_observed": False,
                }
            },
            idempotent=False,
            manifest_sha256="d" * 64,
            canonical_snapshot_sha256="c" * 64,
        )
        publication_started = threading.Event()
        allow_publication = threading.Event()
        update_started = threading.Event()
        update_finished = threading.Event()
        notes_mutated = threading.Event()
        reconfirmation_result: list[dict[str, object]] = []
        reconfirmation_errors: list[BaseException] = []

        def pause_at_old_race_boundary(**_kwargs: object) -> mock.Mock:
            publication_started.set()
            if not allow_publication.wait(timeout=5):
                raise TimeoutError("test did not release config-lineage publication")
            return fake_generation

        def reconfirm() -> None:
            try:
                reconfirmation_result.append(first.reconfirm_broadcast_config_lineage("run-fixture", request))
            except BaseException as exc:  # pragma: no cover - reported by the assertions below
                reconfirmation_errors.append(exc)

        def update_notes_if_unconfirmed() -> None:
            update_started.set()
            try:
                with second._registry_transaction() as current:
                    child_exists = any(
                        item.get("source") == "config_lineage_reconfirmation"
                        and item.get("parent_run_id") == "run-fixture"
                        for item in current["runs"]
                    )
                    if child_exists:
                        return
                    parent = next(item for item in current["runs"] if item.get("run_id") == "run-fixture")
                    changed = dict(confirmation)
                    changed["expected_config_sha256"] = "e" * 64
                    parent["notes"] = json.dumps(changed)
                    notes_mutated.set()
            finally:
                update_finished.set()

        try:
            with (
                mock.patch.object(first, "_derive_config_lineage_workflow_bindings", return_value={}),
                mock.patch.object(second, "_derive_config_lineage_workflow_bindings", return_value={}),
                mock.patch(
                    "football_tracking.api.service.reconfirm_config_lineage",
                    side_effect=pause_at_old_race_boundary,
                ),
            ):
                reconfirm_thread = threading.Thread(target=reconfirm)
                reconfirm_thread.start()
                self.assertTrue(publication_started.wait(timeout=5))
                update_thread = threading.Thread(target=update_notes_if_unconfirmed)
                update_thread.start()
                self.assertTrue(update_started.wait(timeout=5))
                update_completed_at_boundary = update_finished.wait(timeout=1)
                allow_publication.set()
                reconfirm_thread.join(timeout=5)
                update_thread.join(timeout=5)

                self.assertFalse(reconfirm_thread.is_alive())
                self.assertFalse(update_thread.is_alive())
                self.assertFalse(update_completed_at_boundary)
                self.assertEqual([], reconfirmation_errors)
                self.assertEqual("reconfirmed", reconfirmation_result[0]["status"])
                self.assertFalse(notes_mutated.is_set())
                self.assertTrue(update_finished.is_set())

                replay = second.reconfirm_broadcast_config_lineage("run-fixture", request)
                self.assertEqual(reconfirmation_result[0], replay)
                children = [
                    item
                    for item in second._read_registry()["runs"]
                    if item.get("source") == "config_lineage_reconfirmation"
                    and item.get("parent_run_id") == "run-fixture"
                ]
                self.assertEqual(1, len(children))
        finally:
            allow_publication.set()
            first.close()
            second.close()

    def test_builder_publishes_self_contained_bundle_and_validator_rehashes_it(self) -> None:
        self._write_fixture()

        built = build_review_evidence_bundle(self.source, self.root / "published")
        validated = validate_review_evidence_bundle(built.root)

        self.assertEqual("review-evidence-fixture", validated.manifest["bundle_id"])
        self.assertEqual(1, validated.manifest["target"]["max_review_windows"])
        self.assertEqual(1, json.loads(validated.queue_path.read_text(encoding="utf-8"))["candidate_count"])
        inventory_paths = {row["path"] for row in validated.manifest["inventory"]}
        self.assertNotIn("review_evidence_bundle.draft.json", inventory_paths)
        self.assertNotIn(BUNDLE_MANIFEST_NAME, inventory_paths)

    def test_builder_and_validator_reject_unknown_package_descriptor_artifact(
        self,
    ) -> None:
        draft = self._write_fixture()
        clean = build_review_evidence_bundle(self.source, self.root / "clean-published")
        smuggled_relative = "policy-qualification/smuggled-target-labels.bin"
        smuggled_payload = b"candidate-1\x00match_ball\x00secret-target-truth"
        smuggled_source = self.source / smuggled_relative
        smuggled_source.write_bytes(smuggled_payload)
        policy_descriptor = draft["packages"]["policy_qualification"]
        policy_descriptor["smuggled_target_labels_path"] = smuggled_relative
        policy_descriptor["smuggled_target_labels_sha256"] = hashlib.sha256(smuggled_payload).hexdigest()
        self._write_json(self.source / "review_evidence_bundle.draft.json", draft)

        with self.assertRaises(ReviewEvidenceBundleError) as build_caught:
            build_review_evidence_bundle(self.source, self.root / "smuggled-build")
        self.assertEqual("invalid_package_descriptor", build_caught.exception.code)

        smuggled_published = clean.root / smuggled_relative
        smuggled_published.write_bytes(smuggled_payload)
        manifest = json.loads(clean.manifest_path.read_text(encoding="utf-8"))
        manifest["packages"]["policy_qualification"]["smuggled_target_labels_path"] = smuggled_relative
        manifest["packages"]["policy_qualification"]["smuggled_target_labels_sha256"] = hashlib.sha256(
            smuggled_payload
        ).hexdigest()
        manifest["inventory"].append(
            {
                "path": smuggled_relative,
                "sha256": hashlib.sha256(smuggled_payload).hexdigest(),
                "size_bytes": len(smuggled_payload),
            }
        )
        manifest["inventory"].sort(key=lambda row: row["path"])
        self._write_json(clean.manifest_path, manifest)

        with self.assertRaises(ReviewEvidenceBundleError) as validate_caught:
            validate_review_evidence_bundle(clean.root)
        self.assertEqual("invalid_package_descriptor", validate_caught.exception.code)

    def test_builder_rejects_removed_adjudication_alias_half_pairs(self) -> None:
        for missing_field in (
            "adjudication_queue_path",
            "adjudication_queue_sha256",
        ):
            with self.subTest(missing_field=missing_field):
                draft = self._write_fixture()
                queue_relative = "model-development/annotation_adjudication_queue.v1.json"
                queue_path = self.source / queue_relative
                self._write_json(
                    queue_path,
                    {
                        "schema_version": "1.0",
                        "artifact_type": "candidate_annotation_adjudication_queue",
                    },
                )
                pair = {
                    "adjudication_queue_path": queue_relative,
                    "adjudication_queue_sha256": sha256_file(queue_path),
                }
                del pair[missing_field]
                draft["packages"]["model_development"].update(pair)
                self._write_json(
                    self.source / "review_evidence_bundle.draft.json",
                    draft,
                )

                with self.assertRaises(ReviewEvidenceBundleError) as caught:
                    build_review_evidence_bundle(
                        self.source,
                        self.root / f"half-pair-{missing_field}",
                    )
                self.assertEqual("invalid_package_descriptor", caught.exception.code)

    def test_removed_adjudication_alias_rejects_binary_smuggling_on_build_and_validation(
        self,
    ) -> None:
        for package_name, package_root in (
            ("model_development", "model-development"),
            ("policy_qualification", "policy-qualification"),
        ):
            with self.subTest(package_name=package_name):
                draft = self._write_fixture()
                clean = build_review_evidence_bundle(
                    self.source,
                    self.root / f"clean-alias-{package_name}",
                )
                queue_relative = f"{package_root}/annotation_adjudication_queue.v1.json"
                binary_truth = b"candidate-1\x00match_ball\x00target-truth"
                queue_path = self.source / queue_relative
                queue_path.write_bytes(binary_truth)
                draft["packages"][package_name].update(
                    {
                        "adjudication_queue_path": queue_relative,
                        "adjudication_queue_sha256": hashlib.sha256(binary_truth).hexdigest(),
                    }
                )
                self._write_json(
                    self.source / "review_evidence_bundle.draft.json",
                    draft,
                )
                try:
                    with self.assertRaises(ReviewEvidenceBundleError) as build_caught:
                        build_review_evidence_bundle(
                            self.source,
                            self.root / f"alias-smuggle-{package_name}",
                        )
                    self.assertEqual(
                        "invalid_package_descriptor",
                        build_caught.exception.code,
                    )
                finally:
                    queue_path.unlink()

                published_queue = clean.root / queue_relative
                published_queue.write_bytes(binary_truth)
                manifest = json.loads(clean.manifest_path.read_text(encoding="utf-8"))
                manifest["packages"][package_name].update(
                    {
                        "adjudication_queue_path": queue_relative,
                        "adjudication_queue_sha256": hashlib.sha256(binary_truth).hexdigest(),
                    }
                )
                manifest["inventory"].append(
                    {
                        "path": queue_relative,
                        "sha256": hashlib.sha256(binary_truth).hexdigest(),
                        "size_bytes": len(binary_truth),
                    }
                )
                manifest["inventory"].sort(key=lambda row: row["path"])
                self._write_json(clean.manifest_path, manifest)
                with self.assertRaises(ReviewEvidenceBundleError) as validate_caught:
                    validate_review_evidence_bundle(clean.root)
                self.assertEqual(
                    "invalid_package_descriptor",
                    validate_caught.exception.code,
                )

    def test_linked_adjudication_queue_requires_fixed_name_for_both_packages(
        self,
    ) -> None:
        for package_name in ("model_development", "policy_qualification"):
            with self.subTest(package_name=package_name, mode="build"):
                draft = self._write_fixture()
                queue_path = self._configure_linked_adjudication_queue(
                    draft,
                    package_name,
                    linked_name="renamed-adjudication-queue.json",
                )
                try:
                    with self.assertRaises(ReviewEvidenceBundleError) as caught:
                        build_review_evidence_bundle(
                            self.source,
                            self.root / f"wrong-queue-name-{package_name}",
                        )
                    self.assertEqual(
                        "invalid_authority_manifest",
                        caught.exception.code,
                    )
                finally:
                    queue_path.unlink()

            with self.subTest(package_name=package_name, mode="published"):
                draft = self._write_fixture()
                queue_path = self._configure_linked_adjudication_queue(
                    draft,
                    package_name,
                )
                try:
                    built = build_review_evidence_bundle(
                        self.source,
                        self.root / f"published-wrong-queue-name-{package_name}",
                    )
                finally:
                    queue_path.unlink()
                manifest = json.loads(built.manifest_path.read_text(encoding="utf-8"))
                descriptor = manifest["packages"][package_name]
                annotation_relative = descriptor["annotation_resolution_path"]
                annotation_path = built.root / annotation_relative
                annotation = json.loads(annotation_path.read_text(encoding="utf-8"))
                annotation["linked_artifacts"]["adjudication_queue"] = "renamed-adjudication-queue.json"
                self._write_json(annotation_path, annotation)
                self._refresh_published_file_binding(
                    manifest,
                    built.root,
                    annotation_relative,
                    descriptor=descriptor,
                    sha_field="annotation_resolution_sha256",
                )
                if package_name == "policy_qualification":
                    self._refresh_published_annotation_queue_binding(
                        manifest,
                        built.root,
                        annotation_relative,
                    )
                self._write_json(built.manifest_path, manifest)

                with self.assertRaises(ReviewEvidenceBundleError) as caught:
                    validate_review_evidence_bundle(built.root)
                self.assertEqual(
                    "invalid_authority_manifest",
                    caught.exception.code,
                )

    def test_linked_adjudication_queue_semantics_are_exact_for_both_packages(
        self,
    ) -> None:
        mutations = {
            "schema_version": {"schema_version": "2.0"},
            "artifact_type": {
                "artifact_type": "candidate_annotation_resolution",
            },
            "source_resolution": {
                "source_resolution": "other-resolution.json",
            },
            "candidate_count": {"candidate_count": 2},
            "candidates": {
                "candidates": [{"candidate_id": "different-candidate"}],
            },
            "extra_field": {"target_truth": "smuggled"},
        }
        for package_name in ("model_development", "policy_qualification"):
            for case_name, overrides in mutations.items():
                with self.subTest(
                    package_name=package_name,
                    case=case_name,
                    mode="build",
                ):
                    draft = self._write_fixture()
                    queue_path = self._configure_linked_adjudication_queue(
                        draft,
                        package_name,
                        queue_overrides=overrides,
                    )
                    try:
                        with self.assertRaises(ReviewEvidenceBundleError) as caught:
                            build_review_evidence_bundle(
                                self.source,
                                self.root / (f"invalid-queue-build-{package_name}-{case_name}"),
                            )
                        self.assertEqual(
                            "invalid_authority_manifest",
                            caught.exception.code,
                        )
                    finally:
                        queue_path.unlink()

                with self.subTest(
                    package_name=package_name,
                    case=case_name,
                    mode="published",
                ):
                    draft = self._write_fixture()
                    queue_path = self._configure_linked_adjudication_queue(
                        draft,
                        package_name,
                    )
                    try:
                        built = build_review_evidence_bundle(
                            self.source,
                            self.root / (f"invalid-queue-published-{package_name}-{case_name}"),
                        )
                    finally:
                        queue_path.unlink()
                    manifest = json.loads(built.manifest_path.read_text(encoding="utf-8"))
                    descriptor = manifest["packages"][package_name]
                    queue_relative = (
                        PurePosixPath(descriptor["annotation_resolution_path"])
                        .parent.joinpath(ADJUDICATION_QUEUE_NAME)
                        .as_posix()
                    )
                    published_queue = built.root / queue_relative
                    queue = json.loads(published_queue.read_text(encoding="utf-8"))
                    queue.update(overrides)
                    self._write_json(published_queue, queue)
                    self._refresh_published_file_binding(
                        manifest,
                        built.root,
                        queue_relative,
                    )
                    self._write_json(built.manifest_path, manifest)

                    with self.assertRaises(ReviewEvidenceBundleError) as caught:
                        validate_review_evidence_bundle(built.root)
                    self.assertEqual(
                        "invalid_authority_manifest",
                        caught.exception.code,
                    )

    def test_exact_linked_adjudication_queue_is_derived_for_both_packages(
        self,
    ) -> None:
        for package_name in ("model_development", "policy_qualification"):
            with self.subTest(package_name=package_name):
                draft = self._write_fixture()
                queue_path = self._configure_linked_adjudication_queue(
                    draft,
                    package_name,
                )
                try:
                    built = build_review_evidence_bundle(
                        self.source,
                        self.root / f"exact-queue-{package_name}",
                    )
                finally:
                    queue_path.unlink()
                validated = validate_review_evidence_bundle(built.root)
                descriptor = validated.manifest["packages"][package_name]
                expected_relative = (
                    PurePosixPath(descriptor["annotation_resolution_path"])
                    .parent.joinpath(ADJUDICATION_QUEUE_NAME)
                    .as_posix()
                )
                self.assertIn(
                    expected_relative,
                    {row["path"] for row in validated.manifest["inventory"]},
                )

    def test_unlinked_adjudication_queue_is_rejected_for_both_packages(
        self,
    ) -> None:
        for package_name in ("model_development", "policy_qualification"):
            with self.subTest(package_name=package_name):
                draft = self._write_fixture()
                descriptor = draft["packages"][package_name]
                annotation_relative = PurePosixPath(descriptor["annotation_resolution_path"])
                queue_path = self.source / annotation_relative.parent / ADJUDICATION_QUEUE_NAME
                self._write_json(
                    queue_path,
                    {
                        "schema_version": "1.0",
                        "artifact_type": ("candidate_annotation_adjudication_queue"),
                        "source_resolution": annotation_relative.name,
                        "candidate_count": 0,
                        "candidates": [],
                    },
                )
                try:
                    with self.assertRaises(ReviewEvidenceBundleError) as caught:
                        build_review_evidence_bundle(
                            self.source,
                            self.root / f"unlinked-queue-{package_name}",
                        )
                    self.assertEqual(
                        "invalid_authority_manifest",
                        caught.exception.code,
                    )
                finally:
                    queue_path.unlink()

    def test_previous_vote_ledger_remains_optional_for_non_target_packages(
        self,
    ) -> None:
        draft = self._write_fixture()
        for package_name in ("model_development", "policy_qualification"):
            descriptor = draft["packages"][package_name]
            descriptor["previous_vote_ledger_path"] = descriptor["vote_ledger_path"]
            descriptor["previous_vote_ledger_sha256"] = descriptor["vote_ledger_sha256"]
        self._write_json(
            self.source / "review_evidence_bundle.draft.json",
            draft,
        )

        built = build_review_evidence_bundle(
            self.source,
            self.root / "previous-ledger-compatible",
        )
        validate_review_evidence_bundle(built.root)

    def test_target_finite_population_bundle_uses_new_legacy_rejecting_envelope(self) -> None:
        from football_tracking import target_finite_population as target_module

        draft = self._write_fixture()
        target = draft["target"]
        exact_target_bindings = {
            "target_run_id": target["run_id"],
            "source_sha256": target["source_sha256"],
            "root_contract_sha256": target["root_contract_sha256"],
            "candidate_population_sha256": target["candidate_population_sha256"],
            "model_sha256": "7" * 64,
            "model_version": "model-v1",
            "confirmed_config_sha256": target["confirmed_config_sha256"],
            "policy_sha256": "8" * 64,
            "policy_version": "policy-v1",
            "thresholds_sha256": "9" * 64,
        }
        plan_sha256 = "a" * 64
        commitment_sha256 = "b" * 64
        design_sha256 = "c" * 64
        sample_sha256 = "d" * 64
        application_content_sha256 = "e" * 64
        qualification_sha256 = "f" * 64
        external_commitment_sha256 = "0" * 64
        for package_name, replacement_id, contract_names in (
            (
                "model_development",
                "development-only-candidate",
                ("source_contract", "resolved_contract"),
            ),
            (
                "policy_qualification",
                "qualification-only-candidate",
                ("source_contract",),
            ),
        ):
            descriptor = draft["packages"][package_name]
            for contract_name in contract_names:
                path = self.source / descriptor[f"{contract_name}_path"]
                contract = json.loads(path.read_text(encoding="utf-8"))
                contract["candidates"][0]["candidate_id"] = replacement_id
                self._write_json(path, contract)
                descriptor[f"{contract_name}_sha256"] = sha256_file(path)
        target_artifacts = {}
        for name, artifact_type in (
            ("target_audit_plan", "target_finite_population_audit_plan"),
            ("target_audit_labels", "target_finite_population_audit_labels"),
            ("target_qualification", "target_finite_population_qualification"),
            ("target_frozen_application", "target_finite_population_application"),
            ("target_prelabel_commitment", "target_finite_population_prelabel_commitment"),
        ):
            relative = f"target-application/{name}.json"
            path = self.source / relative
            payload = {
                "schema_version": "1.0",
                "artifact_type": artifact_type,
                "qualification_scope": "target_finite_population",
            }
            if name == "target_audit_plan":
                payload.update(
                    {
                        "bindings": exact_target_bindings,
                        "plan_sha256": plan_sha256,
                        "plan_commitment_sha256": commitment_sha256,
                        "sampling_design_sha256": design_sha256,
                        "sample_sha256": sample_sha256,
                        "frozen_application_content_sha256": application_content_sha256,
                        "external_commitment": {
                            "record_sha256": external_commitment_sha256,
                        },
                    }
                )
            elif name == "target_audit_labels":
                payload.update(
                    {
                        "plan_sha256": plan_sha256,
                        "plan_commitment_sha256": commitment_sha256,
                        "sampling_design_sha256": design_sha256,
                        "sample_sha256": sample_sha256,
                        "external_commitment_sha256": external_commitment_sha256,
                    }
                )
            elif name == "target_qualification":
                payload.update(
                    {
                        "bindings": exact_target_bindings,
                        "plan_sha256": plan_sha256,
                        "qualification_sha256": qualification_sha256,
                        "external_commitment_sha256": external_commitment_sha256,
                    }
                )
            elif name == "target_prelabel_commitment":
                payload.update(
                    {
                        "plan_sha256": plan_sha256,
                        "plan_commitment_sha256": commitment_sha256,
                        "sample_sha256": sample_sha256,
                    }
                )
            else:
                payload.update(
                    {
                        "target_binding_evidence": {
                            key: value
                            for key, value in exact_target_bindings.items()
                            if key not in {"target_run_id", "confirmed_config_sha256"}
                        },
                        "application_content_sha256": application_content_sha256,
                    }
                )
            self._write_json(path, payload)
            target_artifacts[name] = relative
            descriptor = draft["packages"]["target_application"]
            descriptor[f"{name}_path"] = relative
            descriptor[f"{name}_sha256"] = sha256_file(path)

        decisions_path = self.source / "target-application/decisions.json"
        declared_application = json.loads(decisions_path.read_text(encoding="utf-8"))
        declared_application["schema_version"] = "1.0"
        declared_application["artifact_type"] = "target_finite_population_qualified_application"
        declared_application["qualification_scope"] = "target_finite_population"
        declared_application["bindings"] = exact_target_bindings
        declared_application["plan_sha256"] = plan_sha256
        declared_application["qualification_sha256"] = qualification_sha256
        declared_application["external_commitment_sha256"] = external_commitment_sha256
        self._write_json(decisions_path, declared_application)
        draft["packages"]["target_application"]["decisions_sha256"] = sha256_file(decisions_path)

        queue_path = self.source / "selective_review_queue.v1.json"
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        queue["artifact_type"] = "target_finite_population_review_queue"
        queue["qualification_scope"] = "target_finite_population"
        queue["target_bindings"] = exact_target_bindings
        queue["bindings"]["decisions"]["sha256"] = sha256_file(decisions_path)
        for name, relative in target_artifacts.items():
            queue["bindings"][name] = {
                "path": relative,
                "sha256": sha256_file(self.source / relative),
            }
        self._write_json(queue_path, queue)
        draft["qualification_scope"] = "target_finite_population"
        draft["queue"]["sha256"] = sha256_file(queue_path)
        self._write_json(self.source / "review_evidence_bundle.draft.json", draft)

        frozen_payload = json.loads(
            (self.source / target_artifacts["target_frozen_application"]).read_text(encoding="utf-8")
        )
        copied_real_row = {
            "candidate_id": "candidate-1",
            "candidate_fingerprint": self._fixture_candidate_fingerprint(),
            "label": "match_ball",
            "evidence_sha256": "6" * 64,
        }
        normalized_plan = {
            "population": [
                {
                    "candidate_id": copied_real_row["candidate_id"],
                    "candidate_fingerprint": copied_real_row["candidate_fingerprint"],
                }
            ],
            "plan_sha256": plan_sha256,
            "external_commitment": {
                "record_sha256": external_commitment_sha256,
            },
            "plan_commitment_sha256": commitment_sha256,
            "sampling_design_sha256": design_sha256,
            "sample_sha256": sample_sha256,
        }
        normalized_labels = [copied_real_row]
        normalized_labels_manifest = {"annotation_package": {}}
        leakage_validation = mock.Mock(wraps=target_module.validate_target_label_non_leakage)
        with (
            mock.patch(
                "football_tracking.review_evidence_bundle.validate_target_audit_application_binding",
                return_value={"application": frozen_payload},
            ),
            mock.patch(
                "football_tracking.review_evidence_bundle.build_target_qualified_application",
                return_value=declared_application,
            ),
            mock.patch(
                "football_tracking.review_evidence_bundle.validate_target_label_non_leakage",
                leakage_validation,
            ),
            mock.patch(
                "football_tracking.api.broadcast_api.validate_target_prelabel_commitment",
            ),
            mock.patch(
                "football_tracking.target_finite_population._validated_plan",
                return_value=normalized_plan,
            ),
            mock.patch(
                "football_tracking.target_finite_population._validated_labels",
                return_value=(normalized_labels, normalized_labels_manifest),
            ),
            mock.patch(
                "football_tracking.review_evidence_bundle._validate_trusted_prelabel_commitment_anchor",
            ),
        ):
            built = build_review_evidence_bundle(self.source, self.root / "target-published")
            validated = validate_review_evidence_bundle(built.root)
            model_manifest_path = self.source / draft["packages"]["model_development"]["manifest_path"]
            original_model_manifest = model_manifest_path.read_bytes()
            original_queue = queue_path.read_bytes()
            original_draft = (self.source / "review_evidence_bundle.draft.json").read_bytes()
            smuggled_nested_path = model_manifest_path.parent / "smuggled-target-labels.bin"
            smuggled_nested_path.write_bytes(b"candidate-1\x00match_ball\x00nested-target-truth")
            model_manifest = json.loads(model_manifest_path.read_text(encoding="utf-8"))
            model_manifest["unrecognized_authority"] = {
                "path": smuggled_nested_path.name,
                "sha256": sha256_file(smuggled_nested_path),
            }
            self._write_json(model_manifest_path, model_manifest)
            queue["bindings"]["model"]["sha256"] = sha256_file(model_manifest_path)
            self._write_json(queue_path, queue)
            draft["queue"]["sha256"] = sha256_file(queue_path)
            self._write_json(
                self.source / "review_evidence_bundle.draft.json",
                draft,
            )
            try:
                with self.assertRaises(ReviewEvidenceBundleError) as nested_caught:
                    build_review_evidence_bundle(
                        self.source,
                        self.root / "nested-authority-smuggling",
                    )
                self.assertEqual(
                    "invalid_authority_manifest",
                    nested_caught.exception.code,
                )
            finally:
                model_manifest_path.write_bytes(original_model_manifest)
                queue_path.write_bytes(original_queue)
                (self.source / "review_evidence_bundle.draft.json").write_bytes(original_draft)
                smuggled_nested_path.unlink()
                queue = json.loads(queue_path.read_text(encoding="utf-8"))
                draft = json.loads((self.source / "review_evidence_bundle.draft.json").read_text(encoding="utf-8"))
            legacy_digests = {
                sha256_file(self.source / target_artifacts["target_audit_labels"]),
                "1" * 64,
                "2" * 64,
            }
            for package_root in ("model-development", "policy-qualification"):
                leaked_path = self.source / package_root / "copied-target-label-row.json"
                self._write_json(
                    leaked_path,
                    {
                        "training_rows": [copied_real_row],
                        "undeclared_text_padding": "development-only " * 8192,
                    },
                )
                self.assertGreater(leaked_path.stat().st_size, 128 * 1024)
                self.assertTrue(all(digest not in leaked_path.read_text(encoding="utf-8") for digest in legacy_digests))
                try:
                    with (
                        self.subTest(package_root=package_root),
                        self.assertRaises(ReviewEvidenceBundleError) as caught,
                    ):
                        build_review_evidence_bundle(
                            self.source,
                            self.root / f"leaked-{package_root}",
                        )
                    self.assertEqual(
                        "undeclared_non_target_artifact",
                        caught.exception.code,
                    )
                finally:
                    leaked_path.unlink()
            activation_output = self.root / "target-activation"
            activation_output.mkdir()
            activated = activate_review_evidence_bundle(
                built.root,
                activation_output,
                expected_run_id=target["run_id"],
                expected_source_sha256=target["source_sha256"],
                expected_root_contract_sha256=target["root_contract_sha256"],
            )
            self.assertTrue(activated.queue_path.is_file())

        self.assertEqual("2.0", validated.manifest["schema_version"])
        self.assertEqual(
            "target_finite_population_review_evidence_bundle",
            validated.manifest["artifact_type"],
        )
        self.assertEqual(
            "target_finite_population_review_queue",
            json.loads(validated.queue_path.read_text(encoding="utf-8"))["artifact_type"],
        )
        scanned_roots = set()
        published_scan_sets = []
        for call in leakage_validation.call_args_list:
            scan_root = Path(call.kwargs["plan_path"]).parents[1]
            scanned = {Path(path).relative_to(scan_root) for path in call.args[1]}
            if scan_root == built.root:
                published_scan_sets.append(scanned)
            for relative in scanned:
                scanned_roots.add(relative.parts[0])
        self.assertEqual({"model-development", "policy-qualification"}, scanned_roots)
        expected_published_scan = {
            path.relative_to(built.root)
            for package_root in ("model-development", "policy-qualification")
            for path in (built.root / package_root).rglob("*")
            if path.is_file()
        }
        self.assertTrue(published_scan_sets)
        self.assertTrue(all(scanned == expected_published_scan for scanned in published_scan_sets))

    def test_builder_rebases_nested_producer_queue_onto_bundle_root(self) -> None:
        draft = self._write_fixture()
        source_queue = self.source / "selective_review_queue.v1.json"
        producer_queue = self.source / "target-application" / "generated" / source_queue.name
        producer_queue.parent.mkdir(parents=True)
        source_queue.replace(producer_queue)
        draft["queue"] = {
            "source_path": producer_queue.relative_to(self.source).as_posix(),
            "source_sha256": sha256_file(producer_queue),
        }
        self._write_json(self.source / "review_evidence_bundle.draft.json", draft)

        built = build_review_evidence_bundle(self.source, self.root / "published")
        queue = json.loads(built.queue_path.read_text(encoding="utf-8"))

        self.assertEqual(self.root / "published" / "selective_review_queue.v1.json", built.queue_path)
        self.assertFalse((built.root / "target-application" / "generated" / source_queue.name).exists())
        self.assertEqual("target-application/dataset.json", queue["bindings"]["dataset"]["path"])

    def test_validator_rejects_artifact_changed_after_bundle_build(self) -> None:
        self._write_fixture()
        built = build_review_evidence_bundle(self.source, self.root / "published")
        (built.root / "target-application" / "predictions.json").write_text("tampered", encoding="utf-8")

        with self.assertRaisesRegex(ReviewEvidenceBundleError, "inventory changed") as caught:
            validate_review_evidence_bundle(built.root)

        self.assertEqual("bundle_inventory_mismatch", caught.exception.code)

    def test_validator_rejects_handwritten_qualified_policy_when_native_validation_runs(self) -> None:
        self._write_fixture()
        self._strict_patches[2].stop()
        try:
            with self.assertRaises(ReviewEvidenceBundleError) as caught:
                build_review_evidence_bundle(self.source, self.root / "strict-published")
        finally:
            self._strict_patches[2].start()

        self.assertEqual("invalid_policy_qualification_evidence", caught.exception.code)

    def test_builder_rejects_parent_traversal_in_queue_binding(self) -> None:
        draft = self._write_fixture()
        queue_path = self.source / "selective_review_queue.v1.json"
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        queue["bindings"]["policy"]["path"] = "../policy.json"
        self._write_json(queue_path, queue)
        draft["queue"]["sha256"] = sha256_file(queue_path)
        self._write_json(self.source / "review_evidence_bundle.draft.json", draft)

        with self.assertRaises(ReviewEvidenceBundleError) as caught:
            build_review_evidence_bundle(self.source, self.root / "published")

        self.assertEqual("unsafe_bundle_path", caught.exception.code)

    def test_validator_rejects_model_binding_crossed_into_target_application(self) -> None:
        draft = self._write_fixture()
        queue_path = self.source / "selective_review_queue.v1.json"
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        crossed_path = self.source / "target-application" / "predictions.json"
        queue["bindings"]["model"] = {
            "path": "target-application/predictions.json",
            "sha256": sha256_file(crossed_path),
        }
        self._write_json(queue_path, queue)
        draft["queue"]["sha256"] = sha256_file(queue_path)
        self._write_json(self.source / "review_evidence_bundle.draft.json", draft)

        with self.assertRaises(ReviewEvidenceBundleError) as caught:
            build_review_evidence_bundle(self.source, self.root / "published")

        self.assertEqual("invalid_package_partition", caught.exception.code)

    def test_builder_rejects_output_inside_source(self) -> None:
        self._write_fixture()

        with self.assertRaises(ReviewEvidenceBundleError) as caught:
            build_review_evidence_bundle(self.source, self.source / "published")

        self.assertEqual("unsafe_bundle_output", caught.exception.code)

    def test_builder_rejects_source_symlink_when_platform_supports_it(self) -> None:
        self._write_fixture()
        external = self.root / "external.bin"
        external.write_bytes(b"outside-bundle")
        linked = self.source / "target-application" / "evidence" / "linked.bin"
        try:
            linked.symlink_to(external)
        except OSError as exc:
            self.skipTest(f"file symlinks are unavailable: {exc}")

        with self.assertRaises(ReviewEvidenceBundleError) as caught:
            build_review_evidence_bundle(self.source, self.root / "published")

        self.assertEqual("unsafe_bundle_path", caught.exception.code)

    def test_validator_rejects_handwritten_symlink_directory(self) -> None:
        self._write_fixture()
        built = build_review_evidence_bundle(self.source, self.root / "published")
        external = self.root / "external-directory"
        external.mkdir()
        (external / "outside.bin").write_bytes(b"outside-bundle")
        linked = built.root / "target-application" / "linked-directory"
        try:
            linked.symlink_to(external, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks are unavailable: {exc}")

        with self.assertRaises(ReviewEvidenceBundleError) as caught:
            validate_review_evidence_bundle(built.root)

        self.assertEqual("unsafe_bundle_path", caught.exception.code)

    def test_validator_rejects_handwritten_fifo(self) -> None:
        if os.name == "nt" or not hasattr(os, "mkfifo"):
            self.skipTest("POSIX FIFO creation is unavailable")
        self._write_fixture()
        built = build_review_evidence_bundle(self.source, self.root / "published")
        fifo = built.root / "target-application" / "unexpected.fifo"
        os.mkfifo(fifo)

        with self.assertRaises(ReviewEvidenceBundleError) as caught:
            validate_review_evidence_bundle(built.root)

        self.assertEqual("unsafe_bundle_path", caught.exception.code)

    def test_validator_rejects_undeclared_empty_directory(self) -> None:
        self._write_fixture()
        built = build_review_evidence_bundle(self.source, self.root / "published")
        (built.root / "target-application" / "undeclared-directory").mkdir()

        with self.assertRaises(ReviewEvidenceBundleError) as caught:
            validate_review_evidence_bundle(built.root)

        self.assertEqual("invalid_bundle_inventory", caught.exception.code)

    def test_windows_reparse_attribute_is_rejected_even_without_symlink_mode(self) -> None:
        from football_tracking import review_evidence_bundle as bundle_module

        fake_stat = mock.Mock(st_mode=0, st_file_attributes=0x400)
        with mock.patch.object(Path, "lstat", return_value=fake_stat):
            self.assertTrue(bundle_module._is_link_or_reparse(self.root / "junction"))

    def test_validator_rejects_qualification_dataset_reused_for_application(self) -> None:
        draft = self._write_fixture()
        draft["packages"]["policy_qualification"]["dataset_path"] = draft["packages"]["target_application"][
            "dataset_path"
        ]
        draft["packages"]["policy_qualification"]["dataset_sha256"] = draft["packages"]["target_application"][
            "dataset_sha256"
        ]
        # It also crosses the package boundary, but independence is checked first.
        self._write_json(self.source / "review_evidence_bundle.draft.json", draft)

        with self.assertRaises(ReviewEvidenceBundleError) as caught:
            build_review_evidence_bundle(self.source, self.root / "published")

        self.assertEqual("qualification_application_not_independent", caught.exception.code)

    def test_validator_rejects_candidate_identity_leakage_between_populations(self) -> None:
        draft = self._write_fixture()
        dataset_path = self.source / "model-development" / "development-dataset.json"
        dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
        dataset["samples"][0]["candidate_id"] = "candidate-1"
        self._write_json(dataset_path, dataset)
        draft["packages"]["model_development"]["dataset_sha256"] = sha256_file(dataset_path)
        self._write_json(self.source / "review_evidence_bundle.draft.json", draft)

        with self.assertRaises(ReviewEvidenceBundleError) as caught:
            build_review_evidence_bundle(self.source, self.root / "published")

        self.assertEqual("evidence_population_leakage", caught.exception.code)

    def test_builder_rejects_attempt_quota_smaller_than_bundle(self) -> None:
        draft = self._write_fixture()
        draft["provisioning"]["attempt_quota_bytes"] = 1
        self._write_json(self.source / "review_evidence_bundle.draft.json", draft)

        with self.assertRaises(ReviewEvidenceBundleError) as caught:
            build_review_evidence_bundle(self.source, self.root / "published")

        self.assertEqual("bundle_capacity_exceeded", caught.exception.code)

    def test_validator_counts_the_bundle_manifest_against_attempt_quota(self) -> None:
        self._write_fixture()
        built = build_review_evidence_bundle(self.source, self.root / "published")
        manifest = json.loads(built.manifest_path.read_text(encoding="utf-8"))
        quota = int(manifest["provisioning"]["attempt_quota_bytes"])
        manifest["unused_padding"] = "x" * quota
        self._write_json(built.manifest_path, manifest)
        self.assertGreater(built.manifest_path.stat().st_size, quota)

        with self.assertRaises(ReviewEvidenceBundleError) as caught:
            validate_review_evidence_bundle(built.root)

        self.assertEqual("bundle_capacity_exceeded", caught.exception.code)

    def test_builder_enforces_the_file_count_ceiling_before_publication(self) -> None:
        self._write_fixture()

        with (
            mock.patch("football_tracking.review_evidence_bundle.MAX_BUNDLE_FILES", 1),
            self.assertRaises(ReviewEvidenceBundleError) as caught,
        ):
            build_review_evidence_bundle(self.source, self.root / "published")

        self.assertEqual("bundle_capacity_exceeded", caught.exception.code)
        self.assertFalse((self.root / "published").exists())

    def test_validator_enforces_real_json_file_limit_at_the_exact_boundary(self) -> None:
        self._write_fixture()
        built = build_review_evidence_bundle(self.source, self.root / "published")
        largest_json_size = max(path.stat().st_size for path in built.root.rglob("*.json") if path.is_file())

        with mock.patch(
            "football_tracking.review_evidence_bundle.MAX_JSON_BYTES",
            largest_json_size,
        ):
            validate_review_evidence_bundle(built.root)
        with (
            mock.patch(
                "football_tracking.review_evidence_bundle.MAX_JSON_BYTES",
                largest_json_size - 1,
            ),
            self.assertRaises(ReviewEvidenceBundleError) as caught,
        ):
            validate_review_evidence_bundle(built.root)

        self.assertEqual("bundle_capacity_exceeded", caught.exception.code)

    def test_discovery_hides_nonmatching_context_and_hidden_staging_entries(self) -> None:
        draft = self._write_fixture()
        inbox = self.root / "inbox"
        build_review_evidence_bundle(self.source, inbox / "fixture")
        hidden = inbox / ".fixture.staging-interrupted"
        hidden.mkdir()
        (hidden / "not-a-bundle.txt").write_text("incomplete", encoding="utf-8")
        wrong_target = {**draft["target"], "profile_digest": "f" * 64}

        discovered = discover_review_evidence_bundles(
            inbox,
            run_id="run-fixture",
            source_sha256=draft["target"]["source_sha256"],
            root_contract_sha256=draft["target"]["root_contract_sha256"],
            expected_target=wrong_target,
        )

        self.assertEqual([], discovered)

    def test_validator_rejects_incomplete_queue_that_requests_another_round(self) -> None:
        draft = self._write_fixture()
        queue_path = self.source / "selective_review_queue.v1.json"
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        queue["selection"]["coverage_complete"] = False
        queue["selection"]["requires_additional_round"] = True
        self._write_json(queue_path, queue)
        draft["queue"]["sha256"] = sha256_file(queue_path)
        self._write_json(self.source / "review_evidence_bundle.draft.json", draft)

        with self.assertRaises(ReviewEvidenceBundleError) as caught:
            build_review_evidence_bundle(self.source, self.root / "published")

        self.assertEqual("incomplete_review_coverage", caught.exception.code)

    def test_discovery_and_activation_publish_root_queue_last_and_are_idempotent(self) -> None:
        draft = self._write_fixture()
        inbox = self.root / "inbox"
        built = build_review_evidence_bundle(self.source, inbox / "fixture")
        output = self.root / "run-output"
        output.mkdir()

        discovered = discover_review_evidence_bundles(
            inbox,
            run_id="run-fixture",
            source_sha256=draft["target"]["source_sha256"],
            root_contract_sha256=draft["target"]["root_contract_sha256"],
        )
        stages: list[tuple[str, float]] = []
        activated = activate_review_evidence_bundle(
            built.root,
            output,
            expected_run_id="run-fixture",
            expected_source_sha256=draft["target"]["source_sha256"],
            expected_root_contract_sha256=draft["target"]["root_contract_sha256"],
            on_stage=lambda stage, percent: stages.append((stage, percent)),
        )
        repeated = activate_review_evidence_bundle(
            built.root,
            output,
            expected_run_id="run-fixture",
            expected_source_sha256=draft["target"]["source_sha256"],
            expected_root_contract_sha256=draft["target"]["root_contract_sha256"],
        )

        self.assertEqual("available", discovered[0]["status"])
        self.assertTrue((activated.generation_dir / "review_evidence_activation.v1.json").is_file())
        self.assertTrue((output / "selective_review_queue.v1.json").is_file())
        self.assertFalse(activated.idempotent)
        self.assertEqual([("validating", 60.0)], stages)
        self.assertTrue(repeated.idempotent)
        self.assertEqual(activated.queue_sha256, repeated.queue_sha256)

    def test_activation_rejects_fixed_review_decisions(self) -> None:
        draft = self._write_fixture()
        built = build_review_evidence_bundle(self.source, self.root / "bundle")
        output = self.root / "run-output"
        output.mkdir()
        (output / "review_decisions.json").write_text("{}\n", encoding="utf-8")

        with self.assertRaises(ReviewEvidenceBundleError) as caught:
            activate_review_evidence_bundle(
                built.root,
                output,
                expected_run_id="run-fixture",
                expected_source_sha256=draft["target"]["source_sha256"],
                expected_root_contract_sha256=draft["target"]["root_contract_sha256"],
            )

        self.assertEqual("review_evidence_fixed", caught.exception.code)

    def test_activation_cancellation_before_commit_leaves_no_root_queue(self) -> None:
        draft = self._write_fixture()
        built = build_review_evidence_bundle(self.source, self.root / "bundle")
        output = self.root / "run-output"
        output.mkdir()

        with self.assertRaises(ReviewEvidenceBundleError) as caught:
            activate_review_evidence_bundle(
                built.root,
                output,
                expected_run_id="run-fixture",
                expected_source_sha256=draft["target"]["source_sha256"],
                expected_root_contract_sha256=draft["target"]["root_contract_sha256"],
                should_cancel=lambda: True,
            )

        self.assertEqual("review_evidence_import_cancelled", caught.exception.code)
        self.assertFalse((output / "selective_review_queue.v1.json").exists())

    def test_activation_requires_the_exact_requested_bundle_manifest(self) -> None:
        draft = self._write_fixture()
        built = build_review_evidence_bundle(self.source, self.root / "bundle")
        output = self.root / "run-output"
        output.mkdir()

        with self.assertRaises(ReviewEvidenceBundleError) as caught:
            activate_review_evidence_bundle(
                built.root,
                output,
                expected_run_id="run-fixture",
                expected_source_sha256=draft["target"]["source_sha256"],
                expected_root_contract_sha256=draft["target"]["root_contract_sha256"],
                expected_bundle_id="review-evidence-fixture",
                expected_bundle_manifest_sha256="f" * 64,
            )

        self.assertEqual("bundle_identity_mismatch", caught.exception.code)
        self.assertFalse((output / "selective_review_queue.v1.json").exists())

    def test_activation_rejects_disk_exhaustion_before_staging(self) -> None:
        draft = self._write_fixture()
        built = build_review_evidence_bundle(self.source, self.root / "bundle")
        output = self.root / "run-output"
        output.mkdir()

        with (
            mock.patch(
                "football_tracking.review_evidence_bundle.shutil.disk_usage",
                return_value=mock.Mock(free=0),
            ),
            self.assertRaises(ReviewEvidenceBundleError) as caught,
        ):
            activate_review_evidence_bundle(
                built.root,
                output,
                expected_run_id="run-fixture",
                expected_source_sha256=draft["target"]["source_sha256"],
                expected_root_contract_sha256=draft["target"]["root_contract_sha256"],
            )

        self.assertEqual("insufficient_review_evidence_capacity", caught.exception.code)
        self.assertFalse((output / "review_evidence").exists())

    def test_activation_revalidates_source_after_copy_and_rejects_mutation(self) -> None:
        draft = self._write_fixture()
        built = build_review_evidence_bundle(self.source, self.root / "bundle")
        output = self.root / "run-output"
        output.mkdir()
        from football_tracking import review_evidence_bundle as bundle_module

        real_copy = bundle_module._copy_validated_bundle

        def mutate_source_after_copy(*args: object, **kwargs: object) -> None:
            real_copy(*args, **kwargs)
            (built.root / "target-application" / "timing.json").write_text('{"mutated":true}\n', encoding="utf-8")

        with (
            mock.patch.object(
                bundle_module,
                "_copy_validated_bundle",
                side_effect=mutate_source_after_copy,
            ),
            self.assertRaises(ReviewEvidenceBundleError) as caught,
        ):
            activate_review_evidence_bundle(
                built.root,
                output,
                expected_run_id="run-fixture",
                expected_source_sha256=draft["target"]["source_sha256"],
                expected_root_contract_sha256=draft["target"]["root_contract_sha256"],
            )

        self.assertEqual("bundle_inventory_mismatch", caught.exception.code)
        self.assertFalse((output / "selective_review_queue.v1.json").exists())

    def test_root_hard_link_publication_failure_leaves_root_absent_and_retryable(self) -> None:
        draft = self._write_fixture()
        built = build_review_evidence_bundle(self.source, self.root / "bundle")
        output = self.root / "run-output"
        output.mkdir()

        with (
            mock.patch(
                "football_tracking.review_evidence_bundle.os.link",
                side_effect=OSError("injected hard-link failure"),
            ),
            self.assertRaisesRegex(OSError, "hard-link failure"),
        ):
            activate_review_evidence_bundle(
                built.root,
                output,
                expected_run_id="run-fixture",
                expected_source_sha256=draft["target"]["source_sha256"],
                expected_root_contract_sha256=draft["target"]["root_contract_sha256"],
            )

        self.assertFalse((output / "selective_review_queue.v1.json").exists())
        self.assertEqual(1, len(list((output / "review_evidence" / "generations").iterdir())))
        recovered = activate_review_evidence_bundle(
            built.root,
            output,
            expected_run_id="run-fixture",
            expected_source_sha256=draft["target"]["source_sha256"],
            expected_root_contract_sha256=draft["target"]["root_contract_sha256"],
        )

        self.assertTrue(recovered.idempotent)
        self.assertTrue((output / "selective_review_queue.v1.json").is_file())

    def test_activation_consumer_failure_after_generation_rename_never_publishes_root_queue(self) -> None:
        draft = self._write_fixture()
        built = build_review_evidence_bundle(self.source, self.root / "bundle")
        output = self.root / "run-output"
        output.mkdir()
        from football_tracking import review_evidence_bundle as bundle_module

        real_collect = bundle_module.collect_review_evidence_paths

        def fail_activated_queue(
            queue_path: Path,
            trusted_root: Path,
            *,
            binding_base: Path | None = None,
        ) -> list[Path]:
            if queue_path.name == "selective_review_queue.v1.json" and queue_path.parent.name.startswith(
                "review-evidence-"
            ):
                raise BroadcastApiError("injected activated consumer failure")
            return real_collect(queue_path, trusted_root, binding_base=binding_base)

        with (
            mock.patch.object(bundle_module, "collect_review_evidence_paths", side_effect=fail_activated_queue),
            self.assertRaises(ReviewEvidenceBundleError) as caught,
        ):
            activate_review_evidence_bundle(
                built.root,
                output,
                expected_run_id="run-fixture",
                expected_source_sha256=draft["target"]["source_sha256"],
                expected_root_contract_sha256=draft["target"]["root_contract_sha256"],
            )

        self.assertEqual("activated_review_queue_invalid", caught.exception.code)
        self.assertFalse((output / "selective_review_queue.v1.json").exists())
        self.assertEqual(1, len(list((output / "review_evidence" / "generations").iterdir())))

    def test_import_request_requires_manifest_sha256(self) -> None:
        with self.assertRaises(Exception):
            BroadcastReviewEvidenceImportRequest(bundle_id="review-evidence-fixture")

    def test_service_discovers_imports_and_replays_identical_bundle_without_losing_parent_state(self) -> None:
        draft = self._write_fixture()
        service, parent_output = self._create_service_parent(draft)
        try:
            built = build_review_evidence_bundle(
                self.source,
                service.review_evidence_inbox_dir / "fixture",
            )

            available = service.get_broadcast_review_evidence("run-fixture")
            queued = service.import_broadcast_review_evidence(
                "run-fixture",
                {
                    "bundle_id": "review-evidence-fixture",
                    "bundle_manifest_sha256": built.bundle_sha256,
                },
            )
            terminal = self._wait_for_terminal(service, queued["run_id"])
            ready = service.get_broadcast_review_evidence("run-fixture")
            replay = service.import_broadcast_review_evidence(
                "run-fixture",
                {
                    "bundle_id": "review-evidence-fixture",
                    "bundle_manifest_sha256": built.bundle_sha256,
                },
            )
            parent = service.get_run("run-fixture")

            self.assertEqual("available", available["status"])
            self.assertEqual(
                json.loads(service.get_run("run-fixture")["notes"])["expected_config_sha256"],
                draft["target"]["confirmed_config_sha256"],
            )
            self.assertEqual("completed", terminal["status"], terminal.get("error"))
            self.assertEqual("ready", ready["status"])
            self.assertEqual(queued["run_id"], replay["run_id"])
            self.assertEqual("completed", replay["status"])
            self.assertTrue((parent_output / "selective_review_queue.v1.json").is_file())
            self.assertEqual("needs_review", parent["broadcast"]["status"])
            self.assertTrue(parent["broadcast"]["terminal_tail_acknowledged"])
            self.assertIn("existing_non_evidence_blocker", parent["broadcast"]["blocking_reasons"])
            self.assertIn("existing_limitation", parent["broadcast"]["limitations"])
            self.assertNotIn("last_operation", parent["broadcast"])
        finally:
            service.close()

    def test_service_blocks_discovery_and_import_when_config_changed_after_confirmation(self) -> None:
        draft = self._write_fixture()
        service, _ = self._create_service_parent(draft)
        try:
            built = build_review_evidence_bundle(
                self.source,
                service.review_evidence_inbox_dir / "fixture",
            )
            config_path = Path(service.get_run("run-fixture")["config_path"])
            config_path.write_text("input_video: changed-after-confirmation.mp4\n", encoding="utf-8")

            state = service.get_broadcast_review_evidence("run-fixture")

            self.assertEqual("blocked", state["status"])
            self.assertEqual("config_lineage_snapshot_mismatch", state["blocker_code"])
            self.assertEqual("config_lineage_snapshot_mismatch", state["error_code"])
            self.assertEqual(
                ["config_lineage_snapshot_mismatch"],
                state["blocking_reasons"],
            )
            self.assertIn("does not match", state["message"])
            self.assertEqual([], state["bundles"])
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                service.import_broadcast_review_evidence(
                    "run-fixture",
                    {
                        "bundle_id": "review-evidence-fixture",
                        "bundle_manifest_sha256": built.bundle_sha256,
                    },
                )
            self.assertEqual([], service._review_evidence_children("run-fixture"))

            app = FastAPI()
            app.include_router(broadcast_router)
            app.dependency_overrides[get_service] = lambda: service
            client = TestClient(app)
            discovered = client.get("/runs/run-fixture/broadcast/review-evidence")
            rejected = client.post(
                "/runs/run-fixture/broadcast/review-evidence/import",
                json={
                    "bundle_id": "review-evidence-fixture",
                    "bundle_manifest_sha256": built.bundle_sha256,
                },
            )

            self.assertEqual(200, discovered.status_code)
            self.assertEqual("blocked", discovered.json()["status"])
            self.assertEqual(409, rejected.status_code)
            self.assertIn("does not match", rejected.json()["detail"])
        finally:
            service.close()

    def test_service_reconfirms_crlf_lineage_and_revalidates_it_before_import(self) -> None:
        draft = self._write_fixture()
        service, _ = self._create_service_parent(draft)
        try:
            parent = service.get_run("run-fixture")
            config_path = Path(parent["config_path"])
            config_path.write_bytes(config_path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
            registry = service._read_registry()
            target = next(item for item in registry["runs"] if item["run_id"] == "run-fixture")
            confirmation = json.loads(target["notes"])
            confirmation["expected_config_sha256"] = hashlib.sha256(
                config_path.read_bytes().replace(b"\r\n", b"\n")
            ).hexdigest()
            target["notes"] = json.dumps(confirmation, sort_keys=True, separators=(",", ":"))
            service._write_registry(registry)

            registry = service._read_registry()
            target = next(item for item in registry["runs"] if item["run_id"] == "run-fixture")
            target["broadcast"]["submission_id"] = "submission-completed"
            target["broadcast"]["generation_id"] = "generation-completed"
            accepted_trial = {
                **json.loads(json.dumps(target)),
                "run_id": "trial-accepted",
                "source": "tracking",
                "status": "completed",
                "parent_run_id": None,
                "output_dir": str(self.root / "trial-accepted"),
                "notes": json.dumps(
                    {
                        "purpose": "trial",
                        "workflow_id": "workflow-1",
                        "trial_intent_sha256": "2" * 64,
                        "calibration_digest": "5" * 64,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "broadcast": {},
            }
            failed = {
                **json.loads(json.dumps(target)),
                "run_id": "run-failed",
                "status": "failed",
                "output_dir": str(self.root / "run-failed"),
                "notes": target["notes"],
                "broadcast": {
                    **target["broadcast"],
                    "submission_id": "submission-failed",
                    "generation_id": "generation-failed",
                },
            }
            registry["runs"].extend([accepted_trial, failed])
            service._write_registry(registry)

            required = service.get_broadcast_review_evidence("run-fixture")
            self.assertEqual("blocked", required["status"])
            self.assertEqual(
                "confirmed_config_lineage_reconfirmation_required",
                required["blocker_code"],
            )
            self.assertEqual("reconfirm_production_config", required["recovery_action"])
            challenge = required["config_lineage_reconfirmation"]
            self.assertEqual("run-fixture", challenge["target_run_id"])
            self.assertEqual("fixture.yaml", challenge["confirmed_config_name"])
            self.assertEqual(
                confirmation["expected_config_sha256"],
                challenge["confirmed_text_sha256"],
            )
            self.assertEqual(sha256_file(config_path), challenge["expected_observed_raw_sha256"])
            workflow_bindings = challenge["workflow_bindings"]
            workflow_source_signature = yaml.safe_load(config_path.read_text(encoding="utf-8"))["metadata"][
                "production_workflow"
            ]["source_signature"]
            self.assertEqual(
                {"path", "size_bytes", "modified_at"},
                set(workflow_source_signature),
            )

            app = FastAPI()
            app.include_router(broadcast_router)
            app.dependency_overrides[get_service] = lambda: service
            client = TestClient(app)
            discovered = client.get("/runs/run-fixture/broadcast/review-evidence")
            missing_reviewer = client.post(
                "/runs/run-fixture/broadcast/config-lineage-reconfirmation",
                json={
                    **challenge,
                    "operator_id": "operator-1",
                },
            )
            same_identity = client.post(
                "/runs/run-fixture/broadcast/config-lineage-reconfirmation",
                json={
                    **challenge,
                    "operator_id": "operator-1",
                    "reviewer_id": "operator-1",
                },
            )
            self.assertEqual(200, discovered.status_code)
            self.assertEqual(
                challenge,
                discovered.json()["config_lineage_reconfirmation"],
            )
            self.assertEqual(422, missing_reviewer.status_code)
            self.assertEqual(422, same_identity.status_code)

            for field in (
                "workflow_id",
                "accepted_trial",
                "request",
                "intent",
                "trial_patch",
                "production_patch",
                "calibration",
                "source_signature",
                "historical_full_runs",
            ):
                tampered = json.loads(json.dumps(workflow_bindings))
                if field == "workflow_id":
                    tampered[field] = "workflow-tampered"
                elif field == "historical_full_runs":
                    tampered[field][0]["record_sha256"] = "f" * 64
                elif field == "accepted_trial":
                    tampered[field]["record_sha256"] = "f" * 64
                else:
                    tampered[field]["sha256"] = "f" * 64
                with (
                    self.subTest(authoritative_field=field),
                    self.assertRaisesRegex(
                        ConfigLineageError,
                        "server-derived authority",
                    ),
                ):
                    service.reconfirm_broadcast_config_lineage(
                        "run-fixture",
                        {
                            **challenge,
                            "expected_observed_raw_sha256": sha256_file(config_path),
                            "workflow_bindings": tampered,
                            "operator_id": "operator-1",
                            "reviewer_id": "reviewer-1",
                        },
                    )

            config_path.write_bytes(config_path.read_bytes().replace(b"\r\n", b"\n"))
            with self.assertRaisesRegex(ConfigLineageError, "snapshot mismatch"):
                service.reconfirm_broadcast_config_lineage(
                    "run-fixture",
                    {
                        **challenge,
                        "operator_id": "operator-1",
                        "reviewer_id": "reviewer-1",
                    },
                )
            normalized = service.get_broadcast_review_evidence("run-fixture")
            self.assertNotIn("config_lineage_reconfirmation", normalized)

            config_path.write_bytes(config_path.read_bytes().replace(b"\n", b"\r\n"))
            refreshed = service.get_broadcast_review_evidence("run-fixture")
            challenge = refreshed["config_lineage_reconfirmation"]
            self.assertEqual(
                required["config_lineage_reconfirmation"]["expected_observed_raw_sha256"],
                challenge["expected_observed_raw_sha256"],
            )

            if os.name == "nt":
                with self.assertRaises(ConfigLineageError) as unsupported:
                    service.reconfirm_broadcast_config_lineage(
                        "run-fixture",
                        {
                            **challenge,
                            "operator_id": "operator-1",
                            "reviewer_id": "reviewer-1",
                        },
                    )
                self.assertEqual(CONFIG_LINEAGE_UNSAFE, unsupported.exception.code)
                return
            response = service.reconfirm_broadcast_config_lineage(
                "run-fixture",
                {
                    **challenge,
                    "operator_id": "operator-1",
                    "reviewer_id": "reviewer-1",
                },
            )
            self.assertEqual("reconfirmed", response["status"])
            self.assertFalse(response["historical_raw_snapshot_observed"])
            parent_after = service.get_run("run-fixture")
            self.assertEqual(
                target["broadcast"]["blocking_reasons"],
                parent_after["broadcast"]["blocking_reasons"],
            )

            draft["target"] = service._review_evidence_target(
                "run-fixture",
                Path(target["output_dir"]),
                parent=parent_after,
            )
            self._write_json(self.source / "review_evidence_bundle.draft.json", draft)
            build_review_evidence_bundle(
                self.source,
                service.review_evidence_inbox_dir / "lineage-fixture",
            )
            available = service.get_broadcast_review_evidence("run-fixture")
            self.assertEqual("available", available["status"])

            config_path.write_bytes(b"input_video: tampered.mp4\r\n")
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                service.import_broadcast_review_evidence(
                    "run-fixture",
                    {
                        "bundle_id": "review-evidence-fixture",
                        "bundle_manifest_sha256": available["bundles"][0]["bundle_manifest_sha256"],
                    },
                )
            self.assertEqual([], service._review_evidence_children("run-fixture"))
        finally:
            service.close()

    @unittest.skipIf(os.name == "nt", "POSIX config-lineage regression coverage requires fork")
    def test_review_evidence_state_does_not_reenter_lock_after_crlf_lineage_reconfirmation(self) -> None:
        draft = self._write_fixture()
        service, _ = self._create_service_parent(draft)
        try:
            self._reconfirm_crlf_config_lineage(service)

            state = self._call_in_forked_process(
                lambda: service.get_broadcast_review_evidence("run-fixture"),
                label="review evidence state",
            )

            self.assertEqual("not_available", state["status"])
            self.assertEqual("review_evidence_bundle_not_available", state["blocker_code"])
        finally:
            service.close()

    @unittest.skipIf(os.name == "nt", "POSIX config-lineage regression coverage requires fork")
    def test_review_windows_do_not_reenter_lock_after_crlf_lineage_reconfirmation(self) -> None:
        draft = self._write_fixture()
        service, _ = self._create_service_parent(draft)
        try:
            built = build_review_evidence_bundle(
                self.source,
                service.review_evidence_inbox_dir / "pre-lineage-fixture",
            )
            queued = service.import_broadcast_review_evidence(
                "run-fixture",
                {
                    "bundle_id": "review-evidence-fixture",
                    "bundle_manifest_sha256": built.bundle_sha256,
                },
            )
            terminal = self._wait_for_terminal(service, queued["run_id"])
            self.assertEqual("completed", terminal["status"], terminal.get("error"))
            self._reconfirm_crlf_config_lineage(service)

            state = self._call_in_forked_process(
                lambda: service.get_broadcast_review_windows("run-fixture"),
                label="review windows",
            )

            self.assertEqual("needs_review", state["status"])
            self.assertEqual("invalid_or_stale_selective_review_evidence", state["reason"])
        finally:
            service.close()

    def test_activated_review_consumers_reject_changed_confirmed_config(self) -> None:
        self._assert_activated_review_consumers_reject_target_mutation("config")

    def test_activated_review_consumers_reject_changed_root_contract(self) -> None:
        self._assert_activated_review_consumers_reject_target_mutation("root_contract")

    def test_activated_review_consumers_reject_changed_action_signal_binding(self) -> None:
        self._assert_activated_review_consumers_reject_target_mutation("action_binding")

    def test_direct_service_import_requires_exact_manifest_sha256(self) -> None:
        draft = self._write_fixture()
        service, _ = self._create_service_parent(draft)
        try:
            build_review_evidence_bundle(self.source, service.review_evidence_inbox_dir / "fixture")

            with self.assertRaisesRegex(ValueError, "bundle_manifest_sha256"):
                service.import_broadcast_review_evidence(
                    "run-fixture",
                    {"bundle_id": "review-evidence-fixture"},
                )
        finally:
            service.close()

    def test_service_cancellation_before_commit_is_terminal_and_retryable(self) -> None:
        draft = self._write_fixture()
        service, parent_output = self._create_service_parent(draft)
        try:
            built = build_review_evidence_bundle(
                self.source,
                service.review_evidence_inbox_dir / "fixture",
            )
            entered = __import__("threading").Event()

            def cancellable_import(*_args: object, should_cancel=None, **_kwargs: object) -> object:
                entered.set()
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    if should_cancel is not None and should_cancel():
                        raise ReviewEvidenceBundleError(
                            "review_evidence_import_cancelled", "review evidence import was cancelled"
                        )
                    time.sleep(0.01)
                raise AssertionError("test import did not observe cancellation")

            with mock.patch(
                "football_tracking.api.service.activate_review_evidence_bundle",
                side_effect=cancellable_import,
            ):
                queued = service.import_broadcast_review_evidence(
                    "run-fixture",
                    {
                        "bundle_id": "review-evidence-fixture",
                        "bundle_manifest_sha256": built.bundle_sha256,
                    },
                )
                self.assertTrue(entered.wait(2.0))
                service.cancel_run(queued["run_id"])
                terminal = self._wait_for_terminal(service, queued["run_id"])

            state = service.get_broadcast_review_evidence("run-fixture")
            self.assertEqual("cancelled", terminal["status"])
            self.assertEqual("cancelled", state["status"])
            self.assertEqual(queued["run_id"], state["retry_from_job_id"])
            self.assertFalse((parent_output / "selective_review_queue.v1.json").exists())
        finally:
            service.close()

    def test_commit_boundary_rechecks_persisted_cancellation_before_mutation(self) -> None:
        draft = self._write_fixture()
        service, parent_output = self._create_service_parent(draft)
        try:
            built = build_review_evidence_bundle(
                self.source,
                service.review_evidence_inbox_dir / "fixture",
            )
            entered = threading.Event()
            advance_to_commit = threading.Event()

            def cancelled_at_commit(*_args: object, on_commit_started=None, **_kwargs: object) -> object:
                entered.set()
                if not advance_to_commit.wait(3.0):
                    raise AssertionError("test did not advance to commit boundary")
                if on_commit_started is None:
                    raise AssertionError("activation omitted commit callback")
                on_commit_started()
                raise AssertionError("cancelled commit boundary was allowed to mutate")

            with mock.patch(
                "football_tracking.api.service.activate_review_evidence_bundle",
                side_effect=cancelled_at_commit,
            ):
                queued = service.import_broadcast_review_evidence(
                    "run-fixture",
                    {
                        "bundle_id": "review-evidence-fixture",
                        "bundle_manifest_sha256": built.bundle_sha256,
                    },
                )
                self.assertTrue(entered.wait(2.0))
                service.cancel_run(queued["run_id"])
                advance_to_commit.set()
                terminal = self._wait_for_terminal(service, queued["run_id"])

            self.assertEqual("cancelled", terminal["status"])
            self.assertFalse(terminal["broadcast"]["commit_started"])
            self.assertFalse((parent_output / "selective_review_queue.v1.json").exists())
        finally:
            service.close()

    def test_commit_boundary_rehashes_exact_parent_context_before_mutation(self) -> None:
        draft = self._write_fixture()
        service, parent_output = self._create_service_parent(draft)
        try:
            built = build_review_evidence_bundle(
                self.source,
                service.review_evidence_inbox_dir / "fixture",
            )

            def mutate_context_at_commit(
                *args: object,
                on_commit_started=None,
                **kwargs: object,
            ) -> object:
                if on_commit_started is None:
                    raise AssertionError("activation omitted commit callback")

                def changed_context_commit() -> None:
                    self._write_json(
                        parent_output / "action_signal_binding.v1.json",
                        {
                            "schema_version": "1.0",
                            "artifact_type": "broadcast_action_signal_binding",
                            "mutated": True,
                        },
                    )
                    on_commit_started()

                return activate_review_evidence_bundle(
                    *args,
                    on_commit_started=changed_context_commit,
                    **kwargs,
                )

            with mock.patch(
                "football_tracking.api.service.activate_review_evidence_bundle",
                side_effect=mutate_context_at_commit,
            ):
                queued = service.import_broadcast_review_evidence(
                    "run-fixture",
                    {
                        "bundle_id": "review-evidence-fixture",
                        "bundle_manifest_sha256": built.bundle_sha256,
                    },
                )
                terminal = self._wait_for_terminal(service, queued["run_id"])

            self.assertEqual("failed", terminal["status"])
            self.assertEqual("blocked", terminal["broadcast"]["operation_status"])
            self.assertEqual("target_binding_mismatch", terminal["broadcast"]["error_code"])
            self.assertFalse((parent_output / "selective_review_queue.v1.json").exists())
            self.assertEqual([], list((parent_output / "review_evidence" / "generations").iterdir()))
        finally:
            service.close()

    def test_cancel_is_explicitly_refused_after_commit_started(self) -> None:
        draft = self._write_fixture()
        service, parent_output = self._create_service_parent(draft)
        try:
            built = build_review_evidence_bundle(
                self.source,
                service.review_evidence_inbox_dir / "fixture",
            )
            commit_entered = threading.Event()
            release_commit = threading.Event()

            def commit_in_progress(*_args: object, on_commit_started=None, **_kwargs: object) -> object:
                if on_commit_started is None:
                    raise AssertionError("activation omitted commit callback")
                on_commit_started()
                commit_entered.set()
                if not release_commit.wait(3.0):
                    raise AssertionError("test did not release commit")
                raise ReviewEvidenceBundleError(
                    "injected_after_commit_started",
                    "injected failure after commit boundary",
                )

            with mock.patch(
                "football_tracking.api.service.activate_review_evidence_bundle",
                side_effect=commit_in_progress,
            ):
                queued = service.import_broadcast_review_evidence(
                    "run-fixture",
                    {
                        "bundle_id": "review-evidence-fixture",
                        "bundle_manifest_sha256": built.bundle_sha256,
                    },
                )
                self.assertTrue(commit_entered.wait(2.0))
                with self.assertRaisesRegex(RuntimeError, "commit has already started"):
                    service.cancel_run(queued["run_id"])
                active = service.get_run(queued["run_id"])
                self.assertTrue(active["broadcast"]["commit_started"])
                self.assertIsNot(active["broadcast"].get("cancel_requested"), True)
                release_commit.set()
                terminal = self._wait_for_terminal(service, queued["run_id"])

            self.assertEqual("failed", terminal["status"])
            self.assertTrue(terminal["broadcast"]["commit_started"])
            self.assertFalse((parent_output / "selective_review_queue.v1.json").exists())
        finally:
            release_commit.set()
            service.close()

    def test_service_restart_marks_orphaned_import_retryable_and_explicit_retry_completes(self) -> None:
        draft = self._write_fixture()
        service, _ = self._create_service_parent(draft)
        built = build_review_evidence_bundle(self.source, service.review_evidence_inbox_dir / "fixture")
        with mock.patch.object(service, "_start_thread_or_cleanup"):
            orphan = service.import_broadcast_review_evidence(
                "run-fixture",
                {
                    "bundle_id": "review-evidence-fixture",
                    "bundle_manifest_sha256": built.bundle_sha256,
                },
            )
        repo = service.repo_root
        service.close()
        recovered_service = ApiService(repo)
        try:
            recovered = recovered_service.get_run(orphan["run_id"])
            state = recovered_service.get_broadcast_review_evidence("run-fixture")
            retried = recovered_service.import_broadcast_review_evidence(
                "run-fixture",
                {
                    "bundle_id": "review-evidence-fixture",
                    "bundle_manifest_sha256": built.bundle_sha256,
                    "retry_from_job_id": orphan["run_id"],
                },
            )
            terminal = self._wait_for_terminal(recovered_service, retried["run_id"])

            self.assertEqual("failed", recovered["status"])
            self.assertEqual("review_evidence_import_interrupted", recovered["broadcast"]["error_code"])
            self.assertEqual("failed", state["status"])
            self.assertTrue(state["retryable"])
            self.assertEqual("completed", terminal["status"], terminal.get("error"))
        finally:
            recovered_service.close()

    def test_service_restart_recovers_failed_registry_write_after_authoritative_root_commit(self) -> None:
        draft = self._write_fixture()
        service, parent_output = self._create_service_parent(draft)
        built = build_review_evidence_bundle(self.source, service.review_evidence_inbox_dir / "fixture")
        queued = service.import_broadcast_review_evidence(
            "run-fixture",
            {
                "bundle_id": "review-evidence-fixture",
                "bundle_manifest_sha256": built.bundle_sha256,
            },
        )
        terminal = self._wait_for_terminal(service, queued["run_id"])
        self.assertEqual("completed", terminal["status"], terminal.get("error"))
        self.assertTrue((parent_output / "selective_review_queue.v1.json").is_file())
        with service._lock, service._registry_transaction() as registry:
            child = next(item for item in registry["runs"] if item.get("run_id") == queued["run_id"])
            child["status"] = "failed"
            child["error"] = "injected registry write interruption"
            child["broadcast"]["operation_status"] = "failed"
        repo = service.repo_root
        service.close()

        recovered_service = ApiService(repo)
        try:
            recovered = recovered_service.get_run(queued["run_id"])
            parent = recovered_service.get_run("run-fixture")

            self.assertEqual("completed", recovered["status"])
            self.assertTrue(recovered["broadcast"]["recovered"])
            self.assertEqual("ready", parent["broadcast"]["review_evidence"]["status"])
            self.assertTrue((Path(recovered["output_dir"]) / "review_evidence_import_report.v1.json").is_file())
        finally:
            recovered_service.close()

    def test_recovery_blocks_expected_target_context_without_swallowing_unknown_runtime(self) -> None:
        repo = self.root / "recovery-unit-repo"
        service = ApiService(repo)
        parent_output = repo / "outputs" / "runs" / "fixture" / "run-fixture"
        child_output = repo / "outputs" / "runs" / "review-evidence" / "import-fixture"
        parent_output.mkdir(parents=True)
        child_output.mkdir(parents=True)
        queue_path = parent_output / "selective_review_queue.v1.json"
        queue_path.write_bytes(b"committed-root-queue")
        generation_file = parent_output / "review_evidence" / "generations" / "generation-1" / "payload.bin"
        generation_file.parent.mkdir(parents=True)
        generation_file.write_bytes(b"committed-generation")
        registry = service._read_registry()
        registry["runs"].extend(
            [
                {
                    "run_id": "run-fixture",
                    "source": "broadcast_hybrid",
                    "status": "completed",
                    "output_dir": str(parent_output),
                    "broadcast": {
                        "status": "needs_review",
                        "blocking_reasons": ["existing_non_evidence_blocker"],
                    },
                },
                {
                    "run_id": "import-fixture",
                    "source": "broadcast_review_evidence_import",
                    "status": "failed",
                    "parent_run_id": "run-fixture",
                    "output_dir": str(child_output),
                    "broadcast": {
                        "operation_status": "failed",
                        "request": {
                            "bundle_id": "bundle-1",
                            "bundle_manifest_sha256": "1" * 64,
                        },
                    },
                },
            ]
        )
        service._write_registry(registry)
        service.close()

        target_error = service_module._ReviewEvidenceTargetContextError(
            CONFIG_LINEAGE_MISMATCH,
            "injected config lineage mismatch",
        )
        with mock.patch.object(
            ApiService,
            "_validate_current_review_queue_locked",
            side_effect=target_error,
        ):
            recovered_service = ApiService(repo)
        try:
            recovered = recovered_service.get_run("import-fixture")
            parent = recovered_service.get_run("run-fixture")

            self.assertEqual("failed", recovered["status"])
            self.assertEqual("blocked", recovered["broadcast"]["operation_status"])
            self.assertEqual(CONFIG_LINEAGE_MISMATCH, recovered["broadcast"]["blocker_code"])
            self.assertEqual(CONFIG_LINEAGE_MISMATCH, recovered["broadcast"]["error_code"])
            self.assertEqual("inspect_production_config_lineage", recovered["broadcast"]["recovery_action"])
            self.assertEqual("needs_review", parent["broadcast"]["status"])
            self.assertIn(CONFIG_LINEAGE_MISMATCH, parent["broadcast"]["blocking_reasons"])
            self.assertEqual("blocked", parent["broadcast"]["review_evidence"]["status"])
            self.assertEqual(
                CONFIG_LINEAGE_MISMATCH,
                parent["broadcast"]["review_evidence"]["blocker_code"],
            )
            self.assertEqual(
                "inspect_production_config_lineage",
                parent["broadcast"]["review_evidence"]["recovery_action"],
            )
            self.assertEqual(b"committed-root-queue", queue_path.read_bytes())
            self.assertEqual(b"committed-generation", generation_file.read_bytes())
        finally:
            recovered_service.close()

        with (
            mock.patch.object(
                ApiService,
                "_validate_current_review_queue_locked",
                side_effect=RuntimeError("injected unexpected recovery bug"),
            ),
            self.assertRaisesRegex(RuntimeError, "injected unexpected recovery bug"),
        ):
            ApiService(repo)

    def test_service_restart_blocks_failed_committed_import_when_config_lineage_is_invalid(self) -> None:
        self._assert_restart_blocks_committed_import_for_lineage_failure(
            orphaned_status="failed",
            config_failure="mismatch",
            expected_code=CONFIG_LINEAGE_MISMATCH,
            expected_recovery_action="inspect_production_config_lineage",
            assert_unknown_runtime=True,
        )

    def test_service_restart_blocks_cancelled_committed_import_when_config_lineage_is_missing(self) -> None:
        self._assert_restart_blocks_committed_import_for_lineage_failure(
            orphaned_status="cancelled",
            config_failure="reconfirmation_required",
            expected_code=CONFIG_LINEAGE_REQUIRED,
            expected_recovery_action="reconfirm_production_config",
        )

    def test_service_revokes_exact_unconsumed_activation_and_recovers_parent_state(self) -> None:
        draft = self._write_fixture()
        service, parent_output = self._create_service_parent(draft)
        try:
            built = build_review_evidence_bundle(self.source, service.review_evidence_inbox_dir / "fixture")
            queued = service.import_broadcast_review_evidence(
                "run-fixture",
                {
                    "bundle_id": "review-evidence-fixture",
                    "bundle_manifest_sha256": built.bundle_sha256,
                },
            )
            terminal = self._wait_for_terminal(service, queued["run_id"])
            result = terminal["broadcast"]["result"]
            previous_generation = service.get_run("run-fixture")["broadcast"]["status_generation"]

            revoked = service.revoke_broadcast_review_evidence(
                "run-fixture",
                result["review_evidence_generation_id"],
                result["queue_sha256"],
            )
            parent = service.get_run("run-fixture")

            self.assertEqual("revoked", revoked["status"])
            self.assertFalse((parent_output / "selective_review_queue.v1.json").exists())
            self.assertEqual("revoked", parent["broadcast"]["review_evidence"]["status"])
            self.assertIn("missing_qualified_selective_review_queue", parent["broadcast"]["blocking_reasons"])
            self.assertNotEqual(previous_generation, parent["broadcast"]["status_generation"])
            report = json.loads(
                (
                    parent_output
                    / "broadcast_status"
                    / parent["broadcast"]["status_generation"]
                    / "broadcast_quality_report.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(parent["broadcast"]["status"], report["status"])
            self.assertIn("missing_qualified_selective_review_queue", report["blocking_reasons"])
            self.assertEqual(parent["broadcast"]["status_generation"], report["status_generation"])
        finally:
            service.close()

    def test_stale_review_post_is_rejected_after_revoke_and_reimport(self) -> None:
        draft = self._write_fixture()
        service, parent_output = self._create_service_parent(draft)
        try:
            built = build_review_evidence_bundle(self.source, service.review_evidence_inbox_dir / "fixture")
            queued = service.import_broadcast_review_evidence(
                "run-fixture",
                {
                    "bundle_id": "review-evidence-fixture",
                    "bundle_manifest_sha256": built.bundle_sha256,
                },
            )
            terminal = self._wait_for_terminal(service, queued["run_id"])
            result = terminal["broadcast"]["result"]
            old_windows = service.get_broadcast_review_windows("run-fixture")
            self.assertEqual("ready", old_windows["status"])

            service.revoke_broadcast_review_evidence(
                "run-fixture",
                result["review_evidence_generation_id"],
                result["queue_sha256"],
            )
            draft["bundle_id"] = "review-evidence-replacement"
            self._write_json(self.source / "review_evidence_bundle.draft.json", draft)
            replacement = build_review_evidence_bundle(
                self.source,
                service.review_evidence_inbox_dir / "replacement",
            )
            replacement_job = service.import_broadcast_review_evidence(
                "run-fixture",
                {
                    "bundle_id": "review-evidence-replacement",
                    "bundle_manifest_sha256": replacement.bundle_sha256,
                },
            )
            replacement_terminal = self._wait_for_terminal(service, replacement_job["run_id"])
            self.assertEqual("completed", replacement_terminal["status"])
            current_windows = service.get_broadcast_review_windows("run-fixture")
            self.assertNotEqual(old_windows["queue_sha256"], current_windows["queue_sha256"])

            app = FastAPI()
            app.include_router(broadcast_router)
            app.dependency_overrides[get_service] = lambda: service
            client = TestClient(app)
            review_item = old_windows["items"][0]
            candidate = review_item["candidates"][0]
            response = client.post(
                "/runs/run-fixture/broadcast/review-actions",
                json={
                    "queue_sha256": old_windows["queue_sha256"],
                    "actions": [
                        {
                            "action_id": "stale-action",
                            "review_item_id": review_item["review_item_id"],
                            "candidate_id": candidate["candidate_id"],
                            "reviewer_id": "operator",
                            "action": "confirm_ball",
                        }
                    ],
                },
            )

            self.assertEqual(409, response.status_code)
            self.assertIn("queue changed", response.json()["detail"])
            self.assertFalse((parent_output / "review_decisions.json").exists())
        finally:
            service.close()

    def test_service_refuses_revoke_after_review_consumption(self) -> None:
        draft = self._write_fixture()
        service, parent_output = self._create_service_parent(draft)
        try:
            built = build_review_evidence_bundle(self.source, service.review_evidence_inbox_dir / "fixture")
            queued = service.import_broadcast_review_evidence(
                "run-fixture",
                {
                    "bundle_id": "review-evidence-fixture",
                    "bundle_manifest_sha256": built.bundle_sha256,
                },
            )
            terminal = self._wait_for_terminal(service, queued["run_id"])
            result = terminal["broadcast"]["result"]
            (parent_output / "review_decisions.json").write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "already consumed"):
                service.revoke_broadcast_review_evidence(
                    "run-fixture",
                    result["review_evidence_generation_id"],
                    result["queue_sha256"],
                )

            self.assertTrue((parent_output / "selective_review_queue.v1.json").is_file())
        finally:
            service.close()

    def test_delete_revoke_route_handles_identity_consumption_and_success(self) -> None:
        draft = self._write_fixture()
        service, parent_output = self._create_service_parent(draft)
        try:
            built = build_review_evidence_bundle(self.source, service.review_evidence_inbox_dir / "fixture")
            queued = service.import_broadcast_review_evidence(
                "run-fixture",
                {
                    "bundle_id": "review-evidence-fixture",
                    "bundle_manifest_sha256": built.bundle_sha256,
                },
            )
            terminal = self._wait_for_terminal(service, queued["run_id"])
            result = terminal["broadcast"]["result"]
            path = f"/runs/run-fixture/broadcast/review-evidence/{result['review_evidence_generation_id']}"
            app = FastAPI()
            app.include_router(broadcast_router)
            app.dependency_overrides[get_service] = lambda: service
            client = TestClient(app)

            identity_mismatch = client.delete(path, params={"queue_sha256": "f" * 64})
            self.assertEqual(409, identity_mismatch.status_code)
            self.assertTrue((parent_output / "selective_review_queue.v1.json").is_file())

            (parent_output / "review_decisions.json").write_text("{}\n", encoding="utf-8")
            consumed = client.delete(path, params={"queue_sha256": result["queue_sha256"]})
            self.assertEqual(409, consumed.status_code)
            self.assertIn("already consumed", consumed.json()["detail"])
            (parent_output / "review_decisions.json").unlink()

            revoked = client.delete(path, params={"queue_sha256": result["queue_sha256"]})
            self.assertEqual(200, revoked.status_code)
            self.assertEqual(
                {
                    "run_id": "run-fixture",
                    "status": "revoked",
                    "generation_id": result["review_evidence_generation_id"],
                    "queue_sha256": result["queue_sha256"],
                },
                {key: revoked.json()[key] for key in ("run_id", "status", "generation_id", "queue_sha256")},
            )
            self.assertIsInstance(revoked.json()["revoked_at"], str)
            self.assertFalse((parent_output / "selective_review_queue.v1.json").exists())
        finally:
            service.close()

    def test_review_action_and_revoke_race_has_exactly_one_authoritative_winner(self) -> None:
        draft = self._write_fixture()
        service, parent_output = self._create_service_parent(draft)
        try:
            built = build_review_evidence_bundle(self.source, service.review_evidence_inbox_dir / "fixture")
            queued = service.import_broadcast_review_evidence(
                "run-fixture",
                {
                    "bundle_id": "review-evidence-fixture",
                    "bundle_manifest_sha256": built.bundle_sha256,
                },
            )
            terminal = self._wait_for_terminal(service, queued["run_id"])
            result = terminal["broadcast"]["result"]
            barrier = threading.Barrier(3)
            successes: list[str] = []
            failures: list[BaseException] = []

            def submit_action() -> None:
                barrier.wait()
                try:
                    service.submit_broadcast_review_actions(
                        "run-fixture",
                        {
                            "queue_sha256": result["queue_sha256"],
                            "actions": [
                                {
                                    "action_id": "action-1",
                                    "review_item_id": "window-1",
                                    "candidate_id": "candidate-1",
                                    "reviewer_id": "operator",
                                    "created_at": "2026-07-15T00:02:00Z",
                                    "action": "confirm_ball",
                                    "noise_subtype": None,
                                }
                            ],
                        },
                    )
                    successes.append("action")
                except BaseException as exc:
                    failures.append(exc)

            def revoke() -> None:
                barrier.wait()
                try:
                    service.revoke_broadcast_review_evidence(
                        "run-fixture",
                        result["review_evidence_generation_id"],
                        result["queue_sha256"],
                    )
                    successes.append("revoke")
                except BaseException as exc:
                    failures.append(exc)

            action_thread = threading.Thread(target=submit_action)
            revoke_thread = threading.Thread(target=revoke)
            action_thread.start()
            revoke_thread.start()
            barrier.wait()
            action_thread.join(timeout=5.0)
            revoke_thread.join(timeout=5.0)

            generation_dir = parent_output / "review_evidence" / "generations" / result["review_evidence_generation_id"]
            decisions_exist = (parent_output / "review_decisions.json").is_file()
            revocation_exists = (generation_dir / "review_evidence_revocation.v1.json").is_file()
            queue_exists = (parent_output / "selective_review_queue.v1.json").is_file()
            self.assertFalse(action_thread.is_alive())
            self.assertFalse(revoke_thread.is_alive())
            self.assertEqual(1, len(successes), failures)
            self.assertEqual(1, len(failures), successes)
            self.assertNotEqual(decisions_exist, revocation_exists)
            self.assertEqual(decisions_exist, queue_exists)
            self.assertEqual("action" if decisions_exist else "revoke", successes[0])
        finally:
            service.close()

    def test_service_restart_recovers_filesystem_revocation_before_registry_update(self) -> None:
        draft = self._write_fixture()
        service, parent_output = self._create_service_parent(draft)
        built = build_review_evidence_bundle(self.source, service.review_evidence_inbox_dir / "fixture")
        queued = service.import_broadcast_review_evidence(
            "run-fixture",
            {
                "bundle_id": "review-evidence-fixture",
                "bundle_manifest_sha256": built.bundle_sha256,
            },
        )
        terminal = self._wait_for_terminal(service, queued["run_id"])
        result = terminal["broadcast"]["result"]
        previous_generation = service.get_run("run-fixture")["broadcast"]["status_generation"]
        revoke_review_evidence_activation(
            parent_output,
            generation_id=result["review_evidence_generation_id"],
            expected_queue_sha256=result["queue_sha256"],
        )
        repo = service.repo_root
        service.close()

        recovered_service = ApiService(repo)
        try:
            child = recovered_service.get_run(queued["run_id"])
            parent = recovered_service.get_run("run-fixture")

            self.assertEqual("revoked", child["broadcast"]["result"]["status"])
            self.assertEqual("revoked", parent["broadcast"]["review_evidence"]["status"])
            self.assertIn(
                "missing_qualified_selective_review_queue",
                parent["broadcast"]["blocking_reasons"],
            )
            self.assertNotEqual(previous_generation, parent["broadcast"]["status_generation"])
            report = json.loads(
                (
                    parent_output
                    / "broadcast_status"
                    / parent["broadcast"]["status_generation"]
                    / "broadcast_quality_report.json"
                ).read_text(encoding="utf-8")
            )
            self.assertIn("missing_qualified_selective_review_queue", report["blocking_reasons"])
            self.assertEqual(parent["broadcast"]["status"], report["status"])
            for limitation in report["limitations"]:
                self.assertIn(limitation, parent["broadcast"]["limitations"])
        finally:
            recovered_service.close()

    def test_service_restart_finishes_revocation_root_queue_removal(self) -> None:
        draft = self._write_fixture()
        service, parent_output = self._create_service_parent(draft)
        built = build_review_evidence_bundle(self.source, service.review_evidence_inbox_dir / "fixture")
        queued = service.import_broadcast_review_evidence(
            "run-fixture",
            {
                "bundle_id": "review-evidence-fixture",
                "bundle_manifest_sha256": built.bundle_sha256,
            },
        )
        terminal = self._wait_for_terminal(service, queued["run_id"])
        result = terminal["broadcast"]["result"]
        generation_dir = parent_output / "review_evidence" / "generations" / result["review_evidence_generation_id"]
        self._write_json(
            generation_dir / "review_evidence_revocation.v1.json",
            {
                "schema_version": "1.0",
                "artifact_type": "broadcast_review_evidence_revocation",
                "generation_id": result["review_evidence_generation_id"],
                "queue_sha256": result["queue_sha256"],
                "revoked_at": "2026-07-15T22:00:00+00:00",
                "reason": "pre_consumption_revoke",
            },
        )
        root_queue = parent_output / "selective_review_queue.v1.json"
        self.assertTrue(root_queue.is_file())
        repo = service.repo_root
        service.close()

        recovered_service = ApiService(repo)
        try:
            child = recovered_service.get_run(queued["run_id"])
            parent = recovered_service.get_run("run-fixture")

            self.assertFalse(root_queue.exists())
            self.assertEqual("revoked", child["broadcast"]["result"]["status"])
            self.assertEqual("revoked", parent["broadcast"]["review_evidence"]["status"])
            self.assertEqual(
                "missing_qualified_selective_review_queue",
                recovered_service.get_broadcast_review_windows("run-fixture")["reason"],
            )
            draft["bundle_id"] = "review-evidence-replacement"
            self._write_json(self.source / "review_evidence_bundle.draft.json", draft)
            replacement = build_review_evidence_bundle(
                self.source,
                recovered_service.review_evidence_inbox_dir / "replacement",
            )
            replacement_job = recovered_service.import_broadcast_review_evidence(
                "run-fixture",
                {
                    "bundle_id": "review-evidence-replacement",
                    "bundle_manifest_sha256": replacement.bundle_sha256,
                },
            )
            replacement_terminal = self._wait_for_terminal(
                recovered_service,
                replacement_job["run_id"],
            )
            self.assertEqual("completed", replacement_terminal["status"])
            self.assertTrue(root_queue.is_file())
        finally:
            recovered_service.close()

    def test_different_bundle_conflicts_while_an_import_is_active(self) -> None:
        draft = self._write_fixture()
        service, _ = self._create_service_parent(draft)
        try:
            first = build_review_evidence_bundle(self.source, service.review_evidence_inbox_dir / "first")
            draft["bundle_id"] = "review-evidence-second"
            self._write_json(self.source / "review_evidence_bundle.draft.json", draft)
            second = build_review_evidence_bundle(self.source, service.review_evidence_inbox_dir / "second")
            with mock.patch.object(service, "_start_thread_or_cleanup"):
                queued = service.import_broadcast_review_evidence(
                    "run-fixture",
                    {
                        "bundle_id": "review-evidence-fixture",
                        "bundle_manifest_sha256": first.bundle_sha256,
                    },
                )
                replay = service.import_broadcast_review_evidence(
                    "run-fixture",
                    {
                        "bundle_id": "review-evidence-fixture",
                        "bundle_manifest_sha256": first.bundle_sha256,
                    },
                )
                with self.assertRaisesRegex(RuntimeError, "Another run is already active"):
                    service.import_broadcast_review_evidence(
                        "run-fixture",
                        {
                            "bundle_id": "review-evidence-second",
                            "bundle_manifest_sha256": second.bundle_sha256,
                        },
                    )
            self.assertEqual(queued["run_id"], replay["run_id"])
        finally:
            service.close()

    def _create_service_parent(self, draft: dict[str, object]) -> tuple[ApiService, Path]:
        if os.name == "nt":
            self.skipTest(
                "Windows review-evidence config lineage fails closed until a native handle-relative backend exists"
            )
        repo = self.root / "repo"
        for name in ("config", "data", "outputs", "weights"):
            (repo / name).mkdir(parents=True, exist_ok=True)
        service = ApiService(repo)
        parent_output = repo / "outputs" / "runs" / "fixture" / "run-fixture"
        parent_output.mkdir(parents=True)
        config_path = repo / "config" / "fixture.yaml"
        source_path = self.source / "target-application" / "source.mp4"
        source_stat = source_path.stat()
        workflow_metadata = {
            "workflow_id": "workflow-1",
            "accepted_trial_run_id": "trial-accepted",
            "trial_request_sha256": "1" * 64,
            "trial_intent_sha256": "2" * 64,
            "trial_patch_sha256": "3" * 64,
            "patch_sha256": "4" * 64,
            "calibration_digest": "5" * 64,
            "source_signature": {
                "path": str(source_path.resolve()),
                "size_bytes": source_stat.st_size,
                "modified_at": datetime.fromtimestamp(
                    source_stat.st_mtime,
                    tz=timezone.utc,
                ).isoformat(),
            },
        }
        config_path.write_text(
            yaml.safe_dump(
                {
                    "input_video": "fixture.mp4",
                    "metadata": {"production_workflow": workflow_metadata},
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        expected_config_sha256 = sha256_file(config_path)
        source_signature_sha256 = hashlib.sha256(
            json.dumps(
                workflow_metadata["source_signature"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        contract_bytes = (self.source / "target-application" / "root-contract.json").read_bytes()
        (parent_output / "tracking_contract.v2.json").write_bytes(contract_bytes)
        self._write_json(
            parent_output / "action_signal_binding.v1.json",
            {"schema_version": "1.0", "artifact_type": "broadcast_action_signal_binding"},
        )
        registry = service._read_registry()
        registry["runs"].append(
            {
                "run_id": "run-fixture",
                "source": "broadcast_hybrid",
                "status": "completed",
                "created_at": "2026-07-15T00:00:00+00:00",
                "started_at": "2026-07-15T00:00:00+00:00",
                "completed_at": "2026-07-15T00:01:00+00:00",
                "config_name": "fixture.yaml",
                "config_path": str(config_path),
                "input_video": str(source_path),
                "parent_run_id": None,
                "output_dir": str(parent_output),
                "modules_enabled": {"broadcast_hybrid": True},
                "artifacts": [],
                "stats": {},
                "broadcast": {
                    "status": "needs_review",
                    "quality_profile": "stable_broadcast",
                    "max_manual_review_windows": 1,
                    "preflight": {"fixture": True},
                    "blocking_reasons": [
                        "missing_qualified_selective_review_queue",
                        "existing_non_evidence_blocker",
                    ],
                    "limitations": ["existing_limitation"],
                    "terminal_tail_acknowledged": True,
                },
                "progress": None,
                "notes": json.dumps(
                    {
                        "schema_version": "1.0",
                        "purpose": "production_full",
                        "workflow_id": workflow_metadata["workflow_id"],
                        "confirmed_config_name": "fixture.yaml",
                        "expected_config_sha256": expected_config_sha256,
                        "calibration_digest": workflow_metadata["calibration_digest"],
                        "source_signature_sha256": source_signature_sha256,
                        "accepted_trial_run_id": workflow_metadata["accepted_trial_run_id"],
                        "trial_request_sha256": workflow_metadata["trial_request_sha256"],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "error": None,
            }
        )
        service._write_registry(registry)
        draft["target"] = service._review_evidence_target(
            "run-fixture",
            parent_output,
            parent=registry["runs"][-1],
        )
        self._write_json(self.source / "review_evidence_bundle.draft.json", draft)
        self.assertEqual(
            draft["target"]["root_contract_sha256"], sha256_file(parent_output / "tracking_contract.v2.json")
        )
        return service, parent_output

    def _reconfirm_crlf_config_lineage(self, service: ApiService) -> None:
        parent = service.get_run("run-fixture")
        config_path = Path(parent["config_path"])
        config_path.write_bytes(config_path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        registry = service._read_registry()
        target = next(item for item in registry["runs"] if item["run_id"] == "run-fixture")
        target["broadcast"]["submission_id"] = "submission-completed"
        target["broadcast"]["generation_id"] = "generation-completed"
        accepted_trial = {
            **json.loads(json.dumps(target)),
            "run_id": "trial-accepted",
            "source": "tracking",
            "status": "completed",
            "parent_run_id": None,
            "output_dir": str(self.root / "trial-accepted"),
            "notes": json.dumps(
                {
                    "purpose": "trial",
                    "workflow_id": "workflow-1",
                    "trial_intent_sha256": "2" * 64,
                    "calibration_digest": "5" * 64,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            "broadcast": {},
        }
        failed = {
            **json.loads(json.dumps(target)),
            "run_id": "run-failed",
            "status": "failed",
            "output_dir": str(self.root / "run-failed"),
            "broadcast": {
                **target["broadcast"],
                "submission_id": "submission-failed",
                "generation_id": "generation-failed",
            },
        }
        registry["runs"].extend([accepted_trial, failed])
        service._write_registry(registry)

        current_registry = service._read_registry()
        current_parent = next(item for item in current_registry["runs"] if item["run_id"] == "run-fixture")
        challenge = service._broadcast_config_lineage_reconfirmation_challenge(
            current_parent,
            registry=current_registry,
        )
        response = service.reconfirm_broadcast_config_lineage(
            "run-fixture",
            {
                **challenge,
                "operator_id": "operator-1",
                "reviewer_id": "reviewer-1",
            },
        )
        self.assertEqual("reconfirmed", response["status"])

    def _call_in_forked_process(
        self,
        callback: Callable[[], object],
        *,
        label: str,
        timeout: float = 5.0,
    ) -> object:
        if os.name == "nt":
            self.skipTest("fork-based deadlock regression coverage requires POSIX")
        context = multiprocessing.get_context("fork")
        receiver, sender = context.Pipe(duplex=False)

        def invoke() -> None:
            try:
                sender.send(("ok", callback()))
            except BaseException as exc:
                sender.send(("error", f"{type(exc).__name__}: {exc}"))
            finally:
                sender.close()

        process = context.Process(target=invoke, name=f"review-evidence-{label.replace(' ', '-')}")
        process.start()
        sender.close()
        process.join(timeout)
        if process.is_alive():
            process.terminate()
            process.join(1.0)
            if process.is_alive():
                process.kill()
                process.join(1.0)
            receiver.close()
            self.fail(f"{label} did not complete within {timeout:.1f} seconds")
        try:
            if not receiver.poll():
                self.fail(f"{label} exited without returning a result (exit code {process.exitcode})")
            status, payload = receiver.recv()
        finally:
            receiver.close()
        if status != "ok":
            self.fail(f"{label} failed in child process: {payload}")
        self.assertEqual(0, process.exitcode)
        return payload

    def _assert_restart_blocks_committed_import_for_lineage_failure(
        self,
        *,
        orphaned_status: str,
        config_failure: str,
        expected_code: str,
        expected_recovery_action: str,
        assert_unknown_runtime: bool = False,
    ) -> None:
        draft = self._write_fixture()
        service, parent_output = self._create_service_parent(draft)
        built = build_review_evidence_bundle(self.source, service.review_evidence_inbox_dir / "fixture")
        queued = service.import_broadcast_review_evidence(
            "run-fixture",
            {
                "bundle_id": "review-evidence-fixture",
                "bundle_manifest_sha256": built.bundle_sha256,
            },
        )
        terminal = self._wait_for_terminal(service, queued["run_id"])
        self.assertEqual("completed", terminal["status"], terminal.get("error"))

        queue_path = parent_output / "selective_review_queue.v1.json"
        generations_root = parent_output / "review_evidence" / "generations"
        queue_bytes = queue_path.read_bytes()
        generation_bytes = {
            path.relative_to(generations_root).as_posix(): path.read_bytes()
            for path in generations_root.rglob("*")
            if path.is_file()
        }
        self.assertTrue(generation_bytes)
        with service._lock, service._registry_transaction() as registry:
            child = next(item for item in registry["runs"] if item.get("run_id") == queued["run_id"])
            child["status"] = orphaned_status
            child["error"] = "injected registry interruption after root commit"
            child["broadcast"]["operation_status"] = orphaned_status
            if config_failure == "reconfirmation_required":
                parent = next(item for item in registry["runs"] if item.get("run_id") == "run-fixture")
                parent["broadcast"]["submission_id"] = "submission-completed"
                parent["broadcast"]["generation_id"] = "generation-completed"
                accepted_trial = {
                    **json.loads(json.dumps(parent)),
                    "run_id": "trial-accepted",
                    "source": "tracking",
                    "status": "completed",
                    "parent_run_id": None,
                    "output_dir": str(self.root / "trial-accepted"),
                    "notes": json.dumps(
                        {
                            "purpose": "trial",
                            "workflow_id": "workflow-1",
                            "trial_intent_sha256": "2" * 64,
                            "calibration_digest": "5" * 64,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "broadcast": {},
                }
                failed = {
                    **json.loads(json.dumps(parent)),
                    "run_id": "run-failed",
                    "status": "failed",
                    "output_dir": str(self.root / "run-failed"),
                    "broadcast": {
                        **parent["broadcast"],
                        "submission_id": "submission-failed",
                        "generation_id": "generation-failed",
                    },
                }
                registry["runs"].extend([accepted_trial, failed])

        config_path = Path(service.get_run("run-fixture")["config_path"])
        if config_failure == "mismatch":
            config_path.write_text("input_video: changed-after-root-commit.mp4\n", encoding="utf-8")
        elif config_failure == "reconfirmation_required":
            config_path.write_bytes(config_path.read_bytes().replace(b"\n", b"\r\n"))
        else:
            self.fail(f"unsupported config failure: {config_failure}")
        repo = service.repo_root
        service.close()

        recovered_service = ApiService(repo)
        try:
            recovered = recovered_service.get_run(queued["run_id"])
            parent = recovered_service.get_run("run-fixture")
            state = recovered_service.get_broadcast_review_evidence("run-fixture")
            windows = recovered_service.get_broadcast_review_windows("run-fixture")

            self.assertEqual("failed", recovered["status"])
            self.assertEqual("blocked", recovered["broadcast"]["operation_status"])
            self.assertEqual(expected_code, recovered["broadcast"]["blocker_code"])
            self.assertEqual(expected_code, recovered["broadcast"]["error_code"])
            self.assertEqual(
                expected_recovery_action,
                recovered["broadcast"]["recovery_action"],
            )
            self.assertEqual("needs_review", parent["broadcast"]["status"])
            self.assertIn(expected_code, parent["broadcast"]["blocking_reasons"])
            self.assertEqual("blocked", parent["broadcast"]["review_evidence"]["status"])
            self.assertEqual(
                expected_code,
                parent["broadcast"]["review_evidence"]["blocker_code"],
            )
            self.assertEqual(
                expected_recovery_action,
                parent["broadcast"]["review_evidence"]["recovery_action"],
            )
            self.assertEqual("blocked", state["status"])
            self.assertEqual(expected_code, state["blocker_code"])
            self.assertEqual(expected_recovery_action, state["recovery_action"])
            self.assertEqual("needs_review", windows["status"])
            self.assertEqual("invalid_or_stale_selective_review_evidence", windows["reason"])
            self.assertEqual(queue_bytes, queue_path.read_bytes())
            self.assertEqual(
                generation_bytes,
                {
                    path.relative_to(generations_root).as_posix(): path.read_bytes()
                    for path in generations_root.rglob("*")
                    if path.is_file()
                },
            )
        finally:
            recovered_service.close()

        if assert_unknown_runtime:
            with (
                mock.patch.object(
                    ApiService,
                    "_validate_current_review_queue_locked",
                    side_effect=RuntimeError("injected unexpected recovery bug"),
                ),
                self.assertRaisesRegex(RuntimeError, "injected unexpected recovery bug"),
            ):
                ApiService(repo)

    def _assert_activated_review_consumers_reject_target_mutation(self, mutation: str) -> None:
        draft = self._write_fixture()
        service, parent_output = self._create_service_parent(draft)
        try:
            built = build_review_evidence_bundle(self.source, service.review_evidence_inbox_dir / "fixture")
            queued = service.import_broadcast_review_evidence(
                "run-fixture",
                {
                    "bundle_id": "review-evidence-fixture",
                    "bundle_manifest_sha256": built.bundle_sha256,
                },
            )
            terminal = self._wait_for_terminal(service, queued["run_id"])
            self.assertEqual("completed", terminal["status"])
            ready = service.get_broadcast_review_windows("run-fixture")
            self.assertEqual("ready", ready["status"])

            if mutation == "config":
                Path(service.get_run("run-fixture")["config_path"]).write_text(
                    "input_video: changed-after-activation.mp4\n",
                    encoding="utf-8",
                )
            elif mutation == "root_contract":
                contract_path = parent_output / "tracking_contract.v2.json"
                contract = json.loads(contract_path.read_text(encoding="utf-8"))
                contract["candidates"][0]["confidence"] = 0.7
                self._write_json(contract_path, contract)
            elif mutation == "action_binding":
                binding_path = parent_output / "action_signal_binding.v1.json"
                binding = json.loads(binding_path.read_text(encoding="utf-8"))
                binding["changed_after_activation"] = True
                self._write_json(binding_path, binding)
            else:
                self.fail(f"unsupported target mutation: {mutation}")

            blocked = service.get_broadcast_review_windows("run-fixture")
            self.assertEqual("needs_review", blocked["status"])
            self.assertEqual("invalid_or_stale_selective_review_evidence", blocked["reason"])
            with (
                mock.patch.object(service, "get_broadcast_review_windows", return_value=ready),
                self.assertRaisesRegex(RuntimeError, "changed|current run context"),
            ):
                service.submit_broadcast_review_actions(
                    "run-fixture",
                    {"queue_sha256": ready["queue_sha256"], "actions": []},
                )
            self.assertFalse((parent_output / "review_decisions.json").exists())
        finally:
            service.close()

    def _wait_for_terminal(self, service: ApiService, run_id: str) -> dict[str, object]:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            run = service.get_run(run_id)
            if run.get("status") in {"completed", "failed", "cancelled"}:
                return run
            time.sleep(0.01)
        self.fail(f"run did not become terminal: {run_id}")

    def _configure_linked_adjudication_queue(
        self,
        draft: dict[str, object],
        package_name: str,
        *,
        linked_name: str = ADJUDICATION_QUEUE_NAME,
        queue_overrides: dict[str, object] | None = None,
    ) -> Path:
        descriptor = draft["packages"][package_name]
        annotation_relative = PurePosixPath(descriptor["annotation_resolution_path"])
        annotation_path = self.source / annotation_relative
        candidates = [
            {
                "candidate_id": f"{package_name}-candidate",
                "reason": "conflicting_votes",
            }
        ]
        annotation = json.loads(annotation_path.read_text(encoding="utf-8"))
        annotation["adjudication_queue"] = candidates
        annotation["linked_artifacts"] = {
            "adjudication_queue": linked_name,
            "derived_tracking_contract": PurePosixPath(descriptor["resolved_contract_path"]).name,
        }
        self._write_json(annotation_path, annotation)
        descriptor["annotation_resolution_sha256"] = sha256_file(annotation_path)
        if package_name == "policy_qualification":
            queue_path = self.source / "selective_review_queue.v1.json"
            review_queue = json.loads(queue_path.read_text(encoding="utf-8"))
            review_queue["bindings"]["annotation_resolution"]["sha256"] = sha256_file(annotation_path)
            self._write_json(queue_path, review_queue)
            draft["queue"]["sha256"] = sha256_file(queue_path)

        adjudication_path = annotation_path.parent / linked_name
        payload: dict[str, object] = {
            "schema_version": "1.0",
            "artifact_type": "candidate_annotation_adjudication_queue",
            "source_resolution": annotation_relative.name,
            "candidate_count": len(candidates),
            "candidates": candidates,
        }
        if queue_overrides is not None:
            payload.update(queue_overrides)
        self._write_json(adjudication_path, payload)
        self._write_json(
            self.source / "review_evidence_bundle.draft.json",
            draft,
        )
        return adjudication_path

    @staticmethod
    def _refresh_published_file_binding(
        manifest: dict[str, object],
        root: Path,
        relative: str,
        *,
        descriptor: dict[str, object] | None = None,
        sha_field: str | None = None,
    ) -> None:
        path = root / relative
        digest = sha256_file(path)
        if descriptor is not None and sha_field is not None:
            descriptor[sha_field] = digest
        for row in manifest["inventory"]:
            if row["path"] == relative:
                row["sha256"] = digest
                row["size_bytes"] = path.stat().st_size
                return
        raise AssertionError(f"missing inventory row: {relative}")

    def _refresh_published_annotation_queue_binding(
        self,
        manifest: dict[str, object],
        root: Path,
        annotation_relative: str,
    ) -> None:
        queue_relative = manifest["queue"]["path"]
        queue_path = root / queue_relative
        review_queue = json.loads(queue_path.read_text(encoding="utf-8"))
        review_queue["bindings"]["annotation_resolution"]["sha256"] = sha256_file(root / annotation_relative)
        self._write_json(queue_path, review_queue)
        self._refresh_published_file_binding(
            manifest,
            root,
            queue_relative,
            descriptor=manifest["queue"],
            sha_field="sha256",
        )

    def _write_fixture(self) -> dict[str, object]:
        source_sha = hashlib.sha256(b"fixture-video").hexdigest()
        candidate = {
            "candidate_id": "candidate-1",
            "frame_index": 10,
            "bbox": [10.0, 10.0, 20.0, 20.0],
            "confidence": 0.8,
            "source": "detector",
        }
        root_contract = (
            json.dumps(
                build_tracking_contract(
                    source={
                        "video_sha256": source_sha,
                        "fps": 25.0,
                        "width": 100,
                        "height": 100,
                        "frame_count": 200,
                    },
                    candidates=[candidate],
                ),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        files: dict[str, bytes] = {
            "model-development/model.json": b'{"artifact_type":"candidate_classifier_model"}\n',
            "model-development/development-dataset.json": (
                b'{"artifact_type":"candidate_dataset","purpose":"model_development","samples":[]}\n'
            ),
            "model-development/training.json": b"{}\n",
            "model-development/model.pt": b"weights",
            "model-development/source-contract.json": root_contract,
            "model-development/votes.jsonl": b"{}\n",
            "model-development/annotation.json": b"{}\n",
            "model-development/resolved-contract.json": root_contract,
            "policy-qualification/qualification-dataset.json": (
                b'{"artifact_type":"candidate_dataset","purpose":"policy_qualification","samples":[]}\n'
            ),
            "policy-qualification/policy.json": json.dumps(
                {
                    "artifact_type": "selective_policy",
                    "status": "qualified",
                    "version_inputs": {
                        "qualification": {
                            "qualified": True,
                            "policy_status": "qualified",
                            "acceptance_status": "qualified",
                            "calibration_certified": True,
                            "audit_qualified": True,
                        }
                    },
                },
                sort_keys=True,
            ).encode("utf-8")
            + b"\n",
            "policy-qualification/annotation.json": b"{}\n",
            "policy-qualification/resolved-contract.json": b"{}\n",
            "policy-qualification/roles.json": b"{}\n",
            "policy-qualification/source-contract.json": root_contract,
            "policy-qualification/votes.jsonl": b"{}\n",
            "policy-qualification/predictions.json": b"{}\n",
            "policy-qualification/decisions.json": b"{}\n",
            "target-application/dataset.json": b'{"artifact_type":"candidate_dataset","samples":[]}\n',
            "target-application/predictions.json": b"{}\n",
            "target-application/decisions.json": b"{}\n",
            "target-application/source.mp4": b"fixture-video",
            "target-application/root-contract.json": root_contract,
            "target-application/timing.json": b"{}\n",
            "target-application/evidence/tight.bin": b"tight",
            "target-application/evidence/context.bin": b"context",
            "target-application/evidence/montage.jpg": b"montage",
        }
        for relative, content in files.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self._write_json(
            self.source / "model-development/model.json",
            {
                "schema_version": "1.0",
                "artifact_type": "candidate_classifier_model",
                "weights_path": "model.pt",
                "weights_sha256": sha256_file(self.source / "model-development/model.pt"),
                "training_report_path": "training.json",
                "training_report_sha256": sha256_file(self.source / "model-development/training.json"),
            },
        )
        for relative in (
            "model-development/annotation.json",
            "policy-qualification/annotation.json",
        ):
            self._write_json(
                self.source / relative,
                {
                    "schema_version": "1.0",
                    "artifact_type": "candidate_annotation_resolution",
                },
            )

        artifacts = {
            "tight_tensor": self._artifact_descriptor("target-application/evidence/tight.bin"),
            "context_tensor": self._artifact_descriptor("target-application/evidence/context.bin"),
            "review_montage": self._artifact_descriptor("target-application/evidence/montage.jpg"),
        }
        dataset_version = "d" * 64
        sample = {
            "sample_id": "sample-1",
            "candidate_id": "candidate-1",
            "artifacts": artifacts,
        }
        dataset = {
            "schema_version": "1.0",
            "artifact_type": "candidate_dataset",
            "dataset_version": dataset_version,
            "summary": {"status": "ok", "sample_count": 1, "source_count": 1},
            "sources": [
                {
                    "path": "source.mp4",
                    "sha256": source_sha,
                    "variant_id": "target-variant",
                    "width": 100,
                    "height": 100,
                    "frame_count": 200,
                    "fps": 25.0,
                    "group_id": "target-group",
                    "split_group": "target-split",
                    "temporal_group": "target-temporal",
                    "candidate_ids": ["candidate-1"],
                }
            ],
            "samples": [sample],
        }
        self._write_json(self.source / "target-application/dataset.json", dataset)
        candidate_fingerprint = self._fixture_candidate_fingerprint()
        self._write_json(
            self.source / "target-application/predictions.json",
            {"predictions": [{"candidate_id": "candidate-1", "candidate_fingerprint": candidate_fingerprint}]},
        )
        self._write_json(
            self.source / "target-application/decisions.json",
            {
                "decisions": [
                    {
                        "candidate_id": "candidate-1",
                        "candidate_fingerprint": candidate_fingerprint,
                        "decision": "abstain",
                        "existing_decision_preserved": False,
                    }
                ]
            },
        )
        for package_name, prefix in (
            ("model-development/development-dataset.json", "development"),
            ("policy-qualification/qualification-dataset.json", "qualification"),
        ):
            dataset_path = self.source / package_name
            source_path = dataset_path.parent / f"{prefix}-source.mp4"
            source_path.write_bytes(f"{prefix}-video".encode())
            self._write_json(
                dataset_path,
                {
                    "artifact_type": "candidate_dataset",
                    "samples": [{"candidate_id": f"{prefix}-candidate"}],
                    "sources": [
                        {
                            "path": source_path.name,
                            "variant_id": f"{prefix}-variant",
                            "sha256": sha256_file(source_path),
                            "group_id": f"{prefix}-group",
                            "split_group": f"{prefix}-split",
                            "temporal_group": f"{prefix}-temporal",
                        }
                    ],
                },
            )
        bindings = {
            "review_timing": "target-application/timing.json",
            "policy": "policy-qualification/policy.json",
            "decisions": "target-application/decisions.json",
            "model": "model-development/model.json",
            "training_report": "model-development/training.json",
            "model_weights": "model-development/model.pt",
            "dataset": "target-application/dataset.json",
            "predictions": "target-application/predictions.json",
            "contract": "target-application/root-contract.json",
            "annotation_resolution": "policy-qualification/annotation.json",
            "resolved_tracking_contract": "policy-qualification/resolved-contract.json",
            "policy_roles": "policy-qualification/roles.json",
            "qualification_dataset": "policy-qualification/qualification-dataset.json",
            "qualification_predictions": "policy-qualification/predictions.json",
            "qualification_decisions": "policy-qualification/decisions.json",
        }
        queue = {
            "schema_version": "1.0",
            "artifact_type": "selective_review_queue",
            "max_windows": 1,
            "review_item_count": 1,
            "candidate_count": 1,
            "selection": {
                "eligible": 1,
                "selected": 1,
                "dropped": 0,
                "dropped_candidate_ids": [],
                "coverage_complete": True,
                "requires_additional_round": False,
            },
            "bindings": {
                name: {"path": relative, "sha256": sha256_file(self.source / relative)}
                for name, relative in bindings.items()
            },
            "items": [
                {
                    "review_item_id": "window-1",
                    "candidates": [
                        {
                            "candidate_id": "candidate-1",
                            "candidate_fingerprint": candidate_fingerprint,
                            "evidence": {
                                "sample_id": "sample-1",
                                "dataset_version": dataset_version,
                                "sha256": sample_evidence_sha256(sample),
                                "artifacts": artifacts,
                            },
                        }
                    ],
                }
            ],
        }
        queue_path = self.source / "selective_review_queue.v1.json"
        self._write_json(queue_path, queue)
        contract_sha = sha256_file(self.source / "target-application/root-contract.json")
        draft: dict[str, object] = {
            "bundle_id": "review-evidence-fixture",
            "target": {
                "run_id": "run-fixture",
                "source_sha256": source_sha,
                "root_contract_sha256": contract_sha,
                "max_review_windows": 1,
                "max_manual_review_windows": 1,
                "action_signal_binding_sha256": "a" * 64,
                "confirmed_config_sha256": "b" * 64,
                "profile_digest": "c" * 64,
                "quality_profile": "stable_broadcast",
                "provisioner_version": "review-evidence-provisioner-v2",
                "candidate_population_sha256": self._fixture_population_sha256(),
                "candidate_population_count": 1,
            },
            "provisioning": {
                "attempt_quota_bytes": 1024 * 1024,
                "retention": {
                    "policy": "manual-audit-retention-v1",
                    "retain_until": "2030-01-01T00:00:00+00:00",
                    "automatic_delete": False,
                },
            },
            "packages": {
                "model_development": {
                    "root": "model-development",
                    "manifest_path": "model-development/model.json",
                    "dataset_path": "model-development/development-dataset.json",
                    "dataset_sha256": sha256_file(self.source / "model-development/development-dataset.json"),
                    "source_contract_path": "model-development/source-contract.json",
                    "source_contract_sha256": sha256_file(self.source / "model-development/source-contract.json"),
                    "vote_ledger_path": "model-development/votes.jsonl",
                    "vote_ledger_sha256": sha256_file(self.source / "model-development/votes.jsonl"),
                    "annotation_resolution_path": "model-development/annotation.json",
                    "annotation_resolution_sha256": sha256_file(self.source / "model-development/annotation.json"),
                    "resolved_contract_path": "model-development/resolved-contract.json",
                    "resolved_contract_sha256": sha256_file(self.source / "model-development/resolved-contract.json"),
                },
                "policy_qualification": {
                    "root": "policy-qualification",
                    "manifest_path": "policy-qualification/policy.json",
                    "dataset_path": "policy-qualification/qualification-dataset.json",
                    "dataset_sha256": sha256_file(self.source / "policy-qualification/qualification-dataset.json"),
                    "policy_path": "policy-qualification/policy.json",
                    "policy_sha256": sha256_file(self.source / "policy-qualification/policy.json"),
                    "source_contract_path": "policy-qualification/source-contract.json",
                    "source_contract_sha256": sha256_file(self.source / "policy-qualification/source-contract.json"),
                    "vote_ledger_path": "policy-qualification/votes.jsonl",
                    "vote_ledger_sha256": sha256_file(self.source / "policy-qualification/votes.jsonl"),
                    "annotation_resolution_path": "policy-qualification/annotation.json",
                    "annotation_resolution_sha256": sha256_file(self.source / "policy-qualification/annotation.json"),
                    "resolved_contract_path": "policy-qualification/resolved-contract.json",
                    "resolved_contract_sha256": sha256_file(
                        self.source / "policy-qualification/resolved-contract.json"
                    ),
                    "predictions_path": "policy-qualification/predictions.json",
                    "predictions_sha256": sha256_file(self.source / "policy-qualification/predictions.json"),
                    "decisions_path": "policy-qualification/decisions.json",
                    "decisions_sha256": sha256_file(self.source / "policy-qualification/decisions.json"),
                    "policy_roles_path": "policy-qualification/roles.json",
                    "policy_roles_sha256": sha256_file(self.source / "policy-qualification/roles.json"),
                },
                "target_application": {
                    "root": "target-application",
                    "manifest_path": "target-application/dataset.json",
                    "dataset_path": "target-application/dataset.json",
                    "dataset_sha256": sha256_file(self.source / "target-application/dataset.json"),
                    "predictions_path": "target-application/predictions.json",
                    "predictions_sha256": sha256_file(self.source / "target-application/predictions.json"),
                    "decisions_path": "target-application/decisions.json",
                    "decisions_sha256": sha256_file(self.source / "target-application/decisions.json"),
                    "source_path": "target-application/source.mp4",
                    "source_sha256": source_sha,
                    "root_contract_path": "target-application/root-contract.json",
                    "root_contract_sha256": contract_sha,
                },
            },
            "queue": {"path": "selective_review_queue.v1.json", "sha256": sha256_file(queue_path)},
        }
        self._write_json(self.source / "review_evidence_bundle.draft.json", draft)
        return draft

    @staticmethod
    def _fixture_candidate_fingerprint() -> str:
        identity = {
            "candidate_id": "candidate-1",
            "frame_index": 10,
            "bbox": [10.0, 10.0, 20.0, 20.0],
            "detector_source": "detector",
            "confidence": 0.8,
        }
        return hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()

    @classmethod
    def _fixture_population_sha256(cls) -> str:
        population = [
            {
                "candidate_id": "candidate-1",
                "candidate_fingerprint": cls._fixture_candidate_fingerprint(),
            }
        ]
        return hashlib.sha256(
            json.dumps(population, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _artifact_descriptor(self, relative: str) -> dict[str, object]:
        path = self.source / relative
        return {
            "path": Path(relative).relative_to("target-application").as_posix(),
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
        }


if __name__ == "__main__":
    unittest.main()
