from __future__ import annotations

import unittest

from football_tracking.config import SelectionConfig, SelectionPriorsConfig, TrackingConfig
from football_tracking.selection_priors import nearest_recent_player_foot_distance_px
from football_tracking.selector import UniqueBallSelector
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


def make_context(
    *,
    anchor: tuple[float, float],
    player_tracks_report: dict[str, object] | None = None,
    pitch_calibration: dict[str, object] | None = None,
) -> TrackerContext:
    return TrackerContext(
        state=TrackState.TRACKING,
        last_position=anchor,
        predicted_position=anchor,
        gating_radius=120.0,
        velocity=(0.0, 0.0),
        history_length=6,
        player_tracks_report=player_tracks_report,
        pitch_calibration=pitch_calibration,
    )


class UniqueBallSelectorPriorTests(unittest.TestCase):
    def test_player_foot_prior_can_beat_slightly_higher_confidence_noise(self) -> None:
        frame_index = 12
        near_player = make_candidate(frame_index, (150.0, 100.0), confidence=0.55)
        noise = make_candidate(frame_index, (50.0, 100.0), confidence=0.65)
        player_tracks_report = {
            "tracks": [
                {
                    "id": "P001",
                    "samples": [
                        {
                            "frame": 11,
                            "foot_point": {"x": 150.0, "y": 100.0},
                            "confidence": 0.9,
                            "team": "home",
                        }
                    ],
                }
            ]
        }
        selector = UniqueBallSelector(SelectionConfig(priors=SelectionPriorsConfig(enabled=True)), TrackingConfig())

        decision = selector.select(
            [noise, near_player],
            make_context(anchor=(100.0, 100.0), player_tracks_report=player_tracks_report),
            frame_index,
        )

        self.assertIs(decision.selected_candidate, near_player)
        selected_score = next(score for score in decision.candidate_scores if score.candidate is near_player)
        noise_score = next(score for score in decision.candidate_scores if score.candidate is noise)
        self.assertGreater(selected_score.prior_score, 0.0)
        self.assertGreater(selected_score.player_foot_bonus, 0.0)
        self.assertEqual(0.0, selected_score.nearest_player_foot_distance_px)
        self.assertEqual(0.0, noise_score.player_foot_bonus)
        debug = selected_score.to_debug_dict()
        self.assertGreater(debug["prior_score"], 0.0)
        self.assertGreater(debug["player_foot_bonus"], 0.0)
        self.assertIn("nearest_player_foot_distance_px", debug)
        self.assertIn("player_foot_bonus", selected_score.reason)
        self.assertIn("prior_score", selected_score.reason)

    def test_pitch_boundary_prior_penalizes_out_of_pitch_candidate(self) -> None:
        frame_index = 7
        in_pitch = make_candidate(frame_index, (99.0, 50.0), confidence=0.65)
        outside_pitch = make_candidate(frame_index, (101.0, 50.0), confidence=0.70)
        pitch_calibration = {
            "image_to_pitch_matrix": [
                [1.05, 0.0, 0.0],
                [0.0, 0.68, 0.0],
                [0.0, 0.0, 1.0],
            ],
            "pitch_dimensions": {"length_m": 105.0, "width_m": 68.0},
        }
        selector = UniqueBallSelector(SelectionConfig(priors=SelectionPriorsConfig(enabled=True)), TrackingConfig())

        decision = selector.select(
            [outside_pitch, in_pitch],
            make_context(anchor=(100.0, 50.0), pitch_calibration=pitch_calibration),
            frame_index,
        )

        self.assertIs(decision.selected_candidate, in_pitch)
        outside_score = next(score for score in decision.candidate_scores if score.candidate is outside_pitch)
        self.assertLess(outside_score.pitch_boundary_penalty, 0.0)
        self.assertLess(outside_score.prior_score, 0.0)
        debug = outside_score.to_debug_dict()
        self.assertTrue(debug["outside_pitch"])
        self.assertLess(debug["pitch_boundary_penalty"], 0.0)
        self.assertEqual([106.05, 34.0], debug["pitch_point_m"])
        self.assertIn("pitch_boundary_penalty", outside_score.reason)
        self.assertIn("outside_pitch=True", outside_score.reason)

    def test_no_prior_context_keeps_existing_motion_confidence_ranking(self) -> None:
        frame_index = 3
        lower_confidence = make_candidate(frame_index, (95.0, 100.0), confidence=0.40)
        higher_confidence = make_candidate(frame_index, (105.0, 100.0), confidence=0.80)
        selector = UniqueBallSelector(SelectionConfig(), TrackingConfig())

        decision = selector.select(
            [lower_confidence, higher_confidence],
            make_context(anchor=(100.0, 100.0)),
            frame_index,
        )

        self.assertIs(decision.selected_candidate, higher_confidence)
        higher_score = next(score for score in decision.candidate_scores if score.candidate is higher_confidence)
        self.assertEqual(0.0, higher_score.prior_score)
        self.assertEqual(0.0, higher_score.player_foot_bonus)
        self.assertEqual(0.0, higher_score.pitch_boundary_penalty)
        self.assertAlmostEqual(0.6732, higher_score.total_score, places=4)
        self.assertNotIn("prior_score", higher_score.reason)

    def test_malformed_player_track_frames_are_ignored(self) -> None:
        report = {
            "tracks": [
                {
                    "samples": [
                        {"frame": "Infinity", "foot_point": {"x": 0.0, "y": 0.0}},
                        {"frame": "NaN", "foot_point": {"x": 5.0, "y": 5.0}},
                        {"frame": "not-a-frame", "foot_point": {"x": 7.0, "y": 7.0}},
                        {"frame": 5, "foot_point": {"x": 10.0, "y": 10.0}},
                    ],
                }
            ]
        }

        distance = nearest_recent_player_foot_distance_px(
            report=report,
            frame_index=5,
            point=(10.0, 10.0),
            frame_window=1,
        )

        self.assertEqual(0.0, distance)


if __name__ == "__main__":
    unittest.main()
