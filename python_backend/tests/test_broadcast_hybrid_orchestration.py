from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from concurrent.futures import CancelledError
from pathlib import Path
from typing import Any
from unittest import mock

from football_tracking import broadcast_hybrid_orchestration as orchestration
from football_tracking.api.broadcast_api import (
    BroadcastApiError,
    publish_broadcast_facade,
    validate_broadcast_quality_report,
)
from football_tracking.camera_path_renderer import CameraPathRenderResult
from football_tracking.candidate_annotations import ADJUDICATION_QUEUE_NAME, ANNOTATION_RESOLUTION_NAME
from football_tracking.candidate_classifier import (
    MODEL_MANIFEST_NAME,
    MODEL_WEIGHTS_NAME,
    PREDICTIONS_NAME,
    TRAINING_REPORT_NAME,
)
from football_tracking.global_ball_trajectory import (
    ALGORITHM_VERSION as TRAJECTORY_ALGORITHM_VERSION,
)
from football_tracking.global_ball_trajectory import (
    REPORT_NAME as TRAJECTORY_REPORT_NAME,
)
from football_tracking.global_ball_trajectory import TRACK_NAME
from football_tracking.hybrid_broadcast_camera import (
    REPORT_NAME as HYBRID_REPORT_NAME,
)
from football_tracking.hybrid_broadcast_camera import HybridCameraConfig
from football_tracking.selective_review import (
    ACTIVE_ROUND_NAME,
    HUMAN_VOTES_NAME,
    MATERIALIZATION_REPORT_NAME,
    REVIEW_QUEUE_NAME,
    TRAJECTORY_CORRECTIONS_NAME,
)
from football_tracking.tracking_contracts import TRACKING_CONTRACT_REPORT_NAME


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _artifact(path: Path) -> dict[str, object]:
    return {"sha256": _sha256(path), "size": path.stat().st_size}


class _BoundRun:
    def __init__(self, root: Path) -> None:
        self.run_dir = root / "run"
        self.run_dir.mkdir()
        self.source_video = self.run_dir / "source.avi"
        self.source_video.write_bytes(b"tiny-bound-video")

        package_dir = self.run_dir / "bound_model"
        self.paths = {
            "review_timing": self.run_dir / "review_timing.v1.json",
            "policy": self.run_dir / "inputs" / "policy.json",
            "decisions": self.run_dir / "inputs" / "decisions.json",
            "model": package_dir / MODEL_MANIFEST_NAME,
            "training_report": package_dir / TRAINING_REPORT_NAME,
            "model_weights": package_dir / MODEL_WEIGHTS_NAME,
            "dataset": self.run_dir / "inputs" / "dataset.json",
            "predictions": self.run_dir / "inputs" / "candidate_predictions.json",
            "contract": self.run_dir / TRACKING_CONTRACT_REPORT_NAME,
            "annotation_resolution": self.run_dir / "inputs" / "annotation_resolution.json",
            "resolved_tracking_contract": self.run_dir / "inputs" / "resolved_tracking_contract.json",
            "policy_roles": self.run_dir / "inputs" / "policy_roles.json",
        }
        payloads: dict[str, object] = {
            "review_timing": {"artifact_type": "selective_review_timing", "variants": []},
            "policy": {"artifact_type": "selective_policy"},
            "decisions": {"artifact_type": "selective_decisions"},
            "model": {"artifact_type": "candidate_classifier_model", "model_version": "fixture-v1"},
            "training_report": {"artifact_type": "candidate_classifier_training_report"},
            "dataset": {
                "artifact_type": "candidate_dataset",
                "sources": [
                    {
                        "path": str(self.source_video.resolve()),
                        "sha256": _sha256(self.source_video),
                    }
                ],
            },
            "predictions": {"artifact_type": "candidate_predictions", "predictions": []},
            "contract": {
                "schema_version": "2.0",
                "source": {"video_sha256": _sha256(self.source_video)},
                "candidates": [],
            },
            "annotation_resolution": {"artifact_type": "candidate_annotation_resolution"},
            "resolved_tracking_contract": {"schema_version": "2.0", "candidates": []},
            "policy_roles": {"artifact_type": "selective_policy_roles"},
        }
        for name, payload in payloads.items():
            _write_json(self.paths[name], payload)
        self.paths["model_weights"].write_bytes(b"bound-model-weights")

        self.queue_path = self.run_dir / REVIEW_QUEUE_NAME
        bindings: dict[str, dict[str, str]] = {}
        for name, path in self.paths.items():
            bindings[name] = {
                "path": path.resolve().relative_to(self.run_dir.resolve()).as_posix(),
                "sha256": _sha256(path),
            }
        bindings["decisions"]["source"] = "independent_artifact"
        _write_json(
            self.queue_path,
            {
                "schema_version": "1.0",
                "artifact_type": "selective_review_queue",
                "bindings": bindings,
                "items": [{"review_item_id": "window-1", "candidates": [{"candidate_id": "candidate-1"}]}],
            },
        )
        self.actions_path = self.run_dir / "review_decisions.json"
        _write_json(
            self.actions_path,
            {
                "schema_version": "1.0",
                "artifact_type": "selective_review_actions",
                "actions": [
                    {
                        "action_id": "action-1",
                        "review_item_id": "window-1",
                        "candidate_id": "candidate-1",
                        "action": "mark_unknown",
                    }
                ],
            },
        )
        action_track = self.run_dir / "action_track.csv"
        action_track.write_text("Frame,Action\n0,open_play\n", encoding="utf-8")
        action_report = self.run_dir / "action_signal_report.v1.json"
        _write_json(
            action_report,
            {
                "schema_version": "1.0",
                "artifact_type": "action_signal_report",
                "status": "complete",
                "input_video": str(self.source_video.resolve()),
                "artifacts": {"track": "action_track.csv"},
            },
        )
        _write_json(
            self.run_dir / orchestration.ACTION_SIGNAL_BINDING_NAME,
            {
                "schema_version": "1.0",
                "artifact_type": "broadcast_action_signal_binding",
                "source": {
                    "video_sha256": _sha256(self.source_video),
                    "tracking_contract_sha256": _sha256(self.paths["contract"]),
                },
                "artifacts": {
                    "action_track.csv": {
                        "sha256": _sha256(action_track),
                        "size_bytes": action_track.stat().st_size,
                    },
                    "action_signal_report.v1.json": {
                        "sha256": _sha256(action_report),
                        "size_bytes": action_report.stat().st_size,
                    },
                },
            },
        )


