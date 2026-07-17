from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from football_tracking.api.broadcast_api import (
    BroadcastApiError,
    collect_review_evidence_paths,
    validate_review_queue_bindings,
)
from football_tracking.candidate_annotations import (
    ADJUDICATION_QUEUE_NAME,
    validate_candidate_annotation_package,
)
from football_tracking.candidate_classifier import (
    ClassifierError,
    validate_candidate_classifier_package,
)
from football_tracking.selective_policy import (
    SelectivePolicyError,
    validate_selective_policy_application_binding,
    validate_selective_policy_evidence_binding,
    validate_target_audit_application_binding,
)
from football_tracking.target_finite_population import (
    TargetFinitePopulationError,
    build_target_qualified_application,
    capture_target_prelabel_commitment_file,
    capture_target_prelabel_registry,
    validate_target_label_non_leakage,
    validate_target_prelabel_commitment_bytes,
)

BUNDLE_MANIFEST_NAME = "review_evidence_bundle.v1.json"
BUNDLE_SCHEMA_VERSION = "1.0"
BUNDLE_ARTIFACT_TYPE = "broadcast_review_evidence_bundle"
TARGET_BUNDLE_SCHEMA_VERSION = "2.0"
TARGET_BUNDLE_ARTIFACT_TYPE = "target_finite_population_review_evidence_bundle"
REQUIRED_PACKAGE_NAMES = frozenset({"model_development", "policy_qualification", "target_application"})
MAX_REVIEW_WINDOWS = 30
ACTIVATION_MANIFEST_NAME = "review_evidence_activation.v1.json"
ACTIVATED_QUEUE_NAME = "selective_review_queue.v1.json"
REVOCATION_NAME = "review_evidence_revocation.v1.json"
RECONCILIATION_NAME = "review_evidence_reconciliation.v1.json"
PROVISIONER_VERSION = "review-evidence-provisioner-v2"
MAX_BUNDLE_FILES = 100_000
MAX_BUNDLE_BYTES = 256 * 1024 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 64 * 1024 * 1024 * 1024
MAX_JSON_BYTES = 256 * 1024 * 1024
_MODEL_DEVELOPMENT_REQUIRED_ARTIFACTS = frozenset(
    {
        "dataset",
        "source_contract",
        "vote_ledger",
        "annotation_resolution",
        "resolved_contract",
    }
)
_POLICY_QUALIFICATION_REQUIRED_ARTIFACTS = frozenset(
    {
        "dataset",
        "policy",
        "source_contract",
        "vote_ledger",
        "annotation_resolution",
        "resolved_contract",
        "predictions",
        "decisions",
        "policy_roles",
    }
)
_NON_TARGET_OPTIONAL_ARTIFACTS = frozenset(
    {"previous_vote_ledger"}
)
_TARGET_APPLICATION_REQUIRED_ARTIFACTS = frozenset(
    {"dataset", "predictions", "decisions", "source", "root_contract"}
)
_TARGET_SCOPE_REQUIRED_ARTIFACTS = frozenset(
    {
        "target_audit_plan",
        "target_audit_labels",
        "target_qualification",
        "target_frozen_application",
        "target_prelabel_commitment",
    }
)


