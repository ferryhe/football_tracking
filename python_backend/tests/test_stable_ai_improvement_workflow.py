from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.run_stable_ai_improvement_workflow import main, run_workflow


class StableAiImprovementWorkflowTests(unittest.TestCase):
    def test_dry_run_workflow_records_stages_without_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            provider_calls: list[dict[str, object]] = []

            def fake_improvement(path: Path, **kwargs: object) -> dict[str, object]:
                provider_calls.append(kwargs)
                _write_ai_report(path)
                return {"summary": {"status": "ok"}, "model": kwargs.get("model"), "improvements": []}

            with patch(
                "scripts.run_stable_ai_improvement_workflow.write_ai_improvement_report",
                side_effect=fake_improvement,
            ):
                report = run_workflow(output_dir=output_dir, dry_run=True, model="gpt-stable")

        stage_names = [stage["name"] for stage in report["stages"]]
        self.assertEqual(
            [
                "metrics_artifacts_refresh",
                "before_review_hash_snapshot",
                "review_packets",
                "visual_review",
                "ai_improvement",
                "after_ai_improvement_hash_snapshot",
                "approved_child_rerun",
                "follow_cam_rerender_plan",
                "highlight_render",
                "quality_gate",
            ],
            stage_names,
        )
        self.assertTrue(report["dry_run"])
        self.assertEqual("gpt-stable", report["model"])
        self.assertEqual(True, provider_calls[0]["dry_run"])
        self.assertEqual("review_only", provider_calls[0]["candidate_intent"])
        ai_stage = _stage(report, "ai_improvement")
        self.assertTrue(ai_stage["provider_dry_run"])
        self.assertEqual("dry-run", ai_stage["provider_mode"])
        self.assertEqual("review_only", ai_stage["candidate_intent"])

    def test_workflow_candidate_intent_is_independent_from_quality_gate_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            provider_calls: list[dict[str, object]] = []

            def fake_improvement(path: Path, **kwargs: object) -> dict[str, object]:
                provider_calls.append(kwargs)
                _write_ai_report(path)
                return {
                    "summary": {"status": "ok"},
                    "candidate_intent": kwargs.get("candidate_intent"),
                    "model": kwargs.get("model"),
                    "improvements": [],
                }

            with patch(
                "scripts.run_stable_ai_improvement_workflow.write_ai_improvement_report",
                side_effect=fake_improvement,
            ):
                report = run_workflow(
                    output_dir=output_dir,
                    dry_run=False,
                    quality_gate_mode="artifact-only",
                    candidate_intent="prepare_approved_candidates",
                )

        self.assertEqual(True, provider_calls[0]["dry_run"])
        self.assertEqual("prepare_approved_candidates", provider_calls[0]["candidate_intent"])
        self.assertEqual("prepare_approved_candidates", report["inputs"]["candidate_intent"])
        self.assertEqual("prepare_approved_candidates", _stage(report, "ai_improvement")["candidate_intent"])

    def test_workflow_records_before_and_after_hash_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)

            run_workflow(output_dir=output_dir, dry_run=True)
            snapshots = json.loads((output_dir / "ai_improvement_hash_snapshots.json").read_text(encoding="utf-8"))

        self.assertEqual(["before_review", "after_ai_improvement"], [item["stage_name"] for item in snapshots["snapshots"]])

    def test_workflow_refuses_implicit_approved_actions_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            _write_json(
                output_dir / "ai_improvement_approved_actions.json",
                {"approved_actions": [_approval("approval_1", start=10, end=20)]},
            )

            report = run_workflow(output_dir=output_dir, dry_run=True)

        child_stage = _stage(report, "approved_child_rerun")
        highlight_stage = _stage(report, "highlight_render")
        self.assertEqual("skipped", child_stage["status"])
        self.assertEqual("skipped", highlight_stage["status"])
        self.assertFalse(report["inputs"]["approval_intent"]["has_explicit_approval_intent"])
        self.assertTrue(any("not passed explicitly" in warning for warning in report["warnings"]))

    def test_unknown_approval_ids_fail_in_non_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            approved_path = output_dir / "approved_actions.json"
            _write_json(
                approved_path,
                {"approved_actions": [_approval("approval_1", start=10, end=20)]},
            )

            with self.assertRaisesRegex(ValueError, "Unknown approval ids: missing_approval"):
                run_workflow(
                    output_dir=output_dir,
                    dry_run=False,
                    approved_actions_path=approved_path,
                    approval_ids=["missing_approval"],
                )

    def test_programmatic_approval_ids_are_stripped_before_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            approved_path = output_dir / "approved_actions.json"
            _write_json(
                approved_path,
                {"approved_actions": [_approval("approval_1", start=10, end=20)]},
            )

            report = run_workflow(
                output_dir=output_dir,
                dry_run=False,
                approved_actions_path=approved_path,
                approval_ids=[" approval_1 ", ""],
            )

        selection = report["inputs"]["approval_selection"]
        self.assertEqual(["approval_1"], selection["requested_ids"])
        self.assertEqual(["approval_1"], selection["consumed_ids"])

    def test_approved_action_id_without_path_fails_in_non_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)

            with self.assertRaisesRegex(ValueError, "--approved-action-id requires --approved-actions-path"):
                run_workflow(output_dir=output_dir, dry_run=False, approved_action_id="highlight_1")

    def test_approved_action_id_without_path_warns_in_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)

            report = run_workflow(output_dir=output_dir, dry_run=True, approved_action_id=" highlight_1 ")

        self.assertTrue(any("--approved-action-id requires --approved-actions-path" in warning for warning in report["warnings"]))
        self.assertEqual(["highlight_1"], report["inputs"]["approval_selection"]["single_action_ids"])

    def test_unknown_approval_ids_warn_in_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            approved_path = output_dir / "approved_actions.json"
            _write_json(
                approved_path,
                {"approved_actions": [_approval("approval_1", start=10, end=20)]},
            )

            report = run_workflow(
                output_dir=output_dir,
                dry_run=True,
                approved_actions_path=approved_path,
                approval_ids=["missing_approval"],
            )

        self.assertTrue(any("Unknown approval ids: missing_approval" in warning for warning in report["warnings"]))
        child_stage = _stage(report, "approved_child_rerun")
        self.assertEqual("skipped", child_stage["status"])

    def test_mode_dry_run_warns_for_unknown_ids_without_dry_run_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            approved_path = output_dir / "approved_actions.json"
            _write_json(
                approved_path,
                {"approved_actions": [_approval("approval_1", start=10, end=20)]},
            )

            report = run_workflow(
                output_dir=output_dir,
                dry_run=False,
                quality_gate_mode="dry-run",
                approved_actions_path=approved_path,
                approval_ids=["missing_approval"],
            )

        self.assertTrue(any("Unknown approval ids: missing_approval" in warning for warning in report["warnings"]))
        self.assertEqual("dry-run", report["quality_gate"]["mode"])

    def test_duplicate_approval_ids_fail_in_non_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            approved_path = output_dir / "approved_actions.json"
            _write_json(
                approved_path,
                {"approved_actions": [_approval("approval_1", start=10, end=20)]},
            )

            with self.assertRaisesRegex(ValueError, "Duplicate approval ids: approval_1"):
                run_workflow(
                    output_dir=output_dir,
                    dry_run=False,
                    approved_actions_path=approved_path,
                    approval_ids=["approval_1", "approval_1"],
                )

    def test_duplicate_approval_ids_inside_artifact_fail_in_non_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            approved_path = output_dir / "approved_actions.json"
            _write_json(
                approved_path,
                {
                    "approved_actions": [
                        _approval("approval_1", start=10, end=20),
                        _approval("approval_1", start=30, end=40),
                    ]
                },
            )

            with self.assertRaisesRegex(ValueError, "Duplicate approval ids in artifact: approval_1"):
                run_workflow(output_dir=output_dir, dry_run=False, approved_actions_path=approved_path)

    def test_approved_action_id_does_not_consume_entire_approval_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            approved_path = output_dir / "approved_actions.json"
            _write_json(
                approved_path,
                {
                    "approved_actions": [
                        _approval("rerun_1", start=10, end=20),
                        _highlight_approval("highlight_1", start=90, end=120),
                    ]
                },
            )

            report = run_workflow(
                output_dir=output_dir,
                dry_run=False,
                approved_actions_path=approved_path,
                approved_action_id="highlight_1",
            )

        self.assertEqual("skipped", _stage(report, "approved_child_rerun")["status"])
        self.assertEqual("planned", _stage(report, "highlight_render")["status"])
        selection = report["inputs"]["approval_selection"]
        self.assertEqual(["highlight_1"], selection["consumed_ids"])
        self.assertEqual(["rerun_1"], selection["skipped_ids"])
        self.assertEqual({"rerun_1": "not_requested"}, selection["skipped_reasons"])

    def test_approved_action_id_targeted_rerun_is_rejected_before_gate_credit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            _write_2079_gap(output_dir)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544)
            _write_json(
                output_dir / "ai_improvement_report.json",
                {
                    "summary": {"status": "needs_rerun"},
                    "model": "gpt-stable",
                    "improvements": [
                        {
                            "id": "imp_2079",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "recommended_action": "targeted_rerun",
                            "start_frame": 2049,
                            "end_frame": 2544,
                            "rerun_scope": {"start_frame": 2049, "end_frame": 2544},
                            "source_packet_id": "packet_2079",
                        }
                    ],
                },
            )
            approved_path = output_dir / "approved_actions.json"
            _write_json(approved_path, {"approved_actions": [_approval("rerun_1", start=2049, end=2544)]})

            def fake_improvement(path: Path, **_: object) -> dict[str, object]:
                return json.loads((path / "ai_improvement_report.json").read_text(encoding="utf-8"))

            with patch(
                "scripts.run_stable_ai_improvement_workflow.write_ai_improvement_report",
                side_effect=fake_improvement,
            ):
                with self.assertRaisesRegex(ValueError, "only supports follow-up actions"):
                    run_workflow(
                        output_dir=output_dir,
                        dry_run=False,
                        approved_actions_path=approved_path,
                        approved_action_id="rerun_1",
                    )

    def test_mixed_approval_ids_and_approved_action_id_do_not_grant_extra_gate_credit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            _write_2079_gap(output_dir)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544)
            approved_path = output_dir / "approved_actions.json"
            _write_json(
                approved_path,
                {
                    "approved_actions": [
                        _approval("cover_2079", start=2049, end=2544),
                        _highlight_approval("highlight_1", start=90, end=120),
                    ]
                },
            )

            def fake_improvement(path: Path, **_: object) -> dict[str, object]:
                _write_ai_report(path)
                return {"summary": {"status": "ok"}, "improvements": []}

            with patch(
                "scripts.run_stable_ai_improvement_workflow.write_ai_improvement_report",
                side_effect=fake_improvement,
            ):
                with self.assertRaisesRegex(ValueError, "only supports follow-up actions"):
                    run_workflow(
                        output_dir=output_dir,
                        dry_run=False,
                        approved_actions_path=approved_path,
                        approval_ids=["highlight_1"],
                        approved_action_id="cover_2079",
                    )

    def test_missing_approval_id_in_artifact_fails_in_non_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            approved_path = output_dir / "approved_actions.json"
            action = _approval("approval_1", start=10, end=20)
            action.pop("approval_id")
            _write_json(approved_path, {"approved_actions": [action]})

            with self.assertRaisesRegex(ValueError, "non-empty string approval_id"):
                run_workflow(output_dir=output_dir, dry_run=False, approved_actions_path=approved_path)

    def test_tracking_rerun_before_follow_cam_is_follow_cam_plan_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            approved_path = output_dir / "approved_actions.json"
            _write_json(
                approved_path,
                {"approved_actions": [_tracking_rerun_before_follow_cam_approval("camera_1", start=50, end=80)]},
            )

            report = run_workflow(
                output_dir=output_dir,
                dry_run=False,
                approved_actions_path=approved_path,
                approval_ids=["camera_1"],
            )

        follow_stage = _stage(report, "follow_cam_rerender_plan")
        self.assertEqual("planned", follow_stage["status"])
        self.assertEqual(1, follow_stage["approved_action_count"])

    def test_follow_cam_approved_action_id_does_not_plan_highlight_render(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            approved_path = output_dir / "approved_actions.json"
            _write_json(
                approved_path,
                {"approved_actions": [_tracking_rerun_before_follow_cam_approval("camera_1", start=10, end=20)]},
            )

            report = run_workflow(
                output_dir=output_dir,
                dry_run=False,
                approved_actions_path=approved_path,
                approved_action_id="camera_1",
            )

        self.assertEqual("skipped", _stage(report, "highlight_render")["status"])

    def test_mixed_follow_cam_action_id_and_highlight_approval_ids_do_not_plan_highlight(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            approved_path = output_dir / "approved_actions.json"
            _write_json(
                approved_path,
                {
                    "approved_actions": [
                        _tracking_rerun_before_follow_cam_approval("camera_1", start=10, end=20),
                        _highlight_approval("highlight_1", start=30, end=60),
                    ]
                },
            )

            report = run_workflow(
                output_dir=output_dir,
                dry_run=False,
                approved_actions_path=approved_path,
                approval_ids=["highlight_1"],
                approved_action_id="camera_1",
            )

        self.assertEqual("skipped", _stage(report, "highlight_render")["status"])

    def test_targeted_rerun_approved_action_id_fails_in_non_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            approved_path = output_dir / "approved_actions.json"
            _write_json(approved_path, {"approved_actions": [_approval("rerun_1", start=10, end=20)]})

            with self.assertRaisesRegex(ValueError, "only supports follow-up actions"):
                run_workflow(
                    output_dir=output_dir,
                    dry_run=False,
                    approved_actions_path=approved_path,
                    approved_action_id="rerun_1",
                )

    def test_malformed_approved_actions_fail_before_provider_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            approved_path = output_dir / "approved_actions.json"
            _write_json(approved_path, {"approved_actions": ["bad-entry"]})
            provider_calls: list[dict[str, object]] = []

            def fake_improvement(path: Path, **kwargs: object) -> dict[str, object]:
                provider_calls.append(kwargs)
                _write_ai_report(path)
                return {"summary": {"status": "ok"}, "improvements": []}

            with patch(
                "scripts.run_stable_ai_improvement_workflow.write_ai_improvement_report",
                side_effect=fake_improvement,
            ):
                with self.assertRaisesRegex(ValueError, "approved_actions entries must be objects"):
                    run_workflow(output_dir=output_dir, dry_run=False, approved_actions_path=approved_path)

        self.assertEqual([], provider_calls)

    def test_malformed_approved_actions_do_not_reach_quality_gate_in_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            _write_2079_gap(output_dir)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544)
            _write_json(
                output_dir / "ai_improvement_report.json",
                {
                    "summary": {"status": "needs_rerun"},
                    "model": "gpt-stable",
                    "improvements": [
                        {
                            "id": "imp_2079",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "recommended_action": "targeted_rerun",
                            "start_frame": 2049,
                            "end_frame": 2544,
                            "rerun_scope": {"start_frame": 2049, "end_frame": 2544},
                            "source_packet_id": "packet_2079",
                        }
                    ],
                },
            )
            approved_path = output_dir / "approved_actions.json"
            action = _approval("approval_2079", start=2049, end=2544)
            action.pop("approval_id")
            _write_json(approved_path, {"approved_actions": [action]})

            def fake_improvement(path: Path, **_: object) -> dict[str, object]:
                return json.loads((path / "ai_improvement_report.json").read_text(encoding="utf-8"))

            with patch(
                "scripts.run_stable_ai_improvement_workflow.write_ai_improvement_report",
                side_effect=fake_improvement,
            ):
                report = run_workflow(
                    output_dir=output_dir,
                    dry_run=True,
                    approved_actions_path=approved_path,
                )

        self.assertTrue(any("non-empty string approval_id" in warning for warning in report["warnings"]))
        self.assertEqual("fail", report["quality_gate"]["summary"]["status"])

    def test_missing_approved_actions_path_fails_before_provider_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            provider_calls: list[dict[str, object]] = []

            def fake_improvement(path: Path, **kwargs: object) -> dict[str, object]:
                provider_calls.append(kwargs)
                _write_ai_report(path)
                return {"summary": {"status": "ok"}, "improvements": []}

            with patch(
                "scripts.run_stable_ai_improvement_workflow.write_ai_improvement_report",
                side_effect=fake_improvement,
            ):
                with self.assertRaisesRegex(ValueError, "approved actions could not be loaded"):
                    run_workflow(
                        output_dir=output_dir,
                        dry_run=False,
                        approved_actions_path=output_dir / "missing_approved_actions.json",
                    )

        self.assertEqual([], provider_calls)

    def test_workflow_quality_gate_catches_2079_gap_without_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            _write_2079_gap(output_dir)

            def fake_improvement(path: Path, **_: object) -> dict[str, object]:
                _write_ai_report(path)
                return {"summary": {"status": "ok"}, "improvements": []}

            with patch(
                "scripts.run_stable_ai_improvement_workflow.write_ai_improvement_report",
                side_effect=fake_improvement,
            ):
                report = run_workflow(output_dir=output_dir, dry_run=True)

        self.assertEqual("fail", report["quality_gate"]["summary"]["status"])
        self.assertEqual("fail", _stage(report, "quality_gate")["summary"]["status"])

    def test_temporal_mode_passes_chunk_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)

            report = run_workflow(output_dir=output_dir, dry_run=True, parallel_mode="temporal")

        strategy = report["strategy"]
        self.assertEqual("temporal", strategy["parallel_mode"])
        self.assertEqual("temporal_chunks", strategy["full_video_speed_strategy"])
        self.assertEqual(
            {
                "enabled": True,
                "chunk_frames": 1200,
                "overlap_frames": 80,
                "decode_preroll_frames": 120,
                "merge_strategy": "overlap_quality",
            },
            strategy["temporal_chunk_settings"],
        )
        self.assertEqual("do_not_run_full_video_sahi", strategy["sahi_roi_policy"]["full_video_sahi"])

    def test_sahi_roi_selected_only_for_bounded_approved_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            approved_path = output_dir / "approved_actions.json"
            _write_json(
                approved_path,
                {"approved_actions": [_approval("approval_1", start=10, end=20)]},
            )

            report = run_workflow(
                output_dir=output_dir,
                dry_run=False,
                approved_actions_path=approved_path,
                approval_ids=["approval_1"],
            )

        child_stage = _stage(report, "approved_child_rerun")
        self.assertEqual("planned", child_stage["status"])
        self.assertEqual("sahi_roi", child_stage["rerun_mode"])
        self.assertEqual([{"approval_id": "approval_1", "start_frame": 10, "end_frame": 20}], child_stage["bounded_windows"])
        self.assertFalse(child_stage["runs_full_video_sahi"])

    def test_workflow_report_records_consumed_and_skipped_approval_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            approved_path = output_dir / "approved_actions.json"
            _write_json(
                approved_path,
                {
                    "approved_actions": [
                        _approval("approval_1", start=10, end=20),
                        _approval("approval_2", start=30, end=40),
                    ]
                },
            )

            report = run_workflow(
                output_dir=output_dir,
                dry_run=False,
                approved_actions_path=approved_path,
                approval_ids=["approval_1"],
            )

        selection = report["inputs"]["approval_selection"]
        self.assertEqual("path", selection["approval_source"])
        self.assertEqual(str(approved_path), selection["source_path"])
        self.assertEqual(["approval_1"], selection["requested_ids"])
        self.assertEqual([], selection["single_action_ids"])
        self.assertEqual(["approval_1"], selection["consumed_ids"])
        self.assertEqual(["approval_2"], selection["skipped_ids"])
        self.assertEqual({"approval_2": "not_requested"}, selection["skipped_reasons"])

    def test_missing_source_video_keeps_artifact_only_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            missing_video = output_dir / "missing.mp4"

            report = run_workflow(output_dir=output_dir, dry_run=False, input_video=missing_video)

        self.assertEqual("artifact-only", report["quality_gate"]["mode"])
        self.assertEqual("unavailable", _stage(report, "review_packets")["video_status"])
        self.assertEqual("artifact-only", _stage(report, "review_packets")["status"])
        self.assertTrue(any("input video" in warning for warning in report["warnings"]))

    def test_workflow_report_includes_quality_gate_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)

            report = run_workflow(output_dir=output_dir, dry_run=True)
            written = json.loads((output_dir / "stable_ai_improvement_workflow_report.json").read_text(encoding="utf-8"))

        self.assertIn("quality_gate", report)
        self.assertIn("ai_improvement_quality_gate.json", report["produced_artifacts"])
        self.assertIn("ai_improvement_hash_snapshots.json", report["produced_artifacts"])
        self.assertEqual(report["quality_gate"]["summary"], written["quality_gate"]["summary"])

    def test_workflow_fails_quality_gate_when_real_mode_missing_required_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["--output-dir", temp_name, "--mode", "real"])

            report = json.loads((Path(temp_name) / "stable_ai_improvement_workflow_report.json").read_text(encoding="utf-8"))

        self.assertEqual(1, exit_code)
        self.assertEqual("fail", report["quality_gate"]["summary"]["status"])

    def test_invalid_approval_ids_json_uses_argparse_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            stderr = io.StringIO()

            with self.assertRaises(SystemExit) as caught:
                with contextlib.redirect_stderr(stderr):
                    main(["--output-dir", temp_name, "--approval-ids", "[not-json"])

        self.assertEqual(2, caught.exception.code)
        self.assertIn("--approval-ids", stderr.getvalue())


