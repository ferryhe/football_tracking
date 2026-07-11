from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from football_tracking.detector_candidate_contract import (
    CandidateSourceChangedError,
    RuntimeTrackingContractWriter,
    _acquire_contract_output_lock,
    assign_candidate_ids,
    candidate_to_contract_record,
    capture_candidate_source_snapshot,
    compute_candidate_source_sha256,
    verify_candidate_source_snapshot,
)
from football_tracking.tracking_contracts import TRACKING_CONTRACT_REPORT_NAME, load_tracking_contract
from football_tracking.types import Candidate, OutputStatus, TrackResult, TrackState

TEST_SOURCE_SHA256 = hashlib.sha256(b"source").hexdigest()


def _candidate(frame_index: int, x: float, *, label: str = "ball") -> Candidate:
    return Candidate(
        frame_index=frame_index,
        x1=x,
        y1=10.0,
        x2=x + 4.0,
        y2=14.0,
        confidence=0.75,
        label=label,
        source="yolo_sahi",
    )


def _track_result(frame_index: int, candidates: list[Candidate]) -> TrackResult:
    return TrackResult(
        frame_index=frame_index,
        output_status=OutputStatus.LOST,
        state=TrackState.LOST,
        point=None,
        confidence=0.0,
        reason="no_candidate",
        lost_frames=1,
        raw_candidate_count=len(candidates),
        filtered_candidate_count=0,
        raw_candidates=candidates,
    )


class DetectorCandidateIdentityTests(unittest.TestCase):
    def test_ids_are_source_scoped_and_detector_order_independent(self) -> None:
        source_a = hashlib.sha256(b"source-a").hexdigest()
        source_b = hashlib.sha256(b"source-b").hexdigest()
        first = [_candidate(8, 30.0), _candidate(8, 10.0, label="class-a"), _candidate(8, 10.0, label="class-b")]
        second = [_candidate(8, 10.0, label="class-b"), _candidate(8, 30.0), _candidate(8, 10.0, label="class-a")]

        assign_candidate_ids(first, source_a)
        assign_candidate_ids(second, source_a)
        first_records = sorted(
            (candidate_to_contract_record(item) for item in first), key=lambda item: item["candidate_id"]
        )
        second_records = sorted(
            (candidate_to_contract_record(item) for item in second), key=lambda item: item["candidate_id"]
        )

        self.assertEqual(first_records, second_records)
        self.assertEqual(3, len({item["candidate_id"] for item in first_records}))

        assign_candidate_ids(second, source_b)
        self.assertTrue(
            {item["candidate_id"] for item in first_records}.isdisjoint(
                {candidate.candidate_id for candidate in second}
            )
        )

    def test_real_source_scope_uses_streamed_video_content_not_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            first_path = root / "first.mp4"
            moved_path = root / "renamed.mp4"
            first_path.write_bytes(b"same-video-content")
            config = SimpleNamespace(
                input_video=first_path,
                runtime=SimpleNamespace(candidate_source_sha256=None),
                mock=SimpleNamespace(enabled=False),
            )

            first_sha256 = compute_candidate_source_sha256(config)
            first_path.rename(moved_path)
            config.input_video = moved_path
            moved_sha256 = compute_candidate_source_sha256(config)
            moved_path.write_bytes(b"different-video-content")
            changed_sha256 = compute_candidate_source_sha256(config)

        self.assertEqual(hashlib.sha256(b"same-video-content").hexdigest(), first_sha256)
        self.assertEqual(first_sha256, moved_sha256)
        self.assertNotEqual(first_sha256, changed_sha256)

    def test_precomputed_source_scope_must_be_canonical_sha256(self) -> None:
        config = SimpleNamespace(
            input_video=Path("unused.mp4"),
            runtime=SimpleNamespace(candidate_source_sha256="ABC"),
            mock=SimpleNamespace(enabled=False),
        )

        with self.assertRaisesRegex(ValueError, "64 lowercase"):
            compute_candidate_source_sha256(config)

    def test_source_snapshot_detects_content_change_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            video_path = Path(temp_name) / "input.mp4"
            video_path.write_bytes(b"source-a")
            config = SimpleNamespace(
                input_video=video_path,
                runtime=SimpleNamespace(candidate_source_sha256=None),
                mock=SimpleNamespace(enabled=False),
            )
            snapshot = capture_candidate_source_snapshot(config)
            video_path.write_bytes(b"source-b")

            with self.assertRaisesRegex(CandidateSourceChangedError, "content changed"):
                verify_candidate_source_snapshot(config, snapshot, verify_content=True)


