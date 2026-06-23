from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from football_tracking.ai_improvement_quality_gate import (
    build_ai_improvement_quality_gate,
    write_ai_improvement_quality_gate,
    write_track_hash_snapshot,
)
from football_tracking.final_artifact_manifest import finalize_ai_candidate, write_final_artifact_manifest


class AiImprovementQualityGateTests(unittest.TestCase):
    def test_missing_inputs_are_unavailable_in_artifact_mode_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)

            payload = write_ai_improvement_quality_gate(output_dir, mode="artifact-only")
            written = json.loads((output_dir / "ai_improvement_quality_gate.json").read_text(encoding="utf-8"))

        self.assertEqual("warn", payload["summary"]["status"])
        self.assertEqual("unavailable", payload["checks"]["track_hash_unchanged"]["status"])
        self.assertEqual("unavailable", payload["checks"]["long_lost_gap_improvement_coverage"]["status"])
        self.assertEqual("unavailable", payload["checks"]["model_routing_recorded"]["status"])
        self.assertEqual(payload, written)

    def test_real_mode_fails_when_required_artifacts_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            payload = build_ai_improvement_quality_gate(Path(temp_name), mode="real")

        self.assertEqual("fail", payload["summary"]["status"])
        self.assertEqual("fail", payload["checks"]["track_hash_unchanged"]["status"])
        self.assertEqual("fail", payload["checks"]["long_lost_gap_improvement_coverage"]["status"])
        self.assertGreater(payload["summary"]["failed_check_count"], 0)

    def test_real_mode_fails_when_review_packets_are_missing_even_without_lost_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            write_track_hash_snapshot(output_dir, "before_review")
            write_track_hash_snapshot(output_dir, "after_ai_improvement")
            _write_json(output_dir / "ball_audit.json", {"review_events": []})
            _write_json(
                output_dir / "ai_improvement_report.json",
                {
                    "summary": {"status": "ok"},
                    "model": "gpt-improve",
                    "model_selection_source": "explicit",
                    "improvements": [],
                },
            )

            payload = build_ai_improvement_quality_gate(output_dir, mode="real")

        self.assertEqual("fail", payload["checks"]["long_lost_gap_improvement_coverage"]["status"])
        self.assertIn("review_packets.json", payload["checks"]["long_lost_gap_improvement_coverage"]["reason"])

    def test_hash_snapshots_pass_when_tracks_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir, raw="Frame,X,Y,Status\n1,10,20,Detected\n")

            before = write_track_hash_snapshot(output_dir, "before_review")
            after = write_track_hash_snapshot(output_dir, "after_ai_improvement")
            expected_hash = _sha256_file(output_dir / "ball_track.csv")
            payload = build_ai_improvement_quality_gate(output_dir)

        self.assertEqual(expected_hash, before["files"]["ball_track.csv"]["sha256"])
        self.assertEqual(before["files"], after["files"])
        self.assertEqual("pass", payload["checks"]["track_hash_unchanged"]["status"])

    def test_hash_snapshots_fail_when_review_phase_mutates_track_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir, raw="Frame,X,Y,Status\n1,10,20,Detected\n")
            write_track_hash_snapshot(output_dir, "before_review")
            _write_tracks(output_dir, raw="Frame,X,Y,Status\n1,99,20,Detected\n")
            write_track_hash_snapshot(output_dir, "after_ai_improvement")

            payload = build_ai_improvement_quality_gate(output_dir)

        self.assertEqual("fail", payload["checks"]["track_hash_unchanged"]["status"])
        self.assertIn("ball_track.csv", payload["checks"]["track_hash_unchanged"]["changed_files"])

    def test_approved_actions_file_presence_is_not_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_2079_gap(output_dir)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544)
            _write_ai_report(output_dir, [_missing_ball_improvement(start=2049, end=2544)])
            _write_approved_actions(output_dir, [_targeted_rerun_approval(start=2049, end=2544)])

            payload = build_ai_improvement_quality_gate(output_dir)

        self.assertEqual("fail", payload["checks"]["approved_actions_explicitly_consumed"]["status"])
        self.assertEqual("fail", payload["checks"]["long_lost_gap_improvement_coverage"]["status"])

    def test_long_lost_gap_2079_fails_without_packet_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_2079_gap(output_dir)
            _write_ai_report(output_dir, [_missing_ball_improvement(start=2049, end=2544)])
            approved_path = _write_approved_actions(output_dir, [_targeted_rerun_approval(start=2049, end=2544)])

            payload = build_ai_improvement_quality_gate(output_dir, approved_actions_path=approved_path)

        self.assertEqual("fail", payload["checks"]["long_lost_gap_improvement_coverage"]["status"])
        self.assertIn("packet", payload["checks"]["long_lost_gap_improvement_coverage"]["reasons"][0])

    def test_long_lost_gap_2079_fails_without_ai_or_approval_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_2079_gap(output_dir)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544)
            _write_ai_report(output_dir, [])

            payload = build_ai_improvement_quality_gate(output_dir)

        self.assertEqual("fail", payload["checks"]["long_lost_gap_improvement_coverage"]["status"])
        self.assertTrue(any("AI improvement" in reason for reason in payload["checks"]["long_lost_gap_improvement_coverage"]["reasons"]))

    def test_long_lost_gap_2079_passes_with_explicit_targeted_rerun_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_2079_gap(output_dir)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544)
            _write_ai_report(output_dir, [_missing_ball_improvement(start=2049, end=2544)])
            approved_path = _write_approved_actions(output_dir, [_targeted_rerun_approval(start=2049, end=2544)])

            payload = build_ai_improvement_quality_gate(output_dir, approved_actions_path=approved_path)

        self.assertEqual("pass", payload["checks"]["long_lost_gap_improvement_coverage"]["status"])
        self.assertEqual("pass", payload["checks"]["missing_ball_roi_or_not_visible_present"]["status"])

    def test_real_mode_selected_missing_ball_approval_requires_comparison_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            write_track_hash_snapshot(output_dir, "before_review")
            write_track_hash_snapshot(output_dir, "after_ai_improvement")
            _write_2079_gap(output_dir)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544)
            _write_ai_report(output_dir, [_missing_ball_improvement(start=2049, end=2544)])
            approval = {**_targeted_rerun_approval(start=2049, end=2544), "candidate_id": "candidate_2079"}
            approved_path = _write_approved_actions(output_dir, [approval])

            payload = build_ai_improvement_quality_gate(output_dir, mode="real", approved_actions_path=approved_path)

        self.assertNotEqual("pass", payload["summary"]["status"])
        self.assertEqual("unavailable", payload["checks"]["candidate_comparisons_ok"]["status"])
        self.assertTrue(
            any(
                report.get("artifact_status") == "selected_missing_ball_approval_missing_comparison"
                for report in payload["checks"]["candidate_comparisons_ok"]["reports"]
            )
        )

    def test_real_mode_selected_missing_ball_approval_ignores_other_problem_type_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            write_track_hash_snapshot(output_dir, "before_review")
            write_track_hash_snapshot(output_dir, "after_ai_improvement")
            _write_2079_gap(output_dir)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544)
            _write_ai_report(output_dir, [_missing_ball_improvement(start=2049, end=2544)])
            approval = {**_targeted_rerun_approval(start=2049, end=2544), "candidate_id": "candidate_2079"}
            approved_path = _write_approved_actions(output_dir, [approval])
            comparison = _comparison_payload("candidate_2079", "pass")
            comparison["problem_type"] = "noise"
            comparison["approval_id"] = approval["approval_id"]
            comparison["consumed_approval_ids"] = [approval["approval_id"]]
            _write_json(output_dir / "missing_ball_recovery_comparison.json", comparison)

            payload = build_ai_improvement_quality_gate(output_dir, mode="real", approved_actions_path=approved_path)

        self.assertEqual("unavailable", payload["checks"]["candidate_comparisons_ok"]["status"])
        self.assertTrue(
            any(
                report.get("artifact_status") == "selected_missing_ball_approval_missing_comparison"
                for report in payload["checks"]["candidate_comparisons_ok"]["reports"]
            )
        )

    def test_real_mode_selected_missing_ball_approval_requires_consumed_approval_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            write_track_hash_snapshot(output_dir, "before_review")
            write_track_hash_snapshot(output_dir, "after_ai_improvement")
            _write_2079_gap(output_dir)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544)
            _write_ai_report(output_dir, [_missing_ball_improvement(start=2049, end=2544)])
            approval = {**_targeted_rerun_approval(start=2049, end=2544), "candidate_id": "candidate_2079"}
            approved_path = _write_approved_actions(output_dir, [approval])
            comparison = _comparison_payload("candidate_2079", "pass")
            comparison["approval_id"] = "other_approval"
            comparison["consumed_approval_ids"] = ["other_approval"]
            _write_json(output_dir / "missing_ball_recovery_comparison.json", comparison)

            payload = build_ai_improvement_quality_gate(output_dir, mode="real", approved_actions_path=approved_path)

        self.assertEqual("unavailable", payload["checks"]["candidate_comparisons_ok"]["status"])
        self.assertTrue(
            any(
                report.get("artifact_status") == "selected_missing_ball_approval_missing_comparison"
                for report in payload["checks"]["candidate_comparisons_ok"]["reports"]
            )
        )

    def test_real_mode_selected_noise_approval_requires_noise_comparison_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            write_track_hash_snapshot(output_dir, "before_review")
            write_track_hash_snapshot(output_dir, "after_ai_improvement")
            _write_packet(output_dir, packet_id="packet_noise", start=10, end=20)
            _write_ai_report(
                output_dir,
                [
                    {
                        "id": "imp_noise",
                        "area": "tracking",
                        "failure_tags": ["shoe_confusion"],
                        "recommended_action": "noise_filter_adjustment",
                        "start_frame": 10,
                        "end_frame": 20,
                        "source_packet_id": "packet_noise",
                    }
                ],
            )
            approved_path = _write_approved_actions(
                output_dir,
                [
                    {
                        "approval_id": "approval_noise",
                        "candidate_id": "noise-candidate",
                        "approved_action": "noise_filter_adjustment",
                        "problem_type": "noise",
                        "start_frame": 10,
                        "end_frame": 20,
                        "source_packet_id": "packet_noise",
                        "false_positive_class": "shoe_confusion",
                        "config_patch": {"selection": {"min_accept_score": 0.62}},
                    }
                ],
            )

            payload = build_ai_improvement_quality_gate(output_dir, mode="real", approved_actions_path=approved_path)

        self.assertEqual("unavailable", payload["checks"]["candidate_comparisons_ok"]["status"])
        self.assertTrue(
            any(
                report.get("artifact_status") == "selected_noise_approval_missing_comparison"
                for report in payload["checks"]["candidate_comparisons_ok"]["reports"]
            )
        )

    def test_real_mode_selected_noise_approval_requires_candidate_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            write_track_hash_snapshot(output_dir, "before_review")
            write_track_hash_snapshot(output_dir, "after_ai_improvement")
            _write_packet(output_dir, packet_id="packet_noise", start=10, end=20)
            _write_ai_report(
                output_dir,
                [
                    {
                        "id": "imp_noise",
                        "area": "tracking",
                        "failure_tags": ["shoe_confusion"],
                        "recommended_action": "noise_filter_adjustment",
                        "start_frame": 10,
                        "end_frame": 20,
                        "source_packet_id": "packet_noise",
                    }
                ],
            )
            approved_path = _write_approved_actions(
                output_dir,
                [
                    {
                        "approval_id": "approval_noise",
                        "approved_action": "noise_filter_adjustment",
                        "problem_type": "noise",
                        "start_frame": 10,
                        "end_frame": 20,
                        "source_packet_id": "packet_noise",
                        "false_positive_class": "shoe_confusion",
                    }
                ],
            )

            payload = build_ai_improvement_quality_gate(output_dir, mode="real", approved_actions_path=approved_path)

        self.assertEqual("unavailable", payload["checks"]["candidate_comparisons_ok"]["status"])
        self.assertTrue(
            any(
                report.get("artifact_status") == "selected_noise_approval_missing_candidate_id"
                for report in payload["checks"]["candidate_comparisons_ok"]["reports"]
            )
        )

    def test_long_lost_gap_2079_fails_when_approval_lacks_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_2079_gap(output_dir)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544)
            _write_ai_report(output_dir, [_missing_ball_improvement(start=2049, end=2544)])
            approval = _targeted_rerun_approval(start=2049, end=2544)
            approval.pop("provenance")
            approved_path = _write_approved_actions(output_dir, [approval])

            payload = build_ai_improvement_quality_gate(output_dir, approved_actions_path=approved_path)

        self.assertEqual("fail", payload["checks"]["long_lost_gap_improvement_coverage"]["status"])
        self.assertTrue(
            any("provenance" in reason for reason in payload["checks"]["long_lost_gap_improvement_coverage"]["reasons"])
        )

    def test_long_lost_gap_2079_warns_for_evidence_backed_not_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_2079_gap(output_dir)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544)
            _write_ai_report(
                output_dir,
                [_not_visible_improvement(start=2049, end=2544, source_packet_id="packet_2079")],
            )

            payload = build_ai_improvement_quality_gate(output_dir)

        self.assertEqual("warn", payload["checks"]["long_lost_gap_improvement_coverage"]["status"])
        self.assertEqual("warn", payload["checks"]["missing_ball_roi_or_not_visible_present"]["status"])

    def test_long_lost_gap_2079_real_mode_fails_for_unavailable_not_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_2079_gap(output_dir)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544)
            _write_ai_report(
                output_dir,
                [_not_visible_improvement(start=2049, end=2544, status="unavailable", source_packet_id="packet_2079")],
            )
            _write_tracks(output_dir)
            write_track_hash_snapshot(output_dir, "before_review")
            write_track_hash_snapshot(output_dir, "after_ai_improvement")

            payload = build_ai_improvement_quality_gate(output_dir, mode="real")

        self.assertEqual("fail", payload["checks"]["long_lost_gap_improvement_coverage"]["status"])

    def test_long_lost_gap_2079_real_mode_warns_for_evidence_backed_not_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_2079_gap(output_dir)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544)
            _write_ai_report(
                output_dir,
                [_not_visible_improvement(start=2049, end=2544, source_packet_id="packet_2079")],
            )
            _write_tracks(output_dir)
            write_track_hash_snapshot(output_dir, "before_review")
            write_track_hash_snapshot(output_dir, "after_ai_improvement")

            payload = build_ai_improvement_quality_gate(output_dir, mode="real")

        self.assertEqual("warn", payload["checks"]["long_lost_gap_improvement_coverage"]["status"])
        self.assertEqual("warn", payload["checks"]["missing_ball_roi_or_not_visible_present"]["status"])

    def test_missing_ball_resolution_passes_with_packet_not_visible_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            _write_2079_gap(output_dir)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544, label="ball_not_visible")
            _write_missing_ball_resolution(output_dir, start=2049, end=2544, source_packet_id="packet_2079")

            payload = build_ai_improvement_quality_gate(output_dir)

        self.assertEqual("pass", payload["checks"]["long_lost_gap_improvement_coverage"]["status"])
        missing_ball_check = payload["checks"]["missing_ball_roi_or_not_visible_present"]
        self.assertEqual("pass", missing_ball_check["status"])
        self.assertTrue(missing_ball_check["resolved_not_visible"])

    def test_missing_ball_resolution_without_packet_or_visual_evidence_fails_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            _write_2079_gap(output_dir)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544, label="needs_ai_review")
            _write_missing_ball_resolution(output_dir, start=2049, end=2544, source_packet_id="packet_2079")

            payload = build_ai_improvement_quality_gate(output_dir)

        self.assertEqual("fail", payload["checks"]["long_lost_gap_improvement_coverage"]["status"])
        self.assertTrue(
            any(
                "resolution lacks not_visible packet or visual evidence" in reason
                for reason in payload["checks"]["long_lost_gap_improvement_coverage"]["reasons"]
            )
        )

    def test_missing_ball_resolution_requires_evidence_window_to_cover_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            _write_2079_gap(output_dir)
            _write_packet(output_dir, packet_id="packet_2079", start=2079, end=2079, label="ball_not_visible")
            _write_missing_ball_resolution(output_dir, start=2049, end=2544, source_packet_id="packet_2079")

            payload = build_ai_improvement_quality_gate(output_dir)

        self.assertEqual("fail", payload["checks"]["long_lost_gap_improvement_coverage"]["status"])
        self.assertTrue(
            any(
                "resolution lacks not_visible packet or visual evidence" in reason
                for reason in payload["checks"]["long_lost_gap_improvement_coverage"]["reasons"]
            )
        )

    def test_dense_noise_requires_failure_tag_and_window_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_review_triggers.json",
                {"triggers": [{"id": "noise_1", "type": "dense_noise", "start_frame": 100, "end_frame": 150}]},
            )
            _write_ai_report(
                output_dir,
                [
                    {
                        "id": "imp_noise_bad",
                        "area": "tracking",
                        "failure_tags": ["ball_lost"],
                        "recommended_action": "noise_filter_adjustment",
                        "start_frame": 200,
                        "end_frame": 210,
                    }
                ],
            )

            failing = build_ai_improvement_quality_gate(output_dir)
            _write_ai_report(
                output_dir,
                [
                    {
                        "id": "imp_noise_good",
                        "area": "tracking",
                        "failure_tags": ["shoe_confusion"],
                        "recommended_action": "noise_filter_adjustment",
                        "start_frame": 120,
                        "end_frame": 130,
                    }
                ],
            )
            passing = build_ai_improvement_quality_gate(output_dir)

        self.assertEqual("fail", failing["checks"]["noise_failure_tags_present"]["status"])
        self.assertEqual("pass", passing["checks"]["noise_failure_tags_present"]["status"])

    def test_short_tracklet_noise_requires_false_positive_tag_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ball_audit.json",
                {
                    "review_events": [
                        {
                            "type": "short_tracklet",
                            "start_frame": 300,
                            "end_frame": 301,
                            "frame_count": 2,
                            "flags": ["short_tracklet"],
                        }
                    ]
                },
            )
            _write_ai_report(output_dir, [])

            failing = build_ai_improvement_quality_gate(output_dir)
            _write_ai_report(
                output_dir,
                [
                    {
                        "id": "imp_noise_tracklet",
                        "area": "tracking",
                        "failure_tags": ["foot_confusion"],
                        "recommended_action": "reject_noise",
                        "start_frame": 300,
                        "end_frame": 301,
                        "false_positive_class": "foot_confusion",
                    }
                ],
            )
            passing = build_ai_improvement_quality_gate(output_dir)

        self.assertEqual("fail", failing["checks"]["noise_failure_tags_present"]["status"])
        self.assertEqual("pass", passing["checks"]["noise_failure_tags_present"]["status"])

    def test_camera_regression_passes_within_five_percent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source_dir = Path(temp_name) / "source"
            candidate_dir = Path(temp_name) / "candidate"
            _write_camera_audit(source_dir, review_events=10, max_pan=100.0, p95_pan=80.0)
            _write_camera_audit(candidate_dir, review_events=10, max_pan=105.0, p95_pan=84.0)

            payload = build_ai_improvement_quality_gate(source_dir, candidate_output_dir=candidate_dir)

        self.assertEqual("pass", payload["checks"]["camera_regression"]["status"])

    def test_camera_regression_fails_beyond_five_percent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source_dir = Path(temp_name) / "source"
            candidate_dir = Path(temp_name) / "candidate"
            _write_camera_audit(source_dir, review_events=10, max_pan=100.0, p95_pan=80.0)
            _write_camera_audit(candidate_dir, review_events=12, max_pan=106.0, p95_pan=90.0)

            payload = build_ai_improvement_quality_gate(source_dir, candidate_output_dir=candidate_dir)

        self.assertEqual("fail", payload["checks"]["camera_regression"]["status"])
        self.assertIn("max_pan_step_px", payload["checks"]["camera_regression"]["regressions"])

    def test_highlight_tail_fails_when_suggested_window_clips_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_event_candidate(output_dir, candidate_id="clip_1", core_end=100, min_tail=40, source_frames=200)
            _write_ai_report(
                output_dir,
                [
                    {
                        "id": "imp_highlight",
                        "area": "highlights",
                        "failure_tags": ["post_roll_too_short"],
                        "recommended_action": "render_suggested_highlight",
                        "candidate_id": "clip_1",
                        "suggested_window": {"start_frame": 80, "end_frame": 120},
                    }
                ],
            )

            payload = build_ai_improvement_quality_gate(output_dir)

        self.assertEqual("fail", payload["checks"]["highlight_tail_ok"]["status"])

    def test_highlight_tail_passes_when_tail_reaches_source_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_event_candidate(output_dir, candidate_id="clip_1", core_end=100, min_tail=40, source_frames=121)
            _write_ai_report(
                output_dir,
                [
                    {
                        "id": "imp_highlight",
                        "area": "highlights",
                        "failure_tags": ["post_roll_too_short"],
                        "recommended_action": "render_suggested_highlight",
                        "candidate_id": "clip_1",
                        "suggested_window": {"start_frame": 80, "end_frame": 120},
                    }
                ],
            )

            payload = build_ai_improvement_quality_gate(output_dir)

        self.assertEqual("pass", payload["checks"]["highlight_tail_ok"]["status"])

    def test_highlight_tail_does_not_clamp_to_generic_frame_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "event_candidates.json",
                {
                    "summary": {"frame_count": 121},
                    "candidates": [
                        {
                            "id": "clip_1",
                            "core_window": {"start_frame": 90, "end_frame": 100},
                            "render_window": {"start_frame": 80, "end_frame": 120},
                            "buffer_policy": {"min_tail_frames": 40},
                        }
                    ],
                },
            )
            _write_ai_report(
                output_dir,
                [
                    {
                        "id": "imp_highlight",
                        "area": "highlights",
                        "failure_tags": ["post_roll_too_short"],
                        "recommended_action": "render_suggested_highlight",
                        "candidate_id": "clip_1",
                        "suggested_window": {"start_frame": 80, "end_frame": 120},
                    }
                ],
            )

            payload = build_ai_improvement_quality_gate(output_dir)

        self.assertEqual("fail", payload["checks"]["highlight_tail_ok"]["status"])
        self.assertEqual(140, payload["checks"]["highlight_tail_ok"]["failures"][0]["required_end_frame"])

    def test_real_mode_missing_event_candidates_is_unavailable_not_failure_without_highlight_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_tracks(output_dir)
            write_track_hash_snapshot(output_dir, "before_review")
            write_track_hash_snapshot(output_dir, "after_ai_improvement")
            _write_json(output_dir / "ball_audit.json", {"review_events": []})
            _write_json(output_dir / "review_packets.json", {"packets": []})
            _write_json(
                output_dir / "ai_improvement_report.json",
                {
                    "summary": {"status": "ok"},
                    "model": "gpt-improve",
                    "model_selection_source": "explicit",
                    "improvements": [],
                },
            )

            payload = build_ai_improvement_quality_gate(output_dir, mode="real")

        self.assertEqual("unavailable", payload["checks"]["highlight_tail_ok"]["status"])
        self.assertNotEqual("fail", payload["summary"]["status"])

    def test_real_provider_mode_requires_selected_models(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_real_required_without_models(output_dir)

            missing = build_ai_improvement_quality_gate(output_dir, mode="real")
            _write_json(
                output_dir / "ai_improvement_report.json",
                {
                    "summary": {"status": "ok"},
                    "model": "gpt-improve",
                    "model_selection_source": "explicit",
                    "improvements": [],
                },
            )
            _write_json(output_dir / "ai_visual_review.json", {"model": "gpt-vision", "reviews": []})
            present = build_ai_improvement_quality_gate(output_dir, mode="real")

        self.assertEqual("fail", missing["checks"]["model_routing_recorded"]["status"])
        self.assertEqual("pass", present["checks"]["model_routing_recorded"]["status"])

    def test_real_provider_mode_rejects_dry_run_model_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_real_required_without_models(output_dir)
            _write_json(
                output_dir / "ai_improvement_report.json",
                {
                    "summary": {"status": "ok"},
                    "model": "gpt-improve",
                    "model_selection_source": "explicit",
                    "dry_run": True,
                    "improvements": [],
                },
            )

            payload = build_ai_improvement_quality_gate(output_dir, mode="real")

        self.assertEqual("fail", payload["checks"]["model_routing_recorded"]["status"])
        self.assertIn("dry-run", payload["checks"]["model_routing_recorded"]["reason"])

    def test_dry_run_provider_unavailable_is_warning_not_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(output_dir / "ai_improvement_report.json", {"summary": {"status": "unavailable"}, "improvements": []})

            payload = build_ai_improvement_quality_gate(output_dir, mode="dry-run")

        self.assertEqual("warn", payload["checks"]["model_routing_recorded"]["status"])
        self.assertEqual("dry-run", payload["checks"]["model_routing_recorded"]["mode"])
        self.assertNotEqual("fail", payload["summary"]["status"])

    def test_malformed_approved_actions_artifact_fails_explicit_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(approved_path, {"approved_actions": {"not": "a list"}})

            payload = build_ai_improvement_quality_gate(output_dir, approved_actions_path=approved_path)

        self.assertEqual("fail", payload["checks"]["approved_actions_explicitly_consumed"]["status"])
        self.assertIn("approved_actions", payload["checks"]["approved_actions_explicitly_consumed"]["reason"])

    def test_missing_explicit_approved_actions_path_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            approved_path = output_dir / "missing_approved_actions.json"

            payload = build_ai_improvement_quality_gate(output_dir, approved_actions_path=approved_path)

        self.assertEqual("fail", payload["checks"]["approved_actions_explicitly_consumed"]["status"])
        self.assertIn("could not be loaded", payload["checks"]["approved_actions_explicitly_consumed"]["reason"])

    def test_provenance_must_reference_existing_packet_or_visual_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_2079_gap(output_dir)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544)
            _write_ai_report(output_dir, [_missing_ball_improvement(start=2049, end=2544)])
            approval = _targeted_rerun_approval(start=2049, end=2544)
            approval["provenance"] = {"source_packet_id": "packet_missing"}
            approved_path = _write_approved_actions(output_dir, [approval])

            payload = build_ai_improvement_quality_gate(output_dir, approved_actions_path=approved_path)

        self.assertEqual("fail", payload["checks"]["long_lost_gap_improvement_coverage"]["status"])
        self.assertTrue(
            any("provenance" in reason for reason in payload["checks"]["long_lost_gap_improvement_coverage"]["reasons"])
        )

    def test_evidence_payload_packet_provenance_can_cover_not_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_2079_gap(output_dir)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544)
            improvement = _not_visible_improvement(start=2049, end=2544)
            improvement["evidence_payload"] = {"source_packet_id": "packet_2079"}
            _write_ai_report(output_dir, [improvement])

            payload = build_ai_improvement_quality_gate(output_dir)

        self.assertEqual("warn", payload["checks"]["long_lost_gap_improvement_coverage"]["status"])

    def test_candidate_comparison_reports_are_summarized_in_quality_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(output_dir / "missing_ball_comparison.json", _comparison_payload("candidate-pass", "pass"))
            _write_json(output_dir / "noise_comparison.json", _comparison_payload("candidate-warn", "warn"))
            _write_json(output_dir / "follow_cam_comparison.json", _comparison_payload("candidate-fail", "fail"))

            payload = build_ai_improvement_quality_gate(output_dir)

        comparison_check = payload["checks"]["candidate_comparisons_ok"]
        self.assertEqual("fail", comparison_check["status"])
        self.assertEqual(3, comparison_check["report_count"])
        self.assertEqual({"pass": 1, "warn": 1, "fail": 1, "unavailable": 0}, comparison_check["status_counts"])
        self.assertEqual(comparison_check["status_counts"], payload["summary"]["candidate_comparisons"]["status_counts"])
        self.assertCountEqual(
            ["candidate-pass", "candidate-warn", "candidate-fail"],
            [item["candidate_id"] for item in comparison_check["reports"]],
        )

    def test_candidate_comparison_reports_can_be_loaded_from_final_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            report_path = output_dir / "reports" / "manifest_report.json"
            report = _comparison_payload("candidate-warn", "warn")
            report["comparison_report"] = "reports/manifest_report.json"
            _write_json(report_path, report)
            _write_json(
                output_dir / "final_ai_improvement_artifact_manifest.json",
                {"comparison_reports": [{"path": "reports/manifest_report.json"}]},
            )

            payload = build_ai_improvement_quality_gate(output_dir)

        comparison_check = payload["checks"]["candidate_comparisons_ok"]
        self.assertEqual("warn", comparison_check["status"])
        self.assertEqual(1, comparison_check["report_count"])
        self.assertEqual(1, comparison_check["status_counts"]["warn"])
        self.assertEqual("candidate-warn", comparison_check["reports"][0]["candidate_id"])

    def test_manifest_absolute_in_output_path_can_match_relative_payload_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            report_path = output_dir / "reports" / "manifest_report.json"
            report = _comparison_payload("candidate-warn", "warn")
            report["comparison_report"] = "reports/manifest_report.json"
            _write_json(report_path, report)
            _write_json(
                output_dir / "final_ai_improvement_artifact_manifest.json",
                {"comparison_reports": [{"path": str(report_path)}]},
            )

            payload = build_ai_improvement_quality_gate(output_dir)

        comparison_check = payload["checks"]["candidate_comparisons_ok"]
        self.assertEqual("warn", comparison_check["status"])
        self.assertEqual(1, comparison_check["report_count"])
        self.assertEqual("loaded", comparison_check["reports"][0]["artifact_status"])

    def test_manifest_absolute_path_rejects_payload_basename_report(self) -> None:
        for payload_report in ("manifest_report.json", "ports/manifest_report.json"):
            with self.subTest(payload_report=payload_report):
                with tempfile.TemporaryDirectory() as temp_name:
                    output_dir = Path(temp_name)
                    report_path = output_dir / "reports" / "manifest_report.json"
                    report = _comparison_payload("candidate-warn", "warn")
                    report["comparison_report"] = payload_report
                    _write_json(report_path, report)
                    _write_json(
                        output_dir / "final_ai_improvement_artifact_manifest.json",
                        {"comparison_reports": [{"path": str(report_path), "candidate_id": "candidate-warn"}]},
                    )

                    payload = build_ai_improvement_quality_gate(output_dir)

                comparison_check = payload["checks"]["candidate_comparisons_ok"]
                self.assertEqual("unavailable", comparison_check["status"])
                self.assertEqual("manifest_comparison_mismatch", comparison_check["reports"][0]["artifact_status"])
                self.assertEqual(["comparison_report"], comparison_check["reports"][0]["mismatched_fields"])

    def test_manifest_comparison_mismatch_is_not_hidden_by_globbed_same_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            comparison = _comparison_payload("candidate-b", "pass")
            comparison["comparison_report"] = "missing_ball_recovery_comparison.json"
            _write_json(output_dir / "missing_ball_recovery_comparison.json", comparison)
            _write_json(
                output_dir / "final_ai_improvement_artifact_manifest.json",
                {
                    "comparison_reports": [
                        {
                            "path": "missing_ball_recovery_comparison.json",
                            "candidate_id": "candidate-a",
                            "problem_type": "missing_ball",
                        }
                    ]
                },
            )

            payload = build_ai_improvement_quality_gate(output_dir)

        comparison_check = payload["checks"]["candidate_comparisons_ok"]
        self.assertEqual("unavailable", comparison_check["status"])
        self.assertEqual(2, comparison_check["report_count"])
        self.assertEqual(1, comparison_check["status_counts"]["pass"])
        self.assertEqual(1, comparison_check["status_counts"]["unavailable"])
        mismatch_reports = [
            report
            for report in comparison_check["reports"]
            if report["artifact_status"] == "manifest_comparison_mismatch"
        ]
        self.assertEqual(1, len(mismatch_reports))
        self.assertEqual(["candidate_id"], mismatch_reports[0]["mismatched_fields"])

    def test_candidate_comparison_absence_is_backward_compatible_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)

            payload = build_ai_improvement_quality_gate(output_dir)

        comparison_check = payload["checks"]["candidate_comparisons_ok"]
        self.assertEqual("pass", comparison_check["status"])
        self.assertEqual(0, comparison_check["report_count"])
        self.assertEqual("No candidate comparison reports found", comparison_check["reason"])

    def test_candidate_comparison_summary_mismatch_is_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            payload = _comparison_payload("candidate-lie", "pass")
            payload["checks"] = [{"name": "camera_regression", "status": "fail"}]
            _write_json(output_dir / "follow_cam_comparison.json", payload)

            gate = build_ai_improvement_quality_gate(output_dir)

        comparison_check = gate["checks"]["candidate_comparisons_ok"]
        self.assertEqual("fail", comparison_check["status"])
        self.assertEqual("fail", comparison_check["reports"][0]["status"])
        self.assertEqual("summary_check_mismatch", comparison_check["reports"][0]["artifact_status"])

    def test_candidate_comparison_invalid_summary_does_not_downgrade_failing_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            payload = _comparison_payload("candidate-invalid-summary", "pass")
            payload["summary"]["status"] = "mystery"
            payload["checks"] = [{"name": "camera_regression", "status": "fail"}]
            _write_json(output_dir / "follow_cam_comparison.json", payload)

            gate = build_ai_improvement_quality_gate(output_dir)

        comparison_check = gate["checks"]["candidate_comparisons_ok"]
        self.assertEqual("fail", comparison_check["status"])
        self.assertEqual("fail", comparison_check["reports"][0]["status"])
        self.assertEqual("invalid_summary", comparison_check["reports"][0]["artifact_status"])

    def test_candidate_comparison_empty_checks_are_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            payload = _comparison_payload("candidate-empty", "pass")
            payload["checks"] = []
            _write_json(output_dir / "missing_ball_comparison.json", payload)

            gate = build_ai_improvement_quality_gate(output_dir)

        comparison_check = gate["checks"]["candidate_comparisons_ok"]
        self.assertEqual("unavailable", comparison_check["reports"][0]["status"])
        self.assertEqual("invalid_checks", comparison_check["reports"][0]["artifact_status"])

    def test_candidate_comparison_unavailable_check_takes_precedence_over_warn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            payload = _comparison_payload("candidate-mixed", "unavailable")
            payload["checks"] = [
                {"name": "precision_budget", "status": "warn"},
                {"name": "false_positive_count_missing", "status": "unavailable"},
            ]
            _write_json(output_dir / "noise_comparison.json", payload)

            gate = build_ai_improvement_quality_gate(output_dir)

        comparison_check = gate["checks"]["candidate_comparisons_ok"]
        self.assertEqual("unavailable", comparison_check["status"])
        self.assertEqual("unavailable", comparison_check["reports"][0]["status"])

    def test_candidate_comparison_aggregate_unavailable_takes_precedence_over_warn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(output_dir / "noise_comparison.json", _comparison_payload("candidate-warn", "warn"))
            _write_json(
                output_dir / "highlight_comparison.json",
                _comparison_payload("candidate-unavailable", "unavailable"),
            )

            gate = build_ai_improvement_quality_gate(output_dir)

        comparison_check = gate["checks"]["candidate_comparisons_ok"]
        self.assertEqual("unavailable", comparison_check["status"])
        self.assertEqual(1, comparison_check["status_counts"]["warn"])
        self.assertEqual(1, comparison_check["status_counts"]["unavailable"])

    def test_candidate_comparison_manifest_path_must_stay_under_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            outside_path = output_dir.parent / "outside_comparison.json"
            _write_json(outside_path, _comparison_payload("candidate-outside", "pass"))
            _write_json(
                output_dir / "final_ai_improvement_artifact_manifest.json",
                {"comparison_reports": [{"path": str(outside_path)}]},
            )

            gate = build_ai_improvement_quality_gate(output_dir)

        comparison_check = gate["checks"]["candidate_comparisons_ok"]
        self.assertEqual("unavailable", comparison_check["reports"][0]["status"])
        self.assertEqual("path_outside_output_dir", comparison_check["reports"][0]["artifact_status"])

    def test_candidate_comparison_manifest_missing_report_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "final_ai_improvement_artifact_manifest.json",
                {"comparison_reports": [{"path": "reports/missing_comparison.json", "candidate_id": "candidate-missing"}]},
            )

            gate = build_ai_improvement_quality_gate(output_dir)

        comparison_check = gate["checks"]["candidate_comparisons_ok"]
        self.assertEqual("unavailable", comparison_check["status"])
        self.assertEqual("candidate-missing", comparison_check["reports"][0]["candidate_id"])
        self.assertEqual("missing", comparison_check["reports"][0]["artifact_status"])

    def test_finalized_missing_ball_and_noise_selections_remain_visible_to_quality_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            missing_dir = output_dir / "ai_candidates" / "missing_ball" / "candidate-missing"
            noise_dir = output_dir / "ai_candidates" / "noise" / "candidate-noise"
            missing_dir.mkdir(parents=True)
            noise_dir.mkdir(parents=True)
            (missing_dir / "ball_track.csv").write_text("Frame,X,Y,Status\n1,10,20,Detected\n", encoding="utf-8")
            (noise_dir / "ball_track.cleaned.csv").write_text("Frame,X,Y,Status\n1,10,20,Detected\n", encoding="utf-8")
            missing_comparison = _comparison_payload("candidate-missing", "pass")
            missing_comparison["problem_type"] = "missing_ball"
            missing_comparison["path"] = "ai_candidates/missing_ball/candidate-missing/missing_ball_recovery_comparison.json"
            missing_comparison["comparison_report"] = missing_comparison["path"]
            missing_comparison["approval_id"] = "approval-missing"
            missing_comparison["consumed_approval_ids"] = ["approval-missing"]
            missing_comparison["candidate_artifacts"] = ["ai_candidates/missing_ball/candidate-missing/ball_track.csv"]
            noise_comparison = _comparison_payload("candidate-noise", "pass")
            noise_comparison["problem_type"] = "noise"
            noise_comparison["path"] = "ai_candidates/noise/candidate-noise/noise_candidate_comparison.json"
            noise_comparison["comparison_report"] = noise_comparison["path"]
            noise_comparison["approval_id"] = "approval-noise"
            noise_comparison["consumed_approval_ids"] = ["approval-noise"]
            noise_comparison["candidate_artifacts"] = ["ai_candidates/noise/candidate-noise/ball_track.cleaned.csv"]
            _write_json(missing_dir / "missing_ball_recovery_comparison.json", missing_comparison)
            _write_json(noise_dir / "noise_candidate_comparison.json", noise_comparison)
            write_final_artifact_manifest(
                output_dir,
                baseline_output={"path": "ball_track.csv"},
                candidate_outputs=[
                    {
                        "id": "candidate-missing",
                        "candidate_id": "candidate-missing",
                        "problem_type": "missing_ball",
                        "path": "ai_candidates/missing_ball/candidate-missing",
                        "candidate_artifacts": ["ai_candidates/missing_ball/candidate-missing/ball_track.csv"],
                    },
                    {
                        "id": "candidate-noise",
                        "candidate_id": "candidate-noise",
                        "problem_type": "noise",
                        "path": "ai_candidates/noise/candidate-noise",
                        "candidate_artifacts": ["ai_candidates/noise/candidate-noise/ball_track.cleaned.csv"],
                    },
                ],
                final_artifacts=[],
                consumed_approvals=[
                    {"approval_id": "approval-missing", "candidate_id": "candidate-missing"},
                    {"approval_id": "approval-noise", "candidate_id": "candidate-noise"},
                ],
                comparison_reports=[missing_comparison, noise_comparison],
            )

            finalize_ai_candidate(output_dir, problem_type="missing_ball", candidate_id="candidate-missing", approval_id="approval-missing", decision="promote", output_role="missing_ball_track")
            finalize_ai_candidate(output_dir, problem_type="noise", candidate_id="candidate-noise", approval_id="approval-noise", decision="promote", output_role="noise_cleaned_track")
            payload = build_ai_improvement_quality_gate(output_dir)

        comparison_check = payload["checks"]["candidate_comparisons_ok"]
        self.assertEqual("pass", comparison_check["status"])
        self.assertEqual(2, comparison_check["report_count"])
        self.assertEqual(["candidate-missing", "candidate-noise"], [item["candidate_id"] for item in comparison_check["reports"]])

    def test_cli_returns_nonzero_when_quality_gate_fails(self) -> None:
        from scripts.run_ai_improvement_quality_gate import main

        with tempfile.TemporaryDirectory() as temp_name:
            exit_code = main(["--output-dir", temp_name, "--mode", "real"])

        self.assertEqual(1, exit_code)

    def test_cli_rejects_missing_output_directory(self) -> None:
        from scripts.run_ai_improvement_quality_gate import main

        with tempfile.TemporaryDirectory() as temp_name:
            missing_dir = Path(temp_name) / "missing"

            with self.assertRaises(SystemExit) as raised:
                main(["--output-dir", str(missing_dir)])

        self.assertEqual(2, raised.exception.code)

    def test_cli_accepts_inline_approved_actions_json(self) -> None:
        from scripts.run_ai_improvement_quality_gate import main

        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_2079_gap(output_dir)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544)
            _write_ai_report(output_dir, [_missing_ball_improvement(start=2049, end=2544)])

            with patch("builtins.print"):
                exit_code = main(
                    [
                        "--output-dir",
                        str(output_dir),
                        "--approved-actions",
                        json.dumps({"approved_actions": [_targeted_rerun_approval(start=2049, end=2544)]}),
                    ]
                )

            payload = json.loads((output_dir / "ai_improvement_quality_gate.json").read_text(encoding="utf-8"))

        self.assertEqual(0, exit_code)
        self.assertEqual("pass", payload["checks"]["approved_actions_explicitly_consumed"]["status"])
        self.assertEqual("pass", payload["checks"]["long_lost_gap_improvement_coverage"]["status"])


