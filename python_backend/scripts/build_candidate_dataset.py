from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    python_backend_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(python_backend_root))
    from football_tracking.candidate_dataset import main as dataset_main

    return dataset_main()


if __name__ == "__main__":
    raise SystemExit(main())
