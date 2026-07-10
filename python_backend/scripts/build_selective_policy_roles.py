from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from football_tracking.selective_policy import build_roles_cli_main

    return build_roles_cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
