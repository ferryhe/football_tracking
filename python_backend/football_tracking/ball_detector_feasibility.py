from __future__ import annotations

import hashlib
import math
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Iterable

from football_tracking.ball_detector_annotations import annotation_etag, validate_ball_annotation
from football_tracking.detector_development_common import (
    canonical_sha256,
    json_object_from_bytes,
    read_regular_bytes,
    require_sha256,
)

TEMPORAL_GROUPING_PROFILE_ID = "tiny_ball_temporal_groups_v1"
TEMPORAL_BLOCK_SAMPLING_PROFILE_ID = "tiny_ball_temporal_block_hash_v1"
FEASIBILITY_METRIC_PROFILE_ID = "tiny_ball_feasibility_metric_v1"
TEMPORAL_GUARD_RADIUS_FRAMES = 2
SCALE_STRATA = ("near", "mid", "far")
LIGHTING_STRATA = ("bright_sun", "shadow", "backlight", "twilight", "artificial_light")
MOTION_OCCLUSION_STRATA = (
    "none",
    "ground",
    "airborne",
    "motion_blurred",
    "occluded",
    "reappearance",
    "stationary",
)
HUMAN_CONFIRMED_PROVENANCE_VALUES = frozenset(
    {
        "manual_human_annotation",
        "detector_candidate_human_confirmed",
        "propagation_suggestion_human_confirmed",
        "suggestion_dismissed_manual",
    }
)

METRIC_PROFILE: dict[str, Any] = {
    "profile_id": FEASIBILITY_METRIC_PROFILE_ID,
    "candidate_budget": 5,
    "top1_recall_target": 0.60,
    "top5_recall_target": 0.80,
    "minimum_total_frames": 20,
    "maximum_total_frames": 50,
    "minimum_localizable_positives": 15,
    "minimum_confirmed_absent": 5,
    "minimum_applicable_stratum_positives": 3,
    "exploratory_small_n_threshold": 10,
    "apparent_size_rule": {
        "name": "source-height-bound-ball-diagonal-v1",
        "plausible_diagonal_min_source_px": 1.0,
        "far_max_source_height_divisor": 80.0,
        "mid_max_source_height_divisor": 40.0,
        "near_max_source_height_multiplier": 0.075,
        "aspect_ratio_min": 0.25,
        "aspect_ratio_max": 4.0,
    },
    "matching_rule": {
        "name": "confirmed-box-center-region-v1",
        "minimum_radius_source_px": 4.0,
        "confirmed_box_diagonal_multiplier": 0.75,
        "source_height_cap_divisor": 45.0,
        "one_to_one": True,
    },
    "intervals": {
        "confidence": 0.95,
        "recall": "one-sided-wilson-score-v1",
        "false_candidates": "bounded-hoeffding-upper-v1",
        "false_candidate_range": [0.0, 5.0],
    },
}
METRIC_PROFILE_SHA256 = canonical_sha256(METRIC_PROFILE)


class FeasibilityError(ValueError):
    """A frozen feasibility contract could not be evaluated honestly."""


def inherit_temporal_group(
    source_group: dict[str, Any],
    *,
    artifact_type: str,
    artifact_id: str,
) -> dict[str, Any]:
    """Bind a derivative artifact to an existing group without regrouping."""

    if artifact_type not in {"proxy", "crop", "tile", "alternate_encode", "propagation"}:
        raise FeasibilityError("derivative artifact type is invalid")
    if not isinstance(artifact_id, str) or not artifact_id:
        raise FeasibilityError("derivative artifact identity is required")
    if not isinstance(source_group, dict):
        raise FeasibilityError("source temporal group is required")
    source_sha256 = source_group.get("source_sha256")
    seed_frame_index = source_group.get("seed_frame_index")
    if (
        not isinstance(source_sha256, str)
        or isinstance(seed_frame_index, bool)
        or not isinstance(seed_frame_index, int)
    ):
        raise FeasibilityError("source temporal group authority is invalid")
    expected = temporal_group_for_frame(source_sha256, seed_frame_index)
    if any(source_group.get(key) != value for key, value in expected.items()):
        raise FeasibilityError("source temporal group is not canonical")
    inherited = {key: (list(value) if isinstance(value, list) else value) for key, value in expected.items()}
    inherited["derivative"] = {
        "artifact_type": artifact_type,
        "artifact_id": artifact_id,
        "inheritance_rule": "inherit-source-group-without-regrouping-v1",
    }
    inherited["derivative_binding_sha256"] = canonical_sha256(inherited)
    return inherited


def validate_derivative_ancestry(
    source_group: dict[str, Any],
    derivative_group: dict[str, Any],
) -> None:
    if not isinstance(derivative_group, dict):
        raise FeasibilityError("derivative temporal group is invalid")
    derivative = derivative_group.get("derivative")
    if not isinstance(derivative, dict):
        raise FeasibilityError("derivative temporal group inheritance is missing")
    expected = inherit_temporal_group(
        source_group,
        artifact_type=derivative.get("artifact_type"),
        artifact_id=derivative.get("artifact_id"),
    )
    if derivative_group != expected:
        raise FeasibilityError("derivative artifact recomputed or changed its temporal group")


def temporal_group_for_frame(
    source_sha256: str,
    frame_index: int,
    *,
    profile_id: str = TEMPORAL_GROUPING_PROFILE_ID,
) -> dict[str, Any]:
    require_sha256(source_sha256, "source sha256")
    if isinstance(frame_index, bool) or not isinstance(frame_index, int) or frame_index < 0:
        raise FeasibilityError("frame_index must be a non-negative integer")
    if profile_id != TEMPORAL_GROUPING_PROFILE_ID:
        raise FeasibilityError("unsupported temporal grouping profile")
    start = max(0, frame_index - TEMPORAL_GUARD_RADIUS_FRAMES)
    end = frame_index + TEMPORAL_GUARD_RADIUS_FRAMES
    group_id = canonical_sha256(
        {
            "profile_id": profile_id,
            "source_sha256": source_sha256,
            "seed_frame_index": frame_index,
            "guard_radius_frames": TEMPORAL_GUARD_RADIUS_FRAMES,
        }
    )
    canonical_moment_id = canonical_sha256(
        {
            "source_sha256": source_sha256,
            "frame_index": frame_index,
            "pts_basis": "source_frame_index",
        }
    )
    return {
        "group_id": group_id,
        "profile_id": profile_id,
        "source_sha256": source_sha256,
        "seed_frame_index": frame_index,
        "start_frame": start,
        "end_frame": end,
        "derivative_family": [start, end],
        "canonical_moment_id": canonical_moment_id,
        "derivative_family_id": group_id,
        "ancestry_profile": "source-proxy-crop-tile-propagation-closure-v1",
    }


def sample_unseen_temporal_groups(
    *,
    source_sha256: str,
    candidate_frame_indices: Iterable[int],
    target_count: int,
    excluded_group_ids: set[str],
    reserved_group_ids: set[str],
    seed: str,
    excluded_groups: Iterable[dict[str, Any]] = (),
    reserved_groups: Iterable[dict[str, Any]] = (),
    lighting_strata: Iterable[dict[str, Any]] = (),
    candidate_start_frame: int | None = None,
    candidate_end_frame: int | None = None,
    candidate_frame_count: int | None = None,
) -> list[dict[str, Any]]:
    if isinstance(target_count, bool) or not isinstance(target_count, int) or not 20 <= target_count <= 50:
        raise FeasibilityError("target_count must be between 20 and 50")
    if not isinstance(seed, str) or not seed:
        raise FeasibilityError("sampling seed must be non-empty")
    excluded_entries = list(excluded_groups)
    reserved_entries = list(reserved_groups)
    _require_complete_group_authority(source_sha256, excluded_group_ids, excluded_entries, "excluded")
    _require_complete_group_authority(source_sha256, reserved_group_ids, reserved_entries, "reserved")
    start_frame, end_frame, expected_count = _candidate_stream_bounds(
        candidate_frame_indices,
        candidate_start_frame=candidate_start_frame,
        candidate_end_frame=candidate_end_frame,
        candidate_frame_count=candidate_frame_count,
    )
    declared_lighting = _normalize_lighting_strata(
        lighting_strata,
        target_count=target_count,
        candidate_start_frame=start_frame,
        candidate_end_frame=end_frame,
    )
    sampling_rows: list[dict[str, Any] | None] = (
        list(declared_lighting)
        if declared_lighting
        else [
            {
                "stratum": None,
                "quota": target_count,
                "frame_intervals": [{"start_frame": start_frame, "end_frame": end_frame}],
            }
        ]
    )
    block_options: list[list[list[tuple[bytes, int]]]] = [
        [[] for _ in range(int(row["quota"]))] for row in sampling_rows
    ]
    unavailable_ids = set(excluded_group_ids) | set(reserved_group_ids)
    unavailable_spans = _merged_spans([_group_span(group) for group in [*excluded_entries, *reserved_entries]])
    unavailable_index = 0
    previous_frame: int | None = None
    observed_count = 0
    for frame_index in candidate_frame_indices:
        if (
            isinstance(frame_index, bool)
            or not isinstance(frame_index, int)
            or frame_index < 0
            or (previous_frame is not None and frame_index <= previous_frame)
        ):
            raise FeasibilityError("candidate frame indices must be strictly increasing non-negative integers")
        previous_frame = frame_index
        observed_count += 1
        if not start_frame <= frame_index <= end_frame:
            raise FeasibilityError("candidate frame stream differs from its frozen bounds")
        span = (
            max(0, frame_index - TEMPORAL_GUARD_RADIUS_FRAMES),
            frame_index + TEMPORAL_GUARD_RADIUS_FRAMES,
        )
        while unavailable_index < len(unavailable_spans) and unavailable_spans[unavailable_index][1] < span[0]:
            unavailable_index += 1
        if unavailable_index < len(unavailable_spans) and _spans_overlap(span, unavailable_spans[unavailable_index]):
            continue
        group_id = canonical_sha256(
            {
                "profile_id": TEMPORAL_GROUPING_PROFILE_ID,
                "source_sha256": source_sha256,
                "seed_frame_index": frame_index,
                "guard_radius_frames": TEMPORAL_GUARD_RADIUS_FRAMES,
            }
        )
        if group_id in unavailable_ids:
            continue
        for row_index, row in enumerate(sampling_rows):
            intervals = row["frame_intervals"]
            if not any(
                interval["start_frame"] <= span[0] and span[1] <= interval["end_frame"] for interval in intervals
            ):
                continue
            quota = int(row["quota"])
            pool_start = min(interval["start_frame"] for interval in intervals)
            pool_end = max(interval["end_frame"] for interval in intervals)
            block_index = min(
                quota - 1,
                ((frame_index - pool_start) * quota) // (pool_end - pool_start + 1),
            )
            rank = hashlib.sha256(
                (f"{seed}:lighting:{row['stratum']}:time-block-{block_index}:{source_sha256}:{frame_index}").encode(
                    "utf-8"
                )
            ).digest()
            _offer_bounded_option(block_options[row_index][block_index], rank, frame_index)
            break
    if observed_count != expected_count:
        raise FeasibilityError("candidate frame stream differs from its frozen count")

    selected: list[dict[str, Any]] = []
    for row, blocks in zip(sampling_rows, block_options, strict=True):
        chosen_frames = _select_temporally_diverse_candidates(
            blocks,
            int(row["quota"]),
            already_selected=selected,
        )
        stratum = row["stratum"]
        if len(chosen_frames) != row["quota"]:
            if stratum is not None:
                raise FeasibilityError(
                    f"lighting stratum {stratum} has only {len(chosen_frames)} unseen non-overlapping groups; {row['quota']} are required"
                )
            raise FeasibilityError(
                f"only {len(chosen_frames)} non-overlapping unseen temporal groups remain; {target_count} are required"
            )
        for frame_index in chosen_frames:
            group = {
                **temporal_group_for_frame(source_sha256, frame_index),
                "frame_index": frame_index,
            }
            if stratum is not None:
                group["pre_reveal_lighting_stratum"] = stratum
            selected.append(group)
    if len(selected) < target_count:
        raise FeasibilityError(
            f"only {len(selected)} non-overlapping unseen temporal groups remain; {target_count} are required"
        )
    return sorted(selected, key=lambda item: item["frame_index"])


