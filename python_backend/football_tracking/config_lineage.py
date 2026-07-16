from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping
from uuid import uuid4

CONFIG_LINEAGE_SCHEMA_VERSION = "1.0"
CONFIG_LINEAGE_MANIFEST_NAME = "config_lineage_reconfirmation.v1.json"
CANONICAL_CONFIG_NAME = "confirmed_config.canonical-lf.yaml"
CONFIG_LINEAGE_REQUIRED = "confirmed_config_lineage_reconfirmation_required"
CONFIG_LINEAGE_UNSAFE = "config_lineage_snapshot_unsafe"
CONFIG_LINEAGE_MISMATCH = "config_lineage_snapshot_mismatch"
CONFIG_LINEAGE_CONFLICT = "config_lineage_reconfirmation_conflict"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_REQUIRED_WORKFLOW_BINDINGS = frozenset(
    {
        "workflow_id",
        "accepted_trial",
        "request",
        "intent",
        "trial_patch",
        "production_patch",
        "calibration",
        "source_signature",
        "historical_full_runs",
    }
)
_READ_CHUNK = 1024 * 1024


class ConfigLineageError(ValueError):
    """A stable, fail-closed configuration lineage error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ConfigByteInspection:
    canonical_bytes: bytes
    observed_raw_sha256: str
    confirmed_text_sha256: str
    observed_size_bytes: int
    canonical_size_bytes: int
    crlf_count: int
    lf_count: int


@dataclass(frozen=True)
class ConfigLineageGeneration:
    generation_id: str
    generation_dir: Path
    manifest_path: Path
    canonical_snapshot_path: Path
    manifest: dict[str, Any]
    idempotent: bool
    manifest_sha256: str | None = None
    canonical_snapshot_sha256: str | None = None


class _AnchoredDir:
    """Owned POSIX directory descriptor; every child operation is one component."""

    def __init__(
        self,
        descriptor: int,
        path: Path,
        *,
        anchor_descriptor: int | None = None,
        anchor_name: str | None = None,
    ) -> None:
        self.descriptor = descriptor
        self.path = path
        self.anchor_descriptor = anchor_descriptor
        self.anchor_name = anchor_name
        self._chain_links: list[tuple[int, str, tuple[int, int, int, int, int]]] = []
        details = os.fstat(descriptor)
        if not stat.S_ISDIR(details.st_mode):
            os.close(descriptor)
            raise ConfigLineageError(
                CONFIG_LINEAGE_UNSAFE,
                "config lineage anchored node is not a directory",
            )
        self.identity = _stat_token(details)

    def __enter__(self) -> "_AnchoredDir":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1
        if self.anchor_descriptor is not None:
            os.close(self.anchor_descriptor)
            self.anchor_descriptor = None
        for parent_descriptor, _name, _identity in self._chain_links:
            os.close(parent_descriptor)
        self._chain_links = []

    def assert_current(self) -> None:
        _verify_directory_identity(self)

    def child(self, name: str, *, create: bool = False) -> "_AnchoredDir":
        _single_component(name, "directory")
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=self.descriptor)
            except FileExistsError:
                pass
        try:
            descriptor = os.open(name, _directory_open_flags(), dir_fd=self.descriptor)
        except OSError as exc:
            raise ConfigLineageError(
                CONFIG_LINEAGE_UNSAFE,
                f"config lineage directory {name!r} is unavailable or unsafe",
            ) from exc
        anchored = _AnchoredDir(
            descriptor,
            self.path / name,
            anchor_descriptor=os.dup(self.descriptor),
            anchor_name=name,
        )
        anchored._chain_links = [
            (os.dup(parent_descriptor), link_name, identity)
            for parent_descriptor, link_name, identity in self._chain_links
        ]
        anchored._chain_links.append(
            (os.dup(self.descriptor), name, anchored.identity)
        )
        _verify_directory_identity(anchored)
        return anchored

    def create_exclusive_child(self, name: str) -> "_AnchoredDir":
        _single_component(name, "directory")
        self.assert_current()
        try:
            os.mkdir(name, 0o700, dir_fd=self.descriptor)
        except OSError as exc:
            raise ConfigLineageError(
                CONFIG_LINEAGE_CONFLICT if exc.errno == errno.EEXIST else CONFIG_LINEAGE_UNSAFE,
                "config lineage exclusive staging directory creation failed",
            ) from exc
        return self.child(name)

    def names(self) -> list[str]:
        self.assert_current()
        try:
            names = sorted(os.listdir(self.descriptor))
        except OSError as exc:
            raise ConfigLineageError(
                CONFIG_LINEAGE_UNSAFE,
                "config lineage directory enumeration failed",
            ) from exc
        self.assert_current()
        return names

    def read_regular(self, name: str) -> tuple[bytes, dict[str, int]]:
        self.assert_current()
        _single_component(name, "file")
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=self.descriptor,
            )
        except OSError as exc:
            raise ConfigLineageError(
                CONFIG_LINEAGE_UNSAFE,
                "config lineage snapshot unsafe: anchored file open failed",
            ) from exc
        try:
            before = os.fstat(descriptor)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, _READ_CHUNK)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
            replay_descriptor = os.open(
                name,
                os.O_RDONLY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=self.descriptor,
            )
            try:
                replay = os.fstat(replay_descriptor)
            finally:
                os.close(replay_descriptor)
        except OSError as exc:
            raise ConfigLineageError(
                CONFIG_LINEAGE_UNSAFE,
                "config lineage snapshot unsafe: anchored stable read failed",
            ) from exc
        finally:
            os.close(descriptor)
        tokens = (
            _stat_token(before),
            _stat_token(after),
            _stat_token(replay),
        )
        if (
            len(set(tokens)) != 1
            or not stat.S_ISREG(after.st_mode)
            or int(after.st_nlink) != 1
        ):
            raise ConfigLineageError(
                CONFIG_LINEAGE_UNSAFE,
                "config lineage snapshot unsafe: anchored file identity changed",
            )
        self.assert_current()
        return b"".join(chunks), {
            "device": int(after.st_dev),
            "inode": int(after.st_ino),
            "size_bytes": int(after.st_size),
        }

    def write_exclusive(self, name: str, content: bytes) -> None:
        self.assert_current()
        _single_component(name, "file")
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=self.descriptor,
            )
        except OSError as exc:
            raise ConfigLineageError(
                CONFIG_LINEAGE_CONFLICT if exc.errno == errno.EEXIST else CONFIG_LINEAGE_UNSAFE,
                "config lineage anchored exclusive write failed",
            ) from exc
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError(errno.EIO, "short config lineage write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.assert_current()

    def fsync(self) -> None:
        self.assert_current()
        os.fsync(self.descriptor)

    @contextmanager
    def lock(self, name: str) -> Iterator[None]:
        import fcntl

        _single_component(name, "lock file")
        try:
            descriptor = os.open(
                name,
                os.O_RDWR
                | os.O_CREAT
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=self.descriptor,
            )
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or int(details.st_nlink) != 1:
                raise ConfigLineageError(
                    CONFIG_LINEAGE_UNSAFE,
                    "config lineage lock is not a unique regular file",
                )
            if details.st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            if _opened_entry_identity(
                self.descriptor,
                name,
                directory=False,
            ) != _stat_token(os.fstat(descriptor)):
                raise ConfigLineageError(
                    CONFIG_LINEAGE_UNSAFE,
                    "config lineage lock identity changed",
                )
            self.assert_current()
            yield
        except ConfigLineageError:
            raise
        except OSError as exc:
            raise ConfigLineageError(
                CONFIG_LINEAGE_UNSAFE,
                "config lineage anchored lock failed",
            ) from exc
        finally:
            if "descriptor" in locals():
                try:
                    self.assert_current()
                    if _opened_entry_identity(
                        self.descriptor,
                        name,
                        directory=False,
                    ) != _stat_token(os.fstat(descriptor)):
                        raise ConfigLineageError(
                            CONFIG_LINEAGE_UNSAFE,
                            "config lineage lock identity changed before unlock",
                        )
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def rename_noreplace(
        self,
        source_name: str,
        destination_name: str,
        *,
        source: "_AnchoredDir",
    ) -> None:
        import ctypes

        _single_component(source_name, "source directory")
        _single_component(destination_name, "destination directory")
        self.assert_current()
        source.assert_current()
        source_entry_identity = _opened_entry_identity(
            self.descriptor,
            source_name,
            directory=True,
        )
        if source_entry_identity != source.identity:
            raise ConfigLineageError(
                CONFIG_LINEAGE_UNSAFE,
                "config lineage staging source identity changed before publish",
            )
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise ConfigLineageError(
                CONFIG_LINEAGE_UNSAFE,
                "atomic handle-relative no-replace publish is unavailable",
            )
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        result = renameat2(
            self.descriptor,
            os.fsencode(source_name),
            self.descriptor,
            os.fsencode(destination_name),
            1,
        )
        if result == 0:
            if source.anchor_descriptor is not None:
                os.close(source.anchor_descriptor)
            source.anchor_descriptor = os.dup(self.descriptor)
            source.anchor_name = destination_name
            if source._chain_links:
                parent_descriptor, _old_name, _old_identity = source._chain_links.pop()
                os.close(parent_descriptor)
            source._chain_links.append(
                (os.dup(self.descriptor), destination_name, source.identity)
            )
            source.path = self.path / destination_name
            source.assert_current()
            self.assert_current()
            return
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise ConfigLineageError(
                CONFIG_LINEAGE_CONFLICT,
                "config lineage destination exists",
            )
        raise ConfigLineageError(
            CONFIG_LINEAGE_UNSAFE,
            f"handle-relative config lineage publish failed: {os.strerror(error)}",
        )

    def remove_staging(self, name: str, staging: "_AnchoredDir") -> None:
        _single_component(name, "staging directory")
        try:
            self.assert_current()
            staging.assert_current()
            if _opened_entry_identity(
                self.descriptor,
                name,
                directory=True,
            ) != staging.identity:
                raise ConfigLineageError(
                    CONFIG_LINEAGE_UNSAFE,
                    "config lineage staging identity changed before cleanup",
                )
            for entry in staging.names():
                try:
                    descriptor = os.open(
                        entry,
                        os.O_RDONLY
                        | os.O_NOFOLLOW
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NONBLOCK", 0),
                        dir_fd=staging.descriptor,
                    )
                    details = os.fstat(descriptor)
                except OSError as exc:
                    raise ConfigLineageError(
                        CONFIG_LINEAGE_UNSAFE,
                        "config lineage staging entry is unsafe",
                    ) from exc
                finally:
                    if "descriptor" in locals():
                        os.close(descriptor)
                        del descriptor
                if not stat.S_ISREG(details.st_mode) or int(details.st_nlink) != 1:
                    raise ConfigLineageError(
                        CONFIG_LINEAGE_UNSAFE,
                        "config lineage staging contains an unsafe entry",
                    )
                os.unlink(entry, dir_fd=staging.descriptor)
            staging.assert_current()
            os.rmdir(name, dir_fd=self.descriptor)
        except FileNotFoundError:
            return


def _require_handle_relative_backend() -> None:
    if os.name == "nt":
        raise ConfigLineageError(
            CONFIG_LINEAGE_UNSAFE,
            "Windows handle-relative config lineage backend is unavailable",
        )
    required_dir_fd = (os.open, os.stat, os.mkdir, os.rename, os.unlink, os.rmdir)
    if (
        os.name != "posix"
        or getattr(os, "O_NOFOLLOW", None) is None
        or getattr(os, "O_DIRECTORY", None) is None
        or any(function not in os.supports_dir_fd for function in required_dir_fd)
    ):
        raise ConfigLineageError(
            CONFIG_LINEAGE_UNSAFE,
            "POSIX handle-relative config lineage backend is unavailable",
        )


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _opened_entry_identity(
    parent_descriptor: int,
    name: str,
    *,
    directory: bool,
) -> tuple[int, int, int, int, int]:
    flags = (
        _directory_open_flags()
        if directory
        else os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    try:
        return _stat_token(os.fstat(descriptor))
    finally:
        os.close(descriptor)


def _single_component(name: str, label: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "/" in name
        or os.sep in name
        or (os.altsep is not None and os.altsep in name)
    ):
        raise ConfigLineageError(
            CONFIG_LINEAGE_UNSAFE,
            f"config lineage {label} must be one path component",
        )
    return name


def _verify_directory_identity(directory: _AnchoredDir) -> None:
    if _stat_token(os.fstat(directory.descriptor)) != directory.identity:
        raise ConfigLineageError(
            CONFIG_LINEAGE_UNSAFE,
            "config lineage anchored directory identity changed",
        )
    for parent_descriptor, name, expected_identity in directory._chain_links:
        try:
            descriptor = os.open(
                name,
                _directory_open_flags(),
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise ConfigLineageError(
                CONFIG_LINEAGE_UNSAFE,
                "config lineage anchored directory entry is unavailable",
            ) from exc
        try:
            identity = _stat_token(os.fstat(descriptor))
        finally:
            os.close(descriptor)
        if identity != expected_identity:
            raise ConfigLineageError(
                CONFIG_LINEAGE_UNSAFE,
                "config lineage anchored directory entry identity changed",
            )


def _open_absolute_directory(
    path: Path,
    *,
    create: bool,
    missing_code: str = CONFIG_LINEAGE_UNSAFE,
) -> _AnchoredDir:
    _require_handle_relative_backend()
    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute():
        raise ConfigLineageError(CONFIG_LINEAGE_UNSAFE, "config lineage root must be absolute")
    try:
        current = _AnchoredDir(os.open("/", _directory_open_flags()), Path("/"))
        for part in absolute.parts[1:]:
            try:
                child = current.child(part, create=create)
            except ConfigLineageError as exc:
                current.close()
                if not create and isinstance(exc.__cause__, FileNotFoundError):
                    raise ConfigLineageError(
                        missing_code,
                        "confirmed config lineage reconfirmation is required",
                    ) from exc
                raise
            current.close()
            current = child
        current.assert_current()
        return current
    except ConfigLineageError:
        raise
    except OSError as exc:
        raise ConfigLineageError(
            missing_code,
            "config lineage anchored root is unavailable",
        ) from exc


def _anchored_capture_absolute_file(
    trusted_root: Path,
    path: Path,
) -> tuple[bytes, dict[str, int], str]:
    root_path = Path(os.path.abspath(trusted_root))
    absolute = Path(os.path.abspath(path))
    try:
        relative = absolute.relative_to(root_path)
    except ValueError as exc:
        raise ConfigLineageError(
            CONFIG_LINEAGE_UNSAFE,
            "config lineage file escapes its trusted root",
        ) from exc
    if not relative.parts:
        raise ConfigLineageError(CONFIG_LINEAGE_UNSAFE, "config lineage file path is invalid")
    with _open_absolute_directory(root_path, create=False) as root:
        current = root
        owned: list[_AnchoredDir] = []
        try:
            for part in relative.parts[:-1]:
                child = current.child(part)
                owned.append(child)
                current = child
            raw, identity = current.read_regular(relative.parts[-1])
        finally:
            for directory in reversed(owned):
                directory.close()
    return raw, identity, relative.as_posix()


def _anchored_generation(
    generations: _AnchoredDir,
    generation_id: str,
    *,
    idempotent: bool,
) -> tuple[ConfigLineageGeneration, bytes, bytes]:
    _single_component(generation_id, "generation")
    with generations.child(generation_id) as generation_dir:
        initial_names = generation_dir.names()
        if set(initial_names) != {
            CANONICAL_CONFIG_NAME,
            CONFIG_LINEAGE_MANIFEST_NAME,
        }:
            raise ConfigLineageError(
                CONFIG_LINEAGE_UNSAFE,
                "config lineage snapshot unsafe: unexpected generation entries",
            )
        canonical_bytes, _ = generation_dir.read_regular(CANONICAL_CONFIG_NAME)
        manifest_bytes, _ = generation_dir.read_regular(CONFIG_LINEAGE_MANIFEST_NAME)
        if generation_dir.names() != initial_names:
            raise ConfigLineageError(
                CONFIG_LINEAGE_UNSAFE,
                "config lineage generation entries changed during replay",
            )
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigLineageError(
                CONFIG_LINEAGE_UNSAFE,
                "config lineage snapshot unsafe: invalid manifest",
            ) from exc
        if not isinstance(manifest, dict) or _json_bytes(manifest) != manifest_bytes:
            raise ConfigLineageError(
                CONFIG_LINEAGE_UNSAFE,
                "config lineage snapshot unsafe: manifest is not canonical",
            )
        declared_generation_id = manifest.get("generation_id")
        base_manifest = dict(manifest)
        base_manifest.pop("generation_id", None)
        projection = base_manifest.get("projection")
        if isinstance(projection, dict):
            normalized_projection = dict(projection)
            normalized_projection.pop("lineage_generation_id", None)
            base_manifest["projection"] = normalized_projection
        expected_id = f"lineage-{_canonical_sha256(base_manifest)[:24]}"
        canonical_manifest = manifest.get("canonical_snapshot")
        proof = manifest.get("proof")
        if (
            manifest.get("schema_version") != CONFIG_LINEAGE_SCHEMA_VERSION
            or manifest.get("artifact_type") != "config_lineage_reconfirmation"
            or declared_generation_id != generation_id
            or declared_generation_id != expected_id
            or not isinstance(canonical_manifest, dict)
            or canonical_manifest.get("path") != CANONICAL_CONFIG_NAME
            or canonical_manifest.get("canonical_snapshot_sha256")
            != hashlib.sha256(canonical_bytes).hexdigest()
            or canonical_manifest.get("size_bytes") != len(canonical_bytes)
            or canonical_manifest.get("encoding") != "utf-8"
            or canonical_manifest.get("newline") != "lf"
            or canonical_manifest.get("bom") is not False
            or not isinstance(proof, dict)
            or proof.get("content") != "same_text_content_reconfirmed"
            or proof.get("historical_snapshot")
            != "historical_raw_snapshot_not_observed"
        ):
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                "config lineage snapshot mismatch",
            )
        inspected = inspect_config_bytes(canonical_bytes)
        if inspected.canonical_bytes != canonical_bytes or inspected.crlf_count:
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                "config lineage canonical snapshot is not LF",
            )
        generation_path = generations.path / generation_id
        return (
            ConfigLineageGeneration(
                generation_id=generation_id,
                generation_dir=generation_path,
                manifest_path=generation_path / CONFIG_LINEAGE_MANIFEST_NAME,
                canonical_snapshot_path=generation_path / CANONICAL_CONFIG_NAME,
                manifest=manifest,
                idempotent=idempotent,
                manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                canonical_snapshot_sha256=hashlib.sha256(canonical_bytes).hexdigest(),
            ),
            manifest_bytes,
            canonical_bytes,
        )


def _anchored_visible_generation_names(generations: _AnchoredDir) -> list[str]:
    names_before = generations.names()
    identities: dict[str, tuple[int, int, int, int, int]] = {}
    for name in names_before:
        if name.startswith("."):
            raise ConfigLineageError(
                CONFIG_LINEAGE_UNSAFE,
                "config lineage snapshot unsafe: incomplete or hidden generation exists",
            )
        with generations.child(name) as child:
            child.assert_current()
            identities[name] = child.identity
    names_after = generations.names()
    if names_after != names_before:
        raise ConfigLineageError(
            CONFIG_LINEAGE_UNSAFE,
            "config lineage generation set changed during enumeration",
        )
    for name, expected_identity in identities.items():
        if _opened_entry_identity(
            generations.descriptor,
            name,
            directory=True,
        ) != expected_identity:
            raise ConfigLineageError(
                CONFIG_LINEAGE_UNSAFE,
                "config lineage generation identity changed during enumeration",
            )
    return names_before


def _anchored_publish_generation(
    lineage_root: Path,
    *,
    run_id: str,
    generation_id: str,
    manifest: dict[str, Any],
    manifest_bytes: bytes,
    canonical_bytes: bytes,
) -> ConfigLineageGeneration:
    with _open_absolute_directory(lineage_root, create=True) as managed:
        with managed.child(run_id, create=True) as target:
            with target.child("generations", create=True) as generations:
                with target.lock(".config-lineage.lock"):
                    existing = _anchored_visible_generation_names(generations)
                    if existing:
                        if existing != [generation_id]:
                            raise ConfigLineageError(
                                CONFIG_LINEAGE_CONFLICT,
                                "config lineage reconfirmation conflict: a different or additional generation exists",
                            )
                        generation, observed_manifest, observed_canonical = _anchored_generation(
                            generations,
                            generation_id,
                            idempotent=True,
                        )
                        if (
                            generation.manifest != manifest
                            or observed_manifest != manifest_bytes
                            or observed_canonical != canonical_bytes
                        ):
                            raise ConfigLineageError(
                                CONFIG_LINEAGE_CONFLICT,
                                "config lineage reconfirmation conflict: existing generation is not byte-identical",
                            )
                        return generation
                    staging_name = f".staging-{uuid4().hex}"
                    staging: _AnchoredDir | None = None
                    renamed = False
                    try:
                        staging = generations.create_exclusive_child(staging_name)
                        staging.write_exclusive(
                            CANONICAL_CONFIG_NAME,
                            canonical_bytes,
                        )
                        staging.write_exclusive(
                            CONFIG_LINEAGE_MANIFEST_NAME,
                            manifest_bytes,
                        )
                        staging.fsync()
                        generations.rename_noreplace(
                            staging_name,
                            generation_id,
                            source=staging,
                        )
                        renamed = True
                        generations.fsync()
                    except BaseException:
                        if staging is not None and not renamed:
                            try:
                                generations.remove_staging(staging_name, staging)
                            except BaseException:
                                pass
                        raise
                    finally:
                        if staging is not None:
                            staging.close()
                    generation, observed_manifest, observed_canonical = _anchored_generation(
                        generations,
                        generation_id,
                        idempotent=False,
                    )
                    if (
                        generation.manifest != manifest
                        or observed_manifest != manifest_bytes
                        or observed_canonical != canonical_bytes
                    ):
                        raise ConfigLineageError(
                            CONFIG_LINEAGE_CONFLICT,
                            "published config lineage generation is not byte-identical",
                        )
                    return generation


def _anchored_load_generation(
    lineage_root: Path,
    *,
    run_id: str,
) -> tuple[ConfigLineageGeneration, bytes]:
    with _open_absolute_directory(
        lineage_root,
        create=False,
        missing_code=CONFIG_LINEAGE_REQUIRED,
    ) as managed:
        try:
            target = managed.child(run_id)
        except ConfigLineageError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                raise ConfigLineageError(
                    CONFIG_LINEAGE_REQUIRED,
                    "confirmed config lineage reconfirmation is required",
                ) from exc
            raise
        with target:
            try:
                generations = target.child("generations")
            except ConfigLineageError as exc:
                if isinstance(exc.__cause__, FileNotFoundError):
                    raise ConfigLineageError(
                        CONFIG_LINEAGE_REQUIRED,
                        "confirmed config lineage reconfirmation is required",
                    ) from exc
                raise
            with generations:
                names = _anchored_visible_generation_names(generations)
                if not names:
                    raise ConfigLineageError(
                        CONFIG_LINEAGE_REQUIRED,
                        "confirmed config lineage reconfirmation is required",
                    )
                if len(names) != 1:
                    raise ConfigLineageError(
                        CONFIG_LINEAGE_CONFLICT,
                        "config lineage reconfirmation conflict: multiple generations exist",
                    )
                generation, _manifest_bytes, canonical_bytes = _anchored_generation(
                    generations,
                    names[0],
                    idempotent=True,
                )
                return generation, canonical_bytes


def inspect_config_bytes(raw_bytes: bytes) -> ConfigByteInspection:
    """Validate UTF-8 YAML text and canonicalize one uniform newline convention to LF."""

    if not isinstance(raw_bytes, bytes):
        raise ConfigLineageError(CONFIG_LINEAGE_MISMATCH, "config content must be bytes")
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        raise ConfigLineageError(CONFIG_LINEAGE_MISMATCH, "config snapshot mismatch: UTF-8 BOM is forbidden")
    try:
        text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ConfigLineageError(CONFIG_LINEAGE_MISMATCH, "config snapshot mismatch: invalid UTF-8") from exc
    if "\x00" in text:
        raise ConfigLineageError(CONFIG_LINEAGE_MISMATCH, "config snapshot mismatch: NUL is forbidden")
    crlf_count = text.count("\r\n")
    without_crlf = text.replace("\r\n", "")
    if "\r" in without_crlf:
        raise ConfigLineageError(CONFIG_LINEAGE_MISMATCH, "config snapshot mismatch: bare CR is forbidden")
    lf_count = without_crlf.count("\n")
    if crlf_count and lf_count:
        raise ConfigLineageError(CONFIG_LINEAGE_MISMATCH, "config snapshot mismatch: mixed newlines")
    canonical_bytes = text.replace("\r\n", "\n").encode("utf-8")
    return ConfigByteInspection(
        canonical_bytes=canonical_bytes,
        observed_raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        confirmed_text_sha256=hashlib.sha256(canonical_bytes).hexdigest(),
        observed_size_bytes=len(raw_bytes),
        canonical_size_bytes=len(canonical_bytes),
        crlf_count=crlf_count,
        lf_count=lf_count,
    )


def capture_config_bytes(
    trusted_config_root: Path,
    observed_config_path: Path,
) -> tuple[bytes, ConfigByteInspection]:
    """Capture config bytes through the secure handle-relative backend."""

    _require_handle_relative_backend()
    raw_bytes, _identity, _relative = _anchored_capture_absolute_file(
        trusted_config_root,
        observed_config_path,
    )
    return raw_bytes, inspect_config_bytes(raw_bytes)


def capture_regular_file_stat(path: Path) -> dict[str, int]:
    """Capture an arbitrary regular-file identity without reopening a full path."""

    _require_handle_relative_backend()
    absolute = Path(os.path.abspath(path))
    parent = absolute.parent
    with _open_absolute_directory(parent, create=False) as directory:
        name = absolute.name
        _single_component(name, "file")
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory.descriptor,
            )
            first = os.fstat(descriptor)
            replay = _opened_entry_identity(
                directory.descriptor,
                name,
                directory=False,
            )
        except OSError as exc:
            raise ConfigLineageError(
                CONFIG_LINEAGE_UNSAFE,
                "authoritative regular-file identity is unavailable",
            ) from exc
        finally:
            if "descriptor" in locals():
                os.close(descriptor)
        if (
            not stat.S_ISREG(first.st_mode)
            or int(first.st_nlink) != 1
            or _stat_token(first) != replay
        ):
            raise ConfigLineageError(
                CONFIG_LINEAGE_UNSAFE,
                "authoritative regular-file identity is unstable",
            )
        directory.assert_current()
        return {
            "size_bytes": int(first.st_size),
            "mtime_ns": int(first.st_mtime_ns),
        }


def reconfirm_config_lineage(
    *,
    trusted_config_root: Path,
    observed_config_path: Path,
    lineage_root: Path,
    target_run_id: str,
    confirmed_config_name: str,
    confirmed_text_sha256: str,
    expected_observed_raw_sha256: str,
    workflow_bindings: Mapping[str, Any],
    operator_id: str,
    reviewer_id: str,
) -> ConfigLineageGeneration:
    """Publish one immutable canonical snapshot without changing the observed or historical files."""

    _require_handle_relative_backend()
    run_id = _safe_run_id(target_run_id)
    config_name = _required_text(confirmed_config_name, "confirmed_config_name")
    expected_text_sha256 = _required_sha256(confirmed_text_sha256, "confirmed_text_sha256")
    expected_raw_sha256 = _required_sha256(
        expected_observed_raw_sha256,
        "expected_observed_raw_sha256",
    )
    operator = _required_text(operator_id, "operator_id")
    reviewer = _required_text(reviewer_id, "reviewer_id")
    if operator == reviewer:
        raise ConfigLineageError(CONFIG_LINEAGE_MISMATCH, "operator and independent reviewer must differ")
    bindings = _validated_workflow_bindings(workflow_bindings)
    raw_bytes, identity, observed_relative = _anchored_capture_absolute_file(
        trusted_config_root,
        observed_config_path,
    )
    inspection = inspect_config_bytes(raw_bytes)
    if (
        inspection.observed_raw_sha256 != expected_raw_sha256
        or inspection.confirmed_text_sha256 != expected_text_sha256
    ):
        raise ConfigLineageError(
            CONFIG_LINEAGE_MISMATCH,
            "config lineage snapshot mismatch: observed raw or canonical text digest differs",
        )
    if (
        Path(config_name).name != config_name
        or config_name != Path(observed_relative).name
    ):
        raise ConfigLineageError(
            CONFIG_LINEAGE_MISMATCH,
            "config lineage snapshot mismatch: confirmed config name differs from the observed file",
        )

    base_manifest = {
        "schema_version": CONFIG_LINEAGE_SCHEMA_VERSION,
        "artifact_type": "config_lineage_reconfirmation",
        "target_run_id": run_id,
        "proof": {
            "content": "same_text_content_reconfirmed",
            "historical_snapshot": "historical_raw_snapshot_not_observed",
        },
        "observed_config": {
            "path": observed_relative,
            "name": config_name,
            "observed_raw_sha256": inspection.observed_raw_sha256,
            "size_bytes": inspection.observed_size_bytes,
            "file_identity": identity,
            "newline_counts": {
                "crlf": inspection.crlf_count,
                "lf": inspection.lf_count,
            },
        },
        "canonical_snapshot": {
            "path": CANONICAL_CONFIG_NAME,
            "confirmed_text_sha256": expected_text_sha256,
            "canonical_snapshot_sha256": inspection.confirmed_text_sha256,
            "size_bytes": inspection.canonical_size_bytes,
            "encoding": "utf-8",
            "newline": "lf",
            "bom": False,
        },
        "workflow_bindings": bindings,
        "roles": {
            "operator_id": operator,
            "independent_reviewer_id": reviewer,
        },
        "projection": {
            "confirmed_text_sha256": expected_text_sha256,
            "observed_raw_sha256": inspection.observed_raw_sha256,
            "canonical_snapshot_sha256": inspection.confirmed_text_sha256,
            "historical_raw_snapshot_observed": False,
        },
    }
    generation_id = f"lineage-{_canonical_sha256(base_manifest)[:24]}"
    manifest = {
        **base_manifest,
        "generation_id": generation_id,
        "projection": {
            **base_manifest["projection"],
            "lineage_generation_id": generation_id,
        },
    }
    manifest_bytes = _json_bytes(manifest)
    canonical_bytes = inspection.canonical_bytes

    return _anchored_publish_generation(
        lineage_root,
        run_id=run_id,
        generation_id=generation_id,
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        canonical_bytes=canonical_bytes,
    )


def load_config_lineage_reconfirmation(
    lineage_root: Path,
    *,
    target_run_id: str,
    trusted_config_root: Path,
    observed_config_path: Path,
    confirmed_config_name: str,
    confirmed_text_sha256: str,
    expected_workflow_bindings: Mapping[str, Any],
) -> ConfigLineageGeneration:
    """Load and revalidate the sole immutable lineage generation for one target."""

    _require_handle_relative_backend()
    run_id = _safe_run_id(target_run_id)
    expected_text_sha256 = _required_sha256(confirmed_text_sha256, "confirmed_text_sha256")
    expected_bindings = _validated_workflow_bindings(expected_workflow_bindings)
    raw_bytes, identity, observed_relative = _anchored_capture_absolute_file(
        trusted_config_root,
        observed_config_path,
    )
    inspection = inspect_config_bytes(raw_bytes)
    if inspection.confirmed_text_sha256 != expected_text_sha256:
        raise ConfigLineageError(
            CONFIG_LINEAGE_MISMATCH,
            "config lineage snapshot mismatch: current canonical text does not match confirmation",
        )
    generation, generation_canonical_bytes = _anchored_load_generation(
        lineage_root,
        run_id=run_id,
    )
    manifest = generation.manifest
    observed_manifest = manifest.get("observed_config")
    canonical_manifest = manifest.get("canonical_snapshot")
    projection = manifest.get("projection")
    if (
        manifest.get("target_run_id") != run_id
        or not isinstance(observed_manifest, dict)
        or observed_manifest.get("name") != confirmed_config_name
        or observed_manifest.get("path") != observed_relative
        or observed_manifest.get("observed_raw_sha256") != inspection.observed_raw_sha256
        or observed_manifest.get("size_bytes") != inspection.observed_size_bytes
        or observed_manifest.get("file_identity") != identity
        or not isinstance(canonical_manifest, dict)
        or canonical_manifest.get("confirmed_text_sha256") != expected_text_sha256
        or canonical_manifest.get("canonical_snapshot_sha256") != inspection.confirmed_text_sha256
        or not isinstance(projection, dict)
        or projection.get("historical_raw_snapshot_observed") is not False
        or manifest.get("workflow_bindings") != expected_bindings
    ):
        raise ConfigLineageError(
            CONFIG_LINEAGE_MISMATCH,
            "config lineage snapshot mismatch: manifest no longer binds the exact observed configuration",
        )
    if generation_canonical_bytes != inspection.canonical_bytes:
        raise ConfigLineageError(
            CONFIG_LINEAGE_MISMATCH,
            "config lineage snapshot mismatch: canonical bytes differ",
        )
    return generation


def _validated_workflow_bindings(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _REQUIRED_WORKFLOW_BINDINGS:
        raise ConfigLineageError(
            CONFIG_LINEAGE_MISMATCH,
            "config lineage snapshot mismatch: workflow bindings are incomplete or contain extras",
        )
    normalized = json.loads(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False))
    if not isinstance(normalized.get("workflow_id"), str) or not normalized["workflow_id"].strip():
        raise ConfigLineageError(
            CONFIG_LINEAGE_MISMATCH,
            "config lineage snapshot mismatch: workflow_id must be non-empty",
        )
    for name in _REQUIRED_WORKFLOW_BINDINGS - {"workflow_id", "historical_full_runs"}:
        item = normalized.get(name)
        if not isinstance(item, dict) or not item:
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                f"config lineage snapshot mismatch: {name} binding must be non-empty",
            )
    accepted_trial = normalized["accepted_trial"]
    if (
        set(accepted_trial) != {"run_id", "record_sha256", "notes_sha256"}
        or not isinstance(accepted_trial.get("run_id"), str)
        or _SHA256.fullmatch(accepted_trial.get("record_sha256", "")) is None
        or _SHA256.fullmatch(accepted_trial.get("notes_sha256", "")) is None
    ):
        raise ConfigLineageError(
            CONFIG_LINEAGE_MISMATCH,
            "config lineage snapshot mismatch: accepted trial identity is invalid",
        )
    for name in (
        "request",
        "intent",
        "trial_patch",
        "production_patch",
        "calibration",
        "source_signature",
    ):
        binding = normalized[name]
        if set(binding) != {"sha256"} or _SHA256.fullmatch(binding.get("sha256", "")) is None:
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                f"config lineage snapshot mismatch: {name} must be an exact SHA-256 binding",
            )
    historical_runs = normalized.get("historical_full_runs")
    if not isinstance(historical_runs, list) or len(historical_runs) != 2:
        raise ConfigLineageError(
            CONFIG_LINEAGE_MISMATCH,
            "config lineage snapshot mismatch: both historical full runs are required",
        )
    seen_run_ids: set[str] = set()
    statuses: set[str] = set()
    for run in historical_runs:
        if not isinstance(run, dict) or set(run) != {
            "run_id",
            "submission_id",
            "generation_id",
            "status",
            "record_sha256",
            "notes_sha256",
        }:
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                "config lineage snapshot mismatch: historical full-run identity is invalid",
            )
        for field in ("run_id", "submission_id", "generation_id", "status"):
            if not isinstance(run[field], str) or not run[field].strip():
                raise ConfigLineageError(
                    CONFIG_LINEAGE_MISMATCH,
                    "config lineage snapshot mismatch: historical full-run text identity is invalid",
                )
        if (
            _SHA256.fullmatch(run["record_sha256"]) is None
            or _SHA256.fullmatch(run["notes_sha256"]) is None
            or run["run_id"] in seen_run_ids
            or run["status"] not in {"failed", "completed"}
        ):
            raise ConfigLineageError(
                CONFIG_LINEAGE_MISMATCH,
                "config lineage snapshot mismatch: historical full-run digest or status is invalid",
            )
        seen_run_ids.add(run["run_id"])
        statuses.add(run["status"])
    if statuses != {"failed", "completed"}:
        raise ConfigLineageError(
            CONFIG_LINEAGE_MISMATCH,
            "config lineage snapshot mismatch: failed and completed historical runs are both required",
        )
    return normalized


def _stat_token(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(getattr(value, "st_ctime_ns", 0)),
    )


def _safe_run_id(value: Any) -> str:
    text = _required_text(value, "target_run_id")
    if _SAFE_RUN_ID.fullmatch(text) is None or text in {".", ".."}:
        raise ConfigLineageError(CONFIG_LINEAGE_UNSAFE, "target_run_id is not path-safe")
    return text


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ConfigLineageError(CONFIG_LINEAGE_MISMATCH, f"{label} must be non-empty trimmed text")
    return value


def _required_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ConfigLineageError(CONFIG_LINEAGE_MISMATCH, f"{label} must be a lowercase SHA-256")
    return value


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
