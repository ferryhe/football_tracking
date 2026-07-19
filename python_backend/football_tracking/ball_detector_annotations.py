from __future__ import annotations

import math
from typing import Any

from football_tracking.detector_development_common import canonical_sha256

PRESENCE_VALUES = frozenset({"present", "absent", "unknown"})
VISIBILITY_VALUES = frozenset({"visible", "partial", "unresolvable", "not_applicable"})
TRAINING_USE_VALUES = frozenset({"positive", "background", "excluded"})
ANNOTATION_STATE_VALUES = frozenset({"suggested", "confirmed"})
SCALE_VALUES = frozenset({"near", "mid", "far", "not_applicable"})
LIGHTING_VALUES = frozenset({"bright_sun", "shadow", "backlight", "twilight", "artificial_light", "not_applicable"})
MOTION_OCCLUSION_VALUES = frozenset({"ground", "airborne", "motion_blurred", "occluded", "reappearance", "stationary"})
PROVENANCE_VALUES = frozenset(
    {
        "manual_human_annotation",
        "detector_candidate_human_confirmed",
        "propagation_suggestion_human_confirmed",
        "suggestion_dismissed_manual",
    }
)

_ANNOTATION_FIELDS = frozenset(
    {
        "point_source_px",
        "bbox_source_px",
        "presence",
        "visibility",
        "training_use",
        "annotation_state",
        "scale_stratum",
        "lighting_tag",
        "motion_occlusion_tags",
        "provenance",
    }
)


class BallAnnotationError(ValueError):
    """A stable fail-closed annotation-contract error."""


def validate_ball_annotation(
    value: Any,
    *,
    width: int,
    height: int,
    data_role: str = "development",
) -> dict[str, Any]:
    """Validate and normalize one source-pixel human/suggestion record.

    Presence, visibility and training use deliberately remain separate fields.
    This function only accepts the detector-development schema and never emits
    PR4A/PR4B review truth.
    """

    if not isinstance(value, dict):
        raise BallAnnotationError("annotation must be an object")
    unexpected = sorted(set(value) - _ANNOTATION_FIELDS)
    if unexpected:
        raise BallAnnotationError(f"annotation contains unexpected fields: {unexpected}")
    if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
        raise BallAnnotationError("source width must be a positive integer")
    if isinstance(height, bool) or not isinstance(height, int) or height <= 0:
        raise BallAnnotationError("source height must be a positive integer")
    if data_role not in {"development", "check"}:
        raise BallAnnotationError("data_role must be development or check")

    presence = _enum(value.get("presence"), PRESENCE_VALUES, "presence")
    visibility = _enum(value.get("visibility"), VISIBILITY_VALUES, "visibility")
    training_use = _enum(value.get("training_use"), TRAINING_USE_VALUES, "training_use")
    state = _enum(value.get("annotation_state"), ANNOTATION_STATE_VALUES, "annotation_state")
    scale = _enum(value.get("scale_stratum"), SCALE_VALUES, "scale_stratum")
    lighting = _enum(value.get("lighting_tag"), LIGHTING_VALUES, "lighting_tag")
    provenance = _enum(value.get("provenance"), PROVENANCE_VALUES, "provenance")

    raw_tags = value.get("motion_occlusion_tags")
    if not isinstance(raw_tags, list) or len(raw_tags) > len(MOTION_OCCLUSION_VALUES):
        raise BallAnnotationError("motion_occlusion_tags must be a bounded list")
    tags: list[str] = []
    for item in raw_tags:
        tag = _enum(item, MOTION_OCCLUSION_VALUES, "motion_occlusion_tag")
        if tag in tags:
            raise BallAnnotationError("motion_occlusion_tags must be unique")
        tags.append(tag)
    tags.sort()

    point = _point(value.get("point_source_px"), width=width, height=height)
    box = _box(value.get("bbox_source_px"), width=width, height=height)
    if point is not None and box is not None:
        center_x = (box["left"] + box["right"]) / 2.0
        center_y = (box["top"] + box["bottom"]) / 2.0
        if abs(point["x"] - center_x) > 0.5 or abs(point["y"] - center_y) > 0.5:
            raise BallAnnotationError("point and bounding box centers are inconsistent")
        point = {"x": center_x, "y": center_y}
    elif point is None and box is not None:
        point = {
            "x": (box["left"] + box["right"]) / 2.0,
            "y": (box["top"] + box["bottom"]) / 2.0,
        }

    if data_role == "check" and training_use != "excluded":
        raise BallAnnotationError("check annotations are evaluation-only and must be excluded from training")
    if state == "suggested" and training_use != "excluded":
        raise BallAnnotationError("suggested annotations are never truth and must be excluded")

    if presence == "absent":
        if point is not None or box is not None:
            raise BallAnnotationError("confirmed absent annotations cannot carry coordinates")
        if visibility != "not_applicable" or training_use not in {"background", "excluded"}:
            raise BallAnnotationError(
                "confirmed absent requires not_applicable visibility and background or excluded use"
            )
        if scale != "not_applicable":
            raise BallAnnotationError("confirmed absent requires not_applicable scale")
    elif presence == "unknown":
        if point is not None or box is not None:
            raise BallAnnotationError("unknown annotations cannot carry coordinates")
        if visibility not in {"unresolvable", "not_applicable"} or training_use != "excluded":
            raise BallAnnotationError("unknown annotations must be unresolvable/not_applicable and excluded")
        if scale != "not_applicable":
            raise BallAnnotationError("unknown annotations require not_applicable scale")
    elif visibility == "unresolvable":
        if point is not None or box is not None or training_use != "excluded":
            raise BallAnnotationError("present but unresolvable annotations have no coordinates and are excluded")
        if scale != "not_applicable":
            raise BallAnnotationError("present but unresolvable annotations require not_applicable scale")
    else:
        if visibility not in {"visible", "partial"}:
            raise BallAnnotationError("present localizable annotations require visible or partial visibility")
        if data_role == "check" and box is None:
            raise BallAnnotationError("check localizable truth requires a human-confirmed bounding box")
        if training_use == "positive" and (state != "confirmed" or box is None):
            raise BallAnnotationError("a positive requires a human-confirmed bounding box")
        if point is None:
            raise BallAnnotationError("present localizable annotations require a source-pixel point or box")
        if scale == "not_applicable":
            raise BallAnnotationError("present localizable annotations require a scale stratum")
        if training_use == "background":
            raise BallAnnotationError("present annotations cannot be background")

    return {
        "point_source_px": point,
        "bbox_source_px": box,
        "presence": presence,
        "visibility": visibility,
        "training_use": training_use,
        "annotation_state": state,
        "scale_stratum": scale,
        "lighting_tag": lighting,
        "motion_occlusion_tags": tags,
        "provenance": provenance,
    }


