from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from concurrent.futures import CancelledError
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from football_tracking.chunk_runner import (
    build_chunk_config,
    build_high_recall_window_config,
    effective_worker_count,
    enforce_raw_chunk_config,
    run_chunk,
    run_high_recall_chunk,
    run_high_recall_windows,
    run_temporal_chunks,
    write_chunk_config,
    write_high_recall_window_config,
)
from football_tracking.config import (
    AppConfig,
    DetectorConfig,
    FilteringConfig,
    FollowCamConfig,
    LoggingConfig,
    MockConfig,
    OutputConfig,
    PostprocessConfig,
    RuntimeConfig,
    SahiConfig,
    SceneBiasConfig,
    SelectionConfig,
    TemporalChunkConfig,
    TrackingConfig,
    load_config,
)
from football_tracking.detector_candidate_contract import (
    CandidateSourceChangedError,
    assign_candidate_ids,
    candidate_to_contract_record,
    capture_candidate_source_snapshot,
)
from football_tracking.temporal_chunks import TemporalChunk
from football_tracking.tracking_contracts import write_tracking_contract
from football_tracking.types import Candidate


def load_backend_main_module():
    module_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("backend_main_for_test", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load backend main module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_app_config(base_dir: Path) -> AppConfig:
    input_video = base_dir / "data" / "input.mp4"
    input_video.parent.mkdir(parents=True, exist_ok=True)
    input_video.write_bytes(b"chunk-runner-test-video")
    return AppConfig(
        input_video=input_video,
        output_dir=base_dir / "outputs" / "source",
        logging=LoggingConfig(level="DEBUG", save_debug_jsonl=True),
        detector=DetectorConfig(
            model_path=base_dir / "weights" / "football_ball_yolo.pt",
            device="cpu",
            inference_mode="sahi",
            use_half=False,
        ),
        sahi=SahiConfig(),
        filtering=FilteringConfig(),
        scene_bias=SceneBiasConfig(),
        selection=SelectionConfig(),
        tracking=TrackingConfig(kalman_enabled=False),
        output=OutputConfig(
            save_video=False,
            save_frames=False,
            save_csv=True,
            save_debug_jsonl=True,
        ),
        postprocess=PostprocessConfig(enabled=True),
        follow_cam=FollowCamConfig(enabled=True),
        runtime=RuntimeConfig(use_gpu_if_available=False, start_frame=17, max_frames=200),
        mock=MockConfig(enabled=True),
        temporal_chunks=TemporalChunkConfig(
            enabled=True,
            chunk_frames=100,
            overlap_frames=10,
            decode_preroll_frames=5,
        ),
    )


def make_chunk() -> TemporalChunk:
    return TemporalChunk(
        index=1,
        decode_start_frame=80,
        start_frame=90,
        end_frame=149,
        core_start_frame=100,
        core_end_frame=139,
        output_dir_name="chunk_0001",
    )


def make_runner_chunks(count: int = 2) -> list[TemporalChunk]:
    chunks: list[TemporalChunk] = []
    for index in range(count):
        core_start = index * 5
        core_end = core_start + 4
        start = max(0, core_start - 1)
        end = core_end + 1
        chunks.append(
            TemporalChunk(
                index=index,
                decode_start_frame=start,
                start_frame=start,
                end_frame=end,
                core_start_frame=core_start,
                core_end_frame=core_end,
                output_dir_name=f"chunk_{index:04d}",
            )
        )
    return chunks


def write_raw_outputs_for_chunk_config(config_path: Path) -> None:
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    output_dir = Path(payload["output_dir"])
    start_frame = int(payload["runtime"]["start_frame"])
    max_frames = int(payload["runtime"]["max_frames"])
    candidate_source_sha256 = payload["runtime"]["candidate_source_sha256"]
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "ball_track.csv").open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["Frame", "X", "Y", "Confidence", "Status"])
        for frame in range(start_frame, start_frame + max_frames):
            writer.writerow([frame, frame + 1, frame + 2, "0.90", "DETECTED"])
    with (output_dir / "debug.jsonl").open("w", encoding="utf-8") as debug_file:
        for frame in range(start_frame, start_frame + max_frames):
            debug_file.write(json.dumps({"frame": frame, "source": output_dir.name}) + "\n")
    write_tracking_contract(
        output_dir,
        source={"video_sha256": candidate_source_sha256},
        frames=[
            {
                "frame_index": frame,
                "status": "detected",
                "x": frame + 1,
                "y": frame + 2,
                "confidence": 0.9,
            }
            for frame in range(start_frame, start_frame + max_frames)
        ],
        candidates=[
            candidate_to_contract_record(candidate)
            for frame in range(start_frame, start_frame + max_frames)
            for candidate in assign_candidate_ids(
                [
                    Candidate(
                        frame_index=frame,
                        x1=frame + 1,
                        y1=frame + 2,
                        x2=frame + 2,
                        y2=frame + 3,
                        confidence=0.9,
                        source="test_detector",
                    )
                ],
                candidate_source_sha256,
            )
        ],
    )


class SuccessfulChunkPopen:
    instances: list["SuccessfulChunkPopen"] = []

    def __init__(self, command: list[str], **kwargs: object) -> None:
        self.command = list(command)
        self.kwargs = kwargs
        self.config_path = Path(self.command[self.command.index("--config") + 1])
        self.returncode: int | None = None
        self.terminated = False
        self._poll_count = 0
        write_raw_outputs_for_chunk_config(self.config_path)
        stdout = kwargs.get("stdout")
        if stdout is not None and hasattr(stdout, "write"):
            stdout.write(f"finished {self.config_path.name}")
            stdout.flush()
        self.instances.append(self)

    def poll(self) -> int | None:
        self._poll_count += 1
        if self._poll_count < 2:
            return None
        self.returncode = 0
        return self.returncode

    def communicate(self) -> tuple[str, str]:
        if self.returncode is None:
            self.returncode = 0
        return ("", "")

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


class FailingFirstChunkPopen:
    instances: list["FailingFirstChunkPopen"] = []

    def __init__(self, command: list[str], **kwargs: object) -> None:
        self.command = list(command)
        self.kwargs = kwargs
        self.config_path = Path(self.command[self.command.index("--config") + 1])
        self.returncode: int | None = None
        self.terminated = False
        self.is_failure = "chunk_0000" in self.config_path.parts
        if self.is_failure:
            stderr = kwargs.get("stderr")
            if stderr is not None and hasattr(stderr, "write"):
                stderr.write("worker failed")
                stderr.flush()
        self.instances.append(self)

    def poll(self) -> int | None:
        if self.is_failure:
            self.returncode = 7
            return self.returncode
        return self.returncode

    def communicate(self) -> tuple[str, str]:
        if self.is_failure:
            self.returncode = 7
            return ("", "")
        if self.returncode is None:
            self.returncode = -15 if self.terminated else 0
        return ("", "")

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = -15 if self.terminated else 0
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


class FailingFirstCompletedSecondChunkPopen:
    instances: list["FailingFirstCompletedSecondChunkPopen"] = []

    def __init__(self, command: list[str], **kwargs: object) -> None:
        self.command = list(command)
        self.kwargs = kwargs
        self.config_path = Path(self.command[self.command.index("--config") + 1])
        self.returncode: int | None = None
        self.terminated = False
        self.is_failure = "chunk_0000" in self.config_path.parts
        self.instances.append(self)

    def poll(self) -> int | None:
        self.returncode = 7 if self.is_failure else 0
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = -15 if self.terminated else 0
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


class RunningChunkPopen:
    instances: list["RunningChunkPopen"] = []

    def __init__(self, command: list[str], **kwargs: object) -> None:
        self.command = list(command)
        self.kwargs = kwargs
        self.config_path = Path(self.command[self.command.index("--config") + 1])
        self.returncode: int | None = None
        self.terminated = False
        self.instances.append(self)

    def poll(self) -> int | None:
        return self.returncode

    def communicate(self) -> tuple[str, str]:
        if self.returncode is None:
            self.returncode = -15 if self.terminated else 0
        return ("", "")

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = -15 if self.terminated else 0
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


class StartFailingChunkPopen:
    instances: list["StartFailingChunkPopen"] = []

    def __init__(self, command: list[str], **kwargs: object) -> None:
        self.command = list(command)
        self.kwargs = kwargs
        self.instances.append(self)
        raise OSError("cannot start worker")


class ChunkRunnerSchedulerTests(unittest.TestCase):
    def test_effective_worker_count_caps_single_gpu_without_oversubscription(self) -> None:
        self.assertEqual(
            1,
            effective_worker_count(
                requested=4,
                detector_device="cuda:0",
                devices=(),
                allow_gpu_oversubscription=False,
            ),
        )

    def test_effective_worker_count_allows_cpu_parallelism(self) -> None:
        self.assertEqual(
            4,
            effective_worker_count(
                requested=4,
                detector_device="cpu",
                devices=(),
                allow_gpu_oversubscription=False,
            ),
        )

    def test_effective_worker_count_uses_multi_gpu_device_list(self) -> None:
        self.assertEqual(
            2,
            effective_worker_count(
                requested=4,
                detector_device="cuda:0",
                devices=("cuda:0", "cuda:1"),
                allow_gpu_oversubscription=False,
            ),
        )

    def test_effective_worker_count_allows_oversubscription_and_normalizes_zero_or_negative(self) -> None:
        self.assertEqual(
            4,
            effective_worker_count(
                requested=4,
                detector_device="cuda:0",
                devices=(),
                allow_gpu_oversubscription=True,
            ),
        )
        self.assertEqual(
            1,
            effective_worker_count(
                requested=0,
                detector_device="cpu",
                devices=(),
                allow_gpu_oversubscription=False,
            ),
        )
        self.assertEqual(
            1,
            effective_worker_count(
                requested=-3,
                detector_device="cuda:0",
                devices=("cuda:0", "cuda:1"),
                allow_gpu_oversubscription=False,
            ),
        )


