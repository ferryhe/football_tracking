from __future__ import annotations

# ruff: noqa: E402
import argparse
import json
import sys
from pathlib import Path

PYTHON_BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PYTHON_BACKEND_ROOT))

from football_tracking.broadcast_acceptance import DEFAULT_SEGMENT_FRAMES, validate_broadcast_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a ready broadcast run and publish its acceptance report.")
    parser.add_argument("--run-dir", type=Path, required=True, help="Ready broadcast run directory.")
    parser.add_argument("--ffmpeg", help="Explicit ffmpeg executable path or command name.")
    parser.add_argument(
        "--segment-frames",
        type=int,
        default=DEFAULT_SEGMENT_FRAMES,
        help="Frames per resumable validation segment.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse completed segments only when every checkpoint identity binding still matches.",
    )
    args = parser.parse_args(argv)

    try:
        result = validate_broadcast_run(
            args.run_dir,
            ffmpeg_executable=args.ffmpeg,
            segment_frames=args.segment_frames,
            resume=args.resume,
            progress_callback=lambda update: print(
                json.dumps(update, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                file=sys.stderr,
                flush=True,
            ),
        )
    except KeyboardInterrupt:
        result = {"status": "unavailable", "error": "interrupted"}
        exit_code = 130
    except Exception as exc:
        result = {
            "status": "fail",
            "error": " ".join(str(exc).split())[:500] or exc.__class__.__name__,
        }
        exit_code = 1
    else:
        exit_code = 0 if result.get("status") == "pass" else 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
