from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_candidate_classifier import _write_sample, _write_training_inputs
from test_selective_policy import _write_inputs

from football_tracking.api.dependencies import get_service
from football_tracking.api.routes.broadcast import router as broadcast_router
from football_tracking.api.service import ApiService
from football_tracking.broadcast_hybrid_orchestration import (
    preflight_recompute_reviewed_trajectory,
    recompute_reviewed_trajectory,
)
from football_tracking.candidate_annotations import (
    ANNOTATION_RESOLUTION_NAME,
    resolve_candidate_annotations,
    sample_evidence_sha256,
)
from football_tracking.candidate_classifier import (
    MODEL_MANIFEST_NAME,
    PREDICTIONS_NAME,
    TrainingConfig,
    classify_candidates,
    train_candidate_classifier,
)
from football_tracking.detector_candidate_contract import assign_candidate_ids, candidate_to_contract_record
from football_tracking.review_evidence_bundle import build_review_evidence_bundle, sha256_file
from football_tracking.selective_policy import (
    SELECTIVE_APPLICATION_NAME,
    SELECTIVE_DECISIONS_NAME,
    SELECTIVE_POLICY_NAME,
    SELECTIVE_POLICY_ROLES_NAME,
    SelectivePolicyConfig,
    SelectivePolicyError,
    apply_frozen_selective_policy,
    build_selective_policy_roles,
    fit_selective_policy,
    validate_selective_policy_application_binding,
    validate_selective_policy_evidence_binding,
)
from football_tracking.selective_review import (
    MATERIALIZATION_REPORT_NAME,
    REVIEW_QUEUE_NAME,
    build_selective_review_queue,
    materialize_selective_review_actions,
)
from football_tracking.tracking_contracts import TRACKING_CONTRACT_REPORT_NAME, build_tracking_contract
from football_tracking.types import Candidate


