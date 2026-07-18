from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO

import pytest

from scripts.acquire_detector_models import (
    OFFICIAL_MODEL_CATALOG,
    AcquisitionError,
    acquire_official_model,
)


def test_official_catalog_is_pinned_to_v840_assets() -> None:
    assert {
        model_id: (entry.filename, entry.size_bytes, entry.sha256, entry.url)
        for model_id, entry in OFFICIAL_MODEL_CATALOG.items()
    } == {
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
    }
    assert all("latest" not in entry.url for entry in OFFICIAL_MODEL_CATALOG.values())


def test_acquisition_accepts_catalog_ids_only(tmp_path: Path) -> None:
    called = False

    def fetch(_: str, __: BinaryIO) -> None:
        nonlocal called
        called = True

    with pytest.raises(AcquisitionError, match="catalog"):
        acquire_official_model(
            "https://attacker.invalid/arbitrary.pt",
            weights_root=tmp_path,
            fetch=fetch,
        )

    assert called is False
    assert list(tmp_path.iterdir()) == []


def test_acquisition_fails_closed_on_size_or_digest_mismatch(tmp_path: Path) -> None:
    def fetch(_: str, target: BinaryIO) -> None:
        target.write(b"not the pinned model")

    with pytest.raises(AcquisitionError, match="size|digest"):
        acquire_official_model(
            "official-coco-yolo11n",
            weights_root=tmp_path,
            fetch=fetch,
        )

    assert not (tmp_path / "yolo11n.pt").exists()
    assert list(tmp_path.iterdir()) == []


def test_acquisition_aborts_on_the_first_byte_beyond_the_pinned_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = OFFICIAL_MODEL_CATALOG["official-coco-yolo11n"]
    monkeypatch.setitem(
        OFFICIAL_MODEL_CATALOG,
        "official-coco-yolo11n",
        replace(entry, size_bytes=4, sha256=hashlib.sha256(b"four").hexdigest()),
    )
    observed_position = None

    def fetch(_: str, target: BinaryIO) -> None:
        nonlocal observed_position
        target.write(b"four")
        try:
            target.write(b"x")
        finally:
            observed_position = target.tell()

    with pytest.raises(AcquisitionError, match="exceeded pinned size"):
        acquire_official_model(
            "official-coco-yolo11n",
            weights_root=tmp_path,
            fetch=fetch,
        )

    assert observed_position == 4
    assert list(tmp_path.iterdir()) == []


def test_acquisition_reuses_only_an_exact_existing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"fixture weight bytes"
    digest = hashlib.sha256(payload).hexdigest()
    entry = OFFICIAL_MODEL_CATALOG["official-coco-yolo11n"]
    monkeypatch.setitem(
        OFFICIAL_MODEL_CATALOG,
        "official-coco-yolo11n",
        replace(entry, size_bytes=len(payload), sha256=digest),
    )
    destination = tmp_path / entry.filename
    destination.write_bytes(payload)

    def unexpected_fetch(_: str, __: BinaryIO) -> None:
        raise AssertionError("an exact local file must not be downloaded again")

    result = acquire_official_model(
        "official-coco-yolo11n",
        weights_root=tmp_path,
        fetch=unexpected_fetch,
    )

    assert result.path == destination
    assert result.sha256 == digest
    assert result.size_bytes == len(payload)


def test_acquisition_detects_hash_to_publish_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"fixture weight bytes"
    digest = hashlib.sha256(payload).hexdigest()
    entry = OFFICIAL_MODEL_CATALOG["official-coco-yolo11n"]
    monkeypatch.setitem(
        OFFICIAL_MODEL_CATALOG,
        "official-coco-yolo11n",
        replace(entry, size_bytes=len(payload), sha256=digest),
    )

    def fetch(_: str, target: BinaryIO) -> None:
        target.write(payload)

    real_replace = os.replace

    def replace_after_drift(source: str | Path, destination: str | Path) -> None:
        Path(source).write_bytes(b"attacker replacement")
        real_replace(source, destination)

    monkeypatch.setattr("scripts.acquire_detector_models.os.replace", replace_after_drift)

    with pytest.raises(AcquisitionError, match="size|digest|changed"):
        acquire_official_model(
            "official-coco-yolo11n",
            weights_root=tmp_path,
            fetch=fetch,
        )

    assert not (tmp_path / entry.filename).exists()
    assert list(tmp_path.iterdir()) == []


def test_acquisition_creates_the_fixed_weights_directory_on_a_clean_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"fixture weight bytes"
    digest = hashlib.sha256(payload).hexdigest()
    entry = OFFICIAL_MODEL_CATALOG["official-coco-yolo11n"]
    monkeypatch.setitem(
        OFFICIAL_MODEL_CATALOG,
        "official-coco-yolo11n",
        replace(entry, size_bytes=len(payload), sha256=digest),
    )
    weights_root = tmp_path / "weights"

    def fetch(_: str, target: BinaryIO) -> None:
        target.write(payload)

    acquired = acquire_official_model(
        "official-coco-yolo11n",
        weights_root=weights_root,
        fetch=fetch,
    )

    assert acquired.path == weights_root / entry.filename
    assert acquired.path.read_bytes() == payload


def test_acquisition_fails_closed_if_the_weights_root_identity_drifts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"fixture weight bytes"
    digest = hashlib.sha256(payload).hexdigest()
    entry = OFFICIAL_MODEL_CATALOG["official-coco-yolo11n"]
    monkeypatch.setitem(
        OFFICIAL_MODEL_CATALOG,
        "official-coco-yolo11n",
        replace(entry, size_bytes=len(payload), sha256=digest),
    )
    checks = 0

    def fetch(_: str, target: BinaryIO) -> None:
        target.write(payload)

    def reject_after_download(_path: Path, _expected: tuple[int, int]) -> None:
        nonlocal checks
        checks += 1
        if checks >= 2:
            raise AcquisitionError("weights root identity changed during acquisition")

    monkeypatch.setattr(
        "scripts.acquire_detector_models._assert_root_identity",
        reject_after_download,
    )

    with pytest.raises(AcquisitionError, match="root identity changed"):
        acquire_official_model(
            "official-coco-yolo11n",
            weights_root=tmp_path,
            fetch=fetch,
        )

    assert not (tmp_path / entry.filename).exists()
    assert not list(tmp_path.glob("*.partial"))
