from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from football_tracking.chunk_stitcher import stitch_chunk_outputs
from football_tracking.config import OutputConfig
from football_tracking.detector_candidate_contract import (
    assign_candidate_ids,
    candidate_to_contract_record,
)
from football_tracking.temporal_chunks import TemporalChunk
from football_tracking.tracking_contracts import (
    build_tracking_contract,
    load_tracking_contract,
    write_tracking_contract,
)
from football_tracking.types import Candidate

TEST_SOURCE_SHA256 = "a" * 64


def candidate_record(frame: int) -> dict[str, object]:
    candidate = Candidate(
        frame_index=frame,
        x1=frame + 0.1,
        y1=frame + 0.2,
        x2=frame + 1.1,
        y2=frame + 1.2,
        confidence=0.9,
        source="test_detector",
    )
    assign_candidate_ids([candidate], TEST_SOURCE_SHA256)
    return candidate_to_contract_record(candidate)


def make_chunk(
    index: int,
    *,
    decode_start_frame: int,
    start_frame: int,
    end_frame: int,
    core_start_frame: int,
    core_end_frame: int,
) -> TemporalChunk:
    return TemporalChunk(
        index=index,
        decode_start_frame=decode_start_frame,
        start_frame=start_frame,
        end_frame=end_frame,
        core_start_frame=core_start_frame,
        core_end_frame=core_end_frame,
        output_dir_name=f"chunk_{index:04d}",
    )


def write_chunk_outputs(
    chunk_dir: Path,
    *,
    frame_start: int,
    frame_end: int,
    missing_csv_frames: set[int] | None = None,
    missing_debug_frames: set[int] | None = None,
    output_config: OutputConfig | None = None,
) -> None:
    output_config = output_config or OutputConfig()
    missing_csv_frames = missing_csv_frames or set()
    missing_debug_frames = missing_debug_frames or set()
    chunk_dir.mkdir(parents=True, exist_ok=True)
    with (chunk_dir / output_config.csv_name).open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["Frame", "X", "Y", "Confidence", "Status"])
        for frame in range(frame_start, frame_end + 1):
            if frame in missing_csv_frames:
                continue
            writer.writerow([frame, f"{frame + 0.1:.2f}", f"{frame + 0.2:.2f}", "0.9000", "DETECTED"])
    with (chunk_dir / output_config.debug_jsonl_name).open("w", encoding="utf-8") as debug_file:
        for frame in range(frame_start, frame_end + 1):
            if frame in missing_debug_frames:
                continue
            debug_file.write(json.dumps({"frame": frame, "source_chunk": chunk_dir.name}) + "\n")
    write_tracking_contract(
        chunk_dir,
        frames=[
            {
                "frame_index": frame,
                "status": "detected",
                "x": frame + 0.1,
                "y": frame + 0.2,
                "confidence": 0.9,
            }
            for frame in range(frame_start, frame_end + 1)
        ],
        candidates=[candidate_record(frame) for frame in range(frame_start, frame_end + 1)],
    )


