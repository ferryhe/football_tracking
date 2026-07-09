from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from football_tracking.tracking_contracts import (
    TRACKING_CONTRACT_REPORT_NAME,
    build_tracking_contract,
    load_legacy_track_csv,
    load_tracking_contract,
    write_tracking_contract,
)


class TrackingContractsTests(unittest.TestCase):
    def test_builds_versioned_contract_and_distinguishes_prelabels_from_confirmed_labels(self) -> None:
        payload = build_tracking_contract(
            frames=[
                {"frame_index": 0, "status": "detected", "x": 12.0, "y": 8.0, "confidence": 0.9},
                {"frame_index": 1, "status": "unknown"},
                {"frame_index": 2, "status": "out_of_view"},
            ],
            candidates=[
                {
                    "candidate_id": "candidate-1",
                    "frame_index": 0,
                    "bbox": [10.0, 6.0, 14.0, 10.0],
                    "confidence": 0.8,
                    "source": "yolo",
                }
            ],
            classifications=[
                {
                    "candidate_id": "candidate-1",
                    "label": "match_ball",
                    "label_origin": "prelabel",
                    "confidence": 0.7,
                },
                {
                    "candidate_id": "candidate-1",
                    "label": "match_ball",
                    "label_origin": "human_confirmed",
                    "confidence": 1.0,
                },
            ],
            decisions=[
                {
                    "candidate_id": "candidate-1",
                    "decision": "accept",
                    "confidence": 0.99,
                    "reason": "above calibrated threshold",
                }
            ],
        )

        self.assertEqual("2.0", payload["schema_version"])
        self.assertEqual("ok", payload["summary"]["status"])
        self.assertEqual([], payload["validation_errors"])
        self.assertFalse(payload["classifications"][0]["confirmed"])
        self.assertTrue(payload["classifications"][1]["confirmed"])
        self.assertEqual(1, payload["summary"]["prelabel_count"])
        self.assertEqual(1, payload["summary"]["confirmed_label_count"])

    def test_invalid_records_fail_closed_with_validation_errors(self) -> None:
        payload = build_tracking_contract(
            frames=[
                {"frame_index": -1, "status": "Predicted", "x": float("nan"), "y": 2.0},
                {"frame_index": 1, "status": "detected"},
            ],
            candidates=[{"candidate_id": "", "frame_index": 0, "bbox": [1, 2, 0, 4], "confidence": 2.0}],
            classifications=[{"candidate_id": "candidate-1", "label": "match_ball", "label_origin": "prelabel"}],
            decisions=[{"candidate_id": "candidate-1", "decision": "guess", "confidence": 0.8}],
        )

        self.assertEqual("invalid", payload["summary"]["status"])
        self.assertGreaterEqual(len(payload["validation_errors"]), 6)
        self.assertEqual([], payload["frames"])
        self.assertEqual([], payload["candidates"])
        self.assertEqual([], payload["decisions"])

    def test_rejects_duplicate_frames_candidates_and_dangling_references(self) -> None:
        payload = build_tracking_contract(
            frames=[
                {"frame_index": 0, "status": "unknown"},
                {"frame_index": 0, "status": "out_of_view"},
            ],
            candidates=[
                {
                    "candidate_id": "duplicate",
                    "frame_index": 0,
                    "bbox": [1, 1, 2, 2],
                    "confidence": 0.9,
                    "source": "yolo",
                },
                {
                    "candidate_id": "duplicate",
                    "frame_index": 1,
                    "bbox": [2, 2, 3, 3],
                    "confidence": 0.8,
                    "source": "motion",
                },
            ],
            classifications=[
                {
                    "candidate_id": "absent-classification",
                    "label": "equipment_or_background",
                    "label_origin": "human_confirmed",
                    "confidence": 1.0,
                }
            ],
            decisions=[
                {
                    "candidate_id": "absent-decision",
                    "decision": "abstain",
                    "confidence": 0.5,
                }
            ],
        )

        self.assertEqual("invalid", payload["summary"]["status"])
        self.assertEqual(1, len(payload["frames"]))
        self.assertEqual(1, len(payload["candidates"]))
        self.assertTrue(any("duplicate frame_index" in error for error in payload["validation_errors"]))
        self.assertTrue(any("duplicate candidate_id" in error for error in payload["validation_errors"]))
        self.assertTrue(any("absent-classification" in error for error in payload["validation_errors"]))
        self.assertTrue(any("absent-decision" in error for error in payload["validation_errors"]))

    def test_allows_multiple_classification_origins_for_one_candidate(self) -> None:
        payload = build_tracking_contract(
            candidates=[
                {
                    "candidate_id": "candidate-1",
                    "frame_index": 0,
                    "bbox": [1, 1, 2, 2],
                    "confidence": 0.9,
                    "source": "yolo",
                }
            ],
            classifications=[
                {
                    "candidate_id": "candidate-1",
                    "label": "unknown",
                    "label_origin": "prelabel",
                    "confidence": 0.5,
                },
                {
                    "candidate_id": "candidate-1",
                    "label": "match_ball",
                    "label_origin": "human_confirmed",
                    "confidence": 1.0,
                },
            ],
        )

        self.assertEqual("ok", payload["summary"]["status"])
        self.assertEqual(2, len(payload["classifications"]))

    def test_write_and_load_validate_json_and_handle_missing_or_corrupt_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            written = write_tracking_contract(
                output_dir,
                frames=[{"frame_index": 0, "status": "interpolated", "x": 5, "y": 6, "confidence": 0.4}],
            )
            raw = (output_dir / TRACKING_CONTRACT_REPORT_NAME).read_text(encoding="utf-8")
            loaded = load_tracking_contract(output_dir)
            missing = load_tracking_contract(output_dir / "missing")
            corrupt_path = output_dir / "corrupt.json"
            corrupt_path.write_text("{not-json", encoding="utf-8")
            corrupt = load_tracking_contract(corrupt_path)

        self.assertTrue(raw.endswith("\n"))
        self.assertEqual(written, json.loads(raw))
        self.assertEqual("loaded", loaded["artifact_status"])
        self.assertEqual(written["frames"], loaded["frames"])
        self.assertEqual("missing", missing["artifact_status"])
        self.assertEqual("invalid", corrupt["artifact_status"])
        self.assertEqual("invalid", corrupt["summary"]["status"])

    def test_correct_version_skeletal_or_malformed_envelope_is_invalid(self) -> None:
        malformed_payloads = [
            {"schema_version": "2.0"},
            {
                "schema_version": "2.0",
                "generated_at": 123,
                "summary": [],
                "frames": [],
                "candidates": [],
                "classifications": [],
                "decisions": [],
                "validation_errors": {},
            },
        ]
        with tempfile.TemporaryDirectory() as temp_name:
            results = []
            for index, payload in enumerate(malformed_payloads):
                path = Path(temp_name) / f"malformed-{index}.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                results.append(load_tracking_contract(path))

        for result in results:
            self.assertEqual("invalid", result["artifact_status"])
            self.assertEqual("invalid", result["summary"]["status"])
            self.assertGreater(len(result["validation_errors"]), 0)

    def test_legacy_csv_reader_maps_statuses_and_preserves_original_rows_losslessly(self) -> None:
        rows = [
            {"Frame": "0", "X": "1.25", "Y": "2.50", "Confidence": "0.9000", "Status": "Detected", "Note": "raw"},
            {"Frame": "1", "X": "3", "Y": "4", "Confidence": "0.5000", "Status": "Predicted", "Note": ""},
            {"Frame": "2", "X": "", "Y": "", "Confidence": "0.0000", "Status": "Lost", "Note": "occluded"},
        ]
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "ball_track.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)

            payload = load_legacy_track_csv(path)

        self.assertEqual(["detected", "interpolated", "unknown"], [row["status"] for row in payload["frames"]])
        self.assertEqual(["Detected", "Predicted", "Lost"], [row["legacy_status"] for row in payload["frames"]])
        self.assertEqual(rows, [row["legacy_row"] for row in payload["frames"]])
        self.assertEqual(list(rows[0]), payload["legacy_columns"])
        self.assertEqual([], payload["validation_errors"])

    def test_legacy_csv_reader_rejects_missing_headers_and_header_only_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            missing_header = root / "missing-header.csv"
            missing_header.write_text("Frame,X,Y,Status\n0,1,2,Detected\n", encoding="utf-8")
            header_only = root / "header-only.csv"
            header_only.write_text("Frame,X,Y,Confidence,Status\n", encoding="utf-8")

            missing_header_result = load_legacy_track_csv(missing_header)
            header_only_result = load_legacy_track_csv(header_only)

        self.assertEqual("invalid", missing_header_result["artifact_status"])
        self.assertTrue(
            any("missing required headers" in error for error in missing_header_result["validation_errors"])
        )
        self.assertEqual([], missing_header_result["frames"])
        self.assertEqual("invalid", header_only_result["artifact_status"])
        self.assertTrue(any("no data rows" in error for error in header_only_result["validation_errors"]))

    def test_legacy_csv_source_path_is_consistent_for_missing_invalid_and_loaded_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            missing_path = root / "missing.csv"
            invalid_path = root / "invalid.csv"
            invalid_path.write_text("Frame,X,Y,Confidence,Status\n", encoding="utf-8")
            loaded_path = root / "loaded.csv"
            loaded_path.write_text(
                "Frame,X,Y,Confidence,Status\n0,1,2,0.9,Detected\n",
                encoding="utf-8",
            )

            results = [
                (missing_path, load_legacy_track_csv(missing_path)),
                (invalid_path, load_legacy_track_csv(invalid_path)),
                (loaded_path, load_legacy_track_csv(loaded_path)),
            ]

        for path, result in results:
            self.assertEqual(str(path), result["source_path"])

    def test_legacy_csv_duplicate_frames_remain_invalid_after_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "duplicate-frames.csv"
            path.write_text(
                "Frame,X,Y,Confidence,Status\n0,1,2,0.9,Detected\n0,2,3,0.8,Predicted\n",
                encoding="utf-8",
            )

            result = load_legacy_track_csv(path)

        self.assertEqual("invalid", result["artifact_status"])
        self.assertEqual(1, len(result["frames"]))
        self.assertTrue(any("duplicate frame_index" in error for error in result["validation_errors"]))


if __name__ == "__main__":
    unittest.main()