def _write_real_required_without_models(output_dir: Path) -> None:
    _write_tracks(output_dir)
    write_track_hash_snapshot(output_dir, "before_review")
    write_track_hash_snapshot(output_dir, "after_ai_improvement")
    _write_json(output_dir / "ball_audit.json", {"review_events": []})
    _write_json(output_dir / "review_packets.json", {"packets": []})
    _write_json(output_dir / "ai_improvement_report.json", {"summary": {"status": "ok"}, "improvements": []})


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


def _write_ai_report(output_dir: Path, improvements: list[dict[str, object]]) -> None:
    _write_json(
        output_dir / "ai_improvement_report.json",
        {
            "schema_version": "1.0",
            "summary": {"status": "needs_rerun" if improvements else "ok"},
            "model": "gpt-improve",
            "model_selection_source": "explicit",
            "improvements": improvements,
            "highlight_adjustments": [],
        },
    )


def _missing_ball_improvement(*, start: int, end: int) -> dict[str, object]:
    return {
        "id": "imp_2079",
        "area": "tracking",
        "failure_tags": ["ball_lost"],
        "recommended_action": "targeted_rerun",
        "start_frame": start,
        "end_frame": end,
        "rerun_scope": {"start_frame": start, "end_frame": end},
        "source_packet_id": "packet_2079",
    }