class ReviewEvidenceBundleError(ValueError):
    """A stable, fail-closed review-evidence bundle validation failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ValidatedReviewEvidenceBundle:
    root: Path
    manifest_path: Path
    manifest: dict[str, Any]
    bundle_sha256: str
    queue_path: Path
    queue_sha256: str
    total_size_bytes: int


@dataclass(frozen=True)
class ActivatedReviewEvidence:
    generation_id: str
    generation_dir: Path
    activation_manifest_path: Path
    queue_path: Path
    queue_sha256: str
    idempotent: bool


def revoke_review_evidence_activation(
    output_dir: Path,
    *,
    generation_id: str,
    expected_queue_sha256: str,
) -> dict[str, Any]:
    """Revoke an exact activation before any review action or downstream generation consumes it."""

    output = _trusted_directory(output_dir, "broadcast output")
    root_queue = _contained_nonlink_file(output, output / ACTIVATED_QUEUE_NAME, "active review queue")
    try:
        queue, queue_sha256 = validate_review_queue_bindings(root_queue, trusted_root=output)
    except (BroadcastApiError, OSError, ValueError) as exc:
        raise ReviewEvidenceBundleError("activated_review_queue_invalid", str(exc)) from exc
    activation = _required_mapping(queue.get("activation"), "review queue activation")
    if activation.get("generation_id") != generation_id:
        raise ReviewEvidenceBundleError("review_evidence_revoke_mismatch", "generation id does not match active queue")
    expected_sha256 = _required_sha256(expected_queue_sha256, "expected_queue_sha256")
    if queue_sha256 != expected_sha256:
        raise ReviewEvidenceBundleError("review_evidence_revoke_mismatch", "queue hash does not match active queue")
    if (output / "review_decisions.json").exists():
        raise ReviewEvidenceBundleError("review_evidence_already_consumed", "review actions already consumed evidence")
    downstream = output / "broadcast_generations"
    if downstream.exists() and (not downstream.is_dir() or any(downstream.iterdir())):
        raise ReviewEvidenceBundleError(
            "review_evidence_already_consumed", "downstream broadcast generations already consumed evidence"
        )
    generation_dir = output / "review_evidence" / "generations" / generation_id
    if _is_link_or_reparse(generation_dir) or not generation_dir.is_dir():
        raise ReviewEvidenceBundleError("review_evidence_revoke_mismatch", "activation generation is unavailable")
    revocation_path = generation_dir / REVOCATION_NAME
    if revocation_path.exists():
        existing_path = _contained_nonlink_file(output, revocation_path, "review evidence revocation")
        report = _load_json(existing_path, "review evidence revocation")
        if (
            report.get("schema_version") != BUNDLE_SCHEMA_VERSION
            or report.get("artifact_type") != "broadcast_review_evidence_revocation"
            or report.get("generation_id") != generation_id
            or report.get("queue_sha256") != queue_sha256
            or report.get("reason") != "pre_consumption_revoke"
        ):
            raise ReviewEvidenceBundleError(
                "review_evidence_revoke_mismatch", "existing revocation marker does not match the activation"
            )
        _required_timestamp(report.get("revoked_at"), "revocation.revoked_at")
    else:
        report = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "artifact_type": "broadcast_review_evidence_revocation",
            "generation_id": generation_id,
            "queue_sha256": queue_sha256,
            "revoked_at": datetime.now(timezone.utc).isoformat(),
            "reason": "pre_consumption_revoke",
        }
        _write_json(revocation_path, report, exclusive=True)
        _fsync_directory(generation_dir)
    if sha256_file(root_queue) != queue_sha256:
        raise ReviewEvidenceBundleError("review_evidence_revoke_mismatch", "active queue changed before revoke")
    root_queue.unlink()
    _fsync_directory(output)
    return report


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_review_evidence_bundle(
    source_dir: Path,
    output_dir: Path,
    *,
    draft_manifest_path: Path | None = None,
) -> ValidatedReviewEvidenceBundle:
    """Copy a prepared offline bundle, generate its inventory, and publish it atomically."""

    source = _trusted_directory(source_dir, "bundle source")
    output = Path(output_dir).resolve()
    try:
        output.relative_to(source)
    except ValueError:
        pass
    else:
        raise ReviewEvidenceBundleError(
            "unsafe_bundle_output", "bundle output and its staging directory may not be inside the source"
        )
    if output.exists():
        raise ReviewEvidenceBundleError("bundle_output_exists", f"bundle output already exists: {output}")
    draft_path = Path(draft_manifest_path or source / "review_evidence_bundle.draft.json")
    draft_path = _contained_nonlink_file(source, draft_path, "bundle draft manifest")
    draft = _load_json(draft_path, "bundle draft manifest")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent))
    try:
        copied_file_count = 0
        copied_size_bytes = 0
        for path in sorted(source.rglob("*")):
            if path == draft_path or path.name == BUNDLE_MANIFEST_NAME:
                continue
            relative = path.relative_to(source)
            _require_safe_relative_path(relative.as_posix(), "bundle source path")
            if _is_link_or_reparse(path):
                raise ReviewEvidenceBundleError("unsafe_bundle_path", f"bundle source contains a link: {relative}")
            if any(part.startswith(".") and "staging" in part.lower() for part in relative.parts):
                raise ReviewEvidenceBundleError("unsafe_bundle_path", "bundle source contains a hidden staging entry")
            destination = staging / relative
            if path.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            elif path.is_file():
                size = path.stat().st_size
                copied_file_count += 1
                copied_size_bytes += size
                if (
                    copied_file_count > MAX_BUNDLE_FILES
                    or size > MAX_SINGLE_FILE_BYTES
                    or copied_size_bytes > MAX_BUNDLE_BYTES
                ):
                    raise ReviewEvidenceBundleError("bundle_capacity_exceeded", "bundle exceeds provisioner limits")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
            else:
                raise ReviewEvidenceBundleError("unsafe_bundle_path", f"unsupported bundle entry: {relative}")
        manifest = dict(draft)
        target_scope = draft.get("qualification_scope") == "target_finite_population"
        manifest["schema_version"] = TARGET_BUNDLE_SCHEMA_VERSION if target_scope else BUNDLE_SCHEMA_VERSION
        manifest["artifact_type"] = TARGET_BUNDLE_ARTIFACT_TYPE if target_scope else BUNDLE_ARTIFACT_TYPE
        _stage_bundle_root_queue(source, staging, manifest)
        reconciliation = _build_reconciliation(staging, manifest)
        target_root = _require_safe_relative_path(
            _required_mapping(
                _required_mapping(manifest.get("packages"), "packages").get("target_application"), "target_application"
            ).get("root"),
            "packages.target_application.root",
        )
        reconciliation_path = staging.joinpath(*target_root.parts) / RECONCILIATION_NAME
        _write_json(reconciliation_path, reconciliation, exclusive=True)
        manifest["reconciliation"] = {
            "path": reconciliation_path.relative_to(staging).as_posix(),
            "sha256": sha256_file(reconciliation_path),
        }
        manifest["inventory"] = _build_inventory(staging)
        _write_json(staging / BUNDLE_MANIFEST_NAME, manifest, exclusive=True)
        validated = validate_review_evidence_bundle(staging)
        os.replace(staging, output)
        return ValidatedReviewEvidenceBundle(
            root=output,
            manifest_path=output / BUNDLE_MANIFEST_NAME,
            manifest=validated.manifest,
            bundle_sha256=validated.bundle_sha256,
            queue_path=output / validated.queue_path.relative_to(validated.root),
            queue_sha256=validated.queue_sha256,
            total_size_bytes=validated.total_size_bytes,
        )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def validate_review_evidence_bundle(bundle_dir: Path) -> ValidatedReviewEvidenceBundle:
    """Re-hash and validate a self-contained three-package review-evidence bundle."""

    root = _trusted_directory(bundle_dir, "review evidence bundle")
    manifest_path = _contained_nonlink_file(root, root / BUNDLE_MANIFEST_NAME, "bundle manifest")
    initial_manifest_sha256 = sha256_file(manifest_path)
    manifest = _load_json(manifest_path, "bundle manifest")
    target_scope = (
        manifest.get("schema_version") == TARGET_BUNDLE_SCHEMA_VERSION
        and manifest.get("artifact_type") == TARGET_BUNDLE_ARTIFACT_TYPE
        and manifest.get("qualification_scope") == "target_finite_population"
    )
    if not target_scope and (
        manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION
        or manifest.get("artifact_type") != BUNDLE_ARTIFACT_TYPE
        or "qualification_scope" in manifest
    ):
        raise ReviewEvidenceBundleError("invalid_bundle_envelope", "invalid review evidence bundle envelope")
    bundle_id = _required_text(manifest.get("bundle_id"), "bundle_id")
    if not bundle_id.startswith("review-evidence-") or len(bundle_id) > 96:
        raise ReviewEvidenceBundleError("invalid_bundle_id", "bundle_id must start with 'review-evidence-'")

    target = _required_mapping(manifest.get("target"), "target")
    _required_text(target.get("run_id"), "target.run_id")
    source_sha256 = _required_sha256(target.get("source_sha256"), "target.source_sha256")
    root_contract_sha256 = _required_sha256(target.get("root_contract_sha256"), "target.root_contract_sha256")
    max_windows = _positive_int(target.get("max_review_windows"), "target.max_review_windows")
    if max_windows > MAX_REVIEW_WINDOWS:
        raise ReviewEvidenceBundleError(
            "review_window_limit_exceeded", f"target.max_review_windows exceeds {MAX_REVIEW_WINDOWS}"
        )
    if _positive_int(target.get("max_manual_review_windows"), "target.max_manual_review_windows") != max_windows:
        raise ReviewEvidenceBundleError("target_binding_mismatch", "manual review window limits disagree")
    for field in ("action_signal_binding_sha256", "confirmed_config_sha256", "profile_digest"):
        _required_sha256(target.get(field), f"target.{field}")
    config_lineage = target.get("config_lineage")
    if config_lineage is not None:
        config_lineage = _required_mapping(config_lineage, "target.config_lineage")
        if set(config_lineage) != {
            "confirmed_text_sha256",
            "observed_raw_sha256",
            "canonical_snapshot_sha256",
            "generation_id",
            "manifest_sha256",
            "historical_raw_snapshot_observed",
        }:
            raise ReviewEvidenceBundleError(
                "target_binding_mismatch",
                "target config-lineage projection fields are invalid",
            )
        for field in (
            "confirmed_text_sha256",
            "observed_raw_sha256",
            "canonical_snapshot_sha256",
            "manifest_sha256",
        ):
            _required_sha256(config_lineage.get(field), f"target.config_lineage.{field}")
        generation_id = _required_text(
            config_lineage.get("generation_id"),
            "target.config_lineage.generation_id",
        )
        if re.fullmatch(r"lineage-[0-9a-f]{24}", generation_id) is None:
            raise ReviewEvidenceBundleError(
                "target_binding_mismatch",
                "target config-lineage generation id is invalid",
            )
        if (
            config_lineage.get("confirmed_text_sha256") != target.get("confirmed_config_sha256")
            or config_lineage.get("historical_raw_snapshot_observed") is not False
        ):
            raise ReviewEvidenceBundleError(
                "target_binding_mismatch",
                "target config-lineage projection does not reconcile",
            )
    _required_text(target.get("quality_profile"), "target.quality_profile")
    if target.get("provisioner_version") != PROVISIONER_VERSION:
        raise ReviewEvidenceBundleError("invalid_provisioner_version", "unsupported review evidence provisioner")
    declared_population_sha256 = _required_sha256(
        target.get("candidate_population_sha256"), "target.candidate_population_sha256"
    )
    declared_population_count = _positive_int(
        target.get("candidate_population_count"), "target.candidate_population_count"
    )

    inventory = _validate_inventory(root, manifest.get("inventory"))
    manifest_size_bytes = manifest_path.stat().st_size
    total_size_bytes = manifest_size_bytes + sum(row["size_bytes"] for row in inventory.values())
    if (
        len(inventory) + 1 > MAX_BUNDLE_FILES
        or manifest_size_bytes > MAX_SINGLE_FILE_BYTES
        or total_size_bytes > MAX_BUNDLE_BYTES
    ):
        raise ReviewEvidenceBundleError(
            "bundle_capacity_exceeded", "bundle manifest and inventoried artifacts exceed provisioner limits"
        )
    provisioning = _required_mapping(manifest.get("provisioning"), "provisioning")
    attempt_quota_bytes = _positive_int(provisioning.get("attempt_quota_bytes"), "provisioning.attempt_quota_bytes")
    if attempt_quota_bytes < total_size_bytes or attempt_quota_bytes > MAX_BUNDLE_BYTES:
        raise ReviewEvidenceBundleError(
            "bundle_capacity_exceeded", "attempt quota must contain the bundle and stay within the server ceiling"
        )
    retention = _required_mapping(provisioning.get("retention"), "provisioning.retention")
    if retention.get("policy") != "manual-audit-retention-v1" or retention.get("automatic_delete") is not False:
        raise ReviewEvidenceBundleError(
            "invalid_retention_policy", "qualified evidence requires manual audit retention"
        )
    _required_timestamp(retention.get("retain_until"), "provisioning.retention.retain_until")
    packages = _required_mapping(manifest.get("packages"), "packages")
    if set(packages) != REQUIRED_PACKAGE_NAMES:
        raise ReviewEvidenceBundleError(
            "invalid_package_partition",
            f"packages must be exactly {sorted(REQUIRED_PACKAGE_NAMES)}",
        )
    package_roots: dict[str, PurePosixPath] = {}
    package_descriptors: dict[str, Mapping[str, Any]] = {}
    for name in sorted(REQUIRED_PACKAGE_NAMES):
        descriptor = _required_mapping(packages[name], f"packages.{name}")
        _validate_package_descriptor_schema(
            name,
            descriptor,
            target_scope=target_scope,
        )
        package_descriptors[name] = descriptor
        root_path = _require_safe_relative_path(descriptor.get("root"), f"packages.{name}.root")
        if root_path == PurePosixPath("."):
            raise ReviewEvidenceBundleError("invalid_package_partition", "package roots may not be the bundle root")
        manifest_relative = _require_safe_relative_path(
            descriptor.get("manifest_path"), f"packages.{name}.manifest_path"
        )
        if not manifest_relative.is_relative_to(root_path):
            raise ReviewEvidenceBundleError(
                "invalid_package_partition", f"packages.{name}.manifest_path is outside its package root"
            )
        _require_inventory_path(inventory, manifest_relative, f"packages.{name}.manifest_path")
        package_roots[name] = root_path
    for left_name, left_root in package_roots.items():
        for right_name, right_root in package_roots.items():
            if left_name >= right_name:
                continue
            if left_root.is_relative_to(right_root) or right_root.is_relative_to(left_root):
                raise ReviewEvidenceBundleError(
                    "invalid_package_partition", f"package roots overlap: {left_name} and {right_name}"
                )
    development_dataset_sha256 = _required_sha256(
        package_descriptors["model_development"].get("dataset_sha256"),
        "packages.model_development.dataset_sha256",
    )
    qualification_dataset_sha256 = _required_sha256(
        package_descriptors["policy_qualification"].get("dataset_sha256"),
        "packages.policy_qualification.dataset_sha256",
    )
    qualification_policy_sha256 = _required_sha256(
        package_descriptors["policy_qualification"].get("policy_sha256"),
        "packages.policy_qualification.policy_sha256",
    )
    application = package_descriptors["target_application"]
    application_dataset_sha256 = _required_sha256(
        application.get("dataset_sha256"), "packages.target_application.dataset_sha256"
    )
    if len({development_dataset_sha256, qualification_dataset_sha256, application_dataset_sha256}) != 3:
        raise ReviewEvidenceBundleError(
            "qualification_application_not_independent",
            "model development, policy qualification, and target application require distinct dataset snapshots",
        )
    for package_name, descriptor in package_descriptors.items():
        for path_field in sorted(descriptor):
            if path_field == "manifest_path" or not path_field.endswith("_path"):
                continue
            artifact_name = path_field.removesuffix("_path")
            _validate_declared_file(
                inventory,
                package_roots[package_name],
                descriptor,
                path_field=path_field,
                sha_field=f"{artifact_name}_sha256",
                label=f"packages.{package_name}.{artifact_name}",
            )
    _validate_declared_file(
        inventory,
        package_roots["model_development"],
        package_descriptors["model_development"],
        path_field="dataset_path",
        sha_field="dataset_sha256",
        label="packages.model_development.dataset",
    )
    model_development = package_descriptors["model_development"]
    development_paths = {
        name: _declared_file_path(
            inventory,
            package_roots["model_development"],
            model_development,
            path_field=f"{name}_path",
            sha_field=f"{name}_sha256",
            label=f"packages.model_development.{name}",
        )
        for name in ("source_contract", "vote_ledger", "annotation_resolution", "resolved_contract")
    }
    model_manifest_relative = _require_safe_relative_path(
        model_development.get("manifest_path"), "packages.model_development.manifest_path"
    )
    model_manifest_path = _require_inventory_path(
        inventory, model_manifest_relative, "packages.model_development.manifest_path"
    )
    development_dataset_path = _declared_file_path(
        inventory,
        package_roots["model_development"],
        model_development,
        path_field="dataset_path",
        sha_field="dataset_sha256",
        label="packages.model_development.dataset",
    )
    try:
        validate_candidate_annotation_package(
            development_paths["source_contract"],
            development_paths["vote_ledger"],
            development_dataset_path,
            development_paths["annotation_resolution"],
            previous_ledger_path=_optional_declared_file_path(
                inventory,
                package_roots["model_development"],
                model_development,
                path_field="previous_vote_ledger_path",
                sha_field="previous_vote_ledger_sha256",
                label="packages.model_development.previous_vote_ledger",
            ),
        )
        validate_candidate_classifier_package(
            model_manifest_path.parent,
            development_dataset_path,
            development_paths["annotation_resolution"],
            development_paths["resolved_contract"],
        )
    except (ClassifierError, OSError, ValueError) as exc:
        raise ReviewEvidenceBundleError("invalid_model_development_evidence", str(exc)) from exc
    _validate_declared_file(
        inventory,
        package_roots["policy_qualification"],
        package_descriptors["policy_qualification"],
        path_field="dataset_path",
        sha_field="dataset_sha256",
        label="packages.policy_qualification.dataset",
    )
    _validate_declared_file(
        inventory,
        package_roots["policy_qualification"],
        package_descriptors["policy_qualification"],
        path_field="policy_path",
        sha_field="policy_sha256",
        label="packages.policy_qualification.policy",
    )
    qualification_descriptor = package_descriptors["policy_qualification"]
    qualification_paths = {
        name: _declared_file_path(
            inventory,
            package_roots["policy_qualification"],
            qualification_descriptor,
            path_field=f"{name}_path",
            sha_field=f"{name}_sha256",
            label=f"packages.policy_qualification.{name}",
        )
        for name in (
            "source_contract",
            "vote_ledger",
            "annotation_resolution",
            "resolved_contract",
            "predictions",
            "decisions",
            "policy_roles",
        )
    }
    qualification_dataset_path = _declared_file_path(
        inventory,
        package_roots["policy_qualification"],
        qualification_descriptor,
        path_field="dataset_path",
        sha_field="dataset_sha256",
        label="packages.policy_qualification.dataset",
    )
    policy_relative = _require_safe_relative_path(
        package_descriptors["policy_qualification"].get("policy_path"),
        "packages.policy_qualification.policy_path",
    )
    policy_payload = _load_json(
        _require_inventory_path(inventory, policy_relative, "packages.policy_qualification.policy_path"),
        "qualified selective policy",
    )
    version_inputs = _required_mapping(policy_payload.get("version_inputs"), "selective policy version_inputs")
    qualification = _required_mapping(version_inputs.get("qualification"), "selective policy qualification")
    legacy_policy_qualified = (
        policy_payload.get("artifact_type") == "selective_policy"
        and policy_payload.get("status") == "qualified"
        and qualification.get("qualified") is True
        and qualification.get("policy_status") == "qualified"
        and qualification.get("acceptance_status") == "qualified"
        and qualification.get("calibration_certified") is True
        and qualification.get("audit_qualified") is True
    )
    target_policy_frozen = (
        target_scope
        and policy_payload.get("artifact_type") == "selective_policy"
        and policy_payload.get("status") in {"qualified", "review_only"}
        and qualification.get("policy_status") == policy_payload.get("status")
    )
    if not legacy_policy_qualified and not target_policy_frozen:
        raise ReviewEvidenceBundleError(
            "selective_policy_not_qualified",
            "the frozen selective policy does not carry qualified calibration and audit evidence",
        )
    try:
        validate_candidate_annotation_package(
            qualification_paths["source_contract"],
            qualification_paths["vote_ledger"],
            qualification_dataset_path,
            qualification_paths["annotation_resolution"],
            previous_ledger_path=_optional_declared_file_path(
                inventory,
                package_roots["policy_qualification"],
                qualification_descriptor,
                path_field="previous_vote_ledger_path",
                sha_field="previous_vote_ledger_sha256",
                label="packages.policy_qualification.previous_vote_ledger",
            ),
        )
        validate_selective_policy_evidence_binding(
            _require_inventory_path(inventory, policy_relative, "qualified selective policy"),
            qualification_paths["decisions"],
            qualification_paths["predictions"],
            qualification_dataset_path,
            qualification_paths["annotation_resolution"],
            qualification_paths["resolved_contract"],
            model_manifest_path,
            qualification_paths["policy_roles"],
        )
    except (OSError, ValueError, SelectivePolicyError) as exc:
        raise ReviewEvidenceBundleError("invalid_policy_qualification_evidence", str(exc)) from exc
    application_declared_hashes = {}
    for artifact_name in ("dataset", "predictions", "decisions", "source", "root_contract"):
        application_declared_hashes[artifact_name] = _validate_declared_file(
            inventory,
            package_roots["target_application"],
            application,
            path_field=f"{artifact_name}_path",
            sha_field=f"{artifact_name}_sha256",
            label=f"packages.target_application.{artifact_name}",
        )
    application_paths = {
        name: _declared_file_path(
            inventory,
            package_roots["target_application"],
            application,
            path_field=f"{name}_path",
            sha_field=f"{name}_sha256",
            label=f"packages.target_application.{name}",
        )
        for name in ("dataset", "predictions", "decisions", "root_contract")
    }
    if target_scope:
        target_paths = {
            name: _declared_file_path(
                inventory,
                package_roots["target_application"],
                application,
                path_field=f"{name}_path",
                sha_field=f"{name}_sha256",
                label=f"packages.target_application.{name}",
            )
            for name in (
                "target_audit_plan",
                "target_audit_labels",
                "target_qualification",
                "target_frozen_application",
                "target_prelabel_commitment",
            )
        }
        try:
            frozen_validation = validate_target_audit_application_binding(
                _require_inventory_path(inventory, policy_relative, "frozen selective policy"),
                target_paths["target_frozen_application"],
                application_paths["predictions"],
                application_paths["dataset"],
                application_paths["root_contract"],
                model_manifest_path,
            )
            expected_application = build_target_qualified_application(
                _load_json(target_paths["target_frozen_application"], "target frozen application"),
                _load_json(target_paths["target_audit_plan"], "target audit plan"),
                target_paths["target_audit_labels"],
                _load_json(target_paths["target_qualification"], "target qualification"),
                commitment_path=target_paths["target_prelabel_commitment"],
            )
            declared_application = _load_json(
                application_paths["decisions"],
                "target qualified application",
            )
            if (
                frozen_validation.get("application")
                != _load_json(target_paths["target_frozen_application"], "target frozen application")
                or expected_application != declared_application
            ):
                raise TargetFinitePopulationError(
                    "target qualified application does not match its frozen audit evidence"
                )
            application_validation = {
                "candidate_ids": sorted(row["candidate_id"] for row in declared_application["decisions"]),
                "candidate_population_sha256": _canonical_sha256(
                    [
                        {
                            "candidate_id": row["candidate_id"],
                            "candidate_fingerprint": row["candidate_fingerprint"],
                        }
                        for row in declared_application["decisions"]
                    ]
                ),
            }
        except (
            ClassifierError,
            OSError,
            ValueError,
            SelectivePolicyError,
            TargetFinitePopulationError,
        ) as exc:
            raise ReviewEvidenceBundleError("invalid_target_application_evidence", str(exc)) from exc
    else:
        try:
            application_validation = validate_selective_policy_application_binding(
                _require_inventory_path(inventory, policy_relative, "qualified selective policy"),
                application_paths["decisions"],
                application_paths["predictions"],
                application_paths["dataset"],
                application_paths["root_contract"],
                model_manifest_path,
            )
        except (ClassifierError, OSError, ValueError, SelectivePolicyError) as exc:
            raise ReviewEvidenceBundleError("invalid_target_application_evidence", str(exc)) from exc
    if (
        application_validation.get("candidate_population_sha256") != declared_population_sha256
        or len(application_validation.get("candidate_ids", [])) != declared_population_count
    ):
        raise ReviewEvidenceBundleError(
            "target_population_mismatch", "target candidate population identity does not match the manifest"
        )
    _validate_population_independence(
        development_dataset_path,
        qualification_dataset_path,
        application_paths["dataset"],
    )
    if _required_sha256(application.get("source_sha256"), "packages.target_application.source_sha256") != source_sha256:
        raise ReviewEvidenceBundleError("target_binding_mismatch", "target application source does not match target")
    if (
        _required_sha256(application.get("root_contract_sha256"), "packages.target_application.root_contract_sha256")
        != root_contract_sha256
    ):
        raise ReviewEvidenceBundleError("target_binding_mismatch", "target application contract does not match target")

    queue_descriptor = _required_mapping(manifest.get("queue"), "queue")
    queue_relative = _require_safe_relative_path(queue_descriptor.get("path"), "queue.path")
    if len(queue_relative.parts) != 1 or queue_relative.name != "selective_review_queue.v1.json":
        raise ReviewEvidenceBundleError(
            "invalid_review_queue_location", "the review queue must be the bundle-root commit marker"
        )
    queue_path = _require_inventory_path(inventory, queue_relative, "queue.path")
    if inventory[queue_relative.as_posix()]["sha256"] != _required_sha256(
        queue_descriptor.get("sha256"), "queue.sha256"
    ):
        raise ReviewEvidenceBundleError("bundle_inventory_mismatch", "queue hash does not match bundle inventory")
    try:
        queue, queue_sha256 = validate_review_queue_bindings(queue_path, trusted_root=root)
        review_media_paths = collect_review_evidence_paths(queue_path, trusted_root=root)
    except (BroadcastApiError, OSError, ValueError) as exc:
        raise ReviewEvidenceBundleError("invalid_review_queue_bindings", str(exc)) from exc
    bindings = _required_mapping(queue.get("bindings"), "review queue bindings")
    if target_scope:
        required_qualification_bindings = {
            "qualification_dataset",
            "qualification_predictions",
            "qualification_decisions",
        }
        if not required_qualification_bindings.issubset(bindings):
            raise ReviewEvidenceBundleError(
                "target_binding_mismatch",
                "target review queue qualification bindings must be complete",
            )
        queue_target_bindings = _required_mapping(
            queue.get("target_bindings"),
            "review queue target_bindings",
        )
        expected_target_projection = {
            "target_run_id": target["run_id"],
            "source_sha256": source_sha256,
            "root_contract_sha256": root_contract_sha256,
            "candidate_population_sha256": declared_population_sha256,
            "confirmed_config_sha256": target["confirmed_config_sha256"],
        }
        if any(
            queue_target_bindings.get(name) != value
            for name, value in expected_target_projection.items()
        ):
            raise ReviewEvidenceBundleError(
                "target_binding_mismatch",
                "review queue exact target bindings do not match the server target manifest",
            )
    binding_packages = {
        "model": "model_development",
        "training_report": "model_development",
        "model_weights": "model_development",
        "policy": "policy_qualification",
        "annotation_resolution": "policy_qualification",
        "resolved_tracking_contract": "policy_qualification",
        "policy_roles": "policy_qualification",
        "qualification_dataset": "policy_qualification",
        "qualification_predictions": "policy_qualification",
        "qualification_decisions": "policy_qualification",
        "dataset": "target_application",
        "predictions": "target_application",
        "decisions": "target_application",
        "review_timing": "target_application",
        "contract": "target_application",
        "target_audit_plan": "target_application",
        "target_audit_labels": "target_application",
        "target_qualification": "target_application",
        "target_frozen_application": "target_application",
        "target_prelabel_commitment": "target_application",
    }
    for binding_name, raw_binding in bindings.items():
        binding = _required_mapping(raw_binding, f"review queue bindings.{binding_name}")
        binding_relative = _require_safe_relative_path(
            binding.get("path"), f"review queue bindings.{binding_name}.path"
        )
        _require_inventory_path(inventory, binding_relative, f"review queue bindings.{binding_name}.path")
        expected_package = binding_packages.get(binding_name)
        if expected_package is not None and not binding_relative.is_relative_to(package_roots[expected_package]):
            raise ReviewEvidenceBundleError(
                "invalid_package_partition",
                f"review queue binding {binding_name} is outside {expected_package}",
            )
    for media_path in review_media_paths:
        media_relative = PurePosixPath(media_path.relative_to(root).as_posix())
        if not media_relative.is_relative_to(package_roots["target_application"]):
            raise ReviewEvidenceBundleError(
                "invalid_package_partition", "review media must remain inside target_application"
            )
    expected_bindings = {
        "dataset": application_dataset_sha256,
        "predictions": application_declared_hashes["predictions"],
        "decisions": application_declared_hashes["decisions"],
        "policy": qualification_policy_sha256,
        "qualification_dataset": qualification_dataset_sha256,
        "qualification_predictions": _required_sha256(
            qualification_descriptor.get("predictions_sha256"),
            "packages.policy_qualification.predictions_sha256",
        ),
        "qualification_decisions": _required_sha256(
            qualification_descriptor.get("decisions_sha256"),
            "packages.policy_qualification.decisions_sha256",
        ),
        "contract": root_contract_sha256,
    }
    for binding_name, expected_sha256 in expected_bindings.items():
        binding = _required_mapping(bindings.get(binding_name), f"review queue bindings.{binding_name}")
        if binding.get("sha256") != expected_sha256:
            raise ReviewEvidenceBundleError(
                "target_binding_mismatch", f"review queue {binding_name} binding does not match the declared package"
            )
    non_target_consumer_paths = _declared_non_target_consumer_paths(
        inventory,
        package_roots,
        package_descriptors,
        bindings,
        binding_packages,
    )
    if target_scope:
        try:
            validate_target_label_non_leakage(
                target_paths["target_audit_labels"],
                non_target_consumer_paths,
                plan_path=target_paths["target_audit_plan"],
                commitment_path=target_paths["target_prelabel_commitment"],
            )
        except (OSError, ValueError, TargetFinitePopulationError) as exc:
            raise ReviewEvidenceBundleError(
                "invalid_target_application_evidence",
                str(exc),
            ) from exc
    _validate_queue_coverage(queue, max_windows=max_windows)
    reconciliation_descriptor = _required_mapping(manifest.get("reconciliation"), "reconciliation")
    reconciliation_relative = _require_safe_relative_path(reconciliation_descriptor.get("path"), "reconciliation.path")
    if not reconciliation_relative.is_relative_to(package_roots["target_application"]):
        raise ReviewEvidenceBundleError("invalid_package_partition", "reconciliation must be in target_application")
    reconciliation_path = _require_inventory_path(inventory, reconciliation_relative, "reconciliation.path")
    reconciliation_sha256 = _required_sha256(reconciliation_descriptor.get("sha256"), "reconciliation.sha256")
    if sha256_file(reconciliation_path) != reconciliation_sha256:
        raise ReviewEvidenceBundleError("bundle_inventory_mismatch", "reconciliation hash mismatch")
    expected_reconciliation = _build_reconciliation(root, manifest)
    if _load_json(reconciliation_path, "review evidence reconciliation") != expected_reconciliation:
        raise ReviewEvidenceBundleError(
            "target_reconciliation_mismatch", "target application reconciliation is incomplete or stale"
        )
    if expected_reconciliation.get("candidate_population_sha256") != declared_population_sha256:
        raise ReviewEvidenceBundleError("target_population_mismatch", "reconciliation population identity mismatch")
    if sha256_file(manifest_path) != initial_manifest_sha256:
        raise ReviewEvidenceBundleError("bundle_changed_during_validation", "bundle manifest changed")
    return ValidatedReviewEvidenceBundle(
        root=root,
        manifest_path=manifest_path,
        manifest=manifest,
        bundle_sha256=initial_manifest_sha256,
        queue_path=queue_path,
        queue_sha256=queue_sha256,
        total_size_bytes=total_size_bytes,
    )


def discover_review_evidence_bundles(
    inbox_dir: Path,
    *,
    run_id: str | None = None,
    source_sha256: str | None = None,
    root_contract_sha256: str | None = None,
    expected_target: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Discover direct child bundles in a server-managed inbox without trusting their metadata."""

    inbox = Path(os.path.abspath(inbox_dir))
    if not inbox.exists():
        return []
    if _is_link_or_reparse(inbox) or not inbox.is_dir():
        raise ReviewEvidenceBundleError("unsafe_bundle_path", "review evidence inbox must be a non-link directory")
    results: list[dict[str, Any]] = []
    for candidate in sorted(inbox.iterdir(), key=lambda path: path.name):
        if candidate.name.startswith(".") or "staging" in candidate.name.lower():
            continue
        if _is_link_or_reparse(candidate) or not candidate.is_dir():
            continue
        try:
            bundle = validate_review_evidence_bundle(candidate)
            target = bundle.manifest["target"]
            expected_fields = dict(expected_target or {})
            expected_fields.update(
                {
                    field: expected
                    for field, expected in (
                        ("run_id", run_id),
                        ("source_sha256", source_sha256),
                        ("root_contract_sha256", root_contract_sha256),
                    )
                    if expected is not None
                }
            )
            matches = all(target.get(field) == expected for field, expected in expected_fields.items())
            if not matches:
                continue
            results.append(
                {
                    "status": "available",
                    "bundle_id": bundle.manifest["bundle_id"],
                    "bundle_manifest_sha256": bundle.bundle_sha256,
                    "queue_sha256": bundle.queue_sha256,
                    "total_size_bytes": bundle.total_size_bytes,
                    "required_free_bytes": int(bundle.manifest["provisioning"]["attempt_quota_bytes"])
                    + bundle.total_size_bytes,
                    "available_free_bytes": shutil.disk_usage(inbox).free,
                    "capacity_status": (
                        "sufficient"
                        if shutil.disk_usage(inbox).free
                        >= int(bundle.manifest["provisioning"]["attempt_quota_bytes"]) + bundle.total_size_bytes
                        else "insufficient"
                    ),
                    "provisioner_limits": {
                        "max_files": MAX_BUNDLE_FILES,
                        "max_bundle_bytes": MAX_BUNDLE_BYTES,
                        "max_single_file_bytes": MAX_SINGLE_FILE_BYTES,
                    },
                    "attempt_quota_bytes": bundle.manifest["provisioning"]["attempt_quota_bytes"],
                    "retention": bundle.manifest["provisioning"]["retention"],
                    "target": target,
                    "inbox_entry": candidate.name,
                    "error_code": None,
                    "error": None,
                }
            )
        except ReviewEvidenceBundleError as exc:
            results.append(
                {
                    "status": "invalid",
                    "bundle_id": None,
                    "bundle_manifest_sha256": None,
                    "queue_sha256": None,
                    "total_size_bytes": None,
                    "target": None,
                    "inbox_entry": candidate.name,
                    "error_code": exc.code,
                    "error": "bundle failed fail-closed validation",
                }
            )
    return results


