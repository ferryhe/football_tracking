from __future__ import annotations

import gc
import math
import os
import platform
import time
import uuid
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pydantic
import pydantic_core

from football_tracking.candidate_dataset import (
    CandidateDatasetCancelled,
    CandidateDatasetError,
    decode_verified_frames,
)
from football_tracking.config import DetectorConfig, SahiConfig
from football_tracking.detector import YOLOSahiBallDetector
from football_tracking.detector_development_common import (
    CorruptProbeFrameError,
    DetectorDevelopmentError,
    canonical_sha256,
    hash_regular_file,
)
from football_tracking.media_integrity import inspect_frame, inspect_image


class ArtifactWriteError(RuntimeError):
    """Raised when derived visual evidence cannot be encoded or committed."""


def normalize_probe_candidates(
    raw_candidates: list[dict[str, Any]],
    *,
    frame_index: int,
    frame_width: int,
    frame_height: int,
    mode: str,
    class_map: dict[str, str],
    tile_origin: tuple[float, float] = (0.0, 0.0),
) -> dict[str, Any]:
    if mode not in {"direct", "sahi"}:
        raise DetectorDevelopmentError("invalid_probe_mode", "Probe mode must be direct or sahi", status_code=400)
    if frame_width <= 0 or frame_height <= 0:
        raise DetectorDevelopmentError("invalid_frame_dimensions", "Probe frame dimensions must be positive")
    if isinstance(frame_index, bool) or not isinstance(frame_index, int) or frame_index < 0:
        raise DetectorDevelopmentError("invalid_frame_index", "Probe frame_index must be a nonnegative integer")
    origin_x, origin_y = tile_origin if mode == "sahi" else (0.0, 0.0)
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0.0
        for value in (origin_x, origin_y)
    ):
        raise DetectorDevelopmentError("invalid_tile_origin", "Probe tile origin must be finite and nonnegative")
    mapped = {str(key).strip().lower(): str(value).strip().lower() for key, value in class_map.items()}
    candidates: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    for raw in raw_candidates:
        bbox = raw.get("bbox")
        confidence = raw.get("confidence")
        class_name = str(raw.get("class_name") or "").strip()
        values = [*bbox, confidence, origin_x, origin_y] if isinstance(bbox, (list, tuple)) and len(bbox) == 4 else []
        if not values or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))
            for value in values
        ):
            reasons["non_finite_candidate"] += 1
            continue
        normalized_class = class_name.lower()
        if mapped.get(normalized_class) != "ball":
            reasons["class_not_mapped"] += 1
            continue
        x1, y1, x2, y2 = (float(value) for value in bbox)
        x1 += float(origin_x)
        x2 += float(origin_x)
        y1 += float(origin_y)
        y2 += float(origin_y)
        parsed_confidence = float(confidence)
        if (
            x1 < 0.0
            or y1 < 0.0
            or x2 > float(frame_width)
            or y2 > float(frame_height)
            or x2 <= x1
            or y2 <= y1
            or not 0.0 <= parsed_confidence <= 1.0
        ):
            reasons["bbox_outside_source"] += 1
            continue
        candidates.append(
            {
                "frame_index": frame_index,
                "bbox_source_px": [x1, y1, x2, y2],
                "confidence": parsed_confidence,
                "class_name": "ball",
                "checkpoint_class_name": class_name,
                "source": str(raw.get("source") or mode),
                "coordinate_reason": ("direct_source_coordinates" if mode == "direct" else "sahi_tile_offset_applied"),
            }
        )
    return {
        "candidates": candidates,
        "rejection_reasons": dict(sorted(reasons.items())),
    }


def merge_probe_candidates(
    candidates: list[dict[str, Any]],
    *,
    top_k: int = 5,
    iou_threshold: float = 0.5,
) -> dict[str, Any]:
    if top_k != 5:
        raise DetectorDevelopmentError("invalid_top_k", "Detector probe top_k is fixed at 5", status_code=400)
    if not 0.0 <= iou_threshold <= 1.0:
        raise DetectorDevelopmentError("invalid_merge_threshold", "Detector probe IoU threshold is invalid")
    ordered = sorted(
        (deepcopy(item) for item in candidates),
        key=lambda item: (
            -float(item["confidence"]),
            tuple(float(value) for value in item["bbox_source_px"]),
            str(item.get("class_name") or ""),
            str(item.get("source") or ""),
        ),
    )
    retained: list[dict[str, Any]] = []
    duplicate_count = 0
    for candidate in ordered:
        if any(
            _intersection_over_union(candidate["bbox_source_px"], kept["bbox_source_px"]) >= iou_threshold
            for kept in retained
        ):
            duplicate_count += 1
            continue
        retained.append(candidate)
    top_k_rejected = max(0, len(retained) - top_k)
    retained = retained[:top_k]
    for candidate in retained:
        candidate["merge_reason"] = "retained_top_k"
    reasons = {}
    if duplicate_count:
        reasons["duplicate_suppressed_iou"] = duplicate_count
    if top_k_rejected:
        reasons["top_k_limit"] = top_k_rejected
    return {
        "candidates": retained,
        "display_candidate": deepcopy(retained[0]) if retained else None,
        "rejection_reasons": reasons,
    }


