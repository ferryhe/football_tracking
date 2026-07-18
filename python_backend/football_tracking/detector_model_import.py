from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import threading
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from football_tracking.detector_development_common import (
    DetectorDevelopmentError,
    atomic_write_json,
    canonical_sha256,
    hash_regular_file,
    is_link_or_reparse,
    json_object_from_bytes,
    read_regular_bytes,
    regular_file_change_identity,
    regular_file_identity,
    require_safe_id,
    require_sha256,
    require_trusted_relative_path,
    secure_mkdirs,
    stat_token,
)
from football_tracking.detector_development_common import (
    snapshot_identity_is_current as _snapshot_identity_is_current,
)

_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_WEIGHT_BYTES = 2 * 1024 * 1024 * 1024
_LICENSE_KINDS = ("dataset", "model", "runtime", "deployment")
_IMPORT_ROOT_RELATIVE = Path("data") / "ball_detector_development_v1" / "import_inbox"
_MODEL_ROOT_RELATIVE = Path("data") / "ball_detector_development_v1" / "models"
_DESCRIPTOR_KEYS = {
    "schema_version", "artifact_type", "model_id", "version", "display_name", "architecture_family",
    "source", "weights", "class_names", "class_map", "input", "memory_envelope", "licenses", "egress",
    "lifecycle_state", "bindings",
}
_PUBLISHED_DESCRIPTOR_KEYS = {
    "schema_version",
    "artifact_type",
    "model_id",
    "version",
    "model_version",
    "display_name",
    "architecture_family",
    "source",
    "weights",
    "runtime_contract",
    "class_names",
    "class_map",
    "expected_input",
    "memory_envelope",
    "licenses",
    "egress",
    "lifecycle_state",
    "bindings",
    "import_manifest_sha256",
    "descriptor_sha256",
}
_IMPORT_VERIFICATION_CACHE_ENTRIES = 64
_IMPORT_VERIFICATION_CACHE_LOCK = threading.RLock()
_IMPORT_VERIFICATION_CACHE: OrderedDict[
    tuple[str, str, str], dict[str, Any]
] = OrderedDict()


def ensure_detector_import_roots(repo_root: Path) -> None:
    root = Path(repo_root)
    development = secure_mkdirs(root, "data", "ball_detector_development_v1")
    secure_mkdirs(development, "import_inbox")
    secure_mkdirs(development, "models")


