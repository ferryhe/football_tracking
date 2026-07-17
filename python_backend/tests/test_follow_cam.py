from __future__ import annotations

import csv
import json
import os
import subprocess
import tempfile
import unittest
from concurrent.futures import CancelledError
from pathlib import Path
from unittest import mock

import numpy as np
import yaml

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
    TrackingConfig,
    load_config,
)
from football_tracking.follow_cam import CameraPathEntry, FollowCamFrame, FollowCamGenerator
from football_tracking.types import OutputStatus


class DummyCapture:
    def __init__(self, frame_count: int, width: int = 1280, height: int = 720) -> None:
        self.frames_remaining = frame_count
        self.frame = np.zeros((height, width, 3), dtype=np.uint8)
        self.released = False

    def isOpened(self) -> bool:
        return True

    def get(self, prop_id: int) -> float:
        if prop_id == 5:
            return 20.0
        if prop_id == 3:
            return float(self.frame.shape[1])
        if prop_id == 4:
            return float(self.frame.shape[0])
        return 0.0

    def read(self) -> tuple[bool, np.ndarray | None]:
        if self.frames_remaining <= 0:
            return False, None
        self.frames_remaining -= 1
        return True, self.frame.copy()

    def release(self) -> None:
        self.released = True


class DummyWriter:
    def __init__(self) -> None:
        self.frames: list[np.ndarray] = []

    def write(self, frame: np.ndarray) -> None:
        self.frames.append(frame)

    def release(self) -> None:
        pass


class DummyVideoCapture:
    def __init__(self) -> None:
        self.released = False

    def isOpened(self) -> bool:
        return True

    def get(self, prop_id: int) -> float:
        if prop_id == 5:
            return 20.0
        if prop_id == 3:
            return 1280.0
        if prop_id == 4:
            return 720.0
        return 0.0

    def release(self) -> None:
        self.released = True


