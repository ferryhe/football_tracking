from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from football_tracking.review_packets import (
    build_review_packet_report,
    compact_review_packet_summary,
    write_review_packet_report,
)


class ReviewPacketTests(unittest.TestCase):
    def test_build_review_packet_report_selects_trigger_and_event_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            (output_dir / "ball_track.cleaned.csv").write_text(
                "\n".join(
                    [
                        "Frame,X,Y,Confidence,Status",
                        "10,100,100,0.90,Detected",
                        "11,110,100,0.88,Detected",
                        "12,300,120,0.30,Detected",
                        "13,310,120,0.25,Predicted",
                        "14,,,0.00,Lost",
                        "15,,,0.00,Lost",
                        "30,500,500,0.95,Detected",
                        "31,510,500,0.96,Detected",
                        "32,520,510,0.94,Detected",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (output_dir / "ai_review_triggers.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "decision": {"needs_ai_review": True},
                        "triggers": [
                            {
                                "id": "event:0:large_jump:10-13",
                                "type": "large_jump",
                                "priority": "high",
                                "source": "cleaned",
                                "start_frame": 10,
                                "end_frame": 13,
                                "frame_count": 4,
                                "reason": "large jump",
                                "evidence": {"max_step_px": 190.0},
                            },
                            {
                                "id": "dense_noise_cluster:10-999",
                                "type": "dense_noise_cluster",
                                "priority": "high",
                                "source": "ball_audit",
                                "start_frame": 10,
                                "end_frame": 999,
                                "frame_count": 990,
                                "reason": "dense",
                                "evidence": {},
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "event_candidates.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "source": {"name": "cleaned", "path": "ball_track.cleaned.csv", "row_count": 9},
                        "summary": {"candidate_count": 1},
                        "candidates": [
                            {
                                "id": "cleaned:goal_candidate:30-32",
                                "type": "goal_candidate",
                                "start_frame": 30,
                                "end_frame": 32,
                                "frame_count": 3,
                                "score": 0.96,
                                "reason": "goal zone burst",
                                "render_window": {"start_frame": 25, "end_frame": 40},
                                "evidence": {},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_review_packet_report(output_dir, max_packets=4, include_media=False)

            self.assertEqual("1.0", report["schema_version"])
            self.assertEqual(4, report["summary"]["packet_count"])
            labels = {packet["decision"]["label"] for packet in report["packets"]}
            self.assertIn("needs_ai_review", labels)
            self.assertIn("highlight_worthy", labels)
            dense_packets = [
                packet for packet in report["packets"] if packet["source"]["id"].startswith("dense_noise_cluster")
            ]
            self.assertTrue(dense_packets)
            self.assertTrue(all(packet["window"]["frame_count"] <= 96 for packet in dense_packets))

    def test_build_review_packet_report_reserves_space_for_triggers_when_events_fill_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            rows = ["Frame,X,Y,Confidence,Status"]
            rows.extend(f"{frame},{100 + frame},{100 + frame},0.90,Detected" for frame in range(80))
            (output_dir / "ball_track.cleaned.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
            (output_dir / "event_candidates.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "candidates": [
                            {
                                "id": f"candidate:{index}",
                                "type": "goal_candidate",
                                "start_frame": 10 + index,
                                "end_frame": 10 + index,
                                "score": 0.95 - index * 0.01,
                                "reason": "goal",
                                "evidence": {},
                            }
                            for index in range(6)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "ai_review_triggers.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "decision": {"needs_ai_review": True},
                        "triggers": [
                            {
                                "id": "event:0:large_jump:40-41",
                                "type": "large_jump",
                                "priority": "high",
                                "source": "cleaned",
                                "start_frame": 40,
                                "end_frame": 41,
                                "frame_count": 2,
                                "reason": "large jump",
                                "evidence": {"max_step_px": 400.0},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_review_packet_report(output_dir, max_packets=3, include_media=False)

            source_kinds = [packet["source"]["kind"] for packet in report["packets"]]
            self.assertIn("event_candidate", source_kinds)
            self.assertIn("trigger", source_kinds)

    def test_build_review_packet_report_reserves_space_for_long_lost_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            rows = ["Frame,X,Y,Confidence,Status"]
            for frame in range(2601):
                if 2049 <= frame <= 2544:
                    rows.append(f"{frame},,,0.00,Lost")
                else:
                    rows.append(f"{frame},{100 + frame * 0.1:.1f},100,0.90,Detected")
            (output_dir / "ball_track.cleaned.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
            (output_dir / "event_candidates.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "candidates": [
                            {
                                "id": f"goal:{index}",
                                "type": "goal_candidate",
                                "start_frame": 100 + index * 10,
                                "end_frame": 104 + index * 10,
                                "score": 0.96 - index * 0.01,
                                "reason": "goal",
                                "evidence": {},
                            }
                            for index in range(8)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            triggers = [
                {
                    "id": f"event:{index}:large_jump:{300 + index * 10}-{301 + index * 10}",
                    "type": "large_jump",
                    "priority": "high",
                    "source": "cleaned",
                    "start_frame": 300 + index * 10,
                    "end_frame": 301 + index * 10,
                    "frame_count": 2,
                    "reason": "large jump",
                    "evidence": {"max_step_px": 400.0},
                }
                for index in range(8)
            ]
            triggers.append(
                {
                    "id": "event:61:lost_gap:2049-2544",
                    "type": "lost_gap",
                    "priority": "medium",
                    "source": "cleaned",
                    "start_frame": 2049,
                    "end_frame": 2544,
                    "frame_count": 496,
                    "reason": "Ball track is lost for 496 frames between tracklets.",
                    "evidence": {"lost_frame_count": 496},
                }
            )
            (output_dir / "ai_review_triggers.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "decision": {"needs_ai_review": True},
                        "triggers": triggers,
                    }
                ),
                encoding="utf-8",
            )

            report = build_review_packet_report(output_dir, max_packets=10, include_media=False)

        lost_gap_packets = [
            packet
            for packet in report["packets"]
            if packet["source"]["type"] == "lost_gap"
            and packet["source"]["start_frame"] == 2049
            and packet["source"]["end_frame"] == 2544
        ]
        self.assertEqual(1, len(lost_gap_packets))
        self.assertEqual("ball_not_visible", lost_gap_packets[0]["decision"]["label"])

    def test_build_review_packet_report_keeps_oversized_long_lost_gap_for_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            rows = ["Frame,X,Y,Confidence,Status"]
            for frame in range(900):
                if 100 <= frame <= 800:
                    rows.append(f"{frame},,,0.00,Lost")
                else:
                    rows.append(f"{frame},{100 + frame * 0.1:.1f},100,0.90,Detected")
            (output_dir / "ball_track.cleaned.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
            (output_dir / "ai_review_triggers.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "decision": {"needs_ai_review": True},
                        "triggers": [
                            {
                                "id": "event:1:lost_gap:100-800",
                                "type": "lost_gap",
                                "priority": "medium",
                                "source": "cleaned",
                                "start_frame": 100,
                                "end_frame": 800,
                                "frame_count": 701,
                                "reason": "Ball track is lost for 701 frames between tracklets.",
                                "evidence": {"lost_frame_count": 701},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_review_packet_report(output_dir, max_packets=10, include_media=False)

        self.assertEqual(1, report["summary"]["packet_count"])
        packet = report["packets"][0]
        self.assertEqual("lost_gap", packet["source"]["type"])
        self.assertEqual({"start_frame": 70, "end_frame": 830, "frame_count": 761}, packet["window"])

    def test_build_review_packet_report_includes_rejected_high_recall_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            rows = ["Frame,X,Y,Confidence,Status"]
            rows.extend(
                [
                    "10,100,100,0.90,Detected",
                    "11,,,0.00,Lost",
                    "12,,,0.00,Lost",
                    "13,130,100,0.90,Detected",
                ]
            )
            (output_dir / "ball_track.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
            report_dir = output_dir / "high_recall_windows"
            report_dir.mkdir()
            (report_dir / "reconcile_report.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "review_packet_clues": [
                            {
                                "start_frame": 11,
                                "end_frame": 12,
                                "reason": "lost_gap",
                                "priority": "high",
                                "rejection_reason": "jump_gate_failed",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_review_packet_report(output_dir, max_packets=2, include_media=False)

        self.assertEqual(1, report["summary"]["packet_count"])
        packet = report["packets"][0]
        self.assertEqual("high_recall_rejection", packet["source"]["kind"])
        self.assertEqual("needs_ai_review", packet["decision"]["label"])
        self.assertEqual({"start_frame": 11, "end_frame": 12, "frame_count": 2}, packet["window"])

    def test_build_review_packet_report_includes_budget_rejected_high_recall_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            rows = ["Frame,X,Y,Confidence,Status"]
            rows.extend(f"{frame},{100 + frame},{100},0.90,Detected" for frame in range(90))
            (output_dir / "ball_track.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
            report_dir = output_dir / "high_recall_windows"
            report_dir.mkdir()
            (report_dir / "report.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "windows": [],
                        "rejected_windows": [
                            {
                                "start_frame": 40,
                                "end_frame": 45,
                                "reason": "ball_audit: lost_gap",
                                "priority": "high",
                                "rejection_reason": "max_total_frames_exceeded",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_review_packet_report(output_dir, max_packets=2, include_media=False)

        self.assertEqual(1, report["summary"]["packet_count"])
        packet = report["packets"][0]
        self.assertEqual("high_recall_rejection", packet["source"]["kind"])
        self.assertEqual("max_total_frames_exceeded", packet["source"]["evidence"]["rejection_reason"])
        self.assertEqual("needs_ai_review", packet["decision"]["label"])

    def test_build_review_packet_report_reserves_long_high_recall_lost_gap_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            rows = ["Frame,X,Y,Confidence,Status"]
            for frame in range(2601):
                if 2049 <= frame <= 2544:
                    rows.append(f"{frame},,,0.00,Lost")
                else:
                    rows.append(f"{frame},{100 + frame * 0.1:.1f},100,0.90,Detected")
            (output_dir / "ball_track.cleaned.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
            (output_dir / "event_candidates.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "candidates": [
                            {
                                "id": f"goal:{index}",
                                "type": "goal_candidate",
                                "start_frame": 100 + index * 10,
                                "end_frame": 104 + index * 10,
                                "score": 0.96 - index * 0.01,
                                "reason": "goal",
                                "evidence": {},
                            }
                            for index in range(8)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "ai_review_triggers.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "decision": {"needs_ai_review": True},
                        "triggers": [
                            {
                                "id": f"event:{index}:large_jump:{300 + index * 10}-{301 + index * 10}",
                                "type": "large_jump",
                                "priority": "high",
                                "source": "cleaned",
                                "start_frame": 300 + index * 10,
                                "end_frame": 301 + index * 10,
                                "frame_count": 2,
                                "reason": "large jump",
                                "evidence": {"max_step_px": 400.0},
                            }
                            for index in range(8)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            report_dir = output_dir / "high_recall_windows"
            report_dir.mkdir()
            (report_dir / "report.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "windows": [],
                        "rejected_windows": [
                            {
                                "start_frame": 2049,
                                "end_frame": 2544,
                                "reason": "ball_audit: lost_gap",
                                "priority": "medium",
                                "rejection_reason": "max_total_frames_exceeded",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_review_packet_report(output_dir, max_packets=10, include_media=False)

        high_recall_lost_gap_packets = [
            packet
            for packet in report["packets"]
            if packet["source"]["kind"] == "high_recall_rejection"
            and packet["source"]["start_frame"] == 2049
            and packet["source"]["end_frame"] == 2544
        ]
        self.assertEqual(1, len(high_recall_lost_gap_packets))
        packet = high_recall_lost_gap_packets[0]
        self.assertEqual("ball_audit: lost_gap", packet["source"]["evidence"]["window_reason"])
        self.assertEqual("needs_ai_review", packet["decision"]["label"])

    def test_build_review_packet_report_discovers_custom_high_recall_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            rows = ["Frame,X,Y,Confidence,Status"]
            rows.extend(f"{frame},{frame},{frame},0.90,Detected" for frame in range(30))
            (output_dir / "ball_track.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
            report_dir = output_dir / "second_pass_windows"
            report_dir.mkdir()
            (report_dir / "report.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "windows": [],
                        "rejected_windows": [
                            {
                                "start_frame": 8,
                                "end_frame": 10,
                                "reason": "ai_review: large_jump",
                                "priority": "medium",
                                "rejection_reason": "max_total_frames_exceeded",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_review_packet_report(output_dir, max_packets=2, include_media=False)

        self.assertEqual(1, report["summary"]["packet_count"])
        self.assertEqual("high_recall_rejection", report["packets"][0]["source"]["kind"])

    def test_build_review_packet_report_does_not_duplicate_embedded_and_standalone_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            rows = ["Frame,X,Y,Confidence,Status"]
            rows.extend(f"{frame},{frame},{frame},0.90,Detected" for frame in range(30))
            (output_dir / "ball_track.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
            report_dir = output_dir / "high_recall_windows"
            report_dir.mkdir()
            reconcile = {
                "review_packet_clues": [
                    {
                        "start_frame": 8,
                        "end_frame": 10,
                        "reason": "lost_gap",
                        "priority": "high",
                        "rejection_reason": "jump_gate_failed",
                    }
                ]
            }
            (report_dir / "report.json").write_text(
                json.dumps({"schema_version": "1.0", "windows": [], "reconcile": reconcile}),
                encoding="utf-8",
            )
            (report_dir / "reconcile_report.json").write_text(json.dumps(reconcile), encoding="utf-8")

            report = build_review_packet_report(output_dir, max_packets=4, include_media=False)

        sources = [packet["source"] for packet in report["packets"] if packet["source"]["kind"] == "high_recall_rejection"]
        self.assertEqual(1, len(sources))

    def test_build_review_packet_report_splits_large_high_recall_rejection_around_failure_anchors(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            _write_detected_track(output_dir, 5300)
            (output_dir / "ball_audit.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "review_events": [
                            {
                                "type": "large_jump",
                                "severity": "fail",
                                "start_frame": 480,
                                "end_frame": 480,
                                "frame_count": 1,
                                "reason": "large jump after reacquire",
                                "evidence": {"max_step_px": 420.0},
                            },
                            {
                                "type": "lost_gap",
                                "severity": "warn",
                                "start_frame": 1180,
                                "end_frame": 1250,
                                "frame_count": 71,
                                "reason": "Ball track is lost between tracklets.",
                                "evidence": {"gap_frames": 71},
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "cleanup_report.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "actions": [
                            {
                                "start_frame": 1800,
                                "end_frame": 1802,
                                "island_length": 3,
                                "reason": "postprocess_short_detected_island",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "camera_motion_audit.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "review_events": [
                            {
                                "type": "camera_motion_spike",
                                "severity": "warn",
                                "start_frame": 2400,
                                "end_frame": 2400,
                                "frame_count": 1,
                                "reason": "Camera catch-up spike.",
                                "evidence": {"pan_modes": ["catch_up"]},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            report_dir = output_dir / "high_recall_windows"
            report_dir.mkdir()
            (report_dir / "report.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "windows": [],
                        "rejected_windows": [
                            {
                                "start_frame": 118,
                                "end_frame": 5191,
                                "reason": "high_recall mixed failure window",
                                "priority": "high",
                                "rejection_reason": "max_total_frames_exceeded",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_review_packet_report(output_dir, max_packets=6, include_media=False)

        high_recall_packets = [
            packet for packet in report["packets"] if packet["source"]["kind"] == "high_recall_rejection"
        ]
        self.assertGreaterEqual(len(high_recall_packets), 4)
        self.assertLessEqual(len(high_recall_packets), 6)
        self.assertTrue(all(packet["window"]["frame_count"] <= 96 for packet in high_recall_packets))
        self.assertTrue(all("_118_5191" not in packet["packet_id"] for packet in high_recall_packets))
        for packet in high_recall_packets:
            self.assertEqual(
                {"start_frame": 118, "end_frame": 5191, "frame_count": 5074},
                packet["source"]["evidence"]["parent_window"],
            )
            self.assertEqual(
                {"start_frame": 118, "end_frame": 5191, "frame_count": 5074},
                packet["parent_window"],
            )
            self.assertEqual("high_recall_rejection:0:118-5191", packet["source_packet_id"])
            self.assertTrue(packet["suspected_failure_tags"])
            self.assertTrue(packet["root_cause_candidates"])
            self.assertIsInstance(packet["packet_purpose"], str)

        windows = [(packet["source"]["start_frame"], packet["source"]["end_frame"]) for packet in high_recall_packets]
        self.assertTrue(any(start <= 480 <= end for start, end in windows))
        self.assertTrue(any(start <= 1180 <= end for start, end in windows))
        self.assertTrue(any(start <= 1250 <= end for start, end in windows))
        self.assertTrue(any(start <= 1800 <= end for start, end in windows))
        self.assertTrue(any(start <= 2400 <= end for start, end in windows))
        camera_packet = next(
            packet for packet in high_recall_packets if packet["source"]["start_frame"] <= 2400 <= packet["source"]["end_frame"]
        )
        self.assertIn("camera_catchup_spike", camera_packet["suspected_failure_tags"])
        self.assertIn("follow_cam", camera_packet["root_cause_candidates"])

    def test_build_review_packet_report_splits_large_high_recall_rejection_without_anchors_across_span(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            _write_detected_track(output_dir, 5300)
            report_dir = output_dir / "high_recall_windows"
            report_dir.mkdir()
            (report_dir / "report.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "windows": [],
                        "rejected_windows": [
                            {
                                "start_frame": 118,
                                "end_frame": 5191,
                                "reason": "high_recall mixed failure window",
                                "priority": "medium",
                                "rejection_reason": "max_total_frames_exceeded",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_review_packet_report(output_dir, max_packets=3, include_media=False)

        high_recall_packets = [
            packet for packet in report["packets"] if packet["source"]["kind"] == "high_recall_rejection"
        ]
        self.assertEqual(3, len(high_recall_packets))
        self.assertTrue(all(packet["window"]["frame_count"] <= 96 for packet in high_recall_packets))
        centers = [
            (packet["source"]["start_frame"] + packet["source"]["end_frame"]) // 2
            for packet in high_recall_packets
        ]
        self.assertEqual(sorted(centers), centers)
        self.assertGreater(centers[0], 1000)
        self.assertLess(centers[-1], 4300)

    def test_build_review_packet_report_splits_large_high_recall_rejection_with_lost_gap_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            _write_detected_track(output_dir, 5300)
            report_dir = output_dir / "high_recall_windows"
            report_dir.mkdir()
            (report_dir / "report.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "windows": [],
                        "rejected_windows": [
                            {
                                "start_frame": 118,
                                "end_frame": 5191,
                                "reason": "lost_gap-like noisy rejection but not ball_audit source",
                                "priority": "medium",
                                "rejection_reason": "lost_gap text in reason",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_review_packet_report(output_dir, max_packets=2, include_media=False)

        high_recall_packets = [
            packet for packet in report["packets"] if packet["source"]["kind"] == "high_recall_rejection"
        ]
        self.assertEqual(2, len(high_recall_packets))
        self.assertTrue(all(packet["window"]["frame_count"] <= 96 for packet in high_recall_packets))

    def test_build_review_packet_report_splits_dense_noise_cluster_for_diagnosis(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            _write_detected_track(output_dir, 1300)
            (output_dir / "ai_review_triggers.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "decision": {"needs_ai_review": True},
                        "triggers": [
                            {
                                "id": "dense_noise_cluster:100-1199",
                                "type": "dense_noise_cluster",
                                "priority": "high",
                                "start_frame": 100,
                                "end_frame": 1199,
                                "frame_count": 1100,
                                "reason": "dense foot and sideline-like false positives near wall background",
                                "evidence": {"peak_frames": [180, 640, 1120]},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_review_packet_report(output_dir, max_packets=3, include_media=False)

        dense_packets = [
            packet for packet in report["packets"] if packet["source"]["type"] == "dense_noise_cluster"
        ]
        self.assertEqual(3, len(dense_packets))
        self.assertTrue(all(packet["window"]["frame_count"] <= 96 for packet in dense_packets))
        self.assertTrue(
            all(packet["source"]["evidence"]["parent_window"] == {"start_frame": 100, "end_frame": 1199, "frame_count": 1100} for packet in dense_packets)
        )
        self.assertTrue(
            all(packet["parent_window"] == {"start_frame": 100, "end_frame": 1199, "frame_count": 1100} for packet in dense_packets)
        )
        self.assertTrue(all(packet["source_packet_id"] == "dense_noise_cluster:100-1199" for packet in dense_packets))
        self.assertTrue(all(packet["packet_purpose"] == "diagnose_noise" for packet in dense_packets))
        for packet in dense_packets:
            self.assertIn("foot_confusion", packet["suspected_failure_tags"])
            self.assertIn("sideline_confusion", packet["suspected_failure_tags"])
            self.assertIn("wall_background_drift", packet["suspected_failure_tags"])
            self.assertIn("detection", packet["root_cause_candidates"])

    def test_build_review_packet_report_anchors_dense_noise_cluster_on_cleanup_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            _write_detected_track(output_dir, 1300)
            (output_dir / "ai_review_triggers.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "triggers": [
                            {
                                "id": "dense_noise_cluster:100-1199",
                                "type": "dense_noise_cluster",
                                "priority": "high",
                                "start_frame": 100,
                                "end_frame": 1199,
                                "reason": "dense shoe-like false positives",
                                "evidence": {},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "cleanup_report.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "actions": [
                            {"start_frame": 777, "end_frame": 779, "reason": "postprocess_short_detected_island"}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_review_packet_report(output_dir, max_packets=2, include_media=False)

        dense_packets = [
            packet for packet in report["packets"] if packet["source"]["type"] == "dense_noise_cluster"
        ]
        self.assertTrue(any(packet["source"]["start_frame"] <= 777 <= packet["source"]["end_frame"] for packet in dense_packets))
        self.assertTrue(all(packet["window"]["frame_count"] <= 96 for packet in dense_packets))

    def test_build_review_packet_report_keeps_small_diagnostic_sources_within_source_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            _write_detected_track(output_dir, 300)
            (output_dir / "ai_review_triggers.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "triggers": [
                            {
                                "id": "dense_noise_cluster:100-150",
                                "type": "dense_noise_cluster",
                                "priority": "high",
                                "start_frame": 100,
                                "end_frame": 150,
                                "reason": "dense sideline false positives",
                                "evidence": {},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_review_packet_report(output_dir, max_packets=1, include_media=False)

        self.assertEqual({"start_frame": 100, "end_frame": 150, "frame_count": 51}, report["packets"][0]["window"])

    def test_build_review_packet_report_keeps_small_high_recall_source_within_source_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            _write_detected_track(output_dir, 300)
            report_dir = output_dir / "high_recall_windows"
            report_dir.mkdir()
            (report_dir / "report.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "windows": [],
                        "rejected_windows": [
                            {
                                "start_frame": 100,
                                "end_frame": 150,
                                "reason": "high_recall short rejection",
                                "priority": "high",
                                "rejection_reason": "short_noise",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_review_packet_report(output_dir, max_packets=1, include_media=False)

        self.assertEqual({"start_frame": 100, "end_frame": 150, "frame_count": 51}, report["packets"][0]["window"])

    def test_build_review_packet_report_interleaves_dense_noise_and_high_recall_sources_under_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            _write_detected_track(output_dir, 1500)
            (output_dir / "ai_review_triggers.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "triggers": [
                            {
                                "id": "dense_noise_cluster:100-1199",
                                "type": "dense_noise_cluster",
                                "priority": "high",
                                "start_frame": 100,
                                "end_frame": 1199,
                                "reason": "dense foot false positives",
                                "evidence": {"peak_frames": [180, 640, 1120]},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            report_dir = output_dir / "high_recall_windows"
            report_dir.mkdir()
            (report_dir / "report.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "windows": [],
                        "rejected_windows": [
                            {
                                "start_frame": 300,
                                "end_frame": 900,
                                "reason": "high_recall mixed failure window",
                                "priority": "high",
                                "rejection_reason": "max_total_frames_exceeded",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_review_packet_report(output_dir, max_packets=4, include_media=False)

        source_kinds = [packet["source"]["kind"] for packet in report["packets"]]
        self.assertIn("trigger", source_kinds)
        self.assertIn("high_recall_rejection", source_kinds)

    def test_build_review_packet_report_reserves_high_recall_when_dense_noise_budget_is_tight(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            _write_detected_track(output_dir, 1500)
            (output_dir / "ai_review_triggers.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "triggers": [
                            {
                                "id": "dense_noise_cluster:100-600",
                                "type": "dense_noise_cluster",
                                "priority": "high",
                                "start_frame": 100,
                                "end_frame": 600,
                                "reason": "dense foot false positives",
                                "evidence": {"peak_frames": [120, 300, 580]},
                            },
                            {
                                "id": "dense_noise_cluster:700-1200",
                                "type": "dense_noise_cluster",
                                "priority": "high",
                                "start_frame": 700,
                                "end_frame": 1200,
                                "reason": "dense sideline false positives",
                                "evidence": {"peak_frames": [720, 950, 1180]},
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            report_dir = output_dir / "high_recall_windows"
            report_dir.mkdir()
            (report_dir / "report.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "windows": [],
                        "rejected_windows": [
                            {
                                "start_frame": 300,
                                "end_frame": 900,
                                "reason": "high_recall mixed failure window",
                                "priority": "high",
                                "rejection_reason": "max_total_frames_exceeded",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            one_packet_report = build_review_packet_report(output_dir, max_packets=1, include_media=False)
            two_packet_report = build_review_packet_report(output_dir, max_packets=2, include_media=False)

        self.assertEqual("high_recall_rejection", one_packet_report["packets"][0]["source"]["kind"])
        self.assertIn("high_recall_rejection", [packet["source"]["kind"] for packet in two_packet_report["packets"]])
        self.assertIn("trigger", [packet["source"]["kind"] for packet in two_packet_report["packets"]])

    def test_build_review_packet_report_prioritizes_high_recall_and_dense_noise_over_events_under_tight_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            _write_detected_track(output_dir, 1500)
            (output_dir / "event_candidates.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "candidates": [
                            {
                                "id": "goal:100",
                                "type": "goal_candidate",
                                "start_frame": 100,
                                "end_frame": 104,
                                "score": 0.99,
                                "reason": "goal",
                                "evidence": {},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "ai_review_triggers.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "triggers": [
                            {
                                "id": "dense_noise_cluster:700-1200",
                                "type": "dense_noise_cluster",
                                "priority": "high",
                                "start_frame": 700,
                                "end_frame": 1200,
                                "reason": "dense sideline false positives",
                                "evidence": {"peak_frames": [720, 950, 1180]},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            report_dir = output_dir / "high_recall_windows"
            report_dir.mkdir()
            (report_dir / "report.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "windows": [],
                        "rejected_windows": [
                            {
                                "start_frame": 300,
                                "end_frame": 900,
                                "reason": "high_recall mixed failure window",
                                "priority": "high",
                                "rejection_reason": "max_total_frames_exceeded",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_review_packet_report(output_dir, max_packets=2, include_media=False)

        source_kinds = [packet["source"]["kind"] for packet in report["packets"]]
        self.assertEqual(["high_recall_rejection", "trigger"], source_kinds)

    def test_build_review_packet_report_records_frame_dimensions_from_media(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            input_video = output_dir / "input.mp4"
            _write_video(input_video, width=160, height=90, frame_count=5)
            rows = ["Frame,X,Y,Confidence,Status"]
            rows.extend(f"{frame},80,45,0.90,Detected" for frame in range(5))
            (output_dir / "ball_track.cleaned.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
            (output_dir / "ai_review_triggers.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "triggers": [
                            {
                                "id": "event:large_jump:0-4",
                                "type": "large_jump",
                                "priority": "high",
                                "start_frame": 0,
                                "end_frame": 4,
                                "reason": "large jump",
                                "evidence": {},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_review_packet_report(
                output_dir,
                input_video=input_video,
                max_packets=1,
                include_media=True,
            )

        self.assertEqual({"width": 160, "height": 90}, report["packets"][0]["frame_dimensions"])

    def test_write_review_packet_report_persists_packet_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            (output_dir / "ball_track.cleaned.csv").write_text(
                "Frame,X,Y,Confidence,Status\n1,10,10,0.9,Detected\n2,,,0.0,Lost\n",
                encoding="utf-8",
            )
            (output_dir / "ai_review_triggers.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "decision": {"needs_ai_review": True},
                        "triggers": [
                            {
                                "id": "event:1:lost_gap:1-2",
                                "type": "lost_gap",
                                "priority": "medium",
                                "source": "cleaned",
                                "start_frame": 1,
                                "end_frame": 2,
                                "frame_count": 2,
                                "reason": "lost",
                                "evidence": {},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = write_review_packet_report(output_dir, max_packets=2, include_media=False)

            self.assertTrue((output_dir / "review_packets.json").exists())
            packet_dir = output_dir / "review_packets" / report["packets"][0]["packet_id"]
            self.assertTrue((packet_dir / "manifest.json").exists())
            manifest = json.loads((packet_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(report["packets"][0]["decision"], manifest["decision"])

    def test_compact_review_packet_summary_tolerates_malformed_counts(self) -> None:
        summary = compact_review_packet_summary(
                    {
                        "schema_version": "1.0",
                        "summary": {
                            "packet_count": "inf",
                            "media_packet_count": "not-a-number",
                            "counts_by_label": {"needs_ai_review": 1},
                        },
                    }
        )

        self.assertEqual(0, summary["packet_count"])
        self.assertEqual(0, summary["media_packet_count"])
        self.assertEqual({"needs_ai_review": 1}, summary["counts_by_label"])


    def test_build_review_packets_cli_writes_report_without_media(self) -> None:
        from scripts.build_review_packets import main

        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            (output_dir / "ball_track.cleaned.csv").write_text(
                "Frame,X,Y,Confidence,Status\n1,10,10,0.9,Detected\n2,250,10,0.8,Detected\n",
                encoding="utf-8",
            )
            (output_dir / "ai_review_triggers.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "decision": {"needs_ai_review": True},
                        "triggers": [
                            {
                                "id": "event:1:large_jump:1-2",
                                "type": "large_jump",
                                "priority": "high",
                                "source": "cleaned",
                                "start_frame": 1,
                                "end_frame": 2,
                                "frame_count": 2,
                                "reason": "large jump",
                                "evidence": {},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = main([str(output_dir), "--max-packets", "1", "--no-media"])

            self.assertEqual(0, exit_code)
            report = json.loads((output_dir / "review_packets.json").read_text(encoding="utf-8"))
            self.assertEqual(0, report["summary"]["media_packet_count"])
            self.assertEqual({}, report["packets"][0]["media"])
            packet_dir = output_dir / "review_packets" / report["packets"][0]["packet_id"]
            self.assertTrue((packet_dir / "manifest.json").exists())

    def test_build_review_packets_cli_refreshes_existing_metrics_report(self) -> None:
        from scripts.build_review_packets import main

        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp)
            (output_dir / "ball_track.cleaned.csv").write_text(
                "Frame,X,Y,Confidence,Status\n1,10,10,0.9,Detected\n2,250,10,0.8,Detected\n",
                encoding="utf-8",
            )
            (output_dir / "ai_review_triggers.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "decision": {"needs_ai_review": True},
                        "triggers": [
                            {
                                "id": "event:1:large_jump:1-2",
                                "type": "large_jump",
                                "priority": "high",
                                "source": "cleaned",
                                "start_frame": 1,
                                "end_frame": 2,
                                "frame_count": 2,
                                "reason": "large jump",
                                "evidence": {},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "metrics_report.json").write_text(
                json.dumps({"schema_version": "1.0", "tracks": {}}),
                encoding="utf-8",
            )

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = main([str(output_dir), "--max-packets", "1", "--no-media"])

            self.assertEqual(0, exit_code)
            metrics_report = json.loads((output_dir / "metrics_report.json").read_text(encoding="utf-8"))
            self.assertEqual(1, metrics_report["review_packets"]["packet_count"])

    def test_build_review_packets_cli_rejects_bad_inputs(self) -> None:
        from scripts.build_review_packets import main

        with tempfile.TemporaryDirectory() as temp:
            missing_dir = Path(temp) / "missing"
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as context:
                    main([str(missing_dir), "--max-packets", "0", "--no-media"])

        self.assertNotEqual(0, context.exception.code)


def _write_detected_track(output_dir: Path, frame_count: int) -> None:
    rows = ["Frame,X,Y,Confidence,Status"]
    rows.extend(f"{frame},{100 + frame * 0.1:.1f},100,0.90,Detected" for frame in range(frame_count))
    (output_dir / "ball_track.cleaned.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _write_video(path: Path, *, width: int, height: int, frame_count: int) -> None:
    writer = cv2.VideoWriter(
        str(path),
        cv2.VideoWriter.fourcc(*"mp4v"),
        6.0,
        (width, height),
    )
    if not writer.isOpened():
        raise unittest.SkipTest("OpenCV video writer is unavailable in this environment.")
    for frame_index in range(frame_count):
        frame = np.full((height, width, 3), frame_index * 20, dtype=np.uint8)
        writer.write(frame)
    writer.release()


if __name__ == "__main__":
    unittest.main()