def import_detector_model(repo_root: Path, request: dict[str, Any]) -> dict[str, Any]:
    """Validate and copy a trusted lineage package without loading its weights."""

    root = Path(repo_root).resolve(strict=True)
    ensure_detector_import_roots(root)
    if set(request) != {"package_relative_path", "manifest_sha256"}:
        raise DetectorDevelopmentError(
            "invalid_import_request",
            "Model import accepts only package_relative_path and manifest_sha256",
            status_code=400,
        )
    expected_manifest_sha256 = require_sha256(request.get("manifest_sha256"), "manifest_sha256")
    inbox = root / _IMPORT_ROOT_RELATIVE
    package = require_trusted_relative_path(
        inbox,
        request.get("package_relative_path"),
        "detector import package",
    )
    if not package.is_dir():
        raise DetectorDevelopmentError("not_regular_directory", "Detector import package must be a directory")
    package_identity = _directory_identity(package)
    manifest_path = require_trusted_relative_path(
        package,
        "descriptor.json",
        "detector import descriptor",
    )
    content, manifest_sha256 = read_regular_bytes(
        manifest_path,
        "detector import descriptor",
        max_bytes=_MAX_MANIFEST_BYTES,
        trusted_root=package,
    )
    if manifest_sha256 != expected_manifest_sha256:
        raise DetectorDevelopmentError("manifest_digest_mismatch", "Detector import descriptor SHA-256 does not match")
    manifest = json_object_from_bytes(content, "detector import descriptor")
    normalized = _validate_import_manifest(manifest)
    weights_relative = normalized["weights"]["relative_path"]
    weights_path = require_trusted_relative_path(package, weights_relative, "detector import weights")
    if weights_path.suffix.lower() not in {".pt", ".onnx"}:
        raise DetectorDevelopmentError("unsupported_weight_format", "Detector import weights must be .pt or .onnx")
    expected_weights_sha256 = normalized["weights"]["sha256"]

    model_id = normalized["model_id"]
    version = normalized["version"]
    from football_tracking.detector_model_registry import builtin_model_identities

    if (model_id, version) in builtin_model_identities():
        raise DetectorDevelopmentError(
            "model_identity_conflict",
            "Imported detector identity conflicts with a built-in model",
        )
    model_root = root / _MODEL_ROOT_RELATIVE
    require_trusted_relative_path(
        model_root,
        model_id,
        "detector model_id storage segment",
        must_exist=False,
    )
    model_parent = secure_mkdirs(model_root, model_id)
    require_trusted_relative_path(
        model_parent,
        version,
        "detector version storage segment",
        must_exist=False,
    )
    model_parent_identity = _directory_identity(model_parent)
    final_dir = model_parent / version
    final_relative_weight = (final_dir / weights_path.name).relative_to(root).as_posix()
    source_weights_sha256, source_weights_size = hash_regular_file(
        weights_path,
        "detector import weights",
        max_bytes=_MAX_WEIGHT_BYTES,
        trusted_root=package,
    )
    if source_weights_sha256 != expected_weights_sha256:
        raise DetectorDevelopmentError(
            "weights_digest_mismatch",
            "Detector import weights SHA-256 does not match",
        )
    if not _directory_identity_is_current(package, package_identity):
        raise DetectorDevelopmentError("source_changed", "Detector import package changed before publication")
    current_manifest_sha256, _ = hash_regular_file(
        manifest_path,
        "detector import descriptor",
        max_bytes=_MAX_MANIFEST_BYTES,
        trusted_root=package,
    )
    if current_manifest_sha256 != manifest_sha256:
        raise DetectorDevelopmentError(
            "source_changed",
            "Detector import descriptor changed before publication",
        )
    descriptor = _build_imported_descriptor(
        normalized,
        manifest_sha256=manifest_sha256,
        weights_relative_path=final_relative_weight,
        weights_size_bytes=source_weights_size,
    )
    if _path_lexically_exists(final_dir):
        return _existing_import_result(root, final_dir, descriptor)

    staging = Path(tempfile.mkdtemp(prefix=f".{version}.staging-", dir=model_parent))
    staging_identity = _directory_identity(staging)
    published = False
    try:
        copied_weights = staging / weights_path.name
        copied_sha256, copied_size = _copy_regular_file_snapshot(
            weights_path,
            copied_weights,
            "detector import weights",
            expected_sha256=expected_weights_sha256,
            max_bytes=_MAX_WEIGHT_BYTES,
        )
        if not _directory_identity_is_current(package, package_identity):
            raise DetectorDevelopmentError("source_changed", "Detector import package changed while it was copied")
        final_manifest_sha256, _ = hash_regular_file(
            manifest_path,
            "detector import descriptor",
            max_bytes=_MAX_MANIFEST_BYTES,
            trusted_root=package,
        )
        if final_manifest_sha256 != manifest_sha256:
            raise DetectorDevelopmentError("source_changed", "Detector import descriptor changed during import")
        if copied_size != source_weights_size or copied_sha256 != source_weights_sha256:
            raise DetectorDevelopmentError("source_changed", "Detector import weights changed during copy")
        atomic_write_json(staging / "descriptor.json", descriptor, trusted_root=staging)
        (staging / "source-descriptor.json").write_bytes(content)
        source_sha256, source_size = hash_regular_file(
            staging / "source-descriptor.json",
            "copied detector import descriptor",
            trusted_root=staging,
        )
        artifact_manifest = {
            "artifact_type": "detector_model_import_artifact_manifest",
            "schema_version": "1.0",
            "model_id": model_id,
            "version": version,
            "artifacts": [
                {
                    "name": "source_descriptor",
                    "relative_path": "source-descriptor.json",
                    "sha256": source_sha256,
                    "size_bytes": source_size,
                },
                {
                    "name": "weights",
                    "relative_path": weights_path.name,
                    "sha256": copied_sha256,
                    "size_bytes": copied_size,
                },
            ],
            "descriptor_sha256": descriptor["descriptor_sha256"],
        }
        atomic_write_json(staging / "artifact-manifest.json", artifact_manifest, trusted_root=staging)
        if (
            not _directory_identity_is_current(staging, staging_identity)
            or not _directory_identity_is_current(model_parent, model_parent_identity)
        ):
            raise DetectorDevelopmentError("source_changed", "Detector import staging identity changed")
        if _path_lexically_exists(final_dir):
            existing_result = _existing_import_result(root, final_dir, descriptor)
            _cleanup_staging_if_current(
                staging,
                staging_identity,
                model_parent,
                model_parent_identity,
            )
            return existing_result
        try:
            os.replace(staging, final_dir)
        except OSError as exc:
            if _path_lexically_exists(final_dir):
                existing_result = _existing_import_result(root, final_dir, descriptor)
                _cleanup_staging_if_current(
                    staging,
                    staging_identity,
                    model_parent,
                    model_parent_identity,
                )
                return existing_result
            raise DetectorDevelopmentError("import_publish_failed", "Detector import could not be atomically published") from exc
        published = True
        if not _directory_identity_is_current(final_dir, staging_identity):
            raise DetectorDevelopmentError("import_publish_verification_failed", "Published detector directory identity changed")
        _verify_and_cache_published_import(root, final_dir, descriptor)
    except BaseException:
        if published:
            _cleanup_staging_if_current(
                final_dir,
                staging_identity,
                model_parent,
                model_parent_identity,
            )
        _cleanup_staging_if_current(staging, staging_identity, model_parent, model_parent_identity)
        raise
    return {"created": True, "model": _imported_model_record(descriptor)}


def load_imported_model_records(repo_root: Path) -> list[dict[str, Any]]:
    root = Path(repo_root).resolve(strict=True)
    model_root = root / _MODEL_ROOT_RELATIVE
    if (
        not model_root.is_dir()
        or model_root.is_symlink()
        or is_link_or_reparse(model_root)
    ):
        return []
    for attempt in range(2):
        membership = _model_membership_snapshot(model_root)
        records: list[dict[str, Any]] = []
        for model_id, version in membership:
            try:
                final_dir = model_root / model_id / version
                if not (final_dir / "descriptor.json").is_file():
                    continue
                records.append(_load_verified_import_record(root, final_dir))
            except (DetectorDevelopmentError, OSError, TypeError, ValueError, KeyError):
                continue
        if membership == _model_membership_snapshot(model_root):
            return records
        if attempt == 0:
            continue
    raise DetectorDevelopmentError(
        "source_changed", "Imported detector catalog changed during enumeration"
    )