class FollowCamTests(unittest.TestCase):
    def test_load_config_parses_profile_defaults_and_action_center_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            repo_root = Path(temp_name)
            player_tracks_path = repo_root / "outputs" / "players.json"
            config_path = self.write_yaml(
                repo_root,
                {
                    "input_video": "./data/input.mp4",
                    "output_dir": "./outputs/run_a",
                    "detector": {"model_path": "./weights/model.pt"},
                    "follow_cam": {
                        "profile": "tactical",
                        "max_crop_height": 1500,
                        "lost_action_hold_frames": 120,
                        "lost_action_hold_edge_margin_ratio": 0.20,
                        "lost_action_hold_min_confidence": 0.33,
                        "lost_action_hold_smoothing": 0.06,
                        "action_center": {
                            "enabled": True,
                            "player_tracks_path": "./outputs/players.json",
                        },
                    },
                },
            )

            config = load_config(config_path)

        self.assertEqual("tactical", config.follow_cam.profile)
        self.assertTrue(config.follow_cam.action_center_enabled)
        self.assertEqual(player_tracks_path.resolve(), config.follow_cam.action_center_player_tracks_path)
        self.assertGreater(config.follow_cam.min_crop_height, FollowCamConfig().min_crop_height)
        self.assertEqual(1500, config.follow_cam.max_crop_height)
        self.assertEqual(120, config.follow_cam.lost_action_hold_frames)
        self.assertEqual(0.20, config.follow_cam.lost_action_hold_edge_margin_ratio)
        self.assertEqual(0.33, config.follow_cam.lost_action_hold_min_confidence)
        self.assertEqual(0.06, config.follow_cam.lost_action_hold_smoothing)
        self.assertEqual("custom", FollowCamConfig().profile)
        self.assertFalse(FollowCamConfig().action_center_enabled)

    def test_load_config_rejects_non_dict_follow_cam_with_clear_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            repo_root = Path(temp_name)
            config_path = self.write_yaml(
                repo_root,
                {
                    "input_video": "./data/input.mp4",
                    "output_dir": "./outputs/run_a",
                    "detector": {"model_path": "./weights/model.pt"},
                    "follow_cam": "enabled",
                },
            )

            with self.assertRaisesRegex(ValueError, "follow_cam must be a dict or null"):
                load_config(config_path)

    def test_profile_preserves_explicit_legacy_value_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            repo_root = Path(temp_name)
            config_path = self.write_yaml(
                repo_root,
                {
                    "input_video": "./data/input.mp4",
                    "output_dir": "./outputs/run_a",
                    "detector": {"model_path": "./weights/model.pt"},
                    "follow_cam": {
                        "profile": "tactical",
                        "max_crop_height": 1260,
                    },
                },
            )

            config = load_config(config_path)

        self.assertEqual("tactical", config.follow_cam.profile)
        self.assertGreater(config.follow_cam.min_crop_height, FollowCamConfig().min_crop_height)
        self.assertEqual(1260, config.follow_cam.max_crop_height)

    def test_tactical_profile_yields_wider_steadier_crop_than_broadcast_and_custom(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            repo_root = Path(temp_name)
            custom = load_config(
                self.write_yaml(
                    repo_root,
                    {
                        "input_video": "./data/input.mp4",
                        "output_dir": "./outputs/run_a",
                        "detector": {"model_path": "./weights/model.pt"},
                        "follow_cam": {"profile": "custom"},
                    },
                )
            )
            broadcast = load_config(
                self.write_yaml(
                    repo_root,
                    {
                        "input_video": "./data/input.mp4",
                        "output_dir": "./outputs/run_a",
                        "detector": {"model_path": "./weights/model.pt"},
                        "follow_cam": {"profile": "broadcast"},
                    },
                )
            )
            tactical = load_config(
                self.write_yaml(
                    repo_root,
                    {
                        "input_video": "./data/input.mp4",
                        "output_dir": "./outputs/run_a",
                        "detector": {"model_path": "./weights/model.pt"},
                        "follow_cam": {"profile": "tactical"},
                    },
                )
            )
        high_speed_low_confidence_frame = FollowCamFrame(
            frame_index=10,
            x=600.0,
            y=320.0,
            confidence=0.12,
            status=OutputStatus.PREDICTED,
        )

        custom_height, _ = FollowCamGenerator(custom)._desired_crop_height(
            high_speed_low_confidence_frame,
            speed=140.0,
            source_height=1440,
        )
        broadcast_height, _ = FollowCamGenerator(broadcast)._desired_crop_height(
            high_speed_low_confidence_frame,
            speed=140.0,
            source_height=1440,
        )
        tactical_height, _ = FollowCamGenerator(tactical)._desired_crop_height(
            high_speed_low_confidence_frame,
            speed=140.0,
            source_height=1440,
        )

        self.assertGreater(tactical_height, custom_height)
        self.assertGreater(tactical_height, broadcast_height)
        self.assertLess(tactical.follow_cam.glide_pan_smoothing, broadcast.follow_cam.glide_pan_smoothing)
        self.assertLess(tactical.follow_cam.max_zoom_out_per_frame, broadcast.follow_cam.max_zoom_out_per_frame)

    def test_custom_profile_keeps_legacy_crop_height_calculation(self) -> None:
        generator = FollowCamGenerator(self.make_app_config(FollowCamConfig()))
        steady_frame = FollowCamFrame(
            frame_index=1,
            x=500.0,
            y=300.0,
            confidence=0.90,
            status=OutputStatus.DETECTED,
        )
        high_speed_low_confidence_frame = FollowCamFrame(
            frame_index=2,
            x=600.0,
            y=320.0,
            confidence=0.12,
            status=OutputStatus.PREDICTED,
        )

        steady_height, steady_ratio = generator._desired_crop_height(
            steady_frame,
            speed=0.0,
            source_height=1440,
        )
        wide_height, wide_ratio = generator._desired_crop_height(
            high_speed_low_confidence_frame,
            speed=140.0,
            source_height=1440,
        )

        self.assertEqual(900, steady_height)
        self.assertEqual(0.0, steady_ratio)
        self.assertEqual(1260, wide_height)
        self.assertEqual(1.0, wide_ratio)

    def test_action_center_debug_fields_are_written_to_camera_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            generator = FollowCamGenerator(
                self.make_app_config(
                    FollowCamConfig(
                        action_center_enabled=True,
                        draw_ball_marker=False,
                        draw_frame_text=False,
                    ),
                    output_dir=Path(temp_name),
                )
            )
            path = Path(temp_name) / "camera_path.csv"
            entry = CameraPathEntry(
                frame_index=7,
                center_x=500.0,
                center_y=300.0,
                crop_x1=100,
                crop_y1=50,
                crop_x2=900,
                crop_y2=500,
                crop_width=800,
                crop_height=450,
                source_status="Detected",
                track_x=520.0,
                track_y=310.0,
                confidence=0.81,
                speed=12.5,
                zoom_out_ratio=0.25,
                pan_mode="glide",
                profile="custom",
                action_center_enabled=True,
                action_center_x=540.0,
                action_center_y=330.0,
                action_center_source="ball_players",
                action_center_player_count=3,
            )

            generator._write_camera_path(path, [entry])
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual("custom", rows[0]["Profile"])
        self.assertEqual("1", rows[0]["ActionCenterEnabled"])
        self.assertEqual("520.00", rows[0]["TrackX"])
        self.assertEqual("540.00", rows[0]["ActionCenterX"])
        self.assertEqual("330.00", rows[0]["ActionCenterY"])
        self.assertEqual("ball_players", rows[0]["ActionCenterSource"])
        self.assertEqual("3", rows[0]["ActionCenterPlayerCount"])

    def test_render_uses_action_center_without_changing_raw_track_debug(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            player_tracks_path = output_dir / "players.json"
            player_tracks_path.write_text(
                json.dumps(
                    {
                        "tracks": [
                            {
                                "id": "P001",
                                "samples": [
                                    {"frame": 0, "foot_point": {"x": 900.0, "y": 620.0}},
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            generator = FollowCamGenerator(
                self.make_app_config(
                    FollowCamConfig(
                        action_center_enabled=True,
                        action_center_player_tracks_path=player_tracks_path,
                        draw_ball_marker=False,
                        draw_frame_text=False,
                    ),
                    output_dir=output_dir,
                )
            )
            entries = generator._render_follow_cam(
                capture=DummyCapture(frame_count=1),
                writer=DummyWriter(),
                frames=[
                    FollowCamFrame(
                        frame_index=0,
                        x=400.0,
                        y=300.0,
                        confidence=0.90,
                        status=OutputStatus.DETECTED,
                    )
                ],
                source_width=1280,
                source_height=720,
            )

        self.assertEqual(400.0, entries[0].track_x)
        self.assertEqual(300.0, entries[0].track_y)
        self.assertEqual("ball_players", entries[0].action_center_source)
        self.assertEqual(1, entries[0].action_center_player_count)
        action_center_x = entries[0].action_center_x
        action_center_y = entries[0].action_center_y
        track_x = entries[0].track_x
        track_y = entries[0].track_y
        self.assertIsNotNone(action_center_x)
        self.assertIsNotNone(action_center_y)
        self.assertIsNotNone(track_x)
        self.assertIsNotNone(track_y)
        assert action_center_x is not None and track_x is not None
        assert action_center_y is not None and track_y is not None
        self.assertGreater(action_center_x, track_x)
        self.assertGreater(action_center_y, track_y)

    def test_lost_tail_after_right_edge_action_holds_camera_target(self) -> None:
        generator = FollowCamGenerator(
            self.make_app_config(
                FollowCamConfig(
                    target_width=640,
                    target_height=360,
                    min_crop_height=360,
                    max_crop_height=360,
                    lost_recenter_frames=2,
                    recenter_smoothing=1.0,
                    draw_ball_marker=False,
                    draw_frame_text=False,
                )
            )
        )
        frames = [
            FollowCamFrame(frame_index, 1180.0, 650.0, 0.90, OutputStatus.DETECTED)
            for frame_index in range(100, 106)
        ]
        frames.extend(
            FollowCamFrame(frame_index, None, None, 0.0, OutputStatus.LOST)
            for frame_index in range(106, 112)
        )

        entries = generator._render_follow_cam(
            capture=DummyCapture(frame_count=len(frames)),
            writer=DummyWriter(),
            frames=frames,
            source_width=1280,
            source_height=720,
        )

        lost_entries = [entry for entry in entries if entry.source_status == OutputStatus.LOST.value]
        self.assertGreater(lost_entries[-1].center_x, 900.0)
        self.assertIn("action_hold", {entry.pan_mode for entry in lost_entries})

    def test_lost_tail_action_hold_uses_glide_pan_cap(self) -> None:
        generator = FollowCamGenerator(
            self.make_app_config(
                FollowCamConfig(
                    glide_max_pan_per_frame_x=12.0,
                    glide_max_pan_per_frame_y=8.0,
                    lost_action_hold_smoothing=1.0,
                )
            )
        )

        next_center = generator._move_towards_action_hold((100.0, 100.0), (1000.0, 600.0))

        self.assertEqual((112.0, 108.0), next_center)

    def test_lost_tail_after_midfield_action_recenters_to_home(self) -> None:
        generator = FollowCamGenerator(
            self.make_app_config(
                FollowCamConfig(
                    target_width=640,
                    target_height=360,
                    min_crop_height=360,
                    max_crop_height=360,
                    lost_recenter_frames=2,
                    recenter_smoothing=1.0,
                    draw_ball_marker=False,
                    draw_frame_text=False,
                )
            )
        )
        frames = [
            FollowCamFrame(frame_index, 500.0, 360.0, 0.90, OutputStatus.DETECTED)
            for frame_index in range(100, 106)
        ]
        frames.extend(
            FollowCamFrame(frame_index, None, None, 0.0, OutputStatus.LOST)
            for frame_index in range(106, 112)
        )

        entries = generator._render_follow_cam(
            capture=DummyCapture(frame_count=len(frames)),
            writer=DummyWriter(),
            frames=frames,
            source_width=1280,
            source_height=720,
        )

        self.assertAlmostEqual(640.0, entries[-1].center_x)
        self.assertNotIn("action_hold", {entry.pan_mode for entry in entries})

    def test_reliable_midfield_detection_clears_lost_tail_action_hold_seed(self) -> None:
        generator = FollowCamGenerator(
            self.make_app_config(
                FollowCamConfig(
                    target_width=640,
                    target_height=360,
                    min_crop_height=360,
                    max_crop_height=360,
                    lost_recenter_frames=2,
                    recenter_smoothing=1.0,
                    draw_ball_marker=False,
                    draw_frame_text=False,
                )
            )
        )
        frames = [
            FollowCamFrame(frame_index, 1180.0, 650.0, 0.90, OutputStatus.DETECTED)
            for frame_index in range(100, 103)
        ]
        frames.extend(
            FollowCamFrame(frame_index, 640.0, 360.0, 0.90, OutputStatus.DETECTED)
            for frame_index in range(103, 112)
        )
        frames.extend(
            FollowCamFrame(frame_index, None, None, 0.0, OutputStatus.LOST)
            for frame_index in range(112, 118)
        )

        entries = generator._render_follow_cam(
            capture=DummyCapture(frame_count=len(frames)),
            writer=DummyWriter(),
            frames=frames,
            source_width=1280,
            source_height=720,
        )

        lost_entries = [entry for entry in entries if entry.source_status == OutputStatus.LOST.value]
        self.assertNotIn("action_hold", {entry.pan_mode for entry in lost_entries})
        self.assertAlmostEqual(640.0, entries[-1].center_x)

    def test_bottom_center_detection_does_not_seed_lost_tail_action_hold(self) -> None:
        generator = FollowCamGenerator(
            self.make_app_config(
                FollowCamConfig(
                    target_width=640,
                    target_height=360,
                    min_crop_height=360,
                    max_crop_height=360,
                    lost_recenter_frames=2,
                    recenter_smoothing=1.0,
                    draw_ball_marker=False,
                    draw_frame_text=False,
                )
            )
        )
        frames = [
            FollowCamFrame(frame_index, 640.0, 650.0, 0.90, OutputStatus.DETECTED)
            for frame_index in range(100, 106)
        ]
        frames.extend(
            FollowCamFrame(frame_index, None, None, 0.0, OutputStatus.LOST)
            for frame_index in range(106, 112)
        )

        entries = generator._render_follow_cam(
            capture=DummyCapture(frame_count=len(frames)),
            writer=DummyWriter(),
            frames=frames,
            source_width=1280,
            source_height=720,
        )

        self.assertAlmostEqual(640.0, entries[-1].center_x)
        self.assertNotIn("action_hold", {entry.pan_mode for entry in entries})

    def test_lost_tail_action_hold_expires_and_recenters(self) -> None:
        generator = FollowCamGenerator(
            self.make_app_config(
                FollowCamConfig(
                    target_width=640,
                    target_height=360,
                    min_crop_height=360,
                    max_crop_height=360,
                    lost_recenter_frames=2,
                    lost_action_hold_frames=3,
                    recenter_smoothing=1.0,
                    draw_ball_marker=False,
                    draw_frame_text=False,
                )
            )
        )
        frames = [
            FollowCamFrame(frame_index, 1180.0, 650.0, 0.90, OutputStatus.DETECTED)
            for frame_index in range(100, 103)
        ]
        frames.extend(
            FollowCamFrame(frame_index, None, None, 0.0, OutputStatus.LOST)
            for frame_index in range(103, 109)
        )

        entries = generator._render_follow_cam(
            capture=DummyCapture(frame_count=len(frames)),
            writer=DummyWriter(),
            frames=frames,
            source_width=1280,
            source_height=720,
        )

        lost_entries = [entry for entry in entries if entry.source_status == OutputStatus.LOST.value]
        self.assertIn("action_hold", {entry.pan_mode for entry in lost_entries})
        self.assertEqual("hold", lost_entries[-1].pan_mode)
        self.assertAlmostEqual(640.0, entries[-1].center_x)

    def test_low_confidence_predicted_edge_point_does_not_replace_lost_action_hold_seed(self) -> None:
        generator = FollowCamGenerator(
            self.make_app_config(
                FollowCamConfig(
                    target_width=640,
                    target_height=360,
                    min_crop_height=360,
                    max_crop_height=360,
                    lost_recenter_frames=2,
                    lost_action_hold_frames=30,
                    lost_action_hold_min_confidence=0.25,
                    recenter_smoothing=1.0,
                    draw_ball_marker=False,
                    draw_frame_text=False,
                )
            )
        )
        frames = [
            FollowCamFrame(frame_index, 1180.0, 650.0, 0.90, OutputStatus.DETECTED)
            for frame_index in range(100, 106)
        ]
        frames.extend(
            [
                FollowCamFrame(106, 0.0, 650.0, 0.08, OutputStatus.PREDICTED),
                FollowCamFrame(107, None, None, 0.0, OutputStatus.LOST),
                FollowCamFrame(108, None, None, 0.0, OutputStatus.LOST),
                FollowCamFrame(109, None, None, 0.0, OutputStatus.LOST),
                FollowCamFrame(110, None, None, 0.0, OutputStatus.LOST),
                FollowCamFrame(111, None, None, 0.0, OutputStatus.LOST),
            ]
        )

        entries = generator._render_follow_cam(
            capture=DummyCapture(frame_count=len(frames)),
            writer=DummyWriter(),
            frames=frames,
            source_width=1280,
            source_height=720,
        )

        lost_entries = [entry for entry in entries if entry.source_status == OutputStatus.LOST.value]
        self.assertTrue(all(entry.action_center_x == 1180.0 for entry in lost_entries))
        self.assertGreater(lost_entries[-1].center_x, 900.0)

    def test_low_confidence_predicted_edge_point_does_not_draw_marker(self) -> None:
        generator = FollowCamGenerator(
            self.make_app_config(
                FollowCamConfig(
                    target_width=640,
                    target_height=360,
                    min_crop_height=720,
                    max_crop_height=720,
                    lost_action_hold_min_confidence=0.25,
                    draw_ball_marker=True,
                    draw_frame_text=False,
                )
            )
        )
        writer = DummyWriter()

        entries = generator._render_follow_cam(
            capture=DummyCapture(frame_count=1, width=1280, height=720),
            writer=writer,
            frames=[
                FollowCamFrame(100, 0.0, 360.0, 0.08, OutputStatus.PREDICTED),
            ],
            source_width=1280,
            source_height=720,
        )

        yellow_pixels = np.count_nonzero(
            (writer.frames[0][:, :, 0] < 40)
            & (writer.frames[0][:, :, 1] > 200)
            & (writer.frames[0][:, :, 2] > 200)
        )
        self.assertEqual(0, yellow_pixels)
        self.assertEqual("missing_track", entries[0].action_center_source)

    def test_lost_tail_action_hold_can_be_disabled(self) -> None:
        generator = FollowCamGenerator(
            self.make_app_config(
                FollowCamConfig(
                    target_width=640,
                    target_height=360,
                    min_crop_height=360,
                    max_crop_height=360,
                    lost_recenter_frames=2,
                    lost_action_hold_enabled=False,
                    recenter_smoothing=1.0,
                    draw_ball_marker=False,
                    draw_frame_text=False,
                )
            )
        )
        frames = [
            FollowCamFrame(frame_index, 1180.0, 650.0, 0.90, OutputStatus.DETECTED)
            for frame_index in range(100, 106)
        ]
        frames.extend(
            FollowCamFrame(frame_index, None, None, 0.0, OutputStatus.LOST)
            for frame_index in range(106, 112)
        )

        entries = generator._render_follow_cam(
            capture=DummyCapture(frame_count=len(frames)),
            writer=DummyWriter(),
            frames=frames,
            source_width=1280,
            source_height=720,
        )

        self.assertAlmostEqual(640.0, entries[-1].center_x)
        self.assertNotIn("action_hold", {entry.pan_mode for entry in entries})

    def test_action_center_does_not_auto_load_stale_player_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            stale_path = output_dir / "player_tracks.json"
            stale_path.write_text(
                json.dumps({"tracks": [{"samples": [{"frame": 0, "foot_point": {"x": 1.0, "y": 2.0}}]}]}),
                encoding="utf-8",
            )
            implicit_generator = FollowCamGenerator(
                self.make_app_config(
                    FollowCamConfig(action_center_enabled=True),
                    output_dir=output_dir,
                )
            )
            explicit_generator = FollowCamGenerator(
                self.make_app_config(
                    FollowCamConfig(
                        action_center_enabled=True,
                        action_center_player_tracks_path=stale_path,
                    ),
                    output_dir=output_dir,
                )
            )

            self.assertIsNone(implicit_generator._load_action_center_player_tracks())
            self.assertIsNotNone(explicit_generator._load_action_center_player_tracks())

    def test_malformed_action_center_player_samples_are_ignored(self) -> None:
        generator = FollowCamGenerator(self.make_app_config(FollowCamConfig(action_center_enabled=True)))
        report = {
            "tracks": [
                {
                    "samples": [
                        {"frame": "NaN", "foot_point": {"x": 10.0, "y": 20.0}},
                        {"frame": "Infinity", "foot_point": {"x": 30.0, "y": 40.0}},
                        {"frame": 3, "foot_point": {"x": "nan", "y": 20.0}},
                        {"frame": 3, "foot_point": ["Infinity", 20.0]},
                        {"frame": 3, "foot_point": [100.0]},
                        {"frame": 3, "foot_point": {"x": 12.0, "y": 34.0}},
                    ],
                }
            ]
        }

        points_by_frame = generator._player_points_by_frame(report)

        self.assertEqual({3: [(12.0, 34.0)]}, points_by_frame)

    def test_run_writes_camera_motion_audit_and_report_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            track_csv = output_dir / "ball_track.csv"
            generator = FollowCamGenerator(
                self.make_app_config(
                    FollowCamConfig(
                        enabled=True,
                        draw_ball_marker=False,
                        draw_frame_text=False,
                    ),
                    output_dir=output_dir,
                )
            )
            frames = [
                FollowCamFrame(0, 100.0, 100.0, 0.9, OutputStatus.DETECTED),
                FollowCamFrame(1, 180.0, 100.0, 0.9, OutputStatus.DETECTED),
            ]
            path_entries = [
                self.camera_path_entry(0, center_x=100.0, pan_mode="glide"),
                self.camera_path_entry(1, center_x=180.0, pan_mode="glide"),
            ]

            def open_pending_writer(path: Path, _fps: float) -> DummyWriter:
                path.write_bytes(b"pending browser output")
                return DummyWriter()

            with (
                mock.patch("football_tracking.follow_cam.cv2.VideoCapture", return_value=DummyVideoCapture()),
                mock.patch.object(generator, "_resolve_track_csv", return_value=(track_csv, "raw")),
                mock.patch.object(generator, "_load_frames", return_value=frames),
                mock.patch.object(generator, "_open_writer", side_effect=open_pending_writer),
                mock.patch.object(generator, "_render_follow_cam", return_value=path_entries),
                mock.patch.object(generator, "_validate_browser_video"),
            ):
                generator.run()

            with (output_dir / "camera_motion_audit.json").open("r", encoding="utf-8") as handle:
                audit_payload = json.load(handle)
            with (output_dir / "follow_cam_report.json").open("r", encoding="utf-8") as handle:
                report_payload = json.load(handle)

        self.assertEqual("fail", audit_payload["summary"]["status"])
        self.assertEqual(
            {
                "report": "camera_motion_audit.json",
                "summary": audit_payload["summary"],
            },
            report_payload["camera_motion_audit"],
        )

    def test_browser_video_writer_publishes_h264_yuv420p_faststart_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            generator = FollowCamGenerator(
                self.make_app_config(
                    FollowCamConfig(
                        enabled=True,
                        target_width=64,
                        target_height=64,
                        output_video_name="follow_cam.stable.mp4",
                    ),
                    output_dir=output_dir,
                )
            )
            pending_path = output_dir / ".follow_cam.stable.pending.mp4"
            output_path = output_dir / "follow_cam.stable.mp4"

            writer = generator._open_writer(pending_path, 5.0)
            for channel in range(3):
                frame = np.zeros((64, 64, 3), dtype=np.uint8)
                frame[:, :, channel] = 255
                writer.write(frame)
            writer.release()

            generator._publish_browser_video(pending_path, output_path, expected_frame_count=3)

            import imageio_ffmpeg  # pyright: ignore[reportMissingImports]

            reader = imageio_ffmpeg.read_frames(output_path, pix_fmt="rgb24")
            try:
                metadata = next(reader)
                first_frame = next(reader)
            finally:
                reader.close()

            self.assertFalse(pending_path.exists())
            self.assertTrue(output_path.exists())
            self.assertEqual("h264", metadata["codec"])
            self.assertTrue(str(metadata["pix_fmt"]).startswith("yuv420p"))
            self.assertEqual((64, 64), metadata["size"])
            self.assertEqual(64 * 64 * 3, len(first_frame))
            box_types = generator._mp4_top_level_box_types(output_path)
            self.assertLess(box_types.index(b"moov"), box_types.index(b"mdat"))
            self.assertIn(b"avc1", output_path.read_bytes()[: 1024 * 1024])

    def test_publish_browser_video_rejects_incompatible_codec_without_replacing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            generator = FollowCamGenerator(
                self.make_app_config(
                    FollowCamConfig(enabled=True, target_width=64, target_height=64),
                    output_dir=output_dir,
                )
            )
            pending_path = output_dir / ".follow_cam.pending.mp4"
            output_path = output_dir / "follow_cam.mp4"
            output_path.write_bytes(b"previous browser-compatible output")

            import imageio_ffmpeg  # pyright: ignore[reportMissingImports]

            writer = imageio_ffmpeg.write_frames(
                pending_path,
                (64, 64),
                pix_fmt_in="bgr24",
                pix_fmt_out="yuv420p",
                fps=5.0,
                codec="mpeg4",
                macro_block_size=1,
                ffmpeg_log_level="error",
            )
            writer.send(None)
            writer.send(np.zeros((64, 64, 3), dtype=np.uint8).tobytes())
            writer.close()

            with self.assertRaisesRegex(RuntimeError, "not H.264"):
                generator._publish_browser_video(pending_path, output_path, expected_frame_count=1)

            self.assertEqual(b"previous browser-compatible output", output_path.read_bytes())
            self.assertTrue(pending_path.exists())

    def test_run_cancellation_cleans_pending_video_without_replacing_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            track_csv = output_dir / "ball_track.csv"
            generator = FollowCamGenerator(self.make_app_config(FollowCamConfig(enabled=True), output_dir=output_dir))
            output_path = output_dir / generator.config.output_video_name
            output_path.write_bytes(b"previous browser-compatible output")
            frames = [FollowCamFrame(0, 100.0, 100.0, 0.9, OutputStatus.DETECTED)]

            def open_pending_writer(path: Path, _fps: float) -> DummyWriter:
                path.write_bytes(b"partial output")
                return DummyWriter()

            with (
                mock.patch("football_tracking.follow_cam.cv2.VideoCapture", return_value=DummyVideoCapture()),
                mock.patch.object(generator, "_resolve_track_csv", return_value=(track_csv, "raw")),
                mock.patch.object(generator, "_load_frames", return_value=frames),
                mock.patch.object(generator, "_open_writer", side_effect=open_pending_writer),
                mock.patch.object(generator, "_render_follow_cam", side_effect=CancelledError()),
            ):
                with self.assertRaises(CancelledError):
                    generator.run()

            self.assertEqual(b"previous browser-compatible output", output_path.read_bytes())
            self.assertEqual([], list(output_dir.glob(".*.pending.mp4")))

    def test_open_writer_fails_closed_when_h264_encoder_cannot_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            generator = FollowCamGenerator(
                self.make_app_config(
                    FollowCamConfig(enabled=True, target_width=64, target_height=64),
                    output_dir=output_dir,
                )
            )
            pending_path = output_dir / ".follow_cam.pending.mp4"

            with mock.patch("imageio_ffmpeg.write_frames", side_effect=OSError("libx264 unavailable")):
                with self.assertRaisesRegex(RuntimeError, "bundled ffmpeg with libx264 is required"):
                    generator._open_writer(pending_path, 5.0)

            self.assertFalse(pending_path.exists())

    def test_publish_browser_video_rejects_partial_frame_count_without_replacing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            generator = FollowCamGenerator(
                self.make_app_config(
                    FollowCamConfig(enabled=True, target_width=64, target_height=64),
                    output_dir=output_dir,
                )
            )
            pending_path = output_dir / ".follow_cam.partial.mp4"
            output_path = output_dir / "follow_cam.mp4"
            output_path.write_bytes(b"previous browser-compatible output")
            writer = generator._open_writer(pending_path, 5.0)
            writer.write(np.zeros((64, 64, 3), dtype=np.uint8))
            writer.release()

            with self.assertRaisesRegex(RuntimeError, "expected 3, got 1"):
                generator._publish_browser_video(pending_path, output_path, expected_frame_count=3)

            self.assertEqual(b"previous browser-compatible output", output_path.read_bytes())
            self.assertTrue(pending_path.exists())

    def test_run_checks_cancellation_after_sidecars_and_before_video_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            track_csv = output_dir / "ball_track.csv"
            generator = FollowCamGenerator(
                self.make_app_config(FollowCamConfig(enabled=True), output_dir=output_dir)
            )
            output_path = output_dir / generator.config.output_video_name
            existing_delivery = {
                output_path: b"previous browser-compatible output",
                output_dir / generator.config.camera_path_name: b"previous camera path",
                output_dir / "camera_motion_audit.json": b"previous camera audit",
                output_dir / generator.config.report_name: b"previous follow-cam report",
            }
            for path, content in existing_delivery.items():
                path.write_bytes(content)
            frames = [FollowCamFrame(0, 100.0, 100.0, 0.9, OutputStatus.DETECTED)]
            path_entries = [self.camera_path_entry(0, center_x=100.0, pan_mode="glide")]
            capture = DummyVideoCapture()

            def open_pending_writer(path: Path, _fps: float) -> DummyWriter:
                path.write_bytes(b"pending browser output")
                return DummyWriter()

            should_cancel = mock.Mock(side_effect=[False, False, True])
            with (
                mock.patch("football_tracking.follow_cam.cv2.VideoCapture", return_value=capture),
                mock.patch.object(generator, "_resolve_track_csv", return_value=(track_csv, "raw")),
                mock.patch.object(generator, "_load_frames", return_value=frames),
                mock.patch.object(generator, "_open_writer", side_effect=open_pending_writer),
                mock.patch.object(generator, "_render_follow_cam", return_value=path_entries),
                mock.patch.object(generator, "_validate_browser_video") as validate_video,
            ):
                with self.assertRaises(CancelledError):
                    generator.run(should_cancel=should_cancel)

            validate_video.assert_called_once_with(
                mock.ANY,
                expected_frame_count=1,
                expected_fps=20.0,
                should_cancel=should_cancel,
            )
            for path, content in existing_delivery.items():
                self.assertEqual(content, path.read_bytes())
            self.assertTrue(capture.released)
            self.assertEqual([], list(output_dir.glob(".*.pending.mp4")))
            self.assertEqual([], list(output_dir.glob(".follow_cam.*.pending")))

    def test_run_sidecar_failure_does_not_replace_existing_video(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            track_csv = output_dir / "ball_track.csv"
            generator = FollowCamGenerator(
                self.make_app_config(FollowCamConfig(enabled=True), output_dir=output_dir)
            )
            output_path = output_dir / generator.config.output_video_name
            output_path.write_bytes(b"previous browser-compatible output")
            frames = [FollowCamFrame(0, 100.0, 100.0, 0.9, OutputStatus.DETECTED)]
            path_entries = [self.camera_path_entry(0, center_x=100.0, pan_mode="glide")]

            def open_pending_writer(path: Path, _fps: float) -> DummyWriter:
                path.write_bytes(b"pending browser output")
                return DummyWriter()

            with (
                mock.patch("football_tracking.follow_cam.cv2.VideoCapture", return_value=DummyVideoCapture()),
                mock.patch.object(generator, "_resolve_track_csv", return_value=(track_csv, "raw")),
                mock.patch.object(generator, "_load_frames", return_value=frames),
                mock.patch.object(generator, "_open_writer", side_effect=open_pending_writer),
                mock.patch.object(generator, "_render_follow_cam", return_value=path_entries),
                mock.patch.object(generator, "_write_report", side_effect=OSError("sidecar write failed")),
                mock.patch.object(generator, "_publish_delivery_bundle") as publish_bundle,
            ):
                with self.assertRaisesRegex(OSError, "sidecar write failed"):
                    generator.run()

            publish_bundle.assert_not_called()
            self.assertEqual(b"previous browser-compatible output", output_path.read_bytes())
            self.assertEqual([], list(output_dir.glob(".*.pending.mp4")))
            self.assertEqual([], list(output_dir.glob(".follow_cam.*.pending")))

    def test_run_rejects_non_mp4_final_output_before_opening_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            generator = FollowCamGenerator(
                self.make_app_config(
                    FollowCamConfig(enabled=True, output_video_name="follow_cam.avi"),
                    output_dir=output_dir,
                )
            )

            with mock.patch("football_tracking.follow_cam.cv2.VideoCapture") as video_capture:
                with self.assertRaisesRegex(RuntimeError, "must use an .mp4 container"):
                    generator.run()

            video_capture.assert_not_called()

    def test_open_writer_configures_finite_ffmpeg_finalize_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            generator = FollowCamGenerator(
                self.make_app_config(
                    FollowCamConfig(enabled=True, target_width=64, target_height=64),
                    output_dir=output_dir,
                )
            )
            imageio_writer = mock.MagicMock()

            with mock.patch("imageio_ffmpeg.write_frames", return_value=imageio_writer) as write_frames:
                writer = generator._open_writer(output_dir / ".follow_cam.pending.mp4", 5.0)
                writer.release()

            self.assertEqual(30.0, write_frames.call_args.kwargs["ffmpeg_timeout"])

    def test_finalize_failure_releases_capture_and_cleans_pending_video(self) -> None:
        class FinalizeFailureWriter(DummyWriter):
            def release(self) -> None:
                raise RuntimeError("ffmpeg finalize timeout")

        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            track_csv = output_dir / "ball_track.csv"
            generator = FollowCamGenerator(
                self.make_app_config(FollowCamConfig(enabled=True), output_dir=output_dir)
            )
            output_path = output_dir / generator.config.output_video_name
            output_path.write_bytes(b"previous browser-compatible output")
            capture = DummyVideoCapture()
            frames = [FollowCamFrame(0, 100.0, 100.0, 0.9, OutputStatus.DETECTED)]
            path_entries = [self.camera_path_entry(0, center_x=100.0, pan_mode="glide")]

            def open_pending_writer(path: Path, _fps: float) -> FinalizeFailureWriter:
                path.write_bytes(b"partial output")
                return FinalizeFailureWriter()

            with (
                mock.patch("football_tracking.follow_cam.cv2.VideoCapture", return_value=capture),
                mock.patch.object(generator, "_resolve_track_csv", return_value=(track_csv, "raw")),
                mock.patch.object(generator, "_load_frames", return_value=frames),
                mock.patch.object(generator, "_open_writer", side_effect=open_pending_writer),
                mock.patch.object(generator, "_render_follow_cam", return_value=path_entries),
            ):
                with self.assertRaisesRegex(RuntimeError, "ffmpeg finalize timeout"):
                    generator.run()

            self.assertTrue(capture.released)
            self.assertEqual(b"previous browser-compatible output", output_path.read_bytes())
            self.assertEqual([], list(output_dir.glob(".*.pending.mp4")))

    def test_seek_failure_releases_capture_without_creating_pending_video(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            track_csv = output_dir / "ball_track.csv"
            generator = FollowCamGenerator(
                self.make_app_config(FollowCamConfig(enabled=True), output_dir=output_dir)
            )
            capture = DummyVideoCapture()
            frames = [FollowCamFrame(10, 100.0, 100.0, 0.9, OutputStatus.DETECTED)]

            with (
                mock.patch("football_tracking.follow_cam.cv2.VideoCapture", return_value=capture),
                mock.patch.object(generator, "_resolve_track_csv", return_value=(track_csv, "raw")),
                mock.patch.object(generator, "_load_frames", return_value=frames),
                mock.patch.object(generator, "_seek_to_frame", side_effect=RuntimeError("seek failed")),
                mock.patch.object(generator, "_open_writer") as open_writer,
            ):
                with self.assertRaisesRegex(RuntimeError, "seek failed"):
                    generator.run()

            open_writer.assert_not_called()
            self.assertTrue(capture.released)
            self.assertEqual([], list(output_dir.glob(".*.pending.mp4")))

    def test_run_rejects_early_capture_eof_and_preserves_existing_video(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            track_csv = output_dir / "ball_track.csv"
            generator = FollowCamGenerator(
                self.make_app_config(
                    FollowCamConfig(
                        enabled=True,
                        target_width=64,
                        target_height=64,
                        draw_ball_marker=False,
                        draw_frame_text=False,
                    ),
                    output_dir=output_dir,
                )
            )
            output_path = output_dir / generator.config.output_video_name
            output_path.write_bytes(b"previous browser-compatible output")
            capture = DummyCapture(frame_count=1)
            frames = [
                FollowCamFrame(0, 100.0, 100.0, 0.9, OutputStatus.DETECTED),
                FollowCamFrame(1, 110.0, 100.0, 0.9, OutputStatus.DETECTED),
            ]
            writer = DummyWriter()

            def open_pending_writer(path: Path, _fps: float) -> DummyWriter:
                path.write_bytes(b"partial output")
                return writer

            with (
                mock.patch("football_tracking.follow_cam.cv2.VideoCapture", return_value=capture),
                mock.patch.object(generator, "_resolve_track_csv", return_value=(track_csv, "raw")),
                mock.patch.object(generator, "_load_frames", return_value=frames),
                mock.patch.object(generator, "_open_writer", side_effect=open_pending_writer),
            ):
                with self.assertRaisesRegex(RuntimeError, "expected 2, got 1"):
                    generator.run()

            self.assertEqual(1, len(writer.frames))
            self.assertTrue(capture.released)
            self.assertEqual(b"previous browser-compatible output", output_path.read_bytes())
            self.assertEqual([], list(output_dir.glob(".*.pending.mp4")))

    def test_video_validation_failure_preserves_entire_existing_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            track_csv = output_dir / "ball_track.csv"
            generator = FollowCamGenerator(
                self.make_app_config(FollowCamConfig(enabled=True), output_dir=output_dir)
            )
            existing_delivery = {
                output_dir / generator.config.output_video_name: b"previous video",
                output_dir / generator.config.camera_path_name: b"previous camera path",
                output_dir / "camera_motion_audit.json": b"previous camera audit",
                output_dir / generator.config.report_name: b"previous report",
            }
            for path, content in existing_delivery.items():
                path.write_bytes(content)
            frames = [FollowCamFrame(0, 100.0, 100.0, 0.9, OutputStatus.DETECTED)]
            path_entries = [self.camera_path_entry(0, center_x=100.0, pan_mode="glide")]

            def open_pending_writer(path: Path, _fps: float) -> DummyWriter:
                path.write_bytes(b"invalid pending video")
                return DummyWriter()

            with (
                mock.patch("football_tracking.follow_cam.cv2.VideoCapture", return_value=DummyVideoCapture()),
                mock.patch.object(generator, "_resolve_track_csv", return_value=(track_csv, "raw")),
                mock.patch.object(generator, "_load_frames", return_value=frames),
                mock.patch.object(generator, "_open_writer", side_effect=open_pending_writer),
                mock.patch.object(generator, "_render_follow_cam", return_value=path_entries),
                mock.patch.object(
                    generator,
                    "_validate_browser_video",
                    side_effect=RuntimeError("video validation failed"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "video validation failed"):
                    generator.run()

            for path, content in existing_delivery.items():
                self.assertEqual(content, path.read_bytes())
            self.assertEqual([], list(output_dir.glob(".*.backup")))

    def test_second_sidecar_replace_failure_rolls_back_entire_delivery_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            track_csv = output_dir / "ball_track.csv"
            generator = FollowCamGenerator(
                self.make_app_config(FollowCamConfig(enabled=True), output_dir=output_dir)
            )
            existing_delivery = {
                output_dir / generator.config.output_video_name: b"previous video",
                output_dir / generator.config.camera_path_name: b"previous camera path",
                output_dir / "camera_motion_audit.json": b"previous camera audit",
                output_dir / generator.config.report_name: b"previous report",
            }
            for path, content in existing_delivery.items():
                path.write_bytes(content)
            frames = [FollowCamFrame(0, 100.0, 100.0, 0.9, OutputStatus.DETECTED)]
            path_entries = [self.camera_path_entry(0, center_x=100.0, pan_mode="glide")]

            def open_pending_writer(path: Path, _fps: float) -> DummyWriter:
                path.write_bytes(b"new video")
                return DummyWriter()

            real_replace = os.replace
            failed_second_sidecar = False

            def fail_second_sidecar(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
                nonlocal failed_second_sidecar
                source_path = Path(source)
                target_path = Path(target)
                if (
                    not failed_second_sidecar
                    and source_path.parent.name.startswith(".follow_cam.")
                    and source_path.name == "camera_motion_audit.json"
                    and target_path.name == "camera_motion_audit.json"
                ):
                    failed_second_sidecar = True
                    raise OSError("second sidecar replace failed")
                real_replace(source, target)

            with (
                mock.patch("football_tracking.follow_cam.cv2.VideoCapture", return_value=DummyVideoCapture()),
                mock.patch.object(generator, "_resolve_track_csv", return_value=(track_csv, "raw")),
                mock.patch.object(generator, "_load_frames", return_value=frames),
                mock.patch.object(generator, "_open_writer", side_effect=open_pending_writer),
                mock.patch.object(generator, "_render_follow_cam", return_value=path_entries),
                mock.patch.object(generator, "_validate_browser_video"),
                mock.patch("football_tracking.follow_cam.os.replace", side_effect=fail_second_sidecar),
            ):
                with self.assertRaisesRegex(OSError, "second sidecar replace failed"):
                    generator.run()

            self.assertTrue(failed_second_sidecar)
            for path, content in existing_delivery.items():
                self.assertEqual(content, path.read_bytes())
            self.assertEqual([], list(output_dir.glob(".*.backup")))

    def test_ffmpeg_probe_cancellation_terminates_process_and_closes_streams(self) -> None:
        generator = FollowCamGenerator(
            self.make_app_config(FollowCamConfig(enabled=True, target_width=64, target_height=64))
        )
        process = mock.MagicMock()
        process.poll.return_value = None
        process.wait.return_value = 0
        process.stdin = mock.MagicMock()
        process.stdout = mock.MagicMock()
        process.stderr = mock.MagicMock()

        with mock.patch("football_tracking.follow_cam.subprocess.Popen", return_value=process):
            with self.assertRaises(CancelledError):
                generator._probe_browser_video(
                    Path("pending.mp4"),
                    expected_frame_count=1,
                    expected_fps=20.0,
                    should_cancel=lambda: True,
                )

        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=2.0)
        process.kill.assert_not_called()
        process.stdin.close.assert_called_once_with()
        process.stdout.close.assert_called_once_with()
        process.stderr.close.assert_called_once_with()

    def test_ffmpeg_probe_timeout_kills_waits_and_closes_process_streams(self) -> None:
        generator = FollowCamGenerator(
            self.make_app_config(FollowCamConfig(enabled=True, target_width=64, target_height=64))
        )
        process = mock.MagicMock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("ffmpeg", 2.0), 0]
        process.stdin = mock.MagicMock()
        process.stdout = mock.MagicMock()
        process.stderr = mock.MagicMock()

        with (
            mock.patch("football_tracking.follow_cam.subprocess.Popen", return_value=process),
            mock.patch.object(generator, "_probe_timeout_seconds", return_value=0.0),
        ):
            with self.assertRaisesRegex(RuntimeError, "probe timed out"):
                generator._probe_browser_video(
                    Path("pending.mp4"),
                    expected_frame_count=1,
                    expected_fps=20.0,
                    should_cancel=None,
                )

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(2, process.wait.call_count)
        process.stdin.close.assert_called_once_with()
        process.stdout.close.assert_called_once_with()
        process.stderr.close.assert_called_once_with()

    def test_ffmpeg_probe_timeout_scales_with_expected_media_duration(self) -> None:
        generator = FollowCamGenerator(self.make_app_config(FollowCamConfig(enabled=True)))

        timeout_seconds = generator._probe_timeout_seconds(90 * 60 * 20, 20.0)

        self.assertGreater(timeout_seconds, 90 * 60)
        self.assertGreater(timeout_seconds, 600.0)

    def test_bundle_backup_cleanup_failure_does_not_reverse_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            staged_path = output_dir / ".new-report.json"
            output_path = output_dir / "report.json"
            staged_path.write_bytes(b"new report")
            output_path.write_bytes(b"old report")
            real_unlink = Path.unlink

            def fail_backup_cleanup(path: Path, missing_ok: bool = False) -> None:
                if path.name.endswith(".backup"):
                    raise PermissionError("backup held by antivirus")
                real_unlink(path, missing_ok=missing_ok)

            with mock.patch("pathlib.Path.unlink", autospec=True, side_effect=fail_backup_cleanup):
                generator = FollowCamGenerator(self.make_app_config(FollowCamConfig(enabled=True)))
                generator._replace_artifact_bundle([(staged_path, output_path)])

            self.assertEqual(b"new report", output_path.read_bytes())

    def write_yaml(self, repo_root: Path, payload: object) -> Path:
        config_path = repo_root / "config" / "default.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        return config_path

    def make_app_config(self, follow_cam: FollowCamConfig, output_dir: Path | None = None) -> AppConfig:
        root = Path(tempfile.gettempdir()) if output_dir is None else output_dir
        return AppConfig(
            input_video=root / "input.mp4",
            output_dir=root,
            logging=LoggingConfig(),
            detector=DetectorConfig(model_path=root / "model.pt", device="cpu", use_half=False),
            sahi=SahiConfig(),
            filtering=FilteringConfig(),
            scene_bias=SceneBiasConfig(),
            selection=SelectionConfig(),
            tracking=TrackingConfig(kalman_enabled=False),
            output=OutputConfig(save_video=False, save_frames=False, save_csv=False, save_debug_jsonl=False),
            postprocess=PostprocessConfig(enabled=False),
            follow_cam=follow_cam,
            runtime=RuntimeConfig(use_gpu_if_available=False),
            mock=MockConfig(enabled=True),
        )

    def camera_path_entry(self, frame_index: int, *, center_x: float, pan_mode: str) -> CameraPathEntry:
        return CameraPathEntry(
            frame_index=frame_index,
            center_x=center_x,
            center_y=100.0,
            crop_x1=0,
            crop_y1=0,
            crop_x2=960,
            crop_y2=540,
            crop_width=960,
            crop_height=540,
            source_status=OutputStatus.DETECTED.value,
            track_x=center_x,
            track_y=100.0,
            confidence=0.9,
            speed=0.0,
            zoom_out_ratio=0.0,
            pan_mode=pan_mode,
            profile="custom",
            action_center_enabled=False,
            action_center_x=center_x,
            action_center_y=100.0,
            action_center_source="raw_track",
            action_center_player_count=0,
        )


if __name__ == "__main__":
    unittest.main()
