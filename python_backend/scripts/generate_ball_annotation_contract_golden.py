from __future__ import annotations

import json
import sys
from pathlib import Path

PYTHON_BACKEND_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = PYTHON_BACKEND_ROOT / "tests"
REPOSITORY_ROOT = PYTHON_BACKEND_ROOT.parent
GOLDEN_PATH = REPOSITORY_ROOT / "test_fixtures" / "contracts" / "ball_annotation_api_golden.v1.json"


def main() -> None:
    sys.path.insert(0, str(PYTHON_BACKEND_ROOT))
    sys.path.insert(0, str(TESTS_ROOT))
    from test_ball_annotation_schema_contract import (  # noqa: PLC0415
        _build_ball_annotation_contract_examples,
    )

    first = _build_ball_annotation_contract_examples()
    second = _build_ball_annotation_contract_examples()
    if first != second:
        raise RuntimeError("ball annotation contract generation is nondeterministic")
    content = (
        json.dumps(
            first,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
    GOLDEN_PATH.write_text(content, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
