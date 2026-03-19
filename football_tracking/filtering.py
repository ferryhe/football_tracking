from __future__ import annotations

from collections import Counter
from typing import Any

from football_tracking.config import FilteringConfig
from football_tracking.scene_bias import SceneBiasResolver
from football_tracking.types import Candidate, TrackerContext


class CandidateFilter:
    """候选过滤层，只负责基础合法性过滤。"""

    def __init__(self, config: FilteringConfig, scene_bias: SceneBiasResolver | None = None) -> None:
        self.config = config
        self.scene_bias = scene_bias

    def filter(
        self,
        candidates: list[Candidate],
        context: TrackerContext,
        frame_index: int,
    ) -> tuple[list[Candidate], list[dict[str, Any]], dict[str, int]]:
        primary_result = self._run_filter(
            candidates=candidates,
            context=context,
            frame_index=frame_index,
            allow_dynamic_air_recovery=False,
            phase="primary",
        )
        if primary_result[0]:
            return primary_result

        if self.scene_bias is None or not self.scene_bias.is_dynamic_air_recovery_active(context):
            return primary_result

        return self._run_filter(
            candidates=candidates,
            context=context,
            frame_index=frame_index,
            allow_dynamic_air_recovery=True,
            phase="dynamic_air_fallback",
        )

    def _run_filter(
        self,
        candidates: list[Candidate],
        context: TrackerContext,
        frame_index: int,
        allow_dynamic_air_recovery: bool,
        phase: str,
    ) -> tuple[list[Candidate], list[dict[str, Any]], dict[str, int]]:
        filtered: list[Candidate] = []
        rejections: list[dict[str, Any]] = []
        for candidate in candidates:
            passed, rejection = self._passes(
                candidate,
                context,
                frame_index,
                allow_dynamic_air_recovery=allow_dynamic_air_recovery,
                phase=phase,
            )
            if not passed:
                if rejection is not None:
                    rejections.append(rejection)
                continue
            filtered.append(candidate)
        rejection_counts = dict(Counter(item["reason"] for item in rejections))
        return filtered, rejections, rejection_counts

    def _passes(
        self,
        candidate: Candidate,
        context: TrackerContext,
        frame_index: int,
        allow_dynamic_air_recovery: bool,
        phase: str,
    ) -> tuple[bool, dict[str, Any] | None]:
        """按配置检查置信度、尺寸、长宽比和 ROI。"""
        min_confidence = self.config.min_confidence
        min_width = self.config.min_width
        min_height = self.config.min_height
        ground_zone_name = None
        positive_zone_name = None

        if self.scene_bias is not None:
            negative_zone_name = self.scene_bias.get_negative_zone_name(candidate, context, frame_index)
            if negative_zone_name is not None:
                return False, self._build_rejection(
                    candidate,
                    f"negative_zone:{negative_zone_name}",
                    None,
                    None,
                    phase=phase,
                )

            ground_zone_name = self.scene_bias.get_ground_zone_name(candidate, context, frame_index)
            positive_zone_name = self.scene_bias.get_positive_zone_name(
                candidate,
                context,
                frame_index,
                include_dynamic_air_recovery=allow_dynamic_air_recovery,
            )
            if self.scene_bias.has_ground_zones() and ground_zone_name is None and positive_zone_name is None:
                return False, self._build_rejection(
                    candidate,
                    "outside_ground_and_positive_zones",
                    None,
                    None,
                    phase=phase,
                )

            relaxed_filtering, positive_zone_name = self.scene_bias.get_relaxed_filtering(
                candidate,
                context,
                frame_index,
                include_dynamic_air_recovery=allow_dynamic_air_recovery,
            )
            if relaxed_filtering is not None:
                if relaxed_filtering.min_confidence is not None:
                    min_confidence = min(min_confidence, relaxed_filtering.min_confidence)
                if relaxed_filtering.min_width is not None:
                    min_width = min(min_width, relaxed_filtering.min_width)
                if relaxed_filtering.min_height is not None:
                    min_height = min(min_height, relaxed_filtering.min_height)

        if candidate.confidence < min_confidence:
            return False, self._build_rejection(
                candidate,
                "confidence_below_min",
                ground_zone_name,
                positive_zone_name,
                phase=phase,
                threshold=min_confidence,
            )
        if candidate.width < min_width or candidate.width > self.config.max_width:
            return False, self._build_rejection(
                candidate,
                "width_out_of_range",
                ground_zone_name,
                positive_zone_name,
                phase=phase,
                min_value=min_width,
                max_value=self.config.max_width,
            )
        if candidate.height < min_height or candidate.height > self.config.max_height:
            return False, self._build_rejection(
                candidate,
                "height_out_of_range",
                ground_zone_name,
                positive_zone_name,
                phase=phase,
                min_value=min_height,
                max_value=self.config.max_height,
            )
        if candidate.aspect_ratio < self.config.min_aspect_ratio:
            return False, self._build_rejection(
                candidate,
                "aspect_ratio_too_small",
                ground_zone_name,
                positive_zone_name,
                phase=phase,
                threshold=self.config.min_aspect_ratio,
            )
        if candidate.aspect_ratio > self.config.max_aspect_ratio:
            return False, self._build_rejection(
                candidate,
                "aspect_ratio_too_large",
                ground_zone_name,
                positive_zone_name,
                phase=phase,
                threshold=self.config.max_aspect_ratio,
            )
        center_x, center_y = candidate.center
        if self.config.roi is not None:
            x1, y1, x2, y2 = self.config.roi
            if not (x1 <= center_x <= x2 and y1 <= center_y <= y2):
                return False, self._build_rejection(
                    candidate,
                    "outside_filtering_roi",
                    ground_zone_name,
                    positive_zone_name,
                    phase=phase,
                )
        return True, None

    def _build_rejection(
        self,
        candidate: Candidate,
        reason: str,
        ground_zone_name: str | None,
        positive_zone_name: str | None,
        phase: str,
        threshold: float | None = None,
        min_value: float | None = None,
        max_value: float | None = None,
    ) -> dict[str, Any]:
        rejection: dict[str, Any] = {
            "reason": reason,
            "phase": phase,
            "candidate_center": [round(candidate.center[0], 2), round(candidate.center[1], 2)],
            "candidate_bbox": [
                round(candidate.x1, 2),
                round(candidate.y1, 2),
                round(candidate.x2, 2),
                round(candidate.y2, 2),
            ],
            "confidence": round(candidate.confidence, 4),
            "width": round(candidate.width, 2),
            "height": round(candidate.height, 2),
            "aspect_ratio": round(candidate.aspect_ratio, 4),
            "ground_zone": ground_zone_name,
            "positive_zone": positive_zone_name,
        }
        if threshold is not None:
            rejection["threshold"] = round(threshold, 4)
        if min_value is not None:
            rejection["min_value"] = round(min_value, 4)
        if max_value is not None:
            rejection["max_value"] = round(max_value, 4)
        return rejection
