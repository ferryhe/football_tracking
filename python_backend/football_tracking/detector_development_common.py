from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import time
import unicodedata
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,119}$")
WINDOWS_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_WINDOWS_ATOMIC_REPLACE_RETRY_SECONDS = 0.75
_WINDOWS_ATOMIC_REPLACE_INITIAL_BACKOFF_SECONDS = 0.005
_WINDOWS_ATOMIC_REPLACE_MAX_BACKOFF_SECONDS = 0.02
_WINDOWS_ATOMIC_REPLACE_SHARING_ERRORS = {5, 32}
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
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


@lru_cache(maxsize=1)
def _windows_file_api() -> tuple[Any, ...]:
    """Create one process-wide set of Windows file API bindings and ctypes types."""

    if os.name != "nt":
        raise RuntimeError("Windows file APIs are unavailable on this platform")

    import ctypes
    from ctypes import wintypes

    class FileTime(ctypes.Structure):
        _fields_ = (("low", wintypes.DWORD), ("high", wintypes.DWORD))

    class FileBasicInfo(ctypes.Structure):
        _fields_ = [
            ("creation_time", ctypes.c_longlong),
            ("last_access_time", ctypes.c_longlong),
            ("last_write_time", ctypes.c_longlong),
            ("change_time", ctypes.c_longlong),
            ("file_attributes", wintypes.DWORD),
        ]

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = (
            ("file_attributes", wintypes.DWORD),
            ("creation_time", FileTime),
            ("last_access_time", FileTime),
            ("last_write_time", FileTime),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_handle_identity = kernel32.GetFileInformationByHandle
    get_handle_identity.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    )
    get_handle_identity.restype = wintypes.BOOL
    get_file_information = kernel32.GetFileInformationByHandleEx
    get_file_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    get_file_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    return (
        ctypes,
        wintypes,
        FileBasicInfo,
        ByHandleFileInformation,
        create_file,
        get_handle_identity,
        get_file_information,
        close_handle,
    )


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
        raise DetectorDevelopmentError(
            "invalid_path", f"{label} contains unsafe Unicode or control characters", status_code=400
        )
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
    if relative.is_absolute() or raw.startswith("//") or any(part in {"", ".", ".."} for part in relative.parts):
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
                raise DetectorDevelopmentError(
                    "path_unavailable", f"{label} does not exist: {relative.as_posix()}", status_code=404
                )
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


def regular_file_change_identity(path: Path, label: str) -> tuple[int, int, int, int, int, int]:
    """Include the filesystem change clock used by tamper-sensitive caches."""

    identity = regular_file_identity(path, label)
    return _change_identity(path, identity, label, directory=False)


def _change_identity(
    path: Path,
    identity: tuple[int, int, int, int, int],
    label: str,
    *,
    directory: bool,
) -> tuple[int, int, int, int, int, int]:
    change_time = (
        _windows_directory_change_time(path, identity, label)
        if os.name == "nt" and directory
        else (_windows_file_change_time(path, identity, label) if os.name == "nt" else identity[4])
    )
    return (*identity, change_time)