def _model_membership_snapshot(model_root: Path) -> tuple[tuple[str, str], ...]:
    root_identity = _directory_identity(model_root)
    membership: list[tuple[str, str]] = []
    try:
        with os.scandir(model_root) as models:
            model_entries = sorted(models, key=lambda entry: entry.name)
        for model_entry in model_entries:
            model_path = Path(model_entry.path)
            if is_link_or_reparse(model_path):
                raise DetectorDevelopmentError(
                    "unsafe_path", "Imported detector model parent is unsafe"
                )
            if not model_entry.is_dir(follow_symlinks=False):
                continue
            with os.scandir(model_path) as versions:
                version_entries = sorted(versions, key=lambda entry: entry.name)
            for version_entry in version_entries:
                version_path = Path(version_entry.path)
                if is_link_or_reparse(version_path):
                    raise DetectorDevelopmentError(
                        "unsafe_path", "Imported detector version directory is unsafe"
                    )
                if version_entry.is_dir(follow_symlinks=False):
                    membership.append((model_entry.name, version_entry.name))
    except DetectorDevelopmentError:
        raise
    except OSError as exc:
        raise DetectorDevelopmentError(
            "path_unavailable", "Imported detector catalog is unavailable"
        ) from exc
    if root_identity != _directory_identity(model_root):
        raise DetectorDevelopmentError(
            "source_changed", "Imported detector catalog changed during enumeration"
        )
    return tuple(membership)


def _load_verified_import_record(
    repo_root: Path, final_dir: Path
) -> dict[str, Any]:
    key = _import_cache_key(repo_root, final_dir)
    with _IMPORT_VERIFICATION_CACHE_LOCK:
        cached = _IMPORT_VERIFICATION_CACHE.get(key)
        if cached is not None:
            try:
                current = _published_import_fingerprint(
                    repo_root, final_dir, cached["descriptor"]
                )
            except (DetectorDevelopmentError, OSError):
                current = None
            if current == cached["fingerprint"]:
                _IMPORT_VERIFICATION_CACHE.move_to_end(key)
                return deepcopy(cached["record"])
            _IMPORT_VERIFICATION_CACHE.pop(key, None)

        content, _ = read_regular_bytes(
            final_dir / "descriptor.json",
            "imported detector descriptor",
            max_bytes=_MAX_MANIFEST_BYTES,
            trusted_root=repo_root / _MODEL_ROOT_RELATIVE,
        )
        descriptor = json_object_from_bytes(content, "imported detector descriptor")
        if (
            final_dir.parent.name != descriptor.get("model_id")
            or final_dir.name != descriptor.get("version")
        ):
            raise DetectorDevelopmentError(
                "import_publish_verification_failed",
                "Imported detector storage identity changed",
            )
        return _verify_and_cache_published_import_locked(
            repo_root, final_dir, descriptor
        )


def _verify_and_cache_published_import(
    repo_root: Path, final_dir: Path, descriptor: dict[str, Any]
) -> dict[str, Any]:
    with _IMPORT_VERIFICATION_CACHE_LOCK:
        return _verify_and_cache_published_import_locked(
            repo_root, final_dir, descriptor
        )


def _verify_and_cache_published_import_locked(
    repo_root: Path, final_dir: Path, descriptor: dict[str, Any]
) -> dict[str, Any]:
    before = _published_import_fingerprint(repo_root, final_dir, descriptor)
    _verify_published_import(repo_root, final_dir, descriptor)
    after = _published_import_fingerprint(repo_root, final_dir, descriptor)
    if before != after:
        raise DetectorDevelopmentError(
            "source_changed",
            "Imported detector files changed during verification",
        )
    record = _imported_model_record(descriptor)
    key = _import_cache_key(repo_root, final_dir)
    _IMPORT_VERIFICATION_CACHE[key] = {
        "descriptor": deepcopy(descriptor),
        "fingerprint": after,
        "record": deepcopy(record),
    }
    _IMPORT_VERIFICATION_CACHE.move_to_end(key)
    while len(_IMPORT_VERIFICATION_CACHE) > _IMPORT_VERIFICATION_CACHE_ENTRIES:
        _IMPORT_VERIFICATION_CACHE.popitem(last=False)
    return record


def _import_cache_key(
    repo_root: Path, final_dir: Path
) -> tuple[str, str, str]:
    return (
        os.path.normcase(str(repo_root)),
        require_safe_id(final_dir.parent.name, "imported detector model_id"),
        require_safe_id(final_dir.name, "imported detector version"),
    )


