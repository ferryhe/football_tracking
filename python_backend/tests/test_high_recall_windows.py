from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from football_tracking.chunk_runner import build_high_recall_window_config, run_high_recall_windows
from football_tracking.config import load_config
from football_tracking.high_recall_windows import (
    _max_frame_in_csv,
    approved_action_windows_from_report,
    build_high_recall_windows,
    write_high_recall_window_report,
)
from football_tracking.review_packets import build_review_packet_report


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_review_packets(output_dir: Path, *packet_ids: str) -> None:
    _write_json(output_dir / "review_packets.json", {"packets": [{"packet_id": packet_id} for packet_id in packet_ids]})


def _clean_visual_localization_request(visual_localization_id: str, *, source_packet_id: str) -> dict:
    local_roi = {
        "coordinate_space": "image",
        "frame": 2121,
        "x": 5000,
        "y": 960,
        "width": 120,
        "height": 200,
        "confidence": 0.9,
    }
    return {
        "visual_localization_id": visual_localization_id,
        "source_packet_id": source_packet_id,
        "status": "localized",
        "media_warnings": [],
        "media_integrity": {
            "status": "ok",
            "image_count": 2,
            "low_information_image_count": 0,
            "likely_corrupt_image_count": 0,
        },
        "local_search_roi": local_roi,
        "roi_status": "accepted",
        "frames": [
            {
                "frame": 2121,
                "status": "localized",
                "ball_visible": True,
                "confidence": 0.9,
                "local_search_roi": local_roi,
            }
        ],
        "coverage": {
            "covered_subwindows": [{"start_frame": 2121, "end_frame": 2121, "status": "localized"}],
            "uncovered_subwindows": [],
        },
    }