def _windows_file_change_time(
    path: Path,
    expected: tuple[int, int, int, int, int],
    label: str,
) -> int:
    """Return NTFS ChangeTime, which remains useful when mtime is restored."""

    (
        ctypes,
        wintypes,
        FileBasicInfo,
        ByHandleFileInformation,
        create_file,
        get_handle_identity,
        get_file_information,
        close_handle,
    ) = _windows_file_api()

    handle = create_file(
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001,  # FILE_SHARE_READ only: no writes or replacement while sampled.
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        error = ctypes.get_last_error()
        raise DetectorDevelopmentError(
            "path_unavailable", f"{label} is unavailable", status_code=404
        ) from ctypes.WinError(error)
    try:
        handle_identity = ByHandleFileInformation()
        if not get_handle_identity(handle, ctypes.byref(handle_identity)):
            error = ctypes.get_last_error()
            raise DetectorDevelopmentError(
                "path_unavailable", f"{label} is unavailable", status_code=404
            ) from ctypes.WinError(error)
        file_index = (int(handle_identity.file_index_high) << 32) | int(handle_identity.file_index_low)
        file_size = (int(handle_identity.file_size_high) << 32) | int(handle_identity.file_size_low)
        if (
            handle_identity.file_attributes & (0x00000400 | 0x00000010)
            or (
                int(handle_identity.volume_serial_number),
                file_index,
            )
            != expected[:2]
            or file_size != expected[2]
        ):
            raise DetectorDevelopmentError("source_changed", f"{label} changed during identity validation")
        try:
            final = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise DetectorDevelopmentError("path_unavailable", f"{label} is unavailable", status_code=404) from exc
        if stat_token(final) != expected:
            raise DetectorDevelopmentError("source_changed", f"{label} changed during identity validation")
        information = FileBasicInfo()
        if not get_file_information(
            handle,
            0,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = ctypes.get_last_error()
            raise DetectorDevelopmentError(
                "path_unavailable", f"{label} is unavailable", status_code=404
            ) from ctypes.WinError(error)
        return int(information.change_time)
    finally:
        close_handle(handle)


def _windows_directory_change_time(
    path: Path,
    expected: tuple[int, int, int, int, int],
    label: str,
) -> int:
    (
        ctypes,
        wintypes,
        FileBasicInfo,
        ByHandleFileInformation,
        create_file,
        get_handle_identity,
        get_file_information,
        close_handle,
    ) = _windows_file_api()

    def sample(*, verify_path: bool) -> int:
        handle = create_file(
            str(path),
            0x80000000,  # GENERIC_READ blocks rename/delete while sampled.
            0x00000001,  # FILE_SHARE_READ only.
            None,
            3,  # OPEN_EXISTING
            0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
            None,
        )
        if handle == wintypes.HANDLE(-1).value:
            error = ctypes.get_last_error()
            raise DetectorDevelopmentError(
                "path_unavailable", f"{label} is unavailable", status_code=404
            ) from ctypes.WinError(error)
        try:
            handle_identity = ByHandleFileInformation()
            if not get_handle_identity(handle, ctypes.byref(handle_identity)):
                error = ctypes.get_last_error()
                raise DetectorDevelopmentError(
                    "path_unavailable",
                    f"{label} is unavailable",
                    status_code=404,
                ) from ctypes.WinError(error)
            file_index = (int(handle_identity.file_index_high) << 32) | int(handle_identity.file_index_low)
            if (
                handle_identity.file_attributes & 0x00000400
                or not handle_identity.file_attributes & 0x00000010
                or (
                    int(handle_identity.volume_serial_number),
                    file_index,
                )
                != expected[:2]
            ):
                raise DetectorDevelopmentError(
                    "source_changed",
                    f"{label} changed during identity validation",
                )
            if verify_path:
                try:
                    current = path.stat(follow_symlinks=False)
                except OSError as exc:
                    raise DetectorDevelopmentError(
                        "path_unavailable",
                        f"{label} is unavailable",
                        status_code=404,
                    ) from exc
                if stat_token(current) != expected:
                    raise DetectorDevelopmentError(
                        "source_changed",
                        f"{label} changed during identity validation",
                    )
            information = FileBasicInfo()
            if not get_file_information(
                handle,
                0,
                ctypes.byref(information),
                ctypes.sizeof(information),
            ):
                error = ctypes.get_last_error()
                raise DetectorDevelopmentError(
                    "path_unavailable",
                    f"{label} is unavailable",
                    status_code=404,
                ) from ctypes.WinError(error)
            return int(information.change_time)
        finally:
            close_handle(handle)

    sample(verify_path=True)
    return sample(verify_path=False)


def _open_verified_regular_file(
    path: Path,
    expected: tuple[int, int, int, int, int, int],
    label: str,
) -> Any:
    if os.name != "nt":
        return path.open("rb")

    import msvcrt

    (
        ctypes,
        wintypes,
        FileBasicInfo,
        ByHandleFileInformation,
        create_file,
        get_handle_identity,
        get_file_information,
        close_handle,
    ) = _windows_file_api()

    handle = create_file(
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001,  # FILE_SHARE_READ only: no writes or replacement during read.
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        error = ctypes.get_last_error()
        raise DetectorDevelopmentError("path_unavailable", f"{label} could not be opened") from ctypes.WinError(error)
    try:
        handle_identity = ByHandleFileInformation()
        basic = FileBasicInfo()
        if not get_handle_identity(handle, ctypes.byref(handle_identity)) or not get_file_information(
            handle,
            0,
            ctypes.byref(basic),
            ctypes.sizeof(basic),
        ):
            error = ctypes.get_last_error()
            raise DetectorDevelopmentError("path_unavailable", f"{label} could not be opened") from ctypes.WinError(
                error
            )
        file_index = (int(handle_identity.file_index_high) << 32) | int(handle_identity.file_index_low)
        file_size = (int(handle_identity.file_size_high) << 32) | int(handle_identity.file_size_low)
        if (
            handle_identity.file_attributes & (0x00000400 | 0x00000010)
            or (
                int(handle_identity.volume_serial_number),
                file_index,
            )
            != expected[:2]
            or file_size != expected[2]
            or int(basic.change_time) != expected[5]
        ):
            raise DetectorDevelopmentError("source_changed", f"{label} changed while it was opened")
        descriptor = msvcrt.open_osfhandle(
            int(handle),
            os.O_RDONLY | os.O_BINARY,
        )
    except BaseException:
        close_handle(handle)
        raise
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or stat_token(opened) != expected[:5]:
            raise DetectorDevelopmentError("source_changed", f"{label} changed while it was opened")
        return os.fdopen(descriptor, "rb", closefd=True)
    except BaseException:
        os.close(descriptor)
        raise


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


def _open_windows_ancestor_guards(
    identities: tuple[tuple[Path, tuple[int, int]], ...],
    label: str,
) -> tuple[int, ...]:
    """Hold directories without delete sharing while a trusted read is active."""

    if os.name != "nt":
        return ()

    (
        ctypes,
        wintypes,
        _file_basic_info,
        ByHandleFileInformation,
        create_file,
        get_file_information,
        _get_file_information_ex,
        _close_handle,
    ) = _windows_file_api()

    handles: list[int] = []
    try:
        for path, expected in identities:
            handle = create_file(
                str(path),
                0x80000000,  # GENERIC_READ blocks rename/delete of this directory.
                0x00000001 | 0x00000002,  # Share child reads/writes, not directory deletion.
                None,
                3,  # OPEN_EXISTING
                0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
                None,
            )
            if handle == wintypes.HANDLE(-1).value:
                error = ctypes.get_last_error()
                raise DetectorDevelopmentError(
                    "path_unavailable",
                    f"{label} ancestor is unavailable",
                ) from ctypes.WinError(error)
            handles.append(int(handle))
            information = ByHandleFileInformation()
            if not get_file_information(handle, ctypes.byref(information)):
                error = ctypes.get_last_error()
                raise DetectorDevelopmentError(
                    "path_unavailable",
                    f"{label} ancestor is unavailable",
                ) from ctypes.WinError(error)
            file_index = (int(information.file_index_high) << 32) | int(information.file_index_low)
            observed = (int(information.volume_serial_number), file_index)
            try:
                current = path.lstat()
            except OSError as exc:
                raise DetectorDevelopmentError("path_unavailable", f"{label} ancestor is unavailable") from exc
            if (
                information.file_attributes & 0x00000400
                or not information.file_attributes & 0x00000010
                or is_link_or_reparse(path)
                or not stat.S_ISDIR(current.st_mode)
                or observed != expected
                or (int(current.st_dev), int(current.st_ino)) != expected
            ):
                raise DetectorDevelopmentError(
                    "source_changed",
                    f"{label} ancestor changed during guard acquisition",
                )
    except BaseException:
        _close_windows_handles(handles)
        raise
    return tuple(handles)


def _close_windows_handles(handles: list[int] | tuple[int, ...]) -> None:
    if os.name != "nt" or not handles:
        return
    _ctypes, wintypes, _basic, _identity, _create, _get, _get_ex, close_handle = _windows_file_api()
    for handle in reversed(handles):
        close_handle(wintypes.HANDLE(handle))


def hash_regular_file(
    path: Path,
    label: str,
    *,
    max_bytes: int | None = None,
    trusted_root: Path | None = None,
) -> tuple[str, int]:
    ancestors = _capture_ancestor_identities(path, trusted_root, label)
    ancestor_guards = _open_windows_ancestor_guards(ancestors, label)
    try:
        expected = regular_file_change_identity(path, label)
        if max_bytes is not None and expected[2] > max_bytes:
            raise DetectorDevelopmentError("resource_limit_exceeded", f"{label} exceeds its byte limit")
        digest = hashlib.sha256()
        try:
            with _open_verified_regular_file(path, expected, label) as handle:
                opened = os.fstat(handle.fileno())
                if not stat.S_ISREG(opened.st_mode) or stat_token(opened) != expected[:5]:
                    raise DetectorDevelopmentError("source_changed", f"{label} changed while it was opened")
                remaining = expected[2]
                while remaining:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise DetectorDevelopmentError("source_changed", f"{label} changed while it was read")
                    digest.update(chunk)
                    remaining -= len(chunk)
                if handle.read(1):
                    raise DetectorDevelopmentError("source_changed", f"{label} grew while it was read")
                if stat_token(os.fstat(handle.fileno())) != expected[:5]:
                    raise DetectorDevelopmentError("source_changed", f"{label} changed while it was read")
                try:
                    identity_is_current = (
                        snapshot_identity_is_current(path, expected[:5])
                        and regular_file_change_identity(path, label) == expected
                        and _ancestor_identities_are_current(ancestors)
                    )
                except OSError as exc:
                    raise DetectorDevelopmentError("source_changed", f"{label} changed while it was read") from exc
                if not identity_is_current:
                    raise DetectorDevelopmentError("source_changed", f"{label} changed while it was read")
                return digest.hexdigest(), expected[2]
        except DetectorDevelopmentError:
            raise
        except OSError as exc:
            raise DetectorDevelopmentError("path_unavailable", f"{label} could not be read") from exc
    finally:
        _close_windows_handles(ancestor_guards)


def read_regular_bytes(
    path: Path,
    label: str,
    *,
    max_bytes: int,
    trusted_root: Path | None = None,
) -> tuple[bytes, str]:
    ancestors = _capture_ancestor_identities(path, trusted_root, label)
    ancestor_guards = _open_windows_ancestor_guards(ancestors, label)
    try:
        expected = regular_file_change_identity(path, label)
        if expected[2] > max_bytes:
            raise DetectorDevelopmentError("resource_limit_exceeded", f"{label} exceeds its byte limit")
        try:
            with _open_verified_regular_file(path, expected, label) as handle:
                opened = os.fstat(handle.fileno())
                if not stat.S_ISREG(opened.st_mode) or stat_token(opened) != expected[:5]:
                    raise DetectorDevelopmentError("source_changed", f"{label} changed while it was opened")
                content = handle.read(max_bytes + 1)
                after = os.fstat(handle.fileno())
                if len(content) > max_bytes:
                    raise DetectorDevelopmentError("resource_limit_exceeded", f"{label} exceeds its byte limit")
                try:
                    identity_is_current = (
                        stat_token(after) == expected[:5]
                        and snapshot_identity_is_current(path, expected[:5])
                        and regular_file_change_identity(path, label) == expected
                        and _ancestor_identities_are_current(ancestors)
                    )
                except OSError as exc:
                    raise DetectorDevelopmentError("source_changed", f"{label} changed while it was read") from exc
                if not identity_is_current:
                    raise DetectorDevelopmentError("source_changed", f"{label} changed while it was read")
                return content, hashlib.sha256(content).hexdigest()
        except DetectorDevelopmentError:
            raise
        except OSError as exc:
            raise DetectorDevelopmentError("path_unavailable", f"{label} could not be read") from exc
    finally:
        _close_windows_handles(ancestor_guards)


TreeIdentitySnapshot = tuple[tuple[str, str, tuple[int, int, int, int, int, int]], ...]


def exact_regular_tree_snapshot(
    root: Path,
    expected_files: set[str],
    label: str,
    *,
    trusted_root: Path | None = None,
) -> TreeIdentitySnapshot:
    """Validate an exact no-follow regular-file tree and freeze identities.

    Empty or unexpected directories are rejected as strictly as unexpected
    files.  The returned snapshot can be compared with a second call after
    bounded reads to detect swaps anywhere in the tree.  On Windows, every
    expected file is held read-only through the final exact re-enumeration and
    directory sampling.  The result is a linearizable point-in-time snapshot;
    callers must still revalidate later uses because the filesystem may change
    after this function returns.
    """

    candidate = Path(os.path.abspath(root))
    ancestors = _capture_ancestor_identities(
        candidate,
        trusted_root or candidate.parent,
        label,
    )
    normalized_files: set[str] = set()
    for value in expected_files:
        if not isinstance(value, str) or not value or value != value.strip():
            raise DetectorDevelopmentError(
                "invalid_result_allowlist",
                f"{label} contains an invalid expected file",
            )
        relative = PurePosixPath(value)
        if (
            relative.is_absolute()
            or relative.as_posix() != value
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise DetectorDevelopmentError(
                "invalid_result_allowlist",
                f"{label} contains an unsafe expected file",
            )
        normalized_files.add(value)
    if normalized_files != expected_files:
        raise DetectorDevelopmentError(
            "invalid_result_allowlist",
            f"{label} contains duplicate or non-canonical expected files",
        )

    expected_directories: set[str] = set()
    for value in normalized_files:
        parent = PurePosixPath(value).parent
        while parent != PurePosixPath("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent

    captured: dict[str, tuple[str, tuple[int, int, int, int, int, int]]] = {}
    observed_files: set[str] = set()
    observed_directories: set[str] = set()

    def metadata(path: Path) -> os.stat_result:
        try:
            value = path.lstat()
        except OSError as exc:
            raise DetectorDevelopmentError("result_unavailable", f"{label} could not be enumerated") from exc
        if stat.S_ISLNK(value.st_mode) or bool(getattr(value, "st_file_attributes", 0) & WINDOWS_REPARSE_ATTRIBUTE):
            raise DetectorDevelopmentError(
                "unsafe_result_tree",
                f"{label} contains a link or reparse point",
            )
        return value

    root_metadata = metadata(candidate)
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise DetectorDevelopmentError("unsafe_result_tree", f"{label} root is not a regular directory")
    captured["."] = (
        "directory",
        _change_identity(
            candidate,
            stat_token(root_metadata),
            label,
            directory=True,
        ),
    )

    def walk(directory: Path) -> None:
        try:
            with os.scandir(directory) as entries:
                children = sorted(list(entries), key=lambda entry: entry.name)
        except OSError as exc:
            raise DetectorDevelopmentError("result_unavailable", f"{label} could not be enumerated") from exc
        for entry in children:
            path = Path(entry.path)
            relative = path.relative_to(candidate).as_posix()
            value = metadata(path)
            if stat.S_ISDIR(value.st_mode):
                if relative not in expected_directories:
                    raise DetectorDevelopmentError(
                        "unexpected_result_artifact",
                        f"{label} contains an unexpected directory",
                    )
                observed_directories.add(relative)
                captured[relative] = (
                    "directory",
                    _change_identity(
                        path,
                        stat_token(value),
                        label,
                        directory=True,
                    ),
                )
                walk(path)
            elif stat.S_ISREG(value.st_mode):
                if relative not in normalized_files:
                    raise DetectorDevelopmentError(
                        "unexpected_result_artifact",
                        f"{label} contains an unexpected file",
                    )
                observed_files.add(relative)
                captured[relative] = (
                    "file",
                    _change_identity(
                        path,
                        stat_token(value),
                        label,
                        directory=False,
                    ),
                )
            else:
                raise DetectorDevelopmentError(
                    "unsafe_result_tree",
                    f"{label} contains a special file",
                )

    walk(candidate)
    if observed_files != normalized_files or observed_directories != expected_directories:
        raise DetectorDevelopmentError(
            "invalid_result_allowlist",
            f"{label} does not match its exact artifact allowlist",
        )

    def verify_exact_entries(directory: Path) -> tuple[set[str], set[str]]:
        verified_files: set[str] = set()
        verified_directories: set[str] = set()

        def verify_directory(current_directory: Path) -> None:
            try:
                with os.scandir(current_directory) as entries:
                    children = sorted(list(entries), key=lambda entry: entry.name)
            except OSError as exc:
                raise DetectorDevelopmentError("result_unavailable", f"{label} could not be enumerated") from exc
            for entry in children:
                path = Path(entry.path)
                relative = path.relative_to(candidate).as_posix()
                value = metadata(path)
                if stat.S_ISDIR(value.st_mode):
                    if relative not in expected_directories:
                        raise DetectorDevelopmentError(
                            "unexpected_result_artifact",
                            f"{label} contains an unexpected directory",
                        )
                    verified_directories.add(relative)
                    verify_directory(path)
                elif stat.S_ISREG(value.st_mode):
                    if relative not in normalized_files:
                        raise DetectorDevelopmentError(
                            "unexpected_result_artifact",
                            f"{label} contains an unexpected file",
                        )
                    verified_files.add(relative)
                else:
                    raise DetectorDevelopmentError(
                        "unsafe_result_tree",
                        f"{label} contains a special file",
                    )

        verify_directory(directory)
        return verified_files, verified_directories

    directory_guards: tuple[int, ...] = ()
    file_guards: list[Any] = []
    try:
        if os.name == "nt":
            directory_guards = _open_windows_ancestor_guards(
                tuple(
                    (
                        candidate if relative == "." else candidate.joinpath(*PurePosixPath(relative).parts),
                        expected[:2],
                    )
                    for relative, (kind, expected) in captured.items()
                    if kind == "directory"
                ),
                label,
            )
            for relative, (kind, expected) in captured.items():
                if kind != "file":
                    continue
                path = candidate.joinpath(*PurePosixPath(relative).parts)
                file_guards.append(_open_verified_regular_file(path, expected, label))

        try:
            ancestors_are_current = _ancestor_identities_are_current(ancestors)
        except OSError as exc:
            raise DetectorDevelopmentError(
                "source_changed",
                f"{label} ancestors changed during validation",
            ) from exc
        if not ancestors_are_current:
            raise DetectorDevelopmentError("source_changed", f"{label} ancestors changed during validation")

        for relative, (kind, expected) in captured.items():
            if kind != "file":
                continue
            path = candidate.joinpath(*PurePosixPath(relative).parts)
            current = metadata(path)
            if (
                not stat.S_ISREG(current.st_mode)
                or _change_identity(
                    path,
                    stat_token(current),
                    label,
                    directory=False,
                )
                != expected
            ):
                raise DetectorDevelopmentError("source_changed", f"{label} changed during validation")

        final_files, final_directories = verify_exact_entries(candidate)
        if final_files != normalized_files or final_directories != expected_directories:
            raise DetectorDevelopmentError(
                "invalid_result_allowlist",
                f"{label} does not match its exact artifact allowlist",
            )

        for relative, (kind, expected) in captured.items():
            if kind != "directory":
                continue
            path = candidate if relative == "." else candidate.joinpath(*PurePosixPath(relative).parts)
            current = metadata(path)
            if (
                not stat.S_ISDIR(current.st_mode)
                or _change_identity(
                    path,
                    stat_token(current),
                    label,
                    directory=True,
                )
                != expected
            ):
                raise DetectorDevelopmentError("source_changed", f"{label} changed during validation")
        return tuple((relative, kind, identity) for relative, (kind, identity) in sorted(captured.items()))
    finally:
        for guard in reversed(file_guards):
            try:
                guard.close()
            except OSError:
                pass
        _close_windows_handles(directory_guards)


def atomic_write_json(path: Path, value: Any, *, trusted_root: Path | None = None) -> None:
    root = _secure_resolved_root(trusted_root or path.parent, "atomic JSON")
    destination = Path(os.path.abspath(path))
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise DetectorDevelopmentError("path_outside_trusted_root", "atomic JSON destination escapes its root") from exc
    if not destination.parent.is_dir():
        raise DetectorDevelopmentError("path_unavailable", "atomic JSON parent must already exist")
    ancestors = _capture_ancestor_identities(destination, root, "atomic JSON")
    destination_identity = _atomic_json_destination_identity(destination)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.{id(value)}.tmp")
    content = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n"
    expected_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_identity = regular_file_change_identity(
            temporary,
            "atomic JSON temporary",
        )
        _replace_atomic_json_with_bounded_windows_retry(
            temporary,
            destination,
            ancestors=ancestors,
            destination_identity=destination_identity,
            temporary_identity=temporary_identity,
        )
        if not _ancestor_identities_are_current(ancestors):
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


def _replace_atomic_json_with_bounded_windows_retry(
    temporary: Path,
    destination: Path,
    *,
    ancestors: tuple[tuple[Path, tuple[int, int]], ...],
    destination_identity: tuple[int, int, int, int, int] | None,
    temporary_identity: tuple[int, int, int, int, int, int],
) -> None:
    deadline = time.monotonic() + _WINDOWS_ATOMIC_REPLACE_RETRY_SECONDS
    backoff = _WINDOWS_ATOMIC_REPLACE_INITIAL_BACKOFF_SECONDS
    last_sharing_error: PermissionError | None = None
    while True:
        if last_sharing_error is not None and time.monotonic() >= deadline:
            raise last_sharing_error
        if not _ancestor_identities_are_current(ancestors):
            raise DetectorDevelopmentError(
                "source_changed",
                "atomic JSON ancestor changed before publication",
            )
        if _atomic_json_destination_identity(destination) != destination_identity:
            raise DetectorDevelopmentError(
                "source_changed",
                "atomic JSON destination changed before publication",
            )
        try:
            current_temporary_identity = regular_file_change_identity(
                temporary,
                "atomic JSON temporary",
            )
        except DetectorDevelopmentError as exc:
            raise DetectorDevelopmentError(
                "source_changed",
                "atomic JSON temporary changed before publication",
            ) from exc
        if current_temporary_identity != temporary_identity:
            raise DetectorDevelopmentError(
                "source_changed",
                "atomic JSON temporary changed before publication",
            )
        try:
            os.replace(temporary, destination)
            return
        except PermissionError as exc:
            if not _is_windows_atomic_replace_sharing_error(exc):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                raise
            last_sharing_error = exc
            time.sleep(min(backoff, remaining))
            backoff = min(
                backoff * 2.0,
                _WINDOWS_ATOMIC_REPLACE_MAX_BACKOFF_SECONDS,
            )


def _is_windows_atomic_replace_sharing_error(exc: PermissionError) -> bool:
    return os.name == "nt" and getattr(exc, "winerror", None) in _WINDOWS_ATOMIC_REPLACE_SHARING_ERRORS


def _atomic_json_destination_identity(
    destination: Path,
) -> tuple[int, int, int, int, int] | None:
    try:
        metadata = destination.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DetectorDevelopmentError(
            "path_unavailable",
            "atomic JSON destination is unavailable",
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_file_attributes", 0) & WINDOWS_REPARSE_ATTRIBUTE):
        raise DetectorDevelopmentError(
            "unsafe_path",
            "atomic JSON destination must not be a link or reparse point",
        )
    if not stat.S_ISREG(metadata.st_mode):
        raise DetectorDevelopmentError(
            "unsafe_path",
            "atomic JSON destination must be a regular file",
        )
    return stat_token(metadata)


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
) -> tuple[tuple[Path, tuple[int, int]], ...]:
    if trusted_root is None:
        root = _secure_resolved_root(path.parent, label)
    else:
        root = _secure_resolved_root(trusted_root, label)
    candidate = Path(os.path.abspath(path))
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise DetectorDevelopmentError("path_outside_trusted_root", f"{label} escapes its trusted root") from exc
    identities: list[tuple[Path, tuple[int, int]]] = []
    current = root
    relative_parent = candidate.parent.relative_to(root)
    for part in (Path(), *relative_parent.parts):
        if part != Path():
            current = current / part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise DetectorDevelopmentError("path_unavailable", f"{label} ancestor is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0) & WINDOWS_REPARSE_ATTRIBUTE
        ):
            raise DetectorDevelopmentError("unsafe_path", f"{label} traverses a link or reparse point")
        if not stat.S_ISDIR(metadata.st_mode):
            raise DetectorDevelopmentError("unsafe_path", f"{label} ancestor must be a directory")
        identities.append((current, (int(metadata.st_dev), int(metadata.st_ino))))
    return tuple(identities)


def _ancestor_identities_are_current(identities: tuple[tuple[Path, tuple[int, int]], ...]) -> bool:
    for path, expected in identities:
        try:
            metadata = path.lstat()
        except OSError:
            return False
        if (
            stat.S_ISLNK(metadata.st_mode)
            or bool(getattr(metadata, "st_file_attributes", 0) & WINDOWS_REPARSE_ATTRIBUTE)
            or not stat.S_ISDIR(metadata.st_mode)
            or (int(metadata.st_dev), int(metadata.st_ino)) != expected
        ):
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
