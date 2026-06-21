from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from football_tracking.geometry import (
    DEFAULT_PITCH_LENGTH_METERS,
    DEFAULT_PITCH_WIDTH_METERS,
    compute_homography,
    default_pitch_corners,
    invert_homography,
    map_point,
)

Point = tuple[float, float]


def build_pitch_calibration_from_field_polygon(
    field_polygon: Sequence[Any] | None,
    *,
    confidence: str,
    source: str,
    pitch_length_m: float = DEFAULT_PITCH_LENGTH_METERS,
    pitch_width_m: float = DEFAULT_PITCH_WIDTH_METERS,
) -> dict[str, Any] | None:
    image_points = field_polygon_to_pitch_corners(field_polygon)
    if image_points is None:
        return None

    pitch_points = default_pitch_corners(length_m=pitch_length_m, width_m=pitch_width_m)
    image_to_pitch = compute_homography(image_points, pitch_points)
    pitch_to_image = invert_homography(image_to_pitch)
    if image_to_pitch is None or pitch_to_image is None:
        return None

    return {
        "image_points": _json_points(image_points),
        "pitch_points": _json_points(pitch_points),
        "image_to_pitch_matrix": image_to_pitch,
        "pitch_to_image_matrix": pitch_to_image,
        "pitch_dimensions": {
            "length_m": float(pitch_length_m),
            "width_m": float(pitch_width_m),
        },
        "confidence": _calibration_confidence(confidence),
        "source": f"{source}:field-polygon-corners" if source else "field-polygon-corners",
    }


def field_polygon_to_pitch_corners(field_polygon: Sequence[Any] | None) -> list[Point] | None:
    if not isinstance(field_polygon, Sequence):
        return None
    if len(field_polygon) == 4:
        corner_indexes = [0, 1, 2, 3]
    elif len(field_polygon) == 9:
        corner_indexes = [0, 6, len(field_polygon) - 2, len(field_polygon) - 1]
    else:
        return None

    corners: list[Point] = []
    normalized_points: list[Point] = []
    for raw_point in field_polygon:
        point = _normalize_point(raw_point)
        if point is None:
            return None
        normalized_points.append(point)

    if len(normalized_points) == 9 and not _looks_like_generated_nine_point_field(normalized_points):
        return None

    for index in corner_indexes:
        corners.append(normalized_points[index])
    return corners


def image_point_to_pitch(point: Sequence[Any], calibration: dict[str, Any] | None) -> Point | None:
    if not isinstance(calibration, dict):
        return None
    return map_point(point, calibration.get("image_to_pitch_matrix"))


def pitch_point_to_image(point: Sequence[Any], calibration: dict[str, Any] | None) -> Point | None:
    if not isinstance(calibration, dict):
        return None
    return map_point(point, calibration.get("pitch_to_image_matrix"))


def _calibration_confidence(field_confidence: str) -> str:
    if field_confidence == "config":
        return "config"
    if field_confidence == "detected":
        return "estimated"
    return "low"


def _normalize_point(raw_point: Any) -> Point | None:
    if isinstance(raw_point, (str, bytes)) or not isinstance(raw_point, Sequence) or len(raw_point) != 2:
        return None
    try:
        point = (float(raw_point[0]), float(raw_point[1]))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(point[0]) or not math.isfinite(point[1]):
        return None
    return point


def _looks_like_generated_nine_point_field(points: Sequence[Point]) -> bool:
    top_edge = points[:7]
    bottom_right = points[7]
    bottom_left = points[8]
    if any(next_point[0] < current_point[0] - 2.0 for current_point, next_point in zip(top_edge, top_edge[1:])):
        return False

    top_width = top_edge[-1][0] - top_edge[0][0]
    if top_width <= 1e-6:
        return False

    top_y = sum(point[1] for point in top_edge) / len(top_edge)
    bottom_y = min(bottom_right[1], bottom_left[1])
    if bottom_y <= top_y + 1.0:
        return False

    return bottom_right[0] > bottom_left[0]


def _json_points(points: Sequence[Point]) -> list[list[float]]:
    return [[float(x), float(y)] for x, y in points]
