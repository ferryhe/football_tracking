from __future__ import annotations

import unittest

from football_tracking.config import DynamicAirRecoveryConfig, SceneBiasConfig
from football_tracking.scene_bias import SceneBiasResolver
from football_tracking.types import TrackerContext, TrackState


class SceneBiasResolverTests(unittest.TestCase):
    def test_dynamic_air_window_covers_right_corner_drop_from_last_anchor(self) -> None:
        resolver = SceneBiasResolver(
            SceneBiasConfig(
                enabled=True,
                dynamic_air_recovery=DynamicAirRecoveryConfig(enabled=True),
            )
        )
        context = TrackerContext(
            state=TrackState.LOST,
            last_detected_position=(4839.41, 795.80),
            predicted_position=(4861.47, 773.14),
            lost_frames=43,
        )

        window = resolver.get_dynamic_air_window(context, frame_shape=(1440, 5120))

        self.assertIsNotNone(window)
        assert window is not None
        left, top, right, bottom = window
        self.assertLessEqual(left, 4757.0)
        self.assertGreaterEqual(right, 4758.0)
        self.assertLessEqual(top, 972.0)
        self.assertGreaterEqual(bottom, 973.0)


if __name__ == "__main__":
    unittest.main()
