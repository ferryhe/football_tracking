from __future__ import annotations

import hashlib
import math
from copy import deepcopy
from typing import Any

import cv2
import numpy as np

from football_tracking.ball_detector_feasibility import inherit_temporal_group
from football_tracking.detector_development_common import canonical_sha256

TRACKER_PROFILE_ID = "tiny_ball_bounded_template_flow_v1"
TRACKER_PROFILE = {
    "profile_id": TRACKER_PROFILE_ID,
    "version": "1.0",
    "radius_frames_max": 2,
    "search_radius_source_px": 24,
    "minimum_match_score": 0.55,
    "minimum_backward_match_score": 0.55,
    "maximum_forward_backward_error_px": 1.5,
}
TRACKER_PROFILE_SHA256 = canonical_sha256(TRACKER_PROFILE)


class PropagationError(ValueError):
    """A bounded advisory propagation request is unsafe or invalid."""


def build_advisory_suggestions(
    *,
    seed_frame_index: int,
    seed_group: dict[str, Any],
    seed_annotation: dict[str, Any],
    radius_frames: int,
    source_frame_count: int,
    seed_frame_bytes: bytes,
    target_frame_bytes: dict[int, bytes],
    source_width: int,
    source_height: int,
) -> dict[str, Any]:
    """Track a confirmed seed on verified adjacent frames.

    Results remain advisory. A failed self-check emits no coordinates, and no
    result becomes annotation truth without a later human revision.
    """

    target_indices = _validate_request(
        seed_frame_index=seed_frame_index,
        seed_group=seed_group,
        seed_annotation=seed_annotation,
        radius_frames=radius_frames,
        source_frame_count=source_frame_count,
        target_frame_bytes=target_frame_bytes,
        source_width=source_width,
        source_height=source_height,
    )
    seed_gray = _decode_gray(seed_frame_bytes, source_width, source_height, "seed frame")
    center = _annotation_center(seed_annotation)
    patch_radius = _patch_radius(seed_annotation)
    directions = {
        "backward": sorted(
            (index for index in target_indices if index < seed_frame_index),
            reverse=True,
        ),
        "forward": sorted(index for index in target_indices if index > seed_frame_index),
    }
    frame_results: list[dict[str, Any]] = []
    suggestions: list[dict[str, Any]] = []
    succeeded_by_direction: dict[str, int] = {"backward": 0, "forward": 0}
    attempted_by_direction: dict[str, int] = {"backward": 0, "forward": 0}
    for direction, indices in directions.items():
        previous_gray = seed_gray
        previous_center = center
        direction_failed = False
        for frame_index in indices:
            digest = hashlib.sha256(target_frame_bytes[frame_index]).hexdigest()
            if direction_failed:
                frame_results.append(
                    _failed_result(
                        frame_index,
                        direction,
                        digest,
                        "stopped_after_nearer_frame_failed",
                    )
                )
                continue
            attempted_by_direction[direction] += 1
            current_gray = _decode_gray(
                target_frame_bytes[frame_index],
                source_width,
                source_height,
                f"target frame {frame_index}",
            )
            tracked = _track_one_step(
                previous_gray,
                current_gray,
                previous_center,
                patch_radius,
            )
            if tracked["status"] != "success":
                frame_results.append(
                    _failed_result(
                        frame_index,
                        direction,
                        digest,
                        tracked["failure_code"],
                        **tracked["metrics"],
                    )
                )
                direction_failed = True
                continue
            current_center = tracked["center"]
            translated = _translate_geometry(
                seed_annotation,
                dx=current_center[0] - center[0],
                dy=current_center[1] - center[1],
                width=source_width,
                height=source_height,
            )
            if translated is None:
                frame_results.append(
                    _failed_result(
                        frame_index,
                        direction,
                        digest,
                        "translated_geometry_outside_source",
                        **tracked["metrics"],
                    )
                )
                direction_failed = True
                continue
            inherited_group = inherit_temporal_group(
                seed_group,
                artifact_type="propagation",
                artifact_id=(f"propagation-{frame_index}-{digest[:16]}-{TRACKER_PROFILE_SHA256[:16]}"),
            )
            suggestion_id = (
                "propagation-suggestion-"
                + canonical_sha256(
                    {
                        "seed_frame_index": seed_frame_index,
                        "target_frame_index": frame_index,
                        "temporal_group_id": seed_group["group_id"],
                        "tracker_profile_sha256": TRACKER_PROFILE_SHA256,
                        "source_frame_sha256": digest,
                        "geometry": translated,
                    }
                )[:24]
            )
            suggestion = {
                "suggestion_id": suggestion_id,
                "frame_index": frame_index,
                "temporal_group_id": seed_group["group_id"],
                "temporal_group": inherited_group,
                **translated,
                "presence": "present",
                "visibility": seed_annotation.get("visibility", "visible"),
                "training_use": "excluded",
                "annotation_state": "suggested",
                "provenance": TRACKER_PROFILE_ID,
                "source_frame_sha256": digest,
                "self_check": deepcopy(tracked["metrics"]),
                "pending_human_confirmation": True,
            }
            suggestions.append(suggestion)
            frame_results.append(
                {
                    "frame_index": frame_index,
                    "direction": direction,
                    "status": "success",
                    "failure_code": None,
                    "source_frame_sha256": digest,
                    "suggestion_id": suggestion_id,
                    **tracked["metrics"],
                    "pending_human_confirmation": True,
                }
            )
            succeeded_by_direction[direction] += 1
            previous_gray = current_gray
            previous_center = current_center
    frame_results.sort(key=lambda row: row["frame_index"])
    suggestions.sort(key=lambda row: row["frame_index"])
    attempted = sum(attempted_by_direction.values())
    succeeded = sum(succeeded_by_direction.values())
    safe_window = max(succeeded_by_direction.values(), default=0)
    return {
        "tracker_profile": {**TRACKER_PROFILE, "profile_sha256": TRACKER_PROFILE_SHA256},
        "frame_results": frame_results,
        "suggestions": suggestions,
        "summary": {
            "attempted_by_direction": attempted_by_direction,
            "succeeded_by_direction": succeeded_by_direction,
            "attempted_frame_count": attempted,
            "succeeded_frame_count": succeeded,
            "self_check_coverage": succeeded / len(target_indices),
            "self_checked_max_safe_window_frames": safe_window,
            "human_validated_frame_count": 0,
            "human_validated_center_error_px": None,
            "human_validated_iou": None,
            "human_validated_safe_span_frames": None,
            "pending_human_confirmation": succeeded > 0,
        },
    }


