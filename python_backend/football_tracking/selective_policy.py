from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any

from football_tracking.candidate_classifier import (
    ClassifierError,
    validate_candidate_predictions_package,
)
from football_tracking.tracking_benchmark import build_benchmark_report
from football_tracking.tracking_contracts import (
    CLASSIFICATION_LABELS,
    CONFIRMED_LABEL_ORIGINS,
    TRACKING_CONTRACT_REPORT_NAME,
    build_tracking_contract,
    normalize_tracking_contract_payload,
)

POLICY_SCHEMA_VERSION = "1.0"
SELECTIVE_POLICY_NAME = "selective_policy.v1.json"
SELECTIVE_ACCEPTANCE_REPORT_NAME = "selective_acceptance_report.v1.json"
SELECTIVE_DECISIONS_NAME = "selective_decisions.v1.json"
SELECTIVE_APPLICATION_NAME = "selective_application.v1.json"
SELECTIVE_POLICY_ROLES_NAME = "selective_policy_roles.v1.json"
NOISE_LABELS = tuple(label for label in CLASSIFICATION_LABELS if label not in {"match_ball", "unknown"})
POLICY_ROLE_SEED = "football-tracking-selective-policy-role-seed-v1"
POLICY_VERSION_ALGORITHM = "selective-policy-version-v3"
THRESHOLD_ALGORITHM = "learn_then_test_exact_binomial_holm_independent_component_v2"
AUDIT_ALGORITHM = "clopper_pearson_exact_one_sided_bonferroni_independent_component_v2"
DECISION_ALGORITHM = "application-only-selective-decisions-v2"
ROLE_COMPONENT_ID_ALGORITHM = "immutable-video-sha-component-v2"
ROLE_ASSIGNMENT_ALGORITHM = "sha256_connected_evidence_component_partition_v2"
INFERENTIAL_UNIT = "connected_evidence_component"
INFERENTIAL_UNIT_ALGORITHM = "one_candidate_per_connected_evidence_component_v1"
_FILE_READ_CHUNK_BYTES = 1024 * 1024
_MAX_JSON_ARTIFACT_BYTES = 256 * 1024 * 1024
DECISION_ROW_FIELDS = frozenset(
    {
        "candidate_id",
        "candidate_fingerprint",
        "variant_id",
        "frame_index",
        "accept_score",
        "reject_score",
        "unknown_score",
        "top_label",
        "top_margin",
        "raw_decision",
        "decision",
        "decision_scope",
        "policy_role",
        "forced_abstain_reasons",
        "existing_decision_preserved",
        "applied_to_contract",
    }
)
DECISION_FORCED_REASONS = frozenset(
    {
        "confirmed_conflict",
        "confirmed_unknown",
        "existing_decision",
        "conflicting_existing_decisions",
        "top_unknown",
        "top_margin_below_minimum",
        "accept_reject_conflict_margin",
        "unknown_probability_too_high",
        "evaluation_holdout",
    }
)


class SelectivePolicyError(RuntimeError):
    """Raised when selective automation cannot be evaluated safely."""


@dataclass(frozen=True)
class SelectivePolicyConfig:
    accept_precision_target: float = 0.98
    false_reject_target: float = 0.01
    fwer_alpha: float = 0.05
    max_thresholds_per_lane: int = 64
    min_top_margin: float = 0.05
    conflict_margin: float = 0.05
    max_unknown_probability: float = 0.50
    min_audit_accepted: int = 100
    min_audit_true_balls: int = 300
    min_independent_components: int = 3
    min_cluster_accepted: int = 30
    min_cluster_true_balls: int = 100


@dataclass(frozen=True)
class _Snapshot:
    path: Path
    sha256: str
    size: int


def fit_selective_policy(
    predictions_path: Path,
    dataset_manifest_path: Path,
    annotation_resolution_path: Path,
    resolved_contract_path: Path,
    model_manifest_path: Path,
    training_report_path: Path,
    policy_roles_path: Path,
    output_dir: Path,
    *,
    config: SelectivePolicyConfig | None = None,
) -> dict[str, Any]:
    config = _validate_config(config or SelectivePolicyConfig())
    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise SelectivePolicyError(f"output directory already exists: {output_dir}")

    loaded = {
        "predictions": _load_snapshot_json(predictions_path, "candidate predictions"),
        "dataset_manifest": _load_snapshot_json(dataset_manifest_path, "candidate dataset manifest"),
        "annotation_resolution": _load_snapshot_json(annotation_resolution_path, "annotation resolution"),
        "model_manifest": _load_snapshot_json(model_manifest_path, "model manifest"),
        "training_report": _load_snapshot_json(training_report_path, "training report"),
        "policy_roles": _load_snapshot_json(policy_roles_path, "selective policy roles"),
    }
    contract, contract_snapshot = _load_snapshot_tracking_contract(
        resolved_contract_path,
        "resolved tracking contract",
    )
    if contract.get("artifact_status") != "loaded" or contract.get("validation_errors"):
        raise SelectivePolicyError(f"resolved tracking contract is invalid: {contract.get('validation_errors')}")

    predictions, predictions_snapshot = loaded["predictions"]
    dataset, dataset_snapshot = loaded["dataset_manifest"]
    resolution, resolution_snapshot = loaded["annotation_resolution"]
    model_manifest, model_snapshot = loaded["model_manifest"]
    training_report, training_snapshot = loaded["training_report"]
    policy_roles, policy_roles_snapshot = loaded["policy_roles"]
    _validate_training_report_package_path(model_manifest, model_snapshot, training_snapshot)
    weights_path = _safe_package_artifact(
        model_snapshot.path.parent,
        model_manifest.get("weights_path"),
        expected_name="model.pt",
        label="model weights",
    )
    weights_snapshot = _snapshot(weights_path, "model weights")
    snapshots = [
        predictions_snapshot,
        dataset_snapshot,
        resolution_snapshot,
        contract_snapshot,
        model_snapshot,
        training_snapshot,
        policy_roles_snapshot,
        weights_snapshot,
    ]
    lineage = _validate_lineage(
        predictions=predictions,
        predictions_snapshot=predictions_snapshot,
        dataset=dataset,
        dataset_snapshot=dataset_snapshot,
        resolution=resolution,
        resolution_snapshot=resolution_snapshot,
        contract=contract,
        contract_snapshot=contract_snapshot,
        model_manifest=model_manifest,
        model_snapshot=model_snapshot,
        training_report=training_report,
        training_snapshot=training_snapshot,
        policy_roles=policy_roles,
        policy_roles_snapshot=policy_roles_snapshot,
        weights_snapshot=weights_snapshot,
    )
    rows = _evaluation_rows(
        predictions,
        dataset,
        resolution,
        contract,
        policy_roles,
        supported_mask=model_manifest["supported_mask"],
    )
    calibration_rows, audit_rows = _validated_evaluation_cohorts(rows, training_report)
    evaluation_cohorts = {
        "calibration_candidate_ids": sorted(row["candidate_id"] for row in calibration_rows),
        "audit_candidate_ids": sorted(row["candidate_id"] for row in audit_rows),
        "application_candidate_ids": sorted(row["candidate_id"] for row in rows if row["policy_role"] is None),
    }

    thresholds, calibration = _fit_thresholds(calibration_rows, config)
    audit = _audit_fixed_policy(audit_rows, thresholds, calibration["certified"], config)
    qualified = calibration["certified"] and audit["qualified"]
    status = "qualified" if qualified else "review_only"
    rules = _policy_rules(config)
    targets = _policy_targets(config)
    decision_rows, derived_contract = _apply_policy(
        rows,
        contract,
        thresholds=thresholds,
        qualified=qualified,
        config=config,
    )
    decision_report = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "artifact_type": "selective_decisions",
        "decision_algorithm": DECISION_ALGORITHM,
        "generated_at": _utc_now_iso(),
        "status": status,
        "lineage": lineage,
        "summary": {
            "candidate_count": len(decision_rows),
            "accept_count": sum(row["decision"] == "accept" for row in decision_rows),
            "reject_count": sum(row["decision"] == "reject" for row in decision_rows),
            "abstain_count": sum(row["decision"] == "abstain" for row in decision_rows),
            "forced_abstain_count": sum(bool(row["forced_abstain_reasons"]) for row in decision_rows),
            "evaluation_holdout_count": sum(row["decision_scope"] == "evaluation_only" for row in decision_rows),
            "application_count": sum(row["decision_scope"] == "application" for row in decision_rows),
            "preserved_existing_decision_count": sum(row["existing_decision_preserved"] for row in decision_rows),
            "pending_application_count": sum(
                row["decision_scope"] == "application"
                and not row["applied_to_contract"]
                and not row["existing_decision_preserved"]
                for row in decision_rows
            ),
        },
        "decisions": decision_rows,
    }
    report_status = (
        "qualified"
        if qualified
        else (
            "insufficient_evidence"
            if calibration["status"] == "insufficient_evidence" or audit["status"] == "insufficient_evidence"
            else "failed"
        )
    )
    decisions_content_sha256 = _canonical_sha256(_normalized_decisions_content(decision_report))
    inferential_unit = {
        "name": INFERENTIAL_UNIT,
        "algorithm": INFERENTIAL_UNIT_ALGORITHM,
        "calibration_component_count": calibration["calibration_component_count"],
        "audit_component_count": audit["audit_component_count"],
    }
    stable_audit = _stable_diagnostic_identity(audit)
    qualification_evidence = _qualification_evidence_summary(calibration, stable_audit)
    version_inputs = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "version_algorithm": POLICY_VERSION_ALGORITHM,
        "algorithm_versions": _algorithm_versions(),
        "inferential_unit": inferential_unit,
        "evaluation_cohorts": evaluation_cohorts,
        "config": _normalized_config(config),
        "qualification": {
            "qualified": qualified,
            "policy_status": status,
            "acceptance_status": report_status,
            "calibration_status": calibration["status"],
            "calibration_certified": calibration["certified"],
            "audit_status": audit["status"],
            "audit_qualified": audit["qualified"],
        },
        "qualification_evidence": qualification_evidence,
        "thresholds": thresholds,
        "rules": rules,
        "targets": targets,
        "lineage": lineage,
        "calibration_sha256": _canonical_sha256(calibration),
        "audit_sha256": _canonical_sha256(stable_audit),
        "decisions_content_sha256": decisions_content_sha256,
    }
    policy_version = _canonical_sha256(version_inputs)
    decision_report["policy_version"] = policy_version
    _validate_finite_json(decision_report, "selective decisions")
    _validate_finite_json(derived_contract, "derived tracking contract")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent))
    try:
        decisions_path = staging_dir / SELECTIVE_DECISIONS_NAME
        _write_json(decisions_path, decision_report)
        decisions_snapshot = _snapshot(decisions_path, "selective decisions")
        decisions_artifact = {
            "path": SELECTIVE_DECISIONS_NAME,
            "sha256": decisions_snapshot.sha256,
            "content_sha256": decisions_content_sha256,
        }
        policy = {
            "schema_version": POLICY_SCHEMA_VERSION,
            "artifact_type": "selective_policy",
            "generated_at": _utc_now_iso(),
            "status": status,
            "policy_version": policy_version,
            "version_inputs": version_inputs,
            "inferential_unit": inferential_unit,
            "evaluation_cohorts": evaluation_cohorts,
            "qualification_evidence": qualification_evidence,
            "decisions_artifact": decisions_artifact,
            "thresholds": thresholds,
            "rules": rules,
            "targets": targets,
            "lineage": lineage,
            "calibration": calibration,
            "audit": stable_audit,
        }
        acceptance_report = {
            "schema_version": POLICY_SCHEMA_VERSION,
            "artifact_type": "selective_acceptance_report",
            "generated_at": _utc_now_iso(),
            "status": report_status,
            "policy_status": status,
            "policy_version": policy_version,
            "version_inputs": version_inputs,
            "inferential_unit": inferential_unit,
            "evaluation_cohorts": evaluation_cohorts,
            "qualification_evidence": qualification_evidence,
            "decisions_artifact": decisions_artifact,
            "lineage": lineage,
            "targets": targets,
            "calibration": calibration,
            "audit": audit,
            "application_summary": decision_report["summary"],
        }
        _validate_policy_version_payload(policy)
        _validate_decisions_binding_payload(policy, decision_report, decisions_snapshot)
        _validate_decision_rows_against_evidence(policy, decision_report, rows, contract)
        _validate_acceptance_report_payload(policy, acceptance_report)
        _validate_finite_json(policy, "selective policy")
        _validate_finite_json(acceptance_report, "selective acceptance report")
        _write_json(staging_dir / SELECTIVE_POLICY_NAME, policy)
        _write_json(staging_dir / SELECTIVE_ACCEPTANCE_REPORT_NAME, acceptance_report)
        _write_json(staging_dir / TRACKING_CONTRACT_REPORT_NAME, derived_contract)
        _verify_snapshots(snapshots)
        os.replace(staging_dir, output_dir)
        return policy
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def build_selective_policy_roles(
    predictions_path: Path,
    dataset_manifest_path: Path,
    annotation_resolution_path: Path,
    resolved_contract_path: Path,
    model_manifest_path: Path,
    training_report_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Build the fixed, component-closed calibration/audit assignment manifest."""

    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise SelectivePolicyError(f"output directory already exists: {output_dir}")

    loaded = {
        "predictions": _load_snapshot_json(predictions_path, "candidate predictions"),
        "dataset_manifest": _load_snapshot_json(dataset_manifest_path, "candidate dataset manifest"),
        "annotation_resolution": _load_snapshot_json(annotation_resolution_path, "annotation resolution"),
        "model_manifest": _load_snapshot_json(model_manifest_path, "model manifest"),
        "training_report": _load_snapshot_json(training_report_path, "training report"),
    }
    contract, contract_snapshot = _load_snapshot_tracking_contract(
        resolved_contract_path,
        "resolved tracking contract",
    )
    if contract.get("artifact_status") != "loaded" or contract.get("validation_errors"):
        raise SelectivePolicyError(f"resolved tracking contract is invalid: {contract.get('validation_errors')}")

    predictions, predictions_snapshot = loaded["predictions"]
    dataset, dataset_snapshot = loaded["dataset_manifest"]
    resolution, resolution_snapshot = loaded["annotation_resolution"]
    model_manifest, model_snapshot = loaded["model_manifest"]
    training_report, training_snapshot = loaded["training_report"]
    _validate_training_report_package_path(model_manifest, model_snapshot, training_snapshot)
    weights_path = _safe_package_artifact(
        model_snapshot.path.parent,
        model_manifest.get("weights_path"),
        expected_name="model.pt",
        label="model weights",
    )
    weights_snapshot = _snapshot(weights_path, "model weights")
    snapshots = [
        predictions_snapshot,
        dataset_snapshot,
        resolution_snapshot,
        contract_snapshot,
        model_snapshot,
        training_snapshot,
        weights_snapshot,
    ]
    _validate_lineage(
        predictions=predictions,
        predictions_snapshot=predictions_snapshot,
        dataset=dataset,
        dataset_snapshot=dataset_snapshot,
        resolution=resolution,
        resolution_snapshot=resolution_snapshot,
        contract=contract,
        contract_snapshot=contract_snapshot,
        model_manifest=model_manifest,
        model_snapshot=model_snapshot,
        training_report=training_report,
        training_snapshot=training_snapshot,
        policy_roles=None,
        policy_roles_snapshot=None,
        weights_snapshot=weights_snapshot,
    )
    rows = _evaluation_rows(
        predictions,
        dataset,
        resolution,
        contract,
        None,
        supported_mask=model_manifest["supported_mask"],
    )
    human_binary_rows = [
        row for row in rows if row["truth"] in {"match_ball", "noise"} and row["truth_origin"] == "human_confirmed"
    ]
    roles, assigned_components, candidate_component_mapping = _build_deterministic_role_assignment(human_binary_rows)
    role_lineage = _policy_role_lineage(
        predictions_snapshot=predictions_snapshot,
        dataset_snapshot=dataset_snapshot,
        resolution_snapshot=resolution_snapshot,
        contract_snapshot=contract_snapshot,
        model_snapshot=model_snapshot,
        training_snapshot=training_snapshot,
        weights_snapshot=weights_snapshot,
        dataset_version=_required_text(dataset.get("dataset_version"), "dataset_version"),
        model_version=_required_text(model_manifest.get("model_version"), "model_version"),
    )
    manifest = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "artifact_type": "selective_policy_roles",
        "assignment_strategy": ROLE_ASSIGNMENT_ALGORITHM,
        "assignment_seed": POLICY_ROLE_SEED,
        "component_id_algorithm": ROLE_COMPONENT_ID_ALGORITHM,
        "inferential_unit": INFERENTIAL_UNIT,
        "inferential_unit_algorithm": INFERENTIAL_UNIT_ALGORITHM,
        "component_count": len(assigned_components),
        "evaluation_candidate_count": len(candidate_component_mapping),
        "candidate_component_mapping": candidate_component_mapping,
        "lineage": role_lineage,
        "roles": roles,
        "components": assigned_components,
    }
    _validate_policy_roles_envelope(manifest, expected_lineage=role_lineage)
    validated_rows = _evaluation_rows(
        predictions,
        dataset,
        resolution,
        contract,
        manifest,
        supported_mask=model_manifest["supported_mask"],
    )
    _validated_evaluation_cohorts(validated_rows, training_report)
    _validate_finite_json(manifest, "selective policy roles")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent))
    try:
        _write_json(staging_dir / SELECTIVE_POLICY_ROLES_NAME, manifest)
        _verify_snapshots(snapshots)
        os.replace(staging_dir, output_dir)
        return manifest
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def validate_selective_decisions_binding(policy_path: Path, decisions_path: Path) -> dict[str, Any]:
    """Validate the exact decisions snapshot and canonical behavior content bound by a policy."""

    policy_path = Path(policy_path).resolve()
    decisions_path = Path(decisions_path).resolve()
    expected_decisions_path = (policy_path.parent / SELECTIVE_DECISIONS_NAME).resolve()
    if decisions_path != expected_decisions_path:
        raise SelectivePolicyError("selective decisions must be the exact file beside the policy")
    policy, _ = _load_snapshot_json(policy_path, "selective policy")
    decisions, decisions_snapshot = _load_snapshot_json(decisions_path, "selective decisions")
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION or policy.get("artifact_type") != "selective_policy":
        raise SelectivePolicyError("invalid selective policy envelope")
    _validate_policy_version_payload(policy)
    _validate_decisions_binding_payload(policy, decisions, decisions_snapshot)
    return decisions


def validate_selective_decision_semantics(
    policy: dict[str, Any],
    decisions: dict[str, Any],
    rows: list[dict[str, Any]],
    resolved_contract: dict[str, Any],
) -> None:
    """Recompute every decision row from authoritative evidence and the resolved contract."""

    _validate_policy_version_payload(policy)
    if (
        decisions.get("schema_version") != POLICY_SCHEMA_VERSION
        or decisions.get("artifact_type") != "selective_decisions"
        or decisions.get("decision_algorithm") != DECISION_ALGORITHM
        or decisions.get("policy_version") != policy.get("policy_version")
        or decisions.get("status") != policy.get("status")
        or decisions.get("lineage") != policy.get("lineage")
    ):
        raise SelectivePolicyError("selective decisions do not match the validated policy")
    if _canonical_sha256(_normalized_decisions_content(decisions)) != policy["version_inputs"].get(
        "decisions_content_sha256"
    ):
        raise SelectivePolicyError("selective decisions behavior content does not match the validated policy")
    _validate_decision_cohort_partition(policy, decisions)
    _validate_decision_rows_semantics(policy, decisions)
    _validate_decisions_summary(decisions)
    _validate_decision_rows_against_evidence(policy, decisions, rows, resolved_contract)


def validate_selective_policy_evidence_binding(
    policy_path: Path,
    decisions_path: Path,
    predictions_path: Path,
    dataset_manifest_path: Path,
    annotation_resolution_path: Path,
    resolved_contract_path: Path,
    model_manifest_path: Path,
    policy_roles_path: Path,
) -> dict[str, Any]:
    """Rebuild policy qualification and decisions from the exact external evidence lineage."""

    policy, policy_snapshot = _load_snapshot_json(policy_path, "selective policy")
    decisions, decisions_snapshot = _load_snapshot_json(decisions_path, "selective decisions")
    validated_decisions = validate_selective_decisions_binding(policy_snapshot.path, decisions_snapshot.path)
    if validated_decisions != decisions:
        raise SelectivePolicyError("selective decisions changed during evidence binding validation")

    predictions, predictions_snapshot = _load_snapshot_json(predictions_path, "candidate predictions")
    dataset, dataset_snapshot = _load_snapshot_json(dataset_manifest_path, "candidate dataset manifest")
    resolution, resolution_snapshot = _load_snapshot_json(annotation_resolution_path, "annotation resolution")
    model_manifest, model_snapshot = _load_snapshot_json(model_manifest_path, "model manifest")
    policy_roles, policy_roles_snapshot = _load_snapshot_json(policy_roles_path, "selective policy roles")
    resolved_contract, contract_snapshot = _load_snapshot_tracking_contract(
        resolved_contract_path,
        "resolved tracking contract",
    )
    if resolved_contract.get("artifact_status") != "loaded" or resolved_contract.get("validation_errors"):
        raise SelectivePolicyError(
            f"resolved tracking contract is invalid: {resolved_contract.get('validation_errors')}"
        )

    training_path = _safe_package_artifact(
        model_snapshot.path.parent,
        model_manifest.get("training_report_path"),
        expected_name="training_report.v1.json",
        label="training report",
    )
    training_report, training_snapshot = _load_snapshot_json(training_path, "training report")
    _validate_training_report_package_path(model_manifest, model_snapshot, training_snapshot)
    weights_path = _safe_package_artifact(
        model_snapshot.path.parent,
        model_manifest.get("weights_path"),
        expected_name="model.pt",
        label="model weights",
    )
    weights_snapshot = _snapshot(weights_path, "model weights")
    snapshots = [
        policy_snapshot,
        decisions_snapshot,
        predictions_snapshot,
        dataset_snapshot,
        resolution_snapshot,
        contract_snapshot,
        model_snapshot,
        training_snapshot,
        policy_roles_snapshot,
        weights_snapshot,
    ]
    source_contract_snapshot = _qualification_source_contract_snapshot(
        dataset_snapshot,
        resolution,
    )
    try:
        validate_candidate_predictions_package(
            model_snapshot.path.parent,
            dataset_snapshot.path,
            source_contract_snapshot.path,
            predictions_snapshot.path,
        )
    except ClassifierError as exc:
        raise SelectivePolicyError(
            f"qualification predictions do not reproduce from frozen classifier inference: {exc}"
        ) from exc
    snapshots.append(source_contract_snapshot)
    lineage = _validate_lineage(
        predictions=predictions,
        predictions_snapshot=predictions_snapshot,
        dataset=dataset,
        dataset_snapshot=dataset_snapshot,
        resolution=resolution,
        resolution_snapshot=resolution_snapshot,
        contract=resolved_contract,
        contract_snapshot=contract_snapshot,
        model_manifest=model_manifest,
        model_snapshot=model_snapshot,
        training_report=training_report,
        training_snapshot=training_snapshot,
        policy_roles=policy_roles,
        policy_roles_snapshot=policy_roles_snapshot,
        weights_snapshot=weights_snapshot,
    )
    if policy.get("lineage") != lineage or decisions.get("lineage") != lineage:
        raise SelectivePolicyError("policy lineage does not match the supplied qualification evidence snapshots")

    rows = _evaluation_rows(
        predictions,
        dataset,
        resolution,
        resolved_contract,
        policy_roles,
        supported_mask=model_manifest["supported_mask"],
    )
    calibration_rows, audit_rows = _validated_evaluation_cohorts(rows, training_report)
    expected_cohorts = {
        "calibration_candidate_ids": sorted(row["candidate_id"] for row in calibration_rows),
        "audit_candidate_ids": sorted(row["candidate_id"] for row in audit_rows),
        "application_candidate_ids": sorted(row["candidate_id"] for row in rows if row["policy_role"] is None),
    }
    if policy.get("evaluation_cohorts") != expected_cohorts:
        raise SelectivePolicyError("policy evaluation cohorts do not match the supplied human-confirmed evidence")

    version_inputs = policy.get("version_inputs")
    config_payload = version_inputs.get("config") if isinstance(version_inputs, dict) else None
    if not isinstance(config_payload, dict):
        raise SelectivePolicyError("selective policy config is missing")
    try:
        config = _validate_config(SelectivePolicyConfig(**config_payload))
    except TypeError as exc:
        raise SelectivePolicyError("selective policy config fields are invalid") from exc
    if _normalized_config(config) != config_payload:
        raise SelectivePolicyError("selective policy config is not canonical")

    thresholds, calibration = _fit_thresholds(calibration_rows, config)
    if policy.get("thresholds") != thresholds or policy.get("calibration") != calibration:
        raise SelectivePolicyError("policy calibration does not match the supplied qualification evidence")
    audit = _stable_diagnostic_identity(_audit_fixed_policy(audit_rows, thresholds, calibration["certified"], config))
    if policy.get("audit") != audit:
        raise SelectivePolicyError("policy audit does not match the supplied human-confirmed evaluations")
    expected_status = "qualified" if calibration["certified"] and audit["qualified"] else "review_only"
    if policy.get("status") != expected_status:
        raise SelectivePolicyError("policy status does not match recomputed qualification evidence")
    validate_selective_decision_semantics(policy, decisions, rows, resolved_contract)
    _verify_snapshots(snapshots)
    return {
        "policy": policy,
        "decisions": decisions,
        "lineage": lineage,
        "evaluation_cohorts": expected_cohorts,
        "bindings": {
            "annotation_resolution": {
                "path": resolution_snapshot.path.name,
                "sha256": resolution_snapshot.sha256,
            },
            "resolved_tracking_contract": {
                "path": contract_snapshot.path.name,
                "sha256": contract_snapshot.sha256,
            },
            "policy_roles": {
                "path": policy_roles_snapshot.path.name,
                "sha256": policy_roles_snapshot.sha256,
            },
        },
    }


def apply_frozen_selective_policy(
    policy_path: Path,
    predictions_path: Path,
    dataset_manifest_path: Path,
    target_contract_path: Path,
    model_manifest_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Apply an already-qualified policy to a truth-free, disjoint target population."""

    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise SelectivePolicyError(f"output directory already exists: {output_dir}")
    policy, rows, lineage, snapshots, contract = _frozen_application_inputs(
        policy_path,
        predictions_path,
        dataset_manifest_path,
        target_contract_path,
        model_manifest_path,
    )
    decisions, _ = _apply_policy(
        rows,
        contract,
        thresholds=policy["thresholds"],
        qualified=True,
        config=_validated_policy_config(policy),
    )
    application = _frozen_application_payload(policy, decisions, lineage)
    _validate_finite_json(application, "selective policy application")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent))
    try:
        _write_json(staging / SELECTIVE_APPLICATION_NAME, application)
        _verify_snapshots(snapshots)
        os.replace(staging, output_dir)
        return application
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def validate_selective_policy_application_binding(
    policy_path: Path,
    application_path: Path,
    predictions_path: Path,
    dataset_manifest_path: Path,
    target_contract_path: Path,
    model_manifest_path: Path,
) -> dict[str, Any]:
    """Recompute a target application from the frozen policy and exact target evidence."""

    application, application_snapshot = _load_snapshot_json(application_path, "selective policy application")
    policy, rows, lineage, snapshots, contract = _frozen_application_inputs(
        policy_path,
        predictions_path,
        dataset_manifest_path,
        target_contract_path,
        model_manifest_path,
    )
    expected_rows, _ = _apply_policy(
        rows,
        contract,
        thresholds=policy["thresholds"],
        qualified=True,
        config=_validated_policy_config(policy),
    )
    expected = _frozen_application_payload(policy, expected_rows, lineage, generated_at=application.get("generated_at"))
    if application != expected:
        raise SelectivePolicyError("target application does not match the frozen policy and target evidence")
    _verify_snapshots([*snapshots, application_snapshot])
    return {
        "policy": policy,
        "application": application,
        "candidate_ids": sorted(row["candidate_id"] for row in expected_rows),
        "candidate_population_sha256": _canonical_sha256(
            [{"candidate_id": row["candidate_id"], "candidate_fingerprint": row["candidate_fingerprint"]} for row in expected_rows]
        ),
        "lineage": lineage,
    }


