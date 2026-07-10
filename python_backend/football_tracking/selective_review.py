from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
from bisect import insort_right
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from football_tracking.candidate_annotations import (
    ADJUDICATION_QUEUE_NAME,
    ANNOTATION_RESOLUTION_NAME,
    resolve_candidate_annotations,
    sample_evidence_sha256,
)
from football_tracking.selective_policy import (
    SelectivePolicyError,
    validate_selective_decisions_binding,
    validate_selective_policy_evidence_binding,
)
from football_tracking.tracking_contracts import (
    CLASSIFICATION_LABELS,
    FRAME_STATUSES,
    TRACKING_CONTRACT_REPORT_NAME,
    build_tracking_contract,
)

REVIEW_SCHEMA_VERSION = "1.0"
REVIEW_QUEUE_NAME = "selective_review_queue.v1.json"
REVIEW_TIMING_NAME = "review_timing.v1.json"
MATERIALIZATION_REPORT_NAME = "selective_review_materialization.v1.json"
HUMAN_VOTES_NAME = "human_adjudication_votes.v1.jsonl"
TRAJECTORY_CORRECTIONS_NAME = "trajectory_corrections.v1.json"
ACTIVE_ROUND_NAME = "active_learning_round.v1.json"
SELECTIVE_DECISIONS_NAME = "selective_decisions.v1.json"
MAX_REVIEW_WINDOWS = 30
MIN_WINDOW_SECONDS = 5.0
MAX_WINDOW_SECONDS = 10.0
DEFAULT_WINDOW_SECONDS = 7.5
_FILE_READ_CHUNK_BYTES = 1024 * 1024
_MAX_JSON_ARTIFACT_BYTES = 256 * 1024 * 1024

_NOISE_LABELS = tuple(label for label in CLASSIFICATION_LABELS if label not in {"match_ball", "unknown"})
_ACTIONS = frozenset({"confirm_ball", "reject_noise", "mark_unknown", "correct_trajectory"})


class SelectiveReviewError(RuntimeError):
    """Raised when selective review cannot be materialized safely."""


@dataclass(frozen=True)
class _Snapshot:
    path: Path
    sha256: str
    size: int


def build_review_windows(
    candidates: list[dict[str, Any]],
    timings: dict[str, dict[str, Any]],
    *,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    max_windows: int = MAX_REVIEW_WINDOWS,
) -> list[dict[str, Any]]:
    duration = _finite_number(window_seconds, "window_seconds")
    if not MIN_WINDOW_SECONDS <= duration <= MAX_WINDOW_SECONDS:
        raise SelectiveReviewError("window_seconds must be between 5 and 10 seconds")
    if not isinstance(max_windows, int) or isinstance(max_windows, bool) or not 1 <= max_windows <= 30:
        raise SelectiveReviewError("max_windows must be an integer between 1 and 30")
    seen: set[str] = set()
    by_variant: dict[str, list[dict[str, Any]]] = {}
    for index, raw in enumerate(candidates):
        if not isinstance(raw, dict):
            raise SelectiveReviewError(f"review candidate {index} must be an object")
        candidate_id = _required_text(raw.get("candidate_id"), f"candidates[{index}].candidate_id")
        if candidate_id in seen:
            raise SelectiveReviewError(f"duplicate review candidate {candidate_id!r}")
        seen.add(candidate_id)
        variant_id = _required_text(raw.get("variant_id"), f"candidates[{index}].variant_id")
        frame_index = _nonnegative_int(raw.get("frame_index"), f"candidates[{index}].frame_index")
        review_kind = _required_text(raw.get("review_kind"), f"candidates[{index}].review_kind")
        priority = _nonnegative_int(raw.get("priority", _review_priority(review_kind)), "priority")
        by_variant.setdefault(variant_id, []).append(
            {
                **raw,
                "candidate_id": candidate_id,
                "variant_id": variant_id,
                "frame_index": frame_index,
                "review_kind": review_kind,
                "priority": priority,
            }
        )

    windows: list[dict[str, Any]] = []
    for variant_id in sorted(by_variant):
        timing = timings.get(variant_id)
        if not isinstance(timing, dict):
            raise SelectiveReviewError(f"missing timing for variant {variant_id!r}")
        fps = _positive_number(timing.get("fps"), f"timing {variant_id} fps")
        frame_count = _positive_int(timing.get("frame_count"), f"timing {variant_id} frame_count")
        source_duration = frame_count / fps
        source_candidates = sorted(
            by_variant[variant_id], key=lambda row: (row["frame_index"], row["priority"], row["candidate_id"])
        )
        for row in source_candidates:
            if row["frame_index"] >= frame_count:
                raise SelectiveReviewError(f"candidate {row['candidate_id']!r} is outside source frame_count")
        if source_duration < MIN_WINDOW_SECONDS:
            raise SelectiveReviewError(
                f"source {variant_id!r} is shorter than the required 5-second review window; "
                "provide a longer source or additional review context"
            )
        min_frames = max(1, math.ceil(MIN_WINDOW_SECONDS * fps))
        max_frames = math.floor(MAX_WINDOW_SECONDS * fps)
        if max_frames < min_frames:
            raise SelectiveReviewError(
                f"timing {variant_id!r} cannot represent a 5-10 second review window at {fps} fps"
            )
        target_frames = min(frame_count, max(min_frames, min(max_frames, round(duration * fps))))
        seeds: list[dict[str, Any]] = []
        for row in source_candidates:
            start, end = _shifted_interval(row["frame_index"], target_frames, frame_count)
            seeds.append({"start": start, "end": end, "candidates": [row]})
        current = seeds[0]
        max_frames = max(1, math.floor(MAX_WINDOW_SECONDS * fps + 1e-9))
        for seed in seeds[1:]:
            union_start = min(current["start"], seed["start"])
            union_end = max(current["end"], seed["end"])
            overlaps = seed["start"] <= current["end"] + 1
            if overlaps and union_end - union_start + 1 <= max_frames:
                current = {
                    "start": union_start,
                    "end": union_end,
                    "candidates": [*current["candidates"], *seed["candidates"]],
                }
            else:
                windows.append(_window(variant_id, current["start"], current["end"], fps, current["candidates"]))
                current = seed
        windows.append(_window(variant_id, current["start"], current["end"], fps, current["candidates"]))

    if len(windows) > max_windows:
        raise SelectiveReviewError(
            f"review queue generated {len(windows)} windows; hard limit is {max_windows} (maximum 30)"
        )
    return sorted(windows, key=lambda row: (row["variant_id"], row["start_frame"], row["review_item_id"]))


def _shifted_interval(center: int, length: int, frame_count: int) -> tuple[int, int]:
    start = center - length // 2
    start = max(0, min(start, frame_count - length))
    return start, start + length - 1