def _not_visible_improvement(
    *,
    start: int,
    end: int,
    status: str = "ok",
    source_packet_id: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "id": "imp_not_visible",
        "area": "tracking",
        "failure_tags": ["ball_not_visible"],
        "recommended_action": "localize_ball_roi",
        "start_frame": start,
        "end_frame": end,
        "likely_ball_region": {"description": "not_visible", "status": status},
    }
    if source_packet_id is not None:
        result["source_packet_id"] = source_packet_id
    return result


def _targeted_rerun_approval(*, start: int, end: int) -> dict[str, object]:
    return {
        "approval_id": "approval_2079",
        "improvement_id": "imp_2079",
        "approved_action": "targeted_rerun",
        "rerun_scope": {"start_frame": start, "end_frame": end},
        "provenance": {"source_packet_id": "packet_2079"},
    }


def _write_approved_actions(output_dir: Path, actions: list[dict[str, object]]) -> Path:
    path = output_dir / "ai_improvement_approved_actions.json"
    _write_json(path, {"approved_actions": actions})
    return path


def _write_missing_ball_resolution(
    output_dir: Path,
    *,
    start: int,
    end: int,
    source_packet_id: str,
    approval_id: str = "noop_2079",
    candidate_id: str = "resolved_2079",
) -> None:
    _write_json(
        output_dir / "missing_ball_resolution.json",
        {
            "schema_version": "1.0",
            "summary": {
                "status": "resolved_not_visible",
                "resolution_count": 1,
                "consumed_approval_ids": [approval_id],
            },
            "resolutions": [
                {
                    "candidate_id": candidate_id,
                    "approval_id": approval_id,
                    "problem_type": "missing_ball",
                    "status": "resolved_not_visible",
                    "start_frame": start,
                    "end_frame": end,
                    "source_packet_id": source_packet_id,
                    "likely_ball_region": {"description": "not_visible"},
                    "evidence": [{"source_packet_id": source_packet_id, "reason": "packet marks not_visible"}],
                }
            ],
        },
    )