def _frozen_application_inputs(
    policy_path: Path,
    predictions_path: Path,
    dataset_manifest_path: Path,
    target_contract_path: Path,
    model_manifest_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], list[_Snapshot], dict[str, Any]]:
    from football_tracking.candidate_classifier import (
        ClassifierError,
        load_candidate_classifier,
        validate_candidate_predictions_package,
    )

    policy, policy_snapshot = _load_snapshot_json(policy_path, "selective policy")
    _validate_policy_version_payload(policy)
    if policy.get("status") != "qualified":
        raise SelectivePolicyError("frozen policy must be qualified before target application")
    predictions, predictions_snapshot = _load_snapshot_json(predictions_path, "target candidate predictions")
    dataset, dataset_snapshot = _load_snapshot_json(dataset_manifest_path, "target candidate dataset")
    model_manifest, model_snapshot = _load_snapshot_json(model_manifest_path, "model manifest")
    contract, contract_snapshot = _load_snapshot_tracking_contract(target_contract_path, "target tracking contract")
    if contract.get("artifact_status") != "loaded" or contract.get("validation_errors"):
        raise SelectivePolicyError("target tracking contract is invalid")
    try:
        _, validated_model = load_candidate_classifier(model_snapshot.path.parent)
    except (ClassifierError, OSError, ValueError) as exc:
        raise SelectivePolicyError(f"target model package is invalid: {exc}") from exc
    if validated_model != model_manifest:
        raise SelectivePolicyError("model manifest changed during target application validation")
    try:
        validate_candidate_predictions_package(
            model_snapshot.path.parent,
            dataset_snapshot.path,
            contract_snapshot.path,
            predictions_snapshot.path,
        )
    except (ClassifierError, OSError, ValueError) as exc:
        raise SelectivePolicyError(f"target predictions do not reproduce from the frozen model: {exc}") from exc
    if dataset.get("schema_version") != "1.0" or dataset.get("artifact_type") != "candidate_dataset":
        raise SelectivePolicyError("invalid target candidate dataset envelope")
    summary = dataset.get("summary")
    samples = dataset.get("samples")
    sources = dataset.get("sources")
    if (
        not isinstance(summary, dict)
        or summary.get("status") != "ok"
        or not isinstance(samples, list)
        or not samples
        or summary.get("sample_count") != len(samples)
        or not isinstance(sources, list)
        or not sources
        or summary.get("source_count") != len(sources)
    ):
        raise SelectivePolicyError("target candidate dataset must be successful and non-empty")
    if predictions.get("schema_version") != "1.0" or predictions.get("artifact_type") != "candidate_predictions":
        raise SelectivePolicyError("invalid target candidate predictions envelope")
    if predictions.get("dataset_version") != dataset.get("dataset_version"):
        raise SelectivePolicyError("target predictions dataset version mismatch")
    if predictions.get("model_version") != model_manifest.get("model_version"):
        raise SelectivePolicyError("target predictions model version mismatch")
    if predictions.get("source_contract_sha256") != contract_snapshot.sha256:
        raise SelectivePolicyError("target predictions contract binding mismatch")
    contract_binding = dataset.get("contract")
    if not isinstance(contract_binding, dict) or contract_binding.get("sha256") != contract_snapshot.sha256:
        raise SelectivePolicyError("target dataset contract binding mismatch")
    if predictions.get("class_order") != list(CLASSIFICATION_LABELS):
        raise SelectivePolicyError("target predictions class order is incompatible")
    if predictions.get("temperature") != model_manifest.get("calibration", {}).get("temperature"):
        raise SelectivePolicyError("target predictions temperature does not match the frozen model")
    truth_fields = {"label", "truth", "ground_truth", "training_label", "policy_role"}
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            raise SelectivePolicyError(f"target dataset sample {index} is invalid")
        disclosed = sorted(field for field in truth_fields if field in sample)
        if disclosed:
            raise SelectivePolicyError(
                f"target application dataset discloses qualification truth or roles: {disclosed}"
            )
    if any(
        isinstance(row, dict) and row.get("label_origin") in CONFIRMED_LABEL_ORIGINS
        for row in contract.get("classifications", [])
    ):
        raise SelectivePolicyError("target application contract contains confirmed candidate truth")
    candidate_ids = [_required_text(row.get("candidate_id"), "target candidate_id") for row in contract["candidates"]]
    neutral_resolution = {
        "resolutions": [
            {
                "candidate_id": candidate_id,
                "status": "pending_adjudication",
                "label": "unknown",
                "label_origin": "prelabel",
            }
            for candidate_id in candidate_ids
        ]
    }
    rows = _evaluation_rows(
        predictions,
        dataset,
        neutral_resolution,
        contract,
        None,
        supported_mask=model_manifest["supported_mask"],
    )
    if any(row.get("policy_role") is not None or row.get("truth") is not None for row in rows):
        raise SelectivePolicyError("target application may not reuse qualification roles or truth")
    training_path = _safe_package_artifact(
        model_snapshot.path.parent,
        model_manifest.get("training_report_path"),
        expected_name="training_report.v1.json",
        label="training report",
    )
    weights_path = _safe_package_artifact(
        model_snapshot.path.parent,
        model_manifest.get("weights_path"),
        expected_name="model.pt",
        label="model weights",
    )
    training_snapshot = _snapshot(training_path, "training report")
    weights_snapshot = _snapshot(weights_path, "model weights")
    qualification_lineage = policy.get("lineage")
    if not isinstance(qualification_lineage, dict):
        raise SelectivePolicyError("qualified policy lineage is invalid")
    if qualification_lineage.get("model_version") != model_manifest.get("model_version"):
        raise SelectivePolicyError("target model version differs from the qualified frozen model")
    for name, snapshot in (
        ("model_manifest", model_snapshot),
        ("training_report", training_snapshot),
        ("model_weights", weights_snapshot),
    ):
        qualified_descriptor = qualification_lineage.get(name)
        if not isinstance(qualified_descriptor, dict):
            raise SelectivePolicyError(f"qualified policy lineage {name} is invalid")
        if _required_sha256(
            qualified_descriptor.get("sha256"), f"qualified policy lineage {name} sha256"
        ) != snapshot.sha256:
            raise SelectivePolicyError(
                f"target {name.replace('_', ' ')} differs from the qualified frozen model"
            )
    lineage = {
        "policy": {"sha256": policy_snapshot.sha256},
        "predictions": {"sha256": predictions_snapshot.sha256},
        "dataset_manifest": {"sha256": dataset_snapshot.sha256},
        "target_contract": {"sha256": contract_snapshot.sha256},
        "model_manifest": {"sha256": model_snapshot.sha256},
        "training_report": {"sha256": training_snapshot.sha256},
        "model_weights": {"sha256": weights_snapshot.sha256},
        "dataset_version": dataset["dataset_version"],
        "model_version": model_manifest["model_version"],
        "policy_version": policy["policy_version"],
    }
    return (
        policy,
        rows,
        lineage,
        [
            policy_snapshot,
            predictions_snapshot,
            dataset_snapshot,
            contract_snapshot,
            model_snapshot,
            training_snapshot,
            weights_snapshot,
        ],
        contract,
    )


def _frozen_application_payload(
    policy: dict[str, Any],
    decisions: list[dict[str, Any]],
    lineage: dict[str, Any],
    *,
    generated_at: Any = None,
) -> dict[str, Any]:
    if generated_at is None:
        generated_at = _utc_now_iso()
    summary = {
        "candidate_count": len(decisions),
        "accept_count": sum(row["decision"] == "accept" for row in decisions),
        "reject_count": sum(row["decision"] == "reject" for row in decisions),
        "abstain_count": sum(row["decision"] == "abstain" for row in decisions),
        "review_count": sum(row["decision"] == "abstain" for row in decisions),
        "excluded_existing_decision_count": sum(bool(row["existing_decision_preserved"]) for row in decisions),
    }
    content = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "artifact_type": "selective_policy_application",
        "application_algorithm": DECISION_ALGORITHM,
        "status": "qualified_policy_applied",
        "policy_version": policy["policy_version"],
        "dataset_version": lineage["dataset_version"],
        "model_version": lineage["model_version"],
        "lineage": lineage,
        "summary": summary,
        "decisions": decisions,
    }
    return {**content, "generated_at": generated_at, "application_content_sha256": _canonical_sha256(content)}


def _normalized_config(config: SelectivePolicyConfig) -> dict[str, Any]:
    normalized = asdict(config)
    for name in (
        "accept_precision_target",
        "false_reject_target",
        "fwer_alpha",
        "min_top_margin",
        "conflict_margin",
        "max_unknown_probability",
    ):
        normalized[name] = float(normalized[name])
    return normalized


def _policy_rules(config: SelectivePolicyConfig) -> dict[str, Any]:
    return {
        "min_top_margin": float(config.min_top_margin),
        "conflict_margin": float(config.conflict_margin),
        "max_unknown_probability": float(config.max_unknown_probability),
        "confirmed_unknown": "force_abstain",
        "confirmed_conflict": "force_abstain",
        "existing_decision": "preserve_and_abstain",
        "top_unknown": "force_abstain",
    }


def _policy_targets(config: SelectivePolicyConfig) -> dict[str, float]:
    return {
        "auto_accept_precision_min": float(config.accept_precision_target),
        "true_ball_false_reject_rate_max": float(config.false_reject_target),
        "fwer_alpha": float(config.fwer_alpha),
    }


def _algorithm_versions() -> dict[str, str]:
    return {
        "threshold_selection": THRESHOLD_ALGORITHM,
        "fixed_audit": AUDIT_ALGORITHM,
        "application_decisions": DECISION_ALGORITHM,
        "role_component_identity": ROLE_COMPONENT_ID_ALGORITHM,
        "role_assignment": ROLE_ASSIGNMENT_ALGORITHM,
        "inferential_unit": INFERENTIAL_UNIT_ALGORITHM,
    }


def _normalized_decisions_content(decision_report: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in decision_report.items() if key not in {"generated_at", "policy_version"}}


def _stable_diagnostic_identity(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _stable_diagnostic_identity(item) for key, item in value.items() if key != "generated_at"}
    if isinstance(value, list):
        return [_stable_diagnostic_identity(item) for item in value]
    return value


def _qualification_evidence_summary(calibration: dict[str, Any], audit: dict[str, Any]) -> dict[str, Any]:
    calibration_gate = calibration.get("independent_component_gate")
    if not isinstance(calibration_gate, dict):
        calibration_gate = {}
    audit_gates = audit.get("sample_gates")
    if not isinstance(audit_gates, dict):
        audit_gates = {}
    confidence = audit.get("one_sided_confidence")
    if not isinstance(confidence, dict):
        confidence = {}

    def gate(name: str, gates: dict[str, Any]) -> dict[str, Any]:
        value = gates.get(name)
        if not isinstance(value, dict):
            value = {}
        return {key: value.get(key) for key in ("observed", "minimum", "passed")}

    calibration_certified = calibration.get("certified") is True
    audit_qualified = audit.get("qualified") is True
    qualified = calibration_certified and audit_qualified
    calibration_status = calibration.get("status")
    audit_status = audit.get("status")
    acceptance_status = (
        "qualified"
        if qualified
        else (
            "insufficient_evidence"
            if calibration_status == "insufficient_evidence" or audit_status == "insufficient_evidence"
            else "failed"
        )
    )
    return {
        "calibration": {
            "status": calibration_status,
            "certified": calibration.get("certified"),
            "calibration_component_count": calibration.get("calibration_component_count"),
            "independent_component_gate": {key: calibration_gate.get(key) for key in ("observed", "minimum", "passed")},
            "selected_hypothesis_present": calibration.get("selected_hypothesis") is not None,
        },
        "audit": {
            "status": audit_status,
            "qualified": audit.get("qualified"),
            "audit_component_count": audit.get("audit_component_count"),
            "accepted_component_count": confidence.get("accepted_component_count"),
            "true_ball_component_count": confidence.get("true_ball_component_count"),
            "sample_gates": {
                name: gate(name, audit_gates)
                for name in ("accepted_components", "true_ball_components", "independent_components")
            },
            "point_targets_passed": audit.get("point_targets_passed"),
            "exact_confidence_passed": confidence.get("passed"),
        },
        "aggregate": {
            "qualified": qualified,
            "policy_status": "qualified" if qualified else "review_only",
            "acceptance_status": acceptance_status,
        },
    }


