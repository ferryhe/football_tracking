from __future__ import annotations

import unittest

from football_tracking.calibration import (
    build_pitch_calibration_from_field_polygon,
    field_polygon_to_pitch_corners,
    image_point_to_pitch,
    pitch_point_to_image,
)
from football_tracking.geometry import (
    DEFAULT_PITCH_LENGTH_METERS,
    DEFAULT_PITCH_WIDTH_METERS,
    compute_homography,
    default_pitch_corners,
    invert_homography,
    map_point,
)


class GeometryTests(unittest.TestCase):
    def assertPointAlmostEqual(
        self,
        actual: tuple[float, float] | None,
        expected: tuple[float, float],
        *,
        places: int = 5,
    ) -> None:
        self.assertIsNotNone(actual)
        assert actual is not None
        self.assertAlmostEqual(expected[0], actual[0], places=places)
        self.assertAlmostEqual(expected[1], actual[1], places=places)

    def test_default_pitch_corners_define_105_by_68_meter_plane(self) -> None:
        self.assertEqual(105.0, DEFAULT_PITCH_LENGTH_METERS)
        self.assertEqual(68.0, DEFAULT_PITCH_WIDTH_METERS)
        self.assertEqual(
            [(0.0, 0.0), (105.0, 0.0), (105.0, 68.0), (0.0, 68.0)],
            default_pitch_corners(),
        )

    def test_homography_maps_known_rectangle_forward_and_back(self) -> None:
        image_points = [(10.0, 20.0), (220.0, 20.0), (220.0, 156.0), (10.0, 156.0)]
        pitch_points = default_pitch_corners()

        image_to_pitch = compute_homography(image_points, pitch_points)
        pitch_to_image = invert_homography(image_to_pitch)

        self.assertIsNotNone(image_to_pitch)
        self.assertIsNotNone(pitch_to_image)
        self.assertPointAlmostEqual(map_point((10.0, 20.0), image_to_pitch), (0.0, 0.0))
        self.assertPointAlmostEqual(map_point((220.0, 156.0), image_to_pitch), (105.0, 68.0))
        self.assertPointAlmostEqual(map_point((115.0, 88.0), image_to_pitch), (52.5, 34.0))
        self.assertPointAlmostEqual(map_point((52.5, 34.0), pitch_to_image), (115.0, 88.0))

    def test_degenerate_homography_inputs_return_none(self) -> None:
        image_points = [(5.0, 5.0), (5.0, 5.0), (5.0, 5.0), (5.0, 5.0)]

        self.assertIsNone(compute_homography(image_points, default_pitch_corners()))
        self.assertIsNone(compute_homography(None, default_pitch_corners()))
        self.assertIsNone(map_point((1.0, 2.0), None))

    def test_calibration_derives_four_corners_from_nine_point_field_polygon(self) -> None:
        field_polygon = [
            (10, 20),
            (48, 20),
            (86, 20),
            (115, 20),
            (144, 20),
            (182, 20),
            (220, 20),
            (220, 156),
            (10, 156),
        ]

        calibration = build_pitch_calibration_from_field_polygon(
            field_polygon,
            confidence="detected",
            source="field-green-heuristic",
        )

        self.assertIsNotNone(calibration)
        assert calibration is not None
        self.assertEqual([[10.0, 20.0], [220.0, 20.0], [220.0, 156.0], [10.0, 156.0]], calibration["image_points"])
        self.assertEqual([[0.0, 0.0], [105.0, 0.0], [105.0, 68.0], [0.0, 68.0]], calibration["pitch_points"])
        self.assertEqual({"length_m": 105.0, "width_m": 68.0}, calibration["pitch_dimensions"])
        self.assertEqual("estimated", calibration["confidence"])
        self.assertEqual("field-green-heuristic:field-polygon-corners", calibration["source"])
        self.assertPointAlmostEqual(map_point((115.0, 88.0), calibration["image_to_pitch_matrix"]), (52.5, 34.0))
        self.assertPointAlmostEqual(image_point_to_pitch((115.0, 88.0), calibration), (52.5, 34.0))
        self.assertPointAlmostEqual(pitch_point_to_image((52.5, 34.0), calibration), (115.0, 88.0))

    def test_degenerate_field_polygon_omits_calibration(self) -> None:
        self.assertIsNone(
            build_pitch_calibration_from_field_polygon(
                [(5, 5), (5, 5), (5, 5), (5, 5)],
                confidence="fallback",
                source="test",
            )
        )

    def test_non_standard_polygons_do_not_publish_calibration(self) -> None:
        self.assertIsNone(field_polygon_to_pitch_corners([(0, 0), (20, 0), (25, 10), (15, 20), (0, 20)]))
        self.assertIsNone(
            field_polygon_to_pitch_corners(
                [
                    (10, 20),
                    (80, 90),
                    (20, 40),
                    (130, 30),
                    (60, 25),
                    (160, 50),
                    (220, 20),
                    (220, 156),
                    (10, 156),
                ]
            )
        )
        self.assertIsNone(
            field_polygon_to_pitch_corners(
                [
                    (10, 20),
                    (48, 20),
                    (86, 20),
                    (115, 20),
                    (144, 20),
                    (182, 20),
                    (220, 20),
                    (220, 156),
                    (10, 156),
                    (4, 88),
                ]
            )
        )
        self.assertIsNone(
            build_pitch_calibration_from_field_polygon(
                None,
                confidence="fallback",
                source="test",
            )
        )


if __name__ == "__main__":
    unittest.main()
