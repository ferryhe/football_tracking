from __future__ import annotations

import argparse
import errno
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from football_tracking.detector_development_common import (
    CorruptProbeFrameError,
    DetectorDevelopmentError,
    atomic_write_json,
    is_link_or_reparse,
    json_object_from_bytes,
    read_regular_bytes,
)

_CONTROL_FILE_LIMIT = 64 * 1024 * 1024
_WORKER_EXIT_HEARTBEAT_FAILED = 71
_WORKER_EXIT_ERROR_ENVELOPE_UNAVAILABLE = 75
_WORKER_EXIT_DISK_EXHAUSTED = 77
_WORKER_EXIT_CONTAINMENT_UNAVAILABLE = 78
_WORKER_PRESTART_DEADLINE_SECONDS = 10.0
_WAIT_TIMEOUT = 0x00000102


class _WindowsParentMonitor:
    def __init__(self, kernel32: Any, handle: Any) -> None:
        self._kernel32 = kernel32
        self._handle = handle

    @classmethod
    def open(cls, parent_pid: int) -> _WindowsParentMonitor | None:
        if parent_pid <= 0:
            return None
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                ctypes.c_uint32,
                ctypes.c_int,
                ctypes.c_uint32,
            ]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel32.WaitForSingleObject.restype = ctypes.c_uint32
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            handle = kernel32.OpenProcess(0x00100000, False, parent_pid)
        except BaseException:
            return None
        return cls(kernel32, handle) if handle else None

    def is_alive(self) -> bool:
        if self._handle is None:
            return False
        try:
            return self._kernel32.WaitForSingleObject(self._handle, 0) == _WAIT_TIMEOUT
        except BaseException:
            return False

    def close(self) -> bool:
        handle, self._handle = self._handle, None
        if handle is None:
            return True
        try:
            return bool(self._kernel32.CloseHandle(handle))
        except BaseException:
            return False


class _PosixParentMonitor:
    def __init__(self, parent_pid: int) -> None:
        self._parent_pid = parent_pid

    def is_alive(self) -> bool:
        return _parent_is_alive(self._parent_pid)

    @staticmethod
    def close() -> bool:
        return True


def _open_parent_monitor(parent_pid: int) -> _WindowsParentMonitor | _PosixParentMonitor | None:
    if os.name == "nt":
        return _WindowsParentMonitor.open(parent_pid)
    return _PosixParentMonitor(parent_pid) if parent_pid > 0 else None


def _read_control(path: Path, control_root: Path, label: str) -> dict[str, Any]:
    content, _ = read_regular_bytes(
        path,
        label,
        max_bytes=_CONTROL_FILE_LIMIT,
        trusted_root=control_root,
    )
    return json_object_from_bytes(content, label)


def _parent_is_alive(parent_pid: int) -> bool:
    if parent_pid <= 0:
        return False
    if os.name != "nt":
        if os.getppid() != parent_pid:
            return False
        try:
            os.kill(parent_pid, 0)
        except OSError:
            return False
        return True
    monitor = _WindowsParentMonitor.open(parent_pid)
    if monitor is None:
        return False
    try:
        alive = monitor.is_alive()
    finally:
        closed = monitor.close()
    return alive and closed


def _install_parent_death_containment(
    parent_pid: int,
    parent_alive: Callable[[], bool] | None = None,
) -> bool:
    if sys.platform.startswith("linux"):
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        libc.prctl.restype = ctypes.c_int
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            return False
        # The parent may have exited between spawn and prctl().
        return os.getppid() == parent_pid
    return (parent_alive or (lambda: _parent_is_alive(parent_pid)))()


def _start_parent_watchdog() -> bool:
    if os.name == "nt":
        return True
    raw_fd = os.environ.pop("FOOTBALL_TRACKING_PARENT_WATCH_FD", None)
    try:
        read_fd = int(raw_fd) if raw_fd is not None else -1
    except ValueError:
        return False
    if read_fd < 3:
        return False
    try:
        watchdog_pid = os.fork()
    except OSError:
        os.close(read_fd)
        return False
    if watchdog_pid == 0:
        try:
            while os.read(read_fd, 1):
                pass
            os.killpg(os.getpgrp(), signal.SIGKILL)
        except BaseException:
            os._exit(_WORKER_EXIT_CONTAINMENT_UNAVAILABLE)
        os._exit(_WORKER_EXIT_CONTAINMENT_UNAVAILABLE)
    os.close(read_fd)
    return True


