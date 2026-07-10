from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from football_tracking.candidate_annotations import resolve_candidate_annotations


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(description="Resolve candidate classification votes into a derived V2 contract.")
    parser.add_argument("--contract", type=Path, required=True, help="Candidate-populated tracking_contract.v2.json.")
    parser.add_argument("--ledger", type=Path, required=True, help="Versioned JSONL vote ledger.")
    parser.add_argument(
        "--dataset-manifest",
        type=Path,
        help="Hash-verified candidate dataset manifest required for every visual vote; optional only for an empty ledger.",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--min-confidence", type=float, default=0.8)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        report = resolve_candidate_annotations(
            args.contract,
            args.ledger,
            args.output_dir,
            min_confidence=args.min_confidence,
            dataset_manifest_path=args.dataset_manifest,
        )
    except Exception as exc:
        print(
            json.dumps(
                {"event": "annotation_resolution_failed", "status": "failed", "error": str(exc)},
                ensure_ascii=False,
                allow_nan=False,
            ),
            file=sys.stderr,
        )
        return 1

    summary = report["summary"]
    print(
        json.dumps(
            {
                "event": "annotation_resolution_complete",
                "status": "complete",
                "candidate_count": summary["candidate_count"],
                "confirmed_count": summary["confirmed_count"],
                "adjudication_count": summary["adjudication_count"],
                "output_dir": str(args.output_dir),
            },
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
