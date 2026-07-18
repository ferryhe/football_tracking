from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from football_tracking.detector_probe import (
    DetectorProbeCoordinator,
    _WorkerProcess,
)


def _write(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    state = Path(sys.argv[1])
    worker_script = Path(sys.argv[2])
    staging = state / "staging"
    control = staging / ".worker-control"
    control.mkdir(parents=True)
    worker_id = "worker-parent-crash-test"
    _write(
        control / "input.json",
        {
            "schema_version": "1.0",
            "artifact_type": "detector_probe_worker_input",
            "worker_id": worker_id,
            "request": {},
            "profiles": [],
        },
    )
    parent_watch_read_fd = None
    parent_watch_write_fd = None
    environment = dict(os.environ)
    options: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": environment,
    }
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        parent_watch_read_fd, parent_watch_write_fd = os.pipe()
        os.set_inheritable(parent_watch_read_fd, True)
        os.set_inheritable(parent_watch_write_fd, False)
        environment["FOOTBALL_TRACKING_PARENT_WATCH_FD"] = str(
            parent_watch_read_fd
        )
        options["start_new_session"] = True
        options["pass_fds"] = (parent_watch_read_fd,)
    descendant_path = state / "descendant.pid"
    process = subprocess.Popen(
        [
            sys.executable,
            str(worker_script),
            "hang-with-descendant",
            str(control),
            str(staging),
            str(descendant_path),
        ],
        **options,
    )
    if parent_watch_read_fd is not None:
        os.close(parent_watch_read_fd)
    child = _WorkerProcess(
        process,
        staging=staging,
        control=control,
        worker_id=worker_id,
    )
    child.parent_watch_write_fd = parent_watch_write_fd
    DetectorProbeCoordinator._attach_worker_containment(child)
    child.containment_attached = True
    _write(
        control / "start.json",
        {
            "schema_version": "1.0",
            "artifact_type": "detector_probe_worker_start",
            "worker_id": worker_id,
            "launcher_pid": process.pid,
            "parent_pid": os.getpid(),
        },
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        heartbeat_path = control / "heartbeat.json"
        if descendant_path.exists() and heartbeat_path.exists():
            try:
                heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
                descendant_pid = int(descendant_path.read_text(encoding="ascii"))
            except (OSError, ValueError, json.JSONDecodeError):
                time.sleep(0.02)
                continue
            _write(
                state / "ready.json",
                {
                    "harness_pid": os.getpid(),
                    "launcher_pid": process.pid,
                    "worker_pid": heartbeat["worker_pid"],
                    "descendant_pid": descendant_pid,
                },
            )
            while True:
                time.sleep(1)
        time.sleep(0.02)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
