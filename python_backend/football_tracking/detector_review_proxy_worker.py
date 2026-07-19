from __future__ import annotations

import argparse
import os
import signal
import threading
from pathlib import Path
from typing import Any

from football_tracking.detector_development_common import (
    DetectorDevelopmentError,
    atomic_write_json,
    is_link_or_reparse,
    json_object_from_bytes,
    read_regular_bytes,
)
from football_tracking.detector_probe_worker import _open_parent_monitor
from football_tracking.detector_review_proxy import (
    _safe_review_proxy_failure_code,
    _safe_review_proxy_failure_message,
    run_detector_review_proxy,
)


def _read_control(path: Path, root: Path, label: str) -> dict[str, Any]:
    content, _ = read_regular_bytes(path, label, max_bytes=64 * 1024 * 1024, trusted_root=root)
    return json_object_from_bytes(content, label)


def _cancel_requested(control: Path, worker_id: str) -> bool:
    payload = _read_control(control / "cancel.json", control, "review proxy worker cancellation")
    if (
        payload.get("artifact_type") != "detector_review_proxy_worker_cancel"
        or payload.get("worker_id") != worker_id
        or not isinstance(payload.get("cancel_requested"), bool)
    ):
        raise DetectorDevelopmentError("invalid_worker_protocol", "Review proxy worker cancellation is invalid")
    return payload["cancel_requested"]


def _heartbeat_loop(
    stop: threading.Event,
    control: Path,
    worker_id: str,
    parent_pid: int,
    parent_monitor: Any,
) -> None:
    sequence = 0
    while not stop.wait(0.5):
        if not parent_monitor.is_alive():
            if os.name != "nt":
                try:
                    os.killpg(os.getpgrp(), signal.SIGKILL)
                except OSError:
                    pass
            os._exit(70)
        sequence += 1
        try:
            atomic_write_json(
                control / "heartbeat.json",
                {
                    "schema_version": "1.0",
                    "artifact_type": "detector_review_proxy_worker_heartbeat",
                    "worker_id": worker_id,
                    "worker_pid": os.getpid(),
                    "parent_pid": parent_pid,
                    "sequence": sequence,
                },
                trusted_root=control,
            )
        except BaseException:
            os._exit(71)


def _error_binding(exc: BaseException) -> tuple[str, int]:
    if isinstance(exc, DetectorDevelopmentError):
        return exc.code, exc.status_code
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return "disk_exhausted", 409
    if isinstance(exc, MemoryError):
        return "host_out_of_memory", 409
    return "review_proxy_failed", 409


def run_worker(control: Path, staging: Path, parent_pid: int) -> int:
    try:
        control = control.resolve(strict=True)
        staging = staging.resolve(strict=True)
    except OSError:
        return 72
    protocol = control.parent
    if (
        protocol.parent != staging
        or protocol.name != ".worker-protocol"
        or control.name != "control"
        or is_link_or_reparse(control)
        or is_link_or_reparse(protocol)
        or is_link_or_reparse(staging)
    ):
        return 72
    parent_monitor = _open_parent_monitor(parent_pid)
    if parent_monitor is None or not parent_monitor.is_alive():
        return 78
    try:
        envelope = _read_control(control / "input.json", control, "review proxy worker input")
        if (
            envelope.get("artifact_type") != "detector_review_proxy_worker_input"
            or not isinstance(envelope.get("worker_id"), str)
            or not isinstance(envelope.get("request"), dict)
        ):
            return 73
        worker_id = envelope["worker_id"]
        stop = threading.Event()
        heartbeat = threading.Thread(
            target=_heartbeat_loop,
            args=(stop, control, worker_id, parent_pid, parent_monitor),
            name="detector-review-proxy-worker-heartbeat",
            daemon=True,
        )
        heartbeat.start()

        def progress(completed: int, total: int) -> None:
            atomic_write_json(
                control / "progress.json",
                {
                    "schema_version": "1.0",
                    "artifact_type": "detector_review_proxy_worker_progress",
                    "worker_id": worker_id,
                    "completed": completed,
                    "total": total,
                },
                trusted_root=control,
            )

        try:
            output = run_detector_review_proxy(
                envelope["request"],
                staging,
                lambda: _cancel_requested(control, worker_id),
                progress,
            )
            atomic_write_json(
                control / "result.json",
                {
                    "schema_version": "1.0",
                    "artifact_type": "detector_review_proxy_worker_result",
                    "worker_id": worker_id,
                    "runner_output": output,
                },
                trusted_root=control,
            )
            return 0
        except BaseException as exc:
            code, status_code = _error_binding(exc)
            code = _safe_review_proxy_failure_code(code)
            try:
                atomic_write_json(
                    control / "error.json",
                    {
                        "schema_version": "1.0",
                        "artifact_type": "detector_review_proxy_worker_error",
                        "worker_id": worker_id,
                        "code": code,
                        "status_code": status_code,
                        "message": _safe_review_proxy_failure_message(code),
                    },
                    trusted_root=control,
                )
            except Exception:
                return 75
            return 74
        finally:
            stop.set()
            heartbeat.join(timeout=2.0)
    finally:
        parent_monitor.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-dir", type=Path, required=True)
    parser.add_argument("--staging-dir", type=Path, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args()
    return run_worker(args.control_dir, args.staging_dir, args.parent_pid)


if __name__ == "__main__":
    raise SystemExit(main())
