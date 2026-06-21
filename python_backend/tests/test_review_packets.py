from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

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
            self.assertEqual(2, report["summary"]["packet_count"])
            labels = {packet["decision"]["label"] for packet in report["packets"]}
            self.assertIn("needs_ai_review", labels)
            self.assertIn("highlight_worthy", labels)
            self.assertFalse(any(packet["source"]["id"].startswith("dense_noise_cluster") for packet in report["packets"]))

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
        self.assertEqual({"start_frame": 10, "end_frame": 13, "frame_count": 4}, packet["window"])

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


if __name__ == "__main__":
    unittest.main()
