from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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
from football_tracking.pipeline import BallTrackingPipeline
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
            priors=SelectionPriorsConfig(player_tracks_path=player_tracks_path),
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


if __name__ == "__main__":
    unittest.main()
