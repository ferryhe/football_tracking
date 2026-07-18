from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO
from unittest.mock import patch

from scripts.acquire_detector_models import (
    OFFICIAL_MODEL_CATALOG,
    AcquisitionError,
    acquire_official_model,
)


class AcquireDetectorModelsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.weights_root = Path(self.temporary.name)

    def test_official_catalog_is_pinned_to_v840_assets(self) -> None:
        self.assertEqual(
            {
                "official-coco-yolo11n": (
                    "yolo11n.pt",
                    5_613_764,
                    "0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1",
                    "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n.pt",
                ),
                "official-coco-yolo11s": (
                    "yolo11s.pt",
                    19_313_732,
                    "85a76fe86dd8afe384648546b56a7a78580c7cb7b404fc595f97969322d502d5",
                    "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11s.pt",
                ),
            },
            {
                model_id: (entry.filename, entry.size_bytes, entry.sha256, entry.url)
                for model_id, entry in OFFICIAL_MODEL_CATALOG.items()
            },
        )
        self.assertTrue(all("latest" not in entry.url for entry in OFFICIAL_MODEL_CATALOG.values()))

    def test_acquisition_accepts_catalog_ids_only(self) -> None:
        called = False

        def fetch(_: str, __: BinaryIO) -> None:
            nonlocal called
            called = True

        with self.assertRaisesRegex(AcquisitionError, "catalog"):
            acquire_official_model(
                "https://attacker.invalid/arbitrary.pt",
                weights_root=self.weights_root,
                fetch=fetch,
            )

        self.assertFalse(called)
        self.assertEqual([], list(self.weights_root.iterdir()))

    def test_acquisition_fails_closed_on_size_or_digest_mismatch(self) -> None:
        def fetch(_: str, target: BinaryIO) -> None:
            target.write(b"not the pinned model")

        with self.assertRaisesRegex(AcquisitionError, "size|digest"):
            acquire_official_model(
                "official-coco-yolo11n",
                weights_root=self.weights_root,
                fetch=fetch,
            )

        self.assertFalse((self.weights_root / "yolo11n.pt").exists())
        self.assertEqual([], list(self.weights_root.iterdir()))

    def test_acquisition_aborts_on_the_first_byte_beyond_the_pinned_size(self) -> None:
        entry = OFFICIAL_MODEL_CATALOG["official-coco-yolo11n"]
        observed_position: int | None = None

        def fetch(_: str, target: BinaryIO) -> None:
            nonlocal observed_position
            target.write(b"four")
            try:
                target.write(b"x")
            finally:
                observed_position = target.tell()

        with (
            patch.dict(
                OFFICIAL_MODEL_CATALOG,
                {
                    "official-coco-yolo11n": replace(
                        entry,
                        size_bytes=4,
                        sha256=hashlib.sha256(b"four").hexdigest(),
                    )
                },
            ),
            self.assertRaisesRegex(AcquisitionError, "exceeded pinned size"),
        ):
            acquire_official_model(
                "official-coco-yolo11n",
                weights_root=self.weights_root,
                fetch=fetch,
            )

        self.assertEqual(4, observed_position)
        self.assertEqual([], list(self.weights_root.iterdir()))

    def test_acquisition_reuses_only_an_exact_existing_file(self) -> None:
        payload = b"fixture weight bytes"
        digest = hashlib.sha256(payload).hexdigest()
        entry = OFFICIAL_MODEL_CATALOG["official-coco-yolo11n"]
        destination = self.weights_root / entry.filename
        destination.write_bytes(payload)

        def unexpected_fetch(_: str, __: BinaryIO) -> None:
            raise AssertionError("an exact local file must not be downloaded again")

        with patch.dict(
            OFFICIAL_MODEL_CATALOG,
            {
                "official-coco-yolo11n": replace(
                    entry,
                    size_bytes=len(payload),
                    sha256=digest,
                )
            },
        ):
            result = acquire_official_model(
                "official-coco-yolo11n",
                weights_root=self.weights_root,
                fetch=unexpected_fetch,
            )

        self.assertEqual(destination, result.path)
        self.assertEqual(digest, result.sha256)
        self.assertEqual(len(payload), result.size_bytes)

    def test_acquisition_detects_hash_to_publish_replacement(self) -> None:
        payload = b"fixture weight bytes"
        digest = hashlib.sha256(payload).hexdigest()
        entry = OFFICIAL_MODEL_CATALOG["official-coco-yolo11n"]

        def fetch(_: str, target: BinaryIO) -> None:
            target.write(payload)

        real_replace = os.replace

        def replace_after_drift(source: str | Path, destination: str | Path) -> None:
            Path(source).write_bytes(b"attacker replacement")
            real_replace(source, destination)

        with (
            patch.dict(
                OFFICIAL_MODEL_CATALOG,
                {
                    "official-coco-yolo11n": replace(
                        entry,
                        size_bytes=len(payload),
                        sha256=digest,
                    )
                },
            ),
            patch("scripts.acquire_detector_models.os.replace", side_effect=replace_after_drift),
            self.assertRaisesRegex(AcquisitionError, "size|digest|changed"),
        ):
            acquire_official_model(
                "official-coco-yolo11n",
                weights_root=self.weights_root,
                fetch=fetch,
            )

        self.assertFalse((self.weights_root / entry.filename).exists())
        self.assertEqual([], list(self.weights_root.iterdir()))

    def test_acquisition_creates_the_fixed_weights_directory_on_a_clean_checkout(self) -> None:
        payload = b"fixture weight bytes"
        digest = hashlib.sha256(payload).hexdigest()
        entry = OFFICIAL_MODEL_CATALOG["official-coco-yolo11n"]
        weights_root = self.weights_root / "weights"

        def fetch(_: str, target: BinaryIO) -> None:
            target.write(payload)

        with patch.dict(
            OFFICIAL_MODEL_CATALOG,
            {
                "official-coco-yolo11n": replace(
                    entry,
                    size_bytes=len(payload),
                    sha256=digest,
                )
            },
        ):
            acquired = acquire_official_model(
                "official-coco-yolo11n",
                weights_root=weights_root,
                fetch=fetch,
            )

        self.assertEqual(weights_root / entry.filename, acquired.path)
        self.assertEqual(payload, acquired.path.read_bytes())

    def test_acquisition_fails_closed_if_the_weights_root_identity_drifts(self) -> None:
        payload = b"fixture weight bytes"
        digest = hashlib.sha256(payload).hexdigest()
        entry = OFFICIAL_MODEL_CATALOG["official-coco-yolo11n"]
        checks = 0

        def fetch(_: str, target: BinaryIO) -> None:
            target.write(payload)

        def reject_after_download(_path: Path, _expected: tuple[int, int]) -> None:
            nonlocal checks
            checks += 1
            if checks >= 2:
                raise AcquisitionError("weights root identity changed during acquisition")

        with (
            patch.dict(
                OFFICIAL_MODEL_CATALOG,
                {
                    "official-coco-yolo11n": replace(
                        entry,
                        size_bytes=len(payload),
                        sha256=digest,
                    )
                },
            ),
            patch(
                "scripts.acquire_detector_models._assert_root_identity",
                side_effect=reject_after_download,
            ),
            self.assertRaisesRegex(AcquisitionError, "root identity changed"),
        ):
            acquire_official_model(
                "official-coco-yolo11n",
                weights_root=self.weights_root,
                fetch=fetch,
            )

        self.assertFalse((self.weights_root / entry.filename).exists())
        self.assertEqual([], list(self.weights_root.glob("*.partial")))


if __name__ == "__main__":
    unittest.main()
