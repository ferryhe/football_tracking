from __future__ import annotations

from collections import deque

from football_tracking.config import TrackingConfig
from football_tracking.physics import compute_acceleration, compute_velocity, predict_constant_velocity
from football_tracking.types import (
    OutputStatus,
    SelectionDecision,
    TrackPoint,
    TrackResult,
    TrackState,
    TrackerContext,
)


class BallTracker:
    """追踪层：管理状态机、轨迹历史、速度、加速度和短时预测。"""

    def __init__(self, config: TrackingConfig) -> None:
        self.config = config
        self.state = TrackState.INIT
        self.history: deque[TrackPoint] = deque(maxlen=config.history_size)
        self.velocity: tuple[float, float] = (0.0, 0.0)
        self.acceleration: tuple[float, float] = (0.0, 0.0)
        self.lost_frames = 0
        self.last_detected_confidence = 0.0

    def build_context(self) -> TrackerContext:
        """给选择层提供当前追踪上下文。"""
        if self.state == TrackState.LOST or not self.history:
            return TrackerContext(state=self.state, history_length=0, lost_frames=self.lost_frames)

        last_point = self.history[-1]
        predicted_position = predict_constant_velocity((last_point.x, last_point.y), self.velocity, steps=1)
        return TrackerContext(
            state=self.state,
            last_position=(last_point.x, last_point.y),
            predicted_position=predicted_position,
            velocity=self.velocity,
            acceleration=self.acceleration,
            history_length=len(self.history),
            lost_frames=self.lost_frames,
        )

    def update(
        self,
        frame_index: int,
        decision: SelectionDecision,
        raw_candidate_count: int,
        filtered_candidate_count: int,
        missing_reason: str | None = None,
    ) -> TrackResult:
        """根据选择结果更新状态机。"""
        if decision.selected_candidate is not None:
            return self._handle_detection(
                frame_index=frame_index,
                decision=decision,
                raw_candidate_count=raw_candidate_count,
                filtered_candidate_count=filtered_candidate_count,
            )

        return self._handle_missing(
            frame_index=frame_index,
            decision=decision,
            raw_candidate_count=raw_candidate_count,
            filtered_candidate_count=filtered_candidate_count,
            missing_reason=missing_reason or decision.selected_reason,
        )

    def _handle_detection(
        self,
        frame_index: int,
        decision: SelectionDecision,
        raw_candidate_count: int,
        filtered_candidate_count: int,
    ) -> TrackResult:
        candidate = decision.selected_candidate
        assert candidate is not None

        previous_point = self.history[-1] if self.history else None
        candidate_position = candidate.center
        frame_gap = frame_index - previous_point.frame_index if previous_point else 1
        new_velocity = compute_velocity(previous_point, candidate_position, frame_gap=frame_gap)
        new_acceleration = compute_acceleration(self.velocity, new_velocity)

        point = TrackPoint(
            frame_index=frame_index,
            x=candidate_position[0],
            y=candidate_position[1],
            confidence=candidate.confidence,
            status=OutputStatus.DETECTED,
        )
        self.history.append(point)
        self.velocity = new_velocity
        self.acceleration = new_acceleration
        self.lost_frames = 0
        self.last_detected_confidence = candidate.confidence

        if len(self.history) >= self.config.min_history_for_tracking:
            self.state = TrackState.TRACKING
        else:
            self.state = TrackState.INIT

        return TrackResult(
            frame_index=frame_index,
            output_status=OutputStatus.DETECTED,
            state=self.state,
            point=point,
            confidence=candidate.confidence,
            reason=decision.selected_reason,
            lost_frames=self.lost_frames,
            raw_candidate_count=raw_candidate_count,
            filtered_candidate_count=filtered_candidate_count,
            selected_score=decision.selected_score,
            selected_candidate_scores=decision.candidate_scores,
        )

    def _handle_missing(
        self,
        frame_index: int,
        decision: SelectionDecision,
        raw_candidate_count: int,
        filtered_candidate_count: int,
        missing_reason: str,
    ) -> TrackResult:
        if not self.history:
            self.state = TrackState.LOST
            self.lost_frames += 1
            return TrackResult(
                frame_index=frame_index,
                output_status=OutputStatus.LOST,
                state=self.state,
                point=None,
                confidence=0.0,
                reason=missing_reason,
                lost_frames=self.lost_frames,
                raw_candidate_count=raw_candidate_count,
                filtered_candidate_count=filtered_candidate_count,
                selected_score=decision.selected_score,
                selected_candidate_scores=decision.candidate_scores,
            )

        self.lost_frames += 1
        if self.lost_frames <= self.config.max_lost_frames:
            last_point = self.history[-1]
            predicted_position = predict_constant_velocity((last_point.x, last_point.y), self.velocity, steps=1)
            predicted_confidence = self.last_detected_confidence * (
                self.config.predicted_confidence_decay ** self.lost_frames
            )
            point = TrackPoint(
                frame_index=frame_index,
                x=predicted_position[0],
                y=predicted_position[1],
                confidence=predicted_confidence,
                status=OutputStatus.PREDICTED,
            )
            self.history.append(point)
            self.state = TrackState.PREDICTING
            return TrackResult(
                frame_index=frame_index,
                output_status=OutputStatus.PREDICTED,
                state=self.state,
                point=point,
                confidence=predicted_confidence,
                reason=missing_reason,
                lost_frames=self.lost_frames,
                raw_candidate_count=raw_candidate_count,
                filtered_candidate_count=filtered_candidate_count,
                selected_score=decision.selected_score,
                selected_candidate_scores=decision.candidate_scores,
            )

        self.state = TrackState.LOST
        self.velocity = (0.0, 0.0)
        self.acceleration = (0.0, 0.0)
        self.history.clear()
        return TrackResult(
            frame_index=frame_index,
            output_status=OutputStatus.LOST,
            state=self.state,
            point=None,
            confidence=0.0,
            reason="lost_after_prediction_threshold",
            lost_frames=self.lost_frames,
            raw_candidate_count=raw_candidate_count,
            filtered_candidate_count=filtered_candidate_count,
            selected_score=decision.selected_score,
            selected_candidate_scores=decision.candidate_scores,
        )
