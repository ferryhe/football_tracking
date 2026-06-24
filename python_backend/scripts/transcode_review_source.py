from __future__ import annotations

# ruff: noqa: E402
import argparse
import json
import sys
from pathlib import Path

PYTHON_BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PYTHON_BACKEND_ROOT))

from football_tracking.media_integrity import transcode_review_source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a sequential mp4v review source for AI visual evidence.")
    parser.add_argument("--input-video", type=Path, required=True, help="Original source video to decode sequentially.")
    parser.add_argument("--output-video", type=Path, required=True, help="Review-friendly mp4v output path.")
    args = parser.parse_args(argv)

    result = transcode_review_source(args.input_video, args.output_video)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
