from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import shutil
import stat
import threading
import uuid
from contextlib import AbstractContextManager
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Callable

from football_tracking.ball_annotation_propagation import (
    TRACKER_PROFILE,
    TRACKER_PROFILE_SHA256,
    PropagationError,
    build_advisory_suggestions,
)
from football_tracking.ball_detector_annotations import (
    BallAnnotationError,
    annotation_etag,
    validate_ball_annotation,
)
from football_tracking.ball_detector_feasibility import (
    FEASIBILITY_METRIC_PROFILE_ID,
    METRIC_PROFILE_SHA256,
    TEMPORAL_BLOCK_SAMPLING_PROFILE_ID,
    TEMPORAL_GROUPING_PROFILE_ID,
    FeasibilityError,
    build_candidate_universe_authority,
    build_feasibility_report,
    inherit_temporal_group,
    sample_unseen_temporal_groups,
    temporal_group_for_frame,
)
from football_tracking.ball_frame_evidence import (
    BallFrameEvidenceError,
    build_detector_probe_job_authority,
    build_detector_probe_result_manifest_authority,
    build_frame_evidence_row,
    build_nullable_proxy_binding,
    build_source_frame_timing_binding,
    normalize_detector_probe_candidates,
    validate_detector_probe_candidate_accounting,
    verify_frame_evidence_package,
)
from football_tracking.detector_audited_authority import (
    AUDITED_T2_LEGACY_PROBE_BINDINGS as _AUDITED_T2_LEGACY_REPORT_BINDINGS,
)
from football_tracking.detector_development_common import (
    DetectorDevelopmentError,
    atomic_write_json,
    canonical_json_bytes,
    canonical_sha256,
    exact_regular_tree_snapshot,
    is_link_or_reparse,
    json_object_from_bytes,
    read_regular_bytes,
    require_safe_id,
    require_sha256,
    secure_mkdirs,
    utc_now_iso,
)
from football_tracking.review_proxy_mapping import (
    ReviewProxyError,
    validate_review_proxy_manifest,
)

_MAX_SESSION_BYTES = 64 * 1024 * 1024
_MAX_SESSION_CANONICAL_BYTES = 48 * 1024 * 1024
_MAX_REVISIONS_PER_FRAME = 128
_MAX_REVISIONS_PER_SESSION = 4096
_MAX_REGISTRY_BYTES = 16 * 1024 * 1024
_MAX_FRAME_BYTES = 32 * 1024 * 1024
_MAX_FINAL_RESULT_BYTES = 64 * 1024 * 1024
_MAX_FINAL_FRAME_MEDIA = 70
_MAX_FINAL_PROPAGATION_REPORT_FILES = 20
_MAX_FINAL_FRAME_INDEX = 999_999_999
_TRUE_PRESENTATION_TIMESTAMP_NOT_COLLECTED = {
    "status": "not_collected",
    "value_seconds": None,
    "method": None,
}
_SCALE_STRATA = ("near", "mid", "far")
_LIGHTING_STRATA = ("bright_sun", "shadow", "backlight", "twilight", "artificial_light")
_SESSION_REQUEST_FIELDS = frozenset(
    {
        "data_role",
        "development_probe_job_ids",
        "locked_profile_id",
        "target_frame_count",
        "sampling_profile_id",
        "metric_profile_id",
        "operator_id",
        "strata_applicability",
        "retry_from_session_id",
        "development_package_session_id",
        "development_package_sha256",
    }
)
_ANNOTATION_REQUIRED_FIELDS = frozenset(
    {"mutation_id", "expected_revision", "operation", "undo_revision", "annotation"}
)
_ANNOTATION_REQUEST_FIELDS = _ANNOTATION_REQUIRED_FIELDS | {
    "suggestion_kind",
    "suggestion_id",
    "accepted_suggestion_job_id",
    "accepted_suggestion_sha256",
    "dismissed_suggestion_kind",
    "dismissed_suggestion_id",
    "dismissed_suggestion_job_id",
    "dismissed_suggestion_sha256",
}
_PROPAGATION_REQUEST_FIELDS = frozenset({"mutation_id", "seed_frame_index", "radius_frames", "expected_seed_revision"})
_MAX_SUPPLEMENTAL_FRAMES_PER_SESSION = 20
_MAX_PROPAGATION_REPORTS_PER_SESSION = 20
_REPORT_CAPABLE_PROPAGATION_STATUSES = frozenset({"queued", "waiting_probe", "committing", "ready"})
_TEMPORAL_GROUP_FIELDS = (
    "group_id",
    "profile_id",
    "source_sha256",
    "seed_frame_index",
    "start_frame",
    "end_frame",
    "derivative_family",
    "canonical_moment_id",
    "derivative_family_id",
    "ancestry_profile",
)
_REGISTRY_ENTRY_FIELDS = frozenset(
    {
        *_TEMPORAL_GROUP_FIELDS,
        "frame_index",
        "session_id",
        "data_role",
        "state",
        "retired_for_all_profiles",
        "created_at",
        "updated_at",
    }
)
_REGISTRY_OPTIONAL_FIELDS = frozenset({"pre_reveal_lighting_stratum"})
_REGISTRY_FIELDS = frozenset({"schema_version", "artifact_type", "entries", "registry_sha256"})
_ACTIVE_CHECK_STATUSES = {"queued", "running", "committing"}
_TERMINAL_CHECK_FAILURES = {"failed", "cancelled", "blocked"}
_REVIEW_PROXY_REQUIRED_ERRORS = {
    "corrupt_probe_frame",
    "decode_integrity_failed",
    "invalid_probe_decode",
    "probe_frame_integrity_failed",
}
_COORDINATION_LOCKS_GUARD = threading.Lock()
_COORDINATION_LOCKS: dict[str, "_CoordinationLock"] = {}
_CoordinationLockHandle = BinaryIO | int


class _CoordinationLock(AbstractContextManager["_CoordinationLock"]):
    """Cross-instance/process lock for sampling reservation and session mutation."""

    def __init__(
        self,
        root: Path,
        *,
        before_open_hook: Callable[[Path], None] | None = None,
        after_open_hook: Callable[[Path], None] | None = None,
    ) -> None:
        self._root = root
        self._path = root / "coordination.lock"
        self._thread_lock = threading.RLock()
        self._local = threading.local()
        self._before_open_hook = before_open_hook
        self._after_open_hook = after_open_hook

    def __enter__(self) -> "_CoordinationLock":
        self._thread_lock.acquire()
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            return self
        handle: _CoordinationLockHandle | None = None
        target = self._path if os.name == "nt" else self._root
        try:
            if self._before_open_hook is not None:
                self._before_open_hook(target)
            handle = _open_coordination_lock_handle(self._root, self._path)
            if self._after_open_hook is not None:
                self._after_open_hook(target)
            _validate_coordination_lock_handle(target, handle)
            _lock_coordination_lock_handle(handle)
            _validate_coordination_lock_handle(target, handle)
        except BaseException as exc:
            try:
                if handle is not None:
                    _close_coordination_lock_handle(handle)
            finally:
                self._thread_lock.release()
            if isinstance(exc, OSError):
                raise BallAnnotationServiceError("unsafe_lock", "Annotation coordination lock is unsafe") from exc
            raise
        self._local.handle = handle
        self._local.depth = 1
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        depth = getattr(self._local, "depth", 0)
        if depth > 1:
            self._local.depth = depth - 1
            self._thread_lock.release()
            return
        handle = self._local.handle
        try:
            _unlock_coordination_lock_handle(handle)
        finally:
            try:
                _close_coordination_lock_handle(handle)
            finally:
                self._local.depth = 0
                self._local.handle = None
                self._thread_lock.release()


def _open_coordination_lock_handle(root: Path, path: Path) -> _CoordinationLockHandle:
    if os.name == "nt":
        return _open_windows_coordination_lock_file(path)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    close_on_exec = getattr(os, "O_CLOEXEC", None)
    if no_follow is None or directory is None or close_on_exec is None:
        raise BallAnnotationServiceError("unsafe_lock", "Annotation coordination lock is unsafe")
    return os.open(root, os.O_RDONLY | directory | no_follow | close_on_exec)


def _open_windows_coordination_lock_file(path: Path) -> BinaryIO:
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
    get_file_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_void_p,
    )
    get_file_information.restype = wintypes.BOOL
    get_file_type = kernel32.GetFileType
    get_file_type.argtypes = (wintypes.HANDLE,)
    get_file_type.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    raw_handle = create_file(
        str(path),
        0xC0000000,  # GENERIC_READ | GENERIC_WRITE
        0x00000003,  # share read/write, but deny delete/rename while locked
        None,
        4,  # OPEN_ALWAYS
        0x00200080,  # FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT
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
            get_file_type(raw_handle) != 1  # FILE_TYPE_DISK
            or int(information.file_attributes) & 0x00000410  # reparse point or directory
            or int(information.number_of_links) != 1
        ):
            raise BallAnnotationServiceError("unsafe_lock", "Annotation coordination lock is unsafe")
        descriptor = msvcrt.open_osfhandle(
            int(raw_handle),
            os.O_RDWR | getattr(os, "O_BINARY", 0),
        )
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


def _coordination_lock_fileno(handle: _CoordinationLockHandle) -> int:
    return handle if isinstance(handle, int) else handle.fileno()


def _lock_coordination_lock_handle(handle: _CoordinationLockHandle) -> None:
    descriptor = _coordination_lock_fileno(handle)
    if os.name == "nt":
        import msvcrt

        assert not isinstance(handle, int)
        handle.seek(0)
        msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX)


def _unlock_coordination_lock_handle(handle: _CoordinationLockHandle) -> None:
    descriptor = _coordination_lock_fileno(handle)
    if os.name == "nt":
        import msvcrt

        assert not isinstance(handle, int)
        handle.seek(0)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _close_coordination_lock_handle(handle: _CoordinationLockHandle) -> None:
    if isinstance(handle, int):
        os.close(handle)
    else:
        handle.close()


def _validate_coordination_lock_handle(path: Path, handle: _CoordinationLockHandle) -> None:
    try:
        opened = os.fstat(_coordination_lock_fileno(handle))
        current = path.lstat()
    except OSError as exc:
        raise BallAnnotationServiceError("unsafe_lock", "Annotation coordination lock is unsafe") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x00000400)
    expected_type_matches = (
        stat.S_ISREG(opened.st_mode) and stat.S_ISREG(current.st_mode)
        if os.name == "nt"
        else stat.S_ISDIR(opened.st_mode) and stat.S_ISDIR(current.st_mode)
    )
    if (
        not expected_type_matches
        or stat.S_ISLNK(current.st_mode)
        or bool(getattr(current, "st_file_attributes", 0) & reparse_flag)
        or (os.name == "nt" and (int(opened.st_nlink) != 1 or int(current.st_nlink) != 1))
        or (int(opened.st_dev), int(opened.st_ino)) != (int(current.st_dev), int(current.st_ino))
    ):
        raise BallAnnotationServiceError("unsafe_lock", "Annotation coordination lock is unsafe")


def _coordination_lock(root: Path) -> _CoordinationLock:
    key = os.path.normcase(str(root.resolve()))
    with _COORDINATION_LOCKS_GUARD:
        return _COORDINATION_LOCKS.setdefault(key, _CoordinationLock(root))


class BallAnnotationServiceError(DetectorDevelopmentError):
    """Stable API-safe failure for detector-development annotation work."""


