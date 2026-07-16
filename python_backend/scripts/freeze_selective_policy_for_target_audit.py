from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from football_tracking.selective_policy import freeze_selective_policy_for_target_audit

    parser = argparse.ArgumentParser(description="Freeze target decisions before opening exact-target audit labels")
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--dataset-manifest", required=True, type=Path)
    parser.add_argument("--target-contract", required=True, type=Path)
    parser.add_argument("--model-manifest", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        application = freeze_selective_policy_for_target_audit(
            args.policy,
            args.predictions,
            args.dataset_manifest,
            args.target_contract,
            args.model_manifest,
            args.output_dir,
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "status": application["status"],
                "policy_version": application["policy_version"],
                "candidate_count": application["summary"]["candidate_count"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
