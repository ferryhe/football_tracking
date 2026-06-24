from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from football_tracking.ai_candidate_lifecycle import build_ai_candidate_lifecycle
from scripts.run_stable_ai_improvement_workflow import (
    _approved_child_rerun_stage,
    _final_artifact_manifest_stage,
    _load_selected_approved_actions,
    main,
    run_workflow,
)


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
                "selected_approval_dispatcher",
                "approved_child_rerun",
                "follow_cam_rerender_plan",
                "highlight_render",
                "missing_ball_noop_resolution",
                "quality_gate",
                "final_artifact_manifest",
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

    def test_real_mode_runs_review_packets_then_visual_review_before_ai_improvement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            input_video = output_dir / "source.mp4"
            input_video.write_bytes(b"fake-video")
            _write_tracks(output_dir)
            _write_json(output_dir / "ball_audit.json", {"review_events": []})
            call_order: list[str] = []

            def fake_review_packets(path: Path, **kwargs: object) -> dict[str, object]:
                call_order.append("review_packets")
                _write_packet(path, packet_id="packet_001", start=10, end=20)
                return {"summary": {"packet_count": 1}}

            def fake_visual_review(path: Path, **kwargs: object) -> dict[str, object]:
                self.assertTrue((path / "review_packets.json").exists())
                call_order.append("visual_review")
                payload = {
                    "summary": {"status": "ok", "reviewed_count": 1, "error_count": 0},
                    "model": kwargs.get("model"),
                    "model_selection": {
                        "model": kwargs.get("model"),
                        "source": "explicit",
                        "provider_dry_run": False,
                        "provider_mode": "real",
                    },
                    "candidate_intent": "visual_localization",
                    "reviews": [],
                    "errors": [],
                }
                _write_json(path / "ai_visual_review.json", payload)
                return payload

            def fake_improvement(path: Path, **kwargs: object) -> dict[str, object]:
                self.assertTrue((path / "ai_visual_review.json").exists())
                call_order.append("ai_improvement")
                payload = {
                    "summary": {"status": "ok"},
                    "model": kwargs.get("model"),
                    "model_selection": {
                        "model": kwargs.get("model"),
                        "source": "explicit",
                        "provider_dry_run": False,
                        "provider_mode": "real",
                    },
                    "candidate_intent": kwargs.get("candidate_intent"),
                    "improvements": [],
                    "highlight_adjustments": [],
                }
                _write_json(path / "ai_improvement_report.json", payload)
                return payload

            with (
                patch("scripts.run_stable_ai_improvement_workflow.write_review_packet_report", side_effect=fake_review_packets),
                patch(
                    "scripts.run_stable_ai_improvement_workflow.write_ai_visual_review_report",
                    side_effect=fake_visual_review,
                    create=True,
                ),
                patch("scripts.run_stable_ai_improvement_workflow.write_ai_improvement_report", side_effect=fake_improvement),
            ):
                report = run_workflow(
                    output_dir=output_dir,
                    input_video=input_video,
                    dry_run=False,
                    model="gpt-strong",
                    quality_gate_mode="real",
                )

        self.assertEqual(["review_packets", "visual_review", "ai_improvement"], call_order)
        self.assertEqual("succeeded", _stage(report, "visual_review")["status"])
        self.assertEqual("gpt-strong", _stage(report, "visual_review")["model"])

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

    def test_approved_actions_path_without_ids_does_not_select_or_execute_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            approved_path = output_dir / "approved_actions.json"
            _write_json(approved_path, {"approved_actions": [_approval("approval_1", start=10, end=20)]})

            report = run_workflow(output_dir=output_dir, dry_run=False, approved_actions_path=approved_path)

        selection = report["inputs"]["approval_selection"]
        self.assertEqual([], selection["consumed_ids"])
        self.assertEqual(["approval_1"], selection["skipped_ids"])
        self.assertEqual({"approval_1": "not_requested"}, selection["skipped_reasons"])
        self.assertEqual("skipped", _stage(report, "approved_child_rerun")["status"])

    def test_external_legacy_targeted_rerun_approval_is_normalized_at_workflow_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            approved_path = output_dir / "approved_actions.json"
            approval = _approval("approval_1", start=10, end=20)
            approval["approved_action"] = "targeted_rerun"
            approval["candidate_id"] = "candidate_001"
            approval["source_packet_id"] = "packet_001"
            approval["evidence"] = [{"source_packet_id": "packet_001"}]
            _write_json(approved_path, {"approved_actions": [approval]})

            payload = _load_selected_approved_actions(
                approved_actions_path=approved_path,
                approval_ids=["approval_1"],
                approved_action_id=None,
                fail_on_selection_error=True,
                warnings=[],
            )

        self.assertEqual(["approval_1"], payload["approval_selection"]["consumed_ids"])
        self.assertEqual("rerun_ball_window", payload["approved_actions"][0]["approved_action"])
        self.assertEqual("targeted_rerun", payload["approved_actions"][0]["legacy_approved_action"])

    def test_existing_missing_ball_resolution_is_preserved_without_current_noop_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            _write_2079_gap(output_dir)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544, label="ball_not_visible")
            _write_json(
                output_dir / "missing_ball_resolution.json",
                {
                    "schema_version": "1.0",
                    "summary": {"status": "resolved_not_visible", "resolution_count": 1},
                    "resolutions": [
                        {
                            "candidate_id": "stale",
                            "approval_id": "stale_approval",
                            "problem_type": "missing_ball",
                            "status": "resolved_not_visible",
                            "start_frame": 2049,
                            "end_frame": 2544,
                            "source_packet_id": "packet_2079",
                            "likely_ball_region": {"description": "not_visible"},
                            "evidence": [
                                {
                                    "source_packet_id": "packet_2079",
                                    "start_frame": 2049,
                                    "end_frame": 2544,
                                    "reason": "packet marks ball_not_visible",
                                }
                            ],
                        }
                    ],
                },
            )

            def fake_improvement(path: Path, **_: object) -> dict[str, object]:
                _write_ai_report(path)
                return {"summary": {"status": "ok"}, "improvements": []}

            with patch(
                "scripts.run_stable_ai_improvement_workflow.write_ai_improvement_report",
                side_effect=fake_improvement,
            ):
                report = run_workflow(output_dir=output_dir, dry_run=False)
            resolution_exists = (output_dir / "missing_ball_resolution.json").exists()

        self.assertEqual("skipped", _stage(report, "missing_ball_noop_resolution")["status"])
        self.assertEqual(1, _stage(report, "missing_ball_noop_resolution")["preserved_resolution_count"])
        self.assertTrue(resolution_exists)

    def test_noise_candidate_not_final_until_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_noise_tracks(output_dir)
            _write_packet(output_dir, packet_id="packet_noise", start=0, end=79)
            approved_path = output_dir / "approved_actions.json"
            _write_json(
                approved_path,
                {
                    "approved_actions": [
                        _noise_approval("noise_1", start=0, end=79),
                        _tracking_rerun_before_follow_cam_approval("camera_1", start=30, end=40),
                    ]
                },
            )

            report = run_workflow(
                output_dir=output_dir,
                dry_run=False,
                approved_actions_path=approved_path,
                approval_ids=["noise_1", "camera_1"],
            )
            candidate_cleaned_exists = (
                output_dir / "ai_candidates" / "noise" / "noise-candidate-1" / "ball_track.cleaned.csv"
            ).exists()
            registry = json.loads((output_dir / "ai_candidate_registry.json").read_text(encoding="utf-8"))
            manifest = json.loads(
                (output_dir / "final_ai_improvement_artifact_manifest.json").read_text(encoding="utf-8")
            )
            lifecycle = build_ai_candidate_lifecycle(output_dir)

        dispatcher = _stage(report, "selected_approval_dispatcher")
        self.assertEqual("completed", dispatcher["status"])
        self.assertEqual([], dispatcher["missing_ball_execution_path"]["approval_ids"])
        self.assertEqual("skipped", dispatcher["missing_ball_execution_path"]["status"])
        self.assertEqual("succeeded", dispatcher["noise_candidate_execution_path"]["status"])
        self.assertEqual(["noise_1"], dispatcher["noise_candidate_execution_path"]["approval_ids"])
        self.assertEqual(["noise-candidate-1"], dispatcher["noise_candidate_execution_path"]["candidate_ids"])
        self.assertEqual("blocked", dispatcher["follow_cam_candidate_execution_path"]["status"])
        self.assertEqual(
            "linked_tracking_candidate_evidence_required",
            dispatcher["follow_cam_candidate_execution_path"]["reason"],
        )
        self.assertEqual([], dispatcher["unsupported_actions"])
        self.assertTrue(candidate_cleaned_exists)
        self.assertEqual(1, registry["summary"]["counts_by_problem_type"]["noise"])
        self.assertEqual(1, manifest["summary"]["candidate_output_count"])
        self.assertEqual("noise-candidate-1", manifest["candidate_outputs"][0]["candidate_id"])
        self.assertEqual([], manifest["final_selected_artifacts"])
        self.assertEqual(
            ["noise-candidate-1", "follow-cam-1"],
            [item["candidate_id"] for item in manifest["pending_candidates"]],
        )
        self.assertEqual("blocked", manifest["pending_candidates"][1]["status"])
        self.assertEqual("pass", manifest["comparison_reports"][0]["status"])
        self.assertNotEqual("invalid_checks", manifest["comparison_reports"][0]["artifact_status"])
        self.assertEqual(report["quality_gate"]["summary"]["status"], manifest["quality_gate_status"]["status"])
        self.assertEqual("finalized", lifecycle["summary"]["stage"])
        self.assertEqual("unavailable", lifecycle["summary"]["comparison_status"])
        lifecycle_by_id = {item["candidate_id"]: item for item in lifecycle["candidates"]}
        self.assertEqual("noise", lifecycle_by_id["noise-candidate-1"]["problem_type"])
        self.assertEqual("pass", lifecycle_by_id["noise-candidate-1"]["comparison_status"])
        self.assertEqual("not_promoted", lifecycle_by_id["noise-candidate-1"]["promotion_status"])
        self.assertEqual(["noise_1"], lifecycle_by_id["noise-candidate-1"]["approval_ids"])
        self.assertEqual("follow_cam", lifecycle_by_id["follow-cam-1"]["problem_type"])
        self.assertEqual(["camera_1"], lifecycle_by_id["follow-cam-1"]["approval_ids"])
        self.assertEqual("skipped", _stage(report, "approved_child_rerun")["status"])
        self.assertEqual("blocked", _stage(report, "follow_cam_rerender_plan")["status"])
        self.assertEqual("skipped", _stage(report, "highlight_render")["status"])

    def test_noise_approval_file_presence_alone_does_not_execute_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_noise_tracks(output_dir)
            approved_path = output_dir / "approved_actions.json"
            _write_json(
                approved_path,
                {"approved_actions": [_noise_approval("noise_1", start=0, end=79)]},
            )

            report = run_workflow(output_dir=output_dir, dry_run=False, approved_actions_path=approved_path)
            candidate_dir_exists = (output_dir / "ai_candidates" / "noise" / "noise-candidate-1").exists()

        dispatcher = _stage(report, "selected_approval_dispatcher")
        self.assertEqual("skipped", dispatcher["noise_candidate_execution_path"]["status"])
        self.assertFalse(candidate_dir_exists)

    def test_missing_ball_candidate_not_final_until_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            output_dir = root / "outputs" / "baseline"
            output_dir.mkdir(parents=True)
            config_path = _write_recovery_config(root, output_dir)
            _write_lost_tracks(output_dir, start=2049, end=2544)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544)
            _write_json(
                output_dir / "run_manifest.json",
                {"config_path": str(config_path), "input_video": str(root / "data" / "input.mp4")},
            )
            approved_path = output_dir / "approved_actions.json"
            approval = {**_approval("approval_2079", start=2049, end=2544), "candidate_id": "candidate_2079"}
            approval["source_packet_id"] = "packet_2079"
            _write_json(approved_path, {"approved_actions": [approval]})

            def fake_high_recall(config, **_: object) -> dict[str, object]:
                rows = ["Frame,X,Y,Confidence,Status"]
                rows.extend(f"{frame},5700,1390,0.9000,Detected" for frame in range(2049, 2545))
                (config.output_dir / "ball_track.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
                (config.output_dir / "ball_track.cleaned.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
                return {
                    "windows": [{"approval_id": "approval_2079", "start_frame": 2049, "end_frame": 2544}],
                    "execution": {"status": "succeeded"},
                }

            with patch("scripts.run_stable_ai_improvement_workflow.run_high_recall_windows", side_effect=fake_high_recall):
                report = run_workflow(
                    output_dir=output_dir,
                    dry_run=False,
                    approved_actions_path=approved_path,
                    approval_ids=["approval_2079"],
                )
            candidate_dir = output_dir / "ai_candidates" / "missing_ball" / "candidate_2079"
            dispatcher = _stage(report, "selected_approval_dispatcher")
            child_stage = _stage(report, "approved_child_rerun")
            manifest = json.loads((output_dir / "final_ai_improvement_artifact_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("succeeded", dispatcher["missing_ball_execution_path"]["status"])
            self.assertEqual("executed", dispatcher["missing_ball_execution_path"]["execution_status"])
            self.assertEqual(["candidate_2079"], dispatcher["missing_ball_execution_path"]["candidate_ids"])
            self.assertTrue((candidate_dir / "missing_ball_recovery_comparison.json").exists())
            self.assertTrue((candidate_dir / "candidate_manifest.json").exists())
            self.assertEqual("succeeded", child_stage["status"])
            self.assertEqual("executed", child_stage["execution_status"])
            self.assertEqual(1, manifest["summary"]["candidate_output_count"])
            self.assertEqual("candidate_2079", manifest["candidate_outputs"][0]["candidate_id"])
            self.assertEqual([], manifest["final_selected_artifacts"])
            self.assertEqual(["candidate_2079"], [item["candidate_id"] for item in manifest["pending_candidates"]])

    def test_selected_missing_ball_recovery_executes_multiple_candidate_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            output_dir = root / "outputs" / "baseline"
            output_dir.mkdir(parents=True)
            config_path = _write_recovery_config(root, output_dir)
            _write_lost_tracks(output_dir, start=2049, end=2650)
            _write_json(
                output_dir / "review_packets.json",
                {
                    "packets": [
                        {
                            "packet_id": "packet_2079",
                            "window": {"start_frame": 2049, "end_frame": 2544},
                            "decision": {"label": "needs_ai_review"},
                        },
                        {
                            "packet_id": "packet_2600",
                            "window": {"start_frame": 2600, "end_frame": 2650},
                            "decision": {"label": "needs_ai_review"},
                        },
                        {
                            "packet_id": "packet_2550",
                            "window": {"start_frame": 2550, "end_frame": 2560},
                            "decision": {"label": "needs_ai_review"},
                        },
                    ]
                },
            )
            _write_json(
                output_dir / "run_manifest.json",
                {"config_path": str(config_path), "input_video": str(root / "data" / "input.mp4")},
            )
            approved_path = output_dir / "approved_actions.json"
            approval_2079 = {**_approval("approval_2079", start=2049, end=2544), "candidate_id": "candidate_2079"}
            approval_2079["source_packet_id"] = "packet_2079"
            approval_2550 = {**_approval("approval_2550", start=2550, end=2560), "candidate_id": "candidate_2079"}
            approval_2550["source_packet_id"] = "packet_2550"
            approval_2600 = {**_approval("approval_2600", start=2600, end=2650), "candidate_id": "candidate_2600"}
            approval_2600["source_packet_id"] = "packet_2600"
            _write_json(approved_path, {"approved_actions": [approval_2079, approval_2550, approval_2600]})

            def fake_high_recall(config, **_: object) -> dict[str, object]:
                selected = json.loads((config.output_dir / "ai_improvement_approved_actions.json").read_text(encoding="utf-8"))
                actions = selected["approved_actions"]
                rows = ["Frame,X,Y,Confidence,Status"]
                windows: list[dict[str, object]] = []
                for action in actions:
                    scope = action["rerun_scope"]
                    start = int(scope["start_frame"])
                    end = int(scope["end_frame"])
                    rows.extend(f"{frame},5700,1390,0.9000,Detected" for frame in range(start, end + 1))
                    windows.append({"approval_id": action["approval_id"], "start_frame": start, "end_frame": end})
                (config.output_dir / "ball_track.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
                (config.output_dir / "ball_track.cleaned.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
                return {"windows": windows, "execution": {"status": "succeeded"}}

            with patch("scripts.run_stable_ai_improvement_workflow.run_high_recall_windows", side_effect=fake_high_recall):
                report = run_workflow(
                    output_dir=output_dir,
                    dry_run=False,
                    approved_actions_path=approved_path,
                    approval_ids=["approval_2079", "approval_2550", "approval_2600"],
                )

            dispatcher = _stage(report, "selected_approval_dispatcher")
            missing_path = dispatcher["missing_ball_execution_path"]
            manifest = json.loads((output_dir / "final_ai_improvement_artifact_manifest.json").read_text(encoding="utf-8"))
            candidate_2079_exists = (output_dir / "ai_candidates" / "missing_ball" / "candidate_2079").exists()
            candidate_2600_exists = (output_dir / "ai_candidates" / "missing_ball" / "candidate_2600").exists()
            candidate_2079_selected = json.loads(
                (
                    output_dir
                    / "ai_candidates"
                    / "missing_ball"
                    / "candidate_2079"
                    / "ai_improvement_approved_actions.json"
                ).read_text(encoding="utf-8")
            )

        self.assertEqual("succeeded", missing_path["status"])
        self.assertEqual("executed", missing_path["execution_status"])
        self.assertEqual(["candidate_2079", "candidate_2600"], missing_path["candidate_ids"])
        self.assertEqual(
            ["candidate_2079", "candidate_2600"],
            [item["candidate_id"] for item in missing_path["candidate_outputs"]],
        )
        self.assertTrue(candidate_2079_exists)
        self.assertTrue(candidate_2600_exists)
        self.assertEqual(
            ["approval_2079", "approval_2550"],
            [item["approval_id"] for item in candidate_2079_selected["approved_actions"]],
        )
        self.assertEqual(2, manifest["summary"]["candidate_output_count"])
        self.assertEqual(
            ["candidate_2079", "candidate_2600"],
            [item["candidate_id"] for item in manifest["candidate_outputs"]],
        )

    def test_failed_missing_ball_candidate_group_records_error_while_other_group_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            output_dir = root / "outputs" / "baseline"
            output_dir.mkdir(parents=True)
            config_path = _write_recovery_config(root, output_dir)
            _write_lost_tracks(output_dir, start=2049, end=2650)
            _write_json(
                output_dir / "review_packets.json",
                {
                    "packets": [
                        {
                            "packet_id": "packet_2079",
                            "window": {"start_frame": 2049, "end_frame": 2544},
                            "decision": {"label": "needs_ai_review"},
                        },
                        {
                            "packet_id": "packet_2600",
                            "window": {"start_frame": 2600, "end_frame": 2650},
                            "decision": {"label": "needs_ai_review"},
                        },
                    ]
                },
            )
            _write_json(
                output_dir / "run_manifest.json",
                {"config_path": str(config_path), "input_video": str(root / "data" / "input.mp4")},
            )
            approved_path = output_dir / "approved_actions.json"
            approval_2079 = {**_approval("approval_2079", start=2049, end=2544), "candidate_id": "candidate_2079"}
            approval_2079["source_packet_id"] = "packet_2079"
            approval_2600 = {**_approval("approval_2600", start=2600, end=2650), "candidate_id": "candidate_2600"}
            approval_2600["source_packet_id"] = "packet_2600"
            _write_json(approved_path, {"approved_actions": [approval_2079, approval_2600]})

            def fake_high_recall(config, **_: object) -> dict[str, object]:
                if config.output_dir.name == "candidate_2600":
                    raise RuntimeError("synthetic recovery failure")
                selected = json.loads((config.output_dir / "ai_improvement_approved_actions.json").read_text(encoding="utf-8"))
                action = selected["approved_actions"][0]
                scope = action["rerun_scope"]
                start = int(scope["start_frame"])
                end = int(scope["end_frame"])
                rows = ["Frame,X,Y,Confidence,Status"]
                rows.extend(f"{frame},5700,1390,0.9000,Detected" for frame in range(start, end + 1))
                (config.output_dir / "ball_track.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
                (config.output_dir / "ball_track.cleaned.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
                return {
                    "windows": [{"approval_id": action["approval_id"], "start_frame": start, "end_frame": end}],
                    "execution": {"status": "succeeded"},
                }

            with patch("scripts.run_stable_ai_improvement_workflow.run_high_recall_windows", side_effect=fake_high_recall):
                report = run_workflow(
                    output_dir=output_dir,
                    dry_run=False,
                    approved_actions_path=approved_path,
                    approval_ids=["approval_2079", "approval_2600"],
                )
            written_report = json.loads(
                (output_dir / "stable_ai_improvement_workflow_report.json").read_text(encoding="utf-8")
            )
            manifest = json.loads((output_dir / "final_ai_improvement_artifact_manifest.json").read_text(encoding="utf-8"))
            lifecycle = build_ai_candidate_lifecycle(output_dir)
            failed_candidate_dir_exists = (output_dir / "ai_candidates" / "missing_ball" / "candidate_2600").exists()

        dispatcher = _stage(report, "selected_approval_dispatcher")
        missing_path = dispatcher["missing_ball_execution_path"]
        child_stage = _stage(report, "approved_child_rerun")
        written_missing_path = _stage(written_report, "selected_approval_dispatcher")["missing_ball_execution_path"]
        self.assertEqual("partial_failure", missing_path["status"])
        self.assertEqual("partial_failure", missing_path["execution_status"])
        self.assertEqual(["candidate_2079"], [item["candidate_id"] for item in missing_path["candidate_outputs"]])
        self.assertEqual(["candidate_2600"], missing_path["errors"][0]["candidate_ids"])
        self.assertEqual(["approval_2600"], missing_path["errors"][0]["approval_ids"])
        self.assertIn("synthetic recovery failure", missing_path["errors"][0]["error"])
        self.assertEqual("partial_failure", child_stage["status"])
        self.assertEqual("failed", report["quality_gate"]["summary"]["workflow_status"])
        self.assertEqual(missing_path["errors"], written_missing_path["errors"])
        self.assertEqual(1, manifest["summary"]["candidate_output_count"])
        self.assertEqual("candidate_2079", manifest["candidate_outputs"][0]["candidate_id"])
        self.assertEqual(1, manifest["summary"]["rejected_candidate_count"])
        self.assertEqual("candidate_2600", manifest["rejected_candidates"][0]["candidate_id"])
        self.assertEqual(["approval_2600"], manifest["rejected_candidates"][0]["approval_ids"])
        self.assertEqual("comparison_unavailable", manifest["rejected_candidates"][0]["reason"])
        self.assertEqual("unavailable", manifest["rejected_candidates"][0]["comparison_status"])
        self.assertIn("synthetic recovery failure", manifest["rejected_candidates"][0]["error"])
        failed_lifecycle = {
            item["candidate_id"]: item
            for item in lifecycle["candidates"]
            if isinstance(item.get("candidate_id"), str)
        }["candidate_2600"]
        self.assertEqual("rejected", failed_lifecycle["promotion_status"])
        self.assertIn("missing_evidence", failed_lifecycle["blocking_reasons"])
        self.assertFalse(failed_candidate_dir_exists)

    def test_approved_child_stage_prefers_dispatcher_failure_when_bounded_windows_empty(self) -> None:
        stage = _approved_child_rerun_stage(
            dry_run=False,
            approval_intent={
                "has_explicit_approval_intent": True,
                "approved_actions_path_explicit": True,
                "approval_ids_explicit": True,
                "approved_action_id": None,
            },
            bounded_windows=[],
            missing_ball_execution_path={
                "status": "failed",
                "execution_status": "failed",
                "approval_ids": ["approval_bad"],
                "candidate_ids": ["candidate_bad"],
                "errors": [{"candidate_id": "candidate_bad", "error": "invalid selected recovery scope"}],
                "strategy": "bounded_missing_ball_recovery_candidate",
            },
        )

        self.assertEqual("failed", stage["status"])
        self.assertEqual("failed", stage["execution_status"])
        self.assertEqual(["approval_bad"], stage["approval_ids"])
        self.assertEqual([{"candidate_id": "candidate_bad", "error": "invalid selected recovery scope"}], stage["errors"])

    def test_missing_ball_recovery_resolves_run_manifest_paths_relative_to_output_dir_from_other_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            output_dir = root / "outputs" / "baseline"
            output_dir.mkdir(parents=True)
            config_path = _write_recovery_config(output_dir, output_dir)
            _write_lost_tracks(output_dir, start=2049, end=2544)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544)
            _write_json(
                output_dir / "run_manifest.json",
                {"config_path": config_path.name, "input_video": "data/input.mp4"},
            )
            approved_path = output_dir / "approved_actions.json"
            approval = {**_approval("approval_2079", start=2049, end=2544), "candidate_id": "candidate_2079"}
            approval["source_packet_id"] = "packet_2079"
            _write_json(approved_path, {"approved_actions": [approval]})
            other_cwd = root / "different_cwd"
            other_cwd.mkdir()

            def fake_high_recall(config, **_: object) -> dict[str, object]:
                self.assertEqual((output_dir / "data" / "input.mp4").resolve(), Path(config.input_video).resolve())
                rows = ["Frame,X,Y,Confidence,Status"]
                rows.extend(f"{frame},5700,1390,0.9000,Detected" for frame in range(2049, 2545))
                (config.output_dir / "ball_track.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
                (config.output_dir / "ball_track.cleaned.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
                return {
                    "windows": [{"approval_id": "approval_2079", "start_frame": 2049, "end_frame": 2544}],
                    "execution": {"status": "succeeded"},
                }

            with (
                contextlib.chdir(other_cwd),
                patch("scripts.run_stable_ai_improvement_workflow.run_high_recall_windows", side_effect=fake_high_recall),
            ):
                report = run_workflow(
                    output_dir=output_dir,
                    dry_run=False,
                    approved_actions_path=approved_path,
                    approval_ids=["approval_2079"],
                )

        self.assertEqual("succeeded", _stage(report, "selected_approval_dispatcher")["missing_ball_execution_path"]["status"])

    def test_selected_noise_approval_missing_candidate_id_fails_without_candidate_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_noise_tracks(output_dir)
            _write_packet(output_dir, packet_id="packet_noise", start=0, end=79)
            approved_path = output_dir / "approved_actions.json"
            approval = _noise_approval("noise_1", start=0, end=79)
            approval.pop("candidate_id")
            _write_json(approved_path, {"approved_actions": [approval]})

            report = run_workflow(
                output_dir=output_dir,
                dry_run=False,
                approved_actions_path=approved_path,
                approval_ids=["noise_1"],
            )
            candidate_parent_exists = (output_dir / "ai_candidates" / "noise").exists()
            quality_gate = json.loads((output_dir / "ai_improvement_quality_gate.json").read_text(encoding="utf-8"))

        dispatcher = _stage(report, "selected_approval_dispatcher")
        self.assertEqual("failed", dispatcher["status"])
        self.assertEqual("failed", dispatcher["noise_candidate_execution_path"]["status"])
        self.assertIsNone(dispatcher["noise_candidate_execution_path"]["errors"][0]["candidate_id"])
        self.assertIn("candidate_id", dispatcher["noise_candidate_execution_path"]["errors"][0]["error"])
        self.assertFalse(candidate_parent_exists)
        self.assertEqual("failed", report["quality_gate"]["summary"]["workflow_status"])
        self.assertEqual("failed", quality_gate["summary"]["workflow_status"])

    def test_selected_noise_execution_failure_records_manifest_and_lifecycle_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_noise_tracks(output_dir)
            approved_path = output_dir / "approved_actions.json"
            _write_json(approved_path, {"approved_actions": [_noise_approval("noise_1", start=0, end=79)]})

            report = run_workflow(
                output_dir=output_dir,
                dry_run=False,
                approved_actions_path=approved_path,
                approval_ids=["noise_1"],
            )
            manifest = json.loads(
                (output_dir / "final_ai_improvement_artifact_manifest.json").read_text(encoding="utf-8")
            )
            lifecycle = build_ai_candidate_lifecycle(output_dir)

        dispatcher = _stage(report, "selected_approval_dispatcher")
        self.assertEqual("failed", dispatcher["noise_candidate_execution_path"]["status"])
        self.assertEqual("noise-candidate-1", dispatcher["noise_candidate_execution_path"]["errors"][0]["candidate_id"])
        self.assertEqual(1, manifest["summary"]["rejected_candidate_count"])
        self.assertEqual("noise-candidate-1", manifest["rejected_candidates"][0]["candidate_id"])
        self.assertEqual(["noise_1"], manifest["rejected_candidates"][0]["approval_ids"])
        self.assertEqual("noise", manifest["rejected_candidates"][0]["problem_type"])
        self.assertIn("traceable packet or visual evidence", manifest["rejected_candidates"][0]["error"])
        self.assertEqual("finalized", lifecycle["summary"]["stage"])
        self.assertEqual("rejected", lifecycle["summary"]["promotion_status"])
        self.assertEqual(["noise_1"], lifecycle["candidates"][0]["approval_ids"])

    def test_selected_noise_full_video_sahi_failure_records_manifest_and_lifecycle_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_noise_tracks(output_dir)
            _write_packet(output_dir, packet_id="packet_noise", start=0, end=79)
            approved_path = output_dir / "approved_actions.json"
            approval = _noise_approval("noise_1", start=0, end=79)
            approval["strategy_provenance"] = {
                "strategy": "bounded_full_video_sahi",
                "full_video_sahi": True,
                "start_frame": 0,
                "end_frame": 79,
            }
            _write_json(approved_path, {"approved_actions": [approval]})

            report = run_workflow(
                output_dir=output_dir,
                dry_run=False,
                approved_actions_path=approved_path,
                approval_ids=["noise_1"],
            )
            manifest = json.loads(
                (output_dir / "final_ai_improvement_artifact_manifest.json").read_text(encoding="utf-8")
            )
            lifecycle = build_ai_candidate_lifecycle(output_dir)
            candidate_dir_exists = (output_dir / "ai_candidates" / "noise" / "noise-candidate-1").exists()

        dispatcher = _stage(report, "selected_approval_dispatcher")
        self.assertEqual("failed", dispatcher["noise_candidate_execution_path"]["status"])
        self.assertFalse(candidate_dir_exists)
        self.assertEqual(1, manifest["summary"]["rejected_candidate_count"])
        self.assertEqual("noise-candidate-1", manifest["rejected_candidates"][0]["candidate_id"])
        self.assertEqual(["noise_1"], manifest["rejected_candidates"][0]["approval_ids"])
        self.assertIn("unbounded full-video spatial/SAHI", manifest["rejected_candidates"][0]["error"])
        self.assertEqual("finalized", lifecycle["summary"]["stage"])
        self.assertEqual("rejected", lifecycle["summary"]["promotion_status"])
        self.assertEqual(["noise_1"], lifecycle["candidates"][0]["approval_ids"])

    def test_main_returns_failure_when_selected_noise_execution_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_noise_tracks(output_dir)
            _write_packet(output_dir, packet_id="packet_noise", start=0, end=79)
            approved_path = output_dir / "approved_actions.json"
            approval = _noise_approval("noise_1", start=0, end=79)
            approval.pop("candidate_id")
            _write_json(approved_path, {"approved_actions": [approval]})

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--output-dir",
                        temp_name,
                        "--approved-actions-path",
                        str(approved_path),
                        "--approval-ids",
                        "noise_1",
                    ]
                )

        self.assertEqual(1, exit_code)
        printed = json.loads(stdout.getvalue())
        self.assertEqual("failed", printed["stable_ai_improvement_workflow"]["workflow_status"])

    def test_selected_not_visible_noop_writes_resolution_only_for_explicit_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            _write_2079_gap(output_dir)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544, label="ball_not_visible")
            approved_path = output_dir / "approved_actions.json"
            _write_json(
                approved_path,
                {
                    "approved_actions": [
                        _not_visible_approval("noop_2079", candidate_id="resolved_2079", start=2049, end=2544),
                        _not_visible_approval("noop_unselected", candidate_id="resolved_unselected", start=3000, end=3050),
                    ]
                },
            )

            report = run_workflow(
                output_dir=output_dir,
                dry_run=False,
                approved_actions_path=approved_path,
                approval_ids=["noop_2079"],
            )
            resolution = json.loads((output_dir / "missing_ball_resolution.json").read_text(encoding="utf-8"))

        self.assertEqual("resolved_not_visible", resolution["summary"]["status"])
        self.assertEqual(["noop_2079"], resolution["summary"]["consumed_approval_ids"])
        self.assertEqual(["noop_2079"], [item["approval_id"] for item in resolution["resolutions"]])
        self.assertEqual(["resolved_2079"], [item["candidate_id"] for item in resolution["resolutions"]])
        self.assertNotIn("noop_unselected", json.dumps(resolution))
        noop_stage = _stage(report, "missing_ball_noop_resolution")
        self.assertEqual("succeeded", noop_stage["status"])
        self.assertEqual("missing_ball_resolution.json", noop_stage["artifact"])

    def test_mark_ball_not_visible_approval_uses_resolution_not_recovery_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            _write_2079_gap(output_dir)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544, label="ball_not_visible")
            approved_path = output_dir / "approved_actions.json"
            approval = _not_visible_approval("noop_2079", candidate_id="resolved_2079", start=2049, end=2544)
            approval["approved_action"] = "mark_ball_not_visible"
            _write_json(approved_path, {"approved_actions": [approval]})

            report = run_workflow(
                output_dir=output_dir,
                dry_run=False,
                approved_actions_path=approved_path,
                approval_ids=["noop_2079"],
            )
            resolution = json.loads((output_dir / "missing_ball_resolution.json").read_text(encoding="utf-8"))

        self.assertEqual("resolved_not_visible", resolution["summary"]["status"])
        self.assertEqual("succeeded", _stage(report, "missing_ball_noop_resolution")["status"])
        self.assertEqual("skipped", _stage(report, "selected_approval_dispatcher")["missing_ball_execution_path"]["status"])
        comparison_check = report["quality_gate"]["checks"]["candidate_comparisons_ok"]
        self.assertNotEqual("selected_missing_ball_approval_missing_comparison", json.dumps(comparison_check))

    def test_real_visual_review_unavailable_is_not_reported_as_successful_localization(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            _write_packet(output_dir, packet_id="packet_001", start=10, end=20)

            def fake_visual_review(path: Path, **kwargs: object) -> dict[str, object]:
                payload = {
                    "summary": {"status": "unavailable", "reviewed_count": 0, "error_count": 1},
                    "model": None,
                    "model_selection": {"source": "strong_model_unavailable", "provider_mode": "real"},
                    "reviews": [],
                    "errors": [{"packet_id": "packet_001", "error_type": "strong_visual_model_unavailable"}],
                }
                _write_json(path / "ai_visual_review.json", payload)
                return payload

            with patch(
                "scripts.run_stable_ai_improvement_workflow.write_ai_visual_review_report",
                side_effect=fake_visual_review,
            ):
                report = run_workflow(output_dir=output_dir, dry_run=False, quality_gate_mode="real", model="gpt-strong")

        visual_stage = _stage(report, "visual_review")
        self.assertEqual("unavailable", visual_stage["status"])
        self.assertEqual("unavailable", visual_stage["summary"]["status"])

    def test_final_manifest_records_final_quality_gate_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)

            report = run_workflow(output_dir=output_dir, dry_run=True)
            manifest = json.loads((output_dir / "final_ai_improvement_artifact_manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(report["quality_gate"]["summary"]["status"], manifest["quality_gate_status"]["status"])

    def test_final_manifest_stage_preserves_existing_finalized_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            existing_comparison = _comparison_payload("candidate-final", "pass")
            existing_comparison["problem_type"] = "missing_ball"
            existing_comparison["approval_id"] = "approval-final"
            existing_comparison["consumed_approval_ids"] = ["approval-final"]
            _write_json(
                output_dir / "final_ai_improvement_artifact_manifest.json",
                {
                    "schema_version": "1.0",
                    "candidate_outputs": [
                        {
                            "id": "candidate-final",
                            "candidate_id": "candidate-final",
                            "problem_type": "missing_ball",
                            "path": "ai_candidates/missing_ball/candidate-final",
                            "candidate_artifacts": ["ai_candidates/missing_ball/candidate-final/ball_track.csv"],
                        }
                    ],
                    "final_selected_artifacts": [
                        {
                            "candidate_id": "candidate-final",
                            "approval_id": "approval-final",
                            "problem_type": "missing_ball",
                            "output_role": "missing_ball_track",
                            "path": "ai_candidates/missing_ball/candidate-final/ball_track.csv",
                            "operator_decision": {
                                "decision_id": "promote:missing_ball:candidate-final:approval-final:missing_ball_track",
                                "decision": "promote",
                                "approval_id": "approval-final",
                                "candidate_id": "candidate-final",
                                "problem_type": "missing_ball",
                                "output_role": "missing_ball_track",
                            },
                        }
                    ],
                    "consumed_approvals": [{"approval_id": "approval-final", "candidate_id": "candidate-final"}],
                    "comparison_reports": [existing_comparison],
                },
            )

            _final_artifact_manifest_stage(
                output_dir=output_dir,
                dispatcher_stage={
                    "missing_ball_execution_path": {"status": "skipped"},
                    "noise_candidate_execution_path": {"status": "skipped"},
                    "unsupported_actions": [],
                },
                approved_payload={"approved_actions": []},
                resolved_noop_candidates=[],
                quality_gate_summary={"status": "pass"},
            )
            manifest = json.loads((output_dir / "final_ai_improvement_artifact_manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(["candidate-final"], [item["candidate_id"] for item in manifest["final_selected_artifacts"]])
        self.assertEqual(1, manifest["summary"]["final_artifact_count"])

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

            highlight_report = _highlight_comparison_report()
            with patch(
                "scripts.run_stable_ai_improvement_workflow.execute_highlight_candidate",
                return_value=highlight_report,
            ) as execute_highlight:
                report = run_workflow(
                    output_dir=output_dir,
                    dry_run=False,
                    approved_actions_path=approved_path,
                    approved_action_id="highlight_1",
                )
            manifest = json.loads((output_dir / "final_ai_improvement_artifact_manifest.json").read_text(encoding="utf-8"))

        self.assertEqual("skipped", _stage(report, "approved_child_rerun")["status"])
        self.assertEqual("succeeded", _stage(report, "highlight_render")["status"])
        dispatcher = _stage(report, "selected_approval_dispatcher")
        self.assertEqual([], dispatcher["unsupported_actions"])
        self.assertEqual("succeeded", dispatcher["highlight_candidate_execution_path"]["status"])
        self.assertEqual(["highlight_1"], dispatcher["highlight_candidate_execution_path"]["approval_ids"])
        self.assertEqual(["highlight-candidate-1"], dispatcher["highlight_candidate_execution_path"]["candidate_ids"])
        execute_highlight.assert_called_once()
        self.assertEqual("highlight-candidate-1", manifest["candidate_outputs"][0]["candidate_id"])
        self.assertEqual(["highlight-candidate-1"], [item["candidate_id"] for item in manifest["pending_candidates"]])
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

    def test_selected_unknown_approval_action_is_reported_as_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            approved_path = output_dir / "approved_actions.json"
            _write_json(
                approved_path,
                {
                    "approved_actions": [
                        {
                            "approval_id": "mystery_1",
                            "candidate_id": "mystery-candidate-1",
                            "approved_action": "mystery_action",
                            "problem_type": "highlight",
                        }
                    ]
                },
            )

            report = run_workflow(
                output_dir=output_dir,
                dry_run=False,
                approved_actions_path=approved_path,
                approval_ids=["mystery_1"],
            )
            manifest = json.loads((output_dir / "final_ai_improvement_artifact_manifest.json").read_text(encoding="utf-8"))

        dispatcher = _stage(report, "selected_approval_dispatcher")
        self.assertEqual("failed", dispatcher["status"])
        self.assertEqual(["mystery_1"], [item["approval_id"] for item in dispatcher["unsupported_actions"]])
        self.assertEqual("unsupported_candidate_type", dispatcher["unsupported_actions"][0]["reason"])
        self.assertEqual(["mystery_1"], [item["approval_id"] for item in manifest["unsupported_candidates"]])
        self.assertEqual("failed", report["quality_gate"]["summary"]["workflow_status"])

    def test_tracking_rerun_before_follow_cam_blocks_without_linked_tracking_evidence(self) -> None:
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

        dispatcher = _stage(report, "selected_approval_dispatcher")
        self.assertEqual([], dispatcher["unsupported_actions"])
        follow_path = dispatcher["follow_cam_candidate_execution_path"]
        self.assertEqual("blocked", follow_path["status"])
        self.assertEqual(["camera_1"], follow_path["approval_ids"])
        self.assertEqual("linked_tracking_candidate_evidence_required", follow_path["reason"])
        follow_stage = _stage(report, "follow_cam_rerender_plan")
        self.assertEqual("blocked", follow_stage["status"])
        self.assertEqual("linked_tracking_candidate_evidence_required", follow_stage["reason"])

    def test_adjust_follow_cam_selected_action_executes_candidate_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            output_dir = root / "outputs" / "baseline"
            _write_tracks(output_dir)
            input_video = root / "data" / "input.mp4"
            config_path = _write_recovery_config(root, output_dir)
            _write_json(
                output_dir / "run_manifest.json",
                {"config_path": str(config_path), "input_video": str(input_video)},
            )
            approved_path = output_dir / "approved_actions.json"
            _write_json(
                approved_path,
                {"approved_actions": [_adjust_follow_cam_approval("camera_1")]},
            )
            expected_report = {
                "candidate_id": "follow-cam-1",
                "approval_id": "camera_1",
                "problem_type": "follow_cam",
                "candidate_dir": "ai_candidates/follow_cam/follow-cam-1",
                "comparison_report": "ai_candidates/follow_cam/follow-cam-1/follow_cam_candidate_comparison.json",
                "comparison_status": "pass",
                "summary": {"status": "pass"},
                "checks": [{"name": "ok", "status": "pass"}],
                "candidate_artifacts": ["ai_candidates/follow_cam/follow-cam-1/follow_cam.mp4"],
                "consumed_approval_ids": ["camera_1"],
            }

            with patch(
                "scripts.run_stable_ai_improvement_workflow.execute_follow_cam_candidate",
                return_value=expected_report,
            ) as executor:
                report = run_workflow(
                    output_dir=output_dir,
                    input_video=input_video,
                    dry_run=False,
                    approved_actions_path=approved_path,
                    approval_ids=["camera_1"],
                )
            lifecycle = build_ai_candidate_lifecycle(output_dir)

        executor.assert_called_once()
        dispatcher = _stage(report, "selected_approval_dispatcher")
        self.assertEqual([], dispatcher["unsupported_actions"])
        follow_path = dispatcher["follow_cam_candidate_execution_path"]
        self.assertEqual("succeeded", follow_path["status"])
        self.assertEqual(["follow-cam-1"], follow_path["candidate_ids"])
        follow_stage = _stage(report, "follow_cam_rerender_plan")
        self.assertEqual("succeeded", follow_stage["status"])
        manifest_stage = _stage(report, "final_artifact_manifest")
        self.assertEqual(1, manifest_stage["summary"]["candidate_output_count"])
        self.assertEqual("follow_cam", lifecycle["candidates"][0]["problem_type"])

    def test_mixed_follow_cam_selection_executes_valid_action_and_blocks_missing_tracking_link(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            output_dir = root / "outputs" / "baseline"
            _write_tracks(output_dir)
            input_video = root / "data" / "input.mp4"
            config_path = _write_recovery_config(root, output_dir)
            _write_json(
                output_dir / "run_manifest.json",
                {"config_path": str(config_path), "input_video": str(input_video)},
            )
            blocked_action = _tracking_rerun_before_follow_cam_approval("camera_2", start=50, end=80)
            blocked_action["candidate_id"] = "follow-cam-blocked"
            approved_path = output_dir / "approved_actions.json"
            _write_json(
                approved_path,
                {"approved_actions": [_adjust_follow_cam_approval("camera_1"), blocked_action]},
            )
            expected_report = {
                "candidate_id": "follow-cam-1",
                "approval_id": "camera_1",
                "problem_type": "follow_cam",
                "candidate_dir": "ai_candidates/follow_cam/follow-cam-1",
                "comparison_report": "ai_candidates/follow_cam/follow-cam-1/follow_cam_candidate_comparison.json",
                "comparison_status": "pass",
                "summary": {"status": "pass"},
                "checks": [{"name": "ok", "status": "pass"}],
                "candidate_artifacts": ["ai_candidates/follow_cam/follow-cam-1/follow_cam.mp4"],
                "consumed_approval_ids": ["camera_1"],
            }

            with patch(
                "scripts.run_stable_ai_improvement_workflow.execute_follow_cam_candidate",
                return_value=expected_report,
            ) as executor:
                report = run_workflow(
                    output_dir=output_dir,
                    input_video=input_video,
                    dry_run=False,
                    approved_actions_path=approved_path,
                    approval_ids=["camera_1", "camera_2"],
                )
            manifest = json.loads((output_dir / "final_ai_improvement_artifact_manifest.json").read_text(encoding="utf-8"))
            lifecycle = build_ai_candidate_lifecycle(output_dir)

        executor.assert_called_once()
        dispatcher = _stage(report, "selected_approval_dispatcher")
        self.assertEqual("failed", dispatcher["status"])
        follow_path = dispatcher["follow_cam_candidate_execution_path"]
        self.assertEqual("partial_failure", follow_path["status"])
        self.assertEqual(["follow-cam-1"], [item["candidate_id"] for item in follow_path["candidate_outputs"]])
        self.assertEqual(["follow-cam-blocked"], [item["candidate_id"] for item in follow_path["blocked"]])
        self.assertEqual(
            ["follow-cam-1", "follow-cam-blocked"],
            [item["candidate_id"] for item in manifest["pending_candidates"]],
        )
        lifecycle_by_id = {item["candidate_id"]: item for item in lifecycle["candidates"]}
        self.assertEqual("blocked", lifecycle_by_id["follow-cam-blocked"]["promotion_status"])
        self.assertIn("missing_evidence", lifecycle_by_id["follow-cam-blocked"]["blocking_reasons"])

    def test_follow_cam_preflight_failure_keeps_rejected_candidate_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            approved_path = output_dir / "approved_actions.json"
            _write_json(
                approved_path,
                {"approved_actions": [_adjust_follow_cam_approval("camera_1")]},
            )

            report = run_workflow(
                output_dir=output_dir,
                dry_run=False,
                approved_actions_path=approved_path,
                approval_ids=["camera_1"],
            )
            manifest = json.loads((output_dir / "final_ai_improvement_artifact_manifest.json").read_text(encoding="utf-8"))

        dispatcher = _stage(report, "selected_approval_dispatcher")
        self.assertEqual("failed", dispatcher["status"])
        follow_path = dispatcher["follow_cam_candidate_execution_path"]
        self.assertEqual("failed", follow_path["status"])
        self.assertEqual("follow-cam-1", follow_path["errors"][0]["candidate_id"])
        self.assertEqual("camera_1", follow_path["errors"][0]["approval_id"])
        self.assertEqual(1, manifest["summary"]["rejected_candidate_count"])
        self.assertEqual("follow-cam-1", manifest["rejected_candidates"][0]["candidate_id"])
        self.assertEqual(["camera_1"], manifest["rejected_candidates"][0]["approval_ids"])

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

    def test_mixed_follow_cam_action_id_and_highlight_approval_ids_execute_selected_highlight(self) -> None:
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

            with patch(
                "scripts.run_stable_ai_improvement_workflow.execute_highlight_candidate",
                return_value=_highlight_comparison_report(),
            ) as execute_highlight:
                report = run_workflow(
                    output_dir=output_dir,
                    dry_run=False,
                    approved_actions_path=approved_path,
                    approval_ids=["highlight_1"],
                    approved_action_id="camera_1",
                )
            manifest = json.loads((output_dir / "final_ai_improvement_artifact_manifest.json").read_text(encoding="utf-8"))

        self.assertEqual("succeeded", _stage(report, "highlight_render")["status"])
        execute_highlight.assert_called_once()
        self.assertEqual(
            ["highlight-candidate-1", "follow-cam-1"],
            [item["candidate_id"] for item in manifest["pending_candidates"]],
        )

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
            manifest = json.loads((output_dir / "final_ai_improvement_artifact_manifest.json").read_text(encoding="utf-8"))

        child_stage = _stage(report, "approved_child_rerun")
        dispatcher = _stage(report, "selected_approval_dispatcher")
        self.assertEqual("failed", dispatcher["missing_ball_execution_path"]["status"])
        self.assertEqual("failed", dispatcher["missing_ball_execution_path"]["execution_status"])
        self.assertEqual("failed", child_stage["status"])
        self.assertFalse(child_stage["api_required"])
        self.assertEqual("sahi_roi", child_stage["intended_rerun_mode"])
        self.assertEqual([{"approval_id": "approval_1", "start_frame": 10, "end_frame": 20}], child_stage["bounded_windows"])
        self.assertFalse(child_stage["runs_full_video_sahi"])
        self.assertEqual(0, manifest["summary"]["candidate_output_count"])
        self.assertEqual(0, manifest["summary"]["pending_candidate_count"])

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
        "candidate_id": "highlight-candidate-1",
        "event_candidate_id": "event-1",
        "problem_type": "highlight",
        "approved_action": "render_suggested_highlight",
        "suggested_window": {"start_frame": start, "end_frame": end},
        "source_packet_id": "packet_highlight",
    }


def _tracking_rerun_before_follow_cam_approval(approval_id: str, *, start: int, end: int) -> dict[str, object]:
    return {
        "approval_id": approval_id,
        "improvement_id": "camera_imp_1",
        "candidate_id": "follow-cam-1",
        "approved_action": "tracking_rerun_before_follow_cam",
        "rerun_scope": {"start_frame": start, "end_frame": end},
        "source_packet_id": "packet_camera",
    }


def _adjust_follow_cam_approval(approval_id: str) -> dict[str, object]:
    return {
        "approval_id": approval_id,
        "improvement_id": "camera_imp_1",
        "candidate_id": "follow-cam-1",
        "problem_type": "follow_cam",
        "approved_action": "adjust_follow_cam",
        "config_patch": {"follow_cam": {"glide_pan_smoothing": 0.07}},
        "source_packet_id": "packet_camera",
    }


def _noise_approval(approval_id: str, *, start: int, end: int) -> dict[str, object]:
    return {
        "approval_id": approval_id,
        "improvement_id": "noise_imp_1",
        "candidate_id": "noise-candidate-1",
        "problem_type": "noise",
        "approved_action": "noise_filter_adjustment",
        "start_frame": start,
        "end_frame": end,
        "false_positive_class": "shoe_confusion",
        "config_patch": {"selection": {"priors": {"enabled": True}}},
        "source_packet_id": "packet_noise",
    }


def _not_visible_approval(approval_id: str, *, candidate_id: str, start: int, end: int) -> dict[str, object]:
    return {
        "approval_id": approval_id,
        "improvement_id": f"imp_{approval_id}",
        "candidate_id": candidate_id,
        "problem_type": "missing_ball",
        "approved_action": "manual_review",
        "start_frame": start,
        "end_frame": end,
        "source_packet_id": "packet_2079",
        "likely_ball_region": {"description": "not_visible", "status": "resolved_not_visible"},
        "resolution": "not_visible",
        "evidence": [{"source_packet_id": "packet_2079", "reason": "packet marks ball_not_visible"}],
    }


def _write_tracks(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    text = "Frame,X,Y,Status\n1,10,20,Detected\n"
    (output_dir / "ball_track.csv").write_text(text, encoding="utf-8")
    (output_dir / "ball_track.cleaned.csv").write_text(text, encoding="utf-8")


def _write_lost_tracks(output_dir: Path, *, start: int, end: int) -> None:
    rows = ["Frame,X,Y,Confidence,Status"]
    rows.extend(f"{frame},,,0.0000,Lost" for frame in range(start, end + 1))
    text = "\n".join(rows) + "\n"
    (output_dir / "ball_track.csv").write_text(text, encoding="utf-8")
    (output_dir / "ball_track.cleaned.csv").write_text(text, encoding="utf-8")


def _write_recovery_config(root: Path, output_dir: Path) -> Path:
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "weights").mkdir(parents=True, exist_ok=True)
    (root / "data" / "input.mp4").write_text("fake video", encoding="utf-8")
    (root / "weights" / "football_ball_yolo.pt").write_text("fake weights", encoding="utf-8")
    config_path = root / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                f"input_video: {str(root / 'data' / 'input.mp4')}",
                f"output_dir: {str(output_dir)}",
                "detector:",
                f"  model_path: {str(root / 'weights' / 'football_ball_yolo.pt')}",
                "postprocess:",
                "  enabled: true",
                "follow_cam:",
                "  enabled: true",
                "temporal_chunks:",
                "  enabled: false",
                "high_recall_windows:",
                "  enabled: false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return config_path


def _write_noise_tracks(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = ["Frame,X,Y,Confidence,Status"]
    detected_ranges = [(10, 24), (30, 31), (40, 40), (50, 51)]
    for frame in range(80):
        detected = any(start <= frame <= end for start, end in detected_ranges)
        if detected:
            rows.append(f"{frame},{100 + frame},{200 + frame},0.90,Detected")
        else:
            rows.append(f"{frame},,,0.00,Lost")
    text = "\n".join(rows) + "\n"
    (output_dir / "ball_track.csv").write_text(text, encoding="utf-8")
    (output_dir / "ball_track.cleaned.csv").write_text(text, encoding="utf-8")
    _write_json(
        output_dir / "ball_audit.json",
        {
            "summary": {"review_event_count": 3},
            "review_events": [
                {
                    "type": "short_tracklet",
                    "start_frame": start,
                    "end_frame": end,
                    "frame_count": end - start + 1,
                    "reason": "short false-positive island",
                }
                for start, end in detected_ranges[1:]
            ],
        },
    )


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


def _write_packet(output_dir: Path, *, packet_id: str, start: int, end: int, label: str = "needs_ai_review") -> None:
    _write_json(
        output_dir / "review_packets.json",
        {
            "packets": [
                {
                    "packet_id": packet_id,
                    "window": {"start_frame": start, "end_frame": end},
                    "decision": {"label": label},
                }
            ]
        },
    )


def _comparison_payload(candidate_id: str, status: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "candidate_id": candidate_id,
        "problem_type": "missing_ball",
        "status": status,
        "summary": {"status": status},
        "checks": [{"name": "fixture", "status": status}],
        "candidate": {"id": candidate_id, "path": f"ai_candidates/missing_ball/{candidate_id}"},
    }


def _highlight_comparison_report(status: str = "pass") -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "candidate_id": "highlight-candidate-1",
        "approval_id": "highlight_1",
        "consumed_approval_ids": ["highlight_1"],
        "problem_type": "highlight",
        "event_candidate_id": "event-1",
        "candidate_dir": "ai_candidates/highlight/highlight-candidate-1",
        "comparison_report": "ai_candidates/highlight/highlight-candidate-1/highlight_candidate_comparison.json",
        "comparison_status": status,
        "candidate_artifacts": [
            "ai_candidates/highlight/highlight-candidate-1/highlight.mp4",
            "ai_candidates/highlight/highlight-candidate-1/highlight_report.json",
            "ai_candidates/highlight/highlight-candidate-1/highlight_window_validation.json",
            "ai_candidates/highlight/highlight-candidate-1/highlight_candidate_comparison.json",
            "ai_candidates/highlight/highlight-candidate-1/candidate_manifest.json",
        ],
        "candidate": {
            "id": "highlight-candidate-1",
            "path": "ai_candidates/highlight/highlight-candidate-1/highlight.mp4",
        },
        "summary": {
            "status": status,
            "check_count": 1,
            "passed_check_count": 1 if status == "pass" else 0,
            "failed_check_count": 1 if status == "fail" else 0,
            "warning_count": 0,
            "unavailable_count": 0,
            "requires_human_confirmation": False,
            "promotion_eligible": status == "pass",
        },
        "checks": [{"name": "fixture", "status": status}],
    }


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
