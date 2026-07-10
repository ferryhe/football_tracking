from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from football_tracking.camera_motion_audit import (
    write_camera_motion_audit_report,
    write_streaming_camera_motion_audit_report,
)


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
            self.assertEqual(0, payload["summary"]["cut_count"])
            self.assertEqual(1, payload["summary"]["continuous_segment_count"])
            self.assertEqual(0, payload["summary"]["low_confidence_motion_frame_count"])
            self.assertEqual([], payload["review_events"])

    def test_cut_row_excludes_cross_boundary_motion_and_resets_velocity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            self.write_camera_path_with_motion_evidence(
                output_dir,
                [
                    ["0", "0", "0", "960", "540", "Detected", "glide", "shot-1", "false", "0.9"],
                    ["1", "40", "0", "960", "540", "Detected", "glide", "shot-1", "false", "0.9"],
                    ["2", "1000", "500", "480", "270", "Unknown", "cut_reset", "shot-2", "true", "0.2"],
                    ["3", "1010", "500", "480", "270", "Detected", "glide", "shot-2", "false", "0.4"],
                ],
            )

            payload = write_camera_motion_audit_report(output_dir)

            self.assertEqual("ok", payload["summary"]["status"])
            self.assertEqual(1, payload["summary"]["cut_count"])
            self.assertEqual(2, payload["summary"]["continuous_segment_count"])
            self.assertEqual(2, payload["summary"]["low_confidence_motion_frame_count"])
            self.assertEqual(0.0, payload["summary"]["max_pan_accel_px"])
            self.assertEqual(0.0, payload["summary"]["max_zoom_step_px"])
            self.assertEqual([], payload["review_events"])

    def test_shot_id_change_excludes_unmarked_cut_jump(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            self.write_camera_path_with_motion_evidence(
                output_dir,
                [
                    ["0", "0", "0", "960", "540", "Detected", "glide", "shot-1", "false", ""],
                    ["1", "900", "500", "480", "270", "Detected", "glide", "shot-2", "false", ""],
                ],
            )

            payload = write_camera_motion_audit_report(output_dir)

            self.assertEqual("ok", payload["summary"]["status"])
            self.assertEqual(1, payload["summary"]["cut_count"])
            self.assertEqual(2, payload["summary"]["continuous_segment_count"])
            self.assertEqual(0.0, payload["summary"]["max_pan_step_px"])
            self.assertEqual([], payload["review_events"])

    def test_review_events_are_not_merged_across_cut_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            self.write_camera_path_with_motion_evidence(
                output_dir,
                [
                    ["0", "0", "0", "1000", "500", "Detected", "glide", "shot-1", "false", "1"],
                    ["1", "100", "0", "1000", "500", "Detected", "glide", "shot-1", "false", "1"],
                    ["2", "500", "0", "1000", "500", "Detected", "cut_reset", "shot-2", "true", "1"],
                    ["3", "600", "0", "1000", "500", "Detected", "glide", "shot-2", "false", "1"],
                ],
            )

            payload = write_camera_motion_audit_report(output_dir, target_width=1000, target_height=500)

            self.assertEqual(2, payload["summary"]["review_event_count"])
            self.assertEqual([1, 3], [event["start_frame"] for event in payload["review_events"]])

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

    def test_streaming_audit_matches_legacy_metrics_and_events_across_cut_segments(self) -> None:
        rows = [
            ["0", "0", "0", "1000", "500", "Detected", "glide", "0", "0", "0.9"],
            ["1", "100", "0", "1000", "500", "Detected", "glide", "0", "0", "0.9"],
            ["2", "300", "0", "1000", "550", "Predicted", "catch_up", "0", "0", "0.4"],
            ["3", "800", "0", "1000", "550", "unknown", "scene_cut", "1", "1", ""],
            ["4", "810", "0", "1000", "550", "Detected", "glide", "1", "0", "0.8"],
        ]
        with tempfile.TemporaryDirectory() as first_name, tempfile.TemporaryDirectory() as second_name:
            first = Path(first_name)
            second = Path(second_name)
            self.write_camera_path_with_motion_evidence(first, rows)
            self.write_camera_path_with_motion_evidence(second, rows)
            legacy = write_camera_motion_audit_report(first, target_width=1000, target_height=500)
            streamed = write_streaming_camera_motion_audit_report(
                second,
                target_width=1000,
                target_height=500,
                generated_at=None,
            )
            streamed_file = self.read_report(second)

        self.assertEqual(legacy["summary"], streamed["summary"])
        self.assertEqual(legacy["summary"], streamed_file["summary"])
        self.assertEqual(legacy["review_events"], streamed_file["review_events"])
        self.assertIsNone(streamed_file["generated_at"])

    def test_streaming_audit_handles_one_hundred_thousand_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            with (output_dir / "camera_path.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["Frame", "CenterX", "CenterY", "CropWidth", "CropHeight", "Status", "PanMode"])
                for frame in range(100_000):
                    writer.writerow([frame, "500", "250", "1000", "500", "unknown", "hold"])

            payload = write_streaming_camera_motion_audit_report(
                output_dir,
                target_width=1000,
                target_height=500,
                generated_at=None,
            )

        self.assertEqual(100_000, payload["summary"]["frame_count"])
        self.assertEqual(0.0, payload["summary"]["p95_pan_step_px"])
        self.assertEqual(0, payload["summary"]["review_event_count"])

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

    def write_camera_path_with_motion_evidence(self, output_dir: Path, rows: list[list[str]]) -> None:
        self.write_rows(
            output_dir,
            [
                "Frame",
                "CenterX",
                "CenterY",
                "CropWidth",
                "CropHeight",
                "Status",
                "PanMode",
                "ShotId",
                "CutDetected",
                "MotionConfidence",
            ],
            rows,
        )


if __name__ == "__main__":
    unittest.main()
