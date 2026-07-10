from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

from football_tracking.camera_path_renderer import render_camera_path_video
from football_tracking.hybrid_broadcast_camera import (
    CAMERA_PATH_NAME,
    DECISIONS_NAME,
    MOTION_EVIDENCE_NAME,
    REPORT_NAME,
    BallTrackRow,
    CameraMotionEvidence,
    HybridBroadcastCameraError,
    HybridCameraConfig,
    _bounded_axis_step,
    _estimate_camera_motion,
    _initial_state,
    _plan_frame,
    solve_hybrid_broadcast_camera,
)


class DummyCapture:
    def __init__(self, frames: list[np.ndarray]) -> None:
        self.frames = frames
        self.index = 0
        self.released = False

    def isOpened(self) -> bool:
        return True

    def read(self) -> tuple[bool, np.ndarray | None]:
        if self.index >= len(self.frames):
            return False, None
        frame = self.frames[self.index]
        self.index += 1
        return True, frame.copy()

    def release(self) -> None:
        self.released = True


class HybridBroadcastCameraTests(unittest.TestCase):
    def test_solve_publishes_auditable_generation_without_unknown_ball_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = self._write_inputs(
                root,
                [
                    self._track_row(0, "detected", x=16.0, y=12.0, confidence=0.9, candidate_id="candidate-0"),
                    self._track_row(1, "unknown"),
                    self._track_row(2, "out_of_view"),
                ],
            )
            motions = [
                self._motion(0, method="shot_start"),
                self._motion(1, dx=2.0, dy=0.0, confidence=0.9),
                self._motion(2, cut=True, cut_score=0.9, method="appearance_cut"),
            ]
            output = root / "camera-generation"
            with (
                self._patched_video(inputs, frame_count=3),
                mock.patch(
                    "football_tracking.hybrid_broadcast_camera._estimate_camera_motion",
                    side_effect=motions,
                ),
            ):
                report = solve_hybrid_broadcast_camera(*inputs, output)

            self.assertTrue((output / REPORT_NAME).is_file())
            self.assertEqual("succeeded", report["status"])
            self.assertTrue(report["complete"])
            self.assertEqual(3, report["summary"]["row_count"])
            self.assertTrue(report["summary"]["no_ball_targets_for_unknown"])
            self.assertEqual(1, report["summary"]["cut_count"])
            rows = self._read_csv(output / CAMERA_PATH_NAME)
            self.assertEqual(3, len(rows))
            self.assertNotEqual("", rows[0]["TargetX"])
            for row in rows[1:]:
                self.assertEqual("", row["TrackX"])
                self.assertEqual("", row["TrackY"])
                self.assertEqual("", row["TargetX"])
                self.assertEqual("", row["TargetY"])
                self.assertEqual("none", row["TargetSource"])
            self.assertEqual("1", rows[2]["CutDetected"])
            self.assertEqual("1", rows[2]["ShotId"])
            self.assertEqual("scene_cut", rows[2]["PanMode"])
            for artifact_name in (CAMERA_PATH_NAME, MOTION_EVIDENCE_NAME, DECISIONS_NAME, "camera_motion_audit.json"):
                artifact = output / artifact_name
                binding = report["artifacts"][artifact_name]
                self.assertEqual(self._sha256(artifact), binding["sha256"])
                self.assertEqual(artifact.stat().st_size, binding["size"])

            decisions = [json.loads(line) for line in (output / DECISIONS_NAME).read_text().splitlines()]
            self.assertIsNone(decisions[1]["ball"]["x"])
            self.assertIsNone(decisions[1]["ball"]["confidence"])
            self.assertIsNone(decisions[1]["fusion"]["target_x"])
            self.assertEqual("camera_motion_only", decisions[1]["fusion"]["evidence_mode"])

    def test_interpolated_target_keeps_status_and_receives_lower_weight(self) -> None:
        config = HybridCameraConfig()
        detected_state = _initial_state(1920, 1080, config)
        interpolated_state = _initial_state(1920, 1080, config)
        motion = self._motion(0, method="shot_start")
        detected = _plan_frame(
            BallTrackRow(0, 1500.0, 600.0, 0.8, "detected", "candidate", "graph", "selected"),
            motion,
            detected_state,
            1920,
            1080,
            config,
        )
        interpolated = _plan_frame(
            BallTrackRow(0, 1500.0, 600.0, 0.8, "interpolated", None, "kalman", "bounded_gap"),
            motion,
            interpolated_state,
            1920,
            1080,
            config,
        )
        self.assertEqual("detected", detected.path["Status"])
        self.assertEqual("interpolated", interpolated.path["Status"])
        self.assertLess(interpolated.path["TargetConfidence"], detected.path["TargetConfidence"])
        self.assertTrue(detected.path["TargetVisible"])
        self.assertTrue(interpolated.path["TargetVisible"])

    def test_unknown_without_motion_uses_bounded_hold_then_wide_home_without_ball_coordinates(self) -> None:
        config = replace(HybridCameraConfig(), unknown_hold_frames=2)
        state = _initial_state(1920, 1080, config)
        state.center_x = 1400.0
        state.crop_height = 600.0
        rows = []
        for frame_index in range(4):
            plan = _plan_frame(
                BallTrackRow(frame_index, None, None, None, "unknown", None, "", "no_evidence"),
                self._motion(frame_index, method="insufficient_features"),
                state,
                1920,
                1080,
                config,
            )
            rows.append(plan.path)
        self.assertEqual("bounded_hold", rows[0]["EvidenceMode"])
        self.assertEqual("bounded_hold", rows[1]["EvidenceMode"])
        self.assertEqual("wide_home_fallback", rows[2]["EvidenceMode"])
        self.assertLess(rows[3]["CenterX"], rows[2]["CenterX"])
        for row in rows:
            self.assertIsNone(row["TrackX"])
            self.assertIsNone(row["TrackY"])
            self.assertIsNone(row["TargetX"])
            self.assertIsNone(row["TargetY"])

    def test_scene_cut_resets_shot_velocity_and_does_not_bridge_unknown_ball(self) -> None:
        config = HybridCameraConfig()
        state = _initial_state(1920, 1080, config)
        state.center_x = 1400.0
        state.pan_velocity_x = 25.0
        state.zoom_velocity = -8.0
        state.previous_ball_x = 1500.0
        state.previous_ball_y = 500.0
        state.previous_ball_frame = 8
        plan = _plan_frame(
            BallTrackRow(9, None, None, None, "unknown", None, "", "cut_unknown"),
            self._motion(9, cut=True, cut_score=0.95, method="appearance_cut"),
            state,
            1920,
            1080,
            config,
        )
        self.assertEqual(1, plan.path["ShotId"])
        self.assertEqual("scene_cut_no_ball", plan.path["EvidenceMode"])
        self.assertIsNone(plan.path["TargetX"])
        self.assertEqual(0.0, plan.path["PanVelocityX"])
        self.assertEqual(0.0, plan.path["ZoomVelocity"])
        self.assertIsNone(state.previous_ball_x)

    def test_motion_estimator_recognizes_pan_without_cut_and_hard_appearance_cut(self) -> None:
        config = replace(
            HybridCameraConfig(),
            analysis_max_dimension=160,
            min_features=6,
            min_inliers=4,
            motion_confidence_threshold=0.05,
        )
        checker = self._checkerboard(160, 90)
        shifted = np.zeros_like(checker)
        shifted[:, 4:] = checker[:, :-4]
        pan = _estimate_camera_motion(1, checker, shifted, 1600, 900, config)
        self.assertFalse(pan.cut_before)
        self.assertIn(pan.method, {"partial_affine", "phase_translation"})
        self.assertIsNotNone(pan.dx)
        self.assertGreater(float(pan.dx), 20.0)

        rng = np.random.default_rng(7)
        first_scene = rng.integers(0, 256, size=(90, 160), dtype=np.uint8)
        second_scene = rng.integers(0, 256, size=(90, 160), dtype=np.uint8)
        cut = _estimate_camera_motion(2, first_scene, second_scene, 1600, 900, config)
        self.assertTrue(cut.cut_before)
        self.assertEqual("appearance_cut", cut.method)
        self.assertIsNone(cut.dx)

        fast_pan_frame = cv2.warpAffine(
            first_scene,
            np.asarray([[1.0, 0.0, 40.0], [0.0, 1.0, 0.0]], dtype=np.float32),
            (160, 90),
            borderMode=cv2.BORDER_REFLECT,
        )
        fast_pan = _estimate_camera_motion(3, first_scene, fast_pan_frame, 1600, 900, config)
        self.assertFalse(fast_pan.cut_before)

        entering_frame = np.zeros_like(first_scene)
        entering_frame[:, 40:] = first_scene[:, :-40]
        entering_pan = _estimate_camera_motion(4, first_scene, entering_frame, 1600, 900, config)
        self.assertFalse(entering_pan.cut_before)
        self.assertIn(entering_pan.method, {"partial_affine", "phase_translation"})

        for offset in (73, 80, 90):
            with self.subTest(large_pan_offset=offset):
                large_entering_frame = np.zeros_like(first_scene)
                large_entering_frame[:, offset:] = first_scene[:, :-offset]
                large_pan = _estimate_camera_motion(4, first_scene, large_entering_frame, 1600, 900, config)
                self.assertFalse(large_pan.cut_before)
                self.assertEqual("coherent_motion_out_of_bounds", large_pan.method)
                self.assertIsNone(large_pan.dx)

        black = np.zeros((90, 160), dtype=np.uint8)
        white = np.full((90, 160), 255, dtype=np.uint8)
        flash = _estimate_camera_motion(5, black, white, 1600, 900, config)
        self.assertFalse(flash.cut_before)
        self.assertIsNone(flash.dx)
        self.assertEqual("low_texture_or_photometric_change", flash.reject_reason)

    def test_source_zoom_moves_world_relative_crop_in_same_scale_direction(self) -> None:
        config = HybridCameraConfig()
        state = _initial_state(1920, 1080, config)
        state.crop_height = 600.0
        plan = _plan_frame(
            BallTrackRow(1, None, None, None, "unknown", None, "global_candidate_graph", "unknown"),
            self._motion(1, dx=-96.0, dy=-54.0, scale=1.1, confidence=0.9),
            state,
            1920,
            1080,
            config,
        )
        self.assertEqual("camera_motion_only", plan.path["EvidenceMode"])
        self.assertGreater(plan.path["CropHeight"], 600)

    def test_far_non_cut_target_cannot_bypass_pan_or_acceleration_bounds(self) -> None:
        config = HybridCameraConfig()
        state = _initial_state(1920, 1080, config)
        state.center_x = 520.0
        state.center_y = 540.0
        state.crop_height = 520.0
        previous_x = state.center_x
        previous_y = state.center_y
        previous_crop = state.crop_height
        plan = _plan_frame(
            BallTrackRow(1, 1900.0, 540.0, 0.99, "detected", "candidate", "graph", "far_target"),
            self._motion(1, method="insufficient_features"),
            state,
            1920,
            1080,
            config,
        )
        actual_x = plan.path["CenterX"] - previous_x
        actual_y = plan.path["CenterY"] - previous_y
        actual_zoom = plan.path["CropHeight"] - previous_crop
        self.assertLessEqual(abs(actual_x), 1920 * config.max_pan_step_x_ratio)
        self.assertLessEqual(abs(actual_y), 1080 * config.max_pan_step_y_ratio)
        self.assertLessEqual(abs(actual_x), 1920 * config.max_pan_acceleration_x_ratio)
        self.assertLessEqual(abs(actual_y), 1080 * config.max_pan_acceleration_y_ratio)
        self.assertLessEqual(abs(actual_zoom), 1080 * config.max_zoom_step_ratio)
        self.assertAlmostEqual(actual_x, plan.path["PanVelocityX"])
        self.assertAlmostEqual(actual_y, plan.path["PanVelocityY"])
        self.assertAlmostEqual(actual_zoom, plan.path["ZoomVelocity"])
        self.assertFalse(plan.path["TargetVisible"])
        self.assertEqual("bounded_pan_target_not_yet_visible", plan.path["FallbackReason"])

    def test_axis_step_never_breaks_acceleration_to_avoid_target_overshoot(self) -> None:
        _next, velocity = _bounded_axis_step(
            current=0.0,
            desired=1.0,
            previous_velocity=67.0,
            max_step=100.0,
            max_acceleration=34.56,
            smoothing=1.0,
        )
        self.assertLessEqual(abs(velocity - 67.0), 34.56)
        self.assertGreater(velocity, 1.0)

    def test_boundary_aware_braking_preserves_step_and_acceleration_over_consecutive_frames(self) -> None:
        config = HybridCameraConfig()
        state = _initial_state(1920, 1080, config)
        state.crop_height = 600.0
        state.center_x = 583.5
        state.center_y = 540.0
        state.pan_velocity_x = -16.0
        previous_velocity = state.pan_velocity_x
        previous_center = state.center_x
        for frame_index in (1, 2, 3):
            plan = _plan_frame(
                BallTrackRow(
                    frame_index,
                    None,
                    None,
                    None,
                    "unknown",
                    None,
                    "global_candidate_graph",
                    "unknown",
                ),
                self._motion(frame_index, dx=-1000.0, confidence=0.9),
                state,
                1920,
                1080,
                config,
            )
            velocity = plan.path["CenterX"] - previous_center
            self.assertAlmostEqual(velocity, plan.path["PanVelocityX"])
            self.assertLessEqual(abs(velocity), 1920 * config.max_pan_step_x_ratio + 1e-6)
            self.assertLessEqual(
                abs(velocity - previous_velocity),
                1920 * config.max_pan_acceleration_x_ratio + 1e-6,
            )
            self.assertGreaterEqual(plan.path["CropX1"], 0)
            previous_velocity = velocity
            previous_center = plan.path["CenterX"]

    def test_zoom_out_and_pan_are_jointly_bounded_near_frame_edge(self) -> None:
        config = HybridCameraConfig()
        state = _initial_state(1920, 1080, config)
        state.crop_height = 520.0
        state.center_x = 512.0
        state.center_y = 540.0
        state.pan_velocity_x = -16.0
        previous_velocity = state.pan_velocity_x
        previous_zoom_velocity = state.zoom_velocity
        previous_center = state.center_x
        previous_crop = state.crop_height
        for frame_index in (1, 2, 3):
            plan = _plan_frame(
                BallTrackRow(
                    frame_index,
                    None,
                    None,
                    None,
                    "unknown",
                    None,
                    "global_candidate_graph",
                    "unknown",
                ),
                self._motion(frame_index, dx=-1000.0, scale=1.1, confidence=0.9),
                state,
                1920,
                1080,
                config,
            )
            velocity = plan.path["CenterX"] - previous_center
            zoom_velocity = plan.path["CropHeight"] - previous_crop
            self.assertLessEqual(
                abs(velocity - previous_velocity),
                1920 * config.max_pan_acceleration_x_ratio + 1e-6,
            )
            self.assertLessEqual(abs(zoom_velocity), 1080 * config.max_zoom_step_ratio + 1e-6)
            self.assertLessEqual(
                abs(zoom_velocity - previous_zoom_velocity),
                1080 * config.max_zoom_step_ratio + 1e-6,
            )
            self.assertEqual(velocity, plan.path["PanVelocityX"])
            self.assertEqual(zoom_velocity, plan.path["ZoomVelocity"])
            previous_velocity = velocity
            previous_zoom_velocity = zoom_velocity
            previous_center = plan.path["CenterX"]
            previous_crop = plan.path["CropHeight"]

    def test_discrete_portrait_crop_falls_back_instead_of_breaking_zoom_budget(self) -> None:
        config = replace(HybridCameraConfig(), target_width=1080, target_height=1920)
        state = _initial_state(1920, 1080, config)
        state.crop_height = 519.0
        state.center_x = 960.0
        state.center_y = 540.0
        state.zoom_velocity = 12.0
        plan = _plan_frame(
            BallTrackRow(1, None, None, None, "unknown", None, "graph", "unknown"),
            self._motion(1, dx=0.0, scale=1.25, confidence=0.9),
            state,
            1920,
            1080,
            config,
        )
        self.assertLessEqual(abs(plan.path["ZoomVelocity"]), 1080 * config.max_zoom_step_ratio)
        self.assertLessEqual(
            abs(plan.path["ZoomVelocity"] - 12.0),
            1080 * config.max_zoom_step_ratio,
        )

    def test_ball_anchor_composes_camera_motion_across_unknown_gap(self) -> None:
        config = HybridCameraConfig()
        state = _initial_state(1920, 1080, config)
        _plan_frame(
            BallTrackRow(0, 500.0, 500.0, 0.9, "detected", "c0", "graph", "detected"),
            self._motion(0, method="shot_start"),
            state,
            1920,
            1080,
            config,
        )
        for frame_index in (1, 2):
            unknown = _plan_frame(
                BallTrackRow(frame_index, None, None, None, "unknown", None, "graph", "unknown"),
                self._motion(frame_index, dx=100.0, confidence=0.9),
                state,
                1920,
                1080,
                config,
            )
            self.assertIsNone(unknown.path["TargetX"])
        self.assertAlmostEqual(700.0, float(state.previous_ball_x))
        returned = _plan_frame(
            BallTrackRow(3, 800.0, 500.0, 0.9, "detected", "c3", "graph", "returned"),
            self._motion(3, dx=100.0, confidence=0.9),
            state,
            1920,
            1080,
            config,
        )
        self.assertLessEqual(returned.path["ZoomVelocity"], 0.0)

    def test_untrusted_camera_motion_clears_saved_ball_anchor(self) -> None:
        config = HybridCameraConfig()
        state = _initial_state(1920, 1080, config)
        _plan_frame(
            BallTrackRow(0, 500.0, 500.0, 0.9, "detected", "c0", "graph", "detected"),
            self._motion(0, method="shot_start"),
            state,
            1920,
            1080,
            config,
        )
        _plan_frame(
            BallTrackRow(1, None, None, None, "unknown", None, "graph", "unknown"),
            self._motion(1, dx=100.0, confidence=0.1),
            state,
            1920,
            1080,
            config,
        )
        self.assertIsNone(state.previous_ball_x)

    def test_fail_closed_on_source_or_track_report_binding_mismatch(self) -> None:
        cases = ("source_sha", "width", "fps", "frame_count", "track_sha", "track_size")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                inputs = self._write_inputs(root, [self._track_row(0, "unknown")])
                report = json.loads(inputs[2].read_text(encoding="utf-8"))
                if case == "source_sha":
                    report["source_video"]["sha256"] = "0" * 64
                elif case in {"width", "frame_count"}:
                    report["source_video"][case] += 1
                elif case == "fps":
                    report["source_video"][case] = 24.0
                elif case == "track_sha":
                    report["artifacts"]["ball_track.v2.csv"]["sha256"] = "0" * 64
                else:
                    report["artifacts"]["ball_track.v2.csv"]["size"] += 1
                inputs[2].write_text(json.dumps(report), encoding="utf-8")
                output = root / "generation"
                with self._patched_video(inputs, frame_count=1):
                    with self.assertRaises(HybridBroadcastCameraError):
                        solve_hybrid_broadcast_camera(*inputs, output)
                self.assertFalse(output.exists())

    def test_fail_closed_on_track_gap_extra_row_and_invalid_unknown_payload(self) -> None:
        cases = {
            "gap": [self._track_row(1, "unknown")],
            "extra": [self._track_row(0, "unknown"), self._track_row(1, "unknown")],
            "unknown_coordinate": [self._track_row(0, "unknown", x=5.0, y=6.0)],
        }
        for case, rows in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                inputs = self._write_inputs(root, rows)
                output = root / "generation"
                frame_count = 1
                with self._patched_video(inputs, frame_count=frame_count):
                    with self.assertRaises(HybridBroadcastCameraError):
                        solve_hybrid_broadcast_camera(*inputs, output)
                self.assertFalse(output.exists())

    def test_fail_closed_on_duplicate_track_columns_and_extra_row_fields(self) -> None:
        header = "Frame,X,Y,Confidence,Status,SelectedCandidateId,Source,Reason"
        cases = {
            "duplicate": (
                "Frame,X,Y,Confidence,Status,SelectedCandidateId,Source,Reason,Frame\n"
                "0,,,,unknown,,global_candidate_graph,test,0\n"
            ),
            "extra": f"{header}\n0,,,,unknown,,global_candidate_graph,test,unexpected\n",
        }
        for case, track_text in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                inputs = self._write_inputs(root, [self._track_row(0, "unknown")])
                inputs[1].write_text(track_text, encoding="utf-8")
                report = json.loads(inputs[2].read_text(encoding="utf-8"))
                report["artifacts"]["ball_track.v2.csv"] = {
                    "sha256": self._sha256(inputs[1]),
                    "size": inputs[1].stat().st_size,
                }
                inputs[2].write_text(json.dumps(report), encoding="utf-8")
                with self._patched_video(inputs, frame_count=1), self.assertRaises(HybridBroadcastCameraError):
                    solve_hybrid_broadcast_camera(*inputs, root / "generation")

    def test_existing_generation_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = self._write_inputs(root, [self._track_row(0, "unknown")])
            output = root / "generation"
            output.mkdir()
            sentinel = output / "sentinel.txt"
            sentinel.write_text("keep", encoding="utf-8")
            with self._patched_video(inputs, frame_count=1):
                with self.assertRaisesRegex(HybridBroadcastCameraError, "already exists"):
                    solve_hybrid_broadcast_camera(*inputs, output)
            self.assertEqual("keep", sentinel.read_text(encoding="utf-8"))

    def test_failure_after_publish_removes_incomplete_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = self._write_inputs(root, [self._track_row(0, "unknown")])
            output = root / "generation"
            with (
                self._patched_video(inputs, frame_count=1),
                mock.patch(
                    "football_tracking.hybrid_broadcast_camera._write_json_commit",
                    side_effect=OSError("disk full"),
                ),
            ):
                with self.assertRaises(HybridBroadcastCameraError):
                    solve_hybrid_broadcast_camera(*inputs, output)
            self.assertFalse(output.exists())

    def test_unavailable_or_mismatched_camera_audit_cannot_publish_success(self) -> None:
        cases = (
            {"status": "unavailable", "frame_count": 0, "cut_count": 0},
            {"status": "ok", "frame_count": 99, "cut_count": 0},
            {"status": "ok", "frame_count": 1, "cut_count": 99},
        )
        for audit_summary in cases:
            with self.subTest(audit_summary=audit_summary), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                inputs = self._write_inputs(root, [self._track_row(0, "unknown")])
                output = root / "generation"

                def fake_audit(staging_dir: Path, **_kwargs):
                    (staging_dir / "camera_motion_audit.json").write_text("{}", encoding="utf-8")
                    return {"summary": audit_summary}

                with (
                    self._patched_video(inputs, frame_count=1),
                    mock.patch(
                        "football_tracking.hybrid_broadcast_camera.write_streaming_camera_motion_audit_report",
                        side_effect=fake_audit,
                    ),
                    self.assertRaises(HybridBroadcastCameraError),
                ):
                    solve_hybrid_broadcast_camera(*inputs, output)
                self.assertFalse(output.exists())

    def test_source_verification_failure_after_publish_removes_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = self._write_inputs(root, [self._track_row(0, "unknown")])
            output = root / "generation"
            with (
                self._patched_video(inputs, frame_count=1),
                mock.patch(
                    "football_tracking.hybrid_broadcast_camera._verify_source_lease",
                    side_effect=GlobalBallTrajectoryErrorForTest("source changed"),
                ),
            ):
                with self.assertRaises(HybridBroadcastCameraError):
                    solve_hybrid_broadcast_camera(*inputs, output)
            self.assertFalse(output.exists())

    def test_path_and_decisions_are_deterministic_for_captured_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = self._write_inputs(
                root,
                [
                    self._track_row(0, "detected", x=10.0, y=10.0, confidence=0.8, candidate_id="c"),
                    self._track_row(1, "interpolated", x=12.0, y=10.0, confidence=0.5),
                ],
            )
            motions = [self._motion(0, method="shot_start"), self._motion(1, dx=1.0, confidence=0.8)]
            for output_name in ("one", "two"):
                with (
                    self._patched_video(inputs, frame_count=2),
                    mock.patch(
                        "football_tracking.hybrid_broadcast_camera._estimate_camera_motion",
                        side_effect=list(motions),
                    ),
                ):
                    solve_hybrid_broadcast_camera(*inputs, root / output_name)
            for name in (
                CAMERA_PATH_NAME,
                MOTION_EVIDENCE_NAME,
                DECISIONS_NAME,
                "camera_motion_audit.json",
                REPORT_NAME,
            ):
                self.assertEqual((root / "one" / name).read_bytes(), (root / "two" / name).read_bytes())

    def test_invalid_config_is_rejected_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = self._write_inputs(root, [self._track_row(0, "unknown")])
            with self.assertRaisesRegex(HybridBroadcastCameraError, "minimum_crop"):
                solve_hybrid_broadcast_camera(
                    *inputs,
                    root / "generation",
                    config=replace(
                        HybridCameraConfig(),
                        minimum_crop_height_ratio=0.9,
                        maximum_crop_height_ratio=0.5,
                    ),
                )

    def test_system_exit_control_flow_is_not_reclassified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = self._write_inputs(root, [self._track_row(0, "unknown")])
            with (
                mock.patch(
                    "football_tracking.hybrid_broadcast_camera._acquire_output_lock",
                    side_effect=SystemExit(2),
                ),
                self.assertRaises(SystemExit),
            ):
                solve_hybrid_broadcast_camera(*inputs, root / "generation")

    def test_tiny_real_video_runs_through_lease_decode_and_report_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "source.avi"
            writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"MJPG"), 10.0, (64, 36))
            self.assertTrue(writer.isOpened())
            try:
                for index in range(4):
                    frame = np.zeros((36, 64, 3), dtype=np.uint8)
                    frame[8:24, 8 + index * 2 : 24 + index * 2] = (255, 255, 255)
                    writer.write(frame)
            finally:
                writer.release()
            capture = cv2.VideoCapture(str(source))
            try:
                metadata = {
                    "width": int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
                    "height": int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                    "fps": float(capture.get(cv2.CAP_PROP_FPS)),
                    "frame_count": int(capture.get(cv2.CAP_PROP_FRAME_COUNT)),
                }
            finally:
                capture.release()
            self.assertEqual(4, metadata["frame_count"])
            rows = [
                self._track_row(0, "detected", x=16.0, y=16.0, confidence=0.9, candidate_id="c0"),
                self._track_row(1, "interpolated", x=18.0, y=16.0, confidence=0.6),
                self._track_row(2, "unknown"),
                self._track_row(3, "detected", x=22.0, y=16.0, confidence=0.8, candidate_id="c3"),
            ]
            track = root / "ball_track.v2.csv"
            self._write_track(track, rows)
            trajectory_report = root / "global_ball_trajectory_report.v1.json"
            trajectory_report.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "artifact_type": "global_ball_trajectory_report",
                        "status": "succeeded",
                        "complete": True,
                        "algorithm": {"version": "global-ball-trajectory-v1"},
                        "source_video": {"sha256": self._sha256(source), **metadata},
                        "artifacts": {
                            "ball_track.v2.csv": {
                                "sha256": self._sha256(track),
                                "size": track.stat().st_size,
                            }
                        },
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            output = root / "generation"
            report = solve_hybrid_broadcast_camera(source, track, trajectory_report, output)
            self.assertEqual(4, report["summary"]["row_count"])
            self.assertTrue((output / REPORT_NAME).is_file())
            rendered_path = root / "rendered.avi"
            rendered = render_camera_path_video(
                source,
                output / CAMERA_PATH_NAME,
                output / REPORT_NAME,
                rendered_path,
                target_width=64,
                target_height=36,
                codec="MJPG",
            )
            self.assertEqual(4, rendered.frame_count)
            self.assertTrue(rendered_path.is_file())

    def _write_inputs(self, root: Path, rows: list[dict[str, str]]) -> tuple[Path, Path, Path]:
        source = root / "source.mp4"
        source.write_bytes(b"stable-source-video")
        track = root / "ball_track.v2.csv"
        self._write_track(track, rows)
        report = root / "global_ball_trajectory_report.v1.json"
        payload = {
            "schema_version": "1.0",
            "artifact_type": "global_ball_trajectory_report",
            "status": "succeeded",
            "complete": True,
            "algorithm": {"version": "global-ball-trajectory-v1"},
            "source_video": {
                "sha256": self._sha256(source),
                "width": 32,
                "height": 18,
                "fps": 30.0,
                "frame_count": len(rows),
            },
            "artifacts": {
                "ball_track.v2.csv": {
                    "sha256": self._sha256(track),
                    "size": track.stat().st_size,
                }
            },
        }
        report.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return source, track, report

    def _write_track(self, track: Path, rows: list[dict[str, str]]) -> None:
        with track.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "Frame",
                    "X",
                    "Y",
                    "Confidence",
                    "Status",
                    "SelectedCandidateId",
                    "Source",
                    "Reason",
                ],
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(rows)

    def _track_row(
        self,
        frame: int,
        status: str,
        *,
        x: float | None = None,
        y: float | None = None,
        confidence: float | None = None,
        candidate_id: str | None = None,
    ) -> dict[str, str]:
        if confidence is None and status not in {"unknown", "out_of_view"}:
            confidence = 0.5
        if status == "detected" and candidate_id is None:
            candidate_id = f"candidate-{frame}"
        return {
            "Frame": str(frame),
            "X": "" if x is None else str(x),
            "Y": "" if y is None else str(y),
            "Confidence": "" if confidence is None else str(confidence),
            "Status": status,
            "SelectedCandidateId": candidate_id or "",
            "Source": "global_candidate_graph" if status == "detected" else "bounded_evidence",
            "Reason": "test_evidence",
        }

    def _motion(
        self,
        frame: int,
        *,
        dx: float | None = None,
        dy: float | None = None,
        scale: float | None = None,
        rotation: float | None = None,
        confidence: float | None = None,
        cut: bool = False,
        cut_score: float = 0.0,
        method: str = "insufficient_features",
    ) -> CameraMotionEvidence:
        if dx is not None:
            dy = 0.0 if dy is None else dy
            scale = 1.0 if scale is None else scale
            rotation = 0.0 if rotation is None else rotation
            confidence = 0.8 if confidence is None else confidence
            method = "partial_affine" if method == "insufficient_features" else method
        return CameraMotionEvidence(
            frame,
            dx,
            dy,
            scale,
            rotation,
            confidence,
            confidence,
            20 if dx is not None else 0,
            cut_score,
            cut,
            method,
            None if cut or confidence is None or confidence >= 0.35 else "motion_confidence_below_threshold",
        )

    def _patched_video(self, inputs: tuple[Path, Path, Path], *, frame_count: int):
        _ = inputs
        metadata = {"width": 32, "height": 18, "fps": 30.0, "frame_count": frame_count}
        frames = [self._frame(index) for index in range(frame_count)]
        return _PatchVideoContext(metadata, frames)

    def _frame(self, index: int) -> np.ndarray:
        frame = np.zeros((18, 32, 3), dtype=np.uint8)
        frame[:, :, 1] = index * 20
        frame[4:12, 6 + index : 14 + index] = (255, 255, 255)
        return frame

    def _checkerboard(self, width: int, height: int) -> np.ndarray:
        y, x = np.indices((height, width))
        return (((x // 5 + y // 5) % 2) * 255).astype(np.uint8)

    def _read_csv(self, path: Path) -> list[dict[str, str]]:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def _sha256(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()


class _PatchVideoContext:
    def __init__(self, metadata: dict[str, object], frames: list[np.ndarray]) -> None:
        self.metadata = metadata
        self.frames = frames
        self.probe_patch = mock.patch(
            "football_tracking.hybrid_broadcast_camera._probe_video_metadata",
            return_value=self.metadata,
        )
        self.capture_patch = mock.patch(
            "football_tracking.hybrid_broadcast_camera.cv2.VideoCapture",
            side_effect=lambda *_args, **_kwargs: DummyCapture(self.frames),
        )

    def __enter__(self):
        self.probe_patch.start()
        self.capture_patch.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.capture_patch.stop()
        self.probe_patch.stop()


class GlobalBallTrajectoryErrorForTest(Exception):
    pass


if __name__ == "__main__":
    unittest.main()
