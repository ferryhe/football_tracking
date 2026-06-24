from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from football_tracking.media_integrity import inspect_image, transcode_review_source


class MediaIntegrityTests(unittest.TestCase):
    def test_gray_low_variance_image_is_low_information(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            image_path = Path(temp) / "gray.jpg"
            image = np.full((80, 120, 3), 128, dtype=np.uint8)
            self.assertTrue(cv2.imwrite(str(image_path), image))

            result = inspect_image(image_path)

        self.assertEqual(str(image_path), result["path"])
        self.assertFalse(result["likely_corrupt"])
        self.assertTrue(result["gray"])
        self.assertTrue(result["low_information"])
        self.assertIn("low_variance", result["reasons"])

    def test_high_texture_image_passes_integrity_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            image_path = Path(temp) / "texture.png"
            rng = np.random.default_rng(7)
            image = rng.integers(0, 256, size=(96, 128, 3), dtype=np.uint8)
            self.assertTrue(cv2.imwrite(str(image_path), image))

            result = inspect_image(image_path)

        self.assertFalse(result["likely_corrupt"])
        self.assertFalse(result["gray"])
        self.assertFalse(result["low_information"])
        self.assertGreater(result["texture_tile_ratio"], 0.3)

    def test_flat_non_gray_sheet_with_labels_is_low_information(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            image_path = Path(temp) / "green_sheet.png"
            image = np.full((180, 240, 3), (30, 180, 40), dtype=np.uint8)
            cv2.putText(image, "frame 100", (16, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
            self.assertTrue(cv2.imwrite(str(image_path), image))

            result = inspect_image(image_path)

        self.assertFalse(result["gray"])
        self.assertTrue(result["low_information"])
        self.assertIn("dominant_flat_color", result["reasons"])

    def test_transcode_review_source_writes_sequential_mp4v_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            input_path = Path(temp) / "input.mp4"
            output_path = Path(temp) / "input.review_source.mp4"
            _write_tiny_video(input_path, width=48, height=32, frame_count=4)

            result = transcode_review_source(input_path, output_path)

            self.assertEqual("ok", result["status"])
            self.assertEqual(str(input_path.resolve()), result["input_video"])
            self.assertEqual(str(output_path.resolve()), result["review_source_video"])
            self.assertEqual("mp4v", result["codec"])
            self.assertEqual(4, result["frames_written"])
            self.assertTrue(output_path.exists())
            self.assertGreater(output_path.stat().st_size, 0)

    def test_transcode_review_source_rejects_overwriting_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            input_path = Path(temp) / "input.mp4"
            _write_tiny_video(input_path, width=48, height=32, frame_count=4)
            original_size = input_path.stat().st_size

            result = transcode_review_source(input_path, input_path)

            self.assertEqual("error", result["status"])
            self.assertIn("must differ", result["reason"])
            self.assertEqual(original_size, input_path.stat().st_size)

    def test_transcode_review_source_cli_reports_missing_input_as_json(self) -> None:
        from scripts.transcode_review_source import main

        with tempfile.TemporaryDirectory() as temp:
            missing_input = Path(temp) / "missing.mp4"
            output_path = Path(temp) / "review.mp4"
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["--input-video", str(missing_input), "--output-video", str(output_path)])

            payload = json.loads(stdout.getvalue())

        self.assertEqual(1, exit_code)
        self.assertEqual("unavailable", payload["status"])
        self.assertIn("input video could not be opened", payload["reason"])


def _write_tiny_video(path: Path, *, width: int, height: int, frame_count: int) -> None:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"mp4v"), 8.0, (width, height))
    if not writer.isOpened():
        raise RuntimeError("OpenCV VideoWriter could not create a tiny test video.")
    for index in range(frame_count):
        frame = np.full((height, width, 3), (index * 40, 20, 120), dtype=np.uint8)
        writer.write(frame)
    writer.release()


if __name__ == "__main__":
    unittest.main()
