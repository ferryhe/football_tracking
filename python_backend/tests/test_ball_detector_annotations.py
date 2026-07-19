from __future__ import annotations

import unittest

from football_tracking.ball_detector_annotations import (
    BallAnnotationError,
    annotation_etag,
    validate_ball_annotation,
)


class BallDetectorAnnotationContractTests(unittest.TestCase):
    def test_confirmed_positive_requires_human_box_and_normalizes_center(self) -> None:
        annotation = validate_ball_annotation(
            {
                "point_source_px": {"x": 15.0, "y": 12.0},
                "bbox_source_px": {"left": 10.0, "top": 8.0, "right": 20.0, "bottom": 16.0},
                "presence": "present",
                "visibility": "visible",
                "training_use": "positive",
                "annotation_state": "confirmed",
                "scale_stratum": "far",
                "lighting_tag": "bright_sun",
                "motion_occlusion_tags": ["airborne"],
                "provenance": "manual_human_annotation",
            },
            width=64,
            height=32,
            data_role="development",
        )

        self.assertEqual({"x": 15.0, "y": 12.0}, annotation["point_source_px"])
        self.assertEqual("positive", annotation["training_use"])
        self.assertEqual("confirmed", annotation["annotation_state"])

    def test_presence_visibility_and_training_use_are_orthogonal_and_fail_closed(self) -> None:
        base = {
            "point_source_px": None,
            "bbox_source_px": None,
            "presence": "unknown",
            "visibility": "unresolvable",
            "training_use": "excluded",
            "annotation_state": "confirmed",
            "scale_stratum": "not_applicable",
            "lighting_tag": "shadow",
            "motion_occlusion_tags": ["occluded"],
            "provenance": "manual_human_annotation",
        }
        self.assertEqual("unknown", validate_ball_annotation(base, width=64, height=32)["presence"])

        invalid = (
            (
                {**base, "presence": "absent", "visibility": "not_applicable", "training_use": "positive"},
                "confirmed absent",
            ),
            (
                {**base, "presence": "present", "visibility": "visible", "training_use": "positive"},
                "confirmed bounding box",
            ),
            ({**base, "presence": "unknown", "point_source_px": {"x": 1.0, "y": 1.0}}, "cannot carry coordinates"),
            (
                {**base, "presence": "present", "visibility": "unresolvable", "point_source_px": {"x": 1.0, "y": 1.0}},
                "unresolvable",
            ),
        )
        for payload, message in invalid:
            with self.subTest(message=message), self.assertRaisesRegex(BallAnnotationError, message):
                validate_ball_annotation(payload, width=64, height=32)

    def test_check_truth_is_evaluation_only_and_suggestions_never_become_truth(self) -> None:
        box = {
            "point_source_px": {"x": 15.0, "y": 12.0},
            "bbox_source_px": {"left": 10.0, "top": 8.0, "right": 20.0, "bottom": 16.0},
            "presence": "present",
            "visibility": "partial",
            "training_use": "excluded",
            "annotation_state": "confirmed",
            "scale_stratum": "far",
            "lighting_tag": "backlight",
            "motion_occlusion_tags": [],
            "provenance": "manual_human_annotation",
        }
        self.assertEqual(
            "excluded",
            validate_ball_annotation(box, width=64, height=32, data_role="check")["training_use"],
        )
        with self.assertRaisesRegex(BallAnnotationError, "check annotations are evaluation-only"):
            validate_ball_annotation({**box, "training_use": "positive"}, width=64, height=32, data_role="check")
        with self.assertRaisesRegex(BallAnnotationError, "suggested annotations are never truth"):
            validate_ball_annotation(
                {**box, "annotation_state": "suggested", "training_use": "positive"},
                width=64,
                height=32,
            )
        with self.assertRaisesRegex(BallAnnotationError, "confirmed bounding box"):
            validate_ball_annotation(
                {
                    **box,
                    "bbox_source_px": None,
                    "provenance": "manual_human_annotation",
                },
                width=64,
                height=32,
                data_role="check",
            )

    def test_source_pixel_bounds_finiteness_and_point_box_consistency(self) -> None:
        base = {
            "presence": "present",
            "visibility": "visible",
            "training_use": "excluded",
            "annotation_state": "confirmed",
            "scale_stratum": "mid",
            "lighting_tag": "shadow",
            "motion_occlusion_tags": [],
            "provenance": "manual_human_annotation",
        }
        invalid = (
            {**base, "point_source_px": {"x": 64.0, "y": 1.0}, "bbox_source_px": None},
            {**base, "point_source_px": {"x": float("nan"), "y": 1.0}, "bbox_source_px": None},
            {
                **base,
                "point_source_px": {"x": 9.0, "y": 9.0},
                "bbox_source_px": {"left": 10.0, "top": 8.0, "right": 20.0, "bottom": 16.0},
            },
            {
                **base,
                "point_source_px": None,
                "bbox_source_px": {"left": 10.0, "top": 8.0, "right": 65.0, "bottom": 16.0},
            },
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(BallAnnotationError):
                validate_ball_annotation(payload, width=64, height=32)

    def test_etag_binds_effective_revision_content(self) -> None:
        first = annotation_etag("session-a", 10, 1, {"presence": "absent"})
        self.assertEqual(first, annotation_etag("session-a", 10, 1, {"presence": "absent"}))
        self.assertNotEqual(first, annotation_etag("session-a", 10, 2, {"presence": "absent"}))
        self.assertNotEqual(first, annotation_etag("session-a", 10, 1, {"presence": "unknown"}))


if __name__ == "__main__":
    unittest.main()
