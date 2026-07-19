from __future__ import annotations

import hashlib
import inspect
import io
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

import football_tracking.detector_review_proxy as review_proxy_module
from football_tracking.detector_development_common import (
    DetectorDevelopmentError,
    hash_regular_file,
    regular_file_change_identity,
)
from football_tracking.detector_review_proxy import (
    DetectorReviewProxyCoordinator,
    _repair_execution_binding,
    _verify_staged_media,
    run_detector_review_proxy,
)


class DetectorReviewProxyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo_root = Path(self.temporary.name).resolve()
        (self.repo_root / "data").mkdir()
        self.source = self.repo_root / "data" / "source.mp4"
        self._write_source(self.source, frame_count=4, fps=5.0)
        self.source_sha256, self.source_size = hash_regular_file(
            self.source, "test source", trusted_root=self.repo_root
        )

    @staticmethod
    def _write_source(path: Path, *, frame_count: int, fps: float) -> None:
        import imageio_ffmpeg

        writer = imageio_ffmpeg.write_frames(
            path,
            (512, 144),
            pix_fmt_in="bgr24",
            pix_fmt_out="yuv420p",
            fps=fps,
            codec="libx264",
            macro_block_size=1,
            ffmpeg_log_level="error",
        )
        writer.send(None)
        try:
            for index in range(frame_count):
                y, x = np.indices((144, 512), dtype=np.uint16)
                frame = np.stack(
                    (
                        (3 * x + 5 * y + 17 * index) % 256,
                        (7 * x + 2 * y + 43 * index) % 256,
                        (11 * x + 13 * y + 71 * index) % 256,
                    ),
                    axis=2,
                ).astype(np.uint8)
                cv2.putText(
                    frame,
                    str(index),
                    (20, 90),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    2,
                    (255, 255, 255),
                    3,
                )
                writer.send(frame.tobytes())
        finally:
            writer.close()

    @staticmethod
    def _write_gray_source(path: Path, *, frame_count: int, fps: float) -> None:
        import imageio_ffmpeg

        writer = imageio_ffmpeg.write_frames(
            path,
            (512, 144),
            pix_fmt_in="bgr24",
            pix_fmt_out="yuv420p",
            fps=fps,
            codec="libx264",
            macro_block_size=1,
            ffmpeg_log_level="error",
        )
        writer.send(None)
        try:
            gray = np.full((144, 512, 3), 128, dtype=np.uint8)
            for _index in range(frame_count):
                writer.send(gray.tobytes())
        finally:
            writer.close()

    def request(self) -> dict[str, object]:
        return {
            "source_id": "source-1",
            "source_relative_path": "data/source.mp4",
            "source_sha256": self.source_sha256,
            "source_size_bytes": self.source_size,
            "source_width": 512,
            "source_height": 144,
            "source_frame_count": 4,
            "source_fps": 5.0,
            "sampled_frame_indices": [0, 2, 3],
        }

    def test_runner_builds_fixed_h264_proxy_and_hash_bound_samples(self) -> None:
        staging = self.repo_root / "staging"
        staging.mkdir()
        request = {
            **self.request(),
            "_source_path": str(self.source),
            "_source_change_identity": list(regular_file_change_identity(self.source, "test source")),
            "_output_hard_limit_bytes": 256 * 1024 * 1024,
            "repair_execution_binding": _repair_execution_binding(),
        }

        progress: list[tuple[int, int]] = []
        output = run_detector_review_proxy(
            request,
            staging,
            lambda: False,
            lambda completed, total: progress.append((completed, total)),
        )

        self.assertEqual((2560, 720), (output["proxy_width"], output["proxy_height"]))
        self.assertEqual(4, output["proxy_frame_count"])
        self.assertAlmostEqual(5.0, output["proxy_fps"], places=3)
        self.assertEqual("libx264", output["encoding"]["codec"])
        self.assertEqual("one_output_per_decoded_source_frame", output["encoding"]["frame_sync"])
        self.assertEqual(0.1, output["encoding"]["timing_residual_tolerance_msec"])
        self.assertIn("setpts=N/(5*TB)", output["encoding"]["video_filter"])
        self.assertEqual([0, 2, 3], [item["frame_index"] for item in output["sampled_frames"]])
        self.assertEqual(3, len({item["sha256"] for item in output["sampled_frames"]}))
        for item in output["sampled_frames"]:
            sample = staging / item["relative_path"]
            self.assertTrue(sample.is_file())
            self.assertEqual(hashlib.sha256(sample.read_bytes()).hexdigest(), item["sha256"])
        self.assertTrue(progress)
        self.assertEqual((11, 15), progress[-1])
        _verify_staged_media(output, staging, request, lambda: False, lambda *item: progress.append(item))
        self.assertEqual((15, 15), progress[-1])

    def test_default_verifier_rejects_corrupt_sample_after_valid_full_decode(self) -> None:
        staging = self.repo_root / "verify-staging"
        staging.mkdir()
        request = {
            **self.request(),
            "_source_path": str(self.source),
            "_source_change_identity": list(regular_file_change_identity(self.source, "test source")),
            "_output_hard_limit_bytes": 256 * 1024 * 1024,
            "repair_execution_binding": _repair_execution_binding(),
        }
        output = run_detector_review_proxy(request, staging, lambda: False, lambda *_: None)
        sample = staging / output["sampled_frames"][0]["relative_path"]
        sample.write_bytes(b"not a JPEG")

        with self.assertRaisesRegex(DetectorDevelopmentError, "sampled proxy JPEG"):
            _verify_staged_media(output, staging, request, lambda: False, lambda *_: None)

    def test_default_verifier_rejects_valid_sample_from_the_wrong_proxy_frame(self) -> None:
        staging = self.repo_root / "wrong-mapping-staging"
        staging.mkdir()
        request = {
            **self.request(),
            "_source_path": str(self.source),
            "_source_change_identity": list(regular_file_change_identity(self.source, "test source")),
            "_output_hard_limit_bytes": 256 * 1024 * 1024,
            "repair_execution_binding": _repair_execution_binding(),
        }
        output = run_detector_review_proxy(request, staging, lambda: False, lambda *_: None)
        first = staging / output["sampled_frames"][0]["relative_path"]
        second = staging / output["sampled_frames"][1]["relative_path"]
        first_bytes = first.read_bytes()
        first.write_bytes(second.read_bytes())
        second.write_bytes(first_bytes)

        with self.assertRaisesRegex(DetectorDevelopmentError, "mapped proxy JPEG"):
            _verify_staged_media(output, staging, request, lambda: False, lambda *_: None)

    def test_default_verifier_honors_cancellation(self) -> None:
        staging = self.repo_root / "cancel-verifier-staging"
        staging.mkdir()
        request = {
            **self.request(),
            "_source_path": str(self.source),
            "_source_change_identity": list(regular_file_change_identity(self.source, "test source")),
            "_output_hard_limit_bytes": 256 * 1024 * 1024,
            "repair_execution_binding": _repair_execution_binding(),
        }
        output = run_detector_review_proxy(request, staging, lambda: False, lambda *_: None)

        with self.assertRaisesRegex(DetectorDevelopmentError, "cancelled"):
            _verify_staged_media(
                output,
                staging,
                request,
                lambda: True,
                lambda *_: None,
            )

    def test_default_verifier_requires_explicit_supervision_callbacks(self) -> None:
        parameters = inspect.signature(_verify_staged_media).parameters

        self.assertIs(inspect.Parameter.empty, parameters["should_cancel"].default)
        self.assertIs(inspect.Parameter.empty, parameters["progress"].default)

    def test_default_verifier_rejects_a_real_gray_low_information_jpeg(self) -> None:
        staging = self.repo_root / "gray-sample-verifier-staging"
        staging.mkdir()
        request = {
            **self.request(),
            "_source_path": str(self.source),
            "_source_change_identity": list(regular_file_change_identity(self.source, "test source")),
            "_output_hard_limit_bytes": 256 * 1024 * 1024,
            "repair_execution_binding": _repair_execution_binding(),
        }
        output = run_detector_review_proxy(request, staging, lambda: False, lambda *_: None)
        sample = staging / output["sampled_frames"][0]["relative_path"]
        self.assertTrue(cv2.imwrite(str(sample), np.full((720, 2560, 3), 128, dtype=np.uint8)))

        with self.assertRaises(DetectorDevelopmentError) as raised:
            _verify_staged_media(output, staging, request, lambda: False, lambda *_: None)

        self.assertEqual("review_proxy_sample_low_information", raised.exception.code)

    def test_runner_rejects_gray_low_information_generated_samples(self) -> None:
        gray_source = self.repo_root / "data" / "gray-source.mp4"
        self._write_gray_source(gray_source, frame_count=4, fps=5.0)
        gray_sha, gray_size = hash_regular_file(gray_source, "gray source", trusted_root=self.repo_root)
        staging = self.repo_root / "gray-runner-staging"
        staging.mkdir()
        request = {
            **self.request(),
            "source_relative_path": "data/gray-source.mp4",
            "source_sha256": gray_sha,
            "source_size_bytes": gray_size,
            "_source_path": str(gray_source),
            "_source_change_identity": list(regular_file_change_identity(gray_source, "gray source")),
            "_output_hard_limit_bytes": 256 * 1024 * 1024,
            "repair_execution_binding": _repair_execution_binding(),
        }

        with self.assertRaises(DetectorDevelopmentError) as raised:
            run_detector_review_proxy(request, staging, lambda: False, lambda *_: None)

        self.assertEqual("review_proxy_sample_low_information", raised.exception.code)

    def test_real_worker_process_reaches_ready_with_default_verifier(self) -> None:
        coordinator = DetectorReviewProxyCoordinator(
            self.repo_root,
            auto_start_workers=False,
            worker_deadline_seconds=60.0,
            output_hard_limit_bytes=256 * 1024 * 1024,
            disk_reserve_bytes=0,
        )
        self.addCleanup(coordinator.close)
        created = coordinator.create_proxy(self.request())

        coordinator.execute_proxy(created["repair_id"])
        ready = coordinator.get_verified_proxy(created["repair_id"])

        self.assertEqual("ready", ready["status"], ready)
        self.assertTrue(ready["report"]["integrity"]["full_proxy_decode_verified"])
        self.assertTrue(ready["report"]["integrity"]["sample_media_integrity_verified"])
        self.assertTrue(ready["report"]["integrity"]["generated_sample_media_integrity_verified"])
        self.assertEqual(3, ready["report"]["integrity"]["sample_count"])
        self.assertTrue(
            all(
                item["media_integrity"]["low_information"] is False
                and item["media_integrity"]["likely_corrupt"] is False
                and item["media_integrity"]["gray"] is False
                for item in ready["report"]["sampled_frames"]
            )
        )
        self.assertEqual(
            ready["report"]["repair_execution_binding"]["binding_sha256"],
            ready["frozen_request"]["repair_execution_binding"]["binding_sha256"],
        )
        result_root = (
            self.repo_root
            / "data"
            / "ball_detector_development_v1"
            / "review_proxies"
            / "results"
            / created["repair_id"]
        )
        self.assertEqual(
            {
                "detector_review_proxy_manifest.v1.json",
                "detector_review_proxy_report.v1.json",
                "review_proxy.mp4",
                "sampled_frames/frame_0000000000.jpg",
                "sampled_frames/frame_0000000002.jpg",
                "sampled_frames/frame_0000000003.jpg",
            },
            {path.relative_to(result_root).as_posix() for path in result_root.rglob("*") if path.is_file()},
        )
        (result_root / "unexpected.txt").write_text("not allowlisted", encoding="utf-8")
        with self.assertRaisesRegex(DetectorDevelopmentError, "unexpected"):
            coordinator.get_verified_proxy(created["repair_id"])

    def test_repair_execution_bundle_covers_imported_parent_monitor_code(self) -> None:
        binding = _repair_execution_binding()

        self.assertIn(
            "football_tracking/detector_probe_worker.py",
            binding["code_files"],
        )
        self.assertIn("football_tracking/media_integrity.py", binding["code_files"])

    def test_runner_fails_closed_when_expected_source_digest_is_wrong(self) -> None:
        staging = self.repo_root / "bad-staging"
        staging.mkdir()
        request = {
            **self.request(),
            "source_sha256": "0" * 64,
            "_source_path": str(self.source),
            "_source_change_identity": list(regular_file_change_identity(self.source, "test source")),
            "_output_hard_limit_bytes": 256 * 1024 * 1024,
            "repair_execution_binding": _repair_execution_binding(),
        }

        with self.assertRaisesRegex(DetectorDevelopmentError, "digest"):
            run_detector_review_proxy(request, staging, lambda: False, lambda *_: None)
        self.assertFalse((staging / "review_proxy.mp4").exists())

    def test_cfr_encode_does_not_hide_source_decode_shortfall(self) -> None:
        staging = self.repo_root / "short-source-staging"
        staging.mkdir()
        request = {
            **self.request(),
            "source_frame_count": 5,
            "_source_path": str(self.source),
            "_source_change_identity": list(regular_file_change_identity(self.source, "test source")),
            "_output_hard_limit_bytes": 256 * 1024 * 1024,
            "repair_execution_binding": _repair_execution_binding(),
        }

        with self.assertRaises(DetectorDevelopmentError) as raised:
            run_detector_review_proxy(request, staging, lambda: False, lambda *_: None)

        self.assertIn(
            raised.exception.code,
            {"review_proxy_frame_count_mismatch", "review_proxy_frame_sync_changed"},
        )

    def test_runner_detects_source_mutation_during_generation(self) -> None:
        staging = self.repo_root / "mutating-source-staging"
        staging.mkdir()
        request = {
            **self.request(),
            "_source_path": str(self.source),
            "_source_change_identity": list(regular_file_change_identity(self.source, "test source")),
            "_output_hard_limit_bytes": 256 * 1024 * 1024,
            "repair_execution_binding": _repair_execution_binding(),
        }
        mutated = False

        def mutate_after_encode_started(completed: int, _total: int) -> None:
            nonlocal mutated
            if completed > 0 and not mutated:
                with self.source.open("ab") as handle:
                    handle.write(b"source mutation")
                mutated = True

        with self.assertRaisesRegex(DetectorDevelopmentError, "changed"):
            run_detector_review_proxy(request, staging, lambda: False, mutate_after_encode_started)
        self.assertTrue(mutated)

    def test_runner_enforces_output_hard_limit(self) -> None:
        staging = self.repo_root / "limited-output-staging"
        staging.mkdir()
        request = {
            **self.request(),
            "_source_path": str(self.source),
            "_source_change_identity": list(regular_file_change_identity(self.source, "test source")),
            "_output_hard_limit_bytes": 1,
            "repair_execution_binding": _repair_execution_binding(),
        }

        with self.assertRaises(DetectorDevelopmentError) as raised:
            run_detector_review_proxy(request, staging, lambda: False, lambda *_: None)

        self.assertEqual("review_proxy_output_limit_exceeded", raised.exception.code)
        self.assertFalse((staging / "sampled_frames").exists())

    def test_verifier_rejects_an_auxiliary_audio_stream(self) -> None:
        staging = self.repo_root / "audio-stream-staging"
        staging.mkdir()
        request = {
            **self.request(),
            "_source_path": str(self.source),
            "_source_change_identity": list(regular_file_change_identity(self.source, "test source")),
            "_output_hard_limit_bytes": 256 * 1024 * 1024,
            "repair_execution_binding": _repair_execution_binding(),
        }
        output = run_detector_review_proxy(request, staging, lambda: False, lambda *_: None)
        proxy = staging / output["proxy_relative_path"]
        with_audio = staging / "with-audio.mp4"
        ffmpeg_path, _binding = review_proxy_module._bundled_ffmpeg_binding()
        completed = subprocess.run(
            [
                ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(proxy),
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=8000:cl=mono",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-shortest",
                "-y",
                str(with_audio),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        with_audio.replace(proxy)

        with self.assertRaisesRegex(DetectorDevelopmentError, "exactly one video stream"):
            _verify_staged_media(output, staging, request, lambda: False, lambda *_: None)

    def test_coordinator_persists_and_atomically_publishes_runner_output(self) -> None:
        def fake_runner(request, staging, should_cancel, progress):
            self.assertFalse(should_cancel())
            proxy = staging / "review_proxy.mp4"
            proxy.write_bytes(b"sealed proxy")
            samples = staging / "sampled_frames"
            samples.mkdir()
            sample = samples / "frame_0000000000.jpg"
            y, x = np.indices((720, 2560), dtype=np.uint16)
            sample_image = np.stack(
                ((3 * x + 5 * y) % 256, (7 * x + 2 * y) % 256, (11 * x + 13 * y) % 256),
                axis=2,
            ).astype(np.uint8)
            self.assertTrue(cv2.imwrite(str(sample), sample_image))
            proxy_sha, proxy_size = hash_regular_file(proxy, "proxy", trusted_root=staging)
            sample_sha, sample_size = hash_regular_file(sample, "sample", trusted_root=staging)
            sample_integrity = review_proxy_module._reviewable_image_integrity(sample)
            progress(1, request["source_frame_count"] * 3 + 1)
            return {
                "schema_version": "1.0",
                "artifact_type": "detector_review_proxy_runner_output",
                "source_sha256": request["source_sha256"],
                "source_size_bytes": request["source_size_bytes"],
                "source_change_identity_before": request["_source_change_identity"],
                "source_change_identity_after": request["_source_change_identity"],
                "source_width": request["source_width"],
                "source_height": request["source_height"],
                "source_frame_count": request["source_frame_count"],
                "source_fps": request["source_fps"],
                "proxy_relative_path": "review_proxy.mp4",
                "proxy_sha256": proxy_sha,
                "proxy_size_bytes": proxy_size,
                "proxy_width": 2560,
                "proxy_height": 720,
                "proxy_frame_count": request["source_frame_count"],
                "proxy_fps": request["source_fps"],
                "proxy_stream_fps": request["source_fps"],
                "proxy_average_fps": request["source_fps"],
                "encoding": {"codec": "libx264", "video_filter": "fixed"},
                "ffmpeg": {"sha256": "f" * 64, "version": "test"},
                "repair_execution_binding": request["repair_execution_binding"],
                "sampled_frames": [
                    {
                        "frame_index": 0,
                        "relative_path": "sampled_frames/frame_0000000000.jpg",
                        "sha256": sample_sha,
                        "size_bytes": sample_size,
                        "width": 2560,
                        "height": 720,
                        "proxy_time_seconds": 0.0,
                        "media_integrity": sample_integrity,
                    }
                ],
            }

        coordinator = DetectorReviewProxyCoordinator(
            self.repo_root,
            runner=fake_runner,
            verifier=lambda _output, _staging, _frozen, _should_cancel, _progress: None,
            auto_start_workers=False,
            output_hard_limit_bytes=256 * 1024 * 1024,
            disk_reserve_bytes=0,
        )
        self.addCleanup(coordinator.close)
        request = self.request()
        request["sampled_frame_indices"] = [0]
        created = coordinator.create_proxy(request)
        coordinator.execute_proxy(created["repair_id"])
        ready = coordinator.get_verified_proxy(created["repair_id"])

        self.assertEqual("ready", ready["status"])
        self.assertFalse(ready["report"]["integrity"]["independent_verification_performed"])
        self.assertFalse(ready["report"]["integrity"]["full_proxy_decode_verified"])
        self.assertFalse(ready["report"]["integrity"]["generated_sample_media_integrity_verified"])
        self.assertEqual(created["repair_id"], ready["repair_id"])
        self.assertEqual(
            f"/api/v1/detector-review-proxy-repairs/{created['repair_id']}",
            ready["status_url"],
        )
        result_root = (
            self.repo_root
            / "data"
            / "ball_detector_development_v1"
            / "review_proxies"
            / "results"
            / created["repair_id"]
        )
        self.assertTrue((result_root / "review_proxy.mp4").is_file())
        self.assertFalse(any(result_root.parent.glob(f".{created['repair_id']}.staging-*")))

    def test_restart_finalizes_result_published_before_ready_record(self) -> None:
        coordinator = DetectorReviewProxyCoordinator(
            self.repo_root,
            auto_start_workers=False,
            worker_deadline_seconds=60.0,
            output_hard_limit_bytes=256 * 1024 * 1024,
            disk_reserve_bytes=0,
        )
        created = coordinator.create_proxy(self.request())
        real_publish = review_proxy_module._publish_staging_directory

        def crash_after_publish(staging: Path, destination: Path) -> None:
            real_publish(staging, destination)
            raise SystemExit("synthetic process death after atomic publication")

        with (
            mock.patch.object(
                review_proxy_module,
                "_publish_staging_directory",
                side_effect=crash_after_publish,
            ),
            self.assertRaisesRegex(SystemExit, "synthetic process death"),
        ):
            coordinator.execute_proxy(created["repair_id"])
        coordinator.close()

        recovered = DetectorReviewProxyCoordinator(
            self.repo_root,
            auto_start_workers=False,
            worker_deadline_seconds=60.0,
            output_hard_limit_bytes=256 * 1024 * 1024,
            disk_reserve_bytes=0,
        )
        self.addCleanup(recovered.close)
        ready = recovered.get_verified_proxy(created["repair_id"])

        self.assertEqual("ready", ready["status"], ready)

    def test_cancelled_queued_job_never_runs(self) -> None:
        runner = mock.Mock(side_effect=AssertionError("runner must not execute"))
        coordinator = DetectorReviewProxyCoordinator(
            self.repo_root,
            runner=runner,
            auto_start_workers=False,
            output_hard_limit_bytes=256 * 1024 * 1024,
            disk_reserve_bytes=0,
        )
        self.addCleanup(coordinator.close)
        created = coordinator.create_proxy(self.request())

        cancelled = coordinator.cancel_proxy(created["repair_id"])
        coordinator.execute_proxy(created["repair_id"])

        self.assertEqual("cancelled", cancelled["status"])
        self.assertEqual("cancelled", coordinator.get_proxy(created["repair_id"])["status"])
        runner.assert_not_called()

    def test_running_job_can_be_cancelled_and_reaps_worker(self) -> None:
        coordinator = DetectorReviewProxyCoordinator(
            self.repo_root,
            auto_start_workers=False,
            worker_deadline_seconds=60.0,
            output_hard_limit_bytes=256 * 1024 * 1024,
            disk_reserve_bytes=0,
        )
        self.addCleanup(coordinator.close)
        created = coordinator.create_proxy(self.request())
        execution = threading.Thread(
            target=coordinator.execute_proxy,
            args=(created["repair_id"],),
            daemon=True,
        )
        execution.start()
        deadline = time.monotonic() + 5.0
        while coordinator.get_proxy(created["repair_id"])["status"] == "queued":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)

        coordinator.cancel_proxy(created["repair_id"])
        execution.join(timeout=10.0)

        self.assertFalse(execution.is_alive())
        self.assertEqual("cancelled", coordinator.get_proxy(created["repair_id"])["status"])
        self.assertIsNone(coordinator._child)

    def test_dynamic_deadline_is_duration_aware_and_bounded_by_configured_cap(self) -> None:
        coordinator = DetectorReviewProxyCoordinator(
            self.repo_root,
            auto_start_workers=False,
            worker_deadline_seconds=3600.0,
            output_hard_limit_bytes=256 * 1024 * 1024,
            disk_reserve_bytes=0,
        )
        self.addCleanup(coordinator.close)
        short = coordinator._dynamic_deadline_seconds({"source_frame_count": 10, "source_fps": 5.0})
        long = coordinator._dynamic_deadline_seconds({"source_frame_count": 100_000, "source_fps": 5.0})

        self.assertGreater(long, short)
        self.assertEqual(3600.0, long)
        self.assertGreaterEqual(short, 300.0)

    def test_worker_control_read_retries_one_atomic_replacement_race(self) -> None:
        for error_code in ("source_changed", "path_unavailable"):
            with self.subTest(error_code=error_code):
                control = self.repo_root / f"control-{error_code}"
                control.mkdir()
                heartbeat = control / "heartbeat.json"
                heartbeat.write_text("{}", encoding="utf-8")
                original_reader = review_proxy_module.read_regular_bytes
                test_thread = threading.get_ident()
                calls = 0

                def replace_once(*args, **kwargs):
                    nonlocal calls
                    if threading.get_ident() != test_thread or not args or Path(args[0]) != heartbeat:
                        return original_reader(*args, **kwargs)
                    calls += 1
                    if calls == 1:
                        replacement = heartbeat.with_suffix(".replacement")
                        replacement.write_text('{"sequence": 7}', encoding="utf-8")
                        replacement.replace(heartbeat)
                        raise DetectorDevelopmentError(
                            error_code,
                            "review proxy worker heartbeat changed during identity validation",
                        )
                    return original_reader(*args, **kwargs)

                with mock.patch.object(
                    review_proxy_module,
                    "read_regular_bytes",
                    side_effect=replace_once,
                ):
                    value = review_proxy_module._read_optional_json(
                        heartbeat,
                        control,
                        "review proxy worker heartbeat",
                    )

                self.assertEqual({"sequence": 7}, value)
                self.assertEqual(2, calls)

    def test_worker_control_read_bounds_persistent_leaf_unavailability(self) -> None:
        control = self.repo_root / "control-unavailable"
        control.mkdir()
        heartbeat = control / "heartbeat.json"
        heartbeat.write_text("{}", encoding="utf-8")
        original_reader = review_proxy_module.read_regular_bytes
        test_thread = threading.get_ident()
        calls = 0

        def unavailable_on_target(*args, **kwargs):
            nonlocal calls
            if threading.get_ident() != test_thread or not args or Path(args[0]) != heartbeat:
                return original_reader(*args, **kwargs)
            calls += 1
            raise DetectorDevelopmentError(
                "path_unavailable",
                "review proxy worker heartbeat is temporarily unavailable",
            )

        with (
            mock.patch.object(
                review_proxy_module,
                "read_regular_bytes",
                side_effect=unavailable_on_target,
            ),
            self.assertRaisesRegex(DetectorDevelopmentError, "did not stabilize") as raised,
        ):
            review_proxy_module._read_optional_json(
                heartbeat,
                control,
                "review proxy worker heartbeat",
            )

        self.assertEqual("invalid_worker_protocol", raised.exception.code)
        self.assertEqual(8, calls)

    @unittest.skipUnless(review_proxy_module.os.name == "nt", "Windows ctypes cache behavior only")
    def test_worker_control_polling_reuses_windows_ctypes_pointer_types(self) -> None:
        import ctypes

        staging = self.repo_root / "staging-memory"
        control = staging / ".worker-protocol" / "control"
        control.mkdir(parents=True)
        heartbeat = control / "heartbeat.json"
        heartbeat.write_text('{"sequence":1}', encoding="utf-8")
        review_proxy_module._read_optional_json(
            heartbeat,
            control,
            "review proxy worker heartbeat",
        )
        pointer_types = set(ctypes._pointer_type_cache)

        for _ in range(100):
            self.assertEqual(
                {"sequence": 1},
                review_proxy_module._read_optional_json(
                    heartbeat,
                    control,
                    "review proxy worker heartbeat",
                ),
            )

        self.assertEqual(pointer_types, set(ctypes._pointer_type_cache))

    def test_worker_control_read_allows_concurrent_staging_output_creation(self) -> None:
        staging = self.repo_root / "staging"
        control = staging / ".worker-protocol" / "control"
        control.mkdir(parents=True)
        heartbeat = control / "heartbeat.json"
        heartbeat.write_text('{"sequence": 1}', encoding="utf-8")
        original_reader = review_proxy_module.read_regular_bytes
        test_thread = threading.get_ident()
        calls = 0

        def create_output_then_read(*args, **kwargs):
            nonlocal calls
            if threading.get_ident() != test_thread or not args or Path(args[0]) != heartbeat:
                return original_reader(*args, **kwargs)
            calls += 1
            (staging / "review_proxy.mp4").write_bytes(b"proxy")
            return original_reader(*args, **kwargs)

        with mock.patch.object(
            review_proxy_module,
            "read_regular_bytes",
            side_effect=create_output_then_read,
        ):
            value = review_proxy_module._read_optional_json(
                heartbeat,
                control,
                "review proxy worker heartbeat",
            )

        self.assertEqual({"sequence": 1}, value)
        self.assertEqual(1, calls)

    def test_worker_control_read_rejects_trusted_directory_replacement(self) -> None:
        for error_code in ("source_changed", "path_unavailable"):
            with self.subTest(error_code=error_code):
                control = self.repo_root / f"control-replaced-{error_code}"
                control.mkdir()
                heartbeat = control / "heartbeat.json"
                heartbeat.write_text('{"sequence": 1}', encoding="utf-8")
                displaced = self.repo_root / f"displaced-control-{error_code}"
                original_reader = review_proxy_module.read_regular_bytes
                test_thread = threading.get_ident()
                calls = 0

                def replace_directory(*args, **kwargs):
                    nonlocal calls
                    if threading.get_ident() != test_thread or not args or Path(args[0]) != heartbeat:
                        return original_reader(*args, **kwargs)
                    calls += 1
                    control.rename(displaced)
                    control.mkdir()
                    heartbeat.write_text('{"sequence": 2}', encoding="utf-8")
                    raise DetectorDevelopmentError(
                        error_code,
                        "review proxy worker heartbeat changed during identity validation",
                    )

                with (
                    mock.patch.object(
                        review_proxy_module,
                        "read_regular_bytes",
                        side_effect=replace_directory,
                    ),
                    self.assertRaisesRegex(DetectorDevelopmentError, "trusted directory changed"),
                ):
                    review_proxy_module._read_optional_json(
                        heartbeat,
                        control,
                        "review proxy worker heartbeat",
                    )

                self.assertEqual(1, calls)

    def test_worker_control_read_rejects_swap_and_restore_after_reading_impostor(self) -> None:
        control = self.repo_root / "control"
        control.mkdir()
        heartbeat = control / "heartbeat.json"
        heartbeat.write_text('{"sequence": 1}', encoding="utf-8")
        displaced = self.repo_root / "displaced-control"
        impostor = self.repo_root / "impostor-control"
        impostor.mkdir()
        (impostor / "heartbeat.json").write_text('{"sequence": 999}', encoding="utf-8")
        original_reader = review_proxy_module.read_regular_bytes
        original_guards = review_proxy_module._open_windows_ancestor_guards
        test_thread = threading.get_ident()
        sampled: list[bytes] = []
        calls = 0

        def read_from_impostor_then_restore(*args, **kwargs):
            nonlocal calls
            if threading.get_ident() != test_thread or not args or Path(args[0]) != heartbeat:
                return original_reader(*args, **kwargs)
            calls += 1
            if calls != 1:
                return original_reader(*args, **kwargs)
            control.rename(displaced)
            impostor.rename(control)
            try:
                result = original_reader(*args, **kwargs)
                sampled.append(result[0])
                return result
            finally:
                control.rename(impostor)
                displaced.rename(control)

        def omit_target_guards(identities, label):
            if threading.get_ident() == test_thread and any(Path(path) == control for path, _identity in identities):
                return ()
            return original_guards(identities, label)

        with (
            mock.patch.object(
                review_proxy_module,
                "_open_windows_ancestor_guards",
                side_effect=omit_target_guards,
            ),
            mock.patch.object(
                review_proxy_module,
                "read_regular_bytes",
                side_effect=read_from_impostor_then_restore,
            ),
            self.assertRaisesRegex(DetectorDevelopmentError, "trusted directory changed") as captured,
        ):
            review_proxy_module._read_optional_json(
                heartbeat,
                control,
                "review proxy worker heartbeat",
            )

        self.assertEqual([b'{"sequence": 999}'], sampled)
        self.assertEqual("invalid_worker_protocol", captured.exception.code)
        self.assertEqual(1, calls)

    def test_worker_control_read_fails_closed_when_replacement_never_stabilizes(self) -> None:
        control = self.repo_root / "control"
        control.mkdir()
        heartbeat = control / "heartbeat.json"
        heartbeat.write_text("{}", encoding="utf-8")
        original_reader = review_proxy_module.read_regular_bytes
        test_thread = threading.get_ident()
        calls = 0
        replacement = DetectorDevelopmentError(
            "source_changed",
            "review proxy worker heartbeat changed during identity validation",
        )

        def fail_target(*args, **kwargs):
            nonlocal calls
            if threading.get_ident() != test_thread or not args or Path(args[0]) != heartbeat:
                return original_reader(*args, **kwargs)
            calls += 1
            raise replacement

        with (
            mock.patch.object(
                review_proxy_module,
                "read_regular_bytes",
                side_effect=fail_target,
            ),
            self.assertRaisesRegex(DetectorDevelopmentError, "did not stabilize"),
        ):
            review_proxy_module._read_optional_json(
                heartbeat,
                control,
                "review proxy worker heartbeat",
            )

        self.assertEqual(8, calls)

    def test_execution_deadline_stops_a_running_runner(self) -> None:
        def stalled_runner(_request, _staging, should_cancel, _progress):
            while True:
                should_cancel()
                time.sleep(0.01)

        coordinator = DetectorReviewProxyCoordinator(
            self.repo_root,
            runner=stalled_runner,
            auto_start_workers=False,
            worker_deadline_seconds=1.0,
            output_hard_limit_bytes=256 * 1024 * 1024,
            disk_reserve_bytes=0,
        )
        self.addCleanup(coordinator.close)
        created = coordinator.create_proxy(self.request())

        coordinator.execute_proxy(created["repair_id"])
        failed = coordinator.get_proxy(created["repair_id"])

        self.assertEqual("failed", failed["status"])
        self.assertEqual("review_proxy_worker_timeout", failed["error_code"])

    def test_ffmpeg_uses_an_independent_posix_process_group(self) -> None:
        process = mock.Mock()
        process.stdout = io.StringIO("progress=end\n")
        process.stderr = io.StringIO("")
        process.poll.return_value = 0
        process.returncode = 0

        with (
            mock.patch.object(review_proxy_module.os, "name", "posix"),
            mock.patch.object(review_proxy_module.subprocess, "Popen", return_value=process) as popen,
        ):
            review_proxy_module._run_ffmpeg(
                ["ffmpeg", "-progress", "pipe:1"],
                lambda: False,
            )

        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertNotIn("creationflags", popen.call_args.kwargs)

    def test_disk_preflight_uses_hard_limit_plus_reserve(self) -> None:
        coordinator = DetectorReviewProxyCoordinator(
            self.repo_root,
            runner=mock.Mock(),
            auto_start_workers=False,
            output_hard_limit_bytes=256 * 1024 * 1024,
            disk_reserve_bytes=128 * 1024 * 1024,
        )
        self.addCleanup(coordinator.close)
        created = coordinator.create_proxy(self.request())
        with mock.patch(
            "football_tracking.detector_review_proxy.shutil.disk_usage",
            return_value=mock.Mock(free=100),
        ):
            coordinator.execute_proxy(created["repair_id"])

        failed = coordinator.get_proxy(created["repair_id"])
        self.assertEqual("failed", failed["status"])
        self.assertEqual("insufficient_proxy_disk_capacity", failed["error_code"])

    def test_close_is_bounded_for_idle_coordinator(self) -> None:
        coordinator = DetectorReviewProxyCoordinator(
            self.repo_root,
            runner=mock.Mock(),
            auto_start_workers=True,
            output_hard_limit_bytes=256 * 1024 * 1024,
            disk_reserve_bytes=0,
        )
        started = time.monotonic()
        coordinator.close()
        self.assertLess(time.monotonic() - started, 2.0)


if __name__ == "__main__":
    unittest.main()
