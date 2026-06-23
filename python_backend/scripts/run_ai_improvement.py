from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PYTHON_BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PYTHON_BACKEND_ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build an AI improvement report from tracking run artifacts.")
    parser.add_argument("output_dir", type=Path, help="Directory containing tracking review artifacts.")
    parser.add_argument("--model", default=None, help="OpenAI model override.")
    parser.add_argument("--dry-run", action="store_true", help="Write deterministic suggestions without calling the model.")
    parser.add_argument("--max-items", type=int, default=20, help="Maximum items per artifact list to send in context.")
    parser.add_argument(
        "--candidate-intent",
        choices=("review_only", "suggest_candidates", "prepare_approved_candidates"),
        default=None,
        help="AI candidate intent, distinct from --dry-run.",
    )
    args = parser.parse_args(argv)

    if not args.output_dir.exists() or not args.output_dir.is_dir():
        parser.error(f"output_dir does not exist or is not a directory: {args.output_dir}")
    from football_tracking.ai_improvement import MAX_CONTEXT_ITEMS, compact_ai_improvement_summary, write_ai_improvement_report
    from football_tracking.metrics import build_metrics_report

    if args.max_items < 1:
        parser.error("--max-items must be at least 1")
    if args.max_items > MAX_CONTEXT_ITEMS:
        parser.error(f"--max-items must be at most {MAX_CONTEXT_ITEMS}")

    report = write_ai_improvement_report(
        args.output_dir,
        model=args.model,
        dry_run=args.dry_run,
        max_items=args.max_items,
        candidate_intent=args.candidate_intent,
    )

    metrics_path = args.output_dir / "metrics_report.json"
    if metrics_path.exists():
        metrics_path.write_text(
            json.dumps(build_metrics_report(args.output_dir), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    print(json.dumps({"ai_improvement": compact_ai_improvement_summary(report) or {}}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
