from __future__ import annotations

import unittest

import cv2
import numpy as np

from football_tracking.ball_annotation_propagation import (
    PropagationError,
    build_advisory_suggestions,
)
from football_tracking.ball_detector_feasibility import temporal_group_for_frame


class BallAnnotationPropagationTests(unittest.TestCase):
    @staticmethod
    def _frame(ball_x: int) -> bytes:
        image = np.zeros((48, 96, 3), dtype=np.uint8)
        image[:, :] = (16, 128, 16)
        cv2.circle(image, (ball_x, 24), 3, (240, 240, 240), -1)
        ok, encoded = cv2.imencode(".jpg", image)
        if not ok:
            raise AssertionError("frame fixture could not be encoded")
        return encoded.tobytes()

    @staticmethod
    def _textured_frame(ball_x: int | None) -> bytes:
        rng = np.random.default_rng(5)
        texture = rng.integers(-25, 26, size=(48, 96), dtype=np.int16)
        image = np.empty((48, 96, 3), dtype=np.int16)
        image[:, :, 0] = 16 + texture
        image[:, :, 1] = 128 + texture
        image[:, :, 2] = 16 + texture
        image = np.clip(image, 0, 255).astype(np.uint8)
        if ball_x is not None:
            cv2.circle(image, (ball_x, 24), 3, (240, 240, 240), -1)
        ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        if not ok:
            raise AssertionError("textured frame fixture could not be encoded")
        return encoded.tobytes()

    def test_short_window_outputs_are_suggested_excluded_and_bound_to_seed_group(self) -> None:
        result = build_advisory_suggestions(
            seed_frame_index=20,
            seed_group=temporal_group_for_frame("b" * 64, 20),
            seed_annotation={
                "point_source_px": {"x": 40.0, "y": 24.0},
                "bbox_source_px": {"left": 37.0, "top": 21.0, "right": 43.0, "bottom": 27.0},
                "visibility": "visible",
            },
            radius_frames=2,
            source_frame_count=100,
            seed_frame_bytes=self._frame(40),
            target_frame_bytes={
                18: self._frame(36),
                19: self._frame(38),
                21: self._frame(42),
                22: self._frame(44),
            },
            source_width=96,
            source_height=48,
        )
        suggestions = result["suggestions"]
        self.assertEqual([18, 19, 21, 22], [item["frame_index"] for item in suggestions])
        self.assertTrue(all(item["annotation_state"] == "suggested" for item in suggestions))
        self.assertTrue(all(item["training_use"] == "excluded" for item in suggestions))
        expected_group = temporal_group_for_frame("b" * 64, 20)
        self.assertTrue(all(item["temporal_group_id"] == expected_group["group_id"] for item in suggestions))
        self.assertTrue(all(item["temporal_group"]["group_id"] == expected_group["group_id"] for item in suggestions))
        self.assertTrue(any(item["point_source_px"]["x"] != 40.0 for item in suggestions))
        self.assertEqual(4, result["summary"]["succeeded_frame_count"])
        self.assertIsNone(result["summary"]["human_validated_safe_span_frames"])

    def test_propagation_cannot_cross_group_or_invent_from_pointless_seed(self) -> None:
        with self.assertRaisesRegex(PropagationError, "confirmed point or box"):
            build_advisory_suggestions(
                seed_frame_index=20,
                seed_group=temporal_group_for_frame("b" * 64, 20),
                seed_annotation={"point_source_px": None, "bbox_source_px": None},
                radius_frames=2,
                source_frame_count=100,
                seed_frame_bytes=self._frame(40),
                target_frame_bytes={18: self._frame(36), 19: self._frame(38), 21: self._frame(42), 22: self._frame(44)},
                source_width=96,
                source_height=48,
            )
        with self.assertRaisesRegex(PropagationError, "within the frozen temporal group"):
            build_advisory_suggestions(
                seed_frame_index=20,
                seed_group={
                    **temporal_group_for_frame("b" * 64, 20),
                    "start_frame": 20,
                    "end_frame": 20,
                },
                seed_annotation={
                    "point_source_px": {"x": 12.0, "y": 12.0},
                    "bbox_source_px": None,
                },
                radius_frames=2,
                source_frame_count=100,
                seed_frame_bytes=self._frame(40),
                target_frame_bytes={
                    18: self._frame(36),
                    19: self._frame(38),
                    21: self._frame(42),
                    22: self._frame(44),
                },
                source_width=96,
                source_height=48,
            )

    def test_static_grass_texture_cannot_become_a_ball_when_ball_disappears(self) -> None:
        group = temporal_group_for_frame("c" * 64, 20)
        seed = {
            "point_source_px": {"x": 40.0, "y": 24.0},
            "bbox_source_px": {
                "left": 37.0,
                "top": 21.0,
                "right": 43.0,
                "bottom": 27.0,
            },
            "visibility": "visible",
        }
        result = build_advisory_suggestions(
            seed_frame_index=20,
            seed_group=group,
            seed_annotation=seed,
            radius_frames=2,
            source_frame_count=100,
            seed_frame_bytes=self._textured_frame(40),
            target_frame_bytes={
                18: self._textured_frame(None),
                19: self._textured_frame(38),
                21: self._textured_frame(42),
                22: self._textured_frame(None),
            },
            source_width=96,
            source_height=48,
        )
        self.assertEqual([19, 21], [item["frame_index"] for item in result["suggestions"]])
        centers = {item["frame_index"]: item["point_source_px"]["x"] for item in result["suggestions"]}
        self.assertAlmostEqual(38.0, centers[19], delta=1.0)
        self.assertAlmostEqual(42.0, centers[21], delta=1.0)
        failed = {item["frame_index"]: item for item in result["frame_results"] if item["status"] == "failed"}
        self.assertEqual("forward_match_below_threshold", failed[18]["failure_code"])
        self.assertEqual("forward_match_below_threshold", failed[22]["failure_code"])
        self.assertTrue(all(item["suggestion_id"] is None for item in failed.values()))

        stopped = build_advisory_suggestions(
            seed_frame_index=20,
            seed_group=group,
            seed_annotation=seed,
            radius_frames=2,
            source_frame_count=100,
            seed_frame_bytes=self._textured_frame(40),
            target_frame_bytes={
                18: self._textured_frame(36),
                19: self._textured_frame(None),
                21: self._textured_frame(42),
                22: self._textured_frame(44),
            },
            source_width=96,
            source_height=48,
        )
        stopped_by_frame = {item["frame_index"]: item for item in stopped["frame_results"]}
        self.assertEqual("forward_match_below_threshold", stopped_by_frame[19]["failure_code"])
        self.assertEqual(
            "stopped_after_nearer_frame_failed",
            stopped_by_frame[18]["failure_code"],
        )
        self.assertNotIn(18, {item["frame_index"] for item in stopped["suggestions"]})


if __name__ == "__main__":
    unittest.main()
