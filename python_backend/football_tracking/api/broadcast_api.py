from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from football_tracking.action_signal import (
    ACTION_SIGNAL_MAX_TERMINAL_SHORTFALL_FRAMES,
    ACTION_SIGNAL_MAX_TERMINAL_SHORTFALL_SECONDS,
    ACTION_SIGNAL_TERMINAL_SHORTFALL_LIMITATION,
    ACTION_SIGNAL_TERMINAL_SHORTFALL_REASON,
    ACTION_SIGNAL_TERMINAL_SHORTFALL_STATUS,
)
from football_tracking.broadcast_hybrid_orchestration import (
    BroadcastHybridOrchestrationError,
    validate_final_broadcast_artifacts,
)
from football_tracking.candidate_annotations import sample_evidence_sha256
from football_tracking.target_finite_population import (
    TargetFinitePopulationError,
    validate_target_prelabel_commitment,
)

PUBLIC_ARTIFACT_NAMES = (
    "ball_candidates.jsonl",
    "candidate_classifications.jsonl",
    "ball_track.v2.csv",
    "action_track.csv",
    "review_decisions.json",
    "camera_target.csv",
    "broadcast.mp4",
    "broadcast_quality_report.json",
)
QUALITY_REPORT_NAME = "broadcast_quality_report.json"
FINAL_BINDINGS_NAME = "broadcast_artifact_bindings.v1.json"
TERMINAL_TAIL_REVIEW_NAME = "terminal_tail_review.v1.json"
TERMINAL_TAIL_REVIEW_BLOCKER = "terminal_decoder_shortfall_requires_operator_review"
TERMINAL_TAIL_REVIEW_INVALID_BLOCKER = "invalid_terminal_decoder_shortfall_review_evidence"
TERMINAL_TAIL_LIMITATION = ACTION_SIGNAL_TERMINAL_SHORTFALL_LIMITATION
_QUEUE_NAME = "selective_review_queue.v1.json"
_REVIEW_EVIDENCE_ACTIVATION_NAME = "review_evidence_activation.v1.json"
_REVIEW_EVIDENCE_BUNDLE_NAME = "review_evidence_bundle.v1.json"
_REVIEW_EVIDENCE_REVOCATION_NAME = "review_evidence_revocation.v1.json"
_MAX_JSON_BYTES = 256 * 1024 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024
_REVIEW_EVIDENCE_ARTIFACTS = ("tight_tensor", "context_tensor", "review_montage")
_REVIEW_QUEUE_BINDING_NAMES = frozenset(
    {
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
)
_TARGET_AUDIT_REVIEW_QUEUE_BINDING_NAMES = frozenset(
    {
        "target_audit_plan",
        "target_audit_labels",
        "target_qualification",
        "target_frozen_application",
        "target_prelabel_commitment",
    }
)
_TARGET_QUALIFICATION_REVIEW_QUEUE_BINDING_NAMES = frozenset(
    {
        "qualification_dataset",
        "qualification_predictions",
        "qualification_decisions",
    }
)
_TARGET_REVIEW_QUEUE_BINDING_NAMES = (
    _TARGET_AUDIT_REVIEW_QUEUE_BINDING_NAMES
    | _TARGET_QUALIFICATION_REVIEW_QUEUE_BINDING_NAMES
)
_TARGET_IDENTITY_BINDING_NAMES = frozenset(
    {
        "target_run_id",
        "source_sha256",
        "root_contract_sha256",
        "candidate_population_sha256",
        "model_sha256",
        "model_version",
        "confirmed_config_sha256",
        "policy_sha256",
        "policy_version",
        "thresholds_sha256",
    }
)
_WINDOWS_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class BroadcastApiError(RuntimeError):
    """Raised when the public broadcast facade cannot preserve evidence lineage."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_bound_json(path: Path, label: str) -> tuple[dict[str, Any], str]:
    path = Path(path).resolve()
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise BroadcastApiError(f"{label} is unavailable: {path}") from exc
    if size > _MAX_JSON_BYTES:
        raise BroadcastApiError(f"{label} exceeds the {_MAX_JSON_BYTES}-byte API limit")
    snapshot = path.read_bytes()
    digest = hashlib.sha256(snapshot).hexdigest()
    try:
        payload = json.loads(snapshot.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BroadcastApiError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise BroadcastApiError(f"{label} must be a JSON object")
    if sha256_file(path) != digest:
        raise BroadcastApiError(f"{label} changed while it was being read")
    return payload, digest


def inspect_terminal_tail_review(output_dir: Path) -> dict[str, Any]:
    """Return the evidence-bound operator-review state for an audited terminal shortfall."""

    root = _trusted_directory(output_dir, "terminal-tail review root")
    binding_path = root / "action_signal_binding.v1.json"
    acknowledgement_path = root / TERMINAL_TAIL_REVIEW_NAME
    if not binding_path.is_file():
        if acknowledgement_path.exists():
            raise BroadcastApiError("terminal-tail acknowledgement exists without action-signal evidence")
        return {"status": "not_required", "reason": None, "evidence": None}

    binding, binding_sha256 = load_bound_json(
        _contained_nonlink_file(root, binding_path, "action-signal binding"),
        "action-signal binding",
    )
    if binding.get("artifact_type") != "broadcast_action_signal_binding":
        raise BroadcastApiError("action-signal binding artifact_type is invalid")
    source = _required_mapping(binding.get("source"), "action-signal source")
    source_video_sha256 = _required_sha256(source.get("video_sha256"), "action-signal source video sha256")
    tracking_contract_sha256 = _required_sha256(
        source.get("tracking_contract_sha256"), "action-signal tracking contract sha256"
    )
    artifacts = _required_mapping(binding.get("artifacts"), "action-signal artifacts")
    action_descriptor = _required_mapping(
        artifacts.get("action_signal_report.v1.json"), "action-signal report descriptor"
    )
    action_signal_report_sha256 = _required_sha256(action_descriptor.get("sha256"), "action-signal report sha256")
    action_size = _required_nonnegative_int(action_descriptor.get("size_bytes"), "action-signal report size")
    action_path = _contained_nonlink_file(root, root / "action_signal_report.v1.json", "action-signal report")
    if action_path.stat().st_size != action_size or sha256_file(action_path) != action_signal_report_sha256:
        raise BroadcastApiError("action-signal report changed after it was bound")
    action_report, action_snapshot_sha256 = load_bound_json(action_path, "action-signal report")
    if action_snapshot_sha256 != action_signal_report_sha256:
        raise BroadcastApiError("action-signal report snapshot does not match its binding")

    raw_audit = binding.get("terminal_shortfall_evidence")
    if raw_audit is None:
        limitations = action_report.get("limitations")
        if (
            action_report.get("status") == ACTION_SIGNAL_TERMINAL_SHORTFALL_STATUS
            or isinstance(limitations, list)
            and any(isinstance(item, dict) and item.get("code") == TERMINAL_TAIL_LIMITATION for item in limitations)
        ):
            raise BroadcastApiError("terminal shortfall is missing its trusted audit evidence")
        if acknowledgement_path.exists():
            raise BroadcastApiError("terminal-tail acknowledgement exists when no review is required")
        return {"status": "not_required", "reason": None, "evidence": None}

    audit = _required_mapping(raw_audit, "terminal shortfall audit")
    if _required_sha256(audit.get("source_video_sha256"), "terminal audit source video sha256") != source_video_sha256:
        raise BroadcastApiError("terminal audit source video does not match the action-signal binding")
    if (
        _required_sha256(audit.get("tracking_contract_sha256"), "terminal audit tracking contract sha256")
        != tracking_contract_sha256
    ):
        raise BroadcastApiError("terminal audit tracking contract does not match the action-signal binding")

    contract_path = _contained_nonlink_file(root, root / "tracking_contract.v2.json", "tracking contract")
    if sha256_file(contract_path) != tracking_contract_sha256:
        raise BroadcastApiError("tracking contract changed after terminal evidence was audited")
    contract, contract_snapshot_sha256 = load_bound_json(contract_path, "tracking contract")
    if contract_snapshot_sha256 != tracking_contract_sha256:
        raise BroadcastApiError("tracking contract snapshot does not match terminal evidence")
    contract_source = _required_mapping(contract.get("source"), "tracking contract source")
    contract_summary = _required_mapping(contract.get("summary"), "tracking contract summary")

    temporal_chunks_report_sha256 = _required_sha256(
        audit.get("temporal_chunks_report_sha256"), "terminal audit temporal chunks sha256"
    )
    temporal_path = _contained_nonlink_file(root, root / "temporal_chunks_report.json", "temporal chunks report")
    if sha256_file(temporal_path) != temporal_chunks_report_sha256:
        raise BroadcastApiError("temporal chunks report changed after terminal evidence was audited")
    temporal_report, temporal_snapshot_sha256 = load_bound_json(temporal_path, "temporal chunks report")
    if temporal_snapshot_sha256 != temporal_chunks_report_sha256:
        raise BroadcastApiError("temporal chunks snapshot does not match terminal evidence")

    reported = _required_positive_int(audit.get("reported_frame_count"), "terminal reported frame count")
    verified = _required_positive_int(audit.get("verified_frame_count"), "terminal verified frame count")
    gap_frames = _required_positive_int(audit.get("missing_frame_count"), "terminal missing frame count")
    gap_seconds = _required_positive_finite(audit.get("missing_duration_seconds"), "terminal missing duration")
    source_fps = _required_positive_finite(audit.get("source_fps"), "terminal source fps")
    contract_fps = _required_positive_finite(contract_source.get("fps"), "tracking source fps")
    action_fps = _required_positive_finite(action_report.get("fps"), "action-signal fps")
    policy = _required_mapping(audit.get("policy"), "terminal shortfall policy")
    boundary_events = temporal_report.get("boundary_events")
    boundary_event = (
        boundary_events[0]
        if isinstance(boundary_events, list) and len(boundary_events) == 1 and isinstance(boundary_events[0], dict)
        else None
    )
    limitation_rows = action_report.get("limitations")
    limitation = (
        limitation_rows[0]
        if isinstance(limitation_rows, list) and len(limitation_rows) == 1 and isinstance(limitation_rows[0], dict)
        else None
    )
    if (
        reported - verified != gap_frames
        or gap_frames > ACTION_SIGNAL_MAX_TERMINAL_SHORTFALL_FRAMES
        or gap_seconds > ACTION_SIGNAL_MAX_TERMINAL_SHORTFALL_SECONDS + 1e-9
        or not math.isclose(gap_seconds, gap_frames / source_fps, rel_tol=0.0, abs_tol=1e-9)
        or not math.isclose(source_fps, contract_fps, rel_tol=0.0, abs_tol=1e-6)
        or not math.isclose(source_fps, action_fps, rel_tol=0.0, abs_tol=1e-6)
        or policy
        != {
            "max_missing_frames": ACTION_SIGNAL_MAX_TERMINAL_SHORTFALL_FRAMES,
            "max_missing_seconds": ACTION_SIGNAL_MAX_TERMINAL_SHORTFALL_SECONDS,
            "requires_manual_review": True,
        }
        or _required_positive_int(contract_source.get("frame_count"), "tracking source frame count") != reported
        or _required_sha256(contract_source.get("video_sha256"), "tracking source video sha256") != source_video_sha256
        or _required_positive_int(contract_summary.get("frame_count"), "tracking summary frame count") != verified
        or temporal_report.get("frame_count") != verified
        or not isinstance(boundary_event, dict)
        or boundary_event.get("type") != "truncated_final_tail"
        or boundary_event.get("first_missing_frame") != verified
        or boundary_event.get("last_missing_frame") != reported - 1
        or boundary_event.get("missing_frame_count") != gap_frames
        or boundary_event.get("planned_core_end_frame") != reported - 1
        or boundary_event.get("stitched_core_end_frame") != verified - 1
        or action_report.get("status") != ACTION_SIGNAL_TERMINAL_SHORTFALL_STATUS
        or action_report.get("termination_reason") != ACTION_SIGNAL_TERMINAL_SHORTFALL_REASON
        or action_report.get("expected_frame_count") != reported
        or action_report.get("frame_count") != verified
        or not isinstance(limitation, dict)
        or limitation.get("code") != TERMINAL_TAIL_LIMITATION
        or limitation.get("requires_manual_review") is not True
        or limitation.get("expected_frame_count") != reported
        or limitation.get("decoded_frame_count") != verified
        or limitation.get("missing_terminal_frames") != gap_frames
        or not _numbers_close(limitation.get("missing_terminal_seconds"), gap_seconds)
        or limitation.get("expected_terminal_shortfall_frames") != gap_frames
        or not _numbers_close(
            limitation.get("max_accepted_terminal_shortfall_seconds"),
            ACTION_SIGNAL_MAX_TERMINAL_SHORTFALL_SECONDS,
        )
        or limitation.get("policy") != "trusted_full_source_terminal_tail_only"
        or audit.get("first_missing_frame") != verified
        or audit.get("last_missing_frame") != reported - 1
    ):
        raise BroadcastApiError("terminal shortfall evidence is internally inconsistent")

    evidence_core = {
        "source_video_sha256": source_video_sha256,
        "tracking_contract_sha256": tracking_contract_sha256,
        "action_signal_report_sha256": action_signal_report_sha256,
        "temporal_chunks_report_sha256": temporal_chunks_report_sha256,
        "reported_frame_count": reported,
        "verified_frame_count": verified,
        "gap_frames": gap_frames,
        "gap_seconds": gap_seconds,
    }
    evidence_sha256 = _canonical_sha256(evidence_core)
    evidence = {**evidence_core, "evidence_sha256": evidence_sha256}
    if not acknowledgement_path.is_file():
        return {"status": "required", "reason": TERMINAL_TAIL_REVIEW_BLOCKER, "evidence": evidence}

    acknowledgement, acknowledgement_sha256 = load_bound_json(
        _contained_nonlink_file(root, acknowledgement_path, "terminal-tail acknowledgement"),
        "terminal-tail acknowledgement",
    )
    if set(acknowledgement) != {
        "schema_version",
        "artifact_type",
        "reviewed_at",
        "decision",
        "reviewer_id",
        "action_signal_binding_sha256",
        "evidence",
    }:
        raise BroadcastApiError("terminal-tail acknowledgement fields do not match the immutable contract")
    if (
        acknowledgement.get("schema_version") != "1.0"
        or acknowledgement.get("artifact_type") != "broadcast_terminal_tail_review"
        or acknowledgement.get("decision") != "accept_terminal_shortfall"
        or _required_sha256(
            acknowledgement.get("action_signal_binding_sha256"),
            "terminal-tail acknowledgement action binding sha256",
        )
        != binding_sha256
        or _required_mapping(acknowledgement.get("evidence"), "terminal-tail acknowledgement evidence") != evidence
    ):
        raise BroadcastApiError("terminal-tail acknowledgement does not match current evidence")
    reviewer_id = _required_text(acknowledgement.get("reviewer_id"), "terminal-tail reviewer id")
    reviewed_at = _required_timestamp(acknowledgement.get("reviewed_at"), "terminal-tail reviewed_at")
    return {
        "status": "accepted",
        "reason": None,
        "evidence": evidence,
        "decision": "accept_terminal_shortfall",
        "reviewer_id": reviewer_id,
        "reviewed_at": reviewed_at,
        "acknowledgement_sha256": acknowledgement_sha256,
    }


def build_terminal_tail_review_acknowledgement(
    output_dir: Path,
    *,
    decision: str,
    reviewer_id: str,
    evidence_sha256: str,
    reviewed_at: str | None = None,
) -> dict[str, Any]:
    state = inspect_terminal_tail_review(output_dir)
    reviewer = _required_text(reviewer_id, "terminal-tail reviewer id")
    if decision != "accept_terminal_shortfall":
        raise BroadcastApiError("terminal-tail review decision is unsupported")
    expected_evidence_sha256 = _required_sha256(evidence_sha256, "terminal-tail evidence sha256")
    evidence = _required_mapping(state.get("evidence"), "terminal-tail evidence")
    if evidence.get("evidence_sha256") != expected_evidence_sha256:
        raise BroadcastApiError("terminal-tail evidence changed before acknowledgement")
    if state.get("status") == "accepted":
        if state.get("reviewer_id") != reviewer or state.get("decision") != decision:
            raise BroadcastApiError("terminal-tail acknowledgement is immutable")
        acknowledgement, _ = load_bound_json(
            Path(output_dir).resolve() / TERMINAL_TAIL_REVIEW_NAME,
            "terminal-tail acknowledgement",
        )
        return acknowledgement
    if state.get("status") != "required":
        raise BroadcastApiError("terminal-tail review is not available for acknowledgement")
    binding_path = Path(output_dir).resolve() / "action_signal_binding.v1.json"
    return {
        "schema_version": "1.0",
        "artifact_type": "broadcast_terminal_tail_review",
        "reviewed_at": _required_timestamp(reviewed_at or _utc_now_iso(), "terminal-tail reviewed_at"),
        "decision": decision,
        "reviewer_id": reviewer,
        "action_signal_binding_sha256": sha256_file(binding_path),
        "evidence": evidence,
    }


def build_review_action_envelope(
    queue_path: Path,
    actions: list[dict[str, Any]],
    *,
    trusted_root: Path | None = None,
) -> dict[str, Any]:
    if trusted_root is None:
        queue_path = Path(queue_path).resolve()
    else:
        root = _trusted_directory(trusted_root, "review action root")
        queue_path = _contained_nonlink_file(root, Path(queue_path), "selective review queue")
    queue, queue_sha256 = load_bound_json(queue_path, "selective review queue")
    target_finite_queue = queue.get("artifact_type") == "target_finite_population_review_queue"
    if queue_path.name != _QUEUE_NAME or (
        queue.get("artifact_type") != "selective_review_queue" and not target_finite_queue
    ):
        raise BroadcastApiError("review actions require a selective_review_queue.v1.json artifact")
    if target_finite_queue and (
        queue.get("schema_version") != "1.0" or queue.get("qualification_scope") != "target_finite_population"
    ):
        raise BroadcastApiError("target finite-population review queue envelope is invalid")
    queue_bindings = _required_mapping(queue.get("bindings"), "review queue bindings")
    binding_fields = {
        "timing_sha256": "review_timing",
        "policy_sha256": "policy",
        "decisions_sha256": "decisions",
        "model_sha256": "model",
        "training_report_sha256": "training_report",
        "model_weights_sha256": "model_weights",
        "dataset_sha256": "dataset",
        "predictions_sha256": "predictions",
        "contract_sha256": "contract",
        "annotation_resolution_sha256": "annotation_resolution",
        "resolved_tracking_contract_sha256": "resolved_tracking_contract",
        "policy_roles_sha256": "policy_roles",
    }
    qualification_binding_fields = {
        "qualification_dataset_sha256": "qualification_dataset",
        "qualification_predictions_sha256": "qualification_predictions",
        "qualification_decisions_sha256": "qualification_decisions",
    }
    present_qualification = {
        output_name: source_name
        for output_name, source_name in qualification_binding_fields.items()
        if source_name in queue_bindings
    }
    if target_finite_queue:
        missing_qualification = set(qualification_binding_fields.values()) - set(
            queue_bindings
        )
        if missing_qualification:
            raise BroadcastApiError(
                "target review queue qualification bindings must be complete: "
                f"{sorted(missing_qualification)}"
            )
        binding_fields.update(qualification_binding_fields)
    else:
        if present_qualification and len(present_qualification) != len(
            qualification_binding_fields
        ):
            raise BroadcastApiError("review queue qualification bindings must be complete")
        binding_fields.update(present_qualification)

    target_binding_fields = {
        "target_audit_plan_sha256": "target_audit_plan",
        "target_audit_labels_sha256": "target_audit_labels",
        "target_qualification_sha256": "target_qualification",
        "target_frozen_application_sha256": "target_frozen_application",
        "target_prelabel_commitment_sha256": "target_prelabel_commitment",
    }
    if target_finite_queue:
        missing_target_bindings = set(target_binding_fields.values()) - set(queue_bindings)
        if missing_target_bindings:
            raise BroadcastApiError(
                "target review queue target audit bindings must be complete: "
                f"{sorted(missing_target_bindings)}"
            )
        binding_fields.update(target_binding_fields)
    elif set(target_binding_fields.values()) & set(queue_bindings):
        raise BroadcastApiError("legacy review queue may not carry target finite-population bindings")
    shared_bindings = {"queue_sha256": queue_sha256}
    for output_name, source_name in binding_fields.items():
        binding = _required_mapping(queue_bindings.get(source_name), f"review queue binding {source_name}")
        shared_bindings[output_name] = _required_sha256(binding.get("sha256"), f"{source_name}.sha256")

    indexed_candidates: dict[str, tuple[str, dict[str, Any]]] = {}
    items = queue.get("items")
    if not isinstance(items, list):
        raise BroadcastApiError("review queue items must be a list")
    review_item_count = queue.get("review_item_count")
    if isinstance(review_item_count, bool) or not isinstance(review_item_count, int):
        raise BroadcastApiError("review queue review_item_count must be an integer")
    if review_item_count != len(items):
        raise BroadcastApiError("review queue review_item_count does not match items")
    for item_index, item in enumerate(items):
        item = _required_mapping(item, f"review queue item {item_index}")
        review_item_id = _required_text(item.get("review_item_id"), f"review queue item {item_index} id")
        candidates = item.get("candidates")
        if not isinstance(candidates, list):
            raise BroadcastApiError(f"review queue item {review_item_id!r} candidates must be a list")
        for candidate_index, candidate in enumerate(candidates):
            candidate = _required_mapping(candidate, f"review candidate {candidate_index}")
            candidate_id = _required_text(candidate.get("candidate_id"), "review candidate id")
            if candidate_id in indexed_candidates:
                raise BroadcastApiError(f"candidate {candidate_id!r} appears in multiple review items")
            indexed_candidates[candidate_id] = (review_item_id, candidate)

    bound_actions: list[dict[str, Any]] = []
    seen_action_ids: set[str] = set()
    seen_candidate_ids: set[str] = set()
    for index, raw_action in enumerate(actions):
        action = dict(_required_mapping(raw_action, f"review action {index}"))
        action_id = _required_text(action.get("action_id"), f"review action {index} action_id")
        if action_id in seen_action_ids:
            raise BroadcastApiError(f"duplicate review action_id: {action_id}")
        seen_action_ids.add(action_id)
        review_item_id = _required_text(action.get("review_item_id"), f"review action {index} review_item_id")
        candidate_id = _required_text(action.get("candidate_id"), f"review action {index} candidate_id")
        if candidate_id in seen_candidate_ids:
            raise BroadcastApiError(f"candidate {candidate_id!r} has multiple review actions")
        seen_candidate_ids.add(candidate_id)
        entry = indexed_candidates.get(candidate_id)
        if entry is None or entry[0] != review_item_id:
            raise BroadcastApiError(
                f"review action does not match the bound queue item/candidate: {review_item_id}/{candidate_id}"
            )
        candidate = entry[1]
        evidence = _required_mapping(candidate.get("evidence"), f"candidate {candidate_id} evidence")
        action["created_at"] = _required_timestamp(
            action.get("created_at") or _utc_now_iso(),
            "review action created_at",
        )
        action["bindings"] = {
            **shared_bindings,
            "evidence_sha256": _required_sha256(evidence.get("sha256"), f"candidate {candidate_id} evidence sha256"),
            "candidate_fingerprint": _required_sha256(
                candidate.get("candidate_fingerprint"), f"candidate {candidate_id} fingerprint"
            ),
        }
        bound_actions.append(action)
    if seen_candidate_ids != set(indexed_candidates):
        missing = sorted(set(indexed_candidates) - seen_candidate_ids)
        raise BroadcastApiError(f"review actions must cover every bound queue candidate; missing={missing}")
    if sha256_file(queue_path) != queue_sha256:
        raise BroadcastApiError("selective review queue changed while actions were being bound")
    return {"schema_version": "1.0", "artifact_type": "selective_review_actions", "actions": bound_actions}


def validate_review_queue_bindings(
    queue_path: Path,
    *,
    trusted_root: Path | None = None,
    binding_base: Path | None = None,
) -> tuple[dict[str, Any], str]:
    """Re-hash every evidence artifact referenced by a selective review queue."""

    root: Path | None = None
    if trusted_root is None:
        queue_path = Path(queue_path).resolve()
    else:
        root = _trusted_directory(trusted_root, "review queue root")
        queue_path = _contained_nonlink_file(root, Path(queue_path), "selective review queue")
    resolved_binding_base = queue_path.parent
    if binding_base is not None:
        resolved_binding_base = _trusted_directory(binding_base, "review queue binding base")
        if root is not None:
            try:
                resolved_binding_base.relative_to(root)
            except ValueError as exc:
                raise BroadcastApiError("review queue binding base must remain inside the trusted review root") from exc
    queue, queue_sha256 = load_bound_json(queue_path, "selective review queue")
    target_finite_queue = queue.get("artifact_type") == "target_finite_population_review_queue"
    if queue_path.name != _QUEUE_NAME or (
        queue.get("artifact_type") != "selective_review_queue" and not target_finite_queue
    ):
        raise BroadcastApiError("review windows require a selective_review_queue.v1.json artifact")
    if target_finite_queue and (
        queue.get("schema_version") != "1.0" or queue.get("qualification_scope") != "target_finite_population"
    ):
        raise BroadcastApiError("target finite-population review queue envelope is invalid")
    bindings = _required_mapping(queue.get("bindings"), "review queue bindings")
    required = (
        _REVIEW_QUEUE_BINDING_NAMES | _TARGET_REVIEW_QUEUE_BINDING_NAMES
        if target_finite_queue
        else _REVIEW_QUEUE_BINDING_NAMES
    )
    if not target_finite_queue and _TARGET_AUDIT_REVIEW_QUEUE_BINDING_NAMES & set(
        bindings
    ):
        raise BroadcastApiError("legacy review queue may not carry target finite-population bindings")
    if not required.issubset(bindings):
        raise BroadcastApiError(f"review queue bindings are incomplete: {sorted(required - set(bindings))}")
    validated_paths: dict[str, Path] = {}
    for name in sorted(required):
        binding = _required_mapping(bindings[name], f"review queue binding {name}")
        raw_path = Path(_required_text(binding.get("path"), f"review queue binding {name} path"))
        path = raw_path if raw_path.is_absolute() else resolved_binding_base / raw_path
        if root is None:
            path = path.resolve()
            if not path.is_file():
                raise BroadcastApiError(f"bound review evidence is unavailable: {name}")
        else:
            path = _contained_nonlink_file(root, path, f"bound review evidence {name}")
        if sha256_file(path) != _required_sha256(binding.get("sha256"), f"review queue binding {name} sha256"):
            raise BroadcastApiError(f"bound review evidence changed: {name}")
        validated_paths[name] = path
    if target_finite_queue:
        _validate_target_review_queue_chain(queue, validated_paths)
    elif "target_bindings" in queue:
        raise BroadcastApiError("legacy review queue may not carry exact target bindings")
    _validate_bound_dataset_sample_artifacts(
        queue_path,
        bindings["dataset"],
        trusted_root=root,
        binding_base=resolved_binding_base,
    )
    if sha256_file(queue_path) != queue_sha256:
        raise BroadcastApiError("selective review queue changed during validation")
    return queue, queue_sha256


def _validate_target_review_queue_chain(
    queue: dict[str, Any],
    artifact_paths: dict[str, Path],
) -> None:
    target_bindings = _required_mapping(
        queue.get("target_bindings"),
        "review queue exact target bindings",
    )
    if set(target_bindings) != _TARGET_IDENTITY_BINDING_NAMES:
        raise BroadcastApiError("review queue exact target bindings are incomplete or contain extras")
    for field in _TARGET_IDENTITY_BINDING_NAMES:
        if field.endswith("_sha256"):
            _required_sha256(target_bindings.get(field), f"target_bindings.{field}")
        else:
            _required_text(target_bindings.get(field), f"target_bindings.{field}")

    plan, _ = load_bound_json(
        artifact_paths["target_audit_plan"],
        "target audit plan",
    )
    qualification, _ = load_bound_json(
        artifact_paths["target_qualification"],
        "target qualification",
    )
    application, _ = load_bound_json(
        artifact_paths["decisions"],
        "target qualified application",
    )
    frozen_application, _ = load_bound_json(
        artifact_paths["target_frozen_application"],
        "target frozen application",
    )
    labels, _ = load_bound_json(
        artifact_paths["target_audit_labels"],
        "target audit labels",
    )
    try:
        validate_target_prelabel_commitment(
            plan,
            artifact_paths["target_prelabel_commitment"],
        )
    except (OSError, ValueError, TargetFinitePopulationError) as exc:
        raise BroadcastApiError(f"review queue target pre-label commitment is invalid: {exc}") from exc
    external_commitment = _required_mapping(
        plan.get("external_commitment"),
        "target audit plan external commitment",
    )
    external_commitment_sha256 = external_commitment.get("record_sha256")
    if (
        plan.get("artifact_type") != "target_finite_population_audit_plan"
        or plan.get("bindings") != target_bindings
        or qualification.get("artifact_type") != "target_finite_population_qualification"
        or qualification.get("bindings") != target_bindings
        or qualification.get("plan_sha256") != plan.get("plan_sha256")
        or qualification.get("external_commitment_sha256") != external_commitment_sha256
        or application.get("artifact_type") != "target_finite_population_qualified_application"
        or application.get("bindings") != target_bindings
        or application.get("plan_sha256") != plan.get("plan_sha256")
        or application.get("external_commitment_sha256") != external_commitment_sha256
        or application.get("qualification_sha256") != qualification.get("qualification_sha256")
        or labels.get("artifact_type") != "target_finite_population_audit_labels"
        or labels.get("plan_sha256") != plan.get("plan_sha256")
        or labels.get("external_commitment_sha256") != external_commitment_sha256
        or labels.get("plan_commitment_sha256") != plan.get("plan_commitment_sha256")
        or labels.get("sampling_design_sha256") != plan.get("sampling_design_sha256")
        or labels.get("sample_sha256") != plan.get("sample_sha256")
    ):
        raise BroadcastApiError("review queue target artifacts do not share one exact target identity")
    expected_evidence = {
        name: target_bindings[name]
        for name in (
            "source_sha256",
            "root_contract_sha256",
            "candidate_population_sha256",
            "model_sha256",
            "model_version",
            "policy_sha256",
            "policy_version",
            "thresholds_sha256",
        )
    }
    if (
        frozen_application.get("artifact_type") != "target_finite_population_application"
        or frozen_application.get("target_binding_evidence") != expected_evidence
        or plan.get("frozen_application_content_sha256")
        != frozen_application.get("application_content_sha256")
    ):
        raise BroadcastApiError("review queue frozen application target identity is inconsistent")


def validate_review_queue_activation(
    output_dir: Path,
    queue_path: Path,
    *,
    expected_target: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """Validate an imported queue's immutable generation and activation manifest, when present."""

    root = _trusted_directory(output_dir, "review activation root")
    queue_path = _contained_nonlink_file(root, Path(queue_path), "selective review queue")
    queue, queue_sha256 = validate_review_queue_bindings(queue_path, trusted_root=root)
    activation = queue.get("activation")
    if activation is None:
        return queue, queue_sha256
    collect_review_evidence_paths(queue_path, trusted_root=root)
    activation = _required_mapping(activation, "review queue activation")
    generation_id = _required_text(activation.get("generation_id"), "review queue activation generation_id")
    suffix = generation_id.removeprefix("review-evidence-")
    if (
        not generation_id.startswith("review-evidence-")
        or len(suffix) != 24
        or any(character not in "0123456789abcdef" for character in suffix)
    ):
        raise BroadcastApiError("review queue activation generation_id is invalid")
    bundle_id = _required_text(activation.get("bundle_id"), "review queue activation bundle_id")
    generation_dir = root / "review_evidence" / "generations" / generation_id
    if _is_link_or_reparse(generation_dir) or not generation_dir.is_dir():
        raise BroadcastApiError("review evidence generation is unavailable")
    if (generation_dir / _REVIEW_EVIDENCE_REVOCATION_NAME).exists():
        raise BroadcastApiError("review evidence generation was revoked")
    activation_path = _contained_nonlink_file(
        root,
        generation_dir / _REVIEW_EVIDENCE_ACTIVATION_NAME,
        "review evidence activation manifest",
    )
    activation_manifest, _ = load_bound_json(activation_path, "review evidence activation manifest")
    if (
        activation_manifest.get("artifact_type") != "broadcast_review_evidence_activation"
        or activation_manifest.get("generation_id") != generation_id
        or activation_manifest.get("bundle_id") != bundle_id
    ):
        raise BroadcastApiError("review evidence activation manifest does not match the root queue")
    activation_target = _required_mapping(
        activation_manifest.get("target"),
        "review evidence activation target",
    )
    if expected_target is not None and activation_target != expected_target:
        raise BroadcastApiError("review evidence activation target does not match the current run context")
    expected_queue_sha256 = _required_sha256(
        activation_manifest.get("activated_queue_sha256"),
        "review evidence activation queue sha256",
    )
    generation_queue_path = _contained_nonlink_file(
        root,
        generation_dir / _QUEUE_NAME,
        "review evidence generation queue",
    )
    if sha256_file(generation_queue_path) != expected_queue_sha256 or queue_sha256 != expected_queue_sha256:
        raise BroadcastApiError("root review queue does not match its immutable generation")
    generation_queue, generation_queue_sha256 = validate_review_queue_bindings(
        generation_queue_path,
        trusted_root=root,
        binding_base=root,
    )
    collect_review_evidence_paths(generation_queue_path, trusted_root=root, binding_base=root)
    if generation_queue != queue or generation_queue_sha256 != queue_sha256:
        raise BroadcastApiError("review evidence generation queue does not match the root queue")
    bundle_manifest_path = _contained_nonlink_file(
        root,
        generation_dir / "bundle" / _REVIEW_EVIDENCE_BUNDLE_NAME,
        "review evidence bundle manifest",
    )
    if sha256_file(bundle_manifest_path) != _required_sha256(
        activation_manifest.get("bundle_sha256"), "review evidence activation bundle sha256"
    ):
        raise BroadcastApiError("review evidence bundle manifest changed after activation")
    bundle_manifest, _ = load_bound_json(bundle_manifest_path, "review evidence bundle manifest")
    if bundle_manifest.get("bundle_id") != bundle_id or bundle_manifest.get("target") != activation_target:
        raise BroadcastApiError("review evidence bundle target does not match activation")
    request_identity = _required_mapping(
        activation_manifest.get("request_identity"), "review evidence activation request identity"
    )
    expected_target_sha256 = hashlib.sha256(
        json.dumps(
            bundle_manifest.get("target"),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    if (
        request_identity.get("bundle_id") != bundle_id
        or request_identity.get("bundle_manifest_sha256") != sha256_file(bundle_manifest_path)
        or request_identity.get("target_sha256") != expected_target_sha256
    ):
        raise BroadcastApiError("review evidence activation request identity is stale")
    reconciliation = _required_mapping(
        activation_manifest.get("reconciliation"), "review evidence activation reconciliation"
    )
    bundle_reconciliation = _required_mapping(
        bundle_manifest.get("reconciliation"), "review evidence bundle reconciliation"
    )
    if reconciliation != bundle_reconciliation:
        raise BroadcastApiError("review evidence activation reconciliation binding is stale")
    reconciliation_path = _contained_nonlink_file(
        root,
        generation_dir / "bundle" / _required_text(reconciliation.get("path"), "reconciliation path"),
        "review evidence reconciliation",
    )
    if sha256_file(reconciliation_path) != _required_sha256(
        reconciliation.get("sha256"), "review evidence reconciliation sha256"
    ):
        raise BroadcastApiError("review evidence reconciliation changed after activation")
    return queue, queue_sha256


def _validate_bound_dataset_sample_artifacts(
    queue_path: Path,
    raw_dataset_binding: Any,
    *,
    trusted_root: Path | None,
    binding_base: Path,
) -> None:
    """Re-hash every file descriptor carried by the queue-bound dataset."""

    binding = _required_mapping(raw_dataset_binding, "review queue dataset binding")
    raw_dataset_path = Path(_required_text(binding.get("path"), "review queue dataset path"))
    if not raw_dataset_path.is_absolute():
        raw_dataset_path = binding_base / raw_dataset_path
    if trusted_root is None:
        dataset_path = Path(os.path.abspath(raw_dataset_path))
        if _is_link_or_reparse(dataset_path) or not dataset_path.is_file():
            raise BroadcastApiError("candidate dataset manifest is unavailable")
    else:
        dataset_path = _contained_nonlink_file(
            trusted_root,
            raw_dataset_path,
            "candidate dataset manifest",
        )
    dataset, dataset_sha256 = load_bound_json(dataset_path, "candidate dataset manifest")
    if dataset_sha256 != _required_sha256(binding.get("sha256"), "review queue dataset sha256"):
        raise BroadcastApiError("candidate dataset manifest changed after queue validation")
    if dataset.get("artifact_type") != "candidate_dataset":
        raise BroadcastApiError("review queue dataset binding requires artifact_type 'candidate_dataset'")

    raw_samples = dataset.get("samples", [])
    if not isinstance(raw_samples, list):
        raise BroadcastApiError("candidate dataset samples must be a list")
    dataset_root = _trusted_directory(dataset_path.parent, "candidate dataset root")
    for sample_index, raw_sample in enumerate(raw_samples):
        sample = _required_mapping(raw_sample, f"candidate dataset sample {sample_index}")
        sample_id = sample.get("sample_id")
        sample_label = sample_id if isinstance(sample_id, str) and sample_id else str(sample_index)
        artifacts = _required_mapping(
            sample.get("artifacts"),
            f"candidate dataset sample {sample_label} artifacts",
        )
        for artifact_name, raw_descriptor in artifacts.items():
            if not isinstance(artifact_name, str) or not artifact_name:
                raise BroadcastApiError(f"candidate dataset sample {sample_label} artifact name is invalid")
            descriptor = _required_mapping(
                raw_descriptor,
                f"candidate dataset sample {sample_label} artifact {artifact_name}",
            )
            raw_artifact_path = Path(
                _required_text(
                    descriptor.get("path"),
                    f"candidate dataset sample {sample_label} artifact {artifact_name} path",
                )
            )
            if raw_artifact_path.is_absolute() or ".." in raw_artifact_path.parts:
                raise BroadcastApiError(
                    f"candidate dataset sample {sample_label} artifact {artifact_name} path must be contained and relative"
                )
            artifact_path = _contained_nonlink_file(
                dataset_root,
                dataset_path.parent / raw_artifact_path,
                f"candidate dataset sample {sample_label} artifact {artifact_name}",
            )
            _verify_bound_file(
                artifact_path,
                expected_sha256=_required_sha256(
                    descriptor.get("sha256"),
                    f"candidate dataset sample {sample_label} artifact {artifact_name} sha256",
                ),
                expected_size=_required_nonnegative_int(
                    descriptor.get("size_bytes"),
                    f"candidate dataset sample {sample_label} artifact {artifact_name} size_bytes",
                ),
                label=f"candidate dataset sample {sample_label} artifact {artifact_name}",
            )
    if sha256_file(dataset_path) != dataset_sha256:
        raise BroadcastApiError("candidate dataset manifest changed during sample validation")


def collect_review_evidence_paths(
    queue_path: Path,
    trusted_root: Path,
    *,
    binding_base: Path | None = None,
) -> list[Path]:
    """Return only queue-bound sample artifacts that are safe for run downloads."""

    root = _trusted_directory(trusted_root, "review evidence root")
    queue_path = _contained_nonlink_file(root, Path(queue_path), "selective review queue")
    resolved_binding_base = (
        queue_path.parent if binding_base is None else _trusted_directory(binding_base, "review binding base")
    )
    try:
        resolved_binding_base.relative_to(root)
    except ValueError as exc:
        raise BroadcastApiError("review binding base must remain inside the trusted review root") from exc
    queue, queue_sha256 = validate_review_queue_bindings(
        queue_path,
        trusted_root=root,
        binding_base=resolved_binding_base if binding_base is not None else None,
    )
    bindings = _required_mapping(queue.get("bindings"), "review queue bindings")
    dataset_binding = _required_mapping(bindings.get("dataset"), "review queue dataset binding")
    raw_dataset_path = Path(_required_text(dataset_binding.get("path"), "review queue dataset path"))
    if not raw_dataset_path.is_absolute():
        raw_dataset_path = resolved_binding_base / raw_dataset_path
    dataset_path = _contained_nonlink_file(root, raw_dataset_path, "candidate dataset manifest")
    dataset, dataset_sha256 = load_bound_json(dataset_path, "candidate dataset manifest")
    expected_dataset_sha256 = _required_sha256(
        dataset_binding.get("sha256"),
        "review queue dataset sha256",
    )
    if dataset_sha256 != expected_dataset_sha256:
        raise BroadcastApiError("candidate dataset manifest changed after queue validation")
    if dataset.get("artifact_type") != "candidate_dataset":
        raise BroadcastApiError("review evidence requires a candidate_dataset manifest")
    dataset_version = _required_sha256(dataset.get("dataset_version"), "candidate dataset version")
    raw_samples = dataset.get("samples")
    if not isinstance(raw_samples, list):
        raise BroadcastApiError("candidate dataset samples must be a list")

    samples: dict[tuple[str, str], dict[str, Any]] = {}
    for index, raw_sample in enumerate(raw_samples):
        sample = _required_mapping(raw_sample, f"candidate dataset sample {index}")
        sample_id = _required_text(sample.get("sample_id"), f"candidate dataset sample {index} sample_id")
        candidate_id = _required_text(
            sample.get("candidate_id"),
            f"candidate dataset sample {index} candidate_id",
        )
        key = (sample_id, candidate_id)
        if key in samples:
            raise BroadcastApiError(f"duplicate candidate dataset sample: {sample_id}/{candidate_id}")
        samples[key] = sample

    items = queue.get("items")
    if not isinstance(items, list):
        raise BroadcastApiError("review queue items must be a list")
    if queue.get("review_item_count") != len(items):
        raise BroadcastApiError("review queue review_item_count does not match items")
    evidence_paths: set[Path] = set()
    seen_candidates: set[str] = set()
    for item_index, raw_item in enumerate(items):
        item = _required_mapping(raw_item, f"review queue item {item_index}")
        candidates = item.get("candidates")
        if not isinstance(candidates, list):
            raise BroadcastApiError(f"review queue item {item_index} candidates must be a list")
        for candidate_index, raw_candidate in enumerate(candidates):
            candidate = _required_mapping(raw_candidate, f"review queue candidate {candidate_index}")
            candidate_id = _required_text(candidate.get("candidate_id"), "review queue candidate id")
            if candidate_id in seen_candidates:
                raise BroadcastApiError(f"candidate {candidate_id!r} appears in multiple review windows")
            seen_candidates.add(candidate_id)
            evidence = _required_mapping(candidate.get("evidence"), f"candidate {candidate_id} evidence")
            sample_id = _required_text(evidence.get("sample_id"), f"candidate {candidate_id} sample_id")
            sample = samples.get((sample_id, candidate_id))
            if sample is None:
                raise BroadcastApiError(f"candidate {candidate_id!r} evidence is absent from the bound dataset")
            if evidence.get("dataset_version") != dataset_version:
                raise BroadcastApiError(f"candidate {candidate_id!r} dataset version binding is stale")
            try:
                expected_evidence_sha256 = sample_evidence_sha256(sample)
            except ValueError as exc:
                raise BroadcastApiError(f"candidate {candidate_id!r} dataset evidence is invalid: {exc}") from exc
            if evidence.get("sha256") != expected_evidence_sha256:
                raise BroadcastApiError(f"candidate {candidate_id!r} aggregate evidence hash is stale")
            sample_artifacts = _required_mapping(sample.get("artifacts"), f"sample {sample_id} artifacts")
            queue_artifacts = _required_mapping(evidence.get("artifacts"), f"candidate {candidate_id} artifacts")
            if queue_artifacts != sample_artifacts:
                raise BroadcastApiError(f"candidate {candidate_id!r} artifacts do not match the bound dataset")
            for artifact_name in _REVIEW_EVIDENCE_ARTIFACTS:
                descriptor = _required_mapping(
                    sample_artifacts.get(artifact_name),
                    f"sample {sample_id} {artifact_name}",
                )
                raw_path = Path(_required_text(descriptor.get("path"), f"sample {sample_id} {artifact_name} path"))
                if raw_path.is_absolute() or ".." in raw_path.parts:
                    raise BroadcastApiError(f"sample {sample_id!r} {artifact_name} path must be contained and relative")
                artifact_path = _contained_nonlink_file(
                    root,
                    dataset_path.parent / raw_path,
                    f"sample {sample_id} {artifact_name}",
                )
                _verify_bound_file(
                    artifact_path,
                    expected_sha256=_required_sha256(
                        descriptor.get("sha256"),
                        f"sample {sample_id} {artifact_name} sha256",
                    ),
                    expected_size=_required_nonnegative_int(
                        descriptor.get("size_bytes"),
                        f"sample {sample_id} {artifact_name} size_bytes",
                    ),
                    label=f"sample {sample_id} {artifact_name}",
                )
                evidence_paths.add(artifact_path)

    if sha256_file(dataset_path) != dataset_sha256:
        raise BroadcastApiError("candidate dataset manifest changed during evidence validation")
    if sha256_file(queue_path) != queue_sha256:
        raise BroadcastApiError("selective review queue changed during evidence validation")
    return sorted(evidence_paths, key=lambda path: path.relative_to(root).as_posix())


def publish_json_exclusive(
    path: Path,
    payload: dict[str, Any],
    *,
    trusted_root: Path | None = None,
) -> str:
    path = _safe_publish_target(path, trusted_root=trusted_root, label="JSON artifact")
    serialized = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode(
        "utf-8"
    )
    digest = hashlib.sha256(serialized).hexdigest()
    if path.exists():
        if sha256_file(path) != digest:
            raise BroadcastApiError(f"refusing to overwrite existing artifact: {path.name}")
        return digest
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_path, path)
        except FileExistsError:
            if _is_link_or_reparse(path) or not path.is_file() or sha256_file(path) != digest:
                raise BroadcastApiError(f"refusing to overwrite concurrently published artifact: {path.name}")
        return digest
    finally:
        temp_path.unlink(missing_ok=True)


def publish_broadcast_facade(output_dir: Path) -> dict[str, Any]:
    """Publish an immutable, versioned status snapshot for fixed public artifacts.

    The fixed root quality report is reserved for a fully validated final artifact set.
    Intermediate ``needs_review`` states live under ``broadcast_status/<state-id>/`` so
    later review/recompute/render stages never overwrite history. Candidate and
    classification aliases are deliberately withheld until final orchestration publishes
    the reviewed evidence set; publishing the initial aliases here would occupy immutable
    names that legitimately change after review and classifier re-inference.
    """

    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / QUALITY_REPORT_NAME
    if report_path.exists():
        return validate_broadcast_quality_report(output_dir, report_path)
    source_bindings: dict[str, dict[str, Any]] = {}
    contract_path = output_dir / "tracking_contract.v2.json"
    predictions_path = output_dir / "candidate_predictions.v1.json"
    if contract_path.is_file():
        source_bindings["tracking_contract.v2.json"] = _snapshot_binding(contract_path, output_dir)
    if predictions_path.is_file():
        source_bindings["candidate_predictions.v1.json"] = _snapshot_binding(predictions_path, output_dir)

    try:
        terminal_tail_review = inspect_terminal_tail_review(output_dir)
    except BroadcastApiError as exc:
        terminal_tail_review = {
            "status": "invalid",
            "reason": str(exc),
            "evidence": None,
        }
    if terminal_tail_review.get("evidence") is not None:
        for name in (
            "action_signal_report.v1.json",
            "temporal_chunks_report.json",
        ):
            source_path = output_dir / name
            if source_path.is_file():
                source_bindings[name] = _snapshot_binding(source_path, output_dir)
    if terminal_tail_review.get("status") == "accepted":
        acknowledgement_path = output_dir / TERMINAL_TAIL_REVIEW_NAME
        source_bindings[TERMINAL_TAIL_REVIEW_NAME] = _snapshot_binding(acknowledgement_path, output_dir)

    artifacts: dict[str, dict[str, Any]] = {}
    for public_name in PUBLIC_ARTIFACT_NAMES:
        if public_name == QUALITY_REPORT_NAME:
            continue
        target_path = output_dir / public_name
        if target_path.is_file():
            artifacts[public_name] = {
                "status": "available",
                "path": public_name,
                "sha256": sha256_file(target_path),
                "size_bytes": target_path.stat().st_size,
            }
        else:
            artifacts[public_name] = {"status": "missing", "path": public_name}

    blocking_reasons: list[str] = []
    if terminal_tail_review.get("status") == "required":
        blocking_reasons.append(TERMINAL_TAIL_REVIEW_BLOCKER)
    elif terminal_tail_review.get("status") == "invalid":
        blocking_reasons.append(TERMINAL_TAIL_REVIEW_INVALID_BLOCKER)
    if artifacts["ball_candidates.jsonl"]["status"] != "available":
        blocking_reasons.append("missing_reviewed_tracking_candidate_contract")
    if artifacts["candidate_classifications.jsonl"]["status"] != "available":
        blocking_reasons.append("missing_reviewed_classifier_predictions")
    queue_path = output_dir / _QUEUE_NAME
    if not queue_path.is_file():
        blocking_reasons.append("missing_qualified_selective_review_queue")
    else:
        try:
            validate_review_queue_activation(output_dir, queue_path)
        except BroadcastApiError:
            blocking_reasons.append("invalid_or_stale_selective_review_evidence")
    if artifacts["action_track.csv"]["status"] != "available":
        blocking_reasons.append("missing_action_track")
    if artifacts["review_decisions.json"]["status"] != "available":
        blocking_reasons.append("missing_bound_review_decisions")
    else:
        decisions, _ = load_bound_json(output_dir / "review_decisions.json", "broadcast review decisions")
        if decisions.get("artifact_type") != "selective_review_actions":
            blocking_reasons.append("invalid_bound_review_decisions")
    if artifacts["ball_track.v2.csv"]["status"] != "available":
        blocking_reasons.append("missing_global_ball_trajectory")
    if artifacts["camera_target.csv"]["status"] != "available":
        blocking_reasons.append("missing_hybrid_camera_path")
    if artifacts["broadcast.mp4"]["status"] != "available":
        blocking_reasons.append("missing_broadcast_render")
    binding_blockers, final_bindings = _validated_final_bindings(output_dir, artifacts)
    blocking_reasons.extend(binding_blockers)
    limitations = [
        "camera_solver_does_not_consume_action_track",
        "cooperative_cancellation_at_stage_boundaries_only",
        "source_audio_not_preserved",
    ]
    if terminal_tail_review.get("evidence") is not None:
        limitations.append(TERMINAL_TAIL_LIMITATION)

    state = {
        "schema_version": "1.0",
        "artifact_type": "broadcast_quality_report",
        "status": "needs_review" if blocking_reasons else "ready",
        "blocking_reasons": blocking_reasons,
        "limitations": limitations,
        "review_evidence": {"terminal_tail_review": terminal_tail_review},
        "lineage": {"sources": source_bindings},
        "artifacts": artifacts,
        "final_bindings": final_bindings,
        "capabilities": {
            "classifier_truth_synthesized": False,
            "selective_policy_weakened": False,
            "trajectory_corrections_applied": False,
            "action_track_consumed_by_camera_solver": False,
            "source_audio_preserved": False,
        },
    }
    state_id = hashlib.sha256(
        json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    status_path = _safe_status_generation_dir(output_dir, state_id) / QUALITY_REPORT_NAME
    if status_path.exists():
        report, _ = load_bound_json(status_path, "broadcast status generation")
        _verify_report_artifacts(output_dir, report)
        stable_report = {
            key: value for key, value in report.items() if key not in {"generated_at", "status_generation"}
        }
        if report.get("status_generation") != state_id or stable_report != state:
            raise BroadcastApiError("broadcast status generation does not match its immutable directory state")
    else:
        report = {**state, "generated_at": _utc_now_iso(), "status_generation": state_id}
        publish_json_exclusive(status_path, report, trusted_root=output_dir)
    if report["status"] == "ready":
        publish_json_exclusive(report_path, report, trusted_root=output_dir)
    return report


def validate_broadcast_quality_report(output_dir: Path, report_path: Path) -> dict[str, Any]:
    """Validate a root or versioned quality report and its complete artifact lineage."""

    output_dir = Path(output_dir).resolve()
    report_path = Path(report_path).resolve()
    report, report_sha256 = load_bound_json(report_path, "broadcast quality report")
    _verify_report_artifacts(output_dir, report)
    if report_path.parent == output_dir and report_path.name == QUALITY_REPORT_NAME:
        if report.get("status") != "ready":
            raise BroadcastApiError("root broadcast quality report must be ready")
        generation = _required_sha256(report.get("status_generation"), "status generation")
        versioned_path = _contained_nonlink_file(
            output_dir,
            output_dir / "broadcast_status" / generation / QUALITY_REPORT_NAME,
            "versioned broadcast quality report",
        )
        versioned_report, versioned_sha256 = load_bound_json(versioned_path, "versioned broadcast quality report")
        if versioned_report != report or versioned_sha256 != report_sha256:
            raise BroadcastApiError("root broadcast quality report does not match its immutable status generation")
        return report
    expected = (
        output_dir
        / "broadcast_status"
        / _required_text(report.get("status_generation"), "status generation")
        / QUALITY_REPORT_NAME
    ).resolve()
    if report_path != expected:
        raise BroadcastApiError("broadcast quality report path does not match its status generation")
    return report


def _safe_status_generation_dir(output_dir: Path, state_id: str) -> Path:
    raw_root = output_dir / "broadcast_status"
    try:
        raw_root.mkdir(parents=False, exist_ok=False)
    except FileExistsError:
        pass
    root = raw_root.resolve()
    if _is_link_or_reparse(raw_root) or root.parent != output_dir or not root.is_dir():
        raise BroadcastApiError("broadcast status root must be a direct non-symlink directory")
    raw_generation = raw_root / state_id
    try:
        raw_generation.mkdir(parents=False, exist_ok=False)
    except FileExistsError:
        pass
    generation = raw_generation.resolve()
    if _is_link_or_reparse(raw_generation) or generation.parent != root or not generation.is_dir():
        raise BroadcastApiError("broadcast status generation must be a direct non-symlink directory")
    return generation


def _verify_report_artifacts(output_dir: Path, report: dict[str, Any]) -> None:
    if report.get("artifact_type") != "broadcast_quality_report":
        raise BroadcastApiError("broadcast quality report artifact_type is invalid")
    status = report.get("status")
    if status not in {"needs_review", "ready"}:
        raise BroadcastApiError("broadcast quality report status is invalid")
    stable_state = {key: value for key, value in report.items() if key not in {"generated_at", "status_generation"}}
    expected_generation = hashlib.sha256(
        json.dumps(
            stable_state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    if report.get("status_generation") != expected_generation:
        raise BroadcastApiError("broadcast quality report status generation is invalid")
    lineage = _required_mapping(report.get("lineage"), "broadcast quality lineage")
    sources = _required_mapping(lineage.get("sources"), "broadcast quality sources")
    for source_name, raw_binding in sources.items():
        _verify_relative_binding(output_dir, source_name, raw_binding)
    artifacts = _required_mapping(report.get("artifacts"), "broadcast quality artifacts")
    expected_artifacts: set[str] = set(PUBLIC_ARTIFACT_NAMES)
    expected_artifacts.discard(QUALITY_REPORT_NAME)
    if set(artifacts) != expected_artifacts:
        raise BroadcastApiError("broadcast quality report artifacts do not match the fixed public contract")
    for public_name, raw_artifact in artifacts.items():
        artifact = _required_mapping(raw_artifact, f"public artifact {public_name}")
        if artifact.get("status") != "available":
            continue
        path = (output_dir / public_name).resolve()
        if path.parent != output_dir or not path.is_file():
            raise BroadcastApiError(f"published broadcast artifact is unavailable: {public_name}")
        if sha256_file(path) != _required_sha256(artifact.get("sha256"), f"public artifact {public_name} sha256"):
            raise BroadcastApiError(f"published broadcast artifact changed: {public_name}")
    if status == "ready":
        if report.get("blocking_reasons"):
            raise BroadcastApiError("ready broadcast quality report cannot contain blocking reasons")
        terminal_tail_review = inspect_terminal_tail_review(output_dir)
        if terminal_tail_review.get("status") not in {"not_required", "accepted"}:
            raise BroadcastApiError("ready broadcast quality report lacks terminal-tail operator review")
        review_evidence = _required_mapping(report.get("review_evidence"), "broadcast review evidence")
        if review_evidence.get("terminal_tail_review") != terminal_tail_review:
            raise BroadcastApiError("ready broadcast terminal-tail evidence is stale or invalid")
        limitations = report.get("limitations")
        if not isinstance(limitations, list):
            raise BroadcastApiError("ready broadcast limitations are invalid")
        expects_terminal_limitation = terminal_tail_review.get("evidence") is not None
        if (TERMINAL_TAIL_LIMITATION in limitations) is not expects_terminal_limitation:
            raise BroadcastApiError("ready broadcast terminal-tail limitation is inconsistent")
        validate_review_queue_bindings(output_dir / _QUEUE_NAME, trusted_root=output_dir)
        blockers, final_bindings = _validated_final_bindings(output_dir, artifacts)
        if blockers or report.get("final_bindings") != final_bindings:
            raise BroadcastApiError("ready broadcast quality report final bindings are stale or invalid")


def _validated_final_bindings(
    output_dir: Path, artifacts: dict[str, dict[str, Any]]
) -> tuple[list[str], dict[str, Any]]:
    available_names = {
        name
        for name, artifact in artifacts.items()
        if artifact.get("status") == "available" and name != QUALITY_REPORT_NAME
    }
    expected_artifacts: set[str] = set(PUBLIC_ARTIFACT_NAMES)
    expected_artifacts.discard(QUALITY_REPORT_NAME)
    if available_names != expected_artifacts:
        return ["incomplete_final_public_artifact_set"], {}
    manifest_path = output_dir / FINAL_BINDINGS_NAME
    if not manifest_path.is_file():
        return ["missing_validated_final_artifact_bindings"], {}
    try:
        validate_final_broadcast_artifacts(output_dir)
    except BroadcastHybridOrchestrationError as exc:
        raise BroadcastApiError(f"broadcast final artifact validation failed: {exc}") from exc
    manifest, manifest_sha256 = load_bound_json(manifest_path, "broadcast final artifact bindings")
    if manifest.get("artifact_type") != "broadcast_artifact_bindings":
        raise BroadcastApiError("broadcast final artifact bindings artifact_type is invalid")
    if manifest.get("orchestration_version") != "broadcast-hybrid-orchestration-v1":
        raise BroadcastApiError("broadcast final artifact bindings orchestration version is invalid")
    bindings = _required_mapping(manifest.get("artifacts"), "broadcast final artifact bindings")
    if set(bindings) != available_names:
        raise BroadcastApiError("broadcast final artifact bindings must cover every fixed public artifact exactly")
    for public_name in sorted(available_names):
        binding = _required_mapping(bindings[public_name], f"final binding {public_name}")
        expected_hash = _required_sha256(binding.get("sha256"), f"final binding {public_name} sha256")
        if expected_hash != artifacts[public_name]["sha256"]:
            raise BroadcastApiError(f"final binding does not match public artifact: {public_name}")
        report_binding = _required_mapping(binding.get("source_report"), f"final binding {public_name} source_report")
        _verify_relative_binding(output_dir, f"{public_name} source report", report_binding)
        report_path = (output_dir / _required_text(report_binding.get("path"), "final source report path")).resolve()
        source_report, _ = load_bound_json(report_path, f"{public_name} source report")
        expected_artifact_types = {
            "candidate_classifications.jsonl": "candidate_predictions",
            "ball_track.v2.csv": "global_ball_trajectory_report",
            "action_track.csv": "broadcast_action_signal_binding",
            "review_decisions.json": "selective_review_materialization",
            "camera_target.csv": "hybrid_broadcast_camera_report",
            "broadcast.mp4": "broadcast_render_report",
        }
        expected_type = expected_artifact_types.get(public_name)
        if expected_type is not None and source_report.get("artifact_type") != expected_type:
            raise BroadcastApiError(f"final binding source report type is invalid: {public_name}")
        if public_name == "ball_candidates.jsonl" and (
            source_report.get("schema_version") != "2.0" or not isinstance(source_report.get("candidates"), list)
        ):
            raise BroadcastApiError("final candidate contract source report is invalid")
    return [], {"path": FINAL_BINDINGS_NAME, "sha256": manifest_sha256}


def _verify_relative_binding(output_dir: Path, label: str, raw_binding: Any) -> None:
    binding = _required_mapping(raw_binding, f"binding {label}")
    relative = Path(_required_text(binding.get("path"), f"binding {label} path"))
    if relative.is_absolute() or ".." in relative.parts:
        raise BroadcastApiError(f"binding {label} path must be run-relative")
    path = (output_dir / relative).resolve()
    if output_dir not in path.parents or not path.is_file():
        raise BroadcastApiError(f"bound broadcast source is unavailable: {label}")
    if sha256_file(path) != _required_sha256(binding.get("sha256"), f"binding {label} sha256"):
        raise BroadcastApiError(f"bound broadcast source changed: {label}")


def _snapshot_binding(path: Path, output_dir: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if resolved.parent != output_dir:
        raise BroadcastApiError(f"broadcast source must be a direct run artifact: {path.name}")
    return {"path": path.name, "sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def _trusted_directory(path: Path, label: str) -> Path:
    raw = Path(path)
    if _is_link_or_reparse(raw):
        raise BroadcastApiError(f"{label} must not be a symlink or reparse point")
    resolved = raw.resolve()
    if not resolved.is_dir():
        raise BroadcastApiError(f"{label} must be an existing directory")
    return resolved


def _contained_nonlink_file(root: Path, path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(path))
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise BroadcastApiError(f"{label} must remain inside the trusted review root") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if _is_link_or_reparse(current):
            raise BroadcastApiError(f"{label} must not traverse a symlink or reparse point")
    if not current.is_file():
        raise BroadcastApiError(f"{label} is unavailable")
    return current


def _verify_bound_file(path: Path, *, expected_sha256: str, expected_size: int, label: str) -> None:
    before = path.stat()
    if before.st_size != expected_size:
        raise BroadcastApiError(f"{label} size changed")
    if sha256_file(path) != expected_sha256:
        raise BroadcastApiError(f"{label} hash changed")
    after = path.stat()
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if after_identity != before_identity:
        raise BroadcastApiError(f"{label} changed during validation")


def _safe_publish_target(path: Path, *, trusted_root: Path | None, label: str) -> Path:
    raw_path = Path(os.path.abspath(path))
    if raw_path.name in {"", ".", ".."}:
        raise BroadcastApiError(f"{label} target name is invalid")
    if _is_link_or_reparse(raw_path):
        raise BroadcastApiError(f"{label} target must not be a symlink or reparse point")
    raw_parent = raw_path.parent
    if trusted_root is None:
        raw_parent.mkdir(parents=True, exist_ok=True)
        if _is_link_or_reparse(raw_parent):
            raise BroadcastApiError(f"{label} parent must not be a symlink or reparse point")
        root = raw_parent.resolve()
    else:
        root = _trusted_directory(trusted_root, f"{label} trusted root")
        try:
            relative_parent = raw_parent.relative_to(root)
        except ValueError as exc:
            raise BroadcastApiError(f"{label} parent escapes its trusted root") from exc
        current = root
        for part in relative_parent.parts:
            current = current / part
            if _is_link_or_reparse(current):
                raise BroadcastApiError(f"{label} parent must not traverse a symlink or reparse point")
        raw_parent.mkdir(parents=True, exist_ok=True)
        current = root
        for part in relative_parent.parts:
            current = current / part
            if _is_link_or_reparse(current):
                raise BroadcastApiError(f"{label} parent must not traverse a symlink or reparse point")
        if raw_parent.resolve() != current:
            raise BroadcastApiError(f"{label} parent changed while it was prepared")
    if _is_link_or_reparse(raw_path):
        raise BroadcastApiError(f"{label} target must not be a symlink or reparse point")
    return root / raw_path.name if raw_parent.resolve() == root else raw_parent.resolve() / raw_path.name


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


def _required_nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BroadcastApiError(f"{name} must be a non-negative integer")
    return value


def _required_positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BroadcastApiError(f"{name} must be a positive integer")
    return value


def _required_positive_finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BroadcastApiError(f"{name} must be a positive finite number")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise BroadcastApiError(f"{name} must be a positive finite number")
    return parsed


def _numbers_close(value: Any, expected: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and math.isclose(float(value), expected, rel_tol=0.0, abs_tol=1e-9)
    )


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _required_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BroadcastApiError(f"{name} must be an object")
    return value


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BroadcastApiError(f"{name} must be a non-empty string")
    return value.strip()


def _required_sha256(value: Any, name: str) -> str:
    text = _required_text(value, name).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise BroadcastApiError(f"{name} must be a lowercase SHA-256")
    return text


def _required_timestamp(value: Any, name: str) -> str:
    text = _required_text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BroadcastApiError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BroadcastApiError(f"{name} must be an ISO-8601 timestamp with timezone")
    return text


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
