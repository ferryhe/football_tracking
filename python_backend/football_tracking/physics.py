from __future__ import annotations

import math

from football_tracking.types import TrackPoint


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def euclidean_distance(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    dx = p1[0] - p2[0]
    dy = p1[1] - p2[1]
    return math.hypot(dx, dy)


def vector_magnitude(vector: tuple[float, float]) -> float:
    return math.hypot(vector[0], vector[1])


def compute_velocity(previous_point: TrackPoint | None, current_point: tuple[float, float], frame_gap: int) -> tuple[float, float]:
    """计算逐帧速度，frame_gap 至少为 1。"""
    if previous_point is None:
        return (0.0, 0.0)
    safe_gap = max(1, frame_gap)
    return (
        (current_point[0] - previous_point.x) / safe_gap,
        (current_point[1] - previous_point.y) / safe_gap,
    )


def compute_acceleration(previous_velocity: tuple[float, float], current_velocity: tuple[float, float]) -> tuple[float, float]:
    """计算速度变化，用于抑制非物理跳变。"""
    return (
        current_velocity[0] - previous_velocity[0],
        current_velocity[1] - previous_velocity[1],
    )


def predict_constant_velocity(position: tuple[float, float], velocity: tuple[float, float], steps: int = 1) -> tuple[float, float]:
    """匀速模型预测下一帧位置。"""
    return (
        position[0] + velocity[0] * steps,
        position[1] + velocity[1] * steps,
    )


def distance_score(candidate_position: tuple[float, float], anchor_position: tuple[float, float] | None, match_distance: float) -> float:
    """距离越接近预测点，得分越高。"""
    if anchor_position is None:
        return 0.5
    distance = euclidean_distance(candidate_position, anchor_position)
    if match_distance <= 1e-6:
        return 0.0
    return clamp01(1.0 - distance / match_distance)


def direction_score(reference_velocity: tuple[float, float], displacement: tuple[float, float]) -> float:
    """方向连续性得分，使用余弦相似度映射到 0~1。"""
    reference_norm = vector_magnitude(reference_velocity)
    displacement_norm = vector_magnitude(displacement)
    if reference_norm <= 1e-6 or displacement_norm <= 1e-6:
        return 0.5

    cosine = (
        reference_velocity[0] * displacement[0] + reference_velocity[1] * displacement[1]
    ) / (reference_norm * displacement_norm)
    cosine = max(-1.0, min(1.0, cosine))
    return (cosine + 1.0) / 2.0


def velocity_score(candidate_velocity: tuple[float, float], expected_velocity: tuple[float, float], max_speed: float) -> float:
    """速度越接近历史合理范围，得分越高。"""
    candidate_speed = vector_magnitude(candidate_velocity)
    expected_speed = vector_magnitude(expected_velocity)
    if candidate_speed > max_speed:
        return 0.0
    if max_speed <= 1e-6:
        return 0.0
    delta = abs(candidate_speed - expected_speed)
    return clamp01(1.0 - delta / max_speed)


def acceleration_penalty(
    previous_velocity: tuple[float, float],
    candidate_velocity: tuple[float, float],
    max_acceleration: float,
) -> float:
    """加速度越突变，惩罚越大。返回负值，便于直接加权到总分。"""
    if max_acceleration <= 1e-6:
        return -1.0
    acceleration = compute_acceleration(previous_velocity, candidate_velocity)
    penalty = clamp01(vector_magnitude(acceleration) / max_acceleration)
    return -penalty


def trajectory_length_bonus(history_length: int, stable_history_length: int) -> float:
    """轨迹越长，可信度越高。"""
    if stable_history_length <= 0:
        return 0.0
    return clamp01(history_length / stable_history_length)
