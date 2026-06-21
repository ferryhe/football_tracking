from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from football_tracking.player_tracks import (
    build_player_tracks_report,
    compact_player_tracks_summary,
    write_player_tracks_artifacts,
)


class PlayerTracksTests(unittest.TestCase):
    def test_missing_input_returns_empty_missing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            report = build_player_tracks_report(Path(temp_name))

        self.assertEqual("missing", report["source"]["status"])
        self.assertEqual("player_detections.jsonl", report["source"]["path"])
        self.assertEqual(0, report["source"]["detection_count"])
        self.assertEqual(0, report["summary"]["track_count"])
        self.assertEqual([], report["tracks"])
        self.assertIsNone(compact_player_tracks_summary({"summary": "bad"}))

    def test_mixed_line_formats_ignore_bad_lines_and_non_players(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            self.write_jsonl(
                output_dir / "player_detections.jsonl",
                [
                    {
                        "frame": 0,
                        "detections": [
                            {"bbox": [10, 20, 30, 80], "confidence": 0.9, "label": "person", "team": "home"},
                            {"bbox": [300, 20, 330, 80], "confidence": 0.7, "label": "ball"},
                        ],
                    },
                    "{not json",
                    {"frame": 1, "bbox": [12, 22, 32, 82], "confidence": 0.8, "label": "player"},
                    {"frame": 2, "bbox": [14, 24, 34, 84], "confidence": 0.6, "label": "referee", "team": "away"},
                ],
            )

            report = build_player_tracks_report(output_dir)

        self.assertEqual("loaded", report["source"]["status"])
        self.assertEqual(3, report["source"]["detection_count"])
        self.assertEqual(1, report["source"]["malformed_line_count"])
        self.assertEqual(1, report["summary"]["track_count"])
        self.assertEqual(3, report["summary"]["frame_count"])
        self.assertEqual({"home": 1}, report["summary"]["teams"])
        track = report["tracks"][0]
        self.assertEqual("P001", track["id"])
        self.assertEqual("home", track["team"])
        self.assertEqual([20.0, 80.0], [track["first_foot_point"]["x"], track["first_foot_point"]["y"]])
        self.assertEqual([24.0, 84.0], [track["last_foot_point"]["x"], track["last_foot_point"]["y"]])

    def test_tracks_are_stitched_by_foot_point_and_ids_sort_by_first_position(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            self.write_jsonl(
                output_dir / "player_detections.jsonl",
                [
                    {"frame": 0, "bbox": [200, 10, 220, 70], "confidence": 0.7, "label": "person", "team": "away"},
                    {"frame": 0, "bbox": [20, 10, 40, 70], "confidence": 0.8, "label": "person", "team": "home"},
                    {"frame": 1, "bbox": [22, 12, 42, 72], "confidence": 0.9, "label": "person", "team": "home"},
                    {"frame": 2, "bbox": [202, 12, 222, 72], "confidence": 0.6, "label": "person", "team": "away"},
                ],
            )

            report = build_player_tracks_report(output_dir)

        self.assertEqual(["P001", "P002"], [track["id"] for track in report["tracks"]])
        self.assertEqual([0, 1], [report["tracks"][0]["start_frame"], report["tracks"][0]["end_frame"]])
        self.assertEqual([0, 2], [report["tracks"][1]["start_frame"], report["tracks"][1]["end_frame"]])
        self.assertEqual(2, report["summary"]["active_track_count"])
        self.assertEqual(2.0, report["summary"]["mean_track_length"])

    def test_gap_and_distance_limits_split_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            self.write_jsonl(
                output_dir / "player_detections.jsonl",
                [
                    {"frame": 0, "bbox": [0, 0, 20, 50], "confidence": 0.9, "label": "person"},
                    {"frame": 3, "bbox": [2, 0, 22, 50], "confidence": 0.8, "label": "person"},
                    {"frame": 4, "bbox": [200, 0, 220, 50], "confidence": 0.7, "label": "person"},
                ],
            )

            report = build_player_tracks_report(output_dir)

        self.assertEqual(3, report["summary"]["track_count"])
        self.assertEqual(0, report["summary"]["active_track_count"])
        self.assertEqual([1, 1, 1], [track["length"] for track in report["tracks"]])

    def test_team_uses_most_common_non_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            self.write_jsonl(
                output_dir / "player_detections.jsonl",
                [
                    {"frame": 0, "bbox": [0, 0, 20, 50], "confidence": 0.9, "label": "person"},
                    {"frame": 1, "bbox": [1, 0, 21, 50], "confidence": 0.8, "label": "person", "team": "away"},
                    {"frame": 2, "bbox": [2, 0, 22, 50], "confidence": 0.7, "label": "person", "team": "away"},
                ],
            )

            report = build_player_tracks_report(output_dir)

        self.assertEqual("away", report["tracks"][0]["team"])
        self.assertEqual({"away": 1}, report["summary"]["teams"])

    def test_write_artifacts_writes_json_and_csv_when_samples_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            self.write_jsonl(
                output_dir / "player_detections.jsonl",
                [
                    {"frame": 0, "bbox": [0, 1, 20, 51], "confidence": 0.9, "label": "person", "team": "home"},
                    {"frame": 1, "bbox": [2, 3, 22, 53], "confidence": 0.8, "label": "person", "team": "home"},
                ],
            )

            report = write_player_tracks_artifacts(output_dir)
            csv_path = output_dir / "player_tracks.csv"
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))

            self.assertTrue((output_dir / "player_tracks.json").exists())
            self.assertTrue(csv_path.exists())
            self.assertEqual(1, report["summary"]["track_count"])
            self.assertEqual(
                ["Frame", "TrackId", "FootX", "FootY", "X1", "Y1", "X2", "Y2", "Confidence", "Label", "Team"],
                list(rows[0].keys()),
            )
            self.assertEqual("P001", rows[0]["TrackId"])
            self.assertEqual("10.00", rows[0]["FootX"])
            self.assertEqual("home", rows[0]["Team"])

    def write_jsonl(self, path: Path, lines: list[object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload_lines = [
            line if isinstance(line, str) else json.dumps(line, ensure_ascii=False)
            for line in lines
        ]
        path.write_text("\n".join(payload_lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