def _published_import_fingerprint(
    repo_root: Path, final_dir: Path, descriptor: dict[str, Any]
) -> dict[str, Any]:
    model_root = repo_root / _MODEL_ROOT_RELATIVE
    model_parent = final_dir.parent
    if model_parent.parent != model_root or final_dir.parent != model_parent:
        raise DetectorDevelopmentError(
            "unsafe_path", "Imported detector directory escaped the model root"
        )
    weights = descriptor.get("weights")
    if not isinstance(weights, dict):
        raise DetectorDevelopmentError(
            "import_publish_verification_failed",
            "Imported detector weights binding is invalid",
        )
    weights_path = require_trusted_relative_path(
        repo_root,
        weights.get("relative_path"),
        "cached imported detector weights",
    )
    if weights_path.parent != final_dir:
        raise DetectorDevelopmentError(
            "import_publish_verification_failed",
            "Imported detector weights escaped their exact model identity",
        )
    expected_listing = {
        "descriptor.json",
        "source-descriptor.json",
        "artifact-manifest.json",
        weights_path.name,
    }
    ancestor_identities = (
        _directory_identity(model_root),
        _directory_identity(model_parent),
    )
    final_directory_identity = _directory_change_identity(final_dir)
    listing = _verified_directory_listing(final_dir)
    if set(listing) != expected_listing:
        raise DetectorDevelopmentError(
            "import_publish_verification_failed",
            "Imported detector directory listing changed",
        )
    file_identities = {
        name: regular_file_change_identity(
            final_dir / name, f"cached imported detector {name}"
        )
        for name in sorted(expected_listing)
    }
    if ancestor_identities != (
        _directory_identity(model_root),
        _directory_identity(model_parent),
    ) or final_directory_identity != _directory_change_identity(final_dir):
        raise DetectorDevelopmentError(
            "source_changed",
            "Imported detector directory identity changed during cache validation",
        )
    return {
        "ancestors": ancestor_identities,
        "final_directory": final_directory_identity,
        "listing": listing,
        "files": file_identities,
    }


def _verified_directory_listing(path: Path) -> tuple[str, ...]:
    if is_link_or_reparse(path):
        raise DetectorDevelopmentError(
            "unsafe_path", "Imported detector directory must not be a link"
        )
    names: list[str] = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                entry_path = Path(entry.path)
                if is_link_or_reparse(entry_path) or not entry.is_file(
                    follow_symlinks=False
                ):
                    raise DetectorDevelopmentError(
                        "unsafe_path",
                        "Imported detector directory contains an unsafe entry",
                    )
                names.append(entry.name)
    except DetectorDevelopmentError:
        raise
    except OSError as exc:
        raise DetectorDevelopmentError(
            "path_unavailable", "Imported detector directory is unavailable"
        ) from exc
    return tuple(sorted(names))