def _validate_trusted_prelabel_commitment_anchor(
    bundle: ValidatedReviewEvidenceBundle,
    trusted_root: Path | None,
) -> None:
    if bundle.manifest.get("artifact_type") != TARGET_BUNDLE_ARTIFACT_TYPE:
        return
    if trusted_root is None:
        raise ReviewEvidenceBundleError(
            "prelabel_commitment_anchor_required",
            "target review evidence requires the canonical server pre-label commitment registry",
        )
    queue = _load_json(bundle.queue_path, "target review queue")
    bindings = _required_mapping(queue.get("bindings"), "target review queue bindings")

    def bound_artifact(name: str) -> Path:
        descriptor = _required_mapping(
            bindings.get(name),
            f"target review queue bindings.{name}",
        )
        relative = _require_safe_relative_path(
            descriptor.get("path"),
            f"target review queue bindings.{name}.path",
        )
        artifact = _contained_nonlink_file(
            bundle.root,
            bundle.root.joinpath(*relative.parts),
            name.replace("_", " "),
        )
        if sha256_file(artifact) != _required_sha256(
            descriptor.get("sha256"),
            f"target review queue bindings.{name}.sha256",
        ):
            raise ReviewEvidenceBundleError(
                "prelabel_commitment_mismatch",
                f"target review queue {name} changed after bundle validation",
            )
        return artifact

    plan_path = bound_artifact("target_audit_plan")
    bundled_record = bound_artifact("target_prelabel_commitment")
    plan = _load_json(plan_path, "target audit plan")
    descriptor = _required_mapping(
        plan.get("external_commitment"),
        "target audit plan external commitment",
    )
    record_name = _required_text(
        descriptor.get("record_name"),
        "target audit plan external commitment record_name",
    )
    if Path(record_name).name != record_name:
        raise ReviewEvidenceBundleError(
            "prelabel_commitment_mismatch",
            "target audit plan commitment record name is unsafe",
        )
    try:
        anchored_record_bytes, registry_records = capture_target_prelabel_registry(
            trusted_root,
            record_name=record_name,
        )
    except FileNotFoundError as exc:
        raise ReviewEvidenceBundleError(
            "prelabel_commitment_anchor_missing",
            "the exact target commitment was not pre-registered in the canonical server registry",
        ) from exc
    except (OSError, TargetFinitePopulationError) as exc:
        raise ReviewEvidenceBundleError(
            "unsafe_bundle_path",
            f"canonical server pre-label commitment registry is unsafe: {exc}",
        ) from exc
    try:
        bundled_record_bytes = capture_target_prelabel_commitment_file(bundled_record)
    except (OSError, TargetFinitePopulationError) as exc:
        raise ReviewEvidenceBundleError(
            "unsafe_bundle_path",
            f"bundled target pre-label commitment is unsafe: {exc}",
        ) from exc
    if anchored_record_bytes != bundled_record_bytes:
        raise ReviewEvidenceBundleError(
            "prelabel_commitment_conflict",
            "bundle commitment differs from the canonical server pre-label commitment",
        )
    try:
        validate_target_prelabel_commitment_bytes(
            plan,
            anchored_record_bytes,
            record_name=record_name,
        )
    except (OSError, ValueError, TargetFinitePopulationError) as exc:
        raise ReviewEvidenceBundleError(
            "prelabel_commitment_mismatch",
            f"canonical target pre-label commitment is invalid: {exc}",
        ) from exc
    target_key = _required_sha256(
        descriptor.get("target_key"),
        "target audit plan external commitment target_key",
    )
    for candidate_name, candidate_bytes in registry_records:
        if candidate_name == record_name:
            continue
        try:
            candidate_payload = json.loads(candidate_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewEvidenceBundleError(
                "prelabel_commitment_conflict",
                "canonical server pre-label commitment registry contains an invalid record",
            ) from exc
        if isinstance(candidate_payload, dict) and candidate_payload.get("target_key") == target_key:
            raise ReviewEvidenceBundleError(
                "prelabel_commitment_conflict",
                "canonical server registry contains duplicate commitments for one target",
            )


def activate_review_evidence_bundle(
    bundle_dir: Path,
    output_dir: Path,
    *,
    expected_run_id: str,
    expected_source_sha256: str,
    expected_root_contract_sha256: str,
    expected_target: Mapping[str, Any] | None = None,
    expected_bundle_id: str | None = None,
    expected_bundle_manifest_sha256: str | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_stage: Callable[[str, float], None] | None = None,
    on_commit_started: Callable[[], None] | None = None,
    minimum_free_bytes: int = 0,
    trusted_prelabel_commitment_root: Path | None = None,
) -> ActivatedReviewEvidence:
    """Stage, re-hash, and atomically activate a qualified review queue for one run."""

    bundle = validate_review_evidence_bundle(bundle_dir)
    _validate_trusted_prelabel_commitment_anchor(
        bundle,
        trusted_prelabel_commitment_root,
    )
    if expected_bundle_id is not None and bundle.manifest.get("bundle_id") != expected_bundle_id:
        raise ReviewEvidenceBundleError("bundle_identity_mismatch", "requested bundle id changed before copy")
    if expected_bundle_manifest_sha256 is not None and bundle.bundle_sha256 != _required_sha256(
        expected_bundle_manifest_sha256, "expected_bundle_manifest_sha256"
    ):
        raise ReviewEvidenceBundleError("bundle_identity_mismatch", "requested bundle manifest changed before copy")
    output = _trusted_directory(output_dir, "broadcast output")
    try:
        output.relative_to(bundle.root)
    except ValueError:
        pass
    else:
        raise ReviewEvidenceBundleError("unsafe_activation_target", "broadcast output may not be inside the bundle")
    target = bundle.manifest["target"]
    expected = {
        "run_id": expected_run_id,
        "source_sha256": _required_sha256(expected_source_sha256, "expected_source_sha256"),
        "root_contract_sha256": _required_sha256(expected_root_contract_sha256, "expected_root_contract_sha256"),
    }
    expected.update(dict(expected_target or {}))
    mismatches = [name for name, value in expected.items() if target.get(name) != value]
    if mismatches:
        raise ReviewEvidenceBundleError(
            "target_binding_mismatch", f"bundle target does not match the run: {sorted(mismatches)}"
        )
    if (output / "review_decisions.json").exists():
        raise ReviewEvidenceBundleError(
            "review_evidence_fixed", "review decisions already exist; review evidence cannot be replaced"
        )

    generation_id = f"review-evidence-{bundle.bundle_sha256[:24]}"
    evidence_root = output / "review_evidence"
    generations_root = evidence_root / "generations"
    generation_dir = generations_root / generation_id
    root_queue_path = output / "selective_review_queue.v1.json"
    existing = _load_existing_activation(
        generation_dir,
        root_queue_path,
        expected_bundle_sha256=bundle.bundle_sha256,
    )
    if existing is not None:
        return existing
    if root_queue_path.exists():
        raise ReviewEvidenceBundleError(
            "review_evidence_conflict", "a different selective review queue is already active"
        )
    if generation_dir.exists():
        raise ReviewEvidenceBundleError(
            "review_evidence_generation_conflict", "an incomplete or conflicting evidence generation exists"
        )
    attempt_quota_bytes = _positive_int(
        bundle.manifest["provisioning"].get("attempt_quota_bytes"),
        "provisioning.attempt_quota_bytes",
    )
    required_free = max(0, minimum_free_bytes) + attempt_quota_bytes + bundle.total_size_bytes
    available_free = shutil.disk_usage(output).free
    if available_free < required_free:
        raise ReviewEvidenceBundleError(
            "insufficient_review_evidence_capacity",
            f"review evidence activation requires at least {required_free} free bytes",
        )
    if should_cancel is not None and should_cancel():
        raise ReviewEvidenceBundleError("review_evidence_import_cancelled", "review evidence import was cancelled")

    generations_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{generation_id}.staging-", dir=generations_root))
    staged_bundle = staging / "bundle"
    try:
        _copy_validated_bundle(bundle, staged_bundle, should_cancel=should_cancel)
        if on_stage is not None:
            on_stage("validating", 60.0)
        copied = validate_review_evidence_bundle(staged_bundle)
        source_after_copy = validate_review_evidence_bundle(bundle.root)
        if (
            copied.bundle_sha256 != bundle.bundle_sha256
            or copied.queue_sha256 != bundle.queue_sha256
            or source_after_copy.bundle_sha256 != bundle.bundle_sha256
            or source_after_copy.queue_sha256 != bundle.queue_sha256
            or copied.manifest.get("bundle_id") != bundle.manifest.get("bundle_id")
            or source_after_copy.manifest.get("bundle_id") != bundle.manifest.get("bundle_id")
        ):
            raise ReviewEvidenceBundleError(
                "bundle_changed_during_validation", "review evidence bundle changed while it was copied"
            )
        activated_queue = _activation_queue(
            copied.manifest,
            copied.queue_path,
            generation_id=generation_id,
        )
        activated_queue_path = staging / ACTIVATED_QUEUE_NAME
        _write_json(activated_queue_path, activated_queue, exclusive=True)
        activated_queue_sha256 = sha256_file(activated_queue_path)
        activation = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "artifact_type": "broadcast_review_evidence_activation",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generation_id": generation_id,
            "bundle_id": bundle.manifest["bundle_id"],
            "bundle_sha256": bundle.bundle_sha256,
            "source_queue_sha256": bundle.queue_sha256,
            "activated_queue_sha256": activated_queue_sha256,
            "target": dict(target),
            "request_identity": {
                "bundle_id": bundle.manifest["bundle_id"],
                "bundle_manifest_sha256": bundle.bundle_sha256,
                "target_sha256": _canonical_sha256(target),
            },
            "reconciliation": dict(_required_mapping(bundle.manifest.get("reconciliation"), "reconciliation")),
            "capacity": {
                "bundle_size_bytes": bundle.total_size_bytes,
                "attempt_quota_bytes": attempt_quota_bytes,
                "required_free_bytes": required_free,
                "available_free_bytes_at_commit": available_free,
            },
            "retention": dict(_required_mapping(bundle.manifest["provisioning"].get("retention"), "retention")),
            "paths": {
                "bundle": "bundle",
                "activated_queue": ACTIVATED_QUEUE_NAME,
                "root_commit_marker": "selective_review_queue.v1.json",
            },
        }
        _write_json(staging / ACTIVATION_MANIFEST_NAME, activation, exclusive=True)
        if should_cancel is not None and should_cancel():
            raise ReviewEvidenceBundleError("review_evidence_import_cancelled", "review evidence import was cancelled")
        if on_commit_started is not None:
            on_commit_started()
        os.replace(staging, generation_dir)
        _fsync_directory(generations_root)
        committed_queue_path = generation_dir / ACTIVATED_QUEUE_NAME
        try:
            _, committed_queue_sha256 = validate_review_queue_bindings(
                committed_queue_path,
                trusted_root=output,
                binding_base=output,
            )
            collect_review_evidence_paths(committed_queue_path, trusted_root=output, binding_base=output)
        except (BroadcastApiError, OSError, ValueError) as exc:
            raise ReviewEvidenceBundleError("activated_review_queue_invalid", str(exc)) from exc
        if committed_queue_sha256 != activated_queue_sha256:
            raise ReviewEvidenceBundleError("activated_review_queue_invalid", "staged activation queue hash changed")
        _publish_bytes_exclusive(root_queue_path, committed_queue_path.read_bytes())
        try:
            _, root_queue_sha256 = validate_review_queue_bindings(root_queue_path, trusted_root=output)
            collect_review_evidence_paths(root_queue_path, trusted_root=output)
        except (BroadcastApiError, OSError, ValueError) as exc:
            # The root marker is immutable. Reaching this branch is a hard integrity failure,
            # not permission to delete or replace it.
            raise ReviewEvidenceBundleError("activated_review_queue_invalid", str(exc)) from exc
        if root_queue_sha256 != activated_queue_sha256:
            raise ReviewEvidenceBundleError("activated_review_queue_invalid", "activated queue hash changed")
        return ActivatedReviewEvidence(
            generation_id=generation_id,
            generation_dir=generation_dir,
            activation_manifest_path=generation_dir / ACTIVATION_MANIFEST_NAME,
            queue_path=root_queue_path,
            queue_sha256=root_queue_sha256,
            idempotent=False,
        )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _copy_validated_bundle(
    bundle: ValidatedReviewEvidenceBundle,
    destination: Path,
    *,
    should_cancel: Callable[[], bool] | None,
) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    relative_paths = [row["path"] for row in bundle.manifest["inventory"]]
    relative_paths.append(BUNDLE_MANIFEST_NAME)
    for relative_text in sorted(relative_paths):
        if should_cancel is not None and should_cancel():
            raise ReviewEvidenceBundleError("review_evidence_import_cancelled", "review evidence import was cancelled")
        relative = _require_safe_relative_path(relative_text, "copied bundle path")
        source = _contained_nonlink_file(bundle.root, bundle.root.joinpath(*relative.parts), "copied bundle artifact")
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as source_handle, target.open("xb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())


def _stage_bundle_root_queue(source: Path, staging: Path, manifest: dict[str, Any]) -> None:
    """Rebase a producer queue onto the self-contained bundle root before inventory hashing."""

    descriptor = dict(_required_mapping(manifest.get("queue"), "queue"))
    raw_source_path = descriptor.get("source_path", descriptor.get("path"))
    source_relative = _require_safe_relative_path(raw_source_path, "queue.source_path")
    source_queue = _contained_nonlink_file(source, source.joinpath(*source_relative.parts), "producer review queue")
    declared_source_sha = descriptor.get("source_sha256", descriptor.get("sha256"))
    if sha256_file(source_queue) != _required_sha256(declared_source_sha, "queue.source_sha256"):
        raise ReviewEvidenceBundleError("bundle_inventory_mismatch", "producer review queue hash mismatch")
    staged_source_queue = _contained_nonlink_file(
        staging,
        staging.joinpath(*source_relative.parts),
        "staged producer review queue",
    )
    queue = _load_json(staged_source_queue, "producer review queue")
    bindings = _required_mapping(queue.get("bindings"), "producer review queue bindings")
    rewritten: dict[str, dict[str, Any]] = {}
    source_queue_parent = source_queue.parent
    for name, raw_binding in bindings.items():
        binding = dict(_required_mapping(raw_binding, f"producer review queue bindings.{name}"))
        raw_path = Path(_required_text(binding.get("path"), f"producer binding {name} path"))
        if raw_path.is_absolute():
            resolved = Path(os.path.abspath(raw_path))
        else:
            root_candidate = Path(os.path.abspath(source / raw_path))
            queue_candidate = Path(os.path.abspath(source_queue_parent / raw_path))
            resolved = root_candidate if root_candidate.exists() else queue_candidate
        try:
            relative = resolved.relative_to(source)
        except ValueError as exc:
            raise ReviewEvidenceBundleError(
                "unsafe_bundle_path", f"producer review queue binding {name} is outside bundle source"
            ) from exc
        staged_binding_path = _contained_nonlink_file(
            staging,
            staging / relative,
            f"producer review queue binding {name}",
        )
        if sha256_file(staged_binding_path) != _required_sha256(binding.get("sha256"), f"binding {name} sha256"):
            raise ReviewEvidenceBundleError("bundle_inventory_mismatch", f"producer binding changed: {name}")
        binding["path"] = relative.as_posix()
        rewritten[name] = binding
    root_queue = staging / ACTIVATED_QUEUE_NAME
    if root_queue.exists() and root_queue != staged_source_queue:
        raise ReviewEvidenceBundleError("bundle_output_exists", "bundle root queue already exists")
    queue["bindings"] = rewritten
    if root_queue == staged_source_queue:
        _write_json(root_queue, queue)
    else:
        _write_json(root_queue, queue, exclusive=True)
        staged_source_queue.unlink()
        empty_parent = staged_source_queue.parent
        while empty_parent != staging:
            try:
                empty_parent.rmdir()
            except OSError:
                break
            empty_parent = empty_parent.parent
    try:
        validate_review_queue_bindings(root_queue, trusted_root=staging)
        collect_review_evidence_paths(root_queue, trusted_root=staging)
    except (BroadcastApiError, OSError, ValueError) as exc:
        raise ReviewEvidenceBundleError("invalid_review_queue_bindings", str(exc)) from exc
    manifest["queue"] = {"path": ACTIVATED_QUEUE_NAME, "sha256": sha256_file(root_queue)}


def _activation_queue(manifest: Mapping[str, Any], queue_path: Path, *, generation_id: str) -> dict[str, Any]:
    queue = _load_json(queue_path, "selective review queue")
    bindings = _required_mapping(queue.get("bindings"), "review queue bindings")
    prefix = PurePosixPath("review_evidence", "generations", generation_id, "bundle")
    rewritten = {}
    for name, raw_binding in bindings.items():
        binding = dict(_required_mapping(raw_binding, f"review queue bindings.{name}"))
        relative = _require_safe_relative_path(binding.get("path"), f"review queue bindings.{name}.path")
        binding["path"] = (prefix / relative).as_posix()
        rewritten[name] = binding
    return {
        **queue,
        "bindings": rewritten,
        "activation": {
            "generation_id": generation_id,
            "bundle_id": manifest["bundle_id"],
        },
    }


def _load_existing_activation(
    generation_dir: Path,
    root_queue_path: Path,
    *,
    expected_bundle_sha256: str,
) -> ActivatedReviewEvidence | None:
    if not generation_dir.exists():
        return None
    activation_path = generation_dir / ACTIVATION_MANIFEST_NAME
    activated_queue_path = generation_dir / ACTIVATED_QUEUE_NAME
    if (generation_dir / REVOCATION_NAME).exists():
        raise ReviewEvidenceBundleError("review_evidence_revoked", "review evidence generation was revoked")
    if not activation_path.is_file() or not activated_queue_path.is_file():
        return None
    activation = _load_json(activation_path, "review evidence activation")
    if (
        activation.get("artifact_type") != "broadcast_review_evidence_activation"
        or activation.get("bundle_sha256") != expected_bundle_sha256
        or activation.get("generation_id") != generation_dir.name
    ):
        return None
    expected_queue_sha256 = _required_sha256(
        activation.get("activated_queue_sha256"), "activation.activated_queue_sha256"
    )
    if sha256_file(activated_queue_path) != expected_queue_sha256:
        raise ReviewEvidenceBundleError("review_evidence_generation_conflict", "activated generation changed")
    output = generation_dir.parents[2]
    try:
        _, validated_queue_sha256 = validate_review_queue_bindings(
            activated_queue_path,
            trusted_root=output,
            binding_base=output,
        )
        collect_review_evidence_paths(activated_queue_path, trusted_root=output, binding_base=output)
    except (BroadcastApiError, OSError, ValueError) as exc:
        raise ReviewEvidenceBundleError("activated_review_queue_invalid", str(exc)) from exc
    if validated_queue_sha256 != expected_queue_sha256:
        raise ReviewEvidenceBundleError("activated_review_queue_invalid", "activation queue hash changed")
    if not root_queue_path.exists():
        _publish_bytes_exclusive(root_queue_path, activated_queue_path.read_bytes())
    if sha256_file(root_queue_path) != expected_queue_sha256:
        raise ReviewEvidenceBundleError("review_evidence_conflict", "active root queue belongs to another bundle")
    return ActivatedReviewEvidence(
        generation_id=generation_dir.name,
        generation_dir=generation_dir,
        activation_manifest_path=activation_path,
        queue_path=root_queue_path,
        queue_sha256=expected_queue_sha256,
        idempotent=True,
    )


def _publish_bytes_exclusive(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.publish-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
            _fsync_directory(path.parent)
        except FileExistsError:
            if _is_link_or_reparse(path) or not path.is_file() or path.read_bytes() != payload:
                raise ReviewEvidenceBundleError("review_evidence_conflict", "root review queue already exists")
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _validate_queue_coverage(queue: Mapping[str, Any], *, max_windows: int) -> None:
    items = queue.get("items")
    if not isinstance(items, list):
        raise ReviewEvidenceBundleError("incomplete_review_coverage", "review queue items must be a list")
    review_item_count = _nonnegative_int(queue.get("review_item_count"), "review queue review_item_count")
    candidate_count = _positive_int(queue.get("candidate_count"), "review queue candidate_count")
    if review_item_count != len(items) or review_item_count > max_windows or review_item_count > MAX_REVIEW_WINDOWS:
        raise ReviewEvidenceBundleError("review_window_limit_exceeded", "review queue window count is invalid")
    actual_candidate_count = 0
    seen_candidates: set[str] = set()
    for item_index, item in enumerate(items):
        item = _required_mapping(item, f"review queue items[{item_index}]")
        candidates = item.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ReviewEvidenceBundleError("incomplete_review_coverage", "each review window must contain candidates")
        actual_candidate_count += len(candidates)
        for candidate_index, candidate in enumerate(candidates):
            candidate = _required_mapping(candidate, f"review queue candidate {candidate_index}")
            candidate_id = _required_text(candidate.get("candidate_id"), "review queue candidate_id")
            if candidate_id in seen_candidates:
                raise ReviewEvidenceBundleError(
                    "incomplete_review_coverage", f"candidate appears more than once: {candidate_id}"
                )
            seen_candidates.add(candidate_id)
    if actual_candidate_count != candidate_count:
        raise ReviewEvidenceBundleError("incomplete_review_coverage", "review queue candidate_count is stale")
    selection = _required_mapping(queue.get("selection"), "review queue selection")
    if selection.get("coverage_complete") is not True or selection.get("requires_additional_round") is not False:
        raise ReviewEvidenceBundleError(
            "incomplete_review_coverage", "review queue must be complete and require no additional round"
        )
    if selection.get("dropped_candidate_ids") not in ([], None) or selection.get("dropped") not in (0, None):
        raise ReviewEvidenceBundleError("incomplete_review_coverage", "review queue may not drop eligible candidates")
    selected = selection.get("selected")
    eligible = selection.get("eligible")
    if selected is not None and selected != candidate_count:
        raise ReviewEvidenceBundleError("incomplete_review_coverage", "selected candidate count is stale")
    if eligible is not None and eligible != candidate_count:
        raise ReviewEvidenceBundleError("incomplete_review_coverage", "eligible candidate count is stale")


def _build_inventory(root: Path) -> list[dict[str, Any]]:
    rows = []
    total_size = 0
    files, directories = _collect_bundle_entries(root)
    expected_directories = _inventory_parent_directories(files)
    if directories != expected_directories:
        raise ReviewEvidenceBundleError(
            "invalid_bundle_inventory",
            "bundle contains a directory that is not required by an inventoried file",
        )
    for relative, path in sorted(files.items()):
        size = path.stat().st_size
        total_size += size
        if len(rows) + 1 > MAX_BUNDLE_FILES or size > MAX_SINGLE_FILE_BYTES or total_size > MAX_BUNDLE_BYTES:
            raise ReviewEvidenceBundleError("bundle_capacity_exceeded", "bundle exceeds provisioner limits")
        rows.append({"path": relative, "sha256": sha256_file(path), "size_bytes": size})
    return rows


def _validate_inventory(root: Path, raw_inventory: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_inventory, list) or not raw_inventory:
        raise ReviewEvidenceBundleError("invalid_bundle_inventory", "bundle inventory must be a non-empty list")
    if len(raw_inventory) > MAX_BUNDLE_FILES:
        raise ReviewEvidenceBundleError("bundle_capacity_exceeded", "bundle file count exceeds provisioner limits")
    inventory: dict[str, dict[str, Any]] = {}
    total_size = 0
    for index, raw_row in enumerate(raw_inventory):
        row = dict(_required_mapping(raw_row, f"inventory[{index}]"))
        relative = _require_safe_relative_path(row.get("path"), f"inventory[{index}].path")
        key = relative.as_posix()
        if key in inventory or key == BUNDLE_MANIFEST_NAME:
            raise ReviewEvidenceBundleError("invalid_bundle_inventory", f"duplicate or reserved inventory path: {key}")
        path = _contained_nonlink_file(root, root.joinpath(*relative.parts), f"inventory file {key}")
        expected_size = _nonnegative_int(row.get("size_bytes"), f"inventory[{index}].size_bytes")
        total_size += expected_size
        if expected_size > MAX_SINGLE_FILE_BYTES or total_size > MAX_BUNDLE_BYTES:
            raise ReviewEvidenceBundleError("bundle_capacity_exceeded", "bundle size exceeds provisioner limits")
        expected_sha256 = _required_sha256(row.get("sha256"), f"inventory[{index}].sha256")
        if path.stat().st_size != expected_size or sha256_file(path) != expected_sha256:
            raise ReviewEvidenceBundleError("bundle_inventory_mismatch", f"bundle inventory changed: {key}")
        inventory[key] = {"path": path, "sha256": expected_sha256, "size_bytes": expected_size}
    actual_files, actual_directories = _collect_bundle_entries(root)
    actual = set(actual_files) - {BUNDLE_MANIFEST_NAME}
    if actual != set(inventory):
        raise ReviewEvidenceBundleError(
            "invalid_bundle_inventory",
            f"bundle inventory is not exhaustive; missing={sorted(actual - set(inventory))}, extra={sorted(set(inventory) - actual)}",
        )
    expected_directories = _inventory_parent_directories(inventory)
    if actual_directories != expected_directories:
        raise ReviewEvidenceBundleError(
            "invalid_bundle_inventory",
            "bundle inventory is not exhaustive for directories; "
            f"undeclared={sorted(actual_directories - expected_directories)}, "
            f"missing={sorted(expected_directories - actual_directories)}",
        )
    return inventory


def _collect_bundle_entries(root: Path) -> tuple[dict[str, Path], set[str]]:
    files: dict[str, Path] = {}
    directories: set[str] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise ReviewEvidenceBundleError(
                "unsafe_bundle_path", f"could not inspect bundle directory: {directory}"
            ) from exc
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            _require_safe_relative_path(relative, "inventory path")
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ReviewEvidenceBundleError(
                    "unsafe_bundle_path", f"could not inspect bundle entry: {relative}"
                ) from exc
            if _stat_is_link_or_reparse(info):
                raise ReviewEvidenceBundleError(
                    "unsafe_bundle_path", f"bundle contains a link or reparse point: {relative}"
                )
            if stat.S_ISDIR(info.st_mode):
                directories.add(relative)
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                files[relative] = path
            else:
                raise ReviewEvidenceBundleError(
                    "unsafe_bundle_path", f"bundle contains a non-regular entry: {relative}"
                )
    return files, directories


def _inventory_parent_directories(paths: Mapping[str, Any]) -> set[str]:
    directories: set[str] = set()
    for relative in paths:
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _require_inventory_path(inventory: Mapping[str, Mapping[str, Any]], relative: PurePosixPath, label: str) -> Path:
    row = inventory.get(relative.as_posix())
    if row is None:
        raise ReviewEvidenceBundleError("invalid_bundle_inventory", f"{label} is absent from inventory")
    path = row.get("path")
    if not isinstance(path, Path):
        raise ReviewEvidenceBundleError("invalid_bundle_inventory", f"{label} inventory path is invalid")
    return path


def _validate_package_descriptor_schema(
    package_name: str,
    descriptor: Mapping[str, Any],
    *,
    target_scope: bool,
) -> None:
    if package_name == "model_development":
        required_artifacts = _MODEL_DEVELOPMENT_REQUIRED_ARTIFACTS
        optional_artifacts = _NON_TARGET_OPTIONAL_ARTIFACTS
    elif package_name == "policy_qualification":
        required_artifacts = _POLICY_QUALIFICATION_REQUIRED_ARTIFACTS
        optional_artifacts = _NON_TARGET_OPTIONAL_ARTIFACTS
    elif package_name == "target_application":
        required_artifacts = _TARGET_APPLICATION_REQUIRED_ARTIFACTS | (
            _TARGET_SCOPE_REQUIRED_ARTIFACTS if target_scope else frozenset()
        )
        optional_artifacts = frozenset()
    else:
        raise ReviewEvidenceBundleError(
            "invalid_package_descriptor",
            f"unsupported package descriptor: {package_name}",
        )

    required_fields = {"root", "manifest_path"}
    for artifact_name in required_artifacts:
        required_fields.update(
            {f"{artifact_name}_path", f"{artifact_name}_sha256"}
        )
    optional_fields = {
        field
        for artifact_name in optional_artifacts
        for field in (
            f"{artifact_name}_path",
            f"{artifact_name}_sha256",
        )
    }
    actual_fields = set(descriptor)
    missing = sorted(required_fields - actual_fields)
    unknown = sorted(actual_fields - required_fields - optional_fields)
    if missing or unknown:
        raise ReviewEvidenceBundleError(
            "invalid_package_descriptor",
            (
                f"packages.{package_name} fields are not exact; "
                f"missing={missing!r}, unknown={unknown!r}"
            ),
        )
    for artifact_name in optional_artifacts:
        path_field = f"{artifact_name}_path"
        sha_field = f"{artifact_name}_sha256"
        if (path_field in descriptor) != (sha_field in descriptor):
            raise ReviewEvidenceBundleError(
                "invalid_package_descriptor",
                (
                    f"packages.{package_name}.{artifact_name} "
                    "path/hash must appear together"
                ),
            )


def _validate_declared_file(
    inventory: Mapping[str, Mapping[str, Any]],
    package_root: PurePosixPath,
    descriptor: Mapping[str, Any],
    *,
    path_field: str,
    sha_field: str,
    label: str,
) -> str:
    relative = _require_safe_relative_path(descriptor.get(path_field), f"{label}_path")
    if not relative.is_relative_to(package_root):
        raise ReviewEvidenceBundleError("invalid_package_partition", f"{label} is outside its package root")
    _require_inventory_path(inventory, relative, f"{label}_path")
    expected_sha256 = _required_sha256(descriptor.get(sha_field), f"{label}_sha256")
    if inventory[relative.as_posix()]["sha256"] != expected_sha256:
        raise ReviewEvidenceBundleError("bundle_inventory_mismatch", f"{label} hash does not match inventory")
    return expected_sha256


def _declared_file_path(
    inventory: Mapping[str, Mapping[str, Any]],
    package_root: PurePosixPath,
    descriptor: Mapping[str, Any],
    *,
    path_field: str,
    sha_field: str,
    label: str,
) -> Path:
    _validate_declared_file(
        inventory,
        package_root,
        descriptor,
        path_field=path_field,
        sha_field=sha_field,
        label=label,
    )
    relative = _require_safe_relative_path(descriptor.get(path_field), f"{label}_path")
    return _require_inventory_path(inventory, relative, f"{label}_path")


def _optional_declared_file_path(
    inventory: Mapping[str, Mapping[str, Any]],
    package_root: PurePosixPath,
    descriptor: Mapping[str, Any],
    *,
    path_field: str,
    sha_field: str,
    label: str,
) -> Path | None:
    if path_field not in descriptor and sha_field not in descriptor:
        return None
    if path_field not in descriptor or sha_field not in descriptor:
        raise ReviewEvidenceBundleError("invalid_bundle_manifest", f"{label} path/hash must appear together")
    return _declared_file_path(
        inventory,
        package_root,
        descriptor,
        path_field=path_field,
        sha_field=sha_field,
        label=label,
    )


def _declared_non_target_consumer_paths(
    inventory: Mapping[str, Mapping[str, Any]],
    package_roots: Mapping[str, PurePosixPath],
    package_descriptors: Mapping[str, Mapping[str, Any]],
    bindings: Mapping[str, Any],
    binding_packages: Mapping[str, str],
) -> list[Path]:
    package_names = ("model_development", "policy_qualification")
    declared: dict[str, str] = {}
    authority_dependencies: dict[str, set[str]] = {
        package_name: set() for package_name in package_names
    }
    authority_manifests: list[tuple[str, PurePosixPath]] = []
    for package_name in package_names:
        package_root = package_roots[package_name]
        descriptor = package_descriptors[package_name]
        for field, raw_value in descriptor.items():
            if not isinstance(field, str) or not field.endswith("_path"):
                continue
            relative = _require_safe_relative_path(
                raw_value,
                f"packages.{package_name}.{field}",
            )
            if not relative.is_relative_to(package_root):
                raise ReviewEvidenceBundleError(
                    "invalid_package_partition",
                    f"packages.{package_name}.{field} is outside its package root",
                )
            _require_inventory_path(
                inventory,
                relative,
                f"packages.{package_name}.{field}",
            )
            if field != "manifest_path":
                artifact_name = field.removesuffix("_path")
                _validate_declared_file(
                    inventory,
                    package_root,
                    descriptor,
                    path_field=field,
                    sha_field=f"{artifact_name}_sha256",
                    label=f"packages.{package_name}.{artifact_name}",
                )
            declared[relative.as_posix()] = package_name
            if field in {
                "manifest_path",
                "dataset_path",
                "annotation_resolution_path",
            }:
                authority_manifests.append((package_name, relative))
    for binding_name, raw_binding in bindings.items():
        package_name = binding_packages.get(binding_name)
        if package_name not in package_names:
            continue
        binding = _required_mapping(raw_binding, f"review queue bindings.{binding_name}")
        relative = _require_safe_relative_path(
            binding.get("path"),
            f"review queue bindings.{binding_name}.path",
        )
        if not relative.is_relative_to(package_roots[package_name]):
            raise ReviewEvidenceBundleError(
                "invalid_package_partition",
                f"review queue binding {binding_name} is outside {package_name}",
            )
        _require_inventory_path(
            inventory,
            relative,
            f"review queue bindings.{binding_name}.path",
        )
        declared[relative.as_posix()] = package_name
        if binding_name in {"model", "policy"}:
            authority_manifests.append((package_name, relative))

    for package_name, manifest_relative in sorted(set(authority_manifests)):
        manifest_path = _require_inventory_path(
            inventory,
            manifest_relative,
            f"{package_name} authority manifest",
        )
        payload = _load_json(manifest_path, f"{package_name} authority manifest")
        if payload.get("artifact_type") == "candidate_annotation_resolution":
            adjudication_relative = _validated_annotation_adjudication_queue(
                payload,
                resolution_relative=manifest_relative,
                package_root=package_roots[package_name],
                inventory=inventory,
                label=f"{package_name} annotation resolution",
            )
            if adjudication_relative is not None:
                declared[adjudication_relative.as_posix()] = package_name
        for (
            semantic_key,
            raw_path,
            raw_sha256,
            allow_declared_relocation_on_mismatch,
        ) in _extract_authority_manifest_paths(
            payload,
            label=f"{package_name} authority manifest",
        ):
            candidate, dependency_package = _resolve_declared_manifest_path(
                raw_path,
                raw_sha256=raw_sha256,
                semantic_key=semantic_key,
                declaring_manifest=manifest_relative,
                declaring_package=package_name,
                package_root=package_roots[package_name],
                package_roots=package_roots,
                inventory=inventory,
                already_declared=declared,
                allow_declared_relocation_on_mismatch=(
                    allow_declared_relocation_on_mismatch
                ),
                label=f"{package_name} authority manifest path",
            )
            declared[candidate.as_posix()] = dependency_package
            authority_dependencies[package_name].add(dependency_package)

    allowed_dependencies = {
        "model_development": {"model_development"},
        "policy_qualification": {"policy_qualification", "model_development"},
    }
    for package_name, dependencies in authority_dependencies.items():
        if not dependencies <= allowed_dependencies[package_name]:
            raise ReviewEvidenceBundleError(
                "invalid_package_partition",
                f"{package_name} has forbidden authority dependencies: {sorted(dependencies)!r}",
            )

    actual = {
        relative
        for relative in inventory
        if any(
            PurePosixPath(relative).is_relative_to(package_roots[package_name])
            for package_name in package_names
        )
    }
    unexplained = sorted(actual - set(declared))
    if unexplained:
        preview = unexplained[:10]
        raise ReviewEvidenceBundleError(
            "undeclared_non_target_artifact",
            (
                "non-target package contains files outside its validated manifest/binding "
                f"lineage: {preview!r} ({len(unexplained)} total)"
            ),
        )
    return [
        _require_inventory_path(
            inventory,
            PurePosixPath(relative),
            "declared non-target consumer artifact",
        )
        for relative in sorted(declared)
    ]


def _validated_annotation_adjudication_queue(
    resolution: Mapping[str, Any],
    *,
    resolution_relative: PurePosixPath,
    package_root: PurePosixPath,
    inventory: Mapping[str, Mapping[str, Any]],
    label: str,
) -> PurePosixPath | None:
    linked_artifacts = resolution.get("linked_artifacts")
    fixed_candidates = [
        PurePosixPath(relative)
        for relative in inventory
        if PurePosixPath(relative).is_relative_to(package_root)
        and PurePosixPath(relative).name == ADJUDICATION_QUEUE_NAME
    ]
    if linked_artifacts is None:
        if fixed_candidates:
            raise ReviewEvidenceBundleError(
                "invalid_authority_manifest",
                f"{label} has an independent adjudication queue without a validated link",
            )
        return None
    linked_artifacts = _closed_authority_mapping(
        linked_artifacts,
        required={"adjudication_queue", "derived_tracking_contract"},
        allowed={"adjudication_queue", "derived_tracking_contract"},
        label=f"{label}.linked_artifacts",
    )
    if linked_artifacts.get("adjudication_queue") != ADJUDICATION_QUEUE_NAME:
        raise ReviewEvidenceBundleError(
            "invalid_authority_manifest",
            (
                f"{label}.linked_artifacts.adjudication_queue must be "
                f"{ADJUDICATION_QUEUE_NAME!r}"
            ),
        )
    adjudication_relative = resolution_relative.parent / ADJUDICATION_QUEUE_NAME
    if (
        not adjudication_relative.is_relative_to(package_root)
        or len(fixed_candidates) != 1
        or set(fixed_candidates) != {adjudication_relative}
    ):
        raise ReviewEvidenceBundleError(
            "invalid_authority_manifest",
            f"{label} adjudication queue is missing, duplicated, or outside its package",
        )
    adjudication_path = _require_inventory_path(
        inventory,
        adjudication_relative,
        f"{label} adjudication queue",
    )
    queue = _load_json(adjudication_path, f"{label} adjudication queue")
    queue = _closed_authority_mapping(
        queue,
        required={
            "schema_version",
            "artifact_type",
            "source_resolution",
            "candidate_count",
            "candidates",
        },
        allowed={
            "schema_version",
            "artifact_type",
            "source_resolution",
            "candidate_count",
            "candidates",
        },
        label=f"{label}.adjudication_queue",
    )
    embedded_candidates = resolution.get("adjudication_queue")
    if not isinstance(embedded_candidates, list):
        raise ReviewEvidenceBundleError(
            "invalid_authority_manifest",
            f"{label}.adjudication_queue must be a list",
        )
    candidate_count = queue.get("candidate_count")
    if (
        queue.get("schema_version") != "1.0"
        or queue.get("artifact_type")
        != "candidate_annotation_adjudication_queue"
        or queue.get("source_resolution") != resolution_relative.name
        or isinstance(candidate_count, bool)
        or not isinstance(candidate_count, int)
        or candidate_count != len(embedded_candidates)
        or queue.get("candidates") != embedded_candidates
    ):
        raise ReviewEvidenceBundleError(
            "invalid_authority_manifest",
            f"{label} adjudication queue does not exactly match its resolution",
        )
    return adjudication_relative


def _extract_authority_manifest_paths(
    value: Mapping[str, Any],
    *,
    label: str,
) -> list[tuple[str, str, str, bool]]:
    artifact_type = value.get("artifact_type")
    allowed_path_locations: set[tuple[str | int, ...]] = set()
    if artifact_type == "candidate_classifier_model":
        paths = _extract_model_manifest_paths(
            value,
            label=label,
            allowed_path_locations=allowed_path_locations,
        )
    elif artifact_type == "selective_policy":
        paths = _extract_policy_manifest_paths(
            value,
            label=label,
            allowed_path_locations=allowed_path_locations,
        )
    elif artifact_type == "candidate_dataset":
        paths = _extract_dataset_manifest_paths(
            value,
            label=label,
            allowed_path_locations=allowed_path_locations,
        )
    elif artifact_type == "candidate_annotation_resolution":
        paths = _extract_annotation_manifest_paths(
            value,
            label=label,
            allowed_path_locations=allowed_path_locations,
        )
    else:
        raise ReviewEvidenceBundleError(
            "invalid_authority_manifest",
            f"{label} has unsupported artifact_type {artifact_type!r}",
        )
    _reject_unknown_authority_path_fields(
        value,
        allowed_path_locations,
        label=label,
    )
    return paths


def _extract_model_manifest_paths(
    value: Mapping[str, Any],
    *,
    label: str,
    allowed_path_locations: set[tuple[str | int, ...]],
) -> list[tuple[str, str, str, bool]]:
    allowed_fields = {
        "schema_version",
        "artifact_type",
        "model_version",
        "weights_path",
        "weights_sha256",
        "training_report_path",
        "training_report_sha256",
        "class_order",
        "supported_classes",
        "supported_mask",
        "architecture",
        "input_contract",
        "preprocessing",
        "state_shapes",
        "calibration",
        "data_binding",
        "training_config",
        "seed",
        "code_sha256",
        "runtime",
    }
    manifest = _closed_authority_mapping(
        value,
        required={
            "artifact_type",
            "weights_path",
            "weights_sha256",
            "training_report_path",
            "training_report_sha256",
        },
        allowed=allowed_fields,
        label=label,
    )
    paths = []
    for semantic_key in ("weights", "training_report"):
        path_field = f"{semantic_key}_path"
        sha_field = f"{semantic_key}_sha256"
        allowed_path_locations.add((path_field,))
        paths.append(
            (
                semantic_key,
                _required_text(manifest.get(path_field), f"{label}.{path_field}"),
                _required_sha256(manifest.get(sha_field), f"{label}.{sha_field}"),
                False,
            )
        )
    return paths


def _extract_policy_manifest_paths(
    value: Mapping[str, Any],
    *,
    label: str,
    allowed_path_locations: set[tuple[str | int, ...]],
) -> list[tuple[str, str, str, bool]]:
    policy = _closed_authority_mapping(
        value,
        required={"artifact_type"},
        allowed={
            "schema_version",
            "artifact_type",
            "generated_at",
            "status",
            "policy_version",
            "version_inputs",
            "inferential_unit",
            "evaluation_cohorts",
            "qualification_evidence",
            "decisions_artifact",
            "thresholds",
            "rules",
            "targets",
            "lineage",
            "calibration",
            "audit",
        },
        label=label,
    )
    paths: list[tuple[str, str, str, bool]] = []
    decisions_artifact = policy.get("decisions_artifact")
    if decisions_artifact is not None:
        descriptor = _closed_authority_mapping(
            decisions_artifact,
            required={"path", "sha256"},
            allowed={"path", "sha256", "content_sha256"},
            label=f"{label}.decisions_artifact",
        )
        allowed_path_locations.add(("decisions_artifact", "path"))
        paths.append(
            _authority_descriptor_path(
                "decisions",
                descriptor,
                label=f"{label}.decisions_artifact",
            )
        )

    version_inputs = policy.get("version_inputs")
    if version_inputs is not None:
        version_inputs = _closed_authority_mapping(
            version_inputs,
            required=set(),
            allowed={
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
            },
            label=f"{label}.version_inputs",
        )
        paths.extend(
            _extract_policy_lineage_paths(
                version_inputs.get("lineage"),
                location=("version_inputs", "lineage"),
                label=f"{label}.version_inputs.lineage",
                allowed_path_locations=allowed_path_locations,
            )
        )
    paths.extend(
        _extract_policy_lineage_paths(
            policy.get("lineage"),
            location=("lineage",),
            label=f"{label}.lineage",
            allowed_path_locations=allowed_path_locations,
        )
    )
    return paths


def _extract_policy_lineage_paths(
    raw_lineage: Any,
    *,
    location: tuple[str | int, ...],
    label: str,
    allowed_path_locations: set[tuple[str | int, ...]],
) -> list[tuple[str, str, str, bool]]:
    if raw_lineage is None:
        return []
    descriptor_names = {
        "predictions",
        "dataset_manifest",
        "annotation_resolution",
        "resolved_tracking_contract",
        "model_manifest",
        "training_report",
        "model_weights",
        "policy_roles",
    }
    lineage = _closed_authority_mapping(
        raw_lineage,
        required=set(),
        allowed=descriptor_names
        | {"source_contract_sha256", "dataset_version", "model_version"},
        label=label,
    )
    paths = []
    for descriptor_name in sorted(descriptor_names):
        raw_descriptor = lineage.get(descriptor_name)
        if raw_descriptor is None:
            continue
        descriptor = _closed_authority_mapping(
            raw_descriptor,
            required={"path", "sha256"},
            allowed={"path", "sha256"},
            label=f"{label}.{descriptor_name}",
        )
        allowed_path_locations.add((*location, descriptor_name, "path"))
        paths.append(
            _authority_descriptor_path(
                descriptor_name,
                descriptor,
                label=f"{label}.{descriptor_name}",
            )
        )
    return paths


def _extract_dataset_manifest_paths(
    value: Mapping[str, Any],
    *,
    label: str,
    allowed_path_locations: set[tuple[str | int, ...]],
) -> list[tuple[str, str, str, bool]]:
    dataset = _closed_authority_mapping(
        value,
        required={"artifact_type", "sources", "samples"},
        allowed={
            "schema_version",
            "artifact_type",
            "builder_version",
            "dataset_version",
            "preprocessing_runtime",
            "contract",
            "source_mapping",
            "frame_offsets",
            "tensor_contract",
            "summary",
            "sources",
            "samples",
            "purpose",
        },
        label=label,
    )
    paths: list[tuple[str, str, str, bool]] = []
    for descriptor_name in ("contract", "source_mapping"):
        raw_descriptor = dataset.get(descriptor_name)
        if raw_descriptor is None:
            continue
        if isinstance(raw_descriptor, Mapping) and set(raw_descriptor) == {
            "sha256"
        }:
            _required_sha256(
                raw_descriptor.get("sha256"),
                f"{label}.{descriptor_name}.sha256",
            )
            continue
        descriptor = _closed_authority_mapping(
            raw_descriptor,
            required={"path", "sha256"},
            allowed={"schema_version", "path", "sha256"},
            label=f"{label}.{descriptor_name}",
        )
        allowed_path_locations.add((descriptor_name, "path"))
        paths.append(
            _authority_descriptor_path(
                descriptor_name,
                descriptor,
                label=f"{label}.{descriptor_name}",
            )
        )

    sources = dataset.get("sources")
    if not isinstance(sources, list):
        raise ReviewEvidenceBundleError(
            "invalid_authority_manifest",
            f"{label}.sources must be a list",
        )
    source_fields = {
        "path",
        "sha256",
        "width",
        "height",
        "frame_count",
        "fps",
        "variant_id",
        "group_id",
        "temporal_group",
        "split_group",
        "candidate_ids",
        "requested_decode_mode",
        "effective_decode_mode",
    }
    for index, raw_source in enumerate(sources):
        source = _closed_authority_mapping(
            raw_source,
            required=set(),
            allowed=source_fields,
            label=f"{label}.sources[{index}]",
        )
        if ("path" in source) != ("sha256" in source):
            raise ReviewEvidenceBundleError(
                "invalid_authority_manifest",
                f"{label}.sources[{index}] path/hash must appear together",
            )
        if "path" in source:
            allowed_path_locations.add(("sources", index, "path"))
            paths.append(
                _authority_descriptor_path(
                    f"source_{index}",
                    source,
                    label=f"{label}.sources[{index}]",
                )
            )

    samples = dataset.get("samples")
    if not isinstance(samples, list):
        raise ReviewEvidenceBundleError(
            "invalid_authority_manifest",
            f"{label}.samples must be a list",
        )
    sample_fields = {
        "sample_id",
        "candidate_id",
        "detector_source",
        "frame_index",
        "frames",
        "bbox_requested_pixels",
        "bbox_clamped_pixels",
        "bbox_normalized",
        "confidence",
        "crop_windows",
        "variant_id",
        "group_id",
        "temporal_group",
        "split_group",
        "artifacts",
    }
    artifact_names = {"tight_tensor", "context_tensor", "review_montage"}
    for sample_index, raw_sample in enumerate(samples):
        sample = _closed_authority_mapping(
            raw_sample,
            required={"candidate_id"},
            allowed=sample_fields,
            label=f"{label}.samples[{sample_index}]",
        )
        raw_artifacts = sample.get("artifacts")
        if raw_artifacts is None:
            continue
        artifacts = _closed_authority_mapping(
            raw_artifacts,
            required=artifact_names,
            allowed=artifact_names,
            label=f"{label}.samples[{sample_index}].artifacts",
        )
        for artifact_name in sorted(artifact_names):
            descriptor = _closed_authority_mapping(
                artifacts[artifact_name],
                required={"path", "sha256"},
                allowed={
                    "path",
                    "sha256",
                    "size_bytes",
                    "shape",
                    "dtype",
                    "color_space",
                },
                label=(
                    f"{label}.samples[{sample_index}]."
                    f"artifacts.{artifact_name}"
                ),
            )
            allowed_path_locations.add(
                ("samples", sample_index, "artifacts", artifact_name, "path")
            )
            paths.append(
                _authority_descriptor_path(
                    f"sample_{sample_index}_{artifact_name}",
                    descriptor,
                    label=(
                        f"{label}.samples[{sample_index}]."
                        f"artifacts.{artifact_name}"
                    ),
                )
            )
    return paths


def _extract_annotation_manifest_paths(
    value: Mapping[str, Any],
    *,
    label: str,
    allowed_path_locations: set[tuple[str | int, ...]],
) -> list[tuple[str, str, str, bool]]:
    annotation = _closed_authority_mapping(
        value,
        required={"artifact_type"},
        allowed={
            "schema_version",
            "artifact_type",
            "qualification_scope",
            "training_eligible",
            "reusable",
            "source_contract",
            "source_vote_ledger",
            "source_dataset_manifest",
            "evidence_hash_policy",
            "linked_artifacts",
            "derived_tracking_contract",
            "min_confidence",
            "summary",
            "ledger_header",
            "vote_history",
            "resolutions",
            "adjudication_queue",
        },
        label=label,
    )
    paths: list[tuple[str, str, str, bool]] = []
    descriptors = {
        "source_contract": {"path", "sha256"},
        "source_vote_ledger": {
            "path",
            "sha256",
            "schema_version",
            "contract_sha256",
            "dataset_version",
            "evidence_manifest_sha256",
        },
        "source_dataset_manifest": {
            "path",
            "sha256",
            "dataset_version",
            "sample_count",
        },
        "derived_tracking_contract": {"path", "sha256"},
    }
    for descriptor_name, allowed_fields in descriptors.items():
        raw_descriptor = annotation.get(descriptor_name)
        if raw_descriptor is None:
            continue
        descriptor = _closed_authority_mapping(
            raw_descriptor,
            required={"path", "sha256"},
            allowed=allowed_fields,
            label=f"{label}.{descriptor_name}",
        )
        allowed_path_locations.add((descriptor_name, "path"))
        paths.append(
            _authority_descriptor_path(
                descriptor_name,
                descriptor,
                label=f"{label}.{descriptor_name}",
                allow_declared_relocation_on_mismatch=(
                    descriptor_name == "source_contract"
                ),
            )
        )
    linked_artifacts = annotation.get("linked_artifacts")
    if linked_artifacts is not None:
        _closed_authority_mapping(
            linked_artifacts,
            required={"adjudication_queue", "derived_tracking_contract"},
            allowed={"adjudication_queue", "derived_tracking_contract"},
            label=f"{label}.linked_artifacts",
        )
    return paths


def _authority_descriptor_path(
    semantic_key: str,
    descriptor: Mapping[str, Any],
    *,
    label: str,
    allow_declared_relocation_on_mismatch: bool = False,
) -> tuple[str, str, str, bool]:
    return (
        semantic_key,
        _required_text(descriptor.get("path"), f"{label}.path"),
        _required_sha256(descriptor.get("sha256"), f"{label}.sha256"),
        allow_declared_relocation_on_mismatch,
    )


def _closed_authority_mapping(
    value: Any,
    *,
    required: set[str],
    allowed: set[str],
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReviewEvidenceBundleError(
            "invalid_authority_manifest",
            f"{label} must be an object",
        )
    fields = set(value)
    missing = sorted(required - fields)
    unknown = sorted(
        (str(field) for field in fields - allowed),
    )
    if missing or unknown:
        raise ReviewEvidenceBundleError(
            "invalid_authority_manifest",
            f"{label} fields are not exact; missing={missing!r}, unknown={unknown!r}",
        )
    return value


def _reject_unknown_authority_path_fields(
    value: Any,
    allowed_locations: set[tuple[str | int, ...]],
    *,
    label: str,
    location: tuple[str | int, ...] = (),
) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_location = (*location, key)
            if (key == "path" or key.endswith("_path")) and child_location not in allowed_locations:
                raise ReviewEvidenceBundleError(
                    "invalid_authority_manifest",
                    f"{label} contains an unrecognized path field at {child_location!r}",
                )
            _reject_unknown_authority_path_fields(
                child,
                allowed_locations,
                label=label,
                location=child_location,
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_unknown_authority_path_fields(
                child,
                allowed_locations,
                label=label,
                location=(*location, index),
            )


def _resolve_declared_manifest_path(
    raw_path: str,
    *,
    raw_sha256: str | None,
    semantic_key: str,
    declaring_manifest: PurePosixPath,
    declaring_package: str,
    package_root: PurePosixPath,
    package_roots: Mapping[str, PurePosixPath],
    inventory: Mapping[str, Mapping[str, Any]],
    already_declared: Mapping[str, str],
    label: str,
    allow_declared_relocation_on_mismatch: bool = False,
) -> tuple[PurePosixPath, str]:
    relative = _require_safe_relative_path(raw_path, label)
    candidates = (
        declaring_manifest.parent / relative,
        package_root / relative,
    )
    for candidate in candidates:
        normalized = PurePosixPath(*candidate.parts)
        if normalized.is_relative_to(package_root) and normalized.as_posix() in inventory:
            if raw_sha256 is not None and inventory[normalized.as_posix()]["sha256"] != _required_sha256(
                raw_sha256,
                f"{label} {semantic_key}_sha256",
            ):
                if allow_declared_relocation_on_mismatch:
                    continue
                raise ReviewEvidenceBundleError(
                    "undeclared_non_target_artifact",
                    f"{label} hash does not match its inventoried file",
                )
            return normalized, declaring_package
    if raw_sha256 is None:
        raise ReviewEvidenceBundleError(
            "undeclared_non_target_artifact",
            (
                f"{label} cannot use a relocated authoritative binding without "
                f"{semantic_key}_sha256"
            ),
        )
    expected_sha256 = _required_sha256(
        raw_sha256,
        f"{label} {semantic_key}_sha256",
    )
    allowed_dependency_packages = {declaring_package}
    if declaring_package == "policy_qualification":
        allowed_dependency_packages.add("model_development")
    authoritative_matches = [
        (PurePosixPath(candidate), dependency_package)
        for candidate, dependency_package in already_declared.items()
        if dependency_package in allowed_dependency_packages
        and PurePosixPath(candidate).is_relative_to(package_roots[dependency_package])
        and PurePosixPath(candidate).name == relative.name
        and inventory[candidate]["sha256"] == expected_sha256
    ]
    if len(authoritative_matches) == 1:
        return authoritative_matches[0]
    raise ReviewEvidenceBundleError(
        "undeclared_non_target_artifact",
        (
            f"{label} does not resolve to an inventoried file or one unique "
            f"allowed already-authoritative binding for {declaring_package}"
        ),
    )


def _dataset_population(path: Path, label: str) -> dict[str, set[str]]:
    dataset = _load_json(path, label)
    samples = dataset.get("samples")
    sources = dataset.get("sources")
    if not isinstance(samples, list) or not samples or not isinstance(sources, list) or not sources:
        raise ReviewEvidenceBundleError("empty_evidence_population", f"{label} must be non-empty")
    result = {
        name: set()
        for name in ("candidate_id", "variant_id", "video_sha256", "group_id", "split_group", "temporal_group")
    }
    for index, sample in enumerate(samples):
        sample = _required_mapping(sample, f"{label}.samples[{index}]")
        result["candidate_id"].add(_required_text(sample.get("candidate_id"), f"{label} candidate_id"))
    for index, source in enumerate(sources):
        source = _required_mapping(source, f"{label}.sources[{index}]")
        result["variant_id"].add(_required_text(source.get("variant_id"), f"{label} variant_id"))
        result["video_sha256"].add(_required_sha256(source.get("sha256"), f"{label} source sha256"))
        for field in ("group_id", "split_group", "temporal_group"):
            result[field].add(_required_text(source.get(field), f"{label} source {field}"))
    return result


def _validate_population_independence(
    development_dataset_path: Path,
    qualification_dataset_path: Path,
    application_dataset_path: Path,
) -> None:
    populations = {
        "model_development": _dataset_population(development_dataset_path, "model development dataset"),
        "policy_qualification": _dataset_population(qualification_dataset_path, "policy qualification dataset"),
        "target_application": _dataset_population(application_dataset_path, "target application dataset"),
    }
    names = sorted(populations)
    for left_index, left in enumerate(names):
        for right in names[left_index + 1 :]:
            for dimension in populations[left]:
                overlap = populations[left][dimension] & populations[right][dimension]
                if overlap:
                    raise ReviewEvidenceBundleError(
                        "evidence_population_leakage",
                        f"{left}/{right} overlap on {dimension}",
                    )


def _build_reconciliation(root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    packages = _required_mapping(manifest.get("packages"), "packages")
    application = _required_mapping(packages.get("target_application"), "packages.target_application")

    def artifact(field: str) -> Path:
        relative = _require_safe_relative_path(application.get(f"{field}_path"), f"target_application.{field}_path")
        return _contained_nonlink_file(root, root.joinpath(*relative.parts), f"target application {field}")

    contract = _load_json(artifact("root_contract"), "target root contract")
    dataset = _load_json(artifact("dataset"), "target dataset")
    predictions = _load_json(artifact("predictions"), "target predictions")
    decisions = _load_json(artifact("decisions"), "target decisions")
    queue_descriptor = _required_mapping(manifest.get("queue"), "queue")
    queue_relative = _require_safe_relative_path(queue_descriptor.get("path"), "queue.path")
    queue = _load_json(
        _contained_nonlink_file(root, root.joinpath(*queue_relative.parts), "target review queue"),
        "target review queue",
    )

    def indexed(rows: Any, name: str) -> dict[str, Mapping[str, Any]]:
        if not isinstance(rows, list) or not rows:
            raise ReviewEvidenceBundleError("empty_evidence_population", f"{name} must be non-empty")
        result: dict[str, Mapping[str, Any]] = {}
        for index, raw in enumerate(rows):
            row = _required_mapping(raw, f"{name}[{index}]")
            candidate_id = _required_text(row.get("candidate_id"), f"{name}[{index}].candidate_id")
            if candidate_id in result:
                raise ReviewEvidenceBundleError("target_reconciliation_mismatch", f"duplicate {name} candidate")
            result[candidate_id] = row
        return result

    candidates = indexed(contract.get("candidates"), "contract candidates")
    samples = indexed(dataset.get("samples"), "dataset samples")
    prediction_rows = indexed(predictions.get("predictions"), "prediction rows")
    decision_rows = indexed(decisions.get("decisions"), "decision rows")
    expected_ids = set(candidates)
    if any(set(rows) != expected_ids for rows in (samples, prediction_rows, decision_rows)):
        raise ReviewEvidenceBundleError(
            "target_reconciliation_mismatch", "contract/dataset/predictions/decisions candidate ids differ"
        )
    queue_ids: set[str] = set()
    for item in queue.get("items", []):
        item = _required_mapping(item, "review queue item")
        for raw_candidate in item.get("candidates", []):
            candidate = _required_mapping(raw_candidate, "review queue candidate")
            candidate_id = _required_text(candidate.get("candidate_id"), "review queue candidate_id")
            if candidate_id in queue_ids:
                raise ReviewEvidenceBundleError("target_reconciliation_mismatch", "duplicate queue candidate")
            queue_ids.add(candidate_id)
    if queue_ids != expected_ids:
        raise ReviewEvidenceBundleError(
            "target_reconciliation_mismatch", "review queue does not exactly cover the target population"
        )
    rows = []
    for candidate_id in sorted(expected_ids):
        prediction_fingerprint = _required_sha256(
            prediction_rows[candidate_id].get("candidate_fingerprint"), "prediction candidate_fingerprint"
        )
        decision_fingerprint = _required_sha256(
            decision_rows[candidate_id].get("candidate_fingerprint"), "decision candidate_fingerprint"
        )
        if prediction_fingerprint != decision_fingerprint:
            raise ReviewEvidenceBundleError(
                "target_reconciliation_mismatch", f"candidate fingerprint mismatch: {candidate_id}"
            )
        decision = decision_rows[candidate_id].get("decision")
        if decision not in {"accept", "reject", "abstain"}:
            raise ReviewEvidenceBundleError("target_reconciliation_mismatch", "invalid target decision")
        rows.append(
            {
                "candidate_id": candidate_id,
                "candidate_fingerprint": decision_fingerprint,
                "decision": decision,
                "queued_for_review": candidate_id in queue_ids,
                "excluded_existing_decision": bool(decision_rows[candidate_id].get("existing_decision_preserved")),
            }
        )
    population_identity = [
        {"candidate_id": row["candidate_id"], "candidate_fingerprint": row["candidate_fingerprint"]} for row in rows
    ]
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "artifact_type": "broadcast_review_evidence_reconciliation",
        "candidate_population_sha256": _canonical_sha256(population_identity),
        "summary": {
            "candidate_count": len(rows),
            "accept_count": sum(row["decision"] == "accept" for row in rows),
            "reject_count": sum(row["decision"] == "reject" for row in rows),
            "abstain_count": sum(row["decision"] == "abstain" for row in rows),
            "review_count": sum(row["queued_for_review"] for row in rows),
            "exclusion_count": sum(row["excluded_existing_decision"] for row in rows),
        },
        "candidates": rows,
    }


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    ).hexdigest()


def _require_safe_relative_path(value: Any, label: str) -> PurePosixPath:
    text = _required_text(value, label)
    if "\\" in text:
        raise ReviewEvidenceBundleError("unsafe_bundle_path", f"{label} must use bundle-relative POSIX syntax")
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ReviewEvidenceBundleError("unsafe_bundle_path", f"{label} must be contained and relative")
    if len(path.parts[0]) >= 2 and path.parts[0][1:2] == ":":
        raise ReviewEvidenceBundleError("unsafe_bundle_path", f"{label} must not be drive-qualified")
    return path


def _trusted_directory(path: Path, label: str) -> Path:
    candidate = Path(os.path.abspath(path))
    if _is_link_or_reparse(candidate) or not candidate.is_dir():
        raise ReviewEvidenceBundleError("unsafe_bundle_path", f"{label} must be a non-link directory")
    return candidate


def _contained_nonlink_file(root: Path, path: Path, label: str) -> Path:
    candidate = Path(os.path.abspath(path))
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ReviewEvidenceBundleError("unsafe_bundle_path", f"{label} is outside the bundle") from exc
    current = root
    for part in candidate.relative_to(root).parts:
        current = current / part
        if _is_link_or_reparse(current):
            raise ReviewEvidenceBundleError("unsafe_bundle_path", f"{label} contains a link")
    if not candidate.is_file():
        raise ReviewEvidenceBundleError("missing_bundle_artifact", f"{label} is unavailable")
    return candidate


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return _stat_is_link_or_reparse(info)


def _stat_is_link_or_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            raise ReviewEvidenceBundleError("bundle_capacity_exceeded", f"{label} exceeds the JSON size limit")
    except OSError as exc:
        raise ReviewEvidenceBundleError("invalid_bundle_json", f"could not stat {label}") from exc
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewEvidenceBundleError("invalid_bundle_json", f"could not read {label}") from exc
    if not isinstance(payload, dict):
        raise ReviewEvidenceBundleError("invalid_bundle_json", f"{label} must be an object")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any], *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "x" if exclusive else "w"
    with path.open(mode, encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _required_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ReviewEvidenceBundleError("invalid_bundle_manifest", f"{label} must be an object")
    return value


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewEvidenceBundleError("invalid_bundle_manifest", f"{label} must be non-empty text")
    return value.strip()


def _required_sha256(value: Any, label: str) -> str:
    text = _required_text(value, label).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ReviewEvidenceBundleError("invalid_bundle_manifest", f"{label} must be a SHA-256 digest")
    return text


def _required_timestamp(value: Any, label: str) -> str:
    text = _required_text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReviewEvidenceBundleError("invalid_bundle_manifest", f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ReviewEvidenceBundleError("invalid_bundle_manifest", f"{label} must include a timezone")
    return text


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReviewEvidenceBundleError("invalid_bundle_manifest", f"{label} must be a non-negative integer")
    return value


def _positive_int(value: Any, label: str) -> int:
    result = _nonnegative_int(value, label)
    if result == 0:
        raise ReviewEvidenceBundleError("invalid_bundle_manifest", f"{label} must be positive")
    return result
