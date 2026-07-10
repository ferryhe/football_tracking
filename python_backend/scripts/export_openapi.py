from __future__ import annotations

import argparse
import difflib
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

API_PREFIX = "/api/v1"
REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON_BACKEND_ROOT = REPO_ROOT / "python_backend"
DEFAULT_OUTPUT_PATH = REPO_ROOT / "lib" / "api-spec" / "openapi.yaml"

sys.path.insert(0, str(PYTHON_BACKEND_ROOT))


def build_openapi_document() -> dict[str, Any]:
    from football_tracking.api.app import create_app

    app = create_app(PYTHON_BACKEND_ROOT, initialize_service=False)
    document = deepcopy(app.openapi())

    document["info"] = {
        "title": "Api",
        "version": document.get("info", {}).get("version", "0.1.0"),
    }
    document["servers"] = [{"url": "/api"}]
    document["paths"] = _frontend_paths(document.get("paths", {}))
    document["paths"]["/healthz"] = _healthz_path()
    document.setdefault("components", {}).setdefault("schemas", {})["HealthStatus"] = {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
        },
        "required": ["status"],
    }

    return document


def dump_openapi(document: dict[str, Any]) -> str:
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=False)


def write_openapi(output_path: Path = DEFAULT_OUTPUT_PATH) -> None:
    output_path.write_text(dump_openapi(build_openapi_document()), encoding="utf-8")


def check_openapi(output_path: Path = DEFAULT_OUTPUT_PATH) -> int:
    expected = dump_openapi(build_openapi_document())
    actual = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
    if actual == expected:
        return 0

    diff = difflib.unified_diff(
        actual.splitlines(),
        expected.splitlines(),
        fromfile=str(output_path),
        tofile="generated openapi",
        lineterm="",
    )
    sys.stderr.write("\n".join(diff) + "\n")
    return 1


def _frontend_paths(paths: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for path, path_item in sorted(paths.items()):
        if not _is_api_prefix_path(path):
            continue
        path_item = deepcopy(path_item)
        _normalize_operation_ids(path_item, path)
        frontend_path = path.removeprefix(API_PREFIX) or "/"
        normalized[frontend_path] = path_item
    return normalized


def _is_api_prefix_path(path: str) -> bool:
    return path == API_PREFIX or path.startswith(f"{API_PREFIX}/")


def _normalize_operation_ids(path_item: dict[str, Any], original_path: str) -> None:
    for method, operation in path_item.items():
        if not isinstance(operation, dict):
            continue
        operation_id = operation.get("operationId")
        if not isinstance(operation_id, str):
            continue
        suffix = _fastapi_operation_suffix(original_path, method)
        if operation_id.endswith(suffix):
            operation["operationId"] = operation_id[: -len(suffix)]


def _fastapi_operation_suffix(path: str, method: str) -> str:
    route_token = f"{path}_{method.lower()}".strip("/")
    return f"_{re.sub(r'[^0-9A-Za-z_]', '_', route_token)}"


def _healthz_path() -> dict[str, Any]:
    return {
        "get": {
            "tags": ["health"],
            "summary": "Health check",
            "operationId": "healthCheck",
            "responses": {
                "200": {
                    "description": "Healthy",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/HealthStatus"},
                        },
                    },
                },
            },
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export the FastAPI OpenAPI contract for the frontend proxy.")
    parser.add_argument("--check", action="store_true", help="Exit non-zero if openapi.yaml is not up to date.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH, help="Path to write or check.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.check:
        return check_openapi(args.output)

    write_openapi(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