def _fake_materialize(
    queue_path: Path,
    actions_path: Path,
    _dataset_path: Path,
    _predictions_path: Path,
    _policy_path: Path,
    _model_path: Path,
    _contract_path: Path,
    output_dir: Path,
    **_kwargs: object,
) -> dict[str, object]:
    output_dir.mkdir(parents=True)
    votes_path = output_dir / HUMAN_VOTES_NAME
    votes_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "record_type": "ledger_header",
                "contract_sha256": "fixture-contract",
                "dataset_version": "fixture-dataset-v1",
                "evidence_manifest_sha256": "fixture-dataset",
                "source": "selective_review",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    contract_path = output_dir / "annotations" / TRACKING_CONTRACT_REPORT_NAME
    _write_json(
        contract_path,
        {
            "schema_version": "2.0",
            "source": {"video_sha256": "unused-by-orchestrator"},
            "frames": [{"frame_index": 0}],
            "candidates": [{"candidate_id": "candidate-1", "frame_index": 0, "bbox": [1, 2, 3, 4]}],
            "classifications": [],
            "decisions": [],
            "validation_errors": [],
        },
    )
    _write_json(
        output_dir / TRAJECTORY_CORRECTIONS_NAME,
        {
            "schema_version": "1.0",
            "artifact_type": "trajectory_corrections",
            "queue_sha256": _sha256(queue_path),
            "correction_count": 0,
            "corrections": [],
        },
    )
    resolution_path = output_dir / "annotations" / ANNOTATION_RESOLUTION_NAME
    _write_json(
        resolution_path,
        {
            "schema_version": "1.0",
            "artifact_type": "candidate_annotation_resolution",
            "source_vote_ledger": {"path": str(votes_path), "sha256": _sha256(votes_path)},
            "derived_tracking_contract": {
                "path": TRACKING_CONTRACT_REPORT_NAME,
                "sha256": _sha256(contract_path),
            },
            "summary": {"status": "complete"},
        },
    )
    adjudication_path = output_dir / "annotations" / ADJUDICATION_QUEUE_NAME
    _write_json(
        adjudication_path,
        {
            "schema_version": "1.0",
            "artifact_type": "candidate_annotation_adjudication_queue",
            "candidate_count": 0,
            "candidates": [],
        },
    )
    bindings = {
        **json.loads(queue_path.read_text(encoding="utf-8"))["bindings"],
        "queue": {"path": str(queue_path.resolve()), "sha256": _sha256(queue_path)},
        "actions": {"path": str(actions_path.resolve()), "sha256": _sha256(actions_path)},
    }
    summary = {"action_count": 1, "vote_count": 1, "trajectory_correction_count": 0}
    artifacts = {
        "human_votes": {"path": HUMAN_VOTES_NAME, "sha256": _sha256(votes_path)},
        "trajectory_corrections": {
            "path": TRAJECTORY_CORRECTIONS_NAME,
            "sha256": _sha256(output_dir / TRAJECTORY_CORRECTIONS_NAME),
        },
        "annotation_resolution": {
            "path": f"annotations/{ANNOTATION_RESOLUTION_NAME}",
            "sha256": _sha256(resolution_path),
        },
        "annotation_adjudication_queue": {
            "path": f"annotations/{ADJUDICATION_QUEUE_NAME}",
            "sha256": _sha256(adjudication_path),
        },
        "derived_annotations_contract": {
            "path": f"annotations/{TRACKING_CONTRACT_REPORT_NAME}",
            "sha256": _sha256(contract_path),
        },
    }
    round_report = {
        "schema_version": "1.0",
        "artifact_type": "active_learning_round",
        "round_id": "round-fixture",
        "status": "materialized",
        "training_invoked": False,
        "bindings": bindings,
        "summary": summary,
        "artifacts": artifacts,
    }
    round_path = output_dir / ACTIVE_ROUND_NAME
    _write_json(round_path, round_report)
    report = {
        "schema_version": "1.0",
        "artifact_type": "selective_review_materialization",
        "status": "complete",
        "training_invoked": False,
        "round_id": "round-fixture",
        "bindings": bindings,
        "summary": summary,
        "annotation_summary": {"status": "complete"},
        "artifacts": {
            **artifacts,
            "active_learning_round": {"path": ACTIVE_ROUND_NAME, "sha256": _sha256(round_path)},
        },
    }
    _write_json(output_dir / MATERIALIZATION_REPORT_NAME, report)
    return report


def _fake_classify(
    package_dir: Path,
    dataset_path: Path,
    source_contract_path: Path,
    output_dir: Path,
    *,
    batch_size: int,
) -> dict[str, object]:
    if batch_size <= 0:
        raise AssertionError("orchestrator passed an invalid batch size")
    output_dir.mkdir(parents=True)
    model = json.loads((package_dir / MODEL_MANIFEST_NAME).read_text(encoding="utf-8"))
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    source_contract = json.loads(source_contract_path.read_text(encoding="utf-8"))
    model_version = model["model_version"]
    candidates = source_contract["candidates"]
    report = {
        "schema_version": "1.0",
        "artifact_type": "candidate_predictions",
        "model_version": model_version,
        "dataset_version": dataset.get("dataset_version", "fixture-dataset-v1"),
        "source_contract_sha256": _sha256(source_contract_path),
        "class_order": ["match_ball", "unknown"],
        "temperature": 1.0,
        "prediction_count": 1,
        "predictions": [
            {
                "candidate_id": "candidate-1",
                "candidate_fingerprint": "fixture-candidate-fingerprint",
                "predicted_label": "match_ball",
                "confidence": 0.99,
                "probabilities": {"match_ball": 0.99, "unknown": 0.01},
                "model_version": model_version,
            }
        ],
    }
    _write_json(output_dir / PREDICTIONS_NAME, report)
    _write_json(
        output_dir / TRACKING_CONTRACT_REPORT_NAME,
        {
            "schema_version": "2.0",
            "source": source_contract.get("source"),
            "frames": source_contract.get("frames", []),
            "candidates": candidates,
            "classifications": [
                {
                    "candidate_id": "candidate-1",
                    "label": "match_ball",
                    "label_origin": "prelabel",
                    "confidence": 0.99,
                }
            ],
            "decisions": source_contract.get("decisions", []),
            "validation_errors": [],
        },
    )
    return report


def _fake_solve_trajectory(
    source_video: Path,
    source_contract: Path,
    predictions_path: Path,
    output_dir: Path,
) -> dict[str, object]:
    output_dir.mkdir(parents=True)
    track_path = output_dir / TRACK_NAME
    track_path.write_text("Frame,X,Y,Status\n0,10,20,observed\n", encoding="utf-8")
    decisions_path = output_dir / orchestration.TRAJECTORY_DECISIONS_NAME
    decisions_path.write_text('{"frame_index":0,"decision":"selected"}\n', encoding="utf-8")
    inputs = {
        "source video": {
            "path": str(source_video.resolve()),
            "sha256": _sha256(source_video),
            "size": source_video.stat().st_size,
        },
        "source tracking contract": {
            "path": str(source_contract.resolve()),
            "sha256": _sha256(source_contract),
            "size": source_contract.stat().st_size,
        },
        "candidate predictions": {
            "path": str(predictions_path.resolve()),
            "sha256": _sha256(predictions_path),
            "size": predictions_path.stat().st_size,
        },
    }
    report = {
        "schema_version": "1.0",
        "artifact_type": "global_ball_trajectory_report",
        "status": "succeeded",
        "complete": True,
        "algorithm": {"version": TRAJECTORY_ALGORITHM_VERSION},
        "source_video": {"sha256": _sha256(source_video)},
        "inputs": inputs,
        "artifacts": {
            TRACK_NAME: _artifact(track_path),
            orchestration.TRAJECTORY_DECISIONS_NAME: _artifact(decisions_path),
        },
    }
    _write_json(output_dir / TRAJECTORY_REPORT_NAME, report)
    return report


