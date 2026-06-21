from __future__ import annotations

import dataclasses
import unittest

from football_tracking.temporal_chunks import TemporalChunk, plan_temporal_chunks


def chunk_tuples(chunks: list[TemporalChunk]) -> list[tuple[int, int, int, int, int]]:
    return [
        (
            chunk.decode_start_frame,
            chunk.start_frame,
            chunk.end_frame,
            chunk.core_start_frame,
            chunk.core_end_frame,
        )
        for chunk in chunks
    ]


def chunk_names(chunks: list[TemporalChunk]) -> list[tuple[int, str]]:
    return [(chunk.index, chunk.output_dir_name) for chunk in chunks]


class TemporalChunkPlanningTests(unittest.TestCase):
    def test_plan_temporal_chunks_adds_overlap_around_contiguous_core_ranges(self) -> None:
        chunks = plan_temporal_chunks(source_total_frames=250, chunk_frames=100, overlap_frames=10)

        self.assertEqual(
            [
                (0, 0, 109, 0, 99),
                (90, 90, 209, 100, 199),
                (190, 190, 249, 200, 249),
            ],
            chunk_tuples(chunks),
        )
        self.assertEqual(
            [(0, "chunk_0000"), (1, "chunk_0001"), (2, "chunk_0002")],
            chunk_names(chunks),
        )

    def test_plan_temporal_chunks_honors_runtime_start_and_max_frames(self) -> None:
        chunks = plan_temporal_chunks(
            source_total_frames=500,
            chunk_frames=100,
            overlap_frames=10,
            start_frame=50,
            max_frames=180,
        )

        self.assertEqual(
            [
                (40, 40, 159, 50, 149),
                (140, 140, 239, 150, 229),
            ],
            chunk_tuples(chunks),
        )

    def test_plan_temporal_chunks_treats_source_total_frames_as_global_source_count(self) -> None:
        chunks = plan_temporal_chunks(
            source_total_frames=500,
            chunk_frames=100,
            overlap_frames=10,
            start_frame=420,
        )

        self.assertEqual([(410, 410, 499, 420, 499)], chunk_tuples(chunks))

    def test_plan_temporal_chunks_tracks_decode_preroll_separately_from_overlap(self) -> None:
        chunks = plan_temporal_chunks(
            source_total_frames=250,
            chunk_frames=100,
            overlap_frames=10,
            decode_preroll_frames=30,
        )

        self.assertEqual(
            [
                (0, 0, 109, 0, 99),
                (60, 90, 209, 100, 199),
                (160, 190, 249, 200, 249),
            ],
            chunk_tuples(chunks),
        )

    def test_plan_temporal_chunks_returns_empty_for_empty_effective_ranges(self) -> None:
        self.assertEqual([], plan_temporal_chunks(source_total_frames=0, chunk_frames=100, overlap_frames=10))
        self.assertEqual(
            [],
            plan_temporal_chunks(source_total_frames=100, chunk_frames=100, overlap_frames=10, start_frame=100),
        )
        self.assertEqual(
            [],
            plan_temporal_chunks(source_total_frames=100, chunk_frames=100, overlap_frames=10, max_frames=0),
        )

    def test_plan_temporal_chunks_rejects_invalid_chunk_and_overlap_sizes(self) -> None:
        with self.assertRaises(ValueError):
            plan_temporal_chunks(source_total_frames=100, chunk_frames=0, overlap_frames=0)

        with self.assertRaises(ValueError):
            plan_temporal_chunks(source_total_frames=100, chunk_frames=100, overlap_frames=-1)

        with self.assertRaises(ValueError):
            plan_temporal_chunks(source_total_frames=100, chunk_frames=100, overlap_frames=100)

        with self.assertRaises(ValueError):
            plan_temporal_chunks(
                source_total_frames=100,
                chunk_frames=100,
                overlap_frames=10,
                decode_preroll_frames=-1,
            )

    def test_temporal_chunk_is_frozen(self) -> None:
        chunk = TemporalChunk(
            index=0,
            decode_start_frame=0,
            start_frame=0,
            end_frame=9,
            core_start_frame=0,
            core_end_frame=9,
            output_dir_name="chunk_0000",
        )

        with self.assertRaises(dataclasses.FrozenInstanceError):
            chunk.start_frame = 1


if __name__ == "__main__":
    unittest.main()