class ReviewEvidenceNativeIntegrationTests(unittest.TestCase):
    """Exercise the real three-population evidence chain without validator mocks."""

    def test_native_multi_video_chain_builds_activates_serves_and_materializes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "source"
            source.mkdir()

            development = self._build_model_development(source / "model-development")
            qualification = self._build_policy_qualification(
                source / "policy-qualification",
                development,
            )
            target = self._build_target_application(
                source / "target-application",
                development,
                qualification,
            )
            queue_dir = target["root"] / "queue-source"
            queue = build_selective_review_queue(
                target["dataset"],
                target["predictions"],
                qualification["policy"],
                development["model_manifest"],
                target["contract"],
                queue_dir,
                decisions_path=target["decisions"],
                annotation_resolution_path=qualification["annotation_resolution"],
                resolved_contract_path=qualification["resolved_contract"],
                policy_roles_path=qualification["policy_roles"],
                qualification_dataset_manifest_path=qualification["dataset"],
                qualification_predictions_path=qualification["predictions"],
                qualification_decisions_path=qualification["qualification_decisions"],
                max_windows=1,
            )
            self.assertEqual(1, queue["candidate_count"])
            self.assertEqual(1, queue["review_item_count"])

            application_validation = validate_selective_policy_application_binding(
                qualification["policy"],
                target["decisions"],
                target["predictions"],
                target["dataset"],
                target["contract"],
                development["model_manifest"],
            )
            draft = self._write_bundle_draft(
                source,
                development,
                qualification,
                target,
                queue_dir / REVIEW_QUEUE_NAME,
                application_validation,
            )
            service, parent_output = self._create_service_parent(root / "repo", source, target, draft)
            try:
                built = build_review_evidence_bundle(
                    source,
                    service.review_evidence_inbox_dir / "native-multi-video",
                )
                queued = service.import_broadcast_review_evidence(
                    "run-native-e2e",
                    {
                        "bundle_id": draft["bundle_id"],
                        "bundle_manifest_sha256": built.bundle_sha256,
                    },
                )
                terminal = self._wait_for_terminal(service, queued["run_id"])
                self.assertEqual("completed", terminal["status"])

                app = FastAPI()
                app.include_router(broadcast_router)
                app.dependency_overrides[get_service] = lambda: service
                client = TestClient(app)
                windows_response = client.get("/runs/run-native-e2e/broadcast/review-windows")
                self.assertEqual(200, windows_response.status_code)
                windows = windows_response.json()
                self.assertEqual("ready", windows["status"])
                self.assertEqual(1, windows["review_item_count"])
                review_item = windows["items"][0]
                candidate = review_item["candidates"][0]
                action_response = client.post(
                    "/runs/run-native-e2e/broadcast/review-actions",
                    json={
                        "actions": [
                            {
                                "action_id": "native-e2e-confirm",
                                "review_item_id": review_item["review_item_id"],
                                "candidate_id": candidate["candidate_id"],
                                "reviewer_id": "native-e2e-reviewer",
                                "created_at": "2026-07-15T12:00:00Z",
                                "action": "confirm_ball",
                                "noise_subtype": None,
                            }
                        ]
                    },
                )
                self.assertEqual(200, action_response.status_code, action_response.text)

                active_queue_path = parent_output / REVIEW_QUEUE_NAME
                active_queue = _read_json(active_queue_path)

                def bound(name: str) -> Path:
                    return parent_output / active_queue["bindings"][name]["path"]

                materialized = materialize_selective_review_actions(
                    active_queue_path,
                    parent_output / "review_decisions.json",
                    bound("dataset"),
                    bound("predictions"),
                    bound("policy"),
                    bound("model"),
                    bound("contract"),
                    parent_output / "native-materialized",
                    decisions_path=bound("decisions"),
                    annotation_resolution_path=bound("annotation_resolution"),
                    resolved_contract_path=bound("resolved_tracking_contract"),
                    policy_roles_path=bound("policy_roles"),
                    qualification_dataset_manifest_path=bound("qualification_dataset"),
                    qualification_predictions_path=bound("qualification_predictions"),
                    qualification_decisions_path=bound("qualification_decisions"),
                )
                self.assertEqual("complete", materialized["status"])
                self.assertFalse(materialized["training_invoked"])

                review_decisions_path = parent_output / "review_decisions.json"
                review_decisions_sha256 = sha256_file(review_decisions_path)
                action_envelope = _read_json(review_decisions_path)
                self.assertEqual(
                    windows["queue_sha256"],
                    action_envelope["actions"][0]["bindings"]["queue_sha256"],
                )
                activation_generation_id = active_queue["activation"]["generation_id"]
                self.assertEqual(
                    terminal["broadcast"]["result"]["review_evidence_generation_id"],
                    activation_generation_id,
                )
                frozen = preflight_recompute_reviewed_trajectory(parent_output)
                self.assertEqual(windows["queue_sha256"], frozen["queue_sha256"])
                self.assertEqual(review_decisions_sha256, frozen["review_decisions_sha256"])

                recomputed = recompute_reviewed_trajectory(parent_output, batch_size=1)
                self.assertEqual("completed", recomputed["status"])
                orchestration = recomputed["orchestration_report"]
                self.assertEqual(
                    {
                        "review": recomputed["review_generation_id"],
                        "classification": recomputed["classification_generation_id"],
                        "trajectory": recomputed["trajectory_generation_id"],
                    },
                    orchestration["generation_ids"],
                )
                self.assertEqual(
                    windows["queue_sha256"],
                    orchestration["bindings"]["queue"]["sha256"],
                )
                self.assertEqual(
                    review_decisions_sha256,
                    orchestration["bindings"]["review_decisions"]["sha256"],
                )
                review_generation = (
                    parent_output
                    / "broadcast_generations"
                    / recomputed["review_generation_id"]
                    / MATERIALIZATION_REPORT_NAME
                )
                recompute_materialization = _read_json(review_generation)
                self.assertEqual("complete", recompute_materialization["status"])
                self.assertFalse(recompute_materialization["training_invoked"])
                self.assertEqual(
                    windows["queue_sha256"],
                    recompute_materialization["bindings"]["queue"]["sha256"],
                )
                self.assertEqual(
                    review_decisions_sha256,
                    recompute_materialization["bindings"]["actions"]["sha256"],
                )
            finally:
                service.close()

    def _build_model_development(self, root: Path) -> dict[str, Path]:
        root.mkdir()
        inputs = _write_training_inputs(root)
        original_resolution = _read_json(inputs["resolution"])
        labels = {
            row["candidate_id"]: row["label"]
            for row in original_resolution["resolutions"]
            if row["candidate_id"].startswith(("g0-", "g1-", "g2-"))
            and row["label"] in {"match_ball", "equipment_or_background"}
        }
        selected_ids = set(labels)
        candidates = [row for row in inputs["candidates"] if row["candidate_id"] in selected_ids]
        source_contract = root / "source-contract.json"
        _write_json(source_contract, build_tracking_contract(candidates=candidates))

        dataset = _read_json(inputs["dataset"])
        dataset["samples"] = [row for row in dataset["samples"] if row["candidate_id"] in selected_ids]
        dataset["sources"] = [
            {**row, "candidate_ids": [candidate_id for candidate_id in row["candidate_ids"] if candidate_id in selected_ids]}
            for row in dataset["sources"]
            if any(candidate_id in selected_ids for candidate_id in row["candidate_ids"])
        ]
        dataset["summary"]["sample_count"] = len(dataset["samples"])
        dataset["summary"]["source_count"] = len(dataset["sources"])
        dataset["contract"] = {"sha256": sha256_file(source_contract)}
        for sample in dataset["samples"]:
            value = 240 if labels[sample["candidate_id"]] == "match_ball" else 15
            for artifact_name, shape in (
                ("tight_tensor", (5, 3, 64, 64)),
                ("context_tensor", (5, 3, 128, 128)),
            ):
                descriptor = sample["artifacts"][artifact_name]
                tensor_path = inputs["dataset"].parent / descriptor["path"]
                np.save(tensor_path, np.full(shape, value, dtype=np.uint8), allow_pickle=False)
                descriptor["sha256"] = sha256_file(tensor_path)
        _write_json(inputs["dataset"], dataset)
        for source_row in dataset["sources"]:
            source_path = inputs["dataset"].parent / source_row["path"]
            source_path.write_bytes(Path(source_row["path"]).stem.encode("utf-8"))
            self.assertEqual(source_row["sha256"], sha256_file(source_path))
        self.assertEqual(3, len({row["sha256"] for row in dataset["sources"]}))

        annotation = self._resolve_human_annotations(
            root,
            source_contract,
            inputs["dataset"],
            labels,
        )
        package = root / "model"
        train_candidate_classifier(
            inputs["dataset"],
            annotation["annotation_resolution"],
            annotation["resolved_contract"],
            package,
            config=TrainingConfig(epochs=15, batch_size=2, learning_rate=0.01, seed=317),
        )
        return {
            "root": root,
            "source_contract": source_contract,
            "vote_ledger": annotation["vote_ledger"],
            "annotation_resolution": annotation["annotation_resolution"],
            "resolved_contract": annotation["resolved_contract"],
            "dataset": inputs["dataset"],
            "model_manifest": package / MODEL_MANIFEST_NAME,
            "training_report": package / "training_report.v1.json",
            "model_weights": package / "model.pt",
        }

    def _build_policy_qualification(
        self,
        root: Path,
        development: dict[str, Path],
    ) -> dict[str, Path]:
        root.mkdir()
        input_root = root / "input"
        dataset_root = root / "dataset"
        input_root.mkdir()
        dataset_root.mkdir()
        inputs = _write_inputs(dataset_root, calibration_per_class=368, audit_per_class=368)
        generated_source_contract = dataset_root / "source-contract.json"
        input_source_contract = input_root / TRACKING_CONTRACT_REPORT_NAME
        bound_source_contract = dataset_root / TRACKING_CONTRACT_REPORT_NAME
        shutil.copyfile(generated_source_contract, input_source_contract)
        shutil.copyfile(input_source_contract, bound_source_contract)
        generated_source_contract.unlink()
        self.assertEqual(sha256_file(input_source_contract), sha256_file(bound_source_contract))
        dataset = _read_json(inputs["dataset_manifest_path"])
        resolved_fixture = _read_json(inputs["resolved_contract_path"])
        labels = {row["candidate_id"]: row["label"] for row in resolved_fixture["classifications"]}
        shared = dataset_root / "evidence"
        shared.mkdir()
        evidence_by_label = {}
        for label, value in (("match_ball", 240), ("equipment_or_background", 15)):
            evidence = {}
            for artifact_name, shape, filename in (
                ("tight_tensor", (5, 3, 64, 64), f"{label}-tight.npy"),
                ("context_tensor", (5, 3, 128, 128), f"{label}-context.npy"),
            ):
                artifact_path = shared / filename
                np.save(artifact_path, np.full(shape, value, dtype=np.uint8), allow_pickle=False)
                evidence[artifact_name] = {
                    "path": artifact_path.relative_to(dataset_root).as_posix(),
                    "sha256": sha256_file(artifact_path),
                    "shape": list(shape),
                    "dtype": "uint8",
                    "color_space": "RGB",
                }
            montage_path = shared / f"{label}-montage.png"
            montage_path.write_bytes(f"native-qualification-{label}".encode("utf-8"))
            evidence["review_montage"] = {
                "path": montage_path.relative_to(dataset_root).as_posix(),
                "sha256": sha256_file(montage_path),
            }
            evidence_by_label[label] = evidence
        for sample in dataset["samples"]:
            sample["artifacts"] = evidence_by_label[labels[sample["candidate_id"]]]
        dataset["frame_offsets"] = [-2, -1, 0, 1, 2]
        dataset["preprocessing_runtime"] = _preprocessing_runtime()
        dataset["tensor_contract"] = _tensor_contract()

        source_hashes = []
        for source_row in dataset["sources"]:
            source_path = dataset_root / source_row["path"]
            source_path.write_bytes(f"eval-video-{source_row['variant_id']}".encode("utf-8"))
            self.assertEqual(source_row["sha256"], sha256_file(source_path))
            source_hashes.append(source_row["sha256"])
        self.assertEqual(len(dataset["sources"]), len(set(source_hashes)))
        self.assertEqual(len(dataset["sources"]), len({row["variant_id"] for row in dataset["sources"]}))
        self.assertEqual(len(dataset["sources"]), len({row["group_id"] for row in dataset["sources"]}))
        _write_json(inputs["dataset_manifest_path"], dataset)

        annotation = self._resolve_human_annotations(
            root,
            input_source_contract,
            inputs["dataset_manifest_path"],
            labels,
        )

        inference_dir = root / "predictions"
        classify_candidates(
            development["model_manifest"].parent,
            inputs["dataset_manifest_path"],
            bound_source_contract,
            inference_dir,
            batch_size=128,
        )
        inputs["predictions_path"] = inference_dir / PREDICTIONS_NAME

        roles_dir = root / "roles"
        build_selective_policy_roles(
            inputs["predictions_path"],
            inputs["dataset_manifest_path"],
            annotation["annotation_resolution"],
            annotation["resolved_contract"],
            development["model_manifest"],
            development["training_report"],
            roles_dir,
        )
        policy_dir = root / "policy"
        policy = fit_selective_policy(
            inputs["predictions_path"],
            inputs["dataset_manifest_path"],
            annotation["annotation_resolution"],
            annotation["resolved_contract"],
            development["model_manifest"],
            development["training_report"],
            roles_dir / SELECTIVE_POLICY_ROLES_NAME,
            policy_dir,
            config=SelectivePolicyConfig(max_thresholds_per_lane=1),
        )
        self.assertEqual("qualified", policy["status"])
        validated = validate_selective_policy_evidence_binding(
            policy_dir / SELECTIVE_POLICY_NAME,
            policy_dir / SELECTIVE_DECISIONS_NAME,
            inputs["predictions_path"],
            inputs["dataset_manifest_path"],
            annotation["annotation_resolution"],
            annotation["resolved_contract"],
            development["model_manifest"],
            roles_dir / SELECTIVE_POLICY_ROLES_NAME,
        )
        self.assertEqual(policy, validated["policy"])

        bound_bytes = bound_source_contract.read_bytes()
        bound_source_contract.unlink()
        try:
            with self.assertRaisesRegex(SelectivePolicyError, "qualification source contract is missing"):
                validate_selective_policy_evidence_binding(
                    policy_dir / SELECTIVE_POLICY_NAME,
                    policy_dir / SELECTIVE_DECISIONS_NAME,
                    inputs["predictions_path"],
                    inputs["dataset_manifest_path"],
                    annotation["annotation_resolution"],
                    annotation["resolved_contract"],
                    development["model_manifest"],
                    roles_dir / SELECTIVE_POLICY_ROLES_NAME,
                )
        finally:
            bound_source_contract.write_bytes(bound_bytes)
        bound_source_contract.write_bytes(b"hash-mismatched-source-contract")
        try:
            with self.assertRaisesRegex(SelectivePolicyError, "source contract sha256"):
                validate_selective_policy_evidence_binding(
                    policy_dir / SELECTIVE_POLICY_NAME,
                    policy_dir / SELECTIVE_DECISIONS_NAME,
                    inputs["predictions_path"],
                    inputs["dataset_manifest_path"],
                    annotation["annotation_resolution"],
                    annotation["resolved_contract"],
                    development["model_manifest"],
                    roles_dir / SELECTIVE_POLICY_ROLES_NAME,
                )
        finally:
            bound_source_contract.write_bytes(bound_bytes)

        tampered_predictions_path = root / "tampered-qualification-predictions.json"
        tampered = _read_json(inputs["predictions_path"])
        first = tampered["predictions"][0]
        first["probabilities"]["match_ball"] = 0.75
        first["probabilities"]["equipment_or_background"] = 0.25
        first["predicted_label"] = "match_ball"
        first["confidence"] = 0.75
        _write_json(tampered_predictions_path, tampered)
        with self.assertRaisesRegex(SelectivePolicyError, "frozen classifier inference"):
            validate_selective_policy_evidence_binding(
                policy_dir / SELECTIVE_POLICY_NAME,
                policy_dir / SELECTIVE_DECISIONS_NAME,
                tampered_predictions_path,
                inputs["dataset_manifest_path"],
                annotation["annotation_resolution"],
                annotation["resolved_contract"],
                development["model_manifest"],
                roles_dir / SELECTIVE_POLICY_ROLES_NAME,
            )
        return {
            "root": root,
            "source_contract": bound_source_contract,
            "vote_ledger": annotation["vote_ledger"],
            "annotation_resolution": annotation["annotation_resolution"],
            "resolved_contract": annotation["resolved_contract"],
            "dataset": inputs["dataset_manifest_path"],
            "predictions": inputs["predictions_path"],
            "policy_roles": roles_dir / SELECTIVE_POLICY_ROLES_NAME,
            "policy": policy_dir / SELECTIVE_POLICY_NAME,
            "qualification_decisions": policy_dir / SELECTIVE_DECISIONS_NAME,
        }

    def _build_target_application(
        self,
        root: Path,
        development: dict[str, Path],
        qualification: dict[str, Path],
    ) -> dict[str, Path]:
        root.mkdir()
        source_path = root / "source.mp4"
        writer = cv2.VideoWriter(
            str(source_path),
            cv2.VideoWriter_fourcc(*"mp4v"),
            25.0,
            (100, 100),
        )
        self.assertTrue(writer.isOpened())
        try:
            for frame_index in range(200):
                frame = np.zeros((100, 100, 3), dtype=np.uint8)
                frame[:, :, 1] = 48
                cv2.circle(frame, (10 + frame_index % 80, 50), 3, (255, 255, 255), -1)
                writer.write(frame)
        finally:
            writer.release()
        source_sha256 = sha256_file(source_path)
        detector_candidate = Candidate(
            frame_index=100,
            x1=10.0,
            y1=10.0,
            x2=20.0,
            y2=20.0,
            confidence=0.8,
            source="detector",
        )
        assign_candidate_ids([detector_candidate], source_sha256)
        candidate = candidate_to_contract_record(detector_candidate)
        contract_path = root / "tracking_contract.v2.json"
        _write_json(
            contract_path,
            build_tracking_contract(
                source={
                    "video_sha256": source_sha256,
                    "fps": 25.0,
                    "width": 100,
                    "height": 100,
                    "frame_count": 200,
                },
                frames=[{"frame_index": frame_index, "status": "unknown"} for frame_index in range(200)],
                candidates=[candidate],
            ),
        )
        sample = _write_sample(
            root,
            np.random.default_rng(991),
            candidate["candidate_id"],
            candidate["frame_index"],
            "target-native-variant",
            "target-native-group",
            "target-native-split",
            candidate["bbox"],
        )
        for descriptor in sample["artifacts"].values():
            descriptor["size_bytes"] = (root / descriptor["path"]).stat().st_size
        dataset_path = root / "candidate_dataset_manifest.json"
        _write_json(
            dataset_path,
            {
                "schema_version": "1.0",
                "artifact_type": "candidate_dataset",
                "builder_version": "candidate-dataset-v1",
                "dataset_version": hashlib.sha256(b"native-target-dataset").hexdigest(),
                "contract": {"sha256": sha256_file(contract_path)},
                "frame_offsets": [-2, -1, 0, 1, 2],
                "preprocessing_runtime": _preprocessing_runtime(),
                "tensor_contract": _tensor_contract(),
                "summary": {"status": "ok", "sample_count": 1, "source_count": 1},
                "sources": [
                    {
                        "path": source_path.name,
                        "sha256": source_sha256,
                        "variant_id": "target-native-variant",
                        "width": 100,
                        "height": 100,
                        "frame_count": 200,
                        "fps": 25.0,
                        "group_id": "target-native-group",
                        "temporal_group": "target-native-variant-temporal",
                        "split_group": "target-native-split",
                        "candidate_ids": [candidate["candidate_id"]],
                    }
                ],
                "samples": [sample],
            },
        )
        inference_dir = root / "native-inference"
        classify_candidates(
            development["model_manifest"].parent,
            dataset_path,
            contract_path,
            inference_dir,
            batch_size=1,
        )
        application_dir = root / "native-application"
        apply_frozen_selective_policy(
            qualification["policy"],
            inference_dir / PREDICTIONS_NAME,
            dataset_path,
            contract_path,
            development["model_manifest"],
            application_dir,
        )
        return {
            "root": root,
            "source": source_path,
            "contract": contract_path,
            "dataset": dataset_path,
            "predictions": inference_dir / PREDICTIONS_NAME,
            "decisions": application_dir / SELECTIVE_APPLICATION_NAME,
        }

    def _resolve_human_annotations(
        self,
        root: Path,
        contract_path: Path,
        dataset_path: Path,
        labels: dict[str, str],
    ) -> dict[str, Path]:
        dataset = _read_json(dataset_path)
        samples = {row["candidate_id"]: row for row in dataset["samples"]}
        votes = []
        for candidate_id in sorted(labels):
            for reviewer in ("a", "b"):
                votes.append(
                    {
                        "schema_version": "1.0",
                        "record_type": "vote",
                        "vote_id": f"{candidate_id}-{reviewer}",
                        "candidate_id": candidate_id,
                        "stage": "primary",
                        "reviewer_type": "human",
                        "annotator_id": f"native-reviewer-{reviewer}",
                        "fingerprint": f"native-reviewer-{reviewer}-device",
                        "label": labels[candidate_id],
                        "confidence": 0.99,
                        "blind": True,
                        "created_at": "2026-07-15T10:00:00Z",
                        "audit_note": "native integration fixture",
                        "dataset_version": dataset["dataset_version"],
                        "sample_id": samples[candidate_id]["sample_id"],
                        "evidence_sha256": sample_evidence_sha256(samples[candidate_id]),
                    }
                )
        ledger_path = contract_path.parent / "votes.sequence-001.jsonl"
        header = {
            "schema_version": "1.0",
            "record_type": "ledger_header",
            "contract_sha256": sha256_file(contract_path),
            "dataset_version": dataset["dataset_version"],
            "evidence_manifest_sha256": sha256_file(dataset_path),
            "append_only_chain": {
                "algorithm": "sha256-ledger-chain-v1",
                "sequence": 1,
                "previous_ledger_sha256": None,
            },
        }
        ledger_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in [header, *votes]),
            encoding="utf-8",
        )
        output_dir = root / "annotations"
        report = resolve_candidate_annotations(
            contract_path,
            ledger_path,
            output_dir,
            dataset_manifest_path=dataset_path,
        )
        self.assertEqual(len(labels), report["summary"]["training_eligible_count"])
        return {
            "vote_ledger": ledger_path,
            "annotation_resolution": output_dir / ANNOTATION_RESOLUTION_NAME,
            "resolved_contract": output_dir / TRACKING_CONTRACT_REPORT_NAME,
        }

    def _write_bundle_draft(
        self,
        source: Path,
        development: dict[str, Path],
        qualification: dict[str, Path],
        target: dict[str, Path],
        queue_path: Path,
        application_validation: dict[str, object],
    ) -> dict[str, object]:
        def relative(path: Path) -> str:
            return path.relative_to(source).as_posix()

        def package_file(path: Path) -> tuple[str, str]:
            return relative(path), sha256_file(path)

        dev_source, dev_source_sha = package_file(development["source_contract"])
        dev_ledger, dev_ledger_sha = package_file(development["vote_ledger"])
        dev_annotation, dev_annotation_sha = package_file(development["annotation_resolution"])
        dev_resolved, dev_resolved_sha = package_file(development["resolved_contract"])
        qualification_source, qualification_source_sha = package_file(qualification["source_contract"])
        qualification_ledger, qualification_ledger_sha = package_file(qualification["vote_ledger"])
        qualification_annotation, qualification_annotation_sha = package_file(
            qualification["annotation_resolution"]
        )
        qualification_resolved, qualification_resolved_sha = package_file(qualification["resolved_contract"])
        qualification_predictions, qualification_predictions_sha = package_file(qualification["predictions"])
        qualification_decisions, qualification_decisions_sha = package_file(
            qualification["qualification_decisions"]
        )
        qualification_roles, qualification_roles_sha = package_file(qualification["policy_roles"])
        target_dataset, target_dataset_sha = package_file(target["dataset"])
        target_predictions, target_predictions_sha = package_file(target["predictions"])
        target_decisions, target_decisions_sha = package_file(target["decisions"])
        target_source, target_source_sha = package_file(target["source"])
        target_contract, target_contract_sha = package_file(target["contract"])
        draft = {
            "bundle_id": "review-evidence-native-multi-video-e2e",
            "target": {
                "run_id": "run-native-e2e",
                "source_sha256": target_source_sha,
                "root_contract_sha256": target_contract_sha,
                "max_review_windows": 1,
                "max_manual_review_windows": 1,
                "action_signal_binding_sha256": "a" * 64,
                "confirmed_config_sha256": "b" * 64,
                "profile_digest": "c" * 64,
                "quality_profile": "stable_broadcast",
                "provisioner_version": "review-evidence-provisioner-v2",
                "candidate_population_sha256": application_validation["candidate_population_sha256"],
                "candidate_population_count": len(application_validation["candidate_ids"]),
            },
            "provisioning": {
                "attempt_quota_bytes": 256 * 1024 * 1024,
                "retention": {
                    "policy": "manual-audit-retention-v1",
                    "retain_until": "2030-01-01T00:00:00+00:00",
                    "automatic_delete": False,
                },
            },
            "packages": {
                "model_development": {
                    "root": relative(development["root"]),
                    "manifest_path": relative(development["model_manifest"]),
                    "dataset_path": relative(development["dataset"]),
                    "dataset_sha256": sha256_file(development["dataset"]),
                    "source_contract_path": dev_source,
                    "source_contract_sha256": dev_source_sha,
                    "vote_ledger_path": dev_ledger,
                    "vote_ledger_sha256": dev_ledger_sha,
                    "annotation_resolution_path": dev_annotation,
                    "annotation_resolution_sha256": dev_annotation_sha,
                    "resolved_contract_path": dev_resolved,
                    "resolved_contract_sha256": dev_resolved_sha,
                },
                "policy_qualification": {
                    "root": relative(qualification["root"]),
                    "manifest_path": relative(qualification["policy"]),
                    "dataset_path": relative(qualification["dataset"]),
                    "dataset_sha256": sha256_file(qualification["dataset"]),
                    "policy_path": relative(qualification["policy"]),
                    "policy_sha256": sha256_file(qualification["policy"]),
                    "source_contract_path": qualification_source,
                    "source_contract_sha256": qualification_source_sha,
                    "vote_ledger_path": qualification_ledger,
                    "vote_ledger_sha256": qualification_ledger_sha,
                    "annotation_resolution_path": qualification_annotation,
                    "annotation_resolution_sha256": qualification_annotation_sha,
                    "resolved_contract_path": qualification_resolved,
                    "resolved_contract_sha256": qualification_resolved_sha,
                    "predictions_path": qualification_predictions,
                    "predictions_sha256": qualification_predictions_sha,
                    "decisions_path": qualification_decisions,
                    "decisions_sha256": qualification_decisions_sha,
                    "policy_roles_path": qualification_roles,
                    "policy_roles_sha256": qualification_roles_sha,
                },
                "target_application": {
                    "root": relative(target["root"]),
                    "manifest_path": target_dataset,
                    "dataset_path": target_dataset,
                    "dataset_sha256": target_dataset_sha,
                    "predictions_path": target_predictions,
                    "predictions_sha256": target_predictions_sha,
                    "decisions_path": target_decisions,
                    "decisions_sha256": target_decisions_sha,
                    "source_path": target_source,
                    "source_sha256": target_source_sha,
                    "root_contract_path": target_contract,
                    "root_contract_sha256": target_contract_sha,
                },
            },
            "queue": {
                "source_path": relative(queue_path),
                "source_sha256": sha256_file(queue_path),
            },
        }
        _write_json(source / "review_evidence_bundle.draft.json", draft)
        return draft

    def _create_service_parent(
        self,
        repo: Path,
        source: Path,
        target: dict[str, Path],
        draft: dict[str, object],
    ) -> tuple[ApiService, Path]:
        for name in ("config", "data", "outputs", "weights"):
            (repo / name).mkdir(parents=True, exist_ok=True)
        service = ApiService(repo)
        parent_output = repo / "outputs" / "runs" / "native" / "run-native-e2e"
        parent_output.mkdir(parents=True)
        config_path = repo / "config" / "native.yaml"
        config_path.write_text("input_video: native.mp4\n", encoding="utf-8")
        expected_config_sha256 = sha256_file(config_path)
        (parent_output / TRACKING_CONTRACT_REPORT_NAME).write_bytes(target["contract"].read_bytes())
        action_track = parent_output / "action_track.csv"
        action_track.write_text("Frame,Action\n0,open_play\n", encoding="utf-8")
        action_report = parent_output / "action_signal_report.v1.json"
        _write_json(
            action_report,
            {
                "schema_version": "1.0",
                "artifact_type": "action_signal_report",
                "status": "complete",
                "input_video": str(target["source"].resolve()),
                "artifacts": {"track": action_track.name},
            },
        )
        _write_json(
            parent_output / "action_signal_binding.v1.json",
            {
                "schema_version": "1.0",
                "artifact_type": "broadcast_action_signal_binding",
                "source": {
                    "video_sha256": sha256_file(target["source"]),
                    "tracking_contract_sha256": sha256_file(parent_output / TRACKING_CONTRACT_REPORT_NAME),
                },
                "artifacts": {
                    action_track.name: {
                        "sha256": sha256_file(action_track),
                        "size_bytes": action_track.stat().st_size,
                    },
                    action_report.name: {
                        "sha256": sha256_file(action_report),
                        "size_bytes": action_report.stat().st_size,
                    },
                },
            },
        )
        registry = service._read_registry()
        registry["runs"].append(
            {
                "run_id": "run-native-e2e",
                "source": "broadcast_hybrid",
                "status": "completed",
                "created_at": "2026-07-15T00:00:00+00:00",
                "started_at": "2026-07-15T00:00:00+00:00",
                "completed_at": "2026-07-15T00:01:00+00:00",
                "config_name": "native.yaml",
                "config_path": str(config_path),
                "input_video": str(target["source"]),
                "parent_run_id": None,
                "output_dir": str(parent_output),
                "modules_enabled": {"broadcast_hybrid": True},
                "artifacts": [],
                "stats": {},
                "broadcast": {
                    "status": "needs_review",
                    "quality_profile": "stable_broadcast",
                    "max_manual_review_windows": 1,
                    "preflight": {"native_fixture": True},
                    "blocking_reasons": ["missing_qualified_selective_review_queue"],
                    "limitations": [],
                    "terminal_tail_acknowledged": True,
                },
                "progress": None,
                "notes": json.dumps(
                    {
                        "schema_version": "1.0",
                        "purpose": "production_full",
                        "confirmed_config_name": "native.yaml",
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
            "run-native-e2e",
            parent_output,
            parent=registry["runs"][-1],
        )
        _write_json(source / "review_evidence_bundle.draft.json", draft)
        return service, parent_output

    def _wait_for_terminal(self, service: ApiService, run_id: str) -> dict[str, object]:
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            run = service.get_run(run_id)
            if run.get("status") in {"completed", "failed", "cancelled"}:
                return run
            time.sleep(0.02)
        self.fail(f"run did not become terminal: {run_id}")


def _tensor_contract() -> dict[str, object]:
    return {
        "color_space": "RGB",
        "dtype": "uint8",
        "tight_shape": [5, 3, 64, 64],
        "context_shape": [5, 3, 128, 128],
        "markup": False,
    }


def _preprocessing_runtime() -> dict[str, object]:
    return {
        "pipeline": "candidate-dataset-v1",
        "frame_offsets": [-2, -1, 0, 1, 2],
        "tight_crop_scale": 1.25,
        "context_crop_scale": 4.0,
        "tight_size": 64,
        "context_size": 128,
        "color_conversion": "opencv:BGR2RGB",
        "resize_down": "INTER_AREA",
        "resize_up": "INTER_LINEAR",
        "python": "test",
        "opencv": "test",
        "numpy": np.__version__,
    }


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
