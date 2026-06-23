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
                approval={"approval_id": "camera_1", "approved_action": "adjust_follow_cam"},
            )
            payload = self.read_json(report_path)

        self.assertEqual("pass", payload["comparison_status"])
        self.assertEqual("pass", self.check(payload, "motion_improvement")["status"])

    def test_shaky_candidate_vs_stable_baseline_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            baseline = root / "baseline"
            candidate = root / "candidate"
            self.write_path(baseline, centers=[0, 10, 20, 30, 40], crop_heights=[540] * 5)
            self.write_path(candidate, centers=[0, 180, 0, 180, 0], crop_heights=[540] * 5)
            write_camera_motion_audit_report(baseline)
            write_camera_motion_audit_report(candidate)

            payload = self.write_and_read(candidate, baseline)

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

            payload = self.write_and_read(candidate, baseline)

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

            payload = self.write_and_read(candidate, baseline)

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

            payload = self.write_and_read(candidate, baseline)

        self.assertEqual("unavailable", payload["comparison_status"])
        self.assertEqual("unavailable", self.check(payload, "camera_path_evidence_available")["status"])

    def write_and_read(self, candidate: Path, baseline: Path) -> dict[str, object]:
        report_path = write_follow_cam_candidate_comparison(
            candidate,
            baseline_dir=baseline,
            candidate_id="follow-cam-1",
            approval={"approval_id": "camera_1", "approved_action": "adjust_follow_cam"},
        )
        return self.read_json(report_path)

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
