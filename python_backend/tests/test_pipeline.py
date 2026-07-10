from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import CancelledError
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

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
    SceneZoneConfig,
    SelectionConfig,
    SelectionPriorsConfig,
    TrackingConfig,
)
from football_tracking.detector_candidate_contract import (
    RuntimeTrackingContractWriter,
    compute_candidate_source_sha256,
)
from football_tracking.exporter import TrackingContractWriteError, TrackingExporter
from football_tracking.pipeline import BallTrackingPipeline
from football_tracking.tracking_contracts import load_tracking_contract
from football_tracking.types import Candidate, TrackerContext, TrackState


def make_candidate(frame_index: int, center: tuple[float, float], confidence: float) -> Candidate:
    x, y = center
    return Candidate(
        frame_index=frame_index,
        x1=x - 4.0,
        y1=y - 4.0,
        x2=x + 4.0,
        y2=y + 4.0,
        confidence=confidence,
    )


class StaticDetector:
    def __init__(self, candidates: list[Candidate]) -> None:
        self.candidates = candidates

    def detect(self, frame, frame_index: int) -> list[Candidate]:
        return self.candidates


class ReacquireDetector:
    def detect(self, frame, frame_index: int) -> list[Candidate]:
        return []

    def detect_direct_in_roi(self, frame, frame_index: int, **kwargs: object) -> list[Candidate]:
        return [make_candidate(frame_index, (80.0, 50.0), confidence=0.65)]


class MutatingMockSourceDetector:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def detect(self, frame, frame_index: int) -> list[Candidate]:
        self.config.mock.background_color += 1
        return [make_candidate(frame_index, (80.0, 50.0), confidence=0.65)]


class InvalidFirstCandidateDetector:
    def __init__(self) -> None:
        self.calls = 0

    def detect(self, frame, frame_index: int) -> list[Candidate]:
        self.calls += 1
        candidate = make_candidate(frame_index, (80.0, 50.0), confidence=0.65)
        if frame_index == 0:
            candidate.x2 = candidate.x1
        return [candidate]


def make_pipeline_config(output_dir: Path, player_tracks_path: Path | None = None) -> AppConfig:
    return AppConfig(
        input_video=output_dir / "input.mp4",
        output_dir=output_dir,
        logging=LoggingConfig(save_debug_jsonl=False),
        detector=DetectorConfig(
            model_path=output_dir / "weights" / "football_ball_yolo.pt",
            device="cpu",
            use_half=False,
        ),
        sahi=SahiConfig(),
        filtering=FilteringConfig(),
        scene_bias=SceneBiasConfig(
            enabled=True,
            ground_zones=[
                SceneZoneConfig(
                    name="main_pitch",
                    points=((0, 0), (100, 0), (100, 100), (0, 100)),
                )
            ],
            positive_rois=[
                SceneZoneConfig(
                    name="main_pitch_buffer",
                    points=((-20, -20), (120, -20), (120, 120), (-20, 120)),
                )
            ],
        ),
        selection=SelectionConfig(
            priors=SelectionPriorsConfig(enabled=True, player_tracks_path=player_tracks_path),
        ),
        tracking=TrackingConfig(kalman_enabled=False),
        output=OutputConfig(
            save_video=False,
            save_frames=False,
            save_csv=False,
            save_debug_jsonl=False,
        ),
        postprocess=PostprocessConfig(enabled=False),
        follow_cam=FollowCamConfig(enabled=False),
        runtime=RuntimeConfig(use_gpu_if_available=False),
        mock=MockConfig(enabled=True, frame_width=220, frame_height=120, frame_count=1),
    )


