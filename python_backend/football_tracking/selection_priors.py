from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from football_tracking.calibration import image_point_to_pitch
from football_tracking.config import SelectionPriorsConfig
from football_tracking.geometry import DEFAULT_PITCH_LENGTH_METERS, DEFAULT_PITCH_WIDTH_METERS
from football_tracking.physics import euclidean_distance
from football_tracking.types import Candidate, TrackerContext


Point = tuple[float, float]


@dataclass(frozen=True, slots=True)
class SelectionPriorScore:
    prior_score: float = 0.0
    player_foot_bonus: float = 0.0
    nearest_player_foot_distance_px: float | None = None
    pitch_boundary_penalty: float = 0.0
    pitch_point_m: Point | None = None
    outside_pitch: bool | None = None


def score_selection_priors(
    candidate: Candidate,
    context: TrackerContext,
    frame_index: int,
    config: SelectionPriorsConfig,
) -> SelectionPriorScore:
    if not config.enabled:
        return SelectionPriorScore()

    nearest_player_foot_distance = nearest_recent_player_foot_distance_px(
        report=context.player_tracks_report,
        frame_index=frame_index,
        point=candidate.center,
        frame_window=config.recent_player_frame_window,
    )
    player_foot_bonus = _player_foot_bonus(nearest_player_foot_distance, config)

    pitch_point = image_point_to_pitch(candidate.center, context.pitch_calibration)
    outside_pitch = None
    pitch_boundary_penalty = 0.0
    if pitch_point is not None:
        outside_pitch = _outside_pitch_boundary(
            pitch_point=pitch_point,
            calibration=context.pitch_calibration,
            margin_m=config.pitch_boundary_margin_m,
        )
        if outside_pitch:
            pitch_boundary_penalty = min(0.0, config.pitch_boundary_penalty)

    prior_score = player_foot_bonus + pitch_boundary_penalty
    return SelectionPriorScore(
        prior_score=prior_score,
        player_foot_bonus=player_foot_bonus,
        nearest_player_foot_distance_px=nearest_player_foot_distance,
        pitch_boundary_penalty=pitch_boundary_penalty,
        pitch_point_m=pitch_point,
        outside_pitch=outside_pitch,
    )


def nearest_recent_player_foot_distance_px(
    *,
    report: dict[str, Any] | None,
    frame_index: int,
    point: Point,
    frame_window: int,
) -> float | None:
    if not isinstance(report, Mapping):
        return None

    tracks = report.get("tracks")
    if not isinstance(tracks, list):
        return None

    nearest_distance: float | None = None
    for track in tracks:
        if not isinstance(track, Mapping):
            continue
        samples = track.get("samples")
        if not isinstance(samples, list):
            continue
        for sample in samples:
            if not isinstance(sample, Mapping):
                continue
            sample_frame = _sample_frame(sample)
            if sample_frame is None:
                continue
            sample_age = frame_index - sample_frame
            if sample_age < 0 or sample_age > frame_window:
                continue
            foot_point = _sample_foot_point(sample)
            if foot_point is None:
                continue
            distance = euclidean_distance(point, foot_point)
            if nearest_distance is None or distance < nearest_distance:
                nearest_distance = distance

    return nearest_distance


def _player_foot_bonus(distance_px: float | None, config: SelectionPriorsConfig) -> float:
    radius = max(0.0, config.player_foot_radius_px)
    if distance_px is None or radius <= 1e-6 or distance_px > radius:
        return 0.0
    return max(0.0, config.player_foot_bonus) * (1.0 - distance_px / radius)


def _outside_pitch_boundary(
    *,
    pitch_point: Point,
    calibration: dict[str, Any] | None,
    margin_m: float,
) -> bool:
    length_m, width_m = _pitch_dimensions(calibration)
    margin = max(0.0, margin_m)
    x, y = pitch_point
    return x < -margin or x > length_m + margin or y < -margin or y > width_m + margin


def _pitch_dimensions(calibration: dict[str, Any] | None) -> tuple[float, float]:
    dimensions = calibration.get("pitch_dimensions") if isinstance(calibration, Mapping) else None
    if not isinstance(dimensions, Mapping):
        return (DEFAULT_PITCH_LENGTH_METERS, DEFAULT_PITCH_WIDTH_METERS)
    return (
        _finite_float(dimensions.get("length_m"), DEFAULT_PITCH_LENGTH_METERS),
        _finite_float(dimensions.get("width_m"), DEFAULT_PITCH_WIDTH_METERS),
    )


def _sample_frame(sample: Mapping[str, Any]) -> int | None:
    value = sample.get("frame")
    if value is None:
        value = sample.get("frame_index")
    try:
        parsed_float = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed_float):
        return None
    try:
        return int(parsed_float)
    except OverflowError:
        return None


def _sample_foot_point(sample: Mapping[str, Any]) -> Point | None:
    point = _point_from_payload(sample.get("foot_point"))
    if point is not None:
        return point

    bbox = sample.get("bbox")
    if not isinstance(bbox, Sequence) or isinstance(bbox, (str, bytes)) or len(bbox) != 4:
        return None
    try:
        x1 = float(bbox[0])
        x2 = float(bbox[2])
        y2 = float(bbox[3])
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (x1, x2, y2)):
        return None
    return ((x1 + x2) / 2.0, y2)


def _point_from_payload(payload: Any) -> Point | None:
    if isinstance(payload, Mapping):
        raw_x = payload.get("x")
        raw_y = payload.get("y")
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)) and len(payload) == 2:
        raw_x = payload[0]
        raw_y = payload[1]
    else:
        return None

    try:
        point = (float(raw_x), float(raw_y))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(point[0]) or not math.isfinite(point[1]):
        return None
    return point


def _finite_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed) or parsed <= 0.0:
        return default
    return parsed
