from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from football_tracking.ball_audit import build_ball_audit_report, write_ball_audit_report


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    headers = ["Frame", "X", "Y", "Confidence", "Status"]
    lines = [",".join(headers)]
    for row in rows:
        lines.append(",".join(str(row.get(header, "")) for header in headers))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class BallAuditTests(unittest.TestCase):
    def test_build_ball_audit_report_groups_tracklets_and_review_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            write_csv(
                output_dir / "ball_track.csv",
                [
                    {"Frame": 0, "X": 10, "Y": 10, "Confidence": 0.90, "Status": "Detected"},
                    {"Frame": 1, "X": 20, "Y": 20, "Confidence": 0.80, "Status": "Detected"},
                    {"Frame": 2, "Confidence": 0.00, "Status": "Lost"},
                    {"Frame": 3, "Confidence": 0.00, "Status": "Lost"},
                    {"Frame": 4, "Confidence": 0.00, "Status": "Lost"},
                    {"Frame": 5, "X": 210, "Y": 210, "Confidence": 0.40, "Status": "Detected"},
                    {"Frame": 6, "Confidence": 0.00, "Status": "Lost"},
                    {"Frame": 7, "X": 15, "Y": 15, "Confidence": 0.90, "Status": "Detected"},
                    {"Frame": 8, "X": 220, "Y": 15, "Confidence": 0.88, "Status": "Detected"},
                    {"Frame": 9, "X": 225, "Y": 16, "Confidence": 0.86, "Status": "Predicted"},
                ],
            )
            write_csv(
                output_dir / "ball_track.cleaned.csv",
                [
                    {"Frame": 0, "X": 10, "Y": 10, "Confidence": 0.90, "Status": "Detected"},
                    {"Frame": 1, "X": 20, "Y": 20, "Confidence": 0.80, "Status": "Detected"},
                    {"Frame": 2, "X": 30, "Y": 25, "Confidence": 0.70, "Status": "Predicted"},
                    {"Frame": 3, "Confidence": 0.00, "Status": "Lost"},
                ],
            )
            (output_dir / "debug.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "frame": 8,
                                "candidate_scores": [
                                    {"total_score": 0.73, "candidate_center": [220, 15]},
                                    {"total_score": 0.68, "candidate_center": [210, 17]},
                                ],
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (output_dir / "cleanup_report.json").write_text(
                json.dumps(
                    {
                        "actions": [
                            {
                                "start_frame": 5,
                                "end_frame": 5,
                                "action": "scrub",
                                "reason": "short false positive island",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            report = build_ball_audit_report(output_dir)

        self.assertEqual("1.0", report["schema_version"])
        self.assertEqual(10, report["summary"]["frame_count"])
        self.assertEqual(2, report["summary"]["source_count"])
        self.assertEqual(4, report["summary"]["tracklet_count"])
        self.assertEqual(3, report["summary"]["suspicious_tracklet_count"])
        self.assertEqual(1, report["summary"]["lost_gap_count"])
        self.assertEqual(205.0, report["summary"]["max_step_px"])
        self.assertEqual(["raw", "cleaned"], [source["name"] for source in report["sources"]])

        raw_short = next(item for item in report["tracklets"] if item["source"] == "raw" and item["start_frame"] == 5)
        self.assertEqual(["short_tracklet", "low_confidence"], raw_short["flags"])
        self.assertGreater(raw_short["suspicion_score"], 0)
        self.assertEqual({"x": 210.0, "y": 210.0}, raw_short["start_point"])
        self.assertEqual({"Detected": 1}, raw_short["status_counts"])

        large_jump = next(item for item in report["tracklets"] if item["source"] == "raw" and item["start_frame"] == 7)
        self.assertIn("large_jump", large_jump["flags"])
        self.assertEqual(205.0, large_jump["max_step_px"])

        event_types = [event["type"] for event in report["review_events"]]
        self.assertIn("short_tracklet", event_types)
        self.assertIn("low_confidence", event_types)
        self.assertIn("large_jump", event_types)
        self.assertIn("lost_gap", event_types)
        self.assertIn("candidate_ambiguity", event_types)
        self.assertIn("postprocess_action", event_types)
        ambiguity = next(event for event in report["review_events"] if event["type"] == "candidate_ambiguity")
        self.assertEqual("warn", ambiguity["severity"])
        self.assertEqual(8, ambiguity["start_frame"])
        self.assertLessEqual(ambiguity["evidence"]["score_delta"], 0.08)

    def test_write_ball_audit_report_persists_stable_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            write_csv(
                output_dir / "ball_track.csv",
                [{"Frame": 0, "X": 1, "Y": 2, "Confidence": 0.90, "Status": "Detected"}],
            )

            report = write_ball_audit_report(output_dir)
            loaded = json.loads((output_dir / "ball_audit.json").read_text(encoding="utf-8"))

        self.assertEqual(report["summary"], loaded["summary"])
        self.assertEqual("raw:0-0", loaded["tracklets"][0]["id"])

    def test_frame_zero_sorting_keeps_out_of_order_rows_contiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            write_csv(
                output_dir / "ball_track.csv",
                [
                    {"Frame": 1, "X": 11, "Y": 11, "Confidence": 0.90, "Status": "Detected"},
                    {"Frame": 0, "X": 10, "Y": 10, "Confidence": 0.90, "Status": "Detected"},
                    {"Frame": 2, "X": 12, "Y": 12, "Confidence": 0.90, "Status": "Detected"},
                ],
            )

            report = build_ball_audit_report(output_dir)

        self.assertEqual(1, len(report["tracklets"]))
        self.assertEqual("raw:0-2", report["tracklets"][0]["id"])
        self.assertEqual(3, report["tracklets"][0]["length"])


if __name__ == "__main__":
    unittest.main()