class BallTrackingPipelinePriorTests(unittest.TestCase):
    def test_process_frame_attaches_pitch_and_player_priors_to_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            player_tracks_path = output_dir / "player_tracks.json"
            player_tracks_path.write_text(
                json.dumps(
                    {
                        "tracks": [
                            {
                                "id": "P001",
                                "samples": [
                                    {
                                        "frame": 11,
                                        "foot_point": {"x": 80.0, "y": 50.0},
                                        "confidence": 0.95,
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            pipeline = BallTrackingPipeline(make_pipeline_config(output_dir, player_tracks_path))
            near_player = make_candidate(12, (80.0, 50.0), confidence=0.55)
            off_pitch_noise = make_candidate(12, (103.0, 50.0), confidence=0.75)
            pipeline.detector = StaticDetector([off_pitch_noise, near_player])

            result = pipeline._process_frame(np.zeros((120, 220, 3), dtype=np.uint8), frame_index=12)

        self.assertIsNotNone(pipeline.selection_pitch_calibration)
        self.assertIsNotNone(pipeline.selection_player_tracks_report)
        self.assertIsNotNone(result.point)
        assert result.point is not None
        self.assertEqual((80.0, 50.0), (result.point.x, result.point.y))
        near_score = next(score for score in result.selected_candidate_scores if score.candidate is near_player)
        noise_score = next(score for score in result.selected_candidate_scores if score.candidate is off_pitch_noise)
        self.assertGreater(near_score.player_foot_bonus, 0.0)
        self.assertTrue(noise_score.outside_pitch)
        self.assertLess(noise_score.pitch_boundary_penalty, 0.0)

    def test_pipeline_does_not_auto_load_stale_player_tracks_from_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "player_tracks.json").write_text(
                json.dumps({"tracks": [{"samples": [{"frame": 1, "foot_point": {"x": 1.0, "y": 1.0}}]}]}),
                encoding="utf-8",
            )

            pipeline = BallTrackingPipeline(make_pipeline_config(output_dir))

        self.assertIsNone(pipeline.selection_player_tracks_report)

    def test_reacquire_context_preserves_selection_priors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            pipeline = BallTrackingPipeline(make_pipeline_config(Path(temp_name)))
        context = TrackerContext(
            state=TrackState.TRACKING,
            last_position=(10.0, 10.0),
            predicted_position=(12.0, 10.0),
            velocity=(2.0, 0.0),
            history_length=4,
            pitch_calibration={"image_to_pitch_matrix": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]},
            player_tracks_report={"tracks": []},
        )

        reacquire_context = pipeline._build_reacquire_context(context, burst_active=True)

        self.assertIsNot(reacquire_context, context)
        self.assertIs(reacquire_context.pitch_calibration, context.pitch_calibration)
        self.assertIs(reacquire_context.player_tracks_report, context.player_tracks_report)

    def test_process_frame_assigns_ids_to_primary_detector_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            pipeline = BallTrackingPipeline(make_pipeline_config(Path(temp_name)))
            candidates = [
                make_candidate(4, (40.0, 50.0), confidence=0.7),
                make_candidate(4, (80.0, 50.0), confidence=0.8),
            ]
            pipeline.detector = StaticDetector(candidates)

            result = pipeline._process_frame(np.zeros((120, 220, 3), dtype=np.uint8), frame_index=4)

        self.assertEqual(candidates, result.raw_candidates)
        self.assertTrue(all(candidate.candidate_id for candidate in result.raw_candidates))
        self.assertEqual(2, len({candidate.candidate_id for candidate in result.raw_candidates}))

    def test_process_frame_captures_dynamic_roi_reacquire_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            pipeline = BallTrackingPipeline(make_pipeline_config(Path(temp_name)))
            pipeline.config.scene_bias.dynamic_air_recovery.enabled = True
            pipeline.air_burst_frames_remaining = 1
            pipeline.detector = ReacquireDetector()
            pipeline.scene_bias.get_dynamic_air_window = lambda *args, **kwargs: (0, 0, 120, 100)

            result = pipeline._process_frame(np.zeros((120, 220, 3), dtype=np.uint8), frame_index=7)

        self.assertTrue(result.reacquire_attempted)
        self.assertEqual(1, result.raw_candidate_count)
        self.assertEqual(1, len(result.raw_candidates))
        self.assertIsNotNone(result.raw_candidates[0].candidate_id)

    def test_dynamic_roi_candidate_survives_second_filter_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            pipeline = BallTrackingPipeline(make_pipeline_config(Path(temp_name)))
            pipeline.config.scene_bias.dynamic_air_recovery.enabled = True
            pipeline.air_burst_frames_remaining = 1
            pipeline.detector = ReacquireDetector()
            pipeline.scene_bias.get_dynamic_air_window = lambda *args, **kwargs: (0, 0, 120, 100)
            pipeline.candidate_filter.filter = Mock(
                side_effect=[([], [], {}), RuntimeError("second filter failed")]
            )

            result = pipeline._process_frame(np.zeros((120, 220, 3), dtype=np.uint8), frame_index=9)

        self.assertEqual(1, result.raw_candidate_count)
        self.assertEqual(1, len(result.raw_candidates))
        self.assertIsNotNone(result.raw_candidates[0].candidate_id)
        self.assertIn("second filter failed", result.reason)

    def test_normal_mock_run_writes_candidate_populated_v2_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            config = make_pipeline_config(output_dir)
            config.mock.frame_count = 3
            pipeline = BallTrackingPipeline(config)

            pipeline.run()

            contract = load_tracking_contract(output_dir)

        self.assertEqual("loaded", contract["artifact_status"])
        self.assertEqual([], contract["validation_errors"])
        self.assertEqual(
            {
                "video_sha256": compute_candidate_source_sha256(config, use_precomputed=False),
                "fps": config.mock.fps,
                "width": config.mock.frame_width,
                "height": config.mock.frame_height,
                "frame_count": config.mock.frame_count,
            },
            contract["source"],
        )
        self.assertEqual([0, 1, 2], [frame["frame_index"] for frame in contract["frames"]])
        self.assertEqual(3, len(contract["candidates"]))
        self.assertTrue(all(candidate["candidate_id"] for candidate in contract["candidates"]))
        self.assertEqual([], contract["classifications"])
        self.assertEqual([], contract["decisions"])

    def test_normal_run_recomputes_source_scope_instead_of_trusting_internal_config_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            config = make_pipeline_config(output_dir)
            config.runtime.candidate_source_sha256 = "a" * 64
            expected_source_sha256 = compute_candidate_source_sha256(config, use_precomputed=False)
            pipeline = BallTrackingPipeline(config)

            pipeline.run()

            contract = load_tracking_contract(output_dir)

        self.assertEqual("loaded", contract["artifact_status"])
        self.assertTrue(
            all(
                candidate["candidate_id"].startswith(f"candidate-v1-{expected_source_sha256[:16]}-")
                for candidate in contract["candidates"]
            )
        )

    def test_disabled_contract_output_removes_stale_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            stale_path = output_dir / "tracking_contract.v2.json"
            stale_path.write_text("stale", encoding="utf-8")
            config = make_pipeline_config(output_dir)
            config.output.save_tracking_contract = False

            BallTrackingPipeline(config).run()

            self.assertFalse(stale_path.exists())

    def test_constructor_failure_removes_stale_contract_before_detector_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            stale_path = output_dir / "tracking_contract.v2.json"
            stale_path.write_text("stale", encoding="utf-8")
            config = make_pipeline_config(output_dir)

            with patch.object(BallTrackingPipeline, "_build_detector", side_effect=RuntimeError("detector failed")):
                with self.assertRaisesRegex(RuntimeError, "detector failed"):
                    BallTrackingPipeline(config)

            self.assertFalse(stale_path.exists())

    def test_cancelled_run_does_not_publish_partial_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            config = make_pipeline_config(output_dir)

            with self.assertRaises(CancelledError):
                BallTrackingPipeline(config).run(should_cancel=lambda: True)

            self.assertFalse((output_dir / "tracking_contract.v2.json").exists())
            self.assertEqual([], list(output_dir.glob(".tracking_contract.v2.json.*")))

    def test_source_change_during_run_prevents_contract_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            config = make_pipeline_config(output_dir)
            pipeline = BallTrackingPipeline(config)
            pipeline.detector = MutatingMockSourceDetector(config)

            with self.assertRaisesRegex(RuntimeError, "source changed before contract publication"):
                pipeline.run()

            self.assertFalse((output_dir / "tracking_contract.v2.json").exists())
            self.assertEqual([], list(output_dir.glob(".tracking_contract.v2.json.*")))

    def test_postprocess_source_change_and_failure_removes_published_contract(self) -> None:
        class OneFrameCapture:
            def __init__(self) -> None:
                self.read_count = 0

            def read(self):
                if self.read_count:
                    return False, None
                self.read_count += 1
                return True, np.zeros((120, 220, 3), dtype=np.uint8)

            def get(self, _property: int) -> float:
                return 1.0

            def release(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            config = make_pipeline_config(output_dir)
            config.input_video.write_bytes(b"source-a")
            pipeline = BallTrackingPipeline(config)
            pipeline.detector = StaticDetector([make_candidate(0, (80.0, 50.0), confidence=0.65)])
            config.mock.enabled = False
            config.postprocess.enabled = True
            pipeline._open_frame_source = lambda: (OneFrameCapture(), 220, 120, 20.0)

            def mutate_source_and_fail() -> None:
                config.input_video.write_bytes(b"source-b-with-new-size")
                raise RuntimeError("postprocess failed")

            with patch("football_tracking.pipeline.TrackPostprocessor") as postprocessor_cls:
                postprocessor_cls.return_value.run.side_effect = mutate_source_and_fail
                with self.assertRaisesRegex(RuntimeError, "source changed during post-tracking outputs"):
                    pipeline.run()

            self.assertFalse((output_dir / "tracking_contract.v2.json").exists())
            self.assertEqual([], list(output_dir.glob(".tracking_contract.v2.json.*")))

    def test_contract_capture_failure_aborts_without_processing_later_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            config = make_pipeline_config(output_dir)
            config.mock.frame_count = 3
            config.output.save_csv = True
            detector = InvalidFirstCandidateDetector()
            pipeline = BallTrackingPipeline(config)
            pipeline.detector = detector

            with self.assertRaisesRegex(TrackingContractWriteError, "frame 0"):
                pipeline.run()

            csv_lines = (output_dir / config.output.csv_name).read_text(encoding="utf-8-sig").splitlines()
            self.assertEqual(1, detector.calls)
            self.assertEqual(1, len(csv_lines))
            self.assertFalse((output_dir / "tracking_contract.v2.json").exists())
            self.assertEqual([], list(output_dir.glob(".tracking_contract.v2.json.*")))

    def test_exporter_close_failure_still_closes_contract_writer_and_releases_lock(self) -> None:
        class CloseAfterDelegateFailure:
            def __init__(self, delegate) -> None:
                self.delegate = delegate

            def __getattr__(self, name: str):
                return getattr(self.delegate, name)

            def close(self) -> None:
                self.delegate.close()
                raise OSError("csv close failed")

        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            config = make_pipeline_config(output_dir)
            config.output.save_csv = True
            source_sha256 = compute_candidate_source_sha256(config, use_precomputed=False)
            exporter = TrackingExporter(
                output_dir=output_dir,
                config=config.output,
                logging_config=config.logging,
                frame_size=(config.mock.frame_width, config.mock.frame_height),
                fps=config.mock.fps,
                candidate_source_sha256=source_sha256,
            )
            contract_writer = exporter.contract_writer
            self.assertIsNotNone(contract_writer)
            exporter.csv_file = CloseAfterDelegateFailure(exporter.csv_file)

            with self.assertRaisesRegex(OSError, "csv close failed"):
                exporter.close(publish_tracking_contract=True)

            assert contract_writer is not None
            self.assertTrue(contract_writer._closed)
            self.assertEqual([], list(output_dir.glob(".tracking_contract.v2.json.*")))
            next_writer = RuntimeTrackingContractWriter(output_dir, source_sha256)
            next_writer.close(publish=False)

    def test_rejected_concurrent_exporter_does_not_delete_locked_published_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            config = make_pipeline_config(output_dir)
            source_sha256 = compute_candidate_source_sha256(config, use_precomputed=False)
            active_writer = RuntimeTrackingContractWriter(output_dir, source_sha256)
            assert active_writer._frames_file is not None
            assert active_writer._candidates_file is not None
            active_writer._frames_file.close()
            active_writer._candidates_file.close()
            active_writer._publish()
            final_path = output_dir / "tracking_contract.v2.json"

            with self.assertRaisesRegex(RuntimeError, "another tracking contract writer"):
                TrackingExporter(
                    output_dir=output_dir,
                    config=config.output,
                    logging_config=config.logging,
                    frame_size=(config.mock.frame_width, config.mock.frame_height),
                    fps=config.mock.fps,
                    candidate_source_sha256=source_sha256,
                )

            self.assertTrue(final_path.exists())
            active_writer.close(publish=False)


if __name__ == "__main__":
    unittest.main()
