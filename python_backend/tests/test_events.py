from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from football_tracking.events import build_event_candidate_report, write_event_candidate_report


def write_track_csv(path: Path, rows: list[dict[str, object]]) -> None:
    headers = ["Frame", "X", "Y", "Confidence", "Status"]
    lines = [",".join(headers)]
    for row in rows:
        lines.append(",".join(str(row.get(header, "")) for header in headers))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class EventCandidateTests(unittest.TestCase):
    def test_build_event_candidate_report_prefers_cleaned_track_and_labels_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            write_track_csv(
                output_dir / "ball_track.csv",
                [
                    {"Frame": 0, "X": 10, "Y": 40, "Confidence": 0.90, "Status": "Detected"},
                    {"Frame": 1, "X": 12, "Y": 40, "Confidence": 0.90, "Status": "Detected"},
                    {"Frame": 2, "X": 14, "Y": 40, "Confidence": 0.90, "Status": "Detected"},
                ],
            )
            write_track_csv(
                output_dir / "ball_track.cleaned.csv",
                [
                    {"Frame": 0, "X": 120, "Y": 100, "Confidence": 0.92, "Status": "Detected"},
                    {"Frame": 1, "X": 135, "Y": 101, "Confidence": 0.90, "Status": "Detected"},
                    {"Frame": 2, "X": 151, "Y": 102, "Confidence": 0.91, "Status": "Detected"},
                    {"Frame": 3, "Confidence": 0.00, "Status": "Lost"},
                    {"Frame": 20, "X": 300, "Y": 140, "Confidence": 0.95, "Status": "Detected"},
                    {"Frame": 21, "X": 335, "Y": 140, "Confidence": 0.94, "Status": "Detected"},
                    {"Frame": 22, "X": 390, "Y": 141, "Confidence": 0.96, "Status": "Detected"},
                    {"Frame": 23, "X": 462, "Y": 141, "Confidence": 0.95, "Status": "Detected"},
                    {"Frame": 24, "X": 530, "Y": 142, "Confidence": 0.94, "Status": "Predicted"},
                    {"Frame": 40, "X": 650, "Y": 160, "Confidence": 0.95, "Status": "Detected"},
                    {"Frame": 41, "X": 720, "Y": 160, "Confidence": 0.96, "Status": "Detected"},
                    {"Frame": 42, "X": 815, "Y": 159, "Confidence": 0.95, "Status": "Detected"},
                    {"Frame": 43, "X": 930, "Y": 158, "Confidence": 0.94, "Status": "Detected"},
                    {"Frame": 44, "X": 960, "Y": 158, "Confidence": 0.93, "Status": "Detected"},
                ],
            )

            first_report = build_event_candidate_report(output_dir)
            second_report = build_event_candidate_report(output_dir)

        self.assertEqual(first_report, second_report)
        self.assertEqual("1.0", first_report["schema_version"])
        self.assertEqual({"name": "cleaned", "path": "ball_track.cleaned.csv", "row_count": 14}, first_report["source"])
        self.assertEqual(["fps unavailable; defaulted to 20.0fps for event candidate buffers."], first_report["warnings"])
        self.assertEqual(14, first_report["summary"]["frame_count"])
        self.assertEqual(2, first_report["summary"]["candidate_count"])
        self.assertEqual({"goal_candidate": 1, "shot_candidate": 1}, first_report["summary"]["counts_by_type"])

        candidates = first_report["candidates"]
        self.assertEqual(["shot_candidate", "goal_candidate"], [candidate["type"] for candidate in candidates])
        self.assertEqual("cleaned:shot_candidate:20-24", candidates[0]["id"])
        self.assertEqual("cleaned:goal_candidate:40-44", candidates[1]["id"])
        self.assertEqual({"start_frame": 20, "end_frame": 24}, candidates[0]["core_window"])
        self.assertEqual({"start_frame": 5, "end_frame": 114}, candidates[0]["render_window"])
        self.assertEqual(
            {
                "fps": 20.0,
                "fps_source": "default",
                "pre_buffer_seconds": 0.75,
                "post_buffer_seconds": 4.5,
                "pre_buffer_frames": 15,
                "post_buffer_frames": 90,
                "min_post_event_frames": 90,
                "min_tail_frames": 90,
            },
            candidates[0]["buffer_policy"],
        )
        self.assertEqual({"start_frame": 25, "end_frame": 164}, candidates[1]["render_window"])
        self.assertEqual(120, candidates[1]["buffer_policy"]["min_tail_frames"])
        self.assertEqual("candidate", candidates[1]["label"])
        self.assertIn("near right goal zone", candidates[1]["reason"])
        self.assertGreaterEqual(candidates[1]["evidence"]["max_speed_px_per_frame"], 90.0)
        self.assertEqual("right", candidates[1]["evidence"]["goal_side"])

    def test_event_candidate_buffers_convert_from_fps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            write_track_csv(
                output_dir / "ball_track.csv",
                [
                    {"Frame": 100, "X": 10, "Y": 40, "Confidence": 0.90, "Status": "Detected"},
                    {"Frame": 101, "X": 58, "Y": 40, "Confidence": 0.90, "Status": "Detected"},
                    {"Frame": 102, "X": 112, "Y": 40, "Confidence": 0.90, "Status": "Detected"},
                ],
            )

            report_20fps = build_event_candidate_report(output_dir, fps=20.0)
            report_30fps = build_event_candidate_report(output_dir, fps=30.0)

        candidate_20fps = report_20fps["candidates"][0]
        candidate_30fps = report_30fps["candidates"][0]
        self.assertEqual({"start_frame": 85, "end_frame": 192}, candidate_20fps["render_window"])
        self.assertEqual("explicit", candidate_20fps["buffer_policy"]["fps_source"])
        self.assertEqual(15, candidate_20fps["buffer_policy"]["pre_buffer_frames"])
        self.assertEqual(90, candidate_20fps["buffer_policy"]["post_buffer_frames"])
        self.assertEqual({"start_frame": 77, "end_frame": 237}, candidate_30fps["render_window"])
        self.assertEqual("explicit", candidate_30fps["buffer_policy"]["fps_source"])
        self.assertEqual(23, candidate_30fps["buffer_policy"]["pre_buffer_frames"])
        self.assertEqual(135, candidate_30fps["buffer_policy"]["post_buffer_frames"])

    def test_build_event_candidate_report_returns_stable_empty_report_without_track_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)

            report = build_event_candidate_report(output_dir)

        self.assertEqual("1.0", report["schema_version"])
        self.assertEqual({"name": "none", "path": None, "row_count": 0}, report["source"])
        self.assertEqual(
            {
                "frame_count": 0,
                "detected_frame_count": 0,
                "candidate_count": 0,
                "counts_by_type": {},
                "min_frame": None,
                "max_frame": None,
            },
            report["summary"],
        )
        self.assertEqual([], report["candidates"])
        self.assertEqual([], report["warnings"])

    def test_write_event_candidate_report_persists_json_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            write_track_csv(
                output_dir / "ball_track.csv",
                [
                    {"Frame": 0, "X": 0, "Y": 50, "Confidence": 0.90, "Status": "Detected"},
                    {"Frame": 1, "X": 40, "Y": 50, "Confidence": 0.90, "Status": "Detected"},
                    {"Frame": 2, "X": 92, "Y": 50, "Confidence": 0.90, "Status": "Detected"},
                    {"Frame": 3, "X": 150, "Y": 50, "Confidence": 0.90, "Status": "Detected"},
                ],
            )

            report = write_event_candidate_report(output_dir)
            loaded = json.loads((output_dir / "event_candidates.json").read_text(encoding="utf-8"))

        self.assertEqual(report, loaded)
        self.assertEqual("raw", loaded["source"]["name"])
        self.assertEqual(1, loaded["summary"]["candidate_count"])
        self.assertEqual("default", loaded["candidates"][0]["buffer_policy"]["fps_source"])
        self.assertTrue(loaded["warnings"])

    def test_unknown_status_rows_do_not_create_event_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            write_track_csv(
                output_dir / "ball_track.csv",
                [
                    {"Frame": 0, "X": 0, "Y": 50, "Confidence": 0.90, "Status": "Detected"},
                    {"Frame": 1, "X": 52, "Y": 50, "Confidence": 0.90, "Status": ""},
                    {"Frame": 2, "X": 104, "Y": 50, "Confidence": 0.90, "Status": "Detected"},
                ],
            )

            report = build_event_candidate_report(output_dir)

        self.assertEqual(0, report["summary"]["candidate_count"])
        self.assertEqual([], report["candidates"])


if __name__ == "__main__":
    unittest.main()
