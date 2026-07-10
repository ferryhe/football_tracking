from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from football_tracking.detector_candidate_contract import validate_versioned_candidate_records
from football_tracking.tracking_contracts import SCHEMA_VERSION as TRACKING_CONTRACT_SCHEMA_VERSION
from football_tracking.tracking_contracts import load_tracking_contract

DATASET_SCHEMA_VERSION = "1.0"
SOURCE_MAP_SCHEMA_VERSION = "1.0"
DATASET_MANIFEST_NAME = "candidate_dataset_manifest.json"
BUILDER_VERSION = "candidate-dataset-v1"
FRAME_OFFSETS = (-2, -1, 0, 1, 2)
TIGHT_SHAPE = (5, 3, 64, 64)
CONTEXT_SHAPE = (5, 3, 128, 128)
TIGHT_CROP_SCALE = 1.25
CONTEXT_CROP_SCALE = 4.0
PREROLL_FRAMES = 12
DECODE_MODES = ("sequential", "preroll", "direct")


class CandidateDatasetError(RuntimeError):
    """Raised when dataset inputs cannot produce a trustworthy artifact."""


class _SeekError(RuntimeError):
    pass


def build_candidate_dataset(contract_path: Path, source_map_path: Path, output_dir: Path) -> dict[str, Any]:
    contract_path = Path(contract_path).resolve()
    source_map_path = Path(source_map_path).resolve()
    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise CandidateDatasetError(f"output directory already exists: {output_dir}")

    contract_sha256 = _sha256_file(contract_path)
    contract = load_tracking_contract(contract_path)
    if _sha256_file(contract_path) != contract_sha256:
        raise CandidateDatasetError("tracking contract changed while it was being loaded")
    candidates = _validated_candidates(contract)
    source_map_sha256 = _sha256_file(source_map_path)
    source_entries = _load_source_map(source_map_path)
    if _sha256_file(source_map_path) != source_map_sha256:
        raise CandidateDatasetError("source mapping changed while it was being loaded")
    candidate_bindings = _bind_candidates(candidates, source_entries)

    ordered_candidates = sorted(
        candidates,
        key=lambda item: (
            candidate_bindings[item["candidate_id"]]["variant_id"],
            item["frame_index"],
            item["candidate_id"],
        ),
    )
    for candidate in ordered_candidates:
        entry = candidate_bindings[candidate["candidate_id"]]
        if candidate["frame_index"] >= entry["frame_count"]:
            raise CandidateDatasetError(
                f"candidate {candidate['candidate_id']!r} frame {candidate['frame_index']} is outside "
                f"variant {entry['variant_id']!r} frame count {entry['frame_count']}"
            )

    source_descriptors = _source_descriptors(source_entries)
    _validate_versioned_candidate_bindings(candidates, candidate_bindings, source_descriptors)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent))
    try:
        samples: list[dict[str, Any]] = []
        source_manifest: list[dict[str, Any]] = []
        candidates_by_variant = _group_candidates(ordered_candidates, candidate_bindings)
        for variant_id in sorted(candidates_by_variant):
            entry = source_entries[variant_id]
            source_candidates = candidates_by_variant[variant_id]
            source_samples, effective_decode_mode, actual_metadata = _extract_source_samples(
                staging_dir,
                sample_index_start=len(samples),
                source_entry=entry,
                candidates=source_candidates,
            )
            samples.extend(source_samples)
            descriptor = source_descriptors[variant_id]
            source_manifest.append(
                {
                    "path": _relative_path(entry["resolved_video_path"], output_dir),
                    "sha256": descriptor["sha256"],
                    "width": actual_metadata["width"],
                    "height": actual_metadata["height"],
                    "frame_count": actual_metadata["frame_count"],
                    "variant_id": entry["variant_id"],
                    "group_id": entry["group_id"],
                    "temporal_group": entry["temporal_group"],
                    "split_group": entry["split_group"],
                    "candidate_ids": sorted(entry["candidate_ids"]),
                    "requested_decode_mode": entry["decode_mode"],
                    "effective_decode_mode": effective_decode_mode,
                }
            )
        preprocessing_runtime = _preprocessing_runtime()
        dataset_version = _dataset_version(
            ordered_candidates,
            source_descriptors,
            samples,
            preprocessing_runtime,
        )
        manifest = {
            "schema_version": DATASET_SCHEMA_VERSION,
            "artifact_type": "candidate_dataset",
            "builder_version": BUILDER_VERSION,
            "dataset_version": dataset_version,
            "preprocessing_runtime": preprocessing_runtime,
            "contract": {
                "schema_version": TRACKING_CONTRACT_SCHEMA_VERSION,
                "path": _relative_path(contract_path, output_dir),
                "sha256": contract_sha256,
            },
            "source_mapping": {
                "schema_version": SOURCE_MAP_SCHEMA_VERSION,
                "path": _relative_path(source_map_path, output_dir),
                "sha256": source_map_sha256,
            },
            "frame_offsets": list(FRAME_OFFSETS),
            "tensor_contract": {
                "color_space": "RGB",
                "dtype": "uint8",
                "tight_shape": list(TIGHT_SHAPE),
                "context_shape": list(CONTEXT_SHAPE),
                "markup": False,
            },
            "summary": {"status": "ok", "sample_count": len(samples), "source_count": len(source_manifest)},
            "sources": source_manifest,
            "samples": samples,
        }
        _verify_inputs_unchanged(
            contract_path,
            contract_sha256,
            source_map_path,
            source_map_sha256,
            source_entries,
            source_descriptors,
        )
        _write_manifest(staging_dir / DATASET_MANIFEST_NAME, manifest)
        os.replace(staging_dir, output_dir)
        return manifest
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def _validated_candidates(contract: dict[str, Any]) -> list[dict[str, Any]]:
    if contract.get("artifact_status") != "loaded" or contract.get("validation_errors"):
        errors = contract.get("validation_errors") or [f"artifact_status={contract.get('artifact_status')}"]
        raise CandidateDatasetError(f"tracking contract is invalid: {errors}")
    if contract.get("schema_version") != TRACKING_CONTRACT_SCHEMA_VERSION:
        raise CandidateDatasetError(f"tracking contract must use schema {TRACKING_CONTRACT_SCHEMA_VERSION}")
    candidates = contract.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise CandidateDatasetError("tracking contract must contain at least one candidate")
    return candidates


