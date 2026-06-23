from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from football_tracking.ai_candidate_lifecycle import build_ai_candidate_lifecycle
from football_tracking.ai_candidate_registry import load_candidate_registry
from football_tracking.camera_motion_audit import write_camera_motion_audit_report
from football_tracking.final_artifact_manifest import build_final_artifact_manifest
from football_tracking.follow_cam_candidate_executor import (
    execute_follow_cam_candidate,
    follow_cam_candidate_output_dir,
)


class FollowCamCandidateExecutorTests(unittest.TestCase):
    def test_adjust_follow_cam_creates_candidate_artifacts_and_registry_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            output_dir = root / "outputs" / "baseline"
            config_path = self.write_config(root, output_dir)
            self.write_track(output_dir)
            self.write_camera_path(output_dir, centers=[0, 160, 0, 160], crop_heights=[540] * 4)
            write_camera_motion_audit_report(output_dir)
            approval = {
                "approval_id": "camera_1",
                "candidate_id": "follow-cam-1",
                "approved_action": "adjust_follow_cam",
                "config_patch": {"follow_cam": {"glide_pan_smoothing": 0.07}},
            }

            def fake_run(instance: object) -> None:
                candidate_dir = instance.app_config.output_dir
                (candidate_dir / "follow_cam.mp4").write_bytes(b"candidate-video")
                self.write_camera_path(candidate_dir, centers=[0, 20, 40, 60], crop_heights=[540] * 4)
                write_camera_motion_audit_report(candidate_dir)
                (candidate_dir / "follow_cam_report.json").write_text(
                    json.dumps({"track_source": "raw", "frame_count": 4}) + "\n",
                    encoding="utf-8",
                )

            with patch("football_tracking.follow_cam_candidate_executor.FollowCamGenerator.run", autospec=True, side_effect=fake_run) as run:
                report = execute_follow_cam_candidate(
                    output_dir,
                    approval,
                    config_path=config_path,
                    input_video=root / "input.mp4",
                )

            candidate_dir = output_dir / "ai_candidates" / "follow_cam" / "follow-cam-1"
            self.assertEqual(1, run.call_count)
            self.assertEqual("pass", report["comparison_status"])
            self.assertTrue((candidate_dir / "follow_cam.mp4").exists())
            self.assertTrue((candidate_dir / "camera_path.csv").exists())
            self.assertTrue((candidate_dir / "follow_cam_report.json").exists())
            self.assertTrue((candidate_dir / "camera_motion_audit.json").exists())
            self.assertTrue((candidate_dir / "follow_cam_candidate_comparison.json").exists())
            self.assertTrue((candidate_dir / "candidate_manifest.json").exists())
            registry = load_candidate_registry(output_dir)
            self.assertEqual("loaded", registry["artifact_status"])
            self.assertEqual(["follow-cam-1"], [item["candidate_id"] for item in registry["candidates"]])
            lifecycle = build_ai_candidate_lifecycle(output_dir)
            self.assertEqual("follow_cam", lifecycle["candidates"][0]["problem_type"])
            manifest = build_final_artifact_manifest(
                baseline_output={"path": str(output_dir), "status": "baseline"},
                candidate_outputs=[
                    {
                        "id": report["candidate_id"],
                        "candidate_id": report["candidate_id"],
                        "problem_type": "follow_cam",
                        "path": report["candidate_dir"],
                        "type": "video",
                        "candidate_artifacts": report["candidate_artifacts"],
                    }
                ],
                final_artifacts=[],
                comparison_reports=[report],
                quality_gate_status={"status": "pass"},
            )
            self.assertEqual(1, manifest["summary"]["candidate_output_count"])
            self.assertEqual(0, manifest["summary"]["final_artifact_count"])

    def test_unknown_follow_cam_config_patch_key_fails_before_render(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            output_dir = root / "outputs" / "baseline"
            config_path = self.write_config(root, output_dir)
            self.write_track(output_dir)
            self.write_camera_path(output_dir, centers=[0, 160, 0], crop_heights=[540] * 3)
            write_camera_motion_audit_report(output_dir)

            with patch("football_tracking.follow_cam_candidate_executor.FollowCamGenerator.run") as run:
                with self.assertRaisesRegex(ValueError, "Unknown follow_cam config patch key"):
                    execute_follow_cam_candidate(
                        output_dir,
                        {
                            "approval_id": "camera_1",
                            "candidate_id": "follow-cam-1",
                            "approved_action": "adjust_follow_cam",
                            "config_patch": {"follow_cam": {"not_a_real_key": 1}},
                        },
                        config_path=config_path,
                        input_video=root / "input.mp4",
                    )

            run.assert_not_called()

    def test_tracking_rerun_before_follow_cam_blocks_without_linked_passed_tracking_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            output_dir = root / "outputs" / "baseline"
            config_path = self.write_config(root, output_dir)
            self.write_track(output_dir)

            with self.assertRaisesRegex(ValueError, "requires linked passed tracking candidate evidence"):
                execute_follow_cam_candidate(
                    output_dir,
                    {
                        "approval_id": "camera_1",
                        "candidate_id": "follow-cam-1",
                        "approved_action": "tracking_rerun_before_follow_cam",
                    },
                    config_path=config_path,
                    input_video=root / "input.mp4",
                )

    def test_linked_passed_tracking_candidate_can_be_used_for_rerender(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            output_dir = root / "outputs" / "baseline"
            config_path = self.write_config(root, output_dir)
            self.write_track(output_dir)
            self.write_camera_path(output_dir, centers=[0, 160, 0, 160], crop_heights=[540] * 4)
            write_camera_motion_audit_report(output_dir)
            tracking_dir = output_dir / "ai_candidates" / "missing_ball" / "track-pass"
            self.write_track(tracking_dir, xs=[0, 20, 40, 60])
            comparison = {
                "schema_version": "1.0",
                "problem_type": "missing_ball",
                "candidate_id": "track-pass",
                "comparison_status": "pass",
                "summary": {"status": "pass"},
                "checks": [{"name": "ok", "status": "pass"}],
                "candidate": {"id": "track-pass", "path": "ai_candidates/missing_ball/track-pass/ball_track.csv"},
                "candidate_artifacts": ["ai_candidates/missing_ball/track-pass/ball_track.csv"],
                "comparison_report": "ai_candidates/missing_ball/track-pass/missing_ball_recovery_comparison.json",
                "candidate_dir": "ai_candidates/missing_ball/track-pass",
                "consumed_approval_ids": ["track_1"],
            }
            (tracking_dir / "missing_ball_recovery_comparison.json").write_text(json.dumps(comparison), encoding="utf-8")

            def fake_run(instance: object) -> None:
                candidate_dir = instance.app_config.output_dir
                self.assertTrue((candidate_dir / "ball_track.csv").exists())
                (candidate_dir / "follow_cam.mp4").write_bytes(b"candidate-video")
                self.write_camera_path(candidate_dir, centers=[0, 20, 40, 60], crop_heights=[540] * 4)
                write_camera_motion_audit_report(candidate_dir)
                (candidate_dir / "follow_cam_report.json").write_text("{}\n", encoding="utf-8")

            with patch("football_tracking.follow_cam_candidate_executor.FollowCamGenerator.run", autospec=True, side_effect=fake_run):
                report = execute_follow_cam_candidate(
                    output_dir,
                    {
                        "approval_id": "camera_1",
                        "candidate_id": "follow-cam-1",
                        "approved_action": "tracking_rerun_before_follow_cam",
                        "linked_tracking_candidate_id": "track-pass",
                    },
                    config_path=config_path,
                    input_video=root / "input.mp4",
                )

        self.assertEqual("pass", report["comparison_status"])
        self.assertEqual("track-pass", report["linked_tracking_candidate"]["candidate_id"])

    def test_candidate_id_must_be_safe_single_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            for candidate_id in ("../x", "bad\\id", "CON", "trailingspace ", "dot."):
                with self.subTest(candidate_id=candidate_id):
                    with self.assertRaises(ValueError):
                        follow_cam_candidate_output_dir(output_dir, candidate_id)

    def test_render_failure_removes_candidate_dir_without_registry_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            output_dir = root / "outputs" / "baseline"
            config_path = self.write_config(root, output_dir)
            self.write_track(output_dir)
            self.write_camera_path(output_dir, centers=[0, 160, 0], crop_heights=[540] * 3)
            write_camera_motion_audit_report(output_dir)
            approval = {
                "approval_id": "camera_1",
                "candidate_id": "follow-cam-1",
                "approved_action": "adjust_follow_cam",
            }

            with patch(
                "football_tracking.follow_cam_candidate_executor.FollowCamGenerator.run",
                side_effect=RuntimeError("synthetic render failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "synthetic render failure"):
                    execute_follow_cam_candidate(
                        output_dir,
                        approval,
                        config_path=config_path,
                        input_video=root / "input.mp4",
                    )

            candidate_dir = output_dir / "ai_candidates" / "follow_cam" / "follow-cam-1"
            self.assertFalse(candidate_dir.exists())
            self.assertEqual("missing", load_candidate_registry(output_dir)["artifact_status"])

    def write_config(self, root: Path, output_dir: Path) -> Path:
        (root / "weights").mkdir(parents=True, exist_ok=True)
        (root / "input.mp4").write_bytes(b"fake")
        config_path = root / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    f"input_video: {root / 'input.mp4'}",
                    f"output_dir: {output_dir}",
                    "detector:",
                    f"  model_path: {root / 'weights' / 'model.pt'}",
                    "follow_cam:",
                    "  enabled: true",
                    "  output_video_name: follow_cam.mp4",
                    "  camera_path_name: camera_path.csv",
                    "  report_name: follow_cam_report.json",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return config_path

    def write_track(self, output_dir: Path, xs: list[int] | None = None) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        xs = xs or [0, 20, 40, 60]
        with (output_dir / "ball_track.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Frame", "X", "Y", "Confidence", "Status"])
            for frame, x in enumerate(xs):
                writer.writerow([frame, x, 100, 0.9, "Detected"])

    def write_camera_path(self, output_dir: Path, *, centers: list[int], crop_heights: list[int]) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        with (output_dir / "camera_path.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Frame", "CenterX", "CenterY", "CropX1", "CropY1", "CropX2", "CropY2", "CropWidth", "CropHeight", "Status", "TrackX", "TrackY", "PanMode"])
            for frame, (center, height) in enumerate(zip(centers, crop_heights)):
                writer.writerow([frame, center, height / 2, center - 480, 0, center + 480, height, 960, height, "Detected", center, height / 2, "glide"])


if __name__ == "__main__":
    unittest.main()
