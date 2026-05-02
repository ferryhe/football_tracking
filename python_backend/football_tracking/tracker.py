from __future__ import annotations

import math
from collections import deque

from football_tracking.config import TrackingConfig
from football_tracking.kalman import ConstantAccelerationKalmanFilter
from football_tracking.physics import compute_acceleration, compute_velocity, predict_constant_velocity
from football_tracking.types import (
    OutputStatus,
    SelectionDecision,
    TrackerContext,
    TrackPoint,
    TrackResult,
    TrackState,
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
        self.last_anchor_position: tuple[float, float] | None = None
        self.last_detected_position: tuple[float, float] | None = None
        self.kalman = ConstantAccelerationKalmanFilter() if config.kalman_enabled else None
        self.reacquire_stabilization_frames_remaining = 0
        self.out_of_view_prediction_frames = 0

    def build_context(self) -> TrackerContext:
        """给选择层提供当前追踪上下文。"""
        if self.kalman is not None and self.kalman.is_initialized:
            current_position = self.kalman.get_position()
            predicted_position, predicted_velocity, predicted_acceleration, gating_radius = self._peek_kalman_context()
            if self.reacquire_stabilization_frames_remaining > 0:
                gating_radius *= self.config.reacquire_stabilization_gate_scale
            return TrackerContext(
                state=self.state,
                last_position=current_position,
                predicted_position=predicted_position,
                last_detected_position=self.last_detected_position,
                gating_radius=gating_radius,
                velocity=predicted_velocity,
                acceleration=predicted_acceleration,
                history_length=len(self.history),
                lost_frames=self.lost_frames,
            )

        if self.state == TrackState.LOST or not self.history:
            return TrackerContext(
                state=self.state,
                last_position=self.last_anchor_position,
                predicted_position=self.last_anchor_position,
                last_detected_position=self.last_detected_position,
                history_length=0,
                lost_frames=self.lost_frames,
            )

        last_point = self.history[-1]
        predicted_position = predict_constant_velocity((last_point.x, last_point.y), self.velocity, steps=1)
        return TrackerContext(
            state=self.state,
            last_position=(last_point.x, last_point.y),
            predicted_position=predicted_position,
            last_detected_position=self.last_detected_position,
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
        frame_size: tuple[int, int] | None = None,
        force_lost: bool = False,
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
            frame_size=frame_size,
            force_lost=force_lost,
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
        reacquire = self.lost_frames > 0 or "yolo_direct" in candidate.source
        stabilization_active = self.reacquire_stabilization_frames_remaining > 0

        if self.kalman is not None:
            if not self.kalman.is_initialized:
                initial_velocity = compute_velocity(previous_point, candidate_position, frame_gap=frame_gap)
                self.kalman.initialize(candidate_position, velocity=initial_velocity)
            else:
                self.kalman.predict(
                    dt=1.0,
                    process_noise_scale=self._process_noise_scale(reacquire=reacquire),
                )
                self.kalman.update(
                    candidate_position,
                    measurement_noise_scale=self._measurement_noise_scale(candidate.confidence),
                )

            new_velocity = self.kalman.get_velocity()
            new_acceleration = self.kalman.get_acceleration()
            if reacquire or stabilization_active:
                new_velocity = self._tame_reacquired_velocity(new_velocity)
                self.kalman.set_motion(velocity=new_velocity, acceleration=(0.0, 0.0))
                new_acceleration = (0.0, 0.0)
        else:
            new_velocity = compute_velocity(previous_point, candidate_position, frame_gap=frame_gap)
            new_acceleration = compute_acceleration(self.velocity, new_velocity)
            if reacquire or stabilization_active:
                new_velocity = self._tame_reacquired_velocity(new_velocity)
                new_acceleration = (0.0, 0.0)

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
        self.last_anchor_position = candidate_position
        self.last_detected_position = candidate_position
        self.out_of_view_prediction_frames = 0

        if len(self.history) >= self.config.min_history_for_tracking:
            self.state = TrackState.TRACKING
        else:
            self.state = TrackState.INIT
        self._advance_reacquire_stabilization(trigger_reacquire=reacquire)

        result = TrackResult(
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
        result.reacquire_stabilization_frames_remaining = self.reacquire_stabilization_frames_remaining
        result.out_of_view_prediction_frames = self.out_of_view_prediction_frames
        return result

    def _handle_missing(
        self,
        frame_index: int,
        decision: SelectionDecision,
        raw_candidate_count: int,
        filtered_candidate_count: int,
        missing_reason: str,
        frame_size: tuple[int, int] | None,
        force_lost: bool = False,
    ) -> TrackResult:
        if not self.history:
            self.state = TrackState.LOST
            self.lost_frames += 1
            result = TrackResult(
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
            result.out_of_view_prediction_frames = self.out_of_view_prediction_frames
            return result

        self.lost_frames += 1
        if force_lost:
            return self._transition_to_lost(
                frame_index=frame_index,
                decision=decision,
                raw_candidate_count=raw_candidate_count,
                filtered_candidate_count=filtered_candidate_count,
                reason=missing_reason,
            )
        if self.lost_frames <= self.config.max_lost_frames:
            stabilization_active = self.reacquire_stabilization_frames_remaining > 0
            out_of_view_active = False
            if self.kalman is not None and self.kalman.is_initialized:
                self.kalman.predict(
                    dt=1.0,
                    process_noise_scale=self._process_noise_scale(reacquire=False),
                )
                predicted_position = self.kalman.get_position()
                decayed_velocity = self._decay_prediction_velocity(
                    position=predicted_position,
                    velocity=self.kalman.get_velocity(),
                    frame_size=frame_size,
                )
                if stabilization_active:
                    decayed_velocity = (
                        decayed_velocity[0] * self.config.reacquire_stabilization_velocity_decay,
                        decayed_velocity[1] * self.config.reacquire_stabilization_velocity_decay,
                    )
                decayed_velocity, out_of_view_active, should_stop_prediction = self._apply_out_of_view_decay(
                    predicted_position=predicted_position,
                    velocity=decayed_velocity,
                    frame_size=frame_size,
                )
                if should_stop_prediction:
                    self.last_anchor_position = self.kalman.get_position()
                    return self._transition_to_lost(
                        frame_index=frame_index,
                        decision=decision,
                        raw_candidate_count=raw_candidate_count,
                        filtered_candidate_count=filtered_candidate_count,
                        reason="lost_out_of_view",
                    )
                clamped_position, adjusted_velocity = self._clamp_predicted_motion(
                    predicted_position=predicted_position,
                    velocity=decayed_velocity,
                    frame_size=frame_size,
                )
                self.kalman.set_position(clamped_position)
                self.kalman.set_motion(velocity=adjusted_velocity)
            else:
                last_point = self.history[-1]
                decayed_velocity = self._decay_prediction_velocity(
                    position=(last_point.x, last_point.y),
                    velocity=self.velocity,
                    frame_size=frame_size,
                )
                if stabilization_active:
                    decayed_velocity = (
                        decayed_velocity[0] * self.config.reacquire_stabilization_velocity_decay,
                        decayed_velocity[1] * self.config.reacquire_stabilization_velocity_decay,
                    )
                predicted_position = predict_constant_velocity((last_point.x, last_point.y), decayed_velocity, steps=1)
                decayed_velocity, out_of_view_active, should_stop_prediction = self._apply_out_of_view_decay(
                    predicted_position=predicted_position,
                    velocity=decayed_velocity,
                    frame_size=frame_size,
                )
                if should_stop_prediction:
                    self.last_anchor_position = (last_point.x, last_point.y)
                    return self._transition_to_lost(
                        frame_index=frame_index,
                        decision=decision,
                        raw_candidate_count=raw_candidate_count,
                        filtered_candidate_count=filtered_candidate_count,
                        reason="lost_out_of_view",
                    )
                clamped_position, adjusted_velocity = self._clamp_predicted_motion(
                    predicted_position=predicted_position,
                    velocity=decayed_velocity,
                    frame_size=frame_size,
                )
            predicted_confidence = self.last_detected_confidence * (
                self.config.predicted_confidence_decay ** self.lost_frames
            )
            point = TrackPoint(
                frame_index=frame_index,
                x=clamped_position[0],
                y=clamped_position[1],
                confidence=predicted_confidence,
                status=OutputStatus.PREDICTED,
            )
            self.history.append(point)
            self.velocity = adjusted_velocity
            self.acceleration = (0.0, 0.0)
            self.last_anchor_position = clamped_position
            self.state = TrackState.PREDICTING
            self._advance_reacquire_stabilization(trigger_reacquire=False)
            result = TrackResult(
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
            result.reacquire_stabilization_frames_remaining = self.reacquire_stabilization_frames_remaining
            result.out_of_view_active = out_of_view_active
            result.out_of_view_prediction_frames = self.out_of_view_prediction_frames
            return result

        return self._transition_to_lost(
            frame_index=frame_index,
            decision=decision,
            raw_candidate_count=raw_candidate_count,
            filtered_candidate_count=filtered_candidate_count,
            reason="lost_after_prediction_threshold",
        )

    def _transition_to_lost(
        self,
        frame_index: int,
        decision: SelectionDecision,
        raw_candidate_count: int,
        filtered_candidate_count: int,
        reason: str,
    ) -> TrackResult:
        self.state = TrackState.LOST
        self.velocity = (0.0, 0.0)
        self.acceleration = (0.0, 0.0)
        if self.history:
            self.last_anchor_position = (self.history[-1].x, self.history[-1].y)
        if self.kalman is not None:
            self.kalman.set_motion(velocity=(0.0, 0.0), acceleration=(0.0, 0.0))
        self.history.clear()
        self.reacquire_stabilization_frames_remaining = 0
        self.out_of_view_prediction_frames = 0
        result = TrackResult(
            frame_index=frame_index,
            output_status=OutputStatus.LOST,
            state=self.state,
            point=None,
            confidence=0.0,
            reason=reason,
            lost_frames=self.lost_frames,
            raw_candidate_count=raw_candidate_count,
            filtered_candidate_count=filtered_candidate_count,
            selected_score=decision.selected_score,
            selected_candidate_scores=decision.candidate_scores,
        )
        result.reacquire_stabilization_frames_remaining = self.reacquire_stabilization_frames_remaining
        result.out_of_view_prediction_frames = self.out_of_view_prediction_frames
        return result

    def _decay_prediction_velocity(
        self,
        position: tuple[float, float],
        velocity: tuple[float, float],
        frame_size: tuple[int, int] | None,
    ) -> tuple[float, float]:
        vx = velocity[0] * self.config.prediction_velocity_decay
        vy = velocity[1] * self.config.prediction_velocity_decay
        if frame_size is None:
            return (vx, vy)

        if self._is_near_boundary(position, frame_size):
            vx *= self.config.prediction_boundary_extra_decay
            vy *= self.config.prediction_boundary_extra_decay
        return (vx, vy)

    def _apply_out_of_view_decay(
        self,
        predicted_position: tuple[float, float],
        velocity: tuple[float, float],
        frame_size: tuple[int, int] | None,
    ) -> tuple[tuple[float, float], bool, bool]:
        if frame_size is None or not self.config.out_of_view_enabled:
            self.out_of_view_prediction_frames = 0
            return velocity, False, False

        out_of_view_active = self._is_heading_out_of_view(predicted_position, velocity, frame_size)
        if not out_of_view_active:
            self.out_of_view_prediction_frames = 0
            return velocity, False, False

        self.out_of_view_prediction_frames += 1
        decayed_velocity = (
            velocity[0] * self.config.out_of_view_extra_decay,
            velocity[1] * self.config.out_of_view_extra_decay,
        )
        should_stop_prediction = self.out_of_view_prediction_frames >= max(1, self.config.out_of_view_prediction_limit)
        return decayed_velocity, True, should_stop_prediction

    def _clamp_predicted_motion(
        self,
        predicted_position: tuple[float, float],
        velocity: tuple[float, float],
        frame_size: tuple[int, int] | None,
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        if frame_size is None:
            return predicted_position, velocity

        frame_width, frame_height = frame_size
        clamped_x = min(max(predicted_position[0], 0.0), max(0.0, frame_width - 1.0))
        clamped_y = min(max(predicted_position[1], 0.0), max(0.0, frame_height - 1.0))
        adjusted_vx, adjusted_vy = velocity

        if clamped_x != predicted_position[0]:
            adjusted_vx = 0.0
        if clamped_y != predicted_position[1]:
            adjusted_vy = 0.0

        clamped_position = (clamped_x, clamped_y)
        if self._is_near_boundary(clamped_position, frame_size):
            adjusted_vx *= self.config.prediction_boundary_extra_decay
            adjusted_vy *= self.config.prediction_boundary_extra_decay
        return clamped_position, (adjusted_vx, adjusted_vy)

    def _is_heading_out_of_view(
        self,
        position: tuple[float, float],
        velocity: tuple[float, float],
        frame_size: tuple[int, int],
    ) -> bool:
        frame_width, frame_height = frame_size
        top_margin = frame_height * self.config.out_of_view_top_margin_ratio
        side_margin = frame_width * self.config.out_of_view_side_margin_ratio
        bottom_margin = frame_height * self.config.out_of_view_bottom_margin_ratio
        velocity_threshold = max(0.0, self.config.out_of_view_velocity_threshold)
        x, y = position
        vx, vy = velocity

        moving_out_top = (y <= top_margin or y < 0.0) and vy <= -velocity_threshold
        moving_out_left = (x <= side_margin or x < 0.0) and vx <= -velocity_threshold
        moving_out_right = (x >= frame_width - 1.0 - side_margin or x > frame_width - 1.0) and vx >= velocity_threshold
        moving_out_bottom = (
            (y >= frame_height - 1.0 - bottom_margin or y > frame_height - 1.0)
            and vy >= velocity_threshold
        )
        return moving_out_top or moving_out_left or moving_out_right or moving_out_bottom

    def _is_near_boundary(self, position: tuple[float, float], frame_size: tuple[int, int]) -> bool:
        frame_width, frame_height = frame_size
        margin_x = frame_width * self.config.prediction_boundary_margin_ratio
        margin_y = frame_height * self.config.prediction_boundary_margin_ratio
        x, y = position
        return (
            x <= margin_x
            or x >= frame_width - 1.0 - margin_x
            or y <= margin_y
            or y >= frame_height - 1.0 - margin_y
        )

    def _peek_kalman_context(self) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float], float]:
        assert self.kalman is not None and self.kalman.is_initialized
        predicted_state, predicted_covariance = self.kalman.peek_predict(
            dt=1.0,
            process_noise_scale=self._process_noise_scale(reacquire=False),
        )
        predicted_position = (float(predicted_state[0]), float(predicted_state[1]))
        predicted_velocity = (float(predicted_state[2]), float(predicted_state[3]))
        predicted_acceleration = (float(predicted_state[4]), float(predicted_state[5]))
        position_std = math.sqrt(max(0.0, float(max(predicted_covariance[0, 0], predicted_covariance[1, 1]))))
        gating_radius = self.config.gate_sigma_scale * position_std
        gating_radius = min(max(gating_radius, self.config.gate_radius_min), self.config.gate_radius_max)
        return predicted_position, predicted_velocity, predicted_acceleration, gating_radius

    def _process_noise_scale(self, reacquire: bool) -> float:
        scale = self.config.process_noise_base
        if self.lost_frames > 0:
            scale *= 1.0 + self.lost_frames * (self.config.process_noise_lost_multiplier - 1.0)
        if reacquire:
            scale *= self.config.process_noise_reacquire_multiplier
        return max(scale, 1e-6)

    def _measurement_noise_scale(self, confidence: float) -> float:
        low_confidence_scale = 1.0 + max(0.0, 1.0 - confidence) * self.config.measurement_noise_low_conf_multiplier
        return self.config.measurement_noise_base * low_confidence_scale

    def _tame_reacquired_velocity(self, new_velocity: tuple[float, float]) -> tuple[float, float]:
        blend = min(max(self.config.velocity_blend_after_reacquire, 0.0), 1.0)
        blended_velocity = (
            self.velocity[0] * (1.0 - blend) + new_velocity[0] * blend,
            self.velocity[1] * (1.0 - blend) + new_velocity[1] * blend,
        )
        speed = math.hypot(blended_velocity[0], blended_velocity[1])
        speed_cap = max(1e-6, self.config.speed_cap_after_reacquire)
        if speed <= speed_cap:
            return blended_velocity

        ratio = speed_cap / speed
        return (blended_velocity[0] * ratio, blended_velocity[1] * ratio)

    def _advance_reacquire_stabilization(self, trigger_reacquire: bool) -> None:
        if trigger_reacquire:
            self.reacquire_stabilization_frames_remaining = max(0, self.config.reacquire_stabilization_frames)
            return
        if self.reacquire_stabilization_frames_remaining > 0:
            self.reacquire_stabilization_frames_remaining -= 1