def _window(
    variant_id: str,
    start: int,
    end: int,
    fps: float,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    duration_seconds = (end - start + 1) / fps
    if not MIN_WINDOW_SECONDS <= duration_seconds <= MAX_WINDOW_SECONDS:
        raise SelectiveReviewError("review window frame rounding produced a duration outside 5-10 seconds")
    candidate_rows = sorted(
        ({key: value for key, value in candidate.items() if key not in {"priority"}} for candidate in candidates),
        key=lambda row: (row["frame_index"], row["candidate_id"]),
    )
    identity = {
        "variant_id": variant_id,
        "start_frame": start,
        "end_frame": end,
        "candidate_ids": [row["candidate_id"] for row in candidate_rows],
    }
    return {
        "review_item_id": f"review-{_canonical_sha256(identity)[:20]}",
        "variant_id": variant_id,
        "start_frame": start,
        "end_frame": end,
        "duration_seconds": duration_seconds,
        "compliance": "compliant",
        "priority": min(candidate.get("priority", 99) for candidate in candidates),
        "candidates": candidate_rows,
    }


def _review_priority(kind: str) -> int:
    return {"uncertainty": 0, "conflict": 1, "audit_reject": 2, "audit_accept": 3}.get(kind, 4)


def _ordered_audit_candidates(
    audit_groups: dict[tuple[str, str], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Return the stable decision/variant round-robin order without repeated scans."""

    group_keys = sorted(audit_groups)
    round_count = max((len(audit_groups[key]) for key in group_keys), default=0)
    return [
        audit_groups[key][round_index]
        for round_index in range(round_count)
        for key in group_keys
        if round_index < len(audit_groups[key])
    ]


def _candidate_window_interval(
    candidate: dict[str, Any],
    timings: dict[str, dict[str, Any]],
    *,
    window_seconds: float,
) -> tuple[int, int, int]:
    """Build the same seed interval as ``build_review_windows`` for incremental budgeting."""

    variant_id = candidate["variant_id"]
    timing = timings[variant_id]
    fps = _positive_number(timing.get("fps"), f"timing {variant_id} fps")
    frame_count = _positive_int(timing.get("frame_count"), f"timing {variant_id} frame_count")
    min_frames = max(1, math.ceil(MIN_WINDOW_SECONDS * fps))
    max_frames = math.floor(MAX_WINDOW_SECONDS * fps)
    target_frames = min(frame_count, max(min_frames, min(max_frames, round(window_seconds * fps))))
    start, end = _shifted_interval(candidate["frame_index"], target_frames, frame_count)
    return start, end, max(1, math.floor(MAX_WINDOW_SECONDS * fps + 1e-9))


def _merge_window_intervals(intervals: list[tuple[int, int]], max_frames: int) -> list[tuple[int, int]]:
    """Merge compact window footprints with the production window span rule."""

    return _merge_sorted_window_intervals(sorted(intervals), max_frames)


def _merge_sorted_window_intervals(ordered: list[tuple[int, int]], max_frames: int) -> list[tuple[int, int]]:
    """Merge seed intervals that are already in production window order."""

    if not ordered:
        return []
    merged: list[tuple[int, int]] = []
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        union_start = min(current_start, start)
        union_end = max(current_end, end)
        if start <= current_end + 1 and union_end - union_start + 1 <= max_frames:
            current_start, current_end = union_start, union_end
        else:
            merged.append((current_start, current_end))
            current_start, current_end = start, end
    merged.append((current_start, current_end))
    return merged


def _interval_overlaps_footprints(
    start: int,
    end: int,
    footprints: list[tuple[int, int]],
) -> bool:
    return any(
        start <= footprint_end + 1 and end + 1 >= footprint_start for footprint_start, footprint_end in footprints
    )


def _select_review_candidates(
    candidates: list[dict[str, Any]],
    timings: dict[str, dict[str, Any]],
    *,
    window_seconds: float,
    max_windows: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    mandatory = [row for row in candidates if row["review_kind"] in {"conflict", "uncertainty"}]
    try:
        mandatory_windows = build_review_windows(
            mandatory,
            timings,
            window_seconds=window_seconds,
            max_windows=max_windows,
        )
    except SelectiveReviewError as exc:
        if "hard limit" not in str(exc):
            raise
        raise SelectiveReviewError(
            f"uncertainty/conflict review windows exceed {max_windows}; narrow the input scope before review"
        ) from exc

    audit_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in candidates:
        if row["review_kind"] not in {"audit_accept", "audit_reject"}:
            continue
        key = (row["selective_decision"], row["variant_id"])
        audit_groups.setdefault(key, []).append(row)
    for rows in audit_groups.values():
        rows.sort(
            key=lambda row: _canonical_sha256(
                {
                    "sampling_policy": "stable-decision-variant-audit-v1",
                    "candidate_id": row["candidate_id"],
                    "candidate_fingerprint": row["candidate_fingerprint"],
                }
            )
        )
    ordered_audits = _ordered_audit_candidates(audit_groups)
    full_population = [*mandatory, *ordered_audits]
    try:
        windows = build_review_windows(
            full_population,
            timings,
            window_seconds=window_seconds,
            max_windows=max_windows,
        )
    except SelectiveReviewError as exc:
        if "hard limit" not in str(exc):
            raise
    else:
        selection = _selection_report(
            candidates,
            full_population,
            max_windows=max_windows,
            mandatory_window_count=len(mandatory_windows),
        )
        return full_population, windows, selection

    interval_state: dict[str, list[tuple[int, int]]] = {}
    for window in mandatory_windows:
        interval_state.setdefault(window["variant_id"], []).append((window["start_frame"], window["end_frame"]))
    window_count = len(mandatory_windows)

    def try_add(candidate: dict[str, Any]) -> bool:
        nonlocal window_count
        variant_id = candidate["variant_id"]
        start, end, max_frames = _candidate_window_interval(
            candidate,
            timings,
            window_seconds=window_seconds,
        )
        current = interval_state.get(variant_id, [])
        updated = _merge_window_intervals([*current, (start, end)], max_frames)
        updated_count = window_count - len(current) + len(updated)
        if updated_count > max_windows:
            return False
        interval_state[variant_id] = updated
        window_count = updated_count
        return True

    selected_audits: list[dict[str, Any]] = []
    rejected_audits: list[dict[str, Any]] = []
    for candidate in ordered_audits:
        if try_add(candidate):
            selected_audits.append(candidate)
        else:
            rejected_audits.append(candidate)
    while rejected_audits:
        still_rejected: list[dict[str, Any]] = []
        accepted_any = False
        for candidate in rejected_audits:
            if not try_add(candidate):
                still_rejected.append(candidate)
                continue
            selected_audits.append(candidate)
            accepted_any = True
        rejected_audits = still_rejected
        if not accepted_any:
            break
    selected = [*mandatory, *selected_audits]
    seed_state: dict[str, list[tuple[int, int]]] = {}
    max_frames_by_variant: dict[str, int] = {}
    for candidate in selected:
        start, end, max_frames = _candidate_window_interval(
            candidate,
            timings,
            window_seconds=window_seconds,
        )
        seed_state.setdefault(candidate["variant_id"], []).append((start, end))
        max_frames_by_variant[candidate["variant_id"]] = max_frames
    for intervals in seed_state.values():
        intervals.sort()
    exact_footprints = {
        variant_id: _merge_sorted_window_intervals(
            intervals,
            max_frames_by_variant[variant_id],
        )
        for variant_id, intervals in seed_state.items()
    }
    exact_window_count = sum(len(intervals) for intervals in exact_footprints.values())
    if exact_window_count > max_windows:
        raise SelectiveReviewError("incremental review window budget diverged from exact seed partitioning")

    remaining = list(rejected_audits)
    while remaining:
        still_rejected: list[dict[str, Any]] = []
        accepted_any = False
        for candidate in remaining:
            variant_id = candidate["variant_id"]
            start, end, max_frames = _candidate_window_interval(
                candidate,
                timings,
                window_seconds=window_seconds,
            )
            current_footprints = exact_footprints.get(variant_id, [])
            if exact_window_count >= max_windows and not _interval_overlaps_footprints(start, end, current_footprints):
                still_rejected.append(candidate)
                continue
            current_seeds = seed_state.get(variant_id, [])
            trial_seeds = list(current_seeds)
            insort_right(trial_seeds, (start, end))
            trial_footprints = _merge_sorted_window_intervals(trial_seeds, max_frames)
            trial_window_count = exact_window_count - len(current_footprints) + len(trial_footprints)
            if trial_window_count > max_windows:
                still_rejected.append(candidate)
                continue
            seed_state[variant_id] = trial_seeds
            max_frames_by_variant[variant_id] = max_frames
            exact_footprints[variant_id] = trial_footprints
            exact_window_count = trial_window_count
            selected.append(candidate)
            selected_audits.append(candidate)
            accepted_any = True
        remaining = still_rejected
        if not accepted_any:
            break

    windows = build_review_windows(
        selected,
        timings,
        window_seconds=window_seconds,
        max_windows=max_windows,
    )
    if len(windows) != exact_window_count:
        raise SelectiveReviewError("exact review window budget diverged from production window construction")

    selection = _selection_report(
        candidates,
        selected,
        max_windows=max_windows,
        mandatory_window_count=len(mandatory_windows),
    )
    return selected, windows, selection


def _selection_report(
    eligible: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    *,
    max_windows: int,
    mandatory_window_count: int,
) -> dict[str, Any]:
    selected_ids = {row["candidate_id"] for row in selected}

    def grouped(fields: tuple[str, ...]) -> list[dict[str, Any]]:
        keys = sorted({tuple(row[field] for field in fields) for row in eligible})
        result = []
        for key in keys:
            rows = [row for row in eligible if tuple(row[field] for field in fields) == key]
            selected_count = sum(row["candidate_id"] in selected_ids for row in rows)
            result.append(
                {
                    **dict(zip(fields, key)),
                    "eligible": len(rows),
                    "selected": selected_count,
                    "dropped": len(rows) - selected_count,
                }
            )
        return result

    dropped_ids = sorted(row["candidate_id"] for row in eligible if row["candidate_id"] not in selected_ids)
    decision_variant = grouped(("selective_decision", "variant_id"))
    for row in decision_variant:
        row["decision"] = row.pop("selective_decision")
    return {
        "policy": "mandatory-uncertainty-conflict-plus-stable-decision-variant-audit-v1",
        "window_budget": max_windows,
        "mandatory_window_count": mandatory_window_count,
        "counts": {
            "eligible": len(eligible),
            "selected": len(selected),
            "dropped": len(dropped_ids),
        },
        "by_kind": grouped(("review_kind",)),
        "by_variant": grouped(("variant_id",)),
        "by_decision_variant": decision_variant,
        "dropped_candidate_ids": dropped_ids,
        "coverage_complete": not dropped_ids,
        "requires_additional_round": bool(dropped_ids),
    }


def build_selective_review_queue(
    dataset_manifest_path: Path,
    predictions_path: Path,
    policy_path: Path,
    model_manifest_path: Path,
    contract_path: Path,
    output_dir: Path,
    *,
    decisions_path: Path | None = None,
    annotation_resolution_path: Path | None = None,
    resolved_contract_path: Path | None = None,
    policy_roles_path: Path | None = None,
    fps_overrides: dict[str, float] | None = None,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    max_windows: int = MAX_REVIEW_WINDOWS,
) -> dict[str, Any]:
    """Build a hash-bound human review queue in a new atomically published directory."""

    output_dir = Path(output_dir).resolve()
    _require_new_output_dir(output_dir)
    bundle = _load_source_bundle(
        dataset_manifest_path,
        predictions_path,
        policy_path,
        model_manifest_path,
        contract_path,
        decisions_path=decisions_path,
        annotation_resolution_path=annotation_resolution_path,
        resolved_contract_path=resolved_contract_path,
        policy_roles_path=policy_roles_path,
        fps_overrides=fps_overrides,
    )
    _, windows, selection = _select_review_candidates(
        bundle["review_candidates"],
        bundle["timings"],
        window_seconds=window_seconds,
        max_windows=max_windows,
    )
    timing_report = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "artifact_type": "selective_review_timing",
        "generated_at": _utc_now_iso(),
        "variants": [bundle["timings"][variant_id] for variant_id in sorted(bundle["timings"])],
    }
    _validate_finite_json(timing_report, "review timing")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent))
    try:
        timing_path = staging_dir / REVIEW_TIMING_NAME
        _write_json(timing_path, timing_report)
        timing_sha256 = _sha256_file(timing_path)
        queue = {
            "schema_version": REVIEW_SCHEMA_VERSION,
            "artifact_type": "selective_review_queue",
            "generated_at": _utc_now_iso(),
            "window_seconds": float(window_seconds),
            "max_windows": max_windows,
            "review_item_count": len(windows),
            "candidate_count": sum(len(item["candidates"]) for item in windows),
            "selection": selection,
            "bindings": {
                "review_timing": {
                    "path": REVIEW_TIMING_NAME,
                    "sha256": timing_sha256,
                },
                **bundle["bindings"],
            },
            "items": windows,
        }
        _validate_finite_json(queue, "selective review queue")
        _write_json(staging_dir / REVIEW_QUEUE_NAME, queue)
        _verify_snapshots(bundle["snapshots"], context="selective review queue build")
        _require_new_output_dir(output_dir)
        os.replace(staging_dir, output_dir)
        return queue
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def materialize_selective_review_actions(
    queue_path: Path,
    actions_path: Path,
    dataset_manifest_path: Path,
    predictions_path: Path,
    policy_path: Path,
    model_manifest_path: Path,
    contract_path: Path,
    output_dir: Path,
    *,
    decisions_path: Path | None = None,
    annotation_resolution_path: Path | None = None,
    resolved_contract_path: Path | None = None,
    policy_roles_path: Path | None = None,
) -> dict[str, Any]:
    """Validate review actions and atomically publish annotation/correction artifacts."""

    output_dir = Path(output_dir).resolve()
    _require_new_output_dir(output_dir)
    queue, queue_snapshot = _load_json_snapshot(queue_path, "selective review queue")
    actions_report, actions_snapshot = _load_json_snapshot(actions_path, "selective review actions")
    _require_envelope(queue, artifact_type="selective_review_queue", name="selective review queue")
    _require_envelope(actions_report, artifact_type="selective_review_actions", name="selective review actions")

    queue_dir = queue_snapshot.path.parent
    timing_path = queue_dir / REVIEW_TIMING_NAME
    timing, timing_snapshot = _load_json_snapshot(timing_path, "review timing")
    _require_envelope(timing, artifact_type="selective_review_timing", name="review timing")
    queue_bindings = _required_object(queue.get("bindings"), "queue.bindings")
    expected_binding_names = {
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
    if set(queue_bindings) != expected_binding_names:
        raise SelectiveReviewError("queue binding keys do not match the selective review contract")
    timing_binding = _required_object(queue_bindings.get("review_timing"), "queue.bindings.review_timing")
    expected_timing_binding = {"path": REVIEW_TIMING_NAME, "sha256": timing_snapshot.sha256}
    if timing_binding != expected_timing_binding:
        raise SelectiveReviewError("queue review_timing binding does not match the timing artifact")
    timing_map = _parse_timing_report(timing)
    overrides = {
        variant_id: row["fps"] for variant_id, row in timing_map.items() if row["fps_source"] == "explicit_override"
    }

    decisions_binding = _required_object(queue_bindings.get("decisions"), "queue.bindings.decisions")
    decision_source = _required_text(decisions_binding.get("source"), "queue.bindings.decisions.source")
    if decision_source != "independent_artifact" or decisions_path is None:
        raise SelectiveReviewError("independent selective decisions input is required")

    bundle = _load_source_bundle(
        dataset_manifest_path,
        predictions_path,
        policy_path,
        model_manifest_path,
        contract_path,
        decisions_path=decisions_path,
        annotation_resolution_path=annotation_resolution_path,
        resolved_contract_path=resolved_contract_path,
        policy_roles_path=policy_roles_path,
        fps_overrides=overrides,
    )
    for name, descriptor in bundle["bindings"].items():
        expected = _required_object(queue_bindings.get(name), f"queue.bindings.{name}")
        if expected != descriptor:
            raise SelectiveReviewError(f"queue {name} binding does not match the supplied input snapshot")
    if timing_map != bundle["timings"]:
        raise SelectiveReviewError("review timing does not match the bound video metadata")

    window_seconds = _finite_number(queue.get("window_seconds"), "queue.window_seconds")
    max_windows = _positive_int(queue.get("max_windows"), "queue.max_windows")
    _, expected_items, expected_selection = _select_review_candidates(
        bundle["review_candidates"],
        bundle["timings"],
        window_seconds=window_seconds,
        max_windows=max_windows,
    )
    if queue.get("selection") != expected_selection:
        raise SelectiveReviewError("queue selection report does not match the bound candidate population")
    if queue.get("items") != expected_items:
        raise SelectiveReviewError("queue items do not match the bound candidates, decisions, and timing")
    if queue.get("review_item_count") != len(expected_items):
        raise SelectiveReviewError("queue review_item_count does not match items")
    expected_candidate_count = sum(len(item["candidates"]) for item in expected_items)
    if queue.get("candidate_count") != expected_candidate_count:
        raise SelectiveReviewError("queue candidate_count does not match items")

    raw_actions = actions_report.get("actions")
    if not isinstance(raw_actions, list):
        raise SelectiveReviewError("selective review actions.actions must be a list")
    actions = deduplicate_actions(raw_actions)
    normalized_actions = _validate_actions(
        actions,
        queue=queue,
        queue_sha256=queue_snapshot.sha256,
        timings=timing_map,
    )
    votes = [row["vote"] for row in normalized_actions if row["vote"] is not None]
    corrections = [row["correction"] for row in normalized_actions if row["correction"] is not None]

    ledger_header = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "record_type": "ledger_header",
        "contract_sha256": bundle["bindings"]["contract"]["sha256"],
        "dataset_version": bundle["dataset_version"],
        "evidence_manifest_sha256": bundle["bindings"]["dataset"]["sha256"],
        "source": "selective_review",
    }
    corrections_report = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "artifact_type": "trajectory_corrections",
        "generated_at": _utc_now_iso(),
        "queue_sha256": queue_snapshot.sha256,
        "correction_count": len(corrections),
        "corrections": corrections,
    }
    _validate_finite_json(corrections_report, "trajectory corrections")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent))
    try:
        votes_path = staging_dir / HUMAN_VOTES_NAME
        _write_jsonl(votes_path, [ledger_header, *votes])
        corrections_path = staging_dir / TRAJECTORY_CORRECTIONS_NAME
        _write_json(corrections_path, corrections_report)
        annotations_dir = staging_dir / "annotations"
        try:
            annotation_report = resolve_candidate_annotations(
                bundle["contract_snapshot"].path,
                votes_path,
                annotations_dir,
                dataset_manifest_path=bundle["dataset_snapshot"].path,
            )
        except (OSError, ValueError) as exc:
            raise SelectiveReviewError(f"could not derive annotations: {exc}") from exc

        annotation_contract_path = annotations_dir / TRACKING_CONTRACT_REPORT_NAME
        round_identity = {
            "queue_sha256": queue_snapshot.sha256,
            "actions_sha256": actions_snapshot.sha256,
            "action_ids": [row["action_id"] for row in normalized_actions],
        }
        round_report = {
            "schema_version": REVIEW_SCHEMA_VERSION,
            "artifact_type": "active_learning_round",
            "generated_at": _utc_now_iso(),
            "round_id": f"round-{_canonical_sha256(round_identity)[:20]}",
            "status": "materialized",
            "training_invoked": False,
            "bindings": {
                **queue_bindings,
                "queue": {"path": str(queue_snapshot.path), "sha256": queue_snapshot.sha256},
                "actions": {"path": str(actions_snapshot.path), "sha256": actions_snapshot.sha256},
            },
            "summary": {
                "action_count": len(normalized_actions),
                "vote_count": len(votes),
                "trajectory_correction_count": len(corrections),
            },
            "artifacts": {
                "human_votes": {"path": HUMAN_VOTES_NAME, "sha256": _sha256_file(votes_path)},
                "trajectory_corrections": {
                    "path": TRAJECTORY_CORRECTIONS_NAME,
                    "sha256": _sha256_file(corrections_path),
                },
                "annotation_resolution": {
                    "path": f"annotations/{ANNOTATION_RESOLUTION_NAME}",
                    "sha256": _sha256_file(annotations_dir / ANNOTATION_RESOLUTION_NAME),
                },
                "annotation_adjudication_queue": {
                    "path": f"annotations/{ADJUDICATION_QUEUE_NAME}",
                    "sha256": _sha256_file(annotations_dir / ADJUDICATION_QUEUE_NAME),
                },
                "derived_annotations_contract": {
                    "path": f"annotations/{TRACKING_CONTRACT_REPORT_NAME}",
                    "sha256": _sha256_file(annotation_contract_path),
                },
            },
        }
        _validate_finite_json(round_report, "active learning round")
        round_path = staging_dir / ACTIVE_ROUND_NAME
        _write_json(round_path, round_report)
        report = {
            "schema_version": REVIEW_SCHEMA_VERSION,
            "artifact_type": "selective_review_materialization",
            "generated_at": _utc_now_iso(),
            "status": "complete",
            "training_invoked": False,
            "round_id": round_report["round_id"],
            "bindings": round_report["bindings"],
            "summary": round_report["summary"],
            "annotation_summary": annotation_report["summary"],
            "artifacts": {
                **round_report["artifacts"],
                "active_learning_round": {
                    "path": ACTIVE_ROUND_NAME,
                    "sha256": _sha256_file(round_path),
                },
            },
        }
        _validate_finite_json(report, "selective review materialization")
        _write_json(staging_dir / MATERIALIZATION_REPORT_NAME, report)
        _verify_snapshots(
            [queue_snapshot, actions_snapshot, timing_snapshot, *bundle["snapshots"]],
            context="selective review materialization",
        )
        _require_new_output_dir(output_dir)
        os.replace(staging_dir, output_dir)
        return report
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def deduplicate_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse exact action retries and reject reuse of an action ID with another payload."""

    if not isinstance(actions, list):
        raise SelectiveReviewError("actions must be a list")
    seen: dict[str, tuple[str, dict[str, Any]]] = {}
    result: list[dict[str, Any]] = []
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise SelectiveReviewError(f"actions[{index}] must be an object")
        action_id = _required_text(action.get("action_id"), f"actions[{index}].action_id")
        encoded = _canonical_json(action, f"actions[{index}]")
        previous = seen.get(action_id)
        if previous is None:
            copied = dict(action)
            seen[action_id] = (encoded, copied)
            result.append(copied)
        elif previous[0] != encoded:
            raise SelectiveReviewError(f"action_id {action_id!r} has a conflicting payload")
    return result


def _load_source_bundle(
    dataset_manifest_path: Path,
    predictions_path: Path,
    policy_path: Path,
    model_manifest_path: Path,
    contract_path: Path,
    *,
    decisions_path: Path | None,
    annotation_resolution_path: Path | None,
    resolved_contract_path: Path | None,
    policy_roles_path: Path | None,
    fps_overrides: dict[str, float] | None,
) -> dict[str, Any]:
    dataset, dataset_snapshot = _load_json_snapshot(dataset_manifest_path, "candidate dataset manifest")
    predictions, predictions_snapshot = _load_json_snapshot(predictions_path, "candidate predictions")
    policy, policy_snapshot = _load_json_snapshot(policy_path, "selective policy")
    model, model_snapshot = _load_json_snapshot(model_manifest_path, "model manifest")
    contract, contract_snapshot = _load_json_snapshot(contract_path, "tracking contract")
    if decisions_path is None:
        raise SelectiveReviewError("independent selective decisions input is required")
    missing_evidence = [
        name
        for name, path in (
            ("annotation resolution", annotation_resolution_path),
            ("resolved tracking contract", resolved_contract_path),
            ("selective policy roles", policy_roles_path),
        )
        if path is None
    ]
    if missing_evidence:
        raise SelectiveReviewError(
            f"strict selective policy qualification evidence is required: {', '.join(missing_evidence)}"
        )
    decisions, decisions_snapshot = _load_json_snapshot(decisions_path, "selective decisions")

    _require_envelope(dataset, artifact_type="candidate_dataset", name="candidate dataset manifest")
    _require_envelope(predictions, artifact_type="candidate_predictions", name="candidate predictions")
    _require_envelope(policy, artifact_type="selective_policy", name="selective policy")
    _require_envelope(model, artifact_type="candidate_classifier_model", name="model manifest")
    _require_envelope(decisions, artifact_type="selective_decisions", name="selective decisions")
    try:
        validated_decisions = validate_selective_decisions_binding(policy_snapshot.path, decisions_snapshot.path)
    except (OSError, ValueError, SelectivePolicyError) as exc:
        raise SelectiveReviewError(f"selective policy/decisions binding is invalid: {exc}") from exc
    if validated_decisions != decisions:
        raise SelectiveReviewError("selective decisions changed during strict policy binding validation")
    _validate_policy_decisions_contract(
        policy,
        decisions,
        policy_snapshot=policy_snapshot,
        decisions_snapshot=decisions_snapshot,
    )
    _, training_report_snapshot, weights_snapshot = _load_model_package(model, model_snapshot)
    try:
        evidence_validation = validate_selective_policy_evidence_binding(
            policy_snapshot.path,
            decisions_snapshot.path,
            predictions_snapshot.path,
            dataset_snapshot.path,
            annotation_resolution_path,
            resolved_contract_path,
            model_snapshot.path,
            policy_roles_path,
        )
    except (OSError, ValueError, SelectivePolicyError) as exc:
        raise SelectiveReviewError(f"selective policy qualification evidence is invalid: {exc}") from exc
    if evidence_validation.get("policy") != policy or evidence_validation.get("decisions") != decisions:
        raise SelectiveReviewError("selective policy or decisions changed during qualification evidence validation")
    evidence_bindings = _required_object(evidence_validation.get("bindings"), "qualification evidence bindings")
    evidence_paths = {
        "annotation_resolution": Path(annotation_resolution_path),
        "resolved_tracking_contract": Path(resolved_contract_path),
        "policy_roles": Path(policy_roles_path),
    }
    qualification_snapshots: dict[str, _Snapshot] = {}
    for name, path in evidence_paths.items():
        snapshot = _snapshot(path, name.replace("_", " "))
        expected = _required_object(evidence_bindings.get(name), f"qualification evidence bindings.{name}")
        if expected != {"path": snapshot.path.name, "sha256": snapshot.sha256}:
            raise SelectiveReviewError(f"{name.replace('_', ' ')} binding changed after strict validation")
        qualification_snapshots[name] = snapshot
    if contract.get("schema_version") != "2.0":
        raise SelectiveReviewError("tracking contract must use schema version 2.0")
    if contract.get("validation_errors") != []:
        raise SelectiveReviewError("tracking contract must be valid before review")
    collections: dict[str, list[dict[str, Any]]] = {}
    for collection_name in ("frames", "candidates", "classifications", "decisions"):
        collection = contract.get(collection_name)
        if not isinstance(collection, list):
            raise SelectiveReviewError(f"tracking contract {collection_name} must be a list")
        collections[collection_name] = collection
    normalized_contract = build_tracking_contract(**collections)
    if normalized_contract["validation_errors"] or any(
        normalized_contract[name] != contract[name] for name in collections
    ):
        raise SelectiveReviewError("tracking contract fails canonical V2 validation")

    dataset_version = _required_text(dataset.get("dataset_version"), "dataset.dataset_version")
    model_version = _required_text(model.get("model_version"), "model.model_version")
    policy_version = _required_text(policy.get("policy_version"), "policy.policy_version")
    for value, name, expected in (
        (predictions.get("dataset_version"), "predictions.dataset_version", dataset_version),
        (predictions.get("model_version"), "predictions.model_version", model_version),
        (policy.get("dataset_version", dataset_version), "policy.dataset_version", dataset_version),
        (policy.get("model_version", model_version), "policy.model_version", model_version),
        (decisions.get("policy_version"), "decisions.policy_version", policy_version),
        (decisions.get("dataset_version", dataset_version), "decisions.dataset_version", dataset_version),
        (decisions.get("model_version", model_version), "decisions.model_version", model_version),
    ):
        if value != expected:
            raise SelectiveReviewError(f"{name} does not match {expected!r}")
    contract_descriptor = _required_object(dataset.get("contract"), "dataset.contract")
    if contract_descriptor.get("sha256") != contract_snapshot.sha256:
        raise SelectiveReviewError("dataset contract binding does not match the supplied tracking contract")
    if predictions.get("source_contract_sha256") != contract_snapshot.sha256:
        raise SelectiveReviewError("predictions contract binding does not match the supplied tracking contract")
    _validate_policy_lineage(
        policy,
        decisions,
        predictions_sha256=predictions_snapshot.sha256,
        dataset_sha256=dataset_snapshot.sha256,
        model_sha256=model_snapshot.sha256,
        training_report_sha256=training_report_snapshot.sha256,
        model_weights_sha256=weights_snapshot.sha256,
        contract_sha256=contract_snapshot.sha256,
    )

    candidates = _unique_rows(
        normalized_contract.get("candidates"),
        "candidate_id",
        "contract.candidates",
    )
    if not candidates:
        raise SelectiveReviewError("tracking contract must contain candidates")
    predictions_by_id = _unique_rows(predictions.get("predictions"), "candidate_id", "predictions.predictions")
    decisions_by_id = _unique_rows(decisions.get("decisions"), "candidate_id", "decisions.decisions")
    samples_by_id = _unique_rows(dataset.get("samples"), "candidate_id", "dataset.samples")
    candidate_ids = set(candidates)
    for rows, name in (
        (predictions_by_id, "predictions"),
        (decisions_by_id, "decisions"),
        (samples_by_id, "dataset samples"),
    ):
        if set(rows) != candidate_ids:
            raise SelectiveReviewError(f"{name} candidate IDs do not match the tracking contract")
    if predictions.get("prediction_count") != len(predictions_by_id):
        raise SelectiveReviewError("predictions prediction_count does not match predictions")
    dataset_summary = _required_object(dataset.get("summary"), "dataset.summary")
    if dataset_summary.get("status") != "ok":
        raise SelectiveReviewError("candidate dataset status must be ok")
    if dataset_summary.get("sample_count") != len(samples_by_id):
        raise SelectiveReviewError("dataset sample_count does not match samples")
    if dataset_summary.get("source_count") != len(dataset.get("sources", [])):
        raise SelectiveReviewError("dataset source_count does not match sources")
    if dataset.get("frame_offsets") != [-2, -1, 0, 1, 2]:
        raise SelectiveReviewError("candidate dataset must bind frame offsets [-2,-1,0,1,2]")
    expected_tensor_contract = {
        "color_space": "RGB",
        "dtype": "uint8",
        "tight_shape": [5, 3, 64, 64],
        "context_shape": [5, 3, 128, 128],
        "markup": False,
    }
    if dataset.get("tensor_contract") != expected_tensor_contract:
        raise SelectiveReviewError("candidate dataset tensor contract is incompatible with review evidence")
    sample_ids: set[str] = set()
    for candidate_id, sample in samples_by_id.items():
        sample_id = _required_text(sample.get("sample_id"), f"sample {candidate_id} sample_id")
        if sample_id in sample_ids:
            raise SelectiveReviewError(f"duplicate dataset sample_id {sample_id!r}")
        sample_ids.add(sample_id)

    snapshots = [
        dataset_snapshot,
        predictions_snapshot,
        policy_snapshot,
        model_snapshot,
        training_report_snapshot,
        weights_snapshot,
        contract_snapshot,
        decisions_snapshot,
        *qualification_snapshots.values(),
    ]
    sources = dataset.get("sources")
    if not isinstance(sources, list) or not sources:
        raise SelectiveReviewError("dataset.sources must be a non-empty list")
    source_by_variant: dict[str, dict[str, Any]] = {}
    variant_by_candidate: dict[str, str] = {}
    timing_by_variant: dict[str, dict[str, Any]] = {}
    overrides = dict(fps_overrides or {})
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise SelectiveReviewError(f"dataset.sources[{index}] must be an object")
        variant_id = _required_text(source.get("variant_id"), f"dataset.sources[{index}].variant_id")
        if variant_id in source_by_variant:
            raise SelectiveReviewError(f"duplicate dataset source variant_id {variant_id!r}")
        source_by_variant[variant_id] = source
        width = _positive_int(source.get("width"), f"source {variant_id} width")
        height = _positive_int(source.get("height"), f"source {variant_id} height")
        frame_count = _positive_int(source.get("frame_count"), f"source {variant_id} frame_count")
        candidate_id_values = source.get("candidate_ids")
        if not isinstance(candidate_id_values, list):
            raise SelectiveReviewError(f"source {variant_id} candidate_ids must be a list")
        for candidate_id_value in candidate_id_values:
            candidate_id = _required_text(candidate_id_value, f"source {variant_id} candidate_id")
            if candidate_id not in candidates:
                raise SelectiveReviewError(f"source {variant_id} references absent candidate {candidate_id!r}")
            if candidate_id in variant_by_candidate:
                raise SelectiveReviewError(f"candidate {candidate_id!r} appears in multiple video sources")
            variant_by_candidate[candidate_id] = variant_id

        if not any(
            decisions_by_id[candidate_id].get("decision_scope") == "application" for candidate_id in candidate_id_values
        ):
            continue

        video_path = _referenced_input_path(
            dataset_snapshot.path.parent,
            source.get("path"),
            f"source {variant_id} video",
        )
        video_snapshot = _snapshot(video_path, f"source {variant_id} video")
        expected_video_sha = _required_sha256(source.get("sha256"), f"source {variant_id} sha256")
        if video_snapshot.sha256 != expected_video_sha:
            raise SelectiveReviewError(f"source {variant_id} video sha256 mismatch")
        snapshots.append(video_snapshot)
        if variant_id in overrides:
            fps = _positive_number(overrides.pop(variant_id), f"fps override {variant_id}")
            fps_source = "explicit_override"
        elif source.get("fps") is not None:
            fps = _positive_number(source.get("fps"), f"source {variant_id} fps")
            fps_source = "dataset_manifest"
        else:
            raise SelectiveReviewError(f"source {variant_id!r} is missing fps; an explicit fps override is required")
        timing_by_variant[variant_id] = {
            "variant_id": variant_id,
            "fps": fps,
            "fps_source": fps_source,
            "frame_count": frame_count,
            "width": width,
            "height": height,
            "video": {
                "path": str(source.get("path")),
                "sha256": video_snapshot.sha256,
            },
        }
    if overrides:
        raise SelectiveReviewError(f"fps overrides reference unknown variants: {sorted(overrides)}")
    if set(variant_by_candidate) != candidate_ids:
        raise SelectiveReviewError("dataset source candidate IDs do not cover the tracking contract")

    review_candidates: list[dict[str, Any]] = []
    for candidate_id in sorted(candidate_ids):
        candidate = candidates[candidate_id]
        sample = samples_by_id[candidate_id]
        prediction = predictions_by_id[candidate_id]
        decision = decisions_by_id[candidate_id]
        reasons = _decision_reasons(decision)
        if not _decision_is_queue_eligible(decision, candidate_id=candidate_id, reasons=reasons):
            continue
        variant_id = variant_by_candidate[candidate_id]
        timing = timing_by_variant.get(variant_id)
        if timing is None:
            raise SelectiveReviewError(f"application candidate {candidate_id!r} has no reviewable source timing")
        frame_index = _nonnegative_int(candidate.get("frame_index"), f"candidate {candidate_id} frame_index")
        if frame_index >= timing["frame_count"]:
            raise SelectiveReviewError(f"candidate {candidate_id!r} lies outside its source video")
        fingerprint = _candidate_fingerprint(candidate)
        if prediction.get("candidate_fingerprint") != fingerprint:
            raise SelectiveReviewError(f"prediction candidate fingerprint mismatch for {candidate_id!r}")
        if "candidate_fingerprint" in decision and decision.get("candidate_fingerprint") != fingerprint:
            raise SelectiveReviewError(f"decision candidate fingerprint mismatch for {candidate_id!r}")
        if prediction.get("model_version") != model_version:
            raise SelectiveReviewError(f"prediction model version mismatch for {candidate_id!r}")
        predicted_label = prediction.get("predicted_label")
        if predicted_label not in CLASSIFICATION_LABELS:
            raise SelectiveReviewError(f"prediction label is invalid for {candidate_id!r}")
        prediction_confidence = _finite_number(
            prediction.get("confidence"),
            f"prediction confidence for {candidate_id}",
        )
        if not 0.0 <= prediction_confidence <= 1.0:
            raise SelectiveReviewError(f"prediction confidence is invalid for {candidate_id!r}")
        if "variant_id" in decision and decision.get("variant_id") != variant_id:
            raise SelectiveReviewError(f"decision variant mismatch for {candidate_id!r}")
        if "frame_index" in decision and decision.get("frame_index") != frame_index:
            raise SelectiveReviewError(f"decision frame mismatch for {candidate_id!r}")
        if sample.get("variant_id") != variant_id or sample.get("frame_index") != frame_index:
            raise SelectiveReviewError(f"dataset sample lineage mismatch for {candidate_id!r}")
        if sample.get("bbox_requested_pixels") != candidate.get("bbox"):
            raise SelectiveReviewError(f"dataset sample bbox mismatch for {candidate_id!r}")
        if sample.get("confidence") != candidate.get("confidence"):
            raise SelectiveReviewError(f"dataset sample confidence mismatch for {candidate_id!r}")
        if sample.get("detector_source") != candidate.get("source"):
            raise SelectiveReviewError(f"dataset sample detector source mismatch for {candidate_id!r}")
        evidence_sha, evidence_snapshots = _validate_sample_evidence(sample, dataset_snapshot.path.parent)
        snapshots.extend(evidence_snapshots)
        selective_decision = decision.get("decision")
        if selective_decision not in {"accept", "reject", "abstain"}:
            raise SelectiveReviewError(f"invalid selective decision for {candidate_id!r}")
        review_kind = _decision_review_kind(selective_decision, reasons)
        review_candidates.append(
            {
                "candidate_id": candidate_id,
                "candidate_fingerprint": fingerprint,
                "variant_id": variant_id,
                "frame_index": frame_index,
                "bbox": candidate.get("bbox"),
                "detector_source": candidate.get("source"),
                "detector_confidence": candidate.get("confidence"),
                "predicted_label": predicted_label,
                "prediction_confidence": prediction_confidence,
                "selective_decision": selective_decision,
                "decision_reasons": reasons,
                "review_kind": review_kind,
                "priority": _review_priority(review_kind),
                "evidence": {
                    "sample_id": sample.get("sample_id"),
                    "sha256": evidence_sha,
                    "dataset_version": dataset_version,
                    "artifacts": sample.get("artifacts"),
                },
            }
        )

    bindings = {
        "policy": _artifact_binding(policy_snapshot, policy_version=policy_version),
        "decisions": {
            **_artifact_binding(decisions_snapshot, policy_version=policy_version),
            "source": "independent_artifact",
        },
        "model": _artifact_binding(model_snapshot, model_version=model_version),
        "training_report": _artifact_binding(training_report_snapshot, model_version=model_version),
        "model_weights": _artifact_binding(weights_snapshot, model_version=model_version),
        "dataset": _artifact_binding(dataset_snapshot, dataset_version=dataset_version),
        "predictions": _artifact_binding(
            predictions_snapshot,
            dataset_version=dataset_version,
            model_version=model_version,
        ),
        "contract": _artifact_binding(contract_snapshot, schema_version="2.0"),
        "annotation_resolution": _artifact_binding(qualification_snapshots["annotation_resolution"]),
        "resolved_tracking_contract": _artifact_binding(qualification_snapshots["resolved_tracking_contract"]),
        "policy_roles": _artifact_binding(qualification_snapshots["policy_roles"]),
    }
    return {
        "dataset_version": dataset_version,
        "bindings": bindings,
        "review_candidates": review_candidates,
        "timings": timing_by_variant,
        "snapshots": snapshots,
        "contract_snapshot": contract_snapshot,
        "dataset_snapshot": dataset_snapshot,
    }


def _validate_policy_decisions_contract(
    policy: dict[str, Any],
    decisions: dict[str, Any],
    *,
    policy_snapshot: _Snapshot,
    decisions_snapshot: _Snapshot,
) -> None:
    if decisions_snapshot.path.name != SELECTIVE_DECISIONS_NAME:
        raise SelectiveReviewError(f"selective decisions path must be named {SELECTIVE_DECISIONS_NAME!r}")
    if decisions_snapshot.path.parent != policy_snapshot.path.parent:
        raise SelectiveReviewError("selective policy and decisions must be sibling artifacts")
    version_inputs = _required_object(policy.get("version_inputs"), "policy.version_inputs")
    policy_version = _required_sha256(policy.get("policy_version"), "policy.policy_version")
    if _canonical_sha256(version_inputs) != policy_version:
        raise SelectiveReviewError("policy version_inputs do not reproduce policy_version")
    qualification = _required_object(version_inputs.get("qualification"), "policy.version_inputs.qualification")
    if qualification.get("policy_status") != policy.get("status"):
        raise SelectiveReviewError("policy status does not match version_inputs qualification")
    decisions_content = {
        key: value for key, value in decisions.items() if key not in {"generated_at", "policy_version"}
    }
    content_sha256 = _canonical_sha256(decisions_content)
    declared_content_sha256 = _required_sha256(
        version_inputs.get("decisions_content_sha256"),
        "policy.version_inputs.decisions_content_sha256",
    )
    artifact = _required_object(policy.get("decisions_artifact"), "policy.decisions_artifact")
    expected_artifact = {
        "path": SELECTIVE_DECISIONS_NAME,
        "sha256": decisions_snapshot.sha256,
        "content_sha256": content_sha256,
    }
    if artifact != expected_artifact:
        raise SelectiveReviewError("policy decisions_artifact does not match the exact decisions snapshot")
    if declared_content_sha256 != content_sha256:
        raise SelectiveReviewError("selective decisions canonical content sha256 mismatch")
    if decisions.get("policy_version") != policy_version:
        raise SelectiveReviewError("selective decisions policy_version does not match policy")
    if decisions.get("status") != policy.get("status") or decisions.get("lineage") != policy.get("lineage"):
        raise SelectiveReviewError("selective decisions status or lineage does not match policy")


def _load_model_package(
    model: dict[str, Any],
    model_snapshot: _Snapshot,
) -> tuple[dict[str, Any], _Snapshot, _Snapshot]:
    package_dir = model_snapshot.path.parent
    training_report_path = _safe_model_artifact(
        package_dir,
        model.get("training_report_path"),
        expected_name="training_report.v1.json",
        label="model training report",
    )
    weights_path = _safe_model_artifact(
        package_dir,
        model.get("weights_path"),
        expected_name="model.pt",
        label="model weights",
    )
    training_report, training_snapshot = _load_json_snapshot(training_report_path, "model training report")
    weights_snapshot = _snapshot(weights_path, "model weights")
    if training_snapshot.sha256 != _required_sha256(
        model.get("training_report_sha256"),
        "model.training_report_sha256",
    ):
        raise SelectiveReviewError("model training report sha256 mismatch")
    if weights_snapshot.sha256 != _required_sha256(model.get("weights_sha256"), "model.weights_sha256"):
        raise SelectiveReviewError("model weights sha256 mismatch")
    _require_envelope(
        training_report,
        artifact_type="candidate_classifier_training_report",
        name="model training report",
    )
    if training_report.get("status") != "complete":
        raise SelectiveReviewError("model training report status must be complete")
    if training_report.get("model_version") != model.get("model_version"):
        raise SelectiveReviewError("model training report model_version mismatch")
    return training_report, training_snapshot, weights_snapshot


def _safe_model_artifact(root: Path, value: Any, *, expected_name: str, label: str) -> Path:
    if value != expected_name:
        raise SelectiveReviewError(f"{label} path must be the safe relative name {expected_name!r}")
    path = (root / expected_name).resolve()
    if path.parent != root.resolve():
        raise SelectiveReviewError(f"{label} path escapes the model package")
    return path


def _validate_policy_lineage(
    policy: dict[str, Any],
    decisions: dict[str, Any],
    *,
    predictions_sha256: str,
    dataset_sha256: str,
    model_sha256: str,
    training_report_sha256: str,
    model_weights_sha256: str,
    contract_sha256: str,
) -> None:
    policy_lineage = policy.get("lineage")
    decisions_lineage = decisions.get("lineage")
    if not isinstance(policy_lineage, dict) or decisions_lineage != policy_lineage:
        raise SelectiveReviewError("policy and decisions lineage must both exist and match")
    expected = {
        "predictions": predictions_sha256,
        "dataset_manifest": dataset_sha256,
        "model_manifest": model_sha256,
        "training_report": training_report_sha256,
        "model_weights": model_weights_sha256,
    }
    for name, sha256 in expected.items():
        descriptor = _required_object(policy_lineage.get(name), f"policy.lineage.{name}")
        if descriptor.get("sha256") != sha256:
            raise SelectiveReviewError(f"policy lineage {name} sha256 mismatch")
    if policy_lineage.get("source_contract_sha256") != contract_sha256:
        raise SelectiveReviewError("policy lineage source contract sha256 mismatch")


def _validate_sample_evidence(sample: dict[str, Any], dataset_root: Path) -> tuple[str, list[_Snapshot]]:
    sample_id = _required_text(sample.get("sample_id"), "dataset sample_id")
    artifacts = _required_object(sample.get("artifacts"), f"sample {sample_id} artifacts")
    snapshots: list[_Snapshot] = []
    for artifact_name in ("tight_tensor", "context_tensor", "review_montage"):
        descriptor = _required_object(artifacts.get(artifact_name), f"sample {sample_id} {artifact_name}")
        artifact_path = _contained_artifact_path(
            dataset_root,
            descriptor.get("path"),
            f"sample {sample_id} {artifact_name}",
        )
        snapshot = _snapshot(artifact_path, f"sample {sample_id} {artifact_name}")
        expected_sha = _required_sha256(
            descriptor.get("sha256"),
            f"sample {sample_id} {artifact_name} sha256",
        )
        if snapshot.sha256 != expected_sha:
            raise SelectiveReviewError(f"sample {sample_id!r} {artifact_name} sha256 mismatch")
        snapshots.append(snapshot)
    try:
        return sample_evidence_sha256(sample), snapshots
    except ValueError as exc:
        raise SelectiveReviewError(f"invalid evidence for sample {sample_id!r}: {exc}") from exc


def _validate_actions(
    actions: list[dict[str, Any]],
    *,
    queue: dict[str, Any],
    queue_sha256: str,
    timings: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    queue_bindings = _required_object(queue.get("bindings"), "queue.bindings")
    item_by_id: dict[str, dict[str, Any]] = {}
    candidate_entries: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for item in queue["items"]:
        review_item_id = _required_text(item.get("review_item_id"), "queue item review_item_id")
        if review_item_id in item_by_id:
            raise SelectiveReviewError(f"duplicate review_item_id {review_item_id!r}")
        item_by_id[review_item_id] = item
        for candidate in item["candidates"]:
            candidate_id = _required_text(candidate.get("candidate_id"), "queue candidate_id")
            if candidate_id in candidate_entries:
                raise SelectiveReviewError(f"candidate {candidate_id!r} appears in multiple review items")
            candidate_entries[candidate_id] = (item, candidate)

    seen_candidates: dict[str, str] = {}
    normalized: list[dict[str, Any]] = []
    for index, action in enumerate(actions):
        prefix = f"actions[{index}]"
        action_id = _required_text(action.get("action_id"), f"{prefix}.action_id")
        candidate_id = _required_text(action.get("candidate_id"), f"{prefix}.candidate_id")
        previous_action_id = seen_candidates.get(candidate_id)
        if previous_action_id is not None:
            raise SelectiveReviewError(
                f"candidate {candidate_id!r} has conflicting actions {previous_action_id!r} and {action_id!r}"
            )
        seen_candidates[candidate_id] = action_id
        review_item_id = _required_text(action.get("review_item_id"), f"{prefix}.review_item_id")
        entry = candidate_entries.get(candidate_id)
        if entry is None or entry[0].get("review_item_id") != review_item_id:
            raise SelectiveReviewError(f"action {action_id!r} does not reference its bound queue candidate")
        item, candidate = entry
        reviewer_id = _required_text(action.get("reviewer_id"), f"{prefix}.reviewer_id")
        created_at = _required_text(action.get("created_at"), f"{prefix}.created_at")
        _validate_timestamp(created_at, f"{prefix}.created_at")
        action_name = action.get("action")
        if action_name not in _ACTIONS:
            raise SelectiveReviewError(f"{prefix}.action must be one of {sorted(_ACTIONS)}")
        bindings = _required_object(action.get("bindings"), f"{prefix}.bindings")
        _expect_hash(bindings.get("queue_sha256"), queue_sha256, f"{prefix}.bindings.queue_sha256")
        for binding_name in (
            "timing",
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
            queue_name = "review_timing" if binding_name == "timing" else binding_name
            descriptor = _required_object(queue_bindings.get(queue_name), f"queue.bindings.{queue_name}")
            _expect_hash(
                bindings.get(f"{binding_name}_sha256"),
                descriptor.get("sha256"),
                f"{prefix}.bindings.{binding_name}_sha256",
            )
        _expect_hash(
            bindings.get("evidence_sha256"),
            _required_object(candidate.get("evidence"), "queue candidate evidence").get("sha256"),
            f"{prefix}.bindings.evidence_sha256",
        )
        _expect_hash(
            bindings.get("candidate_fingerprint"),
            candidate.get("candidate_fingerprint"),
            f"{prefix}.bindings.candidate_fingerprint",
        )

        vote: dict[str, Any] | None = None
        correction: dict[str, Any] | None = None
        if action_name == "reject_noise":
            label = action.get("noise_subtype")
            if label not in _NOISE_LABELS:
                raise SelectiveReviewError(f"{prefix}.noise_subtype must be a concrete V2 noise label")
            vote = _human_vote(action_id, candidate, reviewer_id, created_at, label)
        elif action_name == "confirm_ball":
            vote = _human_vote(action_id, candidate, reviewer_id, created_at, "match_ball")
        elif action_name == "mark_unknown":
            vote = _human_vote(action_id, candidate, reviewer_id, created_at, "unknown")
        else:
            variant_id = _required_text(candidate.get("variant_id"), "queue candidate variant_id")
            timing = timings[variant_id]
            keypoints = _validate_keypoints(
                action.get("keypoints"),
                item=item,
                timing=timing,
                name=f"{prefix}.keypoints",
            )
            correction = {
                "action_id": action_id,
                "review_item_id": review_item_id,
                "candidate_id": candidate_id,
                "candidate_fingerprint": candidate["candidate_fingerprint"],
                "reviewer_id": reviewer_id,
                "created_at": created_at,
                "variant_id": variant_id,
                "selective_decision": "abstain",
                "keypoints": keypoints,
            }
        normalized.append({"action_id": action_id, "vote": vote, "correction": correction})
    missing_candidate_ids = sorted(set(candidate_entries) - set(seen_candidates))
    unexpected_candidate_ids = sorted(set(seen_candidates) - set(candidate_entries))
    if missing_candidate_ids or unexpected_candidate_ids:
        raise SelectiveReviewError(
            "action coverage must exactly match queue candidates: "
            f"missing={missing_candidate_ids}, unexpected={unexpected_candidate_ids}"
        )
    return normalized


def _human_vote(
    action_id: str,
    candidate: dict[str, Any],
    reviewer_id: str,
    created_at: str,
    label: str,
) -> dict[str, Any]:
    evidence = _required_object(candidate.get("evidence"), "queue candidate evidence")
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "record_type": "vote",
        "vote_id": action_id,
        "candidate_id": candidate["candidate_id"],
        "stage": "adjudication",
        "reviewer_type": "human",
        "annotator_id": reviewer_id,
        "fingerprint": _canonical_sha256(
            {
                "reviewer_id": reviewer_id,
                "candidate_fingerprint": candidate["candidate_fingerprint"],
            }
        ),
        "label": label,
        "confidence": 1.0,
        "blind": False,
        "created_at": created_at,
        "dataset_version": evidence["dataset_version"],
        "sample_id": evidence["sample_id"],
        "evidence_sha256": evidence["sha256"],
    }


def _validate_keypoints(
    value: Any,
    *,
    item: dict[str, Any],
    timing: dict[str, Any],
    name: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise SelectiveReviewError(f"{name} must be a non-empty list")
    result: list[dict[str, Any]] = []
    previous_frame = -1
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise SelectiveReviewError(f"{name}[{index}] must be an object")
        frame_index = _nonnegative_int(raw.get("frame_index"), f"{name}[{index}].frame_index")
        if frame_index <= previous_frame:
            raise SelectiveReviewError(f"{name} frame indexes must be strictly increasing and unique")
        previous_frame = frame_index
        if not item["start_frame"] <= frame_index <= item["end_frame"]:
            raise SelectiveReviewError(f"{name}[{index}] is outside the review window")
        if frame_index >= timing["frame_count"]:
            raise SelectiveReviewError(f"{name}[{index}] is outside the source video frame range")
        status = raw.get("status")
        if status not in FRAME_STATUSES:
            raise SelectiveReviewError(f"{name}[{index}].status must be one of {FRAME_STATUSES}")
        has_x = "x" in raw and raw.get("x") is not None
        has_y = "y" in raw and raw.get("y") is not None
        if has_x != has_y:
            raise SelectiveReviewError(f"{name}[{index}] x and y must both be present or absent")
        if status in {"detected", "interpolated"} and not has_x:
            raise SelectiveReviewError(f"{name}[{index}] coordinates are required for status {status}")
        row: dict[str, Any] = {"frame_index": frame_index, "status": status}
        if has_x:
            x = _finite_number(raw.get("x"), f"{name}[{index}].x")
            y = _finite_number(raw.get("y"), f"{name}[{index}].y")
            if not 0.0 <= x < timing["width"] or not 0.0 <= y < timing["height"]:
                raise SelectiveReviewError(f"{name}[{index}] coordinates are outside the source dimensions")
            row.update({"x": x, "y": y})
        if "confidence" in raw:
            confidence = _finite_number(raw.get("confidence"), f"{name}[{index}].confidence")
            if not 0.0 <= confidence <= 1.0:
                raise SelectiveReviewError(f"{name}[{index}].confidence must be between 0 and 1")
            row["confidence"] = confidence
        result.append(row)
    return result


def _candidate_fingerprint(candidate: dict[str, Any]) -> str:
    candidate_id = _required_text(candidate.get("candidate_id"), "candidate candidate_id")
    frame_index = _nonnegative_int(candidate.get("frame_index"), f"candidate {candidate_id} frame_index")
    raw_bbox = candidate.get("bbox")
    if not isinstance(raw_bbox, list) or len(raw_bbox) != 4:
        raise SelectiveReviewError(f"candidate {candidate_id!r} bbox must contain four coordinates")
    bbox = [_finite_number(value, f"candidate {candidate_id} bbox") for value in raw_bbox]
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise SelectiveReviewError(f"candidate {candidate_id!r} bbox is invalid")
    detector_source = _required_text(candidate.get("source"), f"candidate {candidate_id} source")
    confidence = _finite_number(candidate.get("confidence"), f"candidate {candidate_id} confidence")
    if not 0.0 <= confidence <= 1.0:
        raise SelectiveReviewError(f"candidate {candidate_id!r} confidence must be between zero and one")
    return _canonical_sha256(
        {
            "candidate_id": candidate_id,
            "frame_index": frame_index,
            "bbox": bbox,
            "detector_source": detector_source,
            "confidence": confidence,
        }
    )


def _decision_reasons(decision: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    for name in ("reasons", "forced_abstain_reasons"):
        raw = decision.get(name, [])
        if raw is None:
            continue
        if not isinstance(raw, list):
            raise SelectiveReviewError(f"decision {name} must be a list")
        values.extend(raw)
    result = []
    for value in values:
        text = _required_text(value, "decision reason")
        if text not in result:
            result.append(text)
    return result


def _decision_is_queue_eligible(
    decision: dict[str, Any],
    *,
    candidate_id: str,
    reasons: list[str],
) -> bool:
    scope = decision.get("decision_scope")
    if scope == "application":
        return True
    if scope != "evaluation_only":
        raise SelectiveReviewError(
            f"decision_scope for candidate {candidate_id!r} must be application or evaluation_only"
        )
    if (
        decision.get("decision") != "abstain"
        or decision.get("applied_to_contract") is not False
        or decision.get("policy_role") not in {"policy_calibration", "policy_audit"}
        or "evaluation_holdout" not in reasons
    ):
        raise SelectiveReviewError(f"evaluation holdout invariants failed for candidate {candidate_id!r}")
    return False


def _decision_review_kind(decision: str, reasons: list[str]) -> str:
    if decision == "accept":
        return "audit_accept"
    if decision == "reject":
        return "audit_reject"
    return "conflict" if any("conflict" in reason.lower() for reason in reasons) else "uncertainty"


def _parse_timing_report(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    variants = report.get("variants")
    if not isinstance(variants, list):
        raise SelectiveReviewError("review timing variants must be a list")
    result: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(variants):
        if not isinstance(raw, dict):
            raise SelectiveReviewError(f"review timing variants[{index}] must be an object")
        variant_id = _required_text(raw.get("variant_id"), f"review timing variants[{index}].variant_id")
        if variant_id in result:
            raise SelectiveReviewError(f"duplicate review timing variant {variant_id!r}")
        fps_source = raw.get("fps_source")
        if fps_source not in {"dataset_manifest", "explicit_override"}:
            raise SelectiveReviewError(f"invalid fps_source for variant {variant_id!r}")
        video = _required_object(raw.get("video"), f"review timing {variant_id} video")
        result[variant_id] = {
            "variant_id": variant_id,
            "fps": _positive_number(raw.get("fps"), f"review timing {variant_id} fps"),
            "fps_source": fps_source,
            "frame_count": _positive_int(raw.get("frame_count"), f"review timing {variant_id} frame_count"),
            "width": _positive_int(raw.get("width"), f"review timing {variant_id} width"),
            "height": _positive_int(raw.get("height"), f"review timing {variant_id} height"),
            "video": {
                "path": _required_text(video.get("path"), f"review timing {variant_id} video path"),
                "sha256": _required_sha256(video.get("sha256"), f"review timing {variant_id} video sha256"),
            },
        }
    return result


def _require_envelope(value: dict[str, Any], *, artifact_type: str, name: str) -> None:
    if value.get("schema_version") != REVIEW_SCHEMA_VERSION or value.get("artifact_type") != artifact_type:
        raise SelectiveReviewError(f"{name} must use {artifact_type} schema {REVIEW_SCHEMA_VERSION}")


def _load_json_snapshot(path: Path, name: str) -> tuple[dict[str, Any], _Snapshot]:
    captured, snapshot = _capture_snapshot(path, name, max_bytes=_MAX_JSON_ARTIFACT_BYTES)
    assert captured is not None
    try:
        value = json.loads(
            captured.decode("utf-8"),
            parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(f"non-finite {constant}")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SelectiveReviewError(f"invalid {name}: {exc}") from exc
    if not isinstance(value, dict):
        raise SelectiveReviewError(f"{name} must be a JSON object")
    return value, snapshot


def _snapshot(path: Path, name: str) -> _Snapshot:
    _, snapshot = _capture_snapshot(path, name)
    return snapshot


def _capture_snapshot(
    path: Path,
    name: str,
    *,
    max_bytes: int | None = None,
) -> tuple[bytes | None, _Snapshot]:
    path = Path(path).resolve()
    digest = hashlib.sha256()
    size = 0
    chunks: list[bytes] | None = [] if max_bytes is not None else None
    try:
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            if max_bytes is not None and before.st_size > max_bytes:
                raise SelectiveReviewError(f"{name} exceeds the {max_bytes}-byte JSON artifact limit")
            while True:
                read_size = _FILE_READ_CHUNK_BYTES
                if max_bytes is not None:
                    read_size = min(read_size, max_bytes + 1 - size)
                chunk = source.read(read_size)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                if max_bytes is not None:
                    if size > max_bytes:
                        raise SelectiveReviewError(f"{name} exceeds the {max_bytes}-byte JSON artifact limit")
                    assert chunks is not None
                    chunks.append(chunk)
            after = os.fstat(source.fileno())
    except FileNotFoundError as exc:
        raise SelectiveReviewError(f"{name} is missing: {path}") from exc
    except OSError as exc:
        raise SelectiveReviewError(f"could not read {name}: {exc}") from exc
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if size != before.st_size or after_identity != before_identity:
        raise SelectiveReviewError(f"{name} changed or ended early while it was captured")
    snapshot = _Snapshot(path=path, sha256=digest.hexdigest(), size=size)
    return (b"".join(chunks) if chunks is not None else None), snapshot


def _verify_snapshots(snapshots: list[_Snapshot], *, context: str) -> None:
    seen: set[Path] = set()
    for expected in snapshots:
        if expected.path in seen:
            continue
        seen.add(expected.path)
        if _snapshot(expected.path, expected.path.name) != expected:
            raise SelectiveReviewError(f"input changed during {context}: {expected.path}")


def _artifact_binding(snapshot: _Snapshot, **metadata: Any) -> dict[str, Any]:
    return {"path": str(snapshot.path), "sha256": snapshot.sha256, **metadata}


def _expect_hash(value: Any, expected: Any, name: str) -> None:
    actual = _required_sha256(value, name)
    expected_hash = _required_sha256(expected, f"expected {name}")
    if actual != expected_hash:
        raise SelectiveReviewError(f"{name} does not match the bound input")


def _unique_rows(value: Any, field: str, name: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        raise SelectiveReviewError(f"{name} must be a list")
    result: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            raise SelectiveReviewError(f"{name}[{index}] must be an object")
        key = _required_text(row.get(field), f"{name}[{index}].{field}")
        if key in result:
            raise SelectiveReviewError(f"duplicate {field} in {name}: {key!r}")
        result[key] = row
    return result


def _contained_artifact_path(root: Path, value: Any, name: str) -> Path:
    text = _required_text(value, f"{name} path")
    relative = Path(text)
    if relative.is_absolute() or ".." in relative.parts:
        raise SelectiveReviewError(f"{name} path must be contained and relative")
    root = root.resolve()
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise SelectiveReviewError(f"{name} path escapes its manifest directory")
    return resolved


def _referenced_input_path(root: Path, value: Any, name: str) -> Path:
    text = _required_text(value, f"{name} path")
    relative = Path(text)
    if relative.is_absolute():
        raise SelectiveReviewError(f"{name} path must be relative to its manifest")
    return (root.resolve() / relative).resolve()


def _required_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SelectiveReviewError(f"{name} must be an object")
    return value


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise SelectiveReviewError(f"{name} must be a non-empty string without surrounding whitespace")
    return value


def _required_sha256(value: Any, name: str) -> str:
    text = _required_text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise SelectiveReviewError(f"{name} must be a lowercase sha256")
    return text


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SelectiveReviewError(f"{name} must be finite")
    return float(value)


def _positive_number(value: Any, name: str) -> float:
    result = _finite_number(value, name)
    if result <= 0:
        raise SelectiveReviewError(f"{name} must be positive")
    return result


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SelectiveReviewError(f"{name} must be a non-negative integer")
    return value


def _positive_int(value: Any, name: str) -> int:
    result = _nonnegative_int(value, name)
    if result == 0:
        raise SelectiveReviewError(f"{name} must be positive")
    return result


def _validate_timestamp(value: str, name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SelectiveReviewError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SelectiveReviewError(f"{name} must include a timezone")


def _canonical_json(value: Any, name: str) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SelectiveReviewError(f"{name} must be finite JSON") from exc


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value, "hash input").encode("utf-8")).hexdigest()


def _validate_finite_json(value: Any, name: str) -> None:
    _canonical_json(value, name)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _require_new_output_dir(output_dir: Path) -> None:
    if output_dir.exists():
        raise SelectiveReviewError(f"output directory must be new: {output_dir}")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise SelectiveReviewError(f"argument error: {message}")


def _fps_override_arguments(values: list[str]) -> dict[str, float]:
    result: dict[str, float] = {}
    for value in values:
        if "=" not in value:
            raise SelectiveReviewError("fps override must use variant=fps")
        variant_id, raw_fps = value.split("=", 1)
        variant_id = _required_text(variant_id, "fps override variant")
        if variant_id in result:
            raise SelectiveReviewError(f"duplicate fps override for variant {variant_id!r}")
        try:
            parsed = float(raw_fps)
        except (TypeError, ValueError, OverflowError) as exc:
            raise SelectiveReviewError(f"fps override for {variant_id!r} must be positive and finite") from exc
        result[variant_id] = _positive_number(parsed, f"fps override {variant_id}")
    return result


def build_cli_main(argv: list[str] | None = None) -> int:
    parser = _JsonArgumentParser(description="Build an atomic, bounded selective review queue.")
    parser.add_argument("--dataset-manifest", required=True, type=Path)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--decisions", required=True, type=Path)
    parser.add_argument("--model-manifest", required=True, type=Path)
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--annotation-resolution", required=True, type=Path)
    parser.add_argument("--resolved-contract", required=True, type=Path)
    parser.add_argument("--policy-roles", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--fps-override", action="append", default=[])
    parser.add_argument("--window-seconds", type=float, default=DEFAULT_WINDOW_SECONDS)
    parser.add_argument("--max-windows", type=int, default=MAX_REVIEW_WINDOWS)
    try:
        args = parser.parse_args(argv)
        queue = build_selective_review_queue(
            args.dataset_manifest,
            args.predictions,
            args.policy,
            args.model_manifest,
            args.contract,
            args.output_dir,
            decisions_path=args.decisions,
            annotation_resolution_path=args.annotation_resolution,
            resolved_contract_path=args.resolved_contract,
            policy_roles_path=args.policy_roles,
            fps_overrides=_fps_override_arguments(args.fps_override),
            window_seconds=args.window_seconds,
            max_windows=args.max_windows,
        )
    except Exception as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, allow_nan=False),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "output_dir": str(args.output_dir),
                "review_item_count": queue["review_item_count"],
                "coverage_complete": queue["selection"]["coverage_complete"],
                "requires_additional_round": queue["selection"]["requires_additional_round"],
            },
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    return 0


def materialize_cli_main(argv: list[str] | None = None) -> int:
    parser = _JsonArgumentParser(description="Validate and atomically materialize selective review actions.")
    parser.add_argument("--queue", required=True, type=Path)
    parser.add_argument("--actions", required=True, type=Path)
    parser.add_argument("--dataset-manifest", required=True, type=Path)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--decisions", required=True, type=Path)
    parser.add_argument("--model-manifest", required=True, type=Path)
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--annotation-resolution", required=True, type=Path)
    parser.add_argument("--resolved-contract", required=True, type=Path)
    parser.add_argument("--policy-roles", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    try:
        args = parser.parse_args(argv)
        report = materialize_selective_review_actions(
            args.queue,
            args.actions,
            args.dataset_manifest,
            args.predictions,
            args.policy,
            args.model_manifest,
            args.contract,
            args.output_dir,
            decisions_path=args.decisions,
            annotation_resolution_path=args.annotation_resolution,
            resolved_contract_path=args.resolved_contract,
            policy_roles_path=args.policy_roles,
        )
    except Exception as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, allow_nan=False),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "output_dir": str(args.output_dir),
                "round_id": report["round_id"],
                "training_invoked": report["training_invoked"],
            },
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    return 0
