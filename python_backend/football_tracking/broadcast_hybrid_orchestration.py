from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import CancelledError
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ijson  # pyright: ignore[reportMissingImports]

from football_tracking.action_signal import (
    ACTION_SIGNAL_REPORT_NAME,
    ACTION_SIGNAL_SUCCESS_STATUSES,
    ACTION_TRACK_NAME,
)
from football_tracking.camera_path_renderer import (
    CAMERA_PATH_NAME,
    CameraPathRenderError,
    render_camera_path_video,
)
from football_tracking.candidate_annotations import ADJUDICATION_QUEUE_NAME, ANNOTATION_RESOLUTION_NAME
from football_tracking.candidate_classifier import (
    MODEL_MANIFEST_NAME,
    MODEL_WEIGHTS_NAME,
    PREDICTIONS_NAME,
    TRAINING_REPORT_NAME,
    ClassifierError,
    classify_candidates,
)
from football_tracking.global_ball_trajectory import (
    ALGORITHM_VERSION as TRAJECTORY_ALGORITHM_VERSION,
)
from football_tracking.global_ball_trajectory import DECISIONS_NAME as TRAJECTORY_DECISIONS_NAME
from football_tracking.global_ball_trajectory import (
    REPORT_NAME as TRAJECTORY_REPORT_NAME,
)
from football_tracking.global_ball_trajectory import (
    TRACK_NAME,
    GlobalBallTrajectoryError,
    solve_global_ball_trajectory,
)
from football_tracking.hybrid_broadcast_camera import (
    AUDIT_NAME as CAMERA_AUDIT_NAME,
)
from football_tracking.hybrid_broadcast_camera import (
    DECISIONS_NAME as CAMERA_DECISIONS_NAME,
)
from football_tracking.hybrid_broadcast_camera import (
    MOTION_EVIDENCE_NAME as CAMERA_MOTION_EVIDENCE_NAME,
)
from football_tracking.hybrid_broadcast_camera import (
    REPORT_NAME as HYBRID_REPORT_NAME,
)
from football_tracking.hybrid_broadcast_camera import (
    HybridBroadcastCameraError,
    HybridCameraConfig,
    solve_hybrid_broadcast_camera,
)
from football_tracking.selective_review import (
    ACTIVE_ROUND_NAME,
    HUMAN_VOTES_NAME,
    MATERIALIZATION_REPORT_NAME,
    REVIEW_QUEUE_NAME,
    TRAJECTORY_CORRECTIONS_NAME,
    SelectiveReviewError,
    materialize_selective_review_actions,
)
from football_tracking.tracking_contracts import TRACKING_CONTRACT_REPORT_NAME

ORCHESTRATION_VERSION = "broadcast-hybrid-orchestration-v1"
ORCHESTRATION_REPORT_NAME = "broadcast_trajectory_orchestration.v1.json"
INFERENCE_REPORT_NAME = "broadcast_classifier_inference.v1.json"
RENDER_REPORT_NAME = "broadcast_render_report.v1.json"
FINAL_BINDINGS_NAME = "broadcast_artifact_bindings.v1.json"
ACTION_SIGNAL_BINDING_NAME = "action_signal_binding.v1.json"
CANCELLATION_LIMITATION = "cooperative_cancellation_at_stage_boundaries_only"
PUBLIC_ARTIFACTS = (
    "ball_candidates.jsonl",
    "candidate_classifications.jsonl",
    TRACK_NAME,
    ACTION_TRACK_NAME,
    "review_decisions.json",
    "camera_target.csv",
    "broadcast.mp4",
)
_GENERATION_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,119}\Z")
_HASH_CHUNK_BYTES = 1024 * 1024
_MAX_JSON_BYTES = 256 * 1024 * 1024
_WINDOWS_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_GENERATION_VALIDATION_CACHE_LOCK = threading.RLock()
_VALIDATED_MATERIALIZATION_GENERATIONS: dict[Path, tuple[tuple[str, str], ...]] = {}
_VALIDATED_CLASSIFIER_GENERATIONS: dict[Path, tuple[tuple[str, str], ...]] = {}
_VALIDATED_TRAJECTORY_GENERATIONS: dict[Path, tuple[tuple[str, str], ...]] = {}
_VALIDATED_CAMERA_GENERATIONS: dict[Path, tuple[tuple[str, str], ...]] = {}
_VALIDATED_RENDER_GENERATIONS: dict[Path, tuple[tuple[str, str], ...]] = {}


class BroadcastHybridOrchestrationError(RuntimeError):
    """Raised when a broadcast generation cannot be built without weakening evidence."""


@dataclass(frozen=True)
class _SourceVideoSnapshot:
    path: Path
    sha256: str
    stat_token: tuple[int, int, int, int, int]


def preflight_recompute_reviewed_trajectory(run_dir: Path) -> dict[str, Any]:
    """Validate every immutable recompute input without creating generations."""

    run_dir = _run_directory(run_dir)
    queue_path = run_dir / REVIEW_QUEUE_NAME
    actions_path = run_dir / "review_decisions.json"
    queue, queue_sha256 = _load_json_snapshot(queue_path, "selective review queue")
    actions, actions_sha256 = _load_json_snapshot(actions_path, "review decisions")
    if queue.get("artifact_type") != "selective_review_queue":
        raise BroadcastHybridOrchestrationError("selective review queue artifact_type is invalid")
    if actions.get("artifact_type") != "selective_review_actions":
        raise BroadcastHybridOrchestrationError("review decisions artifact_type is invalid")
    raw_actions = actions.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise BroadcastHybridOrchestrationError("review decisions must contain at least one action")
    if any(isinstance(action, dict) and action.get("action") == "correct_trajectory" for action in raw_actions):
        raise BroadcastHybridOrchestrationError(
            "correct_trajectory is not supported because global_ball_trajectory does not consume trajectory corrections"
        )
    bound = _resolve_queue_bindings(queue_path, queue)
    source_video, source_video_sha256 = _source_video_from_dataset(bound["dataset"])
    source_contract_sha256, action_signal_binding_path = _validate_run_lineage(
        run_dir=run_dir,
        queue_contract_path=bound["contract"],
        dataset_source_video=source_video,
        dataset_source_sha256=source_video_sha256,
    )
    return {
        "queue_sha256": queue_sha256,
        "review_decisions_sha256": actions_sha256,
        "source_video_sha256": source_video_sha256,
        "source_contract_sha256": source_contract_sha256,
        "action_signal_binding_sha256": _sha256_file(action_signal_binding_path),
        "dataset_sha256": _sha256_file(bound["dataset"]),
        "model_manifest_sha256": _sha256_file(bound["model"]),
        "model_weights_sha256": _sha256_file(bound["model_weights"]),
        "training_report_sha256": _sha256_file(bound["training_report"]),
    }


def preflight_render_broadcast_trajectory(
    run_dir: Path,
    trajectory_generation_id: str,
    *,
    target_width: int = 1920,
    target_height: int = 1080,
) -> dict[str, Any]:
    """Validate a completed trajectory generation and render inputs without writing files."""

    run_dir = _run_directory(run_dir)
    trajectory_generation_id = _generation_id(trajectory_generation_id, prefix="trajectory-")
    trajectory_dir = _contained_generation(run_dir, trajectory_generation_id)
    orchestration, trajectory_core_dir, _ = _validate_trajectory_generation(trajectory_dir, None, run_dir)
    trajectory_report_path = trajectory_core_dir / TRAJECTORY_REPORT_NAME
    trajectory_report, trajectory_report_sha256 = _load_json_snapshot(
        trajectory_report_path, "global trajectory report"
    )
    source_video = _source_video_snapshot_from_trajectory_report(trajectory_report)
    orchestration_inputs = _required_mapping(orchestration.get("inputs"), "trajectory orchestration inputs")
    _validate_action_signal_binding(
        run_dir,
        source_video=source_video.path,
        source_video_sha256=source_video.sha256,
        source_contract_sha256=_required_sha256(
            orchestration_inputs.get("source_contract_sha256"), "trajectory source contract sha256"
        ),
    )
    _verify_source_video_snapshot_unchanged(source_video, "trajectory-bound source video")
    return {
        "trajectory_generation_id": trajectory_generation_id,
        "trajectory_orchestration_sha256": _sha256_file(trajectory_dir / ORCHESTRATION_REPORT_NAME),
        "trajectory_report_sha256": trajectory_report_sha256,
        "source_video_sha256": source_video.sha256,
        "target_width": _positive_int(target_width, "target_width"),
        "target_height": _positive_int(target_height, "target_height"),
    }