def _stage(report: dict[str, object], name: str) -> dict[str, object]:
    stages = report["stages"]
    if not isinstance(stages, list):
        raise AssertionError("report stages must be a list")
    for stage in stages:
        if isinstance(stage, dict) and stage.get("name") == name:
            return stage
    raise AssertionError(f"missing stage: {name}")


def _approval(approval_id: str, *, start: int, end: int) -> dict[str, object]:
    return {
        "approval_id": approval_id,
        "improvement_id": "imp_1",
        "approved_action": "targeted_rerun",
        "rerun_scope": {"start_frame": start, "end_frame": end},
        "source_packet_id": "packet_1",
        "local_search_roi": {
            "coordinate_space": "image",
            "frame": start,
            "x": 120,
            "y": 40,
            "width": 80,
            "height": 50,
            "confidence": 0.72,
        },
    }


def _highlight_approval(approval_id: str, *, start: int, end: int) -> dict[str, object]:
    return {
        "approval_id": approval_id,
        "improvement_id": "highlight_imp_1",
        "approved_action": "render_suggested_highlight",
        "suggested_window": {"start_frame": start, "end_frame": end},
        "source_packet_id": "packet_highlight",
    }


def _tracking_rerun_before_follow_cam_approval(approval_id: str, *, start: int, end: int) -> dict[str, object]:
    return {
        "approval_id": approval_id,
        "improvement_id": "camera_imp_1",
        "approved_action": "tracking_rerun_before_follow_cam",
        "rerun_scope": {"start_frame": start, "end_frame": end},
        "source_packet_id": "packet_camera",
    }


