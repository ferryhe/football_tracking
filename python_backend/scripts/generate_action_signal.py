from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from football_tracking.action_signal import (
    ACTION_SIGNAL_REPORT_NAME,
    ACTION_SIGNAL_SUCCESS_STATUSES,
    ActionSignalSettings,
    generate_action_track,
    load_action_calibration,
)


def build_parser() -> argparse.ArgumentParser:
    defaults = ActionSignalSettings()
    parser = argparse.ArgumentParser(description="Generate a calibrated foreground-action track.")
    parser.add_argument("--input-video", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start-frame", type=int, default=0)
    parser.add_argument("--max-frames", type=int)
    parser.add_argument("--process-width", type=int, default=defaults.process_width)
    parser.add_argument("--smoothing", type=float, default=defaults.smoothing)
    parser.add_argument("--min-area", type=int, default=defaults.min_component_area)
    parser.add_argument("--max-area", type=int, default=defaults.max_component_area)
    parser.add_argument("--history", type=int, default=defaults.background_history)
    parser.add_argument("--var-threshold", type=float, default=defaults.variance_threshold)
    parser.add_argument("--warmup-frames", type=int, default=defaults.warmup_frames)
    parser.add_argument("--hold-frames", type=int, default=defaults.hold_frames)
    parser.add_argument("--hold-confidence-decay", type=float, default=defaults.hold_confidence_decay)
    parser.add_argument("--progress-interval-frames", type=int, default=250)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = ActionSignalSettings(
            process_width=args.process_width,
            smoothing=args.smoothing,
            min_component_area=args.min_area,
            max_component_area=args.max_area,
            background_history=args.history,
            variance_threshold=args.var_threshold,
            warmup_frames=args.warmup_frames,
            hold_frames=args.hold_frames,
            hold_confidence_decay=args.hold_confidence_decay,
        )
        calibration = load_action_calibration(args.calibration)
        report = generate_action_track(
            input_video=args.input_video,
            calibration=calibration,
            output_dir=args.output_dir,
            settings=settings,
            start_frame=args.start_frame,
            max_frames=args.max_frames,
            calibration_source=args.calibration,
            progress_callback=_print_progress,
            progress_interval_frames=args.progress_interval_frames,
        )
    except Exception as exc:
        print(
            json.dumps(
                {"event": "failed", "status": "failed", "error": str(exc)},
                ensure_ascii=False,
                allow_nan=False,
            ),
            file=sys.stderr,
        )
        return 1
    if report.get("status") not in ACTION_SIGNAL_SUCCESS_STATUSES:
        print(
            json.dumps(
                {
                    "event": "failed",
                    "status": report.get("status", "failed"),
                    "termination_reason": report.get("termination_reason", "unknown"),
                },
                ensure_ascii=False,
                allow_nan=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(f"report={(args.output_dir.resolve() / ACTION_SIGNAL_REPORT_NAME)}", flush=True)
    return 0


def _print_progress(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