def _fake_solve_camera(
    source_video: Path,
    track_path: Path,
    trajectory_report_path: Path,
    output_dir: Path,
    *,
    config: HybridCameraConfig,
) -> dict[str, object]:
    output_dir.mkdir(parents=True)
    camera_path = output_dir / orchestration.CAMERA_PATH_NAME
    camera_path.write_text(
        "Frame,CenterX,CenterY,CropX1,CropY1,CropX2,CropY2,CropWidth,CropHeight,Status,PanMode\n"
        "0,10,20,0,0,32,18,32,18,observed,hold\n",
        encoding="utf-8",
    )
    motion_path = output_dir / orchestration.CAMERA_MOTION_EVIDENCE_NAME
    motion_path.write_text('{"frame_index":0,"status":"unknown"}\n', encoding="utf-8")
    decisions_path = output_dir / orchestration.CAMERA_DECISIONS_NAME
    decisions_path.write_text('{"frame_index":0,"decision":"hold"}\n', encoding="utf-8")
    audit_path = output_dir / orchestration.CAMERA_AUDIT_NAME
    _write_json(audit_path, {"summary": {"status": "ok", "frame_count": 1, "cut_count": 0}})
    report = {
        "schema_version": "1.0",
        "artifact_type": "hybrid_broadcast_camera_report",
        "status": "succeeded",
        "complete": True,
        "source_video": {
            "path": str(source_video.resolve()),
            "sha256": _sha256(source_video),
            "size": source_video.stat().st_size,
        },
        "inputs": {
            "global_ball_track": {"path": str(track_path), "sha256": _sha256(track_path)},
            "global_trajectory_report": {
                "path": str(trajectory_report_path),
                "sha256": _sha256(trajectory_report_path),
            },
        },
        "rendering": {
            "target_width": config.target_width,
            "target_height": config.target_height,
        },
        "artifacts": {
            orchestration.CAMERA_PATH_NAME: _artifact(camera_path),
            orchestration.CAMERA_MOTION_EVIDENCE_NAME: _artifact(motion_path),
            orchestration.CAMERA_DECISIONS_NAME: _artifact(decisions_path),
            orchestration.CAMERA_AUDIT_NAME: _artifact(audit_path),
        },
    }
    _write_json(output_dir / HYBRID_REPORT_NAME, report)
    return report


def _fake_render(
    _source_video: Path,
    _camera_path: Path,
    _hybrid_report: Path,
    output_video: Path,
    *,
    target_width: int,
    target_height: int,
) -> CameraPathRenderResult:
    output_video.write_bytes(b"rendered-broadcast-video")
    return CameraPathRenderResult(
        frame_count=1,
        target_width=target_width,
        target_height=target_height,
        status_counts={"observed": 1},
        output_video_path=output_video,
    )