def _validate_policy_version_payload(policy: dict[str, Any]) -> None:
    version_inputs = policy.get("version_inputs")
    expected_version_fields = {
        "schema_version",
        "version_algorithm",
        "algorithm_versions",
        "inferential_unit",
        "evaluation_cohorts",
        "config",
        "qualification",
        "qualification_evidence",
        "thresholds",
        "rules",
        "targets",
        "lineage",
        "calibration_sha256",
        "audit_sha256",
        "decisions_content_sha256",
    }
    if not isinstance(version_inputs, dict) or set(version_inputs) != expected_version_fields:
        raise SelectivePolicyError("selective policy version_inputs are incomplete or contain unsupported fields")
    expected_version = _canonical_sha256(version_inputs)
    if policy.get("policy_version") != expected_version:
        raise SelectivePolicyError("selective policy_version does not match version_inputs")
    if version_inputs.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise SelectivePolicyError("selective policy version_inputs schema is incompatible")
    if version_inputs.get("version_algorithm") != POLICY_VERSION_ALGORITHM:
        raise SelectivePolicyError("selective policy version algorithm is incompatible")
    if version_inputs.get("algorithm_versions") != _algorithm_versions():
        raise SelectivePolicyError("selective policy algorithm versions are incompatible")
    inferential_unit = version_inputs.get("inferential_unit")
    expected_inferential_fields = {
        "name",
        "algorithm",
        "calibration_component_count",
        "audit_component_count",
    }
    if not isinstance(inferential_unit, dict) or set(inferential_unit) != expected_inferential_fields:
        raise SelectivePolicyError("selective policy inferential unit is incomplete")
    if (
        inferential_unit.get("name") != INFERENTIAL_UNIT
        or inferential_unit.get("algorithm") != INFERENTIAL_UNIT_ALGORITHM
    ):
        raise SelectivePolicyError("selective policy inferential unit algorithm is incompatible")
    for name in ("calibration_component_count", "audit_component_count"):
        count = inferential_unit.get(name)
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            raise SelectivePolicyError(f"selective policy {name} must be a positive integer")
    if policy.get("inferential_unit") != inferential_unit:
        raise SelectivePolicyError("selective policy inferential unit does not match version_inputs")
    evaluation_cohorts = version_inputs.get("evaluation_cohorts")
    if policy.get("evaluation_cohorts") != evaluation_cohorts:
        raise SelectivePolicyError("selective policy evaluation cohorts do not match version_inputs")
    calibration_candidate_ids, audit_candidate_ids, _application_candidate_ids = _validate_evaluation_cohorts(
        evaluation_cohorts,
        calibration_component_count=inferential_unit["calibration_component_count"],
        audit_component_count=inferential_unit["audit_component_count"],
    )
    config = version_inputs.get("config")
    if not isinstance(config, dict) or set(config) != set(asdict(SelectivePolicyConfig())):
        raise SelectivePolicyError("selective policy normalized config is incomplete")
    try:
        normalized_config = _validate_config(SelectivePolicyConfig(**config))
    except (TypeError, ValueError, SelectivePolicyError) as exc:
        raise SelectivePolicyError(f"selective policy normalized config is invalid: {exc}") from exc
    if config != _normalized_config(normalized_config):
        raise SelectivePolicyError("selective policy config is not normalized")
    for field in ("thresholds", "rules", "targets", "lineage"):
        if version_inputs.get(field) != policy.get(field):
            raise SelectivePolicyError(f"selective policy {field} does not match version_inputs")
    thresholds = version_inputs.get("thresholds")
    if not isinstance(thresholds, dict) or set(thresholds) != {"accept", "reject"}:
        raise SelectivePolicyError("selective policy thresholds are invalid")
    accept_threshold = _finite_number(thresholds.get("accept"), "selective policy accept threshold")
    reject_threshold = _finite_number(thresholds.get("reject"), "selective policy reject threshold")
    if not 0.0 <= accept_threshold <= 1.0 or not 0.0 <= reject_threshold <= 1.0:
        raise SelectivePolicyError("selective policy thresholds must be between zero and one")
    if version_inputs.get("rules") != _policy_rules(normalized_config):
        raise SelectivePolicyError("selective policy rules do not match normalized config")
    if version_inputs.get("targets") != _policy_targets(normalized_config):
        raise SelectivePolicyError("selective policy targets do not match normalized config")
    if not isinstance(version_inputs.get("lineage"), dict) or not version_inputs["lineage"]:
        raise SelectivePolicyError("selective policy lineage is missing")
    calibration = policy.get("calibration")
    if not isinstance(calibration, dict):
        raise SelectivePolicyError("selective policy calibration is missing")
    calibration_sha256 = _required_sha256(
        version_inputs.get("calibration_sha256"), "selective policy calibration sha256"
    )
    audit_sha256 = _required_sha256(version_inputs.get("audit_sha256"), "selective policy audit sha256")
    _required_sha256(version_inputs.get("decisions_content_sha256"), "selective policy decisions content sha256")
    if calibration_sha256 != _canonical_sha256(calibration):
        raise SelectivePolicyError("selective policy calibration does not match version_inputs")
    if calibration.get("method") != THRESHOLD_ALGORITHM:
        raise SelectivePolicyError("selective policy calibration algorithm is incompatible")
    if (
        calibration.get("inferential_unit") != INFERENTIAL_UNIT
        or calibration.get("inferential_unit_algorithm") != INFERENTIAL_UNIT_ALGORITHM
    ):
        raise SelectivePolicyError("selective policy calibration inferential unit is incompatible")
    if calibration.get("calibration_component_count") != inferential_unit["calibration_component_count"]:
        raise SelectivePolicyError("selective policy calibration component count does not match version_inputs")
    _validate_calibration_semantics(
        calibration,
        config=normalized_config,
        thresholds=thresholds,
        calibration_component_count=inferential_unit["calibration_component_count"],
        calibration_candidate_ids=calibration_candidate_ids,
    )
    audit = policy.get("audit")
    if not isinstance(audit, dict):
        raise SelectivePolicyError("selective policy audit evidence is missing")
    if _canonical_sha256(audit) != audit_sha256:
        raise SelectivePolicyError("selective policy audit evidence does not match version_inputs")
    _validate_audit_semantics(
        audit,
        config=normalized_config,
        thresholds=thresholds,
        calibration_certified=calibration["certified"],
        audit_component_count=inferential_unit["audit_component_count"],
        audit_candidate_ids=audit_candidate_ids,
    )
    qualification = version_inputs.get("qualification")
    expected_qualification_fields = {
        "qualified",
        "policy_status",
        "acceptance_status",
        "calibration_status",
        "calibration_certified",
        "audit_status",
        "audit_qualified",
    }
    if not isinstance(qualification, dict) or set(qualification) != expected_qualification_fields:
        raise SelectivePolicyError("selective policy qualification status does not match version_inputs")
    qualified = qualification.get("qualified")
    calibration_certified = qualification.get("calibration_certified")
    audit_qualified = qualification.get("audit_qualified")
    if not all(isinstance(value, bool) for value in (qualified, calibration_certified, audit_qualified)):
        raise SelectivePolicyError("selective policy qualification flags must be booleans")
    calibration_status = qualification.get("calibration_status")
    audit_status = qualification.get("audit_status")
    if calibration_status not in {"certified", "insufficient_evidence"}:
        raise SelectivePolicyError("selective policy calibration status is invalid")
    if audit_status not in {"qualified", "failed", "insufficient_evidence"}:
        raise SelectivePolicyError("selective policy audit status is invalid")
    if calibration.get("status") != calibration_status or calibration.get("certified") is not calibration_certified:
        raise SelectivePolicyError("selective policy calibration qualification is inconsistent")
    if calibration_certified != (calibration_status == "certified"):
        raise SelectivePolicyError("selective policy calibration status and certification disagree")
    if audit_qualified != (audit_status == "qualified") or qualified != (calibration_certified and audit_qualified):
        raise SelectivePolicyError("selective policy audit or aggregate qualification is inconsistent")
    expected_policy_status = "qualified" if qualified else "review_only"
    if policy.get("status") != expected_policy_status or qualification.get("policy_status") != expected_policy_status:
        raise SelectivePolicyError("selective policy status does not match qualification")
    expected_acceptance_status = (
        "qualified"
        if qualified
        else (
            "insufficient_evidence"
            if calibration_status == "insufficient_evidence" or audit_status == "insufficient_evidence"
            else "failed"
        )
    )
    if qualification.get("acceptance_status") != expected_acceptance_status:
        raise SelectivePolicyError("selective policy acceptance status does not match qualification")
    qualification_evidence = version_inputs.get("qualification_evidence")
    if policy.get("qualification_evidence") != qualification_evidence:
        raise SelectivePolicyError("selective policy qualification evidence does not match version_inputs")
    if qualification_evidence != _qualification_evidence_summary(calibration, audit):
        raise SelectivePolicyError("selective policy qualification evidence is not derived from calibration and audit")
    _validate_qualification_evidence(
        qualification_evidence,
        config=normalized_config,
        inferential_unit=inferential_unit,
        calibration=calibration,
        qualification=qualification,
    )


def _validate_evaluation_cohorts(
    value: Any,
    *,
    calibration_component_count: int,
    audit_component_count: int,
) -> tuple[list[str], list[str], list[str]]:
    expected_fields = {"calibration_candidate_ids", "audit_candidate_ids", "application_candidate_ids"}
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise SelectivePolicyError("selective policy evaluation cohorts have invalid fields")
    cohorts: dict[str, list[str]] = {}
    for name in sorted(expected_fields):
        candidate_ids = value.get(name)
        if not isinstance(candidate_ids, list) or not all(
            isinstance(candidate_id, str) and candidate_id and candidate_id == candidate_id.strip()
            for candidate_id in candidate_ids
        ):
            raise SelectivePolicyError(f"selective policy {name} must be a list of candidate IDs")
        if candidate_ids != sorted(set(candidate_ids)):
            raise SelectivePolicyError(f"selective policy {name} must be sorted and unique")
        cohorts[name] = candidate_ids
    calibration_ids = cohorts["calibration_candidate_ids"]
    audit_ids = cohorts["audit_candidate_ids"]
    application_ids = cohorts["application_candidate_ids"]
    if len(calibration_ids) != calibration_component_count or len(audit_ids) != audit_component_count:
        raise SelectivePolicyError("selective policy evaluation cohort lengths do not match inferential counts")
    cohort_sets = [set(calibration_ids), set(audit_ids), set(application_ids)]
    if any(left & right for index, left in enumerate(cohort_sets) for right in cohort_sets[index + 1 :]):
        raise SelectivePolicyError("selective policy evaluation cohorts overlap")
    return calibration_ids, audit_ids, application_ids


def _validate_calibration_semantics(
    calibration: dict[str, Any],
    *,
    config: SelectivePolicyConfig,
    thresholds: dict[str, float],
    calibration_component_count: int,
    calibration_candidate_ids: list[str],
) -> None:
    expected_fields = {
        "status",
        "certified",
        "method",
        "inferential_unit",
        "inferential_unit_algorithm",
        "calibration_count",
        "calibration_component_count",
        "calibration_candidate_ids",
        "evidence_dimension_counts",
        "independent_component_gate",
        "accept_threshold_grid",
        "reject_threshold_grid",
        "predeclared_pair_count",
        "component_hypothesis_count",
        "holm_rejected_hypotheses",
        "minimum_zero_error_samples",
        "selected_hypothesis",
        "accept_hypotheses",
        "reject_hypotheses",
        "certified_pairs",
        "cluster_diagnostics",
        "hypothesis_family_sha256",
    }
    if set(calibration) != expected_fields:
        raise SelectivePolicyError("selective policy calibration structure is invalid")
    if (
        calibration.get("method") != THRESHOLD_ALGORITHM
        or calibration.get("inferential_unit") != INFERENTIAL_UNIT
        or calibration.get("inferential_unit_algorithm") != INFERENTIAL_UNIT_ALGORITHM
    ):
        raise SelectivePolicyError("selective policy calibration method or inferential unit is incompatible")
    calibration_count = _qualification_count(calibration.get("calibration_count"), "selective policy calibration count")
    component_count = _qualification_count(
        calibration.get("calibration_component_count"), "selective policy calibration component count"
    )
    if calibration_count != component_count or component_count != calibration_component_count:
        raise SelectivePolicyError("selective policy calibration counts do not reconcile")
    embedded_candidate_ids = calibration.get("calibration_candidate_ids")
    if (
        not isinstance(embedded_candidate_ids, list)
        or not all(isinstance(candidate_id, str) and candidate_id for candidate_id in embedded_candidate_ids)
        or embedded_candidate_ids != sorted(set(embedded_candidate_ids))
        or len(embedded_candidate_ids) != component_count
        or embedded_candidate_ids != calibration_candidate_ids
    ):
        raise SelectivePolicyError(
            "selective policy calibration candidate IDs do not match the version-bound calibration cohort"
        )
    _validate_dimension_counts(
        calibration.get("evidence_dimension_counts"), component_count, "selective policy calibration"
    )
    independent_gate = _validate_qualification_gate(
        calibration.get("independent_component_gate"),
        label="selective policy calibration independent component gate",
        expected_observed=component_count,
        expected_minimum=config.min_independent_components,
    )
    accept_grid = _validate_threshold_grid(
        calibration.get("accept_threshold_grid"), config.max_thresholds_per_lane, "accept"
    )
    reject_grid = _validate_threshold_grid(
        calibration.get("reject_threshold_grid"), config.max_thresholds_per_lane, "reject"
    )
    accept_reports = calibration.get("accept_hypotheses")
    reject_reports = calibration.get("reject_hypotheses")
    if not isinstance(accept_reports, list) or len(accept_reports) != len(accept_grid):
        raise SelectivePolicyError("selective policy accept hypothesis reports do not match the threshold grid")
    if not isinstance(reject_reports, list) or len(reject_reports) != len(reject_grid):
        raise SelectivePolicyError("selective policy reject hypothesis reports do not match the threshold grid")

    hypotheses: list[tuple[str, float]] = []
    accept_by_id: dict[str, dict[str, Any]] = {}
    for index, (threshold, report) in enumerate(zip(accept_grid, accept_reports)):
        expected_report_fields = {
            "hypothesis_id",
            "threshold",
            "inferential_unit",
            "n",
            "selected_component_count",
            "selected_count",
            "error_count",
            "p_value",
        }
        if not isinstance(report, dict) or set(report) != expected_report_fields:
            raise SelectivePolicyError("selective policy accept hypothesis structure is invalid")
        hypothesis_id = f"accept-{index:02d}"
        if report.get("hypothesis_id") != hypothesis_id or report.get("inferential_unit") != INFERENTIAL_UNIT:
            raise SelectivePolicyError("selective policy accept hypothesis identity is invalid")
        if _bounded_probability(report.get("threshold"), "selective policy accept hypothesis threshold") != threshold:
            raise SelectivePolicyError("selective policy accept hypothesis threshold does not match its grid")
        selected_count = _qualification_count(
            report.get("selected_count"), "selective policy accept hypothesis selected count"
        )
        selected_component_count = _qualification_count(
            report.get("selected_component_count"),
            "selective policy accept hypothesis selected component count",
        )
        n = _qualification_count(report.get("n"), "selective policy accept hypothesis sample count")
        errors = _qualification_count(report.get("error_count"), "selective policy accept hypothesis error count")
        if selected_count != selected_component_count or n != selected_count or errors > n or n > component_count:
            raise SelectivePolicyError("selective policy accept hypothesis counts do not reconcile")
        expected_p = _binomial_lower_tail(errors, n, 1.0 - config.accept_precision_target) if n else 1.0
        p_value = _bounded_probability(report.get("p_value"), "selective policy accept hypothesis p-value")
        if p_value != expected_p:
            raise SelectivePolicyError("selective policy accept hypothesis p-value is invalid")
        accept_by_id[hypothesis_id] = report
        hypotheses.append((hypothesis_id, p_value))

    reject_by_id: dict[str, dict[str, Any]] = {}
    reject_sample_counts: set[int] = set()
    for index, (threshold, report) in enumerate(zip(reject_grid, reject_reports)):
        expected_report_fields = {
            "hypothesis_id",
            "threshold",
            "inferential_unit",
            "n",
            "true_ball_component_count",
            "selected_count",
            "true_ball_count",
            "error_count",
            "p_value",
        }
        if not isinstance(report, dict) or set(report) != expected_report_fields:
            raise SelectivePolicyError("selective policy reject hypothesis structure is invalid")
        hypothesis_id = f"reject-{index:02d}"
        if report.get("hypothesis_id") != hypothesis_id or report.get("inferential_unit") != INFERENTIAL_UNIT:
            raise SelectivePolicyError("selective policy reject hypothesis identity is invalid")
        if _bounded_probability(report.get("threshold"), "selective policy reject hypothesis threshold") != threshold:
            raise SelectivePolicyError("selective policy reject hypothesis threshold does not match its grid")
        selected_count = _qualification_count(
            report.get("selected_count"), "selective policy reject hypothesis selected count"
        )
        true_ball_count = _qualification_count(
            report.get("true_ball_count"), "selective policy reject hypothesis true-ball count"
        )
        true_ball_component_count = _qualification_count(
            report.get("true_ball_component_count"),
            "selective policy reject hypothesis true-ball component count",
        )
        n = _qualification_count(report.get("n"), "selective policy reject hypothesis sample count")
        errors = _qualification_count(report.get("error_count"), "selective policy reject hypothesis error count")
        if (
            n != true_ball_count
            or n != true_ball_component_count
            or errors > n
            or errors > selected_count
            or n > component_count
            or selected_count > component_count
        ):
            raise SelectivePolicyError("selective policy reject hypothesis counts do not reconcile")
        expected_p = _binomial_lower_tail(errors, n, config.false_reject_target) if n else 1.0
        p_value = _bounded_probability(report.get("p_value"), "selective policy reject hypothesis p-value")
        if p_value != expected_p:
            raise SelectivePolicyError("selective policy reject hypothesis p-value is invalid")
        reject_sample_counts.add(n)
        reject_by_id[hypothesis_id] = report
        hypotheses.append((hypothesis_id, p_value))
    if len(reject_sample_counts) > 1:
        raise SelectivePolicyError("selective policy reject hypotheses use inconsistent true-ball cohorts")

    component_hypothesis_count = _qualification_count(
        calibration.get("component_hypothesis_count"),
        "selective policy component hypothesis count",
    )
    if component_hypothesis_count != len(hypotheses):
        raise SelectivePolicyError("selective policy component hypothesis count is invalid")
    expected_rejected = _holm_rejections(hypotheses, alpha=config.fwer_alpha)
    rejected = calibration.get("holm_rejected_hypotheses")
    if rejected != sorted(expected_rejected):
        raise SelectivePolicyError("selective policy Holm rejected hypothesis set is invalid")
    family_payload = [{"id": identifier, "p_value": p_value} for identifier, p_value in sorted(hypotheses)]
    if calibration.get("hypothesis_family_sha256") != _canonical_sha256(family_payload):
        raise SelectivePolicyError("selective policy hypothesis family hash is invalid")
    minimum_samples = calibration.get("minimum_zero_error_samples")
    expected_minimum_samples = {
        "accept": _minimum_zero_error_sample(
            1.0 - config.accept_precision_target, config.fwer_alpha, max(1, len(hypotheses))
        ),
        "reject": _minimum_zero_error_sample(config.false_reject_target, config.fwer_alpha, max(1, len(hypotheses))),
    }
    if minimum_samples != expected_minimum_samples:
        raise SelectivePolicyError("selective policy minimum zero-error samples are invalid")

    pairs = calibration.get("certified_pairs")
    if not isinstance(pairs, list):
        raise SelectivePolicyError("selective policy certified pairs must be a list")
    expected_pair_ids = [
        (accept_report["hypothesis_id"], reject_report["hypothesis_id"])
        for accept_report in accept_reports
        if accept_report["hypothesis_id"] in expected_rejected
        for reject_report in reject_reports
        if reject_report["hypothesis_id"] in expected_rejected
        and accept_report["threshold"] + reject_report["threshold"] > 1.0
    ]
    actual_pair_ids: list[tuple[str, str]] = []
    for pair in pairs:
        expected_pair_fields = {
            "accept_hypothesis_id",
            "reject_hypothesis_id",
            "accept_threshold",
            "reject_threshold",
            "accepted_count",
            "automated_count",
            "accepted_component_count",
            "automated_component_count",
            "cluster_gate",
        }
        if not isinstance(pair, dict) or set(pair) != expected_pair_fields:
            raise SelectivePolicyError("selective policy certified pair structure is invalid")
        accept_id = pair.get("accept_hypothesis_id")
        reject_id = pair.get("reject_hypothesis_id")
        if accept_id not in expected_rejected or reject_id not in expected_rejected:
            raise SelectivePolicyError("selective policy certified pair references a non-rejected hypothesis")
        accept_report = accept_by_id.get(accept_id)
        reject_report = reject_by_id.get(reject_id)
        if accept_report is None or reject_report is None:
            raise SelectivePolicyError("selective policy certified pair references an unknown hypothesis")
        accept_threshold = _bounded_probability(
            pair.get("accept_threshold"), "selective policy certified pair accept threshold"
        )
        reject_threshold = _bounded_probability(
            pair.get("reject_threshold"), "selective policy certified pair reject threshold"
        )
        if (
            accept_threshold != accept_report["threshold"]
            or reject_threshold != reject_report["threshold"]
            or accept_threshold + reject_threshold <= 1.0
        ):
            raise SelectivePolicyError("selective policy certified pair thresholds are invalid")
        accepted_count = _qualification_count(
            pair.get("accepted_count"), "selective policy certified pair accepted count"
        )
        automated_count = _qualification_count(
            pair.get("automated_count"), "selective policy certified pair automated count"
        )
        accepted_component_count = _qualification_count(
            pair.get("accepted_component_count"),
            "selective policy certified pair accepted component count",
        )
        automated_component_count = _qualification_count(
            pair.get("automated_component_count"),
            "selective policy certified pair automated component count",
        )
        expected_accepted = accept_report["selected_count"]
        expected_automated = expected_accepted + reject_report["selected_count"]
        if (
            accepted_count != accepted_component_count
            or automated_count != automated_component_count
            or accepted_count != expected_accepted
            or automated_count != expected_automated
            or automated_count > component_count
        ):
            raise SelectivePolicyError("selective policy certified pair counts do not reconcile")
        _validate_cluster_gate(pair.get("cluster_gate"), config, "selective policy certified pair")
        actual_pair_ids.append((accept_id, reject_id))
    predeclared_pair_count = _qualification_count(
        calibration.get("predeclared_pair_count"), "selective policy predeclared pair count"
    )
    if actual_pair_ids != expected_pair_ids or predeclared_pair_count != len(pairs):
        raise SelectivePolicyError("selective policy certified pair family is incomplete or reordered")

    eligible_pairs = list(pairs) if independent_gate else []
    eligible_pairs.sort(
        key=lambda pair: (
            -pair["automated_count"],
            -pair["accepted_count"],
            pair["accept_threshold"],
            pair["reject_threshold"],
            pair["accept_hypothesis_id"],
            pair["reject_hypothesis_id"],
        )
    )
    selected = eligible_pairs[0] if eligible_pairs else None
    if calibration.get("selected_hypothesis") != selected:
        raise SelectivePolicyError("selective policy selected calibration hypothesis is invalid")
    expected_thresholds = {
        "accept": float(selected["accept_threshold"] if selected else 1.0),
        "reject": float(selected["reject_threshold"] if selected else 1.0),
    }
    if thresholds != expected_thresholds:
        raise SelectivePolicyError("selective policy thresholds do not match the selected calibration pair")
    certified = selected is not None
    if calibration.get("certified") is not certified or calibration.get("status") != (
        "certified" if certified else "insufficient_evidence"
    ):
        raise SelectivePolicyError("selective policy calibration certification is invalid")
    _validate_cluster_diagnostics(calibration.get("cluster_diagnostics"), "selective policy calibration")


