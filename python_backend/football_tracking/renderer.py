from __future__ import annotations

import cv2

from football_tracking.config import OutputConfig
from football_tracking.types import OutputStatus, TrackResult


class FrameRenderer:
    """输出层的一部分：负责在帧上绘制球和文本。"""

    def __init__(self, config: OutputConfig) -> None:
        self.config = config
        self.ball_color = (0, 255, 255)
        self.text_color = (255, 255, 255)
        self.text_shadow = (0, 0, 0)

    def render(self, frame, track_result: TrackResult):
        annotated = frame.copy()

        # 只有真实检测和短时预测允许继续绘制，Lost 必须停止绘制球。
        if track_result.point is not None and track_result.output_status != OutputStatus.LOST:
            center = (int(round(track_result.point.x)), int(round(track_result.point.y)))
            cv2.circle(
                annotated,
                center,
                self.config.draw_radius,
                self.ball_color,
                self.config.draw_thickness,
                lineType=cv2.LINE_AA,
            )

        self._draw_frame_index(annotated, track_result.frame_index)
        if self.config.draw_status_text:
            self._draw_status(annotated, track_result)
        return annotated

    def _draw_frame_index(self, frame, frame_index: int) -> None:
        text = f"Frame: {frame_index}"
        origin = (24, frame.shape[0] - 24)
        self._draw_text(frame, text, origin)

    def _draw_status(self, frame, track_result: TrackResult) -> None:
        text = f"Status: {track_result.output_status.value} | State: {track_result.state.value}"
        origin = (24, 40)
        self._draw_text(frame, text, origin)

    def _draw_text(self, frame, text: str, origin: tuple[int, int]) -> None:
        cv2.putText(
            frame,
            text,
            (origin[0] + 2, origin[1] + 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            self.config.frame_text_scale,
            self.text_shadow,
            self.config.frame_text_thickness + 1,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            text,
            origin,
            cv2.FONT_HERSHEY_SIMPLEX,
            self.config.frame_text_scale,
            self.text_color,
            self.config.frame_text_thickness,
            cv2.LINE_AA,
        )
