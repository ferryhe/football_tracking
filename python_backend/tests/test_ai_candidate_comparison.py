from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from football_tracking.ai_candidate_comparison import (
    ARTIFACT_ROLES,
    CANDIDATE_STATUSES,
    build_candidate_comparison,
    write_candidate_comparison_report,
)


FIXED_NOW = "2026-06-23T00:00:00+00:00"


class AiCandidateComparisonTests(unittest.TestCase):
    def test_contract_defines_shared_roles_and_statuses(self) -> None:
        self.assertEqual(("baseline", "candidate", "final"), ARTIFACT_ROLES)
        self.assertEqual(("pass", "warn", "fail", "unavailable"), CANDIDATE_STATUSES)

    def test_build_pass_candidate_comparison_golden_json(self) -> None:
        with patch("football_tracking.ai_candidate_comparison._utc_now_iso", return_value=FIXED_NOW):
            payload = build_candidate_comparison(
                problem_type="missing_ball",
                baseline={"path": "baseline/ball_track.csv", "status": "available"},
                candidate={"id": "candidate-pass", "path": "candidate/ball_track.csv", "status": "available"},
                approval={"approval_id": "approval-pass", "status": "approved"},
                checks=[
                    {
                        "name": "lost_gap_covered",
                        "status": "pass",
                        "baseline_value": 496,
                        "candidate_value": 0,
                    }
                ],
            )

        self.assertEqual(
            {
                "schema_version": "1.0",
                "generated_at": FIXED_NOW,
                "problem_type": "missing_ball",
                "baseline": {"role": "baseline", "path": "baseline/ball_track.csv", "status": "available"},
                "candidate": {
                    "role": "candidate",
                    "id": "candidate-pass",
                    "path": "candidate/ball_track.csv",
                    "status": "available",
                },
                "approval": {"approval_id": "approval-pass", "status": "approved"},
                "summary": {
                    "status": "pass",
                    "check_count": 1,
                    "passed_check_count": 1,
                    "failed_check_count": 0,
                    "warning_count": 0,
                    "unavailable_count": 0,
                    "requires_human_confirmation": False,
                    "promotion_eligible": True,
                },
                "checks": [
                    {
                        "name": "lost_gap_covered",
                        "status": "pass",
                        "baseline_value": 496,
                        "candidate_value": 0,
                    }
                ],
            },
            payload,
        )

    def test_warn_candidate_requires_human_confirmation_before_promotion(self) -> None:
        with patch("football_tracking.ai_candidate_comparison._utc_now_iso", return_value=FIXED_NOW):
            payload = build_candidate_comparison(
                problem_type="noise",
                baseline={"path": "baseline/noise.json"},
                candidate={"id": "candidate-warn", "path": "candidate/noise.json"},
                checks=[
                    {"name": "false_positive_reduction", "status": "pass"},
                    {"name": "precision_regression_budget", "status": "warn", "reason": "minor regression"},
                ],
            )

        self.assertEqual("warn", payload["summary"]["status"])
        self.assertEqual(1, payload["summary"]["warning_count"])
        self.assertTrue(payload["summary"]["requires_human_confirmation"])
        self.assertFalse(payload["summary"]["promotion_eligible"])

    def test_fail_candidate_comparison_counts_failures_and_blocks_promotion(self) -> None:
        with patch("football_tracking.ai_candidate_comparison._utc_now_iso", return_value=FIXED_NOW):
            payload = build_candidate_comparison(
                problem_type="follow_cam",
                baseline={"path": "baseline/camera_motion_audit.json"},
                candidate={"id": "candidate-fail", "path": "candidate/camera_motion_audit.json"},
                checks=[
                    {"name": "camera_regression", "status": "fail", "reason": "p95 pan increased"},
                    {"name": "render_available", "status": "pass"},
                ],
            )

        self.assertEqual("fail", payload["summary"]["status"])
        self.assertEqual(1, payload["summary"]["failed_check_count"])
        self.assertFalse(payload["summary"]["promotion_eligible"])

    def test_unavailable_candidate_comparison_has_unavailable_status(self) -> None:
        with patch("football_tracking.ai_candidate_comparison._utc_now_iso", return_value=FIXED_NOW):
            payload = build_candidate_comparison(
                problem_type="highlight",
                baseline={"path": "baseline/event_candidates.json"},
                candidate={"id": "candidate-unavailable", "path": "candidate/highlight.mp4"},
                checks=[{"name": "clip_rendered", "status": "unavailable", "reason": "dry run"}],
            )

        self.assertEqual("unavailable", payload["summary"]["status"])
        self.assertEqual(1, payload["summary"]["unavailable_count"])
        self.assertTrue(payload["summary"]["requires_human_confirmation"])
        self.assertFalse(payload["summary"]["promotion_eligible"])

    def test_unavailable_status_takes_precedence_over_warning(self) -> None:
        with patch("football_tracking.ai_candidate_comparison._utc_now_iso", return_value=FIXED_NOW):
            payload = build_candidate_comparison(
                problem_type="noise",
                baseline={"path": "baseline/noise.json"},
                candidate={"id": "candidate-mixed", "path": "candidate/noise.json"},
                checks=[
                    {"name": "precision_regression_budget", "status": "warn"},
                    {"name": "false_positive_count_missing", "status": "unavailable"},
                ],
            )

        self.assertEqual("unavailable", payload["summary"]["status"])
        self.assertFalse(payload["summary"]["promotion_eligible"])

    def test_candidate_comparison_requires_at_least_one_check(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one check"):
            build_candidate_comparison(
                problem_type="missing_ball",
                baseline={"path": "baseline"},
                candidate={"id": "candidate-empty", "path": "candidate"},
                checks=[],
            )

    def test_write_candidate_comparison_report_persists_json_without_mutating_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            before_hashes = _track_hashes(output_dir)
            before_contents = _track_contents(output_dir)

            with patch("football_tracking.ai_candidate_comparison._utc_now_iso", return_value=FIXED_NOW):
                payload = build_candidate_comparison(
                    problem_type="missing_ball",
                    baseline={"path": str(output_dir / "ball_track.csv")},
                    candidate={"id": "candidate-pass", "path": "candidate/ball_track.csv"},
                    checks=[{"name": "lost_gap_covered", "status": "pass"}],
                )
                written_path = write_candidate_comparison_report(
                    output_dir,
                    payload,
                    name="missing_ball_comparison.json",
                )

            loaded = json.loads(written_path.read_text(encoding="utf-8"))

            self.assertEqual(payload, loaded)
            self.assertEqual("missing_ball_comparison.json", written_path.name)
            self.assertEqual(before_hashes, _track_hashes(output_dir))
            self.assertEqual(before_contents, _track_contents(output_dir))

    def test_write_candidate_comparison_report_rejects_path_traversal_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            payload = build_candidate_comparison(
                problem_type="missing_ball",
                baseline={"path": "baseline"},
                candidate={"id": "candidate-pass", "path": "candidate"},
                checks=[{"name": "comparison", "status": "pass"}],
            )

            with self.assertRaisesRegex(ValueError, "report name"):
                write_candidate_comparison_report(output_dir, payload, name="../escape.json")

            with self.assertRaisesRegex(ValueError, "report name"):
                write_candidate_comparison_report(output_dir, payload, name="bad:name.json")

            with self.assertRaisesRegex(ValueError, "report name"):
                write_candidate_comparison_report(output_dir, payload, name="CON.json")


def _write_tracks(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw = "Frame,X,Y,Status\n1,10,20,Detected\n"
    (output_dir / "ball_track.csv").write_text(raw, encoding="utf-8")
    (output_dir / "ball_track.cleaned.csv").write_text(raw, encoding="utf-8")


def _track_hashes(output_dir: Path) -> dict[str, str]:
    return {
        file_name: hashlib.sha256((output_dir / file_name).read_bytes()).hexdigest()
        for file_name in ("ball_track.csv", "ball_track.cleaned.csv")
    }


def _track_contents(output_dir: Path) -> dict[str, str]:
    return {
        file_name: (output_dir / file_name).read_text(encoding="utf-8")
        for file_name in ("ball_track.csv", "ball_track.cleaned.csv")
    }


if __name__ == "__main__":
    unittest.main()
