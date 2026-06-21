from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from football_tracking.chunk_stitcher import stitch_chunk_outputs
from football_tracking.config import OutputConfig
from football_tracking.temporal_chunks import TemporalChunk


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


class ChunkStitcherTests(unittest.TestCase):
    def test_stitch_chunk_outputs_keeps_only_core_frames_sorted_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp_dir = Path(temp_name)
            chunks = [
                make_chunk(0, decode_start_frame=0, start_frame=0, end_frame=109, core_start_frame=0, core_end_frame=99),
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

            report = stitch_chunk_outputs(chunks, chunk_dirs, output_dir)

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

            stitch_chunk_outputs([chunk], [chunk_dir], temp_dir / "merged", output_config=output_config)

            self.assertTrue((temp_dir / "merged" / "custom_track.csv").exists())
            self.assertTrue((temp_dir / "merged" / "custom_debug.jsonl").exists())

    def test_stitch_chunk_outputs_rejects_empty_chunk_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            with self.assertRaisesRegex(ValueError, "No temporal chunks"):
                stitch_chunk_outputs([], [], Path(temp_name) / "merged")

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
                stitch_chunk_outputs(chunks, chunk_dirs, temp_dir / "merged")

    def test_stitch_chunk_outputs_rejects_missing_selected_csv_or_debug_frame(self) -> None:
        cases = [
            ("csv", {1}, set(), "Missing CSV frame"),
            ("debug", set(), {1}, "Missing debug frame"),
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
                    stitch_chunk_outputs([chunk], [chunk_dir], temp_dir / "merged")


if __name__ == "__main__":
    unittest.main()
