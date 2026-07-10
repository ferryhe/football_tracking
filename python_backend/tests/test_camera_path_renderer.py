from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

from football_tracking.camera_path_renderer import (
    CAMERA_PATH_NAME,
    REPORT_NAME,
    CameraPathRenderError,
    _acquire_render_output_lock,
    _release_render_output_lock,
    _validate_rendered_video,
    render_camera_path_video,
)


class DummyCapture:
    def __init__(
        self,
        frames: list[np.ndarray],
        *,
        width: int = 32,
        height: int = 18,
        fps: float = 10.0,
        frame_count: int | None = None,
        opened: bool = True,
    ) -> None:
        self.frames = list(frames)
        self.width = width
        self.height = height
        self.fps = fps
        self.frame_count = len(frames) if frame_count is None else frame_count
        self.opened = opened
        self.released = False

    def isOpened(self) -> bool:
        return self.opened

    def get(self, property_id: int) -> float:
        if property_id == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self.width)
        if property_id == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self.height)
        if property_id == cv2.CAP_PROP_FPS:
            return self.fps
        if property_id == cv2.CAP_PROP_FRAME_COUNT:
            return float(self.frame_count)
        return 0.0

    def read(self) -> tuple[bool, np.ndarray | None]:
        if not self.frames:
            return False, None
        return True, self.frames.pop(0).copy()

    def release(self) -> None:
        self.released = True


class DummyWriter:
    def __init__(self, *, opened: bool = True) -> None:
        self.opened = opened
        self.frames: list[np.ndarray] = []
        self.released = False

    def isOpened(self) -> bool:
        return self.opened

    def write(self, frame: np.ndarray) -> None:
        self.frames.append(frame.copy())

    def release(self) -> None:
        self.released = True