def _select_temporally_diverse_candidates(
    blocks: list[list[tuple[bytes, int]]],
    target_count: int,
    *,
    already_selected: list[dict[str, Any]],
) -> list[int]:
    if target_count <= 0 or not blocks:
        return []
    selected: list[int] = []

    def overlaps(frame_index: int) -> bool:
        span = (
            max(0, frame_index - TEMPORAL_GUARD_RADIUS_FRAMES),
            frame_index + TEMPORAL_GUARD_RADIUS_FRAMES,
        )
        return any(
            _spans_overlap(span, (chosen["start_frame"], chosen["end_frame"])) for chosen in already_selected
        ) or any(
            _spans_overlap(
                span,
                (
                    max(0, chosen - TEMPORAL_GUARD_RADIUS_FRAMES),
                    chosen + TEMPORAL_GUARD_RADIUS_FRAMES,
                ),
            )
            for chosen in selected
        )

    for block in blocks:
        choice = next(
            (frame_index for _, frame_index in block if not overlaps(frame_index)),
            None,
        )
        if choice is not None:
            selected.append(choice)
    if len(selected) < target_count:
        fallback = sorted(
            {(rank, frame_index) for block in blocks for rank, frame_index in block if frame_index not in selected}
        )
        for _, frame_index in fallback:
            if overlaps(frame_index):
                continue
            selected.append(frame_index)
            if len(selected) == target_count:
                break
    return selected