def _validate_request(
    *,
    seed_frame_index: int,
    seed_group: dict[str, Any],
    seed_annotation: dict[str, Any],
    radius_frames: int,
    source_frame_count: int,
    target_frame_bytes: dict[int, bytes],
    source_width: int,
    source_height: int,
) -> list[int]:
    if isinstance(seed_frame_index, bool) or not isinstance(seed_frame_index, int) or seed_frame_index < 0:
        raise PropagationError("seed frame index is invalid")
    if isinstance(source_frame_count, bool) or not isinstance(source_frame_count, int) or source_frame_count <= 0:
        raise PropagationError("source frame count is invalid")
    if isinstance(radius_frames, bool) or not isinstance(radius_frames, int) or not 1 <= radius_frames <= 2:
        raise PropagationError("propagation radius must be between one and two frames")
    if (
        isinstance(source_width, bool)
        or not isinstance(source_width, int)
        or source_width <= 0
        or isinstance(source_height, bool)
        or not isinstance(source_height, int)
        or source_height <= 0
    ):
        raise PropagationError("source dimensions are invalid")
    if not isinstance(seed_group, dict):
        raise PropagationError("frozen temporal group is required")
    group_id = seed_group.get("group_id")
    start = seed_group.get("start_frame")
    end = seed_group.get("end_frame")
    if (
        not isinstance(group_id, str)
        or len(group_id) != 64
        or isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or not 0 <= start <= seed_frame_index <= end
    ):
        raise PropagationError("frozen temporal group is invalid")
    if not isinstance(seed_annotation, dict):
        raise PropagationError("seed annotation is required")
    point = seed_annotation.get("point_source_px")
    box = seed_annotation.get("bbox_source_px")
    if point is None and box is None:
        raise PropagationError("propagation requires a confirmed point or box")
    target_start = max(0, seed_frame_index - radius_frames)
    target_end = min(source_frame_count - 1, seed_frame_index + radius_frames)
    if target_start < start or target_end > end:
        raise PropagationError("propagation must remain within the frozen temporal group")
    expected = [frame_index for frame_index in range(target_start, target_end + 1) if frame_index != seed_frame_index]
    if not isinstance(target_frame_bytes, dict) or set(target_frame_bytes) != set(expected):
        raise PropagationError("every bounded target frame requires verified bytes")
    if any(not isinstance(value, bytes) or not value for value in target_frame_bytes.values()):
        raise PropagationError("verified target frame bytes are invalid")
    return expected


def _decode_gray(content: bytes, width: int, height: int, label: str) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None or image.shape != (height, width):
        raise PropagationError(f"{label} is not a verified source-size image")
    return image


def _annotation_center(annotation: dict[str, Any]) -> tuple[float, float]:
    point = annotation.get("point_source_px")
    if isinstance(point, dict):
        return float(point["x"]), float(point["y"])
    box = annotation["bbox_source_px"]
    return (
        (float(box["left"]) + float(box["right"])) / 2.0,
        (float(box["top"]) + float(box["bottom"])) / 2.0,
    )


def _patch_radius(annotation: dict[str, Any]) -> int:
    box = annotation.get("bbox_source_px")
    if not isinstance(box, dict):
        return 6
    width = float(box["right"]) - float(box["left"])
    height = float(box["bottom"]) - float(box["top"])
    return max(4, min(16, int(math.ceil(max(width, height)))))


