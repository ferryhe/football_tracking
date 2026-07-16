from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _load(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _publish(output_dir: Path, name: str, payload: dict[str, Any]) -> None:
    output = output_dir.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    published_path = output / name
    try:
        output.mkdir()
        with published_path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise ValueError(f"output directory already exists: {output}") from exc
    except BaseException:
        try:
            published_path.unlink()
        except OSError:
            pass
        try:
            output.rmdir()
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from football_tracking.target_finite_population import (
        build_target_audit_labels_from_annotation_package,
        build_target_audit_plan,
        build_target_qualified_application,
        evaluate_target_audit,
    )

    parser = argparse.ArgumentParser(
        description="Freeze, evaluate, or activate an exact-target finite-population audit"
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)

    freeze = subparsers.add_parser("freeze")
    freeze.add_argument("--frozen-application", required=True, type=Path)
    freeze.add_argument("--target-run-id", required=True)
    freeze.add_argument("--confirmed-config-sha256", required=True)
    freeze.add_argument(
        "--commitment-root",
        required=True,
        type=Path,
        help="Canonical server-owned outputs/target_prelabel_commitments registry",
    )
    freeze.add_argument("--output-dir", required=True, type=Path)

    bind_labels = subparsers.add_parser("bind-labels")
    bind_labels.add_argument("--plan", required=True, type=Path)
    bind_labels.add_argument("--package-root", required=True, type=Path)
    bind_labels.add_argument("--contract", required=True, type=Path)
    bind_labels.add_argument("--ledger", required=True, type=Path)
    bind_labels.add_argument("--dataset-manifest", required=True, type=Path)
    bind_labels.add_argument("--annotation-resolution", required=True, type=Path)
    bind_labels.add_argument("--previous-ledger", type=Path)
    bind_labels.add_argument(
        "--commitment-record",
        required=True,
        type=Path,
        help="Record from the canonical server-owned pre-label commitment registry",
    )
    bind_labels.add_argument("--output-dir", required=True, type=Path)

    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--plan", required=True, type=Path)
    evaluate.add_argument("--labels", required=True, type=Path)
    evaluate.add_argument(
        "--commitment-record",
        required=True,
        type=Path,
        help="Record from the canonical server-owned pre-label commitment registry",
    )
    evaluate.add_argument("--output-dir", required=True, type=Path)

    activate = subparsers.add_parser("activate")
    activate.add_argument("--frozen-application", required=True, type=Path)
    activate.add_argument("--plan", required=True, type=Path)
    activate.add_argument("--labels", required=True, type=Path)
    activate.add_argument("--qualification", required=True, type=Path)
    activate.add_argument(
        "--commitment-record",
        required=True,
        type=Path,
        help="Record from the canonical server-owned pre-label commitment registry",
    )
    activate.add_argument("--output-dir", required=True, type=Path)

    args = parser.parse_args(argv)
    try:
        if args.operation == "freeze":
            application = _load(args.frozen_application, "frozen application")
            if (
                application.get("artifact_type") != "target_finite_population_application"
                or application.get("qualification_scope") != "target_finite_population"
                or application.get("status") != "frozen_before_labels"
            ):
                raise ValueError("frozen application envelope is invalid")
            plan = build_target_audit_plan(
                application,
                target_run_id=args.target_run_id,
                confirmed_config_sha256=args.confirmed_config_sha256,
                commitment_root=args.commitment_root,
            )
            _publish(args.output_dir, "target_finite_population_audit_plan.v1.json", plan)
            result = {"status": plan["status"], "plan_sha256": plan["plan_sha256"]}
        elif args.operation == "bind-labels":
            labels = build_target_audit_labels_from_annotation_package(
                _load(args.plan, "target audit plan"),
                package_root=args.package_root,
                contract_path=args.contract,
                ledger_path=args.ledger,
                dataset_manifest_path=args.dataset_manifest,
                annotation_resolution_path=args.annotation_resolution,
                commitment_path=args.commitment_record,
                previous_ledger_path=args.previous_ledger,
            )
            _publish(
                args.output_dir,
                "target_finite_population_audit_labels.v1.json",
                labels,
            )
            result = {
                "status": "bound",
                "plan_sha256": labels["plan_sha256"],
            }
        elif args.operation == "evaluate":
            qualification = evaluate_target_audit(
                _load(args.plan, "target audit plan"),
                args.labels,
                commitment_path=args.commitment_record,
            )
            _publish(
                args.output_dir,
                "target_finite_population_qualification.v1.json",
                qualification,
            )
            result = {
                "status": qualification["status"],
                "qualification_sha256": qualification["qualification_sha256"],
            }
        else:
            application = build_target_qualified_application(
                _load(args.frozen_application, "frozen application"),
                _load(args.plan, "target audit plan"),
                args.labels,
                _load(args.qualification, "target qualification"),
                commitment_path=args.commitment_record,
            )
            _publish(
                args.output_dir,
                "target_finite_population_qualified_application.v1.json",
                application,
            )
            result = {
                "status": application["status"],
                "application_content_sha256": application["application_content_sha256"],
            }
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
