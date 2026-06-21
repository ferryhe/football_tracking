from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PYTHON_BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PYTHON_BACKEND_ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run offline AI visual review for review packets.")
    parser.add_argument("output_dir", type=Path, help="Directory containing review_packets.json.")
    parser.add_argument("--model", default=None, help="OpenAI model override.")
    parser.add_argument("--max-packets", type=int, default=None, help="Maximum packets to review.")
    parser.add_argument("--only-label", action="append", default=None, help="Review only packets with this decision label.")
    parser.add_argument("--dry-run", action="store_true", help="Write human-review decisions without calling the model.")
    parser.add_argument("--skip-accepted-copy", action="store_true", help="Do not copy accepted highlight clips.")
    parser.add_argument(
        "--accepted-dir-name",
        default="highlights_ai_accepted",
        help="Output directory name for accepted highlights under output_dir.",
    )
    args = parser.parse_args(argv)

    if not args.output_dir.exists() or not args.output_dir.is_dir():
        parser.error(f"output_dir does not exist or is not a directory: {args.output_dir}")
    if args.max_packets is not None and args.max_packets < 1:
        parser.error("--max-packets must be at least 1")
    if _is_bad_dir_name(args.accepted_dir_name):
        parser.error("--accepted-dir-name must be a directory name, not a path")

    from football_tracking.accepted_highlights import (
        compact_accepted_highlights_summary,
        write_accepted_highlights_report,
    )
    from football_tracking.ai_visual_review import write_ai_visual_review_report
    from football_tracking.metrics import build_metrics_report

    ai_report = write_ai_visual_review_report(
        args.output_dir,
        model=args.model,
        max_packets=args.max_packets,
        only_labels=args.only_label,
        dry_run=args.dry_run,
    )
    summary: dict[str, object] = {"ai_visual_review": ai_report["summary"]}
    accepted_report: dict[str, object] | None = None

    if not args.skip_accepted_copy and not args.dry_run:
        accepted_report = write_accepted_highlights_report(
            args.output_dir,
            accepted_dir_name=args.accepted_dir_name,
        )
        summary["accepted_highlights"] = accepted_report["summary"]

    metrics_path = args.output_dir / "metrics_report.json"
    if metrics_path.exists():
        metrics_report = build_metrics_report(args.output_dir)
        if accepted_report is not None:
            compact_accepted = compact_accepted_highlights_summary(accepted_report)
            if compact_accepted is not None:
                metrics_report["accepted_highlights"] = compact_accepted
        metrics_path.write_text(
            json.dumps(metrics_report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _is_bad_dir_name(value: str) -> bool:
    if not value or not value.strip():
        return True
    stripped = value.strip()
    return stripped in {".", ".."} or Path(stripped).is_absolute() or "/" in stripped or "\\" in stripped


if __name__ == "__main__":
    raise SystemExit(main())
