from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import cv2
import numpy as np

DEFAULT_PITCH_LENGTH_METERS = 105.0
DEFAULT_PITCH_WIDTH_METERS = 68.0

Point = tuple[float, float]
Matrix = list[list[float]]


def default_pitch_corners(
    *,
    length_m: float = DEFAULT_PITCH_LENGTH_METERS,
    width_m: float = DEFAULT_PITCH_WIDTH_METERS,
) -> list[Point]:
    return [(0.0, 0.0), (float(length_m), 0.0), (float(length_m), float(width_m)), (0.0, float(width_m))]


def compute_homography(source_points: Sequence[Any] | None, destination_points: Sequence[Any] | None) -> Matrix | None:
    source = _normalize_four_points(source_points)
    destination = _normalize_four_points(destination_points)
    if source is None or destination is None:
        return None
    if _polygon_area(source) <= 1e-6 or _polygon_area(destination) <= 1e-6:
        return None

    matrix = cv2.getPerspectiveTransform(
        np.asarray(source, dtype=np.float32),
        np.asarray(destination, dtype=np.float32),
    )
    return _matrix_to_jsonable(matrix)


def invert_homography(matrix: Sequence[Sequence[Any]] | None) -> Matrix | None:
    normalized = _normalize_matrix(matrix)
    if normalized is None:
        return None
    try:
        inverse = np.linalg.inv(normalized)
    except np.linalg.LinAlgError:
        return None
    return _matrix_to_jsonable(inverse)


def map_point(point: Sequence[Any], matrix: Sequence[Sequence[Any]] | None) -> Point | None:
    normalized_point = _normalize_point(point)
    normalized_matrix = _normalize_matrix(matrix)
    if normalized_point is None or normalized_matrix is None:
        return None

    vector = np.asarray([normalized_point[0], normalized_point[1], 1.0], dtype=np.float64)
    projected = normalized_matrix @ vector
    denominator = float(projected[2])
    if not math.isfinite(denominator) or abs(denominator) <= 1e-12:
        return None

    mapped_x = float(projected[0] / denominator)
    mapped_y = float(projected[1] / denominator)
    if not math.isfinite(mapped_x) or not math.isfinite(mapped_y):
        return None
    return (mapped_x, mapped_y)


def _normalize_four_points(raw_points: Sequence[Any] | None) -> list[Point] | None:
    if not isinstance(raw_points, Sequence) or len(raw_points) != 4:
        return None
    points: list[Point] = []
    for raw_point in raw_points:
        point = _normalize_point(raw_point)
        if point is None:
            return None
        points.append(point)
    return points


def _normalize_point(raw_point: Sequence[Any]) -> Point | None:
    if isinstance(raw_point, (str, bytes)) or not isinstance(raw_point, Sequence) or len(raw_point) != 2:
        return None
    try:
        x = float(raw_point[0])
        y = float(raw_point[1])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x) or not math.isfinite(y):
        return None
    return (x, y)


def _normalize_matrix(matrix: Sequence[Sequence[Any]] | None) -> np.ndarray | None:
    if matrix is None:
        return None
    try:
        normalized = np.asarray(matrix, dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if normalized.shape != (3, 3) or not np.isfinite(normalized).all():
        return None
    determinant = float(np.linalg.det(normalized))
    if not math.isfinite(determinant) or abs(determinant) <= 1e-12:
        return None
    return normalized


def _matrix_to_jsonable(matrix: np.ndarray) -> Matrix | None:
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        return None
    if abs(float(np.linalg.det(matrix))) <= 1e-12:
        return None

    scale = float(matrix[2, 2])
    if math.isfinite(scale) and abs(scale) > 1e-12:
        matrix = matrix / scale
    return [[float(value) for value in row] for row in matrix.tolist()]


def _polygon_area(points: Sequence[Point]) -> float:
    area = 0.0
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0
