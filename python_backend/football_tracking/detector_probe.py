from __future__ import annotations

import errno
import hashlib
import math
import os
import platform
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from football_tracking.detector_audited_authority import (
    AUDITED_T2_LEGACY_PROBE_BINDINGS as _AUDITED_T2_LEGACY_REPORT_BINDINGS,
)
from football_tracking.detector_development_common import (
    CorruptProbeFrameError,
    DetectorDevelopmentError,
    ProbeWorkerDiedError,
    atomic_write_json,
    canonical_sha256,
    exact_regular_tree_snapshot,
    hash_regular_file,
    is_link_or_reparse,
    json_object_from_bytes,
    read_regular_bytes,
    regular_file_change_identity,
    require_safe_id,
    require_sha256,
    require_trusted_relative_path,
    secure_mkdirs,
    stat_token,
    utc_now_iso,
)
from football_tracking.detector_model_registry import build_builtin_model_catalog, find_model_and_profile
from football_tracking.detector_probe_runner import (
    ArtifactWriteError,
    probe_execution_environment,
    run_detector_probe,
)
from football_tracking.review_proxy_mapping import (
    ReviewProxyError,
    build_review_proxy_manifest,
    validate_review_proxy_manifest,
)

_TERMINAL_STATUSES = {"ready", "failed", "cancelled", "blocked"}
_ACTIVE_STATUSES = {"queued", "running", "committing"}
_MAX_DECODED_FRAME_BYTES = 1_200_000_000
_OUTPUT_CAPACITY_RESERVE_BYTES = 512 * 1024 * 1024
_MAX_WEIGHT_SNAPSHOT_BYTES = 256 * 1024 * 1024
_ARTIFACT_READ_CONCURRENCY = 3
_ARTIFACT_INDEX_CACHE_ENTRIES = 8
_ARTIFACT_READ_ACQUIRE_TIMEOUT_SECONDS = 5.0
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_CODE_BUNDLE_FILES = (
    "__init__.py",
    "ai_contracts.py",
    "ai_improvement_prompt_contract.py",
    "api/__init__.py",
    "api/schemas.py",
    "candidate_dataset.py",
    "config.py",
    "detector.py",
    "detector_candidate_contract.py",
    "detector_development_common.py",
    "detector_model_registry.py",
    "detector_probe.py",
    "detector_probe_runner.py",
    "detector_probe_worker.py",
    "media_integrity.py",
    "review_proxy_mapping.py",
    "tracking_contracts.py",
    "types.py",
)
_AUDITED_T2_CODE_BUNDLE_FILES = tuple(name for name in _CODE_BUNDLE_FILES if name != "review_proxy_mapping.py")
_AUDITED_T2_EXECUTION_BUNDLE_BINDINGS = {
    "fe8ffe2aca1d83f5b3ed19d6fff96999fa3224d3688147c23e1622fe58947494": {
        "code_bundle_sha256": "aa23d207df609403a6abde6de3435914ed4c27bc151498069c2821fa3a6ef5e1",
        "runtime_environment_sha256": "6ae75ef83b968801c2c0f16a628e9eba94fe88aed27384c2896d08c7e90054d3",
        "code_commit": "7b8d45e121184f6bebe22e9a1fed4c9dca8a3a50",
        "code_commit_blob_bundle_sha256": "4d381c6de23caa97e08fb6602e6876d425f2f16043d95aacb8b912eb4f59c59e",
    },
}
_CODE_BUNDLE_REPO_FILES = tuple(f"python_backend/football_tracking/{name}" for name in _CODE_BUNDLE_FILES)
_MAX_GIT_PROVENANCE_OUTPUT_BYTES = 64 * 1024
_MAX_CODE_BUNDLE_FILE_BYTES = 4 * 1024 * 1024
_CODE_COMMIT_BINDING_KIND = "exact_or_crlf_to_lf_commit_blob"
_RUNTIME_NAMES = ("sahi", "torch", "ultralytics")
_WORKER_DEADLINE_SECONDS = 20 * 60.0
_WORKER_HEARTBEAT_TIMEOUT_SECONDS = 10.0
_WORKER_CANCEL_GRACE_SECONDS = 1.0
_WORKER_TERMINATE_GRACE_SECONDS = 3.0
_WORKER_KILL_WAIT_SECONDS = 5.0
_WORKER_EXIT_ERROR_ENVELOPE_UNAVAILABLE = 75
_WORKER_EXIT_DISK_EXHAUSTED = 77
_WORKER_EXIT_CONTAINMENT_UNAVAILABLE = 78
_REQUEST_FIELDS = {
    "parent_trial_id",
    "source_id",
    "source_relative_path",
    "source_sha256",
    "tracking_contract_relative_path",
    "tracking_contract_sha256",
    "base_config_relative_path",
    "base_config_sha256",
    "effective_config_relative_path",
    "effective_config_sha256",
    "trial_intent_sha256",
    "tuning_patch_binding",
    "tuning_patch_sha256",
    "profile_ids",
    "frame_indices",
    "top_k",
    "requested_decode_mode",
    "retry_from_job_id",
    "annotation_sampling_manifest_sha256",
}

_SEMANTIC_INTENT_FIELDS = (
    "parent_trial_id",
    "source_id",
    "source_relative_path",
    "source_sha256",
    "source_file_identity_sha256",
    "source_size_bytes",
    "source_width",
    "source_height",
    "source_frame_count",
    "tracking_contract_relative_path",
    "tracking_contract_sha256",
    "base_config_relative_path",
    "base_config_sha256",
    "effective_config_relative_path",
    "effective_config_sha256",
    "trial_intent_sha256",
    "tuning_patch_binding",
    "tuning_patch_sha256",
    "profile_ids",
    "frozen_profiles_sha256",
    "profile_sha256s",
    "profile_bindings",
    "frame_indices",
    "top_k",
    "requested_decode_mode",
)


def semantic_probe_intent_sha256(frozen_request: dict[str, Any]) -> str:
    """Hash only detector evidence authority, never attempt/runtime mechanics.

    The explicit field allowlist is intentional: review-proxy upgrades may change
    the server-observed runtime and execution bundle, but no source, model,
    candidate-frame, sampling, or decode request authority.
    """

    missing = [field for field in _SEMANTIC_INTENT_FIELDS if field not in frozen_request]
    if missing:
        raise DetectorDevelopmentError(
            "invalid_probe_intent",
            f"Detector probe semantic intent is incomplete: {', '.join(missing)}",
        )
    payload = {
        "schema_version": "1.0",
        **{field: frozen_request[field] for field in _SEMANTIC_INTENT_FIELDS},
        "annotation_sampling_manifest_sha256": frozen_request.get("annotation_sampling_manifest_sha256"),
    }
    return canonical_sha256(payload)


def _review_proxy_continuation_execution_binding() -> dict[str, Any]:
    package_root = Path(__file__).resolve().parent
    continuation_names = (
        "api/routes/ball_annotations.py",
        "api/routes/detectors.py",
        "api/service.py",
        "ball_annotation_service.py",
        "ball_detector_annotations.py",
        "ball_detector_feasibility.py",
        "ball_frame_evidence.py",
        "detector_development.py",
    )
    # Child assembly calls the same freeze/catalog/report validators as a
    # normal current detector probe.  Bind that complete detector execution
    # closure, then add the annotation/API continuation surface.
    relative_names = tuple(dict.fromkeys((*_CODE_BUNDLE_FILES, *continuation_names)))
    code_files: dict[str, str] = {}
    for relative_name in relative_names:
        _content, digest = read_regular_bytes(
            package_root / relative_name,
            "review-proxy continuation code bundle file",
            max_bytes=_MAX_CODE_BUNDLE_FILE_BYTES,
            trusted_root=package_root,
        )
        code_files[f"football_tracking/{relative_name}"] = digest
    try:
        import fastapi
        import pydantic

        fastapi_version = str(fastapi.__version__)
        pydantic_version = str(pydantic.__version__)
    except (ImportError, AttributeError) as exc:
        raise DetectorDevelopmentError(
            "continuation_runtime_unavailable",
            "Review-proxy continuation runtime identity is unavailable",
        ) from exc
    runtime = {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "platform_system": platform.system(),
        "platform_machine": platform.machine(),
        "fastapi_version": fastapi_version,
        "pydantic_version": pydantic_version,
    }
    binding = {
        "schema_version": "1.0",
        "artifact_type": ("detector_review_proxy_continuation_execution_binding"),
        "code_files": code_files,
        "code_bundle_sha256": canonical_sha256(code_files),
        "runtime": runtime,
        "runtime_sha256": canonical_sha256(runtime),
    }
    binding["binding_sha256"] = canonical_sha256(binding)
    return binding


@dataclass(frozen=True)
class _CodeCommitBinding:
    worktree_files: dict[str, str]
    code_commit: str | None
    code_commit_status: str
    code_commit_reason: str | None
    code_commit_blob_files: dict[str, str] | None
    code_commit_blob_bundle_sha256: str | None
    code_commit_binding_kind: str | None


_ROOT_LOCKS_GUARD = threading.Lock()
_ROOT_LOCKS: dict[str, _RootRegistryLock] = {}
_ROOT_EXECUTION_LOCKS: dict[str, threading.Lock] = {}
_SOURCE_DIGEST_CACHE_LOCK = threading.RLock()
_SOURCE_DIGEST_CACHE: OrderedDict[tuple[str, str], tuple[str, int]] = OrderedDict()
_SOURCE_DIGEST_CACHE_ENTRIES = 8
_SOURCE_HASH_CHUNK_BYTES = 4 * 1024 * 1024


