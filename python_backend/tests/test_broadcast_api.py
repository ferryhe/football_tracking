from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import CancelledError
from pathlib import Path
from typing import Any
from unittest import mock

import cv2
import numpy as np
import yaml
from pydantic import ValidationError

from football_tracking.action_signal import ActionCalibration, generate_action_track
from football_tracking.api.app import create_app
from football_tracking.api.broadcast_api import (
    BroadcastApiError,
    _safe_status_generation_dir,
    build_review_action_envelope,
    collect_review_evidence_paths,
    publish_broadcast_facade,
    publish_json_exclusive,
    validate_broadcast_quality_report,
    validate_review_queue_bindings,
)
from football_tracking.api.routes.artifacts import get_artifact
from football_tracking.api.schemas import (
    BroadcastOperationResponse,
    BroadcastReviewAction,
    BroadcastReviewActionsRequest,
    BroadcastReviewWindowsResponse,
    CreateRunRequest,
    RunRecord,
)
from football_tracking.api.service import ApiService
from football_tracking.broadcast_hybrid_orchestration import (
    PUBLIC_ARTIFACTS,
    BroadcastHybridOrchestrationError,
)
from football_tracking.candidate_annotations import sample_evidence_sha256
from football_tracking.tracking_contracts import build_tracking_contract


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class BroadcastRequestSchemaTests(unittest.TestCase):
    def test_public_broadcast_payloads_use_named_typed_models(self) -> None:
        run_schema = RunRecord.model_json_schema()
        operation_schema = BroadcastOperationResponse.model_json_schema()
        windows_schema = BroadcastReviewWindowsResponse.model_json_schema()

        self.assertEqual("#/$defs/BroadcastRunState", run_schema["properties"]["broadcast"]["$ref"])
        self.assertEqual(
            "#/$defs/BroadcastOperationDetails",
            operation_schema["properties"]["details"]["$ref"],
        )
        self.assertEqual(
            "#/$defs/BroadcastReviewWindow",
            windows_schema["properties"]["items"]["items"]["$ref"],
        )
        self.assertIn("BroadcastReviewEvidenceArtifact", windows_schema["$defs"])

    def test_broadcast_request_requires_stable_profile_and_three_frame_calibration(self) -> None:
        request = CreateRunRequest(
            config_name="default.yaml",
            pipeline_mode="broadcast_hybrid",
            quality_profile="stable_broadcast",
            max_manual_review_windows=30,
            calibration_confirmation={
                "source_resolution": [5120, 1440],
                "confirmed_sample_frames": [0, 100, 200],
                "field_polygon": [[0, 0], [5119, 0], [5119, 1439], [0, 1439]],
                "exclusion_polygons": [],
            },
        )

        self.assertEqual("broadcast_hybrid", request.pipeline_mode)
        self.assertEqual((0, 100, 200), request.calibration_confirmation.confirmed_sample_frames)

        with self.assertRaises(ValidationError):
            CreateRunRequest(
                config_name="default.yaml",
                pipeline_mode="broadcast_hybrid",
                quality_profile="stable_broadcast",
                max_manual_review_windows=31,
                calibration_confirmation={
                    "source_resolution": [5120, 1440],
                    "confirmed_sample_frames": [0, 100, 200],
                    "field_polygon": [[0, 0], [1, 0], [1, 1]],
                },
            )

        with self.assertRaises(ValidationError):
            CreateRunRequest(config_name="default.yaml", pipeline_mode="broadcast_hybrid")
        with self.assertRaises(ValidationError):
            CreateRunRequest(
                parent_run_id="parent",
                approved_action_ids=["approved"],
                pipeline_mode="broadcast_hybrid",
                quality_profile="stable_broadcast",
                calibration_confirmation=request.calibration_confirmation,
            )
        with self.assertRaises(ValidationError):
            CreateRunRequest(config_name="default.yaml", max_manual_review_windows=10)
        with self.assertRaises(ValidationError):
            CreateRunRequest(
                config_name="default.yaml",
                pipeline_mode="broadcast_hybrid",
                quality_profile="stable_broadcast",
                calibration_confirmation={
                    "source_resolution": [100, 100],
                    "confirmed_sample_frames": [0, 10, 20],
                    "field_polygon": [[0, 0], [10, 10], [20, 20]],
                },
            )

    def test_review_action_shapes_fail_closed(self) -> None:
        self.assertEqual([], BroadcastReviewActionsRequest(actions=[]).actions)
        valid = BroadcastReviewActionsRequest(
            actions=[
                BroadcastReviewAction(
                    action_id="a1",
                    review_item_id="window-1",
                    candidate_id="candidate-1",
                    reviewer_id="operator",
                    action="reject_noise",
                    noise_subtype="field_line_or_mark",
                )
            ]
        )
        self.assertEqual("reject_noise", valid.actions[0].action)

        with self.assertRaises(ValidationError):
            BroadcastReviewAction(
                action_id="a1",
                review_item_id="window-1",
                candidate_id="candidate-1",
                reviewer_id="operator",
                action="reject_noise",
            )
        with self.assertRaises(ValidationError):
            BroadcastReviewAction(
                action_id="a1",
                review_item_id="window-1",
                candidate_id="candidate-1",
                reviewer_id="operator",
                action="correct_trajectory",
            )


class BroadcastReviewBindingTests(unittest.TestCase):
    def test_server_builds_exact_queue_and_candidate_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            queue_path = root / "selective_review_queue.v1.json"
            binding_names = (
                "review_timing",
                "policy",
                "decisions",
                "model",
                "training_report",
                "model_weights",
                "dataset",
                "predictions",
                "contract",
                "annotation_resolution",
                "resolved_tracking_contract",
                "policy_roles",
            )
            queue = {
                "schema_version": "1.0",
                "artifact_type": "selective_review_queue",
                "review_item_count": 1,
                "bindings": {
                    name: {"path": f"{name}.json", "sha256": format(index, "x")[-1] * 64}
                    for index, name in enumerate(binding_names, 1)
                },
                "items": [
                    {
                        "review_item_id": "window-1",
                        "candidates": [
                            {
                                "candidate_id": "candidate-1",
                                "candidate_fingerprint": "e" * 64,
                                "evidence": {"sha256": "f" * 64},
                            }
                        ],
                    }
                ],
            }
            _write_json(queue_path, queue)
            request = BroadcastReviewActionsRequest(
                actions=[
                    BroadcastReviewAction(
                        action_id="action-1",
                        review_item_id="window-1",
                        candidate_id="candidate-1",
                        reviewer_id="operator",
                        action="mark_unknown",
                    )
                ]
            )

            envelope = build_review_action_envelope(queue_path, request.model_dump(mode="json")["actions"])

            action = envelope["actions"][0]
            self.assertEqual(_sha256(queue_path), action["bindings"]["queue_sha256"])
            self.assertEqual("2" * 64, action["bindings"]["policy_sha256"])
            self.assertEqual("f" * 64, action["bindings"]["evidence_sha256"])
            self.assertEqual("e" * 64, action["bindings"]["candidate_fingerprint"])

            with self.assertRaises(BroadcastApiError):
                build_review_action_envelope(
                    queue_path,
                    [{**request.model_dump(mode="json")["actions"][0], "candidate_id": "missing"}],
                )

            queue["items"][0]["candidates"].append(
                {
                    "candidate_id": "candidate-2",
                    "candidate_fingerprint": "d" * 64,
                    "evidence": {"sha256": "c" * 64},
                }
            )
            _write_json(queue_path, queue)
            with self.assertRaisesRegex(BroadcastApiError, "cover every bound queue candidate"):
                build_review_action_envelope(queue_path, request.model_dump(mode="json")["actions"])

    def test_server_rejects_duplicate_queue_candidate_and_invalid_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            queue_path = root / "selective_review_queue.v1.json"
            binding_names = (
                "review_timing",
                "policy",
                "decisions",
                "model",
                "training_report",
                "model_weights",
                "dataset",
                "predictions",
                "contract",
                "annotation_resolution",
                "resolved_tracking_contract",
                "policy_roles",
            )
            candidate = {
                "candidate_id": "candidate-1",
                "candidate_fingerprint": "e" * 64,
                "evidence": {"sha256": "f" * 64},
            }
            queue = {
                "schema_version": "1.0",
                "artifact_type": "selective_review_queue",
                "review_item_count": 2,
                "bindings": {name: {"path": f"{name}.json", "sha256": "a" * 64} for name in binding_names},
                "items": [
                    {"review_item_id": "window-1", "candidates": [candidate]},
                    {"review_item_id": "window-2", "candidates": [candidate]},
                ],
            }
            _write_json(queue_path, queue)
            action = {
                "action_id": "action-1",
                "review_item_id": "window-1",
                "candidate_id": "candidate-1",
                "reviewer_id": "operator",
                "created_at": "not-a-timestamp",
                "action": "mark_unknown",
                "noise_subtype": None,
                "keypoints": [],
            }
            with self.assertRaisesRegex(BroadcastApiError, "multiple review items"):
                build_review_action_envelope(queue_path, [action])

            queue["items"].pop()
            queue["review_item_count"] = 1
            _write_json(queue_path, queue)
            with self.assertRaisesRegex(BroadcastApiError, "ISO-8601"):
                build_review_action_envelope(queue_path, [action])

    def test_downloadable_review_evidence_is_dataset_bound_and_rehashed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            dataset_dir = root / "candidate_dataset"
            sample_dir = dataset_dir / "samples" / "000001-candidate-1"
            sample_dir.mkdir(parents=True)
            artifact_paths = {
                "tight_tensor": sample_dir / "tight.npy",
                "context_tensor": sample_dir / "context.npy",
                "review_montage": sample_dir / "review_montage.png",
            }
            for name, path in artifact_paths.items():
                path.write_bytes(f"bound-{name}".encode())
            artifacts = {
                name: {
                    "path": path.relative_to(dataset_dir).as_posix(),
                    "sha256": _sha256(path),
                    "size_bytes": path.stat().st_size,
                }
                for name, path in artifact_paths.items()
            }
            sample = {
                "sample_id": "000001-candidate-1",
                "candidate_id": "candidate-1",
                "artifacts": artifacts,
            }
            dataset_version = "d" * 64
            dataset_path = dataset_dir / "candidate_dataset_manifest.json"
            _write_json(
                dataset_path,
                {
                    "schema_version": "1.0",
                    "artifact_type": "candidate_dataset",
                    "dataset_version": dataset_version,
                    "samples": [sample],
                },
            )
            binding_names = (
                "review_timing",
                "policy",
                "decisions",
                "model",
                "training_report",
                "model_weights",
                "predictions",
                "contract",
                "annotation_resolution",
                "resolved_tracking_contract",
                "policy_roles",
            )
            bindings: dict[str, dict[str, str]] = {
                "dataset": {
                    "path": dataset_path.relative_to(root).as_posix(),
                    "sha256": _sha256(dataset_path),
                }
            }
            for name in binding_names:
                path = root / "bindings" / f"{name}.json"
                _write_json(path, {"artifact_type": name})
                bindings[name] = {"path": path.relative_to(root).as_posix(), "sha256": _sha256(path)}
            queue_path = root / "selective_review_queue.v1.json"
            _write_json(
                queue_path,
                {
                    "schema_version": "1.0",
                    "artifact_type": "selective_review_queue",
                    "review_item_count": 1,
                    "bindings": bindings,
                    "items": [
                        {
                            "review_item_id": "window-1",
                            "candidates": [
                                {
                                    "candidate_id": "candidate-1",
                                    "evidence": {
                                        "sample_id": sample["sample_id"],
                                        "sha256": sample_evidence_sha256(sample),
                                        "dataset_version": dataset_version,
                                        "artifacts": artifacts,
                                    },
                                }
                            ],
                        }
                    ],
                },
            )
            private_path = root / "private" / "secret.bin"
            private_path.parent.mkdir()
            private_path.write_bytes(b"not queue bound")

            collected = collect_review_evidence_paths(queue_path, root)

            self.assertEqual(set(artifact_paths.values()), set(collected))
            self.assertNotIn(private_path, collected)
            artifact_paths["review_montage"].write_bytes(b"tampered")
            with self.assertRaisesRegex(BroadcastApiError, "size changed|hash changed"):
                collect_review_evidence_paths(queue_path, root)

            artifacts["review_montage"] = {
                "path": "../private/secret.bin",
                "sha256": _sha256(private_path),
                "size_bytes": private_path.stat().st_size,
            }
            _write_json(
                dataset_path,
                {
                    "schema_version": "1.0",
                    "artifact_type": "candidate_dataset",
                    "dataset_version": dataset_version,
                    "samples": [sample],
                },
            )
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            queue["bindings"]["dataset"]["sha256"] = _sha256(dataset_path)
            queue_evidence = queue["items"][0]["candidates"][0]["evidence"]
            queue_evidence["artifacts"] = artifacts
            queue_evidence["sha256"] = sample_evidence_sha256(sample)
            _write_json(queue_path, queue)
            with self.assertRaisesRegex(BroadcastApiError, "contained and relative"):
                collect_review_evidence_paths(queue_path, root)

    def test_json_exclusive_publish_rejects_a_dangling_target_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as external_dir:
            root = Path(temp_dir).resolve()
            external_target = Path(external_dir).resolve() / "escaped.json"
            target = root / "report.json"
            try:
                target.symlink_to(external_target)
            except OSError as exc:
                self.skipTest(f"file symlinks are unavailable: {exc}")

            with self.assertRaisesRegex(BroadcastApiError, "symlink or reparse"):
                publish_json_exclusive(target, {"status": "blocked"}, trusted_root=root)
            self.assertFalse(external_target.exists())

    def test_queue_binding_rehashes_every_dataset_sample_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inputs = root / "inputs"
            inputs.mkdir()
            evidence = inputs / "evidence.bin"
            evidence.write_bytes(b"original-evidence")
            binding_names = (
                "review_timing",
                "policy",
                "decisions",
                "model",
                "training_report",
                "model_weights",
                "dataset",
                "predictions",
                "contract",
                "annotation_resolution",
                "resolved_tracking_contract",
                "policy_roles",
            )
            bindings: dict[str, dict[str, str]] = {}
            for name in binding_names:
                path = inputs / f"{name}.json"
                payload: object = {"name": name}
                if name == "dataset":
                    payload = {
                        "artifact_type": "candidate_dataset",
                        "samples": [
                            {
                                "sample_id": "sample-1",
                                "candidate_id": "candidate-1",
                                "artifacts": {
                                    "frame": {
                                        "path": evidence.name,
                                        "sha256": _sha256(evidence),
                                        "size_bytes": evidence.stat().st_size,
                                    }
                                },
                            }
                        ],
                    }
                _write_json(path, payload)
                bindings[name] = {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": _sha256(path),
                }
            queue_path = root / "selective_review_queue.v1.json"
            _write_json(
                queue_path,
                {
                    "artifact_type": "selective_review_queue",
                    "bindings": bindings,
                },
            )

            validate_review_queue_bindings(queue_path, trusted_root=root)
            evidence.write_bytes(b"mutated-evidence!")

            with self.assertRaisesRegex(BroadcastApiError, "hash changed"):
                validate_review_queue_bindings(queue_path, trusted_root=root)

            evidence.write_bytes(b"original-evidence")
            dataset_path = inputs / "dataset.json"
            dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
            dataset["artifact_type"] = "candidate_dataset_manifest"
            _write_json(dataset_path, dataset)
            bindings["dataset"]["sha256"] = _sha256(dataset_path)
            _write_json(queue_path, {"artifact_type": "selective_review_queue", "bindings": bindings})

            with self.assertRaisesRegex(BroadcastApiError, "requires a candidate_dataset"):
                validate_review_queue_bindings(queue_path, trusted_root=root)