class BallAnnotationService:
    """Durable source-bound point/box annotation and one-time feasibility service."""

    def __init__(
        self,
        repo_root: Path,
        *,
        get_probe: Callable[[str], dict[str, Any]],
        create_probe: Callable[[dict[str, Any]], dict[str, Any]],
        read_probe_artifact: Callable[[str, str], tuple[bytes, str, str]],
        create_propagation_probe: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        cancel_propagation_probe: Callable[[str], dict[str, Any]] | None = None,
        auto_execute_propagation: bool = True,
        finalize_failpoint: Callable[[str], None] | None = None,
        propagation_failpoint: Callable[[str], None] | None = None,
        confirmation_failpoint: Callable[[str], None] | None = None,
        session_setup_failpoint: Callable[[str], None] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve(strict=True)
        development_root = secure_mkdirs(self.repo_root, "data", "ball_detector_development_v1")
        self._root = secure_mkdirs(development_root, "annotation_sessions")
        self._sessions_root = secure_mkdirs(self._root, "sessions")
        self._packages_root = secure_mkdirs(self._root, "packages")
        self._reports_root = secure_mkdirs(self._root, "feasibility_reports")
        self._final_results_root = secure_mkdirs(self._root, "final_results")
        self._sampling_locks_root = secure_mkdirs(self._root, "sampling_locks")
        self._propagation_root = secure_mkdirs(self._root, "propagation_jobs")
        self._registry_path = self._root / "temporal_group_registry.json"
        self._coordination_lock = _coordination_lock(self._root)
        self._get_probe = get_probe
        self._create_probe = create_probe
        self._create_propagation_probe = create_propagation_probe or create_probe
        self._cancel_propagation_probe = cancel_propagation_probe
        self._read_probe_artifact = read_probe_artifact
        self._auto_execute_propagation = auto_execute_propagation
        self._finalize_failpoint = finalize_failpoint
        self._propagation_failpoint = propagation_failpoint
        self._confirmation_failpoint = confirmation_failpoint
        self._session_setup_failpoint = session_setup_failpoint
        self._lock = threading.RLock()
        self._closed = False
        with self._coordination_lock, self._lock:
            if not self._registry_path.exists():
                self._write_registry(
                    {
                        "schema_version": "1.0",
                        "artifact_type": "ball_temporal_group_registry",
                        "entries": [],
                    }
                )
            self._remove_orphan_final_staging()
            self._recover_initial_check_setup_transactions()
            self._recover_retry_reservation_transactions()
            self._recover_development_group_publications()
            self._recover_orphan_reservations()

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def create_session(self, request: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_session_request(request)
        request_sha256 = canonical_sha256(normalized)
        with self._coordination_lock, self._lock:
            self._require_open()
            for path in self._sessions_root.glob("*.json"):
                existing = self._read_json(path, "annotation session", _MAX_SESSION_BYTES)
                if existing.get("request_sha256") == request_sha256:
                    # A review-proxy replacement is a durable continuation.
                    # If the process stopped after the session record commit
                    # but before temporal-group publication, finish that
                    # publication before returning the same session identity.
                    existing_retry_lineage = existing.get("retry_lineage")
                    if (
                        isinstance(existing_retry_lineage, dict)
                        and existing_retry_lineage.get("mode") == "review_proxy_decode_upgrade"
                    ):
                        self._recover_development_group_publications()
                    return self._public_session(self._reconcile_session(existing))

            retry_from_session_id = normalized["retry_from_session_id"]
            if retry_from_session_id is not None:
                try:
                    retry_parent = self._load_session(retry_from_session_id)
                except BallAnnotationServiceError as exc:
                    if exc.code != "session_not_found":
                        raise
                    raise BallAnnotationServiceError(
                        "invalid_retry",
                        "retry_from_session_id must identify a blocked session with the same data role",
                        status_code=400,
                    ) from exc
                if retry_parent.get("data_role") != normalized["data_role"] or retry_parent.get("status") != "blocked":
                    raise BallAnnotationServiceError(
                        "invalid_retry",
                        "retry_from_session_id must identify a blocked session with the same data role",
                        status_code=400,
                    )

            authority = self._resolve_development_authority(
                normalized["development_probe_job_ids"],
                normalized["locked_profile_id"],
            )
            session_id = f"annotation-{request_sha256[:16]}-{uuid.uuid4().hex[:12]}"
            now = utc_now_iso()
            source = authority["source"]
            development_groups = self._development_groups(authority["frames"], source["sha256"])
            retry_from = normalized["retry_from_session_id"]
            development_package_binding = (
                self._resolve_development_package_binding(normalized, authority)
                if normalized["data_role"] == "check"
                else None
            )
            if normalized["data_role"] == "check" and retry_from is None:
                selection_authority = self._sampling_selection_authority(
                    attempt_family_sha256=development_package_binding["attempt_family_sha256"],
                    development_package_sha256=development_package_binding["package_sha256"],
                    source_sha256=source["sha256"],
                    locked_profile=authority["locked_profile"],
                    request=normalized,
                )
                for path in self._sessions_root.glob("*.json"):
                    existing = self._read_json(path, "annotation session", _MAX_SESSION_BYTES)
                    if (
                        existing.get("data_role") == "check"
                        and existing.get("development_package_binding", {}).get("package_sha256")
                        == development_package_binding["package_sha256"]
                        and existing.get("sampling_manifest", {}).get("selection_authority") == selection_authority
                    ):
                        raise BallAnnotationServiceError(
                            "check_attempt_already_exists",
                            "This development package and sampling authority already have a check attempt; reuse the exact request or explicitly retry a blocked attempt",
                            status_code=409,
                        )
            if retry_from is not None:
                session = self._create_retry_session(
                    session_id=session_id,
                    request_sha256=request_sha256,
                    request=normalized,
                    authority=authority,
                    development_package_binding=development_package_binding,
                    now=now,
                )
            else:
                session = self._new_session(
                    session_id=session_id,
                    request_sha256=request_sha256,
                    request=normalized,
                    authority=authority,
                    development_groups=development_groups,
                    development_package_binding=development_package_binding,
                    now=now,
                )
            try:
                self._persist_session(session)
                self._hit_session_setup_failpoint("after_session_persist")
                if session["data_role"] == "check":
                    self._persist_sampling_lock(session)
                    self._hit_session_setup_failpoint("after_sampling_lock_persist")
                    if session.get("retry_from_session_id") is not None:
                        self._transfer_retry_reservations(session)
                        self._hit_session_setup_failpoint("after_retry_reservation_transfer")
                        self._complete_retry_reservation_transaction(session)
                    if session.get("_initial_check_setup_transaction") is not None:
                        self._complete_initial_check_setup_transaction(session)
                if session["data_role"] == "development":
                    if session["frames"]:
                        self._record_groups(
                            session_id,
                            source["sha256"],
                            session["sampling_manifest"]["groups"],
                            data_role="development",
                            state="revealed",
                        )
            except BaseException:
                # The proxy-upgrade retry is already bound by a higher-level
                # durable continuation intent.  Preserve its committed session
                # row so restart/repeated POST can publish missing groups and
                # return the same session, instead of manufacturing a second
                # replacement attempt.
                retry_lineage = session.get("retry_lineage")
                durable_proxy_retry = (
                    isinstance(retry_lineage, dict)
                    and retry_lineage.get("mode") == "review_proxy_decode_upgrade"
                    and (self._sessions_root / f"{session_id}.json").is_file()
                )
                if not durable_proxy_retry:
                    self._discard_unstarted_session(
                        session_id,
                        retry_reservation_snapshot=session.get("_retry_reservation_snapshot"),
                    )
                raise
            session = self._reconcile_session(session)
            return self._public_session(session)

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._coordination_lock, self._lock:
            self._require_open()
            session = self._load_session(session_id)
            return self._public_session(self._reconcile_session(session))

    def authorize_check_probe_creation(self, session_id: str) -> dict[str, Any]:
        """Re-derive the exact persisted pre-reveal authority for one check probe."""

        with self._coordination_lock, self._lock:
            self._require_open()
            session = self._load_session(session_id)
            if (
                session.get("data_role") != "check"
                or session.get("status") != "sampling_locked"
                or session.get("check_probe_job_id") is not None
                or session.get("check_probe_authority") is not None
                or session.get("frames") != []
                or session.get("revisions") != []
            ):
                raise BallAnnotationServiceError(
                    "check_probe_creation_ineligible",
                    "Only an unrevealed persisted check sampling lock can authorize a probe",
                    status_code=409,
                )
            sampling_lock = self._require_verified_sampling_lock(session)
            self._require_reserved_check_groups(session)
            return self._expected_check_probe_creation_authority(session, sampling_lock)

    def get_review_proxy_repair_authority(self, session_id: str) -> dict[str, Any]:
        """Return a frozen repair seed only for an entirely unrevealed blocker.

        This method intentionally reads the private persisted session under the
        annotation coordination lock.  Public session views omit revisions and
        final-result state and therefore cannot establish repair eligibility.
        """

        with self._coordination_lock, self._lock:
            self._require_open()
            try:
                session = self._load_session(session_id)
            except BallAnnotationServiceError as exc:
                if exc.code != "invalid_session_request_authority":
                    raise
                raise BallAnnotationServiceError(
                    "review_proxy_repair_ineligible",
                    "Blocked session request authority is invalid",
                    status_code=409,
                ) from exc
            if not self._review_proxy_repair_parent_is_pristine(session):
                raise BallAnnotationServiceError(
                    "review_proxy_repair_ineligible",
                    "Review-proxy repair requires a pristine blocked development session",
                    status_code=409,
                )
            registry = self._read_registry()
            if any(entry.get("session_id") == session["session_id"] for entry in registry["entries"]):
                raise BallAnnotationServiceError(
                    "review_proxy_repair_ineligible",
                    "Review-proxy repair is forbidden after temporal-group reveal",
                    status_code=409,
                )
            for path in sorted(self._sessions_root.glob("*.json")):
                if path.name == f"{session['session_id']}.json" or path == self._registry_path:
                    continue
                other = self._read_json(path, "annotation session", _MAX_SESSION_BYTES)
                if other.get("retry_from_session_id") == session["session_id"]:
                    raise BallAnnotationServiceError(
                        "review_proxy_repair_ineligible",
                        "This blocked session already has a replacement attempt",
                        status_code=409,
                    )
            lineage = session.get("lineage")
            jobs = lineage.get("development_probe_job_ids") if isinstance(lineage, dict) else None
            if not isinstance(jobs, list) or not jobs or len(set(jobs)) != len(jobs):
                raise BallAnnotationServiceError(
                    "review_proxy_repair_ineligible",
                    "Blocked session detector lineage is invalid",
                    status_code=409,
                )
            sampling_manifest = session.get("sampling_manifest")
            groups = sampling_manifest.get("groups") if isinstance(sampling_manifest, dict) else None
            frame_indices = sampling_manifest.get("frame_indices") if isinstance(sampling_manifest, dict) else None
            if not isinstance(groups, list) or not groups or not isinstance(frame_indices, list) or not frame_indices:
                raise BallAnnotationServiceError(
                    "review_proxy_repair_ineligible",
                    "Blocked session sampling authority is incomplete",
                    status_code=409,
                )
            session_path = self._sessions_root / f"{session['session_id']}.json"
            _, session_record_sha256 = read_regular_bytes(
                session_path,
                "blocked annotation session",
                max_bytes=_MAX_SESSION_BYTES,
                trusted_root=self._sessions_root,
            )
            locked_profile = session.get("locked_profile")
            source = session.get("source")
            if not isinstance(locked_profile, dict) or not isinstance(source, dict):
                raise BallAnnotationServiceError(
                    "review_proxy_repair_ineligible",
                    "Blocked session source/profile authority is incomplete",
                    status_code=409,
                )
            return {
                "blocked_session_id": session["session_id"],
                "blocked_session_request_sha256": require_sha256(
                    session.get("request_sha256"), "blocked session request sha256"
                ),
                "blocked_session_record_sha256": session_record_sha256,
                "parent_probe_job_id": require_safe_id(jobs[-1], "repair parent probe job_id"),
                "development_probe_job_ids": deepcopy(jobs),
                "source": deepcopy(source),
                "locked_profile": deepcopy(locked_profile),
                "frame_indices": deepcopy(frame_indices),
                "sampling_manifest_sha256": require_sha256(
                    sampling_manifest.get("manifest_sha256"),
                    "repair sampling manifest sha256",
                ),
                "temporal_groups_sha256": canonical_sha256(groups),
                "replacement_request_authority_sha256": canonical_sha256(
                    {
                        "operator_id": session.get("operator_id"),
                        "sampling_profile_id": session.get("sampling_profile_id"),
                        "metric_profile_id": session.get("metric_profile_id"),
                        "strata_applicability": sampling_manifest.get("strata_applicability"),
                    }
                ),
            }

    def create_review_proxy_replacement_session(
        self, blocked_session_id: str, child_probe_job_id: str
    ) -> dict[str, Any]:
        """Create the sole server-derived retry after a verified proxy child."""

        blocked_session_id = require_safe_id(blocked_session_id, "blocked annotation session_id")
        child_probe_job_id = require_safe_id(child_probe_job_id, "review-proxy child probe job_id")
        with self._coordination_lock, self._lock:
            self._require_open()
            previous = self._load_session(blocked_session_id)
            request = self._review_proxy_replacement_request(previous, child_probe_job_id)
            request_sha256 = canonical_sha256(request)
            expected_lineage = [
                *previous["lineage"]["development_probe_job_ids"],
                child_probe_job_id,
            ]
            matching: list[dict[str, Any]] = []
            conflicting = False
            for path in sorted(self._sessions_root.glob("*.json")):
                candidate = self._load_session(path.stem)
                if candidate.get("retry_from_session_id") != blocked_session_id:
                    if candidate.get("request_sha256") == request_sha256:
                        conflicting = True
                    continue
                lineage = candidate.get("lineage")
                retry_lineage = candidate.get("retry_lineage")
                jobs = lineage.get("development_probe_job_ids") if isinstance(lineage, dict) else None
                if (
                    isinstance(retry_lineage, dict)
                    and retry_lineage.get("mode") == "review_proxy_decode_upgrade"
                    and jobs == expected_lineage
                    and candidate.get("request_sha256") == request_sha256
                    and candidate.get("idempotency_key") == request_sha256
                    and candidate.get("_normalized_session_request") == request
                ):
                    matching.append(candidate)
                else:
                    conflicting = True
            if len(matching) > 1 or conflicting:
                raise BallAnnotationServiceError(
                    "review_proxy_repair_ineligible",
                    "The blocked session already has a different replacement attempt",
                    status_code=409,
                )
            if matching:
                candidate = matching[0]
                registry = self._read_registry()
                if any(entry.get("session_id") == blocked_session_id for entry in registry["entries"]):
                    raise BallAnnotationServiceError(
                        "review_proxy_repair_ineligible",
                        "Review-proxy repair requires an unrevealed pristine blocked session",
                        status_code=409,
                    )
                self._require_exact_review_proxy_replacement_creation(
                    candidate,
                    previous,
                    child_probe_job_id,
                )
                self._recover_development_group_publications()
                return self._public_session(self._reconcile_session(candidate))
            self.get_review_proxy_repair_authority(blocked_session_id)
            return self.create_session(self._review_proxy_replacement_public_request(request))

    @staticmethod
    def _review_proxy_replacement_request(previous: dict[str, Any], child_probe_job_id: str) -> dict[str, Any]:
        lineage = previous.get("lineage")
        jobs = lineage.get("development_probe_job_ids") if isinstance(lineage, dict) else None
        locked_profile = previous.get("locked_profile")
        sampling_manifest = previous.get("sampling_manifest")
        applicability = sampling_manifest.get("strata_applicability") if isinstance(sampling_manifest, dict) else None
        scale_rows = applicability.get("scale") if isinstance(applicability, dict) else None
        lighting_rows = applicability.get("lighting") if isinstance(applicability, dict) else None
        if (
            not isinstance(jobs, list)
            or not jobs
            or len(jobs) != len(set(jobs))
            or child_probe_job_id in jobs
            or not isinstance(locked_profile, dict)
            or not isinstance(scale_rows, list)
            or not isinstance(lighting_rows, list)
            or not all(isinstance(row, dict) for row in [*scale_rows, *lighting_rows])
        ):
            raise BallAnnotationServiceError(
                "review_proxy_repair_ineligible",
                "Blocked session replacement request authority is invalid",
                status_code=409,
            )
        return {
            "data_role": "development",
            "development_probe_job_ids": sorted([*jobs, child_probe_job_id]),
            "locked_profile_id": require_safe_id(locked_profile.get("profile_id"), "locked profile_id"),
            "target_frame_count": None,
            "sampling_profile_id": require_safe_id(
                previous.get("sampling_profile_id"),
                "sampling profile_id",
            ),
            "metric_profile_id": require_safe_id(previous.get("metric_profile_id"), "metric profile_id"),
            "operator_id": require_safe_id(previous.get("operator_id"), "operator_id"),
            "strata_applicability": deepcopy(applicability),
            "applicable_scale_strata": [row.get("stratum") for row in scale_rows if row.get("status") == "applicable"],
            "applicable_lighting_strata": [
                row.get("stratum") for row in lighting_rows if row.get("status") == "applicable"
            ],
            "retry_from_session_id": require_safe_id(previous.get("session_id"), "retry_from_session_id"),
            "development_package_session_id": None,
            "development_package_sha256": None,
        }

    def _derive_review_proxy_replacement_creation_authority(
        self,
        candidate: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            authority = self._resolve_development_authority(
                request["development_probe_job_ids"],
                request["locked_profile_id"],
            )
            expected = self._create_retry_session(
                session_id=require_safe_id(candidate.get("session_id"), "replacement session_id"),
                request_sha256=canonical_sha256(request),
                request=request,
                authority=authority,
                development_package_binding=None,
                now=candidate.get("created_at"),
            )
            return self._review_proxy_session_creation_authority(expected)
        except (BallAnnotationServiceError, DetectorDevelopmentError, KeyError, TypeError) as exc:
            raise BallAnnotationServiceError(
                "replacement_session_mismatch",
                "Replacement session creation authority cannot be re-derived",
                status_code=409,
            ) from exc

    def _require_exact_review_proxy_replacement_creation(
        self,
        candidate: dict[str, Any],
        previous: dict[str, Any],
        child_probe_job_id: str,
    ) -> None:
        request = self._review_proxy_replacement_request(previous, child_probe_job_id)
        previous_lineage = previous.get("lineage")
        candidate_lineage = candidate.get("lineage")
        previous_jobs = (
            previous_lineage.get("development_probe_job_ids") if isinstance(previous_lineage, dict) else None
        )
        candidate_jobs = (
            candidate_lineage.get("development_probe_job_ids") if isinstance(candidate_lineage, dict) else None
        )
        request_sha256 = canonical_sha256(request)
        if (
            not self._review_proxy_repair_parent_is_pristine(previous)
            or not self._review_proxy_session_is_pristine_creation(candidate)
            or not isinstance(previous_jobs, list)
            or candidate_jobs != [*previous_jobs, child_probe_job_id]
            or candidate.get("retry_from_session_id") != previous.get("session_id")
            or candidate.get("request_sha256") != request_sha256
            or candidate.get("idempotency_key") != request_sha256
            or candidate.get("_normalized_session_request") != request
        ):
            raise BallAnnotationServiceError(
                "replacement_session_mismatch",
                "Replacement session does not match its exact blocked parent and requested child",
                status_code=409,
            )
        observed_creation_authority = self._review_proxy_session_creation_authority(candidate)
        expected_creation_authority = self._derive_review_proxy_replacement_creation_authority(
            candidate,
            request,
        )
        if observed_creation_authority != expected_creation_authority:
            raise BallAnnotationServiceError(
                "replacement_session_mismatch",
                "Replacement session does not match the exact server-derived creation authority",
                status_code=409,
            )

    @staticmethod
    def _review_proxy_replacement_public_request(request: dict[str, Any]) -> dict[str, Any]:
        applicability = request.get("strata_applicability")
        if not isinstance(applicability, dict):
            raise BallAnnotationServiceError(
                "review_proxy_repair_ineligible",
                "Blocked session strata authority is invalid",
                status_code=409,
            )
        public_applicability: dict[str, list[dict[str, Any]]] = {"scale": [], "lighting": []}
        for dimension in ("scale", "lighting"):
            rows = applicability.get(dimension)
            if not isinstance(rows, list):
                raise BallAnnotationServiceError(
                    "review_proxy_repair_ineligible",
                    "Blocked session strata authority is invalid",
                    status_code=409,
                )
            for row in rows:
                evidence = row.get("evidence") if isinstance(row, dict) else None
                if not isinstance(row, dict) or not isinstance(evidence, dict):
                    raise BallAnnotationServiceError(
                        "review_proxy_repair_ineligible",
                        "Blocked session strata authority is invalid",
                        status_code=409,
                    )
                public_row = {
                    "stratum": row.get("stratum"),
                    "status": row.get("status"),
                    "evidence_note": evidence.get("note"),
                }
                if dimension == "lighting":
                    public_row.update(
                        {
                            "quota": row.get("quota"),
                            "frame_intervals": deepcopy(row.get("frame_intervals")),
                        }
                    )
                public_applicability[dimension].append(public_row)
        return {
            key: deepcopy(value)
            for key, value in request.items()
            if key not in {"applicable_scale_strata", "applicable_lighting_strata", "strata_applicability"}
        } | {"strata_applicability": public_applicability}

    def inspect_review_proxy_replacement_side_effect(
        self,
        blocked_session_id: str,
        *,
        child_probe_job_id: str,
        expected_development_probe_job_ids: list[str],
        blocked_session_record_sha256: str,
        expected_group_commit: dict[str, Any] | None = None,
        replacement_session_witnessed: bool = False,
    ) -> dict[str, Any] | None:
        """Read the exact durable replacement prefix without repairing it."""

        try:
            blocked_session_id = require_safe_id(blocked_session_id, "blocked annotation session_id")
            child_probe_job_id = require_safe_id(child_probe_job_id, "review-proxy child probe job_id")
            blocked_session_record_sha256 = require_sha256(
                blocked_session_record_sha256,
                "blocked annotation session record sha256",
            )
            if not isinstance(expected_development_probe_job_ids, list) or not expected_development_probe_job_ids:
                raise DetectorDevelopmentError("invalid_probe_jobs", "Development probe lineage is missing")
            expected_parent_jobs = [
                require_safe_id(job_id, "development probe job_id") for job_id in expected_development_probe_job_ids
            ]
            if len(expected_parent_jobs) != len(set(expected_parent_jobs)):
                raise DetectorDevelopmentError("invalid_probe_jobs", "Development probe lineage is duplicated")
            if type(replacement_session_witnessed) is not bool:
                raise DetectorDevelopmentError(
                    "invalid_replacement_witness",
                    "Replacement session witness flag is invalid",
                )
        except DetectorDevelopmentError as exc:
            raise BallAnnotationServiceError(
                "replacement_session_mismatch",
                "Review-proxy replacement inspection authority is invalid",
                status_code=409,
            ) from exc

        with self._coordination_lock, self._lock:
            self._require_open()
            try:
                blocked = self._load_session(blocked_session_id)
                blocked_path = self._sessions_root / f"{blocked_session_id}.json"
                _blocked_bytes, observed_blocked_sha256 = read_regular_bytes(
                    blocked_path,
                    "blocked annotation session",
                    max_bytes=_MAX_SESSION_BYTES,
                    trusted_root=self._sessions_root,
                )
                candidates = [
                    self._load_session(path.stem)
                    for path in sorted(self._sessions_root.glob("*.json"))
                    if path.stem != blocked_session_id
                ]
                registry = self._read_registry()
            except (DetectorDevelopmentError, OSError) as exc:
                raise BallAnnotationServiceError(
                    "replacement_session_mismatch",
                    "Review-proxy replacement lower authority cannot be verified",
                    status_code=409,
                ) from exc

            blocked_lineage = blocked.get("lineage")
            if (
                observed_blocked_sha256 != blocked_session_record_sha256
                or blocked.get("data_role") != "development"
                or blocked.get("status") != "blocked"
                or blocked.get("stage") != "blocked"
                or blocked.get("error_code") != "invalid_source_timing"
                or blocked.get("blocker_code") != "review_proxy_required"
                or blocked.get("frames") != []
                or blocked.get("revisions") != []
                or blocked.get("final_package") is not None
                or blocked.get("final_result") is not None
                or not isinstance(blocked_lineage, dict)
                or blocked_lineage.get("development_probe_job_ids") != expected_parent_jobs
            ):
                raise BallAnnotationServiceError(
                    "historical_parent_changed",
                    "Original blocked annotation session changed during repair",
                    status_code=409,
                )

            replacements = [
                candidate for candidate in candidates if candidate.get("retry_from_session_id") == blocked_session_id
            ]
            if not replacements:
                return None
            if len(replacements) != 1:
                raise BallAnnotationServiceError(
                    "replacement_session_mismatch",
                    "Blocked annotation session has multiple replacement attempts",
                    status_code=409,
                )

            session = replacements[0]
            lineage = session.get("lineage")
            expected_jobs = [*expected_parent_jobs, child_probe_job_id]
            retry_lineage = session.get("retry_lineage")
            sampling_manifest = blocked.get("sampling_manifest")
            if not isinstance(lineage, dict) or not isinstance(sampling_manifest, dict):
                raise BallAnnotationServiceError(
                    "replacement_session_mismatch",
                    "Review-proxy replacement lineage is invalid",
                    status_code=409,
                )
            expected_retry_lineage = {
                "mode": "review_proxy_decode_upgrade",
                "previous_session_id": blocked_session_id,
                "previous_error_code": blocked.get("error_code"),
                "previous_blocker_code": blocked.get("blocker_code"),
                "previous_lineage_sha256": canonical_sha256(blocked_lineage),
                "current_lineage_sha256": canonical_sha256(lineage),
                "sampling_manifest_sha256": sampling_manifest.get("manifest_sha256"),
            }
            expected_request = {
                "data_role": "development",
                "development_probe_job_ids": sorted(expected_jobs),
                "locked_profile_id": blocked.get("locked_profile", {}).get("profile_id"),
                "target_frame_count": None,
                "sampling_profile_id": blocked.get("sampling_profile_id"),
                "metric_profile_id": blocked.get("metric_profile_id"),
                "operator_id": blocked.get("operator_id"),
                "strata_applicability": deepcopy(sampling_manifest.get("strata_applicability")),
                "applicable_scale_strata": deepcopy(blocked.get("applicable_scale_strata")),
                "applicable_lighting_strata": deepcopy(blocked.get("applicable_lighting_strata")),
                "retry_from_session_id": blocked_session_id,
                "development_package_session_id": None,
                "development_package_sha256": None,
            }
            expected_request_sha256 = canonical_sha256(expected_request)
            preserved_fields = (
                "source",
                "locked_profile",
                "control_profile_id",
                "control_profile",
                "sampling_profile_id",
                "metric_profile_id",
                "metric_profile_sha256",
                "sampling_manifest",
                "operator_id",
                "applicable_scale_strata",
                "applicable_lighting_strata",
                "development_package_binding",
            )
            lifecycle_valid = self._review_proxy_live_lifecycle_is_valid(session)
            if (
                session.get("data_role") != "development"
                or not lifecycle_valid
                or lineage.get("development_probe_job_ids") != expected_jobs
                or retry_lineage != expected_retry_lineage
                or session.get("request_sha256") != expected_request_sha256
                or session.get("idempotency_key") != expected_request_sha256
                or session.get("_normalized_session_request") != expected_request
                or any(session.get(field) != blocked.get(field) for field in preserved_fields)
                or not isinstance(session.get("frames"), list)
                or not session["frames"]
                or session.get("check_probe_job_id") is not None
                or session.get("check_probe_authority") is not None
                or session.get("error_code") is not None
                or session.get("blocker_code") is not None
            ):
                raise BallAnnotationServiceError(
                    "replacement_session_mismatch",
                    "Review-proxy replacement session authority changed",
                    status_code=409,
                )

            try:
                session_creation_authority_sha256 = canonical_sha256(
                    self._review_proxy_session_creation_authority(session)
                )
            except (BallAnnotationServiceError, DetectorDevelopmentError) as exc:
                raise BallAnnotationServiceError(
                    "replacement_session_mismatch",
                    "Review-proxy replacement creation authority is invalid",
                    status_code=409,
                ) from exc
            creation_pristine = self._review_proxy_session_is_pristine_creation(session)

            session_id = require_safe_id(session.get("session_id"), "replacement annotation session_id")
            rows = sorted(
                (deepcopy(entry) for entry in registry["entries"] if entry.get("session_id") == session_id),
                key=self._registry_entry_sort_key,
            )
            if not rows:
                if expected_group_commit is not None or (not creation_pristine and not replacement_session_witnessed):
                    raise BallAnnotationServiceError(
                        "replacement_session_mismatch",
                        "Advanced replacement session has no frozen upper witness",
                        status_code=409,
                    )
                return {"session": deepcopy(session), "group_commit": None}

            expected_groups = sampling_manifest.get("groups")
            if not isinstance(expected_groups, list) or not expected_groups:
                raise BallAnnotationServiceError(
                    "group_registry_mismatch",
                    "Replacement sampling groups are invalid",
                    status_code=409,
                )
            expected_by_id = {group["group_id"]: group for group in expected_groups}
            if (
                len(rows) != len(expected_by_id)
                or {row.get("group_id") for row in rows} != set(expected_by_id)
                or any(
                    row.get("source_sha256") != blocked.get("source", {}).get("sha256")
                    or row.get("data_role") != "development"
                    or row.get("state") != "revealed"
                    or row.get("retired_for_all_profiles") is not True
                    or self._registry_group(row) != expected_by_id[row["group_id"]]
                    for row in rows
                )
            ):
                raise BallAnnotationServiceError(
                    "group_registry_mismatch",
                    "Replacement temporal-group publication is incomplete",
                    status_code=409,
                )

            group_publication_sha256 = canonical_sha256(rows)
            if expected_group_commit is not None:
                expected_fields = {
                    "session_id",
                    "blocked_session_id",
                    "child_probe_job_id",
                    "session_record_sha256",
                    "session_creation_authority_sha256",
                    "group_publication_sha256",
                    "commit_sha256",
                }
                expected_body = (
                    {key: expected_group_commit.get(key) for key in expected_fields - {"commit_sha256"}}
                    if isinstance(expected_group_commit, dict)
                    else {}
                )
                try:
                    witness_valid = bool(
                        isinstance(expected_group_commit, dict)
                        and set(expected_group_commit) == expected_fields
                        and expected_group_commit.get("session_id") == session_id
                        and expected_group_commit.get("blocked_session_id") == blocked_session_id
                        and expected_group_commit.get("child_probe_job_id") == child_probe_job_id
                        and require_sha256(
                            expected_group_commit.get("session_record_sha256"),
                            "replacement session record sha256",
                        )
                        == expected_group_commit["session_record_sha256"]
                        and require_sha256(
                            expected_group_commit.get("session_creation_authority_sha256"),
                            "replacement session creation authority sha256",
                        )
                        == session_creation_authority_sha256
                        and require_sha256(
                            expected_group_commit.get("group_publication_sha256"),
                            "replacement group publication sha256",
                        )
                        == group_publication_sha256
                        and require_sha256(
                            expected_group_commit.get("commit_sha256"),
                            "replacement group commit sha256",
                        )
                        == canonical_sha256(expected_body)
                    )
                except DetectorDevelopmentError:
                    witness_valid = False
                if not witness_valid:
                    raise BallAnnotationServiceError(
                        "group_registry_mismatch",
                        "Replacement temporal-group witness changed",
                        status_code=409,
                    )
                return {
                    "session": deepcopy(session),
                    "group_commit": deepcopy(expected_group_commit),
                }

            if not creation_pristine:
                raise BallAnnotationServiceError(
                    "replacement_session_mismatch",
                    "Advanced replacement session cannot synthesize an upper group witness",
                    status_code=409,
                )
            session_path = self._sessions_root / f"{session_id}.json"
            _session_bytes, session_record_sha256 = read_regular_bytes(
                session_path,
                "replacement annotation session",
                max_bytes=_MAX_SESSION_BYTES,
                trusted_root=self._sessions_root,
            )
            commit = {
                "session_id": session_id,
                "blocked_session_id": blocked_session_id,
                "child_probe_job_id": child_probe_job_id,
                "session_record_sha256": session_record_sha256,
                "session_creation_authority_sha256": session_creation_authority_sha256,
                "group_publication_sha256": group_publication_sha256,
            }
            return {
                "session": deepcopy(session),
                "group_commit": {
                    **commit,
                    "commit_sha256": canonical_sha256(commit),
                },
            }

    def get_review_proxy_replacement_commit(
        self,
        session_id: str,
        *,
        blocked_session_id: str,
        child_probe_job_id: str,
    ) -> dict[str, Any]:
        """Verify the replacement row and its revealed-group publication."""

        session_id = require_safe_id(session_id, "replacement session_id")
        blocked_session_id = require_safe_id(blocked_session_id, "blocked annotation session_id")
        child_probe_job_id = require_safe_id(child_probe_job_id, "review-proxy child probe job_id")
        with self._coordination_lock, self._lock:
            session = self._load_session(session_id)
            if not self._review_proxy_session_is_pristine_creation(session):
                raise BallAnnotationServiceError(
                    "replacement_session_mismatch",
                    "Advanced replacement session cannot synthesize an upper group witness",
                    status_code=409,
                )
            self._recover_development_group_publications()
            session = self._load_session(session_id)
            retry_lineage = session.get("retry_lineage")
            lineage = session.get("lineage")
            jobs = lineage.get("development_probe_job_ids") if isinstance(lineage, dict) else None
            if (
                not self._review_proxy_session_is_pristine_creation(session)
                or session.get("retry_from_session_id") != blocked_session_id
                or not isinstance(retry_lineage, dict)
                or retry_lineage.get("mode") != "review_proxy_decode_upgrade"
                or not isinstance(jobs, list)
                or len(jobs) < 2
                or jobs[-1] != child_probe_job_id
            ):
                raise BallAnnotationServiceError(
                    "replacement_session_mismatch",
                    "Review-proxy replacement session authority changed",
                )
            expected_groups = session.get("sampling_manifest", {}).get("groups")
            registry = self._read_registry()
            rows = sorted(
                (entry for entry in registry["entries"] if entry.get("session_id") == session_id),
                key=self._registry_entry_sort_key,
            )
            expected_by_id = {group["group_id"]: group for group in expected_groups}
            if (
                len(rows) != len(expected_by_id)
                or {row.get("group_id") for row in rows} != set(expected_by_id)
                or any(
                    row.get("source_sha256") != session["source"]["sha256"]
                    or row.get("data_role") != "development"
                    or row.get("state") != "revealed"
                    or row.get("retired_for_all_profiles") is not True
                    or self._registry_group(row) != expected_by_id[row["group_id"]]
                    for row in rows
                )
            ):
                raise BallAnnotationServiceError(
                    "group_registry_mismatch",
                    "Replacement temporal-group publication is incomplete",
                )
            session_path = self._sessions_root / f"{session_id}.json"
            _bytes, session_record_sha256 = read_regular_bytes(
                session_path,
                "replacement annotation session",
                max_bytes=_MAX_SESSION_BYTES,
                trusted_root=self._sessions_root,
            )
            commit = {
                "session_id": session_id,
                "blocked_session_id": blocked_session_id,
                "child_probe_job_id": child_probe_job_id,
                "session_record_sha256": session_record_sha256,
                "session_creation_authority_sha256": canonical_sha256(
                    self._review_proxy_session_creation_authority(session)
                ),
                "group_publication_sha256": canonical_sha256(rows),
            }
            return {
                "session": self._public_session(session),
                **commit,
                "commit_sha256": canonical_sha256(commit),
            }

    def verify_blocked_review_proxy_parent_immutable(self, session_id: str, expected_record_sha256: str) -> str:
        """Re-hash the original blocker without re-running eligibility rules."""

        session_id = require_safe_id(session_id, "blocked annotation session_id")
        expected_record_sha256 = require_sha256(expected_record_sha256, "blocked session record sha256")
        with self._coordination_lock, self._lock:
            session = self._load_session(session_id)
            path = self._sessions_root / f"{session_id}.json"
            _bytes, observed = read_regular_bytes(
                path,
                "blocked annotation session",
                max_bytes=_MAX_SESSION_BYTES,
                trusted_root=self._sessions_root,
            )
            if (
                observed != expected_record_sha256
                or session.get("status") != "blocked"
                or session.get("blocker_code") != "review_proxy_required"
                or session.get("frames") != []
                or session.get("revisions") != []
                or session.get("final_package") is not None
                or session.get("final_result") is not None
            ):
                raise BallAnnotationServiceError(
                    "historical_parent_changed",
                    "Original blocked annotation session changed during repair",
                )
            return observed

    def verify_ready_review_proxy_replacement(
        self,
        *,
        blocked_session_id: str,
        blocked_session_record_sha256: str,
        replacement_session_id: str,
        child_probe_job_id: str,
        session_creation_authority_sha256: str,
        group_publication_sha256: str,
    ) -> dict[str, Any]:
        """Read-only replay of immutable replacement creation authority."""

        blocked_session_id = require_safe_id(blocked_session_id, "blocked annotation session_id")
        replacement_session_id = require_safe_id(replacement_session_id, "replacement annotation session_id")
        child_probe_job_id = require_safe_id(child_probe_job_id, "review-proxy child probe job_id")
        blocked_session_record_sha256 = require_sha256(blocked_session_record_sha256, "blocked session record sha256")
        session_creation_authority_sha256 = require_sha256(
            session_creation_authority_sha256,
            "replacement session creation authority sha256",
        )
        group_publication_sha256 = require_sha256(
            group_publication_sha256,
            "replacement group publication sha256",
        )
        with self._coordination_lock, self._lock:
            self._require_open()
            blocked = self._load_session(blocked_session_id)
            blocked_path = self._sessions_root / f"{blocked_session_id}.json"
            _blocked_bytes, observed_blocked_sha256 = read_regular_bytes(
                blocked_path,
                "blocked annotation session",
                max_bytes=_MAX_SESSION_BYTES,
                trusted_root=self._sessions_root,
            )
            if (
                observed_blocked_sha256 != blocked_session_record_sha256
                or blocked.get("status") != "blocked"
                or blocked.get("blocker_code") != "review_proxy_required"
                or blocked.get("frames") != []
                or blocked.get("revisions") != []
                or blocked.get("final_package") is not None
                or blocked.get("final_result") is not None
            ):
                raise BallAnnotationServiceError(
                    "historical_parent_changed",
                    "Original blocked annotation session changed during repair",
                )

            replacements: list[dict[str, Any]] = []
            for path in sorted(self._sessions_root.glob("*.json")):
                if path.name == f"{blocked_session_id}.json":
                    continue
                candidate = self._read_json(path, "annotation session", _MAX_SESSION_BYTES)
                if candidate.get("retry_from_session_id") == blocked_session_id:
                    replacements.append(candidate)
            if len(replacements) != 1 or replacements[0].get("session_id") != replacement_session_id:
                raise BallAnnotationServiceError(
                    "replacement_session_mismatch",
                    "Ready repair does not have exactly its frozen replacement",
                )
            session = replacements[0]
            retry_lineage = session.get("retry_lineage")
            lineage = session.get("lineage")
            jobs = lineage.get("development_probe_job_ids") if isinstance(lineage, dict) else None
            if (
                not self._review_proxy_live_lifecycle_is_valid(session)
                or not isinstance(retry_lineage, dict)
                or retry_lineage.get("mode") != "review_proxy_decode_upgrade"
                or not isinstance(jobs, list)
                or len(jobs) < 2
                or jobs[-1] != child_probe_job_id
                or canonical_sha256(self._review_proxy_session_creation_authority(session))
                != session_creation_authority_sha256
            ):
                raise BallAnnotationServiceError(
                    "replacement_session_mismatch",
                    "Replacement creation authority changed after publication",
                )

            expected_groups = session.get("sampling_manifest", {}).get("groups")
            if not isinstance(expected_groups, list):
                raise BallAnnotationServiceError(
                    "group_registry_mismatch",
                    "Replacement sampling groups are invalid",
                )
            registry = self._read_registry()
            rows = sorted(
                (entry for entry in registry["entries"] if entry.get("session_id") == replacement_session_id),
                key=self._registry_entry_sort_key,
            )
            expected_by_id = {group["group_id"]: group for group in expected_groups}
            if (
                len(rows) != len(expected_by_id)
                or {row.get("group_id") for row in rows} != set(expected_by_id)
                or any(
                    row.get("source_sha256") != session["source"]["sha256"]
                    or row.get("data_role") != "development"
                    or row.get("state") != "revealed"
                    or row.get("retired_for_all_profiles") is not True
                    or self._registry_group(row) != expected_by_id[row["group_id"]]
                    for row in rows
                )
                or canonical_sha256(rows) != group_publication_sha256
            ):
                raise BallAnnotationServiceError(
                    "group_registry_mismatch",
                    "Replacement temporal-group publication changed",
                )
            return {
                "session": self._public_session(session),
                "blocked_authority": self._review_proxy_repair_authority_from_session(blocked, observed_blocked_sha256),
                "session_creation_authority_sha256": (session_creation_authority_sha256),
                "group_publication_sha256": group_publication_sha256,
            }

    @staticmethod
    def _review_proxy_live_lifecycle_is_valid(session: dict[str, Any]) -> bool:
        return (session.get("status"), session.get("stage")) in {
            ("annotating", "annotating"),
            ("finalizing", "finalizing"),
            ("finalized", "finalized"),
        }

    @staticmethod
    def _review_proxy_repair_parent_is_pristine(session: dict[str, Any]) -> bool:
        return bool(
            session.get("data_role") == "development"
            and session.get("status") == "blocked"
            and session.get("stage") == "blocked"
            and session.get("error_code") == "invalid_source_timing"
            and session.get("blocker_code") == "review_proxy_required"
            and session.get("frames") == []
            and session.get("revisions") == []
            and session.get("final_package") is None
            and session.get("final_result") is None
            and session.get("retry_from_session_id") is None
            and session.get("retry_lineage") is None
            and session.get("check_probe_job_id") is None
            and session.get("check_probe_authority") is None
        )

    @classmethod
    def _review_proxy_session_is_pristine_creation(cls, session: dict[str, Any]) -> bool:
        frames = session.get("frames")
        return bool(
            (session.get("status"), session.get("stage")) == ("annotating", "annotating")
            and session.get("revisions") == []
            and session.get("final_package") is None
            and session.get("final_result") is None
            and session.get("finalize_mutation_id") is None
            and session.get("finalization_started_at") is None
            and session.get("finalization_input_sha256") is None
            and isinstance(frames, list)
            and frames
            and all(
                frame.get("annotation_revision") == 0
                and frame.get("current_annotation") is None
                and frame.get("propagation_job_ids") == []
                and frame.get("propagation_suggestions") == []
                for frame in frames
                if isinstance(frame, dict)
            )
            and all(isinstance(frame, dict) for frame in frames)
        )

    @classmethod
    def _review_proxy_session_creation_authority(cls, session: dict[str, Any]) -> dict[str, Any]:
        cls._session_request_authority(session)
        try:
            expected_attempt_family_sha256 = cls._attempt_family_sha256(session)
        except (BallAnnotationServiceError, DetectorDevelopmentError, KeyError, TypeError) as exc:
            raise BallAnnotationServiceError(
                "replacement_session_mismatch",
                "Replacement attempt-family authority is invalid",
            ) from exc
        if session.get("attempt_family_sha256") != expected_attempt_family_sha256:
            raise BallAnnotationServiceError(
                "replacement_session_mismatch",
                "Replacement attempt-family authority changed after creation",
            )
        authority = {
            key: deepcopy(session.get(key))
            for key in (
                "schema_version",
                "artifact_type",
                "session_id",
                "request_sha256",
                "data_role",
                "retry_from_session_id",
                "retry_lineage",
                "attempt_family_sha256",
                "source",
                "lineage",
                "locked_profile",
                "control_profile_id",
                "control_profile",
                "sampling_profile_id",
                "metric_profile_id",
                "metric_profile_sha256",
                "sampling_manifest",
                "operator_id",
                "created_at",
            )
        }
        authority["_normalized_session_request"] = deepcopy(session.get("_normalized_session_request"))
        authority["frame_review_proxy_authority"] = deepcopy(session.get("_frame_review_proxy_authority"))
        authority["detector_probe_authorities"] = deepcopy(session.get("_detector_probe_authorities", []))
        authority["primary_frames"] = cls._review_proxy_primary_frames(session)
        return authority

    @staticmethod
    def _review_proxy_primary_frames(session: dict[str, Any]) -> list[dict[str, Any]]:
        sampling = session.get("sampling_manifest")
        expected_indices = sampling.get("frame_indices") if isinstance(sampling, dict) else None
        frames = session.get("frames")
        if (
            not isinstance(expected_indices, list)
            or not isinstance(frames, list)
            or (
                expected_indices != sorted(set(expected_indices))
                or any(
                    isinstance(frame_index, bool) or not isinstance(frame_index, int)
                    for frame_index in expected_indices
                )
            )
        ):
            raise BallAnnotationServiceError(
                "replacement_session_mismatch",
                "Replacement primary-frame creation authority is invalid",
            )
        seen_indices: set[int] = set()
        for frame in frames:
            if not isinstance(frame, dict):
                raise BallAnnotationServiceError(
                    "replacement_session_mismatch",
                    "Replacement frame creation authority is invalid",
                )
            frame_index = frame.get("frame_index")
            primary_sample = frame.get("primary_sample")
            frame_role = frame.get("frame_role")
            if (
                isinstance(frame_index, bool)
                or not isinstance(frame_index, int)
                or frame_index in seen_indices
                or (primary_sample is True and frame_role != "primary_sample")
                or (primary_sample is False and frame_role != "propagation_target")
                or type(primary_sample) is not bool
            ):
                raise BallAnnotationServiceError(
                    "replacement_session_mismatch",
                    "Replacement frame roles or identities changed after creation",
                )
            seen_indices.add(frame_index)
        primary = sorted(
            (frame for frame in frames if isinstance(frame, dict) and frame.get("primary_sample") is True),
            key=lambda frame: frame.get("frame_index", -1),
        )
        if (
            [frame.get("frame_index") for frame in primary] != expected_indices
            or any(
                frame.get("frame_index") in set(expected_indices)
                for frame in frames
                if frame.get("primary_sample") is False
            )
            or any(frame.get("frame_role") != "primary_sample" for frame in primary)
        ):
            raise BallAnnotationServiceError(
                "replacement_session_mismatch",
                "Replacement primary-frame set changed after creation",
            )
        immutable_fields = (
            "frame_index",
            "source_frame_sha256",
            "source_frame_size_bytes",
            "suggested_candidates",
            "_probe_job_id",
            "_artifact_id",
            "_probe_report_sha256",
            "_probe_result_manifest_sha256",
            "_candidate_probe_job_id",
            "_candidate_probe_report_sha256",
            "_candidate_probe_result_manifest_sha256",
            "_candidate_evidence_sha256",
            "_candidate_artifact_id",
            "_runtime_environment_sha256",
            "_requested_decode_mode",
            "_effective_decode_mode",
            "_decoded_frame_position",
            "_position_verification",
            "_decoder_reported_pos_msec",
            "_decoder_timing_observation_method",
            "_true_presentation_timestamp",
            "_proxy_binding",
            "_locked_evidence_sha256",
            "source_timing_status",
            "decoder_reported_pos_msec",
            "decoder_time_seconds",
            "display_time_seconds",
            "true_presentation_timestamp",
            "proxy_binding",
            "temporal_group_id",
            "frame_url",
            "frame_role",
            "primary_sample",
        )
        return [{key: deepcopy(frame.get(key)) for key in immutable_fields} for frame in primary]

    @staticmethod
    def _review_proxy_repair_authority_from_session(
        session: dict[str, Any], session_record_sha256: str
    ) -> dict[str, Any]:
        lineage = session["lineage"]
        jobs = lineage["development_probe_job_ids"]
        sampling_manifest = session["sampling_manifest"]
        groups = sampling_manifest["groups"]
        return {
            "blocked_session_id": session["session_id"],
            "blocked_session_request_sha256": require_sha256(
                session.get("request_sha256"),
                "blocked session request sha256",
            ),
            "blocked_session_record_sha256": session_record_sha256,
            "parent_probe_job_id": require_safe_id(jobs[-1], "repair parent probe job_id"),
            "development_probe_job_ids": deepcopy(jobs),
            "source": deepcopy(session["source"]),
            "locked_profile": deepcopy(session["locked_profile"]),
            "frame_indices": deepcopy(sampling_manifest["frame_indices"]),
            "sampling_manifest_sha256": require_sha256(
                sampling_manifest.get("manifest_sha256"),
                "repair sampling manifest sha256",
            ),
            "temporal_groups_sha256": canonical_sha256(groups),
            "replacement_request_authority_sha256": canonical_sha256(
                {
                    "operator_id": session.get("operator_id"),
                    "sampling_profile_id": session.get("sampling_profile_id"),
                    "metric_profile_id": session.get("metric_profile_id"),
                    "strata_applicability": sampling_manifest.get("strata_applicability"),
                }
            ),
        }

    def read_frame(self, session_id: str, frame_index: int) -> tuple[bytes, str, str]:
        with self._coordination_lock, self._lock:
            session = self._reconcile_session(self._load_session(session_id))
            frame = self._frame(session, frame_index)
            if session["status"] == "finalized":
                result = self._read_final_result(session["session_id"])
                entry = next(
                    (item for item in result["package"]["frame_media"] if item["frame_index"] == frame_index),
                    None,
                )
                if not isinstance(entry, dict):
                    raise BallAnnotationServiceError(
                        "invalid_final_result",
                        "Final frame media binding is missing",
                    )
                content, digest = read_regular_bytes(
                    self._final_results_root / session["session_id"] / entry["relative_path"],
                    "sealed source frame JPEG",
                    max_bytes=_MAX_FRAME_BYTES,
                    trusted_root=(self._final_results_root / session["session_id"]),
                )
                return content, "image/jpeg", digest
            probe_job_id = frame.get("_probe_job_id")
            artifact_id = frame.get("_artifact_id")
            if not isinstance(probe_job_id, str) or not isinstance(artifact_id, str):
                raise BallAnnotationServiceError(
                    "frame_not_ready", "The exact source-bound frame is not ready", status_code=409
                )
            try:
                content, media_type, observed_digest = self._read_probe_artifact(probe_job_id, artifact_id)
            except (KeyError, OSError, DetectorDevelopmentError) as exc:
                raise BallAnnotationServiceError(
                    "frame_unavailable", "The exact source-bound frame is unavailable", status_code=409
                ) from exc
            expected_digest = require_sha256(frame.get("source_frame_sha256"), "source frame sha256")
            content_digest = hashlib.sha256(content).hexdigest()
            if observed_digest != expected_digest or content_digest != expected_digest:
                raise BallAnnotationServiceError(
                    "frame_digest_mismatch", "The exact frame content digest does not match its frozen binding"
                )
            if media_type != "image/jpeg" or not 0 < len(content) <= _MAX_FRAME_BYTES:
                raise BallAnnotationServiceError(
                    "invalid_frame_artifact", "The exact frame artifact type or size is invalid"
                )
            self._validate_jpeg(content, session["source"]["width"], session["source"]["height"])
            return content, media_type, content_digest

    def put_annotation(
        self,
        session_id: str,
        frame_index: int,
        request: dict[str, Any],
        *,
        if_match: str | None,
    ) -> dict[str, Any]:
        normalized = self._normalize_annotation_request(request)
        with self._coordination_lock, self._lock:
            session = self._reconcile_session(self._load_session(session_id))
            if session["status"] == "finalized":
                raise BallAnnotationServiceError(
                    "session_finalized", "The annotation session is finalized and immutable"
                )
            if session["status"] != "annotating":
                raise BallAnnotationServiceError(
                    "session_not_annotating", "The annotation session is not ready for annotation"
                )
            frame = self._frame(session, frame_index)
            if normalized["operation"] == "set":
                normalized["annotation"]["provenance"] = self._server_derived_annotation_provenance(normalized)
                try:
                    normalized["annotation"] = validate_ball_annotation(
                        normalized["annotation"],
                        width=session["source"]["width"],
                        height=session["source"]["height"],
                        data_role=session["data_role"],
                    )
                except BallAnnotationError as exc:
                    raise BallAnnotationServiceError("invalid_annotation", str(exc), status_code=400) from exc
            request_sha256 = canonical_sha256(
                {
                    "session_id": session["session_id"],
                    "frame_index": frame_index,
                    "request": normalized,
                }
            )
            for revision in session["revisions"]:
                if revision["mutation_id"] != normalized["mutation_id"]:
                    continue
                if revision["mutation_sha256"] != request_sha256:
                    raise BallAnnotationServiceError(
                        "mutation_conflict", "mutation_id was already used for different annotation content"
                    )
                return self._public_revision(revision)
            if if_match is None:
                raise BallAnnotationServiceError(
                    "precondition_required", "If-Match is required for annotation updates", status_code=428
                )
            current_etag = frame["annotation_etag"]
            if (
                self._parse_if_match(if_match) != current_etag
                or normalized["expected_revision"] != frame["annotation_revision"]
            ):
                raise BallAnnotationServiceError(
                    "precondition_failed", "The annotation revision or ETag is stale", status_code=412
                )
            previous = deepcopy(frame["current_annotation"])
            operation = normalized["operation"]
            accepted_suggestion_job_id: str | None = None
            accepted_suggestion_sha256: str | None = None
            dismissed_suggestion_job_id: str | None = None
            dismissed_suggestion_sha256: str | None = None
            if operation == "set":
                effective = deepcopy(normalized["annotation"])
                suggestion_id = normalized["suggestion_id"]
                if suggestion_id is not None:
                    suggestion_kind = normalized["suggestion_kind"]
                    if effective.get("annotation_state") != "confirmed":
                        raise BallAnnotationServiceError(
                            "suggestion_not_confirmed",
                            "Accepting a suggestion requires human-confirmed truth",
                            status_code=400,
                        )
                    if effective.get("presence") != "present" or (
                        effective.get("point_source_px") is None and effective.get("bbox_source_px") is None
                    ):
                        raise BallAnnotationServiceError(
                            "suggestion_not_localizable",
                            "Accepting a suggestion requires localizable present truth; absence/unknown must be a dismissal or ordinary revision",
                            status_code=400,
                        )
                    if suggestion_kind == "propagation":
                        suggestion = self._pending_propagation_suggestion(frame, suggestion_id)
                        expected_job_id = self._validated_propagation_job(session, frame, suggestion)["job_id"]
                        expected_sha256 = canonical_sha256(self._suggestion_authority_payload(suggestion))
                        self._require_client_suggestion_binding(
                            supplied_job_id=normalized["accepted_suggestion_job_id"],
                            supplied_sha256=normalized["accepted_suggestion_sha256"],
                            expected_job_id=expected_job_id,
                            expected_sha256=expected_sha256,
                        )
                    else:
                        candidate = self._pending_detector_candidate(session, frame, suggestion_id)
                        expected_job_id = frame["_candidate_probe_job_id"]
                        expected_sha256 = canonical_sha256(self._detector_candidate_authority_payload(candidate))
                        self._require_client_suggestion_binding(
                            supplied_job_id=normalized["accepted_suggestion_job_id"],
                            supplied_sha256=normalized["accepted_suggestion_sha256"],
                            expected_job_id=expected_job_id,
                            expected_sha256=expected_sha256,
                        )
                    accepted_suggestion_job_id = expected_job_id
                    accepted_suggestion_sha256 = expected_sha256
                dismissed_suggestion_id = normalized["dismissed_suggestion_id"]
                if dismissed_suggestion_id is not None:
                    dismissed_kind = normalized["dismissed_suggestion_kind"]
                    if dismissed_kind == "propagation":
                        dismissed_suggestion = self._pending_propagation_suggestion(frame, dismissed_suggestion_id)
                        expected_job_id = self._validated_propagation_job(session, frame, dismissed_suggestion)[
                            "job_id"
                        ]
                        expected_sha256 = canonical_sha256(self._suggestion_authority_payload(dismissed_suggestion))
                        self._require_client_suggestion_binding(
                            supplied_job_id=normalized["dismissed_suggestion_job_id"],
                            supplied_sha256=normalized["dismissed_suggestion_sha256"],
                            expected_job_id=expected_job_id,
                            expected_sha256=expected_sha256,
                        )
                    else:
                        dismissed_candidate = self._pending_detector_candidate(session, frame, dismissed_suggestion_id)
                        expected_job_id = frame["_candidate_probe_job_id"]
                        expected_sha256 = canonical_sha256(
                            self._detector_candidate_authority_payload(dismissed_candidate)
                        )
                        self._require_client_suggestion_binding(
                            supplied_job_id=normalized["dismissed_suggestion_job_id"],
                            supplied_sha256=normalized["dismissed_suggestion_sha256"],
                            expected_job_id=expected_job_id,
                            expected_sha256=expected_sha256,
                        )
                    dismissed_suggestion_job_id = expected_job_id
                    dismissed_suggestion_sha256 = expected_sha256
            elif operation == "delete":
                effective = None
            else:
                if normalized["undo_revision"] != frame["annotation_revision"]:
                    raise BallAnnotationServiceError(
                        "invalid_undo", "undo_revision must identify the current effective revision", status_code=400
                    )
                current_record = next(
                    (
                        item
                        for item in reversed(session["revisions"])
                        if item["frame_index"] == frame_index and item["revision"] == normalized["undo_revision"]
                    ),
                    None,
                )
                if current_record is None:
                    raise BallAnnotationServiceError(
                        "invalid_undo", "The requested revision cannot be undone", status_code=400
                    )
                effective = deepcopy(current_record["previous_effective_annotation"])

            revision_number = frame["annotation_revision"] + 1
            new_etag = annotation_etag(session_id, frame_index, revision_number, effective)
            revision = {
                "schema_version": "1.0",
                "artifact_type": "ball_annotation_revision",
                "revision_id": f"revision-{canonical_sha256({'session_id': session_id, 'frame_index': frame_index, 'revision': revision_number})[:24]}",
                "session_id": session_id,
                "frame_index": frame_index,
                "revision": revision_number,
                "operation": operation,
                "mutation_id": normalized["mutation_id"],
                "mutation_sha256": request_sha256,
                "expected_revision": normalized["expected_revision"],
                "supersedes_revision": frame["annotation_revision"] or None,
                "undo_revision": normalized["undo_revision"],
                "accepted_suggestion_kind": normalized["suggestion_kind"],
                "accepted_suggestion_id": normalized["suggestion_id"],
                "accepted_suggestion_job_id": accepted_suggestion_job_id,
                "accepted_suggestion_sha256": accepted_suggestion_sha256,
                "dismissed_suggestion_kind": normalized["dismissed_suggestion_kind"],
                "dismissed_suggestion_id": normalized["dismissed_suggestion_id"],
                "dismissed_suggestion_job_id": dismissed_suggestion_job_id,
                "dismissed_suggestion_sha256": dismissed_suggestion_sha256,
                "previous_effective_annotation": previous,
                "effective_annotation": effective,
                "operator_id": session["operator_id"],
                "annotation_etag": new_etag,
                "created_at": utc_now_iso(),
            }
            session["revisions"].append(revision)
            frame["annotation_revision"] = revision_number
            frame["annotation_etag"] = new_etag
            frame["current_annotation"] = effective
            session["updated_at"] = revision["created_at"]
            self._persist_session(session)
            if (
                normalized["suggestion_kind"] == "propagation"
                or normalized["dismissed_suggestion_kind"] == "propagation"
            ):
                self._hit_confirmation_failpoint("after_confirmation_intent")
                session = self._reconcile_propagation_confirmations(session)
            return self._public_revision(revision)

    def create_propagation_job(
        self,
        session_id: str,
        request: dict[str, Any],
        *,
        if_match: str | None,
    ) -> dict[str, Any]:
        normalized = self._normalize_propagation_request(request)
        with self._coordination_lock, self._lock:
            session = self._reconcile_session(self._load_session(session_id))
            if session["data_role"] != "development" or session["status"] != "annotating":
                raise BallAnnotationServiceError(
                    "propagation_not_allowed", "Propagation is limited to active development annotations"
                )
            seed = self._frame(session, normalized["seed_frame_index"])
            annotation = seed["current_annotation"]
            if not isinstance(annotation, dict) or annotation.get("annotation_state") != "confirmed":
                raise BallAnnotationServiceError(
                    "seed_not_confirmed", "Propagation requires a human-confirmed seed annotation"
                )
            if annotation.get("point_source_px") is None and annotation.get("bbox_source_px") is None:
                raise BallAnnotationServiceError(
                    "seed_not_localizable", "Propagation requires a localizable confirmed seed"
                )
            if if_match is None:
                raise BallAnnotationServiceError(
                    "precondition_required",
                    "If-Match is required for propagation seed binding",
                    status_code=428,
                )
            if (
                self._parse_if_match(if_match) != seed["annotation_etag"]
                or normalized["expected_seed_revision"] != seed["annotation_revision"]
            ):
                raise BallAnnotationServiceError(
                    "precondition_failed",
                    "The propagation seed revision or ETag is stale",
                    status_code=412,
                )
            group = next(
                item for item in session["sampling_manifest"]["groups"] if item["group_id"] == seed["temporal_group_id"]
            )
            target_start = max(
                0,
                group["start_frame"],
                seed["frame_index"] - normalized["radius_frames"],
            )
            target_end = min(
                session["source"]["frame_count"] - 1,
                group["end_frame"],
                seed["frame_index"] + normalized["radius_frames"],
            )
            target_frame_indices = [
                index for index in range(target_start, target_end + 1) if index != seed["frame_index"]
            ]
            if not target_frame_indices:
                raise BallAnnotationServiceError(
                    "propagation_window_empty", "No bounded adjacent source frames are available"
                )
            seed_binding = {
                "frame_index": seed["frame_index"],
                "annotation_revision": seed["annotation_revision"],
                "annotation_etag": seed["annotation_etag"],
                "annotation_sha256": canonical_sha256(annotation),
                "source_frame_sha256": seed["source_frame_sha256"],
                "temporal_group_id": seed["temporal_group_id"],
                "sampling_manifest_sha256": session["sampling_manifest"]["manifest_sha256"],
                "tracker_profile_sha256": TRACKER_PROFILE_SHA256,
            }
            intent = {
                "session_id": session_id,
                **normalized,
                "seed_binding": seed_binding,
                "target_frame_indices": target_frame_indices,
            }
            intent_sha256 = canonical_sha256(intent)
            semantic_intent_sha256 = canonical_sha256(
                {key: value for key, value in intent.items() if key != "mutation_id"}
            )
            existing_jobs: list[dict[str, Any]] = []
            for path in self._propagation_root.glob("*.json"):
                existing = self._read_json(path, "propagation job", _MAX_SESSION_BYTES)
                existing_jobs.append(existing)
                if (
                    existing.get("mutation_id") == normalized["mutation_id"]
                    and existing.get("intent_sha256") != intent_sha256
                ):
                    raise BallAnnotationServiceError(
                        "mutation_conflict",
                        "Propagation mutation_id was already used for different seed authority",
                    )
            for existing in existing_jobs:
                if existing.get("mutation_id") != normalized["mutation_id"]:
                    continue
                return self._public_propagation(self._reconcile_propagation_job(existing, session))
            for existing in existing_jobs:
                if existing.get("session_id") != session_id:
                    continue
                existing_semantic_sha256 = canonical_sha256(
                    {
                        "session_id": existing.get("session_id"),
                        "seed_frame_index": existing.get("seed_frame_index"),
                        "radius_frames": existing.get("radius_frames"),
                        "expected_seed_revision": existing.get("expected_seed_revision"),
                        "seed_binding": existing.get("seed_binding"),
                        "target_frame_indices": existing.get("target_frame_indices"),
                    }
                )
                stored_semantic_sha256 = existing.get("_semantic_intent_sha256")
                if stored_semantic_sha256 not in {
                    None,
                    existing_semantic_sha256,
                }:
                    raise BallAnnotationServiceError(
                        "invalid_propagation_job",
                        "Persisted propagation semantic authority changed",
                    )
                if existing_semantic_sha256 == semantic_intent_sha256:
                    return self._public_propagation(self._reconcile_propagation_job(existing, session))
                existing_targets = existing.get("target_frame_indices")
                if (
                    isinstance(existing_targets, list)
                    and set(existing_targets).intersection(target_frame_indices)
                    and self._propagation_reserves_targets(existing)
                ):
                    raise BallAnnotationServiceError(
                        "propagation_target_conflict",
                        "Propagation targets already have a different producer authority",
                        status_code=409,
                    )
            self._require_propagation_creation_capacity(
                session,
                target_frame_indices=target_frame_indices,
                existing_jobs=existing_jobs,
            )
            job_id = f"propagation-{intent_sha256[:16]}-{uuid.uuid4().hex[:12]}"
            now = utc_now_iso()
            job = {
                "schema_version": "1.0",
                "artifact_type": "ball_propagation_job",
                "job_id": job_id,
                "session_id": session_id,
                "intent_sha256": intent_sha256,
                "_semantic_intent_sha256": semantic_intent_sha256,
                "mutation_id": normalized["mutation_id"],
                "seed_frame_index": seed["frame_index"],
                "expected_seed_revision": normalized["expected_seed_revision"],
                "radius_frames": normalized["radius_frames"],
                "seed_binding": seed_binding,
                "target_frame_indices": target_frame_indices,
                "tracker_profile": {
                    **TRACKER_PROFILE,
                    "profile_sha256": TRACKER_PROFILE_SHA256,
                },
                "neighbor_probe_job_id": None,
                "neighbor_probe_cancel_status": None,
                "neighbor_probe_cancel_error_code": None,
                "_seed_annotation": deepcopy(annotation),
                "_seed_frame": deepcopy(seed),
                "_commit_frames": None,
                "status": "queued",
                "stage": "queued",
                "frame_results": [],
                "summary": None,
                "suggestions": [],
                "error_code": None,
                "created_at": now,
                "updated_at": now,
                "status_url": f"/api/v1/ball-annotation-sessions/{session_id}/propagation-jobs/{job_id}",
                "cancel_url": f"/api/v1/ball-annotation-sessions/{session_id}/propagation-jobs/{job_id}/cancel",
            }
            self._persist_propagation(job)
            if self._auto_execute_propagation:
                job = self._reconcile_propagation_job(job, session)
            return self._public_propagation(job)

    @staticmethod
    def _propagation_reserves_targets(job: dict[str, Any]) -> bool:
        if job.get("status") not in {"failed", "cancelled"}:
            return True
        return bool(job.get("suggestions") or job.get("frame_results") or job.get("_commit_frames"))

    @staticmethod
    def _propagation_job_target_indices(job: dict[str, Any]) -> set[int]:
        raw_targets = job.get("target_frame_indices")
        if not isinstance(raw_targets, list) or any(
            isinstance(frame_index, bool) or not isinstance(frame_index, int) or frame_index < 0
            for frame_index in raw_targets
        ):
            raise BallAnnotationServiceError(
                "invalid_propagation_job",
                "Persisted propagation target authority is invalid",
            )
        return set(raw_targets)

    def _require_propagation_creation_capacity(
        self,
        session: dict[str, Any],
        *,
        target_frame_indices: list[int],
        existing_jobs: list[dict[str, Any]],
    ) -> None:
        session_jobs = [job for job in existing_jobs if job.get("session_id") == session["session_id"]]
        report_capable_job_ids = {
            require_safe_id(job.get("job_id"), "propagation job_id")
            for job in session_jobs
            if job.get("status") in _REPORT_CAPABLE_PROPAGATION_STATUSES
        }
        if len(report_capable_job_ids) >= _MAX_PROPAGATION_REPORTS_PER_SESSION:
            raise BallAnnotationServiceError(
                "propagation_report_limit",
                "Propagation report-capable job limit would be exceeded",
                status_code=409,
            )

        primary_indices = set(session["sampling_manifest"]["frame_indices"])
        reserved_supplemental = {
            frame["frame_index"] for frame in session["frames"] if frame.get("frame_role") == "propagation_target"
        }
        for existing in session_jobs:
            if self._propagation_reserves_targets(existing):
                reserved_supplemental.update(self._propagation_job_target_indices(existing) - primary_indices)
        reserved_supplemental.update(set(target_frame_indices) - primary_indices)
        if len(reserved_supplemental) > _MAX_SUPPLEMENTAL_FRAMES_PER_SESSION:
            raise BallAnnotationServiceError(
                "supplemental_frame_limit",
                "Propagation supplemental-frame limit would be exceeded",
                status_code=409,
            )

    def get_propagation_job(self, session_id: str, job_id: str) -> dict[str, Any]:
        with self._coordination_lock, self._lock:
            job = self._load_propagation(job_id)
            if job.get("session_id") != require_safe_id(session_id, "annotation session_id"):
                raise BallAnnotationServiceError(
                    "propagation_not_found", "Propagation job was not found", status_code=404
                )
            session = self._reconcile_session(self._load_session(session_id))
            job = self._load_propagation(job_id)
            return self._public_propagation(self._reconcile_propagation_job(job, session))

    def cancel_propagation_job(self, session_id: str, job_id: str) -> dict[str, Any]:
        with self._coordination_lock, self._lock:
            job = self._load_propagation(job_id)
            if job.get("session_id") != require_safe_id(session_id, "annotation session_id"):
                raise BallAnnotationServiceError(
                    "propagation_not_found", "Propagation job was not found", status_code=404
                )
            if job["status"] == "committing":
                raise BallAnnotationServiceError(
                    "commit_in_progress",
                    "Propagation commit can no longer be cancelled",
                    status_code=409,
                )
            if job["status"] in {"ready", "failed", "blocked", "cancelled"}:
                return self._public_propagation(job)
            cancel_status, cancel_error_code = self._cancel_neighbor_probe(job)
            job.update(
                {
                    "status": "cancelled",
                    "stage": "cancelled",
                    "error_code": "cancelled",
                    "neighbor_probe_cancel_status": cancel_status,
                    "neighbor_probe_cancel_error_code": cancel_error_code,
                    "updated_at": utc_now_iso(),
                }
            )
            self._persist_propagation(job)
            return self._public_propagation(job)

    def finalize_session(self, session_id: str, mutation_id: str) -> dict[str, Any]:
        mutation_id = require_safe_id(mutation_id, "finalize mutation_id")
        with self._coordination_lock, self._lock:
            session = self._reconcile_session(self._load_session(session_id))
            if session["status"] == "finalized":
                return self._read_final_result(session["session_id"])
            if session["status"] != "annotating":
                raise BallAnnotationServiceError("session_not_ready", "Annotation session cannot be finalized")
            if any(frame["current_annotation"] is None for frame in session["frames"]):
                raise BallAnnotationServiceError(
                    "annotation_incomplete", "Every sampled frame requires an effective confirmed annotation"
                )
            if any(frame["current_annotation"].get("annotation_state") != "confirmed" for frame in session["frames"]):
                raise BallAnnotationServiceError(
                    "annotation_not_confirmed",
                    "Every effective annotation must be human-confirmed before finalization",
                )
            pending_detector = self._pending_detector_candidate_count(session)
            pending_propagation = sum(
                suggestion.get("pending_human_confirmation") is True
                for frame in session["frames"]
                for suggestion in frame.get("propagation_suggestions", [])
            )
            if pending_detector or pending_propagation:
                raise BallAnnotationServiceError(
                    "suggestion_decisions_incomplete",
                    "Every detector and propagation suggestion requires an explicit human accept or dismiss decision before finalization",
                    status_code=409,
                )
            self._invalidate_active_propagations(session, "propagation_session_finalized")
            session["status"] = "finalizing"
            session["stage"] = "finalizing"
            session["finalize_mutation_id"] = mutation_id
            session["finalization_started_at"] = utc_now_iso()
            session["finalization_input_sha256"] = self._finalization_input_sha256(session)
            session["updated_at"] = session["finalization_started_at"]
            self._persist_session(session)
            self._hit_finalize_failpoint("after_intent")
            _session, result = self._complete_finalization(session)
            return result

    def get_final_result(self, session_id: str) -> dict[str, Any]:
        with self._coordination_lock, self._lock:
            session = self._reconcile_session(self._load_session(session_id))
            if session["status"] != "finalized":
                raise BallAnnotationServiceError(
                    "result_not_ready",
                    "The immutable annotation result is not ready",
                    status_code=409,
                )
            return self._read_final_result(session["session_id"])

    def _complete_finalization(self, session: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        session_id = require_safe_id(session.get("session_id"), "annotation session_id")
        if session.get("status") != "finalizing":
            raise BallAnnotationServiceError("invalid_finalization_state", "Annotation finalization state is invalid")
        expected_input_sha256 = require_sha256(
            session.get("finalization_input_sha256"),
            "annotation finalization input sha256",
        )
        if self._finalization_input_sha256(session) != expected_input_sha256:
            raise BallAnnotationServiceError(
                "finalization_input_changed",
                "Annotation inputs changed after finalization was frozen",
            )
        destination = self._final_results_root / session_id
        if destination.is_dir() and not is_link_or_reparse(destination):
            result = self._read_final_result_dir(
                destination,
                session_id,
                expected_session_request_sha256=session["request_sha256"],
                expected_finalization_input_sha256=expected_input_sha256,
            )
            expected_result = self._rebuild_expected_final_payload(session)
            if result != expected_result:
                raise BallAnnotationServiceError(
                    "invalid_final_result",
                    "Published final result differs from frozen finalization authority",
                )
        else:
            if destination.exists() or is_link_or_reparse(destination):
                raise BallAnnotationServiceError("unsafe_final_result", "Immutable final result destination is unsafe")
            staging = self._final_results_root / (f".staging-{session_id}-{uuid.uuid4().hex}")
            staging.mkdir()
            try:
                frame_media = self._stage_final_frame_media(session, staging)
                propagation_reports = self._build_propagation_reports(session)
                propagation_report_files = self._stage_final_propagation_reports(propagation_reports, staging)
                package = self._build_final_package(session, frame_media, propagation_reports)
                package_path = staging / "annotation_package.v1.json"
                atomic_write_json(package_path, package, trusted_root=staging)
                if session["data_role"] == "check":
                    try:
                        report = build_feasibility_report(
                            package_path,
                            trusted_root=staging,
                            get_probe=self._get_probe,
                            get_sampling_lock=self._get_sampling_lock,
                        )
                    except FeasibilityError as exc:
                        raise BallAnnotationServiceError("invalid_feasibility_evidence", str(exc)) from exc
                else:
                    report = self._development_not_applicable_report(session, package)
                report_path = staging / "feasibility_report.v1.json"
                atomic_write_json(report_path, report, trusted_root=staging)
                package_bytes, package_file_sha256 = read_regular_bytes(
                    package_path,
                    "sealed annotation package",
                    max_bytes=_MAX_FINAL_RESULT_BYTES,
                    trusted_root=staging,
                )
                report_bytes, report_file_sha256 = read_regular_bytes(
                    report_path,
                    "sealed feasibility report",
                    max_bytes=_MAX_FINAL_RESULT_BYTES,
                    trusted_root=staging,
                )
                manifest: dict[str, Any] = {
                    "schema_version": "1.0",
                    "artifact_type": "ball_annotation_final_result_manifest",
                    "session_id": session_id,
                    "session_request_sha256": session["request_sha256"],
                    "finalization_input_sha256": expected_input_sha256,
                    "package_relative_path": "annotation_package.v1.json",
                    "package_file_sha256": package_file_sha256,
                    "package_file_size_bytes": len(package_bytes),
                    "package_sha256": package["package_sha256"],
                    "report_relative_path": "feasibility_report.v1.json",
                    "report_file_sha256": report_file_sha256,
                    "report_file_size_bytes": len(report_bytes),
                    "report_sha256": report["report_sha256"],
                    "report_status": report["status"],
                    "frame_media": deepcopy(frame_media),
                    "frame_media_sha256": canonical_sha256(frame_media),
                    "propagation_report_files": deepcopy(propagation_report_files),
                    "propagation_report_files_sha256": canonical_sha256(propagation_report_files),
                    "created_at": session["finalization_started_at"],
                }
                manifest["manifest_sha256"] = canonical_sha256(manifest)
                atomic_write_json(
                    staging / "final_result_manifest.v1.json",
                    manifest,
                    trusted_root=staging,
                )
                result = self._read_final_result_dir(
                    staging,
                    session_id,
                    expected_session_request_sha256=session["request_sha256"],
                )
                self._hit_finalize_failpoint("before_publish")
                os.replace(staging, destination)
                self._hit_finalize_failpoint("after_publish")
            finally:
                if staging.exists():
                    self._remove_final_result_tree(staging)
        manifest = self._read_json(
            destination / "final_result_manifest.v1.json",
            "final result manifest",
            _MAX_FINAL_RESULT_BYTES,
        )
        session["status"] = "finalized"
        session["stage"] = "finalized"
        session["final_result"] = None
        session["final_package"] = {
            "result_url": f"/api/v1/ball-annotation-sessions/{session_id}/result",
            "manifest_sha256": manifest["manifest_sha256"],
            "package_sha256": result["package"]["package_sha256"],
            "report_sha256": result["feasibility_report"]["report_sha256"],
            "status": result["feasibility_report"]["status"],
        }
        session["updated_at"] = utc_now_iso()
        if session["data_role"] == "check":
            self._transition_session_groups(session_id, "scored")
        self._persist_session(session)
        self._hit_finalize_failpoint("after_session_commit")
        return session, deepcopy(result)

    def _rebuild_expected_final_payload(self, session: dict[str, Any]) -> dict[str, Any]:
        """Rebuild frozen package/report authority without trusting published files."""

        session_id = require_safe_id(session.get("session_id"), "annotation session_id")
        staging = self._final_results_root / (f".rebuild-{session_id}-{uuid.uuid4().hex}")
        staging.mkdir()
        try:
            frame_media = self._stage_final_frame_media(session, staging)
            propagation_reports = self._build_propagation_reports(session)
            package = self._build_final_package(session, frame_media, propagation_reports)
            package_path = staging / "annotation_package.v1.json"
            atomic_write_json(package_path, package, trusted_root=staging)
            if session["data_role"] == "check":
                try:
                    report = build_feasibility_report(
                        package_path,
                        trusted_root=staging,
                        get_probe=self._get_probe,
                        get_sampling_lock=self._get_sampling_lock,
                    )
                except FeasibilityError as exc:
                    raise BallAnnotationServiceError("invalid_feasibility_evidence", str(exc)) from exc
            else:
                report = self._development_not_applicable_report(session, package)
            return {
                "package": deepcopy(package),
                "feasibility_report": deepcopy(report),
            }
        finally:
            if staging.exists():
                self._remove_final_result_tree(staging)

    @staticmethod
    def _session_request_authority(session: dict[str, Any]) -> dict[str, Any]:
        normalized_request = session.get("_normalized_session_request")
        request_sha256 = require_sha256(
            session.get("request_sha256"),
            "annotation session request sha256",
        )
        if (
            not isinstance(normalized_request, dict)
            or canonical_sha256(normalized_request) != request_sha256
            or session.get("idempotency_key") != request_sha256
        ):
            raise BallAnnotationServiceError(
                "invalid_session_request_authority",
                "Annotation session request authority is invalid",
            )
        session_id = require_safe_id(
            session.get("session_id"),
            "annotation session_id",
        )
        prefix = f"annotation-{request_sha256[:16]}-"
        suffix = session_id.removeprefix(prefix)
        lineage = session.get("lineage")
        locked_profile = session.get("locked_profile")
        sampling_manifest = session.get("sampling_manifest")
        if (
            not session_id.startswith(prefix)
            or len(suffix) != 12
            or any(character not in "0123456789abcdef" for character in suffix)
            or normalized_request.get("data_role") != session.get("data_role")
            or not isinstance(lineage, dict)
            or set(normalized_request.get("development_probe_job_ids", []))
            != set(lineage.get("development_probe_job_ids", []))
            or not isinstance(locked_profile, dict)
            or normalized_request.get("locked_profile_id") != locked_profile.get("profile_id")
            or normalized_request.get("operator_id") != session.get("operator_id")
            or normalized_request.get("sampling_profile_id") != session.get("sampling_profile_id")
            or normalized_request.get("metric_profile_id") != session.get("metric_profile_id")
            or not isinstance(sampling_manifest, dict)
            or normalized_request.get("strata_applicability") != sampling_manifest.get("strata_applicability")
        ):
            raise BallAnnotationServiceError(
                "invalid_session_request_authority",
                "Annotation session request selection is invalid",
            )
        development_binding = session.get("development_package_binding")
        if session.get("data_role") == "development":
            binding_valid = (
                normalized_request.get("target_frame_count") is None
                and normalized_request.get("development_package_session_id") is None
                and normalized_request.get("development_package_sha256") is None
                and development_binding is None
            )
        else:
            binding_valid = (
                isinstance(development_binding, dict)
                and normalized_request.get("target_frame_count") == sampling_manifest.get("target_frame_count")
                and normalized_request.get("development_package_session_id") == development_binding.get("session_id")
                and normalized_request.get("development_package_sha256") == development_binding.get("package_sha256")
            )
        if not binding_valid:
            raise BallAnnotationServiceError(
                "invalid_session_request_authority",
                "Annotation session request binding is invalid",
            )
        body: dict[str, Any] = {
            "schema_version": "1.0",
            "artifact_type": "ball_annotation_session_request_authority",
            "session_id": session_id,
            "request_sha256": request_sha256,
            "normalized_request": deepcopy(normalized_request),
        }
        return {
            **body,
            "authority_sha256": canonical_sha256(body),
        }

    def _build_final_package(
        self,
        session: dict[str, Any],
        frame_media: list[dict[str, Any]],
        propagation_reports: list[dict[str, Any]],
    ) -> dict[str, Any]:
        annotations = [
            {
                "frame_index": frame["frame_index"],
                **deepcopy(frame["current_annotation"]),
            }
            for frame in session["frames"]
        ]
        supplemental_frame_indices = sorted(
            frame["frame_index"] for frame in session["frames"] if frame.get("frame_role") == "propagation_target"
        )
        detector_candidate_evidence = self._build_detector_candidate_evidence(session)
        frame_evidence = self._build_frame_evidence_rows(session, annotations, propagation_reports)
        pending_suggestion_count = sum(
            suggestion.get("pending_human_confirmation") is True
            for frame in session["frames"]
            for suggestion in frame.get("propagation_suggestions", [])
        )
        pending_detector_candidate_count = sum(item["decision"] is None for item in detector_candidate_evidence)
        pending_decision_count = pending_suggestion_count + pending_detector_candidate_count
        localizable_positive_seed_count = sum(
            annotation.get("presence") == "present"
            and (annotation.get("point_source_px") is not None or annotation.get("bbox_source_px") is not None)
            for annotation in (frame["current_annotation"] for frame in session["frames"])
        )
        eligibility_reasons: list[str] = []
        if session["data_role"] != "development":
            eligibility_reasons.append("check_role_is_evaluation_only")
        if pending_decision_count:
            eligibility_reasons.append("pending_suggestion_decisions")
        if session["data_role"] == "development" and localizable_positive_seed_count == 0:
            eligibility_reasons.append("no_localizable_positive_seed")
        dataset_expansion_eligible = session["data_role"] == "development" and not eligibility_reasons
        dataset_expansion_eligibility = {
            "eligible": dataset_expansion_eligible,
            "reasons": eligibility_reasons,
            "validation_evidence": {
                "all_frames_human_confirmed": True,
                "all_primary_roles_complete": True,
                "all_supplemental_roles_complete": True,
                "exact_frame_media_sha256": canonical_sha256(frame_media),
                "frame_evidence_sha256": canonical_sha256(frame_evidence),
                "revision_chain_sha256": canonical_sha256(session["revisions"]),
                "pending_propagation_suggestion_count": pending_suggestion_count,
                "pending_detector_candidate_count": pending_detector_candidate_count,
                "pending_suggestion_decision_count": pending_decision_count,
                "localizable_positive_seed_count": localizable_positive_seed_count,
            },
        }
        package: dict[str, Any] = {
            "schema_version": "1.0",
            "artifact_type": "ball_annotation_package",
            "session_id": session["session_id"],
            "session_request_authority": self._session_request_authority(session),
            "data_role": session["data_role"],
            "source": deepcopy(session["source"]),
            "lineage": deepcopy(session["lineage"]),
            "frame_review_proxy_authority": deepcopy(session.get("_frame_review_proxy_authority")),
            "detector_probe_authorities": deepcopy(session.get("_detector_probe_authorities", [])),
            "attempt_family_sha256": session["attempt_family_sha256"],
            "development_package_binding": deepcopy(session.get("development_package_binding")),
            "operator_id": session["operator_id"],
            "locked_profile": deepcopy(session["locked_profile"]),
            "control_profile_id": session["control_profile_id"],
            "control_profile": deepcopy(session["control_profile"]),
            "sampling_profile_id": session["sampling_profile_id"],
            "metric_profile_id": session["metric_profile_id"],
            "metric_profile_sha256": session["metric_profile_sha256"],
            "sampling_manifest": deepcopy(session["sampling_manifest"]),
            "check_probe_job_id": session.get("check_probe_job_id"),
            "check_probe_authority": deepcopy(session.get("check_probe_authority")),
            "effective_annotations": annotations,
            "revision_chain": deepcopy(session["revisions"]),
            "supplemental_frame_indices": supplemental_frame_indices,
            "frame_evidence": frame_evidence,
            "frame_evidence_sha256": canonical_sha256(frame_evidence),
            "frame_media": deepcopy(frame_media),
            "frame_media_sha256": canonical_sha256(frame_media),
            "detector_candidate_evidence": detector_candidate_evidence,
            "detector_candidate_evidence_sha256": canonical_sha256(detector_candidate_evidence),
            "propagation_reports": deepcopy(propagation_reports),
            "propagation_reports_sha256": canonical_sha256(propagation_reports),
            "created_at": session["finalization_started_at"],
            "training_eligible": False,
            "may_seed_dataset_expansion": dataset_expansion_eligible,
            "dataset_expansion_eligibility": dataset_expansion_eligibility,
            "qualification_eligible": False,
            "pr4a_pr4b_truth_compatible": False,
        }
        package["package_sha256"] = canonical_sha256(package)
        try:
            verify_frame_evidence_package(package)
        except BallFrameEvidenceError as exc:
            raise BallAnnotationServiceError("invalid_frame_evidence", str(exc)) from exc
        return package

    def _build_frame_evidence_rows(
        self,
        session: dict[str, Any],
        annotations: list[dict[str, Any]],
        propagation_reports: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        annotation_by_frame = {annotation["frame_index"]: annotation for annotation in annotations}
        revisions_by_frame: dict[int, list[dict[str, Any]]] = {}
        for revision in session["revisions"]:
            revisions_by_frame.setdefault(revision["frame_index"], []).append(revision)
        manifest_groups = {group["group_id"]: group for group in session["sampling_manifest"]["groups"]}
        propagation_report_sha256s = {report["job_id"]: report["report_sha256"] for report in propagation_reports}
        rows: list[dict[str, Any]] = []
        for frame in sorted(session["frames"], key=lambda item: item["frame_index"]):
            frame_index = frame["frame_index"]
            role = "primary" if frame.get("primary_sample") is True else "supplemental"
            timing = build_source_frame_timing_binding(
                source_sha256=session["source"]["sha256"],
                runtime_environment_sha256=frame.get("_runtime_environment_sha256"),
                source_frame_jpeg_sha256=frame["source_frame_sha256"],
                frame_index=frame_index,
                decoded_frame_position=frame.get("_decoded_frame_position"),
                fps=session["source"]["fps"],
                effective_decode_mode=frame.get("_effective_decode_mode"),
                decoder_reported_pos_msec=frame.get("_decoder_reported_pos_msec"),
                decoder_timing_observation_method=frame.get("_decoder_timing_observation_method"),
                position_verification=frame.get("_position_verification"),
                true_presentation_timestamp=deepcopy(
                    frame.get(
                        "_true_presentation_timestamp",
                        _TRUE_PRESENTATION_TIMESTAMP_NOT_COLLECTED,
                    )
                ),
                cross_decode_verification=None,
                timing_status=frame.get(
                    "source_timing_status",
                    ("not_collected" if frame.get("_decoder_reported_pos_msec") is None else "observed"),
                ),
            )
            probe_evidence = {
                "probe_job_id": frame.get("_probe_job_id"),
                "probe_report_sha256": frame.get("_probe_report_sha256"),
                "probe_result_manifest_sha256": frame.get("_probe_result_manifest_sha256"),
                "artifact_id": frame.get("_artifact_id"),
            }
            propagation_evidence = None
            if role == "primary":
                temporal_group = temporal_group_for_frame(session["source"]["sha256"], frame_index)
            else:
                source_group_row = manifest_groups.get(frame.get("temporal_group_id"))
                if not isinstance(source_group_row, dict):
                    raise BallAnnotationServiceError(
                        "invalid_frame_evidence",
                        "Supplemental frame temporal ancestry is missing",
                    )
                source_group = temporal_group_for_frame(
                    session["source"]["sha256"],
                    source_group_row["seed_frame_index"],
                )
                temporal_group = inherit_temporal_group(
                    source_group,
                    artifact_type="propagation",
                    artifact_id=frame.get("_artifact_id"),
                )
                propagation_evidence = self._supplemental_propagation_evidence(frame, propagation_report_sha256s)
            try:
                row = build_frame_evidence_row(
                    frame_role=role,
                    source={
                        "sha256": session["source"]["sha256"],
                        "width": session["source"]["width"],
                        "height": session["source"]["height"],
                    },
                    frame_index=frame_index,
                    source_frame_jpeg_sha256=frame["source_frame_sha256"],
                    source_frame_jpeg_size_bytes=frame["source_frame_size_bytes"],
                    temporal_group=temporal_group,
                    probe_evidence=probe_evidence,
                    timing_binding=timing,
                    proxy_binding=deepcopy(frame.get("_proxy_binding")),
                    effective_annotation=annotation_by_frame[frame_index],
                    revision_chain=revisions_by_frame.get(frame_index, []),
                    propagation_evidence=propagation_evidence,
                )
            except (BallFrameEvidenceError, FeasibilityError) as exc:
                raise BallAnnotationServiceError("invalid_frame_evidence", str(exc)) from exc
            rows.append(row)
        return rows

    def _supplemental_propagation_evidence(
        self,
        frame: dict[str, Any],
        propagation_report_sha256s: dict[str, str],
    ) -> dict[str, Any]:
        probe_job_id = frame.get("_probe_job_id")
        source_job_id = frame.get("_source_propagation_job_id")
        if not isinstance(source_job_id, str) or source_job_id not in frame.get("propagation_job_ids", []):
            raise BallAnnotationServiceError(
                "invalid_frame_evidence",
                "Supplemental frame producing propagation authority is missing",
            )
        job = self._load_propagation(source_job_id)
        propagation_report_sha256 = propagation_report_sha256s.get(source_job_id)
        commit_frame = next(
            (item for item in job.get("_commit_frames") or [] if item.get("frame_index") == frame["frame_index"]),
            None,
        )
        frame_result = next(
            (item for item in job.get("frame_results", []) if item.get("frame_index") == frame["frame_index"]),
            None,
        )
        if (
            job.get("status") != "ready"
            or not isinstance(propagation_report_sha256, str)
            or job.get("neighbor_probe_job_id") != probe_job_id
            or not isinstance(commit_frame, dict)
            or not isinstance(frame_result, dict)
            or commit_frame.get("source_frame_sha256") != frame.get("source_frame_sha256")
            or commit_frame.get("_artifact_id") != frame.get("_artifact_id")
            or commit_frame.get("_probe_report_sha256") != frame.get("_probe_report_sha256")
            or commit_frame.get("_probe_result_manifest_sha256") != frame.get("_probe_result_manifest_sha256")
        ):
            raise BallAnnotationServiceError(
                "invalid_frame_evidence",
                "Supplemental propagation commit lineage is incomplete or changed",
            )
        expected_intent = {
            "session_id": job.get("session_id"),
            "mutation_id": job.get("mutation_id"),
            "seed_frame_index": job.get("seed_frame_index"),
            "radius_frames": job.get("radius_frames"),
            "expected_seed_revision": job.get("expected_seed_revision"),
            "seed_binding": deepcopy(job.get("seed_binding")),
            "target_frame_indices": deepcopy(job.get("target_frame_indices")),
        }
        if canonical_sha256(expected_intent) != job.get("intent_sha256") or job.get("tracker_profile") != {
            **TRACKER_PROFILE,
            "profile_sha256": TRACKER_PROFILE_SHA256,
        }:
            raise BallAnnotationServiceError(
                "invalid_frame_evidence",
                "Supplemental propagation intent, seed, or tracker authority changed",
            )
        suggestion_id = frame_result.get("suggestion_id")
        suggestion = next(
            (
                item
                for item in job.get("suggestions", [])
                if item.get("suggestion_id") == suggestion_id and item.get("frame_index") == frame["frame_index"]
            ),
            None,
        )
        if suggestion_id is None:
            suggestion_sha256 = None
        elif not isinstance(suggestion, dict):
            raise BallAnnotationServiceError(
                "invalid_frame_evidence",
                "Supplemental suggestion lineage is incomplete",
            )
        else:
            suggestion_sha256 = canonical_sha256(self._suggestion_authority_payload(suggestion))
        return {
            "propagation_job_id": job["job_id"],
            "neighbor_probe_job_id": job["neighbor_probe_job_id"],
            "neighbor_probe_report_sha256": frame["_probe_report_sha256"],
            "neighbor_probe_result_manifest_sha256": frame["_probe_result_manifest_sha256"],
            "neighbor_artifact_id": frame["_artifact_id"],
            "propagation_intent_sha256": job["intent_sha256"],
            "seed_binding_sha256": canonical_sha256(job["seed_binding"]),
            "tracker_profile_sha256": TRACKER_PROFILE_SHA256,
            "propagation_report_sha256": propagation_report_sha256,
            "propagation_frame_result_sha256": canonical_sha256(frame_result),
            "suggestion_id": suggestion_id,
            "suggestion_sha256": suggestion_sha256,
        }

    def _read_final_result(self, session_id: str) -> dict[str, Any]:
        session_id = require_safe_id(session_id, "annotation session_id")
        session = self._load_session(session_id)
        expected_anchor = session.get("final_package") if session.get("status") == "finalized" else None
        if session.get("status") == "finalized" and not isinstance(expected_anchor, dict):
            raise BallAnnotationServiceError(
                "invalid_final_result",
                "Finalized session is missing its durable result anchor",
            )
        return self._read_final_result_dir(
            self._final_results_root / session_id,
            session_id,
            expected_anchor=expected_anchor,
            expected_session_request_sha256=session.get("request_sha256"),
            expected_finalization_input_sha256=(
                session.get("finalization_input_sha256") if session.get("status") == "finalizing" else None
            ),
        )

    def _read_final_result_dir(
        self,
        root: Path,
        session_id: str,
        *,
        expected_anchor: dict[str, Any] | None = None,
        expected_session_request_sha256: str | None = None,
        expected_finalization_input_sha256: str | None = None,
    ) -> dict[str, Any]:
        if is_link_or_reparse(root) or not root.is_dir():
            raise BallAnnotationServiceError("result_not_ready", "Immutable annotation result is unavailable")
        manifest = self._read_json(
            root / "final_result_manifest.v1.json",
            "final result manifest",
            _MAX_FINAL_RESULT_BYTES,
        )
        published_manifest_sha256 = manifest.get("manifest_sha256")
        if (
            manifest.get("schema_version") != "1.0"
            or manifest.get("artifact_type") != "ball_annotation_final_result_manifest"
            or manifest.get("session_id") != session_id
            or (
                expected_session_request_sha256 is not None
                and manifest.get("session_request_sha256") != expected_session_request_sha256
            )
            or (
                expected_finalization_input_sha256 is not None
                and manifest.get("finalization_input_sha256") != expected_finalization_input_sha256
            )
            or published_manifest_sha256
            != canonical_sha256({key: value for key, value in manifest.items() if key != "manifest_sha256"})
        ):
            raise BallAnnotationServiceError("invalid_final_result", "Final result manifest is invalid")
        frame_media = manifest.get("frame_media")
        propagation_report_files = manifest.get("propagation_report_files")
        self._validate_final_result_manifest_collections(frame_media, propagation_report_files)
        if manifest.get("frame_media_sha256") != canonical_sha256(frame_media):
            raise BallAnnotationServiceError("invalid_final_result", "Final frame media manifest is invalid")
        if manifest.get("propagation_report_files_sha256") != canonical_sha256(propagation_report_files):
            raise BallAnnotationServiceError(
                "invalid_final_result",
                "Final propagation report manifest is invalid",
            )
        expected_files = {
            "annotation_package.v1.json",
            "feasibility_report.v1.json",
            "final_result_manifest.v1.json",
            *(entry["relative_path"] for entry in frame_media),
            *(entry["relative_path"] for entry in propagation_report_files),
        }
        try:
            tree_before = exact_regular_tree_snapshot(
                root,
                expected_files,
                "final annotation result tree",
                trusted_root=self._final_results_root,
            )
            repeated_manifest_bytes, _ = read_regular_bytes(
                root / "final_result_manifest.v1.json",
                "final result manifest",
                max_bytes=_MAX_FINAL_RESULT_BYTES,
                trusted_root=root,
            )
            if json_object_from_bytes(repeated_manifest_bytes, "final result manifest") != manifest:
                raise DetectorDevelopmentError(
                    "source_changed",
                    "Final result manifest changed during validation",
                )
        except DetectorDevelopmentError as exc:
            raise BallAnnotationServiceError(
                "invalid_final_result",
                "Final result file allowlist is invalid",
            ) from exc
        package_bytes, package_file_sha256 = read_regular_bytes(
            root / "annotation_package.v1.json",
            "sealed annotation package",
            max_bytes=_MAX_FINAL_RESULT_BYTES,
            trusted_root=root,
        )
        report_bytes, report_file_sha256 = read_regular_bytes(
            root / "feasibility_report.v1.json",
            "sealed feasibility report",
            max_bytes=_MAX_FINAL_RESULT_BYTES,
            trusted_root=root,
        )
        package = json_object_from_bytes(package_bytes, "sealed annotation package")
        report = json_object_from_bytes(report_bytes, "sealed feasibility report")
        session_request_authority = package.get("session_request_authority")
        if (
            package_file_sha256 != manifest.get("package_file_sha256")
            or len(package_bytes) != manifest.get("package_file_size_bytes")
            or report_file_sha256 != manifest.get("report_file_sha256")
            or len(report_bytes) != manifest.get("report_file_size_bytes")
            or package.get("session_id") != session_id
            or package.get("package_sha256") != manifest.get("package_sha256")
            or package.get("package_sha256")
            != canonical_sha256({key: value for key, value in package.items() if key != "package_sha256"})
            or not isinstance(session_request_authority, dict)
            or session_request_authority.get("request_sha256") != manifest.get("session_request_sha256")
            or report.get("session_id") != session_id
            or report.get("report_sha256") != manifest.get("report_sha256")
            or report.get("report_sha256")
            != canonical_sha256({key: value for key, value in report.items() if key != "report_sha256"})
            or report.get("status") != manifest.get("report_status")
            or package.get("frame_media") != frame_media
            or package.get("frame_media_sha256") != manifest.get("frame_media_sha256")
            or not isinstance(package.get("propagation_reports"), list)
            or package.get("propagation_reports_sha256") != canonical_sha256(package.get("propagation_reports"))
        ):
            raise BallAnnotationServiceError("invalid_final_result", "Final result content binding is invalid")
        self._validate_final_report_binding(package, report)
        if expected_anchor is not None and (
            manifest.get("manifest_sha256") != expected_anchor.get("manifest_sha256")
            or package.get("package_sha256") != expected_anchor.get("package_sha256")
            or report.get("report_sha256") != expected_anchor.get("report_sha256")
            or report.get("status") != expected_anchor.get("status")
        ):
            raise BallAnnotationServiceError(
                "invalid_final_result",
                "Final result differs from the finalized session anchor",
            )
        reports_by_job = {item.get("job_id"): item for item in package["propagation_reports"] if isinstance(item, dict)}
        if len(reports_by_job) != len(package["propagation_reports"]):
            raise BallAnnotationServiceError(
                "invalid_final_result",
                "Sealed propagation reports have duplicate or invalid jobs",
            )
        if [entry.get("job_id") for entry in propagation_report_files] != sorted(reports_by_job):
            raise BallAnnotationServiceError(
                "invalid_final_result",
                "Propagation report files do not match the package",
            )
        for entry in propagation_report_files:
            if not isinstance(entry, dict) or set(entry) != {
                "job_id",
                "relative_path",
                "report_sha256",
                "file_sha256",
                "file_size_bytes",
            }:
                raise BallAnnotationServiceError(
                    "invalid_final_result",
                    "Propagation report file entry is invalid",
                )
            job_id = require_safe_id(entry.get("job_id"), "sealed propagation report job_id")
            relative_path = entry.get("relative_path")
            if relative_path != f"propagation_reports/{job_id}.v1.json":
                raise BallAnnotationServiceError(
                    "invalid_final_result",
                    "Propagation report relative path is invalid",
                )
            content, digest = read_regular_bytes(
                root / relative_path,
                "sealed propagation report",
                max_bytes=_MAX_FINAL_RESULT_BYTES,
                trusted_root=root,
            )
            sealed_report = json_object_from_bytes(content, "sealed propagation report")
            package_report = reports_by_job[job_id]
            if (
                digest != entry.get("file_sha256")
                or len(content) != entry.get("file_size_bytes")
                or sealed_report != package_report
                or package_report.get("session_id") != session_id
                or package_report.get("report_sha256") != entry.get("report_sha256")
                or package_report.get("report_sha256")
                != canonical_sha256({key: value for key, value in package_report.items() if key != "report_sha256"})
            ):
                raise BallAnnotationServiceError(
                    "invalid_final_result",
                    "Sealed propagation report binding changed",
                )
        source = package.get("source")
        if not isinstance(source, dict):
            raise BallAnnotationServiceError("invalid_final_result", "Final result source binding is invalid")
        for entry in frame_media:
            if not isinstance(entry, dict):
                raise BallAnnotationServiceError("invalid_final_result", "Final frame media entry is invalid")
            relative_path = entry.get("relative_path")
            frame_index = entry.get("frame_index")
            if (
                not isinstance(relative_path, str)
                or isinstance(frame_index, bool)
                or not isinstance(frame_index, int)
                or frame_index < 0
                or relative_path != f"frames/{frame_index:09d}.jpg"
                or entry.get("media_type") != "image/jpeg"
                or entry.get("width") != source.get("width")
                or entry.get("height") != source.get("height")
            ):
                raise BallAnnotationServiceError("invalid_final_result", "Final frame media binding is invalid")
            content, digest = read_regular_bytes(
                root / relative_path,
                "sealed source frame JPEG",
                max_bytes=_MAX_FRAME_BYTES,
                trusted_root=root,
            )
            if digest != entry.get("sha256") or len(content) != entry.get("size_bytes"):
                raise BallAnnotationServiceError("invalid_final_result", "Final frame media digest changed")
            self._validate_jpeg(content, int(source["width"]), int(source["height"]))
        try:
            verify_frame_evidence_package(package)
        except BallFrameEvidenceError as exc:
            raise BallAnnotationServiceError("invalid_final_result", "Final result frame evidence is invalid") from exc
        try:
            tree_after = exact_regular_tree_snapshot(
                root,
                expected_files,
                "final annotation result tree",
                trusted_root=self._final_results_root,
            )
        except DetectorDevelopmentError as exc:
            raise BallAnnotationServiceError(
                "invalid_final_result",
                "Final result file allowlist is invalid",
            ) from exc
        if tree_after != tree_before:
            raise BallAnnotationServiceError(
                "invalid_final_result",
                "Final result changed during validation",
            )
        return {"package": package, "feasibility_report": report}

    @staticmethod
    def _validate_final_result_manifest_collections(
        frame_media: Any,
        propagation_report_files: Any,
    ) -> None:
        if not isinstance(frame_media, list) or not 1 <= len(frame_media) <= _MAX_FINAL_FRAME_MEDIA:
            raise BallAnnotationServiceError(
                "invalid_final_result",
                "Final frame media manifest is invalid",
            )
        if (
            not isinstance(propagation_report_files, list)
            or len(propagation_report_files) > _MAX_FINAL_PROPAGATION_REPORT_FILES
        ):
            raise BallAnnotationServiceError(
                "invalid_final_result",
                "Final propagation report manifest is invalid",
            )

        frame_indices: list[int] = []
        frame_paths: list[str] = []
        for entry in frame_media:
            if not isinstance(entry, dict):
                raise BallAnnotationServiceError(
                    "invalid_final_result",
                    "Final frame media entry is invalid",
                )
            frame_index = entry.get("frame_index")
            relative_path = entry.get("relative_path")
            if (
                isinstance(frame_index, bool)
                or not isinstance(frame_index, int)
                or not 0 <= frame_index <= _MAX_FINAL_FRAME_INDEX
                or relative_path != f"frames/{frame_index:09d}.jpg"
            ):
                raise BallAnnotationServiceError(
                    "invalid_final_result",
                    "Final frame media identity is invalid",
                )
            frame_indices.append(frame_index)
            frame_paths.append(relative_path)
        if frame_indices != sorted(set(frame_indices)) or len(frame_paths) != len(set(frame_paths)):
            raise BallAnnotationServiceError(
                "invalid_final_result",
                "Final frame media order or identity is invalid",
            )

        report_job_ids: list[str] = []
        report_paths: list[str] = []
        for entry in propagation_report_files:
            if not isinstance(entry, dict):
                raise BallAnnotationServiceError(
                    "invalid_final_result",
                    "Final propagation report entry is invalid",
                )
            try:
                job_id = require_safe_id(entry.get("job_id"), "sealed propagation report job_id")
            except DetectorDevelopmentError as exc:
                raise BallAnnotationServiceError(
                    "invalid_final_result",
                    "Final propagation report identity is invalid",
                ) from exc
            relative_path = entry.get("relative_path")
            if relative_path != f"propagation_reports/{job_id}.v1.json":
                raise BallAnnotationServiceError(
                    "invalid_final_result",
                    "Final propagation report identity is invalid",
                )
            report_job_ids.append(job_id)
            report_paths.append(relative_path)
        if report_job_ids != sorted(set(report_job_ids)) or len(report_paths) != len(set(report_paths)):
            raise BallAnnotationServiceError(
                "invalid_final_result",
                "Final propagation report order or identity is invalid",
            )

    @staticmethod
    def _validate_final_report_binding(package: dict[str, Any], report: dict[str, Any]) -> None:
        sealed = report.get("sealed_evidence")
        manifest = package.get("sampling_manifest")
        if (
            report.get("schema_version") != "1.0"
            or report.get("artifact_type") != "ball_feasibility_report"
            or not isinstance(sealed, dict)
            or not isinstance(manifest, dict)
            or sealed.get("annotation_package_sha256") != package.get("package_sha256")
            or sealed.get("sampling_manifest_sha256") != manifest.get("manifest_sha256")
            or report.get("attempt_family_sha256") != package.get("attempt_family_sha256")
            or sealed.get("attempt_family_sha256") != package.get("attempt_family_sha256")
        ):
            raise BallAnnotationServiceError(
                "invalid_final_result",
                "Feasibility report is not bound to the annotation package",
            )
        if package.get("data_role") == "development":
            if (
                report.get("status") != "not_applicable"
                or report.get("development_package_binding") is not None
                or package.get("development_package_binding") is not None
                or sealed.get("check_probe_job_id") is not None
                or sealed.get("check_probe_report_sha256") is not None
                or sealed.get("dataset_expansion_eligibility") != package.get("dataset_expansion_eligibility")
            ):
                raise BallAnnotationServiceError(
                    "invalid_final_result",
                    "Development feasibility report variant is inconsistent",
                )
            return
        binding = package.get("development_package_binding")
        check_authority = package.get("check_probe_authority")
        if (
            package.get("data_role") != "check"
            or report.get("status") == "not_applicable"
            or not isinstance(binding, dict)
            or not isinstance(check_authority, dict)
            or report.get("development_package_binding") != binding
            or sealed.get("development_annotation_session_id") != binding.get("session_id")
            or sealed.get("development_annotation_package_sha256") != binding.get("package_sha256")
            or sealed.get("check_probe_job_id") != check_authority.get("job_id")
            or sealed.get("check_probe_report_sha256") != check_authority.get("report_sha256")
        ):
            raise BallAnnotationServiceError(
                "invalid_final_result",
                "Check feasibility report variant is inconsistent",
            )

    def _finalization_input_sha256(self, session: dict[str, Any]) -> str:
        propagation_reports = self._build_propagation_reports(session)
        return canonical_sha256(
            {
                "session_id": session["session_id"],
                "session_request_authority": self._session_request_authority(session),
                "data_role": session["data_role"],
                "source": session["source"],
                "lineage": session["lineage"],
                "frame_review_proxy_authority": session.get("_frame_review_proxy_authority"),
                "detector_probe_authorities": session.get("_detector_probe_authorities", []),
                "locked_profile": session["locked_profile"],
                "control_profile_id": session["control_profile_id"],
                "control_profile": session["control_profile"],
                "attempt_family_sha256": session["attempt_family_sha256"],
                "development_package_binding": session.get("development_package_binding"),
                "operator_id": session["operator_id"],
                "sampling_profile_id": session["sampling_profile_id"],
                "metric_profile_id": session["metric_profile_id"],
                "metric_profile_sha256": session["metric_profile_sha256"],
                "sampling_manifest": session["sampling_manifest"],
                "check_probe_job_id": session.get("check_probe_job_id"),
                "check_probe_authority": session.get("check_probe_authority"),
                "frames": session["frames"],
                "revisions": session["revisions"],
                "propagation_reports_sha256": canonical_sha256(propagation_reports),
                "finalize_mutation_id": session["finalize_mutation_id"],
                "finalization_started_at": session["finalization_started_at"],
            }
        )

    def _hit_finalize_failpoint(self, stage: str) -> None:
        if self._finalize_failpoint is not None:
            self._finalize_failpoint(stage)

    def _hit_propagation_failpoint(self, stage: str) -> None:
        if self._propagation_failpoint is not None:
            self._propagation_failpoint(stage)

    def _hit_confirmation_failpoint(self, stage: str) -> None:
        if self._confirmation_failpoint is not None:
            self._confirmation_failpoint(stage)

    def _hit_session_setup_failpoint(self, stage: str) -> None:
        if self._session_setup_failpoint is not None:
            self._session_setup_failpoint(stage)

    def _remove_orphan_final_staging(self) -> None:
        for pattern in (".staging-*", ".rebuild-*"):
            for path in self._final_results_root.glob(pattern):
                self._remove_final_result_tree(path)

    def _remove_final_result_tree(self, path: Path) -> None:
        try:
            if path.parent.resolve(strict=True) != self._final_results_root:
                raise BallAnnotationServiceError("unsafe_final_result", "Final result cleanup path is unsafe")
        except OSError as exc:
            raise BallAnnotationServiceError("unsafe_final_result", "Final result cleanup root is unavailable") from exc
        if not path.name.startswith((".staging-", ".rebuild-")):
            raise BallAnnotationServiceError("unsafe_final_result", "Final result cleanup path is unsafe")
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise BallAnnotationServiceError("unsafe_final_result", "Final result cleanup path is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x00000400)
        ):
            raise BallAnnotationServiceError("unsafe_final_result", "Final result cleanup cannot follow a link")
        if not stat.S_ISDIR(metadata.st_mode):
            raise BallAnnotationServiceError("unsafe_final_result", "Final result cleanup target must be a directory")
        shutil.rmtree(path)

    @staticmethod
    def _attempt_family_authority(value: dict[str, Any]) -> dict[str, Any]:
        lineage = value["lineage"]
        sampling_manifest = value["sampling_manifest"]
        return {
            "schema_version": "1.0",
            "artifact_type": "ball_annotation_attempt_family_authority",
            "source": deepcopy(value["source"]),
            "locked_profile": deepcopy(value["locked_profile"]),
            "control_profile_id": value["control_profile_id"],
            "control_profile": deepcopy(value["control_profile"]),
            "parent_trial_id": lineage["parent_trial_id"],
            "development_probe_job_ids": deepcopy(lineage["development_probe_job_ids"]),
            "development_probe_report_sha256s": deepcopy(lineage["development_probe_report_sha256s"]),
            "development_probe_result_manifest_sha256s": deepcopy(lineage["development_probe_result_manifest_sha256s"]),
            "development_probe_execution_bundle_sha256s": deepcopy(
                lineage["development_probe_execution_bundle_sha256s"]
            ),
            "development_probe_frozen_profiles_sha256s": deepcopy(lineage["development_probe_frozen_profiles_sha256s"]),
            "decode": deepcopy(lineage["decode"]),
            "runtime_environment_sha256": lineage["runtime_environment_sha256"],
            "development_sampling_manifest_sha256": sampling_manifest["manifest_sha256"],
            "development_sampling_groups": deepcopy(sampling_manifest["groups"]),
            "sampling_profile_id": value["sampling_profile_id"],
            "metric_profile_id": value["metric_profile_id"],
            "metric_profile_sha256": value["metric_profile_sha256"],
        }

    @classmethod
    def _attempt_family_sha256(cls, value: dict[str, Any]) -> str:
        return canonical_sha256(cls._attempt_family_authority(value))

    @staticmethod
    def _sampling_selection_authority(
        *,
        attempt_family_sha256: str,
        development_package_sha256: str,
        source_sha256: str,
        locked_profile: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            attempt_family_sha256 = require_sha256(attempt_family_sha256, "annotation attempt family sha256")
            development_package_sha256 = require_sha256(
                development_package_sha256,
                "development annotation package sha256",
            )
            source_sha256 = require_sha256(source_sha256, "annotation source sha256")
            locked_profile_id = require_safe_id(locked_profile["profile_id"], "locked profile_id")
            locked_profile_sha256 = require_sha256(locked_profile["profile_sha256"], "locked profile sha256")
            scale_by_name = {row["stratum"]: row for row in request["strata_applicability"]["scale"]}
            lighting_by_name = {row["stratum"]: row for row in request["strata_applicability"]["lighting"]}
            scale_applicability = [
                {
                    "stratum": stratum,
                    "status": scale_by_name[stratum]["status"],
                }
                for stratum in _SCALE_STRATA
            ]
            lighting_applicability = [
                {
                    "stratum": stratum,
                    "status": lighting_by_name[stratum]["status"],
                    "quota": lighting_by_name[stratum]["quota"],
                    "frame_intervals": sorted(
                        deepcopy(lighting_by_name[stratum]["frame_intervals"]),
                        key=lambda interval: (interval["start_frame"], interval["end_frame"]),
                    ),
                }
                for stratum in _LIGHTING_STRATA
            ]
        except (KeyError, TypeError, DetectorDevelopmentError) as exc:
            raise BallAnnotationServiceError(
                "invalid_sampling_authority",
                "Sampling selection authority is incomplete",
            ) from exc
        return {
            "schema_version": "1.0",
            "artifact_type": "ball_annotation_sampling_selection_authority",
            "attempt_family_sha256": attempt_family_sha256,
            "development_package_sha256": development_package_sha256,
            "source_sha256": source_sha256,
            "locked_profile_id": locked_profile_id,
            "locked_profile_sha256": locked_profile_sha256,
            "sampling_profile_id": request["sampling_profile_id"],
            "metric_profile_id": request["metric_profile_id"],
            "metric_profile_sha256": METRIC_PROFILE_SHA256,
            "target_frame_count": request["target_frame_count"],
            "scale_applicability": scale_applicability,
            "lighting_applicability": lighting_applicability,
        }

    def _resolve_development_package_binding(
        self,
        request: dict[str, Any],
        authority: dict[str, Any],
    ) -> dict[str, Any]:
        development_session_id = request["development_package_session_id"]
        development_package_sha256 = request["development_package_sha256"]
        try:
            result = self._read_final_result(development_session_id)
        except (BallAnnotationServiceError, DetectorDevelopmentError) as exc:
            raise BallAnnotationServiceError(
                "development_package_not_ready",
                "Check annotation requires an immutable finalized development package",
                status_code=409,
            ) from exc
        package = result["package"]
        if (
            package.get("data_role") != "development"
            or package.get("package_sha256") != development_package_sha256
            or package.get("may_seed_dataset_expansion") is not True
            or package.get("source") != authority["source"]
            or package.get("locked_profile") != authority["locked_profile"]
            or package.get("control_profile_id") != authority["control_profile_id"]
            or package.get("control_profile") != authority["control_profile"]
            or package.get("lineage") != authority["lineage"]
            or package.get("sampling_profile_id") != request["sampling_profile_id"]
            or package.get("metric_profile_id") != request["metric_profile_id"]
            or package.get("metric_profile_sha256") != METRIC_PROFILE_SHA256
            or not isinstance(package.get("sampling_manifest"), dict)
            or package["sampling_manifest"].get("profile_id") != request["sampling_profile_id"]
            or package["sampling_manifest"].get("metric_profile_id") != request["metric_profile_id"]
            or package["sampling_manifest"].get("metric_profile_sha256") != METRIC_PROFILE_SHA256
        ):
            raise BallAnnotationServiceError(
                "development_package_mismatch",
                "Finalized development package does not match the check attempt authority",
                status_code=409,
            )
        attempt_family_sha256 = package.get("attempt_family_sha256")
        if not isinstance(attempt_family_sha256, str) or attempt_family_sha256 != self._attempt_family_sha256(package):
            raise BallAnnotationServiceError(
                "development_package_mismatch",
                "Development attempt-family authority is invalid",
                status_code=409,
            )
        return {
            "session_id": development_session_id,
            "package_sha256": development_package_sha256,
            "attempt_family_sha256": attempt_family_sha256,
        }

    def _new_session(
        self,
        *,
        session_id: str,
        request_sha256: str,
        request: dict[str, Any],
        authority: dict[str, Any],
        development_groups: list[dict[str, Any]],
        development_package_binding: dict[str, Any] | None,
        now: str,
    ) -> dict[str, Any]:
        source = authority["source"]
        authority_frame_indices = sorted(frame["frame_index"] for frame in authority["frames"])
        if request["data_role"] == "development":
            groups = development_groups
            frames = (
                self._session_frames(
                    session_id,
                    authority["frames"],
                    groups,
                    authority["decode"]["fps"],
                )
                if authority["review_timing_valid"]
                else []
            )
            status = "annotating"
            candidate_indices = authority_frame_indices
            candidate_universe_authority = None
            selection_authority = None
            selection_seed_sha256 = canonical_sha256({"selection_seed": request_sha256})
        else:
            registry = self._read_registry()
            registry_groups = [
                self._registry_group(entry)
                for entry in registry["entries"]
                if entry.get("source_sha256") == source["sha256"]
                and entry.get("state") in {"reserved", "revealed", "scored"}
            ]
            unavailable = {group["group_id"]: group for group in [*development_groups, *registry_groups]}
            candidate_start = 0
            candidate_end = source["frame_count"] - 1
            self._validate_pre_reveal_lighting_authority(
                request,
                source_frame_count=source["frame_count"],
                candidate_start=candidate_start,
                candidate_end=candidate_end,
            )
            candidate_indices = range(candidate_start, candidate_end + 1)
            lighting_rows = [row for row in request["strata_applicability"]["lighting"] if row["quota"] > 0]
            try:
                selection_authority = self._sampling_selection_authority(
                    attempt_family_sha256=development_package_binding["attempt_family_sha256"],
                    development_package_sha256=development_package_binding["package_sha256"],
                    source_sha256=source["sha256"],
                    locked_profile=authority["locked_profile"],
                    request=request,
                )
                selection_seed_sha256 = canonical_sha256(selection_authority)
                candidate_universe_authority = build_candidate_universe_authority(
                    source_sha256=source["sha256"],
                    start_frame=candidate_start,
                    end_frame=candidate_end,
                    lighting_strata=lighting_rows,
                    excluded_groups=list(unavailable.values()),
                )
                groups = sample_unseen_temporal_groups(
                    source_sha256=source["sha256"],
                    candidate_frame_indices=candidate_indices,
                    target_count=request["target_frame_count"],
                    excluded_group_ids=set(unavailable),
                    reserved_group_ids=set(),
                    excluded_groups=list(unavailable.values()),
                    seed=selection_seed_sha256,
                    lighting_strata=lighting_rows,
                )
            except FeasibilityError as exc:
                raise BallAnnotationServiceError(
                    "predeclared_sampling_infeasible",
                    str(exc),
                    status_code=409,
                ) from exc
            frames = []
            status = "sampling_locked"
        sampling_manifest: dict[str, Any] = {
            "schema_version": "1.0",
            "artifact_type": "ball_annotation_sampling_manifest",
            "profile_id": request["sampling_profile_id"],
            "selection_profile_id": (
                TEMPORAL_BLOCK_SAMPLING_PROFILE_ID if request["data_role"] == "check" else "development_probe_frames_v1"
            ),
            "scale_stratification_mode": "post_reveal_support_gate_only",
            "lighting_stratification_mode": (
                "predeclared_frame_intervals_and_quota_v1"
                if request["data_role"] == "check"
                else "not_applicable_development_evidence"
            ),
            "selection_seed_sha256": selection_seed_sha256,
            "candidate_universe_sha256": (
                canonical_sha256(candidate_universe_authority)
                if candidate_universe_authority is not None
                else canonical_sha256(
                    {
                        "source_sha256": source["sha256"],
                        "candidate_frame_indices": candidate_indices,
                    }
                )
            ),
            "candidate_universe_start_frame": min(candidate_indices),
            "candidate_universe_end_frame": max(candidate_indices),
            "metric_profile_id": request["metric_profile_id"],
            "metric_profile_sha256": METRIC_PROFILE_SHA256,
            "data_role": request["data_role"],
            "target_frame_count": (request["target_frame_count"] if request["data_role"] == "check" else len(groups)),
            "frame_indices": [group["frame_index"] for group in groups],
            "groups": groups,
            "excluded_development_groups": development_groups if request["data_role"] == "check" else [],
            "locked_before_probe": request["data_role"] == "check",
            "source_sha256": source["sha256"],
            "locked_profile_id": authority["locked_profile"]["profile_id"],
            "locked_profile_sha256": authority["locked_profile"]["profile_sha256"],
            "strata_applicability": deepcopy(request["strata_applicability"]),
        }
        if candidate_universe_authority is not None:
            sampling_manifest["candidate_universe_authority"] = candidate_universe_authority
        if selection_authority is not None:
            sampling_manifest["selection_authority"] = selection_authority
        sampling_manifest["manifest_sha256"] = canonical_sha256(sampling_manifest)
        session = {
            "schema_version": "1.0",
            "artifact_type": "ball_annotation_session",
            "session_id": session_id,
            "idempotency_key": request_sha256,
            "request_sha256": request_sha256,
            "_normalized_session_request": deepcopy(request),
            "data_role": request["data_role"],
            "status": status,
            "stage": status,
            "source": source,
            "lineage": authority["lineage"],
            "_frame_review_proxy_authority": (
                deepcopy(authority.get("frame_review_proxy_authority"))
                if request["data_role"] == "development"
                else None
            ),
            "_detector_probe_authorities": (
                deepcopy(authority.get("detector_probe_authorities", []))
                if request["data_role"] == "development"
                else []
            ),
            "locked_profile": authority["locked_profile"],
            "control_profile_id": authority["control_profile_id"],
            "control_profile": authority["control_profile"],
            "sampling_profile_id": request["sampling_profile_id"],
            "metric_profile_id": request["metric_profile_id"],
            "metric_profile_sha256": METRIC_PROFILE_SHA256,
            "sampling_manifest": sampling_manifest,
            "operator_id": request["operator_id"],
            "applicable_scale_strata": request["applicable_scale_strata"],
            "applicable_lighting_strata": request["applicable_lighting_strata"],
            "retry_from_session_id": None,
            "retry_lineage": None,
            "development_package_binding": deepcopy(development_package_binding),
            "check_probe_job_id": None,
            "check_probe_authority": None,
            "frames": frames,
            "revisions": [],
            "final_package": None,
            "final_result": None,
            "error_code": None,
            "blocker_code": None,
            "created_at": now,
            "updated_at": now,
        }
        if not authority["review_timing_valid"]:
            session.update(
                {
                    "status": "blocked",
                    "stage": "blocked",
                    "error_code": "invalid_source_timing",
                    "blocker_code": "review_proxy_required",
                }
            )
        if request["data_role"] == "development":
            session["attempt_family_sha256"] = self._attempt_family_sha256(session)
        else:
            if not isinstance(development_package_binding, dict):
                raise BallAnnotationServiceError(
                    "development_package_required",
                    "Check annotation requires a finalized eligible development package",
                    status_code=400,
                )
            session["attempt_family_sha256"] = development_package_binding["attempt_family_sha256"]
        if request["data_role"] == "check":
            self._record_groups(
                session_id,
                source["sha256"],
                groups,
                data_role="check",
                state="reserved",
            )
            session["_initial_check_setup_transaction"] = self._initial_check_setup_transaction(session)
        return session

    def _create_retry_session(
        self,
        *,
        session_id: str,
        request_sha256: str,
        request: dict[str, Any],
        authority: dict[str, Any],
        development_package_binding: dict[str, Any] | None,
        now: str,
    ) -> dict[str, Any]:
        try:
            previous = self._load_session(request["retry_from_session_id"])
        except BallAnnotationServiceError as exc:
            if exc.code != "session_not_found":
                raise
            raise BallAnnotationServiceError(
                "invalid_retry",
                "retry_from_session_id must identify a blocked session with the same data role",
                status_code=400,
            ) from exc
        if previous["data_role"] != request["data_role"] or previous["status"] != "blocked":
            raise BallAnnotationServiceError(
                "invalid_retry",
                "retry_from_session_id must identify a blocked session with the same data role",
                status_code=400,
            )
        for field in (
            "source",
            "locked_profile",
            "control_profile_id",
            "control_profile",
            "sampling_profile_id",
            "metric_profile_id",
        ):
            expected = authority[field] if field in authority else request[field]
            if previous[field] != expected:
                raise BallAnnotationServiceError("retry_lineage_mismatch", "A retry must preserve frozen authority")
        previous_lineage = previous["lineage"]
        current_lineage = authority["lineage"]
        if previous_lineage.get("parent_trial_id") != current_lineage.get("parent_trial_id") or set(
            previous_lineage.get("development_probe_frozen_profiles_sha256s", {}).values()
        ) != set(current_lineage.get("development_probe_frozen_profiles_sha256s", {}).values()):
            raise BallAnnotationServiceError(
                "retry_lineage_mismatch",
                "A retry must preserve frozen parent-trial and model/profile authority",
            )
        if previous.get("development_package_binding") != development_package_binding:
            raise BallAnnotationServiceError(
                "retry_lineage_mismatch",
                "A retry must preserve the finalized development package binding",
            )
        previous_blocker = previous.get("blocker_code")
        if previous_blocker == "review_proxy_required":
            if (
                not authority["review_timing_valid"]
                or not authority["frames"]
                or any(frame.get("_proxy_binding") is None for frame in authority["frames"])
            ):
                raise BallAnnotationServiceError(
                    "retry_lineage_mismatch",
                    "A decode-upgrade retry requires a complete verified review proxy",
                )
            retry_mode = "review_proxy_decode_upgrade"
        else:
            if request["data_role"] == "development":
                raise BallAnnotationServiceError(
                    "invalid_retry",
                    "Development retries are limited to verified review-proxy upgrades",
                    status_code=400,
                )
            if previous_lineage.get("decode") != current_lineage.get("decode"):
                raise BallAnnotationServiceError(
                    "retry_lineage_mismatch",
                    "A worker retry must preserve frozen source decode authority",
                )
            retry_mode = "same_authority" if previous_lineage == current_lineage else "worker_runtime_reexecution"
        if previous["sampling_manifest"]["strata_applicability"] != request["strata_applicability"]:
            raise BallAnnotationServiceError(
                "retry_lineage_mismatch",
                "A retry must preserve frozen sampling and lighting authority",
            )
        if request["data_role"] == "check" and (
            previous["sampling_manifest"]["target_frame_count"] != request["target_frame_count"]
        ):
            raise BallAnnotationServiceError(
                "retry_lineage_mismatch",
                "A retry must preserve frozen sampling and lighting authority",
            )
        if request["data_role"] == "development":
            previous_jobs = previous_lineage.get("development_probe_job_ids")
            current_jobs = current_lineage.get("development_probe_job_ids")
            lineage_maps = (
                "development_probe_report_sha256s",
                "development_probe_result_manifest_sha256s",
                "development_probe_execution_bundle_sha256s",
                "development_probe_frozen_profiles_sha256s",
            )
            same_decode_upgrade = previous_lineage.get("decode") == current_lineage.get("decode")
            if not same_decode_upgrade:
                previous_decode = deepcopy(previous_lineage.get("decode"))
                current_decode = deepcopy(current_lineage.get("decode"))
                if isinstance(previous_decode, dict) and isinstance(current_decode, dict):
                    previous_decode.pop("position_verification", None)
                    current_decode.pop("position_verification", None)
                    same_decode_upgrade = (
                        previous_decode == current_decode
                        and current_lineage.get("decode", {}).get("position_verification")
                        == "verified_review_proxy_frame_index_mapping_v1"
                    )
            if (
                not isinstance(previous_jobs, list)
                or not isinstance(current_jobs, list)
                or len(current_jobs) != len(previous_jobs) + 1
                or current_jobs[: len(previous_jobs)] != previous_jobs
                or not same_decode_upgrade
                or any(
                    not isinstance(previous_lineage.get(field), dict)
                    or not isinstance(current_lineage.get(field), dict)
                    or any(
                        current_lineage[field].get(job_id) != previous_lineage[field].get(job_id)
                        for job_id in previous_jobs
                    )
                    for field in lineage_maps
                )
            ):
                raise BallAnnotationServiceError(
                    "retry_lineage_mismatch",
                    "A review-proxy upgrade must preserve source, profile, semantic decode, and label authority",
                )
            parent_job_id = previous_jobs[-1]
            child_job_id = current_jobs[-1]
            child_job = self._get_probe(child_job_id)
            parent_job = self._get_probe(parent_job_id)
            child_report = child_job.get("report")
            child_lineage = child_report.get("lineage") if isinstance(child_report, dict) else None
            child_frozen = child_job.get("frozen_request")
            upgrade = child_frozen.get("review_proxy_upgrade") if isinstance(child_frozen, dict) else None
            inherited = upgrade.get("inherited_evidence") if isinstance(upgrade, dict) else None
            try:
                from football_tracking.detector_probe import (
                    semantic_probe_intent_sha256,
                )

                parent_semantic = semantic_probe_intent_sha256(parent_job["frozen_request"])
                child_semantic = semantic_probe_intent_sha256(child_frozen)
            except (DetectorDevelopmentError, KeyError, TypeError) as exc:
                raise BallAnnotationServiceError(
                    "retry_lineage_mismatch",
                    "Review-proxy child semantic authority is invalid",
                ) from exc
            if (
                child_job.get("status") != "ready"
                or child_job.get("retry_from_job_id") != parent_job_id
                or child_job.get("retry_kind") != "review_proxy_decode_upgrade"
                or child_frozen.get("retry_kind") != "review_proxy_decode_upgrade"
                or not isinstance(child_lineage, dict)
                or child_lineage.get("review_proxy_upgrade") != upgrade
                or not isinstance(inherited, dict)
                or inherited.get("parent_probe_job_id") != parent_job_id
                or inherited.get("parent_probe_request_sha256") != parent_job.get("request_sha256")
                or inherited.get("parent_probe_intent_sha256") != parent_job.get("intent_sha256")
                or inherited.get("parent_probe_semantic_intent_sha256") != parent_semantic
                or child_semantic != parent_semantic
                or child_job.get("semantic_intent_sha256") != parent_semantic
            ):
                raise BallAnnotationServiceError(
                    "retry_lineage_mismatch",
                    "Development repair requires exactly one explicit semantic-preserving proxy child",
                )
            current_groups = self._development_groups(
                authority["frames"],
                authority["source"]["sha256"],
            )
            if (
                current_groups != previous["sampling_manifest"]["groups"]
                or [frame["frame_index"] for frame in authority["frames"]]
                != previous["sampling_manifest"]["frame_indices"]
            ):
                raise BallAnnotationServiceError(
                    "retry_lineage_mismatch",
                    "A review-proxy upgrade must preserve the original development frame groups",
                )
            retry_frames = self._session_frames(
                session_id,
                authority["frames"],
                current_groups,
                authority["decode"]["fps"],
            )
            retry_status = "annotating"
        else:
            retry_frames = []
            retry_status = "sampling_locked"
        session = deepcopy(previous)
        session.update(
            {
                "session_id": session_id,
                "idempotency_key": request_sha256,
                "request_sha256": request_sha256,
                "_normalized_session_request": deepcopy(request),
                "operator_id": request["operator_id"],
                "applicable_scale_strata": request["applicable_scale_strata"],
                "applicable_lighting_strata": request["applicable_lighting_strata"],
                "status": retry_status,
                "stage": retry_status,
                "retry_from_session_id": previous["session_id"],
                "retry_lineage": {
                    "mode": retry_mode,
                    "previous_session_id": previous["session_id"],
                    "previous_error_code": previous.get("error_code"),
                    "previous_blocker_code": previous_blocker,
                    "previous_lineage_sha256": canonical_sha256(previous_lineage),
                    "current_lineage_sha256": canonical_sha256(current_lineage),
                    "sampling_manifest_sha256": previous["sampling_manifest"]["manifest_sha256"],
                },
                "check_probe_job_id": None,
                "check_probe_authority": None,
                "frames": retry_frames,
                "revisions": [],
                "final_package": None,
                "final_result": None,
                "error_code": None,
                "blocker_code": None,
                "created_at": now,
                "updated_at": now,
            }
        )
        session["lineage"] = deepcopy(current_lineage)
        session["_frame_review_proxy_authority"] = (
            deepcopy(authority.get("frame_review_proxy_authority")) if request["data_role"] == "development" else None
        )
        session["_detector_probe_authorities"] = (
            deepcopy(authority.get("detector_probe_authorities", [])) if request["data_role"] == "development" else []
        )
        if request["data_role"] == "development":
            session["attempt_family_sha256"] = self._attempt_family_sha256(session)
        else:
            session["_retry_from_probe_job_id"] = previous.get("check_probe_job_id")
            session["_retry_reservation_snapshot"] = self._retry_reservation_snapshot(previous)
            session["_retry_reservation_transfer_updated_at"] = now
        return session

    def _reconcile_session(self, session: dict[str, Any]) -> dict[str, Any]:
        if session.get("_initial_check_setup_transaction") is not None:
            session = self._recover_initial_check_setup_transaction(session)
        if session["data_role"] == "check":
            self._require_verified_sampling_lock(session)
        session = self._reconcile_propagation_confirmations(session)
        if session["status"] == "finalizing":
            session, _result = self._complete_finalization(session)
            return session
        if session["data_role"] != "check" or session["status"] in {"annotating", "finalized", "blocked"}:
            return session
        if session["check_probe_job_id"] is None:
            request = {
                "parent_trial_id": session["lineage"]["parent_trial_id"],
                "profile_ids": sorted([session["locked_profile"]["profile_id"], session["control_profile_id"]]),
                "frame_indices": session["sampling_manifest"]["frame_indices"],
                "top_k": 5,
                "annotation_sampling_manifest_sha256": session["sampling_manifest"]["manifest_sha256"],
                "_annotation_session_id": session["session_id"],
            }
            retry_from = session.get("_retry_from_probe_job_id")
            if retry_from is not None:
                request["retry_from_job_id"] = retry_from
            # The sampling manifest and group reservation have already been
            # atomically persisted before this server-owned T2 call.
            created = self._create_probe(request)
            job_id = created.get("job_id")
            if not isinstance(job_id, str):
                raise BallAnnotationServiceError(
                    "invalid_probe_response", "Detector probe did not return a job identity"
                )
            session["check_probe_job_id"] = require_safe_id(job_id, "check probe job_id")
        job = self._get_probe(session["check_probe_job_id"])
        status = job.get("status")
        if status in _ACTIVE_CHECK_STATUSES:
            session["status"] = f"check_probe_{status}"
            session["stage"] = session["status"]
            session["updated_at"] = utc_now_iso()
            self._persist_session(session)
            return session
        if status in _TERMINAL_CHECK_FAILURES:
            session["status"] = "blocked"
            session["stage"] = "blocked"
            session["error_code"] = job.get("error_code") or f"check_probe_{status}"
            session["blocker_code"] = (
                "review_proxy_required"
                if session["error_code"] in _REVIEW_PROXY_REQUIRED_ERRORS
                else job.get("blocker_code")
            )
            session["updated_at"] = utc_now_iso()
            self._persist_session(session)
            return session
        if status != "ready":
            raise BallAnnotationServiceError("invalid_probe_status", "Check probe status is invalid")
        authority = self._resolve_ready_jobs([job], session["locked_profile"]["profile_id"])
        check_decode = deepcopy(authority["decode"])
        frozen_decode = deepcopy(session["lineage"]["decode"])
        check_position = check_decode.pop("position_verification", None)
        frozen_position = frozen_decode.pop("position_verification", None)
        same_check_decode = check_decode == frozen_decode and {
            check_position,
            frozen_position,
        } <= {
            "opencv_next_frame_index_with_0.25_tolerance",
            "verified_review_proxy_frame_index_mapping_v1",
        }
        if (
            authority["source"] != session["source"]
            or not same_check_decode
            or authority["locked_profile"] != session["locked_profile"]
            or authority["control_profile"] != session["control_profile"]
            or authority["lineage"]["parent_trial_id"] != session["lineage"]["parent_trial_id"]
        ):
            raise BallAnnotationServiceError(
                "check_probe_authority_mismatch", "Check probe source and decode authority changed"
            )
        self._validate_check_probe_intent(session, job, authority)
        expected_frames = session["sampling_manifest"]["frame_indices"]
        if [frame["frame_index"] for frame in authority["frames"]] != expected_frames:
            raise BallAnnotationServiceError(
                "check_probe_frame_mismatch", "Check probe did not return the frozen frame set"
            )
        if not authority["review_timing_valid"]:
            session.update(
                {
                    "status": "blocked",
                    "stage": "blocked",
                    "error_code": "invalid_source_timing",
                    "blocker_code": "review_proxy_required",
                    "frames": [],
                    "check_probe_authority": None,
                    "updated_at": utc_now_iso(),
                }
            )
            self._persist_session(session)
            return session
        session["_frame_review_proxy_authority"] = deepcopy(authority.get("frame_review_proxy_authority"))
        session["_detector_probe_authorities"] = deepcopy(authority.get("detector_probe_authorities", []))
        session["frames"] = self._session_frames(
            session["session_id"],
            authority["frames"],
            session["sampling_manifest"]["groups"],
            session["source"]["fps"],
        )
        session["status"] = "annotating"
        session["stage"] = "annotating"
        session["updated_at"] = utc_now_iso()
        report = job["report"]
        lineage = report["lineage"]
        session["check_probe_authority"] = {
            "job_id": job["job_id"],
            "request_sha256": require_sha256(job.get("request_sha256"), "check request sha256"),
            "intent_sha256": require_sha256(job.get("intent_sha256"), "check intent sha256"),
            "result_manifest_sha256": require_sha256(job.get("result_manifest_sha256"), "check result manifest sha256"),
            "report_sha256": require_sha256(report.get("report_sha256"), "check report sha256"),
            "parent_trial_id": lineage["parent_trial_id"],
            "runtime_environment_sha256": require_sha256(
                lineage.get("runtime_environment_sha256"), "check runtime sha256"
            ),
            "execution_bundle_sha256": require_sha256(
                lineage.get("execution_bundle_sha256"), "check execution bundle sha256"
            ),
            "frozen_profiles_sha256": require_sha256(
                lineage.get("frozen_profiles_sha256"), "check frozen profiles sha256"
            ),
            "locked_profile": deepcopy(authority["locked_profile"]),
            "control_profile": deepcopy(authority["control_profile"]),
        }
        self._transition_session_groups(session["session_id"], "revealed")
        self._persist_session(session)
        return session

    def _resolve_development_authority(self, job_ids: list[str], locked_profile_id: str) -> dict[str, Any]:
        jobs = []
        for job_id in job_ids:
            try:
                job = self._get_probe(job_id)
            except (KeyError, DetectorDevelopmentError) as exc:
                raise BallAnnotationServiceError(
                    "development_probe_not_found", "A development detector probe was not found", status_code=404
                ) from exc
            if job.get("status") in _TERMINAL_CHECK_FAILURES and job.get("error_code") in _REVIEW_PROXY_REQUIRED_ERRORS:
                raise BallAnnotationServiceError(
                    "review_proxy_required",
                    "The source decode is not stable enough for annotation; create and verify a review proxy first",
                    status_code=409,
                )
            jobs.append(job)
        return self._resolve_ready_jobs(jobs, locked_profile_id)

    def _resolve_ready_jobs(self, jobs: list[dict[str, Any]], locked_profile_id: str) -> dict[str, Any]:
        known_job_ids = {job.get("job_id") for job in jobs}
        historical_parents: list[dict[str, Any]] = []
        for job in jobs:
            report = job.get("report")
            upgrade = report.get("lineage", {}).get("review_proxy_upgrade") if isinstance(report, dict) else None
            inherited = upgrade.get("inherited_evidence") if isinstance(upgrade, dict) else None
            parent_job_id = inherited.get("parent_probe_job_id") if isinstance(inherited, dict) else None
            if parent_job_id is not None and parent_job_id not in known_job_ids:
                try:
                    parent_job = self._get_probe(parent_job_id)
                except Exception as exc:
                    raise BallAnnotationServiceError(
                        "invalid_review_proxy",
                        "Review-proxy child historical parent is unavailable",
                    ) from exc
                historical_parents.append(parent_job)
                known_job_ids.add(parent_job_id)
        jobs = [*historical_parents, *jobs]
        jobs = self._ordered_probe_retry_chain(jobs)
        authorities: list[dict[str, Any]] = []
        for job in jobs:
            if job.get("status") != "ready" or not isinstance(job.get("report"), dict):
                raise BallAnnotationServiceError(
                    "development_probe_not_ready", "Annotation sessions require ready T2 detector probes"
                )
            report = job["report"]
            published_report_sha = require_sha256(report.get("report_sha256"), "probe report sha256")
            if (
                canonical_sha256({key: value for key, value in report.items() if key != "report_sha256"})
                != published_report_sha
            ):
                raise BallAnnotationServiceError(
                    "probe_report_digest_mismatch", "Detector probe report digest is invalid"
                )
            result_manifest_sha = require_sha256(job.get("result_manifest_sha256"), "probe result manifest sha256")
            probe_result_manifest = None
            source = self._source_binding(report)
            decode = self._decode_binding(report)
            if (
                source["width"] != decode["width"]
                or source["height"] != decode["height"]
                or source["frame_count"] != decode["frame_count"]
            ):
                raise BallAnnotationServiceError(
                    "probe_source_decode_mismatch",
                    "Detector probe source and decode dimensions changed",
                )
            profiles = self._profile_bindings(report)
            if locked_profile_id not in profiles:
                raise BallAnnotationServiceError(
                    "locked_profile_missing", "Locked profile is absent from a development probe"
                )
            runtime_environment_sha256 = require_sha256(
                report.get("lineage", {}).get("runtime_environment_sha256"),
                "probe runtime environment sha256",
            )
            execution_bundle_sha256 = require_sha256(
                report.get("lineage", {}).get("execution_bundle_sha256"),
                "probe execution bundle sha256",
            )
            frozen_profiles_sha256 = require_sha256(
                report.get("lineage", {}).get("frozen_profiles_sha256"),
                "probe frozen profiles sha256",
            )
            frames = self._probe_frames(
                job["job_id"],
                report,
                locked_profile_id,
                source,
                decode,
                probe_report_sha256=published_report_sha,
                probe_result_manifest_sha256=result_manifest_sha,
                runtime_environment_sha256=runtime_environment_sha256,
            )
            review_proxy_manifest = report.get("review_proxy_manifest")
            if review_proxy_manifest is not None:
                try:
                    review_proxy_manifest = validate_review_proxy_manifest(review_proxy_manifest)
                except ReviewProxyError as exc:
                    raise BallAnnotationServiceError("invalid_review_proxy", str(exc)) from exc
            try:
                probe_result_manifest, rebuilt_result_manifest_sha256 = build_detector_probe_result_manifest_authority(
                    report
                )
                probe_job_authority = build_detector_probe_job_authority(job)
            except BallFrameEvidenceError as exc:
                raise BallAnnotationServiceError("invalid_probe_result_authority", str(exc)) from exc
            if rebuilt_result_manifest_sha256 != result_manifest_sha:
                raise BallAnnotationServiceError(
                    "probe_result_manifest_mismatch",
                    "Detector probe report does not rebuild the published result manifest",
                )
            authorities.append(
                {
                    "job_id": job["job_id"],
                    "report_sha256": published_report_sha,
                    "result_manifest_sha256": result_manifest_sha,
                    "report": deepcopy(report),
                    "result_manifest": probe_result_manifest,
                    "source": source,
                    "decode": decode,
                    "profiles": profiles,
                    "frames": frames,
                    "review_timing_valid": self._frames_have_review_timing(frames),
                    "parent_trial_id": report.get("lineage", {}).get("parent_trial_id"),
                    "runtime_environment_sha256": report.get("lineage", {}).get("runtime_environment_sha256"),
                    "execution_bundle_sha256": execution_bundle_sha256,
                    "frozen_profiles_sha256": frozen_profiles_sha256,
                    "review_proxy_manifest": review_proxy_manifest,
                    "probe_job_authority": probe_job_authority,
                }
            )
        reference = authorities[0]
        reference_frame_indices = [frame["frame_index"] for frame in reference["frames"]]
        if len(reference_frame_indices) > 50:
            raise BallAnnotationServiceError(
                "development_frame_set_too_large",
                "A development annotation attempt is limited to 50 primary frames",
                status_code=409,
            )
        for authority in authorities[1:]:
            same_decode_authority = authority["decode"] == reference["decode"]
            if not same_decode_authority:
                reference_decode = deepcopy(reference["decode"])
                current_decode = deepcopy(authority["decode"])
                reference_decode.pop("position_verification", None)
                current_decode.pop("position_verification", None)
                same_decode_authority = (
                    reference_decode == current_decode
                    and authority["decode"].get("position_verification")
                    == "verified_review_proxy_frame_index_mapping_v1"
                )
            if (
                authority["source"] != reference["source"]
                or not same_decode_authority
                or authority["parent_trial_id"] != reference["parent_trial_id"]
                or authority["profiles"] != reference["profiles"]
                or authority["frozen_profiles_sha256"] != reference["frozen_profiles_sha256"]
                or [frame["frame_index"] for frame in authority["frames"]] != reference_frame_indices
            ):
                raise BallAnnotationServiceError(
                    "development_authority_mismatch",
                    "Development probes must share exact source, decode, profile, and frozen frame-set authority",
                )
        frame_by_index: dict[int, dict[str, Any]] = {}
        for authority_index, authority in enumerate(authorities):
            allow_proxy_upgrade = (
                authority_index > 0
                and authorities[authority_index - 1]["review_timing_valid"] is False
                and authority["review_timing_valid"] is True
            )
            for frame in authority["frames"]:
                existing = frame_by_index.get(frame["frame_index"])
                if existing is not None:
                    existing_proxy = existing.get("_proxy_binding")
                    current_proxy = frame.get("_proxy_binding")
                    same_frame_authority = self._retry_frame_authority_payload(
                        existing
                    ) == self._retry_frame_authority_payload(frame)
                    verified_timing_upgrade = allow_proxy_upgrade and self._is_verified_legacy_timing_upgrade(
                        existing, frame
                    )
                    if not (same_frame_authority or verified_timing_upgrade) or not (
                        current_proxy == existing_proxy
                        or (allow_proxy_upgrade and existing_proxy is None and isinstance(current_proxy, dict))
                    ):
                        raise BallAnnotationServiceError(
                            "retry_frame_mismatch",
                            "Probe retry source, timing, decode, candidate, or proxy evidence changed",
                        )
                frame_by_index[frame["frame_index"]] = frame
        effective = authorities[-1]
        effective_proxy_manifest = effective.get("review_proxy_manifest")
        frame_review_proxy_authority = None
        if isinstance(effective_proxy_manifest, dict):
            effective_upgrade = effective["report"].get("lineage", {}).get("review_proxy_upgrade")
            inherited = effective_upgrade.get("inherited_evidence") if isinstance(effective_upgrade, dict) else None
            frame_review_proxy_authority = {
                "probe_job_id": effective["job_id"],
                "probe_report_sha256": effective["report_sha256"],
                "probe_result_manifest_sha256": effective["result_manifest_sha256"],
                "probe_report": deepcopy(effective["report"]),
                "probe_result_manifest": deepcopy(effective["result_manifest"]),
                "review_proxy_manifest": deepcopy(effective_proxy_manifest),
            }
            if isinstance(effective_upgrade, dict):
                historical = next(
                    (
                        authority
                        for authority in authorities
                        if isinstance(inherited, dict) and authority["job_id"] == inherited.get("parent_probe_job_id")
                    ),
                    None,
                )
                if not isinstance(inherited, dict) or historical is None:
                    raise BallAnnotationServiceError(
                        "invalid_review_proxy",
                        "Review-proxy child is missing its audited historical parent",
                    )
                historical_result = historical.get("result_manifest")
                if historical_result is None:
                    try:
                        historical_result, historical_result_sha256 = build_detector_probe_result_manifest_authority(
                            historical["report"]
                        )
                    except BallFrameEvidenceError as exc:
                        raise BallAnnotationServiceError("invalid_probe_result_authority", str(exc)) from exc
                    if historical_result_sha256 != historical["result_manifest_sha256"]:
                        raise BallAnnotationServiceError(
                            "probe_result_manifest_mismatch",
                            "Historical detector probe does not rebuild its published result manifest",
                        )
                frame_review_proxy_authority["historical_probe_authority"] = {
                    "probe_job_id": historical["job_id"],
                    "probe_report_sha256": historical["report_sha256"],
                    "probe_result_manifest_sha256": historical["result_manifest_sha256"],
                    "probe_report": deepcopy(historical["report"]),
                    "probe_result_manifest": deepcopy(historical_result),
                    "source_frame_evidence_sha256": inherited["source_frame_evidence_sha256"],
                    "candidate_evidence_sha256": inherited["candidate_evidence_sha256"],
                }
        available_profiles = effective["profiles"]
        other_profile_ids = sorted(profile_id for profile_id in available_profiles if profile_id != locked_profile_id)
        if not other_profile_ids:
            raise BallAnnotationServiceError("control_profile_missing", "A deterministic control profile is required")
        preferred = "current-coco-yolov8n-direct"
        control_profile_id = preferred if preferred in other_profile_ids else other_profile_ids[0]
        source = {**effective["source"], "fps": effective["decode"]["fps"]}
        lineage = {
            "parent_trial_id": effective["parent_trial_id"],
            "development_probe_job_ids": [authority["job_id"] for authority in authorities],
            "development_probe_report_sha256s": {
                authority["job_id"]: authority["report_sha256"] for authority in authorities
            },
            "development_probe_result_manifest_sha256s": {
                authority["job_id"]: authority["result_manifest_sha256"] for authority in authorities
            },
            "development_probe_execution_bundle_sha256s": {
                authority["job_id"]: authority["execution_bundle_sha256"] for authority in authorities
            },
            "development_probe_frozen_profiles_sha256s": {
                authority["job_id"]: authority["frozen_profiles_sha256"] for authority in authorities
            },
            "decode": effective["decode"],
            "runtime_environment_sha256": effective["runtime_environment_sha256"],
        }
        return {
            "source": source,
            "decode": effective["decode"],
            "lineage": lineage,
            "locked_profile": available_profiles[locked_profile_id],
            "control_profile_id": control_profile_id,
            "control_profile": available_profiles[control_profile_id],
            "frames": [frame_by_index[index] for index in sorted(frame_by_index)],
            "frame_review_proxy_authority": frame_review_proxy_authority,
            "detector_probe_authorities": [deepcopy(authority["probe_job_authority"]) for authority in authorities],
            "review_timing_valid": self._frames_have_review_timing(
                [frame_by_index[index] for index in sorted(frame_by_index)]
            ),
        }

    @staticmethod
    def _ordered_probe_retry_chain(
        jobs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if len(jobs) <= 1:
            return jobs
        by_id = {job.get("job_id"): job for job in jobs}
        if len(by_id) != len(jobs) or None in by_id:
            raise BallAnnotationServiceError(
                "invalid_probe_retry_chain",
                "Development probe retry identities are invalid",
            )
        starts = [job for job in jobs if job.get("retry_from_job_id") not in by_id]
        children: dict[str, list[dict[str, Any]]] = {}
        for job in jobs:
            parent = job.get("retry_from_job_id")
            if parent in by_id:
                children.setdefault(parent, []).append(job)
        if len(starts) != 1 or any(len(rows) != 1 for rows in children.values()):
            raise BallAnnotationServiceError(
                "invalid_probe_retry_chain",
                "Development probes must form one linear exact retry chain",
            )
        ordered: list[dict[str, Any]] = []
        current = starts[0]
        while current is not None:
            if current in ordered:
                raise BallAnnotationServiceError(
                    "invalid_probe_retry_chain",
                    "Development probe retry chain contains a cycle",
                )
            ordered.append(current)
            next_rows = children.get(current["job_id"], [])
            current = next_rows[0] if next_rows else None
        if len(ordered) != len(jobs):
            raise BallAnnotationServiceError(
                "invalid_probe_retry_chain",
                "Development probes do not form one complete retry chain",
            )
        return ordered

    @staticmethod
    def _retry_frame_authority_payload(frame: dict[str, Any]) -> dict[str, Any]:
        payload = {
            key: deepcopy(frame.get(key))
            for key in (
                "frame_index",
                "source_frame_sha256",
                "source_frame_size_bytes",
                "_locked_evidence_sha256",
                "_requested_decode_mode",
                "_effective_decode_mode",
                "_decoded_frame_position",
                "_position_verification",
                "_decoder_reported_pos_msec",
                "_decoder_timing_observation_method",
                "_true_presentation_timestamp",
            )
        }
        payload["suggested_candidates"] = [
            {
                key: deepcopy(value)
                for key, value in candidate.items()
                if key not in {"suggestion_job_id", "suggestion_sha256"}
            }
            for candidate in frame.get("suggested_candidates", [])
        ]
        return payload

    @classmethod
    def _is_verified_legacy_timing_upgrade(
        cls,
        previous: dict[str, Any],
        current: dict[str, Any],
    ) -> bool:
        current_time = current.get("_decoder_reported_pos_msec")
        proxy = current.get("_proxy_binding")
        proxy_source = proxy.get("source_frame") if isinstance(proxy, dict) else None
        if (
            previous.get("_proxy_binding") is not None
            or current_time is not None
            or current.get("_decoder_timing_observation_method") is not None
            or not isinstance(proxy_source, dict)
            or proxy_source.get("frame_index") != current.get("frame_index")
            or proxy_source.get("sha256") != current.get("source_frame_sha256")
            or proxy_source.get("timing_status") != "not_collected"
            or proxy_source.get("decoder_reported_pos_msec") is not None
        ):
            return False
        previous_authority = cls._retry_frame_authority_payload(previous)
        current_authority = cls._retry_frame_authority_payload(current)
        for field in (
            "_position_verification",
            "_decoder_reported_pos_msec",
            "_decoder_timing_observation_method",
        ):
            previous_authority.pop(field)
            current_authority.pop(field)
        return previous_authority == current_authority

    @staticmethod
    def _frames_have_review_timing(frames: list[dict[str, Any]]) -> bool:
        ordered = sorted(frames, key=lambda frame: frame["frame_index"])
        if len(ordered) < 2:
            return True
        source_times = [frame.get("_decoder_reported_pos_msec") for frame in ordered]
        if all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
            for value in source_times
        ) and all(current > previous for previous, current in zip(source_times, source_times[1:])):
            return True
        proxy_times = []
        for frame in ordered:
            proxy = frame.get("_proxy_binding")
            if not isinstance(proxy, dict):
                return False
            proxy_times.append(proxy.get("proxy_frame", {}).get("cfr_time_msec"))
        return all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
            for value in proxy_times
        ) and all(current > previous for previous, current in zip(proxy_times, proxy_times[1:]))

    @staticmethod
    def _source_binding(report: dict[str, Any]) -> dict[str, Any]:
        source = report.get("source")
        if not isinstance(source, dict):
            raise BallAnnotationServiceError("invalid_probe_source", "Detector probe source binding is missing")
        result = {
            "source_id": require_safe_id(source.get("source_id"), "source_id"),
            "sha256": require_sha256(source.get("sha256"), "source sha256"),
            "file_identity_sha256": require_sha256(source.get("file_identity_sha256"), "source identity sha256"),
            "size_bytes": BallAnnotationService._positive_int(source.get("size_bytes"), "source size"),
            "width": BallAnnotationService._positive_int(source.get("width"), "source width"),
            "height": BallAnnotationService._positive_int(source.get("height"), "source height"),
            "frame_count": BallAnnotationService._positive_int(source.get("frame_count"), "source frame count"),
            "tracking_contract_sha256": require_sha256(
                source.get("tracking_contract_sha256"), "tracking contract sha256"
            ),
        }
        relative_path = source.get("relative_path")
        tracking_path = source.get("tracking_contract_relative_path")
        if (
            not isinstance(relative_path, str)
            or not relative_path
            or not isinstance(tracking_path, str)
            or not tracking_path
        ):
            raise BallAnnotationServiceError("invalid_probe_source", "Detector probe source paths are incomplete")
        result["relative_path"] = relative_path
        result["tracking_contract_relative_path"] = tracking_path
        return result

    @staticmethod
    def _decode_binding(report: dict[str, Any]) -> dict[str, Any]:
        decode = report.get("decode")
        if not isinstance(decode, dict):
            raise BallAnnotationServiceError("invalid_probe_decode", "Detector probe decode binding is missing")
        fps = decode.get("fps")
        if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(float(fps)) or fps <= 0:
            raise BallAnnotationServiceError("invalid_probe_decode", "Detector probe FPS is invalid")
        result = {
            "width": BallAnnotationService._positive_int(decode.get("width"), "decode width"),
            "height": BallAnnotationService._positive_int(decode.get("height"), "decode height"),
            "frame_count": BallAnnotationService._positive_int(decode.get("frame_count"), "decode frame count"),
            "fps": float(fps),
            "requested_decode_mode": decode.get("requested_decode_mode"),
            "effective_decode_mode": decode.get("effective_decode_mode"),
            "position_verification": decode.get("position_verification"),
        }
        if (
            result["requested_decode_mode"] not in {"sequential", "preroll", "direct"}
            or result["effective_decode_mode"]
            not in {"sequential", "preroll_verified", "direct_verified", "sequential_fallback"}
            or result["position_verification"]
            != (
                "verified_review_proxy_frame_index_mapping_v1"
                if report.get("review_proxy_manifest") is not None
                else "opencv_next_frame_index_with_0.25_tolerance"
            )
        ):
            raise BallAnnotationServiceError("invalid_probe_decode", "Detector probe decode mode is not verified")
        return result

    @staticmethod
    def _profile_bindings(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
        profiles = report.get("frozen_profiles")
        if not isinstance(profiles, list) or not 2 <= len(profiles) <= 6:
            raise BallAnnotationServiceError("invalid_probe_profiles", "Detector probe profile bindings are invalid")
        result = {}
        for profile in profiles:
            if not isinstance(profile, dict):
                raise BallAnnotationServiceError("invalid_probe_profiles", "Detector probe profile binding is invalid")
            profile_id = require_safe_id(profile.get("profile_id"), "profile_id")
            descriptor = profile.get("model_descriptor")
            weights = descriptor.get("weights") if isinstance(descriptor, dict) else None
            result[profile_id] = {
                "profile_id": profile_id,
                "profile_sha256": require_sha256(profile.get("profile_sha256"), "profile sha256"),
                "model_id": require_safe_id(profile.get("model_id"), "model_id"),
                "model_version": str(profile.get("model_version")),
                "model_descriptor_sha256": require_sha256(
                    profile.get("model_descriptor_sha256"), "model descriptor sha256"
                ),
                "weights_sha256": require_sha256(
                    weights.get("sha256") if isinstance(weights, dict) else None, "weights sha256"
                ),
            }
        if len(result) != len(profiles):
            raise BallAnnotationServiceError("invalid_probe_profiles", "Detector probe contains duplicate profiles")
        return result

    @staticmethod
    def _probe_frames(
        job_id: str,
        report: dict[str, Any],
        locked_profile_id: str,
        source: dict[str, Any],
        decode: dict[str, Any],
        *,
        probe_report_sha256: str,
        probe_result_manifest_sha256: str,
        runtime_environment_sha256: str,
    ) -> list[dict[str, Any]]:
        raw_frames = report.get("frames")
        if not isinstance(raw_frames, list) or not raw_frames:
            raise BallAnnotationServiceError("invalid_probe_frames", "Detector probe frames are missing")
        upgrade = report.get("lineage", {}).get("review_proxy_upgrade")
        inherited = upgrade.get("inherited_evidence") if isinstance(upgrade, dict) else None
        if isinstance(inherited, dict):
            candidate_probe_job_id = require_safe_id(
                inherited.get("parent_probe_job_id"),
                "candidate origin probe job_id",
            )
            candidate_probe_report_sha256 = require_sha256(
                inherited.get("parent_probe_report_sha256"),
                "candidate origin probe report sha256",
            )
            candidate_probe_result_manifest_sha256 = require_sha256(
                inherited.get("parent_probe_result_manifest_sha256"),
                "candidate origin result manifest sha256",
            )
            candidate_evidence_sha256 = require_sha256(
                inherited.get("candidate_evidence_sha256"),
                "candidate origin evidence sha256",
            )
        else:
            candidate_probe_job_id = job_id
            candidate_probe_report_sha256 = probe_report_sha256
            candidate_probe_result_manifest_sha256 = probe_result_manifest_sha256
            candidate_evidence_sha256 = canonical_sha256(
                [
                    {
                        "frame_index": frame.get("frame_index"),
                        "profile_results": deepcopy(frame.get("profile_results")),
                    }
                    for frame in raw_frames
                ]
            )
        legacy_binding = _AUDITED_T2_LEGACY_REPORT_BINDINGS.get(job_id)
        legacy_timing_absent = bool(
            legacy_binding is not None
            and probe_report_sha256 == legacy_binding["report_sha256"]
            and report.get("lineage", {}).get("execution_bundle_sha256") == legacy_binding["execution_bundle_sha256"]
            and "review_proxy_manifest" not in report
            and isinstance(report.get("decode"), dict)
            and "frame_timing_observations" not in report["decode"]
        )
        proxy_manifest = report.get("review_proxy_manifest")
        if proxy_manifest is None:
            proxy_mappings: dict[int, dict[str, Any]] = {}
        else:
            try:
                proxy_manifest = validate_review_proxy_manifest(proxy_manifest)
            except (ReviewProxyError, DetectorDevelopmentError) as exc:
                raise BallAnnotationServiceError(
                    "invalid_review_proxy",
                    "Detector probe review proxy manifest is invalid",
                ) from exc
            proxy_mappings = {mapping["source_frame_index"]: mapping for mapping in proxy_manifest["mappings"]}
            if set(proxy_mappings) != {raw.get("frame_index") for raw in raw_frames if isinstance(raw, dict)}:
                raise BallAnnotationServiceError(
                    "invalid_review_proxy",
                    "Review proxy map does not cover the exact probe frame set",
                )
            proxy_source = proxy_manifest["source"]
            upgrade_binding = report.get("lineage", {}).get("review_proxy_upgrade")
            repair_evidence = upgrade_binding.get("repair_evidence") if isinstance(upgrade_binding, dict) else None
            if (
                proxy_source.get("sha256") != source["sha256"]
                or proxy_source.get("file_identity_sha256") != source["file_identity_sha256"]
                or proxy_source.get("size_bytes") != source["size_bytes"]
                or proxy_source.get("width") != source["width"]
                or proxy_source.get("height") != source["height"]
                or proxy_source.get("frame_count") != source["frame_count"]
                or not math.isclose(
                    float(proxy_source.get("fps", 0.0)),
                    float(decode["fps"]),
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                or not isinstance(repair_evidence, dict)
                or proxy_manifest["decoder_fingerprint_sha256"]
                != repair_evidence.get("repair_decoder_fingerprint_sha256")
            ):
                raise BallAnnotationServiceError(
                    "invalid_review_proxy",
                    "Review proxy changed source, decode, or runtime authority",
                )
        frames = []
        for raw in raw_frames:
            if not isinstance(raw, dict):
                raise BallAnnotationServiceError("invalid_probe_frames", "Detector probe frame is invalid")
            frame_index = raw.get("frame_index")
            if (
                isinstance(frame_index, bool)
                or not isinstance(frame_index, int)
                or not 0 <= frame_index < source["frame_count"]
            ):
                raise BallAnnotationServiceError("invalid_probe_frames", "Detector probe frame index is invalid")
            integrity = raw.get("media_integrity")
            decoder_pos_msec = raw.get("decoder_reported_pos_msec")
            mapping_for_frame = proxy_mappings.get(frame_index)
            proxy_source_timing_not_collected = bool(
                isinstance(mapping_for_frame, dict)
                and mapping_for_frame.get("source_timing_status") == "not_collected"
                and mapping_for_frame.get("source_decoder_pos_msec") is None
            )
            if (
                raw.get("decoded_frame_position") != frame_index
                or raw.get("source_width") != source["width"]
                or raw.get("source_height") != source["height"]
                or raw.get("requested_decode_mode") != decode["requested_decode_mode"]
                or raw.get("effective_decode_mode") != decode["effective_decode_mode"]
                or (
                    legacy_timing_absent
                    and ("decoder_reported_pos_msec" in raw or "decoder_timing_observation_method" in raw)
                )
                or (
                    not legacy_timing_absent
                    and not proxy_source_timing_not_collected
                    and (
                        isinstance(decoder_pos_msec, bool)
                        or not isinstance(decoder_pos_msec, (int, float))
                        or not math.isfinite(float(decoder_pos_msec))
                        or raw.get("decoder_timing_observation_method")
                        != (
                            "verified_review_proxy_frame_index_mapping_v1"
                            if proxy_manifest is not None
                            else "opencv_cap_prop_pos_msec_after_verified_frame_read"
                        )
                    )
                )
                or (
                    proxy_source_timing_not_collected
                    and (decoder_pos_msec is not None or raw.get("decoder_timing_observation_method") is not None)
                )
                or not isinstance(integrity, dict)
                or integrity.get("status") != "ok"
                or integrity.get("gray") is not False
                or integrity.get("low_information") is not False
                or integrity.get("likely_corrupt") is not False
            ):
                raise BallAnnotationServiceError(
                    "probe_frame_integrity_failed", "Detector probe frame integrity failed"
                )
            url = raw.get("source_artifact_url")
            prefix = f"/api/v1/detector-probes/{job_id}/artifacts/"
            if not isinstance(url, str) or not url.startswith(prefix):
                raise BallAnnotationServiceError(
                    "invalid_probe_artifact", "Detector probe frame artifact binding is invalid"
                )
            artifact_id = require_safe_id(url[len(prefix) :], "source frame artifact_id")
            profile_results = raw.get("profile_results")
            locked = (
                next(
                    (
                        item
                        for item in profile_results
                        if isinstance(item, dict) and item.get("profile_id") == locked_profile_id
                    ),
                    None,
                )
                if isinstance(profile_results, list)
                else None
            )
            if not isinstance(locked, dict) or locked.get("status") != "completed" or locked.get("top_k") != 5:
                raise BallAnnotationServiceError(
                    "invalid_locked_profile_evidence", "Locked profile frame evidence is incomplete"
                )
            try:
                raw_candidates = validate_detector_probe_candidate_accounting(locked)
            except BallFrameEvidenceError as exc:
                raise BallAnnotationServiceError(
                    "invalid_locked_profile_evidence",
                    str(exc),
                ) from exc
            candidates = BallAnnotationService._suggested_candidates(
                frame_index,
                candidate_probe_job_id,
                locked_profile_id,
                raw_candidates,
                source["width"],
                source["height"],
            )
            locked_evidence = {
                "profile_id": locked_profile_id,
                "profile_sha256": locked.get("profile_sha256"),
                "candidate_count": locked.get("candidate_count"),
                "top_k": locked.get("top_k"),
                "raw_candidates": deepcopy(locked.get("raw_candidates")),
                "filter_reasons": deepcopy(locked.get("filter_reasons")),
                "source_frame_sha256": raw.get("source_frame_sha256"),
            }
            if proxy_manifest is None:
                proxy_binding = None
            else:
                mapping = proxy_mappings[frame_index]
                if (
                    mapping["source_frame_sha256"] != raw.get("source_frame_sha256")
                    or (
                        mapping["source_timing_status"] == "observed"
                        and not math.isclose(
                            mapping["source_decoder_pos_msec"],
                            float(decoder_pos_msec),
                            rel_tol=0.0,
                            abs_tol=proxy_manifest["map_time_tolerance_msec"],
                        )
                    )
                    or (mapping["source_timing_status"] == "not_collected" and decoder_pos_msec is not None)
                ):
                    raise BallAnnotationServiceError(
                        "invalid_review_proxy",
                        "Review proxy source-frame mapping changed exact probe evidence",
                    )
                try:
                    proxy_binding = build_nullable_proxy_binding(
                        {
                            "proxy": {
                                "sha256": proxy_manifest["proxy"]["sha256"],
                                "size_bytes": proxy_manifest["proxy"]["size_bytes"],
                                "width": proxy_manifest["proxy"]["width"],
                                "height": proxy_manifest["proxy"]["height"],
                            },
                            "map_sha256": proxy_manifest["mapping_sha256"],
                            "source_frame": {
                                "frame_index": frame_index,
                                "timing_status": mapping["source_timing_status"],
                                "decoder_reported_pos_msec": mapping["source_decoder_pos_msec"],
                                "sha256": mapping["source_frame_sha256"],
                            },
                            "proxy_frame": {
                                "frame_index": mapping["proxy_frame_index"],
                                "timing_basis": mapping["proxy_timing_basis"],
                                "cfr_time_msec": mapping["proxy_cfr_time_msec"],
                                "sha256": mapping["proxy_frame_sha256"],
                            },
                            "map_time_tolerance_msec": proxy_manifest["map_time_tolerance_msec"],
                            "declared_offset_msec": proxy_manifest["declared_offset_msec"],
                        }
                    )
                except BallFrameEvidenceError as exc:
                    raise BallAnnotationServiceError(
                        "invalid_review_proxy",
                        "Review proxy frame binding is invalid",
                    ) from exc
            frames.append(
                {
                    "frame_index": frame_index,
                    "source_frame_sha256": require_sha256(raw.get("source_frame_sha256"), "source frame sha256"),
                    "source_frame_size_bytes": BallAnnotationService._positive_int(
                        raw.get("source_frame_size_bytes"), "source frame size"
                    ),
                    "suggested_candidates": candidates,
                    "_probe_job_id": job_id,
                    "_artifact_id": artifact_id,
                    "_probe_report_sha256": probe_report_sha256,
                    "_probe_result_manifest_sha256": probe_result_manifest_sha256,
                    "_candidate_probe_job_id": candidate_probe_job_id,
                    "_candidate_probe_report_sha256": (candidate_probe_report_sha256),
                    "_candidate_probe_result_manifest_sha256": (candidate_probe_result_manifest_sha256),
                    "_candidate_evidence_sha256": candidate_evidence_sha256,
                    "_candidate_artifact_id": artifact_id,
                    "_runtime_environment_sha256": runtime_environment_sha256,
                    "_requested_decode_mode": decode["requested_decode_mode"],
                    "_effective_decode_mode": decode["effective_decode_mode"],
                    "_decoded_frame_position": frame_index,
                    "_position_verification": decode["position_verification"],
                    "_decoder_reported_pos_msec": (
                        None if legacy_timing_absent or proxy_source_timing_not_collected else float(decoder_pos_msec)
                    ),
                    "_decoder_timing_observation_method": raw.get("decoder_timing_observation_method"),
                    "_true_presentation_timestamp": deepcopy(_TRUE_PRESENTATION_TIMESTAMP_NOT_COLLECTED),
                    "_proxy_binding": proxy_binding,
                    "_locked_evidence_sha256": canonical_sha256(locked_evidence),
                }
            )
        if [frame["frame_index"] for frame in frames] != sorted({frame["frame_index"] for frame in frames}):
            raise BallAnnotationServiceError("invalid_probe_frames", "Detector probe frames must be unique and ordered")
        return frames

    @staticmethod
    def _suggested_candidates(
        frame_index: int,
        probe_job_id: str,
        profile_id: str,
        raw_candidates: Any,
        width: int,
        height: int,
    ) -> list[dict[str, Any]]:
        try:
            return normalize_detector_probe_candidates(
                frame_index=frame_index,
                probe_job_id=probe_job_id,
                profile_id=profile_id,
                raw_candidates=raw_candidates,
                width=width,
                height=height,
            )
        except BallFrameEvidenceError as exc:
            raise BallAnnotationServiceError("invalid_probe_candidates", str(exc)) from exc

    @staticmethod
    def _development_not_applicable_report(
        session: dict[str, Any],
        package: dict[str, Any],
    ) -> dict[str, Any]:
        report: dict[str, Any] = {
            "schema_version": "1.0",
            "artifact_type": "ball_feasibility_report",
            "session_id": session["session_id"],
            "attempt_family_sha256": session["attempt_family_sha256"],
            "development_package_binding": None,
            "status": "not_applicable",
            "reason": "development_package_is_not_one_time_check_evidence",
            "sealed_evidence": {
                "annotation_package_sha256": package["package_sha256"],
                "sampling_manifest_sha256": session["sampling_manifest"]["manifest_sha256"],
                "check_probe_job_id": None,
                "check_probe_report_sha256": None,
                "attempt_family_sha256": session["attempt_family_sha256"],
                "dataset_expansion_eligibility": deepcopy(package["dataset_expansion_eligibility"]),
            },
            "authorizations": {
                "may_expand_to_100_300_boxes": False,
                "trial_eligible": False,
                "source_segment_qualified": False,
                "camera_qualified": False,
                "production_approved": False,
                "full_run_authorized": False,
            },
        }
        report["report_sha256"] = canonical_sha256(report)
        return report

    @staticmethod
    def _validate_check_probe_intent(
        session: dict[str, Any],
        job: dict[str, Any],
        authority: dict[str, Any],
    ) -> None:
        frozen = job.get("frozen_request")
        report = job.get("report")
        lineage = report.get("lineage") if isinstance(report, dict) else None
        expected_profiles = sorted([session["locked_profile"]["profile_id"], session["control_profile"]["profile_id"]])
        if (
            not isinstance(frozen, dict)
            or not isinstance(lineage, dict)
            or frozen.get("parent_trial_id") != session["lineage"]["parent_trial_id"]
            or frozen.get("profile_ids") != expected_profiles
            or frozen.get("frame_indices") != session["sampling_manifest"]["frame_indices"]
            or frozen.get("annotation_sampling_manifest_sha256") != session["sampling_manifest"]["manifest_sha256"]
            or frozen.get("top_k", report.get("top_k")) != 5
            or authority["locked_profile"] != session["locked_profile"]
            or authority["control_profile"] != session["control_profile"]
        ):
            raise BallAnnotationServiceError(
                "check_probe_intent_mismatch",
                "Check probe server intent does not match the frozen session",
            )

    @staticmethod
    def _session_frames(
        session_id: str,
        frames: list[dict[str, Any]],
        groups: list[dict[str, Any]],
        fps: float,
    ) -> list[dict[str, Any]]:
        group_by_frame = {group["frame_index"]: group for group in groups}
        result = []
        for raw in frames:
            group = group_by_frame.get(raw["frame_index"])
            if group is None:
                raise BallAnnotationServiceError("frame_group_mismatch", "Frame is outside the frozen sampling groups")
            initial_etag = annotation_etag(session_id, raw["frame_index"], 0, None)
            decoder_pos_msec = raw["_decoder_reported_pos_msec"]
            result.append(
                {
                    **deepcopy(raw),
                    "source_timing_status": ("not_collected" if decoder_pos_msec is None else "observed"),
                    "decoder_reported_pos_msec": decoder_pos_msec,
                    "decoder_time_seconds": (None if decoder_pos_msec is None else decoder_pos_msec / 1000.0),
                    "display_time_seconds": raw["frame_index"] / fps,
                    "true_presentation_timestamp": deepcopy(
                        raw.get(
                            "_true_presentation_timestamp",
                            _TRUE_PRESENTATION_TIMESTAMP_NOT_COLLECTED,
                        )
                    ),
                    "proxy_binding": deepcopy(raw.get("_proxy_binding")),
                    "temporal_group_id": group["group_id"],
                    "frame_url": f"/api/v1/ball-annotation-sessions/{session_id}/frames/{raw['frame_index']}",
                    "annotation_revision": 0,
                    "annotation_etag": initial_etag,
                    "current_annotation": None,
                    "frame_role": "primary_sample",
                    "primary_sample": True,
                    "propagation_job_ids": [],
                    "propagation_suggestions": [],
                }
            )
        return result

    @staticmethod
    def _development_groups(frames: list[dict[str, Any]], source_sha256: str) -> list[dict[str, Any]]:
        return [
            {**temporal_group_for_frame(source_sha256, frame["frame_index"]), "frame_index": frame["frame_index"]}
            for frame in frames
        ]

    @staticmethod
    def _validate_pre_reveal_lighting_authority(
        request: dict[str, Any],
        *,
        source_frame_count: int,
        candidate_start: int,
        candidate_end: int,
    ) -> None:
        rows = [row for row in request["strata_applicability"]["lighting"] if row["quota"] > 0]
        insufficient = [row["stratum"] for row in rows if row["quota"] < 3]
        if insufficient:
            raise BallAnnotationServiceError(
                "predeclared_insufficient_quota",
                "Every applicable check lighting stratum requires at least 3 predeclared frames before reveal",
                status_code=400,
            )
        if sum(row["quota"] for row in rows) != request["target_frame_count"]:
            raise BallAnnotationServiceError(
                "invalid_lighting_sampling_authority",
                "Pre-reveal lighting quotas must equal target_frame_count",
                status_code=400,
            )
        intervals = sorted(
            (
                interval["start_frame"],
                interval["end_frame"],
                row["stratum"],
            )
            for row in rows
            for interval in row["frame_intervals"]
        )
        if not intervals:
            raise BallAnnotationServiceError(
                "invalid_lighting_sampling_authority",
                "Pre-reveal lighting intervals are required for check sampling",
                status_code=400,
            )
        expected_start = candidate_start
        for start, end, _stratum in intervals:
            if (
                start != expected_start
                or end < start
                or end >= source_frame_count
                or start < candidate_start
                or end > candidate_end
            ):
                raise BallAnnotationServiceError(
                    "invalid_lighting_sampling_authority",
                    "Lighting intervals must be non-overlapping, in range, and cover the complete frozen candidate universe without gaps",
                    status_code=400,
                )
            expected_start = end + 1
        if expected_start != candidate_end + 1:
            raise BallAnnotationServiceError(
                "invalid_lighting_sampling_authority",
                "Lighting intervals leave an unknown gap in the frozen candidate universe",
                status_code=400,
            )

    def _normalize_session_request(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise BallAnnotationServiceError(
                "invalid_request", "Annotation session request must be an object", status_code=400
            )
        unexpected = sorted(set(request) - _SESSION_REQUEST_FIELDS)
        if unexpected:
            raise BallAnnotationServiceError(
                "forged_annotation_authority",
                "Public annotation requests cannot provide client authority for source, frames, check jobs, candidates, or artifacts",
                status_code=400,
            )
        missing = sorted(_SESSION_REQUEST_FIELDS - set(request))
        if missing:
            raise BallAnnotationServiceError(
                "invalid_request", f"Annotation request fields are missing: {missing}", status_code=400
            )
        data_role = request.get("data_role")
        if data_role not in {"development", "check"}:
            raise BallAnnotationServiceError(
                "invalid_data_role", "data_role must be development or check", status_code=400
            )
        raw_jobs = request.get("development_probe_job_ids")
        if not isinstance(raw_jobs, list) or not 1 <= len(raw_jobs) <= 8:
            raise BallAnnotationServiceError(
                "invalid_probe_jobs", "One to eight development probe jobs are required", status_code=400
            )
        jobs = [require_safe_id(item, "development probe job_id") for item in raw_jobs]
        if len(jobs) != len(set(jobs)):
            raise BallAnnotationServiceError(
                "invalid_probe_jobs", "Development probe job IDs must be unique", status_code=400
            )
        jobs.sort()
        target = request.get("target_frame_count")
        if data_role == "check":
            if isinstance(target, bool) or not isinstance(target, int) or not 20 <= target <= 50:
                raise BallAnnotationServiceError(
                    "invalid_target_count",
                    "check target_frame_count must be between 20 and 50",
                    status_code=400,
                )
        elif target is not None:
            raise BallAnnotationServiceError(
                "invalid_target_count",
                "development target_frame_count must be null because actual ready probe frames are authoritative",
                status_code=400,
            )
        raw_development_session_id = request.get("development_package_session_id")
        raw_development_package_sha256 = request.get("development_package_sha256")
        if data_role == "development":
            if raw_development_session_id is not None or raw_development_package_sha256 is not None:
                raise BallAnnotationServiceError(
                    "invalid_development_package_binding",
                    "Development sessions cannot bind another development package",
                    status_code=400,
                )
            development_session_id = None
            development_package_sha256 = None
        else:
            development_session_id = require_safe_id(
                raw_development_session_id,
                "development package session_id",
            )
            development_package_sha256 = require_sha256(
                raw_development_package_sha256,
                "development package sha256",
            )
        if request.get("sampling_profile_id") != TEMPORAL_GROUPING_PROFILE_ID:
            raise BallAnnotationServiceError(
                "invalid_sampling_profile", "Sampling profile is unsupported", status_code=400
            )
        if request.get("metric_profile_id") != FEASIBILITY_METRIC_PROFILE_ID:
            raise BallAnnotationServiceError("invalid_metric_profile", "Metric profile is unsupported", status_code=400)
        applicability, scales, lights = self._normalize_strata_applicability(request.get("strata_applicability"))
        lighting_quota = sum(row["quota"] for row in applicability["lighting"])
        has_lighting_intervals = any(row["frame_intervals"] for row in applicability["lighting"])
        if data_role == "development" and (lighting_quota != 0 or has_lighting_intervals):
            raise BallAnnotationServiceError(
                "invalid_strata",
                "development sessions cannot freeze check lighting quotas or intervals",
                status_code=400,
            )
        if data_role == "check" and lighting_quota != target:
            raise BallAnnotationServiceError(
                "invalid_strata",
                "check lighting quotas must sum to target_frame_count",
                status_code=400,
            )
        if data_role == "check" and any(
            row["status"] == "applicable" and (row["quota"] <= 0 or not row["frame_intervals"])
            for row in applicability["lighting"]
        ):
            raise BallAnnotationServiceError(
                "invalid_strata",
                "every applicable check lighting stratum requires positive quota and intervals",
                status_code=400,
            )
        return {
            "data_role": data_role,
            "development_probe_job_ids": jobs,
            "locked_profile_id": require_safe_id(request.get("locked_profile_id"), "locked_profile_id"),
            "target_frame_count": target,
            "sampling_profile_id": TEMPORAL_GROUPING_PROFILE_ID,
            "metric_profile_id": FEASIBILITY_METRIC_PROFILE_ID,
            "operator_id": require_safe_id(request.get("operator_id"), "operator_id"),
            "strata_applicability": applicability,
            "applicable_scale_strata": scales,
            "applicable_lighting_strata": lights,
            "retry_from_session_id": (
                require_safe_id(request["retry_from_session_id"], "retry_from_session_id")
                if request.get("retry_from_session_id") is not None
                else None
            ),
            "development_package_session_id": development_session_id,
            "development_package_sha256": development_package_sha256,
        }

    @staticmethod
    def _normalize_annotation_request(request: Any) -> dict[str, Any]:
        if (
            not isinstance(request, dict)
            or not _ANNOTATION_REQUIRED_FIELDS.issubset(request)
            or not set(request).issubset(_ANNOTATION_REQUEST_FIELDS)
        ):
            raise BallAnnotationServiceError(
                "invalid_annotation_request", "Annotation revision request fields are invalid", status_code=400
            )
        operation = request.get("operation")
        if operation not in {"set", "delete", "undo"}:
            raise BallAnnotationServiceError(
                "invalid_annotation_operation", "Annotation operation is invalid", status_code=400
            )
        expected = request.get("expected_revision")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            raise BallAnnotationServiceError(
                "invalid_revision", "expected_revision must be non-negative", status_code=400
            )
        undo = request.get("undo_revision")
        if operation == "undo":
            if isinstance(undo, bool) or not isinstance(undo, int) or undo <= 0:
                raise BallAnnotationServiceError("invalid_undo", "undo_revision is required", status_code=400)
        elif undo is not None:
            raise BallAnnotationServiceError("invalid_undo", "undo_revision is only valid for undo", status_code=400)
        if operation == "set" and not isinstance(request.get("annotation"), dict):
            raise BallAnnotationServiceError("invalid_annotation", "set requires an annotation object", status_code=400)
        if operation != "set" and request.get("annotation") is not None:
            raise BallAnnotationServiceError(
                "invalid_annotation", "delete/undo cannot carry annotation content", status_code=400
            )
        suggestion_kind = request.get("suggestion_kind")
        suggestion_id = request.get("suggestion_id")
        accepted_suggestion_job_id = request.get("accepted_suggestion_job_id")
        accepted_suggestion_sha256 = request.get("accepted_suggestion_sha256")
        dismissed_suggestion_kind = request.get("dismissed_suggestion_kind")
        dismissed_suggestion_id = request.get("dismissed_suggestion_id")
        dismissed_suggestion_job_id = request.get("dismissed_suggestion_job_id")
        dismissed_suggestion_sha256 = request.get("dismissed_suggestion_sha256")
        if suggestion_id is not None and dismissed_suggestion_id is not None:
            raise BallAnnotationServiceError(
                "invalid_suggestion",
                "A revision cannot both accept and dismiss a propagation suggestion",
                status_code=400,
            )
        accepted_binding = (
            suggestion_kind,
            suggestion_id,
            accepted_suggestion_job_id,
            accepted_suggestion_sha256,
        )
        if (
            any(value is not None for value in accepted_binding)
            and not all(value is not None for value in accepted_binding)
        ) or (suggestion_kind is not None and suggestion_kind not in {"detector_candidate", "propagation"}):
            raise BallAnnotationServiceError(
                "invalid_suggestion",
                "Accepted suggestion kind, id, job, and digest must form one complete supported reference",
                status_code=400,
            )
        dismissed_binding = (
            dismissed_suggestion_kind,
            dismissed_suggestion_id,
            dismissed_suggestion_job_id,
            dismissed_suggestion_sha256,
        )
        if (
            any(value is not None for value in dismissed_binding)
            and not all(value is not None for value in dismissed_binding)
        ) or (
            dismissed_suggestion_kind is not None
            and dismissed_suggestion_kind not in {"detector_candidate", "propagation"}
        ):
            raise BallAnnotationServiceError(
                "invalid_suggestion",
                "Dismissed suggestion kind, id, job, and digest must form one complete supported reference",
                status_code=400,
            )
        if suggestion_id is not None and operation != "set":
            raise BallAnnotationServiceError(
                "invalid_suggestion",
                "suggestion_id is only valid for a set operation",
                status_code=400,
            )
        if dismissed_suggestion_id is not None and operation != "set":
            raise BallAnnotationServiceError(
                "invalid_suggestion",
                "dismissed_suggestion_id is only valid for a set operation",
                status_code=400,
            )
        return {
            "mutation_id": require_safe_id(request.get("mutation_id"), "annotation mutation_id"),
            "expected_revision": expected,
            "operation": operation,
            "undo_revision": undo,
            "annotation": deepcopy(request.get("annotation")),
            "suggestion_kind": suggestion_kind,
            "suggestion_id": (require_safe_id(suggestion_id, "suggestion_id") if suggestion_id is not None else None),
            "accepted_suggestion_job_id": (
                require_safe_id(
                    accepted_suggestion_job_id,
                    "accepted suggestion job_id",
                )
                if accepted_suggestion_job_id is not None
                else None
            ),
            "accepted_suggestion_sha256": (
                require_sha256(
                    accepted_suggestion_sha256,
                    "accepted suggestion sha256",
                )
                if accepted_suggestion_sha256 is not None
                else None
            ),
            "dismissed_suggestion_kind": dismissed_suggestion_kind,
            "dismissed_suggestion_id": (
                require_safe_id(
                    dismissed_suggestion_id,
                    "dismissed suggestion_id",
                )
                if dismissed_suggestion_id is not None
                else None
            ),
            "dismissed_suggestion_job_id": (
                require_safe_id(
                    dismissed_suggestion_job_id,
                    "dismissed suggestion job_id",
                )
                if dismissed_suggestion_job_id is not None
                else None
            ),
            "dismissed_suggestion_sha256": (
                require_sha256(
                    dismissed_suggestion_sha256,
                    "dismissed suggestion sha256",
                )
                if dismissed_suggestion_sha256 is not None
                else None
            ),
        }

    @staticmethod
    def _normalize_propagation_request(request: Any) -> dict[str, Any]:
        if not isinstance(request, dict) or set(request) != _PROPAGATION_REQUEST_FIELDS:
            raise BallAnnotationServiceError(
                "invalid_propagation_request", "Propagation request fields are invalid", status_code=400
            )
        frame_index = request.get("seed_frame_index")
        radius = request.get("radius_frames")
        expected_revision = request.get("expected_seed_revision")
        if isinstance(frame_index, bool) or not isinstance(frame_index, int) or frame_index < 0:
            raise BallAnnotationServiceError("invalid_seed_frame", "seed_frame_index is invalid", status_code=400)
        if isinstance(radius, bool) or not isinstance(radius, int) or not 1 <= radius <= 2:
            raise BallAnnotationServiceError(
                "invalid_propagation_radius", "radius_frames must be one or two", status_code=400
            )
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision <= 0:
            raise BallAnnotationServiceError(
                "invalid_seed_revision",
                "expected_seed_revision must identify a confirmed revision",
                status_code=400,
            )
        return {
            "mutation_id": require_safe_id(request.get("mutation_id"), "propagation mutation_id"),
            "seed_frame_index": frame_index,
            "radius_frames": radius,
            "expected_seed_revision": expected_revision,
        }

    def _propagation_authority_error(self, job: dict[str, Any], session: dict[str, Any]) -> str | None:
        if session.get("data_role") != "development" or session.get("status") != "annotating":
            return "propagation_session_not_active"
        binding = job.get("seed_binding")
        seed = next(
            (item for item in session.get("frames", []) if item.get("frame_index") == job.get("seed_frame_index")),
            None,
        )
        annotation = seed.get("current_annotation") if isinstance(seed, dict) else None
        if (
            not isinstance(binding, dict)
            or not isinstance(seed, dict)
            or not isinstance(annotation, dict)
            or annotation.get("annotation_state") != "confirmed"
            or seed.get("annotation_revision") != binding.get("annotation_revision")
            or seed.get("annotation_etag") != binding.get("annotation_etag")
            or canonical_sha256(annotation) != binding.get("annotation_sha256")
            or annotation != job.get("_seed_annotation")
            or seed.get("source_frame_sha256") != binding.get("source_frame_sha256")
            or seed.get("temporal_group_id") != binding.get("temporal_group_id")
            or session.get("sampling_manifest", {}).get("manifest_sha256") != binding.get("sampling_manifest_sha256")
            or binding.get("tracker_profile_sha256") != TRACKER_PROFILE_SHA256
            or job.get("tracker_profile") != {**TRACKER_PROFILE, "profile_sha256": TRACKER_PROFILE_SHA256}
            or not isinstance(job.get("_seed_frame"), dict)
            or job["_seed_frame"].get("frame_index") != seed.get("frame_index")
            or job["_seed_frame"].get("source_frame_sha256") != seed.get("source_frame_sha256")
            or job["_seed_frame"].get("temporal_group_id") != seed.get("temporal_group_id")
        ):
            return "propagation_seed_authority_stale"
        return None

    def _cancel_neighbor_probe(self, job: dict[str, Any]) -> tuple[str, str | None]:
        probe_job_id = job.get("neighbor_probe_job_id")
        if not isinstance(probe_job_id, str):
            return "not_started", None
        if self._cancel_propagation_probe is None:
            return "cancel_failed", "cancel_callback_unavailable"
        try:
            child = self._cancel_propagation_probe(probe_job_id)
            child_status = child.get("status")
            if child_status == "cancelled":
                return "cancelled", None
            if child_status in {"queued", "running"}:
                return "cancel_requested", None
            if child_status in {"ready", "failed", "blocked"}:
                return "already_terminal", None
            return "cancel_failed", "invalid_child_cancel_status"
        except (KeyError, OSError, DetectorDevelopmentError, RuntimeError) as exc:
            return "cancel_failed", getattr(exc, "code", "child_cancel_failed")

    def _invalidate_active_propagations(self, session: dict[str, Any], error_code: str) -> None:
        for path in self._propagation_root.glob("*.json"):
            job = self._read_json(path, "propagation job", _MAX_SESSION_BYTES)
            if job.get("session_id") != session["session_id"] or job.get("status") in {"ready", "failed", "cancelled"}:
                continue
            self._invalidate_propagation_job(job, session, error_code)

    def _invalidate_propagation_job(
        self,
        job: dict[str, Any],
        session: dict[str, Any],
        error_code: str,
    ) -> dict[str, Any]:
        if job.get("status") in {"ready", "failed", "cancelled"}:
            return job
        cancel_status, cancel_error_code = self._cancel_neighbor_probe(job)
        suggestion_ids = {
            item.get("suggestion_id")
            for item in job.get("suggestions", [])
            if isinstance(item, dict) and isinstance(item.get("suggestion_id"), str)
        }
        session_changed = False
        retained_frames: list[dict[str, Any]] = []
        primary_indices = set(session["sampling_manifest"]["frame_indices"])
        for frame in session["frames"]:
            job_ids = frame.get("propagation_job_ids", [])
            if job["job_id"] in job_ids:
                frame["propagation_job_ids"] = [item for item in job_ids if item != job["job_id"]]
                session_changed = True
            suggestions = frame.get("propagation_suggestions", [])
            retained_suggestions = [item for item in suggestions if item.get("suggestion_id") not in suggestion_ids]
            if retained_suggestions != suggestions:
                frame["propagation_suggestions"] = retained_suggestions
                session_changed = True
            if (
                frame.get("frame_role") == "propagation_target"
                and frame.get("frame_index") not in primary_indices
                and frame.get("current_annotation") is None
                and not frame.get("propagation_job_ids")
                and not frame.get("propagation_suggestions")
            ):
                session_changed = True
                continue
            retained_frames.append(frame)
        if session_changed:
            session["frames"] = retained_frames
            session["updated_at"] = utc_now_iso()
            self._persist_session(session)
        job.update(
            {
                "status": "failed",
                "stage": "failed",
                "frame_results": [],
                "summary": None,
                "suggestions": [],
                "error_code": error_code,
                "neighbor_probe_cancel_status": cancel_status,
                "neighbor_probe_cancel_error_code": cancel_error_code,
                "_commit_frames": None,
                "updated_at": utc_now_iso(),
            }
        )
        self._persist_propagation(job)
        return job

    def _reconcile_propagation_job(self, job: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
        if job["status"] in {"ready", "failed", "blocked", "cancelled"}:
            return job
        authority_error = self._propagation_authority_error(job, session)
        if authority_error is not None:
            return self._invalidate_propagation_job(job, session, authority_error)
        if job["status"] == "committing":
            return self._commit_propagation(job, session)
        if job["neighbor_probe_job_id"] is None:
            request = {
                "parent_trial_id": session["lineage"]["parent_trial_id"],
                "profile_ids": sorted(
                    [
                        session["locked_profile"]["profile_id"],
                        session["control_profile"]["profile_id"],
                    ]
                ),
                "frame_indices": job["target_frame_indices"],
                "top_k": 5,
            }
            created = self._create_propagation_probe(request)
            self._hit_propagation_failpoint("after_neighbor_probe_create")
            probe_job_id = created.get("job_id")
            if not isinstance(probe_job_id, str):
                raise BallAnnotationServiceError(
                    "invalid_probe_response",
                    "Propagation frame probe did not return a job identity",
                )
            job["neighbor_probe_job_id"] = require_safe_id(probe_job_id, "propagation frame probe job_id")
            job["status"] = "waiting_probe"
            job["stage"] = "waiting_probe"
            job["updated_at"] = utc_now_iso()
            self._persist_propagation(job)
        try:
            probe = self._get_probe(job["neighbor_probe_job_id"])
        except (KeyError, DetectorDevelopmentError) as exc:
            raise BallAnnotationServiceError(
                "propagation_probe_unavailable",
                "Propagation frame probe is unavailable",
            ) from exc
        probe_status = probe.get("status")
        if probe_status in _ACTIVE_CHECK_STATUSES:
            job["status"] = "waiting_probe"
            job["stage"] = f"neighbor_probe_{probe_status}"
            job["updated_at"] = utc_now_iso()
            self._persist_propagation(job)
            return job
        if probe_status in _TERMINAL_CHECK_FAILURES:
            job.update(
                {
                    "status": "failed",
                    "stage": "failed",
                    "error_code": probe.get("error_code") or f"neighbor_probe_{probe_status}",
                    "updated_at": utc_now_iso(),
                }
            )
            self._persist_propagation(job)
            return job
        if probe_status != "ready":
            raise BallAnnotationServiceError("invalid_probe_status", "Propagation frame probe status is invalid")
        try:
            authority = self._resolve_ready_jobs([probe], session["locked_profile"]["profile_id"])
            self._validate_propagation_probe(job, session, probe, authority)
            seed_bytes = self._read_bound_frame_bytes(session, job["_seed_frame"])
            target_bytes = {
                frame["frame_index"]: self._read_bound_frame_bytes(session, frame) for frame in authority["frames"]
            }
            group = next(
                item
                for item in session["sampling_manifest"]["groups"]
                if item["group_id"] == job["seed_binding"]["temporal_group_id"]
            )
            tracked = build_advisory_suggestions(
                seed_frame_index=job["seed_frame_index"],
                seed_group=group,
                seed_annotation=job["_seed_annotation"],
                radius_frames=job["radius_frames"],
                source_frame_count=session["source"]["frame_count"],
                seed_frame_bytes=seed_bytes,
                target_frame_bytes=target_bytes,
                source_width=session["source"]["width"],
                source_height=session["source"]["height"],
            )
        except (BallAnnotationServiceError, PropagationError) as exc:
            if isinstance(exc, BallAnnotationServiceError) and exc.code == "review_proxy_required":
                job.update(
                    {
                        "status": "blocked",
                        "stage": "blocked",
                        "frame_results": [],
                        "summary": None,
                        "suggestions": [],
                        "error_code": "review_proxy_required",
                        "_commit_frames": None,
                        "updated_at": utc_now_iso(),
                    }
                )
                self._persist_propagation(job)
                return job
            job.update(
                {
                    "status": "failed",
                    "stage": "failed",
                    "error_code": "propagation_evidence_failed",
                    "updated_at": utc_now_iso(),
                }
            )
            self._persist_propagation(job)
            if isinstance(exc, BallAnnotationServiceError):
                raise
            return job
        tracked["summary"].update(
            {
                "human_dismissed_frame_count": 0,
                "pending_human_confirmation_count": tracked["summary"]["succeeded_frame_count"],
            }
        )
        for suggestion in tracked["suggestions"]:
            suggestion["suggestion_job_id"] = job["job_id"]
            suggestion["suggestion_sha256"] = canonical_sha256(self._suggestion_authority_payload(suggestion))
        job.update(
            {
                "status": "committing",
                "stage": "committing",
                "frame_results": tracked["frame_results"],
                "summary": tracked["summary"],
                "suggestions": tracked["suggestions"],
                "_commit_frames": deepcopy(authority["frames"]),
                "updated_at": utc_now_iso(),
            }
        )
        self._persist_propagation(job)
        self._hit_propagation_failpoint("after_propagation_commit_intent")
        return self._commit_propagation(job, session)

    @staticmethod
    def _validate_propagation_probe(
        job: dict[str, Any],
        session: dict[str, Any],
        probe: dict[str, Any],
        authority: dict[str, Any],
    ) -> None:
        frozen = probe.get("frozen_request")
        expected_profiles = sorted(
            [
                session["locked_profile"]["profile_id"],
                session["control_profile"]["profile_id"],
            ]
        )
        if (
            not isinstance(frozen, dict)
            or frozen.get("parent_trial_id") != session["lineage"]["parent_trial_id"]
            or frozen.get("profile_ids") != expected_profiles
            or frozen.get("frame_indices") != job["target_frame_indices"]
            or frozen.get("top_k") != 5
            or authority["source"] != session["source"]
            or authority["decode"] != session["lineage"]["decode"]
            or authority["locked_profile"] != session["locked_profile"]
            or authority["control_profile"] != session["control_profile"]
            or [frame["frame_index"] for frame in authority["frames"]] != job["target_frame_indices"]
        ):
            raise BallAnnotationServiceError(
                "propagation_probe_authority_mismatch",
                "Propagation frame probe changed frozen source/profile/decode authority",
            )
        if not BallAnnotationService._frames_have_review_timing([job["_seed_frame"], *authority["frames"]]):
            raise BallAnnotationServiceError(
                "review_proxy_required",
                "Propagation frame timing is not strictly ordered; a verified review proxy is required",
                status_code=409,
            )

    def _read_bound_frame_bytes(self, session: dict[str, Any], frame: dict[str, Any]) -> bytes:
        probe_job_id = frame.get("_probe_job_id")
        artifact_id = frame.get("_artifact_id")
        if not isinstance(probe_job_id, str) or not isinstance(artifact_id, str):
            raise BallAnnotationServiceError("frame_not_ready", "Verified propagation frame binding is incomplete")
        try:
            content, media_type, observed_digest = self._read_probe_artifact(probe_job_id, artifact_id)
        except (KeyError, OSError, DetectorDevelopmentError) as exc:
            raise BallAnnotationServiceError("frame_unavailable", "Verified propagation frame is unavailable") from exc
        expected_digest = require_sha256(frame.get("source_frame_sha256"), "source frame sha256")
        if (
            media_type != "image/jpeg"
            or observed_digest != expected_digest
            or hashlib.sha256(content).hexdigest() != expected_digest
            or len(content) != frame.get("source_frame_size_bytes")
            or not 0 < len(content) <= _MAX_FRAME_BYTES
        ):
            raise BallAnnotationServiceError(
                "frame_digest_mismatch",
                "Verified propagation frame bytes changed",
            )
        self._validate_jpeg(content, session["source"]["width"], session["source"]["height"])
        return content

    def _stage_final_frame_media(self, session: dict[str, Any], staging: Path) -> list[dict[str, Any]]:
        frames_root = staging / "frames"
        frames_root.mkdir()
        entries: list[dict[str, Any]] = []
        for frame in sorted(session["frames"], key=lambda item: item["frame_index"]):
            content = self._read_bound_frame_bytes(session, frame)
            frame_index = frame["frame_index"]
            relative_path = f"frames/{frame_index:09d}.jpg"
            path = staging / relative_path
            with path.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            entries.append(
                {
                    "frame_index": frame_index,
                    "relative_path": relative_path,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                    "media_type": "image/jpeg",
                    "width": session["source"]["width"],
                    "height": session["source"]["height"],
                }
            )
        return entries

    def _build_propagation_reports(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        raw_source_job_ids = [
            job_id for frame in session.get("frames", []) for job_id in frame.get("propagation_job_ids", [])
        ]
        if any(not isinstance(job_id, str) for job_id in raw_source_job_ids):
            raise BallAnnotationServiceError(
                "invalid_propagation_report",
                "Supplemental frames are missing producing propagation jobs",
            )
        source_job_ids = sorted(set(raw_source_job_ids))
        reports: list[dict[str, Any]] = []
        for job_id in source_job_ids:
            job = self._load_propagation(job_id)
            commit_frames = job.get("_commit_frames")
            frame_results = deepcopy(job.get("frame_results"))
            suggestions = deepcopy(job.get("suggestions"))
            summary = deepcopy(job.get("summary"))
            if (
                job.get("status") != "ready"
                or job.get("session_id") != session.get("session_id")
                or not isinstance(commit_frames, list)
                or not commit_frames
                or not isinstance(frame_results, list)
                or not isinstance(suggestions, list)
                or not isinstance(summary, dict)
                or [item.get("frame_index") for item in commit_frames] != job.get("target_frame_indices")
                or [item.get("frame_index") for item in frame_results] != job.get("target_frame_indices")
            ):
                raise BallAnnotationServiceError(
                    "invalid_propagation_report",
                    "Producing propagation job is incomplete or changed",
                )
            report_sha256s = {item.get("_probe_report_sha256") for item in commit_frames}
            manifest_sha256s = {item.get("_probe_result_manifest_sha256") for item in commit_frames}
            probe_job_ids = {item.get("_probe_job_id") for item in commit_frames}
            if (
                probe_job_ids != {job.get("neighbor_probe_job_id")}
                or len(report_sha256s) != 1
                or len(manifest_sha256s) != 1
            ):
                raise BallAnnotationServiceError(
                    "invalid_propagation_report",
                    "Propagation producer probe authority is inconsistent",
                )
            expected_intent = {
                "session_id": job.get("session_id"),
                "mutation_id": job.get("mutation_id"),
                "seed_frame_index": job.get("seed_frame_index"),
                "radius_frames": job.get("radius_frames"),
                "expected_seed_revision": job.get("expected_seed_revision"),
                "seed_binding": deepcopy(job.get("seed_binding")),
                "target_frame_indices": deepcopy(job.get("target_frame_indices")),
            }
            if canonical_sha256(expected_intent) != job.get("intent_sha256") or job.get("tracker_profile") != {
                **TRACKER_PROFILE,
                "profile_sha256": TRACKER_PROFILE_SHA256,
            }:
                raise BallAnnotationServiceError(
                    "invalid_propagation_report",
                    "Propagation intent or tracker authority changed",
                )
            confirmed = sum(isinstance(item.get("human_confirmation"), dict) for item in frame_results)
            dismissed = sum(isinstance(item.get("human_decision"), dict) for item in frame_results)
            pending = sum(item.get("pending_human_confirmation") is True for item in frame_results)
            successes = sum(item.get("status") == "success" for item in frame_results)
            if (
                confirmed + dismissed + pending != successes
                or summary.get("succeeded_frame_count") != successes
                or summary.get("human_validated_frame_count") != confirmed
                or summary.get("human_dismissed_frame_count") != dismissed
                or summary.get("pending_human_confirmation_count") != pending
                or summary.get("pending_human_confirmation") is not (pending > 0)
            ):
                raise BallAnnotationServiceError(
                    "invalid_propagation_report",
                    "Propagation human decision accounting is inconsistent",
                )
            report: dict[str, Any] = {
                "schema_version": "1.0",
                "artifact_type": "ball_propagation_report",
                "job_id": job["job_id"],
                "session_id": job["session_id"],
                "intent_sha256": job["intent_sha256"],
                "mutation_id": job["mutation_id"],
                "seed_frame_index": job["seed_frame_index"],
                "expected_seed_revision": job["expected_seed_revision"],
                "radius_frames": job["radius_frames"],
                "seed_binding": deepcopy(job["seed_binding"]),
                "seed_binding_sha256": canonical_sha256(job["seed_binding"]),
                "target_frame_indices": deepcopy(job["target_frame_indices"]),
                "tracker_profile": deepcopy(job["tracker_profile"]),
                "tracker_profile_sha256": TRACKER_PROFILE_SHA256,
                "neighbor_probe_job_id": job["neighbor_probe_job_id"],
                "neighbor_probe_report_sha256": next(iter(report_sha256s)),
                "neighbor_probe_result_manifest_sha256": next(iter(manifest_sha256s)),
                "frame_results": frame_results,
                "suggestions": suggestions,
                "summary": summary,
                "decision_counts": {
                    "confirmed": confirmed,
                    "dismissed": dismissed,
                    "pending": pending,
                },
                "created_at": job["created_at"],
                "updated_at": job["updated_at"],
            }
            report["report_sha256"] = canonical_sha256(report)
            reports.append(report)
        return reports

    @staticmethod
    def _build_detector_candidate_evidence(session: dict[str, Any]) -> list[dict[str, Any]]:
        decisions: dict[tuple[int, str], dict[str, Any]] = {}
        for revision in session.get("revisions", []):
            for prefix, decision_name in (
                ("accepted", "accepted_human_annotation"),
                ("dismissed", "dismissed_manual_annotation"),
            ):
                if revision.get(f"{prefix}_suggestion_kind") != "detector_candidate":
                    continue
                candidate_id = revision.get(f"{prefix}_suggestion_id")
                key = (revision.get("frame_index"), candidate_id)
                if key in decisions:
                    raise BallAnnotationServiceError(
                        "invalid_detector_candidate_evidence",
                        "Detector candidate has more than one human decision",
                    )
                decisions[key] = {
                    "decision": decision_name,
                    "revision_id": revision["revision_id"],
                    "revision": revision["revision"],
                    "operator_id": revision["operator_id"],
                    "decided_at": revision["created_at"],
                    "probe_job_id": revision[f"{prefix}_suggestion_job_id"],
                    "candidate_sha256": revision[f"{prefix}_suggestion_sha256"],
                }
        evidence: list[dict[str, Any]] = []
        for frame in sorted(session.get("frames", []), key=lambda item: item["frame_index"]):
            if frame.get("primary_sample") is not True:
                continue
            for candidate in frame.get("suggested_candidates", []):
                candidate_sha256 = canonical_sha256(
                    BallAnnotationService._detector_candidate_authority_payload(candidate)
                )
                decision = decisions.pop((frame["frame_index"], candidate["candidate_id"]), None)
                if decision is not None and (
                    decision.pop("probe_job_id") != frame.get("_candidate_probe_job_id")
                    or decision.pop("candidate_sha256") != candidate_sha256
                ):
                    raise BallAnnotationServiceError(
                        "invalid_detector_candidate_evidence",
                        "Detector candidate decision authority changed",
                    )
                evidence.append(
                    {
                        "frame_index": frame["frame_index"],
                        "candidate_origin": {
                            "probe_job_id": frame.get("_candidate_probe_job_id"),
                            "probe_report_sha256": frame.get("_candidate_probe_report_sha256"),
                            "probe_result_manifest_sha256": frame.get("_candidate_probe_result_manifest_sha256"),
                            "source_artifact_id": frame.get("_candidate_artifact_id"),
                            "candidate_evidence_sha256": frame.get("_candidate_evidence_sha256"),
                        },
                        "review_media": {
                            "probe_job_id": frame.get("_probe_job_id"),
                            "probe_report_sha256": frame.get("_probe_report_sha256"),
                            "probe_result_manifest_sha256": frame.get("_probe_result_manifest_sha256"),
                            "source_artifact_id": frame.get("_artifact_id"),
                            "proxy_binding_sha256": (
                                canonical_sha256(frame["_proxy_binding"])
                                if isinstance(frame.get("_proxy_binding"), dict)
                                else None
                            ),
                        },
                        "candidate": deepcopy(candidate),
                        "candidate_sha256": candidate_sha256,
                        "decision": decision,
                    }
                )
        if decisions:
            raise BallAnnotationServiceError(
                "invalid_detector_candidate_evidence",
                "Detector candidate decision has no locked candidate authority",
            )
        return evidence

    def _stage_final_propagation_reports(self, reports: list[dict[str, Any]], staging: Path) -> list[dict[str, Any]]:
        if not reports:
            return []
        reports_root = staging / "propagation_reports"
        reports_root.mkdir()
        entries: list[dict[str, Any]] = []
        for report in reports:
            job_id = require_safe_id(report.get("job_id"), "propagation report job_id")
            relative_path = f"propagation_reports/{job_id}.v1.json"
            path = staging / relative_path
            atomic_write_json(path, report, trusted_root=staging)
            content, digest = read_regular_bytes(
                path,
                "sealed propagation report",
                max_bytes=_MAX_FINAL_RESULT_BYTES,
                trusted_root=staging,
            )
            entries.append(
                {
                    "job_id": job_id,
                    "relative_path": relative_path,
                    "report_sha256": report["report_sha256"],
                    "file_sha256": digest,
                    "file_size_bytes": len(content),
                }
            )
        return entries

    def _commit_propagation(self, job: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
        authority_error = self._propagation_authority_error(job, session)
        if authority_error is not None:
            return self._invalidate_propagation_job(job, session, authority_error)
        raw_frames = job.get("_commit_frames")
        if not isinstance(raw_frames, list):
            raise BallAnnotationServiceError(
                "invalid_propagation_commit",
                "Propagation commit frames are missing",
            )
        capacity_error = self._propagation_commit_capacity_error(job, session, raw_frames)
        if capacity_error is not None:
            return self._invalidate_propagation_job(job, session, capacity_error)
        suggestions_by_frame: dict[int, list[dict[str, Any]]] = {}
        for suggestion in job["suggestions"]:
            suggestions_by_frame.setdefault(suggestion["frame_index"], []).append(deepcopy(suggestion))
        by_index = {frame["frame_index"]: frame for frame in session["frames"]}
        expected_group_id = job["seed_binding"]["temporal_group_id"]
        for raw in raw_frames:
            existing = by_index.get(raw["frame_index"])
            if existing is None:
                continue
            if (
                existing.get("source_frame_sha256") != raw.get("source_frame_sha256")
                or existing.get("temporal_group_id") != expected_group_id
            ):
                return self._invalidate_propagation_job(job, session, "propagation_frame_conflict")
        for raw in raw_frames:
            frame_index = raw["frame_index"]
            existing = by_index.get(frame_index)
            if existing is None:
                decoder_pos_msec = raw["_decoder_reported_pos_msec"]
                frame = {
                    **deepcopy(raw),
                    # Neighbor probes provide immutable review media for
                    # propagation targets, not primary detector-feasibility
                    # candidates.
                    "suggested_candidates": [],
                    "source_timing_status": ("not_collected" if decoder_pos_msec is None else "observed"),
                    "decoder_reported_pos_msec": decoder_pos_msec,
                    "decoder_time_seconds": (None if decoder_pos_msec is None else decoder_pos_msec / 1000.0),
                    "display_time_seconds": frame_index / session["source"]["fps"],
                    "true_presentation_timestamp": deepcopy(
                        raw.get(
                            "_true_presentation_timestamp",
                            _TRUE_PRESENTATION_TIMESTAMP_NOT_COLLECTED,
                        )
                    ),
                    "proxy_binding": deepcopy(raw.get("_proxy_binding")),
                    "temporal_group_id": job["seed_binding"]["temporal_group_id"],
                    "frame_url": f"/api/v1/ball-annotation-sessions/{session['session_id']}/frames/{frame_index}",
                    "annotation_revision": 0,
                    "annotation_etag": annotation_etag(session["session_id"], frame_index, 0, None),
                    "current_annotation": None,
                    "frame_role": "propagation_target",
                    "primary_sample": False,
                    "propagation_job_ids": [job["job_id"]],
                    "_source_propagation_job_id": job["job_id"],
                    "propagation_suggestions": suggestions_by_frame.get(frame_index, []),
                }
                session["frames"].append(frame)
                by_index[frame_index] = frame
                continue
            if existing.get("primary_sample") is not True:
                existing["suggested_candidates"] = []
            job_ids = existing.setdefault("propagation_job_ids", [])
            if job["job_id"] not in job_ids:
                job_ids.append(job["job_id"])
            existing_suggestions = existing.setdefault("propagation_suggestions", [])
            existing_ids = {suggestion["suggestion_id"] for suggestion in existing_suggestions}
            existing_suggestions.extend(
                suggestion
                for suggestion in suggestions_by_frame.get(frame_index, [])
                if suggestion["suggestion_id"] not in existing_ids
            )
        session["frames"].sort(key=lambda frame: frame["frame_index"])
        session["updated_at"] = utc_now_iso()
        self._persist_session(session)
        self._hit_propagation_failpoint("after_propagation_session_commit")
        job.update(
            {
                "status": "ready",
                "stage": "ready",
                "error_code": None,
                "updated_at": utc_now_iso(),
            }
        )
        self._persist_propagation(job)
        return job

    @staticmethod
    def _propagation_commit_capacity_error(
        job: dict[str, Any],
        session: dict[str, Any],
        raw_frames: list[dict[str, Any]],
    ) -> str | None:
        primary_indices = set(session["sampling_manifest"]["frame_indices"])
        prospective_supplemental = {
            frame["frame_index"] for frame in session["frames"] if frame.get("frame_role") == "propagation_target"
        }
        prospective_supplemental.update(
            frame["frame_index"] for frame in raw_frames if frame["frame_index"] not in primary_indices
        )
        if len(prospective_supplemental) > _MAX_SUPPLEMENTAL_FRAMES_PER_SESSION:
            return "supplemental_frame_limit"

        producing_job_ids = {
            producing_job_id for frame in session["frames"] for producing_job_id in frame.get("propagation_job_ids", [])
        }
        producing_job_ids.add(job["job_id"])
        if len(producing_job_ids) > _MAX_PROPAGATION_REPORTS_PER_SESSION:
            return "propagation_report_limit"
        return None

    def _reconcile_propagation_confirmations(self, session: dict[str, Any]) -> dict[str, Any]:
        """Finish confirmations whose durable revision intent already exists.

        The annotation revision is the recovery authority.  A crash after that
        revision is persisted but before the propagation job is updated is
        repaired on the next session/job read or idempotent mutation retry.
        """

        session_changed = False
        for revision in session.get("revisions", []):
            accepted_suggestion_kind = revision.get("accepted_suggestion_kind")
            dismissed_suggestion_kind = revision.get("dismissed_suggestion_kind")
            if accepted_suggestion_kind == "detector_candidate" or dismissed_suggestion_kind == "detector_candidate":
                continue
            accepted_suggestion_id = revision.get("accepted_suggestion_id")
            dismissed_suggestion_id = revision.get("dismissed_suggestion_id")
            if accepted_suggestion_id is None and dismissed_suggestion_id is None:
                continue
            if (accepted_suggestion_id is not None and accepted_suggestion_kind != "propagation") or (
                dismissed_suggestion_id is not None and dismissed_suggestion_kind != "propagation"
            ):
                raise BallAnnotationServiceError(
                    "suggestion_lineage_missing",
                    "Propagation decision revision is missing its suggestion kind",
                    status_code=409,
                )
            if accepted_suggestion_id is not None and dismissed_suggestion_id is not None:
                raise BallAnnotationServiceError(
                    "suggestion_lineage_missing",
                    "A revision cannot accept and dismiss propagation evidence together",
                    status_code=409,
                )
            dismissed = dismissed_suggestion_id is not None
            suggestion_id = dismissed_suggestion_id if dismissed else accepted_suggestion_id
            job_id = revision.get("dismissed_suggestion_job_id" if dismissed else "accepted_suggestion_job_id")
            suggestion_sha256 = revision.get(
                "dismissed_suggestion_sha256" if dismissed else "accepted_suggestion_sha256"
            )
            if not isinstance(job_id, str) or not isinstance(suggestion_sha256, str):
                raise BallAnnotationServiceError(
                    "suggestion_lineage_missing",
                    "Propagation decision revision is missing immutable job lineage",
                    status_code=409,
                )
            frame = self._frame(session, revision["frame_index"])
            job = self._load_propagation(job_id)
            job_suggestion = next(
                (item for item in job.get("suggestions", []) if item.get("suggestion_id") == suggestion_id),
                None,
            )
            frame_suggestion = next(
                (
                    item
                    for item in frame.get("propagation_suggestions", [])
                    if item.get("suggestion_id") == suggestion_id
                ),
                None,
            )
            result = next(
                (
                    item
                    for item in job.get("frame_results", [])
                    if item.get("suggestion_id") == suggestion_id and item.get("frame_index") == frame["frame_index"]
                ),
                None,
            )
            if (
                job.get("status") != "ready"
                or job.get("session_id") != session["session_id"]
                or job_suggestion is None
                or frame_suggestion is None
                or result is None
                or job_id not in frame.get("propagation_job_ids", [])
                or job_suggestion.get("frame_index") != frame["frame_index"]
                or job_suggestion.get("source_frame_sha256") != frame["source_frame_sha256"]
                or job_suggestion.get("temporal_group_id") != frame["temporal_group_id"]
                or canonical_sha256(self._suggestion_authority_payload(job_suggestion)) != suggestion_sha256
                or canonical_sha256(self._suggestion_authority_payload(frame_suggestion)) != suggestion_sha256
            ):
                raise BallAnnotationServiceError(
                    "suggestion_authority_mismatch",
                    "Propagation suggestion authority changed before decision recovery",
                    status_code=409,
                )
            if dismissed:
                decision = {
                    "decision": "dismissed_manual_annotation",
                    "revision_id": revision["revision_id"],
                    "revision": revision["revision"],
                    "operator_id": revision["operator_id"],
                    "decided_at": revision["created_at"],
                }
                job_changed = False
                for record in (job_suggestion, frame_suggestion, result):
                    observed = record.get("human_decision")
                    if (observed is not None and observed != decision) or record.get("human_confirmation") is not None:
                        raise BallAnnotationServiceError(
                            "suggestion_decision_conflict",
                            "Propagation suggestion already has different human decision evidence",
                            status_code=409,
                        )
                    if observed != decision or record.get("pending_human_confirmation") is not False:
                        record["human_decision"] = deepcopy(decision)
                        record["pending_human_confirmation"] = False
                        if record is frame_suggestion:
                            session_changed = True
                        else:
                            job_changed = True
                previous_summary = deepcopy(job["summary"])
                self._refresh_propagation_human_summary(job)
                job_changed = job_changed or job["summary"] != previous_summary
                if job_changed:
                    job["updated_at"] = utc_now_iso()
                    self._persist_propagation(job)
                continue
            effective = revision.get("effective_annotation")
            if not isinstance(effective, dict):
                raise BallAnnotationServiceError(
                    "suggestion_confirmation_invalid",
                    "Accepted propagation revision has no confirmed annotation",
                    status_code=409,
                )
            center_error = self._annotation_center_error(job_suggestion, effective)
            iou = self._annotation_iou(job_suggestion, effective)
            confirmation = {
                "revision_id": revision["revision_id"],
                "revision": revision["revision"],
                "operator_id": revision["operator_id"],
                "center_error_px": center_error,
                "iou": iou,
                "corrected": center_error > 0.5 or (iou is not None and iou < 0.95),
                "confirmed_at": revision["created_at"],
            }
            job_changed = False
            for record in (job_suggestion, frame_suggestion, result):
                observed = record.get("human_confirmation")
                if observed is not None and observed != confirmation:
                    raise BallAnnotationServiceError(
                        "suggestion_confirmation_conflict",
                        "Propagation suggestion already has different human confirmation evidence",
                        status_code=409,
                    )
                if observed != confirmation or record.get("pending_human_confirmation") is not False:
                    record["human_confirmation"] = deepcopy(confirmation)
                    record["pending_human_confirmation"] = False
                    if record is frame_suggestion:
                        session_changed = True
                    else:
                        job_changed = True
            previous_summary = deepcopy(job["summary"])
            self._refresh_propagation_human_summary(job)
            job_changed = job_changed or job["summary"] != previous_summary
            if job_changed:
                job["updated_at"] = utc_now_iso()
                self._persist_propagation(job)
        if session_changed:
            session["updated_at"] = utc_now_iso()
            self._persist_session(session)
        return session

    @staticmethod
    def _suggestion_authority_payload(suggestion: dict[str, Any]) -> dict[str, Any]:
        return {
            key: deepcopy(value)
            for key, value in suggestion.items()
            if key
            not in {
                "pending_human_confirmation",
                "human_confirmation",
                "human_decision",
                "suggestion_job_id",
                "suggestion_sha256",
            }
        }

    @staticmethod
    def _detector_candidate_authority_payload(
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            key: deepcopy(value)
            for key, value in candidate.items()
            if key not in {"suggestion_job_id", "suggestion_sha256"}
        }

    @staticmethod
    def _require_client_suggestion_binding(
        *,
        supplied_job_id: str,
        supplied_sha256: str,
        expected_job_id: str,
        expected_sha256: str,
    ) -> None:
        if supplied_job_id != expected_job_id or not hmac.compare_digest(supplied_sha256, expected_sha256):
            raise BallAnnotationServiceError(
                "suggestion_binding_mismatch",
                "Suggestion job or digest does not match current frozen frame evidence",
                status_code=409,
            )

    @staticmethod
    def _refresh_propagation_human_summary(job: dict[str, Any]) -> None:
        confirmed_rows = [item for item in job["frame_results"] if isinstance(item.get("human_confirmation"), dict)]
        confirmations = [item["human_confirmation"] for item in confirmed_rows]
        dismissed_rows = [
            item
            for item in job["frame_results"]
            if isinstance(item.get("human_decision"), dict)
            and item["human_decision"].get("decision") == "dismissed_manual_annotation"
        ]
        safe_indices = {
            item["frame_index"] for item in confirmed_rows if item["human_confirmation"].get("corrected") is False
        }
        safe_by_direction = []
        for step in (-1, 1):
            safe = 0
            for distance in range(1, job["radius_frames"] + 1):
                if job["seed_frame_index"] + step * distance not in safe_indices:
                    break
                safe = distance
            safe_by_direction.append(safe)
        ious = [item["iou"] for item in confirmations if item.get("iou") is not None]
        job["summary"].update(
            {
                "human_validated_frame_count": len(confirmations),
                "human_dismissed_frame_count": len(dismissed_rows),
                "pending_human_confirmation_count": max(
                    0,
                    job["summary"]["succeeded_frame_count"] - len(confirmations) - len(dismissed_rows),
                ),
                "human_validated_center_error_px": (
                    sum(item["center_error_px"] for item in confirmations) / len(confirmations)
                    if confirmations
                    else None
                ),
                "human_validated_iou": (sum(ious) / len(ious) if ious else None),
                "human_validated_safe_span_frames": (max(safe_by_direction) if confirmations else None),
                "pending_human_confirmation": (
                    len(confirmations) + len(dismissed_rows) < job["summary"]["succeeded_frame_count"]
                ),
            }
        )

    @staticmethod
    def _server_derived_annotation_provenance(
        request: dict[str, Any],
    ) -> str:
        if request.get("dismissed_suggestion_id") is not None:
            return "suggestion_dismissed_manual"
        if request.get("suggestion_kind") == "detector_candidate":
            return "detector_candidate_human_confirmed"
        if request.get("suggestion_kind") == "propagation":
            return "propagation_suggestion_human_confirmed"
        return "manual_human_annotation"

    @staticmethod
    def _pending_propagation_suggestion(frame: dict[str, Any], suggestion_id: str) -> dict[str, Any]:
        suggestion = next(
            (item for item in frame.get("propagation_suggestions", []) if item.get("suggestion_id") == suggestion_id),
            None,
        )
        if suggestion is None:
            raise BallAnnotationServiceError(
                "suggestion_not_found",
                "Propagation suggestion is not bound to this frame",
                status_code=400,
            )
        if suggestion.get("source_frame_sha256") != frame.get("source_frame_sha256"):
            raise BallAnnotationServiceError(
                "suggestion_source_mismatch",
                "Propagation suggestion source frame binding changed",
            )
        if suggestion.get("pending_human_confirmation") is not True:
            raise BallAnnotationServiceError(
                "suggestion_already_decided",
                "Propagation suggestion already has a human decision",
                status_code=409,
            )
        return suggestion

    @staticmethod
    def _pending_detector_candidate(
        session: dict[str, Any], frame: dict[str, Any], candidate_id: str
    ) -> dict[str, Any]:
        if frame.get("primary_sample") is not True:
            raise BallAnnotationServiceError(
                "suggestion_not_found",
                "Detector candidates are only bound to primary sampled frames",
                status_code=400,
            )
        candidate = next(
            (item for item in frame.get("suggested_candidates", []) if item.get("candidate_id") == candidate_id),
            None,
        )
        if candidate is None:
            raise BallAnnotationServiceError(
                "suggestion_not_found",
                "Locked detector candidate is not bound to this frame",
                status_code=400,
            )
        if (
            candidate.get("profile_id") != session["locked_profile"]["profile_id"]
            or candidate.get("suggestion_job_id") != frame.get("_candidate_probe_job_id")
            or not hmac.compare_digest(
                str(candidate.get("suggestion_sha256")),
                canonical_sha256(BallAnnotationService._detector_candidate_authority_payload(candidate)),
            )
        ):
            raise BallAnnotationServiceError(
                "suggestion_authority_mismatch",
                "Locked detector candidate authority changed",
                status_code=409,
            )
        for revision in session.get("revisions", []):
            if revision.get("frame_index") != frame.get("frame_index"):
                continue
            accepted = (
                revision.get("accepted_suggestion_kind") == "detector_candidate"
                and revision.get("accepted_suggestion_id") == candidate_id
            )
            dismissed = (
                revision.get("dismissed_suggestion_kind") == "detector_candidate"
                and revision.get("dismissed_suggestion_id") == candidate_id
            )
            if accepted or dismissed:
                raise BallAnnotationServiceError(
                    "suggestion_already_decided",
                    "Locked detector candidate already has a human decision",
                    status_code=409,
                )
        return candidate

    def _validated_propagation_job(
        self,
        session: dict[str, Any],
        frame: dict[str, Any],
        suggestion: dict[str, Any],
    ) -> dict[str, Any]:
        for job_id in frame.get("propagation_job_ids", []):
            job = self._load_propagation(job_id)
            bound = next(
                (
                    item
                    for item in job.get("suggestions", [])
                    if item.get("suggestion_id") == suggestion.get("suggestion_id")
                ),
                None,
            )
            if bound is None:
                continue
            if (
                job.get("status") != "ready"
                or job.get("session_id") != session["session_id"]
                or bound.get("suggestion_job_id") != job["job_id"]
                or suggestion.get("suggestion_job_id") != job["job_id"]
                or canonical_sha256(self._suggestion_authority_payload(bound))
                != canonical_sha256(self._suggestion_authority_payload(suggestion))
                or not hmac.compare_digest(
                    str(bound.get("suggestion_sha256")),
                    canonical_sha256(self._suggestion_authority_payload(bound)),
                )
                or bound.get("suggestion_sha256") != suggestion.get("suggestion_sha256")
                or bound.get("frame_index") != frame["frame_index"]
                or bound.get("source_frame_sha256") != frame["source_frame_sha256"]
                or bound.get("temporal_group_id") != frame["temporal_group_id"]
            ):
                raise BallAnnotationServiceError(
                    "suggestion_authority_mismatch",
                    "Propagation suggestion authority changed before confirmation",
                )
            return job
        raise BallAnnotationServiceError(
            "suggestion_lineage_missing",
            "Propagation suggestion job lineage is missing",
        )

    @staticmethod
    def _annotation_center_error(suggestion: dict[str, Any], annotation: dict[str, Any]) -> float:
        def center(value: dict[str, Any]) -> tuple[float, float]:
            point = value.get("point_source_px")
            if isinstance(point, dict):
                return float(point["x"]), float(point["y"])
            box = value["bbox_source_px"]
            return (
                (float(box["left"]) + float(box["right"])) / 2.0,
                (float(box["top"]) + float(box["bottom"])) / 2.0,
            )

        suggested_center = center(suggestion)
        confirmed_center = center(annotation)
        return math.hypot(
            suggested_center[0] - confirmed_center[0],
            suggested_center[1] - confirmed_center[1],
        )

    @staticmethod
    def _annotation_iou(suggestion: dict[str, Any], annotation: dict[str, Any]) -> float | None:
        first = suggestion.get("bbox_source_px")
        second = annotation.get("bbox_source_px")
        if not isinstance(first, dict) or not isinstance(second, dict):
            return None
        intersection_width = max(
            0.0,
            min(float(first["right"]), float(second["right"])) - max(float(first["left"]), float(second["left"])),
        )
        intersection_height = max(
            0.0,
            min(float(first["bottom"]), float(second["bottom"])) - max(float(first["top"]), float(second["top"])),
        )
        intersection = intersection_width * intersection_height
        first_area = (float(first["right"]) - float(first["left"])) * (float(first["bottom"]) - float(first["top"]))
        second_area = (float(second["right"]) - float(second["left"])) * (
            float(second["bottom"]) - float(second["top"])
        )
        union = first_area + second_area - intersection
        return intersection / union if union > 0 else 0.0

    @staticmethod
    def _public_propagation(job: dict[str, Any]) -> dict[str, Any]:
        public = deepcopy(job)
        for key in list(public):
            if key.startswith("_"):
                public.pop(key)
        return public

    def _record_groups(
        self,
        session_id: str,
        source_sha256: str,
        groups: list[dict[str, Any]],
        *,
        data_role: str,
        state: str,
        allow_reassignment_from: str | None = None,
    ) -> None:
        try:
            session_id = require_safe_id(session_id, "annotation session_id")
            source_sha256 = require_sha256(source_sha256, "annotation source sha256")
            if allow_reassignment_from is not None:
                allow_reassignment_from = require_safe_id(allow_reassignment_from, "previous annotation session_id")
        except DetectorDevelopmentError as exc:
            raise BallAnnotationServiceError(
                "invalid_group_registry", "Temporal group registry authority is invalid"
            ) from exc
        if data_role not in {"development", "check"} or state not in {
            "reserved",
            "revealed",
            "scored",
        }:
            raise BallAnnotationServiceError("invalid_group_registry", "Temporal group registry state is invalid")
        if data_role == "development" and state != "revealed":
            raise BallAnnotationServiceError(
                "invalid_group_registry",
                "Development temporal groups must remain revealed evidence",
            )
        if not isinstance(groups, list) or not groups:
            raise BallAnnotationServiceError("invalid_temporal_group", "Temporal group authority is missing")
        canonical_groups = [self._canonical_registry_group(group, source_sha256) for group in groups]
        canonical_groups.sort(key=lambda group: (group["start_frame"], group["end_frame"], group["group_id"]))
        registry = self._read_registry()
        for group in canonical_groups:
            group_span = (group["start_frame"], group["end_frame"])
            overlap = next(
                (
                    entry
                    for entry in registry["entries"]
                    if entry.get("source_sha256") == source_sha256
                    and entry.get("state") in {"reserved", "revealed", "scored"}
                    and entry.get("start_frame") <= group_span[1]
                    and group_span[0] <= entry.get("end_frame")
                    and not (
                        allow_reassignment_from is not None
                        and entry.get("session_id") == allow_reassignment_from
                        and entry.get("state") == "reserved"
                        and entry.get("group_id") == group.get("group_id")
                    )
                    and entry.get("session_id") != session_id
                ),
                None,
            )
            if overlap is not None:
                raise BallAnnotationServiceError(
                    "temporal_group_conflict",
                    "A temporal derivative span is already reserved or revealed",
                )
            existing = next(
                (
                    entry
                    for entry in registry["entries"]
                    if entry["source_sha256"] == source_sha256 and entry["group_id"] == group["group_id"]
                ),
                None,
            )
            if existing is not None:
                if existing["session_id"] == session_id:
                    if (
                        self._registry_group(existing) != group
                        or existing["data_role"] != data_role
                        or existing["state"] != state
                    ):
                        raise BallAnnotationServiceError(
                            "temporal_group_conflict",
                            "An existing temporal group has different authority",
                        )
                    continue
                if (
                    allow_reassignment_from is not None
                    and existing["session_id"] == allow_reassignment_from
                    and existing["state"] == "reserved"
                ):
                    existing["session_id"] = session_id
                    existing["updated_at"] = utc_now_iso()
                    continue
                raise BallAnnotationServiceError(
                    "temporal_group_conflict", "A temporal group is already reserved or revealed"
                )
            timestamp = utc_now_iso()
            registry["entries"].append(
                {
                    **deepcopy(group),
                    "session_id": session_id,
                    "data_role": data_role,
                    "state": state,
                    "retired_for_all_profiles": state in {"revealed", "scored"},
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
            )
        self._write_registry(registry)

    def _retry_reservation_snapshot(self, previous: dict[str, Any]) -> list[dict[str, Any]]:
        """Capture the exact blocked-check reservation authority before transfer."""

        registry = self._read_registry()
        expected_keys = {
            (previous["source"]["sha256"], group["group_id"]) for group in previous["sampling_manifest"]["groups"]
        }
        snapshot = [
            deepcopy(entry)
            for entry in registry["entries"]
            if (entry.get("source_sha256"), entry.get("group_id")) in expected_keys
        ]
        if (
            len(snapshot) != len(expected_keys)
            or {(entry.get("source_sha256"), entry.get("group_id")) for entry in snapshot} != expected_keys
            or any(
                entry.get("session_id") != previous["session_id"]
                or entry.get("state") != "reserved"
                or entry.get("data_role") != "check"
                for entry in snapshot
            )
        ):
            raise BallAnnotationServiceError(
                "retry_reservation_mismatch",
                "Blocked check reservation authority is incomplete or changed",
                status_code=409,
            )
        snapshot.sort(
            key=lambda entry: (
                entry["source_sha256"],
                entry["start_frame"],
                entry["session_id"],
            )
        )
        return snapshot

    def _transfer_retry_reservations(self, session: dict[str, Any]) -> None:
        """Atomically replace exact previous reservations with the retry owner."""

        snapshot = session.get("_retry_reservation_snapshot")
        if not isinstance(snapshot, list) or not snapshot:
            raise BallAnnotationServiceError(
                "retry_reservation_mismatch",
                "Retry reservation snapshot is missing",
                status_code=409,
            )
        previous_id = session.get("retry_from_session_id")
        new_id = session.get("session_id")
        transfer_updated_at = session.get("_retry_reservation_transfer_updated_at")
        if not all(isinstance(value, str) and value for value in (previous_id, new_id, transfer_updated_at)):
            raise BallAnnotationServiceError(
                "retry_reservation_mismatch",
                "Retry reservation transfer metadata is invalid",
                status_code=409,
            )
        registry = self._read_registry()
        by_key = {(entry.get("source_sha256"), entry.get("group_id")): entry for entry in registry["entries"]}
        replacements: list[tuple[dict[str, Any], dict[str, Any]]] = []
        observed_states: set[str] = set()
        for original in snapshot:
            key = (original.get("source_sha256"), original.get("group_id"))
            current = by_key.get(key)
            expected_new = {
                **deepcopy(original),
                "session_id": new_id,
                "updated_at": transfer_updated_at,
            }
            if current == original:
                observed_states.add("previous")
                replacements.append((current, expected_new))
            elif current == expected_new:
                observed_states.add("retry")
            else:
                raise BallAnnotationServiceError(
                    "retry_reservation_mismatch",
                    "Retry reservation authority was concurrently changed",
                    status_code=409,
                )
        if observed_states == {"retry"}:
            return
        if observed_states != {"previous"}:
            raise BallAnnotationServiceError(
                "retry_reservation_mismatch",
                "Retry reservation transfer is only partially published",
                status_code=409,
            )
        for current, replacement in replacements:
            current.clear()
            current.update(replacement)
        registry["entries"].sort(
            key=lambda entry: (
                entry["source_sha256"],
                entry["start_frame"],
                entry["session_id"],
            )
        )
        self._write_registry(registry)

    def _restore_retry_reservation_snapshot(
        self,
        snapshot: list[dict[str, Any]],
        *,
        retry_session_id: str,
    ) -> None:
        """Restore byte-for-byte previous entries after setup failure/crash."""

        registry = self._read_registry()
        by_key = {(entry.get("source_sha256"), entry.get("group_id")): entry for entry in registry["entries"]}
        changed = False
        for original in snapshot:
            if not isinstance(original, dict):
                raise BallAnnotationServiceError(
                    "retry_reservation_mismatch",
                    "Retry reservation snapshot is invalid",
                )
            key = (original.get("source_sha256"), original.get("group_id"))
            current = by_key.get(key)
            if current == original:
                continue
            if current is None:
                restored = deepcopy(original)
                registry["entries"].append(restored)
                by_key[key] = restored
                changed = True
                continue
            if current.get("session_id") != retry_session_id:
                raise BallAnnotationServiceError(
                    "retry_reservation_mismatch",
                    "Retry rollback cannot overwrite another reservation owner",
                )
            current.clear()
            current.update(deepcopy(original))
            changed = True
        retained = [
            entry
            for entry in registry["entries"]
            if entry.get("session_id") != retry_session_id
            or (entry.get("source_sha256"), entry.get("group_id"))
            in {
                (entry.get("source_sha256"), entry.get("group_id"))
                for entry in snapshot
                if entry.get("session_id") == retry_session_id
            }
        ]
        if len(retained) != len(registry["entries"]):
            registry["entries"] = retained
            changed = True
        if changed:
            registry["entries"].sort(
                key=lambda entry: (
                    entry["source_sha256"],
                    entry["start_frame"],
                    entry["session_id"],
                )
            )
            self._write_registry(registry)

    def _complete_retry_reservation_transaction(self, session: dict[str, Any]) -> None:
        """Durably mark a retry reservation transfer as published."""

        cleaned = deepcopy(session)
        cleaned.pop("_retry_reservation_snapshot", None)
        cleaned.pop("_retry_reservation_transfer_updated_at", None)
        self._persist_session(cleaned)
        session.clear()
        session.update(cleaned)

    def _retry_reservations_are_owned(self, session: dict[str, Any], snapshot: list[dict[str, Any]]) -> bool:
        registry = self._read_registry()
        by_key = {(entry.get("source_sha256"), entry.get("group_id")): entry for entry in registry["entries"]}
        return all(
            (current := by_key.get((original.get("source_sha256"), original.get("group_id")))) is not None
            and current.get("session_id") == session.get("session_id")
            and current.get("data_role") == "check"
            and current.get("state") in {"reserved", "revealed", "scored"}
            for original in snapshot
        )

    def _recover_retry_reservation_transactions(self) -> None:
        """Complete or roll back retry publication interrupted by process death."""

        for path in sorted(self._sessions_root.glob("*.json")):
            session = self._read_json(path, "annotation session", _MAX_SESSION_BYTES)
            snapshot = session.get("_retry_reservation_snapshot")
            if not isinstance(snapshot, list) or not snapshot:
                continue
            session_id = require_safe_id(session.get("session_id"), "annotation session_id")
            lock_path = self._sampling_locks_root / f"{session_id}.json"
            if lock_path.is_file():
                if not self._retry_reservations_are_owned(session, snapshot):
                    self._transfer_retry_reservations(session)
                self._complete_retry_reservation_transaction(session)
                continue
            self._restore_retry_reservation_snapshot(snapshot, retry_session_id=session_id)
            path.unlink(missing_ok=True)
            lock_path.unlink(missing_ok=True)

    @staticmethod
    def _initial_check_setup_transaction(session: dict[str, Any]) -> dict[str, Any]:
        transaction: dict[str, Any] = {
            "schema_version": "1.0",
            "artifact_type": "ball_initial_check_setup_transaction",
            "session_id": session["session_id"],
            "request_sha256": session["request_sha256"],
            "sampling_manifest_sha256": session["sampling_manifest"]["manifest_sha256"],
            "source_sha256": session["source"]["sha256"],
            "locked_profile_id": session["locked_profile"]["profile_id"],
            "locked_profile_sha256": session["locked_profile"]["profile_sha256"],
            "group_ids": [group["group_id"] for group in session["sampling_manifest"]["groups"]],
        }
        transaction["transaction_sha256"] = canonical_sha256(transaction)
        return transaction

    def _initial_check_reservations_are_owned(
        self,
        session: dict[str, Any],
    ) -> bool:
        registry = self._read_registry()
        expected_groups = {group["group_id"]: group for group in session["sampling_manifest"]["groups"]}
        entries = [entry for entry in registry["entries"] if entry.get("session_id") == session.get("session_id")]
        return (
            len(entries) == len(expected_groups)
            and {entry.get("group_id") for entry in entries} == set(expected_groups)
            and all(
                entry.get("source_sha256") == session["source"]["sha256"]
                and entry.get("data_role") == "check"
                and entry.get("state") == "reserved"
                and entry.get("retired_for_all_profiles") is False
                and self._registry_group(entry) == expected_groups[entry["group_id"]]
                for entry in entries
            )
        )

    def _block_initial_check_setup(
        self,
        session: dict[str, Any],
    ) -> dict[str, Any]:
        blocked = deepcopy(session)
        blocked.pop("_initial_check_setup_transaction", None)
        blocked.update(
            {
                "status": "blocked",
                "stage": "blocked",
                "error_code": "invalid_sampling_setup",
                "blocker_code": "sampling_lock_conflict",
                "frames": [],
                "check_probe_job_id": None,
                "check_probe_authority": None,
                "updated_at": utc_now_iso(),
            }
        )
        self._persist_session(blocked)
        return blocked

    def _recover_initial_check_setup_transaction(
        self,
        session: dict[str, Any],
    ) -> dict[str, Any]:
        transaction = session.get("_initial_check_setup_transaction")
        expected_transaction = self._initial_check_setup_transaction(session)
        pristine = (
            session.get("data_role") == "check"
            and session.get("retry_from_session_id") is None
            and session.get("status") == "sampling_locked"
            and session.get("stage") == "sampling_locked"
            and session.get("check_probe_job_id") is None
            and session.get("check_probe_authority") is None
            and session.get("frames") == []
            and session.get("revisions") == []
            and session.get("final_package") is None
            and session.get("final_result") is None
        )
        if (
            not isinstance(transaction, dict)
            or transaction != expected_transaction
            or not pristine
            or not self._initial_check_reservations_are_owned(session)
        ):
            return self._block_initial_check_setup(session)
        try:
            self._persist_sampling_lock(session)
            self._require_verified_sampling_lock(session)
        except (BallAnnotationServiceError, DetectorDevelopmentError, OSError):
            return self._block_initial_check_setup(session)
        self._complete_initial_check_setup_transaction(session)
        return session

    def _recover_initial_check_setup_transactions(self) -> None:
        for path in sorted(self._sessions_root.glob("*.json")):
            session = self._read_json(path, "annotation session", _MAX_SESSION_BYTES)
            if session.get("_initial_check_setup_transaction") is not None:
                self._recover_initial_check_setup_transaction(session)

    def _complete_initial_check_setup_transaction(
        self,
        session: dict[str, Any],
    ) -> None:
        cleaned = deepcopy(session)
        cleaned.pop("_initial_check_setup_transaction", None)
        self._persist_session(cleaned)
        session.clear()
        session.update(cleaned)

    def _recover_development_group_publications(self) -> None:
        """Never allow already revealed development moments to be sampled by check."""

        for path in sorted(self._sessions_root.glob("*.json")):
            session = self._load_session(path.stem)
            if session.get("data_role") != "development":
                continue
            if (
                session.get("status") == "blocked"
                and session.get("blocker_code") == "review_proxy_required"
                and session.get("frames") == []
            ):
                continue
            retry_lineage = session.get("retry_lineage")
            if isinstance(retry_lineage, dict) and retry_lineage.get("mode") == "review_proxy_decode_upgrade":
                try:
                    self._review_proxy_session_creation_authority(session)
                except (BallAnnotationServiceError, DetectorDevelopmentError) as exc:
                    raise BallAnnotationServiceError(
                        "replacement_session_mismatch",
                        "Review-proxy replacement creation authority is invalid",
                        status_code=409,
                    ) from exc
                registry = self._read_registry()
                session_id = session.get("session_id")
                source_sha256 = session.get("source", {}).get("sha256")
                expected_groups = session.get("sampling_manifest", {}).get("groups")
                expected_by_id = (
                    {group.get("group_id"): group for group in expected_groups}
                    if isinstance(expected_groups, list) and all(isinstance(group, dict) for group in expected_groups)
                    else {}
                )
                rows = [entry for entry in registry["entries"] if entry.get("session_id") == session_id]
                publication_exact = bool(
                    expected_by_id
                    and len(rows) == len(expected_by_id)
                    and {row.get("group_id") for row in rows} == set(expected_by_id)
                    and all(
                        row.get("source_sha256") == source_sha256
                        and row.get("data_role") == "development"
                        and row.get("state") == "revealed"
                        and row.get("retired_for_all_profiles") is True
                        and self._registry_group(row) == expected_by_id[row["group_id"]]
                        for row in rows
                    )
                )
                if publication_exact:
                    continue
                previous_session_id = require_safe_id(
                    session.get("retry_from_session_id"),
                    "replacement parent session_id",
                )
                previous = self._load_session(previous_session_id)
                previous_lineage = previous.get("lineage")
                session_lineage = session.get("lineage")
                previous_jobs = (
                    previous_lineage.get("development_probe_job_ids") if isinstance(previous_lineage, dict) else None
                )
                session_jobs = (
                    session_lineage.get("development_probe_job_ids") if isinstance(session_lineage, dict) else None
                )
                if (
                    not isinstance(previous_jobs, list)
                    or not isinstance(session_jobs, list)
                    or len(session_jobs) != len(previous_jobs) + 1
                    or session_jobs[: len(previous_jobs)] != previous_jobs
                    or any(entry.get("session_id") == previous_session_id for entry in registry["entries"])
                ):
                    raise BallAnnotationServiceError(
                        "replacement_session_mismatch",
                        "Replacement startup recovery lineage is invalid",
                        status_code=409,
                    )
                child_probe_job_id = require_safe_id(
                    session_jobs[-1],
                    "replacement child probe job_id",
                )
                self._require_exact_review_proxy_replacement_creation(
                    session,
                    previous,
                    child_probe_job_id,
                )
            self._record_groups(
                require_safe_id(session.get("session_id"), "annotation session_id"),
                require_sha256(
                    session.get("source", {}).get("sha256"),
                    "annotation source sha256",
                ),
                session.get("sampling_manifest", {}).get("groups"),
                data_role="development",
                state="revealed",
            )

    def _transition_session_groups(self, session_id: str, state: str) -> None:
        try:
            session_id = require_safe_id(session_id, "annotation session_id")
        except DetectorDevelopmentError as exc:
            raise BallAnnotationServiceError(
                "invalid_group_transition", "Temporal group transition is invalid"
            ) from exc
        if state not in {"revealed", "scored"}:
            raise BallAnnotationServiceError("invalid_group_transition", "Temporal group transition is invalid")
        registry = self._read_registry()
        entries = [entry for entry in registry["entries"] if entry["session_id"] == session_id]
        if not entries:
            raise BallAnnotationServiceError(
                "group_registry_mismatch", "Session temporal groups are absent from registry"
            )
        current_states = {entry["state"] for entry in entries}
        if len(current_states) != 1:
            raise BallAnnotationServiceError("invalid_group_transition", "Session temporal group states disagree")
        current_state = next(iter(current_states))
        if entries[0]["data_role"] == "development":
            raise BallAnnotationServiceError(
                "invalid_group_transition",
                "Development temporal groups remain revealed evidence",
            )
        if current_state == state:
            return
        allowed = {("reserved", "revealed"), ("revealed", "scored")}
        if (current_state, state) not in allowed:
            raise BallAnnotationServiceError("invalid_group_transition", "Temporal group state cannot regress or skip")
        changed_at = utc_now_iso()
        for entry in entries:
            entry["state"] = state
            entry["retired_for_all_profiles"] = True
            entry["updated_at"] = changed_at
        self._write_registry(registry)

    @classmethod
    def _registry_group(cls, entry: dict[str, Any]) -> dict[str, Any]:
        group = {key: deepcopy(entry[key]) for key in _TEMPORAL_GROUP_FIELDS} | {"frame_index": entry["frame_index"]}
        if "pre_reveal_lighting_stratum" in entry:
            group["pre_reveal_lighting_stratum"] = entry["pre_reveal_lighting_stratum"]
        return group

    @staticmethod
    def _canonical_registry_group(group: Any, source_sha256: str) -> dict[str, Any]:
        allowed_fields = {
            *_TEMPORAL_GROUP_FIELDS,
            "frame_index",
            "pre_reveal_lighting_stratum",
        }
        if not isinstance(group, dict) or not (
            set(group) == allowed_fields - {"pre_reveal_lighting_stratum"} or set(group) == allowed_fields
        ):
            raise BallAnnotationServiceError("invalid_temporal_group", "Temporal group authority is invalid")
        frame_index = group.get("frame_index")
        if isinstance(frame_index, bool) or not isinstance(frame_index, int) or frame_index < 0:
            raise BallAnnotationServiceError("invalid_temporal_group", "Temporal group frame is invalid")
        expected = temporal_group_for_frame(source_sha256, frame_index)
        if any(group.get(key) != value for key, value in expected.items()):
            raise BallAnnotationServiceError("invalid_temporal_group", "Temporal group ancestry is not canonical")
        lighting = group.get("pre_reveal_lighting_stratum")
        if lighting is not None and lighting not in _LIGHTING_STRATA:
            raise BallAnnotationServiceError("invalid_temporal_group", "Temporal group lighting binding is invalid")
        return deepcopy(group)

    def _recover_orphan_reservations(self) -> None:
        registry = self._read_registry()
        existing_sessions = {path.stem for path in self._sessions_root.glob("*.json")}
        retained = [
            entry
            for entry in registry["entries"]
            if entry.get("state") != "reserved" or entry.get("session_id") in existing_sessions
        ]
        if len(retained) != len(registry["entries"]):
            registry["entries"] = retained
            self._write_registry(registry)

    def _read_registry(self) -> dict[str, Any]:
        registry = self._read_json(self._registry_path, "temporal group registry", _MAX_REGISTRY_BYTES)
        if set(registry) == _REGISTRY_FIELDS - {"registry_sha256"}:
            raise BallAnnotationServiceError(
                "group_registry_migration_required",
                "Pre-canonical temporal group registry requires explicit migration",
            )
        if (
            set(registry) != _REGISTRY_FIELDS
            or registry.get("schema_version") != "1.0"
            or registry.get("artifact_type") != "ball_temporal_group_registry"
            or not isinstance(registry.get("entries"), list)
        ):
            raise BallAnnotationServiceError("invalid_group_registry", "Temporal group registry is invalid")
        try:
            digest = require_sha256(registry.get("registry_sha256"), "temporal group registry sha256")
            expected_digest = canonical_sha256(
                {key: value for key, value in registry.items() if key != "registry_sha256"}
            )
        except DetectorDevelopmentError as exc:
            raise BallAnnotationServiceError(
                "invalid_group_registry", "Temporal group registry digest is invalid"
            ) from exc
        if digest != expected_digest:
            raise BallAnnotationServiceError("invalid_group_registry", "Temporal group registry digest changed")
        self._validate_registry_entries(registry["entries"], require_sorted=True)
        return registry

    def _write_registry(self, registry: dict[str, Any]) -> None:
        if not isinstance(registry, dict) or frozenset(registry) not in {
            _REGISTRY_FIELDS,
            _REGISTRY_FIELDS - {"registry_sha256"},
        }:
            raise BallAnnotationServiceError("invalid_group_registry", "Temporal group registry is invalid")
        sealed = deepcopy(registry)
        sealed.pop("registry_sha256", None)
        if (
            sealed.get("schema_version") != "1.0"
            or sealed.get("artifact_type") != "ball_temporal_group_registry"
            or not isinstance(sealed.get("entries"), list)
        ):
            raise BallAnnotationServiceError("invalid_group_registry", "Temporal group registry is invalid")
        sealed["entries"].sort(key=self._registry_entry_sort_key)
        self._validate_registry_entries(sealed["entries"], require_sorted=True)
        sealed["registry_sha256"] = canonical_sha256(sealed)
        canonical_size = len(canonical_json_bytes(sealed))
        persisted_size = len(
            (
                json.dumps(
                    sealed,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            ).encode("utf-8")
        )
        if canonical_size > _MAX_REGISTRY_BYTES or persisted_size > _MAX_REGISTRY_BYTES:
            raise BallAnnotationServiceError(
                "resource_limit_exceeded", "Temporal group registry exceeds its byte limit"
            )
        atomic_write_json(self._registry_path, sealed, trusted_root=self._root)

    @staticmethod
    def _registry_entry_sort_key(entry: dict[str, Any]) -> tuple[Any, ...]:
        return (
            entry.get("source_sha256"),
            entry.get("start_frame"),
            entry.get("end_frame"),
            entry.get("group_id"),
            entry.get("session_id"),
        )

    @classmethod
    def _validate_registry_entries(cls, entries: list[Any], *, require_sorted: bool) -> None:
        seen_groups: set[tuple[str, str]] = set()
        session_authority: dict[str, tuple[str, str, str]] = {}
        previous_by_source: dict[str, tuple[int, str]] = {}
        for entry in entries:
            fields = frozenset(entry) if isinstance(entry, dict) else frozenset()
            if fields not in {
                _REGISTRY_ENTRY_FIELDS,
                _REGISTRY_ENTRY_FIELDS | _REGISTRY_OPTIONAL_FIELDS,
            }:
                raise BallAnnotationServiceError(
                    "invalid_group_registry", "Temporal group registry entry fields are invalid"
                )
            try:
                source_sha256 = require_sha256(entry["source_sha256"], "registry source sha256")
                session_id = require_safe_id(entry["session_id"], "registry annotation session_id")
            except DetectorDevelopmentError as exc:
                raise BallAnnotationServiceError(
                    "invalid_group_registry", "Temporal group registry identity is invalid"
                ) from exc
            data_role = entry["data_role"]
            state = entry["state"]
            if (
                data_role not in {"development", "check"}
                or state not in {"reserved", "revealed", "scored"}
                or (data_role == "development" and state != "revealed")
                or entry["retired_for_all_profiles"] is not (state in {"revealed", "scored"})
            ):
                raise BallAnnotationServiceError(
                    "invalid_group_registry", "Temporal group registry state is inconsistent"
                )
            lighting = entry.get("pre_reveal_lighting_stratum")
            if (lighting is not None and lighting not in _LIGHTING_STRATA) or (
                lighting is not None and data_role != "check"
            ):
                raise BallAnnotationServiceError(
                    "invalid_group_registry", "Temporal group lighting authority is invalid"
                )
            try:
                created_at = datetime.fromisoformat(entry["created_at"])
                updated_at = datetime.fromisoformat(entry["updated_at"])
            except (TypeError, ValueError) as exc:
                raise BallAnnotationServiceError(
                    "invalid_group_registry", "Temporal group timestamps are invalid"
                ) from exc
            if (
                created_at.tzinfo is None
                or updated_at.tzinfo is None
                or created_at.utcoffset() is None
                or updated_at.utcoffset() is None
                or created_at.utcoffset().total_seconds() != 0
                or updated_at.utcoffset().total_seconds() != 0
                or created_at.isoformat() != entry["created_at"]
                or updated_at.isoformat() != entry["updated_at"]
                or updated_at < created_at
            ):
                raise BallAnnotationServiceError("invalid_group_registry", "Temporal group timestamps are invalid")
            try:
                canonical_group = cls._canonical_registry_group(cls._registry_group(entry), source_sha256)
            except (BallAnnotationServiceError, DetectorDevelopmentError) as exc:
                raise BallAnnotationServiceError(
                    "invalid_group_registry", "Temporal group ancestry is not canonical"
                ) from exc
            if canonical_group["frame_index"] != canonical_group["seed_frame_index"]:
                raise BallAnnotationServiceError(
                    "invalid_group_registry", "Temporal group frame authority is inconsistent"
                )
            group_key = (source_sha256, entry["group_id"])
            if group_key in seen_groups:
                raise BallAnnotationServiceError(
                    "invalid_group_registry", "Temporal group registry contains duplicates"
                )
            seen_groups.add(group_key)
            observed_session = (source_sha256, data_role, state)
            previous_session = session_authority.setdefault(session_id, observed_session)
            if previous_session != observed_session:
                raise BallAnnotationServiceError("invalid_group_registry", "Session temporal group states disagree")
            span = (entry["start_frame"], entry["end_frame"])
            previous_cluster = previous_by_source.get(source_sha256)
            if previous_cluster is not None and previous_cluster[0] >= span[0]:
                if previous_cluster[1] != session_id:
                    raise BallAnnotationServiceError(
                        "invalid_group_registry",
                        "Temporal group registry spans overlap across sessions",
                    )
                previous_by_source[source_sha256] = (
                    max(previous_cluster[0], span[1]),
                    session_id,
                )
            else:
                previous_by_source[source_sha256] = (span[1], session_id)
        if require_sorted and entries != sorted(entries, key=cls._registry_entry_sort_key):
            raise BallAnnotationServiceError("invalid_group_registry", "Temporal group registry is not canonical")

    def _load_session(self, session_id: str) -> dict[str, Any]:
        session_id = require_safe_id(session_id, "annotation session_id")
        path = self._sessions_root / f"{session_id}.json"
        if not path.is_file():
            raise BallAnnotationServiceError("session_not_found", "Annotation session was not found", status_code=404)
        session = self._read_json(path, "annotation session", _MAX_SESSION_BYTES)
        if session.get("artifact_type") != "ball_annotation_session" or session.get("session_id") != session_id:
            raise BallAnnotationServiceError("invalid_session", "Persisted annotation session is invalid")
        self._session_request_authority(session)
        return session

    def _persist_session(self, session: dict[str, Any]) -> None:
        session_id = require_safe_id(session.get("session_id"), "annotation session_id")
        self._session_request_authority(session)
        self._validate_session_resource_bounds(session)
        atomic_write_json(self._sessions_root / f"{session_id}.json", session, trusted_root=self._sessions_root)

    @staticmethod
    def _validate_session_resource_bounds(session: dict[str, Any]) -> None:
        """Reject prospective growth before replacing the last readable session."""

        revisions = session.get("revisions")
        if not isinstance(revisions, list):
            raise BallAnnotationServiceError(
                "invalid_session",
                "Annotation session revision history is invalid",
            )
        if len(revisions) > _MAX_REVISIONS_PER_SESSION:
            raise BallAnnotationServiceError(
                "resource_limit_exceeded",
                "Annotation session revision limit would be exceeded",
                status_code=409,
            )
        revisions_by_frame: dict[int, int] = {}
        for revision in revisions:
            frame_index = revision.get("frame_index") if isinstance(revision, dict) else None
            if isinstance(frame_index, bool) or not isinstance(frame_index, int) or frame_index < 0:
                raise BallAnnotationServiceError(
                    "invalid_session",
                    "Annotation session revision history is invalid",
                )
            revisions_by_frame[frame_index] = revisions_by_frame.get(frame_index, 0) + 1
            if revisions_by_frame[frame_index] > _MAX_REVISIONS_PER_FRAME:
                raise BallAnnotationServiceError(
                    "resource_limit_exceeded",
                    "Per-frame annotation revision limit would be exceeded",
                    status_code=409,
                )

        canonical_size = len(canonical_json_bytes(session))
        if canonical_size > _MAX_SESSION_CANONICAL_BYTES:
            raise BallAnnotationServiceError(
                "resource_limit_exceeded",
                "Annotation session canonical byte limit would be exceeded",
                status_code=409,
            )
        persisted_size = len(
            (
                json.dumps(
                    session,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            ).encode("utf-8")
        )
        if persisted_size > _MAX_SESSION_BYTES:
            raise BallAnnotationServiceError(
                "resource_limit_exceeded",
                "Annotation session persisted byte limit would be exceeded",
                status_code=409,
            )

    @staticmethod
    def _expected_sampling_lock(session: dict[str, Any]) -> dict[str, Any]:
        session_id = require_safe_id(session.get("session_id"), "annotation session_id")
        manifest_sha256 = require_sha256(
            session.get("sampling_manifest", {}).get("manifest_sha256"),
            "sampling manifest sha256",
        )
        lock: dict[str, Any] = {
            "schema_version": "1.0",
            "artifact_type": "ball_annotation_sampling_lock",
            "session_id": session_id,
            "sampling_manifest_sha256": manifest_sha256,
            "source_sha256": session["source"]["sha256"],
            "locked_profile_id": session["locked_profile"]["profile_id"],
            "locked_profile_sha256": session["locked_profile"]["profile_sha256"],
            "locked_before_probe": True,
            "created_at": session["created_at"],
        }
        lock["lock_sha256"] = canonical_sha256(lock)
        return lock

    @staticmethod
    def _expected_check_probe_creation_authority(
        session: dict[str, Any],
        sampling_lock: dict[str, Any],
    ) -> dict[str, Any]:
        manifest = session.get("sampling_manifest")
        if not isinstance(manifest, dict):
            raise BallAnnotationServiceError(
                "invalid_check_probe_authority",
                "Check sampling manifest authority is missing",
                status_code=409,
            )
        manifest_payload = deepcopy(manifest)
        manifest_sha256 = manifest_payload.pop("manifest_sha256", None)
        frame_indices = manifest.get("frame_indices")
        groups = manifest.get("groups")
        source = session.get("source")
        source_frame_count = source.get("frame_count") if isinstance(source, dict) else None
        development_binding = session.get("development_package_binding")
        locked_profile = session.get("locked_profile")
        control_profile = session.get("control_profile")
        lineage = session.get("lineage")
        if (
            not isinstance(manifest_sha256, str)
            or canonical_sha256(manifest_payload) != manifest_sha256
            or not isinstance(frame_indices, list)
            or not 20 <= len(frame_indices) <= 50
            or any(isinstance(frame_index, bool) or not isinstance(frame_index, int) for frame_index in frame_indices)
            or frame_indices != sorted(set(frame_indices))
            or not isinstance(groups, list)
            or not isinstance(source, dict)
            or isinstance(source_frame_count, bool)
            or not isinstance(source_frame_count, int)
            or source_frame_count <= 0
            or any(not 0 <= frame_index < source_frame_count for frame_index in frame_indices)
            or not isinstance(development_binding, dict)
            or not isinstance(locked_profile, dict)
            or not isinstance(control_profile, dict)
            or not isinstance(lineage, dict)
            or manifest.get("data_role") != "check"
            or manifest.get("target_frame_count") != len(frame_indices)
            or manifest.get("locked_before_probe") is not True
            or manifest.get("source_sha256") != source.get("sha256")
            or manifest.get("locked_profile_id") != locked_profile.get("profile_id")
            or manifest.get("locked_profile_sha256") != locked_profile.get("profile_sha256")
            or manifest.get("profile_id") != session.get("sampling_profile_id")
            or manifest.get("metric_profile_id") != session.get("metric_profile_id")
            or manifest.get("metric_profile_sha256") != session.get("metric_profile_sha256")
            or len(groups) != len(frame_indices)
            or any(not isinstance(group, dict) for group in groups)
            or [group.get("frame_index") for group in groups] != frame_indices
            or control_profile.get("profile_id") != session.get("control_profile_id")
        ):
            raise BallAnnotationServiceError(
                "invalid_check_probe_authority",
                "Check sampling manifest authority is invalid",
                status_code=409,
            )
        profile_bindings = sorted(
            [
                {
                    "profile_id": require_safe_id(locked_profile.get("profile_id"), "locked profile_id"),
                    "profile_sha256": require_sha256(locked_profile.get("profile_sha256"), "locked profile sha256"),
                },
                {
                    "profile_id": require_safe_id(control_profile.get("profile_id"), "control profile_id"),
                    "profile_sha256": require_sha256(control_profile.get("profile_sha256"), "control profile sha256"),
                },
            ],
            key=lambda item: item["profile_id"],
        )
        authority: dict[str, Any] = {
            "schema_version": "1.0",
            "artifact_type": "ball_annotation_check_probe_creation_authority",
            "session_id": require_safe_id(session.get("session_id"), "annotation session_id"),
            "attempt_family_sha256": require_sha256(
                session.get("attempt_family_sha256"), "annotation attempt family sha256"
            ),
            "development_package_sha256": require_sha256(
                development_binding.get("package_sha256"), "development annotation package sha256"
            ),
            "parent_trial_id": require_safe_id(lineage.get("parent_trial_id"), "parent trial_id"),
            "source_sha256": require_sha256(source.get("sha256"), "annotation source sha256"),
            "source_frame_count": source_frame_count,
            "sampling_manifest_sha256": require_sha256(manifest_sha256, "sampling manifest sha256"),
            "sampling_lock_sha256": require_sha256(sampling_lock.get("lock_sha256"), "sampling lock sha256"),
            "frame_indices": deepcopy(frame_indices),
            "profile_bindings": profile_bindings,
        }
        authority["authority_sha256"] = canonical_sha256(authority)
        return authority

    def _require_reserved_check_groups(self, session: dict[str, Any]) -> None:
        source_sha256 = session["source"]["sha256"]
        expected_groups = {
            group["group_id"]: self._canonical_registry_group(group, source_sha256)
            for group in session["sampling_manifest"]["groups"]
        }
        registry = self._read_registry()
        observed = [
            entry
            for entry in registry["entries"]
            if entry.get("source_sha256") == source_sha256 and entry.get("group_id") in expected_groups
        ]
        if (
            len(observed) != len(expected_groups)
            or {entry.get("group_id") for entry in observed} != set(expected_groups)
            or any(
                entry.get("session_id") != session["session_id"]
                or entry.get("data_role") != "check"
                or entry.get("state") != "reserved"
                or entry.get("retired_for_all_profiles") is not False
                or self._registry_group(entry) != expected_groups[entry["group_id"]]
                for entry in observed
            )
        ):
            raise BallAnnotationServiceError(
                "check_probe_reservation_mismatch",
                "Check sampling groups are not exactly reserved by this session",
                status_code=409,
            )

    def _persist_sampling_lock(self, session: dict[str, Any]) -> None:
        lock = self._expected_sampling_lock(session)
        session_id = lock["session_id"]
        path = self._sampling_locks_root / f"{session_id}.json"
        if path.exists():
            existing = self._read_json(path, "sampling lock", _MAX_SESSION_BYTES)
            if existing != lock:
                raise BallAnnotationServiceError(
                    "sampling_lock_conflict", "Immutable pre-reveal sampling lock already differs"
                )
            return
        atomic_write_json(path, lock, trusted_root=self._sampling_locks_root)

    def _require_verified_sampling_lock(
        self,
        session: dict[str, Any],
    ) -> dict[str, Any]:
        session_id = require_safe_id(session.get("session_id"), "annotation session_id")
        path = self._sampling_locks_root / f"{session_id}.json"
        if not path.is_file():
            raise BallAnnotationServiceError(
                "sampling_lock_not_found",
                "Pre-reveal sampling lock was not found",
                status_code=409,
            )
        observed = self._read_json(path, "sampling lock", _MAX_SESSION_BYTES)
        expected = self._expected_sampling_lock(session)
        if observed != expected:
            raise BallAnnotationServiceError(
                "sampling_lock_conflict",
                "Immutable pre-reveal sampling lock differs from the session authority",
                status_code=409,
            )
        return observed

    def _get_sampling_lock(self, session_id: str) -> dict[str, Any]:
        session_id = require_safe_id(session_id, "annotation session_id")
        return self._require_verified_sampling_lock(self._load_session(session_id))

    def _load_propagation(self, job_id: str) -> dict[str, Any]:
        job_id = require_safe_id(job_id, "propagation job_id")
        path = self._propagation_root / f"{job_id}.json"
        if not path.is_file():
            raise BallAnnotationServiceError("propagation_not_found", "Propagation job was not found", status_code=404)
        job = self._read_json(path, "propagation job", _MAX_SESSION_BYTES)
        if job.get("artifact_type") != "ball_propagation_job" or job.get("job_id") != job_id:
            raise BallAnnotationServiceError("invalid_propagation_job", "Persisted propagation job is invalid")
        return job

    def _persist_propagation(self, job: dict[str, Any]) -> None:
        job_id = require_safe_id(job.get("job_id"), "propagation job_id")
        atomic_write_json(self._propagation_root / f"{job_id}.json", job, trusted_root=self._propagation_root)

    @staticmethod
    def _read_json(path: Path, label: str, max_bytes: int) -> dict[str, Any]:
        content, _digest = read_regular_bytes(path, label, max_bytes=max_bytes, trusted_root=path.parent)
        return json_object_from_bytes(content, label)

    @staticmethod
    def _frame(session: dict[str, Any], frame_index: int) -> dict[str, Any]:
        if isinstance(frame_index, bool) or not isinstance(frame_index, int) or frame_index < 0:
            raise BallAnnotationServiceError("invalid_frame_index", "frame_index is invalid", status_code=400)
        frame = next((item for item in session["frames"] if item["frame_index"] == frame_index), None)
        if frame is None:
            raise BallAnnotationServiceError("frame_not_found", "Frame is outside the frozen session", status_code=404)
        return frame

    @staticmethod
    def _parse_if_match(value: str) -> str:
        if (
            not isinstance(value, str)
            or value.startswith("W/")
            or "," in value
            or value == "*"
            or len(value) < 2
            or not value.startswith('"')
            or not value.endswith('"')
        ):
            raise BallAnnotationServiceError(
                "invalid_if_match", "If-Match must contain one strong annotation ETag", status_code=400
            )
        return value[1:-1]

    def _discard_unstarted_session(
        self,
        session_id: str,
        *,
        retry_reservation_snapshot: list[dict[str, Any]] | None = None,
    ) -> None:
        """Compensate setup failures before any server-owned probe can start."""

        session_id = require_safe_id(session_id, "annotation session_id")
        if retry_reservation_snapshot:
            self._restore_retry_reservation_snapshot(
                retry_reservation_snapshot,
                retry_session_id=session_id,
            )
        (self._sessions_root / f"{session_id}.json").unlink(missing_ok=True)
        (self._sampling_locks_root / f"{session_id}.json").unlink(missing_ok=True)
        registry = self._read_registry()
        retained = [entry for entry in registry["entries"] if entry.get("session_id") != session_id]
        if len(retained) != len(registry["entries"]):
            registry["entries"] = retained
            self._write_registry(registry)

    @staticmethod
    def _detector_candidate_decisions(
        session: dict[str, Any],
    ) -> dict[tuple[int, str], str]:
        decisions: dict[tuple[int, str], str] = {}
        for revision in session.get("revisions", []):
            for kind, suggestion_id, decision in (
                (
                    revision.get("accepted_suggestion_kind"),
                    revision.get("accepted_suggestion_id"),
                    "accepted",
                ),
                (
                    revision.get("dismissed_suggestion_kind"),
                    revision.get("dismissed_suggestion_id"),
                    "dismissed",
                ),
            ):
                if kind != "detector_candidate" or suggestion_id is None:
                    continue
                key = (revision.get("frame_index"), suggestion_id)
                if key in decisions:
                    raise BallAnnotationServiceError(
                        "invalid_detector_candidate_evidence",
                        "Detector candidate has more than one human decision",
                    )
                decisions[key] = decision
        return decisions

    @classmethod
    def _decided_detector_candidate_keys(
        cls,
        session: dict[str, Any],
    ) -> set[tuple[int, str]]:
        return set(cls._detector_candidate_decisions(session))

    @classmethod
    def _pending_detector_candidate_count(cls, session: dict[str, Any]) -> int:
        decided = cls._decided_detector_candidate_keys(session)
        return sum(
            (frame["frame_index"], candidate["candidate_id"]) not in decided
            for frame in session.get("frames", [])
            if frame.get("primary_sample") is True
            for candidate in frame.get("suggested_candidates", [])
        )

    @staticmethod
    def _public_session(session: dict[str, Any]) -> dict[str, Any]:
        public = deepcopy(session)
        detector_candidate_decisions = BallAnnotationService._detector_candidate_decisions(public)
        public.pop("revisions", None)
        public.pop("final_result", None)
        public.pop("finalize_mutation_id", None)
        public.pop("finalization_started_at", None)
        public.pop("finalization_input_sha256", None)
        public.pop("_retry_from_probe_job_id", None)
        public.pop("_retry_reservation_snapshot", None)
        public.pop("_retry_reservation_transfer_updated_at", None)
        public.pop("_initial_check_setup_transaction", None)
        public.pop("_normalized_session_request", None)
        public.pop("_frame_review_proxy_authority", None)
        public.pop("_detector_probe_authorities", None)
        for frame in public["frames"]:
            true_pts = frame.get(
                "_true_presentation_timestamp",
                _TRUE_PRESENTATION_TIMESTAMP_NOT_COLLECTED,
            )
            if true_pts != _TRUE_PRESENTATION_TIMESTAMP_NOT_COLLECTED:
                raise BallAnnotationServiceError(
                    "invalid_frame_timing",
                    "True presentation timestamp authority is invalid",
                )
            frame["true_presentation_timestamp"] = deepcopy(true_pts)
            for key in list(frame):
                if key.startswith("_"):
                    frame.pop(key)
            for candidate in frame["suggested_candidates"]:
                candidate["decision"] = detector_candidate_decisions.get(
                    (frame["frame_index"], candidate["candidate_id"]),
                    "pending",
                )
        primary_frames = [frame for frame in public["frames"] if frame.get("primary_sample") is True]
        supplemental_frames = [frame for frame in public["frames"] if frame.get("frame_role") == "propagation_target"]
        public["progress"] = {
            "annotated_frames": sum(frame["current_annotation"] is not None for frame in public["frames"]),
            "total_frames": len(public["frames"]),
            "unconfirmed_suggestions": sum(
                sum(candidate["decision"] == "pending" for candidate in frame["suggested_candidates"])
                for frame in public["frames"]
            ),
            "primary_annotated_frames": sum(frame["current_annotation"] is not None for frame in primary_frames),
            "primary_total_frames": len(primary_frames),
            "supplemental_annotated_frames": sum(
                frame["current_annotation"] is not None for frame in supplemental_frames
            ),
            "supplemental_total_frames": len(supplemental_frames),
            "unconfirmed_propagation_suggestions": sum(
                sum(
                    suggestion.get("pending_human_confirmation") is True
                    for suggestion in frame.get("propagation_suggestions", [])
                )
                for frame in public["frames"]
            ),
        }
        return public

    @staticmethod
    def _public_revision(revision: dict[str, Any]) -> dict[str, Any]:
        public = deepcopy(revision)
        public.pop("mutation_sha256", None)
        public.pop("previous_effective_annotation", None)
        return public

    @staticmethod
    def _validate_jpeg(content: bytes, width: int, height: int) -> None:
        try:
            import cv2
            import numpy as np

            image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
        except Exception as exc:
            raise BallAnnotationServiceError("frame_decode_failed", "Exact frame JPEG could not be decoded") from exc
        if image is None or image.shape[1] != width or image.shape[0] != height:
            raise BallAnnotationServiceError("frame_dimension_mismatch", "Exact frame JPEG dimensions changed")

    @staticmethod
    def _positive_int(value: Any, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise BallAnnotationServiceError("invalid_probe_authority", f"{label} must be a positive integer")
        return value

    @staticmethod
    def _normalize_strata_applicability(
        value: Any,
    ) -> tuple[dict[str, list[dict[str, Any]]], list[str], list[str]]:
        if not isinstance(value, dict) or set(value) != {"scale", "lighting"}:
            raise BallAnnotationServiceError(
                "invalid_strata", "Full pre-reveal scale and lighting applicability is required", status_code=400
            )
        normalized: dict[str, list[dict[str, Any]]] = {"scale": [], "lighting": []}
        applicable: dict[str, list[str]] = {"scale": [], "lighting": []}
        for dimension, expected in (("scale", _SCALE_STRATA), ("lighting", _LIGHTING_STRATA)):
            rows = value.get(dimension)
            if not isinstance(rows, list) or len(rows) != len(expected):
                raise BallAnnotationServiceError(
                    "invalid_strata", f"Every {dimension} stratum requires a pre-reveal declaration", status_code=400
                )
            by_name = {row.get("stratum"): row for row in rows if isinstance(row, dict)}
            if set(by_name) != set(expected) or len(by_name) != len(rows):
                raise BallAnnotationServiceError(
                    "invalid_strata", f"{dimension} applicability is incomplete or duplicated", status_code=400
                )
            for stratum in expected:
                row = by_name[stratum]
                expected_fields = {"stratum", "status", "evidence_note"}
                if dimension == "lighting":
                    expected_fields |= {"quota", "frame_intervals"}
                if set(row) != expected_fields or row.get("status") not in {"applicable", "not_applicable"}:
                    raise BallAnnotationServiceError(
                        "invalid_strata", f"{dimension} applicability row is invalid", status_code=400
                    )
                note = row.get("evidence_note")
                if not isinstance(note, str) or len(note.strip()) < 3 or len(note) > 500:
                    raise BallAnnotationServiceError(
                        "invalid_strata", f"{dimension} applicability requires bounded evidence", status_code=400
                    )
                status = row["status"]
                authority: dict[str, Any] = {
                    "dimension": dimension,
                    "stratum": stratum,
                    "status": status,
                    "note": note,
                }
                normalized_row: dict[str, Any] = {
                    "stratum": stratum,
                    "status": status,
                }
                if dimension == "lighting":
                    quota = row.get("quota")
                    intervals = row.get("frame_intervals")
                    if (
                        isinstance(quota, bool)
                        or not isinstance(quota, int)
                        or not 0 <= quota <= 50
                        or not isinstance(intervals, list)
                        or len(intervals) > 32
                    ):
                        raise BallAnnotationServiceError(
                            "invalid_strata",
                            "lighting quota and frame intervals are invalid",
                            status_code=400,
                        )
                    normalized_intervals: list[dict[str, int]] = []
                    for interval in intervals:
                        if not isinstance(interval, dict) or set(interval) != {
                            "start_frame",
                            "end_frame",
                        }:
                            raise BallAnnotationServiceError(
                                "invalid_strata",
                                "lighting frame interval is invalid",
                                status_code=400,
                            )
                        start = interval.get("start_frame")
                        end = interval.get("end_frame")
                        if (
                            isinstance(start, bool)
                            or not isinstance(start, int)
                            or isinstance(end, bool)
                            or not isinstance(end, int)
                            or start < 0
                            or end < start
                        ):
                            raise BallAnnotationServiceError(
                                "invalid_strata",
                                "lighting frame interval bounds are invalid",
                                status_code=400,
                            )
                        normalized_intervals.append({"start_frame": start, "end_frame": end})
                    if status == "applicable":
                        if (quota == 0) != (not normalized_intervals):
                            raise BallAnnotationServiceError(
                                "invalid_strata",
                                "applicable lighting quota and intervals must both be present or both be zero for development",
                                status_code=400,
                            )
                    elif quota != 0 or normalized_intervals:
                        raise BallAnnotationServiceError(
                            "invalid_strata",
                            "not-applicable lighting cannot receive quota or intervals",
                            status_code=400,
                        )
                    normalized_row.update(
                        {
                            "quota": quota,
                            "frame_intervals": normalized_intervals,
                        }
                    )
                    authority.update({"quota": quota, "frame_intervals": normalized_intervals})
                normalized_row["evidence"] = {
                    "declared_before_reveal": True,
                    "note": note,
                    "evidence_sha256": canonical_sha256(authority),
                }
                normalized[dimension].append(normalized_row)
                if status == "applicable":
                    applicable[dimension].append(stratum)
        if not applicable["scale"] or not applicable["lighting"]:
            raise BallAnnotationServiceError(
                "invalid_strata", "At least one scale and lighting stratum must be applicable", status_code=400
            )
        return normalized, applicable["scale"], applicable["lighting"]

    def _require_open(self) -> None:
        if self._closed:
            raise BallAnnotationServiceError("service_closed", "Ball annotation service is closed", status_code=503)


__all__ = ["BallAnnotationService", "BallAnnotationServiceError"]
