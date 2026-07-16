from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from football_tracking.api.broadcast_api import BroadcastApiError
from football_tracking.api.dependencies import get_service
from football_tracking.api.routes.broadcast import router as broadcast_router
from football_tracking.api.schemas import BroadcastReviewEvidenceImportRequest
from football_tracking.api.service import ApiService
from football_tracking.candidate_annotations import sample_evidence_sha256
from football_tracking.review_evidence_bundle import (
    BUNDLE_MANIFEST_NAME,
    ReviewEvidenceBundleError,
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
            self.assertEqual("confirmed_config_changed_after_confirmation", state["blocker_code"])
            self.assertEqual("confirmed_config_changed_after_confirmation", state["error_code"])
            self.assertEqual(
                ["confirmed_config_changed_after_confirmation"],
                state["blocking_reasons"],
            )
            self.assertEqual("config changed after confirmation", state["message"])
            self.assertEqual([], state["bundles"])
            with self.assertRaisesRegex(RuntimeError, "config changed after confirmation"):
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
            self.assertEqual("config changed after confirmation", rejected.json()["detail"])
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
        repo = self.root / "repo"
        for name in ("config", "data", "outputs", "weights"):
            (repo / name).mkdir(parents=True, exist_ok=True)
        service = ApiService(repo)
        parent_output = repo / "outputs" / "runs" / "fixture" / "run-fixture"
        parent_output.mkdir(parents=True)
        config_path = repo / "config" / "fixture.yaml"
        config_path.write_text("input_video: fixture.mp4\n", encoding="utf-8")
        expected_config_sha256 = sha256_file(config_path)
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
                "input_video": str(self.source / "target-application" / "source.mp4"),
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
                        "confirmed_config_name": "fixture.yaml",
                        "expected_config_sha256": expected_config_sha256,
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
            self._write_json(
                self.source / package_name,
                {
                    "artifact_type": "candidate_dataset",
                    "samples": [{"candidate_id": f"{prefix}-candidate"}],
                    "sources": [
                        {
                            "variant_id": f"{prefix}-variant",
                            "sha256": hashlib.sha256(f"{prefix}-video".encode()).hexdigest(),
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
