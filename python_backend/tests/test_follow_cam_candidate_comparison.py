from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from football_tracking.camera_motion_audit import write_camera_motion_audit_report
from football_tracking.follow_cam_candidate_comparison import write_follow_cam_candidate_comparison


class FollowCamCandidateComparisonTests(unittest.TestCase):
    def test_smooth_candidate_vs_shaky_baseline_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            baseline = root / "baseline"
            candidate = root / "candidate"
            self.write_path(baseline, centers=[0, 160, 0, 160, 0], crop_heights=[540] * 5)
            self.write_path(candidate, centers=[0, 20, 40, 60, 80], crop_heights=[540] * 5)
            write_camera_motion_audit_report(baseline)
            write_camera_motion_audit_report(candidate)

            report_path = write_follow_cam_candidate_comparison(
                candidate,
                baseline_dir=baseline,
                candidate_id="follow-cam-1",
                approval=self.follow_cam_approval(start=0, end=4, roi=[0, 100, 20, 120]),
            )
            payload = self.read_json(report_path)

        self.assertEqual("pass", payload["comparison_status"])
        self.assertEqual("pass", self.check(payload, "motion_improvement")["status"])
        self.assertEqual("pass", self.check(payload, "target_window_visibility")["status"])

    def test_target_window_visibility_uses_window_track_points(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            baseline = root / "baseline"
            candidate = root / "candidate"
            self.write_path(baseline, centers=[100, 110, 120, 130], crop_heights=[540] * 4, track_x=[100, 110, 120, 130])
            self.write_path(candidate, centers=[100, 110, 120, 130], crop_heights=[540] * 4, track_x=[100, 110, 120, 130])
            write_camera_motion_audit_report(baseline)
            write_camera_motion_audit_report(candidate)

            payload = self.write_and_read(
                candidate,
                baseline,
                approval=self.follow_cam_approval(start=0, end=3, roi=None),
            )

        visibility = self.check(payload, "target_window_visibility")
        self.assertEqual("pass", payload["comparison_status"])
        self.assertEqual("pass", visibility["status"])
        self.assertEqual(4, visibility["sample_count"])
        self.assertEqual(["candidate_camera_path_track"], payload["metrics"]["target_window_visibility"]["sample_sources"])

    def test_target_window_visibility_counts_missing_candidate_frames_as_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            baseline = root / "baseline"
            candidate = root / "candidate"
            self.write_path(baseline, centers=[100, 110, 120, 130, 140], crop_heights=[540] * 5, track_x=[100, 110, 120, 130, 140])
            self.write_path(candidate, centers=[100, 110], crop_heights=[540] * 2, track_x=[100, 110])
            write_camera_motion_audit_report(baseline)
            write_camera_motion_audit_report(candidate)

            payload = self.write_and_read(
                candidate,
                baseline,
                approval=self.follow_cam_approval(start=0, end=4, roi=[100, 100, 120, 120]),
            )

        visibility = self.check(payload, "target_window_visibility")
        self.assertEqual("fail", payload["comparison_status"])
        self.assertEqual("fail", visibility["status"])
        self.assertGreaterEqual(visibility["hidden_sample_count"], 3)

    def test_target_window_visibility_checks_roi_and_track_samples_on_same_frame(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            baseline = root / "baseline"
            candidate = root / "candidate"
            self.write_path(baseline, centers=[100, 110, 120, 130], crop_heights=[540] * 4, track_x=[100, 110, 120, 130])
            self.write_path(candidate, centers=[100, 110, 120, 130], crop_heights=[540] * 4, track_x=[2000, 2000, 2000, 2000])
            write_camera_motion_audit_report(baseline)
            write_camera_motion_audit_report(candidate)

            payload = self.write_and_read(
                candidate,
                baseline,
                approval=self.follow_cam_approval(start=0, end=3, roi=[100, 100, 120, 120]),
            )

        visibility = self.check(payload, "target_window_visibility")
        self.assertEqual("fail", payload["comparison_status"])
        self.assertEqual("fail", visibility["status"])
        self.assertEqual(
            ["candidate_camera_path_track", "effective_roi"],
            payload["metrics"]["target_window_visibility"]["sample_sources"],
        )

    def test_target_window_visibility_ignores_nested_provenance_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            baseline = root / "baseline"
            candidate = root / "candidate"
            self.write_path(baseline, centers=[100, 110, 120, 130], crop_heights=[540] * 4, track_x=[100, 110, 120, 130])
            self.write_path(candidate, centers=[100, 110, 120, 130], crop_heights=[540] * 4, track_x=[100, 110, 120, 130])
            write_camera_motion_audit_report(baseline)
            write_camera_motion_audit_report(candidate)
            approval = self.follow_cam_approval(start=0, end=3, roi=[100, 100, 120, 120])
            approval["evidence"] = {
                "source": {"start_frame": 100, "end_frame": 200},
                "effective_roi": [5000, 5000, 5100, 5100],
            }

            payload = self.write_and_read(candidate, baseline, approval=approval)

        visibility = self.check(payload, "target_window_visibility")
        self.assertEqual("pass", payload["comparison_status"])
        self.assertEqual("pass", visibility["status"])
        self.assertEqual([{"start_frame": 0, "end_frame": 3, "frame_count": 4}], visibility["target_windows"])

    def test_target_window_visibility_merges_overlapping_explicit_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            baseline = root / "baseline"
            candidate = root / "candidate"
            centers = [100, 110, 120, 130, 140, 150]
            self.write_path(baseline, centers=centers, crop_heights=[540] * 6, track_x=centers)
            self.write_path(candidate, centers=centers, crop_heights=[540] * 6, track_x=centers)
            write_camera_motion_audit_report(baseline)
            write_camera_motion_audit_report(candidate)
            approval = self.follow_cam_approval(start=0, end=3, roi=None)
            approval["rerun_scope"] = {"start_frame": 2, "end_frame": 5}

            payload = self.write_and_read(candidate, baseline, approval=approval)

        visibility = self.check(payload, "target_window_visibility")
        self.assertEqual("pass", payload["comparison_status"])
        self.assertEqual("pass", visibility["status"])
        self.assertEqual(6, visibility["sample_count"])
        self.assertEqual([{"start_frame": 0, "end_frame": 5, "frame_count": 6}], visibility["target_windows"])

    def test_target_window_visibility_without_target_evidence_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            baseline = root / "baseline"
            candidate = root / "candidate"
            self.write_path(baseline, centers=[100, 110, 120, 130], crop_heights=[540] * 4, track_x=[100, 110, 120, 130])
            self.write_path(candidate, centers=[100, 110, 120, 130], crop_heights=[540] * 4, track_x=[100, 110, 120, 130])
            write_camera_motion_audit_report(baseline)
            write_camera_motion_audit_report(candidate)

            payload = self.write_and_read(
                candidate,
                baseline,
                approval={"approval_id": "camera_1", "approved_action": "adjust_follow_cam"},
            )

        self.assertEqual("unavailable", payload["comparison_status"])
        self.assertEqual("unavailable", self.check(payload, "target_window_visibility")["status"])

    def test_shaky_candidate_vs_stable_baseline_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            baseline = root / "baseline"
            candidate = root / "candidate"
            self.write_path(baseline, centers=[0, 10, 20, 30, 40], crop_heights=[540] * 5)
            self.write_path(candidate, centers=[0, 180, 0, 180, 0], crop_heights=[540] * 5)
            write_camera_motion_audit_report(baseline)
            write_camera_motion_audit_report(candidate)

            payload = self.write_and_read(
                candidate,
                baseline,
                approval={"approval_id": "camera_1", "approved_action": "adjust_follow_cam"},
            )

        self.assertEqual("fail", payload["comparison_status"])
        self.assertEqual("fail", self.check(payload, "review_events_not_worse")["status"])

    def test_zoom_out_only_candidate_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            baseline = root / "baseline"
            candidate = root / "candidate"
            self.write_path(baseline, centers=[0, 120, 240, 360, 480], crop_heights=[540] * 5)
            self.write_path(candidate, centers=[0, 120, 240, 360, 480], crop_heights=[900] * 5)
            write_camera_motion_audit_report(baseline)
            write_camera_motion_audit_report(candidate)

            payload = self.write_and_read(
                candidate,
                baseline,
                approval={"approval_id": "camera_1", "approved_action": "adjust_follow_cam"},
            )

        self.assertEqual("fail", payload["comparison_status"])
        self.assertEqual("fail", self.check(payload, "not_zoom_out_only")["status"])

    def test_ball_crop_coverage_drop_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            baseline = root / "baseline"
            candidate = root / "candidate"
            self.write_path(baseline, centers=[500, 510, 520, 530], crop_heights=[540] * 4, track_x=[500, 510, 520, 530])
            self.write_path(candidate, centers=[0, 10, 20, 30], crop_heights=[540] * 4, track_x=[500, 510, 520, 530])
            write_camera_motion_audit_report(baseline)
            write_camera_motion_audit_report(candidate)

            payload = self.write_and_read(
                candidate,
                baseline,
                approval={"approval_id": "camera_1", "approved_action": "adjust_follow_cam"},
            )

        self.assertEqual("fail", payload["comparison_status"])
        coverage = self.check(payload, "ball_crop_coverage")
        self.assertEqual("fail", coverage["status"])
        self.assertLess(coverage["candidate_value"], coverage["baseline_value"])

    def test_missing_candidate_track_points_count_as_not_covered(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            baseline = root / "baseline"
            candidate = root / "candidate"
            self.write_path(baseline, centers=[500, 510, 520, 530], crop_heights=[540] * 4, track_x=[500, 510, 520, 530])
            self.write_path(candidate, centers=[500, 510, 520, 530], crop_heights=[540] * 4, track_x=[500, None, None, 530])
            write_camera_motion_audit_report(baseline)
            write_camera_motion_audit_report(candidate)

            payload = self.write_and_read(candidate, baseline)

        self.assertEqual("fail", payload["comparison_status"])
        coverage = self.check(payload, "ball_crop_coverage")
        self.assertEqual("fail", coverage["status"])
        self.assertEqual(0.5, coverage["candidate_value"])
        self.assertEqual(2, payload["metrics"]["ball_crop_coverage"]["candidate_missing_track_frames"])

    def test_sparse_camera_path_is_unavailable_not_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            baseline = root / "baseline"
            candidate = root / "candidate"
            self.write_path(baseline, centers=[0, 100], crop_heights=[540, 540])
            self.write_path(candidate, centers=[0, 10], crop_heights=[540, 540])
            write_camera_motion_audit_report(baseline)
            write_camera_motion_audit_report(candidate)

            payload = self.write_and_read(
                candidate,
                baseline,
                approval={"approval_id": "camera_1", "approved_action": "adjust_follow_cam"},
            )

        self.assertEqual("unavailable", payload["comparison_status"])
        self.assertEqual("unavailable", self.check(payload, "camera_path_evidence_available")["status"])

    def write_and_read(
        self,
        candidate: Path,
        baseline: Path,
        *,
        approval: dict[str, object] | None = None,
    ) -> dict[str, object]:
        report_path = write_follow_cam_candidate_comparison(
            candidate,
            baseline_dir=baseline,
            candidate_id="follow-cam-1",
            approval=approval or self.follow_cam_approval(start=0, end=4, roi=[0, 100, 20, 120]),
        )
        return self.read_json(report_path)

    def follow_cam_approval(self, *, start: int, end: int, roi: list[int] | None) -> dict[str, object]:
        approval: dict[str, object] = {
            "approval_id": "camera_1",
            "approved_action": "adjust_follow_cam",
            "start_frame": start,
            "end_frame": end,
        }
        if roi is not None:
            approval["effective_roi"] = roi
        return approval

    def write_path(
        self,
        output_dir: Path,
        *,
        centers: list[int],
        crop_heights: list[int],
        track_x: list[int | None] | None = None,
    ) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        rows = []
        for index, (center_x, crop_height) in enumerate(zip(centers, crop_heights)):
            crop_width = 960
            x1 = int(center_x - crop_width / 2)
            x2 = int(center_x + crop_width / 2)
            y1 = 0
            y2 = crop_height
            tx = center_x if track_x is None else track_x[index]
            rows.append(
                {
                    "Frame": index,
                    "CenterX": center_x,
                    "CenterY": crop_height / 2,
                    "CropX1": x1,
                    "CropY1": y1,
                    "CropX2": x2,
                    "CropY2": y2,
                    "CropWidth": crop_width,
                    "CropHeight": crop_height,
                    "Status": "Detected",
                    "TrackX": "" if tx is None else tx,
                    "TrackY": "" if tx is None else crop_height / 2,
                    "PanMode": "glide",
                }
            )
        with (output_dir / "camera_path.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    def read_json(self, path: Path) -> dict[str, object]:
        return json.loads(path.read_text(encoding="utf-8"))

    def check(self, payload: dict[str, object], name: str) -> dict[str, object]:
        checks = payload["checks"]
        self.assertIsInstance(checks, list)
        for check in checks:
            if isinstance(check, dict) and check.get("name") == name:
                return check
        raise AssertionError(f"missing check {name}")


if __name__ == "__main__":
    unittest.main()