def build_candidate_universe_authority(
    *,
    source_sha256: str,
    start_frame: int,
    end_frame: int,
    lighting_strata: Iterable[dict[str, Any]],
    excluded_groups: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Build the bounded authority whose digest freezes a whole-source sample."""

    require_sha256(source_sha256, "source sha256")
    if (
        isinstance(start_frame, bool)
        or not isinstance(start_frame, int)
        or isinstance(end_frame, bool)
        or not isinstance(end_frame, int)
        or start_frame < 0
        or end_frame < start_frame
    ):
        raise FeasibilityError("candidate universe bounds are invalid")
    rows = list(lighting_strata)
    target_count = sum(row.get("quota", 0) if isinstance(row, dict) else 0 for row in rows)
    normalized_lighting = _normalize_lighting_strata(
        rows,
        target_count=target_count,
        candidate_start_frame=start_frame,
        candidate_end_frame=end_frame,
    )
    canonical_exclusions: dict[str, dict[str, Any]] = {}
    for entry in excluded_groups:
        if not isinstance(entry, dict):
            raise FeasibilityError("excluded temporal group registry entry must be an object")
        group_id = entry.get("group_id")
        _require_complete_group_authority(source_sha256, {group_id}, [entry], "excluded")
        canonical = temporal_group_for_frame(source_sha256, entry["seed_frame_index"])
        existing = canonical_exclusions.get(group_id)
        if existing is not None and existing != canonical:
            raise FeasibilityError("excluded temporal group authority is inconsistent")
        canonical_exclusions[group_id] = canonical
    return {
        "schema_version": "1.0",
        "artifact_type": "ball_annotation_candidate_universe",
        "source_sha256": source_sha256,
        "start_frame": start_frame,
        "end_frame": end_frame,
        "candidate_frame_count": end_frame - start_frame + 1,
        "grouping_profile_id": TEMPORAL_GROUPING_PROFILE_ID,
        "selection_profile_id": TEMPORAL_BLOCK_SAMPLING_PROFILE_ID,
        "lighting_strata": normalized_lighting,
        "excluded_temporal_groups": sorted(
            canonical_exclusions.values(),
            key=lambda group: (
                group["source_sha256"],
                group["start_frame"],
                group["end_frame"],
                group["group_id"],
            ),
        ),
    }


def _candidate_stream_bounds(
    candidate_frame_indices: Iterable[int],
    *,
    candidate_start_frame: int | None,
    candidate_end_frame: int | None,
    candidate_frame_count: int | None,
) -> tuple[int, int, int]:
    if isinstance(candidate_frame_indices, range):
        observed_count = len(candidate_frame_indices)
        if observed_count == 0 or candidate_frame_indices.step <= 0:
            raise FeasibilityError("candidate frame universe must be non-empty and increasing")
        observed_start = candidate_frame_indices[0]
        observed_end = candidate_frame_indices[-1]
    elif isinstance(candidate_frame_indices, (list, tuple)):
        observed_count = len(candidate_frame_indices)
        if observed_count == 0:
            raise FeasibilityError("candidate frame universe must be non-empty and increasing")
        observed_start = candidate_frame_indices[0]
        observed_end = candidate_frame_indices[-1]
    else:
        if None in (
            candidate_start_frame,
            candidate_end_frame,
            candidate_frame_count,
        ):
            raise FeasibilityError("streaming candidate frame authority requires frozen bounds and count")
        observed_start = candidate_start_frame
        observed_end = candidate_end_frame
        observed_count = candidate_frame_count
    start = observed_start if candidate_start_frame is None else candidate_start_frame
    end = observed_end if candidate_end_frame is None else candidate_end_frame
    count = observed_count if candidate_frame_count is None else candidate_frame_count
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or start < 0
        or end < start
        or count <= 0
        or observed_start < start
        or observed_end > end
        or observed_count != count
    ):
        raise FeasibilityError("candidate frame stream differs from its frozen authority")
    return start, end, count


def _normalize_lighting_strata(
    lighting_strata: Iterable[dict[str, Any]],
    *,
    target_count: int,
    candidate_start_frame: int,
    candidate_end_frame: int,
) -> list[dict[str, Any]]:
    declared = list(lighting_strata)
    if not declared:
        return []
    if any(not isinstance(row, dict) for row in declared):
        raise FeasibilityError("pre-reveal lighting sampling authority is invalid")
    if len({row.get("stratum") for row in declared}) != len(declared):
        raise FeasibilityError("pre-reveal lighting sampling authority is invalid")
    normalized: list[dict[str, Any]] = []
    for row in declared:
        stratum = row.get("stratum")
        quota = row.get("quota")
        intervals = row.get("frame_intervals")
        if (
            stratum not in LIGHTING_STRATA
            or isinstance(quota, bool)
            or not isinstance(quota, int)
            or quota < 3
            or not isinstance(intervals, list)
            or not intervals
        ):
            raise FeasibilityError("pre-reveal lighting sampling authority is invalid")
        normalized_intervals: list[dict[str, int]] = []
        for interval in intervals:
            if (
                not isinstance(interval, dict)
                or set(interval) != {"start_frame", "end_frame"}
                or isinstance(interval.get("start_frame"), bool)
                or not isinstance(interval.get("start_frame"), int)
                or isinstance(interval.get("end_frame"), bool)
                or not isinstance(interval.get("end_frame"), int)
                or interval["start_frame"] < 0
                or interval["end_frame"] < interval["start_frame"]
            ):
                raise FeasibilityError("pre-reveal lighting sampling authority is invalid")
            if interval["start_frame"] < candidate_start_frame or interval["end_frame"] > candidate_end_frame:
                raise FeasibilityError("pre-reveal lighting intervals exceed the candidate universe")
            normalized_intervals.append(dict(interval))
        normalized.append(
            {
                "stratum": stratum,
                "quota": quota,
                "frame_intervals": sorted(
                    normalized_intervals,
                    key=lambda interval: (interval["start_frame"], interval["end_frame"]),
                ),
            }
        )
    if sum(row["quota"] for row in normalized) != target_count:
        raise FeasibilityError("pre-reveal lighting quotas must equal target_count")
    all_intervals = sorted(
        (interval for row in normalized for interval in row["frame_intervals"]),
        key=lambda interval: (interval["start_frame"], interval["end_frame"]),
    )
    previous_end: int | None = None
    for interval in all_intervals:
        if previous_end is not None and interval["start_frame"] <= previous_end:
            raise FeasibilityError("pre-reveal lighting intervals overlap")
        if previous_end is not None and interval["start_frame"] != previous_end + 1:
            raise FeasibilityError("pre-reveal lighting intervals contain a gap")
        previous_end = interval["end_frame"]
    if (
        all_intervals[0]["start_frame"] != candidate_start_frame
        or all_intervals[-1]["end_frame"] != candidate_end_frame
    ):
        raise FeasibilityError("pre-reveal lighting intervals do not cover the candidate universe")
    return sorted(normalized, key=lambda row: LIGHTING_STRATA.index(row["stratum"]))


def _offer_bounded_option(options: list[tuple[bytes, int]], rank: bytes, frame_index: int) -> None:
    options.append((rank, frame_index))
    options.sort()
    del options[16:]


def _merged_spans(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def build_feasibility_report(
    package_path: Path,
    *,
    trusted_root: Path,
    get_probe: Callable[[str], dict[str, Any]],
    get_sampling_lock: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    """Score only one server-owned, sealed check package and exact T2 report.

    Callers cannot supply annotations, candidates, applicability subsets or an
    opaque manifest digest.  Those values are reloaded and cross-validated
    from the immutable package and its server-resolved ready probe.
    """

    package_path = Path(package_path)
    trusted_root = Path(trusted_root)
    try:
        content, _ = read_regular_bytes(
            package_path,
            "sealed ball annotation package",
            max_bytes=64 * 1024 * 1024,
            trusted_root=trusted_root,
        )
        package = json_object_from_bytes(content, "sealed ball annotation package")
    except Exception as exc:
        raise FeasibilityError("sealed annotation package is unavailable or unsafe") from exc
    if package.get("artifact_type") != "ball_annotation_package" or package.get("schema_version") != "1.0":
        raise FeasibilityError("sealed annotation package schema is invalid")
    if package.get("data_role") != "check":
        raise FeasibilityError("only a pre-reveal locked check package can authorize feasibility")
    attempt_family_sha256 = require_sha256(package.get("attempt_family_sha256"), "attempt family sha256")
    development_package_binding = package.get("development_package_binding")
    if (
        not isinstance(development_package_binding, dict)
        or set(development_package_binding) != {"session_id", "package_sha256", "attempt_family_sha256"}
        or not isinstance(development_package_binding.get("session_id"), str)
        or not development_package_binding["session_id"]
        or require_sha256(
            development_package_binding.get("package_sha256"),
            "development annotation package sha256",
        )
        != development_package_binding.get("package_sha256")
        or development_package_binding.get("attempt_family_sha256") != attempt_family_sha256
    ):
        raise FeasibilityError("development package attempt-family binding is invalid")
    package_sha256 = package.get("package_sha256")
    require_sha256(package_sha256, "annotation package sha256")
    if canonical_sha256({key: value for key, value in package.items() if key != "package_sha256"}) != package_sha256:
        raise FeasibilityError("sealed annotation package digest does not match its contents")

    manifest = package.get("sampling_manifest")
    if not isinstance(manifest, dict):
        raise FeasibilityError("sealed sampling manifest is missing")
    manifest_sha256 = manifest.get("manifest_sha256")
    require_sha256(manifest_sha256, "sampling manifest sha256")
    if canonical_sha256({key: value for key, value in manifest.items() if key != "manifest_sha256"}) != manifest_sha256:
        raise FeasibilityError("sealed sampling manifest digest does not match its contents")
    session_id = package.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise FeasibilityError("sealed annotation session identity is invalid")
    try:
        sampling_lock = get_sampling_lock(session_id)
    except Exception as exc:
        raise FeasibilityError("pre-reveal sampling lock cannot be resolved by the server") from exc
    if not isinstance(sampling_lock, dict):
        raise FeasibilityError("pre-reveal sampling lock is invalid")
    lock_sha256 = sampling_lock.get("lock_sha256")
    require_sha256(lock_sha256, "sampling lock sha256")
    if (
        sampling_lock.get("artifact_type") != "ball_annotation_sampling_lock"
        or sampling_lock.get("schema_version") != "1.0"
        or sampling_lock.get("session_id") != session_id
        or sampling_lock.get("sampling_manifest_sha256") != manifest_sha256
        or sampling_lock.get("locked_before_probe") is not True
        or canonical_sha256({key: value for key, value in sampling_lock.items() if key != "lock_sha256"}) != lock_sha256
    ):
        raise FeasibilityError("package manifest differs from the immutable pre-reveal sampling lock")
    source = package.get("source")
    locked_profile = package.get("locked_profile")
    if not isinstance(source, dict) or not isinstance(locked_profile, dict):
        raise FeasibilityError("sealed source or locked profile binding is missing")
    source_sha256 = require_sha256(source.get("sha256"), "package source sha256")
    locked_profile_id = locked_profile.get("profile_id")
    locked_profile_sha256 = require_sha256(locked_profile.get("profile_sha256"), "locked profile sha256")
    if not isinstance(locked_profile_id, str) or not locked_profile_id:
        raise FeasibilityError("locked profile identity is invalid")
    frame_indices = _validate_sampling_manifest(
        manifest,
        attempt_family_sha256=attempt_family_sha256,
        development_package_sha256=development_package_binding["package_sha256"],
        source_sha256=source_sha256,
        source_frame_count=source.get("frame_count"),
        locked_profile_id=locked_profile_id,
        locked_profile_sha256=locked_profile_sha256,
    )
    applicable_scale, applicable_lighting = _applicable_strata_from_manifest(manifest)
    annotations = _validate_package_revision_truth(package, frame_indices)

    check_probe_job_id = package.get("check_probe_job_id")
    if not isinstance(check_probe_job_id, str) or not check_probe_job_id:
        raise FeasibilityError("sealed check probe identity is missing")
    try:
        job = get_probe(check_probe_job_id)
    except Exception as exc:
        raise FeasibilityError("sealed check probe cannot be resolved by the server") from exc
    candidates_by_frame, raw_candidate_counts, probe_report_sha256 = _validate_locked_probe_evidence(
        job,
        source=source,
        locked_profile=locked_profile,
        frame_indices=frame_indices,
        frozen_authority=package.get("check_probe_authority"),
        sampling_manifest_sha256=manifest_sha256,
    )
    report = _score_validated_feasibility(
        session_id=session_id,
        source_sha256=source_sha256,
        locked_profile_id=locked_profile_id,
        locked_profile_sha256=locked_profile_sha256,
        metric_profile_id=manifest["metric_profile_id"],
        sampling_manifest_sha256=manifest_sha256,
        annotations=annotations,
        candidates_by_frame=candidates_by_frame,
        raw_candidate_counts_by_frame=raw_candidate_counts,
        source_height=source.get("height"),
        frozen_lighting_by_frame={
            group["frame_index"]: group["pre_reveal_lighting_stratum"] for group in manifest["groups"]
        },
        attempt_family_sha256=attempt_family_sha256,
        development_package_binding=development_package_binding,
        applicable_scale_strata=applicable_scale,
        applicable_lighting_strata=applicable_lighting,
    )
    report["sealed_evidence"] = {
        "annotation_package_sha256": package_sha256,
        "sampling_manifest_sha256": manifest_sha256,
        "sampling_lock_sha256": lock_sha256,
        "check_probe_job_id": check_probe_job_id,
        "check_probe_report_sha256": probe_report_sha256,
        "attempt_family_sha256": attempt_family_sha256,
        "development_annotation_session_id": development_package_binding["session_id"],
        "development_annotation_package_sha256": development_package_binding["package_sha256"],
        "dataset_expansion_eligibility": deepcopy(package["dataset_expansion_eligibility"]),
    }
    report["report_sha256"] = canonical_sha256({key: value for key, value in report.items() if key != "report_sha256"})
    return report


def _score_validated_feasibility(
    *,
    session_id: str,
    source_sha256: str,
    locked_profile_id: str,
    locked_profile_sha256: str,
    metric_profile_id: str,
    sampling_manifest_sha256: str,
    annotations: list[dict[str, Any]],
    candidates_by_frame: dict[int, list[dict[str, Any]]],
    raw_candidate_counts_by_frame: dict[int, int] | None = None,
    source_height: int,
    frozen_lighting_by_frame: dict[int, str],
    attempt_family_sha256: str,
    development_package_binding: dict[str, str],
    applicable_scale_strata: list[str],
    applicable_lighting_strata: list[str],
) -> dict[str, Any]:
    require_sha256(source_sha256, "source sha256")
    require_sha256(locked_profile_sha256, "locked profile sha256")
    require_sha256(sampling_manifest_sha256, "sampling manifest sha256")
    require_sha256(attempt_family_sha256, "attempt family sha256")
    if metric_profile_id != FEASIBILITY_METRIC_PROFILE_ID:
        raise FeasibilityError("unsupported feasibility metric profile")
    if (
        not isinstance(session_id, str)
        or not session_id
        or not isinstance(locked_profile_id, str)
        or not locked_profile_id
    ):
        raise FeasibilityError("session and locked profile identities are required")
    _validate_strata(applicable_scale_strata, SCALE_STRATA, "scale")
    _validate_strata(applicable_lighting_strata, LIGHTING_STRATA, "lighting")
    if isinstance(source_height, bool) or not isinstance(source_height, int) or source_height <= 0:
        raise FeasibilityError("source height must be a positive integer")
    if not isinstance(frozen_lighting_by_frame, dict):
        raise FeasibilityError("frozen lighting frame authority is invalid")
    if (
        not isinstance(development_package_binding, dict)
        or set(development_package_binding) != {"session_id", "package_sha256", "attempt_family_sha256"}
        or not isinstance(development_package_binding.get("session_id"), str)
        or not development_package_binding["session_id"]
        or require_sha256(
            development_package_binding.get("package_sha256"),
            "development annotation package sha256",
        )
        != development_package_binding.get("package_sha256")
        or development_package_binding.get("attempt_family_sha256") != attempt_family_sha256
    ):
        raise FeasibilityError("development package attempt-family binding is invalid")
    if not annotations or any(item.get("annotation_state") != "confirmed" for item in annotations):
        raise FeasibilityError("feasibility scoring accepts only confirmed annotations")

    seen_frames: set[int] = set()
    for annotation in annotations:
        frame_index = annotation.get("frame_index")
        if (
            isinstance(frame_index, bool)
            or not isinstance(frame_index, int)
            or frame_index < 0
            or frame_index in seen_frames
        ):
            raise FeasibilityError("annotations require unique non-negative frame indices")
        seen_frames.add(frame_index)
    if set(frozen_lighting_by_frame) != seen_frames or any(
        isinstance(frame_index, bool) or not isinstance(frame_index, int) or lighting not in LIGHTING_STRATA
        for frame_index, lighting in frozen_lighting_by_frame.items()
    ):
        raise FeasibilityError("frozen lighting frame authority does not match the annotation set")

    source_bounds = _computed_source_px_bounds(source_height)
    positives: list[dict[str, Any]] = []
    absent: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    annotation_diagnostics: dict[int, dict[str, Any]] = {}
    plausibility_contradictions = 0
    scale_mismatches = 0
    lighting_mismatches = 0
    for annotation in annotations:
        frame_index = int(annotation["frame_index"])
        observed_lighting = annotation.get("lighting_tag")
        frozen_lighting = frozen_lighting_by_frame[frame_index]
        codes: list[str] = []
        if observed_lighting != frozen_lighting:
            codes.append(f"lighting_stratum_mismatch:{frozen_lighting}:{observed_lighting}")
            lighting_mismatches += 1

        bbox_diagonal: float | None = None
        bbox_aspect_ratio: float | None = None
        derived_scale: str | None = None
        presence = annotation.get("presence")
        localizable = presence == "present" and annotation.get("visibility") in {
            "visible",
            "partial",
        }
        if localizable:
            bbox = annotation.get("bbox_source_px")
            if not isinstance(bbox, dict):
                raise FeasibilityError("localizable positive annotations require confirmed boxes")
            bbox_diagonal, bbox_aspect_ratio, derived_scale, size_codes = _classify_annotation_box(
                bbox, source_bounds=source_bounds
            )
            codes.extend(size_codes)
            plausibility_contradictions += int(bool(size_codes))
            observed_scale = annotation.get("scale_stratum")
            if derived_scale is not None and observed_scale != derived_scale:
                codes.append(f"scale_stratum_mismatch:{observed_scale}:{derived_scale}")
                scale_mismatches += 1
        else:
            observed_scale = annotation.get("scale_stratum")

        diagnostic = {
            "observed_lighting_tag": observed_lighting,
            "frozen_lighting_stratum": frozen_lighting,
            "observed_scale_stratum": observed_scale,
            "derived_scale_stratum": derived_scale,
            "bbox_diagonal_source_px": bbox_diagonal,
            "bbox_aspect_ratio": bbox_aspect_ratio,
            "motion_occlusion_tags": list(annotation.get("motion_occlusion_tags", [])),
            "diagnostic_codes": codes,
        }
        annotation_diagnostics[frame_index] = diagnostic
        if codes:
            excluded.append(annotation)
        elif localizable:
            positives.append(annotation)
        elif presence == "absent" and annotation.get("visibility") == "not_applicable":
            absent.append(annotation)
        else:
            excluded.append(annotation)

    top1_hits = 0
    top5_hits = 0
    false_candidates = 0
    candidate_counts: list[int] = []
    raw_candidate_counts: list[int] = []
    per_frame: list[dict[str, Any]] = []
    metric_records: list[dict[str, Any]] = []
    eligible_ids = {id(annotation) for annotation in [*positives, *absent]}
    positive_ids = {id(annotation) for annotation in positives}
    for annotation in annotations:
        frame_index = int(annotation["frame_index"])
        raw = candidates_by_frame.get(frame_index, [])
        if not isinstance(raw, list):
            raise FeasibilityError("candidate map values must be lists")
        scored = raw[: METRIC_PROFILE["candidate_budget"]]
        raw_candidate_count = (
            raw_candidate_counts_by_frame.get(frame_index, len(raw))
            if raw_candidate_counts_by_frame is not None
            else len(raw)
        )
        if (
            isinstance(raw_candidate_count, bool)
            or not isinstance(raw_candidate_count, int)
            or raw_candidate_count < len(raw)
        ):
            raise FeasibilityError("raw candidate count is inconsistent with retained Top-5 evidence")
        metric_eligible = id(annotation) in eligible_ids
        is_positive = id(annotation) in positive_ids
        false_count = 0
        if metric_eligible and is_positive:
            diagnostics = [
                _candidate_measurements(
                    annotation,
                    candidate,
                    rank=index + 1,
                    source_height=source_height,
                )
                for index, candidate in enumerate(scored)
            ]
            matching = [index for index, diagnostic in enumerate(diagnostics) if diagnostic["matched"]]
            hit1 = bool(matching and matching[0] == 0)
            hit5 = bool(matching)
            top1_hits += int(hit1)
            top5_hits += int(hit5)
            false_count = len(scored) - int(hit5)
        elif metric_eligible:
            hit1 = False
            hit5 = False
            diagnostics = [
                {
                    "rank": index + 1,
                    "matched": False,
                    "center_distance_source_px": None,
                    "iou": None,
                    "evaluation_radius_source_px": None,
                }
                for index, _candidate in enumerate(scored)
            ]
            false_count = len(scored)
        else:
            hit1 = None
            hit5 = None
            diagnostics = []
        if metric_eligible:
            candidate_counts.append(len(scored))
            raw_candidate_counts.append(raw_candidate_count)
            false_candidates += false_count
            metric_records.append(
                {
                    "presence": annotation["presence"],
                    "scale": annotation.get("scale_stratum"),
                    "lighting": annotation.get("lighting_tag"),
                    "motion_occlusion_tags": list(annotation.get("motion_occlusion_tags", [])),
                    "top1_hit": bool(hit1),
                    "top5_hit": bool(hit5),
                    "false_candidate_count": false_count,
                    "scored_candidate_count": len(scored),
                    "raw_candidate_count": raw_candidate_count,
                }
            )
        annotation_diagnostic = annotation_diagnostics[frame_index]
        per_frame.append(
            {
                "frame_index": frame_index,
                "presence": annotation["presence"],
                "metric_eligible": metric_eligible,
                "scored_candidate_count": len(scored),
                "raw_candidate_count": raw_candidate_count,
                "top1_hit": hit1,
                "top5_hit": hit5,
                "candidate_diagnostics": diagnostics,
                **annotation_diagnostic,
            }
        )

    positive_count = len(positives)
    evaluable_count = len(positives) + len(absent)
    top1_point = top1_hits / positive_count if positive_count else 0.0
    top5_point = top5_hits / positive_count if positive_count else 0.0
    false_point = false_candidates / evaluable_count if evaluable_count else 0.0
    scale_support = Counter(str(item.get("scale_stratum")) for item in positives)
    lighting_support = Counter(str(item.get("lighting_tag")) for item in positives)
    missing: list[str] = []
    if (
        len(annotations) < METRIC_PROFILE["minimum_total_frames"]
        or len(annotations) > METRIC_PROFILE["maximum_total_frames"]
    ):
        missing.append("total_frame_support")
    if positive_count < METRIC_PROFILE["minimum_localizable_positives"]:
        missing.append("localizable_positive_support")
    if len(absent) < METRIC_PROFILE["minimum_confirmed_absent"]:
        missing.append("confirmed_absent_support")
    for stratum in applicable_scale_strata:
        if scale_support[stratum] < METRIC_PROFILE["minimum_applicable_stratum_positives"]:
            missing.append(f"scale:{stratum}")
    for stratum in SCALE_STRATA:
        if stratum not in applicable_scale_strata and scale_support[stratum] > 0:
            missing.append(f"applicability_contradiction:scale:{stratum}")
    for stratum in applicable_lighting_strata:
        if lighting_support[stratum] < METRIC_PROFILE["minimum_applicable_stratum_positives"]:
            missing.append(f"lighting:{stratum}")
    for stratum in LIGHTING_STRATA:
        if stratum not in applicable_lighting_strata and lighting_support[stratum] > 0:
            missing.append(f"applicability_contradiction:lighting:{stratum}")
    if plausibility_contradictions:
        missing.append("annotation_plausibility_contradiction")
    if scale_mismatches:
        missing.append("scale_strata_mismatch")
    if lighting_mismatches:
        missing.append("lighting_strata_mismatch")

    if missing:
        status = "insufficient_evidence"
    elif top1_point >= METRIC_PROFILE["top1_recall_target"] and top5_point >= METRIC_PROFILE["top5_recall_target"]:
        status = "feasibility_passed"
    else:
        status = "feasibility_failed"

    report: dict[str, Any] = {
        "schema_version": "1.0",
        "artifact_type": "ball_feasibility_report",
        "session_id": session_id,
        "source_sha256": source_sha256,
        "locked_profile_id": locked_profile_id,
        "locked_profile_sha256": locked_profile_sha256,
        "sampling_manifest_sha256": sampling_manifest_sha256,
        "metric_profile": METRIC_PROFILE,
        "metric_profile_sha256": METRIC_PROFILE_SHA256,
        "attempt_family_sha256": attempt_family_sha256,
        "development_package_binding": development_package_binding,
        "computed_source_px_bounds": source_bounds,
        "status": status,
        "support": {
            "total_frames": len(annotations),
            "localizable_positives": positive_count,
            "confirmed_absent": len(absent),
            "excluded_or_unresolvable": len(excluded),
            "scale": {name: scale_support[name] for name in SCALE_STRATA},
            "lighting": {name: lighting_support[name] for name in LIGHTING_STRATA},
            "applicable_scale_strata": list(applicable_scale_strata),
            "applicable_lighting_strata": list(applicable_lighting_strata),
            "missing": missing,
        },
        "metrics": {
            "top1_recall": {
                "raw": {"numerator": top1_hits, "denominator": positive_count},
                "point_estimate": top1_point,
                "one_sided_95_lower": _wilson_lower(top1_hits, positive_count),
            },
            "top5_recall": {
                "raw": {"numerator": top5_hits, "denominator": positive_count},
                "point_estimate": top5_point,
                "one_sided_95_lower": _wilson_lower(top5_hits, positive_count),
            },
            "false_candidates_per_evaluable_frame": {
                "raw": {"numerator": false_candidates, "denominator": evaluable_count},
                "point_estimate": false_point,
                "one_sided_95_upper": _bounded_hoeffding_upper(false_point, evaluable_count),
            },
            "candidates_per_evaluable_frame": {
                "raw": {"numerator": sum(candidate_counts), "denominator": evaluable_count},
                "point_estimate": sum(candidate_counts) / evaluable_count if evaluable_count else 0.0,
            },
            "raw_candidates_per_evaluable_frame": {
                "raw": {"numerator": sum(raw_candidate_counts), "denominator": evaluable_count},
                "point_estimate": sum(raw_candidate_counts) / evaluable_count if evaluable_count else 0.0,
            },
        },
        "strata_metrics": _build_strata_metrics(metric_records),
        "frames": sorted(per_frame, key=lambda item: item["frame_index"]),
        "contradictions": [
            {
                "frame_index": frame["frame_index"],
                "diagnostic_codes": frame["diagnostic_codes"],
            }
            for frame in sorted(per_frame, key=lambda item: item["frame_index"])
            if frame["diagnostic_codes"]
        ],
        "resolution": {
            "requires_new_attempt": bool(plausibility_contradictions or scale_mismatches or lighting_mismatches),
            "reason_codes": [
                code
                for code, active in (
                    (
                        "annotation_plausibility_contradiction",
                        plausibility_contradictions > 0,
                    ),
                    ("scale_strata_mismatch", scale_mismatches > 0),
                    ("lighting_strata_mismatch", lighting_mismatches > 0),
                )
                if active
            ],
            "raw_annotation_plausibility_contradiction_count": plausibility_contradictions,
            "raw_scale_mismatch_count": scale_mismatches,
            "raw_lighting_mismatch_count": lighting_mismatches,
        },
        "authorizations": {
            "may_expand_to_100_300_boxes": status == "feasibility_passed",
            "trial_eligible": False,
            "source_segment_qualified": False,
            "camera_qualified": False,
            "production_approved": False,
            "full_run_authorized": False,
        },
        "limitations": [
            "one_time_directional_feasibility_only",
            "small_support_is_exploratory",
            "revealed_group_must_be_retired_for_all_profiles",
        ],
    }
    report["report_sha256"] = canonical_sha256(report)
    return report


def _computed_source_px_bounds(source_height: int) -> dict[str, float | int]:
    rule = METRIC_PROFILE["apparent_size_rule"]
    minimum_radius = float(METRIC_PROFILE["matching_rule"]["minimum_radius_source_px"])
    near_max = source_height * float(rule["near_max_source_height_multiplier"])
    return {
        "source_height_px": source_height,
        "plausible_diagonal_min_source_px": float(rule["plausible_diagonal_min_source_px"]),
        "far_diagonal_max_source_px": source_height / float(rule["far_max_source_height_divisor"]),
        "mid_diagonal_max_source_px": source_height / float(rule["mid_max_source_height_divisor"]),
        "near_diagonal_max_source_px": near_max,
        "plausible_diagonal_max_source_px": near_max,
        "aspect_ratio_min": float(rule["aspect_ratio_min"]),
        "aspect_ratio_max": float(rule["aspect_ratio_max"]),
        "matching_radius_cap_source_px": max(
            minimum_radius,
            source_height / float(METRIC_PROFILE["matching_rule"]["source_height_cap_divisor"]),
        ),
    }


def _classify_annotation_box(
    bbox: dict[str, Any],
    *,
    source_bounds: dict[str, float | int],
) -> tuple[float, float, str | None, list[str]]:
    try:
        width = float(bbox["right"]) - float(bbox["left"])
        height = float(bbox["bottom"]) - float(bbox["top"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FeasibilityError("confirmed annotation box geometry is invalid") from exc
    if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
        raise FeasibilityError("confirmed annotation box geometry is invalid")
    diagonal = math.hypot(width, height)
    aspect_ratio = width / height
    minimum = float(source_bounds["plausible_diagonal_min_source_px"])
    maximum = float(source_bounds["plausible_diagonal_max_source_px"])
    codes: list[str] = []
    if diagonal < minimum:
        codes.append("bbox_diagonal_below_minimum")
    if diagonal > maximum:
        codes.append("bbox_diagonal_above_maximum")
    if not (float(source_bounds["aspect_ratio_min"]) <= aspect_ratio <= float(source_bounds["aspect_ratio_max"])):
        codes.append("bbox_aspect_ratio_out_of_bounds")

    derived_scale: str | None = None
    tolerance = 1e-9
    if minimum <= diagonal <= maximum + tolerance:
        if diagonal <= float(source_bounds["far_diagonal_max_source_px"]) + tolerance:
            derived_scale = "far"
        elif diagonal <= float(source_bounds["mid_diagonal_max_source_px"]) + tolerance:
            derived_scale = "mid"
        else:
            derived_scale = "near"
    return diagonal, aspect_ratio, derived_scale, codes


def _stratum_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    positives = [row for row in records if row["presence"] == "present"]
    absent = [row for row in records if row["presence"] == "absent"]
    positive_count = len(positives)
    evaluable_count = len(records)
    top1_hits = sum(bool(row["top1_hit"]) for row in positives)
    top5_hits = sum(bool(row["top5_hit"]) for row in positives)
    false_total = sum(int(row["false_candidate_count"]) for row in records)
    scored_total = sum(int(row["scored_candidate_count"]) for row in records)
    raw_total = sum(int(row["raw_candidate_count"]) for row in records)
    false_point = false_total / evaluable_count if evaluable_count else 0.0
    return {
        "support": {
            "localizable_positives": positive_count,
            "confirmed_absent": len(absent),
            "evaluable_frames": evaluable_count,
        },
        "top1_recall": {
            "raw": {"numerator": top1_hits, "denominator": positive_count},
            "point_estimate": top1_hits / positive_count if positive_count else 0.0,
            "one_sided_95_lower": _wilson_lower(top1_hits, positive_count),
        },
        "top5_recall": {
            "raw": {"numerator": top5_hits, "denominator": positive_count},
            "point_estimate": top5_hits / positive_count if positive_count else 0.0,
            "one_sided_95_lower": _wilson_lower(top5_hits, positive_count),
        },
        "candidate_totals": {
            "false": false_total,
            "scored": scored_total,
            "raw": raw_total,
        },
        "false_candidates_per_evaluable_frame": {
            "raw": {"numerator": false_total, "denominator": evaluable_count},
            "point_estimate": false_point,
            "one_sided_95_upper": _bounded_hoeffding_upper(false_point, evaluable_count),
        },
        "exploratory_small_n": positive_count < int(METRIC_PROFILE["exploratory_small_n_threshold"]),
    }


def _build_strata_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    scale = {
        stratum: _stratum_metrics([row for row in records if row["presence"] == "present" and row["scale"] == stratum])
        for stratum in SCALE_STRATA
    }
    lighting = {
        stratum: _stratum_metrics([row for row in records if row["lighting"] == stratum]) for stratum in LIGHTING_STRATA
    }
    motion_occlusion = {
        stratum: _stratum_metrics(
            [
                row
                for row in records
                if row["presence"] == "present"
                and (
                    (stratum == "none" and not row["motion_occlusion_tags"]) or stratum in row["motion_occlusion_tags"]
                )
            ]
        )
        for stratum in MOTION_OCCLUSION_STRATA
    }
    return {
        "scale": scale,
        "lighting": lighting,
        "motion_occlusion": motion_occlusion,
    }


def _validate_sampling_manifest(
    manifest: dict[str, Any],
    *,
    attempt_family_sha256: str,
    development_package_sha256: str,
    source_sha256: str,
    source_frame_count: int,
    locked_profile_id: str,
    locked_profile_sha256: str,
) -> list[int]:
    if (
        manifest.get("artifact_type") != "ball_annotation_sampling_manifest"
        or manifest.get("schema_version") != "1.0"
        or manifest.get("profile_id") != TEMPORAL_GROUPING_PROFILE_ID
        or manifest.get("selection_profile_id") != TEMPORAL_BLOCK_SAMPLING_PROFILE_ID
        or manifest.get("scale_stratification_mode") != "post_reveal_support_gate_only"
        or manifest.get("lighting_stratification_mode") != "predeclared_frame_intervals_and_quota_v1"
        or manifest.get("metric_profile_id") != FEASIBILITY_METRIC_PROFILE_ID
        or manifest.get("metric_profile_sha256") != METRIC_PROFILE_SHA256
        or manifest.get("data_role") != "check"
        or manifest.get("locked_before_probe") is not True
        or manifest.get("source_sha256") != source_sha256
        or manifest.get("locked_profile_id") != locked_profile_id
        or manifest.get("locked_profile_sha256") != locked_profile_sha256
    ):
        raise FeasibilityError("sealed sampling manifest authority is invalid")
    selection_seed_sha256 = manifest.get("selection_seed_sha256")
    selection_authority = manifest.get("selection_authority")
    universe_sha256 = manifest.get("candidate_universe_sha256")
    universe_start = manifest.get("candidate_universe_start_frame")
    universe_end = manifest.get("candidate_universe_end_frame")
    universe_authority = manifest.get("candidate_universe_authority")
    require_sha256(selection_seed_sha256, "selection seed sha256")
    require_sha256(universe_sha256, "candidate universe sha256")
    applicability = manifest.get("strata_applicability")
    try:
        scale_rows = applicability["scale"]
        lighting_rows = applicability["lighting"]
        scale_by_name = {row["stratum"]: row for row in scale_rows}
        lighting_by_name = {row["stratum"]: row for row in lighting_rows}
        expected_scale_authority = [
            {"stratum": stratum, "status": scale_by_name[stratum]["status"]} for stratum in SCALE_STRATA
        ]
        expected_lighting_authority = [
            {
                "stratum": stratum,
                "status": lighting_by_name[stratum]["status"],
                "quota": lighting_by_name[stratum]["quota"],
                "frame_intervals": sorted(
                    lighting_by_name[stratum]["frame_intervals"],
                    key=lambda interval: (interval["start_frame"], interval["end_frame"]),
                ),
            }
            for stratum in LIGHTING_STRATA
        ]
    except (KeyError, TypeError) as exc:
        raise FeasibilityError("sampling selection authority is invalid") from exc
    if (
        not isinstance(selection_authority, dict)
        or set(selection_authority)
        != {
            "schema_version",
            "artifact_type",
            "attempt_family_sha256",
            "development_package_sha256",
            "source_sha256",
            "locked_profile_id",
            "locked_profile_sha256",
            "sampling_profile_id",
            "metric_profile_id",
            "metric_profile_sha256",
            "target_frame_count",
            "scale_applicability",
            "lighting_applicability",
        }
        or selection_authority.get("schema_version") != "1.0"
        or selection_authority.get("artifact_type") != "ball_annotation_sampling_selection_authority"
        or selection_authority.get("attempt_family_sha256") != attempt_family_sha256
        or selection_authority.get("development_package_sha256") != development_package_sha256
        or selection_authority.get("source_sha256") != source_sha256
        or selection_authority.get("locked_profile_id") != locked_profile_id
        or selection_authority.get("locked_profile_sha256") != locked_profile_sha256
        or selection_authority.get("sampling_profile_id") != TEMPORAL_GROUPING_PROFILE_ID
        or selection_authority.get("metric_profile_id") != FEASIBILITY_METRIC_PROFILE_ID
        or selection_authority.get("metric_profile_sha256") != METRIC_PROFILE_SHA256
        or selection_authority.get("target_frame_count") != manifest.get("target_frame_count")
        or selection_authority.get("scale_applicability") != expected_scale_authority
        or selection_authority.get("lighting_applicability") != expected_lighting_authority
        or selection_seed_sha256 != canonical_sha256(selection_authority)
    ):
        raise FeasibilityError("sampling selection authority is invalid")
    if (
        isinstance(source_frame_count, bool)
        or not isinstance(source_frame_count, int)
        or source_frame_count <= 0
        or isinstance(universe_start, bool)
        or not isinstance(universe_start, int)
        or isinstance(universe_end, bool)
        or not isinstance(universe_end, int)
        or universe_start != 0
        or universe_end != source_frame_count - 1
        or not isinstance(universe_authority, dict)
        or set(universe_authority)
        != {
            "schema_version",
            "artifact_type",
            "source_sha256",
            "start_frame",
            "end_frame",
            "candidate_frame_count",
            "grouping_profile_id",
            "selection_profile_id",
            "lighting_strata",
            "excluded_temporal_groups",
        }
        or universe_authority.get("source_sha256") != source_sha256
        or universe_authority.get("start_frame") != universe_start
        or universe_authority.get("end_frame") != universe_end
        or universe_authority.get("candidate_frame_count") != source_frame_count
        or universe_authority.get("grouping_profile_id") != TEMPORAL_GROUPING_PROFILE_ID
        or universe_authority.get("selection_profile_id") != TEMPORAL_BLOCK_SAMPLING_PROFILE_ID
        or universe_sha256 != canonical_sha256(universe_authority)
    ):
        raise FeasibilityError("frozen candidate universe authority is invalid")
    frame_indices = manifest.get("frame_indices")
    groups = manifest.get("groups")
    if (
        not isinstance(frame_indices, list)
        or not 20 <= len(frame_indices) <= 50
        or frame_indices != sorted(set(frame_indices))
        or any(
            isinstance(value, bool) or not isinstance(value, int) or not universe_start <= value <= universe_end
            for value in frame_indices
        )
        or not isinstance(groups, list)
        or len(groups) != len(frame_indices)
    ):
        raise FeasibilityError("sealed sampling frame set is invalid")
    previous_span: tuple[int, int] | None = None
    for frame_index, group in zip(frame_indices, groups, strict=True):
        lighting_stratum = group.get("pre_reveal_lighting_stratum")
        expected = {
            **temporal_group_for_frame(source_sha256, frame_index),
            "frame_index": frame_index,
            "pre_reveal_lighting_stratum": lighting_stratum,
        }
        if group != expected:
            raise FeasibilityError("sampling temporal group ancestry is not canonical")
        span = (group["start_frame"], group["end_frame"])
        if previous_span is not None and _spans_overlap(previous_span, span):
            raise FeasibilityError("sampling temporal derivative families overlap")
        previous_span = span
    applicability = manifest.get("strata_applicability")
    lighting_rows = applicability.get("lighting") if isinstance(applicability, dict) else None
    if not isinstance(lighting_rows, list):
        raise FeasibilityError("pre-reveal lighting sampling authority is missing")
    lighting_by_name = {row.get("stratum"): row for row in lighting_rows if isinstance(row, dict)}
    expected_authority_lighting = sorted(
        (
            {
                "stratum": stratum,
                "quota": row.get("quota"),
                "frame_intervals": row.get("frame_intervals"),
            }
            for stratum, row in lighting_by_name.items()
            if isinstance(row.get("quota"), int) and not isinstance(row.get("quota"), bool) and row["quota"] > 0
        ),
        key=lambda row: row["stratum"],
    )
    authority_exclusions = universe_authority.get("excluded_temporal_groups")
    if universe_authority.get("lighting_strata") != expected_authority_lighting or not isinstance(
        authority_exclusions, list
    ):
        raise FeasibilityError("frozen candidate universe authority is invalid")
    try:
        canonical_authority = build_candidate_universe_authority(
            source_sha256=source_sha256,
            start_frame=universe_start,
            end_frame=universe_end,
            lighting_strata=expected_authority_lighting,
            excluded_groups=authority_exclusions,
        )
    except Exception as exc:
        raise FeasibilityError("frozen candidate universe authority is invalid") from exc
    if canonical_authority != universe_authority:
        raise FeasibilityError("frozen candidate universe authority is invalid")
    observed_lighting = Counter(group.get("pre_reveal_lighting_stratum") for group in groups)
    expected_observed_strata = {
        stratum
        for stratum, row in lighting_by_name.items()
        if row.get("status") == "applicable"
        and isinstance(row.get("quota"), int)
        and not isinstance(row.get("quota"), bool)
        and row["quota"] > 0
    }
    if set(observed_lighting) != expected_observed_strata:
        raise FeasibilityError("sampled lighting stratum binding is invalid")
    for stratum, row in lighting_by_name.items():
        quota = row.get("quota")
        intervals = row.get("frame_intervals")
        if observed_lighting.get(stratum, 0) != quota:
            raise FeasibilityError("pre-reveal lighting quota binding is invalid")
        for group in groups:
            if group.get("pre_reveal_lighting_stratum") != stratum:
                continue
            if not isinstance(intervals, list):
                raise FeasibilityError("sampled frame is outside its pre-reveal lighting interval")
            in_declared_interval = False
            for interval in intervals:
                if not isinstance(interval, dict):
                    raise FeasibilityError("sampled frame is outside its pre-reveal lighting interval")
                start = interval.get("start_frame")
                end = interval.get("end_frame")
                if (
                    isinstance(start, bool)
                    or not isinstance(start, int)
                    or isinstance(end, bool)
                    or not isinstance(end, int)
                ):
                    raise FeasibilityError("sampled frame is outside its pre-reveal lighting interval")
                if start <= group["start_frame"] and group["end_frame"] <= end:
                    in_declared_interval = True
            if not in_declared_interval:
                raise FeasibilityError("sampled frame is outside its pre-reveal lighting interval")
    development_groups = manifest.get("excluded_development_groups")
    if not isinstance(development_groups, list):
        raise FeasibilityError("development exclusion closure is missing")
    authority_exclusions_by_id = {group["group_id"]: group for group in authority_exclusions}
    for development_group in development_groups:
        _require_complete_group_authority(
            source_sha256,
            {development_group.get("group_id")},
            [development_group],
            "development",
        )
        expected_exclusion = temporal_group_for_frame(source_sha256, development_group["seed_frame_index"])
        if authority_exclusions_by_id.get(development_group.get("group_id")) != expected_exclusion:
            raise FeasibilityError("development exclusion is missing from candidate universe authority")
        development_span = _group_span(development_group)
        if any(_spans_overlap(development_span, _group_span(group)) for group in groups):
            raise FeasibilityError("check group overlaps revealed development derivatives")
    return frame_indices


def _applicable_strata_from_manifest(manifest: dict[str, Any]) -> tuple[list[str], list[str]]:
    applicability = manifest.get("strata_applicability")
    if not isinstance(applicability, dict) or set(applicability) != {"scale", "lighting"}:
        raise FeasibilityError("pre-reveal strata applicability evidence is missing")
    applicable: dict[str, list[str]] = {"scale": [], "lighting": []}
    for dimension, expected in (("scale", SCALE_STRATA), ("lighting", LIGHTING_STRATA)):
        rows = applicability.get(dimension)
        if not isinstance(rows, list) or len(rows) != len(expected):
            raise FeasibilityError(f"pre-reveal {dimension} applicability must cover every stratum")
        by_name = {row.get("stratum"): row for row in rows if isinstance(row, dict)}
        if set(by_name) != set(expected) or len(by_name) != len(rows):
            raise FeasibilityError(f"pre-reveal {dimension} applicability is incomplete or duplicated")
        for stratum in expected:
            row = by_name[stratum]
            status = row.get("status")
            evidence = row.get("evidence")
            if status not in {"applicable", "not_applicable"} or not isinstance(evidence, dict):
                raise FeasibilityError(f"pre-reveal {dimension} applicability is invalid")
            note = evidence.get("note")
            digest = evidence.get("evidence_sha256")
            authority = {
                "dimension": dimension,
                "stratum": stratum,
                "status": status,
                "note": note,
            }
            if dimension == "lighting":
                quota = row.get("quota")
                intervals = row.get("frame_intervals")
                if (
                    isinstance(quota, bool)
                    or not isinstance(quota, int)
                    or not isinstance(intervals, list)
                    or (status == "applicable" and quota < 3)
                    or (status == "not_applicable" and (quota != 0 or intervals))
                ):
                    raise FeasibilityError("pre-reveal lighting quota authority is invalid")
                authority.update({"quota": quota, "frame_intervals": intervals})
            if (
                evidence.get("declared_before_reveal") is not True
                or not isinstance(note, str)
                or len(note.strip()) < 3
                or digest != canonical_sha256(authority)
            ):
                raise FeasibilityError(f"pre-reveal {dimension} applicability evidence is invalid")
            if status == "applicable":
                applicable[dimension].append(stratum)
    if not applicable["scale"] or not applicable["lighting"]:
        raise FeasibilityError("applicable scale and lighting strata cannot be empty")
    return applicable["scale"], applicable["lighting"]


def _validate_package_revision_truth(
    package: dict[str, Any],
    frame_indices: list[int],
) -> list[dict[str, Any]]:
    session_id = package.get("session_id")
    operator_id = package.get("operator_id")
    source = package.get("source")
    revisions = package.get("revision_chain")
    effective = package.get("effective_annotations")
    if (
        not isinstance(session_id, str)
        or not session_id
        or not isinstance(operator_id, str)
        or not operator_id
        or not isinstance(source, dict)
        or not isinstance(revisions, list)
        or not isinstance(effective, list)
    ):
        raise FeasibilityError("sealed annotation revision authority is incomplete")
    effective_by_frame = {row.get("frame_index"): row for row in effective if isinstance(row, dict)}
    if set(effective_by_frame) != set(frame_indices) or len(effective_by_frame) != len(effective):
        raise FeasibilityError("effective annotation frame set does not match the sealed sampling set")
    mutation_ids: set[str] = set()
    validated: list[dict[str, Any]] = []
    for frame_index in frame_indices:
        chain = sorted(
            [row for row in revisions if isinstance(row, dict) and row.get("frame_index") == frame_index],
            key=lambda row: row.get("revision", -1),
        )
        if not chain:
            raise FeasibilityError("every check frame requires an append-only human revision")
        previous_revision = 0
        previous_effective: dict[str, Any] | None = None
        for row in chain:
            revision = row.get("revision")
            mutation_id = row.get("mutation_id")
            if (
                revision != previous_revision + 1
                or row.get("session_id") != session_id
                or row.get("operator_id") != operator_id
                or row.get("supersedes_revision") != (previous_revision or None)
                or not isinstance(mutation_id, str)
                or mutation_id in mutation_ids
                or row.get("previous_effective_annotation") != previous_effective
            ):
                raise FeasibilityError("annotation revision chain is not append-only or operator-bound")
            current_effective = row.get("effective_annotation")
            if row.get("annotation_etag") != annotation_etag(session_id, frame_index, revision, current_effective):
                raise FeasibilityError("annotation revision ETag does not bind effective truth")
            mutation_ids.add(mutation_id)
            previous_revision = revision
            previous_effective = current_effective
        row = effective_by_frame[frame_index]
        raw_annotation = {key: value for key, value in row.items() if key != "frame_index"}
        if previous_effective != raw_annotation:
            raise FeasibilityError("effective annotation does not match the append-only revision head")
        try:
            normalized = validate_ball_annotation(
                raw_annotation,
                width=int(source["width"]),
                height=int(source["height"]),
                data_role="check",
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FeasibilityError("effective check annotation violates the truth contract") from exc
        if (
            normalized.get("annotation_state") != "confirmed"
            or normalized.get("provenance") not in HUMAN_CONFIRMED_PROVENANCE_VALUES
        ):
            raise FeasibilityError("only effective human-confirmed revisions can be scored")
        validated.append({"frame_index": frame_index, **normalized})
    if len(revisions) != sum(
        1 for row in revisions if isinstance(row, dict) and row.get("frame_index") in set(frame_indices)
    ):
        raise FeasibilityError("revision chain contains out-of-sample frames")
    return validated


def _validate_locked_probe_evidence(
    job: dict[str, Any],
    *,
    source: dict[str, Any],
    locked_profile: dict[str, Any],
    frame_indices: list[int],
    frozen_authority: Any,
    sampling_manifest_sha256: str,
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, int], str]:
    if not isinstance(job, dict) or job.get("status") != "ready" or not isinstance(job.get("report"), dict):
        raise FeasibilityError("sealed check probe is not ready")
    report = job["report"]
    if not isinstance(frozen_authority, dict):
        raise FeasibilityError("sealed check probe authority is missing")
    required_authority = {
        "job_id",
        "request_sha256",
        "intent_sha256",
        "result_manifest_sha256",
        "report_sha256",
        "parent_trial_id",
        "runtime_environment_sha256",
        "execution_bundle_sha256",
        "frozen_profiles_sha256",
        "locked_profile",
        "control_profile",
    }
    if set(frozen_authority) != required_authority:
        raise FeasibilityError("sealed check probe authority fields are invalid")
    lineage = report.get("lineage")
    frozen_request = job.get("frozen_request")
    if not isinstance(lineage, dict) or not isinstance(frozen_request, dict):
        raise FeasibilityError("check probe frozen request/lineage is missing")
    profile_ids = frozen_request.get("profile_ids")
    expected_profile_ids = sorted(
        [
            frozen_authority.get("locked_profile", {}).get("profile_id"),
            frozen_authority.get("control_profile", {}).get("profile_id"),
        ]
    )
    if (
        frozen_authority.get("job_id") != job.get("job_id")
        or frozen_authority.get("request_sha256") != job.get("request_sha256")
        or frozen_authority.get("intent_sha256") != job.get("intent_sha256")
        or frozen_authority.get("result_manifest_sha256") != job.get("result_manifest_sha256")
        or frozen_authority.get("report_sha256") != report.get("report_sha256")
        or frozen_authority.get("parent_trial_id") != lineage.get("parent_trial_id")
        or frozen_authority.get("runtime_environment_sha256") != lineage.get("runtime_environment_sha256")
        or frozen_authority.get("execution_bundle_sha256") != lineage.get("execution_bundle_sha256")
        or frozen_authority.get("frozen_profiles_sha256") != lineage.get("frozen_profiles_sha256")
        or frozen_request.get("frame_indices") != frame_indices
        or frozen_request.get("annotation_sampling_manifest_sha256") != sampling_manifest_sha256
        or profile_ids != expected_profile_ids
        or frozen_request.get("top_k", report.get("top_k")) != 5
    ):
        raise FeasibilityError("check probe server intent does not match the sealed session freeze")
    for value, label in (
        (frozen_authority.get("request_sha256"), "check request sha256"),
        (frozen_authority.get("intent_sha256"), "check intent sha256"),
        (frozen_authority.get("result_manifest_sha256"), "check result manifest sha256"),
        (frozen_authority.get("report_sha256"), "check report sha256"),
        (frozen_authority.get("runtime_environment_sha256"), "check runtime sha256"),
        (frozen_authority.get("execution_bundle_sha256"), "check execution bundle sha256"),
        (frozen_authority.get("frozen_profiles_sha256"), "check frozen profiles sha256"),
    ):
        require_sha256(value, label)
    report_sha256 = require_sha256(report.get("report_sha256"), "check probe report sha256")
    if canonical_sha256({key: value for key, value in report.items() if key != "report_sha256"}) != report_sha256:
        raise FeasibilityError("check probe report digest is invalid")
    report_source = report.get("source")
    decode = report.get("decode")
    if not isinstance(report_source, dict) or not isinstance(decode, dict):
        raise FeasibilityError("check probe source/decode binding is missing")
    for key in (
        "source_id",
        "sha256",
        "file_identity_sha256",
        "width",
        "height",
        "frame_count",
        "tracking_contract_sha256",
    ):
        if report_source.get(key) != source.get(key):
            raise FeasibilityError("check probe source does not match the sealed package")
    if (
        decode.get("verified_frame_indices") != frame_indices
        or decode.get("width") != source.get("width")
        or decode.get("height") != source.get("height")
        or decode.get("frame_count") != source.get("frame_count")
        or decode.get("fps") != source.get("fps")
        or decode.get("effective_decode_mode")
        not in {"sequential", "preroll_verified", "direct_verified", "sequential_fallback"}
    ):
        raise FeasibilityError("check probe decode/frame set does not match the sealed package")
    frozen_profiles = report.get("frozen_profiles")
    frozen = (
        next(
            (
                row
                for row in frozen_profiles
                if isinstance(row, dict) and row.get("profile_id") == locked_profile.get("profile_id")
            ),
            None,
        )
        if isinstance(frozen_profiles, list)
        else None
    )
    if (
        not isinstance(frozen, dict)
        or frozen.get("profile_sha256") != locked_profile.get("profile_sha256")
        or frozen.get("model_id") != locked_profile.get("model_id")
        or frozen.get("model_version") != locked_profile.get("model_version")
    ):
        raise FeasibilityError("check probe locked profile does not match the sealed package")
    frozen_by_id = {row.get("profile_id"): row for row in frozen_profiles if isinstance(row, dict)}
    for label, binding in (
        ("locked", frozen_authority["locked_profile"]),
        ("control", frozen_authority["control_profile"]),
    ):
        if not isinstance(binding, dict) or set(binding) != {
            "profile_id",
            "profile_sha256",
            "model_id",
            "model_version",
            "model_descriptor_sha256",
            "weights_sha256",
        }:
            raise FeasibilityError(f"sealed {label} profile binding is invalid")
        profile = frozen_by_id.get(binding["profile_id"])
        descriptor = profile.get("model_descriptor") if isinstance(profile, dict) else None
        weights = descriptor.get("weights") if isinstance(descriptor, dict) else None
        if (
            not isinstance(profile, dict)
            or profile.get("profile_sha256") != binding["profile_sha256"]
            or profile.get("model_id") != binding["model_id"]
            or profile.get("model_version") != binding["model_version"]
            or profile.get("model_descriptor_sha256") != binding["model_descriptor_sha256"]
            or not isinstance(weights, dict)
            or weights.get("sha256") != binding["weights_sha256"]
        ):
            raise FeasibilityError(f"check probe {label} model/weights binding changed")
    report_frames = report.get("frames")
    if (
        not isinstance(report_frames, list)
        or [row.get("frame_index") for row in report_frames if isinstance(row, dict)] != frame_indices
    ):
        raise FeasibilityError("check probe frame order does not match the sealed package")
    candidates_by_frame: dict[int, list[dict[str, Any]]] = {}
    raw_candidate_counts: dict[int, int] = {}
    for frame in report_frames:
        frame_index = frame["frame_index"]
        profile_results = frame.get("profile_results")
        result = (
            next(
                (
                    row
                    for row in profile_results
                    if isinstance(row, dict) and row.get("profile_id") == locked_profile.get("profile_id")
                ),
                None,
            )
            if isinstance(profile_results, list)
            else None
        )
        raw_candidates = result.get("raw_candidates") if isinstance(result, dict) else None
        candidate_count = result.get("candidate_count") if isinstance(result, dict) else None
        filter_reasons = result.get("filter_reasons") if isinstance(result, dict) else None
        display_candidate = result.get("display_candidate") if isinstance(result, dict) else None
        if (
            not isinstance(result, dict)
            or result.get("status") != "completed"
            or result.get("profile_sha256") != locked_profile.get("profile_sha256")
            or result.get("top_k") != 5
            or not isinstance(raw_candidates, list)
            or len(raw_candidates) > 5
            or isinstance(candidate_count, bool)
            or not isinstance(candidate_count, int)
            or candidate_count < len(raw_candidates)
            or candidate_count > 1_000_000
            or not isinstance(filter_reasons, dict)
            or any(
                not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for key, value in filter_reasons.items()
            )
        ):
            raise FeasibilityError("check probe locked candidate budget/count is invalid")
        deduplicated_count = candidate_count - filter_reasons.get("duplicate_suppressed_iou", 0)
        if (
            deduplicated_count < 0
            or len(raw_candidates) != min(deduplicated_count, 5)
            or filter_reasons.get("top_k_limit", 0) != max(0, deduplicated_count - 5)
            or display_candidate != (raw_candidates[0] if raw_candidates else None)
        ):
            raise FeasibilityError("check probe locked candidate accounting is inconsistent")
        previous_confidence = math.inf
        normalized_candidates = []
        for rank, candidate in enumerate(raw_candidates, start=1):
            if not isinstance(candidate, dict) or candidate.get("frame_index") != frame_index:
                raise FeasibilityError("check probe candidate frame provenance is invalid")
            confidence = candidate.get("confidence")
            bbox = candidate.get("bbox_source_px")
            if (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not math.isfinite(float(confidence))
                or not 0 <= float(confidence) <= 1
                or float(confidence) > previous_confidence
                or not isinstance(bbox, list)
                or len(bbox) != 4
                or any(
                    isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))
                    for value in bbox
                )
                or candidate.get("class_name") != "ball"
                or candidate.get("coordinate_reason") not in {"direct_source_coordinates", "sahi_tile_offset_applied"}
            ):
                raise FeasibilityError("check probe candidate rank/confidence/class provenance is invalid")
            left, top, right, bottom = (float(value) for value in bbox)
            if not (0 <= left < right <= source["width"] and 0 <= top < bottom <= source["height"]):
                raise FeasibilityError("check probe candidate coordinates are outside the source frame")
            previous_confidence = float(confidence)
            normalized_candidates.append(
                {
                    "frame_index": frame_index,
                    "rank": rank,
                    "bbox_source_px": [left, top, right, bottom],
                    "confidence": float(confidence),
                    "profile_id": locked_profile["profile_id"],
                    "profile_sha256": locked_profile["profile_sha256"],
                    "provenance": "locked_t2_raw_candidate",
                }
            )
        candidates_by_frame[frame_index] = normalized_candidates
        raw_candidate_counts[frame_index] = candidate_count
    return candidates_by_frame, raw_candidate_counts, report_sha256


def _candidate_measurements(
    annotation: dict[str, Any],
    candidate: dict[str, Any],
    *,
    rank: int,
    source_height: int,
) -> dict[str, Any]:
    bbox = annotation["bbox_source_px"]
    candidate_box = candidate.get("bbox_source_px") if isinstance(candidate, dict) else None
    if (
        not isinstance(candidate_box, (list, tuple))
        or len(candidate_box) != 4
        or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))
            for value in candidate_box
        )
    ):
        return {
            "rank": rank,
            "matched": False,
            "center_distance_source_px": None,
            "iou": None,
            "evaluation_radius_source_px": None,
        }
    left, top, right, bottom = (float(value) for value in candidate_box)
    if right <= left or bottom <= top:
        return {
            "rank": rank,
            "matched": False,
            "center_distance_source_px": None,
            "iou": None,
            "evaluation_radius_source_px": None,
        }
    truth_x = (float(bbox["left"]) + float(bbox["right"])) / 2.0
    truth_y = (float(bbox["top"]) + float(bbox["bottom"])) / 2.0
    candidate_x = (left + right) / 2.0
    candidate_y = (top + bottom) / 2.0
    diagonal = math.hypot(float(bbox["right"]) - float(bbox["left"]), float(bbox["bottom"]) - float(bbox["top"]))
    minimum_radius = float(METRIC_PROFILE["matching_rule"]["minimum_radius_source_px"])
    radius = min(
        max(
            minimum_radius,
            diagonal * float(METRIC_PROFILE["matching_rule"]["confirmed_box_diagonal_multiplier"]),
        ),
        max(
            minimum_radius,
            source_height / float(METRIC_PROFILE["matching_rule"]["source_height_cap_divisor"]),
        ),
    )
    center_distance = math.hypot(candidate_x - truth_x, candidate_y - truth_y)
    intersection_left = max(left, float(bbox["left"]))
    intersection_top = max(top, float(bbox["top"]))
    intersection_right = min(right, float(bbox["right"]))
    intersection_bottom = min(bottom, float(bbox["bottom"]))
    intersection = max(0.0, intersection_right - intersection_left) * max(0.0, intersection_bottom - intersection_top)
    candidate_area = (right - left) * (bottom - top)
    truth_area = (float(bbox["right"]) - float(bbox["left"])) * (float(bbox["bottom"]) - float(bbox["top"]))
    union = candidate_area + truth_area - intersection
    return {
        "rank": rank,
        "matched": center_distance <= radius,
        "center_distance_source_px": center_distance,
        "iou": intersection / union if union > 0 else 0.0,
        "evaluation_radius_source_px": radius,
    }


def _validate_strata(values: list[str], allowed: tuple[str, ...], label: str) -> None:
    if (
        not isinstance(values, list)
        or values != list(dict.fromkeys(values))
        or any(value not in allowed for value in values)
    ):
        raise FeasibilityError(f"applicable {label} strata are invalid")


def _group_span(group: dict[str, Any]) -> tuple[int, int]:
    if not isinstance(group, dict):
        raise FeasibilityError("temporal group registry entry must be an object")
    start = group.get("start_frame")
    end = group.get("end_frame")
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or start < 0
        or end < start
    ):
        raise FeasibilityError("temporal group registry span is invalid")
    return start, end


def _require_complete_group_authority(
    source_sha256: str,
    group_ids: set[str],
    entries: list[dict[str, Any]],
    label: str,
) -> None:
    entry_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise FeasibilityError(f"{label} temporal group registry entry must be an object")
        group_id = entry.get("group_id")
        seed_frame_index = entry.get("seed_frame_index")
        if (
            not isinstance(group_id, str)
            or entry.get("source_sha256") != source_sha256
            or entry.get("profile_id") != TEMPORAL_GROUPING_PROFILE_ID
            or isinstance(seed_frame_index, bool)
            or not isinstance(seed_frame_index, int)
            or temporal_group_for_frame(source_sha256, seed_frame_index)
            != {
                key: entry.get(key)
                for key in (
                    "group_id",
                    "profile_id",
                    "source_sha256",
                    "seed_frame_index",
                    "start_frame",
                    "end_frame",
                    "derivative_family",
                    "canonical_moment_id",
                    "derivative_family_id",
                    "ancestry_profile",
                )
            }
        ):
            raise FeasibilityError(f"{label} temporal group registry entry is invalid")
        if group_id in entry_ids:
            raise FeasibilityError(f"{label} temporal group registry contains duplicates")
        entry_ids.add(group_id)
    if entry_ids != set(group_ids):
        raise FeasibilityError(f"{label} temporal group IDs require complete span-bound registry entries")


def _spans_overlap(first: tuple[int, int], second: tuple[int, int]) -> bool:
    return first[0] <= second[1] and second[0] <= first[1]


def _wilson_lower(successes: int, total: int) -> float:
    if total <= 0:
        return 0.0
    z = 1.6448536269514722
    point = successes / total
    denominator = 1.0 + z * z / total
    center = point + z * z / (2.0 * total)
    spread = z * math.sqrt((point * (1.0 - point) + z * z / (4.0 * total)) / total)
    return max(0.0, (center - spread) / denominator)


def _bounded_hoeffding_upper(point: float, total: int) -> float:
    if total <= 0:
        return 5.0
    radius = 5.0 * math.sqrt(math.log(1.0 / 0.05) / (2.0 * total))
    return min(5.0, point + radius)


__all__ = [
    "FEASIBILITY_METRIC_PROFILE_ID",
    "FeasibilityError",
    "METRIC_PROFILE",
    "METRIC_PROFILE_SHA256",
    "TEMPORAL_BLOCK_SAMPLING_PROFILE_ID",
    "TEMPORAL_GROUPING_PROFILE_ID",
    "build_candidate_universe_authority",
    "build_feasibility_report",
    "inherit_temporal_group",
    "sample_unseen_temporal_groups",
    "temporal_group_for_frame",
    "validate_derivative_ancestry",
]
