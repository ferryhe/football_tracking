from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from football_tracking.chunk_runner import build_chunk_config, enforce_raw_chunk_config, run_chunk, write_chunk_config
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
        pipeline.run.assert_called_once_with()

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
        pipeline.run.assert_called_once_with()
        log_exception.assert_called_once()


class ChunkWorkerCliTests(unittest.TestCase):
    def test_main_returns_run_chunk_exit_code(self) -> None:
        from football_tracking import chunk_worker

        with tempfile.TemporaryDirectory() as temp_name:
            config_path = Path(temp_name) / "chunk_config.yaml"

            with patch("football_tracking.chunk_worker.run_chunk", return_value=7) as run_chunk_mock:
                exit_code = chunk_worker.main(["--config", str(config_path)])

        self.assertEqual(7, exit_code)
        run_chunk_mock.assert_called_once_with(config_path)


if __name__ == "__main__":
    unittest.main()
