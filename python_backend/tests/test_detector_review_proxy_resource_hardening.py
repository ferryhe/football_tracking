from __future__ import annotations

import errno
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

import football_tracking.detector_review_proxy as review_proxy_module
import football_tracking.detector_review_proxy_worker as review_proxy_worker_module
from football_tracking.detector_development_common import (
    DetectorDevelopmentError,
    regular_file_change_identity,
)


class DetectorReviewProxyResourceHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source_root = self.root / "data"
        self.source_root.mkdir()
        self.source = self.source_root / "source.mp4"
        self.source.write_bytes(b"frozen-source-bytes")

    @staticmethod
    def _write_video(path: Path, *, gray: bool) -> None:
        import imageio_ffmpeg

        writer = imageio_ffmpeg.write_frames(
            path,
            (512, 144),
            pix_fmt_in="bgr24",
            pix_fmt_out="yuv420p",
            fps=5.0,
            codec="libx264",
            macro_block_size=1,
            ffmpeg_log_level="error",
        )
        writer.send(None)
        try:
            for index in range(2):
                if gray:
                    frame = np.full((144, 512, 3), 128, dtype=np.uint8)
                else:
                    y, x = np.indices((144, 512), dtype=np.uint16)
                    frame = np.stack(
                        (
                            (3 * x + 5 * y + 17 * index) % 256,
                            (7 * x + 2 * y + 43 * index) % 256,
                            (11 * x + 13 * y + 71 * index) % 256,
                        ),
                        axis=2,
                    ).astype(np.uint8)
                writer.send(frame.tobytes())
        finally:
            writer.close()

    def test_hash_rejects_expected_size_mismatch_before_opening_file(self) -> None:
        expected_identity = regular_file_change_identity(self.source, "test review proxy source")
        with (
            mock.patch.object(review_proxy_module.os, "open", wraps=os.open) as opened,
            self.assertRaises(DetectorDevelopmentError) as raised,
        ):
            review_proxy_module._hash_file_cancellable(
                self.source,
                lambda: False,
                expected_identity=expected_identity,
                expected_size=self.source.stat().st_size + 1,
                max_bytes=self.source.stat().st_size + 1,
                trusted_root=self.root,
            )

        self.assertEqual("source_changed", raised.exception.code)
        opened.assert_not_called()

    def test_hash_rejects_hard_limit_before_opening_file(self) -> None:
        expected_identity = regular_file_change_identity(self.source, "test review proxy source")
        with (
            mock.patch.object(review_proxy_module.os, "open", wraps=os.open) as opened,
            self.assertRaises(DetectorDevelopmentError) as raised,
        ):
            review_proxy_module._hash_file_cancellable(
                self.source,
                lambda: False,
                expected_identity=expected_identity,
                expected_size=self.source.stat().st_size,
                max_bytes=self.source.stat().st_size - 1,
                trusted_root=self.root,
            )

        self.assertEqual("resource_limit_exceeded", raised.exception.code)
        opened.assert_not_called()

    def test_hash_rejects_opened_handle_for_a_different_file(self) -> None:
        expected_identity = regular_file_change_identity(self.source, "test review proxy source")
        replacement = self.source_root / "replacement.mp4"
        replacement.write_bytes(b"different-file")
        replacement_fd = os.open(replacement, os.O_RDONLY | getattr(os, "O_BINARY", 0))

        with (
            mock.patch.object(review_proxy_module.os, "open", return_value=replacement_fd),
            self.assertRaises(DetectorDevelopmentError) as raised,
        ):
            review_proxy_module._hash_file_cancellable(
                self.source,
                lambda: False,
                expected_identity=expected_identity,
                expected_size=self.source.stat().st_size,
                max_bytes=self.source.stat().st_size,
                trusted_root=self.root,
            )

        self.assertEqual("source_changed", raised.exception.code)

    def test_hash_detects_growth_past_frozen_size(self) -> None:
        expected_identity = regular_file_change_identity(self.source, "test review proxy source")
        calls = 0

        def grow_after_identity_capture() -> bool:
            nonlocal calls
            calls += 1
            if calls == 2:
                with self.source.open("ab") as handle:
                    handle.write(b"growth")
            return False

        with self.assertRaises(DetectorDevelopmentError) as raised:
            review_proxy_module._hash_file_cancellable(
                self.source,
                grow_after_identity_capture,
                expected_identity=expected_identity,
                expected_size=len(b"frozen-source-bytes"),
                max_bytes=1024,
                trusted_root=self.root,
            )

        self.assertEqual("source_changed", raised.exception.code)

    def test_hash_honors_cancellation_between_bounded_reads(self) -> None:
        self.source.write_bytes(b"x" * (2 * 1024 * 1024))
        expected_identity = regular_file_change_identity(self.source, "test review proxy source")
        calls = 0

        def cancel_after_open() -> bool:
            nonlocal calls
            calls += 1
            return calls >= 3

        with self.assertRaises(DetectorDevelopmentError) as raised:
            review_proxy_module._hash_file_cancellable(
                self.source,
                cancel_after_open,
                expected_identity=expected_identity,
                expected_size=self.source.stat().st_size,
                max_bytes=self.source.stat().st_size,
                trusted_root=self.root,
            )

        self.assertEqual("cancelled", raised.exception.code)

    def test_hash_rejects_changed_ancestor_snapshot(self) -> None:
        expected_identity = regular_file_change_identity(self.source, "test review proxy source")
        with (
            mock.patch.object(
                review_proxy_module,
                "_ancestor_identities_are_current",
                return_value=False,
                create=True,
            ),
            self.assertRaises(DetectorDevelopmentError) as raised,
        ):
            review_proxy_module._hash_file_cancellable(
                self.source,
                lambda: False,
                expected_identity=expected_identity,
                expected_size=self.source.stat().st_size,
                max_bytes=self.source.stat().st_size,
                trusted_root=self.root,
            )

        self.assertEqual("source_changed", raised.exception.code)

    def test_hash_rejects_same_bytes_from_a_new_inode_against_frozen_identity(
        self,
    ) -> None:
        expected_identity = regular_file_change_identity(self.source, "test review proxy source")
        replacement = self.source.with_name("same-bytes-replacement.mp4")
        replacement.write_bytes(self.source.read_bytes())
        os.replace(replacement, self.source)
        self.assertNotEqual(
            expected_identity,
            regular_file_change_identity(self.source, "replacement source"),
        )

        with self.assertRaises(DetectorDevelopmentError) as raised:
            review_proxy_module._hash_file_cancellable(
                self.source,
                lambda: False,
                expected_identity=expected_identity,
                expected_size=self.source.stat().st_size,
                max_bytes=self.source.stat().st_size,
                trusted_root=self.root,
            )

        self.assertEqual("source_changed", raised.exception.code)

    def test_transcode_uses_verified_snapshot_during_source_swap_restore(
        self,
    ) -> None:
        self._write_video(self.source, gray=False)
        replacement = self.source.with_name("gray-replacement.mp4")
        self._write_video(replacement, gray=True)
        source_bytes = self.source.read_bytes()
        replacement_bytes = replacement.read_bytes()
        expected_identity = regular_file_change_identity(self.source, "test review proxy source")
        staging = self.root / "staging"
        staging.mkdir()
        request = {
            "source_id": "source-1",
            "source_relative_path": "data/source.mp4",
            "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "source_size_bytes": len(source_bytes),
            "source_width": 512,
            "source_height": 144,
            "source_frame_count": 2,
            "source_fps": 5.0,
            "sampled_frame_indices": [0, 1],
            "_source_path": str(self.source),
            "_source_trusted_root": str(self.root),
            "_source_change_identity": list(expected_identity),
            "_output_hard_limit_bytes": 256 * 1024 * 1024,
            "repair_execution_binding": review_proxy_module._repair_execution_binding(),
        }
        real_run_ffmpeg = review_proxy_module._run_ffmpeg
        observed_input: str | None = None
        path_swap_blocked = False

        def swap_while_encoding(command, should_cancel, **kwargs):
            nonlocal observed_input, path_swap_blocked
            if observed_input is not None:
                return real_run_ffmpeg(command, should_cancel, **kwargs)
            observed_input = command[command.index("-i") + 1]
            self.assertEqual("fd:", observed_input)
            snapshot_handle = kwargs["stdin_handle"]
            self.assertEqual(source_bytes, snapshot_handle.read())
            snapshot_handle.seek(0)
            snapshot_path = staging / ".verified-source.mp4"
            attacker_snapshot = staging / "attacker-snapshot.mp4"
            attacker_snapshot.write_bytes(replacement_bytes)
            try:
                os.replace(attacker_snapshot, snapshot_path)
            except PermissionError:
                path_swap_blocked = True
            original = self.source.with_name("original-during-transcode.mp4")
            self.source.replace(original)
            replacement.replace(self.source)
            try:
                result = real_run_ffmpeg(command, should_cancel, **kwargs)
            finally:
                self.source.unlink()
                original.replace(self.source)
            self.assertEqual(2, result)
            raise DetectorDevelopmentError(
                "synthetic_after_encode",
                "stop after the real transcode seam",
            )

        with (
            mock.patch.object(
                review_proxy_module,
                "_run_ffmpeg",
                side_effect=swap_while_encoding,
            ),
            self.assertRaises(DetectorDevelopmentError) as raised,
        ):
            review_proxy_module.run_detector_review_proxy(
                request,
                staging,
                lambda: False,
                lambda *_: None,
            )

        self.assertEqual("synthetic_after_encode", raised.exception.code)
        self.assertEqual("fd:", observed_input)
        if os.name == "nt":
            self.assertTrue(path_swap_blocked)
        self.assertFalse((staging / ".verified-source.mp4").exists())
        self.assertEqual(source_bytes, self.source.read_bytes())
        capture = cv2.VideoCapture(str(staging / "review_proxy.mp4"))
        try:
            ok, frame = capture.read()
        finally:
            capture.release()
        self.assertTrue(ok)
        self.assertGreater(float(frame.std()), 10.0)

    def test_successful_job_reads_frozen_source_in_one_full_hash_pass(self) -> None:
        self._write_video(self.source, gray=False)
        content = self.source.read_bytes()
        source_hash_calls = 0
        real_hash = review_proxy_module._hash_file_cancellable

        def count_source_hash(path, *args, **kwargs):
            nonlocal source_hash_calls
            if Path(path) == self.source:
                source_hash_calls += 1
            return real_hash(path, *args, **kwargs)

        coordinator = review_proxy_module.DetectorReviewProxyCoordinator(
            self.root,
            runner=review_proxy_module.run_detector_review_proxy,
            auto_start_workers=False,
            output_hard_limit_bytes=256 * 1024 * 1024,
            disk_reserve_bytes=0,
        )
        self.addCleanup(coordinator.close)
        request = {
            "source_id": "source-1",
            "source_relative_path": "data/source.mp4",
            "source_sha256": hashlib.sha256(content).hexdigest(),
            "source_size_bytes": len(content),
            "source_width": 512,
            "source_height": 144,
            "source_frame_count": 2,
            "source_fps": 5.0,
            "sampled_frame_indices": [0, 1],
        }
        created = coordinator.create_proxy(request)

        with mock.patch.object(
            review_proxy_module,
            "_hash_file_cancellable",
            side_effect=count_source_hash,
        ):
            coordinator.execute_proxy(created["repair_id"])

        ready = coordinator.get_proxy(created["repair_id"])
        self.assertEqual("ready", ready["status"], ready)
        self.assertEqual(1, source_hash_calls)

    def test_create_and_close_do_not_hash_bound_11_gib_source_synchronously(
        self,
    ) -> None:
        coordinator = review_proxy_module.DetectorReviewProxyCoordinator(
            self.root,
            auto_start_workers=False,
            output_hard_limit_bytes=1024,
            disk_reserve_bytes=0,
        )
        frozen_identity = list(regular_file_change_identity(self.source, "test review proxy source"))
        frozen_identity[2] = 11_258_707_917
        request = {
            "source_id": "source-1",
            "source_relative_path": "data/source.mp4",
            "source_sha256": "0" * 64,
            "source_size_bytes": frozen_identity[2],
            "source_width": 512,
            "source_height": 144,
            "source_frame_count": 1,
            "source_fps": 20.0,
            "sampled_frame_indices": [0],
        }

        started = time.monotonic()
        with (
            mock.patch.object(
                review_proxy_module,
                "regular_file_change_identity",
                return_value=tuple(frozen_identity),
            ),
            mock.patch.object(
                review_proxy_module,
                "_hash_file_cancellable",
                side_effect=AssertionError("source hashing must run only in the worker"),
            ),
        ):
            created = coordinator.create_proxy(request)
            coordinator.close()

        self.assertEqual("queued", created["status"])
        self.assertLess(time.monotonic() - started, 2.0)

    def test_close_cancels_in_progress_source_hash_and_releases_handle(self) -> None:
        content = b"x" * (2 * 1024 * 1024)
        self.source.write_bytes(content)
        coordinator = review_proxy_module.DetectorReviewProxyCoordinator(
            self.root,
            runner=mock.Mock(side_effect=AssertionError("runner must not start")),
            auto_start_workers=False,
            output_hard_limit_bytes=1024,
            disk_reserve_bytes=0,
        )
        request = {
            "source_id": "source-1",
            "source_relative_path": "data/source.mp4",
            "source_sha256": hashlib.sha256(content).hexdigest(),
            "source_size_bytes": len(content),
            "source_width": 512,
            "source_height": 144,
            "source_frame_count": 1,
            "source_fps": 20.0,
            "sampled_frame_indices": [0],
        }
        created = coordinator.create_proxy(request)
        execution = threading.Thread(
            target=coordinator.execute_proxy,
            args=(created["repair_id"],),
            daemon=True,
        )

        with mock.patch.object(review_proxy_module, "_SOURCE_HASH_CHUNK_BYTES", 1):
            execution.start()
            deadline = time.monotonic() + 5.0
            while coordinator.get_proxy(created["repair_id"])["status"] == "queued":
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            coordinator.close()
            execution.join(timeout=5.0)

        self.assertFalse(execution.is_alive())
        moved = self.source.with_name("source-moved.mp4")
        self.source.replace(moved)
        self.assertTrue(moved.is_file())

    def test_disk_preflight_includes_verified_source_snapshot_bytes(self) -> None:
        content = self.source.read_bytes()
        output_limit = 1024
        reserve = 128
        runner = mock.Mock(side_effect=AssertionError("runner must not start"))
        coordinator = review_proxy_module.DetectorReviewProxyCoordinator(
            self.root,
            runner=runner,
            auto_start_workers=False,
            output_hard_limit_bytes=output_limit,
            disk_reserve_bytes=reserve,
        )
        self.addCleanup(coordinator.close)
        request = {
            "source_id": "source-1",
            "source_relative_path": "data/source.mp4",
            "source_sha256": hashlib.sha256(content).hexdigest(),
            "source_size_bytes": len(content),
            "source_width": 512,
            "source_height": 144,
            "source_frame_count": 1,
            "source_fps": 20.0,
            "sampled_frame_indices": [0],
        }
        created = coordinator.create_proxy(request)
        available_without_full_snapshot = output_limit + reserve + len(content) - 1

        with mock.patch.object(
            review_proxy_module.shutil,
            "disk_usage",
            return_value=mock.Mock(free=available_without_full_snapshot),
        ):
            coordinator.execute_proxy(created["repair_id"])

        failed = coordinator.get_proxy(created["repair_id"])
        self.assertEqual("failed", failed["status"])
        self.assertEqual("insufficient_proxy_disk_capacity", failed["error_code"])
        runner.assert_not_called()

    def test_snapshot_disk_fill_is_reported_as_disk_exhausted(self) -> None:
        identity = regular_file_change_identity(
            self.source,
            "test review proxy source",
        )
        snapshot = self.root / "snapshot.mp4"
        with (
            mock.patch.object(
                review_proxy_module.os,
                "write",
                side_effect=OSError(errno.ENOSPC, "disk full", str(snapshot)),
            ),
            self.assertRaises(DetectorDevelopmentError) as raised,
        ):
            review_proxy_module._hash_file_cancellable(
                self.source,
                lambda: False,
                expected_identity=identity,
                expected_size=self.source.stat().st_size,
                max_bytes=self.source.stat().st_size,
                trusted_root=self.root,
                copy_to=snapshot,
                copy_trusted_root=self.root,
            )

        self.assertEqual("disk_exhausted", raised.exception.code)
        self.assertEqual(
            "Review proxy storage capacity was exhausted",
            str(raised.exception),
        )
        self.assertFalse(snapshot.exists())

    def test_source_delete_race_never_exposes_path_from_in_process_runner(
        self,
    ) -> None:
        content = self.source.read_bytes()

        def delete_then_run(request, staging, should_cancel, progress):
            self.source.unlink()
            return review_proxy_module.run_detector_review_proxy(
                request,
                staging,
                should_cancel,
                progress,
            )

        coordinator = review_proxy_module.DetectorReviewProxyCoordinator(
            self.root,
            runner=delete_then_run,
            auto_start_workers=False,
            output_hard_limit_bytes=1024,
            disk_reserve_bytes=0,
        )
        self.addCleanup(coordinator.close)
        request = {
            "source_id": "source-1",
            "source_relative_path": "data/source.mp4",
            "source_sha256": hashlib.sha256(content).hexdigest(),
            "source_size_bytes": len(content),
            "source_width": 512,
            "source_height": 144,
            "source_frame_count": 1,
            "source_fps": 20.0,
            "sampled_frame_indices": [0],
        }
        created = coordinator.create_proxy(request)

        coordinator.execute_proxy(created["repair_id"])

        failed = coordinator.get_proxy(created["repair_id"])
        self.assertEqual("source_changed", failed["error_code"])
        self.assertEqual(
            "Review proxy source changed or became unavailable",
            failed["error_message"],
        )
        self.assertNotIn(str(self.root), failed["error_message"])

    def test_source_delete_race_never_exposes_path_from_supervised_worker(
        self,
    ) -> None:
        content = self.source.read_bytes()
        coordinator = review_proxy_module.DetectorReviewProxyCoordinator(
            self.root,
            auto_start_workers=False,
            output_hard_limit_bytes=1024,
            disk_reserve_bytes=0,
        )
        self.addCleanup(coordinator.close)
        request = {
            "source_id": "source-1",
            "source_relative_path": "data/source.mp4",
            "source_sha256": hashlib.sha256(content).hexdigest(),
            "source_size_bytes": len(content),
            "source_width": 512,
            "source_height": 144,
            "source_frame_count": 1,
            "source_fps": 20.0,
            "sampled_frame_indices": [0],
        }
        created = coordinator.create_proxy(request)
        real_popen = review_proxy_module.subprocess.Popen

        def delete_then_launch(*args, **kwargs):
            self.source.unlink(missing_ok=True)
            return real_popen(*args, **kwargs)

        with mock.patch.object(
            review_proxy_module.subprocess,
            "Popen",
            side_effect=delete_then_launch,
        ):
            coordinator.execute_proxy(created["repair_id"])

        failed = coordinator.get_proxy(created["repair_id"])
        self.assertEqual("source_changed", failed["error_code"])
        self.assertEqual(
            "Review proxy source changed or became unavailable",
            failed["error_message"],
        )
        self.assertNotIn(str(self.root), failed["error_message"])

    def test_worker_error_envelope_rejects_arbitrary_code_and_exception_path(
        self,
    ) -> None:
        staging = self.root / "worker-staging"
        control = staging / ".worker-protocol" / "control"
        control.mkdir(parents=True)
        worker_id = "worker-1"
        review_proxy_module.atomic_write_json(
            control / "input.json",
            {
                "schema_version": "1.0",
                "artifact_type": "detector_review_proxy_worker_input",
                "worker_id": worker_id,
                "request": {},
            },
            trusted_root=control,
        )
        secret_path = self.root / "private" / "source.mp4"
        parent_monitor = mock.Mock()
        parent_monitor.is_alive.return_value = True

        with (
            mock.patch.object(
                review_proxy_worker_module,
                "_open_parent_monitor",
                return_value=parent_monitor,
            ),
            mock.patch.object(
                review_proxy_worker_module,
                "run_detector_review_proxy",
                side_effect=DetectorDevelopmentError(
                    "private_secret_token",
                    str(secret_path),
                ),
            ),
        ):
            return_code = review_proxy_worker_module.run_worker(
                control,
                staging,
                os.getpid(),
            )

        self.assertEqual(74, return_code)
        error = json.loads((control / "error.json").read_text(encoding="utf-8"))
        self.assertEqual("review_proxy_failed", error["code"])
        self.assertEqual("Review proxy operation failed", error["message"])
        self.assertNotIn(str(secret_path), json.dumps(error))
        parent_monitor.close.assert_called_once()

    def test_in_process_failure_rejects_arbitrary_public_error_code(self) -> None:
        content = self.source.read_bytes()
        secret_path = self.root / "private" / "source.mp4"

        def forged_failure(*_args, **_kwargs):
            raise DetectorDevelopmentError(
                "private_secret_token",
                str(secret_path),
            )

        coordinator = review_proxy_module.DetectorReviewProxyCoordinator(
            self.root,
            runner=forged_failure,
            auto_start_workers=False,
            output_hard_limit_bytes=1024,
            disk_reserve_bytes=0,
        )
        self.addCleanup(coordinator.close)
        created = coordinator.create_proxy(
            {
                "source_id": "source-1",
                "source_relative_path": "data/source.mp4",
                "source_sha256": hashlib.sha256(content).hexdigest(),
                "source_size_bytes": len(content),
                "source_width": 512,
                "source_height": 144,
                "source_frame_count": 1,
                "source_fps": 20.0,
                "sampled_frame_indices": [0],
            }
        )

        coordinator.execute_proxy(created["repair_id"])

        failed = coordinator.get_proxy(created["repair_id"])
        self.assertEqual("failed", failed["status"])
        self.assertEqual("review_proxy_failed", failed["error_code"])
        self.assertEqual("Review proxy operation failed", failed["error_message"])
        self.assertNotIn(str(secret_path), json.dumps(failed))

    def test_unrequested_runner_cancel_cannot_forge_cancelled_lifecycle(self) -> None:
        content = self.source.read_bytes()

        def forged_cancel(*_args, **_kwargs):
            raise DetectorDevelopmentError(
                "cancelled",
                "forged cancellation",
            )

        coordinator = review_proxy_module.DetectorReviewProxyCoordinator(
            self.root,
            runner=forged_cancel,
            auto_start_workers=False,
            output_hard_limit_bytes=1024,
            disk_reserve_bytes=0,
        )
        self.addCleanup(coordinator.close)
        created = coordinator.create_proxy(
            {
                "source_id": "source-1",
                "source_relative_path": "data/source.mp4",
                "source_sha256": hashlib.sha256(content).hexdigest(),
                "source_size_bytes": len(content),
                "source_width": 512,
                "source_height": 144,
                "source_frame_count": 1,
                "source_fps": 20.0,
                "sampled_frame_indices": [0],
            }
        )

        coordinator.execute_proxy(created["repair_id"])

        failed = coordinator.get_proxy(created["repair_id"])
        self.assertEqual("failed", failed["status"])
        self.assertEqual("review_proxy_failed", failed["error_code"])
        self.assertEqual("Review proxy operation failed", failed["error_message"])

    def test_ffmpeg_rejects_one_oversized_line_and_reaps_drainers(self) -> None:
        command = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'x' * 65); sys.stdout.flush()",
        ]

        with (
            mock.patch.object(review_proxy_module, "_FFMPEG_MAX_LINE_BYTES", 64, create=True),
            mock.patch.object(
                review_proxy_module,
                "_terminate_process_tree",
                wraps=review_proxy_module._terminate_process_tree,
            ) as terminate,
            self.assertRaises(DetectorDevelopmentError) as raised,
        ):
            review_proxy_module._run_ffmpeg(command, lambda: False)

        self.assertEqual("ffmpeg_output_limit_exceeded", raised.exception.code)
        terminate.assert_called()
        self._assert_no_ffmpeg_drain_threads()

    def test_ffmpeg_rejects_aggregate_output_and_reaps_drainers(self) -> None:
        command = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write((b'x\\n') * 256 + b'x'); sys.stdout.flush()",
        ]

        with (
            mock.patch.object(review_proxy_module, "_FFMPEG_MAX_OUTPUT_BYTES", 512, create=True),
            mock.patch.object(review_proxy_module, "_FFMPEG_DRAIN_QUEUE_SIZE", 2, create=True),
            mock.patch.object(
                review_proxy_module,
                "_terminate_process_tree",
                wraps=review_proxy_module._terminate_process_tree,
            ) as terminate,
            self.assertRaises(DetectorDevelopmentError) as raised,
        ):
            review_proxy_module._run_ffmpeg(command, lambda: False)

        self.assertEqual("ffmpeg_output_limit_exceeded", raised.exception.code)
        terminate.assert_called()
        self._assert_no_ffmpeg_drain_threads()

    def test_ffmpeg_accepts_exact_line_and_aggregate_output_limits(self) -> None:
        line_command = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'x' * 64); sys.stdout.flush()",
        ]
        aggregate_command = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write((b'x\\n') * 256); sys.stdout.flush()",
        ]

        with mock.patch.object(review_proxy_module, "_FFMPEG_MAX_LINE_BYTES", 64):
            self.assertEqual(
                0,
                review_proxy_module._run_ffmpeg(line_command, lambda: False),
            )
        self._assert_no_ffmpeg_drain_threads()
        with (
            mock.patch.object(review_proxy_module, "_FFMPEG_MAX_LINE_BYTES", 64),
            mock.patch.object(review_proxy_module, "_FFMPEG_MAX_OUTPUT_BYTES", 512),
            mock.patch.object(review_proxy_module, "_FFMPEG_DRAIN_QUEUE_SIZE", 2),
        ):
            self.assertEqual(
                0,
                review_proxy_module._run_ffmpeg(aggregate_command, lambda: False),
            )
        self._assert_no_ffmpeg_drain_threads()

    def test_ffmpeg_cancellation_terminates_process_and_reaps_drainers(self) -> None:
        command = [sys.executable, "-c", "import time; time.sleep(30)"]

        with (
            mock.patch.object(
                review_proxy_module,
                "_terminate_process_tree",
                wraps=review_proxy_module._terminate_process_tree,
            ) as terminate,
            self.assertRaises(DetectorDevelopmentError) as raised,
        ):
            review_proxy_module._run_ffmpeg(command, lambda: True)

        self.assertEqual("cancelled", raised.exception.code)
        terminate.assert_called()
        self._assert_no_ffmpeg_drain_threads()

    def test_ffmpeg_deadline_terminates_process_and_reaps_drainers(self) -> None:
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        deadline = time.monotonic() + 0.05

        def timeout() -> bool:
            if time.monotonic() >= deadline:
                raise DetectorDevelopmentError(
                    "review_proxy_worker_timeout",
                    "bounded test deadline expired",
                )
            return False

        with (
            mock.patch.object(
                review_proxy_module,
                "_terminate_process_tree",
                wraps=review_proxy_module._terminate_process_tree,
            ) as terminate,
            self.assertRaises(DetectorDevelopmentError) as raised,
        ):
            review_proxy_module._run_ffmpeg(command, timeout)

        self.assertEqual("review_proxy_worker_timeout", raised.exception.code)
        terminate.assert_called()
        self._assert_no_ffmpeg_drain_threads()

    def test_ffmpeg_cleanup_failure_never_masks_primary_errors(self) -> None:
        real_is_alive = threading.Thread.is_alive

        def force_drain_cleanup_failure(thread: threading.Thread) -> bool:
            if thread.name.startswith("detector-review-proxy-ffmpeg-drain-"):
                return True
            return real_is_alive(thread)

        cases = (
            (
                "cancelled",
                [sys.executable, "-c", "import time; time.sleep(30)"],
                lambda: True,
            ),
            (
                "review_proxy_worker_timeout",
                [sys.executable, "-c", "import time; time.sleep(30)"],
                lambda: (_ for _ in ()).throw(
                    DetectorDevelopmentError(
                        "review_proxy_worker_timeout",
                        "bounded test deadline expired",
                    )
                ),
            ),
        )
        for expected_code, command, should_cancel in cases:
            with (
                self.subTest(expected_code=expected_code),
                mock.patch.object(
                    threading.Thread,
                    "is_alive",
                    force_drain_cleanup_failure,
                ),
                self.assertRaises(DetectorDevelopmentError) as raised,
            ):
                review_proxy_module._run_ffmpeg(command, should_cancel)
            self.assertEqual(expected_code, raised.exception.code)
        self._assert_no_ffmpeg_drain_threads()

    def test_ffmpeg_cleanup_failure_never_masks_output_limit(self) -> None:
        real_is_alive = threading.Thread.is_alive

        def force_drain_cleanup_failure(thread: threading.Thread) -> bool:
            if thread.name.startswith("detector-review-proxy-ffmpeg-drain-"):
                return True
            return real_is_alive(thread)

        command = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'x' * 65); sys.stdout.flush()",
        ]
        with (
            mock.patch.object(review_proxy_module, "_FFMPEG_MAX_LINE_BYTES", 64),
            mock.patch.object(
                threading.Thread,
                "is_alive",
                force_drain_cleanup_failure,
            ),
            self.assertRaises(DetectorDevelopmentError) as raised,
        ):
            review_proxy_module._run_ffmpeg(command, lambda: False)

        self.assertEqual("ffmpeg_output_limit_exceeded", raised.exception.code)
        self._assert_no_ffmpeg_drain_threads()

    def test_ffmpeg_failure_never_exposes_stderr_paths_or_secrets(self) -> None:
        secret = "secret-token C:/private/source.mp4"
        command = [
            sys.executable,
            "-c",
            f"import sys; sys.stderr.write({secret!r}); sys.exit(7)",
        ]

        with self.assertRaises(DetectorDevelopmentError) as raised:
            review_proxy_module._run_ffmpeg(command, lambda: False)

        self.assertEqual("ffmpeg_failed", raised.exception.code)
        self.assertEqual("Bundled ffmpeg failed", str(raised.exception))
        self.assertNotIn("secret-token", str(raised.exception))
        self.assertNotIn("private/source.mp4", str(raised.exception))
        diagnostics = raised.exception._internal_diagnostics
        self.assertEqual(
            {"stderr_tail_bytes", "stderr_tail_sha256"},
            set(diagnostics),
        )

    def _assert_no_ffmpeg_drain_threads(self) -> None:
        leaked = [
            thread.name
            for thread in threading.enumerate()
            if thread.name.startswith("detector-review-proxy-ffmpeg-drain-")
        ]
        self.assertEqual([], leaked)


if __name__ == "__main__":
    unittest.main()
