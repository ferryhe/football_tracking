from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from football_tracking.camera_motion_audit import write_camera_motion_audit_report


class CameraMotionAuditTests(unittest.TestCase):
    def test_missing_camera_path_writes_unavailable_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)

            payload = write_camera_motion_audit_report(output_dir)

            self.assertEqual("unavailable", payload["summary"]["status"])
            self.assertEqual("camera_path.csv not found", payload["summary"]["reason"])
            self.assertEqual(0, payload["summary"]["frame_count"])
            self.assertEqual([], payload["review_events"])
            self.assertEqual(payload, self.read_report(output_dir))

    def test_empty_camera_path_writes_unavailable_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "camera_path.csv").write_text("", encoding="utf-8")

            payload = write_camera_motion_audit_report(output_dir)

            self.assertEqual("unavailable", payload["summary"]["status"])
            self.assertEqual("camera_path.csv is empty", payload["summary"]["reason"])
            self.assertEqual([], payload["review_events"])

    def test_missing_required_columns_writes_unavailable_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            self.write_rows(output_dir, ["Frame", "CenterX"], [["0", "100"]])

            payload = write_camera_motion_audit_report(output_dir)

            self.assertEqual("unavailable", payload["summary"]["status"])
            self.assertIn("missing required columns", payload["summary"]["reason"])
            self.assertEqual([], payload["review_events"])

    def test_invalid_numeric_value_writes_unavailable_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            self.write_camera_path(output_dir, [["0", "not-a-number", "0", "960", "540", "Detected", "glide"]])

            payload = write_camera_motion_audit_report(output_dir)

            self.assertEqual("unavailable", payload["summary"]["status"])
            self.assertEqual("camera_path.csv contains invalid numeric data", payload["summary"]["reason"])
            self.assertEqual([], payload["review_events"])

    def test_fractional_frame_writes_unavailable_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            self.write_camera_path(output_dir, [["1.9", "0", "0", "960", "540", "Detected", "glide"]])

            payload = write_camera_motion_audit_report(output_dir)

            self.assertEqual("unavailable", payload["summary"]["status"])
            self.assertEqual("camera_path.csv contains invalid numeric data", payload["summary"]["reason"])

    def test_duplicate_frame_writes_unavailable_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            self.write_camera_path(
                output_dir,
                [
                    ["0", "0", "0", "960", "540", "Detected", "glide"],
                    ["0", "10", "0", "960", "540", "Detected", "glide"],
                ],
            )

            payload = write_camera_motion_audit_report(output_dir)

            self.assertEqual("unavailable", payload["summary"]["status"])
            self.assertEqual("camera_path.csv contains invalid numeric data", payload["summary"]["reason"])

    def test_smooth_camera_path_has_no_review_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            self.write_camera_path(
                output_dir,
                [
                    ["0", "100", "100", "960", "540", "Detected", "glide"],
                    ["1", "110", "104", "960", "540", "Detected", "glide"],
                    ["2", "120", "108", "960", "540", "Predicted", "catch_up"],
                ],
            )

            payload = write_camera_motion_audit_report(output_dir)

            self.assertEqual("ok", payload["summary"]["status"])
            self.assertEqual(3, payload["summary"]["frame_count"])
            self.assertEqual(0, payload["summary"]["review_event_count"])
            self.assertEqual([], payload["review_events"])

    def test_pan_spike_creates_warn_review_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            self.write_camera_path(
                output_dir,
                [
                    ["0", "100", "100", "960", "540", "Detected", "glide"],
                    ["1", "150", "100", "960", "540", "Predicted", "catch_up"],
                ],
            )

            payload = write_camera_motion_audit_report(output_dir)

            self.assertEqual("warn", payload["summary"]["status"])
            self.assertEqual(1, payload["summary"]["review_event_count"])
            event = payload["review_events"][0]
            self.assertEqual("camera_motion_spike", event["type"])
            self.assertEqual("warn", event["severity"])
            self.assertEqual(100.0, event["evidence"]["max_step_px"])
            self.assertEqual(["catch_up"], event["evidence"]["pan_modes"])
            self.assertEqual(["Predicted"], event["evidence"]["statuses"])

    def test_fail_pan_spike_sets_fail_severity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            self.write_camera_path(
                output_dir,
                [
                    ["0", "100", "100", "960", "540", "Detected", "glide"],
                    ["1", "180", "100", "960", "540", "Detected", "glide"],
                ],
            )

            payload = write_camera_motion_audit_report(output_dir)

            self.assertEqual("fail", payload["summary"]["status"])
            self.assertEqual("fail", payload["review_events"][0]["severity"])
            self.assertEqual(160.0, payload["review_events"][0]["evidence"]["max_step_px"])

    def test_acceleration_spike_uses_adjacent_output_space_velocity_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            self.write_camera_path(
                output_dir,
                [
                    ["0", "0", "0", "960", "540", "Detected", "glide"],
                    ["1", "40", "0", "960", "540", "Detected", "glide"],
                    ["2", "0", "0", "960", "540", "Detected", "glide"],
                ],
            )

            payload = write_camera_motion_audit_report(output_dir)

            self.assertEqual(1, payload["summary"]["review_event_count"])
            event = payload["review_events"][0]
            self.assertEqual("camera_acceleration_spike", event["type"])
            self.assertEqual("fail", event["severity"])
            self.assertEqual(160.0, event["evidence"]["max_accel_px"])

    def test_zoom_jump_creates_review_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            self.write_camera_path(
                output_dir,
                [
                    ["0", "100", "100", "960", "540", "Detected", "glide"],
                    ["1", "100", "100", "960", "570", "Detected", "glide"],
                ],
            )

            payload = write_camera_motion_audit_report(output_dir)

            self.assertEqual("warn", payload["summary"]["status"])
            event = payload["review_events"][0]
            self.assertEqual("camera_zoom_jump", event["type"])
            self.assertEqual("warn", event["severity"])
            self.assertEqual(30.0, event["evidence"]["max_zoom_step_px"])
            self.assertAlmostEqual(30 / 540, event["evidence"]["max_zoom_step_ratio"])

    def test_non_contiguous_frames_divide_zoom_by_frame_delta(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            self.write_camera_path(
                output_dir,
                [
                    ["0", "100", "100", "960", "540", "Detected", "glide"],
                    ["10", "100", "100", "960", "640", "Detected", "glide"],
                ],
            )

            payload = write_camera_motion_audit_report(output_dir)

            self.assertEqual("ok", payload["summary"]["status"])
            self.assertEqual(10.0, payload["summary"]["max_zoom_step_px"])
            self.assertAlmostEqual(10 / 540, payload["summary"]["max_zoom_step_ratio"])
            self.assertEqual([], payload["review_events"])

    def test_output_space_scaling_uses_current_crop_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            self.write_camera_path(
                output_dir,
                [
                    ["0", "0", "0", "1000", "500", "Detected", "glide"],
                    ["1", "50", "0", "500", "500", "Detected", "glide"],
                ],
            )

            payload = write_camera_motion_audit_report(output_dir, target_width=1000, target_height=500)

            self.assertEqual(100.0, payload["summary"]["max_pan_step_px"])
            self.assertEqual("camera_motion_spike", payload["review_events"][0]["type"])

    def test_non_contiguous_frames_divide_motion_by_frame_delta(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            self.write_camera_path(
                output_dir,
                [
                    ["0", "0", "0", "1000", "500", "Detected", "glide"],
                    ["2", "100", "0", "1000", "500", "Detected", "glide"],
                ],
            )

            payload = write_camera_motion_audit_report(output_dir, target_width=1000, target_height=500)

            self.assertEqual("ok", payload["summary"]["status"])
            self.assertEqual(50.0, payload["summary"]["max_pan_step_px"])
            self.assertEqual([], payload["review_events"])

    def test_non_contiguous_frames_use_frame_delta_for_acceleration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            self.write_camera_path(
                output_dir,
                [
                    ["0", "0", "0", "1000", "500", "Detected", "glide"],
                    ["2", "100", "0", "1000", "500", "Detected", "glide"],
                    ["4", "260", "0", "1000", "500", "Detected", "glide"],
                ],
            )

            payload = write_camera_motion_audit_report(output_dir, target_width=1000, target_height=500)

            self.assertEqual(30.0, payload["summary"]["max_pan_accel_px"])
            self.assertEqual([], payload["review_events"])

    def test_zoom_jump_reaches_fail_severity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            self.write_camera_path(
                output_dir,
                [
                    ["0", "100", "100", "960", "540", "Detected", "glide"],
                    ["1", "100", "100", "960", "590", "Detected", "glide"],
                ],
            )

            payload = write_camera_motion_audit_report(output_dir)

            self.assertEqual("fail", payload["summary"]["status"])
            self.assertEqual("camera_zoom_jump", payload["review_events"][0]["type"])
            self.assertEqual("fail", payload["review_events"][0]["severity"])

    def test_same_type_events_with_one_frame_gap_are_merged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            self.write_camera_path(
                output_dir,
                [
                    ["0", "0", "0", "1000", "500", "Detected", "glide"],
                    ["1", "100", "0", "1000", "500", "Detected", "glide"],
                    ["3", "300", "0", "1000", "500", "Predicted", "catch_up"],
                ],
            )

            payload = write_camera_motion_audit_report(output_dir, target_width=1000, target_height=500)

            self.assertEqual(1, payload["summary"]["review_event_count"])
            event = payload["review_events"][0]
            self.assertEqual("camera_motion_spike", event["type"])
            self.assertEqual(1, event["start_frame"])
            self.assertEqual(3, event["end_frame"])
            self.assertEqual(["glide", "catch_up"], event["evidence"]["pan_modes"])
            self.assertEqual(["Detected", "Predicted"], event["evidence"]["statuses"])

    def read_report(self, output_dir: Path) -> dict[str, object]:
        with (output_dir / "camera_motion_audit.json").open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def write_camera_path(self, output_dir: Path, rows: list[list[str]]) -> None:
        self.write_rows(
            output_dir,
            ["Frame", "CenterX", "CenterY", "CropWidth", "CropHeight", "Status", "PanMode"],
            rows,
        )

    def write_rows(self, output_dir: Path, headers: list[str], rows: list[list[str]]) -> None:
        with (output_dir / "camera_path.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerows(rows)


if __name__ == "__main__":
    unittest.main()