def _validate_audit_semantics(
    audit: dict[str, Any],
    *,
    config: SelectivePolicyConfig,
    thresholds: dict[str, float],
    calibration_certified: bool,
    audit_component_count: int,
    audit_candidate_ids: list[str],
) -> None:
    expected_fields = {
        "status",
        "qualified",
        "qualification_scope",
        "inferential_unit",
        "inferential_unit_algorithm",
        "audit_component_count",
        "evidence_dimension_counts",
        "fixed_thresholds",
        "benchmark",
        "reconciled",
        "point_targets_passed",
        "one_sided_confidence",
        "sample_gates",
        "cluster_gate",
        "slices",
        "cluster_diagnostics",
    }
    if set(audit) != expected_fields:
        raise SelectivePolicyError("selective policy audit structure is invalid")
    if (
        audit.get("qualification_scope") != "fixed_aggregate_audit_cohort"
        or audit.get("inferential_unit") != INFERENTIAL_UNIT
        or audit.get("inferential_unit_algorithm") != INFERENTIAL_UNIT_ALGORITHM
        or audit.get("fixed_thresholds") != thresholds
    ):
        raise SelectivePolicyError("selective policy audit method, inferential unit, or thresholds are invalid")
    component_count = _qualification_count(audit.get("audit_component_count"), "selective policy audit component count")
    if component_count != audit_component_count:
        raise SelectivePolicyError("selective policy audit component count does not match the inferential unit")
    _validate_dimension_counts(audit.get("evidence_dimension_counts"), component_count, "selective policy audit")

    benchmark = audit.get("benchmark")
    if not isinstance(benchmark, dict):
        raise SelectivePolicyError("selective policy audit benchmark is missing")
    candidate_evaluations = benchmark.get("candidate_evaluations")
    frame_evaluations = benchmark.get("frame_evaluations")
    if not isinstance(candidate_evaluations, list) or not isinstance(frame_evaluations, list):
        raise SelectivePolicyError("selective policy audit benchmark evaluations are invalid")
    rebuilt_benchmark = _stable_diagnostic_identity(
        build_benchmark_report(
            candidate_evaluations=candidate_evaluations,
            frame_evaluations=frame_evaluations,
        )
    )
    if benchmark != rebuilt_benchmark or benchmark.get("validation_errors") != []:
        raise SelectivePolicyError("selective policy audit benchmark does not recompute exactly")
    benchmark_candidate_ids = [
        row.get("candidate_id") if isinstance(row, dict) else None for row in candidate_evaluations
    ]
    if (
        not all(isinstance(candidate_id, str) and candidate_id for candidate_id in benchmark_candidate_ids)
        or len(benchmark_candidate_ids) != len(set(benchmark_candidate_ids))
        or sorted(benchmark_candidate_ids) != audit_candidate_ids
    ):
        raise SelectivePolicyError("selective policy audit benchmark candidate IDs do not match the audit cohort")
    if len(candidate_evaluations) != component_count or any(
        not isinstance(row, dict) or row.get("truth_origin") != "human_confirmed" for row in candidate_evaluations
    ):
        raise SelectivePolicyError("selective policy audit benchmark is not the bound human-confirmed cohort")
    metrics = benchmark.get("metrics")
    if not isinstance(metrics, dict):
        raise SelectivePolicyError("selective policy audit benchmark metrics are missing")
    precision = metrics.get("auto_accepted_candidate_precision")
    false_reject = metrics.get("true_ball_false_reject_rate")
    if not isinstance(precision, dict) or not isinstance(false_reject, dict):
        raise SelectivePolicyError("selective policy audit endpoint metrics are missing")
    accepted_count = _qualification_count(
        precision.get("denominator"), "selective policy audit accepted component count"
    )
    accepted_correct = _qualification_count(precision.get("numerator"), "selective policy audit accepted correct count")
    true_ball_count = _qualification_count(
        false_reject.get("denominator"), "selective policy audit true-ball component count"
    )
    false_reject_errors = _qualification_count(
        false_reject.get("numerator"), "selective policy audit false-reject error count"
    )
    if accepted_correct > accepted_count or false_reject_errors > true_ball_count:
        raise SelectivePolicyError("selective policy audit endpoint counts are invalid")
    accept_errors = accepted_count - accepted_correct
    expected_precision = accepted_correct / accepted_count if accepted_count else None
    expected_false_reject = false_reject_errors / true_ball_count if true_ball_count else None
    if precision.get("value") != expected_precision or false_reject.get("value") != expected_false_reject:
        raise SelectivePolicyError("selective policy audit endpoint values do not reconcile")

    confidence = audit.get("one_sided_confidence")
    expected_confidence_fields = {
        "qualification_method",
        "inferential_unit",
        "accepted_component_count",
        "true_ball_component_count",
        "familywise_alpha",
        "per_endpoint_alpha",
        "accept_error_exact_upper",
        "false_reject_exact_upper",
        "accept_error_upper",
        "false_reject_upper",
        "wilson_is_diagnostic_only",
        "scope",
        "passed",
    }
    if not isinstance(confidence, dict) or set(confidence) != expected_confidence_fields:
        raise SelectivePolicyError("selective policy audit confidence structure is invalid")
    endpoint_alpha = config.fwer_alpha / 2.0
    if (
        confidence.get("qualification_method") != AUDIT_ALGORITHM
        or confidence.get("inferential_unit") != INFERENTIAL_UNIT
        or confidence.get("accepted_component_count") != accepted_count
        or confidence.get("true_ball_component_count") != true_ball_count
        or confidence.get("familywise_alpha") != config.fwer_alpha
        or confidence.get("per_endpoint_alpha") != endpoint_alpha
        or confidence.get("wilson_is_diagnostic_only") is not True
        or confidence.get("scope") != "fixed_aggregate_audit_cohort"
    ):
        raise SelectivePolicyError("selective policy audit confidence identity or counts are invalid")
    expected_accept_exact = _exact_binomial_upper_bound(accept_errors, accepted_count, alpha=endpoint_alpha)
    expected_reject_exact = _exact_binomial_upper_bound(false_reject_errors, true_ball_count, alpha=endpoint_alpha)
    expected_accept_wilson = _wilson_upper_bound(accept_errors, accepted_count, alpha=endpoint_alpha)
    expected_reject_wilson = _wilson_upper_bound(false_reject_errors, true_ball_count, alpha=endpoint_alpha)
    if (
        confidence.get("accept_error_exact_upper") != expected_accept_exact
        or confidence.get("false_reject_exact_upper") != expected_reject_exact
        or confidence.get("accept_error_upper") != expected_accept_wilson
        or confidence.get("false_reject_upper") != expected_reject_wilson
    ):
        raise SelectivePolicyError("selective policy audit confidence bounds do not recompute")
    point_passed = (
        expected_precision is not None
        and expected_precision >= config.accept_precision_target
        and expected_false_reject is not None
        and expected_false_reject <= config.false_reject_target
    )
    exact_passed = (
        expected_accept_exact is not None
        and expected_accept_exact <= 1.0 - config.accept_precision_target
        and expected_reject_exact is not None
        and expected_reject_exact <= config.false_reject_target
    )
    if audit.get("point_targets_passed") is not point_passed or confidence.get("passed") is not exact_passed:
        raise SelectivePolicyError("selective policy audit point or exact confidence result is invalid")

    sample_gates = audit.get("sample_gates")
    if not isinstance(sample_gates, dict) or set(sample_gates) != {
        "accepted_components",
        "true_ball_components",
        "independent_components",
    }:
        raise SelectivePolicyError("selective policy audit sample gate structure is invalid")
    accepted_gate = _validate_qualification_gate(
        sample_gates.get("accepted_components"),
        label="selective policy audit accepted component gate",
        expected_observed=accepted_count,
        expected_minimum=config.min_audit_accepted,
    )
    true_ball_gate = _validate_qualification_gate(
        sample_gates.get("true_ball_components"),
        label="selective policy audit true-ball component gate",
        expected_observed=true_ball_count,
        expected_minimum=config.min_audit_true_balls,
    )
    independent_gate = _validate_qualification_gate(
        sample_gates.get("independent_components"),
        label="selective policy audit independent component gate",
        expected_observed=component_count,
        expected_minimum=config.min_independent_components,
    )
    gates_passed = accepted_gate and true_ball_gate and independent_gate
    qualified = calibration_certified and point_passed and exact_passed and gates_passed
    expected_status = (
        "qualified" if qualified else ("failed" if gates_passed and not point_passed else "insufficient_evidence")
    )
    if audit.get("qualified") is not qualified or audit.get("status") != expected_status:
        raise SelectivePolicyError("selective policy audit qualification is invalid")
    if audit.get("reconciled") is not True:
        raise SelectivePolicyError("selective policy audit benchmark reconciliation is invalid")
    _validate_cluster_gate(audit.get("cluster_gate"), config, "selective policy audit")
    if not isinstance(audit.get("slices"), list):
        raise SelectivePolicyError("selective policy audit slices must be a list")
    _validate_cluster_diagnostics(audit.get("cluster_diagnostics"), "selective policy audit")


def _validate_threshold_grid(value: Any, limit: int, lane: str) -> list[float]:
    if not isinstance(value, list) or not value or len(value) > limit:
        raise SelectivePolicyError(f"selective policy {lane} threshold grid is invalid")
    result = [_bounded_probability(item, f"selective policy {lane} threshold") for item in value]
    if result != sorted(set(result)):
        raise SelectivePolicyError(f"selective policy {lane} threshold grid is not sorted and unique")
    return result


def _bounded_probability(value: Any, label: str) -> float:
    result = _finite_number(value, label)
    if not 0.0 <= result <= 1.0:
        raise SelectivePolicyError(f"{label} must be between zero and one")
    return result


def _validate_dimension_counts(value: Any, component_count: int, label: str) -> None:
    expected_fields = {"variant_id", "video_sha256", "group_id", "split_group", "temporal_group"}
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise SelectivePolicyError(f"{label} evidence dimension counts are invalid")
    for name, count in value.items():
        parsed = _qualification_count(count, f"{label} {name} count")
        if parsed > component_count:
            raise SelectivePolicyError(f"{label} {name} count exceeds the component count")


