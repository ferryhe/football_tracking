from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from football_tracking.high_recall_reconcile import reconcile_high_recall_window, write_track_csv


def _row(frame: int, x: float | None, y: float | None, status: str, confidence: float = 0.9) -> dict[str, str]:
    return {
        "Frame": str(frame),
        "X": "" if x is None else str(x),
        "Y": "" if y is None else str(y),
        "Confidence": "" if x is None or y is None else str(confidence),
        "Status": status,
    }


class HighRecallReconcileTests(unittest.TestCase):
    def test_reconcile_accepts_high_recall_rows_that_fill_lost_gap(self) -> None:
        main_rows = [
            _row(0, 0, 0, "Detected"),
            _row(1, 10, 0, "Detected"),
            _row(2, None, None, "Lost", 0.0),
            _row(3, None, None, "Lost", 0.0),
            _row(4, 40, 0, "Detected"),
            _row(5, 50, 0, "Detected"),
        ]
        high_recall_rows = [
            _row(2, 20, 0, "Detected", 0.72),
            _row(3, 30, 0, "Detected", 0.73),
        ]

        result = reconcile_high_recall_window(
            main_rows,
            high_recall_rows,
            {"start_frame": 2, "end_frame": 3, "reason": "lost_gap", "priority": "high"},
            max_speed_px_per_frame=15.0,
            max_jump_px=20.0,
        )

        self.assertTrue(result["accepted"])
        self.assertEqual("accepted", result["reason"])
        self.assertEqual([2, 3], result["accepted_frames"])
        by_frame = {int(row["Frame"]): row for row in result["rows"]}
        self.assertEqual("Detected", by_frame[2]["Status"])
        self.assertEqual("20.0", by_frame[2]["X"])
        self.assertEqual("30.0", by_frame[3]["X"])

    def test_reconcile_rejects_rows_that_create_large_jump_and_records_review_clue(self) -> None:
        main_rows = [
            _row(0, 0, 0, "Detected"),
            _row(1, None, None, "Lost", 0.0),
            _row(2, None, None, "Lost", 0.0),
            _row(3, 30, 0, "Detected"),
        ]
        high_recall_rows = [
            _row(1, 500, 0, "Detected", 0.95),
            _row(2, 510, 0, "Detected", 0.95),
        ]
        window = {"start_frame": 1, "end_frame": 2, "reason": "lost_gap", "priority": "high"}

        result = reconcile_high_recall_window(
            main_rows,
            high_recall_rows,
            window,
            max_speed_px_per_frame=100.0,
            max_jump_px=120.0,
        )

        self.assertFalse(result["accepted"])
        self.assertEqual("jump_gate_failed", result["reason"])
        self.assertEqual([], result["accepted_frames"])
        self.assertEqual(main_rows, result["rows"])
        self.assertEqual(
            {
                "start_frame": 1,
                "end_frame": 2,
                "reason": "lost_gap",
                "priority": "high",
                "rejection_reason": "jump_gate_failed",
            },
            result["review_packet_clues"][0],
        )

    def test_reconcile_accepts_clean_subsegments_from_noisy_high_recall_window(self) -> None:
        main_rows = [
            _row(0, 4700, 940, "Detected"),
            _row(1, 3200, 1100, "Predicted", 0.12),
            _row(2, None, None, "Lost", 0.0),
            _row(3, None, None, "Lost", 0.0),
            _row(4, None, None, "Lost", 0.0),
            _row(5, None, None, "Lost", 0.0),
            _row(6, None, None, "Lost", 0.0),
            _row(7, None, None, "Lost", 0.0),
        ]
        high_recall_rows = [
            _row(1, 4710, 942, "Detected", 0.70),
            _row(2, 4720, 944, "Detected", 0.71),
            _row(3, 4730, 946, "Predicted", 0.55),
            _row(4, 3300, 1080, "Detected", 0.75),
            _row(5, 5040, 1025, "Detected", 0.68),
            _row(6, 5050, 1028, "Detected", 0.69),
        ]

        result = reconcile_high_recall_window(
            main_rows,
            high_recall_rows,
            {
                "start_frame": 1,
                "end_frame": 6,
                "reason": "large_jump; suspicious_tracklet; lost_gap",
                "priority": "high",
            },
            max_speed_px_per_frame=180.0,
            max_jump_px=260.0,
        )

        self.assertTrue(result["accepted"])
        self.assertEqual([1, 2, 3, 5, 6], result["accepted_frames"])
        by_frame = {int(row["Frame"]): row for row in result["rows"]}
        self.assertEqual("4710.0", by_frame[1]["X"])
        self.assertEqual("4730.0", by_frame[3]["X"])
        self.assertEqual("Lost", by_frame[4]["Status"])
        self.assertEqual("5050.0", by_frame[6]["X"])

    def test_reconcile_uses_speed_gate_across_long_lost_gaps(self) -> None:
        main_rows = [
            _row(0, 0, 0, "Detected"),
            _row(1, None, None, "Lost", 0.0),
            _row(2, None, None, "Lost", 0.0),
            _row(3, None, None, "Lost", 0.0),
            _row(4, None, None, "Lost", 0.0),
            _row(5, None, None, "Lost", 0.0),
            _row(6, 600, 0, "Detected"),
        ]
        high_recall_rows = [_row(3, 300, 0, "Detected", 0.8)]

        result = reconcile_high_recall_window(
            main_rows,
            high_recall_rows,
            {"start_frame": 1, "end_frame": 5, "reason": "lost_gap", "priority": "medium"},
            max_speed_px_per_frame=120.0,
            max_jump_px=180.0,
        )

        self.assertTrue(result["accepted"])
        self.assertEqual([3], result["accepted_frames"])

    def test_reconcile_prefers_long_segment_over_short_false_bridge(self) -> None:
        main_rows = [_row(0, 4937, 1010, "Detected")]
        main_rows.extend(_row(frame, None, None, "Lost", 0.0) for frame in range(1, 35))
        high_recall_rows = [
            _row(10, 3773, 769, "Detected", 0.43),
            *[_row(frame, 4980 + frame, 1010, "Detected", 0.65) for frame in range(11, 31)],
        ]

        result = reconcile_high_recall_window(
            main_rows,
            high_recall_rows,
            {"start_frame": 1, "end_frame": 34, "reason": "lost_gap", "priority": "high"},
            max_speed_px_per_frame=180.0,
            max_jump_px=260.0,
        )

        self.assertTrue(result["accepted"])
        self.assertNotIn(10, result["accepted_frames"])
        self.assertEqual(list(range(11, 31)), result["accepted_frames"])

    def test_reconcile_trims_bad_prefix_and_keeps_later_continuous_segment(self) -> None:
        main_rows = [_row(0, 4980, 1000, "Detected")]
        main_rows.extend(_row(frame, None, None, "Lost", 0.0) for frame in range(1, 40))
        high_recall_rows = [
            _row(1, 4200, 720, "Detected", 0.5),
            *[_row(frame, 3400 + frame, 880, "Detected", 0.62) for frame in range(20, 31)],
        ]

        result = reconcile_high_recall_window(
            main_rows,
            high_recall_rows,
            {"start_frame": 1, "end_frame": 39, "reason": "lost_gap", "priority": "high"},
            max_speed_px_per_frame=180.0,
            max_jump_px=260.0,
        )

        self.assertTrue(result["accepted"])
        self.assertNotIn(1, result["accepted_frames"])
        self.assertEqual(list(range(20, 31)), result["accepted_frames"])

    def test_reconcile_accepts_case_insensitive_detected_status_and_normalizes_output(self) -> None:
        main_rows = [
            _row(0, 0, 0, "Detected"),
            _row(1, None, None, "Lost", 0.0),
            _row(2, 20, 0, "Detected"),
        ]
        high_recall_rows = [_row(1, 10, 0, "DETECTED", 0.8)]

        result = reconcile_high_recall_window(
            main_rows,
            high_recall_rows,
            {"start_frame": 1, "end_frame": 1, "reason": "lost_gap", "priority": "medium"},
            max_speed_px_per_frame=15.0,
            max_jump_px=20.0,
        )

        self.assertTrue(result["accepted"])
        by_frame = {int(row["Frame"]): row for row in result["rows"]}
        self.assertEqual("Detected", by_frame[1]["Status"])

    def test_reconcile_preserves_untouched_row_formatting(self) -> None:
        main_rows = [
            {"Frame": "0", "X": "0.00", "Y": "0.00", "Confidence": "0.9000", "Status": "Detected"},
            _row(1, None, None, "Lost", 0.0),
            {"Frame": "2", "X": "20.00", "Y": "0.00", "Confidence": "0.9100", "Status": "Detected"},
        ]
        high_recall_rows = [_row(1, 10, 0, "Detected", 0.8)]

        result = reconcile_high_recall_window(
            main_rows,
            high_recall_rows,
            {"start_frame": 1, "end_frame": 1, "reason": "lost_gap", "priority": "medium"},
            max_speed_px_per_frame=15.0,
            max_jump_px=20.0,
        )

        by_frame = {int(row["Frame"]): row for row in result["rows"]}
        self.assertEqual("0.9000", by_frame[0]["Confidence"])
        self.assertEqual("20.00", by_frame[2]["X"])
        self.assertEqual("10.0", by_frame[1]["X"])

    def test_write_track_csv_uses_atomic_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            csv_path = Path(temp_name) / "ball_track.csv"
            original_text = "Frame,X,Y,Confidence,Status\n0,1,2,0.9000,Detected\n"
            csv_path.write_text(original_text, encoding="utf-8")

            with patch("pathlib.Path.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    write_track_csv(csv_path, [_row(0, 10, 20, "Detected", 0.8)])

            self.assertEqual(original_text, csv_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