class HighRecallWindowTests(unittest.TestCase):
    def test_max_frame_in_csv_streams_frame_column_and_skips_invalid_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "ball_track.csv"
            path.write_text(
                "X,Frame,Y\n"
                "10,3,20\n"
                "bad,not-a-frame,row\n"
                "11,15,21\n",
                encoding="utf-8",
            )

            self.assertEqual(15, _max_frame_in_csv(path))

    def test_build_combines_sources_applies_margin_and_merges_close_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_review_triggers.json",
                {
                    "decision": {
                        "priority": "high",
                        "recommended_review_windows": [
                            {"start_frame": 10, "end_frame": 15, "reason": "large_jump"},
                        ],
                    },
                },
            )
            _write_json(
                output_dir / "ball_audit.json",
                {
                    "review_events": [
                        {
                            "type": "lost_gap",
                            "severity": "warn",
                            "start_frame": 18,
                            "end_frame": 20,
                            "reason": "lost gap",
                        },
                    ],
                    "tracklets": [],
                },
            )
            _write_json(
                output_dir / "event_candidates.json",
                {
                    "candidates": [
                        {
                            "type": "shot_candidate",
                            "score": 0.82,
                            "start_frame": 70,
                            "end_frame": 72,
                            "render_window": {"start_frame": 60, "end_frame": 90},
                            "reason": "speed burst",
                        },
                    ],
                },
            )

            report = build_high_recall_windows(
                output_dir,
                margin_frames=2,
                merge_gap_frames=10,
                max_total_frames=200,
                total_frames=120,
                mode="sahi",
            )

        self.assertEqual("succeeded", report["summary"]["status"])
        self.assertEqual(2, report["summary"]["selected_window_count"])
        self.assertEqual(
            [
                {"start_frame": 8, "end_frame": 22, "mode": "sahi", "priority": "high"},
                {"start_frame": 58, "end_frame": 92, "mode": "sahi", "priority": "high"},
            ],
            [
                {
                    "start_frame": window["start_frame"],
                    "end_frame": window["end_frame"],
                    "mode": window["mode"],
                    "priority": window["priority"],
                }
                for window in report["windows"]
            ],
        )
        self.assertIn("ai_review", report["windows"][0]["reason"])
        self.assertIn("ball_audit", report["windows"][0]["reason"])
        self.assertIn("event_candidates", report["windows"][1]["reason"])

    def test_max_total_frames_keeps_higher_priority_window_and_records_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_review_triggers.json",
                {
                    "decision": {
                        "priority": "medium",
                        "recommended_review_windows": [
                            {"start_frame": 0, "end_frame": 49, "reason": "wide medium review"},
                        ],
                    },
                },
            )
            _write_json(
                output_dir / "event_candidates.json",
                {
                    "candidates": [
                        {
                            "type": "goal_candidate",
                            "score": 0.91,
                            "start_frame": 80,
                            "end_frame": 89,
                            "reason": "short high-value candidate",
                        },
                    ],
                },
            )

            report = build_high_recall_windows(
                output_dir,
                margin_frames=0,
                merge_gap_frames=0,
                max_total_frames=15,
                mode="sahi",
            )

        self.assertEqual("capped", report["summary"]["status"])
        self.assertEqual([(80, 89)], [(item["start_frame"], item["end_frame"]) for item in report["windows"]])
        self.assertEqual(1, report["summary"]["rejected_count"])
        self.assertEqual("max_total_frames_exceeded", report["rejected_windows"][0]["rejection_reason"])

    def test_default_budget_caps_large_window_plans(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_review_triggers.json",
                {
                    "decision": {
                        "priority": "high",
                        "recommended_review_windows": [
                            {"start_frame": 0, "end_frame": 1999, "reason": "wide review"},
                        ],
                    },
                },
            )

            report = build_high_recall_windows(output_dir)

        self.assertEqual("rejected", report["summary"]["status"])
        self.assertEqual(0, report["summary"]["selected_window_count"])
        self.assertEqual(1, report["summary"]["rejected_count"])
        self.assertEqual(1800, report["settings"]["max_total_frames"])

    def test_budget_selects_long_lost_gap_before_noisy_merged_span(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_review_triggers.json",
                {
                    "triggers": [
                        {
                            "type": "dense_noise_cluster",
                            "priority": "high",
                            "start_frame": 160,
                            "end_frame": 5191,
                            "reason": "dense audit signals",
                        },
                        *[
                            {
                                "type": "large_jump",
                                "priority": "high",
                                "start_frame": start,
                                "end_frame": start + 12,
                                "reason": "large jump",
                            }
                            for start in range(180, 5200, 45)
                        ],
                        {
                            "type": "lost_gap",
                            "priority": "medium",
                            "start_frame": 2049,
                            "end_frame": 2544,
                            "reason": "Ball track is lost for 496 frames between tracklets.",
                        },
                    ]
                },
            )

            report = build_high_recall_windows(
                output_dir,
                margin_frames=12,
                merge_gap_frames=30,
                max_total_frames=1800,
                total_frames=5194,
                mode="sahi",
            )

        selected_ranges = [(window["start_frame"], window["end_frame"]) for window in report["windows"]]
        self.assertTrue(any(start <= 2049 and end >= 2544 for start, end in selected_ranges))
        self.assertGreater(report["summary"]["selected_window_count"], 0)
        self.assertNotEqual("rejected", report["summary"]["status"])

    def test_non_positive_budget_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)

            with self.assertRaises(ValueError):
                build_high_recall_windows(output_dir, max_total_frames=0)

    def test_invalid_budget_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)

            with self.assertRaises(ValueError):
                build_high_recall_windows(output_dir, max_total_frames="inf")  # type: ignore[arg-type]

    def test_three_input_sources_are_parsed_and_report_can_be_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_review_triggers.json",
                {
                    "triggers": [
                        {
                            "type": "large_jump",
                            "priority": "high",
                            "start_frame": 5,
                            "end_frame": 6,
                            "reason": "triggered jump",
                        },
                    ],
                },
            )
            _write_json(
                output_dir / "ball_audit.json",
                {
                    "review_events": [
                        {
                            "type": "candidate_ambiguity",
                            "severity": "warn",
                            "start_frame": 25,
                            "end_frame": 25,
                        },
                    ],
                    "tracklets": [
                        {
                            "id": "raw:50-52",
                            "source": "raw",
                            "start_frame": 50,
                            "end_frame": 52,
                            "flags": ["low_confidence"],
                            "suspicion_score": 0.35,
                        }
                    ],
                },
            )
            _write_json(
                output_dir / "event_candidates.json",
                {
                    "candidates": [
                        {
                            "type": "shot_candidate",
                            "score": 0.76,
                            "start_frame": 75,
                            "end_frame": 79,
                        },
                    ],
                },
            )

            report = write_high_recall_window_report(
                output_dir,
                margin_frames=0,
                merge_gap_frames=0,
                max_total_frames=200,
            )
            saved = json.loads((output_dir / "high_recall_windows" / "report.json").read_text(encoding="utf-8"))

        sources = [window["sources"][0] for window in report["windows"]]
        self.assertEqual(["ai_review_triggers", "ball_audit", "ball_audit", "event_candidates"], sources)
        self.assertEqual(report["windows"], saved["windows"])

    def test_approved_actions_file_is_not_consumed_without_explicit_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_improvement_approved_actions.json",
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval_001",
                            "improvement_id": "imp_001",
                            "approved_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 10, "end_frame": 20},
                        }
                    ],
                },
            )

            report = build_high_recall_windows(output_dir, margin_frames=0, merge_gap_frames=0)

        self.assertEqual([], report["windows"])
        self.assertEqual(0, report["summary"]["candidate_window_count"])

    def test_explicit_approved_targeted_rerun_creates_window_with_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(output_dir, "packet_001")
            _write_json(
                output_dir / "ai_visual_review.json",
                {"reviews": [{"visual_review_id": "visual_review:packet_001", "source_packet_id": "packet_001"}]},
            )
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(
                approved_path,
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval_001",
                            "improvement_id": "imp_001",
                            "candidate_id": "candidate_001",
                            "approved_action": "targeted_rerun",
                            "approval_source": "api",
                            "rerun_scope": {"start_frame": 10, "end_frame": 20},
                            "source_packet_id": "packet_001",
                            "visual_review_id": "visual_review:packet_001",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 15,
                                "x": 120,
                                "y": 40,
                                "width": 80,
                                "height": 50,
                                "confidence": 0.72,
                            },
                            "provenance": {"source": "ai_improvement", "model": "gpt-improve"},
                        }
                    ],
                },
            )

            report = build_high_recall_windows(
                output_dir,
                margin_frames=0,
                merge_gap_frames=0,
                max_total_frames=100,
                approved_actions_path=approved_path,
            )

        self.assertEqual(1, report["summary"]["selected_window_count"])
        window = report["windows"][0]
        self.assertEqual("ai_improvement", window["sources"][0])
        self.assertEqual("approval_001", window["approval_id"])
        self.assertEqual("imp_001", window["improvement_id"])
        self.assertEqual("api", window["approval_source"])
        self.assertEqual("packet_001", window["source_packet_id"])
        self.assertEqual("visual_review:packet_001", window["visual_review_id"])
        self.assertEqual(120, window["local_search_roi"]["x"])
        self.assertEqual({"start_frame": 10, "end_frame": 20}, window["rerun_scope"])
        self.assertEqual("approval_001", window["approval_provenance"][0]["approval_id"])
        self.assertEqual("imp_001", window["approval_provenance"][0]["improvement_id"])
        self.assertEqual({"start_frame": 10, "end_frame": 20}, window["approval_provenance"][0]["rerun_scope"])
        self.assertEqual("gpt-improve", window["approval_provenance"][0]["provenance"]["model"])

    def test_approved_targeted_rerun_with_local_search_roi_writes_roi_policy_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(output_dir, "packet_001")
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(
                approved_path,
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval_001",
                            "improvement_id": "imp_001",
                            "candidate_id": "candidate_001",
                            "approved_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 10, "end_frame": 20},
                            "source_packet_id": "packet_001",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 15,
                                "x": 120,
                                "y": 40,
                                "width": 80,
                                "height": 50,
                                "confidence": 0.72,
                            },
                        }
                    ],
                },
            )

            report = build_high_recall_windows(
                output_dir,
                margin_frames=0,
                merge_gap_frames=0,
                max_total_frames=100,
                approved_actions_path=approved_path,
            )

        window = report["windows"][0]
        self.assertEqual([120, 40, 200, 90], window["approved_roi"])
        self.assertEqual([88, 8, 232, 122], window["padded_roi"])
        self.assertEqual([88, 8, 232, 122], window["effective_roi"])
        self.assertEqual("sahi_roi", window["sahi_policy"])

    def test_approved_targeted_rerun_with_local_search_roi_requires_traceable_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(
                approved_path,
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval_001",
                            "improvement_id": "imp_001",
                            "candidate_id": "candidate_001",
                            "approved_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 10, "end_frame": 20},
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 15,
                                "x": 120,
                                "y": 40,
                                "width": 80,
                                "height": 50,
                                "confidence": 0.72,
                            },
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(ValueError, "requires source_packet_id or visual_review_id provenance"):
                build_high_recall_windows(output_dir, approved_actions_path=approved_path)

    def test_explicit_approved_actions_path_rejects_corrupt_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            approved_path.write_text("{", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "ai_improvement_approved_actions.json.*corrupt"):
                build_high_recall_windows(output_dir, approved_actions_path=approved_path)

    def test_explicit_approved_actions_path_reports_generic_shape_errors_for_custom_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            approved_path = output_dir / "custom-approved-actions.json"
            _write_json(approved_path, {"schema_version": "1.0", "approved_actions": ["not-an-action"]})

            with self.assertRaisesRegex(ValueError, "approved actions artifact invalid"):
                build_high_recall_windows(output_dir, approved_actions_path=approved_path)

    def test_explicit_approved_actions_path_rejects_stale_targeted_rerun_without_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(
                approved_path,
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval_001",
                            "improvement_id": "imp_001",
                            "approved_action": "targeted_rerun",
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(ValueError, "targeted_rerun.*rerun_scope"):
                build_high_recall_windows(output_dir, approved_actions_path=approved_path)

    def test_explicit_approved_actions_path_rejects_malformed_targeted_rerun_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(
                approved_path,
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approved_action": "targeted_rerun",
                            "improvement_id": "imp_001",
                            "rerun_scope": {"start_frame": 10, "end_frame": 20},
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(ValueError, "approval_id is required"):
                build_high_recall_windows(output_dir, approved_actions_path=approved_path)

            _write_json(
                approved_path,
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval_001",
                            "approved_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 10, "end_frame": 20},
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(ValueError, "improvement_id is required"):
                build_high_recall_windows(output_dir, approved_actions_path=approved_path)

    def test_explicit_approved_actions_path_rejects_fractional_targeted_rerun_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(
                approved_path,
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval_001",
                            "improvement_id": "imp_001",
                            "candidate_id": "candidate_001",
                            "approved_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 10.9, "end_frame": 20},
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(ValueError, "integer start_frame"):
                build_high_recall_windows(output_dir, approved_actions_path=approved_path)

    def test_approved_non_rerun_action_does_not_create_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(
                approved_path,
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval_001",
                            "improvement_id": "imp_001",
                            "approved_action": "manual_review",
                            "rerun_scope": {"start_frame": 10, "end_frame": 20},
                        }
                    ],
                },
            )

            report = build_high_recall_windows(output_dir, approved_actions_path=approved_path)

        self.assertEqual([], report["windows"])
        self.assertEqual(0, report["summary"]["candidate_window_count"])

    def test_approved_localize_ball_roi_with_bounded_frames_creates_roi_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(output_dir, "packet_001")
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(
                approved_path,
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval_001",
                            "improvement_id": "imp_001",
                            "candidate_id": "candidate_001",
                            "approved_action": "localize_ball_roi",
                            "start_frame": 10,
                            "end_frame": 20,
                            "source_packet_id": "packet_001",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 15,
                                "x": 120,
                                "y": 40,
                                "width": 80,
                                "height": 50,
                                "confidence": 0.72,
                            },
                        }
                    ],
                },
            )

            report = build_high_recall_windows(
                output_dir,
                margin_frames=0,
                merge_gap_frames=0,
                max_total_frames=100,
                total_frames=100,
                approved_actions_path=approved_path,
                approved_only=True,
            )

        self.assertEqual(1, report["summary"]["selected_window_count"])
        window = report["windows"][0]
        self.assertEqual((10, 20), (window["start_frame"], window["end_frame"]))
        self.assertEqual("localize_ball_roi", window["approved_action"])
        self.assertEqual("candidate_001", window["candidate_id"])
        self.assertEqual("candidate_001", window["approval_provenance"][0]["candidate_id"])
        self.assertEqual([120, 40, 200, 90], window["approved_roi"])
        self.assertEqual([88, 8, 232, 122], window["effective_roi"])
        self.assertEqual("sahi_roi", window["sahi_policy"])
        self.assertEqual("packet_001", window["source_packet_id"])

    def test_approved_localize_ball_roi_accepts_visual_localization_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_visual_localization.json",
                {
                    "requests": [
                        _clean_visual_localization_request(
                            "visual_localization:2049_2544_right_corner",
                            source_packet_id="packet_001",
                        )
                    ]
                },
            )
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(
                approved_path,
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval_001",
                            "improvement_id": "imp_001",
                            "candidate_id": "candidate_001",
                            "approved_action": "localize_ball_roi",
                            "start_frame": 2049,
                            "end_frame": 2544,
                            "visual_localization_id": "visual_localization:2049_2544_right_corner",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 2121,
                                "x": 5000,
                                "y": 960,
                                "width": 120,
                                "height": 200,
                                "confidence": 0.9,
                            },
                        }
                    ],
                },
            )

            report = build_high_recall_windows(
                output_dir,
                margin_frames=0,
                merge_gap_frames=0,
                max_total_frames=600,
                total_frames=3000,
                approved_actions_path=approved_path,
                approved_only=True,
            )

        window = report["windows"][0]
        self.assertEqual("visual_localization:2049_2544_right_corner", window["visual_localization_id"])
        self.assertEqual(
            "visual_localization:2049_2544_right_corner",
            window["approval_provenance"][0]["visual_localization_id"],
        )

    def test_approved_localize_ball_roi_rejects_dirty_visual_localization_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(output_dir, "packet_001")
            dirty_request = _clean_visual_localization_request(
                "visual_localization:2049_2544_right_corner",
                source_packet_id="packet_001",
            )
            dirty_request["media_warnings"] = ["contact_sheet_unreadable"]
            _write_json(output_dir / "ai_visual_localization.json", {"requests": [dirty_request]})
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(
                approved_path,
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval_001",
                            "improvement_id": "imp_001",
                            "candidate_id": "candidate_001",
                            "approved_action": "localize_ball_roi",
                            "start_frame": 2049,
                            "end_frame": 2544,
                            "source_packet_id": "packet_001",
                            "visual_localization_id": "visual_localization:2049_2544_right_corner",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 2121,
                                "x": 5000,
                                "y": 960,
                                "width": 120,
                                "height": 200,
                                "confidence": 0.9,
                            },
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(ValueError, "clean ai_visual_localization evidence"):
                build_high_recall_windows(
                    output_dir,
                    total_frames=3000,
                    approved_actions_path=approved_path,
                    approved_only=True,
                )

    def test_approved_localize_ball_roi_rejects_nested_corrupt_visual_localization_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(output_dir, "packet_001")
            dirty_request = _clean_visual_localization_request(
                "visual_localization:2049_2544_right_corner",
                source_packet_id="packet_001",
            )
            dirty_request["media_warnings"] = []
            dirty_request["media_integrity"] = {
                "contact_sheet": {
                    "status": "ok",
                    "likely_corrupt": True,
                    "low_information": False,
                    "gray": False,
                }
            }
            _write_json(output_dir / "ai_visual_localization.json", {"requests": [dirty_request]})
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(
                approved_path,
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval_001",
                            "improvement_id": "imp_001",
                            "candidate_id": "candidate_001",
                            "approved_action": "localize_ball_roi",
                            "start_frame": 2049,
                            "end_frame": 2544,
                            "source_packet_id": "packet_001",
                            "visual_localization_id": "visual_localization:2049_2544_right_corner",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 2121,
                                "x": 5000,
                                "y": 960,
                                "width": 120,
                                "height": 200,
                                "confidence": 0.9,
                            },
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(ValueError, "clean ai_visual_localization evidence"):
                build_high_recall_windows(
                    output_dir,
                    total_frames=3000,
                    approved_actions_path=approved_path,
                    approved_only=True,
                )

    def test_approved_localize_ball_roi_rejects_unknown_visual_localization_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_visual_localization.json",
                {"requests": [{"visual_localization_id": "visual_localization:known"}]},
            )
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(
                approved_path,
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval_001",
                            "improvement_id": "imp_001",
                            "candidate_id": "candidate_001",
                            "approved_action": "localize_ball_roi",
                            "start_frame": 2049,
                            "end_frame": 2544,
                            "visual_localization_id": "visual_localization:missing",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 2121,
                                "x": 5000,
                                "y": 960,
                                "width": 120,
                                "height": 200,
                                "confidence": 0.9,
                            },
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(ValueError, "visual_localization_id does not match"):
                build_high_recall_windows(
                    output_dir,
                    total_frames=3000,
                    approved_actions_path=approved_path,
                    approved_only=True,
                )

    def test_approved_localize_ball_roi_requires_bounded_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(
                approved_path,
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval_001",
                            "improvement_id": "imp_001",
                            "candidate_id": "candidate_001",
                            "approved_action": "localize_ball_roi",
                            "source_packet_id": "packet_001",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 15,
                                "x": 120,
                                "y": 40,
                                "width": 80,
                                "height": 50,
                                "confidence": 0.72,
                            },
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(ValueError, "localize_ball_roi.*frame bounds"):
                build_high_recall_windows(output_dir, approved_actions_path=approved_path)

    def test_approved_localize_ball_roi_requires_candidate_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(
                approved_path,
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval_001",
                            "improvement_id": "imp_001",
                            "approved_action": "localize_ball_roi",
                            "start_frame": 10,
                            "end_frame": 20,
                            "source_packet_id": "packet_001",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 15,
                                "x": 120,
                                "y": 40,
                                "width": 80,
                                "height": 50,
                                "confidence": 0.72,
                            },
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(ValueError, "localize_ball_roi.*candidate_id"):
                build_high_recall_windows(output_dir, approved_actions_path=approved_path)

    def test_approved_localize_ball_roi_rejects_unknown_packet_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(output_dir / "review_packets.json", {"packets": [{"packet_id": "packet_known"}]})
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(
                approved_path,
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval_001",
                            "improvement_id": "imp_001",
                            "candidate_id": "candidate_001",
                            "approved_action": "localize_ball_roi",
                            "start_frame": 10,
                            "end_frame": 20,
                            "source_packet_id": "packet_fake",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 15,
                                "x": 120,
                                "y": 40,
                                "width": 80,
                                "height": 50,
                                "confidence": 0.72,
                            },
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(ValueError, "source_packet_id does not match"):
                build_high_recall_windows(output_dir, approved_actions_path=approved_path)

    def test_approved_localize_ball_roi_rejects_roi_frame_outside_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(output_dir, "packet_001")
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(
                approved_path,
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval_001",
                            "improvement_id": "imp_001",
                            "candidate_id": "candidate_001",
                            "approved_action": "localize_ball_roi",
                            "start_frame": 10,
                            "end_frame": 20,
                            "source_packet_id": "packet_001",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 25,
                                "x": 120,
                                "y": 40,
                                "width": 80,
                                "height": 50,
                                "confidence": 0.72,
                            },
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(ValueError, "localize_ball_roi.*frame must fall inside"):
                build_high_recall_windows(output_dir, approved_actions_path=approved_path)

    def test_approved_localize_ball_roi_rejects_roi_frame_outside_final_clamped_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(output_dir, "packet_001")
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(
                approved_path,
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval_001",
                            "improvement_id": "imp_001",
                            "candidate_id": "candidate_001",
                            "approved_action": "localize_ball_roi",
                            "start_frame": 95,
                            "end_frame": 150,
                            "source_packet_id": "packet_001",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 120,
                                "x": 120,
                                "y": 40,
                                "width": 80,
                                "height": 50,
                                "confidence": 0.72,
                            },
                        }
                    ],
                },
            )

            report = build_high_recall_windows(
                output_dir,
                margin_frames=0,
                merge_gap_frames=0,
                max_total_frames=None,
                total_frames=100,
                approved_actions_path=approved_path,
                approved_only=True,
            )

        self.assertEqual("rejected", report["summary"]["status"])
        self.assertEqual("local_search_roi_frame_outside_final_window", report["rejected_windows"][0]["rejection_reason"])

    def test_approved_localize_ball_roi_honors_frame_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(output_dir, "packet_001")
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(
                approved_path,
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval_001",
                            "improvement_id": "imp_001",
                            "candidate_id": "candidate_001",
                            "approved_action": "localize_ball_roi",
                            "start_frame": 10,
                            "end_frame": 20,
                            "source_packet_id": "packet_001",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 15,
                                "x": 120,
                                "y": 40,
                                "width": 80,
                                "height": 50,
                                "confidence": 0.72,
                            },
                        }
                    ],
                },
            )

            report = build_high_recall_windows(
                output_dir,
                margin_frames=0,
                merge_gap_frames=0,
                max_total_frames=5,
                total_frames=100,
                approved_actions_path=approved_path,
                approved_only=True,
            )

        self.assertEqual("rejected", report["summary"]["status"])
        self.assertEqual([], report["windows"])
        self.assertEqual("max_total_frames_exceeded", report["rejected_windows"][0]["rejection_reason"])

    def test_approved_localize_ball_roi_rejects_full_video_scope_without_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(output_dir, "packet_001")
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(
                approved_path,
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval_001",
                            "improvement_id": "imp_001",
                            "candidate_id": "candidate_001",
                            "approved_action": "localize_ball_roi",
                            "start_frame": 0,
                            "end_frame": 99,
                            "source_packet_id": "packet_001",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 15,
                                "x": 120,
                                "y": 40,
                                "width": 80,
                                "height": 50,
                                "confidence": 0.72,
                            },
                        }
                    ],
                },
            )

            report = build_high_recall_windows(
                output_dir,
                margin_frames=0,
                merge_gap_frames=0,
                max_total_frames=None,
                total_frames=100,
                approved_actions_path=approved_path,
                approved_only=True,
            )

        self.assertEqual("rejected", report["summary"]["status"])
        self.assertEqual([], report["windows"])
        self.assertEqual("full_video_localize_scope_rejected", report["rejected_windows"][0]["rejection_reason"])

    def test_approved_localize_ball_roi_rejects_full_video_scope_after_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(output_dir, "packet_001")
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            actions = []
            for approval_id, start_frame, end_frame, roi_frame in [
                ("approval_001", 0, 49, 20),
                ("approval_002", 50, 99, 70),
            ]:
                actions.append(
                    {
                        "approval_id": approval_id,
                        "improvement_id": f"imp_{approval_id[-3:]}",
                        "candidate_id": f"candidate_{approval_id[-3:]}",
                        "approved_action": "localize_ball_roi",
                        "start_frame": start_frame,
                        "end_frame": end_frame,
                        "source_packet_id": "packet_001",
                        "local_search_roi": {
                            "coordinate_space": "image",
                            "frame": roi_frame,
                            "x": 120,
                            "y": 40,
                            "width": 80,
                            "height": 50,
                            "confidence": 0.72,
                        },
                    }
                )
            _write_json(approved_path, {"schema_version": "1.0", "approved_actions": actions})

            report = build_high_recall_windows(
                output_dir,
                margin_frames=0,
                merge_gap_frames=0,
                max_total_frames=None,
                total_frames=100,
                approved_actions_path=approved_path,
                approved_only=True,
            )

        self.assertEqual("rejected", report["summary"]["status"])
        self.assertEqual([], report["windows"])
        self.assertEqual(
            ["full_video_localize_scope_rejected", "full_video_localize_scope_rejected"],
            [window["rejection_reason"] for window in report["rejected_windows"]],
        )
        self.assertEqual(
            [(0, 49), (50, 99)],
            [(window["start_frame"], window["end_frame"]) for window in report["rejected_windows"]],
        )

    def test_deterministic_windows_keep_priority_over_approved_ai_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_review_triggers.json",
                {
                    "triggers": [
                        {
                            "type": "large_jump",
                            "priority": "high",
                            "start_frame": 100,
                            "end_frame": 104,
                            "reason": "deterministic high",
                        }
                    ],
                },
            )
            _write_review_packets(output_dir, "packet_001")
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(
                approved_path,
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval_001",
                            "improvement_id": "imp_001",
                            "candidate_id": "candidate_001",
                            "approved_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 10, "end_frame": 40},
                            "source_packet_id": "packet_001",
                        }
                    ],
                },
            )

            report = build_high_recall_windows(
                output_dir,
                margin_frames=0,
                merge_gap_frames=0,
                max_total_frames=10,
                approved_actions_path=approved_path,
            )

        self.assertEqual([(100, 104)], [(item["start_frame"], item["end_frame"]) for item in report["windows"]])
        self.assertEqual("ai_improvement", report["rejected_windows"][0]["sources"][0])

    def test_deterministic_window_is_not_merged_into_approved_roi_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(output_dir, "packet_001")
            _write_json(
                output_dir / "ai_review_triggers.json",
                {
                    "triggers": [
                        {
                            "type": "lost_gap",
                            "priority": "medium",
                            "start_frame": 10,
                            "end_frame": 20,
                            "reason": "deterministic overlap",
                        }
                    ],
                },
            )
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(
                approved_path,
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval_001",
                            "improvement_id": "imp_001",
                            "candidate_id": "candidate_001",
                            "approved_action": "targeted_rerun",
                            "approval_source": "api",
                            "rerun_scope": {"start_frame": 15, "end_frame": 25},
                            "source_packet_id": "packet_001",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 18,
                                "x": 120,
                                "y": 40,
                                "width": 80,
                                "height": 50,
                                "confidence": 0.72,
                            },
                        }
                    ],
                },
            )

            report = build_high_recall_windows(
                output_dir,
                margin_frames=0,
                merge_gap_frames=30,
                max_total_frames=100,
                approved_actions_path=approved_path,
            )

        self.assertEqual(2, report["summary"]["selected_window_count"])
        deterministic_window, approved_window = report["windows"]
        self.assertEqual(["ai_review_triggers"], deterministic_window["sources"])
        self.assertNotIn("effective_roi", deterministic_window)
        self.assertEqual(["ai_improvement"], approved_window["sources"])
        self.assertEqual("approval_001", approved_window["approval_provenance"][0]["approval_id"])
        self.assertEqual("imp_001", approved_window["approval_provenance"][0]["improvement_id"])
        self.assertEqual(120, approved_window["approval_provenance"][0]["local_search_roi"]["x"])

    def test_approved_targeted_reruns_with_different_rois_are_not_merged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(output_dir, "packet_001", "packet_002")
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(
                approved_path,
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval_001",
                            "improvement_id": "imp_001",
                            "candidate_id": "candidate_001",
                            "approved_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 10, "end_frame": 20},
                            "source_packet_id": "packet_001",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 15,
                                "x": 120,
                                "y": 40,
                                "width": 80,
                                "height": 50,
                                "confidence": 0.72,
                            },
                        },
                        {
                            "approval_id": "approval_002",
                            "improvement_id": "imp_002",
                            "candidate_id": "candidate_002",
                            "approved_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 21, "end_frame": 28},
                            "source_packet_id": "packet_002",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 23,
                                "x": 600,
                                "y": 300,
                                "width": 70,
                                "height": 40,
                                "confidence": 0.76,
                            },
                        },
                    ],
                },
            )

            report = build_high_recall_windows(
                output_dir,
                margin_frames=0,
                merge_gap_frames=30,
                max_total_frames=100,
                approved_actions_path=approved_path,
            )

        self.assertEqual(2, report["summary"]["selected_window_count"])
        self.assertEqual(["approval_001", "approval_002"], [window["approval_id"] for window in report["windows"]])
        self.assertEqual([[120, 40, 200, 90], [600, 300, 670, 340]], [window["approved_roi"] for window in report["windows"]])

    def test_approved_action_windows_from_report_returns_executable_normalized_windows(self) -> None:
        report = {
            "schema_version": "1.0",
            "approved_actions": [
                {
                    "approval_id": "approval_001",
                    "improvement_id": "imp_001",
                    "candidate_id": "candidate_001",
                    "approved_action": "targeted_rerun",
                    "approval_source": "api",
                    "rerun_scope": {"start_frame": 10, "end_frame": 12},
                    "source_packet_id": "packet_001",
                },
                {
                    "approval_id": "approval_manual",
                    "improvement_id": "imp_manual",
                    "approved_action": "manual_review",
                },
            ],
        }

        windows = approved_action_windows_from_report(report, mode="sahi")

        self.assertEqual(1, len(windows))
        self.assertEqual({"start_frame": 10, "end_frame": 12}, windows[0]["rerun_scope"])
        self.assertEqual("approval_001", windows[0]["approval_id"])
        self.assertEqual("sahi", windows[0]["mode"])

    def test_approved_only_ignores_deterministic_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_review_triggers.json",
                {
                    "triggers": [
                        {
                            "type": "large_jump",
                            "priority": "high",
                            "start_frame": 100,
                            "end_frame": 104,
                            "reason": "deterministic high",
                        }
                    ],
                },
            )
            _write_review_packets(output_dir, "packet_001")
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(
                approved_path,
                {
                    "schema_version": "1.0",
                    "approved_actions": [
                        {
                            "approval_id": "approval_001",
                            "improvement_id": "imp_001",
                            "candidate_id": "candidate_001",
                            "approved_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 10, "end_frame": 12},
                            "source_packet_id": "packet_001",
                        }
                    ],
                },
            )

            report = build_high_recall_windows(
                output_dir,
                margin_frames=0,
                merge_gap_frames=0,
                max_total_frames=100,
                approved_actions_path=approved_path,
                approved_only=True,
            )

        self.assertTrue(report["settings"]["approved_only"])
        self.assertEqual([(10, 12)], [(item["start_frame"], item["end_frame"]) for item in report["windows"]])
        self.assertEqual(1, report["summary"]["candidate_window_count"])