def _validate_cluster_gate(value: Any, config: SelectivePolicyConfig, label: str) -> None:
    expected_fields = {
        "method",
        "purpose",
        "affects_qualification",
        "qualification_scope",
        "per_cluster_statistical_guarantee",
        "minimum_accepted_per_cluster",
        "minimum_true_balls_per_cluster",
        "failed_clusters",
        "passed",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise SelectivePolicyError(f"{label} cluster gate structure is invalid")
    failed = value.get("failed_clusters")
    if not isinstance(failed, list) or not all(isinstance(item, str) and item for item in failed):
        raise SelectivePolicyError(f"{label} cluster gate failed clusters are invalid")
    if (
        value.get("method") != "heterogeneity_descriptive_diagnostic_v2"
        or value.get("purpose") != "diagnostic_only"
        or value.get("affects_qualification") is not False
        or value.get("qualification_scope") != "fixed_aggregate_audit_cohort"
        or value.get("per_cluster_statistical_guarantee") != "none"
        or value.get("minimum_accepted_per_cluster") != config.min_cluster_accepted
        or value.get("minimum_true_balls_per_cluster") != config.min_cluster_true_balls
        or value.get("passed") is not (not failed)
    ):
        raise SelectivePolicyError(f"{label} cluster gate is invalid")


def _validate_cluster_diagnostics(value: Any, label: str) -> None:
    expected_fields = {"video_sha256", "group_id", "split_group", "temporal_group"}
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise SelectivePolicyError(f"{label} cluster diagnostics are invalid")
    if not all(isinstance(items, list) for items in value.values()):
        raise SelectivePolicyError(f"{label} cluster diagnostics entries must be lists")


def _validate_qualification_evidence(
    evidence: Any,
    *,
    config: SelectivePolicyConfig,
    inferential_unit: dict[str, Any],
    calibration: dict[str, Any],
    qualification: dict[str, Any],
) -> None:
    expected_fields = {"calibration", "audit", "aggregate"}
    if not isinstance(evidence, dict) or set(evidence) != expected_fields:
        raise SelectivePolicyError("selective policy qualification evidence has invalid fields")

    calibration_evidence = evidence.get("calibration")
    expected_calibration_fields = {
        "status",
        "certified",
        "calibration_component_count",
        "independent_component_gate",
        "selected_hypothesis_present",
    }
    if not isinstance(calibration_evidence, dict) or set(calibration_evidence) != expected_calibration_fields:
        raise SelectivePolicyError("selective policy calibration qualification evidence has invalid fields")
    calibration_count = _qualification_count(
        calibration_evidence.get("calibration_component_count"),
        "selective policy calibration qualification component count",
    )
    if calibration_count != inferential_unit["calibration_component_count"]:
        raise SelectivePolicyError("selective policy calibration qualification component count mismatch")
    calibration_gate = _validate_qualification_gate(
        calibration_evidence.get("independent_component_gate"),
        label="selective policy calibration independent component gate",
        expected_observed=calibration_count,
        expected_minimum=config.min_independent_components,
    )
    calibration_certified = calibration_evidence.get("certified")
    selected_hypothesis_present = calibration_evidence.get("selected_hypothesis_present")
    if not isinstance(calibration_certified, bool) or not isinstance(selected_hypothesis_present, bool):
        raise SelectivePolicyError("selective policy calibration qualification flags must be booleans")
    calibration_status = calibration_evidence.get("status")
    if calibration_status not in {"certified", "insufficient_evidence"}:
        raise SelectivePolicyError("selective policy calibration qualification evidence status is invalid")
    if calibration_certified != (calibration_status == "certified"):
        raise SelectivePolicyError("selective policy calibration qualification evidence status disagrees")
    if calibration_certified != (calibration_gate and selected_hypothesis_present):
        raise SelectivePolicyError("selective policy calibration qualification evidence is inconsistent")
    if (
        calibration.get("status") != calibration_status
        or calibration.get("certified") is not calibration_certified
        or calibration.get("calibration_component_count") != calibration_count
        or calibration.get("independent_component_gate") != calibration_evidence["independent_component_gate"]
        or (calibration.get("selected_hypothesis") is not None) is not selected_hypothesis_present
    ):
        raise SelectivePolicyError("selective policy calibration does not match qualification evidence")

    audit_evidence = evidence.get("audit")
    expected_audit_fields = {
        "status",
        "qualified",
        "audit_component_count",
        "accepted_component_count",
        "true_ball_component_count",
        "sample_gates",
        "point_targets_passed",
        "exact_confidence_passed",
    }
    if not isinstance(audit_evidence, dict) or set(audit_evidence) != expected_audit_fields:
        raise SelectivePolicyError("selective policy audit qualification evidence has invalid fields")
    audit_count = _qualification_count(
        audit_evidence.get("audit_component_count"),
        "selective policy audit qualification component count",
    )
    accepted_count = _qualification_count(
        audit_evidence.get("accepted_component_count"),
        "selective policy audit accepted component count",
    )
    true_ball_count = _qualification_count(
        audit_evidence.get("true_ball_component_count"),
        "selective policy audit true-ball component count",
    )
    if audit_count != inferential_unit["audit_component_count"]:
        raise SelectivePolicyError("selective policy audit qualification component count mismatch")
    if accepted_count > audit_count or true_ball_count > audit_count:
        raise SelectivePolicyError("selective policy audit qualification counts exceed the audit component count")
    sample_gates = audit_evidence.get("sample_gates")
    expected_gate_names = {"accepted_components", "true_ball_components", "independent_components"}
    if not isinstance(sample_gates, dict) or set(sample_gates) != expected_gate_names:
        raise SelectivePolicyError("selective policy audit sample gates have invalid fields")
    accepted_gate = _validate_qualification_gate(
        sample_gates.get("accepted_components"),
        label="selective policy audit accepted component gate",
        expected_observed=accepted_count,
        expected_minimum=config.min_audit_accepted,
    )
    true_ball_gate = _validate_qualification_gate(
        sample_gates.get("true_ball_components"),
        label="selective policy audit true-ball component gate",
        expected_observed=true_ball_count,
        expected_minimum=config.min_audit_true_balls,
    )
    independent_gate = _validate_qualification_gate(
        sample_gates.get("independent_components"),
        label="selective policy audit independent component gate",
        expected_observed=audit_count,
        expected_minimum=config.min_independent_components,
    )
    point_targets_passed = audit_evidence.get("point_targets_passed")
    exact_confidence_passed = audit_evidence.get("exact_confidence_passed")
    audit_qualified = audit_evidence.get("qualified")
    if not all(isinstance(value, bool) for value in (point_targets_passed, exact_confidence_passed, audit_qualified)):
        raise SelectivePolicyError("selective policy audit qualification flags must be booleans")
    gates_passed = accepted_gate and true_ball_gate and independent_gate
    expected_audit_qualified = (
        calibration_certified and point_targets_passed and exact_confidence_passed and gates_passed
    )
    if audit_qualified is not expected_audit_qualified:
        raise SelectivePolicyError("selective policy audit qualification evidence is inconsistent")
    expected_audit_status = (
        "qualified"
        if audit_qualified
        else ("failed" if gates_passed and not point_targets_passed else "insufficient_evidence")
    )
    if audit_evidence.get("status") != expected_audit_status:
        raise SelectivePolicyError("selective policy audit qualification evidence status is inconsistent")

    aggregate = evidence.get("aggregate")
    expected_aggregate_fields = {"qualified", "policy_status", "acceptance_status"}
    if not isinstance(aggregate, dict) or set(aggregate) != expected_aggregate_fields:
        raise SelectivePolicyError("selective policy aggregate qualification evidence has invalid fields")
    aggregate_qualified = aggregate.get("qualified")
    if not isinstance(aggregate_qualified, bool):
        raise SelectivePolicyError("selective policy aggregate qualification flag must be boolean")
    expected_aggregate_qualified = calibration_certified and audit_qualified
    expected_policy_status = "qualified" if expected_aggregate_qualified else "review_only"
    expected_acceptance_status = (
        "qualified"
        if expected_aggregate_qualified
        else (
            "insufficient_evidence"
            if calibration_status == "insufficient_evidence" or expected_audit_status == "insufficient_evidence"
            else "failed"
        )
    )
    if (
        aggregate_qualified is not expected_aggregate_qualified
        or aggregate.get("policy_status") != expected_policy_status
        or aggregate.get("acceptance_status") != expected_acceptance_status
    ):
        raise SelectivePolicyError("selective policy aggregate qualification evidence is inconsistent")
    if (
        qualification.get("calibration_status") != calibration_status
        or qualification.get("calibration_certified") is not calibration_certified
        or qualification.get("audit_status") != expected_audit_status
        or qualification.get("audit_qualified") is not audit_qualified
        or qualification.get("qualified") is not aggregate_qualified
        or qualification.get("policy_status") != expected_policy_status
        or qualification.get("acceptance_status") != expected_acceptance_status
    ):
        raise SelectivePolicyError("selective policy qualification evidence does not reconcile with qualification")
    if aggregate_qualified and not (
        calibration_count >= config.min_independent_components
        and accepted_count >= config.min_audit_accepted
        and true_ball_count >= config.min_audit_true_balls
        and audit_count >= config.min_independent_components
        and calibration_gate
        and gates_passed
        and point_targets_passed
        and exact_confidence_passed
    ):
        raise SelectivePolicyError("qualified selective policy does not satisfy qualification evidence minima")


def _qualification_count(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SelectivePolicyError(f"{label} must be a non-negative integer")
    return value


def _validate_qualification_gate(
    value: Any,
    *,
    label: str,
    expected_observed: int,
    expected_minimum: int,
) -> bool:
    if not isinstance(value, dict) or set(value) != {"observed", "minimum", "passed"}:
        raise SelectivePolicyError(f"{label} has invalid fields")
    observed = _qualification_count(value.get("observed"), f"{label} observed")
    minimum = _qualification_count(value.get("minimum"), f"{label} minimum")
    passed = value.get("passed")
    if not isinstance(passed, bool):
        raise SelectivePolicyError(f"{label} passed must be boolean")
    if observed != expected_observed or minimum != expected_minimum:
        raise SelectivePolicyError(f"{label} does not match the bound counts or config")
    if passed is not (observed >= minimum):
        raise SelectivePolicyError(f"{label} result is inconsistent")
    return passed


def _validate_acceptance_report_payload(policy: dict[str, Any], report: dict[str, Any]) -> None:
    for field in (
        "policy_version",
        "version_inputs",
        "inferential_unit",
        "evaluation_cohorts",
        "qualification_evidence",
        "decisions_artifact",
        "lineage",
        "targets",
    ):
        if report.get(field) != policy.get(field):
            raise SelectivePolicyError(f"selective acceptance report {field} does not match policy")
    if report.get("policy_status") != policy.get("status"):
        raise SelectivePolicyError("selective acceptance report policy status mismatch")
    qualification = policy["version_inputs"]["qualification"]
    if report.get("status") != qualification.get("acceptance_status"):
        raise SelectivePolicyError("selective acceptance report qualification status mismatch")
    if _canonical_sha256(report.get("calibration")) != policy["version_inputs"].get("calibration_sha256"):
        raise SelectivePolicyError("selective acceptance report calibration mismatch")
    stable_report_audit = _stable_diagnostic_identity(report.get("audit"))
    if _canonical_sha256(stable_report_audit) != policy["version_inputs"].get("audit_sha256"):
        raise SelectivePolicyError("selective acceptance report audit mismatch")
    if stable_report_audit != policy.get("audit"):
        raise SelectivePolicyError("selective acceptance report audit does not match embedded policy audit")
    audit = report.get("audit")
    if not isinstance(audit, dict) or audit.get("audit_component_count") != policy["inferential_unit"].get(
        "audit_component_count"
    ):
        raise SelectivePolicyError("selective acceptance report audit component count mismatch")
    if _qualification_evidence_summary(report.get("calibration"), stable_report_audit) != policy.get(
        "qualification_evidence"
    ):
        raise SelectivePolicyError("selective acceptance report qualification evidence mismatch")


def _validate_decisions_binding_payload(
    policy: dict[str, Any], decisions: dict[str, Any], decisions_snapshot: _Snapshot
) -> None:
    descriptor = policy.get("decisions_artifact")
    if not isinstance(descriptor, dict) or set(descriptor) != {"path", "sha256", "content_sha256"}:
        raise SelectivePolicyError("selective policy decisions_artifact binding is invalid")
    if descriptor.get("path") != SELECTIVE_DECISIONS_NAME:
        raise SelectivePolicyError("selective policy decisions artifact path is unsafe")
    if descriptor.get("sha256") != decisions_snapshot.sha256:
        raise SelectivePolicyError("selective decisions artifact sha256 mismatch")
    if (
        decisions.get("schema_version") != POLICY_SCHEMA_VERSION
        or decisions.get("artifact_type") != "selective_decisions"
    ):
        raise SelectivePolicyError("invalid selective decisions envelope")
    if decisions.get("decision_algorithm") != DECISION_ALGORITHM:
        raise SelectivePolicyError("selective decisions algorithm is incompatible")
    content_sha256 = _canonical_sha256(_normalized_decisions_content(decisions))
    if descriptor.get("content_sha256") != content_sha256:
        raise SelectivePolicyError("selective decisions content sha256 mismatch")
    version_inputs = policy.get("version_inputs")
    if not isinstance(version_inputs, dict) or version_inputs.get("decisions_content_sha256") != content_sha256:
        raise SelectivePolicyError("selective decisions content is not bound by policy version_inputs")
    if decisions.get("policy_version") != policy.get("policy_version"):
        raise SelectivePolicyError("selective decisions policy_version mismatch")
    if decisions.get("status") != policy.get("status") or decisions.get("lineage") != policy.get("lineage"):
        raise SelectivePolicyError("selective decisions status or lineage mismatch")
    _validate_decision_cohort_partition(policy, decisions)
    _validate_decision_rows_semantics(policy, decisions)
    _validate_decisions_summary(decisions)


def _validate_decision_cohort_partition(policy: dict[str, Any], decisions: dict[str, Any]) -> None:
    cohorts = policy["evaluation_cohorts"]
    calibration_ids = set(cohorts["calibration_candidate_ids"])
    audit_ids = set(cohorts["audit_candidate_ids"])
    application_ids = set(cohorts["application_candidate_ids"])
    expected_roles = {
        **{candidate_id: "policy_calibration" for candidate_id in calibration_ids},
        **{candidate_id: "policy_audit" for candidate_id in audit_ids},
    }
    rows = decisions.get("decisions")
    if not isinstance(rows, list):
        raise SelectivePolicyError("selective decisions rows must be a list")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise SelectivePolicyError("selective decision row must be an object")
        candidate_id = row.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id or candidate_id != candidate_id.strip():
            raise SelectivePolicyError("selective decision candidate_id is invalid")
        if candidate_id in seen:
            raise SelectivePolicyError(f"duplicate selective decision candidate_id {candidate_id!r}")
        seen.add(candidate_id)
        expected_role = expected_roles.get(candidate_id)
        if expected_role is not None:
            forced_reasons = row.get("forced_abstain_reasons")
            if (
                row.get("policy_role") != expected_role
                or row.get("decision_scope") != "evaluation_only"
                or row.get("decision") != "abstain"
                or row.get("applied_to_contract") is not False
                or not isinstance(forced_reasons, list)
                or "evaluation_holdout" not in forced_reasons
            ):
                raise SelectivePolicyError(
                    f"selective decision evaluation cohort invariants failed for {candidate_id!r}"
                )
        elif candidate_id in application_ids:
            if row.get("policy_role") is not None or row.get("decision_scope") != "application":
                raise SelectivePolicyError(f"application selective decision must have no policy role: {candidate_id!r}")
        else:
            raise SelectivePolicyError(
                f"selective decision candidate is outside the version-bound population: {candidate_id!r}"
            )
    expected_population = set(expected_roles) | application_ids
    missing = sorted(expected_population - seen)
    unexpected = sorted(seen - expected_population)
    if missing or unexpected:
        raise SelectivePolicyError(
            f"selective decisions do not match the version-bound population: missing={missing}, unexpected={unexpected}"
        )


def _validate_decisions_summary(decisions: dict[str, Any]) -> None:
    rows = decisions.get("decisions")
    if not isinstance(rows, list):
        raise SelectivePolicyError("selective decisions rows must be a list")
    for row in rows:
        if not isinstance(row, dict):
            raise SelectivePolicyError("selective decision row must be an object")
        if row.get("decision") not in {"accept", "reject", "abstain"}:
            raise SelectivePolicyError("selective decision value is invalid")
        forced_reasons = row.get("forced_abstain_reasons")
        if not isinstance(forced_reasons, list) or not all(
            isinstance(reason, str) and reason for reason in forced_reasons
        ):
            raise SelectivePolicyError("selective decision forced abstain reasons are invalid")
        if not isinstance(row.get("applied_to_contract"), bool) or not isinstance(
            row.get("existing_decision_preserved"), bool
        ):
            raise SelectivePolicyError("selective decision application flags must be booleans")
    expected_summary = {
        "candidate_count": len(rows),
        "accept_count": sum(row["decision"] == "accept" for row in rows),
        "reject_count": sum(row["decision"] == "reject" for row in rows),
        "abstain_count": sum(row["decision"] == "abstain" for row in rows),
        "forced_abstain_count": sum(bool(row["forced_abstain_reasons"]) for row in rows),
        "evaluation_holdout_count": sum(row.get("decision_scope") == "evaluation_only" for row in rows),
        "application_count": sum(row.get("decision_scope") == "application" for row in rows),
        "preserved_existing_decision_count": sum(row["existing_decision_preserved"] for row in rows),
        "pending_application_count": sum(
            row.get("decision_scope") == "application"
            and not row["applied_to_contract"]
            and not row["existing_decision_preserved"]
            for row in rows
        ),
    }
    summary = decisions.get("summary")
    if (
        not isinstance(summary, dict)
        or set(summary) != set(expected_summary)
        or not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in summary.values())
        or summary != expected_summary
    ):
        raise SelectivePolicyError("selective decisions summary does not recompute exactly")


def _validate_decision_rows_semantics(policy: dict[str, Any], decisions: dict[str, Any]) -> None:
    rows = decisions.get("decisions")
    if not isinstance(rows, list):
        raise SelectivePolicyError("selective decisions rows must be a list")
    candidate_ids = [row.get("candidate_id") for row in rows if isinstance(row, dict)]
    if len(candidate_ids) != len(rows) or candidate_ids != sorted(candidate_ids):
        raise SelectivePolicyError("selective decision rows must be sorted by candidate_id")

    config = _validated_policy_config(policy)
    thresholds = policy["thresholds"]
    qualified = policy.get("status") == "qualified"
    for row in rows:
        if set(row) != DECISION_ROW_FIELDS:
            raise SelectivePolicyError("selective decision row fields do not match the production schema")
        candidate_id = _required_text(row.get("candidate_id"), "selective decision candidate_id")
        _required_sha256(row.get("candidate_fingerprint"), f"selective decision {candidate_id} fingerprint")
        _required_text(row.get("variant_id"), f"selective decision {candidate_id} variant_id")
        frame_index = row.get("frame_index")
        if not isinstance(frame_index, int) or isinstance(frame_index, bool) or frame_index < 0:
            raise SelectivePolicyError(f"selective decision {candidate_id} frame_index is invalid")

        accept_score = _decision_probability(row.get("accept_score"), candidate_id, "accept_score")
        reject_score = _decision_probability(row.get("reject_score"), candidate_id, "reject_score")
        unknown_score = _decision_probability(row.get("unknown_score"), candidate_id, "unknown_score")
        if not math.isclose(accept_score + reject_score + unknown_score, 1.0, abs_tol=1e-6):
            raise SelectivePolicyError(f"selective decision {candidate_id} lane scores do not sum to one")
        top_label = row.get("top_label")
        if top_label not in CLASSIFICATION_LABELS:
            raise SelectivePolicyError(f"selective decision {candidate_id} top_label is invalid")
        top_margin = _decision_probability(row.get("top_margin"), candidate_id, "top_margin")
        if top_label == "match_ball" and accept_score < unknown_score:
            raise SelectivePolicyError(f"selective decision {candidate_id} top_label contradicts its scores")
        if top_label == "unknown" and unknown_score <= accept_score:
            raise SelectivePolicyError(f"selective decision {candidate_id} top_label contradicts its scores")
        if top_label in NOISE_LABELS and reject_score < max(accept_score, unknown_score):
            raise SelectivePolicyError(f"selective decision {candidate_id} top_label contradicts its scores")

        scope = row.get("decision_scope")
        role = row.get("policy_role")
        if scope not in {"application", "evaluation_only"}:
            raise SelectivePolicyError(f"selective decision {candidate_id} scope is invalid")
        if role not in {None, "policy_calibration", "policy_audit"}:
            raise SelectivePolicyError(f"selective decision {candidate_id} policy_role is invalid")
        evaluation_holdout = scope == "evaluation_only"

        reasons = row.get("forced_abstain_reasons")
        if (
            not isinstance(reasons, list)
            or not all(isinstance(reason, str) and reason in DECISION_FORCED_REASONS for reason in reasons)
            or reasons != sorted(set(reasons))
        ):
            raise SelectivePolicyError(f"selective decision {candidate_id} forced abstain reasons are invalid")
        reason_set = set(reasons)
        expected_computable_reasons: set[str] = set()
        if top_label == "unknown":
            expected_computable_reasons.add("top_unknown")
        if top_margin < config.min_top_margin:
            expected_computable_reasons.add("top_margin_below_minimum")
        if abs(accept_score - reject_score) < config.conflict_margin:
            expected_computable_reasons.add("accept_reject_conflict_margin")
        if unknown_score >= config.max_unknown_probability:
            expected_computable_reasons.add("unknown_probability_too_high")
        if evaluation_holdout:
            expected_computable_reasons.add("evaluation_holdout")
        computable_reason_names = {
            "top_unknown",
            "top_margin_below_minimum",
            "accept_reject_conflict_margin",
            "unknown_probability_too_high",
            "evaluation_holdout",
        }
        if reason_set & computable_reason_names != expected_computable_reasons:
            raise SelectivePolicyError(
                f"selective decision {candidate_id} forced abstain reasons do not recompute from row fields"
            )
        if {"confirmed_conflict", "confirmed_unknown"} <= reason_set:
            raise SelectivePolicyError(f"selective decision {candidate_id} has contradictory confirmed reasons")

        preserved = row.get("existing_decision_preserved")
        applied = row.get("applied_to_contract")
        if not isinstance(preserved, bool) or not isinstance(applied, bool):
            raise SelectivePolicyError(f"selective decision {candidate_id} application flags must be booleans")
        existing_reasons = reason_set & {"existing_decision", "conflicting_existing_decisions"}
        if len(existing_reasons) > 1 or preserved != (len(existing_reasons) == 1):
            raise SelectivePolicyError(
                f"selective decision {candidate_id} existing-decision reason does not match preservation flag"
            )

        raw_decision = row.get("raw_decision")
        decision = row.get("decision")
        if raw_decision not in {"accept", "reject", "abstain"} or decision not in {
            "accept",
            "reject",
            "abstain",
        }:
            raise SelectivePolicyError(f"selective decision {candidate_id} decision value is invalid")
        if evaluation_holdout or reasons:
            expected_raw = "abstain"
        else:
            accept = accept_score >= thresholds["accept"]
            reject = reject_score >= thresholds["reject"]
            if accept and reject:
                raise SelectivePolicyError(f"selective decision {candidate_id} passes contradictory thresholds")
            expected_raw = "accept" if accept else "reject" if reject else "abstain"
        if raw_decision != expected_raw:
            raise SelectivePolicyError(f"selective decision {candidate_id} raw_decision does not recompute")
        expected_decision = expected_raw if qualified else "abstain"
        if decision != expected_decision:
            raise SelectivePolicyError(f"selective decision {candidate_id} decision does not match policy status")
        expected_applied = (
            qualified and scope == "application" and expected_decision in {"accept", "reject"} and not preserved
        )
        if applied is not expected_applied:
            raise SelectivePolicyError(f"selective decision {candidate_id} applied_to_contract is invalid")


def _validated_policy_config(policy: dict[str, Any]) -> SelectivePolicyConfig:
    config = policy.get("version_inputs", {}).get("config")
    if not isinstance(config, dict):
        raise SelectivePolicyError("selective policy normalized config is missing")
    try:
        return _validate_config(SelectivePolicyConfig(**config))
    except (TypeError, ValueError, SelectivePolicyError) as exc:
        raise SelectivePolicyError(f"selective policy normalized config is invalid: {exc}") from exc


def _decision_probability(value: Any, candidate_id: str, field: str) -> float:
    number = _finite_number(value, f"selective decision {candidate_id} {field}")
    if not 0.0 <= number <= 1.0:
        raise SelectivePolicyError(f"selective decision {candidate_id} {field} must be between zero and one")
    return number


def _validate_decision_rows_against_evidence(
    policy: dict[str, Any],
    decisions: dict[str, Any],
    rows: list[dict[str, Any]],
    resolved_contract: dict[str, Any],
) -> None:
    _validate_existing_decision_evidence(rows, resolved_contract)
    expected_rows, _derived_contract = _apply_policy(
        rows,
        resolved_contract,
        thresholds=policy["thresholds"],
        qualified=policy.get("status") == "qualified",
        config=_validated_policy_config(policy),
    )
    if decisions.get("decisions") != expected_rows:
        raise SelectivePolicyError(
            "selective decision rows do not match authoritative evidence and the resolved contract"
        )


def _validate_existing_decision_evidence(rows: list[dict[str, Any]], resolved_contract: dict[str, Any]) -> None:
    contract_decisions = resolved_contract.get("decisions")
    if not isinstance(contract_decisions, list):
        raise SelectivePolicyError("resolved tracking contract decisions are missing")
    decision_counts: dict[str, int] = {}
    for decision in contract_decisions:
        if not isinstance(decision, dict):
            raise SelectivePolicyError("resolved tracking contract decision must be an object")
        candidate_id = _required_text(decision.get("candidate_id"), "resolved contract decision candidate_id")
        decision_counts[candidate_id] = decision_counts.get(candidate_id, 0) + 1

    for row in rows:
        if not isinstance(row, dict):
            raise SelectivePolicyError("authoritative selective decision evidence row must be an object")
        candidate_id = _required_text(row.get("candidate_id"), "authoritative evidence candidate_id")
        count = decision_counts.get(candidate_id, 0)
        expected_reason = (
            {"existing_decision"} if count == 1 else {"conflicting_existing_decisions"} if count > 1 else set()
        )
        base_reasons = row.get("base_forced_reasons")
        if not isinstance(base_reasons, list):
            raise SelectivePolicyError(f"authoritative evidence {candidate_id} base forced reasons are invalid")
        observed_reasons = set(base_reasons) & {"existing_decision", "conflicting_existing_decisions"}
        if row.get("has_existing_decision") is not bool(count) or observed_reasons != expected_reason:
            raise SelectivePolicyError(
                f"authoritative evidence {candidate_id} does not match existing decisions in the resolved contract"
            )


def _validate_config(config: SelectivePolicyConfig) -> SelectivePolicyConfig:
    probability_fields = {
        "accept_precision_target": config.accept_precision_target,
        "false_reject_target": config.false_reject_target,
        "fwer_alpha": config.fwer_alpha,
        "min_top_margin": config.min_top_margin,
        "conflict_margin": config.conflict_margin,
        "max_unknown_probability": config.max_unknown_probability,
    }
    for name, value in probability_fields.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise SelectivePolicyError(f"{name} must be finite")
    if not 0.98 <= config.accept_precision_target < 1.0:
        raise SelectivePolicyError("accept_precision_target cannot be lower than 0.98")
    if not 0.0 < config.false_reject_target <= 0.01 or not 0.0 < config.fwer_alpha < 0.5:
        raise SelectivePolicyError("false_reject_target cannot exceed 0.01 and alpha must be below 0.5")
    for name in ("min_top_margin", "conflict_margin", "max_unknown_probability"):
        if not 0.0 <= float(getattr(config, name)) <= 1.0:
            raise SelectivePolicyError(f"{name} must be between 0 and 1")
    for name in (
        "max_thresholds_per_lane",
        "min_audit_accepted",
        "min_audit_true_balls",
        "min_independent_components",
        "min_cluster_accepted",
        "min_cluster_true_balls",
    ):
        value = getattr(config, name)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise SelectivePolicyError(f"{name} must be a positive integer")
    if config.max_thresholds_per_lane > 64:
        raise SelectivePolicyError("max_thresholds_per_lane cannot exceed 64")
    if config.min_independent_components < 3:
        raise SelectivePolicyError("min_independent_components cannot be lower than 3")
    if config.min_cluster_accepted < 30 or config.min_cluster_true_balls < 100:
        raise SelectivePolicyError("cluster support requirements cannot be lower than 30 accepted and 100 true balls")
    return config


def _validate_lineage(
    *,
    predictions: dict[str, Any],
    predictions_snapshot: _Snapshot,
    dataset: dict[str, Any],
    dataset_snapshot: _Snapshot,
    resolution: dict[str, Any],
    resolution_snapshot: _Snapshot,
    contract: dict[str, Any],
    contract_snapshot: _Snapshot,
    model_manifest: dict[str, Any],
    model_snapshot: _Snapshot,
    training_report: dict[str, Any],
    training_snapshot: _Snapshot,
    policy_roles: dict[str, Any] | None,
    policy_roles_snapshot: _Snapshot | None,
    weights_snapshot: _Snapshot,
) -> dict[str, Any]:
    if predictions.get("schema_version") != "1.0" or predictions.get("artifact_type") != "candidate_predictions":
        raise SelectivePolicyError("invalid candidate predictions envelope")
    if dataset.get("schema_version") != "1.0" or dataset.get("artifact_type") != "candidate_dataset":
        raise SelectivePolicyError("invalid candidate dataset envelope")
    dataset_summary = dataset.get("summary")
    if (
        not isinstance(dataset_summary, dict)
        or dataset_summary.get("status") != "ok"
        or not isinstance(dataset.get("samples"), list)
    ):
        raise SelectivePolicyError("candidate dataset is incomplete")
    if not isinstance(dataset.get("sources"), list) or not dataset["sources"]:
        raise SelectivePolicyError("candidate dataset sources are missing")
    if (
        resolution.get("schema_version") != "1.0"
        or resolution.get("artifact_type") != "candidate_annotation_resolution"
    ):
        raise SelectivePolicyError("invalid annotation resolution envelope")
    resolution_summary = resolution.get("summary")
    if (
        not isinstance(resolution_summary, dict)
        or resolution_summary.get("status") != "complete"
        or not isinstance(resolution.get("resolutions"), list)
    ):
        raise SelectivePolicyError("annotation resolution is incomplete")
    if (
        model_manifest.get("schema_version") != "1.0"
        or model_manifest.get("artifact_type") != "candidate_classifier_model"
    ):
        raise SelectivePolicyError("invalid model manifest envelope")
    if (
        training_report.get("schema_version") != "1.0"
        or training_report.get("artifact_type") != "candidate_classifier_training_report"
        or training_report.get("status") != "complete"
    ):
        raise SelectivePolicyError("invalid training report envelope")
    if model_manifest.get("training_report_path") != "training_report.v1.json":
        raise SelectivePolicyError("model manifest training report path is unsafe")
    if model_manifest.get("training_report_sha256") != training_snapshot.sha256:
        raise SelectivePolicyError("model manifest training report sha256 mismatch")
    if (
        model_manifest.get("weights_path") != "model.pt"
        or model_manifest.get("weights_sha256") != weights_snapshot.sha256
    ):
        raise SelectivePolicyError("model manifest weights sha256 mismatch")
    for field in (
        "model_version",
        "class_order",
        "supported_classes",
        "supported_mask",
        "calibration",
        "data_binding",
        "training_config",
    ):
        if model_manifest.get(field) != training_report.get(field):
            raise SelectivePolicyError(f"model manifest and training report disagree on {field}")
    if model_manifest.get("class_order") != list(CLASSIFICATION_LABELS):
        raise SelectivePolicyError("model class order does not match V2")
    supported_mask = model_manifest.get("supported_mask")
    if (
        not isinstance(supported_mask, list)
        or len(supported_mask) != len(CLASSIFICATION_LABELS)
        or not all(isinstance(value, bool) for value in supported_mask)
    ):
        raise SelectivePolicyError("model supported_mask is invalid")
    expected_supported = [label for label, supported in zip(CLASSIFICATION_LABELS, supported_mask) if supported]
    if model_manifest.get("supported_classes") != expected_supported:
        raise SelectivePolicyError("model supported classes do not match supported_mask")
    model_version_inputs = {
        "weights_sha256": weights_snapshot.sha256,
        "data_binding": model_manifest.get("data_binding"),
        "training_config": model_manifest.get("training_config"),
        "calibration": model_manifest.get("calibration"),
        "supported_mask": supported_mask,
        "class_order": model_manifest.get("class_order"),
        "architecture": model_manifest.get("architecture"),
        "input_contract": model_manifest.get("input_contract"),
        "code_sha256": model_manifest.get("code_sha256"),
        "runtime": model_manifest.get("runtime"),
    }
    if _canonical_sha256(model_version_inputs) != model_manifest.get("model_version"):
        raise SelectivePolicyError("model_version does not match the bound package")
    model_version = _required_text(model_manifest.get("model_version"), "model_version")
    dataset_version = _required_text(dataset.get("dataset_version"), "dataset_version")
    if predictions.get("model_version") != model_version or predictions.get("dataset_version") != dataset_version:
        raise SelectivePolicyError("predictions model/dataset version mismatch")
    if predictions.get("class_order") != list(CLASSIFICATION_LABELS):
        raise SelectivePolicyError("prediction class order does not match V2")
    prediction_temperature = _finite_number(predictions.get("temperature"), "prediction temperature")
    model_temperature = _positive_number(model_manifest.get("calibration", {}).get("temperature"), "model temperature")
    if prediction_temperature != model_temperature:
        raise SelectivePolicyError("prediction temperature does not match model calibration")

    dataset_contract = dataset.get("contract")
    source_contract_sha = dataset_contract.get("sha256") if isinstance(dataset_contract, dict) else None
    source_contract_sha = _required_sha256(source_contract_sha, "dataset source contract sha256")
    if predictions.get("source_contract_sha256") != source_contract_sha:
        raise SelectivePolicyError("predictions source contract does not match dataset")
    if resolution.get("source_contract", {}).get("sha256") != source_contract_sha:
        raise SelectivePolicyError("annotation source contract does not match dataset")
    dataset_binding = resolution.get("source_dataset_manifest")
    if not isinstance(dataset_binding, dict):
        raise SelectivePolicyError("annotation resolution lacks dataset binding")
    if (
        dataset_binding.get("sha256") != dataset_snapshot.sha256
        or dataset_binding.get("dataset_version") != dataset_version
    ):
        raise SelectivePolicyError("annotation resolution dataset binding mismatch")
    if resolution.get("derived_tracking_contract", {}).get("sha256") != contract_snapshot.sha256:
        raise SelectivePolicyError("annotation derived V2 sha256 mismatch")
    data_binding = model_manifest.get("data_binding")
    if not isinstance(data_binding, dict):
        raise SelectivePolicyError("model package lacks training data binding")
    _required_text(data_binding.get("dataset_version"), "model training dataset_version")
    for key in ("dataset_manifest_sha256", "annotation_resolution_sha256", "resolved_contract_sha256"):
        _required_sha256(data_binding.get(key), f"model training {key}")
    split = training_report.get("split")
    if not isinstance(split, dict) or split.get("leakage_checks") != {"passed": True, "violations": []}:
        raise SelectivePolicyError("training report split leakage checks did not pass")
    expected_role_binding = _policy_role_lineage(
        predictions_snapshot=predictions_snapshot,
        dataset_snapshot=dataset_snapshot,
        resolution_snapshot=resolution_snapshot,
        contract_snapshot=contract_snapshot,
        model_snapshot=model_snapshot,
        training_snapshot=training_snapshot,
        weights_snapshot=weights_snapshot,
        dataset_version=dataset_version,
        model_version=model_version,
    )
    if policy_roles is not None:
        if policy_roles_snapshot is None:
            raise SelectivePolicyError("selective policy roles snapshot is missing")
        _validate_policy_roles_envelope(policy_roles, expected_lineage=expected_role_binding)
    elif policy_roles_snapshot is not None:
        raise SelectivePolicyError("selective policy roles payload is missing")

    lineage = {
        "predictions": {"path": predictions_snapshot.path.name, "sha256": predictions_snapshot.sha256},
        "dataset_manifest": {"path": dataset_snapshot.path.name, "sha256": dataset_snapshot.sha256},
        "annotation_resolution": {"path": resolution_snapshot.path.name, "sha256": resolution_snapshot.sha256},
        "resolved_tracking_contract": {"path": contract_snapshot.path.name, "sha256": contract_snapshot.sha256},
        "model_manifest": {"path": model_snapshot.path.name, "sha256": model_snapshot.sha256},
        "training_report": {"path": training_snapshot.path.name, "sha256": training_snapshot.sha256},
        "model_weights": {"path": weights_snapshot.path.name, "sha256": weights_snapshot.sha256},
        "source_contract_sha256": source_contract_sha,
        "dataset_version": dataset_version,
        "model_version": model_version,
    }
    if policy_roles_snapshot is not None:
        lineage["policy_roles"] = {
            "path": policy_roles_snapshot.path.name,
            "sha256": policy_roles_snapshot.sha256,
        }
    return lineage


def _policy_role_lineage(
    *,
    predictions_snapshot: _Snapshot,
    dataset_snapshot: _Snapshot,
    resolution_snapshot: _Snapshot,
    contract_snapshot: _Snapshot,
    model_snapshot: _Snapshot,
    training_snapshot: _Snapshot,
    weights_snapshot: _Snapshot,
    dataset_version: str,
    model_version: str,
) -> dict[str, Any]:
    return {
        "predictions_sha256": predictions_snapshot.sha256,
        "dataset_manifest_sha256": dataset_snapshot.sha256,
        "annotation_resolution_sha256": resolution_snapshot.sha256,
        "resolved_contract_sha256": contract_snapshot.sha256,
        "model_manifest_sha256": model_snapshot.sha256,
        "training_report_sha256": training_snapshot.sha256,
        "model_weights_sha256": weights_snapshot.sha256,
        "dataset_version": dataset_version,
        "model_version": model_version,
    }


def _validate_policy_roles_envelope(policy_roles: dict[str, Any], *, expected_lineage: dict[str, Any]) -> None:
    if policy_roles.get("schema_version") != "1.0" or policy_roles.get("artifact_type") != "selective_policy_roles":
        raise SelectivePolicyError("invalid selective policy roles envelope")
    if policy_roles.get("lineage") != expected_lineage:
        raise SelectivePolicyError("selective policy roles lineage mismatch")
    roles = policy_roles.get("roles")
    if not isinstance(roles, dict):
        raise SelectivePolicyError("selective policy roles are missing")
    for role_name in ("policy_calibration", "policy_audit"):
        role_ids = roles.get(role_name)
        if (
            not isinstance(role_ids, list)
            or not role_ids
            or not all(isinstance(candidate_id, str) and candidate_id for candidate_id in role_ids)
        ):
            raise SelectivePolicyError(f"{role_name} must be a non-empty candidate id list")
        if len(role_ids) != len(set(role_ids)):
            raise SelectivePolicyError(f"{role_name} contains duplicate candidates")
    if set(roles["policy_calibration"]) & set(roles["policy_audit"]):
        raise SelectivePolicyError("policy calibration/audit roles overlap")
    if policy_roles.get("assignment_strategy") != ROLE_ASSIGNMENT_ALGORITHM:
        raise SelectivePolicyError("selective policy roles require deterministic connected-component assignment")
    if policy_roles.get("assignment_seed") != POLICY_ROLE_SEED:
        raise SelectivePolicyError("selective policy assignment_seed is not the predeclared v1 seed")
    if policy_roles.get("component_id_algorithm") != ROLE_COMPONENT_ID_ALGORITHM:
        raise SelectivePolicyError("selective policy component_id algorithm is incompatible")
    if policy_roles.get("inferential_unit") != INFERENTIAL_UNIT:
        raise SelectivePolicyError("selective policy inferential unit is incompatible")
    if policy_roles.get("inferential_unit_algorithm") != INFERENTIAL_UNIT_ALGORITHM:
        raise SelectivePolicyError("selective policy inferential unit algorithm is incompatible")
    components = policy_roles.get("components")
    mapping = policy_roles.get("candidate_component_mapping")
    if not isinstance(components, list) or not components:
        raise SelectivePolicyError("selective policy connected evidence components are missing")
    if not isinstance(mapping, list) or not mapping:
        raise SelectivePolicyError("selective policy candidate/component mapping is missing")
    component_by_id: dict[str, dict[str, Any]] = {}
    expected_mapping: list[dict[str, str]] = []
    for index, component in enumerate(components):
        if not isinstance(component, dict):
            raise SelectivePolicyError(f"selective policy component {index} must be an object")
        component_id = _required_sha256(component.get("component_id"), f"policy component {index} id")
        if component_id in component_by_id:
            raise SelectivePolicyError("selective policy component ids must be unique")
        component_by_id[component_id] = component
        candidate_ids = component.get("candidate_ids")
        role = component.get("role")
        if not isinstance(candidate_ids, list) or len(candidate_ids) != 1:
            raise SelectivePolicyError(
                "each connected evidence component must contain exactly one human-confirmed evaluation candidate"
            )
        candidate_id = _required_text(candidate_ids[0], f"policy component {component_id} candidate_id")
        if role not in {"policy_calibration", "policy_audit"}:
            raise SelectivePolicyError("selective policy component role is invalid")
        expected_mapping.append({"candidate_id": candidate_id, "component_id": component_id, "role": role})
    expected_mapping.sort(key=lambda row: row["candidate_id"])
    if mapping != expected_mapping:
        raise SelectivePolicyError("selective policy candidate/component mapping is not the exact component inverse")
    mapped_ids = [row["candidate_id"] for row in expected_mapping]
    mapped_components = [row["component_id"] for row in expected_mapping]
    if len(mapped_ids) != len(set(mapped_ids)) or len(mapped_components) != len(set(mapped_components)):
        raise SelectivePolicyError("selective policy candidate/component mapping must be one-to-one")
    expected_roles = {
        role: sorted(row["candidate_id"] for row in expected_mapping if row["role"] == role)
        for role in ("policy_calibration", "policy_audit")
    }
    if {role: sorted(candidate_ids) for role, candidate_ids in roles.items()} != expected_roles:
        raise SelectivePolicyError("selective policy roles do not match candidate/component mapping")
    if policy_roles.get("component_count") != len(components):
        raise SelectivePolicyError("selective policy component_count does not match components")
    if policy_roles.get("evaluation_candidate_count") != len(mapping):
        raise SelectivePolicyError("selective policy evaluation_candidate_count does not match mapping")


def _evaluation_rows(
    predictions: dict[str, Any],
    dataset: dict[str, Any],
    resolution: dict[str, Any],
    contract: dict[str, Any],
    policy_roles: dict[str, Any] | None,
    *,
    supported_mask: list[bool],
) -> list[dict[str, Any]]:
    samples = _unique_by(dataset["samples"], "candidate_id", "dataset samples")
    candidates = _unique_by(contract["candidates"], "candidate_id", "contract candidates")
    resolutions = _unique_by(resolution["resolutions"], "candidate_id", "annotation resolutions")
    raw_predictions = predictions.get("predictions")
    if not isinstance(raw_predictions, list) or predictions.get("prediction_count") != len(raw_predictions):
        raise SelectivePolicyError("prediction_count does not match predictions")
    prediction_by_id = _unique_by(raw_predictions, "candidate_id", "candidate predictions")
    expected_ids = set(samples)
    for name, mapping in (
        ("contract", candidates),
        ("annotation resolution", resolutions),
        ("predictions", prediction_by_id),
    ):
        if set(mapping) != expected_ids:
            raise SelectivePolicyError(f"dataset/{name} candidate ids do not match exactly")
    sources = _unique_by(dataset["sources"], "variant_id", "dataset sources")
    source_binding: dict[str, str] = {}
    for variant_id, source in sources.items():
        _required_sha256(source.get("sha256"), f"source {variant_id} sha256")
        for key in ("group_id", "split_group", "temporal_group"):
            _required_text(source.get(key), f"source {variant_id} {key}")
        candidate_ids = source.get("candidate_ids")
        if not isinstance(candidate_ids, list) or not candidate_ids:
            raise SelectivePolicyError(f"source {variant_id!r} candidate_ids must be non-empty")
        for candidate_id in candidate_ids:
            candidate_id = _required_text(candidate_id, f"source {variant_id} candidate_id")
            if candidate_id in source_binding:
                raise SelectivePolicyError(f"candidate {candidate_id!r} is bound to multiple sources")
            source_binding[candidate_id] = variant_id
    if set(source_binding) != expected_ids:
        raise SelectivePolicyError("dataset source bindings do not match candidate ids")
    roles = policy_roles["roles"] if policy_roles is not None else {}
    calibration_ids = set(roles.get("policy_calibration", []))
    audit_ids = set(roles.get("policy_audit", []))
    if not calibration_ids | audit_ids <= expected_ids:
        raise SelectivePolicyError("selective policy roles reference absent candidates")

    confirmed: dict[str, list[dict[str, Any]]] = {}
    for row in contract["classifications"]:
        if row.get("label_origin") in CONFIRMED_LABEL_ORIGINS:
            confirmed.setdefault(row["candidate_id"], []).append(row)
    existing_decisions: dict[str, list[dict[str, Any]]] = {}
    for row in contract["decisions"]:
        existing_decisions.setdefault(row["candidate_id"], []).append(row)

    rows: list[dict[str, Any]] = []
    for candidate_id in sorted(expected_ids):
        sample = samples[candidate_id]
        candidate = candidates[candidate_id]
        source = sources.get(sample.get("variant_id"))
        if source is None:
            raise SelectivePolicyError(f"sample {candidate_id!r} references absent source")
        if source_binding[candidate_id] != source["variant_id"]:
            raise SelectivePolicyError(f"sample/source binding mismatch for {candidate_id!r}")
        for key in ("group_id", "split_group", "temporal_group"):
            if sample.get(key) != source.get(key):
                raise SelectivePolicyError(f"sample/source {key} mismatch for {candidate_id!r}")
        fingerprint = _candidate_fingerprint(candidate, sample, source)
        prediction = prediction_by_id[candidate_id]
        probabilities = _validate_prediction(
            prediction,
            model_version=predictions["model_version"],
            supported_mask=supported_mask,
        )
        if prediction.get("candidate_fingerprint") != fingerprint:
            raise SelectivePolicyError(f"candidate fingerprint mismatch for {candidate_id!r}")
        resolution_row = resolutions[candidate_id]
        confirmed_rows = confirmed.get(candidate_id, [])
        confirmed_labels = {row["label"] for row in confirmed_rows}
        has_human_confirmed_truth = any(
            row.get("label_origin") == "human_confirmed" and row.get("label") == resolution_row.get("label")
            for row in confirmed_rows
        )
        forced: list[str] = []
        truth: str | None = None
        truth_origin: str | None = None
        if len(confirmed_labels) > 1 or resolution_row.get("status") == "existing_confirmed_conflict":
            forced.append("confirmed_conflict")
        elif (
            resolution_row.get("status") == "confirmed"
            and resolution_row.get("label_origin") in CONFIRMED_LABEL_ORIGINS
            and len(confirmed_labels) == 1
            and resolution_row.get("label") in confirmed_labels
        ):
            label = resolution_row["label"]
            truth_origin = resolution_row["label_origin"]
            if label == "match_ball":
                truth = "match_ball"
            elif label in NOISE_LABELS:
                truth = "noise"
            elif label == "unknown":
                forced.append("confirmed_unknown")
        if existing_decisions.get(candidate_id):
            forced.append(
                "existing_decision" if len(existing_decisions[candidate_id]) == 1 else "conflicting_existing_decisions"
            )
        ordered_probabilities = sorted(
            ((value, CLASSIFICATION_LABELS.index(label), label) for label, value in probabilities.items()),
            key=lambda item: (-item[0], item[1]),
        )
        top_score, _, top_label = ordered_probabilities[0]
        top_margin = top_score - ordered_probabilities[1][0]
        accept_score = probabilities["match_ball"]
        reject_score = sum(probabilities[label] for label in NOISE_LABELS)
        unknown_score = probabilities["unknown"]
        if top_label == "unknown":
            forced.append("top_unknown")
        if top_margin < 0:
            raise SelectivePolicyError("negative top margin is impossible")
        policy_role = (
            "policy_calibration"
            if candidate_id in calibration_ids
            else "policy_audit"
            if candidate_id in audit_ids
            else None
        )
        if policy_role is not None and (truth_origin != "human_confirmed" or not has_human_confirmed_truth):
            raise SelectivePolicyError(f"{policy_role} candidate {candidate_id!r} requires human_confirmed truth")
        if policy_role is not None and truth is None:
            raise SelectivePolicyError(f"{policy_role} candidate {candidate_id!r} lacks confirmed binary truth")
        rows.append(
            {
                "candidate_id": candidate_id,
                "candidate_fingerprint": fingerprint,
                "variant_id": source["variant_id"],
                "group_id": source.get("group_id"),
                "split_group": source.get("split_group"),
                "temporal_group": source.get("temporal_group"),
                "video_sha256": source.get("sha256"),
                "frame_index": candidate["frame_index"],
                "accept_score": accept_score,
                "reject_score": reject_score,
                "unknown_score": unknown_score,
                "top_label": top_label,
                "top_margin": top_margin,
                "truth": truth,
                "truth_origin": truth_origin,
                "policy_role": policy_role,
                "base_forced_reasons": sorted(set(forced)),
                "has_existing_decision": bool(existing_decisions.get(candidate_id)),
            }
        )
    evaluation_rows = [
        row for row in rows if row["truth"] in {"match_ball", "noise"} and row["truth_origin"] == "human_confirmed"
    ]
    if policy_roles is not None:
        if any(row["policy_role"] is None for row in evaluation_rows):
            missing = sorted(row["candidate_id"] for row in evaluation_rows if row["policy_role"] is None)
            raise SelectivePolicyError(f"policy roles must cover every confirmed binary candidate: {missing[:5]}")
        normalized_role_ids, expected_components, expected_mapping = _build_deterministic_role_assignment(
            evaluation_rows
        )
        if {role: sorted(candidate_ids) for role, candidate_ids in roles.items()} != normalized_role_ids:
            raise SelectivePolicyError("policy role candidate ids do not match recomputed components")
        if policy_roles.get("components") != expected_components:
            raise SelectivePolicyError("policy role component manifest does not match recomputed evidence")
        if policy_roles.get("candidate_component_mapping") != expected_mapping:
            raise SelectivePolicyError("policy role candidate/component mapping does not match recomputed evidence")
        component_by_candidate = {row["candidate_id"]: row["component_id"] for row in expected_mapping}
        for row in evaluation_rows:
            row["component_id"] = component_by_candidate[row["candidate_id"]]
    return rows


def _validate_prediction(
    prediction: dict[str, Any],
    *,
    model_version: str,
    supported_mask: list[bool],
) -> dict[str, float]:
    if not isinstance(prediction, dict) or prediction.get("model_version") != model_version:
        raise SelectivePolicyError("prediction model_version mismatch")
    probabilities = prediction.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != set(CLASSIFICATION_LABELS):
        raise SelectivePolicyError("prediction probabilities must use the exact V2 class set")
    result: dict[str, float] = {}
    for label in CLASSIFICATION_LABELS:
        value = probabilities[label]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise SelectivePolicyError("prediction probabilities must be finite")
        if not 0.0 <= float(value) <= 1.0:
            raise SelectivePolicyError("prediction probabilities must be normalized")
        result[label] = float(value)
    unsupported = [
        label
        for label, supported in zip(CLASSIFICATION_LABELS, supported_mask)
        if not supported and result[label] != 0.0
    ]
    if unsupported:
        raise SelectivePolicyError(f"prediction probability for unsupported model class must be zero: {unsupported}")
    if not math.isclose(sum(result.values()), 1.0, abs_tol=1e-6):
        raise SelectivePolicyError("prediction probabilities must sum to one")
    predicted_label = max(CLASSIFICATION_LABELS, key=lambda label: (result[label], -CLASSIFICATION_LABELS.index(label)))
    if prediction.get("predicted_label") != predicted_label:
        raise SelectivePolicyError("predicted_label does not match probabilities")
    confidence = prediction.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not math.isclose(float(confidence), result[predicted_label], abs_tol=1e-8)
    ):
        raise SelectivePolicyError("prediction confidence does not match top probability")
    return result


