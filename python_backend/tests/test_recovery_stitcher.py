from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from football_tracking.recovery_stitcher import stitch_localize_recovery_rows, write_recovery_stitch_report


def _row(frame: int, x: float | None, y: float | None, status: str = "Detected", confidence: float = 0.9) -> dict[str, str]:
    return {
        "Frame": str(frame),
        "X": "" if x is None else str(x),
        "Y": "" if y is None else str(y),
        "Confidence": "" if x is None or y is None else str(confidence),
        "Status": status,
    }


def _localize_window(start: int = 1, end: int = 12) -> dict[str, object]:
    return {
        "start_frame": start,
        "end_frame": end,
        "approval_id": "approval_roi",
        "improvement_id": "imp_roi",
        "candidate_id": "candidate_roi",
        "approved_action": "localize_ball_roi",
        "source_packet_id": "packet_roi",
        "effective_roi": [480, 880, 620, 1040],
    }


class RecoveryStitcherTests(unittest.TestCase):
    def test_localize_roi_stitch_replaces_wrong_parent_points_and_records_boundary_warning(self) -> None:
        parent_rows = [_row(0, 0, 0)]
        parent_rows.extend(_row(frame, 20, 20) for frame in range(1, 13))
        child_rows = [_row(frame, 500 + frame, 900 + frame) for frame in range(1, 13)]

        stitched_rows, attempt = stitch_localize_recovery_rows(parent_rows, child_rows, _localize_window())

        self.assertEqual("pass", attempt["status"])
        self.assertEqual("roi_stitch_accepted", attempt["reason"])
        self.assertTrue(attempt["boundary_transition_warning"])
        self.assertEqual(12, attempt["metrics"]["longest_roi_run_frames"])
        self.assertEqual(12, attempt["metrics"]["changed_frame_count"])
        by_frame = {int(row["Frame"]): row for row in stitched_rows}
        self.assertEqual("501.0", by_frame[1]["X"])
        self.assertEqual("512.0", by_frame[12]["X"])

    def test_non_localize_window_is_skipped(self) -> None:
        parent_rows = [_row(frame, None, None, "Lost", 0.0) for frame in range(1, 13)]
        child_rows = [_row(frame, 500 + frame, 900 + frame) for frame in range(1, 13)]

        stitched_rows, attempt = stitch_localize_recovery_rows(
            parent_rows,
            child_rows,
            {"start_frame": 1, "end_frame": 12, "approved_action": "rerun_ball_window"},
        )

        self.assertEqual("skipped", attempt["status"])
        self.assertEqual(parent_rows, stitched_rows)

    def test_short_localize_roi_run_fails(self) -> None:
        parent_rows = [_row(frame, None, None, "Lost", 0.0) for frame in range(1, 13)]
        child_rows = [_row(frame, 500 + frame, 900 + frame) for frame in range(1, 11)]

        _, attempt = stitch_localize_recovery_rows(parent_rows, child_rows, _localize_window())

        self.assertEqual("fail", attempt["status"])
        self.assertEqual(10, attempt["metrics"]["longest_roi_run_frames"])
        self.assertIn("insufficient_stitch_frames", attempt["blocking_reasons"])

    def test_outside_roi_ratio_and_internal_step_fail(self) -> None:
        parent_rows = [_row(frame, None, None, "Lost", 0.0) for frame in range(1, 15)]
        child_rows = [_row(frame, 500 + frame, 900 + frame) for frame in range(1, 13)]
        child_rows.append(_row(13, 900, 1200))
        child_rows.append(_row(14, 2000, 2000))

        window = _localize_window(1, 14)
        window["effective_roi"] = [480, 880, 1000, 1300]
        _, attempt = stitch_localize_recovery_rows(parent_rows, child_rows, window)

        self.assertEqual("fail", attempt["status"])
        self.assertIn("outside_roi_ratio_exceeded", attempt["blocking_reasons"])
        self.assertIn("roi_internal_step_exceeded", attempt["blocking_reasons"])

    def test_write_recovery_stitch_report_summarizes_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            attempt = {
                "status": "pass",
                "metrics": {"changed_frame_count": 12},
                "boundary_transition_warning": True,
            }

            payload = write_recovery_stitch_report(output_dir, [attempt])

            self.assertEqual("pass", payload["summary"]["status"])
            self.assertEqual(12, payload["summary"]["changed_frame_count"])
            loaded = json.loads((output_dir / "recovery_stitch_report.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"], loaded["summary"])

    def test_partial_right_corner_recovery_reports_uncovered_head_and_tail(self) -> None:
        parent_rows = [_row(frame, None, None, "Lost", 0.0) for frame in range(2049, 2545)]
        child_rows = [_row(frame, 5700, 1390) for frame in range(2079, 2301)]
        window = _localize_window(2049, 2544)
        window["effective_roi"] = [5600, 1320, 5760, 1440]

        _, attempt = stitch_localize_recovery_rows(parent_rows, child_rows, window)

        self.assertEqual("pass", attempt["status"])
        coverage = attempt["metrics"]["required_window_coverage"]
        self.assertEqual("fail", coverage["status"])
        self.assertEqual(
            [
                {"start_frame": 2049, "end_frame": 2078, "frame_count": 30},
                {"start_frame": 2301, "end_frame": 2544, "frame_count": 244},
            ],
            coverage["uncovered_ranges"],
        )


if __name__ == "__main__":
    unittest.main()
