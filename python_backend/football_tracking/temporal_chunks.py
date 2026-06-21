from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TemporalChunk:
    index: int
    decode_start_frame: int
    start_frame: int
    end_frame: int
    core_start_frame: int
    core_end_frame: int
    output_dir_name: str


def plan_temporal_chunks(
    *,
    source_total_frames: int,
    chunk_frames: int,
    overlap_frames: int,
    start_frame: int = 0,
    max_frames: int | None = None,
    decode_preroll_frames: int = 0,
) -> list[TemporalChunk]:
    """Plan global-frame chunk ranges for the source video.

    ``source_total_frames`` is the full source-video frame count, not the
    remaining frame count after ``start_frame``. Chunk ``start_frame`` and
    ``end_frame`` include tracking overlap; ``core_*`` is the non-overlapping
    range to keep after stitching; ``decode_start_frame`` is seek/decode warmup
    only and may be earlier than the tracking range.
    """
    if chunk_frames <= 0:
        raise ValueError("chunk_frames must be greater than 0")
    if overlap_frames < 0 or overlap_frames >= chunk_frames:
        raise ValueError("overlap_frames must be >= 0 and less than chunk_frames")
    if decode_preroll_frames < 0:
        raise ValueError("decode_preroll_frames must be greater than or equal to 0")

    if source_total_frames <= 0:
        return []

    effective_start = max(0, int(start_frame))
    if effective_start >= source_total_frames:
        return []

    if max_frames is None:
        effective_end_exclusive = source_total_frames
    else:
        max_frames = int(max_frames)
        if max_frames <= 0:
            return []
        effective_end_exclusive = min(source_total_frames, effective_start + max_frames)

    if effective_end_exclusive <= effective_start:
        return []

    chunks: list[TemporalChunk] = []
    core_start_frame = effective_start
    while core_start_frame < effective_end_exclusive:
        core_end_frame = min(core_start_frame + chunk_frames, effective_end_exclusive) - 1
        index = len(chunks)
        tracking_start_frame = max(0, core_start_frame - overlap_frames)
        chunks.append(
            TemporalChunk(
                index=index,
                decode_start_frame=max(0, tracking_start_frame - decode_preroll_frames),
                start_frame=tracking_start_frame,
                end_frame=min(source_total_frames - 1, core_end_frame + overlap_frames),
                core_start_frame=core_start_frame,
                core_end_frame=core_end_frame,
                output_dir_name=f"chunk_{index:04d}",
            )
        )
        core_start_frame = core_end_frame + 1

    return chunks
