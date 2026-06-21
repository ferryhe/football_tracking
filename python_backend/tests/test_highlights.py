from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import CancelledError
from pathlib import Path

import cv2
import numpy as np

from football_tracking.highlights import render_highlight_clip


class HighlightRenderTests(unittest.TestCase):
    def write_video(self, path: Path, *, frame_count: int = 5) -> None:
        writer = cv2.VideoWriter(
            str(path),
            cv2.VideoWriter.fourcc(*"mp4v"),
            6.0,
            (160, 90),
        )
        if not writer.isOpened():
            self.skipTest("OpenCV video writer is unavailable in this environment.")
        for frame_index in range(frame_count):
            frame = np.full((90, 160, 3), frame_index * 30, dtype=np.uint8)
            writer.write(frame)
        writer.release()

    def test_render_highlight_clip_writes_complete_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp_dir = Path(temp_name)
            input_video = temp_dir / "input.mp4"
            output_video = temp_dir / "highlight.mp4"
            self.write_video(input_video, frame_count=5)

            report = render_highlight_clip(
                input_video=input_video,
                output_path=output_video,
                start_frame=1,
                end_frame=3,
            )

            self.assertTrue(output_video.exists())
            self.assertEqual(3, report["frame_count"])
            self.assertEqual([160, 90], report["resolution"])
            self.assertFalse(any(path.name.endswith(".tmp.mp4") for path in temp_dir.iterdir()))

    def test_render_highlight_clip_rejects_out_of_bounds_window_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp_dir = Path(temp_name)
            input_video = temp_dir / "input.mp4"
            output_video = temp_dir / "highlight.mp4"
            self.write_video(input_video, frame_count=5)

            with self.assertRaises(ValueError):
                render_highlight_clip(
                    input_video=input_video,
                    output_path=output_video,
                    start_frame=0,
                    end_frame=12,
                )

            self.assertFalse(output_video.exists())
            self.assertFalse(any(path.name.endswith(".tmp.mp4") for path in temp_dir.iterdir()))

    def test_render_highlight_clip_cleans_partial_file_when_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp_dir = Path(temp_name)
            input_video = temp_dir / "input.mp4"
            output_video = temp_dir / "highlight.mp4"
            self.write_video(input_video, frame_count=5)
            cancel_checks = 0

            def should_cancel() -> bool:
                nonlocal cancel_checks
                cancel_checks += 1
                return cancel_checks > 1

            with self.assertRaises(CancelledError):
                render_highlight_clip(
                    input_video=input_video,
                    output_path=output_video,
                    start_frame=0,
                    end_frame=3,
                    should_cancel=should_cancel,
                )

            self.assertFalse(output_video.exists())
            self.assertFalse(any(path.name.endswith(".tmp.mp4") for path in temp_dir.iterdir()))


if __name__ == "__main__":
    unittest.main()
