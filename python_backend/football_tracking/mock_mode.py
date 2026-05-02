from __future__ import annotations

import numpy as np

from football_tracking.config import MockConfig


class MockFrameSource:
    """联调用假帧源，生成固定尺寸的纯色帧，模拟视频读取接口。"""

    def __init__(self, config: MockConfig) -> None:
        self.config = config
        self.frame_index = 0
        self.frame_count = max(1, config.frame_count)
        self.width = max(64, config.frame_width)
        self.height = max(64, config.frame_height)
        self.fps = max(1.0, config.fps)
        self.background_color = max(0, min(255, config.background_color))

    def read(self) -> tuple[bool, np.ndarray | None]:
        """按 cv2.VideoCapture.read() 风格返回假帧。"""
        if self.frame_index >= self.frame_count:
            return False, None

        frame = np.full(
            (self.height, self.width, 3),
            fill_value=self.background_color,
            dtype=np.uint8,
        )
        self.frame_index += 1
        return True, frame

    def release(self) -> None:
        """与真实视频源保持统一接口，mock 模式下无需释放资源。"""
        return None