def recompute_reviewed_trajectory(
    run_dir: Path,
    *,
    batch_size: int = 32,
    should_cancel: Callable[[], bool] | None = None,
    before_commit: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Materialize bound review actions, re-run the bound model, and solve a new trajectory."""

    run_dir = _run_directory(run_dir)
    batch_size = _positive_int(batch_size, "batch_size")
    _raise_if_cancelled(should_cancel)
    queue_path = run_dir / REVIEW_QUEUE_NAME
    actions_path = run_dir / "review_decisions.json"
    queue, queue_sha256 = _load_json_snapshot(queue_path, "selective review queue")
    actions, actions_sha256 = _load_json_snapshot(actions_path, "review decisions")
    if queue.get("artifact_type") != "selective_review_queue":
        raise BroadcastHybridOrchestrationError("selective review queue artifact_type is invalid")
    if actions.get("artifact_type") != "selective_review_actions":
        raise BroadcastHybridOrchestrationError("review decisions artifact_type is invalid")
    raw_actions = actions.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise BroadcastHybridOrchestrationError("review decisions must contain at least one action")
    if any(isinstance(action, dict) and action.get("action") == "correct_trajectory" for action in raw_actions):
        raise BroadcastHybridOrchestrationError(
            "correct_trajectory is not supported because global_ball_trajectory does not consume trajectory corrections"
        )

    bound = _resolve_queue_bindings(queue_path, queue)
    source_video, source_video_sha256 = _source_video_from_dataset(bound["dataset"])
    source_contract_sha256, action_signal_binding_path = _validate_run_lineage(
        run_dir=run_dir,
        queue_contract_path=bound["contract"],
        dataset_source_video=source_video,
        dataset_source_sha256=source_video_sha256,
    )
    generations = _generation_root(run_dir, create=True)

    review_id = (
        "review-"
        + _identity_sha256(
            {"version": ORCHESTRATION_VERSION, "queue_sha256": queue_sha256, "actions_sha256": actions_sha256}
        )[:24]
    )
    review_dir = generations / review_id
    _raise_if_cancelled(should_cancel)
    review_created = False
    if not review_dir.exists():
        materialize_selective_review_actions(
            queue_path,
            actions_path,
            bound["dataset"],
            bound["predictions"],
            bound["policy"],
            bound["model"],
            bound["contract"],
            review_dir,
            decisions_path=bound["decisions"],
            annotation_resolution_path=bound["annotation_resolution"],
            resolved_contract_path=bound["resolved_tracking_contract"],
            policy_roles_path=bound["policy_roles"],
        )
        review_created = True
    _raise_if_cancelled(should_cancel)
    materialization = _validate_materialization(
        run_dir,
        review_dir,
        queue_path,
        actions_path,
        trust_new_generation=review_created,
    )
    if materialization["summary"].get("trajectory_correction_count") != 0:
        raise BroadcastHybridOrchestrationError("materialized review unexpectedly contains trajectory corrections")
    reviewed_contract = review_dir / "annotations" / TRACKING_CONTRACT_REPORT_NAME
    reviewed_contract_sha256 = _sha256_file(reviewed_contract)

    inference_identity = {
        "version": ORCHESTRATION_VERSION,
        "batch_size": batch_size,
        "reviewed_contract_sha256": reviewed_contract_sha256,
        "dataset_sha256": _sha256_file(bound["dataset"]),
        "model_manifest_sha256": _sha256_file(bound["model"]),
        "model_weights_sha256": _sha256_file(bound["model_weights"]),
        "training_report_sha256": _sha256_file(bound["training_report"]),
    }
    inference_digest = _identity_sha256(inference_identity)[:24]
    inference_core_id = "classifier-core-" + inference_digest
    inference_core_dir = generations / inference_core_id
    _raise_if_cancelled(should_cancel)
    classifier_created = False
    if not inference_core_dir.exists():
        classify_candidates(
            bound["model"].parent,
            bound["dataset"],
            reviewed_contract,
            inference_core_dir,
            batch_size=batch_size,
        )
        classifier_created = True
    _raise_if_cancelled(should_cancel)
    _validate_classifier_core(
        run_dir,
        inference_core_dir,
        inference_identity,
        reviewed_contract,
        trust_new_generation=classifier_created,
    )

    inference_id = "classification-" + inference_digest
    inference_dir = generations / inference_id
    if not inference_dir.exists():
        _publish_manifest_generation(
            inference_dir,
            INFERENCE_REPORT_NAME,
            {
                "schema_version": "1.0",
                "artifact_type": "broadcast_classifier_inference",
                "generated_at": _utc_now_iso(),
                "generation_id": inference_id,
                "core_generation_id": inference_core_id,
                "inputs": inference_identity,
                "artifacts": {
                    PREDICTIONS_NAME: _file_binding(inference_core_dir / PREDICTIONS_NAME, run_dir),
                    TRACKING_CONTRACT_REPORT_NAME: _file_binding(
                        inference_core_dir / TRACKING_CONTRACT_REPORT_NAME, run_dir
                    ),
                },
            },
        )
    _, predictions_path, _ = _validate_inference(
        run_dir,
        inference_dir,
        inference_identity,
        reviewed_contract,
        verify_unknown=True,
    )

    trajectory_identity = {
        "version": ORCHESTRATION_VERSION,
        "algorithm_version": TRAJECTORY_ALGORITHM_VERSION,
        "source_video_sha256": source_video_sha256,
        "source_contract_sha256": source_contract_sha256,
        "action_signal_binding_sha256": _sha256_file(action_signal_binding_path),
        "reviewed_contract_sha256": reviewed_contract_sha256,
        "predictions_sha256": _sha256_file(predictions_path),
    }
    trajectory_digest = _identity_sha256(trajectory_identity)[:24]
    trajectory_core_id = "trajectory-core-" + trajectory_digest
    trajectory_core_dir = generations / trajectory_core_id
    _raise_if_cancelled(should_cancel)
    trajectory_created = False
    if not trajectory_core_dir.exists():
        solve_global_ball_trajectory(source_video, reviewed_contract, predictions_path, trajectory_core_dir)
        trajectory_created = True
    _raise_if_cancelled(should_cancel)
    _validate_trajectory_core(
        run_dir,
        trajectory_core_dir,
        trajectory_identity,
        source_video,
        reviewed_contract,
        predictions_path,
        trust_new_generation=trajectory_created,
        verify_unknown=True,
    )
    trajectory_report_path = trajectory_core_dir / TRAJECTORY_REPORT_NAME

    trajectory_id = "trajectory-" + trajectory_digest
    trajectory_dir = generations / trajectory_id
    _raise_if_cancelled(should_cancel)
    if before_commit is not None:
        before_commit()
    if not trajectory_dir.exists():
        _publish_manifest_generation(
            trajectory_dir,
            ORCHESTRATION_REPORT_NAME,
            {
                "schema_version": "1.0",
                "artifact_type": "broadcast_trajectory_orchestration",
                "generated_at": _utc_now_iso(),
                "orchestration_version": ORCHESTRATION_VERSION,
                "limitations": [CANCELLATION_LIMITATION],
                "generation_ids": {
                    "review": review_id,
                    "classification": inference_id,
                    "trajectory": trajectory_id,
                },
                "core_generation_ids": {
                    "classification": inference_core_id,
                    "trajectory": trajectory_core_id,
                },
                "inputs": trajectory_identity,
                "bindings": {
                    "queue": _file_binding(queue_path, run_dir),
                    "review_decisions": _file_binding(actions_path, run_dir),
                    "root_tracking_contract": _file_binding(run_dir / TRACKING_CONTRACT_REPORT_NAME, run_dir),
                    "action_signal_binding": _file_binding(action_signal_binding_path, run_dir),
                    "materialization_report": _file_binding(review_dir / MATERIALIZATION_REPORT_NAME, run_dir),
                    "inference_report": _file_binding(inference_dir / INFERENCE_REPORT_NAME, run_dir),
                    "trajectory_report": _file_binding(trajectory_report_path, run_dir),
                },
            },
        )
    orchestration, validated_trajectory_core, _ = _validate_trajectory_generation(
        trajectory_dir, trajectory_identity, run_dir
    )
    _verify_unchanged(queue_path, queue_sha256, "selective review queue")
    _verify_unchanged(actions_path, actions_sha256, "review decisions")
    _raise_if_cancelled(should_cancel)
    return {
        "status": "completed",
        "review_generation_id": review_id,
        "classification_generation_id": inference_id,
        "trajectory_generation_id": trajectory_id,
        "trajectory_report": str(validated_trajectory_core / TRAJECTORY_REPORT_NAME),
        "orchestration_report": orchestration,
    }


def render_broadcast_trajectory(
    run_dir: Path,
    trajectory_generation_id: str,
    *,
    target_width: int = 1920,
    target_height: int = 1080,
    should_cancel: Callable[[], bool] | None = None,
    before_commit: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Render a validated trajectory generation and publish the fixed public artifact facade."""

    run_dir = _run_directory(run_dir)
    _raise_if_cancelled(should_cancel)
    trajectory_generation_id = _generation_id(trajectory_generation_id, prefix="trajectory-")
    trajectory_dir = _contained_generation(run_dir, trajectory_generation_id)
    orchestration, trajectory_core_dir, inference_core_dir = _validate_trajectory_generation(
        trajectory_dir,
        None,
        run_dir,
        verify_unknown=True,
    )
    trajectory_report_path = trajectory_core_dir / TRAJECTORY_REPORT_NAME
    trajectory_report, trajectory_report_sha256 = _load_json_snapshot(
        trajectory_report_path, "global trajectory report"
    )
    track_path = trajectory_core_dir / TRACK_NAME
    _validate_report_artifact(trajectory_report, TRACK_NAME, track_path)
    source_video = _source_video_snapshot_from_trajectory_report(trajectory_report)
    orchestration_inputs = _required_mapping(orchestration.get("inputs"), "trajectory orchestration inputs")
    _validate_action_signal_binding(
        run_dir,
        source_video=source_video.path,
        source_video_sha256=source_video.sha256,
        source_contract_sha256=_required_sha256(
            orchestration_inputs.get("source_contract_sha256"), "trajectory source contract sha256"
        ),
    )

    camera_identity = {
        "version": ORCHESTRATION_VERSION,
        "trajectory_report_sha256": trajectory_report_sha256,
        "target_width": _positive_int(target_width, "target_width"),
        "target_height": _positive_int(target_height, "target_height"),
    }
    camera_id = "camera-" + _identity_sha256(camera_identity)[:24]
    camera_dir = run_dir / "broadcast_generations" / camera_id
    _raise_if_cancelled(should_cancel)
    camera_created = False
    if not camera_dir.exists():
        solve_hybrid_broadcast_camera(
            source_video.path,
            track_path,
            trajectory_report_path,
            camera_dir,
            config=HybridCameraConfig(target_width=target_width, target_height=target_height),
        )
        camera_created = True
    _raise_if_cancelled(should_cancel)
    hybrid_report_path = camera_dir / HYBRID_REPORT_NAME
    camera_path = camera_dir / CAMERA_PATH_NAME
    _validate_camera_core(
        run_dir,
        camera_dir,
        source_video=source_video.path,
        track_path=track_path,
        trajectory_report_path=trajectory_report_path,
        source_video_sha256=source_video.sha256,
        target_width=target_width,
        target_height=target_height,
        trust_new_generation=camera_created,
        verify_unknown=True,
    )
    hybrid_report_sha256 = _sha256_file(hybrid_report_path)

    render_identity = {
        "version": ORCHESTRATION_VERSION,
        "hybrid_report_sha256": hybrid_report_sha256,
        "target_width": target_width,
        "target_height": target_height,
    }
    render_id = "render-" + _identity_sha256(render_identity)[:24]
    render_dir = run_dir / "broadcast_generations" / render_id
    _raise_if_cancelled(should_cancel)
    render_created = False
    if not render_dir.exists():
        _build_render_generation(
            render_dir,
            render_identity=render_identity,
            source_video=source_video,
            camera_path=camera_path,
            hybrid_report_path=hybrid_report_path,
            camera_id=camera_id,
            target_width=target_width,
            target_height=target_height,
            should_cancel=should_cancel,
        )
        render_created = True
    _validate_render_core(
        run_dir,
        render_dir,
        render_identity=render_identity,
        source_video=source_video,
        camera_path=camera_path,
        hybrid_report_path=hybrid_report_path,
        camera_id=camera_id,
        target_width=target_width,
        target_height=target_height,
        trust_new_generation=render_created,
        verify_unknown=True,
    )
    _raise_if_cancelled(should_cancel)
    _verify_source_video_snapshot_unchanged(source_video, "trajectory-bound source video")
    if before_commit is not None:
        before_commit()
    final_manifest = _publish_public_artifacts(
        run_dir=run_dir,
        orchestration=orchestration,
        trajectory_dir=trajectory_core_dir,
        inference_core_dir=inference_core_dir,
        camera_dir=camera_dir,
        render_dir=render_dir,
    )
    return {
        "status": "completed",
        "trajectory_generation_id": trajectory_generation_id,
        "camera_generation_id": camera_id,
        "render_generation_id": render_id,
        "broadcast_video": str(run_dir / "broadcast.mp4"),
        "final_bindings": final_manifest,
        "limitations": ["camera_solver_does_not_consume_action_track", CANCELLATION_LIMITATION],
    }


def validate_final_broadcast_artifacts(run_dir: Path) -> dict[str, Any]:
    """Rebuild final lineage and verify every public alias against immutable generations."""

    run_dir = _run_directory(run_dir)
    manifest_path = run_dir / FINAL_BINDINGS_NAME
    manifest, _ = _load_json_snapshot(manifest_path, "broadcast final artifact bindings")
    if (
        manifest.get("schema_version") != "1.0"
        or manifest.get("artifact_type") != "broadcast_artifact_bindings"
        or manifest.get("orchestration_version") != ORCHESTRATION_VERSION
    ):
        raise BroadcastHybridOrchestrationError("broadcast final artifact bindings envelope is invalid")
    generation_ids = _required_mapping(manifest.get("generation_ids"), "final generation ids")
    if set(generation_ids) != {"review", "classification", "trajectory", "camera", "render"}:
        raise BroadcastHybridOrchestrationError("broadcast final generation ids are incomplete or unexpected")

    trajectory_id = _generation_id(generation_ids.get("trajectory"), prefix="trajectory-")
    trajectory_dir = _contained_generation(run_dir, trajectory_id)
    orchestration, trajectory_core_dir, inference_core_dir = _validate_trajectory_generation(
        trajectory_dir, None, run_dir
    )
    orchestration_ids = _required_mapping(orchestration.get("generation_ids"), "trajectory generation ids")
    for name in ("review", "classification", "trajectory"):
        if generation_ids.get(name) != orchestration_ids.get(name):
            raise BroadcastHybridOrchestrationError(f"final generation id is stale: {name}")

    trajectory_report_path = trajectory_core_dir / TRAJECTORY_REPORT_NAME
    trajectory_report, trajectory_report_sha256 = _load_json_snapshot(
        trajectory_report_path, "global trajectory report"
    )
    track_path = trajectory_core_dir / TRACK_NAME
    _validate_report_artifact(trajectory_report, TRACK_NAME, track_path)
    source_video = _source_video_snapshot_from_trajectory_report(trajectory_report)

    camera_id = _generation_id(generation_ids.get("camera"), prefix="camera-")
    camera_dir = _contained_generation(run_dir, camera_id)
    hybrid_report_path = camera_dir / HYBRID_REPORT_NAME
    hybrid_report, hybrid_report_sha256 = _load_json_snapshot(hybrid_report_path, "hybrid camera report")
    rendering = _required_mapping(hybrid_report.get("rendering"), "hybrid camera rendering")
    target_width = _positive_int(rendering.get("target_width"), "hybrid target_width")
    target_height = _positive_int(rendering.get("target_height"), "hybrid target_height")
    camera_path = camera_dir / CAMERA_PATH_NAME
    _validate_camera_core(
        run_dir,
        camera_dir,
        source_video=source_video.path,
        track_path=track_path,
        trajectory_report_path=trajectory_report_path,
        source_video_sha256=source_video.sha256,
        target_width=target_width,
        target_height=target_height,
        verify_unknown=False,
    )
    expected_camera_id = (
        "camera-"
        + _identity_sha256(
            {
                "version": ORCHESTRATION_VERSION,
                "trajectory_report_sha256": trajectory_report_sha256,
                "target_width": target_width,
                "target_height": target_height,
            }
        )[:24]
    )
    if camera_id != expected_camera_id:
        raise BroadcastHybridOrchestrationError("final camera generation id is not deterministic")

    render_id = _generation_id(generation_ids.get("render"), prefix="render-")
    render_dir = _contained_generation(run_dir, render_id)
    render_report_path = render_dir / RENDER_REPORT_NAME
    render_identity = {
        "version": ORCHESTRATION_VERSION,
        "hybrid_report_sha256": hybrid_report_sha256,
        "target_width": target_width,
        "target_height": target_height,
    }
    expected_render_id = "render-" + _identity_sha256(render_identity)[:24]
    if render_id != expected_render_id:
        raise BroadcastHybridOrchestrationError("final render generation identity is stale")
    _validate_render_core(
        run_dir,
        render_dir,
        render_identity=render_identity,
        source_video=source_video,
        camera_path=camera_path,
        hybrid_report_path=hybrid_report_path,
        camera_id=camera_id,
        target_width=target_width,
        target_height=target_height,
        verify_unknown=False,
    )
    rendered_video = render_dir / "broadcast.mp4"

    review_id = _generation_id(generation_ids.get("review"), prefix="review-")
    review_dir = _contained_generation(run_dir, review_id)
    materialized_contract = review_dir / "annotations" / TRACKING_CONTRACT_REPORT_NAME
    predictions = inference_core_dir / PREDICTIONS_NAME
    action_track = run_dir / ACTION_TRACK_NAME
    action_binding = run_dir / ACTION_SIGNAL_BINDING_NAME
    review_decisions = run_dir / "review_decisions.json"
    orchestration_inputs = _required_mapping(orchestration.get("inputs"), "trajectory orchestration inputs")
    _validate_action_signal_binding(
        run_dir,
        source_video=source_video.path,
        source_video_sha256=source_video.sha256,
        source_contract_sha256=_required_sha256(
            orchestration_inputs.get("source_contract_sha256"), "trajectory source contract sha256"
        ),
    )

    source_reports = {
        "ball_candidates.jsonl": materialized_contract,
        "candidate_classifications.jsonl": predictions,
        TRACK_NAME: trajectory_report_path,
        ACTION_TRACK_NAME: action_binding,
        "review_decisions.json": review_dir / MATERIALIZATION_REPORT_NAME,
        "camera_target.csv": hybrid_report_path,
        "broadcast.mp4": render_report_path,
    }
    direct_sources = {
        TRACK_NAME: track_path,
        ACTION_TRACK_NAME: action_track,
        "review_decisions.json": review_decisions,
        "camera_target.csv": camera_path,
        "broadcast.mp4": rendered_video,
    }
    bindings = _required_mapping(manifest.get("artifacts"), "final public artifact bindings")
    if set(bindings) != set(PUBLIC_ARTIFACTS):
        raise BroadcastHybridOrchestrationError("final public artifact bindings are incomplete or unexpected")

    staging = Path(tempfile.mkdtemp(prefix=".broadcast-final-validation-", dir=run_dir))
    try:
        _stream_json_array(materialized_contract, "candidates.item", staging / "ball_candidates.jsonl")
        _stream_json_array(predictions, "predictions.item", staging / "candidate_classifications.jsonl")
        direct_sources["ball_candidates.jsonl"] = staging / "ball_candidates.jsonl"
        direct_sources["candidate_classifications.jsonl"] = staging / "candidate_classifications.jsonl"
        for name in PUBLIC_ARTIFACTS:
            public_path = run_dir / name
            if _is_link_or_reparse(public_path) or public_path.resolve().parent != run_dir or not public_path.is_file():
                raise BroadcastHybridOrchestrationError(f"final public artifact is unavailable: {name}")
            expected_binding = {
                "sha256": _sha256_file(public_path),
                "size_bytes": public_path.stat().st_size,
                "source_report": _file_binding(source_reports[name], run_dir),
            }
            if bindings.get(name) != expected_binding:
                raise BroadcastHybridOrchestrationError(f"final public artifact binding is stale: {name}")
            source_path = direct_sources[name]
            if public_path.stat().st_size != source_path.stat().st_size or _sha256_file(public_path) != _sha256_file(
                source_path
            ):
                raise BroadcastHybridOrchestrationError(f"final public artifact does not match its source: {name}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    _verify_source_video_snapshot_unchanged(source_video, "final broadcast source video")
    return manifest


def rollback_uncommitted_final_public_artifacts(run_dir: Path) -> None:
    """Remove only hash-matching final aliases when no ready quality report committed them."""

    run_dir = _run_directory(run_dir)
    if (run_dir / "broadcast_quality_report.json").exists():
        raise BroadcastHybridOrchestrationError("ready broadcast artifacts cannot be rolled back")
    manifest_path = run_dir / FINAL_BINDINGS_NAME
    manifest, _ = _load_json_snapshot(manifest_path, "broadcast final artifact bindings")
    if manifest.get("artifact_type") != "broadcast_artifact_bindings":
        raise BroadcastHybridOrchestrationError("uncommitted final artifact bindings are invalid")
    bindings = _required_mapping(manifest.get("artifacts"), "uncommitted final artifact bindings")
    removable = (
        "ball_candidates.jsonl",
        "candidate_classifications.jsonl",
        TRACK_NAME,
        "camera_target.csv",
        "broadcast.mp4",
    )
    for name in removable:
        binding = _required_mapping(bindings.get(name), f"uncommitted final binding {name}")
        path = run_dir / name
        if _is_link_or_reparse(path) or path.resolve().parent != run_dir or not path.is_file():
            raise BroadcastHybridOrchestrationError(f"uncommitted public artifact is unavailable: {name}")
        if _sha256_file(path) != _required_sha256(binding.get("sha256"), f"uncommitted {name} sha256"):
            raise BroadcastHybridOrchestrationError(f"uncommitted public artifact changed: {name}")
    manifest_path.unlink()
    for name in reversed(removable):
        (run_dir / name).unlink()


def _resolve_queue_bindings(queue_path: Path, queue: dict[str, Any]) -> dict[str, Path]:
    raw_bindings = _required_mapping(queue.get("bindings"), "queue.bindings")
    required = {
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
    }
    if set(raw_bindings) != required:
        raise BroadcastHybridOrchestrationError("selective review queue binding keys are incomplete or unexpected")
    resolved: dict[str, Path] = {}
    for name in sorted(required):
        binding = _required_mapping(raw_bindings[name], f"queue.bindings.{name}")
        raw_path = Path(_required_text(binding.get("path"), f"queue.bindings.{name}.path"))
        path = raw_path if raw_path.is_absolute() else queue_path.parent / raw_path
        path = path.resolve()
        if not path.is_file():
            raise BroadcastHybridOrchestrationError(f"queue-bound artifact is unavailable: {name}")
        expected = _required_sha256(binding.get("sha256"), f"queue.bindings.{name}.sha256")
        if _sha256_file(path) != expected:
            raise BroadcastHybridOrchestrationError(f"queue-bound artifact changed: {name}")
        resolved[name] = path
    if resolved["model"].name != MODEL_MANIFEST_NAME:
        raise BroadcastHybridOrchestrationError(f"queue-bound model must be named {MODEL_MANIFEST_NAME}")
    if (
        resolved["model_weights"].name != MODEL_WEIGHTS_NAME
        or resolved["model_weights"].parent != resolved["model"].parent
    ):
        raise BroadcastHybridOrchestrationError("queue-bound model weights are not in the bound model package")
    if (
        resolved["training_report"].name != TRAINING_REPORT_NAME
        or resolved["training_report"].parent != resolved["model"].parent
    ):
        raise BroadcastHybridOrchestrationError("queue-bound training report is not in the bound model package")
    return resolved


def _source_video_from_dataset(dataset_path: Path) -> tuple[Path, str]:
    dataset, _ = _load_json_snapshot(dataset_path, "candidate dataset")
    sources = dataset.get("sources")
    if not isinstance(sources, list) or len(sources) != 1 or not isinstance(sources[0], dict):
        raise BroadcastHybridOrchestrationError("broadcast orchestration requires exactly one dataset source video")
    source = sources[0]
    raw_path = Path(_required_text(source.get("path"), "dataset source path"))
    path = raw_path if raw_path.is_absolute() else dataset_path.parent / raw_path
    path = path.resolve()
    if not path.is_file():
        raise BroadcastHybridOrchestrationError("queue-bound dataset source video is unavailable")
    before = _stat_token(path)
    source_sha256 = _sha256_file(path)
    if _stat_token(path) != before:
        raise BroadcastHybridOrchestrationError("queue-bound dataset source video changed while hashing")
    if source_sha256 != _required_sha256(source.get("sha256"), "dataset source sha256"):
        raise BroadcastHybridOrchestrationError("queue-bound dataset source video changed")
    return path, source_sha256


def _validate_run_lineage(
    *,
    run_dir: Path,
    queue_contract_path: Path,
    dataset_source_video: Path,
    dataset_source_sha256: str,
) -> tuple[str, Path]:
    root_contract_path = run_dir / TRACKING_CONTRACT_REPORT_NAME
    root_contract, root_contract_sha256 = _load_json_snapshot(root_contract_path, "run tracking contract")
    if _sha256_file(queue_contract_path) != root_contract_sha256:
        raise BroadcastHybridOrchestrationError(
            "queue-bound tracking contract does not match the run root tracking contract"
        )
    source = _required_mapping(root_contract.get("source"), "run tracking contract source")
    source_video_sha256 = _required_sha256(source.get("video_sha256"), "run tracking contract source video sha256")
    if dataset_source_sha256 != source_video_sha256:
        raise BroadcastHybridOrchestrationError(
            "queue-bound dataset source video does not match the run root tracking contract"
        )
    action_binding_path = _validate_action_signal_binding(
        run_dir,
        source_video=dataset_source_video,
        source_video_sha256=dataset_source_sha256,
        source_contract_sha256=root_contract_sha256,
    )
    return root_contract_sha256, action_binding_path


def _validate_action_signal_binding(
    run_dir: Path,
    *,
    source_video: Path,
    source_video_sha256: str | None = None,
    source_contract_sha256: str,
) -> Path:
    binding_path = run_dir / ACTION_SIGNAL_BINDING_NAME
    binding, _ = _load_json_snapshot(binding_path, "broadcast action signal binding")
    if binding.get("artifact_type") != "broadcast_action_signal_binding":
        raise BroadcastHybridOrchestrationError("broadcast action signal binding artifact_type is invalid")
    source = _required_mapping(binding.get("source"), "broadcast action signal source")
    if source_video_sha256 is None:
        source_video_sha256 = _sha256_file(source_video)
    if source.get("video_sha256") != source_video_sha256:
        raise BroadcastHybridOrchestrationError("broadcast action signal is bound to a different source video")
    if source.get("tracking_contract_sha256") != source_contract_sha256:
        raise BroadcastHybridOrchestrationError("broadcast action signal is bound to a different tracking contract")

    action_track = run_dir / ACTION_TRACK_NAME
    action_report_path = run_dir / ACTION_SIGNAL_REPORT_NAME
    artifacts = _required_mapping(binding.get("artifacts"), "broadcast action signal artifacts")
    for name, path in ((ACTION_TRACK_NAME, action_track), (ACTION_SIGNAL_REPORT_NAME, action_report_path)):
        artifact = _required_mapping(artifacts.get(name), f"broadcast action signal artifact {name}")
        expected_hash = _required_sha256(artifact.get("sha256"), f"broadcast action signal artifact {name} sha256")
        expected_size = artifact.get("size_bytes")
        if not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size < 0:
            raise BroadcastHybridOrchestrationError(f"broadcast action signal artifact {name} size is invalid")
        if not path.is_file() or path.stat().st_size != expected_size or _sha256_file(path) != expected_hash:
            raise BroadcastHybridOrchestrationError(f"broadcast action signal artifact changed: {name}")

    action_report, _ = _load_json_snapshot(action_report_path, "action signal report")
    if (
        action_report.get("artifact_type") != "action_signal_report"
        or action_report.get("status") not in ACTION_SIGNAL_SUCCESS_STATUSES
    ):
        raise BroadcastHybridOrchestrationError("action signal report is not a successful generation")
    report_source = Path(_required_text(action_report.get("input_video"), "action signal input video")).resolve()
    if report_source != source_video.resolve():
        raise BroadcastHybridOrchestrationError("action signal report is bound to a different source video path")
    report_artifacts = _required_mapping(action_report.get("artifacts"), "action signal report artifacts")
    if report_artifacts.get("track") != ACTION_TRACK_NAME:
        raise BroadcastHybridOrchestrationError("action signal report does not bind the canonical action track")
    return binding_path


def _validate_materialization(
    run_dir: Path,
    review_dir: Path,
    queue_path: Path,
    actions_path: Path,
    *,
    trust_new_generation: bool = False,
    verify_unknown: bool = True,
) -> dict[str, Any]:
    queue, queue_sha256 = _load_json_snapshot(queue_path, "selective review queue")
    actions, actions_sha256 = _load_json_snapshot(actions_path, "review decisions")
    if queue.get("artifact_type") != "selective_review_queue":
        raise BroadcastHybridOrchestrationError("selective review queue artifact_type is invalid")
    if actions.get("artifact_type") != "selective_review_actions":
        raise BroadcastHybridOrchestrationError("review decisions artifact_type is invalid")
    bound = _resolve_queue_bindings(queue_path, queue)
    report, signature = _validate_materialization_structure(
        review_dir,
        queue,
        queue_path,
        queue_sha256,
        actions_path,
        actions_sha256,
    )
    cache_key = review_dir.resolve()
    with _GENERATION_VALIDATION_CACHE_LOCK:
        cached_signature = _VALIDATED_MATERIALIZATION_GENERATIONS.get(cache_key)
    if not trust_new_generation and cached_signature != signature:
        if not verify_unknown:
            return report
        verification_parent = Path(
            tempfile.mkdtemp(prefix=f".{review_dir.name}.verify-", dir=_generation_root(run_dir, create=False))
        )
        verification_dir = verification_parent / "output"
        try:
            try:
                materialize_selective_review_actions(
                    queue_path,
                    actions_path,
                    bound["dataset"],
                    bound["predictions"],
                    bound["policy"],
                    bound["model"],
                    bound["contract"],
                    verification_dir,
                    decisions_path=bound["decisions"],
                    annotation_resolution_path=bound["annotation_resolution"],
                    resolved_contract_path=bound["resolved_tracking_contract"],
                    policy_roles_path=bound["policy_roles"],
                )
            except (OSError, SelectiveReviewError, ValueError) as exc:
                raise BroadcastHybridOrchestrationError(
                    f"could not verify cached review materialization: {exc}"
                ) from exc
            _validate_materialization_structure(
                verification_dir,
                queue,
                queue_path,
                queue_sha256,
                actions_path,
                actions_sha256,
            )
            if _materialization_semantic_snapshot(review_dir) != _materialization_semantic_snapshot(verification_dir):
                raise BroadcastHybridOrchestrationError(
                    "cached review materialization does not match the bound materializer output"
                )
        finally:
            shutil.rmtree(verification_parent, ignore_errors=True)
    with _GENERATION_VALIDATION_CACHE_LOCK:
        _VALIDATED_MATERIALIZATION_GENERATIONS[cache_key] = signature
    return report


def _validate_materialization_structure(
    review_dir: Path,
    queue: dict[str, Any],
    queue_path: Path,
    queue_sha256: str,
    actions_path: Path,
    actions_sha256: str,
) -> tuple[dict[str, Any], tuple[tuple[str, str], ...]]:
    artifact_paths = {
        "human_votes": HUMAN_VOTES_NAME,
        "trajectory_corrections": TRAJECTORY_CORRECTIONS_NAME,
        "annotation_resolution": f"annotations/{ANNOTATION_RESOLUTION_NAME}",
        "annotation_adjudication_queue": f"annotations/{ADJUDICATION_QUEUE_NAME}",
        "derived_annotations_contract": f"annotations/{TRACKING_CONTRACT_REPORT_NAME}",
    }
    expected_files = {
        MATERIALIZATION_REPORT_NAME,
        ACTIVE_ROUND_NAME,
        *artifact_paths.values(),
    }
    signature = _strict_generation_signature(review_dir, expected_files, "review materialization")
    report, _ = _load_json_snapshot(review_dir / MATERIALIZATION_REPORT_NAME, "review materialization report")
    round_report, _ = _load_json_snapshot(review_dir / ACTIVE_ROUND_NAME, "active learning round")
    if (
        report.get("schema_version") != "1.0"
        or report.get("artifact_type") != "selective_review_materialization"
        or report.get("status") != "complete"
        or report.get("training_invoked") is not False
    ):
        raise BroadcastHybridOrchestrationError("review materialization is not complete")
    if (
        round_report.get("schema_version") != "1.0"
        or round_report.get("artifact_type") != "active_learning_round"
        or round_report.get("status") != "materialized"
        or round_report.get("training_invoked") is not False
    ):
        raise BroadcastHybridOrchestrationError("active learning round is invalid")
    expected_bindings = {
        **_required_mapping(queue.get("bindings"), "queue.bindings"),
        "queue": {"path": str(queue_path.resolve()), "sha256": queue_sha256},
        "actions": {"path": str(actions_path.resolve()), "sha256": actions_sha256},
    }
    if report.get("bindings") != expected_bindings or round_report.get("bindings") != expected_bindings:
        raise BroadcastHybridOrchestrationError("review materialization input bindings are incomplete or stale")
    summary = _required_mapping(report.get("summary"), "review materialization summary")
    if summary != round_report.get("summary"):
        raise BroadcastHybridOrchestrationError("review materialization summary disagrees with its active round")
    if summary.get("trajectory_correction_count") != 0:
        raise BroadcastHybridOrchestrationError("review materialization contains unsupported trajectory corrections")
    if report.get("round_id") != round_report.get("round_id"):
        raise BroadcastHybridOrchestrationError("review materialization active round identity is stale")
    _validate_materialization_artifact_bindings(
        review_dir,
        round_report.get("artifacts"),
        artifact_paths,
        "active learning round",
    )
    _validate_materialization_artifact_bindings(
        review_dir,
        report.get("artifacts"),
        {**artifact_paths, "active_learning_round": ACTIVE_ROUND_NAME},
        "review materialization",
    )
    corrections, _ = _load_json_snapshot(review_dir / TRAJECTORY_CORRECTIONS_NAME, "trajectory corrections")
    if (
        corrections.get("schema_version") != "1.0"
        or corrections.get("artifact_type") != "trajectory_corrections"
        or corrections.get("queue_sha256") != queue_sha256
        or corrections.get("correction_count") != 0
        or corrections.get("corrections") != []
    ):
        raise BroadcastHybridOrchestrationError("trajectory corrections cannot enter the current global solver")
    resolution, _ = _load_json_snapshot(
        review_dir / "annotations" / ANNOTATION_RESOLUTION_NAME,
        "candidate annotation resolution",
    )
    if resolution.get("artifact_type") != "candidate_annotation_resolution":
        raise BroadcastHybridOrchestrationError("candidate annotation resolution is invalid")
    adjudication, _ = _load_json_snapshot(
        review_dir / "annotations" / ADJUDICATION_QUEUE_NAME,
        "candidate annotation adjudication queue",
    )
    if adjudication.get("artifact_type") != "candidate_annotation_adjudication_queue":
        raise BroadcastHybridOrchestrationError("candidate annotation adjudication queue is invalid")
    contract, _ = _load_json_snapshot(
        review_dir / "annotations" / TRACKING_CONTRACT_REPORT_NAME,
        "materialized tracking contract",
    )
    if contract.get("schema_version") != "2.0" or contract.get("validation_errors") != []:
        raise BroadcastHybridOrchestrationError("materialized tracking contract is invalid")
    _load_jsonl_snapshot(review_dir / HUMAN_VOTES_NAME, "human adjudication votes")
    return report, signature


def _validate_materialization_artifact_bindings(
    review_dir: Path,
    raw_artifacts: Any,
    expected_paths: dict[str, str],
    label: str,
) -> None:
    artifacts = _required_mapping(raw_artifacts, f"{label} artifacts")
    if set(artifacts) != set(expected_paths):
        raise BroadcastHybridOrchestrationError(f"{label} artifact bindings are incomplete or unexpected")
    for name, relative in expected_paths.items():
        binding = _required_mapping(artifacts.get(name), f"{label} artifact {name}")
        if set(binding) != {"path", "sha256"} or binding.get("path") != relative:
            raise BroadcastHybridOrchestrationError(f"{label} artifact path is invalid: {name}")
        path = review_dir / Path(relative)
        if _sha256_file(path) != _required_sha256(binding.get("sha256"), f"{label} artifact {name} sha256"):
            raise BroadcastHybridOrchestrationError(f"{label} artifact changed: {name}")


def _materialization_semantic_snapshot(directory: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        HUMAN_VOTES_NAME: _without_generated_at(
            _load_jsonl_snapshot(directory / HUMAN_VOTES_NAME, "human adjudication votes")
        )
    }
    json_paths = (
        TRAJECTORY_CORRECTIONS_NAME,
        f"annotations/{ANNOTATION_RESOLUTION_NAME}",
        f"annotations/{ADJUDICATION_QUEUE_NAME}",
        f"annotations/{TRACKING_CONTRACT_REPORT_NAME}",
        ACTIVE_ROUND_NAME,
        MATERIALIZATION_REPORT_NAME,
    )
    for relative in json_paths:
        payload, _ = _load_json_snapshot(directory / Path(relative), relative)
        normalized = _without_generated_at(payload)
        if relative == f"annotations/{ANNOTATION_RESOLUTION_NAME}":
            source_ledger = _required_mapping(normalized.get("source_vote_ledger"), "source vote ledger")
            source_ledger["path"] = HUMAN_VOTES_NAME
            derived = _required_mapping(
                normalized.get("derived_tracking_contract"), "derived tracking contract binding"
            )
            derived["sha256"] = "<verified-derived-contract>"
        if relative in {ACTIVE_ROUND_NAME, MATERIALIZATION_REPORT_NAME}:
            artifacts = _required_mapping(normalized.get("artifacts"), f"{relative} artifacts")
            for binding in artifacts.values():
                _required_mapping(binding, f"{relative} artifact")["sha256"] = "<verified-artifact>"
        result[relative] = normalized
    return result


def _validate_classifier_core(
    run_dir: Path,
    core_dir: Path,
    expected_inputs: dict[str, Any],
    reviewed_contract: Path,
    *,
    trust_new_generation: bool = False,
    verify_unknown: bool = True,
) -> None:
    queue_path = run_dir / REVIEW_QUEUE_NAME
    queue, _ = _load_json_snapshot(queue_path, "selective review queue")
    bound = _resolve_queue_bindings(queue_path, queue)
    expected_hashes = {
        "dataset": "dataset_sha256",
        "model": "model_manifest_sha256",
        "model_weights": "model_weights_sha256",
        "training_report": "training_report_sha256",
    }
    for name, input_name in expected_hashes.items():
        if _sha256_file(bound[name]) != expected_inputs.get(input_name):
            raise BroadcastHybridOrchestrationError(f"classifier core input binding is stale: {name}")
    if _sha256_file(reviewed_contract) != expected_inputs.get("reviewed_contract_sha256"):
        raise BroadcastHybridOrchestrationError("classifier core reviewed contract binding is stale")
    signature = _validate_classifier_core_structure(core_dir, expected_inputs, bound["model"], reviewed_contract)
    cache_key = core_dir.resolve()
    with _GENERATION_VALIDATION_CACHE_LOCK:
        cached_signature = _VALIDATED_CLASSIFIER_GENERATIONS.get(cache_key)
    if not trust_new_generation and cached_signature != signature:
        if not verify_unknown:
            return
        verification_parent = Path(
            tempfile.mkdtemp(prefix=f".{core_dir.name}.verify-", dir=_generation_root(run_dir, create=False))
        )
        verification_dir = verification_parent / "output"
        try:
            try:
                classify_candidates(
                    bound["model"].parent,
                    bound["dataset"],
                    reviewed_contract,
                    verification_dir,
                    batch_size=_positive_int(expected_inputs.get("batch_size"), "classifier batch_size"),
                )
            except (ClassifierError, OSError, ValueError) as exc:
                raise BroadcastHybridOrchestrationError(f"could not verify cached classifier core: {exc}") from exc
            _validate_classifier_core_structure(
                verification_dir,
                expected_inputs,
                bound["model"],
                reviewed_contract,
            )
            cached_predictions = (core_dir / PREDICTIONS_NAME).read_bytes()
            verified_predictions = (verification_dir / PREDICTIONS_NAME).read_bytes()
            cached_contract, _ = _load_json_snapshot(
                core_dir / TRACKING_CONTRACT_REPORT_NAME,
                "classifier-derived tracking contract",
            )
            verified_contract, _ = _load_json_snapshot(
                verification_dir / TRACKING_CONTRACT_REPORT_NAME,
                "verified classifier-derived tracking contract",
            )
            if cached_predictions != verified_predictions or _without_generated_at(
                cached_contract
            ) != _without_generated_at(verified_contract):
                raise BroadcastHybridOrchestrationError(
                    "cached classifier core does not match the bound classifier output"
                )
        finally:
            shutil.rmtree(verification_parent, ignore_errors=True)
    with _GENERATION_VALIDATION_CACHE_LOCK:
        _VALIDATED_CLASSIFIER_GENERATIONS[cache_key] = signature


def _validate_classifier_core_structure(
    core_dir: Path,
    expected_inputs: dict[str, Any],
    model_manifest_path: Path,
    reviewed_contract_path: Path,
) -> tuple[tuple[str, str], ...]:
    signature = _strict_generation_signature(
        core_dir,
        {PREDICTIONS_NAME, TRACKING_CONTRACT_REPORT_NAME},
        "classifier core generation",
    )
    predictions, _ = _load_json_snapshot(core_dir / PREDICTIONS_NAME, "candidate predictions")
    model_manifest, _ = _load_json_snapshot(model_manifest_path, "candidate classifier model manifest")
    reviewed_contract, _ = _load_json_snapshot(reviewed_contract_path, "materialized tracking contract")
    derived_contract, _ = _load_json_snapshot(
        core_dir / TRACKING_CONTRACT_REPORT_NAME,
        "classifier-derived tracking contract",
    )
    raw_predictions = predictions.get("predictions")
    if not isinstance(raw_predictions, list):
        raise BroadcastHybridOrchestrationError("classifier predictions rows are invalid")
    if (
        predictions.get("schema_version") != "1.0"
        or predictions.get("artifact_type") != "candidate_predictions"
        or predictions.get("source_contract_sha256") != expected_inputs["reviewed_contract_sha256"]
        or predictions.get("model_version") != model_manifest.get("model_version")
        or predictions.get("prediction_count") != len(raw_predictions)
    ):
        raise BroadcastHybridOrchestrationError("classifier predictions are stale for the deterministic inputs")
    reviewed_candidates = reviewed_contract.get("candidates")
    derived_candidates = derived_contract.get("candidates")
    if not isinstance(reviewed_candidates, list) or not isinstance(derived_candidates, list):
        raise BroadcastHybridOrchestrationError("classifier-derived tracking contract candidates are invalid")
    reviewed_ids = _unique_candidate_ids(reviewed_candidates, "materialized tracking contract")
    derived_ids = _unique_candidate_ids(derived_candidates, "classifier-derived tracking contract")
    prediction_ids = _unique_candidate_ids(raw_predictions, "candidate predictions")
    if prediction_ids != reviewed_ids or derived_ids != reviewed_ids:
        raise BroadcastHybridOrchestrationError("classifier output candidate identities do not match its contract")
    for index, prediction in enumerate(raw_predictions):
        if prediction.get("model_version") != predictions.get("model_version"):
            raise BroadcastHybridOrchestrationError(f"classifier prediction model_version is stale at index {index}")
        if not isinstance(prediction.get("candidate_fingerprint"), str):
            raise BroadcastHybridOrchestrationError(
                f"classifier prediction candidate_fingerprint is invalid at index {index}"
            )
        probabilities = prediction.get("probabilities")
        if not isinstance(probabilities, dict) or prediction.get("predicted_label") not in probabilities:
            raise BroadcastHybridOrchestrationError(f"classifier prediction probabilities are invalid at index {index}")
    if derived_contract.get("schema_version") != "2.0" or derived_contract.get("validation_errors") != []:
        raise BroadcastHybridOrchestrationError("classifier-derived tracking contract is invalid")
    return signature


def _unique_candidate_ids(rows: list[Any], label: str) -> set[str]:
    result: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise BroadcastHybridOrchestrationError(f"{label} contains a non-object row at index {index}")
        candidate_id = _required_text(row.get("candidate_id"), f"{label} candidate_id")
        if candidate_id in result:
            raise BroadcastHybridOrchestrationError(f"{label} contains duplicate candidate_id: {candidate_id}")
        result.add(candidate_id)
    return result


def _strict_generation_signature(
    directory: Path,
    expected_relative_files: set[str],
    label: str,
) -> tuple[tuple[str, str], ...]:
    directory = Path(directory)
    if _is_link_or_reparse(directory) or not directory.is_dir():
        raise BroadcastHybridOrchestrationError(f"{label} directory is unavailable or unsafe")
    actual_files: set[str] = set()
    for path in directory.rglob("*"):
        if _is_link_or_reparse(path):
            raise BroadcastHybridOrchestrationError(f"{label} contains a symlink or reparse point")
        relative = path.relative_to(directory).as_posix()
        if path.is_file():
            actual_files.add(relative)
        elif not path.is_dir():
            raise BroadcastHybridOrchestrationError(f"{label} contains an unsupported filesystem entry")
    if actual_files != expected_relative_files:
        raise BroadcastHybridOrchestrationError(f"{label} files are incomplete or unexpected")
    return tuple((relative, _sha256_file(directory / Path(relative))) for relative in sorted(actual_files))


def _load_jsonl_snapshot(path: Path, label: str) -> list[dict[str, Any]]:
    if _is_link_or_reparse(path):
        raise BroadcastHybridOrchestrationError(f"{label} must not be a symlink or reparse point")
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            raise BroadcastHybridOrchestrationError(f"{label} exceeds the JSON size bound")
        payload_bytes = path.read_bytes()
    except OSError as exc:
        raise BroadcastHybridOrchestrationError(f"{label} is unavailable") from exc
    digest = hashlib.sha256(payload_bytes).hexdigest()
    rows: list[dict[str, Any]] = []
    try:
        for index, line in enumerate(payload_bytes.decode("utf-8-sig").splitlines()):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise BroadcastHybridOrchestrationError(f"{label} row {index} must be an object")
            rows.append(row)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BroadcastHybridOrchestrationError(f"{label} is invalid JSONL") from exc
    if not rows:
        raise BroadcastHybridOrchestrationError(f"{label} must contain at least one row")
    _verify_unchanged(path, digest, label)
    return rows


def _without_generated_at(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_generated_at(item) for key, item in value.items() if key != "generated_at"}
    if isinstance(value, list):
        return [_without_generated_at(item) for item in value]
    return value


def _validate_inference(
    run_dir: Path,
    inference_dir: Path,
    expected_inputs: dict[str, Any],
    reviewed_contract: Path,
    *,
    verify_unknown: bool,
) -> tuple[dict[str, Any], Path, Path]:
    report, _ = _load_json_snapshot(inference_dir / INFERENCE_REPORT_NAME, "broadcast classifier inference report")
    if report.get("artifact_type") != "broadcast_classifier_inference" or report.get("inputs") != expected_inputs:
        raise BroadcastHybridOrchestrationError(
            "classifier inference generation does not match its deterministic inputs"
        )
    digest = _identity_sha256(expected_inputs)[:24]
    expected_generation_id = "classification-" + digest
    expected_core_id = "classifier-core-" + digest
    if report.get("generation_id") != expected_generation_id or inference_dir.name != expected_generation_id:
        raise BroadcastHybridOrchestrationError("classifier inference manifest is bound to a different generation")
    core_id = _generation_id(report.get("core_generation_id"), prefix="classifier-core-")
    if core_id != expected_core_id:
        raise BroadcastHybridOrchestrationError("classifier inference core generation identity is stale")
    core_dir = _contained_generation(run_dir, core_id)
    _validate_classifier_core(
        run_dir,
        core_dir,
        expected_inputs,
        reviewed_contract,
        verify_unknown=verify_unknown,
    )
    artifacts = _required_mapping(report.get("artifacts"), "classifier inference artifacts")
    expected_paths = {
        PREDICTIONS_NAME: core_dir / PREDICTIONS_NAME,
        TRACKING_CONTRACT_REPORT_NAME: core_dir / TRACKING_CONTRACT_REPORT_NAME,
    }
    if set(artifacts) != set(expected_paths):
        raise BroadcastHybridOrchestrationError("classifier inference artifact bindings are incomplete or unexpected")
    for name, path in expected_paths.items():
        _verify_file_binding(run_dir, artifacts.get(name), name, expected_path=path)
    return report, expected_paths[PREDICTIONS_NAME], core_dir


def _validate_trajectory_generation(
    trajectory_dir: Path,
    expected_inputs: dict[str, Any] | None,
    run_dir: Path,
    *,
    verify_unknown: bool = False,
) -> tuple[dict[str, Any], Path, Path]:
    report, _ = _load_json_snapshot(trajectory_dir / ORCHESTRATION_REPORT_NAME, "trajectory orchestration report")
    if report.get("artifact_type") != "broadcast_trajectory_orchestration":
        raise BroadcastHybridOrchestrationError("trajectory orchestration report is invalid")
    inputs = _required_mapping(report.get("inputs"), "trajectory orchestration inputs")
    if expected_inputs is not None and inputs != expected_inputs:
        raise BroadcastHybridOrchestrationError("trajectory generation does not match its deterministic inputs")
    generation_ids = _required_mapping(report.get("generation_ids"), "trajectory orchestration generation ids")
    if set(generation_ids) != {"review", "classification", "trajectory"}:
        raise BroadcastHybridOrchestrationError("trajectory orchestration generation ids are incomplete or unexpected")
    review_id = _generation_id(generation_ids.get("review"), prefix="review-")
    inference_id = _generation_id(generation_ids.get("classification"), prefix="classification-")
    trajectory_id = _generation_id(generation_ids.get("trajectory"), prefix="trajectory-")
    if trajectory_id != trajectory_dir.name:
        raise BroadcastHybridOrchestrationError("trajectory orchestration is bound to a different generation")
    digest = _identity_sha256(inputs)[:24]
    if trajectory_id != "trajectory-" + digest:
        raise BroadcastHybridOrchestrationError("trajectory orchestration deterministic identity is stale")
    core_generation_ids = _required_mapping(
        report.get("core_generation_ids"), "trajectory orchestration core generation ids"
    )
    if set(core_generation_ids) != {"classification", "trajectory"}:
        raise BroadcastHybridOrchestrationError("trajectory core generation ids are incomplete or unexpected")
    inference_core_id = _generation_id(core_generation_ids.get("classification"), prefix="classifier-core-")
    trajectory_core_id = _generation_id(core_generation_ids.get("trajectory"), prefix="trajectory-core-")
    if trajectory_core_id != "trajectory-core-" + digest:
        raise BroadcastHybridOrchestrationError("trajectory core generation identity is stale")
    review_dir = _contained_generation(run_dir, review_id)
    queue_path = run_dir / REVIEW_QUEUE_NAME
    actions_path = run_dir / "review_decisions.json"
    _validate_materialization(
        run_dir,
        review_dir,
        queue_path,
        actions_path,
        verify_unknown=verify_unknown,
    )
    reviewed_contract = review_dir / "annotations" / TRACKING_CONTRACT_REPORT_NAME
    inference_dir = _contained_generation(run_dir, inference_id)
    inference_report_payload, _ = _load_json_snapshot(
        inference_dir / INFERENCE_REPORT_NAME, "broadcast classifier inference report"
    )
    inference_inputs = _required_mapping(inference_report_payload.get("inputs"), "classifier inference inputs")
    inference_manifest, predictions_path, inference_core_dir = _validate_inference(
        run_dir,
        inference_dir,
        inference_inputs,
        reviewed_contract,
        verify_unknown=verify_unknown,
    )
    if inference_core_dir.name != inference_core_id:
        raise BroadcastHybridOrchestrationError("trajectory orchestration classifier core binding is stale")
    if _sha256_file(predictions_path) != inputs.get("predictions_sha256"):
        raise BroadcastHybridOrchestrationError("trajectory orchestration predictions binding is stale")
    if inference_manifest.get("generation_id") != inference_id:
        raise BroadcastHybridOrchestrationError("trajectory orchestration classifier manifest binding is stale")
    trajectory_core_dir = _contained_generation(run_dir, trajectory_core_id)
    bindings = _required_mapping(report.get("bindings"), "trajectory orchestration bindings")
    expected_paths = {
        "queue": run_dir / REVIEW_QUEUE_NAME,
        "review_decisions": run_dir / "review_decisions.json",
        "root_tracking_contract": run_dir / TRACKING_CONTRACT_REPORT_NAME,
        "action_signal_binding": run_dir / ACTION_SIGNAL_BINDING_NAME,
        "materialization_report": review_dir / MATERIALIZATION_REPORT_NAME,
        "inference_report": inference_dir / INFERENCE_REPORT_NAME,
        "trajectory_report": trajectory_core_dir / TRAJECTORY_REPORT_NAME,
    }
    if set(bindings) != set(expected_paths):
        raise BroadcastHybridOrchestrationError("trajectory orchestration bindings are incomplete or unexpected")
    for name, path in expected_paths.items():
        _verify_file_binding(run_dir, bindings.get(name), name, expected_path=path)
    if _sha256_file(run_dir / TRACKING_CONTRACT_REPORT_NAME) != inputs.get("source_contract_sha256"):
        raise BroadcastHybridOrchestrationError("trajectory orchestration run tracking contract binding is stale")
    if _sha256_file(run_dir / ACTION_SIGNAL_BINDING_NAME) != inputs.get("action_signal_binding_sha256"):
        raise BroadcastHybridOrchestrationError("trajectory orchestration action signal binding is stale")
    queue, _ = _load_json_snapshot(run_dir / REVIEW_QUEUE_NAME, "selective review queue")
    queue_bindings = _required_mapping(queue.get("bindings"), "queue bindings")
    queue_contract = _required_mapping(queue_bindings.get("contract"), "queue contract binding")
    if queue_contract.get("sha256") != inputs.get("source_contract_sha256"):
        raise BroadcastHybridOrchestrationError("trajectory orchestration queue contract lineage is stale")
    inference_queue_hashes = {
        "dataset": "dataset_sha256",
        "model": "model_manifest_sha256",
        "model_weights": "model_weights_sha256",
        "training_report": "training_report_sha256",
    }
    for queue_name, inference_name in inference_queue_hashes.items():
        queue_binding = _required_mapping(queue_bindings.get(queue_name), f"queue {queue_name} binding")
        if queue_binding.get("sha256") != inference_inputs.get(inference_name):
            raise BroadcastHybridOrchestrationError(f"classifier inference queue binding is stale: {queue_name}")
    if _sha256_file(reviewed_contract) != inference_inputs.get("reviewed_contract_sha256"):
        raise BroadcastHybridOrchestrationError("classifier inference materialized contract binding is stale")
    _validate_trajectory_core(
        run_dir,
        trajectory_core_dir,
        inputs,
        None,
        reviewed_contract,
        predictions_path,
        verify_unknown=verify_unknown,
    )
    return report, trajectory_core_dir, inference_core_dir


def _validate_trajectory_core(
    run_dir: Path,
    core_dir: Path,
    inputs: dict[str, Any],
    source_video: Path | None,
    reviewed_contract: Path,
    predictions_path: Path,
    *,
    trust_new_generation: bool = False,
    verify_unknown: bool,
) -> dict[str, Any]:
    expected_files = {TRAJECTORY_REPORT_NAME, TRACK_NAME, TRAJECTORY_DECISIONS_NAME}
    signature = _strict_generation_signature(core_dir, expected_files, "global trajectory core generation")
    report, _ = _load_json_snapshot(core_dir / TRAJECTORY_REPORT_NAME, "global trajectory report")
    _validate_global_trajectory_report(report, core_dir / TRACK_NAME, inputs)
    cache_key = core_dir.resolve()
    with _GENERATION_VALIDATION_CACHE_LOCK:
        cached_signature = _VALIDATED_TRAJECTORY_GENERATIONS.get(cache_key)
    if not trust_new_generation and cached_signature != signature:
        if not verify_unknown:
            return report
        verification_parent = Path(
            tempfile.mkdtemp(prefix=f".{core_dir.name}.verify-", dir=_generation_root(run_dir, create=False))
        )
        verification_dir = verification_parent / "output"
        try:
            if source_video is None:
                queue, _ = _load_json_snapshot(run_dir / REVIEW_QUEUE_NAME, "selective review queue")
                bound = _resolve_queue_bindings(run_dir / REVIEW_QUEUE_NAME, queue)
                source_video, source_video_sha256 = _source_video_from_dataset(bound["dataset"])
                if source_video_sha256 != inputs.get("source_video_sha256"):
                    raise BroadcastHybridOrchestrationError("trajectory source video binding is stale")
            try:
                solve_global_ball_trajectory(
                    source_video,
                    reviewed_contract,
                    predictions_path,
                    verification_dir,
                )
            except (GlobalBallTrajectoryError, OSError, ValueError) as exc:
                raise BroadcastHybridOrchestrationError(f"could not verify cached trajectory core: {exc}") from exc
            verified_signature = _strict_generation_signature(
                verification_dir,
                expected_files,
                "verified global trajectory core generation",
            )
            verified_report, _ = _load_json_snapshot(
                verification_dir / TRAJECTORY_REPORT_NAME,
                "verified global trajectory report",
            )
            _validate_global_trajectory_report(verified_report, verification_dir / TRACK_NAME, inputs)
            if signature != verified_signature:
                raise BroadcastHybridOrchestrationError(
                    "cached trajectory core does not match the bound trajectory solver output"
                )
        finally:
            shutil.rmtree(verification_parent, ignore_errors=True)
    with _GENERATION_VALIDATION_CACHE_LOCK:
        _VALIDATED_TRAJECTORY_GENERATIONS[cache_key] = signature
    return report


def _validate_global_trajectory_report(report: dict[str, Any], track_path: Path, inputs: dict[str, Any]) -> None:
    if (
        report.get("artifact_type") != "global_ball_trajectory_report"
        or report.get("status") != "succeeded"
        or report.get("complete") is not True
    ):
        raise BroadcastHybridOrchestrationError("global trajectory report is not a completed successful generation")
    algorithm = _required_mapping(report.get("algorithm"), "global trajectory algorithm")
    if algorithm.get("version") != inputs.get("algorithm_version"):
        raise BroadcastHybridOrchestrationError("global trajectory algorithm binding is stale")
    report_inputs = _required_mapping(report.get("inputs"), "global trajectory inputs")
    expected_hashes = {
        "source video": inputs.get("source_video_sha256"),
        "source tracking contract": inputs.get("reviewed_contract_sha256"),
        "candidate predictions": inputs.get("predictions_sha256"),
    }
    for name, expected_hash in expected_hashes.items():
        binding = _required_mapping(report_inputs.get(name), f"global trajectory input {name}")
        if binding.get("sha256") != expected_hash:
            raise BroadcastHybridOrchestrationError(f"global trajectory input binding is stale: {name}")
    _validate_report_artifact(report, TRACK_NAME, track_path)


def _validate_camera_core(
    run_dir: Path,
    camera_dir: Path,
    *,
    source_video: Path,
    track_path: Path,
    trajectory_report_path: Path,
    source_video_sha256: str,
    target_width: int,
    target_height: int,
    trust_new_generation: bool = False,
    verify_unknown: bool,
) -> dict[str, Any]:
    expected_files = {
        HYBRID_REPORT_NAME,
        CAMERA_PATH_NAME,
        CAMERA_MOTION_EVIDENCE_NAME,
        CAMERA_DECISIONS_NAME,
        CAMERA_AUDIT_NAME,
    }
    signature = _strict_generation_signature(camera_dir, expected_files, "hybrid camera core generation")
    report, _ = _load_json_snapshot(camera_dir / HYBRID_REPORT_NAME, "hybrid camera report")
    _validate_hybrid_generation(
        report,
        camera_path=camera_dir / CAMERA_PATH_NAME,
        track_path=track_path,
        trajectory_report_path=trajectory_report_path,
        source_video_sha256=source_video_sha256,
        target_width=target_width,
        target_height=target_height,
    )
    cache_key = camera_dir.resolve()
    with _GENERATION_VALIDATION_CACHE_LOCK:
        cached_signature = _VALIDATED_CAMERA_GENERATIONS.get(cache_key)
    if not trust_new_generation and cached_signature != signature:
        if not verify_unknown:
            return report
        verification_parent = Path(
            tempfile.mkdtemp(prefix=f".{camera_dir.name}.verify-", dir=_generation_root(run_dir, create=False))
        )
        verification_dir = verification_parent / "output"
        try:
            try:
                solve_hybrid_broadcast_camera(
                    source_video,
                    track_path,
                    trajectory_report_path,
                    verification_dir,
                    config=HybridCameraConfig(target_width=target_width, target_height=target_height),
                )
            except (HybridBroadcastCameraError, OSError, ValueError) as exc:
                raise BroadcastHybridOrchestrationError(f"could not verify cached camera core: {exc}") from exc
            verified_signature = _strict_generation_signature(
                verification_dir,
                expected_files,
                "verified hybrid camera core generation",
            )
            verified_report, _ = _load_json_snapshot(
                verification_dir / HYBRID_REPORT_NAME,
                "verified hybrid camera report",
            )
            _validate_hybrid_generation(
                verified_report,
                camera_path=verification_dir / CAMERA_PATH_NAME,
                track_path=track_path,
                trajectory_report_path=trajectory_report_path,
                source_video_sha256=source_video_sha256,
                target_width=target_width,
                target_height=target_height,
            )
            if signature != verified_signature:
                raise BroadcastHybridOrchestrationError(
                    "cached camera core does not match the bound camera solver output"
                )
        finally:
            shutil.rmtree(verification_parent, ignore_errors=True)
    with _GENERATION_VALIDATION_CACHE_LOCK:
        _VALIDATED_CAMERA_GENERATIONS[cache_key] = signature
    return report


def _validate_hybrid_generation(
    report: dict[str, Any],
    *,
    camera_path: Path,
    track_path: Path,
    trajectory_report_path: Path,
    source_video_sha256: str,
    target_width: int,
    target_height: int,
) -> None:
    if (
        report.get("artifact_type") != "hybrid_broadcast_camera_report"
        or report.get("status") != "succeeded"
        or report.get("complete") is not True
    ):
        raise BroadcastHybridOrchestrationError("hybrid camera report is not a completed successful generation")
    source = _required_mapping(report.get("source_video"), "hybrid camera source video")
    if source.get("sha256") != source_video_sha256:
        raise BroadcastHybridOrchestrationError("hybrid camera source video binding is stale")
    inputs = _required_mapping(report.get("inputs"), "hybrid camera inputs")
    expected = {
        "global_ball_track": _sha256_file(track_path),
        "global_trajectory_report": _sha256_file(trajectory_report_path),
    }
    for name, expected_hash in expected.items():
        binding = _required_mapping(inputs.get(name), f"hybrid camera input {name}")
        if binding.get("sha256") != expected_hash:
            raise BroadcastHybridOrchestrationError(f"hybrid camera input binding is stale: {name}")
    rendering = _required_mapping(report.get("rendering"), "hybrid camera rendering")
    if rendering.get("target_width") != target_width or rendering.get("target_height") != target_height:
        raise BroadcastHybridOrchestrationError("hybrid camera target dimensions are stale")
    _validate_report_artifact(report, CAMERA_PATH_NAME, camera_path)


def _build_render_generation(
    render_dir: Path,
    *,
    render_identity: dict[str, Any],
    source_video: _SourceVideoSnapshot,
    camera_path: Path,
    hybrid_report_path: Path,
    camera_id: str,
    target_width: int,
    target_height: int,
    should_cancel: Callable[[], bool] | None,
) -> None:
    staging = Path(tempfile.mkdtemp(prefix=f".{render_dir.name}.staging-", dir=render_dir.parent))
    try:
        output_video = staging / "broadcast.mp4"
        result = render_camera_path_video(
            source_video.path,
            camera_path,
            hybrid_report_path,
            output_video,
            target_width=target_width,
            target_height=target_height,
        )
        _raise_if_cancelled(should_cancel)
        _write_json(
            staging / RENDER_REPORT_NAME,
            {
                "schema_version": "1.0",
                "artifact_type": "broadcast_render_report",
                "generated_at": _utc_now_iso(),
                "inputs": render_identity,
                "source_video": {"path": str(source_video.path), "sha256": source_video.sha256},
                "camera_generation_id": camera_id,
                "limitations": [CANCELLATION_LIMITATION],
                "result": _jsonable(result),
                "artifacts": {"broadcast.mp4": _file_binding(output_video, staging)},
            },
        )
        os.rename(staging, render_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _validate_render_core(
    run_dir: Path,
    render_dir: Path,
    *,
    render_identity: dict[str, Any],
    source_video: _SourceVideoSnapshot,
    camera_path: Path,
    hybrid_report_path: Path,
    camera_id: str,
    target_width: int,
    target_height: int,
    trust_new_generation: bool = False,
    verify_unknown: bool,
) -> dict[str, Any]:
    expected_files = {RENDER_REPORT_NAME, "broadcast.mp4"}
    signature = _strict_generation_signature(render_dir, expected_files, "broadcast render core generation")
    report, _ = _load_json_snapshot(render_dir / RENDER_REPORT_NAME, "broadcast render report")
    _validate_render_report(
        report,
        render_dir=render_dir,
        render_identity=render_identity,
        source_video=source_video,
        camera_id=camera_id,
        target_width=target_width,
        target_height=target_height,
    )
    cache_key = render_dir.resolve()
    with _GENERATION_VALIDATION_CACHE_LOCK:
        cached_signature = _VALIDATED_RENDER_GENERATIONS.get(cache_key)
    if not trust_new_generation and cached_signature != signature:
        if not verify_unknown:
            return report
        verification_parent = Path(
            tempfile.mkdtemp(prefix=f".{render_dir.name}.verify-", dir=_generation_root(run_dir, create=False))
        )
        verification_dir = verification_parent / "output"
        try:
            try:
                _build_render_generation(
                    verification_dir,
                    render_identity=render_identity,
                    source_video=source_video,
                    camera_path=camera_path,
                    hybrid_report_path=hybrid_report_path,
                    camera_id=camera_id,
                    target_width=target_width,
                    target_height=target_height,
                    should_cancel=None,
                )
            except (CameraPathRenderError, OSError, ValueError) as exc:
                raise BroadcastHybridOrchestrationError(f"could not verify cached render core: {exc}") from exc
            _strict_generation_signature(
                verification_dir,
                expected_files,
                "verified broadcast render core generation",
            )
            verified_report, _ = _load_json_snapshot(
                verification_dir / RENDER_REPORT_NAME,
                "verified broadcast render report",
            )
            _validate_render_report(
                verified_report,
                render_dir=verification_dir,
                render_identity=render_identity,
                source_video=source_video,
                camera_id=camera_id,
                target_width=target_width,
                target_height=target_height,
            )
            if _sha256_file(render_dir / "broadcast.mp4") != _sha256_file(
                verification_dir / "broadcast.mp4"
            ) or _normalized_render_report(report) != _normalized_render_report(verified_report):
                raise BroadcastHybridOrchestrationError("cached render core does not match the bound renderer output")
        finally:
            shutil.rmtree(verification_parent, ignore_errors=True)
    with _GENERATION_VALIDATION_CACHE_LOCK:
        _VALIDATED_RENDER_GENERATIONS[cache_key] = signature
    return report


def _validate_render_report(
    report: dict[str, Any],
    *,
    render_dir: Path,
    render_identity: dict[str, Any],
    source_video: _SourceVideoSnapshot,
    camera_id: str,
    target_width: int,
    target_height: int,
) -> None:
    if (
        report.get("schema_version") != "1.0"
        or report.get("artifact_type") != "broadcast_render_report"
        or report.get("inputs") != render_identity
        or report.get("camera_generation_id") != camera_id
    ):
        raise BroadcastHybridOrchestrationError("broadcast render generation does not match its deterministic inputs")
    render_source = _required_mapping(report.get("source_video"), "broadcast render source video")
    if (
        render_source.get("sha256") != source_video.sha256
        or Path(_required_text(render_source.get("path"), "broadcast render source path")).resolve()
        != source_video.path
    ):
        raise BroadcastHybridOrchestrationError("broadcast render source video binding is stale")
    result = _required_mapping(report.get("result"), "broadcast render result")
    if result.get("target_width") != target_width or result.get("target_height") != target_height:
        raise BroadcastHybridOrchestrationError("broadcast render result dimensions are stale")
    _validate_report_artifact(report, "broadcast.mp4", render_dir / "broadcast.mp4")


def _normalized_render_report(report: dict[str, Any]) -> dict[str, Any]:
    normalized = _without_generated_at(report)
    result = _required_mapping(normalized.get("result"), "broadcast render result")
    result["output_video_path"] = "broadcast.mp4"
    return normalized


def _source_video_snapshot_from_trajectory_report(report: dict[str, Any]) -> _SourceVideoSnapshot:
    inputs = _required_mapping(report.get("inputs"), "trajectory inputs")
    source = _required_mapping(inputs.get("source video"), "trajectory source video binding")
    path = Path(_required_text(source.get("path"), "trajectory source video path")).resolve()
    if not path.is_file():
        raise BroadcastHybridOrchestrationError("trajectory-bound source video is unavailable or changed")
    try:
        before = _stat_token(path)
        source_sha256 = _sha256_file(path)
        after = _stat_token(path)
    except OSError as exc:
        raise BroadcastHybridOrchestrationError("trajectory-bound source video is unavailable or changed") from exc
    if before != after:
        raise BroadcastHybridOrchestrationError("trajectory-bound source video changed while hashing")
    if source_sha256 != _required_sha256(source.get("sha256"), "trajectory source sha256"):
        raise BroadcastHybridOrchestrationError("trajectory-bound source video is unavailable or changed")
    return _SourceVideoSnapshot(path=path, sha256=source_sha256, stat_token=after)


def _verify_source_video_snapshot_unchanged(snapshot: _SourceVideoSnapshot, label: str) -> None:
    try:
        current = _stat_token(snapshot.path)
    except OSError as exc:
        raise BroadcastHybridOrchestrationError(f"{label} changed during validation") from exc
    if current != snapshot.stat_token:
        raise BroadcastHybridOrchestrationError(f"{label} changed during validation")


def _publish_public_artifacts(
    *,
    run_dir: Path,
    orchestration: dict[str, Any],
    trajectory_dir: Path,
    inference_core_dir: Path,
    camera_dir: Path,
    render_dir: Path,
) -> dict[str, Any]:
    generation_ids = _required_mapping(orchestration.get("generation_ids"), "orchestration generation ids")
    review_dir = _contained_generation(run_dir, _generation_id(generation_ids.get("review"), prefix="review-"))
    materialized_contract = review_dir / "annotations" / TRACKING_CONTRACT_REPORT_NAME
    predictions = inference_core_dir / PREDICTIONS_NAME
    action_track = run_dir / ACTION_TRACK_NAME
    action_binding = run_dir / ACTION_SIGNAL_BINDING_NAME
    review_decisions = run_dir / "review_decisions.json"
    for path, label in (
        (action_track, "action track"),
        (action_binding, "action signal binding"),
        (review_decisions, "review decisions"),
    ):
        if not path.is_file():
            raise BroadcastHybridOrchestrationError(f"required final public evidence is unavailable: {label}")

    staging = Path(tempfile.mkdtemp(prefix=".broadcast-public-", dir=run_dir))
    created: list[Path] = []
    try:
        _stream_json_array(materialized_contract, "candidates.item", staging / "ball_candidates.jsonl")
        _stream_json_array(predictions, "predictions.item", staging / "candidate_classifications.jsonl")
        sources = {
            "ball_candidates.jsonl": staging / "ball_candidates.jsonl",
            "candidate_classifications.jsonl": staging / "candidate_classifications.jsonl",
            TRACK_NAME: trajectory_dir / TRACK_NAME,
            ACTION_TRACK_NAME: action_track,
            "review_decisions.json": review_decisions,
            "camera_target.csv": camera_dir / CAMERA_PATH_NAME,
            "broadcast.mp4": render_dir / "broadcast.mp4",
        }
        for name, source in sources.items():
            target = run_dir / name
            if _is_link_or_reparse(target):
                raise BroadcastHybridOrchestrationError(
                    f"public artifact target must not be a symlink or reparse point: {name}"
                )
            if Path(os.path.abspath(source)) == Path(os.path.abspath(target)):
                continue
            if _publish_file_exclusive(source, target, trusted_root=run_dir):
                created.append(target)

        source_reports = {
            "ball_candidates.jsonl": materialized_contract,
            "candidate_classifications.jsonl": predictions,
            TRACK_NAME: trajectory_dir / TRAJECTORY_REPORT_NAME,
            ACTION_TRACK_NAME: action_binding,
            "review_decisions.json": review_dir / MATERIALIZATION_REPORT_NAME,
            "camera_target.csv": camera_dir / HYBRID_REPORT_NAME,
            "broadcast.mp4": render_dir / RENDER_REPORT_NAME,
        }
        bindings = {
            name: {
                "sha256": _sha256_file(run_dir / name),
                "size_bytes": (run_dir / name).stat().st_size,
                "source_report": _file_binding(report_path, run_dir),
            }
            for name, report_path in source_reports.items()
        }
        manifest = {
            "schema_version": "1.0",
            "artifact_type": "broadcast_artifact_bindings",
            "generated_at": _utc_now_iso(),
            "orchestration_version": ORCHESTRATION_VERSION,
            "generation_ids": {
                **generation_ids,
                "camera": camera_dir.name,
                "render": render_dir.name,
            },
            "artifacts": bindings,
            "limitations": ["camera_solver_does_not_consume_action_track", CANCELLATION_LIMITATION],
        }
        manifest_path = run_dir / FINAL_BINDINGS_NAME
        if manifest_path.exists():
            existing, _ = _load_json_snapshot(manifest_path, "broadcast final artifact bindings")
            stable_existing = {key: value for key, value in existing.items() if key != "generated_at"}
            stable_expected = {key: value for key, value in manifest.items() if key != "generated_at"}
            if stable_existing != stable_expected:
                raise BroadcastHybridOrchestrationError("refusing to overwrite stale final artifact bindings")
            return existing
        _publish_json_exclusive(manifest_path, manifest)
        created.append(manifest_path)
        return manifest
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _validate_report_artifact(report: dict[str, Any], name: str, path: Path) -> None:
    artifacts = _required_mapping(report.get("artifacts"), f"report artifacts for {name}")
    binding = _required_mapping(artifacts.get(name), f"report artifact {name}")
    expected_hash = _required_sha256(binding.get("sha256"), f"report artifact {name} sha256")
    expected_size = binding.get("size", binding.get("size_bytes"))
    if not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size < 0:
        raise BroadcastHybridOrchestrationError(f"report artifact {name} size is invalid")
    if (
        _is_link_or_reparse(path)
        or not path.is_file()
        or path.stat().st_size != expected_size
        or _sha256_file(path) != expected_hash
    ):
        raise BroadcastHybridOrchestrationError(f"report artifact changed or is unavailable: {name}")


def _run_directory(path: Path) -> Path:
    raw = Path(path)
    resolved = raw.resolve()
    if _is_link_or_reparse(raw) or not resolved.is_dir():
        raise BroadcastHybridOrchestrationError("run_dir must be an existing non-reparse directory")
    return resolved


def _contained_generation(run_dir: Path, generation_id: str) -> Path:
    raw_root = run_dir / "broadcast_generations"
    root = _generation_root(run_dir, create=False)
    raw_path = raw_root / generation_id
    path = raw_path.resolve()
    if _is_link_or_reparse(raw_path) or path.parent != root or not path.is_dir():
        raise BroadcastHybridOrchestrationError(f"broadcast generation is unavailable: {generation_id}")
    return path


def _generation_root(run_dir: Path, *, create: bool) -> Path:
    raw_root = run_dir / "broadcast_generations"
    if not raw_root.exists():
        if not create:
            raise BroadcastHybridOrchestrationError("broadcast generation root is unavailable")
        raw_root.mkdir(parents=False, exist_ok=False)
    root = raw_root.resolve()
    if _is_link_or_reparse(raw_root) or root.parent != run_dir or not root.is_dir():
        raise BroadcastHybridOrchestrationError("broadcast generation root must be a direct non-reparse directory")
    return root


def _generation_id(value: Any, *, prefix: str) -> str:
    text = _required_text(value, "generation id")
    if not text.startswith(prefix) or _GENERATION_PATTERN.fullmatch(text) is None:
        raise BroadcastHybridOrchestrationError(f"generation id must start with {prefix!r} and contain safe characters")
    return text


def _load_json_snapshot(path: Path, label: str) -> tuple[dict[str, Any], str]:
    raw_path = Path(path)
    if _is_link_or_reparse(raw_path):
        raise BroadcastHybridOrchestrationError(f"{label} must not be a symlink or reparse point")
    path = raw_path.resolve()
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise BroadcastHybridOrchestrationError(f"{label} is unavailable") from exc
    if size > _MAX_JSON_BYTES:
        raise BroadcastHybridOrchestrationError(f"{label} exceeds the JSON size bound")
    payload_bytes = path.read_bytes()
    digest = hashlib.sha256(payload_bytes).hexdigest()
    try:
        payload = json.loads(payload_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BroadcastHybridOrchestrationError(f"{label} is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise BroadcastHybridOrchestrationError(f"{label} must be an object")
    _verify_unchanged(path, digest, label)
    return payload, digest


def _verify_unchanged(path: Path, expected_sha256: str, label: str) -> None:
    if not path.is_file() or _sha256_file(path) != expected_sha256:
        raise BroadcastHybridOrchestrationError(f"{label} changed during orchestration")


def _file_binding(path: Path, root: Path) -> dict[str, Any]:
    resolved = Path(path).resolve()
    root = Path(root).resolve()
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise BroadcastHybridOrchestrationError(f"artifact is outside its bound root: {resolved}") from exc
    return {"path": relative, "sha256": _sha256_file(resolved), "size_bytes": resolved.stat().st_size}


def _verify_file_binding(root: Path, raw: Any, label: str, *, expected_path: Path | None = None) -> None:
    binding = _required_mapping(raw, f"binding {label}")
    relative = Path(_required_text(binding.get("path"), f"binding {label} path"))
    if relative.is_absolute() or ".." in relative.parts:
        raise BroadcastHybridOrchestrationError(f"binding {label} path is not root-relative")
    path = (Path(root).resolve() / relative).resolve()
    if Path(root).resolve() not in path.parents or not path.is_file():
        raise BroadcastHybridOrchestrationError(f"binding {label} is unavailable")
    if expected_path is not None and path != Path(expected_path).resolve():
        raise BroadcastHybridOrchestrationError(f"binding {label} points to an unexpected artifact")
    if _sha256_file(path) != _required_sha256(binding.get("sha256"), f"binding {label} sha256"):
        raise BroadcastHybridOrchestrationError(f"binding {label} changed")


def _publish_manifest_generation(directory: Path, name: str, payload: dict[str, Any]) -> None:
    if directory.exists():
        return
    directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{directory.name}.manifest-", dir=directory.parent))
    try:
        _write_json(staging / name, payload)
        try:
            os.rename(staging, directory)
        except OSError:
            if not directory.is_dir():
                raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _publish_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    serialized = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode(
        "utf-8"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != serialized:
            raise BroadcastHybridOrchestrationError(f"refusing to overwrite immutable artifact: {path.name}")
        return
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            if path.read_bytes() != serialized:
                raise BroadcastHybridOrchestrationError(f"immutable artifact was concurrently replaced: {path.name}")
    finally:
        temp.unlink(missing_ok=True)


def _publish_file_exclusive(source: Path, target: Path, *, trusted_root: Path) -> bool:
    source = source.resolve()
    root = _run_directory(trusted_root)
    target = Path(os.path.abspath(target))
    if target.parent != root:
        raise BroadcastHybridOrchestrationError("public artifact target escapes the trusted run directory")
    if _is_link_or_reparse(target):
        raise BroadcastHybridOrchestrationError(
            f"public artifact target must not be a symlink or reparse point: {target.name}"
        )
    source_hash = _sha256_file(source)
    if target.exists():
        if not target.is_file() or _sha256_file(target) != source_hash:
            raise BroadcastHybridOrchestrationError(f"refusing to overwrite public artifact: {target.name}")
        return False
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{target.name}.copy-", suffix=".tmp", dir=target.parent)
    temp = Path(raw_temp)
    try:
        with source.open("rb") as input_handle, os.fdopen(descriptor, "wb") as output_handle:
            shutil.copyfileobj(input_handle, output_handle, length=_HASH_CHUNK_BYTES)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        if _sha256_file(temp) != source_hash or _sha256_file(source) != source_hash:
            raise BroadcastHybridOrchestrationError(f"source changed while copying public artifact: {target.name}")
        try:
            # The link is from a private copied inode, never from the immutable generation.
            os.link(temp, target)
            return True
        except FileExistsError:
            if _is_link_or_reparse(target) or not target.is_file() or _sha256_file(target) != source_hash:
                raise BroadcastHybridOrchestrationError(f"public artifact was concurrently replaced: {target.name}")
            return False
    finally:
        temp.unlink(missing_ok=True)


def _is_link_or_reparse(path: Path) -> bool:
    candidate = Path(path)
    try:
        if candidate.is_symlink():
            return True
        is_junction = getattr(candidate, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = getattr(candidate.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & _WINDOWS_REPARSE_ATTRIBUTE)


def _stream_json_array(source: Path, prefix: str, target: Path) -> None:
    initial_hash = _sha256_file(source)
    try:
        with source.open("rb") as input_handle, target.open("xb") as output_handle:
            for row in ijson.items(input_handle, prefix, use_float=True):
                if not isinstance(row, dict):
                    raise BroadcastHybridOrchestrationError(f"{source.name} contains a non-object {prefix} row")
                output_handle.write(
                    (
                        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
                        + "\n"
                    ).encode("utf-8")
                )
            output_handle.flush()
            os.fsync(output_handle.fileno())
    except (OSError, ValueError, ijson.JSONError) as exc:
        raise BroadcastHybridOrchestrationError(f"unable to stream {source.name}") from exc
    _verify_unchanged(source, initial_hash, source.name)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )


def _identity_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stat_token(path: Path) -> tuple[int, int, int, int, int]:
    stat = Path(path).stat()
    return (
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(stat.st_ctime_ns),
    )


def _required_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BroadcastHybridOrchestrationError(f"{name} must be an object")
    return value


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BroadcastHybridOrchestrationError(f"{name} must be a non-empty string")
    return value.strip()


def _required_sha256(value: Any, name: str) -> str:
    text = _required_text(value, name).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise BroadcastHybridOrchestrationError(f"{name} must be a SHA-256")
    return text


def _positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise BroadcastHybridOrchestrationError(f"{name} must be a positive integer")
    return value


def _raise_if_cancelled(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel is not None and should_cancel():
        raise CancelledError()


def _jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
