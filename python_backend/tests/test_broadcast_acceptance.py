from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from football_tracking import broadcast_acceptance as acceptance


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _healthy_frame(frame_index: int) -> dict[str, object]:
    return {
        "frame_index": frame_index,
        "status": "ok",
        "width": 16,
        "height": 9,
        "low_information": False,
        "likely_corrupt": False,
        "reasons": [],
    }


class BroadcastAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temporary.name)
        self.source = self.run_dir / "source.mp4"
        self.output = self.run_dir / "broadcast.mp4"
        self.quality_path = self.run_dir / "broadcast_quality_report.json"
        self.render_report = self.run_dir / "render_report.json"
        self.source.write_bytes(b"source-video")
        self.output.write_bytes(b"output-video")
        self.quality = {
            "status": "ready",
            "limitations": ["source_audio_not_preserved"],
            "capabilities": {"source_audio_preserved": False},
        }
        self.quality_path.write_text(json.dumps(self.quality), encoding="utf-8")
        self.render_report.write_text(
            json.dumps(
                {
                    "source_video": {
                        "path": str(self.source.resolve()),
                        "sha256": _sha256(self.source),
                    }
                }
            ),
            encoding="utf-8",
        )
        self.real_full_decode = acceptance._full_decode_video
        self.real_ffmpeg_identity = acceptance._ffmpeg_identity
        self.full_decode = patch.object(
            acceptance,
            "_full_decode_video",
            return_value={"present": True, "duration_seconds": 1.0, "frame_count": 5},
        )
        self.full_decode_mock = self.full_decode.start()
        self.addCleanup(self.full_decode.stop)
        self.ffmpeg_identity = patch.object(
            acceptance,
            "_ffmpeg_identity",
            return_value={"path": "ffmpeg", "sha256": "f" * 64, "version": "ffmpeg version test"},
        )
        self.ffmpeg_identity.start()
        self.addCleanup(self.ffmpeg_identity.stop)
        self.opencv_identity = patch.object(
            acceptance,
            "_opencv_decoder_identity",
            return_value={"version": "test", "backend": "FFMPEG"},
        )
        self.opencv_identity.start()
        self.addCleanup(self.opencv_identity.stop)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _manifest(self) -> dict[str, object]:
        return {
            "artifacts": {
                "broadcast.mp4": {
                    "sha256": _sha256(self.output),
                    "source_report": {
                        "path": self.render_report.name,
                        "sha256": _sha256(self.render_report),
                    },
                }
            }
        }

    @staticmethod
    def _media_probe(path: Path, _executable: str) -> dict[str, object]:
        return {
            "path": str(path),
            "video": {"present": True, "duration_seconds": 1.0, "frame_count": 5},
            "audio": {"present": False, "duration_seconds": None},
        }

    @staticmethod
    def _decoded_segment(
        _path: Path,
        segment: dict[str, int],
        sample_frames: set[int],
        *,
        width: int,
        height: int,
        frame_count: int,
    ) -> dict[str, object]:
        start = segment["start_frame"]
        end = segment["end_frame_exclusive"]
        return {
            **segment,
            "status": "completed",
            "decoded_frames": end - start,
            "sample_results": {
                str(frame): _healthy_frame(frame) for frame in sorted(sample_frames) if start <= frame < end
            },
            "dimensions": {"width": width, "height": height},
        }

    def _patch_success_dependencies(self):
        return (
            patch.object(acceptance, "validate_final_broadcast_artifacts", return_value=self._manifest()),
            patch.object(
                acceptance,
                "validate_broadcast_quality_report",
                return_value=self.quality,
            ),
            patch.object(acceptance, "_resolve_ffmpeg", return_value="ffmpeg"),
            patch.object(acceptance, "_probe_media", side_effect=self._media_probe),
            patch.object(acceptance, "_video_metadata", return_value=(5, 5.0, 16, 9)),
        )

    def test_full_segment_validation_publishes_independent_report_and_checkpoint(self) -> None:
        dependencies = self._patch_success_dependencies()
        with (
            dependencies[0],
            dependencies[1],
            dependencies[2],
            dependencies[3],
            dependencies[4],
            patch.object(acceptance, "_decode_segment", side_effect=self._decoded_segment) as decode,
        ):
            report = acceptance.validate_broadcast_run(self.run_dir, segment_frames=2, resume=True)

        self.assertEqual("pass", report["status"])
        self.assertEqual(3, decode.call_count)
        self.assertEqual([0, 1, 2, 3, 4], report["frame_validation"]["sample_frames"])
        self.assertEqual(5, report["frame_validation"]["decoded_frame_count"])
        self.assertTrue((self.run_dir / acceptance.REPORT_NAME).is_file())
        checkpoint = json.loads((self.run_dir / acceptance.PROGRESS_NAME).read_text(encoding="utf-8"))
        self.assertEqual("completed", checkpoint["status"])
        self.assertEqual(report["identity"], checkpoint["identity"])

    def test_interrupt_keeps_checkpoint_and_does_not_publish_final_report_then_resumes(self) -> None:
        dependencies = self._patch_success_dependencies()
        first = {"index": 0, "start_frame": 0, "end_frame_exclusive": 2}
        (self.run_dir / acceptance.REPORT_NAME).write_text('{"status":"pass"}', encoding="utf-8")

        def interrupted_decode(*args, **kwargs):
            segment = args[1]
            if segment["index"] == 0:
                return self._decoded_segment(*args, **kwargs)
            raise KeyboardInterrupt

        with (
            dependencies[0],
            dependencies[1],
            dependencies[2],
            dependencies[3],
            dependencies[4],
            patch.object(acceptance, "_decode_segment", side_effect=interrupted_decode),
        ):
            with self.assertRaises(KeyboardInterrupt):
                acceptance.validate_broadcast_run(self.run_dir, segment_frames=2, resume=True)

        self.assertFalse((self.run_dir / acceptance.REPORT_NAME).exists())
        checkpoint = json.loads((self.run_dir / acceptance.PROGRESS_NAME).read_text(encoding="utf-8"))
        self.assertEqual("completed", checkpoint["segments"][0]["status"])
        self.assertEqual(first, {key: checkpoint["segments"][0][key] for key in first})

        dependencies = self._patch_success_dependencies()
        with (
            dependencies[0],
            dependencies[1],
            dependencies[2],
            dependencies[3],
            dependencies[4],
            patch.object(acceptance, "_decode_segment", side_effect=self._decoded_segment) as decode,
        ):
            report = acceptance.validate_broadcast_run(self.run_dir, segment_frames=2, resume=True)

        self.assertEqual("pass", report["status"])
        self.assertEqual([1, 2], [call.args[1]["index"] for call in decode.call_args_list])
        self.assertEqual(1, report["frame_validation"]["reused_segment_count"])

    def test_mismatched_checkpoint_identity_is_not_reused(self) -> None:
        progress_path = self.run_dir / acceptance.PROGRESS_NAME
        progress_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "artifact_type": "broadcast_acceptance_progress",
                    "tool_version": acceptance.TOOL_VERSION,
                    "identity": {
                        "tool_version": acceptance.TOOL_VERSION,
                        "quality_report_sha256": "0" * 64,
                        "source_video_sha256": "0" * 64,
                        "output_video_sha256": "0" * 64,
                        "segment_plan_sha256": "0" * 64,
                    },
                    "status": "in_progress",
                    "segments": [
                        {
                            "index": 0,
                            "start_frame": 0,
                            "end_frame_exclusive": 2,
                            "decoded_frames": 2,
                            "status": "completed",
                            "sample_results": {"0": _healthy_frame(0)},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        dependencies = self._patch_success_dependencies()
        with (
            dependencies[0],
            dependencies[1],
            dependencies[2],
            dependencies[3],
            dependencies[4],
            patch.object(acceptance, "_decode_segment", side_effect=self._decoded_segment) as decode,
        ):
            report = acceptance.validate_broadcast_run(self.run_dir, segment_frames=2, resume=True)

        self.assertEqual("pass", report["status"])
        self.assertEqual([0, 1, 2], [call.args[1]["index"] for call in decode.call_args_list])
        self.assertEqual(0, report["frame_validation"]["reused_segment_count"])

    def test_matching_checkpoint_is_ignored_without_resume(self) -> None:
        dependencies = self._patch_success_dependencies()
        with (
            dependencies[0],
            dependencies[1],
            dependencies[2],
            dependencies[3],
            dependencies[4],
            patch.object(acceptance, "_decode_segment", side_effect=self._decoded_segment),
        ):
            acceptance.validate_broadcast_run(self.run_dir, segment_frames=2, resume=True)

        dependencies = self._patch_success_dependencies()
        with (
            dependencies[0],
            dependencies[1],
            dependencies[2],
            dependencies[3],
            dependencies[4],
            patch.object(acceptance, "_decode_segment", side_effect=self._decoded_segment) as decode,
        ):
            report = acceptance.validate_broadcast_run(self.run_dir, segment_frames=2)

        self.assertEqual([0, 1, 2], [call.args[1]["index"] for call in decode.call_args_list])
        self.assertFalse(report["frame_validation"]["resume_requested"])
        self.assertEqual(0, report["frame_validation"]["reused_segment_count"])

    def test_all_reused_segments_still_require_one_strict_full_decode(self) -> None:
        dependencies = self._patch_success_dependencies()
        with (
            dependencies[0],
            dependencies[1],
            dependencies[2],
            dependencies[3],
            dependencies[4],
            patch.object(acceptance, "_decode_segment", side_effect=self._decoded_segment),
        ):
            acceptance.validate_broadcast_run(self.run_dir, segment_frames=2, resume=True)

        self.full_decode_mock.reset_mock()
        dependencies = self._patch_success_dependencies()
        with (
            dependencies[0],
            dependencies[1],
            dependencies[2],
            dependencies[3],
            dependencies[4],
            patch.object(acceptance, "_decode_segment", side_effect=self._decoded_segment) as segment_decode,
        ):
            report = acceptance.validate_broadcast_run(self.run_dir, segment_frames=2, resume=True)

        self.assertEqual("pass", report["status"])
        segment_decode.assert_not_called()
        self.full_decode_mock.assert_called_once()

    def test_strict_full_decode_frame_count_mismatch_blocks_pass(self) -> None:
        self.full_decode_mock.return_value = {"present": True, "duration_seconds": 1.0, "frame_count": 4}
        dependencies = self._patch_success_dependencies()
        with (
            dependencies[0],
            dependencies[1],
            dependencies[2],
            dependencies[3],
            dependencies[4],
            patch.object(acceptance, "_decode_segment", side_effect=self._decoded_segment),
        ):
            report = acceptance.validate_broadcast_run(self.run_dir, segment_frames=2)

        self.assertEqual("fail", report["status"])
        self.assertIn("strict ffmpeg decode frame count", report["errors"][0]["message"])

    def test_stale_lineage_fails_before_media_probe(self) -> None:
        with (
            patch.object(
                acceptance,
                "validate_final_broadcast_artifacts",
                side_effect=RuntimeError("stale final binding"),
            ),
            patch.object(acceptance, "_probe_media") as probe,
        ):
            report = acceptance.validate_broadcast_run(self.run_dir)

        self.assertEqual("fail", report["status"])
        self.assertEqual("lineage_validation_failed", report["errors"][0]["code"])
        probe.assert_not_called()
        self.assertTrue((self.run_dir / acceptance.REPORT_NAME).is_file())

    def test_lineage_interrupt_removes_an_old_pass_before_expensive_validation(self) -> None:
        report_path = self.run_dir / acceptance.REPORT_NAME
        report_path.write_text('{"status":"pass"}', encoding="utf-8")
        with patch.object(acceptance, "validate_final_broadcast_artifacts", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                acceptance.validate_broadcast_run(self.run_dir)

        self.assertFalse(report_path.exists())

    def test_lineage_is_revalidated_after_media_decode(self) -> None:
        dependencies = self._patch_success_dependencies()
        with (
            patch.object(
                acceptance,
                "validate_final_broadcast_artifacts",
                side_effect=[self._manifest(), RuntimeError("final binding changed")],
            ),
            dependencies[1],
            dependencies[2],
            dependencies[3],
            dependencies[4],
            patch.object(acceptance, "_decode_segment", side_effect=self._decoded_segment),
        ):
            report = acceptance.validate_broadcast_run(self.run_dir, segment_frames=2)

        self.assertEqual("fail", report["status"])
        self.assertEqual("acceptance_validation_failed", report["errors"][0]["code"])

    def test_concurrent_writer_fails_without_replacing_existing_report(self) -> None:
        report_path = self.run_dir / acceptance.REPORT_NAME
        report_path.write_text('{"status":"pass"}', encoding="utf-8")
        with patch.object(
            acceptance,
            "_acquire_acceptance_lock",
            side_effect=acceptance.BroadcastAcceptanceError("busy"),
        ):
            report = acceptance.validate_broadcast_run(self.run_dir)

        self.assertEqual("unavailable", report["status"])
        self.assertEqual("acceptance_writer_busy", report["errors"][0]["code"])
        self.assertEqual({"status": "pass"}, json.loads(report_path.read_text(encoding="utf-8")))

    def test_symlink_run_alias_never_writes_to_the_target(self) -> None:
        alias = self.run_dir.parent / f"{self.run_dir.name}-alias"
        try:
            alias.symlink_to(self.run_dir, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks are unavailable: {exc}")
        self.addCleanup(alias.unlink)
        report_path = self.run_dir / acceptance.REPORT_NAME
        report_path.write_text('{"status":"pass"}', encoding="utf-8")

        report = acceptance.validate_broadcast_run(alias)

        self.assertEqual("fail", report["status"])
        self.assertEqual("invalid_run_directory", report["errors"][0]["code"])
        self.assertEqual({"status": "pass"}, json.loads(report_path.read_text(encoding="utf-8")))
        self.assertEqual([], list(self.run_dir.glob(".broadcast-acceptance-*.lock")))

    def test_replaced_run_directory_never_receives_old_writer_results(self) -> None:
        moved = self.run_dir.with_name(f"{self.run_dir.name}-moved")

        def replace_after_decode(*args, **kwargs):
            completed = self._decoded_segment(*args, **kwargs)
            if args[1]["index"] == 0:
                self.run_dir.rename(moved)
                self.run_dir.mkdir()
            return completed

        dependencies = self._patch_success_dependencies()
        try:
            with (
                dependencies[0],
                dependencies[1],
                dependencies[2],
                dependencies[3],
                dependencies[4],
                patch.object(acceptance, "_decode_segment", side_effect=replace_after_decode),
            ):
                with self.assertRaises(acceptance.BroadcastAcceptanceError):
                    acceptance.validate_broadcast_run(self.run_dir, segment_frames=2)

            self.assertFalse((self.run_dir / acceptance.REPORT_NAME).exists())
            self.assertFalse((self.run_dir / acceptance.PROGRESS_NAME).exists())
        finally:
            if self.run_dir.exists():
                shutil.rmtree(self.run_dir)
            if moved.exists():
                moved.rename(self.run_dir)

    def test_quality_audio_capability_must_match_declared_limitation(self) -> None:
        self.quality["capabilities"] = {"source_audio_preserved": True}
        self.quality_path.write_text(json.dumps(self.quality), encoding="utf-8")
        with (
            patch.object(acceptance, "validate_final_broadcast_artifacts", return_value=self._manifest()),
            patch.object(
                acceptance,
                "validate_broadcast_quality_report",
                return_value=self.quality,
            ),
        ):
            report = acceptance.validate_broadcast_run(self.run_dir)

        self.assertEqual("fail", report["status"])
        self.assertEqual("lineage_validation_failed", report["errors"][0]["code"])

    def test_duration_and_audio_policy_handles_known_limitation_and_mismatches(self) -> None:
        source = {
            "video": {"present": True, "duration_seconds": 10.0},
            "audio": {"present": True, "duration_seconds": 10.0},
        }
        silent_output = {
            "video": {"present": True, "duration_seconds": 10.05},
            "audio": {"present": False, "duration_seconds": None},
        }
        accepted = acceptance._evaluate_durations(
            source, silent_output, fps=20.0, limitations=["source_audio_not_preserved"]
        )
        self.assertEqual("known_limitation", accepted["status"])
        self.assertEqual([], accepted["errors"])

        rejected = acceptance._evaluate_durations(source, silent_output, fps=20.0, limitations=[])
        self.assertEqual("fail", rejected["status"])
        self.assertIn("source_audio_missing_from_output", {error["code"] for error in rejected["errors"]})

        unexpected_audio = acceptance._evaluate_durations(
            {**source, "audio": {"present": False, "duration_seconds": None}},
            {**silent_output, "audio": {"present": True, "duration_seconds": 10.0}},
            fps=20.0,
            limitations=[],
        )
        self.assertIn("unexpected_output_audio", {error["code"] for error in unexpected_audio["errors"]})

        bad_duration = acceptance._evaluate_durations(
            source,
            {"video": {"present": True, "duration_seconds": 10.2}, "audio": source["audio"]},
            fps=20.0,
            limitations=[],
        )
        self.assertIn("video_duration_mismatch", {error["code"] for error in bad_duration["errors"]})

    def test_copy_probe_allows_missing_ffmpeg_frame_progress_but_full_decode_requires_it(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["ffmpeg"],
            returncode=0,
            stdout="out_time_us=1000000\nprogress=end\n",
            stderr="",
        )
        with patch.object(acceptance.subprocess, "run", return_value=completed):
            copied = acceptance._probe_stream(
                self.output,
                "ffmpeg",
                stream="video",
                required=True,
                copy_stream=True,
            )
            with self.assertRaisesRegex(acceptance.BroadcastAcceptanceError, "frame count is unavailable"):
                acceptance._probe_stream(
                    self.output,
                    "ffmpeg",
                    stream="video",
                    required=True,
                    copy_stream=False,
                )

        self.assertIsNone(copied["frame_count"])

    def test_media_probe_uses_decoder_frame_count_when_copy_progress_omits_it(self) -> None:
        with (
            patch.object(
                acceptance,
                "_probe_stream",
                side_effect=[
                    {"present": True, "duration_seconds": 1.95, "frame_count": None},
                    {"present": False, "duration_seconds": None, "frame_count": None},
                ],
            ),
            patch.object(acceptance, "_video_metadata", return_value=(40, 20.0, 160, 90)),
        ):
            probe = acceptance._probe_media(self.output, "ffmpeg")

        self.assertEqual(40, probe["video"]["frame_count"])
        self.assertIsNone(probe["video"]["ffmpeg_progress_frame_count"])
        self.assertEqual(2.0, probe["video"]["duration_seconds"])

    def test_real_ffmpeg_video_duration_uses_full_frame_extent(self) -> None:
        executable = acceptance._resolve_ffmpeg(None)
        ffmpeg_identity = self.real_ffmpeg_identity(executable)
        source = self.run_dir / "timed-source.mp4"
        output = self.run_dir / "timed-output.mp4"
        common_input = [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=20:duration=2",
        ]
        subprocess.run(
            [
                *common_input,
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=1000:sample_rate=48000:duration=2",
                "-shortest",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-y",
                str(source),
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [*common_input, "-c:v", "libx264", "-an", "-y", str(output)],
            check=True,
            capture_output=True,
        )

        source_probe = acceptance._probe_media(source, executable)
        output_probe = acceptance._probe_media(output, executable)
        result = acceptance._evaluate_durations(
            source_probe,
            output_probe,
            fps=20.0,
            limitations=["source_audio_not_preserved"],
        )

        self.assertEqual(40, source_probe["video"]["frame_count"])
        self.assertEqual(_sha256(Path(executable)), ffmpeg_identity["sha256"])
        self.assertTrue(ffmpeg_identity["version"].startswith("ffmpeg version "))
        self.assertEqual(2.0, source_probe["video"]["duration_seconds"])
        self.assertEqual("known_limitation", result["status"])
        self.assertEqual([], result["errors"])
        strict = self.real_full_decode(output, executable)
        self.assertEqual(40, strict["frame_count"])

    def test_strict_ffmpeg_decode_rejects_corrupt_h264_payload(self) -> None:
        executable = acceptance._resolve_ffmpeg(None)
        healthy = self.run_dir / "healthy-h264.mp4"
        corrupt = self.run_dir / "corrupt-h264.mp4"
        subprocess.run(
            [
                executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x180:rate=20:duration=5",
                "-c:v",
                "libx264",
                "-g",
                "20",
                "-an",
                "-y",
                str(healthy),
            ],
            check=True,
            capture_output=True,
        )
        import cv2

        healthy_payload = healthy.read_bytes()
        found_masked_corruption = False
        for step in range(26):
            payload = bytearray(healthy_payload)
            offset = int(len(payload) * (0.45 + step * 0.002)) + 256
            if offset >= len(payload):
                continue
            payload[offset] ^= 0x80
            corrupt.write_bytes(payload)
            capture = cv2.VideoCapture(str(corrupt))
            decoded = 0
            try:
                while True:
                    ok, frame = capture.read()
                    if not ok:
                        break
                    self.assertIsNotNone(frame)
                    decoded += 1
            finally:
                capture.release()
            if decoded != 100:
                continue
            try:
                self.real_full_decode(corrupt, executable)
            except acceptance.BroadcastAcceptanceError:
                found_masked_corruption = True
                break

        self.assertTrue(found_masked_corruption, "could not construct a decoder-masked H.264 corruption")

        with self.assertRaises(acceptance.BroadcastAcceptanceError):
            self.real_full_decode(corrupt, executable)

    def test_atomic_json_failure_leaves_no_partial_file(self) -> None:
        path = self.run_dir / "atomic.json"
        with patch.object(acceptance.os, "replace", side_effect=OSError("injected replace failure")):
            with self.assertRaises(OSError):
                acceptance._atomic_write_json(path, {"status": "pass"})
        self.assertFalse(path.exists())
        self.assertEqual([], list(self.run_dir.glob(".atomic.json.*.tmp")))


class BroadcastAcceptanceCliTests(unittest.TestCase):
    def test_cli_prints_one_json_value_and_uses_status_exit_codes(self) -> None:
        from scripts import validate_broadcast_run as cli

        for status, expected in (("pass", 0), ("fail", 1), ("unavailable", 1)):
            stdout = io.StringIO()
            with patch.object(cli, "validate_broadcast_run", return_value={"status": status}), redirect_stdout(stdout):
                code = cli.main(["--run-dir", "."])
            self.assertEqual(expected, code)
            self.assertEqual({"status": status}, json.loads(stdout.getvalue()))
            self.assertEqual(1, len(stdout.getvalue().splitlines()))

    def test_cli_accepts_resume_flag(self) -> None:
        from scripts import validate_broadcast_run as cli

        stdout = io.StringIO()
        with (
            patch.object(cli, "validate_broadcast_run", return_value={"status": "pass"}) as validate,
            redirect_stdout(stdout),
        ):
            code = cli.main(["--run-dir", ".", "--resume"])

        self.assertEqual(0, code)
        self.assertTrue(validate.call_args.kwargs["resume"])

    def test_cli_keyboard_interrupt_returns_130_without_traceback(self) -> None:
        from scripts import validate_broadcast_run as cli

        stdout = io.StringIO()
        with patch.object(cli, "validate_broadcast_run", side_effect=KeyboardInterrupt), redirect_stdout(stdout):
            code = cli.main(["--run-dir", "."])
        self.assertEqual(130, code)
        self.assertEqual({"status": "unavailable", "error": "interrupted"}, json.loads(stdout.getvalue()))


if __name__ == "__main__":
    unittest.main()
