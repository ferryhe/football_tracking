from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from football_tracking.final_artifact_manifest import (
    FINAL_ARTIFACT_MANIFEST_NAME,
    build_final_artifact_manifest,
    write_final_artifact_manifest,
)


FIXED_NOW = "2026-06-23T00:00:00+00:00"


class FinalArtifactManifestTests(unittest.TestCase):
    def test_manifest_records_baseline_candidates_final_approvals_quality_gate_and_media(self) -> None:
        with patch("football_tracking.final_artifact_manifest._utc_now_iso", return_value=FIXED_NOW):
            payload = build_final_artifact_manifest(
                baseline_output={"path": "baseline/output.mp4", "status": "available", "type": "video"},
                candidate_outputs=[
                    {"id": "candidate-pass", "path": "candidate/pass.mp4", "status": "available", "type": "video"},
                    {"id": "candidate-warn", "path": "candidate/warn.mp4", "status": "available", "type": "clip"},
                    {"id": "candidate-fail", "path": "candidate/fail.mp4", "status": "available", "type": "video"},
                ],
                final_artifacts=[
                    {"candidate_id": "candidate-pass", "path": "final/pass.mp4", "type": "video"},
                    {
                        "candidate_id": "candidate-warn",
                        "path": "final/warn.mp4",
                        "type": "clip",
                        "requires_human_confirmation": True,
                    },
                    {"candidate_id": "candidate-fail", "path": "final/fail.mp4", "type": "video"},
                ],
                consumed_approvals=[
                    {
                        "approval_id": "approval-warn",
                        "candidate_id": "candidate-warn",
                        "status": "approved",
                        "approval_type": "human_confirmation",
                    }
                ],
                comparison_reports=[
                    _comparison("candidate-pass", "pass", "missing_ball_comparison.json"),
                    _comparison("candidate-warn", "warn", "noise_comparison.json"),
                    _comparison("candidate-fail", "fail", "follow_cam_comparison.json"),
                ],
                quality_gate_status={"status": "pass", "report_path": "ai_improvement_quality_gate.json"},
                warnings=["operator confirmed candidate-warn"],
            )

        self.assertEqual("1.0", payload["schema_version"])
        self.assertEqual(FIXED_NOW, payload["generated_at"])
        self.assertEqual("baseline", payload["baseline_output"]["role"])
        self.assertEqual(["candidate", "candidate", "candidate"], [item["role"] for item in payload["candidate_outputs"]])
        self.assertEqual("pass", payload["quality_gate_status"]["status"])
        self.assertEqual(["approval-warn"], [item["approval_id"] for item in payload["consumed_approvals"]])
        self.assertEqual(["candidate-pass", "candidate-warn"], [item["candidate_id"] for item in payload["final_selected_artifacts"]])
        self.assertEqual(["candidate-fail"], [item["candidate_id"] for item in payload["rejected_candidates"]])
        self.assertEqual("comparison_failed", payload["rejected_candidates"][0]["reason"])
        self.assertEqual(4, len(payload["videos"]))
        self.assertEqual(2, len(payload["clips"]))
        self.assertEqual(["candidate-pass", "candidate-warn", "candidate-fail"], [item["candidate_id"] for item in payload["comparison_reports"]])

    def test_warn_candidate_is_not_silently_promoted_without_confirmation(self) -> None:
        with patch("football_tracking.final_artifact_manifest._utc_now_iso", return_value=FIXED_NOW):
            payload = build_final_artifact_manifest(
                baseline_output={"path": "baseline/output.mp4", "type": "video"},
                candidate_outputs=[{"id": "candidate-warn", "path": "candidate/warn.mp4", "type": "clip"}],
                final_artifacts=[
                    {
                        "candidate_id": "candidate-warn",
                        "path": "final/warn.mp4",
                        "type": "clip",
                        "requires_human_confirmation": True,
                    }
                ],
                comparison_reports=[_comparison("candidate-warn", "warn", "noise_comparison.json")],
                quality_gate_status={"status": "warn"},
            )

        self.assertEqual([], payload["final_selected_artifacts"])
        self.assertEqual("candidate-warn", payload["rejected_candidates"][0]["candidate_id"])
        self.assertEqual("requires_human_confirmation", payload["rejected_candidates"][0]["reason"])
        self.assertIn("candidate-warn requires human confirmation before promotion", payload["warnings"])

    def test_warn_candidate_is_promoted_with_consumed_human_confirmation(self) -> None:
        with patch("football_tracking.final_artifact_manifest._utc_now_iso", return_value=FIXED_NOW):
            payload = build_final_artifact_manifest(
                baseline_output={"path": "baseline/output.mp4", "type": "video"},
                candidate_outputs=[{"id": "candidate-warn", "path": "candidate/warn.mp4", "type": "clip"}],
                final_artifacts=[
                    {
                        "candidate_id": "candidate-warn",
                        "path": "final/warn.mp4",
                        "type": "clip",
                        "requires_human_confirmation": True,
                    }
                ],
                consumed_approvals=[
                    {
                        "approval_id": "approval-warn",
                        "candidate_id": "candidate-warn",
                        "status": "approved",
                        "approval_type": "human_confirmation",
                    }
                ],
                comparison_reports=[_comparison("candidate-warn", "warn", "noise_comparison.json")],
                quality_gate_status={"status": "warn"},
            )

        self.assertEqual(["candidate-warn"], [item["candidate_id"] for item in payload["final_selected_artifacts"]])
        self.assertEqual([], payload["rejected_candidates"])

    def test_warn_candidate_requires_explicit_approved_confirmation_status(self) -> None:
        with patch("football_tracking.final_artifact_manifest._utc_now_iso", return_value=FIXED_NOW):
            payload = build_final_artifact_manifest(
                baseline_output={"path": "baseline/output.mp4", "type": "video"},
                candidate_outputs=[{"id": "candidate-warn", "path": "candidate/warn.mp4", "type": "clip"}],
                final_artifacts=[
                    {
                        "candidate_id": "candidate-warn",
                        "path": "final/warn.mp4",
                        "type": "clip",
                        "requires_human_confirmation": True,
                    }
                ],
                consumed_approvals=[
                    {
                        "approval_id": "approval-warn",
                        "candidate_id": "candidate-warn",
                        "approval_type": "human_confirmation",
                    }
                ],
                comparison_reports=[_comparison("candidate-warn", "warn", "noise_comparison.json")],
                quality_gate_status={"status": "warn"},
            )

        self.assertEqual([], payload["final_selected_artifacts"])
        self.assertEqual("requires_human_confirmation", payload["rejected_candidates"][0]["reason"])

    def test_duplicate_comparison_reports_use_worst_status_for_promotion(self) -> None:
        with patch("football_tracking.final_artifact_manifest._utc_now_iso", return_value=FIXED_NOW):
            payload = build_final_artifact_manifest(
                baseline_output={"path": "baseline/output.mp4", "type": "video"},
                candidate_outputs=[{"id": "candidate-mixed", "path": "candidate/mixed.mp4", "type": "video"}],
                final_artifacts=[{"candidate_id": "candidate-mixed", "path": "final/mixed.mp4", "type": "video"}],
                comparison_reports=[
                    _comparison("candidate-mixed", "fail", "first_fail_comparison.json"),
                    _comparison("candidate-mixed", "pass", "later_pass_comparison.json"),
                ],
                quality_gate_status={"status": "fail"},
            )

        self.assertEqual([], payload["final_selected_artifacts"])
        self.assertEqual("candidate-mixed", payload["rejected_candidates"][0]["candidate_id"])
        self.assertEqual("comparison_failed", payload["rejected_candidates"][0]["reason"])

    def test_final_artifact_without_candidate_id_is_not_promoted(self) -> None:
        with patch("football_tracking.final_artifact_manifest._utc_now_iso", return_value=FIXED_NOW):
            payload = build_final_artifact_manifest(
                baseline_output={"path": "baseline/output.mp4", "type": "video"},
                candidate_outputs=[{"id": "candidate-pass", "path": "candidate/pass.mp4", "type": "video"}],
                final_artifacts=[{"path": "final/unknown.mp4", "type": "video"}],
                comparison_reports=[_comparison("candidate-pass", "pass", "missing_ball_comparison.json")],
                quality_gate_status={"status": "pass"},
            )

        self.assertEqual([], payload["final_selected_artifacts"])
        self.assertEqual("missing_candidate_id", payload["rejected_candidates"][0]["reason"])

    def test_final_artifact_unknown_candidate_id_is_not_promoted(self) -> None:
        with patch("football_tracking.final_artifact_manifest._utc_now_iso", return_value=FIXED_NOW):
            payload = build_final_artifact_manifest(
                baseline_output={"path": "baseline/output.mp4", "type": "video"},
                candidate_outputs=[{"id": "candidate-pass", "path": "candidate/pass.mp4", "type": "video"}],
                final_artifacts=[{"candidate_id": "candidate-ghost", "path": "final/ghost.mp4", "type": "video"}],
                comparison_reports=[_comparison("candidate-ghost", "pass", "ghost_comparison.json")],
                quality_gate_status={"status": "pass"},
            )

        self.assertEqual([], payload["final_selected_artifacts"])
        self.assertEqual("unknown_candidate_id", payload["rejected_candidates"][0]["reason"])

    def test_summary_pass_with_failing_check_is_not_promoted(self) -> None:
        comparison = _comparison("candidate-lie", "pass", "lie_comparison.json")
        comparison["checks"] = [{"name": "camera_regression", "status": "fail"}]
        with patch("football_tracking.final_artifact_manifest._utc_now_iso", return_value=FIXED_NOW):
            payload = build_final_artifact_manifest(
                baseline_output={"path": "baseline/output.mp4", "type": "video"},
                candidate_outputs=[{"id": "candidate-lie", "path": "candidate/lie.mp4", "type": "video"}],
                final_artifacts=[{"candidate_id": "candidate-lie", "path": "final/lie.mp4", "type": "video"}],
                comparison_reports=[comparison],
                quality_gate_status={"status": "fail"},
            )

        self.assertEqual([], payload["final_selected_artifacts"])
        self.assertEqual("comparison_failed", payload["rejected_candidates"][0]["reason"])
        self.assertEqual("summary_check_mismatch", payload["comparison_reports"][0]["artifact_status"])

    def test_unavailable_candidate_is_not_promoted(self) -> None:
        with patch("football_tracking.final_artifact_manifest._utc_now_iso", return_value=FIXED_NOW):
            payload = build_final_artifact_manifest(
                baseline_output={"path": "baseline/output.mp4", "type": "video"},
                candidate_outputs=[{"id": "candidate-unavailable", "path": "candidate/unavailable.mp4", "type": "video"}],
                final_artifacts=[{"candidate_id": "candidate-unavailable", "path": "final/unavailable.mp4", "type": "video"}],
                comparison_reports=[_comparison("candidate-unavailable", "unavailable", "missing_report.json")],
                quality_gate_status={"status": "warn"},
            )

        self.assertEqual([], payload["final_selected_artifacts"])
        self.assertEqual("comparison_unavailable", payload["rejected_candidates"][0]["reason"])

    def test_write_manifest_records_paths_only_and_does_not_copy_or_move_media(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            candidate_video = output_dir / "candidate" / "pass.mp4"
            final_video = output_dir / "final" / "pass.mp4"
            candidate_video.parent.mkdir(parents=True)
            candidate_video.write_bytes(b"video-bytes")

            with patch("football_tracking.final_artifact_manifest._utc_now_iso", return_value=FIXED_NOW):
                payload = write_final_artifact_manifest(
                    output_dir,
                    baseline_output={"path": str(output_dir / "baseline" / "output.mp4"), "type": "video"},
                    candidate_outputs=[{"id": "candidate-pass", "path": str(candidate_video), "type": "video"}],
                    final_artifacts=[{"candidate_id": "candidate-pass", "path": str(final_video), "type": "video"}],
                    comparison_reports=[_comparison("candidate-pass", "pass", "missing_ball_comparison.json")],
                    quality_gate_status={"status": "pass"},
                )
                loaded = json.loads((output_dir / FINAL_ARTIFACT_MANIFEST_NAME).read_text(encoding="utf-8"))

            self.assertEqual(payload, loaded)
            self.assertTrue(candidate_video.exists())
            self.assertFalse(final_video.exists())
            self.assertEqual(str(final_video), loaded["final_selected_artifacts"][0]["path"])

    def test_write_manifest_rejects_path_traversal_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)

            with self.assertRaisesRegex(ValueError, "manifest name"):
                write_final_artifact_manifest(
                    output_dir,
                    baseline_output={"path": "baseline/output.mp4"},
                    candidate_outputs=[],
                    final_artifacts=[],
                    name="../escape.json",
                )


def _comparison(candidate_id: str, status: str, path: str) -> dict[str, object]:
    return {
        "path": path,
        "problem_type": "missing_ball",
        "candidate": {"id": candidate_id},
        "summary": {
            "status": status,
            "check_count": 1,
            "failed_check_count": 1 if status == "fail" else 0,
            "warning_count": 1 if status == "warn" else 0,
            "unavailable_count": 1 if status == "unavailable" else 0,
        },
        "checks": [{"name": "comparison", "status": status}],
    }


if __name__ == "__main__":
    unittest.main()
