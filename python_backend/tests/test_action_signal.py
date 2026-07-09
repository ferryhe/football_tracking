from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from football_tracking.action_signal import (
    ACTION_DIRECTOR_SOURCE_SHA256,
    ACTION_SIGNAL_DIAGNOSTICS_NAME,
    ACTION_SIGNAL_REPORT_NAME,
    ACTION_SIGNAL_SCHEMA_VERSION,
    ACTION_TRACK_NAME,
    ActionCalibration,
    ActionMeasurement,
    ActionSignalProcessor,
    ActionSignalSettings,
    ActionSignalTracker,
    build_action_mask,
    generate_action_track,
    measure_action,
    validate_calibration_for_video,
)
from scripts.generate_action_signal import main as action_signal_main


class ActionCalibrationTests(unittest.TestCase):
    def make_calibration(self) -> ActionCalibration:
        return ActionCalibration.from_dict(
            {
                "schema_version": "1.0",
                "source_resolution": [100, 50],
                "confirmed_sample_frames": [0, 50, 100],
                "field_polygon": [[10, 5], [90, 5], [90, 45], [10, 45]],
                "exclusion_polygons": [
                    [[40, 15], [60, 15], [60, 35], [40, 35]],
                ],
            }
        )

    def test_contract_requires_three_distinct_confirmed_frames(self) -> None:
        payload = {
            "schema_version": "1.0",
            "source_resolution": [100, 50],
            "confirmed_sample_frames": [0, 10],
            "field_polygon": [[0, 0], [100, 0], [100, 50], [0, 50]],
            "exclusion_polygons": [],
        }

        with self.assertRaisesRegex(ValueError, "exactly three"):
            ActionCalibration.from_dict(payload)

        payload["confirmed_sample_frames"] = [0, 10, 10]
        with self.assertRaisesRegex(ValueError, "distinct"):
            ActionCalibration.from_dict(payload)

        payload["confirmed_sample_frames"] = [10, 0, 20]
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            ActionCalibration.from_dict(payload)

    def test_mask_is_derived_only_from_scaled_field_and_exclusions(self) -> None:
        calibration = self.make_calibration()

        mask = build_action_mask(calibration, width=50, height=25)

        self.assertEqual((25, 50), mask.shape)
        self.assertEqual(0, int(mask[1, 1]))
        self.assertEqual(255, int(mask[5, 8]))
        self.assertEqual(0, int(mask[12, 25]))
        self.assertEqual(0, int(mask[24, 49]))

    def test_mask_scales_to_a_different_resolution(self) -> None:
        calibration = self.make_calibration()

        native = build_action_mask(calibration, width=100, height=50)
        doubled = build_action_mask(calibration, width=200, height=100)

        self.assertEqual(int(native[10, 20]), int(doubled[20, 40]))
        self.assertEqual(int(native[25, 50]), int(doubled[50, 100]))
        self.assertEqual(int(native[2, 2]), int(doubled[4, 4]))

    def test_video_validation_accepts_scaled_aspect_and_rejects_incompatible_video(self) -> None:
        calibration = ActionCalibration.from_dict(
            {
                "schema_version": "1.0",
                "source_resolution": [5120, 1440],
                "confirmed_sample_frames": [0, 50, 100],
                "field_polygon": [[0, 0], [5120, 0], [5120, 1440], [0, 1440]],
                "exclusion_polygons": [],
            }
        )

        validate_calibration_for_video(
            calibration,
            source_width=7680,
            source_height=2160,
            total_source_frames=101,
        )

        with self.assertRaisesRegex(ValueError, "aspect ratio"):
            validate_calibration_for_video(
                calibration,
                source_width=1920,
                source_height=1080,
                total_source_frames=101,
            )

    def test_video_validation_requires_confirmed_frames_inside_known_source(self) -> None:
        calibration = self.make_calibration()

        with self.assertRaisesRegex(ValueError, "outside video frame range"):
            validate_calibration_for_video(
                calibration,
                source_width=200,
                source_height=100,
                total_source_frames=100,
            )


