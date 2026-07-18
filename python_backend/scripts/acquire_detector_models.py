from __future__ import annotations

import argparse
import hashlib
import os
import stat
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
WEIGHTS_ROOT = REPO_ROOT / "python_backend" / "weights"


class AcquisitionError(RuntimeError):
    """A pinned model could not be acquired without weakening its identity."""


@dataclass(frozen=True)
class OfficialModelCatalogEntry:
    model_id: str
    filename: str
    size_bytes: int
    sha256: str
    url: str


@dataclass(frozen=True)
class AcquiredModel:
    model_id: str
    path: Path
    size_bytes: int
    sha256: str


class _BoundedDownloadTarget:
    def __init__(self, target: BinaryIO, expected_size: int) -> None:
        self._target = target
        self._expected_size = expected_size
        self._written = 0

    def write(self, payload: bytes) -> int:
        if self._written + len(payload) > self._expected_size:
            raise AcquisitionError(
                f"model download exceeded pinned size {self._expected_size} bytes"
            )
        written = self._target.write(payload)
        self._written += written
        return written

    @property
    def remaining(self) -> int:
        return self._expected_size - self._written

    def __getattr__(self, name: str):
        return getattr(self._target, name)


OFFICIAL_MODEL_CATALOG = {
    "official-coco-yolo11n": OfficialModelCatalogEntry(
        model_id="official-coco-yolo11n",
        filename="yolo11n.pt",
        size_bytes=5_613_764,
        sha256="0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1",
        url="https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n.pt",
    ),
    "official-coco-yolo11s": OfficialModelCatalogEntry(
        model_id="official-coco-yolo11s",
        filename="yolo11s.pt",
        size_bytes=19_313_732,
        sha256="85a76fe86dd8afe384648546b56a7a78580c7cb7b404fc595f97969322d502d5",
        url="https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11s.pt",
    ),
}


def _assert_no_link_ancestors(path: Path) -> None:
    current = path
    while True:
        result = current.stat(follow_symlinks=False)
        if current.is_symlink() or _is_reparse(result):
            raise AcquisitionError("weights root must not traverse a symlink or reparse point")
        if current.parent == current:
            return
        current = current.parent


def _is_reparse(stat_result: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(stat_result, "st_file_attributes", 0) & reparse_flag)


def _assert_regular_file(path: Path, *, label: str) -> os.stat_result:
    if path.is_symlink():
        raise AcquisitionError(f"{label} must not be a symlink or reparse point")
    try:
        stat_result = path.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise AcquisitionError(f"{label} is missing") from exc
    if _is_reparse(stat_result):
        raise AcquisitionError(f"{label} must not be a symlink or reparse point")
    if not stat.S_ISREG(stat_result.st_mode):
        raise AcquisitionError(f"{label} must be a regular file")
    return stat_result


def _validate(path: Path, entry: OfficialModelCatalogEntry) -> AcquiredModel:
    expected_identity = _path_identity(path, label="acquired model")
    open_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, open_flags)
    with os.fdopen(descriptor, "rb") as handle:
        size_bytes, digest, open_identity = _validate_open(handle, entry)
    if open_identity != expected_identity or _path_identity(path, label="acquired model") != open_identity:
        raise AcquisitionError("acquired model file identity changed during validation")
    return AcquiredModel(entry.model_id, path, size_bytes, digest)


def _download(url: str, target: BinaryIO) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "football-tracking-model-acquisition/1"})
    with urllib.request.urlopen(request, timeout=120) as response:
        while chunk := response.read(
            min(1024 * 1024, int(getattr(target, "remaining", 1024 * 1024)) + 1)
        ):
            target.write(chunk)


def _open_identity(handle: BinaryIO) -> tuple[int, int]:
    stat_result = os.fstat(handle.fileno())
    if _is_reparse(stat_result) or not stat.S_ISREG(stat_result.st_mode):
        raise AcquisitionError("temporary model must remain a regular non-reparse file")
    return stat_result.st_dev, stat_result.st_ino


def _open_stat_token(handle: BinaryIO) -> tuple[int, int, int, int, int]:
    stat_result = os.fstat(handle.fileno())
    if _is_reparse(stat_result) or not stat.S_ISREG(stat_result.st_mode):
        raise AcquisitionError("temporary model must remain a regular non-reparse file")
    return (
        int(stat_result.st_dev),
        int(stat_result.st_ino),
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
        int(stat_result.st_ctime_ns),
    )


def _validate_open(
    handle: BinaryIO,
    entry: OfficialModelCatalogEntry,
) -> tuple[int, str, tuple[int, int]]:
    if handle.writable():
        handle.flush()
        os.fsync(handle.fileno())
    initial_token = _open_stat_token(handle)
    identity = initial_token[:2]
    size_bytes = initial_token[2]
    if size_bytes != entry.size_bytes:
        raise AcquisitionError(
            f"model size mismatch: expected {entry.size_bytes}, observed {size_bytes}"
        )
    digest = hashlib.sha256()
    handle.seek(0)
    remaining = entry.size_bytes
    while remaining:
        chunk = handle.read(min(1024 * 1024, remaining))
        if not chunk:
            raise AcquisitionError("model ended before its pinned size")
        digest.update(chunk)
        remaining -= len(chunk)
    if handle.read(1):
        raise AcquisitionError("model grew beyond its pinned size during validation")
    observed = digest.hexdigest()
    if observed != entry.sha256:
        raise AcquisitionError(f"model digest mismatch: expected {entry.sha256}, observed {observed}")
    if _open_stat_token(handle) != initial_token:
        raise AcquisitionError("temporary model file identity changed during validation")
    return size_bytes, observed, identity