class _WorkerProcess:
    """One contained probe process tree and its private staging ownership."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        staging: Path,
        control: Path,
        worker_id: str,
    ) -> None:
        self.process = process
        self.staging = staging
        self.control = control
        self.worker_id = worker_id
        self.windows_job_handle: int | None = None
        self.parent_watch_write_fd: int | None = None
        self.containment_attached = False
        self.termination_lock = threading.Lock()
        self.quarantined = False


class _RootRegistryLock:
    """Re-entrant process lock backed by one fixed cross-process byte lock."""

    def __init__(self, path: Path) -> None:
        self._thread_lock = threading.RLock()
        self._path = path / "leases" / "registry.lock"
        self._local = threading.local()

    def __enter__(self):
        self._thread_lock.acquire()
        depth = int(getattr(self._local, "depth", 0))
        if depth:
            self._local.depth = depth + 1
            return self
        handle = None
        try:
            if is_link_or_reparse(self._path):
                raise DetectorDevelopmentError("unsafe_registry_lease", "Detector probe registry lease is unsafe")
            handle = self._path.open("a+b")
            _require_lock_file_identity(handle, self._path, "registry")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
                os.fsync(handle.fileno())
            while not _try_lock_handle(handle):
                threading.Event().wait(0.05)
            _require_lock_file_identity(handle, self._path, "registry")
            self._local.handle = handle
            self._local.depth = 1
            return self
        except Exception:
            if handle is not None:
                handle.close()
            self._thread_lock.release()
            raise

    def __exit__(self, exc_type, exc, traceback) -> None:
        depth = int(getattr(self._local, "depth", 0))
        try:
            if depth > 1:
                self._local.depth = depth - 1
                return
            handle = getattr(self._local, "handle", None)
            self._local.depth = 0
            self._local.handle = None
            if handle is not None:
                _unlock_handle(handle)
                handle.close()
        finally:
            self._thread_lock.release()


def _root_lock(path: Path) -> _RootRegistryLock:
    key = os.path.normcase(str(path))
    with _ROOT_LOCKS_GUARD:
        return _ROOT_LOCKS.setdefault(key, _RootRegistryLock(path))


def _root_execution_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path))
    with _ROOT_LOCKS_GUARD:
        return _ROOT_EXECUTION_LOCKS.setdefault(key, threading.Lock())


def _is_torch_cuda_out_of_memory(exc: BaseException) -> bool:
    exception_type = type(exc)
    return (
        exception_type.__name__ == "OutOfMemoryError"
        and exception_type.__module__ == "torch"
        and isinstance(exc, RuntimeError)
    )


def _source_file_identity(repo_root: Path, source_path: Path) -> tuple[str, int]:
    root = Path(os.path.abspath(repo_root))
    source = Path(os.path.abspath(source_path))
    try:
        relative_parent = source.parent.relative_to(root)
    except ValueError as exc:
        raise DetectorDevelopmentError(
            "path_outside_trusted_root",
            "Detector probe source escaped its trusted root",
        ) from exc

    def ancestors() -> tuple[tuple[str, tuple[int, int]], ...]:
        current = root
        captured = [(".", _stable_directory_object_identity(current))]
        for part in relative_parent.parts:
            current = current / part
            captured.append((current.relative_to(root).as_posix(), _stable_directory_object_identity(current)))
        return tuple(captured)

    before_ancestors = ancestors()
    file_identity = regular_file_change_identity(source, "detector probe source identity")
    if before_ancestors != ancestors():
        raise DetectorDevelopmentError(
            "source_changed",
            "Detector probe source ancestor changed during identity capture",
        )
    identity_sha256 = canonical_sha256(
        {
            "schema_version": "1.0",
            "file_identity": list(file_identity),
            "trusted_ancestor_identities": [
                {"relative_path": relative, "object_identity": list(identity)}
                for relative, identity in before_ancestors
            ],
        }
    )
    return identity_sha256, file_identity[2]


def _stable_directory_object_identity(path: Path) -> tuple[int, int]:
    if is_link_or_reparse(path):
        raise DetectorDevelopmentError("unsafe_path", "Detector probe source ancestor must not be a link")
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise DetectorDevelopmentError("path_unavailable", "Detector probe source ancestor is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise DetectorDevelopmentError("not_regular_directory", "Detector probe source ancestor is not a directory")
    return int(metadata.st_dev), int(metadata.st_ino)


def _path_entry_is_absent(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _verify_source_digest_cached(
    repo_root: Path,
    source_path: Path,
    *,
    declared_sha256: str,
    frozen_identity_sha256: str,
    frozen_size_bytes: int,
    should_cancel: Callable[[], bool],
    should_shutdown: Callable[[], bool],
) -> None:
    identity_sha256, size_bytes = _source_file_identity(repo_root, source_path)
    if identity_sha256 != frozen_identity_sha256 or size_bytes != frozen_size_bytes:
        raise DetectorDevelopmentError("source_changed", "Detector probe source identity changed after freeze")
    key = (
        os.path.normcase(os.path.abspath(source_path)),
        frozen_identity_sha256,
    )
    with _SOURCE_DIGEST_CACHE_LOCK:
        current_identity, current_size = _source_file_identity(repo_root, source_path)
        if current_identity != frozen_identity_sha256 or current_size != frozen_size_bytes:
            raise DetectorDevelopmentError("source_changed", "Detector probe source identity changed before hashing")
        cached = _SOURCE_DIGEST_CACHE.get(key)
        if cached is not None:
            _SOURCE_DIGEST_CACHE.move_to_end(key)
            digest, hashed_size = cached
            _raise_if_source_hash_aborted(should_cancel, should_shutdown)
        else:
            digest, hashed_size = _hash_source_file_cancellable(
                repo_root,
                source_path,
                frozen_identity_sha256=frozen_identity_sha256,
                frozen_size_bytes=frozen_size_bytes,
                should_cancel=should_cancel,
                should_shutdown=should_shutdown,
            )
            _raise_if_source_hash_aborted(should_cancel, should_shutdown)
            binding = (digest, hashed_size)
            _SOURCE_DIGEST_CACHE[key] = binding
            try:
                _raise_if_source_hash_aborted(should_cancel, should_shutdown)
            except DetectorDevelopmentError:
                if _SOURCE_DIGEST_CACHE.get(key) == binding:
                    _SOURCE_DIGEST_CACHE.pop(key, None)
                raise
            _SOURCE_DIGEST_CACHE.move_to_end(key)
            while len(_SOURCE_DIGEST_CACHE) > _SOURCE_DIGEST_CACHE_ENTRIES:
                _SOURCE_DIGEST_CACHE.popitem(last=False)
        if digest != declared_sha256:
            raise DetectorDevelopmentError(
                "source_digest_mismatch",
                "Detector probe source bytes do not match the declared source SHA-256",
            )


def _hash_source_file_cancellable(
    repo_root: Path,
    source_path: Path,
    *,
    frozen_identity_sha256: str,
    frozen_size_bytes: int,
    should_cancel: Callable[[], bool],
    should_shutdown: Callable[[], bool],
) -> tuple[str, int]:
    """Hash one frozen source while keeping cancellation and shutdown bounded."""

    _raise_if_source_hash_aborted(should_cancel, should_shutdown)
    initial_identity, initial_size = _source_file_identity(repo_root, source_path)
    if initial_identity != frozen_identity_sha256 or initial_size != frozen_size_bytes:
        raise DetectorDevelopmentError("source_changed", "Detector probe source identity changed before hashing")
    expected = regular_file_change_identity(source_path, "detector probe source")
    digest = hashlib.sha256()
    try:
        with source_path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or stat_token(opened) != expected[:5]:
                raise DetectorDevelopmentError("source_changed", "Detector probe source changed while it was opened")
            while True:
                _raise_if_source_hash_aborted(should_cancel, should_shutdown)
                chunk = _read_source_hash_chunk(handle)
                _raise_if_source_hash_aborted(should_cancel, should_shutdown)
                if not chunk:
                    break
                digest.update(chunk)
            if stat_token(os.fstat(handle.fileno())) != expected[:5]:
                raise DetectorDevelopmentError("source_changed", "Detector probe source changed while it was hashed")
    except DetectorDevelopmentError:
        raise
    except OSError as exc:
        raise DetectorDevelopmentError("path_unavailable", "Detector probe source could not be read") from exc
    final_identity, final_size = _source_file_identity(repo_root, source_path)
    if final_identity != frozen_identity_sha256 or final_size != frozen_size_bytes or final_size != expected[2]:
        raise DetectorDevelopmentError("source_changed", "Detector probe source changed while it was hashed")
    _raise_if_source_hash_aborted(should_cancel, should_shutdown)
    return digest.hexdigest(), final_size


def _raise_if_source_hash_aborted(should_cancel: Callable[[], bool], should_shutdown: Callable[[], bool]) -> None:
    if should_cancel():
        raise DetectorDevelopmentError("cancelled", "Detector probe was cancelled")
    if should_shutdown():
        raise DetectorDevelopmentError("service_shutting_down", "Detector probe service is shutting down")


def _read_source_hash_chunk(handle: Any) -> bytes:
    return handle.read(_SOURCE_HASH_CHUNK_BYTES)


def _validated_tuning_patch_binding(value: Any) -> dict[str, Any]:
    expected_fields = {
        "state",
        "schema_version",
        "version_id",
        "parent_version_id",
        "values_sha256",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected_fields
        or value.get("state") not in {"absent", "versioned"}
        or value.get("schema_version") != "1.0"
    ):
        raise DetectorDevelopmentError(
            "invalid_tuning_patch_binding",
            "Detector probe tuning-patch binding is invalid",
            status_code=400,
        )
    values_sha256 = require_sha256(value.get("values_sha256"), "tuning values_sha256")
    version_id = value.get("version_id")
    parent_version_id = value.get("parent_version_id")
    if value["state"] == "absent":
        if version_id is not None or parent_version_id is not None or values_sha256 != canonical_sha256({}):
            raise DetectorDevelopmentError(
                "invalid_tuning_patch_binding",
                "Absent detector tuning must bind the canonical empty patch",
                status_code=400,
            )
    elif (
        not isinstance(version_id, str)
        or not version_id.strip()
        or len(version_id) > 120
        or not (
            parent_version_id is None
            or (
                isinstance(parent_version_id, str) and bool(parent_version_id.strip()) and len(parent_version_id) <= 120
            )
        )
    ):
        raise DetectorDevelopmentError(
            "invalid_tuning_patch_binding",
            "Versioned detector tuning has an invalid version identity",
            status_code=400,
        )
    return deepcopy(value)


class DetectorProbeCoordinator:
    """Durable, bounded coordinator for source-bound detector comparisons."""

    def __init__(
        self,
        repo_root: Path,
        *,
        probe_runner: Callable[..., dict[str, Any]] | None = None,
        auto_start_workers: bool = True,
        catalog_provider: Callable[[], dict[str, Any]] | None = None,
        worker_deadline_seconds: float = _WORKER_DEADLINE_SECONDS,
        worker_heartbeat_timeout_seconds: float = _WORKER_HEARTBEAT_TIMEOUT_SECONDS,
        worker_command_factory: Callable[[Path, Path, int], list[str]] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve(strict=True)
        development = secure_mkdirs(self.repo_root, "data", "ball_detector_development_v1")
        self._probe_root = secure_mkdirs(development, "probes")
        self._jobs_root = secure_mkdirs(self._probe_root, "jobs")
        self._cancel_root = secure_mkdirs(self._probe_root, "cancel")
        self._results_root = secure_mkdirs(self._probe_root, "results")
        self._leases_root = secure_mkdirs(self._probe_root, "leases")
        self._review_proxy_child_claims_root = secure_mkdirs(self._probe_root, "review_proxy_child_claims")
        self._execution_lease_path = self._leases_root / "execution.lock"
        self._lock = _root_lock(self._probe_root)
        self._execution_lock = _root_execution_lock(self._probe_root)
        self._runner = probe_runner or run_detector_probe
        self._supervise_runner = probe_runner is None
        self._worker_deadline_seconds = max(0.1, float(worker_deadline_seconds))
        self._worker_heartbeat_timeout_seconds = max(0.1, float(worker_heartbeat_timeout_seconds))
        self._worker_command_factory = worker_command_factory or self._default_worker_command
        self._catalog_provider = catalog_provider or (lambda: build_builtin_model_catalog(self.repo_root))
        self._auto_start_workers = bool(auto_start_workers)
        self._owner_id = f"probe-owner-{uuid.uuid4().hex}"
        self._owner_lease_path = self._leases_root / f"{self._owner_id}.lock"
        self._owner_lease = self._acquire_owner_lease()
        self._closed = False
        self._jobs: dict[str, dict[str, Any]] = {}
        self._artifact_read_slots = threading.BoundedSemaphore(_ARTIFACT_READ_CONCURRENCY)
        self._artifact_cache_lock = threading.Lock()
        self._artifact_index_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._dispatch_event = threading.Event()
        self._shutdown_event = threading.Event()
        self._dispatcher: threading.Thread | None = None
        self._children_lock = threading.Lock()
        self._children: dict[str, _WorkerProcess] = {}
        self._active_executions_lock = threading.Lock()
        self._active_executions = 0
        self._executions_drained = threading.Event()
        self._executions_drained.set()
        self._quarantine_lock = threading.Lock()
        self._execution_quarantined = False
        self._quarantined_execution_lease: Any | None = None
        self._quarantine_child_exited = False
        self._quarantine_watchers = 0
        self._quarantine_cleanup_drained = threading.Event()
        self._quarantine_cleanup_drained.set()
        try:
            with self._lock:
                self._load_and_recover_jobs()
                recovered = [job_id for job_id, record in self._jobs.items() if record.get("status") == "queued"]
        except Exception:
            self._release_owner_lease()
            raise
        if self._auto_start_workers:
            self._start_dispatcher()
            if recovered:
                self._dispatch_event.set()

    def create_probe(
        self,
        request: dict[str, Any],
        *,
        _expected_profile_sha256s: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if self._closed:
            raise DetectorDevelopmentError("service_closed", "Detector probe service is closed")
        frozen_request, frozen_profiles = self._freeze_request(request)
        if _expected_profile_sha256s is not None and frozen_request["profile_sha256s"] != _expected_profile_sha256s:
            raise DetectorDevelopmentError(
                "invalid_annotation_check_authority",
                "Detector profiles changed after the annotation check was locked",
                status_code=409,
            )
        retry_from_job_id = frozen_request.get("retry_from_job_id")
        request_sha256 = canonical_sha256(frozen_request)
        intent_request = {key: value for key, value in frozen_request.items() if key != "retry_from_job_id"}
        intent_sha256 = canonical_sha256(intent_request)
        semantic_intent_sha256 = semantic_probe_intent_sha256(frozen_request)
        resource_sha256 = canonical_sha256(
            {
                key: frozen_request[key]
                for key in (
                    "parent_trial_id",
                    "source_id",
                    "source_sha256",
                    "source_file_identity_sha256",
                    "tracking_contract_sha256",
                    "base_config_relative_path",
                    "base_config_sha256",
                    "effective_config_relative_path",
                    "effective_config_sha256",
                    "trial_intent_sha256",
                    "tuning_patch_sha256",
                )
            }
        )
        with self._lock:
            self._refresh_jobs_from_disk()
            if retry_from_job_id is not None:
                previous = self._jobs.get(retry_from_job_id)
                if previous is None or previous.get("status") not in _TERMINAL_STATUSES:
                    raise DetectorDevelopmentError(
                        "invalid_retry",
                        "retry_from_job_id must identify a terminal detector probe",
                        status_code=400,
                    )
                if previous.get("intent_sha256") != intent_sha256:
                    raise DetectorDevelopmentError(
                        "retry_lineage_mismatch",
                        "A detector probe retry must preserve the exact frozen intent",
                        status_code=400,
                    )

            for record in self._jobs.values():
                if record.get("request_sha256") == request_sha256:
                    if record.get("status") in _ACTIVE_STATUSES or retry_from_job_id is not None:
                        return self._public_record(record)
                    break
            for record in self._jobs.values():
                if record.get("status") not in _ACTIVE_STATUSES:
                    continue
                if record.get("resource_sha256") == resource_sha256:
                    raise DetectorDevelopmentError(
                        "active_probe_conflict",
                        "A conflicting detector probe is already active for this frozen resource",
                    )

            if retry_from_job_id is None:
                if any(
                    record.get("intent_sha256") == intent_sha256 and record.get("status") in _TERMINAL_STATUSES
                    for record in self._jobs.values()
                ):
                    raise DetectorDevelopmentError(
                        "explicit_retry_required",
                        "The same detector probe already finished; provide retry_from_job_id to retry",
                    )

            job_id = f"probe-{request_sha256[:16]}-{uuid.uuid4().hex[:12]}"
            now = utc_now_iso()
            record: dict[str, Any] = {
                "schema_version": "1.0",
                "artifact_type": "detector_probe_job",
                "job_id": job_id,
                "idempotency_key": request_sha256,
                "request_sha256": request_sha256,
                "intent_sha256": intent_sha256,
                "semantic_intent_sha256": semantic_intent_sha256,
                "resource_sha256": resource_sha256,
                "frozen_profiles_sha256": frozen_request["frozen_profiles_sha256"],
                "status": "queued",
                "stage": "queued",
                "progress": {
                    "completed": 0,
                    "total": len(frozen_request["frame_indices"]) * len(frozen_profiles),
                    "updated_at": now,
                },
                "frozen_request": frozen_request,
                "frozen_profiles": frozen_profiles,
                "retry_from_job_id": retry_from_job_id,
                "owner_id": None,
                "cancel_requested": False,
                "error_code": None,
                "blocker_code": None,
                "recovery_action": None,
                "report": None,
                "result_manifest_sha256": None,
                "created_at": now,
                "updated_at": now,
            }
            self._persist_record(record)
            self._jobs[job_id] = record
            response = self._public_record(record)
        if self._auto_start_workers:
            self._start_worker(job_id)
        return response

    def get_probe(self, job_id: str) -> dict[str, Any]:
        job_id = require_safe_id(job_id, "detector probe job_id")
        with self._lock:
            record = self._record(job_id)
            return self._public_record(record)

    def get_verified_probe(self, job_id: str) -> dict[str, Any]:
        """Return a ready probe only after replaying its sealed result tree."""

        return self._public_record(self.get_verified_probe_job_record(job_id))

    def get_verified_probe_job_record(self, job_id: str) -> dict[str, Any]:
        """Return the exact persisted job record after replaying its sealed result tree."""

        job_id = require_safe_id(job_id, "detector probe job_id")
        with self._lock:
            record = deepcopy(self._record(job_id))
        if record.get("status") != "ready":
            return record
        report, manifest_sha256 = self._validate_result_tree(
            self._results_root / job_id,
            record,
        )
        if manifest_sha256 != record.get("result_manifest_sha256"):
            raise DetectorDevelopmentError(
                "result_manifest_mismatch",
                "Detector probe result manifest changed after publication",
            )
        if record.get("report") != report:
            raise DetectorDevelopmentError(
                "result_report_mismatch",
                "Detector probe job report differs from its sealed result tree",
            )
        with self._lock:
            repeated_record = deepcopy(self._record(job_id))
        if repeated_record != record:
            raise DetectorDevelopmentError(
                "source_changed",
                "Detector probe job record changed during sealed-result verification",
            )
        return record

    def get_review_proxy_upgrade_parent(self, job_id: str) -> dict[str, Any]:
        """Freeze the one audited legacy parent eligible for proxy repair."""

        job_id = require_safe_id(job_id, "review-proxy parent job_id")
        verified = self.get_verified_probe(job_id)
        report = verified.get("report")
        legacy = _AUDITED_T2_LEGACY_REPORT_BINDINGS.get(job_id)
        if (
            verified.get("status") != "ready"
            or not isinstance(report, dict)
            or legacy is None
            or report.get("report_sha256") != legacy["report_sha256"]
            or report.get("lineage", {}).get("execution_bundle_sha256") != legacy["execution_bundle_sha256"]
            or report.get("review_proxy_manifest") is not None
            or report.get("decode", {}).get("frame_timing_observations") is not None
            or any(
                not isinstance(frame, dict)
                or frame.get("decoder_reported_pos_msec") is not None
                or frame.get("decoder_timing_observation_method") is not None
                for frame in report.get("frames", [])
            )
        ):
            raise DetectorDevelopmentError(
                "review_proxy_repair_ineligible",
                "Review-proxy repair requires the exact audited legacy detector probe",
                status_code=409,
            )
        job_path = self._jobs_root / f"{job_id}.json"
        _, parent_record_sha256 = read_regular_bytes(
            job_path,
            "review-proxy parent detector job",
            max_bytes=4 * 1024 * 1024,
            trusted_root=self._jobs_root,
        )
        frozen = verified["frozen_request"]
        candidate_payload = []
        source_artifact_payload = []
        for frame in report["frames"]:
            source_artifact_payload.append(
                {
                    "frame_index": frame["frame_index"],
                    "source_artifact_url": frame["source_artifact_url"],
                    "source_frame_sha256": frame["source_frame_sha256"],
                    "source_frame_size_bytes": frame["source_frame_size_bytes"],
                }
            )
            candidate_payload.append(
                {
                    "frame_index": frame["frame_index"],
                    "profile_results": deepcopy(frame["profile_results"]),
                }
            )
        return {
            "parent_probe_job_id": job_id,
            "parent_probe_request_sha256": verified["request_sha256"],
            "parent_probe_intent_sha256": verified["intent_sha256"],
            "parent_probe_semantic_intent_sha256": semantic_probe_intent_sha256(frozen),
            "parent_probe_report_sha256": report["report_sha256"],
            "parent_probe_result_manifest_sha256": verified["result_manifest_sha256"],
            "parent_probe_record_sha256": parent_record_sha256,
            "parent_execution_bundle_sha256": frozen["execution_bundle_sha256"],
            "parent_runtime_environment_sha256": frozen["runtime_environment_sha256"],
            "source_frame_evidence_sha256": canonical_sha256(source_artifact_payload),
            "candidate_evidence_sha256": canonical_sha256(candidate_payload),
            "source": deepcopy(report["source"]),
            "report_decode_fps": float(report["decode"]["fps"]),
            "report_effective_decode_mode": report["decode"]["effective_decode_mode"],
            "frame_indices": deepcopy(frozen["frame_indices"]),
            "frozen_request": deepcopy(frozen),
            "frozen_profiles": deepcopy(verified["frozen_profiles"]),
        }

    def get_review_proxy_upgrade_child(self, parent_job_id: str) -> dict[str, Any] | None:
        """Return the sole durable proxy-upgrade child for a parent, if any."""

        parent_job_id = require_safe_id(parent_job_id, "review-proxy parent probe job_id")
        with self._lock:
            self._refresh_jobs_from_disk()
            matches = [
                deepcopy(record)
                for record in self._jobs.values()
                if record.get("retry_from_job_id") == parent_job_id
                and record.get("retry_kind") == "review_proxy_decode_upgrade"
                and isinstance(
                    record.get("frozen_request", {}).get("review_proxy_upgrade"),
                    dict,
                )
            ]
            claim = self._read_review_proxy_child_claim(parent_job_id)
            if claim is not None:
                child = self._claimed_review_proxy_child_record(claim)
                if any(record.get("job_id") != child.get("job_id") for record in matches):
                    raise DetectorDevelopmentError(
                        "duplicate_review_proxy_child",
                        "An audited detector probe has multiple review-proxy children",
                    )
                matches = [child]
        if len(matches) > 1:
            raise DetectorDevelopmentError(
                "duplicate_review_proxy_child",
                "An audited detector probe has multiple review-proxy children",
            )
        if not matches:
            return None
        child = matches[0]
        if child.get("status") == "ready":
            return self.get_verified_probe(child["job_id"])
        return self._public_record(child)

    def review_proxy_upgrade_child_plan(self, parent_job_id: str, *, repair_evidence: dict[str, Any]) -> dict[str, Any]:
        """Freeze the exact current-bundle child plan before continuation intent."""

        return deepcopy(
            self._prepare_review_proxy_upgrade_child(parent_job_id, repair_evidence=repair_evidence)["child_plan"]
        )

    def _prepare_review_proxy_upgrade_child(
        self, parent_job_id: str, *, repair_evidence: dict[str, Any]
    ) -> dict[str, Any]:
        if self._closed:
            raise DetectorDevelopmentError("service_closed", "Detector probe service is closed")
        parent = self.get_review_proxy_upgrade_parent(parent_job_id)
        parent_job_id = parent["parent_probe_job_id"]
        from football_tracking.api.schemas import (
            DetectorProbeRepairEvidenceView,
            DetectorProbeReviewProxyUpgradeBindingView,
        )

        try:
            normalized_repair = DetectorProbeRepairEvidenceView.model_validate(repair_evidence).model_dump(mode="json")
        except Exception as exc:
            raise DetectorDevelopmentError(
                "invalid_review_proxy_repair_evidence",
                "Review-proxy repair evidence is incomplete or inconsistent",
            ) from exc
        request = {
            key: deepcopy(parent["frozen_request"][key])
            for key in _REQUEST_FIELDS
            if key in parent["frozen_request"] and key != "retry_from_job_id"
        }
        request["retry_from_job_id"] = parent_job_id
        frozen, frozen_profiles = self._freeze_request(request)
        semantic_intent_sha256 = semantic_probe_intent_sha256(frozen)
        if semantic_intent_sha256 != parent["parent_probe_semantic_intent_sha256"]:
            raise DetectorDevelopmentError(
                "retry_lineage_mismatch",
                "Review-proxy child changed detector semantic intent",
            )
        continuation_binding = _review_proxy_continuation_execution_binding()
        upgrade_without_digest = {
            "schema_version": "1.0",
            "retry_kind": "review_proxy_decode_upgrade",
            "inherited_evidence": {
                key: deepcopy(parent[key])
                for key in (
                    "parent_probe_job_id",
                    "parent_probe_request_sha256",
                    "parent_probe_intent_sha256",
                    "parent_probe_semantic_intent_sha256",
                    "parent_probe_report_sha256",
                    "parent_probe_result_manifest_sha256",
                    "parent_probe_record_sha256",
                    "parent_execution_bundle_sha256",
                    "parent_runtime_environment_sha256",
                    "source_frame_evidence_sha256",
                    "candidate_evidence_sha256",
                )
            },
            "repair_evidence": normalized_repair,
            "continuation_execution_binding": continuation_binding,
        }
        try:
            upgrade = DetectorProbeReviewProxyUpgradeBindingView.model_validate(
                {
                    **upgrade_without_digest,
                    "binding_sha256": canonical_sha256(upgrade_without_digest),
                }
            ).model_dump(mode="json")
        except Exception as exc:
            raise DetectorDevelopmentError(
                "invalid_review_proxy_upgrade_binding",
                "Review-proxy child provenance binding is invalid",
            ) from exc
        frozen["retry_kind"] = "review_proxy_decode_upgrade"
        frozen["review_proxy_upgrade"] = upgrade
        request_sha256 = canonical_sha256(frozen)
        intent_sha256 = canonical_sha256({key: value for key, value in frozen.items() if key != "retry_from_job_id"})
        resource_sha256 = canonical_sha256(
            {
                key: frozen[key]
                for key in (
                    "parent_trial_id",
                    "source_id",
                    "source_sha256",
                    "source_file_identity_sha256",
                    "tracking_contract_sha256",
                    "base_config_relative_path",
                    "base_config_sha256",
                    "effective_config_relative_path",
                    "effective_config_sha256",
                    "trial_intent_sha256",
                    "tuning_patch_sha256",
                )
            }
        )
        child_plan_body = {
            "schema_version": "1.0",
            "artifact_type": "detector_review_proxy_child_plan",
            "parent_probe_job_id": parent_job_id,
            "repair_id": normalized_repair["repair_id"],
            "request_sha256": request_sha256,
            "intent_sha256": intent_sha256,
            "semantic_intent_sha256": semantic_intent_sha256,
            "resource_sha256": resource_sha256,
            "execution_bundle_sha256": frozen["execution_bundle_sha256"],
            "runtime_environment_sha256": frozen["runtime_environment_sha256"],
            "frozen_profiles_sha256": frozen["frozen_profiles_sha256"],
            "continuation_execution_binding": continuation_binding,
        }
        return {
            "parent": parent,
            "normalized_repair": normalized_repair,
            "frozen": frozen,
            "frozen_profiles": frozen_profiles,
            "semantic_intent_sha256": semantic_intent_sha256,
            "request_sha256": request_sha256,
            "intent_sha256": intent_sha256,
            "resource_sha256": resource_sha256,
            "child_plan": {
                **child_plan_body,
                "plan_sha256": canonical_sha256(child_plan_body),
            },
        }

    def create_review_proxy_upgrade_child(
        self,
        parent_job_id: str,
        *,
        repair_evidence: dict[str, Any],
        proxy_media: dict[str, Any],
        proxy_sample_bytes: dict[int, bytes],
        expected_child_plan: dict[str, Any],
    ) -> dict[str, Any]:
        """Publish a current-bundle child without rerunning detector inference.

        The historical source frames, candidate rows, and overlays are copied
        byte-for-byte from the exact audited parent.  Only independently
        verified review-proxy JPEGs and their mapping are new evidence.
        """

        prepared = self._prepare_review_proxy_upgrade_child(parent_job_id, repair_evidence=repair_evidence)
        if expected_child_plan != prepared["child_plan"]:
            raise DetectorDevelopmentError(
                "continuation_child_plan_changed",
                "Current continuation bundle differs from the frozen child plan",
                status_code=409,
            )
        parent = prepared["parent"]
        parent_job_id = parent["parent_probe_job_id"]
        parent_record_sha256 = parent["parent_probe_record_sha256"]
        normalized_repair = prepared["normalized_repair"]
        frozen = prepared["frozen"]
        frozen_profiles = prepared["frozen_profiles"]
        semantic_intent_sha256 = prepared["semantic_intent_sha256"]
        request_sha256 = prepared["request_sha256"]
        intent_sha256 = prepared["intent_sha256"]
        resource_sha256 = prepared["resource_sha256"]
        expected_indices = parent["frame_indices"]
        expected_sample_sha256s = {
            str(index): hashlib.sha256(content).hexdigest()
            for index, content in sorted(proxy_sample_bytes.items())
            if isinstance(index, int) and isinstance(content, bytes)
        }
        if (
            sorted(proxy_sample_bytes) != expected_indices
            or normalized_repair["sampled_frame_sha256s"] != expected_sample_sha256s
        ):
            raise DetectorDevelopmentError(
                "review_proxy_sample_mismatch",
                "Review-proxy samples do not bind the exact inherited frame set",
            )
        normalized_proxy = self._validated_review_proxy_media(proxy_media, parent=parent)

        repair_identity = canonical_sha256(
            {
                "request_sha256": request_sha256,
                "repair_id": normalized_repair["repair_id"],
            }
        )
        job_id = f"probe-{request_sha256[:16]}-{repair_identity[:12]}"

        with self._execution_lock:
            with self._lock:
                self._refresh_jobs_from_disk()
                existing_children = [
                    existing
                    for existing in self._jobs.values()
                    if existing.get("retry_from_job_id") == parent_job_id
                    and existing.get("retry_kind") == "review_proxy_decode_upgrade"
                    and isinstance(
                        existing.get("frozen_request", {}).get("review_proxy_upgrade"),
                        dict,
                    )
                ]
                claim = self._read_review_proxy_child_claim(parent_job_id)
                if claim is not None:
                    claimed_job_path = self._jobs_root / f"{claim['child_probe_job_id']}.json"
                    exact_unmaterialized_reservation = bool(
                        claim.get("child_probe_job_id") == job_id
                        and claim.get("repair_id") == normalized_repair["repair_id"]
                        and claim.get("child_request_sha256") == request_sha256
                        and not claimed_job_path.exists()
                        and not is_link_or_reparse(claimed_job_path)
                        and not (self._results_root / job_id).exists()
                        and not is_link_or_reparse(self._results_root / job_id)
                    )
                    if exact_unmaterialized_reservation:
                        if existing_children:
                            raise DetectorDevelopmentError(
                                "duplicate_review_proxy_child",
                                "An audited detector probe has multiple review-proxy children",
                            )
                        # A process may die after the immutable reservation is
                        # published but before its deterministic job row is
                        # materialized. Only the exact reserved plan may fill
                        # that empty slot; a malformed row or any result tree
                        # remains fail-closed.
                        existing_children = []
                    else:
                        claimed_child = self._claimed_review_proxy_child_record(claim)
                        if any(existing.get("job_id") != claimed_child.get("job_id") for existing in existing_children):
                            raise DetectorDevelopmentError(
                                "duplicate_review_proxy_child",
                                "An audited detector probe has multiple review-proxy children",
                            )
                        existing_children = [claimed_child]
                elif existing_children:
                    if len(existing_children) > 1:
                        raise DetectorDevelopmentError(
                            "duplicate_review_proxy_child",
                            "An audited detector probe has multiple review-proxy children",
                        )
                    existing_upgrade = existing_children[0]["frozen_request"]["review_proxy_upgrade"]
                    self._publish_review_proxy_child_claim(
                        parent_job_id=parent_job_id,
                        child_job_id=existing_children[0]["job_id"],
                        repair_id=existing_upgrade["repair_evidence"]["repair_id"],
                        child_request_sha256=existing_children[0]["request_sha256"],
                    )
                if len(existing_children) > 1:
                    raise DetectorDevelopmentError(
                        "duplicate_review_proxy_child",
                        "An audited detector probe has multiple review-proxy children",
                    )
                for existing in existing_children:
                    existing_upgrade = existing.get("frozen_request", {}).get("review_proxy_upgrade", {})
                    if (
                        existing.get("retry_from_job_id") == parent_job_id
                        and existing_upgrade.get("repair_evidence", {}).get("repair_id")
                        == normalized_repair["repair_id"]
                    ):
                        if (
                            existing.get("request_sha256") != request_sha256
                            or existing.get("frozen_request") != frozen
                            or existing.get("frozen_profiles") != frozen_profiles
                        ):
                            raise DetectorDevelopmentError(
                                "review_proxy_child_plan_conflict",
                                "The repair already has a child with a different frozen plan",
                                status_code=409,
                            )
                        if existing.get("status") == "ready":
                            return self.get_verified_probe(existing["job_id"])
                        if existing.get("status") in {"running", "committing"}:
                            raise DetectorDevelopmentError(
                                "review_proxy_child_in_progress",
                                "The exact review-proxy child is already being committed",
                                status_code=409,
                            )
                        if existing.get("status") in {
                            "failed",
                            "blocked",
                            "cancelled",
                        }:
                            raise DetectorDevelopmentError(
                                "review_proxy_child_terminal",
                                "The terminal review-proxy child is immutable; retry the repair as a new attempt",
                                status_code=409,
                            )
                    raise DetectorDevelopmentError(
                        "review_proxy_parent_child_exists",
                        "The audited parent already has a child from another repair attempt",
                        status_code=409,
                    )
                # The continuation transaction may be interrupted after its
                # durable intent is written but before (or while) this record
                # is assembled.  Bind the child identity to the exact frozen
                # request and repair instead of allocating a fresh UUID on
                # every retry, so restart/repeated POST converges on one child.
                self._publish_review_proxy_child_claim(
                    parent_job_id=parent_job_id,
                    child_job_id=job_id,
                    repair_id=normalized_repair["repair_id"],
                    child_request_sha256=request_sha256,
                )
                now = utc_now_iso()
                record: dict[str, Any] = {
                    "schema_version": "1.0",
                    "artifact_type": "detector_probe_job",
                    "job_id": job_id,
                    "idempotency_key": request_sha256,
                    "request_sha256": request_sha256,
                    "intent_sha256": intent_sha256,
                    "semantic_intent_sha256": semantic_intent_sha256,
                    "resource_sha256": resource_sha256,
                    "frozen_profiles_sha256": frozen["frozen_profiles_sha256"],
                    "status": "running",
                    "stage": "assembling_review_proxy_child",
                    "progress": {
                        "completed": 0,
                        "total": len(expected_indices) * len(frozen_profiles),
                        "updated_at": now,
                    },
                    "frozen_request": frozen,
                    "frozen_profiles": frozen_profiles,
                    "retry_from_job_id": parent_job_id,
                    "retry_kind": "review_proxy_decode_upgrade",
                    "owner_id": self._owner_id,
                    "cancel_requested": False,
                    "error_code": None,
                    "blocker_code": None,
                    "recovery_action": None,
                    "report": None,
                    "result_manifest_sha256": None,
                    "created_at": now,
                    "updated_at": now,
                }
                self._persist_record(record)
                self._jobs[job_id] = record

            staging: Path | None = None
            published = False
            destination = self._results_root / job_id
            try:
                staging = secure_mkdirs(
                    self._results_root,
                    f".{job_id}.staging-{uuid.uuid4().hex}",
                )
                runner_output = self._assemble_review_proxy_child_output(
                    job_id=job_id,
                    parent=parent,
                    child_frozen=frozen,
                    repair_evidence=normalized_repair,
                    proxy_media=normalized_proxy,
                    proxy_sample_bytes=proxy_sample_bytes,
                    staging=staging,
                )
                report, manifest = self._build_result(record, runner_output, staging)
                atomic_write_json(
                    staging / "detector_probe_report.v1.json",
                    report,
                    trusted_root=staging,
                )
                report_file_sha256, report_file_size = hash_regular_file(
                    staging / "detector_probe_report.v1.json",
                    "review-proxy child detector report",
                    trusted_root=staging,
                )
                manifest["report_file_sha256"] = report_file_sha256
                manifest["report_file_size_bytes"] = report_file_size
                atomic_write_json(
                    staging / "detector_probe_manifest.v1.json",
                    manifest,
                    trusted_root=staging,
                )
                manifest_sha256, _ = hash_regular_file(
                    staging / "detector_probe_manifest.v1.json",
                    "review-proxy child detector manifest",
                    trusted_root=staging,
                )
                self._validate_result_tree(staging, record)
                with self._lock:
                    current = self._record(job_id)
                    current.update(
                        status="committing",
                        stage="committing",
                        owner_id=self._owner_id,
                        updated_at=utc_now_iso(),
                    )
                    current["progress"].update(
                        completed=current["progress"]["total"],
                        updated_at=current["updated_at"],
                    )
                    self._persist_record(current)
                try:
                    _publish_staging_directory(staging, destination)
                finally:
                    # os.replace is the immutable commit point.  Even if the
                    # helper raises during its post-rename identity check, a
                    # consumed staging path means the destination must never
                    # be treated as an unpublished tree and deleted.
                    if staging is not None and not staging.exists():
                        published = True
                        staging = None
                committed_report, committed_manifest_sha256 = self._validate_result_tree(destination, record)
                if committed_manifest_sha256 != manifest_sha256:
                    raise DetectorDevelopmentError(
                        "committed_manifest_mismatch",
                        "Review-proxy child changed during atomic publication",
                    )
                current_parent = self.get_review_proxy_upgrade_parent(parent_job_id)
                if current_parent["parent_probe_record_sha256"] != parent_record_sha256:
                    raise DetectorDevelopmentError(
                        "historical_parent_changed",
                        "Historical detector probe changed during proxy continuation",
                    )
                with self._lock:
                    current = self._record(job_id)
                    current.update(
                        status="ready",
                        stage="ready",
                        owner_id=None,
                        cancel_requested=False,
                        error_code=None,
                        blocker_code=None,
                        recovery_action=None,
                        report=committed_report,
                        result_manifest_sha256=committed_manifest_sha256,
                        updated_at=utc_now_iso(),
                    )
                    current["progress"].update(
                        completed=current["progress"]["total"],
                        updated_at=current["updated_at"],
                    )
                    self._persist_record(current)
                    return self._public_record(current)
            except Exception as exc:
                if not published:
                    self._remove_tree(destination)
                    self._record_failure(job_id, exc)
                # Once atomically published, leave the record committing. The
                # normal startup recovery validates and finalizes it; it must
                # never delete a complete destination after the commit point.
                raise
            finally:
                if staging is not None:
                    self._remove_tree(staging)

    @staticmethod
    def _validated_review_proxy_media(value: dict[str, Any], *, parent: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != {
            "sha256",
            "size_bytes",
            "width",
            "height",
            "frame_count",
            "fps",
        }:
            raise DetectorDevelopmentError("invalid_review_proxy_media", "Review-proxy media binding is invalid")
        normalized = {
            "sha256": require_sha256(value.get("sha256"), "review proxy sha256"),
            "size_bytes": value.get("size_bytes"),
            "width": value.get("width"),
            "height": value.get("height"),
            "frame_count": value.get("frame_count"),
            "fps": value.get("fps"),
        }
        parent_frozen = parent["frozen_request"]
        if (
            isinstance(normalized["size_bytes"], bool)
            or not isinstance(normalized["size_bytes"], int)
            or normalized["size_bytes"] <= 0
            or normalized["width"] != 2560
            or normalized["height"] != 720
            or normalized["frame_count"] != parent_frozen["source_frame_count"]
            or isinstance(normalized["fps"], bool)
            or not isinstance(normalized["fps"], (int, float))
            or not math.isclose(
                float(normalized["fps"]),
                float(parent["report_decode_fps"]),
                rel_tol=0.0,
                abs_tol=1e-6,
            )
        ):
            raise DetectorDevelopmentError(
                "invalid_review_proxy_media",
                "Review-proxy media does not preserve the exact source timeline",
            )
        normalized["fps"] = float(normalized["fps"])
        return normalized

    def _assemble_review_proxy_child_output(
        self,
        *,
        job_id: str,
        parent: dict[str, Any],
        child_frozen: dict[str, Any],
        repair_evidence: dict[str, Any],
        proxy_media: dict[str, Any],
        proxy_sample_bytes: dict[int, bytes],
        staging: Path,
    ) -> dict[str, Any]:
        del job_id
        parent_job_id = parent["parent_probe_job_id"]
        verified_parent = self.get_verified_probe(parent_job_id)
        parent_report = verified_parent["report"]
        frame_root = secure_mkdirs(staging, "frames")
        overlay_root = secure_mkdirs(staging, "overlays")
        proxy_root = secure_mkdirs(staging, "proxy_frames")
        fps = float(parent["report_decode_fps"])
        mappings: list[dict[str, Any]] = []
        runner_frames: list[dict[str, Any]] = []
        parent_prefix = f"/api/v1/detector-probes/{parent_job_id}/artifacts/"

        for frame in parent_report["frames"]:
            frame_index = frame["frame_index"]
            source_artifact_id = self._artifact_id_from_bound_url(frame["source_artifact_url"], parent_prefix)
            source_bytes, source_media_type, source_digest = self.read_probe_artifact(parent_job_id, source_artifact_id)
            if source_media_type != "image/jpeg" or source_digest != frame["source_frame_sha256"]:
                raise DetectorDevelopmentError(
                    "historical_parent_changed",
                    "Inherited source-frame evidence changed before continuation",
                )
            source_relative = f"frames/frame_{frame_index:010d}.jpg"
            self._write_exact_child_bytes(
                frame_root / f"frame_{frame_index:010d}.jpg",
                source_bytes,
                staging,
            )

            proxy_bytes = proxy_sample_bytes[frame_index]
            proxy_digest = hashlib.sha256(proxy_bytes).hexdigest()
            if proxy_digest != repair_evidence["sampled_frame_sha256s"][str(frame_index)]:
                raise DetectorDevelopmentError(
                    "review_proxy_sample_mismatch",
                    "Review-proxy sampled JPEG changed before child publication",
                )
            proxy_relative = f"proxy_frames/frame_{frame_index:010d}.jpg"
            self._write_exact_child_bytes(
                proxy_root / f"frame_{frame_index:010d}.jpg",
                proxy_bytes,
                staging,
            )

            inherited_profiles: list[dict[str, Any]] = []
            for profile in frame["profile_results"]:
                profile_id = profile["profile_id"]
                overlay_artifact_id = self._artifact_id_from_bound_url(
                    profile["raw_overlay_artifact_url"], parent_prefix
                )
                overlay_bytes, overlay_media_type, overlay_digest = self.read_probe_artifact(
                    parent_job_id, overlay_artifact_id
                )
                if overlay_media_type != "image/jpeg" or overlay_digest != profile["raw_overlay_sha256"]:
                    raise DetectorDevelopmentError(
                        "historical_parent_changed",
                        "Inherited detector overlay changed before continuation",
                    )
                overlay_relative = f"overlays/frame_{frame_index:010d}_{profile_id}.jpg"
                self._write_exact_child_bytes(
                    overlay_root / f"frame_{frame_index:010d}_{profile_id}.jpg",
                    overlay_bytes,
                    staging,
                )
                inherited = deepcopy(profile)
                inherited.pop("raw_overlay_artifact_url", None)
                inherited.pop("raw_overlay_sha256", None)
                inherited.pop("raw_overlay_size_bytes", None)
                inherited["raw_overlay_relative_path"] = overlay_relative
                inherited_profiles.append(inherited)

            proxy_mapped_msec = frame_index / fps * 1000.0
            mappings.append(
                {
                    "source_frame_index": frame_index,
                    "source_timing_status": "not_collected",
                    "source_decoder_pos_msec": None,
                    "proxy_frame_index": frame_index,
                    "proxy_timing_basis": "verified_cfr_frame_index_time_v1",
                    "proxy_cfr_time_msec": proxy_mapped_msec,
                    "source_frame_sha256": source_digest,
                    "proxy_frame_sha256": proxy_digest,
                    "media_integrity": {
                        "status": "ok",
                        "gray": False,
                        "low_information": False,
                        "likely_corrupt": False,
                    },
                }
            )
            runner_frames.append(
                {
                    "frame_index": frame_index,
                    "source_frame_relative_path": source_relative,
                    "proxy_frame_relative_path": proxy_relative,
                    "requested_decode_mode": child_frozen["requested_decode_mode"],
                    "effective_decode_mode": parent["report_effective_decode_mode"],
                    "decoded_frame_position": frame_index,
                    "decoder_reported_pos_msec": None,
                    "decoder_timing_observation_method": None,
                    "media_integrity": deepcopy(frame["media_integrity"]),
                    "profile_results": inherited_profiles,
                }
            )

        manifest = build_review_proxy_manifest(
            source={
                "sha256": child_frozen["source_sha256"],
                "file_identity_sha256": child_frozen["source_file_identity_sha256"],
                "size_bytes": child_frozen["source_size_bytes"],
                "width": child_frozen["source_width"],
                "height": child_frozen["source_height"],
                "fps": fps,
                "frame_count": child_frozen["source_frame_count"],
                "codec": "hevc",
            },
            proxy={
                **proxy_media,
                "codec": "h264",
            },
            mappings=mappings,
            expected_frame_indices=parent["frame_indices"],
            decoder_fingerprint_sha256=repair_evidence["repair_decoder_fingerprint_sha256"],
            requested_decode_mode=child_frozen["requested_decode_mode"],
            effective_decode_mode=parent["report_effective_decode_mode"],
            map_time_tolerance_msec=0.1,
            declared_offset_msec=0.0,
        )
        execution_environment = child_frozen["execution_bundle"]["execution_environment"]
        return {
            "frames": runner_frames,
            "decode": {
                "width": child_frozen["source_width"],
                "height": child_frozen["source_height"],
                "frame_count": child_frozen["source_frame_count"],
                "fps": fps,
                "requested_decode_mode": child_frozen["requested_decode_mode"],
                "effective_decode_mode": parent["report_effective_decode_mode"],
                "verified_frame_indices": parent["frame_indices"],
                "position_verification": ("verified_review_proxy_frame_index_mapping_v1"),
                "frame_timing_observations": None,
            },
            "review_proxy_manifest": manifest,
            "execution": {
                "device": execution_environment["device"],
                "precision": execution_environment["precision"],
            },
        }

    @staticmethod
    def _artifact_id_from_bound_url(value: Any, prefix: str) -> str:
        if not isinstance(value, str) or not value.startswith(prefix):
            raise DetectorDevelopmentError(
                "historical_parent_changed",
                "Historical detector artifact URL is not canonically bound",
            )
        return require_safe_id(value[len(prefix) :], "inherited artifact_id")

    @staticmethod
    def _write_exact_child_bytes(path: Path, content: bytes, root: Path) -> None:
        if path.parent.parent != root or is_link_or_reparse(path.parent):
            raise DetectorDevelopmentError("unsafe_result_tree", "Review-proxy child evidence path is unsafe")
        try:
            with path.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise DetectorDevelopmentError(
                "artifact_write_failed",
                "Review-proxy child evidence could not be written",
            ) from exc

    def cancel_probe(self, job_id: str) -> dict[str, Any]:
        job_id = require_safe_id(job_id, "detector probe job_id")
        with self._lock:
            record = self._record(job_id)
            status = record.get("status")
            if status == "committing":
                raise DetectorDevelopmentError(
                    "commit_in_progress",
                    "Detector probe commit has started and can no longer be cancelled",
                )
            if status in _TERMINAL_STATUSES:
                return self._public_record(record)
            record["cancel_requested"] = True
            record["updated_at"] = utc_now_iso()
            if status == "queued":
                record.update(
                    {
                        "status": "cancelled",
                        "stage": "cancelled",
                        "owner_id": None,
                        "error_code": "cancelled",
                        "recovery_action": "retry",
                    }
                )
            self._persist_record(record)
            return self._public_record(record)

    def execute_probe(self, job_id: str) -> None:
        # A single detector probe can retain up to fifty full-resolution source
        # frames. Serialize every execution for one development root, including
        # manual execution and multiple coordinator instances in this process.
        job_id = require_safe_id(job_id, "detector probe job_id")
        self._execute_in_global_slot(job_id)

    def _execute_in_global_slot(self, requested_job_id: str | None) -> bool:
        self._begin_active_execution()
        try:
            with self._execution_lock:
                execution_lease = self._acquire_execution_lease()
                try:
                    self._recover_orphaned_active_jobs()
                    claimed = self._claim_probe(requested_job_id)
                    if claimed is None:
                        return False
                    job_id, record = claimed
                    self._execute_probe_serial(job_id, record)
                    return True
                finally:
                    if not self._retain_quarantined_execution_lease(execution_lease):
                        _unlock_handle(execution_lease)
                        execution_lease.close()
        finally:
            self._end_active_execution()

    def _claim_probe(self, requested_job_id: str | None) -> tuple[str, dict[str, Any]] | None:
        with self._lock:
            if self._closed:
                raise DetectorDevelopmentError("service_closed", "Detector probe service is closed")
            self._refresh_jobs_from_disk()
            if requested_job_id is None:
                queued = sorted(
                    (record for record in self._jobs.values() if record.get("status") == "queued"),
                    key=lambda record: (
                        str(record.get("created_at")),
                        str(record.get("job_id")),
                    ),
                )
                if not queued:
                    return None
                record = queued[0]
                job_id = str(record["job_id"])
            else:
                job_id = require_safe_id(requested_job_id, "detector probe job_id")
                record = self._record(job_id)
            if record.get("status") in _TERMINAL_STATUSES | {"committing"}:
                return None
            if record.get("status") == "running":
                if record.get("owner_id") == self._owner_id:
                    return None
                raise DetectorDevelopmentError(
                    "probe_already_running", "Detector probe is already owned by another worker"
                )
            if record.get("status") != "queued":
                return None
            if record.get("cancel_requested") is True:
                self._mark_cancelled(record)
                return None
            record.update(
                {
                    "status": "running",
                    "stage": "preparing",
                    "owner_id": self._owner_id,
                    "updated_at": utc_now_iso(),
                }
            )
            record["progress"]["updated_at"] = record["updated_at"]
            self._persist_record(record)
            return job_id, deepcopy(record)

    def _execute_probe_serial(self, job_id: str, record: dict[str, Any]) -> None:
        staging: Path | None = None
        published = False
        destination = self._results_root / job_id
        try:
            self._set_running_stage(job_id, "verifying_source")
            execution_request = self._execution_request(record, verify_source_digest=True)
            self._ensure_probe_capacity(record)
            staging_name = f".{job_id}.staging-{uuid.uuid4().hex}"
            staging = secure_mkdirs(self._results_root, staging_name)
            self._set_running_stage(job_id, "inference")
            runtime_dir, weight_snapshots = self._snapshot_weights(record, staging)
            execution_request["_runtime_weights_root"] = str(runtime_dir)
            execution_request["_weight_snapshot_paths"] = {
                digest: str(path) for digest, path in weight_snapshots.items()
            }
            try:
                if self._cancellation_requested(job_id):
                    raise DetectorDevelopmentError("cancelled", "Detector probe was cancelled")
                if self._supervise_runner:
                    runner_output = self._run_supervised_worker(
                        job_id,
                        execution_request,
                        deepcopy(record["frozen_profiles"]),
                        staging,
                        lambda: self._cancellation_requested(job_id),
                        lambda completed, total: self._record_progress(job_id, completed, total),
                    )
                else:
                    runner_output = self._runner(
                        execution_request,
                        deepcopy(record["frozen_profiles"]),
                        staging,
                        lambda: self._cancellation_requested(job_id),
                        lambda completed, total: self._record_progress(job_id, completed, total),
                    )
                self._verify_weight_snapshots(weight_snapshots)
                if self._cancellation_requested(job_id):
                    raise DetectorDevelopmentError("cancelled", "Detector probe was cancelled")
            finally:
                if not self._worker_is_alive(job_id):
                    self._remove_weight_snapshots(runtime_dir, weight_snapshots)
            # Recheck frozen source identity, then re-hash contract/config snapshots.
            self._execution_request(record)
            report, manifest = self._build_result(record, runner_output, staging)
            atomic_write_json(
                staging / "detector_probe_report.v1.json",
                report,
                trusted_root=staging,
            )
            report_file_sha256, report_file_size = hash_regular_file(
                staging / "detector_probe_report.v1.json",
                "detector probe report",
                trusted_root=staging,
            )
            manifest["report_file_sha256"] = report_file_sha256
            manifest["report_file_size_bytes"] = report_file_size
            atomic_write_json(
                staging / "detector_probe_manifest.v1.json",
                manifest,
                trusted_root=staging,
            )
            manifest_sha256, _ = hash_regular_file(
                staging / "detector_probe_manifest.v1.json",
                "detector probe result manifest",
                trusted_root=staging,
            )
            self._validate_result_tree(staging, record)

            with self._lock:
                current = self._record(job_id)
                if current.get("status") != "running" or current.get("owner_id") != self._owner_id:
                    raise DetectorDevelopmentError(
                        "probe_ownership_lost",
                        "Detector probe worker lost ownership before commit",
                    )
                if current.get("cancel_requested") is True:
                    self._mark_cancelled(current)
                    return
                if self._closed:
                    raise DetectorDevelopmentError(
                        "service_shutting_down",
                        "Detector probe service is shutting down",
                    )
                current.update(
                    {
                        "status": "committing",
                        "stage": "committing",
                        "owner_id": self._owner_id,
                        "updated_at": utc_now_iso(),
                    }
                )
                current["progress"].update(
                    {
                        "completed": current["progress"]["total"],
                        "updated_at": current["updated_at"],
                    }
                )
                self._persist_record(current)

            # Final narrow-window lineage check immediately before publication.
            self._execution_request(record)
            try:
                _publish_staging_directory(staging, destination)
            finally:
                if staging is not None and not staging.exists():
                    published = True
                    staging = None
            committed_report, committed_manifest_sha256 = self._validate_result_tree(destination, record)
            if committed_manifest_sha256 != manifest_sha256:
                raise DetectorDevelopmentError(
                    "committed_manifest_mismatch",
                    "Detector probe manifest changed during atomic publication",
                )
            with self._lock:
                current = self._record(job_id)
                if current.get("status") != "committing" or current.get("owner_id") != self._owner_id:
                    raise DetectorDevelopmentError(
                        "probe_ownership_lost",
                        "Detector probe worker lost ownership during commit",
                    )
                current.update(
                    {
                        "status": "ready",
                        "stage": "ready",
                        "owner_id": None,
                        "cancel_requested": False,
                        "error_code": None,
                        "blocker_code": None,
                        "recovery_action": None,
                        "report": committed_report,
                        "result_manifest_sha256": committed_manifest_sha256,
                        "updated_at": utc_now_iso(),
                    }
                )
                current["progress"].update(
                    {
                        "completed": current["progress"]["total"],
                        "updated_at": current["updated_at"],
                    }
                )
                self._persist_record(current)
        except Exception as exc:
            if published and destination.is_dir():
                try:
                    committed_report, committed_manifest_sha256 = self._validate_result_tree(destination, record)
                except Exception:
                    self._remove_tree(destination)
                else:
                    with self._lock:
                        current = self._record(job_id)
                        if current.get("status") != "committing" or current.get("owner_id") != self._owner_id:
                            return
                        current.update(
                            {
                                "status": "ready",
                                "stage": "ready",
                                "owner_id": None,
                                "cancel_requested": False,
                                "error_code": None,
                                "blocker_code": None,
                                "recovery_action": None,
                                "report": committed_report,
                                "result_manifest_sha256": committed_manifest_sha256,
                                "updated_at": utc_now_iso(),
                            }
                        )
                        current["progress"].update(
                            {
                                "completed": current["progress"]["total"],
                                "updated_at": current["updated_at"],
                            }
                        )
                        self._persist_record(current)
                    return
            if not published:
                self._remove_tree(destination)
            if isinstance(exc, DetectorDevelopmentError) and exc.code == "service_shutting_down" and not published:
                self._requeue_after_shutdown(job_id)
            else:
                self._record_failure(job_id, exc)
        finally:
            if staging is not None and not self._worker_is_alive(job_id):
                self._remove_tree(staging)

    def get_probe_artifact(self, job_id: str, artifact_id: str):
        path, media_type, digest, _content = self._read_probe_artifact(
            job_id,
            artifact_id,
        )
        return path, media_type, digest

    def read_probe_artifact(
        self,
        job_id: str,
        artifact_id: str,
    ) -> tuple[bytes, str, str]:
        _path, media_type, digest, content = self._read_probe_artifact(
            job_id,
            artifact_id,
        )
        return content, media_type, digest

    def _read_probe_artifact(
        self,
        job_id: str,
        artifact_id: str,
    ) -> tuple[Path, str, str, bytes]:
        job_id = require_safe_id(job_id, "detector probe job_id")
        artifact_id = require_safe_id(artifact_id, "detector probe artifact_id")
        with self._lock:
            record = deepcopy(self._record(job_id))
            if record.get("status") != "ready":
                raise DetectorDevelopmentError("probe_not_ready", "Detector probe artifacts are not ready")
        root = self._results_root / job_id
        artifacts = self._cached_result_artifact_index(root, record)
        artifact = artifacts.get(artifact_id)
        if artifact is None:
            raise DetectorDevelopmentError(
                "artifact_not_found", "Detector probe artifact was not found", status_code=404
            )
        if not self._artifact_read_slots.acquire(timeout=_ARTIFACT_READ_ACQUIRE_TIMEOUT_SECONDS):
            raise DetectorDevelopmentError(
                "artifact_read_capacity_exceeded",
                "Detector probe artifact reader capacity is temporarily exhausted",
                status_code=503,
            )
        try:
            if self._closed:
                raise DetectorDevelopmentError("service_closed", "Detector probe service is closed", status_code=503)
            path, digest, content = self._validate_result_artifact(root, artifact, record)
            return path, artifact["media_type"], digest, content
        finally:
            self._artifact_read_slots.release()

    def _cached_result_artifact_index(
        self,
        root: Path,
        record: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        manifest_path = root / "detector_probe_manifest.v1.json"
        report_path = root / "detector_probe_report.v1.json"
        manifest_identity = regular_file_change_identity(manifest_path, "detector probe result manifest")
        report_identity = regular_file_change_identity(report_path, "detector probe report")
        result_digest = record.get("result_manifest_sha256")
        with self._artifact_cache_lock:
            cached = self._artifact_index_cache.get(str(record["job_id"]))
            if (
                cached is not None
                and cached["result_manifest_sha256"] == result_digest
                and cached["manifest_identity"] == manifest_identity
                and cached["report_identity"] == report_identity
            ):
                self._artifact_index_cache.move_to_end(str(record["job_id"]))
                return deepcopy(cached["artifacts"])

            report, _manifest, manifest_sha256 = self._read_result_documents(root, record)
            if result_digest != manifest_sha256:
                raise DetectorDevelopmentError(
                    "artifact_manifest_mismatch",
                    "Detector probe artifact manifest is stale",
                )
            artifacts = {str(item["artifact_id"]): deepcopy(item) for item in report["artifacts"]}
            if len(artifacts) != len(report["artifacts"]):
                raise DetectorDevelopmentError(
                    "invalid_probe_report",
                    "Detector probe report artifact identities are duplicated",
                )
            current_manifest_identity = regular_file_change_identity(manifest_path, "detector probe result manifest")
            current_report_identity = regular_file_change_identity(report_path, "detector probe report")
            if current_manifest_identity != manifest_identity or current_report_identity != report_identity:
                raise DetectorDevelopmentError(
                    "source_changed",
                    "Detector probe result documents changed during artifact indexing",
                )
            self._artifact_index_cache[str(record["job_id"])] = {
                "result_manifest_sha256": result_digest,
                "manifest_identity": manifest_identity,
                "report_identity": report_identity,
                "artifacts": artifacts,
            }
            self._artifact_index_cache.move_to_end(str(record["job_id"]))
            while len(self._artifact_index_cache) > _ARTIFACT_INDEX_CACHE_ENTRIES:
                self._artifact_index_cache.popitem(last=False)
            return deepcopy(artifacts)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._shutdown_event.set()
            self._dispatch_event.set()
            dispatcher = self._dispatcher
        with self._children_lock:
            children = list(self._children.items())
        for job_id, child in children:
            try:
                self._terminate_and_reap_worker(child)
            except DetectorDevelopmentError as exc:
                if exc.code == "worker_termination_failed":
                    self._quarantine_worker(job_id, child)
        deadline = time.monotonic() + (_WORKER_TERMINATE_GRACE_SECONDS + _WORKER_KILL_WAIT_SECONDS + 2.0)
        if dispatcher is not None:
            dispatcher.join(timeout=max(0.0, deadline - time.monotonic()))
        self._executions_drained.wait(timeout=max(0.0, deadline - time.monotonic()))
        self._quarantine_cleanup_drained.wait(timeout=max(0.0, deadline - time.monotonic()))
        shutdown_complete = (
            (dispatcher is None or not dispatcher.is_alive())
            and self._executions_drained.is_set()
            and self._quarantine_cleanup_drained.is_set()
        )
        if not shutdown_complete:
            threading.Thread(
                target=self._release_lease_after_shutdown,
                args=(dispatcher,),
                name=f"detector-probe-close-{self._owner_id}",
                daemon=True,
            ).start()
        else:
            self._release_owner_lease()

    def _release_lease_after_shutdown(self, dispatcher: threading.Thread | None) -> None:
        if dispatcher is not None:
            dispatcher.join()
        self._executions_drained.wait()
        self._quarantine_cleanup_drained.wait()
        self._release_owner_lease()

    def _begin_active_execution(self) -> None:
        with self._active_executions_lock:
            self._active_executions += 1
            self._executions_drained.clear()

    def _end_active_execution(self) -> None:
        with self._active_executions_lock:
            self._active_executions -= 1
            if self._active_executions == 0:
                self._executions_drained.set()

    def _retain_quarantined_execution_lease(self, execution_lease: Any) -> bool:
        with self._quarantine_lock:
            if not self._execution_quarantined:
                return False
            if self._quarantine_child_exited:
                self._execution_quarantined = False
                self._quarantine_child_exited = False
                return False
            if self._quarantined_execution_lease is not None:
                raise RuntimeError("detector probe execution lease is already quarantined")
            self._quarantined_execution_lease = execution_lease
            return True

    def _acquire_owner_lease(self):
        handle = self._owner_lease_path.open("x+b")
        try:
            _require_lock_file_identity(handle, self._owner_lease_path, "owner")
            handle.write(b"0")
            handle.flush()
            os.fsync(handle.fileno())
            if not _try_lock_handle(handle):
                raise DetectorDevelopmentError(
                    "lease_unavailable", "Detector probe service lease could not be acquired"
                )
            _require_lock_file_identity(handle, self._owner_lease_path, "owner")
            return handle
        except Exception:
            handle.close()
            self._owner_lease_path.unlink(missing_ok=True)
            raise

    def _acquire_execution_lease(self):
        if is_link_or_reparse(self._execution_lease_path):
            raise DetectorDevelopmentError("unsafe_execution_lease", "Detector probe execution lease is unsafe")
        handle = self._execution_lease_path.open("a+b")
        try:
            _require_lock_file_identity(handle, self._execution_lease_path, "execution")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
                os.fsync(handle.fileno())
            while True:
                with self._lock:
                    if self._closed:
                        raise DetectorDevelopmentError("service_closed", "Detector probe service is closed")
                if _try_lock_handle(handle):
                    _require_lock_file_identity(handle, self._execution_lease_path, "execution")
                    return handle
                self._dispatch_event.wait(timeout=0.1)
        except Exception:
            handle.close()
            raise

    def _release_owner_lease(self) -> None:
        with self._lock:
            handle = self._owner_lease
            if handle is None:
                return
            self._owner_lease = None
        try:
            _unlock_handle(handle)
        finally:
            handle.close()
            try:
                self._owner_lease_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _owner_is_active(self, owner_id: Any) -> bool | None:
        if owner_id == self._owner_id:
            return self._owner_lease is not None
        try:
            owner_id = require_safe_id(owner_id, "detector probe owner_id")
        except DetectorDevelopmentError:
            return None
        path = self._leases_root / f"{owner_id}.lock"
        if is_link_or_reparse(path):
            return None
        if not path.exists():
            return False
        if not path.is_file():
            return None
        try:
            handle = path.open("r+b")
        except OSError:
            return None
        try:
            try:
                _require_lock_file_identity(handle, path, "owner")
            except DetectorDevelopmentError:
                return None
            if not _try_lock_handle(handle):
                return True
            _unlock_handle(handle)
            return False
        finally:
            handle.close()

    def _execution_request(self, record: dict[str, Any], *, verify_source_digest: bool = False) -> dict[str, Any]:
        """Re-verify the persisted server-derived lineage immediately before execution."""

        frozen = deepcopy(record["frozen_request"])
        current_catalog = self._catalog_provider()
        current_profiles = self._selected_frozen_profiles(current_catalog, list(frozen["profile_ids"]))
        current_profiles_sha256 = canonical_sha256(current_profiles)
        if (
            current_profiles_sha256 != frozen["frozen_profiles_sha256"]
            or current_profiles_sha256 != record["frozen_profiles_sha256"]
            or current_profiles != record["frozen_profiles"]
        ):
            raise DetectorDevelopmentError(
                "runtime_environment_changed",
                "Detector probe profile or model binding changed after request freeze",
            )
        current_bundle = self._build_execution_bundle(current_catalog, current_profiles)
        if (
            current_bundle != frozen["execution_bundle"]
            or canonical_sha256(current_bundle) != frozen["execution_bundle_sha256"]
        ):
            raise DetectorDevelopmentError(
                "runtime_environment_changed",
                "Detector probe runtime environment changed after request freeze",
            )
        source_path = require_trusted_relative_path(
            self.repo_root,
            frozen["source_relative_path"],
            "detector probe source",
            allowed_first_parts={"data"},
        )
        source_identity_sha256, source_size = _source_file_identity(self.repo_root, source_path)
        if (
            source_identity_sha256 != frozen["source_file_identity_sha256"]
            or source_size != frozen["source_size_bytes"]
        ):
            raise DetectorDevelopmentError("source_changed", "Detector probe source changed after request freeze")
        if verify_source_digest:
            _verify_source_digest_cached(
                self.repo_root,
                source_path,
                declared_sha256=frozen["source_sha256"],
                frozen_identity_sha256=frozen["source_file_identity_sha256"],
                frozen_size_bytes=frozen["source_size_bytes"],
                should_cancel=lambda: self._durable_cancellation_requested(str(record["job_id"])),
                should_shutdown=self._shutdown_event.is_set,
            )
        contract_path = require_trusted_relative_path(
            self.repo_root,
            frozen["tracking_contract_relative_path"],
            "detector probe tracking contract",
            allowed_first_parts={"outputs"},
        )
        contract_sha256, _ = hash_regular_file(
            contract_path,
            "detector probe tracking contract",
            trusted_root=self.repo_root,
        )
        if contract_sha256 != frozen["tracking_contract_sha256"]:
            raise DetectorDevelopmentError(
                "tracking_contract_changed",
                "Detector probe tracking contract changed after request freeze",
            )
        for label in ("base", "effective"):
            path_key = f"{label}_config_relative_path"
            digest_key = f"{label}_config_sha256"
            config_path = require_trusted_relative_path(
                self.repo_root,
                frozen[path_key],
                f"detector probe {label} config",
                allowed_first_parts={"config"},
            )
            config_sha256, _ = hash_regular_file(
                config_path,
                f"detector probe {label} config",
                trusted_root=self.repo_root / "config",
            )
            if config_sha256 != frozen[digest_key]:
                raise DetectorDevelopmentError(
                    f"{label}_config_changed",
                    f"Detector probe {label} config changed after request freeze",
                )
        frozen.update(
            {
                "_repo_root": str(self.repo_root),
                "_source_path": str(source_path),
                "_source_width": frozen["source_width"],
                "_source_height": frozen["source_height"],
                "_source_frame_count": frozen["source_frame_count"],
                "_requested_decode_mode": frozen["requested_decode_mode"],
                "_execution_environment": deepcopy(frozen["execution_bundle"]["execution_environment"]),
            }
        )
        return frozen

    def _set_running_stage(self, job_id: str, stage: str) -> None:
        with self._lock:
            record = self._record(job_id)
            if record.get("status") != "running" or record.get("owner_id") != self._owner_id:
                raise DetectorDevelopmentError(
                    "probe_ownership_lost", "Detector probe worker lost its durable ownership"
                )
            record["stage"] = stage
            record["updated_at"] = utc_now_iso()
            record["progress"]["updated_at"] = record["updated_at"]
            self._persist_record(record)

    def _cancellation_requested(self, job_id: str) -> bool:
        if self._closed:
            raise DetectorDevelopmentError(
                "service_shutting_down",
                "Detector probe service is shutting down",
            )
        return self._durable_cancellation_requested(job_id)

    def _durable_cancellation_requested(self, job_id: str) -> bool:
        token_path = self._cancel_root / f"{require_safe_id(job_id, 'detector probe job_id')}.json"
        try:
            content, _ = read_regular_bytes(
                token_path,
                "detector probe cancellation token",
                max_bytes=1024,
                trusted_root=self._cancel_root,
            )
            token = json_object_from_bytes(content, "detector probe cancellation token")
        except DetectorDevelopmentError:
            if token_path.exists() or is_link_or_reparse(token_path):
                raise
            with self._lock:
                record = self._record(job_id)
                self._persist_cancel_token(record)
                return record.get("cancel_requested") is True
        if (
            token.get("schema_version") != "1.0"
            or token.get("artifact_type") != "detector_probe_cancel_token"
            or token.get("job_id") != job_id
            or not isinstance(token.get("cancel_requested"), bool)
        ):
            raise DetectorDevelopmentError(
                "invalid_cancel_token",
                "Detector probe cancellation token is invalid",
            )
        return token["cancel_requested"]

    def _record_progress(self, job_id: str, completed: Any, _runner_total: Any) -> None:
        if isinstance(completed, bool) or not isinstance(completed, int) or completed < 0:
            raise DetectorDevelopmentError("invalid_probe_progress", "Detector probe runner reported invalid progress")
        with self._lock:
            record = self._record(job_id)
            if record.get("status") != "running" or record.get("owner_id") != self._owner_id:
                raise DetectorDevelopmentError(
                    "probe_ownership_lost", "Detector probe worker lost its durable ownership"
                )
            total = int(record["progress"]["total"])
            record["progress"]["completed"] = min(completed, total)
            record["progress"]["updated_at"] = utc_now_iso()
            record["updated_at"] = record["progress"]["updated_at"]
            self._persist_record(record)

    def _ensure_probe_capacity(self, record: dict[str, Any]) -> None:
        frozen = record["frozen_request"]
        frame_count = len(frozen["frame_indices"])
        profile_count = len(frozen["profile_ids"])
        decoded_bytes = int(frozen["source_width"]) * int(frozen["source_height"]) * 3 * frame_count
        if decoded_bytes > _MAX_DECODED_FRAME_BYTES:
            raise DetectorDevelopmentError(
                "probe_memory_envelope_exceeded",
                "Detector probe frame selection exceeds the bounded decoded-frame memory envelope",
                status_code=400,
            )
        # JPEG evidence is normally much smaller than BGR pixels. Budget one
        # full uncompressed image per source frame and per profile overlay,
        # plus a fixed reserve for manifests, temporary encoder output, and
        # unrelated filesystem activity.
        evidence_budget = decoded_bytes * (profile_count + 1)
        required_free = evidence_budget + _OUTPUT_CAPACITY_RESERVE_BYTES
        available = shutil.disk_usage(self._results_root).free
        if available < required_free:
            raise DetectorDevelopmentError(
                "insufficient_probe_disk_capacity",
                "Detector probe output capacity preflight failed",
            )

    def _snapshot_weights(
        self,
        record: dict[str, Any],
        staging: Path,
    ) -> tuple[Path, dict[str, Path]]:
        runtime_dir = secure_mkdirs(staging, ".runtime-weights")
        bindings: dict[str, dict[str, Any]] = {}
        for profile in record["frozen_profiles"]:
            weights = profile["model_descriptor"]["weights"]
            bindings.setdefault(str(weights["sha256"]), weights)
        total_size = sum(int(binding["size_bytes"]) for binding in bindings.values())
        if total_size > _MAX_WEIGHT_SNAPSHOT_BYTES:
            raise DetectorDevelopmentError(
                "weight_snapshot_budget_exceeded",
                "Selected detector weights exceed the bounded private snapshot budget",
                status_code=400,
            )
        snapshots: dict[str, Path] = {}
        for digest, binding in sorted(bindings.items()):
            source = require_trusted_relative_path(
                self.repo_root,
                binding["relative_path"],
                "frozen detector weights",
                allowed_first_parts={"weights"},
            )
            expected_size = int(binding["size_bytes"])
            destination = runtime_dir / f"{digest}.pt"
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            source_fd = os.open(source, flags)
            actual_digest = hashlib.sha256()
            copied = 0
            try:
                metadata = os.fstat(source_fd)
                if not stat.S_ISREG(metadata.st_mode) or int(metadata.st_size) != expected_size:
                    raise DetectorDevelopmentError(
                        "weights_digest_or_size_mismatch",
                        "Frozen detector weights changed before private snapshot",
                    )
                with (
                    os.fdopen(source_fd, "rb", closefd=False) as source_handle,
                    destination.open("xb") as destination_handle,
                ):
                    while True:
                        chunk = source_handle.read(1024 * 1024)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > expected_size:
                            raise DetectorDevelopmentError(
                                "weights_digest_or_size_mismatch",
                                "Frozen detector weights exceed their frozen size",
                            )
                        actual_digest.update(chunk)
                        destination_handle.write(chunk)
                    destination_handle.flush()
                    os.fsync(destination_handle.fileno())
            finally:
                os.close(source_fd)
            if copied != expected_size or actual_digest.hexdigest() != digest:
                destination.unlink(missing_ok=True)
                raise DetectorDevelopmentError(
                    "weights_digest_or_size_mismatch",
                    "Frozen detector weights changed before private snapshot",
                )
            snapshots[digest] = destination
        return runtime_dir, snapshots

    @staticmethod
    def _verify_weight_snapshots(snapshots: dict[str, Path]) -> None:
        for expected_digest, path in snapshots.items():
            digest, _ = hash_regular_file(
                path,
                "private detector weight snapshot",
                trusted_root=path.parent,
            )
            if digest != expected_digest:
                raise DetectorDevelopmentError(
                    "weights_snapshot_changed",
                    "Private detector weight snapshot changed during inference",
                )

    @staticmethod
    def _remove_weight_snapshots(runtime_dir: Path, snapshots: dict[str, Path]) -> None:
        for path in snapshots.values():
            if path.parent == runtime_dir:
                if is_link_or_reparse(path):
                    path.unlink(missing_ok=True)
                elif path.is_file():
                    path.unlink(missing_ok=True)
        try:
            runtime_dir.rmdir()
        except OSError:
            # Never recursively delete runner-controlled content. The enclosing
            # staging cleanup performs its own link-safe bounded walk.
            pass

    @staticmethod
    def _default_worker_command(
        control_dir: Path,
        staging_dir: Path,
        parent_pid: int,
    ) -> list[str]:
        return [
            sys.executable,
            "-m",
            "football_tracking.detector_probe_worker",
            "--control-dir",
            str(control_dir),
            "--staging-dir",
            str(staging_dir),
            "--parent-pid",
            str(parent_pid),
        ]

    def _run_supervised_worker(
        self,
        job_id: str,
        request: dict[str, Any],
        profiles: list[dict[str, Any]],
        staging: Path,
        should_cancel: Callable[[], bool],
        progress: Callable[[int, int], None],
    ) -> dict[str, Any]:
        control = secure_mkdirs(staging, ".worker-control")
        worker_id = f"worker-{uuid.uuid4().hex}"
        atomic_write_json(
            control / "input.json",
            {
                "schema_version": "1.0",
                "artifact_type": "detector_probe_worker_input",
                "worker_id": worker_id,
                "request": request,
                "profiles": profiles,
            },
            trusted_root=control,
        )
        self._write_worker_cancel(control, worker_id, False)
        command = self._worker_command_factory(control, staging, os.getpid())
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(item, str) or not item for item in command)
        ):
            raise DetectorDevelopmentError("invalid_worker_command", "Detector probe worker command is invalid")
        parent_watch_read_fd: int | None = None
        parent_watch_write_fd: int | None = None
        worker_environment = {**os.environ, "PYTHONNOUSERSITE": "1"}
        if os.name != "nt":
            parent_watch_read_fd, parent_watch_write_fd = os.pipe()
            os.set_inheritable(parent_watch_read_fd, True)
            os.set_inheritable(parent_watch_write_fd, False)
            worker_environment["FOOTBALL_TRACKING_PARENT_WATCH_FD"] = str(parent_watch_read_fd)
        popen_options: dict[str, Any] = {
            "cwd": str(Path(__file__).resolve().parents[1]),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "env": worker_environment,
            "close_fds": True,
        }
        if os.name == "nt":
            popen_options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            popen_options["start_new_session"] = True
            popen_options["pass_fds"] = (parent_watch_read_fd,)
        try:
            process = subprocess.Popen(command, **popen_options)
        except OSError as exc:
            if parent_watch_read_fd is not None:
                os.close(parent_watch_read_fd)
            if parent_watch_write_fd is not None:
                os.close(parent_watch_write_fd)
            self._remove_tree(control, trusted_parent=staging)
            raise ProbeWorkerDiedError("Detector probe worker could not start") from exc
        if parent_watch_read_fd is not None:
            os.close(parent_watch_read_fd)
        child = _WorkerProcess(process, staging=staging, control=control, worker_id=worker_id)
        child.parent_watch_write_fd = parent_watch_write_fd
        try:
            self._attach_worker_containment(child)
            child.containment_attached = True
        except Exception as exc:
            try:
                safely_stopped = self._abort_uncontained_worker(child, control, worker_id)
            except Exception:
                safely_stopped = False
            if not safely_stopped:
                with self._children_lock:
                    self._children[job_id] = child
                self._quarantine_worker(job_id, child)
                raise DetectorDevelopmentError(
                    "worker_termination_failed",
                    "Uncontained detector probe worker could not be confirmed stopped",
                    status_code=503,
                ) from exc
            with self._children_lock:
                if self._children.get(job_id) is child:
                    self._children.pop(job_id, None)
            self._remove_tree(control, trusted_parent=staging)
            if self._shutdown_event.is_set():
                raise DetectorDevelopmentError(
                    "service_shutting_down",
                    "Detector probe service is shutting down",
                ) from exc
            raise DetectorDevelopmentError(
                "worker_containment_unavailable",
                "Detector probe worker process-tree containment could not be established",
                status_code=503,
            ) from exc
        with self._children_lock:
            self._children[job_id] = child
        if self._shutdown_event.is_set():
            try:
                self._terminate_and_reap_worker(child)
            except DetectorDevelopmentError as exc:
                if exc.code == "worker_termination_failed":
                    self._quarantine_worker(job_id, child)
                raise
            with self._children_lock:
                if self._children.get(job_id) is child:
                    self._children.pop(job_id, None)
            self._remove_tree(control, trusted_parent=staging)
            raise DetectorDevelopmentError(
                "service_shutting_down",
                "Detector probe service is shutting down",
            )
        try:
            self._write_worker_start(control, worker_id, process.pid)
        except Exception:
            try:
                self._terminate_and_reap_worker(child)
            except DetectorDevelopmentError as exc:
                if exc.code == "worker_termination_failed":
                    self._quarantine_worker(job_id, child)
                raise
            with self._children_lock:
                if self._children.get(job_id) is child:
                    self._children.pop(job_id, None)
            self._remove_tree(control, trusted_parent=staging)
            raise

        started = time.monotonic()
        last_heartbeat = started
        last_sequence = -1
        observed_worker_pid: int | None = None
        last_completed = -1
        cancellation_started: float | None = None
        try:
            while process.poll() is None:
                now = time.monotonic()
                if self._shutdown_event.is_set():
                    self._terminate_and_reap_worker(child)
                    raise DetectorDevelopmentError(
                        "service_shutting_down",
                        "Detector probe service is shutting down",
                    )
                try:
                    cancellation_requested = should_cancel()
                except BaseException:
                    self._terminate_and_reap_worker(child)
                    raise
                if cancellation_requested:
                    if cancellation_started is None:
                        cancellation_started = now
                        self._write_worker_cancel(control, worker_id, True)
                    elif now - cancellation_started >= _WORKER_CANCEL_GRACE_SECONDS:
                        self._terminate_and_reap_worker(child)
                        raise DetectorDevelopmentError("cancelled", "Detector probe was cancelled")
                if now - started >= self._worker_deadline_seconds:
                    self._terminate_and_reap_worker(child)
                    raise DetectorDevelopmentError(
                        "probe_worker_timeout",
                        "Detector probe worker exceeded its fixed execution deadline",
                    )

                heartbeat = self._read_worker_document(
                    control / "heartbeat.json",
                    control,
                    "detector probe worker heartbeat",
                    max_bytes=4096,
                )
                if heartbeat is not None:
                    sequence, heartbeat_worker_pid = self._validate_worker_heartbeat(heartbeat, worker_id)
                    if observed_worker_pid is None:
                        observed_worker_pid = heartbeat_worker_pid
                    elif heartbeat_worker_pid != observed_worker_pid:
                        raise DetectorDevelopmentError(
                            "invalid_worker_protocol",
                            "Detector probe worker heartbeat process changed",
                        )
                    if sequence > last_sequence:
                        last_sequence = sequence
                        last_heartbeat = now
                if now - last_heartbeat >= self._worker_heartbeat_timeout_seconds:
                    self._terminate_and_reap_worker(child)
                    raise DetectorDevelopmentError(
                        "probe_worker_heartbeat_timeout",
                        "Detector probe worker heartbeat stopped",
                    )

                progress_payload = self._read_worker_document(
                    control / "progress.json",
                    control,
                    "detector probe worker progress",
                    max_bytes=4096,
                )
                if progress_payload is not None:
                    completed, total = self._validate_worker_progress(progress_payload, worker_id)
                    if completed > last_completed:
                        progress(completed, total)
                        last_completed = completed
                self._shutdown_event.wait(0.05)

            if self._shutdown_event.is_set():
                raise DetectorDevelopmentError(
                    "service_shutting_down",
                    "Detector probe service is shutting down",
                )
            if cancellation_started is not None:
                raise DetectorDevelopmentError("cancelled", "Detector probe was cancelled")
            result = self._read_worker_document(
                control / "result.json",
                control,
                "detector probe worker result",
                max_bytes=64 * 1024 * 1024,
            )
            error = self._read_worker_document(
                control / "error.json",
                control,
                "detector probe worker error",
                max_bytes=4096,
            )
            if process.returncode == 0 and result is not None and error is None:
                if (
                    set(result)
                    != {
                        "schema_version",
                        "artifact_type",
                        "worker_id",
                        "runner_output",
                    }
                    or result.get("schema_version") != "1.0"
                    or result.get("artifact_type") != "detector_probe_worker_result"
                    or result.get("worker_id") != worker_id
                    or not isinstance(result.get("runner_output"), dict)
                ):
                    raise DetectorDevelopmentError(
                        "invalid_worker_protocol",
                        "Detector probe worker result is invalid",
                    )
                return result["runner_output"]
            if error is not None:
                self._raise_worker_error(error, worker_id)
            if process.returncode == _WORKER_EXIT_DISK_EXHAUSTED:
                raise DetectorDevelopmentError(
                    "disk_exhausted",
                    "Detector probe worker could not persist its disk failure envelope",
                )
            if process.returncode == _WORKER_EXIT_ERROR_ENVELOPE_UNAVAILABLE:
                raise DetectorDevelopmentError(
                    "worker_error_envelope_unavailable",
                    "Detector probe worker could not persist its failure envelope",
                )
            if process.returncode == _WORKER_EXIT_CONTAINMENT_UNAVAILABLE:
                raise DetectorDevelopmentError(
                    "worker_containment_unavailable",
                    "Detector probe worker could not establish parent-death containment",
                    status_code=503,
                )
            raise ProbeWorkerDiedError(f"Detector probe worker exited unexpectedly ({process.returncode})")
        finally:
            if process.poll() is None:
                try:
                    self._terminate_and_reap_worker(child)
                except DetectorDevelopmentError as exc:
                    if exc.code == "worker_termination_failed":
                        self._quarantine_worker(job_id, child)
                    raise
            if process.poll() is not None:
                try:
                    containment_closed = self._finalize_worker_containment(child, terminate=True)
                except Exception as exc:
                    self._quarantine_worker(job_id, child)
                    raise DetectorDevelopmentError(
                        "worker_termination_failed",
                        "Detector probe worker descendants could not be confirmed stopped",
                        status_code=503,
                    ) from exc
                if not containment_closed:
                    self._quarantine_worker(job_id, child)
                    raise DetectorDevelopmentError(
                        "worker_termination_failed",
                        "Detector probe worker descendants could not be confirmed stopped",
                        status_code=503,
                    )
                with self._children_lock:
                    if self._children.get(job_id) is child:
                        self._children.pop(job_id, None)
                self._remove_tree(control, trusted_parent=staging)

    @staticmethod
    def _write_worker_start(control: Path, worker_id: str, worker_pid: int) -> None:
        atomic_write_json(
            control / "start.json",
            {
                "schema_version": "1.0",
                "artifact_type": "detector_probe_worker_start",
                "worker_id": worker_id,
                "launcher_pid": worker_pid,
                "parent_pid": os.getpid(),
            },
            trusted_root=control,
        )

    def _abort_uncontained_worker(self, child: _WorkerProcess, control: Path, worker_id: str) -> bool:
        atomic_write_json(
            control / "abort.json",
            {
                "schema_version": "1.0",
                "artifact_type": "detector_probe_worker_abort",
                "worker_id": worker_id,
                "abort_requested": True,
            },
            trusted_root=control,
        )
        if not self._wait_for_worker_exit(child.process, _WORKER_TERMINATE_GRACE_SECONDS):
            return False
        if not self._worker_abort_acknowledged(child, worker_id):
            if os.name == "nt":
                return False
        return self._finalize_worker_containment(child, terminate=True)

    def _worker_abort_acknowledged(self, child: _WorkerProcess, worker_id: str) -> bool:
        payload = self._read_worker_document(
            child.control / "abort-ack.json",
            child.control,
            "detector probe worker abort acknowledgement",
            max_bytes=4096,
        )
        return bool(
            payload is not None
            and set(payload)
            == {
                "schema_version",
                "artifact_type",
                "worker_id",
                "worker_pid",
            }
            and payload.get("schema_version") == "1.0"
            and payload.get("artifact_type") == "detector_probe_worker_abort_ack"
            and payload.get("worker_id") == worker_id
            and isinstance(payload.get("worker_pid"), int)
            and not isinstance(payload.get("worker_pid"), bool)
            and payload["worker_pid"] > 0
        )

    @staticmethod
    def _write_worker_cancel(control: Path, worker_id: str, requested: bool) -> None:
        atomic_write_json(
            control / "cancel.json",
            {
                "schema_version": "1.0",
                "artifact_type": "detector_probe_worker_cancel",
                "worker_id": worker_id,
                "cancel_requested": requested,
            },
            trusted_root=control,
        )

    @staticmethod
    def _read_worker_document(
        path: Path,
        control: Path,
        label: str,
        *,
        max_bytes: int,
    ) -> dict[str, Any] | None:
        if not path.exists() and not is_link_or_reparse(path):
            return None
        try:
            content, _ = read_regular_bytes(
                path,
                label,
                max_bytes=max_bytes,
                trusted_root=control,
            )
        except DetectorDevelopmentError as exc:
            if exc.code in {"path_unavailable", "source_changed"} and not is_link_or_reparse(path):
                return None
            raise
        return json_object_from_bytes(content, label)

    @staticmethod
    def _validate_worker_heartbeat(payload: dict[str, Any], worker_id: str) -> tuple[int, int]:
        if (
            set(payload)
            != {
                "schema_version",
                "artifact_type",
                "worker_id",
                "worker_pid",
                "parent_pid",
                "sequence",
            }
            or payload.get("schema_version") != "1.0"
            or payload.get("artifact_type") != "detector_probe_worker_heartbeat"
            or payload.get("worker_id") != worker_id
            or isinstance(payload.get("worker_pid"), bool)
            or not isinstance(payload.get("worker_pid"), int)
            or payload["worker_pid"] <= 0
            or payload.get("parent_pid") != os.getpid()
            or isinstance(payload.get("sequence"), bool)
            or not isinstance(payload.get("sequence"), int)
            or payload["sequence"] < 1
        ):
            raise DetectorDevelopmentError("invalid_worker_protocol", "Detector probe worker heartbeat is invalid")
        return payload["sequence"], payload["worker_pid"]

    @staticmethod
    def _validate_worker_progress(payload: dict[str, Any], worker_id: str) -> tuple[int, int]:
        if (
            set(payload)
            != {
                "schema_version",
                "artifact_type",
                "worker_id",
                "completed",
                "total",
            }
            or payload.get("schema_version") != "1.0"
            or payload.get("artifact_type") != "detector_probe_worker_progress"
            or payload.get("worker_id") != worker_id
            or isinstance(payload.get("completed"), bool)
            or not isinstance(payload.get("completed"), int)
            or payload["completed"] < 0
            or isinstance(payload.get("total"), bool)
            or not isinstance(payload.get("total"), int)
            or payload["total"] <= 0
            or payload["completed"] > payload["total"]
        ):
            raise DetectorDevelopmentError("invalid_worker_protocol", "Detector probe worker progress is invalid")
        return payload["completed"], payload["total"]

    @staticmethod
    def _raise_worker_error(payload: dict[str, Any], worker_id: str) -> None:
        if (
            set(payload)
            != {
                "schema_version",
                "artifact_type",
                "worker_id",
                "code",
                "status_code",
            }
            or payload.get("schema_version") != "1.0"
            or payload.get("artifact_type") != "detector_probe_worker_error"
            or payload.get("worker_id") != worker_id
            or not isinstance(payload.get("code"), str)
            or not payload["code"]
            or isinstance(payload.get("status_code"), bool)
            or not isinstance(payload.get("status_code"), int)
            or payload["status_code"] not in {400, 404, 409, 503}
        ):
            raise DetectorDevelopmentError("invalid_worker_protocol", "Detector probe worker error is invalid")
        raise DetectorDevelopmentError(
            payload["code"],
            "Detector probe worker reported a structured failure",
            status_code=payload["status_code"],
        )

    @staticmethod
    def _attach_worker_containment(child: _WorkerProcess) -> None:
        if os.name != "nt":
            # start_new_session=True made the worker PID the process-group ID.
            if child.process.poll() is not None:
                raise RuntimeError("detector probe worker exited before containment")
            return

        import ctypes
        from ctypes import wintypes

        class _JobBasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _JobBasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        job_handle = kernel32.CreateJobObjectW(None, None)
        if not job_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        child.windows_job_handle = int(job_handle)
        try:
            information = _ExtendedLimitInformation()
            information.BasicLimitInformation.LimitFlags = 0x00002000
            if not kernel32.SetInformationJobObject(
                job_handle,
                9,
                ctypes.byref(information),
                ctypes.sizeof(information),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel32.AssignProcessToJobObject(job_handle, wintypes.HANDLE(int(child.process._handle))):
                raise ctypes.WinError(ctypes.get_last_error())
        except Exception:
            if kernel32.CloseHandle(job_handle):
                child.windows_job_handle = None
            raise

    @staticmethod
    def _signal_worker_tree(child: _WorkerProcess, *, force: bool) -> None:
        process = child.process
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            except ProcessLookupError:
                pass
            return
        if child.windows_job_handle is not None:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.TerminateJobObject.argtypes = [
                wintypes.HANDLE,
                wintypes.UINT,
            ]
            kernel32.TerminateJobObject.restype = wintypes.BOOL
            if not kernel32.TerminateJobObject(wintypes.HANDLE(child.windows_job_handle), 1):
                error = ctypes.get_last_error()
                if error not in {5, 6}:
                    raise ctypes.WinError(error)
            return
        if force:
            process.kill()
        else:
            process.terminate()

    @staticmethod
    def _wait_for_worker_exit(process: subprocess.Popen[bytes], timeout: float) -> bool:
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        return True

    @staticmethod
    def _wait_posix_process_group_empty(process_group_id: int) -> bool:
        deadline = time.monotonic() + _WORKER_KILL_WAIT_SECONDS
        while time.monotonic() < deadline:
            try:
                os.killpg(process_group_id, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.02)
        return False

    @staticmethod
    def _wait_windows_job_empty(job_handle: int) -> bool:
        import ctypes
        from ctypes import wintypes

        class _BasicAccountingInformation(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        deadline = time.monotonic() + _WORKER_KILL_WAIT_SECONDS
        while time.monotonic() < deadline:
            information = _BasicAccountingInformation()
            if not kernel32.QueryInformationJobObject(
                wintypes.HANDLE(job_handle),
                1,
                ctypes.byref(information),
                ctypes.sizeof(information),
                None,
            ):
                error = ctypes.get_last_error()
                if error == 6:
                    return True
                raise ctypes.WinError(error)
            if information.ActiveProcesses == 0:
                return True
            time.sleep(0.02)
        return False

    @classmethod
    def _close_worker_containment(cls, child: _WorkerProcess, *, terminate: bool) -> bool:
        if child.parent_watch_write_fd is not None:
            os.close(child.parent_watch_write_fd)
            child.parent_watch_write_fd = None
        if os.name != "nt":
            if terminate:
                try:
                    os.killpg(child.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    return True
                return cls._wait_posix_process_group_empty(child.process.pid)
            return True
        handle = child.windows_job_handle
        if handle is None:
            return True
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        if terminate:
            if not kernel32.TerminateJobObject(wintypes.HANDLE(handle), 1):
                error = ctypes.get_last_error()
                if error != 5:
                    raise ctypes.WinError(error)
            if not cls._wait_windows_job_empty(handle):
                return False
        if not kernel32.CloseHandle(wintypes.HANDLE(handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        child.windows_job_handle = None
        return True

    def _terminate_and_reap_worker(self, child: _WorkerProcess) -> None:
        with child.termination_lock:
            process = child.process
            try:
                if process.poll() is None:
                    try:
                        self._signal_worker_tree(child, force=False)
                    except OSError:
                        pass
                    if not self._wait_for_worker_exit(process, _WORKER_TERMINATE_GRACE_SECONDS):
                        try:
                            self._signal_worker_tree(child, force=True)
                        except OSError:
                            pass
                        if not self._wait_for_worker_exit(process, _WORKER_KILL_WAIT_SECONDS):
                            raise DetectorDevelopmentError(
                                "worker_termination_failed",
                                "Detector probe worker could not be confirmed stopped",
                                status_code=503,
                            )
                if self._close_worker_containment(child, terminate=True):
                    return
            except DetectorDevelopmentError:
                raise
            except Exception as exc:
                raise DetectorDevelopmentError(
                    "worker_termination_failed",
                    "Detector probe worker process tree termination could not be verified",
                    status_code=503,
                ) from exc
            raise DetectorDevelopmentError(
                "worker_termination_failed",
                "Detector probe worker process tree could not be confirmed stopped",
                status_code=503,
            )

    def _finalize_worker_containment(self, child: _WorkerProcess, *, terminate: bool) -> bool:
        with child.termination_lock:
            return self._close_worker_containment(child, terminate=terminate)

    def _quarantine_worker(self, job_id: str, child: _WorkerProcess) -> None:
        with self._quarantine_lock:
            if child.quarantined:
                return
            child.quarantined = True
            self._execution_quarantined = True
            self._quarantine_child_exited = False
            self._quarantine_watchers += 1
            self._quarantine_cleanup_drained.clear()
        threading.Thread(
            target=self._watch_quarantined_worker,
            args=(job_id, child),
            name=f"detector-probe-quarantine-{job_id}",
            daemon=True,
        ).start()

    def _watch_quarantined_worker(self, job_id: str, child: _WorkerProcess) -> None:
        while child.process.poll() is None:
            time.sleep(0.05)
        if os.name == "nt" and not child.containment_attached:
            while not self._worker_abort_acknowledged(child, child.worker_id):
                time.sleep(0.05)
        while True:
            try:
                if self._finalize_worker_containment(child, terminate=True):
                    break
            except Exception:
                pass
            time.sleep(0.05)
        while not _path_entry_is_absent(child.staging):
            self._remove_tree(child.staging)
            if _path_entry_is_absent(child.staging):
                break
            time.sleep(0.05)
        with self._children_lock:
            if self._children.get(job_id) is child:
                self._children.pop(job_id, None)
        with self._quarantine_lock:
            execution_lease = self._quarantined_execution_lease
            self._quarantined_execution_lease = None
            if execution_lease is None:
                self._quarantine_child_exited = True
            else:
                self._execution_quarantined = False
                self._quarantine_child_exited = False
        if execution_lease is not None:
            try:
                _unlock_handle(execution_lease)
            finally:
                execution_lease.close()
        with self._quarantine_lock:
            self._quarantine_watchers -= 1
            if self._quarantine_watchers == 0:
                self._quarantine_cleanup_drained.set()

    def _worker_is_alive(self, job_id: str) -> bool:
        with self._children_lock:
            child = self._children.get(job_id)
        return child is not None

    def _build_result(
        self,
        record: dict[str, Any],
        runner_output: Any,
        staging: Path,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(runner_output, dict) or not isinstance(runner_output.get("frames"), list):
            raise DetectorDevelopmentError(
                "partial_probe_result", "Detector probe runner returned no complete frame set"
            )
        frozen = record["frozen_request"]
        expected_frames = list(frozen["frame_indices"])
        expected_profile_ids = list(frozen["profile_ids"])
        frozen_profiles = {profile["profile_id"]: profile for profile in record["frozen_profiles"]}
        frame_rows = runner_output["frames"]
        observed_indices = [row.get("frame_index") if isinstance(row, dict) else None for row in frame_rows]
        if (
            len(frame_rows) != len(expected_frames)
            or len(set(observed_indices)) != len(observed_indices)
            or sorted(observed_indices) != expected_frames
        ):
            raise DetectorDevelopmentError(
                "partial_probe_result",
                "Detector probe runner did not return every exact requested frame once",
            )

        artifacts: list[dict[str, Any]] = []
        artifact_ids: set[str] = set()
        report_frames: list[dict[str, Any]] = []
        review_proxy_manifest = runner_output.get("review_proxy_manifest")
        proxy_dimensions = None
        if review_proxy_manifest is not None:
            try:
                review_proxy_manifest = validate_review_proxy_manifest(review_proxy_manifest)
            except (ReviewProxyError, DetectorDevelopmentError) as exc:
                raise DetectorDevelopmentError(
                    "invalid_review_proxy",
                    "Detector probe runner returned an invalid review proxy",
                ) from exc
            proxy_dimensions = (
                review_proxy_manifest["proxy"]["width"],
                review_proxy_manifest["proxy"]["height"],
            )
        for row in sorted(frame_rows, key=lambda item: item["frame_index"]):
            frame_index = row["frame_index"]
            source_artifact = self._evidence_artifact(
                staging,
                row.get("source_frame_relative_path"),
                f"source-frame-{frame_index:09d}",
                {"frames"},
                artifact_ids,
                expected_width=frozen["source_width"],
                expected_height=frozen["source_height"],
            )
            artifacts.append(source_artifact)
            proxy_artifact = None
            if proxy_dimensions is not None:
                proxy_artifact = self._evidence_artifact(
                    staging,
                    row.get("proxy_frame_relative_path"),
                    f"review-proxy-frame-{frame_index:09d}",
                    {"proxy_frames"},
                    artifact_ids,
                    expected_width=proxy_dimensions[0],
                    expected_height=proxy_dimensions[1],
                )
                artifacts.append(proxy_artifact)
            elif row.get("proxy_frame_relative_path") is not None:
                raise DetectorDevelopmentError(
                    "invalid_review_proxy",
                    "Direct detector probe cannot publish an unbound proxy frame",
                )
            raw_profile_rows = row.get("profile_results")
            if not isinstance(raw_profile_rows, list):
                raise DetectorDevelopmentError("partial_probe_result", "Detector probe frame has no profile results")
            by_profile = {
                item.get("profile_id"): item
                for item in raw_profile_rows
                if isinstance(item, dict) and isinstance(item.get("profile_id"), str)
            }
            if len(by_profile) != len(raw_profile_rows) or set(by_profile) != set(expected_profile_ids):
                raise DetectorDevelopmentError(
                    "partial_probe_result",
                    "Detector probe frame does not contain every exact frozen profile once",
                )
            profile_results: list[dict[str, Any]] = []
            for profile_id in expected_profile_ids:
                result = by_profile[profile_id]
                frozen_profile = frozen_profiles[profile_id]
                if (
                    result.get("profile_sha256") != frozen_profile["profile_sha256"]
                    or result.get("status") != "completed"
                    or result.get("top_k") != 5
                    or result.get("failure_code") is not None
                ):
                    raise DetectorDevelopmentError(
                        "partial_probe_result",
                        f"Detector probe profile result is incomplete: {profile_id}",
                    )
                raw_candidates = result.get("raw_candidates")
                if not isinstance(raw_candidates, list) or len(raw_candidates) > 5:
                    raise DetectorDevelopmentError(
                        "partial_probe_result", "Detector probe candidate evidence exceeds top_k"
                    )
                candidates = [
                    self._validated_candidate(
                        item,
                        frame_index=frame_index,
                        width=frozen["source_width"],
                        height=frozen["source_height"],
                        frozen_profile=frozen_profile,
                    )
                    for item in raw_candidates
                ]
                display_candidate = result.get("display_candidate")
                if display_candidate is not None:
                    display_candidate = self._validated_candidate(
                        display_candidate,
                        frame_index=frame_index,
                        width=frozen["source_width"],
                        height=frozen["source_height"],
                        frozen_profile=frozen_profile,
                    )
                    if display_candidate not in candidates:
                        raise DetectorDevelopmentError(
                            "partial_probe_result",
                            "Detector probe display candidate is not in the bounded candidate list",
                        )
                overlay_artifact = self._evidence_artifact(
                    staging,
                    result.get("raw_overlay_relative_path"),
                    f"raw-overlay-{frame_index:09d}-{profile_id}",
                    {"overlays"},
                    artifact_ids,
                    expected_width=frozen["source_width"],
                    expected_height=frozen["source_height"],
                )
                artifacts.append(overlay_artifact)
                candidate_count = result.get("candidate_count")
                if (
                    isinstance(candidate_count, bool)
                    or not isinstance(candidate_count, int)
                    or candidate_count < len(candidates)
                ):
                    raise DetectorDevelopmentError(
                        "partial_probe_result", "Detector probe candidate count is inconsistent"
                    )
                latency_ms = result.get("latency_ms")
                if (
                    isinstance(latency_ms, bool)
                    or not isinstance(latency_ms, (int, float))
                    or not math.isfinite(float(latency_ms))
                    or float(latency_ms) < 0
                ):
                    raise DetectorDevelopmentError("partial_probe_result", "Detector probe latency is invalid")
                filter_reasons = result.get("filter_reasons")
                if not isinstance(filter_reasons, dict) or any(
                    not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, int) or value <= 0
                    for key, value in filter_reasons.items()
                ):
                    raise DetectorDevelopmentError("partial_probe_result", "Detector probe filter reasons are invalid")
                duplicate_count = filter_reasons.get("duplicate_suppressed_iou", 0)
                deduplicated_count = candidate_count - duplicate_count
                if (
                    deduplicated_count < 0
                    or len(candidates) != min(deduplicated_count, 5)
                    or filter_reasons.get("top_k_limit", 0) != max(0, deduplicated_count - 5)
                    or display_candidate != (candidates[0] if candidates else None)
                ):
                    raise DetectorDevelopmentError(
                        "partial_probe_result",
                        "Detector probe candidate accounting is inconsistent",
                    )
                profile_results.append(
                    {
                        "profile_id": profile_id,
                        "profile_sha256": frozen_profile["profile_sha256"],
                        "status": "completed",
                        "latency_ms": float(latency_ms),
                        "candidate_count": candidate_count,
                        "top_k": 5,
                        "raw_candidates": candidates,
                        "display_candidate": display_candidate,
                        "filter_reasons": dict(sorted(filter_reasons.items())),
                        "failure_code": None,
                        "raw_overlay_artifact_url": (
                            f"/api/v1/detector-probes/{record['job_id']}/artifacts/{overlay_artifact['artifact_id']}"
                        ),
                        "raw_overlay_sha256": overlay_artifact["sha256"],
                        "raw_overlay_size_bytes": overlay_artifact["size_bytes"],
                    }
                )
            report_frames.append(
                {
                    "frame_index": frame_index,
                    "source_width": frozen["source_width"],
                    "source_height": frozen["source_height"],
                    "requested_decode_mode": row.get("requested_decode_mode", frozen["requested_decode_mode"]),
                    "effective_decode_mode": row.get("effective_decode_mode"),
                    "decoded_frame_position": row.get("decoded_frame_position", frame_index),
                    "decoder_reported_pos_msec": row.get("decoder_reported_pos_msec"),
                    "decoder_timing_observation_method": row.get("decoder_timing_observation_method"),
                    "media_integrity": deepcopy(row.get("media_integrity")),
                    "source_artifact_url": (
                        f"/api/v1/detector-probes/{record['job_id']}/artifacts/{source_artifact['artifact_id']}"
                    ),
                    "source_frame_sha256": source_artifact["sha256"],
                    "source_frame_size_bytes": source_artifact["size_bytes"],
                    "proxy_artifact_url": (
                        f"/api/v1/detector-probes/{record['job_id']}/artifacts/{proxy_artifact['artifact_id']}"
                        if proxy_artifact is not None
                        else None
                    ),
                    "proxy_frame_sha256": (proxy_artifact["sha256"] if proxy_artifact is not None else None),
                    "proxy_frame_size_bytes": (proxy_artifact["size_bytes"] if proxy_artifact is not None else None),
                    "profile_results": profile_results,
                }
            )

        artifacts.sort(key=lambda item: item["artifact_id"])
        report: dict[str, Any] = {
            "schema_version": "1.0",
            "artifact_type": "detector_probe_report",
            "job_id": record["job_id"],
            "request_sha256": record["request_sha256"],
            "source": {
                "source_id": frozen["source_id"],
                "relative_path": frozen["source_relative_path"],
                "sha256": frozen["source_sha256"],
                "file_identity_sha256": frozen["source_file_identity_sha256"],
                "size_bytes": frozen["source_size_bytes"],
                "width": frozen["source_width"],
                "height": frozen["source_height"],
                "frame_count": frozen["source_frame_count"],
                "tracking_contract_relative_path": frozen["tracking_contract_relative_path"],
                "tracking_contract_sha256": frozen["tracking_contract_sha256"],
            },
            "lineage": {
                "parent_trial_id": frozen["parent_trial_id"],
                "base_config_relative_path": frozen["base_config_relative_path"],
                "base_config_sha256": frozen["base_config_sha256"],
                "effective_config_relative_path": frozen["effective_config_relative_path"],
                "effective_config_sha256": frozen["effective_config_sha256"],
                "trial_intent_sha256": frozen["trial_intent_sha256"],
                "tuning_patch_binding": deepcopy(frozen["tuning_patch_binding"]),
                "tuning_patch_sha256": frozen["tuning_patch_sha256"],
                "profile_sha256s": deepcopy(frozen["profile_sha256s"]),
                "frozen_profiles_sha256": frozen["frozen_profiles_sha256"],
                "execution_bundle": deepcopy(frozen["execution_bundle"]),
                "execution_bundle_sha256": frozen["execution_bundle_sha256"],
                "runtime_environment_sha256": frozen["runtime_environment_sha256"],
                "intent_sha256": record["intent_sha256"],
                "semantic_intent_sha256": record.get("semantic_intent_sha256"),
                "retry_from_job_id": record.get("retry_from_job_id"),
                "retry_kind": frozen.get("retry_kind"),
                "review_proxy_upgrade": deepcopy(frozen.get("review_proxy_upgrade")),
            },
            "frozen_profiles": deepcopy(record["frozen_profiles"]),
            "top_k": 5,
            "frames": report_frames,
            "decode": deepcopy(runner_output.get("decode")),
            "review_proxy_manifest": deepcopy(review_proxy_manifest),
            "execution": deepcopy(runner_output.get("execution")),
            "artifacts": artifacts,
            "created_at": utc_now_iso(),
        }
        report["report_sha256"] = canonical_sha256(report)
        self._validate_report_contract(report, record)
        manifest = {
            "schema_version": "1.0",
            "artifact_type": "detector_probe_result_manifest",
            "job_id": record["job_id"],
            "request_sha256": record["request_sha256"],
            "frozen_profiles_sha256": frozen["frozen_profiles_sha256"],
            "execution_bundle_sha256": frozen["execution_bundle_sha256"],
            "runtime_environment_sha256": frozen["runtime_environment_sha256"],
            "source_file_identity_sha256": frozen["source_file_identity_sha256"],
            "report_content_sha256": report["report_sha256"],
            "artifacts": deepcopy(artifacts),
        }
        return report, manifest

    def _evidence_artifact(
        self,
        root: Path,
        relative_path: Any,
        artifact_id: str,
        allowed_roots: set[str],
        seen_ids: set[str],
        *,
        expected_width: int,
        expected_height: int,
    ) -> dict[str, Any]:
        artifact_id = require_safe_id(artifact_id, "detector probe artifact_id")
        if artifact_id in seen_ids:
            raise DetectorDevelopmentError("partial_probe_result", "Detector probe artifact ID is duplicated")
        path = require_trusted_relative_path(
            root,
            relative_path,
            "detector probe evidence",
            allowed_first_parts=allowed_roots,
        )
        if path.suffix.lower() not in {".jpg", ".jpeg"}:
            raise DetectorDevelopmentError("invalid_artifact_type", "Detector probe visual evidence must be JPEG")
        content, digest = read_regular_bytes(
            path,
            "detector probe evidence",
            max_bytes=64 * 1024 * 1024,
            trusted_root=root,
        )
        if not content or not content.startswith(b"\xff\xd8\xff") or not content.endswith(b"\xff\xd9"):
            raise DetectorDevelopmentError("corrupt_frame", "Detector probe visual evidence is not a complete JPEG")
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or getattr(image, "size", 0) == 0:
            raise DetectorDevelopmentError("corrupt_frame", "Detector probe visual evidence is not a decodable JPEG")
        height, width = image.shape[:2]
        if width != expected_width or height != expected_height:
            raise DetectorDevelopmentError(
                "artifact_dimension_mismatch",
                "Detector probe visual evidence dimensions do not match the frozen source",
            )
        seen_ids.add(artifact_id)
        return {
            "artifact_id": artifact_id,
            "relative_path": path.relative_to(root).as_posix(),
            "media_type": "image/jpeg",
            "sha256": digest,
            "size_bytes": len(content),
            "width": width,
            "height": height,
        }

    @staticmethod
    def _validated_candidate(
        candidate: Any,
        *,
        frame_index: int,
        width: int,
        height: int,
        frozen_profile: dict[str, Any],
    ) -> dict[str, Any]:
        expected_fields = {
            "frame_index",
            "bbox_source_px",
            "confidence",
            "class_name",
            "checkpoint_class_name",
            "source",
            "coordinate_reason",
            "merge_reason",
        }
        if (
            not isinstance(candidate, dict)
            or set(candidate) != expected_fields
            or candidate.get("frame_index") != frame_index
        ):
            raise DetectorDevelopmentError("partial_probe_result", "Detector probe candidate has invalid frame lineage")
        bbox = candidate.get("bbox_source_px")
        confidence = candidate.get("confidence")
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or any(
                isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))
                for value in [*bbox, confidence]
            )
        ):
            raise DetectorDevelopmentError("partial_probe_result", "Detector probe candidate coordinates are invalid")
        x1, y1, x2, y2 = (float(value) for value in bbox)
        parsed_confidence = float(confidence)
        mode = frozen_profile.get("mode")
        checkpoint_class_name = candidate.get("checkpoint_class_name")
        class_map = frozen_profile.get("model_descriptor", {}).get("class_map")
        expected_source = "yolo_direct" if mode == "direct" else "yolo_sahi"
        expected_coordinate_reason = "direct_source_coordinates" if mode == "direct" else "sahi_tile_offset_applied"
        if (
            x1 < 0
            or y1 < 0
            or x2 <= x1
            or y2 <= y1
            or x2 > width
            or y2 > height
            or not 0 <= parsed_confidence <= 1
            or candidate.get("class_name") != "ball"
            or mode not in {"direct", "sahi"}
            or candidate.get("source") != expected_source
            or candidate.get("coordinate_reason") != expected_coordinate_reason
            or candidate.get("merge_reason") != "retained_top_k"
            or not isinstance(checkpoint_class_name, str)
            or not checkpoint_class_name.strip()
            or not isinstance(class_map, dict)
            or class_map.get(checkpoint_class_name.strip().lower()) != "ball"
        ):
            raise DetectorDevelopmentError(
                "partial_probe_result", "Detector probe candidate is outside source coordinates"
            )
        return deepcopy(candidate)

    @staticmethod
    def _is_audited_t2_legacy_report(
        report: dict[str, Any],
        frozen: dict[str, Any],
    ) -> bool:
        binding = _AUDITED_T2_LEGACY_REPORT_BINDINGS.get(report.get("job_id"))
        lineage = report.get("lineage")
        return bool(
            binding is not None
            and report.get("report_sha256") == binding["report_sha256"]
            and frozen.get("execution_bundle_sha256") == binding["execution_bundle_sha256"]
            and isinstance(lineage, dict)
            and lineage.get("execution_bundle_sha256") == binding["execution_bundle_sha256"]
            and "review_proxy_manifest" not in report
        )

    def _validate_report_contract(
        self,
        report: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        # Keep durable evidence independently valid against the same strict
        # contract exposed by the HTTP API. A malformed runner result must fail
        # before publication, never become a ready row that GET cannot encode.
        try:
            from football_tracking.api.schemas import DetectorProbeReportView

            DetectorProbeReportView.model_validate(report)
        except Exception as exc:
            raise DetectorDevelopmentError(
                "invalid_probe_report",
                "Detector probe report does not satisfy the public evidence contract",
            ) from exc

        frozen = record["frozen_request"]
        legacy_timing_absent = self._is_audited_t2_legacy_report(report, frozen)
        expected_frames = list(frozen["frame_indices"])
        expected_profiles = list(frozen["profile_ids"])
        frozen_profiles = {profile["profile_id"]: profile for profile in record["frozen_profiles"]}
        expected_source = {
            "source_id": frozen["source_id"],
            "relative_path": frozen["source_relative_path"],
            "sha256": frozen["source_sha256"],
            "file_identity_sha256": frozen["source_file_identity_sha256"],
            "size_bytes": frozen["source_size_bytes"],
            "width": frozen["source_width"],
            "height": frozen["source_height"],
            "frame_count": frozen["source_frame_count"],
            "tracking_contract_relative_path": frozen["tracking_contract_relative_path"],
            "tracking_contract_sha256": frozen["tracking_contract_sha256"],
        }
        expected_lineage = {
            "parent_trial_id": frozen["parent_trial_id"],
            "base_config_relative_path": frozen["base_config_relative_path"],
            "base_config_sha256": frozen["base_config_sha256"],
            "effective_config_relative_path": frozen["effective_config_relative_path"],
            "effective_config_sha256": frozen["effective_config_sha256"],
            "trial_intent_sha256": frozen["trial_intent_sha256"],
            "tuning_patch_binding": frozen["tuning_patch_binding"],
            "tuning_patch_sha256": frozen["tuning_patch_sha256"],
            "profile_sha256s": frozen["profile_sha256s"],
            "frozen_profiles_sha256": frozen["frozen_profiles_sha256"],
            "execution_bundle": frozen["execution_bundle"],
            "execution_bundle_sha256": frozen["execution_bundle_sha256"],
            "runtime_environment_sha256": frozen["runtime_environment_sha256"],
            "intent_sha256": record["intent_sha256"],
            "retry_from_job_id": record.get("retry_from_job_id"),
        }
        optional_lineage = {
            "semantic_intent_sha256": record.get("semantic_intent_sha256"),
            "retry_kind": frozen.get("retry_kind"),
            "review_proxy_upgrade": frozen.get("review_proxy_upgrade"),
        }
        lineage = report.get("lineage")
        lineage_matches = isinstance(lineage, dict) and all(
            lineage.get(key) == value for key, value in expected_lineage.items()
        )
        if lineage_matches:
            for key, value in optional_lineage.items():
                if value is not None and lineage.get(key) != value:
                    lineage_matches = False
                    break
                if value is None and key in lineage and lineage[key] is not None:
                    lineage_matches = False
                    break
            if set(lineage) - set(expected_lineage) - set(optional_lineage):
                lineage_matches = False
        decode = report.get("decode")
        execution = report.get("execution")
        timing_observations = decode.get("frame_timing_observations") if isinstance(decode, dict) else None
        expected_position_verification = (
            "verified_review_proxy_frame_index_mapping_v1"
            if report.get("review_proxy_manifest") is not None
            else "opencv_next_frame_index_with_0.25_tolerance"
        )
        raw_proxy_mappings = (
            report.get("review_proxy_manifest", {}).get("mappings")
            if isinstance(report.get("review_proxy_manifest"), dict)
            else None
        )
        proxy_source_timing_not_collected = bool(
            isinstance(raw_proxy_mappings, list)
            and raw_proxy_mappings
            and all(
                isinstance(item, dict)
                and item.get("source_timing_status") == "not_collected"
                and item.get("source_decoder_pos_msec") is None
                for item in raw_proxy_mappings
            )
        )
        if (
            report.get("source") != expected_source
            or not lineage_matches
            or report.get("frozen_profiles") != record["frozen_profiles"]
            or report.get("top_k") != 5
            or not isinstance(decode, dict)
            or decode.get("width") != frozen["source_width"]
            or decode.get("height") != frozen["source_height"]
            or decode.get("frame_count") != frozen["source_frame_count"]
            or decode.get("requested_decode_mode") != frozen["requested_decode_mode"]
            or decode.get("verified_frame_indices") != expected_frames
            or decode.get("position_verification") != expected_position_verification
            or (legacy_timing_absent and (timing_observations is not None or "frame_timing_observations" in decode))
            or (
                not legacy_timing_absent
                and not proxy_source_timing_not_collected
                and (
                    not isinstance(timing_observations, list)
                    or [item.get("frame_index") for item in timing_observations if isinstance(item, dict)]
                    != expected_frames
                )
            )
            or (proxy_source_timing_not_collected and timing_observations is not None)
            or not isinstance(execution, dict)
            or execution.get("device") != frozen["execution_bundle"]["execution_environment"]["device"]
            or execution.get("precision") != frozen["execution_bundle"]["execution_environment"]["precision"]
        ):
            raise DetectorDevelopmentError(
                "invalid_probe_report", "Detector probe report frozen lineage is inconsistent"
            )
        effective_mode = decode.get("effective_decode_mode")
        allowed_effective_modes = {
            "sequential": {"sequential"},
            "preroll": {"preroll_verified", "sequential_fallback"},
            "direct": {"direct_verified", "sequential_fallback"},
        }
        if effective_mode not in allowed_effective_modes[frozen["requested_decode_mode"]]:
            raise DetectorDevelopmentError(
                "invalid_probe_report", "Detector probe decode mode evidence is inconsistent"
            )

        frames = report.get("frames")
        if not isinstance(frames, list) or [row.get("frame_index") for row in frames] != expected_frames:
            raise DetectorDevelopmentError("invalid_probe_report", "Detector probe report frame set is inconsistent")
        review_proxy_manifest = report.get("review_proxy_manifest")
        if review_proxy_manifest is not None:
            try:
                verified_proxy = validate_review_proxy_manifest(review_proxy_manifest)
            except (ReviewProxyError, DetectorDevelopmentError) as exc:
                raise DetectorDevelopmentError(
                    "invalid_probe_report",
                    "Detector probe review proxy manifest is invalid",
                ) from exc
            execution_environment = frozen["execution_bundle"].get("execution_environment", {})
            upgrade_binding = frozen.get("review_proxy_upgrade")
            expected_decoder_fingerprint = (
                upgrade_binding["repair_evidence"]["repair_decoder_fingerprint_sha256"]
                if isinstance(upgrade_binding, dict)
                else execution_environment.get("decoder_fingerprint_sha256")
            )
            proxy_source = verified_proxy["source"]
            if (
                verified_proxy["expected_frame_indices"] != expected_frames
                or verified_proxy["requested_decode_mode"] != frozen["requested_decode_mode"]
                or verified_proxy["effective_decode_mode"] != effective_mode
                or verified_proxy["decoder_fingerprint_sha256"] != expected_decoder_fingerprint
                or proxy_source["sha256"] != expected_source["sha256"]
                or proxy_source["file_identity_sha256"] != expected_source["file_identity_sha256"]
                or proxy_source["size_bytes"] != expected_source["size_bytes"]
                or proxy_source["width"] != expected_source["width"]
                or proxy_source["height"] != expected_source["height"]
                or proxy_source["frame_count"] != expected_source["frame_count"]
                or not math.isclose(
                    proxy_source["fps"],
                    float(decode.get("fps")),
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
            ):
                raise DetectorDevelopmentError(
                    "invalid_probe_report",
                    "Detector probe review proxy authority changed source/decode lineage",
                )
            mappings_by_frame = {item["source_frame_index"]: item for item in verified_proxy["mappings"]}
            if any(
                mappings_by_frame[row["frame_index"]]["source_frame_sha256"] != row.get("source_frame_sha256")
                or (
                    mappings_by_frame[row["frame_index"]]["source_timing_status"] == "observed"
                    and not math.isclose(
                        mappings_by_frame[row["frame_index"]]["source_decoder_pos_msec"],
                        float(row.get("decoder_reported_pos_msec")),
                        rel_tol=0.0,
                        abs_tol=verified_proxy["map_time_tolerance_msec"],
                    )
                )
                or (
                    mappings_by_frame[row["frame_index"]]["source_timing_status"] == "not_collected"
                    and row.get("decoder_reported_pos_msec") is not None
                )
                for row in frames
            ):
                raise DetectorDevelopmentError(
                    "invalid_probe_report",
                    "Detector probe review proxy frame map changed exact source evidence",
                )
        artifacts = report.get("artifacts")
        if not isinstance(artifacts, list):
            raise DetectorDevelopmentError("invalid_probe_report", "Detector probe report artifacts are invalid")
        artifacts_by_id = {item.get("artifact_id"): item for item in artifacts if isinstance(item, dict)}
        relative_paths = {item.get("relative_path") for item in artifacts if isinstance(item, dict)}
        if (
            len(artifacts_by_id) != len(artifacts)
            or len(relative_paths) != len(artifacts)
            or len(artifacts)
            != len(expected_frames) * (len(expected_profiles) + 1 + (1 if review_proxy_manifest is not None else 0))
        ):
            raise DetectorDevelopmentError(
                "invalid_probe_report", "Detector probe artifact references are not one-to-one"
            )
        referenced: set[str] = set()
        url_prefix = f"/api/v1/detector-probes/{record['job_id']}/artifacts/"
        timing_rows: list[dict[str, Any] | None]
        if legacy_timing_absent or proxy_source_timing_not_collected:
            timing_rows = [None] * len(frames)
        else:
            timing_rows = timing_observations
        for row, timing in zip(frames, timing_rows, strict=True):
            integrity = row.get("media_integrity")
            decoder_pos_msec = row.get("decoder_reported_pos_msec")
            expected_timing_method = (
                "verified_review_proxy_frame_index_mapping_v1"
                if review_proxy_manifest is not None
                else "opencv_cap_prop_pos_msec_after_verified_frame_read"
            )
            if (
                row.get("source_width") != frozen["source_width"]
                or row.get("source_height") != frozen["source_height"]
                or row.get("requested_decode_mode") != frozen["requested_decode_mode"]
                or row.get("effective_decode_mode") != effective_mode
                or row.get("decoded_frame_position") != row.get("frame_index")
                or (
                    (legacy_timing_absent or proxy_source_timing_not_collected)
                    and (
                        row.get("decoder_reported_pos_msec") is not None
                        or row.get("decoder_timing_observation_method") is not None
                        or timing is not None
                    )
                )
                or (
                    not legacy_timing_absent
                    and not proxy_source_timing_not_collected
                    and (
                        isinstance(decoder_pos_msec, bool)
                        or not isinstance(decoder_pos_msec, (int, float))
                        or not math.isfinite(float(decoder_pos_msec))
                        or row.get("decoder_timing_observation_method") != expected_timing_method
                        or timing
                        != {
                            "frame_index": row.get("frame_index"),
                            "decoder_reported_pos_msec": decoder_pos_msec,
                            "observation_method": expected_timing_method,
                        }
                    )
                )
                or not isinstance(integrity, dict)
                or integrity.get("status") != "ok"
                or integrity.get("width") != frozen["source_width"]
                or integrity.get("height") != frozen["source_height"]
                or integrity.get("gray") is not False
                or integrity.get("low_information") is not False
                or integrity.get("likely_corrupt") is not False
            ):
                raise DetectorDevelopmentError("invalid_probe_report", "Detector probe frame evidence is invalid")
            source_url = row.get("source_artifact_url")
            source_artifact_id = (
                source_url[len(url_prefix) :]
                if isinstance(source_url, str) and source_url.startswith(url_prefix)
                else ""
            )
            source_artifact = artifacts_by_id.get(source_artifact_id)
            if (
                not source_artifact_id
                or source_artifact is None
                or source_artifact.get("sha256") != row.get("source_frame_sha256")
                or source_artifact.get("size_bytes") != row.get("source_frame_size_bytes")
            ):
                raise DetectorDevelopmentError(
                    "invalid_probe_report", "Detector probe source artifact binding is invalid"
                )
            referenced.add(source_artifact_id)
            proxy_url = row.get("proxy_artifact_url")
            proxy_artifact_id = (
                proxy_url[len(url_prefix) :] if isinstance(proxy_url, str) and proxy_url.startswith(url_prefix) else ""
            )
            if review_proxy_manifest is None:
                if (
                    proxy_artifact_id
                    or row.get("proxy_frame_sha256") is not None
                    or row.get("proxy_frame_size_bytes") is not None
                ):
                    raise DetectorDevelopmentError(
                        "invalid_probe_report",
                        "Direct detector probe cannot publish proxy-frame evidence",
                    )
            else:
                mapping = mappings_by_frame[row["frame_index"]]
                proxy_artifact = artifacts_by_id.get(proxy_artifact_id)
                if (
                    not proxy_artifact_id
                    or proxy_artifact is None
                    or proxy_artifact.get("sha256") != mapping["proxy_frame_sha256"]
                    or proxy_artifact.get("sha256") != row.get("proxy_frame_sha256")
                    or proxy_artifact.get("size_bytes") != row.get("proxy_frame_size_bytes")
                    or mapping["source_timing_status"] != ("not_collected" if decoder_pos_msec is None else "observed")
                    or (
                        decoder_pos_msec is not None
                        and not math.isclose(
                            float(decoder_pos_msec),
                            mapping["source_decoder_pos_msec"],
                            rel_tol=0.0,
                            abs_tol=review_proxy_manifest["map_time_tolerance_msec"],
                        )
                    )
                ):
                    raise DetectorDevelopmentError(
                        "invalid_probe_report",
                        "Detector probe proxy-frame artifact binding is invalid",
                    )
                referenced.add(proxy_artifact_id)
            profile_rows = row.get("profile_results")
            if (
                not isinstance(profile_rows, list)
                or [item.get("profile_id") for item in profile_rows] != expected_profiles
            ):
                raise DetectorDevelopmentError("invalid_probe_report", "Detector probe profile evidence is incomplete")
            for profile_row in profile_rows:
                profile_id = profile_row["profile_id"]
                frozen_profile = frozen_profiles[profile_id]
                if profile_row.get("profile_sha256") != frozen["profile_sha256s"][profile_id]:
                    raise DetectorDevelopmentError(
                        "invalid_probe_report", "Detector probe profile digest is inconsistent"
                    )
                raw_candidates = profile_row.get("raw_candidates")
                if not isinstance(raw_candidates, list):
                    raise DetectorDevelopmentError(
                        "invalid_probe_report",
                        "Detector probe raw candidate evidence is invalid",
                    )
                validated_candidates = []
                for candidate in raw_candidates:
                    self._validated_candidate(
                        candidate,
                        frame_index=row["frame_index"],
                        width=frozen["source_width"],
                        height=frozen["source_height"],
                        frozen_profile=frozen_profile,
                    )
                    validated_candidates.append(candidate)
                display_candidate = profile_row.get("display_candidate")
                if display_candidate is not None:
                    self._validated_candidate(
                        display_candidate,
                        frame_index=row["frame_index"],
                        width=frozen["source_width"],
                        height=frozen["source_height"],
                        frozen_profile=frozen_profile,
                    )
                candidate_count = profile_row.get("candidate_count")
                filter_reasons = profile_row.get("filter_reasons")
                if (
                    isinstance(candidate_count, bool)
                    or not isinstance(candidate_count, int)
                    or candidate_count < 0
                    or not isinstance(filter_reasons, dict)
                    or any(
                        not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, int) or value <= 0
                        for key, value in filter_reasons.items()
                    )
                ):
                    raise DetectorDevelopmentError(
                        "invalid_probe_report",
                        "Detector probe candidate accounting is invalid",
                    )
                deduplicated_count = candidate_count - filter_reasons.get("duplicate_suppressed_iou", 0)
                if (
                    deduplicated_count < 0
                    or len(validated_candidates) != min(deduplicated_count, 5)
                    or filter_reasons.get("top_k_limit", 0) != max(0, deduplicated_count - 5)
                    or display_candidate != (validated_candidates[0] if validated_candidates else None)
                ):
                    raise DetectorDevelopmentError(
                        "invalid_probe_report",
                        "Detector probe candidate accounting is inconsistent",
                    )
                overlay_url = profile_row.get("raw_overlay_artifact_url")
                overlay_id = (
                    overlay_url[len(url_prefix) :]
                    if isinstance(overlay_url, str) and overlay_url.startswith(url_prefix)
                    else ""
                )
                overlay = artifacts_by_id.get(overlay_id)
                if (
                    not overlay_id
                    or overlay is None
                    or overlay.get("sha256") != profile_row.get("raw_overlay_sha256")
                    or overlay.get("size_bytes") != profile_row.get("raw_overlay_size_bytes")
                ):
                    raise DetectorDevelopmentError("invalid_probe_report", "Detector probe overlay binding is invalid")
                referenced.add(overlay_id)
        if referenced != set(artifacts_by_id):
            raise DetectorDevelopmentError("invalid_probe_report", "Detector probe report has unreferenced artifacts")
        if isinstance(frozen.get("review_proxy_upgrade"), dict):
            self._validate_review_proxy_child_inherited_evidence(
                report,
                frozen["review_proxy_upgrade"],
            )

    def _validate_review_proxy_child_inherited_evidence(
        self,
        child_report: dict[str, Any],
        upgrade: dict[str, Any],
    ) -> None:
        """Replay the audited parent behind a proxy-upgrade child.

        Producer URLs legitimately move into the child result tree.  Every
        semantic candidate row and every copied source/overlay digest and size
        must otherwise remain identical to the freshly verified parent.
        """

        inherited = upgrade.get("inherited_evidence")
        if not isinstance(inherited, dict):
            raise DetectorDevelopmentError(
                "invalid_probe_report",
                "Review-proxy child inherited evidence is missing",
            )
        parent_id = require_safe_id(
            inherited.get("parent_probe_job_id"),
            "inherited parent probe job_id",
        )
        actual_parent = self.get_review_proxy_upgrade_parent(parent_id)
        inherited_fields = (
            "parent_probe_job_id",
            "parent_probe_request_sha256",
            "parent_probe_intent_sha256",
            "parent_probe_semantic_intent_sha256",
            "parent_probe_report_sha256",
            "parent_probe_result_manifest_sha256",
            "parent_probe_record_sha256",
            "parent_execution_bundle_sha256",
            "parent_runtime_environment_sha256",
            "source_frame_evidence_sha256",
            "candidate_evidence_sha256",
        )
        if any(inherited.get(field) != actual_parent.get(field) for field in inherited_fields):
            raise DetectorDevelopmentError(
                "historical_parent_changed",
                "Review-proxy child inherited digests differ from its audited parent",
            )
        verified_parent = self.get_verified_probe(parent_id)
        parent_report = verified_parent.get("report")
        parent_frames = parent_report.get("frames") if isinstance(parent_report, dict) else None
        child_frames = child_report.get("frames")
        if not isinstance(parent_frames, list) or not isinstance(child_frames, list):
            raise DetectorDevelopmentError(
                "invalid_probe_report",
                "Review-proxy inherited frame evidence is invalid",
            )
        parent_by_index = {row.get("frame_index"): row for row in parent_frames if isinstance(row, dict)}
        if len(parent_by_index) != len(parent_frames) or [row.get("frame_index") for row in child_frames] != [
            row.get("frame_index") for row in parent_frames
        ]:
            raise DetectorDevelopmentError(
                "historical_parent_changed",
                "Review-proxy child frame set differs from its audited parent",
            )

        def semantic_profile(row: Any) -> dict[str, Any] | None:
            if not isinstance(row, dict):
                return None
            value = deepcopy(row)
            value.pop("raw_overlay_artifact_url", None)
            return value

        for child in child_frames:
            parent = parent_by_index[child["frame_index"]]
            if any(
                child.get(field) != parent.get(field)
                for field in (
                    "frame_index",
                    "source_width",
                    "source_height",
                    "source_frame_sha256",
                    "source_frame_size_bytes",
                    "media_integrity",
                )
            ):
                raise DetectorDevelopmentError(
                    "historical_parent_changed",
                    "Review-proxy child source-frame evidence differs from its audited parent",
                )
            child_profiles = child.get("profile_results")
            parent_profiles = parent.get("profile_results")
            if (
                not isinstance(child_profiles, list)
                or not isinstance(parent_profiles, list)
                or [semantic_profile(row) for row in child_profiles]
                != [semantic_profile(row) for row in parent_profiles]
            ):
                raise DetectorDevelopmentError(
                    "historical_parent_changed",
                    "Review-proxy child candidate evidence differs from its audited parent",
                )

    def _validate_result_tree(
        self,
        root: Path,
        record: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        report, manifest, manifest_sha256 = self._read_result_documents(root, record)

        expected_files = {
            "detector_probe_manifest.v1.json",
            "detector_probe_report.v1.json",
        }
        seen_ids: set[str] = set()
        for artifact in manifest["artifacts"]:
            if not isinstance(artifact, dict):
                raise DetectorDevelopmentError("invalid_result_manifest", "Detector probe artifact manifest is invalid")
            artifact_id = require_safe_id(artifact.get("artifact_id"), "detector probe artifact_id")
            if artifact_id in seen_ids:
                raise DetectorDevelopmentError(
                    "invalid_result_manifest", "Detector probe artifact allowlist is invalid"
                )
            seen_ids.add(artifact_id)
            path = require_trusted_relative_path(
                root,
                artifact.get("relative_path"),
                "detector probe result artifact",
                allowed_first_parts={"frames", "overlays", "proxy_frames"},
            )
            expected_files.add(path.relative_to(root).as_posix())
        try:
            tree_before = exact_regular_tree_snapshot(
                root,
                expected_files,
                "detector probe result tree",
                trusted_root=self._results_root,
            )
        except DetectorDevelopmentError as exc:
            if exc.code in {
                "unexpected_result_artifact",
                "invalid_result_allowlist",
            }:
                raise DetectorDevelopmentError(
                    "artifact_allowlist_mismatch",
                    "Detector probe result contains missing or unlisted files",
                ) from exc
            raise
        repeated_report, repeated_manifest, repeated_manifest_sha256 = self._read_result_documents(root, record)
        if repeated_report != report or repeated_manifest != manifest or repeated_manifest_sha256 != manifest_sha256:
            raise DetectorDevelopmentError(
                "source_changed",
                "Detector probe result documents changed during validation",
            )
        for artifact in repeated_manifest["artifacts"]:
            self._validate_result_artifact(root, artifact, record)
        try:
            tree_after = exact_regular_tree_snapshot(
                root,
                expected_files,
                "detector probe result tree",
                trusted_root=self._results_root,
            )
        except DetectorDevelopmentError as exc:
            if exc.code in {
                "unexpected_result_artifact",
                "invalid_result_allowlist",
            }:
                raise DetectorDevelopmentError(
                    "artifact_allowlist_mismatch",
                    "Detector probe result contains missing or unlisted files",
                ) from exc
            raise
        if tree_after != tree_before:
            raise DetectorDevelopmentError(
                "source_changed",
                "Detector probe result tree changed during validation",
            )
        return report, manifest_sha256

    def _read_result_documents(
        self,
        root: Path,
        record: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        if is_link_or_reparse(root) or not root.is_dir():
            raise DetectorDevelopmentError("result_unavailable", "Detector probe result directory is unavailable")
        manifest_path = require_trusted_relative_path(
            root,
            "detector_probe_manifest.v1.json",
            "detector probe result manifest",
        )
        manifest_bytes, manifest_sha256 = read_regular_bytes(
            manifest_path,
            "detector probe result manifest",
            max_bytes=4 * 1024 * 1024,
            trusted_root=root,
        )
        manifest = json_object_from_bytes(manifest_bytes, "detector probe result manifest")
        if (
            manifest.get("schema_version") != "1.0"
            or manifest.get("artifact_type") != "detector_probe_result_manifest"
            or manifest.get("job_id") != record.get("job_id")
            or manifest.get("request_sha256") != record.get("request_sha256")
            or manifest.get("frozen_profiles_sha256") != record.get("frozen_request", {}).get("frozen_profiles_sha256")
            or manifest.get("execution_bundle_sha256")
            != record.get("frozen_request", {}).get("execution_bundle_sha256")
            or manifest.get("runtime_environment_sha256")
            != record.get("frozen_request", {}).get("runtime_environment_sha256")
            or manifest.get("source_file_identity_sha256")
            != record.get("frozen_request", {}).get("source_file_identity_sha256")
            or not isinstance(manifest.get("artifacts"), list)
        ):
            raise DetectorDevelopmentError(
                "invalid_result_manifest", "Detector probe result manifest lineage is invalid"
            )
        report_path = require_trusted_relative_path(
            root,
            "detector_probe_report.v1.json",
            "detector probe report",
        )
        report_bytes, report_file_sha256 = read_regular_bytes(
            report_path,
            "detector probe report",
            max_bytes=32 * 1024 * 1024,
            trusted_root=root,
        )
        report = json_object_from_bytes(report_bytes, "detector probe report")
        if (
            report_file_sha256 != manifest.get("report_file_sha256")
            or len(report_bytes) != manifest.get("report_file_size_bytes")
            or report.get("schema_version") != "1.0"
            or report.get("artifact_type") != "detector_probe_report"
            or report.get("job_id") != record.get("job_id")
            or report.get("request_sha256") != record.get("request_sha256")
        ):
            raise DetectorDevelopmentError("invalid_probe_report", "Detector probe report lineage or bytes are invalid")
        reported_content_sha256 = report.pop("report_sha256", None)
        actual_content_sha256 = canonical_sha256(report)
        report["report_sha256"] = reported_content_sha256
        if (
            reported_content_sha256 != actual_content_sha256
            or manifest.get("report_content_sha256") != actual_content_sha256
            or report.get("artifacts") != manifest.get("artifacts")
        ):
            raise DetectorDevelopmentError("invalid_probe_report", "Detector probe report digest is invalid")

        self._validate_report_contract(report, record)
        return report, manifest, manifest_sha256

    def _validate_result_artifact(
        self,
        root: Path,
        artifact: dict[str, Any],
        record: dict[str, Any],
    ) -> tuple[Path, str, bytes]:
        require_safe_id(artifact.get("artifact_id"), "detector probe artifact_id")
        if artifact.get("media_type") != "image/jpeg":
            raise DetectorDevelopmentError("invalid_result_manifest", "Detector probe artifact allowlist is invalid")
        path = require_trusted_relative_path(
            root,
            artifact.get("relative_path"),
            "detector probe result artifact",
            allowed_first_parts={"frames", "overlays", "proxy_frames"},
        )
        content, digest = read_regular_bytes(
            path,
            "detector probe result artifact",
            max_bytes=_MAX_ARTIFACT_BYTES,
            trusted_root=root,
        )
        if not content.startswith(b"\xff\xd8\xff") or not content.endswith(b"\xff\xd9"):
            raise DetectorDevelopmentError("corrupt_frame", "Detector probe result artifact is not a complete JPEG")
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or getattr(image, "size", 0) == 0:
            raise DetectorDevelopmentError("corrupt_frame", "Detector probe result artifact is not decodable")
        height, width = image.shape[:2]
        proxy_artifact = path.relative_to(root).parts[0] == "proxy_frames"
        expected_width = 2560 if proxy_artifact else record["frozen_request"]["source_width"]
        expected_height = 720 if proxy_artifact else record["frozen_request"]["source_height"]
        if (
            digest != artifact.get("sha256")
            or len(content) != artifact.get("size_bytes")
            or width != expected_width
            or height != expected_height
            or width != artifact.get("width")
            or height != artifact.get("height")
        ):
            raise DetectorDevelopmentError("artifact_digest_mismatch", "Detector probe artifact bytes are invalid")
        return path, digest, content

    def _record_failure(self, job_id: str, exc: Exception) -> None:
        status, code, recovery_action = self._classify_failure(exc)
        with self._lock:
            record = self._record(job_id)
            if record.get("status") == "ready":
                return
            if record.get("status") not in {"running", "committing"} or record.get("owner_id") != self._owner_id:
                return
            if record.get("cancel_requested") is True:
                self._mark_cancelled(record)
                return
            now = utc_now_iso()
            record.update(
                {
                    "status": status,
                    "stage": status,
                    "owner_id": None,
                    "cancel_requested": status == "cancelled",
                    "error_code": code if status in {"failed", "cancelled"} else None,
                    "blocker_code": code if status == "blocked" else None,
                    "error_message": str(exc)[:1000],
                    "recovery_action": recovery_action,
                    "report": None,
                    "result_manifest_sha256": None,
                    "updated_at": now,
                }
            )
            record["progress"]["updated_at"] = now
            self._persist_record(record)

    def _requeue_after_shutdown(self, job_id: str) -> None:
        """Release only this owner's running claim without forging cancellation."""

        with self._lock:
            record = self._record(job_id)
            if record.get("status") != "running" or record.get("owner_id") != self._owner_id:
                return
            if record.get("cancel_requested") is True:
                self._mark_cancelled(record)
                return
            now = utc_now_iso()
            record.update(
                {
                    "status": "queued",
                    "stage": "recovered_after_shutdown",
                    "owner_id": None,
                    "cancel_requested": False,
                    "error_code": None,
                    "blocker_code": None,
                    "error_message": None,
                    "recovery_action": None,
                    "report": None,
                    "result_manifest_sha256": None,
                    "updated_at": now,
                }
            )
            record["progress"].update({"completed": 0, "updated_at": now})
            self._persist_record(record)

    @staticmethod
    def _classify_failure(exc: Exception) -> tuple[str, str, str]:
        current: BaseException | None = exc
        while current is not None:
            if isinstance(current, OSError) and current.errno == errno.ENOSPC:
                return "failed", "disk_exhausted", "free_disk_space"
            current = current.__cause__ or current.__context__
        if isinstance(exc, DetectorDevelopmentError):
            if exc.code == "cancelled":
                return "cancelled", "cancelled", "retry"
            if exc.code in {
                "source_changed",
                "source_digest_mismatch",
                "tracking_contract_changed",
                "base_config_changed",
                "effective_config_changed",
                "weights_digest_or_size_mismatch",
                "weights_snapshot_changed",
                "probe_ownership_lost",
                "profile_unavailable",
                "invalid_registry",
                "runtime_environment_changed",
            }:
                return "blocked", exc.code, "refresh_lineage"
            if exc.code == "insufficient_probe_disk_capacity":
                return "failed", exc.code, "free_disk_space"
            if exc.code == "disk_exhausted":
                return "failed", exc.code, "free_disk_space"
            if exc.code == "device_out_of_memory":
                return "failed", exc.code, "reduce_probe_or_use_cpu"
            if exc.code == "corrupt_frame":
                return "failed", exc.code, "repair_review_source"
            if exc.code == "probe_memory_envelope_exceeded":
                return "failed", exc.code, "reduce_probe_or_use_cpu"
            return "failed", exc.code, "retry"
        if isinstance(exc, MemoryError):
            return "failed", "device_out_of_memory", "reduce_probe_or_use_cpu"
        if _is_torch_cuda_out_of_memory(exc):
            return "failed", "device_out_of_memory", "reduce_probe_or_use_cpu"
        if isinstance(exc, ProbeWorkerDiedError):
            return "failed", "worker_died", "retry"
        if isinstance(exc, CorruptProbeFrameError):
            return "failed", "corrupt_frame", "repair_review_source"
        if isinstance(exc, ArtifactWriteError):
            return "failed", "artifact_write_failed", "retry"
        return "failed", "probe_failed", "retry"

    def _mark_cancelled(self, record: dict[str, Any]) -> None:
        now = utc_now_iso()
        record.update(
            {
                "status": "cancelled",
                "stage": "cancelled",
                "owner_id": None,
                "cancel_requested": True,
                "error_code": "cancelled",
                "blocker_code": None,
                "recovery_action": "retry",
                "report": None,
                "result_manifest_sha256": None,
                "updated_at": now,
            }
        )
        record["progress"]["updated_at"] = now
        self._persist_record(record)

    def _validated_tree_files(self, root: Path) -> set[str]:
        root = Path(os.path.abspath(root))
        if root.parent != self._results_root or is_link_or_reparse(root):
            raise DetectorDevelopmentError(
                "unsafe_result_tree", "Detector probe result tree is outside its trusted root"
            )
        try:
            root_metadata = root.stat()
        except OSError as exc:
            raise DetectorDevelopmentError("result_unavailable", "Detector probe result tree is unavailable") from exc
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise DetectorDevelopmentError("unsafe_result_tree", "Detector probe result tree is not a directory")
        root_identity = (int(root_metadata.st_dev), int(root_metadata.st_ino))
        files: set[str] = set()

        def walk(directory: Path) -> None:
            if is_link_or_reparse(directory):
                raise DetectorDevelopmentError(
                    "unsafe_result_tree", "Detector probe result tree contains a link or reparse point"
                )
            try:
                with os.scandir(directory) as entries:
                    snapshot = list(entries)
            except OSError as exc:
                raise DetectorDevelopmentError(
                    "result_unavailable", "Detector probe result tree could not be enumerated"
                ) from exc
            for entry in snapshot:
                path = Path(entry.path)
                if is_link_or_reparse(path):
                    raise DetectorDevelopmentError(
                        "unsafe_result_tree",
                        "Detector probe result tree contains a link or reparse point",
                    )
                if entry.is_dir(follow_symlinks=False):
                    walk(path)
                elif entry.is_file(follow_symlinks=False):
                    files.add(path.relative_to(root).as_posix())
                else:
                    raise DetectorDevelopmentError(
                        "unsafe_result_tree", "Detector probe result tree contains a special file"
                    )

        walk(root)
        current = root.stat()
        if (int(current.st_dev), int(current.st_ino)) != root_identity:
            raise DetectorDevelopmentError("source_changed", "Detector probe result tree changed during validation")
        return files

    def _remove_tree(self, path: Path, *, trusted_parent: Path | None = None) -> None:
        candidate = Path(os.path.abspath(path))
        parent = self._results_root if trusted_parent is None else Path(os.path.abspath(trusted_parent))
        if candidate.parent != parent:
            return
        if _path_entry_is_absent(candidate):
            return
        try:
            if is_link_or_reparse(candidate):
                self._unlink_link_object(candidate)
                return
            directories: list[tuple[Path, tuple[int, int]]] = []
            files: list[Path] = []
            links: list[Path] = []

            def snapshot(directory: Path) -> None:
                if is_link_or_reparse(directory):
                    raise DetectorDevelopmentError(
                        "unsafe_result_tree", "Refusing to clean a linked detector probe tree"
                    )
                metadata = directory.stat()
                if not stat.S_ISDIR(metadata.st_mode):
                    raise DetectorDevelopmentError("unsafe_result_tree", "Refusing to clean a non-directory probe tree")
                directories.append((directory, (int(metadata.st_dev), int(metadata.st_ino))))
                with os.scandir(directory) as entries:
                    children = list(entries)
                for entry in children:
                    child = Path(entry.path)
                    if is_link_or_reparse(child):
                        links.append(child)
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        snapshot(child)
                    elif entry.is_file(follow_symlinks=False):
                        files.append(child)
                    else:
                        raise DetectorDevelopmentError(
                            "unsafe_result_tree",
                            "Refusing to clean a detector probe tree containing a special file",
                        )

            snapshot(candidate)
            for file_path in files:
                if is_link_or_reparse(file_path):
                    self._unlink_link_object(file_path)
                    continue
                file_path.unlink()
            for link_path in reversed(links):
                if not is_link_or_reparse(link_path):
                    return
                self._unlink_link_object(link_path)
            for directory, identity in reversed(directories):
                if is_link_or_reparse(directory):
                    return
                metadata = directory.stat()
                if (int(metadata.st_dev), int(metadata.st_ino)) != identity:
                    return
                directory.rmdir()
        except (DetectorDevelopmentError, OSError):
            # Unsafe or raced trees remain unpublished and are never traversed further.
            return

    @staticmethod
    def _unlink_link_object(path: Path) -> None:
        metadata = path.lstat()
        if not is_link_or_reparse(path):
            raise DetectorDevelopmentError("unsafe_result_tree", "Detector probe cleanup target changed identity")
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            os.rmdir(path)
        else:
            path.unlink()

    def _freeze_request(
        self,
        request: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Freeze a trusted internal request already resolved from the parent trial.

        Public API callers must never supply source/config/contract paths or digests as
        authority.  The API service resolves those values from the authoritative parent
        run and passes the complete lineage here for byte-level re-verification.
        """

        if not isinstance(request, dict):
            raise DetectorDevelopmentError(
                "invalid_probe_request", "Detector probe request must be an object", status_code=400
            )
        unknown = set(request) - _REQUEST_FIELDS
        if unknown:
            raise DetectorDevelopmentError(
                "invalid_probe_request",
                f"Detector probe request contains unsupported fields: {', '.join(sorted(unknown))}",
                status_code=400,
            )
        parent_trial_id = require_safe_id(request.get("parent_trial_id"), "parent_trial_id")
        source_id = require_safe_id(request.get("source_id"), "source_id")
        source_path = require_trusted_relative_path(
            self.repo_root,
            request.get("source_relative_path"),
            "detector probe source",
            allowed_first_parts={"data"},
        )
        source_relative_path = source_path.relative_to(self.repo_root).as_posix()
        source_sha256 = require_sha256(request.get("source_sha256"), "source_sha256")
        source_file_identity_sha256, source_size_bytes = _source_file_identity(self.repo_root, source_path)

        contract_path = require_trusted_relative_path(
            self.repo_root,
            request.get("tracking_contract_relative_path"),
            "detector probe tracking contract",
            allowed_first_parts={"outputs"},
        )
        if contract_path.name != "tracking_contract.v2.json" or parent_trial_id not in contract_path.parts:
            raise DetectorDevelopmentError(
                "invalid_tracking_contract",
                "Detector probe tracking contract must be the parent trial tracking_contract.v2.json",
                status_code=400,
            )
        contract_bytes, contract_sha256 = read_regular_bytes(
            contract_path,
            "detector probe tracking contract",
            max_bytes=32 * 1024 * 1024,
            trusted_root=self.repo_root,
        )
        if contract_sha256 != require_sha256(request.get("tracking_contract_sha256"), "tracking_contract_sha256"):
            raise DetectorDevelopmentError(
                "tracking_contract_digest_mismatch",
                "Detector probe tracking contract bytes do not match tracking_contract_sha256",
            )
        contract = json_object_from_bytes(contract_bytes, "detector probe tracking contract")
        source_binding = contract.get("source")
        if contract.get("schema_version") != "2.0" or not isinstance(source_binding, dict):
            raise DetectorDevelopmentError(
                "invalid_tracking_contract", "Detector probe requires tracking contract schema 2.0"
            )
        if source_binding.get("video_sha256") != source_sha256:
            raise DetectorDevelopmentError(
                "tracking_contract_source_mismatch",
                "Tracking contract is not bound to the detector probe source",
            )
        source_width = self._positive_int(source_binding.get("width"), "source width")
        source_height = self._positive_int(source_binding.get("height"), "source height")
        source_frame_count = self._positive_int(source_binding.get("frame_count"), "source frame count")

        base_config_path = require_trusted_relative_path(
            self.repo_root,
            request.get("base_config_relative_path"),
            "detector probe base config",
            allowed_first_parts={"config"},
        )
        base_config_sha256 = require_sha256(request.get("base_config_sha256"), "base_config_sha256")
        actual_base_config_sha256, _ = hash_regular_file(
            base_config_path,
            "detector probe base config",
            trusted_root=self.repo_root / "config",
        )
        if actual_base_config_sha256 != base_config_sha256:
            raise DetectorDevelopmentError(
                "base_config_digest_mismatch",
                "Detector probe base config bytes do not match base_config_sha256",
            )
        effective_config_path = require_trusted_relative_path(
            self.repo_root,
            request.get("effective_config_relative_path"),
            "detector probe effective config",
            allowed_first_parts={"config"},
        )
        effective_config_sha256 = require_sha256(request.get("effective_config_sha256"), "effective_config_sha256")
        actual_effective_config_sha256, _ = hash_regular_file(
            effective_config_path,
            "detector probe effective config",
            trusted_root=self.repo_root / "config",
        )
        if actual_effective_config_sha256 != effective_config_sha256:
            raise DetectorDevelopmentError(
                "effective_config_digest_mismatch",
                "Detector probe effective config bytes do not match effective_config_sha256",
            )
        trial_intent_sha256 = require_sha256(request.get("trial_intent_sha256"), "trial_intent_sha256")
        tuning_patch_binding = _validated_tuning_patch_binding(request.get("tuning_patch_binding"))
        tuning_patch_sha256 = require_sha256(request.get("tuning_patch_sha256"), "tuning_patch_sha256")
        if canonical_sha256(tuning_patch_binding) != tuning_patch_sha256:
            raise DetectorDevelopmentError(
                "tuning_patch_digest_mismatch",
                "Detector probe tuning-patch binding does not match tuning_patch_sha256",
                status_code=400,
            )
        profile_ids = request.get("profile_ids")
        if (
            not isinstance(profile_ids, list)
            or not 2 <= len(profile_ids) <= 6
            or any(not isinstance(item, str) for item in profile_ids)
            or len(set(profile_ids)) != len(profile_ids)
        ):
            raise DetectorDevelopmentError(
                "invalid_profile_set",
                "Detector probes require two to six unique exact profiles",
                status_code=400,
            )
        ordered_profile_ids = sorted(require_safe_id(item, "detector profile_id") for item in profile_ids)
        catalog = self._catalog_provider()
        frozen_profiles = self._selected_frozen_profiles(catalog, ordered_profile_ids)
        frozen_profiles_sha256 = canonical_sha256(frozen_profiles)
        execution_bundle = self._build_execution_bundle(catalog, frozen_profiles)
        execution_bundle_sha256 = canonical_sha256(execution_bundle)

        raw_frame_indices = request.get("frame_indices")
        if raw_frame_indices is None:
            contract_frames = contract.get("frames")
            if not isinstance(contract_frames, list):
                contract_frames = []
            raw_frame_indices = [item.get("frame_index") for item in contract_frames if isinstance(item, dict)]
        if not isinstance(raw_frame_indices, list):
            raise DetectorDevelopmentError(
                "invalid_frame_set", "Detector probe frame_indices must be a list", status_code=400
            )
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0 or item >= source_frame_count
            for item in raw_frame_indices
        ):
            raise DetectorDevelopmentError(
                "invalid_frame_set", "Detector probe frames must exist in the source", status_code=400
            )
        frame_indices = sorted(set(raw_frame_indices))
        if not 1 <= len(frame_indices) <= 50:
            raise DetectorDevelopmentError(
                "invalid_frame_set", "Detector probes require one to fifty unique frames", status_code=400
            )
        top_k = request.get("top_k", 5)
        if isinstance(top_k, bool) or top_k != 5:
            raise DetectorDevelopmentError("invalid_top_k", "Detector probe top_k is fixed at 5", status_code=400)
        requested_decode_mode = request.get("requested_decode_mode", "sequential")
        if requested_decode_mode not in {"sequential", "preroll", "direct"}:
            raise DetectorDevelopmentError(
                "invalid_decode_mode", "Detector probe decode mode is invalid", status_code=400
            )
        retry_from_job_id = request.get("retry_from_job_id")
        if retry_from_job_id is not None:
            retry_from_job_id = require_safe_id(retry_from_job_id, "retry_from_job_id")
        annotation_sampling_manifest_sha256 = request.get("annotation_sampling_manifest_sha256")
        if annotation_sampling_manifest_sha256 is not None:
            annotation_sampling_manifest_sha256 = require_sha256(
                annotation_sampling_manifest_sha256,
                "annotation_sampling_manifest_sha256",
            )

        final_source_identity, final_source_size = _source_file_identity(self.repo_root, source_path)
        if final_source_identity != source_file_identity_sha256 or final_source_size != source_size_bytes:
            raise DetectorDevelopmentError(
                "source_changed", "Detector probe source changed while the request was frozen"
            )
        frozen: dict[str, Any] = {
            "parent_trial_id": parent_trial_id,
            "source_id": source_id,
            "source_relative_path": source_relative_path,
            "source_sha256": source_sha256,
            "source_file_identity_sha256": source_file_identity_sha256,
            "source_size_bytes": source_size_bytes,
            "source_width": source_width,
            "source_height": source_height,
            "source_frame_count": source_frame_count,
            "tracking_contract_relative_path": contract_path.relative_to(self.repo_root).as_posix(),
            "tracking_contract_sha256": contract_sha256,
            "base_config_relative_path": base_config_path.relative_to(self.repo_root).as_posix(),
            "base_config_sha256": base_config_sha256,
            "effective_config_relative_path": effective_config_path.relative_to(self.repo_root).as_posix(),
            "effective_config_sha256": effective_config_sha256,
            "trial_intent_sha256": trial_intent_sha256,
            "tuning_patch_binding": tuning_patch_binding,
            "tuning_patch_sha256": tuning_patch_sha256,
            "profile_ids": ordered_profile_ids,
            "frozen_profiles_sha256": frozen_profiles_sha256,
            "profile_sha256s": {profile["profile_id"]: profile["profile_sha256"] for profile in frozen_profiles},
            "profile_bindings": [
                {
                    "profile_id": profile["profile_id"],
                    "profile_sha256": profile["profile_sha256"],
                    "model_id": profile["model_id"],
                    "model_version": profile["model_version"],
                    "model_descriptor_sha256": profile["model_descriptor_sha256"],
                    "weights_sha256": profile["model_descriptor"]["weights"]["sha256"],
                    "weights_size_bytes": profile["model_descriptor"]["weights"]["size_bytes"],
                }
                for profile in frozen_profiles
            ],
            "execution_bundle": execution_bundle,
            "execution_bundle_sha256": execution_bundle_sha256,
            "runtime_environment_sha256": execution_bundle["runtime_environment_sha256"],
            "frame_indices": frame_indices,
            "top_k": 5,
            "requested_decode_mode": requested_decode_mode,
        }
        if retry_from_job_id is not None:
            frozen["retry_from_job_id"] = retry_from_job_id
        if annotation_sampling_manifest_sha256 is not None:
            frozen["annotation_sampling_manifest_sha256"] = annotation_sampling_manifest_sha256
        return frozen, frozen_profiles

    @staticmethod
    def _selected_frozen_profiles(
        catalog: dict[str, Any],
        ordered_profile_ids: list[str],
    ) -> list[dict[str, Any]]:
        frozen_profiles: list[dict[str, Any]] = []
        for profile_id in ordered_profile_ids:
            model, profile = find_model_and_profile(catalog, profile_id)
            descriptor = model.get("descriptor")
            if not isinstance(descriptor, dict):
                raise DetectorDevelopmentError("invalid_registry", "Detector model descriptor is missing")
            if (
                model.get("availability", {}).get("status") != "available"
                or model.get("selectable_for_probe") is not True
                or profile.get("availability", {}).get("status") != "available"
                or profile.get("selectable_for_probe") is not True
            ):
                raise DetectorDevelopmentError(
                    "profile_unavailable",
                    f"Detector profile is not probe-selectable: {profile_id}",
                )
            require_sha256(descriptor.get("descriptor_sha256"), "model descriptor_sha256")
            require_sha256(profile.get("profile_sha256"), "profile_sha256")
            weights = descriptor.get("weights")
            if (
                not isinstance(weights, dict)
                or not isinstance(weights.get("relative_path"), str)
                or isinstance(weights.get("size_bytes"), bool)
                or not isinstance(weights.get("size_bytes"), int)
                or weights.get("size_bytes") <= 0
            ):
                raise DetectorDevelopmentError(
                    "invalid_registry",
                    f"Detector model weight binding is invalid: {profile_id}",
                )
            require_sha256(weights.get("sha256"), "model weights sha256")
            if (
                profile.get("model_id") != descriptor.get("model_id")
                or profile.get("model_version") != descriptor.get("version")
                or profile.get("model_descriptor_sha256") != descriptor.get("descriptor_sha256")
            ):
                raise DetectorDevelopmentError(
                    "invalid_registry",
                    f"Detector profile lineage is inconsistent: {profile_id}",
                )
            frozen_profiles.append({**deepcopy(profile), "model_descriptor": deepcopy(descriptor)})
        return frozen_profiles

    def _build_execution_bundle(
        self,
        catalog: dict[str, Any],
        frozen_profiles: list[dict[str, Any]],
    ) -> dict[str, Any]:
        installed_runtime: dict[str, str] | None = None
        runtime_contract: dict[str, str] | None = None
        observation_sha256s: dict[str, str] = {}
        for frozen_profile in frozen_profiles:
            profile_id = str(frozen_profile["profile_id"])
            model, _profile = find_model_and_profile(catalog, profile_id)
            descriptor = model.get("descriptor")
            availability = model.get("availability")
            observations = availability.get("observations") if isinstance(availability, dict) else None
            runtime_load = observations.get("runtime_load") if isinstance(observations, dict) else None
            if (
                not isinstance(descriptor, dict)
                or not isinstance(runtime_load, dict)
                or runtime_load.get("status") != "pass"
            ):
                raise DetectorDevelopmentError(
                    "invalid_registry",
                    f"Detector profile lacks passed runtime evidence: {profile_id}",
                )
            evidence_sha256 = require_sha256(
                runtime_load.get("evidence_sha256"),
                "runtime observation evidence_sha256",
            )
            raw_installed = runtime_load.get("installed_runtime")
            if (
                not isinstance(raw_installed, dict)
                or set(raw_installed) != set(_RUNTIME_NAMES)
                or any(
                    not isinstance(raw_installed.get(name), str) or not raw_installed[name] for name in _RUNTIME_NAMES
                )
            ):
                raise DetectorDevelopmentError(
                    "invalid_registry",
                    f"Detector profile runtime versions are incomplete: {profile_id}",
                )
            exact_installed = {name: str(raw_installed[name]) for name in _RUNTIME_NAMES}
            raw_contract = descriptor.get("runtime_contract")
            if (
                not isinstance(raw_contract, dict)
                or set(raw_contract) != set(_RUNTIME_NAMES)
                or any(not isinstance(raw_contract.get(name), str) or not raw_contract[name] for name in _RUNTIME_NAMES)
            ):
                raise DetectorDevelopmentError(
                    "invalid_registry",
                    f"Detector profile runtime contract is invalid: {profile_id}",
                )
            exact_contract = {name: str(raw_contract[name]) for name in _RUNTIME_NAMES}
            if installed_runtime is None:
                installed_runtime = exact_installed
            elif installed_runtime != exact_installed:
                raise DetectorDevelopmentError(
                    "invalid_registry",
                    "Selected detector profiles disagree on installed runtime versions",
                )
            if runtime_contract is None:
                runtime_contract = exact_contract
            elif runtime_contract != exact_contract:
                raise DetectorDevelopmentError(
                    "invalid_registry",
                    "Selected detector profiles disagree on the runtime contract",
                )
            observation_sha256s[profile_id] = evidence_sha256

        if installed_runtime is None or runtime_contract is None:
            raise DetectorDevelopmentError("invalid_registry", "Detector probe execution bundle is empty")
        repo_root = Path(__file__).resolve().parents[2]
        package_root = Path(__file__).resolve().parent
        code_bundle_files: dict[str, str] = {}
        worktree_contents: dict[str, bytes] = {}
        for name, repo_relative_path in zip(
            _CODE_BUNDLE_FILES,
            _CODE_BUNDLE_REPO_FILES,
            strict=True,
        ):
            path = package_root / name
            content, digest = read_regular_bytes(
                path,
                "detector probe code bundle file",
                max_bytes=_MAX_CODE_BUNDLE_FILE_BYTES,
                trusted_root=package_root,
            )
            code_bundle_files[f"football_tracking/{name}"] = digest
            worktree_contents[repo_relative_path] = content
        code_bundle_sha256 = canonical_sha256(code_bundle_files)
        code_binding = self._code_commit_binding(
            repo_root,
            _CODE_BUNDLE_REPO_FILES,
            worktree_contents,
        )
        expected_worktree_files = {
            repo_relative_path: code_bundle_files[f"football_tracking/{name}"]
            for name, repo_relative_path in zip(
                _CODE_BUNDLE_FILES,
                _CODE_BUNDLE_REPO_FILES,
                strict=True,
            )
        }
        if code_binding.worktree_files != expected_worktree_files:
            raise DetectorDevelopmentError(
                "source_changed",
                "Detector probe execution worktree changed during provenance capture",
            )
        code_commit_blob_files = (
            None
            if code_binding.code_commit_blob_files is None
            else {
                f"football_tracking/{name}": code_binding.code_commit_blob_files[repo_relative_path]
                for name, repo_relative_path in zip(
                    _CODE_BUNDLE_FILES,
                    _CODE_BUNDLE_REPO_FILES,
                    strict=True,
                )
            }
        )
        code_commit_blob_bundle_sha256 = (
            None if code_commit_blob_files is None else canonical_sha256(code_commit_blob_files)
        )
        if (
            code_binding.code_commit_blob_files is not None
            and canonical_sha256(code_binding.code_commit_blob_files) != code_binding.code_commit_blob_bundle_sha256
        ):
            raise DetectorDevelopmentError(
                "source_changed",
                "Detector probe commit blob provenance changed during capture",
            )
        execution_environment = probe_execution_environment()
        runtime_environment = {
            "installed_runtime": installed_runtime,
            "runtime_observation_evidence_sha256s": dict(sorted(observation_sha256s.items())),
            "execution_environment": execution_environment,
            "code_bundle_sha256": code_bundle_sha256,
            "code_commit": code_binding.code_commit,
            "code_commit_status": code_binding.code_commit_status,
            "code_commit_reason": code_binding.code_commit_reason,
            "code_commit_blob_bundle_sha256": code_commit_blob_bundle_sha256,
            "code_commit_binding_kind": code_binding.code_commit_binding_kind,
        }
        return {
            "schema_version": "1.0",
            "installed_runtime": installed_runtime,
            "runtime_contract": runtime_contract,
            "runtime_contract_sha256": canonical_sha256(runtime_contract),
            "runtime_observation_evidence_sha256s": dict(sorted(observation_sha256s.items())),
            "execution_environment": execution_environment,
            "runtime_environment_sha256": canonical_sha256(runtime_environment),
            "code_bundle_files": code_bundle_files,
            "code_bundle_sha256": code_bundle_sha256,
            "code_commit": code_binding.code_commit,
            "code_commit_status": code_binding.code_commit_status,
            "code_commit_reason": code_binding.code_commit_reason,
            "code_commit_blob_files": code_commit_blob_files,
            "code_commit_blob_bundle_sha256": code_commit_blob_bundle_sha256,
            "code_commit_binding_kind": code_binding.code_commit_binding_kind,
            "frozen_profiles_sha256": canonical_sha256(frozen_profiles),
        }

    @staticmethod
    def _code_commit_binding(
        repo_root: Path | None = None,
        repo_relative_files: tuple[str, ...] | None = None,
        worktree_contents: dict[str, bytes] | None = None,
    ) -> _CodeCommitBinding:
        root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[2]
        paths = repo_relative_files if repo_relative_files is not None else _CODE_BUNDLE_REPO_FILES

        def result(
            status: str,
            reason: str | None,
            *,
            commit: str | None = None,
            captured_worktree_files: dict[str, str] | None = None,
            blob_files: dict[str, str] | None = None,
        ) -> _CodeCommitBinding:
            return _CodeCommitBinding(
                worktree_files=dict(captured_worktree_files or {}),
                code_commit=commit,
                code_commit_status=status,
                code_commit_reason=reason,
                code_commit_blob_files=(None if blob_files is None else dict(blob_files)),
                code_commit_blob_bundle_sha256=(None if blob_files is None else canonical_sha256(blob_files)),
                code_commit_binding_kind=(_CODE_COMMIT_BINDING_KIND if status == "bound" else None),
            )

        provided_worktree_files: dict[str, str] = {}
        if worktree_contents is not None:
            if set(worktree_contents) != set(paths) or any(
                not isinstance(content, bytes) or len(content) > _MAX_CODE_BUNDLE_FILE_BYTES
                for content in worktree_contents.values()
            ):
                return result("unavailable", "repository_commit_unavailable")
            provided_worktree_files = {path: hashlib.sha256(worktree_contents[path]).hexdigest() for path in paths}
        if (
            not paths
            or len(set(paths)) != len(paths)
            or any(
                not isinstance(path, str)
                or not path
                or "\\" in path
                or path.startswith("/")
                or any(character in path for character in ":*?[")
                or any(character in path for character in "\0\r\n")
                or any(part in {"", ".", ".."} for part in path.split("/"))
                for path in paths
            )
        ):
            return result(
                "unavailable",
                "repository_commit_unavailable",
                captured_worktree_files=provided_worktree_files,
            )
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        git_environment = {name: value for name, value in os.environ.items() if not name.upper().startswith("GIT_")}
        git_environment["GIT_TERMINAL_PROMPT"] = "0"
        try:
            trusted_root = root.resolve(strict=False)
        except (OSError, RuntimeError):
            return result(
                "unavailable",
                "repository_commit_unavailable",
                captured_worktree_files=provided_worktree_files,
            )

        def run(
            arguments: list[str],
            *,
            max_output_bytes: int = _MAX_GIT_PROVENANCE_OUTPUT_BYTES,
        ) -> bytes | None:
            try:
                completed = subprocess.run(
                    ["git", *arguments],
                    cwd=trusted_root,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=False,
                    shell=False,
                    check=False,
                    timeout=2,
                    env=git_environment,
                    **kwargs,
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            stdout = completed.stdout
            if completed.returncode != 0 or not isinstance(stdout, bytes) or len(stdout) > max_output_bytes:
                return None
            return stdout

        def parse_single_line(raw: bytes | None) -> bytes | None:
            if raw is None or not raw:
                return None
            value = raw.rstrip(b"\r\n")
            if raw not in {value, value + b"\n", value + b"\r\n"}:
                return None
            return value

        raw_toplevel = run(["rev-parse", "--show-toplevel"])
        toplevel_bytes = parse_single_line(raw_toplevel)
        if toplevel_bytes is None:
            return result(
                "unavailable",
                "repository_commit_unavailable",
                captured_worktree_files=provided_worktree_files,
            )
        try:
            reported_path = Path(os.fsdecode(toplevel_bytes))
            if not reported_path.is_absolute():
                raise ValueError("Git repository root is not absolute")
            reported_root = reported_path.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return result(
                "unavailable",
                "repository_commit_unavailable",
                captured_worktree_files=provided_worktree_files,
            )
        if os.path.normcase(str(reported_root)) != os.path.normcase(str(trusted_root)):
            return result(
                "unavailable",
                "repository_commit_unavailable",
                captured_worktree_files=provided_worktree_files,
            )

        raw_commit = run(["rev-parse", "--verify", "HEAD"])
        commit_bytes = parse_single_line(raw_commit)
        if commit_bytes is None:
            return result(
                "unavailable",
                "repository_commit_unavailable",
                captured_worktree_files=provided_worktree_files,
            )
        try:
            commit = commit_bytes.decode("ascii").lower()
        except UnicodeDecodeError:
            return result(
                "unavailable",
                "repository_commit_unavailable",
                captured_worktree_files=provided_worktree_files,
            )
        if len(commit) not in {40, 64} or any(character not in "0123456789abcdef" for character in commit):
            return result(
                "unavailable",
                "repository_commit_unavailable",
                captured_worktree_files=provided_worktree_files,
            )

        status = run(
            [
                "--literal-pathspecs",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--",
                *paths,
            ]
        )
        if status is None:
            return result(
                "unavailable",
                "repository_commit_unavailable",
                captured_worktree_files=provided_worktree_files,
            )
        if status:
            return result(
                "unbound",
                "code_bundle_differs_from_commit",
                captured_worktree_files=provided_worktree_files,
            )

        raw_tree = run(["--literal-pathspecs", "ls-tree", "-r", "-z", commit, "--", *paths])
        if raw_tree is None:
            return result(
                "unavailable",
                "repository_commit_unavailable",
                captured_worktree_files=provided_worktree_files,
            )
        if not raw_tree.endswith(b"\0"):
            return result(
                "unavailable",
                "repository_commit_unavailable",
                captured_worktree_files=provided_worktree_files,
            )
        tree_objects: dict[str, str] = {}
        try:
            for raw_entry in raw_tree[:-1].split(b"\0"):
                raw_metadata, raw_path = raw_entry.split(b"\t", 1)
                mode, object_type, raw_oid = raw_metadata.split(b" ", 2)
                path = raw_path.decode("utf-8")
                oid = raw_oid.decode("ascii").lower()
                if (
                    mode not in {b"100644", b"100755"}
                    or object_type != b"blob"
                    or len(oid) != len(commit)
                    or any(character not in "0123456789abcdef" for character in oid)
                    or path in tree_objects
                ):
                    return result(
                        "unbound",
                        "code_bundle_differs_from_commit",
                        captured_worktree_files=provided_worktree_files,
                    )
                tree_objects[path] = oid
        except (UnicodeDecodeError, ValueError):
            return result(
                "unavailable",
                "repository_commit_unavailable",
                captured_worktree_files=provided_worktree_files,
            )
        if len(tree_objects) != len(paths) or set(tree_objects) != set(paths):
            return result(
                "unbound",
                "code_bundle_differs_from_commit",
                captured_worktree_files=provided_worktree_files,
            )

        captured_contents: dict[str, bytes] = {}
        captured_worktree_files = dict(provided_worktree_files)
        for path in paths:
            if worktree_contents is None:
                try:
                    content, digest = read_regular_bytes(
                        root / Path(path),
                        "detector probe execution worktree file",
                        max_bytes=_MAX_CODE_BUNDLE_FILE_BYTES,
                        trusted_root=root,
                    )
                except DetectorDevelopmentError:
                    return result(
                        "unbound",
                        "code_bundle_differs_from_commit",
                        captured_worktree_files=captured_worktree_files,
                    )
                captured_contents[path] = content
                captured_worktree_files[path] = digest
            else:
                captured_contents[path] = worktree_contents[path]

        blob_files: dict[str, str] = {}
        for path in paths:
            raw_size = run(["cat-file", "-s", tree_objects[path]])
            if raw_size is None:
                return result(
                    "unavailable",
                    "repository_commit_unavailable",
                    captured_worktree_files=captured_worktree_files,
                )
            try:
                blob_size = int(raw_size.rstrip(b"\r\n").decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                return result(
                    "unavailable",
                    "repository_commit_unavailable",
                    captured_worktree_files=captured_worktree_files,
                )
            if blob_size < 0 or blob_size > _MAX_CODE_BUNDLE_FILE_BYTES:
                return result(
                    "unavailable",
                    "repository_commit_unavailable",
                    captured_worktree_files=captured_worktree_files,
                )
            blob = run(
                ["cat-file", "blob", tree_objects[path]],
                max_output_bytes=_MAX_CODE_BUNDLE_FILE_BYTES,
            )
            if blob is None or len(blob) != blob_size:
                return result(
                    "unavailable",
                    "repository_commit_unavailable",
                    captured_worktree_files=captured_worktree_files,
                )
            worktree_content = captured_contents[path]
            if worktree_content != blob and worktree_content.replace(b"\r\n", b"\n") != blob:
                return result(
                    "unbound",
                    "code_bundle_differs_from_commit",
                    captured_worktree_files=captured_worktree_files,
                )
            blob_files[path] = hashlib.sha256(blob).hexdigest()

        final_status = run(
            [
                "--literal-pathspecs",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--",
                *paths,
            ]
        )
        if final_status is None:
            return result(
                "unavailable",
                "repository_commit_unavailable",
                captured_worktree_files=captured_worktree_files,
            )
        if final_status:
            return result(
                "unbound",
                "code_bundle_differs_from_commit",
                captured_worktree_files=captured_worktree_files,
            )
        for path in paths:
            try:
                _content, digest = read_regular_bytes(
                    root / Path(path),
                    "detector probe execution worktree file",
                    max_bytes=_MAX_CODE_BUNDLE_FILE_BYTES,
                    trusted_root=root,
                )
            except DetectorDevelopmentError:
                return result(
                    "unbound",
                    "code_bundle_differs_from_commit",
                    captured_worktree_files=captured_worktree_files,
                )
            if digest != captured_worktree_files[path]:
                return result(
                    "unbound",
                    "code_bundle_differs_from_commit",
                    captured_worktree_files=captured_worktree_files,
                )

        final_commit = run(["rev-parse", "--verify", "HEAD"])
        if final_commit != raw_commit:
            return result(
                "unavailable",
                "repository_commit_unavailable",
                captured_worktree_files=captured_worktree_files,
            )
        return result(
            "bound",
            None,
            commit=commit,
            captured_worktree_files=captured_worktree_files,
            blob_files=blob_files,
        )

    @staticmethod
    def _positive_int(value: Any, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise DetectorDevelopmentError("invalid_tracking_contract", f"Detector probe {label} must be positive")
        return value

    def _load_and_recover_jobs(self) -> None:
        self._refresh_jobs_from_disk()
        for job_id, record in list(self._jobs.items()):
            status = record.get("status")
            destination = self._results_root / job_id
            owner_state = self._owner_is_active(record.get("owner_id"))
            if status in {"running", "committing"} and owner_state is not False:
                continue
            if (
                status == "running"
                and record.get("frozen_request", {}).get("retry_kind") == "review_proxy_decode_upgrade"
            ):
                self._remove_uncommitted_outputs(job_id)
                now = utc_now_iso()
                record.update(
                    {
                        "status": "failed",
                        "stage": "failed",
                        "owner_id": None,
                        "cancel_requested": False,
                        "error_code": "review_proxy_child_assembly_interrupted",
                        "error_message": ("Review-proxy child assembly stopped before its atomic commit"),
                        "recovery_action": "retry",
                        "report": None,
                        "result_manifest_sha256": None,
                        "updated_at": now,
                    }
                )
                record["progress"].update(updated_at=now)
                self._persist_record(record)
                continue
            if status in {"queued", "running"}:
                self._remove_uncommitted_outputs(job_id)
                if status == "running" and record.get("cancel_requested") is True:
                    self._mark_cancelled(record)
                    continue
                now = utc_now_iso()
                record.update(
                    {
                        "status": "queued",
                        "stage": ("recovered_after_restart" if status == "running" else record.get("stage", "queued")),
                        "owner_id": None,
                        "cancel_requested": False,
                        "report": None,
                        "result_manifest_sha256": None,
                        "updated_at": now,
                    }
                )
                record["progress"].update({"completed": 0, "updated_at": now})
                self._persist_record(record)
            elif status == "committing":
                try:
                    report, manifest_sha256 = self._validate_result_tree(destination, record)
                except Exception as exc:
                    self._remove_uncommitted_outputs(job_id)
                    now = utc_now_iso()
                    record.update(
                        {
                            "status": "failed",
                            "stage": "failed",
                            "owner_id": None,
                            "cancel_requested": False,
                            "error_code": "commit_interrupted",
                            "error_message": str(exc)[:1000],
                            "recovery_action": "retry",
                            "report": None,
                            "result_manifest_sha256": None,
                            "updated_at": now,
                        }
                    )
                else:
                    now = utc_now_iso()
                    record.update(
                        {
                            "status": "ready",
                            "stage": "ready",
                            "owner_id": None,
                            "cancel_requested": False,
                            "error_code": None,
                            "blocker_code": None,
                            "recovery_action": None,
                            "report": report,
                            "result_manifest_sha256": manifest_sha256,
                            "updated_at": now,
                        }
                    )
                    record["progress"]["completed"] = record["progress"]["total"]
                record["progress"]["updated_at"] = now
                self._persist_record(record)
            elif status == "ready":
                try:
                    report, manifest_sha256 = self._validate_result_tree(destination, record)
                except Exception as exc:
                    now = utc_now_iso()
                    record.update(
                        {
                            "status": "blocked",
                            "stage": "blocked",
                            "owner_id": None,
                            "blocker_code": "persisted_result_invalid",
                            "error_code": None,
                            "error_message": str(exc)[:1000],
                            "recovery_action": "retry",
                            "report": None,
                            "result_manifest_sha256": None,
                            "updated_at": now,
                        }
                    )
                else:
                    if (
                        record.get("report") == report
                        and record.get("result_manifest_sha256") == manifest_sha256
                        and record.get("owner_id") is None
                    ):
                        continue
                    record["report"] = report
                    record["result_manifest_sha256"] = manifest_sha256
                    record["owner_id"] = None
                    record["updated_at"] = utc_now_iso()
                record["progress"]["updated_at"] = record["updated_at"]
                self._persist_record(record)

    def _remove_uncommitted_outputs(self, job_id: str) -> None:
        self._remove_tree(self._results_root / job_id)
        for path in self._results_root.glob(f".{job_id}.staging-*"):
            self._remove_tree(path)

    def _refresh_jobs_from_disk(self) -> None:
        loaded: dict[str, dict[str, Any]] = {}
        for path in sorted(self._jobs_root.glob("*.json")):
            try:
                record = self._read_job_file(path)
            except (DetectorDevelopmentError, OSError):
                # A malformed unrelated row must not take the whole durable
                # queue offline. Exact job reads remain fail-closed below.
                continue
            job_id = str(record["job_id"])
            loaded[job_id] = record
        self._jobs = loaded

    def _read_review_proxy_child_claim(self, parent_job_id: str) -> dict[str, Any] | None:
        parent_job_id = require_safe_id(parent_job_id, "review-proxy parent probe job_id")
        path = self._review_proxy_child_claims_root / f"{parent_job_id}.json"
        if not path.exists() and not is_link_or_reparse(path):
            return None
        try:
            content, _ = read_regular_bytes(
                path,
                "review-proxy child claim",
                max_bytes=16 * 1024,
                trusted_root=self._review_proxy_child_claims_root,
            )
            claim = json_object_from_bytes(content, "review-proxy child claim")
            claim_body = deepcopy(claim)
            claim_sha256 = claim_body.pop("claim_sha256", None)
            expected_keys = {
                "schema_version",
                "artifact_type",
                "parent_probe_job_id",
                "child_probe_job_id",
                "repair_id",
                "child_request_sha256",
                "created_at",
            }
            if (
                set(claim_body) != expected_keys
                or claim_body.get("schema_version") != "1.0"
                or claim_body.get("artifact_type") != "detector_review_proxy_child_claim"
                or claim_body.get("parent_probe_job_id") != parent_job_id
                or canonical_sha256(claim_body) != claim_sha256
            ):
                raise DetectorDevelopmentError(
                    "invalid_review_proxy_child_claim",
                    "Review-proxy child claim is inconsistent",
                    status_code=409,
                )
            require_safe_id(
                claim_body.get("child_probe_job_id"),
                "claimed review-proxy child probe job_id",
            )
            require_safe_id(
                claim_body.get("repair_id"),
                "claimed review-proxy repair_id",
            )
            require_sha256(
                claim_body.get("child_request_sha256"),
                "claimed review-proxy child request_sha256",
            )
            if not isinstance(claim_body.get("created_at"), str) or not claim_body["created_at"]:
                raise DetectorDevelopmentError(
                    "invalid_review_proxy_child_claim",
                    "Review-proxy child claim timestamp is invalid",
                    status_code=409,
                )
            return claim
        except DetectorDevelopmentError as exc:
            if exc.code == "invalid_review_proxy_child_claim":
                raise
            raise DetectorDevelopmentError(
                "invalid_review_proxy_child_claim",
                "Review-proxy child claim cannot be verified",
                status_code=409,
            ) from exc

    def _publish_review_proxy_child_claim(
        self,
        *,
        parent_job_id: str,
        child_job_id: str,
        repair_id: str,
        child_request_sha256: str,
    ) -> dict[str, Any]:
        parent_job_id = require_safe_id(parent_job_id, "review-proxy parent probe job_id")
        child_job_id = require_safe_id(child_job_id, "review-proxy child probe job_id")
        repair_id = require_safe_id(repair_id, "review-proxy repair_id")
        child_request_sha256 = require_sha256(child_request_sha256, "review-proxy child request_sha256")
        existing = self._read_review_proxy_child_claim(parent_job_id)
        if existing is not None:
            return existing
        body = {
            "schema_version": "1.0",
            "artifact_type": "detector_review_proxy_child_claim",
            "parent_probe_job_id": parent_job_id,
            "child_probe_job_id": child_job_id,
            "repair_id": repair_id,
            "child_request_sha256": child_request_sha256,
            "created_at": utc_now_iso(),
        }
        claim = {**body, "claim_sha256": canonical_sha256(body)}
        atomic_write_json(
            self._review_proxy_child_claims_root / f"{parent_job_id}.json",
            claim,
            trusted_root=self._review_proxy_child_claims_root,
        )
        return claim

    def _claimed_review_proxy_child_record(self, claim: dict[str, Any]) -> dict[str, Any]:
        child_job_id = claim["child_probe_job_id"]
        try:
            child = self._read_job_file(self._jobs_root / f"{child_job_id}.json")
            frozen = child.get("frozen_request")
            upgrade = frozen.get("review_proxy_upgrade") if isinstance(frozen, dict) else None
            if (
                child.get("job_id") != child_job_id
                or child.get("request_sha256") != claim["child_request_sha256"]
                or child.get("retry_from_job_id") != claim["parent_probe_job_id"]
                or child.get("retry_kind") != "review_proxy_decode_upgrade"
                or not isinstance(upgrade, dict)
                or upgrade.get("repair_evidence", {}).get("repair_id") != claim["repair_id"]
            ):
                raise DetectorDevelopmentError(
                    "invalid_persisted_job",
                    "Claimed review-proxy child binding is inconsistent",
                )
            return child
        except (DetectorDevelopmentError, OSError) as exc:
            raise DetectorDevelopmentError(
                "review_proxy_parent_child_claimed",
                "The audited parent has a durable child claim whose child cannot be verified",
                status_code=409,
            ) from exc

    def _read_job_file(self, path: Path) -> dict[str, Any]:
        content, _ = read_regular_bytes(
            path,
            "detector probe job",
            max_bytes=4 * 1024 * 1024,
            trusted_root=self._jobs_root,
        )
        record = json_object_from_bytes(content, "detector probe job")
        job_id = require_safe_id(record.get("job_id"), "detector probe job_id")
        if path.name != f"{job_id}.json":
            raise DetectorDevelopmentError("invalid_persisted_job", "Detector probe job filename is inconsistent")
        self._validate_persisted_record(record)
        return record

    @staticmethod
    def _validate_persisted_record(record: dict[str, Any]) -> None:
        if (
            record.get("schema_version") != "1.0"
            or record.get("artifact_type") != "detector_probe_job"
            or record.get("status") not in _ACTIVE_STATUSES | _TERMINAL_STATUSES
            or not isinstance(record.get("frozen_request"), dict)
            or not isinstance(record.get("frozen_profiles"), list)
            or not isinstance(record.get("progress"), dict)
        ):
            raise DetectorDevelopmentError("invalid_persisted_job", "Detector probe job schema is invalid")
        frozen = record["frozen_request"]
        request_sha256 = canonical_sha256(frozen)
        intent_sha256 = canonical_sha256({key: value for key, value in frozen.items() if key != "retry_from_job_id"})
        semantic_intent_sha256 = semantic_probe_intent_sha256(frozen)
        required_resource_fields = (
            "parent_trial_id",
            "source_id",
            "source_sha256",
            "source_file_identity_sha256",
            "tracking_contract_sha256",
            "base_config_relative_path",
            "base_config_sha256",
            "effective_config_relative_path",
            "effective_config_sha256",
            "trial_intent_sha256",
            "tuning_patch_sha256",
        )
        if any(key not in frozen for key in required_resource_fields):
            raise DetectorDevelopmentError("invalid_persisted_job", "Detector probe job lineage is incomplete")
        resource_sha256 = canonical_sha256({key: frozen[key] for key in required_resource_fields})
        try:
            tuning_patch_binding = _validated_tuning_patch_binding(frozen.get("tuning_patch_binding"))
        except DetectorDevelopmentError as exc:
            raise DetectorDevelopmentError(
                "invalid_persisted_job", "Detector probe tuning-patch lineage is invalid"
            ) from exc
        if canonical_sha256(tuning_patch_binding) != frozen.get("tuning_patch_sha256"):
            raise DetectorDevelopmentError(
                "persisted_job_digest_mismatch",
                "Detector probe tuning-patch digest is inconsistent",
            )
        if (
            record.get("request_sha256") != request_sha256
            or record.get("idempotency_key") != request_sha256
            or record.get("intent_sha256") != intent_sha256
            or (
                record.get("semantic_intent_sha256") is not None
                and record.get("semantic_intent_sha256") != semantic_intent_sha256
            )
            or record.get("resource_sha256") != resource_sha256
            or record.get("frozen_profiles_sha256") != canonical_sha256(record["frozen_profiles"])
            or record.get("retry_from_job_id") != frozen.get("retry_from_job_id")
            or record.get("retry_kind") != frozen.get("retry_kind")
        ):
            raise DetectorDevelopmentError(
                "persisted_job_digest_mismatch",
                "Detector probe immutable job digests are inconsistent",
            )
        profile_ids = frozen.get("profile_ids")
        profile_sha256s = frozen.get("profile_sha256s")
        if (
            not isinstance(profile_ids, list)
            or not 2 <= len(profile_ids) <= 6
            or profile_ids != sorted(set(profile_ids))
            or not isinstance(profile_sha256s, dict)
            or len(record["frozen_profiles"]) != len(profile_ids)
        ):
            raise DetectorDevelopmentError("invalid_persisted_job", "Detector probe frozen profiles are invalid")
        for profile in record["frozen_profiles"]:
            if (
                not isinstance(profile, dict)
                or profile.get("profile_id") not in profile_ids
                or profile_sha256s.get(profile.get("profile_id")) != profile.get("profile_sha256")
            ):
                raise DetectorDevelopmentError(
                    "persisted_job_digest_mismatch",
                    "Detector probe frozen profile digest is inconsistent",
                )
        DetectorProbeCoordinator._validate_frozen_execution_bundle(frozen, record["frozen_profiles"])
        completed = record["progress"].get("completed")
        total = record["progress"].get("total")
        if (
            isinstance(completed, bool)
            or not isinstance(completed, int)
            or completed < 0
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total <= 0
            or completed > total
        ):
            raise DetectorDevelopmentError("invalid_persisted_job", "Detector probe progress is invalid")

    @staticmethod
    def _validate_frozen_execution_bundle(
        frozen: dict[str, Any],
        frozen_profiles: list[dict[str, Any]],
    ) -> None:
        bundle = frozen.get("execution_bundle")
        expected_bundle_keys = {
            "schema_version",
            "installed_runtime",
            "runtime_contract",
            "runtime_contract_sha256",
            "runtime_observation_evidence_sha256s",
            "execution_environment",
            "runtime_environment_sha256",
            "code_bundle_files",
            "code_bundle_sha256",
            "code_commit",
            "code_commit_status",
            "code_commit_reason",
            "code_commit_blob_files",
            "code_commit_blob_bundle_sha256",
            "code_commit_binding_kind",
            "frozen_profiles_sha256",
        }
        execution_environment_keys = {
            "device",
            "precision",
            "cuda_available",
            "cuda_device_count",
            "cuda_visible_devices",
            "cuda_compiled_version",
            "cudnn_version",
            "gpu_name",
            "gpu_compute_capability",
            "gpu_total_memory_bytes",
            "cuda_driver_version",
            "python_implementation",
            "python_version",
            "numpy_version",
            "opencv_version",
            "pydantic_version",
            "pydantic_core_version",
            "opencv_build_information_sha256",
            "opencv_ffmpeg_enabled",
            "decoder_fingerprint_sha256",
        }
        expected_code_bundle_paths = DetectorProbeCoordinator._validated_code_bundle_paths(
            frozen,
            bundle,
        )
        if (
            not isinstance(bundle, dict)
            or set(bundle) != expected_bundle_keys
            or bundle.get("schema_version") != "1.0"
            or not isinstance(bundle.get("installed_runtime"), dict)
            or set(bundle["installed_runtime"]) != set(_RUNTIME_NAMES)
            or any(
                not isinstance(bundle["installed_runtime"].get(name), str) or not bundle["installed_runtime"][name]
                for name in _RUNTIME_NAMES
            )
            or not isinstance(bundle.get("runtime_contract"), dict)
            or set(bundle["runtime_contract"]) != set(_RUNTIME_NAMES)
            or any(
                not isinstance(bundle["runtime_contract"].get(name), str) or not bundle["runtime_contract"][name]
                for name in _RUNTIME_NAMES
            )
            or not isinstance(bundle.get("execution_environment"), dict)
            or set(bundle["execution_environment"]) != execution_environment_keys
            or not isinstance(bundle.get("code_bundle_files"), dict)
            or tuple(bundle["code_bundle_files"]) != expected_code_bundle_paths
            or not isinstance(bundle.get("runtime_observation_evidence_sha256s"), dict)
            or set(bundle["runtime_observation_evidence_sha256s"]) != set(frozen.get("profile_ids", []))
        ):
            raise DetectorDevelopmentError(
                "invalid_persisted_job",
                "Detector probe frozen execution bundle is invalid",
            )
        for value in (
            bundle["runtime_contract_sha256"],
            bundle["runtime_environment_sha256"],
            bundle["code_bundle_sha256"],
            bundle["frozen_profiles_sha256"],
            frozen.get("execution_bundle_sha256"),
            frozen.get("runtime_environment_sha256"),
            frozen.get("frozen_profiles_sha256"),
            *bundle["runtime_observation_evidence_sha256s"].values(),
            *bundle["code_bundle_files"].values(),
        ):
            try:
                require_sha256(value, "frozen execution binding sha256")
            except DetectorDevelopmentError as exc:
                raise DetectorDevelopmentError(
                    "invalid_persisted_job",
                    "Detector probe frozen execution digest is invalid",
                ) from exc
        code_commit_blob_files = bundle.get("code_commit_blob_files")
        code_commit_blob_bundle_sha256 = bundle.get("code_commit_blob_bundle_sha256")
        if code_commit_blob_files is not None:
            if (
                not isinstance(code_commit_blob_files, dict)
                or tuple(code_commit_blob_files) != expected_code_bundle_paths
            ):
                raise DetectorDevelopmentError(
                    "invalid_persisted_job",
                    "Detector probe commit blob file binding is invalid",
                )
            for value in code_commit_blob_files.values():
                try:
                    require_sha256(value, "commit blob file sha256")
                except DetectorDevelopmentError as exc:
                    raise DetectorDevelopmentError(
                        "invalid_persisted_job",
                        "Detector probe commit blob digest is invalid",
                    ) from exc
            try:
                require_sha256(
                    code_commit_blob_bundle_sha256,
                    "commit blob bundle sha256",
                )
            except DetectorDevelopmentError as exc:
                raise DetectorDevelopmentError(
                    "invalid_persisted_job",
                    "Detector probe commit blob bundle digest is invalid",
                ) from exc
            if canonical_sha256(code_commit_blob_files) != code_commit_blob_bundle_sha256:
                raise DetectorDevelopmentError(
                    "persisted_job_digest_mismatch",
                    "Detector probe commit blob bundle digest is inconsistent",
                )
        elif code_commit_blob_bundle_sha256 is not None:
            raise DetectorDevelopmentError(
                "invalid_persisted_job",
                "Detector probe commit blob binding is incomplete",
            )
        environment = bundle["execution_environment"]
        try:
            require_sha256(
                environment.get("opencv_build_information_sha256"),
                "OpenCV build information sha256",
            )
            require_sha256(
                environment.get("decoder_fingerprint_sha256"),
                "decoder fingerprint sha256",
            )
        except DetectorDevelopmentError as exc:
            raise DetectorDevelopmentError(
                "invalid_persisted_job",
                "Detector probe decoder fingerprint digest is invalid",
            ) from exc
        decoder_fingerprint = {
            "python_implementation": environment.get("python_implementation"),
            "python_version": environment.get("python_version"),
            "numpy_version": environment.get("numpy_version"),
            "opencv_version": environment.get("opencv_version"),
            "opencv_build_information_sha256": environment.get("opencv_build_information_sha256"),
            "opencv_ffmpeg_enabled": environment.get("opencv_ffmpeg_enabled"),
        }
        if (
            environment.get("device") not in {"cpu", "cuda:0"}
            or environment.get("precision") != "fp32"
            or not isinstance(environment.get("cuda_available"), bool)
            or isinstance(environment.get("cuda_device_count"), bool)
            or not isinstance(environment.get("cuda_device_count"), int)
            or environment["cuda_device_count"] < 0
            or (environment["device"] == "cuda:0") != environment["cuda_available"]
            or any(
                not isinstance(environment.get(name), str) or not environment[name]
                for name in (
                    "python_implementation",
                    "python_version",
                    "numpy_version",
                    "opencv_version",
                    "pydantic_version",
                    "pydantic_core_version",
                )
            )
            or canonical_sha256(decoder_fingerprint) != environment["decoder_fingerprint_sha256"]
        ):
            raise DetectorDevelopmentError(
                "invalid_persisted_job",
                "Detector probe execution device binding is invalid",
            )
        code_commit = bundle.get("code_commit")
        if bundle.get("code_commit_status") == "bound":
            if (
                not isinstance(code_commit, str)
                or len(code_commit) not in {40, 64}
                or any(character not in "0123456789abcdef" for character in code_commit)
                or bundle.get("code_commit_reason") is not None
                or code_commit_blob_files is None
                or bundle.get("code_commit_binding_kind") != _CODE_COMMIT_BINDING_KIND
            ):
                raise DetectorDevelopmentError(
                    "invalid_persisted_job",
                    "Detector probe code commit binding is invalid",
                )
        elif bundle.get("code_commit_status") == "unbound":
            if (
                code_commit is not None
                or bundle.get("code_commit_reason") != "code_bundle_differs_from_commit"
                or code_commit_blob_files is not None
                or code_commit_blob_bundle_sha256 is not None
                or bundle.get("code_commit_binding_kind") is not None
            ):
                raise DetectorDevelopmentError(
                    "invalid_persisted_job",
                    "Detector probe unbound code commit binding is invalid",
                )
        elif (
            bundle.get("code_commit_status") != "unavailable"
            or code_commit is not None
            or bundle.get("code_commit_reason") != "repository_commit_unavailable"
            or code_commit_blob_files is not None
            or code_commit_blob_bundle_sha256 is not None
            or bundle.get("code_commit_binding_kind") is not None
        ):
            raise DetectorDevelopmentError(
                "invalid_persisted_job",
                "Detector probe unavailable code commit binding is invalid",
            )
        runtime_environment = {
            "installed_runtime": bundle["installed_runtime"],
            "runtime_observation_evidence_sha256s": bundle["runtime_observation_evidence_sha256s"],
            "execution_environment": environment,
            "code_bundle_sha256": bundle["code_bundle_sha256"],
            "code_commit": code_commit,
            "code_commit_status": bundle["code_commit_status"],
            "code_commit_reason": bundle["code_commit_reason"],
            "code_commit_blob_bundle_sha256": code_commit_blob_bundle_sha256,
            "code_commit_binding_kind": bundle["code_commit_binding_kind"],
        }
        frozen_profiles_sha256 = canonical_sha256(frozen_profiles)
        if (
            canonical_sha256(bundle["runtime_contract"]) != bundle["runtime_contract_sha256"]
            or canonical_sha256(bundle["code_bundle_files"]) != bundle["code_bundle_sha256"]
            or canonical_sha256(runtime_environment) != bundle["runtime_environment_sha256"]
            or canonical_sha256(bundle) != frozen["execution_bundle_sha256"]
            or bundle["runtime_environment_sha256"] != frozen["runtime_environment_sha256"]
            or frozen_profiles_sha256 != bundle["frozen_profiles_sha256"]
            or frozen_profiles_sha256 != frozen["frozen_profiles_sha256"]
        ):
            raise DetectorDevelopmentError(
                "persisted_job_digest_mismatch",
                "Detector probe frozen execution bundle digest is inconsistent",
            )

    @staticmethod
    def _validated_code_bundle_paths(
        frozen: dict[str, Any],
        bundle: Any,
    ) -> tuple[str, ...]:
        if not isinstance(bundle, dict) or not isinstance(bundle.get("code_bundle_files"), dict):
            raise DetectorDevelopmentError(
                "invalid_persisted_job",
                "Detector probe frozen execution bundle is invalid",
            )
        current_paths = tuple(f"football_tracking/{name}" for name in _CODE_BUNDLE_FILES)
        observed_paths = tuple(bundle["code_bundle_files"])
        if observed_paths == current_paths:
            return current_paths
        historical = _AUDITED_T2_EXECUTION_BUNDLE_BINDINGS.get(frozen.get("execution_bundle_sha256"))
        historical_paths = tuple(f"football_tracking/{name}" for name in _AUDITED_T2_CODE_BUNDLE_FILES)
        if (
            historical is None
            or observed_paths != historical_paths
            or any(bundle.get(field) != value for field, value in historical.items())
        ):
            raise DetectorDevelopmentError(
                "invalid_persisted_job",
                "Detector probe frozen execution bundle paths are invalid",
            )
        return historical_paths

    def _record(self, job_id: str) -> dict[str, Any]:
        job_id = require_safe_id(job_id, "detector probe job_id")
        path = self._jobs_root / f"{job_id}.json"
        if not path.exists() and not is_link_or_reparse(path):
            raise DetectorDevelopmentError("probe_not_found", "Detector probe job was not found", status_code=404)
        try:
            record = self._read_job_file(path)
        except DetectorDevelopmentError as exc:
            raise DetectorDevelopmentError(
                "invalid_persisted_job",
                f"Persisted detector probe job is invalid: {path.name}",
            ) from exc
        self._jobs[job_id] = record
        return record

    def _persist_record(self, record: dict[str, Any]) -> None:
        job_id = require_safe_id(record.get("job_id"), "detector probe job_id")
        atomic_write_json(
            self._jobs_root / f"{job_id}.json",
            record,
            trusted_root=self._jobs_root,
        )
        token_path = self._cancel_root / f"{job_id}.json"
        if record.get("cancel_requested") is True or not token_path.exists():
            self._persist_cancel_token(record)

    def _persist_cancel_token(self, record: dict[str, Any]) -> None:
        job_id = require_safe_id(record.get("job_id"), "detector probe job_id")
        atomic_write_json(
            self._cancel_root / f"{job_id}.json",
            {
                "schema_version": "1.0",
                "artifact_type": "detector_probe_cancel_token",
                "job_id": job_id,
                "cancel_requested": record.get("cancel_requested") is True,
            },
            trusted_root=self._cancel_root,
        )

    @staticmethod
    def _public_record(record: dict[str, Any]) -> dict[str, Any]:
        public = deepcopy(record)
        public.pop("resource_sha256", None)
        public.pop("owner_id", None)
        public.pop("cancel_requested", None)
        public.pop("error_message", None)
        job_id = str(public["job_id"])
        public["status_url"] = f"/api/v1/detector-probes/{job_id}"
        public["cancel_url"] = f"{public['status_url']}/cancel"
        public["can_cancel"] = public.get("status") in {"queued", "running"}
        return public

    def _start_worker(self, job_id: str) -> None:
        del job_id
        self._start_dispatcher()
        self._dispatch_event.set()

    def _start_dispatcher(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._dispatcher is not None and self._dispatcher.is_alive():
                return
            self._dispatcher = threading.Thread(
                target=self._dispatch_queued_jobs,
                name=f"detector-probe-dispatcher-{self._owner_id}",
                daemon=True,
            )
            self._dispatcher.start()

    def _dispatch_queued_jobs(self) -> None:
        while True:
            self._dispatch_event.wait(timeout=0.25)
            self._dispatch_event.clear()
            while True:
                try:
                    if not self._execute_in_global_slot(None):
                        break
                except DetectorDevelopmentError as exc:
                    # All failures after a successful claim are recorded by
                    # execute_probe itself. Pre-claim errors must never mutate a
                    # durable row that another process may own.
                    if exc.code == "service_closed":
                        return

    def _recover_orphaned_active_jobs(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._refresh_jobs_from_disk()
            for job_id, record in list(self._jobs.items()):
                status = record.get("status")
                owner_state = self._owner_is_active(record.get("owner_id"))
                if status not in {"running", "committing"} or owner_state is not False:
                    continue
                now = utc_now_iso()
                if status == "running":
                    self._remove_uncommitted_outputs(job_id)
                    if record.get("cancel_requested") is True:
                        self._mark_cancelled(record)
                    else:
                        record.update(
                            {
                                "status": "queued",
                                "stage": "recovered_after_restart",
                                "owner_id": None,
                                "cancel_requested": False,
                                "report": None,
                                "result_manifest_sha256": None,
                                "updated_at": now,
                            }
                        )
                        record["progress"].update({"completed": 0, "updated_at": now})
                        self._persist_record(record)
                    continue
                destination = self._results_root / job_id
                try:
                    report, manifest_sha256 = self._validate_result_tree(destination, record)
                except Exception as exc:
                    self._remove_uncommitted_outputs(job_id)
                    record.update(
                        {
                            "status": "failed",
                            "stage": "failed",
                            "owner_id": None,
                            "cancel_requested": False,
                            "error_code": "commit_interrupted",
                            "error_message": str(exc)[:1000],
                            "recovery_action": "retry",
                            "report": None,
                            "result_manifest_sha256": None,
                            "updated_at": now,
                        }
                    )
                else:
                    record.update(
                        {
                            "status": "ready",
                            "stage": "ready",
                            "owner_id": None,
                            "cancel_requested": False,
                            "error_code": None,
                            "blocker_code": None,
                            "recovery_action": None,
                            "report": report,
                            "result_manifest_sha256": manifest_sha256,
                            "updated_at": now,
                        }
                    )
                    record["progress"]["completed"] = record["progress"]["total"]
                record["progress"]["updated_at"] = now
                self._persist_record(record)


def _publish_staging_directory(staging: Path, destination: Path) -> None:
    if staging.parent != destination.parent or is_link_or_reparse(staging):
        raise DetectorDevelopmentError("unsafe_result_tree", "Detector probe staging directory is unsafe")
    if destination.exists() or destination.is_symlink() or is_link_or_reparse(destination):
        raise DetectorDevelopmentError("result_already_exists", "Detector probe result destination already exists")
    parent_metadata = destination.parent.stat()
    parent_identity = (int(parent_metadata.st_dev), int(parent_metadata.st_ino))
    os.replace(staging, destination)
    current_parent = destination.parent.stat()
    if is_link_or_reparse(destination) or (int(current_parent.st_dev), int(current_parent.st_ino)) != parent_identity:
        raise DetectorDevelopmentError("source_changed", "Detector probe result root changed during publication")


def _try_lock_handle(handle) -> bool:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        return True
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _require_lock_file_identity(handle, path: Path, label: str) -> None:
    try:
        opened = os.fstat(handle.fileno())
        current = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise DetectorDevelopmentError(
            "unsafe_lock_file", f"Detector probe {label} lock identity is unavailable"
        ) from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or bool(getattr(opened, "st_file_attributes", 0) & reparse_flag)
        or bool(getattr(current, "st_file_attributes", 0) & reparse_flag)
        or is_link_or_reparse(path)
        or (int(opened.st_dev), int(opened.st_ino)) != (int(current.st_dev), int(current.st_ino))
    ):
        raise DetectorDevelopmentError("unsafe_lock_file", f"Detector probe {label} lock identity changed")


def _unlock_handle(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
