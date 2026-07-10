from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from football_tracking.candidate_classifier import classify_cli_main

    return classify_cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