def _candidate_fingerprint(candidate: dict[str, Any], sample: dict[str, Any], source: dict[str, Any]) -> str:
    candidate_id = candidate["candidate_id"]
    bbox = _finite_vector(candidate.get("bbox"), 4, f"candidate {candidate_id} bbox")
    sample_bbox = _finite_vector(sample.get("bbox_requested_pixels"), 4, f"sample {candidate_id} bbox")
    identity = {
        "candidate_id": candidate_id,
        "frame_index": candidate.get("frame_index"),
        "bbox": bbox,
        "detector_source": candidate.get("source"),
        "confidence": _finite_number(candidate.get("confidence"), "candidate confidence"),
    }
    sample_identity = {
        "candidate_id": sample.get("candidate_id"),
        "frame_index": sample.get("frame_index"),
        "bbox": sample_bbox,
        "detector_source": sample.get("detector_source"),
        "confidence": _finite_number(sample.get("confidence"), "sample confidence"),
    }
    if identity != sample_identity:
        raise SelectivePolicyError(f"dataset/contract identity mismatch for {candidate_id!r}")
    width = _positive_number(source.get("width"), "source width")
    height = _positive_number(source.get("height"), "source height")
    expected_clamped = [
        min(width, max(0.0, bbox[0])),
        min(height, max(0.0, bbox[1])),
        min(width, max(0.0, bbox[2])),
        min(height, max(0.0, bbox[3])),
    ]
    if _finite_vector(sample.get("bbox_clamped_pixels"), 4, "clamped bbox") != expected_clamped:
        raise SelectivePolicyError(f"clamped bbox mismatch for {candidate_id!r}")
    expected_normalized = [
        expected_clamped[0] / width,
        expected_clamped[1] / height,
        expected_clamped[2] / width,
        expected_clamped[3] / height,
    ]
    normalized = _finite_vector(sample.get("bbox_normalized"), 4, "normalized bbox")
    if any(not math.isclose(left, right, abs_tol=1e-8) for left, right in zip(normalized, expected_normalized)):
        raise SelectivePolicyError(f"normalized bbox mismatch for {candidate_id!r}")
    return _canonical_sha256(identity)