class CameraPathRendererTests(unittest.TestCase):
    def test_renders_exact_crops_and_preserves_unknown_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            rows = self._default_rows()
            source, path, report = self._write_inputs(root, rows)
            frames = []
            for index in range(3):
                frame = np.zeros((18, 32, 3), dtype=np.uint8)
                frame[:, :16] = 10 + index
                frame[:, 16:] = 200 + index
                frames.append(frame)
            capture = DummyCapture(frames)
            writer = DummyWriter()

            result = render_camera_path_video(
                source,
                path,
                report,
                target_width=16,
                target_height=9,
                capture=capture,
                writer=writer,
            )

        self.assertEqual(3, result.frame_count)
        self.assertEqual({"ball_guided": 1, "broadcast_guided": 1, "unknown": 1}, result.status_counts)
        self.assertIsNone(result.output_video_path)
        self.assertEqual(3, len(writer.frames))
        self.assertTrue(np.all(writer.frames[0] == 10))
        self.assertTrue(np.all(writer.frames[1] == 201))
        self.assertEqual((9, 16, 3), writer.frames[2].shape)
        self.assertTrue(capture.released)
        self.assertTrue(writer.released)

    def test_rejects_report_that_is_not_both_complete_and_succeeded(self) -> None:
        for report_change in ({"complete": False}, {"status": "failed"}):
            with self.subTest(report_change=report_change), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                source, path, report = self._write_inputs(root, self._default_rows())
                payload = json.loads(report.read_text(encoding="utf-8"))
                payload.update(report_change)
                report.write_text(json.dumps(payload), encoding="utf-8")

                with self.assertRaisesRegex(CameraPathRenderError, "not complete and succeeded"):
                    render_camera_path_video(
                        source,
                        path,
                        report,
                        target_width=16,
                        target_height=9,
                        capture=DummyCapture(self._frames()),
                        writer=DummyWriter(),
                    )

    def test_rejects_camera_path_hash_or_size_mismatch(self) -> None:
        for artifact_change in ({"sha256": "0" * 64}, {"size": 1}):
            with self.subTest(artifact_change=artifact_change), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                source, path, report = self._write_inputs(root, self._default_rows())
                payload = json.loads(report.read_text(encoding="utf-8"))
                payload["artifacts"][CAMERA_PATH_NAME].update(artifact_change)
                report.write_text(json.dumps(payload), encoding="utf-8")

                with self.assertRaisesRegex(CameraPathRenderError, "does not match"):
                    render_camera_path_video(
                        source,
                        path,
                        report,
                        target_width=16,
                        target_height=9,
                        capture=DummyCapture(self._frames()),
                        writer=DummyWriter(),
                    )

    def test_rejects_source_hash_and_capture_metadata_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source, path, report = self._write_inputs(root, self._default_rows())
            payload = json.loads(report.read_text(encoding="utf-8"))
            payload["source_video"]["sha256"] = "0" * 64
            report.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(CameraPathRenderError, "source video sha256"):
                render_camera_path_video(
                    source,
                    path,
                    report,
                    target_width=16,
                    target_height=9,
                    capture=DummyCapture(self._frames()),
                    writer=DummyWriter(),
                )

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source, path, report = self._write_inputs(root, self._default_rows())
            capture = DummyCapture(self._frames(), width=31)
            writer = DummyWriter()
            with self.assertRaisesRegex(CameraPathRenderError, "metadata does not match"):
                render_camera_path_video(
                    source,
                    path,
                    report,
                    target_width=16,
                    target_height=9,
                    capture=capture,
                    writer=writer,
                )
            self.assertTrue(capture.released)
            self.assertTrue(writer.released)

    def test_rejects_invalid_crop_bounds_dimensions_aspect_or_center(self) -> None:
        changes = (
            ({"CropX2": "33", "CropWidth": "33", "CenterX": "16.5"}, "x bounds"),
            ({"CropWidth": "15"}, "dimensions are inconsistent"),
            ({"CropX2": "14", "CropWidth": "14", "CenterX": "7"}, "aspect ratio"),
            ({"CenterX": "9"}, "CenterX is inconsistent"),
        )
        for row_change, message in changes:
            with self.subTest(row_change=row_change), tempfile.TemporaryDirectory() as temp_name:
                rows = self._default_rows()
                rows[0].update(row_change)
                source, path, report = self._write_inputs(Path(temp_name), rows)
                with self.assertRaisesRegex(CameraPathRenderError, message):
                    render_camera_path_video(
                        source,
                        path,
                        report,
                        target_width=16,
                        target_height=9,
                        capture=DummyCapture(self._frames()),
                        writer=DummyWriter(),
                    )

    def test_rejects_gaps_and_rows_outside_source_frame_domain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            rows = self._default_rows()
            rows[1]["Frame"] = "2"
            source, path, report = self._write_inputs(Path(temp_name), rows)
            with self.assertRaisesRegex(CameraPathRenderError, "contiguous from zero"):
                render_camera_path_video(
                    source,
                    path,
                    report,
                    target_width=16,
                    target_height=9,
                    capture=DummyCapture(self._frames()),
                    writer=DummyWriter(),
                )

        with tempfile.TemporaryDirectory() as temp_name:
            rows = self._default_rows()
            rows.append({**rows[-1], "Frame": "3"})
            source, path, report = self._write_inputs(Path(temp_name), rows, frame_count=3)
            with self.assertRaisesRegex(CameraPathRenderError, "outside the source domain"):
                render_camera_path_video(
                    source,
                    path,
                    report,
                    target_width=16,
                    target_height=9,
                    capture=DummyCapture(self._frames()),
                    writer=DummyWriter(),
                )

    def test_rejects_short_decode_and_releases_resources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source, path, report = self._write_inputs(Path(temp_name), self._default_rows())
            capture = DummyCapture(self._frames()[:2], frame_count=3)
            writer = DummyWriter()
            with self.assertRaisesRegex(CameraPathRenderError, "source video ends before frame 2"):
                render_camera_path_video(
                    source,
                    path,
                    report,
                    target_width=16,
                    target_height=9,
                    capture=capture,
                    writer=writer,
                )
            self.assertTrue(capture.released)
            self.assertTrue(writer.released)

    def test_rejects_output_inside_immutable_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source, path, report = self._write_inputs(Path(temp_name), self._default_rows())
            with self.assertRaisesRegex(CameraPathRenderError, "immutable hybrid camera generation"):
                render_camera_path_video(
                    source,
                    path,
                    report,
                    report.parent / "rendered.mp4",
                    target_width=16,
                    target_height=9,
                )

    def test_renders_from_hash_bound_snapshot_and_rejects_original_path_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source, path, report = self._write_inputs(Path(temp_name), self._default_rows())
            original_render_rows = __import__(
                "football_tracking.camera_path_renderer",
                fromlist=["_render_rows"],
            )._render_rows

            def mutate_original_then_render(*args, **kwargs):
                before = path.stat()
                payload = bytearray(path.read_bytes())
                payload[-2] = ord("1") if payload[-2] != ord("1") else ord("2")
                path.write_bytes(payload)
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                return original_render_rows(*args, **kwargs)

            with (
                mock.patch(
                    "football_tracking.camera_path_renderer._render_rows",
                    side_effect=mutate_original_then_render,
                ),
                self.assertRaisesRegex(CameraPathRenderError, "changed during rendering"),
            ):
                render_camera_path_video(
                    source,
                    path,
                    report,
                    target_width=16,
                    target_height=9,
                    capture=DummyCapture(self._frames()),
                    writer=DummyWriter(),
                )

    def test_releases_all_resources_when_release_raises_base_exception(self) -> None:
        class InterruptingWriter(DummyWriter):
            def release(self) -> None:
                self.released = True
                raise KeyboardInterrupt("release interrupted")

        with tempfile.TemporaryDirectory() as temp_name:
            source, path, report = self._write_inputs(Path(temp_name), self._default_rows())
            capture = DummyCapture(self._frames())
            writer = InterruptingWriter()
            with self.assertRaises(KeyboardInterrupt):
                render_camera_path_video(
                    source,
                    path,
                    report,
                    target_width=16,
                    target_height=9,
                    capture=capture,
                    writer=writer,
                )
            self.assertTrue(writer.released)
            self.assertTrue(capture.released)

    def test_output_is_fail_if_exists_and_cross_process_locked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base = Path(temp_name)
            generation = base / "generation"
            generation.mkdir()
            source, path, report = self._write_inputs(generation, self._default_rows())
            output = base / "rendered.mp4"
            output.write_bytes(b"keep")
            with self.assertRaisesRegex(CameraPathRenderError, "already exists"):
                render_camera_path_video(
                    source,
                    path,
                    report,
                    output,
                    target_width=16,
                    target_height=9,
                )
            self.assertEqual(b"keep", output.read_bytes())

            output.unlink()
            lock = _acquire_render_output_lock(output)
            try:
                with self.assertRaisesRegex(CameraPathRenderError, "already locked"):
                    render_camera_path_video(
                        source,
                        path,
                        report,
                        output,
                        target_width=16,
                        target_height=9,
                    )
            finally:
                _release_render_output_lock(lock)

    def test_published_output_cannot_use_an_injected_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base = Path(temp_name)
            generation = base / "generation"
            generation.mkdir()
            source, path, report = self._write_inputs(generation, self._default_rows())
            with self.assertRaisesRegex(CameraPathRenderError, "injected capture"):
                render_camera_path_video(
                    source,
                    path,
                    report,
                    base / "rendered.mp4",
                    target_width=16,
                    target_height=9,
                    capture=DummyCapture(self._frames()),
                )

    def test_render_validation_accepts_standard_ntsc_timebase_quantization(self) -> None:
        for exact_fps, container_fps in ((30_000 / 1001, 29.97), (24_000 / 1001, 23.976)):
            with self.subTest(exact_fps=exact_fps), tempfile.TemporaryDirectory() as temp_name:
                path = Path(temp_name) / "rendered.avi"
                writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), container_fps, (32, 18))
                self.assertTrue(writer.isOpened())
                try:
                    writer.write(np.zeros((18, 32, 3), dtype=np.uint8))
                    writer.write(np.ones((18, 32, 3), dtype=np.uint8))
                finally:
                    writer.release()
                _validate_rendered_video(path, frame_count=2, width=32, height=18, fps=exact_fps)

    def _write_inputs(
        self,
        root: Path,
        rows: list[dict[str, str]],
        *,
        frame_count: int = 3,
    ) -> tuple[Path, Path, Path]:
        source = root / "source.mp4"
        source.write_bytes(b"bound source video")
        camera_path = root / CAMERA_PATH_NAME
        with camera_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        report = root / REPORT_NAME
        report.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "artifact_type": "hybrid_broadcast_camera_report",
                    "status": "succeeded",
                    "complete": True,
                    "source_video": {
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "width": 32,
                        "height": 18,
                        "fps": 10.0,
                        "frame_count": frame_count,
                    },
                    "artifacts": {
                        CAMERA_PATH_NAME: {
                            "sha256": hashlib.sha256(camera_path.read_bytes()).hexdigest(),
                            "size": camera_path.stat().st_size,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return source, camera_path, report

    def _default_rows(self) -> list[dict[str, str]]:
        return [
            self._row(0, 0, 0, "ball_guided"),
            self._row(1, 16, 9, "unknown"),
            self._row(2, 0, 0, "broadcast_guided"),
        ]

    def _row(self, frame: int, x1: int, y1: int, status: str) -> dict[str, str]:
        return {
            "Frame": str(frame),
            "CenterX": str(x1 + 8.0),
            "CenterY": str(y1 + 4.5),
            "CropX1": str(x1),
            "CropY1": str(y1),
            "CropX2": str(x1 + 16),
            "CropY2": str(y1 + 9),
            "CropWidth": "16",
            "CropHeight": "9",
            "Status": status,
            "PanMode": "hold" if status == "unknown" else "glide",
        }

    def _frames(self) -> list[np.ndarray]:
        return [np.zeros((18, 32, 3), dtype=np.uint8) for _ in range(3)]


if __name__ == "__main__":
    unittest.main()