def _load_source_map(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        raise CandidateDatasetError(f"source mapping is missing: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateDatasetError(f"source mapping is invalid: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != SOURCE_MAP_SCHEMA_VERSION:
        raise CandidateDatasetError(
            f"source mapping must be an object with schema_version {SOURCE_MAP_SCHEMA_VERSION!r}"
        )
    raw_sources = raw.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise CandidateDatasetError("source mapping must contain a non-empty sources list")

    result: dict[str, dict[str, Any]] = {}
    for index, raw_entry in enumerate(raw_sources):
        if not isinstance(raw_entry, dict):
            raise CandidateDatasetError(f"sources[{index}] must be an object")
        variant_id = _required_text(raw_entry.get("variant_id"), f"sources[{index}].variant_id")
        if variant_id in result:
            raise CandidateDatasetError(f"duplicate source variant: {variant_id!r}")
        candidate_ids = raw_entry.get("candidate_ids")
        if not isinstance(candidate_ids, list) or not candidate_ids:
            raise CandidateDatasetError(f"sources[{index}].candidate_ids must be a non-empty list")
        normalized_candidate_ids = [
            _required_text(candidate_id, f"sources[{index}].candidate_ids[{candidate_index}]")
            for candidate_index, candidate_id in enumerate(candidate_ids)
        ]
        if len(normalized_candidate_ids) != len(set(normalized_candidate_ids)):
            raise CandidateDatasetError(f"sources[{index}].candidate_ids contains duplicates")
        video_value = _required_text(raw_entry.get("video_path"), f"sources[{index}].video_path")
        video_path = Path(video_value)
        if video_path.is_absolute() or ".." in video_path.parts:
            raise CandidateDatasetError(f"sources[{index}].video_path must be relative and cannot traverse directories")
        mapping_root = path.parent.resolve()
        resolved_video_path = (mapping_root / video_path).resolve()
        if not resolved_video_path.is_relative_to(mapping_root):
            raise CandidateDatasetError(f"sources[{index}].video_path escapes the source mapping directory")
        if not resolved_video_path.is_file():
            raise CandidateDatasetError(f"mapped video is missing for variant {variant_id!r}: {resolved_video_path}")
        result[variant_id] = {
            "video_path": video_value,
            "resolved_video_path": resolved_video_path,
            "candidate_ids": normalized_candidate_ids,
            "video_sha256": _required_sha256(raw_entry.get("video_sha256"), f"sources[{index}].video_sha256"),
            "decode_mode": _decode_mode(raw_entry.get("decode_mode"), f"sources[{index}].decode_mode"),
            "width": _positive_int(raw_entry.get("width"), f"sources[{index}].width"),
            "height": _positive_int(raw_entry.get("height"), f"sources[{index}].height"),
            "frame_count": _positive_int(raw_entry.get("frame_count"), f"sources[{index}].frame_count"),
            "variant_id": variant_id,
            "group_id": _required_text(raw_entry.get("group_id"), f"sources[{index}].group_id"),
            "temporal_group": _required_text(raw_entry.get("temporal_group"), f"sources[{index}].temporal_group"),
            "split_group": _required_text(raw_entry.get("split_group"), f"sources[{index}].split_group"),
        }
    return result


def _bind_candidates(
    candidates: list[dict[str, Any]],
    entries: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    bindings: dict[str, dict[str, Any]] = {}
    for entry in entries.values():
        for candidate_id in entry["candidate_ids"]:
            if candidate_id in bindings:
                raise CandidateDatasetError(f"candidate {candidate_id!r} is bound to multiple source variants")
            bindings[candidate_id] = entry
    contract_ids = {candidate["candidate_id"] for candidate in candidates}
    mapping_ids = set(bindings)
    missing = sorted(contract_ids - mapping_ids)
    dangling = sorted(mapping_ids - contract_ids)
    if missing or dangling:
        raise CandidateDatasetError(f"candidate source binding mismatch: missing={missing}, dangling={dangling}")
    return bindings


def _source_descriptors(entries: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    content_groups: dict[str, tuple[str, str, str]] = {}
    for variant_id, entry in entries.items():
        actual_sha256 = _sha256_file(entry["resolved_video_path"])
        if actual_sha256 != entry["video_sha256"]:
            raise CandidateDatasetError(
                f"video_sha256 mismatch for variant {variant_id!r}: "
                f"expected {entry['video_sha256']}, actual {actual_sha256}"
            )
        group_key = (entry["group_id"], entry["split_group"])
        prior = content_groups.get(actual_sha256)
        if prior is not None and group_key != prior[:2]:
            raise CandidateDatasetError(
                f"identical video sha256 must share group_id and split_group: variants {prior[2]!r} and {variant_id!r}"
            )
        content_groups[actual_sha256] = (*group_key, variant_id)
        result[variant_id] = {
            "sha256": actual_sha256,
            "width": entry["width"],
            "height": entry["height"],
            "frame_count": entry["frame_count"],
            "decode_mode": entry["decode_mode"],
            "variant_id": entry["variant_id"],
            "group_id": entry["group_id"],
            "temporal_group": entry["temporal_group"],
            "split_group": entry["split_group"],
            "candidate_ids": sorted(entry["candidate_ids"]),
        }
    return result


def _validate_versioned_candidate_bindings(
    candidates: list[dict[str, Any]],
    bindings: dict[str, dict[str, Any]],
    source_descriptors: dict[str, dict[str, Any]],
) -> None:
    candidates_by_video_sha256: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        entry = bindings[candidate["candidate_id"]]
        video_sha256 = source_descriptors[entry["variant_id"]]["sha256"]
        candidates_by_video_sha256.setdefault(video_sha256, []).append(candidate)

    for video_sha256, source_candidates in candidates_by_video_sha256.items():
        try:
            validate_versioned_candidate_records(source_candidates, video_sha256)
        except ValueError as exc:
            raise CandidateDatasetError(f"source-scoped candidate ID validation failed: {exc}") from exc


def _dataset_version(
    candidates: list[dict[str, Any]],
    sources: dict[str, dict[str, Any]],
    samples: list[dict[str, Any]],
    preprocessing_runtime: dict[str, Any],
) -> str:
    payload = {
        "builder_version": BUILDER_VERSION,
        "frame_offsets": FRAME_OFFSETS,
        "tight_shape": TIGHT_SHAPE,
        "context_shape": CONTEXT_SHAPE,
        "candidates": candidates,
        "sources": sources,
        "preprocessing_runtime": preprocessing_runtime,
        "artifact_hashes": [
            {
                "sample_id": sample["sample_id"],
                "artifacts": {name: artifact["sha256"] for name, artifact in sorted(sample["artifacts"].items())},
            }
            for sample in samples
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _group_candidates(
    candidates: list[dict[str, Any]],
    bindings: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        variant_id = bindings[candidate["candidate_id"]]["variant_id"]
        result.setdefault(variant_id, []).append(candidate)
    return result


def _extract_source_samples(
    staging_dir: Path,
    *,
    sample_index_start: int,
    source_entry: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str, dict[str, int]]:
    video_path = source_entry["resolved_video_path"]
    captures: list[Any] = []
    direct = cv2.VideoCapture(str(video_path))
    captures.append(direct)
    try:
        metadata = _validate_capture(
            direct,
            video_path,
            expected_width=source_entry["width"],
            expected_height=source_entry["height"],
            expected_frame_count=source_entry["frame_count"],
        )
        if source_entry["decode_mode"] == "sequential":
            return (
                _stream_sequential_samples(
                    direct,
                    staging_dir,
                    sample_index_start=sample_index_start,
                    source_entry=source_entry,
                    candidates=candidates,
                ),
                "sequential",
                metadata,
            )
        generated: list[dict[str, Any]] = []
        try:
            generated = _stream_seek_samples(
                direct,
                staging_dir,
                sample_index_start=sample_index_start,
                source_entry=source_entry,
                candidates=candidates,
                mode=source_entry["decode_mode"],
                generated=generated,
            )
            effective_mode = "preroll_verified" if source_entry["decode_mode"] == "preroll" else "direct_verified"
            return generated, effective_mode, metadata
        except _SeekError:
            _remove_generated_samples(staging_dir, generated)
            direct.release()
            fallback = cv2.VideoCapture(str(video_path))
            captures.append(fallback)
            _validate_capture(
                fallback,
                video_path,
                expected_width=source_entry["width"],
                expected_height=source_entry["height"],
                expected_frame_count=source_entry["frame_count"],
            )
            return (
                _stream_sequential_samples(
                    fallback,
                    staging_dir,
                    sample_index_start=sample_index_start,
                    source_entry=source_entry,
                    candidates=candidates,
                ),
                "sequential_fallback",
                metadata,
            )
    except CandidateDatasetError:
        raise
    except Exception as exc:
        raise CandidateDatasetError(f"failed to decode source video {video_path}: {exc}") from exc
    finally:
        for capture in captures:
            capture.release()


def _stream_sequential_samples(
    capture: Any,
    staging_dir: Path,
    *,
    sample_index_start: int,
    source_entry: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not math.isclose(float(capture.get(cv2.CAP_PROP_POS_FRAMES)), 0.0, abs_tol=0.25):
        raise CandidateDatasetError("sequential decode did not start at frame 0")
    ordered = sorted(candidates, key=lambda item: (item["frame_index"], item["candidate_id"]))
    frame_count = source_entry["frame_count"]
    final_index = min(frame_count - 1, ordered[-1]["frame_index"] + 2)
    cache: dict[int, np.ndarray] = {}
    generated: list[dict[str, Any]] = []
    pending_index = 0
    for frame_index in range(final_index + 1):
        ok, frame = capture.read()
        if not ok or frame is None:
            raise CandidateDatasetError(f"sequential decode ended before required frame {frame_index}")
        cache[frame_index] = _validate_frame(frame, frame_index, source_entry["width"], source_entry["height"])
        while len(cache) > len(FRAME_OFFSETS):
            del cache[min(cache)]
        _observe_frame_cache(len(cache))
        while (
            pending_index < len(ordered)
            and min(frame_count - 1, ordered[pending_index]["frame_index"] + 2) <= frame_index
        ):
            candidate = ordered[pending_index]
            generated.append(
                _write_sample(
                    staging_dir,
                    sample_index=sample_index_start + pending_index,
                    candidate=candidate,
                    source_entry=source_entry,
                    decoded_frames=cache,
                )
            )
            pending_index += 1
    if pending_index != len(ordered):
        raise CandidateDatasetError("sequential decode did not emit every candidate")
    return generated


def _stream_seek_samples(
    capture: Any,
    staging_dir: Path,
    *,
    sample_index_start: int,
    source_entry: dict[str, Any],
    candidates: list[dict[str, Any]],
    mode: str,
    generated: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    for candidate_index, candidate in enumerate(candidates):
        indices = _candidate_actual_indices(candidate, source_entry["frame_count"])
        if mode == "preroll":
            frames = _read_candidate_preroll(capture, indices, source_entry["width"], source_entry["height"])
        else:
            frames = _read_candidate_direct(capture, indices, source_entry["width"], source_entry["height"])
        generated.append(
            _write_sample(
                staging_dir,
                sample_index=sample_index_start + candidate_index,
                candidate=candidate,
                source_entry=source_entry,
                decoded_frames=frames,
            )
        )
    return generated


def _candidate_actual_indices(candidate: dict[str, Any], frame_count: int) -> list[int]:
    return sorted({min(frame_count - 1, max(0, candidate["frame_index"] + offset)) for offset in FRAME_OFFSETS})


def _validate_capture(
    capture: Any,
    video_path: Path,
    *,
    expected_width: int,
    expected_height: int,
    expected_frame_count: int,
) -> dict[str, int]:
    if not capture.isOpened():
        raise CandidateDatasetError(f"unable to open mapped video: {video_path}")
    actual = {
        "width": _capture_positive_int(capture, cv2.CAP_PROP_FRAME_WIDTH, "width"),
        "height": _capture_positive_int(capture, cv2.CAP_PROP_FRAME_HEIGHT, "height"),
        "frame_count": _capture_positive_int(capture, cv2.CAP_PROP_FRAME_COUNT, "frame_count"),
    }
    expected = {"width": expected_width, "height": expected_height, "frame_count": expected_frame_count}
    if actual != expected:
        raise CandidateDatasetError(f"source metadata mismatch for {video_path}: expected={expected}, actual={actual}")
    return actual


def _read_candidate_direct(capture: Any, indices: list[int], width: int, height: int) -> dict[int, np.ndarray]:
    result: dict[int, np.ndarray] = {}
    for index in indices:
        if not capture.set(cv2.CAP_PROP_POS_FRAMES, index):
            raise _SeekError(f"seek to frame {index} failed")
        if not math.isclose(float(capture.get(cv2.CAP_PROP_POS_FRAMES)), float(index), abs_tol=0.25):
            raise _SeekError(f"seek to frame {index} was not verified")
        ok, frame = capture.read()
        if not ok or frame is None:
            raise _SeekError(f"frame {index} could not be decoded after seek")
        if not math.isclose(float(capture.get(cv2.CAP_PROP_POS_FRAMES)), float(index + 1), abs_tol=0.25):
            raise _SeekError(f"decoded frame position {index} was not verified")
        result[index] = _validate_frame(frame, index, width, height)
        _observe_frame_cache(len(result))
    return result


def _read_candidate_preroll(capture: Any, indices: list[int], width: int, height: int) -> dict[int, np.ndarray]:
    start = max(0, min(indices) - PREROLL_FRAMES)
    if not capture.set(cv2.CAP_PROP_POS_FRAMES, start):
        raise _SeekError(f"preroll seek to frame {start} failed")
    if not math.isclose(float(capture.get(cv2.CAP_PROP_POS_FRAMES)), float(start), abs_tol=0.25):
        raise _SeekError(f"preroll seek to frame {start} was not verified")
    required = set(indices)
    result: dict[int, np.ndarray] = {}
    for index in range(start, max(indices) + 1):
        ok, frame = capture.read()
        if not ok or frame is None:
            raise _SeekError(f"preroll decode ended before required frame {index}")
        expected_position = index + 1
        if not math.isclose(float(capture.get(cv2.CAP_PROP_POS_FRAMES)), float(expected_position), abs_tol=0.25):
            raise _SeekError(f"preroll decoded frame position {index} was not verified")
        if index in required:
            result[index] = _validate_frame(frame, index, width, height)
            _observe_frame_cache(len(result))
    return result


def _remove_generated_samples(staging_dir: Path, samples: list[dict[str, Any]]) -> None:
    for sample in samples:
        shutil.rmtree(staging_dir / "samples" / sample["sample_id"], ignore_errors=True)


def _observe_frame_cache(retained_frame_count: int) -> None:
    del retained_frame_count


def _validate_frame(frame: Any, index: int, width: int, height: int) -> np.ndarray:
    if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8:
        raise CandidateDatasetError(f"frame {index} must be a uint8 array")
    if frame.shape != (height, width, 3):
        raise CandidateDatasetError(f"frame {index} shape mismatch: expected {(height, width, 3)}, got {frame.shape}")
    return frame.copy()


def _write_sample(
    staging_dir: Path,
    *,
    sample_index: int,
    candidate: dict[str, Any],
    source_entry: dict[str, Any],
    decoded_frames: dict[int, np.ndarray],
) -> dict[str, Any]:
    frame_records = []
    temporal_frames = []
    for offset in FRAME_OFFSETS:
        requested = candidate["frame_index"] + offset
        actual = min(source_entry["frame_count"] - 1, max(0, requested))
        frame_records.append(
            {
                "offset": offset,
                "requested_index": requested,
                "actual_index": actual,
                "padding": "nearest_edge" if actual != requested else None,
            }
        )
        temporal_frames.append(decoded_frames[actual])

    bbox = _clamp_bbox(candidate["bbox"], source_entry["width"], source_entry["height"], candidate["candidate_id"])
    tight_window = _crop_window(source_entry["width"], source_entry["height"], bbox, TIGHT_CROP_SCALE)
    context_window = _crop_window(source_entry["width"], source_entry["height"], bbox, CONTEXT_CROP_SCALE)
    tight = _tensor_from_frames(temporal_frames, tight_window, size=64)
    context = _tensor_from_frames(temporal_frames, context_window, size=128)
    sample_id = f"{sample_index:06d}-{_safe_id(candidate['candidate_id'])}"
    sample_dir = staging_dir / "samples" / sample_id
    sample_dir.mkdir(parents=True, exist_ok=False)
    tight_path = sample_dir / "tight.npy"
    context_path = sample_dir / "context.npy"
    montage_path = sample_dir / "review_montage.png"
    np.save(tight_path, tight, allow_pickle=False)
    np.save(context_path, context, allow_pickle=False)
    _write_review_montage(montage_path, context, frame_records)
    return {
        "sample_id": sample_id,
        "candidate_id": candidate["candidate_id"],
        "detector_source": candidate["source"],
        "frame_index": candidate["frame_index"],
        "frames": frame_records,
        "bbox_requested_pixels": [float(value) for value in candidate["bbox"]],
        "bbox_clamped_pixels": list(bbox),
        "bbox_normalized": [
            round(bbox[0] / source_entry["width"], 8),
            round(bbox[1] / source_entry["height"], 8),
            round(bbox[2] / source_entry["width"], 8),
            round(bbox[3] / source_entry["height"], 8),
        ],
        "confidence": candidate["confidence"],
        "crop_windows": {
            "tight_pixels": list(tight_window),
            "context_pixels": list(context_window),
        },
        "variant_id": source_entry["variant_id"],
        "group_id": source_entry["group_id"],
        "temporal_group": source_entry["temporal_group"],
        "split_group": source_entry["split_group"],
        "artifacts": {
            "tight_tensor": _artifact(staging_dir, tight_path, shape=TIGHT_SHAPE, color_space="RGB"),
            "context_tensor": _artifact(staging_dir, context_path, shape=CONTEXT_SHAPE, color_space="RGB"),
            "review_montage": _artifact(staging_dir, montage_path, color_space="RGB"),
        },
    }


def _clamp_bbox(bbox: list[float], width: int, height: int, candidate_id: str) -> tuple[float, float, float, float]:
    x1 = min(float(width), max(0.0, float(bbox[0])))
    y1 = min(float(height), max(0.0, float(bbox[1])))
    x2 = min(float(width), max(0.0, float(bbox[2])))
    y2 = min(float(height), max(0.0, float(bbox[3])))
    if x2 <= x1 or y2 <= y1:
        raise CandidateDatasetError(f"candidate {candidate_id!r} bbox is empty after resolution clamp")
    return x1, y1, x2, y2


def _tensor_from_frames(
    frames: list[np.ndarray],
    crop_window: tuple[int, int, int, int],
    *,
    size: int,
) -> np.ndarray:
    tensors = []
    for frame in frames:
        x1, y1, x2, y2 = crop_window
        crop = frame[y1:y2, x1:x2]
        interpolation = cv2.INTER_AREA if crop.shape[0] > size or crop.shape[1] > size else cv2.INTER_LINEAR
        resized = cv2.resize(crop, (size, size), interpolation=interpolation)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        tensors.append(np.transpose(rgb, (2, 0, 1)))
    return np.stack(tensors).astype(np.uint8, copy=False)


def _crop_window(
    frame_width: int,
    frame_height: int,
    bbox: tuple[float, float, float, float],
    scale: float,
) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = bbox
    center_x = (x1 + x2) / 2.0
    center_y = (y1 + y2) / 2.0
    side = max(x2 - x1, y2 - y1) * scale
    crop_x1 = max(0, int(math.floor(center_x - side / 2.0)))
    crop_y1 = max(0, int(math.floor(center_y - side / 2.0)))
    crop_x2 = min(frame_width, int(math.ceil(center_x + side / 2.0)))
    crop_y2 = min(frame_height, int(math.ceil(center_y + side / 2.0)))
    if crop_x2 <= crop_x1 or crop_y2 <= crop_y1:
        raise CandidateDatasetError("resolution-relative crop is empty")
    return crop_x1, crop_y1, crop_x2, crop_y2


def _write_review_montage(path: Path, context: np.ndarray, frames: list[dict[str, Any]]) -> None:
    tiles = []
    for tensor, frame_record in zip(context, frames):
        rgb = np.transpose(tensor, (1, 2, 0))
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        label = f"{frame_record['requested_index']}->{frame_record['actual_index']}"
        cv2.putText(bgr, label, (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1, cv2.LINE_AA)
        tiles.append(bgr)
    if not cv2.imwrite(str(path), np.concatenate(tiles, axis=1)):
        raise CandidateDatasetError(f"failed to write review montage: {path}")


def _artifact(
    root: Path,
    path: Path,
    *,
    shape: tuple[int, ...] | None = None,
    color_space: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": path.relative_to(root).as_posix(),
        "sha256": _sha256_file(path),
        "size_bytes": path.stat().st_size,
    }
    if shape is not None:
        result.update({"shape": list(shape), "dtype": "uint8"})
    if color_space is not None:
        result["color_space"] = color_space
    return result


def _verify_inputs_unchanged(
    contract_path: Path,
    contract_sha256: str,
    source_map_path: Path,
    source_map_sha256: str,
    source_entries: dict[str, dict[str, Any]],
    source_descriptors: dict[str, dict[str, Any]],
) -> None:
    if _sha256_file(contract_path) != contract_sha256:
        raise CandidateDatasetError("tracking contract changed during dataset extraction")
    if _sha256_file(source_map_path) != source_map_sha256:
        raise CandidateDatasetError("source mapping changed during dataset extraction")
    for variant_id, entry in source_entries.items():
        if _sha256_file(entry["resolved_video_path"]) != source_descriptors[variant_id]["sha256"]:
            raise CandidateDatasetError(f"source video changed during dataset extraction: {variant_id!r}")


def _preprocessing_runtime() -> dict[str, Any]:
    return {
        "pipeline": BUILDER_VERSION,
        "frame_offsets": list(FRAME_OFFSETS),
        "tight_crop_scale": TIGHT_CROP_SCALE,
        "context_crop_scale": CONTEXT_CROP_SCALE,
        "tight_size": 64,
        "context_size": 128,
        "color_conversion": "opencv:BGR2RGB",
        "resize_down": "INTER_AREA",
        "resize_up": "INTER_LINEAR",
        "python": platform.python_version(),
        "numpy": np.__version__,
        "opencv": cv2.__version__,
    }


def _capture_positive_int(capture: Any, prop: int, name: str) -> int:
    value = float(capture.get(prop))
    rounded = int(round(value)) if math.isfinite(value) else 0
    if rounded <= 0 or not math.isclose(value, rounded, abs_tol=0.01):
        raise CandidateDatasetError(f"source {name} must be a positive integer, got {value}")
    return rounded


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CandidateDatasetError(f"{field} must be a non-empty string")
    return value.strip()


def _required_sha256(value: Any, field: str) -> str:
    text = _required_text(value, field).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise CandidateDatasetError(f"{field} must be a 64-character hexadecimal digest")
    return text


def _decode_mode(value: Any, field: str) -> str:
    mode = _required_text(value, field)
    if mode not in DECODE_MODES:
        raise CandidateDatasetError(f"{field} must be one of {DECODE_MODES}")
    return mode


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise CandidateDatasetError(f"{field} must be a positive integer")
    try:
        parsed_float = float(value)
        parsed = int(parsed_float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CandidateDatasetError(f"{field} must be a positive integer") from exc
    if not math.isfinite(parsed_float) or parsed_float != parsed or parsed <= 0:
        raise CandidateDatasetError(f"{field} must be a positive integer")
    return parsed


def _safe_id(value: str) -> str:
    safe = "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in value)
    return safe.strip("_") or "candidate"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_path(path: Path, base: Path) -> str:
    return Path(os.path.relpath(path, base)).as_posix()


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CandidateDatasetError(f"argument error: {message}")


def main(argv: list[str] | None = None) -> int:
    parser = _JsonArgumentParser(description="Build an atomic candidate tensor dataset from a V2 tracking contract.")
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--source-map", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    try:
        args = parser.parse_args(argv)
        manifest = build_candidate_dataset(args.contract, args.source_map, args.output_dir)
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}},
                ensure_ascii=False,
                allow_nan=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "manifest": str(Path(args.output_dir) / DATASET_MANIFEST_NAME),
                "dataset_version": manifest["dataset_version"],
                "sample_count": manifest["summary"]["sample_count"],
            },
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
