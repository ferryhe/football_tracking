from __future__ import annotations

import math

import numpy as np


class ConstantAccelerationKalmanFilter:
    """Small CA Kalman filter used only inside tracking."""

    def __init__(self) -> None:
        self.state: np.ndarray | None = None
        self.covariance: np.ndarray | None = None

    @property
    def is_initialized(self) -> bool:
        return self.state is not None and self.covariance is not None

    def initialize(
        self,
        position: tuple[float, float],
        velocity: tuple[float, float] = (0.0, 0.0),
        acceleration: tuple[float, float] = (0.0, 0.0),
    ) -> None:
        x, y = position
        vx, vy = velocity
        ax, ay = acceleration
        self.state = np.array([x, y, vx, vy, ax, ay], dtype=float)
        self.covariance = np.diag([400.0, 400.0, 225.0, 225.0, 100.0, 100.0]).astype(float)

    def predict(self, dt: float = 1.0, process_noise_scale: float = 1.0) -> None:
        if not self.is_initialized:
            return
        predicted_state, predicted_covariance = self.peek_predict(dt, process_noise_scale)
        self.state = predicted_state
        self.covariance = predicted_covariance

    def peek_predict(self, dt: float = 1.0, process_noise_scale: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
        if not self.is_initialized:
            raise RuntimeError("Kalman filter must be initialized before predict.")
        transition = self._transition_matrix(dt)
        process_noise = self._process_noise_matrix(dt, process_noise_scale)
        predicted_state = transition @ self.state
        predicted_covariance = transition @ self.covariance @ transition.T + process_noise
        return predicted_state, predicted_covariance

    def update(self, measurement: tuple[float, float], measurement_noise_scale: float = 1.0) -> None:
        if not self.is_initialized:
            self.initialize(measurement)
            return

        measurement_vector = np.array([[measurement[0]], [measurement[1]]], dtype=float)
        measurement_matrix = np.array(
            [
                [1.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                [0.0, 1.0, 0.0, 0.0, 0.0, 0.0],
            ],
            dtype=float,
        )
        measurement_noise = np.eye(2, dtype=float) * measurement_noise_scale

        state_column = self.state.reshape(6, 1)
        innovation = measurement_vector - measurement_matrix @ state_column
        innovation_covariance = measurement_matrix @ self.covariance @ measurement_matrix.T + measurement_noise
        kalman_gain = self.covariance @ measurement_matrix.T @ np.linalg.inv(innovation_covariance)

        updated_state = state_column + kalman_gain @ innovation
        identity = np.eye(6, dtype=float)
        updated_covariance = (identity - kalman_gain @ measurement_matrix) @ self.covariance

        self.state = updated_state.reshape(6)
        self.covariance = updated_covariance

    def get_position(self) -> tuple[float, float]:
        if not self.is_initialized:
            return (0.0, 0.0)
        return (float(self.state[0]), float(self.state[1]))

    def get_velocity(self) -> tuple[float, float]:
        if not self.is_initialized:
            return (0.0, 0.0)
        return (float(self.state[2]), float(self.state[3]))

    def get_acceleration(self) -> tuple[float, float]:
        if not self.is_initialized:
            return (0.0, 0.0)
        return (float(self.state[4]), float(self.state[5]))

    def set_position(self, position: tuple[float, float]) -> None:
        if not self.is_initialized:
            self.initialize(position)
            return
        self.state[0] = position[0]
        self.state[1] = position[1]

    def set_motion(
        self,
        velocity: tuple[float, float] | None = None,
        acceleration: tuple[float, float] | None = None,
    ) -> None:
        if not self.is_initialized:
            return
        if velocity is not None:
            self.state[2] = velocity[0]
            self.state[3] = velocity[1]
        if acceleration is not None:
            self.state[4] = acceleration[0]
            self.state[5] = acceleration[1]

    def get_position_std(self) -> float:
        if not self.is_initialized:
            return 0.0
        variance_x = max(0.0, float(self.covariance[0, 0]))
        variance_y = max(0.0, float(self.covariance[1, 1]))
        return math.sqrt(max(variance_x, variance_y))

    def _transition_matrix(self, dt: float) -> np.ndarray:
        half_dt_sq = 0.5 * dt * dt
        return np.array(
            [
                [1.0, 0.0, dt, 0.0, half_dt_sq, 0.0],
                [0.0, 1.0, 0.0, dt, 0.0, half_dt_sq],
                [0.0, 0.0, 1.0, 0.0, dt, 0.0],
                [0.0, 0.0, 0.0, 1.0, 0.0, dt],
                [0.0, 0.0, 0.0, 0.0, 1.0, 0.0],
                [0.0, 0.0, 0.0, 0.0, 0.0, 1.0],
            ],
            dtype=float,
        )

    def _process_noise_matrix(self, dt: float, process_noise_scale: float) -> np.ndarray:
        axis_noise = np.array([[dt**3 / 6.0], [dt**2 / 2.0], [dt]], dtype=float)
        axis_covariance = process_noise_scale * (axis_noise @ axis_noise.T)
        process_noise = np.zeros((6, 6), dtype=float)
        process_noise[np.ix_([0, 2, 4], [0, 2, 4])] = axis_covariance
        process_noise[np.ix_([1, 3, 5], [1, 3, 5])] = axis_covariance
        return process_noise