class BroadcastHybridOrchestrationTests(unittest.TestCase):
    def _create_directory_link(self, link: Path, target: Path) -> None:
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                self.skipTest(f"directory junction unavailable: {completed.stderr or completed.stdout}")
            return
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")

    @staticmethod
    def _remove_directory_link(link: Path) -> None:
        if not os.path.lexists(link):
            return
        if os.name == "nt":
            os.rmdir(link)
        else:
            link.unlink()

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.fixture = _BoundRun(Path(self.temp_dir.name))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _publish_ready_final(self) -> dict[str, object]:
        with (
            mock.patch.object(orchestration, "materialize_selective_review_actions", side_effect=_fake_materialize),
            mock.patch.object(orchestration, "classify_candidates", side_effect=_fake_classify),
            mock.patch.object(orchestration, "solve_global_ball_trajectory", side_effect=_fake_solve_trajectory),
        ):
            recomputed = orchestration.recompute_reviewed_trajectory(self.fixture.run_dir)
        with (
            mock.patch.object(orchestration, "solve_hybrid_broadcast_camera", side_effect=_fake_solve_camera),
            mock.patch.object(orchestration, "render_camera_path_video", side_effect=_fake_render),
        ):
            return orchestration.render_broadcast_trajectory(
                self.fixture.run_dir,
                str(recomputed["trajectory_generation_id"]),
                target_width=32,
                target_height=18,
            )

    def test_exclusive_publication_rejects_a_dangling_target_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as external_dir:
            source = self.fixture.run_dir / "source-public.bin"
            source.write_bytes(b"bound public bytes")
            external_target = Path(external_dir).resolve() / "escaped.bin"
            target = self.fixture.run_dir / "public.bin"
            try:
                target.symlink_to(external_target)
            except OSError as exc:
                self.skipTest(f"file symlinks are unavailable: {exc}")

            with self.assertRaisesRegex(
                orchestration.BroadcastHybridOrchestrationError,
                "symlink or reparse",
            ):
                orchestration._publish_file_exclusive(
                    source,
                    target,
                    trusted_root=self.fixture.run_dir,
                )
            self.assertFalse(external_target.exists())

    def test_real_bound_success_path_is_deterministic_and_publishes_fixed_facade(self) -> None:
        with (
            mock.patch.object(
                orchestration, "materialize_selective_review_actions", side_effect=_fake_materialize
            ) as materialize,
            mock.patch.object(orchestration, "classify_candidates", side_effect=_fake_classify) as classify,
            mock.patch.object(
                orchestration, "solve_global_ball_trajectory", side_effect=_fake_solve_trajectory
            ) as trajectory,
        ):
            first = orchestration.recompute_reviewed_trajectory(self.fixture.run_dir, batch_size=4)
            second = orchestration.recompute_reviewed_trajectory(self.fixture.run_dir, batch_size=4)

        self.assertEqual(first, second)
        self.assertEqual(1, materialize.call_count)
        self.assertEqual(1, classify.call_count)
        self.assertEqual(1, trajectory.call_count)
        materialize_args = materialize.call_args.args
        self.assertEqual(self.fixture.queue_path.resolve(), materialize_args[0].resolve())
        self.assertEqual(self.fixture.paths["dataset"].resolve(), materialize_args[2].resolve())
        self.assertEqual(self.fixture.paths["model"].resolve(), materialize_args[5].resolve())
        self.assertEqual(self.fixture.paths["model"].parent.resolve(), classify.call_args.args[0].resolve())
        self.assertEqual(self.fixture.source_video.resolve(), trajectory.call_args.args[0].resolve())
        self.assertTrue(first["trajectory_generation_id"].startswith("trajectory-"))

        with (
            mock.patch.object(orchestration, "solve_hybrid_broadcast_camera", side_effect=_fake_solve_camera) as camera,
            mock.patch.object(orchestration, "render_camera_path_video", side_effect=_fake_render) as renderer,
        ):
            rendered = orchestration.render_broadcast_trajectory(
                self.fixture.run_dir,
                first["trajectory_generation_id"],
                target_width=32,
                target_height=18,
            )
            replayed = orchestration.render_broadcast_trajectory(
                self.fixture.run_dir,
                first["trajectory_generation_id"],
                target_width=32,
                target_height=18,
            )

        self.assertEqual(rendered, replayed)
        self.assertEqual(1, camera.call_count)
        self.assertEqual(1, renderer.call_count)
        self.assertEqual(set(orchestration.PUBLIC_ARTIFACTS), set(rendered["final_bindings"]["artifacts"]))
        self.assertEqual(
            orchestration.ACTION_SIGNAL_BINDING_NAME,
            rendered["final_bindings"]["artifacts"]["action_track.csv"]["source_report"]["path"],
        )
        for name in orchestration.PUBLIC_ARTIFACTS:
            self.assertTrue((self.fixture.run_dir / name).is_file(), name)
        camera_generation = self.fixture.run_dir / "broadcast_generations" / rendered["camera_generation_id"]
        render_generation = self.fixture.run_dir / "broadcast_generations" / rendered["render_generation_id"]
        self.assertEqual(
            _sha256(camera_generation / orchestration.CAMERA_PATH_NAME),
            _sha256(self.fixture.run_dir / "camera_target.csv"),
        )
        self.assertFalse((camera_generation / "broadcast.mp4").exists())
        self.assertTrue((render_generation / "broadcast.mp4").is_file())
        self.assertEqual(
            "match_ball",
            json.loads((self.fixture.run_dir / "candidate_classifications.jsonl").read_text())["predicted_label"],
        )
        self.assertIn("camera_solver_does_not_consume_action_track", rendered["limitations"])
        self.assertIn(orchestration.CANCELLATION_LIMITATION, rendered["limitations"])
        self.assertEqual(
            rendered["final_bindings"], orchestration.validate_final_broadcast_artifacts(self.fixture.run_dir)
        )
        quality_report = publish_broadcast_facade(self.fixture.run_dir)
        self.assertEqual("ready", quality_report["status"])
        self.assertTrue((self.fixture.run_dir / "broadcast_quality_report.json").is_file())
        immutable_track = Path(first["trajectory_report"]).parent / TRACK_NAME
        immutable_bytes = immutable_track.read_bytes()
        (self.fixture.run_dir / TRACK_NAME).write_bytes(b"mutated-public-copy")
        self.assertEqual(immutable_bytes, immutable_track.read_bytes())

    def test_ready_facade_revalidates_every_bound_dataset_sample_artifact(self) -> None:
        evidence = self.fixture.paths["dataset"].parent / "evidence.bin"
        evidence.write_bytes(b"original-evidence")
        dataset = json.loads(self.fixture.paths["dataset"].read_text(encoding="utf-8"))
        dataset["samples"] = [
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
        ]
        _write_json(self.fixture.paths["dataset"], dataset)
        queue = json.loads(self.fixture.queue_path.read_text(encoding="utf-8"))
        queue["bindings"]["dataset"]["sha256"] = _sha256(self.fixture.paths["dataset"])
        _write_json(self.fixture.queue_path, queue)

        self._publish_ready_final()
        quality = publish_broadcast_facade(self.fixture.run_dir)
        root_report = self.fixture.run_dir / "broadcast_quality_report.json"
        self.assertEqual(quality, validate_broadcast_quality_report(self.fixture.run_dir, root_report))

        evidence.write_bytes(b"mutated-evidence!")

        with self.assertRaisesRegex(BroadcastApiError, "hash changed"):
            validate_broadcast_quality_report(self.fixture.run_dir, root_report)

    def test_ready_facade_requires_its_exact_versioned_status_report(self) -> None:
        self._publish_ready_final()
        quality = publish_broadcast_facade(self.fixture.run_dir)
        root_report = self.fixture.run_dir / "broadcast_quality_report.json"
        versioned_report = (
            self.fixture.run_dir
            / "broadcast_status"
            / str(quality["status_generation"])
            / "broadcast_quality_report.json"
        )
        self.assertEqual(quality, validate_broadcast_quality_report(self.fixture.run_dir, root_report))

        versioned_report.unlink()

        with self.assertRaisesRegex(BroadcastApiError, "versioned broadcast quality report"):
            validate_broadcast_quality_report(self.fixture.run_dir, root_report)

    def test_zero_candidate_review_runs_preflight_classifier_and_trajectory(self) -> None:
        queue = json.loads(self.fixture.queue_path.read_text(encoding="utf-8"))
        queue["items"] = []
        queue["review_item_count"] = 0
        queue["candidate_count"] = 0
        _write_json(self.fixture.queue_path, queue)
        decisions = json.loads(self.fixture.actions_path.read_text(encoding="utf-8"))
        decisions["actions"] = []
        _write_json(self.fixture.actions_path, decisions)

        preflight = orchestration.preflight_recompute_reviewed_trajectory(self.fixture.run_dir)
        self.assertEqual(_sha256(self.fixture.actions_path), preflight["review_decisions_sha256"])
        with (
            mock.patch.object(
                orchestration,
                "materialize_selective_review_actions",
                side_effect=_fake_materialize,
            ) as materialize,
            mock.patch.object(orchestration, "classify_candidates", side_effect=_fake_classify) as classify,
            mock.patch.object(
                orchestration,
                "solve_global_ball_trajectory",
                side_effect=_fake_solve_trajectory,
            ) as trajectory,
        ):
            recomputed = orchestration.recompute_reviewed_trajectory(self.fixture.run_dir)

        self.assertTrue(str(recomputed["trajectory_generation_id"]).startswith("trajectory-"))
        materialize.assert_called_once()
        classify.assert_called_once()
        trajectory.assert_called_once()

    def test_correct_trajectory_is_rejected_before_any_generation_or_solver_call(self) -> None:
        actions = json.loads(self.fixture.actions_path.read_text(encoding="utf-8"))
        actions["actions"][0]["action"] = "correct_trajectory"
        _write_json(self.fixture.actions_path, actions)

        with (
            mock.patch.object(orchestration, "materialize_selective_review_actions") as materialize,
            mock.patch.object(orchestration, "classify_candidates") as classify,
            mock.patch.object(orchestration, "solve_global_ball_trajectory") as trajectory,
            self.assertRaisesRegex(orchestration.BroadcastHybridOrchestrationError, "correct_trajectory"),
        ):
            orchestration.recompute_reviewed_trajectory(self.fixture.run_dir)

        materialize.assert_not_called()
        classify.assert_not_called()
        trajectory.assert_not_called()
        self.assertFalse((self.fixture.run_dir / "broadcast_generations").exists())

    def test_changed_queue_binding_is_rejected_before_materialization(self) -> None:
        self.fixture.paths["predictions"].write_text("changed\n", encoding="utf-8")

        with (
            mock.patch.object(orchestration, "materialize_selective_review_actions") as materialize,
            self.assertRaisesRegex(orchestration.BroadcastHybridOrchestrationError, "queue-bound artifact changed"),
        ):
            orchestration.recompute_reviewed_trajectory(self.fixture.run_dir)

            materialize.assert_not_called()

    def test_recompute_preflight_hashes_the_large_source_video_once(self) -> None:
        real_hash = orchestration._sha256_file
        source_hash_calls = 0

        def counted_hash(path: Path) -> str:
            nonlocal source_hash_calls
            if Path(path).resolve() == self.fixture.source_video.resolve():
                source_hash_calls += 1
            return real_hash(path)

        with mock.patch.object(orchestration, "_sha256_file", side_effect=counted_hash):
            preflight = orchestration.preflight_recompute_reviewed_trajectory(self.fixture.run_dir)

        self.assertEqual(_sha256(self.fixture.source_video), preflight["source_video_sha256"])
        self.assertEqual(1, source_hash_calls)

    def test_final_validation_hashes_the_large_source_video_once(self) -> None:
        rendered = self._publish_ready_final()
        real_hash = orchestration._sha256_file
        source_hash_calls = 0
        run_directory_token = orchestration._stat_token(self.fixture.run_dir)

        def counted_hash(path: Path) -> str:
            nonlocal source_hash_calls
            if Path(path).resolve() == self.fixture.source_video.resolve():
                source_hash_calls += 1
            return real_hash(path)

        with mock.patch.object(orchestration, "_sha256_file", side_effect=counted_hash):
            validated = orchestration.validate_final_broadcast_artifacts(self.fixture.run_dir)

        self.assertEqual(rendered["final_bindings"], validated)
        self.assertEqual(1, source_hash_calls)
        self.assertEqual(run_directory_token, orchestration._stat_token(self.fixture.run_dir))

    def test_normal_render_reuses_the_trusted_trajectory_and_hashes_the_source_once(self) -> None:
        with (
            mock.patch.object(orchestration, "materialize_selective_review_actions", side_effect=_fake_materialize),
            mock.patch.object(orchestration, "classify_candidates", side_effect=_fake_classify),
            mock.patch.object(orchestration, "solve_global_ball_trajectory", side_effect=_fake_solve_trajectory),
        ):
            recomputed = orchestration.recompute_reviewed_trajectory(self.fixture.run_dir)
        real_hash = orchestration._sha256_file
        source_hash_calls = 0

        def counted_hash(path: Path) -> str:
            nonlocal source_hash_calls
            if Path(path).resolve() == self.fixture.source_video.resolve():
                source_hash_calls += 1
            return real_hash(path)

        with (
            mock.patch.object(orchestration, "_sha256_file", side_effect=counted_hash),
            mock.patch.object(orchestration, "solve_hybrid_broadcast_camera", side_effect=_fake_solve_camera),
            mock.patch.object(orchestration, "render_camera_path_video", side_effect=_fake_render),
        ):
            orchestration.render_broadcast_trajectory(
                self.fixture.run_dir,
                str(recomputed["trajectory_generation_id"]),
                target_width=32,
                target_height=18,
            )

        self.assertEqual(1, source_hash_calls)

    def test_final_validation_rejects_same_content_source_replacement(self) -> None:
        self._publish_ready_final()
        real_stream = orchestration._stream_json_array
        source_stat = self.fixture.source_video.stat()
        replaced = False

        def replace_source_after_stream(source: Path, prefix: str, target: Path) -> None:
            nonlocal replaced
            real_stream(source, prefix, target)
            if replaced:
                return
            replacement = self.fixture.source_video.with_suffix(".replacement")
            replacement.write_bytes(self.fixture.source_video.read_bytes())
            os.utime(replacement, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
            replacement.replace(self.fixture.source_video)
            replaced = True

        with (
            mock.patch.object(orchestration, "_stream_json_array", side_effect=replace_source_after_stream),
            self.assertRaisesRegex(
                orchestration.BroadcastHybridOrchestrationError,
                "final broadcast source video changed during validation",
            ),
        ):
            orchestration.validate_final_broadcast_artifacts(self.fixture.run_dir)

        self.assertTrue(replaced)

    def test_generation_root_symlink_is_rejected_before_external_writes(self) -> None:
        external = Path(self.temp_dir.name) / "external-generations"
        external.mkdir()
        generation_root = self.fixture.run_dir / "broadcast_generations"
        try:
            generation_root.symlink_to(external, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks are unavailable: {exc}")

        with (
            mock.patch.object(orchestration, "materialize_selective_review_actions") as materialize,
            self.assertRaisesRegex(orchestration.BroadcastHybridOrchestrationError, "generation root"),
        ):
            orchestration.recompute_reviewed_trajectory(self.fixture.run_dir)

        materialize.assert_not_called()
        self.assertEqual([], list(external.iterdir()))
        self.assertTrue(generation_root.is_symlink())
        self.assertEqual(external.resolve(), generation_root.resolve())

    def test_dangling_generation_root_symlink_is_rejected_before_external_writes(self) -> None:
        external = Path(self.temp_dir.name) / "missing-external-generations"
        generation_root = self.fixture.run_dir / "broadcast_generations"
        try:
            generation_root.symlink_to(external, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks are unavailable: {exc}")

        with (
            mock.patch.object(orchestration, "materialize_selective_review_actions") as materialize,
            self.assertRaisesRegex(orchestration.BroadcastHybridOrchestrationError, "generation root"),
        ):
            orchestration.recompute_reviewed_trajectory(self.fixture.run_dir)

        materialize.assert_not_called()
        self.assertTrue(generation_root.is_symlink())
        self.assertFalse(external.exists())

    def test_run_and_generation_directory_reparse_points_are_rejected(self) -> None:
        external_run = Path(self.temp_dir.name) / "external-run"
        external_run.mkdir()
        linked_run = Path(self.temp_dir.name) / "linked-run"
        self._create_directory_link(linked_run, external_run)
        try:
            with self.assertRaisesRegex(orchestration.BroadcastHybridOrchestrationError, "non-reparse"):
                orchestration._run_directory(linked_run)
        finally:
            self._remove_directory_link(linked_run)

        external_generations = Path(self.temp_dir.name) / "external-generations-reparse"
        external_generations.mkdir()
        generation_root = self.fixture.run_dir / "broadcast_generations"
        self._create_directory_link(generation_root, external_generations)
        try:
            with self.assertRaisesRegex(orchestration.BroadcastHybridOrchestrationError, "generation root"):
                orchestration._generation_root(self.fixture.run_dir, create=False)
        finally:
            self._remove_directory_link(generation_root)

    def test_cross_run_queue_lineage_is_rejected_before_materialization(self) -> None:
        foreign_dir = self.fixture.run_dir / "foreign"
        foreign_source = foreign_dir / "other.avi"
        foreign_source.parent.mkdir()
        foreign_source.write_bytes(b"different-match-video")
        foreign_contract = foreign_dir / TRACKING_CONTRACT_REPORT_NAME
        _write_json(
            foreign_contract,
            {
                "schema_version": "2.0",
                "source": {"video_sha256": _sha256(foreign_source)},
                "candidates": [],
            },
        )
        dataset = json.loads(self.fixture.paths["dataset"].read_text(encoding="utf-8"))
        dataset["sources"] = [{"path": str(foreign_source.resolve()), "sha256": _sha256(foreign_source)}]
        _write_json(self.fixture.paths["dataset"], dataset)
        queue = json.loads(self.fixture.queue_path.read_text(encoding="utf-8"))
        queue["bindings"]["dataset"]["sha256"] = _sha256(self.fixture.paths["dataset"])
        queue["bindings"]["contract"] = {
            "path": foreign_contract.relative_to(self.fixture.run_dir).as_posix(),
            "sha256": _sha256(foreign_contract),
        }
        _write_json(self.fixture.queue_path, queue)

        with (
            mock.patch.object(orchestration, "materialize_selective_review_actions") as materialize,
            self.assertRaisesRegex(orchestration.BroadcastHybridOrchestrationError, "run root tracking contract"),
        ):
            orchestration.recompute_reviewed_trajectory(self.fixture.run_dir)

        materialize.assert_not_called()
        self.assertFalse((self.fixture.run_dir / "broadcast_generations").exists())

    def test_invalid_dataset_artifact_type_is_rejected_before_materialization(self) -> None:
        dataset = json.loads(self.fixture.paths["dataset"].read_text(encoding="utf-8"))
        dataset["artifact_type"] = "candidate_dataset_manifest"
        _write_json(self.fixture.paths["dataset"], dataset)
        queue = json.loads(self.fixture.queue_path.read_text(encoding="utf-8"))
        queue["bindings"]["dataset"]["sha256"] = _sha256(self.fixture.paths["dataset"])
        _write_json(self.fixture.queue_path, queue)

        with (
            mock.patch.object(orchestration, "materialize_selective_review_actions") as materialize,
            self.assertRaisesRegex(orchestration.BroadcastHybridOrchestrationError, "artifact_type is invalid"),
        ):
            orchestration.recompute_reviewed_trajectory(self.fixture.run_dir)

        materialize.assert_not_called()
        self.assertFalse((self.fixture.run_dir / "broadcast_generations").exists())

    def test_forged_cached_materialization_is_recomputed_and_rejected(self) -> None:
        with (
            mock.patch.object(orchestration, "materialize_selective_review_actions", side_effect=_fake_materialize),
            mock.patch.object(orchestration, "classify_candidates", side_effect=RuntimeError("stop after review")),
            self.assertRaisesRegex(RuntimeError, "stop after review"),
        ):
            orchestration.recompute_reviewed_trajectory(self.fixture.run_dir)

        review_dir = next((self.fixture.run_dir / "broadcast_generations").glob("review-*"))
        contract_path = review_dir / "annotations" / TRACKING_CONTRACT_REPORT_NAME
        resolution_path = review_dir / "annotations" / ANNOTATION_RESOLUTION_NAME
        round_path = review_dir / ACTIVE_ROUND_NAME
        report_path = review_dir / MATERIALIZATION_REPORT_NAME

        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["candidates"][0]["bbox"] = [99, 98, 97, 96]
        _write_json(contract_path, contract)
        resolution = json.loads(resolution_path.read_text(encoding="utf-8"))
        resolution["derived_tracking_contract"]["sha256"] = _sha256(contract_path)
        _write_json(resolution_path, resolution)
        round_report = json.loads(round_path.read_text(encoding="utf-8"))
        round_report["artifacts"]["derived_annotations_contract"]["sha256"] = _sha256(contract_path)
        round_report["artifacts"]["annotation_resolution"]["sha256"] = _sha256(resolution_path)
        _write_json(round_path, round_report)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["artifacts"]["derived_annotations_contract"]["sha256"] = _sha256(contract_path)
        report["artifacts"]["annotation_resolution"]["sha256"] = _sha256(resolution_path)
        report["artifacts"]["active_learning_round"]["sha256"] = _sha256(round_path)
        _write_json(report_path, report)
        orchestration._VALIDATED_MATERIALIZATION_GENERATIONS.clear()

        with (
            mock.patch.object(
                orchestration,
                "materialize_selective_review_actions",
                side_effect=_fake_materialize,
            ) as materialize,
            mock.patch.object(orchestration, "classify_candidates") as classify,
            self.assertRaisesRegex(
                orchestration.BroadcastHybridOrchestrationError,
                "does not match the bound materializer output",
            ),
        ):
            orchestration.recompute_reviewed_trajectory(self.fixture.run_dir)

        materialize.assert_called_once()
        classify.assert_not_called()

    def test_forged_cached_classifier_core_is_recomputed_and_rejected(self) -> None:
        with (
            mock.patch.object(orchestration, "materialize_selective_review_actions", side_effect=_fake_materialize),
            mock.patch.object(orchestration, "classify_candidates", side_effect=_fake_classify),
            mock.patch.object(orchestration, "solve_global_ball_trajectory", side_effect=_fake_solve_trajectory),
        ):
            orchestration.recompute_reviewed_trajectory(self.fixture.run_dir)

        core_dir = next((self.fixture.run_dir / "broadcast_generations").glob("classifier-core-*"))
        predictions_path = core_dir / PREDICTIONS_NAME
        contract_path = core_dir / TRACKING_CONTRACT_REPORT_NAME
        predictions = json.loads(predictions_path.read_text(encoding="utf-8"))
        predictions["predictions"][0].update(
            {
                "predicted_label": "unknown",
                "confidence": 0.75,
                "probabilities": {"match_ball": 0.25, "unknown": 0.75},
            }
        )
        _write_json(predictions_path, predictions)
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["classifications"][0].update({"label": "unknown", "confidence": 0.75})
        _write_json(contract_path, contract)
        orchestration._VALIDATED_CLASSIFIER_GENERATIONS.clear()

        with (
            mock.patch.object(orchestration, "materialize_selective_review_actions", side_effect=_fake_materialize),
            mock.patch.object(orchestration, "classify_candidates", side_effect=_fake_classify) as classify,
            mock.patch.object(orchestration, "solve_global_ball_trajectory") as trajectory,
            self.assertRaisesRegex(
                orchestration.BroadcastHybridOrchestrationError,
                "does not match the bound classifier output",
            ),
        ):
            orchestration.recompute_reviewed_trajectory(self.fixture.run_dir)

        classify.assert_called_once()
        trajectory.assert_not_called()

    def test_forged_cached_trajectory_core_is_recomputed_and_rejected(self) -> None:
        with (
            mock.patch.object(orchestration, "materialize_selective_review_actions", side_effect=_fake_materialize),
            mock.patch.object(orchestration, "classify_candidates", side_effect=_fake_classify),
            mock.patch.object(orchestration, "solve_global_ball_trajectory", side_effect=_fake_solve_trajectory),
        ):
            recomputed = orchestration.recompute_reviewed_trajectory(self.fixture.run_dir)

        core_dir = next((self.fixture.run_dir / "broadcast_generations").glob("trajectory-core-*"))
        track_path = core_dir / TRACK_NAME
        report_path = core_dir / TRAJECTORY_REPORT_NAME
        track_path.write_text("Frame,X,Y,Status\n0,999,999,observed\n", encoding="utf-8")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["artifacts"][TRACK_NAME] = _artifact(track_path)
        _write_json(report_path, report)
        orchestration._VALIDATED_TRAJECTORY_GENERATIONS.clear()

        with (
            mock.patch.object(
                orchestration, "solve_global_ball_trajectory", side_effect=_fake_solve_trajectory
            ) as solve,
            self.assertRaisesRegex(
                orchestration.BroadcastHybridOrchestrationError,
                "does not match the bound trajectory solver output",
            ),
        ):
            orchestration.recompute_reviewed_trajectory(self.fixture.run_dir)

        solve.assert_called_once()
        self.assertTrue(str(recomputed["trajectory_generation_id"]).startswith("trajectory-"))

    def test_forged_cached_camera_core_is_recomputed_and_rejected(self) -> None:
        rendered = self._publish_ready_final()
        camera_dir = self.fixture.run_dir / "broadcast_generations" / str(rendered["camera_generation_id"])
        camera_path = camera_dir / orchestration.CAMERA_PATH_NAME
        report_path = camera_dir / HYBRID_REPORT_NAME
        camera_path.write_text(
            "Frame,CenterX,CenterY,CropX1,CropY1,CropX2,CropY2,CropWidth,CropHeight,Status,PanMode\n"
            "0,999,999,0,0,32,18,32,18,observed,hold\n",
            encoding="utf-8",
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["artifacts"][orchestration.CAMERA_PATH_NAME] = _artifact(camera_path)
        _write_json(report_path, report)
        orchestration._VALIDATED_CAMERA_GENERATIONS.clear()

        with (
            mock.patch.object(
                orchestration,
                "solve_hybrid_broadcast_camera",
                side_effect=_fake_solve_camera,
            ) as camera,
            mock.patch.object(orchestration, "render_camera_path_video") as renderer,
            self.assertRaisesRegex(
                orchestration.BroadcastHybridOrchestrationError,
                "does not match the bound camera solver output",
            ),
        ):
            orchestration.render_broadcast_trajectory(
                self.fixture.run_dir,
                str(rendered["trajectory_generation_id"]),
                target_width=32,
                target_height=18,
            )

        camera.assert_called_once()
        renderer.assert_not_called()

    def test_forged_cached_render_core_is_rerendered_and_rejected(self) -> None:
        rendered = self._publish_ready_final()
        render_dir = self.fixture.run_dir / "broadcast_generations" / str(rendered["render_generation_id"])
        video_path = render_dir / "broadcast.mp4"
        report_path = render_dir / orchestration.RENDER_REPORT_NAME
        video_path.write_bytes(b"FORGED VIDEO - RENDERER NEVER CALLED")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["artifacts"]["broadcast.mp4"] = _artifact(video_path)
        _write_json(report_path, report)
        orchestration._VALIDATED_RENDER_GENERATIONS.clear()

        with (
            mock.patch.object(orchestration, "solve_hybrid_broadcast_camera") as camera,
            mock.patch.object(orchestration, "render_camera_path_video", side_effect=_fake_render) as renderer,
            self.assertRaisesRegex(
                orchestration.BroadcastHybridOrchestrationError,
                "does not match the bound renderer output",
            ),
        ):
            orchestration.render_broadcast_trajectory(
                self.fixture.run_dir,
                str(rendered["trajectory_generation_id"]),
                target_width=32,
                target_height=18,
            )

        camera.assert_not_called()
        renderer.assert_called_once()

    def test_final_read_only_validation_never_recomputes_unknown_generations(self) -> None:
        rendered = self._publish_ready_final()
        for cache in (
            orchestration._VALIDATED_MATERIALIZATION_GENERATIONS,
            orchestration._VALIDATED_CLASSIFIER_GENERATIONS,
            orchestration._VALIDATED_TRAJECTORY_GENERATIONS,
            orchestration._VALIDATED_CAMERA_GENERATIONS,
            orchestration._VALIDATED_RENDER_GENERATIONS,
        ):
            cache.clear()

        with (
            mock.patch.object(orchestration, "materialize_selective_review_actions") as materialize,
            mock.patch.object(orchestration, "classify_candidates") as classify,
            mock.patch.object(orchestration, "solve_global_ball_trajectory") as trajectory,
            mock.patch.object(orchestration, "solve_hybrid_broadcast_camera") as camera,
            mock.patch.object(orchestration, "render_camera_path_video") as renderer,
        ):
            validated = orchestration.validate_final_broadcast_artifacts(self.fixture.run_dir)

        self.assertEqual(rendered["final_bindings"], validated)
        materialize.assert_not_called()
        classify.assert_not_called()
        trajectory.assert_not_called()
        camera.assert_not_called()
        renderer.assert_not_called()

    def test_classifier_core_publish_recovers_when_manifest_publish_was_interrupted(self) -> None:
        publish_manifest = orchestration._publish_manifest_generation

        def interrupt_classifier_manifest(directory: Path, name: str, payload: dict[str, Any]) -> None:
            if directory.name.startswith("classification-"):
                raise RuntimeError("simulated crash after classifier core publish")
            publish_manifest(directory, name, payload)

        with (
            mock.patch.object(orchestration, "materialize_selective_review_actions", side_effect=_fake_materialize),
            mock.patch.object(orchestration, "classify_candidates", side_effect=_fake_classify) as classify,
            mock.patch.object(
                orchestration, "solve_global_ball_trajectory", side_effect=_fake_solve_trajectory
            ) as trajectory,
        ):
            with (
                mock.patch.object(
                    orchestration, "_publish_manifest_generation", side_effect=interrupt_classifier_manifest
                ),
                self.assertRaisesRegex(RuntimeError, "simulated crash"),
            ):
                orchestration.recompute_reviewed_trajectory(self.fixture.run_dir)

            core_dirs = list((self.fixture.run_dir / "broadcast_generations").glob("classifier-core-*"))
            self.assertEqual(1, len(core_dirs))
            self.assertFalse((core_dirs[0] / orchestration.INFERENCE_REPORT_NAME).exists())
            recovered = orchestration.recompute_reviewed_trajectory(self.fixture.run_dir)

        self.assertEqual(1, classify.call_count)
        self.assertEqual(1, trajectory.call_count)
        self.assertTrue(recovered["classification_generation_id"].startswith("classification-"))

    def test_trajectory_core_publish_recovers_when_manifest_publish_was_interrupted(self) -> None:
        publish_manifest = orchestration._publish_manifest_generation

        def interrupt_trajectory_manifest(directory: Path, name: str, payload: dict[str, Any]) -> None:
            if directory.name.startswith("trajectory-"):
                raise RuntimeError("simulated crash after trajectory core publish")
            publish_manifest(directory, name, payload)

        with (
            mock.patch.object(orchestration, "materialize_selective_review_actions", side_effect=_fake_materialize),
            mock.patch.object(orchestration, "classify_candidates", side_effect=_fake_classify) as classify,
            mock.patch.object(
                orchestration, "solve_global_ball_trajectory", side_effect=_fake_solve_trajectory
            ) as trajectory,
        ):
            with (
                mock.patch.object(
                    orchestration, "_publish_manifest_generation", side_effect=interrupt_trajectory_manifest
                ),
                self.assertRaisesRegex(RuntimeError, "simulated crash"),
            ):
                orchestration.recompute_reviewed_trajectory(self.fixture.run_dir)

            core_dirs = list((self.fixture.run_dir / "broadcast_generations").glob("trajectory-core-*"))
            self.assertEqual(1, len(core_dirs))
            self.assertFalse((core_dirs[0] / orchestration.ORCHESTRATION_REPORT_NAME).exists())
            recovered = orchestration.recompute_reviewed_trajectory(self.fixture.run_dir)

        self.assertEqual(1, classify.call_count)
        self.assertEqual(1, trajectory.call_count)
        self.assertTrue(recovered["trajectory_generation_id"].startswith("trajectory-"))

    def test_render_rejects_traversal_and_mismatched_trajectory_generation_before_camera(self) -> None:
        with self.assertRaisesRegex(orchestration.BroadcastHybridOrchestrationError, "safe characters"):
            orchestration.render_broadcast_trajectory(self.fixture.run_dir, "trajectory-../../outside")

        with (
            mock.patch.object(orchestration, "materialize_selective_review_actions", side_effect=_fake_materialize),
            mock.patch.object(orchestration, "classify_candidates", side_effect=_fake_classify),
            mock.patch.object(orchestration, "solve_global_ball_trajectory", side_effect=_fake_solve_trajectory),
        ):
            recomputed = orchestration.recompute_reviewed_trajectory(self.fixture.run_dir)
        trajectory_dir = self.fixture.run_dir / "broadcast_generations" / recomputed["trajectory_generation_id"]
        report_path = trajectory_dir / orchestration.ORCHESTRATION_REPORT_NAME
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["generation_ids"]["trajectory"] = "trajectory-different"
        _write_json(report_path, report)

        with (
            mock.patch.object(orchestration, "solve_hybrid_broadcast_camera") as camera,
            self.assertRaisesRegex(orchestration.BroadcastHybridOrchestrationError, "different generation"),
        ):
            orchestration.render_broadcast_trajectory(
                self.fixture.run_dir,
                recomputed["trajectory_generation_id"],
            )
        camera.assert_not_called()

    def test_public_alias_conflict_is_not_overwritten_and_new_aliases_roll_back(self) -> None:
        with (
            mock.patch.object(orchestration, "materialize_selective_review_actions", side_effect=_fake_materialize),
            mock.patch.object(orchestration, "classify_candidates", side_effect=_fake_classify),
            mock.patch.object(orchestration, "solve_global_ball_trajectory", side_effect=_fake_solve_trajectory),
        ):
            recomputed = orchestration.recompute_reviewed_trajectory(self.fixture.run_dir)
        conflicting = self.fixture.run_dir / "camera_target.csv"
        conflicting.write_bytes(b"preexisting-public-camera")

        with (
            mock.patch.object(orchestration, "solve_hybrid_broadcast_camera", side_effect=_fake_solve_camera),
            mock.patch.object(orchestration, "render_camera_path_video", side_effect=_fake_render),
            self.assertRaisesRegex(orchestration.BroadcastHybridOrchestrationError, "refusing to overwrite"),
        ):
            orchestration.render_broadcast_trajectory(
                self.fixture.run_dir,
                recomputed["trajectory_generation_id"],
                target_width=32,
                target_height=18,
            )

        self.assertEqual(b"preexisting-public-camera", conflicting.read_bytes())
        self.assertFalse((self.fixture.run_dir / "ball_candidates.jsonl").exists())
        self.assertFalse((self.fixture.run_dir / "candidate_classifications.jsonl").exists())
        self.assertFalse((self.fixture.run_dir / TRACK_NAME).exists())
        self.assertFalse((self.fixture.run_dir / "broadcast.mp4").exists())
        self.assertFalse((self.fixture.run_dir / orchestration.FINAL_BINDINGS_NAME).exists())

    def test_cancellation_before_final_publish_leaves_no_new_root_public_artifacts(self) -> None:
        with (
            mock.patch.object(orchestration, "materialize_selective_review_actions", side_effect=_fake_materialize),
            mock.patch.object(orchestration, "classify_candidates", side_effect=_fake_classify),
            mock.patch.object(orchestration, "solve_global_ball_trajectory", side_effect=_fake_solve_trajectory),
        ):
            recomputed = orchestration.recompute_reviewed_trajectory(self.fixture.run_dir)

        check_count = 0

        def cancel_at_final_publish() -> bool:
            nonlocal check_count
            check_count += 1
            return check_count == 6

        with (
            mock.patch.object(orchestration, "solve_hybrid_broadcast_camera", side_effect=_fake_solve_camera),
            mock.patch.object(orchestration, "render_camera_path_video", side_effect=_fake_render) as renderer,
            self.assertRaises(CancelledError),
        ):
            orchestration.render_broadcast_trajectory(
                self.fixture.run_dir,
                recomputed["trajectory_generation_id"],
                target_width=32,
                target_height=18,
                should_cancel=cancel_at_final_publish,
            )

        renderer.assert_called_once()
        self.assertEqual(6, check_count)
        for name in (
            "ball_candidates.jsonl",
            "candidate_classifications.jsonl",
            TRACK_NAME,
            "camera_target.csv",
            "broadcast.mp4",
            orchestration.FINAL_BINDINGS_NAME,
        ):
            self.assertFalse((self.fixture.run_dir / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
