from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from football_tracking.config import load_config
from football_tracking.pipeline import BallTrackingPipeline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a benchmarked tracking pipeline job.")
    parser.add_argument("--config", type=Path, required=True, help="Path to the source YAML config.")
    parser.add_argument("--output-dir", type=Path, required=True, help="Output directory for this benchmark run.")
    parser.add_argument("--batch-size", type=int, default=None, help="Override SAHI slice batch size.")
    parser.add_argument("--start-frame", type=int, default=None, help="Optional runtime start_frame override.")
    parser.add_argument("--max-frames", type=int, default=None, help="Optional runtime max_frames override.")
    parser.add_argument(
        "--summary-name",
        default="benchmark_summary.json",
        help="Filename for the benchmark summary written into the output directory.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    config.output_dir = args.output_dir.resolve()

    if args.batch_size is not None:
        config.sahi.batch_size = max(1, int(args.batch_size))
    if args.start_frame is not None:
        config.runtime.start_frame = max(0, int(args.start_frame))
    if args.max_frames is not None:
        config.runtime.max_frames = max(1, int(args.max_frames))

    started_at = datetime.now(timezone.utc)
    started_perf = time.perf_counter()

    BallTrackingPipeline(config).run()

    finished_at = datetime.now(timezone.utc)
    elapsed_seconds = time.perf_counter() - started_perf
    summary = {
        "config_path": str(args.config.resolve()),
        "output_dir": str(config.output_dir),
        "input_video": str(config.input_video),
        "sahi_batch_size": config.sahi.batch_size,
        "started_at_utc": started_at.isoformat(),
        "finished_at_utc": finished_at.isoformat(),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "elapsed_minutes": round(elapsed_seconds / 60.0, 3),
    }

    config.output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = config.output_dir / args.summary_name
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
