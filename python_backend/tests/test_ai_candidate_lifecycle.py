from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from football_tracking.ai_candidate_lifecycle import build_ai_candidate_lifecycle


class AiCandidateLifecycleTests(unittest.TestCase):
    def test_empty_output_returns_stable_empty_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)

            first = build_ai_candidate_lifecycle(output_dir)
            second = build_ai_candidate_lifecycle(output_dir)

        self.assertEqual(first, second)
        self.assertEqual("1.0", first["schema_version"])
        self.assertIsNone(first["generated_at"])
        self.assertEqual([], first["candidates"])
        self.assertEqual(
            {
                "stage": "review_only",
                "comparison_status": "none",
                "promotion_status": "not_promoted",
                "resolution_status": "none",
                "blocking_reasons": [],
                "candidate_count": 0,
                "approved_action_count": 0,
                "comparison_report_count": 0,
            },
            first["summary"],
        )

    def test_review_report_without_approval_is_proposed_never_applied(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_improvement_report.json",
                {
                    "schema_version": "1.0",
                    "generated_at": "2026-06-23T00:00:00+00:00",
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_missing",
                            "candidate_id": "candidate-missing",
                            "problem_type": "missing_ball",
                            "recommended_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 10, "end_frame": 20},
                        }
                    ],
                },
            )

            lifecycle = build_ai_candidate_lifecycle(output_dir)

        self.assertEqual("proposed", lifecycle["summary"]["stage"])
        self.assertEqual("not_promoted", lifecycle["summary"]["promotion_status"])
        self.assertEqual("none", lifecycle["summary"]["comparison_status"])
        self.assertEqual([], lifecycle["summary"]["blocking_reasons"])
        self.assertEqual("proposed", lifecycle["candidates"][0]["stage"])
        self.assertNotIn(lifecycle["candidates"][0]["stage"], {"approved", "executed", "compared", "finalized"})

    def test_approval_without_candidate_artifact_is_pending_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_improvement_approved_actions.json",
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval-missing",
                            "improvement_id": "imp_missing",
                            "candidate_id": "candidate-missing",
                            "approved_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 10, "end_frame": 20},
                        }
                    ],
                },
            )

            lifecycle = build_ai_candidate_lifecycle(output_dir)

        self.assertEqual("pending_execution", lifecycle["summary"]["stage"])
        self.assertEqual(["pending_api_execution"], lifecycle["summary"]["blocking_reasons"])
        self.assertEqual(1, lifecycle["summary"]["approved_action_count"])
        self.assertEqual("candidate-missing", lifecycle["candidates"][0]["candidate_id"])
        self.assertEqual(["approval-missing"], lifecycle["candidates"][0]["approval_ids"])

    def test_executable_highlight_approval_without_candidate_id_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_improvement_approved_actions.json",
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval-highlight",
                            "improvement_id": "imp_highlight",
                            "approved_action": "render_suggested_highlight",
                            "suggested_window": {"start_frame": 90, "end_frame": 120},
                        }
                    ],
                },
            )

            lifecycle = build_ai_candidate_lifecycle(output_dir)

        self.assertEqual("pending_execution", lifecycle["summary"]["stage"])
        self.assertEqual(
            ["missing_candidate_id", "pending_api_execution"],
            lifecycle["summary"]["blocking_reasons"],
        )
        self.assertEqual(
            ["missing_candidate_id", "pending_api_execution"],
            lifecycle["candidates"][0]["blocking_reasons"],
        )

    def test_quality_gate_does_not_upgrade_approval_without_candidate_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_improvement_approved_actions.json",
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval-missing",
                            "improvement_id": "imp_missing",
                            "candidate_id": "candidate-missing",
                            "approved_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 10, "end_frame": 20},
                        }
                    ],
                },
            )
            _write_json(
                output_dir / "ai_improvement_quality_gate.json",
                {"schema_version": "1.0", "summary": {"status": "pass"}},
            )

            lifecycle = build_ai_candidate_lifecycle(output_dir)

        self.assertEqual("pending_execution", lifecycle["summary"]["stage"])
        self.assertIn("pending_api_execution", lifecycle["summary"]["blocking_reasons"])
        self.assertEqual("pending_execution", lifecycle["candidates"][0]["stage"])
        self.assertIn("pending_api_execution", lifecycle["candidates"][0]["blocking_reasons"])

    def test_manual_review_approval_without_candidate_id_dedupes_and_does_not_pending_execute(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_improvement_report.json",
                {
                    "schema_version": "1.0",
                    "improvements": [
                        {
                            "id": "imp_manual",
                            "recommended_action": "manual_review",
                            "diagnosis": "Operator should inspect this one.",
                        }
                    ],
                },
            )
            _write_json(
                output_dir / "ai_improvement_approved_actions.json",
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval-manual",
                            "improvement_id": "imp_manual",
                            "approved_action": "manual_review",
                        }
                    ],
                },
            )

            lifecycle = build_ai_candidate_lifecycle(output_dir)

        self.assertEqual(1, lifecycle["summary"]["candidate_count"])
        self.assertEqual("approved", lifecycle["summary"]["stage"])
        self.assertEqual([], lifecycle["summary"]["blocking_reasons"])
        self.assertEqual("approved", lifecycle["candidates"][0]["stage"])
        self.assertEqual(["imp_manual"], lifecycle["candidates"][0]["improvement_ids"])
        self.assertEqual(["approval-manual"], lifecycle["candidates"][0]["approval_ids"])
        self.assertNotIn("pending_api_execution", lifecycle["candidates"][0]["blocking_reasons"])
        self.assertNotIn("missing_candidate_id", lifecycle["candidates"][0]["blocking_reasons"])

    def test_registry_plus_comparison_statuses_map_correctly(self) -> None:
        for status in ("pass", "warn", "fail", "unavailable"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp_name:
                output_dir = Path(temp_name)
                _write_json(
                    output_dir / "ai_candidate_registry.json",
                    {
                        "schema_version": "1.0",
                        "candidates": [
                            {
                                "candidate_id": f"candidate-{status}",
                                "approval_id": f"approval-{status}",
                                "problem_type": "missing_ball",
                                "candidate_dir": f"ai_candidates/{status}",
                                "candidate_artifacts": [f"ai_candidates/{status}/ball_track.csv"],
                                "comparison_report": f"ai_candidates/{status}/missing_ball_recovery_comparison.json",
                                "comparison_status": status,
                                "promotion_status": "pending_confirmation" if status == "warn" else "not_promoted",
                                "consumed_approval_ids": [f"approval-{status}"],
                            }
                        ],
                    },
                )
                _write_json(
                    output_dir / "ai_candidates" / status / "missing_ball_recovery_comparison.json",
                    _comparison_payload(f"candidate-{status}", status),
                )

                lifecycle = build_ai_candidate_lifecycle(output_dir)

            self.assertEqual("compared", lifecycle["summary"]["stage"])
            self.assertEqual(status, lifecycle["summary"]["comparison_status"])
            self.assertEqual(status, lifecycle["candidates"][0]["comparison_status"])
            if status == "warn":
                self.assertEqual("pending_confirmation", lifecycle["summary"]["promotion_status"])
                self.assertEqual(["pending_human_confirmation"], lifecycle["summary"]["blocking_reasons"])

    def test_registry_explicit_comparison_report_does_not_fallback_to_stale_same_candidate_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_candidate_registry.json",
                {
                    "schema_version": "1.0",
                    "candidates": [
                        {
                            "candidate_id": "candidate-stale",
                            "approval_id": "approval-stale",
                            "problem_type": "missing_ball",
                            "candidate_dir": "ai_candidates/current",
                            "candidate_artifacts": ["ai_candidates/current/ball_track.csv"],
                            "comparison_report": "ai_candidates/current/missing_ball_recovery_comparison.json",
                            "comparison_status": "pass",
                            "promotion_status": "not_promoted",
                            "consumed_approval_ids": ["approval-stale"],
                        }
                    ],
                },
            )
            _write_json(
                output_dir / "ai_candidates" / "stale" / "missing_ball_recovery_comparison.json",
                _comparison_payload("candidate-stale", "pass"),
            )

            lifecycle = build_ai_candidate_lifecycle(output_dir)

        self.assertEqual("executed", lifecycle["summary"]["stage"])
        self.assertEqual("unavailable", lifecycle["summary"]["comparison_status"])
        self.assertEqual(["missing_comparison"], lifecycle["summary"]["blocking_reasons"])
        self.assertEqual("executed", lifecycle["candidates"][0]["stage"])
        self.assertEqual("unavailable", lifecycle["candidates"][0]["comparison_status"])
        self.assertEqual(["missing_comparison"], lifecycle["candidates"][0]["blocking_reasons"])

    def test_missing_ball_resolution_with_full_window_evidence_maps_to_resolved_not_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "missing_ball_resolution.json",
                {
                    "schema_version": "1.0",
                    "summary": {
                        "status": "resolved_not_visible",
                        "resolution_count": 1,
                        "consumed_approval_ids": ["approval-noop"],
                    },
                    "resolutions": [
                        {
                            "candidate_id": "resolved-window",
                            "approval_id": "approval-noop",
                            "problem_type": "missing_ball",
                            "status": "resolved_not_visible",
                            "start_frame": 100,
                            "end_frame": 160,
                            "likely_ball_region": {"description": "not_visible"},
                            "evidence": [{"source_packet_id": "packet-100", "reason": "full window not visible"}],
                        }
                    ],
                },
            )

            lifecycle = build_ai_candidate_lifecycle(output_dir)

        self.assertEqual("finalized", lifecycle["summary"]["stage"])
        self.assertEqual("resolved_not_visible", lifecycle["summary"]["resolution_status"])
        self.assertEqual("resolved_not_visible", lifecycle["candidates"][0]["resolution_status"])
        self.assertEqual(["approval-noop"], lifecycle["candidates"][0]["approval_ids"])

    def test_final_manifest_promoted_and_rejected_states_override_review_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            promoted_dir = Path(temp_name) / "promoted"
            rejected_dir = Path(temp_name) / "rejected"
            promoted_dir.mkdir()
            rejected_dir.mkdir()
            _write_json(
                promoted_dir / "ai_improvement_report.json",
                {
                    "improvements": [
                        {
                            "id": "imp_promoted",
                            "candidate_id": "candidate-promoted",
                            "recommended_action": "manual_review",
                        }
                    ]
                },
            )
            _write_json(
                promoted_dir / "final_ai_improvement_artifact_manifest.json",
                {
                    "schema_version": "1.0",
                    "final_selected_artifacts": [{"candidate_id": "candidate-promoted", "path": "final/output.mp4"}],
                    "rejected_candidates": [],
                    "summary": {"final_artifact_count": 1, "rejected_candidate_count": 0},
                },
            )
            _write_json(
                rejected_dir / "ai_improvement_report.json",
                {
                    "improvements": [
                        {
                            "id": "imp_rejected",
                            "candidate_id": "candidate-rejected",
                            "recommended_action": "targeted_rerun",
                        }
                    ]
                },
            )
            _write_json(
                rejected_dir / "final_ai_improvement_artifact_manifest.json",
                {
                    "schema_version": "1.0",
                    "final_selected_artifacts": [],
                    "rejected_candidates": [
                        {"candidate_id": "candidate-rejected", "reason": "comparison_failed"}
                    ],
                    "summary": {"final_artifact_count": 0, "rejected_candidate_count": 1},
                },
            )

            promoted = build_ai_candidate_lifecycle(promoted_dir)
            rejected = build_ai_candidate_lifecycle(rejected_dir)

        self.assertEqual("promoted", promoted["summary"]["promotion_status"])
        self.assertEqual("promoted", promoted["candidates"][0]["promotion_status"])
        self.assertEqual("rejected", rejected["summary"]["promotion_status"])
        self.assertEqual("rejected", rejected["candidates"][0]["promotion_status"])

    def test_final_manifest_selection_overrides_registry_pending_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_candidate_registry.json",
                {
                    "schema_version": "1.0",
                    "candidates": [
                        {
                            "candidate_id": "candidate-warn-promoted",
                            "approval_id": "approval-warn-promoted",
                            "problem_type": "missing_ball",
                            "candidate_dir": "ai_candidates/warn-promoted",
                            "candidate_artifacts": ["ai_candidates/warn-promoted/ball_track.csv"],
                            "comparison_report": "ai_candidates/warn-promoted/missing_ball_recovery_comparison.json",
                            "comparison_status": "warn",
                            "promotion_status": "pending_confirmation",
                            "consumed_approval_ids": ["approval-warn-promoted"],
                        }
                    ],
                },
            )
            _write_json(
                output_dir / "ai_candidates" / "warn-promoted" / "missing_ball_recovery_comparison.json",
                _comparison_payload("candidate-warn-promoted", "warn"),
            )
            _write_json(
                output_dir / "final_ai_improvement_artifact_manifest.json",
                {
                    "schema_version": "1.0",
                    "candidate_outputs": [
                        {"id": "candidate-warn-promoted", "path": "candidate/output.mp4", "type": "video"}
                    ],
                    "final_selected_artifacts": [
                        {"candidate_id": "candidate-warn-promoted", "path": "final/output.mp4", "type": "video"}
                    ],
                    "comparison_reports": [
                        {
                            **_comparison_payload("candidate-warn-promoted", "warn"),
                            "path": "ai_candidates/warn-promoted/missing_ball_recovery_comparison.json",
                        }
                    ],
                    "rejected_candidates": [],
                },
            )

            lifecycle = build_ai_candidate_lifecycle(output_dir)

        self.assertEqual("promoted", lifecycle["summary"]["promotion_status"])
        self.assertNotIn("pending_human_confirmation", lifecycle["summary"]["blocking_reasons"])
        self.assertEqual("promoted", lifecycle["candidates"][0]["promotion_status"])
        self.assertNotIn("pending_human_confirmation", lifecycle["candidates"][0]["blocking_reasons"])

    def test_final_manifest_selection_without_comparison_marks_missing_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "final_ai_improvement_artifact_manifest.json",
                {
                    "schema_version": "1.0",
                    "candidate_outputs": [
                        {"id": "candidate-no-comparison", "path": "candidate/output.mp4", "type": "video"}
                    ],
                    "final_selected_artifacts": [
                        {"candidate_id": "candidate-no-comparison", "path": "final/output.mp4", "type": "video"}
                    ],
                    "comparison_reports": [],
                    "rejected_candidates": [],
                },
            )

            lifecycle = build_ai_candidate_lifecycle(output_dir)

        self.assertEqual("finalized", lifecycle["summary"]["stage"])
        self.assertEqual("unavailable", lifecycle["summary"]["comparison_status"])
        self.assertIn("missing_comparison", lifecycle["summary"]["blocking_reasons"])
        self.assertEqual("unavailable", lifecycle["candidates"][0]["comparison_status"])
        self.assertIn("missing_comparison", lifecycle["candidates"][0]["blocking_reasons"])

    def test_final_manifest_comparison_count_dedupes_relative_and_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            comparison_path = output_dir / "missing_ball_recovery_comparison.json"
            _write_json(comparison_path, _comparison_payload("candidate-deduped", "pass"))
            _write_json(
                output_dir / "final_ai_improvement_artifact_manifest.json",
                {
                    "schema_version": "1.0",
                    "candidate_outputs": [
                        {"id": "candidate-deduped", "path": "candidate/output.mp4", "type": "video"}
                    ],
                    "final_selected_artifacts": [
                        {"candidate_id": "candidate-deduped", "path": "final/output.mp4", "type": "video"}
                    ],
                    "comparison_reports": [
                        {
                            **_comparison_payload("candidate-deduped", "pass"),
                            "path": "missing_ball_recovery_comparison.json",
                        }
                    ],
                },
            )

            lifecycle = build_ai_candidate_lifecycle(output_dir)

        self.assertEqual(1, lifecycle["summary"]["comparison_report_count"])

    def test_debug_comparison_notes_are_not_discovered_as_candidate_comparisons(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(output_dir / "debug_comparison_notes.json", {"summary": {"status": "pass"}})

            lifecycle = build_ai_candidate_lifecycle(output_dir)

        self.assertEqual(0, lifecycle["summary"]["comparison_report_count"])
        self.assertEqual([], lifecycle["candidates"])

    def test_missing_comparison_creates_missing_comparison_blocking_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_candidate_registry.json",
                {
                    "schema_version": "1.0",
                    "candidates": [
                        {
                            "candidate_id": "candidate-no-comparison",
                            "approval_id": "approval-no-comparison",
                            "problem_type": "missing_ball",
                            "candidate_dir": "ai_candidates/no-comparison",
                            "candidate_artifacts": ["ai_candidates/no-comparison/ball_track.csv"],
                            "comparison_report": "ai_candidates/no-comparison/missing_ball_recovery_comparison.json",
                            "comparison_status": "pass",
                            "promotion_status": "not_promoted",
                            "consumed_approval_ids": ["approval-no-comparison"],
                        }
                    ],
                },
            )

            lifecycle = build_ai_candidate_lifecycle(output_dir)

        self.assertEqual("executed", lifecycle["summary"]["stage"])
        self.assertEqual("unavailable", lifecycle["summary"]["comparison_status"])
        self.assertEqual(["missing_comparison"], lifecycle["summary"]["blocking_reasons"])
        self.assertEqual(["missing_comparison"], lifecycle["candidates"][0]["blocking_reasons"])

    def test_failed_quality_gate_creates_failed_quality_gate_blocking_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_candidate_registry.json",
                {
                    "schema_version": "1.0",
                    "candidates": [
                        {
                            "candidate_id": "candidate-gated",
                            "approval_id": "approval-gated",
                            "problem_type": "missing_ball",
                            "candidate_dir": "ai_candidates/gated",
                            "candidate_artifacts": ["ai_candidates/gated/ball_track.csv"],
                            "comparison_report": "ai_candidates/gated/missing_ball_recovery_comparison.json",
                            "comparison_status": "pass",
                            "promotion_status": "not_promoted",
                            "consumed_approval_ids": ["approval-gated"],
                        }
                    ],
                },
            )
            _write_json(
                output_dir / "ai_candidates" / "gated" / "missing_ball_recovery_comparison.json",
                _comparison_payload("candidate-gated", "pass"),
            )
            _write_json(
                output_dir / "ai_improvement_quality_gate.json",
                {"schema_version": "1.0", "summary": {"status": "fail"}},
            )

            lifecycle = build_ai_candidate_lifecycle(output_dir)

        self.assertEqual("gated", lifecycle["summary"]["stage"])
        self.assertEqual(["failed_quality_gate"], lifecycle["summary"]["blocking_reasons"])
        self.assertEqual(["failed_quality_gate"], lifecycle["candidates"][0]["blocking_reasons"])


def _comparison_payload(candidate_id: str, status: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "problem_type": "missing_ball",
        "candidate_id": candidate_id,
        "candidate": {"id": candidate_id, "path": f"ai_candidates/{candidate_id}/ball_track.csv"},
        "summary": {
            "status": status,
            "check_count": 1,
            "passed_check_count": 1 if status == "pass" else 0,
            "failed_check_count": 1 if status == "fail" else 0,
            "warning_count": 1 if status == "warn" else 0,
            "unavailable_count": 1 if status == "unavailable" else 0,
        },
        "checks": [{"name": "comparison", "status": status}],
    }


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