def _path_identity(path: Path, *, label: str) -> tuple[int, int]:
    stat_result = _assert_regular_file(path, label=label)
    return stat_result.st_dev, stat_result.st_ino


def _directory_identity(path: Path, *, label: str) -> tuple[int, int]:
    if path.is_symlink():
        raise AcquisitionError(f"{label} must not be a symlink or reparse point")
    try:
        stat_result = path.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise AcquisitionError(f"{label} is missing") from exc
    if _is_reparse(stat_result) or not stat.S_ISDIR(stat_result.st_mode):
        raise AcquisitionError(f"{label} must be a non-link directory")
    return stat_result.st_dev, stat_result.st_ino


def _assert_root_identity(path: Path, expected: tuple[int, int]) -> None:
    if _directory_identity(path, label="weights root") != expected:
        raise AcquisitionError("weights root identity changed during acquisition")


def acquire_official_model(
    model_id: str,
    *,
    weights_root: Path = WEIGHTS_ROOT,
    fetch: Callable[[str, BinaryIO], None] = _download,
) -> AcquiredModel:
    """Acquire one fixed catalog item; no caller-provided URL is accepted."""

    entry = OFFICIAL_MODEL_CATALOG.get(model_id)
    if entry is None:
        raise AcquisitionError(f"model ID is not in the pinned official catalog: {model_id!r}")

    raw_root = weights_root.absolute()
    _assert_no_link_ancestors(raw_root.parent)
    if not raw_root.exists() and not raw_root.is_symlink():
        try:
            raw_root.mkdir(mode=0o700)
        except FileExistsError:
            pass
    root = raw_root.resolve(strict=True)
    _assert_no_link_ancestors(raw_root)
    root_stat = weights_root.stat(follow_symlinks=False)
    if weights_root.is_symlink() or _is_reparse(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        raise AcquisitionError("weights root must be an existing non-link directory")
    root_identity = _directory_identity(raw_root, label="weights root")
    destination = root / entry.filename
    if destination.parent != root:
        raise AcquisitionError("catalog destination escapes the fixed weights root")
    if destination.exists():
        existing = _validate(destination, entry)
        _assert_root_identity(raw_root, root_identity)
        return existing

    lock_path = root / f".{entry.filename}.acquire.lock"
    temporary_path: Path | None = None
    lock_fd: int | None = None
    lock_identity: tuple[int, int] | None = None
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        lock_stat = os.fstat(lock_fd)
        if _is_reparse(lock_stat) or not stat.S_ISREG(lock_stat.st_mode):
            raise AcquisitionError("acquisition lock is not a regular file")
        lock_identity = (lock_stat.st_dev, lock_stat.st_ino)
        if destination.exists() or destination.is_symlink():
            existing = _validate(destination, entry)
            _assert_root_identity(raw_root, root_identity)
            return existing

        _assert_root_identity(raw_root, root_identity)
        temporary_fd, temporary_name = tempfile.mkstemp(
            prefix=f".{entry.filename}.", suffix=".partial", dir=root
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(temporary_fd, "w+b") as temporary:
            fetch(entry.url, _BoundedDownloadTarget(temporary, entry.size_bytes))
            size_bytes, digest, identity = _validate_open(temporary, entry)
            if _path_identity(temporary_path, label="temporary model") != identity:
                raise AcquisitionError("temporary model path changed before publication")

        _assert_root_identity(raw_root, root_identity)
        if _path_identity(temporary_path, label="temporary model") != identity:
            raise AcquisitionError("temporary model path changed before publication")
        _validate(temporary_path, entry)
        _assert_root_identity(raw_root, root_identity)
        os.replace(temporary_path, destination)
        temporary_path = None
        _assert_root_identity(raw_root, root_identity)
        try:
            published = _validate(destination, entry)
        except AcquisitionError:
            if destination.exists() and not destination.is_symlink():
                destination.unlink()
            raise
        if published.size_bytes != size_bytes or published.sha256 != digest:
            if destination.exists() and not destination.is_symlink():
                destination.unlink()
            raise AcquisitionError("published model changed after validation")
        return published
    except Exception as exc:
        try:
            if temporary_path is not None and temporary_path.exists() and not temporary_path.is_symlink():
                temporary_path.unlink()
        except OSError:
            pass
        if isinstance(exc, AcquisitionError):
            raise
        raise AcquisitionError(f"model acquisition failed: {exc}") from exc
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
            try:
                if lock_identity is not None and _path_identity(
                    lock_path, label="acquisition lock"
                ) == lock_identity:
                    lock_path.unlink()
            except FileNotFoundError:
                pass


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Acquire exact official detector weights from the fixed catalog.")
    parser.add_argument(
        "model_ids",
        nargs="+",
        choices=tuple(OFFICIAL_MODEL_CATALOG),
        help="One or more fixed catalog model IDs; arbitrary URLs and paths are not accepted.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    for model_id in args.model_ids:
        acquired = acquire_official_model(model_id)
        print(f"{acquired.model_id}\t{acquired.size_bytes}\t{acquired.sha256}\t{acquired.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