def run_detector_probe(
    request: dict[str, Any],
    profiles: list[dict[str, Any]],
    staging_dir: Path,
    should_cancel,
    progress,
) -> dict[str, Any]:
    """Run exact pinned profiles over one verified, source-bound bounded frame set."""

    execution_environment = probe_execution_environment()
    if request.get("_execution_environment") != execution_environment:
        raise DetectorDevelopmentError(
            "runtime_environment_changed",
            "Detector probe execution environment changed after request freeze",
        )

    source_path = Path(request["_source_path"])
    frame_indices = list(request["frame_indices"])
    width = int(request["_source_width"])
    height = int(request["_source_height"])
    frame_count = int(request["_source_frame_count"])
    try:
        frames, decode = decode_verified_frames(
            source_path,
            frame_indices,
            requested_decode_mode=str(request.get("_requested_decode_mode") or "sequential"),
            expected_width=width,
            expected_height=height,
            expected_frame_count=frame_count,
            cancel_callback=should_cancel,
        )
    except CandidateDatasetCancelled as exc:
        raise DetectorDevelopmentError("cancelled", "Detector probe was cancelled") from exc
    except CandidateDatasetError as exc:
        raise CorruptProbeFrameError(str(exc)) from exc

    timing_by_frame = {row["frame_index"]: row for row in decode["frame_timing_observations"]}

    frame_rows_by_index: dict[int, dict[str, Any]] = {}
    total = len(frame_indices) * len(profiles)
    completed = 0
    for frame_index in frame_indices:
        if should_cancel():
            raise DetectorDevelopmentError("cancelled", "Detector probe was cancelled")
        frame = frames[frame_index]
        integrity = inspect_frame(frame)
        if integrity.get("likely_corrupt") or integrity.get("low_information"):
            raise CorruptProbeFrameError(f"frame {frame_index} failed media integrity: {integrity.get('reasons')}")
        source_path_relative = Path("frames") / f"{frame_index:09d}.jpg"
        source_artifact = staging_dir / source_path_relative
        source_artifact.parent.mkdir(parents=True, exist_ok=True)
        _write_jpeg_artifact(source_artifact, frame)
        written_integrity = inspect_image(source_artifact)
        if written_integrity.get("likely_corrupt") or written_integrity.get("low_information"):
            raise CorruptProbeFrameError(
                f"frame {frame_index} encoded evidence failed integrity: {written_integrity.get('reasons')}"
            )
        frame_rows_by_index[frame_index] = {
            "frame_index": frame_index,
            "source_frame_relative_path": source_path_relative.as_posix(),
            "source_width": width,
            "source_height": height,
            "requested_decode_mode": decode["requested_decode_mode"],
            "effective_decode_mode": decode["effective_decode_mode"],
            "decoded_frame_position": frame_index,
            "decoder_reported_pos_msec": timing_by_frame[frame_index]["decoder_reported_pos_msec"],
            "decoder_timing_observation_method": timing_by_frame[frame_index]["observation_method"],
            "media_integrity": integrity,
            "profile_results": [],
        }

    # Profile-major execution keeps at most one detector/checkpoint resident.
    # The decoded frame dictionary remains explicitly bounded by the coordinator
    # to 1.2 GB for the current 50-frame, 5120x1440 operating envelope.
    for profile in profiles:
        if should_cancel():
            raise DetectorDevelopmentError("cancelled", "Detector probe was cancelled")
        detector = _build_probe_detector(request, profile)
        try:
            for frame_index in frame_indices:
                if should_cancel():
                    raise DetectorDevelopmentError("cancelled", "Detector probe was cancelled")
                frame = frames[frame_index]
                profile_id = str(profile["profile_id"])
                started = time.perf_counter()
                detected = detector.detect(frame, frame_index)
                latency_ms = (time.perf_counter() - started) * 1000.0
                raw = [
                    {
                        "bbox": [item.x1, item.y1, item.x2, item.y2],
                        "confidence": item.confidence,
                        "class_name": item.label,
                        "source": item.source,
                    }
                    for item in detected
                ]
                descriptor = profile["model_descriptor"]
                normalized = normalize_probe_candidates(
                    raw,
                    frame_index=frame_index,
                    frame_width=width,
                    frame_height=height,
                    mode=str(profile["mode"]),
                    class_map=dict(descriptor["class_map"]),
                )
                merged = merge_probe_candidates(normalized["candidates"], top_k=5, iou_threshold=0.5)
                reasons = Counter(normalized["rejection_reasons"])
                reasons.update(merged["rejection_reasons"])
                evidence = detector.last_stage_evidence
                if evidence is not None:
                    reasons.update({f"class:{key}": value for key, value in evidence.class_rejection_counts.items()})
                overlay = frame.copy()
                for candidate in merged["candidates"]:
                    x1, y1, x2, y2 = (int(round(value)) for value in candidate["bbox_source_px"])
                    cv2.rectangle(overlay, (x1, y1), (x2, y2), (0, 255, 255), 2)
                    cv2.putText(
                        overlay,
                        f"{candidate['confidence']:.3f}",
                        (x1, max(12, y1 - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.45,
                        (0, 255, 255),
                        1,
                        cv2.LINE_AA,
                    )
                overlay_relative = Path("overlays") / f"{frame_index:09d}-{profile_id}.jpg"
                overlay_path = staging_dir / overlay_relative
                overlay_path.parent.mkdir(parents=True, exist_ok=True)
                _write_jpeg_artifact(overlay_path, overlay)
                overlay_integrity = inspect_image(overlay_path)
                if overlay_integrity.get("likely_corrupt"):
                    raise CorruptProbeFrameError(f"frame {frame_index} overlay is unreadable")
                frame_rows_by_index[frame_index]["profile_results"].append(
                    {
                        "profile_id": profile_id,
                        "profile_sha256": profile["profile_sha256"],
                        "status": "completed",
                        "latency_ms": round(latency_ms, 3),
                        "candidate_count": len(normalized["candidates"]),
                        "top_k": 5,
                        "raw_candidates": merged["candidates"],
                        "display_candidate": merged["display_candidate"],
                        "filter_reasons": dict(sorted(reasons.items())),
                        "failure_code": None,
                        "raw_overlay_relative_path": overlay_relative.as_posix(),
                    }
                )
                completed += 1
                progress(completed, total)
        finally:
            del detector
            gc.collect()
            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except (ImportError, RuntimeError):
                pass
    frame_rows = [frame_rows_by_index[index] for index in frame_indices]
    return {
        "frames": frame_rows,
        "decode": decode,
        "execution": {
            "device": execution_environment["device"],
            "precision": execution_environment["precision"],
        },
    }


def _write_jpeg_artifact(path: Path, image: Any) -> None:
    try:
        encoded, payload = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 92])
    except cv2.error as exc:
        raise ArtifactWriteError("Detector probe JPEG encoding failed") from exc
    if not encoded:
        raise ArtifactWriteError("Detector probe JPEG encoding failed")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        content = payload.tobytes()
        with temporary.open("xb") as handle:
            written = handle.write(content)
            if written != len(content):
                raise ArtifactWriteError("Detector probe JPEG write was incomplete")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _build_probe_detector(request: dict[str, Any], profile: dict[str, Any]) -> YOLOSahiBallDetector:
    descriptor = profile["model_descriptor"]
    weights = descriptor["weights"]
    runtime_root = Path(request["_runtime_weights_root"]).resolve(strict=True)
    snapshot_paths = request.get("_weight_snapshot_paths")
    if not isinstance(snapshot_paths, dict) or weights["sha256"] not in snapshot_paths:
        raise DetectorDevelopmentError("weights_snapshot_missing", "Private detector weight snapshot is unavailable")
    weights_path = Path(snapshot_paths[weights["sha256"]]).resolve(strict=True)
    try:
        weights_path.relative_to(runtime_root)
    except ValueError as exc:
        raise DetectorDevelopmentError(
            "unsafe_weights_snapshot", "Private detector weight snapshot is outside its runtime root"
        ) from exc
    if weights_path.parent != runtime_root or weights_path.name != f"{weights['sha256']}.pt":
        raise DetectorDevelopmentError(
            "unsafe_weights_snapshot", "Private detector weight snapshot identity is invalid"
        )
    actual_sha256, actual_size = hash_regular_file(
        weights_path,
        "private detector profile weights",
        trusted_root=runtime_root,
    )
    if actual_sha256 != weights["sha256"] or actual_size != weights["size_bytes"]:
        raise DetectorDevelopmentError(
            "weights_digest_or_size_mismatch",
            "Frozen detector profile weights changed before runtime load",
        )
    settings = profile["settings"]
    execution_environment = request.get("_execution_environment")
    if (
        not isinstance(execution_environment, dict)
        or execution_environment.get("device") not in {"cpu", "cuda:0"}
        or execution_environment.get("precision") != "fp32"
    ):
        raise DetectorDevelopmentError(
            "runtime_environment_changed",
            "Frozen detector execution device binding is unavailable",
        )
    detector = DetectorConfig(
        model_path=weights_path,
        device=str(execution_environment["device"]),
        inference_mode="direct_full_frame" if profile["mode"] == "direct" else "sahi",
        confidence_threshold=float(settings.get("confidence_threshold", 0.05)),
        image_size=int(settings.get("image_size", 1280)),
        use_half=False,
        allowed_labels=list(settings.get("allowed_labels") or descriptor["class_map"].keys()),
    )
    sahi = SahiConfig(
        slice_height=int(settings.get("slice_height", 720)),
        slice_width=int(settings.get("slice_width", 1280)),
        overlap_height_ratio=float(settings.get("overlap_height_ratio", 0.2)),
        overlap_width_ratio=float(settings.get("overlap_width_ratio", 0.2)),
        perform_standard_pred=bool(settings.get("perform_standard_pred", False)),
        postprocess_type=str(settings.get("postprocess_type", "NMS")),
        postprocess_match_metric=str(settings.get("postprocess_match_metric", "IOS")),
        postprocess_match_threshold=float(settings.get("postprocess_match_threshold", 0.5)),
        verbose=0,
    )
    return YOLOSahiBallDetector(detector, sahi)


