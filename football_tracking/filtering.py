from __future__ import annotations

from football_tracking.config import FilteringConfig
from football_tracking.types import Candidate


class CandidateFilter:
    """候选过滤层，只负责基础合法性过滤。"""

    def __init__(self, config: FilteringConfig) -> None:
        self.config = config

    def filter(self, candidates: list[Candidate]) -> list[Candidate]:
        filtered: list[Candidate] = []
        for candidate in candidates:
            if not self._passes(candidate):
                continue
            filtered.append(candidate)
        return filtered

    def _passes(self, candidate: Candidate) -> bool:
        """按配置检查置信度、尺寸、长宽比和 ROI。"""
        if candidate.confidence < self.config.min_confidence:
            return False
        if candidate.width < self.config.min_width or candidate.width > self.config.max_width:
            return False
        if candidate.height < self.config.min_height or candidate.height > self.config.max_height:
            return False
        if candidate.aspect_ratio < self.config.min_aspect_ratio:
            return False
        if candidate.aspect_ratio > self.config.max_aspect_ratio:
            return False
        if self.config.roi is not None:
            center_x, center_y = candidate.center
            x1, y1, x2, y2 = self.config.roi
            if not (x1 <= center_x <= x2 and y1 <= center_y <= y2):
                return False
        return True
