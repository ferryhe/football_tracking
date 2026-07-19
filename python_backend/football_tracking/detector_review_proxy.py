from __future__ import annotations

import errno
import hashlib
import math
import os
import platform
import queue
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

import cv2

from football_tracking.detector_development_common import (
    DetectorDevelopmentError,
    _ancestor_identities_are_current,
    _capture_ancestor_identities,
    _change_identity,
    _close_windows_handles,
    _open_windows_ancestor_guards,
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
from football_tracking.media_integrity import inspect_frame, inspect_image

PROXY_WIDTH = 2560
PROXY_HEIGHT = 720
_DEFAULT_OUTPUT_HARD_LIMIT_BYTES = 32 * 1024 * 1024 * 1024
_DEFAULT_DISK_RESERVE_BYTES = 2 * 1024 * 1024 * 1024
_DEFAULT_WORKER_DEADLINE_SECONDS = 4 * 60 * 60.0
_DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 15.0
_MINIMUM_DYNAMIC_DEADLINE_SECONDS = 5 * 60.0
_SOURCE_DURATION_DEADLINE_FACTOR = 4.0
_MAX_SAMPLE_COUNT = 50
_MAX_JOB_DOCUMENT_BYTES = 2 * 1024 * 1024
_MAX_STALE_OWNER_SWEEP = 256
_CFR_TIMING_TOLERANCE_MSEC = 0.1
_SOURCE_HASH_CHUNK_BYTES = 1024 * 1024
_FFMPEG_DRAIN_QUEUE_SIZE = 256
_FFMPEG_MAX_LINE_BYTES = 256 * 1024
_FFMPEG_MAX_OUTPUT_BYTES = 128 * 1024 * 1024
_FFMPEG_DRAIN_JOIN_SECONDS = 2.0
_TERMINAL_STATUSES = {"ready", "failed", "cancelled", "blocked"}
_ACTIVE_STATUSES = {"queued", "running", "committing"}
_ROOT_LOCKS_GUARD = threading.Lock()
_ROOT_LOCKS: dict[str, _RootRegistryLock] = {}
_ROOT_EXECUTION_LOCKS: dict[str, threading.Lock] = {}
_DIRECTORY_MUTATION_CONTEXT = threading.local()
_SAFE_FAILURE_CODES = frozenset(
    {
        "active_review_proxy_conflict",
        "artifact_write_failed",
        "cancelled",
        "commit_in_progress",
        "disk_exhausted",
        "duplicate_review_proxy_retry",
        "ffmpeg_drain_failed",
        "ffmpeg_failed",
        "ffmpeg_output_limit_exceeded",
        "ffmpeg_unavailable",
        "host_out_of_memory",
        "insufficient_proxy_disk_capacity",
        "invalid_cancel_token",
        "invalid_resource_limit",
        "invalid_review_proxy_job",
        "invalid_review_proxy_media",
        "invalid_review_proxy_output",
        "invalid_review_proxy_progress",
        "invalid_review_proxy_request",
        "invalid_review_proxy_result",
        "invalid_review_proxy_retry",
        "invalid_review_proxy_sample",
        "invalid_review_proxy_samples",
        "invalid_review_proxy_streams",
        "invalid_review_proxy_verifier_evidence",
        "invalid_snapshot_path",
        "invalid_worker_command",
        "invalid_worker_protocol",
        "libx264_unavailable",
        "path_unavailable",
        "repair_execution_binding_changed",
        "resource_limit_exceeded",
        "result_already_exists",
        "review_proxy_decode_shortfall",
        "review_proxy_digest_mismatch",
        "review_proxy_failed",
        "review_proxy_frame_count_mismatch",
        "review_proxy_frame_sync_changed",
        "review_proxy_manifest_mismatch",
        "review_proxy_not_found",
        "review_proxy_output_limit_exceeded",
        "review_proxy_ownership_lost",
        "review_proxy_retry_ineligible",
        "review_proxy_sample_integrity_mismatch",
        "review_proxy_sample_low_information",
        "review_proxy_sample_mapping_mismatch",
        "review_proxy_sample_mismatch",
        "review_proxy_sample_shortfall",
        "review_proxy_timing_invalid",
        "review_proxy_worker_containment_unavailable",
        "review_proxy_worker_died",
        "review_proxy_worker_heartbeat_timeout",
        "review_proxy_worker_start_failed",
        "review_proxy_worker_timeout",
        "service_closed",
        "service_shutting_down",
        "source_changed",
        "source_digest_or_size_mismatch",
        "source_snapshot_changed",
        "source_snapshot_close_failed",
        "source_snapshot_missing",
        "unsafe_result_tree",
        "unsafe_review_proxy_output",
        "unsafe_snapshot_path",
        "unsupported_proxy_aspect_ratio",
    }
)
_SAFE_FAILURE_MESSAGES = {
    "cancelled": "Detector review proxy was cancelled",
    "disk_exhausted": "Review proxy storage capacity was exhausted",
    "insufficient_proxy_disk_capacity": "Review proxy output capacity preflight failed",
    "review_proxy_worker_timeout": "Review proxy worker exceeded its bounded execution deadline",
    "review_proxy_worker_heartbeat_timeout": "Review proxy worker heartbeat stopped",
    "source_changed": "Review proxy source changed or became unavailable",
    "source_digest_or_size_mismatch": "Review proxy source no longer matches its frozen binding",
}


def _safe_review_proxy_failure_code(value: Any) -> str:
    if isinstance(value, str) and value in _SAFE_FAILURE_CODES:
        return value
    return "review_proxy_failed"


def _safe_review_proxy_failure_message(code: Any) -> str:
    safe_code = _safe_review_proxy_failure_code(code)
    return _SAFE_FAILURE_MESSAGES.get(safe_code, "Review proxy operation failed")


def _record_generation(record: dict[str, Any]) -> int:
    generation = record.get("record_generation", 0)
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise DetectorDevelopmentError(
            "invalid_review_proxy_job",
            "Persisted review proxy record generation is invalid",
        )
    return generation


def _lease_identity_value(value: Any) -> tuple[int, int] | None:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(isinstance(part, bool) or not isinstance(part, int) or part < 0 for part in value)
    ):
        return None
    return int(value[0]), int(value[1])


class _RootRegistryLock:
    """Re-entrant metadata transaction lock backed by one hardened OS lease."""

    def __init__(self, root: Path) -> None:
        self._thread_lock = threading.RLock()
        leases = secure_mkdirs(root, "leases")
        self._lease = _HardenedLease(
            secure_mkdirs(leases, "registry"),
            trusted_root=leases,
            label="registry",
        )
        self._local = threading.local()

    def __enter__(self) -> _RootRegistryLock:
        self._thread_lock.acquire()
        depth = int(getattr(self._local, "depth", 0))
        if depth:
            self._local.depth = depth + 1
            return self
        held = None
        try:
            held = self._lease.acquire(blocking=True)
            if held is None:
                raise DetectorDevelopmentError("unsafe_result_tree", "Review proxy registry lease is unavailable")
            self._local.held = held
            self._local.depth = 1
            return self
        except BaseException:
            try:
                if held is not None:
                    held.release()
            finally:
                self._thread_lock.release()
            raise

    def validate(self) -> None:
        held = getattr(self._local, "held", None)
        if held is None or int(getattr(self._local, "depth", 0)) <= 0:
            raise DetectorDevelopmentError("unsafe_result_tree", "Review proxy registry lease is not held")
        held.validate()

    @property
    def identity(self) -> tuple[int, int]:
        return self._lease.identity

    def __exit__(self, exc_type, exc, traceback) -> None:
        depth = int(getattr(self._local, "depth", 0))
        try:
            if depth > 1:
                self._local.depth = depth - 1
                return
            held = getattr(self._local, "held", None)
            self._local.depth = 0
            self._local.held = None
            if held is not None:
                held.release()
        finally:
            self._thread_lock.release()


def _root_lock(root: Path) -> _RootRegistryLock:
    key = os.path.normcase(str(root))
    with _ROOT_LOCKS_GUARD:
        return _ROOT_LOCKS.setdefault(key, _RootRegistryLock(root))


def _execution_lock(root: Path) -> threading.Lock:
    key = os.path.normcase(str(root))
    with _ROOT_LOCKS_GUARD:
        return _ROOT_EXECUTION_LOCKS.setdefault(key, threading.Lock())


