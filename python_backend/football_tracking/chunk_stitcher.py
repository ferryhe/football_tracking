from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from football_tracking.config import OutputConfig
from football_tracking.temporal_chunks import TemporalChunk

CSV_HEADER = ["Frame", "X", "Y", "Confidence", "Status"]


def stitch_chunk_outputs(
    chunks: list[TemporalChunk],
    chunk_dirs: list[Path],
    output_dir: Path,
    output_config: OutputConfig | None = None,
) -> dict[str, Any]:
    """Merge raw chunk CSV/debug outputs, keeping each chunk's core frames."""
    if len(chunks) != len(chunk_dirs):
        raise ValueError("chunks and chunk_dirs must have the same length")
    if not chunks:
        raise ValueError("No temporal chunks to stitch")

    config = output_config or OutputConfig()
    selected_csv_rows: dict[int, list[str]] = {}
    selected_debug_items: dict[int, dict[str, Any]] = {}

    for chunk, chunk_dir in zip(chunks, chunk_dirs):
        csv_rows = _read_csv_rows(chunk_dir / config.csv_name)
        debug_items = _read_debug_items(chunk_dir / config.debug_jsonl_name)

        for frame in range(chunk.core_start_frame, chunk.core_end_frame + 1):
            if frame in selected_csv_rows:
                raise ValueError(f"Duplicate selected frame {frame}")
            if frame not in csv_rows:
                raise ValueError(f"Missing CSV frame {frame} in {chunk.output_dir_name}")
            if frame not in debug_items:
                raise ValueError(f"Missing debug frame {frame} in {chunk.output_dir_name}")
            selected_csv_rows[frame] = csv_rows[frame]
            selected_debug_items[frame] = debug_items[frame]

    output_dir.mkdir(parents=True, exist_ok=True)
    ordered_frames = sorted(selected_csv_rows)
    _write_csv_rows(output_dir / config.csv_name, ordered_frames, selected_csv_rows)
    _write_debug_items(output_dir / config.debug_jsonl_name, ordered_frames, selected_debug_items)

    report = _build_report(chunks, frame_count=len(ordered_frames))
    with (output_dir / "temporal_chunks_report.json").open("w", encoding="utf-8") as report_file:
        json.dump(report, report_file, ensure_ascii=False, indent=2)
    return report


def _read_csv_rows(path: Path) -> dict[int, list[str]]:
    rows: dict[int, list[str]] = {}
    with path.open("r", newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.reader(csv_file)
        header = next(reader, None)
        if header != CSV_HEADER:
            raise ValueError(f"Unexpected CSV header in {path}: {header}")
        for row in reader:
            if not row:
                continue
            frame = int(row[0])
            if frame in rows:
                raise ValueError(f"Duplicate CSV frame {frame} in {path}")
            rows[frame] = row
    return rows


def _read_debug_items(path: Path) -> dict[int, dict[str, Any]]:
    items: dict[int, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as debug_file:
        for line in debug_file:
            if not line.strip():
                continue
            item = json.loads(line)
            frame = int(item["frame"])
            if frame in items:
                raise ValueError(f"Duplicate debug frame {frame} in {path}")
            items[frame] = item
    return items


def _write_csv_rows(path: Path, frames: list[int], rows: dict[int, list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(CSV_HEADER)
        for frame in frames:
            writer.writerow(rows[frame])


def _write_debug_items(path: Path, frames: list[int], items: dict[int, dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as debug_file:
        for frame in frames:
            debug_file.write(json.dumps(items[frame], ensure_ascii=False) + "\n")


def _build_report(chunks: list[TemporalChunk], *, frame_count: int) -> dict[str, Any]:
    return {
        "chunk_count": len(chunks),
        "frame_count": frame_count,
        "source_chunk_names": [chunk.output_dir_name for chunk in chunks],
        "chunks": [
            {
                "index": chunk.index,
                "name": chunk.output_dir_name,
                "decode_start_frame": chunk.decode_start_frame,
                "start_frame": chunk.start_frame,
                "end_frame": chunk.end_frame,
                "core_start_frame": chunk.core_start_frame,
                "core_end_frame": chunk.core_end_frame,
            }
            for chunk in chunks
        ],
        "boundary_events": [],
    }
