from __future__ import annotations

from football_tracking.config import SelectionConfig, TrackingConfig
from football_tracking.physics import (
    acceleration_penalty,
    direction_score,
    distance_score,
    euclidean_distance,
    trajectory_length_bonus,
    velocity_score,
)
from football_tracking.scene_bias import SceneBiasResolver
from football_tracking.types import Candidate, CandidateScore, SelectionDecision, TrackerContext


class UniqueBallSelector:
    """选择层：从多个合理候选中选出唯一比赛用球。"""

    def __init__(
        self,
        config: SelectionConfig,
        tracking_config: TrackingConfig,
        scene_bias: SceneBiasResolver | None = None,
    ) -> None:
        self.config = config
        self.tracking_config = tracking_config
        self.scene_bias = scene_bias

    def select(self, candidates: list[Candidate], context: TrackerContext, frame_index: int) -> SelectionDecision:
        if not candidates:
            return SelectionDecision(
                selected_candidate=None,
                selected_score=0.0,
                selected_reason="no_filtered_candidates",
                candidate_scores=[],
            )

        scored_candidates = [self._score_candidate(candidate, context, frame_index) for candidate in candidates]
        scored_candidates.sort(key=lambda item: item.total_score, reverse=True)
        best_candidate = scored_candidates[0]

        if best_candidate.total_score < self.config.min_accept_score and context.history_length > 0:
            return SelectionDecision(
                selected_candidate=None,
                selected_score=best_candidate.total_score,
                selected_reason="best_candidate_below_accept_threshold",
                candidate_scores=scored_candidates,
            )

        return SelectionDecision(
            selected_candidate=best_candidate.candidate,
            selected_score=best_candidate.total_score,
            selected_reason=best_candidate.reason,
            candidate_scores=scored_candidates,
        )

    def _score_candidate(self, candidate: Candidate, context: TrackerContext, frame_index: int) -> CandidateScore:
        anchor_position = context.predicted_position or context.last_position
        candidate_position = candidate.center

        if context.last_position is None:
            # 冷启动阶段缺少历史轨迹，只能在基础过滤后以轻量规则启动轨迹。
            candidate_velocity = (0.0, 0.0)
            displacement = (0.0, 0.0)
        else:
            displacement = (
                candidate_position[0] - context.last_position[0],
                candidate_position[1] - context.last_position[1],
            )
            candidate_velocity = displacement

        distance_component = distance_score(
            candidate_position=candidate_position,
            anchor_position=anchor_position,
            match_distance=self.tracking_config.match_distance,
        )
        direction_component = direction_score(context.velocity, displacement)
        velocity_component = velocity_score(
            candidate_velocity=candidate_velocity,
            expected_velocity=context.velocity,
            max_speed=self.tracking_config.max_speed,
        )
        acceleration_component = acceleration_penalty(
            previous_velocity=context.velocity,
            candidate_velocity=candidate_velocity,
            max_acceleration=self.tracking_config.max_acceleration,
        )
        history_component = trajectory_length_bonus(
            history_length=context.history_length,
            stable_history_length=self.config.stable_history_length,
        )
        confidence_component = candidate.confidence
        scene_bonus = 0.0
        scene_zone = None
        if self.scene_bias is not None:
            scene_bonus, scene_zone = self.scene_bias.get_selection_bonus(candidate, context, frame_index)

        weights = self.config.weights
        total_score = (
            weights.distance_score * distance_component
            + weights.direction_score * direction_component
            + weights.velocity_score * velocity_component
            + weights.acceleration_penalty * acceleration_component
            + weights.trajectory_length_bonus * history_component
            + weights.confidence * confidence_component
            + scene_bonus
        )

        reason = self._build_reason(
            candidate=candidate,
            anchor_position=anchor_position,
            distance_component=distance_component,
            direction_component=direction_component,
            velocity_component=velocity_component,
            acceleration_component=acceleration_component,
            history_component=history_component,
            scene_bonus=scene_bonus,
            scene_zone=scene_zone,
        )

        return CandidateScore(
            candidate=candidate,
            total_score=total_score,
            distance_score=distance_component,
            direction_score=direction_component,
            velocity_score=velocity_component,
            acceleration_penalty=acceleration_component,
            trajectory_length_bonus=history_component,
            confidence_score=confidence_component,
            scene_bonus=scene_bonus,
            scene_zone=scene_zone,
            reason=reason,
        )

    def _build_reason(
        self,
        candidate: Candidate,
        anchor_position: tuple[float, float] | None,
        distance_component: float,
        direction_component: float,
        velocity_component: float,
        acceleration_component: float,
        history_component: float,
        scene_bonus: float,
        scene_zone: str | None,
    ) -> str:
        if anchor_position is None:
            return "bootstrap_selection_without_history"

        distance = euclidean_distance(candidate.center, anchor_position)
        reason = (
            f"distance={distance:.2f}, distance_score={distance_component:.3f}, "
            f"direction_score={direction_component:.3f}, velocity_score={velocity_component:.3f}, "
            f"acceleration_penalty={acceleration_component:.3f}, history_bonus={history_component:.3f}"
        )
        if scene_zone is not None or scene_bonus != 0.0:
            reason += f", scene_zone={scene_zone}, scene_bonus={scene_bonus:.3f}"
        return reason