class DetectorReviewProxyCoordinator:
    """Persistent single-slot coordinator for server-owned review proxies."""

    def __init__(
        self,
        repo_root: Path,
        *,
        runner: Callable[..., dict[str, Any]] | None = None,
        verifier: Callable[
            [dict[str, Any], Path, dict[str, Any], Callable[[], bool], Callable[[int, int], None]],
            dict[str, Any] | None,
        ]
        | None = None,
        auto_start_workers: bool = True,
        worker_deadline_seconds: float = _DEFAULT_WORKER_DEADLINE_SECONDS,
        worker_heartbeat_timeout_seconds: float = _DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
        output_hard_limit_bytes: int = _DEFAULT_OUTPUT_HARD_LIMIT_BYTES,
        disk_reserve_bytes: int = _DEFAULT_DISK_RESERVE_BYTES,
        worker_command_factory: Callable[[Path, Path, int], list[str]] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve(strict=True)
        development = secure_mkdirs(self.repo_root, "data", "ball_detector_development_v1")
        self._root = secure_mkdirs(development, "review_proxies")
        self._jobs_root = secure_mkdirs(self._root, "jobs")
        self._cancel_root = secure_mkdirs(self._root, "cancel")
        self._results_root = secure_mkdirs(self._root, "results")
        self._leases_root = secure_mkdirs(self._root, "leases")
        self._job_leases_root = secure_mkdirs(self._leases_root, "jobs")
        self._owner_leases_root = secure_mkdirs(self._leases_root, "owners")
        self._execution_lease = _HardenedLease(
            secure_mkdirs(self._leases_root, "execution"),
            trusted_root=self._leases_root,
            label="execution",
        )
        self._lock = _root_lock(self._root)
        self._execution_lock = _execution_lock(self._root)
        self._runner = runner
        self._verifier = verifier or _verify_staged_media
        self._auto_start_workers = bool(auto_start_workers)
        self._worker_deadline_seconds = max(1.0, float(worker_deadline_seconds))
        self._heartbeat_timeout_seconds = max(1.0, float(worker_heartbeat_timeout_seconds))
        self._output_hard_limit_bytes = _positive_int(output_hard_limit_bytes, "review proxy output hard limit")
        self._disk_reserve_bytes = _nonnegative_int(disk_reserve_bytes, "review proxy disk reserve")
        self._worker_command_factory = worker_command_factory or self._default_worker_command
        self._owner_id = f"proxy-owner-{uuid.uuid4().hex}"
        self._owner_generation = f"proxy-generation-{uuid.uuid4().hex}"
        self._owner_started_at = utc_now_iso()
        owner_lease_key = hashlib.sha256(f"{self._owner_id}\0{self._owner_generation}".encode()).hexdigest()[:32]
        with self._lock:
            self._owner_lease_dir = secure_mkdirs(self._owner_leases_root, f"owner-{owner_lease_key}")
            self._owner_lease_object = _HardenedLease(
                self._owner_lease_dir,
                trusted_root=self._leases_root,
                label="owner",
            )
            self._owner_lease = self._acquire_owner_lease()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._closed = False
        self._execution_lifetime = threading.Condition()
        self._active_execution_threads: dict[int, int] = {}
        self._owner_release_pending = False
        self._dispatch_event = threading.Event()
        self._shutdown_event = threading.Event()
        self._dispatcher: threading.Thread | None = None
        self._child_lock = threading.Lock()
        self._child: subprocess.Popen[bytes] | None = None
        self._child_job_handle: int | None = None
        try:
            self._load_and_recover_jobs()
        except BaseException:
            self._release_owner_lease()
            raise
        if self._auto_start_workers:
            self._start_dispatcher()
            if any(record["status"] == "queued" for record in self._jobs.values()):
                self._dispatch_event.set()

    def create_proxy(self, request: dict[str, Any]) -> dict[str, Any]:
        self._require_open()
        frozen = self._freeze_request(request)
        request_sha256 = canonical_sha256(frozen)
        with self._lock:
            self._refresh_jobs_from_disk()
            for record in self._jobs.values():
                if record.get("request_sha256") == request_sha256:
                    return self._public_record(record)
            if any(record.get("status") in _ACTIVE_STATUSES for record in self._jobs.values()):
                raise DetectorDevelopmentError(
                    "active_review_proxy_conflict",
                    "Another detector review proxy job is already active",
                )
            job_id = f"repair-{request_sha256[:16]}-{uuid.uuid4().hex[:12]}"
            now = utc_now_iso()
            total = int(frozen["source_frame_count"]) * 3 + len(frozen["sampled_frame_indices"])
            record: dict[str, Any] = {
                "schema_version": "1.0",
                "artifact_type": "detector_review_proxy_job",
                "job_id": job_id,
                "repair_id": job_id,
                "attempt_root_repair_id": job_id,
                "attempt_number": 1,
                "retry_from_repair_id": None,
                "request_sha256": request_sha256,
                "status": "queued",
                "stage": "queued",
                "progress": {"completed": 0, "total": total, "updated_at": now},
                "frozen_request": frozen,
                "owner_id": None,
                "owner_generation": None,
                "owner_heartbeat_at": None,
                "cancel_requested": False,
                "error_code": None,
                "error_message": None,
                "recovery_action": None,
                "report": None,
                "result_manifest_sha256": None,
                "commit_manifest_sha256": None,
                "record_generation": 0,
                "coordination_bindings": self._coordination_bindings(),
                "created_at": now,
                "updated_at": now,
            }
            self._jobs[job_id] = record
            self._persist_record(record)
            self._persist_cancel_token(record)
            response = self._public_record(record)
        if self._auto_start_workers:
            self._dispatch_event.set()
        return response

    def retry_proxy(self, repair_id: str, *, allow_ready_pre_reveal: bool = False) -> dict[str, Any]:
        """Create one append-only server-authoritative retry attempt."""

        self._require_open()
        repair_id = require_safe_id(repair_id, "retry source repair_id")
        with self._lock:
            self._refresh_jobs_from_disk()
            source_record = self._record(repair_id)
            allowed_statuses = {"failed", "blocked", "cancelled"}
            if allow_ready_pre_reveal:
                allowed_statuses.add("ready")
            if source_record.get("status") not in allowed_statuses:
                raise DetectorDevelopmentError(
                    "review_proxy_retry_ineligible",
                    "Only a terminal unsuccessful review proxy can be retried",
                    status_code=409,
                )
            existing = [record for record in self._jobs.values() if record.get("retry_from_repair_id") == repair_id]
            if len(existing) > 1:
                raise DetectorDevelopmentError(
                    "duplicate_review_proxy_retry",
                    "Multiple review-proxy retries share one parent attempt",
                )
            if existing:
                return self._public_record(existing[0])
            if any(record.get("status") in _ACTIVE_STATUSES for record in self._jobs.values()):
                raise DetectorDevelopmentError(
                    "active_review_proxy_conflict",
                    "Another detector review proxy job is already active",
                    status_code=409,
                )
            prior = source_record.get("frozen_request")
            if not isinstance(prior, dict):
                raise DetectorDevelopmentError(
                    "invalid_review_proxy_retry",
                    "Retry source authority is incomplete",
                )
            frozen = self._freeze_request(
                {
                    "source_id": prior["source_id"],
                    "source_relative_path": prior["source_relative_path"],
                    "source_sha256": prior["source_sha256"],
                    "source_size_bytes": prior["source_size_bytes"],
                    "source_width": prior["source_width"],
                    "source_height": prior["source_height"],
                    "source_frame_count": prior["source_frame_count"],
                    "source_fps": prior["source_fps"],
                    "sampled_frame_indices": prior["sampled_frame_indices"],
                }
            )
            root_id = require_safe_id(
                source_record.get("attempt_root_repair_id", repair_id),
                "retry root repair_id",
            )
            prior_attempt = source_record.get("attempt_number", 1)
            if isinstance(prior_attempt, bool) or not isinstance(prior_attempt, int) or prior_attempt < 1:
                raise DetectorDevelopmentError("invalid_review_proxy_retry", "Retry attempt lineage is invalid")
            attempt_number = prior_attempt + 1
            frozen.update(
                retry_from_repair_id=repair_id,
                attempt_root_repair_id=root_id,
                attempt_number=attempt_number,
            )
            request_sha256 = canonical_sha256(frozen)
            job_id = f"repair-{request_sha256[:16]}-{uuid.uuid4().hex[:12]}"
            now = utc_now_iso()
            total = int(frozen["source_frame_count"]) * 3 + len(frozen["sampled_frame_indices"])
            record = {
                "schema_version": "1.0",
                "artifact_type": "detector_review_proxy_job",
                "job_id": job_id,
                "repair_id": job_id,
                "attempt_root_repair_id": root_id,
                "attempt_number": attempt_number,
                "retry_from_repair_id": repair_id,
                "request_sha256": request_sha256,
                "status": "queued",
                "stage": "queued",
                "progress": {"completed": 0, "total": total, "updated_at": now},
                "frozen_request": frozen,
                "owner_id": None,
                "owner_generation": None,
                "owner_heartbeat_at": None,
                "cancel_requested": False,
                "error_code": None,
                "error_message": None,
                "recovery_action": None,
                "report": None,
                "result_manifest_sha256": None,
                "commit_manifest_sha256": None,
                "record_generation": 0,
                "coordination_bindings": self._coordination_bindings(),
                "created_at": now,
                "updated_at": now,
            }
            self._jobs[job_id] = record
            self._persist_record(record)
            self._persist_cancel_token(record)
            response = self._public_record(record)
        if self._auto_start_workers:
            self._dispatch_event.set()
        return response

    retry_repair = retry_proxy

    def get_proxy(self, job_id: str) -> dict[str, Any]:
        job_id = require_safe_id(job_id, "detector review proxy job_id")
        with self._lock:
            self._refresh_jobs_from_disk()
            return self._public_record(self._record(job_id))

    create_repair = create_proxy
    get_repair = get_proxy

    def get_verified_proxy(self, job_id: str) -> dict[str, Any]:
        job_id = require_safe_id(job_id, "detector review proxy job_id")
        with self._lock:
            self._refresh_jobs_from_disk()
            record = deepcopy(self._record(job_id))
        if record.get("status") != "ready":
            return self._public_record(record)
        report, manifest_sha256 = self._validate_result_tree(self._results_root / job_id, record)
        if manifest_sha256 != record.get("result_manifest_sha256"):
            raise DetectorDevelopmentError(
                "review_proxy_manifest_mismatch",
                "Detector review proxy manifest changed after publication",
            )
        record["report"] = report
        return self._public_record(record)

    def cancel_proxy(self, job_id: str) -> dict[str, Any]:
        job_id = require_safe_id(job_id, "detector review proxy job_id")
        with self._lock:
            record = self._refresh_job_from_disk(job_id)
            if record["status"] == "committing":
                raise DetectorDevelopmentError(
                    "commit_in_progress",
                    "Detector review proxy commit can no longer be cancelled",
                )
            if record["status"] in _TERMINAL_STATUSES:
                return self._public_record(record)
            record["cancel_requested"] = True
            record["updated_at"] = utc_now_iso()
            if record["status"] == "queued":
                record.update(
                    status="cancelled",
                    stage="cancelled",
                    owner_id=None,
                    owner_generation=None,
                    owner_heartbeat_at=None,
                    error_code="cancelled",
                    recovery_action="retry",
                )
            self._persist_record(record)
            self._persist_cancel_token(record)
            return self._public_record(record)

    cancel_repair = cancel_proxy

    def execute_proxy(self, job_id: str) -> None:
        job_id = require_safe_id(job_id, "detector review proxy job_id")
        self._execute_in_global_slot(job_id)

    execute_repair = execute_proxy

    def close(self) -> None:
        with self._execution_lifetime:
            if self._closed:
                return
            self._closed = True
        self._shutdown_event.set()
        self._dispatch_event.set()
        with self._child_lock:
            child = self._child
        if child is not None and child.poll() is None:
            _terminate_process_tree(child)
        if self._dispatcher is not None and self._dispatcher is not threading.current_thread():
            self._dispatcher.join(timeout=2.0)
        release_owner = False
        with self._execution_lifetime:
            if self._active_execution_threads:
                self._owner_release_pending = True
            else:
                release_owner = True
        if release_owner:
            self._release_owner_lease()

    def _require_open(self) -> None:
        if self._closed:
            raise DetectorDevelopmentError("service_closed", "Detector review proxy service is closed")

    def _freeze_request(self, request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise DetectorDevelopmentError(
                "invalid_review_proxy_request",
                "Detector review proxy request must be an object",
                status_code=400,
            )
        allowed = {
            "source_id",
            "source_relative_path",
            "source_sha256",
            "source_size_bytes",
            "source_width",
            "source_height",
            "source_frame_count",
            "source_fps",
            "sampled_frame_indices",
        }
        if set(request) != allowed:
            raise DetectorDevelopmentError(
                "invalid_review_proxy_request",
                "Detector review proxy request fields are invalid",
                status_code=400,
            )
        source_id = require_safe_id(request["source_id"], "review proxy source_id")
        source_path = require_trusted_relative_path(
            self.repo_root,
            request["source_relative_path"],
            "review proxy source",
            allowed_first_parts={"data"},
        )
        expected_sha256 = require_sha256(request["source_sha256"], "review proxy source_sha256")
        expected_size = _positive_int(request["source_size_bytes"], "review proxy source size")
        width = _positive_int(request["source_width"], "review proxy source width")
        height = _positive_int(request["source_height"], "review proxy source height")
        frame_count = _positive_int(request["source_frame_count"], "review proxy source frame count")
        fps = _positive_finite(request["source_fps"], "review proxy source FPS")
        if width * PROXY_HEIGHT != height * PROXY_WIDTH:
            raise DetectorDevelopmentError(
                "unsupported_proxy_aspect_ratio",
                "Review proxy requires the source to have the same 32:9 aspect ratio",
                status_code=400,
            )
        indices = request["sampled_frame_indices"]
        if (
            not isinstance(indices, list)
            or not indices
            or len(indices) > _MAX_SAMPLE_COUNT
            or any(isinstance(index, bool) or not isinstance(index, int) for index in indices)
            or indices != sorted(set(indices))
            or indices[0] < 0
            or indices[-1] >= frame_count
        ):
            raise DetectorDevelopmentError(
                "invalid_review_proxy_samples",
                "Review proxy sampled frame indices must be unique, sorted, and in range",
                status_code=400,
            )
        ancestors = _capture_ancestor_identities(
            source_path,
            self.repo_root,
            "review proxy source",
        )
        identity = regular_file_change_identity(source_path, "review proxy source")
        if identity[2] != expected_size:
            raise DetectorDevelopmentError(
                "source_digest_or_size_mismatch",
                "Review proxy source size does not match its server binding",
            )
        if not _ancestor_identities_are_current(ancestors):
            raise DetectorDevelopmentError(
                "source_changed",
                "Review proxy source ancestor changed while freezing the request",
            )
        repair_execution_binding = _repair_execution_binding()
        return {
            "source_id": source_id,
            "source_relative_path": str(request["source_relative_path"]).replace("\\", "/"),
            "source_sha256": expected_sha256,
            "source_size_bytes": expected_size,
            "source_change_identity": list(identity),
            "source_width": width,
            "source_height": height,
            "source_frame_count": frame_count,
            "source_fps": fps,
            "sampled_frame_indices": list(indices),
            "proxy_width": PROXY_WIDTH,
            "proxy_height": PROXY_HEIGHT,
            "proxy_policy": "server_owned_h264_cfr_v1",
            "repair_execution_binding": repair_execution_binding,
        }

    def _execute_in_global_slot(self, requested_job_id: str | None) -> bool:
        self._begin_execution_lifetime()
        try:
            with self._execution_lock:
                execution_lease = self._acquire_execution_lease()
                try:
                    execution_lease.validate()
                    self._recover_orphaned_active_jobs(execution_lease)
                    claimed = self._claim_job(requested_job_id, execution_lease)
                    if claimed is None:
                        return False
                    job_id, record, job_lease = claimed
                    try:
                        self._execute_claimed(
                            job_id,
                            record,
                            execution_lease=execution_lease,
                            job_lease=job_lease,
                        )
                    finally:
                        job_lease.release()
                    return True
                finally:
                    execution_lease.release()
        finally:
            self._end_execution_lifetime()

    def _begin_execution_lifetime(self) -> None:
        thread_id = threading.get_ident()
        with self._execution_lifetime:
            if self._closed:
                raise DetectorDevelopmentError("service_closed", "Detector review proxy service is closed")
            self._active_execution_threads[thread_id] = self._active_execution_threads.get(thread_id, 0) + 1

    def _end_execution_lifetime(self) -> None:
        thread_id = threading.get_ident()
        release_owner = False
        with self._execution_lifetime:
            depth = self._active_execution_threads.get(thread_id, 0)
            if depth <= 1:
                self._active_execution_threads.pop(thread_id, None)
            else:
                self._active_execution_threads[thread_id] = depth - 1
            if not self._active_execution_threads:
                self._execution_lifetime.notify_all()
                if self._closed and self._owner_release_pending:
                    self._owner_release_pending = False
                    release_owner = True
        if release_owner:
            self._release_owner_lease()

    def _claim_job(
        self,
        requested_job_id: str | None,
        execution_lease: _HeldLease,
    ) -> tuple[str, dict[str, Any], _HeldLease] | None:
        execution_lease.validate()
        with self._lock:
            execution_lease.validate()
            self._refresh_jobs_from_disk()
            candidates = (
                [requested_job_id]
                if requested_job_id is not None
                else sorted(
                    (job_id for job_id, record in self._jobs.items() if record.get("status") == "queued"),
                    key=lambda item: self._jobs[item]["created_at"],
                )
            )
            for job_id in candidates:
                if job_id is None:
                    continue
                record = self._jobs.get(job_id)
                if record is None or record.get("status") != "queued":
                    continue
                if not self._root_coordination_matches(record):
                    raise DetectorDevelopmentError(
                        "unsafe_result_tree",
                        "Review proxy coordination identity changed before claim",
                    )
                if record.get("cancel_requested") is True:
                    self._mark_cancelled(record)
                    return None
                selected_job_id = job_id
                selected_generation = _record_generation(record)
                break
            else:
                return None
        job_lease, job_lease_identity = self._acquire_job_lease(selected_job_id)
        transferred = False
        try:
            with self._lock:
                execution_lease.validate()
                job_lease.validate()
                self._owner_lease.validate()
                record = self._refresh_job_from_disk(selected_job_id)
                if (
                    record.get("status") != "queued"
                    or _record_generation(record) != selected_generation
                    or not self._root_coordination_matches(record)
                ):
                    return None
                if record.get("cancel_requested") is True:
                    self._mark_cancelled(record)
                    return None
                now = utc_now_iso()
                record.update(
                    status="running",
                    stage="verifying_source",
                    owner_id=self._owner_id,
                    owner_generation=self._owner_generation,
                    owner_heartbeat_at=now,
                    coordination_bindings=self._coordination_bindings(
                        job_identity=job_lease_identity,
                        owner_identity=self._owner_lease_object.identity,
                    ),
                    updated_at=now,
                )
                record["progress"]["updated_at"] = now
                self._persist_record(record)
                transferred = True
                return selected_job_id, deepcopy(record), job_lease
        finally:
            if not transferred:
                job_lease.release()

    def _execute_claimed(
        self,
        job_id: str,
        record: dict[str, Any],
        *,
        execution_lease: _HeldLease,
        job_lease: _HeldLease,
    ) -> None:
        staging: Path | None = None
        staging_identity: tuple[int, int] | None = None
        destination = self._results_root / job_id
        try:
            execution_lease.validate()
            job_lease.validate()
            deadline = time.monotonic() + self._dynamic_deadline_seconds(record["frozen_request"])

            def should_cancel() -> bool:
                if self._shutdown_event.is_set():
                    raise DetectorDevelopmentError(
                        "service_shutting_down",
                        "Review proxy service is shutting down",
                    )
                if time.monotonic() >= deadline:
                    raise DetectorDevelopmentError(
                        "review_proxy_worker_timeout",
                        "Review proxy job exceeded its bounded execution deadline",
                    )
                return self._cancellation_requested(job_id)

            def progress(completed: int, total: int) -> None:
                self._record_progress(job_id, completed, total)

            available = shutil.disk_usage(self._results_root).free
            source_snapshot_bytes = _positive_int(
                record["frozen_request"]["source_size_bytes"],
                "review proxy source size",
            )
            required = self._output_hard_limit_bytes + source_snapshot_bytes + self._disk_reserve_bytes
            if available < required:
                raise DetectorDevelopmentError(
                    "insufficient_proxy_disk_capacity",
                    "Review proxy output capacity preflight failed",
                )
            request = self._execution_request(record, should_cancel)
            staging = secure_mkdirs(self._results_root, f".{job_id}.staging-{uuid.uuid4().hex}")
            staging_identity = _safe_directory_identity(staging)
            self._set_stage(job_id, "transcoding")
            if self._runner is None:
                output = self._run_supervised_worker(job_id, request, staging, deadline)
                _safe_remove_tree(staging / ".worker-protocol", staging)
            else:
                output = self._runner(
                    request,
                    staging,
                    should_cancel,
                    progress,
                )
            if should_cancel():
                raise DetectorDevelopmentError("cancelled", "Detector review proxy was cancelled")
            self._execution_request(record, should_cancel)
            self._set_stage(job_id, "independent_verification")
            report, manifest = self._seal_result(record, output, staging, should_cancel, progress)
            atomic_write_json(
                staging / "detector_review_proxy_report.v1.json",
                report,
                trusted_root=staging,
            )
            report_sha, report_size = hash_regular_file(
                staging / "detector_review_proxy_report.v1.json",
                "review proxy report",
                trusted_root=staging,
            )
            manifest["report_file_sha256"] = report_sha
            manifest["report_file_size_bytes"] = report_size
            atomic_write_json(
                staging / "detector_review_proxy_manifest.v1.json",
                manifest,
                trusted_root=staging,
            )
            manifest_sha, _ = hash_regular_file(
                staging / "detector_review_proxy_manifest.v1.json",
                "review proxy manifest",
                trusted_root=staging,
            )
            self._validate_result_tree(staging, record)
            with self._lock:
                self._lock.validate()
                execution_lease.validate()
                job_lease.validate()
                self._owner_lease.validate()
                current = self._refresh_job_from_disk(job_id)
                self._require_current_owner(current, status="running")
                if current["cancel_requested"]:
                    expected_generation = _record_generation(current)
                    self._coordination_failpoint("before_cancel_cleanup", job_id)
                    self._lock.validate()
                    execution_lease.validate()
                    job_lease.validate()
                    self._owner_lease.validate()
                    current = self._refresh_job_from_disk(job_id)
                    self._require_current_owner(current, status="running")
                    if current["cancel_requested"] is not True or _record_generation(current) != expected_generation:
                        raise DetectorDevelopmentError(
                            "review_proxy_ownership_lost",
                            "Review proxy cancellation authority changed before cleanup",
                        )
                    if not _safe_remove_tree(
                        staging,
                        self._results_root,
                        expected_identity=staging_identity,
                    ):
                        raise DetectorDevelopmentError(
                            "unsafe_result_tree",
                            "Review proxy staging cleanup could not be verified",
                        )
                    staging = None
                    self._lock.validate()
                    execution_lease.validate()
                    job_lease.validate()
                    self._owner_lease.validate()
                    current = self._refresh_job_from_disk(job_id)
                    self._require_current_owner(current, status="running")
                    if current["cancel_requested"] is not True or _record_generation(current) != expected_generation:
                        raise DetectorDevelopmentError(
                            "review_proxy_ownership_lost",
                            "Review proxy cancellation authority changed during cleanup",
                        )
                    self._mark_cancelled(current)
                    return
                now = utc_now_iso()
                current.update(
                    status="committing",
                    stage="committing",
                    commit_manifest_sha256=manifest_sha,
                    owner_heartbeat_at=now,
                    updated_at=now,
                )
                self._persist_record(current)
            self._execution_request(record, should_cancel)
            with self._lock:
                self._lock.validate()
                execution_lease.validate()
                job_lease.validate()
                self._owner_lease.validate()
                current = self._refresh_job_from_disk(job_id)
                self._require_current_owner(current, status="committing")
                if current.get("commit_manifest_sha256") != manifest_sha:
                    raise DetectorDevelopmentError(
                        "review_proxy_ownership_lost",
                        "Review proxy commit authority changed before publication",
                    )
                expected_generation = _record_generation(current)
                self._coordination_failpoint("before_publish", job_id)
                self._lock.validate()
                execution_lease.validate()
                job_lease.validate()
                self._owner_lease.validate()
                current = self._refresh_job_from_disk(job_id)
                self._require_current_owner(current, status="committing")
                if (
                    current.get("commit_manifest_sha256") != manifest_sha
                    or _record_generation(current) != expected_generation
                ):
                    raise DetectorDevelopmentError(
                        "review_proxy_ownership_lost",
                        "Review proxy commit authority changed before publication",
                    )
                previous_identity = getattr(_DIRECTORY_MUTATION_CONTEXT, "publish_identity", None)
                _DIRECTORY_MUTATION_CONTEXT.publish_identity = staging_identity
                try:
                    _publish_staging_directory(staging, destination)
                finally:
                    _DIRECTORY_MUTATION_CONTEXT.publish_identity = previous_identity
                staging = None
                self._coordination_failpoint("after_publish", job_id)
                self._lock.validate()
                execution_lease.validate()
                job_lease.validate()
                self._owner_lease.validate()
                current = self._refresh_job_from_disk(job_id)
                self._require_current_owner(current, status="committing")
                if (
                    current.get("commit_manifest_sha256") != manifest_sha
                    or _record_generation(current) != expected_generation
                ):
                    raise DetectorDevelopmentError(
                        "review_proxy_ownership_lost",
                        "Review proxy commit authority changed during publication",
                    )
            committed_report, committed_sha = self._validate_result_tree(destination, record)
            if committed_sha != manifest_sha:
                raise DetectorDevelopmentError(
                    "review_proxy_manifest_mismatch",
                    "Review proxy manifest changed during publication",
                )
            self._finalize_ready(
                job_id,
                committed_report,
                committed_sha,
                expected_owner_id=self._owner_id,
                expected_owner_generation=self._owner_generation,
                execution_lease=execution_lease,
                job_lease=job_lease,
            )
        except Exception as exc:
            if isinstance(exc, DetectorDevelopmentError) and exc.code == "review_proxy_ownership_lost":
                return
            self._recover_or_fail_owned_execution(
                job_id,
                exc,
                staging,
                staging_identity,
                destination,
                execution_lease=execution_lease,
                job_lease=job_lease,
            )

    def _coordination_failpoint(self, _stage: str, _job_id: str) -> None:
        """Test seam for generation changes at destructive transaction boundaries."""

    def _recover_or_fail_owned_execution(
        self,
        job_id: str,
        exc: Exception,
        staging: Path | None,
        staging_identity: tuple[int, int] | None,
        destination: Path,
        *,
        execution_lease: _HeldLease,
        job_lease: _HeldLease,
    ) -> None:
        code = _safe_review_proxy_failure_code(exc.code if isinstance(exc, DetectorDevelopmentError) else None)
        with self._lock:
            self._lock.validate()
            execution_lease.validate()
            job_lease.validate()
            self._owner_lease.validate()
            try:
                current = self._refresh_job_from_disk(job_id)
            except DetectorDevelopmentError:
                return
            if (
                current.get("status") not in {"running", "committing"}
                or current.get("owner_id") != self._owner_id
                or current.get("owner_generation") != self._owner_generation
            ):
                return
            expected_generation = _record_generation(current)
            expected_status = current.get("status")
            expected_commit = current.get("commit_manifest_sha256")
            self._coordination_failpoint("before_failure_cleanup", job_id)
            self._lock.validate()
            execution_lease.validate()
            job_lease.validate()
            self._owner_lease.validate()
            current = self._refresh_job_from_disk(job_id)
            if (
                current.get("status") != expected_status
                or current.get("owner_id") != self._owner_id
                or current.get("owner_generation") != self._owner_generation
                or current.get("commit_manifest_sha256") != expected_commit
                or _record_generation(current) != expected_generation
            ):
                return

            expected = current.get("commit_manifest_sha256")
            if current.get("status") == "committing" and isinstance(expected, str) and destination.exists():
                try:
                    report, actual = self._validate_result_tree(destination, current)
                except Exception:
                    pass
                else:
                    if actual == expected:
                        self._lock.validate()
                        execution_lease.validate()
                        job_lease.validate()
                        self._owner_lease.validate()
                        current = self._refresh_job_from_disk(job_id)
                        if (
                            current.get("status") != expected_status
                            or current.get("owner_id") != self._owner_id
                            or current.get("owner_generation") != self._owner_generation
                            or current.get("commit_manifest_sha256") != expected_commit
                            or _record_generation(current) != expected_generation
                        ):
                            return
                        self._apply_ready_record(current, report, actual)
                        self._persist_record(current)
                        self._persist_cancel_token(current)
                        return

            # Cleanup and the terminal state transition are one metadata
            # transaction. A replacement generation observed above leaves all
            # artifacts untouched for its rightful owner or later recovery.
            self._lock.validate()
            execution_lease.validate()
            job_lease.validate()
            self._owner_lease.validate()
            current = self._refresh_job_from_disk(job_id)
            if (
                current.get("status") != expected_status
                or current.get("owner_id") != self._owner_id
                or current.get("owner_generation") != self._owner_generation
                or current.get("commit_manifest_sha256") != expected_commit
                or _record_generation(current) != expected_generation
            ):
                return
            if _directory_entry_exists(destination):
                if staging_identity is None or not _safe_remove_tree(
                    destination,
                    self._results_root,
                    expected_identity=staging_identity,
                ):
                    raise DetectorDevelopmentError(
                        "unsafe_result_tree",
                        "Review proxy destination cleanup could not be verified",
                    )
            if (
                staging is not None
                and _directory_entry_exists(staging)
                and not _safe_remove_tree(
                    staging,
                    self._results_root,
                    expected_identity=staging_identity,
                )
            ):
                raise DetectorDevelopmentError(
                    "unsafe_result_tree",
                    "Review proxy staging cleanup could not be verified",
                )
            self._lock.validate()
            execution_lease.validate()
            job_lease.validate()
            self._owner_lease.validate()
            current = self._refresh_job_from_disk(job_id)
            if (
                current.get("status") != expected_status
                or current.get("owner_id") != self._owner_id
                or current.get("owner_generation") != self._owner_generation
                or current.get("commit_manifest_sha256") != expected_commit
                or _record_generation(current) != expected_generation
            ):
                return
            if code == "cancelled" and current.get("cancel_requested") is not True:
                code = "review_proxy_failed"
            status = "cancelled" if code == "cancelled" else "failed"
            current.update(
                status=status,
                stage=status,
                owner_id=None,
                owner_generation=None,
                owner_heartbeat_at=None,
                cancel_requested=status == "cancelled",
                error_code=code,
                error_message=_safe_review_proxy_failure_message(code),
                recovery_action="retry",
                report=None,
                result_manifest_sha256=None,
                commit_manifest_sha256=None,
                updated_at=utc_now_iso(),
            )
            current["progress"]["updated_at"] = current["updated_at"]
            self._persist_record(current)
            self._persist_cancel_token(current)

    def _dynamic_deadline_seconds(self, request: dict[str, Any]) -> float:
        duration = _positive_int(request["source_frame_count"], "review proxy source frame count") / (
            _positive_finite(request["source_fps"], "review proxy source FPS")
        )
        estimate = max(
            _MINIMUM_DYNAMIC_DEADLINE_SECONDS,
            _MINIMUM_DYNAMIC_DEADLINE_SECONDS + duration * _SOURCE_DURATION_DEADLINE_FACTOR,
        )
        return min(self._worker_deadline_seconds, estimate)

    def _finalize_ready(
        self,
        job_id: str,
        report: dict[str, Any],
        manifest_sha256: str,
        *,
        expected_owner_id: Any,
        expected_owner_generation: Any,
        execution_lease: _HeldLease,
        job_lease: _HeldLease,
    ) -> None:
        with self._lock:
            self._lock.validate()
            execution_lease.validate()
            job_lease.validate()
            self._owner_lease.validate()
            current = self._refresh_job_from_disk(job_id)
            if (
                current.get("status") != "committing"
                or current.get("owner_id") != expected_owner_id
                or current.get("owner_generation") != expected_owner_generation
                or current.get("commit_manifest_sha256") != manifest_sha256
            ):
                raise DetectorDevelopmentError(
                    "review_proxy_ownership_lost",
                    "Review proxy commit authority changed before finalization",
                )
            expected_generation = _record_generation(current)
            self._coordination_failpoint("before_finalize", job_id)
            self._lock.validate()
            execution_lease.validate()
            job_lease.validate()
            self._owner_lease.validate()
            current = self._refresh_job_from_disk(job_id)
            if (
                current.get("status") != "committing"
                or current.get("owner_id") != expected_owner_id
                or current.get("owner_generation") != expected_owner_generation
                or current.get("commit_manifest_sha256") != manifest_sha256
                or _record_generation(current) != expected_generation
            ):
                raise DetectorDevelopmentError(
                    "review_proxy_ownership_lost",
                    "Review proxy commit authority changed during finalization",
                )
            self._apply_ready_record(current, report, manifest_sha256)
            self._persist_record(current)
            self._persist_cancel_token(current)

    @staticmethod
    def _apply_ready_record(record: dict[str, Any], report: dict[str, Any], manifest_sha256: str) -> None:
        record.update(
            status="ready",
            stage="ready",
            owner_id=None,
            owner_generation=None,
            owner_heartbeat_at=None,
            cancel_requested=False,
            error_code=None,
            error_message=None,
            recovery_action=None,
            report=report,
            result_manifest_sha256=manifest_sha256,
            commit_manifest_sha256=None,
            updated_at=utc_now_iso(),
        )
        record["progress"].update(
            completed=record["progress"]["total"],
            updated_at=record["updated_at"],
        )

    def _execution_request(
        self,
        record: dict[str, Any],
        should_cancel: Callable[[], bool],
    ) -> dict[str, Any]:
        frozen = deepcopy(record["frozen_request"])
        _raise_if_cancelled(should_cancel)
        source = require_trusted_relative_path(
            self.repo_root,
            frozen["source_relative_path"],
            "frozen review proxy source",
            allowed_first_parts={"data"},
        )
        if list(regular_file_change_identity(source, "frozen review proxy source")) != frozen["source_change_identity"]:
            raise DetectorDevelopmentError("source_changed", "Review proxy source changed after request freezing")
        frozen["_source_path"] = str(source)
        frozen["_source_trusted_root"] = str(self.repo_root)
        frozen["_source_change_identity"] = list(frozen["source_change_identity"])
        frozen["_output_hard_limit_bytes"] = self._output_hard_limit_bytes
        return frozen

    def _seal_result(
        self,
        record: dict[str, Any],
        output: dict[str, Any],
        staging: Path,
        should_cancel: Callable[[], bool],
        progress: Callable[[int, int], None],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(output, dict):
            raise DetectorDevelopmentError("invalid_review_proxy_output", "Review proxy worker output is invalid")
        frozen = record["frozen_request"]
        required_equal = {
            "source_sha256": frozen["source_sha256"],
            "source_size_bytes": frozen["source_size_bytes"],
            "source_width": frozen["source_width"],
            "source_height": frozen["source_height"],
            "source_frame_count": frozen["source_frame_count"],
            "source_fps": frozen["source_fps"],
            "proxy_width": PROXY_WIDTH,
            "proxy_height": PROXY_HEIGHT,
            "proxy_frame_count": frozen["source_frame_count"],
            "proxy_fps": frozen["source_fps"],
        }
        if any(output.get(key) != value for key, value in required_equal.items()):
            raise DetectorDevelopmentError(
                "invalid_review_proxy_output",
                "Review proxy output does not preserve the frozen media contract",
            )
        if any(
            not math.isclose(
                _positive_finite(output.get(key), f"review proxy {key}"),
                float(frozen["source_fps"]),
                rel_tol=0.0,
                abs_tol=1e-3,
            )
            for key in ("proxy_stream_fps", "proxy_average_fps")
        ):
            raise DetectorDevelopmentError(
                "invalid_review_proxy_media",
                "Review proxy stream or average FPS differs from the frozen source FPS",
            )
        if (
            output.get("source_change_identity_before") != frozen["source_change_identity"]
            or output.get("source_change_identity_after") != frozen["source_change_identity"]
        ):
            raise DetectorDevelopmentError("source_changed", "Review proxy worker observed a changed source")
        if output.get("repair_execution_binding") != frozen["repair_execution_binding"]:
            raise DetectorDevelopmentError(
                "repair_execution_binding_changed",
                "Review proxy repair execution binding changed",
            )
        verifier_evidence = self._verifier(output, staging, frozen, should_cancel, progress)
        proxy_path = _trusted_result_file(staging, output.get("proxy_relative_path"))
        proxy_sha, proxy_size = hash_regular_file(
            proxy_path,
            "review proxy media",
            max_bytes=self._output_hard_limit_bytes,
            trusted_root=staging,
        )
        if proxy_sha != output.get("proxy_sha256") or proxy_size != output.get("proxy_size_bytes"):
            raise DetectorDevelopmentError("review_proxy_digest_mismatch", "Review proxy media digest changed")
        _fsync_regular_file(proxy_path)
        samples = output.get("sampled_frames")
        if (
            not isinstance(samples, list)
            or [item.get("frame_index") for item in samples] != frozen["sampled_frame_indices"]
        ):
            raise DetectorDevelopmentError(
                "invalid_review_proxy_samples",
                "Review proxy samples do not cover the frozen exact indices",
            )
        for item in samples:
            _raise_if_cancelled(should_cancel)
            sample_path = _trusted_result_file(staging, item.get("relative_path"))
            sample_sha, sample_size = hash_regular_file(
                sample_path, "review proxy sample", max_bytes=32 * 1024 * 1024, trusted_root=staging
            )
            if (
                sample_sha != item.get("sha256")
                or sample_size != item.get("size_bytes")
                or item.get("width") != PROXY_WIDTH
                or item.get("height") != PROXY_HEIGHT
            ):
                raise DetectorDevelopmentError("review_proxy_sample_mismatch", "Review proxy sample binding changed")
            if not _is_clean_recorded_media_integrity(item.get("media_integrity")):
                raise DetectorDevelopmentError(
                    "review_proxy_sample_integrity_mismatch",
                    "Review proxy sample media-integrity binding is invalid",
                )
            _fsync_regular_file(sample_path)
        integrity: dict[str, Any] = {
            "source_identity_and_sha256_verified_before_and_after": True,
            "artifact_digests_verified": True,
            "generated_sample_media_integrity_verified": False,
            "independent_verification_performed": False,
            "full_proxy_decode_verified": False,
            "frame_count_exact": False,
            "fps_exact": False,
            "dimensions_exact": False,
            "pixel_format_yuv420p_verified": False,
            "sample_aspect_ratio_1_1_verified": False,
            "single_video_stream_without_auxiliary_streams": False,
            "sample_indices_exact": False,
            "sample_pixels_match_exact_proxy_frames": False,
            "sample_media_integrity_verified": False,
            "sample_count": 0,
            "sample_media_integrity": [],
        }
        if self._verifier is _verify_staged_media:
            if not _is_complete_verifier_evidence(verifier_evidence, frozen["sampled_frame_indices"]):
                raise DetectorDevelopmentError(
                    "invalid_review_proxy_verifier_evidence",
                    "Independent review proxy verifier evidence is incomplete",
                )
            integrity.update(deepcopy(verifier_evidence))
        report = {
            "schema_version": "1.0",
            "artifact_type": "detector_review_proxy_report",
            "repair_id": record["job_id"],
            "request_sha256": record["request_sha256"],
            "source": {
                key: frozen[key]
                for key in (
                    "source_id",
                    "source_relative_path",
                    "source_sha256",
                    "source_size_bytes",
                    "source_change_identity",
                    "source_width",
                    "source_height",
                    "source_frame_count",
                    "source_fps",
                )
            },
            "proxy": {
                "relative_path": output["proxy_relative_path"],
                "sha256": proxy_sha,
                "size_bytes": proxy_size,
                "width": PROXY_WIDTH,
                "height": PROXY_HEIGHT,
                "frame_count": frozen["source_frame_count"],
                "fps": frozen["source_fps"],
                "stream_fps": output.get("proxy_stream_fps"),
                "average_fps": output.get("proxy_average_fps"),
            },
            "sampled_frames": deepcopy(samples),
            "encoding": deepcopy(output.get("encoding")),
            "ffmpeg": deepcopy(output.get("ffmpeg")),
            "repair_execution_binding": deepcopy(output["repair_execution_binding"]),
            "integrity": integrity,
        }
        manifest = {
            "schema_version": "1.0",
            "artifact_type": "detector_review_proxy_result_manifest",
            "repair_id": record["job_id"],
            "request_sha256": record["request_sha256"],
            "proxy_sha256": proxy_sha,
            "proxy_size_bytes": proxy_size,
            "sample_sha256s": [item["sha256"] for item in samples],
            "integrity_sha256": canonical_sha256(integrity),
        }
        return report, manifest

    def _validate_result_tree(self, root: Path, record: dict[str, Any]) -> tuple[dict[str, Any], str]:
        report_bytes, _ = read_regular_bytes(
            root / "detector_review_proxy_report.v1.json",
            "review proxy report",
            max_bytes=_MAX_JOB_DOCUMENT_BYTES,
            trusted_root=root,
        )
        manifest_bytes, manifest_sha = read_regular_bytes(
            root / "detector_review_proxy_manifest.v1.json",
            "review proxy manifest",
            max_bytes=_MAX_JOB_DOCUMENT_BYTES,
            trusted_root=root,
        )
        report = json_object_from_bytes(report_bytes, "review proxy report")
        manifest = json_object_from_bytes(manifest_bytes, "review proxy manifest")
        if (
            report.get("repair_id") != record["job_id"]
            or report.get("request_sha256") != record["request_sha256"]
            or manifest.get("repair_id") != record["job_id"]
            or manifest.get("request_sha256") != record["request_sha256"]
            or hashlib.sha256(report_bytes).hexdigest() != manifest.get("report_file_sha256")
            or len(report_bytes) != manifest.get("report_file_size_bytes")
            or not isinstance(report.get("integrity"), dict)
            or canonical_sha256(report["integrity"]) != manifest.get("integrity_sha256")
        ):
            raise DetectorDevelopmentError(
                "invalid_review_proxy_result", "Published review proxy result binding is invalid"
            )
        proxy = report.get("proxy")
        if not isinstance(proxy, dict):
            raise DetectorDevelopmentError(
                "invalid_review_proxy_result", "Published review proxy media binding is invalid"
            )
        proxy_path = _trusted_result_file(root, proxy.get("relative_path"))
        samples = report.get("sampled_frames")
        if (
            not isinstance(samples, list)
            or any(not isinstance(item, dict) for item in samples)
            or [item.get("frame_index") for item in samples] != record["frozen_request"]["sampled_frame_indices"]
        ):
            raise DetectorDevelopmentError("invalid_review_proxy_samples", "Published review proxy samples are invalid")
        sample_paths = [_trusted_result_file(root, item.get("relative_path")) for item in samples]
        expected_files = {
            "detector_review_proxy_report.v1.json",
            "detector_review_proxy_manifest.v1.json",
            proxy_path.relative_to(root).as_posix(),
            *(sample.relative_to(root).as_posix() for sample in sample_paths),
        }
        tree_before = exact_regular_tree_snapshot(
            root,
            expected_files,
            "review proxy result tree",
            trusted_root=self._results_root,
        )
        repeated_report, _ = read_regular_bytes(
            root / "detector_review_proxy_report.v1.json",
            "review proxy report",
            max_bytes=_MAX_JOB_DOCUMENT_BYTES,
            trusted_root=root,
        )
        repeated_manifest, repeated_manifest_sha = read_regular_bytes(
            root / "detector_review_proxy_manifest.v1.json",
            "review proxy manifest",
            max_bytes=_MAX_JOB_DOCUMENT_BYTES,
            trusted_root=root,
        )
        if (
            repeated_report != report_bytes
            or repeated_manifest != manifest_bytes
            or repeated_manifest_sha != manifest_sha
        ):
            raise DetectorDevelopmentError(
                "source_changed",
                "Published review proxy documents changed during validation",
            )
        sealed_proxy_size = proxy.get("size_bytes")
        if (
            isinstance(sealed_proxy_size, bool)
            or not isinstance(sealed_proxy_size, int)
            or not 0 < sealed_proxy_size <= self._output_hard_limit_bytes
        ):
            raise DetectorDevelopmentError(
                "invalid_review_proxy_result",
                "Published review proxy size authority is invalid",
            )
        proxy_sha, proxy_size = hash_regular_file(
            proxy_path,
            "published review proxy",
            max_bytes=sealed_proxy_size,
            trusted_root=root,
        )
        if (
            proxy_sha != proxy.get("sha256")
            or proxy_size != proxy.get("size_bytes")
            or proxy_sha != manifest.get("proxy_sha256")
            or proxy_size != manifest.get("proxy_size_bytes")
        ):
            raise DetectorDevelopmentError("review_proxy_digest_mismatch", "Published review proxy digest changed")
        sample_hashes: list[str] = []
        sample_relative_paths: list[str] = []
        for item, sample in zip(samples, sample_paths, strict=True):
            digest, size = hash_regular_file(
                sample, "published review proxy sample", max_bytes=32 * 1024 * 1024, trusted_root=root
            )
            if digest != item.get("sha256") or size != item.get("size_bytes"):
                raise DetectorDevelopmentError("review_proxy_sample_mismatch", "Published review proxy sample changed")
            sample_hashes.append(digest)
            sample_relative_paths.append(sample.relative_to(root).as_posix())
        if sample_hashes != manifest.get("sample_sha256s"):
            raise DetectorDevelopmentError("review_proxy_sample_mismatch", "Review proxy sample manifest changed")
        tree_after = exact_regular_tree_snapshot(
            root,
            expected_files,
            "review proxy result tree",
            trusted_root=self._results_root,
        )
        if tree_after != tree_before:
            raise DetectorDevelopmentError(
                "source_changed",
                "Published review proxy result changed during validation",
            )
        return report, manifest_sha

    def _run_supervised_worker(
        self,
        job_id: str,
        request: dict[str, Any],
        staging: Path,
        deadline_monotonic: float,
    ) -> dict[str, Any]:
        protocol = secure_mkdirs(staging, ".worker-protocol")
        control = secure_mkdirs(protocol, "control")
        worker_id = f"proxy-worker-{uuid.uuid4().hex}"
        atomic_write_json(
            control / "input.json",
            {
                "schema_version": "1.0",
                "artifact_type": "detector_review_proxy_worker_input",
                "worker_id": worker_id,
                "request": request,
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
            raise DetectorDevelopmentError("invalid_worker_command", "Review proxy worker command is invalid")
        options: dict[str, Any] = {
            "cwd": str(Path(__file__).resolve().parents[1]),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "env": {**os.environ, "PYTHONNOUSERSITE": "1"},
            "close_fds": True,
        }
        if os.name == "nt":
            options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            options["start_new_session"] = True
        try:
            process = subprocess.Popen(command, **options)
        except OSError as exc:
            raise DetectorDevelopmentError(
                "review_proxy_worker_start_failed", "Review proxy worker could not start"
            ) from exc
        try:
            job_handle = _attach_windows_kill_job(process)
        except Exception as exc:
            _terminate_process_tree(process)
            raise DetectorDevelopmentError(
                "review_proxy_worker_containment_unavailable",
                "Review proxy worker process-tree containment is unavailable",
                status_code=503,
            ) from exc
        with self._child_lock:
            self._child = process
            self._child_job_handle = job_handle
        started = time.monotonic()
        last_heartbeat = started
        last_sequence = -1
        cancellation_started: float | None = None
        try:
            while process.poll() is None:
                now = time.monotonic()
                if self._shutdown_event.is_set():
                    _terminate_process_tree(process)
                    raise DetectorDevelopmentError("service_shutting_down", "Review proxy service is shutting down")
                if self._cancellation_requested(job_id):
                    if cancellation_started is None:
                        cancellation_started = now
                        self._write_worker_cancel(control, worker_id, True)
                    elif now - cancellation_started >= 2.0:
                        _terminate_process_tree(process)
                        raise DetectorDevelopmentError("cancelled", "Detector review proxy was cancelled")
                if now >= deadline_monotonic:
                    _terminate_process_tree(process)
                    raise DetectorDevelopmentError(
                        "review_proxy_worker_timeout",
                        "Review proxy worker exceeded its bounded execution deadline",
                    )
                heartbeat = _read_optional_json(control / "heartbeat.json", control, "review proxy worker heartbeat")
                if heartbeat is not None:
                    sequence = heartbeat.get("sequence")
                    if (
                        heartbeat.get("artifact_type") != "detector_review_proxy_worker_heartbeat"
                        or heartbeat.get("worker_id") != worker_id
                        or isinstance(sequence, bool)
                        or not isinstance(sequence, int)
                        or sequence < 0
                    ):
                        raise DetectorDevelopmentError(
                            "invalid_worker_protocol", "Review proxy worker heartbeat is invalid"
                        )
                    if sequence > last_sequence:
                        last_sequence = sequence
                        last_heartbeat = now
                if now - last_heartbeat >= self._heartbeat_timeout_seconds:
                    _terminate_process_tree(process)
                    raise DetectorDevelopmentError(
                        "review_proxy_worker_heartbeat_timeout",
                        "Review proxy worker heartbeat stopped",
                    )
                progress = _read_optional_json(control / "progress.json", control, "review proxy worker progress")
                if progress is not None:
                    self._record_progress(job_id, progress.get("completed"), progress.get("total"))
                self._shutdown_event.wait(0.1)
            if cancellation_started is not None:
                raise DetectorDevelopmentError("cancelled", "Detector review proxy was cancelled")
            result = _read_optional_json(
                control / "result.json", control, "review proxy worker result", max_bytes=64 * 1024 * 1024
            )
            error = _read_optional_json(control / "error.json", control, "review proxy worker error")
            if process.returncode == 0 and result is not None and error is None:
                if (
                    result.get("artifact_type") != "detector_review_proxy_worker_result"
                    or result.get("worker_id") != worker_id
                    or not isinstance(result.get("runner_output"), dict)
                ):
                    raise DetectorDevelopmentError("invalid_worker_protocol", "Review proxy worker result is invalid")
                return result["runner_output"]
            if error is not None and error.get("worker_id") == worker_id:
                code = _safe_review_proxy_failure_code(error.get("code"))
                status_code = error.get("status_code")
                if isinstance(status_code, bool) or not isinstance(status_code, int) or not 400 <= status_code <= 599:
                    status_code = 409
                raise DetectorDevelopmentError(
                    code,
                    _safe_review_proxy_failure_message(code),
                    status_code=status_code,
                )
            raise DetectorDevelopmentError(
                "review_proxy_worker_died", "Review proxy worker exited without a valid result"
            )
        finally:
            with self._child_lock:
                if self._child is process:
                    self._child = None
                    self._child_job_handle = None
            _close_windows_handle(job_handle)

    def _default_worker_command(self, control: Path, staging: Path, parent_pid: int) -> list[str]:
        return [
            sys.executable,
            "-m",
            "football_tracking.detector_review_proxy_worker",
            "--control-dir",
            str(control),
            "--staging-dir",
            str(staging),
            "--parent-pid",
            str(parent_pid),
        ]

    def _start_dispatcher(self) -> None:
        if self._dispatcher is not None:
            return
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop,
            name="detector-review-proxy-dispatcher",
            daemon=True,
        )
        self._dispatcher.start()

    def _dispatch_loop(self) -> None:
        while not self._shutdown_event.is_set():
            self._dispatch_event.wait(0.25)
            self._dispatch_event.clear()
            if self._shutdown_event.is_set():
                return
            try:
                self._begin_execution_lifetime()
            except DetectorDevelopmentError as exc:
                if exc.code == "service_closed":
                    return
                continue
            try:
                try:
                    # The outer lifetime fence intentionally covers both the
                    # nested execution and its trailing disk refresh. close()
                    # must not release the owner lease while this iteration can
                    # still touch coordinator state.
                    self._execute_in_global_slot(None)
                    if self._shutdown_event.is_set():
                        return
                    with self._lock:
                        self._refresh_jobs_from_disk()
                        if any(record.get("status") == "queued" for record in self._jobs.values()):
                            self._dispatch_event.set()
                except DetectorDevelopmentError as exc:
                    if exc.code == "service_closed" or self._shutdown_event.is_set():
                        return
                    # A transient metadata sampling failure must not kill the
                    # only dispatcher. The bounded wait at the top retries it.
                    continue
            finally:
                self._end_execution_lifetime()

    def _record_progress(self, job_id: str, completed: Any, total: Any) -> None:
        if (
            isinstance(completed, bool)
            or not isinstance(completed, int)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or completed < 0
            or total <= 0
            or completed > total
        ):
            raise DetectorDevelopmentError("invalid_review_proxy_progress", "Review proxy worker progress is invalid")
        with self._lock:
            record = self._refresh_job_from_disk(job_id)
            self._require_current_owner(record, status="running")
            expected_total = record["progress"]["total"]
            if total != expected_total:
                raise DetectorDevelopmentError("invalid_review_proxy_progress", "Review proxy progress total changed")
            if completed < record["progress"]["completed"]:
                raise DetectorDevelopmentError("invalid_review_proxy_progress", "Review proxy progress moved backwards")
            now = utc_now_iso()
            record["progress"].update(completed=completed, updated_at=now)
            record["owner_heartbeat_at"] = now
            record["updated_at"] = now
            self._persist_record(record)

    def _set_stage(self, job_id: str, stage: str) -> None:
        with self._lock:
            record = self._refresh_job_from_disk(job_id)
            self._require_current_owner(record, status="running")
            now = utc_now_iso()
            record["stage"] = stage
            record["owner_heartbeat_at"] = now
            record["updated_at"] = now
            self._persist_record(record)

    def _mark_cancelled(self, record: dict[str, Any]) -> None:
        record.update(
            status="cancelled",
            stage="cancelled",
            owner_id=None,
            owner_generation=None,
            owner_heartbeat_at=None,
            cancel_requested=True,
            error_code="cancelled",
            recovery_action="retry",
            updated_at=utc_now_iso(),
        )
        self._persist_record(record)
        self._persist_cancel_token(record)

    def _cancellation_requested(self, job_id: str) -> bool:
        token = _read_optional_json(
            self._cancel_root / f"{job_id}.json",
            self._cancel_root,
            "review proxy cancellation token",
        )
        if token is None:
            with self._lock:
                record = self._refresh_job_from_disk(job_id)
                self._persist_cancel_token(record)
                return bool(record["cancel_requested"])
        if (
            token.get("artifact_type") != "detector_review_proxy_cancel_token"
            or token.get("job_id") != job_id
            or not isinstance(token.get("cancel_requested"), bool)
        ):
            raise DetectorDevelopmentError("invalid_cancel_token", "Review proxy cancellation token is invalid")
        return token["cancel_requested"]

    def _write_worker_cancel(self, control: Path, worker_id: str, requested: bool) -> None:
        atomic_write_json(
            control / "cancel.json",
            {
                "schema_version": "1.0",
                "artifact_type": "detector_review_proxy_worker_cancel",
                "worker_id": worker_id,
                "cancel_requested": requested,
            },
            trusted_root=control,
        )

    def _persist_record(self, record: dict[str, Any]) -> None:
        self._lock.validate()
        path = self._jobs_root / f"{record['job_id']}.json"
        expected_generation = _record_generation(record)
        try:
            path.lstat()
        except FileNotFoundError:
            if expected_generation != 0:
                raise DetectorDevelopmentError(
                    "review_proxy_ownership_lost",
                    "Review proxy record disappeared before its generation update",
                )
        else:
            content, _ = read_regular_bytes(
                path,
                "review proxy job",
                max_bytes=_MAX_JOB_DOCUMENT_BYTES,
                trusted_root=self._jobs_root,
            )
            persisted = json_object_from_bytes(content, "review proxy job")
            if persisted.get("job_id") != record.get("job_id") or _record_generation(persisted) != expected_generation:
                raise DetectorDevelopmentError(
                    "review_proxy_ownership_lost",
                    "Review proxy record generation changed before persistence",
                )
        next_record = deepcopy(record)
        next_record["record_generation"] = expected_generation + 1
        atomic_write_json(
            path,
            next_record,
            trusted_root=self._jobs_root,
        )
        record["record_generation"] = expected_generation + 1
        self._jobs[record["job_id"]] = record

    def _persist_cancel_token(self, record: dict[str, Any]) -> None:
        self._lock.validate()
        atomic_write_json(
            self._cancel_root / f"{record['job_id']}.json",
            {
                "schema_version": "1.0",
                "artifact_type": "detector_review_proxy_cancel_token",
                "job_id": record["job_id"],
                "cancel_requested": bool(record["cancel_requested"]),
            },
            trusted_root=self._cancel_root,
        )

    def _acquire_owner_lease(self):
        held = self._owner_lease_object.acquire(blocking=False)
        if held is None:
            raise DetectorDevelopmentError(
                "review_proxy_ownership_lost",
                "Review proxy service owner lease could not be acquired",
            )
        return held

    def _release_owner_lease(self) -> None:
        handle = getattr(self, "_owner_lease", None)
        if handle is None:
            return
        self._owner_lease = None
        handle.release()
        _remove_verified_lease_artifact(self._owner_lease_object)

    def _acquire_execution_lease(self):
        while True:
            if self._closed:
                raise DetectorDevelopmentError("service_closed", "Detector review proxy service is closed")
            held = self._execution_lease.acquire(blocking=False)
            if held is not None:
                return held
            self._shutdown_event.wait(0.05)

    def _try_acquire_execution_lease(self) -> _HeldLease | None:
        return self._execution_lease.acquire(blocking=False)

    def _acquire_job_lease(self, job_id: str) -> tuple[_HeldLease, tuple[int, int]]:
        lease = self._job_lease(job_id, create=True)
        if lease is None:
            raise DetectorDevelopmentError("review_proxy_ownership_lost", "Review proxy job lease is unavailable")
        held = lease.acquire(blocking=False)
        if held is None:
            raise DetectorDevelopmentError(
                "review_proxy_ownership_lost",
                "Review proxy job lease is owned by another process",
            )
        return held, lease.identity

    def _coordination_bindings(
        self,
        *,
        job_identity: tuple[int, int] | None = None,
        owner_identity: tuple[int, int] | None = None,
    ) -> dict[str, list[int]]:
        bindings = {
            "registry": list(self._lock.identity),
            "execution": list(self._execution_lease.identity),
        }
        if job_identity is not None:
            bindings["job"] = list(job_identity)
        if owner_identity is not None:
            bindings["owner"] = list(owner_identity)
        return bindings

    def _root_coordination_matches(self, record: dict[str, Any]) -> bool:
        bindings = record.get("coordination_bindings")
        if bindings is None:
            return True
        if not isinstance(bindings, dict):
            return False
        return (
            _lease_identity_value(bindings.get("registry")) == self._lock.identity
            and _lease_identity_value(bindings.get("execution")) == self._execution_lease.identity
        )

    def _job_lease(self, job_id: str, *, create: bool) -> _HardenedLease | None:
        job_id = require_safe_id(job_id, "review proxy job_id")
        lease_dir = self._job_leases_root / job_id
        if create:
            lease_dir = secure_mkdirs(self._job_leases_root, job_id)
        else:
            try:
                metadata = lease_dir.lstat()
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise DetectorDevelopmentError(
                    "unsafe_result_tree",
                    "Review proxy job lease is unavailable",
                ) from exc
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or bool(getattr(metadata, "st_file_attributes", 0) & 0x00000400)
            ):
                raise DetectorDevelopmentError("unsafe_result_tree", "Review proxy job lease is unsafe")
        return _HardenedLease(lease_dir, trusted_root=self._leases_root, label="job")

    def _probe_owner_lease(
        self,
        record: dict[str, Any],
    ) -> tuple[bool | None, _HeldLease | None, _HardenedLease | None]:
        # The death-released OS byte lock is the liveness authority. Persisted
        # heartbeat timestamps are audit evidence only and never justify a
        # clock-based takeover of a process that still owns its lease.
        owner_id = record.get("owner_id")
        owner_generation = record.get("owner_generation")
        bindings = record.get("coordination_bindings")
        expected_owner_identity = _lease_identity_value(bindings.get("owner")) if isinstance(bindings, dict) else None
        if owner_id == self._owner_id and owner_generation == self._owner_generation:
            if expected_owner_identity is not None and expected_owner_identity != self._owner_lease_object.identity:
                return None, None, self._owner_lease_object
            if self._owner_lease is None:
                return False, None, self._owner_lease_object
            try:
                self._owner_lease.validate()
            except DetectorDevelopmentError:
                return None, None, self._owner_lease_object
            return True, None, self._owner_lease_object
        try:
            owner_id = require_safe_id(owner_id, "review proxy owner_id")
            owner_generation = require_safe_id(owner_generation, "review proxy owner generation")
        except DetectorDevelopmentError:
            return None, None, None
        key = hashlib.sha256(f"{owner_id}\0{owner_generation}".encode()).hexdigest()[:32]
        lease_dir = self._owner_leases_root / f"owner-{key}"
        try:
            metadata = lease_dir.lstat()
        except FileNotFoundError:
            return False, None, None
        except OSError:
            return None, None, None
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or bool(getattr(metadata, "st_file_attributes", 0) & 0x00000400)
            or (
                expected_owner_identity is not None
                and (int(metadata.st_dev), int(metadata.st_ino)) != expected_owner_identity
            )
        ):
            return None, None, None
        try:
            lease = _HardenedLease(lease_dir, trusted_root=self._leases_root, label="owner")
            held = lease.acquire(blocking=False)
        except DetectorDevelopmentError:
            return None, None, None
        if held is None:
            return True, None, lease
        return False, held, lease

    def _require_current_owner(self, record: dict[str, Any], *, status: str) -> None:
        if (
            record.get("status") != status
            or record.get("owner_id") != self._owner_id
            or record.get("owner_generation") != self._owner_generation
        ):
            raise DetectorDevelopmentError("review_proxy_ownership_lost", "Review proxy job lost ownership")

    def _load_and_recover_jobs(self) -> None:
        with self._lock:
            self._refresh_jobs_from_disk()
        with self._execution_lock:
            execution_lease = self._try_acquire_execution_lease()
            if execution_lease is None:
                return
            try:
                execution_lease.validate()
                self._recover_orphaned_active_jobs(execution_lease)
                self._cleanup_stale_owner_leases(execution_lease)
                with self._lock:
                    self._refresh_jobs_from_disk()
                    ready_records = [
                        deepcopy(record) for record in self._jobs.values() if record.get("status") == "ready"
                    ]
                for snapshot in ready_records:
                    if not self._root_coordination_matches(snapshot):
                        continue
                    try:
                        report, digest = self._validate_result_tree(
                            self._results_root / snapshot["job_id"],
                            snapshot,
                        )
                        if digest != snapshot.get("result_manifest_sha256"):
                            raise DetectorDevelopmentError(
                                "review_proxy_manifest_mismatch",
                                "Review proxy result changed",
                            )
                    except Exception:
                        with self._lock:
                            execution_lease.validate()
                            current = self._refresh_job_from_disk(snapshot["job_id"])
                            if (
                                current.get("status") != "ready"
                                or current.get("result_manifest_sha256") != snapshot.get("result_manifest_sha256")
                                or _record_generation(current) != _record_generation(snapshot)
                            ):
                                continue
                            current.update(
                                status="blocked",
                                stage="blocked",
                                owner_id=None,
                                owner_generation=None,
                                owner_heartbeat_at=None,
                                error_code=None,
                                error_message="Review proxy published result could not be verified",
                                recovery_action="retry",
                                report=None,
                                result_manifest_sha256=None,
                                updated_at=utc_now_iso(),
                            )
                            self._persist_record(current)
                    else:
                        with self._lock:
                            execution_lease.validate()
                            current = self._refresh_job_from_disk(snapshot["job_id"])
                            if (
                                current.get("status") == "ready"
                                and current.get("result_manifest_sha256") == digest
                                and _record_generation(current) == _record_generation(snapshot)
                            ):
                                current["report"] = report
                                self._jobs[snapshot["job_id"]] = current
            finally:
                execution_lease.release()

    def _cleanup_stale_owner_leases(self, execution_lease: _HeldLease) -> None:
        """Bounded cleanup of dead owner generations not referenced by any job."""

        execution_lease.validate()
        with self._lock:
            self._lock.validate()
            execution_lease.validate()
            self._owner_lease.validate()
            self._refresh_jobs_from_disk()
            referenced: set[str] = set()
            for record in self._jobs.values():
                try:
                    owner_id = require_safe_id(record.get("owner_id"), "review proxy owner_id")
                    generation = require_safe_id(
                        record.get("owner_generation"),
                        "review proxy owner generation",
                    )
                except DetectorDevelopmentError:
                    continue
                key = hashlib.sha256(f"{owner_id}\0{generation}".encode()).hexdigest()[:32]
                referenced.add(f"owner-{key}")
            try:
                candidates = sorted(self._owner_leases_root.iterdir(), key=lambda path: path.name)
            except OSError:
                return
            inspected = 0
            for path in candidates:
                if inspected >= _MAX_STALE_OWNER_SWEEP:
                    break
                if not re.fullmatch(r"owner-[0-9a-f]{32}", path.name):
                    continue
                inspected += 1
                if path == self._owner_lease_dir or path.name in referenced:
                    continue
                try:
                    metadata = path.lstat()
                except OSError:
                    continue
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or stat.S_ISLNK(metadata.st_mode)
                    or bool(getattr(metadata, "st_file_attributes", 0) & 0x00000400)
                ):
                    continue
                try:
                    lease = _HardenedLease(path, trusted_root=self._leases_root, label="owner")
                except DetectorDevelopmentError:
                    continue
                _remove_verified_lease_artifact(lease)

    def _recover_orphaned_active_jobs(
        self,
        execution_lease: _HeldLease,
        *,
        refresh: bool = True,
    ) -> None:
        execution_lease.validate()
        with self._lock:
            execution_lease.validate()
            if refresh:
                self._refresh_jobs_from_disk()
            candidates = [
                deepcopy(record) for record in self._jobs.values() if record.get("status") in {"running", "committing"}
            ]
        for snapshot in candidates:
            self._recover_orphaned_job(snapshot, execution_lease)

    def _recover_orphaned_job(self, snapshot: dict[str, Any], execution_lease: _HeldLease) -> None:
        execution_lease.validate()
        if not self._root_coordination_matches(snapshot):
            return
        job_id = require_safe_id(snapshot.get("job_id"), "review proxy job_id")
        owner_state, dead_owner_held, owner_lease = self._probe_owner_lease(snapshot)
        if owner_state is not False:
            return
        bindings = snapshot.get("coordination_bindings")
        expected_job_identity = _lease_identity_value(bindings.get("job")) if isinstance(bindings, dict) else None
        try:
            job_lease = self._job_lease(job_id, create=expected_job_identity is None)
        except BaseException:
            if dead_owner_held is not None:
                dead_owner_held.release()
            raise
        if job_lease is None:
            if dead_owner_held is not None:
                dead_owner_held.release()
            return
        if expected_job_identity is not None and job_lease.identity != expected_job_identity:
            if dead_owner_held is not None:
                dead_owner_held.release()
            return
        try:
            held_job = job_lease.acquire(blocking=False)
        except DetectorDevelopmentError:
            if dead_owner_held is not None:
                dead_owner_held.release()
            return
        except BaseException:
            if dead_owner_held is not None:
                dead_owner_held.release()
            raise
        if held_job is None:
            if dead_owner_held is not None:
                dead_owner_held.release()
            return
        recovered = False
        try:
            status = snapshot.get("status")
            expected_owner_id = snapshot.get("owner_id")
            expected_owner_generation = snapshot.get("owner_generation")
            expected_record_generation = _record_generation(snapshot)
            expected_commit = snapshot.get("commit_manifest_sha256")
            destination = self._results_root / job_id
            with self._lock:
                self._lock.validate()
                execution_lease.validate()
                held_job.validate()
                self._owner_lease.validate()
                if dead_owner_held is not None:
                    dead_owner_held.validate()
                current = self._refresh_job_from_disk(job_id)
                if (
                    current.get("status") != status
                    or current.get("owner_id") != expected_owner_id
                    or current.get("owner_generation") != expected_owner_generation
                    or current.get("commit_manifest_sha256") != expected_commit
                    or _record_generation(current) != expected_record_generation
                ):
                    return
                self._coordination_failpoint("before_recovery_cleanup", job_id)
                self._lock.validate()
                execution_lease.validate()
                held_job.validate()
                self._owner_lease.validate()
                if dead_owner_held is not None:
                    dead_owner_held.validate()
                current = self._refresh_job_from_disk(job_id)
                if (
                    current.get("status") != status
                    or current.get("owner_id") != expected_owner_id
                    or current.get("owner_generation") != expected_owner_generation
                    or current.get("commit_manifest_sha256") != expected_commit
                    or _record_generation(current) != expected_record_generation
                ):
                    return
                now = utc_now_iso()
                if status == "committing" and _directory_entry_exists(destination):
                    committed: tuple[dict[str, Any], str] | None = None
                    try:
                        report, digest = self._validate_result_tree(destination, current)
                        if digest == current.get("commit_manifest_sha256"):
                            committed = (report, digest)
                    except Exception:
                        committed = None
                    self._lock.validate()
                    execution_lease.validate()
                    held_job.validate()
                    self._owner_lease.validate()
                    if dead_owner_held is not None:
                        dead_owner_held.validate()
                    current = self._refresh_job_from_disk(job_id)
                    if (
                        current.get("status") != status
                        or current.get("owner_id") != expected_owner_id
                        or current.get("owner_generation") != expected_owner_generation
                        or current.get("commit_manifest_sha256") != expected_commit
                        or _record_generation(current) != expected_record_generation
                    ):
                        return
                    if not _safe_remove_matching_staging(job_id, self._results_root):
                        return
                    if committed is None:
                        if not _safe_remove_tree(destination, self._results_root):
                            return
                    self._lock.validate()
                    execution_lease.validate()
                    held_job.validate()
                    self._owner_lease.validate()
                    if dead_owner_held is not None:
                        dead_owner_held.validate()
                    current = self._refresh_job_from_disk(job_id)
                    if (
                        current.get("status") != status
                        or current.get("owner_id") != expected_owner_id
                        or current.get("owner_generation") != expected_owner_generation
                        or current.get("commit_manifest_sha256") != expected_commit
                        or _record_generation(current) != expected_record_generation
                    ):
                        return
                    if committed is not None:
                        report, digest = committed
                        self._apply_ready_record(current, report, digest)
                    else:
                        current.update(
                            status="blocked",
                            stage="blocked",
                            owner_id=None,
                            owner_generation=None,
                            owner_heartbeat_at=None,
                            cancel_requested=False,
                            error_code="invalid_published_commit",
                            error_message="Published review proxy commit could not be verified after restart",
                            recovery_action="retry",
                            report=None,
                            result_manifest_sha256=None,
                            commit_manifest_sha256=None,
                            updated_at=now,
                        )
                        current["progress"]["updated_at"] = now
                    self._persist_record(current)
                    self._persist_cancel_token(current)
                    recovered = True
                    return
                if not _safe_remove_matching_staging(job_id, self._results_root):
                    return
                unexpected_destination = _directory_entry_exists(destination)
                if unexpected_destination:
                    if not _safe_remove_tree(destination, self._results_root):
                        return
                self._lock.validate()
                execution_lease.validate()
                held_job.validate()
                self._owner_lease.validate()
                if dead_owner_held is not None:
                    dead_owner_held.validate()
                current = self._refresh_job_from_disk(job_id)
                if (
                    current.get("status") != status
                    or current.get("owner_id") != expected_owner_id
                    or current.get("owner_generation") != expected_owner_generation
                    or current.get("commit_manifest_sha256") != expected_commit
                    or _record_generation(current) != expected_record_generation
                ):
                    return
                if _directory_entry_exists(destination):
                    return
                if unexpected_destination:
                    current.update(
                        status="blocked",
                        stage="blocked",
                        owner_id=None,
                        owner_generation=None,
                        owner_heartbeat_at=None,
                        error_code="unexpected_published_result",
                        error_message="Unexpected published review proxy result exists after restart",
                        recovery_action="retry",
                        updated_at=now,
                    )
                    current["progress"]["updated_at"] = now
                elif current.get("cancel_requested") is True:
                    self._mark_cancelled(current)
                    recovered = True
                    return
                else:
                    current.update(
                        status="queued",
                        stage="recovered_after_restart",
                        owner_id=None,
                        owner_generation=None,
                        owner_heartbeat_at=None,
                        cancel_requested=False,
                        error_code=None,
                        error_message=None,
                        recovery_action=None,
                        report=None,
                        result_manifest_sha256=None,
                        commit_manifest_sha256=None,
                        updated_at=now,
                    )
                    current["progress"].update(completed=0, updated_at=now)
                self._persist_record(current)
                self._persist_cancel_token(current)
                recovered = True
                return
        finally:
            held_job.release()
            if dead_owner_held is not None:
                dead_owner_held.release()
            if recovered and owner_lease is not None:
                execution_lease.validate()
                _remove_verified_lease_artifact(owner_lease)

    def _refresh_jobs_from_disk(self) -> None:
        for path in self._jobs_root.glob("*.json"):
            self._refresh_job_path_from_disk(path)

    def _refresh_job_from_disk(self, job_id: str) -> dict[str, Any]:
        job_id = require_safe_id(job_id, "review proxy job_id")
        path = self._jobs_root / f"{job_id}.json"
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            raise DetectorDevelopmentError(
                "review_proxy_not_found",
                "Detector review proxy job was not found",
                status_code=404,
            )
        except OSError as exc:
            raise DetectorDevelopmentError(
                "unsafe_result_tree",
                "Detector review proxy job is unavailable",
            ) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or bool(getattr(metadata, "st_file_attributes", 0) & 0x00000400)
        ):
            raise DetectorDevelopmentError("unsafe_result_tree", "Detector review proxy job is unsafe")
        return self._refresh_job_path_from_disk(path)

    def _refresh_job_path_from_disk(self, path: Path) -> dict[str, Any]:
        content, _ = read_regular_bytes(
            path,
            "review proxy job",
            max_bytes=_MAX_JOB_DOCUMENT_BYTES,
            trusted_root=self._jobs_root,
        )
        record = json_object_from_bytes(content, "review proxy job")
        job_id = require_safe_id(record.get("job_id"), "review proxy job_id")
        if path.name != f"{job_id}.json" or record.get("artifact_type") != "detector_review_proxy_job":
            raise DetectorDevelopmentError("invalid_review_proxy_job", "Persisted review proxy job is invalid")
        record.setdefault("owner_generation", None)
        record.setdefault("owner_heartbeat_at", None)
        record.setdefault("record_generation", 0)
        _record_generation(record)
        self._jobs[job_id] = record
        return record

    def _record(self, job_id: str) -> dict[str, Any]:
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise DetectorDevelopmentError(
                "review_proxy_not_found", "Detector review proxy job was not found", status_code=404
            ) from exc

    @staticmethod
    def _public_record(record: dict[str, Any]) -> dict[str, Any]:
        public = deepcopy(record)
        repair_id = public.pop("job_id")
        public["repair_id"] = repair_id
        public.pop("owner_id", None)
        public.pop("owner_generation", None)
        public.pop("owner_heartbeat_at", None)
        public.pop("record_generation", None)
        public.pop("coordination_bindings", None)
        public.pop("cancel_requested", None)
        public.pop("commit_manifest_sha256", None)
        public["can_cancel"] = public.get("status") in {"queued", "running"}
        public["status_url"] = f"/api/v1/detector-review-proxy-repairs/{repair_id}"
        public["cancel_url"] = f"{public['status_url']}/cancel"
        return public