def _write_tracks(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    text = "Frame,X,Y,Status\n1,10,20,Detected\n"
    (output_dir / "ball_track.csv").write_text(text, encoding="utf-8")
    (output_dir / "ball_track.cleaned.csv").write_text(text, encoding="utf-8")


def _write_ai_report(output_dir: Path) -> None:
    _write_json(
        output_dir / "ai_improvement_report.json",
        {
            "summary": {"status": "ok"},
            "model": "gpt-stable",
            "improvements": [],
            "highlight_adjustments": [],
        },
    )


def _write_2079_gap(output_dir: Path) -> None:
    _write_json(
        output_dir / "ball_audit.json",
        {
            "summary": {"lost_gap_count": 1},
            "review_events": [
                {
                    "id": "lost_2079",
                    "type": "lost_gap",
                    "start_frame": 2049,
                    "end_frame": 2544,
                    "frame_count": 496,
                    "evidence": {"field_zone": "lower_right_corner"},
                }
            ],
        },
    )


def _write_packet(output_dir: Path, *, packet_id: str, start: int, end: int) -> None:
    _write_json(
        output_dir / "review_packets.json",
        {
            "packets": [
                {
                    "packet_id": packet_id,
                    "window": {"start_frame": start, "end_frame": end},
                    "decision": {"label": "needs_ai_review"},
                }
            ]
        },
    )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
