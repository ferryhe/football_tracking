from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PYTHON_BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PYTHON_BACKEND_ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build review packets from a tracking output directory.")
    parser.add_argument("output_dir", type=Path, help="Directory containing ball_track, audit, and event artifacts.")
    parser.add_argument("--input-video", type=Path, default=None, help="Original wide video used for contact sheets.")
    parser.add_argument("--follow-cam-video", type=Path, default=None, help="Follow-cam video used for packet clips.")
    parser.add_argument("--max-packets", type=int, default=12, help="Maximum number of packets to generate.")
    parser.add_argument("--no-media", action="store_true", help="Write JSON manifests only.")
    args = parser.parse_args(argv)

    if not args.output_dir.exists() or not args.output_dir.is_dir():
        parser.error(f"output_dir does not exist or is not a directory: {args.output_dir}")
    if args.max_packets < 1:
        parser.error("--max-packets must be at least 1")
    if args.input_video is not None and not args.input_video.exists():
        parser.error(f"--input-video does not exist: {args.input_video}")
    if args.follow_cam_video is not None and not args.follow_cam_video.exists():
        parser.error(f"--follow-cam-video does not exist: {args.follow_cam_video}")

    from football_tracking.metrics import build_metrics_report
    from football_tracking.review_packets import write_review_packet_report

    report = write_review_packet_report(
        args.output_dir,
        input_video=args.input_video,
        follow_cam_video=args.follow_cam_video,
        max_packets=args.max_packets,
        include_media=not args.no_media,
    )
    metrics_path = args.output_dir / "metrics_report.json"
    if metrics_path.exists():
        metrics_path.write_text(
            json.dumps(build_metrics_report(args.output_dir), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