class ActionSignalCoreTests(unittest.TestCase):
    def test_settings_reject_non_finite_numeric_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "variance_threshold"):
            ActionSignalSettings(variance_threshold=float("nan"))
        with self.assertRaisesRegex(ValueError, "hold_frames"):
            ActionSignalTracker(smoothing=0.5, hold_frames=float("nan"), hold_confidence_decay=0.5)  # type: ignore[arg-type]

    def test_measurement_can_reach_frame_boundary_without_a_hidden_clamp(self) -> None:
        binary = np.zeros((20, 40), dtype=np.uint8)
        binary[8:12, 35:40] = 255

        measurement = measure_action(
            binary,
            source_width=400,
            source_height=200,
            min_component_area=1,
            max_component_area=100,
        )

        self.assertIsNotNone(measurement)
        assert measurement is not None
        self.assertGreater(measurement.center_x, 350.0)
        self.assertLessEqual(measurement.center_x, 400.0)
        self.assertGreater(measurement.range_width, 0.0)
        self.assertEqual(1, measurement.component_count)

    def test_tracker_uses_a_bounded_hold_then_reports_unknown(self) -> None:
        tracker = ActionSignalTracker(smoothing=0.5, hold_frames=2, hold_confidence_decay=0.5)
        measurement = ActionMeasurement(
            center_x=10.0,
            center_y=20.0,
            range_width=8.0,
            range_height=6.0,
            confidence=0.8,
            component_count=2,
            motion_area=20,
            motion_fraction=0.1,
        )

        detected = tracker.update(0, measurement)
        held_once = tracker.update(1, None)
        held_twice = tracker.update(2, None)
        unknown = tracker.update(3, None)
        reacquired = tracker.update(
            4,
            ActionMeasurement(
                center_x=90.0,
                center_y=40.0,
                range_width=4.0,
                range_height=4.0,
                confidence=0.9,
                component_count=1,
                motion_area=8,
                motion_fraction=0.04,
            ),
        )

        self.assertEqual("detected", detected.status)
        self.assertEqual("held", held_once.status)
        self.assertEqual("held", held_twice.status)
        self.assertAlmostEqual(0.4, held_once.confidence)
        self.assertAlmostEqual(0.2, held_twice.confidence)
        self.assertEqual("unknown", unknown.status)
        self.assertIsNone(unknown.x)
        self.assertIsNone(unknown.range_width)
        self.assertEqual("detected", reacquired.status)
        self.assertEqual(90.0, reacquired.x)

    def test_processor_handles_empty_and_static_frames_as_unknown(self) -> None:
        calibration = ActionCalibration.from_dict(
            {
                "schema_version": "1.0",
                "source_resolution": [64, 32],
                "confirmed_sample_frames": [0, 1, 2],
                "field_polygon": [[0, 0], [64, 0], [64, 32], [0, 32]],
                "exclusion_polygons": [],
            }
        )
        processor = ActionSignalProcessor(
            calibration=calibration,
            source_width=64,
            source_height=32,
            settings=ActionSignalSettings(
                process_width=64,
                warmup_frames=1,
                min_component_area=1,
                max_component_area=4096,
                hold_frames=0,
            ),
        )
        static = np.zeros((32, 64, 3), dtype=np.uint8)

        empty_result, empty_diagnostic = processor.process_frame(None, frame_index=0)
        first_static, _ = processor.process_frame(static, frame_index=1)
        second_static, _ = processor.process_frame(static.copy(), frame_index=2)

        self.assertEqual("unknown", empty_result.status)
        self.assertEqual("empty_frame", empty_diagnostic["reason"])
        self.assertEqual("unknown", first_static.status)
        self.assertEqual("unknown", second_static.status)

    def test_processor_detects_motion_after_background_warmup(self) -> None:
        calibration = ActionCalibration.from_dict(
            {
                "schema_version": "1.0",
                "source_resolution": [64, 32],
                "confirmed_sample_frames": [0, 1, 2],
                "field_polygon": [[0, 0], [64, 0], [64, 32], [0, 32]],
                "exclusion_polygons": [],
            }
        )
        processor = ActionSignalProcessor(
            calibration=calibration,
            source_width=64,
            source_height=32,
            settings=ActionSignalSettings(
                process_width=64,
                warmup_frames=2,
                min_component_area=2,
                max_component_area=500,
                hold_frames=0,
            ),
        )
        background = np.zeros((32, 64, 3), dtype=np.uint8)
        moving = background.copy()
        moving[10:18, 44:52] = 255

        processor.process_frame(background, frame_index=0)
        processor.process_frame(background.copy(), frame_index=1)
        result, diagnostic = processor.process_frame(moving, frame_index=2)

        self.assertEqual("detected", result.status)
        self.assertGreater(result.x or 0.0, 40.0)
        self.assertEqual("foreground_motion", diagnostic["reason"])

    def test_processor_keeps_dilated_motion_inside_field_and_outside_exclusions(self) -> None:
        calibration = ActionCalibration.from_dict(
            {
                "schema_version": "1.0",
                "source_resolution": [64, 32],
                "confirmed_sample_frames": [0, 1, 2],
                "field_polygon": [[8, 4], [56, 4], [56, 28], [8, 28]],
                "exclusion_polygons": [
                    [[28, 8], [36, 8], [36, 24], [28, 24]],
                ],
            }
        )
        processor = ActionSignalProcessor(
            calibration=calibration,
            source_width=64,
            source_height=32,
            settings=ActionSignalSettings(
                process_width=64,
                warmup_frames=0,
                min_component_area=1,
                max_component_area=500,
                hold_frames=0,
            ),
        )
        foreground = np.zeros((32, 64), dtype=np.uint8)
        foreground[10:16, 8:14] = 255
        foreground[18:24, 23:29] = 255
        processor._subtractor = _FixedForegroundSubtractor(foreground)
        captured_binary: list[np.ndarray] = []

        def capture_measurement(binary: np.ndarray, **kwargs: object) -> ActionMeasurement | None:
            captured_binary.append(binary.copy())
            return measure_action(binary, **kwargs)

        with patch("football_tracking.action_signal.measure_action", side_effect=capture_measurement):
            result, _ = processor.process_frame(np.zeros((32, 64, 3), dtype=np.uint8), frame_index=0)

        self.assertEqual("detected", result.status)
        self.assertEqual(1, len(captured_binary))
        final_binary = captured_binary[0]
        self.assertFalse(np.any(final_binary[processor.field_mask == 0]))
        self.assertEqual(0, int(final_binary[12, 7]))
        self.assertEqual(0, int(final_binary[20, 28]))


