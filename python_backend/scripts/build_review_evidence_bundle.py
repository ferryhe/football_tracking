from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from football_tracking.review_evidence_bundle import build_review_evidence_bundle

    parser = argparse.ArgumentParser(description="Build and validate a self-contained review-evidence bundle.")
    parser.add_argument("source_dir", type=Path, help="Prepared source directory containing the three packages")
    parser.add_argument("output_dir", type=Path, help="New output directory to publish atomically")
    parser.add_argument("--draft-manifest", type=Path, default=None)
    args = parser.parse_args()
    result = build_review_evidence_bundle(
        args.source_dir,
        args.output_dir,
        draft_manifest_path=args.draft_manifest,
    )
    print(
        json.dumps(
            {
                "bundle_id": result.manifest["bundle_id"],
                "bundle_sha256": result.bundle_sha256,
                "queue_sha256": result.queue_sha256,
                "total_size_bytes": result.total_size_bytes,
                "path": str(result.root),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
