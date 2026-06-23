from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from football_tracking.final_artifact_manifest import (
    FINAL_ARTIFACT_MANIFEST_NAME,
    build_final_artifact_manifest,
    finalize_ai_candidate,
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
        self.assertEqual(1, payload["summary"]["comparison_counts_by_problem_type"]["noise"])
        self.assertEqual(1, payload["summary"]["comparison_counts_by_status"]["pass"])
        self.assertEqual(1, payload["summary"]["comparison_counts_by_status"]["warn"])
        self.assertEqual(1, payload["summary"]["comparison_counts_by_status"]["fail"])

    def test_manifest_summarizes_pending_unsupported_and_resolved_noop_candidates(self) -> None:
        with patch("football_tracking.final_artifact_manifest._utc_now_iso", return_value=FIXED_NOW):
            payload = build_final_artifact_manifest(
                baseline_output={"path": "baseline/output.mp4", "status": "available", "type": "video"},
                candidate_outputs=[],
                final_artifacts=[],
                pending_candidates=[{"candidate_id": "candidate-pending", "problem_type": "missing_ball"}],
                unsupported_candidates=[
                    {
                        "approval_id": "noise_1",
                        "problem_type": "noise",
                        "approved_action": "noise_filter_adjustment",
                        "reason": "unsupported_candidate_type",
                    }
                ],
                resolved_noop_candidates=[
                    {
                        "candidate_id": "resolved_2079",
                        "approval_id": "noop_2079",
                        "problem_type": "missing_ball",
                        "status": "resolved_not_visible",
                    }
                ],
                quality_gate_status={"status": "pass"},
            )

        self.assertEqual(["candidate-pending"], [item["candidate_id"] for item in payload["pending_candidates"]])
        self.assertEqual(["noise_1"], [item["approval_id"] for item in payload["unsupported_candidates"]])
        self.assertEqual(["resolved_2079"], [item["candidate_id"] for item in payload["resolved_noop_candidates"]])
        self.assertEqual(1, payload["summary"]["pending_candidate_count"])
        self.assertEqual(1, payload["summary"]["unsupported_candidate_count"])
        self.assertEqual(1, payload["summary"]["resolved_noop_candidate_count"])

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

    def test_finalize_pass_candidate_promotes_missing_ball_track_with_operator_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_seed_manifest(
                output_dir,
                [
                    _candidate_seed(
                        output_dir,
                        problem_type="missing_ball",
                        candidate_id="candidate-pass",
                        approval_id="approval-pass",
                        status="pass",
                    )
                ],
            )

            result = finalize_ai_candidate(
                output_dir,
                problem_type="missing_ball",
                candidate_id="candidate-pass",
                approval_id="approval-pass",
                decision="promote",
                output_role="missing_ball_track",
                note="operator checked the recovered window",
            )
            loaded = json.loads((output_dir / FINAL_ARTIFACT_MANIFEST_NAME).read_text(encoding="utf-8"))

        self.assertEqual(loaded, result["manifest"])
        self.assertEqual(["candidate-pass"], [item["candidate_id"] for item in loaded["final_selected_artifacts"]])
        selected = loaded["final_selected_artifacts"][0]
        self.assertEqual("missing_ball_track", selected["output_role"])
        self.assertEqual("ai_candidates/missing_ball/candidate-pass/ball_track.csv", selected["path"])
        self.assertEqual("approval-pass", selected["approval_id"])
        self.assertEqual("promote", selected["operator_decision"]["decision"])
        self.assertEqual("operator checked the recovered window", selected["operator_decision"]["note"])
        self.assertFalse(selected["operator_decision"]["confirm_warn"])
        self.assertEqual("promoted", result["lifecycle"]["summary"]["promotion_status"])

    def test_finalize_warn_candidate_requires_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_seed_manifest(
                output_dir,
                [
                    _candidate_seed(
                        output_dir,
                        problem_type="noise",
                        candidate_id="candidate-warn",
                        approval_id="approval-warn",
                        status="warn",
                    )
                ],
            )

            with self.assertRaisesRegex(ValueError, "confirmation"):
                finalize_ai_candidate(
                    output_dir,
                    problem_type="noise",
                    candidate_id="candidate-warn",
                    approval_id="approval-warn",
                    decision="promote",
                    output_role="noise_cleaned_track",
                )

            result = finalize_ai_candidate(
                output_dir,
                problem_type="noise",
                candidate_id="candidate-warn",
                approval_id="approval-warn",
                decision="promote",
                confirm_warn=True,
                output_role="noise_cleaned_track",
                note="accepted warning after review",
            )

        selected = result["manifest"]["final_selected_artifacts"][0]
        self.assertEqual("candidate-warn", selected["candidate_id"])
        self.assertEqual("warn", selected["comparison_status"])
        self.assertTrue(selected["operator_decision"]["confirm_warn"])

    def test_finalize_blocks_unpromotable_candidate_statuses(self) -> None:
        for status in ("fail", "unavailable"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp_name:
                output_dir = Path(temp_name)
                _write_seed_manifest(
                    output_dir,
                    [
                        _candidate_seed(
                            output_dir,
                            problem_type="missing_ball",
                            candidate_id=f"candidate-{status}",
                            approval_id=f"approval-{status}",
                            status=status,
                        )
                    ],
                )

                with self.assertRaisesRegex(ValueError, status):
                    finalize_ai_candidate(
                        output_dir,
                        problem_type="missing_ball",
                        candidate_id=f"candidate-{status}",
                        approval_id=f"approval-{status}",
                        decision="promote",
                        output_role="missing_ball_track",
                    )

                loaded = json.loads((output_dir / FINAL_ARTIFACT_MANIFEST_NAME).read_text(encoding="utf-8"))
                self.assertEqual([], loaded["final_selected_artifacts"])

    def test_finalize_blocks_unknown_missing_approval_and_missing_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_seed_manifest(
                output_dir,
                [
                    _candidate_seed(
                        output_dir,
                        problem_type="missing_ball",
                        candidate_id="candidate-known",
                        approval_id="approval-known",
                        status="pass",
                    )
                ],
            )

            with self.assertRaisesRegex(ValueError, "unknown candidate"):
                finalize_ai_candidate(
                    output_dir,
                    problem_type="missing_ball",
                    candidate_id="candidate-ghost",
                    approval_id="approval-known",
                    decision="promote",
                    output_role="missing_ball_track",
                )
            with self.assertRaisesRegex(ValueError, "approval"):
                finalize_ai_candidate(
                    output_dir,
                    problem_type="missing_ball",
                    candidate_id="candidate-known",
                    approval_id="approval-missing",
                    decision="promote",
                    output_role="missing_ball_track",
                )

        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_seed_manifest(
                output_dir,
                [
                    {
                        "candidate": {
                            "id": "candidate-no-comparison",
                            "candidate_id": "candidate-no-comparison",
                            "problem_type": "missing_ball",
                            "path": "ai_candidates/missing_ball/candidate-no-comparison",
                        },
                        "approval": {"approval_id": "approval-no-comparison", "candidate_id": "candidate-no-comparison"},
                        "comparison": None,
                    }
                ],
            )

            with self.assertRaisesRegex(ValueError, "comparison"):
                finalize_ai_candidate(
                    output_dir,
                    problem_type="missing_ball",
                    candidate_id="candidate-no-comparison",
                    approval_id="approval-no-comparison",
                    decision="promote",
                    output_role="missing_ball_track",
                )

    def test_finalize_promotion_requires_comparison_bound_to_requested_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_seed_manifest(
                output_dir,
                [
                    _candidate_seed(
                        output_dir,
                        problem_type="missing_ball",
                        candidate_id="candidate-shared",
                        approval_id="approval-a",
                        status="pass",
                    )
                ],
            )
            manifest_path = output_dir / FINAL_ARTIFACT_MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["consumed_approvals"].append(
                {
                    "approval_id": "approval-b",
                    "candidate_id": "candidate-shared",
                    "problem_type": "missing_ball",
                    "status": "approved",
                }
            )
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "missing comparison"):
                finalize_ai_candidate(
                    output_dir,
                    problem_type="missing_ball",
                    candidate_id="candidate-shared",
                    approval_id="approval-b",
                    decision="promote",
                    output_role="missing_ball_track",
                )

            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual([], loaded["final_selected_artifacts"])

    def test_finalize_accepts_comparison_with_nested_approval_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_seed_manifest(
                output_dir,
                [
                    _candidate_seed(
                        output_dir,
                        problem_type="missing_ball",
                        candidate_id="candidate-nested",
                        approval_id="approval-nested",
                        status="pass",
                    )
                ],
            )
            comparison_path = (
                output_dir
                / "ai_candidates"
                / "missing_ball"
                / "candidate-nested"
                / "missing_ball_recovery_comparison.json"
            )
            comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
            comparison.pop("approval_id", None)
            comparison.pop("consumed_approval_ids", None)
            comparison["approval"] = {"approval_id": "approval-nested", "candidate_id": "candidate-nested"}
            comparison_path.write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            manifest_path = output_dir / FINAL_ARTIFACT_MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["comparison_reports"][0].pop("approval_id", None)
            manifest["comparison_reports"][0].pop("consumed_approval_ids", None)
            manifest["comparison_reports"][0]["approval"] = {"approval_id": "approval-nested", "candidate_id": "candidate-nested"}
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            result = finalize_ai_candidate(
                output_dir,
                problem_type="missing_ball",
                candidate_id="candidate-nested",
                approval_id="approval-nested",
                decision="promote",
                output_role="missing_ball_track",
            )

        selected = result["manifest"]["final_selected_artifacts"][0]
        self.assertEqual("candidate-nested", selected["candidate_id"])
        comparison_ref = result["manifest"]["comparison_reports"][0]
        self.assertEqual("approval-nested", comparison_ref["approval_id"])
        self.assertEqual(["approval-nested"], comparison_ref["consumed_approval_ids"])

    def test_finalize_blocks_review_only_and_unsupported_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_seed_manifest(
                output_dir,
                [
                    _candidate_seed(
                        output_dir,
                        problem_type="missing_ball",
                        candidate_id="candidate-review-only",
                        approval_id="approval-review-only",
                        status="pass",
                    ),
                    _candidate_seed(
                        output_dir,
                        problem_type="noise",
                        candidate_id="candidate-unsupported",
                        approval_id="approval-unsupported",
                        status="pass",
                    ),
                ],
            )
            manifest_path = output_dir / FINAL_ARTIFACT_MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["candidate_outputs"][0]["candidate_intent"] = "review_only"
            manifest["unsupported_candidates"] = [
                {
                    "candidate_id": "candidate-unsupported",
                    "approval_id": "approval-unsupported",
                    "problem_type": "noise",
                    "reason": "unsupported_type",
                }
            ]
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "review_only"):
                finalize_ai_candidate(
                    output_dir,
                    problem_type="missing_ball",
                    candidate_id="candidate-review-only",
                    approval_id="approval-review-only",
                    decision="promote",
                    output_role="missing_ball_track",
                )
            with self.assertRaisesRegex(ValueError, "unsupported_type"):
                finalize_ai_candidate(
                    output_dir,
                    problem_type="noise",
                    candidate_id="candidate-unsupported",
                    approval_id="approval-unsupported",
                    decision="promote",
                    output_role="noise_cleaned_track",
                )

            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual([], loaded["final_selected_artifacts"])

    def test_finalize_repeating_same_promotion_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_seed_manifest(
                output_dir,
                [
                    _candidate_seed(
                        output_dir,
                        problem_type="missing_ball",
                        candidate_id="candidate-pass",
                        approval_id="approval-pass",
                        status="pass",
                    )
                ],
            )

            finalize_ai_candidate(
                output_dir,
                problem_type="missing_ball",
                candidate_id="candidate-pass",
                approval_id="approval-pass",
                decision="promote",
                output_role="missing_ball_track",
            )
            second = finalize_ai_candidate(
                output_dir,
                problem_type="missing_ball",
                candidate_id="candidate-pass",
                approval_id="approval-pass",
                decision="promote",
                output_role="missing_ball_track",
            )

        self.assertEqual(1, len(second["manifest"]["final_selected_artifacts"]))
        self.assertEqual(1, len(second["manifest"]["operator_decisions"]))

    def test_finalize_rejects_candidate_path_outside_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name) / "run"
            outside_dir = Path(temp_name) / "outside"
            output_dir.mkdir()
            outside_dir.mkdir()
            _write_seed_manifest(
                output_dir,
                [
                    _candidate_seed(
                        output_dir,
                        problem_type="missing_ball",
                        candidate_id="candidate-outside",
                        approval_id="approval-outside",
                        status="pass",
                        candidate_path=str(outside_dir / "candidate-outside"),
                    )
                ],
            )

            with self.assertRaisesRegex(ValueError, "outside output_dir"):
                finalize_ai_candidate(
                    output_dir,
                    problem_type="missing_ball",
                    candidate_id="candidate-outside",
                    approval_id="approval-outside",
                    decision="promote",
                    output_role="missing_ball_track",
                )

        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_seed_manifest(
                output_dir,
                [
                    _candidate_seed(
                        output_dir,
                        problem_type="missing_ball",
                        candidate_id="candidate-traversal",
                        approval_id="approval-traversal",
                        status="pass",
                    )
                ],
            )
            manifest_path = output_dir / FINAL_ARTIFACT_MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["candidate_outputs"][0]["candidate_artifacts"] = ["../escape.csv"]
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "path traversal"):
                finalize_ai_candidate(
                    output_dir,
                    problem_type="missing_ball",
                    candidate_id="candidate-traversal",
                    approval_id="approval-traversal",
                    decision="promote",
                    output_role="missing_ball_track",
                )

    def test_finalize_uses_current_comparison_file_over_stale_manifest_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_seed_manifest(
                output_dir,
                [
                    _candidate_seed(
                        output_dir,
                        problem_type="missing_ball",
                        candidate_id="candidate-stale",
                        approval_id="approval-stale",
                        status="pass",
                    )
                ],
            )
            comparison_path = (
                output_dir
                / "ai_candidates"
                / "missing_ball"
                / "candidate-stale"
                / "missing_ball_recovery_comparison.json"
            )
            current_comparison = _comparison(
                "candidate-stale",
                "fail",
                "ai_candidates/missing_ball/candidate-stale/missing_ball_recovery_comparison.json",
            )
            current_comparison["approval_id"] = "approval-stale"
            current_comparison["consumed_approval_ids"] = ["approval-stale"]
            comparison_path.write_text(json.dumps(current_comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "fail"):
                finalize_ai_candidate(
                    output_dir,
                    problem_type="missing_ball",
                    candidate_id="candidate-stale",
                    approval_id="approval-stale",
                    decision="promote",
                    output_role="missing_ball_track",
                )

            loaded = json.loads((output_dir / FINAL_ARTIFACT_MANIFEST_NAME).read_text(encoding="utf-8"))

        self.assertEqual([], loaded["final_selected_artifacts"])

    def test_finalize_refuses_pathless_manifest_only_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_seed_manifest(
                output_dir,
                [
                    _candidate_seed(
                        output_dir,
                        problem_type="missing_ball",
                        candidate_id="candidate-pathless",
                        approval_id="approval-pathless",
                        status="pass",
                    )
                ],
            )
            manifest_path = output_dir / FINAL_ARTIFACT_MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["comparison_reports"][0].pop("path", None)
            manifest["comparison_reports"][0].pop("comparison_report", None)
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unavailable"):
                finalize_ai_candidate(
                    output_dir,
                    problem_type="missing_ball",
                    candidate_id="candidate-pathless",
                    approval_id="approval-pathless",
                    decision="promote",
                    output_role="missing_ball_track",
                )

            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual([], loaded["final_selected_artifacts"])

    def test_finalize_refuses_to_overwrite_corrupt_final_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            manifest_path = output_dir / FINAL_ARTIFACT_MANIFEST_NAME
            manifest_path.write_text("{", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "corrupt"):
                finalize_ai_candidate(
                    output_dir,
                    problem_type="missing_ball",
                    candidate_id="candidate-corrupt",
                    approval_id="approval-corrupt",
                    decision="promote",
                    output_role="missing_ball_track",
                )

            manifest_text = manifest_path.read_text(encoding="utf-8")

        self.assertEqual("{", manifest_text)

    def test_finalize_refuses_invalid_final_manifest_list_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            manifest_path = output_dir / FINAL_ARTIFACT_MANIFEST_NAME
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "baseline_output": {"path": "ball_track.csv"},
                        "candidate_outputs": {"candidate_id": "candidate-invalid"},
                        "final_selected_artifacts": [],
                        "consumed_approvals": [],
                        "comparison_reports": [],
                        "quality_gate_status": {"status": "pass"},
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "candidate_outputs"):
                finalize_ai_candidate(
                    output_dir,
                    problem_type="missing_ball",
                    candidate_id="candidate-invalid",
                    approval_id="approval-invalid",
                    decision="promote",
                    output_role="missing_ball_track",
                )

            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertIsInstance(loaded["candidate_outputs"], dict)

    def test_finalize_replacement_semantics_singletons_and_highlight_clips(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_seed_manifest(
                output_dir,
                [
                    _candidate_seed(output_dir, problem_type="follow_cam", candidate_id="follow-1", approval_id="approval-follow-1", status="pass"),
                    _candidate_seed(output_dir, problem_type="follow_cam", candidate_id="follow-2", approval_id="approval-follow-2", status="pass"),
                    _candidate_seed(output_dir, problem_type="highlight", candidate_id="clip-1", approval_id="approval-clip-1", status="pass"),
                    _candidate_seed(output_dir, problem_type="highlight", candidate_id="clip-2", approval_id="approval-clip-2", status="pass"),
                ],
            )

            finalize_ai_candidate(output_dir, problem_type="follow_cam", candidate_id="follow-1", approval_id="approval-follow-1", decision="promote", output_role="follow_cam_video")
            finalize_ai_candidate(output_dir, problem_type="follow_cam", candidate_id="follow-2", approval_id="approval-follow-2", decision="promote", output_role="follow_cam_video")
            result = finalize_ai_candidate(output_dir, problem_type="highlight", candidate_id="clip-1", approval_id="approval-clip-1", decision="promote", output_role="highlight_clip")
            result = finalize_ai_candidate(output_dir, problem_type="highlight", candidate_id="clip-2", approval_id="approval-clip-2", decision="promote", output_role="highlight_clip")

        selected = result["manifest"]["final_selected_artifacts"]
        self.assertEqual(["follow-2"], [item["candidate_id"] for item in selected if item["output_role"] == "follow_cam_video"])
        self.assertEqual(["clip-1", "clip-2"], [item["candidate_id"] for item in selected if item["output_role"] == "highlight_clip"])

    def test_finalize_rejection_records_reason_note_and_lifecycle_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_seed_manifest(
                output_dir,
                [
                    _candidate_seed(
                        output_dir,
                        problem_type="noise",
                        candidate_id="candidate-reject",
                        approval_id="approval-reject",
                        status="pass",
                    )
                ],
            )

            result = finalize_ai_candidate(
                output_dir,
                problem_type="noise",
                candidate_id="candidate-reject",
                approval_id="approval-reject",
                decision="reject",
                output_role="noise_cleaned_track",
                note="cleanup removed too much continuity",
            )

        self.assertEqual([], result["manifest"]["final_selected_artifacts"])
        rejected = result["manifest"]["rejected_candidates"][0]
        self.assertEqual("candidate-reject", rejected["candidate_id"])
        self.assertEqual("operator_rejected", rejected["reason"])
        self.assertEqual("cleanup removed too much continuity", rejected["operator_decision"]["note"])
        self.assertEqual("rejected", result["lifecycle"]["summary"]["promotion_status"])


def _comparison(candidate_id: str, status: str, path: str) -> dict[str, object]:
    problem_type = "noise" if "noise" in path else "follow_cam" if "follow_cam" in path else "missing_ball"
    return {
        "path": path,
        "problem_type": problem_type,
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


def _candidate_seed(
    output_dir: Path,
    *,
    problem_type: str,
    candidate_id: str,
    approval_id: str,
    status: str,
    candidate_path: str | None = None,
) -> dict[str, object]:
    candidate_dir = Path(candidate_path) if candidate_path is not None else output_dir / "ai_candidates" / problem_type / candidate_id
    if not candidate_dir.is_absolute():
        candidate_dir = output_dir / candidate_dir
    candidate_dir.mkdir(parents=True, exist_ok=True)
    if problem_type == "noise":
        artifact_name = "ball_track.cleaned.csv"
    elif problem_type == "follow_cam":
        artifact_name = "follow_cam.mp4"
    elif problem_type == "highlight":
        artifact_name = "highlight.mp4"
    else:
        artifact_name = "ball_track.csv"
    (candidate_dir / artifact_name).write_text("candidate-artifact\n", encoding="utf-8")
    comparison_name = {
        "missing_ball": "missing_ball_recovery_comparison.json",
        "noise": "noise_candidate_comparison.json",
        "follow_cam": "follow_cam_candidate_comparison.json",
        "highlight": "highlight_candidate_comparison.json",
    }[problem_type]
    comparison_path = candidate_dir / comparison_name
    relative_candidate_dir = _relative_or_string(candidate_dir, output_dir)
    relative_comparison = _relative_or_string(comparison_path, output_dir)
    comparison = _comparison(candidate_id, status, relative_comparison)
    comparison["problem_type"] = problem_type
    comparison["approval_id"] = approval_id
    comparison["consumed_approval_ids"] = [approval_id]
    comparison["candidate_dir"] = relative_candidate_dir
    comparison["comparison_report"] = relative_comparison
    comparison["candidate_artifacts"] = [f"{relative_candidate_dir}/{artifact_name}"]
    comparison_path.write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "candidate": {
            "id": candidate_id,
            "candidate_id": candidate_id,
            "problem_type": problem_type,
            "path": relative_candidate_dir if candidate_path is None else candidate_path,
            "candidate_artifacts": [f"{relative_candidate_dir}/{artifact_name}"],
        },
        "approval": {
            "approval_id": approval_id,
            "candidate_id": candidate_id,
            "problem_type": problem_type,
            "status": "approved",
        },
        "comparison": comparison,
    }


def _write_seed_manifest(output_dir: Path, seeds: list[dict[str, object]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    comparisons = [seed["comparison"] for seed in seeds if isinstance(seed.get("comparison"), dict)]
    write_final_artifact_manifest(
        output_dir,
        baseline_output={"path": "ball_track.csv", "status": "baseline"},
        candidate_outputs=[seed["candidate"] for seed in seeds if isinstance(seed.get("candidate"), dict)],
        final_artifacts=[],
        consumed_approvals=[seed["approval"] for seed in seeds if isinstance(seed.get("approval"), dict)],
        comparison_reports=comparisons,
        quality_gate_status={"status": "pass"},
    )


def _relative_or_string(path: Path, output_dir: Path) -> str:
    try:
        return path.resolve().relative_to(output_dir.resolve()).as_posix()
    except ValueError:
        return str(path)


if __name__ == "__main__":
    unittest.main()
