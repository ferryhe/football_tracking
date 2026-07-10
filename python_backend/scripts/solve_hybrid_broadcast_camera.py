from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import NoReturn


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(f"argument error: {message}")


def main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from football_tracking.hybrid_broadcast_camera import (
        HybridBroadcastCameraError,
        HybridCameraConfig,
        solve_hybrid_broadcast_camera,
    )

    parser = _JsonArgumentParser(description="Solve an evidence-bound hybrid broadcast camera path.")
    parser.add_argument("--source-video", required=True, type=Path)
    parser.add_argument("--ball-track", required=True, type=Path)
    parser.add_argument("--trajectory-report", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--target-width", type=int, default=1920)
    parser.add_argument("--target-height", type=int, default=1080)
    parser.add_argument("--analysis-max-dimension", type=int, default=320)
    parser.add_argument("--max-features", type=int, default=240)
    parser.add_argument("--minimum-ball-confidence", type=float, default=0.15)
    parser.add_argument("--motion-confidence-threshold", type=float, default=0.35)
    parser.add_argument("--unknown-hold-frames", type=int, default=24)
    try:
        args = parser.parse_args(argv)
        report = solve_hybrid_broadcast_camera(
            args.source_video,
            args.ball_track,
            args.trajectory_report,
            args.output_dir,
            config=HybridCameraConfig(
                target_width=args.target_width,
                target_height=args.target_height,
                analysis_max_dimension=args.analysis_max_dimension,
                max_features=args.max_features,
                minimum_ball_confidence=args.minimum_ball_confidence,
                motion_confidence_threshold=args.motion_confidence_threshold,
                unknown_hold_frames=args.unknown_hold_frames,
            ),
        )
    except KeyboardInterrupt:
        print(
            json.dumps({"ok": False, "error": {"type": "KeyboardInterrupt", "message": "cancelled"}}),
            file=sys.stderr,
        )
        return 130
    except (HybridBroadcastCameraError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "status": report["status"],
                "row_count": report["summary"]["row_count"],
                "cut_count": report["summary"]["cut_count"],
                "target_coverage": report["summary"]["target_coverage"],
                "output_dir": str(args.output_dir.resolve()),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
