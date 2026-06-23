from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PYTHON_BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PYTHON_BACKEND_ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the AI improvement quality gate against tracking artifacts.")
    parser.add_argument("--output-dir", required=True, type=Path, help="Directory containing tracking run artifacts.")
    parser.add_argument("--report-name", default="ai_improvement_quality_gate.json", help="Quality gate JSON file name.")
    parser.add_argument(
        "--mode",
        choices=("dry-run", "artifact-only", "real"),
        default="artifact-only",
        help="Gate strictness mode.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Alias for --mode dry-run.")
    parser.add_argument("--approved-actions-path", type=Path, default=None, help="Explicit approved actions JSON path.")
    parser.add_argument(
        "--approved-actions",
        default=None,
        help="Explicit approved actions JSON payload. Mutually exclusive with --approved-actions-path.",
    )
    parser.add_argument("--candidate-output-dir", type=Path, default=None, help="Candidate output directory for comparison.")
    parser.add_argument("--pre-review-stage", default="before_review", help="Hash snapshot stage before review.")
    parser.add_argument("--post-review-stage", default="after_ai_improvement", help="Hash snapshot stage after AI improvement.")
    parser.add_argument(
        "--write-hash-snapshot",
        metavar="STAGE_NAME",
        default=None,
        help="Append a track hash snapshot for STAGE_NAME instead of running the full gate.",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        args.mode = "dry-run"
    if args.approved_actions_path is not None and args.approved_actions is not None:
        parser.error("--approved-actions and --approved-actions-path are mutually exclusive.")

    approved_actions_payload = None
    if args.approved_actions is not None:
        try:
            approved_actions_payload = json.loads(args.approved_actions)
        except json.JSONDecodeError as exc:
            parser.error(f"--approved-actions must be valid JSON: {exc}")
        if not isinstance(approved_actions_payload, dict):
            parser.error("--approved-actions must be a JSON object.")

    from football_tracking.ai_improvement_quality_gate import (
        write_ai_improvement_quality_gate,
        write_track_hash_snapshot,
    )

    if args.write_hash_snapshot:
        snapshot = write_track_hash_snapshot(args.output_dir, args.write_hash_snapshot)
        print(json.dumps({"track_hash_snapshot": snapshot}, ensure_ascii=False, indent=2))
        return 0

    report = write_ai_improvement_quality_gate(
        args.output_dir,
        report_name=args.report_name,
        mode=args.mode,
        approved_actions_path=args.approved_actions_path,
        approved_actions_payload=approved_actions_payload,
        candidate_output_dir=args.candidate_output_dir,
        pre_review_stage=args.pre_review_stage,
        post_review_stage=args.post_review_stage,
    )
    print(json.dumps({"ai_improvement_quality_gate": report["summary"]}, ensure_ascii=False, indent=2))
    return 1 if report["summary"].get("status") == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