class ChunkRunnerConfigTests(unittest.TestCase):
    def test_build_chunk_config_uses_decode_range_and_preserves_original_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            chunk = make_chunk()
            chunk_output_dir = base_dir / "chunks" / chunk.output_dir_name

            chunk_config = build_chunk_config(config, chunk, chunk_output_dir)

        self.assertIsNot(config, chunk_config)
        self.assertIsNot(config.runtime, chunk_config.runtime)
        self.assertEqual(chunk_output_dir.resolve(), chunk_config.output_dir)
        self.assertEqual(chunk.decode_start_frame, chunk_config.runtime.start_frame)
        self.assertEqual(chunk.end_frame - chunk.decode_start_frame + 1, chunk_config.runtime.max_frames)
        self.assertFalse(chunk_config.postprocess.enabled)
        self.assertFalse(chunk_config.follow_cam.enabled)
        self.assertEqual("direct_full_frame", chunk_config.detector.inference_mode)
        self.assertFalse(chunk_config.temporal_chunks.enabled)
        self.assertTrue(chunk_config.output.save_csv)
        self.assertTrue(chunk_config.output.save_debug_jsonl)
        self.assertTrue(chunk_config.logging.save_debug_jsonl)

        self.assertEqual(base_dir / "outputs" / "source", config.output_dir)
        self.assertEqual(17, config.runtime.start_frame)
        self.assertEqual(200, config.runtime.max_frames)
        self.assertTrue(config.postprocess.enabled)
        self.assertTrue(config.follow_cam.enabled)
        self.assertEqual("sahi", config.detector.inference_mode)
        self.assertTrue(config.temporal_chunks.enabled)

    def test_enforce_raw_chunk_config_applies_worker_boundary_guarantees(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            config = make_app_config(Path(temp_name))

            enforced = enforce_raw_chunk_config(config)

        self.assertIs(config, enforced)
        self.assertFalse(enforced.postprocess.enabled)
        self.assertFalse(enforced.follow_cam.enabled)
        self.assertEqual("direct_full_frame", enforced.detector.inference_mode)
        self.assertFalse(enforced.temporal_chunks.enabled)
        self.assertTrue(enforced.output.save_csv)
        self.assertTrue(enforced.output.save_debug_jsonl)
        self.assertTrue(enforced.logging.save_debug_jsonl)

    def test_write_chunk_config_writes_yaml_that_loads_back_as_raw_only_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            chunk = make_chunk()
            chunks_root = base_dir / "outputs" / "source" / "chunks"

            config_path = write_chunk_config(config, chunk, chunks_root)

            expected_path = chunks_root / chunk.output_dir_name / "chunk_config.yaml"
            self.assertEqual(expected_path, config_path)
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            for required_key in (
                "input_video",
                "output_dir",
                "detector",
                "runtime",
                "postprocess",
                "follow_cam",
                "output",
                "logging",
                "mock",
            ):
                self.assertIn(required_key, payload)
            self.assertIsInstance(payload["input_video"], str)
            self.assertIsInstance(payload["output_dir"], str)
            self.assertFalse(payload["temporal_chunks"]["enabled"])

            loaded = load_config(config_path)

        self.assertEqual(expected_path.parent.resolve(), loaded.output_dir)
        self.assertEqual(chunk.decode_start_frame, loaded.runtime.start_frame)
        self.assertEqual(chunk.end_frame - chunk.decode_start_frame + 1, loaded.runtime.max_frames)
        self.assertFalse(loaded.postprocess.enabled)
        self.assertFalse(loaded.follow_cam.enabled)
        self.assertEqual("direct_full_frame", loaded.detector.inference_mode)
        self.assertFalse(loaded.temporal_chunks.enabled)
        self.assertTrue(loaded.output.save_csv)
        self.assertTrue(loaded.output.save_debug_jsonl)
        self.assertTrue(loaded.logging.save_debug_jsonl)

    def test_high_recall_window_config_writes_effective_roi_to_child_yaml_without_mutating_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.detector.inference_mode = "direct_full_frame"
            window = {
                "start_frame": 10,
                "end_frame": 20,
                "approved_action": "targeted_rerun",
                "approved_roi": [120, 40, 200, 90],
                "padded_roi": [88, 8, 232, 122],
                "effective_roi": [88, 8, 232, 122],
                "mode": "sahi",
            }

            config_path = write_high_recall_window_config(
                config,
                window,
                0,
                base_dir / "outputs" / "source" / "high_recall_windows",
            )
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        self.assertEqual([88, 8, 232, 122], payload["filtering"]["roi"])
        self.assertEqual([88, 8, 232, 122], window["effective_roi"])
        self.assertEqual("sahi_roi", window["sahi_policy"])
        self.assertIsNone(config.filtering.roi)

    def test_high_recall_window_config_accepts_canonical_rerun_ball_window_roi(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            window = {
                "start_frame": 10,
                "end_frame": 20,
                "approved_action": "rerun_ball_window",
                "approved_roi": [120, 40, 200, 90],
                "padded_roi": [88, 8, 232, 122],
                "effective_roi": [88, 8, 232, 122],
                "mode": "sahi",
            }

            config_path = write_high_recall_window_config(
                config,
                window,
                0,
                base_dir / "outputs" / "source" / "high_recall_windows",
            )
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        self.assertEqual([88, 8, 232, 122], payload["filtering"]["roi"])
        self.assertEqual("sahi_roi", window["sahi_policy"])

    def test_high_recall_roi_padding_clamps_to_known_mock_frame_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.mock.frame_width = 40
            config.mock.frame_height = 30
            window = {
                "start_frame": 10,
                "end_frame": 20,
                "approved_action": "targeted_rerun",
                "approved_roi": [5, 6, 20, 18],
                "mode": "sahi",
            }

            window_config = build_high_recall_window_config(
                config,
                window,
                base_dir / "outputs" / "source" / "high_recall_windows" / "window_000",
            )

        self.assertEqual((0, 0, 40, 30), window_config.filtering.roi)
        self.assertEqual([0, 0, 40, 30], window["effective_roi"])

    def test_high_recall_window_config_intersects_base_filtering_roi_with_ai_roi(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.filtering.roi = (100, 50, 160, 110)
            window = {
                "start_frame": 10,
                "end_frame": 20,
                "approved_action": "targeted_rerun",
                "approved_roi": [120, 40, 200, 90],
                "mode": "sahi",
            }

            window_config = build_high_recall_window_config(
                config,
                window,
                base_dir / "outputs" / "source" / "high_recall_windows" / "window_000",
            )

        self.assertEqual((100, 50, 160, 110), window_config.filtering.roi)
        self.assertEqual([100, 50, 160, 110], window["effective_roi"])
        self.assertEqual((100, 50, 160, 110), config.filtering.roi)

    def test_high_recall_localize_ball_roi_clamps_right_bottom_child_roi(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.mock.frame_width = 5760
            config.mock.frame_height = 1440
            window = {
                "start_frame": 2049,
                "end_frame": 2544,
                "approved_action": "localize_ball_roi",
                "local_search_roi": {
                    "coordinate_space": "image",
                    "frame": 2079,
                    "x": 5700,
                    "y": 1390,
                    "width": 120,
                    "height": 80,
                    "confidence": 0.74,
                },
                "mode": "sahi",
            }

            window_config = build_high_recall_window_config(
                config,
                window,
                base_dir / "outputs" / "source" / "high_recall_windows" / "window_000",
            )

        self.assertEqual((5668, 1358, 5760, 1440), window_config.filtering.roi)
        self.assertEqual([5668, 1358, 5760, 1440], window["effective_roi"])
        self.assertEqual("sahi_roi", window["sahi_policy"])
        self.assertEqual("executable", window["execution_status"])

    def test_high_recall_localize_ball_roi_empty_base_intersection_needs_manual_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.filtering.roi = (300, 300, 320, 320)
            window = {
                "start_frame": 10,
                "end_frame": 20,
                "approved_action": "localize_ball_roi",
                "local_search_roi": {
                    "coordinate_space": "image",
                    "frame": 15,
                    "x": 20,
                    "y": 20,
                    "width": 20,
                    "height": 20,
                    "confidence": 0.74,
                },
                "mode": "sahi",
            }

            with self.assertRaisesRegex(ValueError, "High-recall window is not executable"):
                build_high_recall_window_config(
                    config,
                    window,
                    base_dir / "outputs" / "source" / "high_recall_windows" / "window_000",
                )

        self.assertEqual("needs_manual_resolution", window["execution_status"])
        self.assertEqual("blocked_empty_roi_intersection", window["sahi_policy"])

    def test_high_recall_empty_roi_intersection_marks_manual_resolution_and_does_not_run_sahi(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.output_dir = base_dir / "outputs" / "source"
            config.filtering.roi = (300, 300, 320, 320)
            config.high_recall_windows.enabled = True
            config.high_recall_windows.mode = "sahi"
            window = {
                "start_frame": 10,
                "end_frame": 20,
                "approved_action": "targeted_rerun",
                "approved_roi": [20, 20, 40, 40],
                "mode": "sahi",
            }
            window_report = {"windows": [window], "summary": {"selected_window_count": 1}}

            with (
                patch("football_tracking.chunk_runner.write_ball_audit_report", return_value={}),
                patch("football_tracking.chunk_runner.write_ai_review_trigger_report", return_value={}),
                patch("football_tracking.chunk_runner.write_event_candidate_report", return_value={}),
                patch("football_tracking.chunk_runner.write_high_recall_window_report", return_value=window_report),
                patch("football_tracking.chunk_runner.write_high_recall_window_config") as write_config,
                patch("football_tracking.chunk_runner.run_high_recall_chunk") as run_chunk_mock,
                patch("football_tracking.chunk_runner.reconcile_high_recall_outputs") as reconcile,
            ):
                report = run_high_recall_windows(config, source_total_frames=100)

        write_config.assert_not_called()
        run_chunk_mock.assert_not_called()
        reconcile.assert_not_called()
        blocked = report["windows"][0]
        self.assertEqual("needs_manual_resolution", blocked["execution_status"])
        self.assertEqual("blocked_empty_roi_intersection", blocked["sahi_policy"])
        self.assertEqual("skipped", report["execution"]["status"])

    def test_high_recall_skipped_window_does_not_shift_executable_window_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.output_dir = base_dir / "outputs" / "source"
            config.output_dir.mkdir(parents=True, exist_ok=True)
            baseline_contract = config.output_dir / "tracking_contract.v2.json"
            baseline_contract.write_bytes(b"baseline-contract")
            config.filtering.roi = (300, 300, 360, 360)
            config.high_recall_windows.enabled = True
            config.high_recall_windows.mode = "sahi"
            blocked_window = {
                "start_frame": 10,
                "end_frame": 20,
                "approved_action": "targeted_rerun",
                "approved_roi": [20, 20, 40, 40],
                "mode": "sahi",
            }
            executable_window = {
                "start_frame": 30,
                "end_frame": 35,
                "approved_action": "targeted_rerun",
                "approved_roi": [310, 310, 330, 330],
                "mode": "sahi",
            }
            window_report = {
                "windows": [blocked_window, executable_window],
                "summary": {"selected_window_count": 2},
            }

            def fake_write_config(config_arg, window_arg, window_index, root_arg):
                window_dir = root_arg / f"window_{window_index:03d}"
                window_dir.mkdir(parents=True, exist_ok=True)
                config_path = window_dir / "chunk_config.yaml"
                config_path.write_text("mock: true\n", encoding="utf-8")
                return config_path

            with (
                patch("football_tracking.chunk_runner.write_ball_audit_report", return_value={}),
                patch("football_tracking.chunk_runner.write_ai_review_trigger_report", return_value={}),
                patch("football_tracking.chunk_runner.write_event_candidate_report", return_value={}),
                patch("football_tracking.chunk_runner.write_high_recall_window_report", return_value=window_report),
                patch(
                    "football_tracking.chunk_runner.write_high_recall_window_config",
                    side_effect=fake_write_config,
                ) as write_config,
                patch("football_tracking.chunk_runner.run_high_recall_chunk", return_value=0),
                patch(
                    "football_tracking.chunk_runner.reconcile_high_recall_outputs",
                    return_value={"summary": {"accepted_count": 0}},
                ) as reconcile,
            ):
                report = run_high_recall_windows(config, source_total_frames=100)
            baseline_contract_bytes = baseline_contract.read_bytes()

        write_config.assert_called_once()
        self.assertEqual(0, write_config.call_args.args[2])
        self.assertEqual("needs_manual_resolution", report["windows"][0]["execution_status"])
        self.assertEqual(1, len(reconcile.call_args.args[1]))
        self.assertEqual(0, reconcile.call_args.args[1][0]["execution_window_index"])
        self.assertEqual(1, reconcile.call_args.args[1][0]["source_window_index"])
        self.assertEqual(b"baseline-contract", baseline_contract_bytes)

    def test_high_recall_source_change_removes_window_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.high_recall_windows.enabled = True
            window_report = {
                "windows": [
                    {
                        "start_frame": 10,
                        "end_frame": 12,
                        "approved_action": "targeted_rerun",
                        "approved_roi": [10, 10, 30, 30],
                        "mode": "sahi",
                    }
                ],
                "summary": {"selected_window_count": 1},
            }

            def mutate_source(config_path: Path, **kwargs: object) -> int:
                (config_path.parent / "tracking_contract.v2.json").write_text("stale", encoding="utf-8")
                config.mock.background_color += 1
                return 0

            with (
                patch("football_tracking.chunk_runner.write_ball_audit_report", return_value={}),
                patch("football_tracking.chunk_runner.write_ai_review_trigger_report", return_value={}),
                patch("football_tracking.chunk_runner.write_event_candidate_report", return_value={}),
                patch("football_tracking.chunk_runner.write_high_recall_window_report", return_value=window_report),
                patch("football_tracking.chunk_runner.run_high_recall_chunk", side_effect=mutate_source),
                patch(
                    "football_tracking.chunk_runner.reconcile_high_recall_outputs",
                    return_value={"summary": {"accepted_count": 0}},
                ),
            ):
                with self.assertRaises(CandidateSourceChangedError):
                    run_high_recall_windows(config, source_total_frames=100)

            window_contract = (
                config.output_dir / config.high_recall_windows.output_dir_name / "window_000" / "tracking_contract.v2.json"
            )
            self.assertFalse(window_contract.exists())

    def test_approved_no_roi_window_uses_direct_full_frame_policy_instead_of_whole_frame_sahi(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.detector.inference_mode = "direct_full_frame"
            config.high_recall_windows.mode = "sahi"
            window = {
                "start_frame": 10,
                "end_frame": 12,
                "approved_action": "targeted_rerun",
                "mode": "sahi",
            }

            window_config = build_high_recall_window_config(
                config,
                window,
                base_dir / "outputs" / "source" / "high_recall_windows" / "window_000",
            )

        self.assertEqual("direct_full_frame", window_config.detector.inference_mode)
        self.assertIsNone(window_config.filtering.roi)
        self.assertEqual("direct_full_frame_no_roi", window["sahi_policy"])
        self.assertEqual("sahi", config.high_recall_windows.mode)

    def test_approved_no_roi_window_records_policy_when_already_direct_full_frame(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.high_recall_windows.mode = "direct_full_frame"
            window = {
                "start_frame": 10,
                "end_frame": 12,
                "approved_action": "targeted_rerun",
                "mode": "direct_full_frame",
            }

            window_config = build_high_recall_window_config(
                config,
                window,
                base_dir / "outputs" / "source" / "high_recall_windows" / "window_000",
            )

        self.assertEqual("direct_full_frame", window_config.detector.inference_mode)
        self.assertEqual("direct_full_frame_no_roi", window["sahi_policy"])
        self.assertEqual("executable", window["execution_status"])

    def test_approved_roi_window_records_direct_full_frame_roi_policy_when_not_using_sahi(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.high_recall_windows.mode = "direct_full_frame"
            window = {
                "start_frame": 10,
                "end_frame": 12,
                "approved_action": "targeted_rerun",
                "approved_roi": [120, 40, 200, 90],
                "mode": "direct_full_frame",
            }

            window_config = build_high_recall_window_config(
                config,
                window,
                base_dir / "outputs" / "source" / "high_recall_windows" / "window_000",
            )

        self.assertEqual("direct_full_frame", window_config.detector.inference_mode)
        self.assertEqual("direct_full_frame_roi", window["sahi_policy"])

    def test_approved_base_roi_without_ai_roi_records_direct_full_frame_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.filtering.roi = (100, 50, 160, 110)
            config.high_recall_windows.mode = "direct_full_frame"
            window = {
                "start_frame": 10,
                "end_frame": 12,
                "approved_action": "targeted_rerun",
                "mode": "direct_full_frame",
            }

            window_config = build_high_recall_window_config(
                config,
                window,
                base_dir / "outputs" / "source" / "high_recall_windows" / "window_000",
            )

        self.assertEqual("direct_full_frame", window_config.detector.inference_mode)
        self.assertEqual((100, 50, 160, 110), window_config.filtering.roi)
        self.assertEqual("direct_full_frame_base_roi_no_ai_roi", window["sahi_policy"])


class ChunkRunnerExecutionTests(unittest.TestCase):
    def test_run_chunk_loads_config_and_runs_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config_path = write_chunk_config(
                make_app_config(base_dir),
                make_chunk(),
                base_dir / "outputs" / "source" / "chunks",
            )
            pipeline = Mock()

            with patch("football_tracking.chunk_runner.BallTrackingPipeline", return_value=pipeline) as pipeline_cls:
                exit_code = run_chunk(config_path)

        self.assertEqual(0, exit_code)
        pipeline_cls.assert_called_once()
        called_config = pipeline_cls.call_args.args[0]
        self.assertEqual("direct_full_frame", called_config.detector.inference_mode)
        self.assertFalse(called_config.postprocess.enabled)
        self.assertFalse(called_config.follow_cam.enabled)
        self.assertFalse(called_config.temporal_chunks.enabled)
        self.assertTrue(called_config.output.save_csv)
        self.assertTrue(called_config.output.save_debug_jsonl)
        self.assertTrue(called_config.logging.save_debug_jsonl)
        pipeline.run.assert_called_once_with(progress_callback=None, should_cancel=None)

    def test_persisted_chunk_config_rejects_changed_source_and_removes_stale_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.mock.enabled = False
            snapshot = capture_candidate_source_snapshot(config)
            config.runtime.candidate_source_sha256 = snapshot.sha256
            config.runtime.candidate_source_stat_token = snapshot.stat_token
            chunks_root = config.output_dir / "chunks"
            config_path = write_chunk_config(config, make_chunk(), chunks_root)
            chunk_output_dir = chunks_root / "chunk_0001"
            stale_contract = chunk_output_dir / "tracking_contract.v2.json"
            stale_contract.write_text("stale", encoding="utf-8")
            config.input_video.write_bytes(b"changed-video")

            exit_code = run_chunk(config_path)

            self.assertNotEqual(0, exit_code)
            self.assertFalse(stale_contract.exists())

    def test_run_chunk_enforces_raw_only_when_given_non_chunk_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config_path = base_dir / "config" / "base.yaml"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "input_video": str(base_dir / "data" / "input.mp4"),
                        "output_dir": str(base_dir / "outputs" / "run"),
                        "detector": {
                            "model_path": str(base_dir / "weights" / "football_ball_yolo.pt"),
                            "device": "cpu",
                            "inference_mode": "sahi",
                            "use_half": False,
                        },
                        "postprocess": {"enabled": True},
                        "follow_cam": {"enabled": True},
                        "temporal_chunks": {"enabled": True},
                        "runtime": {"use_gpu_if_available": False},
                        "mock": {"enabled": True},
                    },
                    sort_keys=False,
                    allow_unicode=False,
                ),
                encoding="utf-8",
            )
            pipeline = Mock()

            with patch("football_tracking.chunk_runner.BallTrackingPipeline", return_value=pipeline) as pipeline_cls:
                exit_code = run_chunk(config_path)

        self.assertEqual(0, exit_code)
        called_config = pipeline_cls.call_args.args[0]
        self.assertEqual("direct_full_frame", called_config.detector.inference_mode)
        self.assertFalse(called_config.postprocess.enabled)
        self.assertFalse(called_config.follow_cam.enabled)
        self.assertFalse(called_config.temporal_chunks.enabled)
        self.assertTrue(called_config.output.save_csv)
        self.assertTrue(called_config.output.save_debug_jsonl)
        self.assertTrue(called_config.logging.save_debug_jsonl)
        pipeline.run.assert_called_once_with(progress_callback=None, should_cancel=None)

    def test_run_chunk_passes_cancellation_to_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config_path = write_chunk_config(
                make_app_config(base_dir),
                make_chunk(),
                base_dir / "outputs" / "source" / "chunks",
            )
            pipeline = Mock()
            progress_callback = Mock()
            should_cancel = Mock(return_value=False)

            with patch("football_tracking.chunk_runner.BallTrackingPipeline", return_value=pipeline):
                exit_code = run_chunk(config_path, progress_callback=progress_callback, should_cancel=should_cancel)

        self.assertEqual(0, exit_code)
        pipeline.run.assert_called_once_with(progress_callback=progress_callback, should_cancel=should_cancel)

    def test_run_chunk_propagates_cancelled_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config_path = write_chunk_config(
                make_app_config(base_dir),
                make_chunk(),
                base_dir / "outputs" / "source" / "chunks",
            )
            pipeline = Mock()
            pipeline.run.side_effect = CancelledError("stop")

            with (
                patch("football_tracking.chunk_runner.BallTrackingPipeline", return_value=pipeline),
                patch("football_tracking.chunk_runner.logger.exception") as log_exception,
            ):
                with self.assertRaises(CancelledError):
                    run_chunk(config_path)

        log_exception.assert_not_called()

    def test_run_chunk_returns_nonzero_when_config_cannot_load(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            missing_config = Path(temp_name) / "missing.yaml"
            stale_contract = missing_config.parent / "tracking_contract.v2.json"
            stale_contract.write_text("stale", encoding="utf-8")

            with (
                patch("football_tracking.chunk_runner.BallTrackingPipeline") as pipeline_cls,
                patch("football_tracking.chunk_runner.logger.exception") as log_exception,
            ):
                exit_code = run_chunk(missing_config)

            self.assertFalse(stale_contract.exists())
        self.assertNotEqual(0, exit_code)
        pipeline_cls.assert_not_called()
        log_exception.assert_called_once()

    def test_high_recall_chunk_clears_stale_contract_before_config_load_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            missing_config = Path(temp_name) / "missing.yaml"
            stale_contract = missing_config.parent / "tracking_contract.v2.json"
            stale_contract.write_text("stale", encoding="utf-8")

            with patch("football_tracking.chunk_runner.logger.exception") as log_exception:
                exit_code = run_high_recall_chunk(missing_config)

            self.assertFalse(stale_contract.exists())
        self.assertNotEqual(0, exit_code)
        log_exception.assert_called_once()

    def test_run_chunk_returns_nonzero_when_pipeline_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config_path = write_chunk_config(
                make_app_config(base_dir),
                make_chunk(),
                base_dir / "outputs" / "source" / "chunks",
            )
            pipeline = Mock()
            pipeline.run.side_effect = RuntimeError("pipeline failed")

            with (
                patch("football_tracking.chunk_runner.BallTrackingPipeline", return_value=pipeline),
                patch("football_tracking.chunk_runner.logger.exception") as log_exception,
            ):
                exit_code = run_chunk(config_path)

        self.assertNotEqual(0, exit_code)
        pipeline.run.assert_called_once_with(progress_callback=None, should_cancel=None)
        log_exception.assert_called_once()


class TemporalChunkSequentialRunnerTests(unittest.TestCase):
    def test_temporal_run_removes_contracts_from_unplanned_stale_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.mock.frame_count = 4
            config.runtime.start_frame = 0
            config.runtime.max_frames = 4
            stale_root_contract = config.output_dir / "tracking_contract.v2.json"
            stale_chunk_contract = config.output_dir / "chunks" / "chunk_9999" / "tracking_contract.v2.json"
            stale_root_contract.parent.mkdir(parents=True, exist_ok=True)
            stale_chunk_contract.parent.mkdir(parents=True, exist_ok=True)
            stale_root_contract.write_text("old-root", encoding="utf-8")
            stale_chunk_contract.write_text("old-child", encoding="utf-8")
            chunk = TemporalChunk(0, 0, 0, 3, 0, 3, "chunk_0000")

            with (
                patch("football_tracking.chunk_runner.plan_temporal_chunks", return_value=[chunk]),
                patch("football_tracking.chunk_runner.run_chunk", return_value=0),
                patch("football_tracking.chunk_runner.stitch_chunk_outputs"),
            ):
                run_temporal_chunks(config)

            self.assertFalse(stale_root_contract.exists())
            self.assertFalse(stale_chunk_contract.exists())

    def test_temporal_run_clears_stale_contracts_before_source_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.mock.enabled = False
            config.temporal_chunks.output_dir_name = "new_parts"
            stale_root_contract = config.output_dir / "tracking_contract.v2.json"
            stale_contracts = [
                stale_root_contract,
                config.output_dir / "chunks" / "chunk_0000" / "tracking_contract.v2.json",
                config.output_dir / "old_parts" / "chunk_0001" / "tracking_contract.v2.json",
                config.output_dir / "new_parts" / "chunk_0000" / "tracking_contract.v2.json",
            ]
            sentinel_contract = config.output_dir / "old_parts" / "not_a_chunk" / "tracking_contract.v2.json"
            for path in [*stale_contracts, sentinel_contract]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("old", encoding="utf-8")
            config.input_video.unlink()

            with self.assertRaises(FileNotFoundError):
                run_temporal_chunks(config)

            self.assertTrue(all(not path.exists() for path in stale_contracts))
            self.assertTrue(sentinel_contract.exists())

    def test_temporal_run_clears_stale_root_contract_before_chunk_root_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            stale_root_contract = config.output_dir / "tracking_contract.v2.json"
            stale_root_contract.parent.mkdir(parents=True, exist_ok=True)
            stale_root_contract.write_text("old-root", encoding="utf-8")
            stale_nested_contract = (
                config.output_dir / "old_parts" / "chunk_0000" / "tracking_contract.v2.json"
            )
            stale_nested_contract.parent.mkdir(parents=True, exist_ok=True)
            stale_nested_contract.write_text("old-child", encoding="utf-8")
            (config.output_dir / "temporal_chunks_report.json").write_text(
                json.dumps(
                    {
                        "chunks_root_name": "old_parts",
                        "chunks": [{"name": "chunk_0000"}],
                        "stitch": {"status": "succeeded"},
                    }
                ),
                encoding="utf-8",
            )
            chunks_root = config.output_dir / config.temporal_chunks.output_dir_name
            chunks_root.write_text("not-a-directory", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                run_temporal_chunks(config)

            self.assertFalse(stale_root_contract.exists())
            self.assertFalse(stale_nested_contract.exists())

    def test_temporal_source_change_before_stitch_removes_chunk_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.mock.frame_count = 4
            config.runtime.start_frame = 0
            config.runtime.max_frames = 4
            chunk = TemporalChunk(0, 0, 0, 3, 0, 3, "chunk_0000")

            def mutate_source_after_chunk(config_path: Path, **kwargs: object) -> int:
                payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                chunk_output_dir = Path(payload["output_dir"])
                chunk_output_dir.mkdir(parents=True, exist_ok=True)
                (chunk_output_dir / "tracking_contract.v2.json").write_text("stale", encoding="utf-8")
                config.mock.background_color += 1
                return 0

            with (
                patch("football_tracking.chunk_runner.plan_temporal_chunks", return_value=[chunk]),
                patch("football_tracking.chunk_runner.run_chunk", side_effect=mutate_source_after_chunk),
                patch("football_tracking.chunk_runner.stitch_chunk_outputs") as stitch,
            ):
                with self.assertRaises(CandidateSourceChangedError):
                    run_temporal_chunks(config)

            chunk_contract = config.output_dir / "chunks" / "chunk_0000" / "tracking_contract.v2.json"
            self.assertFalse(chunk_contract.exists())
            self.assertFalse((config.output_dir / "tracking_contract.v2.json").exists())
            stitch.assert_not_called()

    def test_postprocess_source_change_and_failure_removes_temporal_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.mock.enabled = False
            config.runtime.start_frame = 0
            config.runtime.max_frames = 3
            config.follow_cam.enabled = False
            chunk = TemporalChunk(0, 0, 0, 2, 0, 2, "chunk_0000")

            def write_chunk(config_path: Path, **kwargs: object) -> int:
                write_raw_outputs_for_chunk_config(config_path)
                return 0

            def mutate_source_and_fail() -> None:
                config.input_video.write_bytes(b"source-mutated-by-postprocess")
                raise RuntimeError("postprocess failed")

            with (
                patch("football_tracking.chunk_runner.plan_temporal_chunks", return_value=[chunk]),
                patch("football_tracking.chunk_runner._source_total_frames", return_value=3),
                patch("football_tracking.chunk_runner.run_chunk", side_effect=write_chunk),
                patch("football_tracking.chunk_runner.TrackPostprocessor") as postprocessor_cls,
            ):
                postprocessor_cls.return_value.run.side_effect = mutate_source_and_fail
                with self.assertRaises(CandidateSourceChangedError):
                    run_temporal_chunks(config)

            self.assertFalse((config.output_dir / "tracking_contract.v2.json").exists())
            self.assertFalse(
                (config.output_dir / "chunks" / "chunk_0000" / "tracking_contract.v2.json").exists()
            )

    def test_temporal_worker_failure_removes_contract_from_prior_successful_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.mock.frame_count = 8
            config.runtime.start_frame = 0
            config.runtime.max_frames = 8
            chunks = [
                TemporalChunk(0, 0, 0, 3, 0, 3, "chunk_0000"),
                TemporalChunk(1, 4, 4, 7, 4, 7, "chunk_0001"),
            ]

            def fail_second_worker(config_path: Path, **kwargs: object) -> int:
                payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                chunk_output_dir = Path(payload["output_dir"])
                chunk_output_dir.mkdir(parents=True, exist_ok=True)
                if chunk_output_dir.name == "chunk_0000":
                    (chunk_output_dir / "tracking_contract.v2.json").write_text("first", encoding="utf-8")
                    return 0
                config.mock.background_color += 1
                return 1

            with (
                patch("football_tracking.chunk_runner.plan_temporal_chunks", return_value=chunks),
                patch("football_tracking.chunk_runner.run_chunk", side_effect=fail_second_worker),
                patch("football_tracking.chunk_runner.stitch_chunk_outputs") as stitch,
            ):
                with self.assertRaisesRegex(RuntimeError, "chunk_0001 failed"):
                    run_temporal_chunks(config)

            first_contract = config.output_dir / "chunks" / "chunk_0000" / "tracking_contract.v2.json"
            self.assertFalse(first_contract.exists())
            stitch.assert_not_called()

    def test_post_worker_cancellation_removes_unstitched_chunk_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.mock.frame_count = 8
            config.runtime.start_frame = 0
            config.runtime.max_frames = 8
            chunks = [
                TemporalChunk(0, 0, 0, 3, 0, 3, "chunk_0000"),
                TemporalChunk(1, 4, 4, 7, 4, 7, "chunk_0001"),
            ]

            def write_chunk(config_path: Path, **kwargs: object) -> int:
                write_raw_outputs_for_chunk_config(config_path)
                return 0

            chunk_contracts = [
                config.output_dir / "chunks" / chunk.output_dir_name / "tracking_contract.v2.json"
                for chunk in chunks
            ]

            with (
                patch("football_tracking.chunk_runner.plan_temporal_chunks", return_value=chunks),
                patch("football_tracking.chunk_runner.run_chunk", side_effect=write_chunk),
                patch("football_tracking.chunk_runner.stitch_chunk_outputs") as stitch,
            ):
                with self.assertRaises(CancelledError):
                    run_temporal_chunks(
                        config,
                        should_cancel=lambda: all(path.exists() for path in chunk_contracts),
                    )

            self.assertTrue(all(not path.exists() for path in chunk_contracts))
            self.assertFalse((config.output_dir / "tracking_contract.v2.json").exists())
            stitch.assert_not_called()

    def test_run_temporal_chunks_plans_writes_runs_stitches_and_runs_global_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.mock.frame_count = 250
            config.runtime.start_frame = 20
            config.runtime.max_frames = 180
            config.temporal_chunks.chunk_frames = 100
            config.temporal_chunks.overlap_frames = 10
            config.temporal_chunks.decode_preroll_frames = 7
            config.output.save_csv = False
            config.output.save_debug_jsonl = False
            config.logging.save_debug_jsonl = False
            config.mock.enabled = False
            chunks = [
                TemporalChunk(
                    index=0,
                    decode_start_frame=13,
                    start_frame=20,
                    end_frame=129,
                    core_start_frame=20,
                    core_end_frame=119,
                    output_dir_name="chunk_0000",
                ),
                TemporalChunk(
                    index=1,
                    decode_start_frame=103,
                    start_frame=110,
                    end_frame=209,
                    core_start_frame=120,
                    core_end_frame=199,
                    output_dir_name="chunk_0001",
                ),
            ]
            progress_callback = Mock()
            should_cancel = Mock(return_value=False)

            with (
                patch("football_tracking.chunk_runner.plan_temporal_chunks", return_value=chunks) as plan_chunks,
                patch("football_tracking.chunk_runner.run_chunk", return_value=0) as run_chunk_mock,
                patch("football_tracking.chunk_runner.stitch_chunk_outputs", return_value={"frame_count": 180}) as stitch,
                patch("football_tracking.chunk_runner._source_total_frames", return_value=250),
                patch("football_tracking.chunk_runner.TrackPostprocessor") as postprocessor_cls,
                patch("football_tracking.chunk_runner.FollowCamGenerator") as follow_cam_cls,
            ):
                run_temporal_chunks(config, progress_callback=progress_callback, should_cancel=should_cancel)

            chunks_root = config.output_dir / config.temporal_chunks.output_dir_name
            plan_chunks.assert_called_once_with(
                source_total_frames=250,
                chunk_frames=100,
                overlap_frames=10,
                start_frame=20,
                max_frames=180,
                decode_preroll_frames=7,
            )
            self.assertTrue((chunks_root / "chunk_0000").is_dir())
            self.assertTrue((chunks_root / "chunk_0001").is_dir())
            self.assertEqual(
                [chunks_root / "chunk_0000" / "chunk_config.yaml", chunks_root / "chunk_0001" / "chunk_config.yaml"],
                [call.args[0] for call in run_chunk_mock.call_args_list],
            )
            self.assertTrue(config.output.save_csv)
            self.assertTrue(config.output.save_debug_jsonl)
            self.assertTrue(config.logging.save_debug_jsonl)
            self.assertIsNotNone(config.runtime.candidate_source_sha256)
            self.assertIsNotNone(config.runtime.candidate_source_stat_token)
            self.assertEqual(
                {config.runtime.candidate_source_sha256},
                {
                    yaml.safe_load(call.args[0].read_text(encoding="utf-8"))["runtime"]["candidate_source_sha256"]
                    for call in run_chunk_mock.call_args_list
                },
            )
            self.assertEqual(
                {config.runtime.candidate_source_stat_token},
                {
                    yaml.safe_load(call.args[0].read_text(encoding="utf-8"))["runtime"][
                        "candidate_source_stat_token"
                    ]
                    for call in run_chunk_mock.call_args_list
                },
            )
            for call_item in run_chunk_mock.call_args_list:
                self.assertIs(progress_callback, call_item.kwargs["progress_callback"])
                self.assertIs(should_cancel, call_item.kwargs["should_cancel"])
            stitch.assert_called_once_with(
                chunks,
                [chunks_root / "chunk_0000", chunks_root / "chunk_0001"],
                config.output_dir,
                output_config=config.output,
                candidate_source_sha256=config.runtime.candidate_source_sha256,
                chunks_root_name=config.temporal_chunks.output_dir_name,
            )
            postprocessor_cls.assert_called_once_with(config)
            postprocessor_cls.return_value.run.assert_called_once_with()
            follow_cam_cls.assert_called_once_with(config)
            follow_cam_cls.return_value.run.assert_called_once_with(
                progress_callback=progress_callback,
                should_cancel=should_cancel,
            )
            progress_stages = [call.args[0]["stage"] for call in progress_callback.call_args_list]
            self.assertIn("temporal_chunks", progress_stages)
            self.assertIn("stitch", progress_stages)

    def test_run_temporal_chunks_runs_high_recall_windows_before_global_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.mock.enabled = False
            config.runtime.start_frame = 0
            config.runtime.max_frames = 30
            config.temporal_chunks.chunk_frames = 30
            config.temporal_chunks.overlap_frames = 5
            config.high_recall_windows.enabled = True
            config.high_recall_windows.margin_frames = 4
            config.high_recall_windows.mode = "sahi"
            chunks = [TemporalChunk(0, 0, 0, 29, 0, 29, "chunk_0000")]
            call_order: list[str] = []
            config.output_dir.mkdir(parents=True, exist_ok=True)
            stale_cleaned_csv = config.output_dir / config.postprocess.cleaned_csv_name
            stale_cleanup_report = config.output_dir / config.postprocess.cleanup_report_name
            stale_cleaned_csv.write_text("stale", encoding="utf-8")
            stale_cleanup_report.write_text("stale", encoding="utf-8")

            def fake_temporal_chunk_run(*args: object, **kwargs: object) -> int:
                call_order.append("chunk")
                return 0

            def fake_stitch(*args: object, **kwargs: object) -> dict[str, int]:
                call_order.append("stitch")
                return {"frame_count": 30}

            def fake_window_run(config_path: Path, **kwargs: object) -> int:
                call_order.append("high_recall")
                payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                self.assertEqual("sahi", payload["detector"]["inference_mode"])
                self.assertEqual(10, payload["runtime"]["start_frame"])
                self.assertEqual(6, payload["runtime"]["max_frames"])
                self.assertFalse(payload["postprocess"]["enabled"])
                self.assertFalse(payload["follow_cam"]["enabled"])
                self.assertFalse(payload["temporal_chunks"]["enabled"])
                self.assertFalse(payload["high_recall_windows"]["enabled"])
                return 0

            def fake_reconcile(*args: object, **kwargs: object) -> dict[str, object]:
                call_order.append("reconcile")
                return {"summary": {"accepted_count": 1, "rejected_count": 0}, "windows": []}

            def fake_audit(output_dir: Path) -> dict[str, object]:
                self.assertFalse(stale_cleaned_csv.exists())
                self.assertFalse(stale_cleanup_report.exists())
                call_order.append("audit")
                return {}

            def fake_event_candidates(output_dir: Path, **kwargs: object) -> dict[str, object]:
                self.assertEqual(config.output_dir, output_dir)
                self.assertEqual(30.0, kwargs["fps"])
                self.assertEqual("input_video", kwargs["fps_source"])
                call_order.append("events")
                return {}

            with (
                patch("football_tracking.chunk_runner.plan_temporal_chunks", return_value=chunks),
                patch("football_tracking.chunk_runner.run_chunk", side_effect=fake_temporal_chunk_run),
                patch("football_tracking.chunk_runner.stitch_chunk_outputs", side_effect=fake_stitch),
                patch("football_tracking.chunk_runner._source_total_frames", return_value=30),
                patch("football_tracking.chunk_runner._event_candidate_fps", return_value=(30.0, "input_video")),
                patch("football_tracking.chunk_runner.write_ball_audit_report", side_effect=fake_audit),
                patch("football_tracking.chunk_runner.write_ai_review_trigger_report", side_effect=lambda output_dir: call_order.append("triggers") or {}),
                patch("football_tracking.chunk_runner.write_event_candidate_report", side_effect=fake_event_candidates),
                patch(
                    "football_tracking.chunk_runner.write_high_recall_window_report",
                    return_value={
                        "windows": [{"start_frame": 10, "end_frame": 15, "reason": "lost_gap", "priority": "high"}],
                        "summary": {"selected_window_count": 1},
                    },
                ) as window_report,
                patch("football_tracking.chunk_runner.run_high_recall_chunk", side_effect=fake_window_run),
                patch("football_tracking.chunk_runner.reconcile_high_recall_outputs", side_effect=fake_reconcile) as reconcile,
                patch("football_tracking.chunk_runner.TrackPostprocessor") as postprocessor_cls,
                patch("football_tracking.chunk_runner.FollowCamGenerator") as follow_cam_cls,
            ):
                postprocessor_cls.return_value.run.side_effect = lambda: call_order.append("postprocess")
                follow_cam_cls.return_value.run.side_effect = lambda **kwargs: call_order.append("follow_cam")

                run_temporal_chunks(config)

            self.assertEqual(
                ["chunk", "stitch", "audit", "triggers", "events", "high_recall", "reconcile", "postprocess", "follow_cam"],
                call_order,
            )
            window_report.assert_called_once()
            self.assertEqual(4, window_report.call_args.kwargs["margin_frames"])
            reconcile.assert_called_once()
            self.assertTrue((config.output_dir / "high_recall_windows" / "window_000" / "chunk_config.yaml").exists())

    def test_run_temporal_chunks_records_high_recall_reconcile_failure_before_reraising(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.mock.enabled = False
            config.runtime.start_frame = 0
            config.runtime.max_frames = 30
            config.temporal_chunks.chunk_frames = 30
            config.temporal_chunks.overlap_frames = 5
            config.high_recall_windows.enabled = True
            chunks = [TemporalChunk(0, 0, 0, 29, 0, 29, "chunk_0000")]

            with (
                patch("football_tracking.chunk_runner.plan_temporal_chunks", return_value=chunks),
                patch("football_tracking.chunk_runner.run_chunk", return_value=0),
                patch("football_tracking.chunk_runner.stitch_chunk_outputs", return_value={"frame_count": 30}),
                patch("football_tracking.chunk_runner._source_total_frames", return_value=30),
                patch("football_tracking.chunk_runner.write_ball_audit_report", return_value={}),
                patch("football_tracking.chunk_runner.write_ai_review_trigger_report", return_value={}),
                patch("football_tracking.chunk_runner.write_event_candidate_report", return_value={}),
                patch(
                    "football_tracking.chunk_runner.write_high_recall_window_report",
                    return_value={
                        "windows": [{"start_frame": 10, "end_frame": 15, "reason": "lost_gap", "priority": "high"}],
                        "summary": {"selected_window_count": 1},
                    },
                ),
                patch("football_tracking.chunk_runner.run_high_recall_chunk", return_value=0),
                patch("football_tracking.chunk_runner.reconcile_high_recall_outputs", side_effect=RuntimeError("merge failed")),
                patch("football_tracking.chunk_runner.TrackPostprocessor") as postprocessor_cls,
                patch("football_tracking.chunk_runner.FollowCamGenerator") as follow_cam_cls,
            ):
                with self.assertRaisesRegex(RuntimeError, "merge failed"):
                    run_temporal_chunks(config)

            report = json.loads((config.output_dir / "high_recall_windows" / "report.json").read_text(encoding="utf-8"))
            self.assertEqual("failed", report["reconcile"]["status"])
            self.assertIn("merge failed", report["reconcile"]["error"])
            postprocessor_cls.assert_not_called()
            follow_cam_cls.assert_not_called()

    def test_run_temporal_chunks_skips_global_outputs_in_mock_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            chunks = [TemporalChunk(0, 0, 0, 9, 0, 9, "chunk_0000")]

            with (
                patch("football_tracking.chunk_runner.plan_temporal_chunks", return_value=chunks),
                patch("football_tracking.chunk_runner.run_chunk", return_value=0),
                patch("football_tracking.chunk_runner.stitch_chunk_outputs", return_value={"frame_count": 10}),
                patch("football_tracking.chunk_runner.TrackPostprocessor") as postprocessor_cls,
                patch("football_tracking.chunk_runner.FollowCamGenerator") as follow_cam_cls,
            ):
                run_temporal_chunks(config)

        postprocessor_cls.assert_not_called()
        follow_cam_cls.assert_not_called()

    def test_run_temporal_chunks_fails_when_no_chunks_are_planned(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            config = make_app_config(Path(temp_name))

            with (
                patch("football_tracking.chunk_runner.plan_temporal_chunks", return_value=[]),
                patch("football_tracking.chunk_runner.run_chunk") as run_chunk_mock,
                patch("football_tracking.chunk_runner.stitch_chunk_outputs") as stitch,
            ):
                with self.assertRaisesRegex(RuntimeError, "no chunks"):
                    run_temporal_chunks(config)

        run_chunk_mock.assert_not_called()
        stitch.assert_not_called()

    def test_run_temporal_chunks_mock_mode_writes_stitched_raw_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.mock.frame_count = 8
            config.runtime.start_frame = 0
            config.runtime.max_frames = 8
            config.temporal_chunks.chunk_frames = 4
            config.temporal_chunks.overlap_frames = 1
            config.temporal_chunks.decode_preroll_frames = 1

            run_temporal_chunks(config)

            with (config.output_dir / config.output.csv_name).open("r", newline="", encoding="utf-8-sig") as csv_file:
                rows = list(csv.reader(csv_file))
            debug_lines = (config.output_dir / config.output.debug_jsonl_name).read_text(encoding="utf-8").splitlines()
            report = json.loads((config.output_dir / "temporal_chunks_report.json").read_text(encoding="utf-8"))
            follow_cam_exists = (config.output_dir / config.follow_cam.output_video_name).exists()
            cleanup_report_exists = (config.output_dir / config.postprocess.cleanup_report_name).exists()

        self.assertEqual(["Frame", "X", "Y", "Confidence", "Status"], rows[0])
        self.assertEqual(list(range(8)), [int(row[0]) for row in rows[1:]])
        self.assertEqual(list(range(8)), [json.loads(line)["frame"] for line in debug_lines])
        self.assertEqual(2, report["chunk_count"])
        self.assertEqual(8, report["frame_count"])
        self.assertEqual(["chunk_0000", "chunk_0001"], report["source_chunk_names"])
        self.assertFalse(follow_cam_exists)
        self.assertFalse(cleanup_report_exists)

    def test_run_temporal_chunks_stops_on_first_chunk_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            chunks = [
                TemporalChunk(0, 0, 0, 9, 0, 9, "chunk_0000"),
                TemporalChunk(1, 8, 8, 19, 10, 19, "chunk_0001"),
            ]

            with (
                patch("football_tracking.chunk_runner.plan_temporal_chunks", return_value=chunks),
                patch("football_tracking.chunk_runner.run_chunk", side_effect=[0, 9]) as run_chunk_mock,
                patch("football_tracking.chunk_runner.stitch_chunk_outputs") as stitch,
                patch("football_tracking.chunk_runner.TrackPostprocessor") as postprocessor_cls,
                patch("football_tracking.chunk_runner.FollowCamGenerator") as follow_cam_cls,
            ):
                with self.assertRaisesRegex(RuntimeError, "chunk_0001"):
                    run_temporal_chunks(config)
            report = json.loads((config.output_dir / "temporal_chunks_report.json").read_text(encoding="utf-8"))

            self.assertEqual(2, run_chunk_mock.call_count)
            stitch.assert_not_called()
            postprocessor_cls.assert_not_called()
            follow_cam_cls.assert_not_called()
            self.assertEqual("failed", report["execution"]["status"])
            self.assertIn("chunk_0001", report["execution"]["error"])
            self.assertEqual(9, report["execution"]["results"][1]["exit_code"])

    def test_run_temporal_chunks_cancels_before_starting_next_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            chunks = [
                TemporalChunk(0, 0, 0, 9, 0, 9, "chunk_0000"),
                TemporalChunk(1, 8, 8, 19, 10, 19, "chunk_0001"),
            ]
            should_cancel = Mock(side_effect=[False, True])

            with (
                patch("football_tracking.chunk_runner.plan_temporal_chunks", return_value=chunks),
                patch("football_tracking.chunk_runner.run_chunk", return_value=0) as run_chunk_mock,
                patch("football_tracking.chunk_runner.stitch_chunk_outputs") as stitch,
            ):
                with self.assertRaises(CancelledError):
                    run_temporal_chunks(config, should_cancel=should_cancel)
            report = json.loads((config.output_dir / "temporal_chunks_report.json").read_text(encoding="utf-8"))

            self.assertEqual(1, run_chunk_mock.call_count)
            stitch.assert_not_called()
            self.assertEqual("cancelled", report["execution"]["status"])
            self.assertEqual(1, len(report["execution"]["results"]))

    def test_run_temporal_chunks_preserves_execution_report_when_stitch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            chunks = make_runner_chunks(1)

            with (
                patch("football_tracking.chunk_runner.plan_temporal_chunks", return_value=chunks),
                patch("football_tracking.chunk_runner.run_chunk", return_value=0),
                patch("football_tracking.chunk_runner.stitch_chunk_outputs", side_effect=RuntimeError("stitch failed")),
            ):
                with self.assertRaisesRegex(RuntimeError, "stitch failed"):
                    run_temporal_chunks(config)
            report = json.loads((config.output_dir / "temporal_chunks_report.json").read_text(encoding="utf-8"))

        self.assertEqual("succeeded", report["execution"]["status"])
        self.assertEqual("in_process", report["execution"]["mode"])
        self.assertEqual(1, len(report["execution"]["results"]))
        self.assertEqual("failed", report["stitch"]["status"])
        self.assertIn("stitch failed", report["stitch"]["error"])

    def test_run_temporal_chunks_caps_single_gpu_to_in_process_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.detector.device = "cuda:0"
            config.temporal_chunks.max_workers = 4
            chunks = make_runner_chunks(2)

            with (
                patch("football_tracking.chunk_runner.plan_temporal_chunks", return_value=chunks),
                patch("football_tracking.chunk_runner.run_chunk", return_value=0) as run_chunk_mock,
                patch("football_tracking.chunk_runner.stitch_chunk_outputs", return_value={"frame_count": 10}),
                patch("football_tracking.chunk_runner.subprocess.Popen") as popen,
            ):
                run_temporal_chunks(config)

            self.assertEqual(2, run_chunk_mock.call_count)
            popen.assert_not_called()

    def test_run_temporal_chunks_uses_subprocess_workers_for_cpu_parallelism_and_records_execution(self) -> None:
        SuccessfulChunkPopen.instances = []
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.detector.device = "cpu"
            config.temporal_chunks.max_workers = 2
            config.mock.frame_count = 10
            chunks = make_runner_chunks(2)

            with (
                patch("football_tracking.chunk_runner.plan_temporal_chunks", return_value=chunks),
                patch("football_tracking.chunk_runner.subprocess.Popen", SuccessfulChunkPopen),
                patch("football_tracking.chunk_runner.time.sleep"),
                patch("football_tracking.chunk_runner.run_chunk") as run_chunk_mock,
            ):
                run_temporal_chunks(config)

            report = json.loads((config.output_dir / "temporal_chunks_report.json").read_text(encoding="utf-8"))

        self.assertEqual(2, len(SuccessfulChunkPopen.instances))
        self.assertEqual(Path(__file__).resolve().parents[1], Path(SuccessfulChunkPopen.instances[0].kwargs["cwd"]))
        self.assertIn(str(Path(__file__).resolve().parents[1]), SuccessfulChunkPopen.instances[0].kwargs["env"]["PYTHONPATH"])
        self.assertEqual(
            [
                sys.executable,
                "-m",
                "football_tracking.chunk_worker",
                "--config",
                str(SuccessfulChunkPopen.instances[0].config_path),
            ],
            SuccessfulChunkPopen.instances[0].command,
        )
        run_chunk_mock.assert_not_called()
        self.assertEqual("succeeded", report["execution"]["status"])
        self.assertEqual("subprocess", report["execution"]["mode"])
        self.assertEqual(2, report["execution"]["requested_workers"])
        self.assertEqual(2, report["execution"]["effective_workers"])
        self.assertEqual(2, len(report["execution"]["results"]))
        first_result = report["execution"]["results"][0]
        self.assertTrue(
            {
                "chunk",
                "chunk_index",
                "chunk_name",
                "config_path",
                "command",
                "device",
                "start_time",
                "end_time",
                "duration",
                "exit_code",
                "stdout",
                "stderr",
            }.issubset(first_result)
        )
        self.assertEqual({"index": 0, "name": "chunk_0000"}, first_result["chunk"])
        self.assertEqual(0, first_result["chunk_index"])
        self.assertEqual("chunk_0000", first_result["chunk_name"])
        self.assertEqual(str(SuccessfulChunkPopen.instances[0].config_path), first_result["config_path"])
        self.assertEqual(SuccessfulChunkPopen.instances[0].command, first_result["command"])
        self.assertEqual("cpu", first_result["device"])
        self.assertGreaterEqual(first_result["end_time"], first_result["start_time"])
        self.assertGreaterEqual(first_result["duration"], 0.0)
        self.assertEqual(0, first_result["exit_code"])
        self.assertIn("finished chunk_config.yaml", first_result["stdout"])
        self.assertEqual("", first_result["stderr"])
        self.assertEqual([], report["execution"]["devices"])

    def test_run_temporal_chunks_assigns_configured_devices_to_chunk_configs(self) -> None:
        SuccessfulChunkPopen.instances = []
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.detector.device = "cuda:0"
            config.temporal_chunks.max_workers = 4
            config.temporal_chunks.devices = ("cuda:0", "cuda:1")
            config.mock.frame_count = 15
            chunks = make_runner_chunks(3)

            with (
                patch("football_tracking.chunk_runner.plan_temporal_chunks", return_value=chunks),
                patch("football_tracking.chunk_runner.subprocess.Popen", SuccessfulChunkPopen),
                patch("football_tracking.chunk_runner.time.sleep"),
            ):
                run_temporal_chunks(config)

            written_devices = [
                yaml.safe_load((config.output_dir / "chunks" / chunk.output_dir_name / "chunk_config.yaml").read_text(
                    encoding="utf-8"
                ))["detector"]["device"]
                for chunk in chunks
            ]
            report = json.loads((config.output_dir / "temporal_chunks_report.json").read_text(encoding="utf-8"))

        self.assertEqual(["cuda:0", "cuda:1", "cuda:0"], written_devices)
        self.assertEqual(["cuda:0", "cuda:1"], report["execution"]["devices"])
        self.assertEqual(2, report["execution"]["effective_workers"])

    def test_run_temporal_chunks_stops_subprocess_workers_on_failure(self) -> None:
        FailingFirstChunkPopen.instances = []
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.detector.device = "cpu"
            config.temporal_chunks.max_workers = 2
            chunks = make_runner_chunks(3)

            with (
                patch("football_tracking.chunk_runner.plan_temporal_chunks", return_value=chunks),
                patch("football_tracking.chunk_runner.subprocess.Popen", FailingFirstChunkPopen),
                patch("football_tracking.chunk_runner.time.sleep"),
                patch("football_tracking.chunk_runner.run_chunk") as run_chunk_mock,
                patch("football_tracking.chunk_runner.stitch_chunk_outputs") as stitch,
            ):
                with self.assertRaisesRegex(RuntimeError, "chunk_0000"):
                    run_temporal_chunks(config)
            report = json.loads((config.output_dir / "temporal_chunks_report.json").read_text(encoding="utf-8"))

        self.assertEqual(2, len(FailingFirstChunkPopen.instances))
        self.assertTrue(FailingFirstChunkPopen.instances[1].terminated)
        run_chunk_mock.assert_not_called()
        stitch.assert_not_called()
        self.assertEqual("failed", report["execution"]["status"])
        self.assertIn("chunk_0000", report["execution"]["error"])
        self.assertEqual(2, len(report["execution"]["results"]))
        self.assertEqual(7, report["execution"]["results"][0]["exit_code"])
        self.assertIn("worker failed", report["execution"]["results"][0]["stderr"])

    def test_run_temporal_chunks_preserves_zero_exit_code_when_cleaning_up_completed_companion(self) -> None:
        FailingFirstCompletedSecondChunkPopen.instances = []
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.detector.device = "cpu"
            config.temporal_chunks.max_workers = 2
            chunks = make_runner_chunks(2)

            with (
                patch("football_tracking.chunk_runner.plan_temporal_chunks", return_value=chunks),
                patch("football_tracking.chunk_runner.subprocess.Popen", FailingFirstCompletedSecondChunkPopen),
                patch("football_tracking.chunk_runner.time.sleep"),
                patch("football_tracking.chunk_runner.stitch_chunk_outputs") as stitch,
            ):
                with self.assertRaisesRegex(RuntimeError, "chunk_0000"):
                    run_temporal_chunks(config)
            report = json.loads((config.output_dir / "temporal_chunks_report.json").read_text(encoding="utf-8"))

        self.assertEqual(2, len(FailingFirstCompletedSecondChunkPopen.instances))
        self.assertFalse(FailingFirstCompletedSecondChunkPopen.instances[1].terminated)
        stitch.assert_not_called()
        self.assertEqual("failed", report["execution"]["status"])
        self.assertEqual(7, report["execution"]["results"][0]["exit_code"])
        self.assertEqual(0, report["execution"]["results"][1]["exit_code"])

    def test_run_temporal_chunks_writes_failed_report_when_subprocess_cannot_start(self) -> None:
        StartFailingChunkPopen.instances = []
        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.detector.device = "cpu"
            config.temporal_chunks.max_workers = 2
            chunks = make_runner_chunks(2)

            with (
                patch("football_tracking.chunk_runner.plan_temporal_chunks", return_value=chunks),
                patch("football_tracking.chunk_runner.subprocess.Popen", StartFailingChunkPopen),
                patch("football_tracking.chunk_runner.run_chunk") as run_chunk_mock,
                patch("football_tracking.chunk_runner.stitch_chunk_outputs") as stitch,
            ):
                with self.assertRaisesRegex(RuntimeError, "Unable to start temporal chunk chunk_0000"):
                    run_temporal_chunks(config)
            report = json.loads((config.output_dir / "temporal_chunks_report.json").read_text(encoding="utf-8"))

        self.assertEqual(1, len(StartFailingChunkPopen.instances))
        run_chunk_mock.assert_not_called()
        stitch.assert_not_called()
        self.assertEqual("failed", report["execution"]["status"])
        self.assertIn("cannot start worker", report["execution"]["error"])
        self.assertEqual([], report["execution"]["results"])

    def test_run_temporal_chunks_terminates_subprocess_workers_on_cancel(self) -> None:
        RunningChunkPopen.instances = []
        cancel_calls = {"count": 0}

        def should_cancel() -> bool:
            cancel_calls["count"] += 1
            return cancel_calls["count"] > 1

        with tempfile.TemporaryDirectory() as temp_name:
            base_dir = Path(temp_name)
            config = make_app_config(base_dir)
            config.detector.device = "cpu"
            config.temporal_chunks.max_workers = 2
            chunks = make_runner_chunks(2)

            with (
                patch("football_tracking.chunk_runner.plan_temporal_chunks", return_value=chunks),
                patch("football_tracking.chunk_runner.subprocess.Popen", RunningChunkPopen),
                patch("football_tracking.chunk_runner.time.sleep"),
                patch("football_tracking.chunk_runner.run_chunk") as run_chunk_mock,
                patch("football_tracking.chunk_runner.stitch_chunk_outputs") as stitch,
            ):
                with self.assertRaises(CancelledError):
                    run_temporal_chunks(config, should_cancel=should_cancel)
            report = json.loads((config.output_dir / "temporal_chunks_report.json").read_text(encoding="utf-8"))

        self.assertGreaterEqual(len(RunningChunkPopen.instances), 1)
        self.assertTrue(all(instance.terminated for instance in RunningChunkPopen.instances))
        run_chunk_mock.assert_not_called()
        stitch.assert_not_called()
        self.assertEqual("cancelled", report["execution"]["status"])
        self.assertEqual(len(RunningChunkPopen.instances), len(report["execution"]["results"]))


class ChunkWorkerCliTests(unittest.TestCase):
    def test_main_returns_run_chunk_exit_code(self) -> None:
        from football_tracking import chunk_worker

        with tempfile.TemporaryDirectory() as temp_name:
            config_path = Path(temp_name) / "chunk_config.yaml"

            with patch("football_tracking.chunk_worker.run_chunk", return_value=7) as run_chunk_mock:
                exit_code = chunk_worker.main(["--config", str(config_path)])

        self.assertEqual(7, exit_code)
        run_chunk_mock.assert_called_once_with(config_path)


class MainTemporalChunkBranchTests(unittest.TestCase):
    def test_main_uses_temporal_runner_when_temporal_chunks_enabled_after_mock_flags(self) -> None:
        import argparse

        main_module = load_backend_main_module()

        with tempfile.TemporaryDirectory() as temp_name:
            config = make_app_config(Path(temp_name))
            config.temporal_chunks.enabled = True
            config.mock.enabled = False
            args = argparse.Namespace(config=Path("config.yaml"), mock=True, mock_scenario="C")

            with (
                patch.object(main_module, "parse_args", return_value=args),
                patch.object(main_module, "load_config", return_value=config),
                patch.object(main_module, "run_temporal_chunks") as run_temporal_chunks_mock,
                patch.object(main_module, "BallTrackingPipeline") as pipeline_cls,
            ):
                main_module.main()

        self.assertTrue(config.mock.enabled)
        self.assertEqual("C", config.mock.scenario)
        run_temporal_chunks_mock.assert_called_once_with(config)
        pipeline_cls.assert_not_called()

    def test_main_uses_pipeline_when_temporal_chunks_disabled(self) -> None:
        import argparse

        main_module = load_backend_main_module()

        with tempfile.TemporaryDirectory() as temp_name:
            config = make_app_config(Path(temp_name))
            config.temporal_chunks.enabled = False
            args = argparse.Namespace(config=Path("config.yaml"), mock=False, mock_scenario=None)
            pipeline = Mock()

            with (
                patch.object(main_module, "parse_args", return_value=args),
                patch.object(main_module, "load_config", return_value=config),
                patch.object(main_module, "run_temporal_chunks") as run_temporal_chunks_mock,
                patch.object(main_module, "BallTrackingPipeline", return_value=pipeline) as pipeline_cls,
            ):
                main_module.main()

        run_temporal_chunks_mock.assert_not_called()
        pipeline_cls.assert_called_once_with(config)
        pipeline.run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