def probe_execution_environment() -> dict[str, Any]:
    cuda_available = False
    cuda_device_count = 0
    cuda_compiled_version: str | None = None
    cudnn_version: int | None = None
    gpu_name: str | None = None
    gpu_compute_capability: str | None = None
    gpu_total_memory_bytes: int | None = None
    cuda_driver_version: int | None = None
    opencv_build_information = cv2.getBuildInformation()
    ffmpeg_line = next(
        (
            line.split(":", 1)[1].strip().upper()
            for line in opencv_build_information.splitlines()
            if line.strip().startswith("FFMPEG:")
        ),
        "UNKNOWN",
    )
    decoder_fingerprint = {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "numpy_version": str(np.__version__),
        "opencv_version": str(cv2.__version__),
        "opencv_build_information_sha256": canonical_sha256({"build_information": opencv_build_information}),
        "opencv_ffmpeg_enabled": (True if ffmpeg_line == "YES" else False if ffmpeg_line == "NO" else None),
    }
    try:
        import torch

        cuda_available = bool(torch.cuda.is_available())
        cuda_device_count = int(torch.cuda.device_count()) if cuda_available else 0
        cuda_compiled_version = str(torch.version.cuda) if torch.version.cuda is not None else None
        raw_cudnn_version = torch.backends.cudnn.version()
        cudnn_version = int(raw_cudnn_version) if raw_cudnn_version is not None else None
        if cuda_available:
            properties = torch.cuda.get_device_properties(0)
            gpu_name = str(properties.name)
            gpu_compute_capability = f"{int(properties.major)}.{int(properties.minor)}"
            gpu_total_memory_bytes = int(properties.total_memory)
            driver_version = getattr(torch.cuda, "driver_version", None)
            if callable(driver_version):
                raw_driver_version = driver_version()
                if raw_driver_version is not None:
                    cuda_driver_version = int(raw_driver_version)
    except Exception:
        pass
    return {
        "device": "cuda:0" if cuda_available else "cpu",
        "precision": "fp32",
        "cuda_available": cuda_available,
        "cuda_device_count": cuda_device_count,
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "cuda_compiled_version": cuda_compiled_version,
        "cudnn_version": cudnn_version,
        "gpu_name": gpu_name,
        "gpu_compute_capability": gpu_compute_capability,
        "gpu_total_memory_bytes": gpu_total_memory_bytes,
        "cuda_driver_version": cuda_driver_version,
        "pydantic_version": str(pydantic.__version__),
        "pydantic_core_version": str(pydantic_core.__version__),
        **decoder_fingerprint,
        "decoder_fingerprint_sha256": canonical_sha256(decoder_fingerprint),
    }


def _intersection_over_union(first: list[float], second: list[float]) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    first_area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    second_area = max(0.0, second[2] - second[0]) * max(0.0, second[3] - second[1])
    union = first_area + second_area - intersection
    return 0.0 if union <= 0.0 else intersection / union