def _policy_components(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidate_ids = [row["candidate_id"] for row in rows]
    parent = {candidate_id: candidate_id for candidate_id in candidate_ids}

    def find(candidate_id: str) -> str:
        while parent[candidate_id] != candidate_id:
            parent[candidate_id] = parent[parent[candidate_id]]
            candidate_id = parent[candidate_id]
        return candidate_id

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    first_by_evidence: dict[tuple[str, str], str] = {}
    evidence_keys = ("variant_id", "video_sha256", "group_id", "split_group", "temporal_group")
    for row in rows:
        for key in evidence_keys:
            value = row[key]
            prior = first_by_evidence.setdefault((key, value), row["candidate_id"])
            union(prior, row["candidate_id"])
    members: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        members.setdefault(find(row["candidate_id"]), []).append(row)
    plural = {
        "variant_id": "variant_ids",
        "video_sha256": "video_sha256",
        "group_id": "group_ids",
        "split_group": "split_groups",
        "temporal_group": "temporal_groups",
    }
    result = []
    for component_rows in members.values():
        evidence = {plural[key]: sorted({row[key] for row in component_rows}) for key in evidence_keys}
        immutable_identity = {"video_sha256": evidence["video_sha256"]}
        result.append(
            {
                "component_id": _canonical_sha256(immutable_identity),
                "candidate_ids": sorted(row["candidate_id"] for row in component_rows),
                "evidence": evidence,
            }
        )
    return sorted(result, key=lambda component: component["component_id"])


def _deterministic_policy_role(seed: str, component_id: str) -> str:
    digest = hashlib.sha256(f"{seed}:{component_id}".encode("utf-8")).digest()
    return "policy_calibration" if digest[0] < 128 else "policy_audit"


def _build_deterministic_role_assignment(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, list[str]], list[dict[str, Any]], list[dict[str, str]]]:
    roles: dict[str, list[str]] = {"policy_calibration": [], "policy_audit": []}
    assigned_components: list[dict[str, Any]] = []
    candidate_component_mapping: list[dict[str, str]] = []
    for component in _policy_components(rows):
        if len(component["candidate_ids"]) != 1:
            raise SelectivePolicyError(
                "each connected evidence component must contain exactly one human-confirmed evaluation candidate"
            )
        role = _deterministic_policy_role(POLICY_ROLE_SEED, component["component_id"])
        candidate_id = component["candidate_ids"][0]
        roles[role].append(candidate_id)
        assigned_components.append({**component, "role": role})
        candidate_component_mapping.append(
            {
                "candidate_id": candidate_id,
                "component_id": component["component_id"],
                "role": role,
            }
        )
    normalized_roles = {role: sorted(candidate_ids) for role, candidate_ids in roles.items()}
    candidate_component_mapping.sort(key=lambda row: row["candidate_id"])
    return normalized_roles, assigned_components, candidate_component_mapping