def run_detector_review_proxy(
    request: dict[str, Any],
    staging: Path,
    should_cancel: Callable[[], bool],
    progress: Callable[[int, int], None],
) -> dict[str, Any]:
    """Generate and independently verify one fixed server-owned H.264 proxy."""

    try:
        source = Path(str(request["_source_path"])).resolve(strict=True)
    except OSError as exc:
        raise DetectorDevelopmentError(
            "source_changed",
            _safe_review_proxy_failure_message("source_changed"),
        ) from exc
    try:
        staging = Path(staging).resolve(strict=True)
    except OSError as exc:
        raise DetectorDevelopmentError(
            "path_unavailable",
            _safe_review_proxy_failure_message("path_unavailable"),
        ) from exc
    expected_identity = tuple(int(value) for value in request["_source_change_identity"])
    expected_sha = require_sha256(request["source_sha256"], "review proxy source digest")
    expected_size = _positive_int(request["source_size_bytes"], "review proxy source size")
    expected_count = _positive_int(request["source_frame_count"], "review proxy source frame count")
    fps = _positive_finite(request["source_fps"], "review proxy source FPS")
    indices = list(request["sampled_frame_indices"])
    output_limit = _positive_int(request["_output_hard_limit_bytes"], "review proxy output hard limit")
    total = expected_count * 3 + len(indices)
    _raise_if_cancelled(should_cancel)
    source_trusted_root = Path(str(request.get("_source_trusted_root", source.parent))).resolve(strict=True)
    before = regular_file_change_identity(source, "review proxy source")
    if before != expected_identity:
        raise DetectorDevelopmentError("source_changed", "Review proxy source identity changed")
    source_snapshot = staging / f".verified-source{source.suffix}"
    before_sha, before_size, snapshot_handle = _hash_file_cancellable(
        source,
        should_cancel,
        expected_identity=expected_identity,
        expected_size=expected_size,
        max_bytes=expected_size,
        trusted_root=source_trusted_root,
        copy_to=source_snapshot,
        copy_trusted_root=staging,
    )
    try:
        if before_sha != expected_sha or before_size != expected_size:
            raise DetectorDevelopmentError(
                "source_digest_or_size_mismatch",
                "Review proxy source digest or size changed",
            )
        if snapshot_handle is None:
            raise DetectorDevelopmentError(
                "source_snapshot_missing",
                "Review proxy source snapshot handle is unavailable",
            )
        ffmpeg_path, ffmpeg_binding = _bundled_ffmpeg_binding()
        repair_execution_binding = _repair_execution_binding(ffmpeg_binding=ffmpeg_binding)
        if repair_execution_binding != request.get("repair_execution_binding"):
            raise DetectorDevelopmentError(
                "repair_execution_binding_changed",
                "Review proxy repair execution binding changed before generation",
            )
        proxy = staging / "review_proxy.mp4"
        fps_text = _format_fps(fps)
        video_filter = f"scale={PROXY_WIDTH}:{PROXY_HEIGHT}:flags=lanczos,setsar=1,setpts=N/({fps_text}*TB)"
        encode_command = [
            ffmpeg_path,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            "-nostats",
            "-i",
            "fd:",
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            video_filter,
            "-r",
            fps_text,
            "-fps_mode",
            "cfr",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-tag:v",
            "avc1",
            "-fs",
            str(output_limit),
            "-y",
            str(proxy),
        ]
        encoded_count = _run_ffmpeg(
            encode_command,
            should_cancel,
            stdin_handle=snapshot_handle,
            output_path=proxy,
            output_hard_limit_bytes=output_limit,
            on_frame=lambda count: progress(min(count, expected_count), total),
            require_no_frame_sync_changes=True,
        )
    finally:
        primary_exception_active = sys.exc_info()[0] is not None
        close_failed = False
        if snapshot_handle is not None:
            try:
                snapshot_handle.close()
            except (OSError, ValueError):
                close_failed = True
        try:
            source_snapshot.unlink(missing_ok=True)
        except OSError:
            pass
        if close_failed and not primary_exception_active:
            raise DetectorDevelopmentError(
                "source_snapshot_close_failed",
                "Review proxy source snapshot handle could not be closed",
            )
    if encoded_count != expected_count:
        raise DetectorDevelopmentError(
            "review_proxy_frame_count_mismatch",
            "Review proxy encode did not preserve one output frame per source frame",
        )
    metadata = _video_metadata(proxy)
    if (
        metadata["codec"] != "h264"
        or tuple(metadata["size"]) != (PROXY_WIDTH, PROXY_HEIGHT)
        or not math.isclose(float(metadata["fps"]), fps, rel_tol=0.0, abs_tol=1e-3)
        or not str(metadata.get("pix_fmt", "")).startswith("yuv420p")
        or int(metadata.get("rotate", 0)) != 0
    ):
        raise DetectorDevelopmentError(
            "invalid_review_proxy_media", "Review proxy codec, dimensions, or FPS is invalid"
        )
    verified_count = _run_ffmpeg(
        [
            ffmpeg_path,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            "-nostats",
            "-i",
            str(proxy),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            "null",
            "-f",
            "null",
            "-",
        ],
        should_cancel,
        on_frame=lambda count: progress(expected_count + min(count, expected_count), total),
    )
    if verified_count != expected_count:
        raise DetectorDevelopmentError(
            "review_proxy_decode_shortfall", "Review proxy full decode frame count is invalid"
        )
    sample_root = secure_mkdirs(staging, "sampled_frames")
    selection = "+".join(f"eq(n\\,{index})" for index in indices)
    sample_pattern = sample_root / "sample_%03d.jpg"
    _run_ffmpeg(
        [
            ffmpeg_path,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            str(proxy),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            f"select={selection}",
            "-fps_mode",
            "passthrough",
            "-q:v",
            "2",
            "-y",
            str(sample_pattern),
        ],
        should_cancel,
    )
    generated = sorted(sample_root.glob("sample_*.jpg"))
    if len(generated) != len(indices):
        raise DetectorDevelopmentError("review_proxy_sample_shortfall", "Review proxy sample extraction was incomplete")
    sampled_frames: list[dict[str, Any]] = []
    for completed, (index, generated_path) in enumerate(zip(indices, generated), start=1):
        destination = sample_root / f"frame_{index:010d}.jpg"
        os.replace(generated_path, destination)
        image = cv2.imread(str(destination), cv2.IMREAD_COLOR)
        if image is None or image.shape[:2] != (PROXY_HEIGHT, PROXY_WIDTH):
            raise DetectorDevelopmentError("invalid_review_proxy_sample", "Review proxy sample image is invalid")
        media_integrity = _reviewable_image_integrity(destination)
        digest, size = hash_regular_file(
            destination,
            "review proxy sample",
            max_bytes=32 * 1024 * 1024,
            trusted_root=staging,
        )
        sampled_frames.append(
            {
                "frame_index": index,
                "relative_path": destination.relative_to(staging).as_posix(),
                "sha256": digest,
                "size_bytes": size,
                "width": PROXY_WIDTH,
                "height": PROXY_HEIGHT,
                "proxy_time_seconds": index / fps,
                "media_integrity": media_integrity,
            }
        )
        progress(expected_count * 2 + completed, total)
    proxy_sha, proxy_size = hash_regular_file(
        proxy,
        "review proxy media",
        max_bytes=output_limit,
        trusted_root=staging,
    )
    _raise_if_cancelled(should_cancel)
    after = regular_file_change_identity(source, "review proxy source")
    if after != expected_identity:
        raise DetectorDevelopmentError("source_changed", "Review proxy source changed during generation")
    return {
        "schema_version": "1.0",
        "artifact_type": "detector_review_proxy_runner_output",
        "source_sha256": expected_sha,
        "source_size_bytes": expected_size,
        "source_change_identity_before": list(before),
        "source_change_identity_after": list(after),
        "source_width": int(request["source_width"]),
        "source_height": int(request["source_height"]),
        "source_frame_count": expected_count,
        "source_fps": fps,
        "proxy_relative_path": proxy.relative_to(staging).as_posix(),
        "proxy_sha256": proxy_sha,
        "proxy_size_bytes": proxy_size,
        "proxy_width": PROXY_WIDTH,
        "proxy_height": PROXY_HEIGHT,
        "proxy_frame_count": verified_count,
        "proxy_fps": fps,
        "proxy_stream_fps": float(metadata["fps"]),
        "proxy_average_fps": float(metadata["fps"]),
        "encoding": {
            "codec": "libx264",
            "pixel_format": "yuv420p",
            "sample_aspect_ratio": "1/1",
            "preset": "medium",
            "crf": 20,
            "video_filter": video_filter,
            "frame_sync": "one_output_per_decoded_source_frame",
            "movflags": "+faststart",
            "codec_tag": "avc1",
            "timing_residual_tolerance_msec": _CFR_TIMING_TOLERANCE_MSEC,
        },
        "ffmpeg": ffmpeg_binding,
        "repair_execution_binding": repair_execution_binding,
        "sampled_frames": sampled_frames,
    }