def annotation_etag(
    session_id: str,
    frame_index: int,
    revision: int,
    effective_annotation: dict[str, Any] | None,
) -> str:
    """Return an unquoted content ETag for one effective append-only revision."""

    return canonical_sha256(
        {
            "schema_version": "1.0",
            "artifact_type": "ball_annotation_effective_revision",
            "session_id": session_id,
            "frame_index": frame_index,
            "revision": revision,
            "effective_annotation": effective_annotation,
        }
    )


def _enum(value: Any, allowed: frozenset[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise BallAnnotationError(f"{label} is invalid")
    return value


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise BallAnnotationError(f"{label} must be finite")
    return float(value)


def _point(value: Any, *, width: int, height: int) -> dict[str, float] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"x", "y"}:
        raise BallAnnotationError("point_source_px must contain only x and y")
    x = _finite_number(value["x"], "point x")
    y = _finite_number(value["y"], "point y")
    if not 0 <= x < width or not 0 <= y < height:
        raise BallAnnotationError("point_source_px is outside the source frame")
    return {"x": x, "y": y}


def _box(value: Any, *, width: int, height: int) -> dict[str, float] | None:
    if value is None:
        return None
    keys = {"left", "top", "right", "bottom"}
    if not isinstance(value, dict) or set(value) != keys:
        raise BallAnnotationError("bbox_source_px must contain only left, top, right and bottom")
    box = {key: _finite_number(value[key], f"box {key}") for key in keys}
    if not (0 <= box["left"] < box["right"] <= width and 0 <= box["top"] < box["bottom"] <= height):
        raise BallAnnotationError("bbox_source_px is outside the source frame")
    return {key: box[key] for key in ("left", "top", "right", "bottom")}


__all__ = [
    "ANNOTATION_STATE_VALUES",
    "BallAnnotationError",
    "LIGHTING_VALUES",
    "MOTION_OCCLUSION_VALUES",
    "PRESENCE_VALUES",
    "PROVENANCE_VALUES",
    "SCALE_VALUES",
    "TRAINING_USE_VALUES",
    "VISIBILITY_VALUES",
    "annotation_etag",
    "validate_ball_annotation",
]
