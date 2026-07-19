from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from football_tracking.candidate_dataset import (
    DATASET_SCHEMA_VERSION,
    MAX_DECODE_WORK_FRAMES,
    SOURCE_MAP_SCHEMA_VERSION,
    CandidateDatasetError,
    build_candidate_dataset,
    decode_verified_frames,
    main,
)
from football_tracking.detector_candidate_contract import RuntimeTrackingContractWriter, assign_candidate_ids
from football_tracking.detector_development_common import DetectorDevelopmentError
from football_tracking.tracking_contracts import TRACKING_CONTRACT_REPORT_NAME, write_tracking_contract
from football_tracking.types import Candidate, OutputStatus, TrackResult, TrackState


class CandidateDatasetTests(unittest.TestCase):
    def test_verified_bounded_decode_returns_only_exact_source_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "source.bin"
            path.write_bytes(b"fixture")
            frames = _solid_frames(8, width=8, height=6)
            factory = CaptureFactory(
                {
                    str(path.resolve()): CaptureSpec(
                        frames,
                        decoder_positions_msec=[-50.0, 50.0, 100.0, 150.0, 200.0, 250.0, 300.0, 350.0],
                    )
                }
            )

            with patch(
                "football_tracking.candidate_dataset.cv2.VideoCapture",
                side_effect=factory,
            ):
                decoded, metadata = decode_verified_frames(
                    path.resolve(),
                    [6, 2, 6],
                    requested_decode_mode="sequential",
                    expected_width=8,
                    expected_height=6,
                    expected_frame_count=8,
                )

        self.assertEqual([2, 6], sorted(decoded))
        self.assertTrue(np.array_equal(frames[2], decoded[2]))
        self.assertEqual("sequential", metadata["effective_decode_mode"])
        self.assertEqual([2, 6], metadata["verified_frame_indices"])
        self.assertEqual(
            [
                {
                    "frame_index": 2,
                    "decoder_reported_pos_msec": 100.0,
                    "observation_method": "opencv_cap_prop_pos_msec_after_verified_frame_read",
                },
                {
                    "frame_index": 6,
                    "decoder_reported_pos_msec": 300.0,
                    "observation_method": "opencv_cap_prop_pos_msec_after_verified_frame_read",
                },
            ],
            metadata["frame_timing_observations"],
        )
        self.assertTrue(all(capture.released for capture in factory.instances))

    def test_verified_bounded_decode_records_seek_fallback_without_losing_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "source.bin"
            path.write_bytes(b"fixture")
            frames = _solid_frames(8, width=8, height=6)
            factory = CaptureFactory(
                {str(path.resolve()): CaptureSpec(frames)},
                first_seek_succeeds=False,
            )

            with patch(
                "football_tracking.candidate_dataset.cv2.VideoCapture",
                side_effect=factory,
            ):
                decoded, metadata = decode_verified_frames(
                    path.resolve(),
                    [3, 7],
                    requested_decode_mode="direct",
                    expected_width=8,
                    expected_height=6,
                    expected_frame_count=8,
                )

        self.assertEqual([3, 7], sorted(decoded))
        self.assertEqual("direct", metadata["requested_decode_mode"])
        self.assertEqual("sequential_fallback", metadata["effective_decode_mode"])
        self.assertEqual(2, len(factory.instances))
        self.assertEqual([], factory.instances[1].seek_calls)
        self.assertEqual(8, factory.instances[1].read_count)
        self.assertTrue(all(capture.released for capture in factory.instances))

    def test_verified_bounded_decode_rejects_missing_decoder_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "source.bin"
            path.write_bytes(b"fixture")
            frames = _solid_frames(3, width=8, height=6)
            factory = CaptureFactory(
                {
                    str(path.resolve()): CaptureSpec(
                        frames,
                        decoder_positions_msec=[-50.0, float("nan"), 50.0],
                    )
                }
            )

            with (
                patch(
                    "football_tracking.candidate_dataset.cv2.VideoCapture",
                    side_effect=factory,
                ),
                self.assertRaisesRegex(CandidateDatasetError, "finite POS_MSEC"),
            ):
                decode_verified_frames(
                    path.resolve(),
                    [1],
                    requested_decode_mode="sequential",
                    expected_width=8,
                    expected_height=6,
                    expected_frame_count=3,
                )

        self.assertTrue(all(capture.released for capture in factory.instances))

    def test_late_frame_seek_failure_fails_closed_without_linear_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "source.bin"
            path.write_bytes(b"fixture")
            frames = _solid_frames(MAX_DECODE_WORK_FRAMES + 100, width=8, height=6)
            factory = CaptureFactory(
                {str(path.resolve()): CaptureSpec(frames)},
                first_seek_succeeds=False,
                all_seeks_fail=True,
            )

            with (
                patch(
                    "football_tracking.candidate_dataset.cv2.VideoCapture",
                    side_effect=factory,
                ),
                self.assertRaisesRegex(CandidateDatasetError, "bounded fallback"),
            ):
                decode_verified_frames(
                    path.resolve(),
                    [MAX_DECODE_WORK_FRAMES + 50],
                    requested_decode_mode="direct",
                    expected_width=8,
                    expected_height=6,
                    expected_frame_count=len(frames),
                )

        self.assertEqual(1, len(factory.instances))
        self.assertEqual(0, sum(capture.read_count for capture in factory.instances))

    def test_sequential_decode_rejects_work_beyond_the_hard_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "source.bin"
            path.write_bytes(b"fixture")
            frames = _solid_frames(MAX_DECODE_WORK_FRAMES + 2, width=8, height=6)
            factory = CaptureFactory({str(path.resolve()): CaptureSpec(frames)})

            with (
                patch(
                    "football_tracking.candidate_dataset.cv2.VideoCapture",
                    side_effect=factory,
                ),
                self.assertRaisesRegex(CandidateDatasetError, "work limit"),
            ):
                decode_verified_frames(
                    path.resolve(),
                    [MAX_DECODE_WORK_FRAMES + 1],
                    requested_decode_mode="sequential",
                    expected_width=8,
                    expected_height=6,
                    expected_frame_count=len(frames),
                )

        self.assertEqual(0, factory.instances[0].read_count)

    def test_sequential_decode_observes_cancellation_inside_the_linear_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "source.bin"
            path.write_bytes(b"fixture")
            frames = _solid_frames(20, width=8, height=6)
            factory = CaptureFactory({str(path.resolve()): CaptureSpec(frames)})

            with (
                patch(
                    "football_tracking.candidate_dataset.cv2.VideoCapture",
                    side_effect=factory,
                ),
                self.assertRaisesRegex(CandidateDatasetError, "cancelled"),
            ):
                decode_verified_frames(
                    path.resolve(),
                    [19],
                    requested_decode_mode="sequential",
                    expected_width=8,
                    expected_height=6,
                    expected_frame_count=len(frames),
                    cancel_callback=lambda: bool(factory.instances) and factory.instances[0].read_count >= 3,
                )

        self.assertEqual(3, factory.instances[0].read_count)

    def test_preroll_sparse_late_frames_use_bounded_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "source.bin"
            path.write_bytes(b"fixture")
            frames = _solid_frames(10_001, width=8, height=6)
            factory = CaptureFactory({str(path.resolve()): CaptureSpec(frames)})

            with patch(
                "football_tracking.candidate_dataset.cv2.VideoCapture",
                side_effect=factory,
            ):
                decoded, metadata = decode_verified_frames(
                    path.resolve(),
                    [1_000, 10_000],
                    requested_decode_mode="preroll",
                    expected_width=8,
                    expected_height=6,
                    expected_frame_count=len(frames),
                )

        self.assertEqual([1_000, 10_000], sorted(decoded))
        self.assertEqual("preroll_verified", metadata["effective_decode_mode"])
        self.assertLessEqual(factory.instances[0].read_count, 26)
        self.assertEqual([988, 9_988], factory.instances[0].seek_calls)

    def test_direct_and_preroll_decode_check_cancellation_per_frame(self) -> None:
        for mode, indices in (("direct", [2, 4]), ("preroll", [15])):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp_name:
                path = Path(temp_name) / "source.bin"
                path.write_bytes(b"fixture")
                frames = _solid_frames(20, width=8, height=6)
                factory = CaptureFactory({str(path.resolve()): CaptureSpec(frames)})

                with (
                    patch(
                        "football_tracking.candidate_dataset.cv2.VideoCapture",
                        side_effect=factory,
                    ),
                    self.assertRaisesRegex(CandidateDatasetError, "cancelled"),
                ):
                    decode_verified_frames(
                        path.resolve(),
                        indices,
                        requested_decode_mode=mode,
                        expected_width=8,
                        expected_height=6,
                        expected_frame_count=len(frames),
                        cancel_callback=lambda: bool(factory.instances) and factory.instances[0].read_count >= 1,
                    )

                self.assertEqual(1, factory.instances[0].read_count)

    def test_decode_preserves_service_shutdown_from_the_cancel_callback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "source.bin"
            path.write_bytes(b"fixture")
            frames = _solid_frames(20, width=8, height=6)
            factory = CaptureFactory({str(path.resolve()): CaptureSpec(frames)})

            def shutting_down() -> bool:
                raise DetectorDevelopmentError("service_shutting_down", "service is closing")

            with patch(
                "football_tracking.candidate_dataset.cv2.VideoCapture",
                side_effect=factory,
            ):
                with self.assertRaises(DetectorDevelopmentError) as raised:
                    decode_verified_frames(
                        path.resolve(),
                        [19],
                        requested_decode_mode="sequential",
                        expected_width=8,
                        expected_height=6,
                        expected_frame_count=len(frames),
                        cancel_callback=shutting_down,
                    )

        self.assertEqual("service_shutting_down", raised.exception.code)

    def test_runtime_contract_is_directly_compatible_with_operator_source_map(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            video_path = root / "source.bin"
            video_path.write_bytes(b"synthetic-video")
            candidate = Candidate(
                frame_index=2,
                x1=2.0,
                y1=1.0,
                x2=6.0,
                y2=5.0,
                confidence=0.75,
                source="yolo_sahi",
            )
            assign_candidate_ids([candidate], _sha256(video_path))
            writer = RuntimeTrackingContractWriter(root / "runtime", _sha256(video_path))
            writer.write(
                TrackResult(
                    frame_index=2,
                    output_status=OutputStatus.LOST,
                    state=TrackState.LOST,
                    point=None,
                    confidence=0.0,
                    reason="no_candidate_selected",
                    lost_frames=1,
                    raw_candidate_count=1,
                    filtered_candidate_count=0,
                    raw_candidates=[candidate],
                )
            )
            writer.close(publish=True)
            assert candidate.candidate_id is not None
            source_map_path, mapped_video_path = _write_source_map(
                root,
                frame_count=5,
                candidate_ids=[candidate.candidate_id],
            )
            factory = CaptureFactory(
                {str(mapped_video_path.resolve()): CaptureSpec(_solid_frames(5, width=8, height=6))}
            )

            with patch("football_tracking.candidate_dataset.cv2.VideoCapture", side_effect=factory):
                manifest = build_candidate_dataset(
                    root / "runtime" / TRACKING_CONTRACT_REPORT_NAME,
                    source_map_path,
                    root / "dataset",
                )

        self.assertEqual([candidate.candidate_id], [sample["candidate_id"] for sample in manifest["samples"]])

    def test_runtime_candidate_ids_reject_tampered_evidence_or_wrong_video_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            video_path = root / "source.bin"
            video_path.write_bytes(b"synthetic-video")
            candidate = Candidate(
                frame_index=2,
                x1=2.0,
                y1=1.0,
                x2=6.0,
                y2=5.0,
                confidence=0.75,
                source="detector",
            )
            assign_candidate_ids([candidate], _sha256(video_path))
            assert candidate.candidate_id is not None
            source_map_path, mapped_video_path = _write_source_map(
                root,
                frame_count=5,
                candidate_ids=[candidate.candidate_id],
            )
            tampered_contract = _write_contract(
                root / "tampered-contract",
                [_candidate(candidate.candidate_id, frame_index=2, bbox=[2, 1, 7, 5])],
            )

            with self.assertRaisesRegex(CandidateDatasetError, "source-scoped candidate ID"):
                build_candidate_dataset(tampered_contract, source_map_path, root / "tampered-dataset")

            valid_contract = _write_contract(
                root / "valid-contract",
                [_candidate(candidate.candidate_id, frame_index=2, bbox=[2, 1, 6, 5])],
            )
            mapped_video_path.write_bytes(b"different-video")
            source_map = json.loads(source_map_path.read_text(encoding="utf-8"))
            source_map["sources"][0]["video_sha256"] = _sha256(mapped_video_path)
            source_map_path.write_text(json.dumps(source_map), encoding="utf-8")

            with self.assertRaisesRegex(CandidateDatasetError, "source-scoped candidate ID"):
                build_candidate_dataset(valid_contract, source_map_path, root / "wrong-video-dataset")

    def test_builds_ordered_rgb_tensors_without_markup_and_relative_manifest_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path = _write_contract(
                root / "contract",
                [
                    _candidate("candidate-b", frame_index=3, bbox=[2, 1, 6, 5]),
                    _candidate("candidate-a", frame_index=2, bbox=[2, 1, 6, 5]),
                ],
            )
            source_map_path, video_path = _write_source_map(
                root,
                frame_count=6,
                candidate_ids=["candidate-a", "candidate-b"],
            )
            frames = _solid_frames(6, width=8, height=6)
            factory = CaptureFactory({str(video_path.resolve()): CaptureSpec(frames)})

            with patch("football_tracking.candidate_dataset.cv2.VideoCapture", side_effect=factory):
                manifest = build_candidate_dataset(contract_path, source_map_path, root / "dataset")

            self.assertEqual(DATASET_SCHEMA_VERSION, manifest["schema_version"])
            self.assertEqual(["candidate-a", "candidate-b"], [sample["candidate_id"] for sample in manifest["samples"]])
            sample = manifest["samples"][0]
            tight_path = root / "dataset" / sample["artifacts"]["tight_tensor"]["path"]
            context_path = root / "dataset" / sample["artifacts"]["context_tensor"]["path"]
            montage_path = root / "dataset" / sample["artifacts"]["review_montage"]["path"]
            tight = np.load(tight_path, allow_pickle=False)
            context = np.load(context_path, allow_pickle=False)

            self.assertEqual((5, 3, 64, 64), tight.shape)
            self.assertEqual((5, 3, 128, 128), context.shape)
            self.assertEqual(np.uint8, tight.dtype)
            self.assertEqual([30, 20, 10], tight[0, :, 0, 0].tolist())
            self.assertTrue(
                all(np.unique(tight[index, channel]).size == 1 for index in range(5) for channel in range(3))
            )
            self.assertTrue(
                all(np.unique(context[index, channel]).size == 1 for index in range(5) for channel in range(3))
            )
            self.assertTrue(montage_path.is_file())
            self.assertEqual([2.0, 1.0, 6.0, 5.0], sample["bbox_requested_pixels"])
            self.assertEqual([2.0, 1.0, 6.0, 5.0], sample["bbox_clamped_pixels"])
            self.assertEqual([1, 0, 7, 6], sample["crop_windows"]["tight_pixels"])
            self.assertEqual([0, 0, 8, 6], sample["crop_windows"]["context_pixels"])
            for artifact in sample["artifacts"].values():
                self.assertFalse(Path(artifact["path"]).is_absolute())
                self.assertEqual(_sha256(root / "dataset" / artifact["path"]), artifact["sha256"])
            self.assertFalse(Path(manifest["contract"]["path"]).is_absolute())
            self.assertFalse(Path(manifest["sources"][0]["path"]).is_absolute())
            self.assertEqual("sequential", manifest["sources"][0]["requested_decode_mode"])
            self.assertEqual("sequential", manifest["sources"][0]["effective_decode_mode"])
            self.assertTrue(all(not capture.seek_calls for capture in factory.instances))

    def test_records_requested_and_actual_indices_with_nearest_edge_padding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path = _write_contract(
                root / "contract",
                [
                    _candidate("first", frame_index=0, bbox=[1, 1, 4, 4]),
                    _candidate("last", frame_index=2, bbox=[1, 1, 4, 4]),
                ],
            )
            source_map_path, video_path = _write_source_map(
                root,
                width=5,
                height=5,
                frame_count=3,
                candidate_ids=["first", "last"],
            )
            factory = CaptureFactory({str(video_path.resolve()): CaptureSpec(_solid_frames(3, width=5, height=5))})

            with patch("football_tracking.candidate_dataset.cv2.VideoCapture", side_effect=factory):
                manifest = build_candidate_dataset(contract_path, source_map_path, root / "dataset")

            by_id = {sample["candidate_id"]: sample for sample in manifest["samples"]}
            first = by_id["first"]["frames"]
            last = by_id["last"]["frames"]
            self.assertEqual([-2, -1, 0, 1, 2], [item["requested_index"] for item in first])
            self.assertEqual([0, 0, 0, 1, 2], [item["actual_index"] for item in first])
            self.assertEqual(["nearest_edge", "nearest_edge", None, None, None], [item["padding"] for item in first])
            self.assertEqual([0, 1, 2, 2, 2], [item["actual_index"] for item in last])
            self.assertEqual([None, None, None, "nearest_edge", "nearest_edge"], [item["padding"] for item in last])

    def test_version_bbox_scaling_and_group_keys_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            candidates = [
                _candidate("small", frame_index=2, bbox=[2, 1, 6, 5]),
                _candidate("large", frame_index=2, bbox=[4, 2, 12, 10]),
            ]
            contract_path = _write_contract(root / "contract", candidates)
            source_map_path, paths = _write_multi_source_map(root)
            specs = {
                str(paths["small"].resolve()): CaptureSpec(_solid_frames(5, width=8, height=6)),
                str(paths["large"].resolve()): CaptureSpec(_solid_frames(5, width=16, height=12)),
            }

            with patch("football_tracking.candidate_dataset.cv2.VideoCapture", side_effect=CaptureFactory(specs)):
                first = build_candidate_dataset(contract_path, source_map_path, root / "dataset-a")
            with patch("football_tracking.candidate_dataset.cv2.VideoCapture", side_effect=CaptureFactory(specs)):
                second = build_candidate_dataset(contract_path, source_map_path, root / "dataset-b")

            self.assertEqual(first["dataset_version"], second["dataset_version"])
            self.assertIn("opencv", first["preprocessing_runtime"])
            self.assertIn("numpy", first["preprocessing_runtime"])
            by_id = {sample["candidate_id"]: sample for sample in first["samples"]}
            self.assertEqual(by_id["small"]["bbox_normalized"], by_id["large"]["bbox_normalized"])
            for sample in first["samples"]:
                suffix = sample["candidate_id"]
                self.assertEqual("detector", sample["detector_source"])
                self.assertEqual(f"variant-{suffix}", sample["variant_id"])
                self.assertEqual(f"group-{suffix}", sample["group_id"])
                self.assertEqual(f"temporal-{suffix}", sample["temporal_group"])
                self.assertEqual(f"split-{suffix}", sample["split_group"])
                self.assertEqual(0.75, sample["confidence"])

            original_resize = cv2.resize

            def changed_resize(*args: object, **kwargs: object) -> np.ndarray:
                resized = original_resize(*args, **kwargs)
                resized[0, 0, 0] ^= 1
                return resized

            with (
                patch("football_tracking.candidate_dataset.cv2.VideoCapture", side_effect=CaptureFactory(specs)),
                patch("football_tracking.candidate_dataset.cv2.resize", side_effect=changed_resize),
            ):
                changed = build_candidate_dataset(contract_path, source_map_path, root / "dataset-c")
            self.assertNotEqual(first["dataset_version"], changed["dataset_version"])

    def test_invalid_empty_dangling_and_metadata_mismatch_fail_closed(self) -> None:
        cases: list[tuple[str, list[dict[str, object]]]] = [
            ("empty", []),
            ("unbound-candidate", [_candidate("absent")]),
        ]
        for name, candidates in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                contract_path = _write_contract(root / "contract", candidates)
                source_map_path, _ = _write_source_map(root)
                with self.assertRaises(CandidateDatasetError):
                    build_candidate_dataset(contract_path, source_map_path, root / "dataset")
                self.assertFalse((root / "dataset").exists())

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            invalid_contract = root / "invalid.json"
            invalid_contract.write_text(json.dumps({"schema_version": "1.0", "candidates": [{}]}), encoding="utf-8")
            source_map_path, _ = _write_source_map(root)
            with self.assertRaises(CandidateDatasetError):
                build_candidate_dataset(invalid_contract, source_map_path, root / "dataset")

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path = _write_contract(root / "contract", [_candidate("one")])
            outside = root.parent / "outside-video.bin"
            outside.write_bytes(b"outside")
            source_map_path, _ = _write_source_map(root)
            payload = json.loads(source_map_path.read_text(encoding="utf-8"))
            payload["sources"][0]["video_path"] = "../outside-video.bin"
            source_map_path.write_text(json.dumps(payload), encoding="utf-8")
            try:
                with self.assertRaises(CandidateDatasetError):
                    build_candidate_dataset(contract_path, source_map_path, root / "dataset")
            finally:
                outside.unlink(missing_ok=True)

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path = _write_contract(root / "contract", [_candidate("one")])
            source_map_path, video_path = _write_source_map(root, width=8, height=6, frame_count=5)
            mismatched = CaptureFactory({str(video_path.resolve()): CaptureSpec(_solid_frames(5, width=9, height=6))})
            with patch("football_tracking.candidate_dataset.cv2.VideoCapture", side_effect=mismatched):
                with self.assertRaises(CandidateDatasetError):
                    build_candidate_dataset(contract_path, source_map_path, root / "dataset")

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path = _write_contract(root / "contract", [_candidate("one")])
            source_map_path, video_path = _write_source_map(root)
            video_path.write_bytes(b"same-metadata-different-content")
            factory = CaptureFactory({str(video_path.resolve()): CaptureSpec(_solid_frames(5, width=8, height=6))})
            with patch("football_tracking.candidate_dataset.cv2.VideoCapture", side_effect=factory):
                with self.assertRaisesRegex(CandidateDatasetError, "video_sha256 mismatch"):
                    build_candidate_dataset(contract_path, source_map_path, root / "dataset")
            self.assertEqual([], factory.instances)

    def test_duplicate_candidate_variant_binding_fails_before_decode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path = _write_contract(root / "contract", [_candidate("one")])
            paths = [root / "one.bin", root / "two.bin"]
            for path in paths:
                path.write_bytes(path.stem.encode())
            entries = [
                _source_entry(
                    path.stem,
                    path.name,
                    width=8,
                    height=6,
                    frame_count=5,
                    candidate_ids=["one"],
                    video_sha256=_sha256(path),
                )
                for path in paths
            ]
            source_map_path = root / "source-map.json"
            source_map_path.write_text(
                json.dumps({"schema_version": SOURCE_MAP_SCHEMA_VERSION, "sources": entries}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CandidateDatasetError, "multiple source variants"):
                build_candidate_dataset(contract_path, source_map_path, root / "dataset")
            self.assertFalse((root / "dataset").exists())

    def test_identical_video_sha_requires_same_group_and_split_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path = _write_contract(root / "contract", [_candidate("one"), _candidate("two")])
            video_path = root / "shared.bin"
            video_path.write_bytes(b"shared-video")
            source_map_path = root / "source-map.json"
            source_map_path.write_text(
                json.dumps(
                    {
                        "schema_version": SOURCE_MAP_SCHEMA_VERSION,
                        "sources": [
                            _source_entry(
                                "one",
                                video_path.name,
                                width=8,
                                height=6,
                                frame_count=5,
                                candidate_ids=["one"],
                                video_sha256=_sha256(video_path),
                            ),
                            {
                                **_source_entry(
                                    "two",
                                    video_path.name,
                                    width=8,
                                    height=6,
                                    frame_count=5,
                                    candidate_ids=["two"],
                                    video_sha256=_sha256(video_path),
                                ),
                                "group_id": "different-group",
                                "split_group": "different-split",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            factory = CaptureFactory({str(video_path.resolve()): CaptureSpec(_solid_frames(5, width=8, height=6))})
            with patch("football_tracking.candidate_dataset.cv2.VideoCapture", side_effect=factory):
                with self.assertRaisesRegex(CandidateDatasetError, "share group_id and split_group"):
                    build_candidate_dataset(contract_path, source_map_path, root / "dataset")
            self.assertEqual([], factory.instances)

    def test_verified_seek_falls_back_sequentially_and_releases_every_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path = _write_contract(root / "contract", [_candidate("one", frame_index=2)])
            source_map_path, video_path = _write_source_map(root, frame_count=5, decode_mode="direct")
            frames = _solid_frames(5, width=8, height=6)
            factory = CaptureFactory(
                {str(video_path.resolve()): CaptureSpec(frames)},
                first_seek_succeeds=False,
            )

            with patch("football_tracking.candidate_dataset.cv2.VideoCapture", side_effect=factory):
                manifest = build_candidate_dataset(contract_path, source_map_path, root / "dataset")

            self.assertEqual("direct", manifest["sources"][0]["requested_decode_mode"])
            self.assertEqual("sequential_fallback", manifest["sources"][0]["effective_decode_mode"])
            self.assertEqual(2, len(factory.instances))
            self.assertTrue(all(capture.released for capture in factory.instances))

    def test_corrupt_or_truncated_sources_fail_and_release_capture(self) -> None:
        for name, spec in (
            ("corrupt", CaptureSpec([], opened=False)),
            ("truncated", CaptureSpec(_solid_frames(2, width=8, height=6), reported_frame_count=5)),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                contract_path = _write_contract(root / "contract", [_candidate("one", frame_index=2)])
                source_map_path, video_path = _write_source_map(root, frame_count=5)
                factory = CaptureFactory({str(video_path.resolve()): spec})
                with patch("football_tracking.candidate_dataset.cv2.VideoCapture", side_effect=factory):
                    with self.assertRaises(CandidateDatasetError):
                        build_candidate_dataset(contract_path, source_map_path, root / "dataset")
                self.assertTrue(factory.instances)
                self.assertTrue(all(capture.released for capture in factory.instances))
                self.assertFalse((root / "dataset").exists())

    def test_manifest_is_written_after_artifacts_and_failed_staging_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path = _write_contract(root / "contract", [_candidate("one", frame_index=2)])
            source_map_path, video_path = _write_source_map(root, frame_count=5)
            factory = CaptureFactory({str(video_path.resolve()): CaptureSpec(_solid_frames(5, width=8, height=6))})

            def assert_artifacts_then_fail(path: Path, payload: dict[str, object]) -> None:
                for sample in payload["samples"]:  # type: ignore[index]
                    for artifact in sample["artifacts"].values():
                        self.assertTrue((path.parent / artifact["path"]).is_file())
                raise RuntimeError("manifest write failed")

            with (
                patch("football_tracking.candidate_dataset.cv2.VideoCapture", side_effect=factory),
                patch("football_tracking.candidate_dataset._write_manifest", side_effect=assert_artifacts_then_fail),
            ):
                with self.assertRaises(RuntimeError):
                    build_candidate_dataset(contract_path, source_map_path, root / "dataset")

            self.assertFalse((root / "dataset").exists())
            self.assertEqual([], list(root.glob(".dataset.staging-*")))

    def test_cli_reports_structured_errors_and_nonzero_status(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = main([])

        payload = json.loads(stderr.getvalue())
        self.assertNotEqual(0, status)
        self.assertFalse(payload["ok"])
        self.assertEqual("CandidateDatasetError", payload["error"]["type"])

    def test_sparse_sequential_candidates_stream_with_five_frame_cache_and_single_scan(self) -> None:
        from football_tracking import candidate_dataset

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            candidate_ids = [f"sparse-{index:03d}" for index in range(30)]
            candidates = [
                _candidate(candidate_id, frame_index=2 + index * 10, bbox=[20, 10, 40, 30])
                for index, candidate_id in enumerate(candidate_ids)
            ]
            contract_path = _write_contract(root / "contract", candidates)
            source_map_path, video_path = _write_source_map(
                root,
                width=128,
                height=72,
                frame_count=305,
                candidate_ids=candidate_ids,
                decode_mode="sequential",
            )
            frames = [
                np.full((72, 128, 3), [index % 200, (index + 1) % 200, (index + 2) % 200], dtype=np.uint8)
                for index in range(305)
            ]
            factory = CaptureFactory({str(video_path.resolve()): CaptureSpec(frames)})
            retained_counts: list[int] = []

            with (
                patch("football_tracking.candidate_dataset.cv2.VideoCapture", side_effect=factory),
                patch.object(
                    candidate_dataset, "_observe_frame_cache", side_effect=retained_counts.append, create=True
                ),
            ):
                manifest = build_candidate_dataset(contract_path, source_map_path, root / "dataset")

            capture = factory.instances[0]
            first = manifest["samples"][0]
            first_tensor = np.load(root / "dataset" / first["artifacts"]["tight_tensor"]["path"], allow_pickle=False)

        self.assertTrue(retained_counts)
        self.assertLessEqual(max(retained_counts), 5)
        self.assertEqual(candidate_ids, [sample["candidate_id"] for sample in manifest["samples"]])
        self.assertEqual(295, capture.read_count)
        self.assertEqual(1, len(factory.instances))
        self.assertTrue(capture.released)
        self.assertEqual([2, 1, 0], first_tensor[0, :, 0, 0].tolist())

    def test_direct_and_preroll_modes_retain_only_one_candidate_window(self) -> None:
        from football_tracking import candidate_dataset

        for decode_mode, maximum_reads_per_candidate in (("direct", 5), ("preroll", 17)):
            with self.subTest(decode_mode=decode_mode), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                candidate_ids = [f"{decode_mode}-{index:02d}" for index in range(10)]
                candidates = [
                    _candidate(candidate_id, frame_index=20 + index * 20)
                    for index, candidate_id in enumerate(candidate_ids)
                ]
                contract_path = _write_contract(root / "contract", candidates)
                source_map_path, video_path = _write_source_map(
                    root,
                    frame_count=225,
                    candidate_ids=candidate_ids,
                    decode_mode=decode_mode,
                )
                factory = CaptureFactory(
                    {str(video_path.resolve()): CaptureSpec(_solid_frames(225, width=8, height=6))}
                )
                retained_counts: list[int] = []

                with (
                    patch("football_tracking.candidate_dataset.cv2.VideoCapture", side_effect=factory),
                    patch.object(candidate_dataset, "_observe_frame_cache", side_effect=retained_counts.append),
                ):
                    manifest = build_candidate_dataset(contract_path, source_map_path, root / "dataset")

                self.assertEqual(candidate_ids, [sample["candidate_id"] for sample in manifest["samples"]])
                self.assertLessEqual(max(retained_counts), 5)
                self.assertLessEqual(factory.instances[0].read_count, maximum_reads_per_candidate * len(candidates))
                self.assertEqual(1, len(factory.instances))
                self.assertTrue(factory.instances[0].released)

    def test_baseexception_during_streaming_releases_capture_and_removes_staging(self) -> None:
        from football_tracking import candidate_dataset

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path = _write_contract(root / "contract", [_candidate("one", frame_index=2)])
            source_map_path, video_path = _write_source_map(root, frame_count=5)
            factory = CaptureFactory({str(video_path.resolve()): CaptureSpec(_solid_frames(5, width=8, height=6))})

            with (
                patch("football_tracking.candidate_dataset.cv2.VideoCapture", side_effect=factory),
                patch.object(candidate_dataset, "_write_sample", side_effect=KeyboardInterrupt("injected interrupt")),
            ):
                with self.assertRaisesRegex(KeyboardInterrupt, "injected"):
                    build_candidate_dataset(contract_path, source_map_path, root / "dataset")

            self.assertFalse((root / "dataset").exists())
            self.assertEqual([], list(root.glob(".dataset.staging-*")))
            self.assertTrue(all(capture.released for capture in factory.instances))

    def test_input_replacement_during_streaming_rolls_back_contract_map_and_video(self) -> None:
        from football_tracking import candidate_dataset

        for target_name in ("contract", "source_map", "video"):
            with self.subTest(target=target_name), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                contract_path = _write_contract(
                    root / "contract",
                    [_candidate("one", frame_index=2), _candidate("two", frame_index=8)],
                )
                source_map_path, video_path = _write_source_map(
                    root,
                    frame_count=12,
                    candidate_ids=["one", "two"],
                )
                factory = CaptureFactory({str(video_path.resolve()): CaptureSpec(_solid_frames(12, width=8, height=6))})
                target = {"contract": contract_path, "source_map": source_map_path, "video": video_path}[target_name]
                real_write_sample = candidate_dataset._write_sample
                mutated = False

                def mutate_after_first_sample(*args: object, **kwargs: object) -> dict[str, object]:
                    nonlocal mutated
                    sample = real_write_sample(*args, **kwargs)
                    if not mutated:
                        mutated = True
                        target.write_bytes(target.read_bytes() + b" ")
                    return sample

                with (
                    patch("football_tracking.candidate_dataset.cv2.VideoCapture", side_effect=factory),
                    patch.object(candidate_dataset, "_write_sample", side_effect=mutate_after_first_sample),
                ):
                    with self.assertRaises(CandidateDatasetError):
                        build_candidate_dataset(contract_path, source_map_path, root / "dataset")

                self.assertFalse((root / "dataset").exists())
                self.assertEqual([], list(root.glob(".dataset.staging-*")))
                self.assertTrue(all(capture.released for capture in factory.instances))


def _candidate(
    candidate_id: str,
    *,
    source: str = "detector",
    frame_index: int = 2,
    bbox: list[int] | None = None,
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "frame_index": frame_index,
        "bbox": bbox or [2, 1, 6, 5],
        "confidence": 0.75,
        "source": source,
    }


def _write_contract(root: Path, candidates: list[dict[str, object]]) -> Path:
    write_tracking_contract(root, candidates=candidates)
    return root / TRACKING_CONTRACT_REPORT_NAME


def _write_source_map(
    root: Path,
    *,
    width: int = 8,
    height: int = 6,
    frame_count: int = 5,
    candidate_ids: list[str] | None = None,
    decode_mode: str = "sequential",
) -> tuple[Path, Path]:
    video_path = root / "source.bin"
    video_path.write_bytes(b"synthetic-video")
    path = root / "source-map.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": SOURCE_MAP_SCHEMA_VERSION,
                "sources": [
                    _source_entry(
                        "detector",
                        video_path.name,
                        width=width,
                        height=height,
                        frame_count=frame_count,
                        candidate_ids=candidate_ids or ["one"],
                        video_sha256=_sha256(video_path),
                        decode_mode=decode_mode,
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    return path, video_path


def _write_multi_source_map(root: Path) -> tuple[Path, dict[str, Path]]:
    paths = {"small": root / "small.bin", "large": root / "large.bin"}
    for source, path in paths.items():
        path.write_bytes(f"video-{source}".encode())
    mapping_path = root / "source-map.json"
    mapping_path.write_text(
        json.dumps(
            {
                "schema_version": SOURCE_MAP_SCHEMA_VERSION,
                "sources": [
                    _source_entry(
                        "small",
                        paths["small"].name,
                        width=8,
                        height=6,
                        frame_count=5,
                        candidate_ids=["small"],
                        video_sha256=_sha256(paths["small"]),
                    ),
                    _source_entry(
                        "large",
                        paths["large"].name,
                        width=16,
                        height=12,
                        frame_count=5,
                        candidate_ids=["large"],
                        video_sha256=_sha256(paths["large"]),
                    ),
                ],
            }
        ),
        encoding="utf-8",
    )
    return mapping_path, paths


def _source_entry(
    suffix: str,
    video_path: str,
    *,
    width: int,
    height: int,
    frame_count: int,
    candidate_ids: list[str],
    video_sha256: str,
    decode_mode: str = "sequential",
) -> dict[str, object]:
    return {
        "video_path": video_path,
        "width": width,
        "height": height,
        "frame_count": frame_count,
        "video_sha256": video_sha256,
        "decode_mode": decode_mode,
        "candidate_ids": candidate_ids,
        "variant_id": f"variant-{suffix}",
        "group_id": f"group-{suffix}",
        "temporal_group": f"temporal-{suffix}",
        "split_group": f"split-{suffix}",
    }


def _solid_frames(count: int, *, width: int, height: int) -> list[np.ndarray]:
    return [np.full((height, width, 3), [10 + index, 20 + index, 30 + index], dtype=np.uint8) for index in range(count)]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CaptureSpec:
    def __init__(
        self,
        frames: list[np.ndarray],
        *,
        opened: bool = True,
        reported_frame_count: int | None = None,
        decoder_positions_msec: list[float] | None = None,
        fps: float = 20.0,
    ) -> None:
        self.frames = frames
        self.opened = opened
        self.reported_frame_count = reported_frame_count
        self.decoder_positions_msec = decoder_positions_msec
        self.fps = fps


class FakeCapture:
    def __init__(self, spec: CaptureSpec, *, seek_succeeds: bool = True) -> None:
        self.frames = [frame.copy() for frame in spec.frames]
        self.opened = spec.opened
        self.reported_frame_count = spec.reported_frame_count
        self.decoder_positions_msec = spec.decoder_positions_msec
        self.fps = spec.fps
        self.seek_succeeds = seek_succeeds
        self.position = 0
        self.released = False
        self.seek_calls: list[int] = []
        self.read_count = 0

    def isOpened(self) -> bool:
        return self.opened

    def get(self, prop: int) -> float:
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self.frames[0].shape[1]) if self.frames else 0.0
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self.frames[0].shape[0]) if self.frames else 0.0
        if prop == cv2.CAP_PROP_FRAME_COUNT:
            return float(self.reported_frame_count if self.reported_frame_count is not None else len(self.frames))
        if prop == cv2.CAP_PROP_POS_FRAMES:
            return float(self.position)
        if prop == cv2.CAP_PROP_POS_MSEC:
            decoded_index = max(0, self.position - 1)
            if self.decoder_positions_msec is not None:
                return float(self.decoder_positions_msec[decoded_index])
            return float(decoded_index * 1000.0 / self.fps)
        if prop == cv2.CAP_PROP_FPS:
            return float(self.fps)
        return 0.0

    def set(self, prop: int, value: float) -> bool:
        if prop != cv2.CAP_PROP_POS_FRAMES or not self.seek_succeeds:
            return False
        self.seek_calls.append(int(value))
        self.position = int(value)
        return True

    def read(self) -> tuple[bool, np.ndarray | None]:
        self.read_count += 1
        if self.position >= len(self.frames):
            return False, None
        frame = self.frames[self.position].copy()
        self.position += 1
        return True, frame

    def release(self) -> None:
        self.released = True


class CaptureFactory:
    def __init__(
        self,
        specs: dict[str, CaptureSpec],
        *,
        first_seek_succeeds: bool = True,
        all_seeks_fail: bool = False,
    ) -> None:
        self.specs = specs
        self.first_seek_succeeds = first_seek_succeeds
        self.all_seeks_fail = all_seeks_fail
        self.instances: list[FakeCapture] = []

    def __call__(self, path: str) -> FakeCapture:
        capture = FakeCapture(
            self.specs[path],
            seek_succeeds=(False if self.all_seeks_fail else self.first_seek_succeeds or bool(self.instances)),
        )
        self.instances.append(capture)
        return capture


if __name__ == "__main__":
    unittest.main()
