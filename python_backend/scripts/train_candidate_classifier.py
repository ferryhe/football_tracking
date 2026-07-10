from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from football_tracking.candidate_classifier import train_cli_main

    return train_cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