def _directory_change_identity(
    path: Path,
) -> tuple[int, int, int, int, int, int]:
    if is_link_or_reparse(path):
        raise DetectorDevelopmentError(
            "unsafe_path", "Imported detector ancestor must not be a link"
        )
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise DetectorDevelopmentError(
            "path_unavailable", "Imported detector ancestor is unavailable"
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise DetectorDevelopmentError(
            "not_regular_directory", "Imported detector ancestor is not a directory"
        )
    identity = stat_token(metadata)
    change_time = (
        _windows_directory_change_time(path, identity)
        if os.name == "nt"
        else identity[4]
    )
    return (*identity, change_time)


def _windows_directory_change_time(
    path: Path, expected: tuple[int, int, int, int, int]
) -> int:
    import ctypes
    from ctypes import wintypes

    class _FileBasicInfo(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateFileW(
        str(path),
        0x0080,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        information = _FileBasicInfo()
        if not kernel32.GetFileInformationByHandleEx(
            handle, 0, ctypes.byref(information), ctypes.sizeof(information)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if stat_token(path.stat(follow_symlinks=False)) != expected:
            raise DetectorDevelopmentError(
                "source_changed",
                "Imported detector directory changed during identity validation",
            )
        return int(information.ChangeTime)
    finally:
        if not kernel32.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


def _validate_import_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    unexpected = sorted(set(manifest) - _DESCRIPTOR_KEYS)
    missing = sorted(_DESCRIPTOR_KEYS - set(manifest))
    if unexpected or missing:
        raise DetectorDevelopmentError(
            "invalid_import_descriptor",
            f"Detector import descriptor fields are invalid; missing={missing}, unexpected={unexpected}",
            status_code=400,
        )
    if manifest.get("schema_version") != "1.0" or manifest.get("artifact_type") != "detector_model_import_package":
        raise DetectorDevelopmentError("invalid_import_descriptor", "Detector import descriptor schema is invalid")
    model_id = require_safe_id(manifest.get("model_id"), "model_id")
    version = require_safe_id(manifest.get("version"), "version")
    display_name = _required_text(manifest.get("display_name"), "display_name", 200)
    architecture = _required_text(manifest.get("architecture_family"), "architecture_family", 80).lower()
    if architecture not in {"yolov8", "yolo11", "yolo26", "rfdetr", "onnx"}:
        raise DetectorDevelopmentError("unsupported_architecture", "Imported detector architecture is not allowed")
    source = _required_mapping(manifest.get("source"), "source")
    if set(source) != {"project", "version", "acquisition_method"}:
        raise DetectorDevelopmentError("invalid_import_source", "Imported detector source fields are invalid")
    source_project = _required_text(source.get("project"), "source.project", 200)
    source_version = _required_text(source.get("version"), "source.version", 120)
    if source_version.lower() == "latest":
        raise DetectorDevelopmentError("unbound_model_version", "Imported detector source may not use latest")
    acquisition = source.get("acquisition_method")
    if acquisition not in {"trusted_local_package", "server_lineage_package"}:
        raise DetectorDevelopmentError("invalid_acquisition_method", "Imported detector acquisition method is not trusted")
    weights = _required_mapping(manifest.get("weights"), "weights")
    if set(weights) != {"relative_path", "sha256"}:
        raise DetectorDevelopmentError("invalid_weights", "Imported detector weights fields are invalid")
    weights_relative = weights.get("relative_path")
    if not isinstance(weights_relative, str) or not weights_relative:
        raise DetectorDevelopmentError("invalid_weights", "Imported detector weights path is invalid")
    weights_sha256 = require_sha256(weights.get("sha256"), "weights.sha256")
    class_names = manifest.get("class_names")
    if (
        not isinstance(class_names, list)
        or not 1 <= len(class_names) <= 200
        or any(not isinstance(item, str) or not item.strip() or len(item) > 100 for item in class_names)
        or len(set(class_names)) != len(class_names)
    ):
        raise DetectorDevelopmentError("invalid_class_map", "Imported detector class_names are invalid")
    class_map = _required_mapping(manifest.get("class_map"), "class_map")
    if not class_map or any(key not in class_names or value != "ball" for key, value in class_map.items()):
        raise DetectorDevelopmentError("invalid_class_map", "Imported detector must map an exact checkpoint class to ball")
    input_metadata = _required_mapping(manifest.get("input"), "input")
    if set(input_metadata) != {"image_size", "precision", "device"}:
        raise DetectorDevelopmentError("invalid_input_contract", "Imported detector input fields are invalid")
    image_size = _bounded_int(input_metadata.get("image_size"), "input.image_size", 32, 8192)
    if input_metadata.get("precision") not in {"fp32", "fp16"} or input_metadata.get("device") not in {"cpu", "cuda"}:
        raise DetectorDevelopmentError("invalid_input_contract", "Imported detector precision or device is invalid")
    memory = _required_mapping(manifest.get("memory_envelope"), "memory_envelope")
    if set(memory) != {"max_ram_mb", "max_vram_mb"}:
        raise DetectorDevelopmentError("invalid_memory_envelope", "Imported detector memory envelope is invalid")
    max_ram = _bounded_int(memory.get("max_ram_mb"), "max_ram_mb", 1, 262_144)
    max_vram = _bounded_int(memory.get("max_vram_mb"), "max_vram_mb", 0, 262_144)
    licenses = _validate_licenses(manifest.get("licenses"))
    egress = _required_mapping(manifest.get("egress"), "egress")
    if egress != {
        "frames_leave_local_machine": False,
        "destination": None,
        "operator_consent": "not_required",
    }:
        raise DetectorDevelopmentError("external_egress_not_allowed", "Imported detector egress must remain local")
    if manifest.get("lifecycle_state") != "unverified":
        raise DetectorDevelopmentError("unsafe_lifecycle", "Imported detector must start unverified")
    bindings = _validate_bindings(manifest.get("bindings"))
    return {
        "model_id": model_id,
        "version": version,
        "display_name": display_name,
        "architecture_family": architecture,
        "source": {
            "project": source_project,
            "version": source_version,
            "asset_release": source_version,
            "weight_url": "trusted-import://server-lineage-package",
            "acquisition_method": acquisition,
            "access_requirement": "trusted_server_lineage_package",
        },
        "weights": {"relative_path": weights_relative, "sha256": weights_sha256},
        "class_names": list(class_names),
        "class_map": deepcopy(class_map),
        "expected_input": {
            "image_size": image_size,
            "precision": input_metadata["precision"],
            "device": input_metadata["device"],
            "source_coordinate_space": "source_pixels_xyxy",
        },
        "memory_envelope": {"max_ram_mb": max_ram, "max_vram_mb": max_vram},
        "licenses": licenses,
        "egress": deepcopy(egress),
        "lifecycle_state": "unverified",
        "bindings": bindings,
    }


def _build_imported_descriptor(
    normalized: dict[str, Any],
    *,
    manifest_sha256: str,
    weights_relative_path: str,
    weights_size_bytes: int,
) -> dict[str, Any]:
    descriptor = {
        "schema_version": "1.0",
        "artifact_type": "detector_model_descriptor",
        **deepcopy(normalized),
        "model_version": normalized["version"],
        "weights": {
            "relative_path": weights_relative_path,
            "sha256": normalized["weights"]["sha256"],
            "size_bytes": weights_size_bytes,
        },
        "runtime_contract": {
            "validation": "server_validation_required",
            "arbitrary_executable_model_code_allowed": False,
        },
        "import_manifest_sha256": manifest_sha256,
    }
    descriptor["descriptor_sha256"] = canonical_sha256(
        {key: value for key, value in descriptor.items() if key != "descriptor_sha256"}
    )
    return descriptor


def _imported_model_record(descriptor: dict[str, Any]) -> dict[str, Any]:
    return {
        "descriptor": deepcopy(descriptor),
        "availability": {
            "status": "blocked",
            "reason_codes": ["server_validation_required"],
            "observations": {
                "file": {"status": "pass", "reason": "content_addressed_import_copy"},
                "digest": {"status": "pass", "reason": "import_digest_verified"},
                "class_map": {"status": "not_run", "reason": "checkpoint_class_map_check_required"},
                "license": {"status": "pass", "reason": "four_layer_license_metadata_complete"},
                "runtime_load": {
                    "status": "not_run",
                    "reason": "server_validation_required",
                    "installed_runtime": {
                        "ultralytics": None,
                        "sahi": None,
                        "torch": None,
                    },
                    "evidence_sha256": None,
                },
            },
        },
        "qualification": {
            "trial_eligible": False,
            "source_segment_qualified": False,
            "camera_qualified": False,
        },
        "selectable_for_probe": False,
    }


def _existing_import_result(
    repo_root: Path,
    final_dir: Path,
    expected: dict[str, Any],
) -> dict[str, Any]:
    try:
        _directory_identity(final_dir)
    except DetectorDevelopmentError as exc:
        raise DetectorDevelopmentError("model_identity_conflict", "Existing detector model identity is invalid") from exc
    descriptor_path = final_dir / "descriptor.json"
    try:
        content, _ = read_regular_bytes(
            descriptor_path,
            "existing imported detector descriptor",
            max_bytes=_MAX_MANIFEST_BYTES,
            trusted_root=final_dir,
        )
        existing = json_object_from_bytes(content, "existing imported detector descriptor")
    except DetectorDevelopmentError as exc:
        raise DetectorDevelopmentError("model_identity_conflict", "Existing detector model identity is invalid") from exc
    existing_digest = existing.get("descriptor_sha256")
    existing_without_digest = {
        key: value for key, value in existing.items() if key != "descriptor_sha256"
    }
    expected_digest = expected.get("descriptor_sha256")
    expected_without_digest = {
        key: value for key, value in expected.items() if key != "descriptor_sha256"
    }
    if (
        existing_digest != canonical_sha256(existing_without_digest)
        or expected_digest != canonical_sha256(expected_without_digest)
        or existing != expected
    ):
        raise DetectorDevelopmentError("model_identity_conflict", "Detector model identity conflict")
    try:
        _verify_and_cache_published_import(repo_root, final_dir, expected)
    except DetectorDevelopmentError as exc:
        raise DetectorDevelopmentError("model_identity_conflict", "Existing detector model identity is invalid") from exc
    return {"created": False, "model": _imported_model_record(existing)}


def _verify_published_import(repo_root: Path, final_dir: Path, expected: dict[str, Any]) -> None:
    _directory_identity(final_dir)
    content, _ = read_regular_bytes(
        final_dir / "descriptor.json",
        "published detector descriptor",
        max_bytes=_MAX_MANIFEST_BYTES,
        trusted_root=final_dir,
    )
    descriptor = json_object_from_bytes(content, "published detector descriptor")
    _validate_published_descriptor(repo_root, final_dir, descriptor)
    if descriptor != expected:
        raise DetectorDevelopmentError("import_publish_verification_failed", "Published detector descriptor changed")
    weights = descriptor["weights"]
    weights_size = weights["size_bytes"]
    weights_path = require_trusted_relative_path(
        repo_root,
        weights["relative_path"],
        "published detector weights",
    )
    digest, size = hash_regular_file(
        weights_path,
        "published detector weights",
        max_bytes=_MAX_WEIGHT_BYTES,
        trusted_root=final_dir,
    )
    if digest != weights["sha256"] or size != weights_size:
        raise DetectorDevelopmentError("import_publish_verification_failed", "Published detector weights changed")
    verified_weights = (digest, size)
    manifest_content, _ = read_regular_bytes(
        final_dir / "artifact-manifest.json",
        "published detector artifact manifest",
        max_bytes=_MAX_MANIFEST_BYTES,
        trusted_root=final_dir,
    )
    artifact_manifest = json_object_from_bytes(
        manifest_content,
        "published detector artifact manifest",
    )
    if (
        set(artifact_manifest)
        != {
            "artifact_type",
            "schema_version",
            "model_id",
            "version",
            "artifacts",
            "descriptor_sha256",
        }
        or artifact_manifest.get("artifact_type") != "detector_model_import_artifact_manifest"
        or artifact_manifest.get("schema_version") != "1.0"
        or artifact_manifest.get("model_id") != descriptor["model_id"]
        or artifact_manifest.get("version") != descriptor["version"]
        or artifact_manifest.get("descriptor_sha256") != descriptor["descriptor_sha256"]
    ):
        raise DetectorDevelopmentError(
            "import_publish_verification_failed",
            "Published detector artifact manifest changed",
        )
    artifacts = artifact_manifest.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 2:
        raise DetectorDevelopmentError(
            "import_publish_verification_failed",
            "Published detector artifact manifest is incomplete",
        )
    expected_artifacts = {
        "source_descriptor": "source-descriptor.json",
        "weights": weights_path.name,
    }
    seen: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != {
            "name",
            "relative_path",
            "sha256",
            "size_bytes",
        }:
            raise DetectorDevelopmentError("import_publish_verification_failed", "Published artifact entry is invalid")
        name = item.get("name")
        relative_path = item.get("relative_path")
        size_bytes = item.get("size_bytes")
        if (
            not isinstance(name, str)
            or not isinstance(relative_path, str)
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or not 0 < size_bytes <= _MAX_WEIGHT_BYTES
            or not isinstance(item.get("sha256"), str)
            or len(item["sha256"]) != 64
            or name not in expected_artifacts
            or relative_path != expected_artifacts[name]
            or name in seen
        ):
            raise DetectorDevelopmentError("import_publish_verification_failed", "Published artifact entry changed")
        artifact_path = require_trusted_relative_path(
            final_dir,
            relative_path,
            "published detector artifact",
        )
        artifact_max_bytes = (
            _MAX_MANIFEST_BYTES if name == "source_descriptor" else _MAX_WEIGHT_BYTES
        )
        if size_bytes > artifact_max_bytes:
            raise DetectorDevelopmentError(
                "import_publish_verification_failed",
                "Published artifact entry exceeds its byte limit",
            )
        if name == "weights":
            artifact_sha256, artifact_size = verified_weights
        else:
            artifact_sha256, artifact_size = hash_regular_file(
                artifact_path,
                "published detector artifact",
                max_bytes=artifact_max_bytes,
                trusted_root=final_dir,
            )
        if artifact_sha256 != item.get("sha256") or artifact_size != item.get("size_bytes"):
            raise DetectorDevelopmentError("import_publish_verification_failed", "Published artifact bytes changed")
        seen.add(name)
    if seen != set(expected_artifacts):
        raise DetectorDevelopmentError("import_publish_verification_failed", "Published artifact manifest is incomplete")


def _copy_regular_file_snapshot(
    source: Path,
    destination: Path,
    label: str,
    *,
    expected_sha256: str,
    max_bytes: int,
) -> tuple[str, int]:
    expected = regular_file_identity(source, label)
    if expected[2] > max_bytes:
        raise DetectorDevelopmentError("resource_limit_exceeded", f"{label} exceeds the byte limit")
    digest = hashlib.sha256()
    try:
        with source.open("rb") as source_handle, destination.open("xb") as destination_handle:
            opened = os.fstat(source_handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or stat_token(opened) != expected:
                raise DetectorDevelopmentError("source_changed", f"{label} changed while it was opened")
            copied = 0
            while True:
                chunk = source_handle.read(1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > max_bytes:
                    raise DetectorDevelopmentError("resource_limit_exceeded", f"{label} exceeds the byte limit")
                digest.update(chunk)
                destination_handle.write(chunk)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
            if stat_token(os.fstat(source_handle.fileno())) != expected:
                raise DetectorDevelopmentError("source_changed", f"{label} changed while it was copied")
    except DetectorDevelopmentError:
        raise
    except OSError as exc:
        raise DetectorDevelopmentError("copy_failed", f"{label} could not be copied") from exc
    if not _snapshot_identity_is_current(source, expected):
        raise DetectorDevelopmentError("source_changed", f"{label} changed while it was copied")
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != expected_sha256:
        raise DetectorDevelopmentError("weights_digest_mismatch", f"{label} SHA-256 does not match")
    return actual_sha256, expected[2]


def _validate_licenses(value: Any) -> dict[str, dict[str, Any]]:
    licenses = _required_mapping(value, "licenses")
    if set(licenses) != set(_LICENSE_KINDS):
        raise DetectorDevelopmentError("incomplete_license_metadata", "All four license layers are required")
    normalized: dict[str, dict[str, Any]] = {}
    for kind in _LICENSE_KINDS:
        license_value = _required_mapping(licenses.get(kind), f"licenses.{kind}")
        if set(license_value) != {
            "name",
            "spdx_id",
            "url",
            "reviewed",
            "approved_for_local_probe",
        }:
            raise DetectorDevelopmentError("incomplete_license_metadata", f"{kind} license metadata is incomplete")
        if license_value.get("reviewed") is not True:
            raise DetectorDevelopmentError("incomplete_license_metadata", f"{kind} license must be reviewed")
        if license_value.get("approved_for_local_probe") is not True:
            raise DetectorDevelopmentError(
                "license_not_approved",
                f"{kind} license is not explicitly approved for the local probe",
            )
        normalized[kind] = {
            "name": _required_text(license_value.get("name"), f"licenses.{kind}.name", 200),
            "spdx_id": _required_text(license_value.get("spdx_id"), f"licenses.{kind}.spdx_id", 120),
            "url": _required_text(license_value.get("url"), f"licenses.{kind}.url", 500),
            "reviewed": True,
            "approved_for_local_probe": True,
        }
    return normalized


def _validate_bindings(value: Any) -> dict[str, Any]:
    bindings = _required_mapping(value, "bindings")
    digest_fields = {
        "source_sha256", "temporal_group_sha256", "camera_profile_sha256", "evaluation_package_sha256",
        "threshold_profile_sha256", "environment_sha256",
    }
    expected = digest_fields | {"code_commit"}
    if set(bindings) != expected:
        raise DetectorDevelopmentError("invalid_bindings", "Imported detector bindings are incomplete")
    normalized: dict[str, Any] = {}
    for field in digest_fields:
        item = bindings.get(field)
        normalized[field] = None if item is None else require_sha256(item, f"bindings.{field}")
    commit = bindings.get("code_commit")
    if commit is not None and (not isinstance(commit, str) or not 1 <= len(commit) <= 200):
        raise DetectorDevelopmentError("invalid_bindings", "bindings.code_commit is invalid")
    normalized["code_commit"] = commit
    return normalized


def _required_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DetectorDevelopmentError("invalid_import_descriptor", f"{label} must be an object", status_code=400)
    return value


def _required_text(value: Any, label: str, max_length: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > max_length:
        raise DetectorDevelopmentError("invalid_import_descriptor", f"{label} is invalid", status_code=400)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DetectorDevelopmentError(
            "invalid_import_descriptor",
            f"{label} contains invalid Unicode",
            status_code=400,
        ) from exc
    return value


def _bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise DetectorDevelopmentError("invalid_import_descriptor", f"{label} is outside its bound", status_code=400)
    return value


def _directory_identity(path: Path) -> tuple[int, int]:
    if is_link_or_reparse(path):
        raise DetectorDevelopmentError(
            "unsafe_path",
            "Detector import directory must not be a link or reparse point",
        )
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise DetectorDevelopmentError(
            "path_unavailable",
            "Detector import directory is unavailable",
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise DetectorDevelopmentError("not_regular_directory", "Detector import package must be a directory")
    return int(metadata.st_dev), int(metadata.st_ino)


def _validate_published_descriptor(
    repo_root: Path,
    final_dir: Path,
    descriptor: dict[str, Any],
) -> None:
    if set(descriptor) != _PUBLISHED_DESCRIPTOR_KEYS:
        raise DetectorDevelopmentError(
            "import_publish_verification_failed",
            "Published detector descriptor schema changed",
        )
    expected_descriptor_sha256 = canonical_sha256(
        {key: value for key, value in descriptor.items() if key != "descriptor_sha256"}
    )
    if descriptor.get("descriptor_sha256") != expected_descriptor_sha256:
        raise DetectorDevelopmentError(
            "import_publish_verification_failed",
            "Published detector descriptor self-hash changed",
        )
    try:
        from football_tracking.api.schemas import DetectorModelDescriptorView

        DetectorModelDescriptorView.model_validate(descriptor)
    except Exception as exc:
        raise DetectorDevelopmentError(
            "import_publish_verification_failed",
            "Published detector descriptor does not satisfy the public schema",
        ) from exc
    source_content, source_sha256 = read_regular_bytes(
        final_dir / "source-descriptor.json",
        "published source detector descriptor",
        max_bytes=_MAX_MANIFEST_BYTES,
        trusted_root=final_dir,
    )
    if source_sha256 != descriptor.get("import_manifest_sha256"):
        raise DetectorDevelopmentError(
            "import_publish_verification_failed",
            "Published source descriptor digest changed",
        )
    source_descriptor = json_object_from_bytes(
        source_content, "published source detector descriptor"
    )
    normalized = _validate_import_manifest(source_descriptor)
    weights = descriptor.get("weights")
    if not isinstance(weights, dict) or set(weights) != {
        "relative_path",
        "sha256",
        "size_bytes",
    }:
        raise DetectorDevelopmentError(
            "import_publish_verification_failed", "Published detector weights binding is invalid"
        )
    size_bytes = weights.get("size_bytes")
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or not 0 < size_bytes <= _MAX_WEIGHT_BYTES
    ):
        raise DetectorDevelopmentError(
            "import_publish_verification_failed",
            "Published detector weight size is invalid",
        )
    relative_path = weights.get("relative_path")
    if not isinstance(relative_path, str):
        raise DetectorDevelopmentError(
            "import_publish_verification_failed",
            "Published detector weight path is invalid",
        )
    source_weight_name = Path(normalized["weights"]["relative_path"]).name
    # The source package no longer exists under this root, but the original
    # relative path remains part of the signed lineage and must still satisfy
    # the same cross-platform lexical path policy on every catalog read.
    require_trusted_relative_path(
        final_dir,
        normalized["weights"]["relative_path"],
        "published source detector weight path",
        must_exist=False,
    )
    expected_final_dir = (
        Path(repo_root)
        / _MODEL_ROOT_RELATIVE
        / descriptor["model_id"]
        / descriptor["version"]
    )
    model_root = Path(repo_root) / _MODEL_ROOT_RELATIVE
    require_trusted_relative_path(
        model_root,
        descriptor["model_id"],
        "published detector model_id storage segment",
        must_exist=False,
    )
    require_trusted_relative_path(
        expected_final_dir.parent,
        descriptor["version"],
        "published detector version storage segment",
        must_exist=False,
    )
    try:
        expected_relative_path = (final_dir / source_weight_name).relative_to(repo_root).as_posix()
    except ValueError as exc:
        raise DetectorDevelopmentError(
            "import_publish_verification_failed",
            "Published detector directory escapes the repository root",
        ) from exc
    if (
        final_dir != expected_final_dir
        or Path(source_weight_name).suffix.lower() not in {".pt", ".onnx"}
        or Path(relative_path).name != source_weight_name
        or relative_path != expected_relative_path
    ):
        raise DetectorDevelopmentError(
            "import_publish_verification_failed",
            "Published detector weight path no longer matches its source manifest",
        )
    rebuilt = _build_imported_descriptor(
        normalized,
        manifest_sha256=source_sha256,
        weights_relative_path=relative_path,
        weights_size_bytes=size_bytes,
    )
    if rebuilt != descriptor:
        raise DetectorDevelopmentError(
            "import_publish_verification_failed",
            "Published detector descriptor no longer matches its source manifest",
        )
def _directory_identity_is_current(path: Path, expected: tuple[int, int]) -> bool:
    try:
        metadata = path.stat(follow_symlinks=False)
        return (
            not is_link_or_reparse(path)
            and stat.S_ISDIR(metadata.st_mode)
            and (int(metadata.st_dev), int(metadata.st_ino)) == expected
        )
    except OSError:
        return False


def _path_lexically_exists(path: Path) -> bool:
    try:
        path.lstat()
    except OSError:
        return False
    return True


def _cleanup_staging_if_current(
    staging: Path,
    staging_identity: tuple[int, int],
    model_parent: Path,
    model_parent_identity: tuple[int, int],
) -> None:
    """Quarantine only our original staging inode before recursive cleanup."""

    if (
        not _directory_identity_is_current(model_parent, model_parent_identity)
        or not _directory_identity_is_current(staging, staging_identity)
        or staging.parent != model_parent
    ):
        return
    quarantine = model_parent / f".cleanup-{uuid4().hex}"
    try:
        os.replace(staging, quarantine)
    except OSError:
        return
    if not _directory_identity_is_current(quarantine, staging_identity):
        return
    try:
        shutil.rmtree(quarantine)
    except OSError:
        return