class BroadcastFacadeTests(unittest.TestCase):
    def test_status_root_creation_tolerates_a_concurrent_creator(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            raced_path = output_dir / "broadcast_status"
            original_mkdir = Path.mkdir

            def mkdir_after_concurrent_creation(
                path: Path,
                mode: int = 0o777,
                parents: bool = False,
                exist_ok: bool = False,
            ) -> None:
                if path == raced_path and not path.exists():
                    original_mkdir(path, mode=mode, parents=parents, exist_ok=False)
                original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

            with mock.patch.object(Path, "mkdir", mkdir_after_concurrent_creation):
                generation = _safe_status_generation_dir(output_dir, "a" * 64)

            self.assertTrue(generation.is_dir())

    def test_status_generation_creation_tolerates_a_concurrent_creator(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            status_root = output_dir / "broadcast_status"
            status_root.mkdir()
            raced_path = status_root / ("a" * 64)
            original_mkdir = Path.mkdir

            def mkdir_after_concurrent_creation(
                path: Path,
                mode: int = 0o777,
                parents: bool = False,
                exist_ok: bool = False,
            ) -> None:
                if path == raced_path and not path.exists():
                    original_mkdir(path, mode=mode, parents=parents, exist_ok=False)
                original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

            with mock.patch.object(Path, "mkdir", mkdir_after_concurrent_creation):
                generation = _safe_status_generation_dir(output_dir, "a" * 64)

            self.assertEqual(raced_path, generation)

    def test_status_paths_reject_regular_files_with_domain_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            status_root = output_dir / "broadcast_status"
            status_root.write_text("not a directory", encoding="utf-8")

            with self.assertRaisesRegex(BroadcastApiError, "status root"):
                _safe_status_generation_dir(output_dir, "a" * 64)

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            status_root = output_dir / "broadcast_status"
            status_root.mkdir()
            (status_root / ("a" * 64)).write_text("not a directory", encoding="utf-8")

            with self.assertRaisesRegex(BroadcastApiError, "status generation"):
                _safe_status_generation_dir(output_dir, "a" * 64)

    def test_facade_withholds_mutable_candidate_aliases_until_final_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            contract_path = output_dir / "tracking_contract.v2.json"
            predictions_path = output_dir / "candidate_predictions.v1.json"
            _write_json(
                contract_path,
                {
                    "schema_version": "2.0",
                    "source": {"video_sha256": "a" * 64},
                    "frames": [],
                    "candidates": [{"candidate_id": "candidate-1", "frame_index": 0}],
                    "classifications": [],
                    "decisions": [],
                    "validation_errors": [],
                },
            )
            _write_json(
                predictions_path,
                {
                    "schema_version": "1.0",
                    "artifact_type": "candidate_predictions",
                    "predictions": [{"candidate_id": "candidate-1", "predicted_label": "match_ball"}],
                },
            )

            report = publish_broadcast_facade(output_dir)

            self.assertEqual("needs_review", report["status"])
            self.assertIn("camera_solver_does_not_consume_action_track", report["limitations"])
            self.assertEqual("missing", report["artifacts"]["ball_candidates.jsonl"]["status"])
            self.assertEqual("missing", report["artifacts"]["candidate_classifications.jsonl"]["status"])
            self.assertFalse((output_dir / "ball_candidates.jsonl").exists())
            self.assertFalse((output_dir / "candidate_classifications.jsonl").exists())
            self.assertFalse((output_dir / "broadcast_quality_report.json").exists())
            self.assertTrue(
                (
                    output_dir / "broadcast_status" / report["status_generation"] / "broadcast_quality_report.json"
                ).is_file()
            )

            self.assertEqual(report, publish_broadcast_facade(output_dir))
            contract_path.write_text("{}", encoding="utf-8")
            changed = publish_broadcast_facade(output_dir)
            self.assertNotEqual(report["status_generation"], changed["status_generation"])
            self.assertEqual(
                report,
                json.loads(
                    (
                        output_dir / "broadcast_status" / report["status_generation"] / "broadcast_quality_report.json"
                    ).read_text(encoding="utf-8")
                ),
            )

    def test_status_root_symlink_is_rejected_before_external_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as external_dir:
            output_dir = Path(temp_dir)
            status_root = output_dir / "broadcast_status"
            try:
                status_root.symlink_to(Path(external_dir), target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")

            with self.assertRaisesRegex(BroadcastApiError, "status root"):
                publish_broadcast_facade(output_dir)
            self.assertEqual([], list(Path(external_dir).iterdir()))

    def test_facade_rejects_self_consistent_report_poisoned_into_another_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            report = publish_broadcast_facade(output_dir)
            original_generation = report["status_generation"]
            status_path = output_dir / "broadcast_status" / original_generation / "broadcast_quality_report.json"
            poisoned = {**report, "blocking_reasons": [*report["blocking_reasons"], "poisoned_state"]}
            stable = {key: value for key, value in poisoned.items() if key not in {"generated_at", "status_generation"}}
            poisoned["status_generation"] = hashlib.sha256(
                json.dumps(
                    stable,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            _write_json(status_path, poisoned)

            with self.assertRaisesRegex(BroadcastApiError, "immutable directory state"):
                publish_broadcast_facade(output_dir)

    def test_ready_root_report_requires_the_exact_immutable_generation_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            report = {
                "artifact_type": "broadcast_quality_report",
                "status": "ready",
                "status_generation": "a" * 64,
            }
            root_report = output_dir / "broadcast_quality_report.json"
            versioned_report = (
                output_dir / "broadcast_status" / str(report["status_generation"]) / "broadcast_quality_report.json"
            )
            _write_json(root_report, report)
            _write_json(versioned_report, report)

            with mock.patch("football_tracking.api.broadcast_api._verify_report_artifacts"):
                self.assertEqual(report, validate_broadcast_quality_report(output_dir, root_report))
                versioned_report.unlink()
                with self.assertRaisesRegex(BroadcastApiError, "versioned broadcast quality report"):
                    validate_broadcast_quality_report(output_dir, root_report)
                versioned_report.write_text(json.dumps(report, indent=2), encoding="utf-8")
                with self.assertRaisesRegex(BroadcastApiError, "does not match"):
                    validate_broadcast_quality_report(output_dir, root_report)


class BroadcastApiServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp_dir.name)
        for name in ("config", "data", "outputs", "weights"):
            (self.repo_root / name).mkdir()
        self.video = self.repo_root / "data" / "match.avi"
        writer = cv2.VideoWriter(str(self.video), cv2.VideoWriter_fourcc(*"MJPG"), 6.0, (64, 36))
        if not writer.isOpened():
            self.skipTest("OpenCV video writer is unavailable")
        for frame_index in range(12):
            writer.write(np.full((36, 64, 3), frame_index, dtype=np.uint8))
        writer.release()
        (self.repo_root / "weights" / "football_ball_yolo.pt").write_bytes(b"model")
        config = {
            "input_video": str(self.video),
            "output_dir": str(self.repo_root / "outputs" / "unused"),
            "detector": {"model_path": str(self.repo_root / "weights" / "football_ball_yolo.pt")},
            "output": {"save_tracking_contract": True},
        }
        (self.repo_root / "config" / "default.yaml").write_text(
            yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
        )
        self.service = ApiService(self.repo_root)
        self.services = [self.service]

    def tearDown(self) -> None:
        for service in reversed(self.services):
            service.close()
        self.temp_dir.cleanup()

    def wait_for_terminal_run(self, service: ApiService, run_id: str, *, timeout: float = 5.0) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = service.get_run(run_id)
            if run.get("status") in {"completed", "failed", "cancelled"}:
                return run
            time.sleep(0.01)
        self.fail(f"run did not reach a terminal state: {run_id}")

    def request(self, *, frames: list[int] | None = None) -> dict[str, object]:
        return {
            "config_name": "default.yaml",
            "input_video": str(self.video),
            "pipeline_mode": "broadcast_hybrid",
            "quality_profile": "stable_broadcast",
            "max_manual_review_windows": 30,
            "calibration_confirmation": {
                "source_resolution": [64, 36],
                "confirmed_sample_frames": frames or [0, 5, 11],
                "field_polygon": [[0, 0], [63, 0], [63, 35], [0, 35]],
                "exclusion_polygons": [],
            },
        }

    def test_preflight_failure_has_no_output_registry_or_thread_side_effects(self) -> None:
        with self.assertRaises(ValueError):
            self.service.create_run(self.request(frames=[0, 5, 12]))

        self.assertEqual([], self.service.list_runs())
        self.assertEqual([], list((self.repo_root / "outputs").iterdir()))
        self.assertEqual({}, self.service._active_threads)

    def test_two_live_services_cannot_queue_duplicate_initial_broadcast_runs(self) -> None:
        with mock.patch.object(self.service, "_start_thread_or_cleanup"):
            first = self.service.create_run(self.request())

        second_service = ApiService(self.repo_root)
        self.services.append(second_service)
        before_output_dirs = {
            path.resolve() for path in (self.repo_root / "outputs" / "runs").rglob("*") if path.is_dir()
        }
        with mock.patch.object(second_service, "_start_thread_or_cleanup"):
            with self.assertRaisesRegex(RuntimeError, "Another run is already active"):
                second_service.create_run(self.request())

        queued = [run for run in second_service.list_runs() if run.get("status") == "queued"]
        self.assertEqual([first["run_id"]], [run["run_id"] for run in queued])
        self.assertEqual(
            before_output_dirs,
            {path.resolve() for path in (self.repo_root / "outputs" / "runs").rglob("*") if path.is_dir()},
        )

    def test_remote_service_cancellation_stops_an_initial_broadcast_worker(self) -> None:
        entered = threading.Event()
        cancellation_seen = threading.Event()

        def blocking_runner(progress_callback=None, should_cancel=None) -> None:
            entered.set()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if should_cancel is not None and should_cancel():
                    cancellation_seen.set()
                    return
                time.sleep(0.01)
            raise RuntimeError("test runner did not observe cancellation")

        with mock.patch.object(self.service, "_tracking_runner", return_value=blocking_runner):
            queued = self.service.create_run(self.request())
            self.assertTrue(entered.wait(2.0))
            second_service = ApiService(self.repo_root)
            self.services.append(second_service)

            second_service.cancel_run(queued["run_id"])
            terminal = self.wait_for_terminal_run(self.service, queued["run_id"])

        self.assertTrue(cancellation_seen.is_set())
        self.assertEqual("cancelled", terminal["status"])
        self.assertEqual("cancelled", terminal["broadcast"]["status"])
        self.assertFalse((Path(terminal["output_dir"]) / "broadcast_quality_report.json").exists())

    def test_restart_marks_orphaned_initial_broadcast_failed_and_releases_queue(self) -> None:
        with mock.patch.object(self.service, "_start_thread_or_cleanup"):
            orphan = self.service.create_run(self.request())
        self.service.close()

        second_service = ApiService(self.repo_root)
        self.services.append(second_service)
        recovered = second_service.get_run(orphan["run_id"])

        self.assertEqual("failed", recovered["status"])
        self.assertEqual("interrupted_initial_run", recovered["broadcast"]["recovery_status"])
        self.assertIn("service restart", recovered["error"])
        with mock.patch.object(second_service, "_start_thread_or_cleanup"):
            replacement = second_service.create_run(self.request())
        self.assertEqual("queued", replacement["status"])

    def test_preflight_uses_effective_config_patch_before_any_side_effect(self) -> None:
        request = self.request()
        request["config_patch"] = {"output": {"save_tracking_contract": False}}
        with self.assertRaisesRegex(ValueError, "save_tracking_contract"):
            self.service.create_run(request)

        second_video = self.repo_root / "data" / "other.avi"
        writer = cv2.VideoWriter(str(second_video), cv2.VideoWriter_fourcc(*"MJPG"), 6.0, (32, 20))
        if not writer.isOpened():
            self.skipTest("OpenCV video writer is unavailable")
        for frame_index in range(12):
            writer.write(np.full((20, 32, 3), frame_index, dtype=np.uint8))
        writer.release()
        request = self.request()
        request.pop("input_video")
        request["config_patch"] = {"input_video": str(second_video)}
        with self.assertRaisesRegex(ValueError, "incompatible aspect ratio"):
            self.service.create_run(request)

        self.assertEqual([], self.service.list_runs())
        self.assertEqual([], list((self.repo_root / "outputs").iterdir()))
        self.assertEqual({}, self.service._active_threads)

    def test_broadcast_preflight_rejects_partial_source_ranges(self) -> None:
        request = self.request()
        request["start_frame"] = 1
        with self.assertRaisesRegex(ValueError, "start_frame=0"):
            self.service.create_run(request)

        request = self.request()
        request["config_patch"] = {"runtime": {"max_frames": 11}}
        with self.assertRaisesRegex(ValueError, "complete source video"):
            self.service.create_run(request)

        request = self.request()
        request["max_frames"] = 12
        with mock.patch.object(self.service, "_start_thread_or_cleanup") as starter:
            run = self.service.create_run(request)
        self.assertEqual("broadcast_hybrid", run["source"])
        starter.assert_called_once()

    def test_create_broadcast_run_records_preflight_before_thread_start(self) -> None:
        with mock.patch.object(self.service, "_start_thread_or_cleanup") as starter:
            run = self.service.create_run(self.request())

        self.assertEqual("broadcast_hybrid", run["source"])
        self.assertEqual("stable_broadcast", run["broadcast"]["quality_profile"])
        self.assertEqual([64, 36], run["broadcast"]["preflight"]["source_resolution"])
        starter.assert_called_once()

    def test_broadcast_execution_generates_source_bound_action_track_before_review(self) -> None:
        with mock.patch.object(self.service, "_start_thread_or_cleanup") as starter:
            run = self.service.create_run(self.request())
        thread = starter.call_args.args[1]

        def fake_tracking(progress_callback=None, should_cancel=None) -> None:
            self.assertFalse(should_cancel())
            contract = build_tracking_contract(
                source={
                    "video_sha256": _sha256(self.video),
                    "fps": 6.0,
                    "width": 64,
                    "height": 36,
                    "frame_count": 12,
                },
                frames=[{"frame_index": index, "status": "unknown"} for index in range(12)],
            )
            _write_json(Path(run["output_dir"]) / "tracking_contract.v2.json", contract)

        with mock.patch.object(self.service, "_tracking_runner", return_value=fake_tracking):
            thread.run()

        completed = self.service.get_run(run["run_id"])
        output_dir = Path(completed["output_dir"])
        self.assertEqual("completed", completed["status"], completed.get("error"))
        self.assertEqual("needs_review", completed["broadcast"]["status"])
        self.assertIn("missing_reviewed_classifier_predictions", completed["broadcast"]["blocking_reasons"])
        for name in (
            "action_calibration.v1.json",
            "action_track.csv",
            "action_signal_diagnostics.v1.jsonl",
            "action_signal_report.v1.json",
            "action_signal_binding.v1.json",
        ):
            self.assertTrue((output_dir / name).is_file(), name)
        binding = json.loads((output_dir / "action_signal_binding.v1.json").read_text(encoding="utf-8"))
        self.assertEqual(_sha256(self.video), binding["source"]["video_sha256"])
        self.assertFalse(completed["modules_enabled"]["follow_cam"])

    def test_action_signal_cancellation_publishes_no_partial_artifacts(self) -> None:
        output_dir = self.repo_root / "outputs" / "cancelled-action"
        calibration = self.request()["calibration_confirmation"]
        with self.assertRaises(CancelledError):
            generate_action_track(
                input_video=self.video,
                calibration=ActionCalibration.from_dict({"schema_version": "1.0", **calibration}),
                output_dir=output_dir,
                should_cancel=lambda: True,
            )
        self.assertFalse((output_dir / "action_track.csv").exists())
        self.assertFalse((output_dir / "action_signal_report.v1.json").exists())

    def test_review_actions_are_bound_and_corrections_fail_closed(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        self._register_broadcast_run(output_dir)
        bindings = {}
        names = (
            "review_timing",
            "policy",
            "decisions",
            "model",
            "training_report",
            "model_weights",
            "dataset",
            "predictions",
            "contract",
            "annotation_resolution",
            "resolved_tracking_contract",
            "policy_roles",
        )
        for name in names:
            path = output_dir / f"{name}.json"
            payload: object = {"name": name}
            if name == "dataset":
                payload = {"artifact_type": "candidate_dataset", "samples": []}
            path.write_text(json.dumps(payload), encoding="utf-8")
            bindings[name] = {"path": path.name, "sha256": _sha256(path)}
        queue_path = output_dir / "selective_review_queue.v1.json"
        _write_json(
            queue_path,
            {
                "schema_version": "1.0",
                "artifact_type": "selective_review_queue",
                "review_item_count": 1,
                "bindings": bindings,
                "items": [
                    {
                        "review_item_id": "window-1",
                        "candidates": [
                            {
                                "candidate_id": "candidate-1",
                                "candidate_fingerprint": "e" * 64,
                                "evidence": {"sha256": "f" * 64},
                            }
                        ],
                    }
                ],
            },
        )
        windows = self.service.get_broadcast_review_windows("broadcast-test")
        self.assertEqual("ready", windows["status"])

        oversized_queue = json.loads(queue_path.read_text(encoding="utf-8"))
        oversized_queue["items"].append({"review_item_id": "window-2", "candidates": []})
        oversized_queue["review_item_count"] = 2
        _write_json(queue_path, oversized_queue)
        limited = self.service.get_broadcast_review_windows("broadcast-test")
        self.assertEqual("invalid_selective_review_queue_window_count", limited["reason"])
        oversized_queue["items"].pop()
        oversized_queue["review_item_count"] = 1
        _write_json(queue_path, oversized_queue)

        with self.assertRaisesRegex(RuntimeError, "correct_trajectory"):
            self.service.submit_broadcast_review_actions(
                "broadcast-test",
                {
                    "actions": [
                        {
                            "action_id": "action-1",
                            "review_item_id": "window-1",
                            "candidate_id": "candidate-1",
                            "reviewer_id": "operator",
                            "created_at": "2026-07-10T00:00:00Z",
                            "action": "correct_trajectory",
                            "noise_subtype": None,
                            "keypoints": [{"frame_index": 2, "status": "detected", "x": 10.0, "y": 11.0}],
                        }
                    ]
                },
            )
        self.assertFalse((output_dir / "review_decisions.json").exists())

        empty_queue = json.loads(queue_path.read_text(encoding="utf-8"))
        empty_queue["items"] = []
        empty_queue["review_item_count"] = 0
        _write_json(queue_path, empty_queue)
        empty_windows = self.service.get_broadcast_review_windows("broadcast-test")
        self.assertEqual("ready", empty_windows["status"])
        self.assertEqual(0, empty_windows["review_item_count"])

        submitted = self.service.submit_broadcast_review_actions(
            "broadcast-test",
            {"actions": []},
        )
        self.assertEqual("review_actions_accepted", submitted["reason"])
        decisions = json.loads((output_dir / "review_decisions.json").read_text(encoding="utf-8"))
        self.assertEqual([], decisions["actions"])

    def test_camera_path_api_prefers_the_v2_public_camera_target(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        self._register_broadcast_run(output_dir)
        (output_dir / "camera_target.csv").write_text(
            "Frame,CenterX,CenterY,Status\n0,10,11,ball_guided\n1,12,13,wide_home_fallback\n",
            encoding="utf-8",
        )
        (output_dir / "camera_path.v2.csv").write_text(
            "Frame,CenterX,CenterY,Status\n0,20,21,v2\n",
            encoding="utf-8",
        )
        (output_dir / "camera_path.csv").write_text(
            "Frame,CenterX,CenterY,Status\n0,30,31,legacy\n",
            encoding="utf-8",
        )

        response = self.service.get_camera_path("broadcast-test", offset=1, limit=1)

        self.assertEqual(2, response["total_rows"])
        self.assertEqual("1", response["rows"][0]["Frame"])
        self.assertEqual("wide_home_fallback", response["rows"][0]["Status"])

        (output_dir / "camera_target.csv").unlink()
        response = self.service.get_camera_path("broadcast-test", offset=0, limit=1)
        self.assertEqual("v2", response["rows"][0]["Status"])

        (output_dir / "camera_path.v2.csv").unlink()
        response = self.service.get_camera_path("broadcast-test", offset=0, limit=1)
        self.assertEqual("legacy", response["rows"][0]["Status"])

    def test_nested_artifacts_require_an_explicit_broadcast_manifest_allowlist(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        self._register_broadcast_run(output_dir)
        private_path = output_dir / "private" / "secret.bin"
        private_path.parent.mkdir()
        private_path.write_bytes(b"secret")

        with self.assertRaises(FileNotFoundError):
            self.service.get_artifact_path("broadcast-test", "private/secret.bin")

        status = publish_broadcast_facade(output_dir)
        generation_id = status["status_generation"]
        status_path = output_dir / "broadcast_status" / generation_id / "broadcast_quality_report.json"
        names = {artifact["name"] for artifact in self.service.get_run("broadcast-test")["artifacts"]}
        self.assertIn(status_path.relative_to(output_dir).as_posix(), names)
        self.assertNotIn(private_path.relative_to(output_dir).as_posix(), names)
        self.assertEqual(
            status_path.resolve(),
            self.service.get_artifact_path("broadcast-test", status_path.relative_to(output_dir).as_posix()),
        )

    def test_ready_broadcast_artifact_access_revalidates_final_bindings(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        quality, _ = self._write_mock_ready_delivery_contract(output_dir)
        self._register_broadcast_run(output_dir)
        registry = self.service._read_registry()
        run = next(item for item in registry["runs"] if item["run_id"] == "broadcast-test")
        run["broadcast"] = {
            "status": "ready",
            "status_generation": quality["status_generation"],
        }
        self.service._write_registry(registry)

        with mock.patch(
            "football_tracking.api.service.validate_broadcast_quality_report",
            side_effect=BroadcastApiError("published broadcast artifact changed"),
        ):
            self.assertEqual([], self.service.list_artifacts("broadcast-test"))
            with self.assertRaises(FileNotFoundError):
                self.service.get_artifact_path("broadcast-test", "broadcast.mp4")

    def test_ready_broadcast_artifacts_require_the_run_status_generation(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        self._write_mock_ready_delivery_contract(output_dir)
        self._register_broadcast_run(output_dir)
        registry = self.service._read_registry()
        run = next(item for item in registry["runs"] if item["run_id"] == "broadcast-test")
        run["broadcast"] = {"status": "ready"}
        self.service._write_registry(registry)

        with mock.patch(
            "football_tracking.api.service.validate_broadcast_quality_report",
            side_effect=AssertionError("missing generation must reject before validation"),
        ):
            self.assertEqual([], self.service.list_artifacts("broadcast-test"))
            with self.assertRaises(FileNotFoundError):
                self.service.get_artifact_path("broadcast-test", "broadcast.mp4")
            with self.assertRaises(FileNotFoundError):
                self.service.acquire_artifact_response_lease("broadcast-test", "broadcast.mp4")

    def test_ready_broadcast_artifact_validation_reuses_a_sealed_delivery_snapshot(self) -> None:
        output_dir = self.repo_root / "outputs" / "ready-cache"
        output_dir.mkdir(parents=True)
        quality, _ = self._write_mock_ready_delivery_contract(output_dir)

        with mock.patch(
            "football_tracking.api.service.validate_broadcast_quality_report",
            return_value=quality,
        ) as full_validation:
            first = self.service._ready_broadcast_delivery_snapshots(
                output_dir,
                expected_status_generation=quality["status_generation"],
            )
            second = self.service._ready_broadcast_delivery_snapshots(
                output_dir,
                expected_status_generation=quality["status_generation"],
            )

        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        assert first is not None
        self.assertNotEqual((output_dir / "broadcast.mp4").resolve(), first["broadcast.mp4"])
        self.assertEqual((output_dir / "broadcast.mp4").read_bytes(), first["broadcast.mp4"].read_bytes())
        full_validation.assert_called_once_with(output_dir, output_dir / "broadcast_quality_report.json")

    def test_ready_broadcast_large_output_tree_uses_tokens_instead_of_one_lease_per_file(self) -> None:
        output_dir = self.repo_root / "outputs" / "ready-cache-large-tree"
        output_dir.mkdir(parents=True)
        quality, _ = self._write_mock_ready_delivery_contract(output_dir)
        samples_dir = output_dir / "candidate_samples"
        samples_dir.mkdir()
        for index in range(800):
            (samples_dir / f"sample-{index:04d}.bin").write_bytes(b"sample")

        with mock.patch(
            "football_tracking.api.service.validate_broadcast_quality_report",
            return_value=quality,
        ):
            snapshots = self.service._ready_broadcast_delivery_snapshots(
                output_dir,
                expected_status_generation=quality["status_generation"],
            )

        self.assertIsNotNone(snapshots)
        entry = next(iter(self.service._ready_broadcast_delivery_cache.values()))
        self.assertEqual(9, len(entry["dependencies"]))
        self.assertLess(len(entry["dependency_tokens"]), 32)
        self.assertIn(samples_dir.resolve(), entry["directory_tokens"])
        self.assertGreaterEqual(len(entry["output_inventory"]), 810)
        with mock.patch.object(
            self.service,
            "_ready_broadcast_output_inventory",
            side_effect=AssertionError("cache hit rescanned every output file"),
        ):
            repeated = self.service._ready_broadcast_delivery_snapshots(
                output_dir,
                expected_status_generation=str(quality["status_generation"]),
            )
        self.assertEqual(snapshots, repeated)

    def test_ready_broadcast_range_acquire_revalidates_the_full_lineage(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        quality, generation_payload = self._write_mock_ready_delivery_contract(output_dir)
        self._register_broadcast_run(output_dir)
        registry = self.service._read_registry()
        run = next(item for item in registry["runs"] if item["run_id"] == "broadcast-test")
        run["broadcast"] = {
            "status": "ready",
            "status_generation": quality["status_generation"],
        }
        self.service._write_registry(registry)

        with mock.patch(
            "football_tracking.api.service.validate_broadcast_quality_report",
            side_effect=[quality, BroadcastApiError("generation lineage changed")],
        ) as full_validation:
            self.assertEqual(8, len(self.service.list_artifacts("broadcast-test")))
            generation_payload.write_bytes(b"omega")
            with self.assertRaises(FileNotFoundError):
                self.service.acquire_artifact_response_lease(
                    "broadcast-test",
                    "broadcast.mp4",
                )

        self.assertEqual(2, full_validation.call_count)

    def test_ready_broadcast_cache_keeps_two_interleaved_generations_with_lru_eviction(self) -> None:
        outputs: list[Path] = []
        qualities: list[dict[str, object]] = []
        for index in range(3):
            output_dir = self.repo_root / "outputs" / f"ready-cache-lru-{index}"
            output_dir.mkdir(parents=True)
            quality, _ = self._write_mock_ready_delivery_contract(output_dir)
            outputs.append(output_dir.resolve())
            qualities.append(quality)

        with mock.patch(
            "football_tracking.api.service.validate_broadcast_quality_report",
            side_effect=qualities,
        ) as full_validation:
            first = self.service._ready_broadcast_delivery_snapshots(
                outputs[0],
                expected_status_generation=str(qualities[0]["status_generation"]),
            )
            second = self.service._ready_broadcast_delivery_snapshots(
                outputs[1],
                expected_status_generation=str(qualities[1]["status_generation"]),
            )
            first_again = self.service._ready_broadcast_delivery_snapshots(
                outputs[0],
                expected_status_generation=str(qualities[0]["status_generation"]),
            )
            third = self.service._ready_broadcast_delivery_snapshots(
                outputs[2],
                expected_status_generation=str(qualities[2]["status_generation"]),
            )

        self.assertEqual(first, first_again)
        self.assertIsNotNone(second)
        self.assertIsNotNone(third)
        self.assertEqual(3, full_validation.call_count)
        self.assertEqual(2, len(self.service._ready_broadcast_delivery_cache))
        cached_output_dirs = {key[0] for key in self.service._ready_broadcast_delivery_cache}
        self.assertEqual({outputs[0], outputs[2]}, cached_output_dirs)
        for entry in self.service._ready_broadcast_delivery_cache.values():
            self.assertEqual(9, len(entry["dependencies"]))
            self.assertEqual(9, len(entry["snapshot_leases"]))

    def test_ready_broadcast_snapshot_rejects_a_generation_mutation_during_validation(self) -> None:
        output_dir = self.repo_root / "outputs" / "ready-cache-race"
        output_dir.mkdir(parents=True)
        quality, generation_payload = self._write_mock_ready_delivery_contract(output_dir)
        original_stat = generation_payload.stat()
        mutation_blocked = False

        def mutate_generation(*_args: object, **_kwargs: object) -> dict[str, object]:
            nonlocal mutation_blocked
            try:
                generation_payload.write_bytes(b"omega")
                os.utime(
                    generation_payload,
                    ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                )
            except OSError:
                mutation_blocked = True
            return quality

        with mock.patch(
            "football_tracking.api.service.validate_broadcast_quality_report",
            side_effect=mutate_generation,
        ):
            snapshots = self.service._ready_broadcast_delivery_snapshots(
                output_dir,
                expected_status_generation=quality["status_generation"],
            )

        if mutation_blocked:
            self.assertIsNotNone(snapshots)
            self.assertEqual(b"alpha", generation_payload.read_bytes())
        else:
            self.assertIsNone(snapshots)
            self.assertEqual({}, self.service._ready_broadcast_delivery_cache)

    def test_ready_broadcast_snapshot_rejects_a_generation_directory_aba(self) -> None:
        output_dir = self.repo_root / "outputs" / "ready-cache-directory-aba"
        output_dir.mkdir(parents=True)
        quality, generation_payload = self._write_mock_ready_delivery_contract(output_dir)
        generation_dir = generation_payload.parent
        saved_dir = generation_dir.with_name("render-saved")
        replacement_dir = generation_dir.with_name("render-replacement")
        replacement_dir.mkdir()
        (replacement_dir / generation_payload.name).write_bytes(b"omega")
        mutation_blocked = False

        def swap_and_restore_generation(*_args: object, **_kwargs: object) -> dict[str, object]:
            nonlocal mutation_blocked
            try:
                generation_dir.rename(saved_dir)
                replacement_dir.rename(generation_dir)
                generation_dir.rename(replacement_dir)
                saved_dir.rename(generation_dir)
            except OSError:
                mutation_blocked = True
            return quality

        with mock.patch(
            "football_tracking.api.service.validate_broadcast_quality_report",
            side_effect=swap_and_restore_generation,
        ):
            snapshots = self.service._ready_broadcast_delivery_snapshots(
                output_dir,
                expected_status_generation=quality["status_generation"],
            )

        if mutation_blocked:
            self.assertIsNotNone(snapshots)
        else:
            self.assertIsNone(snapshots)
            self.assertEqual({}, self.service._ready_broadcast_delivery_cache)

    def test_ready_broadcast_cache_detects_a_transitive_generation_mutation(self) -> None:
        output_dir = self.repo_root / "outputs" / "ready-cache-lineage"
        output_dir.mkdir(parents=True)
        quality, generation_payload = self._write_mock_ready_delivery_contract(output_dir)
        original_stat = generation_payload.stat()

        with mock.patch(
            "football_tracking.api.service.validate_broadcast_quality_report",
            side_effect=[quality, BroadcastApiError("generation lineage changed")],
        ) as full_validation:
            first = self.service._ready_broadcast_delivery_snapshots(
                output_dir,
                expected_status_generation=quality["status_generation"],
            )
            self.assertIsNotNone(first)
            mutation_blocked = False
            try:
                generation_payload.write_bytes(b"omega")
                os.utime(
                    generation_payload,
                    ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                )
            except OSError:
                mutation_blocked = True
            second = self.service._ready_broadcast_delivery_snapshots(
                output_dir,
                expected_status_generation=quality["status_generation"],
            )

        if mutation_blocked:
            self.assertEqual(first, second)
            self.assertEqual(1, full_validation.call_count)
        else:
            self.assertIsNone(second)
            self.assertEqual(2, full_validation.call_count)

    def test_ready_broadcast_cache_rejects_an_unexpected_generation_file(self) -> None:
        output_dir = self.repo_root / "outputs" / "ready-cache-inventory"
        output_dir.mkdir(parents=True)
        quality, generation_payload = self._write_mock_ready_delivery_contract(output_dir)

        with mock.patch(
            "football_tracking.api.service.validate_broadcast_quality_report",
            side_effect=[quality, BroadcastApiError("unexpected generation artifact")],
        ) as full_validation:
            first = self.service._ready_broadcast_delivery_snapshots(
                output_dir,
                expected_status_generation=quality["status_generation"],
            )
            self.assertIsNotNone(first)
            (generation_payload.parent / "unexpected.bin").write_bytes(b"unexpected")
            second = self.service._ready_broadcast_delivery_snapshots(
                output_dir,
                expected_status_generation=quality["status_generation"],
            )

        self.assertIsNone(second)
        self.assertEqual(2, full_validation.call_count)

    def test_ready_broadcast_cache_tracks_external_queue_bindings(self) -> None:
        output_dir = self.repo_root / "outputs" / "ready-cache-external"
        output_dir.mkdir(parents=True)
        quality, _ = self._write_mock_ready_delivery_contract(output_dir)
        external_policy = self._write_mock_ready_queue(output_dir)
        original_stat = external_policy.stat()

        with mock.patch(
            "football_tracking.api.service.validate_broadcast_quality_report",
            side_effect=[quality, BroadcastApiError("external queue binding changed")],
        ) as full_validation:
            first = self.service._ready_broadcast_delivery_snapshots(
                output_dir,
                expected_status_generation=quality["status_generation"],
            )
            self.assertIsNotNone(first)
            mutation_blocked = False
            try:
                external_policy.write_bytes(b"x" * original_stat.st_size)
                os.utime(
                    external_policy,
                    ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                )
            except OSError:
                mutation_blocked = True
            second = self.service._ready_broadcast_delivery_snapshots(
                output_dir,
                expected_status_generation=quality["status_generation"],
            )

        if mutation_blocked:
            self.assertEqual(first, second)
            self.assertEqual(1, full_validation.call_count)
        else:
            self.assertIsNone(second)
            self.assertEqual(1, full_validation.call_count)

    def test_ready_broadcast_snapshot_invalidation_rebuilds_into_a_fresh_directory(self) -> None:
        output_dir = self.repo_root / "outputs" / "ready-cache-rebuild"
        output_dir.mkdir(parents=True)
        quality, _ = self._write_mock_ready_delivery_contract(output_dir)

        with mock.patch(
            "football_tracking.api.service.validate_broadcast_quality_report",
            return_value=quality,
        ) as full_validation:
            first = self.service._ready_broadcast_delivery_snapshots(
                output_dir,
                expected_status_generation=quality["status_generation"],
            )
            self.assertIsNotNone(first)
            assert first is not None
            first_video = first["broadcast.mp4"]
            cache_entry = next(iter(self.service._ready_broadcast_delivery_cache.values()))
            cache_entry["snapshot_tokens"]["broadcast.mp4"] = (-1, -1, -1, -1, -1)
            second = self.service._ready_broadcast_delivery_snapshots(
                output_dir,
                expected_status_generation=quality["status_generation"],
            )

        self.assertIsNotNone(second)
        assert second is not None
        self.assertNotEqual(first_video.parent, second["broadcast.mp4"].parent)
        self.assertEqual((output_dir / "broadcast.mp4").read_bytes(), second["broadcast.mp4"].read_bytes())
        self.assertEqual(2, full_validation.call_count)

    def test_ready_broadcast_snapshot_same_size_rewrite_is_blocked_or_invalidated(self) -> None:
        output_dir = self.repo_root / "outputs" / "ready-cache-snapshot-aba"
        output_dir.mkdir(parents=True)
        quality, _ = self._write_mock_ready_delivery_contract(output_dir)

        with mock.patch(
            "football_tracking.api.service.validate_broadcast_quality_report",
            return_value=quality,
        ) as full_validation:
            first = self.service._ready_broadcast_delivery_snapshots(
                output_dir,
                expected_status_generation=quality["status_generation"],
            )
            self.assertIsNotNone(first)
            assert first is not None
            first_video = first["broadcast.mp4"]
            original = first_video.read_bytes()
            original_stat = first_video.stat()
            mutation_blocked = False
            try:
                first_video.write_bytes(b"x" * len(original))
                os.utime(first_video, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            except OSError:
                mutation_blocked = True
            second = self.service._ready_broadcast_delivery_snapshots(
                output_dir,
                expected_status_generation=quality["status_generation"],
            )

        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual(original, second["broadcast.mp4"].read_bytes())
        self.assertEqual(1 if mutation_blocked else 2, full_validation.call_count)

    def test_ready_broadcast_artifact_path_is_a_private_validated_snapshot(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        quality, _ = self._write_mock_ready_delivery_contract(output_dir)
        self._register_broadcast_run(output_dir)
        registry = self.service._read_registry()
        run = next(item for item in registry["runs"] if item["run_id"] == "broadcast-test")
        run["broadcast"] = {
            "status": "ready",
            "status_generation": quality["status_generation"],
        }
        self.service._write_registry(registry)

        with mock.patch(
            "football_tracking.api.service.validate_broadcast_quality_report",
            return_value=quality,
        ) as full_validation:
            listed_run = next(run for run in self.service.list_runs() if run["run_id"] == "broadcast-test")
            listed_artifacts = self.service.list_artifacts("broadcast-test")
            snapshot_path = self.service.get_artifact_path("broadcast-test", "broadcast.mp4")
            repeated_path = self.service.get_artifact_path("broadcast-test", "broadcast.mp4")
            ranged_response = get_artifact("broadcast-test", "broadcast.mp4", self.service)
            ranged_handle = ranged_response._lease.handle
            ranged_status, ranged_headers, ranged_body = self._invoke_artifact_response(
                ranged_response,
                method="GET",
                headers=[(b"range", b"bytes=2-7")],
            )
            headed_response = get_artifact("broadcast-test", "broadcast.mp4", self.service)
            headed_handle = headed_response._lease.handle
            headed_status, headed_headers, headed_body = self._invoke_artifact_response(
                headed_response,
                method="HEAD",
            )
            truncated_response = get_artifact("broadcast-test", "broadcast.mp4", self.service)
            truncated_handle = truncated_response._lease.handle
            with (
                mock.patch.object(truncated_response, "_read", new=mock.AsyncMock(return_value=b"")),
                self.assertRaisesRegex(RuntimeError, "ended before the declared range"),
            ):
                self._invoke_artifact_response(
                    truncated_response,
                    method="GET",
                    headers=[(b"range", b"bytes=0-1,4-5")],
                )
            partial_response = get_artifact("broadcast-test", "broadcast.mp4", self.service)
            partial_handle = partial_response._lease.handle
            with (
                mock.patch.object(
                    partial_response,
                    "_read",
                    new=mock.AsyncMock(side_effect=[b"x", b""]),
                ),
                self.assertRaisesRegex(RuntimeError, "ended before the declared range"),
            ):
                self._invoke_artifact_response(
                    partial_response,
                    method="GET",
                    headers=[(b"range", b"bytes=0-4")],
                )

        self.assertEqual(snapshot_path, repeated_path)
        self.assertEqual([], listed_run["artifacts"])
        self.assertEqual(
            {*PUBLIC_ARTIFACTS, "broadcast_quality_report.json"},
            {artifact["name"] for artifact in listed_artifacts},
        )
        self.assertNotEqual((output_dir / "broadcast.mp4").resolve(), snapshot_path)
        self.assertEqual((output_dir / "broadcast.mp4").read_bytes(), snapshot_path.read_bytes())
        expected_video = (output_dir / "broadcast.mp4").read_bytes()
        self.assertEqual(206, ranged_status)
        self.assertEqual(expected_video[2:8], ranged_body)
        self.assertEqual(f"bytes 2-7/{len(expected_video)}", ranged_headers["content-range"])
        self.assertEqual(200, headed_status)
        self.assertEqual(b"", headed_body)
        self.assertEqual(str(len(expected_video)), headed_headers["content-length"])
        self.assertTrue(ranged_handle.closed)
        self.assertTrue(headed_handle.closed)
        self.assertTrue(truncated_handle.closed)
        self.assertTrue(partial_handle.closed)
        full_validation.assert_called_once()

    def test_ready_broadcast_cache_releases_leases_before_run_output_deletion(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        quality, _ = self._write_mock_ready_delivery_contract(output_dir)
        self._register_broadcast_run(output_dir)
        registry = self.service._read_registry()
        run = next(item for item in registry["runs"] if item["run_id"] == "broadcast-test")
        run["broadcast"] = {"status": "ready", "status_generation": quality["status_generation"]}
        self.service._write_registry(registry)

        with mock.patch(
            "football_tracking.api.service.validate_broadcast_quality_report",
            return_value=quality,
        ):
            self.assertEqual(8, len(self.service.list_artifacts("broadcast-test")))
            deleted = self.service.delete_run_output("broadcast-test")

        self.assertTrue(deleted["deleted"])
        self.assertFalse(output_dir.exists())
        self.assertEqual({}, self.service._ready_broadcast_delivery_cache)
        self.assertNotIn(
            "broadcast-test",
            {item.get("run_id") for item in self.service._read_registry()["runs"]},
        )

    def test_ready_broadcast_cache_releases_source_lease_before_input_deletion(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        quality, _ = self._write_mock_ready_delivery_contract(output_dir)
        self._register_broadcast_run(output_dir)
        registry = self.service._read_registry()
        run = next(item for item in registry["runs"] if item["run_id"] == "broadcast-test")
        run["broadcast"] = {"status": "ready", "status_generation": quality["status_generation"]}
        self.service._write_registry(registry)

        with mock.patch(
            "football_tracking.api.service.validate_broadcast_quality_report",
            return_value=quality,
        ):
            self.assertEqual(8, len(self.service.list_artifacts("broadcast-test")))
            deleted = self.service.delete_input_video("match.avi")

        self.assertTrue(deleted["deleted"])
        self.assertFalse(self.video.exists())
        self.assertEqual({}, self.service._ready_broadcast_delivery_cache)

    def test_ready_broadcast_eviction_does_not_retain_unrelated_snapshots_during_a_slow_response(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        quality, _ = self._write_mock_ready_delivery_contract(output_dir)
        self._register_broadcast_run(output_dir)
        registry = self.service._read_registry()
        run = next(item for item in registry["runs"] if item["run_id"] == "broadcast-test")
        run["broadcast"] = {"status": "ready", "status_generation": quality["status_generation"]}
        self.service._write_registry(registry)

        with mock.patch(
            "football_tracking.api.service.validate_broadcast_quality_report",
            return_value=quality,
        ):
            response_lease = self.service.acquire_artifact_response_lease("broadcast-test", "broadcast.mp4")
            for index in range(4):
                other_output = self.repo_root / "outputs" / f"ready-cache-slow-response-{index}"
                other_output.mkdir()
                other_quality, _ = self._write_mock_ready_delivery_contract(other_output)
                self.assertIsNotNone(
                    self.service._ready_broadcast_delivery_snapshots(
                        other_output,
                        expected_status_generation=str(other_quality["status_generation"]),
                    )
                )

        snapshot_root = self.service._ready_broadcast_delivery_temp
        assert snapshot_root is not None
        self.assertLessEqual(len(list(snapshot_root.iterdir())), 3)
        self.assertLessEqual(len(self.service._ready_broadcast_retired_snapshot_dirs), 1)

        response_lease.close()

        self.assertEqual(2, len(list(snapshot_root.iterdir())))
        self.assertEqual(set(), self.service._ready_broadcast_retired_snapshot_dirs)

    def test_service_close_defers_ready_snapshot_cleanup_until_response_release(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        quality, _ = self._write_mock_ready_delivery_contract(output_dir)
        self._register_broadcast_run(output_dir)
        registry = self.service._read_registry()
        run = next(item for item in registry["runs"] if item["run_id"] == "broadcast-test")
        run["broadcast"] = {"status": "ready", "status_generation": quality["status_generation"]}
        self.service._write_registry(registry)

        with mock.patch(
            "football_tracking.api.service.validate_broadcast_quality_report",
            return_value=quality,
        ):
            response_lease = self.service.acquire_artifact_response_lease("broadcast-test", "broadcast.mp4")
        snapshot_root = self.service._ready_broadcast_delivery_temp
        assert snapshot_root is not None

        self.service.close()
        self.assertTrue(snapshot_root.exists())
        response_lease.close()

        self.assertFalse(snapshot_root.exists())
        self.assertTrue(response_lease.closed)

    def test_recompute_queues_a_cancellable_child_and_completes_in_the_background(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        self._register_broadcast_run(output_dir)
        frozen = {
            "queue_sha256": "a" * 64,
            "review_decisions_sha256": "b" * 64,
            "source_video_sha256": "c" * 64,
        }
        result = {
            "status": "completed",
            "trajectory_generation_id": "trajectory-" + "d" * 24,
        }
        quality = {
            "status": "needs_review",
            "blocking_reasons": ["missing_hybrid_camera_path"],
            "limitations": ["camera_solver_does_not_consume_action_track"],
            "status_generation": "e" * 64,
        }
        operation_holder: dict[str, str] = {}
        late_cancel_errors: list[str] = []
        commit_attempts = 0

        def committed_recompute(*_args: object, **kwargs: object) -> dict[str, object]:
            before_commit = kwargs["before_commit"]
            self.assertTrue(callable(before_commit))
            before_commit()
            try:
                self.service.cancel_run(operation_holder["run_id"])
            except RuntimeError as exc:
                late_cancel_errors.append(str(exc))
            return result

        def flaky_registry_commit(**kwargs: object) -> None:
            nonlocal commit_attempts
            commit_attempts += 1
            raise OSError("injected persistent registry failure")

        with (
            mock.patch(
                "football_tracking.api.service.preflight_recompute_reviewed_trajectory",
                return_value=frozen,
            ),
            mock.patch(
                "football_tracking.api.service.preflight_render_broadcast_trajectory",
                return_value={"trajectory_generation_id": result["trajectory_generation_id"]},
            ),
            mock.patch(
                "football_tracking.api.service.recompute_reviewed_trajectory",
                side_effect=committed_recompute,
            ) as recompute,
            mock.patch("football_tracking.api.service.publish_broadcast_facade", return_value=quality),
            mock.patch.object(
                self.service,
                "_commit_broadcast_operation_registry",
                side_effect=flaky_registry_commit,
            ),
            mock.patch.object(self.service, "_start_thread_or_cleanup") as starter,
        ):
            response = self.service.recompute_broadcast_trajectory(
                "broadcast-test",
                {"review_decisions_sha256": "b" * 64},
            )
            operation_run_id = response["run_id"]
            operation_holder["run_id"] = operation_run_id
            self.assertEqual("queued", response["status"])
            self.assertEqual("broadcast-test", response["parent_run_id"])
            self.assertEqual("queued", self.service.get_run(operation_run_id)["status"])
            with self.assertRaisesRegex(RuntimeError, "child operation output"):
                self.service.delete_run_output("broadcast-test")

            thread = starter.call_args.args[1]
            thread.run()
            self.wait_for_terminal_run(self.service, operation_run_id)

        child = self.service.get_run(operation_run_id)
        parent = self.service.get_run("broadcast-test")
        self.assertEqual("completed", child["status"], child.get("error"))
        self.assertEqual("broadcast_hybrid_recompute", child["source"])
        self.assertEqual(result["trajectory_generation_id"], parent["broadcast"]["trajectory_generation_id"])
        self.assertEqual("trajectory_ready", parent["broadcast"]["status"])
        self.assertTrue((Path(child["output_dir"]) / "broadcast_operation_report.v1.json").is_file())
        self.assertTrue(callable(recompute.call_args.kwargs["should_cancel"]))
        self.assertEqual(1, len(late_cancel_errors))
        self.assertIn("commit has already started", late_cancel_errors[0])
        self.assertEqual(2, commit_attempts)
        with self.assertRaisesRegex(RuntimeError, "child operation output"):
            self.service.delete_run_output("broadcast-test")

    def test_succeeded_report_remains_reconcilable_after_persistent_atomic_registry_failure(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        self._register_broadcast_run(output_dir)
        frozen = {"review_decisions_sha256": "b" * 64}
        result = {"status": "completed", "trajectory_generation_id": "trajectory-" + "d" * 24}
        quality = {
            "status": "needs_review",
            "blocking_reasons": ["missing_hybrid_camera_path"],
            "limitations": [],
            "status_generation": "e" * 64,
        }

        def committed_recompute(*_args: object, **kwargs: object) -> dict[str, object]:
            kwargs["before_commit"]()
            return result

        with (
            mock.patch(
                "football_tracking.api.service.preflight_recompute_reviewed_trajectory",
                return_value=frozen,
            ),
            mock.patch(
                "football_tracking.api.service.preflight_render_broadcast_trajectory",
                return_value={"trajectory_generation_id": result["trajectory_generation_id"]},
            ),
            mock.patch(
                "football_tracking.api.service.recompute_reviewed_trajectory",
                side_effect=committed_recompute,
            ),
            mock.patch("football_tracking.api.service.publish_broadcast_facade", return_value=quality),
            mock.patch.object(
                self.service,
                "_commit_broadcast_operation_registry_atomic",
                side_effect=OSError("injected persistent atomic registry failure"),
            ),
            mock.patch.object(self.service, "_start_thread_or_cleanup") as starter,
        ):
            response = self.service.recompute_broadcast_trajectory(
                "broadcast-test",
                {"review_decisions_sha256": "b" * 64},
            )
            starter.call_args.args[1].run()

        operation_run_id = response["run_id"]
        pending = self.service.get_run(operation_run_id)
        self.assertEqual("running", pending["status"])
        self.assertEqual("reconciling", pending["broadcast"]["operation_status"])
        self.assertTrue(pending["broadcast"]["worker_exited"])
        self.assertTrue((Path(pending["output_dir"]) / "broadcast_operation_report.v1.json").is_file())
        self.assertNotIn("trajectory_generation_id", self.service.get_run("broadcast-test")["broadcast"])

        with (
            mock.patch.object(ApiService, "_owner_lease_is_active", return_value=False),
            mock.patch(
                "football_tracking.api.service.preflight_render_broadcast_trajectory",
                return_value={"trajectory_generation_id": result["trajectory_generation_id"]},
            ),
            mock.patch("football_tracking.api.service.publish_broadcast_facade", return_value=quality),
        ):
            recovered_service = ApiService(self.repo_root)
            self.services.append(recovered_service)
            recovered = self.wait_for_terminal_run(recovered_service, operation_run_id)

        self.assertEqual("completed", recovered["status"])
        self.assertEqual(
            result["trajectory_generation_id"],
            recovered_service.get_run("broadcast-test")["broadcast"]["trajectory_generation_id"],
        )

    def test_cancelled_render_child_never_calls_the_orchestrator(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        self._register_broadcast_run(output_dir)
        generation_id = "trajectory-" + "d" * 24
        frozen = {
            "trajectory_generation_id": generation_id,
            "trajectory_report_sha256": "a" * 64,
            "target_width": 1920,
            "target_height": 1080,
        }
        with (
            mock.patch(
                "football_tracking.api.service.preflight_render_broadcast_trajectory",
                return_value=frozen,
            ),
            mock.patch("football_tracking.api.service.render_broadcast_trajectory") as render,
            mock.patch.object(self.service, "_start_thread_or_cleanup") as starter,
        ):
            response = self.service.render_broadcast_hybrid(
                "broadcast-test",
                {
                    "trajectory_generation_id": generation_id,
                    "target_width": 1920,
                    "target_height": 1080,
                },
            )
            operation_run_id = response["run_id"]
            self.service.cancel_run(operation_run_id)
            starter.call_args.args[1].run()

        child = self.service.get_run(operation_run_id)
        self.assertEqual("cancelled", child["status"])
        self.assertFalse((Path(child["output_dir"]) / "broadcast_operation_report.v1.json").exists())
        render.assert_not_called()

    def test_two_live_services_cannot_queue_duplicate_broadcast_children(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        self._register_broadcast_run(output_dir)
        frozen = {"review_decisions_sha256": "b" * 64}
        with (
            mock.patch(
                "football_tracking.api.service.preflight_recompute_reviewed_trajectory",
                return_value=frozen,
            ),
            mock.patch.object(self.service, "_start_thread_or_cleanup"),
        ):
            first = self.service.recompute_broadcast_trajectory(
                "broadcast-test",
                {"review_decisions_sha256": "b" * 64},
            )

        second_service = ApiService(self.repo_root)
        self.services.append(second_service)
        with (
            mock.patch(
                "football_tracking.api.service.preflight_recompute_reviewed_trajectory",
                return_value=frozen,
            ),
            mock.patch.object(second_service, "_start_thread_or_cleanup"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Another run is already active"):
                second_service.recompute_broadcast_trajectory(
                    "broadcast-test",
                    {"review_decisions_sha256": "b" * 64},
                )

        children = [run for run in second_service.list_runs() if run.get("source") == "broadcast_hybrid_recompute"]
        self.assertEqual([first["run_id"]], [run["run_id"] for run in children])

    def test_remote_cancel_is_not_overwritten_when_the_owner_claims_its_child(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        self._register_broadcast_run(output_dir)
        generation_id = "trajectory-" + "d" * 24
        frozen = {
            "trajectory_generation_id": generation_id,
            "target_width": 1920,
            "target_height": 1080,
        }
        with (
            mock.patch(
                "football_tracking.api.service.preflight_render_broadcast_trajectory",
                return_value=frozen,
            ),
            mock.patch("football_tracking.api.service.render_broadcast_trajectory") as render,
            mock.patch.object(self.service, "_start_thread_or_cleanup") as starter,
        ):
            response = self.service.render_broadcast_hybrid(
                "broadcast-test",
                {
                    "trajectory_generation_id": generation_id,
                    "target_width": 1920,
                    "target_height": 1080,
                },
            )
            second_service = ApiService(self.repo_root)
            self.services.append(second_service)
            cancelled = second_service.cancel_run(response["run_id"])
            self.assertTrue(cancelled["broadcast"]["cancel_requested"])
            starter.call_args.args[1].run()

        child = self.service.get_run(response["run_id"])
        self.assertEqual("cancelled", child["status"])
        self.assertTrue(child["broadcast"]["cancel_requested"])
        render.assert_not_called()

    def test_operation_report_io_failure_never_publishes_registry_success(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        self._register_broadcast_run(output_dir)
        frozen = {"review_decisions_sha256": "b" * 64}
        result = {"status": "completed", "trajectory_generation_id": "trajectory-" + "d" * 24}
        quality = {
            "status": "needs_review",
            "blocking_reasons": ["missing_hybrid_camera_path"],
            "limitations": [],
            "status_generation": "e" * 64,
        }

        def committed_recompute(*_args: object, **kwargs: object) -> dict[str, object]:
            kwargs["before_commit"]()
            return result

        with (
            mock.patch(
                "football_tracking.api.service.preflight_recompute_reviewed_trajectory",
                return_value=frozen,
            ),
            mock.patch(
                "football_tracking.api.service.preflight_render_broadcast_trajectory",
                return_value={"trajectory_generation_id": result["trajectory_generation_id"]},
            ),
            mock.patch(
                "football_tracking.api.service.recompute_reviewed_trajectory",
                side_effect=committed_recompute,
            ),
            mock.patch("football_tracking.api.service.publish_broadcast_facade", return_value=quality),
            mock.patch.object(self.service, "_start_thread_or_cleanup") as starter,
        ):
            response = self.service.recompute_broadcast_trajectory(
                "broadcast-test",
                {"review_decisions_sha256": "b" * 64},
            )
            with mock.patch(
                "football_tracking.api.service.publish_json_exclusive",
                side_effect=OSError("injected operation report failure"),
            ):
                starter.call_args.args[1].run()

        child = self.service.get_run(response["run_id"])
        parent = self.service.get_run("broadcast-test")
        self.assertEqual("failed", child["status"])
        self.assertIn("operation report failure", child["error"])
        self.assertNotIn("trajectory_generation_id", parent.get("broadcast", {}))

    def test_ready_render_remains_authoritative_when_operation_report_io_fails(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        self._register_broadcast_run(output_dir)
        generation_id = "trajectory-" + "d" * 24
        frozen = {
            "trajectory_generation_id": generation_id,
            "target_width": 1920,
            "target_height": 1080,
        }
        result = {
            "status": "completed",
            "trajectory_generation_id": generation_id,
            "camera_generation_id": "camera-" + "c" * 24,
            "render_generation_id": "render-" + "e" * 24,
        }
        quality = {
            "status": "ready",
            "blocking_reasons": [],
            "limitations": [],
            "status_generation": "f" * 64,
        }

        def committed_render(*_args: object, **kwargs: object) -> dict[str, object]:
            kwargs["before_commit"]()
            return result

        with (
            mock.patch(
                "football_tracking.api.service.preflight_render_broadcast_trajectory",
                return_value=frozen,
            ),
            mock.patch(
                "football_tracking.api.service.render_broadcast_trajectory",
                side_effect=committed_render,
            ),
            mock.patch("football_tracking.api.service.publish_broadcast_facade", return_value=quality),
            mock.patch(
                "football_tracking.api.service.validate_broadcast_quality_report",
                return_value=quality,
            ),
            mock.patch.object(self.service, "_start_thread_or_cleanup") as starter,
        ):
            response = self.service.render_broadcast_hybrid(
                "broadcast-test",
                {
                    "trajectory_generation_id": generation_id,
                    "target_width": 1920,
                    "target_height": 1080,
                },
            )
            with mock.patch(
                "football_tracking.api.service.publish_json_exclusive",
                side_effect=OSError("injected operation report failure"),
            ):
                starter.call_args.args[1].run()

        child = self.service.get_run(response["run_id"])
        parent = self.service.get_run("broadcast-test")
        self.assertEqual("completed", child["status"], child.get("error"))
        self.assertEqual("missing_after_ready_commit", child["broadcast"]["operation_report_status"])
        self.assertIn("Ready render is authoritative", child["broadcast"]["metadata_warnings"][0])
        self.assertEqual("ready", parent["broadcast"]["status"])
        self.assertEqual(result["render_generation_id"], parent["broadcast"]["render_generation_id"])
        self.assertFalse((Path(child["output_dir"]) / "broadcast_operation_report.v1.json").exists())
        with self.assertRaisesRegex(RuntimeError, "already ready and immutable"):
            self.service.render_broadcast_hybrid(
                "broadcast-test",
                {
                    "trajectory_generation_id": generation_id,
                    "target_width": 1920,
                    "target_height": 1080,
                },
            )

    def test_ready_render_retries_reconciliation_after_report_and_registry_failures_clear(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        self._register_broadcast_run(output_dir)
        generation_id = "trajectory-" + "d" * 24
        frozen = {
            "trajectory_generation_id": generation_id,
            "target_width": 1920,
            "target_height": 1080,
        }
        final_manifest = {
            "artifact_type": "broadcast_artifact_bindings",
            "generation_ids": {
                "trajectory": generation_id,
                "camera": "camera-" + "c" * 24,
                "render": "render-" + "e" * 24,
            },
            "artifacts": {},
        }
        result = {
            "status": "completed",
            "trajectory_generation_id": generation_id,
            "camera_generation_id": final_manifest["generation_ids"]["camera"],
            "render_generation_id": final_manifest["generation_ids"]["render"],
        }
        quality = {
            "status": "ready",
            "blocking_reasons": [],
            "limitations": [],
            "status_generation": "f" * 64,
        }
        registry_available = False
        real_atomic_commit = self.service._commit_broadcast_operation_registry_atomic

        def committed_render(*_args: object, **kwargs: object) -> dict[str, object]:
            kwargs["before_commit"]()
            _write_json(output_dir / "broadcast_artifact_bindings.v1.json", final_manifest)
            return result

        def fail_until_released(**kwargs: object) -> None:
            if not registry_available:
                raise OSError("injected persistent atomic registry failure")
            real_atomic_commit(**kwargs)

        with (
            mock.patch(
                "football_tracking.api.service.preflight_render_broadcast_trajectory",
                return_value=frozen,
            ),
            mock.patch(
                "football_tracking.api.service.render_broadcast_trajectory",
                side_effect=committed_render,
            ),
            mock.patch("football_tracking.api.service.publish_broadcast_facade", return_value=quality),
            mock.patch(
                "football_tracking.api.service.validate_broadcast_quality_report",
                return_value=quality,
            ),
            mock.patch.object(
                self.service,
                "_commit_broadcast_operation_registry_atomic",
                side_effect=fail_until_released,
            ),
            mock.patch.object(self.service, "_start_thread_or_cleanup") as starter,
        ):
            response = self.service.render_broadcast_hybrid(
                "broadcast-test",
                {
                    "trajectory_generation_id": generation_id,
                    "target_width": 1920,
                    "target_height": 1080,
                },
            )
            with mock.patch(
                "football_tracking.api.service.publish_json_exclusive",
                side_effect=OSError("injected operation report failure"),
            ):
                starter.call_args.args[1].run()
            pending = self.service.get_run(response["run_id"])
            self.assertEqual("running", pending["status"])
            self.assertEqual("reconciling", pending["broadcast"]["operation_status"])
            self.assertTrue(pending["broadcast"]["worker_exited"])
            registry_available = True
            recovered = self.wait_for_terminal_run(self.service, response["run_id"])

        self.assertEqual("completed", recovered["status"], recovered.get("error"))
        self.assertEqual("available", recovered["broadcast"]["operation_report_status"])
        self.assertTrue((Path(recovered["output_dir"]) / "broadcast_operation_report.v1.json").is_file())
        self.assertEqual("ready", self.service.get_run("broadcast-test")["broadcast"]["status"])
        retry_key = f"{response['run_id']}:reconciliation-retry"
        deadline = time.monotonic() + 2.0
        while retry_key in self.service._active_threads and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertNotIn(retry_key, self.service._active_threads)
        self.assertNotIn(retry_key, self.service._cancel_events)
        self.service.close(timeout=2.0)
        self.assertFalse(self.service._service_lease_path.exists())

    def test_conflicting_operation_report_fails_closed(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        self._register_broadcast_run(output_dir)
        frozen = {"review_decisions_sha256": "b" * 64}
        result = {"status": "completed", "trajectory_generation_id": "trajectory-" + "d" * 24}
        quality = {
            "status": "needs_review",
            "blocking_reasons": ["missing_hybrid_camera_path"],
            "limitations": [],
            "status_generation": "e" * 64,
        }

        def committed_recompute(*_args: object, **kwargs: object) -> dict[str, object]:
            kwargs["before_commit"]()
            return result

        with (
            mock.patch(
                "football_tracking.api.service.preflight_recompute_reviewed_trajectory",
                return_value=frozen,
            ),
            mock.patch(
                "football_tracking.api.service.preflight_render_broadcast_trajectory",
                return_value={"trajectory_generation_id": result["trajectory_generation_id"]},
            ),
            mock.patch(
                "football_tracking.api.service.recompute_reviewed_trajectory",
                side_effect=committed_recompute,
            ),
            mock.patch("football_tracking.api.service.publish_broadcast_facade", return_value=quality),
            mock.patch.object(self.service, "_start_thread_or_cleanup") as starter,
        ):
            response = self.service.recompute_broadcast_trajectory(
                "broadcast-test",
                {"review_decisions_sha256": "b" * 64},
            )
            operation_output = Path(self.service.get_run(response["run_id"])["output_dir"])
            conflicting = operation_output / "broadcast_operation_report.v1.json"
            _write_json(conflicting, {"artifact_type": "tampered_operation_report"})
            starter.call_args.args[1].run()

        child = self.service.get_run(response["run_id"])
        parent = self.service.get_run("broadcast-test")
        self.assertEqual("failed", child["status"])
        self.assertIn("refusing to overwrite", child["error"])
        self.assertEqual("tampered_operation_report", json.loads(conflicting.read_text())["artifact_type"])
        self.assertNotIn("trajectory_generation_id", parent.get("broadcast", {}))

    def test_worker_parent_loss_fails_child_and_always_cleans_active_state(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        self._register_broadcast_run(output_dir)
        frozen = {"review_decisions_sha256": "b" * 64}
        with (
            mock.patch(
                "football_tracking.api.service.preflight_recompute_reviewed_trajectory",
                return_value=frozen,
            ),
            mock.patch.object(self.service, "_start_thread_or_cleanup") as starter,
        ):
            response = self.service.recompute_broadcast_trajectory(
                "broadcast-test",
                {"review_decisions_sha256": "b" * 64},
            )
        operation_run_id = response["run_id"]
        registry = self.service._read_registry()
        registry["runs"] = [run for run in registry["runs"] if run["run_id"] != "broadcast-test"]
        self.service._write_registry(registry)

        starter.call_args.args[1].run()

        child = self.service.get_run(operation_run_id)
        self.assertEqual("failed", child["status"])
        self.assertNotIn(operation_run_id, self.service._active_threads)
        self.assertNotIn(operation_run_id, self.service._cancel_events)

    def test_child_artifact_failure_cannot_downgrade_an_authoritative_core_commit(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        self._register_broadcast_run(output_dir)
        frozen = {"review_decisions_sha256": "b" * 64}
        result = {"status": "completed", "trajectory_generation_id": "trajectory-" + "d" * 24}

        def committed_recompute(*_args: object, **kwargs: object) -> dict[str, object]:
            before_commit = kwargs["before_commit"]
            self.assertTrue(callable(before_commit))
            before_commit()
            return result

        with (
            mock.patch(
                "football_tracking.api.service.preflight_recompute_reviewed_trajectory",
                return_value=frozen,
            ),
            mock.patch("football_tracking.api.service.preflight_render_broadcast_trajectory", return_value={}),
            mock.patch(
                "football_tracking.api.service.recompute_reviewed_trajectory",
                side_effect=committed_recompute,
            ),
            mock.patch(
                "football_tracking.api.service.publish_broadcast_facade",
                return_value={"status": "needs_review", "status_generation": "e" * 64},
            ),
            mock.patch.object(self.service, "_start_thread_or_cleanup") as starter,
        ):
            response = self.service.recompute_broadcast_trajectory(
                "broadcast-test",
                {"review_decisions_sha256": "b" * 64},
            )
            with mock.patch.object(self.service, "_write_run_artifacts", return_value="injected artifact failure"):
                starter.call_args.args[1].run()

        child = self.service.get_run(response["run_id"])
        parent = self.service.get_run("broadcast-test")
        self.assertEqual("completed", child["status"])
        self.assertTrue((Path(child["output_dir"]) / "broadcast_operation_report.v1.json").is_file())
        self.assertEqual("completed", parent.get("broadcast", {}).get("last_operation", {}).get("status"))
        self.assertIn("injected artifact failure", child["broadcast"]["metadata_warnings"][0])

    def test_render_child_fails_if_the_final_facade_is_not_ready(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        self._register_broadcast_run(output_dir)
        generation_id = "trajectory-" + "d" * 24
        frozen = {"trajectory_generation_id": generation_id, "target_width": 1920, "target_height": 1080}

        def committed_render(*_args: object, **kwargs: object) -> dict[str, object]:
            before_commit = kwargs["before_commit"]
            self.assertTrue(callable(before_commit))
            before_commit()
            return {
                "status": "completed",
                "trajectory_generation_id": generation_id,
                "camera_generation_id": "camera-" + "c" * 24,
                "render_generation_id": "render-" + "e" * 24,
            }

        with (
            mock.patch(
                "football_tracking.api.service.preflight_render_broadcast_trajectory",
                return_value=frozen,
            ),
            mock.patch("football_tracking.api.service.render_broadcast_trajectory", side_effect=committed_render),
            mock.patch(
                "football_tracking.api.service.publish_broadcast_facade",
                return_value={"status": "needs_review", "status_generation": "f" * 64},
            ),
            mock.patch.object(self.service, "_start_thread_or_cleanup") as starter,
        ):
            response = self.service.render_broadcast_hybrid(
                "broadcast-test",
                {
                    "trajectory_generation_id": generation_id,
                    "target_width": 1920,
                    "target_height": 1080,
                },
            )
            starter.call_args.args[1].run()

        child = self.service.get_run(response["run_id"])
        self.assertEqual("failed", child["status"])
        self.assertFalse((Path(child["output_dir"]) / "broadcast_operation_report.v1.json").exists())

    def test_render_child_metadata_failure_cannot_downgrade_a_ready_facade(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        self._register_broadcast_run(output_dir)
        generation_id = "trajectory-" + "d" * 24
        frozen = {"trajectory_generation_id": generation_id, "target_width": 1920, "target_height": 1080}
        result = {
            "status": "completed",
            "trajectory_generation_id": generation_id,
            "camera_generation_id": "camera-" + "c" * 24,
            "render_generation_id": "render-" + "e" * 24,
        }

        def committed_render(*_args: object, **kwargs: object) -> dict[str, object]:
            before_commit = kwargs["before_commit"]
            self.assertTrue(callable(before_commit))
            before_commit()
            return result

        quality = {
            "status": "ready",
            "blocking_reasons": [],
            "limitations": [],
            "status_generation": "f" * 64,
        }
        with (
            mock.patch(
                "football_tracking.api.service.preflight_render_broadcast_trajectory",
                return_value=frozen,
            ),
            mock.patch("football_tracking.api.service.render_broadcast_trajectory", side_effect=committed_render),
            mock.patch("football_tracking.api.service.publish_broadcast_facade", return_value=quality),
            mock.patch.object(self.service, "_start_thread_or_cleanup") as starter,
        ):
            response = self.service.render_broadcast_hybrid(
                "broadcast-test",
                {
                    "trajectory_generation_id": generation_id,
                    "target_width": 1920,
                    "target_height": 1080,
                },
            )
            with mock.patch.object(
                self.service, "_write_run_artifacts", return_value="injected child metadata failure"
            ):
                starter.call_args.args[1].run()

        child = self.service.get_run(response["run_id"])
        parent = self.service.get_run("broadcast-test")
        self.assertEqual("completed", child["status"])
        self.assertEqual("ready", parent["broadcast"]["status"])
        self.assertIn("injected child metadata failure", child["broadcast"]["metadata_warnings"][0])

    def test_broadcast_operation_preflight_failure_has_zero_queue_side_effects(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        self._register_broadcast_run(output_dir)
        before_ids = {run["run_id"] for run in self.service.list_runs()}
        with mock.patch(
            "football_tracking.api.service.preflight_recompute_reviewed_trajectory",
            side_effect=BroadcastHybridOrchestrationError("stale model package"),
        ):
            with self.assertRaisesRegex(RuntimeError, "stale model package"):
                self.service.recompute_broadcast_trajectory(
                    "broadcast-test",
                    {"review_decisions_sha256": "b" * 64},
                )

        self.assertEqual(before_ids, {run["run_id"] for run in self.service.list_runs()})
        self.assertEqual({}, self.service._active_threads)

    def test_registry_replace_failure_preserves_the_previous_complete_registry(self) -> None:
        before = self.service.registry_path.read_bytes()
        registry = self.service._read_registry()
        registry["runs"].append({"run_id": "not-committed"})

        with mock.patch("football_tracking.api.service.os.replace", side_effect=OSError("injected")):
            with self.assertRaisesRegex(OSError, "injected"):
                self.service._write_registry(registry)

        self.assertEqual(before, self.service.registry_path.read_bytes())
        self.assertNotIn("not-committed", {run.get("run_id") for run in self.service._read_registry()["runs"]})

    def test_service_restart_fails_an_orphaned_operation_without_a_commit_report(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        self._register_broadcast_run(output_dir)
        frozen = {"review_decisions_sha256": "b" * 64}
        with (
            mock.patch(
                "football_tracking.api.service.preflight_recompute_reviewed_trajectory",
                return_value=frozen,
            ),
            mock.patch.object(self.service, "_start_thread_or_cleanup"),
        ):
            response = self.service.recompute_broadcast_trajectory(
                "broadcast-test",
                {"review_decisions_sha256": "b" * 64},
            )

        with mock.patch.object(ApiService, "_owner_lease_is_active", return_value=False):
            recovered_service = ApiService(self.repo_root)
            self.services.append(recovered_service)
        child = self.wait_for_terminal_run(recovered_service, response["run_id"])
        self.assertEqual("failed", child["status"])
        self.assertTrue((Path(child["output_dir"]) / "run_manifest.json").is_file())

    def test_restart_preserves_the_commit_barrier_for_an_interrupted_render(self) -> None:
        parent_output = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        parent_output.mkdir(parents=True)
        self._register_broadcast_run(parent_output)
        generation_id = "trajectory-" + "d" * 24
        frozen = {"trajectory_generation_id": generation_id, "target_width": 1920, "target_height": 1080}
        with (
            mock.patch(
                "football_tracking.api.service.preflight_render_broadcast_trajectory",
                return_value=frozen,
            ),
            mock.patch.object(self.service, "_start_thread_or_cleanup"),
        ):
            response = self.service.render_broadcast_hybrid(
                "broadcast-test",
                {
                    "trajectory_generation_id": generation_id,
                    "target_width": 1920,
                    "target_height": 1080,
                },
            )
        registry = self.service._read_registry()
        child = next(run for run in registry["runs"] if run["run_id"] == response["run_id"])
        child["status"] = "running"
        child["broadcast"]["operation_status"] = "committing"
        child["broadcast"]["commit_started"] = True
        self.service._write_registry(registry)
        self.service.close()

        with (
            mock.patch.object(ApiService, "_owner_lease_is_active", return_value=False),
            mock.patch("football_tracking.api.service.threading.Thread.start"),
        ):
            recovered_service = ApiService(self.repo_root)
            self.services.append(recovered_service)

        recovered = recovered_service.get_run(response["run_id"])
        self.assertEqual("queued", recovered["status"])
        self.assertTrue(recovered["broadcast"]["commit_started"])
        with self.assertRaisesRegex(RuntimeError, "commit has already started"):
            recovered_service.cancel_run(response["run_id"])

    def test_ready_render_recovery_publishes_terminal_child_artifacts_before_commit_report(self) -> None:
        parent_output = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        parent_output.mkdir(parents=True)
        self._register_broadcast_run(parent_output)
        operation_output = parent_output.parent / "render-recovery"
        operation_output.mkdir()
        operation_run_id = "render-recovery"
        trajectory_generation_id = "trajectory-" + "d" * 24
        request = {
            "trajectory_generation_id": trajectory_generation_id,
            "target_width": 1920,
            "target_height": 1080,
        }
        frozen = {**request, "parent_run_id": "broadcast-test", "parent_output_dir": str(parent_output)}
        registry = self.service._read_registry()
        registry["runs"].append(
            {
                "run_id": operation_run_id,
                "source": "broadcast_hybrid_render",
                "status": "running",
                "created_at": "2026-07-10T00:00:02Z",
                "started_at": "2026-07-10T00:00:03Z",
                "completed_at": None,
                "config_name": "default.yaml",
                "config_path": str(self.repo_root / "config" / "default.yaml"),
                "input_video": str(self.video),
                "parent_run_id": "broadcast-test",
                "output_dir": str(operation_output),
                "modules_enabled": {"broadcast_hybrid": True},
                "artifacts": [],
                "stats": {},
                "broadcast": {
                    "operation": "render",
                    "request": request,
                    "frozen_inputs": frozen,
                    "commit_started": True,
                    "operation_status": "committing",
                },
                "progress": self.service._initial_progress(),
                "notes": None,
                "error": None,
            }
        )
        self.service._write_registry(registry)
        final_manifest = {
            "artifact_type": "broadcast_artifact_bindings",
            "generation_ids": {
                "trajectory": trajectory_generation_id,
                "camera": "camera-" + "c" * 24,
                "render": "render-" + "e" * 24,
            },
            "artifacts": {},
        }
        _write_json(parent_output / "broadcast_artifact_bindings.v1.json", final_manifest)
        quality = {
            "status": "ready",
            "blocking_reasons": [],
            "limitations": [],
            "status_generation": "f" * 64,
        }
        publication_order: list[str] = []
        real_write = self.service._write_run_artifacts

        def record_child_artifacts(output_dir: Path, run: dict[str, object]) -> str | None:
            publication_order.append("child_artifacts")
            return real_write(output_dir, run)

        def record_operation_report(path: Path, payload: dict[str, object], **kwargs: object) -> str:
            publication_order.append("operation_report")
            return publish_json_exclusive(path, payload, **kwargs)

        with (
            mock.patch(
                "football_tracking.api.service.validate_broadcast_quality_report",
                return_value=quality,
            ),
            mock.patch(
                "football_tracking.api.service.preflight_render_broadcast_trajectory",
                return_value=request,
            ),
            mock.patch.object(self.service, "_write_run_artifacts", side_effect=record_child_artifacts),
            mock.patch(
                "football_tracking.api.service.publish_json_exclusive",
                side_effect=record_operation_report,
            ),
        ):
            self.service._reconcile_ready_render_without_operation_report(operation_run_id)

        child = self.service.get_run(operation_run_id)
        run_manifest_path = operation_output / "run_manifest.json"
        operation_report_path = operation_output / "broadcast_operation_report.v1.json"
        run_manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(["child_artifacts", "operation_report"], publication_order)
        self.assertEqual("completed", child["status"])
        self.assertEqual("available", child["broadcast"]["operation_report_status"])
        self.assertEqual("completed", run_manifest["status"])
        self.assertGreaterEqual(operation_report_path.stat().st_mtime_ns, run_manifest_path.stat().st_mtime_ns)

    def test_restart_repairs_a_ready_render_that_crashed_before_its_operation_report(self) -> None:
        parent_output = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        parent_output.mkdir(parents=True)
        self._register_broadcast_run(parent_output)
        trajectory_generation_id = "trajectory-" + "d" * 24
        request = {
            "trajectory_generation_id": trajectory_generation_id,
            "target_width": 1920,
            "target_height": 1080,
        }
        with (
            mock.patch(
                "football_tracking.api.service.preflight_render_broadcast_trajectory",
                return_value=request,
            ),
            mock.patch.object(self.service, "_start_thread_or_cleanup"),
        ):
            response = self.service.render_broadcast_hybrid("broadcast-test", request)
        registry = self.service._read_registry()
        child = next(run for run in registry["runs"] if run["run_id"] == response["run_id"])
        child["status"] = "running"
        child["broadcast"]["operation_status"] = "committing"
        child["broadcast"]["commit_started"] = True
        self.service._write_registry(registry)
        self.service.close()

        quality = {
            "status": "ready",
            "blocking_reasons": [],
            "limitations": [],
            "status_generation": "f" * 64,
        }
        _write_json(parent_output / "broadcast_quality_report.json", quality)
        _write_json(
            parent_output / "broadcast_artifact_bindings.v1.json",
            {
                "artifact_type": "broadcast_artifact_bindings",
                "generation_ids": {
                    "trajectory": trajectory_generation_id,
                    "camera": "camera-" + "c" * 24,
                    "render": "render-" + "e" * 24,
                },
                "artifacts": {},
            },
        )

        with (
            mock.patch.object(ApiService, "_owner_lease_is_active", return_value=False),
            mock.patch(
                "football_tracking.api.service.validate_broadcast_quality_report",
                return_value=quality,
            ),
            mock.patch(
                "football_tracking.api.service.preflight_render_broadcast_trajectory",
                return_value=request,
            ),
        ):
            recovered_service = ApiService(self.repo_root)
            self.services.append(recovered_service)
            recovered = self.wait_for_terminal_run(recovered_service, response["run_id"])

        self.assertEqual("completed", recovered["status"], recovered.get("error"))
        self.assertTrue(recovered["broadcast"]["recovered"])
        self.assertEqual("available", recovered["broadcast"]["operation_report_status"])
        self.assertTrue((Path(recovered["output_dir"]) / "broadcast_operation_report.v1.json").is_file())
        self.assertEqual("ready", recovered_service.get_run("broadcast-test")["broadcast"]["status"])

    def test_second_live_service_does_not_recover_an_owned_operation(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        self._register_broadcast_run(output_dir)
        frozen = {"review_decisions_sha256": "b" * 64}
        with (
            mock.patch(
                "football_tracking.api.service.preflight_recompute_reviewed_trajectory",
                return_value=frozen,
            ),
            mock.patch.object(self.service, "_start_thread_or_cleanup"),
        ):
            response = self.service.recompute_broadcast_trajectory(
                "broadcast-test",
                {"review_decisions_sha256": "b" * 64},
            )

        second_service = ApiService(self.repo_root)
        self.services.append(second_service)
        self.assertEqual("queued", second_service.get_run(response["run_id"])["status"])

    def test_unlocked_instance_lease_defeats_same_pid_reuse(self) -> None:
        stale_instance_id = "stale-instance"
        lease = self.service.service_lease_dir / f"{stale_instance_id}.lock"
        lease.parent.mkdir(parents=True, exist_ok=True)
        lease.write_bytes(b"0")

        self.assertFalse(self.service._owner_lease_is_active(os.getpid(), stale_instance_id))
        lease.unlink()
        self.assertFalse(self.service._owner_lease_is_active(os.getpid(), stale_instance_id))

    def test_constructor_failure_releases_its_service_lease(self) -> None:
        lease_dir = self.service.service_lease_dir
        before = {path.name for path in lease_dir.iterdir()}
        with mock.patch(
            "football_tracking.api.service.load_provider_settings",
            side_effect=RuntimeError("injected provider initialization failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "provider initialization failure"):
                ApiService(self.repo_root)
        self.assertEqual(before, {path.name for path in lease_dir.iterdir()})

    def test_closed_service_rejects_new_runs_without_registry_or_worker_side_effects(self) -> None:
        before_registry = self.service.registry_path.read_bytes()
        self.service.close()

        with self.assertRaisesRegex(RuntimeError, "API service is closed"):
            self.service.create_run(self.request())

        self.assertEqual(before_registry, self.service.registry_path.read_bytes())
        self.assertEqual({}, self.service._active_threads)
        self.assertEqual({}, self.service._cancel_events)

    def test_close_between_thread_registration_and_start_never_launches_an_unleased_worker(self) -> None:
        registration_complete = threading.Event()
        allow_start = threading.Event()
        create_errors: list[BaseException] = []
        real_start = self.service._start_thread_or_cleanup

        def delayed_start(*args: object, **kwargs: object) -> None:
            registration_complete.set()
            self.assertTrue(allow_start.wait(2.0))
            real_start(*args, **kwargs)

        def create_run() -> None:
            try:
                self.service.create_run(self.request())
            except BaseException as exc:
                create_errors.append(exc)

        with mock.patch.object(self.service, "_start_thread_or_cleanup", side_effect=delayed_start):
            creator = threading.Thread(target=create_run, daemon=True)
            creator.start()
            self.assertTrue(registration_complete.wait(2.0))
            self.service.close(timeout=0.0)
            self.assertFalse(self.service._service_lease_path.exists())
            allow_start.set()
            creator.join(timeout=2.0)

        self.assertFalse(creator.is_alive())
        self.assertEqual(1, len(create_errors))
        self.assertIn("API service is closed", str(create_errors[0]))
        self.assertEqual([], self.service.list_runs())
        self.assertEqual({}, self.service._active_threads)
        self.assertEqual({}, self.service._cancel_events)

    def test_close_keeps_the_lease_while_a_registered_start_is_in_flight(self) -> None:
        run_id = "pending-start"
        output_dir = self.repo_root / "outputs" / "runs" / run_id
        output_dir.mkdir(parents=True)
        registry = self.service._read_registry()
        registry["runs"].append({"run_id": run_id, "status": "queued", "output_dir": str(output_dir)})
        self.service._write_registry(registry)
        start_entered = threading.Event()
        allow_failure = threading.Event()
        start_errors: list[BaseException] = []

        class DelayedFailingThread:
            ident = None

            def start(self) -> None:
                start_entered.set()
                self_test = allow_failure.wait(2.0)
                if not self_test:
                    raise RuntimeError("test start gate timed out")
                raise RuntimeError("injected start failure")

            def is_alive(self) -> bool:
                return False

        pending_thread = DelayedFailingThread()
        self.service._active_threads[run_id] = pending_thread  # type: ignore[assignment]
        self.service._cancel_events[run_id] = threading.Event()

        def start_registered_thread() -> None:
            try:
                self.service._start_thread_or_cleanup(
                    run_id,
                    pending_thread,  # type: ignore[arg-type]
                    output_dir=output_dir,
                    remove_output=True,
                )
            except BaseException as exc:
                start_errors.append(exc)

        starter = threading.Thread(target=start_registered_thread, daemon=True)
        starter.start()
        self.assertTrue(start_entered.wait(2.0))
        self.assertIn(run_id, self.service._starting_threads)

        self.service.close(timeout=0.0)
        self.assertTrue(self.service._service_lease_path.exists())
        allow_failure.set()
        starter.join(timeout=2.0)
        deadline = time.monotonic() + 2.0
        while self.service._service_lease_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertFalse(starter.is_alive())
        self.assertEqual(1, len(start_errors))
        self.assertIn("injected start failure", str(start_errors[0]))
        self.assertFalse(self.service._service_lease_path.exists())
        self.assertFalse(output_dir.exists())
        self.assertNotIn(run_id, {run.get("run_id") for run in self.service._read_registry()["runs"]})
        self.assertEqual(set(), self.service._starting_threads)

    def test_close_timeout_releases_the_lease_after_the_last_worker_exits(self) -> None:
        release_worker = threading.Event()
        worker = threading.Thread(target=release_worker.wait, daemon=True)
        self.service._active_threads["blocking-test-worker"] = worker
        worker.start()
        lease_path = self.service._service_lease_path

        self.service.close(timeout=0.01)
        self.assertTrue(lease_path.exists())
        release_worker.set()
        worker.join(timeout=2.0)
        deadline = time.monotonic() + 2.0
        while lease_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertFalse(lease_path.exists())
        self.assertFalse(self.service._owner_lease_is_active(os.getpid(), self.service._instance_id))

    def test_service_restart_rejects_a_commit_report_with_a_missing_generation(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        self._register_broadcast_run(output_dir)
        frozen = {"review_decisions_sha256": "b" * 64}
        trajectory_generation_id = "trajectory-" + "d" * 24
        with (
            mock.patch(
                "football_tracking.api.service.preflight_recompute_reviewed_trajectory",
                return_value=frozen,
            ),
            mock.patch.object(self.service, "_start_thread_or_cleanup"),
        ):
            response = self.service.recompute_broadcast_trajectory(
                "broadcast-test",
                {"review_decisions_sha256": "b" * 64},
            )
        operation_run_id = response["run_id"]
        operation_output = Path(self.service.get_run(operation_run_id)["output_dir"])
        quality = {
            "status": "needs_review",
            "blocking_reasons": ["missing_hybrid_camera_path"],
            "limitations": [],
            "status_generation": "e" * 64,
        }
        _write_json(
            operation_output / "broadcast_operation_report.v1.json",
            {
                "artifact_type": "broadcast_operation_report",
                "status": "succeeded",
                "operation": "recompute",
                "parent_run_id": "broadcast-test",
                "frozen_inputs": frozen,
                "result": {
                    "status": "completed",
                    "trajectory_generation_id": trajectory_generation_id,
                },
                "quality_status_generation": "e" * 64,
            },
        )

        with (
            mock.patch.object(ApiService, "_owner_lease_is_active", return_value=False),
            mock.patch("football_tracking.api.service.publish_broadcast_facade", return_value=quality),
        ):
            recovered_service = ApiService(self.repo_root)
            self.services.append(recovered_service)
            child = self.wait_for_terminal_run(recovered_service, operation_run_id)

        parent = recovered_service.get_run("broadcast-test")
        self.assertEqual("failed", child["status"])
        self.assertNotEqual(trajectory_generation_id, parent.get("broadcast", {}).get("trajectory_generation_id"))

    def test_service_restart_resumes_and_commits_an_interrupted_recompute(self) -> None:
        output_dir = self.repo_root / "outputs" / "runs" / "match" / "broadcast-test"
        output_dir.mkdir(parents=True)
        self._register_broadcast_run(output_dir)
        frozen = {"review_decisions_sha256": "b" * 64}
        trajectory_generation_id = "trajectory-" + "d" * 24
        with (
            mock.patch(
                "football_tracking.api.service.preflight_recompute_reviewed_trajectory",
                return_value=frozen,
            ),
            mock.patch.object(self.service, "_start_thread_or_cleanup"),
        ):
            response = self.service.recompute_broadcast_trajectory(
                "broadcast-test",
                {"review_decisions_sha256": "b" * 64},
            )
        quality = {
            "status": "needs_review",
            "blocking_reasons": ["missing_hybrid_camera_path"],
            "limitations": [],
            "status_generation": "e" * 64,
        }

        def resumed_recompute(*_args: object, **kwargs: object) -> dict[str, object]:
            kwargs["before_commit"]()
            return {"status": "completed", "trajectory_generation_id": trajectory_generation_id}

        with (
            mock.patch.object(ApiService, "_owner_lease_is_active", return_value=False),
            mock.patch(
                "football_tracking.api.service.preflight_recompute_reviewed_trajectory",
                return_value=frozen,
            ),
            mock.patch(
                "football_tracking.api.service.recompute_reviewed_trajectory",
                side_effect=resumed_recompute,
            ),
            mock.patch("football_tracking.api.service.preflight_render_broadcast_trajectory", return_value={}),
            mock.patch("football_tracking.api.service.publish_broadcast_facade", return_value=quality),
        ):
            recovered_service = ApiService(self.repo_root)
            self.services.append(recovered_service)
            child = self.wait_for_terminal_run(recovered_service, response["run_id"])

        parent = recovered_service.get_run("broadcast-test")
        self.assertEqual("completed", child["status"])
        self.assertTrue(child["broadcast"]["recovered"])
        self.assertEqual(trajectory_generation_id, parent["broadcast"]["trajectory_generation_id"])
        self.assertTrue((Path(child["output_dir"]) / "broadcast_operation_report.v1.json").is_file())

    def test_app_registers_typed_broadcast_routes(self) -> None:
        app = create_app(self.repo_root, initialize_service=False)
        document = app.openapi()
        for path in (
            "/api/v1/runs/{run_id}/broadcast/review-windows",
            "/api/v1/runs/{run_id}/broadcast/review-actions",
            "/api/v1/runs/{run_id}/broadcast/trajectory-recompute",
            "/api/v1/runs/{run_id}/broadcast/render",
        ):
            self.assertIn(path, document["paths"])
        self.assertIn(
            "202",
            document["paths"]["/api/v1/runs/{run_id}/broadcast/trajectory-recompute"]["post"]["responses"],
        )
        self.assertIn("202", document["paths"]["/api/v1/runs/{run_id}/broadcast/render"]["post"]["responses"])
        artifact_methods = {
            method
            for route in app.routes
            if getattr(route, "path", None) == "/api/v1/runs/{run_id}/artifacts/{artifact_name:path}"
            for method in getattr(route, "methods", set())
        }
        self.assertTrue({"GET", "HEAD"}.issubset(artifact_methods))

    @staticmethod
    def _invoke_artifact_response(
        response: Any,
        *,
        method: str,
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        events: list[dict[str, Any]] = []

        async def receive() -> dict[str, str]:
            return {"type": "http.disconnect"}

        async def send(event: dict[str, Any]) -> None:
            events.append(event)

        asyncio.run(
            response(
                {
                    "type": "http",
                    "method": method,
                    "headers": headers or [],
                },
                receive,
                send,
            )
        )
        start = next(event for event in events if event["type"] == "http.response.start")
        response_headers = {key.decode("latin-1"): value.decode("latin-1") for key, value in start["headers"]}
        body = b"".join(event.get("body", b"") for event in events if event["type"] == "http.response.body")
        return start["status"], response_headers, body

    def _register_broadcast_run(self, output_dir: Path) -> None:
        registry = self.service._read_registry()
        registry["runs"].append(
            {
                "run_id": "broadcast-test",
                "source": "broadcast_hybrid",
                "status": "completed",
                "created_at": "2026-07-10T00:00:00Z",
                "started_at": "2026-07-10T00:00:00Z",
                "completed_at": "2026-07-10T00:00:01Z",
                "config_name": "default.yaml",
                "config_path": str(self.repo_root / "config" / "default.yaml"),
                "input_video": str(self.video),
                "parent_run_id": None,
                "output_dir": str(output_dir),
                "modules_enabled": {"broadcast_hybrid": True},
                "artifacts": [],
                "stats": {},
                "broadcast": {"max_manual_review_windows": 1},
                "progress": None,
                "notes": None,
                "error": None,
            }
        )
        self.service._write_registry(registry)

    def _write_mock_ready_delivery_contract(
        self,
        output_dir: Path,
    ) -> tuple[dict[str, object], Path]:
        bindings: dict[str, object] = {}
        for name in PUBLIC_ARTIFACTS:
            path = output_dir / name
            path.write_bytes(f"{name}-bytes".encode("utf-8"))
            bindings[name] = {
                "sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
                "source_report": {
                    "path": f"broadcast_generations/source-{name}.json",
                    "sha256": "b" * 64,
                },
            }
        _write_json(
            output_dir / "broadcast_artifact_bindings.v1.json",
            {
                "artifact_type": "broadcast_artifact_bindings",
                "artifacts": bindings,
            },
        )
        quality: dict[str, object] = {
            "artifact_type": "broadcast_quality_report",
            "status": "ready",
            "status_generation": "a" * 64,
        }
        _write_json(output_dir / "broadcast_quality_report.json", quality)
        generation_payload = output_dir / "broadcast_generations" / "render-test" / "payload.bin"
        generation_payload.parent.mkdir(parents=True)
        generation_payload.write_bytes(b"alpha")
        return quality, generation_payload

    def _write_mock_ready_queue(self, output_dir: Path) -> Path:
        external_dir = self.repo_root / "external-ready-bindings"
        external_dir.mkdir()
        binding_names = (
            "review_timing",
            "policy",
            "decisions",
            "model",
            "training_report",
            "model_weights",
            "dataset",
            "predictions",
            "contract",
            "annotation_resolution",
            "resolved_tracking_contract",
            "policy_roles",
        )
        bindings: dict[str, dict[str, str]] = {}
        external_policy = external_dir / "policy.json"
        for name in binding_names:
            path = external_dir / f"{name}.json"
            payload: object = {"name": name}
            if name == "dataset":
                payload = {
                    "artifact_type": "candidate_dataset",
                    "sources": [{"path": str(self.video), "sha256": _sha256(self.video)}],
                    "samples": [],
                }
            _write_json(path, payload)
            bindings[name] = {"path": str(path), "sha256": _sha256(path)}
        _write_json(
            output_dir / "selective_review_queue.v1.json",
            {
                "artifact_type": "selective_review_queue",
                "bindings": bindings,
            },
        )
        return external_policy

    def test_type_only_source_reports_cannot_forge_a_ready_final_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            _write_json(
                output_dir / "tracking_contract.v2.json",
                {"candidates": [{"candidate_id": "candidate-1", "frame_index": 0}]},
            )
            _write_json(
                output_dir / "candidate_predictions.v1.json",
                {"predictions": [{"candidate_id": "candidate-1", "predicted_label": "match_ball"}]},
            )
            first = publish_broadcast_facade(output_dir)
            self.assertEqual("needs_review", first["status"])

            for name, contents in {
                "ball_candidates.jsonl": '{"candidate_id":"candidate-1","frame_index":0}\n',
                "candidate_classifications.jsonl": ('{"candidate_id":"candidate-1","predicted_label":"match_ball"}\n'),
                "action_track.csv": "Frame,X,Y\n0,10,10\n",
                "ball_track.v2.csv": "Frame,X,Y,Confidence,Status,SelectedCandidateId,Source,Reason\n",
                "review_decisions.json": '{"artifact_type":"selective_review_actions","actions":[]}\n',
                "camera_target.csv": "Frame,CenterX,CenterY\n0,10,10\n",
                "broadcast.mp4": "video-bytes",
            }.items():
                (output_dir / name).write_text(contents, encoding="utf-8")
            queue_bindings: dict[str, object] = {}
            for name in (
                "review_timing",
                "policy",
                "decisions",
                "model",
                "training_report",
                "model_weights",
                "dataset",
                "predictions",
                "contract",
                "annotation_resolution",
                "resolved_tracking_contract",
                "policy_roles",
            ):
                path = output_dir / "queue_evidence" / f"{name}.json"
                _write_json(path, {"name": name})
                queue_bindings[name] = {
                    "path": path.relative_to(output_dir).as_posix(),
                    "sha256": _sha256(path),
                }
            _write_json(
                output_dir / "selective_review_queue.v1.json",
                {"artifact_type": "selective_review_queue", "bindings": queue_bindings},
            )

            bindings: dict[str, object] = {}
            source_report_payloads = {
                "ball_candidates.jsonl": {"schema_version": "2.0", "candidates": []},
                "candidate_classifications.jsonl": {"artifact_type": "candidate_predictions"},
                "ball_track.v2.csv": {"artifact_type": "global_ball_trajectory_report"},
                "action_track.csv": {"artifact_type": "broadcast_action_signal_binding"},
                "review_decisions.json": {"artifact_type": "selective_review_materialization"},
                "camera_target.csv": {"artifact_type": "hybrid_broadcast_camera_report"},
                "broadcast.mp4": {"artifact_type": "broadcast_render_report"},
            }
            for name in (
                "ball_candidates.jsonl",
                "candidate_classifications.jsonl",
                "ball_track.v2.csv",
                "action_track.csv",
                "review_decisions.json",
                "camera_target.csv",
                "broadcast.mp4",
            ):
                report_path = output_dir / "source_reports" / f"{name}.json"
                _write_json(report_path, source_report_payloads[name])
                bindings[name] = {
                    "sha256": _sha256(output_dir / name),
                    "source_report": {
                        "path": report_path.relative_to(output_dir).as_posix(),
                        "sha256": _sha256(report_path),
                    },
                }
            _write_json(
                output_dir / "broadcast_artifact_bindings.v1.json",
                {
                    "artifact_type": "broadcast_artifact_bindings",
                    "orchestration_version": "broadcast-hybrid-orchestration-v1",
                    "artifacts": bindings,
                },
            )

            with self.assertRaisesRegex(BroadcastApiError, "final artifact validation"):
                publish_broadcast_facade(output_dir)

            self.assertFalse((output_dir / "broadcast_quality_report.json").exists())
            self.assertEqual(
                first,
                json.loads(
                    (
                        output_dir / "broadcast_status" / first["status_generation"] / "broadcast_quality_report.json"
                    ).read_text(encoding="utf-8")
                ),
            )


if __name__ == "__main__":
    unittest.main()
