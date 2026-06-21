from __future__ import annotations

import csv
import importlib.util
import json
import tempfile
import unittest
from concurrent.futures import CancelledError
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from football_tracking.chunk_runner import (
    build_chunk_config,
    enforce_raw_chunk_config,
    run_chunk,
    run_temporal_chunks,
    write_chunk_config,
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
from football_tracking.temporal_chunks import TemporalChunk


def load_backend_main_module():
    module_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("backend_main_for_test", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load backend main module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_app_config(base_dir: Path) -> AppConfig:
    return AppConfig(
        input_video=base_dir / "data" / "input.mp4",
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

            with (
                patch("football_tracking.chunk_runner.BallTrackingPipeline") as pipeline_cls,
                patch("football_tracking.chunk_runner.logger.exception") as log_exception,
            ):
                exit_code = run_chunk(missing_config)

        self.assertNotEqual(0, exit_code)
        pipeline_cls.assert_not_called()
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
            for call_item in run_chunk_mock.call_args_list:
                self.assertIs(progress_callback, call_item.kwargs["progress_callback"])
                self.assertIs(should_cancel, call_item.kwargs["should_cancel"])
            stitch.assert_called_once_with(
                chunks,
                [chunks_root / "chunk_0000", chunks_root / "chunk_0001"],
                config.output_dir,
                output_config=config.output,
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

            self.assertEqual(2, run_chunk_mock.call_count)
            stitch.assert_not_called()
            postprocessor_cls.assert_not_called()
            follow_cam_cls.assert_not_called()

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

            self.assertEqual(1, run_chunk_mock.call_count)
            stitch.assert_not_called()


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