class ActionSignalCliTests(unittest.TestCase):
    def test_cli_writes_track_and_versioned_diagnostics(self) -> None:
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            video_path = root / "input.avi"
            calibration_path = root / "calibration.json"
            output_dir = root / "output"
            _write_test_video(video_path)
            calibration_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "source_resolution": [64, 32],
                        "confirmed_sample_frames": [0, 2, 4],
                        "field_polygon": [[0, 0], [64, 0], [64, 32], [0, 32]],
                        "exclusion_polygons": [],
                    }
                ),
                encoding="utf-8",
            )

            with redirect_stdout(stdout):
                exit_code = action_signal_main(
                    [
                        "--input-video",
                        str(video_path),
                        "--calibration",
                        str(calibration_path),
                        "--output-dir",
                        str(output_dir),
                        "--process-width",
                        "64",
                        "--warmup-frames",
                        "2",
                        "--min-area",
                        "2",
                        "--max-area",
                        "500",
                        "--hold-frames",
                        "1",
                    ]
                )

            with (output_dir / ACTION_TRACK_NAME).open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            report = json.loads((output_dir / ACTION_SIGNAL_REPORT_NAME).read_text(encoding="utf-8"))
            diagnostics = [
                json.loads(line)
                for line in (output_dir / ACTION_SIGNAL_DIAGNOSTICS_NAME).read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(0, exit_code)
        self.assertEqual(5, len(rows))
        self.assertEqual({"detected", "held", "unknown"}, {row["Status"] for row in rows})
        self.assertEqual(ACTION_SIGNAL_SCHEMA_VERSION, report["schema_version"])
        self.assertEqual(ACTION_DIRECTOR_SOURCE_SHA256, report["provenance"]["source_sha256"])
        self.assertEqual(ACTION_TRACK_NAME, report["artifacts"]["track"])
        self.assertEqual(5, len(diagnostics))
        self.assertTrue(all(item["schema_version"] == ACTION_SIGNAL_SCHEMA_VERSION for item in diagnostics))
        output_events = [json.loads(line) for line in stdout.getvalue().splitlines()]
        completed_events = [event for event in output_events if event["event"] == "completed"]
        self.assertEqual(1, len(completed_events))
        self.assertEqual("complete", completed_events[0]["status"])
        self.assertEqual(str(output_dir / ACTION_SIGNAL_REPORT_NAME), completed_events[0]["report"])

    def test_cli_reports_errors_without_a_traceback(self) -> None:
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_name, redirect_stderr(stderr):
            root = Path(temp_name)
            exit_code = action_signal_main(
                [
                    "--input-video",
                    str(root / "missing.avi"),
                    "--calibration",
                    str(root / "missing.json"),
                    "--output-dir",
                    str(root / "output"),
                ]
            )

        self.assertEqual(1, exit_code)
        error = json.loads(stderr.getvalue())
        self.assertEqual("failed", error["status"])
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_cli_returns_nonzero_when_generation_report_is_truncated(self) -> None:
        stderr = io.StringIO()
        stdout = io.StringIO()
        truncated_report = {
            "status": "truncated",
            "termination_reason": "premature_read_failure",
        }
        with (
            tempfile.TemporaryDirectory() as temp_name,
            patch("scripts.generate_action_signal.load_action_calibration", return_value=object()),
            patch("scripts.generate_action_signal.generate_action_track", return_value=truncated_report),
            redirect_stderr(stderr),
            redirect_stdout(stdout),
        ):
            root = Path(temp_name)
            exit_code = action_signal_main(
                [
                    "--input-video",
                    str(root / "input.avi"),
                    "--calibration",
                    str(root / "calibration.json"),
                    "--output-dir",
                    str(root / "output"),
                ]
            )

        error = json.loads(stderr.getvalue())
        self.assertEqual(1, exit_code)
        self.assertEqual("truncated", error["status"])
        self.assertEqual("premature_read_failure", error["termination_reason"])
        self.assertNotIn("report=", stdout.getvalue())


class ActionSignalGenerationTests(unittest.TestCase):
    def make_calibration(self) -> ActionCalibration:
        return ActionCalibration.from_dict(
            {
                "schema_version": "1.0",
                "source_resolution": [64, 32],
                "confirmed_sample_frames": [0, 2, 4],
                "field_polygon": [[0, 0], [64, 0], [64, 32], [0, 32]],
                "exclusion_polygons": [],
            }
        )

    def test_failed_direct_seek_falls_back_to_exact_sequential_skip(self) -> None:
        frames = [np.zeros((32, 64, 3), dtype=np.uint8) for _ in range(5)]
        direct_capture = _FakeCapture(frames, seek_succeeds=False)
        fallback_capture = _FakeCapture(frames)
        progress: list[dict[str, object]] = []
        with (
            tempfile.TemporaryDirectory() as temp_name,
            patch(
                "football_tracking.action_signal.cv2.VideoCapture",
                side_effect=[direct_capture, fallback_capture],
            ),
        ):
            output_dir = Path(temp_name)
            report = generate_action_track(
                input_video=output_dir / "input.avi",
                calibration=self.make_calibration(),
                output_dir=output_dir,
                start_frame=2,
                max_frames=2,
                settings=ActionSignalSettings(process_width=64, warmup_frames=0),
                progress_callback=progress.append,
                progress_interval_frames=1,
            )
            with (output_dir / ACTION_TRACK_NAME).open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertTrue(direct_capture.released)
        self.assertTrue(fallback_capture.released)
        self.assertEqual(["2", "3"], [row["Frame"] for row in rows])
        self.assertEqual("bounded_complete", report["status"])
        self.assertEqual("max_frames_reached", report["termination_reason"])
        self.assertEqual(2, report["expected_frame_count"])
        self.assertEqual("sequential_fallback", report["seek_mode"])
        self.assertEqual("started", progress[0]["event"])
        self.assertEqual("completed", progress[-1]["event"])

    def test_start_frame_equal_to_known_source_count_is_rejected(self) -> None:
        capture = _FakeCapture([np.zeros((32, 64, 3), dtype=np.uint8) for _ in range(5)])
        with (
            tempfile.TemporaryDirectory() as temp_name,
            patch(
                "football_tracking.action_signal.cv2.VideoCapture",
                return_value=capture,
            ),
        ):
            output_dir = Path(temp_name)
            with self.assertRaisesRegex(ValueError, "start_frame.*source frame count"):
                generate_action_track(
                    input_video=output_dir / "input.avi",
                    calibration=self.make_calibration(),
                    output_dir=output_dir,
                    start_frame=5,
                    settings=ActionSignalSettings(process_width=64, warmup_frames=0),
                )

            self.assertFalse((output_dir / ACTION_SIGNAL_REPORT_NAME).exists())

        self.assertTrue(capture.released)

    def test_premature_read_is_published_as_truncated_not_complete(self) -> None:
        frames = [np.zeros((32, 64, 3), dtype=np.uint8) for _ in range(3)]
        capture = _FakeCapture(frames, reported_frame_count=5)
        with (
            tempfile.TemporaryDirectory() as temp_name,
            patch(
                "football_tracking.action_signal.cv2.VideoCapture",
                return_value=capture,
            ),
        ):
            output_dir = Path(temp_name)
            report = generate_action_track(
                input_video=output_dir / "input.avi",
                calibration=self.make_calibration(),
                output_dir=output_dir,
                settings=ActionSignalSettings(process_width=64, warmup_frames=0),
            )
            written = json.loads((output_dir / ACTION_SIGNAL_REPORT_NAME).read_text(encoding="utf-8"))

        self.assertEqual("truncated", report["status"])
        self.assertEqual("premature_read_failure", report["termination_reason"])
        self.assertEqual(5, report["expected_frame_count"])
        self.assertEqual(3, report["frame_count"])
        self.assertEqual(report["status"], written["status"])
        self.assertTrue(capture.released)

    def test_processing_failure_preserves_existing_artifact_set_and_cleans_temps(self) -> None:
        capture = _FakeCapture([np.zeros((32, 64, 3), dtype=np.uint8) for _ in range(5)])
        final_names = (ACTION_TRACK_NAME, ACTION_SIGNAL_DIAGNOSTICS_NAME, ACTION_SIGNAL_REPORT_NAME)
        progress: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            for name in final_names:
                (output_dir / name).write_text(f"sentinel:{name}", encoding="utf-8")
            with (
                patch("football_tracking.action_signal.cv2.VideoCapture", return_value=capture),
                patch.object(ActionSignalProcessor, "process_frame", side_effect=RuntimeError("injected failure")),
                self.assertRaisesRegex(RuntimeError, "injected failure"),
            ):
                generate_action_track(
                    input_video=output_dir / "input.avi",
                    calibration=self.make_calibration(),
                    output_dir=output_dir,
                    settings=ActionSignalSettings(process_width=64, warmup_frames=0),
                    progress_callback=progress.append,
                )

            contents = {name: (output_dir / name).read_text(encoding="utf-8") for name in final_names}
            temporary_files = [path.name for path in output_dir.iterdir() if path.name.startswith(".")]

        self.assertEqual({name: f"sentinel:{name}" for name in final_names}, contents)
        self.assertEqual([], temporary_files)
        self.assertTrue(capture.released)
        self.assertEqual("failed", progress[-1]["status"])

    def test_non_finite_source_fps_fails_before_publishing(self) -> None:
        capture = _FakeCapture(
            [np.zeros((32, 64, 3), dtype=np.uint8) for _ in range(5)],
            fps=float("nan"),
        )
        with (
            tempfile.TemporaryDirectory() as temp_name,
            patch(
                "football_tracking.action_signal.cv2.VideoCapture",
                return_value=capture,
            ),
        ):
            output_dir = Path(temp_name)
            with self.assertRaisesRegex(RuntimeError, "FPS"):
                generate_action_track(
                    input_video=output_dir / "input.avi",
                    calibration=self.make_calibration(),
                    output_dir=output_dir,
                )

            self.assertFalse((output_dir / ACTION_SIGNAL_REPORT_NAME).exists())

        self.assertTrue(capture.released)

    def test_unknown_source_count_with_no_decoded_frames_is_failed_not_complete(self) -> None:
        capture = _FakeCapture([], reported_frame_count=0)
        with (
            tempfile.TemporaryDirectory() as temp_name,
            patch(
                "football_tracking.action_signal.cv2.VideoCapture",
                return_value=capture,
            ),
        ):
            output_dir = Path(temp_name)
            report = generate_action_track(
                input_video=output_dir / "input.avi",
                calibration=self.make_calibration(),
                output_dir=output_dir,
                settings=ActionSignalSettings(process_width=64, warmup_frames=0),
            )
            written = json.loads((output_dir / ACTION_SIGNAL_REPORT_NAME).read_text(encoding="utf-8"))

        self.assertEqual("failed", report["status"])
        self.assertEqual("no_decodable_frames", report["termination_reason"])
        self.assertEqual(0, report["frame_count"])
        self.assertEqual("failed", written["status"])
        self.assertTrue(capture.released)

    def test_backup_cleanup_failure_does_not_reverse_successful_publish(self) -> None:
        capture = _FakeCapture([np.zeros((32, 64, 3), dtype=np.uint8) for _ in range(5)])
        final_names = (ACTION_TRACK_NAME, ACTION_SIGNAL_DIAGNOSTICS_NAME, ACTION_SIGNAL_REPORT_NAME)
        original_unlink = Path.unlink

        def fail_backup_cleanup(path: Path, missing_ok: bool = False) -> None:
            if path.suffix == ".bak":
                raise PermissionError("injected backup cleanup failure")
            original_unlink(path, missing_ok=missing_ok)

        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            for name in final_names:
                (output_dir / name).write_text(f"sentinel:{name}", encoding="utf-8")
            with (
                patch("football_tracking.action_signal.cv2.VideoCapture", return_value=capture),
                patch.object(Path, "unlink", new=fail_backup_cleanup),
            ):
                report = generate_action_track(
                    input_video=output_dir / "input.avi",
                    calibration=self.make_calibration(),
                    output_dir=output_dir,
                    settings=ActionSignalSettings(process_width=64, warmup_frames=0),
                )
            written = json.loads((output_dir / ACTION_SIGNAL_REPORT_NAME).read_text(encoding="utf-8"))
            track_text = (output_dir / ACTION_TRACK_NAME).read_text(encoding="utf-8-sig")

        self.assertEqual("complete", report["status"])
        self.assertEqual("complete", written["status"])
        self.assertTrue(track_text.startswith("Frame,"))
        self.assertTrue(capture.released)


def _write_test_video(path: Path) -> None:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10.0, (64, 32))
    if not writer.isOpened():
        raise RuntimeError("OpenCV VideoWriter could not create the action-signal test video")
    background = np.zeros((32, 64, 3), dtype=np.uint8)
    moving = background.copy()
    moving[10:18, 44:52] = 255
    for frame in (background, background.copy(), moving, moving.copy(), background.copy()):
        writer.write(frame)
    writer.release()