class RuntimeTrackingContractWriterTests(unittest.TestCase):
    def test_writer_publishes_valid_candidate_populated_contract(self) -> None:
        source_sha256 = TEST_SOURCE_SHA256
        candidates = assign_candidate_ids([_candidate(3, 30.0), _candidate(3, 10.0)], source_sha256)
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            writer = RuntimeTrackingContractWriter(output_dir, source_sha256)
            writer.write(_track_result(3, candidates))

            writer.close(publish=True)

            contract = load_tracking_contract(output_dir)

        self.assertEqual("loaded", contract["artifact_status"])
        self.assertEqual({"video_sha256": source_sha256}, contract["source"])
        self.assertEqual([], contract["validation_errors"])
        self.assertEqual([3], [frame["frame_index"] for frame in contract["frames"]])
        self.assertEqual(
            sorted(candidate.candidate_id for candidate in candidates),
            [candidate["candidate_id"] for candidate in contract["candidates"]],
        )
        self.assertEqual([], contract["classifications"])
        self.assertEqual([], contract["decisions"])

    def test_abort_removes_stale_contract_and_spools(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            final_path = output_dir / TRACKING_CONTRACT_REPORT_NAME
            final_path.write_text("stale", encoding="utf-8")
            writer = RuntimeTrackingContractWriter(output_dir, TEST_SOURCE_SHA256)

            writer.close(publish=False)

            self.assertFalse(final_path.exists())
            self.assertEqual([], list(output_dir.glob(f".{TRACKING_CONTRACT_REPORT_NAME}.*")))

    def test_invalid_candidate_fails_closed_without_partial_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            writer = RuntimeTrackingContractWriter(output_dir, TEST_SOURCE_SHA256)

            with self.assertRaisesRegex(ValueError, "missing a deterministic candidate_id"):
                writer.write(_track_result(1, [_candidate(1, 10.0)]))
            with self.assertRaisesRegex(RuntimeError, "no contract was published"):
                writer.close(publish=True)

            self.assertFalse((output_dir / TRACKING_CONTRACT_REPORT_NAME).exists())
            self.assertEqual([], list(output_dir.glob(f".{TRACKING_CONTRACT_REPORT_NAME}.*")))

    def test_duplicate_or_mismatched_frame_capture_fails_closed(self) -> None:
        source_sha256 = TEST_SOURCE_SHA256
        candidates = assign_candidate_ids([_candidate(2, 10.0)], source_sha256)
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            writer = RuntimeTrackingContractWriter(output_dir, source_sha256)
            writer.write(_track_result(2, candidates))

            with self.assertRaisesRegex(ValueError, "strictly increasing"):
                writer.write(_track_result(2, candidates))
            with self.assertRaisesRegex(RuntimeError, "no contract was published"):
                writer.close(publish=True)

            self.assertFalse((output_dir / TRACKING_CONTRACT_REPORT_NAME).exists())

        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            writer = RuntimeTrackingContractWriter(output_dir, source_sha256)
            mismatched = _track_result(2, candidates)
            mismatched.raw_candidate_count = 0

            with self.assertRaisesRegex(ValueError, "raw_candidate_count"):
                writer.write(mismatched)
            with self.assertRaisesRegex(RuntimeError, "no contract was published"):
                writer.close(publish=True)

            self.assertFalse((output_dir / TRACKING_CONTRACT_REPORT_NAME).exists())

    def test_writer_rejects_candidate_evidence_changed_after_id_assignment(self) -> None:
        candidates = assign_candidate_ids([_candidate(4, 10.0)], TEST_SOURCE_SHA256)
        candidates[0].x2 += 1.0
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            writer = RuntimeTrackingContractWriter(output_dir, TEST_SOURCE_SHA256)

            with self.assertRaisesRegex(ValueError, "mapped video SHA-256 or detector evidence"):
                writer.write(_track_result(4, candidates))
            with self.assertRaisesRegex(RuntimeError, "no contract was published"):
                writer.close(publish=True)

            self.assertFalse((output_dir / TRACKING_CONTRACT_REPORT_NAME).exists())

    def test_writer_rejects_candidate_referencing_another_frame(self) -> None:
        candidates = assign_candidate_ids([_candidate(99, 10.0)], TEST_SOURCE_SHA256)
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            writer = RuntimeTrackingContractWriter(output_dir, TEST_SOURCE_SHA256)

            with self.assertRaisesRegex(ValueError, "candidate frame_index"):
                writer.write(_track_result(2, candidates))
            with self.assertRaisesRegex(RuntimeError, "no contract was published"):
                writer.close(publish=True)

            self.assertFalse((output_dir / TRACKING_CONTRACT_REPORT_NAME).exists())

    def test_close_failure_still_cleans_all_spools_and_releases_output_lock(self) -> None:
        class CloseAfterDelegateFailure:
            def __init__(self, delegate) -> None:
                self.delegate = delegate

            def __getattr__(self, name: str):
                return getattr(self.delegate, name)

            def close(self) -> None:
                self.delegate.close()
                raise OSError("flush failed")

        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            writer = RuntimeTrackingContractWriter(output_dir, TEST_SOURCE_SHA256)
            writer._frames_file = CloseAfterDelegateFailure(writer._frames_file)

            with self.assertRaisesRegex(OSError, "flush failed"):
                writer.close(publish=False)

            self.assertEqual([], list(output_dir.glob(f".{TRACKING_CONTRACT_REPORT_NAME}.*")))
            next_writer = RuntimeTrackingContractWriter(output_dir, TEST_SOURCE_SHA256)
            next_writer.close(publish=False)

    def test_new_writer_removes_orphaned_spools_under_output_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            orphan_paths = [
                output_dir / f".{TRACKING_CONTRACT_REPORT_NAME}.orphan.frames.jsonl",
                output_dir / f".{TRACKING_CONTRACT_REPORT_NAME}.orphan.candidates.jsonl",
                output_dir / f".{TRACKING_CONTRACT_REPORT_NAME}.orphan.tmp",
            ]
            for path in orphan_paths:
                path.write_text("orphan", encoding="utf-8")

            writer = RuntimeTrackingContractWriter(output_dir, TEST_SOURCE_SHA256)

            self.assertTrue(all(not path.exists() for path in orphan_paths))
            writer.close(publish=False)
            self.assertEqual([], list(output_dir.glob(f".{TRACKING_CONTRACT_REPORT_NAME}.*")))

    def test_output_lock_prevents_concurrent_writer_from_cleaning_active_spools(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            writer = RuntimeTrackingContractWriter(output_dir, TEST_SOURCE_SHA256)

            with self.assertRaisesRegex(RuntimeError, "another tracking contract writer"):
                RuntimeTrackingContractWriter(output_dir, TEST_SOURCE_SHA256)

            self.assertTrue(writer.frames_path.exists())
            self.assertTrue(writer.candidates_path.exists())
            writer.close(publish=False)

    def test_lock_file_initialization_failure_is_not_reported_as_contention(self) -> None:
        class BrokenLockHandle:
            closed = False

            def seek(self, _offset: int, _whence: int = 0) -> None:
                raise OSError("lock file seek failed")

            def close(self) -> None:
                self.closed = True

        handle = BrokenLockHandle()
        with tempfile.TemporaryDirectory() as temp_name:
            with (
                patch.object(Path, "open", return_value=handle),
                self.assertRaisesRegex(RuntimeError, r"failed to initialize tracking contract lock file .*\.lock"),
            ):
                _acquire_contract_output_lock(Path(temp_name))

        self.assertTrue(handle.closed)

    def test_atomic_replace_failure_leaves_no_contract_or_temporary_file(self) -> None:
        source_sha256 = TEST_SOURCE_SHA256
        candidates = assign_candidate_ids([_candidate(2, 10.0)], source_sha256)
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            writer = RuntimeTrackingContractWriter(output_dir, source_sha256)
            writer.write(_track_result(2, candidates))

            with patch(
                "football_tracking.detector_candidate_contract.os.replace",
                side_effect=OSError("replace failed"),
            ):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    writer.close(publish=True)

            self.assertFalse((output_dir / TRACKING_CONTRACT_REPORT_NAME).exists())
            self.assertEqual([], list(output_dir.glob(f".{TRACKING_CONTRACT_REPORT_NAME}.*")))


if __name__ == "__main__":
    unittest.main()
