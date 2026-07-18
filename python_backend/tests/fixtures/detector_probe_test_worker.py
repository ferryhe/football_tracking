from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from football_tracking.detector_probe_worker import _start_parent_watchdog


def _write(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _error(control: Path, worker_id: str, code: str) -> int:
    _write(
        control / "error.json",
        {
            "schema_version": "1.0",
            "artifact_type": "detector_probe_worker_error",
            "worker_id": worker_id,
            "code": code,
            "status_code": 409,
        },
    )
    return 74


def _heartbeat(
    stop: threading.Event, control: Path, worker_id: str, parent_pid: int
) -> None:
    sequence = 0
    while not stop.wait(0.05):
        sequence += 1
        _write(
            control / "heartbeat.json",
            {
                "schema_version": "1.0",
                "artifact_type": "detector_probe_worker_heartbeat",
                "worker_id": worker_id,
                "worker_pid": os.getpid(),
                "parent_pid": parent_pid,
                "sequence": sequence,
            },
        )


def _successful_output(
    request: dict[str, Any], profiles: list[dict[str, Any]], staging: Path
) -> dict[str, Any]:
    fixture = staging / "test-worker-fixture.jpg"
    jpeg = fixture.read_bytes()
    fixture.unlink()
    frames = []
    requested_mode = request["_requested_decode_mode"]
    effective_mode = {
        "sequential": "sequential",
        "preroll": "preroll_verified",
        "direct": "direct_verified",
    }[requested_mode]
    total = len(request["frame_indices"])
    for completed, frame_index in enumerate(request["frame_indices"], start=1):
        frame_path = staging / "frames" / f"{frame_index:09d}.jpg"
        frame_path.parent.mkdir(parents=True, exist_ok=True)
        frame_path.write_bytes(jpeg)
        profile_results = []
        for profile in profiles:
            overlay_path = (
                staging
                / "overlays"
                / f"{frame_index:09d}-{profile['profile_id']}.jpg"
            )
            overlay_path.parent.mkdir(parents=True, exist_ok=True)
            overlay_path.write_bytes(jpeg)
            candidate = {
                "frame_index": frame_index,
                "bbox_source_px": [10.0, 20.0, 18.0, 28.0],
                "confidence": 0.8,
                "class_name": "ball",
                "checkpoint_class_name": "sports ball",
                "source": f"yolo_{profile['mode']}",
                "coordinate_reason": (
                    "direct_source_coordinates"
                    if profile["mode"] == "direct"
                    else "sahi_tile_offset_applied"
                ),
                "merge_reason": "retained_top_k",
            }
            profile_results.append(
                {
                    "profile_id": profile["profile_id"],
                    "profile_sha256": profile["profile_sha256"],
                    "status": "completed",
                    "latency_ms": 1.25,
                    "candidate_count": 1,
                    "top_k": request["top_k"],
                    "raw_candidates": [candidate],
                    "display_candidate": candidate,
                    "filter_reasons": {},
                    "failure_code": None,
                    "raw_overlay_relative_path": overlay_path.relative_to(
                        staging
                    ).as_posix(),
                }
            )
        frames.append(
            {
                "frame_index": frame_index,
                "source_frame_relative_path": frame_path.relative_to(
                    staging
                ).as_posix(),
                "requested_decode_mode": requested_mode,
                "effective_decode_mode": effective_mode,
                "decoded_frame_position": frame_index,
                "media_integrity": {
                    "path": None,
                    "status": "ok",
                    "width": request["_source_width"],
                    "height": request["_source_height"],
                    "mean_luma": 90.0,
                    "std_luma": 20.0,
                    "texture_tile_ratio": 0.5,
                    "dominant_color_ratio": 0.2,
                    "gray": False,
                    "low_information": False,
                    "likely_corrupt": False,
                    "reasons": [],
                },
                "profile_results": profile_results,
            }
        )
        _write(
            staging / ".worker-control" / "progress.json",
            {
                "schema_version": "1.0",
                "artifact_type": "detector_probe_worker_progress",
                "worker_id": request["_test_worker_id"],
                "completed": completed,
                "total": total,
            },
        )
    return {
        "frames": frames,
        "decode": {
            "width": request["_source_width"],
            "height": request["_source_height"],
            "frame_count": request["_source_frame_count"],
            "fps": 30.0,
            "requested_decode_mode": requested_mode,
            "effective_decode_mode": effective_mode,
            "verified_frame_indices": request["frame_indices"],
            "position_verification": "opencv_next_frame_index_with_0.25_tolerance",
        },
        "execution": {
            "device": request["_execution_environment"]["device"],
            "precision": request["_execution_environment"]["precision"],
        },
    }


def main() -> int:
    if not _start_parent_watchdog():
        return 78
    mode = sys.argv[1]
    control = Path(sys.argv[2])
    staging = Path(sys.argv[3])
    pid_path = Path(sys.argv[4]) if len(sys.argv) > 4 else None
    envelope = _read(control / "input.json")
    worker_id = envelope["worker_id"]
    while not (control / "start.json").exists():
        if (control / "abort.json").exists():
            _write(
                control / "abort-ack.json",
                {
                    "schema_version": "1.0",
                    "artifact_type": "detector_probe_worker_abort_ack",
                    "worker_id": worker_id,
                    "worker_pid": os.getpid(),
                },
            )
            if mode == "abort-hang":
                while True:
                    time.sleep(0.05)
            return 78
        time.sleep(0.01)
    start = _read(control / "start.json")

    if mode == "unexpected-exit":
        return 9
    if mode == "error-envelope-unavailable":
        return 75
    if mode == "disk-exit":
        return 77
    if mode.startswith("structured-"):
        return _error(control, worker_id, mode.removeprefix("structured-"))

    if "descendant" in mode:
        descendant = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if pid_path is not None:
            pid_path.write_text(str(descendant.pid), encoding="ascii")
        if mode == "leader-with-descendant":
            return 9

    stop = threading.Event()
    heartbeat = None
    if mode != "silent-hang":
        heartbeat = threading.Thread(
            target=_heartbeat,
            args=(stop, control, worker_id, start["parent_pid"]),
            daemon=True,
        )
        heartbeat.start()
    try:
        if mode == "success":
            request = envelope["request"]
            request["_test_worker_id"] = worker_id
            output = _successful_output(request, envelope["profiles"], staging)
            _write(
                control / "result.json",
                {
                    "schema_version": "1.0",
                    "artifact_type": "detector_probe_worker_result",
                    "worker_id": worker_id,
                    "runner_output": output,
                },
            )
            return 0
        if mode == "cancel":
            while True:
                if _read(control / "cancel.json")["cancel_requested"]:
                    return _error(control, worker_id, "cancelled")
                time.sleep(0.02)
        while True:
            time.sleep(0.05)
    finally:
        stop.set()
        if heartbeat is not None:
            heartbeat.join(timeout=1)


if __name__ == "__main__":
    raise SystemExit(main())