def _wait_for_start(
    control_root: Path,
    worker_id: str,
    parent_pid: int,
    parent_alive: Callable[[], bool] | None = None,
) -> bool:
    start_path = control_root / "start.json"
    abort_path = control_root / "abort.json"
    is_parent_alive = parent_alive or (lambda: _parent_is_alive(parent_pid))
    deadline = time.monotonic() + _WORKER_PRESTART_DEADLINE_SECONDS
    while True:
        if time.monotonic() >= deadline or not is_parent_alive():
            return False
        if abort_path.exists() or is_link_or_reparse(abort_path):
            payload = _read_control(
                abort_path, control_root, "detector probe worker pre-start abort"
            )
            if (
                set(payload)
                != {
                    "schema_version",
                    "artifact_type",
                    "worker_id",
                    "abort_requested",
                }
                or payload.get("schema_version") != "1.0"
                or payload.get("artifact_type") != "detector_probe_worker_abort"
                or payload.get("worker_id") != worker_id
                or payload.get("abort_requested") is not True
            ):
                return False
            atomic_write_json(
                control_root / "abort-ack.json",
                {
                    "schema_version": "1.0",
                    "artifact_type": "detector_probe_worker_abort_ack",
                    "worker_id": worker_id,
                    "worker_pid": os.getpid(),
                },
                trusted_root=control_root,
            )
            return False
        if start_path.exists() or is_link_or_reparse(start_path):
            payload = _read_control(
                start_path, control_root, "detector probe worker start gate"
            )
            if (
                set(payload)
                != {
                    "schema_version",
                    "artifact_type",
                    "worker_id",
                    "launcher_pid",
                    "parent_pid",
                }
                or payload.get("schema_version") != "1.0"
                or payload.get("artifact_type") != "detector_probe_worker_start"
                or payload.get("worker_id") != worker_id
                or isinstance(payload.get("launcher_pid"), bool)
                or not isinstance(payload.get("launcher_pid"), int)
                or payload["launcher_pid"] <= 0
                or payload.get("parent_pid") != parent_pid
            ):
                return False
            return True
        time.sleep(0.02)


def _cancel_requested(control_root: Path, worker_id: str) -> bool:
    payload = _read_control(
        control_root / "cancel.json",
        control_root,
        "detector probe worker cancellation",
    )
    if (
        payload.get("schema_version") != "1.0"
        or payload.get("artifact_type") != "detector_probe_worker_cancel"
        or payload.get("worker_id") != worker_id
        or not isinstance(payload.get("cancel_requested"), bool)
    ):
        raise DetectorDevelopmentError(
            "invalid_worker_protocol", "Detector probe worker cancellation is invalid"
        )
    return payload["cancel_requested"]


def _worker_error(exc: BaseException) -> tuple[str, int]:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, OSError) and current.errno == errno.ENOSPC:
            return "disk_exhausted", 409
        current = current.__cause__ or current.__context__
    if isinstance(exc, DetectorDevelopmentError):
        return exc.code, exc.status_code
    if isinstance(exc, MemoryError) or (
        type(exc).__module__.startswith("torch")
        and type(exc).__name__ == "OutOfMemoryError"
    ):
        return "device_out_of_memory", 409
    if isinstance(exc, CorruptProbeFrameError):
        return "corrupt_frame", 409
    if (
        type(exc).__module__ == "football_tracking.detector_probe_runner"
        and type(exc).__name__ == "ArtifactWriteError"
    ):
        return "artifact_write_failed", 409
    return "probe_failed", 409


def _heartbeat_loop(
    stop: threading.Event,
    control_root: Path,
    worker_id: str,
    parent_pid: int,
    parent_alive: Callable[[], bool] | None = None,
) -> None:
    sequence = 0
    is_parent_alive = parent_alive or (lambda: _parent_is_alive(parent_pid))
    while not stop.wait(0.25):
        if not is_parent_alive():
            os._exit(70)
        sequence += 1
        try:
            atomic_write_json(
                control_root / "heartbeat.json",
                {
                    "schema_version": "1.0",
                    "artifact_type": "detector_probe_worker_heartbeat",
                    "worker_id": worker_id,
                    "worker_pid": os.getpid(),
                    "parent_pid": parent_pid,
                    "sequence": sequence,
                },
                trusted_root=control_root,
            )
        except BaseException as exc:
            code, _status_code = _worker_error(exc)
            os._exit(
                _WORKER_EXIT_DISK_EXHAUSTED
                if code == "disk_exhausted"
                else _WORKER_EXIT_HEARTBEAT_FAILED
            )