class ChunkStitcherTests(unittest.TestCase):
    def test_stitch_chunk_outputs_keeps_only_core_frames_sorted_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp_dir = Path(temp_name)
            chunks = [
                make_chunk(
                    0, decode_start_frame=0, start_frame=0, end_frame=109, core_start_frame=0, core_end_frame=99
                ),
                make_chunk(
                    1,
                    decode_start_frame=90,
                    start_frame=90,
                    end_frame=209,
                    core_start_frame=100,
                    core_end_frame=199,
                ),
                make_chunk(
                    2,
                    decode_start_frame=190,
                    start_frame=190,
                    end_frame=249,
                    core_start_frame=200,
                    core_end_frame=249,
                ),
            ]
            chunk_dirs = [temp_dir / chunk.output_dir_name for chunk in chunks]
            write_chunk_outputs(chunk_dirs[0], frame_start=0, frame_end=109)
            write_chunk_outputs(chunk_dirs[1], frame_start=90, frame_end=209)
            write_chunk_outputs(chunk_dirs[2], frame_start=190, frame_end=249)
            output_dir = temp_dir / "merged"

            report = stitch_chunk_outputs(
                chunks,
                chunk_dirs,
                output_dir,
                candidate_source_sha256=TEST_SOURCE_SHA256,
            )

            with (output_dir / "ball_track.csv").open("r", newline="", encoding="utf-8-sig") as csv_file:
                rows = list(csv.reader(csv_file))
            self.assertEqual(["Frame", "X", "Y", "Confidence", "Status"], rows[0])
            frames = [int(row[0]) for row in rows[1:]]
            self.assertEqual(list(range(250)), frames)
            self.assertEqual("99", rows[100][0])
            self.assertEqual("100", rows[101][0])
            self.assertEqual(251, len(rows))

            debug_lines = (output_dir / "debug.jsonl").read_text(encoding="utf-8").splitlines()
            debug_items = [json.loads(line) for line in debug_lines]
            self.assertEqual(list(range(250)), [int(item["frame"]) for item in debug_items])
            self.assertEqual("chunk_0000", debug_items[99]["source_chunk"])
            self.assertEqual("chunk_0001", debug_items[100]["source_chunk"])
            self.assertEqual("chunk_0001", debug_items[199]["source_chunk"])
            self.assertEqual("chunk_0002", debug_items[200]["source_chunk"])

            contract = load_tracking_contract(output_dir)
            self.assertEqual("loaded", contract["artifact_status"])
            self.assertEqual(list(range(250)), [frame["frame_index"] for frame in contract["frames"]])
            self.assertEqual(
                [candidate_record(frame)["candidate_id"] for frame in range(250)],
                [candidate["candidate_id"] for candidate in contract["candidates"]],
            )

            report_payload = json.loads((output_dir / "temporal_chunks_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report_payload, report)
            self.assertEqual(3, report["chunk_count"])
            self.assertEqual(250, report["frame_count"])
            self.assertEqual(["chunk_0000", "chunk_0001", "chunk_0002"], report["source_chunk_names"])
            self.assertEqual([], report["boundary_events"])
            self.assertEqual(
                [
                    {
                        "index": 0,
                        "name": "chunk_0000",
                        "decode_start_frame": 0,
                        "start_frame": 0,
                        "end_frame": 109,
                        "core_start_frame": 0,
                        "core_end_frame": 99,
                    },
                    {
                        "index": 1,
                        "name": "chunk_0001",
                        "decode_start_frame": 90,
                        "start_frame": 90,
                        "end_frame": 209,
                        "core_start_frame": 100,
                        "core_end_frame": 199,
                    },
                    {
                        "index": 2,
                        "name": "chunk_0002",
                        "decode_start_frame": 190,
                        "start_frame": 190,
                        "end_frame": 249,
                        "core_start_frame": 200,
                        "core_end_frame": 249,
                    },
                ],
                report["chunks"],
            )

    def test_stitched_contract_validation_never_builds_a_full_video_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp_dir = Path(temp_name)
            chunks = [
                make_chunk(0, decode_start_frame=0, start_frame=0, end_frame=3, core_start_frame=0, core_end_frame=1),
                make_chunk(1, decode_start_frame=1, start_frame=1, end_frame=4, core_start_frame=2, core_end_frame=4),
            ]
            chunk_dirs = [temp_dir / chunk.output_dir_name for chunk in chunks]
            write_chunk_outputs(chunk_dirs[0], frame_start=0, frame_end=3)
            write_chunk_outputs(chunk_dirs[1], frame_start=1, frame_end=4)

            with patch(
                "football_tracking.detector_candidate_contract.build_tracking_contract",
                wraps=build_tracking_contract,
            ) as build_contract:
                stitch_chunk_outputs(
                    chunks,
                    chunk_dirs,
                    temp_dir / "merged",
                    candidate_source_sha256=TEST_SOURCE_SHA256,
                )

            self.assertEqual(5, build_contract.call_count)
            self.assertTrue(all(len(call.kwargs["frames"]) == 1 for call in build_contract.call_args_list))
            self.assertTrue(all(len(call.kwargs["candidates"]) <= 1 for call in build_contract.call_args_list))

    def test_stitch_chunk_outputs_uses_configured_output_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp_dir = Path(temp_name)
            output_config = OutputConfig(csv_name="custom_track.csv", debug_jsonl_name="custom_debug.jsonl")
            chunk = make_chunk(
                0,
                decode_start_frame=0,
                start_frame=0,
                end_frame=2,
                core_start_frame=0,
                core_end_frame=2,
            )
            chunk_dir = temp_dir / chunk.output_dir_name
            write_chunk_outputs(chunk_dir, frame_start=0, frame_end=2, output_config=output_config)

            stitch_chunk_outputs(
                [chunk],
                [chunk_dir],
                temp_dir / "merged",
                output_config=output_config,
                candidate_source_sha256=TEST_SOURCE_SHA256,
            )

            self.assertTrue((temp_dir / "merged" / "custom_track.csv").exists())
            self.assertTrue((temp_dir / "merged" / "custom_debug.jsonl").exists())

    def test_stitch_chunk_outputs_tolerates_matching_missing_tail_in_final_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp_dir = Path(temp_name)
            chunks = [
                make_chunk(0, decode_start_frame=0, start_frame=0, end_frame=6, core_start_frame=0, core_end_frame=4),
                make_chunk(1, decode_start_frame=3, start_frame=3, end_frame=9, core_start_frame=5, core_end_frame=9),
            ]
            chunk_dirs = [temp_dir / chunk.output_dir_name for chunk in chunks]
            write_chunk_outputs(chunk_dirs[0], frame_start=0, frame_end=6)
            write_chunk_outputs(
                chunk_dirs[1],
                frame_start=3,
                frame_end=9,
                missing_csv_frames={8, 9},
                missing_debug_frames={8, 9},
            )
            output_dir = temp_dir / "merged"

            report = stitch_chunk_outputs(
                chunks,
                chunk_dirs,
                output_dir,
                candidate_source_sha256=TEST_SOURCE_SHA256,
            )

            with (output_dir / "ball_track.csv").open("r", newline="", encoding="utf-8-sig") as csv_file:
                rows = list(csv.reader(csv_file))
            self.assertEqual(list(range(8)), [int(row[0]) for row in rows[1:]])

            debug_lines = (output_dir / "debug.jsonl").read_text(encoding="utf-8").splitlines()
            debug_items = [json.loads(line) for line in debug_lines]
            self.assertEqual(list(range(8)), [int(item["frame"]) for item in debug_items])

            self.assertEqual(8, report["frame_count"])
            self.assertEqual(
                [
                    {
                        "type": "truncated_final_tail",
                        "chunk_index": 1,
                        "chunk_name": "chunk_0001",
                        "first_missing_frame": 8,
                        "last_missing_frame": 9,
                        "missing_frame_count": 2,
                        "planned_core_end_frame": 9,
                        "stitched_core_end_frame": 7,
                    }
                ],
                report["boundary_events"],
            )

    def test_stitch_chunk_outputs_rejects_empty_chunk_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            with self.assertRaisesRegex(ValueError, "No temporal chunks"):
                stitch_chunk_outputs(
                    [],
                    [],
                    Path(temp_name) / "merged",
                    candidate_source_sha256=TEST_SOURCE_SHA256,
                )

    def test_stitch_chunk_outputs_rejects_candidate_with_absent_contract_frame(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp_dir = Path(temp_name)
            chunk = make_chunk(
                0,
                decode_start_frame=0,
                start_frame=0,
                end_frame=0,
                core_start_frame=0,
                core_end_frame=0,
            )
            chunk_dir = temp_dir / chunk.output_dir_name
            write_chunk_outputs(chunk_dir, frame_start=0, frame_end=0)
            write_tracking_contract(
                chunk_dir,
                frames=[{"frame_index": 0, "status": "unknown"}],
                candidates=[
                    {
                        "candidate_id": "candidate-absent-frame",
                        "frame_index": 1,
                        "bbox": [1, 1, 2, 2],
                        "confidence": 0.5,
                        "source": "test_detector",
                    }
                ],
            )

            merged_dir = temp_dir / "merged"
            with self.assertRaisesRegex(ValueError, "references an absent frame"):
                stitch_chunk_outputs(
                    [chunk],
                    [chunk_dir],
                    merged_dir,
                    candidate_source_sha256=TEST_SOURCE_SHA256,
                )
            self.assertFalse((merged_dir / "tracking_contract.v2.json").exists())
            self.assertEqual([], list(merged_dir.glob(".tracking_contract.v2.json.*")))

    def test_stitch_chunk_outputs_rejects_duplicate_selected_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp_dir = Path(temp_name)
            chunks = [
                make_chunk(0, decode_start_frame=0, start_frame=0, end_frame=2, core_start_frame=0, core_end_frame=1),
                make_chunk(1, decode_start_frame=1, start_frame=1, end_frame=3, core_start_frame=1, core_end_frame=2),
            ]
            chunk_dirs = [temp_dir / chunk.output_dir_name for chunk in chunks]
            write_chunk_outputs(chunk_dirs[0], frame_start=0, frame_end=2)
            write_chunk_outputs(chunk_dirs[1], frame_start=1, frame_end=3)

            with self.assertRaisesRegex(ValueError, "Duplicate selected frame"):
                stitch_chunk_outputs(
                    chunks,
                    chunk_dirs,
                    temp_dir / "merged",
                    candidate_source_sha256=TEST_SOURCE_SHA256,
                )

    def test_stitch_chunk_outputs_rejects_matching_missing_tail_in_non_final_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp_dir = Path(temp_name)
            chunks = [
                make_chunk(0, decode_start_frame=0, start_frame=0, end_frame=6, core_start_frame=0, core_end_frame=4),
                make_chunk(1, decode_start_frame=3, start_frame=3, end_frame=9, core_start_frame=5, core_end_frame=9),
            ]
            chunk_dirs = [temp_dir / chunk.output_dir_name for chunk in chunks]
            write_chunk_outputs(
                chunk_dirs[0],
                frame_start=0,
                frame_end=6,
                missing_csv_frames={3, 4},
                missing_debug_frames={3, 4},
            )
            write_chunk_outputs(chunk_dirs[1], frame_start=3, frame_end=9)

            with self.assertRaisesRegex(ValueError, "Missing CSV frame 3 in chunk_0000"):
                stitch_chunk_outputs(
                    chunks,
                    chunk_dirs,
                    temp_dir / "merged",
                    candidate_source_sha256=TEST_SOURCE_SHA256,
                )

    def test_stitch_chunk_outputs_rejects_whole_missing_final_core(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp_dir = Path(temp_name)
            chunk = make_chunk(
                0,
                decode_start_frame=0,
                start_frame=0,
                end_frame=2,
                core_start_frame=0,
                core_end_frame=2,
            )
            chunk_dir = temp_dir / chunk.output_dir_name
            write_chunk_outputs(
                chunk_dir,
                frame_start=0,
                frame_end=2,
                missing_csv_frames={0, 1, 2},
                missing_debug_frames={0, 1, 2},
            )

            with self.assertRaisesRegex(ValueError, "Missing CSV frame 0 in chunk_0000"):
                stitch_chunk_outputs(
                    [chunk],
                    [chunk_dir],
                    temp_dir / "merged",
                    candidate_source_sha256=TEST_SOURCE_SHA256,
                )

    def test_stitch_chunk_outputs_rejects_missing_selected_csv_or_debug_frame(self) -> None:
        cases = [
            ("csv", {1}, set(), "Missing CSV frame"),
            ("debug", set(), {1}, "Missing debug frame"),
            ("both_interior", {1}, {1}, "Missing CSV frame"),
            ("tail_csv_only", {2}, set(), "Missing CSV frame"),
            ("tail_debug_only", set(), {2}, "Missing debug frame"),
        ]
        for _name, missing_csv_frames, missing_debug_frames, expected_message in cases:
            with self.subTest(expected_message=expected_message), tempfile.TemporaryDirectory() as temp_name:
                temp_dir = Path(temp_name)
                chunk = make_chunk(
                    0,
                    decode_start_frame=0,
                    start_frame=0,
                    end_frame=2,
                    core_start_frame=0,
                    core_end_frame=2,
                )
                chunk_dir = temp_dir / chunk.output_dir_name
                write_chunk_outputs(
                    chunk_dir,
                    frame_start=0,
                    frame_end=2,
                    missing_csv_frames=missing_csv_frames,
                    missing_debug_frames=missing_debug_frames,
                )

                with self.assertRaisesRegex(ValueError, expected_message):
                    stitch_chunk_outputs(
                        [chunk],
                        [chunk_dir],
                        temp_dir / "merged",
                        candidate_source_sha256=TEST_SOURCE_SHA256,
                    )


if __name__ == "__main__":
    unittest.main()