def _write_camera_audit(output_dir: Path, *, review_events: int, max_pan: float, p95_pan: float) -> None:
    _write_json(
        output_dir / "camera_motion_audit.json",
        {
            "summary": {
                "review_event_count": review_events,
                "max_pan_step_px": max_pan,
                "p95_pan_step_px": p95_pan,
            }
        },
    )


def _write_event_candidate(
    output_dir: Path,
    *,
    candidate_id: str,
    core_end: int,
    min_tail: int,
    source_frames: int,
) -> None:
    _write_json(
        output_dir / "event_candidates.json",
        {
            "summary": {"total_source_frames": source_frames},
            "candidates": [
                {
                    "id": candidate_id,
                    "core_window": {"start_frame": core_end - 10, "end_frame": core_end},
                    "render_window": {"start_frame": core_end - 20, "end_frame": min(source_frames - 1, core_end + min_tail)},
                    "buffer_policy": {"min_tail_frames": min_tail},
                }
            ],
        },
    )


def _comparison_payload(candidate_id: str, status: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "problem_type": "missing_ball",
        "candidate": {"id": candidate_id, "role": "candidate", "path": f"candidate/{candidate_id}.json"},
        "summary": {
            "status": status,
            "check_count": 1,
            "failed_check_count": 1 if status == "fail" else 0,
            "warning_count": 1 if status == "warn" else 0,
            "unavailable_count": 1 if status == "unavailable" else 0,
        },
        "checks": [{"name": "comparison", "status": status}],
    }


def _write_tracks(output_dir: Path, *, raw: str = "Frame,X,Y,Status\n1,10,20,Detected\n") -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "ball_track.csv").write_text(raw, encoding="utf-8")
    (output_dir / "ball_track.cleaned.csv").write_text(raw, encoding="utf-8")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