def _run_worker_with_monitor(
    control_root: Path,
    staging_root: Path,
    parent_pid: int,
    parent_monitor: _WindowsParentMonitor | _PosixParentMonitor,
) -> int:
    if not _start_parent_watchdog() or not _install_parent_death_containment(
        parent_pid, parent_monitor.is_alive
    ):
        return _WORKER_EXIT_CONTAINMENT_UNAVAILABLE
    control_root = control_root.resolve(strict=True)
    staging_root = staging_root.resolve(strict=True)
    if (
        control_root.parent != staging_root
        or control_root.name != ".worker-control"
        or is_link_or_reparse(control_root)
        or is_link_or_reparse(staging_root)
    ):
        return 72
    envelope = _read_control(
        control_root / "input.json", control_root, "detector probe worker input"
    )
    if (
        envelope.get("schema_version") != "1.0"
        or envelope.get("artifact_type") != "detector_probe_worker_input"
        or not isinstance(envelope.get("worker_id"), str)
        or not isinstance(envelope.get("request"), dict)
        or not isinstance(envelope.get("profiles"), list)
    ):
        return 73
    worker_id = envelope["worker_id"]
    if not _wait_for_start(control_root, worker_id, parent_pid, parent_monitor.is_alive):
        return _WORKER_EXIT_CONTAINMENT_UNAVAILABLE
    from football_tracking.detector_probe_runner import run_detector_probe

    stop = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat_loop,
        args=(stop, control_root, worker_id, parent_pid, parent_monitor.is_alive),
        name="detector-probe-worker-heartbeat",
        daemon=True,
    )
    heartbeat.start()

    def progress(completed: Any, total: Any) -> None:
        if (
            isinstance(completed, bool)
            or not isinstance(completed, int)
            or completed < 0
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total <= 0
            or completed > total
        ):
            raise DetectorDevelopmentError(
                "invalid_probe_progress", "Detector probe worker progress is invalid"
            )
        atomic_write_json(
            control_root / "progress.json",
            {
                "schema_version": "1.0",
                "artifact_type": "detector_probe_worker_progress",
                "worker_id": worker_id,
                "completed": completed,
                "total": total,
            },
            trusted_root=control_root,
        )

    try:
        output = run_detector_probe(
            envelope["request"],
            envelope["profiles"],
            staging_root,
            lambda: _cancel_requested(control_root, worker_id),
            progress,
        )
        atomic_write_json(
            control_root / "result.json",
            {
                "schema_version": "1.0",
                "artifact_type": "detector_probe_worker_result",
                "worker_id": worker_id,
                "runner_output": output,
            },
            trusted_root=control_root,
        )
        return 0
    except BaseException as exc:
        code, status_code = _worker_error(exc)
        try:
            atomic_write_json(
                control_root / "error.json",
                {
                    "schema_version": "1.0",
                    "artifact_type": "detector_probe_worker_error",
                    "worker_id": worker_id,
                    "code": code,
                    "status_code": status_code,
                },
                trusted_root=control_root,
            )
        except Exception:
            return (
                _WORKER_EXIT_DISK_EXHAUSTED
                if code == "disk_exhausted"
                else _WORKER_EXIT_ERROR_ENVELOPE_UNAVAILABLE
            )
        return 74
    finally:
        stop.set()
        heartbeat.join()


def run_worker(control_root: Path, staging_root: Path, parent_pid: int) -> int:
    try:
        parent_monitor = _open_parent_monitor(parent_pid)
    except BaseException:
        return _WORKER_EXIT_CONTAINMENT_UNAVAILABLE
    if parent_monitor is None:
        return _WORKER_EXIT_CONTAINMENT_UNAVAILABLE
    try:
        result = _run_worker_with_monitor(
            control_root,
            staging_root,
            parent_pid,
            parent_monitor,
        )
    except BaseException:
        parent_monitor.close()
        raise
    return result if parent_monitor.close() else _WORKER_EXIT_CONTAINMENT_UNAVAILABLE


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-dir", type=Path, required=True)
    parser.add_argument("--staging-dir", type=Path, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args()
    try:
        return run_worker(args.control_dir, args.staging_dir, args.parent_pid)
    except BaseException as exc:
        code, _status_code = _worker_error(exc)
        return _WORKER_EXIT_DISK_EXHAUSTED if code == "disk_exhausted" else 76


if __name__ == "__main__":
    raise SystemExit(main())
