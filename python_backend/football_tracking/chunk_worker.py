from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from football_tracking.chunk_runner import run_chunk


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one raw-only temporal tracking chunk.")
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to the chunk_config.yaml file.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return run_chunk(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
