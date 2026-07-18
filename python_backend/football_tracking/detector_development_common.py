from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,119}$")
WINDOWS_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class DetectorDevelopmentError(RuntimeError):
    """Stable fail-closed error for detector-development operations."""

    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class CorruptProbeFrameError(RuntimeError):
    """A requested exact frame could not produce trustworthy visual evidence."""


class ProbeWorkerDiedError(RuntimeError):
    """The bounded probe worker terminated without a valid terminal report."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (UnicodeEncodeError, ValueError) as exc:
        raise DetectorDevelopmentError(
            "invalid_unicode",
            "Detector development data contains invalid Unicode or non-finite values",
            status_code=400,
        ) from exc


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise DetectorDevelopmentError("invalid_digest", f"{label} must be a lowercase SHA-256", status_code=400)
    return value


def require_safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or SAFE_ID_RE.fullmatch(value) is None:
        raise DetectorDevelopmentError("invalid_identifier", f"{label} is invalid", status_code=400)
    return value


def is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & WINDOWS_REPARSE_ATTRIBUTE
    )


def require_trusted_relative_path(
    root: Path,
    value: Any,
    label: str,
    *,
    must_exist: bool = True,
    allowed_first_parts: set[str] | None = None,
) -> Path:
    if not isinstance(value, str) or not value or value != value.strip():
        raise DetectorDevelopmentError("invalid_path", f"{label} must be a trusted relative path", status_code=400)
    if unicodedata.normalize("NFC", value) != value or any(ord(character) < 32 for character in value):
        raise DetectorDevelopmentError("invalid_path", f"{label} contains unsafe Unicode or control characters", status_code=400)
    raw = value.replace("\\", "/")
    raw_parts = raw.split("/")
    if any(
        part in {"", ".", ".."}
        or ":" in part
        or any(character in '<>"|?*' for character in part)
        or part.endswith((" ", "."))
        or part.rstrip(" .").split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES
        for part in raw_parts
    ):
        raise DetectorDevelopmentError("path_outside_trusted_root", f"{label} contains an unsafe path segment")
    relative = PurePosixPath(raw)
    if (
        relative.is_absolute()
        or raw.startswith("//")
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise DetectorDevelopmentError("path_outside_trusted_root", f"{label} must remain relative to its trusted root")
    if allowed_first_parts is not None and relative.parts[0] not in allowed_first_parts:
        raise DetectorDevelopmentError("path_outside_trusted_root", f"{label} is outside its allowed trusted root")

    lexical_root = _secure_resolved_root(root, label)
    candidate = lexical_root.joinpath(*relative.parts)
    try:
        if os.path.commonpath((str(lexical_root), str(candidate))) != str(lexical_root):
            raise DetectorDevelopmentError("path_outside_trusted_root", f"{label} escapes its trusted root")
    except ValueError as exc:
        raise DetectorDevelopmentError("path_outside_trusted_root", f"{label} escapes its trusted root") from exc

    current = lexical_root
    for part in relative.parts:
        current = current / part
        if not current.exists() and not current.is_symlink():
            if must_exist:
                raise DetectorDevelopmentError("path_unavailable", f"{label} does not exist: {relative.as_posix()}", status_code=404)
            break
        if is_link_or_reparse(current):
            raise DetectorDevelopmentError("unsafe_path", f"{label} must not traverse a link or reparse point")
    return candidate


def regular_file_identity(path: Path, label: str) -> tuple[int, int, int, int, int]:
    if is_link_or_reparse(path):
        raise DetectorDevelopmentError("unsafe_path", f"{label} must not be a link or reparse point")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise DetectorDevelopmentError("path_unavailable", f"{label} is unavailable", status_code=404) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise DetectorDevelopmentError("not_regular_file", f"{label} must be a regular file")
    return stat_token(metadata)


def regular_file_change_identity(
    path: Path, label: str
) -> tuple[int, int, int, int, int, int]:
    """Include the filesystem change clock used by tamper-sensitive caches."""

    identity = regular_file_identity(path, label)
    change_time = (
        _windows_file_change_time(path, identity, label)
        if os.name == "nt"
        else identity[4]
    )
    return (*identity, change_time)


def _windows_file_change_time(
    path: Path,
    expected: tuple[int, int, int, int, int],
    label: str,
) -> int:
    """Return NTFS ChangeTime, which remains useful when mtime is restored."""

    import ctypes
    import msvcrt

    class FileBasicInfo(ctypes.Structure):
        _fields_ = [
            ("creation_time", ctypes.c_longlong),
            ("last_access_time", ctypes.c_longlong),
            ("last_write_time", ctypes.c_longlong),
            ("change_time", ctypes.c_longlong),
            ("file_attributes", ctypes.c_ulong),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_file_information = kernel32.GetFileInformationByHandleEx
    get_file_information.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_ulong,
    )
    get_file_information.restype = ctypes.c_int
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            opened_token = stat_token(opened)
            if opened_token[:4] != expected[:4]:
                raise DetectorDevelopmentError(
                    "source_changed", f"{label} changed during identity validation"
                )
            information = FileBasicInfo()
            if not get_file_information(
                ctypes.c_void_p(msvcrt.get_osfhandle(handle.fileno())),
                0,
                ctypes.byref(information),
                ctypes.sizeof(information),
            ):
                raise OSError(ctypes.get_last_error(), "GetFileInformationByHandleEx failed")
            final = path.stat()
            if stat_token(final)[:4] != expected[:4]:
                raise DetectorDevelopmentError(
                    "source_changed", f"{label} changed during identity validation"
                )
            return int(information.change_time)
    except DetectorDevelopmentError:
        raise
    except OSError as exc:
        raise DetectorDevelopmentError(
            "path_unavailable", f"{label} is unavailable", status_code=404
        ) from exc


def stat_token(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def snapshot_identity_is_current(path: Path, expected: tuple[int, int, int, int, int]) -> bool:
    try:
        return not is_link_or_reparse(path) and stat_token(path.stat()) == expected
    except OSError:
        return False


def hash_regular_file(
    path: Path,
    label: str,
    *,
    max_bytes: int | None = None,
    trusted_root: Path | None = None,
) -> tuple[str, int]:
    ancestors = _capture_ancestor_identities(path, trusted_root, label)
    expected = regular_file_identity(path, label)
    if max_bytes is not None and expected[2] > max_bytes:
        raise DetectorDevelopmentError("resource_limit_exceeded", f"{label} exceeds its byte limit")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or stat_token(opened) != expected:
                raise DetectorDevelopmentError("source_changed", f"{label} changed while it was opened")
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            if stat_token(os.fstat(handle.fileno())) != expected:
                raise DetectorDevelopmentError("source_changed", f"{label} changed while it was read")
    except DetectorDevelopmentError:
        raise
    except OSError as exc:
        raise DetectorDevelopmentError("path_unavailable", f"{label} could not be read") from exc
    if not snapshot_identity_is_current(path, expected) or not _ancestor_identities_are_current(ancestors):
        raise DetectorDevelopmentError("source_changed", f"{label} changed while it was read")
    return digest.hexdigest(), expected[2]


def read_regular_bytes(
    path: Path,
    label: str,
    *,
    max_bytes: int,
    trusted_root: Path | None = None,
) -> tuple[bytes, str]:
    ancestors = _capture_ancestor_identities(path, trusted_root, label)
    expected = regular_file_identity(path, label)
    if expected[2] > max_bytes:
        raise DetectorDevelopmentError("resource_limit_exceeded", f"{label} exceeds its byte limit")
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or stat_token(opened) != expected:
                raise DetectorDevelopmentError("source_changed", f"{label} changed while it was opened")
            content = handle.read(max_bytes + 1)
            after = os.fstat(handle.fileno())
    except DetectorDevelopmentError:
        raise
    except OSError as exc:
        raise DetectorDevelopmentError("path_unavailable", f"{label} could not be read") from exc
    if len(content) > max_bytes:
        raise DetectorDevelopmentError("resource_limit_exceeded", f"{label} exceeds its byte limit")
    if (
        stat_token(after) != expected
        or not snapshot_identity_is_current(path, expected)
        or not _ancestor_identities_are_current(ancestors)
    ):
        raise DetectorDevelopmentError("source_changed", f"{label} changed while it was read")
    return content, hashlib.sha256(content).hexdigest()


def atomic_write_json(path: Path, value: Any, *, trusted_root: Path | None = None) -> None:
    root = _secure_resolved_root(trusted_root or path.parent, "atomic JSON")
    destination = Path(os.path.abspath(path))
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise DetectorDevelopmentError("path_outside_trusted_root", "atomic JSON destination escapes its root") from exc
    if not destination.parent.is_dir():
        raise DetectorDevelopmentError("path_unavailable", "atomic JSON parent must already exist")
    ancestors = _capture_directory_object_identities(root, destination.parent, "atomic JSON")
    if is_link_or_reparse(destination) or is_link_or_reparse(destination.parent):
        raise DetectorDevelopmentError("unsafe_path", "atomic JSON destination must not be a link or reparse point")
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.{id(value)}.tmp")
    content = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n"
    expected_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if not _directory_object_identities_are_current(ancestors):
            raise DetectorDevelopmentError("source_changed", "atomic JSON ancestor changed before publication")
        os.replace(temporary, destination)
        if not _directory_object_identities_are_current(ancestors):
            raise DetectorDevelopmentError("source_changed", "atomic JSON ancestor changed during publication")
        actual_sha256, _ = hash_regular_file(
            destination,
            "published atomic JSON",
            max_bytes=max(1, len(content.encode("utf-8"))),
            trusted_root=root,
        )
        if actual_sha256 != expected_sha256:
            raise DetectorDevelopmentError("source_changed", "atomic JSON content changed during publication")
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def secure_mkdirs(root: Path, *parts: str) -> Path:
    """Create a fixed/sanitized descendant without traversing links or reparse points."""

    current = _secure_resolved_root(root, "directory creation")
    for part in parts:
        if (
            not part
            or part in {".", ".."}
            or ":" in part
            or part.endswith((" ", "."))
            or any(character in '<>"|?*/\\' for character in part)
            or part.rstrip(" .").split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES
        ):
            raise DetectorDevelopmentError("invalid_path", "Directory segment is unsafe")
        candidate = current / part
        try:
            candidate.mkdir()
        except FileExistsError:
            pass
        except OSError as exc:
            raise DetectorDevelopmentError("path_unavailable", "Trusted directory could not be created") from exc
        if is_link_or_reparse(candidate):
            raise DetectorDevelopmentError("unsafe_path", "Trusted directory must not be a link or reparse point")
        try:
            metadata = candidate.stat()
        except OSError as exc:
            raise DetectorDevelopmentError("path_unavailable", "Trusted directory is unavailable") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise DetectorDevelopmentError("unsafe_path", "Trusted directory path is not a directory")
        current = candidate
    return current


def _capture_ancestor_identities(
    path: Path,
    trusted_root: Path | None,
    label: str,
) -> tuple[tuple[Path, tuple[int, int, int, int, int]], ...]:
    if trusted_root is None:
        root = _secure_resolved_root(path.parent, label)
    else:
        root = _secure_resolved_root(trusted_root, label)
    candidate = Path(os.path.abspath(path))
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise DetectorDevelopmentError("path_outside_trusted_root", f"{label} escapes its trusted root") from exc
    identities: list[tuple[Path, tuple[int, int, int, int, int]]] = []
    current = root
    relative_parent = candidate.parent.relative_to(root)
    for part in (Path(), *relative_parent.parts):
        if part != Path():
            current = current / part
        if is_link_or_reparse(current):
            raise DetectorDevelopmentError("unsafe_path", f"{label} traverses a link or reparse point")
        try:
            metadata = current.stat()
        except OSError as exc:
            raise DetectorDevelopmentError("path_unavailable", f"{label} ancestor is unavailable") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise DetectorDevelopmentError("unsafe_path", f"{label} ancestor must be a directory")
        identities.append((current, stat_token(metadata)))
    return tuple(identities)


def _ancestor_identities_are_current(
    identities: tuple[tuple[Path, tuple[int, int, int, int, int]], ...]
) -> bool:
    for path, expected in identities:
        try:
            if is_link_or_reparse(path) or stat_token(path.stat()) != expected:
                return False
        except OSError:
            return False
    return True


def _secure_resolved_root(root: Path, label: str) -> Path:
    raw = Path(os.path.abspath(root))
    anchor = Path(raw.anchor)
    current = anchor
    for part in raw.parts[1:]:
        current = current / part
        if not current.exists() and not current.is_symlink():
            raise DetectorDevelopmentError("unsafe_trusted_root", f"{label} trusted root is unavailable")
        if is_link_or_reparse(current):
            raise DetectorDevelopmentError(
                "unsafe_trusted_root",
                f"{label} trusted root must not traverse a link or reparse point",
            )
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise DetectorDevelopmentError("unsafe_trusted_root", f"{label} trusted root is unavailable") from exc
    if resolved != raw:
        raise DetectorDevelopmentError("unsafe_trusted_root", f"{label} trusted root identity is ambiguous")
    return resolved


def _capture_directory_object_identities(
    root: Path,
    parent: Path,
    label: str,
) -> tuple[tuple[Path, tuple[int, int]], ...]:
    identities: list[tuple[Path, tuple[int, int]]] = []
    current = root
    for part in (Path(), *parent.relative_to(root).parts):
        if part != Path():
            current = current / part
        if is_link_or_reparse(current):
            raise DetectorDevelopmentError("unsafe_path", f"{label} traverses a link or reparse point")
        metadata = current.stat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise DetectorDevelopmentError("unsafe_path", f"{label} ancestor must be a directory")
        identities.append((current, (int(metadata.st_dev), int(metadata.st_ino))))
    return tuple(identities)


def _directory_object_identities_are_current(
    identities: tuple[tuple[Path, tuple[int, int]], ...]
) -> bool:
    for path, expected in identities:
        try:
            metadata = path.stat()
        except OSError:
            return False
        if (
            is_link_or_reparse(path)
            or not stat.S_ISDIR(metadata.st_mode)
            or (int(metadata.st_dev), int(metadata.st_ino)) != expected
        ):
            return False
    return True


def finite_number(value: Any, label: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise DetectorDevelopmentError("invalid_candidate", f"{label} must be finite", status_code=400)
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DetectorDevelopmentError("invalid_candidate", f"{label} must be finite", status_code=400) from exc
    if not math.isfinite(parsed) or (minimum is not None and parsed < minimum):
        raise DetectorDevelopmentError("invalid_candidate", f"{label} must be finite", status_code=400)
    return parsed


def json_object_from_bytes(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DetectorDevelopmentError("invalid_json", f"{label} must be valid UTF-8 JSON", status_code=400) from exc
    if not isinstance(value, dict):
        raise DetectorDevelopmentError("invalid_json", f"{label} must be a JSON object", status_code=400)
    return value