class _FixedForegroundSubtractor:
    def __init__(self, foreground: np.ndarray) -> None:
        self.foreground = foreground

    def apply(self, frame: np.ndarray) -> np.ndarray:
        return self.foreground.copy()


class _FakeCapture:
    def __init__(
        self,
        frames: list[np.ndarray],
        *,
        seek_succeeds: bool = True,
        reported_frame_count: int | None = None,
        fps: float = 20.0,
    ) -> None:
        self.frames = frames
        self.seek_succeeds = seek_succeeds
        self.reported_frame_count = len(frames) if reported_frame_count is None else reported_frame_count
        self.fps = fps
        self.position = 0
        self.released = False

    def isOpened(self) -> bool:
        return True

    def get(self, prop: int) -> float:
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return 64.0
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return 32.0
        if prop == cv2.CAP_PROP_FPS:
            return self.fps
        if prop == cv2.CAP_PROP_FRAME_COUNT:
            return float(self.reported_frame_count)
        if prop == cv2.CAP_PROP_POS_FRAMES:
            return float(self.position)
        return 0.0

    def set(self, prop: int, value: float) -> bool:
        if prop != cv2.CAP_PROP_POS_FRAMES or not self.seek_succeeds:
            return False
        self.position = int(value)
        return True

    def read(self) -> tuple[bool, np.ndarray | None]:
        if self.position >= len(self.frames):
            return False, None
        frame = self.frames[self.position]
        self.position += 1
        return True, frame.copy()

    def release(self) -> None:
        self.released = True


if __name__ == "__main__":
    unittest.main()