def _track_one_step(
    previous: np.ndarray,
    current: np.ndarray,
    previous_center: tuple[float, float],
    patch_radius: int,
) -> dict[str, Any]:
    template = _crop_patch(previous, previous_center, patch_radius)
    if template is None or float(template.std()) < 1.0:
        return _track_failure("seed_patch_low_information")
    matched = _match_template(
        current,
        template,
        previous_center,
        TRACKER_PROFILE["search_radius_source_px"],
    )
    if matched is None or matched[1] < TRACKER_PROFILE["minimum_match_score"]:
        return _track_failure(
            "forward_match_below_threshold",
            match_score=None if matched is None else matched[1],
        )
    current_center, match_score = matched
    current_patch = _crop_patch(current, current_center, patch_radius)
    if current_patch is None:
        return _track_failure("matched_patch_outside_source", match_score=match_score)
    backward = _match_template(
        previous,
        current_patch,
        current_center,
        TRACKER_PROFILE["search_radius_source_px"],
    )
    if backward is None:
        return _track_failure("backward_match_unavailable", match_score=match_score)
    backward_center, backward_score = backward
    error = math.hypot(
        backward_center[0] - previous_center[0],
        backward_center[1] - previous_center[1],
    )
    displacement = math.hypot(
        current_center[0] - previous_center[0],
        current_center[1] - previous_center[1],
    )
    metrics = {
        "match_score": float(match_score),
        "backward_match_score": float(backward_score),
        "forward_backward_error_px": float(error),
        "step_displacement_px": float(displacement),
    }
    if backward_score < TRACKER_PROFILE["minimum_backward_match_score"]:
        return _track_failure("backward_match_below_threshold", **metrics)
    if error > TRACKER_PROFILE["maximum_forward_backward_error_px"]:
        return _track_failure("forward_backward_error_exceeded", **metrics)
    return {"status": "success", "failure_code": None, "center": current_center, "metrics": metrics}


def _crop_patch(image: np.ndarray, center: tuple[float, float], radius: int) -> np.ndarray | None:
    x = int(round(center[0]))
    y = int(round(center[1]))
    left = x - radius
    top = y - radius
    right = x + radius + 1
    bottom = y + radius + 1
    if left < 0 or top < 0 or right > image.shape[1] or bottom > image.shape[0]:
        return None
    return image[top:bottom, left:right]


def _match_template(
    image: np.ndarray,
    template: np.ndarray,
    center: tuple[float, float],
    search_radius: int,
) -> tuple[tuple[float, float], float] | None:
    half_width = template.shape[1] // 2
    half_height = template.shape[0] // 2
    center_x = int(round(center[0]))
    center_y = int(round(center[1]))
    left = max(0, center_x - search_radius - half_width)
    top = max(0, center_y - search_radius - half_height)
    right = min(image.shape[1], center_x + search_radius + half_width + 1)
    bottom = min(image.shape[0], center_y + search_radius + half_height + 1)
    search = image[top:bottom, left:right]
    if search.shape[0] < template.shape[0] or search.shape[1] < template.shape[1]:
        return None
    scores = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
    _minimum, maximum, _minimum_location, maximum_location = cv2.minMaxLoc(scores)
    match_left = left + maximum_location[0]
    match_top = top + maximum_location[1]
    return (
        (match_left + half_width, match_top + half_height),
        float(maximum),
    )


def _translate_geometry(
    annotation: dict[str, Any],
    *,
    dx: float,
    dy: float,
    width: int,
    height: int,
) -> dict[str, Any] | None:
    point = annotation.get("point_source_px")
    translated_point = None
    if isinstance(point, dict):
        translated_point = {"x": float(point["x"]) + dx, "y": float(point["y"]) + dy}
        if not 0 <= translated_point["x"] < width or not 0 <= translated_point["y"] < height:
            return None
    box = annotation.get("bbox_source_px")
    translated_box = None
    if isinstance(box, dict):
        translated_box = {
            "left": float(box["left"]) + dx,
            "top": float(box["top"]) + dy,
            "right": float(box["right"]) + dx,
            "bottom": float(box["bottom"]) + dy,
        }
        if not (
            0 <= translated_box["left"] < translated_box["right"] <= width
            and 0 <= translated_box["top"] < translated_box["bottom"] <= height
        ):
            return None
    return {"point_source_px": translated_point, "bbox_source_px": translated_box}


def _track_failure(code: str, **metrics: Any) -> dict[str, Any]:
    return {"status": "failed", "failure_code": code, "metrics": metrics}


def _failed_result(
    frame_index: int,
    direction: str,
    source_frame_sha256: str,
    failure_code: str,
    **metrics: Any,
) -> dict[str, Any]:
    return {
        "frame_index": frame_index,
        "direction": direction,
        "status": "failed",
        "failure_code": failure_code,
        "source_frame_sha256": source_frame_sha256,
        "suggestion_id": None,
        "match_score": metrics.get("match_score"),
        "backward_match_score": metrics.get("backward_match_score"),
        "forward_backward_error_px": metrics.get("forward_backward_error_px"),
        "step_displacement_px": metrics.get("step_displacement_px"),
        "pending_human_confirmation": False,
    }


__all__ = [
    "PropagationError",
    "TRACKER_PROFILE",
    "TRACKER_PROFILE_ID",
    "TRACKER_PROFILE_SHA256",
    "build_advisory_suggestions",
]
