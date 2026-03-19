from __future__ import annotations

from football_tracking.config import RelaxedFilteringConfig, SceneBiasConfig, SceneZoneConfig
from football_tracking.types import Candidate, TrackerContext


class SceneBiasResolver:
    """Config-driven scene priors used by filtering and selection."""

    def __init__(self, config: SceneBiasConfig) -> None:
        self.config = config

    def has_ground_zones(self) -> bool:
        return self.config.enabled and bool(self.config.ground_zones)

    def is_dynamic_air_recovery_active(self, context: TrackerContext) -> bool:
        dynamic = self.config.dynamic_air_recovery
        if not self.config.enabled or not dynamic.enabled:
            return False
        if dynamic.active_states and context.state.value not in dynamic.active_states:
            return False
        anchor = self._get_dynamic_air_anchor(context)
        return anchor is not None

    def get_dynamic_air_window(
        self,
        context: TrackerContext,
        frame_shape: tuple[int, int] | None = None,
    ) -> tuple[int, int, int, int] | None:
        window = self._get_dynamic_air_window(context)
        if window is None:
            return None
        if frame_shape is None:
            return tuple(int(round(value)) for value in window)

        frame_height, frame_width = frame_shape
        left, top, right, bottom = window
        left_i = max(0, min(frame_width - 1, int(left)))
        top_i = max(0, min(frame_height - 1, int(top)))
        right_i = max(left_i + 1, min(frame_width, int(right)))
        bottom_i = max(top_i + 1, min(frame_height, int(bottom)))
        return (left_i, top_i, right_i, bottom_i)

    def get_ground_zone_name(
        self,
        candidate: Candidate,
        context: TrackerContext,
        frame_index: int,
    ) -> str | None:
        zone = self._match_zone(self.config.ground_zones, candidate, context, frame_index)
        return None if zone is None else zone.name

    def get_negative_zone_name(
        self,
        candidate: Candidate,
        context: TrackerContext,
        frame_index: int,
    ) -> str | None:
        zone = self._match_zone(self.config.negative_rois, candidate, context, frame_index)
        return None if zone is None else zone.name

    def get_positive_zone_name(
        self,
        candidate: Candidate,
        context: TrackerContext,
        frame_index: int,
        include_dynamic_air_recovery: bool = True,
    ) -> str | None:
        zone = self._match_zone(self.config.positive_rois, candidate, context, frame_index)
        if zone is not None:
            return zone.name
        if include_dynamic_air_recovery and self._is_in_dynamic_air_recovery(candidate, context):
            return "dynamic_air_recovery"
        return None

    def get_relaxed_filtering(
        self,
        candidate: Candidate,
        context: TrackerContext,
        frame_index: int,
        include_dynamic_air_recovery: bool = True,
    ) -> tuple[RelaxedFilteringConfig | None, str | None]:
        zone = self._match_zone(self.config.positive_rois, candidate, context, frame_index)
        if zone is not None:
            if (
                zone.relaxed_filtering.min_confidence is None
                and zone.relaxed_filtering.min_width is None
                and zone.relaxed_filtering.min_height is None
            ):
                return None, zone.name
            return zone.relaxed_filtering, zone.name

        if include_dynamic_air_recovery and self._is_in_dynamic_air_recovery(candidate, context):
            dynamic = self.config.dynamic_air_recovery
            if (
                dynamic.relaxed_filtering.min_confidence is None
                and dynamic.relaxed_filtering.min_width is None
                and dynamic.relaxed_filtering.min_height is None
            ):
                return None, "dynamic_air_recovery"
            return dynamic.relaxed_filtering, "dynamic_air_recovery"

        return None, None

    def get_selection_bonus(
        self,
        candidate: Candidate,
        context: TrackerContext,
        frame_index: int,
    ) -> tuple[float, str | None]:
        zone = self._match_zone(self.config.positive_rois, candidate, context, frame_index)
        if zone is not None:
            return zone.selection_bonus, zone.name

        if self._is_in_dynamic_air_recovery(candidate, context):
            return self.config.dynamic_air_recovery.selection_bonus, "dynamic_air_recovery"

        return 0.0, None

    def _match_zone(
        self,
        zones: list[SceneZoneConfig],
        candidate: Candidate,
        context: TrackerContext,
        frame_index: int,
    ) -> SceneZoneConfig | None:
        if not self.config.enabled:
            return None

        center_x, center_y = candidate.center
        for zone in zones:
            if not self._zone_is_active(zone, context, frame_index):
                continue
            if self._point_in_zone(center_x, center_y, zone):
                return zone
        return None

    def _zone_is_active(self, zone: SceneZoneConfig, context: TrackerContext, frame_index: int) -> bool:
        if zone.frame_range is not None:
            start_frame, end_frame = zone.frame_range
            if not (start_frame <= frame_index <= end_frame):
                return False
        if zone.active_states and context.state.value not in zone.active_states:
            return False
        return True

    def _point_in_zone(self, x: float, y: float, zone: SceneZoneConfig) -> bool:
        if zone.points:
            return self._point_in_polygon(x, y, zone.points)
        if zone.roi is None:
            return False
        x1, y1, x2, y2 = zone.roi
        return x1 <= x <= x2 and y1 <= y <= y2

    def _is_in_dynamic_air_recovery(self, candidate: Candidate, context: TrackerContext) -> bool:
        window = self._get_dynamic_air_window(context)
        if window is None:
            return False
        center_x, center_y = candidate.center
        left, top, right, bottom = window
        return left <= center_x <= right and top <= center_y <= bottom

    def _get_dynamic_air_window(self, context: TrackerContext) -> tuple[float, float, float, float] | None:
        dynamic = self.config.dynamic_air_recovery
        if not self.config.enabled or not dynamic.enabled:
            return None
        if dynamic.active_states and context.state.value not in dynamic.active_states:
            return None

        anchor = self._get_dynamic_air_anchor(context)
        if anchor is None:
            return None

        anchor_x, anchor_y = anchor
        width, up, down = self._dynamic_air_profile(anchor_y)
        return (
            anchor_x - width / 2.0,
            anchor_y - up,
            anchor_x + width / 2.0,
            anchor_y + down,
        )

    def _get_dynamic_air_anchor(self, context: TrackerContext) -> tuple[float, float] | None:
        if context.last_detected_position is not None:
            return context.last_detected_position
        return context.predicted_position or context.last_position

    def _dynamic_air_profile(self, anchor_y: float) -> tuple[float, float, float]:
        profile = self.config.dynamic_air_recovery.profile.lower()
        if profile != "fisheye_180_indoor":
            profile = "fisheye_180_indoor"

        if anchor_y >= 1000:
            return (1100.0, 430.0, 90.0)
        if anchor_y >= 800:
            return (900.0, 320.0, 80.0)
        if anchor_y >= 620:
            return (700.0, 240.0, 60.0)
        return (520.0, 180.0, 50.0)

    def _point_in_polygon(self, x: float, y: float, points: tuple[tuple[int, int], ...]) -> bool:
        inside = False
        point_count = len(points)
        if point_count < 3:
            return False

        x_prev, y_prev = points[-1]
        for x_curr, y_curr in points:
            if self._point_on_segment(x, y, x_prev, y_prev, x_curr, y_curr):
                return True

            intersects = ((y_curr > y) != (y_prev > y)) and (
                x < (x_prev - x_curr) * (y - y_curr) / ((y_prev - y_curr) or 1e-9) + x_curr
            )
            if intersects:
                inside = not inside
            x_prev, y_prev = x_curr, y_curr

        return inside

    def _point_on_segment(
        self,
        x: float,
        y: float,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
    ) -> bool:
        cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
        if abs(cross) > 1e-6:
            return False

        dot = (x - x1) * (x2 - x1) + (y - y1) * (y2 - y1)
        if dot < 0:
            return False

        squared_length = (x2 - x1) ** 2 + (y2 - y1) ** 2
        if dot > squared_length:
            return False

        return True