class HighRecallChunkRunnerHookTests(unittest.TestCase):
    def test_high_recall_sahi_window_disables_half_precision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            config = SimpleNamespace(
                output_dir=output_dir,
                detector=SimpleNamespace(inference_mode="direct_full_frame", use_half=True),
                runtime=SimpleNamespace(start_frame=0, max_frames=None),
                postprocess=SimpleNamespace(enabled=True),
                follow_cam=SimpleNamespace(enabled=True),
                temporal_chunks=SimpleNamespace(enabled=True),
                output=SimpleNamespace(save_csv=False, save_debug_jsonl=False),
                logging=SimpleNamespace(save_debug_jsonl=False),
                high_recall_windows=SimpleNamespace(
                    enabled=True,
                    margin_frames=0,
                    merge_gap_frames=0,
                    max_total_frames=100,
                    mode="sahi",
                    output_dir_name="high_recall_windows",
                    max_speed_px_per_frame=120.0,
                    max_jump_px=180.0,
                ),
            )

            window_config = build_high_recall_window_config(
                config,
                {"start_frame": 12, "end_frame": 18},
                output_dir / "high_recall_windows" / "window_000",
            )

        self.assertEqual("sahi", window_config.detector.inference_mode)
        self.assertFalse(window_config.detector.use_half)

    def test_runner_executes_only_selected_windows_and_reconciles_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            config = SimpleNamespace(
                output_dir=output_dir,
                output=SimpleNamespace(csv_name="ball_track.csv", debug_jsonl_name="debug.jsonl"),
                detector=SimpleNamespace(inference_mode="direct_full_frame"),
                runtime=SimpleNamespace(start_frame=0, max_frames=None),
                high_recall_windows=SimpleNamespace(
                    enabled=True,
                    margin_frames=0,
                    merge_gap_frames=0,
                    max_total_frames=100,
                    mode="sahi",
                    output_dir_name="high_recall_windows",
                    max_speed_px_per_frame=120.0,
                    max_jump_px=180.0,
                ),
            )
            window = {
                "start_frame": 12,
                "end_frame": 18,
                "reason": "ai_review: lost_gap",
                "mode": "sahi",
                "priority": "high",
            }
            window_report = {"windows": [window], "summary": {"selected_window_count": 1}}

            def fake_write_config(config_arg, window_arg, window_index, root_arg):
                window_dir = root_arg / f"window_{window_index:03d}"
                window_dir.mkdir(parents=True, exist_ok=True)
                config_path = window_dir / "chunk_config.yaml"
                config_path.write_text("mock: true\n", encoding="utf-8")
                return config_path

            with (
                patch("football_tracking.chunk_runner.write_ball_audit_report") as write_audit,
                patch("football_tracking.chunk_runner.write_ai_review_trigger_report") as write_ai_review,
                patch("football_tracking.chunk_runner.write_event_candidate_report") as write_events,
                patch(
                    "football_tracking.chunk_runner.write_high_recall_window_report",
                    return_value=window_report,
                ) as write_windows,
                patch(
                    "football_tracking.chunk_runner.write_high_recall_window_config",
                    side_effect=fake_write_config,
                ) as write_config,
                patch("football_tracking.chunk_runner.run_high_recall_chunk", return_value=0) as run_chunk_mock,
                patch(
                    "football_tracking.chunk_runner.reconcile_high_recall_outputs",
                    return_value={"summary": {"accepted_count": 1}},
                ) as reconcile,
            ):
                report = run_high_recall_windows(config, source_total_frames=200)

        write_audit.assert_called_once_with(output_dir)
        write_ai_review.assert_called_once_with(output_dir)
        write_events.assert_called_once_with(output_dir, fps=None, fps_source=None)
        write_windows.assert_called_once()
        self.assertIsNone(write_windows.call_args.kwargs["approved_actions_path"])
        write_config.assert_called_once()
        run_chunk_mock.assert_called_once()
        reconcile.assert_called_once_with(
            output_dir,
            [window],
            high_recall_root=output_dir / "high_recall_windows",
            csv_name="ball_track.csv",
            max_speed_px_per_frame=120.0,
            max_jump_px=180.0,
        )
        self.assertEqual("succeeded", report["execution"]["status"])

    def test_runner_does_not_pass_approved_actions_path_when_artifact_only_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_improvement_approved_actions.json",
                {"schema_version": "1.0", "approved_actions": []},
            )
            config = SimpleNamespace(
                output_dir=output_dir,
                output=SimpleNamespace(csv_name="ball_track.csv", debug_jsonl_name="debug.jsonl"),
                detector=SimpleNamespace(inference_mode="direct_full_frame"),
                runtime=SimpleNamespace(start_frame=0, max_frames=None),
                high_recall_windows=SimpleNamespace(
                    enabled=True,
                    margin_frames=0,
                    merge_gap_frames=0,
                    max_total_frames=100,
                    mode="sahi",
                    output_dir_name="high_recall_windows",
                    max_speed_px_per_frame=120.0,
                    max_jump_px=180.0,
                ),
            )

            with (
                patch("football_tracking.chunk_runner.write_ball_audit_report", return_value={}),
                patch("football_tracking.chunk_runner.write_ai_review_trigger_report", return_value={}),
                patch("football_tracking.chunk_runner.write_event_candidate_report", return_value={}),
                patch(
                    "football_tracking.chunk_runner.write_high_recall_window_report",
                    return_value={"windows": [], "summary": {"selected_window_count": 0}},
                ) as write_windows,
            ):
                run_high_recall_windows(config, source_total_frames=20)

        self.assertIsNone(write_windows.call_args.kwargs["approved_actions_path"])

    def test_runner_passes_configured_approved_actions_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            approved_path = output_dir / "approved" / "ai_improvement_approved_actions.json"
            approved_path.parent.mkdir()
            _write_json(approved_path, {"schema_version": "1.0", "approved_actions": []})
            config = SimpleNamespace(
                output_dir=output_dir,
                output=SimpleNamespace(csv_name="ball_track.csv", debug_jsonl_name="debug.jsonl"),
                detector=SimpleNamespace(inference_mode="direct_full_frame"),
                runtime=SimpleNamespace(start_frame=0, max_frames=None),
                high_recall_windows=SimpleNamespace(
                    enabled=True,
                    margin_frames=0,
                    merge_gap_frames=0,
                    max_total_frames=100,
                    mode="sahi",
                    output_dir_name="high_recall_windows",
                    approved_actions_path=f"  {approved_path}  ",
                    max_speed_px_per_frame=120.0,
                    max_jump_px=180.0,
                ),
            )

            with (
                patch("football_tracking.chunk_runner.write_ball_audit_report", return_value={}),
                patch("football_tracking.chunk_runner.write_ai_review_trigger_report", return_value={}),
                patch("football_tracking.chunk_runner.write_event_candidate_report", return_value={}),
                patch(
                    "football_tracking.chunk_runner.write_high_recall_window_report",
                    return_value={"windows": [], "summary": {"selected_window_count": 0}},
                ) as write_windows,
            ):
                run_high_recall_windows(config, source_total_frames=20)

        self.assertEqual(approved_path, write_windows.call_args.kwargs["approved_actions_path"])

    def test_runner_approved_only_skips_deterministic_report_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            approved_path = output_dir / "ai_improvement_approved_actions.json"
            _write_json(approved_path, {"schema_version": "1.0", "approved_actions": []})
            config = SimpleNamespace(
                output_dir=output_dir,
                output=SimpleNamespace(csv_name="ball_track.csv", debug_jsonl_name="debug.jsonl"),
                detector=SimpleNamespace(inference_mode="direct_full_frame"),
                runtime=SimpleNamespace(start_frame=0, max_frames=None),
                high_recall_windows=SimpleNamespace(
                    enabled=True,
                    margin_frames=0,
                    merge_gap_frames=0,
                    max_total_frames=100,
                    mode="sahi",
                    output_dir_name="high_recall_windows",
                    approved_actions_path=approved_path,
                    approved_only=True,
                    max_speed_px_per_frame=120.0,
                    max_jump_px=180.0,
                ),
            )

            with (
                patch("football_tracking.chunk_runner.write_ball_audit_report") as write_audit,
                patch("football_tracking.chunk_runner.write_ai_review_trigger_report") as write_ai_review,
                patch("football_tracking.chunk_runner.write_event_candidate_report") as write_events,
                patch(
                    "football_tracking.chunk_runner.write_high_recall_window_report",
                    return_value={"windows": [], "summary": {"selected_window_count": 0}},
                ) as write_windows,
            ):
                run_high_recall_windows(config, source_total_frames=20)

        write_audit.assert_not_called()
        write_ai_review.assert_not_called()
        write_events.assert_not_called()
        self.assertTrue(write_windows.call_args.kwargs["approved_only"])
        self.assertEqual(approved_path, write_windows.call_args.kwargs["approved_actions_path"])

    def test_runner_clears_stale_reconcile_report_when_no_windows_are_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "ball_track.csv").write_text(
                "Frame,X,Y,Confidence,Status\n10,10,10,0.90,Detected\n11,11,10,0.90,Detected\n",
                encoding="utf-8",
            )
            stale_root = output_dir / "high_recall_windows"
            stale_root.mkdir()
            (stale_root / "window_000").mkdir()
            (stale_root / "reconcile_report.json").write_text(
                json.dumps(
                    {
                        "review_packet_clues": [
                            {
                                "start_frame": 10,
                                "end_frame": 11,
                                "reason": "stale",
                                "priority": "high",
                                "rejection_reason": "jump_gate_failed",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            stale_custom_root = output_dir / "old_custom_windows"
            stale_custom_root.mkdir()
            (stale_custom_root / "window_000").mkdir()
            (stale_custom_root / "report.json").write_text(
                json.dumps(
                    {
                        "windows": [],
                        "rejected_windows": [
                            {
                                "start_frame": 10,
                                "end_frame": 11,
                                "reason": "stale custom",
                                "priority": "high",
                                "rejection_reason": "max_total_frames_exceeded",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config = SimpleNamespace(
                output_dir=output_dir,
                output=SimpleNamespace(csv_name="ball_track.csv", debug_jsonl_name="debug.jsonl"),
                detector=SimpleNamespace(inference_mode="direct_full_frame"),
                runtime=SimpleNamespace(start_frame=0, max_frames=None),
                high_recall_windows=SimpleNamespace(
                    enabled=True,
                    margin_frames=0,
                    merge_gap_frames=0,
                    max_total_frames=100,
                    mode="sahi",
                    output_dir_name="high_recall_windows",
                    max_speed_px_per_frame=120.0,
                    max_jump_px=180.0,
                ),
            )

            with (
                patch("football_tracking.chunk_runner.write_ball_audit_report", return_value={}),
                patch("football_tracking.chunk_runner.write_ai_review_trigger_report", return_value={}),
                patch("football_tracking.chunk_runner.write_event_candidate_report", return_value={}),
                patch(
                    "football_tracking.chunk_runner.write_high_recall_window_report",
                    return_value={"windows": [], "summary": {"selected_window_count": 0}},
                ),
            ):
                run_high_recall_windows(config, source_total_frames=20)

            packet_report = build_review_packet_report(output_dir, max_packets=2, include_media=False)
            self.assertFalse((stale_root / "reconcile_report.json").exists())
            self.assertFalse((stale_root / "window_000").exists())
            self.assertFalse((stale_custom_root / "report.json").exists())
            self.assertFalse((stale_custom_root / "window_000").exists())
            self.assertEqual(0, packet_report["summary"]["packet_count"])


class HighRecallConfigTests(unittest.TestCase):
    def test_load_config_parses_high_recall_windows_safely_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            repo_root = Path(temp_name)
            config_dir = repo_root / "config"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "high_recall.yaml"
            config_path.write_text(
                """
input_video: ./data/input.mp4
output_dir: ./outputs/run
detector:
  model_path: ./weights/model.pt
high_recall_windows:
  enabled: true
  margin_frames: 12
  merge_gap_frames: 44
  max_total_frames: 300
  mode: sahi
  output_dir_name: high_recall_windows
  approved_actions_path: ai_improvement_approved_actions.json
  approved_only: true
  max_speed_px_per_frame: 140.0
  max_jump_px: 220.0
""".strip()
                + "\n",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertTrue(config.high_recall_windows.enabled)
        self.assertEqual(12, config.high_recall_windows.margin_frames)
        self.assertEqual(30, config.high_recall_windows.merge_gap_frames)
        self.assertEqual(300, config.high_recall_windows.max_total_frames)
        self.assertEqual("sahi", config.high_recall_windows.mode)
        self.assertEqual("high_recall_windows", config.high_recall_windows.output_dir_name)
        self.assertEqual("ai_improvement_approved_actions.json", config.high_recall_windows.approved_actions_path)
        self.assertTrue(config.high_recall_windows.approved_only)
        self.assertEqual(140.0, config.high_recall_windows.max_speed_px_per_frame)
        self.assertEqual(220.0, config.high_recall_windows.max_jump_px)

    def test_load_config_uses_safe_high_recall_budget_when_enabled_minimally(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            repo_root = Path(temp_name)
            config_dir = repo_root / "config"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "high_recall_minimal.yaml"
            config_path.write_text(
                """
input_video: ./data/input.mp4
output_dir: ./outputs/run
detector:
  model_path: ./weights/model.pt
high_recall_windows:
  enabled: true
""".strip()
                + "\n",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertTrue(config.high_recall_windows.enabled)
        self.assertEqual(1800, config.high_recall_windows.max_total_frames)

    def test_load_config_allows_explicit_unbounded_high_recall_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            repo_root = Path(temp_name)
            config_dir = repo_root / "config"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "high_recall_unbounded.yaml"
            config_path.write_text(
                """
input_video: ./data/input.mp4
output_dir: ./outputs/run
detector:
  model_path: ./weights/model.pt
high_recall_windows:
  enabled: true
  max_total_frames:
""".strip()
                + "\n",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertIsNone(config.high_recall_windows.max_total_frames)

    def test_load_config_rejects_non_positive_high_recall_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            repo_root = Path(temp_name)
            config_dir = repo_root / "config"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "high_recall_bad_budget.yaml"
            config_path.write_text(
                """
input_video: ./data/input.mp4
output_dir: ./outputs/run
detector:
  model_path: ./weights/model.pt
high_recall_windows:
  enabled: true
  max_total_frames: 0
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_config(config_path)


if __name__ == "__main__":
    unittest.main()
