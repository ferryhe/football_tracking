from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from football_tracking.ball_audit import write_ball_audit_report
from football_tracking.missing_ball_recovery_comparison import (
    build_missing_ball_recovery_comparison,
    write_missing_ball_recovery_comparison,
)

FIXED_NOW = "2026-06-23T00:00:00+00:00"


class MissingBallRecoveryComparisonTests(unittest.TestCase):
    def test_candidate_passes_when_sustained_lost_gap_is_recovered_without_noise_islands(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            _write_track(baseline, lost_ranges=[(10, 60)])
            _write_track(candidate, lost_ranges=[(10, 20)])

            with patch("football_tracking.ai_candidate_comparison._utc_now_iso", return_value=FIXED_NOW):
                report = build_missing_ball_recovery_comparison(
                    baseline,
                    candidate,
                    candidate_id="candidate-pass",
                    approval=_targeted_approval(),
                    target_window={"start_frame": 10, "end_frame": 60},
                )

        self.assertEqual("missing_ball", report["problem_type"])
        self.assertEqual("pass", report["summary"]["status"])
        self.assertTrue(report["promotion_eligible"])
        self.assertFalse(report["requires_human_confirmation"])
        self.assertEqual(51, report["metrics"]["baseline_lost_frames"])
        self.assertEqual(11, report["metrics"]["candidate_lost_frames"])
        self.assertEqual(40, report["metrics"]["sustained_recovered_frames"])
        self.assertEqual(0, report["metrics"]["new_short_false_positive_islands"])

    def test_candidate_fails_when_recovery_is_only_short_noisy_islands(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            _write_track(baseline, lost_ranges=[(10, 60)])
            _write_track(candidate, lost_ranges=[(10, 20), (24, 60)])

            report = build_missing_ball_recovery_comparison(
                    baseline,
                    candidate,
                    candidate_id="candidate-noisy",
                    approval=_approval("candidate-noisy"),
                    target_window={"start_frame": 10, "end_frame": 60},
                )

        self.assertEqual("fail", report["summary"]["status"])
        self.assertFalse(report["promotion_eligible"])
        failed_checks = {check["name"] for check in report["checks"] if check["status"] == "fail"}
        self.assertIn("short_false_positive_islands", failed_checks)
        self.assertIn("sustained_recovered_frames", failed_checks)
        lost_gap_check = next(check for check in report["checks"] if check["name"] == "lost_gap_reduced")
        self.assertEqual("fail", lost_gap_check["status"])
        self.assertIn("not enough for sustained recovery", lost_gap_check["reason"])

    def test_localize_roi_stitch_report_allows_shorter_sustained_recovery_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            stitch_report = output_dir / "recovery_stitch_report.json"
            _write_track(baseline, lost_ranges=[(10, 31)])
            _write_localize_short_recovery(candidate, recovered_start=19, recovered_end=31)
            stitch_report.write_text(
                json.dumps(
                    {
                        "summary": {
                            "status": "pass",
                            "accepted_frame_count": 13,
                            "passed_count": 1,
                            "boundary_transition_warning_count": 1,
                        },
                        "attempts": [
                            {
                                "status": "pass",
                                "boundary_transition_warning": True,
                                "metrics": {
                                    "longest_roi_run_frames": 13,
                                    "candidate_lost_frames": 9,
                                    "baseline_lost_frames": 22,
                                    "accepted_frame_count": 13,
                                    "required_window_coverage": {"status": "pass", "uncovered_ranges": []},
                                    "roi_internal_quality": {"status": "pass", "outside_roi_ratio": 0.0, "max_step_px": 1.0},
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_missing_ball_recovery_comparison(
                baseline,
                candidate,
                candidate_id="candidate-short-roi",
                approval=_approval("candidate-short-roi"),
                target_window={"start_frame": 10, "end_frame": 31},
                recovery_stitch_report_path=stitch_report,
            )

        self.assertEqual("pass", report["comparison_status"])
        self.assertTrue(report["promotion_eligible"])
        sustained_check = next(check for check in report["checks"] if check["name"] == "sustained_recovered_frames")
        self.assertEqual(13, sustained_check["candidate_value"])
        self.assertEqual(12, sustained_check["minimum_value"])
        stitch_check = next(check for check in report["checks"] if check["name"] == "roi_stitch_recovery")
        self.assertEqual("pass", stitch_check["status"])

    def test_short_localize_recovery_without_stitch_report_keeps_default_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            _write_track(baseline, lost_ranges=[(10, 31)])
            _write_localize_short_recovery(candidate, recovered_start=19, recovered_end=31)

            report = build_missing_ball_recovery_comparison(
                baseline,
                candidate,
                candidate_id="candidate-short-no-stitch",
                approval=_approval("candidate-short-no-stitch"),
                target_window={"start_frame": 10, "end_frame": 31},
            )

        failed_checks = {check["name"] for check in report["checks"] if check["status"] == "fail"}
        self.assertIn("sustained_recovered_frames", failed_checks)

    def test_partial_localize_stitch_does_not_close_full_target_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            stitch_report = output_dir / "recovery_stitch_report.json"
            _write_track(baseline, lost_ranges=[(2049, 2544)], frame_count=2600)
            _write_track(candidate, lost_ranges=[(2049, 2078), (2301, 2544)], frame_count=2600)
            _write_json(
                stitch_report,
                {
                    "schema_version": "1.0",
                    "summary": {"status": "pass", "accepted_frame_count": 222},
                    "metrics": {
                        "status": "pass",
                        "accepted_frame_count": 222,
                        "accepted_frame_ranges": [{"start_frame": 2079, "end_frame": 2300, "frame_count": 222}],
                        "required_window_coverage": {
                            "status": "fail",
                            "uncovered_ranges": [
                                {"start_frame": 2049, "end_frame": 2078, "frame_count": 30},
                                {"start_frame": 2301, "end_frame": 2544, "frame_count": 244},
                            ],
                        },
                        "roi_internal_quality": {"status": "pass", "outside_roi_ratio": 0.0, "max_step_px": 1.0},
                    },
                },
            )

            report = build_missing_ball_recovery_comparison(
                baseline,
                candidate,
                candidate_id="candidate-partial-localize",
                approval={**_approval("candidate-partial-localize"), "start_frame": 2049, "end_frame": 2544},
                target_window={"start_frame": 2049, "end_frame": 2544},
                recovery_stitch_report_path=stitch_report,
            )

        self.assertEqual("fail", report["comparison_status"])
        roi_stitch_check = next(check for check in report["checks"] if check["name"] == "roi_stitch_recovery")
        self.assertEqual("fail", roi_stitch_check["status"])
        self.assertEqual(
            [
                {"start_frame": 2049, "end_frame": 2078, "frame_count": 30},
                {"start_frame": 2301, "end_frame": 2544, "frame_count": 244},
            ],
            roi_stitch_check["required_window_coverage"]["uncovered_ranges"],
        )

    def test_aggregate_localize_stitch_windows_do_not_hide_gap_between_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            stitch_report = output_dir / "recovery_stitch_report.json"
            _write_track(baseline, lost_ranges=[(10, 25)])
            _write_track(candidate, lost_ranges=[])
            _write_json(
                stitch_report,
                {
                    "schema_version": "1.0",
                    "summary": {"status": "pass", "accepted_frame_count": 12},
                    "attempts": [
                        {
                            "status": "pass",
                            "metrics": {
                                "status": "pass",
                                "accepted_frame_count": 6,
                                "accepted_frame_ranges": [{"start_frame": 10, "end_frame": 15, "frame_count": 6}],
                                "required_window_coverage": {"status": "pass", "uncovered_ranges": []},
                                "roi_internal_quality": {"status": "pass", "outside_roi_ratio": 0.0, "max_step_px": 1.0},
                            },
                        },
                        {
                            "status": "pass",
                            "metrics": {
                                "status": "pass",
                                "accepted_frame_count": 6,
                                "accepted_frame_ranges": [{"start_frame": 20, "end_frame": 25, "frame_count": 6}],
                                "required_window_coverage": {"status": "pass", "uncovered_ranges": []},
                                "roi_internal_quality": {"status": "pass", "outside_roi_ratio": 0.0, "max_step_px": 1.0},
                            },
                        },
                    ],
                },
            )

            report = build_missing_ball_recovery_comparison(
                baseline,
                candidate,
                candidate_id="candidate-aggregate-gap",
                approval={**_approval("candidate-aggregate-gap"), "start_frame": 10, "end_frame": 25},
                target_window={"start_frame": 10, "end_frame": 25},
                recovery_stitch_report_path=stitch_report,
            )

        self.assertEqual("fail", report["comparison_status"])
        roi_stitch_check = next(check for check in report["checks"] if check["name"] == "roi_stitch_recovery")
        self.assertEqual([{"start_frame": 16, "end_frame": 19, "frame_count": 4}], roi_stitch_check["required_window_coverage"]["uncovered_ranges"])

    def test_candidate_fails_when_gap_is_filled_only_with_predictions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            _write_track(baseline, lost_ranges=[(10, 60)])
            _write_track(candidate, lost_ranges=[], predicted_ranges=[(10, 60)])

            report = build_missing_ball_recovery_comparison(
                baseline,
                candidate,
                candidate_id="candidate-predicted-only",
                approval=_approval("candidate-predicted-only"),
                target_window={"start_frame": 10, "end_frame": 60},
            )

        self.assertEqual("fail", report["summary"]["status"])
        self.assertEqual(51, report["metrics"]["candidate_lost_frames"])
        failed_checks = {check["name"] for check in report["checks"] if check["status"] == "fail"}
        self.assertIn("sustained_recovered_frames", failed_checks)

    def test_candidate_missing_frames_fail_frame_coverage_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            _write_track(baseline, lost_ranges=[(10, 60)])
            _write_sparse_track(candidate, detected_frames=range(10, 34))

            report = build_missing_ball_recovery_comparison(
                baseline,
                candidate,
                candidate_id="candidate-missing-frames",
                approval=_approval("candidate-missing-frames"),
                target_window={"start_frame": 10, "end_frame": 60},
            )

        self.assertEqual("fail", report["summary"]["status"])
        self.assertEqual(27, report["metrics"]["candidate_missing_frames"])
        failed_checks = {check["name"] for check in report["checks"] if check["status"] == "fail"}
        self.assertIn("candidate_frame_coverage", failed_checks)

    def test_candidate_fails_when_reaudit_reports_large_jump_in_target_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "ball_track.csv"
            _write_track(baseline, lost_ranges=[(10, 60)])
            _write_track_with_jump(candidate, jump_frame=35)
            audit = write_ball_audit_report(output_dir)

            report = build_missing_ball_recovery_comparison(
                baseline,
                candidate,
                candidate_id="candidate-jump",
                approval=_approval("candidate-jump"),
                target_window={"start_frame": 10, "end_frame": 60},
                candidate_audit_path=output_dir / "ball_audit.json",
                require_candidate_audit=True,
            )

        self.assertTrue(any(event["type"] == "large_jump" for event in audit["review_events"]))
        self.assertEqual("fail", report["summary"]["status"])
        failed_checks = {check["name"] for check in report["checks"] if check["status"] == "fail"}
        self.assertIn("candidate_reaudit", failed_checks)

    def test_candidate_fails_when_localize_roi_does_not_match_recovered_point(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            _write_track(baseline, lost_ranges=[(10, 60)])
            _write_track(candidate, lost_ranges=[(10, 20)])
            approval = {
                **_approval("candidate-outside-roi"),
                "local_search_roi": {
                    "coordinate_space": "image",
                    "frame": 30,
                    "x": 1000,
                    "y": 1000,
                    "width": 40,
                    "height": 40,
                },
            }

            report = build_missing_ball_recovery_comparison(
                baseline,
                candidate,
                candidate_id="candidate-outside-roi",
                approval=approval,
                target_window={"start_frame": 10, "end_frame": 60},
            )

        self.assertEqual("fail", report["summary"]["status"])
        failed_checks = {check["name"] for check in report["checks"] if check["status"] == "fail"}
        self.assertIn("localize_roi_plausibility", failed_checks)

    def test_candidate_fails_when_sustained_recovery_leaves_localize_roi(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            _write_track(baseline, lost_ranges=[(10, 60)])
            _write_track_with_roi_escape(candidate)
            approval = {
                **_approval("candidate-stable-wrong-target"),
                "start_frame": 10,
                "end_frame": 60,
                "local_search_roi": {
                    "coordinate_space": "image",
                    "frame": 30,
                    "x": 120,
                    "y": 220,
                    "width": 60,
                    "height": 60,
                },
            }

            report = build_missing_ball_recovery_comparison(
                baseline,
                candidate,
                candidate_id="candidate-stable-wrong-target",
                approval=approval,
                target_window={"start_frame": 10, "end_frame": 60},
            )

        self.assertEqual("fail", report["summary"]["status"])
        failed_checks = {check["name"] for check in report["checks"] if check["status"] == "fail"}
        self.assertIn("localize_roi_plausibility", failed_checks)

    def test_candidate_checks_related_localize_approvals_not_only_first_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            _write_track(baseline, lost_ranges=[(10, 60)])
            _write_track(candidate, lost_ranges=[(10, 20)])
            approval = {
                "approval_id": "approval_targeted",
                "approved_action": "targeted_rerun",
                "candidate_id": "candidate-related-roi",
                "source_packet_id": "packet_001",
                "related_approvals": [
                    {
                        "approval_id": "approval_targeted",
                        "approved_action": "targeted_rerun",
                        "candidate_id": "candidate-related-roi",
                        "source_packet_id": "packet_001",
                    },
                    {
                        **_approval("candidate-related-roi"),
                        "approval_id": "approval_localize",
                        "local_search_roi": {
                            "coordinate_space": "image",
                            "frame": 30,
                            "x": 1000,
                            "y": 1000,
                            "width": 40,
                            "height": 40,
                        },
                    },
                ],
            }

            report = build_missing_ball_recovery_comparison(
                baseline,
                candidate,
                candidate_id="candidate-related-roi",
                approval=approval,
                target_window={"start_frame": 10, "end_frame": 60},
            )

        self.assertEqual("fail", report["summary"]["status"])
        failed_checks = {check["name"] for check in report["checks"] if check["status"] == "fail"}
        self.assertIn("localize_roi_plausibility", failed_checks)

    def test_localize_roi_plausibility_uses_effective_roi_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            _write_track(baseline, lost_ranges=[(10, 60)])
            _write_track(candidate, lost_ranges=[(10, 20)])
            approval = {
                **_approval("candidate-effective-roi"),
                "start_frame": 10,
                "end_frame": 60,
                "local_search_roi": {
                    "coordinate_space": "image",
                    "frame": 30,
                    "x": 100,
                    "y": 200,
                    "width": 60,
                    "height": 60,
                },
                "effective_roi": [140, 240, 180, 280],
            }

            report = build_missing_ball_recovery_comparison(
                baseline,
                candidate,
                candidate_id="candidate-effective-roi",
                approval=approval,
                target_window={"start_frame": 10, "end_frame": 60},
            )

        self.assertEqual("fail", report["summary"]["status"])
        roi_check = next(check for check in report["checks"] if check["name"] == "localize_roi_plausibility")
        self.assertEqual("fail", roi_check["status"])
        self.assertEqual({"x": 140.0, "y": 240.0, "right": 180.0, "bottom": 280.0}, roi_check["results"][0]["roi"])

    def test_candidate_reaudit_can_be_required(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            _write_track(baseline, lost_ranges=[(10, 60)])
            _write_track(candidate, lost_ranges=[(10, 20)])

            report = build_missing_ball_recovery_comparison(
                baseline,
                candidate,
                candidate_id="candidate-needs-audit",
                approval=_targeted_approval("candidate-needs-audit"),
                target_window={"start_frame": 10, "end_frame": 60},
                candidate_audit_path=output_dir / "missing_ball_audit.json",
                require_candidate_audit=True,
            )

        self.assertEqual("unavailable", report["summary"]["status"])
        unavailable_checks = {check["name"] for check in report["checks"] if check["status"] == "unavailable"}
        self.assertIn("candidate_reaudit", unavailable_checks)

    def test_candidate_fails_when_packet_evidence_does_not_cover_recovery_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            review_packets = output_dir / "review_packets.json"
            _write_track(baseline, lost_ranges=[(10, 60)])
            _write_track(candidate, lost_ranges=[(10, 20)])
            review_packets.write_text(
                json.dumps({"packets": [{"packet_id": "packet_001", "start_frame": 0, "end_frame": 5}]}),
                encoding="utf-8",
            )

            report = build_missing_ball_recovery_comparison(
                baseline,
                candidate,
                candidate_id="candidate-packet-mismatch",
                approval={**_approval("candidate-packet-mismatch"), "start_frame": 10, "end_frame": 60},
                target_window={"start_frame": 10, "end_frame": 60},
                review_packets_path=review_packets,
                require_packet_coverage=True,
            )

        self.assertEqual("fail", report["summary"]["status"])
        failed_checks = {check["name"] for check in report["checks"] if check["status"] == "fail"}
        self.assertIn("packet_evidence_coverage", failed_checks)

    def test_packet_coverage_reads_nested_packet_window_and_requires_full_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            review_packets = output_dir / "review_packets.json"
            _write_track(baseline, lost_ranges=[(10, 60)])
            _write_track(candidate, lost_ranges=[(10, 20)])
            review_packets.write_text(
                json.dumps({"packets": [{"packet_id": "packet_001", "window": {"start_frame": 10, "end_frame": 60}}]}),
                encoding="utf-8",
            )

            report = build_missing_ball_recovery_comparison(
                baseline,
                candidate,
                candidate_id="candidate-packet-window",
                approval={**_approval("candidate-packet-window"), "start_frame": 10, "end_frame": 60},
                target_window={"start_frame": 10, "end_frame": 60},
                review_packets_path=review_packets,
                require_packet_coverage=True,
            )

        check = next(item for item in report["checks"] if item["name"] == "packet_evidence_coverage")
        self.assertEqual("pass", check["status"])
        self.assertIn("fully covers", check["reason"])

    def test_packet_coverage_rejects_partial_overlap_for_targeted_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            review_packets = output_dir / "review_packets.json"
            _write_track(baseline, lost_ranges=[(10, 60)])
            _write_track(candidate, lost_ranges=[(10, 20)])
            review_packets.write_text(
                json.dumps({"packets": [{"packet_id": "packet_001", "window": {"start_frame": 10, "end_frame": 20}}]}),
                encoding="utf-8",
            )

            report = build_missing_ball_recovery_comparison(
                baseline,
                candidate,
                candidate_id="candidate-partial-packet",
                approval={
                    "approval_id": "approval_001",
                    "approved_action": "targeted_rerun",
                    "candidate_id": "candidate-partial-packet",
                    "source_packet_id": "packet_001",
                    "rerun_scope": {"start_frame": 10, "end_frame": 60},
                },
                target_window={"start_frame": 10, "end_frame": 60},
                review_packets_path=review_packets,
                require_packet_coverage=True,
            )

        self.assertEqual("fail", report["summary"]["status"])
        failed_checks = {check["name"] for check in report["checks"] if check["status"] == "fail"}
        self.assertIn("packet_evidence_coverage", failed_checks)

    def test_packet_coverage_rejects_partial_overlap_for_localize_ball_roi(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            review_packets = output_dir / "review_packets.json"
            _write_track(baseline, lost_ranges=[(10, 60)])
            _write_track(candidate, lost_ranges=[(10, 20)])
            review_packets.write_text(
                json.dumps({"packets": [{"packet_id": "packet_001", "window": {"start_frame": 30, "end_frame": 30}}]}),
                encoding="utf-8",
            )

            report = build_missing_ball_recovery_comparison(
                baseline,
                candidate,
                candidate_id="candidate-localize-partial-packet",
                approval={**_approval("candidate-localize-partial-packet"), "start_frame": 10, "end_frame": 60},
                target_window={"start_frame": 10, "end_frame": 60},
                review_packets_path=review_packets,
                require_packet_coverage=True,
            )

        self.assertEqual("fail", report["summary"]["status"])
        failed_checks = {check["name"] for check in report["checks"] if check["status"] == "fail"}
        self.assertIn("packet_evidence_coverage", failed_checks)

    def test_2079_fixture_requires_start_middle_end_tail_packet_labels(self) -> None:
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "right_bottom_gap_2049_2544.json").read_text(encoding="utf-8")
        )
        gap = fixture["lost_gap"]
        required_labels = fixture["required_packet_coverage_labels"]
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            review_packets = output_dir / "review_packets.json"
            _write_track(baseline, lost_ranges=[(gap["start_frame"], gap["end_frame"])], frame_count=2600)
            _write_track(candidate, lost_ranges=[], frame_count=2600)
            review_packets.write_text(
                json.dumps(
                    {
                        "packets": [
                            _packet("packet_start", "start", 2049, 2099),
                            _packet("packet_middle", "middle", 2100, 2479),
                            _packet("packet_end", "end", 2480, 2520),
                            _packet("packet_tail", "tail", 2521, 2544),
                        ]
                    }
                ),
                encoding="utf-8",
            )
            approval = {
                "approval_id": "approval_2079",
                "approved_action": "targeted_rerun",
                "candidate_id": "candidate-2079",
                "source_packet_id": "packet_start",
                "rerun_scope": gap,
                "required_packet_coverage_labels": required_labels,
                "related_approvals": [
                    {
                        "approval_id": f"approval_2079_{label}",
                        "approved_action": "targeted_rerun",
                        "candidate_id": "candidate-2079",
                        "source_packet_id": f"packet_{label}",
                        "rerun_scope": gap,
                    }
                    for label in required_labels
                ],
            }

            report = build_missing_ball_recovery_comparison(
                baseline,
                candidate,
                candidate_id="candidate-2079",
                approval=approval,
                target_window=gap,
                review_packets_path=review_packets,
                require_packet_coverage=True,
            )

        packet_check = next(item for item in report["checks"] if item["name"] == "packet_evidence_coverage")
        self.assertEqual("pass", packet_check["status"], packet_check)
        self.assertEqual(required_labels, packet_check["required_labels"])
        self.assertEqual(required_labels, packet_check["covered_labels"])
        self.assertEqual([], packet_check["uncovered_ranges"])

    def test_packet_labels_from_related_approvals_count_toward_required_coverage(self) -> None:
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "right_bottom_gap_2049_2544.json").read_text(encoding="utf-8")
        )
        gap = fixture["lost_gap"]
        required_labels = fixture["required_packet_coverage_labels"]
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            review_packets = output_dir / "review_packets.json"
            _write_track(baseline, lost_ranges=[(gap["start_frame"], gap["end_frame"])], frame_count=2600)
            _write_track(candidate, lost_ranges=[], frame_count=2600)
            review_packets.write_text(
                json.dumps(
                    {
                        "packets": [
                            _packet("packet_start", "start", 2049, 2099),
                            _packet("packet_middle", "middle", 2100, 2479),
                            _packet("packet_end", "end", 2480, 2520),
                            _packet("packet_tail", "tail", 2521, 2544),
                        ]
                    }
                ),
                encoding="utf-8",
            )
            approval = {
                "approval_id": "approval_2079",
                "approved_action": "targeted_rerun",
                "candidate_id": "candidate-2079",
                "source_packet_id": "packet_start",
                "rerun_scope": gap,
                "related_approvals": [
                    {
                        "approval_id": f"approval_2079_{label}",
                        "approved_action": "targeted_rerun",
                        "candidate_id": "candidate-2079",
                        "source_packet_id": f"packet_{label}",
                        "rerun_scope": gap,
                        "required_packet_coverage_labels": [label],
                    }
                    for label in required_labels
                ],
            }

            report = build_missing_ball_recovery_comparison(
                baseline,
                candidate,
                candidate_id="candidate-2079",
                approval=approval,
                target_window=gap,
                review_packets_path=review_packets,
                require_packet_coverage=True,
            )

        packet_check = next(item for item in report["checks"] if item["name"] == "packet_evidence_coverage")
        self.assertEqual("pass", packet_check["status"], packet_check)
        self.assertEqual(required_labels, packet_check["required_labels"])
        self.assertEqual(required_labels, packet_check["covered_labels"])
        self.assertEqual([], packet_check["uncovered_ranges"])

    def test_right_bottom_gap_packet_label_coverage_fails_when_ranges_are_uncovered(self) -> None:
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "right_bottom_gap_2049_2544.json").read_text(encoding="utf-8")
        )
        gap = fixture["lost_gap"]
        required_labels = fixture["required_packet_coverage_labels"]
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            review_packets = output_dir / "review_packets.json"
            _write_track(baseline, lost_ranges=[(gap["start_frame"], gap["end_frame"])], frame_count=2600)
            _write_track(candidate, lost_ranges=[], frame_count=2600)
            review_packets.write_text(
                json.dumps(
                    {
                        "packets": [
                            _packet("packet_start", "start", 2049, 2099),
                            _packet("packet_middle", "middle", 2200, 2250),
                            _packet("packet_end", "end", 2480, 2520),
                            _packet("packet_tail", "tail", 2521, 2544),
                            {
                                "packet_id": "packet_unlabeled_full",
                                "source": {"kind": "test", "start_frame": 2049, "end_frame": 2544},
                                "window": {"start_frame": 2049, "end_frame": 2544},
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            approval = {
                "approval_id": "approval_2079",
                "approved_action": "targeted_rerun",
                "candidate_id": "candidate-2079",
                "source_packet_id": "packet_start",
                "rerun_scope": gap,
                "required_packet_coverage_labels": required_labels,
                "related_approvals": [
                    {
                        "approval_id": f"approval_2079_{label}",
                        "approved_action": "targeted_rerun",
                        "candidate_id": "candidate-2079",
                        "source_packet_id": f"packet_{label}",
                        "rerun_scope": gap,
                    }
                    for label in required_labels
                ]
                + [
                    {
                        "approval_id": "approval_2079_unlabeled_full",
                        "approved_action": "targeted_rerun",
                        "candidate_id": "candidate-2079",
                        "source_packet_id": "packet_unlabeled_full",
                        "rerun_scope": gap,
                    }
                ],
            }

            report = build_missing_ball_recovery_comparison(
                baseline,
                candidate,
                candidate_id="candidate-2079",
                approval=approval,
                target_window=gap,
                review_packets_path=review_packets,
                require_packet_coverage=True,
            )

        packet_check = next(item for item in report["checks"] if item["name"] == "packet_evidence_coverage")
        self.assertEqual("fail", packet_check["status"], packet_check)
        self.assertEqual(required_labels, packet_check["covered_labels"])
        ignored = [
            result
            for result in packet_check["results"]
            if result["packet_id"] == "packet_unlabeled_full"
        ]
        self.assertEqual(["ignored"], [result["status"] for result in ignored])
        self.assertEqual(
            [
                {"start_frame": 2100, "end_frame": 2199},
                {"start_frame": 2251, "end_frame": 2479},
            ],
            packet_check["uncovered_ranges"],
        )

    def test_packet_label_coverage_uses_combined_target_window_not_first_approval_window(self) -> None:
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "right_bottom_gap_2049_2544.json").read_text(encoding="utf-8")
        )
        gap = fixture["lost_gap"]
        required_labels = fixture["required_packet_coverage_labels"]
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            review_packets = output_dir / "review_packets.json"
            _write_track(baseline, lost_ranges=[(gap["start_frame"], gap["end_frame"])], frame_count=2600)
            _write_track(candidate, lost_ranges=[], frame_count=2600)
            review_packets.write_text(
                json.dumps(
                    {
                        "packets": [
                            _packet("packet_start", "start", 2049, 2099),
                            _packet("packet_middle", "middle", 2200, 2250),
                            _packet("packet_end", "end", 2480, 2520),
                            _packet("packet_tail", "tail", 2521, 2544),
                        ]
                    }
                ),
                encoding="utf-8",
            )
            approval = {
                "approval_id": "approval_2079_start",
                "approved_action": "targeted_rerun",
                "candidate_id": "candidate-2079",
                "source_packet_id": "packet_start",
                "rerun_scope": {"start_frame": 2049, "end_frame": 2099},
                "required_packet_coverage_labels": required_labels,
                "related_approvals": [
                    {
                        "approval_id": "approval_2079_start",
                        "approved_action": "targeted_rerun",
                        "candidate_id": "candidate-2079",
                        "source_packet_id": "packet_start",
                        "rerun_scope": {"start_frame": 2049, "end_frame": 2099},
                    },
                    {
                        "approval_id": "approval_2079_middle",
                        "approved_action": "targeted_rerun",
                        "candidate_id": "candidate-2079",
                        "source_packet_id": "packet_middle",
                        "rerun_scope": {"start_frame": 2200, "end_frame": 2250},
                    },
                    {
                        "approval_id": "approval_2079_end",
                        "approved_action": "targeted_rerun",
                        "candidate_id": "candidate-2079",
                        "source_packet_id": "packet_end",
                        "rerun_scope": {"start_frame": 2480, "end_frame": 2520},
                    },
                    {
                        "approval_id": "approval_2079_tail",
                        "approved_action": "targeted_rerun",
                        "candidate_id": "candidate-2079",
                        "source_packet_id": "packet_tail",
                        "rerun_scope": {"start_frame": 2521, "end_frame": 2544},
                    },
                ],
            }

            report = build_missing_ball_recovery_comparison(
                baseline,
                candidate,
                candidate_id="candidate-2079",
                approval=approval,
                target_window=gap,
                review_packets_path=review_packets,
                require_packet_coverage=True,
            )

        packet_check = next(item for item in report["checks"] if item["name"] == "packet_evidence_coverage")
        self.assertEqual("fail", packet_check["status"], packet_check)
        self.assertEqual(required_labels, packet_check["covered_labels"])
        self.assertEqual(
            [
                {"start_frame": 2100, "end_frame": 2199},
                {"start_frame": 2251, "end_frame": 2479},
            ],
            packet_check["uncovered_ranges"],
        )

    def test_uncertain_in_roi_localize_recovery_requires_human_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            _write_track(baseline, lost_ranges=[(10, 60)])
            _write_track(candidate, lost_ranges=[(10, 20)])
            approval = _approval("candidate-in-roi-uncertain")
            approval.pop("match_ball_confirmed")
            stitch_report = output_dir / "recovery_stitch_report.json"
            _write_pass_stitch_report(stitch_report, start=10, end=60, accepted_frame_count=40)

            report = build_missing_ball_recovery_comparison(
                baseline,
                candidate,
                candidate_id="candidate-in-roi-uncertain",
                approval=approval,
                target_window={"start_frame": 10, "end_frame": 60},
                recovery_stitch_report_path=stitch_report,
            )

        self.assertEqual("warn", report["summary"]["status"])
        self.assertTrue(report["requires_human_confirmation"])
        warning_checks = {check["name"] for check in report["checks"] if check["status"] == "warn"}
        self.assertIn("match_ball_confirmation", warning_checks)

    def test_localize_roi_stitch_evidence_can_pass_short_bounded_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            stitch_report = output_dir / "recovery_stitch_report.json"
            _write_track(baseline, lost_ranges=[(10, 21)])
            _write_track(candidate, lost_ranges=[])
            _write_json(
                stitch_report,
                {
                    "schema_version": "1.0",
                    "summary": {
                        "status": "pass",
                        "accepted_frame_count": 12,
                        "approval_id": "approval_001",
                        "start_frame": 10,
                        "end_frame": 21,
                    },
                    "metrics": {
                        "status": "pass",
                        "accepted_frame_count": 12,
                        "required_window": {"start_frame": 10, "end_frame": 21, "frame_count": 12},
                        "accepted_frame_ranges": [{"start_frame": 10, "end_frame": 21, "frame_count": 12}],
                        "required_window_coverage": {"status": "pass", "uncovered_ranges": []},
                        "roi_internal_quality": {"status": "pass", "outside_roi_ratio": 0.0, "max_step_px": 1.0},
                    },
                },
            )
            approval = {
                **_approval("candidate-short-localize"),
                "start_frame": 10,
                "end_frame": 21,
                "local_search_roi": {
                    "coordinate_space": "image",
                    "frame": 15,
                    "x": 100,
                    "y": 200,
                    "width": 60,
                    "height": 60,
                },
            }

            report = build_missing_ball_recovery_comparison(
                baseline,
                candidate,
                candidate_id="candidate-short-localize",
                approval=approval,
                target_window={"start_frame": 10, "end_frame": 21},
                recovery_stitch_report_path=stitch_report,
            )

        self.assertEqual("pass", report["summary"]["status"])
        self.assertEqual("pass", report["comparison_status"])
        roi_stitch_check = next(check for check in report["checks"] if check["name"] == "roi_stitch_recovery")
        sustained_check = next(check for check in report["checks"] if check["name"] == "sustained_recovered_frames")
        self.assertEqual("pass", roi_stitch_check["status"])
        self.assertEqual("pass", sustained_check["status"])
        self.assertEqual("recovery_stitch_report.json", report["metrics"]["roi_stitch_recovery"]["path"])

    def test_approval_linkage_requires_id_action_and_matching_candidate(self) -> None:
        cases = [
            ("missing approval id", {"approved_action": "localize_ball_roi", "source_packet_id": "packet_001"}),
            ("wrong action", {"approval_id": "approval_001", "approved_action": "adjust_follow_cam", "source_packet_id": "packet_001"}),
            (
                "missing candidate id",
                {
                    "approval_id": "approval_001",
                    "approved_action": "localize_ball_roi",
                    "source_packet_id": "packet_001",
                },
            ),
            (
                "candidate mismatch",
                {
                    "approval_id": "approval_001",
                    "approved_action": "localize_ball_roi",
                    "candidate_id": "other-candidate",
                    "source_packet_id": "packet_001",
                },
            ),
        ]
        for _label, approval in cases:
            with self.subTest(_label):
                with tempfile.TemporaryDirectory() as temp_name:
                    output_dir = Path(temp_name)
                    baseline = output_dir / "baseline.csv"
                    candidate = output_dir / "candidate.csv"
                    _write_track(baseline, lost_ranges=[(10, 60)])
                    _write_track(candidate, lost_ranges=[(10, 20)])

                    report = build_missing_ball_recovery_comparison(
                        baseline,
                        candidate,
                        candidate_id="candidate-pass",
                        approval=approval,
                        target_window={"start_frame": 10, "end_frame": 60},
                    )

                self.assertEqual("fail", report["summary"]["status"])
                failed_checks = {check["name"] for check in report["checks"] if check["status"] == "fail"}
                self.assertIn("approval_linkage", failed_checks)

    def test_approval_linkage_accepts_visual_localization_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            _write_track(baseline, lost_ranges=[(10, 20)])
            _write_track(candidate, lost_ranges=[(10, 20)])
            approval = _approval("candidate-visual-localization")
            approval.pop("source_packet_id", None)
            approval["visual_localization_id"] = "visual_localization:10_20_corner"

            report = build_missing_ball_recovery_comparison(
                baseline,
                candidate,
                candidate_id="candidate-visual-localization",
                approval=approval,
                target_window={"start_frame": 10, "end_frame": 20},
            )

        approval_check = next(check for check in report["checks"] if check["name"] == "approval_linkage")
        self.assertEqual("pass", approval_check["status"])

    def test_comparison_is_unavailable_when_required_track_artifacts_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            report = build_missing_ball_recovery_comparison(
                output_dir / "missing-baseline.csv",
                output_dir / "missing-candidate.csv",
                candidate_id="candidate-missing",
                approval=_approval("candidate-missing"),
                target_window={"start_frame": 10, "end_frame": 60},
            )

        self.assertEqual("unavailable", report["summary"]["status"])
        self.assertTrue(any(check["status"] == "unavailable" for check in report["checks"]))

    def test_write_report_uses_shared_contract_and_does_not_mutate_track_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            _write_track(baseline, lost_ranges=[(10, 60)])
            _write_track(candidate, lost_ranges=[(10, 20)])
            before_hashes = _hashes(baseline, candidate)

            path = write_missing_ball_recovery_comparison(
                output_dir,
                baseline,
                candidate,
                candidate_id="candidate-pass",
                approval=_targeted_approval("candidate-pass"),
                target_window={"start_frame": 10, "end_frame": 60},
            )

            loaded = json.loads(path.read_text(encoding="utf-8"))
            after_hashes = _hashes(baseline, candidate)

        self.assertEqual("missing_ball_recovery_comparison.json", path.name)
        self.assertEqual("1.0", loaded["schema_version"])
        self.assertEqual("candidate-pass", loaded["candidate"]["id"])
        self.assertEqual("candidate-pass", loaded["candidate_id"])
        self.assertEqual("approval_001", loaded["approval_id"])
        self.assertEqual("pass", loaded["comparison_status"])
        self.assertEqual("missing_ball_recovery_comparison.json", loaded["comparison_report"])
        self.assertEqual(["approval_001"], loaded["consumed_approval_ids"])
        self.assertEqual("not_promoted", loaded["promotion_status"])
        self.assertEqual(before_hashes, after_hashes)


def _approval(candidate_id: str = "candidate-pass") -> dict[str, object]:
    return {
        "approval_id": "approval_001",
        "approved_action": "localize_ball_roi",
        "candidate_id": candidate_id,
        "source_packet_id": "packet_001",
        "match_ball_confirmed": True,
        "local_search_roi": {
            "coordinate_space": "image",
            "frame": 30,
            "x": 120,
            "y": 220,
            "width": 60,
            "height": 60,
        },
    }


def _targeted_approval(candidate_id: str = "candidate-pass") -> dict[str, object]:
    return {
        "approval_id": "approval_001",
        "approved_action": "targeted_rerun",
        "candidate_id": candidate_id,
        "source_packet_id": "packet_001",
        "rerun_scope": {"start_frame": 10, "end_frame": 60},
    }


def _write_pass_stitch_report(path: Path, *, start: int, end: int, accepted_frame_count: int) -> None:
    _write_json(
        path,
        {
            "schema_version": "1.0",
            "summary": {
                "status": "pass",
                "accepted_frame_count": accepted_frame_count,
                "approval_id": "approval_001",
                "start_frame": start,
                "end_frame": end,
            },
            "metrics": {
                "status": "pass",
                "accepted_frame_count": accepted_frame_count,
                "required_window": {"start_frame": start, "end_frame": end, "frame_count": end - start + 1},
                "accepted_frame_ranges": [{"start_frame": start, "end_frame": end, "frame_count": end - start + 1}],
                "required_window_coverage": {"status": "pass", "uncovered_ranges": []},
                "roi_internal_quality": {"status": "pass", "outside_roi_ratio": 0.0, "max_step_px": 1.0},
            },
        },
    )


def _write_track(
    path: Path,
    *,
    lost_ranges: list[tuple[int, int]],
    predicted_ranges: list[tuple[int, int]] | None = None,
    frame_count: int = 80,
) -> None:
    predicted_ranges = predicted_ranges or []
    rows = ["Frame,X,Y,Confidence,Status"]
    for frame in range(frame_count):
        lost = any(start <= frame <= end for start, end in lost_ranges)
        predicted = any(start <= frame <= end for start, end in predicted_ranges)
        if lost:
            rows.append(f"{frame},,,0.00,Lost")
        elif predicted:
            rows.append(f"{frame},{100 + frame},{200 + frame},0.50,Predicted")
        else:
            rows.append(f"{frame},{100 + frame},{200 + frame},0.90,Detected")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _write_sparse_track(path: Path, *, detected_frames: range) -> None:
    rows = ["Frame,X,Y,Confidence,Status"]
    for frame in detected_frames:
        rows.append(f"{frame},{100 + frame},{200 + frame},0.90,Detected")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _write_localize_short_recovery(path: Path, *, recovered_start: int, recovered_end: int, frame_count: int = 80) -> None:
    rows = ["Frame,X,Y,Confidence,Status"]
    for frame in range(frame_count):
        if recovered_start <= frame <= recovered_end:
            rows.append(f"{frame},140,240,0.90,Detected")
        elif 10 <= frame <= 31:
            rows.append(f"{frame},,,0.00,Lost")
        else:
            rows.append(f"{frame},{100 + frame},{200 + frame},0.90,Detected")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _write_track_with_jump(path: Path, *, jump_frame: int, frame_count: int = 80) -> None:
    rows = ["Frame,X,Y,Confidence,Status"]
    for frame in range(frame_count):
        if frame == jump_frame:
            rows.append(f"{frame},800,900,0.90,Detected")
        else:
            rows.append(f"{frame},{100 + frame},{200 + frame},0.90,Detected")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _write_track_with_roi_escape(path: Path, frame_count: int = 80) -> None:
    rows = ["Frame,X,Y,Confidence,Status"]
    for frame in range(frame_count):
        if frame == 30:
            rows.append(f"{frame},140,240,0.90,Detected")
        elif 10 <= frame <= 60:
            rows.append(f"{frame},900,950,0.90,Detected")
        else:
            rows.append(f"{frame},{100 + frame},{200 + frame},0.90,Detected")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _packet(packet_id: str, label: str, start: int, end: int) -> dict[str, object]:
    return {
        "packet_id": packet_id,
        "coverage_label": label,
        "window": {"start_frame": start, "end_frame": end},
        "decision": {"label": label},
    }


def _hashes(*paths: Path) -> dict[str, str]:
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
