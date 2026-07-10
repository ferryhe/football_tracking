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
    from football_tracking.global_ball_trajectory import (
        GlobalBallTrajectoryError,
        TrajectoryConfig,
        solve_global_ball_trajectory,
    )

    parser = _JsonArgumentParser(description="Solve an evidence-bound offline global ball trajectory.")
    parser.add_argument("--source-video", required=True, type=Path)
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--pitch-report", type=Path)
    parser.add_argument("--player-tracks", type=Path)
    parser.add_argument("--max-interpolation-gap", type=int, default=6)
    parser.add_argument("--max-transition-gap", type=int, default=12)
    parser.add_argument("--candidate-cap-per-frame", type=int, default=24)
    parser.add_argument("--beam-width", type=int, default=64)
    try:
        args = parser.parse_args(argv)
        report = solve_global_ball_trajectory(
            args.source_video,
            args.contract,
            args.predictions,
            args.output_dir,
            pitch_report_path=args.pitch_report,
            player_tracks_path=args.player_tracks,
            config=TrajectoryConfig(
                max_interpolation_gap=args.max_interpolation_gap,
                max_transition_gap=args.max_transition_gap,
                candidate_cap_per_frame=args.candidate_cap_per_frame,
                beam_width=args.beam_width,
            ),
        )
    except KeyboardInterrupt:
        print(
            json.dumps({"ok": False, "error": {"type": "KeyboardInterrupt", "message": "cancelled"}}),
            file=sys.stderr,
        )
        return 130
    except (GlobalBallTrajectoryError, OSError, ValueError) as exc:
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
                "selected_candidate_count": report["summary"]["selected_candidate_count"],
                "output_dir": str(args.output_dir.resolve()),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
