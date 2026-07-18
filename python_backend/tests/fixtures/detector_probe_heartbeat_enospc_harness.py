from __future__ import annotations

import errno
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import football_tracking.detector_probe_worker as worker


def _disk_full(*_args, **_kwargs) -> None:
    raise OSError(errno.ENOSPC, "controlled detector heartbeat disk exhaustion")


worker.atomic_write_json = _disk_full
worker._heartbeat_loop(
    threading.Event(),
    Path(sys.argv[1]),
    "worker-heartbeat-enospc",
    os.getppid(),
)