def _validated_evaluation_cohorts(
    rows: list[dict[str, Any]], training_report: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    calibration_rows = [row for row in rows if row["policy_role"] == "policy_calibration" and row["truth"] is not None]
    audit_rows = [row for row in rows if row["policy_role"] == "policy_audit" and row["truth"] is not None]
    _require_binary_truth(calibration_rows, "policy calibration")
    _require_binary_truth(audit_rows, "policy audit")
    _require_one_candidate_per_component(calibration_rows, "policy calibration")
    _require_one_candidate_per_component(audit_rows, "policy audit")
    if {row["component_id"] for row in calibration_rows} & {row["component_id"] for row in audit_rows}:
        raise SelectivePolicyError("policy calibration/audit connected evidence components overlap")
    _validate_disjoint_evidence(training_report, rows, calibration_rows, audit_rows)
    return calibration_rows, audit_rows


def _validate_disjoint_evidence(
    training_report: dict[str, Any],
    population_rows: list[dict[str, Any]],
    calibration_rows: list[dict[str, Any]],
    audit_rows: list[dict[str, Any]],
) -> None:
    key_pairs = (
        ("candidate_id", "candidate_ids"),
        ("variant_id", "variant_ids"),
        ("video_sha256", "video_sha256"),
        ("group_id", "group_ids"),
        ("split_group", "split_groups"),
        ("temporal_group", "temporal_groups"),
    )
    evidence_by_split = training_report.get("split", {}).get("evidence_by_split")
    if not isinstance(evidence_by_split, dict):
        raise SelectivePolicyError("model training split evidence_by_split is missing")
    model_evidence: dict[str, set[str]] = {plural: set() for _, plural in key_pairs}
    for split_name in ("train", "calibration", "test"):
        split_evidence = evidence_by_split.get(split_name)
        if not isinstance(split_evidence, dict):
            raise SelectivePolicyError(f"model {split_name} split evidence is missing")
        for _, plural in key_pairs:
            values = split_evidence.get(plural)
            if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
                raise SelectivePolicyError(f"model split evidence {plural} must be string lists")
            model_evidence[plural].update(values)
    for key, plural in key_pairs:
        calibration_values = {row[key] for row in calibration_rows}
        audit_values = {row[key] for row in audit_rows}
        if calibration_values & audit_values:
            raise SelectivePolicyError(f"policy calibration/audit leakage in {key}")
        population_values = {row[key] for row in population_rows}
        if population_values & model_evidence[plural]:
            raise SelectivePolicyError(f"policy population leaks model training evidence in {key}")


def _fit_thresholds(
    rows: list[dict[str, Any]], config: SelectivePolicyConfig
) -> tuple[dict[str, float], dict[str, Any]]:
    calibration_component_count = _require_one_candidate_per_component(rows, "policy calibration exact tests")
    accept_grid = _threshold_grid([row["accept_score"] for row in rows], config.max_thresholds_per_lane)
    reject_grid = _threshold_grid([row["reject_score"] for row in rows], config.max_thresholds_per_lane)
    hypotheses: list[tuple[str, float]] = []
    accept_reports: list[dict[str, Any]] = []
    reject_reports: list[dict[str, Any]] = []
    accept_error_target = 1.0 - config.accept_precision_target
    for index, accept_threshold in enumerate(accept_grid):
        accepted = [row for row in rows if not _forced_reasons(row, config) and row["accept_score"] >= accept_threshold]
        errors = sum(row["truth"] != "match_ball" for row in accepted)
        p_value = _binomial_lower_tail(errors, len(accepted), accept_error_target) if accepted else 1.0
        hypothesis_id = f"accept-{index:02d}"
        hypotheses.append((hypothesis_id, p_value))
        accept_reports.append(
            {
                "hypothesis_id": hypothesis_id,
                "threshold": accept_threshold,
                "inferential_unit": INFERENTIAL_UNIT,
                "n": len(accepted),
                "selected_component_count": len(accepted),
                "selected_count": len(accepted),
                "error_count": errors,
                "p_value": p_value,
            }
        )
    true_balls = [row for row in rows if row["truth"] == "match_ball"]
    for index, reject_threshold in enumerate(reject_grid):
        rejected_rows = [
            row for row in rows if not _forced_reasons(row, config) and row["reject_score"] >= reject_threshold
        ]
        errors = sum(row["truth"] == "match_ball" for row in rejected_rows)
        p_value = _binomial_lower_tail(errors, len(true_balls), config.false_reject_target) if true_balls else 1.0
        hypothesis_id = f"reject-{index:02d}"
        hypotheses.append((hypothesis_id, p_value))
        reject_reports.append(
            {
                "hypothesis_id": hypothesis_id,
                "threshold": reject_threshold,
                "inferential_unit": INFERENTIAL_UNIT,
                "n": len(true_balls),
                "true_ball_component_count": len(true_balls),
                "selected_count": len(rejected_rows),
                "true_ball_count": len(true_balls),
                "error_count": errors,
                "p_value": p_value,
            }
        )
    rejected_hypotheses = _holm_rejections(hypotheses, alpha=config.fwer_alpha)
    certified_accept = [row for row in accept_reports if row["hypothesis_id"] in rejected_hypotheses]
    certified_reject = [row for row in reject_reports if row["hypothesis_id"] in rejected_hypotheses]
    pair_reports = []
    for accept_report in certified_accept:
        for reject_report in certified_reject:
            accept_threshold = accept_report["threshold"]
            reject_threshold = reject_report["threshold"]
            if accept_threshold + reject_threshold <= 1.0:
                continue
            decisions = [_raw_decision(row, accept_threshold, reject_threshold, config) for row in rows]
            pair_report = {
                "accept_hypothesis_id": accept_report["hypothesis_id"],
                "reject_hypothesis_id": reject_report["hypothesis_id"],
                "accept_threshold": accept_threshold,
                "reject_threshold": reject_threshold,
                "accepted_count": sum(decision == "accept" for decision in decisions),
                "automated_count": sum(decision != "abstain" for decision in decisions),
                "accepted_component_count": sum(decision == "accept" for decision in decisions),
                "automated_component_count": sum(decision != "abstain" for decision in decisions),
            }
            pair_report["cluster_gate"] = _cluster_gate(
                rows,
                {"accept": accept_threshold, "reject": reject_threshold},
                config,
            )
            pair_reports.append(pair_report)
    evidence_dimension_counts = _independent_counts(rows)
    independent_component_gate = calibration_component_count >= config.min_independent_components
    certified = [report for report in pair_reports if independent_component_gate]
    certified.sort(
        key=lambda report: (
            -report["automated_count"],
            -report["accepted_count"],
            report["accept_threshold"],
            report["reject_threshold"],
            report["accept_hypothesis_id"],
            report["reject_hypothesis_id"],
        )
    )
    chosen = certified[0] if certified else None
    thresholds = {
        "accept": float(chosen["accept_threshold"] if chosen else 1.0),
        "reject": float(chosen["reject_threshold"] if chosen else 1.0),
    }
    family_payload = [{"id": identifier, "p_value": p_value} for identifier, p_value in sorted(hypotheses)]
    minimum_accept = _minimum_zero_error_sample(accept_error_target, config.fwer_alpha, max(1, len(hypotheses)))
    minimum_reject = _minimum_zero_error_sample(config.false_reject_target, config.fwer_alpha, max(1, len(hypotheses)))
    status = "certified" if chosen else "insufficient_evidence"
    return thresholds, {
        "status": status,
        "certified": chosen is not None,
        "method": THRESHOLD_ALGORITHM,
        "inferential_unit": INFERENTIAL_UNIT,
        "inferential_unit_algorithm": INFERENTIAL_UNIT_ALGORITHM,
        "calibration_count": len(rows),
        "calibration_component_count": calibration_component_count,
        "calibration_candidate_ids": sorted(row["candidate_id"] for row in rows),
        "evidence_dimension_counts": evidence_dimension_counts,
        "independent_component_gate": {
            "observed": calibration_component_count,
            "minimum": config.min_independent_components,
            "passed": independent_component_gate,
        },
        "accept_threshold_grid": accept_grid,
        "reject_threshold_grid": reject_grid,
        "predeclared_pair_count": len(pair_reports),
        "component_hypothesis_count": len(hypotheses),
        "holm_rejected_hypotheses": sorted(rejected_hypotheses),
        "minimum_zero_error_samples": {"accept": minimum_accept, "reject": minimum_reject},
        "selected_hypothesis": chosen,
        "accept_hypotheses": accept_reports,
        "reject_hypotheses": reject_reports,
        "certified_pairs": pair_reports,
        "cluster_diagnostics": _cluster_diagnostics(rows, thresholds, config),
        "hypothesis_family_sha256": _canonical_sha256(family_payload),
    }


def _threshold_grid(values: list[float], limit: int) -> list[float]:
    unique = sorted(set(float(value) for value in values))
    if len(unique) <= limit:
        return unique
    if limit == 1:
        return [unique[-1]]
    indices = {round(index * (len(unique) - 1) / (limit - 1)) for index in range(limit)}
    return [unique[index] for index in sorted(indices)]


def _audit_fixed_policy(
    rows: list[dict[str, Any]],
    thresholds: dict[str, float],
    calibration_certified: bool,
    config: SelectivePolicyConfig,
) -> dict[str, Any]:
    audit_component_count = _require_one_candidate_per_component(rows, "policy audit exact tests")
    evaluations = []
    for row in rows:
        decision = _raw_decision(row, thresholds["accept"], thresholds["reject"], config)
        evaluations.append(
            {
                "candidate_id": row["candidate_id"],
                "truth": row["truth"],
                "truth_origin": row["truth_origin"],
                "decision": decision,
                "confidence": max(row["accept_score"], row["reject_score"]),
            }
        )
    benchmark = build_benchmark_report(candidate_evaluations=evaluations)
    if benchmark["validation_errors"]:
        raise SelectivePolicyError(f"audit benchmark is invalid: {benchmark['validation_errors']}")
    precision = benchmark["metrics"]["auto_accepted_candidate_precision"]
    false_reject = benchmark["metrics"]["true_ball_false_reject_rate"]
    accept_errors = precision["denominator"] - precision["numerator"]
    joint_endpoint_alpha = config.fwer_alpha / 2.0
    accept_exact_upper = _exact_binomial_upper_bound(
        accept_errors, precision["denominator"], alpha=joint_endpoint_alpha
    )
    reject_exact_upper = _exact_binomial_upper_bound(
        false_reject["numerator"], false_reject["denominator"], alpha=joint_endpoint_alpha
    )
    accept_upper = _wilson_upper_bound(accept_errors, precision["denominator"], alpha=joint_endpoint_alpha)
    reject_upper = _wilson_upper_bound(
        false_reject["numerator"], false_reject["denominator"], alpha=joint_endpoint_alpha
    )
    evidence_dimension_counts = _independent_counts(rows)
    sample_gates = {
        "accepted_components": {
            "observed": precision["denominator"],
            "minimum": config.min_audit_accepted,
            "passed": precision["denominator"] >= config.min_audit_accepted,
        },
        "true_ball_components": {
            "observed": false_reject["denominator"],
            "minimum": config.min_audit_true_balls,
            "passed": false_reject["denominator"] >= config.min_audit_true_balls,
        },
        "independent_components": {
            "observed": audit_component_count,
            "minimum": config.min_independent_components,
            "passed": audit_component_count >= config.min_independent_components,
        },
    }
    point_passed = (
        precision["value"] is not None
        and precision["value"] >= config.accept_precision_target
        and false_reject["value"] is not None
        and false_reject["value"] <= config.false_reject_target
    )
    exact_confidence_passed = (
        accept_exact_upper is not None
        and accept_exact_upper <= 1.0 - config.accept_precision_target
        and reject_exact_upper is not None
        and reject_exact_upper <= config.false_reject_target
    )
    gates_passed = all(gate["passed"] for gate in sample_gates.values())
    cluster_diagnostics = _cluster_diagnostics(rows, thresholds, config)
    cluster_gate = _cluster_gate(rows, thresholds, config)
    qualified = calibration_certified and point_passed and exact_confidence_passed and gates_passed
    if qualified:
        status = "qualified"
    elif gates_passed and not point_passed:
        status = "failed"
    else:
        status = "insufficient_evidence"
    manual_precision = 1.0 - accept_errors / precision["denominator"] if precision["denominator"] else None
    manual_reject = false_reject["numerator"] / false_reject["denominator"] if false_reject["denominator"] else None
    reconciled = manual_precision == precision["value"] and manual_reject == false_reject["value"]
    if not reconciled:
        raise SelectivePolicyError("audit metrics do not reconcile with tracking_benchmark")
    return {
        "status": status,
        "qualified": qualified,
        "qualification_scope": "fixed_aggregate_audit_cohort",
        "inferential_unit": INFERENTIAL_UNIT,
        "inferential_unit_algorithm": INFERENTIAL_UNIT_ALGORITHM,
        "audit_component_count": audit_component_count,
        "evidence_dimension_counts": evidence_dimension_counts,
        "fixed_thresholds": dict(thresholds),
        "benchmark": benchmark,
        "reconciled": reconciled,
        "point_targets_passed": point_passed,
        "one_sided_confidence": {
            "qualification_method": AUDIT_ALGORITHM,
            "inferential_unit": INFERENTIAL_UNIT,
            "accepted_component_count": precision["denominator"],
            "true_ball_component_count": false_reject["denominator"],
            "familywise_alpha": config.fwer_alpha,
            "per_endpoint_alpha": joint_endpoint_alpha,
            "accept_error_exact_upper": accept_exact_upper,
            "false_reject_exact_upper": reject_exact_upper,
            "accept_error_upper": accept_upper,
            "false_reject_upper": reject_upper,
            "wilson_is_diagnostic_only": True,
            "scope": "fixed_aggregate_audit_cohort",
            "passed": exact_confidence_passed,
        },
        "sample_gates": sample_gates,
        "cluster_gate": cluster_gate,
        "slices": _audit_slices(rows, evaluations),
        "cluster_diagnostics": cluster_diagnostics,
    }


def _audit_slices(rows: list[dict[str, Any]], evaluations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_variant: dict[str, list[dict[str, Any]]] = {}
    variant_by_id = {row["candidate_id"]: row["variant_id"] for row in rows}
    for evaluation in evaluations:
        by_variant.setdefault(variant_by_id[evaluation["candidate_id"]], []).append(evaluation)
    return [
        {
            "slice_type": "variant_id",
            "slice_value": variant_id,
            "benchmark": build_benchmark_report(candidate_evaluations=items),
        }
        for variant_id, items in sorted(by_variant.items())
    ]


def _independent_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        key: len({row[key] for row in rows})
        for key in ("variant_id", "video_sha256", "group_id", "split_group", "temporal_group")
    }


def _cluster_diagnostics(
    rows: list[dict[str, Any]],
    thresholds: dict[str, float],
    config: SelectivePolicyConfig,
) -> dict[str, list[dict[str, Any]]]:
    return {
        dimension: _cluster_dimension(rows, dimension, thresholds, config)
        for dimension in ("video_sha256", "group_id", "split_group", "temporal_group")
    }


def _cluster_dimension(
    rows: list[dict[str, Any]],
    dimension: str,
    thresholds: dict[str, float],
    config: SelectivePolicyConfig,
) -> list[dict[str, Any]]:
    clusters: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        clusters.setdefault(row[dimension], []).append(row)
    result: list[dict[str, Any]] = []
    for cluster_value, items in sorted(clusters.items()):
        decisions = [_raw_decision(row, thresholds["accept"], thresholds["reject"], config) for row in items]
        accepted = [row for row, decision in zip(items, decisions) if decision == "accept"]
        true_balls = [row for row in items if row["truth"] == "match_ball"]
        accept_errors = sum(row["truth"] != "match_ball" for row in accepted)
        false_rejects = sum(
            row["truth"] == "match_ball" and decision == "reject" for row, decision in zip(items, decisions)
        )
        accept_precision = 1.0 - accept_errors / len(accepted) if accepted else None
        false_reject_rate = false_rejects / len(true_balls) if true_balls else None
        support_passed = (
            len(accepted) >= config.min_cluster_accepted and len(true_balls) >= config.min_cluster_true_balls
        )
        point_passed = (
            accept_precision is not None
            and accept_precision >= config.accept_precision_target
            and false_reject_rate is not None
            and false_reject_rate <= config.false_reject_target
        )
        result.append(
            {
                "cluster_value": cluster_value,
                "candidate_count": len(items),
                "accepted_count": len(accepted),
                "accept_error_count": accept_errors,
                "accept_precision": accept_precision,
                "true_ball_count": len(true_balls),
                "false_reject_count": false_rejects,
                "false_reject_rate": false_reject_rate,
                "support_passed": support_passed,
                "point_targets_passed": point_passed,
                "passed": support_passed and point_passed,
            }
        )
    return result


def _cluster_gate(
    rows: list[dict[str, Any]],
    thresholds: dict[str, float],
    config: SelectivePolicyConfig,
) -> dict[str, Any]:
    diagnostics = _cluster_diagnostics(rows, thresholds, config)
    failed = [
        f"{dimension}:{item['cluster_value']}"
        for dimension, items in diagnostics.items()
        for item in items
        if not item["passed"]
    ]
    return {
        "method": "heterogeneity_descriptive_diagnostic_v2",
        "purpose": "diagnostic_only",
        "affects_qualification": False,
        "qualification_scope": "fixed_aggregate_audit_cohort",
        "per_cluster_statistical_guarantee": "none",
        "minimum_accepted_per_cluster": config.min_cluster_accepted,
        "minimum_true_balls_per_cluster": config.min_cluster_true_balls,
        "failed_clusters": failed,
        "passed": not failed,
    }


def _apply_policy(
    rows: list[dict[str, Any]],
    contract: dict[str, Any],
    *,
    thresholds: dict[str, float],
    qualified: bool,
    config: SelectivePolicyConfig,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    decision_rows = []
    additions = []
    for row in rows:
        forced = _forced_reasons(row, config)
        evaluation_holdout = row["policy_role"] in {"policy_calibration", "policy_audit"}
        raw_decision = (
            "abstain" if evaluation_holdout else _raw_decision(row, thresholds["accept"], thresholds["reject"], config)
        )
        if evaluation_holdout:
            forced = sorted({*forced, "evaluation_holdout"})
        decision = raw_decision if qualified and not forced else "abstain"
        confidence = max(row["accept_score"], row["reject_score"])
        applied = (
            qualified
            and not evaluation_holdout
            and decision in {"accept", "reject"}
            and not row["has_existing_decision"]
        )
        decision_rows.append(
            {
                "candidate_id": row["candidate_id"],
                "candidate_fingerprint": row["candidate_fingerprint"],
                "variant_id": row["variant_id"],
                "frame_index": row["frame_index"],
                "accept_score": row["accept_score"],
                "reject_score": row["reject_score"],
                "unknown_score": row["unknown_score"],
                "top_label": row["top_label"],
                "top_margin": row["top_margin"],
                "raw_decision": raw_decision,
                "decision": decision,
                "decision_scope": "evaluation_only" if evaluation_holdout else "application",
                "policy_role": row["policy_role"],
                "forced_abstain_reasons": forced,
                "existing_decision_preserved": row["has_existing_decision"],
                "applied_to_contract": applied,
            }
        )
        if applied:
            additions.append(
                {
                    "candidate_id": row["candidate_id"],
                    "decision": decision,
                    "confidence": confidence,
                    "reason": ",".join(forced) if forced else f"selective_policy_{decision}",
                }
            )
    derived = build_tracking_contract(
        source=contract.get("source"),
        frames=contract["frames"],
        candidates=contract["candidates"],
        classifications=contract["classifications"],
        decisions=[*contract["decisions"], *additions],
    )
    if derived["validation_errors"]:
        raise SelectivePolicyError(f"derived tracking contract is invalid: {derived['validation_errors']}")
    return decision_rows, derived


def _forced_reasons(row: dict[str, Any], config: SelectivePolicyConfig) -> list[str]:
    reasons = list(row["base_forced_reasons"])
    if row["top_margin"] < config.min_top_margin:
        reasons.append("top_margin_below_minimum")
    if abs(row["accept_score"] - row["reject_score"]) < config.conflict_margin:
        reasons.append("accept_reject_conflict_margin")
    if row["unknown_score"] >= config.max_unknown_probability:
        reasons.append("unknown_probability_too_high")
    return sorted(set(reasons))


def _raw_decision(
    row: dict[str, Any], accept_threshold: float, reject_threshold: float, config: SelectivePolicyConfig
) -> str:
    if _forced_reasons(row, config):
        return "abstain"
    accept = row["accept_score"] >= accept_threshold
    reject = row["reject_score"] >= reject_threshold
    if accept and reject:
        raise SelectivePolicyError("overlapping thresholds selected contradictory decisions")
    return "accept" if accept else "reject" if reject else "abstain"


def _binomial_lower_tail(errors: int, total: int, risk_limit: float) -> float:
    if total < 0 or errors < 0 or errors > total:
        raise ValueError("invalid binomial counts")
    if not 0.0 < risk_limit < 1.0:
        raise ValueError("risk_limit must be between zero and one")
    if total == 0:
        return 1.0
    if errors == total:
        return 1.0
    mode = math.floor((total + 1) * risk_limit)
    if errors <= mode:
        log_probability = _log_binomial_pmf(errors, total, risk_limit)
        relative_sum = 1.0
        relative_term = 1.0
        for count in range(errors, 0, -1):
            relative_term *= count * (1.0 - risk_limit) / ((total - count + 1) * risk_limit)
            relative_sum += relative_term
            if relative_term <= relative_sum * 1e-16:
                break
        return min(1.0, math.exp(log_probability + math.log(relative_sum)))
    first_upper = errors + 1
    log_probability = _log_binomial_pmf(first_upper, total, risk_limit)
    relative_sum = 1.0
    relative_term = 1.0
    for count in range(first_upper, total):
        relative_term *= (total - count) * risk_limit / ((count + 1) * (1.0 - risk_limit))
        relative_sum += relative_term
        if relative_term <= relative_sum * 1e-16:
            break
    log_upper = log_probability + math.log(relative_sum)
    return min(1.0, max(0.0, -math.expm1(log_upper)))


def _log_binomial_pmf(count: int, total: int, probability: float) -> float:
    return (
        math.lgamma(total + 1)
        - math.lgamma(count + 1)
        - math.lgamma(total - count + 1)
        + count * math.log(probability)
        + (total - count) * math.log1p(-probability)
    )


def _holm_rejections(hypotheses: list[tuple[str, float]], *, alpha: float) -> set[str]:
    if not hypotheses:
        return set()
    identifiers = [identifier for identifier, _ in hypotheses]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Holm hypothesis identifiers must be unique")
    ordered = sorted(hypotheses, key=lambda item: (item[1], item[0]))
    rejected: set[str] = set()
    total = len(ordered)
    for rank, (identifier, p_value) in enumerate(ordered):
        if not math.isfinite(p_value) or not 0.0 <= p_value <= 1.0:
            raise ValueError("Holm p-values must be finite probabilities")
        if p_value > alpha / (total - rank):
            break
        rejected.add(identifier)
    return rejected


def _wilson_upper_bound(errors: int, total: int, *, alpha: float) -> float | None:
    if total == 0:
        return None
    if errors < 0 or errors > total or not 0.0 < alpha < 0.5:
        raise ValueError("invalid Wilson inputs")
    proportion = errors / total
    z = NormalDist().inv_cdf(1.0 - alpha)
    denominator = 1.0 + z * z / total
    center = proportion + z * z / (2.0 * total)
    radius = z * math.sqrt(proportion * (1.0 - proportion) / total + z * z / (4.0 * total * total))
    return min(1.0, (center + radius) / denominator)


def _exact_binomial_upper_bound(errors: int, total: int, *, alpha: float) -> float | None:
    if total == 0:
        return None
    if errors < 0 or errors > total or not 0.0 < alpha < 0.5:
        raise ValueError("invalid exact binomial bound inputs")
    if errors == total:
        return 1.0
    lower = errors / total
    upper = 1.0
    for _ in range(80):
        midpoint = (lower + upper) / 2.0
        tail = _binomial_lower_tail(errors, total, midpoint)
        if tail > alpha:
            lower = midpoint
        else:
            upper = midpoint
    return upper


def _minimum_zero_error_sample(risk_limit: float, alpha: float, hypothesis_count: int) -> int:
    adjusted = alpha / hypothesis_count
    return math.ceil(math.log(adjusted) / math.log(1.0 - risk_limit))


def _require_binary_truth(rows: list[dict[str, Any]], name: str) -> None:
    truths = {row["truth"] for row in rows}
    if not {"match_ball", "noise"}.issubset(truths):
        raise SelectivePolicyError(f"{name} requires confirmed match_ball and noise truth")


def _require_one_candidate_per_component(rows: list[dict[str, Any]], name: str) -> int:
    component_ids = [_required_sha256(row.get("component_id"), f"{name} candidate component_id") for row in rows]
    if len(component_ids) != len(set(component_ids)):
        raise SelectivePolicyError(
            f"{name} requires exactly one human-confirmed evaluation candidate per connected evidence component"
        )
    return len(component_ids)


def _load_snapshot_json(path: Path, name: str) -> tuple[dict[str, Any], _Snapshot]:
    captured, snapshot = _capture_snapshot(path, name, max_bytes=_MAX_JSON_ARTIFACT_BYTES)
    assert captured is not None
    try:
        value = json.loads(
            captured.decode("utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"non-finite {value}")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SelectivePolicyError(f"invalid {name}: {exc}") from exc
    if not isinstance(value, dict):
        raise SelectivePolicyError(f"{name} must be a JSON object")
    return value, snapshot


def _load_snapshot_tracking_contract(path: Path, name: str) -> tuple[dict[str, Any], _Snapshot]:
    raw, snapshot = _load_snapshot_json(path, name)
    return normalize_tracking_contract_payload(raw, path=snapshot.path), snapshot


def _snapshot(path: Path, name: str) -> _Snapshot:
    _, snapshot = _capture_snapshot(path, name)
    return snapshot


def _qualification_source_contract_snapshot(
    dataset_snapshot: _Snapshot,
    resolution: dict[str, Any],
) -> _Snapshot:
    binding = resolution.get("source_contract")
    if not isinstance(binding, dict):
        raise SelectivePolicyError("annotation resolution lacks its qualification source contract binding")
    raw_path = binding.get("path", "source-contract.json")
    if not isinstance(raw_path, str) or not raw_path or Path(raw_path).name != raw_path:
        raise SelectivePolicyError("qualification source contract path must be a safe sibling file name")
    path = (dataset_snapshot.path.parent / raw_path).resolve()
    if path.parent != dataset_snapshot.path.parent.resolve():
        raise SelectivePolicyError("qualification source contract escapes the dataset package")
    snapshot = _snapshot(path, "qualification source contract")
    if snapshot.sha256 != _required_sha256(
        binding.get("sha256"),
        "annotation qualification source contract sha256",
    ):
        raise SelectivePolicyError("qualification source contract sha256 does not match annotation evidence")
    return snapshot


def _capture_snapshot(
    path: Path,
    name: str,
    *,
    max_bytes: int | None = None,
) -> tuple[bytes | None, _Snapshot]:
    path = Path(path).resolve()
    digest = hashlib.sha256()
    size = 0
    chunks: list[bytes] | None = [] if max_bytes is not None else None
    try:
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            if max_bytes is not None and before.st_size > max_bytes:
                raise SelectivePolicyError(f"{name} exceeds the {max_bytes}-byte JSON artifact limit")
            while True:
                read_size = _FILE_READ_CHUNK_BYTES
                if max_bytes is not None:
                    read_size = min(read_size, max_bytes + 1 - size)
                chunk = source.read(read_size)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                if max_bytes is not None:
                    if size > max_bytes:
                        raise SelectivePolicyError(f"{name} exceeds the {max_bytes}-byte JSON artifact limit")
                    assert chunks is not None
                    chunks.append(chunk)
            after = os.fstat(source.fileno())
    except FileNotFoundError as exc:
        raise SelectivePolicyError(f"{name} is missing: {path}") from exc
    except OSError as exc:
        raise SelectivePolicyError(f"could not read {name}: {exc}") from exc
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if size != before.st_size or after_identity != before_identity:
        raise SelectivePolicyError(f"{name} changed or ended early while it was captured")
    snapshot = _Snapshot(path=path, sha256=digest.hexdigest(), size=size)
    return (b"".join(chunks) if chunks is not None else None), snapshot


def _safe_package_artifact(package_dir: Path, value: Any, *, expected_name: str, label: str) -> Path:
    if value != expected_name:
        raise SelectivePolicyError(f"{label} path must be {expected_name!r}")
    path = (package_dir / expected_name).resolve()
    if path.parent != package_dir.resolve():
        raise SelectivePolicyError(f"{label} path escapes the model package")
    return path


def _validate_training_report_package_path(
    model_manifest: dict[str, Any], model_snapshot: _Snapshot, training_snapshot: _Snapshot
) -> None:
    expected_path = _safe_package_artifact(
        model_snapshot.path.parent,
        model_manifest.get("training_report_path"),
        expected_name="training_report.v1.json",
        label="training report",
    )
    if training_snapshot.path != expected_path:
        raise SelectivePolicyError("training report must be the exact file in the model package")


def _verify_snapshots(snapshots: list[_Snapshot]) -> None:
    for expected in snapshots:
        if _snapshot(expected.path, expected.path.name) != expected:
            raise SelectivePolicyError(f"input changed during selective policy fit: {expected.path}")


def _unique_by(values: Any, field: str, name: str) -> dict[str, dict[str, Any]]:
    if not isinstance(values, list):
        raise SelectivePolicyError(f"{name} must be a list")
    result: dict[str, dict[str, Any]] = {}
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise SelectivePolicyError(f"{name}[{index}] must be an object")
        key = _required_text(value.get(field), f"{name}[{index}].{field}")
        if key in result:
            raise SelectivePolicyError(f"duplicate {field} in {name}: {key!r}")
        result[key] = value
    return result


def _finite_vector(value: Any, length: int, name: str) -> list[float]:
    if not isinstance(value, list) or len(value) != length:
        raise SelectivePolicyError(f"{name} must contain {length} numbers")
    return [_finite_number(item, name) for item in value]


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SelectivePolicyError(f"{name} must be finite")
    return float(value)


def _positive_number(value: Any, name: str) -> float:
    result = _finite_number(value, name)
    if result <= 0:
        raise SelectivePolicyError(f"{name} must be positive")
    return result


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SelectivePolicyError(f"{name} is required")
    return value.strip()


def _required_sha256(value: Any, name: str) -> str:
    result = _required_text(value, name)
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise SelectivePolicyError(f"{name} must be lowercase sha256")
    return result


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


def _validate_finite_json(value: Any, name: str) -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SelectivePolicyError(f"{name} is not finite JSON: {exc}") from exc


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _InvalidArgumentsError(message)


class _InvalidArgumentsError(ValueError):
    pass


def build_roles_cli_main(argv: list[str] | None = None) -> int:
    parser = _JsonArgumentParser(description="Build deterministic selective-policy calibration/audit roles")
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--dataset-manifest", required=True, type=Path)
    parser.add_argument("--annotation-resolution", required=True, type=Path)
    parser.add_argument("--resolved-contract", required=True, type=Path)
    parser.add_argument("--model-manifest", required=True, type=Path)
    parser.add_argument("--training-report", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    try:
        args = parser.parse_args(argv)
        manifest = build_selective_policy_roles(
            args.predictions,
            args.dataset_manifest,
            args.annotation_resolution,
            args.resolved_contract,
            args.model_manifest,
            args.training_report,
            args.output_dir,
        )
    except _InvalidArgumentsError:
        print(json.dumps({"ok": False, "error": "invalid_arguments"}), file=sys.stderr)
        return 2
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "role_counts": {role: len(candidate_ids) for role, candidate_ids in manifest["roles"].items()},
                "output_dir": str(args.output_dir),
            }
        )
    )
    return 0


def fit_cli_main(argv: list[str] | None = None) -> int:
    parser = _JsonArgumentParser(description="Fit and audit a fail-closed selective candidate policy")
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--dataset-manifest", required=True, type=Path)
    parser.add_argument("--annotation-resolution", required=True, type=Path)
    parser.add_argument("--resolved-contract", required=True, type=Path)
    parser.add_argument("--model-manifest", required=True, type=Path)
    parser.add_argument("--training-report", required=True, type=Path)
    parser.add_argument("--policy-roles", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    try:
        args = parser.parse_args(argv)
        policy = fit_selective_policy(
            args.predictions,
            args.dataset_manifest,
            args.annotation_resolution,
            args.resolved_contract,
            args.model_manifest,
            args.training_report,
            args.policy_roles,
            args.output_dir,
        )
    except _InvalidArgumentsError:
        print(json.dumps({"ok": False, "error": "invalid_arguments"}), file=sys.stderr)
        return 2
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "status": policy["status"], "policy_version": policy["policy_version"]}))
    return 0


def apply_cli_main(argv: list[str] | None = None) -> int:
    parser = _JsonArgumentParser(description="Apply a frozen qualified policy to independent target evidence")
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--dataset-manifest", required=True, type=Path)
    parser.add_argument("--target-contract", required=True, type=Path)
    parser.add_argument("--model-manifest", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    try:
        args = parser.parse_args(argv)
        application = apply_frozen_selective_policy(
            args.policy,
            args.predictions,
            args.dataset_manifest,
            args.target_contract,
            args.model_manifest,
            args.output_dir,
        )
    except _InvalidArgumentsError:
        print(json.dumps({"ok": False, "error": "invalid_arguments"}), file=sys.stderr)
        return 2
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "status": application["status"],
                "policy_version": application["policy_version"],
                "candidate_count": application["summary"]["candidate_count"],
                "output_dir": str(args.output_dir),
            }
        )
    )
    return 0