def _bundled_ffmpeg_binding() -> tuple[str, dict[str, Any]]:
    try:
        import imageio_ffmpeg

        executable = Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve(strict=True)
    except (ImportError, OSError) as exc:
        raise DetectorDevelopmentError("ffmpeg_unavailable", "Bundled imageio-ffmpeg is unavailable") from exc
    digest, size = hash_regular_file(executable, "bundled ffmpeg executable")
    try:
        completed = subprocess.run(
            [str(executable), "-hide_banner", "-version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DetectorDevelopmentError("ffmpeg_unavailable", "Bundled ffmpeg identity probe failed") from exc
    first_line = completed.stdout.splitlines()[0] if completed.stdout else ""
    if completed.returncode != 0 or not first_line.startswith("ffmpeg version "):
        raise DetectorDevelopmentError("ffmpeg_unavailable", "Bundled ffmpeg version is invalid")
    encoders = subprocess.run(
        [str(executable), "-hide_banner", "-encoders"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10.0,
        check=False,
    )
    if encoders.returncode != 0 or "libx264" not in encoders.stdout:
        raise DetectorDevelopmentError("libx264_unavailable", "Bundled ffmpeg does not provide libx264")
    return str(executable), {
        "path": str(executable),
        "path_kind": "bundled_imageio_ffmpeg",
        "sha256": digest,
        "size_bytes": size,
        "version": first_line,
        "libx264_available": True,
    }


def _repair_execution_binding(*, ffmpeg_binding: dict[str, Any] | None = None) -> dict[str, Any]:
    package_root = Path(__file__).resolve().parent
    names = (
        "detector_development_common.py",
        "detector_probe_worker.py",
        "detector_review_proxy.py",
        "detector_review_proxy_worker.py",
        "media_integrity.py",
    )
    code_files: dict[str, str] = {}
    for name in names:
        path = package_root / name
        _content, digest = read_regular_bytes(
            path,
            "review proxy repair code bundle file",
            max_bytes=4 * 1024 * 1024,
            trusted_root=package_root,
        )
        code_files[f"football_tracking/{name}"] = digest
    if ffmpeg_binding is None:
        _path, ffmpeg_binding = _bundled_ffmpeg_binding()
    try:
        import imageio_ffmpeg

        imageio_ffmpeg_version = str(imageio_ffmpeg.__version__)
    except (ImportError, AttributeError) as exc:
        raise DetectorDevelopmentError("ffmpeg_unavailable", "imageio-ffmpeg runtime identity is unavailable") from exc
    opencv_build = cv2.getBuildInformation()
    runtime = {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "opencv_version": str(cv2.__version__),
        "opencv_build_information_sha256": canonical_sha256({"build_information": opencv_build}),
        "imageio_ffmpeg_version": imageio_ffmpeg_version,
    }
    encoder_preset = {
        "codec": "libx264",
        "preset": "medium",
        "crf": 20,
        "pixel_format": "yuv420p",
        "width": PROXY_WIDTH,
        "height": PROXY_HEIGHT,
        "scale_flags": "lanczos",
        "setpts": "N/(source_fps*TB)",
        "sample_aspect_ratio": "1/1",
        "frame_sync": "one_output_per_decoded_source_frame",
        "movflags": "+faststart",
        "codec_tag": "avc1",
        "timing_residual_tolerance_msec": _CFR_TIMING_TOLERANCE_MSEC,
    }
    decoder_fingerprint = {
        "ffmpeg_sha256": ffmpeg_binding["sha256"],
        "ffmpeg_version": ffmpeg_binding["version"],
        "opencv_version": runtime["opencv_version"],
        "opencv_build_information_sha256": runtime["opencv_build_information_sha256"],
    }
    binding = {
        "schema_version": "1.0",
        "artifact_type": "detector_review_proxy_repair_execution_binding",
        "code_files": code_files,
        "code_bundle_sha256": canonical_sha256(code_files),
        "runtime": runtime,
        "runtime_sha256": canonical_sha256(runtime),
        "ffmpeg": deepcopy(ffmpeg_binding),
        "encoder_preset": encoder_preset,
        "encoder_preset_sha256": canonical_sha256(encoder_preset),
        "decoder_fingerprint": decoder_fingerprint,
        "decoder_fingerprint_sha256": canonical_sha256(decoder_fingerprint),
    }
    binding["binding_sha256"] = canonical_sha256(binding)
    return binding


def _verify_staged_media(
    output: dict[str, Any],
    staging: Path,
    frozen: dict[str, Any],
    should_cancel: Callable[[], bool],
    progress: Callable[[int, int], None],
) -> dict[str, Any]:
    """Independently reopen every staged medium before the coordinator commits it."""

    _raise_if_cancelled(should_cancel)
    proxy = _trusted_result_file(staging, output.get("proxy_relative_path"))
    expected_count = int(frozen["source_frame_count"])
    expected_fps = float(frozen["source_fps"])
    expected_indices = list(frozen["sampled_frame_indices"])
    total = expected_count * 3 + len(expected_indices)
    verification_base = expected_count * 2 + len(expected_indices)
    ffmpeg_path, current_ffmpeg = _bundled_ffmpeg_binding()
    frozen_ffmpeg = frozen["repair_execution_binding"]["ffmpeg"]
    if current_ffmpeg != frozen_ffmpeg:
        raise DetectorDevelopmentError(
            "repair_execution_binding_changed",
            "Bundled ffmpeg changed before independent verification",
        )
    _verify_single_video_stream(ffmpeg_path, proxy, expected_fps, should_cancel)
    next_index = 0
    timing_error: str | None = None
    tolerance_seconds = _CFR_TIMING_TOLERANCE_MSEC / 1000.0
    showinfo_pattern = re.compile(r"\bn:\s*(\d+)\s+pts:\s*-?\d+\s+pts_time:([+-]?(?:\d+(?:\.\d*)?|\.\d+))")

    def observe_timing(line: str) -> None:
        nonlocal next_index, timing_error
        match = showinfo_pattern.search(line)
        if match is None:
            return
        observed_index = int(match.group(1))
        observed_time = float(match.group(2))
        if observed_index != next_index:
            timing_error = "Review proxy decoded frame indices are not contiguous"
            return
        expected_time = observed_index / expected_fps
        if abs(observed_time - expected_time) > tolerance_seconds:
            timing_error = "Review proxy CFR presentation timing residual is out of bounds"
            return
        if re.search(r"\bfmt:yuv420p(?:\s|$)", line) is None:
            timing_error = "Review proxy decoded pixel format is not yuv420p"
            return
        if re.search(r"\bsar:1/1(?:\s|$)", line) is None:
            timing_error = "Review proxy sample aspect ratio is not 1:1"
            return
        if re.search(rf"\bs:{PROXY_WIDTH}x{PROXY_HEIGHT}(?:\s|$)", line) is None:
            timing_error = "Review proxy decoded frame dimensions are invalid"
            return
        next_index += 1

    count = _run_ffmpeg(
        [
            ffmpeg_path,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "info",
            "-progress",
            "pipe:1",
            "-nostats",
            "-i",
            str(proxy),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            "showinfo",
            "-f",
            "null",
            "-",
        ],
        should_cancel,
        on_frame=lambda verified: progress(
            min(verification_base + min(verified, expected_count), total - 1),
            total,
        ),
        on_stderr=observe_timing,
    )
    if timing_error is not None:
        raise DetectorDevelopmentError("review_proxy_timing_invalid", timing_error)
    if count != expected_count or next_index != expected_count:
        raise DetectorDevelopmentError(
            "review_proxy_decode_shortfall",
            "Coordinator independent full decode did not cover every proxy frame",
        )
    samples = output.get("sampled_frames")
    if not isinstance(samples, list) or [item.get("frame_index") for item in samples] != expected_indices:
        raise DetectorDevelopmentError("invalid_review_proxy_samples", "Review proxy samples are missing")
    verification_root = secure_mkdirs(staging, f".independent-verify-{uuid.uuid4().hex}")
    verified_sample_integrity: list[dict[str, Any]] = []
    try:
        selection = "+".join(f"eq(n\\,{index})" for index in expected_indices)
        verification_pattern = verification_root / "sample_%03d.jpg"
        _run_ffmpeg(
            [
                ffmpeg_path,
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                "-i",
                str(proxy),
                "-map",
                "0:v:0",
                "-an",
                "-vf",
                f"select={selection}",
                "-fps_mode",
                "passthrough",
                "-q:v",
                "2",
                "-y",
                str(verification_pattern),
            ],
            should_cancel,
        )
        independently_decoded = sorted(verification_root.glob("sample_*.jpg"))
        if len(independently_decoded) != len(samples):
            raise DetectorDevelopmentError(
                "review_proxy_sample_shortfall",
                "Independent exact-frame sample extraction was incomplete",
            )
        for item, expected_path in zip(samples, independently_decoded):
            _raise_if_cancelled(should_cancel)
            image_path = _trusted_result_file(staging, item.get("relative_path"))
            image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
            expected_image = cv2.imread(str(expected_path), cv2.IMREAD_COLOR)
            independent_integrity = _reviewable_frame_integrity(expected_image)
            if (
                image is None
                or expected_image is None
                or image.shape != expected_image.shape
                or image.shape[:2] != (PROXY_HEIGHT, PROXY_WIDTH)
                or not (image == expected_image).all()
            ):
                _reviewable_frame_integrity(image)
                raise DetectorDevelopmentError(
                    "review_proxy_sample_mapping_mismatch",
                    "Coordinator independently rejected a sampled proxy JPEG or mapped proxy JPEG",
                )
            declared_integrity = independent_integrity
            if item.get("media_integrity") != declared_integrity:
                raise DetectorDevelopmentError(
                    "review_proxy_sample_integrity_mismatch",
                    "Review proxy generated sample integrity binding changed",
                )
            verified_sample_integrity.append(
                {
                    "frame_index": item["frame_index"],
                    "generated_sample": declared_integrity,
                    "independent_exact_frame_sample": independent_integrity,
                }
            )
    finally:
        _safe_remove_tree(verification_root, staging)
    progress(total, total)
    return {
        "independent_verification_performed": True,
        "full_proxy_decode_verified": True,
        "frame_count_exact": True,
        "fps_exact": True,
        "dimensions_exact": True,
        "pixel_format_yuv420p_verified": True,
        "sample_aspect_ratio_1_1_verified": True,
        "single_video_stream_without_auxiliary_streams": True,
        "sample_indices_exact": True,
        "sample_pixels_match_exact_proxy_frames": True,
        "sample_media_integrity_verified": True,
        "generated_sample_media_integrity_verified": True,
        "sample_count": len(verified_sample_integrity),
        "sample_media_integrity": verified_sample_integrity,
    }


def _verify_single_video_stream(
    ffmpeg_path: str,
    proxy: Path,
    expected_fps: float,
    should_cancel: Callable[[], bool],
) -> None:
    stream_types: dict[int, str] = {}
    stream_descriptions: dict[int, str] = {}
    reading_input = True
    stream_pattern = re.compile(
        r"^\s*Stream #0:(\d+)(?:\[[^\]]+\])?(?:\([^)]*\))?:\s+"
        r"(Video|Audio|Subtitle|Data|Attachment):"
    )

    def observe_stream(line: str) -> None:
        nonlocal reading_input
        if line.strip() == "Stream mapping:":
            reading_input = False
            return
        if not reading_input:
            return
        match = stream_pattern.search(line)
        if match is not None:
            stream_index = int(match.group(1))
            stream_types[stream_index] = match.group(2)
            stream_descriptions[stream_index] = line

    _run_ffmpeg(
        [
            ffmpeg_path,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "info",
            "-progress",
            "pipe:1",
            "-nostats",
            "-i",
            str(proxy),
            "-t",
            "0",
            "-map",
            "0:v:0",
            "-c",
            "copy",
            "-f",
            "null",
            "-",
        ],
        should_cancel,
        on_stderr=observe_stream,
    )
    if stream_types != {0: "Video"}:
        raise DetectorDevelopmentError(
            "invalid_review_proxy_streams",
            "Review proxy must contain exactly one video stream and no other streams",
        )
    description = stream_descriptions[0]
    fps_match = re.search(r",\s*([0-9]+(?:\.[0-9]+)?)\s+fps(?:,|\s)", description)
    if (
        re.search(r"\bVideo:\s+h264\b", description) is None
        or re.search(r"\byuv420p(?:\([^)]*\))?(?:,|\s)", description) is None
        or re.search(rf"\b{PROXY_WIDTH}x{PROXY_HEIGHT}\b", description) is None
        or "[SAR 1:1 " not in description
        or fps_match is None
        or not math.isclose(float(fps_match.group(1)), expected_fps, rel_tol=0.0, abs_tol=1e-3)
    ):
        raise DetectorDevelopmentError(
            "invalid_review_proxy_media",
            "Review proxy codec, pixel format, dimensions, SAR, or FPS is invalid",
        )


def _run_ffmpeg(
    command: list[str],
    should_cancel: Callable[[], bool],
    *,
    stdin_handle: Any | None = None,
    output_path: Path | None = None,
    output_hard_limit_bytes: int | None = None,
    on_frame: Callable[[int], None] | None = None,
    on_stderr: Callable[[str], None] | None = None,
    require_no_frame_sync_changes: bool = False,
) -> int:
    lines: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=_FFMPEG_DRAIN_QUEUE_SIZE)
    stop_drains = threading.Event()
    output_lock = threading.Lock()
    output_bytes = 0
    drain_error: DetectorDevelopmentError | None = None
    stderr_tail = ""
    popen_options: dict[str, Any] = {
        "stdin": stdin_handle if stdin_handle is not None else subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "close_fds": True,
    }
    if os.name == "nt":
        popen_options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        popen_options["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **popen_options)
    except OSError as exc:
        raise DetectorDevelopmentError("ffmpeg_failed", "Bundled ffmpeg could not start") from exc

    def record_drain_error(error: DetectorDevelopmentError) -> None:
        nonlocal drain_error
        with output_lock:
            if drain_error is None:
                drain_error = error

    def enqueue(name: str, line: str) -> bool:
        while not stop_drains.is_set():
            try:
                lines.put((name, line), timeout=0.05)
                return True
            except queue.Full:
                continue
        return False

    def drain(name: str, stream) -> None:
        nonlocal output_bytes
        try:
            while not stop_drains.is_set():
                raw_line = stream.readline(_FFMPEG_MAX_LINE_BYTES + 1)
                if raw_line in {b"", ""}:
                    return
                if isinstance(raw_line, str):
                    encoded_line = raw_line.encode("utf-8", "replace")
                else:
                    encoded_line = bytes(raw_line)
                if len(encoded_line) > _FFMPEG_MAX_LINE_BYTES:
                    record_drain_error(
                        DetectorDevelopmentError(
                            "ffmpeg_output_limit_exceeded",
                            "Bundled ffmpeg emitted an oversized output line",
                        )
                    )
                    return
                with output_lock:
                    output_bytes += len(encoded_line)
                    aggregate_exceeded = output_bytes > _FFMPEG_MAX_OUTPUT_BYTES
                if aggregate_exceeded:
                    record_drain_error(
                        DetectorDevelopmentError(
                            "ffmpeg_output_limit_exceeded",
                            "Bundled ffmpeg exceeded its aggregate output byte limit",
                        )
                    )
                    return
                line = encoded_line.rstrip(b"\r\n").decode("utf-8", "replace")
                if not enqueue(name, line):
                    return
        except (OSError, ValueError):
            if not stop_drains.is_set():
                record_drain_error(
                    DetectorDevelopmentError(
                        "ffmpeg_failed",
                        "Bundled ffmpeg output could not be read",
                    )
                )

    stdout_thread = threading.Thread(
        target=drain,
        args=("stdout", process.stdout),
        name=f"detector-review-proxy-ffmpeg-drain-stdout-{process.pid}",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=drain,
        args=("stderr", process.stderr),
        name=f"detector-review-proxy-ffmpeg-drain-stderr-{process.pid}",
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    frame_count = 0
    duplicated_frames = 0
    dropped_frames = 0
    progress_end = False
    try:
        while process.poll() is None or stdout_thread.is_alive() or stderr_thread.is_alive() or not lines.empty():
            _raise_if_cancelled(should_cancel)
            with output_lock:
                current_drain_error = drain_error
            if current_drain_error is not None:
                raise current_drain_error
            if output_path is not None and output_hard_limit_bytes is not None:
                try:
                    metadata = output_path.lstat()
                except FileNotFoundError:
                    current_size = 0
                except OSError as exc:
                    raise DetectorDevelopmentError(
                        "path_unavailable",
                        "Review proxy output could not be inspected",
                    ) from exc
                else:
                    if is_link_or_reparse(output_path) or not stat.S_ISREG(metadata.st_mode):
                        raise DetectorDevelopmentError(
                            "unsafe_review_proxy_output",
                            "Review proxy output must remain a regular file",
                        )
                    current_size = int(metadata.st_size)
                if current_size > output_hard_limit_bytes:
                    raise DetectorDevelopmentError(
                        "review_proxy_output_limit_exceeded",
                        "Review proxy exceeded its hard output byte limit",
                    )
            try:
                stream_name, line = lines.get(timeout=0.1)
            except queue.Empty:
                continue
            if stream_name == "stderr":
                stderr_tail = f"{stderr_tail}\n{line}"[-2000:]
                if on_stderr is not None:
                    on_stderr(line)
            elif line.startswith("frame="):
                try:
                    frame_count = max(frame_count, int(line.split("=", 1)[1].strip()))
                except ValueError:
                    pass
                if on_frame is not None:
                    on_frame(frame_count)
            elif line.startswith("dup_frames="):
                try:
                    duplicated_frames = max(duplicated_frames, int(line.split("=", 1)[1].strip()))
                except ValueError:
                    pass
            elif line.startswith("drop_frames="):
                try:
                    dropped_frames = max(dropped_frames, int(line.split("=", 1)[1].strip()))
                except ValueError:
                    pass
            elif line == "progress=end":
                progress_end = True
        with output_lock:
            current_drain_error = drain_error
        if current_drain_error is not None:
            raise current_drain_error
    except BaseException:
        stop_drains.set()
        try:
            _terminate_process_tree(process)
        except Exception:
            pass
        raise
    finally:
        primary_exception_active = sys.exc_info()[0] is not None
        cleanup_failed = False
        stop_drains.set()
        if process.poll() is None:
            try:
                _terminate_process_tree(process)
            except Exception:
                cleanup_failed = True
        if process.stdout is not None:
            try:
                process.stdout.close()
            except (OSError, ValueError):
                cleanup_failed = True
        if process.stderr is not None:
            try:
                process.stderr.close()
            except (OSError, ValueError):
                cleanup_failed = True
        try:
            stdout_thread.join(timeout=_FFMPEG_DRAIN_JOIN_SECONDS)
            stderr_thread.join(timeout=_FFMPEG_DRAIN_JOIN_SECONDS)
        except RuntimeError:
            cleanup_failed = True
        cleanup_failed = cleanup_failed or stdout_thread.is_alive() or stderr_thread.is_alive()
        if cleanup_failed and not primary_exception_active:
            raise DetectorDevelopmentError(
                "ffmpeg_drain_failed",
                "Bundled ffmpeg output drainers did not stop",
            )
    if process.returncode != 0:
        error = DetectorDevelopmentError(
            "ffmpeg_failed",
            "Bundled ffmpeg failed",
        )
        error._internal_diagnostics = {
            "stderr_tail_bytes": len(stderr_tail.encode("utf-8")),
            "stderr_tail_sha256": hashlib.sha256(stderr_tail.encode("utf-8")).hexdigest(),
        }
        raise error
    if "-progress" in command and not progress_end:
        raise DetectorDevelopmentError("ffmpeg_failed", "Bundled ffmpeg did not publish a terminal progress record")
    if require_no_frame_sync_changes and (duplicated_frames != 0 or dropped_frames != 0):
        raise DetectorDevelopmentError(
            "review_proxy_frame_sync_changed",
            "Review proxy CFR synchronization duplicated or dropped decoded source frames",
        )
    return frame_count


def _video_metadata(path: Path) -> dict[str, Any]:
    try:
        import imageio_ffmpeg

        reader = imageio_ffmpeg.read_frames(path, pix_fmt="rgb24")
        try:
            metadata = next(reader)
        finally:
            reader.close()
    except (ImportError, OSError, StopIteration, ValueError) as exc:
        raise DetectorDevelopmentError("invalid_review_proxy_media", "Review proxy metadata is unavailable") from exc
    return metadata


_MEDIA_INTEGRITY_FIELDS = (
    "status",
    "width",
    "height",
    "mean_luma",
    "std_luma",
    "texture_tile_ratio",
    "dominant_color_ratio",
    "gray",
    "low_information",
    "likely_corrupt",
    "reasons",
)


def _normalized_media_integrity(result: dict[str, Any]) -> dict[str, Any]:
    return {key: deepcopy(result.get(key)) for key in _MEDIA_INTEGRITY_FIELDS}


def _require_reviewable_media_integrity(result: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalized_media_integrity(result)
    if normalized["likely_corrupt"] is not False or normalized["status"] != "ok":
        raise DetectorDevelopmentError(
            "invalid_review_proxy_sample",
            "Coordinator independently rejected a corrupt sampled proxy JPEG",
        )
    if normalized["low_information"] is not False or normalized["gray"] is not False:
        raise DetectorDevelopmentError(
            "review_proxy_sample_low_information",
            "Review proxy sampled proxy JPEG is gray or low-information",
        )
    return normalized


def _reviewable_image_integrity(path: Path) -> dict[str, Any]:
    return _require_reviewable_media_integrity(inspect_image(path))


def _reviewable_frame_integrity(image: Any) -> dict[str, Any]:
    return _require_reviewable_media_integrity(inspect_frame(image))


def _is_clean_recorded_media_integrity(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == set(_MEDIA_INTEGRITY_FIELDS)
        and value.get("status") == "ok"
        and isinstance(value.get("width"), int)
        and value.get("width") == PROXY_WIDTH
        and isinstance(value.get("height"), int)
        and value.get("height") == PROXY_HEIGHT
        and all(
            isinstance(value.get(key), (int, float)) and not isinstance(value.get(key), bool)
            for key in (
                "mean_luma",
                "std_luma",
                "texture_tile_ratio",
                "dominant_color_ratio",
            )
        )
        and value.get("likely_corrupt") is False
        and value.get("low_information") is False
        and value.get("gray") is False
        and isinstance(value.get("reasons"), list)
        and all(isinstance(reason, str) for reason in value["reasons"])
    )


def _is_complete_verifier_evidence(value: Any, expected_indices: list[int]) -> bool:
    boolean_fields = {
        "independent_verification_performed",
        "full_proxy_decode_verified",
        "frame_count_exact",
        "fps_exact",
        "dimensions_exact",
        "pixel_format_yuv420p_verified",
        "sample_aspect_ratio_1_1_verified",
        "single_video_stream_without_auxiliary_streams",
        "sample_indices_exact",
        "sample_pixels_match_exact_proxy_frames",
        "sample_media_integrity_verified",
        "generated_sample_media_integrity_verified",
    }
    if not isinstance(value, dict) or set(value) != boolean_fields | {"sample_count", "sample_media_integrity"}:
        return False
    if any(value.get(field) is not True for field in boolean_fields):
        return False
    samples = value.get("sample_media_integrity")
    if (
        value.get("sample_count") != len(expected_indices)
        or not isinstance(samples, list)
        or len(samples) != len(expected_indices)
    ):
        return False
    if [sample.get("frame_index") for sample in samples if isinstance(sample, dict)] != expected_indices:
        return False
    return all(
        isinstance(sample, dict)
        and set(sample) == {"frame_index", "generated_sample", "independent_exact_frame_sample"}
        and _is_clean_recorded_media_integrity(sample.get("generated_sample"))
        and _is_clean_recorded_media_integrity(sample.get("independent_exact_frame_sample"))
        and sample["generated_sample"] == sample["independent_exact_frame_sample"]
        for sample in samples
    )


def _create_snapshot_descriptor(path: Path) -> int:
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    if os.name != "nt":
        return os.open(
            path,
            flags | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )

    import ctypes
    import msvcrt
    from ctypes import wintypes

    generic_read_write = 0xC0000000
    create_new = 1
    file_attribute_normal = 0x00000080
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
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    handle = create_file(
        str(path),
        generic_read_write,
        0,
        None,
        create_new,
        file_attribute_normal,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateFileW failed")
    try:
        return msvcrt.open_osfhandle(int(handle), flags)
    except Exception:
        close_handle(handle)
        raise


def _hash_file_cancellable(
    path: Path,
    should_cancel: Callable[[], bool],
    *,
    expected_identity: tuple[int, int, int, int, int, int],
    expected_size: int,
    max_bytes: int,
    trusted_root: Path | None = None,
    label: str = "review proxy source",
    copy_to: Path | None = None,
    copy_trusted_root: Path | None = None,
) -> tuple[str, int, Any | None]:
    """Hash one frozen source and optionally retain its exact snapshot handle."""

    if (
        not isinstance(expected_identity, tuple)
        or len(expected_identity) != 6
        or any(isinstance(value, bool) or not isinstance(value, int) for value in expected_identity)
        or isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size < 0
        or isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes < 0
        or (copy_to is None) is not (copy_trusted_root is None)
    ):
        raise DetectorDevelopmentError(
            "invalid_resource_limit",
            "Review proxy source byte limits are invalid",
        )
    _raise_if_cancelled(should_cancel)
    ancestors = _capture_ancestor_identities(path, trusted_root, label)
    if regular_file_change_identity(path, label) != expected_identity:
        raise DetectorDevelopmentError(
            "source_changed",
            "Review proxy source identity changed before hashing",
        )
    if expected_identity[2] != expected_size:
        raise DetectorDevelopmentError(
            "source_changed",
            "Review proxy source size changed before hashing",
        )
    if expected_size > max_bytes:
        raise DetectorDevelopmentError(
            "resource_limit_exceeded",
            "Review proxy source exceeds its hard byte limit",
        )

    snapshot_ancestors = None
    if copy_to is not None:
        if Path(os.path.abspath(copy_to)) == Path(os.path.abspath(path)):
            raise DetectorDevelopmentError(
                "invalid_snapshot_path",
                "Review proxy source snapshot must be distinct from its source",
            )
        snapshot_ancestors = _capture_ancestor_identities(
            copy_to,
            copy_trusted_root,
            "review proxy source snapshot",
        )

    source_descriptor: int | None = None
    snapshot_descriptor: int | None = None
    digest = hashlib.sha256()
    success = False
    snapshot_path_linked = copy_to is not None

    try:
        source_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        source_flags |= getattr(os, "O_NOFOLLOW", 0)
        source_descriptor = os.open(path, source_flags)
        opened = os.fstat(source_descriptor)
        if not stat.S_ISREG(opened.st_mode) or stat_token(opened) != expected_identity[:5]:
            raise DetectorDevelopmentError(
                "source_changed",
                "Review proxy source changed while it was opened",
            )
        if copy_to is not None:
            snapshot_descriptor = _create_snapshot_descriptor(copy_to)
            if not stat.S_ISREG(os.fstat(snapshot_descriptor).st_mode):
                raise DetectorDevelopmentError(
                    "unsafe_snapshot_path",
                    "Review proxy source snapshot must be a regular file",
                )
            if os.name != "nt":
                copy_to.unlink()
                snapshot_path_linked = False
        remaining = expected_size
        while remaining:
            _raise_if_cancelled(should_cancel)
            chunk = os.read(
                source_descriptor,
                min(_SOURCE_HASH_CHUNK_BYTES, remaining),
            )
            _raise_if_cancelled(should_cancel)
            if not chunk:
                raise DetectorDevelopmentError(
                    "source_changed",
                    "Review proxy source shrank while it was hashed",
                )
            digest.update(chunk)
            if snapshot_descriptor is not None:
                written = 0
                while written < len(chunk):
                    _raise_if_cancelled(should_cancel)
                    count = os.write(snapshot_descriptor, chunk[written:])
                    if count <= 0:
                        raise OSError("review proxy snapshot write made no progress")
                    written += count
            remaining -= len(chunk)
        _raise_if_cancelled(should_cancel)
        if os.read(source_descriptor, 1):
            raise DetectorDevelopmentError(
                "source_changed",
                "Review proxy source grew while it was hashed",
            )
        if stat_token(os.fstat(source_descriptor)) != expected_identity[:5]:
            raise DetectorDevelopmentError(
                "source_changed",
                "Review proxy source changed while it was hashed",
            )
        if snapshot_descriptor is not None:
            os.fsync(snapshot_descriptor)
            if os.fstat(snapshot_descriptor).st_size != expected_size:
                raise DetectorDevelopmentError(
                    "source_snapshot_changed",
                    "Review proxy source snapshot size is invalid",
                )
        if regular_file_change_identity(path, label) != expected_identity or not _ancestor_identities_are_current(
            ancestors
        ):
            raise DetectorDevelopmentError(
                "source_changed",
                "Review proxy source or an ancestor changed while it was hashed",
            )
        _raise_if_cancelled(should_cancel)
        if copy_to is not None:
            if snapshot_ancestors is None or not _ancestor_identities_are_current(snapshot_ancestors):
                raise DetectorDevelopmentError(
                    "source_snapshot_changed",
                    "Review proxy source snapshot ancestor changed",
                )
        _raise_if_cancelled(should_cancel)
        snapshot_handle = None
        if snapshot_descriptor is not None:
            os.lseek(snapshot_descriptor, 0, os.SEEK_SET)
            snapshot_handle = os.fdopen(snapshot_descriptor, "rb")
            snapshot_descriptor = None
        success = True
        return digest.hexdigest(), expected_size, snapshot_handle
    except DetectorDevelopmentError:
        raise
    except OSError as exc:
        if exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) == 112:
            raise DetectorDevelopmentError(
                "disk_exhausted",
                _safe_review_proxy_failure_message("disk_exhausted"),
            ) from exc
        raise DetectorDevelopmentError(
            "path_unavailable",
            "Review proxy source or secure snapshot could not be accessed",
        ) from exc
    finally:
        for open_descriptor in (snapshot_descriptor, source_descriptor):
            if open_descriptor is not None:
                try:
                    os.close(open_descriptor)
                except OSError:
                    pass
        if not success and copy_to is not None and snapshot_path_linked:
            try:
                copy_to.unlink(missing_ok=True)
            except OSError:
                pass


def _attach_windows_kill_job(process: subprocess.Popen[Any]) -> int | None:
    if os.name != "nt":
        if process.poll() is not None:
            raise RuntimeError("review proxy worker exited before containment")
        return None
    import ctypes
    from ctypes import wintypes

    class BasicLimitInformation(ctypes.Structure):
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

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
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
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        information = ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(information), ctypes.sizeof(information)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel32.AssignProcessToJobObject(handle, wintypes.HANDLE(int(process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(handle)
    except Exception:
        _close_windows_handle(int(handle))
        raise


def _close_windows_handle(handle: int | None) -> None:
    if handle is None or os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle(wintypes.HANDLE(handle))


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5.0,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            os.killpg(os.getpgid(process.pid), 15)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5.0)


def _trusted_result_file(root: Path, value: Any) -> Path:
    return require_trusted_relative_path(root, value, "review proxy result file")


def _validate_exact_result_allowlist(root: Path, expected_files: set[str]) -> None:
    exact_regular_tree_snapshot(
        root,
        expected_files,
        "review proxy result tree",
        trusted_root=root.parent,
    )


def _fsync_regular_file(path: Path) -> None:
    try:
        with path.open("r+b") as handle:
            os.fsync(handle.fileno())
    except OSError as exc:
        raise DetectorDevelopmentError(
            "artifact_write_failed", "Review proxy artifact could not be durably flushed"
        ) from exc


def _read_optional_json(path: Path, root: Path, label: str, *, max_bytes: int = 64 * 1024) -> dict[str, Any] | None:
    if not path.exists() and not is_link_or_reparse(path):
        return None
    frozen_ancestors = _capture_ancestor_identities(path, root, label)
    frozen_containers = _worker_protocol_container_change_identities(
        frozen_ancestors,
        label,
    )
    try:
        directory_guards = _open_windows_ancestor_guards(frozen_ancestors, label)
    except DetectorDevelopmentError as exc:
        raise DetectorDevelopmentError(
            "invalid_worker_protocol",
            f"{label} trusted directory changed during protocol sampling",
        ) from exc
    last_sampling_error: DetectorDevelopmentError | None = None
    try:
        for _attempt in range(8):
            _assert_worker_protocol_directories_current(
                frozen_ancestors,
                frozen_containers,
                label,
            )
            if not path.exists() and not is_link_or_reparse(path):
                raise DetectorDevelopmentError(
                    "invalid_worker_protocol",
                    f"{label} disappeared during protocol sampling",
                )
            try:
                content, _ = read_regular_bytes(path, label, max_bytes=max_bytes, trusted_root=root)
            except DetectorDevelopmentError as exc:
                if exc.code not in {"source_changed", "path_unavailable"}:
                    raise
                # Heartbeat and progress files are atomically replaced by the
                # worker while the coordinator samples them. On Windows, a
                # replacement that races one bounded read may surface as either
                # an identity change or a transient unavailable leaf.
                last_sampling_error = exc
                _assert_worker_protocol_directories_current(
                    frozen_ancestors,
                    frozen_containers,
                    label,
                )
                time.sleep(0)
                continue
            except OSError as exc:
                raise DetectorDevelopmentError(
                    "invalid_worker_protocol",
                    f"{label} trusted directory changed during protocol sampling",
                ) from exc
            _assert_worker_protocol_directories_current(
                frozen_ancestors,
                frozen_containers,
                label,
            )
            return json_object_from_bytes(content, label)
        raise DetectorDevelopmentError(
            "invalid_worker_protocol",
            f"{label} did not stabilize after bounded atomic-replacement retries",
        ) from last_sampling_error
    finally:
        _close_windows_handles(directory_guards)


def _worker_protocol_container_change_identities(
    frozen_ancestors: tuple[tuple[Path, tuple[int, int]], ...],
    label: str,
) -> tuple[tuple[Path, tuple[int, int, int, int, int, int]], ...]:
    identities: list[tuple[Path, tuple[int, int, int, int, int, int]]] = []
    seen: set[Path] = set()
    try:
        for ancestor, _expected in frozen_ancestors:
            container = ancestor.parent
            if container in seen:
                continue
            seen.add(container)
            metadata = container.lstat()
            if stat.S_ISLNK(metadata.st_mode) or is_link_or_reparse(container) or not stat.S_ISDIR(metadata.st_mode):
                raise DetectorDevelopmentError(
                    "source_changed",
                    f"{label} trusted directory changed during protocol sampling",
                )
            identity = _change_identity(
                container,
                stat_token(metadata),
                label,
                directory=True,
            )
            identities.append((container, identity))
    except (DetectorDevelopmentError, OSError) as exc:
        raise DetectorDevelopmentError(
            "invalid_worker_protocol",
            f"{label} trusted directory changed during protocol sampling",
        ) from exc
    return tuple(identities)


def _assert_worker_protocol_directories_current(
    frozen_ancestors: tuple[tuple[Path, tuple[int, int]], ...],
    frozen_containers: tuple[tuple[Path, tuple[int, int, int, int, int, int]], ...],
    label: str,
) -> None:
    if (
        not _ancestor_identities_are_current(frozen_ancestors)
        or _worker_protocol_container_change_identities(frozen_ancestors, label) != frozen_containers
    ):
        raise DetectorDevelopmentError(
            "invalid_worker_protocol",
            f"{label} trusted directory changed during protocol sampling",
        )


class _HeldLease:
    def __init__(
        self,
        *,
        target: Path,
        target_handle: Any,
        ancestor_identities: tuple[tuple[Path, tuple[int, int]], ...],
        ancestor_handles: tuple[Any, ...],
    ) -> None:
        self._target = target
        self._target_handle = target_handle
        self._ancestor_identities = ancestor_identities
        self._ancestor_handles = ancestor_handles
        self._released = False

    def validate(self) -> None:
        _validate_lease_ancestors(self._ancestor_identities, self._ancestor_handles)
        _validate_lease_target(self._target, self._target_handle)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            _unlock_lease_handle(self._target_handle)
        finally:
            try:
                _close_lease_handle(self._target_handle)
            finally:
                _close_lease_ancestor_handles(self._ancestor_handles)


class _HardenedLease:
    """Native death-released lease with no-follow and stable-identity guards."""

    def __init__(
        self,
        lease_dir: Path,
        *,
        trusted_root: Path,
        label: str,
        before_open_hook: Callable[[Path], None] | None = None,
        after_open_hook: Callable[[Path], None] | None = None,
    ) -> None:
        self.lease_dir = lease_dir
        self.trusted_root = trusted_root
        self.label = label
        self.lock_path = lease_dir / "coordination.lock"
        self._before_open_hook = before_open_hook
        self._after_open_hook = after_open_hook
        self._expected_ancestor_identities = _capture_lease_ancestor_identities(
            lease_dir,
            trusted_root,
            label,
        )

    @property
    def identity(self) -> tuple[int, int]:
        return self._expected_ancestor_identities[-1][1]

    def acquire(self, *, blocking: bool) -> _HeldLease | None:
        ancestor_identities = _capture_lease_ancestor_identities(self.lease_dir, self.trusted_root, self.label)
        if ancestor_identities != self._expected_ancestor_identities:
            raise DetectorDevelopmentError(
                "unsafe_result_tree",
                f"Review proxy {self.label} lease identity changed",
            )
        ancestor_handles: tuple[Any, ...] = ()
        target_handle: Any | None = None
        target = self.lock_path if os.name == "nt" else self.lease_dir
        try:
            ancestor_handles = _open_lease_ancestor_handles(ancestor_identities, self.label)
            if self._before_open_hook is not None:
                self._before_open_hook(target)
            target_handle = _open_lease_target(self.lease_dir, self.lock_path)
            if self._after_open_hook is not None:
                self._after_open_hook(target)
            held = _HeldLease(
                target=target,
                target_handle=target_handle,
                ancestor_identities=ancestor_identities,
                ancestor_handles=ancestor_handles,
            )
            held.validate()
            if not _lock_lease_handle(target_handle, blocking=blocking):
                handle_to_close = target_handle
                guards_to_close = ancestor_handles
                target_handle = None
                ancestor_handles = ()
                try:
                    _close_lease_handle(handle_to_close)
                finally:
                    _close_lease_ancestor_handles(guards_to_close)
                return None
            held.validate()
            return held
        except BaseException as exc:
            try:
                if target_handle is not None:
                    _close_lease_handle(target_handle)
            finally:
                _close_lease_ancestor_handles(ancestor_handles)
            if isinstance(exc, OSError):
                raise DetectorDevelopmentError(
                    "unsafe_result_tree",
                    f"Review proxy {self.label} lease is unsafe",
                ) from exc
            raise


def _capture_lease_ancestor_identities(
    lease_dir: Path,
    trusted_root: Path,
    label: str,
) -> tuple[tuple[Path, tuple[int, int]], ...]:
    root = Path(os.path.abspath(trusted_root))
    target = Path(os.path.abspath(lease_dir))
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise DetectorDevelopmentError("unsafe_result_tree", f"Review proxy {label} lease escaped its root") from exc
    paths = [root]
    current = root
    for part in relative.parts:
        current = current / part
        paths.append(current)
    identities: list[tuple[Path, tuple[int, int]]] = []
    for path in paths:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise DetectorDevelopmentError(
                "unsafe_result_tree",
                f"Review proxy {label} lease ancestor is unavailable",
            ) from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or bool(getattr(metadata, "st_file_attributes", 0) & 0x00000400)
        ):
            raise DetectorDevelopmentError("unsafe_result_tree", f"Review proxy {label} lease ancestor is unsafe")
        identities.append((path, (int(metadata.st_dev), int(metadata.st_ino))))
    return tuple(identities)


def _open_lease_ancestor_handles(
    identities: tuple[tuple[Path, tuple[int, int]], ...],
    label: str,
) -> tuple[Any, ...]:
    if os.name == "nt":
        return tuple(_open_windows_ancestor_guards(identities, f"review proxy {label} lease"))
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    close_on_exec = getattr(os, "O_CLOEXEC", None)
    if no_follow is None or directory is None or close_on_exec is None:
        raise DetectorDevelopmentError("unsafe_result_tree", f"Review proxy {label} lease is unsupported")
    handles: list[int] = []
    try:
        for path, expected in identities:
            handle = os.open(path, os.O_RDONLY | directory | no_follow | close_on_exec)
            handles.append(handle)
            opened = os.fstat(handle)
            current = path.lstat()
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(current.st_mode)
                or stat.S_ISLNK(current.st_mode)
                or (int(opened.st_dev), int(opened.st_ino)) != expected
                or (int(current.st_dev), int(current.st_ino)) != expected
            ):
                raise DetectorDevelopmentError("unsafe_result_tree", f"Review proxy {label} lease ancestor changed")
        return tuple(handles)
    except BaseException:
        for handle in reversed(handles):
            os.close(handle)
        raise


def _open_lease_target(lease_dir: Path, lock_path: Path) -> Any:
    if os.name == "nt":
        return _open_windows_lease_file(lock_path)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    close_on_exec = getattr(os, "O_CLOEXEC", None)
    if no_follow is None or directory is None or close_on_exec is None:
        raise DetectorDevelopmentError("unsafe_result_tree", "Review proxy native lease is unsupported")
    return os.open(lease_dir, os.O_RDONLY | directory | no_follow | close_on_exec)


def _open_windows_lease_file(path: Path):
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = (
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
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
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_file_information = kernel32.GetFileInformationByHandle
    get_file_information.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
    get_file_information.restype = wintypes.BOOL
    get_file_type = kernel32.GetFileType
    get_file_type.argtypes = (wintypes.HANDLE,)
    get_file_type.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    raw_handle = create_file(
        str(path),
        0xC0000000,
        0x00000003,
        None,
        4,
        0x00200080,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if raw_handle is None or int(raw_handle) == invalid_handle:
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error), str(path))
    descriptor: int | None = None
    try:
        information = ByHandleFileInformation()
        if not get_file_information(raw_handle, ctypes.byref(information)):
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error), str(path))
        if (
            get_file_type(raw_handle) != 1
            or int(information.file_attributes) & 0x00000410
            or int(information.number_of_links) != 1
        ):
            raise DetectorDevelopmentError("unsafe_result_tree", "Review proxy native lease is unsafe")
        descriptor = msvcrt.open_osfhandle(int(raw_handle), os.O_RDWR | getattr(os, "O_BINARY", 0))
        return os.fdopen(descriptor, "r+b", closefd=True)
    except BaseException:
        if descriptor is None:
            close_handle(raw_handle)
        else:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _lease_fileno(handle: Any) -> int:
    return handle if isinstance(handle, int) else handle.fileno()


def _lock_lease_handle(handle: Any, *, blocking: bool) -> bool:
    descriptor = _lease_fileno(handle)
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        while True:
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if not blocking:
                    return False
                time.sleep(0.05)
                continue
            return True
    import fcntl

    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(descriptor, flags)
    except BlockingIOError:
        return False
    return True


def _unlock_lease_handle(handle: Any) -> None:
    descriptor = _lease_fileno(handle)
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        pass


def _close_lease_handle(handle: Any) -> None:
    if isinstance(handle, int):
        os.close(handle)
    else:
        handle.close()


def _close_lease_ancestor_handles(handles: tuple[Any, ...]) -> None:
    if os.name == "nt":
        _close_windows_handles(handles)
        return
    for handle in reversed(handles):
        os.close(handle)


def _validate_lease_ancestors(
    identities: tuple[tuple[Path, tuple[int, int]], ...],
    handles: tuple[Any, ...],
) -> None:
    if len(identities) != len(handles):
        raise DetectorDevelopmentError("unsafe_result_tree", "Review proxy lease ancestor guards are incomplete")
    if os.name == "nt":
        if not _ancestor_identities_are_current(identities):
            raise DetectorDevelopmentError("unsafe_result_tree", "Review proxy lease ancestor changed")
        return
    for (path, expected), handle in zip(identities, handles, strict=True):
        try:
            opened = os.fstat(_lease_fileno(handle))
            current = path.lstat()
        except OSError as exc:
            raise DetectorDevelopmentError("unsafe_result_tree", "Review proxy lease ancestor changed") from exc
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or bool(getattr(current, "st_file_attributes", 0) & 0x00000400)
            or (int(opened.st_dev), int(opened.st_ino)) != expected
            or (int(current.st_dev), int(current.st_ino)) != expected
        ):
            raise DetectorDevelopmentError("unsafe_result_tree", "Review proxy lease ancestor changed")


def _validate_lease_target(path: Path, handle: Any) -> None:
    try:
        opened = os.fstat(_lease_fileno(handle))
        current = path.lstat()
    except OSError as exc:
        raise DetectorDevelopmentError("unsafe_result_tree", "Review proxy lease identity is unavailable") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x00000400)
    expected_type = stat.S_ISREG if os.name == "nt" else stat.S_ISDIR
    if (
        not expected_type(opened.st_mode)
        or not expected_type(current.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or bool(getattr(opened, "st_file_attributes", 0) & reparse_flag)
        or bool(getattr(current, "st_file_attributes", 0) & reparse_flag)
        or (os.name == "nt" and (int(opened.st_nlink) != 1 or int(current.st_nlink) != 1))
        or (int(opened.st_dev), int(opened.st_ino)) != (int(current.st_dev), int(current.st_ino))
    ):
        raise DetectorDevelopmentError("unsafe_result_tree", "Review proxy lease identity changed")


def _remove_verified_lease_artifact(lease: _HardenedLease) -> bool:
    """Remove a unique, unlocked owner lease without following attacker links."""

    if lease.label != "owner":
        return False
    try:
        held = lease.acquire(blocking=False)
    except (DetectorDevelopmentError, OSError):
        return False
    if held is None:
        return False
    released = False
    try:
        held.validate()
        directory_metadata = lease.lease_dir.lstat()
        expected_directory = (int(directory_metadata.st_dev), int(directory_metadata.st_ino))
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or stat.S_ISLNK(directory_metadata.st_mode)
            or bool(getattr(directory_metadata, "st_file_attributes", 0) & 0x00000400)
        ):
            return False

        if os.name != "nt":
            # POSIX permits unlinking an empty directory while its descriptor
            # remains open. Keeping the flock held closes the reacquire/rmdir
            # race and a non-empty or replaced directory is left untouched.
            if any(lease.lease_dir.iterdir()):
                return False
            held.validate()
            lease.lease_dir.rmdir()
            return True

        if {entry.name for entry in lease.lease_dir.iterdir()} != {lease.lock_path.name}:
            return False
        lock_metadata = lease.lock_path.lstat()
        expected_lock = (int(lock_metadata.st_dev), int(lock_metadata.st_ino))
        if (
            not stat.S_ISREG(lock_metadata.st_mode)
            or stat.S_ISLNK(lock_metadata.st_mode)
            or bool(getattr(lock_metadata, "st_file_attributes", 0) & 0x00000400)
            or int(lock_metadata.st_nlink) != 1
        ):
            return False
        held.release()
        released = True
        if not _delete_windows_file_by_handle(lease.lock_path, expected_lock):
            return False
        return _delete_windows_directory_by_handle(lease.lease_dir, expected_directory)
    except (DetectorDevelopmentError, OSError):
        return False
    finally:
        if not released:
            held.release()


def _delete_windows_file_by_handle(path: Path, expected_identity: tuple[int, int]) -> bool:
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = (
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
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
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
    get_information.restype = wintypes.BOOL
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    set_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    raw_handle = create_file(
        str(path),
        0x00010080,
        0x00000003,
        None,
        3,
        0x00200000,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if raw_handle is None or int(raw_handle) == invalid_handle:
        return False
    try:
        information = ByHandleFileInformation()
        if not get_information(raw_handle, ctypes.byref(information)):
            return False
        identity = (
            int(information.volume_serial_number),
            (int(information.file_index_high) << 32) | int(information.file_index_low),
        )
        if (
            identity != expected_identity
            or int(information.file_attributes) & 0x00000410
            or int(information.number_of_links) != 1
        ):
            return False
        delete = wintypes.BOOL(True)
        return bool(set_information(raw_handle, 4, ctypes.byref(delete), ctypes.sizeof(delete)))
    finally:
        close_handle(raw_handle)


def _delete_windows_directory_by_handle(path: Path, expected_identity: tuple[int, int]) -> bool:
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = (
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
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
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
    get_information.restype = wintypes.BOOL
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    set_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    raw_handle = create_file(
        str(path),
        0x00010080,
        0x00000003,
        None,
        3,
        0x02200000,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if raw_handle is None or int(raw_handle) == invalid_handle:
        return False
    marked = False
    try:
        information = ByHandleFileInformation()
        if not get_information(raw_handle, ctypes.byref(information)):
            return False
        identity = (
            int(information.volume_serial_number),
            (int(information.file_index_high) << 32) | int(information.file_index_low),
        )
        if (
            identity != expected_identity
            or not int(information.file_attributes) & 0x00000010
            or int(information.file_attributes) & 0x00000400
            or any(path.iterdir())
        ):
            return False
        delete = wintypes.BOOL(True)
        marked = bool(set_information(raw_handle, 4, ctypes.byref(delete), ctypes.sizeof(delete)))
        return marked
    except OSError:
        return False
    finally:
        close_handle(raw_handle)


def _publish_staging_directory(
    staging: Path,
    destination: Path,
    *,
    attack_hook: Callable[[Path], None] | None = None,
) -> None:
    expected = getattr(_DIRECTORY_MUTATION_CONTEXT, "publish_identity", None)
    if expected is None:
        expected = _safe_directory_identity(staging)
    _fsync_directory(staging)
    _guarded_noreplace_directory_rename(
        staging,
        destination,
        expected_identity=expected,
        attack_hook=attack_hook,
    )
    _fsync_directory(destination.parent)


def _safe_remove_matching_staging(job_id: str, root: Path) -> bool:
    removed = True
    for path in root.glob(f".{job_id}.staging-*"):
        removed = _safe_remove_tree(path, root) and removed
    return removed and not any(root.glob(f".{job_id}.staging-*"))


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_remove_tree(
    path: Path,
    trusted_parent: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
    attack_hook: Callable[[Path], None] | None = None,
    post_quarantine_hook: Callable[[Path], None] | None = None,
    after_quarantine_pin_hook: Callable[[Path], None] | None = None,
) -> bool:
    candidate = Path(os.path.abspath(path))
    parent = Path(os.path.abspath(trusted_parent))
    if candidate.parent != parent:
        return False
    try:
        candidate_metadata = candidate.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    observed_identity = (int(candidate_metadata.st_dev), int(candidate_metadata.st_ino))
    if (
        not stat.S_ISDIR(candidate_metadata.st_mode)
        or stat.S_ISLNK(candidate_metadata.st_mode)
        or bool(getattr(candidate_metadata, "st_file_attributes", 0) & 0x00000400)
        or (expected_identity is not None and observed_identity != expected_identity)
        or not _preflight_safe_tree(candidate)
    ):
        return False
    quarantine = parent / f".delete-{candidate.name}-{uuid.uuid4().hex}"
    try:
        _guarded_noreplace_directory_rename(
            candidate,
            quarantine,
            expected_identity=observed_identity,
            attack_hook=attack_hook,
        )
        if post_quarantine_hook is not None:
            post_quarantine_hook(quarantine)
        if os.name == "posix":
            removed = _remove_posix_anchored_tree(quarantine, parent, observed_identity)
        else:
            removed = _remove_windows_quarantined_tree(
                quarantine,
                observed_identity,
                attack_hook=after_quarantine_pin_hook,
            )
    except (DetectorDevelopmentError, OSError):
        return False
    return removed and not _directory_entry_exists(candidate) and not _directory_entry_exists(quarantine)


def _safe_directory_identity(path: Path) -> tuple[int, int]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise DetectorDevelopmentError("unsafe_result_tree", "Review proxy directory is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & 0x00000400)
    ):
        raise DetectorDevelopmentError("unsafe_result_tree", "Review proxy directory is unsafe")
    return int(metadata.st_dev), int(metadata.st_ino)


def _directory_entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _guarded_noreplace_directory_rename(
    source: Path,
    destination: Path,
    *,
    expected_identity: tuple[int, int],
    attack_hook: Callable[[Path], None] | None = None,
) -> None:
    source = Path(os.path.abspath(source))
    destination = Path(os.path.abspath(destination))
    if source.parent != destination.parent or source.name in {"", ".", ".."} or destination.name in {"", ".", ".."}:
        raise DetectorDevelopmentError("unsafe_result_tree", "Review proxy directory rename is unsafe")
    parent = source.parent
    parent_identity = _safe_directory_identity(parent)
    identities = ((parent, parent_identity),)
    handles = _open_lease_ancestor_handles(identities, "result mutation")
    try:
        _validate_lease_ancestors(identities, handles)
        if _safe_directory_identity(source) != expected_identity:
            raise DetectorDevelopmentError("source_changed", "Review proxy directory identity changed")
        if _directory_entry_exists(destination):
            raise DetectorDevelopmentError("result_already_exists", "Review proxy destination already exists")
        if attack_hook is not None:
            attack_hook(source)
        _validate_lease_ancestors(identities, handles)
        if _safe_directory_identity(source) != expected_identity:
            raise DetectorDevelopmentError("source_changed", "Review proxy directory identity changed")
        if _directory_entry_exists(destination):
            raise DetectorDevelopmentError("result_already_exists", "Review proxy destination already exists")
        if os.name == "posix":
            parent_descriptor = int(handles[-1])
            source_descriptor = os.open(
                source.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_descriptor,
            )
            try:
                opened = os.fstat(source_descriptor)
                if (int(opened.st_dev), int(opened.st_ino)) != expected_identity:
                    raise DetectorDevelopmentError("source_changed", "Review proxy directory identity changed")
                _renameat2_noreplace(parent_descriptor, source.name, destination.name)
            finally:
                os.close(source_descriptor)
        else:
            _windows_rename_directory_by_handle(
                source,
                destination,
                expected_identity,
                int(handles[-1]),
            )
        _validate_lease_ancestors(identities, handles)
        if _safe_directory_identity(destination) != expected_identity or _directory_entry_exists(source):
            raise DetectorDevelopmentError("source_changed", "Review proxy directory changed during rename")
    except FileExistsError as exc:
        raise DetectorDevelopmentError("result_already_exists", "Review proxy destination already exists") from exc
    finally:
        _close_lease_ancestor_handles(handles)


def _windows_rename_directory_by_handle(
    source: Path,
    destination: Path,
    expected_identity: tuple[int, int],
    parent_handle: int,
) -> None:
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = (
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        )

    class FileRenameInformation(ctypes.Structure):
        _fields_ = (
            ("replace_if_exists", ctypes.c_ubyte),
            ("root_directory", wintypes.HANDLE),
            ("file_name_length", wintypes.DWORD),
            ("file_name", wintypes.WCHAR * 1),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
    get_information.restype = wintypes.BOOL
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    set_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    raw_handle = create_file(
        str(source),
        0x00010080,
        0x00000003,
        None,
        3,
        0x02200000,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if raw_handle is None or int(raw_handle) == invalid_handle:
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error), str(source))
    try:
        information = ByHandleFileInformation()
        if not get_information(raw_handle, ctypes.byref(information)):
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error), str(source))
        identity = (
            int(information.volume_serial_number),
            (int(information.file_index_high) << 32) | int(information.file_index_low),
        )
        if (
            identity != expected_identity
            or not int(information.file_attributes) & 0x00000010
            or int(information.file_attributes) & 0x00000400
        ):
            raise DetectorDevelopmentError("source_changed", "Review proxy directory identity changed")
        encoded_name = str(destination).encode("utf-16-le")
        buffer_size = FileRenameInformation.file_name.offset + len(encoded_name) + ctypes.sizeof(wintypes.WCHAR)
        buffer = ctypes.create_string_buffer(buffer_size)
        rename = ctypes.cast(buffer, ctypes.POINTER(FileRenameInformation)).contents
        rename.replace_if_exists = 0
        rename.root_directory = None
        rename.file_name_length = len(encoded_name)
        ctypes.memmove(
            ctypes.addressof(buffer) + FileRenameInformation.file_name.offset,
            encoded_name,
            len(encoded_name),
        )
        if not set_information(raw_handle, 3, buffer, buffer_size):
            error = ctypes.get_last_error()
            if error in {80, 183}:
                raise DetectorDevelopmentError("result_already_exists", "Review proxy destination already exists")
            raise OSError(error, ctypes.FormatError(error), str(destination))
        if _safe_directory_identity(destination) != expected_identity or _directory_entry_exists(source):
            raise DetectorDevelopmentError("source_changed", "Review proxy directory changed during rename")
    finally:
        close_handle(raw_handle)


def _renameat2_noreplace(parent_descriptor: int, source_name: str, destination_name: str) -> None:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise DetectorDevelopmentError(
            "unsafe_result_tree",
            "Atomic no-replace review proxy publication is unavailable",
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
        parent_descriptor,
        os.fsencode(source_name),
        parent_descriptor,
        os.fsencode(destination_name),
        1,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise DetectorDevelopmentError("result_already_exists", "Review proxy destination already exists")
    raise OSError(error, os.strerror(error))


def _preflight_safe_tree(root: Path) -> bool:
    try:
        metadata = root.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or bool(getattr(metadata, "st_file_attributes", 0) & 0x00000400)
        ):
            return False
        for entry in root.iterdir():
            details = entry.lstat()
            if stat.S_ISLNK(details.st_mode) or bool(getattr(details, "st_file_attributes", 0) & 0x00000400):
                return False
            if stat.S_ISDIR(details.st_mode):
                if not _preflight_safe_tree(entry):
                    return False
            elif not stat.S_ISREG(details.st_mode) or int(details.st_nlink) != 1:
                return False
        return True
    except OSError:
        return False


def _remove_posix_anchored_tree(path: Path, parent: Path, expected_identity: tuple[int, int]) -> bool:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    parent_descriptor: int | None = None
    root_descriptor: int | None = None
    try:
        parent_descriptor = os.open(parent, flags)
        root_descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        opened = os.fstat(root_descriptor)
        if (int(opened.st_dev), int(opened.st_ino)) != expected_identity:
            return False
        _remove_posix_directory_contents(root_descriptor)
        current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (int(current.st_dev), int(current.st_ino)) != expected_identity:
            return False
        os.rmdir(path.name, dir_fd=parent_descriptor)
        return True
    except OSError:
        return False
    finally:
        if root_descriptor is not None:
            os.close(root_descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _remove_posix_directory_contents(descriptor: int) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    for name in os.listdir(descriptor):
        details = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(details.st_mode) and not stat.S_ISLNK(details.st_mode):
            child = os.open(name, flags, dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                identity = (int(details.st_dev), int(details.st_ino))
                if (int(opened.st_dev), int(opened.st_ino)) != identity:
                    raise OSError(errno.ESTALE, "review proxy cleanup identity changed")
                _remove_posix_directory_contents(child)
                current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if (int(current.st_dev), int(current.st_ino)) != identity:
                    raise OSError(errno.ESTALE, "review proxy cleanup identity changed")
                os.rmdir(name, dir_fd=descriptor)
            finally:
                os.close(child)
        elif stat.S_ISREG(details.st_mode) and int(details.st_nlink) == 1:
            child = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0),
                dir_fd=descriptor,
            )
            try:
                opened = os.fstat(child)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or int(opened.st_nlink) != 1
                    or (int(opened.st_dev), int(opened.st_ino)) != (int(details.st_dev), int(details.st_ino))
                ):
                    raise OSError(errno.ESTALE, "review proxy cleanup identity changed")
            finally:
                os.close(child)
            os.unlink(name, dir_fd=descriptor)
        else:
            raise OSError(errno.EPERM, "review proxy cleanup encountered an unsafe entry")


def _remove_windows_quarantined_tree(
    path: Path,
    expected_identity: tuple[int, int],
    *,
    attack_hook: Callable[[Path], None] | None = None,
) -> bool:
    """Delete a quarantined tree without ever reopening an unpinned target."""

    if os.name != "nt":
        return False

    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = (
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
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
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
    get_information.restype = wintypes.BOOL
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    set_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    invalid_handle = ctypes.c_void_p(-1).value
    get_file_type = kernel32.GetFileType
    get_file_type.argtypes = (wintypes.HANDLE,)
    get_file_type.restype = wintypes.DWORD

    def path_matches(target: Path, identity: tuple[int, int], *, directory: bool) -> bool:
        try:
            details = target.lstat()
        except OSError:
            return False
        attributes = int(getattr(details, "st_file_attributes", 0))
        return (
            (stat.S_ISDIR(details.st_mode) if directory else stat.S_ISREG(details.st_mode))
            and not stat.S_ISLNK(details.st_mode)
            and not attributes & 0x00000400
            and (directory or int(details.st_nlink) == 1)
            and (int(details.st_dev), int(details.st_ino)) == identity
        )

    def handle_matches(handle: int, identity: tuple[int, int], *, directory: bool) -> bool:
        if get_file_type(wintypes.HANDLE(handle)) != 1:  # FILE_TYPE_DISK
            return False
        information = ByHandleFileInformation()
        if not get_information(wintypes.HANDLE(handle), ctypes.byref(information)):
            return False
        observed = (
            int(information.volume_serial_number),
            (int(information.file_index_high) << 32) | int(information.file_index_low),
        )
        attributes = int(information.file_attributes)
        return (
            observed == identity
            and bool(attributes & 0x00000010) == directory
            and not bool(attributes & 0x00000400)
            and (directory or int(information.number_of_links) == 1)
        )

    def open_pinned(target: Path, identity: tuple[int, int], *, directory: bool) -> int | None:
        raw_handle = create_file(
            str(target),
            0x00010080,  # DELETE | FILE_READ_ATTRIBUTES
            0x00000003,  # Share reads/writes, but never rename/delete.
            None,
            3,  # OPEN_EXISTING
            0x02200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
            None,
        )
        if raw_handle is None or int(raw_handle) == invalid_handle:
            return None
        handle = int(raw_handle)
        if not handle_matches(handle, identity, directory=directory) or not path_matches(
            target,
            identity,
            directory=directory,
        ):
            close_handle(raw_handle)
            return None
        return handle

    def mark_for_delete(handle: int) -> bool:
        delete = ctypes.c_ubyte(1)  # FILE_DISPOSITION_INFO.DeleteFile is BOOLEAN.
        return bool(
            set_information(
                wintypes.HANDLE(handle),
                4,  # FileDispositionInfo
                ctypes.byref(delete),
                ctypes.sizeof(delete),
            )
        )

    def remove_file(target: Path, identity: tuple[int, int]) -> bool:
        handle = open_pinned(target, identity, directory=False)
        if handle is None:
            return False
        marked = False
        try:
            if not handle_matches(handle, identity, directory=False) or not path_matches(
                target,
                identity,
                directory=False,
            ):
                return False
            marked = mark_for_delete(handle)
        finally:
            close_handle(wintypes.HANDLE(handle))
        return marked and not _directory_entry_exists(target)

    def remove_directory(
        target: Path,
        identity: tuple[int, int],
        *,
        root: bool = False,
    ) -> bool:
        handle = open_pinned(target, identity, directory=True)
        if handle is None:
            return False
        marked = False
        try:
            if root and attack_hook is not None:
                attack_hook(target)
            if not handle_matches(handle, identity, directory=True) or not path_matches(
                target,
                identity,
                directory=True,
            ):
                return False
            entries: list[tuple[Path, tuple[int, int], bool]] = []
            for entry in list(target.iterdir()):
                details = entry.lstat()
                attributes = int(getattr(details, "st_file_attributes", 0))
                is_directory = stat.S_ISDIR(details.st_mode)
                if (
                    stat.S_ISLNK(details.st_mode)
                    or attributes & 0x00000400
                    or (not is_directory and (not stat.S_ISREG(details.st_mode) or int(details.st_nlink) != 1))
                ):
                    return False
                entries.append(
                    (
                        entry,
                        (int(details.st_dev), int(details.st_ino)),
                        is_directory,
                    )
                )
            for entry, entry_identity, is_directory in entries:
                removed = (
                    remove_directory(entry, entry_identity) if is_directory else remove_file(entry, entry_identity)
                )
                if not removed:
                    return False
            if (
                not handle_matches(handle, identity, directory=True)
                or not path_matches(target, identity, directory=True)
                or any(target.iterdir())
            ):
                return False
            marked = mark_for_delete(handle)
            if not marked:
                return False
        finally:
            close_handle(wintypes.HANDLE(handle))
        return marked and not _directory_entry_exists(target)

    parent = path.parent
    try:
        parent_identity = _safe_directory_identity(parent)
        identities = ((parent, parent_identity),)
        parent_guards = _open_lease_ancestor_handles(identities, "result deletion")
    except (DetectorDevelopmentError, OSError):
        return False
    try:
        _validate_lease_ancestors(identities, parent_guards)
        removed = remove_directory(path, expected_identity, root=True)
        _validate_lease_ancestors(identities, parent_guards)
        return removed and not _directory_entry_exists(path)
    except (DetectorDevelopmentError, OSError):
        return False
    finally:
        _close_lease_ancestor_handles(parent_guards)


def _raise_if_cancelled(should_cancel: Callable[[], bool]) -> None:
    if should_cancel():
        raise DetectorDevelopmentError("cancelled", "Detector review proxy was cancelled")


def _format_fps(value: float) -> str:
    return format(value, ".12g")


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DetectorDevelopmentError(
            "invalid_review_proxy_request", f"{label} must be a positive integer", status_code=400
        )
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DetectorDevelopmentError(
            "invalid_review_proxy_request", f"{label} must be a non-negative integer", status_code=400
        )
    return value


def _positive_finite(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise DetectorDevelopmentError(
            "invalid_review_proxy_request", f"{label} must be positive and finite", status_code=400
        )
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DetectorDevelopmentError(
            "invalid_review_proxy_request", f"{label} must be positive and finite", status_code=400
        ) from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise DetectorDevelopmentError(
            "invalid_review_proxy_request", f"{label} must be positive and finite", status_code=400
        )
    return parsed
